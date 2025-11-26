"""Session and job execution types for pc-switcher."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class SessionState(StrEnum):
    """States a sync session can be in during its lifecycle."""

    INITIALIZING = "INITIALIZING"
    VALIDATING = "VALIDATING"
    EXECUTING = "EXECUTING"
    CLEANUP = "CLEANUP"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"


class JobResult(StrEnum):
    """Result of a job execution within a sync session."""

    SUCCESS = "SUCCESS"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


def generate_session_id() -> str:
    """Generate a unique session ID.

    Returns:
        8-character hexadecimal session ID
    """
    return uuid.uuid4().hex[:8]


@dataclass
class SyncSession:
    """Represents a single sync operation from source to target.

    Tracks state and progress of sync operation including job results,
    error status, and abort requests.
    """

    id: str
    timestamp: datetime
    source_hostname: str
    target_hostname: str
    enabled_jobs: list[str]
    state: SessionState
    job_results: dict[str, JobResult] = field(default_factory=dict)
    has_errors: bool = False
    abort_requested: bool = False
    lock_path: Path = field(default_factory=lambda: _get_default_lock_path())

    def set_state(self, new_state: SessionState) -> None:
        """Transition to a new session state.

        Args:
            new_state: The state to transition to
        """
        self.state = new_state

    def is_terminal_state(self) -> bool:
        """Check if the session is in a terminal state.

        Terminal states are COMPLETED, ABORTED, or FAILED.

        Returns:
            True if in terminal state, False otherwise
        """
        return self.state in (
            SessionState.COMPLETED,
            SessionState.ABORTED,
            SessionState.FAILED,
        )


def _get_default_lock_path() -> Path:
    """Get the default lock file path.

    Uses $XDG_RUNTIME_DIR/pc-switcher/sync.lock if available,
    otherwise falls back to /tmp/pc-switcher/sync.lock.

    Returns:
        Path to lock file
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    lock_dir = Path(runtime_dir) / "pc-switcher" if runtime_dir else Path("/tmp") / "pc-switcher"
    return lock_dir / "sync.lock"
