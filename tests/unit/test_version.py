"""Tests for version module."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import pytest
from packaging.version import Version as PkgVersion

from pcswitcher.version import (
    Version,
    find_one_version,
    get_this_version,
)


class TestGetCurrentVersion:
    """Tests for get_this_version()."""

    def test_get_this_version_success(self) -> None:
        """Should return Version object from package metadata."""
        with patch("pcswitcher.version.get_pkg_version") as mock_version:
            mock_version.return_value = "1.2.3"
            result = get_this_version()
            assert isinstance(result, Version)
            assert result.pep440_str() == "1.2.3"
            mock_version.assert_called_once_with("pcswitcher")

    def test_get_this_version_package_not_found(self) -> None:
        """Should raise PackageNotFoundError if package not found."""
        with patch("pcswitcher.version.get_pkg_version") as mock_version:
            mock_version.side_effect = PackageNotFoundError("pcswitcher")

            with pytest.raises(PackageNotFoundError) as exc_info:
                get_this_version()

            assert "Cannot determine pc-switcher version" in str(exc_info.value)
            assert "Package metadata not found" in str(exc_info.value)

    def test_get_this_version_invalid_version_in_metadata(self) -> None:
        """Should raise ValueError if package metadata contains invalid version."""
        with patch("pcswitcher.version.get_pkg_version") as mock_version:
            mock_version.return_value = "not-a-version"
            with pytest.raises(ValueError, match="Invalid PEP 440 version"):
                get_this_version()

    def test_get_this_version_epoch_in_metadata_raises(self) -> None:
        """Should raise ValueError if package metadata contains epoch."""
        with patch("pcswitcher.version.get_pkg_version") as mock_version:
            mock_version.return_value = "1!1.0.0"
            with pytest.raises(ValueError, match="epoch is not supported"):
                get_this_version()


class TestFindOneVersion:
    """Tests for find_one_version() - returns Version object or raises."""

    def test_find_single_stable_version(self) -> None:
        """Should parse single stable version."""
        v = find_one_version("version 1.0.0")
        assert isinstance(v, Version)
        assert v.pep440_str() == "1.0.0"

    def test_find_single_pep440_version(self) -> None:
        """Should parse single PEP 440 version."""
        v = find_one_version("pc-switcher 0.1.0a1")
        assert isinstance(v, Version)
        assert v.pep440_str() == "0.1.0a1"

    def test_find_single_semver_version(self) -> None:
        """Should parse single SemVer version."""
        v = find_one_version("version 1.0.0-alpha.1")
        assert isinstance(v, Version)
        assert v.pep440_str() == "1.0.0a1"
        assert v.semver_str() == "1.0.0-alpha.1"

    def test_with_extra_text(self) -> None:
        """Should extract version from output with extra text."""
        v = find_one_version("pc-switcher 0.1.0-beta.1\nsome other text")
        assert isinstance(v, Version)
        assert v.pep440_str() == "0.1.0b1"

    def test_no_version_raises(self) -> None:
        """Should raise ValueError for invalid version string."""
        with pytest.raises(ValueError, match="No version string found"):
            find_one_version("no version here")

    def test_empty_string_raises(self) -> None:
        """Should raise ValueError for empty string."""
        with pytest.raises(ValueError, match="No version string found"):
            find_one_version("")

    def test_multiple_versions_raises(self) -> None:
        """Should raise ValueError for multiple versions."""
        with pytest.raises(ValueError, match="Multiple version strings found"):
            find_one_version("version 1.0.0 and 2.0.0")

    def test_whitespace_only_raises(self) -> None:
        """Should raise ValueError for whitespace-only input."""
        with pytest.raises(ValueError, match="No version string"):
            find_one_version("   \n   ")

    def test_invalid_version_format_raises(self) -> None:
        """Should raise ValueError when matched string is not a valid version."""
        # The regex is quite robust, but if it matches something invalid, Version.parse() will catch it
        with pytest.raises(ValueError):
            find_one_version("1!1.0.0")  # Epoch not supported

    def test_parse_complex_version(self) -> None:
        """Should parse complex versions from CLI output."""
        v = find_one_version("Installing pc-switcher version 0.1.0a3.post23.dev0+da749fc")
        assert isinstance(v, Version)
        assert v.pep440_str() == "0.1.0a3.post23.dev0+da749fc"


class TestVersionParsePep440:
    """Tests for Version.parse_pep440()."""

    def test_stable_release(self) -> None:
        """Should parse stable release versions."""
        v = Version.parse_pep440("1.0.0")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.pre is None
        assert v.pkg_version.post is None
        assert v.pkg_version.dev is None
        assert v.pkg_version.local is None

    def test_alpha_prerelease(self) -> None:
        """Should parse alpha pre-release versions."""
        v = Version.parse_pep440("1.0.0a1")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.pre == ("a", 1)

    def test_beta_prerelease(self) -> None:
        """Should parse beta pre-release versions."""
        v = Version.parse_pep440("1.0.0b2")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.pre == ("b", 2)

    def test_rc_prerelease(self) -> None:
        """Should parse release candidate versions."""
        v = Version.parse_pep440("1.0.0rc1")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.pre == ("rc", 1)

    def test_dev_release(self) -> None:
        """Should parse dev release versions."""
        v = Version.parse_pep440("1.0.0.dev5")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.dev == 5

    def test_post_release(self) -> None:
        """Should parse post release versions."""
        v = Version.parse_pep440("1.0.0.post1")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.post == 1

    def test_local_version(self) -> None:
        """Should parse local version."""
        v = Version.parse_pep440("1.0.0+ubuntu1")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.local == "ubuntu1"

    def test_complex_version(self) -> None:
        """Should parse complex version with all parts."""
        v = Version.parse_pep440("1.0.0a1.post2.dev3+local")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.pre == ("a", 1)
        assert v.pkg_version.post == 2
        assert v.pkg_version.dev == 3
        assert v.pkg_version.local == "local"

    def test_implicit_zero_post(self) -> None:
        """Should handle .post without number (implicit 0)."""
        v = Version.parse_pep440("1.0.0.post")
        assert v.pkg_version.post == 0

    def test_implicit_zero_dev(self) -> None:
        """Should handle .dev without number (implicit 0)."""
        v = Version.parse_pep440("1.0.0.dev")
        assert v.pkg_version.dev == 0

    def test_epoch_raises(self) -> None:
        """Should raise ValueError for epoch."""
        with pytest.raises(ValueError, match="epoch is not supported"):
            Version.parse_pep440("1!1.0.0")


class TestVersionParseSemver:
    """Tests for Version.parse_semver()."""

    def test_stable_release(self) -> None:
        """Should parse stable release versions."""
        v = Version.parse_semver("1.0.0")
        assert v.pkg_version.release == (1, 0, 0)
        assert v.pkg_version.pre is None

    def test_alpha_prerelease(self) -> None:
        """Should parse alpha pre-release versions."""
        v = Version.parse_semver("1.0.0-alpha.1")
        assert v.pkg_version.pre == ("a", 1)

    def test_beta_prerelease(self) -> None:
        """Should parse beta pre-release versions."""
        v = Version.parse_semver("1.0.0-beta.2")
        assert v.pkg_version.pre == ("b", 2)

    def test_rc_prerelease(self) -> None:
        """Should parse release candidate versions."""
        v = Version.parse_semver("1.0.0-rc.1")
        assert v.pkg_version.pre == ("rc", 1)

    def test_dev_prerelease(self) -> None:
        """Should parse dev pre-release versions."""
        v = Version.parse_semver("1.0.0-dev.5")
        assert v.pkg_version.dev == 5

    def test_build_metadata_post(self) -> None:
        """Should parse post release from build metadata."""
        v = Version.parse_semver("1.0.0+post.1")
        assert v.pkg_version.post == 1

    def test_build_metadata_local(self) -> None:
        """Should parse local version from build metadata."""
        v = Version.parse_semver("1.0.0+ubuntu1")
        assert v.pkg_version.local == "ubuntu1"

    def test_invalid_raises(self) -> None:
        """Should raise ValueError for invalid SemVer."""
        with pytest.raises(ValueError, match="Invalid SemVer"):
            Version.parse_semver("1.0")


class TestVersionParse:
    """Tests for Version.parse() - auto-detecting format."""

    def test_pep440_format(self) -> None:
        """Should parse PEP 440 format."""
        v = Version.parse("1.0.0a1")
        assert v.pkg_version.pre == ("a", 1)

    def test_semver_format(self) -> None:
        """Should parse SemVer format."""
        v = Version.parse("1.0.0-alpha.1")
        assert v.pkg_version.pre == ("a", 1)

    def test_stable_version(self) -> None:
        """Should parse stable versions (valid in both formats)."""
        v = Version.parse("1.0.0")
        assert v.pkg_version.release == (1, 0, 0)

    def test_invalid_raises(self) -> None:
        """Should raise ValueError for invalid version."""
        with pytest.raises(ValueError, match="Invalid version"):
            Version.parse("invalid")


class TestVersionPep440Str:
    """Tests for Version.pep440_str()."""

    def test_stable_version(self) -> None:
        """Should format stable version."""
        v = Version.parse_pep440("1.0.0")
        assert v.pep440_str() == "1.0.0"

    def test_prerelease(self) -> None:
        """Should format pre-release version."""
        v = Version.parse_pep440("1.0.0a1")
        assert v.pep440_str() == "1.0.0a1"

    def test_complex(self) -> None:
        """Should format complex version."""
        v = Version.parse_pep440("1.0.0rc1.post2.dev3+build")
        assert v.pep440_str() == "1.0.0rc1.post2.dev3+build"

    def test_from_semver(self) -> None:
        """SemVer input should output normalized PEP 440."""
        v = Version.parse_semver("1.0.0-alpha.1")
        assert v.pep440_str() == "1.0.0a1"


class TestVersionSemverStr:
    """Tests for Version.semver_str()."""

    def test_stable_version(self) -> None:
        """Should format stable version."""
        v = Version.parse_pep440("1.0.0")
        assert v.semver_str() == "1.0.0"

    def test_alpha_prerelease(self) -> None:
        """Alpha should convert to -alpha.N."""
        v = Version.parse_pep440("1.0.0a1")
        assert v.semver_str() == "1.0.0-alpha.1"

    def test_beta_prerelease(self) -> None:
        """Beta should convert to -beta.N."""
        v = Version.parse_pep440("1.0.0b2")
        assert v.semver_str() == "1.0.0-beta.2"

    def test_rc_prerelease(self) -> None:
        """RC should convert to -rc.N."""
        v = Version.parse_pep440("1.0.0rc1")
        assert v.semver_str() == "1.0.0-rc.1"

    def test_dev_release(self) -> None:
        """Dev should convert to -dev.N."""
        v = Version.parse_pep440("1.0.0.dev5")
        assert v.semver_str() == "1.0.0-dev.5"

    def test_prerelease_with_dev(self) -> None:
        """Pre-release with dev should combine in prerelease."""
        v = Version.parse_pep440("1.0.0a1.dev2")
        assert v.semver_str() == "1.0.0-alpha.1.dev.2"

    def test_post_release(self) -> None:
        """Post should convert to +post.N."""
        v = Version.parse_pep440("1.0.0.post1")
        assert v.semver_str() == "1.0.0+post.1"

    def test_post_with_dev(self) -> None:
        """Post with dev should put both in build."""
        v = Version.parse_pep440("1.0.0.post3.dev5")
        assert v.semver_str() == "1.0.0+post.3.dev.5"

    def test_local_only(self) -> None:
        """Local only should go to build."""
        v = Version.parse_pep440("1.0.0+abc123")
        assert v.semver_str() == "1.0.0+abc123"

    def test_post_with_local(self) -> None:
        """Post with local should combine in build."""
        v = Version.parse_pep440("1.0.0.post2+ubuntu1")
        assert v.semver_str() == "1.0.0+post.2.ubuntu1"

    def test_post_dev_with_local(self) -> None:
        """Post.dev with local should all go to build."""
        v = Version.parse_pep440("1.0.0.post3.dev5+abc123")
        assert v.semver_str() == "1.0.0+post.3.dev.5.abc123"

    def test_wrong_release_parts_raises(self) -> None:
        """Should raise ValueError if not 3 release parts."""
        v = Version.parse_pep440("1.0")
        with pytest.raises(ValueError, match="exactly 3 release parts"):
            v.semver_str()


class TestVersionComparison:
    """Tests for Version comparison operators."""

    def test_equal_pep440(self) -> None:
        """Same PEP 440 versions should be equal."""
        v1 = Version.parse_pep440("1.0.0a1")
        v2 = Version.parse_pep440("1.0.0a1")
        assert v1 == v2

    def test_equal_semver(self) -> None:
        """Same SemVer versions should be equal."""
        v1 = Version.parse_semver("1.0.0-alpha.1")
        v2 = Version.parse_semver("1.0.0-alpha.1")
        assert v1 == v2

    def test_equal_mixed_format(self) -> None:
        """PEP 440 and SemVer representing same version should be equal."""
        v1 = Version.parse_pep440("1.0.0a1")
        v2 = Version.parse_semver("1.0.0-alpha.1")
        assert v1 == v2

    def test_equal_with_pkg_version(self) -> None:
        """Should compare equal to packaging.version.Version."""
        v = Version.parse_pep440("1.0.0a1")
        pv = PkgVersion("1.0.0a1")
        assert v == pv

    def test_less_than(self) -> None:
        """Should compare less than correctly."""
        v1 = Version.parse_pep440("1.0.0a1")
        v2 = Version.parse_pep440("1.0.0")
        assert v1 < v2

    def test_less_than_or_equal(self) -> None:
        """Should compare less than or equal correctly."""
        v1 = Version.parse_pep440("1.0.0a1")
        v2 = Version.parse_pep440("1.0.0a1")
        v3 = Version.parse_pep440("1.0.0")
        assert v1 <= v2
        assert v1 <= v3

    def test_greater_than(self) -> None:
        """Should compare greater than correctly."""
        v1 = Version.parse_pep440("1.0.0")
        v2 = Version.parse_pep440("1.0.0a1")
        assert v1 > v2

    def test_greater_than_or_equal(self) -> None:
        """Should compare greater than or equal correctly."""
        v1 = Version.parse_pep440("1.0.0")
        v2 = Version.parse_pep440("1.0.0a1")
        v3 = Version.parse_pep440("1.0.0")
        assert v1 >= v2
        assert v1 >= v3

    def test_hash(self) -> None:
        """Should be hashable for use in sets/dicts."""
        v1 = Version.parse_pep440("1.0.0a1")
        v2 = Version.parse_semver("1.0.0-alpha.1")
        s = {v1, v2}
        assert len(s) == 1  # Same version, should dedupe


class TestVersionStr:
    """Tests for Version.__str__() and __repr__()."""

    def test_str(self) -> None:
        """__str__ should return PEP 440 format."""
        v = Version.parse_pep440("1.0.0a1")
        assert str(v) == "1.0.0a1"

    def test_repr(self) -> None:
        """__repr__ should return detailed representation."""
        v = Version.parse_pep440("1.0.0a1")
        assert repr(v) == "Version('1.0.0a1')"


class TestVersionPkgVersion:
    """Tests for Version.pkg_version property."""

    def test_pkg_version_accessible(self) -> None:
        """Should expose underlying packaging.version.Version."""
        v = Version.parse_pep440("1.0.0a1.post2.dev3+local")
        pv = v.pkg_version

        assert pv.release == (1, 0, 0)
        assert pv.pre == ("a", 1)
        assert pv.post == 2
        assert pv.dev == 3
        assert pv.local == "local"
        assert pv.epoch == 0
        assert pv.major == 1
        assert pv.minor == 0
        assert pv.micro == 0

    def test_pkg_version_is_immutable(self) -> None:
        """The underlying PkgVersion should be immutable."""
        v = Version.parse_pep440("1.0.0")
        pv = v.pkg_version

        with pytest.raises(AttributeError):
            pv.major = 2  # type: ignore[misc]


class TestVersionReleaseVersion:
    """Tests for Version.release_version() - stripping post/dev/local parts."""

    def test_stable_version_unchanged(self) -> None:
        """Stable version should be unchanged."""
        v = Version.parse_pep440("1.0.0")
        assert v.release_version() == v
        assert v.release_version().pep440_str() == "1.0.0"

    def test_prerelease_preserved(self) -> None:
        """Pre-release part should be preserved."""
        v = Version.parse_pep440("1.0.0a1")
        assert v.release_version().pep440_str() == "1.0.0a1"

        v = Version.parse_pep440("1.0.0b2")
        assert v.release_version().pep440_str() == "1.0.0b2"

        v = Version.parse_pep440("1.0.0rc1")
        assert v.release_version().pep440_str() == "1.0.0rc1"

    def test_post_stripped(self) -> None:
        """Post release part should be stripped."""
        v = Version.parse_pep440("1.0.0.post1")
        assert v.release_version().pep440_str() == "1.0.0"

    def test_dev_stripped(self) -> None:
        """Dev release part should be stripped."""
        v = Version.parse_pep440("1.0.0.dev5")
        assert v.release_version().pep440_str() == "1.0.0"

    def test_local_stripped(self) -> None:
        """Local version part should be stripped."""
        v = Version.parse_pep440("1.0.0+ubuntu1")
        assert v.release_version().pep440_str() == "1.0.0"

    def test_prerelease_with_post_dev_local(self) -> None:
        """Pre-release preserved, post/dev/local stripped."""
        # This is the typical dev version format
        v = Version.parse_pep440("0.1.0a3.post23.dev0+da749fc")
        assert v.release_version().pep440_str() == "0.1.0a3"

    def test_beta_with_post_dev(self) -> None:
        """Beta pre-release with post.dev should strip post.dev."""
        v = Version.parse_pep440("2.0.0b1.post5.dev2")
        assert v.release_version().pep440_str() == "2.0.0b1"

    def test_rc_with_local(self) -> None:
        """RC with local should strip local."""
        v = Version.parse_pep440("1.2.3rc1+build123")
        assert v.release_version().pep440_str() == "1.2.3rc1"

    def test_returns_version_instance(self) -> None:
        """Should return a Version instance, not a string."""
        v = Version.parse_pep440("1.0.0.post1")
        release = v.release_version()
        assert isinstance(release, Version)

    def test_release_version_has_no_post_dev_local(self) -> None:
        """Release version should have no post/dev/local parts."""
        v = Version.parse_pep440("1.0.0a1.post2.dev3+local")
        release = v.release_version()
        assert release.pkg_version.post is None
        assert release.pkg_version.dev is None
        assert release.pkg_version.local is None

    def test_semver_str_works_on_release_version(self) -> None:
        """Should be able to call semver_str() on release version."""
        v = Version.parse_pep440("0.1.0a3.post23.dev0+da749fc")
        release = v.release_version()
        assert release.semver_str() == "0.1.0-alpha.3"

    def test_dev_version_greater_than_release_version(self) -> None:
        """Dev version should be greater than its release version.

        This verifies that a full dev version (with post/dev/local parts)
        is considered "newer" than the corresponding release version when
        parsed with PEP 440 semantics. This is important for version
        comparison logic in installation and upgrade scenarios.
        """
        # Full dev version
        dev_version = Version.parse_pep440("0.1.0a3.post23.dev0+da749fc")
        # Corresponding release version
        release = dev_version.release_version()

        # Dev version should be >= release version in PEP 440 ordering
        # (post-releases are "newer" than their base version)
        assert dev_version >= release, f"Dev version {dev_version} should be >= release {release}"

        # Verify round-trip works
        semver_str = release.semver_str()
        reparsed = Version.parse(semver_str)
        assert reparsed == release, "Version should survive semver round-trip"


class TestSymmetricConversion:
    """Tests for round-trip conversion between PEP 440 and SemVer."""

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
    def test_pep440_to_semver_to_pep440(self, pep440: str) -> None:
        """PEP 440 → SemVer string → PEP 440 should preserve meaning."""
        v = Version.parse_pep440(pep440)
        semver_str = v.semver_str()
        v2 = Version.parse_semver(semver_str)
        assert v == v2, f"{pep440} → {semver_str} → {v2.pep440_str()}"

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
    def test_semver_to_pep440_to_semver(self, semver: str) -> None:
        """SemVer → Version → SemVer string should preserve meaning."""
        v = Version.parse_semver(semver)
        pep440_str = v.pep440_str()
        v2 = Version.parse_pep440(pep440_str)
        assert v == v2, f"{semver} → {pep440_str} → {v2.semver_str()}"
