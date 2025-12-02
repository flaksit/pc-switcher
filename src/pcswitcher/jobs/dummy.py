"""Dummy test jobs for infrastructure validation (FR-039)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel, ProgressUpdate, ValidationError


class DummySuccessJob(SyncJob):
    """Dummy job for testing infrastructure (FR-039).

    Simulates 20s operation on source (log every 2s, WARNING at 6s)
    and 20s on target (log every 2s, ERROR at 8s).
    Progress milestones: 0% (start) → 25% (10s source) → 50% (20s, end source)
                       → 75% (30s, 10s target) → 100% (40s, end target)
    """

    name: ClassVar[str] = "dummy_success"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        """Initialize success job with context.

        Args:
            context: JobContext with executors, config, and event bus
        """
        super().__init__(context)

    async def validate(self) -> list[ValidationError]:
        """No prerequisites for dummy job."""
        return []

    async def execute(self) -> None:
        """Execute 40s test sequence with logging and progress reporting."""
        try:
            self._report_progress(ProgressUpdate(percent=0))

            # Source phase: 20s with 2s intervals (10 iterations)
            await self._run_source_phase()
            self._report_progress(ProgressUpdate(percent=50))

            # Target phase: 20s with 2s intervals (10 iterations)
            await self._run_target_phase()
            self._report_progress(ProgressUpdate(percent=100))

        except asyncio.CancelledError:
            self._log(Host.SOURCE, LogLevel.WARNING, "Dummy job termination requested")
            raise

    async def _run_source_phase(self) -> None:
        """Source phase: 20s total, log every 2s, WARNING at 6s."""
        for tick in range(10):  # 10 iterations x 2s = 20s
            elapsed = (tick + 1) * 2  # 2, 4, 6, ..., 20
            self._log(
                Host.SOURCE,
                LogLevel.INFO,
                f"Source phase: {elapsed}s elapsed",
            )

            # WARNING at 6s (after tick 2, when elapsed=6)
            if elapsed == 6:
                self._log(Host.SOURCE, LogLevel.WARNING, "Test warning at 6s")

            # Progress: 25% at halfway (10s)
            if elapsed == 10:
                self._report_progress(ProgressUpdate(percent=25))

            await asyncio.sleep(2)

    async def _run_target_phase(self) -> None:
        """Target phase: 20s total, log every 2s, ERROR at 8s."""
        for tick in range(10):  # 10 iterations x 2s = 20s
            elapsed = (tick + 1) * 2  # 2, 4, 6, ..., 20
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Target phase: {elapsed}s elapsed",
            )

            # ERROR at 8s (after tick 3, when elapsed=8)
            if elapsed == 8:
                self._log(Host.TARGET, LogLevel.ERROR, "Test error at 8s")

            # Progress: 75% at halfway (10s into target = 30s total)
            if elapsed == 10:
                self._report_progress(ProgressUpdate(percent=75))

            await asyncio.sleep(2)


class DummyFailJob(SyncJob):
    """Dummy job that fails at configurable progress percentage (FR-039).

    Used to test error handling and UI behavior during job failures.
    """

    name: ClassVar[str] = "dummy_fail"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "fail_at_percent": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "default": 60,
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        """Initialize fail job with context.

        Args:
            context: JobContext with executors, config, and event bus
        """
        super().__init__(context)

    async def validate(self) -> list[ValidationError]:
        """No prerequisites for dummy job."""
        return []

    async def execute(self) -> None:
        """Execute until configured failure percentage, then raise exception."""
        fail_at_percent = self._context.config.get("fail_at_percent", 60)

        try:
            self._report_progress(ProgressUpdate(percent=0))
            self._log(Host.SOURCE, LogLevel.INFO, f"Dummy fail job will fail at {fail_at_percent}%")

            # Simulate progress in 10% increments
            for percent in range(10, 101, 10):
                await asyncio.sleep(1)
                self._report_progress(ProgressUpdate(percent=percent))
                self._log(Host.SOURCE, LogLevel.INFO, f"Progress: {percent}%")

                if percent >= fail_at_percent:
                    self._log(Host.SOURCE, LogLevel.CRITICAL, f"Simulated failure at {percent}%")
                    raise RuntimeError(f"Dummy job failed at {percent}%")

        except asyncio.CancelledError:
            self._log(Host.SOURCE, LogLevel.WARNING, "Dummy fail job cancelled")
            raise
