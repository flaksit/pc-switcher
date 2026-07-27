"""Unit tests for what `PackageSyncJob` actually shares: review grouping, the converge
dispatch across all four `DiffAction`s, decision-file routing and `execute()`'s order
(D-07/D-24/D-25).

`FakeSyncJob` is a minimal concrete `PackageSyncJob` whose `plan()` diffs items by bare
presence and whose `converge()` only records calls. Deliberately not apt-shaped: a
manager's own diff is that manager's (`_diff_apt_packages` is tested in
`test_apt_sync.py`), and a fake borrowing one would make these tests pass or fail for
reasons that have nothing to do with the shared pipeline.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from pcswitcher.config import Configuration
from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.manual_installs_sync import ManualInstallsSyncJob
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import Decision, ReviewGroup, ReviewOutcome
from pcswitcher.jobs.packages.state import DecisionFile, filter_inert
from pcswitcher.jobs.packages.sync_core import (  # pyright: ignore[reportPrivateUsage]
    _ACTION_VOCABULARY,
    PackageItemFailures,
    PackagePlan,
    PackageSyncJob,
)
from pcswitcher.models import CommandResult, JobStatus, LogLevel, ValidationError
from pcswitcher.orchestrator import Orchestrator

DF_OUTPUT = (
    "Filesystem     1B-blocks       Used  Available Use% Mounted on\n"
    "/dev/sda1  1000000000000 500000000000 500000000000  50% /\n"
)


def make_context(
    *,
    dry_run: bool = False,
    reviewer: object | None = None,
    enabled_sync_jobs: dict[str, bool] | None = None,
) -> JobContext:
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
    target = MagicMock()
    target.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
    return JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
        reviewer=reviewer,  # pyright: ignore[reportArgumentType]
        enabled_sync_jobs=enabled_sync_jobs,
    )


class FakeReviewer:
    """A `Reviewer` that returns a caller-supplied outcome and records the groups it saw.

    Reusable across the package-sync tests wherever the subject is a whole `execute()` run
    rather than `apply()` in isolation: a test supplies a `decisions` map keyed by item id
    (unlisted items default to `SKIP_ONCE`) and can afterwards inspect `groups_seen` to
    assert what the job actually presented for review.
    """

    def __init__(
        self,
        decisions: dict[str, Decision] | None = None,
        *,
        was_interactive: bool = True,
    ) -> None:
        self._decisions = decisions or {}
        self._was_interactive = was_interactive
        self.groups_seen: tuple[ReviewGroup, ...] | None = None
        self.call_count = 0

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self.call_count += 1
        self.groups_seen = tuple(groups)
        item_ids = {entry.item_id for group in groups for entry in group.entries}
        decisions = {item_id: self._decisions.get(item_id, Decision.SKIP_ONCE) for item_id in item_ids}
        return ReviewOutcome(decisions=decisions, was_interactive=self._was_interactive)


def _diff(
    item_id: str, action: DiffAction, diff_class: DiffClass = DiffClass.MISSING_ON_TARGET, detail: str | None = None
) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=diff_class,
        action=action,
        item_id=item_id,
        label=item_id,
        detail=detail,
    )


@dataclass(frozen=True)
class FakeItem:
    """The least an item can be and still flow through the shared pipeline: an id and a
    label. No manager's fields, so nothing these tests assert can be an accident of one
    ecosystem's shape.
    """

    name: str

    @property
    def item_id(self) -> str:
        return f"fake:{self.name}"

    def label(self) -> str:
        return self.name


class FakeSyncJob(PackageSyncJob):
    """Minimal concrete `PackageSyncJob`: items in, presence-diffed, a recording converge().

    Reusable by any test whose subject is the shared pipeline rather than a manager
    (`test_package_state.py` drives its decision-file routing through this class).
    """

    name: ClassVar[str] = "fake_sync"
    manager_id: ClassVar[str] = "fake"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        context: JobContext,
        *,
        source_items: Sequence[FakeItem] = (),
        target_items: Sequence[FakeItem] = (),
    ) -> None:
        super().__init__(context)
        self._source_items = tuple(source_items)
        self._target_items = tuple(target_items)
        self.converge_calls: list[ItemDiff] = []

    async def validate(self) -> list[Any]:
        return []

    async def plan(self) -> PackagePlan:
        """The skeleton every real `plan()` follows -- load both decision files, filter
        each side through its own, diff, drop what is inert, build groups -- with the
        simplest diff that exists: present on one side only.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        target_decisions = await DecisionFile(self.manager_id, self.target).load()
        self._plan_decisions = (source_decisions, target_decisions)

        source_items = await filter_inert(self._source_items, source_decisions)
        target_items = await filter_inert(self._target_items, target_decisions)
        source_ids = {item.item_id: item for item in source_items}
        target_ids = {item.item_id: item for item in target_items}
        diffs = [
            _diff(item.item_id, DiffAction.INSTALL) for item in source_items if item.item_id not in target_ids
        ] + [
            _diff(item.item_id, DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET)
            for item in target_items
            if item.item_id not in source_ids
        ]
        kept = self._drop_inert_diffs(diffs, source_decisions, target_decisions)
        return PackagePlan(manager=self.manager_id, diffs=kept, groups=self._build_review_groups(kept))

    async def converge(self, diff: ItemDiff) -> CommandResult:
        self.converge_calls.append(diff)
        return CommandResult(0, "", "")


