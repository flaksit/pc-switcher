"""Unit tests for AptSyncJob and the shared PackageSyncJob plan()/apply() split.

Covers the tracer's single path — one apt package missing on the target — through
capture, diff, plan/apply separation, the coordinator-accepted-plan ordering guard,
converge (with the apt-get -s transaction guard), dry-run, continue-on-failure, and
validate(). All executor interactions are mocked; no real apt/dpkg/sudo commands run.
"""

from __future__ import annotations

import dataclasses
import shlex
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.executor import LocalExecutor
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import (
    _METADATA_REFRESH_ITEM_ID,
    _TARGET_SUDO_COMMANDS,
    AptSyncJob,
    _parse_pin_file,
    _parse_source_file,
    compare_deb_versions,
    simulate_apt_transaction,
)
from pcswitcher.jobs.packages.items import AptPackageItem, DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import (
    _REMOVAL_ACTIONS,
    COLLATERAL_REVIEW_ACTION,
    Decision,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, Host
from pcswitcher.orchestrator import Orchestrator
from tests.unit.jobs.test_package_sync_core import FakeReviewer

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


class TestManifestIsShowmanualOnly:
    """A-10/A-12: the manifest is `apt-mark showmanual` and nothing else. Every other
    guarantee this job makes rests on that — an auto-installed dependency is invisible to
    the model, and an empty source manifest is a mass removal that must stay visible.
    """

    @pytest.mark.asyncio
    async def test_auto_installed_dependency_produces_no_diff_of_any_kind(self) -> None:
        """`libdep` is installed on the source (dpkg knows it) but is not in either
        machine's `showmanual` set, so it is never an item: never installed on the target,
        never removed, never reported. `_resolve_versions` builds items from the
        `showmanual` names alone, which is exactly the mechanism this pins.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nlibdep\t5.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert not any("libdep" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_empty_source_manifest_offers_every_target_package_as_an_unticked_removal(self) -> None:
        """An empty `apt-mark showmanual` on the source means every target package is
        extra. That mass removal must surface as ordinary EXTRA_ON_TARGET/REMOVE items in
        a removal-direction group (unticked by default, D-07), never silently and never
        pre-approved.
        """
        context, _source, _target = make_context(
            source_responses={"apt-mark showmanual": CommandResult(0, "", "")},
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t2.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert {d.item_id for d in plan.diffs} == {"apt:package:pkg-a", "apt:package:pkg-b"}
        assert all(d.diff_class == DiffClass.EXTRA_ON_TARGET and d.action == DiffAction.REMOVE for d in plan.diffs)
        assert len(plan.groups) == 1
        assert plan.groups[0].action in _REMOVAL_ACTIONS
        assert plan.groups[0].title == "Remove apt packages"


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
    async def test_execute_without_a_reviewer_raises_and_issues_no_command(self) -> None:
        context, _source, target = make_context()
        job = AptSyncJob(context)  # context.reviewer defaults to None

        with pytest.raises(AssertionError, match="no reviewer"):
            await job.execute()

        target.run_command.assert_not_called()


def _install_reviewer(job: AptSyncJob, decisions: dict[str, Decision]) -> None:
    """Inject a `FakeReviewer` returning `decisions`, so `execute()` plans, reviews and
    applies through the same self-contained path production uses. Unlisted item ids
    default to `SKIP_ONCE`, matching the review's own default for an unticked entry.
    """
    job.context = dataclasses.replace(job.context, reviewer=FakeReviewer(decisions))


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
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.SKIP_ONCE})

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
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

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
        _install_reviewer(
            job,
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
    async def test_guard_refuses_drifted_manual_removal_not_seen_at_plan_time(self) -> None:
        """The apply-time guard is the last line of defence (D-30): a real transaction
        that has drifted since plan time to remove a manually-installed package nobody
        reviewed is still refused — D-30 changes what plan time asks, not whether apply
        time verifies. `ghost-pkg` is manual on the target and matches the source, so it
        never appears as a diff; the plan-time simulation is clean, but the apply-time
        simulation removes it.
        """
        sim_cmd = "apt-get -s install -y --no-install-recommends pkg-a"
        state = {"sim": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == "apt-mark showmanual":
                return CommandResult(0, "ghost-pkg\n", "")
            if "dpkg-query" in cmd:
                return CommandResult(0, "ghost-pkg\t1.0\n", "")
            if cmd == sim_cmd:
                state["sim"] += 1
                if state["sim"] == 1:
                    return CommandResult(0, "Inst pkg-a (1.0)\n", "")
                return CommandResult(0, "Inst pkg-a (1.0)\nRemv ghost-pkg [1.0]\n", "")
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nghost-pkg\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nghost-pkg\t1.0\n", ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "ghost-pkg" in message

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_install_whose_only_collateral_is_auto_deps_proceeds(self) -> None:
        """The D-30 win, at the guard: an install whose simulation removes only
        auto-installed dependencies (nothing in the target's manual set) is NOT refused —
        this is the legitimate install the old blanket refusal wrongly blocked.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv auto-dep [1.0]\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)

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
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

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
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "dpkg was interrupted" in message

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)


