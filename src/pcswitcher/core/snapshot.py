"""Snapshot management interface for orchestrator-level infrastructure.

This defines the interface for snapshot management which is NOT a SyncJob.
Snapshot management is orchestrator-level infrastructure that runs before and after
all SyncJobs, providing safety and rollback capabilities.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.job import RemoteExecutor


class SnapshotCallbacks:
    """Callbacks for snapshot manager to interact with orchestrator."""

    def __init__(self, logger: Any, ui: Any) -> None:
        """Initialize callbacks.

        Args:
            logger: Logger instance for logging
            ui: Terminal UI instance for progress reporting
        """
        self._logger = logger
        self._ui = ui

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """Log a message at the specified level."""
        self._logger.log(level, message, **context)

    def emit_progress(self, percentage: float, current_item: str) -> None:
        """Report progress to UI."""
        self._ui.update_progress(percentage, current_item)


class SnapshotManager(ABC):
    """Abstract base class for snapshot management infrastructure.

    This is NOT a SyncJob. It's orchestrator-level infrastructure that:
    - Validates subvolumes before sync starts
    - Creates pre-sync snapshots before any SyncJob runs
    - Creates post-sync snapshots after all SyncJobs complete
    - Provides rollback capability from pre-sync snapshots
    - Manages snapshot cleanup and retention

    The orchestrator calls methods directly at appropriate lifecycle points.
    """

    def __init__(self, config: dict[str, Any], remote: RemoteExecutor) -> None:
        """Initialize snapshot manager.

        Args:
            config: Configuration for snapshot management (subvolumes, retention, etc.)
            remote: Remote executor interface for target machine operations
        """
        self._config = config
        self._remote = remote
        self._callbacks: SnapshotCallbacks | None = None

    @property
    def config(self) -> dict[str, Any]:
        """Get snapshot configuration."""
        return self._config

    @property
    def remote(self) -> RemoteExecutor:
        """Get remote executor."""
        return self._remote

    def set_callbacks(self, callbacks: SnapshotCallbacks) -> None:
        """Set callbacks for logging and progress reporting.

        Args:
            callbacks: Callbacks instance
        """
        self._callbacks = callbacks

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """Log a message if callbacks are set.

        Args:
            level: Log level
            message: Log message
            **context: Additional context
        """
        if self._callbacks:
            self._callbacks.log(level, message, **context)

    @abstractmethod
    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON Schema for configuration validation.

        Returns:
            JSON Schema dict for validating snapshot configuration
        """
        ...

    @abstractmethod
    def validate_subvolumes(self) -> list[str]:
        """Validate that subvolumes exist on both source and target.

        This is called before any sync operations start.

        Returns:
            List of validation errors (empty if valid)
        """
        ...

    @abstractmethod
    def create_presync_snapshots(self, session_id: str) -> None:
        """Create pre-sync snapshots on both source and target.

        Called by orchestrator after validation, before any SyncJob runs.

        Args:
            session_id: Sync session identifier for snapshot naming

        Raises:
            SyncError: If snapshot creation fails
        """
        ...

    @abstractmethod
    def create_postsync_snapshots(self, session_id: str) -> None:
        """Create post-sync snapshots on both source and target.

        Called by orchestrator after all SyncJobs complete successfully.

        Args:
            session_id: Sync session identifier for snapshot naming

        Raises:
            SyncError: If snapshot creation fails
        """
        ...

    @abstractmethod
    def cleanup_old_snapshots(
        self, keep_recent: int | None = None, older_than_days: int | None = None
    ) -> None:
        """Clean up old snapshots according to retention policy.

        Args:
            keep_recent: Number of recent snapshots to keep (overrides config)
            older_than_days: Delete snapshots older than this (overrides config)
        """
        ...

    @abstractmethod
    def rollback_to_presync(self, session_id: str) -> None:
        """Rollback to pre-sync snapshot state.

        Args:
            session_id: Session ID of the snapshots to rollback to

        Raises:
            SyncError: If rollback fails
        """
        ...
