"""Shared unit test fixtures for pc-switcher tests.

Provides:
- Mock JobContext fixtures (T003)
- Time-freezing fixtures for deterministic timestamp tests (T004)
- Common mock patterns for job testing
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun import freeze_time
from rich.console import Console

from pcswitcher.config import Configuration
from pcswitcher.events import EventBus
from pcswitcher.jobs import JobContext
from pcswitcher.models import CommandResult
from pcswitcher.orchestrator import Orchestrator


@pytest.fixture
def mock_local_executor() -> MagicMock:
    """Create a mock LocalExecutor for testing jobs."""
    executor = MagicMock()
    executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    executor.start_process = AsyncMock()
    executor.terminate_all_processes = AsyncMock()
    return executor


@pytest.fixture
def mock_remote_executor() -> MagicMock:
    """Create a mock RemoteExecutor with file transfer methods."""
    executor = MagicMock()
    executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    executor.start_process = AsyncMock()
    executor.terminate_all_processes = AsyncMock()
    executor.send_file = AsyncMock()
    executor.get_file = AsyncMock()
    executor.get_hostname = AsyncMock(return_value="target-host")
    return executor


@pytest.fixture
def mock_event_bus() -> MagicMock:
    """Create a mock EventBus for testing."""
    event_bus = MagicMock(spec=EventBus)
    event_bus.subscribe = MagicMock(return_value=MagicMock())
    event_bus.publish = MagicMock()
    event_bus.close = MagicMock()
    return event_bus


@pytest.fixture
def mock_job_context(
    mock_local_executor: MagicMock,
    mock_remote_executor: MagicMock,
    mock_event_bus: MagicMock,
) -> JobContext:
    """Create a mock JobContext for testing jobs.

    T003: Shared fixture for mock JobContext.

    This fixture provides a fully mocked JobContext suitable for unit testing
    any job that depends on JobContext. All executors and event_bus are mocks.

    The config is empty by default. Tests requiring specific config values
    should create their own JobContext using the individual mock fixtures.
    """
    return JobContext(
        config={},
        source=mock_local_executor,
        target=mock_remote_executor,
        event_bus=mock_event_bus,
        session_id="test-session-12345678",
        source_hostname="source-host",
        target_hostname="target-host",
    )


@pytest.fixture
def mock_job_context_factory(
    mock_local_executor: MagicMock,
    mock_remote_executor: MagicMock,
    mock_event_bus: MagicMock,
) -> Callable[[dict[str, Any] | None, bool], JobContext]:
    """Factory fixture to create JobContext with custom config.

    Usage:
        def test_something(mock_job_context_factory):
            context = mock_job_context_factory(config={"key": "value"})
            context_dry_run = mock_job_context_factory(config={}, dry_run=True)
    """

    def create_context(config: dict[str, Any] | None = None, dry_run: bool = False) -> JobContext:
        return JobContext(
            config=config or {},
            source=mock_local_executor,
            target=mock_remote_executor,
            event_bus=mock_event_bus,
            session_id="test-session-12345678",
            source_hostname="source-host",
            target_hostname="target-host",
            dry_run=dry_run,
        )

    return create_context


@pytest.fixture
def frozen_time():
    """T004: Time-freezing fixture for deterministic timestamp tests.

    Returns a context manager that freezes time to a known value.

    Usage:
        def test_timestamp(frozen_time):
            with frozen_time:
                # Time is frozen to 2025-01-15T10:30:00Z
                now = datetime.now(UTC)
                assert now.year == 2025
    """
    return freeze_time("2025-01-15T10:30:00Z")


@pytest.fixture
def frozen_datetime() -> datetime:
    """Returns the datetime value used by frozen_time fixture.

    Useful for assertions without having to repeat the timestamp string.
    """
    return datetime.fromisoformat("2025-01-15T10:30:00+00:00")


# Common command results for testing
@pytest.fixture
def success_result() -> CommandResult:
    """A successful command result with empty output."""
    return CommandResult(exit_code=0, stdout="", stderr="")


@pytest.fixture
def failed_result() -> CommandResult:
    """A failed command result with error message."""
    return CommandResult(exit_code=1, stdout="", stderr="error occurred")


# Enough Orchestrator wiring for the real job loop to run
_DF_OUTPUT = (
    "Filesystem     1B-blocks       Used  Available Use% Mounted on\n"
    "/dev/sda1  1000000000000 500000000000 500000000000  50% /\n"
)


@pytest.fixture
def wired_orchestrator() -> Orchestrator:
    """A narrowly-constructed Orchestrator with enough wiring for `_execute_jobs` /
    `_run_jobs_in_task_group` to run: mocked local/remote executors returning valid `df`
    output for the background disk-space monitors, a non-interactive Console, and a
    silenced logger/UI.
    """
    config = MagicMock(spec=Configuration)
    config.logging = MagicMock()
    config.logging.file = 10
    config.logging.tui = 20
    config.logging.external = 30
    config.sync_jobs = {}
    config.job_configs = {}
    config.disk = MagicMock()
    config.disk.preflight_minimum = "20%"
    config.disk.runtime_minimum = "15%"
    config.disk.warning_threshold = "25%"
    config.disk.check_interval = 30
    config.btrfs_snapshots = MagicMock()
    config.btrfs_snapshots.subvolumes = []
    config.btrfs_snapshots.keep_recent = 5
    config.btrfs_snapshots.max_age_days = 30

    orchestrator = Orchestrator(target="target-host", config=config)
    orchestrator._console = Console(file=io.StringIO())  # pyright: ignore[reportPrivateUsage]
    orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    local_executor = MagicMock()
    local_executor.run_command = AsyncMock(return_value=CommandResult(0, _DF_OUTPUT, ""))
    remote_executor = MagicMock()
    remote_executor.run_command = AsyncMock(return_value=CommandResult(0, _DF_OUTPUT, ""))
    orchestrator._local_executor = local_executor  # pyright: ignore[reportPrivateUsage]
    orchestrator._remote_executor = remote_executor  # pyright: ignore[reportPrivateUsage]
    return orchestrator
