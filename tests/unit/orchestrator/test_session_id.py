"""Unit tests for session ID generation and consistency.

These tests verify that session IDs are generated correctly and used
consistently throughout the sync process (logs, snapshots, holder info).
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from pcswitcher.btrfs_snapshots import session_folder_name
from pcswitcher.config import Configuration
from pcswitcher.logger import generate_log_filename
from pcswitcher.orchestrator import Orchestrator


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock Configuration for orchestrator initialization."""
    config = MagicMock(spec=Configuration)
    config.logging = MagicMock()
    config.logging.file = 10  # DEBUG
    config.logging.tui = 20  # INFO
    config.logging.external = 30  # WARNING
    config.sync_jobs = {}
    config.job_configs = {}
    config.btrfs_snapshots = MagicMock()
    config.btrfs_snapshots.subvolumes = ["@", "@home"]
    config.disk = MagicMock()
    config.disk.preflight_minimum = "10%"
    return config


class TestSessionIdFormat:
    """Test session ID format requirements."""

    def test_session_id_is_8_char_hex(self, mock_config: MagicMock) -> None:
        """Session ID should be 8 characters of hexadecimal.

        secrets.token_hex(4) produces 8 hex characters (4 bytes = 8 hex chars).
        """
        orchestrator = Orchestrator(target="test-target", config=mock_config)

        session_id = orchestrator._session_id  # pyright: ignore[reportPrivateUsage]

        # Verify length
        assert len(session_id) == 8, f"Session ID should be 8 chars, got {len(session_id)}"

        # Verify hex format (only 0-9 and a-f)
        assert re.match(r"^[0-9a-f]{8}$", session_id), f"Session ID should be lowercase hex, got '{session_id}'"

    def test_session_id_is_unique_per_orchestrator(self, mock_config: MagicMock) -> None:
        """Each orchestrator instance should have a unique session ID."""
        orchestrator1 = Orchestrator(target="target1", config=mock_config)
        orchestrator2 = Orchestrator(target="target2", config=mock_config)

        # pyright: ignore[reportPrivateUsage]
        assert orchestrator1._session_id != orchestrator2._session_id  # pyright: ignore[reportPrivateUsage]


class TestSessionIdInLockHolderInfo:
    """Test that session ID is included in lock holder information."""

    def test_source_lock_holder_includes_session_id(self, mock_config: MagicMock) -> None:
        """Source lock holder info should include the session ID.

        Format: "source:<hostname>:<session_id>"
        """
        orchestrator = Orchestrator(target="test-target", config=mock_config)
        session_id = orchestrator._session_id  # pyright: ignore[reportPrivateUsage]

        # The holder_info format used in _acquire_source_lock
        expected_holder = f"source:{orchestrator._source_hostname}:{session_id}"  # pyright: ignore[reportPrivateUsage]

        # Verify session_id is part of the holder info format
        assert session_id in expected_holder

    def test_target_lock_holder_includes_session_id(self, mock_config: MagicMock) -> None:
        """Target lock holder info should include the session ID.

        Format: "target:<source_hostname>:<session_id>"
        """
        orchestrator = Orchestrator(target="test-target", config=mock_config)
        session_id = orchestrator._session_id  # pyright: ignore[reportPrivateUsage]

        # The holder_info format used in start_persistent_remote_lock
        expected_holder = f"target:{orchestrator._source_hostname}:{session_id}"  # pyright: ignore[reportPrivateUsage]

        # Verify session_id is part of the holder info format
        assert session_id in expected_holder


class TestSessionFolderNaming:
    """Test that session ID is used in snapshot folder naming."""

    def test_session_folder_name_includes_session_id(self) -> None:
        """Session folder name should include the session ID.

        Format: "YYYYMMDDTHHMMSS-<session_id>"
        """
        session_id = "abc12345"
        folder_name = session_folder_name(session_id)

        # Verify session_id is in the folder name
        assert session_id in folder_name

        # Verify format: timestamp followed by session_id
        # Pattern: YYYYMMDDTHHMMSS-<session_id>
        pattern = r"^\d{8}T\d{6}-" + re.escape(session_id) + r"$"
        assert re.match(pattern, folder_name), f"Folder name '{folder_name}' doesn't match expected pattern"

    def test_orchestrator_session_folder_uses_session_id(self, mock_config: MagicMock) -> None:
        """Orchestrator's session folder should incorporate its session ID."""
        orchestrator = Orchestrator(target="test-target", config=mock_config)

        session_id = orchestrator._session_id  # pyright: ignore[reportPrivateUsage]
        session_folder = orchestrator._session_folder  # pyright: ignore[reportPrivateUsage]

        assert session_id in session_folder


class TestSessionIdInJobContext:
    """Test that session ID is passed to jobs via JobContext."""

    def test_job_context_receives_session_id(self, mock_config: MagicMock) -> None:
        """JobContext created by orchestrator should include session ID."""
        orchestrator = Orchestrator(target="test-target", config=mock_config)

        # Mock executors needed for _create_job_context
        orchestrator._local_executor = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._remote_executor = MagicMock()  # pyright: ignore[reportPrivateUsage]

        context = orchestrator._create_job_context({"key": "value"})  # pyright: ignore[reportPrivateUsage]

        assert context.session_id == orchestrator._session_id  # pyright: ignore[reportPrivateUsage]


class TestSessionIdInLogFileName:
    """Test that session ID is used in log file naming."""

    def test_log_filename_includes_session_id(self) -> None:
        """Log filename should include the session ID for traceability.

        Format: sync-YYYYMMDDTHHMMSS-<session_id>.log
        """
        session_id = "abc12345"
        filename = generate_log_filename(session_id)

        # Verify session_id is in the filename
        assert session_id in filename

        # Verify format
        assert filename.startswith("sync-")
        assert filename.endswith(f"-{session_id}.log")


class TestSessionIdConsistencyAcrossComponents:
    """Test that the same session ID is used across all components."""

    def test_same_session_id_in_folder_and_context(self, mock_config: MagicMock) -> None:
        """Session folder and job context should use the same session ID."""
        orchestrator = Orchestrator(target="test-target", config=mock_config)
        orchestrator._local_executor = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._remote_executor = MagicMock()  # pyright: ignore[reportPrivateUsage]

        session_id = orchestrator._session_id  # pyright: ignore[reportPrivateUsage]
        context = orchestrator._create_job_context({})  # pyright: ignore[reportPrivateUsage]

        # Both should reference the same session_id
        assert context.session_id == session_id
        assert session_id in orchestrator._session_folder  # pyright: ignore[reportPrivateUsage]
