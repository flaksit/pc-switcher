"""Tests for version module."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest

from pcswitcher.version import (
    Pep440Version,
    SemverVersion,
    get_this_version,
    is_semver_string,
    parse_version_str_from_cli_output,
    to_semver_str,
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


class TestParseVersionStrFromCliOutput:
    """Tests for parse_version_str_from_cli_output() with SemVer and PEP 440 support."""

    def test_parse_base_version(self) -> None:
        """Should parse base version without pre-release."""
        assert parse_version_str_from_cli_output("pc-switcher 0.1.0") == "0.1.0"
        assert parse_version_str_from_cli_output("pc-switcher 1.2.3") == "1.2.3"
        assert parse_version_str_from_cli_output("0.1.0") == "0.1.0"

    def test_parse_semver_alpha_version(self) -> None:
        """Should parse SemVer-style alpha pre-release versions."""
        assert parse_version_str_from_cli_output("pc-switcher 0.1.0-alpha.1") == "0.1.0-alpha.1"
        assert parse_version_str_from_cli_output("0.1.0-alpha.2") == "0.1.0-alpha.2"

    def test_parse_semver_beta_version(self) -> None:
        """Should parse SemVer-style beta pre-release versions."""
        assert parse_version_str_from_cli_output("pc-switcher 0.1.0-beta.1") == "0.1.0-beta.1"
        assert parse_version_str_from_cli_output("0.2.0-beta.3") == "0.2.0-beta.3"

    def test_parse_semver_rc_version(self) -> None:
        """Should parse SemVer-style release candidate versions."""
        assert parse_version_str_from_cli_output("pc-switcher 0.1.0-rc.1") == "0.1.0-rc.1"
        assert parse_version_str_from_cli_output("1.0.0-rc.2") == "1.0.0-rc.2"

    def test_parse_pep440_alpha_version(self) -> None:
        """Should parse PEP 440-style alpha pre-release versions."""
        assert parse_version_str_from_cli_output("pc-switcher 0.1.0a1") == "0.1.0a1"
        assert parse_version_str_from_cli_output("0.2.0a1") == "0.2.0a1"

    def test_parse_pep440_beta_version(self) -> None:
        """Should parse PEP 440-style beta pre-release versions."""
        assert parse_version_str_from_cli_output("pc-switcher 0.1.0b1") == "0.1.0b1"
        assert parse_version_str_from_cli_output("1.0.0b3") == "1.0.0b3"

    def test_parse_pep440_rc_version(self) -> None:
        """Should parse PEP 440-style release candidate versions."""
        assert parse_version_str_from_cli_output("pc-switcher 0.1.0rc1") == "0.1.0rc1"
        assert parse_version_str_from_cli_output("2.0.0rc1") == "2.0.0rc1"

    def test_parse_with_extra_text(self) -> None:
        """Should extract version from output with extra text."""
        output = "pc-switcher 0.1.0-alpha.1\nsome other text"
        assert parse_version_str_from_cli_output(output) == "0.1.0-alpha.1"

    def test_parse_invalid_version(self) -> None:
        """Should raise ValueError for invalid version string."""
        with pytest.raises(ValueError, match="Cannot parse version"):
            parse_version_str_from_cli_output("no version here")

    def test_parse_empty_string(self) -> None:
        """Should raise ValueError for empty string."""
        with pytest.raises(ValueError, match="Cannot parse version"):
            parse_version_str_from_cli_output("")


class TestIsSemverString:
    """Tests for is_semver_string()."""

    def test_valid_semver(self) -> None:
        """Should return True for valid SemVer strings."""
        assert is_semver_string("1.0.0") is True
        assert is_semver_string("0.1.0-alpha.1") is True
        assert is_semver_string("1.0.0-rc.1+build") is True
        assert is_semver_string("2.0.0+build.123") is True

    def test_invalid_semver(self) -> None:
        """Should return False for invalid SemVer strings."""
        assert is_semver_string("1.0") is False
        assert is_semver_string("1.0.0a1") is False  # PEP 440 format
        assert is_semver_string("v1.0.0") is False
        assert is_semver_string("invalid") is False


class TestPep440VersionParse:
    """Tests for Pep440Version.parse()."""

    def test_stable_release(self) -> None:
        """Should parse stable release versions."""
        v = Pep440Version.parse("1.0.0")
        assert v.release == (1, 0, 0)
        assert v.pre is None
        assert v.post is None
        assert v.dev is None
        assert v.local is None

    def test_alpha_prerelease(self) -> None:
        """Should parse alpha pre-release versions."""
        v = Pep440Version.parse("1.0.0a1")
        assert v.release == (1, 0, 0)
        assert v.pre == ("a", 1)

    def test_beta_prerelease(self) -> None:
        """Should parse beta pre-release versions."""
        v = Pep440Version.parse("1.0.0b2")
        assert v.release == (1, 0, 0)
        assert v.pre == ("b", 2)

    def test_rc_prerelease(self) -> None:
        """Should parse release candidate versions."""
        v = Pep440Version.parse("1.0.0rc1")
        assert v.release == (1, 0, 0)
        assert v.pre == ("rc", 1)

    def test_dev_release(self) -> None:
        """Should parse dev release versions."""
        v = Pep440Version.parse("1.0.0.dev5")
        assert v.release == (1, 0, 0)
        assert v.dev == 5

    def test_post_release(self) -> None:
        """Should parse post release versions."""
        v = Pep440Version.parse("1.0.0.post1")
        assert v.release == (1, 0, 0)
        assert v.post == 1

    def test_local_version(self) -> None:
        """Should parse local version."""
        v = Pep440Version.parse("1.0.0+ubuntu1")
        assert v.release == (1, 0, 0)
        assert v.local == "ubuntu1"

    def test_complex_version(self) -> None:
        """Should parse complex version with all parts."""
        v = Pep440Version.parse("1.0.0a1.post2.dev3+local")
        assert v.release == (1, 0, 0)
        assert v.pre == ("a", 1)
        assert v.post == 2
        assert v.dev == 3
        assert v.local == "local"

    def test_implicit_zero_post(self) -> None:
        """Should handle .post without number (implicit 0)."""
        v = Pep440Version.parse("1.0.0.post")
        assert v.post == 0

    def test_implicit_zero_dev(self) -> None:
        """Should handle .dev without number (implicit 0)."""
        v = Pep440Version.parse("1.0.0.dev")
        assert v.dev == 0

    def test_epoch_raises(self) -> None:
        """Should raise ValueError for epoch."""
        with pytest.raises(ValueError, match="epoch is not supported"):
            Pep440Version.parse("1!1.0.0")


class TestPep440VersionStr:
    """Tests for Pep440Version.__str__()."""

    def test_str_stable(self) -> None:
        """Should format stable version."""
        v = Pep440Version(release=(1, 0, 0))
        assert str(v) == "1.0.0"

    def test_str_prerelease(self) -> None:
        """Should format pre-release version."""
        v = Pep440Version(release=(1, 0, 0), pre=("a", 1))
        assert str(v) == "1.0.0a1"

    def test_str_complex(self) -> None:
        """Should format complex version."""
        v = Pep440Version(release=(1, 0, 0), pre=("rc", 1), post=2, dev=3, local="build")
        assert str(v) == "1.0.0rc1.post2.dev3+build"


class TestSemverVersionParse:
    """Tests for SemverVersion.parse()."""

    def test_stable_release(self) -> None:
        """Should parse stable release versions."""
        v = SemverVersion.parse("1.0.0")
        assert v.major == 1
        assert v.minor == 0
        assert v.patch == 0
        assert v.prerelease is None
        assert v.build is None

    def test_prerelease(self) -> None:
        """Should parse pre-release versions."""
        v = SemverVersion.parse("1.0.0-alpha.1")
        assert v.prerelease == "alpha.1"

    def test_build_metadata(self) -> None:
        """Should parse build metadata."""
        v = SemverVersion.parse("1.0.0+build.123")
        assert v.build == "build.123"

    def test_full_version(self) -> None:
        """Should parse full version with prerelease and build."""
        v = SemverVersion.parse("1.0.0-alpha.1+build.123")
        assert v.prerelease == "alpha.1"
        assert v.build == "build.123"

    def test_invalid_raises(self) -> None:
        """Should raise ValueError for invalid SemVer."""
        with pytest.raises(ValueError, match="Invalid SemVer"):
            SemverVersion.parse("1.0")


class TestSemverVersionStr:
    """Tests for SemverVersion.__str__()."""

    def test_str_stable(self) -> None:
        """Should format stable version."""
        v = SemverVersion(major=1, minor=0, patch=0)
        assert str(v) == "1.0.0"

    def test_str_prerelease(self) -> None:
        """Should format pre-release version."""
        v = SemverVersion(major=1, minor=0, patch=0, prerelease="alpha.1")
        assert str(v) == "1.0.0-alpha.1"

    def test_str_full(self) -> None:
        """Should format full version."""
        v = SemverVersion(major=1, minor=0, patch=0, prerelease="rc.1", build="build.123")
        assert str(v) == "1.0.0-rc.1+build.123"


class TestPep440ToSemver:
    """Tests for Pep440Version.to_semver()."""

    def test_stable_release(self) -> None:
        """Stable release should convert directly."""
        v = Pep440Version.parse("1.0.0")
        s = v.to_semver()
        assert str(s) == "1.0.0"

    def test_alpha_prerelease(self) -> None:
        """Alpha should convert to -alpha.N."""
        v = Pep440Version.parse("1.0.0a1")
        s = v.to_semver()
        assert str(s) == "1.0.0-alpha.1"

    def test_beta_prerelease(self) -> None:
        """Beta should convert to -beta.N."""
        v = Pep440Version.parse("1.0.0b2")
        s = v.to_semver()
        assert str(s) == "1.0.0-beta.2"

    def test_rc_prerelease(self) -> None:
        """RC should convert to -rc.N."""
        v = Pep440Version.parse("1.0.0rc1")
        s = v.to_semver()
        assert str(s) == "1.0.0-rc.1"

    def test_dev_release(self) -> None:
        """Dev should convert to -dev.N."""
        v = Pep440Version.parse("1.0.0.dev5")
        s = v.to_semver()
        assert str(s) == "1.0.0-dev.5"

    def test_prerelease_with_dev(self) -> None:
        """Pre-release with dev (no post) should combine in prerelease."""
        v = Pep440Version.parse("1.0.0a1.dev2")
        s = v.to_semver()
        assert str(s) == "1.0.0-alpha.1.dev.2"

    def test_post_release(self) -> None:
        """Post should convert to +post.N."""
        v = Pep440Version.parse("1.0.0.post1")
        s = v.to_semver()
        assert str(s) == "1.0.0+post.1"

    def test_post_with_dev(self) -> None:
        """Post with dev should put both in build."""
        v = Pep440Version.parse("1.0.0.post3.dev5")
        s = v.to_semver()
        assert str(s) == "1.0.0+post.3.dev.5"

    def test_local_only(self) -> None:
        """Local only should go to build."""
        v = Pep440Version.parse("1.0.0+abc123")
        s = v.to_semver()
        assert str(s) == "1.0.0+abc123"

    def test_post_with_local(self) -> None:
        """Post with local should combine in build."""
        v = Pep440Version.parse("1.0.0.post2+ubuntu1")
        s = v.to_semver()
        assert str(s) == "1.0.0+post.2.ubuntu1"

    def test_post_dev_with_local(self) -> None:
        """Post.dev with local should all go to build."""
        v = Pep440Version.parse("1.0.0.post3.dev5+abc123")
        s = v.to_semver()
        assert str(s) == "1.0.0+post.3.dev.5.abc123"

    def test_wrong_release_parts_raises(self) -> None:
        """Should raise ValueError if not 3 release parts."""
        v = Pep440Version(release=(1, 0))
        with pytest.raises(ValueError, match="exactly 3 release parts"):
            v.to_semver()


class TestSemverToPep440:
    """Tests for SemverVersion.to_pep440()."""

    def test_stable_release(self) -> None:
        """Stable release should convert directly."""
        v = SemverVersion.parse("1.0.0")
        p = v.to_pep440()
        assert str(p) == "1.0.0"

    def test_alpha_prerelease(self) -> None:
        """Alpha should convert to aN."""
        v = SemverVersion.parse("1.0.0-alpha.1")
        p = v.to_pep440()
        assert str(p) == "1.0.0a1"

    def test_beta_prerelease(self) -> None:
        """Beta should convert to bN."""
        v = SemverVersion.parse("1.0.0-beta.2")
        p = v.to_pep440()
        assert str(p) == "1.0.0b2"

    def test_rc_prerelease(self) -> None:
        """RC should convert to rcN."""
        v = SemverVersion.parse("1.0.0-rc.1")
        p = v.to_pep440()
        assert str(p) == "1.0.0rc1"

    def test_dev_release(self) -> None:
        """Dev should convert to .devN."""
        v = SemverVersion.parse("1.0.0-dev.5")
        p = v.to_pep440()
        assert str(p) == "1.0.0.dev5"

    def test_prerelease_with_dev(self) -> None:
        """Pre-release with dev should combine."""
        v = SemverVersion.parse("1.0.0-alpha.1.dev.2")
        p = v.to_pep440()
        assert str(p) == "1.0.0a1.dev2"

    def test_post_release(self) -> None:
        """Build +post.N should convert to .postN."""
        v = SemverVersion.parse("1.0.0+post.1")
        p = v.to_pep440()
        assert str(p) == "1.0.0.post1"

    def test_post_with_dev(self) -> None:
        """Build +post.N.dev.M should convert to .postN.devM."""
        v = SemverVersion.parse("1.0.0+post.3.dev.5")
        p = v.to_pep440()
        assert str(p) == "1.0.0.post3.dev5"

    def test_local_only(self) -> None:
        """Build without post should become local."""
        v = SemverVersion.parse("1.0.0+abc123")
        p = v.to_pep440()
        assert str(p) == "1.0.0+abc123"

    def test_post_with_local(self) -> None:
        """Build +post.N.local should split to .postN+local."""
        v = SemverVersion.parse("1.0.0+post.2.ubuntu1")
        p = v.to_pep440()
        assert str(p) == "1.0.0.post2+ubuntu1"

    def test_post_dev_with_local(self) -> None:
        """Build +post.N.dev.M.local should convert fully."""
        v = SemverVersion.parse("1.0.0+post.3.dev.5.abc123")
        p = v.to_pep440()
        assert str(p) == "1.0.0.post3.dev5+abc123"


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
            "1.0.0.post3.dev5",
            "1.0.0+ubuntu1",
            "1.0.0.post2+ubuntu1",
            "1.0.0.post3.dev5+abc123",
        ],
    )
    def test_pep440_roundtrip(self, pep440: str) -> None:
        """PEP 440 → SemVer → PEP 440 should be identity."""
        p = Pep440Version.parse(pep440)
        s = p.to_semver()
        result = s.to_pep440()
        assert str(result) == pep440, f"{pep440} → {s} → {result}"

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
            "1.0.0+post.1",
            "1.0.0+post.3.dev.5",
            "1.0.0+ubuntu1",
            "1.0.0+post.2.ubuntu1",
            "1.0.0+post.3.dev.5.abc123",
        ],
    )
    def test_semver_roundtrip(self, semver: str) -> None:
        """SemVer → PEP 440 → SemVer should be identity."""
        s = SemverVersion.parse(semver)
        p = s.to_pep440()
        result = p.to_semver()
        assert str(result) == semver, f"{semver} → {p} → {result}"


class TestToSemverStr:
    """Tests for to_semver_str() - converting versions to SemVer format."""

    def test_stable_version(self) -> None:
        """Stable versions should convert."""
        assert to_semver_str("1.0.0") == "1.0.0"

    def test_semver_unchanged(self) -> None:
        """Already SemVer-formatted versions should be returned unchanged."""
        assert to_semver_str("0.1.0-alpha.1") == "0.1.0-alpha.1"
        assert to_semver_str("0.2.0-beta.2") == "0.2.0-beta.2"
        assert to_semver_str("1.0.0-rc.1") == "1.0.0-rc.1"

    def test_pep440_alpha_to_semver(self) -> None:
        """PEP 440 alpha versions should be converted."""
        assert to_semver_str("0.1.0a1") == "0.1.0-alpha.1"

    def test_pep440_beta_to_semver(self) -> None:
        """PEP 440 beta versions should be converted."""
        assert to_semver_str("0.1.0b1") == "0.1.0-beta.1"

    def test_pep440_rc_to_semver(self) -> None:
        """PEP 440 release candidate versions should be converted."""
        assert to_semver_str("0.1.0rc1") == "0.1.0-rc.1"

    def test_post_release(self) -> None:
        """Post release should convert to build metadata."""
        assert to_semver_str("1.0.0.post2") == "1.0.0+post.2"

    def test_post_with_local(self) -> None:
        """Post with local should combine in build."""
        assert to_semver_str("1.0.0.post2+ubuntu1") == "1.0.0+post.2.ubuntu1"

    def test_invalid_raises(self) -> None:
        """Invalid versions should raise ValueError."""
        with pytest.raises(ValueError):
            to_semver_str("invalid")
