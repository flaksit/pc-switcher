"""Unit tests for SnapSyncJob: header-based `snap list --all` parsing, the snap-specific
plan()/diff pipeline, revision+channel convergence, and the D-06 no-hold guarantee.

All executor interactions are mocked; no real snap/snapd commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.packages.items import DiffAction, DiffClass
from pcswitcher.jobs.packages.review import (
    Decision,
    ReviewGroup,
    ReviewOutcome,
    _is_removal_direction,  # pyright: ignore[reportPrivateUsage]
)
from pcswitcher.jobs.packages.sync_core import PackageItemFailures, PackagePlan
from pcswitcher.jobs.snap_sync import SnapSyncJob, snap_sync_exclude_paths
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator

# `Name Version Rev Tracking Publisher Notes` matches the live layout RESEARCH.md
# verified against real snapd 2.76.1 output.
_HEADER = "Name      Version    Rev    Tracking        Publisher    Notes\n"

SNAP_LIST_SOURCE = (
    _HEADER
    + "alpha     1.0        10     latest/stable   pub✓         -\n"
    + "beta      2.0        20     latest/stable   pub✓         -\n"
    + "gamma     3.0        30     latest/edge     pub✓         -\n"
)

SNAP_LIST_TARGET = (
    _HEADER
    + "beta      1.5        15     latest/stable   pub✓         -\n"
    + "gamma     3.0        30     latest/stable   pub✓         -\n"
    + "delta     4.0        40     latest/stable   pub✓         -\n"
)

SNAP_LIST_WITH_DISABLED_REVISION = (
    _HEADER
    + "firefox   118.0      2938   latest/stable   pub✓         -\n"
    + "firefox   117.0      2911   latest/stable   pub✓         disabled\n"
)

# Same rows as SNAP_LIST_SOURCE's `alpha` line, but header AND body columns swapped
# (Notes/Tracking/Name/Rev/Publisher) to prove parsing is header-driven, not positional.
SNAP_LIST_COLUMN_REORDERED = (
    "Notes    Tracking        Name      Rev    Publisher\n" + "-        latest/stable   alpha     10     pub✓\n"
)


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
    """Header-based capture (RESEARCH Open Question 2): parses by column NAME, never
    fixed offsets or assumed order.
    """

    @pytest.mark.asyncio
    async def test_capture_source_items_parses_name_rev_tracking_by_header(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert [item.name for item in items] == ["alpha", "beta", "gamma"]
        assert [item.revision for item in items] == ["10", "20", "30"]
        assert [item.channel for item in items] == ["latest/stable", "latest/stable", "latest/edge"]

    @pytest.mark.asyncio
    async def test_column_reordered_header_still_parses_correctly(self) -> None:
        """Two columns swapped in BOTH header and body — parsing must still be correct,
        proving it is header-driven rather than positional.
        """
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_COLUMN_REORDERED, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert len(items) == 1
        assert items[0].name == "alpha"
        assert items[0].revision == "10"
        assert items[0].channel == "latest/stable"

    @pytest.mark.asyncio
    async def test_disabled_revision_line_produces_no_item(self) -> None:
        """A disabled older-revision line for a snap that also has an active line
        yields only the active revision as an item.
        """
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_WITH_DISABLED_REVISION, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert len(items) == 1
        assert items[0].revision == "2938"

    @pytest.mark.asyncio
    async def test_no_snaps_installed_yields_empty_list_not_a_crash(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")}
        )
        job = SnapSyncJob(context)

        assert await job.capture_source_items() == []


class TestDiff:
    """`plan()`'s snap-specific diff: install/remove/change, D-06's active-converge rule."""

    @pytest.mark.asyncio
    async def test_missing_on_target_yields_install_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        alpha = next(d for d in plan.diffs if d.item_id == "snap:alpha")
        assert alpha.diff_class == DiffClass.MISSING_ON_TARGET
        assert alpha.action == DiffAction.INSTALL

    @pytest.mark.asyncio
    async def test_extra_on_target_yields_remove_diff_in_its_own_group(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        delta = next(d for d in plan.diffs if d.item_id == "snap:delta")
        assert delta.action == DiffAction.REMOVE
        remove_group = next(g for g in plan.groups if g.action == "remove")
        install_group = next(g for g in plan.groups if g.action == "install")
        assert {e.item_id for e in remove_group.entries} == {"snap:delta"}
        assert "snap:delta" not in {e.item_id for e in install_group.entries}

    @pytest.mark.asyncio
    async def test_revision_change_yields_change_diff_naming_both_revisions(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        beta = next(d for d in plan.diffs if d.item_id == "snap:beta")
        assert beta.action == DiffAction.CHANGE
        assert beta.detail is not None
        assert "20" in beta.detail
        assert "15" in beta.detail

    @pytest.mark.asyncio
    async def test_same_revision_different_channel_yields_change_diff_naming_both_channels(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        gamma = next(d for d in plan.diffs if d.item_id == "snap:gamma")
        assert gamma.action == DiffAction.CHANGE
        assert gamma.detail is not None
        assert "latest/edge" in gamma.detail
        assert "latest/stable" in gamma.detail

    @pytest.mark.asyncio
    async def test_identical_snap_yields_no_diff(self) -> None:
        identical = _HEADER + "epsilon   1.0   50   latest/stable   pub✓   -\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, identical, "")},
            target_responses={"snap list --all": CommandResult(0, identical, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()


class TestPlanReadOnly:
    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_snap_command(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 4  # alpha install, beta change, gamma change, delta remove
        for cmd in all_calls(target):
            assert "snap install" not in cmd
            assert "snap refresh" not in cmd
            assert "snap switch" not in cmd
            assert "snap remove" not in cmd


class TestNoHold:
    """The single most important guarantee (D-06, RESEARCH Pitfall 1): no command this
    job issues across install/change/channel-retrack/removal ever sets a snap hold.
    """

    @pytest.mark.asyncio
    async def test_install_change_retrack_and_removal_never_set_a_hold(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        assert len(plan.diffs) == 4

        for diff in plan.diffs:
            await job.converge(diff)

        commands = all_calls(target)
        assert commands
        assert not any("--hold" in cmd for cmd in commands)
        assert any("--revision=" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_install_command_contains_an_explicit_revision(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        alpha_diff = next(d for d in plan.diffs if d.item_id == "snap:alpha")

        await job.converge(alpha_diff)

        commands = all_calls(target)
        assert any("snap install --revision=10 alpha" in cmd for cmd in commands)


# Per-snap hold fixtures (#208): `held` in the Notes column marks a per-snap refresh hold.
SNAP_LIST_SOURCE_HELD_ALPHA = (
    _HEADER
    + "alpha     1.0        10     latest/stable   pub✓         held\n"
    + "beta      2.0        20     latest/stable   pub✓         -\n"
)
SNAP_LIST_TARGET_UNHELD = (
    _HEADER
    + "alpha     1.0        10     latest/stable   pub✓         -\n"
    + "beta      2.0        20     latest/stable   pub✓         -\n"
)
SNAP_LIST_TARGET_HELD_ALPHA = (
    _HEADER
    + "alpha     1.0        10     latest/stable   pub✓         held\n"
    + "beta      2.0        20     latest/stable   pub✓         -\n"
)
# alpha held on the source but ABSENT on the target -> both an install (presence) diff
# and a hold diff, exercising the D8 install-before-hold ordering guarantee.
SNAP_LIST_SOURCE_HELD_ONLY_ALPHA = _HEADER + "alpha     1.0        10     latest/stable   pub✓         held\n"

# One plan carrying BOTH ordinary presence diffs and both hold directions, so the review
# vocabulary is exercised where a hold shares its DiffAction with a snap (#208 D3):
#   alpha   -> source only            -> snap:alpha        INSTALL
#   delta   -> target only            -> snap:delta        REMOVE
#   epsilon -> identical, source-held -> snap:hold:epsilon INSTALL
#   zeta    -> identical, target-held -> snap:hold:zeta    REMOVE
SNAP_LIST_SOURCE_MIXED_HOLDS = (
    _HEADER
    + "alpha     1.0        10     latest/stable   pub✓         -\n"
    + "epsilon   1.0        50     latest/stable   pub✓         held\n"
    + "zeta      1.0        60     latest/stable   pub✓         -\n"
)
SNAP_LIST_TARGET_MIXED_HOLDS = (
    _HEADER
    + "delta     4.0        40     latest/stable   pub✓         -\n"
    + "epsilon   1.0        50     latest/stable   pub✓         -\n"
    + "zeta      1.0        60     latest/stable   pub✓         held\n"
)


class TestParseHeld:
    """`held` in the Notes column sets `SnapItem.held` (#208)."""

    @pytest.mark.asyncio
    async def test_held_note_sets_item_held(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_HELD_ALPHA, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        by_name = {item.name: item for item in items}
        assert by_name["alpha"].held is True
        assert by_name["beta"].held is False


class TestHolds:
    """Per-snap hold membership replication (#208, D2/D4/D6)."""

    @pytest.mark.asyncio
    async def test_source_held_yields_install_hold_diff_and_converges_hold_forever(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_HELD_ALPHA, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET_UNHELD, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        hold = next(d for d in plan.diffs if d.item_id == "snap:hold:alpha")
        assert hold.action == DiffAction.INSTALL
        assert hold.diff_class == DiffClass.MISSING_ON_TARGET

        await job.converge(hold)

        commands = all_calls(target)
        assert any("snap refresh --hold=forever alpha" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_target_held_only_yields_remove_hold_diff_and_converges_unhold(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET_UNHELD, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET_HELD_ALPHA, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        hold = next(d for d in plan.diffs if d.item_id == "snap:hold:alpha")
        assert hold.action == DiffAction.REMOVE
        assert hold.diff_class == DiffClass.EXTRA_ON_TARGET

        await job.converge(hold)

        commands = all_calls(target)
        assert any("snap refresh --unhold alpha" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_both_held_yields_no_hold_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_HELD_ALPHA, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET_HELD_ALPHA, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id.startswith("snap:hold:") for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_hold_diff_emitted_after_presence_diffs(self) -> None:
        """D8 install-before-hold: alpha is new on the target AND held on the source, so
        its `snap:hold:alpha` diff must come after its `snap:alpha` install diff.
        """
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_HELD_ONLY_ALPHA, "")},
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        ids = [d.item_id for d in plan.diffs]
        assert "snap:alpha" in ids
        assert "snap:hold:alpha" in ids
        assert ids.index("snap:alpha") < ids.index("snap:hold:alpha")

    @pytest.mark.asyncio
    async def test_hold_converge_never_emits_bare_hold(self) -> None:
        """The D-06/RESEARCH Pitfall 1 guarantee for the hold path: the snap name is
        always present, so `--hold` never appears without a following snap name.
        """
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_HELD_ALPHA, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET_UNHELD, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        hold = next(d for d in plan.diffs if d.item_id == "snap:hold:alpha")

        await job.converge(hold)

        for cmd in all_calls(target):
            assert "--hold=forever alpha" in cmd or "--hold" not in cmd
            # A bare `snap refresh --hold` with no snap name is the global-hold pitfall.
            assert cmd.strip() != "sudo snap refresh --hold"
            assert not cmd.rstrip().endswith("--hold")


class TestHoldReviewVerbs:
    """#208 D3 — a hold item NEVER displays under an install/remove snap group.

    `_build_review_groups` keys the group title AND every entry's `action_label` off
    `_ACTION_VOCABULARY` by the group's own item class, so a `SNAP_HOLD` INSTALL reads
    "hold" and a `SNAP_HOLD` REMOVE reads "unhold" even when ordinary `SNAP` INSTALL and
    REMOVE diffs share those very actions in the same plan.
    """

    @staticmethod
    async def _mixed_plan() -> PackagePlan:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_MIXED_HOLDS, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET_MIXED_HOLDS, "")},
        )
        return await SnapSyncJob(context).plan()

    @staticmethod
    def _group_holding(plan: PackagePlan, item_id: str) -> ReviewGroup:
        return next(g for g in plan.groups if any(e.item_id == item_id for e in g.entries))

    @pytest.mark.asyncio
    async def test_hold_install_group_reads_hold_never_install(self) -> None:
        plan = await self._mixed_plan()

        group = self._group_holding(plan, "snap:hold:epsilon")

        assert group.title == "Hold snap packages"
        assert [e.action_label for e in group.entries] == ["hold"]
        # The hold has its own group: it never joins the snap install group.
        assert {e.item_id for e in group.entries} == {"snap:hold:epsilon"}

    @pytest.mark.asyncio
    async def test_hold_remove_group_reads_unhold_and_is_removal_direction(self) -> None:
        """The unhold group must be removal-direction so the checkbox screen leaves it
        unticked — the right friction for undoing a block the user deliberately set. That
        classification is `packages/review._is_removal_direction` applied to
        `ReviewGroup.action`, so this asserts against the real classifier rather than
        restating the string.
        """
        plan = await self._mixed_plan()

        group = self._group_holding(plan, "snap:hold:zeta")

        assert group.title == "Unhold snap packages"
        assert [e.action_label for e in group.entries] == ["unhold"]
        assert _is_removal_direction(group.action)

    @pytest.mark.asyncio
    async def test_snap_groups_keep_their_own_verbs_and_exclude_hold_items(self) -> None:
        plan = await self._mixed_plan()

        install_group = self._group_holding(plan, "snap:alpha")
        remove_group = self._group_holding(plan, "snap:delta")

        assert install_group.title == "Install snap packages"
        assert [e.action_label for e in install_group.entries] == ["install"]
        assert remove_group.title == "Remove snap packages"
        assert [e.action_label for e in remove_group.entries] == ["remove"]
        # Neither presence group absorbed a hold item despite sharing its DiffAction.
        assert not any(e.item_id.startswith("snap:hold:") for e in (*install_group.entries, *remove_group.entries))
        # No entry anywhere reads a package verb for a hold item.
        for group in plan.groups:
            for entry in group.entries:
                if entry.item_id.startswith("snap:hold:"):
                    assert entry.action_label in {"hold", "unhold"}


class TestHoldIntentIsSourceAuthoritative:
    @pytest.mark.asyncio
    async def test_hold_on_a_snap_the_source_does_not_have_yields_no_hold_diff(self) -> None:
        """E14 — `_diff_snap_holds` iterates SOURCE snaps only: a hold recorded on the
        target for a snap the source no longer has at all is not the user's current
        intent, so no `snap:hold:` diff is proposed (the snap itself is still offered
        for removal as an ordinary presence diff).
        """
        source = _HEADER + "alpha     1.0        10     latest/stable   pub✓         -\n"
        target = (
            _HEADER
            + "alpha     1.0        10     latest/stable   pub✓         -\n"
            + "orphan    9.0        90     latest/stable   pub✓         held\n"
        )
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id.startswith("snap:hold:") for d in plan.diffs)
        assert [d.item_id for d in plan.diffs] == ["snap:orphan"]


