"""Module interfaces and exceptions for pc-switcher."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from pcswitcher.core.logging import LogLevel


class SyncModuleCallbacks(Protocol):
    """Protocol for orchestrator-provided callbacks. Internal implementation detail.

    Module developers should use SyncModule.emit_progress(), SyncModule.log(), and
    SyncModule.log_remote_output() methods directly instead of accessing this protocol.
    """

    def emit_progress(
        self,
        percentage: float | None = None,
        item: str = "",
        eta: timedelta | None = None,
    ) -> None:
        """Emit progress update. See SyncModule.emit_progress() for full documentation."""
        ...

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """Log a message. See SyncModule.log() for full documentation."""
        ...

    def log_remote_output(
        self,
        hostname: str,
        output: str,
        stream: str = "stdout",
        level: LogLevel = LogLevel.FULL,
    ) -> None:
        """Log remote output. See SyncModule.log_remote_output() for full documentation."""
        ...


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
        Orchestrator also injects callbacks via set_callbacks() after instantiation.
        """
        self.config: dict[str, Any] = config
        self.remote: RemoteExecutor = remote
        self._callbacks: SyncModuleCallbacks | None = None

    def set_callbacks(self, callbacks: SyncModuleCallbacks) -> None:
        """Set the callbacks for this module (called by orchestrator).

        Args:
            callbacks: Callbacks object implementing SyncModuleCallbacks protocol
        """
        self._callbacks = callbacks

    @property
    def callbacks(self) -> SyncModuleCallbacks:
        """Get the callbacks object for logging and progress reporting.

        Returns:
            Callbacks object

        Raises:
            RuntimeError: If callbacks not yet injected by orchestrator
        """
        if self._callbacks is None:
            raise RuntimeError(
                f"Callbacks not injected for module {self.__class__.__name__}. "
                "Orchestrator must call set_callbacks() before module execution."
            )
        return self._callbacks

    def emit_progress(
        self,
        percentage: float | None = None,
        item: str = "",
        eta: timedelta | None = None,
    ) -> None:
        """Report sync progress to the orchestrator.

        Call this method during sync() to provide progress feedback for
        terminal UI display and logging. Progress is per-module (not global).

        Args:
            percentage: Progress as fraction (0.0-1.0) of total module work.
                        Use None if total work is unknown (indeterminate progress).
            item: Brief description of current operation (e.g., "Copying /home/user/docs").
                  Keep concise for terminal display.
            eta: Estimated time to completion for this module. Optional.

        Example:
            self.emit_progress(0.5, "Transferring Docker images")
            self.emit_progress(0.75, "Syncing volumes", eta=timedelta(minutes=2))
            self.emit_progress(item="Processing unknown number of items")  # indeterminate
        """
        self.callbacks.emit_progress(percentage, item, eta)

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """Log a message with structured context.

        Use this method to log operations, warnings, and errors during module execution.
        All log messages are tagged with module name automatically by the orchestrator.

        Args:
            level: Log level determining visibility:
                   - DEBUG: Development/troubleshooting details (rarely shown)
                   - FULL: Verbose operational details (shown in verbose mode)
                   - INFO: Normal operational messages (default visibility)
                   - WARNING: Recoverable issues that may need attention
                   - ERROR: Serious issues that may affect sync integrity
            message: Human-readable log message describing the event.
            **context: Structured key-value data for machine-readable logging.
                       Common keys: subvolume, file_count, duration_seconds, etc.

        Important:
            - Do NOT log at CRITICAL level. Instead, raise SyncError for
              unrecoverable failures. The orchestrator logs CRITICAL errors.

        Example:
            self.log(LogLevel.INFO, "Starting package sync", package_count=150)
            self.log(LogLevel.WARNING, "Package cache outdated", cache_age_days=30)
            self.log(LogLevel.ERROR, "Failed to sync optional file", path="/etc/foo")
        """
        self.callbacks.log(level, message, **context)

    def log_remote_output(
        self,
        hostname: str,
        output: str,
        stream: str = "stdout",
        level: LogLevel = LogLevel.FULL,
    ) -> None:
        """Log output from remote command execution.

        Use this after running commands on the target machine to capture their
        output with proper hostname tagging. Output is typically logged at FULL
        level to avoid cluttering normal output but remain available for debugging.

        Args:
            hostname: Hostname of the remote machine (use self.remote.get_hostname()).
            output: Raw command output (stdout or stderr string).
            stream: Which output stream this came from ("stdout" or "stderr").
                    Helps identify error output during troubleshooting.
            level: Log level for the output lines (default: FULL for detailed logs).
                   Use WARNING or ERROR for important error output.

        Example:
            result = self.remote.run("apt update", sudo=True)
            if result.stdout:
                self.log_remote_output(self.remote.get_hostname(), result.stdout)
            if result.stderr:
                self.log_remote_output(
                    self.remote.get_hostname(), result.stderr, stream="stderr", level=LogLevel.WARNING
                )
        """
        self.callbacks.log_remote_output(hostname, output, stream, level)

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique module identifier (e.g., 'btrfs_snapshots', 'user_data', 'docker').

        Must be unique across all modules (using underscores per Python convention).
        Used for config sections, logging context, execution ordering.
        """

    @property
    @abstractmethod
    def required(self) -> bool:
        """Whether this module can be disabled via configuration.

        Required modules (e.g., btrfs_snapshots) cannot be disabled.
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