def _accept(job: PackageSyncJob, diffs: tuple[ItemDiff, ...], decisions: dict[str, Decision]) -> PackagePlan:
    plan = PackagePlan(manager=job.manager_id, diffs=diffs, groups=job._build_review_groups(diffs))
    job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))
    return plan


class TestReviewGroupsByAction:
    """`_build_review_groups`: one group per action, removal titles name the verb."""

    def test_four_diffs_produce_four_groups_keyed_by_action(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = [
            _diff("i1", DiffAction.INSTALL),
            _diff("c1", DiffAction.CHANGE, DiffClass.VERSION_MISMATCH),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
            _diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),
        ]

        groups = job._build_review_groups(diffs)

        assert len(groups) == 4
        assert {g.action for g in groups} == {"install", "change", "remove", "report_only"}

    def test_group_emission_order_is_install_change_remove_report(self) -> None:
        """Fixed action order (must-have): install, then change, then remove, then report.
        Two runs over the same diff set present the same order, regardless of diff input
        order — so the diffs are supplied here deliberately shuffled.
        """
        job = FakeSyncJob(make_context())
        diffs = [
            _diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
            _diff("i1", DiffAction.INSTALL),
            _diff("c1", DiffAction.CHANGE, DiffClass.VERSION_MISMATCH),
        ]

        groups = job._build_review_groups(diffs)

        assert [g.action for g in groups] == ["install", "change", "remove", "report_only"]

    def test_report_only_falls_back_to_report_for_a_class_with_no_vocabulary_entry(self) -> None:
        """IN-01 regression: `_ACTION_VOCABULARY` only lists an explicit REPORT_ONLY
        entry for APT_PACKAGE. Every other item class's REPORT_ONLY diff must still
        fall back to the word "report", not the raw enum value "report_only" (which
        produced a title like "Report_only fake packages").
        """
        job = FakeSyncJob(make_context())
        diffs = [
            ItemDiff(
                item_class=ItemClass.FLATPAK_REF,
                diff_class=DiffClass.VERSION_MISMATCH,
                action=DiffAction.REPORT_ONLY,
                item_id="p1",
                label="p1",
                detail=None,
            )
        ]

        groups = job._build_review_groups(diffs)

        assert len(groups) == 1
        assert groups[0].entries[0].action_label == "report"
        assert "report_only" not in groups[0].title.lower()
        assert "report" in groups[0].title.lower()

    def test_every_pair_without_a_vocabulary_entry_still_produces_a_usable_group(self) -> None:
        """I5b: `_ACTION_VOCABULARY` covers only the (item_class, action) pairs the four
        managers produce today. The backstop the pipeline promises is that ANY other pair
        still reaches the review with a usable verb — no diff class may silently vanish
        because nobody added a vocabulary row for it. Asserted over every uncovered pair
        rather than one sampled class, since the failure mode is exactly "the new class
        nobody thought about".
        """
        job = FakeSyncJob(make_context())
        uncovered = [
            (item_class, action)
            for item_class in ItemClass
            for action in DiffAction
            if (item_class, action) not in _ACTION_VOCABULARY
        ]
        assert uncovered, "every pair is in the vocabulary — the fallback backstop is untested"

        for item_class, action in uncovered:
            diffs = [
                ItemDiff(
                    item_class=item_class,
                    diff_class=DiffClass.MISSING_ON_TARGET,
                    action=action,
                    item_id="x1",
                    label="x1",
                    detail=None,
                )
            ]

            groups = job._build_review_groups(diffs)

            assert len(groups) == 1, f"{item_class}/{action} produced no review group"
            verb = groups[0].entries[0].action_label
            assert verb, f"{item_class}/{action} produced an empty verb"
            # A raw enum value ("report_only") is not a verb; the fallback must read as one.
            assert "_" not in verb
            assert verb in groups[0].title.lower()
            assert [entry.item_id for entry in groups[0].entries] == ["x1"]

    def test_removal_group_title_names_a_removal_verb_never_apply(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = [_diff("i1", DiffAction.INSTALL), _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET)]

        groups = job._build_review_groups(diffs)

        install_group = next(g for g in groups if g.action == "install")
        remove_group = next(g for g in groups if g.action == "remove")
        assert install_group.title != remove_group.title
        assert "remove" in remove_group.title.lower()
        assert "apply" not in remove_group.title.lower()
        assert "apply" not in install_group.title.lower()


