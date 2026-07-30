"""Unit tests for AptSyncJob and the shared PackageSyncJob plan()/apply() split.

Covers the tracer's single path — one apt package missing on the target — through
capture, diff, plan/apply separation, the coordinator-accepted-plan ordering guard,
converge (with the apt-get --dry-run transaction guard), dry-run, continue-on-failure, and
validate(). All executor interactions are mocked; no real apt/dpkg/sudo commands run.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import re
import shlex
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import override
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.executor import LocalExecutor
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob, simulate_apt_transaction
from pcswitcher.jobs.apt_sync.commands import TARGET_SUDO_COMMANDS, compare_deb_versions
from pcswitcher.jobs.apt_sync.diffing import diff_apt_packages
from pcswitcher.jobs.apt_sync.esm_gate import PRO_STATUS_COMMAND
from pcswitcher.jobs.apt_sync.items import APT_PREFERENCES_DIR, AptPackageItem
from pcswitcher.jobs.apt_sync.messages import (
    build_origin_detail,
    build_origin_mismatch_detail,
    build_origin_refusal_detail,
    build_repo_removal_detail,
    build_repo_unavailable_detail,
)
from pcswitcher.jobs.apt_sync.origins import OriginOutcome, OriginPlan
from pcswitcher.jobs.apt_sync.probe import AptProbe, SourceFileRefs, parse_source_file
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import (
    _REMOVAL_ACTIONS,
    COLLATERAL_REVIEW_ACTION,
    REPO_CONFLICT_REVIEW_ACTION,
    REPO_REMOVAL_REVIEW_ACTION,
    Decision,
    ReviewGroup,
    ReviewOutcome,
    _is_promotable_group,
    _is_removal_direction,
)
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, Host, JobSkipped
from pcswitcher.orchestrator import Orchestrator

# The real `apt-cache policy` blocks manual_installs_sync is tested against, imported
# rather than copied: the A11 ruling is that both jobs decide bare-`.deb` ownership from
# the SAME evidence, which two independently-drifting fixtures would stop demonstrating.
from tests.unit.jobs.test_apt_policy import (
    POLICY_ARCHIVE_CANDIDATE_UNINSTALLED,
    POLICY_INSTALLED_AND_CANDIDATE_DIFFER,
    POLICY_MOZILLA_FIREFOX_INSTALLED,
)
from tests.unit.jobs.test_manual_installs_sync import (
    _POLICY_AUTO_DEP,
    _POLICY_HAND_DEB,
    _POLICY_PINNED_NO_CANDIDATE,
    _POLICY_REPO_INSTALLED,
)
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


# The archive every baseline source fixture is on. Paired with a `ubuntu.sources` scan line
# declaring it, so it resolves to a DISTRIBUTION origin and no package acquires a vendor
# origin it was never given (ADR-020 D-35's exemption).
_BASELINE_ARCHIVE = "http://ftp.belnet.be/ubuntu"


def respond_to_source(mapping: dict[str, CommandResult]) -> Callable[..., CommandResult]:
    """`respond_to`, plus the two answers every real source machine gives about its own
    packages, for a fixture that does not state them.

    A source `apt-cache policy` that prints nothing is a BROKEN apt, not a machine with
    unusual packages — apt prints one block per installed name it is asked about — and
    `_require_apt_answer` now says so rather than reading the silence as "no package has a
    vendor origin". So the baseline answers with one archive block per queried name, plus
    the `ubuntu.sources` scan line that makes that archive a distribution origin. Any test
    with an opinion about either overrides its key and this never fires.
    """
    inner = respond_to(mapping)

    def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
        if not any(pattern in cmd for pattern in mapping):
            if cmd.startswith("apt-cache policy"):
                names = shlex.split(cmd)[2:]
                return CommandResult(0, "".join(_policy_block(name, _BASELINE_ARCHIVE) for name in names), "")
            if _SOURCE_SCAN_CMD in cmd:
                return CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_BELNET), "")
        return inner(cmd, **kwargs)

    return _side_effect


def respond_to_target_apt(
    mapping: dict[str, CommandResult], *, cannot_locate: Sequence[str] = ()
) -> Callable[..., CommandResult]:
    """`respond_to`, plus the one target behaviour the substring fixtures cannot express: a
    real `apt-get --dry-run` exits 100 with `E: Unable to locate package` for a name the
    target's repositories do not carry, and takes the WHOLE batch down with it.

    Name-sensitive on purpose. A blanket `"apt-get --dry-run": CommandResult(100, ...)` entry
    would also fail a rehearsal of packages the target can resolve, so a test could pass
    because the simulation stopped happening rather than because it stopped naming the
    unlocatable package.
    """
    inner = respond_to(mapping)
    unknown = frozenset(cannot_locate)

    def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
        if cmd.startswith("apt-get --dry-run"):
            asked = sorted(unknown & frozenset(shlex.split(cmd)))
            if asked:
                return CommandResult(100, "", f"E: Unable to locate package {asked[0]}\n")
        return inner(cmd, **kwargs)

    return _side_effect


def make_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    target_side_effect: Callable[..., CommandResult] | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to_source(source_responses or {}))
    target = MagicMock()
    target.run_command = AsyncMock(side_effect=target_side_effect or respond_to(target_responses or {}))
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


# The names `make_context` gives the two machines — every review string this job builds
# says one of them, so the assertions below check the machine, not a role word.
MACHINES = Machines(source="source-host", target="target-host")


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
        probe = AptProbe(context.source, context.target)

        items, _origins = await probe.capture_source_items()

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
        probe = AptProbe(context.source, context.target)

        await probe.capture_source_items()

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
        probe = AptProbe(context.source, context.target)

        source_items, _origins = await probe.capture_source_items()
        target_items = await probe.query_target_items()
        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES)

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
        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES)

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
            # `apt-get --dry-run` (simulate) IS expected during plan() — plan 02-05's
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
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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

        # `apt-get --dry-run` (read-only plan-time collateral simulation) still runs even
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
                "apt-cache policy": CommandResult(0, _target_offers("pkg-a", "pkg-b", "pkg-c"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": clean_preview,
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-b": clean_preview,
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-c": clean_preview,
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-b": (
                    CommandResult(1, "", "dpkg error for pkg-b")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-c": (
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
        simulations = [c for c in commands if "apt-get --dry-run" in c]
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
        sim_cmd = "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a"
        state = {"sim": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == "apt-mark showmanual":
                return CommandResult(0, "ghost-pkg\n", "")
            if "dpkg-query" in cmd:
                return CommandResult(0, "ghost-pkg\t1.0\n", "")
            if cmd.startswith("apt-cache policy"):
                return CommandResult(0, _target_offers("pkg-a"), "")
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
                "apt-cache policy": CommandResult(0, _target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv auto-dep [1.0]\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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
        failed `apt-get --dry-run` (dpkg lock contention, unmet dependencies, ...) as an
        empty, falsely-clean preview — that would let both call sites proceed with
        the real command as if nothing would happen.
        """
        target = MagicMock()
        target.run_command = AsyncMock(
            return_value=CommandResult(100, "", "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'")
        )

        with pytest.raises(ConvergeItemFailed, match="dpkg was interrupted"):
            await simulate_apt_transaction(
                target, "install --assume-yes --no-install-recommends pkg-a", login_shell=False
            )

    @pytest.mark.asyncio
    async def test_apply_time_simulation_failure_fails_the_item_not_silently_clean(self) -> None:
        """A plan-time simulation can succeed (nothing wrong yet) while the same
        command fails when re-run at apply time; the item must fail cleanly through
        the normal per-item path rather than the real `apt-get install` running
        against an untrustworthy preview.
        """
        install_cmd = "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a"
        state = {"calls": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == install_cmd:
                state["calls"] += 1
                if state["calls"] == 1:
                    return CommandResult(0, "Inst pkg-a (1.0)\n", "")
                return CommandResult(100, "", "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'")
            if cmd.startswith("apt-cache policy"):
                return CommandResult(0, _target_offers("pkg-a"), "")
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
    """collect_hold_sets: apt-mark showhold on BOTH machines. Pins are read no more: they
    are files, not facts about packages (ADR-020 D-25/D-36)."""

    @pytest.mark.asyncio
    async def test_hold_sets_from_both_machines_surface(self) -> None:
        context, _source, _target = make_context(
            source_responses={"apt-mark showhold": CommandResult(0, "pkg-src-held\n", "")},
            target_responses={"apt-mark showhold": CommandResult(0, "pkg-tgt-held\n", "")},
        )
        probe = AptProbe(context.source, context.target)

        source_holds, target_holds = await probe.collect_hold_sets()

        assert source_holds == frozenset({"pkg-src-held"})
        assert target_holds == frozenset({"pkg-tgt-held"})


class TestAptHold:
    """#208: hold replication — `apt:hold:` membership items, converge via `apt-mark`, a
    held package never double-reported, and sudo scope."""

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
    async def test_held_package_yields_hold_item_not_a_duplicate_package_report(self) -> None:
        """A target-held package produces the `apt:hold:` item and NOT a package-level
        report for the same name (#208 dedup)."""
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
        assert any("mv --force" in cmd and "apt.decisions" in cmd for cmd in source_cmds)

    def test_apt_mark_is_in_the_target_sudo_command_list(self) -> None:
        assert "/usr/bin/apt-mark" in TARGET_SUDO_COMMANDS


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
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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
        """A derived `/etc/apt` write makes `accept_review` rebuild the plan around the
        metadata-refresh marker (repo items, marker, packages, holds) — the hold must stay
        behind its package install through that rebuild too.
        """
        context, _source, target = _repo_context(
            source_responses=_foo_source_responses(**{"apt-mark showhold": CommandResult(0, "pkg-a\n", "")})
        )
        target.run_command = AsyncMock(
            side_effect=_foo_target_side_effect(
                {
                    "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                    "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
                }
            )
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {**_APPROVE_PKG_A, "apt:hold:pkg-a": Decision.APPLY})

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
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-good": CommandResult(
                    0, "Inst pkg-good (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-good": (
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
    `apt-get --dry-run` preview at plan time and none at converge time.
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
        assert not any("apt-get --dry-run" in c for c in commands)


class TestUnavailableCapture:
    """ONE batched `apt-cache policy` on the target answers every origin question this run
    asks of it, and a package whose origin cannot be provided there is reported rather than
    installed from somewhere else (ADR-020 D-34).
    """

    @pytest.mark.asyncio
    async def test_a_package_no_repository_can_supply_is_reported_not_installed(self) -> None:
        """The source's origin is declared by no file the source still has, and the target's
        apt says it will install nothing: two answers that agree, so the package is reported.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "dpkg-query": CommandResult(0, "brscan3\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("brscan3", "https://gone.example.com/apt"), ""),
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
    async def test_one_batched_policy_call_covers_every_package(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://vendor.example.com/apt")
                    + _policy_block("pkg-b", "https://vendor.example.com/apt"),
                    "",
                ),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://vendor.example.com/apt")
                    + "pkg-b:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n",
                    "",
                ),
                "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\nInst pkg-b (1.0)\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        policy_calls = [cmd for cmd in all_calls(target) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        assert "pkg-a" in policy_calls[0]
        assert "pkg-b" in policy_calls[0]

        by_id = {diff.item_id: diff for diff in plan.diffs}
        # pkg-a: the target's candidate is already the source's origin -> ordinary install.
        assert by_id["apt:package:pkg-a"].diff_class == DiffClass.MISSING_ON_TARGET
        # pkg-b: the target has no candidate, but the source declares the origin in a file
        # that can travel -> still an install, with that repository derived from it.
        assert by_id["apt:package:pkg-b"].diff_class == DiffClass.MISSING_ON_TARGET
        assert job._work.origins.plans["apt:package:pkg-b"].derived_files == frozenset({"vendor.list"})  # pyright: ignore[reportPrivateUsage]


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
                "dpkg --search": CommandResult(0, "", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "dpkg-query": CommandResult(0, "brscan3\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.UNREPRODUCIBLE for d in plan.diffs)


class TestBareDebPackagesAreNotAptSyncsBusiness:
    """A11/D-18: a package whose INSTALLED version comes from no configured repository was
    put there with `dpkg --install`, so apt cannot install it anywhere and the target's apt has
    never heard the name. `manual_installs_sync` offers it as an install snippet in the same
    run; `apt_sync` drops it at CAPTURE, so it is structurally absent from every downstream
    stage rather than filtered out of each one.

    Both jobs read the same real `apt-cache policy` blocks, imported rather than copied:
    the point of the ruling is that the two answer the predicate identically, which a
    paraphrased fixture would stop proving.
    """

    @pytest.mark.asyncio
    async def test_bare_deb_package_produces_no_diff_and_no_review_entry(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.129.1-1784303641\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert list(plan.diffs) == []
        assert not any("code" in entry.item_id for group in plan.groups for entry in group.entries)

    @pytest.mark.asyncio
    async def test_bare_deb_package_reaches_no_apt_get_install(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.129.1-1784303641\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:code": Decision.APPLY})

        await job.execute()

        assert not any("apt-get install" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_repo_installed_package_is_still_captured_and_diffed(self) -> None:
        """The guard against over-excluding: `gh`'s block also carries a
        `/var/lib/dpkg/status` line, as every installed package's does."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.item_id, d.diff_class, d.action) for d in plan.diffs] == [
            ("apt:package:gh", DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)
        ]

    @pytest.mark.asyncio
    async def test_one_source_policy_call_covers_the_whole_manual_set(self) -> None:
        policy = _POLICY_HAND_DEB + _POLICY_REPO_INSTALLED + _POLICY_PINNED_NO_CANDIDATE + _POLICY_AUTO_DEP
        context, source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\ngh\ndocker.io\n7zip\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.0\ngh\t2.96.0\ndocker.io\t29.1\n7zip\t23.01\n", ""),
                "apt-cache policy": CommandResult(0, policy, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        policy_calls = [cmd for cmd in all_calls(source) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        for name in ("code", "gh", "docker.io", "7zip"):
            assert name in policy_calls[0]
        # Only `code` is hand-installed; the negatively-pinned and auto-dependency packages
        # both have repository origins and stay apt_sync's to install.
        assert {d.item_id for d in plan.diffs} == {
            "apt:package:gh",
            "apt:package:docker.io",
            "apt:package:7zip",
        }

    @pytest.mark.asyncio
    async def test_excluded_package_reaches_neither_the_simulation_nor_the_availability_probe(self) -> None:
        """Both downstream target reads are built from the diffs, so a package excluded at
        capture cannot appear in the transaction `_collect_plan_time_collateral` asks apt to
        rehearse, nor in the target's origin probe."""
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\ngh\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.0\ngh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB + _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                # Both names offered by the target, so nothing but the capture-time exclusion
                # can keep `code` out of either downstream read.
                "apt-cache policy": CommandResult(0, _target_offers("code", "gh"), ""),
            },
        )

        await AptSyncJob(context).plan()

        simulations = [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        assert simulations and all("code" not in cmd for cmd in simulations)
        assert any("gh" in cmd for cmd in simulations)

        probes = [cmd for cmd in all_calls(target) if "apt-cache policy" in cmd]
        assert probes and all("code" not in cmd for cmd in probes)
        assert any("gh" in cmd for cmd in probes)

    @pytest.mark.asyncio
    async def test_repo_installed_package_the_target_has_never_heard_of_is_still_offered(self) -> None:
        """The target half is untouched (`collect_target_policy`): the target's apt
        printing no block is still "no evidence against", because a repository this same run
        adds may be about to supply the package."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, "", "N: Unable to locate package gh\n"),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)]

    @pytest.mark.asyncio
    async def test_a_name_an_answered_policy_printed_no_block_for_is_not_excluded(self) -> None:
        """Silence inside an ANSWERED probe is not evidence: apt spoke, and it said nothing
        about `ghost-pkg`, which is not the same as saying it came from no repository.
        Indicting on that absence would drop the package from the sync without a word.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nghost-pkg\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nghost-pkg\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", _BASELINE_ARCHIVE), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert [d.item_id for d in plan.diffs] == ["apt:package:pkg-a", "apt:package:ghost-pkg"]

    @pytest.mark.asyncio
    async def test_a_source_policy_that_did_not_run_fails_the_run_naming_the_command(self) -> None:
        """The other side of the same distinction, and a deliberate reversal: a policy read
        that EXITED NON-ZERO answered nothing about any package. Tolerating it silently
        exempted every package from the D-35 origin check and offered
        `manual_installs_sync`'s bare-`.deb` packages as apt installs, both without a word.

        The stdout is a COMPLETE, parseable block on purpose: it isolates the exit code as
        the only thing that can catch this, so the zero-block rule cannot pass the test on
        the exit code's behalf.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    100, _policy_block("pkg-a", _BASELINE_ARCHIVE), "E: could not read the package lists\n"
                ),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-cache policy pkg-a" in str(excinfo.value)
        assert "exited 100" in str(excinfo.value)
        assert "could not read the package lists" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_source_policy_that_printed_nothing_at_all_fails_the_run(self) -> None:
        """Exit 0 and no block for a single name apt must know. Measured: apt prints one
        block per installed name it is asked about, so this output is not apt's answer.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, "", ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "printed no package block" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_excluded_bare_deb_package_is_not_protected_from_collateral(self) -> None:
        """`code` is a bare `.deb` on the source, so it is dropped from the manifest, and it
        is auto on the target. Under ADR-020 D-40 the target's apt owns it: an install whose
        simulation would remove it proceeds with no collateral item and no prompt.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\ngh\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.0\ngh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB + _POLICY_REPO_INSTALLED, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends gh": CommandResult(
                    0, "Inst gh (2.96.0)\nRemv code [1.0]\n", ""
                ),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert not any(d.item_id == "apt:collateral:code" for d in plan.diffs)


class TestRemovalConverge:
    @pytest.mark.asyncio
    async def test_remove_diff_issues_real_apt_get_remove_for_that_package_alone(self) -> None:
        context, _source, target = make_context(
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-extra\n", ""),
                "dpkg-query": CommandResult(0, "pkg-extra\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-extra": CommandResult(0, "Remv pkg-extra [1.0]\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes pkg-extra": CommandResult(0, "", ""),
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
                "apt-get --dry-run remove --assume-yes pkg-a": CommandResult(
                    0, "Remv pkg-a [1.0]\nRemv pkg-b [1.0]\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes pkg-a": CommandResult(0, "", ""),
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
        sim_cmd = "apt-get --dry-run remove --assume-yes pkg-a"
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
                "apt-get --dry-run remove --assume-yes pkg-a": CommandResult(
                    0, "Remv pkg-a [1.0]\nRemv pkg-b [1.0]\n", ""
                ),
                "apt-get --dry-run remove --assume-yes pkg-b": CommandResult(0, "Remv pkg-b [1.0]\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes pkg-a": CommandResult(0, "", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes pkg-b": CommandResult(0, "", ""),
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
        sim_cmd = "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a"
        state = {"sim": 0}

        static = {
            "apt-mark showmanual": CommandResult(0, "manual-dg\n", ""),
            "dpkg --compare-versions 1.0 lt 2.0": CommandResult(0, "", ""),
        }

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == sim_cmd:
                state["sim"] += 1
                if state["sim"] == 1:
                    return CommandResult(0, "Inst pkg-a (1.0)\n", "")
                return CommandResult(0, "Inst pkg-a (1.0)\nInst manual-dg [2.0] (1.0)\n", "")
            if "dpkg-query" in cmd:
                return CommandResult(0, "manual-dg\t2.0\n", "")
            if cmd.startswith("apt-cache policy"):
                return CommandResult(0, _target_offers("pkg-a"), "")
            return static.get(cmd, CommandResult(0, "", ""))

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
                "apt-cache policy": CommandResult(0, _target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst auto-dg [2.0] (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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
                "apt-cache policy": CommandResult(0, _target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
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
        assert collateral[0].detail == "Installing pkg-a on target-host would remove other-manual"

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
                "apt-cache policy": CommandResult(0, _target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
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
                "apt-cache policy": CommandResult(0, _target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
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
                "apt-cache policy": CommandResult(0, _target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\n", ""
                ),
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
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _target_offers(*names), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 10
        simulations = [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        # One, not zero: ten resolvable candidates rehearse in a single batch.
        assert len(simulations) == 1
        assert all(name in simulations[0] for name in names)


_TARGET_GH_NO_CANDIDATE = "gh:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"


class TestAPackageTheTargetCannotResolveYet:
    """ADR-020 D-34 class 3 at plan time: the repository that supplies the package is derived
    from the package's own approval and written during converge, so the target's apt has no
    candidate for the name while `plan()` runs and refuses to rehearse a transaction naming
    it — with the same exit 100 a held dpkg lock produces (ADR-022 D-01).
    """

    @pytest.mark.asyncio
    async def test_plan_survives_a_candidate_the_targets_apt_cannot_locate(self) -> None:
        """The phase's flagship scenario: `gh` comes from `cli.github.com` on the source, the
        target has never heard the name, and the batched rehearsal must not abort the run
        before the user is shown anything.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_side_effect=respond_to_target_apt(
                {"apt-mark showmanual": CommandResult(0, "", ""), "apt-cache policy": CommandResult(0, "", "")},
                cannot_locate=["gh"],
            ),
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)]
        assert not [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        # The premise, asserted rather than assumed: this target really does refuse `gh`, so
        # a fixture that quietly lost its exit 100 cannot carry the test on its own.
        assert not (await target.run_command("apt-get --dry-run install gh")).success

    @pytest.mark.asyncio
    async def test_an_explicit_no_candidate_is_excluded_on_the_same_evidence(self) -> None:
        """apt saying `Candidate: (none)` and apt printing no block at all are different
        answers everywhere else in this job, and the same one here: neither names a version
        the target could install, so neither can enter the rehearsal.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_side_effect=respond_to_target_apt(
                {
                    "apt-mark showmanual": CommandResult(0, "", ""),
                    "apt-cache policy": CommandResult(0, _TARGET_GH_NO_CANDIDATE, ""),
                },
                cannot_locate=["gh"],
            ),
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)]
        assert not [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        # The premise, asserted rather than assumed: this target really does refuse `gh`, so
        # a fixture that quietly lost its exit 100 cannot carry the test on its own.
        assert not (await target.run_command("apt-get --dry-run install gh")).success

    @pytest.mark.asyncio
    async def test_the_resolvable_candidates_are_still_rehearsed_and_still_protected(self) -> None:
        """A narrowing, not a shutdown. `pkg-b` is resolvable, stays in the one batched
        rehearsal alongside nothing else, and its manual collateral still reaches the review
        — which is what the run loses entirely if the whole simulation is skipped instead.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\npkg-b\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\npkg-b\t1.0\nother-manual\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _POLICY_REPO_INSTALLED
                    + _policy_block("pkg-b", _BASELINE_ARCHIVE)
                    + _policy_block("other-manual", _BASELINE_ARCHIVE),
                    "",
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_side_effect=respond_to_target_apt(
                {
                    "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                    "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                    "apt-cache policy": CommandResult(
                        0,
                        _policy_block("pkg-b", _BASELINE_ARCHIVE) + _policy_block("other-manual", _BASELINE_ARCHIVE),
                        "",
                    ),
                    "apt-get --dry-run install --assume-yes --no-install-recommends pkg-b": CommandResult(
                        0, "Inst pkg-b (1.0)\nRemv other-manual [1.0]\n", ""
                    ),
                },
                cannot_locate=["gh"],
            ),
        )

        plan = await AptSyncJob(context).plan()

        simulations = [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        assert simulations == ["apt-get --dry-run install --assume-yes --no-install-recommends pkg-b"]
        assert "apt:collateral:other-manual" in {d.item_id for d in plan.diffs}
        assert {d.item_id for d in plan.diffs if d.action == DiffAction.INSTALL} == {
            "apt:package:gh",
            "apt:package:pkg-b",
        }
        # The premise, asserted rather than assumed: adding `gh` to that one command is what
        # a real target refuses, and it is the only reason the command may not name it.
        assert not (await target.run_command("apt-get --dry-run install gh pkg-b")).success
        assert (await target.run_command("apt-get --dry-run install --assume-yes pkg-b")).success


def _manual_collateral_context() -> tuple[JobContext, MagicMock, MagicMock]:
    """A job whose only install candidate (`pkg-a`) would, per the simulation, remove the
    manually-installed `other-manual` — the shared fixture for the go-ahead / keep-the-package
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
            "apt-cache policy": CommandResult(0, _target_offers("pkg-a"), ""),
            "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""
            ),
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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


def _two_independent_removals_context() -> tuple[JobContext, MagicMock, MagicMock]:
    """Two removal candidates whose transactions are independent: removing `pkg-x` also
    removes the manually-installed `other-manual`, removing `pkg-y` removes nothing else.

    The batched rehearsal cannot tell those two apart — it names both candidates and one
    collateral package — so this fixture also answers the per-candidate rehearsals the
    attribution needs. `other-manual` is manual and identical on both machines, so it is
    collateral and never a diff of its own.
    """
    return make_context(
        source_responses={
            "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
            "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
        },
        target_responses={
            "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\nother-manual\n", ""),
            "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\nother-manual\t1.0\n", ""),
            # Longest first: `respond_to` matches by substring, first match wins.
            "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""
            ),
            "remove --assume-yes pkg-x": CommandResult(0, "Remv pkg-x [1.0]\nRemv other-manual [1.0]\n", ""),
            "remove --assume-yes pkg-y": CommandResult(0, "Remv pkg-y [1.0]\n", ""),
        },
    )


class TestCollateralAttribution:
    """D-30: a collateral item's triggers are the candidates whose OWN transaction causes
    it, so declining it cancels those and nothing else.
    """

    @pytest.mark.asyncio
    async def test_skip_cancels_only_the_candidate_whose_transaction_causes_it(self) -> None:
        """`pkg-y` removes nothing but itself, so a skip on `other-manual` must leave it
        approved and remove it — while `pkg-x`, which really would take `other-manual` with
        it, is the only candidate the skip cancels.
        """
        context, _source, target = _two_independent_removals_context()
        job = AptSyncJob(context)
        _install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.APPLY,
                "apt:collateral:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        real_removals = [cmd for cmd in all_calls(target) if "sudo" in cmd and "apt-get remove" in cmd]
        assert len(real_removals) == 1
        assert "pkg-y" in real_removals[0]
        assert "pkg-x" not in real_removals[0]

    @pytest.mark.asyncio
    async def test_a_collateral_skip_does_not_discard_a_trigger_own_skip_always(self) -> None:
        """The permanent decision is the user's, not the collateral question's. Both
        candidates really do take `other-manual` with them, so both are cancelled by the
        skip — but `pkg-y`'s "always skip" must survive the
        cancellation and still be recorded (D-08a: a REMOVE is target-held).
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\nother-manual\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""
                ),
                "remove --assume-yes pkg-x": CommandResult(0, "Remv pkg-x [1.0]\nRemv other-manual [1.0]\n", ""),
                "remove --assume-yes pkg-y": CommandResult(0, "Remv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.SKIP_ALWAYS,
                "apt:collateral:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        commands = all_calls(target)
        recorded = [cmd for cmd in commands if "mv --force" in cmd and "apt.decisions" in cmd]
        assert len(recorded) == 1
        assert "apt:package:pkg-y" in recorded[0]
        assert "apt:package:pkg-x" not in recorded[0]
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_a_collateral_skip_does_not_discard_an_unrelated_skip_always(self) -> None:
        """The same protection where attribution alone would also have saved it: `pkg-y` is
        no trigger of `other-manual`, so nothing may touch its permanent decision.
        """
        context, _source, target = _two_independent_removals_context()
        job = AptSyncJob(context)
        _install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.SKIP_ALWAYS,
                "apt:collateral:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        commands = all_calls(target)
        recorded = [cmd for cmd in commands if "mv --force" in cmd and "apt.decisions" in cmd]
        assert len(recorded) == 1
        assert "apt:package:pkg-y" in recorded[0]
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_the_narrowing_names_the_causing_candidate_in_the_question(self) -> None:
        """Attribution reaches the user, not just the decision map: the detail the review
        shows names `pkg-x` rather than "the selected packages".
        """
        context, _source, target = _two_independent_removals_context()

        plan = await AptSyncJob(context).plan()

        collateral = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:other-manual")
        assert collateral.detail == "Removing pkg-x on target-host would remove other-manual"
        # The cost, pinned: one batched rehearsal plus one per candidate, and only because
        # the batch found manual collateral.
        rehearsals = [cmd for cmd in all_calls(target) if cmd.startswith("apt-get --dry-run remove")]
        assert len(rehearsals) == 3

    @pytest.mark.asyncio
    async def test_joint_causation_names_the_whole_batch_rather_than_one_package(self) -> None:
        """Neither removal alone drops `other-manual`; only both together do. Declining then
        cancels both, so the sentence the user reads must not name just one of them.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\nother-manual\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""
                ),
                "remove --assume-yes pkg-x": CommandResult(0, "Remv pkg-x [1.0]\n", ""),
                "remove --assume-yes pkg-y": CommandResult(0, "Remv pkg-y [1.0]\n", ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        collateral = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:other-manual")
        assert collateral.detail == "Removing the packages listed earlier on target-host would remove other-manual"

    @pytest.mark.asyncio
    async def test_a_clean_batch_costs_no_extra_rehearsal(self) -> None:
        """No manual collateral, no narrowing: the two-simulation budget is unchanged for
        every run that has nothing to attribute.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\n", ""
                ),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert not any(diff.item_id.startswith("apt:collateral:") for diff in plan.diffs)
        rehearsals = [cmd for cmd in all_calls(target) if cmd.startswith("apt-get --dry-run remove")]
        assert len(rehearsals) == 1

    @pytest.mark.asyncio
    async def test_collateral_no_single_candidate_reproduces_is_blamed_on_the_whole_batch(self) -> None:
        """Joint causation — removing either candidate alone leaves `other-manual` in place,
        removing both takes it — is attributed to the whole batch, which is the honest
        answer and the only conservative one.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\nother-manual\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""
                ),
                "remove --assume-yes pkg-x": CommandResult(0, "Remv pkg-x [1.0]\n", ""),
                "remove --assume-yes pkg-y": CommandResult(0, "Remv pkg-y [1.0]\n", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.APPLY,
                "apt:collateral:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in all_calls(target))


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
            source_responses={"sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required")},
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
                "sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required"),
                "fuser /var/lib/dpkg/lock-frontend": CommandResult(1, "", ""),
            },
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        target_sudo_errors = [e for e in errors if e.host is Host.TARGET and "sudo" in e.message]
        assert len(target_sudo_errors) == 1
        assert all(command in target_sudo_errors[0].message for command in TARGET_SUDO_COMMANDS)


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
    """AptSyncJob.plan() extended with the `/etc/apt` directions that still have a review
    line (D-11/D-13, ADR-020 D-37): repository and pin REMOVALS, apt config in all three.
    """

    @pytest.mark.asyncio
    async def test_deb822_and_legacy_source_each_record_own_format(self) -> None:
        """The format is still recorded, on the one direction that still shows a file to
        the user: a legacy `.list` and a deb822 `.sources` offered for deletion read as
        two distinguishable entries rather than two bare filenames.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("d1", "foo.sources") + sha256_line("d2", "bar.list"), ""
                ),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "cat /etc/apt/sources.list.d/bar.list": CommandResult(0, _LEGACY_BAR, ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        foo_diff = by_id["apt:source:foo.sources"]
        bar_diff = by_id["apt:source:bar.list"]
        assert "deb822" in foo_diff.label
        assert "list" in bar_diff.label
        assert (foo_diff.item_class, foo_diff.action) == (ItemClass.APT_SOURCE, DiffAction.REMOVE)
        assert (bar_diff.item_class, bar_diff.action) == (ItemClass.APT_SOURCE, DiffAction.REMOVE)

    @pytest.mark.asyncio
    async def test_content_hydration_reads_use_sudo_matching_the_digest_capture(self) -> None:
        """WR-04 regression: content reads for diff hydration must use the same
        `sudo`-qualified privilege as the digest capture (`sudo find ... sha256sum`),
        not a plain unprivileged `cat` — otherwise a source file locked down to
        `0600`-or-similar digests correctly (root) but reads back empty (unprivileged),
        and the entry the user is asked to delete claims the wrong format.
        """
        context, _source, target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
            },
        )
        job = AptSyncJob(context)

        await job.plan()

        commands = all_calls(target)
        assert any(cmd == "sudo cat /etc/apt/sources.list.d/foo.sources" for cmd in commands)
        assert not any(cmd == "cat /etc/apt/sources.list.d/foo.sources" for cmd in commands)

    @pytest.mark.asyncio
    async def test_a_repository_never_appears_as_a_review_entry_in_the_add_or_change_direction(self) -> None:
        """Ruling 4's property, in both directions at once and across both file classes:
        `new.sources` is missing on the target, `changed.sources` differs, `new-pin` and
        `changed-pin` likewise. Under the old model that is four review entries; under
        derivation the user is asked about none of them.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("s1", "new.sources") + sha256_line("s2-new", "changed.sources"), ""
                ),
                "find /etc/apt/preferences.d": CommandResult(
                    0, sha256_line("p1", "new-pin") + sha256_line("p2-new", "changed-pin"), ""
                ),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("s2-old", "changed.sources"), ""),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p2-old", "changed-pin"), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert plan.diffs == ()
        assert plan.groups == ()

    @pytest.mark.asyncio
    async def test_pin_and_config_diff_missing_extra_and_changed(self) -> None:
        """The split ruling 11 makes: a pin keeps only the removal direction, apt config
        keeps all three, and the two live side by side in one plan.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("c1", "99update") + sha256_line("c2-new", "80retain"), ""
                ),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(
                    0, sha256_line("p2", "curl-pin") + sha256_line("p3", "extra-pin"), ""
                ),
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("c2-old", "80retain") + sha256_line("c3", "99extra"), ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        assert "apt:pin:curl-pin" not in by_id, "a differing pin is overwritten, never reviewed"
        assert by_id["apt:pin:extra-pin"].diff_class == DiffClass.EXTRA_ON_TARGET
        assert by_id["apt:pin:extra-pin"].action == DiffAction.REMOVE
        assert by_id["apt:config:99update"].action == DiffAction.INSTALL
        assert by_id["apt:config:80retain"].action == DiffAction.CHANGE
        assert by_id["apt:config:99extra"].action == DiffAction.REMOVE


