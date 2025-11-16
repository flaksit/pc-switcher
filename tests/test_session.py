"""Tests for session management."""

from __future__ import annotations

from datetime import datetime

from pcswitcher.core.session import SessionState, SyncSession, generate_session_id


def test_generate_session_id() -> None:
    """Test session ID generation."""
    session_id = generate_session_id()
    assert isinstance(session_id, str)
    assert len(session_id) == 8
    assert session_id.isalnum()


def test_session_id_uniqueness() -> None:
    """Test that session IDs are unique."""
    id1 = generate_session_id()
    id2 = generate_session_id()
    assert id1 != id2


def test_session_state_transitions() -> None:
    """Test session state transitions."""
    session = SyncSession(
        id=generate_session_id(),
        timestamp=datetime.now(),
        source_hostname="source",
        target_hostname="target",
        enabled_modules=[],
        state=SessionState.INITIALIZING,
    )

    assert session.state == SessionState.INITIALIZING
    assert not session.is_terminal_state()

    session.set_state(SessionState.VALIDATING)
    assert session.state == SessionState.VALIDATING
    assert not session.is_terminal_state()

    session.set_state(SessionState.COMPLETED)
    assert session.state == SessionState.COMPLETED
    assert session.is_terminal_state()


def test_session_terminal_states() -> None:
    """Test terminal state detection."""
    session = SyncSession(
        id=generate_session_id(),
        timestamp=datetime.now(),
        source_hostname="source",
        target_hostname="target",
        enabled_modules=[],
        state=SessionState.INITIALIZING,
    )

    terminal_states = [
        SessionState.COMPLETED,
        SessionState.ABORTED,
        SessionState.FAILED,
    ]

    non_terminal_states = [
        SessionState.INITIALIZING,
        SessionState.VALIDATING,
        SessionState.EXECUTING,
        SessionState.CLEANUP,
    ]

    for state in terminal_states:
        session.set_state(state)
        assert session.is_terminal_state()

    for state in non_terminal_states:
        session.set_state(state)
        assert not session.is_terminal_state()
