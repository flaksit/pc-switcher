"""
Module Interface Contract

This file defines the standardized interface that all sync modules must implement.
It serves as both documentation and a reference implementation for module developers.

See User Story 1 in spec.md for complete requirements.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import timedelta
from enum import StrEnum
from typing import Any

import structlog


class LogLevel(StrEnum):
    """Six-level logging hierarchy (FR-019)"""

    DEBUG = "DEBUG"  # 10: Verbose diagnostic information
    FULL = "FULL"  # 15: File-level operation details
    INFO = "INFO"  # 20: High-level operation reporting
    WARNING = "WARNING"  # 30: Unexpected but non-failing conditions
    ERROR = "ERROR"  # 40: Recoverable errors
    CRITICAL = "CRITICAL"  # 50: Unrecoverable errors (triggers sync abort)


class ModuleResult(StrEnum):
    """Module execution result"""

    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class SyncModule(ABC):
    """
    Abstract base class for all sync modules.

    All sync features (user data, packages, Docker, VMs, k3s) implement this interface.
    The orchestrator manages module lifecycle: validate → pre_sync → sync → post_sync → cleanup.

    Requirements: FR-001, FR-002, FR-003, FR-004
    User Story: 1 (Module Architecture and Integration Contract)
    """

    def __init__(self, config: dict[str, Any], logger: structlog.BoundLogger) -> None:
        """
        Initialize module with validated config and logger.

        Args:
            config: Module-specific configuration (validated against schema)
            logger: structlog logger with bound context (module name, session ID)

        Note: Orchestrator calls this after validating config against get_config_schema()
        """
        self.config = config
        self.logger = logger

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Unique module identifier (e.g., "btrfs-snapshots", "user-data", "docker").

        Must be unique across all registered modules.
        Used for config sections, logging context, dependency references.
        """
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """
        Module version (semantic versioning: X.Y.Z).

        Used for compatibility checks and debugging.
        """
        ...

    @property
    @abstractmethod
    def dependencies(self) -> Sequence[str]:
        """
        Module names this module must run after (topological ordering).

        Example: user-data module might depend on ["btrfs-snapshots-pre"]
        to ensure snapshots are created before data sync.

        Returns:
            List of module names (can be empty if no dependencies)

        Requirement: FR-002
        """
        ...

    @property
    @abstractmethod
    def required(self) -> bool:
        """
        Whether this module can be disabled via configuration.

        Required modules (e.g., btrfs-snapshots) cannot be disabled.
        Optional modules (e.g., docker, k3s) can be disabled.

        Requirement: FR-012, FR-035
        """
        ...

    @abstractmethod
    def get_config_schema(self) -> dict[str, Any]:
        """
        Return JSON Schema for module configuration validation.

        Orchestrator validates user config against this schema before instantiating module.

        Example:
            {
                "type": "object",
                "properties": {
                    "enabled": {"type": "boolean"},
                    "exclude_patterns": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["enabled"]
            }

        Returns:
            JSON Schema dict (see https://json-schema.org/)

        Requirement: FR-031
        """
        ...

    @abstractmethod
    def validate(self) -> list[str]:
        """
        Pre-sync validation: check preconditions without modifying state.

        Called during VALIDATING phase before any module executes.
        All modules' validate() methods run before any state changes occur.

        Examples of validations:
        - Check required tools are installed (btrfs, docker, etc.)
        - Verify target paths exist or can be created
        - Check sufficient disk space
        - Validate config values are sensible

        Returns:
            List of validation error messages (empty list = valid)
            Each error should be user-actionable (e.g., "btrfs not installed on target")

        Requirements: FR-003, User Story 1 Scenario 5
        """
        ...

    @abstractmethod
    def pre_sync(self) -> None:
        """
        Pre-sync operations executed before main sync.

        Called during EXECUTING phase, after all validations pass.
        Examples: create snapshots, prepare temporary directories, lock resources.

        May raise exceptions on unrecoverable errors (orchestrator will catch and abort).
        May log at CRITICAL level to trigger abort.

        Requirement: FR-003
        """
        ...

    @abstractmethod
    def sync(self) -> None:
        """
        Main sync operation: transfer data, install packages, etc.

        Called after pre_sync() completes.
        This is where the actual work happens.

        Should emit progress updates via emit_progress().
        May raise exceptions on unrecoverable errors.
        May log at CRITICAL level to trigger abort.

        Requirement: FR-003
        """
        ...

    @abstractmethod
    def post_sync(self) -> None:
        """
        Post-sync operations: cleanup, verification, post-snapshots.

        Called after sync() completes successfully.
        Examples: create post-sync snapshots, verify checksums, update metadata.

        May raise exceptions on unrecoverable errors.
        May log at CRITICAL level to trigger abort.

        Requirement: FR-003
        """
        ...

    @abstractmethod
    def cleanup(self) -> None:
        """
        Cleanup on shutdown, interrupts, or errors (best-effort).

        Called in these scenarios:
        - Normal completion (after post_sync)
        - User interrupt (Ctrl+C)
        - Module raises exception
        - Another module logs CRITICAL

        Must be idempotent and handle partial state gracefully.
        Should not raise exceptions (orchestrator logs but continues shutdown).

        Examples: release locks, delete temporary files, close connections.

        Requirements: FR-004, FR-025, User Story 5
        """
        ...

    def emit_progress(
        self,
        percentage: int,
        current_item: str = "",
        eta: timedelta | None = None,
    ) -> None:
        """
        Report progress to orchestrator for terminal UI display and logging.

        Args:
            percentage: Progress percentage (0-100)
            current_item: Description of current operation (e.g., "Copying file.txt")
            eta: Estimated time to completion (optional)

        This method is provided by the base class and calls orchestrator.
        Modules just call it to report progress.

        Requirements: FR-044, FR-045, FR-046, User Story 9
        """
        # Implementation provided by base class (injected by orchestrator)
        # This is a placeholder showing the interface
        raise NotImplementedError("Orchestrator must inject progress callback")

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """
        Log a message with structured context.

        This is a convenience wrapper around self.logger.
        Modules can also use self.logger directly if preferred.

        Args:
            level: Log level (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL)
            message: Log message
            **context: Structured context data (e.g., file_path="/home/user/file.txt")

        CRITICAL level triggers immediate sync abort (FR-020).

        Requirements: FR-019 through FR-024, User Story 4
        """
        # Map our log levels to standard library levels
        level_map = {
            LogLevel.DEBUG: 10,
            LogLevel.FULL: 15,
            LogLevel.INFO: 20,
            LogLevel.WARNING: 30,
            LogLevel.ERROR: 40,
            LogLevel.CRITICAL: 50,
        }
        self.logger.log(level_map[level], message, **context)


