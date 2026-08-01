"""Converging one install, removal or hold, and the guard chain that may refuse it.

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs.apt_sync import AptSyncJob, simulate_apt_transaction
from pcswitcher.jobs.apt_sync.commands import TARGET_SUDO_COMMANDS
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    Decision,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, LogLevel
from tests.unit.jobs.apt.helpers import (
    _APPROVE_PKG_A,
    CountingReviewer,
    _policy_block,
    _repo_context,
    all_calls,
    decision_file,
    foo_source_responses,
    foo_target_side_effect,
    index_of,
    install_reviewer,
    installed_on_target,
    make_context,
    real_installs,
    respond_with_policy_sequence,
    sha256_line,
    target_offers,
)


class TestConverge:
    """Only APPLY-decided items reach the target; SKIP_ONCE items reach no command."""

    @pytest.mark.asyncio
    async def test_only_apply_decision_installs_skip_once_never_sent(self) -> None:
        """A58 — the approved package installs by NAME, verbatim and with no `=<version>`:
        neither machine holds it, so the target's own repositories decide which version it
        gets. The unticked one reaches no real command.
        """
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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.SKIP_ONCE})

        await job.execute()

        # pkg-b legitimately appears in the plan-time BATCHED simulation command
        # (both pkg-a and pkg-b are missing-on-target candidates before any decision
        # exists) — the guarantee under test is that no REAL install command names it.
        assert real_installs(target) == [
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a"
        ]


def _drifted_install_context(
    *, drifted: str, extra: dict[str, CommandResult] | None = None
) -> tuple[JobContext, MagicMock, MagicMock]:
    """A run whose ONE approved install rehearses clean at plan time and, when the converger
    simulates it again moments before the command, reports `drifted` as well.

    The drifting package is manual on the target and identical on the source, so no review
    saw it and nothing in the plan could have: the second simulation is the first statement
    of the fact.
    """
    sim_cmd = "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a"
    state = {"sim": 0}
    static = {
        "apt-mark showmanual": CommandResult(0, "manual-x\n", ""),
        **(extra or {}),
    }

    def target_side_effect(cmd: str, **_: object) -> CommandResult:
        if cmd == sim_cmd:
            state["sim"] += 1
            return CommandResult(0, "Inst pkg-a (1.0)\n" + (drifted if state["sim"] > 1 else ""), "")
        if "dpkg-query" in cmd:
            return CommandResult(0, "manual-x\t1.0\n", "")
        if cmd.startswith("apt-cache policy"):
            return CommandResult(0, target_offers("pkg-a"), "")
        for pattern, result in static.items():
            if pattern in cmd:
                return result
        return CommandResult(0, "", "")

    context, source, target = make_context(
        source_responses={
            "apt-mark showmanual": CommandResult(0, "pkg-a\nmanual-x\n", ""),
            "dpkg-query": CommandResult(0, "pkg-a\t1.0\nmanual-x\t1.0\n", ""),
        },
    )
    target.run_command = AsyncMock(side_effect=target_side_effect)
    return context, source, target


def _collateral_entries(reviewer: CountingReviewer) -> set[str]:
    """Every collateral item id the reviewer was asked about, across all its rounds."""
    return {
        entry.item_id
        for groups in reviewer.calls
        for group in groups
        if group.action == COLLATERAL_REVIEW_ACTION
        for entry in group.entries
    }


class TestTheDriftedTransactionIsAskedAbout:
    """`PKG-FR-COLLATERAL-MANUAL` with `PKG-FR-ASK-AGAIN`: a protected package the REAL
    transaction would take that no review saw is a fact younger than the review, so the three
    answers are put immediately before the command rather than replaced by a refusal.

    Keeping the package leaves the change unapplied and unfailed, which is the article's own
    remedy — the same outcome the plan-time and repository-late questions produce.
    """

    @pytest.mark.asyncio
    async def test_a_drifted_manual_removal_is_asked_about_and_keeping_it_withdraws_the_install(self) -> None:
        """D39 — the install's transaction drifts onto `manual-x`; the user is asked, keeps
        it, and the install neither runs nor fails."""
        context, _source, target = _drifted_install_context(drifted="Remv manual-x [1.0]\n")
        job = AptSyncJob(context)
        reviewer = CountingReviewer({"apt:package:pkg-a": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert "apt:collateral:install:remove:manual-x" in _collateral_entries(reviewer)
        assert real_installs(target) == []

    @pytest.mark.asyncio
    async def test_going_ahead_at_that_question_runs_the_install(self) -> None:
        """The three answers are real ones: letting the consequence go ahead installs the
        package the question was about, rather than the guard refusing it anyway."""
        context, _source, target = _drifted_install_context(drifted="Remv manual-x [1.0]\n")
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:install:remove:manual-x": Decision.APPLY},
        )

        await job.execute()

        assert len(real_installs(target)) == 1

    @pytest.mark.asyncio
    async def test_a_drifted_downgrade_is_asked_about_too(self) -> None:
        """D41 — a version the transaction would move a protected package BACK to is the
        same question a removal is."""
        context, _source, target = _drifted_install_context(
            drifted="Inst manual-x [1.0] (0.9)\n",
            extra={"dpkg --compare-versions 0.9 lt 1.0": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)
        reviewer = CountingReviewer({"apt:package:pkg-a": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert "apt:collateral:install:downgrade:manual-x" in _collateral_entries(reviewer)
        assert real_installs(target) == []

    @pytest.mark.asyncio
    async def test_a_drifted_upgrade_is_asked_about_too(self) -> None:
        """D71 — an unasked-for upgrade moves a package the user chose off the version it was
        on, which is the imposition a downgrade is; the apply-time question covers it as the
        plan-time one does."""
        context, _source, target = _drifted_install_context(
            drifted="Inst manual-x [1.0] (2.0)\n",
            extra={
                "dpkg --compare-versions 2.0 lt 1.0": CommandResult(1, "", ""),
                "dpkg --compare-versions 2.0 gt 1.0": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        reviewer = CountingReviewer({"apt:package:pkg-a": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert "apt:collateral:install:upgrade:manual-x" in _collateral_entries(reviewer)
        assert real_installs(target) == []


class TestTransactionGuard:
    @pytest.mark.asyncio
    async def test_install_whose_only_collateral_is_auto_deps_proceeds(self) -> None:
        """D2 — The D-30 win, at the guard: an install whose simulation removes only
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
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv auto-dep [1.0]\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

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
                return CommandResult(0, target_offers("pkg-a"), "")
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "dpkg was interrupted" in message

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)


