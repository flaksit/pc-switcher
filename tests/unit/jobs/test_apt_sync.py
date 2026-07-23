"""Unit tests for AptSyncJob and the shared PackageSyncJob plan()/apply() split.

Covers the tracer's single path — one apt package missing on the target — through
capture, diff, plan/apply separation, the coordinator-accepted-plan ordering guard,
converge (with the apt-get -s transaction guard), dry-run, continue-on-failure, and
validate(). All executor interactions are mocked; no real apt/dpkg/sudo commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob, simulate_apt_transaction
from pcswitcher.jobs.package_items import AptPackageItem, DiffAction, DiffClass, ItemClass
from pcswitcher.jobs.package_review import Decision, ReviewOutcome
from pcswitcher.jobs.package_sync_core import ConvergeItemFailed, PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, Host
from pcswitcher.orchestrator import Orchestrator

SHOWMANUAL_3 = "pkg-a\npkg-b\npkg-c\n"
DPKG_QUERY_3 = "pkg-a\t1.0\npkg-b\t2.0\npkg-c\t3.0\n"

# Empty-package, empty-repo-state baseline for both machines: every `find /etc/apt/*`
# listing and `apt-mark showmanual` returns nothing unless a test overrides one entry,
# so a repo-state test only has to specify the directories it actually cares about.
_NO_PACKAGES = {"apt-mark showmanual": CommandResult(0, "", "")}


def sha256_line(digest: str, filename: str) -> str:
    """One `sha256sum`-shaped line: `<digest>  <filename>\\n`."""
    return f"{digest}  {filename}\n"


def respond_to(
    mapping: dict[str, CommandResult], default: CommandResult | None = None
) -> Callable[..., CommandResult]:
    """Build a run_command side_effect matching by substring (first match wins)."""
    fallback = default if default is not None else CommandResult(exit_code=0, stdout="", stderr="")

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


def make_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target = MagicMock()
    target.run_command = AsyncMock(side_effect=respond_to(target_responses or {}))
    target.send_file = AsyncMock(return_value=None)
    context = JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
    )
    return context, source, target


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


class TestCapture:
    """Capture: apt-mark showmanual + one batched dpkg-query call for versions (D-03)."""

    @pytest.mark.asyncio
    async def test_capture_source_items_returns_three_items_with_versions(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            }
        )
        job = AptSyncJob(context)

        items = await job.capture_source_items()

        assert [item.name for item in items] == ["pkg-a", "pkg-b", "pkg-c"]
        assert [item.version for item in items] == ["1.0", "2.0", "3.0"]

    @pytest.mark.asyncio
    async def test_dpkg_query_used_not_apt_list_installed(self) -> None:
        """Backstop: versions come from dpkg-query, never `apt list --installed`."""
        context, source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            }
        )
        job = AptSyncJob(context)

        await job.capture_source_items()

        commands = all_calls(source)
        assert any("dpkg-query" in cmd for cmd in commands)
        assert not any("apt list" in cmd for cmd in commands)


class TestDiff:
    """Target query + diff: the tracer's MISSING_ON_TARGET/INSTALL slice."""

    @pytest.mark.asyncio
    async def test_diff_yields_exactly_two_missing_items(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-b\t2.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        source_items = await job.capture_source_items()
        target_items = await job.query_target_items()
        diffs = job.diff_items(source_items, target_items)

        assert len(diffs) == 2
        assert {d.item_id for d in diffs} == {"apt:package:pkg-a", "apt:package:pkg-c"}
        assert all(d.diff_class == DiffClass.MISSING_ON_TARGET for d in diffs)

    def test_extra_on_target_yields_extra_on_target_remove(self) -> None:
        """A name on the target but not the source yields EXTRA_ON_TARGET/REMOVE
        (plan 02-05 — the tracer's own boundary note for this case no longer holds)."""
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [
            AptPackageItem(name="pkg-a", version="1.0"),
            AptPackageItem(name="pkg-extra", version="9.9"),
        ]
        context, _source, _target = make_context()
        job = AptSyncJob(context)

        diffs = job.diff_items(source_items, target_items)

        assert len(diffs) == 1
        assert diffs[0].item_id == "apt:package:pkg-extra"
        assert diffs[0].diff_class == DiffClass.EXTRA_ON_TARGET
        assert diffs[0].action == DiffAction.REMOVE


class TestPlanApplySplit:
    """plan() issues only read commands; execute() refuses without an accepted plan."""

    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_command(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 3
        for cmd in all_calls(target):
            # `apt-get -s` (simulate) IS expected during plan() — plan 02-05's
            # plan-time collateral simulation is read-only by design (D-24/T-02-32).
            # `sudo find ... sha256sum` IS also expected — plan 02-06's repo-state
            # capture reads `/etc/apt/*` via sudo to guarantee access regardless of
            # file permissions; it is a read, never a write (D-11/D-12/D-13).
            assert "apt-get install" not in cmd
            assert "sudo install" not in cmd
            assert "sudo rm" not in cmd
            assert "sudo apt-get" not in cmd
            assert "sudo cp" not in cmd

    @pytest.mark.asyncio
    async def test_execute_without_accepted_plan_raises_naming_coordinator(self) -> None:
        context, _source, _target = make_context()
        job = AptSyncJob(context)

        with pytest.raises(RuntimeError, match="PackagePhaseCoordinator"):
            await job.execute()

    @pytest.mark.asyncio
    async def test_execute_reraises_stored_plan_failure(self) -> None:
        context, _source, _target = make_context(
            source_responses={"apt-mark showmanual": CommandResult(1, "", "boom")}
        )
        job = AptSyncJob(context)
        failure = RuntimeError("plan blew up")
        job.record_plan_failure(failure)

        with pytest.raises(RuntimeError, match="plan blew up"):
            await job.execute()


def _accept(job: AptSyncJob, plan: Any, decisions: dict[str, Decision]) -> None:
    job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))


