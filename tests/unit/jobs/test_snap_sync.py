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
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import (
    Decision,
    ReviewGroup,
    ReviewOutcome,
    _is_removal_direction,  # pyright: ignore[reportPrivateUsage]
)
from pcswitcher.jobs.packages.sync_core import PackageItemFailures, PackagePlan
from pcswitcher.jobs.snap_sync import SnapItem, SnapSyncJob, snap_sync_exclude_paths, target_snap_revisions
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
        """E1, E22 — name, revision and channel come from the columns their headers name."""
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
        """E23 — two columns swapped in BOTH header and body — parsing must still be correct,
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
        """E24 — a disabled older-revision line for a snap that also has an active line
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
    async def test_a_line_with_fewer_fields_than_the_header_is_dropped(self) -> None:
        """E29 — a truncated line is skipped rather than read across the columns it does
        have, which would hand `--revision=` a value from the wrong column.
        """
        listing = _HEADER + "alpha     1.0\n" + "beta      2.0        20     latest/stable   pub✓         -\n"
        context, _source, _target = make_context(source_responses={"snap list --all": CommandResult(0, listing, "")})
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert [(item.name, item.revision) for item in items] == [("beta", "20")]

    @pytest.mark.asyncio
    async def test_no_snaps_installed_yields_empty_list_not_a_crash(self) -> None:
        """E26 — "No snaps are installed yet." at exit 0 is an empty machine, not a crash."""
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")}
        )
        job = SnapSyncJob(context)

        assert await job.capture_source_items() == []


