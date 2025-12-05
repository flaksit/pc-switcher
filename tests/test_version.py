"""Tests for version module."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest

from pcswitcher.version import get_this_version, parse_version_from_cli_output


class TestGetCurrentVersion:
    """Tests for get_this_version()."""

    def test_get_this_version_success(self) -> None:
        """Should return version from package metadata."""
        with patch("pcswitcher.version.version") as mock_version:
            mock_version.return_value = "1.2.3"
            result = get_this_version()
            assert result == "1.2.3"
            mock_version.assert_called_once_with("pcswitcher")

    def test_get_this_version_package_not_found(self) -> None:
        """Should raise PackageNotFoundError if package not found."""
        with patch("pcswitcher.version.version") as mock_version:
            mock_version.side_effect = PackageNotFoundError("pcswitcher")

            with pytest.raises(PackageNotFoundError) as exc_info:
                get_this_version()

            assert "Cannot determine pc-switcher version" in str(exc_info.value)
            assert "Package metadata not found" in str(exc_info.value)


class TestParseVersionFromCliOutput:
    """Tests for parse_version_from_cli_output() with SemVer support."""

    def test_parse_base_version(self) -> None:
        """Should parse base version without pre-release."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0") == "0.1.0"
        assert parse_version_from_cli_output("pc-switcher 1.2.3") == "1.2.3"
        assert parse_version_from_cli_output("0.1.0") == "0.1.0"

    def test_parse_alpha_version(self) -> None:
        """Should parse alpha pre-release versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0-alpha") == "0.1.0-alpha"
        assert parse_version_from_cli_output("pc-switcher 0.1.0-alpha.1") == "0.1.0-alpha.1"
        assert parse_version_from_cli_output("0.1.0-alpha.2") == "0.1.0-alpha.2"

    def test_parse_beta_version(self) -> None:
        """Should parse beta pre-release versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0-beta") == "0.1.0-beta"
        assert parse_version_from_cli_output("pc-switcher 0.1.0-beta.1") == "0.1.0-beta.1"
        assert parse_version_from_cli_output("0.2.0-beta.3") == "0.2.0-beta.3"

    def test_parse_rc_version(self) -> None:
        """Should parse release candidate versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0-rc") == "0.1.0-rc"
        assert parse_version_from_cli_output("pc-switcher 0.1.0-rc.1") == "0.1.0-rc.1"
        assert parse_version_from_cli_output("1.0.0-rc.2") == "1.0.0-rc.2"

    def test_parse_with_extra_text(self) -> None:
        """Should extract version from output with extra text."""
        output = "pc-switcher 0.1.0-alpha.1\nsome other text"
        assert parse_version_from_cli_output(output) == "0.1.0-alpha.1"

    def test_parse_dev_version(self) -> None:
        """Should parse development versions from git."""
        # uv-dynamic-versioning can create versions like 0.0.0.post125.dev0+09ab5f5
        # Our regex only captures the base version part (0.0.0)
        output = "pc-switcher 0.0.0.post125.dev0+09ab5f5"
        assert parse_version_from_cli_output(output) == "0.0.0"

    def test_parse_invalid_version(self) -> None:
        """Should raise ValueError for invalid version string."""
        with pytest.raises(ValueError) as exc_info:
            parse_version_from_cli_output("no version here")
        assert "Cannot parse version from output" in str(exc_info.value)

    def test_parse_empty_string(self) -> None:
        """Should raise ValueError for empty string."""
        with pytest.raises(ValueError) as exc_info:
            parse_version_from_cli_output("")
        assert "Cannot parse version from output" in str(exc_info.value)
