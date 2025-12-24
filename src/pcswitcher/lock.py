"""File-based locking to prevent concurrent sync operations.

Uses a single unified lock file (~/.local/share/pc-switcher/pc-switcher.lock)
to ensure a machine can only participate in one sync at a time, either as
source or target. This prevents:
- A machine from being targeted while it's acting as source
- A machine from acting as source while it's a target
- Self-sync (A→A)
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import socket
from pathlib import Path

from pcswitcher.executor import RemoteExecutor, RemoteProcess

__all__ = [
    "LOCK_FILE_NAME",
    "SyncLock",
    "get_local_hostname",
    "get_lock_path",
    "release_remote_lock",
    "start_persistent_remote_lock",
]

# Single unified lock file name used for both source and target roles
LOCK_FILE_NAME = "pc-switcher.lock"


def get_lock_path() -> Path:
    """Get the path to the unified lock file.

    Returns:
        Path to ~/.local/share/pc-switcher/pc-switcher.lock
    """
    return Path.home() / ".local/share/pc-switcher" / LOCK_FILE_NAME


class SyncLock:
    """File-based lock using fcntl to prevent concurrent sync executions.

    Uses fcntl.flock() for atomic lock acquisition with automatic release on
    process exit (normal, crash, or kill). This prevents race conditions and
    stale locks that would occur with file existence checking.

    The lock file contains holder information (PID/hostname) for diagnostic
    error messages but the actual lock is the fcntl lock, not file existence.
    """

    def __init__(self, lock_path: Path) -> None:
        """Initialize lock with path.

        Args:
            lock_path: Path to lock file (e.g., ~/.local/share/pc-switcher/pc-switcher.lock)
        """
        self._lock_path = lock_path
        self._lock_fd: int | None = None

    def acquire(self, holder_info: str | None = None) -> bool:
        """Acquire exclusive lock non-blocking.

        Args:
            holder_info: Info to write to lock file for diagnostics (e.g., "source:hostname:session_id")

        Returns:
            True if lock acquired, False if already held by another process
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write holder info for diagnostics
            info = holder_info or str(os.getpid())
            os.ftruncate(self._lock_fd, 0)
            os.write(self._lock_fd, info.encode())
            return True
        except BlockingIOError:
            # Lock is held by another process
            os.close(self._lock_fd)
            self._lock_fd = None
            return False

    def get_holder_info(self) -> str | None:
        """Read info about process holding the lock.

        Returns:
            Holder info string or None if lock file doesn't exist or is empty
        """
        try:
            content = self._lock_path.read_text().strip()
            return content if content else None
        except FileNotFoundError:
            return None

    def release(self) -> None:
        """Release the lock.

        Safe to call multiple times. Does nothing if lock not held.
        Note: Lock is automatically released when process exits.
        """
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None


async def start_persistent_remote_lock(
    executor: RemoteExecutor,
    source_hostname: str,
    session_id: str,
) -> RemoteProcess | None:
    """Start a persistent lock on remote machine via SSH.

    Uses the same lock file as the local source lock, ensuring a machine
    can only participate in one sync at a time (as source or target).

    Starts a long-running background process that holds the flock for the
    entire sync session. The lock is released when the process is terminated
    (via release_remote_lock()) or when the SSH connection closes.

    Args:
        executor: Remote executor for running commands on target
        source_hostname: Hostname of source machine (for diagnostics)
        session_id: Session ID (for diagnostics)

    Returns:
        RemoteProcess object holding the lock, or None if lock is already held
    """
    lock_path = f"$HOME/.local/share/pc-switcher/{LOCK_FILE_NAME}"
    holder_info = f"target:{source_hostname}:{session_id}"

    # First create directory and write holder info to lock file
    # Note: Use $HOME instead of ~ because ~ doesn't expand inside double quotes
    setup_result = await executor.run_command(
        f'mkdir -p "$HOME/.local/share/pc-switcher" && echo "{holder_info}" > "{lock_path}"'
    )
    if not setup_result.success:
        return None

    # Start persistent process that holds the lock
    # flock -n for non-blocking lock attempt
    # -c "read" waits indefinitely by reading from stdin (which will never provide input)
    # This keeps the process alive and holding the lock until terminated
    cmd = f'flock -n "{lock_path}" -c "read"'

    try:
        process = await executor.start_process(cmd)
        # Give flock a moment to acquire the lock or fail
        await asyncio.sleep(0.1)
        return process
    except Exception:
        return None


async def release_remote_lock(process: RemoteProcess) -> None:
    """Release the persistent remote lock.

    Terminates the lock-holding process, which releases the flock.

    Args:
        process: RemoteProcess object returned by start_persistent_remote_lock()
    """
    await process.terminate()


def get_local_hostname() -> str:
    """Get the hostname of the local machine.

    Returns:
        Hostname string
    """
    return socket.gethostname()
