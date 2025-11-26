"""Tests for orchestrator initialization and core functionality."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from pcswitcher.core.config import Configuration
from pcswitcher.core.logging import LogLevel
from pcswitcher.core.orchestrator import Orchestrator
from pcswitcher.core.session import SessionState, SyncSession


def create_mock_remote_executor() -> MagicMock:
    """Create a mock RemoteExecutor for testing."""
    mock = MagicMock()
    mock.run = MagicMock()
    mock.send_file_to_target = MagicMock()
    mock.get_hostname = MagicMock(return_value="test-target")
    return mock


def create_test_config() -> Configuration:
    """Create a test configuration."""
    return Configuration(
        log_file_level=LogLevel.INFO,
        log_cli_level=LogLevel.INFO,
        sync_jobs={"btrfs_snapshots": True},
        job_configs={"btrfs_snapshots": {"subvolumes": ["@", "@home"]}},
        disk={"min_free": 0.20, "reserve_minimum": 0.15, "check_interval": 30},
        config_path=Path("/tmp/test-config.yaml"),
    )


def create_test_session() -> SyncSession:
    """Create a test sync session."""
    return SyncSession(
        id="test-session-123",
        timestamp=datetime.now(UTC),
        source_hostname="source-machine",
        target_hostname="target-machine",
        enabled_jobs=["btrfs_snapshots"],
        state=SessionState.INITIALIZING,
    )


def test_orchestrator_initialization_succeeds() -> None:
    """Test that Orchestrator initializes without raising exceptions.

    This specifically tests that the signal.lock() bug is fixed (replaced with threading.Lock()).
    """
    config = create_test_config()
    remote = create_mock_remote_executor()
    session = create_test_session()

    # This should not raise any exceptions
    orchestrator = Orchestrator(config, remote, session)

    # Verify the orchestrator was initialized correctly
    assert orchestrator.config is config
    assert orchestrator.remote is remote
    assert orchestrator.session is session
    assert orchestrator._interrupt_handler is not None
    assert orchestrator._interrupt_handler._interrupt_lock is not None
    assert isinstance(orchestrator._interrupt_handler._interrupt_lock, type(threading.Lock()))


def test_orchestrator_has_threading_lock() -> None:
    """Test that InterruptHandler uses threading.Lock for interrupt handling."""
    config = create_test_config()
    remote = create_mock_remote_executor()
    session = create_test_session()

    orchestrator = Orchestrator(config, remote, session)

    # Verify it's a threading.Lock (by testing its interface)
    lock = orchestrator._interrupt_handler._interrupt_lock
    assert hasattr(lock, "acquire")
    assert hasattr(lock, "release")
    assert hasattr(lock, "__enter__")
    assert hasattr(lock, "__exit__")


def test_orchestrator_disk_config_uses_correct_location() -> None:
    """Test that orchestrator reads disk config from self.config.disk."""
    config = Configuration(
        log_file_level=LogLevel.INFO,
        log_cli_level=LogLevel.INFO,
        sync_jobs={"btrfs_snapshots": True},
        job_configs={"btrfs_snapshots": {"subvolumes": ["@"]}},
        disk={"min_free": 0.30, "reserve_minimum": 0.10, "check_interval": 60},  # Custom values
        config_path=Path("/tmp/test-config.yaml"),
    )
    remote = create_mock_remote_executor()
    session = create_test_session()

    orchestrator = Orchestrator(config, remote, session)

    # Access the disk config through the orchestrator
    assert orchestrator.config.disk["min_free"] == 0.30
    assert orchestrator.config.disk["reserve_minimum"] == 0.10
    assert orchestrator.config.disk["check_interval"] == 60


def test_orchestrator_signal_handlers_registered() -> None:
    """Test that orchestrator registers signal handlers during initialization."""
    config = create_test_config()
    remote = create_mock_remote_executor()
    session = create_test_session()

    with patch("signal.signal") as mock_signal:
        _ = Orchestrator(config, remote, session)

        # Verify signal handlers were registered
        import signal

        calls = mock_signal.call_args_list
        sigint_registered = any(call[0][0] == signal.SIGINT for call in calls)
        sigterm_registered = any(call[0][0] == signal.SIGTERM for call in calls)

        assert sigint_registered, "SIGINT handler should be registered"
        assert sigterm_registered, "SIGTERM handler should be registered"


def test_orchestrator_jobs_list_initially_empty() -> None:
    """Test that jobs list is empty before loading."""
    config = create_test_config()
    remote = create_mock_remote_executor()
    session = create_test_session()

    orchestrator = Orchestrator(config, remote, session)

    assert orchestrator._job_manager.jobs == []
    assert orchestrator._job_manager.current_job is None


def test_orchestrator_sets_cli_invocation_time() -> None:
    """Test that orchestrator correctly stores CLI invocation time."""
    config = create_test_config()
    remote = create_mock_remote_executor()
    session = create_test_session()

    orchestrator = Orchestrator(config, remote, session)

    test_time = 1234567890.123
    orchestrator.set_cli_invocation_time(test_time)

    assert orchestrator._cli_invocation_time == test_time
