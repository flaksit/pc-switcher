"""Dummy fail job for testing unhandled exception handling."""

from __future__ import annotations

import time
from typing import Any, override

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.job import RemoteExecutor, SyncJob


class DummyFailJob(SyncJob):
    """Test job that raises unhandled exception at 60% progress.

    This job demonstrates unhandled exception behavior:
    - Job raises generic Exception (not SyncError)
    - Orchestrator catches it and treats it as critical failure
    - Orchestrator logs exception as CRITICAL
    - Orchestrator calls abort() and initiates CLEANUP phase

    Used for testing robustness of error handling with unexpected exceptions.
    """

    def __init__(self, config: dict[str, Any], remote: RemoteExecutor) -> None:
        """Initialize DummyFailJob.

        Args:
            config: Job configuration
            remote: Remote executor interface
        """
        super().__init__(config, remote)
        self._aborted = False

    @property
    @override
    def name(self) -> str:
        """Job identifier."""
        return "dummy_fail"

    @property
    @override
    def required(self) -> bool:
        """This is an optional test job."""
        return False

    @override
    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON Schema for configuration validation.

        Returns:
            Schema allowing optional duration_seconds parameter
        """
        return {
            "type": "object",
            "properties": {
                "duration_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 20,
                    "description": "Duration before raising unhandled exception",
                },
            },
            "additionalProperties": False,
        }

    @override
    def validate(self) -> list[str]:
        """Validate job configuration.

        Returns:
            Empty list (always validates successfully)
        """
        self.log(LogLevel.INFO, "Validation passed")
        return []

    @override
    def pre_sync(self) -> None:
        """Execute pre-sync operations.

        For this dummy job, just logs the operation.
        """
        self.log(LogLevel.INFO, "Starting pre-sync")
        self.log(LogLevel.INFO, "Pre-sync complete")

    @override
    def sync(self) -> None:
        """Execute sync operation that fails with unhandled exception at exactly 60%.

        Raises:
            RuntimeError: Always raised at exactly 60% progress (FR-041)
        """
        duration = self.config.get("duration_seconds", 20)
        self.log(LogLevel.INFO, "Starting sync (will fail at exactly 60%)", duration=duration)

        for i in range(duration):
            if self._aborted:
                self.log(LogLevel.INFO, "Sync aborted before failure")
                return

            progress = (i + 1) / duration
            self.emit_progress(progress, f"Step {i + 1}/{duration}")

            # Raise unhandled exception at exactly 60% progress (FR-041)
            # When i=11, progress = 12/20 = 0.6 = 60%
            if progress >= 0.6:
                self.log(LogLevel.INFO, f"Raising exception at {progress * 100:.0f}% progress")
                raise RuntimeError("Simulated unhandled exception at 60% progress for testing")

            time.sleep(1)

    @override
    def post_sync(self) -> None:
        """Execute post-sync operations.

        Note: This should never be called since sync() raises exception.
        """
        self.log(LogLevel.INFO, "Starting post-sync")
        self.log(LogLevel.INFO, "Post-sync complete")

    @override
    def abort(self, timeout: float) -> None:
        """Stop running operations and cleanup.

        Args:
            timeout: Maximum time to spend in cleanup (seconds)

        Sets abort flag to stop sync loop gracefully.
        Logs "Dummy job abort called" per FR-042.
        """
        self.log(LogLevel.INFO, "Dummy job abort called", timeout=timeout)
        self._aborted = True