# -- Capture seams: what apt actually reads, on BOTH machines (ADR-020 D-11) -----------

# The extension predicate the `sources.list.d` capture carries. Keyed FIRST in a response
# mapping so the fake can answer the filtered command differently from the unfiltered one:
# `respond_to` matches by substring, first match wins, so a capture that lost its filter
# falls through to the wider listing and the `.save` file reappears.
_FILTERED_SOURCES_FIND = "-name '*.list' -o -name '*.sources'"
_SOURCES_LIST_DIGEST_CMD = "sudo sha256sum /etc/apt/sources.list"


class TestWhatAptItselfReads:
    """The capture is scoped to the files apt reads, on both machines (ADR-020 D-11)."""

    @pytest.mark.asyncio
    async def test_a_save_file_in_sources_list_d_is_never_captured(self) -> None:
        """Ubuntu's own tooling leaves `.save`/`.curtin.orig` copies beside the real files.
        apt reads neither, so neither may reach the review — the target-only copy below
        would otherwise be offered for deletion as a repository the source lacks.
        """
        unfiltered = sha256_line("d1", "vendor.list") + sha256_line("d2", "vendor.list.save")
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                _FILTERED_SOURCES_FIND: CommandResult(0, sha256_line("d1", "vendor.list"), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, unfiltered, ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        item_ids = {d.item_id for d in plan.diffs}
        assert "apt:source:vendor.list.save" not in item_ids
        assert "apt:source:vendor.list" in item_ids

    @pytest.mark.asyncio
    async def test_preferences_d_and_apt_conf_d_keep_no_extension_filter(self) -> None:
        """apt reads extensionless files in both (six of them in `preferences.d` on the
        development machine), so the narrowing that is right for `sources.list.d` is wrong
        here — on either machine.
        """
        context, source, target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "no-esm-docker"), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        unfiltered = [
            cmd
            for machine in (source, target)
            for cmd in all_calls(machine)
            if "-exec sha256sum" in cmd and ("/etc/apt/preferences.d" in cmd or "/etc/apt/apt.conf.d" in cmd)
        ]
        # Both directories, both machines.
        assert len(unfiltered) == 4
        assert not any("-name" in cmd for cmd in unfiltered)
        assert "apt:pin:no-esm-docker" in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_sources_list_is_digested_on_both_machines_and_is_still_not_an_item(self) -> None:
        """`/etc/apt/sources.list` is a file, not a directory, so it appears in no `find`
        listing and needs its own digest — which ADR-020 D-38's write-when-different rule
        compares. Capturing it must not turn it into a reviewable item.
        """
        context, source, target = make_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCES_LIST_DIGEST_CMD: CommandResult(0, sha256_line("s1", "/etc/apt/sources.list"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCES_LIST_DIGEST_CMD: CommandResult(0, sha256_line("s2", "/etc/apt/sources.list"), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert job._work.source_facts.sources_list_digest == "s1"  # pyright: ignore[reportPrivateUsage]
        assert job._work.target_facts.sources_list_digest == "s2"  # pyright: ignore[reportPrivateUsage]
        assert sum(1 for cmd in all_calls(source) if _SOURCES_LIST_DIGEST_CMD in cmd) == 1
        assert sum(1 for cmd in all_calls(target) if _SOURCES_LIST_DIGEST_CMD in cmd) == 1
        assert not any(d.item_id.endswith(":sources.list") for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_an_absent_sources_list_yields_no_digest_rather_than_an_error(self) -> None:
        """Verified on the development machine: `sha256sum` on a missing path exits 1 and
        prints nothing to stdout, so absence falls out of the parse with no probe.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCES_LIST_DIGEST_CMD: CommandResult(1, "", "sha256sum: /etc/apt/sources.list: No such file\n"),
            },
            target_responses=_NO_PACKAGES,
        )
        job = AptSyncJob(context)

        await job.plan()

        assert job._work.source_facts.sources_list_digest is None  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_ubuntu_sources_is_never_offered_for_removal(self) -> None:
        """D-38: the distribution's own files are written and updated but never removed.
        A target holding `ubuntu.sources` and `ubuntu-esm-apps.sources` that the source does
        not have would otherwise be offered a deletion of its own archive, while a
        `.sources` file with a lookalike name is an ordinary repository and still is.
        """
        target_listing = (
            sha256_line("d1", "ubuntu.sources")
            + sha256_line("d2", "ubuntu-esm-apps.sources")
            + sha256_line("d3", "ubuntu-esm-mine.sources")
        )
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, target_listing, ""),
                "cat /etc/apt/sources.list.d/": CommandResult(0, "Types: deb\nURIs: http://x.example.com\n", ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert {d.item_id for d in plan.diffs} == {"apt:source:ubuntu-esm-mine.sources"}

    @pytest.mark.asyncio
    async def test_the_distribution_files_are_written_when_they_differ(self) -> None:
        """The other half of D-38's always-sync bucket, `/etc/apt/sources.list` included —
        it is a file rather than a directory entry and so travels on its own digest. An
        ordinary vendor repository that feeds no approved package stays put, which is
        ruling 4 working as intended.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("d1", "ubuntu.sources") + sha256_line("d9", "vendor.list"), ""
                ),
                _SOURCES_LIST_DIGEST_CMD: CommandResult(0, sha256_line("s1", "/etc/apt/sources.list"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCES_LIST_DIGEST_CMD: CommandResult(0, sha256_line("s2", "/etc/apt/sources.list"), ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        await job.execute()

        promoted = [c.rsplit(" ", 1)[1] for c in all_calls(target) if c.startswith("sudo install --owner=root")]
        assert promoted == ["/etc/apt/sources.list.d/ubuntu.sources", "/etc/apt/sources.list"]

    @pytest.mark.asyncio
    async def test_a_dry_run_previews_the_derived_writes_and_issues_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A derived write has no review entry, so without a preview line ADR-014's
        rehearsal would report an `apt-get update` and no reason for it.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "mozilla"), ""),
            },
            target_responses={**_NO_PACKAGES},
            dry_run=True,
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        with caplog.at_level(1):
            await job.execute()

        assert "[dry-run] Would write /etc/apt/preferences.d/mozilla from the source" in caplog.text
        assert not any(c.startswith("sudo install") for c in all_calls(target))
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_source_file_scan_runs_against_both_machines(self) -> None:
        """The scan is machine-agnostic and both answers are load-bearing: the target's
        drives keyring reference counting and the removal impact, the source's is what maps
        a package's origin URIs back to the repository file that would have to travel
        (ADR-020 D-34).
        """
        context, source, target = make_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
            },
            target_responses=_NO_PACKAGES,
        )
        job = AptSyncJob(context)

        await job.plan()

        assert sum(1 for cmd in all_calls(source) if _SOURCE_SCAN_CMD in cmd) == 1
        assert sum(1 for cmd in all_calls(target) if _SOURCE_SCAN_CMD in cmd) == 1
        refs, uris = job._work.source_facts.refs.by_filename["vendor.list"]  # pyright: ignore[reportPrivateUsage]
        assert uris == ("https://vendor.example.com/apt",)
        assert refs == ("/etc/apt/keyrings/vendor.gpg",)


class TestOriginCapture:
    """ADR-020 D-34's origin facts: where the source installed each package from, which
    repository file on the source declares that place, and which places are the
    distribution's own.
    """

    def test_source_files_serving_is_the_union_of_every_file_declaring_an_origin(self) -> None:
        """A package's installed version can list several origins and each may be declared
        by a different file — every one of them served it, so none may be dropped.
        """
        refs = {
            "vendor.list": ((), ("https://vendor.example.com/apt",)),
            "mirror.sources": ((), ("https://mirror.example.com/apt",)),
            "unrelated.list": ((), ("https://elsewhere.example.com/apt",)),
        }

        serving = SourceFileRefs(by_filename=refs).files_serving(
            frozenset({"https://vendor.example.com/apt", "https://mirror.example.com/apt"})
        )

        assert serving == frozenset({"vendor.list", "mirror.sources"})

    def test_an_origin_no_file_declares_serves_from_nowhere(self) -> None:
        """The class-4 input: a repository deleted from the source while its packages stay
        installed leaves an origin with no file behind it.
        """
        refs = {"vendor.list": ((), ("https://vendor.example.com/apt",))}

        serving = SourceFileRefs(by_filename=refs).files_serving(frozenset({"https://gone.example.com/apt"}))

        assert serving == frozenset()

    def test_distribution_origins_come_from_the_machines_own_distribution_files(self) -> None:
        """Per machine, from that machine's `ubuntu.sources`/`sources.list`/ESM files — not
        from a list of known Ubuntu hostnames, which is what would make two machines on
        different mirrors disagree about every package.
        """
        refs = {
            "ubuntu.sources": ((), ("http://ftp.belnet.be/ubuntu", "http://security.ubuntu.com/ubuntu")),
            "ubuntu-esm-apps.sources": ((), ("https://esm.ubuntu.com/apps/ubuntu",)),
            "sources.list": ((), ("http://old.example.com/ubuntu",)),
            "vendor.list": ((), ("https://vendor.example.com/apt",)),
        }

        assert SourceFileRefs(by_filename=refs).distribution_origins() == frozenset(
            {
                "http://ftp.belnet.be/ubuntu",
                "http://security.ubuntu.com/ubuntu",
                "https://esm.ubuntu.com/apps/ubuntu",
                "http://old.example.com/ubuntu",
            }
        )

    def test_a_user_named_esm_lookalike_is_not_a_distribution_file(self) -> None:
        """Exact filenames, not a `ubuntu-esm-*` glob: a file the user named that way is
        theirs, and treating its URIs as the distribution's would suppress the origin from
        every review line it feeds.
        """
        refs = {"ubuntu-esm-mine.sources": ((), ("https://mine.example.com/apt",))}

        assert SourceFileRefs(by_filename=refs).distribution_origins() == frozenset()

    @pytest.mark.asyncio
    async def test_the_source_policy_call_answers_both_questions_asked_of_it(self) -> None:
        """One batched `apt-cache policy` on the source, parsed twice: the bare-`.deb`
        exclusion and the installed-origin map. A second call would re-run a full policy
        query to learn something already on screen.
        """
        context, source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\ncode\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\ncode\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0, _policy_block("pkg-a", "https://vendor.example.com/apt") + _policy_block("code", None), ""
                ),
            },
            target_responses=_NO_PACKAGES,
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert sum(1 for cmd in all_calls(source) if "apt-cache policy" in cmd) == 1
        assert job._work.origins.plans["apt:package:pkg-a"].source_origins == frozenset(
            {"https://vendor.example.com/apt"}
        )  # pyright: ignore[reportPrivateUsage]
        # `code` came from no repository, so it is dropped at capture and never diffed.
        assert [diff.item_id for diff in plan.diffs] == ["apt:package:pkg-a"]

    @pytest.mark.asyncio
    async def test_the_source_origin_map_holds_the_installed_row_not_the_candidate_one(self) -> None:
        """The distinction the whole classification rests on: what the source HAS, not what
        the source would install next.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, POLICY_INSTALLED_AND_CANDIDATE_DIFFER, ""),
            },
            target_responses=_NO_PACKAGES,
        )
        job = AptSyncJob(context)

        await job.plan()

        assert job._work.origins.plans["apt:package:gh"].source_origins == frozenset(
            {"https://cli.github.com/packages"}
        )  # pyright: ignore[reportPrivateUsage]


_MOZILLA_SOURCES = (
    "Types: deb\nURIs: https://packages.mozilla.org/apt\nSuites: mozilla\n"
    "Components: main\nSigned-By: /etc/apt/keyrings/packages.mozilla.org.asc\n"
)
_UBUNTU_SOURCES_BELNET = "Types: deb\nURIs: http://ftp.belnet.be/ubuntu\nSuites: noble\nComponents: main\n"
_UBUNTU_SOURCES_ARCHIVE = "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: noble\nComponents: main\n"
_RIVAL_LIST = "deb https://rival.example.com/apt stable main\n"


class TestOriginClassification:
    """ADR-020 D-34 at plan time: a package replicates as (name, origin), so a name the
    target could satisfy from a different vendor is not "already available".
    """

    @pytest.mark.asyncio
    async def test_same_origin_install_derives_no_repository_write(self) -> None:
        """Class 1. The target's own candidate already comes from a place the source uses,
        so nothing about `/etc/apt` has to change for the install to be faithful.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL
        assert job._work.origins.plans["apt:package:pkg-a"].derived_files == frozenset()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_different_origin_install_derives_the_sources_own_repository(self) -> None:
        """Class 2, the Firefox case. The target HAS a candidate for the name — Ubuntu's
        epoch-1 transitional package — and it is not the source's software. Name-only
        matching read this as an ordinary install and shipped the other vendor's package.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "firefox\n", ""),
                "dpkg-query": CommandResult(0, "firefox\t145.0\n", ""),
                "apt-cache policy": CommandResult(0, POLICY_MOZILLA_FIREFOX_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("mozilla.sources", _MOZILLA_SOURCES), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "packages.mozilla.org.asc"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, POLICY_ARCHIVE_CANDIDATE_UNINSTALLED, ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:firefox")
        assert (diff.diff_class, diff.action) == (DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)
        assert diff.detail == "from packages.mozilla.org/apt"
        # The keyring half of the write set is derived at write time from this file's own
        # `Signed-By:`; what the plan owes is the file.
        assert job._work.origins.plans["apt:package:firefox"].derived_files == frozenset({"mozilla.sources"})  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_unreplicable_origin_is_report_only_naming_the_origin(self) -> None:
        """Class 4. The repository the package came from is gone from the source's own
        `/etc/apt`, so there is no file to hand the target and no honest install to offer.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://gone.example.com/apt"), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)
        job.context = dataclasses.replace(job.context, reviewer=FakeReviewer({"apt:package:pkg-a": Decision.APPLY}))

        await job.execute()

        diff = next(d for d in job._accepted_plan.diffs if d.item_id == "apt:package:pkg-a")  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
        assert (diff.diff_class, diff.action) == (DiffClass.REPO_UNAVAILABLE, DiffAction.REPORT_ONLY)
        assert diff.detail is not None and "gone.example.com/apt" in diff.detail
        assert not any("apt-get install" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_dangling_keyring_makes_the_package_unavailable(self) -> None:
        """Class 4's other half. The source declares the repository but references a key it
        does not have, so the file cannot be written and the origin cannot be delivered —
        and it is the PACKAGE that says so, because the package is what the user decided.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert (diff.diff_class, diff.action) == (DiffClass.REPO_UNAVAILABLE, DiffAction.REPORT_ONLY)
        assert diff.detail is not None and "vendor.gpg" in diff.detail

    @pytest.mark.asyncio
    async def test_one_writable_serving_file_is_enough(self) -> None:
        """A package served by both a sound repository file and a broken one is replicable:
        the origin only has to be declared once for the target to install from it, so a
        second file with a dangling key must not condemn the package.
        """
        broken = "deb [signed-by=/etc/apt/keyrings/missing.gpg] https://vendor.example.com/apt old main\n"
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(
                    0, _scan_line("broken.list", broken) + _scan_line("vendor.list", _VENDOR_LIST), ""
                ),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL

    @pytest.mark.asyncio
    async def test_a_distribution_origin_install_names_no_origin(self) -> None:
        """The unremarkable case earns no text: naming the mirror on every archive package
        would bury the two lines that matter under a hundred that do not.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://ftp.belnet.be/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_BELNET), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL
        assert diff.detail is None

    @pytest.mark.asyncio
    async def test_two_machines_on_different_ubuntu_mirrors_produce_no_origin_mismatch(self) -> None:
        """The suppression that makes the provenance comparison usable at all: each machine's
        distribution origins are read from its OWN distribution files, so a Belgian mirror
        and the default archive are one vendor, not two.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://ftp.belnet.be/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_BELNET), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://archive.ubuntu.com/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_ARCHIVE), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert [d for d in plan.diffs if d.diff_class == DiffClass.ORIGIN_MISMATCH] == []

    @pytest.mark.asyncio
    async def test_divergent_vendor_provenance_reports_origin_mismatch(self) -> None:
        """The same name and the same version on both machines, from two vendors. A
        presence-and-version diff sees nothing here, which is why this class exists —
        report only, because converging it means a cross-vendor reinstall nobody asked for.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://rival.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("rival.list", _RIVAL_LIST), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert (diff.diff_class, diff.action) == (DiffClass.ORIGIN_MISMATCH, DiffAction.REPORT_ONLY)
        assert diff.detail is not None
        assert "vendor.example.com/apt" in diff.detail
        assert "rival.example.com/apt" in diff.detail


class TestOriginDetailWording:
    """Ruling 9's naming rules, as pure text."""

    def test_origin_detail_strips_the_scheme_and_names_the_full_path(self) -> None:
        """The path, not the bare host: one Launchpad host serves thousands of PPAs."""
        assert build_origin_detail(["https://ppa.launchpadcontent.net/git-core/ppa/ubuntu"]) == (
            "from ppa.launchpadcontent.net/git-core/ppa/ubuntu"
        )

    def test_origin_detail_is_omitted_for_a_distribution_origin(self) -> None:
        """The caller filters the distribution's own origins out, so nothing left to name
        means the distribution serves it and the line says nothing about origins.
        """
        assert build_origin_detail([]) is None

    def test_several_vendors_are_named_comma_separated(self) -> None:
        assert build_origin_detail(["https://a.example.com/apt", "https://b.example.com/deb"]) == (
            "from a.example.com/apt, b.example.com/deb"
        )

    def test_the_mismatch_detail_names_both_sides(self) -> None:
        detail = build_origin_mismatch_detail(
            ["https://vendor.example.com/apt"], ["https://rival.example.com/apt"], MACHINES
        )

        assert detail == (
            "source-host installed it from vendor.example.com/apt, target-host from rival.example.com/apt"
        )


class TestOriginOutcome:
    """`OriginPlan.outcome` in isolation, for the branches a whole-plan test cannot reach
    cheaply."""

    def test_apt_silence_on_the_target_does_not_condemn_a_package(self) -> None:
        """`df48cd07`'s rule at the classification level: a policy call that produced no
        block for the name answered nothing, and a run whose probe failed must not report a
        repository problem it never established.
        """
        plan = OriginPlan(target_candidate_known=False)

        assert plan.outcome() is not OriginOutcome.UNREPLICABLE

    def test_an_explicit_no_candidate_with_no_origin_to_replicate_is_unreplicable(self) -> None:
        """The other half of the same distinction: apt answered, and its answer was no."""
        plan = OriginPlan(target_candidate_known=True)

        assert plan.outcome() is OriginOutcome.UNREPLICABLE

    def test_a_plan_with_no_origin_fact_at_all_still_installs(self) -> None:
        """The degenerate case: nothing captured, nothing to hold the install to."""
        assert OriginPlan().outcome() is OriginOutcome.SAME_ORIGIN


def respond_with_policy_sequence(
    mapping: dict[str, CommandResult], policy_results: list[CommandResult]
) -> Callable[..., CommandResult]:
    """Like `respond_to`, but successive `apt-cache policy` calls return successive results
    (the last one repeats).

    The shape the target genuinely has across one run: the plan-time policy read and the
    post-`apt-get update` verification ask the same question of two different `/etc/apt`
    states, and a fixture that answers both identically cannot distinguish a verification
    that re-read the target from one that reused the plan's answer.
    """
    fallback = CommandResult(exit_code=0, stdout="", stderr="")
    state = {"policy_calls": 0}

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if "apt-cache policy" in cmd:
            index = min(state["policy_calls"], len(policy_results) - 1)
            state["policy_calls"] += 1
            return policy_results[index]
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


def _real_installs(target: MagicMock) -> list[str]:
    """Every REAL `apt-get install` the target was asked to run — the `--dry-run`
    simulations share the verb and are deliberately excluded."""
    return [cmd for cmd in all_calls(target) if "sudo" in cmd and "apt-get install" in cmd]


def _policy_calls_after_the_update(target: MagicMock) -> list[str]:
    commands = all_calls(target)
    update = _index_of(commands, lambda cmd: "sudo apt-get update" in cmd)
    return [cmd for cmd in commands[update:] if "apt-cache policy" in cmd]


def _mozilla_source_responses() -> dict[str, CommandResult]:
    """A source machine running Mozilla's own `firefox`, with the repository file that
    declares it and the key that file names."""
    return {
        "apt-mark showmanual": CommandResult(0, "firefox\n", ""),
        "dpkg-query": CommandResult(0, "firefox\t145.0\n", ""),
        "apt-cache policy": CommandResult(0, POLICY_MOZILLA_FIREFOX_INSTALLED, ""),
        _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("mozilla.sources", _MOZILLA_SOURCES), ""),
        "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "packages.mozilla.org.asc"), ""),
    }


