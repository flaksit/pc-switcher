"""SSH connection management for target machine communication."""

from __future__ import annotations

import asyncio

import asyncssh

from pcswitcher.events import ConnectionEvent, EventBus

__all__ = ["Connection"]


class Connection:
    """Manages SSH connection to target machine with multiplexing support.

    Uses asyncssh with keepalive for connection health monitoring and
    a semaphore for session multiplexing to prevent overwhelming the SSH server.
    """

    def __init__(
        self,
        target: str,
        event_bus: EventBus,
        max_sessions: int = 10,
        keepalive_interval: int = 15,
        keepalive_count_max: int = 3,
    ) -> None:
        """Initialize connection parameters.

        Args:
            target: Hostname or SSH config alias for target machine
            event_bus: EventBus for publishing connection events
            max_sessions: Maximum concurrent SSH sessions (default 10)
            keepalive_interval: Seconds between keepalive packets (default 15)
            keepalive_count_max: Max missed keepalives before disconnect (default 3)
        """
        self._target = target
        self._event_bus = event_bus
        self._conn: asyncssh.SSHClientConnection | None = None
        self._session_semaphore = asyncio.Semaphore(max_sessions)
        self._keepalive_interval = keepalive_interval
        self._keepalive_count_max = keepalive_count_max

    @property
    def connected(self) -> bool:
        """Check if connection is established."""
        return self._conn is not None

    @property
    def ssh_connection(self) -> asyncssh.SSHClientConnection:
        """Get the underlying SSH connection.

        Returns:
            The asyncssh SSHClientConnection object

        Raises:
            RuntimeError: If not connected
        """
        if self._conn is None:
            raise RuntimeError("Not connected to target")
        return self._conn

    async def connect(self) -> None:
        """Establish SSH connection to target.

        Respects ~/.ssh/config automatically via asyncssh.
        Connection will use keepalive to detect failures proactively.
        """
        self._conn = await asyncssh.connect(
            self._target,
            keepalive_interval=self._keepalive_interval,
            keepalive_count_max=self._keepalive_count_max,
        )
        # Publish connected event with initial latency (0 for now, or could measure)
        self._event_bus.publish(ConnectionEvent(status="connected", latency=0.0))

    async def disconnect(self) -> None:
        """Close the SSH connection gracefully."""
        if self._conn:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None
            self._event_bus.publish(ConnectionEvent(status="disconnected", latency=None))

    async def create_process(self, cmd: str) -> asyncssh.SSHClientProcess[str]:
        """Create a remote process for command execution.

        Uses semaphore to limit concurrent sessions. The returned process
        can be used for streaming output.

        Args:
            cmd: Shell command to execute on remote

        Returns:
            SSHClientProcess for the running command

        Raises:
            RuntimeError: If not connected
        """
        if self._conn is None:
            raise RuntimeError("Not connected to target")
        async with self._session_semaphore:
            return await self._conn.create_process(cmd)

    async def run(self, cmd: str) -> asyncssh.SSHCompletedProcess:
        """Run a command and wait for completion.

        Args:
            cmd: Shell command to execute

        Returns:
            SSHCompletedProcess with exit status, stdout, stderr

        Raises:
            RuntimeError: If not connected
        """
        if self._conn is None:
            raise RuntimeError("Not connected to target")
        async with self._session_semaphore:
            return await self._conn.run(cmd)

    async def start_sftp_client(self) -> asyncssh.SFTPClient:
        """Start SFTP client for file transfers.

        Returns:
            SFTPClient for file operations

        Raises:
            RuntimeError: If not connected
        """
        if self._conn is None:
            raise RuntimeError("Not connected to target")
        return await self._conn.start_sftp_client()

    async def kill_all_remote_processes(self, pattern: str = "pc-switcher") -> None:
        """Kill all processes matching pattern on remote machine.

        Used for cleanup during interrupt handling.

        Args:
            pattern: Process name pattern to match (default "pc-switcher")
        """
        if self._conn is None:
            return
        # Use pkill with pattern matching, ignore if no processes found
        await self._conn.run(f"pkill -f '{pattern}' || true")
