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

__all__ = [
    "get_this_version",
    "parse_version_from_cli_output",
    "pep440_to_semver",
    "semver_to_pep440",
]

# PEP 440 pre-release type mapping
_PEP440_TO_SEMVER_PRE = {"a": "alpha", "b": "beta", "rc": "rc"}
_SEMVER_TO_PEP440_PRE = {"alpha": "a", "beta": "b", "rc": "rc"}


@dataclass
class ParsedPep440:
    """Parsed components of a PEP 440 version string."""

    release: tuple[int, ...]
    pre: tuple[str, int] | None = None  # ("a"|"b"|"rc", N)
    post: int | None = None
    dev: int | None = None
    local: str | None = None


@dataclass
class ParsedSemVer:
    """Parsed components of a SemVer version string."""

    major: int
    minor: int
    patch: int
    prerelease: list[str] | None = None  # dot-separated identifiers
    build: list[str] | None = None  # dot-separated identifiers


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


def parse_version_from_cli_output(output: str) -> str:
    """Parse version string from pc-switcher --version output.

    Supports both SemVer format (0.1.0-alpha.1) and PEP 440 format (0.1.0a1).

    Args:
        output: Command output (e.g., "pc-switcher 0.4.0" or "0.4.0-alpha.1" or "0.1.0a1")

    Returns:
        Version string (e.g., "0.4.0", "0.4.0-alpha.1", or "0.1.0a1")

    Raises:
        ValueError: If version string cannot be parsed

    Examples:
        >>> parse_version_from_cli_output("pc-switcher 0.1.0")
        '0.1.0'
        >>> parse_version_from_cli_output("pc-switcher 0.1.0-alpha.1")
        '0.1.0-alpha.1'
        >>> parse_version_from_cli_output("pc-switcher 0.1.0a1")
        '0.1.0a1'
        >>> parse_version_from_cli_output("0.1.0-rc.2")
        '0.1.0-rc.2'
    """
    # Matches both formats:
    # - SemVer: MAJOR.MINOR.PATCH[-prerelease[.number]] (e.g., 0.1.0-alpha.1, 0.1.0-rc.2)
    # - PEP 440: MAJOR.MINOR.PATCH[{a|b|rc}N] (e.g., 0.1.0a1, 0.2.0b2, 1.0.0rc1)
    match = re.search(r"(\d+\.\d+\.\d+(?:(?:-[\w.]+)|(?:(?:a|b|rc)\d+))?)", output)
    if not match:
        raise ValueError(f"Cannot parse version from output: {output}")
    return match.group(1)


def _parse_pep440(version_str: str) -> ParsedPep440:
    """Parse a PEP 440 version string into components.

    PEP 440 format: [N!]N(.N)*[{a|b|rc}N][.postN][.devN][+local]

    Args:
        version_str: PEP 440 version string

    Returns:
        ParsedPep440 with all components

    Raises:
        ValueError: If version string is invalid or uses epoch
    """
    # Check for epoch (not supported)
    if "!" in version_str:
        raise ValueError(f"PEP 440 epoch is not supported: {version_str}")

    # Split off local version
    local = None
    if "+" in version_str:
        version_str, local = version_str.split("+", 1)

    # Full PEP 440 regex (without epoch)
    # Release: N(.N)*
    # Pre: (a|b|rc)N
    # Post: .postN
    # Dev: .devN
    pattern = r"""
        ^
        (\d+(?:\.\d+)*)           # Release segment (group 1)
        (?:(a|b|rc)(\d+))?        # Pre-release (groups 2, 3)
        (?:\.post(\d+))?          # Post-release (group 4)
        (?:\.dev(\d+))?           # Dev release (group 5)
        $
    """
    match = re.match(pattern, version_str, re.VERBOSE)
    if not match:
        raise ValueError(f"Invalid PEP 440 version: {version_str}")

    release = tuple(int(x) for x in match.group(1).split("."))
    pre = (match.group(2), int(match.group(3))) if match.group(2) else None
    post = int(match.group(4)) if match.group(4) else None
    dev = int(match.group(5)) if match.group(5) else None

    return ParsedPep440(release=release, pre=pre, post=post, dev=dev, local=local)


