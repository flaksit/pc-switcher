"""Tests for version management and installation."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from pcswitcher.remote.installer import InstallationError, VersionManager


def create_mock_remote_executor() -> MagicMock:
    """Create a mock RemoteExecutor for testing."""
    mock = MagicMock()
    mock.run = MagicMock()
    mock.send_file_to_target = MagicMock()
    mock.get_hostname = MagicMock(return_value="test-target")
    return mock


def test_version_comparison() -> None:
    """Test version comparison logic."""
    mock_remote = create_mock_remote_executor()
    manager = VersionManager(mock_remote)

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
    mock_remote = create_mock_remote_executor()
    manager = VersionManager(mock_remote)

    # Shorter version should be treated as having zeros
    assert manager.compare_versions("1.0", "1.0.0") == 0
    assert manager.compare_versions("1.0", "1.0.1") == -1
    assert manager.compare_versions("1.1", "1.0.0") == 1


def test_version_comparison_edge_cases() -> None:
    """Test version comparison with edge cases."""
    mock_remote = create_mock_remote_executor()
    manager = VersionManager(mock_remote)

    # Pre-release versions
    assert manager.compare_versions("1.0.0-alpha", "1.0.0") == 0  # Ignores non-numeric parts
    assert manager.compare_versions("1.0.0-1", "1.0.0-2") == -1
    assert manager.compare_versions("2.0.0-beta", "1.9.9") == 1


def test_ensure_version_sync_missing_local() -> None:
    """Test that missing local version raises InstallationError."""
    mock_remote = create_mock_remote_executor()
    manager = VersionManager(mock_remote)

    with pytest.raises(InstallationError, match="not installed on source"):
        manager.ensure_version_sync(None, "1.0.0")


def test_ensure_version_sync_target_newer_than_source() -> None:
    """Test that target newer than source raises InstallationError (downgrade prevention)."""
    mock_remote = create_mock_remote_executor()
    manager = VersionManager(mock_remote)

    with pytest.raises(InstallationError, match="newer than source"):
        manager.ensure_version_sync("1.0.0", "2.0.0")


def test_ensure_version_sync_matching_versions() -> None:
    """Test that matching versions result in no installation action."""
    mock_remote = create_mock_remote_executor()
    manager = VersionManager(mock_remote)

    # Should not raise and should not call install methods
    manager.ensure_version_sync("1.0.0", "1.0.0")

    # Verify no installation was attempted
    mock_remote.run.assert_not_called()


def test_ensure_version_sync_target_missing() -> None:
    """Test that missing target version triggers installation."""
    mock_remote = create_mock_remote_executor()

    # Setup mock responses for installation flow
    def run_side_effect(cmd: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""

        if "command -v uv" in cmd:
            result.returncode = 0  # uv is available
        elif "uv tool install" in cmd:
            result.returncode = 0  # Installation succeeds
        return result

    mock_remote.run.side_effect = run_side_effect

    manager = VersionManager(mock_remote)
    manager.ensure_version_sync("1.0.0", None)

    # Verify installation was attempted using Git URL
    calls = [call[0][0] for call in mock_remote.run.call_args_list]
    assert any("uv tool install git+" in call and "@v1.0.0" in call for call in calls)


def test_ensure_version_sync_target_older() -> None:
    """Test that older target version triggers upgrade."""
    mock_remote = create_mock_remote_executor()

    def run_side_effect(cmd: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""

        if "command -v uv" in cmd:
            result.returncode = 0
        elif "uv tool install" in cmd:
            result.returncode = 0
        return result

    mock_remote.run.side_effect = run_side_effect

    manager = VersionManager(mock_remote)
    manager.ensure_version_sync("2.0.0", "1.0.0")

    # Verify upgrade was attempted (install with Git URL replaces existing)
    calls = [call[0][0] for call in mock_remote.run.call_args_list]
    assert any("uv tool install git+" in call and "@v2.0.0" in call for call in calls)


def test_get_target_version_not_installed() -> None:
    """Test detection of pc-switcher not installed on target."""
    mock_remote = create_mock_remote_executor()

    def run_side_effect(cmd: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.stdout = ""
        result.stderr = ""

        if "command -v uv" in cmd:
            result.returncode = 1  # uv not found
        return result

    mock_remote.run.side_effect = run_side_effect

    manager = VersionManager(mock_remote)
    version = manager.get_target_version()

    assert version is None


def test_get_target_version_installed() -> None:
    """Test detection of pc-switcher installed on target."""
    mock_remote = create_mock_remote_executor()

    def run_side_effect(cmd: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.stderr = ""

        if "command -v uv" in cmd:
            result.returncode = 0
            result.stdout = "/home/user/.local/bin/uv"
        elif "uv tool list" in cmd:
            result.returncode = 0
            result.stdout = "pc-switcher v1.2.3\n"
        return result

    mock_remote.run.side_effect = run_side_effect

    manager = VersionManager(mock_remote)
    version = manager.get_target_version()

    assert version == "1.2.3"


def test_get_target_version_with_version_prefix() -> None:
    """Test that version prefix 'v' is correctly stripped."""
    mock_remote = create_mock_remote_executor()

    def run_side_effect(cmd: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.stderr = ""

        if "command -v uv" in cmd:
            result.returncode = 0
            result.stdout = "/usr/bin/uv"
        elif "uv tool list" in cmd:
            result.returncode = 0
            result.stdout = "pc-switcher v0.4.0\nother-tool v2.0.0\n"
        return result

    mock_remote.run.side_effect = run_side_effect

    manager = VersionManager(mock_remote)
    version = manager.get_target_version()

    assert version == "0.4.0"


def test_install_ensures_uv_available() -> None:
    """Test that installation first ensures uv is installed on target."""
    mock_remote = create_mock_remote_executor()
    install_calls: list[str] = []

    def run_side_effect(cmd: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        install_calls.append(cmd)
        result = MagicMock(spec=subprocess.CompletedProcess)
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""

        if "curl" in cmd and "astral.sh" in cmd:
            # uv installation
            result.returncode = 0
        elif "~/.local/bin/uv --version" in cmd:
            result.stdout = "uv 0.4.0"

        return result

    mock_remote.run.side_effect = run_side_effect

    manager = VersionManager(mock_remote)
    # Simulate uv not available initially, then install succeeds
    with patch.object(manager, "_ensure_uv_on_target") as mock_ensure:
        manager.install_on_target("1.0.0")
        mock_ensure.assert_called_once()


def test_version_manager_accepts_session_id() -> None:
    """Test that VersionManager correctly accepts session_id for logging."""
    mock_remote = create_mock_remote_executor()

    # Should not raise
    manager = VersionManager(mock_remote, session_id="test-session-123")
    assert manager._logger is not None


def test_installation_failure_raises_error() -> None:
    """Test that installation failure raises InstallationError."""
    mock_remote = create_mock_remote_executor()

    def run_side_effect(cmd: str, timeout: float = 0) -> subprocess.CompletedProcess[str]:
        result = MagicMock(spec=subprocess.CompletedProcess)

        if "command -v uv" in cmd:
            result.returncode = 0
            result.stdout = "/usr/bin/uv"
            result.stderr = ""
        elif "uv tool install" in cmd:
            result.returncode = 1
            result.stdout = ""
            result.stderr = "ERROR: Package not found"
        return result

    mock_remote.run.side_effect = run_side_effect

    manager = VersionManager(mock_remote)

    with pytest.raises(InstallationError, match="Installation failed"):
        manager.install_on_target("1.0.0")
