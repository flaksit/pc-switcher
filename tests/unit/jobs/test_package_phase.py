"""Unit tests for PackagePhaseCoordinator, JobContext.enabled_sync_jobs, and the
orchestrator's PackageItemFailures continuation branch.

Stub `PackageSyncJob` subclasses override `plan()`/`accept_review()` directly (bypassing
`AptSyncJob`'s real capture/diff pipeline, already covered by test_apt_sync.py) so these
tests isolate the coordinator's own three-step contract: plan every job, review once,
distribute decisions.
"""

from __future__ import annotations

import io
from collections.abc import Awaitable, Callable, Sequence
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.package_items import AptPackageItem, DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.package_phase import PackagePhaseCoordinator
from pcswitcher.jobs.package_review import Decision, ReviewEntry, ReviewGroup, ReviewOutcome
from pcswitcher.jobs.package_sync_core import PackageItemFailures, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, JobStatus, ValidationError
from pcswitcher.orchestrator import Orchestrator

DF_OUTPUT = (
    "Filesystem     1B-blocks       Used  Available Use% Mounted on\n"
    "/dev/sda1  1000000000000 500000000000 500000000000  50% /\n"
)


def make_context(*, dry_run: bool = False, enabled_sync_jobs: dict[str, bool] | None = None) -> JobContext:
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
        enabled_sync_jobs=enabled_sync_jobs,
    )


def _diff(item_id: str) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.INSTALL,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


def _plan(manager: str, item_ids: list[str], *, action: str = "install") -> PackagePlan:
    diffs = tuple(_diff(item_id) for item_id in item_ids)
    group = ReviewGroup(
        manager=manager,
        action=action,
        title=f"Install {manager} packages",
        entries=tuple(ReviewEntry(item_id=item_id, label=item_id, action_label="install") for item_id in item_ids),
    )
    return PackagePlan(manager=manager, diffs=diffs, groups=(group,))


class _CallRecorder:
    def __init__(self) -> None:
        self.events: list[str] = []


class _StubPackageJob(PackageSyncJob):
    """Stub whose plan()/accept_review() are overridden and recorded; capture/query/converge
    are unused here (this module tests the coordinator, not AptSyncJob's own diff logic).
    """

    def __init__(
        self,
        context: JobContext,
        recorder: _CallRecorder,
        *,
        plan_result: PackagePlan | None = None,
        plan_error: Exception | None = None,
    ) -> None:
        super().__init__(context)
        self._recorder = recorder
        self._plan_result = plan_result
        self._plan_error = plan_error
        self.received_plan: PackagePlan | None = None
        self.received_outcome: ReviewOutcome | None = None

    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        raise NotImplementedError

    async def query_target_items(self) -> Sequence[AptPackageItem]:
        raise NotImplementedError

    async def converge(self, diff: ItemDiff) -> CommandResult:
        raise NotImplementedError

    async def validate(self) -> list[ValidationError]:
        return []

    async def plan(self) -> PackagePlan:
        self._recorder.events.append(f"{self.manager_id}.plan")
        if self._plan_error is not None:
            raise self._plan_error
        assert self._plan_result is not None
        return self._plan_result

    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        self._recorder.events.append(f"{self.manager_id}.accept_review")
        self.received_plan = plan
        self.received_outcome = outcome
        super().accept_review(plan, outcome)


class _StubAptLikeJob(_StubPackageJob):
    name: ClassVar[str] = "stub_apt_like"
    manager_id: ClassVar[str] = "stub-apt"


class _StubSnapLikeJob(_StubPackageJob):
    name: ClassVar[str] = "stub_snap_like"
    manager_id: ClassVar[str] = "stub-snap"


def _wrapped_review_items(
    recorder: _CallRecorder, captured_groups: list[ReviewGroup]
) -> Callable[..., Awaitable[ReviewOutcome]]:
    async def _run(
        groups: Sequence[ReviewGroup],
        *,
        console: object,
        ui: object,
        logger: object = None,
    ) -> ReviewOutcome:
        recorder.events.append("review_items")
        captured_groups.extend(groups)
        all_ids = {entry.item_id for group in groups for entry in group.entries}
        return ReviewOutcome(decisions=dict.fromkeys(all_ids, Decision.APPLY), was_interactive=True)

    return _run


