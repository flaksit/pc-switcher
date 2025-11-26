"""
Module Interface Contract

This file defines the standardized interface that all modules must implement.
It serves as both documentation and a reference implementation for module developers.

See User Story 1 in spec.md for complete requirements.

Key Design Decisions:
- Simple lifecycle: validate() → execute() → abort() (removed unnecessary pre_sync/sync/post_sync complexity)
- Modules execute sequentially in config order (no dependencies field)
- Modules raise exceptions for critical failures (not log CRITICAL)
- RemoteExecutor injected for target communication
- Logging and progress methods injected by orchestrator
- ProgressUpdate uses optional float 0.0-1.0
- BtrfsSnapshotModule is instantiated twice by orchestrator (phase="pre" and phase="post")
  to bracket all sync operations, but inherits same Module infrastructure
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any


class LogLevel(StrEnum):
    """Six-level logging hierarchy (FR-002)"""

    DEBUG = "DEBUG"  # 10: Verbose diagnostic information
    FULL = "FULL"  # 15: File-level operation details
    INFO = "INFO"  # 20: High-level operation reporting
    WARNING = "WARNING"  # 30: Unexpected but non-failing conditions
    ERROR = "ERROR"  # 40: Recoverable errors
    CRITICAL = "CRITICAL"  # 50: Unrecoverable errors (logged by orchestrator, not modules)


class SyncError(Exception):
    """Base exception for sync failures. Orchestrator logs as CRITICAL and aborts."""


class RemoteExecutor:
    """
    Interface for modules to execute commands on target machine.

    Abstracts SSH details from modules, enabling easier testing and cleaner code.
    Orchestrator creates this wrapper around TargetConnection and injects into modules.
    """

    def run(
        self,
        command: str,
        sudo: bool = False,
        timeout: float | None = None,
    ) -> CompletedProcess[str]:
        """
        Execute command on target machine.

        Args:
            command: Shell command to execute
            sudo: Whether to run with sudo
            timeout: Command timeout in seconds (None = no timeout)

        Returns:
            CompletedProcess with returncode, stdout, stderr, args

        Raises:
            SyncError: If command execution fails critically
        """
        raise NotImplementedError("Orchestrator provides implementation")

    def send_file_to_target(self, local: Path, remote: Path) -> None:
        """
        Upload file from source to target.

        Args:
            local: Path on source machine
            remote: Path on target machine

        Raises:
            SyncError: If file transfer fails

        Note: Currently does not set permissions/ownership (future feature)
        """
        raise NotImplementedError("Orchestrator provides implementation")

    def get_hostname(self) -> str:
        """
        Get target machine hostname.

        Returns:
            Target hostname (e.g., "workstation", not "target")
        """
        raise NotImplementedError("Orchestrator provides implementation")


class Module(ABC):
    """
    Abstract base class for all pc-switcher modules.

    All operations (sync features, snapshot infrastructure) implement this interface.
    The orchestrator manages module lifecycle: validate → execute → abort.

    User-configurable sync modules (SyncModule) execute sequentially in config.yaml order.
    Orchestrator-managed infrastructure modules (like BtrfsSnapshotModule) are hardcoded
    and execute at specific points in the workflow (before/after all sync modules).

    Requirements: FR-001, FR-002, FR-002, FR-002
    User Story: 1 (Module Architecture and Integration Contract)
    """

    def __init__(self, config: dict[str, Any], remote: RemoteExecutor) -> None:
        """
        Initialize module with validated config and remote executor.

        Args:
            config: Module-specific configuration (validated against schema)
            remote: Interface for executing commands on target

        Note: Orchestrator calls this after validating config against get_config_schema().
        Orchestrator also injects log() and emit_progress() methods after instantiation.
        """
        self.config: dict[str, Any] = config
        self.remote: RemoteExecutor = remote

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Unique module identifier (e.g., "btrfs-snapshots", "user-data", "docker").

        Must be unique across all modules.
        Used for config sections, logging context, execution ordering.
        """

    @property
    @abstractmethod
    def required(self) -> bool:
        """
        Whether this module can be disabled via configuration.

        Required modules (e.g., btrfs-snapshots) cannot be disabled.
        Optional modules (e.g., docker, k3s) can be disabled.

        Requirement: FR-002, FR-002
        """

    @abstractmethod
    def get_config_schema(self) -> dict[str, Any]:
        """
        Return JSON Schema for module configuration validation.

        Orchestrator validates user config against this schema before instantiating module.

        Example:
            {
                "type": "object",
                "properties": {
                    "snapshot_dir": {"type": "string", "default": "/.snapshots"},
                    "subvolumes": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["subvolumes"]
            }

        Returns:
            JSON Schema dict (see https://json-schema.org/)

        Requirement: FR-002
        """

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

        Requirements: FR-002, User Story 1 Scenario 5
        """

    @abstractmethod
    def execute(self) -> None:
        """
        Execute the module's operation.

        Called during EXECUTING phase, after all validations pass.
        This is where the module does its work.

        Modules structure their work internally as needed (preparation, main work,
        verification, etc.). For user visibility, use logging and progress reporting.

        Should call emit_progress() to report progress (0.0-1.0 representing all work).
        Should call log() to log operations at appropriate levels.

        Examples:
        - BtrfsSnapshotModule(phase="pre"): Create pre-sync snapshots
        - PackagesModule: Sync packages from source to target
        - BtrfsSnapshotModule(phase="post"): Create post-sync snapshots

        Raises:
            SyncError: On unrecoverable errors (orchestrator logs as CRITICAL and aborts)

        Requirement: FR-002
        """

    @abstractmethod
    def abort(self, timeout: float) -> None:
        """
        Stop running processes, free resources (best-effort, limited by timeout).

        Called in these scenarios:
        - User interrupt (Ctrl+C)
        - Module raises exception
        - Another module raises exception

        Semantics: Stop what you're doing NOW, don't undo work.
        - Stop any running subprocesses
        - Close file handles
        - Release locks
        - Do NOT delete files or rollback changes (that's manual rollback)

        Must be idempotent and handle partial state gracefully.
        Should NOT raise exceptions (best-effort cleanup).

        Args:
            timeout: Maximum time to spend in cleanup (seconds)

        Example:
            def abort(self, timeout: float):
                if self.subprocess:
                    try:
                        self.subprocess.terminate()
                        self.subprocess.wait(timeout=min(timeout, 2.0))
                    except Exception:
                        pass  # Best-effort

        Requirements: FR-002, FR-002, User Story 5

        Note: Only called on the currently-running module, not on completed modules.
        """

    # Injected methods (provided by orchestrator, signatures shown for documentation)

    def emit_progress(
        self,
        percentage: float | None = None,
        item: str = "",
        eta: timedelta | None = None,
    ) -> None:
        """
        Report progress to orchestrator for terminal UI display and logging.

        Injected by orchestrator. Module just calls this method.

        Args:
            percentage: Progress as fraction (0.0-1.0) of TOTAL module work, or None if unknown
            item: Description of current operation (e.g., "Copying file.txt")
            eta: Estimated time to completion (optional)

        Important: percentage represents ALL module work (validation + execution),
        not just the current subtask or operation.

        Requirements: FR-002, FR-002, FR-002, User Story 9
        """
        raise NotImplementedError("Orchestrator injects this method")

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """
        Log a message with structured context.

        Injected by orchestrator. Module just calls this method.

        Args:
            level: Log level (DEBUG, FULL, INFO, WARNING, ERROR)
            message: Log message
            **context: Structured context data (e.g., file_path="/home/user/file.txt")

        Important: Modules should NOT log at CRITICAL level.
        Modules should raise SyncError instead for unrecoverable failures.
        Orchestrator catches exceptions and logs them as CRITICAL.

        ERROR level: Use for recoverable errors (individual file failures, etc.)
        Orchestrator tracks ERROR logs to determine final state (COMPLETED vs FAILED).

        Requirements: FR-002 through FR-002, User Story 4
        """
        raise NotImplementedError("Orchestrator injects this method")


class SyncModule(Module):
    """
    Subclass for user-configurable sync modules.

    This is a marker/documentation class showing that sync modules (packages, docker, VMs, etc.)
    are configured by users in config.yaml sync_modules section.

    Infrastructure modules (like BtrfsSnapshotModule) inherit directly from Module and are
    managed by the orchestrator, not user-configurable.

    This subclass currently adds no additional methods/behavior, but provides conceptual clarity
    and future extensibility for sync-specific functionality if needed.
    """
    pass


# Example: Minimal module implementation demonstrating the contract
class DummySuccessModule(SyncModule):
    """
    Reference implementation of module interface.

    This dummy module demonstrates:
    - All required properties and methods
    - Config schema definition
    - Validation logic
    - Progress reporting (float 0.0-1.0 representing total work)
    - Logging at various levels
    - Exception-based error handling
    - abort() for graceful cleanup

    See User Story 8 for complete requirements.
    """

    @property
    def name(self) -> str:
        return "dummy-success"

    @property
    def required(self) -> bool:
        return False  # Optional module

    def get_config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "duration_seconds": {"type": "integer", "minimum": 1, "default": 40},
            },
        }

    def validate(self) -> list[str]:
        """Always validates successfully"""
        self.log(LogLevel.INFO, "Validation passed")
        return []

    def execute(self) -> None:
        """Simulate long-running operation with progress reporting"""
        import time

        duration = self.config.get("duration_seconds", 40)
        self.log(LogLevel.INFO, "Starting execution", duration=duration)

        # Simulate source-side operation (first 20s)
        self.log(LogLevel.INFO, "Processing on source machine")
        for i in range(20):
            progress = (i + 1) / duration
            self.emit_progress(progress, f"Source operation step {i + 1}/20")

            if i == 6:
                self.log(LogLevel.WARNING, "Example warning at 17.5%")

            time.sleep(1)

        # Simulate target-side operation (next 20s)
        self.log(LogLevel.INFO, "Processing on target machine")
        for i in range(20, 40):
            progress = (i + 1) / duration
            self.emit_progress(progress, f"Target operation step {i + 1 - 20}/20")

            if i == 28:
                self.log(LogLevel.ERROR, "Example ERROR at 72.5% (recoverable, execution continues)")

            time.sleep(1)

        self.log(LogLevel.INFO, "Execution complete")

    def abort(self, timeout: float) -> None:
        """Best-effort cleanup"""
        self.log(LogLevel.INFO, "Dummy module abort called", timeout=timeout)
        # No resources to release in dummy module
