"""Remote execution implementation for pc-switcher."""

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
    """Raised when connection to target fails."""


class TargetConnection:
    """Manages persistent SSH connection to target machine using Fabric.

    Uses ControlMaster for connection persistence and provides methods for
    running commands, transferring files, and managing pc-switcher installation.
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

        Configures ControlMaster for persistent connection.

        Raises:
            ConnectionError: If connection fails
        """
        try:
            # Configure connection with ControlMaster
            # Note: ControlMaster configuration should be set in ~/.ssh/config for persistence
            connect_kwargs = {
                "allow_agent": True,
                "look_for_keys": True,
            }

            self._connection = Connection(
                host=self._host,
                user=self._user,
                port=self._port,
                connect_kwargs=connect_kwargs,
            )

            # Test connection
            result = self._connection.run("echo test", hide=True, warn=True)
            if not result.ok:
                raise ConnectionError("Connection test failed")

        except Exception as e:
            raise ConnectionError(f"Failed to connect to {self._user}@{self._host}: {e}") from e

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

        Args:
            command: Shell command to execute
            sudo: Whether to run with sudo privileges
            timeout: Optional timeout in seconds

        Returns:
            CompletedProcess-like object with stdout, stderr, and returncode

        Raises:
            ConnectionError: If connection is lost
            SyncError: If command execution fails
        """
        conn = self._ensure_connected()

        try:
            # Use Fabric's run or sudo
            if sudo:
                result = conn.sudo(command, hide=True, warn=True, timeout=timeout)
            else:
                result = conn.run(command, hide=True, warn=True, timeout=timeout)

            # Convert Fabric Result to CompletedProcess-like object
            return subprocess.CompletedProcess(
                args=command,
                returncode=result.return_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        except UnexpectedExit as e:
            # Command failed, but that's okay - return result
            return subprocess.CompletedProcess(
                args=command,
                returncode=e.result.return_code,
                stdout=e.result.stdout,
                stderr=e.result.stderr,
            )
        except Exception as e:
            # Try to reconnect once
            try:
                self.disconnect()
                self.connect()
                # Retry command
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
                    f"Lost connection to target and reconnect failed: {reconnect_error}"
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

    def check_version(self) -> str | None:
        """Check pc-switcher version on target machine.

        Returns:
            Version string or None if not installed

        Raises:
            ConnectionError: If connection is lost
        """
        result = self.run("pip show pcswitcher", timeout=10.0)

        if result.returncode != 0:
            return None

        # Parse version from output
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
        return None

    def install_version(self, version: str) -> None:
        """Install specific version of pc-switcher on target.

        Args:
            version: Version to install

        Raises:
            ConnectionError: If connection is lost
            SyncError: If installation fails
        """
        command = f"uv tool install pcswitcher=={version}"
        result = self.run(command, sudo=True, timeout=300.0)

        if result.returncode != 0:
            raise SyncError(f"Installation failed: {result.stderr or result.stdout}")


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
