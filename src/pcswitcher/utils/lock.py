"""Lock file management for preventing concurrent sync operations."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class LockInfo:
    """Information stored in a lock file."""

    pid: int
    timestamp: str
    session_id: str


class LockError(Exception):
    """Raised when lock acquisition or management fails."""


class LockManager:
    """Manages lock files to prevent concurrent sync operations.

    Lock files are stored in $XDG_RUNTIME_DIR/pc-switcher/ (falling back to
    /tmp/pc-switcher if XDG_RUNTIME_DIR is not set). Lock files contain PID,
    timestamp, and session ID.

    Includes stale lock detection using PID validation.
    """

    def __init__(self, session_id: str) -> None:
        """Initialize lock manager.

        Args:
            session_id: Current session ID
        """
        self._session_id = session_id
        self._lock_path = self._get_lock_path()
        self._acquired = False

    def _get_lock_path(self) -> Path:
        """Get the lock file path.

        Uses $XDG_RUNTIME_DIR/pc-switcher/sync.lock if XDG_RUNTIME_DIR is set,
        otherwise falls back to /tmp/pc-switcher/sync.lock.

        Returns:
            Path to lock file
        """
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        lock_dir = Path(runtime_dir) / "pc-switcher" if runtime_dir else Path("/tmp") / "pc-switcher"

        lock_dir.mkdir(parents=True, exist_ok=True)
        return lock_dir / "sync.lock"

    def check_lock_exists(self) -> bool:
        """Check if a lock file exists.

        Returns:
            True if lock file exists, False otherwise
        """
        return self._lock_path.exists()

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with given PID is running.

        Args:
            pid: Process ID to check

        Returns:
            True if process is running, False otherwise
        """
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _read_lock_info(self) -> LockInfo | None:
        """Read lock information from the lock file.

        Returns:
            LockInfo if file exists and is valid, None otherwise
        """
        if not self._lock_path.exists():
            return None

        try:
            with open(self._lock_path) as f:
                data = json.load(f)
            return LockInfo(
                pid=data["pid"],
                timestamp=data["timestamp"],
                session_id=data["session_id"],
            )
        except Exception:
            return None

    def _is_stale_lock(self, lock_info: LockInfo) -> bool:
        """Check if a lock is stale (process no longer running).

        Args:
            lock_info: Lock information to check

        Returns:
            True if lock is stale, False otherwise
        """
        return not self._is_process_running(lock_info.pid)

    def acquire_lock(self, interactive: bool = True) -> None:
        """Acquire the sync lock.

        If a lock exists, validates the PID. If the process is not running,
        treats the lock as stale. If interactive=True, prompts the user to
        remove stale locks. Otherwise, removes stale locks automatically.

        Args:
            interactive: Whether to prompt user for stale lock removal

        Raises:
            LockError: If lock is held by another running process or user
                      declines to remove stale lock
        """
        if self._acquired:
            return

        # Check if lock exists
        lock_info = self._read_lock_info()
        if lock_info is not None:
            # Validate PID
            if self._is_stale_lock(lock_info):
                # Stale lock detected
                if interactive:
                    print("\nStale lock file detected:")
                    print(f"  PID: {lock_info.pid} (not running)")
                    print(f"  Session ID: {lock_info.session_id}")
                    print(f"  Timestamp: {lock_info.timestamp}")
                    response = input("\nRemove stale lock and continue? [y/N]: ")
                    if response.lower() != "y":
                        raise LockError("User declined to remove stale lock")

                # Remove stale lock
                try:
                    self._lock_path.unlink()
                except Exception as e:
                    raise LockError(f"Failed to remove stale lock: {e}") from e
            else:
                # Lock is held by running process
                raise LockError(
                    f"Another sync operation is already running "
                    f"(PID: {lock_info.pid}, Session: {lock_info.session_id})"
                )

        # Create new lock
        lock_data = {
            "pid": os.getpid(),
            "timestamp": datetime.now().isoformat(),
            "session_id": self._session_id,
        }

        try:
            with open(self._lock_path, "w") as f:
                json.dump(lock_data, f, indent=2)
            self._acquired = True
        except Exception as e:
            raise LockError(f"Failed to create lock file: {e}") from e

    def release_lock(self) -> None:
        """Release the sync lock.

        Removes the lock file if it was acquired by this instance.
        Does not raise exceptions on failure (best-effort cleanup).
        """
        if not self._acquired:
            return

        try:
            if self._lock_path.exists():
                # Verify it's our lock before removing
                lock_info = self._read_lock_info()
                if lock_info is not None and lock_info.pid == os.getpid():
                    self._lock_path.unlink()
            self._acquired = False
        except Exception:
            # Best-effort cleanup - don't raise
            pass

    def __enter__(self) -> LockManager:
        """Context manager entry."""
        self.acquire_lock()
        return self

    def __exit__(self, exc_type: type, exc_val: Exception, exc_tb: object) -> None:
        """Context manager exit."""
        self.release_lock()