class TestConverge:
    """Only APPLY-decided items reach the target; SKIP_ONCE items reach no command."""

    @pytest.mark.asyncio
    async def test_only_apply_decision_installs_skip_once_never_sent(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t2.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.SKIP_ONCE})

        await job.execute()

        # pkg-b legitimately appears in the plan-time BATCHED simulation command
        # (both pkg-a and pkg-b are missing-on-target candidates before any decision
        # exists) — the guarantee under test is that no REAL install command names it.
        commands = all_calls(target)
        real_installs = [c for c in commands if "sudo" in c and "apt-get install" in c]
        assert any("pkg-a" in cmd for cmd in real_installs)
        assert not any("pkg-b" in cmd for cmd in real_installs)


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_issues_no_mutating_command(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
            dry_run=True,
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        # `apt-get -s` (read-only plan-time collateral simulation) still runs even
        # under dry_run — dry_run only suppresses the REAL mutating command.
        for cmd in all_calls(target):
            assert "apt-get install" not in cmd


class TestContinueOnFailure:
    @pytest.mark.asyncio
    async def test_second_of_three_fails_all_attempted_one_failure_raised(self) -> None:
        clean_preview = CommandResult(0, "Inst dummy (1.0)\n", "")
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": clean_preview,
                "apt-get -s install -y --no-install-recommends pkg-b": clean_preview,
                "apt-get -s install -y --no-install-recommends pkg-c": clean_preview,
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-b": (
                    CommandResult(1, "", "dpkg error for pkg-b")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-c": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(
            job,
            plan,
            {
                "apt:package:pkg-a": Decision.APPLY,
                "apt:package:pkg-b": Decision.APPLY,
                "apt:package:pkg-c": Decision.APPLY,
            },
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        assert exc_info.value.failures[0][0].item_id == "apt:package:pkg-b"

        commands = all_calls(target)
        real_installs = [c for c in commands if "sudo" in c and "apt-get install" in c]
        assert len(real_installs) == 3
        simulations = [c for c in commands if "apt-get -s" in c]
        # 1 batched plan-time simulation (all three candidates) + 1 apply-time
        # simulation per approved item (D-24/T-02-32's two-layer guard).
        assert len(simulations) == 4


class TestTransactionGuard:
    @pytest.mark.asyncio
    async def test_collateral_removal_refuses_install_and_names_the_package(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv other-pkg [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "other-pkg" in message

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_clean_simulation_proceeds_to_real_install(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_failed_simulation_raises_instead_of_returning_empty_preview(self) -> None:
        """WR-01 regression: `simulate_apt_transaction` must not silently parse a
        failed `apt-get -s` (dpkg lock contention, unmet dependencies, ...) as an
        empty, falsely-clean preview — that would let both call sites proceed with
        the real command as if nothing would happen.
        """
        target = MagicMock()
        target.run_command = AsyncMock(
            return_value=CommandResult(100, "", "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'")
        )

        with pytest.raises(ConvergeItemFailed, match="dpkg was interrupted"):
            await simulate_apt_transaction(target, "install -y --no-install-recommends pkg-a", login_shell=False)

    @pytest.mark.asyncio
    async def test_apply_time_simulation_failure_fails_the_item_not_silently_clean(self) -> None:
        """A plan-time simulation can succeed (nothing wrong yet) while the same
        command fails when re-run at apply time; the item must fail cleanly through
        the normal per-item path rather than the real `apt-get install` running
        against an untrustworthy preview.
        """
        install_cmd = "apt-get -s install -y --no-install-recommends pkg-a"
        state = {"calls": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == install_cmd:
                state["calls"] += 1
                if state["calls"] == 1:
                    return CommandResult(0, "Inst pkg-a (1.0)\n", "")
                return CommandResult(100, "", "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'")
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "dpkg was interrupted" in message

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)


class TestHoldPinCapture:
    """collect_hold_pin_facts: apt-mark showhold on BOTH machines + preferences.d pins."""

    @pytest.mark.asyncio
    async def test_holds_from_both_machines_surface(self) -> None:
        context, _source, _target = make_context(
            source_responses={"apt-mark showhold": CommandResult(0, "pkg-src-held\n", "")},
            target_responses={"apt-mark showhold": CommandResult(0, "pkg-tgt-held\n", "")},
        )
        job = AptSyncJob(context)

        facts = await job.collect_hold_pin_facts()

        packages = {fact.package for fact in facts}
        assert "pkg-src-held" in packages
        assert "pkg-tgt-held" in packages
        assert all(fact.mechanism == "hold" for fact in facts)

    @pytest.mark.asyncio
    async def test_preferences_d_pin_surfaces_with_pin_mechanism_and_filename(self) -> None:
        context, _source, _target = make_context(
            target_responses={
                "find /etc/apt/preferences.d": CommandResult(0, "/etc/apt/preferences.d/curl-pin\tcurl\n", ""),
            },
        )
        job = AptSyncJob(context)

        facts = await job.collect_hold_pin_facts()

        pins = [fact for fact in facts if fact.mechanism == "pin"]
        assert len(pins) == 1
        assert pins[0].package == "curl"
        assert pins[0].source_ref == "/etc/apt/preferences.d/curl-pin"

    @pytest.mark.asyncio
    async def test_hold_and_pin_wired_end_to_end_both_held_or_pinned_and_distinguishable(self) -> None:
        """Must-have: a hold and a pin, read from their two different sources, both
        surface as HELD_OR_PINNED in the SAME plan(), and stay distinguishable facts.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "curl\nheld-pkg\n", ""),
                "dpkg-query": CommandResult(0, "curl\t1.0\nheld-pkg\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "curl\nheld-pkg\n", ""),
                "dpkg-query": CommandResult(0, "curl\t1.0\nheld-pkg\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "held-pkg\n", ""),
                "find /etc/apt/preferences.d": CommandResult(0, "/etc/apt/preferences.d/curl-pin\tcurl\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {diff.item_id: diff for diff in plan.diffs}
        curl_diff = by_id["apt:package:curl"]
        held_diff = by_id["apt:package:held-pkg"]
        assert curl_diff.diff_class == DiffClass.HELD_OR_PINNED
        assert held_diff.diff_class == DiffClass.HELD_OR_PINNED
        assert curl_diff.detail != held_diff.detail


class TestUnavailableCapture:
    """collect_unavailable_item_ids: one batched apt-cache policy call over the
    missing-on-target set — a `Candidate: (none)` package is REPO_UNAVAILABLE, not
    proposed as an INSTALL.
    """

    @pytest.mark.asyncio
    async def test_no_candidate_package_is_reported_not_installed(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "dpkg-query": CommandResult(0, "brscan3\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(
                    0, "brscan3:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 1
        assert plan.diffs[0].diff_class == DiffClass.REPO_UNAVAILABLE
        assert plan.diffs[0].action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_batched_single_apt_cache_policy_call_for_multiple_missing_packages(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, "pkg-a:\n  Candidate: 1.0\npkg-b:\n  Candidate: (none)\n", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        policy_calls = [cmd for cmd in all_calls(target) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        assert "pkg-a" in policy_calls[0]
        assert "pkg-b" in policy_calls[0]

        by_id = {diff.item_id: diff for diff in plan.diffs}
        assert by_id["apt:package:pkg-a"].diff_class == DiffClass.MISSING_ON_TARGET
        assert by_id["apt:package:pkg-b"].diff_class == DiffClass.REPO_UNAVAILABLE


class TestRemovalConverge:
    @pytest.mark.asyncio
    async def test_remove_diff_issues_real_apt_get_remove_for_that_package_alone(self) -> None:
        context, _source, target = make_context(
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-extra\n", ""),
                "dpkg-query": CommandResult(0, "pkg-extra\t1.0\n", ""),
                "apt-get -s remove -y pkg-extra": CommandResult(0, "Remv pkg-extra [1.0]\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y pkg-extra": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-extra": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        real_removals = [cmd for cmd in commands if "sudo" in cmd and "apt-get remove" in cmd]
        assert len(real_removals) == 1
        assert "pkg-extra" in real_removals[0]
        assert not any("apt-get install" in cmd for cmd in commands)


class TestRemovalGuard:
    """ "Removes nothing the user did not approve", not "removes nothing else"."""

    @pytest.mark.asyncio
    async def test_unapproved_collateral_removal_refuses_and_names_the_package(self) -> None:
        context, _source, target = make_context(
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-get -s remove -y pkg-a": CommandResult(0, "Remv pkg-a [1.0]\nRemv pkg-b [1.0]\n", ""),
            },
        )
        job = AptSyncJob(context)
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]
        diffs = job.diff_items([], target_items)
        plan = PackagePlan(manager="apt", diffs=diffs, groups=job._build_review_groups(diffs))
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "pkg-b" in message

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_both_removals_approved_the_first_proceeds(self) -> None:
        context, _source, target = make_context(
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
                "apt-get -s remove -y pkg-a": CommandResult(0, "Remv pkg-a [1.0]\nRemv pkg-b [1.0]\n", ""),
                "apt-get -s remove -y pkg-b": CommandResult(0, "Remv pkg-b [1.0]\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y pkg-a": CommandResult(0, "", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y pkg-b": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        target_items = [AptPackageItem(name="pkg-a", version="1.0"), AptPackageItem(name="pkg-b", version="1.0")]
        diffs = job.diff_items([], target_items)
        plan = PackagePlan(manager="apt", diffs=diffs, groups=job._build_review_groups(diffs))
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        real_removals = [cmd for cmd in commands if "sudo" in cmd and "apt-get remove" in cmd]
        assert any("pkg-a" in cmd for cmd in real_removals)
        assert any("pkg-b" in cmd for cmd in real_removals)


class TestDowngradeGuard:
    @pytest.mark.asyncio
    async def test_downgrade_in_install_simulation_refuses_and_names_the_downgrade(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t2.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a [2.0] (1.0)\n", ""
                ),
                "dpkg --compare-versions 1.0 lt 2.0": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "downgrade" in message.lower()

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)


class TestPlanTimeCollateral:
    """Two BATCHED simulations at plan time surface collateral before any decision."""

    @pytest.mark.asyncio
    async def test_collateral_removal_surfaces_as_report_only_in_its_own_group(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv unrelated-pkg [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = [diff for diff in plan.diffs if diff.item_id == "apt:collateral:unrelated-pkg"]
        assert len(collateral) == 1
        assert collateral[0].action == DiffAction.REPORT_ONLY
        assert collateral[0].label == "unrelated-pkg"
        assert collateral[0].detail is not None
        assert "removed" in collateral[0].detail

        report_group = next(group for group in plan.groups if group.action == "report_only")
        install_group = next(group for group in plan.groups if group.action == "install")
        assert "apt:collateral:unrelated-pkg" in {entry.item_id for entry in report_group.entries}
        assert "apt:collateral:unrelated-pkg" not in {entry.item_id for entry in install_group.entries}

    @pytest.mark.asyncio
    async def test_clean_simulation_produces_no_collateral_entry(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 1
        assert plan.diffs[0].item_id == "apt:package:pkg-a"

    @pytest.mark.asyncio
    async def test_at_most_two_apt_get_dash_s_commands_regardless_of_package_count(self) -> None:
        names = [f"pkg-{i}" for i in range(10)]
        showmanual = "\n".join(names) + "\n"
        dpkg_query = "\n".join(f"{name}\t1.0" for name in names) + "\n"
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, showmanual, ""),
                "dpkg-query": CommandResult(0, dpkg_query, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 10
        simulations = [cmd for cmd in all_calls(target) if "apt-get -s" in cmd]
        assert len(simulations) <= 2


class TestValidate:
    @pytest.mark.asyncio
    async def test_all_checks_pass_returns_no_errors(self) -> None:
        # fuser exits 1 (not 0) when the lock file is NOT held (man fuser EXIT CODES) —
        # the "all clear" baseline, unlike every other check here where 0 means success.
        context, _source, _target = make_context(
            target_responses={"fuser /var/lib/dpkg/lock-frontend": CommandResult(1, "", "")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert errors == []

    @pytest.mark.asyncio
    async def test_apt_mark_unavailable_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            target_responses={"apt-mark --version": CommandResult(127, "", "not found")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert any("apt-mark" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_dpkg_lock_held_yields_distinct_validation_error(self) -> None:
        context, _source, _target = make_context(
            target_responses={"fuser /var/lib/dpkg/lock-frontend": CommandResult(0, "1234", "")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert any("lock" in e.message.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_source_without_passwordless_sudo_yields_validation_error(self) -> None:
        """Capturing /etc/apt state needs `sudo find` on the SOURCE.

        Without this check the capture degrades to empty digest maps and the sync
        reports success having replicated no repository state at all.
        """
        context, _source, _target = make_context(
            source_responses={"sudo -n true": CommandResult(1, "", "sudo: a password is required")},
            target_responses={"fuser /var/lib/dpkg/lock-frontend": CommandResult(1, "", "")},
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "sudo" in e.message for e in errors)


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_apt_sync_to_apt_sync_job(self) -> None:
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("apt_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is AptSyncJob


# -- Task 1: repository/key/pin/config capture and diff (plan 02-06) -------------------

_DEB822_FOO = (
    "Types: deb\nURIs: https://example.com\nSuites: stable\nComponents: main\nSigned-By: /etc/apt/keyrings/foo.gpg\n"
)
_LEGACY_BAR = "deb [signed-by=/etc/apt/keyrings/bar.gpg] https://example.com stable main\n"


class TestRepoStateCapture:
    """AptSyncJob.plan() extended with source/key/pin/config diffs (D-11/D-12/D-13)."""

    @pytest.mark.asyncio
    async def test_deb822_and_legacy_source_each_record_own_format(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("d1", "foo.sources") + sha256_line("d2", "bar.list"), ""
                ),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "cat /etc/apt/sources.list.d/bar.list": CommandResult(0, _LEGACY_BAR, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        foo_diff = by_id["apt:source:foo.sources"]
        bar_diff = by_id["apt:source:bar.list"]
        assert "deb822" in foo_diff.label
        assert "list" in bar_diff.label
        assert foo_diff.item_class == ItemClass.APT_SOURCE
        assert bar_diff.item_class == ItemClass.APT_SOURCE

    @pytest.mark.asyncio
    async def test_source_with_key_present_on_source_yields_plain_install(self) -> None:
        """The keyring `foo.sources` references (`foo.gpg`) exists among the source's
        OWN captured keys — a real link, not a dangling one — so the source is
        proposed for install like any other missing item.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:source:foo.sources")
        assert diff.diff_class == DiffClass.MISSING_ON_TARGET
        assert diff.action == DiffAction.INSTALL
        assert diff.detail is None

    @pytest.mark.asyncio
    async def test_source_with_dangling_keyring_reference_is_flagged_not_installable(self) -> None:
        """`bar.list` names `bar.gpg`, which nothing captured on the source: the diff
        carries the dangling-reference detail and is downgraded to REPORT_ONLY —
        not proposed for install on its own (D-12).
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d2", "bar.list"), ""),
                "cat /etc/apt/sources.list.d/bar.list": CommandResult(0, _LEGACY_BAR, ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:source:bar.list")
        assert diff.action == DiffAction.REPORT_ONLY
        assert diff.detail is not None
        assert "bar.gpg" in diff.detail

    @pytest.mark.asyncio
    async def test_changed_source_with_dangling_keyring_reference_is_downgraded_to_report_only(self) -> None:
        """WR-03 regression: mirrors the missing-file case above — a changed source
        file whose keyring reference is dangling on the source must also be
        downgraded to REPORT_ONLY, not left as an ordinary CHANGE a user can tick and
        have fail at converge time (`_require_keyrings_ready` refuses it anyway).
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d2-new", "bar.list"), ""),
                "cat /etc/apt/sources.list.d/bar.list": CommandResult(0, _LEGACY_BAR, ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d2-old", "bar.list"), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:source:bar.list")
        assert diff.diff_class == DiffClass.VERSION_MISMATCH
        assert diff.action == DiffAction.REPORT_ONLY
        assert diff.detail is not None
        assert "bar.gpg" in diff.detail

    @pytest.mark.asyncio
    async def test_per_repo_and_global_trust_keys_are_distinct_item_ids(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "shared.gpg"), ""),
                "find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("k1", "shared.gpg"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        item_ids = {d.item_id for d in plan.diffs}
        assert "apt:key:per-repo:shared.gpg" in item_ids
        assert "apt:key:global-trust:shared.gpg" in item_ids

    @pytest.mark.asyncio
    async def test_key_matching_digest_on_both_sides_produces_no_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "x.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "x.gpg"), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id.startswith("apt:key:") for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_pin_and_config_diff_missing_extra_and_changed(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "cat /etc/apt/preferences.d/curl-pin": CommandResult(
                    0, "Package: curl libcurl4\nPin: origin example.com\nPin-Priority: 900\n", ""
                ),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99update"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(
                    0, sha256_line("p2", "curl-pin") + sha256_line("p3", "extra-pin"), ""
                ),
                "cat /etc/apt/preferences.d/extra-pin": CommandResult(0, "Package: extra\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        assert by_id["apt:pin:curl-pin"].diff_class == DiffClass.VERSION_MISMATCH
        assert by_id["apt:pin:curl-pin"].action == DiffAction.CHANGE
        assert "p1" in (by_id["apt:pin:curl-pin"].detail or "")
        assert "p2" in (by_id["apt:pin:curl-pin"].detail or "")
        assert by_id["apt:pin:extra-pin"].diff_class == DiffClass.EXTRA_ON_TARGET
        assert by_id["apt:pin:extra-pin"].action == DiffAction.REMOVE
        assert by_id["apt:config:99update"].diff_class == DiffClass.MISSING_ON_TARGET
        assert by_id["apt:config:99update"].action == DiffAction.INSTALL


# -- Task 2: ordered, transactional repository-group convergence -----------------------


def _index_of(commands: list[str], predicate: Callable[[str], bool]) -> int:
    return next(i for i, cmd in enumerate(commands) if predicate(cmd))


def respond_with_update_sequence(
    mapping: dict[str, CommandResult],
    update_results: list[CommandResult],
    default: CommandResult | None = None,
) -> Callable[..., CommandResult]:
    """Like `respond_to`, but `sudo apt-get update` returns successive results from
    `update_results` (last one repeats) — needed to test the rollback-then-reprobe
    sequence, where the same command must fail once and then succeed.
    """
    fallback = default if default is not None else CommandResult(exit_code=0, stdout="", stderr="")
    state = {"update_calls": 0}

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if "sudo apt-get update" in cmd:
            index = min(state["update_calls"], len(update_results) - 1)
            state["update_calls"] += 1
            return update_results[index]
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


def _repo_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    target_side_effect: Callable[..., CommandResult] | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, MagicMock]:
    """`make_context`, plus a resolved target `$HOME` (`/home/target-user`) — every
    repository-group write needs it for the staging path.
    """
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target = MagicMock()
    if target_side_effect is not None:
        target.run_command = AsyncMock(side_effect=target_side_effect)
    else:
        merged = {"echo $HOME": CommandResult(0, "/home/target-user", ""), **(target_responses or {})}
        target.run_command = AsyncMock(side_effect=respond_to(merged))
    target.send_file = AsyncMock(return_value=None)
    context = JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
    )
    return context, source, target