class TestHoldAndRevisionFailuresArePerItem:
    """D-27/D6: a hold or refresh command that snapd rejects fails exactly that item —
    the loop still completes and every other approved item still converges.
    """

    @pytest.mark.asyncio
    async def test_hold_for_a_snap_absent_on_target_fails_only_that_item(self) -> None:
        """E18 — the user skipped alpha's install but applied its hold, so
        `snap refresh --hold=forever alpha` hits an absent snap and exits non-zero. That
        is a normal per-item failure (D6: no gating machinery), and the epsilon hold that
        follows it still converges.
        """
        source = (
            _HEADER
            + "alpha     1.0        10     latest/stable   pub✓         held\n"
            + "epsilon   1.0        50     latest/stable   pub✓         held\n"
        )
        target = _HEADER + "epsilon   1.0        50     latest/stable   pub✓         -\n"
        context, _source, target_mock = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={
                "snap list --all": CommandResult(0, target, ""),
                "snap refresh --hold=forever alpha": CommandResult(1, "", 'snap "alpha" is not installed'),
            },
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={
                    "snap:alpha": Decision.SKIP_ONCE,
                    "snap:hold:alpha": Decision.APPLY,
                    "snap:hold:epsilon": Decision.APPLY,
                },
                was_interactive=True,
            ),
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        assert [diff.item_id for diff, _stderr in exc_info.value.failures] == ["snap:hold:alpha"]
        commands = all_calls(target_mock)
        # The failing item precedes the succeeding one, so this proves the loop continued.
        assert any("snap refresh --hold=forever epsilon" in c for c in commands)
        assert not any("snap install" in c for c in commands)

    @pytest.mark.asyncio
    async def test_unfetchable_revision_is_a_clean_per_item_failure_not_a_crash(self) -> None:
        """E22 — the D-06 assumption that the source's `--revision=N` is fetchable by the
        target's snapd can fail (a revision that never reached this machine's store). The
        refusal surfaces as a per-item `PackageItemFailures`, the channel switch for the
        failed snap is skipped, and gamma's own retrack still runs.
        """
        source = (
            _HEADER
            + "beta      2.0        20     latest/stable   pub✓         -\n"
            + "gamma     3.0        30     latest/stable   pub✓         -\n"
        )
        target = (
            _HEADER
            + "beta      1.5        15     latest/stable   pub✓         -\n"
            + "gamma     3.0        30     latest/edge     pub✓         -\n"
        )
        context, _source, target_mock = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={
                "snap list --all": CommandResult(0, target, ""),
                "snap refresh --revision=20 beta": CommandResult(
                    1, "", 'error: cannot perform the following tasks:\n- Download snap "beta" (20)'
                ),
            },
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={"snap:beta": Decision.APPLY, "snap:gamma": Decision.APPLY},
                was_interactive=True,
            ),
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        assert [diff.item_id for diff, _stderr in exc_info.value.failures] == ["snap:beta"]
        commands = all_calls(target_mock)
        # A failed revision refresh short-circuits its own channel switch...
        assert not any("snap switch" in c and " beta" in c for c in commands)
        # ...but gamma's same-revision retrack still converged.
        assert any("sudo snap switch --channel=latest/stable gamma" in c for c in commands)


