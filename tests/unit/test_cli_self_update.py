"""Tests for cli.py self update command."""

from __future__ import annotations

import json
import subprocess
from http.client import HTTPResponse
from unittest.mock import MagicMock, patch

import pytest
from packaging.version import Version

from pcswitcher import cli


class TestGetLatestGithubVersion:
    """Tests for _get_latest_github_version()."""

    def test_returns_stable_release_by_default(self) -> None:
        """Should return first stable (non-prerelease) version."""
        releases = [
            {"tag_name": "v0.2.0-alpha.1", "prerelease": True, "draft": False},
            {"tag_name": "v0.1.0", "prerelease": False, "draft": False},
        ]
        mock_response = MagicMock(spec=HTTPResponse)
        mock_response.read.return_value = json.dumps(releases).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("pcswitcher.cli.urllib.request.urlopen", return_value=mock_response):
            result = cli._get_latest_github_version()  # pyright: ignore[reportPrivateUsage]
            assert result == "0.1.0"

    def test_returns_prerelease_when_flag_set(self) -> None:
        """Should return prerelease version when include_prerelease=True."""
        releases = [
            {"tag_name": "v0.2.0-alpha.1", "prerelease": True, "draft": False},
            {"tag_name": "v0.1.0", "prerelease": False, "draft": False},
        ]
        mock_response = MagicMock(spec=HTTPResponse)
        mock_response.read.return_value = json.dumps(releases).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("pcswitcher.cli.urllib.request.urlopen", return_value=mock_response):
            result = cli._get_latest_github_version(include_prerelease=True)  # pyright: ignore[reportPrivateUsage]
            assert result == "0.2.0-alpha.1"

    def test_skips_draft_releases(self) -> None:
        """Should skip draft releases even with prerelease flag."""
        releases = [
            {"tag_name": "v0.3.0", "prerelease": False, "draft": True},
            {"tag_name": "v0.2.0-alpha.1", "prerelease": True, "draft": False},
        ]
        mock_response = MagicMock(spec=HTTPResponse)
        mock_response.read.return_value = json.dumps(releases).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("pcswitcher.cli.urllib.request.urlopen", return_value=mock_response):
            result = cli._get_latest_github_version(include_prerelease=True)  # pyright: ignore[reportPrivateUsage]
            assert result == "0.2.0-alpha.1"

    def test_strips_v_prefix(self) -> None:
        """Should strip 'v' prefix from tag name."""
        releases = [{"tag_name": "v1.2.3", "prerelease": False, "draft": False}]
        mock_response = MagicMock(spec=HTTPResponse)
        mock_response.read.return_value = json.dumps(releases).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("pcswitcher.cli.urllib.request.urlopen", return_value=mock_response):
            result = cli._get_latest_github_version()  # pyright: ignore[reportPrivateUsage]
            assert result == "1.2.3"

    def test_handles_no_v_prefix(self) -> None:
        """Should handle tag names without 'v' prefix."""
        releases = [{"tag_name": "1.0.0", "prerelease": False, "draft": False}]
        mock_response = MagicMock(spec=HTTPResponse)
        mock_response.read.return_value = json.dumps(releases).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("pcswitcher.cli.urllib.request.urlopen", return_value=mock_response):
            result = cli._get_latest_github_version()  # pyright: ignore[reportPrivateUsage]
            assert result == "1.0.0"

    def test_no_stable_releases_suggests_prerelease_flag(self) -> None:
        """Should suggest --prerelease when no stable releases exist."""
        releases = [
            {"tag_name": "v0.1.0-alpha.1", "prerelease": True, "draft": False},
        ]
        mock_response = MagicMock(spec=HTTPResponse)
        mock_response.read.return_value = json.dumps(releases).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("pcswitcher.cli.urllib.request.urlopen", return_value=mock_response):
            with pytest.raises(RuntimeError) as exc_info:
                cli._get_latest_github_version()  # pyright: ignore[reportPrivateUsage]
            assert "No stable releases found" in str(exc_info.value)
            assert "--prerelease" in str(exc_info.value)

    def test_no_releases_at_all(self) -> None:
        """Should raise error when no releases exist."""
        releases: list[dict[str, object]] = []
        mock_response = MagicMock(spec=HTTPResponse)
        mock_response.read.return_value = json.dumps(releases).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("pcswitcher.cli.urllib.request.urlopen", return_value=mock_response):
            with pytest.raises(RuntimeError) as exc_info:
                cli._get_latest_github_version(include_prerelease=True)  # pyright: ignore[reportPrivateUsage]
            assert "No releases found" in str(exc_info.value)

    def test_network_error(self) -> None:
        """Should raise RuntimeError on network error."""
        with patch(
            "pcswitcher.cli.urllib.request.urlopen",
            side_effect=Exception("Connection refused"),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                cli._get_latest_github_version()  # pyright: ignore[reportPrivateUsage]
            assert "Failed to fetch releases from GitHub" in str(exc_info.value)


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
            cli._run_uv_tool_install("0.4.0")  # pyright: ignore[reportPrivateUsage]
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
            result = cli._run_uv_tool_install("1.0.0")  # pyright: ignore[reportPrivateUsage]
            assert result.returncode == 0
            assert result.stdout == "Success"


class TestVerifyInstalledVersion:
    """Tests for _verify_installed_version()."""

    def test_success(self) -> None:
        """Should return version string on success."""
        with patch("pcswitcher.cli.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="pc-switcher 0.4.0",
                stderr="",
            )
            result = cli._verify_installed_version()  # pyright: ignore[reportPrivateUsage]
            assert result == "0.4.0"

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
    """Tests for version comparison logic using packaging.version.Version.

    The update command compares versions using packaging.version.Version which
    normalizes different version formats (SemVer vs PEP 440) to equivalent values.
    """

    def test_semver_and_pep440_prerelease_are_equal(self) -> None:
        """SemVer '0.1.0-alpha.1' should equal PEP 440 '0.1.0a1'."""
        semver_style = Version("0.1.0-alpha.1")
        pep440_style = Version("0.1.0a1")
        assert semver_style == pep440_style

    def test_semver_and_pep440_beta_are_equal(self) -> None:
        """SemVer '0.2.0-beta.2' should equal PEP 440 '0.2.0b2'."""
        semver_style = Version("0.2.0-beta.2")
        pep440_style = Version("0.2.0b2")
        assert semver_style == pep440_style

    def test_semver_and_pep440_rc_are_equal(self) -> None:
        """SemVer '1.0.0-rc.1' should equal PEP 440 '1.0.0rc1'."""
        semver_style = Version("1.0.0-rc.1")
        pep440_style = Version("1.0.0rc1")
        assert semver_style == pep440_style

    def test_stable_versions_equal(self) -> None:
        """Stable versions should compare equal regardless of format."""
        assert Version("1.0.0") == Version("1.0.0")
        assert Version("0.1.0") == Version("0.1.0")

    def test_prerelease_less_than_stable(self) -> None:
        """Prerelease versions should be less than stable versions."""
        assert Version("0.1.0a1") < Version("0.1.0")
        assert Version("0.1.0-alpha.1") < Version("0.1.0")
        assert Version("1.0.0rc1") < Version("1.0.0")
