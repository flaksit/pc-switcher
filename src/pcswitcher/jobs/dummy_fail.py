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
        """Execute until configured failure percentage, then raise exception.

        Runs source phase for 0-50%, then target phase for 50-100%.
        Failure occurs at configured percentage (default 60%).
        """
        fail_at_percent = self.context.config.get("fail_at_percent", 60)

        try:
            self._report_progress(ProgressUpdate(percent=0))
            self._log(Host.SOURCE, LogLevel.INFO, f"Dummy fail job will fail at {fail_at_percent}%")

            # Source phase: 0-50% (5 iterations)
            for percent in range(10, 60, 10):
                await asyncio.sleep(1)
                self._report_progress(ProgressUpdate(percent=percent))
                self._log(Host.SOURCE, LogLevel.INFO, f"Source progress: {percent}%")

                if percent >= fail_at_percent:
                    self._log(Host.SOURCE, LogLevel.CRITICAL, f"Simulated failure at {percent}%")
                    raise RuntimeError(f"Dummy job failed at {percent}%")

            # Target phase: 50-100% (execute real commands on target)
            if fail_at_percent > 50:
                await self._run_target_phase(fail_at_percent)

        except asyncio.CancelledError:
            self._log(Host.SOURCE, LogLevel.WARNING, "Dummy fail job cancelled")
            raise

    async def _run_target_phase(self, fail_at_percent: int) -> None:
        """Target phase: execute real commands on target, fail at configured percent.

        Args:
            fail_at_percent: Percentage at which to fail (60-100)
        """
        # Run 5 iterations on target (50-100% in 10% increments)
        iterations = 5
        cmd = f'for i in $(seq 1 {iterations}); do echo "tick $i"; sleep 1; done'

        process = await self.target.start_process(cmd)
        tick = 0

        async for raw_line in process.stdout():
            tick += 1
            line = raw_line.strip()
            percent = 50 + tick * 10

            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Target progress: {percent}% (remote: {line})",
            )
            self._report_progress(ProgressUpdate(percent=percent))

            if percent >= fail_at_percent:
                # Terminate process before raising
                await process.terminate()
                self._log(Host.TARGET, LogLevel.CRITICAL, f"Simulated failure at {percent}%")
                raise RuntimeError(f"Dummy job failed at {percent}%")

        # Wait for process to complete
        result = await process.wait()
        if result.exit_code != 0:
            raise RuntimeError(f"Target phase failed: {result.stderr}")