class TestHoldPinCapture:
    """collect_hold_sets: apt-mark showhold on BOTH machines; collect_hold_pin_facts:
    preferences.d pins only (#208 — holds moved to their own membership item)."""

    @pytest.mark.asyncio
    async def test_hold_sets_from_both_machines_surface(self) -> None:
        context, _source, _target = make_context(
            source_responses={"apt-mark showhold": CommandResult(0, "pkg-src-held\n", "")},
            target_responses={"apt-mark showhold": CommandResult(0, "pkg-tgt-held\n", "")},
        )
        job = AptSyncJob(context)

        source_holds, target_holds = await job.collect_hold_sets()

        assert source_holds == frozenset({"pkg-src-held"})
        assert target_holds == frozenset({"pkg-tgt-held"})

    @pytest.mark.asyncio
    async def test_collect_hold_pin_facts_returns_pins_only_no_holds(self) -> None:
        """#208: `collect_hold_pin_facts` no longer reads `apt-mark showhold` — holds
        travel as `apt:hold:` items, so only pin facts surface here."""
        context, _source, _target = make_context(
            source_responses={"apt-mark showhold": CommandResult(0, "src-held\n", "")},
            target_responses={
                "apt-mark showhold": CommandResult(0, "tgt-held\n", ""),
                "find /etc/apt/preferences.d": CommandResult(
                    0, "/etc/apt/preferences.d/curl-pin\tPackage: curl\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        facts = await job.collect_hold_pin_facts()

        assert all(fact.mechanism == "pin" for fact in facts)
        assert {fact.package for fact in facts} == {"curl"}

    @pytest.mark.asyncio
    async def test_preferences_d_pin_surfaces_with_pin_mechanism_and_filename(self) -> None:
        context, _source, _target = make_context(
            target_responses={
                "find /etc/apt/preferences.d": CommandResult(
                    0, "/etc/apt/preferences.d/curl-pin\tPackage: curl\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        facts = await job.collect_hold_pin_facts()

        pins = [fact for fact in facts if fact.mechanism == "pin"]
        assert len(pins) == 1
        assert pins[0].package == "curl"
        assert pins[0].source_ref == "/etc/apt/preferences.d/curl-pin"


class TestAptHold:
    """#208: hold replication — `apt:hold:` membership items, converge via `apt-mark`,
    the HELD_OR_PINNED reshape (pins echo, holds don't double-report), and sudo scope."""

    @pytest.mark.asyncio
    async def test_source_held_yields_install_hold_item_and_converge_runs_apt_mark_hold(self) -> None:
        """A package held on the source but not the target produces an `apt:hold:`
        INSTALL item; approving it converges via `sudo apt-mark hold`, never apt-get."""
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any(cmd == "sudo apt-mark hold pkg-a" for cmd in commands)
        assert not any("apt-get install" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_target_held_only_yields_remove_unhold_item(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
                "sudo apt-mark unhold pkg-a": CommandResult(0, "Canceled hold on pkg-a.\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()
        by_id = {diff.item_id: diff for diff in plan.diffs}
        assert by_id["apt:hold:pkg-a"].action == DiffAction.REMOVE

        _install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})
        await job.execute()
        assert any(cmd == "sudo apt-mark unhold pkg-a" for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_held_on_both_yields_no_hold_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(diff.item_class == ItemClass.APT_HOLD for diff in plan.diffs)

    @pytest.mark.asyncio
    async def test_held_package_yields_hold_item_not_duplicate_held_or_pinned_report(self) -> None:
        """A target-held package produces the `apt:hold:` item and NOT a package-level
        HELD_OR_PINNED report for the same name (#208 dedup)."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t2.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
            },
            target_responses={
                # Different version on target: without the hold this would be a
                # VERSION_MISMATCH; the hold suppresses that package action.
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {diff.item_id: diff for diff in plan.diffs}
        assert "apt:hold:pkg-a" in by_id
        assert "apt:package:pkg-a" not in by_id
        assert not any(diff.diff_class == DiffClass.HELD_OR_PINNED for diff in plan.diffs)

    @pytest.mark.asyncio
    async def test_pin_still_yields_report_only_echo_alongside_a_hold_item(self) -> None:
        """A pin keeps its REPORT_ONLY HELD_OR_PINNED echo on the package; a separate
        held package produces its own `apt:hold:` item — both coexist (#208)."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "curl\nheld-pkg\n", ""),
                "dpkg-query": CommandResult(0, "curl\t1.0\nheld-pkg\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "curl\nheld-pkg\n", ""),
                "dpkg-query": CommandResult(0, "curl\t1.0\nheld-pkg\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "held-pkg\n", ""),
                "find /etc/apt/preferences.d": CommandResult(
                    0, "/etc/apt/preferences.d/curl-pin\tPackage: curl\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {diff.item_id: diff for diff in plan.diffs}
        curl_diff = by_id["apt:package:curl"]
        assert curl_diff.diff_class == DiffClass.HELD_OR_PINNED
        assert curl_diff.action == DiffAction.REPORT_ONLY
        hold_diff = by_id["apt:hold:held-pkg"]
        assert hold_diff.item_class == ItemClass.APT_HOLD
        assert hold_diff.action == DiffAction.REMOVE

    @pytest.mark.asyncio
    async def test_skip_always_on_a_hold_writes_the_decision_file(self) -> None:
        """SKIP_ALWAYS on an `apt:hold:` INSTALL item (source-held) persists a decision
        on the SOURCE via the machine-local decision file (D-08a)."""
        context, source, _target = make_context()
        job = AptSyncJob(context)
        hold_diff = ItemDiff(
            item_class=ItemClass.APT_HOLD,
            diff_class=DiffClass.MISSING_ON_TARGET,
            action=DiffAction.INSTALL,
            item_id="apt:hold:pkg-a",
            label="pkg-a (hold)",
            detail=None,
        )
        plan = PackagePlan(manager="apt", diffs=(hold_diff,), groups=())
        job.accept_review(
            plan, ReviewOutcome(decisions={"apt:hold:pkg-a": Decision.SKIP_ALWAYS}, was_interactive=True)
        )

        await job.apply()

        source_cmds = all_calls(source)
        assert any("mv -f" in cmd and "apt.decisions" in cmd for cmd in source_cmds)

    def test_apt_mark_is_in_the_target_sudo_command_list(self) -> None:
        assert "/usr/bin/apt-mark" in _TARGET_SUDO_COMMANDS


class TestHoldReviewVerbs:
    """#208 D3 — the single behavioural promise of hold replication: a hold item reads
    "hold"/"unhold" in its group title AND in every entry's `action_label`, and never
    appears under an install/remove packages group, even when ordinary package installs
    and removals share the same `DiffAction` in the same plan.
    """

    @pytest.mark.asyncio
    async def test_hold_items_get_their_own_group_with_hold_and_unhold_verbs(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-install\npkg-common\n", ""),
                "dpkg-query": CommandResult(0, "pkg-install\t1.0\npkg-common\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "hold-add\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-extra\npkg-common\n", ""),
                "dpkg-query": CommandResult(0, "pkg-extra\t9.9\npkg-common\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "hold-drop\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        group_of = {entry.item_id: group for group in plan.groups for entry in group.entries}
        label_of = {entry.item_id: entry.action_label for group in plan.groups for entry in group.entries}

        # The package diffs still read as install/remove — the hold verbs are not a
        # blanket rename, they are per item class.
        assert group_of["apt:package:pkg-install"].title == "Install apt packages"
        assert group_of["apt:package:pkg-extra"].title == "Remove apt packages"

        assert group_of["apt:hold:hold-add"].title == "Hold apt packages"
        assert group_of["apt:hold:hold-drop"].title == "Unhold apt packages"
        assert label_of["apt:hold:hold-add"] == "hold"
        assert label_of["apt:hold:hold-drop"] == "unhold"

    @pytest.mark.asyncio
    async def test_unhold_group_is_removal_direction_and_the_hold_group_is_not(self) -> None:
        """`ReviewGroup.action` is what `review._REMOVAL_ACTIONS` tests to decide whether a
        group's checkboxes default to unticked. Undoing a block the user deliberately set
        needs that friction; adding one does not (#208 D3).
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "hold-add\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "hold-drop\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        group_of = {entry.item_id: group for group in plan.groups for entry in group.entries}
        assert group_of["apt:hold:hold-drop"].action in _REMOVAL_ACTIONS
        assert group_of["apt:hold:hold-add"].action not in _REMOVAL_ACTIONS


class TestInstallBeforeHoldOrdering:
    """#208 D8: a package missing on the target and held on the source converges its
    `apt-mark hold` AFTER its `apt-get install` — dpkg selection state for a package that
    is not there yet is not a state apt can set. Both ordering code paths are covered:
    `plan()`'s `_ITEM_CLASS_ORDER` sort, and `accept_review`'s marker-insertion rebuild.
    """

    @pytest.mark.asyncio
    async def test_hold_follows_install_on_the_plain_plan_sort_path(self) -> None:
        """A repo diff exists (so `plan()` runs its `_ITEM_CLASS_ORDER` sort) but is left
        unapproved, so `accept_review` inserts no metadata-refresh marker and never
        rebuilds the diff order.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "a.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        install_idx = _index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
        hold_idx = _index_of(commands, lambda c: c == "sudo apt-mark hold pkg-a")
        assert install_idx < hold_idx

    @pytest.mark.asyncio
    async def test_hold_follows_install_on_the_accept_review_reorder_path(self) -> None:
        """An approved repo-group item makes `accept_review` rebuild the plan around the
        metadata-refresh marker (repo items, marker, packages, holds) — the hold must stay
        behind its package install through that rebuild too.
        """
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                "test -f /etc/apt/sources.list.d/foo.sources": CommandResult(1, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(
            job,
            {
                "apt:source:foo.sources": Decision.APPLY,
                "apt:package:pkg-a": Decision.APPLY,
                "apt:hold:pkg-a": Decision.APPLY,
            },
        )

        await job.execute()

        commands = all_calls(target)
        key_idx = _index_of(commands, lambda c: "sudo install" in c and "keyrings/foo.gpg" in c)
        update_idx = _index_of(commands, lambda c: c == "sudo apt-get update")
        install_idx = _index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
        hold_idx = _index_of(commands, lambda c: c == "sudo apt-mark hold pkg-a")
        assert key_idx < update_idx < install_idx < hold_idx


class TestHoldOnAnAbsentPackage:
    """#208 D6: a hold approved for a package the target does not have hits `apt-mark`'s
    own error. That is a normal per-item failure (D-27 continue-and-report) — no gating
    machinery, no crash, no aborted run.
    """

    @pytest.mark.asyncio
    async def test_failed_apt_mark_hold_fails_only_that_item(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-good\n", ""),
                "dpkg-query": CommandResult(0, "pkg-good\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "ghost-pkg\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-good": CommandResult(
                    0, "Inst pkg-good (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-good": (
                    CommandResult(0, "", "")
                ),
                "sudo apt-mark hold ghost-pkg": CommandResult(1, "", "E: Unable to locate package ghost-pkg"),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-good": Decision.APPLY, "apt:hold:ghost-pkg": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert [diff.item_id for diff, _ in exc_info.value.failures] == ["apt:hold:ghost-pkg"]
        # The unrelated item in the same run still converged.
        assert any(
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-good" in c for c in all_calls(target)
        )


class TestHoldsDriveNoSimulation:
    """#208 D4: a hold is dpkg selection state, not an apt transaction — so it drives no
    `apt-get -s` preview at plan time and none at converge time.
    """

    @pytest.mark.asyncio
    async def test_hold_only_run_issues_zero_apt_get_simulations(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        assert [d.item_class for d in plan.diffs] == [ItemClass.APT_HOLD]

        _install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})
        await job.execute()

        commands = all_calls(target)
        assert any(c == "sudo apt-mark hold pkg-a" for c in commands)
        assert not any("apt-get -s" in c for c in commands)


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


class TestNoUnreproducibleDetectionInApt:
    """D-18: apt_sync no longer detects, reviews or converges unreproducible items —
    that ownership moved to manual_installs_sync. An input that previously produced an
    UNREPRODUCIBLE diff (a source package with no apt candidate) now produces none.
    """

    @pytest.mark.asyncio
    async def test_apt_plan_emits_no_unreproducible_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "dpkg-query": CommandResult(0, "brscan3\t1.0\n", ""),
                # A source with no apt candidate AND an unowned /usr/local path: both
                # previously became UNREPRODUCIBLE diffs inside apt_sync's own plan().
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
                "find /usr/local": CommandResult(0, "/usr/local/flux\n", ""),
                "dpkg -S": CommandResult(0, "", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "dpkg-query": CommandResult(0, "brscan3\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.UNREPRODUCIBLE for d in plan.diffs)


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
        _install_reviewer(job, {"apt:package:pkg-extra": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        real_removals = [cmd for cmd in commands if "sudo" in cmd and "apt-get remove" in cmd]
        assert len(real_removals) == 1
        assert "pkg-extra" in real_removals[0]
        assert not any("apt-get install" in cmd for cmd in commands)


class TestRemovalGuard:
    """Auto reverse-deps proceed (D-30); an unapproved manual removal is still refused."""

    @pytest.mark.asyncio
    async def test_auto_reverse_dep_removal_proceeds(self) -> None:
        """Removing a package legitimately removes the auto-installed dependencies apt
        pulled in for it (D-30): `pkg-b` is not in the target manual set, so the removal
        of `pkg-a` proceeds even though its transaction also removes `pkg-b`.
        """
        context, _source, target = make_context(
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-get -s remove -y pkg-a": CommandResult(0, "Remv pkg-a [1.0]\nRemv pkg-b [1.0]\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y pkg-a": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get remove" in cmd and "pkg-a" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_drifted_manual_reverse_dep_removal_refused(self) -> None:
        """A removal whose real transaction drifted to also remove a manually-installed
        package nobody reviewed is still refused (D-30). `manual-b` is manual on both
        machines and matches, so it is not a diff; the plan-time simulation is clean.
        """
        sim_cmd = "apt-get -s remove -y pkg-a"
        state = {"sim": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == "apt-mark showmanual":
                return CommandResult(0, "pkg-a\nmanual-b\n", "")
            if "dpkg-query" in cmd:
                return CommandResult(0, "pkg-a\t1.0\nmanual-b\t1.0\n", "")
            if cmd == sim_cmd:
                state["sim"] += 1
                if state["sim"] == 1:
                    return CommandResult(0, "Remv pkg-a [1.0]\n", "")
                return CommandResult(0, "Remv pkg-a [1.0]\nRemv manual-b [1.0]\n", "")
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "manual-b\n", ""),
                "dpkg-query": CommandResult(0, "manual-b\t1.0\n", ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        _diff, message = exc_info.value.failures[0]
        assert "manual-b" in message

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
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        real_removals = [cmd for cmd in commands if "sudo" in cmd and "apt-get remove" in cmd]
        assert any("pkg-a" in cmd for cmd in real_removals)
        assert any("pkg-b" in cmd for cmd in real_removals)


class TestDowngradeGuard:
    @pytest.mark.asyncio
    async def test_guard_refuses_drifted_manual_downgrade(self) -> None:
        """The apply-time guard still refuses a downgrade of a manually-installed package
        that drifted in after plan time (D-30, D-04). `manual-dg` is manual on the target
        at 2.0 and matches the source, so it is not a diff; the plan-time simulation is
        clean, but the apply-time simulation would downgrade it to 1.0.
        """
        sim_cmd = "apt-get -s install -y --no-install-recommends pkg-a"
        state = {"sim": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == "apt-mark showmanual":
                return CommandResult(0, "manual-dg\n", "")
            if "dpkg-query" in cmd:
                return CommandResult(0, "manual-dg\t2.0\n", "")
            if cmd == "dpkg --compare-versions 1.0 lt 2.0":
                return CommandResult(0, "", "")
            if cmd == sim_cmd:
                state["sim"] += 1
                if state["sim"] == 1:
                    return CommandResult(0, "Inst pkg-a (1.0)\n", "")
                return CommandResult(0, "Inst pkg-a (1.0)\nInst manual-dg [2.0] (1.0)\n", "")
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nmanual-dg\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nmanual-dg\t2.0\n", ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "downgrade" in message.lower()
        assert "manual-dg" in message

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_guard_allows_auto_downgrade(self) -> None:
        """An auto-installed package the simulation would downgrade proceeds silently —
        apt resolving its own dependencies (D-30). `auto-dg` is not in the target manual
        set, so the guard never even compares versions for it.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst auto-dg [2.0] (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)
        # auto-dg is not manual, so no version comparison is issued for it.
        assert not any("dpkg --compare-versions" in cmd for cmd in commands)


class TestPlanTimeCollateral:
    """D-30: batched-simulation collateral is split by provenance against the target
    manual set — manual becomes a three-way review item, auto produces nothing.
    """

    @pytest.mark.asyncio
    async def test_manual_collateral_removal_becomes_a_collateral_review_item(self) -> None:
        """A package the install simulation would remove that IS in the target manual set
        becomes exactly one collateral review item, in a COLLATERAL_REVIEW_ACTION group,
        naming the triggering install (`other-manual` distinct from `pkg-a`).
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nother-manual\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = [diff for diff in plan.diffs if diff.item_id == "apt:collateral:other-manual"]
        assert len(collateral) == 1
        assert collateral[0].action == DiffAction.REPORT_ONLY
        assert collateral[0].label == "other-manual"
        assert collateral[0].detail is not None and "removed" in collateral[0].detail

        collateral_group = next(g for g in plan.groups if g.action == COLLATERAL_REVIEW_ACTION)
        assert "apt:collateral:other-manual" in {entry.item_id for entry in collateral_group.entries}
        install_group = next(g for g in plan.groups if g.action == "install")
        assert "apt:collateral:other-manual" not in {entry.item_id for entry in install_group.entries}
        # pkg-a stays a normal, approvable install candidate.
        assert "apt:package:pkg-a" in {entry.item_id for entry in install_group.entries}

    @pytest.mark.asyncio
    async def test_auto_collateral_removal_produces_no_review_item(self) -> None:
        """A package the simulation would remove that is NOT in the target manual set is
        auto-installed — apt's own business (D-30) — so no review item is emitted and the
        install remains approvable.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv auto-dep [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(diff.item_id.startswith("apt:collateral:") for diff in plan.diffs)
        assert not any(group.action == COLLATERAL_REVIEW_ACTION for group in plan.groups)
        install_group = next(g for g in plan.groups if g.action == "install")
        assert "apt:package:pkg-a" in {entry.item_id for entry in install_group.entries}

    @pytest.mark.asyncio
    async def test_manual_downgrade_becomes_item_auto_downgrade_does_not(self) -> None:
        """A downgrade of a manually-installed package produces a collateral item the same
        way a removal does; a downgrade of an auto-installed package produces nothing.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nmanual-dg\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nmanual-dg\t2.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "manual-dg\n", ""),
                "dpkg-query": CommandResult(0, "manual-dg\t2.0\n", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst manual-dg [2.0] (1.0)\nInst auto-dg [2.0] (1.0)\n", ""
                ),
                "dpkg --compare-versions 1.0 lt 2.0": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral_ids = {diff.item_id for diff in plan.diffs if diff.item_id.startswith("apt:collateral:")}
        assert "apt:collateral:manual-dg" in collateral_ids
        assert "apt:collateral:auto-dg" not in collateral_ids
        manual_dg = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:manual-dg")
        assert manual_dg.detail is not None and "downgrade" in manual_dg.detail.lower()

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


def _manual_collateral_context() -> tuple[JobContext, MagicMock, MagicMock]:
    """A job whose only install candidate (`pkg-a`) would, per the simulation, remove the
    manually-installed `other-manual` — the shared fixture for the install-anyway / skip
    flow tests. `other-manual` is manual and identical on both machines, so it is not a
    diff, only collateral. Its name is deliberately distinct from `pkg-a` so a bug that
    conflated the collateral package with its triggering install would be caught.
    """
    return make_context(
        source_responses={
            "apt-mark showmanual": CommandResult(0, "pkg-a\nother-manual\n", ""),
            "dpkg-query": CommandResult(0, "pkg-a\t1.0\nother-manual\t1.0\n", ""),
        },
        target_responses={
            "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
            "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
            "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""
            ),
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                CommandResult(0, "", "")
            ),
        },
    )


class TestCollateralFlow:
    """D-30 three-way outcome, end to end through execute()."""

    @pytest.mark.asyncio
    async def test_install_anyway_proceeds_and_guard_allows_the_collateral_removal(self) -> None:
        context, _source, target = _manual_collateral_context()
        job = AptSyncJob(context)
        _install_reviewer(
            job,
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:other-manual": Decision.APPLY},
        )

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_skip_leaves_the_triggering_install_unapproved(self) -> None:
        context, _source, target = _manual_collateral_context()
        job = AptSyncJob(context)
        _install_reviewer(
            job,
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:other-manual": Decision.SKIP_ONCE},
        )

        await job.execute()

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)


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

    @pytest.mark.asyncio
    async def test_target_without_passwordless_sudo_yields_validation_error_naming_the_binaries(self) -> None:
        """The target error must carry the sudoers remediation, not just a diagnosis:
        every binary the job escalates for has to appear so the user can paste one
        working grant rather than discover the missing paths one failed run at a time.
        """
        context, _source, _target = make_context(
            target_responses={
                "sudo -n true": CommandResult(1, "", "sudo: a password is required"),
                "fuser /var/lib/dpkg/lock-frontend": CommandResult(1, "", ""),
            },
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        target_sudo_errors = [e for e in errors if e.host is Host.TARGET and "sudo" in e.message]
        assert len(target_sudo_errors) == 1
        assert all(command in target_sudo_errors[0].message for command in _TARGET_SUDO_COMMANDS)


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
    async def test_content_hydration_reads_use_sudo_matching_the_digest_capture(self) -> None:
        """WR-04 regression: content reads for diff hydration must use the same
        `sudo`-qualified privilege as the digest capture (`sudo find ... sha256sum`),
        not a plain unprivileged `cat` — otherwise a source file locked down to
        `0600`-or-similar digests correctly (root) but reads back empty (unprivileged),
        silently hiding any keyring reference it names.
        """
        context, source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)

        await job.plan()

        commands = all_calls(source)
        assert any(cmd == "sudo cat /etc/apt/sources.list.d/foo.sources" for cmd in commands)
        assert not any(cmd == "cat /etc/apt/sources.list.d/foo.sources" for cmd in commands)

    @pytest.mark.asyncio
    async def test_source_with_key_present_on_source_yields_plain_install(self) -> None:
        """The keyring `foo.sources` references (`foo.gpg`) exists among the source's
        OWN captured keys — a real link, not a dangling one — so the source is
        proposed for install like any other missing item.

        The target has neither, so approving this one item also writes `foo.gpg`. The key
        is no item of its own (D-12), which is exactly why the write has to be named on
        the item the user does decide about.
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
        assert diff.detail is not None
        assert "foo.gpg" in diff.detail

    @pytest.mark.asyncio
    async def test_source_whose_key_the_target_already_has_names_no_key(self) -> None:
        """The other half of the rule: `foo.gpg` is already on the target byte-identical,
        so approving the repository writes no key and the item says nothing about one.
        Naming a key that will not be written would be the same defect in the other
        direction.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:source:foo.sources")
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


_POLICY_AVAILABLE = (
    "pkg-a:\n  Installed: (none)\n  Candidate: 1.0\n  Version table:\n"
    "     1.0 500\n        500 https://example.com stable/main amd64 Packages\n"
)
_POLICY_NO_CANDIDATE = "pkg-a:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"


class TestRepoGroupOrdering:
    @pytest.mark.asyncio
    async def test_key_then_source_then_update_then_package_install(self) -> None:
        """N5 end to end: the package is one apt reports a real candidate for, so the
        availability classification says INSTALL, and the four commands land in apt's own
        dependency order.
        """
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
                "apt-cache policy": CommandResult(0, _POLICY_AVAILABLE, ""),
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
        _install_reviewer(
            job,
            {"apt:source:foo.sources": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY},
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
    async def test_a_package_apt_reports_no_candidate_for_is_withheld_from_the_first_pass(self) -> None:
        """The other half of what N5's ordering test cannot show: an available package is
        offered, one apt reports `Candidate: (none)` for is not — it is `REPORT_ONLY`, and
        the first review never even shows it as installable.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _POLICY_NO_CANDIDATE, ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.REPO_UNAVAILABLE, DiffAction.REPORT_ONLY)]

    @pytest.mark.asyncio
    async def test_a_package_apt_has_never_heard_of_prints_no_block_and_is_still_offered(self) -> None:
        """`apt-cache policy` prints NOTHING for a name apt does not know — not a block with
        `Candidate: (none)`. That absence must read as "no evidence against", so a package
        whose repository this same run is about to add is still offered for install.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, "", "N: Unable to locate package pkg-a\n"),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)]

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
        _install_reviewer(
            job,
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
        """D-12: `foo.gpg` really is provisioned (the repository that needs it is
        installed), and not one command reaches for a vendor to get it.
        """
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
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:foo.sources": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo install" in cmd and "keyrings/foo.gpg" in cmd for cmd in commands)
        for cmd in commands:
            assert "http://" not in cmd
            assert "https://" not in cmd

    @pytest.mark.asyncio
    async def test_failed_key_write_leaves_dependent_source_unwritten(self) -> None:
        """A keyring that could not be promoted is not a failed ITEM — there is no key
        item — but the repository that references it must not be written anyway (D-12):
        a repo apt cannot verify is worse than no repo, so the SOURCE is what fails.
        """
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
        _install_reviewer(
            job,
            {"apt:key:per-repo:foo.gpg": Decision.APPLY, "apt:source:foo.sources": Decision.APPLY},
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert "apt:source:foo.sources" in failures
        assert "foo.gpg" in failures["apt:source:foo.sources"]
        assert not any(item_id.startswith("apt:key:") for item_id in failures)
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
        _install_reviewer(job, {"apt:source:extra.list": Decision.APPLY})

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
        _install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

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
            _install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

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
        _install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        await job.execute()

        destinations = [call.args[1] for call in target.send_file.call_args_list]
        assert destinations, "expected at least one send_file call"
        for dest in destinations:
            assert dest.startswith("/home/target-user")
            assert "/etc" not in dest


class TestRepoGroupRemovalAndKeyChange:
    """C-24 and C-8: the two repository-group shapes the ordering tests above do not
    exercise — a source file and its key removed together, and a key whose bytes differ
    on the two machines.
    """

    @pytest.mark.asyncio
    async def test_source_and_its_key_both_removed_with_one_update_after_both(self) -> None:
        """Both files are extra on the target and both approved: each gets its own
        `sudo rm -f`, and the run's single `apt-get update` runs after both writes — apt's
        metadata must never be refreshed against a half-removed repository.
        """
        context, _source, target = _repo_context(
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "extra.list"), ""),
                "cat /etc/apt/sources.list.d/extra.list": CommandResult(0, _LEGACY_BAR, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k9", "bar.gpg"), ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(
            job,
            {"apt:key:per-repo:bar.gpg": Decision.APPLY, "apt:source:extra.list": Decision.APPLY},
        )

        await job.execute()

        commands = all_calls(target)
        removals = [c for c in commands if c.startswith("sudo rm -f")]
        assert len(removals) == 2
        assert any("keyrings/bar.gpg" in c for c in removals)
        assert any("sources.list.d/extra.list" in c for c in removals)

        assert sum(1 for c in commands if c == "sudo apt-get update") == 1
        update_idx = _index_of(commands, lambda c: c == "sudo apt-get update")
        assert update_idx > max(commands.index(c) for c in removals)

    @pytest.mark.asyncio
    async def test_rotated_keyring_is_refreshed_although_its_source_file_is_identical(self) -> None:
        """C8: `foo.sources` is byte-identical on both machines and produces NO diff at
        all, but the keyring it names has different bytes — the vendor rotated it. The
        SOURCE's key file is staged under the target's home and promoted with `sudo
        install -o root -g root -m 0644`; never re-fetched, never parsed, never written
        from the target's own copy.
        """
        both_sides = sha256_line("d1", "foo.sources")
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-new", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-old", "foo.gpg"), ""),
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(0, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        assert not plan.diffs, "a rotated key must not manufacture a diff of any kind"
        assert not plan.groups

        _install_reviewer(job, {})
        await job.execute()

        transfers = [(call.args[0], call.args[1]) for call in target.send_file.call_args_list]
        assert len(transfers) == 1
        local_path, staged_dest = transfers[0]
        assert local_path == Path("/etc/apt/keyrings/foo.gpg")
        assert staged_dest.startswith("/home/target-user")

        promotions = [
            c for c in all_calls(target) if c.startswith("sudo install -o root -g root -m 0644") and "foo.gpg" in c
        ]
        assert len(promotions) == 1
        assert promotions[0] == f"sudo install -o root -g root -m 0644 {staged_dest} /etc/apt/keyrings/foo.gpg"


class TestRepoGroupTransaction:
    @pytest.mark.asyncio
    async def test_failed_update_restores_changed_deletes_created_records_group_failures(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, "Package: curl\n", ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "test -f /etc/apt/preferences.d/curl-pin": CommandResult(0, "", ""),
                    "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p2", "curl-pin"), ""),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:config:99conf": Decision.APPLY, "apt:pin:curl-pin": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:config:99conf" in failed_ids
        assert "apt:pin:curl-pin" in failed_ids

        commands = all_calls(target)
        # Restore: the pre-existing pin file is put back from its backup.
        assert any("sudo install" in c and "backup-" in c and "preferences.d/curl-pin" in c for c in commands)
        # Delete: the brand-new config file this run created is removed.
        assert any("sudo rm -f" in c and "apt.conf.d/99conf" in c for c in commands)
        # A clean rollback discards the backup.
        assert any(c.startswith("rm -rf") and "backup-" in c for c in commands)
        # Two `apt-get update` calls: the failing one and the post-rollback reprobe.
        assert sum(1 for c in commands if c == "sudo apt-get update") == 2

    @pytest.mark.asyncio
    async def test_failed_rollback_step_warns_and_keeps_the_backup(self) -> None:
        """A restore that fails must be named, and its backup must survive: that directory
        holds the only remaining copy of the file's pre-run content.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, "Package: curl\n", ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "test -f /etc/apt/preferences.d/curl-pin": CommandResult(0, "", ""),
                    "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p2", "curl-pin"), ""),
                    # The restore itself fails — the case this test exists for.
                    "sudo install -o root -g root -m 0644 /home/target-user/.cache": CommandResult(
                        1, "", "Read-only file system"
                    ),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:pin:curl-pin": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        # The incomplete rollback reaches the user through every group item's failure text,
        # naming the file and where its backup was kept.
        messages = " ".join(stderr for _diff, stderr in exc_info.value.failures)
        assert "ROLLBACK INCOMPLETE" in messages
        assert "preferences.d/curl-pin" in messages
        assert "backup-" in messages

        # The backup is NOT discarded — it is the only copy of the pre-run file left.
        assert not any(c.startswith("rm -rf") and "backup-" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_successful_update_issues_no_restore_command(self) -> None:
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
        _install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert not any("sudo install" in c and "backup-" in c for c in commands)

    @pytest.mark.asyncio
    async def test_rollback_does_not_prevent_package_items_from_being_attempted(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    "apt-mark showmanual": CommandResult(0, "", ""),
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                    "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                        CommandResult(0, "", "")
                    ),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:config:99conf": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:config:99conf" in failed_ids
        assert "apt:package:pkg-a" not in failed_ids

        commands = all_calls(target)
        assert any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c for c in commands)

    @pytest.mark.asyncio
    async def test_post_rollback_install_issues_no_further_apt_get_update(self) -> None:
        """D-18: the rollback's re-probe `apt-get update` succeeded, so `/etc/apt` is the
        pre-run configuration with fresh metadata. The package items that still run after
        the rollback (D-27) must issue no third refresh — the run's single-refresh
        guarantee (decision 1) holds across the rollback path too.
        """
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    "apt-mark showmanual": CommandResult(0, "", ""),
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                    "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                        CommandResult(0, "", "")
                    ),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:config:99conf": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures):
            await job.execute()

        commands = all_calls(target)
        # Exactly two: the group's own failing refresh, and the rollback's re-probe.
        assert sum(1 for c in commands if c == "sudo apt-get update") == 2
        install_idx = _index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
        assert not any(c == "sudo apt-get update" for c in commands[install_idx:])


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
        _install_reviewer(job, {"apt:pin:pin-a": Decision.APPLY, "apt:pin:pin-b": Decision.APPLY})

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

    @staticmethod
    def _fresh_target(**extra: CommandResult) -> tuple[JobContext, MagicMock, MagicMock]:
        """`foo.sources` and the `foo.gpg` it names, both missing on a target that has no
        `/etc/apt/keyrings` directory at all."""
        return _repo_context(
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
                "sudo apt-get update": CommandResult(0, "", ""),
                **extra,
            },
        )

    @pytest.mark.asyncio
    async def test_promotion_ensures_keyrings_directory_before_install(self) -> None:
        context, _source, target = self._fresh_target()
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:foo.sources": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        mkdir_idx = _index_of(commands, lambda c: c == "sudo mkdir -p -m 0755 /etc/apt/keyrings")
        install_idx = _index_of(
            commands, lambda c: "sudo install -o root -g root -m 0644" in c and "keyrings/foo.gpg" in c
        )
        assert mkdir_idx < install_idx

    @pytest.mark.asyncio
    async def test_directory_preparation_failure_fails_the_item_not_the_run(self) -> None:
        """The failure surfaces on the REPOSITORY, the thing the user reviewed: its key
        never landed, so the repo is not written either (D-12)."""
        context, _source, target = self._fresh_target(
            **{"sudo mkdir -p -m 0755 /etc/apt/keyrings": CommandResult(1, "", "permission denied")}
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:foo.sources": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert "apt:source:foo.sources" in failures
        assert "foo.gpg" in failures["apt:source:foo.sources"]
        commands = all_calls(target)
        assert not any("sudo install -o root -g root -m 0644" in c and "keyrings/foo.gpg" in c for c in commands)


# -- Decision 1: one `apt-get update` before installs, across both refresh paths ---------


class TestMetadataRefreshBeforeInstall:
    """Decision 1: a run that approves at least one INSTALL but changes no repo-group item
    still runs exactly one `apt-get update` before the first install; a failed refresh
    aborts the installs; and a run that already refreshed via the repo-group path does not
    refresh a second time.
    """

    @pytest.mark.asyncio
    async def test_install_only_run_refreshes_metadata_once_before_first_install(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t2.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "apt-get -s install -y --no-install-recommends pkg-b": CommandResult(0, "Inst pkg-b (2.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-b": (
                    CommandResult(0, "", "")
                ),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        # Exactly one refresh, even though two packages install (idempotent guard).
        assert sum(1 for c in commands if c == "sudo apt-get update") == 1
        update_idx = _index_of(commands, lambda c: c == "sudo apt-get update")
        first_install_idx = _index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
        assert update_idx < first_install_idx

    @pytest.mark.asyncio
    async def test_failed_metadata_refresh_aborts_installs_with_a_single_update(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t2.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "apt-get -s install -y --no-install-recommends pkg-b": CommandResult(0, "Inst pkg-b (2.0)\n", ""),
                "sudo apt-get update": CommandResult(1, "", "Could not resolve host archive.ubuntu.com"),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        messages = [m for _d, m in exc_info.value.failures]
        assert len(messages) == 2
        assert all("apt-get update" in m for m in messages)
        commands = all_calls(target)
        # The failure is cached: one update issued, then every install aborts on it —
        # never a second `apt-get update`, and never a real install against stale lists.
        assert sum(1 for c in commands if c == "sudo apt-get update") == 1
        assert not any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c for c in commands)

    @pytest.mark.asyncio
    async def test_repo_group_refresh_is_not_repeated_by_the_install_path(self) -> None:
        """A run that changes a repo-group item AND installs a package: the repository-
        group convergence's own `apt-get update` is the run's single refresh; the install
        path sees the flag already set and issues no second one (decision 1)."""
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:key:per-repo:foo.gpg": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert sum(1 for c in commands if c == "sudo apt-get update") == 1
        update_idx = _index_of(commands, lambda c: c == "sudo apt-get update")
        install_idx = _index_of(
            commands,
            lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c,
        )
        assert update_idx < install_idx


class TestReportOnlyRepoItemDecidedApply:
    """D-19: the untested edge of `accept_review`'s `approved_group` test — the only
    approved repository item is one the diff already downgraded to REPORT_ONLY (a source
    file whose keyring reference dangles on the source, D-12).

    End to end that means: the marker IS inserted (the `approved_group` test keys on the
    decision, not the action), the REPORT_ONLY diff itself never reaches `converge()`
    (`apply()` excludes REPORT_ONLY regardless of decision), and the repository group
    therefore has no actionable item — so nothing is written under `/etc/apt` and no
    `apt-get update` runs. Ticking an item the review already flagged as informational is
    a no-op, not a half-applied repository.
    """

    @pytest.mark.asyncio
    async def test_apply_on_a_report_only_source_writes_nothing_and_refreshes_nothing(self) -> None:
        context, _source, target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d2", "bar.list"), ""),
                "cat /etc/apt/sources.list.d/bar.list": CommandResult(0, _LEGACY_BAR, ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:bar.list": Decision.APPLY})

        await job.execute()

        assert job._accepted_plan is not None
        assert job._accepted_outcome is not None
        by_id = {d.item_id: d for d in job._accepted_plan.diffs}
        assert by_id["apt:source:bar.list"].action == DiffAction.REPORT_ONLY
        # The marker is inserted and decided APPLY, then converges as a no-op.
        assert _METADATA_REFRESH_ITEM_ID in by_id
        assert job._accepted_outcome.decisions[_METADATA_REFRESH_ITEM_ID] == Decision.APPLY

        commands = all_calls(target)
        assert not any("sudo apt-get update" in c for c in commands)
        assert not any(c.startswith("sudo install") or c.startswith("sudo rm -f") for c in commands)
        target.send_file.assert_not_called()


# -- Decision 8: collateral protects the SOURCE manual set too (union of target, source) -

_SOURCE_DECISION_SKIP_SRC_ONLY = (
    "machine_specific:\n"
    '  "apt:package:src-only":\n'
    "    item_class: apt_package\n"
    "    label: src-only\n"
    "    recorded_at: '2026-01-01T00:00:00Z'\n"
)


class TestSourceOnlyCollateral:
    """Decision 8: a package in the SOURCE manual set is protected from collateral
    removal/downgrade even when it is absent from the TARGET manual set."""

    @pytest.mark.asyncio
    async def test_source_only_manual_collateral_removal_becomes_a_review_item(self) -> None:
        """`src-only` is manual on the source but skip-recorded there, so it is filtered
        out of the source manifest (not a reviewed install candidate) yet still counts as
        source-manual. It is not in the target manual set. Installing `pkg-a` would remove
        it: under the old target-only rule this was silent auto collateral; under the union
        it becomes a manual-collateral review item.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nsrc-only\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nsrc-only\t1.0\n", ""),
                "apt.decisions.yaml": CommandResult(0, _SOURCE_DECISION_SKIP_SRC_ONLY, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv src-only [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = [d for d in plan.diffs if d.item_id == "apt:collateral:src-only"]
        assert len(collateral) == 1
        assert collateral[0].detail is not None and "removed" in collateral[0].detail
        # src-only was filtered from the source manifest, so it is NOT itself a review
        # candidate — it only surfaces via the source-manual union.
        assert "apt:package:src-only" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_apply_time_guard_refuses_source_only_manual_collateral(self) -> None:
        """The apply-time install guard consults the union too: a drifted real transaction
        that would remove a package manual on the SOURCE only (not the target) is refused.
        `src-only` is skip-recorded on the source so it is not a reviewed candidate.
        """
        sim_cmd = "apt-get -s install -y --no-install-recommends pkg-a"
        state = {"sim": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == "apt-mark showmanual":
                return CommandResult(0, "", "")
            if cmd == sim_cmd:
                state["sim"] += 1
                if state["sim"] == 1:
                    return CommandResult(0, "Inst pkg-a (1.0)\n", "")
                return CommandResult(0, "Inst pkg-a (1.0)\nRemv src-only [1.0]\n", "")
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nsrc-only\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nsrc-only\t1.0\n", ""),
                "apt.decisions.yaml": CommandResult(0, _SOURCE_DECISION_SKIP_SRC_ONLY, ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        _diff, message = exc_info.value.failures[0]
        assert "src-only" in message
        commands = all_calls(target)
        assert not any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c for c in commands)


# -- C26/N7: a repo/key removal names the target-side machine-specific packages ---------

# Distinguishes the source-reference scan (C26's removal impact, and the reference count
# keyring provisioning/collection run on) from `collect_hold_pin_facts`'s own `-exec awk`
# over `preferences.d`, which any looser substring would also match. `/etc/apt/sources.list`
# is part of the scan because a keyring named only there is still in use.
_SOURCE_SCAN_CMD = "/etc/apt/sources.list.d /etc/apt/sources.list -maxdepth 1 -type f -exec awk"

_VENDOR_LIST = "deb [signed-by=/etc/apt/keyrings/vendor.gpg] https://vendor.example.com/apt stable main\n"
_VENDOR_SOURCES = (
    "Types: deb\nURIs: https://vendor.example.com/apt/\nSuites: stable\n"
    "Components: main\nSigned-By: /etc/apt/keyrings/vendor.gpg\n"
)


def _decision_file(*item_ids: str) -> str:
    """A decision file recording each id skip-always as an apt package (D-08)."""
    body = "".join(
        f'  "{item_id}":\n'
        "    item_class: apt_package\n"
        f'    label: "{item_id.removeprefix("apt:package:")}"\n'
        "    reason: null\n"
        "    recorded_at: '2026-07-26T00:00:00Z'\n"
        for item_id in item_ids
    )
    return f"machine_specific:\n{body}"


def _policy_block(name: str, origin: str | None) -> str:
    """One `apt-cache policy` package block, installed, with `origin` as the installed
    version's repository — or dpkg's own record alone when `origin` is None (the shape a
    package installed from a local `.deb` has).
    """
    lines = [f"{name}:", "  Installed: 1.0", "  Candidate: 1.0", "  Version table:", " *** 1.0 500"]
    if origin is not None:
        lines.append(f"        500 {origin} stable/main amd64 Packages")
    lines.append("        100 /var/lib/dpkg/status")
    return "\n".join(lines) + "\n"


def _scan_line(filename: str, content: str, *, path: str | None = None) -> str:
    """The `find ... -exec awk` scan's `<path>\\t<line>` output for one source file,
    filtered the way the shipped awk program filters it. `path` overrides the assumed
    `sources.list.d` location, for the `/etc/apt/sources.list` case.
    """
    keep = ("uris:", "signed-by", "deb ", "deb-src ")
    where = path or f"/etc/apt/sources.list.d/{filename}"
    return "".join(
        f"{where}\t{line}\n" for line in content.splitlines() if any(token in line.lower() for token in keep)
    )


class TestRepoRemovalNamesMachineSpecificPackages:
    """C26/N7 — a source or key offered for removal names what the TARGET still needs.

    The package is recorded skip-always on the target, so `filter_inert` drops it from
    the target manifest and it produces no `ItemDiff` in any run: without this detail the
    review shows a bare file deletion and the user has no way to learn that approving it
    strands software they explicitly told the tool to keep. Disclosure, not refusal — the
    REMOVE action is untouched, as for flatpak's orphaned refs (#214) and apt's own
    transaction collateral (D-30).
    """

    @staticmethod
    def _target_responses(
        *,
        source_files: dict[str, str],
        source_digests: str,
        key_digests: str = "",
        decisions: str,
        policy: str,
    ) -> dict[str, CommandResult]:
        """Target responses for a run whose `/etc/apt` state is entirely target-only.

        `_SOURCE_SCAN_CMD` is listed FIRST: `respond_to` matches by substring and first
        match wins, and the scan command contains `find /etc/apt/sources.list.d` too.
        """
        scan = "".join(_scan_line(name, content) for name, content in source_files.items())
        return {
            **_NO_PACKAGES,
            _SOURCE_SCAN_CMD: CommandResult(0, scan, ""),
            "find /etc/apt/sources.list.d": CommandResult(0, source_digests, ""),
            "find /etc/apt/keyrings": CommandResult(0, key_digests, ""),
            "apt.decisions.yaml": CommandResult(0, decisions, ""),
            "apt-cache policy": CommandResult(0, policy, ""),
            **{
                f"cat /etc/apt/sources.list.d/{name}": CommandResult(0, content, "")
                for name, content in source_files.items()
            },
        }

    @pytest.mark.asyncio
    async def test_source_removal_names_the_machine_specific_package_it_would_strand(self) -> None:
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions=_decision_file("apt:package:vendor-tool"),
                policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:source:vendor.list")
        assert diff.action == DiffAction.REMOVE
        assert diff.detail is not None
        assert "vendor-tool" in diff.detail
        assert "vendor.list" in diff.detail

    @pytest.mark.asyncio
    async def test_the_machine_specific_package_itself_still_produces_no_diff(self) -> None:
        """The inertness this detail exists to compensate for must not regress: naming
        the package in a removal's detail is NOT the same as re-proposing it (D-08).
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **self._target_responses(
                    source_files={"vendor.list": _VENDOR_LIST},
                    source_digests=sha256_line("d1", "vendor.list"),
                    decisions=_decision_file("apt:package:vendor-tool"),
                    policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
                ),
                "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert "apt:package:vendor-tool" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_deb822_uris_match_the_policy_origin_despite_the_trailing_slash(self) -> None:
        """A `.sources` file writes `URIs: https://.../apt/` while `apt-cache policy`
        prints the origin without the trailing slash. Verbatim comparison would find no
        link at all for every repository written the first way.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.sources": _VENDOR_SOURCES},
                source_digests=sha256_line("d1", "vendor.sources"),
                decisions=_decision_file("apt:package:vendor-tool"),
                policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:source:vendor.sources")
        assert diff.detail is not None and "vendor-tool" in diff.detail

    @pytest.mark.asyncio
    async def test_source_removal_with_no_dependent_package_keeps_detail_none(self) -> None:
        """`other-tool` is machine-specific but was installed from a local `.deb`, so its
        only origin is dpkg's own record: no link, no noise.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions=_decision_file("apt:package:other-tool"),
                policy=_policy_block("other-tool", None),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:source:vendor.list")
        assert diff.action == DiffAction.REMOVE
        assert diff.detail is None

    @pytest.mark.asyncio
    async def test_detail_reaches_the_user_through_the_review_entry(self) -> None:
        """The plan's `ItemDiff` is not what the user reads — `ReviewGroup`/`ReviewEntry`
        is. The removal lands in its own unticked removal group carrying the same text.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions=_decision_file("apt:package:vendor-tool"),
                policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        group = next(g for g in plan.groups if g.action in _REMOVAL_ACTIONS)
        entry = next(e for e in group.entries if e.item_id == "apt:source:vendor.list")
        assert entry.detail is not None and "vendor-tool" in entry.detail

    @pytest.mark.asyncio
    async def test_one_apt_cache_policy_call_regardless_of_package_count(self) -> None:
        """The phase-wide batching rule: origins for every recorded package come from ONE
        `apt-cache policy` run, never one per package.
        """
        names = [f"vendor-tool-{i}" for i in range(12)]
        context, _source, target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions=_decision_file(*(f"apt:package:{name}" for name in names)),
                policy="".join(_policy_block(name, "https://vendor.example.com/apt") for name in names),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        policy_calls = [cmd for cmd in all_calls(target) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        assert all(name in policy_calls[0] for name in names)
        diff = next(d for d in plan.diffs if d.item_id == "apt:source:vendor.list")
        assert diff.detail is not None and all(name in diff.detail for name in names)

    @pytest.mark.asyncio
    async def test_no_policy_call_when_nothing_is_offered_for_removal(self) -> None:
        """Nothing extra on the target: the run does not pay for the `apt-cache policy`
        origin lookup. Machine-specific packages exist, so only the removal gate can be
        what stops it.

        The source-file SCAN is not gated the same way and is expected here: which keyrings
        the target's repositories point at is what keeps keys correct on every run, not
        only on a run that offers a removal.
        """
        context, _source, target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "apt.decisions.yaml": CommandResult(0, _decision_file("apt:package:vendor-tool"), ""),
            },
        )
        job = AptSyncJob(context)

        await job.plan()

        commands = all_calls(target)
        assert not any("apt-cache policy" in cmd for cmd in commands)
        assert sum(1 for cmd in commands if _SOURCE_SCAN_CMD in cmd) == 1


# -- Signing keys are handled transparently --------------------------------------------
#
# A key is not an item: no `ItemClass`, no `item_id`, no diff, no review entry, no decision
# file. Provisioning runs before any source write and keeps the target's copy matching the
# source machine's; collection runs after every source write and deletion, only when a
# source was actually removed, against the target's REAL post-write state.

_ROTATED_SOURCES = sha256_line("d1", "foo.sources")
_KEEPER_LIST = "deb [signed-by=/etc/apt/keyrings/shared.gpg] https://keeper.example.com stable main\n"
_GOING_LIST = "deb [signed-by=/etc/apt/keyrings/shared.gpg] https://going.example.com stable main\n"
_INLINE_SOURCES = (
    "Types: deb\nURIs: https://inline.example.com\nSuites: stable\nComponents: main\n"
    "Signed-By:\n -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n mDMEY2FrZQ==\n -----END PGP PUBLIC KEY BLOCK-----\n"
)


def _scanning_target(
    target_sources: dict[str, str],
    *,
    responses: dict[str, CommandResult],
    sources_list: str = "",
) -> Callable[..., CommandResult]:
    """A target whose source-file SCAN reflects the deletions the run has actually issued.

    A `sudo rm -f /etc/apt/sources.list.d/<f>` drops `<f>` from every later scan, which is
    what lets a test prove the keyring reference count is taken against the target's real
    post-write state rather than the state `plan()` saw. `sources_list` is the content of
    `/etc/apt/sources.list`, a file pc-switcher never syncs and never deletes.
    """
    live = dict(target_sources)

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if cmd.startswith("sudo rm -f "):
            live.pop(Path(shlex.split(cmd)[-1]).name, None)
        if _SOURCE_SCAN_CMD in cmd:
            scan = "".join(_scan_line(name, content) for name, content in live.items())
            if sources_list:
                scan += _scan_line("sources.list", sources_list, path="/etc/apt/sources.list")
            return CommandResult(0, scan, "")
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        return CommandResult(0, "", "")

    return _side_effect


_KEY_DEST_PREFIXES = ("/etc/apt/keyrings/", "/etc/apt/trusted.gpg.d/", "/usr/share/keyrings/")


def _key_writes(target: MagicMock) -> list[str]:
    """Every key promotion this run issued, by destination path, across all three key
    directories."""
    return [
        c.rsplit(" ", 1)[1]
        for c in all_calls(target)
        if c.startswith("sudo install -o root -g root -m 0644") and c.rsplit(" ", 1)[1].startswith(_KEY_DEST_PREFIXES)
    ]


def _key_deletions(target: MagicMock) -> list[str]:
    return [c for c in all_calls(target) if c.startswith("sudo rm -f") and "/etc/apt/keyrings/" in c]


class TestKeysAreNotItems:
    """No `apt:key:` identity may reach a diff, a review group or a decision — in any
    direction. The user decides about repositories; keys follow.
    """

    @pytest.mark.asyncio
    async def test_no_key_reaches_a_diff_or_a_review_group_in_any_direction(self) -> None:
        """All three directions at once: `new.gpg` missing on the target, `rot.gpg` present
        with different bytes, `old.gpg` present on the target alone — under the old model
        an INSTALL, a CHANGE and a REMOVE entry.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(
                    0, sha256_line("k1", "new.gpg") + sha256_line("k-new", "rot.gpg"), ""
                ),
                "find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g1", "legacy.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(
                    0, sha256_line("k-old", "rot.gpg") + sha256_line("k9", "old.gpg"), ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(diff.item_id.startswith("apt:key:") for diff in plan.diffs)
        assert not any(diff.item_class.value == "apt_key" for diff in plan.diffs)
        entries = {entry.item_id for group in plan.groups for entry in group.entries}
        assert not any(item_id.startswith("apt:key:") for item_id in entries)
        assert "apt:source:foo.sources" in entries, "the repository itself must still be reviewed"

    @pytest.mark.asyncio
    async def test_key_of_an_installed_repo_is_provisioned_with_no_decision_of_its_own(self) -> None:
        """The reviewer is told about the SOURCE only. `foo.gpg` still lands, and lands
        before the repository that references it.
        """
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
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:foo.sources": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        key_idx = _index_of(commands, lambda c: "sudo install" in c and "keyrings/foo.gpg" in c)
        source_idx = _index_of(commands, lambda c: "sudo install" in c and "sources.list.d/foo.sources" in c)
        assert key_idx < source_idx

    @pytest.mark.asyncio
    async def test_key_of_a_changed_repo_is_provisioned_too(self) -> None:
        """A CHANGED repository may point at a keyring the target has never seen — the
        `Signed-By:` line is part of what changed.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "foo.sources"), ""),
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:foo.sources": Decision.APPLY})

        await job.execute()

        assert _key_writes(target) == ["/etc/apt/keyrings/foo.gpg"]

    @pytest.mark.asyncio
    async def test_a_matching_keyring_is_never_written(self) -> None:
        """Same bytes on both machines: no transfer, no promotion, nothing for
        `--confirm-each-command` to prompt about.
        """
        both_sides = sha256_line("d1", "foo.sources")
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        await job.execute()

        assert not _key_writes(target)
        assert not target.send_file.call_args_list
        assert not any(c == "sudo apt-get update" for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_one_rotated_key_serving_three_repos_is_written_once(self) -> None:
        """1-n: `shared.gpg` is named by three source files, all byte-identical on both
        machines. One rotation, one write.
        """
        names = ["a.list", "b.list", "c.list"]
        both_sides = "".join(sha256_line(f"d-{name}", name) for name in names)
        scan = "".join(_scan_line(name, _KEEPER_LIST) for name in names)
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-new", "shared.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, scan, ""),
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-old", "shared.gpg"), ""),
                "test -f /etc/apt/keyrings/shared.gpg": CommandResult(0, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        await job.execute()

        assert _key_writes(target) == ["/etc/apt/keyrings/shared.gpg"]

    @pytest.mark.asyncio
    async def test_global_trust_keys_are_replicated_whether_missing_or_differing(self) -> None:
        """Nothing references a `trusted.gpg.d` key, so its own content is the only signal
        there is: copy the ones the target lacks, refresh the ones whose bytes differ.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/trusted.gpg.d": CommandResult(
                    0, sha256_line("g1", "fresh.gpg") + sha256_line("g-new", "rot.gpg"), ""
                ),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g-old", "rot.gpg"), ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        await job.execute()

        assert _key_writes(target) == ["/etc/apt/trusted.gpg.d/fresh.gpg", "/etc/apt/trusted.gpg.d/rot.gpg"]

    @pytest.mark.asyncio
    async def test_an_unreferenced_source_keyring_is_not_copied_to_the_target(self) -> None:
        """`/etc/apt/keyrings` is not mirrored wholesale: a key no repository on the target
        points at would be litter, not configuration.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "nobody-wants-me.gpg"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        await job.execute()

        assert not _key_writes(target)

    @pytest.mark.asyncio
    async def test_inline_armored_signed_by_names_no_keyring(self) -> None:
        """A deb822 `Signed-By:` carrying an inline armored block has an empty field value
        and continuation lines. It must yield no reference at all: not a bogus dependency
        on some file, and not a match that makes a real keyring look referenced.
        """
        _fmt, refs, _uris = _parse_source_file("inline.sources", _INLINE_SOURCES)

        assert refs == ()


class TestUnusedKeyringCollection:
    """The removal half: after every repository operation, drop the `/etc/apt/keyrings`
    files no surviving source references — and nothing else.
    """

    @staticmethod
    def _context(
        *,
        target_sources: dict[str, str],
        target_source_digests: str,
        target_keyrings: str,
        source_sources: str = "",
        source_keyrings: str = "",
        sources_list: str = "",
        source_extra: dict[str, CommandResult] | None = None,
        target_extra: dict[str, CommandResult] | None = None,
    ) -> tuple[JobContext, MagicMock, MagicMock]:
        return _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, source_sources, ""),
                "find /etc/apt/keyrings": CommandResult(0, source_keyrings, ""),
                **(source_extra or {}),
            },
            target_side_effect=_scanning_target(
                target_sources,
                sources_list=sources_list,
                responses={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "find /etc/apt/sources.list.d": CommandResult(0, target_source_digests, ""),
                    "find /etc/apt/keyrings": CommandResult(0, target_keyrings, ""),
                    "test -f": CommandResult(0, "", ""),
                    "sudo apt-get update": CommandResult(0, "", ""),
                    **{
                        f"cat /etc/apt/sources.list.d/{name}": CommandResult(0, content, "")
                        for name, content in target_sources.items()
                    },
                    **(target_extra or {}),
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_key_left_unreferenced_by_an_approved_removal_is_deleted(self) -> None:
        """The reference count is taken AFTER the repository is gone: the scan the
        collection pass runs no longer lists `going.list`, so `shared.gpg` is unused.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert _key_deletions(target) == ["sudo rm -f /etc/apt/keyrings/shared.gpg"]
        source_idx = _index_of(commands, lambda c: "sudo rm -f" in c and "sources.list.d/going.list" in c)
        key_idx = _index_of(commands, lambda c: "sudo rm -f" in c and "keyrings/shared.gpg" in c)
        update_idx = _index_of(commands, lambda c: c == "sudo apt-get update")
        assert source_idx < key_idx < update_idx

    @pytest.mark.asyncio
    async def test_key_still_referenced_by_a_surviving_repo_is_kept(self) -> None:
        """`keeper.list` exists on both machines, so it has no diff of its own and nothing
        in the review mentions it — and it is exactly what keeps `shared.gpg` alive.
        """
        keeper_digest = sha256_line("d-keep", "keeper.list")
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST, "keeper.list": _KEEPER_LIST},
            target_source_digests=sha256_line("d9", "going.list") + keeper_digest,
            target_keyrings=sha256_line("k9", "shared.gpg"),
            source_sources=keeper_digest,
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert any("sources.list.d/going.list" in c for c in _all_removals(target))
        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_key_referenced_only_by_a_file_pc_switcher_never_syncs_is_kept(self) -> None:
        """`/etc/apt/sources.list` is not an item, is never captured and is never deleted —
        and a keyring named only there is still very much in use.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
            sources_list=_KEEPER_LIST,
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_key_referenced_by_a_repo_whose_removal_was_declined_is_kept(self) -> None:
        """Unticking the removal keeps the repository, and the repository keeps its key."""
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST, "extra.list": "deb https://other.example.com stable main\n"},
            target_source_digests=sha256_line("d9", "going.list") + sha256_line("d8", "extra.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
        )
        job = AptSyncJob(context)
        # Only the unrelated repo is approved, so a removal happens and the collection pass
        # runs — but `going.list`, which names the key, stays.
        _install_reviewer(job, {"apt:source:extra.list": Decision.APPLY})

        await job.execute()

        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_key_referenced_by_a_machine_specific_repo_is_kept(self) -> None:
        """A source recorded skip-always produces no diff in any run, so nothing else could
        speak for it — and it still counts as a reference.
        """
        decisions_file = (
            'machine_specific:\n  "apt:source:keeper.list":\n    item_class: apt_source\n'
            "    label: \"keeper.list\"\n    reason: null\n    recorded_at: '2026-07-26T00:00:00Z'\n"
        )
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST, "keeper.list": _KEEPER_LIST},
            target_source_digests=sha256_line("d9", "going.list") + sha256_line("d-keep", "keeper.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
            target_extra={"apt.decisions.yaml": CommandResult(0, decisions_file, "")},
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        assert "apt:source:keeper.list" not in {diff.item_id for diff in plan.diffs}

        job.accept_review(
            plan,
            ReviewOutcome(decisions={"apt:source:going.list": Decision.APPLY}, was_interactive=True),
        )
        await job.apply()

        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_a_key_the_source_machine_still_has_is_never_collected(self) -> None:
        """Collection mirrors: a key both machines carry is configuration this sync is
        replicating, not litter, even when nothing on the target references it yet.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
            source_keyrings=sha256_line("k9", "shared.gpg"),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_a_global_trust_key_is_never_collected(self) -> None:
        """`trusted.gpg.d` is ambient trust nothing references by construction, so "unused"
        is not computable for it. It accumulates rather than being deleted on a guess.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings="",
            target_extra={"find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g9", "ambient.gpg"), "")},
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert not any("ambient.gpg" in c for c in _all_removals(target))

    @pytest.mark.asyncio
    async def test_no_source_removed_means_no_collection_pass_at_all(self) -> None:
        """ "Runs after removing sources" is literal: with no source deletion the pass does
        not run, so it does not even pay for the post-write re-scan.
        """
        context, _source, target = self._context(
            target_sources={},
            target_source_digests="",
            target_keyrings=sha256_line("k9", "orphan.gpg"),
            source_sources=sha256_line("c1", "new.sources"),
            source_extra={"cat /etc/apt/sources.list.d/new.sources": CommandResult(0, _DEB822_FOO, "")},
            source_keyrings=sha256_line("k1", "foo.gpg"),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:new.sources": Decision.APPLY})

        await job.execute()

        assert not _key_deletions(target)
        # One scan only: the plan-time one. A second would be the collection pass running.
        assert sum(1 for c in all_calls(target) if _SOURCE_SCAN_CMD in c) == 1

    @pytest.mark.asyncio
    async def test_a_key_only_the_departing_repo_needs_is_not_refreshed_first(self) -> None:
        """The keyring differs on the two machines, but its only referent is on its way
        out: refreshing it and then collecting it in the same run would be absurd.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k-old", "shared.gpg"),
            source_keyrings=sha256_line("k-new", "shared.gpg"),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert not _key_writes(target)

    @pytest.mark.asyncio
    async def test_a_collected_key_is_backed_up_and_gated_as_a_modification(self) -> None:
        """It is backed up before deletion (so a failing `apt-get update` rolls it back)
        and its deletion carries `mutates=`, so `--confirm-each-command` shows it.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        backup_idx = _index_of(commands, lambda c: c.startswith("sudo cp -a /etc/apt/keyrings/shared.gpg"))
        delete_idx = _index_of(commands, lambda c: c == "sudo rm -f /etc/apt/keyrings/shared.gpg")
        assert backup_idx < delete_idx
        delete_call = next(
            call
            for call in target.run_command.call_args_list
            if call.args[0] == "sudo rm -f /etc/apt/keyrings/shared.gpg"
        )
        assert delete_call.kwargs.get("mutates")


def _all_removals(target: MagicMock) -> list[str]:
    return [c for c in all_calls(target) if c.startswith("sudo rm -f")]


_PIN_SCAN_CMD = "-exec awk '/^Package:/"


class TestPinStanzaParsing:
    """One parser for `preferences.d` `Package:` lines, on both the digest-diff path and
    the target-fact path — the awk one-liner used to keep `$2` alone.
    """

    @staticmethod
    def _facts_context(scan: str) -> JobContext:
        context, _source, _target = make_context(
            target_responses={**_NO_PACKAGES, _PIN_SCAN_CMD: CommandResult(0, scan, "")}
        )
        return context

    @pytest.mark.asyncio
    async def test_a_multi_name_stanza_yields_a_fact_for_every_package(self) -> None:
        context = self._facts_context("/etc/apt/preferences.d/multi\tPackage: foo bar baz\n")

        facts = await AptSyncJob(context).collect_hold_pin_facts()

        assert {fact.package for fact in facts} == {"foo", "bar", "baz"}
        assert {fact.source_ref for fact in facts} == {"/etc/apt/preferences.d/multi"}

    @pytest.mark.asyncio
    async def test_a_wildcard_stanza_yields_no_fact(self) -> None:
        """`Package: *` matches every package to apt and none at all to a name-keyed fact."""
        context = self._facts_context("/etc/apt/preferences.d/all\tPackage: *\n")

        assert await AptSyncJob(context).collect_hold_pin_facts() == []

    @pytest.mark.asyncio
    async def test_the_pin_item_records_the_same_names_the_facts_do(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "multi"), ""),
                "cat /etc/apt/preferences.d/multi": CommandResult(0, "Package: foo bar\nPackage: *\n", ""),
            },
            target_responses={**_NO_PACKAGES},
        )

        plan = await AptSyncJob(context).plan()

        assert any(d.item_id == "apt:pin:multi" for d in plan.diffs)
        assert _parse_pin_file("Package: foo bar\nPackage: *\n") == ("foo", "bar")


# -- The third key directory, ownership-aware provisioning, inline-armored keys ---------
#
# `/usr/share/keyrings` is where `add-apt-repository`, Ubuntu's own sources and most
# vendor `.deb`s put the keyring their `Signed-By:` names. Resolving references against
# `/etc/apt/keyrings` and `/etc/apt/trusted.gpg.d` alone made most real repositories look
# dangling, and a `Signed-By:` carrying an inline armored key was read as a path.

_SHARED_SOURCES = (
    "Types: deb\nURIs: https://vendor.example.com\nSuites: stable\nComponents: main\n"
    "Signed-By: /usr/share/keyrings/vendor.gpg\n"
)
_GHOST_SOURCES = (
    "Types: deb\nURIs: https://ghost.example.com\nSuites: stable\nComponents: main\n"
    "Signed-By: /etc/apt/keyrings/ghost.gpg\n"
)
# What `add-apt-repository` actually writes: the armor's FIRST LINE sits on the field line,
# so a bare `\S+` capture reads `-----BEGIN` as a keyring path.
_INLINE_ON_FIELD_LINE = (
    "Types: deb\nURIs: https://ppa.example.com\nSuites: noble\nComponents: main\n"
    "Signed-By: -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n mDMEY2FrZQ==\n"
    " -----END PGP PUBLIC KEY BLOCK-----\n"
)


def _shared_key_context(
    *,
    filename: str = "vendor.sources",
    content: str = _SHARED_SOURCES,
    source_shared: str = sha256_line("k1", "vendor.gpg"),
    target_shared: str = "",
    dpkg_output: str = "",
) -> tuple[JobContext, MagicMock, MagicMock]:
    """One repository whose `Signed-By:` points into `/usr/share/keyrings`, with the
    target's copy of that directory and its `dpkg -S` answer under the test's control.
    """
    return _repo_context(
        source_responses={
            **_NO_PACKAGES,
            "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", filename), ""),
            f"cat /etc/apt/sources.list.d/{filename}": CommandResult(0, content, ""),
            "find /usr/share/keyrings": CommandResult(0, source_shared, ""),
        },
        target_responses={
            **_NO_PACKAGES,
            "find /usr/share/keyrings": CommandResult(0, target_shared, ""),
            # dpkg -S exits non-zero as soon as ANY argument is unowned, which is the norm:
            # the exit code must not be what decides ownership.
            "dpkg -S": CommandResult(1, dpkg_output, "dpkg-query: no path found matching pattern\n"),
            "test -f /usr/share/keyrings/vendor.gpg": CommandResult(1, "", ""),
            f"test -f /etc/apt/sources.list.d/{filename}": CommandResult(1, "", ""),
            "sudo apt-get update": CommandResult(0, "", ""),
        },
    )


class TestSharedKeyringsDirectory:
    """`/usr/share/keyrings` resolves references, is provisioned for referenced keys only,
    and is never collected.
    """

    @pytest.mark.asyncio
    async def test_a_usr_share_keyrings_reference_resolves_and_the_repo_is_installable(self) -> None:
        context, _source, _target = _shared_key_context()

        plan = await AptSyncJob(context).plan()

        source_diff = next(d for d in plan.diffs if d.item_id == "apt:source:vendor.sources")
        assert source_diff.action == DiffAction.INSTALL
        # The reference resolved, so the detail is the key that travels — never the
        # dangling-reference text that would mean `/usr/share/keyrings` went unseen.
        assert source_diff.detail == "signing key copied with it: vendor.gpg"

    @pytest.mark.asyncio
    async def test_a_hand_placed_key_the_target_lacks_is_provisioned(self) -> None:
        """Nothing on this machine owns `vendor.gpg` — it is as machine-local as anything in
        `/etc/apt/keyrings`, and currently replicated nowhere.
        """
        context, _source, target = _shared_key_context()
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:vendor.sources": Decision.APPLY})

        await job.execute()

        assert _key_writes(target) == ["/usr/share/keyrings/vendor.gpg"]

    @pytest.mark.asyncio
    async def test_a_package_owned_key_present_with_different_bytes_is_not_overwritten(self) -> None:
        """The target's own package manages that file. The repository is still written —
        refusing it over a difference this run deliberately did not touch would strand it.
        """
        context, _source, target = _shared_key_context(
            target_shared=sha256_line("k-old", "vendor.gpg"),
            dpkg_output="vendor-keyring: /usr/share/keyrings/vendor.gpg\n",
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:vendor.sources": Decision.APPLY})

        await job.execute()

        assert _key_writes(target) == []
        assert any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/vendor.sources") for c in all_calls(target)
        )

    @pytest.mark.asyncio
    async def test_a_package_owned_key_the_target_is_missing_is_copied_anyway(self) -> None:
        """The bootstrap case. `dpkg -S` answers from the package's FILE LIST, so a keyring
        can be owned and absent at once — and a vendor `.deb` that ships both a repository
        entry and the keyring trusting it can only be installed once that keyring is there.
        Ownership must gate the OVERWRITE, never the COPY.
        """
        context, _source, target = _shared_key_context(
            # The target has a key directory with something else in it, so ownership really
            # is probed, and dpkg names `vendor.gpg` as owned even though it is not there.
            target_shared=sha256_line("s9", "unrelated.gpg"),
            dpkg_output=(
                "unrelated-keyring: /usr/share/keyrings/unrelated.gpg\n"
                "vendor-keyring: /usr/share/keyrings/vendor.gpg\n"
            ),
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:vendor.sources": Decision.APPLY})

        await job.execute()

        assert _key_writes(target) == ["/usr/share/keyrings/vendor.gpg"]

    @pytest.mark.asyncio
    async def test_ownership_is_probed_once_for_every_key_directory(self) -> None:
        """One batched `dpkg -S` naming every key the target has across all three
        directories — never one call per file.
        """
        context, _source, target = _repo_context(
            source_responses={**_NO_PACKAGES},
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "per-repo.gpg"), ""),
                "find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g1", "legacy.gpg"), ""),
                "find /usr/share/keyrings": CommandResult(0, sha256_line("s1", "shared.gpg"), ""),
            },
        )

        await AptSyncJob(context).plan()

        dpkg_calls = [c for c in all_calls(target) if c.startswith("dpkg -S")]
        assert len(dpkg_calls) == 1
        assert "/etc/apt/keyrings/per-repo.gpg" in dpkg_calls[0]
        assert "/etc/apt/trusted.gpg.d/legacy.gpg" in dpkg_calls[0]
        assert "/usr/share/keyrings/shared.gpg" in dpkg_calls[0]

    @pytest.mark.asyncio
    async def test_a_shared_keyring_no_source_references_is_never_copied(self) -> None:
        """`/usr/share/keyrings` is not mirrored wholesale: it is mostly the distro's own."""
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /usr/share/keyrings": CommandResult(0, sha256_line("s1", "ubuntu-archive-keyring.gpg"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        await job.execute()

        assert _key_writes(target) == []

    @pytest.mark.asyncio
    async def test_a_genuinely_missing_key_is_still_reported_dangling(self) -> None:
        """The check must still bite: `ghost.gpg` exists in no key directory on the source."""
        context, _source, _target = _shared_key_context(
            filename="ghost.sources", content=_GHOST_SOURCES, source_shared=""
        )

        plan = await AptSyncJob(context).plan()

        source_diff = next(d for d in plan.diffs if d.item_id == "apt:source:ghost.sources")
        assert source_diff.action == DiffAction.REPORT_ONLY
        assert source_diff.detail is not None
        assert "/etc/apt/keyrings/ghost.gpg" in source_diff.detail


class TestInlineArmoredSignedBy:
    """A `Signed-By:` value that is not an absolute path is an inline armored key, not a
    reference. Every PPA `add-apt-repository` adds is written that way.
    """

    def test_the_armor_first_line_on_the_field_line_yields_no_ref(self) -> None:
        _fmt, refs, _uris = _parse_source_file("ppa.sources", _INLINE_ON_FIELD_LINE)

        assert refs == ()

    @pytest.mark.asyncio
    async def test_a_ppa_with_an_inline_key_installs_normally_and_needs_no_keyring(self) -> None:
        context, _source, target = _shared_key_context(
            filename="ppa.sources", content=_INLINE_ON_FIELD_LINE, source_shared=""
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:ppa.sources": Decision.APPLY})

        plan = await job.plan()
        assert next(d for d in plan.diffs if d.item_id == "apt:source:ppa.sources").action == DiffAction.INSTALL

        await job.execute()

        assert _key_writes(target) == []
        assert any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/ppa.sources") for c in all_calls(target)
        )


# -- The second review: facts this run's own /etc/apt changes invalidated ---------------
#
# `plan()` reads `preferences.d` and asks apt what it can install; the SAME run rewrites
# `/etc/apt`. A pin the user is deleting still suppressed its packages at plan time, so
# their real diff was withheld and only reappeared on the NEXT run.

_CURL_PIN_SCAN = "/etc/apt/preferences.d/curl-pin\tPackage: curl\n"
_CURL_PIN_FILE = "Package: curl\nPin: version 8.0\nPin-Priority: 1001\n"


class _RecordingReviewer(FakeReviewer):
    """`FakeReviewer` that keeps EVERY call's groups, not just the last one."""

    def __init__(self, decisions: dict[str, Decision]) -> None:
        super().__init__(decisions)
        self.calls: list[tuple[ReviewGroup, ...]] = []

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self.calls.append(tuple(groups))
        return await super().review(groups)


def _pin_lifecycle_target(
    responses: dict[str, CommandResult],
    *,
    before: str,
    after: str,
    trigger: str,
) -> Callable[..., CommandResult]:
    """A target whose `preferences.d` pin SCAN changes once this run issues `trigger` —
    the whole point being that the second scan reads the state the run produced, not the
    one `plan()` saw.
    """
    state = {"converged": False}

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if trigger in cmd:
            state["converged"] = True
        if _PIN_SCAN_CMD in cmd:
            return CommandResult(0, after if state["converged"] else before, "")
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        return CommandResult(0, "", "")

    return _side_effect


def _actionable_entry_ids(groups: Sequence[ReviewGroup]) -> set[str]:
    """Item ids the user was actually offered a converge action for. A `REPORT_ONLY` entry
    is shown but implies no verb, so it is exactly what the suppressed cases look like.
    """
    return {
        entry.item_id for group in groups if group.action != DiffAction.REPORT_ONLY.value for entry in group.entries
    }


class TestSecondReviewAfterRepositoryChanges:
    """A decision the user could not have made correctly the first time is asked again,
    not deferred to the next run (ADR-020 D-24: batching is a preference, not a hard rule).
    """

    @staticmethod
    def _deleting_pin_context(pin_decision: Decision) -> tuple[AptSyncJob, MagicMock, _RecordingReviewer]:
        """`curl` is extra on the target and governed by `curl-pin`, which the source
        machine does not have — so the pin is offered for deletion and `curl`'s own removal
        diff is suppressed into `HELD_OR_PINNED` at plan time.
        """
        context, _source, target = _repo_context(
            target_side_effect=_pin_lifecycle_target(
                {
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                    "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                    "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                    "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, _CURL_PIN_FILE, ""),
                    "apt-get -s remove -y curl": CommandResult(0, "Remv curl [8.0]\n", ""),
                },
                before=_CURL_PIN_SCAN,
                after="",
                trigger="sudo rm -f /etc/apt/preferences.d/curl-pin",
            ),
        )
        job = AptSyncJob(context)
        reviewer = _RecordingReviewer({"apt:pin:curl-pin": pin_decision, "apt:package:curl": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)
        return job, target, reviewer

    @pytest.mark.asyncio
    async def test_the_pin_hides_the_package_in_the_first_review(self) -> None:
        job, _target, reviewer = self._deleting_pin_context(Decision.APPLY)

        plan = await job.plan()

        curl = next(d for d in plan.diffs if d.item_id == "apt:package:curl")
        assert (curl.diff_class, curl.action) == (DiffClass.HELD_OR_PINNED, DiffAction.REPORT_ONLY)
        assert reviewer.call_count == 0

    @pytest.mark.asyncio
    async def test_deleting_the_pin_reveals_the_package_in_a_second_review_this_same_run(self) -> None:
        job, target, reviewer = self._deleting_pin_context(Decision.APPLY)

        await job.execute()

        assert reviewer.call_count == 2
        assert "apt:package:curl" not in _actionable_entry_ids(reviewer.calls[0])
        assert "apt:package:curl" in _actionable_entry_ids(reviewer.calls[1])
        assert all("revealed by this run's /etc/apt changes" in group.title for group in reviewer.calls[1])

        commands = all_calls(target)
        pin_idx = _index_of(commands, lambda c: c == "sudo rm -f /etc/apt/preferences.d/curl-pin")
        remove_idx = _index_of(commands, lambda c: "apt-get remove -y curl" in c and c.startswith("sudo DEBIAN"))
        assert pin_idx < remove_idx

    @pytest.mark.asyncio
    async def test_keeping_the_pin_keeps_the_package_hidden_and_asks_nothing_twice(self) -> None:
        """The user unticks the pin deletion: nothing about `/etc/apt` changes, so there is
        no invalidated fact and no second screen.
        """
        job, target, reviewer = self._deleting_pin_context(Decision.SKIP_ONCE)

        await job.execute()

        assert reviewer.call_count == 1
        assert not any("apt-get remove -y curl" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_pin_this_run_installs_withdraws_the_approval_it_contradicts(self) -> None:
        """The other direction. The user approved removing `curl` AND installing a pin file
        that governs it. Once the pin lands, the removal is no longer supported — the
        approval is dropped silently, because withdrawing work needs no decision.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, _CURL_PIN_FILE, ""),
            },
            target_side_effect=_pin_lifecycle_target(
                {
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                    "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                    "apt-get -s remove -y curl": CommandResult(0, "Remv curl [8.0]\n", ""),
                },
                before="",
                after=_CURL_PIN_SCAN,
                trigger="sudo install -o root -g root -m 0644",
            ),
        )
        job = AptSyncJob(context)
        reviewer = _RecordingReviewer({"apt:pin:curl-pin": Decision.APPLY, "apt:package:curl": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert reviewer.call_count == 1, "withdrawing work asks the user nothing"
        assert not any(c.startswith("sudo DEBIAN") and "remove -y curl" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_run_with_no_etc_apt_work_re_reads_nothing_and_reviews_once(self) -> None:
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "apt-cache policy": CommandResult(0, _POLICY_AVAILABLE, ""),
                "apt-get -s install": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
            },
        )
        job = AptSyncJob(context)
        reviewer = _RecordingReviewer({"apt:package:pkg-a": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert reviewer.call_count == 1
        assert len([c for c in all_calls(target) if _PIN_SCAN_CMD in c]) == 1, "nothing is re-read"

    @pytest.mark.asyncio
    async def test_a_dry_run_converges_nothing_so_it_reviews_once(self) -> None:
        job, target, reviewer = self._deleting_pin_context(Decision.APPLY)
        job.context = dataclasses.replace(job.context, dry_run=True)

        await job.execute()

        assert reviewer.call_count == 1
        assert not any(c.startswith("sudo rm -f") for c in all_calls(target))


class TestNewRepositoryMakesAPackageAvailable:
    """The general class the second review closes, not just the pin symptom: at plan time
    the target's apt reports no candidate; the repository this run installs supplies one.
    """

    @pytest.mark.asyncio
    async def test_a_package_apt_could_not_offer_at_plan_time_is_reviewed_once_the_repo_lands(self) -> None:
        policy_results = [CommandResult(0, _POLICY_NO_CANDIDATE, ""), CommandResult(0, _POLICY_AVAILABLE, "")]
        state = {"calls": 0}

        def _target(cmd: str, **_: object) -> CommandResult:
            if "apt-cache policy" in cmd:
                index = min(state["calls"], len(policy_results) - 1)
                state["calls"] += 1
                return policy_results[index]
            for pattern, result in {
                "echo $HOME": CommandResult(0, "/home/target-user", ""),
                "apt-mark showmanual": CommandResult(0, "", ""),
                "test -f": CommandResult(1, "", ""),
                "apt-get -s install": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
            }.items():
                if pattern in cmd:
                    return result
            return CommandResult(0, "", "")

        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_side_effect=_target,
        )
        job = AptSyncJob(context)
        reviewer = _RecordingReviewer({"apt:source:foo.sources": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert "apt:package:pkg-a" not in _actionable_entry_ids(reviewer.calls[0])
        assert "apt:package:pkg-a" in _actionable_entry_ids(reviewer.calls[1])
        assert any(c.startswith("sudo DEBIAN") and "install" in c and "pkg-a" in c for c in all_calls(target))


def _stub_executor(responses: dict[str, CommandResult]) -> MagicMock:
    """A minimal `Executor`-shaped stub matching by substring (first match wins)."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        raise AssertionError(f"no stub response configured for command: {cmd!r}")

    executor = MagicMock()
    executor.run_command = AsyncMock(side_effect=_side_effect)
    return executor


class TestCompareDebVersions:
    """`compare_deb_versions` delegates ordering to `dpkg --compare-versions`."""

    @pytest.mark.asyncio
    async def test_lt_for_debian_revision_ordering(self) -> None:
        executor = _stub_executor(
            {
                "1.0-1 lt 1.0-2": CommandResult(0, "", ""),
                "1.0-1 gt 1.0-2": CommandResult(1, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "1.0-1", "1.0-2")

        assert result < 0

    @pytest.mark.asyncio
    async def test_gt_for_epoch_beats_larger_upstream_number(self) -> None:
        """`2:1.0` outranks `10.0` — the epoch outranks the larger upstream number."""
        executor = _stub_executor(
            {
                "2:1.0 lt 10.0": CommandResult(1, "", ""),
                "2:1.0 gt 10.0": CommandResult(0, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "2:1.0", "10.0")

        assert result > 0

    @pytest.mark.asyncio
    async def test_equal_for_identical_strings_without_a_second_executor_call(self) -> None:
        executor = _stub_executor({})

        result = await compare_deb_versions(executor, "1.0-1", "1.0-1")

        assert result == 0
        executor.run_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_shells_out_with_shlex_quoted_operands(self) -> None:
        executor = _stub_executor(
            {
                "dpkg --compare-versions 'a b' lt 'c;d'": CommandResult(0, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "a b", "c;d")

        assert result < 0
        first_call = executor.run_command.call_args_list[0]
        assert "'a b'" in first_call.args[0]
        assert "'c;d'" in first_call.args[0]

    @pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg not available on this machine")
    @pytest.mark.asyncio
    async def test_real_dpkg_confirms_epoch_and_revision_ordering(self) -> None:
        """Cross-checks the stub-based tests above against the real binary."""
        executor = LocalExecutor()

        assert await compare_deb_versions(executor, "2:1.0", "10.0") > 0
        assert await compare_deb_versions(executor, "1.0-1", "1.0-2") < 0
        assert await compare_deb_versions(executor, "1.0-1", "1.0-1") == 0
