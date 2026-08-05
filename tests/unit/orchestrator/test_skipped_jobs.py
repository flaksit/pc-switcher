"""A job that did nothing is recorded SKIPPED, not SUCCESS (SPEC 02 S8a).

Two orchestrator-level mechanisms: the `except JobSkipped` arm in the sequential job
loop, and the SKIPPED result discovery builds for an enabled job name that resolves to
no class — the one case with no job instance to raise from.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import JobSkipped, JobStatus, SessionStatus, ValidationError
from pcswitcher.orchestrator import Orchestrator, _summarize_job_outcomes  # pyright: ignore[reportPrivateUsage]
from tests.unit.jobs.test_package_sync_core import make_context


class _SkippingJob(SyncJob):
    name: ClassVar[str] = "stub_skipping"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        raise JobSkipped(self.name, "nothing applicable")


class _RanAfterJob(SyncJob):
    name: ClassVar[str] = "stub_ran_after"

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        self.ran = False

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        self.ran = True


class TestSkippedJobArm:
    @pytest.mark.asyncio
    async def test_the_orchestrator_records_a_skipped_job_and_runs_the_next_one(
        self, wired_orchestrator: Orchestrator
    ) -> None:
        """J15, J16, J17 — the run continues, the session is clean, and the reason is kept."""
        skipping = _SkippingJob(make_context())
        following = _RanAfterJob(make_context())

        results = await wired_orchestrator._execute_jobs([skipping, following])  # pyright: ignore[reportPrivateUsage]

        assert [r.status for r in results] == [JobStatus.SKIPPED, JobStatus.SUCCESS]
        assert results[0].job_name == "stub_skipping"
        assert results[0].error_message == "nothing applicable"
        assert following.ran, "JobSkipped must not abort the run — the next job still executes"
        assert _summarize_job_outcomes(results)[0] is SessionStatus.COMPLETED


class TestUnresolvableEnabledJob:
    @pytest.mark.asyncio
    async def test_an_unresolvable_enabled_job_is_recorded_skipped(self, wired_orchestrator: Orchestrator) -> None:
        """K39 — an enabled job whose module does not exist used to leave no JobResult at all."""
        wired_orchestrator._config.sync_jobs = {"no_such_job_module": True}  # pyright: ignore[reportPrivateUsage]

        jobs, unresolved = await wired_orchestrator._discover_and_validate_jobs()  # pyright: ignore[reportPrivateUsage]
        results = await wired_orchestrator._execute_jobs(jobs, unresolved)  # pyright: ignore[reportPrivateUsage]

        assert jobs == []
        assert [(r.job_name, r.status) for r in results] == [("no_such_job_module", JobStatus.SKIPPED)]
        assert _summarize_job_outcomes(results)[0] is SessionStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_a_resolvable_job_leaves_no_skipped_result(self, wired_orchestrator: Orchestrator) -> None:
        """K40 — Guard on the arm above: a real job name must not be recorded as unresolvable."""
        wired_orchestrator._config.sync_jobs = {"dummy_success": True}  # pyright: ignore[reportPrivateUsage]
        wired_orchestrator._config.job_configs = {}  # pyright: ignore[reportPrivateUsage]

        jobs, unresolved = await wired_orchestrator._discover_and_validate_jobs()  # pyright: ignore[reportPrivateUsage]

        assert unresolved == []
        assert [job.name for job in jobs] == ["dummy_success"]
