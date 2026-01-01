"""Core types and dataclasses for pc-switcher."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

__all__ = [
    "CommandResult",
    "ConfigError",
    "DiskSpaceCriticalError",
    "Host",
    "JobResult",
    "JobStatus",
    "LogLevel",
    "ProgressUpdate",
    "SessionStatus",
    "Snapshot",
    "SnapshotPhase",
    "SyncSession",
    "ValidationError",
]


class Host(StrEnum):
    """Logical role of a machine in the sync operation."""

    SOURCE = "source"
    TARGET = "target"


class LogLevel(IntEnum):
    """Six-level logging hierarchy aligned with stdlib logging levels.

    Values match Python's logging module (10-50 range). Level N includes all
    messages at level N and above. FULL is a custom level between DEBUG and INFO.
    """

    DEBUG = 10  # Most verbose, internal diagnostics
    FULL = 15  # Operational details (file-level), custom level
    INFO = 20  # High-level operations
    WARNING = 30  # Unexpected but non-fatal
    ERROR = 40  # Recoverable errors
    CRITICAL = 50  # Unrecoverable, sync must abort


@dataclass(frozen=True)
class CommandResult:
    """Result of executing a command via Executor."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class ProgressUpdate:
    """Progress information emitted by jobs.

    Rendering Logic:
    - percent set → progress bar with percentage
    - current + total set → "45/100 items"
    - current only → "45 items processed"
    - heartbeat=True → spinner/activity indicator
    """

    percent: int | None = None  # 0-100 if known
    current: int | None = None  # Current item count
    total: int | None = None  # Total items if known
    item: str | None = None  # Current item description
    heartbeat: bool = False  # True for activity indication only

    def __post_init__(self) -> None:
        if self.percent is not None and not 0 <= self.percent <= 100:
            raise ValueError(f"percent must be 0-100, got {self.percent}")


@dataclass(frozen=True)
class ConfigError:
    """Error from Phase 1 (schema) or Phase 2 (job config) validation."""

    job: str | None  # None for global config errors
    path: str  # JSON path to invalid value
    message: str


@dataclass(frozen=True)
class ValidationError:
    """Error from Phase 3 (system state) validation."""

    job: str
    host: Host
    message: str


class DiskSpaceCriticalError(Exception):
    """Exception raised when disk space falls below critical threshold.

    Raised by DiskSpaceMonitorJob during runtime monitoring.
    """

    def __init__(
        self,
        host: Host,
        hostname: str,
        free_space: str,
        threshold: str,
    ) -> None:
        self.host = host
        self.hostname = hostname
        self.free_space = free_space
        self.threshold = threshold
        super().__init__(f"{hostname}: Disk space {free_space} below threshold {threshold}")


class SnapshotPhase(StrEnum):
    """Phase in sync workflow when snapshot is created."""

    PRE = "pre"
    POST = "post"


@dataclass(frozen=True)
class Snapshot:
    """Metadata for a btrfs snapshot.

    Represents a btrfs snapshot created during a sync session. The `name` property
    is computed from the subvolume, phase, and timestamp per FR-010.
    """

    subvolume: str  # e.g., "@home"
    phase: SnapshotPhase  # PRE or POST
    timestamp: datetime  # When the snapshot was created
    session_id: str  # 8-char hex session identifier
    host: Host  # SOURCE or TARGET
    path: str  # Full filesystem path

    @property
    def name(self) -> str:
        """Snapshot name per FR-010: pre-@home-20251129T143022."""
        ts = self.timestamp.strftime("%Y%m%dT%H%M%S")
        return f"{self.phase.value}-{self.subvolume}-{ts}"

    @classmethod
    def from_path(cls, path: str, host: Host) -> Snapshot:
        """Parse a Snapshot from its filesystem path.

        Args:
            path: Full path like "/.snapshots/pc-switcher/20251129T143022-abc12345/pre-@home-20251129T143022"
            host: Which machine this snapshot is on

        Returns:
            Snapshot object with parsed metadata

        Raises:
            ValueError: If the path doesn't match expected format
        """
        # Extract session folder and snapshot name from path
        # Pattern: /.snapshots/pc-switcher/<timestamp>-<session_id>/<phase>-<subvolume>-<timestamp>
        match = re.match(
            r".*/(\d{8}T\d{6})-([a-f0-9]{8})/(\w+)-(@\w*)-(\d{8}T\d{6})$",
            path,
        )
        if not match:
            raise ValueError(f"Cannot parse snapshot path: {path}")

        _folder_ts, session_id, phase_str, subvolume, snap_ts = match.groups()

        # Parse phase
        try:
            phase = SnapshotPhase(phase_str)
        except ValueError as e:
            raise ValueError(f"Invalid phase '{phase_str}' in path: {path}") from e

        # Parse timestamp
        timestamp = datetime.strptime(snap_ts, "%Y%m%dT%H%M%S")

        return cls(
            subvolume=subvolume,
            phase=phase,
            timestamp=timestamp,
            session_id=session_id,
            host=host,
            path=path,
        )


class SessionStatus(StrEnum):
    """Status of a sync session."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class JobStatus(StrEnum):
    """Result status for an individual job execution."""

    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class JobResult:
    """Result of executing a single job."""

    job_name: str
    status: JobStatus
    started_at: datetime  # UTC timezone
    ended_at: datetime  # UTC timezone
    error_message: str | None = None


@dataclass
class SyncSession:
    """Complete sync session state and results."""

    session_id: str
    started_at: datetime  # UTC timezone
    source_hostname: str
    target_hostname: str
    config: dict[str, Any]  # Configuration snapshot
    status: SessionStatus
    ended_at: datetime | None = None  # UTC timezone
    job_results: list[JobResult] | None = None
    error_message: str | None = None
    log_file: str | None = None  # Path to log file
