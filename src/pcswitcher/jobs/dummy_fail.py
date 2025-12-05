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
        """Execute until configured failure percentage, then raise exception."""
        fail_at_percent = self.context.config.get("fail_at_percent", 60)

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
