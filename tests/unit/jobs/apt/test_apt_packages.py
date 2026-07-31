"""Converging one install, removal or hold, and the guard chain that may refuse it.

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs.apt_sync import AptSyncJob, simulate_apt_transaction
from pcswitcher.jobs.apt_sync.commands import TARGET_SUDO_COMMANDS
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import (
    Decision,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, LogLevel
from tests.unit.jobs.apt.helpers import (
    _APPROVE_PKG_A,
    _repo_context,
    all_calls,
    foo_source_responses,
    foo_target_side_effect,
    index_of,
    install_reviewer,
    installed_on_target,
    make_context,
    sha256_line,
    target_offers,
)


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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.SKIP_ONCE})

        await job.execute()

        # pkg-b legitimately appears in the plan-time BATCHED simulation command
        # (both pkg-a and pkg-b are missing-on-target candidates before any decision
        # exists) — the guarantee under test is that no REAL install command names it.
        commands = all_calls(target)
        real_installs = [c for c in commands if "sudo" in c and "apt-get install" in c]
        assert any("pkg-a" in cmd for cmd in real_installs)
        assert not any("pkg-b" in cmd for cmd in real_installs)


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
                return CommandResult(0, target_offers("pkg-a"), "")
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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

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
        install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})

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

        install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})
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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        install_idx = index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
        hold_idx = index_of(commands, lambda c: c == "sudo apt-mark hold pkg-a")
        assert install_idx < hold_idx

    @pytest.mark.asyncio
    async def test_hold_follows_install_on_the_accept_review_reorder_path(self) -> None:
        """A derived `/etc/apt` write makes `accept_review` rebuild the plan around the
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
        install_reviewer(job, {"apt:package:pkg-good": Decision.APPLY, "apt:hold:ghost-pkg": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert [diff.item_id for diff, _ in exc_info.value.failures] == ["apt:hold:ghost-pkg"]
        # The unrelated item in the same run still converged.
        assert any(
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-good" in c for c in all_calls(target)
        )


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
        """Never a fallback to the target's own version: that is the outcome the hold would
        then make permanent, so the item fails and says which two versions are in play.
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
        assert not any(cmd == _PINNED_INSTALL for cmd in all_calls(target))


class TestAStaleTargetHoldDoesNotStrandThePackage:
    """`PKG-FR-APT-HOLD-VERSION`: `apt-mark hold` records a hold for a package the machine
    merely does not have. Suppressing on the hold set alone meant such a name produced no
    install and — with both machines holding it — no hold item either, so the target stayed
    without the package for good. apt refuses the install while the selection stands, so the
    run clears it first and re-registers the hold once the package lands.
    """

    @pytest.mark.asyncio
    async def test_the_stale_hold_is_cleared_the_package_installed_and_the_hold_restored(self) -> None:
        context, _source, target = make_context(
            source_responses=_HELD_SOURCE,
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
                "db:Status-Status": installed_on_target("other-pkg"),
                "apt-cache policy": CommandResult(0, _target_offering("1.0"), ""),
                _PINNED_SIMULATION: CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                _PINNED_INSTALL: CommandResult(0, "", ""),
                "sudo apt-mark unhold pkg-a": CommandResult(0, "Canceled hold on pkg-a.\n", ""),
                "sudo apt-mark hold pkg-a": CommandResult(0, "pkg-a set on hold.\n", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:hold:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        unhold = index_of(commands, lambda cmd: cmd == "sudo apt-mark unhold pkg-a")
        install = index_of(commands, lambda cmd: cmd == _PINNED_INSTALL)
        hold = index_of(commands, lambda cmd: cmd == "sudo apt-mark hold pkg-a")
        assert unhold < install < hold
        # The simulation is refused on the same grounds as the install, so it too comes
        # after the selection is cleared.
        assert unhold < index_of(commands, lambda cmd: cmd == _PINNED_SIMULATION)

    @pytest.mark.asyncio
    async def test_a_hold_on_a_package_the_target_has_still_suppresses_its_install(self) -> None:
        """`PKG-FR-APT-HELD-TARGET` is untouched: a real hold — one naming a package the
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
        """The user declined the install, so the hold that pins it is declined too: nothing
        broke, and a hold has no version to pin on a package nobody installed.
        """
        context, _source, target = make_context(source_responses=_HELD_SOURCE, target_responses=self._target())
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:hold:pkg-a": Decision.APPLY})

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        assert not any("apt-mark hold" in cmd for cmd in all_calls(target))
        assert any(
            "not applied" in record.message and "its install was not approved" in record.message
            for record in caplog.records
        )
        assert not any(record.levelno >= LogLevel.ERROR.value for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_hold_whose_install_failed_fails_too(self) -> None:
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
        """The plan-time collateral answer reaches the hold as an ordinary unapproved
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
    async def test_a_hold_on_a_package_the_target_already_has_still_runs(self) -> None:
        """No install item at all: the package is on the target and the hold is the whole
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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

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
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

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
                return CommandResult(0, target_offers("pkg-a"), "")
            return static.get(cmd, CommandResult(0, "", ""))

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nmanual-dg\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nmanual-dg\t2.0\n", ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

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
