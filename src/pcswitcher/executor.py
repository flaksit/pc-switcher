"""Command execution for local and remote machines.

Every operation that reaches either machine goes through an `Executor`, which makes this
module the one place two cross-cutting concerns can be implemented once rather than at
every call site:

- **Debug trace (#210, `PKG-FR-LOG-VERBATIM`).** Every command, file transfer and background
  process is logged verbatim at `DEBUG` before it runs — the literal string handed to the
  shell, or the two paths of a transfer — and every command's own stdout and stderr are
  logged verbatim after it. Reads included: a trace that omits them cannot answer "what did
  the tool actually do".
- **Credential privacy (`PKG-FR-CREDENTIAL-PRIVACY`).** A URL's embedded credential is
  withheld from the confirmation prompt here, and from every log line by
  `logger.CredentialRedactionFilter`.
- **Per-action confirmation (`--confirm-each-command`).** Every operation that is not
  purely read-only is gated: the user sees the same verbatim operation and must proceed or
  abort. A call may omit `mutates=` only when it cannot change ANY state on the machine —
  no file content, no process state, no lock or other advisory state, no package-manager
  database, no credential cache. "It changes no file content" is not on its own grounds to
  leave a call ungated: taking a lock, starting a background process and priming a
  credential cache all change the machine without writing a byte of anyone's data.

`mutates` is therefore both the gate trigger and the human phrase describing the intent
("install firefox"). Callers keep one method for reads and writes — the kwarg is the only
difference — so nothing about a mutating call site is structurally special beyond saying
so. The flip side is that a forgotten `mutates=` is an unannounced change; that is the
invariant to preserve when adding anything that is not a pure read.

The job a command belongs to comes from the `active_job` context variable rather than a
constructor argument, because executors are created once per run and shared by every job.
`asyncio` tasks copy the context at creation, so a background job (`disk_space_monitor`)
running concurrently with a sync job cannot see or clobber the other's label.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import shlex
from collections.abc import AsyncGenerator, AsyncIterator, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar, Protocol

import asyncssh

from pcswitcher.models import CommandResult, Host
from pcswitcher.redaction import redact_credentials
from pcswitcher.step_gate import StepGate

__all__ = [
    "BashLoginRemoteExecutor",
    "Executor",
    "LocalExecutor",
    "LocalProcess",
    "Process",
    "RemoteExecutor",
    "RemoteProcess",
    "active_job",
]

_logger = logging.getLogger("pcswitcher.executor")

# The job currently issuing commands, used to tag the debug trace and the confirmation
# prompt. Defaults to "orchestrator" so the pre-job and teardown phases (locks, config
# sync, snapshots) are labelled correctly without any caller doing anything.
_active_job: contextvars.ContextVar[str] = contextvars.ContextVar("pcswitcher_active_job", default="orchestrator")


@contextmanager
def active_job(name: str) -> Generator[None]:
    """Label every executor operation issued inside this block as belonging to `name`.

    Set around a job's `execute()` by the orchestrator. Restores the previous label on exit.

    Parallel-safe by construction, which is why this is a `ContextVar` rather than an
    attribute on the executor: executors are created once and shared by every job, so a
    mutable `executor.current_job` would be clobbered the moment two jobs ran at once. An
    `asyncio` task inherits a COPY of the context, so a label set inside one task is
    invisible to every other.

    The rule a future parallel job loop must keep: establish the label per JOB TASK, either
    inside the coroutine the task runs or at `create_task()` time (which snapshots the
    context). Wrapping a `gather()` of several DIFFERENT jobs in one `active_job` block
    would stamp them all with the same label — the only way to get this wrong.
    """
    token = _active_job.set(name)
    try:
        yield
    finally:
        _active_job.reset(token)


class Executor(Protocol):
    """Protocol for command execution on local or remote machines.

    Both LocalExecutor and RemoteExecutor implement this protocol,
    allowing code to work with either without knowing which one it is.
    """

    host: ClassVar[Host]

    async def run_command(
        self,
        cmd: str,
        timeout: float | None = None,
        *,
        mutates: str | None = None,
        withhold_output: str | None = None,
    ) -> CommandResult:
        """Run a command and wait for completion.

        Args:
            cmd: Shell command to execute.
            timeout: Optional timeout in seconds.
            mutates: Set to a short phrase ("install firefox") unless this command is
                purely read-only (see the module docstring). Gates the command behind
                `--confirm-each-command` and labels it in the debug trace.
            withhold_output: Set to a short phrase naming what the output carries and the
                article that forbids keeping it, when this command's own output may not be
                logged. The trace then records that phrase in place of the streams.
        """
        ...

    async def terminate_all_processes(self) -> None:
        """Terminate all tracked processes."""
        ...


class _GatedExecutorMixin:
    """Shared debug trace (#210) and confirmation gate for both executor implementations.

    Kept as a mixin rather than duplicated so the two implementations cannot drift on the
    one behaviour that must be identical on both machines: what the user is shown, and
    when they are asked.
    """

    host: ClassVar[Host]

    def __init__(self, gate: StepGate | None = None) -> None:
        self._gate = gate

    async def declare_modification(self, operation: str, *, mutates: str, host: Host | None = None) -> None:
        """Announce a modification made IN-PROCESS rather than by this executor.

        The escape hatch for the handful of source-side writes that are neither a shell
        command nor a transfer — `sync_history.record_role` rewrites its JSON file with
        `os.write` + `rename` in-process, while the target's identical update travels as a
        shell command and is traced automatically. Routing the in-process side through the
        executor keeps ONE funnel for the debug trace and the confirmation gate, instead of
        a second parallel mechanism that would drift.

        `operation` must describe the change concretely enough to audit it — the path, and
        what about it changes — since there is no command text to fall back on.

        The same rule decides whether an in-process step needs announcing at all: anything
        that is not purely read-only does, `mutates` being required here rather than
        optional.
        """
        await self._announce(operation, mutates, host=host)

    async def _announce(self, operation: str, mutates: str | None, host: Host | None = None) -> None:
        """Trace `operation` at DEBUG, then gate it when it is a modification.

        Trace first, so the debug log records the operation even if the user aborts at the
        prompt — "what was I about to be asked" is exactly what the log is for.

        The confirmation prompt is redacted here rather than left to the logging filter: it
        is the one route out of this method that never becomes a log record, and a command
        carrying a repository credential would otherwise be shown in full on screen
        (`PKG-FR-CREDENTIAL-PRIVACY`).
        """
        job = _active_job.get()
        on_host = host if host is not None else self.host
        _logger.debug(
            "%s%s",
            operation,
            f"  [{mutates}]" if mutates is not None else "",
            extra={"job": job, "host": on_host.value},
        )
        if mutates is None or self._gate is None:
            return
        await self._gate.confirm_action(
            job=job,
            host=on_host,
            description=redact_credentials(mutates),
            command=redact_credentials(operation),
        )

    def _trace_output(self, result: CommandResult, host: Host | None = None, *, withhold: str | None = None) -> None:
        """Keep what the command said, verbatim, in the debug log (`PKG-FR-LOG-VERBATIM`).

        The counterpart to `_announce`: the trace records what was asked, this records what
        came back. Both halves are needed to answer "what did the tool actually do" — a
        package manager's own output is the only account of what it did with the transaction
        it was handed, and until now none of it reached the log at any level.

        stdout and stderr are separate records so a formatter never has to guess which
        stream a line came from. Empty streams produce nothing: a run's trace is large
        enough without a line per silent command.

        `withhold` is the one exception `PKG-FR-LOG-VERBATIM` names, and it is enforced
        here rather than by a filter downstream: a caller reading something the user's own
        privacy articles forbid keeping — `pro status`, whose payload names the subscriber
        (`PKG-FR-ESM-PRIVACY`) — declares that at the call, and neither stream reaches a log
        sink at any level. The phrase itself is logged, so the trace still shows the command
        answered and says why the answer is not there.
        """
        extra = {"job": _active_job.get(), "host": (host if host is not None else self.host).value}
        if withhold is not None:
            _logger.debug("output withheld: %s", withhold, extra=extra)
            return
        if result.stdout:
            _logger.debug("stdout: %s", result.stdout.rstrip("\n"), extra=extra)
        if result.stderr:
            _logger.debug("stderr: %s", result.stderr.rstrip("\n"), extra=extra)


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

    async def read_stdout_chunks(self, size: int = 4096) -> AsyncGenerator[bytes]:
        """Yield raw stdout bytes in chunks until EOF.

        Use instead of the line-based `stdout()` iterator when the process
        writes carriage-return-delimited output (e.g. rsync `--info=progress2`)
        that would block a readline-based reader indefinitely (RESEARCH Pitfall 2).

        Args:
            size: Number of bytes to read per chunk.

        Yields:
            Raw bytes chunks as they arrive; stops at EOF.
        """
        if self._proc.stdout is None:
            return
        while True:
            chunk = await self._proc.stdout.read(size)
            if not chunk:
                break
            yield chunk

    async def wait_result(self) -> CommandResult:
        """Wait for process exit and return the result, assuming stdout already consumed.

        Reads stderr to completion (via the stderr pipe) and then waits for the
        process to exit.  The `stdout` field in the returned `CommandResult` is
        empty because stdout was already consumed by `read_stdout_chunks`.

        Use this after draining stdout via `read_stdout_chunks` to obtain the
        exit code and any error output without calling `communicate()` (which
        would attempt to read stdout a second time).
        """
        stderr_bytes = b""
        if self._proc.stderr is not None:
            stderr_bytes = await self._proc.stderr.read()
        await self._proc.wait()
        return CommandResult(
            exit_code=self._proc.returncode or 0,
            stdout="",
            stderr=stderr_bytes.decode(errors="replace"),
        )

    async def terminate(self) -> None:
        """Terminate the process."""
        self._proc.terminate()
        await self._proc.wait()


class LocalExecutor(_GatedExecutorMixin):
    """Executes commands on the source machine via async subprocess."""

    host: ClassVar[Host] = Host.SOURCE

    def __init__(self, gate: StepGate | None = None) -> None:
        super().__init__(gate)
        self._processes: list[asyncio.subprocess.Process] = []

    async def run_command(
        self,
        cmd: str,
        timeout: float | None = None,
        *,
        mutates: str | None = None,
        withhold_output: str | None = None,
    ) -> CommandResult:
        """Run a command and wait for completion.

        Args:
            cmd: Shell command to execute
            timeout: Optional timeout in seconds
            mutates: Short phrase describing what this command changes on the source; None
                only when it is purely read-only (see the module docstring).
            withhold_output: Short phrase naming what the output carries, when it may not
                be logged (`_trace_output`); None keeps the verbatim trace.

        Returns:
            CommandResult with exit code, stdout, and stderr
        """
        await self._announce(cmd, mutates)
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
            result = CommandResult(
                exit_code=proc.returncode or 0,
                stdout=stdout.decode() if stdout else "",
                stderr=stderr.decode() if stderr else "",
            )
            self._trace_output(result, withhold=withhold_output)
            return result
        except TimeoutError:
            proc.terminate()
            await proc.wait()
            raise

    async def start_process(self, cmd: str, *, mutates: str | None = None) -> LocalProcess:
        """Start a long-running process with streaming output.

        Args:
            cmd: Shell command to execute
            mutates: Short phrase describing what starting this process changes on the
                source. Starting one is itself process state, so a background process is
                gated on that alone even when its command only reads.

        Returns:
            LocalProcess wrapper for the subprocess
        """
        await self._announce(f"{cmd}  (background)", mutates)
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

    def poll(self) -> int | None:
        """Return the exit status if the process has finished, else None (still running).

        Mirrors subprocess.Popen.poll(). Used to detect a process that exited
        immediately (e.g. a non-blocking `flock` that failed to acquire the lock).
        """
        return self._proc.exit_status

    async def terminate(self) -> None:
        """Terminate the process."""
        self._proc.terminate()
        await self._proc.wait()


class RemoteExecutor(_GatedExecutorMixin):
    """Executes commands on target machine via SSH connection."""

    host: ClassVar[Host] = Host.TARGET

    def __init__(self, conn: asyncssh.SSHClientConnection, gate: StepGate | None = None) -> None:
        super().__init__(gate)
        self._conn = conn
        self._processes: list[asyncssh.SSHClientProcess[str]] = []
        self._default_login_shell = False

    def _wrap_for_login_shell(self, cmd: str) -> str:
        """Wrap command for execution in bash login shell.

        Remote SSH commands run in non-login shells by default, which means
        ~/.profile isn't sourced and PATH may not include ~/.local/bin.

        This wrapper ensures commands run with full user environment by
        wrapping them in 'bash --login -c "..."', which:
        - Sources /etc/profile and ~/.profile (login shell behavior)
        - Ensures PATH includes user-installed tools (uv, pc-switcher)
        - Simulates interactive SSH session environment

        Args:
            cmd: Original shell command

        Returns:
            Wrapped command with bash --login -c prefix and proper quoting
        """
        return f"bash --login -c {shlex.quote(cmd)}"

    async def run_command(
        self,
        cmd: str,
        timeout: float | None = None,
        login_shell: bool | None = None,
        *,
        mutates: str | None = None,
        withhold_output: str | None = None,
    ) -> CommandResult:
        """Run a command on remote machine and wait for completion.

        Args:
            cmd: Shell command to execute
            timeout: Optional timeout in seconds
            login_shell: If True, wrap command in 'bash --login -c' to source ~/.profile
                and ensure proper PATH. If None, uses the executor's default.
                Useful for commands requiring user-installed tools (e.g., uv, pc-switcher).
            mutates: Short phrase describing what this command changes on the target; None
                only when it is purely read-only (see the module docstring).
            withhold_output: Short phrase naming what the output carries, when it may not
                be logged (`_trace_output`); None keeps the verbatim trace.

        Returns:
            CommandResult with exit code, stdout, and stderr

        Note:
            Using login_shell=True adds overhead (typically 10-50ms per command)
            due to profile sourcing. Only use when environment setup is needed.
        """
        use_login_shell = login_shell if login_shell is not None else self._default_login_shell
        if use_login_shell:
            cmd = self._wrap_for_login_shell(cmd)

        # Announced AFTER the login-shell wrap so the traced and prompted string is
        # byte-for-byte what the remote shell receives.
        await self._announce(cmd, mutates)

        try:
            result = await asyncio.wait_for(
                self._conn.run(cmd),
                timeout=timeout,
            )
            outcome = CommandResult(
                exit_code=result.exit_status or 0,
                stdout=str(result.stdout) if result.stdout else "",
                stderr=str(result.stderr) if result.stderr else "",
            )
            self._trace_output(outcome, withhold=withhold_output)
            return outcome
        except TimeoutError:
            raise

    async def start_process(
        self, cmd: str, login_shell: bool | None = None, *, mutates: str | None = None
    ) -> RemoteProcess:
        """Start a long-running process on remote machine.

        Args:
            cmd: Shell command to execute
            login_shell: If True, wrap command in 'bash --login -c' to source ~/.profile
                and ensure proper PATH. If None, uses the executor's default.
                Useful for background processes requiring user-installed tools.
            mutates: Short phrase describing what starting this process changes on the
                target. Starting one is itself process state, so a background process is
                gated on that alone even when its command only reads — a `flock` that holds
                a lock open changes no file and is a modification all the same.

        Returns:
            RemoteProcess wrapper for the SSH process

        Note:
            Using login_shell=True adds startup overhead. Only use when
            environment setup is needed for the background process.
        """
        use_login_shell = login_shell if login_shell is not None else self._default_login_shell
        if use_login_shell:
            cmd = self._wrap_for_login_shell(cmd)

        await self._announce(f"{cmd}  (background)", mutates)
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

    async def send_file(self, local: Path, remote: str, *, mutates: str | None = None) -> None:
        """Copy a file from local machine to remote.

        Args:
            local: Local file path
            remote: Remote destination path
            mutates: Short phrase describing the change. A transfer is never purely
                read-only at the destination; None only when that destination is scratch
                space nobody would need to audit.
        """
        await self._announce(f"send_file {local} -> {remote}", mutates)
        async with self._conn.start_sftp_client() as sftp:
            await sftp.put(str(local), remote)

    async def get_file(self, remote: str, local: Path, *, mutates: str | None = None) -> None:
        """Copy a file from remote machine to local.

        Args:
            remote: Remote file path
            local: Local destination path
            mutates: Short phrase describing the change this makes on the SOURCE, whose
                filesystem it writes; None only for a fetch into scratch space.
        """
        # Announced against the SOURCE: this direction writes to the local filesystem, so
        # tracing it under the executor's own target host would name the wrong machine.
        await self._announce(f"get_file {remote} -> {local}", mutates, host=Host.SOURCE)
        async with self._conn.start_sftp_client() as sftp:
            await sftp.get(remote, str(local))


class BashLoginRemoteExecutor(RemoteExecutor):
    """RemoteExecutor that runs all commands in bash login shell by default.

    This executor wraps all commands in 'bash --login -c "..."' to ensure:
    - ~/.profile is sourced
    - PATH includes ~/.local/bin (for user-installed tools like uv, pc-switcher)
    - Environment matches interactive SSH sessions

    Primarily used in integration tests where all commands interact with
    user-installed tools. Production code should use base RemoteExecutor
    and explicitly pass login_shell=True only when needed.

    The login_shell parameter can still be overridden per-call:
        executor.run_command("sudo systemctl status", login_shell=False)
    """

    def __init__(self, conn: asyncssh.SSHClientConnection, gate: StepGate | None = None) -> None:
        """Initialize with login shell enabled by default."""
        super().__init__(conn, gate)
        self._default_login_shell = True
