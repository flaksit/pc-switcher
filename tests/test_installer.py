"""Tests for version management and installation."""

from __future__ import annotations

from unittest.mock import MagicMock

from pcswitcher.remote.installer import VersionManager


def test_version_comparison() -> None:
    """Test version comparison logic."""
    mock_connection = MagicMock()
    manager = VersionManager(mock_connection)

    # Equal versions
    assert manager.compare_versions("1.0.0", "1.0.0") == 0
    assert manager.compare_versions("0.1.2", "0.1.2") == 0

    # First version greater
    assert manager.compare_versions("2.0.0", "1.0.0") == 1
    assert manager.compare_versions("1.1.0", "1.0.0") == 1
    assert manager.compare_versions("1.0.1", "1.0.0") == 1

    # First version less
    assert manager.compare_versions("1.0.0", "2.0.0") == -1
    assert manager.compare_versions("1.0.0", "1.1.0") == -1
    assert manager.compare_versions("1.0.0", "1.0.1") == -1


def test_version_comparison_different_lengths() -> None:
    """Test version comparison with different component lengths."""
    mock_connection = MagicMock()
    manager = VersionManager(mock_connection)

    # Shorter version should be treated as having zeros
    assert manager.compare_versions("1.0", "1.0.0") == 0
    assert manager.compare_versions("1.0", "1.0.1") == -1
    assert manager.compare_versions("1.1", "1.0.0") == 1
