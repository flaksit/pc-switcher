"""Unit tests for what `PackageSyncJob` actually shares: review grouping, the converge
dispatch across all four `DiffAction`s, decision-file routing and `execute()`'s order
(D-07/D-24/D-25).

`FakeSyncJob` is a minimal concrete `PackageSyncJob` whose `plan()` diffs items by bare
presence and whose `converge()` only records calls. Deliberately not apt-shaped: a
manager's own diff is that manager's (`diff_apt_packages` is tested in
`test_apt_sync.py`), and a fake borrowing one would make these tests pass or fail for
reasons that have nothing to do with the shared pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

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
from pcswitcher.models import CommandResult, JobSkipped, JobStatus, LogLevel, SyncAbortedByUser, ValidationError
from pcswitcher.orchestrator import Orchestrator


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
        # Gate answers, consumed in order; the list is also the record of what was asked.
        # Empty means "this test's job must not reach a gate" — the pop raises if it does.
        self.gate_answers: list[bool | None] = []
        self.gate_calls: list[dict[str, str]] = []

    async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None:
        self.gate_calls.append(
            {"title": title, "message": message, "proceed_label": proceed_label, "stop_label": stop_label}
        )
        return self.gate_answers.pop(0)

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


def _job_for_manager(manager_id: str) -> FakeSyncJob:
    """A `FakeSyncJob` wearing a real manager's id, for the strings keyed on it."""

    class _Named(FakeSyncJob):
        pass

    _Named.manager_id = manager_id
    return _Named(make_context())


def _accept(job: PackageSyncJob, diffs: tuple[ItemDiff, ...], decisions: dict[str, Decision]) -> PackagePlan:
    plan = PackagePlan(manager=job.manager_id, diffs=diffs, groups=job._build_review_groups(diffs))
    job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))
    return plan


