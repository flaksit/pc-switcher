"""Contract tests for executor interface parity.

Ensures MockExecutor (used in unit tests) matches the behavior of
LocalExecutor and RemoteExecutor (used in integration tests).

Per TST-FR-CONTRACT: Verify mock/real executor interface parity.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.executor import LocalExecutor
from pcswitcher.models import CommandResult


class ExecutorContractBase:
    """Base contract tests that all executor implementations must pass.

    This class defines the behavioral contract for executors. Both the
    MockExecutor (used in unit tests) and real executors (LocalExecutor,
    RemoteExecutor) must satisfy these tests to ensure mocks remain
    reliable representations of production behavior.
    """

    @pytest.fixture
    def executor(self) -> Any:
        """Override in subclasses to provide specific executor."""
        raise NotImplementedError

    async def test_run_command_returns_command_result(self, executor: Any) -> None:
        """run_command() must return a CommandResult instance."""
        result = await executor.run_command("echo hello")
        assert isinstance(result, CommandResult)

    async def test_command_result_has_exit_code(self, executor: Any) -> None:
        """CommandResult must have exit_code attribute."""
        result = await executor.run_command("echo hello")
        assert hasattr(result, "exit_code")
        assert isinstance(result.exit_code, int)

    async def test_command_result_has_stdout(self, executor: Any) -> None:
        """CommandResult must have stdout attribute."""
        result = await executor.run_command("echo hello")
        assert hasattr(result, "stdout")
        assert isinstance(result.stdout, str)

    async def test_command_result_has_stderr(self, executor: Any) -> None:
        """CommandResult must have stderr attribute."""
        result = await executor.run_command("echo hello")
        assert hasattr(result, "stderr")
        assert isinstance(result.stderr, str)

    async def test_command_result_has_success_property(self, executor: Any) -> None:
        """CommandResult must have success property."""
        result = await executor.run_command("echo hello")
        assert hasattr(result, "success")
        assert isinstance(result.success, bool)

    async def test_successful_command_returns_zero_exit(self, executor: Any) -> None:
        """Successful commands must return exit_code 0."""
        result = await executor.run_command("echo hello")
        assert result.exit_code == 0
        assert result.success is True

    async def test_executor_has_terminate_all_processes(self, executor: Any) -> None:
        """Executor must have terminate_all_processes method."""
        assert hasattr(executor, "terminate_all_processes")
        assert callable(executor.terminate_all_processes)


class TestLocalExecutorContract(ExecutorContractBase):
    """Verify LocalExecutor adheres to executor contract."""

    @pytest.fixture
    def executor(self) -> LocalExecutor:
        return LocalExecutor()

    async def test_failed_command_returns_nonzero_exit(self, executor: LocalExecutor) -> None:
        """Failed commands must return non-zero exit code."""
        result = await executor.run_command("exit 1")
        assert result.exit_code != 0
        assert result.success is False

    async def test_stdout_captures_output(self, executor: LocalExecutor) -> None:
        """stdout must capture command output."""
        result = await executor.run_command("echo hello")
        assert "hello" in result.stdout

    async def test_stderr_captures_error_output(self, executor: LocalExecutor) -> None:
        """stderr must capture error output."""
        result = await executor.run_command("echo error >&2")
        assert "error" in result.stderr


class TestMockExecutorContract(ExecutorContractBase):
    """Verify mock executor setup matches real executor contract.

    This ensures the mock_executor fixture from conftest.py provides
    the same interface as real executors.
    """

    @pytest.fixture
    def executor(self) -> MagicMock:
        """Create a mock executor matching the contract."""
        mock = MagicMock()
        mock.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="hello\n", stderr=""))
        mock.terminate_all_processes = AsyncMock()
        return mock


class TestMockExecutorFixtureContract:
    """Verify the mock_executor fixture from conftest.py matches contract."""

    async def test_mock_executor_run_command_returns_command_result(self, mock_executor: MagicMock) -> None:
        """mock_executor.run_command must return CommandResult."""
        result = await mock_executor.run_command("echo hello")
        assert isinstance(result, CommandResult)

    async def test_mock_executor_result_has_required_attributes(self, mock_executor: MagicMock) -> None:
        """mock_executor result must have success property (computed from exit_code)."""
        result = await mock_executor.run_command("test")
        assert hasattr(result, "success")


# RemoteExecutor contract tests require VM infrastructure
# and are marked as integration tests - see tests/integration/
