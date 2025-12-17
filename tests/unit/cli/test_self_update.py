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


class TestVersionComparison:
    """Tests for version comparison logic using pcswitcher.version.Version.

    The update command compares versions using Version which normalizes
    different version formats (SemVer vs PEP 440) to equivalent values.
    """

    def test_semver_and_pep440_prerelease_are_equal(self) -> None:
        """SemVer '0.1.0-alpha.1' should equal PEP 440 '0.1.0a1'."""
        semver_style = Version.parse_semver("0.1.0-alpha.1")
        pep440_style = Version.parse_pep440("0.1.0a1")
        assert semver_style == pep440_style

    def test_semver_and_pep440_beta_are_equal(self) -> None:
        """SemVer '0.2.0-beta.2' should equal PEP 440 '0.2.0b2'."""
        semver_style = Version.parse_semver("0.2.0-beta.2")
        pep440_style = Version.parse_pep440("0.2.0b2")
        assert semver_style == pep440_style

    def test_semver_and_pep440_rc_are_equal(self) -> None:
        """SemVer '1.0.0-rc.1' should equal PEP 440 '1.0.0rc1'."""
        semver_style = Version.parse_semver("1.0.0-rc.1")
        pep440_style = Version.parse_pep440("1.0.0rc1")
        assert semver_style == pep440_style

    def test_stable_versions_equal(self) -> None:
        """Stable versions should compare equal regardless of format."""
        assert Version.parse("1.0.0") == Version.parse("1.0.0")
        assert Version.parse("0.1.0") == Version.parse("0.1.0")

    def test_prerelease_less_than_stable(self) -> None:
        """Prerelease versions should be less than stable versions."""
        assert Version.parse_pep440("0.1.0a1") < Version.parse_pep440("0.1.0")
        assert Version.parse_semver("0.1.0-alpha.1") < Version.parse_pep440("0.1.0")
        assert Version.parse_pep440("1.0.0rc1") < Version.parse_pep440("1.0.0")
