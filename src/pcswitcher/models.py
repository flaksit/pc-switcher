"""Core types and dataclasses for pc-switcher."""

from __future__ import annotations

from dataclasses import dataclass
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
    """Six-level logging hierarchy with explicit ordering.

    Lower value = more verbose. Level N includes all messages at level N and above.
    """

    DEBUG = 0  # Most verbose, internal diagnostics
    FULL = 1  # Operational details (file-level)
    INFO = 2  # High-level operations
    WARNING = 3  # Unexpected but non-fatal
    ERROR = 4  # Recoverable errors
    CRITICAL = 5  # Unrecoverable, sync must abort


@dataclass(frozen=True)
class CommandResult:
    """Result of executing a command via LocalExecutor or RemoteExecutor."""

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
    """Metadata for a btrfs snapshot."""

    name: str  # e.g., "pre-@home-20251129T143022"
    subvolume: str  # e.g., "@home"
    phase: SnapshotPhase  # PRE or POST
    timestamp: str  # ISO 8601 timestamp


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
    started_at: str  # ISO 8601 timestamp
    ended_at: str  # ISO 8601 timestamp
    error_message: str | None = None


@dataclass
class SyncSession:
    """Complete sync session state and results."""

    session_id: str
    started_at: str  # ISO 8601 timestamp
    source_hostname: str
    target_hostname: str
    config: dict[str, Any]  # Configuration snapshot
    status: SessionStatus
    ended_at: str | None = None  # ISO 8601 timestamp
    job_results: list[JobResult] | None = None
    error_message: str | None = None
    log_file: str | None = None  # Path to log file