# Example: Minimal module implementation demonstrating the contract
class DummySuccessModule(SyncModule):
    """
    Reference implementation of module interface.

    This dummy module demonstrates:
    - All required properties and methods
    - Config schema definition
    - Validation logic
    - Progress reporting
    - Logging at various levels
    - Cleanup handling

    See User Story 8 for complete requirements.
    """

    @property
    def name(self) -> str:
        return "dummy-success"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def dependencies(self) -> Sequence[str]:
        return []  # No dependencies

    @property
    def required(self) -> bool:
        return False  # Optional module

    def get_config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "duration_seconds": {"type": "integer", "minimum": 1, "default": 20},
            },
            "required": ["enabled"],
        }

    def validate(self) -> list[str]:
        """Always validates successfully"""
        self.logger.info("Validation passed")
        return []

    def pre_sync(self) -> None:
        """Simulate pre-sync operation"""
        self.logger.info("Starting pre-sync")
        # No actual work in dummy module
        self.logger.info("Pre-sync complete")

    def sync(self) -> None:
        """Simulate long-running sync with progress reporting"""
        import time

        duration = self.config.get("duration_seconds", 20)
        self.logger.info("Starting sync", duration=duration)

        for i in range(duration):
            percentage = int((i + 1) / duration * 100)
            self.emit_progress(percentage, f"Processing step {i + 1}/{duration}")

            if i == 6:
                self.logger.warning("Example warning at 30%")
            if i == 8:
                self.logger.error("Example error at 40% (non-critical)")

            time.sleep(1)

        self.logger.info("Sync complete")

    def post_sync(self) -> None:
        """Simulate post-sync operation"""
        self.logger.info("Starting post-sync")
        # No actual work in dummy module
        self.logger.info("Post-sync complete")

    def cleanup(self) -> None:
        """Best-effort cleanup"""
        self.logger.info("Cleanup called")
        # No resources to release in dummy module
