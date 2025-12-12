"""Unit tests for RemoteExecutor login_shell parameter."""

from __future__ import annotations

import shlex
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.executor import RemoteExecutor


class TestWrapForLoginShell:
    """Test the _wrap_for_login_shell helper method."""

    def test_wraps_simple_command(self) -> None:
        """Simple commands are wrapped in bash -l -c with quotes."""
        conn = MagicMock()
        executor = RemoteExecutor(conn)

        result = executor._wrap_for_login_shell("echo hello")
        expected = f"bash -l -c {shlex.quote('echo hello')}"
        assert result == expected

    def test_wraps_command_with_quotes(self) -> None:
        """Commands with quotes are safely escaped."""
        conn = MagicMock()
        executor = RemoteExecutor(conn)

        cmd = 'echo "hello world"'
        result = executor._wrap_for_login_shell(cmd)
        expected = f"bash -l -c {shlex.quote(cmd)}"
        assert result == expected

    def test_wraps_command_with_special_chars(self) -> None:
        """Commands with special characters are safely escaped."""
        conn = MagicMock()
        executor = RemoteExecutor(conn)

        cmd = "echo $HOME && ls | grep foo"
        result = executor._wrap_for_login_shell(cmd)
        expected = f"bash -l -c {shlex.quote(cmd)}"
        assert result == expected

    def test_wraps_command_with_single_quotes(self) -> None:
        """Commands with single quotes are safely escaped."""
        conn = MagicMock()
        executor = RemoteExecutor(conn)

        cmd = "echo 'hello world'"
        result = executor._wrap_for_login_shell(cmd)
        expected = f"bash -l -c {shlex.quote(cmd)}"
        assert result == expected

    def test_wraps_multiline_command(self) -> None:
        """Multi-line commands are safely escaped."""
        conn = MagicMock()
        executor = RemoteExecutor(conn)

        cmd = "echo hello\necho world"
        result = executor._wrap_for_login_shell(cmd)
        expected = f"bash -l -c {shlex.quote(cmd)}"
        assert result == expected


class TestRunCommandLoginShell:
    """Test run_command with login_shell parameter."""

    @pytest.mark.asyncio
    async def test_login_shell_true_wraps_command(self) -> None:
        """When login_shell=True, command is wrapped."""
        conn = MagicMock()
        conn.run = AsyncMock(
            return_value=MagicMock(
                exit_status=0,
                stdout="output",
                stderr="",
            )
        )

        executor = RemoteExecutor(conn)
        result = await executor.run_command("echo hello", login_shell=True)

        # Verify wrapped command was passed to SSH
        expected_cmd = f"bash -l -c {shlex.quote('echo hello')}"
        conn.run.assert_called_once()
        actual_cmd = conn.run.call_args[0][0]
        assert actual_cmd == expected_cmd

        # Verify result is correct
        assert result.exit_code == 0
        assert result.stdout == "output"

    @pytest.mark.asyncio
    async def test_login_shell_false_no_wrap(self) -> None:
        """When login_shell=False (default), command is not wrapped."""
        conn = MagicMock()
        conn.run = AsyncMock(
            return_value=MagicMock(
                exit_status=0,
                stdout="output",
                stderr="",
            )
        )

        executor = RemoteExecutor(conn)
        result = await executor.run_command("echo hello", login_shell=False)

        # Verify original command was passed to SSH
        conn.run.assert_called_once()
        actual_cmd = conn.run.call_args[0][0]
        assert actual_cmd == "echo hello"

    @pytest.mark.asyncio
    async def test_login_shell_default_false(self) -> None:
        """Default behavior (no login_shell arg) doesn't wrap."""
        conn = MagicMock()
        conn.run = AsyncMock(
            return_value=MagicMock(
                exit_status=0,
                stdout="output",
                stderr="",
            )
        )

        executor = RemoteExecutor(conn)
        result = await executor.run_command("echo hello")

        # Verify original command was passed to SSH (backward compatible)
        actual_cmd = conn.run.call_args[0][0]
        assert actual_cmd == "echo hello"

    @pytest.mark.asyncio
    async def test_timeout_works_with_login_shell(self) -> None:
        """Timeout parameter still works with login_shell=True."""
        import asyncio

        conn = MagicMock()

        # Simulate slow command
        async def slow_run(cmd: str) -> MagicMock:
            await asyncio.sleep(10)
            return MagicMock(exit_status=0, stdout="", stderr="")

        conn.run = slow_run

        executor = RemoteExecutor(conn)

        with pytest.raises(TimeoutError):
            await executor.run_command("sleep 10", timeout=0.1, login_shell=True)

    @pytest.mark.asyncio
    async def test_login_shell_with_complex_command(self) -> None:
        """Complex commands with pipes and redirects are wrapped correctly."""
        conn = MagicMock()
        conn.run = AsyncMock(
            return_value=MagicMock(
                exit_status=0,
                stdout="result",
                stderr="",
            )
        )

        executor = RemoteExecutor(conn)
        cmd = "ps aux | grep python | awk '{print $2}'"
        result = await executor.run_command(cmd, login_shell=True)

        # Verify wrapped command
        expected_cmd = f"bash -l -c {shlex.quote(cmd)}"
        actual_cmd = conn.run.call_args[0][0]
        assert actual_cmd == expected_cmd
        assert result.success


