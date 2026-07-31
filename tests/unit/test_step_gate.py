"""Unit tests for the `--confirm-each-command` gate and the executor debug trace (#210).

Three properties matter and everything here serves one of them: the gate never lets a
modification through without an explicit "proceed", an ungated run costs nothing, and a
read is never mistaken for a write.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from pcswitcher.executor import Executor, LocalExecutor, RemoteExecutor, active_job
from pcswitcher.jobs.packages.items import ItemClass
from pcswitcher.jobs.packages.state import DecisionEntry, DecisionFile, Snippet, SnippetRegistry
from pcswitcher.models import Host, SyncAbortedByUser
from pcswitcher.step_gate import StepGate, TerminalUIStepGate


def _make_gate() -> tuple[TerminalUIStepGate, MagicMock]:
    """A gate wired to a real (non-terminal) Console and a mock UI; return both."""
    ui = MagicMock()
    return TerminalUIStepGate(Console(), ui, source_hostname="atlas", target_hostname="nomad"), ui


def _stub_gate(side_effect: object | None = None) -> MagicMock:
    gate = MagicMock()
    gate.confirm_action = AsyncMock(side_effect=side_effect)
    return gate


def _remote(gate: object | None = None) -> tuple[RemoteExecutor, MagicMock]:
    """A RemoteExecutor over a mock asyncssh connection; returns both."""
    conn = MagicMock()
    conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout="", stderr=""))
    return RemoteExecutor(conn, gate), conn  # pyright: ignore[reportArgumentType] — mock stands in for asyncssh


@contextmanager
def _executor_on(host: Host, gate: StepGate | None) -> Generator[Executor]:
    """The REAL executor for `host`, gated, with nothing actually reaching a machine.

    The local side patches `create_subprocess_shell` rather than swapping in a fake
    executor, so the announce-then-gate path under test is the production one on both
    ends — the point being that the same store code is correct through either.
    """
    if host is Host.TARGET:
        executor, _conn = _remote(gate)
        yield executor
        return

    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b"", b""))
    with patch.object(asyncio, "create_subprocess_shell", AsyncMock(return_value=proc)):
        yield LocalExecutor(gate)


@pytest.mark.asyncio
class TestTerminalUIStepGate:
    async def test_proceed_returns_and_toggles_ui(self) -> None:
        """J151 — proceeding runs the command and hands the display back."""
        gate, ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", return_value="p"):
            await gate.confirm_action(job="apt_sync", host=Host.TARGET, description="install x", command="apt-get x")
        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_abort_raises_and_resumes_ui(self) -> None:
        """H155 — aborting at the prompt stops the sync and hands the display back."""
        gate, ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", return_value="a"), pytest.raises(SyncAbortedByUser):
            await gate.confirm_action(job="apt_sync", host=Host.TARGET, description="install x", command="apt-get x")
        ui.resume.assert_called_once()

    @pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
    async def test_unanswerable_prompt_aborts_never_proceeds(self, interrupt: type[BaseException]) -> None:
        """J165, H155 — an interrupted prompt is an abort, not an approval — the one thing that must
        never silently succeed."""
        gate, ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", side_effect=interrupt), pytest.raises(SyncAbortedByUser):
            await gate.confirm_action(job="snap_sync", host=Host.SOURCE, description="d", command="c")
        ui.resume.assert_called_once()

    async def test_the_panel_names_the_machine_by_hostname(self) -> None:
        """J163, H70 — `PKG-FR-NAME-THE-MACHINES`: this is a question the user answers, so the machine
        about to be changed is named once in the heading and never by its role."""
        ui = MagicMock()
        console = Console(record=True, width=100)
        gate = TerminalUIStepGate(console, ui, source_hostname="atlas", target_hostname="nomad")
        with patch("rich.prompt.Prompt.ask", return_value="p"):
            await gate.confirm_action(job="apt_sync", host=Host.TARGET, description="install x", command="apt-get x")

        rendered = console.export_text()
        assert "apt_sync → nomad" in rendered
        assert "target" not in rendered

    async def test_the_abort_message_names_the_machine_by_hostname(self) -> None:
        """J163, H71 — the abort names the hostname, not the role."""
        gate, _ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", return_value="a"), pytest.raises(SyncAbortedByUser) as excinfo:
            await gate.confirm_action(job="snap_sync", host=Host.SOURCE, description="pause refreshes", command="c")
        assert "on atlas" in str(excinfo.value)
        assert "source" not in str(excinfo.value)

    async def test_the_prompt_offers_no_default(self) -> None:
        """J164 — `PKG-FR-HARMLESS-DEFAULT` has no answer to give here: proceeding and
        aborting are both consequential, so an accidental Enter must re-prompt rather than
        pick one. Asserted on the call, since a `default=` would be invisible in the
        rendered panel.
        """
        gate, _ui = _make_gate()
        with patch("rich.prompt.Prompt.ask", return_value="p") as ask:
            await gate.confirm_action(job="apt_sync", host=Host.TARGET, description="install x", command="apt-get x")

        assert "default" not in ask.call_args.kwargs

    async def test_command_with_markup_characters_does_not_raise(self) -> None:
        """J167 — a command containing Rich markup syntax renders as literal text."""
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
        """J159 — a read never prompts."""
        gate = _stub_gate()
        executor, conn = _remote(gate)
        await executor.run_command("apt-mark showmanual", login_shell=False)
        gate.confirm_action.assert_not_awaited()
        conn.run.assert_awaited_once()

    async def test_write_is_gated_with_the_verbatim_command(self) -> None:
        """J151 — the user sees the command as the shell will receive it, and must answer."""
        gate = _stub_gate()
        executor, conn = _remote(gate)
        await executor.run_command(
            "sudo apt-get install --assume-yes firefox", login_shell=False, mutates="install firefox"
        )
        gate.confirm_action.assert_awaited_once_with(
            job="orchestrator",
            host=Host.TARGET,
            description="install firefox",
            command="sudo apt-get install --assume-yes firefox",
        )
        conn.run.assert_awaited_once()

    async def test_gate_sees_the_login_shell_wrapped_command(self) -> None:
        """J114 — what is displayed must be byte-for-byte what the remote shell receives."""
        gate = _stub_gate()
        executor, _conn = _remote(gate)
        await executor.run_command("pc-switcher --version", login_shell=True, mutates="upgrade")
        shown = gate.confirm_action.await_args.kwargs["command"]
        assert shown.startswith("bash --login -c ")
        assert "pc-switcher --version" in shown

    async def test_abort_prevents_the_command(self) -> None:
        """J160 — an abort issues nothing."""
        gate = _stub_gate(SyncAbortedByUser("declined"))
        executor, conn = _remote(gate)
        with pytest.raises(SyncAbortedByUser):
            await executor.run_command("sudo rm --recursive --force /etc/apt/x", login_shell=False, mutates="delete x")
        conn.run.assert_not_awaited()

    async def test_send_file_shows_both_paths_and_aborts_before_transfer(self) -> None:
        """J154 — a transfer is gated too, naming where the file comes from and where it lands."""
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
        """J161 — the escape hatch for writes that are neither a command nor a transfer."""
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
        await executor.run_command(
            "sudo apt-get install --assume-yes firefox", login_shell=False, mutates="install firefox"
        )
        conn.run.assert_awaited_once()

    async def test_active_job_labels_the_prompt(self) -> None:
        """J168 — the prompt names the job issuing the command."""
        gate = _stub_gate()
        executor, _conn = _remote(gate)
        with active_job("apt_sync"):
            await executor.run_command(
                "sudo apt-get install --assume-yes firefox", login_shell=False, mutates="install"
            )
        assert gate.confirm_action.await_args.kwargs["job"] == "apt_sync"
        # The label is scoped to the block; outside it, the orchestrator owns the traffic.
        await executor.run_command("sudo apt-get remove --assume-yes firefox", login_shell=False, mutates="remove")
        assert gate.confirm_action.await_args.kwargs["job"] == "orchestrator"


@pytest.mark.asyncio
class TestExecutorDebugTrace:
    """Every operation is traced verbatim at DEBUG, reads included (#210)."""

    async def test_read_and_write_are_both_traced(self, caplog: pytest.LogCaptureFixture) -> None:
        """J108, J109 — every command is traced verbatim, and a write carries its declared intent."""
        executor, _conn = _remote(None)
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.executor"):
            await executor.run_command("apt-mark showmanual", login_shell=False)
            await executor.run_command(
                "sudo apt-get install --assume-yes firefox", login_shell=False, mutates="install firefox"
            )

        messages = [record.getMessage() for record in caplog.records]
        assert "apt-mark showmanual" in messages
        # The write is traced with the same verbatim command plus its declared intent.
        assert "sudo apt-get install --assume-yes firefox  [install firefox]" in messages

    async def test_trace_carries_job_and_host(self, caplog: pytest.LogCaptureFixture) -> None:
        """J113 — each trace record is attributed to a job and a machine."""
        executor, _conn = _remote(None)
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.executor"), active_job("snap_sync"):
            await executor.run_command("snap list --all", login_shell=False)

        record = caplog.records[-1]
        assert record.job == "snap_sync"  # pyright: ignore[reportAttributeAccessIssue] — set via `extra`
        assert record.host == "target"  # pyright: ignore[reportAttributeAccessIssue] — set via `extra`

    async def test_trace_is_written_before_the_gate_can_abort(self, caplog: pytest.LogCaptureFixture) -> None:
        """J112 — "what was I about to be asked" is exactly what the debug log is for."""
        executor, _conn = _remote(_stub_gate(SyncAbortedByUser("declined")))
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.executor"), pytest.raises(SyncAbortedByUser):
            await executor.run_command("sudo rm --recursive --force /etc/apt/x", login_shell=False, mutates="delete x")

        assert any("sudo rm --recursive --force /etc/apt/x" in record.getMessage() for record in caplog.records)

    async def test_what_the_command_said_is_traced_too(self, caplog: pytest.LogCaptureFixture) -> None:
        """J110 — `PKG-FR-LOG-VERBATIM`: a package manager's own output is the only account of what
        it did with the transaction it was handed."""
        executor, conn = _remote(None)
        conn.run = AsyncMock(
            return_value=MagicMock(exit_status=100, stdout="Reading package lists...\n", stderr="E: Unable to fetch\n")
        )
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.executor"):
            await executor.run_command("sudo apt-get update", login_shell=False, mutates="refresh package lists")

        messages = [record.getMessage() for record in caplog.records]
        assert "stdout: Reading package lists..." in messages
        assert "stderr: E: Unable to fetch" in messages

    async def test_a_silent_command_adds_no_output_lines(self, caplog: pytest.LogCaptureFixture) -> None:
        """J111 — a run's trace is large enough without a line per command that said nothing."""
        executor, _conn = _remote(None)
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.executor"):
            await executor.run_command("true", login_shell=False)

        assert [record.getMessage() for record in caplog.records] == ["true"]

    async def test_the_confirmation_prompt_withholds_a_url_credential(self) -> None:
        """J121 — `PKG-FR-CREDENTIAL-PRIVACY`: the prompt is the one route out of `_announce` that
        never becomes a log record, so the logging filter cannot cover it."""
        gate = _stub_gate()
        executor, _conn = _remote(gate)

        await executor.run_command(
            "sudo apt-get install --assume-yes --target https://bearer:tok3n@example.com/deb",
            login_shell=False,
            mutates="install from https://bearer:tok3n@example.com/deb",
        )

        kwargs = gate.confirm_action.await_args.kwargs
        assert "tok3n" not in kwargs["command"]
        assert "tok3n" not in kwargs["description"]
        assert "https://***@example.com/deb" in kwargs["command"]


def _entry() -> DecisionEntry:
    return DecisionEntry(
        item_id="apt:firefox",
        item_class=ItemClass.APT_PACKAGE,
        label="firefox",
        reason=None,
        recorded_at="2026-01-01T00:00:00Z",
    )


def _snippet() -> Snippet:
    return Snippet(
        item_id="manual:zoom",
        label="zoom",
        body=(
            "curl --fail --silent --show-error --location https://example.invalid/zoom.deb --output /tmp/z.deb"
            " && sudo apt-get install --assume-yes /tmp/z.deb"
        ),
        authored_at="2026-01-01T00:00:00Z",
        authored_on="laptop",
    )


@pytest.mark.asyncio
class TestStateWritesReachTheGate:
    """The two package-sync state files are modifications like any other.

    `docs/jobs/package-sync.md` promises the gate covers the machine-local decision files
    on BOTH machines and the snippet registry, and both stores are constructed with either
    executor depending on which machine holds the item (D-08a) — so the assertion has to be
    made per role, not once. A read of either file must not prompt: the whole store would
    become unusable at the gate if `load()` asked too.
    """

    @pytest.mark.parametrize("host", [Host.SOURCE, Host.TARGET])
    async def test_recording_a_decision_is_gated(self, host: Host) -> None:
        """J152, H14 — the mark is a modification, on whichever machine holds the item."""
        gate = _stub_gate()
        with _executor_on(host, gate) as executor:
            await DecisionFile("apt", executor).record(_entry())

        gate.confirm_action.assert_awaited_once()  # the preceding `cat` read is not a prompt
        kwargs = gate.confirm_action.await_args.kwargs
        assert kwargs["host"] is host
        assert "firefox" in kwargs["description"]
        assert "apt.decisions.yaml" in kwargs["command"]

    @pytest.mark.parametrize("host", [Host.SOURCE, Host.TARGET])
    async def test_adding_a_snippet_is_gated(self, host: Host) -> None:
        """J153, H15 — writing the registry is a modification, on either machine."""
        gate = _stub_gate()
        with _executor_on(host, gate) as executor:
            await SnippetRegistry(executor).add(_snippet())

        gate.confirm_action.assert_awaited_once()
        kwargs = gate.confirm_action.await_args.kwargs
        assert kwargs["host"] is host
        assert "zoom" in kwargs["description"]
        assert "package-snippets.yaml" in kwargs["command"]

    @pytest.mark.parametrize("host", [Host.SOURCE, Host.TARGET])
    async def test_aborting_leaves_the_file_untouched(self, host: Host) -> None:
        """J160, H14 — declining must stop the write, not record it and then complain."""
        gate = _stub_gate(SyncAbortedByUser("declined"))
        with _executor_on(host, gate) as executor, pytest.raises(SyncAbortedByUser):
            await DecisionFile("apt", executor).record(_entry())

    async def test_pushing_the_registry_to_the_target_is_gated(self) -> None:
        """J154, H15 — The push itself is a modification of the target, distinct from the local write."""
        gate = _stub_gate()
        executor, conn = _remote(gate)
        await executor.send_file(
            Path("/home/u/.config/pc-switcher/package-snippets.yaml"),
            "/home/u/.config/pc-switcher/package-snippets.yaml",
            mutates="push the install-snippet registry",
        )
        assert gate.confirm_action.await_args.kwargs["host"] is Host.TARGET
        assert "package-snippets.yaml" in gate.confirm_action.await_args.kwargs["command"]
        conn.start_sftp_client.assert_called_once()

    @pytest.mark.parametrize("host", [Host.SOURCE, Host.TARGET])
    async def test_reading_either_store_never_prompts(self, host: Host) -> None:
        """J159 — reading the decision file or the snippet registry prompts for nothing."""
        gate = _stub_gate()
        with _executor_on(host, gate) as executor:
            await DecisionFile("apt", executor).load()
            await SnippetRegistry(executor).load()
        gate.confirm_action.assert_not_awaited()
