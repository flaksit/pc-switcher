"""Command execution for local and remote machines."""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

import asyncssh

from pcswitcher.models import CommandResult

__all__ = [
    "BashLoginRemoteExecutor",
    "Executor",
    "LocalExecutor",
    "LocalProcess",
    "Process",
    "RemoteExecutor",
    "RemoteProcess",
]


class Executor(Protocol):
    """Protocol for command execution on local or remote machines.

    Both LocalExecutor and RemoteExecutor implement this protocol,
    allowing code to work with either without knowing which one it is.
    """

    async def run_command(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command and wait for completion."""
        ...

    async def terminate_all_processes(self) -> None:
        """Terminate all tracked processes."""
        ...


class Process(Protocol):
    """Handle for a running process with streaming output.

    Note: stdin is intentionally not supported. All commands must be
    non-interactive. This is a design constraint to ensure reliable
    automated execution without prompts or user input requirements.
    """

    async def stdout(self) -> AsyncIterator[str]:
        """Iterate over stdout lines as they arrive."""
        ...

    async def stderr(self) -> AsyncIterator[str]:
        """Iterate over stderr lines as they arrive."""
        ...

    async def wait(self) -> CommandResult:
        """Wait for process to complete and return result."""
        ...

    async def terminate(self) -> None:
        """Terminate the process."""
        ...


class LocalProcess:
    """Process wrapper for local asyncio subprocess."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc

    async def stdout(self) -> AsyncIterator[str]:
        """Iterate over stdout lines as they arrive."""
        if self._proc.stdout is None:
            return
        async for line in self._proc.stdout:
            yield line.decode()

    async def stderr(self) -> AsyncIterator[str]:
        """Iterate over stderr lines as they arrive."""
        if self._proc.stderr is None:
            return
        async for line in self._proc.stderr:
            yield line.decode()

    async def wait(self) -> CommandResult:
        """Wait for process to complete and return result."""
        stdout_bytes, stderr_bytes = await self._proc.communicate()
        return CommandResult(
            exit_code=self._proc.returncode or 0,
            stdout=stdout_bytes.decode() if stdout_bytes else "",
            stderr=stderr_bytes.decode() if stderr_bytes else "",
        )

    async def terminate(self) -> None:
        """Terminate the process."""
        self._proc.terminate()
        await self._proc.wait()


class LocalExecutor:
    """Executes commands on the source machine via async subprocess."""

    def __init__(self) -> None:
        self._processes: list[asyncio.subprocess.Process] = []

    async def run_command(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command and wait for completion.

        Args:
            cmd: Shell command to execute
            timeout: Optional timeout in seconds

        Returns:
            CommandResult with exit code, stdout, and stderr
        """
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
            return CommandResult(
                exit_code=proc.returncode or 0,
                stdout=stdout.decode() if stdout else "",
                stderr=stderr.decode() if stderr else "",
            )
        except TimeoutError:
            proc.terminate()
            await proc.wait()
            raise

    async def start_process(self, cmd: str) -> LocalProcess:
        """Start a long-running process with streaming output.

        Args:
            cmd: Shell command to execute

        Returns:
            LocalProcess wrapper for the subprocess
        """
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes.append(proc)
        return LocalProcess(proc)

    async def terminate_all_processes(self) -> None:
        """Terminate all tracked processes."""
        for proc in self._processes:
            if proc.returncode is None:  # Still running
                proc.terminate()
        # Wait for all to finish
        await asyncio.gather(
            *(proc.wait() for proc in self._processes if proc.returncode is None),
            return_exceptions=True,
        )
        self._processes.clear()


class RemoteProcess:
    """Process wrapper for SSH remote process."""

    def __init__(self, proc: asyncssh.SSHClientProcess[str]) -> None:
        self._proc = proc

    async def stdout(self) -> AsyncIterator[str]:
        """Iterate over stdout lines as they arrive."""
        async for line in self._proc.stdout:
            yield line

    async def stderr(self) -> AsyncIterator[str]:
        """Iterate over stderr lines as they arrive."""
        async for line in self._proc.stderr:
            yield line

    async def wait(self) -> CommandResult:
        """Wait for process to complete and return result."""
        await self._proc.wait()
        # Read remaining output after process completes
        stdout_data = await self._proc.stdout.read()
        stderr_data = await self._proc.stderr.read()
        return CommandResult(
            exit_code=self._proc.exit_status or 0,
            stdout=stdout_data,
            stderr=stderr_data,
        )

    async def terminate(self) -> None:
        """Terminate the process."""
        self._proc.terminate()
        await self._proc.wait()


class RemoteExecutor:
    """Executes commands on target machine via SSH connection."""

    def __init__(self, conn: asyncssh.SSHClientConnection) -> None:
        self._conn = conn
        self._processes: list[asyncssh.SSHClientProcess[str]] = []
        self._default_login_shell = False

    def _wrap_for_login_shell(self, cmd: str) -> str:
        """Wrap command for execution in bash login shell.

        Remote SSH commands run in non-login shells by default, which means
        ~/.profile isn't sourced and PATH may not include ~/.local/bin.

        This wrapper ensures commands run with full user environment by
        wrapping them in 'bash -l -c "..."', which:
        - Sources /etc/profile and ~/.profile (login shell behavior)
        - Ensures PATH includes user-installed tools (uv, pc-switcher)
        - Simulates interactive SSH session environment

        Args:
            cmd: Original shell command

        Returns:
            Wrapped command with bash -l -c prefix and proper quoting
        """
        return f"bash -l -c {shlex.quote(cmd)}"

    async def run_command(
        self,
        cmd: str,
        timeout: float | None = None,
        login_shell: bool | None = None,
    ) -> CommandResult:
        """Run a command on remote machine and wait for completion.

        Args:
            cmd: Shell command to execute
            timeout: Optional timeout in seconds
            login_shell: If True, wrap command in 'bash -l -c' to source ~/.profile
                and ensure proper PATH. If None, uses the executor's default.
                Useful for commands requiring user-installed tools (e.g., uv, pc-switcher).

        Returns:
            CommandResult with exit code, stdout, and stderr

        Note:
            Using login_shell=True adds overhead (typically 10-50ms per command)
            due to profile sourcing. Only use when environment setup is needed.
        """
        use_login_shell = login_shell if login_shell is not None else self._default_login_shell
        if use_login_shell:
            cmd = self._wrap_for_login_shell(cmd)

        try:
            result = await asyncio.wait_for(
                self._conn.run(cmd),
                timeout=timeout,
            )
            return CommandResult(
                exit_code=result.exit_status or 0,
                stdout=str(result.stdout) if result.stdout else "",
                stderr=str(result.stderr) if result.stderr else "",
            )
        except TimeoutError:
            raise

    async def start_process(self, cmd: str, login_shell: bool | None = None) -> RemoteProcess:
        """Start a long-running process on remote machine.

        Args:
            cmd: Shell command to execute
            login_shell: If True, wrap command in 'bash -l -c' to source ~/.profile
                and ensure proper PATH. If None, uses the executor's default.
                Useful for background processes requiring user-installed tools.

        Returns:
            RemoteProcess wrapper for the SSH process

        Note:
            Using login_shell=True adds startup overhead. Only use when
            environment setup is needed for the background process.
        """
        use_login_shell = login_shell if login_shell is not None else self._default_login_shell
        if use_login_shell:
            cmd = self._wrap_for_login_shell(cmd)

        process = await self._conn.create_process(cmd)
        self._processes.append(process)
        return RemoteProcess(process)

    async def terminate_all_processes(self) -> None:
        """Terminate all tracked remote processes."""
        for process in self._processes:
            process.terminate()
        # Wait for all to finish
        await asyncio.gather(
            *(proc.wait() for proc in self._processes),
            return_exceptions=True,
        )
        self._processes.clear()

    async def send_file(self, local: Path, remote: str) -> None:
        """Copy a file from local machine to remote.

        Args:
            local: Local file path
            remote: Remote destination path
        """
        async with self._conn.start_sftp_client() as sftp:
            await sftp.put(str(local), remote)

    async def get_file(self, remote: str, local: Path) -> None:
        """Copy a file from remote machine to local.

        Args:
            remote: Remote file path
            local: Local destination path
        """
        async with self._conn.start_sftp_client() as sftp:
            await sftp.get(remote, str(local))


class BashLoginRemoteExecutor(RemoteExecutor):
    """RemoteExecutor that runs all commands in bash login shell by default.

    This executor wraps all commands in 'bash -l -c "..."' to ensure:
    - ~/.profile is sourced
    - PATH includes ~/.local/bin (for user-installed tools like uv, pc-switcher)
    - Environment matches interactive SSH sessions

    Primarily used in integration tests where all commands interact with
    user-installed tools. Production code should use base RemoteExecutor
    and explicitly pass login_shell=True only when needed.

    The login_shell parameter can still be overridden per-call:
        executor.run_command("sudo systemctl status", login_shell=False)
    """

    def __init__(self, conn: asyncssh.SSHClientConnection) -> None:
        """Initialize with login shell enabled by default."""
        super().__init__(conn)
        self._default_login_shell = True