class TestStartProcessLoginShell:
    """Test start_process with login_shell parameter."""

    @pytest.mark.asyncio
    async def test_login_shell_true_wraps_command(self) -> None:
        """When login_shell=True, command is wrapped."""
        conn = MagicMock()
        mock_process = MagicMock()
        conn.create_process = AsyncMock(return_value=mock_process)

        executor = RemoteExecutor(conn)
        remote_process = await executor.start_process("tail -f /var/log/syslog", login_shell=True)

        # Verify wrapped command was passed to SSH
        expected_cmd = f"bash -l -c {shlex.quote('tail -f /var/log/syslog')}"
        conn.create_process.assert_called_once()
        actual_cmd = conn.create_process.call_args[0][0]
        assert actual_cmd == expected_cmd

        # Verify RemoteProcess was returned
        assert remote_process is not None

    @pytest.mark.asyncio
    async def test_login_shell_false_no_wrap(self) -> None:
        """When login_shell=False (default), command is not wrapped."""
        conn = MagicMock()
        mock_process = MagicMock()
        conn.create_process = AsyncMock(return_value=mock_process)

        executor = RemoteExecutor(conn)
        remote_process = await executor.start_process("tail -f /var/log/syslog", login_shell=False)

        # Verify original command was passed to SSH
        conn.create_process.assert_called_once()
        actual_cmd = conn.create_process.call_args[0][0]
        assert actual_cmd == "tail -f /var/log/syslog"

    @pytest.mark.asyncio
    async def test_login_shell_default_false(self) -> None:
        """Default behavior (no login_shell arg) doesn't wrap."""
        conn = MagicMock()
        mock_process = MagicMock()
        conn.create_process = AsyncMock(return_value=mock_process)

        executor = RemoteExecutor(conn)
        remote_process = await executor.start_process("tail -f /var/log/syslog")

        # Verify original command was passed to SSH (backward compatible)
        actual_cmd = conn.create_process.call_args[0][0]
        assert actual_cmd == "tail -f /var/log/syslog"

    @pytest.mark.asyncio
    async def test_process_tracking_with_login_shell(self) -> None:
        """Processes started with login_shell=True are tracked correctly."""
        conn = MagicMock()
        mock_process = MagicMock()
        conn.create_process = AsyncMock(return_value=mock_process)

        executor = RemoteExecutor(conn)

        # Start process with login shell
        await executor.start_process("some_command", login_shell=True)

        # Verify process is tracked
        assert len(executor._processes) == 1
        assert executor._processes[0] == mock_process
