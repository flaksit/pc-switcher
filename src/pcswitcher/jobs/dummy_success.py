"""Dummy success job for infrastructure validation (CORE-FR-DUMMY-SIM)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel, ProgressUpdate

if TYPE_CHECKING:
    from pcswitcher.models import ValidationError


class DummySuccessJob(SyncJob):
    """Dummy job for testing infrastructure (CORE-FR-DUMMY-SIM).

    Simulates configurable duration operation on source (log every 2s, WARNING at 6s)
    and target (log every 2s, ERROR at 8s).
    Progress milestones: 0% (start) → 25% (halfway source) → 50% (end source)
                       → 75% (halfway target) → 100% (end target)
    """

    name: ClassVar[str] = "dummy_success"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source_duration": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "description": "Seconds to run on source",
            },
            "target_duration": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "description": "Seconds to run on target",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        """Initialize success job with context.

        Args:
            context: JobContext with executors, config, and event bus
        """
        super().__init__(context)
        self.source_duration = context.config.get("source_duration", 20)
        self.target_duration = context.config.get("target_duration", 20)

    async def validate(self) -> list[ValidationError]:
        """No prerequisites for dummy job."""
        return []

    async def execute(self) -> None:
        """Execute test sequence with logging and progress reporting."""
        try:
            self._report_progress(ProgressUpdate(percent=0))

            # Source phase with configurable duration
            await self._run_source_phase()
            self._report_progress(ProgressUpdate(percent=50))

            # Target phase with configurable duration
            await self._run_target_phase()
            self._report_progress(ProgressUpdate(percent=100))

        except asyncio.CancelledError:
            self._log(Host.SOURCE, LogLevel.WARNING, "Dummy job termination requested")
            raise

    async def _run_source_phase(self) -> None:
        """Source phase: configurable duration, log every 2s, WARNING at 6s."""
        iterations = self.source_duration // 2
        halfway = self.source_duration // 2

        for tick in range(iterations):
            elapsed = (tick + 1) * 2
            self._log(
                Host.SOURCE,
                LogLevel.INFO,
                f"Source phase: {elapsed}s elapsed",
            )

            # WARNING at 6s
            if elapsed == 6:
                self._log(Host.SOURCE, LogLevel.WARNING, "Test warning at 6s")

            # Progress: 25% at halfway through source phase
            if elapsed == halfway * 2:
                self._report_progress(ProgressUpdate(percent=25))

            await asyncio.sleep(2)

    async def _run_target_phase(self) -> None:
        """Target phase: execute real commands on target machine.

        Runs a bash loop on the target that outputs every 2 seconds.
        This validates that job execution actually reaches the target machine.
        """
        iterations = self.target_duration // 2
        halfway = iterations // 2

        # Build bash command that outputs every 2 seconds
        # Output format: "tick N" where N is 1-based iteration count
        cmd = f'for i in $(seq 1 {iterations}); do echo "tick $i"; sleep 2; done'

        process = await self.target.start_process(cmd)
        tick = 0

        async for raw_line in process.stdout():
            tick += 1
            line = raw_line.strip()
            elapsed = tick * 2

            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Target phase: {elapsed}s elapsed (remote: {line})",
            )

            # ERROR at 8s
            if elapsed == 8:
                self._log(Host.TARGET, LogLevel.ERROR, "Test error at 8s")

            # Progress: 75% at halfway through target phase
            if tick == halfway:
                self._report_progress(ProgressUpdate(percent=75))

        # Wait for process to complete
        result = await process.wait()
        if result.exit_code != 0:
            raise RuntimeError(f"Target phase failed: {result.stderr}")
