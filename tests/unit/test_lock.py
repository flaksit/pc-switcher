"""Unit tests for the lock module."""

from __future__ import annotations

from pathlib import Path

import pytest

from pcswitcher.lock import LOCK_FILE_NAME, SyncLock, get_lock_path


class TestLockPath:
    """Tests for lock path utilities."""

    def test_lock_file_name_is_unified(self) -> None:
        """LOCK_FILE_NAME should be a single unified name."""
        assert LOCK_FILE_NAME == "pc-switcher.lock"

    def test_get_lock_path_returns_correct_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_lock_path() should return path in .local/share/pc-switcher."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        expected = tmp_path / ".local/share/pc-switcher" / LOCK_FILE_NAME
        assert get_lock_path() == expected


class TestSyncLock:
    """Tests for SyncLock class."""

    def test_acquire_creates_lock_file(self, tmp_path: Path) -> None:
        """acquire() should create the lock file."""
        lock_path = tmp_path / "test.lock"
        lock = SyncLock(lock_path)
        assert lock.acquire("test-holder")
        assert lock_path.exists()
        lock.release()

    def test_acquire_writes_holder_info(self, tmp_path: Path) -> None:
        """acquire() should write holder info to lock file."""
        lock_path = tmp_path / "test.lock"
        lock = SyncLock(lock_path)
        assert lock.acquire("source:myhost:abc123")
        assert lock_path.read_text() == "source:myhost:abc123"
        lock.release()

    def test_acquire_fails_when_already_held(self, tmp_path: Path) -> None:
        """acquire() should fail when lock is held by another process."""
        lock_path = tmp_path / "test.lock"
        lock1 = SyncLock(lock_path)
        lock2 = SyncLock(lock_path)
        assert lock1.acquire("holder-1")
        assert not lock2.acquire("holder-2")
        lock1.release()

    def test_get_holder_info_returns_content(self, tmp_path: Path) -> None:
        """get_holder_info() should return the holder info."""
        lock_path = tmp_path / "test.lock"
        lock = SyncLock(lock_path)
        lock.acquire("source:myhost:session123")
        assert lock.get_holder_info() == "source:myhost:session123"
        lock.release()

    def test_get_holder_info_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        """get_holder_info() should return None if lock file doesn't exist."""
        lock_path = tmp_path / "nonexistent.lock"
        lock = SyncLock(lock_path)
        assert lock.get_holder_info() is None

    def test_release_allows_reacquisition(self, tmp_path: Path) -> None:
        """release() should allow another process to acquire the lock."""
        lock_path = tmp_path / "test.lock"
        lock1 = SyncLock(lock_path)
        lock2 = SyncLock(lock_path)
        assert lock1.acquire("holder-1")
        lock1.release()
        assert lock2.acquire("holder-2")
        lock2.release()

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        """release() should be safe to call multiple times."""
        lock_path = tmp_path / "test.lock"
        lock = SyncLock(lock_path)
        lock.acquire("holder")
        lock.release()
        lock.release()  # Should not raise

    def test_unified_lock_prevents_concurrent_roles(self, tmp_path: Path) -> None:
        """Same lock file should prevent a machine from being both source and target."""
        lock_path = tmp_path / "pc-switcher.lock"

        # Simulate source acquiring lock
        source_lock = SyncLock(lock_path)
        assert source_lock.acquire("source:hostA:session1")

        # Simulate remote process trying to acquire same lock (as if targeting this machine)
        target_lock = SyncLock(lock_path)
        assert not target_lock.acquire("target:hostB:session2")

        # After release, target can acquire
        source_lock.release()
        assert target_lock.acquire("target:hostB:session2")
        target_lock.release()

    def test_001_fnd_fr_lock(self, tmp_path: Path) -> None:
        """FND-FR-LOCK: Locking prevents concurrent execution.

        System MUST implement locking mechanism to prevent concurrent sync executions.
        When a sync operation holds the lock, any attempt to start another sync should
        fail to acquire the lock, preventing concurrent operations.
        """
        lock_path = tmp_path / "pc-switcher.lock"

        # First sync acquires lock
        sync1_lock = SyncLock(lock_path)
        assert sync1_lock.acquire("source:laptop1:sync1")

        # Second sync attempt should fail to acquire lock
        sync2_lock = SyncLock(lock_path)
        assert not sync2_lock.acquire("source:laptop2:sync2")

        # Lock holder info should show first sync
        assert sync1_lock.get_holder_info() == "source:laptop1:sync1"

        # After first sync releases, second sync can acquire
        sync1_lock.release()
        assert sync2_lock.acquire("source:laptop2:sync2")
        sync2_lock.release()

    def test_001_edge_concurrent_sync_attempts(self, tmp_path: Path) -> None:
        """Edge case: concurrent sync attempts are blocked.

        When multiple processes attempt to acquire the lock simultaneously,
        only one should succeed while all others fail gracefully without
        corrupting the lock state.
        """
        lock_path = tmp_path / "pc-switcher.lock"

        # First process acquires lock
        lock1 = SyncLock(lock_path)
        assert lock1.acquire("sync1:hostA:session1")

        # Multiple concurrent attempts should all fail
        lock2 = SyncLock(lock_path)
        lock3 = SyncLock(lock_path)
        lock4 = SyncLock(lock_path)

        assert not lock2.acquire("sync2:hostB:session2")
        assert not lock3.acquire("sync3:hostC:session3")
        assert not lock4.acquire("sync4:hostD:session4")

        # Lock holder should still be the first process
        assert lock1.get_holder_info() == "sync1:hostA:session1"

        # After release, one of the waiting processes can acquire
        lock1.release()
        assert lock2.acquire("sync2:hostB:session2")

        # Now lock3 and lock4 should still fail
        assert not lock3.acquire("sync3:hostC:session3")
        assert not lock4.acquire("sync4:hostD:session4")

        # Clean up
        lock2.release()