class TestDiff:
    """`plan()`'s snap-specific diff: install/remove/change, D-06's active-converge rule."""

    @pytest.mark.asyncio
    async def test_missing_on_target_yields_install_diff(self) -> None:
        """E7 — a snap only Atlas has is offered for install on Nomad."""
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
        """E10 — a snap only Nomad has is a removal, in a group of its own."""
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
        """E11, E18 — a revision difference is one CHANGE naming both revisions, never a report."""
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
        """E13 — a channel difference alone is one CHANGE naming both channels."""
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
    async def test_revision_and_channel_both_differing_names_both_pairs(self) -> None:
        """E15 — E15, `PKG-FR-SNAP-CASES`: one change, naming both values. Naming the revision alone
        left the retrack out of the only line the user reads before approving it.
        """
        source = _HEADER + "beta      2.0        20     latest/edge     pub✓         -\n"
        target = _HEADER + "beta      1.5        15     latest/stable   pub✓         -\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        beta = next(d for d in plan.diffs if d.item_id == "snap:beta")
        assert beta.detail == (
            "revision: source-host has 20, target-host has 15; "
            "channel: source-host has latest/edge, target-host has latest/stable"
        )

    @pytest.mark.asyncio
    async def test_identical_snap_yields_no_diff(self) -> None:
        """E17, E59 — same revision and channel on both machines: no item, and no hold item."""
        identical = _HEADER + "epsilon   1.0   50   latest/stable   pub✓   -\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, identical, "")},
            target_responses={"snap list --all": CommandResult(0, identical, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_neither_machine_has_any_snap_yields_nothing_and_no_failure(self) -> None:
        """E20 — both halves of "no snaps" at once. Each half alone is ordinary data
        (E17/E26); together they must still produce a plan rather than an empty-manifest
        scare.
        """
        empty = CommandResult(0, "", "No snaps are installed yet. Try 'snap install hello-world'.\n")
        context, _source, _target = make_context(
            source_responses={"snap list --all": empty},
            target_responses={"snap list --all": empty},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert plan.groups == ()


class TestPublisherIsNotIdentity:
    """`PKG-FR-SNAP-IDENTITY` — a snap is identified by its name alone. The Publisher
    column exists in the listing this job parses, so nothing but a test stops it from
    creeping into the match and splitting one snap into an install plus a removal.
    """

    @pytest.mark.asyncio
    async def test_differing_publishers_at_the_same_revision_yield_no_diff(self) -> None:
        """E4 — same name, revision and channel; only the Publisher differs."""
        source = _HEADER + "beta      2.0        20     latest/stable   canonical✓   -\n"
        target = _HEADER + "beta      2.0        20     latest/stable   someone-else -\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_differing_publishers_with_a_revision_difference_yield_one_change(self) -> None:
        """E4 — the same two listings with the revisions apart: one CHANGE for one snap,
        never an install of the source's copy plus a removal of the target's.
        """
        source = _HEADER + "beta      2.0        20     latest/stable   canonical✓   -\n"
        target = _HEADER + "beta      1.5        15     latest/stable   someone-else -\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert [(d.item_id, d.action) for d in plan.diffs] == [("snap:beta", DiffAction.CHANGE)]


class TestPlanReadOnly:
    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_snap_command(self) -> None:
        """H3, J144."""
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
        """E65 — no install, change, retrack or removal command sets a refresh hold."""
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
        """E8 — the install names Atlas's exact revision."""
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


class TestConvergeRevisionAndChannel:
    """`PKG-FR-SNAP-CASES`'s command side: which of the two commands an approved item
    issues follows from which of the two facets actually differs, and a snap that needs
    both gets both, revision first.
    """

    @staticmethod
    def _converge_commands(target: MagicMock) -> list[str]:
        """Every mutating snap command issued, in order — plan()'s own reads filtered out."""
        return [cmd for cmd in all_calls(target) if cmd.startswith("sudo snap ")]

    @pytest.mark.asyncio
    async def test_install_ends_tracking_the_sources_channel(self) -> None:
        """E9 — the channel is set as part of the install, so the target does not silently
        end up on snapd's default channel for a snap the source tracks elsewhere.
        """
        source = _HEADER + "alpha     1.0        10     latest/edge     pub✓         -\n"
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        alpha = next(d for d in plan.diffs if d.item_id == "snap:alpha")

        await job.converge(alpha)

        assert self._converge_commands(target) == [
            "sudo snap install --revision=10 alpha",
            "sudo snap switch --channel=latest/edge alpha",
        ]

    @pytest.mark.asyncio
    async def test_a_revision_only_change_issues_no_channel_switch(self) -> None:
        """E12 — the channels already match, so a retrack would be a command with nothing
        behind it.
        """
        source = _HEADER + "beta      2.0        20     latest/stable   pub✓         -\n"
        target_listing = _HEADER + "beta      1.5        15     latest/stable   pub✓         -\n"
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target_listing, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        beta = next(d for d in plan.diffs if d.item_id == "snap:beta")

        await job.converge(beta)

        assert self._converge_commands(target) == ["sudo snap refresh --revision=20 beta"]

    @pytest.mark.asyncio
    async def test_a_channel_only_change_issues_no_revision_refresh(self) -> None:
        """E14 — the revisions already match, so the retrack travels alone: a `--revision`
        refresh here would re-fetch the revision the target is already on.
        """
        source = _HEADER + "gamma     3.0        30     latest/edge     pub✓         -\n"
        target_listing = _HEADER + "gamma     3.0        30     latest/stable   pub✓         -\n"
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target_listing, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        gamma = next(d for d in plan.diffs if d.item_id == "snap:gamma")

        await job.converge(gamma)

        assert self._converge_commands(target) == ["sudo snap switch --channel=latest/edge gamma"]

    @pytest.mark.asyncio
    async def test_a_change_differing_in_both_moves_the_revision_then_the_channel(self) -> None:
        """E16 — one item, both commands. The revision move comes first: it is the one that
        can fail, and a retrack onto a channel whose revision never landed says nothing true.
        """
        source = _HEADER + "beta      2.0        20     latest/edge     pub✓         -\n"
        target_listing = _HEADER + "beta      1.5        15     latest/stable   pub✓         -\n"
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target_listing, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        beta = next(d for d in plan.diffs if d.item_id == "snap:beta")

        await job.converge(beta)

        assert self._converge_commands(target) == [
            "sudo snap refresh --revision=20 beta",
            "sudo snap switch --channel=latest/edge beta",
        ]


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

# The same plan plus a revision/channel CHANGE, so every action snap can review — install,
# change, remove, hold and unhold — is present at once.
SNAP_LIST_SOURCE_EVERY_ACTION = (
    SNAP_LIST_SOURCE_MIXED_HOLDS + "beta      2.0        20     latest/edge     pub✓         -\n"
)
SNAP_LIST_TARGET_EVERY_ACTION = (
    SNAP_LIST_TARGET_MIXED_HOLDS + "beta      1.5        15     latest/stable   pub✓         -\n"
)


class TestParseHeld:
    """`held` in the Notes column sets `SnapItem.held` (#208)."""

    @pytest.mark.asyncio
    async def test_held_note_sets_item_held(self) -> None:
        """E3 — a per-snap refresh hold is captured as part of that machine's snap state."""
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
        """E54, E55 — Atlas's hold is an item of its own, and applying it holds the snap on Nomad."""
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
    async def test_a_hold_on_a_snap_this_run_installs_is_one_question_with_it(self) -> None:
        """E116 — `alpha` is missing on the target and held on the source, so the two are one
        merged question: no `alpha (hold)` row anywhere, and the install's own row says the
        target ends up holding its refreshes (`PKG-FR-BLOCKS-REPLICATE`).
        """
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_HELD_ALPHA, "")},
            target_responses={"snap list --all": CommandResult(0, _HEADER, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()

        entries = {entry.item_id: entry for group in plan.groups for entry in group.entries}
        assert "snap:hold:alpha" not in entries
        assert "target-host ends up holding its refreshes" in (entries["snap:alpha"].detail or "")

        job.accept_review(plan, ReviewOutcome(decisions={"snap:alpha": Decision.APPLY}, was_interactive=True))
        await job.apply()

        commands = all_calls(target)
        install = next(i for i, cmd in enumerate(commands) if "snap install --revision=10 alpha" in cmd)
        hold = next(i for i, cmd in enumerate(commands) if "snap refresh --hold=forever alpha" in cmd)
        assert install < hold

    @pytest.mark.asyncio
    async def test_target_held_only_yields_remove_hold_diff_and_converges_unhold(self) -> None:
        """E56, E57 — Nomad's own hold is an item proposing to lift it, and applying it does."""
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
        """E58 — both machines hold it: nothing to converge."""
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_HELD_ALPHA, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET_HELD_ALPHA, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id.startswith("snap:hold:") for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_hold_diff_emitted_after_presence_diffs(self) -> None:
        """E63 — E63, D8 install-before-hold: alpha is new on the target AND held on the source, so
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
        """E66 — the D-06/RESEARCH Pitfall 1 guarantee for the hold path: the snap name is
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
        """E61 — a hold item sits in a group of its own reading "hold", never in the install group."""
        plan = await self._mixed_plan()

        group = self._group_holding(plan, "snap:hold:epsilon")

        assert group.title == "Hold snap packages"
        assert [e.action_label for e in group.entries] == ["hold"]
        # The hold has its own group: it never joins the snap install group.
        assert {e.item_id for e in group.entries} == {"snap:hold:epsilon"}

    @pytest.mark.asyncio
    async def test_hold_remove_group_reads_unhold_and_is_removal_direction(self) -> None:
        """E61, E62 — the unhold group must be removal-direction so the checkbox screen leaves it
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
        """E61 — the install and remove groups keep their own verbs and absorb no hold item."""
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


class TestAFullSnapReview:
    """One plan carrying every action snap reviews — install, change, remove, hold and
    unhold — used for the claims that are about the review as a whole.
    """

    @staticmethod
    async def _every_action_plan() -> PackagePlan:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_EVERY_ACTION, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET_EVERY_ACTION, "")},
        )
        return await SnapSyncJob(context).plan()

    @pytest.mark.asyncio
    async def test_the_change_group_reads_as_a_change_and_stands_apart(self) -> None:
        """E19 — the fifth verb, alongside the four `TestHoldReviewVerbs` pins: a snap whose
        revision or channel moved is a change, not an install of the source's copy.
        """
        plan = await self._every_action_plan()

        change_group = next(g for g in plan.groups if any(e.item_id == "snap:beta" for e in g.entries))

        assert change_group.title == "Change snap packages"
        assert [e.action_label for e in change_group.entries] == ["change"]
        assert {e.item_id for e in change_group.entries} == {"snap:beta"}
        assert not _is_removal_direction(change_group.action)

    @pytest.mark.asyncio
    async def test_no_item_or_group_ever_asks_where_a_snap_comes_from(self) -> None:
        """E5, `PKG-NG-SNAP-ORIGIN` — one store serves the device and snapd pins name ->
        publisher itself, so snap has no store, publisher, remote or key for the user to
        decide about. Nothing but this test stands between the article and a future change
        that adds an origin screen by analogy with apt or flatpak.
        """
        plan = await self._every_action_plan()

        # The plan really does carry all five actions, so the sweep below is not vacuous.
        assert {(d.item_id, d.action) for d in plan.diffs} == {
            ("snap:alpha", DiffAction.INSTALL),
            ("snap:beta", DiffAction.CHANGE),
            ("snap:delta", DiffAction.REMOVE),
            ("snap:hold:epsilon", DiffAction.INSTALL),
            ("snap:hold:zeta", DiffAction.REMOVE),
        }

        assert {d.item_class for d in plan.diffs} == {ItemClass.SNAP, ItemClass.SNAP_HOLD}
        origin_words = ("store", "publisher", "remote", "key", "vendor", "origin")
        for group in plan.groups:
            text = " ".join(
                [group.title, group.note or "", *(f"{e.item_id} {e.label} {e.detail or ''}" for e in group.entries)]
            ).lower()
            assert not any(word in text for word in origin_words), text


class TestHoldIntentIsSourceAuthoritative:
    @pytest.mark.asyncio
    async def test_hold_on_a_snap_the_source_does_not_have_yields_no_hold_diff(self) -> None:
        """E60 — `_diff_snap_holds` iterates SOURCE snaps only: a hold recorded on the
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
        """E64 — alpha's merged install-and-hold is approved and the install fails, so
        `snap refresh --hold=forever alpha` hits an absent snap and exits non-zero. That
        is a normal per-item failure (D6: no gating machinery), and the epsilon hold — its
        own item, since epsilon is on both machines — still converges.
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
                "snap install --revision=10 alpha": CommandResult(1, "", 'cannot install "alpha"'),
                "snap refresh --hold=forever alpha": CommandResult(1, "", 'snap "alpha" is not installed'),
            },
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={
                    "snap:alpha": Decision.APPLY,
                    "snap:hold:epsilon": Decision.APPLY,
                },
                was_interactive=True,
            ),
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        assert [diff.item_id for diff, _stderr in exc_info.value.failures] == ["snap:alpha", "snap:hold:alpha"]
        commands = all_calls(target_mock)
        # The failing items precede the succeeding one, so this proves the loop continued.
        assert any("snap refresh --hold=forever epsilon" in c for c in commands)

    @pytest.mark.asyncio
    async def test_unfetchable_revision_is_a_clean_per_item_failure_not_a_crash(self) -> None:
        """E50, E51 — the D-06 assumption that the source's `--revision=N` is fetchable by the
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

    @pytest.mark.asyncio
    async def test_an_install_the_target_cannot_fetch_fails_only_that_snap(self) -> None:
        """E114 — the same refusal on the INSTALL path rather than the refresh path: the
        target's snapd cannot fetch alpha's revision, so alpha fails alone, its channel
        switch never runs, and beta still installs.
        """
        source = (
            _HEADER
            + "alpha     1.0        10     latest/stable   pub✓         -\n"
            + "beta      2.0        20     latest/stable   pub✓         -\n"
        )
        context, _source, target_mock = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={
                "snap list --all": CommandResult(0, "No snaps are installed yet.\n", ""),
                "snap install --revision=10 alpha": CommandResult(
                    1, "", 'error: cannot perform the following tasks:\n- Download snap "alpha" (10)'
                ),
            },
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={"snap:alpha": Decision.APPLY, "snap:beta": Decision.APPLY},
                was_interactive=True,
            ),
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        assert [diff.item_id for diff, _stderr in exc_info.value.failures] == ["snap:alpha"]
        commands = all_calls(target_mock)
        assert not any("snap switch" in c and " alpha" in c for c in commands)
        assert any("sudo snap install --revision=20 beta" in c for c in commands)

    @pytest.mark.asyncio
    async def test_a_removal_the_target_refuses_fails_only_that_snap(self) -> None:
        """E52 — a removal snapd refuses is an ordinary per-item failure: the loop still
        reaches the second approved removal.
        """
        target = (
            _HEADER
            + "delta     4.0        40     latest/stable   pub✓         -\n"
            + "omega     9.0        90     latest/stable   pub✓         -\n"
        )
        context, _source, target_mock = make_context(
            source_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
            target_responses={
                "snap list --all": CommandResult(0, target, ""),
                "snap remove delta": CommandResult(1, "", 'error: cannot remove "delta": snap is being used'),
            },
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={"snap:delta": Decision.APPLY, "snap:omega": Decision.APPLY},
                was_interactive=True,
            ),
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        assert [diff.item_id for diff, _stderr in exc_info.value.failures] == ["snap:delta"]
        assert any("sudo snap remove omega" in c for c in all_calls(target_mock))


# Confinement fixtures: `classic`/`devmode` in the Notes column. `zellij` is the real
# classic snap on this project's own machine; `snap list --all` shows it as
# "zellij 43 disabled,classic" plus an active "classic" line.
SNAP_LIST_SOURCE_CLASSIC = (
    _HEADER
    + "zellij    0.44.1     65     latest/stable   dominz88     classic\n"
    + "beta      2.0        20     latest/stable   pub✓         -\n"
)
SNAP_LIST_SOURCE_DEVMODE = _HEADER + "toy       1.0        7      latest/edge     pub✓         devmode\n"


class TestParseConfinement:
    """`classic`/`devmode` in the Notes column set the confinement fields."""

    @pytest.mark.asyncio
    async def test_classic_note_sets_item_classic(self) -> None:
        """E2 — `classic` in the Notes column is captured alongside the revision and channel."""
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_CLASSIC, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        by_name = {item.name: item for item in items}
        assert by_name["zellij"].classic is True
        assert by_name["zellij"].devmode is False
        assert by_name["beta"].classic is False

    @pytest.mark.asyncio
    async def test_devmode_note_sets_item_devmode(self) -> None:
        """E2 — `devmode` likewise, and it never reads as classic."""
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_DEVMODE, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert items[0].devmode is True
        assert items[0].classic is False

    @pytest.mark.asyncio
    async def test_disabled_classic_line_is_still_skipped(self) -> None:
        """E25 — the real `snap list --all` shape for a classic snap with a retained older
        revision: `disabled,classic` shares one Notes list, and `disabled` still wins.
        """
        listing = (
            _HEADER
            + "zellij    0.43.0     43     latest/stable   dominz88     disabled,classic\n"
            + "zellij    0.44.1     65     latest/stable   dominz88     classic\n"
        )
        context, _source, _target = make_context(source_responses={"snap list --all": CommandResult(0, listing, "")})
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert len(items) == 1
        assert items[0].revision == "65"
        assert items[0].classic is True


class TestConvergeConfinement:
    """snapd refuses a classic/devmode revision without the matching confirmation flag,
    per-revision — `--revision=N` does not bypass it — so the converge commands must
    carry the SOURCE item's confinement.
    """

    @pytest.mark.asyncio
    async def test_install_of_classic_snap_passes_classic(self) -> None:
        """E30 — a classic snap's install carries the confirmation snapd requires."""
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_CLASSIC, "")},
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        zellij = next(d for d in plan.diffs if d.item_id == "snap:zellij")

        await job.converge(zellij)

        assert any("sudo snap install --classic --revision=65 zellij" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_install_of_strict_snap_passes_no_confinement_flag(self) -> None:
        """E32 — a strictly confined snap's install carries neither flag."""
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_CLASSIC, "")},
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        beta = next(d for d in plan.diffs if d.item_id == "snap:beta")

        await job.converge(beta)

        commands = all_calls(target)
        assert any("sudo snap install --revision=20 beta" in c for c in commands)
        assert not any("--classic" in c or "--devmode" in c for c in commands)

    @pytest.mark.asyncio
    async def test_install_of_devmode_snap_passes_devmode_and_never_classic(self) -> None:
        """E31 — a devmode snap's install carries `--devmode` and never `--classic`."""
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_DEVMODE, "")},
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        toy = next(d for d in plan.diffs if d.item_id == "snap:toy")

        await job.converge(toy)

        commands = all_calls(target)
        assert any("sudo snap install --devmode --revision=7 toy" in c for c in commands)
        assert not any("--classic" in c for c in commands)

    @pytest.mark.asyncio
    async def test_refresh_passes_classic_when_target_is_strict(self) -> None:
        """E33 — source classic, target strict, same snap: a plain `snap refresh` preserves the
        TARGET's confinement, which is the wrong one here — snapd would refuse the source's
        classic revision. The flag follows the SOURCE item.
        """
        target_listing = _HEADER + "zellij    0.43.0     43     latest/stable   dominz88     -\n"
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_CLASSIC, "")},
            target_responses={"snap list --all": CommandResult(0, target_listing, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        zellij = next(d for d in plan.diffs if d.item_id == "snap:zellij")

        await job.converge(zellij)

        assert any("sudo snap refresh --classic --revision=65 zellij" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_refresh_passes_no_flag_when_only_the_target_is_classic(self) -> None:
        """E34 — the reverse skew of the test above. The flag follows the SOURCE either
        way, so a strict source revision refreshes with no flag at all; the target's own
        classic confinement is left as it is, because confinement is never a diff.
        """
        source_listing = _HEADER + "zellij    0.44.1     65     latest/stable   dominz88     -\n"
        target_listing = _HEADER + "zellij    0.43.0     43     latest/stable   dominz88     classic\n"
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, source_listing, "")},
            target_responses={"snap list --all": CommandResult(0, target_listing, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        zellij = next(d for d in plan.diffs if d.item_id == "snap:zellij")

        await job.converge(zellij)

        commands = all_calls(target)
        assert any("sudo snap refresh --revision=65 zellij" in c for c in commands)
        assert not any("--classic" in c or "--devmode" in c for c in commands)

    @pytest.mark.asyncio
    async def test_confinement_difference_alone_produces_no_diff(self) -> None:
        """E35 — confinement is a FIELD, never identity and never a diff of its own: same name,
        channel and revision on both sides yields nothing to converge even when the Notes
        column disagrees (there is no command that would resolve it).
        """
        source = _HEADER + "zellij    0.44.1     65     latest/stable   dominz88     classic\n"
        target = _HEADER + "zellij    0.44.1     65     latest/stable   dominz88     -\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()


# Sideloaded fixtures: a snap installed from a local `.snap` file gets a store-less
# revision, rendered `x<N>`, and typically tracks no channel (`-`).
SNAP_LIST_SOURCE_SIDELOADED = (
    _HEADER
    + "homemade  1.0        x1     -               -            try\n"
    + "beta      2.0        20     latest/stable   pub✓         -\n"
)
SNAP_LIST_SOURCE_SIDELOADED_HELD = (
    _HEADER
    + "homemade  1.0        x1     -               -            try,held\n"
    + "beta      2.0        20     latest/stable   pub✓         -\n"
)


class TestSideloadedSnaps:
    """E38-E47, `PKG-FR-SNAP-SIDELOAD` — a snap installed from a local file (`snap install
    --dangerous`, `snap try`) sits at a store-less `x<N>` revision no store can serve.
    Such snaps are out of scope (#221) and ignored on both machines: every diff the name
    could produce is dropped at plan time, and the run says nothing about them.
    """

    @pytest.mark.asyncio
    async def test_sideloaded_source_snap_produces_no_diff(self) -> None:
        """E38 — a sideload only Atlas has is not offered for install."""
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_SIDELOADED, "")},
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == ["snap:beta"]

    @pytest.mark.asyncio
    async def test_a_marked_sideloaded_snap_still_produces_no_diff(self) -> None:
        """E46 — the machine-specific mark changes nothing for a sideload: the name is already
        withheld on both machines, so a marked one is neither an item nor a second reason to
        become one.
        """
        decisions = (
            "machine_specific:\n"
            '  "snap:homemade":\n'
            "    item_class: snap\n"
            "    label: homemade\n"
            "    reason: null\n"
            "    recorded_at: '2026-07-30T00:00:00+00:00'\n"
        )
        context, _source, _target = make_context(
            source_responses={
                "snap list --all": CommandResult(0, SNAP_LIST_SOURCE_SIDELOADED, ""),
                "snap.decisions.yaml": CommandResult(0, decisions, ""),
            },
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == ["snap:beta"]

    @pytest.mark.asyncio
    async def test_sideloaded_snap_that_is_held_produces_no_hold_diff_either(self) -> None:
        """E44 — the hold diff derives from the SOURCE snap (`_diff_snap_holds`), so dropping the
        snap must drop its hold with it — otherwise the run would propose holding a snap it
        just declined to install.
        """
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_SIDELOADED_HELD, "")},
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id.startswith("snap:hold:") for d in plan.diffs)
        assert [d.item_id for d in plan.diffs] == ["snap:beta"]

    @pytest.mark.asyncio
    async def test_store_snaps_in_the_same_listing_still_diff_and_converge(self) -> None:
        """E47 — the filter is surgical: beta's revision difference still converges normally
        alongside the dropped sideloaded snap.
        """
        target = _HEADER + "beta      1.5        15     latest/stable   pub✓         -\n"
        context, _source, target_mock = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_SIDELOADED, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()

        beta = next(d for d in plan.diffs if d.item_id == "snap:beta")
        assert beta.action == DiffAction.CHANGE
        await job.converge(beta)

        commands = all_calls(target_mock)
        assert any("sudo snap refresh --revision=20 beta" in c for c in commands)
        assert not any("homemade" in c for c in commands)

    @pytest.mark.asyncio
    async def test_target_only_sideloaded_snap_is_not_offered_for_removal(self) -> None:
        """E39 — the tool must not offer to delete a snap it cannot reinstall: a sideloaded snap
        only the target has is left alone, not turned into a removal candidate.
        """
        target = _HEADER + "orphan    9.0        x3     -               -            try\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_store_snap_the_target_sideloaded_under_the_same_name_produces_no_diff(self) -> None:
        """E42 — the target's sideloaded copy is what `snap install` would have to displace, so
        the name is withheld on both machines rather than offered as an install.
        """
        source = _HEADER + "beta      2.0        20     latest/stable   pub✓         -\n"
        target = _HEADER + "beta      1.0        x1     -               -            try\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_sideloaded_source_snap_withholds_the_targets_store_copy_too(self) -> None:
        """E43 — the mirror of the case above it: the name is withheld on BOTH machines, so
        a snap the source can no longer describe is never turned into a removal on the target.
        """
        source = _HEADER + "beta      1.0        x1     -               -            try\n"
        target = _HEADER + "beta      2.0        20     latest/stable   pub✓         -\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_hold_on_a_name_sideloaded_on_the_other_machine_yields_no_hold_diff(self) -> None:
        """E45 — the target holds `beta`, which is a sideload there; the source has `beta`
        from the store, unheld. Withholding the name on both machines takes its hold with
        it, so the run never proposes unholding a snap it is deliberately ignoring.
        """
        source = _HEADER + "beta      2.0        20     latest/stable   pub✓         -\n"
        target = _HEADER + "beta      1.0        x1     -               -            try,held\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, source, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id.startswith("snap:hold:") for d in plan.diffs)
        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_sideloaded_snap_present_on_both_is_not_proposed_for_removal(self) -> None:
        """E40 — dropping the source snap must not orphan the target's copy into an
        EXTRA_ON_TARGET removal — "cannot reproduce this" must never become "delete it".
        """
        target = _HEADER + "homemade  1.0        x1     -               -            try\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE_SIDELOADED, "")},
            target_responses={"snap list --all": CommandResult(0, target, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id == "snap:homemade" for d in plan.diffs)


class TestConvergeRemoval:
    @pytest.mark.asyncio
    async def test_removal_never_passes_purge(self) -> None:
        """E36 — the removal carries no purge, so snapd's pre-removal snapshot survives it."""
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
        """E104, E105, K80 — the revision `current` resolves to is mirrored (kept OUT of the exclude set,
        decision 3) where the target is on that same revision; every retained OLDER revision
        dir is excluded, and `common`/`current` are always kept.
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

        paths = snap_sync_exclude_paths({"firefox": "2938"})

        assert old_rev in paths  # retained old revision the target never installed
        assert current_rev not in paths  # active-revision data dir travels
        assert not any(p.name == "common" for p in paths)
        assert not any(p.name == "current" for p in paths)

    def test_a_revision_the_target_did_not_converge_to_is_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """K82 — the user skipped `firefox`'s revision change (or it failed), so the target is
        still on the old revision: the source's active-revision data dir is excluded like any
        other revision the target's snapd never installed.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        firefox_dir = tmp_path / "snap" / "firefox"
        current_rev = firefox_dir / "2938"
        current_rev.mkdir(parents=True)
        (firefox_dir / "current").symlink_to(current_rev, target_is_directory=True)

        assert snap_sync_exclude_paths({"firefox": "2911"}) == [current_rev]

    def test_a_snap_the_target_does_not_hold_has_every_revision_dir_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E115 — `alpha`'s install was declined or failed, so the target holds no revision of it
        at all and its data dir stays home.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        alpha_dir = tmp_path / "snap" / "alpha"
        current_rev = alpha_dir / "10"
        current_rev.mkdir(parents=True)
        (alpha_dir / "current").symlink_to(current_rev, target_is_directory=True)

        assert snap_sync_exclude_paths({"firefox": "2938"}) == [current_rev]

    def test_unknown_target_revisions_exclude_every_revision_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """K81 — with no revision map (snap_sync off, or the target's snapd could not be asked)
        nothing is known to have been installed there, so no revision dir is mirrored.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        firefox_dir = tmp_path / "snap" / "firefox"
        current_rev = firefox_dir / "2938"
        current_rev.mkdir(parents=True)
        (firefox_dir / "current").symlink_to(current_rev, target_is_directory=True)

        assert snap_sync_exclude_paths(None) == [current_rev]

    def test_dangling_current_falls_back_to_excluding_all_revisions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E106, K78 — a dangling `current` means the active revision is indeterminate, so every
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

        paths = snap_sync_exclude_paths({"firefox": "2938"})

        assert rev_a in paths
        assert rev_b in paths

    def test_missing_current_symlink_falls_back_to_excluding_all_revisions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """E107 — the same fallback when there is no `current` symlink at all."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        firefox_dir = tmp_path / "snap" / "firefox"
        rev = firefox_dir / "2938"
        rev.mkdir(parents=True)
        # No `current` symlink created at all.

        paths = snap_sync_exclude_paths({"firefox": "2938"})

        assert rev in paths

    def test_no_snap_directory_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """E108 — no `~/snap` at all: nothing excluded, nothing raised."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert snap_sync_exclude_paths({"firefox": "2938"}) == []


class TestTargetSnapRevisions:
    """The target's own revision listing, which is what decides whether a `~/snap` data dir
    may be mirrored (`PKG-FR-SNAP-DATA-BOUNDARY`).
    """

    @pytest.mark.asyncio
    async def test_the_listing_becomes_a_name_to_revision_map(self) -> None:
        """K82 — read from the target, after the package jobs ran."""
        target = MagicMock()
        target.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout=(
                    "Name     Version  Rev   Tracking       Publisher  Notes\n"
                    "firefox  1.0      2938  latest/stable  mozilla    -\n"
                ),
                stderr="",
            )
        )

        assert await target_snap_revisions(target) == {"firefox": "2938"}
        target.run_command.assert_awaited_once_with("snap list --all", login_shell=False)

    @pytest.mark.asyncio
    async def test_a_read_that_did_not_answer_is_not_an_empty_machine(self) -> None:
        """K81 — a snapd that could not be asked says nothing about which revisions exist, so
        the caller gets None (exclude everything), never an empty map read as fact."""
        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(exit_code=1, stdout="", stderr="error"))

        assert await target_snap_revisions(target) is None

    @pytest.mark.asyncio
    async def test_a_machine_with_no_snaps_is_ordinary_data(self) -> None:
        """K81 — exit 0 with an empty listing is a machine holding no snaps, which is a map with
        no entries rather than an unreadable one."""
        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        assert await target_snap_revisions(target) == {}


class TestValidate:
    @pytest.mark.asyncio
    async def test_snap_unavailable_on_source_yields_validation_error(self) -> None:
        """E103, K48 — validation fails naming Atlas when snap is absent there."""
        context, _source, _target = make_context(
            source_responses={"snap version": CommandResult(127, "", "not found")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "snap is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_snap_unavailable_on_target_yields_validation_error(self) -> None:
        """E103, K49 — and naming Nomad when it is absent there."""
        context, _source, _target = make_context(
            target_responses={"snap version": CommandResult(127, "", "not found")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "snap is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_target_without_passwordless_sudo_yields_validation_error(self) -> None:
        """E102, K47 — validation fails naming Nomad, which needs sudo for install/refresh/remove."""
        context, _source, _target = make_context(
            target_responses={"sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_source_without_passwordless_sudo_yields_validation_error(self) -> None:
        """E101 — the source needs passwordless sudo too: the orchestrator pauses snapd
        auto-refresh via `sudo snap set system refresh.hold` on the source as well (decision 4).
        """
        context, _source, _target = make_context(
            source_responses={"sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors(self) -> None:
        """K50, K51, K62."""
        context, _source, _target = make_context()
        job = SnapSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_snap_sync_to_snap_sync_job(self) -> None:
        """K36."""
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


class TestSnapItem:
    def test_reports_its_item_class(self) -> None:
        assert SnapItem.ITEM_CLASS == ItemClass.SNAP

    def test_label_names_the_snap_channel_and_revision(self) -> None:
        """E6 — a snap's identity is `snap:<name>`, the name alone."""
        item = SnapItem(name="firefox", channel="latest/stable", revision="4536")

        assert item.item_id == "snap:firefox"
        label = item.label()
        assert "firefox" in label
        assert "latest/stable" in label
        assert "4536" in label


class TestAProbeThatDidNotAnswer:
    """ADR-022: a `snap list --all` that did not answer fails the job; one that answered
    "no snaps" is data.

    Both halves matter and only the exit code separates them, measured against the real
    `snap` binary: snapd unreachable exits 1, and snapd reporting zero snaps exits 0 with
    the "No snaps are installed yet." hint on STDERR and an empty stdout.
    """

    @pytest.mark.asyncio
    async def test_a_source_list_that_did_not_answer_fails_the_job(self) -> None:
        """E27, J76, J78 — a source listing that exited non-zero fails the job, naming the command."""
        context, _source, _target = make_context(
            source_responses={
                "snap list --all": CommandResult(
                    1, "", "error: cannot list snaps: cannot communicate with server: dial unix /run/snapd.socket\n"
                )
            },
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await SnapSyncJob(context).plan()

        assert "snap list --all" in str(excinfo.value)
        assert "exited 1" in str(excinfo.value)
        assert "snapd.socket" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_target_list_that_did_not_answer_fails_the_job(self) -> None:
        """E28, J77 — only the TARGET read fails here (the source answers three snaps), so nothing
        but the target's exit code can produce this."""
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(1, "", "error: cannot communicate with server\n")},
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await SnapSyncJob(context).plan()

        assert "exited 1" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_source_with_no_snaps_installed_is_data_not_a_failure(self) -> None:
        """E26, J87 — the legitimate-empty half, and the hazard the guard above exists for: the same
        empty stdout that a failed read produces is a real answer at exit 0, and it must
        still reach the diff as "remove the target's snaps" rather than fail the job.
        """
        context, _source, _target = make_context(
            source_responses={
                "snap list --all": CommandResult(
                    0, "", "No snaps are installed yet. Try 'snap install hello-world'.\n"
                )
            },
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )

        plan = await SnapSyncJob(context).plan()

        removals = {diff.item_id for diff in plan.diffs if diff.action == DiffAction.REMOVE}
        assert removals == {"snap:beta", "snap:gamma", "snap:delta"}
