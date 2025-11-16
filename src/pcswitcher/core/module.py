"""Module interfaces and exceptions for pc-switcher."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from datetime import timedelta
from pathlib import Path
from typing import Any

from pcswitcher.core.logging import LogLevel


class SyncError(Exception):
    """Base exception for all sync-related errors."""

    def __init__(self, message: str | None = None) -> None:
        """Initialize SyncError with optional message.

        Args:
            message: Optional error description
        """
        super().__init__(message)


class RemoteExecutor(ABC):
    """Abstract interface for executing commands on remote machines.

    Provides methods for running commands, transferring files, and
    querying remote machine information.
    """

    @abstractmethod
    def run(
        self,
        command: str,
        sudo: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a command on the remote machine.

        Args:
            command: Shell command to execute
            sudo: Whether to run with sudo privileges
            timeout: Optional timeout in seconds

        Returns:
            CompletedProcess instance with stdout, stderr, and return code

        Raises:
            SyncError: If command execution fails
        """

    @abstractmethod
    def send_file_to_target(self, local: Path, remote: Path) -> None:
        """Transfer a file from local machine to remote target.

        Args:
            local: Path to local file
            remote: Path on remote machine

        Raises:
            SyncError: If file transfer fails
        """

    @abstractmethod
    def get_hostname(self) -> str:
        """Get the hostname of the remote machine.

        Returns:
            Hostname as string

        Raises:
            SyncError: If hostname cannot be determined
        """


class SyncModule(ABC):
    """Abstract base class for all sync modules.

    All sync features (user data, packages, Docker, VMs, k3s) implement this interface.
    The orchestrator manages module lifecycle: validate → pre_sync → sync → post_sync → abort.

    Modules execute sequentially in the order defined in config.yaml sync_modules section.
    """

    def __init__(self, config: dict[str, Any], remote: RemoteExecutor) -> None:
        """Initialize module with validated config and remote executor.

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
        """Unique module identifier (e.g., 'btrfs-snapshots', 'user-data', 'docker').

        Must be unique across all modules.
        Used for config sections, logging context, execution ordering.
        """

    @property
    @abstractmethod
    def required(self) -> bool:
        """Whether this module can be disabled via configuration.

        Required modules (e.g., btrfs-snapshots) cannot be disabled.
        Optional modules (e.g., docker, k3s) can be disabled.
        """

    @abstractmethod
    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON Schema for module configuration validation.

        Orchestrator validates user config against this schema before instantiating module.

        Returns:
            JSON Schema dict (see https://json-schema.org/)
        """

    @abstractmethod
    def validate(self) -> list[str]:
        """Pre-sync validation: check preconditions without modifying state.

        Called during VALIDATING phase before any module executes.
        All modules' validate() methods run before any state changes occur.

        Returns:
            List of validation error messages (empty list = valid)
            Each error should be user-actionable
        """

    @abstractmethod
    def pre_sync(self) -> None:
        """Pre-sync operations executed before main sync.

        Called during EXECUTING phase, after all validations pass.

        Raises:
            SyncError: On unrecoverable errors
        """

    @abstractmethod
    def sync(self) -> None:
        """Main sync operation: transfer data, install packages, etc.

        Called after pre_sync() completes.

        Should call emit_progress() to report progress.
        Should call log() to log operations.

        Raises:
            SyncError: On unrecoverable errors
        """

    @abstractmethod
    def post_sync(self) -> None:
        """Post-sync operations: cleanup, verification, post-snapshots.

        Called after sync() completes successfully.

        Raises:
            SyncError: On unrecoverable errors
        """

    @abstractmethod
    def abort(self, timeout: float) -> None:
        """Stop running processes, free resources (best-effort, limited by timeout).

        Called in scenarios like user interrupt, module exception, etc.

        Semantics: Stop what you're doing NOW, don't undo work.
        Must be idempotent and handle partial state gracefully.
        Should NOT raise exceptions (best-effort cleanup).

        Args:
            timeout: Maximum time to spend in cleanup (seconds)
        """

    def emit_progress(
        self,
        percentage: float | None = None,
        item: str = "",
        eta: timedelta | None = None,
    ) -> None:
        """Report progress to orchestrator for terminal UI display and logging.

        Injected by orchestrator. Module just calls this method.

        Args:
            percentage: Progress as fraction (0.0-1.0) of TOTAL module work, or None if unknown
            item: Description of current operation
            eta: Estimated time to completion (optional)
        """
        raise NotImplementedError("Orchestrator injects this method")

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """Log a message with structured context.

        Injected by orchestrator. Module just calls this method.

        Args:
            level: Log level (DEBUG, FULL, INFO, WARNING, ERROR)
            message: Log message
            **context: Structured context data

        Important: Modules should NOT log at CRITICAL level.
        Modules should raise SyncError instead for unrecoverable failures.
        """
        raise NotImplementedError("Orchestrator injects this method")

    def log_remote_output(
        self,
        hostname: str,
        output: str,
        stream: str = "stdout",
        level: LogLevel = LogLevel.FULL,
    ) -> None:
        """Log output from remote commands with hostname metadata.

        Injected by orchestrator for cross-host log aggregation.
        Automatically includes hostname in log context.

        Args:
            hostname: Hostname of the remote machine
            output: Command output (stdout or stderr)
            stream: Stream name ("stdout" or "stderr")
            level: Log level for output lines (default: FULL for detailed)

        This enables unified log streams containing both source and target
        operations with proper hostname attribution.
        """
        raise NotImplementedError("Orchestrator injects this method")