class TestOriginEnforcement:
    """ADR-020 D-35 at converge time: whatever plan-time classification concluded and
    whatever `/etc/apt` work this run derived, the target may not install a package from a
    vendor the source does not use. Checked against the real post-`apt-get update` state.
    """

    @pytest.mark.asyncio
    async def test_install_is_refused_when_the_post_update_candidate_is_from_the_wrong_origin(self) -> None:
        """The Firefox defect at its last possible catch point: the source runs Mozilla's
        build, the repository did not land (or did not win), and Ubuntu's epoch-1
        transitional package is still what apt would install. It fails as its own item.
        """
        context, _source, target = make_context(
            source_responses=_mozilla_source_responses(),
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, POLICY_ARCHIVE_CANDIDATE_UNINSTALLED, ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:firefox": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as excinfo:
            await job.execute()

        reasons = [reason for _diff, reason in excinfo.value.failures]
        assert len(reasons) == 1
        assert "packages.mozilla.org/apt" in reasons[0]
        assert "ftp.belnet.be/ubuntu" in reasons[0]
        assert not any("firefox" in cmd for cmd in _real_installs(target))

    @pytest.mark.asyncio
    async def test_an_origin_the_converged_target_now_offers_lets_the_install_through(self) -> None:
        """The same run once the repository and its pin have landed: the verification re-reads
        the target and finds Mozilla's copy, so the install proceeds. This is why the check
        re-reads instead of reusing the plan's answer, which still said Ubuntu's archive.
        """
        context, _source, target = make_context(source_responses=_mozilla_source_responses())
        target.run_command = AsyncMock(
            side_effect=respond_with_policy_sequence(
                {"apt-mark showmanual": CommandResult(0, "", "")},
                [
                    CommandResult(0, POLICY_ARCHIVE_CANDIDATE_UNINSTALLED, ""),
                    CommandResult(0, POLICY_MOZILLA_FIREFOX_INSTALLED, ""),
                ],
            )
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:firefox": Decision.APPLY})

        await job.execute()

        assert [cmd for cmd in _real_installs(target) if "firefox" in cmd]

    @pytest.mark.asyncio
    async def test_the_origin_verification_costs_one_batched_policy_call(self) -> None:
        """Three approved vendor installs, one policy read — never one per package. The
        answer cannot change between two installs of the same run.
        """
        names = ("pkg-a", "pkg-b", "pkg-c")
        vendor_policy = "".join(_policy_block(name, "https://vendor.example.com/apt") for name in names)
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
                "apt-cache policy": CommandResult(0, vendor_policy, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, vendor_policy, ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {f"apt:package:{name}": Decision.APPLY for name in names})

        await job.execute()

        assert len(_real_installs(target)) == 3
        assert len(_policy_calls_after_the_update(target)) == 1

    @pytest.mark.asyncio
    async def test_a_distribution_origin_package_is_not_origin_verified(self) -> None:
        """D-35's exemption. The source has this package from its own Ubuntu mirror, so
        whatever mirror the target answers with is the same vendor — and asking the question
        at all would refuse every package on a pair of machines with different mirrors.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://ftp.belnet.be/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_BELNET), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://archive.ubuntu.com/ubuntu"), ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        assert [cmd for cmd in _real_installs(target) if "pkg-a" in cmd]
        assert _policy_calls_after_the_update(target) == []

    @pytest.mark.asyncio
    async def test_a_name_the_answered_verification_skipped_refuses_only_that_install(self) -> None:
        """Stricter than the plan-time rule on purpose: there, apt's silence leaves the
        install to report its own failure; here the install is the thing being guarded, and a
        guarantee that could not be evaluated has not been met. apt DID answer — it printed a
        block for `pkg-b` — so the silence about `pkg-a` is evidence about `pkg-a` alone, and
        `pkg-b` still installs.
        """
        vendor = "https://vendor.example.com/apt"
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0, _policy_block("pkg-a", vendor) + _policy_block("pkg-b", vendor), ""
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-b", vendor), ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as excinfo:
            await job.execute()

        assert [diff.item_id for diff, _reason in excinfo.value.failures] == ["apt:package:pkg-a"]
        assert "no repository at all" in excinfo.value.failures[0][1]
        assert not any("pkg-a" in cmd for cmd in _real_installs(target))
        assert [cmd for cmd in _real_installs(target) if "pkg-b" in cmd]

    @pytest.mark.asyncio
    async def test_a_verification_probe_that_did_not_answer_fails_once_not_per_package(self) -> None:
        """The environment broke, not the request. Three approved vendor installs and a
        policy read that exited non-zero: one failure naming the command, never three
        failures blaming three packages' provenance for an apt that never ran.
        """
        names = ("pkg-a", "pkg-b", "pkg-c")
        vendor_policy = "".join(_policy_block(name, "https://vendor.example.com/apt") for name in names)
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
                "apt-cache policy": CommandResult(0, vendor_policy, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                # A complete, ORIGIN-MATCHING answer alongside the failure, so nothing but
                # the exit code can refuse these three: a guard that ignored it would let
                # all three install off output apt never stood behind.
                "apt-cache policy": CommandResult(
                    100, vendor_policy, "E: Could not get lock /var/lib/dpkg/lock-frontend\n"
                ),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {f"apt:package:{name}": Decision.APPLY for name in names})

        with pytest.raises(ProbeFailed) as excinfo:
            await job.execute()

        assert "apt-cache policy pkg-a pkg-b pkg-c" in str(excinfo.value)
        assert "exited 100" in str(excinfo.value)
        assert "lock-frontend" in str(excinfo.value)
        assert _real_installs(target) == []

    @pytest.mark.asyncio
    async def test_a_verification_probe_that_printed_nothing_fails_once_not_per_package(self) -> None:
        """The ambiguous half, resolved toward failing fast: exit 0 and not one block over a
        set apt owes a block for. Indistinguishable from "apt knows none of these", and
        misattributing a broken probe to every package's provenance is the worse reading.
        """
        names = ("pkg-a", "pkg-b", "pkg-c")
        vendor_policy = "".join(_policy_block(name, "https://vendor.example.com/apt") for name in names)
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
                "apt-cache policy": CommandResult(0, vendor_policy, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {f"apt:package:{name}": Decision.APPLY for name in names})

        with pytest.raises(ProbeFailed) as excinfo:
            await job.execute()

        assert "printed no package block" in str(excinfo.value)
        assert _real_installs(target) == []

    @pytest.mark.asyncio
    async def test_a_skipped_install_is_never_named_in_the_verification(self) -> None:
        """The batch is the APPROVED set, not the planned one: a package the user left
        unticked cannot be refused, and must not widen the command either.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t2.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://vendor.example.com/apt")
                    + _policy_block("pkg-b", "https://vendor.example.com/apt"),
                    "",
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.SKIP_ONCE})

        await job.execute()

        verification = _policy_calls_after_the_update(target)
        assert len(verification) == 1
        assert "pkg-b" not in verification[0]


