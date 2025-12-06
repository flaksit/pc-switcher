"""Version utilities for pc-switcher.

This module provides utilities for:
- Determining the current pc-switcher version from package metadata
- Parsing version strings from CLI output
- Bidirectional conversion between PEP 440 and SemVer formats

For installation and upgrade logic on target machines, see pcswitcher.jobs.install_on_target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

import semver
from packaging.version import Version as PkgVersion

if TYPE_CHECKING:
    from typing import Self

__all__ = [
    "Pep440Version",
    "SemverVersion",
    "get_this_version",
    "is_semver_string",
    "parse_version_str_from_cli_output",
    "to_semver_str",
]

# PEP 440 pre-release type mapping
_PEP440_TO_SEMVER_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}
_SEMVER_TO_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}


@dataclass
class Pep440Version:
    """Parsed components of a PEP 440 version string.

    PEP 440 format: [N!]N(.N)*[{a|b|rc}N][.postN][.devN][+local]

    Attributes:
        release: Tuple of release numbers (e.g., (1, 0, 0))
        pre: Pre-release tuple (type, number) or None (e.g., ("a", 1) for "a1")
        post: Post-release number or None
        dev: Dev release number or None
        local: Local version string or None
    """

    release: tuple[int, ...]
    pre: tuple[str, int] | None = None  # ("a"|"b"|"rc", N)
    post: int | None = None
    dev: int | None = None
    local: str | None = None

    @classmethod
    def parse(cls, version_str: str) -> Self:
        """Parse a PEP 440 version string into components.

        Uses packaging.version.Version for robust PEP 440 parsing.

        Args:
            version_str: PEP 440 version string

        Returns:
            Pep440Version instance

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

        # Extract pre-release: packaging returns (type, number) or None
        # Type is normalized to 'a', 'b', or 'rc'
        pre: tuple[str, int] | None = None
        if parsed.pre is not None:
            pre_type, pre_num = parsed.pre
            # packaging normalizes alpha->a, beta->b, c/preview/pre->rc
            pre = (pre_type, pre_num)

        return cls(
            release=parsed.release,
            pre=pre,
            post=parsed.post,
            dev=parsed.dev,
            local=parsed.local,
        )

    def __str__(self) -> str:
        """Return the PEP 440 string representation."""
        result = ".".join(str(x) for x in self.release)

        if self.pre is not None:
            pre_type, pre_num = self.pre
            result += f"{pre_type}{pre_num}"

        if self.post is not None:
            result += f".post{self.post}"

        if self.dev is not None:
            result += f".dev{self.dev}"

        if self.local is not None:
            result += f"+{self.local}"

        return result

    def to_semver(self) -> SemverVersion:
        """Convert to SemVer format.

        Conversion rules:
        - Release X.Y.Z → X.Y.Z (must be exactly 3 parts)
        - Pre-release aN/bN/rcN → -alpha.N/-beta.N/-rc.N
        - Dev release .devN → -dev.N (added to prerelease, but only if no post)
        - Post release .postN[.devM] → +post.N[.dev.M] (in build metadata)
        - Local version +xxx → appended to build metadata after post/dev

        Returns:
            SemverVersion instance

        Raises:
            ValueError: If release doesn't have exactly 3 parts
        """
        if len(self.release) != 3:
            raise ValueError(
                f"PEP 440 version must have exactly 3 release parts for SemVer conversion: {self}"
            )

        major, minor, patch = self.release

        # Build prerelease part
        prerelease: str | None = None
        prerelease_parts: list[str] = []

        if self.pre is not None:
            pre_type, pre_num = self.pre
            semver_pre = _PEP440_TO_SEMVER_PRE[pre_type]
            prerelease_parts.append(semver_pre)
            prerelease_parts.append(str(pre_num))

        # Dev goes to prerelease only if there's no post release
        # If there's post, dev goes with post in build metadata
        if self.dev is not None and self.post is None:
            prerelease_parts.append("dev")
            prerelease_parts.append(str(self.dev))

        if prerelease_parts:
            prerelease = ".".join(prerelease_parts)

        # Build metadata: post[.dev].local
        build: str | None = None
        build_parts: list[str] = []

        if self.post is not None:
            build_parts.append("post")
            build_parts.append(str(self.post))
            # If we have post, dev goes here too
            if self.dev is not None:
                build_parts.append("dev")
                build_parts.append(str(self.dev))

        if self.local is not None:
            build_parts.append(self.local)

        if build_parts:
            build = ".".join(build_parts)

        return SemverVersion(
            major=major,
            minor=minor,
            patch=patch,
            prerelease=prerelease,
            build=build,
        )