class TestConvergeDispatchByAction:
    """`apply()` routes INSTALL/REMOVE/CHANGE to `converge()`; REPORT_ONLY never reaches it."""

    @pytest.mark.asyncio
    async def test_remove_diff_produces_exactly_one_target_converge_call(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = (_diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),)
        _accept(job, diffs, {"r1": Decision.APPLY})

        await job.apply()

        assert len(job.converge_calls) == 1
        assert job.converge_calls[0].action == DiffAction.REMOVE

    @pytest.mark.asyncio
    async def test_change_diff_reaches_converge_alongside_install_and_remove(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = (_diff("c1", DiffAction.CHANGE, DiffClass.VERSION_MISMATCH),)
        _accept(job, diffs, {"c1": Decision.APPLY})

        await job.apply()

        assert [d.item_id for d in job.converge_calls] == ["c1"]

    @pytest.mark.asyncio
    async def test_report_only_diff_produces_zero_target_commands(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = (_diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),)
        _accept(job, diffs, {"p1": Decision.APPLY})

        await job.apply()

        assert job.converge_calls == []

    @pytest.mark.asyncio
    async def test_ticking_only_install_group_yields_zero_removal_commands(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = (
            _diff("i1", DiffAction.INSTALL),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
        )
        _accept(job, diffs, {"i1": Decision.APPLY, "r1": Decision.SKIP_ONCE})

        await job.apply()

        assert [d.item_id for d in job.converge_calls] == ["i1"]

    @pytest.mark.asyncio
    async def test_dry_run_zero_mutating_commands_across_all_four_action_types(self) -> None:
        job = FakeSyncJob(make_context(dry_run=True))
        diffs = (
            _diff("i1", DiffAction.INSTALL),
            _diff("c1", DiffAction.CHANGE, DiffClass.VERSION_MISMATCH),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
            _diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),
        )
        _accept(job, diffs, {d.item_id: Decision.APPLY for d in diffs})

        await job.apply()

        assert job.converge_calls == []

    @pytest.mark.asyncio
    async def test_dry_run_preview_carries_each_items_detail(self, caplog: pytest.LogCaptureFixture) -> None:
        """ADR-014: the dry run reports exactly what would happen, and for several diff
        classes the WHAT lives in `detail` — the signing keys an apt source install
        copies, the two versions behind a mismatch. A dry run renders no review panel,
        so dropping the detail here is the difference between a preview and a bare list
        of item names.
        """
        caplog.set_level(LogLevel.FULL.value, logger="pcswitcher.jobs.base")
        job = FakeSyncJob(make_context(dry_run=True))
        diffs = (
            _diff("i1", DiffAction.INSTALL, detail="signing key copied with it: vendor.gpg"),
            _diff("i2", DiffAction.INSTALL),
        )
        _accept(job, diffs, {d.item_id: Decision.APPLY for d in diffs})

        await job.apply()

        assert "[dry-run] Would install i1 — signing key copied with it: vendor.gpg" in caplog.messages
        assert "[dry-run] Would install i2" in caplog.messages


class TestIdempotency:
    """J10/N2: a run over an ALREADY-converged pair is a no-op end to end.

    Asserted on the shared pipeline so it holds for every manager rather than for one
    manager's capture code: identical item sets must produce zero diffs, zero review
    groups, no converge and no command carrying `mutates=`. Pins that nothing between
    plan() and apply() has an "always re-propose" branch — the property a real second
    sync depends on. Each manager's own diff carries the same property for its own item
    classes; apt's is covered in `test_apt_sync.py`.
    """

    @pytest.mark.asyncio
    async def test_identical_source_and_target_produce_no_diff_no_group_and_no_mutation(self) -> None:
        reviewer = FakeReviewer()
        context = make_context(reviewer=reviewer)
        items = [FakeItem(name="pkg-a"), FakeItem(name="pkg-b")]
        job = FakeSyncJob(context, source_items=items, target_items=list(items))

        plan = await job.plan()
        assert plan.diffs == ()
        assert plan.groups == ()

        await job.execute()

        assert job.converge_calls == []
        # The review still runs once (I18), with nothing to show.
        assert reviewer.call_count == 1
        assert reviewer.groups_seen == ()
        for executor in (context.source, context.target):
            for call in executor.run_command.call_args_list:  # pyright: ignore[reportAttributeAccessIssue]
                assert "mutates" not in call.kwargs


def _unreproducible_diff(item_id: str, action: DiffAction = DiffAction.REPORT_ONLY) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.UNREPRODUCIBLE,
        diff_class=DiffClass.UNREPRODUCIBLE,
        action=action,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


class _FakeManualJob(ManualInstallsSyncJob):
    """A `ManualInstallsSyncJob` with `manager_id="fake"` so the moved finalize hook's
    decision-file assertions keep reading `fake.decisions` (D-18: finalize/unresolved
    now live on this job, not the base)."""

    name: ClassVar[str] = "fake_manual"
    manager_id: ClassVar[str] = "fake"


class TestFinalizeUnreproducible:
    """D-20/D-21/D-23: the `_finalize_unreproducible` hook (owned by
    `ManualInstallsSyncJob`, D-18) writes this run's snippet authoring and
    unreproducible-item skip-always decisions, both to the SOURCE — never the target, and
    never during a dry run or a non-interactive outcome.
    """

    @pytest.mark.asyncio
    async def test_authored_snippet_is_written_to_the_source_registry_not_target(self) -> None:
        context = make_context()
        job = _FakeManualJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(
            plan,
            ReviewOutcome(decisions={}, was_interactive=True, snippets={diff.item_id: "sudo dpkg -i /tmp/x.deb"}),
        )

        await job.apply()

        source_cmds = [c.args[0] for c in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        target_cmds = [c.args[0] for c in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv -f" in cmd and "package-snippets" in cmd for cmd in source_cmds)
        assert not any("package-snippets" in cmd for cmd in target_cmds)

    @pytest.mark.asyncio
    async def test_skip_always_on_unreproducible_item_records_on_source(self) -> None:
        context = make_context()
        job = _FakeManualJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        source_cmds = [c.args[0] for c in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        target_cmds = [c.args[0] for c in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv -f" in cmd and "fake.decisions" in cmd for cmd in source_cmds)
        assert not any("fake.decisions" in cmd for cmd in target_cmds)

    @pytest.mark.asyncio
    async def test_no_finalize_writes_during_dry_run(self) -> None:
        context = make_context(dry_run=True)
        job = _FakeManualJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={diff.item_id: Decision.SKIP_ALWAYS},
                was_interactive=True,
                snippets={diff.item_id: "echo x"},
            ),
        )

        await job.apply()

        for cmd in [c.args[0] for c in context.source.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv -f" not in cmd

    @pytest.mark.asyncio
    async def test_no_finalize_writes_when_outcome_not_interactive(self) -> None:
        context = make_context()
        job = _FakeManualJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={diff.item_id: Decision.SKIP_ALWAYS},
                was_interactive=False,
                snippets={diff.item_id: "echo x"},
            ),
        )

        await job.apply()

        for cmd in [c.args[0] for c in context.source.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv -f" not in cmd


class TestBaseHooksAreNoOps:
    """D-18: a manager with no unreproducible items (the base `FakeSyncJob`, standing in
    for apt/snap/flatpak) inherits no-op finalize/unresolved hooks — an unresolved list or
    authored snippet on such a job's outcome is ignored, never raising and never writing.
    """

    @pytest.mark.asyncio
    async def test_base_unresolved_hook_is_no_op_and_does_not_raise(self) -> None:
        job = FakeSyncJob(make_context())
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True, unresolved=(diff.item_id,)))

        await job.apply()  # base _unresolved_as_failures is a no-op -> no PackageItemFailures

    @pytest.mark.asyncio
    async def test_base_finalize_hook_writes_nothing(self) -> None:
        context = make_context()
        job = FakeSyncJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={diff.item_id: Decision.SKIP_ALWAYS},
                was_interactive=True,
                snippets={diff.item_id: "echo x"},
            ),
        )

        await job.apply()

        for cmd in [c.args[0] for c in context.source.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "package-snippets" not in cmd
            assert "fake.decisions" not in cmd


class _OrderRecordingJob(FakeSyncJob):
    """`FakeSyncJob` that records the order of plan/accept_review/apply on `events`, so a
    test can assert `apply` is never reached before `review` returns.
    """

    def __init__(self, context: JobContext, events: list[str]) -> None:
        super().__init__(context)
        self._events = events

    async def plan(self) -> PackagePlan:
        self._events.append("plan")
        return await super().plan()

    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        self._events.append("accept_review")
        super().accept_review(plan, outcome)

    async def after_review(self) -> None:
        self._events.append("after_review")
        await super().after_review()

    async def apply(self) -> None:
        self._events.append("apply")
        await super().apply()


class _RecordingReviewer(FakeReviewer):
    """`FakeReviewer` that also appends `review` to a shared event list, to pin call order."""

    def __init__(self, events: list[str], decisions: dict[str, Decision] | None = None) -> None:
        super().__init__(decisions)
        self._events = events

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self._events.append("review")
        return await super().review(groups)


class _RaisingPlanJob(FakeSyncJob):
    """A job whose `plan()` raises, to prove the failure propagates out of `execute()`."""

    def __init__(self, context: JobContext, error: Exception) -> None:
        super().__init__(context)
        self._error = error

    async def plan(self) -> PackagePlan:
        raise self._error


class TestExecuteSelfContained:
    """`execute()` is self-contained (D-24): plan -> review -> accept_review -> apply,
    with the review reached through the injected `JobContext.reviewer`.
    """

    @pytest.mark.asyncio
    async def test_call_order_is_plan_review_accept_review_apply(self) -> None:
        events: list[str] = []
        reviewer = _RecordingReviewer(events)
        job = _OrderRecordingJob(make_context(reviewer=reviewer), events)

        await job.execute()

        # after_review is the seam between accept_review and apply (D-23: where
        # manual_installs_sync pushes its snippet registry before any converge).
        assert events == ["plan", "review", "accept_review", "after_review", "apply"]
        # apply must never precede review returning.
        assert events.index("apply") > events.index("review")
        assert events.index("after_review") > events.index("accept_review")
        assert events.index("after_review") < events.index("apply")

    @pytest.mark.asyncio
    async def test_zero_diff_run_still_calls_review_once(self) -> None:
        events: list[str] = []
        reviewer = _RecordingReviewer(events)
        job = _OrderRecordingJob(make_context(reviewer=reviewer), events)

        await job.execute()

        # FakeSyncJob has empty capture/query, so the plan carries no diffs; the reviewer
        # is still consulted exactly once (with an empty group tuple).
        assert reviewer.call_count == 1
        assert reviewer.groups_seen == ()

    @pytest.mark.asyncio
    async def test_missing_reviewer_raises_and_issues_no_converge(self) -> None:
        job = FakeSyncJob(make_context())  # reviewer defaults to None

        with pytest.raises(AssertionError, match="no reviewer"):
            await job.execute()

        assert job.converge_calls == []
        job.context.target.run_command.assert_not_called()  # pyright: ignore[reportAttributeAccessIssue]

    @pytest.mark.asyncio
    async def test_plan_failure_propagates_out_of_execute_unchanged(self) -> None:
        failure = RuntimeError("manifest capture blew up")
        reviewer = FakeReviewer()
        job = _RaisingPlanJob(make_context(reviewer=reviewer), failure)

        with pytest.raises(RuntimeError, match="manifest capture blew up") as exc_info:
            await job.execute()

        assert exc_info.value is failure
        # The review is never reached when planning fails.
        assert reviewer.call_count == 0


class TestJobContextEnabledSyncJobs:
    """`JobContext.enabled_sync_jobs` is optional with a `None` default (a sibling of the
    `reviewer`/`confirmer` fields), so lightweight test contexts keep working.
    """

    def test_defaults_to_none_and_does_not_raise(self) -> None:
        context = make_context()
        assert context.enabled_sync_jobs is None

    def test_can_be_populated_with_the_full_enablement_map(self) -> None:
        context = make_context(enabled_sync_jobs={"apt_sync": True, "folder_sync": False})
        assert context.enabled_sync_jobs == {"apt_sync": True, "folder_sync": False}


class _StubFailingPackageJob(PackageSyncJob):
    """A package job whose execute() raises PackageItemFailures directly, isolating the
    orchestrator's except-chain branch (D-27): its items failed, so the run continues.
    """

    name: ClassVar[str] = "stub_failing_package"
    manager_id: ClassVar[str] = "stub-failing"

    async def converge(self, diff: ItemDiff) -> CommandResult:
        raise NotImplementedError

    async def validate(self) -> list[ValidationError]:
        return []

    async def plan(self) -> PackagePlan:
        return PackagePlan(manager="stub-failing", diffs=(), groups=())

    async def execute(self) -> None:
        raise PackageItemFailures("stub-failing", [])


class _StubSuccessJob(SyncJob):
    name: ClassVar[str] = "stub_success"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        return None


class _StubOtherFailureJob(SyncJob):
    """A non-package job whose execute() raises a plain exception — the abort-the-run
    path that only `PackageItemFailures` is exempt from; every other exception still
    stops the remaining jobs.
    """

    name: ClassVar[str] = "stub_other_failure"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        raise RuntimeError("unrelated job crashed")


def _make_wired_orchestrator() -> Orchestrator:
    """A narrowly-constructed Orchestrator with enough wiring for `_execute_jobs` /
    `_run_jobs_in_task_group` to run: mocked local/remote executors returning valid `df`
    output for the background disk-space monitors, a non-interactive Console, and a
    silenced logger/UI.
    """
    config = MagicMock(spec=Configuration)
    config.logging = MagicMock()
    config.logging.file = 10
    config.logging.tui = 20
    config.logging.external = 30
    config.sync_jobs = {}
    config.job_configs = {}
    config.disk = MagicMock()
    config.disk.preflight_minimum = "20%"
    config.disk.runtime_minimum = "15%"
    config.disk.warning_threshold = "25%"
    config.disk.check_interval = 30

    orchestrator = Orchestrator(target="target-host", config=config)
    orchestrator._console = Console(file=io.StringIO())  # pyright: ignore[reportPrivateUsage]
    orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    local_executor = MagicMock()
    local_executor.run_command = AsyncMock(return_value=CommandResult(0, DF_OUTPUT, ""))
    remote_executor = MagicMock()
    remote_executor.run_command = AsyncMock(return_value=CommandResult(0, DF_OUTPUT, ""))
    orchestrator._local_executor = local_executor  # pyright: ignore[reportPrivateUsage]
    orchestrator._remote_executor = remote_executor  # pyright: ignore[reportPrivateUsage]
    return orchestrator


class TestOrchestratorPackageItemFailuresContinuation:
    """PackageItemFailures records a FAILED JobResult but does not abort the run (D-27).

    Re-homed from the deleted test_package_phase.py: the coordinator is gone, but the
    orchestrator's per-job except chain that this exercises is a delivered behaviour.
    """

    @pytest.mark.asyncio
    async def test_failing_package_job_does_not_cancel_remaining_jobs(self) -> None:
        orchestrator = _make_wired_orchestrator()
        failing_job = _StubFailingPackageJob(make_context())
        success_job = _StubSuccessJob(make_context())

        results = await orchestrator._execute_jobs([failing_job, success_job])  # pyright: ignore[reportPrivateUsage]

        assert len(results) == 2
        assert results[0].job_name == "stub_failing_package"
        assert results[0].status == JobStatus.FAILED
        assert results[1].job_name == "stub_success"
        assert results[1].status == JobStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_other_exception_types_still_abort_the_run(self) -> None:
        """Regression guard: only PackageItemFailures gets the non-aborting branch —
        every other exception must still stop the remaining jobs from running.
        """
        orchestrator = _make_wired_orchestrator()
        failing_job = _StubOtherFailureJob(make_context())
        never_run_job = _StubSuccessJob(make_context())

        with pytest.raises(RuntimeError, match="unrelated job crashed"):
            await orchestrator._execute_jobs([failing_job, never_run_job])  # pyright: ignore[reportPrivateUsage]
