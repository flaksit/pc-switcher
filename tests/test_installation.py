"""Tests for installation module."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.executor import RemoteExecutor
from pcswitcher.installation import (
    InstallationError,
    check_and_install,
    compare_versions,
    get_current_version,
    get_target_version,
    install_on_target,
)
from pcswitcher.models import CommandResult


class TestGetCurrentVersion:
    """Tests for get_current_version()."""

    def test_get_current_version_success(self) -> None:
        """Should return version from package metadata."""
        with patch("pcswitcher.installation.version") as mock_version:
            mock_version.return_value = "1.2.3"
            result = get_current_version()
            assert result == "1.2.3"
            mock_version.assert_called_once_with("pcswitcher")

    def test_get_current_version_package_not_found(self) -> None:
        """Should raise InstallationError if package not found."""
        with patch("pcswitcher.installation.version") as mock_version:
            mock_version.side_effect = PackageNotFoundError("pcswitcher")

            with pytest.raises(InstallationError) as exc_info:
                get_current_version()

            assert "Cannot determine pc-switcher version" in str(exc_info.value)
            assert "Package metadata not found" in str(exc_info.value)


class TestGetTargetVersion:
    """Tests for get_target_version()."""

    @pytest.mark.asyncio
    async def test_get_target_version_installed(self) -> None:
        """Should return version when pc-switcher is installed."""
        executor = MagicMock(spec=RemoteExecutor)
        executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="pc-switcher version 1.2.3\n",
                stderr="",
            )
        )

        result = await get_target_version(executor)

        assert result == "1.2.3"
        executor.run_command.assert_called_once_with("pc-switcher --version")

    @pytest.mark.asyncio
    async def test_get_target_version_different_format(self) -> None:
        """Should parse version from different output formats."""
        executor = MagicMock(spec=RemoteExecutor)
        executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="0.1.0\n",
                stderr="",
            )
        )

        result = await get_target_version(executor)

        assert result == "0.1.0"

    @pytest.mark.asyncio
    async def test_get_target_version_dev_version(self) -> None:
        """Should parse dev versions correctly."""
        executor = MagicMock(spec=RemoteExecutor)
        executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="1.2.3.dev0\n",
                stderr="",
            )
        )

        result = await get_target_version(executor)

        assert result == "1.2.3.dev0"

    @pytest.mark.asyncio
    async def test_get_target_version_not_installed(self) -> None:
        """Should return None when pc-switcher not installed."""
        executor = MagicMock(spec=RemoteExecutor)
        executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=127,
                stdout="",
                stderr="pc-switcher: command not found\n",
            )
        )

        result = await get_target_version(executor)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_target_version_no_version_in_output(self) -> None:
        """Should return None if version cannot be parsed."""
        executor = MagicMock(spec=RemoteExecutor)
        executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="some error message\n",
                stderr="",
            )
        )

        result = await get_target_version(executor)

        assert result is None


class TestCompareVersions:
    """Tests for compare_versions()."""

    def test_compare_versions_source_newer(self) -> None:
        """Should return 1 when source is newer than target."""
        assert compare_versions("1.2.3", "1.2.2") == 1
        assert compare_versions("2.0.0", "1.9.9") == 1
        assert compare_versions("1.2.3", "1.2.3.dev0") == 1

    def test_compare_versions_equal(self) -> None:
        """Should return 0 when versions are equal."""
        assert compare_versions("1.2.3", "1.2.3") == 0
        assert compare_versions("0.0.1", "0.0.1") == 0

    def test_compare_versions_target_newer(self) -> None:
        """Should return -1 when target is newer than source."""
        assert compare_versions("1.2.2", "1.2.3") == -1
        assert compare_versions("1.9.9", "2.0.0") == -1
        assert compare_versions("1.2.3.dev0", "1.2.3") == -1

    def test_compare_versions_invalid_source(self) -> None:
        """Should raise InstallationError for invalid source version."""
        with pytest.raises(InstallationError) as exc_info:
            compare_versions("invalid", "1.2.3")

        assert "Invalid version string" in str(exc_info.value)
        assert "source='invalid'" in str(exc_info.value)

    def test_compare_versions_invalid_target(self) -> None:
        """Should raise InstallationError for invalid target version."""
        with pytest.raises(InstallationError) as exc_info:
            compare_versions("1.2.3", "not-a-version")

        assert "Invalid version string" in str(exc_info.value)
        assert "target='not-a-version'" in str(exc_info.value)


class TestInstallOnTarget:
    """Tests for install_on_target()."""

    @pytest.mark.asyncio
    async def test_install_on_target_success(self) -> None:
        """Should install pc-switcher on target."""
        executor = MagicMock(spec=RemoteExecutor)
        executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=0,
                stdout="Successfully installed pcswitcher-1.2.3\n",
                stderr="",
            )
        )

        await install_on_target(executor, "1.2.3")

        executor.run_command.assert_called_once_with(
            "uv tool install pcswitcher==1.2.3",
            timeout=300.0,
        )

    @pytest.mark.asyncio
    async def test_install_on_target_failure(self) -> None:
        """Should raise InstallationError on installation failure."""
        executor = MagicMock(spec=RemoteExecutor)
        executor.run_command = AsyncMock(
            return_value=CommandResult(
                exit_code=1,
                stdout="",
                stderr="ERROR: Could not find version 1.2.3\n",
            )
        )

        with pytest.raises(InstallationError) as exc_info:
            await install_on_target(executor, "1.2.3")

        error_msg = str(exc_info.value)
        assert "Failed to install pc-switcher 1.2.3" in error_msg
        assert "Exit code: 1" in error_msg
        assert "Could not find version 1.2.3" in error_msg


class TestCheckAndInstall:
    """Tests for check_and_install()."""

    @pytest.mark.asyncio
    async def test_check_and_install_target_not_installed(self) -> None:
        """Should install when target has no pc-switcher."""
        executor = MagicMock(spec=RemoteExecutor)

        with (
            patch("pcswitcher.installation.get_current_version") as mock_current,
            patch("pcswitcher.installation.get_target_version") as mock_target,
            patch("pcswitcher.installation.install_on_target") as mock_install,
        ):
            mock_current.return_value = "1.2.3"
            mock_target.return_value = None
            mock_install.return_value = None

            await check_and_install(executor)

            mock_install.assert_called_once_with(executor, "1.2.3")

    @pytest.mark.asyncio
    async def test_check_and_install_same_version(self) -> None:
        """Should do nothing when versions match."""
        executor = MagicMock(spec=RemoteExecutor)

        with (
            patch("pcswitcher.installation.get_current_version") as mock_current,
            patch("pcswitcher.installation.get_target_version") as mock_target,
            patch("pcswitcher.installation.install_on_target") as mock_install,
        ):
            mock_current.return_value = "1.2.3"
            mock_target.return_value = "1.2.3"

            await check_and_install(executor)

            mock_install.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_and_install_source_newer(self) -> None:
        """Should install when source is newer than target."""
        executor = MagicMock(spec=RemoteExecutor)

        with (
            patch("pcswitcher.installation.get_current_version") as mock_current,
            patch("pcswitcher.installation.get_target_version") as mock_target,
            patch("pcswitcher.installation.install_on_target") as mock_install,
        ):
            mock_current.return_value = "1.2.3"
            mock_target.return_value = "1.2.2"
            mock_install.return_value = None

            await check_and_install(executor)

            mock_install.assert_called_once_with(executor, "1.2.3")

    @pytest.mark.asyncio
    async def test_check_and_install_target_newer(self) -> None:
        """Should raise error when target is newer than source."""
        executor = MagicMock(spec=RemoteExecutor)

        with (
            patch("pcswitcher.installation.get_current_version") as mock_current,
            patch("pcswitcher.installation.get_target_version") as mock_target,
        ):
            mock_current.return_value = "1.2.2"
            mock_target.return_value = "1.2.3"

            with pytest.raises(InstallationError) as exc_info:
                await check_and_install(executor)

            error_msg = str(exc_info.value)
            assert "Target machine has newer pc-switcher version" in error_msg
            assert "1.2.3" in error_msg
            assert "1.2.2" in error_msg
            assert "Cannot sync from older source to newer target" in error_msg
