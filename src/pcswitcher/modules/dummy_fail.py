"""Dummy fail module for testing unhandled exception handling."""

from __future__ import annotations

import time
from typing import Any, override

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.module import RemoteExecutor, SyncModule


class DummyFailModule(SyncModule):
    """Test module that raises unhandled exception at 60% progress.

    This module demonstrates unhandled exception behavior:
    - Module raises generic Exception (not SyncError)
    - Orchestrator catches it and treats it as critical failure
    - Orchestrator logs exception as CRITICAL
    - Orchestrator calls abort() and initiates CLEANUP phase

    Used for testing robustness of error handling with unexpected exceptions.
    """

    def __init__(self, config: dict[str, Any], remote: RemoteExecutor) -> None:
        """Initialize DummyFailModule.

        Args:
            config: Module configuration
            remote: Remote executor interface
        """
        super().__init__(config, remote)
        self._aborted = False

    @property
    @override
    def name(self) -> str:
        """Module identifier."""
        return "dummy_fail"

    @property
    @override
    def required(self) -> bool:
        """This is an optional test module."""
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
        """Validate module configuration.

        Returns:
            Empty list (always validates successfully)
        """
        self.log(LogLevel.INFO, "Validation passed")
        return []

    @override
    def pre_sync(self) -> None:
        """Execute pre-sync operations.

        For this dummy module, just logs the operation.
        """
        self.log(LogLevel.INFO, "Starting pre-sync")
        self.log(LogLevel.INFO, "Pre-sync complete")

    @override
    def sync(self) -> None:
        """Execute sync operation that fails with unhandled exception at 60%.

        Raises:
            RuntimeError: Always raised at 60% progress to test unhandled exception handling
        """
        duration = self.config.get("duration_seconds", 20)
        self.log(LogLevel.INFO, "Starting sync (will fail at 60%)", duration=duration)

        for i in range(duration):
            if self._aborted:
                self.log(LogLevel.INFO, "Sync aborted before failure")
                return

            progress = (i + 1) / duration
            self.emit_progress(progress, f"Step {i + 1}/{duration}")

            # Raise unhandled exception at 60% progress
            if i == int(duration * 0.6):
                self.log(LogLevel.INFO, "About to raise unhandled exception")
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
        """
        self.log(LogLevel.INFO, "abort() called after unhandled exception", timeout=timeout)
        self._aborted = True
