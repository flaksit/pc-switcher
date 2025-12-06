"""Tests for version module."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest

from pcswitcher.version import (
    get_this_version,
    parse_version_from_cli_output,
    pep440_to_semver,
    semver_to_pep440,
    to_semver_display,
)


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
    """Tests for parse_version_from_cli_output() with SemVer and PEP 440 support."""

    def test_parse_base_version(self) -> None:
        """Should parse base version without pre-release."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0") == "0.1.0"
        assert parse_version_from_cli_output("pc-switcher 1.2.3") == "1.2.3"
        assert parse_version_from_cli_output("0.1.0") == "0.1.0"

    def test_parse_semver_alpha_version(self) -> None:
        """Should parse SemVer-style alpha pre-release versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0-alpha") == "0.1.0-alpha"
        assert parse_version_from_cli_output("pc-switcher 0.1.0-alpha.1") == "0.1.0-alpha.1"
        assert parse_version_from_cli_output("0.1.0-alpha.2") == "0.1.0-alpha.2"

    def test_parse_semver_beta_version(self) -> None:
        """Should parse SemVer-style beta pre-release versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0-beta") == "0.1.0-beta"
        assert parse_version_from_cli_output("pc-switcher 0.1.0-beta.1") == "0.1.0-beta.1"
        assert parse_version_from_cli_output("0.2.0-beta.3") == "0.2.0-beta.3"

    def test_parse_semver_rc_version(self) -> None:
        """Should parse SemVer-style release candidate versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0-rc") == "0.1.0-rc"
        assert parse_version_from_cli_output("pc-switcher 0.1.0-rc.1") == "0.1.0-rc.1"
        assert parse_version_from_cli_output("1.0.0-rc.2") == "1.0.0-rc.2"

    def test_parse_pep440_alpha_version(self) -> None:
        """Should parse PEP 440-style alpha pre-release versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0a1") == "0.1.0a1"
        assert parse_version_from_cli_output("pc-switcher 0.1.0a2") == "0.1.0a2"
        assert parse_version_from_cli_output("0.2.0a1") == "0.2.0a1"

    def test_parse_pep440_beta_version(self) -> None:
        """Should parse PEP 440-style beta pre-release versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0b1") == "0.1.0b1"
        assert parse_version_from_cli_output("pc-switcher 0.2.0b2") == "0.2.0b2"
        assert parse_version_from_cli_output("1.0.0b3") == "1.0.0b3"

    def test_parse_pep440_rc_version(self) -> None:
        """Should parse PEP 440-style release candidate versions."""
        assert parse_version_from_cli_output("pc-switcher 0.1.0rc1") == "0.1.0rc1"
        assert parse_version_from_cli_output("pc-switcher 1.0.0rc2") == "1.0.0rc2"
        assert parse_version_from_cli_output("2.0.0rc1") == "2.0.0rc1"

    def test_parse_with_extra_text(self) -> None:
        """Should extract version from output with extra text."""
        output = "pc-switcher 0.1.0-alpha.1\nsome other text"
        assert parse_version_from_cli_output(output) == "0.1.0-alpha.1"

    def test_parse_dev_version(self) -> None:
        """Should parse development versions from git, extracting base+prerelease."""
        # uv-dynamic-versioning creates versions like 0.1.0a1.post20.dev0+4e7b776
        # Our regex captures the base version + prerelease part (0.1.0a1)
        output = "pc-switcher 0.1.0a1.post20.dev0+4e7b776"
        assert parse_version_from_cli_output(output) == "0.1.0a1"

        # For base versions without prerelease, only captures the base
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


class TestPep440ToSemver:
    """Tests for pep440_to_semver() - converting PEP 440 to SemVer format."""

    # === Release versions ===
    def test_stable_release(self) -> None:
        """Stable release versions should convert directly."""
        assert pep440_to_semver("1.0.0") == "1.0.0"
        assert pep440_to_semver("0.1.0") == "0.1.0"
        assert pep440_to_semver("2.3.4") == "2.3.4"

    def test_release_with_wrong_parts_raises(self) -> None:
        """Release with != 3 parts should raise ValueError."""
        with pytest.raises(ValueError, match="exactly 3 release parts"):
            pep440_to_semver("1.0")
        with pytest.raises(ValueError, match="exactly 3 release parts"):
            pep440_to_semver("1.0.0.0")

    # === Pre-release versions ===
    def test_alpha_prerelease(self) -> None:
        """Alpha pre-release should convert to -alpha.N."""
        assert pep440_to_semver("1.0.0a1") == "1.0.0-alpha.1"
        assert pep440_to_semver("0.2.0a10") == "0.2.0-alpha.10"

    def test_beta_prerelease(self) -> None:
        """Beta pre-release should convert to -beta.N."""
        assert pep440_to_semver("1.0.0b1") == "1.0.0-beta.1"
        assert pep440_to_semver("0.2.0b2") == "0.2.0-beta.2"

    def test_rc_prerelease(self) -> None:
        """Release candidate should convert to -rc.N."""
        assert pep440_to_semver("1.0.0rc1") == "1.0.0-rc.1"
        assert pep440_to_semver("2.0.0rc10") == "2.0.0-rc.10"

    # === Dev releases ===
    def test_dev_release(self) -> None:
        """Dev release should convert to -dev.N."""
        assert pep440_to_semver("1.0.0.dev5") == "1.0.0-dev.5"
        assert pep440_to_semver("0.1.0.dev0") == "0.1.0-dev.0"

    def test_prerelease_with_dev(self) -> None:
        """Pre-release with dev should combine in prerelease."""
        assert pep440_to_semver("1.0.0a1.dev2") == "1.0.0-alpha.1.dev.2"
        assert pep440_to_semver("1.0.0b3.dev0") == "1.0.0-beta.3.dev.0"
        assert pep440_to_semver("1.0.0rc1.dev5") == "1.0.0-rc.1.dev.5"

    # === Post releases ===
    def test_post_release(self) -> None:
        """Post release should convert to +pN build metadata."""
        assert pep440_to_semver("1.0.0.post1") == "1.0.0+p1"
        assert pep440_to_semver("2.0.0.post10") == "2.0.0+p10"

    def test_prerelease_with_post(self) -> None:
        """Pre-release with post should have both."""
        assert pep440_to_semver("1.0.0a1.post2") == "1.0.0-alpha.1+p2"
        assert pep440_to_semver("1.0.0rc1.post1") == "1.0.0-rc.1+p1"

    # === Local versions ===
    def test_local_version(self) -> None:
        """Local version should convert to +l.xxx build metadata."""
        assert pep440_to_semver("1.0.0+ubuntu1") == "1.0.0+l.ubuntu1"
        assert pep440_to_semver("1.0.0+local.build.123") == "1.0.0+l.local.build.123"

    def test_post_with_local(self) -> None:
        """Post with local should have both in build metadata."""
        assert pep440_to_semver("1.0.0.post2+ubuntu1") == "1.0.0+p2.l.ubuntu1"
        assert pep440_to_semver("1.0.0.post1+local.1") == "1.0.0+p1.l.local.1"

    # === Complex combinations ===
    def test_full_complex_version(self) -> None:
        """Full complex version with all parts."""
        assert pep440_to_semver("1.0.0a1.post2.dev3+local") == "1.0.0-alpha.1.dev.3+p2.l.local"

    # === Epoch (not supported) ===
    def test_epoch_raises(self) -> None:
        """Epoch should raise ValueError."""
        with pytest.raises(ValueError, match="epoch is not supported"):
            pep440_to_semver("1!1.0.0")


class TestSemverToPep440:
    """Tests for semver_to_pep440() - converting SemVer to PEP 440 format."""

    # === Release versions ===
    def test_stable_release(self) -> None:
        """Stable release versions should convert directly."""
        assert semver_to_pep440("1.0.0") == "1.0.0"
        assert semver_to_pep440("0.1.0") == "0.1.0"
        assert semver_to_pep440("2.3.4") == "2.3.4"

    # === Pre-release versions ===
    def test_alpha_prerelease(self) -> None:
        """Alpha pre-release should convert to aN."""
        assert semver_to_pep440("1.0.0-alpha.1") == "1.0.0a1"
        assert semver_to_pep440("0.2.0-alpha.10") == "0.2.0a10"

    def test_beta_prerelease(self) -> None:
        """Beta pre-release should convert to bN."""
        assert semver_to_pep440("1.0.0-beta.1") == "1.0.0b1"
        assert semver_to_pep440("0.2.0-beta.2") == "0.2.0b2"

    def test_rc_prerelease(self) -> None:
        """Release candidate should convert to rcN."""
        assert semver_to_pep440("1.0.0-rc.1") == "1.0.0rc1"
        assert semver_to_pep440("2.0.0-rc.10") == "2.0.0rc10"

    # === Dev releases ===
    def test_dev_release(self) -> None:
        """Dev release should convert to .devN."""
        assert semver_to_pep440("1.0.0-dev.5") == "1.0.0.dev5"
        assert semver_to_pep440("0.1.0-dev.0") == "0.1.0.dev0"

    def test_prerelease_with_dev(self) -> None:
        """Pre-release with dev should combine."""
        assert semver_to_pep440("1.0.0-alpha.1.dev.2") == "1.0.0a1.dev2"
        assert semver_to_pep440("1.0.0-beta.3.dev.0") == "1.0.0b3.dev0"
        assert semver_to_pep440("1.0.0-rc.1.dev.5") == "1.0.0rc1.dev5"

    # === Post releases (from build metadata) ===
    def test_post_release(self) -> None:
        """Build metadata pN should convert to .postN."""
        assert semver_to_pep440("1.0.0+p1") == "1.0.0.post1"
        assert semver_to_pep440("2.0.0+p10") == "2.0.0.post10"

    def test_prerelease_with_post(self) -> None:
        """Pre-release with post build metadata."""
        assert semver_to_pep440("1.0.0-alpha.1+p2") == "1.0.0a1.post2"
        assert semver_to_pep440("1.0.0-rc.1+p1") == "1.0.0rc1.post1"

    # === Local versions (from build metadata) ===
    def test_local_version(self) -> None:
        """Build metadata l.xxx should convert to +xxx."""
        assert semver_to_pep440("1.0.0+l.ubuntu1") == "1.0.0+ubuntu1"
        assert semver_to_pep440("1.0.0+l.local.build.123") == "1.0.0+local.build.123"

    def test_post_with_local(self) -> None:
        """Post and local in build metadata."""
        assert semver_to_pep440("1.0.0+p2.l.ubuntu1") == "1.0.0.post2+ubuntu1"
        assert semver_to_pep440("1.0.0+p1.l.local.1") == "1.0.0.post1+local.1"

    # === Complex combinations ===
    def test_full_complex_version(self) -> None:
        """Full complex version with all parts."""
        assert semver_to_pep440("1.0.0-alpha.1.dev.3+p2.l.local") == "1.0.0a1.post2.dev3+local"

    # === Error cases ===
    def test_unrecognized_prerelease_raises(self) -> None:
        """Unrecognized prerelease identifier should raise."""
        with pytest.raises(ValueError, match="Unrecognized prerelease"):
            semver_to_pep440("1.0.0-foo.1")

    def test_unrecognized_build_metadata_raises(self) -> None:
        """Unrecognized build metadata should raise."""
        with pytest.raises(ValueError, match="Unrecognized build metadata"):
            semver_to_pep440("1.0.0+unknown")


class TestSymmetricConversion:
    """Tests for symmetric (round-trip) conversion between PEP 440 and SemVer."""

    @pytest.mark.parametrize(
        "pep440",
        [
            "1.0.0",
            "0.1.0",
            "1.0.0a1",
            "1.0.0b2",
            "1.0.0rc1",
            "1.0.0.dev5",
            "1.0.0a1.dev2",
            "1.0.0.post1",
            "1.0.0a1.post2",
            "1.0.0+ubuntu1",
            "1.0.0.post2+ubuntu1",
            "1.0.0a1.post2.dev3+local",
        ],
    )
    def test_pep440_roundtrip(self, pep440: str) -> None:
        """PEP 440 → SemVer → PEP 440 should be identity."""
        semver = pep440_to_semver(pep440)
        result = semver_to_pep440(semver)
        assert result == pep440, f"{pep440} → {semver} → {result}"

    @pytest.mark.parametrize(
        "semver",
        [
            "1.0.0",
            "0.1.0",
            "1.0.0-alpha.1",
            "1.0.0-beta.2",
            "1.0.0-rc.1",
            "1.0.0-dev.5",
            "1.0.0-alpha.1.dev.2",
            "1.0.0+p1",
            "1.0.0-alpha.1+p2",
            "1.0.0+l.ubuntu1",
            "1.0.0+p2.l.ubuntu1",
            "1.0.0-alpha.1.dev.3+p2.l.local",
        ],
    )
    def test_semver_roundtrip(self, semver: str) -> None:
        """SemVer → PEP 440 → SemVer should be identity."""
        pep440 = semver_to_pep440(semver)
        result = pep440_to_semver(pep440)
        assert result == semver, f"{semver} → {pep440} → {result}"


class TestToSemverDisplay:
    """Tests for to_semver_display() - converting versions to SemVer format for display."""

    def test_stable_version_unchanged(self) -> None:
        """Stable versions should be returned unchanged."""
        assert to_semver_display("1.0.0") == "1.0.0"
        assert to_semver_display("0.1.0") == "0.1.0"
        assert to_semver_display("2.3.4") == "2.3.4"

    def test_semver_format_unchanged(self) -> None:
        """Already SemVer-formatted versions should be returned unchanged."""
        assert to_semver_display("0.1.0-alpha.1") == "0.1.0-alpha.1"
        assert to_semver_display("0.2.0-beta.2") == "0.2.0-beta.2"
        assert to_semver_display("1.0.0-rc.1") == "1.0.0-rc.1"

    def test_pep440_alpha_to_semver(self) -> None:
        """PEP 440 alpha versions should be converted to SemVer format."""
        assert to_semver_display("0.1.0a1") == "0.1.0-alpha.1"
        assert to_semver_display("0.2.0a2") == "0.2.0-alpha.2"
        assert to_semver_display("1.0.0a10") == "1.0.0-alpha.10"

    def test_pep440_beta_to_semver(self) -> None:
        """PEP 440 beta versions should be converted to SemVer format."""
        assert to_semver_display("0.1.0b1") == "0.1.0-beta.1"
        assert to_semver_display("0.2.0b2") == "0.2.0-beta.2"
        assert to_semver_display("1.0.0b3") == "1.0.0-beta.3"

    def test_pep440_rc_to_semver(self) -> None:
        """PEP 440 release candidate versions should be converted to SemVer format."""
        assert to_semver_display("0.1.0rc1") == "0.1.0-rc.1"
        assert to_semver_display("1.0.0rc2") == "1.0.0-rc.2"
        assert to_semver_display("2.0.0rc10") == "2.0.0-rc.10"

    def test_dev_version_with_prerelease(self) -> None:
        """Development versions with prerelease should include full build metadata."""
        assert to_semver_display("0.1.0a1.post20.dev0+4e7b776") == "0.1.0-alpha.1.dev.0+p20.l.4e7b776"
        assert to_semver_display("0.2.0b2.post5.dev0+abc1234") == "0.2.0-beta.2.dev.0+p5.l.abc1234"

    def test_dev_version_without_prerelease(self) -> None:
        """Development versions without prerelease should include full build metadata."""
        assert to_semver_display("0.0.0.post125.dev0+09ab5f5") == "0.0.0-dev.0+p125.l.09ab5f5"
        assert to_semver_display("1.0.0.post1.dev0+abc1234") == "1.0.0-dev.0+p1.l.abc1234"

    def test_invalid_pep440_returns_unchanged(self) -> None:
        """Invalid PEP 440 versions should be returned as-is."""
        # These don't match PEP 440 format
        assert to_semver_display("invalid") == "invalid"
        assert to_semver_display("1.0") == "1.0"
