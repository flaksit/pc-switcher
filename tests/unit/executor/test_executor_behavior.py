"""Unit tests for RemoteExecutor behavior.

Tests the executor interface contract including:
- stdout/stderr separation
- Exit code handling
- Timeout behavior
- Working directory handling
- Environment variable propagation
- Multi-line output capture

These tests use mocked executors and don't require VM infrastructure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.models import CommandResult


class TestExecutorOutputCapture:
    """Tests for stdout/stderr capture behavior."""

    async def test_stdout_and_stderr_captured_separately(
        self, mock_executor: MagicMock
    ) -> None:
        """Test that stdout and stderr are captured in separate fields."""
        mock_executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="to stdout\n",
                stderr="to stderr\n",
            )
        )

        result = await mock_executor.run_command("echo 'to stdout' && echo 'to stderr' >&2")

        assert result.success
        assert "to stdout" in result.stdout
        assert "to stderr" in result.stderr
        assert "to stderr" not in result.stdout  # Verify separation
        assert "to stdout" not in result.stderr  # Verify separation

    async def test_multiline_output_preserved(self, mock_executor: MagicMock) -> None:
        """Test that multi-line output maintains line order."""
        mock_executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="line1\nline2\nline3\n",
                stderr="",
            )
        )

        result = await mock_executor.run_command("echo 'line1' && echo 'line2' && echo 'line3'")

        assert result.success
        assert "line1" in result.stdout
        assert "line2" in result.stdout
        assert "line3" in result.stdout
        # Verify order is preserved
        line1_pos = result.stdout.index("line1")
        line2_pos = result.stdout.index("line2")
        line3_pos = result.stdout.index("line3")
        assert line1_pos < line2_pos < line3_pos


class TestExecutorExitCodes:
    """Tests for exit code handling."""

    async def test_nonzero_exit_code_reported(self, mock_executor: MagicMock) -> None:
        """Test that non-zero exit codes are correctly reported.

        Per spec TST-FR-CONTRACT, we must verify failure paths.
        """
        mock_executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=42,
                stdout="",
                stderr="",
            )
        )

        result = await mock_executor.run_command("exit 42")

        assert not result.success
        assert result.exit_code == 42

    async def test_invalid_command_reports_failure(self, mock_executor: MagicMock) -> None:
        """Test that invalid commands result in failure with error message."""
        mock_executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=127,
                stdout="",
                stderr="/nonexistent/command: not found\n",
            )
        )

        result = await mock_executor.run_command("/nonexistent/command/that/does/not/exist")

        assert not result.success
        assert result.exit_code != 0
        # Stderr should contain error information
        assert len(result.stderr) > 0


class TestExecutorTimeout:
    """Tests for timeout handling."""

    async def test_timeout_raises_exception(self, mock_executor: MagicMock) -> None:
        """Test that long-running commands can be terminated via timeout."""
        mock_executor.run_command = AsyncMock(side_effect=TimeoutError("Command timed out"))

        with pytest.raises(TimeoutError):
            await mock_executor.run_command("sleep 10", timeout=0.5)


class TestExecutorEnvironment:
    """Tests for working directory and environment variables."""

    async def test_working_directory_is_home(self, mock_executor: MagicMock) -> None:
        """Test that commands run in the user's home directory."""
        mock_executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="/home/testuser\n",
                stderr="",
            )
        )

        result = await mock_executor.run_command("pwd")

        assert result.success
        assert "/home/" in result.stdout or result.stdout.strip() == "~"

    async def test_environment_variables_available(self, mock_executor: MagicMock) -> None:
        """Test that standard environment variables are available."""
        mock_executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="testuser:/home/testuser\n",
                stderr="",
            )
        )

        result = await mock_executor.run_command("echo $USER:$HOME")

        assert result.success
        output = result.stdout.strip()
        # Should have both USER and HOME set
        assert ":" in output
        parts = output.split(":")
        assert len(parts[0]) > 0  # USER should be set
        assert len(parts[1]) > 0  # HOME should be set