class TestReviewGroupsByAction:
    """`_build_review_groups`: one group per action, removal titles name the verb."""

    def test_four_diffs_produce_four_groups_keyed_by_action(self) -> None:
        """H97 — installs and removals never share a group: one group per action."""
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

    def test_a_report_group_is_titled_by_its_cause_not_by_the_word_report(self) -> None:
        """H69 — Ruled by the user: "Report apt packages" named none of the three conditions it
        could hold. The title is the cause — the `DiffClass` — and the raw enum value
        ("report_only", which once produced "Report_only fake packages") reaches no title
        in either shape.
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
        assert groups[0].title == "Version differences (fake packages)"
        assert "report_only" not in groups[0].title.lower()
        # No upgrade command is known for a made-up manager, so the note is empty and is
        # dropped rather than printed as a sentence with a hole in it.
        assert groups[0].note is None

    def test_the_origin_report_title_names_the_machine_that_cannot_reproduce_them(self) -> None:
        """H69 — `PKG-FR-NAME-THE-MACHINES`: this is the one report title with a machine in
        it, and it is filled from the run's own hostnames rather than saying "the target".
        """
        job = FakeSyncJob(make_context())
        diffs = [_diff("o1", DiffAction.REPORT_ONLY, DiffClass.REPO_UNAVAILABLE)]

        groups = job._build_review_groups(diffs)

        assert groups[0].title == "Origins target-host cannot reproduce (fake packages)"
        assert "target" not in groups[0].title.replace("target-host", "")

    def test_every_pair_without_a_vocabulary_entry_still_produces_a_usable_group(self) -> None:
        """H174 — `_ACTION_VOCABULARY` covers only the (item_class, action) pairs the four
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

    def test_only_an_apt_config_change_is_flagged_as_overwriting_the_users_own_content(self) -> None:
        """H103, H104 — `PKG-FR-HARMLESS-DEFAULT`: replacing an `/etc/apt/apt.conf.d` file the target
        already holds destroys something the user wrote there, so it must not be the answer
        confirming a screen unread produces. Every other CHANGE converges software the user
        asked for and stays preselected.
        """
        job = FakeSyncJob(make_context())
        diffs = [
            ItemDiff(
                item_class=item_class,
                diff_class=DiffClass.VERSION_MISMATCH,
                action=DiffAction.CHANGE,
                item_id=f"{item_class.value}:c1",
                label="c1",
                detail=None,
            )
            for item_class in (ItemClass.APT_CONFIG, ItemClass.SNAP, ItemClass.APT_PACKAGE)
        ]

        groups = job._build_review_groups(diffs)

        flagged = {g.entries[0].item_id for g in groups if g.overwrites_authored_content}
        assert flagged == {"apt_config:c1"}

    def test_an_apt_config_install_is_not_an_overwrite(self) -> None:
        """H105 — A file the target does not have yet displaces nothing."""
        job = FakeSyncJob(make_context())
        diffs = [
            ItemDiff(
                item_class=ItemClass.APT_CONFIG,
                diff_class=DiffClass.MISSING_ON_TARGET,
                action=DiffAction.INSTALL,
                item_id="apt:config:99proxy",
                label="99proxy",
                detail=None,
            )
        ]

        groups = job._build_review_groups(diffs)

        assert [g.overwrites_authored_content for g in groups] == [False]

    def test_removal_group_title_names_a_removal_verb_never_apply(self) -> None:
        """H83 — a group title names the concrete verb for its item class, never "Apply"."""
        job = FakeSyncJob(make_context())
        diffs = [_diff("i1", DiffAction.INSTALL), _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET)]

        groups = job._build_review_groups(diffs)

        install_group = next(g for g in groups if g.action == "install")
        remove_group = next(g for g in groups if g.action == "remove")
        assert install_group.title != remove_group.title
        assert "remove" in remove_group.title.lower()
        assert "apply" not in remove_group.title.lower()
        assert "apply" not in install_group.title.lower()

    def test_each_manager_names_its_own_software_and_its_own_origins(self) -> None:
        """H86 — `PKG-NG-ORIGIN-CONVERGE` covers apt packages and flatpak applications alike, and
        the narrative calls what a flatpak comes from a remote, never a repository. One
        `ORIGIN_MISMATCH` title said "repositories" and "packages" for both, so the flatpak
        group named two things flatpak does not have.
        """
        diffs = [_diff("o1", DiffAction.REPORT_ONLY, DiffClass.ORIGIN_MISMATCH)]

        apt_group = _job_for_manager("apt")._build_review_groups(diffs)[0]
        flatpak_group = _job_for_manager("flatpak")._build_review_groups(diffs)[0]

        assert apt_group.title == "Installed from different repositories (apt packages)"
        assert flatpak_group.title == "Installed from different remotes (flatpak applications)"

    def test_a_flatpak_action_group_says_applications_too(self) -> None:
        """H86 — The noun is the manager's, not the report group's: an install screen names the
        same things the report does.
        """
        groups = _job_for_manager("flatpak")._build_review_groups([_diff("i1", DiffAction.INSTALL)])

        assert groups[0].title == "Install flatpak applications"