class TestConvergeRemoval:
    @pytest.mark.asyncio
    async def test_removal_never_passes_purge(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        delta_diff = next(d for d in plan.diffs if d.item_id == "snap:delta")

        await job.converge(delta_diff)

        commands = all_calls(target)
        assert any("snap remove delta" in cmd for cmd in commands)
        assert not any("purge" in cmd for cmd in commands)


class TestExcludePaths:
    def test_excludes_old_revisions_keeps_current_common_and_current_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The revision `current` resolves to is mirrored (kept OUT of the exclude set,
        decision 3); every retained OLDER revision dir is excluded, and `common`/`current`
        are always kept.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        firefox_dir = tmp_path / "snap" / "firefox"
        current_rev = firefox_dir / "2938"
        old_rev = firefox_dir / "2911"
        common_dir = firefox_dir / "common"
        current_rev.mkdir(parents=True)
        old_rev.mkdir(parents=True)
        common_dir.mkdir(parents=True)
        (firefox_dir / "current").symlink_to(current_rev, target_is_directory=True)

        paths = snap_sync_exclude_paths()

        assert old_rev in paths  # retained old revision the target never installed
        assert current_rev not in paths  # active-revision data dir travels
        assert not any(p.name == "common" for p in paths)
        assert not any(p.name == "current" for p in paths)

    def test_dangling_current_falls_back_to_excluding_all_revisions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing/dangling `current` means the active revision is indeterminate, so every
        revision dir is excluded (safe default).
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        firefox_dir = tmp_path / "snap" / "firefox"
        rev_a = firefox_dir / "2938"
        rev_b = firefox_dir / "2911"
        rev_a.mkdir(parents=True)
        rev_b.mkdir(parents=True)
        # `current` points at a revision dir that does not exist -> dangling.
        (firefox_dir / "current").symlink_to(firefox_dir / "9999", target_is_directory=True)

        paths = snap_sync_exclude_paths()

        assert rev_a in paths
        assert rev_b in paths

    def test_missing_current_symlink_falls_back_to_excluding_all_revisions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        firefox_dir = tmp_path / "snap" / "firefox"
        rev = firefox_dir / "2938"
        rev.mkdir(parents=True)
        # No `current` symlink created at all.

        paths = snap_sync_exclude_paths()

        assert rev in paths

    def test_no_snap_directory_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert snap_sync_exclude_paths() == []


class TestValidate:
    @pytest.mark.asyncio
    async def test_snap_unavailable_on_source_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap version": CommandResult(127, "", "not found")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "snap is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_snap_unavailable_on_target_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            target_responses={"snap version": CommandResult(127, "", "not found")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "snap is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_target_without_passwordless_sudo_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            target_responses={"sudo -n true": CommandResult(1, "", "sudo: a password is required")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_source_without_passwordless_sudo_yields_validation_error(self) -> None:
        """The source needs passwordless sudo too — the orchestrator pauses snapd
        auto-refresh via `sudo snap set system refresh.hold` on the source as well (decision 4).
        """
        context, _source, _target = make_context(
            source_responses={"sudo -n true": CommandResult(1, "", "sudo: a password is required")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors(self) -> None:
        context, _source, _target = make_context()
        job = SnapSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_snap_sync_to_snap_sync_job(self) -> None:
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("snap_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is SnapSyncJob