class TestOriginRefusalWording:
    """The refusal names both origins, because either half alone is unactionable."""

    def test_both_the_wanted_and_the_offered_origin_are_named(self) -> None:
        detail = build_origin_refusal_detail(
            "firefox", ["https://packages.mozilla.org/apt"], ["http://ftp.belnet.be/ubuntu"], MACHINES
        )

        assert detail == (
            "firefox was not installed: source-host has it from packages.mozilla.org/apt, but after this run's "
            "apt-get update target-host would install it from ftp.belnet.be/ubuntu (ADR-020 D-35)"
        )

    def test_a_target_with_no_candidate_origin_says_so_rather_than_naming_nothing(self) -> None:
        detail = build_origin_refusal_detail("pkg-a", ["https://vendor.example.com/apt"], [], MACHINES)

        assert "offers it from no repository at all" in detail
        assert "vendor.example.com/apt" in detail


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
    source.run_command = AsyncMock(side_effect=respond_to_source(source_responses or {}))
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


def _policy_candidate(origin: str) -> str:
    """`apt-cache policy pkg-a` on a target that does not have the package but can now
    fetch it from `origin` — the shape the post-`apt-get update` verification reads."""
    return (
        "pkg-a:\n  Installed: (none)\n  Candidate: 1.0\n  Version table:\n"
        f"     1.0 500\n        500 {origin} stable/main amd64 Packages\n"
    )


_POLICY_AVAILABLE = _policy_candidate("https://example.com")
_POLICY_NO_CANDIDATE = "pkg-a:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"


def _foo_source_responses(**overrides: CommandResult) -> dict[str, CommandResult]:
    """A source machine whose `pkg-a` comes from the repository `foo.sources` declares.

    The only shape that makes a repository travel now (ADR-020 D-37): a source file is
    derived from the packages approved from it, so a test that wants `foo.sources` written
    must give the source a package whose origin `foo.sources` serves. `foo.gpg` is the key
    that file names, present on the source, so the repository is writable.
    """
    return {
        "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
        "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
        "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://example.com"), ""),
        _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
        "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
        "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
        **overrides,
    }


def _foo_target_side_effect(
    overrides: dict[str, CommandResult] | None = None, *, origin: str = "https://example.com"
) -> Callable[..., CommandResult]:
    """A target that offers `pkg-a` from nowhere at plan time and from `origin` afterwards.

    Two different answers to one command, which is the run's real shape: the plan-time
    policy read is what derives the repository (no candidate -> the source's origin has to
    be replicated), and the post-`apt-get update` read is what D-35 verifies the install
    against. A fixture answering both the same way could not tell the two apart.
    """
    return respond_with_policy_sequence(
        {
            "echo $HOME": CommandResult(0, "/home/target-user", ""),
            "apt-mark showmanual": CommandResult(0, "", ""),
            "test -f": CommandResult(1, "", ""),
            **(overrides or {}),
        },
        [CommandResult(0, _POLICY_NO_CANDIDATE, ""), CommandResult(0, _policy_candidate(origin), "")],
    )


_APPROVE_PKG_A = {"apt:package:pkg-a": Decision.APPLY}