class TestAptHold:
    """#208: hold replication — `apt:hold:` membership items, converge via `apt-mark`, a
    held package never double-reported, and sudo scope."""

    @pytest.mark.asyncio
    async def test_source_held_yields_install_hold_item_and_converge_runs_apt_mark_hold(self) -> None:
        """B1, B30 — a package held on the source but not the target produces an
        `apt:hold:` INSTALL item; approving it converges via `sudo apt-mark hold` and
        nothing else."""
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
        install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any(cmd == "sudo apt-mark hold pkg-a" for cmd in commands)
        assert not any("apt-get install" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_a_hold_whose_package_is_no_item_this_run_still_asks_nothing(self) -> None:
        """B53 — `pkg-a` is on both machines at the same version, so it is no item at all.
        Its hold is still nobody's question: it is derived either way.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
            },
        )
        plan = await AptSyncJob(context).plan()

        assert [diff.item_id for diff in plan.diffs] == ["apt:hold:pkg-a"]
        assert plan.groups == ()

    @pytest.mark.asyncio
    async def test_target_held_only_yields_remove_unhold_item(self) -> None:
        """B2 — a package the target holds and the source does not is an unhold item;
        approving it clears the hold on the target."""
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

        install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})
        await job.execute()
        assert any(cmd == "sudo apt-mark unhold pkg-a" for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_held_on_both_yields_no_hold_diff(self) -> None:
        """B3 — both machines hold it: no hold item."""
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
        """B11, B15 — a target-held package produces the `apt:hold:` item and NOT a
        package-level report for the same name (#208 dedup), even with the versions
        differing."""
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
    async def test_a_forced_permanent_answer_on_a_hold_records_nothing(self) -> None:
        """B10 — `PKG-FR-BLOCKS-DERIVED`: no screen offers the permanent answer for a hold,
        but the automation hook and a hand-built outcome can still carry one, and no decision
        file may come of it.
        """
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

        for machine in (source, _target):
            assert not any("mv --force" in cmd and "apt.decisions" in cmd for cmd in all_calls(machine))

    def test_apt_mark_is_in_the_target_sudo_command_list(self) -> None:
        assert "/usr/bin/apt-mark" in TARGET_SUDO_COMMANDS


class TestInstallBeforeHoldOrdering:
    """#208 D8: a package missing on the target and held on the source converges its
    `apt-mark hold` AFTER its `apt-get install` — dpkg selection state for a package that
    is not there yet is not a state apt can set. Both ordering code paths are covered:
    `plan()`'s `_ITEM_CLASS_ORDER` sort, and `accept_review`'s marker-insertion rebuild.
    """

    @pytest.mark.asyncio
    async def test_hold_follows_install_on_the_plain_plan_sort_path(self) -> None:
        """B38 — a repo diff exists (so `plan()` runs its `_ITEM_CLASS_ORDER` sort) but is left
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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        install_idx = index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
        hold_idx = index_of(commands, lambda c: c == "sudo apt-mark hold pkg-a")
        assert install_idx < hold_idx

    @pytest.mark.asyncio
    async def test_hold_follows_install_on_the_accept_review_reorder_path(self) -> None:
        """B39 — a derived `/etc/apt` write makes `accept_review` rebuild the plan around the
        metadata-refresh marker (repo items, marker, packages, holds) — the hold must stay
        behind its package install through that rebuild too.
        """
        context, _source, target = _repo_context(
            source_responses=foo_source_responses(**{"apt-mark showhold": CommandResult(0, "pkg-a\n", "")})
        )
        target.run_command = AsyncMock(
            side_effect=foo_target_side_effect(
                {
                    "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                    "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
                }
            )
        )
        job = AptSyncJob(context)
        install_reviewer(job, {**_APPROVE_PKG_A, "apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        key_idx = index_of(commands, lambda c: "sudo install" in c and "keyrings/foo.gpg" in c)
        update_idx = index_of(commands, lambda c: c == "sudo apt-get update")
        install_idx = index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
        hold_idx = index_of(commands, lambda c: c == "sudo apt-mark hold pkg-a")
        assert key_idx < update_idx < install_idx < hold_idx


class TestAFailedHoldCommand:
    """#208 D6: an `apt-mark` that exits non-zero is a normal per-item failure (D-27
    continue-and-report) — no gating machinery, no crash, no aborted run.
    """

    @pytest.mark.asyncio
    async def test_failed_apt_mark_hold_fails_only_that_item(self) -> None:
        """B9 — `apt-mark` refusing a hold fails that item alone; every other approved item
        in the run still converges."""
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-good\nfrozen\n", ""),
                "dpkg-query": CommandResult(0, "pkg-good\t1.0\nfrozen\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "frozen\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "frozen\n", ""),
                "dpkg-query": CommandResult(0, "frozen\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-good": CommandResult(
                    0, "Inst pkg-good (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-good": (
                    CommandResult(0, "", "")
                ),
                "sudo apt-mark hold frozen": CommandResult(1, "", "E: dpkg selection could not be written"),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-good": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert [diff.item_id for diff, _ in exc_info.value.failures] == ["apt:hold:frozen"]
        # The unrelated item in the same run still converged.
        assert any(
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-good" in c for c in all_calls(target)
        )


_PKG_A = "apt:package:pkg-a"
_HELD_SOURCE = {
    "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
    "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
    "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
}
_PINNED_SIMULATION = "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a=1.0"
_PINNED_INSTALL = "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a=1.0"


def _target_offering(candidate: str) -> str:
    """`apt-cache policy pkg-a` on a target that lacks the package and offers `candidate`."""
    return (
        f"pkg-a:\n  Installed: (none)\n  Candidate: {candidate}\n  Version table:\n"
        f"     {candidate} 500\n        500 http://ftp.belnet.be/ubuntu stable/main amd64 Packages\n"
    )


class TestAHeldPackageIsInstalledAtTheSourcesVersion:
    """`PKG-FR-APT-HOLD-VERSION`: apt offers no way to say "hold this, but at whatever you
    have", so a hold replicated onto a package installed at the target's own version freezes
    the two machines apart permanently — nothing moves a held package again.
    """

    @pytest.mark.asyncio
    async def test_the_install_names_the_sources_version(self) -> None:
        """B22, B26 — the install asks for the source's version by name, and the hold that
        follows lands on the target."""
        context, _source, target = make_context(
            source_responses=_HELD_SOURCE,
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _target_offering("1.0"), ""),
                _PINNED_SIMULATION: CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                _PINNED_INSTALL: CommandResult(0, "", ""),
                "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any(cmd == _PINNED_INSTALL for cmd in commands)
        assert "sudo apt-mark hold pkg-a" in commands

    @pytest.mark.asyncio
    async def test_a_version_the_target_cannot_supply_fails_naming_both(self) -> None:
        """B23, B24 — never a fallback to the target's own version: that is the outcome the
        hold would then make permanent, so the item fails and says which two versions are in
        play, and no install of `pkg-a` runs at any version at all.
        """
        context, _source, target = make_context(
            source_responses=_HELD_SOURCE,
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _target_offering("2.0"), ""),
                _PINNED_SIMULATION: CommandResult(100, "", "E: Version '1.0' for 'pkg-a' was not found"),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert "source-host holds it at 1.0" in failures["apt:package:pkg-a"]
        assert "target-host offers 2.0" in failures["apt:package:pkg-a"]
        # Not just the pinned command's absence: a fallback to an unpinned
        # `apt-get install pkg-a` is the specific outcome the hold would make permanent.
        assert not any("apt-get install" in cmd and "pkg-a" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_target_offering_no_candidate_says_so_rather_than_naming_a_version(self) -> None:
        """B25 — the other arm of the same refusal: the target's apt names no version it
        would install, so there is no second version to put beside the source's.

        The two policy reads are of two different `/etc/apt` states — the run's own
        `apt-get update` falls between the plan-time read and this one — so a fixture
        answering both identically could not reach this branch at all.
        """
        context, _source, target = make_context(source_responses=_HELD_SOURCE)
        target.run_command = AsyncMock(
            side_effect=respond_with_policy_sequence(
                {
                    "apt-mark showmanual": CommandResult(0, "", ""),
                    "apt-mark showhold": CommandResult(0, "", ""),
                    _PINNED_SIMULATION: CommandResult(100, "", "E: Version '1.0' for 'pkg-a' was not found"),
                    "sudo apt-get update": CommandResult(0, "", ""),
                },
                [
                    CommandResult(0, _target_offering("2.0"), ""),
                    CommandResult(0, "pkg-a:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n", ""),
                ],
            )
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert "source-host holds it at 1.0" in failures["apt:package:pkg-a"]
        assert "target-host offers no other" in failures["apt:package:pkg-a"]

    @staticmethod
    def _target_lacking_pkg_a() -> dict[str, CommandResult]:
        return {
            "apt-mark showmanual": CommandResult(0, "", ""),
            "apt-mark showhold": CommandResult(0, "", ""),
            "apt-cache policy": CommandResult(0, _target_offering("1.0"), ""),
            _PINNED_SIMULATION: CommandResult(0, "Inst pkg-a (1.0)\n", ""),
            _PINNED_INSTALL: CommandResult(0, "", ""),
            "sudo apt-get update": CommandResult(0, "", ""),
        }

    @pytest.mark.asyncio
    async def test_the_install_is_the_one_question_and_the_hold_follows_it(self) -> None:
        """B7, B52 — the hold follows the package it applies to, so there is no way to approve
        the install and decline the hold: one row, the install, and the hold lands after it at
        the source's version.
        """
        context, _source, target = make_context(
            source_responses=_HELD_SOURCE, target_responses=self._target_lacking_pkg_a()
        )
        job = AptSyncJob(context)
        plan = await job.plan()

        entries = {entry.item_id for group in plan.groups for entry in group.entries}
        assert entries == {"apt:package:pkg-a"}

        job.accept_review(plan, ReviewOutcome(decisions={"apt:package:pkg-a": Decision.APPLY}, was_interactive=True))
        await job.apply()

        commands = all_calls(target)
        assert _PINNED_INSTALL in commands
        assert index_of(commands, lambda c: c == _PINNED_INSTALL) < index_of(
            commands, lambda c: "apt-mark hold pkg-a" in c
        )

    @pytest.mark.asyncio
    async def test_declining_the_one_question_leaves_neither_the_package_nor_the_hold(self) -> None:
        """B54 — a skip-once on the install is a skip of its hold too: nothing is installed,
        nothing is held, and nothing is recorded on either machine.
        """
        context, source, target = make_context(
            source_responses=_HELD_SOURCE, target_responses=self._target_lacking_pkg_a()
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        for machine in (source, target):
            assert not any(".decisions.yaml" in cmd and "printf" in cmd for cmd in all_calls(machine))
        commands = all_calls(target)
        assert not any("apt-mark hold" in cmd for cmd in commands)
        assert not any("apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)


class TestAMarkOnThePackageCarriesItsHold:
    """`PKG-FR-BLOCKS-DERIVED` with `PKG-FR-MACHINE-SPECIFIC`: a block follows the software
    it applies to, and a mark on the package is the user's own answer about that software.
    A hold for a package the mark keeps off the target would freeze nothing and block every
    later install of the name.
    """

    @pytest.mark.asyncio
    async def test_a_hold_whose_package_the_source_marked_travels_no_further_than_the_package(self) -> None:
        """B27 — the package does not travel, so neither does its hold: no item, and no
        `apt-mark hold` for a package the target does not have."""
        context, _source, target = make_context(
            source_responses={**_HELD_SOURCE, "apt.decisions.yaml": CommandResult(0, decision_file(_PKG_A), "")},
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _target_offering("1.0"), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {_PKG_A: Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        assert not any("apt-mark hold" in cmd for cmd in all_calls(target))
        assert not any("apt-get install" in cmd and "pkg-a" in cmd for cmd in all_calls(target))


class TestAHeldPackageWithNoCapturedVersion:
    """`PKG-FR-APT-HOLD-VERSION` forbids the fallback, so the converger must have no way to
    reach one — including from a capture that answered nothing about the version.

    The state is unreachable through the shipped probe: `AptProbe._resolve_versions` fails the
    job on a non-zero `dpkg-query`, and every name it queries comes from `apt-mark showmanual`
    and is therefore installed. This pins the refusal so a future capture that does return an
    empty version cannot float the install onto whatever the target offers instead.
    """

    @pytest.mark.asyncio
    async def test_a_held_package_with_no_captured_version_is_refused_rather_than_floated(self) -> None:
        """B28 — the pin is not silently dropped: the install fails as its own item and no
        `apt-get install` names the package at any version."""
        context, _source, target = make_context(
            source_responses={**_HELD_SOURCE, "dpkg-query": CommandResult(0, "\n", "")},
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _target_offering("2.0"), ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {_PKG_A: Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert "no installed version was captured" in failures[_PKG_A]
        assert not any("apt-get install" in cmd and "pkg-a" in cmd for cmd in all_calls(target))


class TestAReplicatedHoldMovesNoVersion:
    """`PKG-FR-APT-HOLD-INERT`: replicating a hold changes no version. The two machines end
    up held at different versions, which is the honest outcome — converging them would mean
    an upgrade or a downgrade nobody asked for, and the hold is what says not to.
    """

    @pytest.mark.asyncio
    async def test_a_hold_replicated_onto_a_differing_version_installs_nothing(self) -> None:
        """B29 — the source holds `pkg-a` at 1.0 and the target has 2.0 unheld: the version
        difference is reported, the hold is registered, and no transaction runs.
        """
        context, _source, target = make_context(
            source_responses=_HELD_SOURCE,
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t2.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()
        by_id = {diff.item_id: diff for diff in plan.diffs}
        assert by_id["apt:package:pkg-a"].diff_class == DiffClass.VERSION_MISMATCH
        assert by_id["apt:package:pkg-a"].action == DiffAction.REPORT_ONLY
        assert by_id["apt:hold:pkg-a"].action == DiffAction.INSTALL

        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})
        await job.execute()

        commands = all_calls(target)
        assert "sudo apt-mark hold pkg-a" in commands
        assert not any("apt-get install" in cmd or "apt-get remove" in cmd for cmd in commands)


class TestAHeldPackageOnTheTargetIsNoItem:
    """`PKG-FR-APT-HELD-TARGET`: apt refuses to move a held package, so a package the target
    has and holds produces no package-level item of any kind.
    """

    @pytest.mark.asyncio
    async def test_a_hold_on_a_package_the_target_has_still_suppresses_its_install(self) -> None:
        """B12 — `PKG-FR-APT-HELD-TARGET` is untouched: a real hold — one naming a package the
        target HAS — keeps suppressing the package item, including for a package apt
        installed there automatically and so absent from its manual set.
        """
        context, _source, target = make_context(
            source_responses=_HELD_SOURCE,
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
                "db:Status-Status": installed_on_target("pkg-a"),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert [diff.item_id for diff in plan.diffs] == []
        assert not any("apt-mark unhold" in cmd for cmd in all_calls(target))


class TestAHoldNeedsItsPackage:
    """`PKG-FR-APT-HOLD-INERT`: measured on `ubuntu:24.04`, `apt-mark hold` exits 0 and
    records the hold for a package that is merely NOT INSTALLED — it refuses only a name apt
    has never heard of. So the guard is this job's, not apt's, and no hold is registered for
    a package this run did not put on the target.

    Which outcome that is depends on WHY the install did not happen: an install the user
    declined declines its hold, an install that broke fails it.
    """

    @staticmethod
    def _target(**overrides: CommandResult) -> dict[str, CommandResult]:
        return {
            "apt-mark showmanual": CommandResult(0, "", ""),
            "apt-mark showhold": CommandResult(0, "", ""),
            "apt-cache policy": CommandResult(0, _target_offering("1.0"), ""),
            _PINNED_SIMULATION: CommandResult(0, "Inst pkg-a (1.0)\n", ""),
            "sudo apt-get update": CommandResult(0, "", ""),
            **overrides,
        }

    @pytest.mark.asyncio
    async def test_a_hold_whose_install_was_skipped_is_declined_not_failed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """B31, B37 — declining the install declines its hold with it: the hold is logged as
        not applied, not as something that broke.
        """
        context, _source, target = make_context(source_responses=_HELD_SOURCE, target_responses=self._target())
        job = AptSyncJob(context)
        install_reviewer(job, {})

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        assert not any("apt-mark hold" in cmd for cmd in all_calls(target))
        assert any("pkg-a (hold)" in record.message and "not applied" in record.message for record in caplog.records)
        assert not any(record.levelno >= LogLevel.ERROR.value for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_hold_whose_install_failed_fails_too(self) -> None:
        """B34 — an approved install that broke fails its hold with it, both named."""
        context, _source, target = make_context(
            source_responses=_HELD_SOURCE,
            target_responses=self._target(**{_PINNED_INSTALL: CommandResult(100, "", "E: dpkg was interrupted")}),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a", "apt:hold:pkg-a"}
        assert "its install failed" in failures["apt:hold:pkg-a"]
        assert not any("apt-mark hold" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_hold_whose_install_a_collateral_answer_cancelled_is_declined_too(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """B32 — the plan-time collateral answer reaches the hold as an ordinary unapproved
        install: `Collateral.resolve` rewrote `pkg-a` to skip-once because keeping
        `other-manual` cancels the transaction that would take it.

        The same answer given late — after `/etc/apt` has converged — already declined its
        hold, and which side of that line a run falls on is decided by nothing but whether
        the repository happened to be on the target already.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nother-manual\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-a": Decision.APPLY,
                "apt:hold:pkg-a": Decision.APPLY,
                "apt:collateral:install:remove:other-manual": Decision.SKIP_ONCE,
            },
        )

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        commands = all_calls(target)
        assert not any("apt-mark hold" in cmd for cmd in commands)
        assert not any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in cmd for cmd in commands)
        assert not any(record.levelno >= LogLevel.ERROR.value for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_hold_whose_install_a_late_collateral_answer_withdrew_is_declined(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """B33 — the same answer as the plan-time case, but only answerable after `/etc/apt`
        has converged: `pkg-a` comes from a repository this run writes, so the target's apt
        cannot say what installing it would take until the file and the refresh have landed.

        Which side of that line a run falls on is decided by nothing the user can see, so the
        outcome must be the same: the hold is declined, named as declined, and nothing about
        the run is a failure.
        """
        context, _source, target = _repo_context(
            source_responses=foo_source_responses(
                **{
                    "apt-mark showmanual": CommandResult(0, "pkg-a\nother-manual\n", ""),
                    "dpkg-query": CommandResult(0, "pkg-a\t1.0\nother-manual\t1.0\n", ""),
                    "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
                }
            )
        )
        target.run_command = AsyncMock(
            side_effect=foo_target_side_effect(
                {
                    "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                    "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                    "apt-mark showhold": CommandResult(0, "", ""),
                    "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""),
                }
            )
        )
        job = AptSyncJob(context)
        # The late collateral entry is unlisted, so it defaults to SKIP_ONCE: keep
        # `other-manual` on the target, which withdraws `pkg-a`'s install.
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        commands = all_calls(target)
        assert not any("apt-mark hold" in cmd for cmd in commands)
        assert real_installs(target) == []
        assert any(
            "hold on pkg-a not applied" in record.message and "its install was withdrawn" in record.message
            for record in caplog.records
        )
        assert not any(record.levelno >= LogLevel.ERROR.value for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_hold_on_a_package_no_repository_can_supply_fails_alone(self) -> None:
        """B35 — nobody declined this one: the source has `pkg-a` from a repository no file
        on the source declares any more, so the package is REPORTED rather than installed and
        the hold has nothing to freeze. That is a finding about the two machines, not an
        answer, so the hold FAILS — and it fails alone, the package itself being report-only.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://gone.example.com/apt"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()
        by_id = {diff.item_id: diff for diff in plan.diffs}
        assert by_id["apt:package:pkg-a"].diff_class == DiffClass.REPO_UNAVAILABLE

        install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})
        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:hold:pkg-a"}
        assert "not on target-host" in failures["apt:hold:pkg-a"]
        assert "cannot reproduce the repository" in failures["apt:hold:pkg-a"]
        assert not any("apt-mark hold" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_hold_on_a_package_the_target_already_has_still_runs(self) -> None:
        """B36 — no install item at all: the package is on the target and the hold is the whole
        change, which is the ordinary case this guard must not touch.
        """
        context, _source, target = make_context(
            source_responses=_HELD_SOURCE,
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
                "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        assert "sudo apt-mark hold pkg-a" in all_calls(target)


class TestHoldsDriveNoSimulation:
    """#208 D4: a hold is dpkg selection state, not an apt transaction — so it drives no
    `apt-get --dry-run` preview at plan time and none at converge time.
    """

    @pytest.mark.asyncio
    async def test_hold_only_run_issues_zero_apt_get_simulations(self) -> None:
        """B8, B30 — a hold is selection state, so a hold-only run rehearses no
        transaction and issues nothing but `apt-mark`."""
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

        install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})
        await job.execute()

        commands = all_calls(target)
        assert any(c == "sudo apt-mark hold pkg-a" for c in commands)
        assert not any("apt-get --dry-run" in c for c in commands)


class TestRemovalConverge:
    @pytest.mark.asyncio
    async def test_remove_diff_issues_real_apt_get_remove_for_that_package_alone(self) -> None:
        """A52, A53 — one `apt-get remove` naming that package and nothing else, and it
        removes without purging: `apt-get purge` would delete the machine's own
        configuration for the package, which nothing the source said asks for.
        """
        context, _source, target = make_context(
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-extra\n", ""),
                "dpkg-query": CommandResult(0, "pkg-extra\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-extra": CommandResult(0, "Remv pkg-extra [1.0]\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes pkg-extra": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-extra": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        real_removals = [cmd for cmd in commands if "sudo" in cmd and "apt-get remove" in cmd]
        assert len(real_removals) == 1
        assert "pkg-extra" in real_removals[0]
        assert not any("apt-get install" in cmd for cmd in commands)
        assert not any("purge" in cmd for cmd in commands)


class TestRemovalGuard:
    """Auto reverse-deps proceed (D-30); an unapproved manual removal is still refused."""

    @pytest.mark.asyncio
    async def test_auto_reverse_dep_removal_proceeds(self) -> None:
        """D4 — Removing a package legitimately removes the auto-installed dependencies apt
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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get remove" in cmd and "pkg-a" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_a_drifted_manual_reverse_dep_removal_is_asked_about(self) -> None:
        """D40 — a removal whose real transaction drifted to also remove a manually-installed
        package nobody reviewed gets the same three answers the install direction does
        (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-ASK-AGAIN`). `manual-b` is manual on both
        machines and matches, so it is not a diff and not a removal candidate: the plan-time
        simulation is clean and nothing could have asked earlier. Keeping it leaves the
        removal unapplied and unfailed.
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
        reviewer = CountingReviewer({"apt:package:pkg-a": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert "apt:collateral:remove:remove:manual-b" in _collateral_entries(reviewer)
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_both_removals_approved_the_first_proceeds(self) -> None:
        """A55, D36 — both candidates approved, so the first one's cascade over the second
        is not an unapproved casualty and it proceeds."""
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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        real_removals = [cmd for cmd in commands if "sudo" in cmd and "apt-get remove" in cmd]
        assert any("pkg-a" in cmd for cmd in real_removals)
        assert any("pkg-b" in cmd for cmd in real_removals)


class TestDowngradeGuard:
    @pytest.mark.asyncio
    async def test_guard_allows_auto_downgrade(self) -> None:
        """D7 — An auto-installed package the simulation would downgrade proceeds silently —
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
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst auto-dg [2.0] (1.0)\n", ""
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)
        # auto-dg is not manual, so no version comparison is issued for it.
        assert not any("dpkg --compare-versions" in cmd for cmd in commands)


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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        # Exactly one refresh, even though two packages install (idempotent guard).
        assert sum(1 for c in commands if c == "sudo apt-get update") == 1
        update_idx = index_of(commands, lambda c: c == "sudo apt-get update")
        first_install_idx = index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

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
        install_reviewer(job, {"apt:key:per-repo:foo.gpg": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert sum(1 for c in commands if c == "sudo apt-get update") == 1
        update_idx = index_of(commands, lambda c: c == "sudo apt-get update")
        install_idx = index_of(
            commands,
            lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c,
        )
        assert update_idx < install_idx