class TestRepoGroupOrdering:
    @pytest.mark.asyncio
    async def test_key_then_source_then_update_then_package_install(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                "test -f /etc/apt/sources.list.d/foo.sources": CommandResult(1, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": CommandResult(
                    0, "", ""
                ),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(
            job,
            plan,
            {
                "apt:key:per-repo:foo.gpg": Decision.APPLY,
                "apt:source:foo.sources": Decision.APPLY,
                "apt:package:pkg-a": Decision.APPLY,
            },
        )

        await job.execute()

        commands = all_calls(target)
        key_idx = _index_of(commands, lambda c: "sudo install" in c and "keyrings/foo.gpg" in c)
        source_idx = _index_of(commands, lambda c: "sudo install" in c and "sources.list.d/foo.sources" in c)
        update_idx = _index_of(commands, lambda c: c == "sudo apt-get update")
        package_idx = _index_of(
            commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c
        )
        assert key_idx < source_idx < update_idx < package_idx

    @pytest.mark.asyncio
    async def test_apt_get_update_runs_exactly_once_for_three_repo_items(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "a-pin"), ""),
                "cat /etc/apt/preferences.d/a-pin": CommandResult(0, "Package: a\n", ""),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "a-conf"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "a.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/preferences.d/a-pin": CommandResult(1, "", ""),
                "test -f /etc/apt/apt.conf.d/a-conf": CommandResult(1, "", ""),
                "test -f /etc/apt/keyrings/a.gpg": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(
            job,
            plan,
            {
                "apt:pin:a-pin": Decision.APPLY,
                "apt:config:a-conf": Decision.APPLY,
                "apt:key:per-repo:a.gpg": Decision.APPLY,
            },
        )

        await job.execute()

        commands = all_calls(target)
        assert sum(1 for c in commands if c == "sudo apt-get update") == 1

    @pytest.mark.asyncio
    async def test_no_key_command_contains_a_url(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "a.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/keyrings/a.gpg": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:key:per-repo:a.gpg": Decision.APPLY})

        await job.execute()

        for cmd in all_calls(target):
            assert "http://" not in cmd
            assert "https://" not in cmd

    @pytest.mark.asyncio
    async def test_failed_key_write_leaves_dependent_source_unwritten(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                "test -f /etc/apt/sources.list.d/foo.sources": CommandResult(1, "", ""),
                "sudo install -o root -g root -m 0644": CommandResult(1, "", "disk full"),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(
            job,
            plan,
            {"apt:key:per-repo:foo.gpg": Decision.APPLY, "apt:source:foo.sources": Decision.APPLY},
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:key:per-repo:foo.gpg" in failed_ids
        assert "apt:source:foo.sources" in failed_ids
        commands = all_calls(target)
        assert not any("sudo install" in c and "sources.list.d/foo.sources" in c for c in commands)

    @pytest.mark.asyncio
    async def test_remove_source_issues_single_rm_naming_that_file(self) -> None:
        context, _source, target = _repo_context(
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "extra.list"), ""),
                "cat /etc/apt/sources.list.d/extra.list": CommandResult(
                    0, "deb https://example.com stable main\n", ""
                ),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:source:extra.list": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        etc_removals = [c for c in commands if "sudo rm -f" in c]
        assert len(etc_removals) == 1
        assert "sources.list.d/extra.list" in etc_removals[0]

    @pytest.mark.asyncio
    async def test_promotion_uses_sudo_install_with_owner_group_mode_never_mv(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:config:99conf": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        promotions = [c for c in commands if "apt.conf.d/99conf" in c and "sudo install" in c]
        assert len(promotions) == 1
        assert "-o root -g root -m 0644" in promotions[0]
        assert not any("sudo mv" in c for c in commands)

    @pytest.mark.asyncio
    async def test_staging_file_removed_after_success_and_after_failure(self) -> None:
        for promote_result, label in (
            (CommandResult(0, "", ""), "success"),
            (CommandResult(1, "", "boom"), "failure"),
        ):
            context, _source, target = _repo_context(
                source_responses={
                    **_NO_PACKAGES,
                    "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
                },
                target_responses={
                    **_NO_PACKAGES,
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "sudo install -o root -g root -m 0644": promote_result,
                    "sudo apt-get update": CommandResult(0, "", ""),
                },
            )
            job = AptSyncJob(context)
            plan = await job.plan()
            _accept(job, plan, {"apt:config:99conf": Decision.APPLY})

            if label == "success":
                await job.execute()
            else:
                with pytest.raises(PackageItemFailures):
                    await job.execute()

            commands = all_calls(target)
            staged_cleanup = [c for c in commands if c.startswith("rm -f") and "apt-staging" in c]
            assert len(staged_cleanup) == 1, f"expected one staging cleanup for {label}"

    @pytest.mark.asyncio
    async def test_send_file_destinations_start_with_home_never_contain_etc(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:config:99conf": Decision.APPLY})

        await job.execute()

        destinations = [call.args[1] for call in target.send_file.call_args_list]
        assert destinations, "expected at least one send_file call"
        for dest in destinations:
            assert dest.startswith("/home/target-user")
            assert "/etc" not in dest


class TestRepoGroupTransaction:
    @pytest.mark.asyncio
    async def test_failed_update_restores_changed_deletes_created_records_group_failures(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, "Package: curl\n", ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                    "test -f /etc/apt/preferences.d/curl-pin": CommandResult(0, "", ""),
                    "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p2", "curl-pin"), ""),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:key:per-repo:foo.gpg": Decision.APPLY, "apt:pin:curl-pin": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:key:per-repo:foo.gpg" in failed_ids
        assert "apt:pin:curl-pin" in failed_ids

        commands = all_calls(target)
        # Restore: the pre-existing pin file is put back from its backup.
        assert any("sudo install" in c and "backup-" in c and "preferences.d/curl-pin" in c for c in commands)
        # Delete: the brand-new key file this run created is removed.
        assert any("sudo rm -f" in c and "keyrings/foo.gpg" in c for c in commands)
        # Two `apt-get update` calls: the failing one and the post-rollback reprobe.
        assert sum(1 for c in commands if c == "sudo apt-get update") == 2

    @pytest.mark.asyncio
    async def test_successful_update_issues_no_restore_command(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:key:per-repo:foo.gpg": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert not any("sudo install" in c and "backup-" in c for c in commands)

    @pytest.mark.asyncio
    async def test_rollback_does_not_prevent_package_items_from_being_attempted(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    "apt-mark showmanual": CommandResult(0, "", ""),
                    "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                    "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                    "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                        CommandResult(0, "", "")
                    ),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:key:per-repo:foo.gpg": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:key:per-repo:foo.gpg" in failed_ids
        assert "apt:package:pkg-a" not in failed_ids

        commands = all_calls(target)
        assert any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c for c in commands)


class TestRepoGroupBackupFailure:
    """CR-01 regression: a `_backup_destination` failure must fail every repository-
    group item through the normal per-item `PackageItemFailures` path, never escape
    as a bare `KeyError` (which would crash the whole job and cancel every other
    already-approved job's `apply()`, violating D-27).
    """

    @pytest.mark.asyncio
    async def test_backup_failure_fails_every_group_item_without_crashing(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(
                    0, sha256_line("p1-new", "pin-a") + sha256_line("p2-new", "pin-b"), ""
                ),
                "cat /etc/apt/preferences.d/pin-a": CommandResult(0, "Package: pin-a\n", ""),
                "cat /etc/apt/preferences.d/pin-b": CommandResult(0, "Package: pin-b\n", ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(
                    0, sha256_line("p1-old", "pin-a") + sha256_line("p2-old", "pin-b"), ""
                ),
                "test -f /etc/apt/preferences.d/pin-a": CommandResult(0, "", ""),
                "test -f /etc/apt/preferences.d/pin-b": CommandResult(0, "", ""),
                "sudo cp -a": CommandResult(1, "", "disk full"),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:pin:pin-a": Decision.APPLY, "apt:pin:pin-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        # Both group items (plus the auto-injected metadata-refresh marker) are
        # reported as failures — not just the one whose backup was actually
        # attempted before the loop aborted — and no KeyError escapes.
        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert {"apt:pin:pin-a", "apt:pin:pin-b"} <= failed_ids

        commands = all_calls(target)
        # Neither pin file was ever written: the group aborts before any write once
        # backing up fails.
        assert not any("sudo install -o root -g root -m 0644" in c and "preferences.d/pin-" in c for c in commands)


class TestKeyringsDirectoryEnsured:
    """CR-02 regression: `/etc/apt/keyrings` does not ship on a fresh Ubuntu 24.04
    target (unlike `sources.list.d`/`preferences.d`/`apt.conf.d`/`trusted.gpg.d`,
    which are part of the `apt` package), so `sudo install` without `-D` fails with
    "No such file or directory" promoting a per-repo key to a fresh machine — exactly
    the "sync a fresh machine" scenario this subsystem exists for.
    """

    @pytest.mark.asyncio
    async def test_promotion_ensures_keyrings_directory_before_install(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:key:per-repo:foo.gpg": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        mkdir_idx = _index_of(commands, lambda c: c == "sudo mkdir -p -m 0755 /etc/apt/keyrings")
        install_idx = _index_of(
            commands, lambda c: "sudo install -o root -g root -m 0644" in c and "keyrings/foo.gpg" in c
        )
        assert mkdir_idx < install_idx

    @pytest.mark.asyncio
    async def test_directory_preparation_failure_fails_the_item_not_the_run(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                "sudo mkdir -p -m 0755 /etc/apt/keyrings": CommandResult(1, "", "permission denied"),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:key:per-repo:foo.gpg": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:key:per-repo:foo.gpg" in failed_ids
        commands = all_calls(target)
        assert not any("sudo install -o root -g root -m 0644" in c and "keyrings/foo.gpg" in c for c in commands)
