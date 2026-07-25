"""Unit tests for the `--confirm-each-command` gate and the executor debug trace (#210).

Three properties matter and everything here serves one of them: the gate never lets a
modification through without an explicit "proceed", an ungated run costs nothing, and a
read is never mistaken for a write.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from pcswitcher.executor import LocalExecutor, RemoteExecutor, active_job
from pcswitcher.models import Host, SyncAbortedByUser
from pcswitcher.step_gate import TerminalUIStepGate


def _make_gate() -> tuple[TerminalUIStepGate, MagicMock]:
    """A gate wired to a real (non-terminal) Console and a mock UI; return both."""
    ui = MagicMock()
    return TerminalUIStepGate(Console(), ui), ui


def _stub_gate(side_effect: object | None = None) -> MagicMock:
    gate = MagicMock()
    gate.confirm_action = AsyncMock(side_effect=side_effect)
    return gate


def _remote(gate: object | None = None) -> tuple[RemoteExecutor, MagicMock]:
    """A RemoteExecutor over a mock asyncssh connection; returns both."""
    conn = MagicMock()
    conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="", stderr=""))
    return RemoteExecutor(conn, gate), conn  # pyright: ignore[reportArgumentType] — mock stands in for asyncssh


@pytest.mark.asyncio
class TestTerminalUIStepGate:
    async def test_proceed_returns_and_toggles_ui(self) -> None:
        gate, ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", return_value="p"):
            await gate.confirm_action(job="apt_sync", host=Host.TARGET, description="install x", command="apt-get x")
        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_abort_raises_and_resumes_ui(self) -> None:
        gate, ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", return_value="a"), pytest.raises(SyncAbortedByUser):
            await gate.confirm_action(job="apt_sync", host=Host.TARGET, description="install x", command="apt-get x")
        ui.resume.assert_called_once()

    @pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
    async def test_unanswerable_prompt_aborts_never_proceeds(self, interrupt: type[BaseException]) -> None:
        """An interrupted prompt is an abort, not an approval — the one thing that must
        never silently succeed."""
        gate, ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", side_effect=interrupt), pytest.raises(SyncAbortedByUser):
            await gate.confirm_action(job="snap_sync", host=Host.SOURCE, description="d", command="c")
        ui.resume.assert_called_once()

    async def test_command_with_markup_characters_does_not_raise(self) -> None:
        """A command containing Rich markup syntax renders as literal text."""
        gate, _ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", return_value="p"):
            await gate.confirm_action(
                job="manual_installs_sync",
                host=Host.TARGET,
                description="replay snippet",
                command="bash -c 'echo [not-a-tag] [/bold]'",
            )


@pytest.mark.asyncio
class TestExecutorGate:
    """`mutates=` is the whole contract: it marks a write, gates it, and describes it."""

    async def test_read_is_never_gated(self) -> None:
        gate = _stub_gate()
        executor, conn = _remote(gate)
        await executor.run_command("apt-mark showmanual", login_shell=False)
        gate.confirm_action.assert_not_awaited()
        conn.run.assert_awaited_once()

    async def test_write_is_gated_with_the_verbatim_command(self) -> None:
        gate = _stub_gate()
        executor, conn = _remote(gate)
        await executor.run_command("sudo apt-get install -y firefox", login_shell=False, mutates="install firefox")
        gate.confirm_action.assert_awaited_once_with(
            job="orchestrator",
            host=Host.TARGET,
            description="install firefox",
            command="sudo apt-get install -y firefox",
        )
        conn.run.assert_awaited_once()

    async def test_gate_sees_the_login_shell_wrapped_command(self) -> None:
        """What is displayed must be byte-for-byte what the remote shell receives."""
        gate = _stub_gate()
        executor, _conn = _remote(gate)
        await executor.run_command("pc-switcher --version", login_shell=True, mutates="upgrade")
        shown = gate.confirm_action.await_args.kwargs["command"]
        assert shown.startswith("bash -l -c ")
        assert "pc-switcher --version" in shown

    async def test_abort_prevents_the_command(self) -> None:
        gate = _stub_gate(SyncAbortedByUser("declined"))
        executor, conn = _remote(gate)
        with pytest.raises(SyncAbortedByUser):
            await executor.run_command("sudo rm -rf /etc/apt/x", login_shell=False, mutates="delete x")
        conn.run.assert_not_awaited()

    async def test_send_file_shows_both_paths_and_aborts_before_transfer(self) -> None:
        gate = _stub_gate(SyncAbortedByUser("declined"))
        executor, conn = _remote(gate)
        with pytest.raises(SyncAbortedByUser):
            await executor.send_file(Path("/local/f"), "/remote/f", mutates="push f")
        assert gate.confirm_action.await_args.kwargs["command"] == "send_file /local/f -> /remote/f"
        conn.start_sftp_client.assert_not_called()

    async def test_local_executor_reports_the_source_host(self) -> None:
        gate = _stub_gate()
        executor = LocalExecutor(gate)
        await executor.run_command("true", mutates="touch the source")
        assert gate.confirm_action.await_args.kwargs["host"] is Host.SOURCE

    async def test_declare_modification_gates_an_in_process_write(self) -> None:
        """The escape hatch for writes that are neither a command nor a transfer."""
        gate = _stub_gate()
        executor = LocalExecutor(gate)
        await executor.declare_modification("write ~/.local/share/x.json", mutates="record the role")
        gate.confirm_action.assert_awaited_once_with(
            job="orchestrator",
            host=Host.SOURCE,
            description="record the role",
            command="write ~/.local/share/x.json",
        )

    async def test_no_gate_configured_is_a_plain_pass_through(self) -> None:
        executor, conn = _remote(None)
        await executor.run_command("sudo apt-get install -y firefox", login_shell=False, mutates="install firefox")
        conn.run.assert_awaited_once()

    async def test_active_job_labels_the_prompt(self) -> None:
        gate = _stub_gate()
        executor, _conn = _remote(gate)
        with active_job("apt_sync"):
            await executor.run_command("sudo apt-get install -y firefox", login_shell=False, mutates="install")
        assert gate.confirm_action.await_args.kwargs["job"] == "apt_sync"
        # The label is scoped to the block; outside it, the orchestrator owns the traffic.
        await executor.run_command("sudo apt-get remove -y firefox", login_shell=False, mutates="remove")
        assert gate.confirm_action.await_args.kwargs["job"] == "orchestrator"


@pytest.mark.asyncio
class TestExecutorDebugTrace:
    """Every operation is traced verbatim at DEBUG, reads included (#210)."""

    async def test_read_and_write_are_both_traced(self, caplog: pytest.LogCaptureFixture) -> None:
        executor, _conn = _remote(None)
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.executor"):
            await executor.run_command("apt-mark showmanual", login_shell=False)
            await executor.run_command("sudo apt-get install -y firefox", login_shell=False, mutates="install firefox")

        messages = [record.getMessage() for record in caplog.records]
        assert "apt-mark showmanual" in messages
        # The write is traced with the same verbatim command plus its declared intent.
        assert "sudo apt-get install -y firefox  [install firefox]" in messages

    async def test_trace_carries_job_and_host(self, caplog: pytest.LogCaptureFixture) -> None:
        executor, _conn = _remote(None)
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.executor"), active_job("snap_sync"):
            await executor.run_command("snap list --all", login_shell=False)

        record = caplog.records[-1]
        assert record.job == "snap_sync"  # pyright: ignore[reportAttributeAccessIssue] — set via `extra`
        assert record.host == "target"  # pyright: ignore[reportAttributeAccessIssue] — set via `extra`

    async def test_trace_is_written_before_the_gate_can_abort(self, caplog: pytest.LogCaptureFixture) -> None:
        """ "What was I about to be asked" is exactly what the debug log is for."""
        executor, _conn = _remote(_stub_gate(SyncAbortedByUser("declined")))
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.executor"), pytest.raises(SyncAbortedByUser):
            await executor.run_command("sudo rm -rf /etc/apt/x", login_shell=False, mutates="delete x")

        assert any("sudo rm -rf /etc/apt/x" in record.getMessage() for record in caplog.records)
