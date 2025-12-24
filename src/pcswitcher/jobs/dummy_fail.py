"""Dummy fail job for infrastructure validation (FR-039)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel, ProgressUpdate

if TYPE_CHECKING:
    from pcswitcher.models import ValidationError


class DummyFailJob(SyncJob):
    """Dummy job that fails at configurable time (FR-039).

    Used to test error handling and UI behavior during job failures.
    Supports configurable source/target durations and failure time.
    """

    name: ClassVar[str] = "dummy_fail"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source_duration": {
                "type": "integer",
                "default": 10,
                "minimum": 2,
                "description": "Seconds to run on source (must be even, logs every 2s)",
            },
            "target_duration": {
                "type": "integer",
                "default": 10,
                "minimum": 2,
                "description": "Seconds to run on target (must be even, logs every 2s)",
            },
            "fail_at": {
                "type": "integer",
                "minimum": 2,
                "default": 12,
                "description": "Elapsed seconds at which to fail (must be even)",
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
        self.source_duration = context.config.get("source_duration", 10)
        self.target_duration = context.config.get("target_duration", 10)
        self.fail_at = context.config.get("fail_at", 12)

    async def validate(self) -> list[ValidationError]:
        """No prerequisites for dummy job."""
        return []

    async def execute(self) -> None:
        """Execute until configured failure time, then raise exception.

        Progress is based on elapsed time as percentage of total duration.
        Failure occurs at configured elapsed time (default 12s).
        """
        total_duration = self.source_duration + self.target_duration

        try:
            self._report_progress(ProgressUpdate(percent=0))
            self._log(Host.SOURCE, LogLevel.INFO, f"Dummy fail job will fail at {self.fail_at}s")

            # Source phase (raises if fail_at <= source_duration)
            await self._run_source_phase(total_duration)

            # Target phase
            await self._run_target_phase(total_duration)

        except asyncio.CancelledError:
            self._log(Host.SOURCE, LogLevel.WARNING, "Dummy fail job cancelled")
            raise

    async def _run_source_phase(self, total_duration: int) -> None:
        """Source phase: configurable duration, log every 2s.

        Args:
            total_duration: Total duration of source + target phases for progress calculation
        """
        iterations = self.source_duration // 2

        for tick in range(iterations):
            elapsed = (tick + 1) * 2
            percent = int((elapsed / total_duration) * 100)

            self._log(
                Host.SOURCE,
                LogLevel.INFO,
                f"Source phase: {elapsed}s elapsed",
            )
            self._report_progress(ProgressUpdate(percent=percent))

            if elapsed >= self.fail_at:
                self._log(Host.SOURCE, LogLevel.CRITICAL, f"Simulated failure at {elapsed}s")
                raise RuntimeError(f"Dummy job failed at {elapsed}s")

            await asyncio.sleep(2)

    async def _run_target_phase(self, total_duration: int) -> None:
        """Target phase: execute real commands on target, fail at configured time.

        Args:
            total_duration: Total duration of source + target phases for progress calculation
        """
        iterations = self.target_duration // 2
        cmd = f'for i in $(seq 1 {iterations}); do echo "tick $i"; sleep 2; done'

        process = await self.target.start_process(cmd)
        tick = 0

        async for raw_line in process.stdout():
            tick += 1
            line = raw_line.strip()
            elapsed = self.source_duration + tick * 2
            percent = int((elapsed / total_duration) * 100)

            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Target phase: {elapsed}s elapsed (remote: {line})",
            )
            self._report_progress(ProgressUpdate(percent=percent))

            if elapsed >= self.fail_at:
                await process.terminate()
                self._log(Host.TARGET, LogLevel.CRITICAL, f"Simulated failure at {elapsed}s")
                raise RuntimeError(f"Dummy job failed at {elapsed}s")

        # Wait for process to complete
        result = await process.wait()
        if result.exit_code != 0:
            raise RuntimeError(f"Target phase failed: {result.stderr}")
