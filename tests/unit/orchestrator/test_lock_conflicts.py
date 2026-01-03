"""Unit tests for orchestrator lock conflict error messages.

These tests verify that the orchestrator produces clear, helpful error
messages when lock conflicts occur. The underlying lock mechanism is
tested in tests/unit/test_lock.py; these tests focus on error message
formatting at the orchestrator level.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config import Configuration
from pcswitcher.lock import SyncLock
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


class TestSourceLockConflictMessages:
    """Test error messages when source lock acquisition fails."""

    @pytest.mark.asyncio
    async def test_001_fnd_fr_lock(self, mock_config: MagicMock, tmp_path: Path) -> None:
        """FND-FR-LOCK: Source lock error message includes holder information.

        When source lock acquisition fails because another sync holds the lock,
        the error message should include the holder info for debugging.
        """
        lock_path = tmp_path / ".local/share/pc-switcher/pc-switcher.lock"

        # Pre-acquire the lock to simulate another process holding it
        existing_lock = SyncLock(lock_path)
        existing_holder = "source:other-host:session123"
        assert existing_lock.acquire(existing_holder)

        try:
            # Patch get_lock_path to use our test path
            with patch("pcswitcher.orchestrator.get_lock_path", return_value=lock_path):
                orchestrator = Orchestrator(target="test-target", config=mock_config)

                with pytest.raises(RuntimeError) as exc_info:
                    await orchestrator._acquire_source_lock()  # pyright: ignore[reportPrivateUsage]

                # Verify error message format
                error_msg = str(exc_info.value)
                assert "already involved in a sync" in error_msg
                assert "held by:" in error_msg
                assert existing_holder in error_msg
        finally:
            existing_lock.release()

    @pytest.mark.asyncio
    async def test_source_lock_error_message_format(self, mock_config: MagicMock, tmp_path: Path) -> None:
        """Source lock error follows expected format for user-facing display.

        Error format: "This machine is already involved in a sync (held by: <holder>)"
        """
        lock_path = tmp_path / ".local/share/pc-switcher/pc-switcher.lock"

        # Pre-acquire with specific holder info
        existing_lock = SyncLock(lock_path)
        holder_info = "source:laptop1:abc12345"
        assert existing_lock.acquire(holder_info)

        try:
            with patch("pcswitcher.orchestrator.get_lock_path", return_value=lock_path):
                orchestrator = Orchestrator(target="test-target", config=mock_config)

                with pytest.raises(RuntimeError) as exc_info:
                    await orchestrator._acquire_source_lock()  # pyright: ignore[reportPrivateUsage]

                # Check exact format for user-facing display
                error_msg = str(exc_info.value)
                assert error_msg.startswith("This machine is already involved in a sync")
                assert "(held by:" in error_msg
                assert ")" in error_msg
        finally:
            existing_lock.release()


class TestTargetLockConflictMessages:
    """Test error messages when target lock acquisition fails."""

    @pytest.mark.asyncio
    async def test_target_lock_error_includes_hostname(self, mock_config: MagicMock) -> None:
        """Target lock error message includes the target hostname.

        When target lock acquisition fails, the error message should
        identify which target machine is already in a sync.
        """
        target_hostname = "my-target-host"
        orchestrator = Orchestrator(target=target_hostname, config=mock_config)

        # Mock the remote executor
        mock_executor = AsyncMock()
        orchestrator._remote_executor = mock_executor  # pyright: ignore[reportPrivateUsage]

        # Mock start_persistent_remote_lock to return None (lock failed)
        with patch(
            "pcswitcher.orchestrator.start_persistent_remote_lock",
            return_value=None,
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await orchestrator._acquire_target_lock()  # pyright: ignore[reportPrivateUsage]

            # Verify error message includes target hostname
            error_msg = str(exc_info.value)
            assert target_hostname in error_msg
            assert "already involved in a sync" in error_msg

    @pytest.mark.asyncio
    async def test_target_lock_error_message_format(self, mock_config: MagicMock) -> None:
        """Target lock error follows expected format for user-facing display.

        Error format: "Target <hostname> is already involved in a sync"
        """
        target_hostname = "pc2"
        orchestrator = Orchestrator(target=target_hostname, config=mock_config)

        mock_executor = AsyncMock()
        orchestrator._remote_executor = mock_executor  # pyright: ignore[reportPrivateUsage]

        with patch(
            "pcswitcher.orchestrator.start_persistent_remote_lock",
            return_value=None,
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await orchestrator._acquire_target_lock()  # pyright: ignore[reportPrivateUsage]

            error_msg = str(exc_info.value)
            assert error_msg == f"Target {target_hostname} is already involved in a sync"


class TestLockErrorMessageClarity:
    """Test that lock error messages are clear and actionable."""

    @pytest.mark.asyncio
    async def test_source_lock_error_no_stack_trace_in_message(self, mock_config: MagicMock, tmp_path: Path) -> None:
        """Source lock error is a clean RuntimeError without internal details.

        The error should be suitable for display to users without exposing
        internal implementation details or stack traces in the message.
        """
        lock_path = tmp_path / ".local/share/pc-switcher/pc-switcher.lock"

        existing_lock = SyncLock(lock_path)
        assert existing_lock.acquire("source:test:123")

        try:
            with patch("pcswitcher.orchestrator.get_lock_path", return_value=lock_path):
                orchestrator = Orchestrator(target="test-target", config=mock_config)

                with pytest.raises(RuntimeError) as exc_info:
                    await orchestrator._acquire_source_lock()  # pyright: ignore[reportPrivateUsage]

                # Error should be RuntimeError (not OSError, BlockingIOError, etc.)
                assert exc_info.type is RuntimeError

                # Message should not contain internal details
                error_msg = str(exc_info.value)
                assert "BlockingIOError" not in error_msg
                assert "fcntl" not in error_msg
                assert "Traceback" not in error_msg
        finally:
            existing_lock.release()

    @pytest.mark.asyncio
    async def test_target_lock_error_no_stack_trace_in_message(self, mock_config: MagicMock) -> None:
        """Target lock error is a clean RuntimeError without internal details."""
        orchestrator = Orchestrator(target="test-target", config=mock_config)
        orchestrator._remote_executor = AsyncMock()  # pyright: ignore[reportPrivateUsage]

        with patch(
            "pcswitcher.orchestrator.start_persistent_remote_lock",
            return_value=None,
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await orchestrator._acquire_target_lock()  # pyright: ignore[reportPrivateUsage]

            assert exc_info.type is RuntimeError

            error_msg = str(exc_info.value)
            assert "SSH" not in error_msg
            assert "asyncssh" not in error_msg
            assert "Traceback" not in error_msg
