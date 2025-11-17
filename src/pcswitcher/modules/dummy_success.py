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
        self.log(LogLevel.INFO, "Validation passed")
        return []

    @override
    def pre_sync(self) -> None:
        """Execute pre-sync operations on source machine.

        Simulates 20-second source operation (FR-039):
        - Logs every 2 seconds
        - WARNING at 6 seconds
        - Emits progress at 0%, 25%, 50%, 75%, 100%
        """
        duration = self.config.get("duration_seconds", 20)
        self.log(LogLevel.INFO, "Starting source phase", duration=duration)

        for i in range(duration):
            if self._aborted:
                self.log(LogLevel.INFO, "Source phase aborted")
                return

            # Emit progress at 0%, 25%, 50%, 75%, 100%
            elapsed_percent = int((i / duration) * 100)
            if i == 0 or elapsed_percent in [25, 50, 75]:
                self.emit_progress(i / duration, f"Source: {elapsed_percent}%")

            # Log every 2 seconds
            if i > 0 and i % 2 == 0:
                self.log(LogLevel.INFO, f"Source phase: {i}s elapsed")

            # WARNING at 6 seconds
            if i == 6:
                self.log(LogLevel.WARNING, "Warning at 6s on source (expected)")

            time.sleep(1)

        self.emit_progress(1.0, "Source: 100%")
        self.log(LogLevel.INFO, "Source phase complete", total_seconds=duration)

    @override
    def sync(self) -> None:
        """Execute main sync operation on target machine.

        Simulates 20-second target operation (FR-039):
        - Logs every 2 seconds
        - ERROR at 8 seconds (recoverable)
        - Emits progress at 0%, 25%, 50%, 75%, 100%

        Respects abort signal for graceful termination.
        """
        duration = self.config.get("duration_seconds", 20)
        self.log(LogLevel.INFO, "Starting target phase", duration=duration)

        for i in range(duration):
            if self._aborted:
                self.log(LogLevel.INFO, "Target phase aborted")
                return

            # Emit progress at 0%, 25%, 50%, 75%, 100%
            elapsed_percent = int((i / duration) * 100)
            if i == 0 or elapsed_percent in [25, 50, 75]:
                self.emit_progress(i / duration, f"Target: {elapsed_percent}%")

            # Log every 2 seconds
            if i > 0 and i % 2 == 0:
                self.log(LogLevel.INFO, f"Target phase: {i}s elapsed")

            # ERROR at 8 seconds (recoverable)
            if i == 8:
                self.log(
                    LogLevel.ERROR,
                    "Error at 8s on target (recoverable, sync continues)",
                )

            time.sleep(1)

        self.emit_progress(1.0, "Target: 100%")
        self.log(LogLevel.INFO, "Target phase complete", total_seconds=duration)

    @override
    def post_sync(self) -> None:
        """Execute post-sync operations.

        For this dummy module, just logs the operation.
        """
        self.log(LogLevel.INFO, "Starting post-sync")
        self.log(LogLevel.INFO, "Post-sync complete")

    @override
    def abort(self, timeout: float) -> None:
        """Stop running operations and cleanup.

        Args:
            timeout: Maximum time to spend in cleanup (seconds)

        Sets abort flag to stop sync loop gracefully.
        Logs "Dummy module abort called" per FR-042.
        """
        self.log(LogLevel.INFO, "Dummy module abort called", timeout=timeout)
        self._aborted = True
