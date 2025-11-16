"""Dummy critical module for testing error handling."""

from __future__ import annotations

import time
from typing import Any, override

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.module import RemoteExecutor, SyncError, SyncModule


class DummyCriticalModule(SyncModule):
    """Test module that raises SyncError at 50% progress.

    This module demonstrates exception-based error handling:
    - Module raises SyncError (not log CRITICAL)
    - Orchestrator catches exception
    - Orchestrator logs exception as CRITICAL
    - Orchestrator calls abort() and initiates CLEANUP phase

    Used for testing error handling, abort logic, and orchestrator cleanup.
    """

    def __init__(self, config: dict[str, Any], remote: RemoteExecutor) -> None:
        """Initialize DummyCriticalModule.

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
        return "dummy_critical"

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
                    "description": "Duration before raising exception",
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
        self.callbacks.log(LogLevel.INFO, "Validation passed")
        return []

    @override
    def pre_sync(self) -> None:
        """Execute pre-sync operations.

        For this dummy module, just logs the operation.
        """
        self.callbacks.log(LogLevel.INFO, "Starting pre-sync")
        self.callbacks.log(LogLevel.INFO, "Pre-sync complete")

    @override
    def sync(self) -> None:
        """Execute sync operation that fails at 50%.

        Raises:
            SyncError: Always raised at 50% progress to test error handling
        """
        duration = self.config.get("duration_seconds", 20)
        self.callbacks.log(LogLevel.INFO, "Starting sync (will fail at 50%)", duration=duration)

        for i in range(duration):
            if self._aborted:
                self.callbacks.log(LogLevel.INFO, "Sync aborted before critical failure")
                return

            progress = (i + 1) / duration
            self.callbacks.emit_progress(progress, f"Step {i + 1}/{duration}")

            # Raise exception at 50% progress
            if i == duration // 2:
                self.callbacks.log(LogLevel.INFO, "About to raise critical error")
                raise SyncError("Simulated critical failure at 50% progress for testing")

            time.sleep(1)

    @override
    def post_sync(self) -> None:
        """Execute post-sync operations.

        Note: This should never be called since sync() raises exception.
        """
        self.callbacks.log(LogLevel.INFO, "Starting post-sync")
        self.callbacks.log(LogLevel.INFO, "Post-sync complete")

    @override
    def abort(self, timeout: float) -> None:
        """Stop running operations and cleanup.

        Args:
            timeout: Maximum time to spend in cleanup (seconds)

        Sets abort flag to stop sync loop gracefully.
        """
        self.callbacks.log(LogLevel.INFO, "abort() called after exception", timeout=timeout)
        self._aborted = True
