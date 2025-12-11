"""Version utilities for pc-switcher.

This module provides utilities for:
- Determining the current pc-switcher version from package metadata
- Parsing version strings from CLI output
- A unified Version class supporting both PEP 440 and SemVer formats

For installation and upgrade logic on target machines, see pcswitcher.jobs.install_on_target.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as get_pkg_version
from typing import TYPE_CHECKING

import semver
from packaging.version import Version as PkgVersion

if TYPE_CHECKING:
    from typing import Self

__all__ = [
    "Version",
    "get_this_version",
    "parse_version_str_from_cli_output",
]

# PEP 440 pre-release type mapping
_PEP440_TO_SEMVER_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}
_SEMVER_TO_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}


class Version:
    """Unified version class supporting both PEP 440 and SemVer formats.

    Internally backed by packaging.version.Version for robust parsing,
    comparison, and normalization. Provides formatting methods for both
    PEP 440 and SemVer output formats.

    The backing packaging.version.Version is immutable (properties without
    setters), so it's safe to expose directly.

    Attributes:
        pkg_version: The underlying packaging.version.Version object.
            Provides access to .release, .pre, .post, .dev, .local, .epoch.

    Examples:
        >>> v = Version.parse_pep440("1.0.0a1")
        >>> v.semver_str()
        '1.0.0-alpha.1'
        >>> v.pep440_str()
        '1.0.0a1'

        >>> v = Version.parse_semver("1.0.0-beta.2")
        >>> v.pep440_str()
        '1.0.0b2'

        >>> v1 = Version.parse("1.0.0a1")
        >>> v2 = Version.parse("1.0.0-alpha.1")
        >>> v1 == v2
        True
    """

    __slots__ = ("_pkg_version",)

    def __init__(self, pkg_version: PkgVersion) -> None:
        """Initialize Version with a packaging.version.Version.

        Args:
            pkg_version: The backing packaging.version.Version object.

        Note:
            Use the class methods parse_pep440(), parse_semver(), or parse()
            instead of calling this constructor directly.
        """
        self._pkg_version = pkg_version

    @property
    def pkg_version(self) -> PkgVersion:
        """The underlying packaging.version.Version object.

        This is immutable (properties without setters), so safe to expose.
        Provides access to: .release, .pre, .post, .dev, .local, .epoch,
        .major, .minor, .micro, .public, .base_version, .is_prerelease,
        .is_postrelease, .is_devrelease.
        """
        return self._pkg_version

    @classmethod
    def parse_pep440(cls, version_str: str) -> Self:
        """Parse a PEP 440 version string.

        Args:
            version_str: PEP 440 version string (e.g., "1.0.0", "1.0.0a1", "1.0.0.post1")

        Returns:
            Version instance

        Raises:
            ValueError: If version string is invalid or uses epoch
        """
        try:
            parsed = PkgVersion(version_str)
        except Exception as e:
            raise ValueError(f"Invalid PEP 440 version: {version_str}") from e

        # Check for epoch (not supported for SemVer conversion)
        if parsed.epoch != 0:
            raise ValueError(f"PEP 440 epoch is not supported: {version_str}")

        return cls(parsed)

    @classmethod
    def parse_semver(cls, version_str: str) -> Self:
        """Parse a SemVer version string.

        Uses the semver package to validate the format, then converts to
        the internal PEP 440 representation.

        Args:
            version_str: SemVer version string (e.g., "1.0.0", "1.0.0-alpha.1")

        Returns:
            Version instance

        Raises:
            ValueError: If version string is not valid SemVer
        """
        try:
            parsed = semver.Version.parse(version_str)
        except ValueError as e:
            raise ValueError(f"Invalid SemVer version: {version_str}") from e

        # Convert SemVer to PEP 440 string, then parse with packaging
        pep440_str = cls._semver_to_pep440_str(parsed)
        return cls(PkgVersion(pep440_str))

    @classmethod
    def parse(cls, version_str: str) -> Self:
        """Parse a version string in either PEP 440 or SemVer format.

        Tries SemVer first (stricter format), then falls back to PEP 440.

        Args:
            version_str: Version string in either format

        Returns:
            Version instance

        Raises:
            ValueError: If version string is invalid in both formats
        """
        # Try SemVer first (more strict format)
        try:
            return cls.parse_semver(version_str)
        except ValueError:
            pass

        # Fall back to PEP 440
        try:
            return cls.parse_pep440(version_str)
        except ValueError:
            pass

        raise ValueError(f"Invalid version string: {version_str}")

    def pep440_str(self) -> str:
        """Return the version in PEP 440 format.

        Returns:
            Normalized PEP 440 version string (e.g., "1.0.0a1", "1.0.0.post1")
        """
        return str(self._pkg_version)

    def release_version(self) -> Self:
        """Return the release version, stripping post/dev/local parts.

        This extracts the base release version that would have a corresponding
        git tag. For example:
        - "0.1.0a3.post23.dev0+da749fc" -> "0.1.0a3"
        - "1.0.0.post1" -> "1.0.0"
        - "1.0.0" -> "1.0.0"

        Returns:
            Version instance with only release and pre-release parts
        """
        pv = self._pkg_version

        # Build version string with only release and pre parts
        result = ".".join(str(x) for x in pv.release)

        if pv.pre is not None:
            pre_type, pre_num = pv.pre
            result += f"{pre_type}{pre_num}"

        return self.parse_pep440(result)

    def is_release_version(self) -> bool:
        """Check if the version is a release version (no pre/post/dev/local parts).

        Returns:
            True if release version, False otherwise
        """
        return self.release_version() == self

    def semver_str(self) -> str:
        """Return the version in SemVer format.

        Conversion rules:
        - Release X.Y.Z → X.Y.Z (must be exactly 3 parts)
        - Pre-release aN/bN/rcN → -alpha.N/-beta.N/-rc.N
        - Dev release .devN → -dev.N (in prerelease, unless post is present)
        - Post release .postN[.devM] → +post.N[.dev.M] (in build metadata)
        - Local version +xxx → appended to build metadata

        Returns:
            SemVer version string (e.g., "1.0.0-alpha.1", "1.0.0+post.1")

        Raises:
            ValueError: If release doesn't have exactly 3 parts
        """
        pv = self._pkg_version

        if len(pv.release) != 3:
            raise ValueError(f"Version must have exactly 3 release parts for SemVer: {pv}")

        major, minor, patch = pv.release

        # Build prerelease part
        prerelease_parts: list[str] = []

        if pv.pre is not None:
            pre_type, pre_num = pv.pre
            semver_pre = _PEP440_TO_SEMVER_PRE[pre_type]
            prerelease_parts.append(semver_pre)
            prerelease_parts.append(str(pre_num))

        # Dev goes to prerelease only if there's no post release
        if pv.dev is not None and pv.post is None:
            prerelease_parts.append("dev")
            prerelease_parts.append(str(pv.dev))

        prerelease = ".".join(prerelease_parts) if prerelease_parts else None

        # Build metadata: post[.dev].local
        build_parts: list[str] = []

        if pv.post is not None:
            build_parts.append("post")
            build_parts.append(str(pv.post))
            # If we have post, dev goes here too
            if pv.dev is not None:
                build_parts.append("dev")
                build_parts.append(str(pv.dev))

        if pv.local is not None:
            build_parts.append(pv.local)

        build = ".".join(build_parts) if build_parts else None

        # Construct SemVer string
        result = f"{major}.{minor}.{patch}"
        if prerelease:
            result += f"-{prerelease}"
        if build:
            result += f"+{build}"

        return result

    @staticmethod
    def _semver_to_pep440_str(sv: semver.Version) -> str:
        """Convert a semver.Version to a PEP 440 string.

        Args:
            sv: semver.Version instance

        Returns:
            PEP 440 version string
        """
        result = f"{sv.major}.{sv.minor}.{sv.patch}"

        # Parse prerelease
        pre: tuple[str, int] | None = None
        dev: int | None = None

        if sv.prerelease is not None:
            pre, dev = Version._parse_semver_prerelease(sv.prerelease)

        # Parse build metadata for post, dev (with post), and local
        post: int | None = None
        local: str | None = None

        if sv.build is not None:
            post, build_dev, local = Version._parse_semver_build(sv.build)
            if build_dev is not None:
                dev = build_dev

        # Build PEP 440 string
        if pre is not None:
            pre_type, pre_num = pre
            result += f"{pre_type}{pre_num}"

        if post is not None:
            result += f".post{post}"

        if dev is not None:
            result += f".dev{dev}"

        if local is not None:
            result += f"+{local}"

        return result

    @staticmethod
    def _parse_semver_prerelease(prerelease: str) -> tuple[tuple[str, int] | None, int | None]:
        """Parse SemVer prerelease into PEP 440 pre and dev components."""
        parts = prerelease.split(".")
        pre: tuple[str, int] | None = None
        dev: int | None = None

        i = 0
        while i < len(parts):
            part = parts[i]
            if part in _SEMVER_TO_PEP440_PRE:
                if i + 1 >= len(parts):
                    raise ValueError(f"Pre-release type '{part}' missing number")
                pre = (_SEMVER_TO_PEP440_PRE[part], int(parts[i + 1]))
                i += 2
            elif part == "dev":
                if i + 1 >= len(parts):
                    raise ValueError("Dev release missing number")
                dev = int(parts[i + 1])
                i += 2
            else:
                raise ValueError(f"Unrecognized prerelease identifier '{part}'")

        return pre, dev

    @staticmethod
    def _parse_semver_build(build: str) -> tuple[int | None, int | None, str | None]:
        """Parse SemVer build metadata into PEP 440 post, dev, and local."""
        parts = build.split(".")
        post: int | None = None
        dev: int | None = None
        local_start: int | None = None

        i = 0

        # Check for post.N at start
        if i < len(parts) and parts[i] == "post" and i + 1 < len(parts) and parts[i + 1].isdigit():
            post = int(parts[i + 1])
            i += 2

            # Check for dev.M after post
            if i < len(parts) and parts[i] == "dev" and i + 1 < len(parts) and parts[i + 1].isdigit():
                dev = int(parts[i + 1])
                i += 2

        # Everything remaining is local
        if i < len(parts):
            local_start = i

        local = ".".join(parts[local_start:]) if local_start is not None else None

        return post, dev, local

    def __str__(self) -> str:
        """Return the PEP 440 string representation (canonical format)."""
        return self.pep440_str()

    def __repr__(self) -> str:
        """Return a detailed representation."""
        return f"Version({self.pep440_str()!r})"

    def __eq__(self, other: object) -> bool:
        """Compare versions for equality."""
        if isinstance(other, Version):
            return self._pkg_version == other._pkg_version
        if isinstance(other, PkgVersion):
            return self._pkg_version == other
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        """Compare versions (less than)."""
        if isinstance(other, Version):
            return self._pkg_version < other._pkg_version
        if isinstance(other, PkgVersion):
            return self._pkg_version < other
        return NotImplemented

    def __le__(self, other: object) -> bool:
        """Compare versions (less than or equal)."""
        if isinstance(other, Version):
            return self._pkg_version <= other._pkg_version
        if isinstance(other, PkgVersion):
            return self._pkg_version <= other
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        """Compare versions (greater than)."""
        if isinstance(other, Version):
            return self._pkg_version > other._pkg_version
        if isinstance(other, PkgVersion):
            return self._pkg_version > other
        return NotImplemented

    def __ge__(self, other: object) -> bool:
        """Compare versions (greater than or equal)."""
        if isinstance(other, Version):
            return self._pkg_version >= other._pkg_version
        if isinstance(other, PkgVersion):
            return self._pkg_version >= other
        return NotImplemented

    def __hash__(self) -> int:
        """Return hash for use in sets and dicts."""
        return hash(self._pkg_version)


def get_this_version() -> str:
    """Get the version of pc-switcher currently running.

    Returns:
        Version string from package metadata (PEP 440 format)

    Raises:
        PackageNotFoundError: If package metadata cannot be read
    """
    try:
        return get_pkg_version("pcswitcher")
    except PackageNotFoundError as e:
        raise PackageNotFoundError(
            "Cannot determine pc-switcher version. Package metadata not found. Is pc-switcher installed correctly?"
        ) from e


def parse_version_str_from_cli_output(output: str) -> str:
    """Parse version string from pc-switcher --version output.

    Supports both SemVer format (0.1.0-alpha.1) and PEP 440 format (0.1.0a1).

    Args:
        output: Command output (e.g., "pc-switcher 0.4.0" or "0.4.0-alpha.1")

    Returns:
        Version string (e.g., "0.4.0", "0.4.0-alpha.1", or "0.1.0a1")

    Raises:
        ValueError: If version string cannot be parsed

    Examples:
        >>> parse_version_str_from_cli_output("pc-switcher 0.1.0")
        '0.1.0'
        >>> parse_version_str_from_cli_output("pc-switcher 0.1.0-alpha.1")
        '0.1.0-alpha.1'
        >>> parse_version_str_from_cli_output("pc-switcher 0.1.0a1")
        '0.1.0a1'
    """
    # Matches both formats:
    # - SemVer: MAJOR.MINOR.PATCH[-prerelease][+build]
    # - PEP 440: MAJOR.MINOR.PATCH[{a|b|rc}N][.postN][.devN][+local]
    match = re.search(r"(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:[-+.][\w.]+)*)", output)
    if not match:
        raise ValueError(f"Cannot parse version from output: {output}")
    return match.group(1)
