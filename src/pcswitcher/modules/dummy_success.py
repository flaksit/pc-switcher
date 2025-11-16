"""Dummy success module for testing pc-switcher."""

from __future__ import annotations

import time
from typing import Any, override

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.module import RemoteExecutor, SyncModule


class DummySuccessModule(SyncModule):
    """Test module that simulates successful sync with progress reporting.

    This module demonstrates:
    - Progress reporting (0-100% over 20 seconds)
    - Logging at various levels (INFO, WARNING, ERROR)
    - Graceful abort handling
    - Config schema with default values

    Used for testing orchestrator, UI, and logging functionality.
    """

    def __init__(self, config: dict[str, Any], remote: RemoteExecutor) -> None:
        """Initialize DummySuccessModule.

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
        return "dummy_success"

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
                    "description": "Duration of sync simulation in seconds",
                },
            },
            "additionalProperties": False,
        }

    @override
    def validate(self) -> list[str]:
        """Validate module configuration and environment.

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
        """Execute main sync operation with progress reporting.

        Simulates a 20-second sync operation with:
        - Progress updates every second (0-100%)
        - INFO log every 2 seconds
        - WARNING at 6 seconds (30%)
        - ERROR at 8 seconds (40%)

        Respects abort signal for graceful termination.
        """
        duration = self.config.get("duration_seconds", 20)
        self.callbacks.log(LogLevel.INFO, "Starting sync", duration=duration)

        for i in range(duration):
            if self._aborted:
                self.callbacks.log(LogLevel.INFO, "Sync aborted by user")
                return

            # Calculate and report progress (0.0 to 1.0)
            progress = (i + 1) / duration
            self.callbacks.emit_progress(progress, f"Processing step {i + 1}/{duration}")

            # Log at various levels to test logging
            if (i + 1) % 2 == 0:
                self.callbacks.log(LogLevel.INFO, f"Completed step {i + 1}/{duration}")

            if i == 6:
                self.callbacks.log(LogLevel.WARNING, "Example warning at 30% progress")

            if i == 8:
                self.callbacks.log(
                    LogLevel.ERROR,
                    "Example ERROR at 40% progress (recoverable, sync continues)",
                )

            time.sleep(1)

        self.callbacks.log(LogLevel.INFO, "Sync complete", total_steps=duration)

    @override
    def post_sync(self) -> None:
        """Execute post-sync operations.

        For this dummy module, just logs the operation.
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
        self.callbacks.log(LogLevel.INFO, "abort() called", timeout=timeout)
        self._aborted = True
