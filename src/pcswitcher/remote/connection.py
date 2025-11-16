"""Remote execution implementation for pc-switcher.

This module provides SSH-based remote execution capabilities using Fabric library.
It implements the RemoteExecutor interface that modules use to communicate with
the target machine.

Key features:
- Connection persistence via SSH ControlMaster
- Automatic reconnection on connection loss
- Command execution with optional sudo
- File transfer from source to target
- Process termination for cleanup operations

The connection architecture uses a layered approach:
- TargetConnection: Low-level SSH operations via Fabric
- SSHRemoteExecutor: Implements RemoteExecutor interface for modules
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from fabric import Connection
from invoke.exceptions import UnexpectedExit

from pcswitcher.core.module import RemoteExecutor, SyncError

if TYPE_CHECKING:
    pass


class ConnectionError(Exception):
    """Raised when connection to target fails or is lost during operation."""


class TargetConnection:
    """Manages persistent SSH connection to target machine using Fabric.

    Uses SSH ControlMaster for connection persistence, which allows multiple
    SSH operations to share a single connection. This reduces connection overhead
    and latency for repeated operations.

    Provides methods for:
    - Command execution (with optional sudo)
    - File transfer (SFTP-based)
    - Process management on target

    Thread Safety: Not thread-safe. Each sync operation should use its own connection.
    """

    def __init__(self, host: str, user: str = "root", port: int = 22) -> None:
        """Initialize target connection.

        Args:
            host: Target hostname or IP address
            user: SSH user (default: root)
            port: SSH port (default: 22)
        """
        self._host = host
        self._user = user
        self._port = port
        self._connection: Connection | None = None

    def connect(self) -> None:
        """Establish connection to target machine.

        Configures Fabric connection with SSH agent forwarding and automatic
        key discovery. ControlMaster settings should be configured in
        ~/.ssh/config for optimal persistence.

        Raises:
            ConnectionError: If connection fails. Common causes:
                - Target machine not reachable (network issue)
                - SSH service not running on target
                - SSH key not authorized on target
                - Incorrect hostname or IP address
        """
        try:
            # Configure connection using SSH agent and local keys
            # ControlMaster (connection multiplexing) is configured via ~/.ssh/config
            connect_kwargs = {
                "allow_agent": True,  # Use SSH agent for key management
                "look_for_keys": True,  # Automatically find keys in ~/.ssh/
            }

            self._connection = Connection(
                host=self._host,
                user=self._user,
                port=self._port,
                connect_kwargs=connect_kwargs,
            )

            # Test connection with simple echo command to verify SSH is working
            result = self._connection.run("echo test", hide=True, warn=True)
            if not result.ok:
                raise ConnectionError(
                    "Connection test failed. SSH connection established but command execution failed. "
                    "Check target machine's shell configuration."
                )

        except Exception as e:
            raise ConnectionError(
                f"Failed to connect to {self._user}@{self._host}:{self._port}. "
                f"Verify: (1) target is reachable (ping), (2) SSH service running, "
                f"(3) SSH key authorized on target. Error: {e}"
            ) from e

    def disconnect(self) -> None:
        """Close connection to target machine.

        Performs graceful closure of the SSH connection.
        """
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                # Best-effort cleanup
                pass
            finally:
                self._connection = None

    def _ensure_connected(self) -> Connection:
        """Ensure connection is established.

        Returns:
            Active connection instance

        Raises:
            ConnectionError: If not connected
        """
        if self._connection is None:
            raise ConnectionError("Not connected to target. Call connect() first.")
        return self._connection

    def run(
        self,
        command: str,
        sudo: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a command on the remote machine.

        The command is executed via SSH. Output is captured (not streamed) and
        returned in a CompletedProcess object for easy inspection.

        On connection failure, one automatic reconnect attempt is made before
        raising ConnectionError. This handles transient network issues.

        Args:
            command: Shell command to execute (passed to remote shell)
            sudo: Whether to run with sudo privileges (requires passwordless sudo)
            timeout: Optional timeout in seconds (None = no timeout)

        Returns:
            CompletedProcess with stdout, stderr, and returncode. Non-zero
            returncode indicates command failure but NOT connection failure.

        Raises:
            ConnectionError: If connection is lost and reconnect fails.
                            This is a network/SSH issue, not a command issue.
        """
        conn = self._ensure_connected()

        try:
            # Execute command with output capture
            # hide=True suppresses local output, warn=True prevents exception on non-zero exit
            if sudo:
                result = conn.sudo(command, hide=True, warn=True, timeout=timeout)
            else:
                result = conn.run(command, hide=True, warn=True, timeout=timeout)

            # Convert Fabric Result to standard library CompletedProcess for consistency
            return subprocess.CompletedProcess(
                args=command,
                returncode=result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        except UnexpectedExit as e:
            # UnexpectedExit means command ran but had non-zero exit
            # This is normal operation, not an error condition
            return subprocess.CompletedProcess(
                args=command,
                returncode=e.result.return_code,
                stdout=e.result.stdout,
                stderr=e.result.stderr,
            )
        except Exception as e:
            # Connection or timeout error - attempt automatic reconnection once
            try:
                self.disconnect()
                self.connect()
                conn = self._ensure_connected()
                # Retry the command after reconnection
                if sudo:
                    result = conn.sudo(command, hide=True, warn=True, timeout=timeout)
                else:
                    result = conn.run(command, hide=True, warn=True, timeout=timeout)

                return subprocess.CompletedProcess(
                    args=command,
                    returncode=result.return_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
            except Exception as reconnect_error:
                raise ConnectionError(
                    f"Lost connection to target during command execution and automatic reconnect failed. "
                    f"Original error: {e}. Reconnect error: {reconnect_error}"
                ) from e

    def send_file_to_target(self, local: Path, remote: Path) -> None:
        """Transfer a file from local machine to remote target.

        Args:
            local: Path to local file
            remote: Path on remote machine

        Raises:
            ConnectionError: If connection is lost
            SyncError: If file transfer fails
        """
        if not local.exists():
            raise SyncError(f"Local file does not exist: {local}")

        conn = self._ensure_connected()

        try:
            conn.put(str(local), str(remote))
        except Exception as e:
            # Try to reconnect once
            try:
                self.disconnect()
                self.connect()
                conn = self._ensure_connected()
                conn.put(str(local), str(remote))
            except Exception as reconnect_error:
                raise ConnectionError(
                    f"Lost connection during file transfer and reconnect failed: {reconnect_error}"
                ) from e

    def terminate_processes(self, process_names: list[str] | None = None) -> None:
        """Terminate pc-switcher processes on target machine.

        Used during abort to clean up running processes.

        Args:
            process_names: Optional list of process names to terminate.
                          If None, terminates all pc-switcher processes.
        """
        if process_names is None:
            process_names = ["pc-switcher", "pcswitcher"]

        for process_name in process_names:
            with contextlib.suppress(Exception):
                # Best-effort cleanup - ignore failures
                self.run(f"pkill -9 {process_name}", sudo=True, timeout=5.0)


class SSHRemoteExecutor(RemoteExecutor):
    """Implementation of RemoteExecutor using TargetConnection.

    Wraps TargetConnection and provides the RemoteExecutor interface
    for modules to use.
    """

    def __init__(self, connection: TargetConnection) -> None:
        """Initialize SSH remote executor.

        Args:
            connection: TargetConnection instance
        """
        self._connection = connection

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
        try:
            return self._connection.run(command, sudo=sudo, timeout=timeout)
        except ConnectionError as e:
            raise SyncError(f"Connection error: {e}") from e

    def send_file_to_target(self, local: Path, remote: Path) -> None:
        """Transfer a file from local machine to remote target.

        Args:
            local: Path to local file
            remote: Path on remote machine

        Raises:
            SyncError: If file transfer fails
        """
        try:
            self._connection.send_file_to_target(local, remote)
        except ConnectionError as e:
            raise SyncError(f"Connection error during file transfer: {e}") from e

    def get_hostname(self) -> str:
        """Get the hostname of the remote machine.

        Returns:
            Hostname as string

        Raises:
            SyncError: If hostname cannot be determined
        """
        result = self.run("hostname", timeout=5.0)
        if result.returncode != 0:
            raise SyncError(f"Failed to get hostname: {result.stderr}")
        return result.stdout.strip()
