"""A dead package-manager read fails its own job and lets the rest run.

`PKG-FR-READ-FAILS-JOB`: `ProbeFailed` (ADR-022) used to fall into the orchestrator's
`except Exception` arm, which records FAILED and re-raises — so an apt lock on one machine
stopped `folder_sync` too. It now shares the non-aborting arm with `PackageItemFailures`.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.models import JobStatus, SessionStatus, ValidationError
from pcswitcher.orchestrator import Orchestrator, _summarize_job_outcomes  # pyright: ignore[reportPrivateUsage]
from tests.unit.jobs.test_package_sync_core import make_context

_MESSAGE = "probe on the target did not answer — `snap list --all` exited 1: cannot communicate with server"


class _ProbeFailingJob(SyncJob):
    name: ClassVar[str] = "stub_probe_failing"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        raise ProbeFailed(_MESSAGE)


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