class TestConvergeDispatchByAction:
    """`apply()` routes INSTALL/REMOVE/CHANGE to `converge()`; REPORT_ONLY never reaches it."""

    @pytest.mark.asyncio
    async def test_remove_diff_produces_exactly_one_target_converge_call(self) -> None:
        """H20 — a removal answered with the act converges once, and it is the removal."""
        job = FakeSyncJob(make_context())
        diffs = (_diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),)
        _accept(job, diffs, {"r1": Decision.APPLY})

        await job.apply()

        assert len(job.converge_calls) == 1
        assert job.converge_calls[0].action == DiffAction.REMOVE

    @pytest.mark.asyncio
    async def test_change_diff_reaches_converge_alongside_install_and_remove(self) -> None:
        """H21 — a change answered with the act converges exactly once."""
        job = FakeSyncJob(make_context())
        diffs = (_diff("c1", DiffAction.CHANGE, DiffClass.VERSION_MISMATCH),)
        _accept(job, diffs, {"c1": Decision.APPLY})

        await job.apply()

        assert [d.item_id for d in job.converge_calls] == ["c1"]

    @pytest.mark.asyncio
    async def test_report_only_diff_produces_zero_target_commands(self) -> None:
        """H24 — a report-only finding never converges, even carrying a forced act decision."""
        job = FakeSyncJob(make_context())
        diffs = (_diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),)
        _accept(job, diffs, {"p1": Decision.APPLY})

        await job.apply()

        assert job.converge_calls == []

    @pytest.mark.asyncio
    async def test_ticking_only_install_group_yields_zero_removal_commands(self) -> None:
        """H19, H22 — the approved install converges once and the declined removal converges nothing."""
        job = FakeSyncJob(make_context())
        diffs = (
            _diff("i1", DiffAction.INSTALL),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
        )
        _accept(job, diffs, {"i1": Decision.APPLY, "r1": Decision.SKIP_ONCE})

        await job.apply()

        assert [d.item_id for d in job.converge_calls] == ["i1"]

    @pytest.mark.asyncio
    async def test_a_marked_item_converges_nothing_while_its_mark_is_written(self) -> None:
        """H23 — `PKG-FR-MACHINE-SPECIFIC`: the permanent answer is a refusal as well as a
        record. Which machine the mark lands on is `test_package_state.py`'s subject; what
        this pins is the other half — that the item itself converges nothing, while a
        neighbouring approved item in the same run still does.
        """
        context = make_context()
        job = FakeSyncJob(context)
        diffs = (
            _diff("i1", DiffAction.INSTALL),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
        )
        _accept(job, diffs, {"i1": Decision.SKIP_ALWAYS, "r1": Decision.APPLY})

        await job.apply()

        assert [d.item_id for d in job.converge_calls] == ["r1"]
        # The mark itself IS written, so this is a refusal to converge and not a lost item.
        source_commands = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("fake.decisions.yaml" in command for command in source_commands)

    @pytest.mark.asyncio
    async def test_dry_run_zero_mutating_commands_across_all_four_action_types(self) -> None:
        """H25, J51 — a dry run issues no converge for any action kind, however the items were answered."""
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
        """J52 — ADR-014: the dry run reports exactly what would happen, and for several diff
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


class TestDecisionsReachTheLog:
    """`PKG-FR-LOG-DECISIONS`: the report says what a job did; the log is where the user
    reconstructs why, so every item it presented is named with the answer it got.
    """

    @pytest.mark.asyncio
    async def test_every_presented_item_is_named_with_its_decision(self, caplog: pytest.LogCaptureFixture) -> None:
        """J99, J101."""
        caplog.set_level(LogLevel.FULL.value, logger="pcswitcher.jobs.base")
        job = FakeSyncJob(make_context())
        diffs = (
            _diff("i1", DiffAction.INSTALL),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
            _diff("r2", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
        )
        _accept(
            job,
            diffs,
            {"i1": Decision.APPLY, "r1": Decision.SKIP_ONCE, "r2": Decision.SKIP_ALWAYS},
        )

        await job.apply()

        assert "reviewed i1 (install): applied" in caplog.messages
        assert "reviewed r1 (remove): skipped this run" in caplog.messages
        assert "reviewed r2 (remove): marked as this machine's own" in caplog.messages

    @pytest.mark.asyncio
    async def test_a_skipped_item_leaves_a_line_where_nothing_else_would(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """H22, J3, J100 — A skipped item converges nothing and enters no report, so this is the only
        record that it was ever offered."""
        caplog.set_level(LogLevel.FULL.value, logger="pcswitcher.jobs.base")
        job = FakeSyncJob(make_context())
        diffs = (_diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),)
        _accept(job, diffs, {"r1": Decision.SKIP_ONCE})

        await job.apply()

        assert job.converge_calls == []
        assert "reviewed r1 (remove): skipped this run" in caplog.messages


class TestEveryLogRecordCarriesItsMachine:
    """H79 — the naming rule exempts log records from saying a hostname, and the exemption
    rests on the record carrying the machine as a field of its own. `_log` puts `host` in
    every record's `extra` by construction; this asserts the property the exemption claims,
    over the records one `apply()` actually emits.
    """

    @pytest.mark.asyncio
    async def test_no_package_job_log_record_leaves_its_machine_unsaid(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level(LogLevel.FULL.value, logger="pcswitcher.jobs.base")
        job = FakeSyncJob(make_context())
        diffs = (
            _diff("i1", DiffAction.INSTALL),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
            _diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),
        )
        _accept(job, diffs, {"i1": Decision.APPLY, "r1": Decision.SKIP_ONCE, "p1": Decision.SKIP_ALWAYS})

        await job.apply()

        from_this_job = [record for record in caplog.records if getattr(record, "job", None) == "fake_sync"]
        assert from_this_job, "apply() emitted no record attributable to the job"
        assert all(getattr(record, "host", "") for record in from_this_job)


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
        """H32, J145 — an already-converged pair produces no diff, no group, no converge and no `mutates=` command."""
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
        """J147."""
        context = make_context()
        job = _FakeManualJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={}, was_interactive=True, snippets={diff.item_id: "sudo dpkg --install /tmp/x.deb"}
            ),
        )

        await job.apply()

        source_cmds = [c.args[0] for c in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        target_cmds = [c.args[0] for c in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv --force" in cmd and "package-snippets" in cmd for cmd in source_cmds)
        assert not any("package-snippets" in cmd for cmd in target_cmds)

    @pytest.mark.asyncio
    async def test_skip_always_on_unreproducible_item_records_on_source(self) -> None:
        """H144 — an unreproducible item answered permanently is marked on the machine that holds it."""
        context = make_context()
        job = _FakeManualJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        source_cmds = [c.args[0] for c in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        target_cmds = [c.args[0] for c in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv --force" in cmd and "fake.decisions" in cmd for cmd in source_cmds)
        assert not any("fake.decisions" in cmd for cmd in target_cmds)

    @pytest.mark.asyncio
    async def test_no_finalize_writes_during_dry_run(self) -> None:
        """J55."""
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
            assert "mv --force" not in cmd

    @pytest.mark.asyncio
    async def test_no_finalize_writes_when_outcome_not_interactive(self) -> None:
        """J45."""
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
            assert "mv --force" not in cmd


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
        """H143 — a report-only diff answered permanently records nothing."""
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

    def __init__(self, context: JobContext, events: list[str], *, source_items: Sequence[FakeItem] = ()) -> None:
        super().__init__(context, source_items=source_items)
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

    def __init__(
        self,
        events: list[str],
        decisions: dict[str, Decision] | None = None,
        *,
        was_interactive: bool = True,
    ) -> None:
        super().__init__(decisions, was_interactive=was_interactive)
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
        """H1 — plan, review, accept, the after-review seam, then apply — and never apply before the review returns."""
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
        """H8 — a job with no reviewer injected fails loudly before any converge or command."""
        job = FakeSyncJob(make_context())  # reviewer defaults to None

        with pytest.raises(AssertionError, match="no reviewer"):
            await job.execute()

        assert job.converge_calls == []
        job.context.target.run_command.assert_not_called()  # pyright: ignore[reportAttributeAccessIssue]

    @pytest.mark.asyncio
    async def test_plan_failure_propagates_out_of_execute_unchanged(self) -> None:
        """H7 — a raising `plan()` propagates unchanged and the review is never reached."""
        failure = RuntimeError("manifest capture blew up")
        reviewer = FakeReviewer()
        job = _RaisingPlanJob(make_context(reviewer=reviewer), failure)

        with pytest.raises(RuntimeError, match="manifest capture blew up") as exc_info:
            await job.execute()

        assert exc_info.value is failure
        # The review is never reached when planning fails.
        assert reviewer.call_count == 0

    @pytest.mark.asyncio
    async def test_a_non_interactive_package_review_skips_the_job_instead_of_applying_nothing(self) -> None:
        """H9 — D-26 forces every item to SKIP_ONCE without a TTY, so the job converges nothing;
        reporting SUCCESS for that made every headless run look like four successful syncs.
        """
        events: list[str] = []
        reviewer = _RecordingReviewer(events, was_interactive=False)
        job = _OrderRecordingJob(make_context(reviewer=reviewer), events, source_items=[FakeItem("pkg-a")])

        with pytest.raises(JobSkipped) as exc_info:
            await job.execute()

        assert exc_info.value.job_name == "fake_sync"
        # Raised before after_review, so manual_installs_sync would not push its registry.
        assert events == ["plan", "review"]
        assert job.converge_calls == []

    @pytest.mark.asyncio
    async def test_an_empty_plan_is_still_a_success_and_transfers_nothing(self) -> None:
        """H10, J8, J47 — Same non-interactive path, nothing to decide: the target already matches the
        source, which is the goal met — not a skip.

        The `after_review` seam is still skipped (`PKG-FR-NO-TERMINAL`). An empty plan says
        this run's scan found nothing to review, not that there is nothing to transfer:
        `manual_installs_sync` pushes the source's whole snippet registry there, entries
        from earlier runs included.
        """
        events: list[str] = []
        reviewer = _RecordingReviewer(events, was_interactive=False)
        job = _OrderRecordingJob(make_context(reviewer=reviewer), events)

        await job.execute()

        assert events == ["plan", "review", "accept_review", "apply"]

    @pytest.mark.asyncio
    async def test_the_after_review_seam_runs_when_a_human_answered(self) -> None:
        events: list[str] = []
        reviewer = _RecordingReviewer(events, was_interactive=True)
        job = _OrderRecordingJob(make_context(reviewer=reviewer), events)

        await job.execute()

        assert events == ["plan", "review", "accept_review", "after_review", "apply"]


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


class _StubAbortingPackageJob(PackageSyncJob):
    """A package job whose execute() raises `SyncAbortedByUser` — what a review screen
    answered "stop the sync", or abandoned with Ctrl-C, hands the orchestrator.
    """

    name: ClassVar[str] = "stub_aborting_package"
    manager_id: ClassVar[str] = "stub-aborting"

    async def converge(self, diff: ItemDiff) -> CommandResult:
        raise NotImplementedError

    async def validate(self) -> list[ValidationError]:
        return []

    async def plan(self) -> PackagePlan:
        return PackagePlan(manager="stub-aborting", diffs=(), groups=())

    async def execute(self) -> None:
        raise SyncAbortedByUser("package review aborted at 'fortunes' (Ctrl-C)")


class _StubSuccessJob(SyncJob):
    name: ClassVar[str] = "stub_success"

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        self.executed = False

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        self.executed = True


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


class TestOrchestratorPackageItemFailuresContinuation:
    """PackageItemFailures records a FAILED JobResult but does not abort the run (D-27).

    Re-homed from the deleted test_package_phase.py: the coordinator is gone, but the
    orchestrator's per-job except chain that this exercises is a delivered behaviour.
    """

    @pytest.mark.asyncio
    async def test_failing_package_job_does_not_cancel_remaining_jobs(self, wired_orchestrator: Orchestrator) -> None:
        """J26."""
        orchestrator = wired_orchestrator
        failing_job = _StubFailingPackageJob(make_context())
        success_job = _StubSuccessJob(make_context())

        results = await orchestrator._execute_jobs([failing_job, success_job])  # pyright: ignore[reportPrivateUsage]

        assert len(results) == 2
        assert results[0].job_name == "stub_failing_package"
        assert results[0].status == JobStatus.FAILED
        assert results[1].job_name == "stub_success"
        assert results[1].status == JobStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_an_abort_raised_in_a_package_job_stops_the_run_untouched(
        self, wired_orchestrator: Orchestrator
    ) -> None:
        """H157 — `PKG-FR-ABORT`: the orchestrator's per-job handler re-raises
        `SyncAbortedByUser` ahead of both the skip branch and the failure branch, so an
        aborted review is not converted into a FAILED job result and no later job runs. The
        absent CRITICAL is the load-bearing half: taking the ordinary failure branch would
        record the run as a job failure instead of an abort.
        """
        orchestrator = wired_orchestrator
        aborting_job = _StubAbortingPackageJob(make_context())
        never_run_job = _StubSuccessJob(make_context())

        with pytest.raises(SyncAbortedByUser, match="fortunes"):
            await orchestrator._execute_jobs([aborting_job, never_run_job])  # pyright: ignore[reportPrivateUsage]

        assert never_run_job.executed is False
        orchestrator._logger.critical.assert_not_called()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

    @pytest.mark.asyncio
    async def test_other_exception_types_still_abort_the_run(self, wired_orchestrator: Orchestrator) -> None:
        """J31 — Regression guard: only `PackageItemFailures` and `ProbeFailed` get the
        non-aborting branch — every other exception must still stop the remaining jobs from
        running.
        """
        orchestrator = wired_orchestrator
        failing_job = _StubOtherFailureJob(make_context())
        never_run_job = _StubSuccessJob(make_context())

        with pytest.raises(RuntimeError, match="unrelated job crashed"):
            await orchestrator._execute_jobs([failing_job, never_run_job])  # pyright: ignore[reportPrivateUsage]
