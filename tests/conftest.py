"""Shared test fixtures for pc-switcher tests."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.events import EventBus
from pcswitcher.models import CommandResult


@pytest.fixture(scope="session", autouse=True)
def configure_test_logging() -> None:
    """Configure logging so test module logs show at INFO level.

    pytest's log_cli_level is set to WARNING to suppress verbose library logs
    (e.g., asyncssh). This fixture sets test module loggers to INFO so their
    output is still visible in live logging.
    """
    logging.getLogger().setLevel(logging.WARNING)  # Keep root at WARNING to suppress libs
    logging.getLogger("pcswitcher").setLevel(logging.DEBUG)
    logging.getLogger("tests").setLevel(logging.DEBUG)


@pytest.fixture
def mock_connection() -> MagicMock:
    """Create a mock asyncssh connection."""
    conn = MagicMock()
    conn.run = AsyncMock(
        return_value=MagicMock(
            exit_status=0,
            stdout="output",
            stderr="",
        )
    )
    conn.create_process = AsyncMock()
    conn.start_sftp_client = AsyncMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock()
    return conn


@pytest.fixture
def mock_executor() -> MagicMock:
    """Create a mock executor for testing jobs."""
    executor = MagicMock()
    executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    executor.start_process = AsyncMock()
    executor.terminate_all_processes = AsyncMock()
    return executor


@pytest.fixture
def mock_remote_executor(mock_executor: MagicMock) -> MagicMock:
    """Create a mock remote executor with file transfer methods."""
    mock_executor.send_file = AsyncMock()
    mock_executor.get_file = AsyncMock()
    mock_executor.get_hostname = AsyncMock(return_value="target-host")
    return mock_executor


@pytest.fixture
def mock_event_bus() -> MagicMock:
    """Create a mock EventBus for testing."""
    event_bus = MagicMock(spec=EventBus)
    event_bus.subscribe = MagicMock(return_value=MagicMock())
    event_bus.publish = MagicMock()
    event_bus.close = MagicMock()
    return event_bus


@pytest.fixture
def sample_command_result() -> CommandResult:
    """Create a sample successful command result."""
    return CommandResult(exit_code=0, stdout="success", stderr="")


@pytest.fixture
def failed_command_result() -> CommandResult:
    """Create a sample failed command result."""
    return CommandResult(exit_code=1, stdout="", stderr="error message")
