"""A failing package job fails alone; the rest of the run continues.

`PKG-FR-JOB-INDEPENDENCE` and `PKG-FR-OUTCOME-FAILED` state it without qualification, so
the orchestrator keys isolation on the job a failure came out of, not on the exception
class: anything a package job raises fails only that job. `PKG-FR-READ-FAILS-JOB` is the
same rule for a dead read (`ProbeFailed`, ADR-022). A lock conflict still ends the run, and
a job outside package sync still aborts it (GitHub issue #220).
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import ItemDiff
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.sync_core import PackagePlan, PackageSyncJob
from pcswitcher.models import (
    CommandResult,
    JobStatus,
    SessionStatus,
    SyncLockedError,
    ValidationError,
)
from pcswitcher.orchestrator import Orchestrator, _summarize_job_outcomes  # pyright: ignore[reportPrivateUsage]
from tests.unit.jobs.test_package_sync_core import make_context

_MESSAGE = "probe on the target did not answer — `snap list --all` exited 1: cannot communicate with server"
_CRASH = "snippet registry transfer to the target failed: rsync exited 12"


class _ProbeFailingJob(SyncJob):
    name: ClassVar[str] = "stub_probe_failing"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        raise ProbeFailed(_MESSAGE)


class _CrashingPackageJob(PackageSyncJob):
    """A package job that fails in none of the package-specific ways: an ordinary
    exception out of the same job whose items the user approved.
    """

    name: ClassVar[str] = "stub_crashing_package"
    manager_id: ClassVar[str] = "stub-crashing"

    async def validate(self) -> list[ValidationError]:
        return []

    async def plan(self) -> PackagePlan:
        return PackagePlan(manager="stub-crashing", diffs=(), groups=())

    async def converge(self, diff: ItemDiff) -> CommandResult:
        raise NotImplementedError

    async def execute(self) -> None:
        raise RuntimeError(_CRASH)


class _LockedPackageJob(_CrashingPackageJob):
    name: ClassVar[str] = "stub_locked_package"

    async def execute(self) -> None:
        raise SyncLockedError("Target pc2 is already involved in a sync.")


class _RanAfterJob(SyncJob):
    name: ClassVar[str] = "stub_ran_after"

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        self.ran = False

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        self.ran = True


class TestProbeFailedFailsOnlyItsOwnJob:
    @pytest.mark.asyncio
    async def test_the_orchestrator_records_it_failed_and_runs_the_next_job(
        self, wired_orchestrator: Orchestrator
    ) -> None:
        probe_failing = _ProbeFailingJob(make_context())
        following = _RanAfterJob(make_context())

        results = await wired_orchestrator._execute_jobs([probe_failing, following])  # pyright: ignore[reportPrivateUsage]

        assert [r.status for r in results] == [JobStatus.FAILED, JobStatus.SUCCESS]
        assert results[0].job_name == "stub_probe_failing"
        assert following.ran, "a dead read in one manager is no evidence about another job"

    @pytest.mark.asyncio
    async def test_the_failed_result_carries_the_command_that_did_not_answer(
        self, wired_orchestrator: Orchestrator
    ) -> None:
        """`PKG-FR-FAIL-NAMED`: the reason reaches the report, not just the log."""
        results = await wired_orchestrator._execute_jobs([_ProbeFailingJob(make_context())])  # pyright: ignore[reportPrivateUsage]

        assert results[0].error_message == _MESSAGE

    @pytest.mark.asyncio
    async def test_the_session_is_still_reported_failed(self, wired_orchestrator: Orchestrator) -> None:
        """Continuing is not forgiving: the run's own outcome still records the failure."""
        results = await wired_orchestrator._execute_jobs(  # pyright: ignore[reportPrivateUsage]
            [_ProbeFailingJob(make_context()), _RanAfterJob(make_context())]
        )

        assert _summarize_job_outcomes(results)[0] is SessionStatus.FAILED


class TestAnyFailureOfAPackageJobStaysInThatJob:
    """The isolation the articles state covers every way a package job can fail."""

    @pytest.mark.asyncio
    async def test_a_generic_exception_does_not_stop_the_following_job(self, wired_orchestrator: Orchestrator) -> None:
        crashing = _CrashingPackageJob(make_context())
        following = _RanAfterJob(make_context())

        results = await wired_orchestrator._execute_jobs([crashing, following])  # pyright: ignore[reportPrivateUsage]

        assert [r.status for r in results] == [JobStatus.FAILED, JobStatus.SUCCESS]
        assert following.ran, "one manager's crash discards no consent the user gave another job"

    @pytest.mark.asyncio
    async def test_the_failed_result_carries_what_went_wrong(self, wired_orchestrator: Orchestrator) -> None:
        results = await wired_orchestrator._execute_jobs([_CrashingPackageJob(make_context())])  # pyright: ignore[reportPrivateUsage]

        assert results[0].error_message == _CRASH

    @pytest.mark.asyncio
    async def test_a_lock_conflict_still_ends_the_run(self, wired_orchestrator: Orchestrator) -> None:
        """This machine is no longer entitled to sync, so no later job may run."""
        never_runs = _RanAfterJob(make_context())

        with pytest.raises(SyncLockedError):
            await wired_orchestrator._execute_jobs([_LockedPackageJob(make_context()), never_runs])  # pyright: ignore[reportPrivateUsage]

        assert not never_runs.ran
