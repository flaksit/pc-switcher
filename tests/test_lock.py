"""Tests for lock management."""

from __future__ import annotations

import os
import tempfile

import pytest

from pcswitcher.utils.lock import LockError, LockManager


def test_lock_manager_acquire_release() -> None:
    """Test basic lock acquisition and release."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Override XDG_RUNTIME_DIR for testing
        old_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmpdir

        try:
            session_id = "test123"
            manager = LockManager(session_id)

            assert not manager.check_lock_exists()

            manager.acquire_lock(interactive=False)
            assert manager.check_lock_exists()

            manager.release_lock()
            assert not manager.check_lock_exists()
        finally:
            if old_xdg is not None:
                os.environ["XDG_RUNTIME_DIR"] = old_xdg
            else:
                os.environ.pop("XDG_RUNTIME_DIR", None)


def test_lock_manager_context_manager() -> None:
    """Test lock manager as context manager."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmpdir

        try:
            session_id = "test456"
            manager = LockManager(session_id)

            with manager:
                assert manager.check_lock_exists()

            assert not manager.check_lock_exists()
        finally:
            if old_xdg is not None:
                os.environ["XDG_RUNTIME_DIR"] = old_xdg
            else:
                os.environ.pop("XDG_RUNTIME_DIR", None)


def test_lock_manager_concurrent_lock() -> None:
    """Test that concurrent lock acquisition fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old_xdg = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = tmpdir

        try:
            session_id1 = "test789"
            session_id2 = "test012"

            manager1 = LockManager(session_id1)
            manager2 = LockManager(session_id2)

            manager1.acquire_lock(interactive=False)

            with pytest.raises(LockError):
                manager2.acquire_lock(interactive=False)

            manager1.release_lock()
        finally:
            if old_xdg is not None:
                os.environ["XDG_RUNTIME_DIR"] = old_xdg
            else:
                os.environ.pop("XDG_RUNTIME_DIR", None)
