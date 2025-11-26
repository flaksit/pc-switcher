"""Tests for signal handling."""

from __future__ import annotations

import signal
from datetime import datetime
from unittest.mock import MagicMock

from pcswitcher.core.session import SessionState, SyncSession, generate_session_id
from pcswitcher.core.signals import InterruptHandler, install_signal_handlers


def test_interrupt_handler_initialization() -> None:
    """Test interrupt handler initialization."""
    session = SyncSession(
        id=generate_session_id(),
        timestamp=datetime.now(),
        source_hostname="source",
        target_hostname="target",
        enabled_jobs=[],
        state=SessionState.INITIALIZING,
    )

    handler = InterruptHandler(session)
    assert not handler.is_interrupted()
    assert session.abort_requested is False


def test_interrupt_handler_first_interrupt() -> None:
    """Test first interrupt sets abort flag."""
    session = SyncSession(
        id=generate_session_id(),
        timestamp=datetime.now(),
        source_hostname="source",
        target_hostname="target",
        enabled_jobs=[],
        state=SessionState.INITIALIZING,
    )

    # Create mock job
    mock_job = MagicMock()
    mock_job.abort = MagicMock()

    handler = InterruptHandler(session)
    handler.set_current_job(mock_job)

    # Simulate interrupt
    handler.handle_interrupt(signal.SIGINT, None)

    assert handler.is_interrupted()
    assert session.abort_requested is True
    mock_job.abort.assert_called_once_with(timeout=5.0)


def test_install_signal_handlers() -> None:
    """Test signal handler installation."""
    session = SyncSession(
        id=generate_session_id(),
        timestamp=datetime.now(),
        source_hostname="source",
        target_hostname="target",
        enabled_jobs=[],
        state=SessionState.INITIALIZING,
    )

    handler = install_signal_handlers(session)
    assert handler is not None

    # Cleanup
    handler.cleanup()