class TestRepoGroupOrdering:
    @pytest.mark.asyncio
    async def test_key_then_source_then_update_then_package_install(self) -> None:
        """N5 end to end, against the derived path: approving `pkg-a` is what makes
        `foo.sources` travel, and the four commands still land in apt's own dependency
        order.
        """
        context, _source, target = _repo_context(source_responses=_foo_source_responses())
        target.run_command = AsyncMock(
            side_effect=_foo_target_side_effect(
                {"apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\n", "")}
            )
        )
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

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
    async def test_pins_travel_without_a_review_line_and_land_before_the_sources(self) -> None:
        """D-36's ordering requirement: the pin is what makes the derived repository's
        origin outrank the archive's, so it has to be in place before the sources it
        governs and before the refresh that reads them — and it reaches the target with no
        review entry of its own.
        """
        context, _source, target = _repo_context(
            source_responses=_foo_source_responses(
                **{"find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "mozilla"), "")}
            )
        )
        target.run_command = AsyncMock(side_effect=_foo_target_side_effect())
        job = AptSyncJob(context)
        reviewer = _CountingReviewer(_APPROVE_PKG_A)
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        commands = all_calls(target)
        pin_idx = _index_of(commands, lambda c: "sudo install" in c and c.endswith("/etc/apt/preferences.d/mozilla"))
        source_idx = _index_of(commands, lambda c: "sudo install" in c and "sources.list.d/foo.sources" in c)
        update_idx = _index_of(commands, lambda c: c == "sudo apt-get update")
        assert pin_idx < source_idx < update_idx
        assert _actionable_entry_ids(reviewer.calls[0]) == {"apt:package:pkg-a"}

    @pytest.mark.asyncio
    async def test_a_package_apt_reports_no_candidate_for_is_withheld_from_the_first_pass(self) -> None:
        """The other half of what N5's ordering test cannot show: an available package is
        offered, one apt reports `Candidate: (none)` for is not — it is `REPORT_ONLY`, and
        the first review never even shows it as installable. The source's origin is one no
        file on the source declares, so nothing this run could add would supply it either.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://gone.example.com/apt"), ""),
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
        derived), and not one command reaches for a vendor to get it.
        """
        context, _source, target = _repo_context(source_responses=_foo_source_responses())
        target.run_command = AsyncMock(side_effect=_foo_target_side_effect())
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        commands = all_calls(target)
        assert any("sudo install" in cmd and "keyrings/foo.gpg" in cmd for cmd in commands)
        for cmd in commands:
            assert "http://" not in cmd
            assert "https://" not in cmd

    @pytest.mark.asyncio
    async def test_a_failed_derived_repository_write_fails_the_package_that_needed_it(self) -> None:
        """D-39's attribution. A keyring that could not be promoted is not a failed item —
        there is no key item, and there is no repository item either — so the repository is
        not written (a repo apt cannot verify is worse than no repo) and the failure lands
        on the PACKAGE, which is the thing the user decided about. The message names the
        file, and the install command is never issued.
        """
        context, _source, target = _repo_context(source_responses=_foo_source_responses())
        target.run_command = AsyncMock(
            side_effect=_foo_target_side_effect(
                {"sudo install --owner=root --group=root --mode=0644": CommandResult(1, "", "disk full")}
            )
        )
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a"}
        assert "/etc/apt/sources.list.d/foo.sources" in failures["apt:package:pkg-a"]
        assert "foo.gpg" in failures["apt:package:pkg-a"]
        commands = all_calls(target)
        assert not any("sudo install" in c and "sources.list.d/foo.sources" in c for c in commands)
        assert not _real_installs(target)

    @pytest.mark.asyncio
    async def test_a_repository_whose_own_promotion_fails_also_fails_its_package(self) -> None:
        """The other way a derived write can fail: the key lands, the repository's own
        `sudo install` does not. The refusal must still reach the package (D-39) — there is
        no repository item left for it to land on.
        """
        context, _source, target = _repo_context(source_responses=_foo_source_responses())
        target.run_command = AsyncMock(
            side_effect=_foo_target_side_effect(
                {
                    "sudo install --owner=root --group=root --mode=0644 "
                    "/home/target-user/.cache/pc-switcher/apt-staging/etc_apt_sources.list.d_foo.sources": (
                        CommandResult(1, "", "Read-only file system")
                    )
                }
            )
        )
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a"}
        assert "/etc/apt/sources.list.d/foo.sources" in failures["apt:package:pkg-a"]
        assert "Read-only file system" in failures["apt:package:pkg-a"]
        assert _key_writes(target) == ["/etc/apt/keyrings/foo.gpg"]
        assert not _real_installs(target)

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
        etc_removals = [c for c in commands if "sudo rm --force" in c]
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
        assert "--owner=root --group=root --mode=0644" in promotions[0]
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
                    "sudo install --owner=root --group=root --mode=0644": promote_result,
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
            staged_cleanup = [c for c in commands if c.startswith("rm --force") and "apt-staging" in c]
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
        `sudo rm --force`, and the run's single `apt-get update` runs after both writes — apt's
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
        removals = [c for c in commands if c.startswith("sudo rm --force")]
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
        install --owner=root --group=root --mode=0644`; never re-fetched, never parsed, never written
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
            c
            for c in all_calls(target)
            if c.startswith("sudo install --owner=root --group=root --mode=0644") and "foo.gpg" in c
        ]
        assert len(promotions) == 1
        assert (
            promotions[0]
            == f"sudo install --owner=root --group=root --mode=0644 {staged_dest} /etc/apt/keyrings/foo.gpg"
        )


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
        _install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:config:99conf" in failed_ids
        # The reviewed half fails as an item; the derived pin has no item to fail, so the
        # rollback records it against its destination instead (D-39) — without which a
        # package depending on it would install against the pre-run `/etc/apt`.
        assert "/etc/apt/preferences.d/curl-pin" in job._work.derived.failed  # pyright: ignore[reportPrivateUsage]

        commands = all_calls(target)
        # Restore: the pre-existing pin file is put back from its backup.
        assert any("sudo install" in c and "backup-" in c and "preferences.d/curl-pin" in c for c in commands)
        # Delete: the brand-new config file this run created is removed.
        assert any("sudo rm --force" in c and "apt.conf.d/99conf" in c for c in commands)
        # A clean rollback discards the backup.
        assert any(c.startswith("rm --recursive --force") and "backup-" in c for c in commands)
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
                    "sudo install --owner=root --group=root --mode=0644 /home/target-user/.cache": CommandResult(
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
        assert not any(c.startswith("rm --recursive --force") and "backup-" in c for c in all_calls(target))

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
                    "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                        0, "Inst pkg-a (1.0)\n", ""
                    ),
                    "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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
                    "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                        0, "Inst pkg-a (1.0)\n", ""
                    ),
                    "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("c1-new", "conf-a") + sha256_line("c2-new", "conf-b"), ""
                ),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1-new", "pin-a"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("c1-old", "conf-a") + sha256_line("c2-old", "conf-b"), ""
                ),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1-old", "pin-a"), ""),
                "test -f /etc/apt/apt.conf.d/conf-": CommandResult(0, "", ""),
                "test -f /etc/apt/preferences.d/pin-a": CommandResult(0, "", ""),
                "sudo cp --archive": CommandResult(1, "", "disk full"),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:config:conf-a": Decision.APPLY, "apt:config:conf-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        # Both group items (plus the auto-injected metadata-refresh marker) are
        # reported as failures — not just the one whose backup was actually
        # attempted before the loop aborted — and no KeyError escapes.
        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert {"apt:config:conf-a", "apt:config:conf-b"} <= failed_ids
        assert "/etc/apt/preferences.d/pin-a" in job._work.derived.failed  # pyright: ignore[reportPrivateUsage]

        commands = all_calls(target)
        # Nothing was written at all: the group aborts before any write once backing up
        # fails, derived files included.
        assert not any(
            "sudo install --owner=root --group=root --mode=0644" in c and "/etc/apt/" in c for c in commands
        )


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
        `/etc/apt/keyrings` directory at all, derived by the `pkg-a` that repository serves.
        """
        context, source, target = _repo_context(source_responses=_foo_source_responses())
        target.run_command = AsyncMock(side_effect=_foo_target_side_effect(extra))
        return context, source, target

    @pytest.mark.asyncio
    async def test_promotion_ensures_keyrings_directory_before_install(self) -> None:
        context, _source, target = self._fresh_target()
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        commands = all_calls(target)
        mkdir_idx = _index_of(commands, lambda c: c == "sudo mkdir --parents --mode=0755 /etc/apt/keyrings")
        install_idx = _index_of(
            commands, lambda c: "sudo install --owner=root --group=root --mode=0644" in c and "keyrings/foo.gpg" in c
        )
        assert mkdir_idx < install_idx

    @pytest.mark.asyncio
    async def test_directory_preparation_failure_fails_the_item_not_the_run(self) -> None:
        """The failure surfaces on the PACKAGE, the thing the user reviewed: its key never
        landed, so the repository is not written either (D-12/D-39)."""
        context, _source, target = self._fresh_target(
            **{"sudo mkdir --parents --mode=0755 /etc/apt/keyrings": CommandResult(1, "", "permission denied")}
        )
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a"}
        assert "foo.gpg" in failures["apt:package:pkg-a"]
        commands = all_calls(target)
        assert not any(
            "sudo install --owner=root --group=root --mode=0644" in c and "keyrings/foo.gpg" in c for c in commands
        )


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
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\n", ""
                ),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-b": CommandResult(
                    0, "Inst pkg-b (2.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-b": (
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
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\n", ""
                ),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-b": CommandResult(
                    0, "Inst pkg-b (2.0)\n", ""
                ),
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
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
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


# -- ADR-020 D-40: collateral protects the TARGET's manual set alone -------------------

_SOURCE_DECISION_SKIP_SRC_ONLY = (
    "machine_specific:\n"
    '  "apt:package:src-only":\n'
    "    item_class: apt_package\n"
    "    label: src-only\n"
    "    recorded_at: '2026-01-01T00:00:00Z'\n"
)


class TestSourceOnlyCollateral:
    """ADR-020 D-40: a package manual on the SOURCE alone is NOT protected from collateral
    removal/downgrade. The loss is deliberate — if the target's apt installed the package
    automatically, the target's apt owns it, and reclaiming it as a user choice on the
    strength of the other machine's bookkeeping is a guess. These two tests are kept
    inverted rather than deleted, as the record that the case was given up on purpose.
    """

    @pytest.mark.asyncio
    async def test_source_only_manual_collateral_removal_is_not_a_review_item(self) -> None:
        """`src-only` is manual on the source but skip-recorded there, so it is filtered
        out of the source manifest, and it is absent from the target manual set. Installing
        `pkg-a` would remove it, and that now happens silently: the target's own apt is the
        only bookkeeping consulted.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nsrc-only\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nsrc-only\t1.0\n", ""),
                "apt.decisions.yaml": CommandResult(0, _SOURCE_DECISION_SKIP_SRC_ONLY, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv src-only [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id == "apt:collateral:src-only" for d in plan.diffs)
        # src-only was filtered from the source manifest, so it is not a review candidate
        # in its own right either — the removal reaches the user in no form at all.
        assert "apt:package:src-only" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_apply_time_guard_allows_source_only_manual_collateral(self) -> None:
        """The apply-time install guard reads the same narrowed set: a drifted real
        transaction that would remove a package manual on the SOURCE only proceeds.
        `src-only` is skip-recorded on the source so it is not a reviewed candidate.
        """
        sim_cmd = "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a"
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

        await job.execute()

        commands = all_calls(target)
        assert any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c for c in commands)


# -- C26/N7: a repo/key removal names the target-side machine-specific packages ---------

# Identifies the source-file reference scan (C26's removal impact, and the reference count
# keyring provisioning and collection run on) by the `-path` selectors that give it its
# unambiguous exit code. Not by `-exec awk`: `respond_to` matches by substring and the first
# match wins, so a key loose enough to also match a second awk command added later would
# silently reroute that command's fixture answer here. `/etc/apt/sources.list` is one of the
# two selected locations because a keyring named only there is still in use.
_SOURCE_SCAN_CMD = "-path /etc/apt/sources.list -o"

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


def _target_offers(*names: str, origin: str = _BASELINE_ARCHIVE) -> str:
    """`apt-cache policy` blocks for names the TARGET does not have installed but CAN
    install — a candidate, no `***` row.

    What a target must answer for a package before that package can enter the plan-time
    rehearsal (`_target_resolvable`). A fixture that omits it is describing a target whose
    apt has never heard the name, on which a real `apt-get --dry-run install` exits 100.
    """
    return "".join(
        f"{name}:\n  Installed: (none)\n  Candidate: 1.0\n  Version table:\n"
        f"     1.0 500\n        500 {origin} stable/main amd64 Packages\n"
        for name in names
    )


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


# The source-side scan that makes the shared `apt-cache policy` fixtures' origins resolve to
# a repository file (ADR-020 D-34). Without one, `gh`'s vendor origin is declared by no file
# on the source and the package is correctly unreplicable — which is a different fact from
# the one those tests are about.
_POLICY_FIXTURE_SCAN = (
    _scan_line("github-cli.list", "deb https://cli.github.com/packages stable main\n")
    + _scan_line(
        "ubuntu.sources",
        "Types: deb\nURIs: http://ftp.belnet.be/ubuntu http://security.ubuntu.com/ubuntu\n",
    )
    + _scan_line("ubuntu-esm-apps.sources", "Types: deb\nURIs: https://esm.ubuntu.com/apps/ubuntu\n")
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
        # Both halves, in the order the decision needs them: what the machine stops getting,
        # then what that costs. The user's ruling — the URL is what the choice is about.
        assert diff.detail == (
            "target-host would stop getting software from https://vendor.example.com/apt; "
            "target-host installs vendor-tool from vendor.list — packages you set to always skip, so they "
            "would stay installed but never get another update"
        )

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
        # The URL half is unconditional (it is what the deletion is about); only the
        # stranded-packages half is omitted when nothing would be stranded.
        assert diff.detail == "target-host would stop getting software from https://vendor.example.com/apt"

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

    A `sudo rm --force /etc/apt/sources.list.d/<f>` drops `<f>` from every later scan, which is
    what lets a test prove the keyring reference count is taken against the target's real
    post-write state rather than the state `plan()` saw. `sources_list` is the content of
    `/etc/apt/sources.list`, a file pc-switcher never syncs and never deletes.
    """
    live = dict(target_sources)

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if cmd.startswith("sudo rm --force "):
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
        if c.startswith("sudo install --owner=root --group=root --mode=0644")
        and c.rsplit(" ", 1)[1].startswith(_KEY_DEST_PREFIXES)
    ]


def _key_deletions(target: MagicMock) -> list[str]:
    return [c for c in all_calls(target) if c.startswith("sudo rm --force") and "/etc/apt/keyrings/" in c]


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
                "find /etc/apt/keyrings": CommandResult(
                    0, sha256_line("k1", "new.gpg") + sha256_line("k-new", "rot.gpg"), ""
                ),
                "find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g1", "legacy.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
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
        assert "apt:source:foo.sources" in entries, "the repository DELETION must still be reviewed"

    @pytest.mark.asyncio
    async def test_key_of_a_derived_repo_is_provisioned_with_no_decision_of_its_own(self) -> None:
        """The reviewer is told about the PACKAGE only. `foo.gpg` still lands, and lands
        before the repository that references it, which lands before the install.
        """
        context, _source, target = _repo_context(source_responses=_foo_source_responses())
        target.run_command = AsyncMock(side_effect=_foo_target_side_effect())
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        commands = all_calls(target)
        key_idx = _index_of(commands, lambda c: "sudo install" in c and "keyrings/foo.gpg" in c)
        source_idx = _index_of(commands, lambda c: "sudo install" in c and "sources.list.d/foo.sources" in c)
        install_idx = _index_of(commands, lambda c: c.startswith("sudo DEBIAN") and "pkg-a" in c)
        assert key_idx < source_idx < install_idx

    @pytest.mark.asyncio
    async def test_key_of_an_overwritten_repo_is_provisioned_too(self) -> None:
        """A repository the target already has with different bytes may point at a keyring
        it has never seen — the `Signed-By:` line is part of what differs.
        """
        context, _source, target = _repo_context(source_responses=_foo_source_responses())
        target.run_command = AsyncMock(
            side_effect=_foo_target_side_effect(
                {"find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "foo.sources"), "")}
            )
        )
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

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
        _fmt, refs, _uris = parse_source_file("inline.sources", _INLINE_SOURCES)

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
        assert _key_deletions(target) == ["sudo rm --force /etc/apt/keyrings/shared.gpg"]
        source_idx = _index_of(commands, lambda c: "sudo rm --force" in c and "sources.list.d/going.list" in c)
        key_idx = _index_of(commands, lambda c: "sudo rm --force" in c and "keyrings/shared.gpg" in c)
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
        backup_idx = _index_of(commands, lambda c: c.startswith("sudo cp --archive /etc/apt/keyrings/shared.gpg"))
        delete_idx = _index_of(commands, lambda c: c == "sudo rm --force /etc/apt/keyrings/shared.gpg")
        assert backup_idx < delete_idx
        delete_call = next(
            call
            for call in target.run_command.call_args_list
            if call.args[0] == "sudo rm --force /etc/apt/keyrings/shared.gpg"
        )
        assert delete_call.kwargs.get("mutates")


def _all_removals(target: MagicMock) -> list[str]:
    return [c for c in all_calls(target) if c.startswith("sudo rm --force")]


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
    origin: str = "https://vendor.example.com",
    source_shared: str = sha256_line("k1", "vendor.gpg"),
    target_shared: str = "",
    dpkg_output: str = "",
) -> tuple[JobContext, MagicMock, MagicMock]:
    """One repository whose `Signed-By:` points into `/usr/share/keyrings`, derived by the
    package `pkg-a` it serves, with the target's copy of that directory and its
    `dpkg --search` answer under the test's control.
    """
    context, source, target = _repo_context(
        source_responses={
            "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
            "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            "apt-cache policy": CommandResult(0, _policy_block("pkg-a", origin), ""),
            _SOURCE_SCAN_CMD: CommandResult(0, _scan_line(filename, content), ""),
            "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", filename), ""),
            "find /usr/share/keyrings": CommandResult(0, source_shared, ""),
        },
    )
    target.run_command = AsyncMock(
        side_effect=_foo_target_side_effect(
            {
                "find /usr/share/keyrings": CommandResult(0, target_shared, ""),
                # dpkg --search exits non-zero as soon as ANY argument is unowned, which is
                # the norm: the exit code must not be what decides ownership.
                "dpkg --search": CommandResult(1, dpkg_output, "dpkg-query: no path found matching pattern\n"),
            },
            origin=origin,
        )
    )
    return context, source, target


class TestSharedKeyringsDirectory:
    """`/usr/share/keyrings` resolves references, is provisioned for referenced keys only,
    and is never collected.
    """

    @pytest.mark.asyncio
    async def test_a_usr_share_keyrings_reference_resolves_and_the_repo_is_replicable(self) -> None:
        context, _source, _target = _shared_key_context()
        job = AptSyncJob(context)

        plan = await job.plan()

        # The reference resolved, so the package is replicable and drags the repository with
        # it. A `/usr/share/keyrings` reference that went unseen would read as dangling and
        # make the package REPO_UNAVAILABLE instead.
        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL
        assert job._work.origins.plans["apt:package:pkg-a"].derived_files == frozenset({"vendor.sources"})  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_a_hand_placed_key_the_target_lacks_is_provisioned(self) -> None:
        """Nothing on this machine owns `vendor.gpg` — it is as machine-local as anything in
        `/etc/apt/keyrings`, and currently replicated nowhere.
        """
        context, _source, target = _shared_key_context()
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

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
        _install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        assert _key_writes(target) == []
        assert any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/vendor.sources") for c in all_calls(target)
        )

    @pytest.mark.asyncio
    async def test_a_package_owned_key_the_target_is_missing_is_copied_anyway(self) -> None:
        """The bootstrap case. `dpkg --search` answers from the package's FILE LIST, so a keyring
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
        _install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        assert _key_writes(target) == ["/usr/share/keyrings/vendor.gpg"]

    @pytest.mark.asyncio
    async def test_ownership_is_probed_once_for_every_key_directory(self) -> None:
        """One batched `dpkg --search` naming every key the target has across all three
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

        dpkg_calls = [c for c in all_calls(target) if c.startswith("dpkg --search")]
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
        """The check must still bite, and it bites on the PACKAGE now (D-39): `ghost.gpg`
        exists in no key directory on the source, so the only file that could deliver
        `pkg-a`'s origin cannot be written and the package is reported, not installed.

        Exactly one line says so. Under the old model the repository ALSO reported the same
        dangling reference, which told the user the same thing twice about two objects.
        """
        context, _source, _target = _shared_key_context(
            filename="ghost.sources",
            content=_GHOST_SOURCES,
            origin="https://ghost.example.com",
            source_shared="",
        )

        plan = await AptSyncJob(context).plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert (diff.diff_class, diff.action) == (DiffClass.REPO_UNAVAILABLE, DiffAction.REPORT_ONLY)
        assert diff.detail is not None
        assert "/etc/apt/keyrings/ghost.gpg" in diff.detail
        assert not any(d.item_id.startswith("apt:source:") for d in plan.diffs)


class TestInlineArmoredSignedBy:
    """A `Signed-By:` value that is not an absolute path is an inline armored key, not a
    reference. Every PPA `add-apt-repository` adds is written that way.
    """

    def test_the_armor_first_line_on_the_field_line_yields_no_ref(self) -> None:
        _fmt, refs, _uris = parse_source_file("ppa.sources", _INLINE_ON_FIELD_LINE)

        assert refs == ()

    @pytest.mark.asyncio
    async def test_a_ppa_with_an_inline_key_installs_normally_and_needs_no_keyring(self) -> None:
        context, _source, target = _shared_key_context(
            filename="ppa.sources",
            content=_INLINE_ON_FIELD_LINE,
            origin="https://ppa.example.com",
            source_shared="",
        )
        job = AptSyncJob(context)
        _install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        assert _key_writes(target) == []
        assert any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/ppa.sources") for c in all_calls(target)
        )


# -- One review per run, before the first write (ADR-020 D-24) -------------------------
#
# A package is classified from the SOURCE's origins, which no run mutates, so nothing this
# run writes can invalidate a decision it already took. The one fact that genuinely depends
# on the target's post-write state — which origin actually wins — is read back by the D-35
# verification and becomes a per-item refusal, never a second question.
#
# Deleting the pin echo is what makes the other half true. It fired for every package a
# target-side `preferences.d` stanza named, which suppressed a target-only package's own
# removal diff and, being `REPORT_ONLY`, could not be silenced with skip-always either.

_CURL_PIN_FILE = "Package: curl\nPin: version 8.0\nPin-Priority: 1001\n"
# The two `preferences.d` reads a run can make, distinguished because a bare
# `find /etc/apt/preferences.d` substring matches both. The digest listing is what makes a
# pin travel as a FILE and must keep happening; the stanza scan is the retired echo's input
# and must not.
_PIN_DIGEST_CMD = "find /etc/apt/preferences.d -maxdepth 1 -type f -exec sha256sum"

# A real pin body: which origin it favours and by how much — none of which the filename
# `vendor-pin` conveys, which is why the deletion screen prints the file.
_VENDOR_PIN = "Package: *\nPin: origin vendor.example.com\nPin-Priority: 900\n"
_PIN_STANZA_SCAN_CMD = "-exec awk '/^Package:/"
_CURL_PIN_STANZAS = "/etc/apt/preferences.d/curl-pin\tPackage: curl\n"
_MOZILLA_PIN_FILE = "Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n"


class _CountingReviewer(FakeReviewer):
    """`FakeReviewer` that keeps EVERY call's groups, not just the last one."""

    def __init__(self, decisions: dict[str, Decision]) -> None:
        super().__init__(decisions)
        self.calls: list[tuple[ReviewGroup, ...]] = []

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self.calls.append(tuple(groups))
        return await super().review(groups)


def _actionable_entry_ids(groups: Sequence[ReviewGroup]) -> set[str]:
    """Item ids the user was actually offered a converge action for. A `REPORT_ONLY` entry
    is shown but implies no verb, so it is exactly what a suppressed case looks like.
    """
    return {
        entry.item_id for group in groups if group.action != DiffAction.REPORT_ONLY.value for entry in group.entries
    }


def _pinned_target_only_package_context(
    **extra_decisions: Decision,
) -> tuple[AptSyncJob, MagicMock, _CountingReviewer]:
    """`curl` exists only on the TARGET, and the target's `preferences.d/curl-pin` names it.
    This is the exact shape the retired echo made unremovable and unsilenceable.

    The target answers BOTH `preferences.d` reads — the digest listing this code issues and
    the `Package:` stanza scan it no longer does — and the stanza scan empties once the pin
    file is actually deleted. Answering only the read the current code makes would let an
    implementation that still consults the stanzas pass these tests by accident.
    """
    responses = {
        "echo $HOME": CommandResult(0, "/home/target-user", ""),
        "apt-mark showmanual": CommandResult(0, "curl\n", ""),
        "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
        _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p1", "curl-pin"), ""),
        "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, _CURL_PIN_FILE, ""),
        "apt-get --dry-run remove --assume-yes curl": CommandResult(0, "Remv curl [8.0]\n", ""),
    }
    state = {"pin_deleted": False}

    def _target(cmd: str, **_: object) -> CommandResult:
        if cmd.startswith("sudo rm --force") and "curl-pin" in cmd:
            state["pin_deleted"] = True
        if _PIN_STANZA_SCAN_CMD in cmd:
            return CommandResult(0, "" if state["pin_deleted"] else _CURL_PIN_STANZAS, "")
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        return CommandResult(0, "", "")

    context, _source, target = _repo_context(target_side_effect=_target)
    job = AptSyncJob(context)
    reviewer = _CountingReviewer({"apt:package:curl": Decision.APPLY, **extra_decisions})
    job.context = dataclasses.replace(job.context, reviewer=reviewer)
    return job, target, reviewer


class TestAPinNeverSpeaksForAPackage:
    """The defect ADR-020 D-25 closes: a package present only on the target and named by
    any pin stanza produced a `REPORT_ONLY` echo instead of its own removal diff — so it
    could neither be removed nor marked machine-specific, and came back every run.
    """

    @pytest.mark.asyncio
    async def test_a_target_only_package_named_by_a_pin_is_offered_for_removal(self) -> None:
        job, _target, _reviewer = _pinned_target_only_package_context()

        plan = await job.plan()

        curl = next(d for d in plan.diffs if d.item_id == "apt:package:curl")
        assert (curl.diff_class, curl.action) == (DiffClass.EXTRA_ON_TARGET, DiffAction.REMOVE)

    @pytest.mark.asyncio
    async def test_the_removal_reaches_the_user_as_an_actionable_review_entry(self) -> None:
        """`REPORT_ONLY` was the whole problem: it is shown but carries no verb, so it can
        be neither applied nor recorded skip-always.
        """
        job, _target, reviewer = _pinned_target_only_package_context()

        await job.execute()

        assert "apt:package:curl" in _actionable_entry_ids(reviewer.calls[0])

    @pytest.mark.asyncio
    async def test_approving_it_actually_removes_the_package(self) -> None:
        job, target, _reviewer = _pinned_target_only_package_context()

        await job.execute()

        assert any(c.startswith("sudo DEBIAN") and "apt-get remove" in c and "curl" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_no_command_asks_the_target_which_packages_its_pins_name(self) -> None:
        """The stanza scan is gone with the echo. A pin file still travels as a FILE — its
        digest is captured — but nothing parses package names out of it any more.
        """
        job, target, _reviewer = _pinned_target_only_package_context()

        await job.execute()

        assert not any("/^Package:/" in cmd for cmd in all_calls(target))
        assert any(_PIN_DIGEST_CMD in cmd for cmd in all_calls(target))


class TestTwoAnswerRemovals:
    """Rulings 5 and 12: a repository or pin the source no longer has is still reviewed —
    nothing derives a deletion — but with two answers, on its own screen, and with no
    machine-local registry behind it.
    """

    @staticmethod
    def _target_only_repo_state() -> tuple[JobContext, MagicMock, MagicMock]:
        """A target carrying one repository and one pin the source does not have, plus an
        apt-config file that must NOT be swept into the same shape (ruling 11)."""
        return make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "vendor.list"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
                "cat /etc/apt/preferences.d/vendor-pin": CommandResult(0, _VENDOR_PIN, ""),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c9", "99extra"), ""),
            },
        )

    @pytest.mark.asyncio
    async def test_repository_and_pin_removals_get_two_separate_two_answer_screens(self) -> None:
        """One sentinel, two groups: `_build_review_groups` keys on the item class, so a
        repository deletion and a pin deletion never share a list. Apt config keeps the
        ordinary action value and therefore the ordinary three-way path.
        """
        context, _source, _target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        by_action = {(group.action, group.entries[0].item_id.split(":")[1]): group for group in plan.groups}
        assert (REPO_REMOVAL_REVIEW_ACTION, "source") in by_action
        assert (REPO_REMOVAL_REVIEW_ACTION, "pin") in by_action
        assert by_action[(REPO_REMOVAL_REVIEW_ACTION, "source")].entries[0].action_label == "delete repository"
        assert by_action[(REPO_REMOVAL_REVIEW_ACTION, "pin")].entries[0].action_label == "delete pin file"
        # Ruling 11: the config file is an ordinary removal, in an ordinary group.
        assert (DiffAction.REMOVE.value, "config") in by_action

    @pytest.mark.asyncio
    async def test_each_two_answer_screen_is_titled_in_correct_english(self) -> None:
        """The title names the plural of the OBJECT, not a verb phrase with an `s` glued on
        the end — "repositorys" is what the latter produces.
        """
        context, _source, _target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        titles = {group.title for group in plan.groups if group.action == REPO_REMOVAL_REVIEW_ACTION}
        assert titles == {
            "Delete repositories source-host no longer has (apt)",
            "Delete pin files source-host no longer has (apt)",
        }

    @pytest.mark.asyncio
    async def test_a_pin_offered_for_deletion_carries_its_whole_content(self) -> None:
        """A pin filename says nothing about which vendor it favours or by how much, and the
        filename is all a decision row can show. The file itself is what the answer needs.
        """
        context, _source, target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        pins = next(
            group
            for group in plan.groups
            if group.action == REPO_REMOVAL_REVIEW_ACTION and group.entries[0].item_id.startswith("apt:pin:")
        )
        assert pins.entries[0].content == _VENDOR_PIN
        assert "sudo cat /etc/apt/preferences.d/vendor-pin" in all_calls(target)

    @pytest.mark.asyncio
    async def test_a_pin_read_that_did_not_answer_fails_the_job(self) -> None:
        """ADR-022: silence from `cat` is not an empty pin file. An empty block on a deletion
        screen is an approval given off nothing at all."""
        context, _source, target = self._target_only_repo_state()
        target.run_command.side_effect = respond_to(
            {
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "vendor.list"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
                "cat /etc/apt/preferences.d/vendor-pin": CommandResult(1, "", "cat: Permission denied"),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c9", "99extra"), ""),
            }
        )

        with pytest.raises(ProbeFailed, match=re.escape("cat /etc/apt/preferences.d/vendor-pin")):
            await AptSyncJob(context).plan()

    @pytest.mark.asyncio
    async def test_a_repository_offered_for_deletion_carries_no_content_block(self) -> None:
        """Its URLs are in the detail line; a second whole-file block would be the same fact
        twice, and a `.sources` body is mostly fields the user is not deciding on."""
        context, _source, _target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        repos = next(
            group
            for group in plan.groups
            if group.action == REPO_REMOVAL_REVIEW_ACTION and group.entries[0].item_id.startswith("apt:source:")
        )
        assert repos.entries[0].content is None
        assert repos.entries[0].detail == (
            "target-host would stop getting software from https://vendor.example.com/apt"
        )

    @pytest.mark.asyncio
    async def test_a_two_answer_group_is_unticked_and_never_offered_permanence(self) -> None:
        """Both halves of the sentinel's contract, read off the real groups this job builds
        rather than a hand-made one: unticked because it is a removal direction, never
        promoted because it is not promotable.
        """
        context, _source, _target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        two_answer = [group for group in plan.groups if group.action == REPO_REMOVAL_REVIEW_ACTION]
        assert len(two_answer) == 2, "the repository and the pin deletion each need their own screen"
        for group in two_answer:
            assert _is_removal_direction(group.action)
            assert not _is_promotable_group(group.action)

    @pytest.mark.asyncio
    async def test_approving_a_pin_removal_deletes_the_file(self) -> None:
        """The answer that acts still acts: two answers, not one."""
        context, _source, target = _repo_context(
            target_responses={
                **_NO_PACKAGES,
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
            }
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:pin:vendor-pin": Decision.APPLY})

        await job.execute()

        removals = [c for c in all_calls(target) if c.startswith("sudo rm --force")]
        assert removals == ["sudo rm --force /etc/apt/preferences.d/vendor-pin"]

    @pytest.mark.asyncio
    async def test_the_repository_goes_before_the_pin_that_prefers_it(self) -> None:
        """Deletion order is the reverse of the write order (§3.3 step 5): a pin naming an
        origin apt no longer has is a worse intermediate state than a repository nothing
        prefers.
        """
        context, _source, target = _repo_context(
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "vendor.list"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
            }
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:source:vendor.list": Decision.APPLY, "apt:pin:vendor-pin": Decision.APPLY})

        await job.execute()

        removals = [c for c in all_calls(target) if c.startswith("sudo rm --force") and "/etc/apt/" in c]
        assert removals == [
            "sudo rm --force /etc/apt/sources.list.d/vendor.list",
            "sudo rm --force /etc/apt/preferences.d/vendor-pin",
        ]


class TestAptConfigVocabulary:
    """Ruling 11's other half: `/etc/apt/apt.conf.d` is the one reviewed class that is not
    a package, so every one of its three directions needs its own verb AND its own noun.
    Without both, a config file is announced as "Install/Change/Remove apt packages".
    """

    @staticmethod
    def _all_three_directions() -> JobContext:
        """One apt-config file per direction: `10add` only on the source, `20update` on
        both with different bytes, `30delete` only on the target."""
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("a1", "10add") + sha256_line("u-new", "20update"), ""
                ),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("u-old", "20update") + sha256_line("d1", "30delete"), ""
                ),
            },
        )
        return context

    @pytest.mark.asyncio
    async def test_each_direction_names_the_config_file_not_a_package(self) -> None:
        context = self._all_three_directions()

        plan = await AptSyncJob(context).plan()

        by_action = {group.action: group for group in plan.groups if group.entries[0].item_id.startswith("apt:config")}
        assert [(group.title, group.entries[0].action_label) for _action, group in sorted(by_action.items())] == [
            ("Update apt configuration files", "update"),
            ("Add apt configuration files", "add"),
            ("Delete apt configuration files", "delete"),
        ]

    @pytest.mark.asyncio
    async def test_no_apt_config_group_claims_to_be_about_packages(self) -> None:
        """The measured defect, pinned so it cannot come back through the fallback verb."""
        context = self._all_three_directions()

        plan = await AptSyncJob(context).plan()

        config_groups = [group for group in plan.groups if group.entries[0].item_id.startswith("apt:config")]
        assert len(config_groups) == 3
        assert not any("packages" in group.title for group in config_groups)


class TestRepositoryConflicts:
    """Ruling 6: a repository file present on both machines with different content is
    overwritten silently — EXCEPT when it feeds a package the target recorded
    machine-specific, which is the one `/etc/apt` change the user is still asked about.
    """

    _CHANGED_VENDOR = "deb [signed-by=/etc/apt/keyrings/vendor.gpg] https://vendor.example.com/apt noble main\n"

    @classmethod
    def _differing_repo(cls, *, recorded: str) -> tuple[JobContext, MagicMock, MagicMock]:
        """`vendor.list` on both machines with different bytes, declaring the origin the
        target's `curl` is installed from. `recorded` is the target's decision file."""
        return _repo_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", cls._CHANGED_VENDOR), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, cls._CHANGED_VENDOR, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                "apt.decisions.yaml": CommandResult(0, recorded, ""),
                "apt-cache policy": CommandResult(0, _policy_block("curl", "https://vendor.example.com/apt"), ""),
            },
        )

    @pytest.mark.asyncio
    async def test_a_changed_repository_with_no_machine_specific_package_is_overwritten_silently(self) -> None:
        """The ordinary case, and the reason the trigger is narrow: two machines whose
        repository definitions have drifted are meant to converge, not to negotiate.
        """
        context, _source, _target = self._differing_repo(recorded="machine_specific: {}\n")

        plan = await AptSyncJob(context).plan()

        assert not any(group.action == REPO_CONFLICT_REVIEW_ACTION for group in plan.groups)

    @pytest.mark.asyncio
    async def test_a_changed_repository_feeding_a_machine_specific_package_asks_and_shows_both_versions(self) -> None:
        """The entry carries both whole files, the target's first — the user asked for the
        two versions, not a unified diff — and offers exactly the two answers.
        """
        context, _source, _target = self._differing_repo(recorded=_decision_file("apt:package:curl"))

        plan = await AptSyncJob(context).plan()

        group = next(g for g in plan.groups if g.action == REPO_CONFLICT_REVIEW_ACTION)
        entry = group.entries[0]
        assert entry.label == "vendor.list"
        assert entry.versions == (_VENDOR_LIST, self._CHANGED_VENDOR)
        assert entry.detail is not None and "curl" in entry.detail

    @pytest.mark.asyncio
    async def test_overwriting_a_conflict_writes_the_sources_version(self) -> None:
        context, _source, target = self._differing_repo(recorded=_decision_file("apt:package:curl"))
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:conflict:vendor.list": Decision.APPLY})

        await job.execute()

        assert any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/vendor.list") for c in all_calls(target)
        )

    @pytest.mark.asyncio
    async def test_skipping_a_conflict_writes_nothing_and_fails_the_package_that_needed_it(self) -> None:
        """The coupling §4.3 requires: a skipped conflict is not the same as no conflict.
        The package the user ticked depends on that file for its origin, so installing it
        anyway would deliver the wrong vendor's software — exactly what D-34 exists to stop.
        """
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", self._CHANGED_VENDOR), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, self._CHANGED_VENDOR, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                "apt.decisions.yaml": CommandResult(0, _decision_file("apt:package:curl"), ""),
                "apt-cache policy": CommandResult(0, _policy_block("curl", "https://vendor.example.com/apt"), ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a"}
        assert "/etc/apt/sources.list.d/vendor.list" in failures["apt:package:pkg-a"]
        assert not any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/vendor.list") for c in all_calls(target)
        )
        assert not _real_installs(target)

    @pytest.mark.asyncio
    async def test_the_conflict_computation_costs_one_batched_policy_call(self) -> None:
        """Both `/etc/apt` follow-ups share one computation (§4.4): a run offering a removal
        AND a conflict asks the target's apt once, not twice.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", self._CHANGED_VENDOR), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, self._CHANGED_VENDOR, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(
                    0, _scan_line("vendor.list", _VENDOR_LIST) + _scan_line("gone.list", _RIVAL_LIST), ""
                ),
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("d-old", "vendor.list") + sha256_line("d9", "gone.list"), ""
                ),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                "cat /etc/apt/sources.list.d/gone.list": CommandResult(0, _RIVAL_LIST, ""),
                "apt.decisions.yaml": CommandResult(0, _decision_file("apt:package:curl"), ""),
                "apt-cache policy": CommandResult(0, _policy_block("curl", "https://vendor.example.com/apt"), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert any(g.action == REPO_CONFLICT_REVIEW_ACTION for g in plan.groups)
        assert sum(1 for c in all_calls(target) if "apt-cache policy" in c) == 1


class TestPinsStillTravelAsFiles:
    """The echo was a REPORT about pins, never the mechanism. A `preferences.d` file is
    what makes a vendor's origin outrank the archive's epoch-1 copy (D-36), so it has to
    keep reaching the target — deleting the echo must not touch that.
    """

    @pytest.mark.asyncio
    async def test_a_pin_file_the_target_lacks_is_written_with_no_review_line(self) -> None:
        """The always-sync bucket (D-36): the reviewer is handed nothing at all, and the pin
        still lands. A pin naming an origin the target does not have is inert, so this cannot
        get a derivation wrong — and it is what makes Mozilla's build outrank the archive's
        epoch-1 copy when the origin DOES arrive.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p1", "mozilla"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        reviewer = _CountingReviewer({})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert any(
            "sudo install" in cmd and cmd.endswith("/etc/apt/preferences.d/mozilla") for cmd in all_calls(target)
        )
        assert reviewer.calls == [()]

    @pytest.mark.asyncio
    async def test_a_differing_pin_is_overwritten_rather_than_reviewed(self) -> None:
        """The change direction of the same rule. Under the old model this was a CHANGE line
        the user could untick; the file now simply travels.
        """
        context, _source, target = _repo_context(
            source_responses={**_NO_PACKAGES, _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p-new", "mozilla"), "")},
            target_responses={
                **_NO_PACKAGES,
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p-old", "mozilla"), ""),
                "test -f /etc/apt/preferences.d/mozilla": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        plan = await job.plan()
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True))
        await job.apply()

        assert not any(d.item_id == "apt:pin:mozilla" for d in plan.diffs)
        assert any(
            "sudo install" in cmd and cmd.endswith("/etc/apt/preferences.d/mozilla") for cmd in all_calls(target)
        )

    @pytest.mark.asyncio
    async def test_the_pin_file_needs_no_read_of_its_contents(self) -> None:
        """Its whole-file digest decides everything. Nothing parses the stanzas any more, so
        the plan-time content read that only hydrated the retired echo is gone too — the
        bytes reach the target through `send_file`, never through a parse.
        """
        context, source, _target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p1", "mozilla"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        _install_reviewer(job, {})

        await job.execute()

        assert not any("cat /etc/apt/preferences.d" in cmd for cmd in all_calls(source))


class TestOneReviewPerRun:
    """Every apt prompt precedes the job's first mutating command, unconditionally."""

    @pytest.mark.asyncio
    async def test_a_package_the_target_had_no_candidate_for_is_installed_in_one_review(self) -> None:
        """At plan time the target's apt reports no candidate at all; the repository this
        run installs supplies one. The package is classified from the SOURCE's origin and
        the file declaring it, so its actionability never depended on a repository this run
        had not written yet — and one screen is enough.
        """
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
                "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
            }.items():
                if pattern in cmd:
                    return result
            return CommandResult(0, "", "")

        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://example.com"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_side_effect=_target,
        )
        job = AptSyncJob(context)
        reviewer = _CountingReviewer({"apt:source:foo.sources": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert len(reviewer.calls) == 1
        assert "apt:package:pkg-a" in _actionable_entry_ids(reviewer.calls[0])
        assert any(c.startswith("sudo DEBIAN") and "install" in c and "pkg-a" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_run_that_rewrites_etc_apt_still_reviews_exactly_once(self) -> None:
        """The general property, asserted against the run shape that used to trigger the
        second screen: the pin the user is deleting really is deleted, `/etc/apt` really is
        refreshed, and the user is still asked exactly once.
        """
        job, target, reviewer = _pinned_target_only_package_context(**{"apt:pin:curl-pin": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any(c.startswith("sudo rm --force") and "curl-pin" in c for c in commands)
        assert any(c.startswith("sudo apt-get update") for c in commands)
        assert len(reviewer.calls) == 1


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


class TestRepoUnavailableWording:
    """`REPO_UNAVAILABLE`'s detail: the source's origin cannot be provided (ADR-020 D-25)."""

    def test_build_repo_unavailable_detail_names_the_package_its_origin_and_the_cause(self) -> None:
        detail = build_repo_unavailable_detail(
            "brscan3", ["https://gone.example.com/apt"], "no repository file on atlas declares it", MACHINES
        )

        assert detail == (
            "target-host cannot install brscan3 from gone.example.com/apt: no repository file on atlas declares it"
        )


class TestRepoRemovalWording:
    """A repository deletion is decided from the URLs it serves, not from its filename —
    which is whatever whoever created the file happened to call it."""

    def test_the_urls_come_first_and_the_stranded_packages_second(self) -> None:
        detail = build_repo_removal_detail(
            ["https://cli.github.com/packages"],
            "target-host installs gh from 99-github.list",
            MACHINES,
        )

        assert detail == (
            "target-host would stop getting software from https://cli.github.com/packages; "
            "target-host installs gh from 99-github.list"
        )

    def test_every_url_the_file_declares_is_named(self) -> None:
        detail = build_repo_removal_detail(["https://a.example.com/apt", "https://b.example.com/deb"], None, MACHINES)

        assert detail == (
            "target-host would stop getting software from https://a.example.com/apt, https://b.example.com/deb"
        )

    def test_a_file_declaring_no_url_says_so_rather_than_trailing_off(self) -> None:
        """A commented-out leftover parses to no URI. Half a sentence would read as a bug."""
        detail = build_repo_removal_detail([], None, MACHINES)

        assert detail == "target-host would stop getting software from nowhere — it declares no repository URL"


class TestDiffEngine:
    """`diff_apt_packages` produces every D-25 diff class."""

    def test_missing_on_target_yields_install(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, [], {}, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.MISSING_ON_TARGET
        assert diffs[0].action == DiffAction.INSTALL

    def test_extra_on_target_yields_remove(self) -> None:
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages([], target_items, {}, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.EXTRA_ON_TARGET
        assert diffs[0].action == DiffAction.REMOVE

    def test_version_mismatch_yields_report_only_with_both_versions(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="2.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.VERSION_MISMATCH
        assert diffs[0].action == DiffAction.REPORT_ONLY
        assert diffs[0].detail is not None
        assert "1.0" in diffs[0].detail
        assert "2.0" in diffs[0].detail

    def test_equal_versions_yields_no_diff(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES)

        assert diffs == []

    def test_source_hold_only_yields_apt_hold_install(self) -> None:
        """#208: a name held on the source but not the target is an `apt:hold:` INSTALL
        (hold), a distinct APT_HOLD item — never a package-level report.
        """
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES, frozenset({"pkg-a"}), frozenset())

        assert len(diffs) == 1
        assert diffs[0].item_class == ItemClass.APT_HOLD
        assert diffs[0].item_id == "apt:hold:pkg-a"
        assert diffs[0].action == DiffAction.INSTALL

    def test_target_hold_only_yields_apt_hold_remove_and_suppresses_package_action(self) -> None:
        """#208: a name held on the target but not the source is an `apt:hold:` REMOVE
        (unhold); the version-mismatch package action it would otherwise carry is
        suppressed (a held package is never proposed for upgrade) and no package-level
        report is emitted for the hold mechanism.
        """
        source_items = [AptPackageItem(name="pkg-a", version="2.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES, frozenset(), frozenset({"pkg-a"}))

        assert len(diffs) == 1
        assert diffs[0].item_class == ItemClass.APT_HOLD
        assert diffs[0].action == DiffAction.REMOVE

    def test_held_on_both_yields_no_diff(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES, frozenset({"pkg-a"}), frozenset({"pkg-a"}))

        assert diffs == []

    def test_the_diff_takes_no_pin_input_at_all(self) -> None:
        """The signature is the deletion, made structural: with no pin argument there is no
        way to reintroduce the echo without changing every caller (ADR-020 D-25).
        """
        parameters = inspect.signature(diff_apt_packages).parameters

        assert not any("pin" in name for name in parameters)

    def test_an_origin_no_source_file_declares_yields_repo_unavailable_not_install(self) -> None:
        """ADR-020 D-34 class 4, the only remaining meaning of `REPO_UNAVAILABLE`: the
        source has the package from a repository that has since been deleted from the
        source's own `/etc/apt`, so the origin cannot be handed to the target at all.
        """
        source_items = [AptPackageItem(name="brscan3", version="")]
        plan = {"apt:package:brscan3": OriginPlan(source_origins=frozenset({"https://gone.example.com/apt"}))}

        diffs = diff_apt_packages(source_items, [], plan, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.REPO_UNAVAILABLE
        assert diffs[0].action == DiffAction.REPORT_ONLY
        assert diffs[0].detail == (
            "target-host cannot install brscan3 from gone.example.com/apt: "
            "no repository file on source-host declares it"
        )


class TestAReadThatDidNotAnswer:
    """ADR-022, applied to the reads that build the two manifests and the `/etc/apt`
    picture: a read that did not answer fails the job naming the command, a read that
    answered "nothing" is data.

    Which of the two an empty result is depends on the command, and every test here isolates
    exactly one read: everything else in the fixture answers normally, so nothing but the
    named read can produce the outcome.
    """

    @pytest.mark.asyncio
    async def test_a_source_manual_set_read_that_did_not_answer_fails_the_job(self) -> None:
        """Measured: `apt-mark showmanual` exits 100 when it cannot read `/var/lib/dpkg/
        status` or parse `apt.conf.d`. Reading that silence as data makes the source
        manifest empty, which offers every package on the target for removal.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(100, "", "E: Problem opening /var/lib/dpkg/status\n")
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\n", ""),
            },
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-mark showmanual" in str(excinfo.value)
        assert "exited 100" in str(excinfo.value)
        assert "/var/lib/dpkg/status" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_empty_source_manual_set_at_exit_zero_is_still_data(self) -> None:
        """The deliberate limit of the rule above, pinned so it is not silently widened
        later: the guard is on the EXIT CODE, and an empty answer at exit 0 still reaches
        the diff as "remove the target's packages". Widening it to "empty means broken"
        would fail every run against a machine whose manual set is legitimately empty.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\n", ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        removals = {d.item_id for d in plan.diffs if d.action == DiffAction.REMOVE}
        assert removals == {"apt:package:pkg-x"}

    @staticmethod
    def _target_failing_nth_showmanual(n: int) -> Callable[..., CommandResult]:
        """A target whose n-th (1-based) `apt-mark showmanual` fails and whose others
        answer normally.

        `plan()` asks the target that ONE command twice — the manifest read, then the
        collateral protection set — and a substring fixture cannot tell the two apart. A
        fixture that failed both would pass on either guard's behalf, which is exactly the
        vacuous shape these two tests exist to avoid.
        """
        state = {"calls": 0}
        inner = respond_to({"dpkg-query": CommandResult(0, "pkg-x\t1.0\n", "")})

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "apt-mark showmanual" in cmd:
                state["calls"] += 1
                if state["calls"] == n:
                    return CommandResult(100, "", "E: Could not open lock file\n")
                return CommandResult(0, "pkg-x\n", "")
            return inner(cmd, **kwargs)

        return _side_effect

    @pytest.mark.asyncio
    async def test_a_target_manifest_read_that_did_not_answer_fails_the_job(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            }
        )
        target.run_command = AsyncMock(side_effect=self._target_failing_nth_showmanual(1))

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-mark showmanual" in str(excinfo.value)
        assert "target" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_collateral_protection_read_that_did_not_answer_fails_the_job(self) -> None:
        """The second of the two. Its silence empties the target's manual set, which
        classifies every collateral package as automatic and switches D-30's protection off
        entirely — the manifest read above it answers normally, so only this one can fail.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            }
        )
        target.run_command = AsyncMock(side_effect=self._target_failing_nth_showmanual(2))

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-mark showmanual" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_version_read_that_did_not_answer_fails_the_job(self) -> None:
        """A `dpkg-query` that does not answer leaves every version empty, which reads as a
        version difference against the other machine on every package at once.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(1, "", "dpkg-query: error: unable to access the database\n"),
            },
            target_responses=_NO_PACKAGES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "dpkg-query --show" in str(excinfo.value)
        assert "exited 1" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_hold_read_that_did_not_answer_fails_the_job(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(100, "", "E: The package lists could not be parsed\n"),
            },
            target_responses=_NO_PACKAGES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-mark showhold" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_empty_hold_set_is_data_not_a_failure(self) -> None:
        """Holding nothing is what most machines do, so an empty `apt-mark showhold` at
        exit 0 must stay ordinary data — the plan completes and proposes no hold.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
            },
            target_responses={**_NO_PACKAGES, "apt-mark showhold": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert not [d for d in plan.diffs if d.item_class == ItemClass.APT_HOLD]

    @pytest.mark.asyncio
    async def test_a_target_policy_read_that_did_not_answer_fails_the_job(self) -> None:
        """The source has a package, so the only `apt-cache policy` the TARGET is asked at
        plan time is `collect_target_policy`'s.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "apt-cache policy": CommandResult(100, "", "E: Could not get lock /var/lib/dpkg/lock-frontend\n"),
            },
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-cache policy pkg-a" in str(excinfo.value)
        assert "lock-frontend" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_target_policy_that_knows_none_of_the_source_names_is_data(self) -> None:
        """The `blocks` half of the apt guard is deliberately NOT applied here: these are
        the SOURCE's names asked of the TARGET's apt, and a target that has never heard of
        any of them is the ordinary case this call exists to detect. It must still plan.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={**_NO_PACKAGES, "apt-cache policy": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert {d.item_id for d in plan.diffs} == {"apt:package:pkg-a"}

    @pytest.mark.asyncio
    async def test_a_directory_digest_read_that_did_not_answer_fails_the_job(self) -> None:
        """`sudo find <dir> ... sha256sum` on the source keyrings directory. Its silence
        empties `_source_key_filenames`, which makes every `Signed-By:` reference look
        dangling.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(1, "", "find: '/etc/apt/keyrings': Permission denied\n"),
            },
            target_responses=_NO_PACKAGES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "find /etc/apt/keyrings" in str(excinfo.value)
        assert "Permission denied" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_absent_directory_answers_nothing_rather_than_failing(self) -> None:
        """The `sudo test -d` wrapper is what keeps a legitimately absent directory out of
        the failure path: it is what makes the command exit 0 with no output, which this
        asserts is planned through rather than raised on.

        The `sudo` on the TEST is pinned as tightly as the wrapper itself: an unprivileged
        `test -d` on a directory inside an unsearchable parent exits 1 and collapses the
        whole `if` to exit 0 with no output, which is the reshape answering "this machine
        has no pins" for a directory root would have listed.
        """
        context, _source, _target = make_context(source_responses=_NO_PACKAGES, target_responses=_NO_PACKAGES)
        job = AptSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert any(
            c.startswith(f"if sudo test -d {APT_PREFERENCES_DIR}; then sudo find {APT_PREFERENCES_DIR}")
            for c in all_calls(_source)
        )

    @pytest.mark.asyncio
    async def test_the_source_file_scan_selects_both_locations_from_one_start_point(self) -> None:
        """The shape of the scan, pinned verbatim, because the shape IS the classification:
        `/etc/apt` is the one start point whose existence apt guarantees, and the two
        locations are `-path` selectors under it.

        Naming `/etc/apt/sources.list` as a start point instead makes find exit 1 while
        still walking the directory when that file is absent, which is the same exit code a
        scan that could not run at all produces — and the scan's silence deletes keys that
        are still in use. A "simplification" back to two start points is the specific edit
        this asserts against, and no substring of the awk program can catch it.
        """
        context, _source, _target = make_context(source_responses=_NO_PACKAGES, target_responses=_NO_PACKAGES)

        await AptSyncJob(context).plan()

        scans = [c for c in all_calls(_source) if "-exec awk" in c]
        assert len(scans) == 1
        assert scans[0].startswith(
            "sudo find /etc/apt -maxdepth 2 -type f "
            "\\( -path /etc/apt/sources.list -o -path '/etc/apt/sources.list.d/*' \\) -exec awk "
        )

    @pytest.mark.asyncio
    async def test_a_source_file_scan_that_did_not_answer_fails_the_job(self) -> None:
        """The scan's silence reads as "no source file references any keyring", which is
        what deletes keys that are still in use.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(1, "", "find: '/etc/apt': No such file or directory\n"),
            },
            target_responses=_NO_PACKAGES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "-exec awk" in str(excinfo.value)
        assert "No such file or directory" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_conflict_content_read_that_did_not_answer_fails_the_job(self) -> None:
        """The two panes the repository-conflict review shows are `sudo cat` output
        (ADR-020 D-37). Reading that silence as CONTENT renders the source's pane empty and asks
        the user to approve an overwrite off a diff nobody could read. The TARGET's `cat`
        runs first and answers normally, so only the source's can fail this.
        """
        context, source, _target = TestRepositoryConflicts._differing_repo(recorded=_decision_file("apt:package:curl"))
        answering = source.run_command.side_effect

        def failing_cat(cmd: str, **kwargs: object) -> CommandResult:
            """The conflict fixture unchanged, except that the source cannot read the file."""
            if cmd.startswith("sudo cat "):
                return CommandResult(1, "", f"cat: {cmd.removeprefix('sudo cat ')}: Permission denied\n")
            return answering(cmd, **kwargs)

        source.run_command = AsyncMock(side_effect=failing_cat)

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        # "probe on the source", not a bare "source": the path itself contains that word.
        assert "sudo cat /etc/apt/sources.list.d/vendor.list" in str(excinfo.value)
        assert "probe on the source" in str(excinfo.value)
        assert "Permission denied" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_removal_content_read_that_did_not_answer_fails_the_job(self) -> None:
        """The other `sudo cat` call site: a file only the target has is read to learn its
        format before it is offered for removal. Its silence makes the removal item describe
        a file this run never read.
        """
        context, _source, _target = _repo_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "gone.list"), ""),
                "cat /etc/apt/sources.list.d/gone.list": CommandResult(
                    1, "", "cat: /etc/apt/sources.list.d/gone.list: Input/output error\n"
                ),
            },
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "sudo cat /etc/apt/sources.list.d/gone.list" in str(excinfo.value)
        assert "probe on the target" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_removal_impact_read_that_did_not_answer_fails_the_job(self) -> None:
        """`_machine_specific_packages_by_source_file`. Its silence answers "this
        repository strands nothing", which is the answer that lets a repository feeding
        machine-specific packages be removed or overwritten with no disclosure. The source
        holds no packages here, so `collect_target_policy` never runs and this is the only
        `apt-cache policy` the target is asked.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "vendor.list"), ""),
                "apt.decisions.yaml": CommandResult(0, _decision_file("apt:package:vendor-tool"), ""),
                "apt-cache policy": CommandResult(100, "", "E: Unable to read the package lists\n"),
            },
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-cache policy vendor-tool" in str(excinfo.value)
        assert "Unable to read the package lists" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The ESM / Ubuntu Pro attachment gate (ADR-020 D-38, spec §5.3)
#
# The hazard is NOT a failing refresh. Measured in a stock `ubuntu:24.04` container with
# both real ESM source files and no credentials: `apt-get update` exits 0, because
# `esm.ubuntu.com` serves its INDEX publicly. The ESM suites then enter candidate selection
# at priority 500 — above `noble/universe` — and only the POOL is 401, so the failure lands
# much later as `apt-get install` exiting 100 on the `.deb`. These fixtures model that
# machine: the target answers `pro status` with `attached: false` and every apt command
# still succeeds, so a test can never pass by accident on a target that is simply broken.

_ESM_APPS = "ubuntu-esm-apps.sources"
_ESM_INFRA = "ubuntu-esm-infra.sources"
# COMPOSED from the real `pro status --format json` shape, not captured: the account block
# is what must never leak, and a genuinely unattached machine reports it empty, so carrying
# a populated one here is the strictly harder input.
_PRO_ACCOUNT_EMAIL = "subscriber-9f3a@example.invalid"
_PRO_ACCOUNT = f'"account": {{"id": "aAbBcC", "name": "{_PRO_ACCOUNT_EMAIL}"}}'
_PRO_UNATTACHED = f'{{"attached": false, {_PRO_ACCOUNT}, "services": []}}'
_PRO_ATTACHED = f'{{"attached": true, {_PRO_ACCOUNT}, "services": [{{"name": "esm-apps"}}]}}'


class _GateReviewer(FakeReviewer):
    """`FakeReviewer` that also records what the TARGET had been asked at the moment each
    gate question was put — which is how "before the first mutating command" is measured
    rather than assumed.
    """

    def __init__(self, target: MagicMock, decisions: dict[str, Decision] | None = None) -> None:
        super().__init__(decisions)
        self._target = target
        self.target_calls_at_gate: list[list[str]] = []

    @override
    async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None:
        self.target_calls_at_gate.append(_mutating_calls(self._target))
        return await super().ask_gate(title=title, message=message, proceed_label=proceed_label, stop_label=stop_label)


def _mutating_calls(target: MagicMock) -> list[str]:
    """Every target command that declared itself a write. `mutates=` is mandatory on writes
    (`tests/unit/test_mutates_audit.py`), so this IS the set of changes the job made.
    """
    return [call.args[0] for call in target.run_command.call_args_list if call.kwargs.get("mutates") is not None] + [
        str(call.args[1]) for call in target.send_file.call_args_list if call.kwargs.get("mutates") is not None
    ]


def _esm_job(
    *,
    pro_status: CommandResult | list[CommandResult],
    gate_answers: Sequence[bool | None] = (),
    source_esm: Sequence[str] = (_ESM_APPS, _ESM_INFRA),
    dry_run: bool = False,
) -> tuple[AptSyncJob, MagicMock, _GateReviewer]:
    """A source carrying `source_esm` beside `ubuntu.sources`, and a target that has none of
    them and answers `pro status` with `pro_status` (a list is consumed one probe at a time,
    the last entry repeating).

    Every other target command succeeds, `apt-get update` included: an unattached machine is
    NOT a broken one, and a fixture that failed the refresh would let a wrong implementation
    pass for the wrong reason.
    """
    probes = pro_status if isinstance(pro_status, list) else [pro_status]
    state = {"probes": 0}

    def _target(cmd: str, **_: object) -> CommandResult:
        if cmd == PRO_STATUS_COMMAND:
            index = min(state["probes"], len(probes) - 1)
            state["probes"] += 1
            return probes[index]
        if "apt-mark showmanual" in cmd:
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    source_listing = sha256_line("d0", "ubuntu.sources") + "".join(
        sha256_line(f"e{n}", name) for n, name in enumerate(source_esm)
    )
    context, _source, target = _repo_context(
        source_responses={**_NO_PACKAGES, "find /etc/apt/sources.list.d": CommandResult(0, source_listing, "")},
        target_side_effect=_target,
        dry_run=dry_run,
    )
    job = AptSyncJob(context)
    reviewer = _GateReviewer(target)
    reviewer.gate_answers = list(gate_answers)
    job.context = dataclasses.replace(job.context, reviewer=reviewer)
    return job, target, reviewer


def _pro_probe_count(target: MagicMock) -> int:
    return sum(1 for cmd in all_calls(target) if cmd == PRO_STATUS_COMMAND)


def _promoted_files(target: MagicMock) -> list[str]:
    return [c.rsplit(" ", 1)[1] for c in all_calls(target) if c.startswith("sudo install --owner=root")]


class TestTheESMAttachmentGate:
    """D-38: the two `ubuntu-esm-*` sources are the one always-sync bucket that waits on a
    fact about the TARGET. Writing them to a machine with no Pro attachment leaves an apt
    whose next install of an ESM-covered package fails with a 401 nobody traces back to the
    sync — and pc-switcher cannot attach the machine itself, because `pro attach` needs a
    dashboard token or a browser short-code flow.
    """

    @pytest.mark.asyncio
    async def test_an_unattached_target_is_asked_about_before_anything_is_written(self) -> None:
        job, _target, reviewer = _esm_job(
            pro_status=[CommandResult(0, _PRO_UNATTACHED, ""), CommandResult(0, _PRO_ATTACHED, "")],
            gate_answers=[True],
        )

        await job.execute()

        assert len(reviewer.gate_calls) == 1
        message = reviewer.gate_calls[0]["message"]
        assert _ESM_APPS in message
        assert _ESM_INFRA in message
        # The gate asks the user to go and attach the other machine, so the message must
        # carry both the commands and the link that outlives them if Ubuntu changes the flow.
        assert "pro attach" in message
        assert "pro enable esm-apps esm-infra" in message
        assert "https://documentation.ubuntu.com/pro/attach-tutorial/" in message
        assert reviewer.target_calls_at_gate == [[]], "the gate must precede the job's first write"

    @pytest.mark.asyncio
    async def test_the_gate_offers_exactly_two_answers_and_names_both_of_them(self) -> None:
        job, _target, reviewer = _esm_job(
            pro_status=[CommandResult(0, _PRO_UNATTACHED, ""), CommandResult(0, _PRO_ATTACHED, "")],
            gate_answers=[True],
        )

        await job.execute()

        call = reviewer.gate_calls[0]
        assert call["proceed_label"] == "I have attached target-host — check again and continue"
        assert call["stop_label"] == "Skip apt_sync this run (every other job still runs)"
        assert call["title"] == "target-host needs an Ubuntu Pro attachment"

    @pytest.mark.asyncio
    async def test_choosing_skip_raises_job_skipped_and_writes_nothing(self) -> None:
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_UNATTACHED, ""), gate_answers=[False])

        with pytest.raises(JobSkipped) as excinfo:
            await job.execute()

        assert excinfo.value.job_name == "apt_sync"
        assert _mutating_calls(target) == []
        assert reviewer.groups_seen is None, "no review may be presented for a job that is about to skip"

    @pytest.mark.asyncio
    async def test_a_non_interactive_run_skips_the_whole_job(self) -> None:
        """The user's ruling, replacing an earlier fallback that withheld only the two files:
        `/etc/apt/preferences.d` always-syncs with no derivation predicate, so the source's
        ESM pins would land on a target without the sources they name.
        """
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_UNATTACHED, ""), gate_answers=[None])

        with pytest.raises(JobSkipped) as excinfo:
            await job.execute()

        assert excinfo.value.job_name == "apt_sync"
        assert _ESM_APPS in excinfo.value.reason
        assert _ESM_INFRA in excinfo.value.reason
        assert "no TTY" in excinfo.value.reason
        assert _mutating_calls(target) == []
        assert reviewer.groups_seen is None

    @pytest.mark.asyncio
    async def test_attach_now_re_probes_and_continues_when_the_target_became_attached(self) -> None:
        job, target, _reviewer = _esm_job(
            pro_status=[CommandResult(0, _PRO_UNATTACHED, ""), CommandResult(0, _PRO_ATTACHED, "")],
            gate_answers=[True],
        )

        await job.execute()

        assert _pro_probe_count(target) == 2, "the answer is re-checked against the machine, never trusted"
        promoted = _promoted_files(target)
        assert f"/etc/apt/sources.list.d/{_ESM_APPS}" in promoted
        assert f"/etc/apt/sources.list.d/{_ESM_INFRA}" in promoted

    @pytest.mark.asyncio
    async def test_attach_now_can_be_answered_any_number_of_times(self) -> None:
        """Unbounded by the user's ruling: re-probing costs nothing and the exit is skip."""
        attempts = 10
        job, target, reviewer = _esm_job(
            pro_status=CommandResult(0, _PRO_UNATTACHED, ""),
            gate_answers=[*([True] * attempts), False],
        )

        with pytest.raises(JobSkipped):
            await job.execute()

        assert reviewer.gate_answers == [], "every answer must be consumed — no bound may cut the loop short"
        assert len(reviewer.gate_calls) == attempts + 1
        assert _pro_probe_count(target) == attempts + 1

    @pytest.mark.asyncio
    async def test_esm_sources_are_written_to_an_attached_target(self, caplog: pytest.LogCaptureFixture) -> None:
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_ATTACHED, ""))

        with caplog.at_level(1):
            await job.execute()

        assert reviewer.gate_calls == []
        assert [r for r in caplog.records if r.levelno >= 30] == []
        assert _pro_probe_count(target) == 1
        promoted = _promoted_files(target)
        assert f"/etc/apt/sources.list.d/{_ESM_APPS}" in promoted
        assert f"/etc/apt/sources.list.d/{_ESM_INFRA}" in promoted

    @pytest.mark.asyncio
    async def test_a_source_with_no_esm_sources_never_probes_at_all(self) -> None:
        """The trigger is a pending write, not the target's Pro state: a source with no ESM
        files has nothing to gate, so the run costs no probe and asks no question.
        """
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_UNATTACHED, ""), source_esm=())

        await job.execute()

        assert _pro_probe_count(target) == 0
        assert reviewer.gate_calls == []

    @pytest.mark.asyncio
    async def test_an_esm_file_the_target_already_matches_is_not_gated(self) -> None:
        """Nothing to write is nothing to ask about. The target holds the same bytes, so the
        always-sync bucket skips the file and the gate must skip the question.
        """
        listing = sha256_line("d0", "ubuntu.sources") + sha256_line("e0", _ESM_APPS)

        def _target(cmd: str, **_: object) -> CommandResult:
            if cmd == PRO_STATUS_COMMAND:
                return CommandResult(0, _PRO_UNATTACHED, "")
            if "find /etc/apt/sources.list.d" in cmd:
                return CommandResult(0, listing, "")
            return CommandResult(0, "", "")

        context, _source, target = _repo_context(
            source_responses={**_NO_PACKAGES, "find /etc/apt/sources.list.d": CommandResult(0, listing, "")},
            target_side_effect=_target,
        )
        job = AptSyncJob(context)
        reviewer = _GateReviewer(target)
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert _pro_probe_count(target) == 0
        assert reviewer.gate_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "probe",
        [
            pytest.param(CommandResult(127, "", "pro: command not found\n"), id="no-pro-binary"),
            pytest.param(CommandResult(1, "", "Failed to access contract server\n"), id="non-zero-exit"),
            pytest.param(CommandResult(0, "attached: no\n", ""), id="not-json"),
            pytest.param(CommandResult(0, '["attached"]', ""), id="json-but-not-an-object"),
            pytest.param(CommandResult(0, '{"services": []}', ""), id="no-attached-key"),
        ],
    )
    async def test_an_unreadable_pro_probe_is_treated_as_unattached(self, probe: CommandResult) -> None:
        """False asks a question the user can answer; True writes files that break the
        target's next install. The recoverable answer is the default (ADR-022 D-01).
        """
        job, target, reviewer = _esm_job(pro_status=probe, gate_answers=[False])

        with pytest.raises(JobSkipped):
            await job.execute()

        assert len(reviewer.gate_calls) == 1
        assert _mutating_calls(target) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [_PRO_UNATTACHED, _PRO_ATTACHED])
    async def test_the_probe_payload_is_never_logged(self, payload: str, caplog: pytest.LogCaptureFixture) -> None:
        """`pro status` names the subscriber. Only the parsed boolean may leave the probe."""
        job, _target, reviewer = _esm_job(pro_status=CommandResult(0, payload, ""), gate_answers=[False])

        with caplog.at_level(1), contextlib.suppress(JobSkipped):
            await job.execute()

        assert _PRO_ACCOUNT_EMAIL not in caplog.text
        assert "aAbBcC" not in caplog.text
        for call in reviewer.gate_calls:
            assert _PRO_ACCOUNT_EMAIL not in "".join(call.values())

    @pytest.mark.asyncio
    async def test_a_dry_run_never_prompts_about_attachment(self, caplog: pytest.LogCaptureFixture) -> None:
        """A rehearsal must not make the user go and attach a machine, and ADR-014 makes the
        preview the whole report — so it has to say the real run would skip the job, not just
        that two files would be held back.
        """
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_UNATTACHED, ""), dry_run=True)

        with caplog.at_level(1):
            await job.execute()

        assert reviewer.gate_calls == []
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= 30]
        assert len(warnings) == 1
        assert _ESM_APPS in warnings[0]
        assert _ESM_INFRA in warnings[0]
        assert "skip apt_sync entirely" in warnings[0]
        assert _ESM_APPS not in "".join(
            r.getMessage() for r in caplog.records if "[dry-run] Would write" in r.getMessage()
        )
        assert _mutating_calls(target) == []
