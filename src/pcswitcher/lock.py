"""File-based locking to prevent concurrent sync operations."""

from __future__ import annotations

import fcntl
import os
import socket
from pathlib import Path

from pcswitcher.executor import RemoteExecutor

__all__ = [
    "SyncLock",
    "acquire_target_lock",
    "get_local_hostname",
]


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
            lock_path: Path to lock file (e.g., ~/.local/share/pc-switcher/sync.lock)
        """
        self._lock_path = lock_path
        self._lock_fd: int | None = None

    def acquire(self, holder_info: str | None = None) -> bool:
        """Acquire exclusive lock non-blocking.

        Args:
            holder_info: Info to write to lock file for diagnostics (e.g., PID, hostname:PID)

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


async def acquire_target_lock(
    executor: RemoteExecutor,
    source_hostname: str,
) -> bool:
    """Acquire lock on target machine via SSH.

    Uses flock command on target machine. Lock is automatically released when
    SSH connection closes (normal exit, crash, or network loss).

    Args:
        executor: Remote executor for running commands on target
        source_hostname: Hostname of source machine (for diagnostics)

    Returns:
        True if lock acquired, False if already held
    """
    lock_path = "~/.local/share/pc-switcher/target.lock"
    holder_info = f"{source_hostname}:{os.getpid()}"

    # Create directory and acquire lock in single command
    # File descriptor 9 is arbitrary, just needs to not conflict with stdio
    # exec 9> opens FD 9 for writing, creating file if needed
    # flock -n 9 tries non-blocking lock on FD 9
    # && echo writes holder info on success
    result = await executor.run_command(
        f"mkdir -p ~/.local/share/pc-switcher && "
        f'exec 9>>"{lock_path}" && '
        f"flock -n 9 && "
        f'truncate -s 0 "{lock_path}" && '
        f'echo "{holder_info}" > "{lock_path}"'
    )
    return result.success


async def get_target_lock_holder(executor: RemoteExecutor) -> str | None:
    """Get info about who holds the target lock.

    Args:
        executor: Remote executor for running commands on target

    Returns:
        Holder info string (e.g., "laptop-work:12345") or None if not held
    """
    result = await executor.run_command("cat ~/.local/share/pc-switcher/target.lock 2>/dev/null")
    if result.success and result.stdout:
        content = result.stdout.strip()
        return content if content else None
    return None


def get_local_hostname() -> str:
    """Get the hostname of the local machine.

    Returns:
        Hostname string
    """
    return socket.gethostname()