def _parse_semver(version_str: str) -> ParsedSemVer:
    """Parse a SemVer version string into components.

    SemVer format: X.Y.Z[-prerelease][+build]

    Args:
        version_str: SemVer version string

    Returns:
        ParsedSemVer with all components

    Raises:
        ValueError: If version string is invalid
    """
    # Split off build metadata
    build = None
    if "+" in version_str:
        version_str, build_str = version_str.split("+", 1)
        build = build_str.split(".")

    # Split off prerelease
    prerelease = None
    if "-" in version_str:
        version_str, pre_str = version_str.split("-", 1)
        prerelease = pre_str.split(".")

    # Parse release
    parts = version_str.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid SemVer version (expected X.Y.Z): {version_str}")

    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as e:
        raise ValueError(f"Invalid SemVer version numbers: {version_str}") from e

    return ParsedSemVer(major=major, minor=minor, patch=patch, prerelease=prerelease, build=build)


def pep440_to_semver(version_str: str) -> str:
    """Convert a PEP 440 version string to SemVer format.

    Conversion rules:
    - Release X.Y.Z → X.Y.Z (must be exactly 3 parts)
    - Pre-release aN/bN/rcN → -alpha.N/-beta.N/-rc.N
    - Dev release .devN → -dev.N (added to prerelease)
    - Post release .postN → +pN (in build metadata with 'p' prefix)
    - Local version +xxx → +lxxx (in build metadata with 'l' prefix)

    The 'p' and 'l' prefixes in build metadata allow symmetric conversion back.

    Args:
        version_str: PEP 440 version string

    Returns:
        SemVer version string

    Raises:
        ValueError: If version uses epoch or has invalid format

    Examples:
        >>> pep440_to_semver("1.0.0")
        '1.0.0'
        >>> pep440_to_semver("1.0.0a1")
        '1.0.0-alpha.1'
        >>> pep440_to_semver("1.0.0b2")
        '1.0.0-beta.2'
        >>> pep440_to_semver("1.0.0rc1")
        '1.0.0-rc.1'
        >>> pep440_to_semver("1.0.0.post1")
        '1.0.0+p1'
        >>> pep440_to_semver("1.0.0a1.post2")
        '1.0.0-alpha.1+p2'
        >>> pep440_to_semver("1.0.0+ubuntu1")
        '1.0.0+l.ubuntu1'
        >>> pep440_to_semver("1.0.0.post2+ubuntu1")
        '1.0.0+p2.l.ubuntu1'
        >>> pep440_to_semver("1.0.0.dev5")
        '1.0.0-dev.5'
        >>> pep440_to_semver("1.0.0a1.dev2")
        '1.0.0-alpha.1.dev.2'
    """
    parsed = _parse_pep440(version_str)

    # Validate release has exactly 3 parts for SemVer
    if len(parsed.release) != 3:
        raise ValueError(
            f"PEP 440 version must have exactly 3 release parts for SemVer conversion: {version_str}"
        )

    # Build release part
    result = f"{parsed.release[0]}.{parsed.release[1]}.{parsed.release[2]}"

    # Build prerelease part
    prerelease_parts: list[str] = []
    if parsed.pre:
        pre_type, pre_num = parsed.pre
        semver_pre = _PEP440_TO_SEMVER_PRE[pre_type]
        prerelease_parts.append(semver_pre)
        prerelease_parts.append(str(pre_num))
    if parsed.dev is not None:
        prerelease_parts.append("dev")
        prerelease_parts.append(str(parsed.dev))

    if prerelease_parts:
        result += "-" + ".".join(prerelease_parts)

    # Build metadata part (post and local)
    # Use 'p' prefix for post, 'l.' prefix for local to make it reversible
    build_parts: list[str] = []
    if parsed.post is not None:
        build_parts.append(f"p{parsed.post}")
    if parsed.local is not None:
        # Use 'l.' as prefix to clearly separate from post
        build_parts.append("l")
        build_parts.append(parsed.local)

    if build_parts:
        result += "+" + ".".join(build_parts)

    return result


def _parse_semver_prerelease(
    parts: list[str], version_str: str
) -> tuple[str | None, int | None, int | None]:
    """Parse SemVer prerelease parts into PEP 440 components.

    Args:
        parts: List of prerelease identifiers
        version_str: Original version string for error messages

    Returns:
        Tuple of (pre_type, pre_num, dev_num)

    Raises:
        ValueError: If prerelease format is invalid
    """
    pre_type: str | None = None
    pre_num: int | None = None
    dev_num: int | None = None

    i = 0
    while i < len(parts):
        part = parts[i]
        if part in _SEMVER_TO_PEP440_PRE:
            if i + 1 >= len(parts):
                raise ValueError(f"Pre-release type '{part}' missing number: {version_str}")
            pre_type = _SEMVER_TO_PEP440_PRE[part]
            pre_num = int(parts[i + 1])
            i += 2
        elif part == "dev":
            if i + 1 >= len(parts):
                raise ValueError(f"Dev release missing number: {version_str}")
            dev_num = int(parts[i + 1])
            i += 2
        else:
            raise ValueError(f"Unrecognized prerelease identifier '{part}': {version_str}")

    return pre_type, pre_num, dev_num