@dataclass
class SemverVersion:
    """Parsed components of a SemVer version string.

    SemVer format: X.Y.Z[-prerelease][+build]

    Attributes:
        major: Major version number
        minor: Minor version number
        patch: Patch version number
        prerelease: Pre-release string (dot-separated identifiers) or None
        build: Build metadata string (dot-separated identifiers) or None
    """

    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    build: str | None = None

    @classmethod
    def parse(cls, version_str: str) -> Self:
        """Parse a SemVer version string into components.

        Uses the semver package for robust parsing.

        Args:
            version_str: SemVer version string

        Returns:
            SemverVersion instance

        Raises:
            ValueError: If version string is not valid SemVer
        """
        try:
            parsed = semver.Version.parse(version_str)
        except ValueError as e:
            raise ValueError(f"Invalid SemVer version: {version_str}") from e

        return cls(
            major=parsed.major,
            minor=parsed.minor,
            patch=parsed.patch,
            prerelease=parsed.prerelease,
            build=parsed.build,
        )

    def __str__(self) -> str:
        """Return the SemVer string representation."""
        result = f"{self.major}.{self.minor}.{self.patch}"

        if self.prerelease is not None:
            result += f"-{self.prerelease}"

        if self.build is not None:
            result += f"+{self.build}"

        return result

    def to_pep440(self) -> Pep440Version:
        """Convert to PEP 440 format.

        Conversion rules (reverse of Pep440Version.to_semver):
        - Release X.Y.Z → X.Y.Z
        - Pre-release -alpha.N/-beta.N/-rc.N → aN/bN/rcN
        - Pre-release -dev.N → .devN
        - Build metadata +post.N[.dev.M][.local] → .postN[.devM][+local]

        Returns:
            Pep440Version instance

        Raises:
            ValueError: If prerelease format is unrecognized
        """
        pre: tuple[str, int] | None = None
        dev: int | None = None
        post: int | None = None
        local: str | None = None

        # Parse prerelease
        if self.prerelease is not None:
            pre, dev = self._parse_prerelease(self.prerelease)

        # Parse build metadata for post, dev (with post), and local
        if self.build is not None:
            post, build_dev, local = self._parse_build(self.build)
            # If dev came from build (with post), use that
            if build_dev is not None:
                dev = build_dev

        return Pep440Version(
            release=(self.major, self.minor, self.patch),
            pre=pre,
            post=post,
            dev=dev,
            local=local,
        )

    def _parse_prerelease(self, prerelease: str) -> tuple[tuple[str, int] | None, int | None]:
        """Parse SemVer prerelease into PEP 440 pre and dev components.

        Args:
            prerelease: Prerelease string (e.g., "alpha.1", "alpha.1.dev.2")

        Returns:
            Tuple of (pre, dev) where pre is (type, num) or None

        Raises:
            ValueError: If prerelease format is unrecognized
        """
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

    def _parse_build(self, build: str) -> tuple[int | None, int | None, str | None]:
        """Parse SemVer build metadata into PEP 440 post, dev, and local.

        Build format: [post.N[.dev.M]][.local...]
        - "post.N" or "post.N.dev.M" at the start indicates post/dev release
        - Everything after that is local version

        Args:
            build: Build metadata string

        Returns:
            Tuple of (post, dev, local)
        """
        parts = build.split(".")
        post: int | None = None
        dev: int | None = None
        local_start: int | None = None

        i = 0

        # Check for post.N at start
        if (
            i < len(parts)
            and parts[i] == "post"
            and i + 1 < len(parts)
            and parts[i + 1].isdigit()
        ):
            post = int(parts[i + 1])
            i += 2

            # Check for dev.M after post
            if (
                i < len(parts)
                and parts[i] == "dev"
                and i + 1 < len(parts)
                and parts[i + 1].isdigit()
            ):
                dev = int(parts[i + 1])
                i += 2

        # Everything remaining is local
        if i < len(parts):
            local_start = i

        local = ".".join(parts[local_start:]) if local_start is not None else None

        return post, dev, local


def get_this_version() -> str:
    """Get the version of pc-switcher currently running.

    Returns:
        Version string from package metadata

    Raises:
        PackageNotFoundError: If package metadata cannot be read
    """
    try:
        return version("pcswitcher")
    except PackageNotFoundError as e:
        raise PackageNotFoundError(
            "Cannot determine pc-switcher version. Package metadata not found. Is pc-switcher installed correctly?"
        ) from e


def parse_version_str_from_cli_output(output: str) -> str:
    """Parse version string from pc-switcher --version output.

    Supports both SemVer format (0.1.0-alpha.1) and PEP 440 format (0.1.0a1).

    Args:
        output: Command output (e.g., "pc-switcher 0.4.0" or "0.4.0-alpha.1" or "0.1.0a1")

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
        >>> parse_version_str_from_cli_output("0.1.0-rc.2")
        '0.1.0-rc.2'
    """
    # Matches both formats:
    # - SemVer: MAJOR.MINOR.PATCH[-prerelease][+build] (e.g., 0.1.0-alpha.1, 0.1.0-rc.2+build)
    # - PEP 440: MAJOR.MINOR.PATCH[{a|b|rc}N][.postN][.devN][+local] (e.g., 0.1.0a1, 0.1.0.post1)
    # The regex captures PEP 440 pre-release (a/b/rc followed by digits) directly after patch version
    match = re.search(r"(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:[-+.][\w.]+)*)", output)
    if not match:
        raise ValueError(f"Cannot parse version from output: {output}")
    return match.group(1)


def is_semver_string(version_str: str) -> bool:
    """Check if a string is a valid SemVer version.

    Args:
        version_str: Version string to check

    Returns:
        True if valid SemVer, False otherwise
    """
    try:
        semver.Version.parse(version_str)
        return True
    except ValueError:
        return False


def to_semver_str(version_str: str) -> str:
    """Convert a version string to SemVer format.

    Args:
        version_str: Version string in PEP 440 or SemVer format

    Returns:
        Version string in SemVer format

    Raises:
        ValueError: If version string is invalid

    Examples:
        >>> to_semver_str("0.1.0a1")
        '0.1.0-alpha.1'
        >>> to_semver_str("0.1.0-alpha.1")
        '0.1.0-alpha.1'
        >>> to_semver_str("1.0.0.post2+ubuntu1")
        '1.0.0+post.2.ubuntu1'
    """
    # Check if it's already valid SemVer
    if is_semver_string(version_str):
        return version_str

    # Try to parse as PEP 440 and convert
    pep440 = Pep440Version.parse(version_str)
    return str(pep440.to_semver())
