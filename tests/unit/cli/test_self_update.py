"""Tests for cli.py self update command."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from pcswitcher import cli
from pcswitcher.version import Release, Version


class TestRunUvToolInstall:
    """Tests for _run_uv_tool_install()."""

    def test_constructs_correct_command(self) -> None:
        """Should construct correct uv tool install command."""
        with patch("pcswitcher.cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="Installed",
                stderr="",
            )
            release = Release(Version.parse("0.4.0"), is_prerelease=False, tag="v0.4.0")
            cli._run_uv_tool_install(release)  # pyright: ignore[reportPrivateUsage]
            mock_run.assert_called_once_with(
                ["uv", "tool", "install", "--force", "git+https://github.com/flaksit/pc-switcher@v0.4.0"],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_returns_completed_process(self) -> None:
        """Should return subprocess.CompletedProcess."""
        expected = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Success",
            stderr="",
        )
        with patch("pcswitcher.cli.subprocess.run", return_value=expected):
            release = Release(Version.parse("1.0.0"), is_prerelease=False, tag="v1.0.0")
            result = cli._run_uv_tool_install(release)  # pyright: ignore[reportPrivateUsage]
            assert result.returncode == 0
            assert result.stdout == "Success"


class TestVerifyInstalledVersion:
    """Tests for _verify_installed_version()."""

    def test_success(self) -> None:
        """Should return Version object on success."""
        with patch("pcswitcher.cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="pc-switcher 0.4.0",
                stderr="",
            )
            result = cli._verify_installed_version()  # pyright: ignore[reportPrivateUsage]
            assert result is not None
            assert result.pep440_str() == "0.4.0"

    def test_command_not_found(self) -> None:
        """Should return None if pc-switcher not found."""
        with patch("pcswitcher.cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=127,
                stdout="",
                stderr="command not found",
            )
            result = cli._verify_installed_version()  # pyright: ignore[reportPrivateUsage]
            assert result is None

    def test_invalid_version_output(self) -> None:
        """Should return None if version output cannot be parsed."""
        with patch("pcswitcher.cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="garbage output",
                stderr="",
            )
            result = cli._verify_installed_version()  # pyright: ignore[reportPrivateUsage]
            assert result is None