def _parse_semver_build(parts: list[str], version_str: str) -> tuple[int | None, str | None]:
    """Parse SemVer build metadata into PEP 440 components.

    Args:
        parts: List of build metadata identifiers
        version_str: Original version string for error messages

    Returns:
        Tuple of (post_num, local)

    Raises:
        ValueError: If build metadata format is invalid
    """
    post_num: int | None = None
    local: str | None = None

    i = 0
    while i < len(parts):
        part = parts[i]
        if part.startswith("p") and part[1:].isdigit():
            post_num = int(part[1:])
            i += 1
        elif part == "l":
            if i + 1 < len(parts):
                local = ".".join(parts[i + 1 :])
            break  # Consume all remaining
        else:
            raise ValueError(f"Unrecognized build metadata '{part}': {version_str}")

    return post_num, local


def semver_to_pep440(version_str: str) -> str:
    """Convert a SemVer version string to PEP 440 format.

    Conversion rules (reverse of pep440_to_semver):
    - Release X.Y.Z → X.Y.Z
    - Pre-release -alpha.N/-beta.N/-rc.N → aN/bN/rcN
    - Pre-release -dev.N → .devN
    - Build metadata +pN → .postN
    - Build metadata +l.xxx → +xxx (local version)

    Args:
        version_str: SemVer version string

    Returns:
        PEP 440 version string

    Raises:
        ValueError: If version has invalid format or unrecognized prerelease

    Examples:
        >>> semver_to_pep440("1.0.0")
        '1.0.0'
        >>> semver_to_pep440("1.0.0-alpha.1")
        '1.0.0a1'
        >>> semver_to_pep440("1.0.0-beta.2")
        '1.0.0b2'
        >>> semver_to_pep440("1.0.0-rc.1")
        '1.0.0rc1'
        >>> semver_to_pep440("1.0.0+p1")
        '1.0.0.post1'
        >>> semver_to_pep440("1.0.0-alpha.1+p2")
        '1.0.0a1.post2'
        >>> semver_to_pep440("1.0.0+l.ubuntu1")
        '1.0.0+ubuntu1'
        >>> semver_to_pep440("1.0.0+p2.l.ubuntu1")
        '1.0.0.post2+ubuntu1'
        >>> semver_to_pep440("1.0.0-dev.5")
        '1.0.0.dev5'
        >>> semver_to_pep440("1.0.0-alpha.1.dev.2")
        '1.0.0a1.dev2'
    """
    parsed = _parse_semver(version_str)

    # Build release part
    result = f"{parsed.major}.{parsed.minor}.{parsed.patch}"

    # Parse prerelease
    pre_type, pre_num, dev_num = (None, None, None)
    if parsed.prerelease:
        pre_type, pre_num, dev_num = _parse_semver_prerelease(parsed.prerelease, version_str)

    # Add pre-release to result
    if pre_type is not None and pre_num is not None:
        result += f"{pre_type}{pre_num}"

    # Parse build metadata for post and local
    post_num, local = (None, None)
    if parsed.build:
        post_num, local = _parse_semver_build(parsed.build, version_str)

    # Add post release
    if post_num is not None:
        result += f".post{post_num}"

    # Add dev release
    if dev_num is not None:
        result += f".dev{dev_num}"

    # Add local version
    if local is not None:
        result += f"+{local}"

    return result


def to_semver_display(version_str: str) -> str:
    """Convert a version string to SemVer format for user display.

    This is a convenience wrapper around pep440_to_semver that handles
    already-SemVer versions gracefully.

    Args:
        version_str: Version string in PEP 440 or SemVer format

    Returns:
        Version string in SemVer format

    Examples:
        >>> to_semver_display("0.1.0a1")
        '0.1.0-alpha.1'
        >>> to_semver_display("0.1.0-alpha.1")
        '0.1.0-alpha.1'
        >>> to_semver_display("1.0.0.post2+ubuntu1")
        '1.0.0+p2.l.ubuntu1'
    """
    # Check if it's already SemVer format (has hyphen for prerelease)
    # PEP 440 never uses hyphen in version strings
    if "-" in version_str:
        return version_str

    # Check if it looks like PEP 440
    try:
        return pep440_to_semver(version_str)
    except ValueError:
        # Not valid PEP 440, return as-is
        return version_str
