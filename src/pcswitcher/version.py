"""Version and release utilities for pc-switcher.

This module provides utilities for:
- Determining the current pc-switcher version from package metadata
- Parsing version strings from CLI output
- A unified Version class supporting both PEP 440 and SemVer formats
- Querying releases (set GITHUB_TOKEN env var to avoid rate limits)

PEP 440 epochs (N!X.Y.Z) are not supported.

For installation and upgrade logic on target machines, see pcswitcher.jobs.install_on_target.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as get_pkg_version
from typing import TYPE_CHECKING, Literal

import semver
from github import Auth, Github
from packaging.version import Version as PkgVersion

if TYPE_CHECKING:
    from typing import Self

logger = logging.getLogger(__name__)

__all__ = [
    "Release",
    "Version",
    "find_one_version",
    "get_highest_release",
    "get_releases",
    "get_this_version",
]

# PEP 440 pre-release type mapping
_PEP440_TO_SEMVER_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}
_SEMVER_TO_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}

_VERSION_REGEX = re.compile(
    r"(?<![^.,+\s-])"  # Start of string or preceded by whitespace or allowed punctuation
    r"(?P<version>"
    # PEP440 format
    r"v?(?:(?P<epoch>[0-9]+)!)?(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?P<pre>[-._]?(?P<pre_l>alpha|beta|preview|pre|a|b|c|rc)[-._]?(?P<pre_n>[0-9]+)?)?"
    r"(?P<post>(?:-(?P<post_n1>[0-9]+))|(?:[-._]?(?P<post_l>post|rev|r)[-._]?(?P<post_n2>[0-9]+)?))?"
    r"(?P<dev>[-._]?dev[-._]?(?P<dev_n>[0-9]+)?)?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-._][a-z0-9]+)*))?"
    r"|"
    # SemVer format
    r"v?(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>(?:0|[1-9][0-9]*|[0-9]*[a-z-][0-9a-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9]*[a-z-][0-9a-z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-z-]+(?:\.[0-9a-z-]+)*))?"
    r")"
    r"(?![^.,+\s-])"  # End of string or followed by whitespace or allowed punctuation
)


@dataclass(frozen=True)
class Release:
    """Immutable release information.

    Attributes:
        version: The parsed version of the release
        is_prerelease: Whether the release is a prerelease
        tag: The exact GitHub release tag (e.g., "v0.1.0", "v0.1.0-alpha.1"), or None if not from GitHub
    """

    version: Version
    is_prerelease: bool
    tag: str | None = None


def get_releases(
    repository: str = "flaksit/pc-switcher",
    *,
    include_prereleases: bool = True,
) -> Iterator[Release]:
    """Fetch all releases from GitHub API with pagination.

    Args:
        include_prereleases: If True, include pre-release versions
        repository: GitHub repository in "owner/repo" format

    Returns:
        List of all non-draft releases

    Raises:
        RuntimeError: If GitHub API request fails

    Note:
        If GITHUB_TOKEN environment variable is set, authenticated requests
        are used (5000 requests/hour). Otherwise, unauthenticated requests
        are used (60 requests/hour).
    """
    try:
        # Use GITHUB_TOKEN if available for higher rate limit (5000/hour vs 60/hour)
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            logger.debug("Using GITHUB_TOKEN for authenticated GitHub API access (5000 req/hr)")
        else:
            logger.debug("GITHUB_TOKEN not set, using unauthenticated GitHub API (60 req/hr)")

        g = Github(auth=Auth.Token(token)) if token else Github()
        repo = g.get_repo(repository)

        # Fetch all releases with pagination
        for release in repo.get_releases():
            # Skip drafts
            if release.draft:
                continue

            # Filter prereleases if requested
            if not include_prereleases and release.prerelease:
                continue

            try:
                version = Version.parse(release.tag_name)
            except ValueError:
                logger.warning(f"Skipping release with invalid version tag: {release.tag_name}")
                continue
            yield Release(version, release.prerelease, release.tag_name)

    except Exception as e:
        raise RuntimeError(f"Failed to fetch GitHub releases: {e}") from e


def get_highest_release(
    repository: str = "flaksit/pc-switcher",
    *,
    include_prereleases: bool = True,
) -> Release:
    """Fetch the latest release from GitHub API.

    Args:
        include_prereleases: If True, include pre-release versions
        repository: GitHub repository in "owner/repo" format

    Returns:
        The latest non-draft release

    Raises:
        RuntimeError: If GitHub API request fails or no releases found
    """
    releases = get_releases(repository, include_prereleases=include_prereleases)
    result = max(releases, key=lambda r: r.version, default=None)
    if result is None:
        raise RuntimeError("No releases found in the repository")
    return result


class Version:
    """Unified version class supporting both PEP 440 and SemVer formats.

    Internally backed by packaging.version.Version for robust parsing,
    comparison, and normalization. Provides formatting methods for both
    PEP 440 and SemVer output formats.

    This class is effectively immutable - all attributes are exposed via
    read-only properties and there are no mutating methods.

    Attributes:
        pkg_version: The underlying packaging.version.Version object.
            Provides access to .release, .pre, .post, .dev, .local, .epoch.
        original: The original version string that was used to create this Version.
        parsed_as: How the version was parsed ('pep440' or 'semver').

    Examples:
        >>> v = Version.parse_pep440("1.0.0a1")
        >>> v.semver_str()
        '1.0.0-alpha.1'
        >>> v.pep440_str()
        '1.0.0a1'
        >>> v.original
        '1.0.0a1'
        >>> v.parsed_as
        'pep440'

        >>> v = Version.parse_semver("1.0.0-beta.2")
        >>> v.pep440_str()
        '1.0.0b2'
        >>> v.original
        '1.0.0-beta.2'
        >>> v.parsed_as
        'semver'

        >>> v1 = Version.parse("1.0.0a1")
        >>> v2 = Version.parse("1.0.0-alpha.1")
        >>> v1 == v2
        True
    """

    __slots__ = ("_original", "_parsed_as", "_pkg_version")

    _original: str
    _parsed_as: Literal["pep440", "semver"]
    _pkg_version: PkgVersion

    def __init__(
        self,
        pkg_version: PkgVersion,
        original: str,
        parsed_as: Literal["pep440", "semver"],
    ) -> None:
        """Initialize Version with a packaging.version.Version.

        Args:
            pkg_version: The backing packaging.version.Version object.
            original: The original version string used to create this Version.
            parsed_as: How the version was parsed ('pep440' or 'semver').

        Note:
            Use the class methods parse_pep440(), parse_semver(), or parse()
            instead of calling this constructor directly.
        """
        self._pkg_version = pkg_version
        self._original = original
        self._parsed_as = parsed_as

    @property
    def pkg_version(self) -> PkgVersion:
        """The underlying packaging.version.Version object.

        This is immutable (properties without setters), so safe to expose.
        Provides access to: .release, .pre, .post, .dev, .local, .epoch,
        .major, .minor, .micro, .public, .base_version, .is_prerelease,
        .is_postrelease, .is_devrelease.
        """
        return self._pkg_version

    @property
    def original(self) -> str:
        """The original version string used to create this Version."""
        return self._original

    @property
    def parsed_as(self) -> Literal["pep440", "semver"]:
        """How the version was parsed ('pep440' or 'semver')."""
        return self._parsed_as

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

        return cls(parsed, original=version_str, parsed_as="pep440")

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
        return cls(PkgVersion(pep440_str), original=version_str, parsed_as="semver")

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

    def get_release_floor(self, *, include_prereleases: bool = True) -> Release:
        """Return the latest GitHub release that is <= this version.

        Queries GitHub API to find the greatest release tag that is <= this version.
        This is the "floor" of this version in the set of GitHub releases.

        Args:
            include_prereleases: If True, include pre-release versions

        For example:
        - "0.1.0a3.post23.dev0+da749fc" -> Release for "0.1.0a3" if that's a release
        - "0.1.0.post1" -> Release for "0.1.0" if that's a release
        - "0.2.0" -> Release for "0.1.0" if 0.2.0 isn't released yet

        Returns:
            Release object representing the GitHub release

        Raises:
            RuntimeError: If GitHub API fails or no matching release found
        """
        # Fetch releases
        all_releases = get_releases(include_prereleases=include_prereleases)

        # Find releases <= this version
        candidates = [release for release in all_releases if release.version <= self]

        if not candidates:
            raise RuntimeError(
                f"No GitHub release found matching version {self}. "
                f"This version may not correspond to any published release."
            )

        # Return the highest (most precise/recent) match
        return max(candidates, key=lambda r: r.version)

    def get_release(self, *, include_prereleases: bool = True) -> Release | None:
        """Get the GitHub Release for this exact version if it exists.

        Args:
            include_prereleases: If True, include pre-release versions

        Returns:
            Release object if this version is an exact GitHub release, None otherwise

        Raises:
            RuntimeError: If GitHub API fails
        """
        # Fetch releases
        releases = get_releases(include_prereleases=include_prereleases)

        # Check for exact match
        for release in releases:
            if release.version == self:
                return release
        return None

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


def get_this_version() -> Version:
    """Get the version of pc-switcher currently running.

    Returns:
        Version object from package metadata (PEP 440 format)

    Raises:
        PackageNotFoundError: If package metadata cannot be read
    """
    try:
        version_str = get_pkg_version("pcswitcher")
        return Version.parse_pep440(version_str)
    except PackageNotFoundError as e:
        raise PackageNotFoundError(
            "Cannot determine pc-switcher version. Package metadata not found. Is pc-switcher installed correctly?"
        ) from e


def _find_one_version_str(text: str) -> str:
    """Return a single first version string in text, raising if found none or multiple.
    Args:
        text: Text to search for version strings
    Returns:
        The first version string found
    Raises:
        ValueError: If no version strings or multiple version strings are found
    """
    matches = _VERSION_REGEX.finditer(text)

    # get the first match
    match = next(matches, None)

    if match:
        # if there are more matches, fail
        if next(matches, None):
            raise ValueError("Multiple version strings found in text")
        else:
            return match.group("version")
    else:
        raise ValueError("No version string found in text")


def find_one_version(text: str) -> Version:
    """Return a single Version instance parsed from text, raising if none or multiple found.

    Supports both SemVer format (0.1.0-alpha.1) and PEP 440 format (0.1.0a1).

    Args:
        output: Command output (e.g., "pc-switcher 0.4.0" or "0.4.0-alpha.1")

    Returns:
        Version string (e.g., "0.4.0", "0.4.0-alpha.1", or "0.1.0a1")

    Raises:
        ValueError: If version string cannot be parsed

    Examples:
        >>> find_one_version("pc-switcher 0.1.0")
        '0.1.0'
        >>> find_one_version("pc-switcher v0.1.0-alpha.1  # this is a pre-release")
        '0.1.0-alpha.1'
        >>> find_one_version("pc-switcher 0.1.0a1")
        '0.1.0a1'
    """
    version_str = _find_one_version_str(text)
    return Version.parse(version_str)