class TestPlanBeforeReview:
    @pytest.mark.asyncio
    async def test_both_jobs_plan_before_review_which_runs_once_then_accept_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _CallRecorder()
        captured_groups: list[ReviewGroup] = []
        review_mock = AsyncMock(side_effect=_wrapped_review_items(recorder, captured_groups))
        monkeypatch.setattr("pcswitcher.jobs.package_phase.review_items", review_mock)

        context = make_context()
        job_a = _StubAptLikeJob(context, recorder, plan_result=_plan("stub-apt", ["stub-apt:1"]))
        job_b = _StubSnapLikeJob(context, recorder, plan_result=_plan("stub-snap", ["stub-snap:1"]))
        coordinator = PackagePhaseCoordinator(Console(file=io.StringIO()), MagicMock())

        await coordinator.run([job_a, job_b])

        assert recorder.events == [
            "stub-apt.plan",
            "stub-snap.plan",
            "review_items",
            "stub-apt.accept_review",
            "stub-snap.accept_review",
        ]
        review_mock.assert_called_once()


class TestMergedGroupOrder:
    @pytest.mark.asyncio
    async def test_manager_order_follows_supplied_job_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Groups are concatenated in the SUPPLIED job order, each plan's own group order
        preserved verbatim (removals-own-group is PackageSyncJob's concern, already tested
        in test_apt_sync.py; here only the coordinator's non-reordering behavior matters).
        """
        recorder = _CallRecorder()
        captured_groups: list[ReviewGroup] = []
        review_mock = AsyncMock(side_effect=_wrapped_review_items(recorder, captured_groups))
        monkeypatch.setattr("pcswitcher.jobs.package_phase.review_items", review_mock)

        context = make_context()
        install_group = ReviewGroup(
            manager="stub-apt",
            action="install",
            title="Install stub-apt packages",
            entries=(ReviewEntry(item_id="stub-apt:1", label="1", action_label="install"),),
        )
        remove_group = ReviewGroup(
            manager="stub-apt",
            action="remove",
            title="Remove stub-apt packages",
            entries=(ReviewEntry(item_id="stub-apt:2", label="2", action_label="remove"),),
        )
        apt_plan = PackagePlan(
            manager="stub-apt",
            diffs=(_diff("stub-apt:1"), _diff("stub-apt:2")),
            groups=(install_group, remove_group),
        )
        job_snap = _StubSnapLikeJob(context, recorder, plan_result=_plan("stub-snap", ["stub-snap:1"]))
        job_apt = _StubAptLikeJob(context, recorder, plan_result=apt_plan)
        coordinator = PackagePhaseCoordinator(Console(file=io.StringIO()), MagicMock())

        # Supplied order: snap first, apt second — the merged groups must follow THIS
        # order, not alphabetical or declaration order.
        await coordinator.run([job_snap, job_apt])

        assert [g.manager for g in captured_groups] == ["stub-snap", "stub-apt", "stub-apt"]
        assert [g.action for g in captured_groups[1:]] == ["install", "remove"]


class TestDecisionDistribution:
    @pytest.mark.asyncio
    async def test_each_job_receives_only_its_own_item_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _CallRecorder()
        captured_groups: list[ReviewGroup] = []
        review_mock = AsyncMock(side_effect=_wrapped_review_items(recorder, captured_groups))
        monkeypatch.setattr("pcswitcher.jobs.package_phase.review_items", review_mock)

        context = make_context()
        job_a = _StubAptLikeJob(context, recorder, plan_result=_plan("stub-apt", ["stub-apt:1", "stub-apt:2"]))
        job_b = _StubSnapLikeJob(context, recorder, plan_result=_plan("stub-snap", ["stub-snap:1"]))
        coordinator = PackagePhaseCoordinator(Console(file=io.StringIO()), MagicMock())

        await coordinator.run([job_a, job_b])

        assert job_a.received_outcome is not None
        assert set(job_a.received_outcome.decisions) == {"stub-apt:1", "stub-apt:2"}
        assert job_b.received_outcome is not None
        assert set(job_b.received_outcome.decisions) == {"stub-snap:1"}

    @pytest.mark.asyncio
    async def test_snippets_and_unresolved_are_also_sliced_per_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """D-21 (plan 02-07): a snippet authored, or an item left unresolved, for one
        manager's item id must not leak into a sibling manager's outcome.
        """

        async def _run_with_snippets_and_unresolved(
            groups: Sequence[ReviewGroup], *, console: object, ui: object, logger: object = None
        ) -> ReviewOutcome:
            all_ids = {entry.item_id for group in groups for entry in group.entries}
            return ReviewOutcome(
                decisions=dict.fromkeys(all_ids, Decision.APPLY),
                was_interactive=True,
                snippets={"stub-apt:1": "echo installed"},
                unresolved=("stub-snap:1",),
            )

        monkeypatch.setattr(
            "pcswitcher.jobs.package_phase.review_items", AsyncMock(side_effect=_run_with_snippets_and_unresolved)
        )

        context = make_context()
        job_a = _StubAptLikeJob(context, _CallRecorder(), plan_result=_plan("stub-apt", ["stub-apt:1"]))
        job_b = _StubSnapLikeJob(context, _CallRecorder(), plan_result=_plan("stub-snap", ["stub-snap:1"]))
        coordinator = PackagePhaseCoordinator(Console(file=io.StringIO()), MagicMock())

        await coordinator.run([job_a, job_b])

        assert job_a.received_outcome is not None
        assert job_a.received_outcome.snippets == {"stub-apt:1": "echo installed"}
        assert job_a.received_outcome.unresolved == ()
        assert job_b.received_outcome is not None
        assert job_b.received_outcome.snippets == {}
        assert job_b.received_outcome.unresolved == ("stub-snap:1",)


class TestPlanFailureIsolation:
    @pytest.mark.asyncio
    async def test_one_jobs_plan_failure_does_not_block_the_other(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _CallRecorder()
        captured_groups: list[ReviewGroup] = []
        review_mock = AsyncMock(side_effect=_wrapped_review_items(recorder, captured_groups))
        monkeypatch.setattr("pcswitcher.jobs.package_phase.review_items", review_mock)

        context = make_context()
        failure = RuntimeError("apt manifest capture blew up")
        job_a = _StubAptLikeJob(context, recorder, plan_error=failure)
        job_b = _StubSnapLikeJob(context, recorder, plan_result=_plan("stub-snap", ["stub-snap:1"]))
        coordinator = PackagePhaseCoordinator(Console(file=io.StringIO()), MagicMock())

        await coordinator.run([job_a, job_b])

        assert recorder.events == ["stub-apt.plan", "stub-snap.plan", "review_items", "stub-snap.accept_review"]
        assert job_b.received_outcome is not None
        review_mock.assert_called_once()
        # Only stub-snap's group was ever reviewed — the failed job contributed nothing.
        assert [g.manager for g in captured_groups] == ["stub-snap"]

        with pytest.raises(RuntimeError, match="apt manifest capture blew up"):
            await job_a.execute()


class TestEmptyJobList:
    @pytest.mark.asyncio
    async def test_no_jobs_returns_without_prompting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        review_mock = AsyncMock()
        monkeypatch.setattr("pcswitcher.jobs.package_phase.review_items", review_mock)
        coordinator = PackagePhaseCoordinator(Console(file=io.StringIO()), MagicMock())

        await coordinator.run([])

        review_mock.assert_not_called()


class TestDryRunStillReviews:
    @pytest.mark.asyncio
    async def test_dry_run_context_still_plans_and_reviews(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _CallRecorder()
        captured_groups: list[ReviewGroup] = []
        review_mock = AsyncMock(side_effect=_wrapped_review_items(recorder, captured_groups))
        monkeypatch.setattr("pcswitcher.jobs.package_phase.review_items", review_mock)

        context = make_context(dry_run=True)
        job = _StubAptLikeJob(context, recorder, plan_result=_plan("stub-apt", ["stub-apt:1"]))
        coordinator = PackagePhaseCoordinator(Console(file=io.StringIO()), MagicMock())

        await coordinator.run([job])

        assert "stub-apt.plan" in recorder.events
        review_mock.assert_called_once()


class TestJobContextEnabledSyncJobs:
    def test_defaults_to_none_and_does_not_raise(self) -> None:
        context = make_context()
        assert context.enabled_sync_jobs is None

    def test_can_be_populated_with_the_full_enablement_map(self) -> None:
        context = make_context(enabled_sync_jobs={"apt_sync": True, "folder_sync": False})
        assert context.enabled_sync_jobs == {"apt_sync": True, "folder_sync": False}


class _StubFailingPackageJob(PackageSyncJob):
    """A package job whose execute() raises PackageItemFailures directly, bypassing plan()
    /apply() (already covered elsewhere) to isolate the orchestrator's except-chain branch.
    """

    name: ClassVar[str] = "stub_failing_package"
    manager_id: ClassVar[str] = "stub-failing"

    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        raise NotImplementedError

    async def query_target_items(self) -> Sequence[AptPackageItem]:
        raise NotImplementedError

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
    """A non-package job whose execute() raises a plain exception — the pre-existing
    abort-the-run path this plan must NOT change (only PackageItemFailures gets a
    non-aborting branch; every other exception type keeps today's behavior).
    """

    name: ClassVar[str] = "stub_other_failure"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        raise RuntimeError("unrelated job crashed")


def _make_wired_orchestrator() -> Orchestrator:
    """A narrowly-constructed Orchestrator with enough wiring for `_execute_jobs` /
    `_run_jobs_in_task_group` to run: mocked local/remote executors returning valid `df`
    output for the background disk-space monitors, a non-interactive Console (so the
    package review never blocks on a prompt), and a silenced logger/UI.
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
    """PackageItemFailures records a FAILED JobResult but does not abort the run (D-24)."""

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
