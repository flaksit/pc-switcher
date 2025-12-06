"""Version utilities for pc-switcher.

This module provides utilities for:
- Determining the current pc-switcher version from package metadata
- Parsing version strings from CLI output

For installation and upgrade logic on target machines, see pcswitcher.jobs.install_on_target.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version

__all__ = [
    "get_this_version",
    "parse_version_from_cli_output",
    "to_semver_display",
]


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


def to_semver_display(version_str: str) -> str:
    """Convert a version string to SemVer format for user display.

    Converts PEP 440 pre-release suffixes to SemVer format:
    - "0.1.0a1" -> "0.1.0-alpha.1"
    - "0.2.0b2" -> "0.2.0-beta.2"
    - "1.0.0rc1" -> "1.0.0-rc.1"

    Also handles development versions with full build metadata:
    - "0.1.0a1.post20.dev0+4e7b776" -> "0.1.0-alpha.1+post20.4e7b776"
    - "0.0.0.post125.dev0+09ab5f5" -> "0.0.0+post125.09ab5f5"

    Already-SemVer versions are returned unchanged:
    - "0.1.0-alpha.1" -> "0.1.0-alpha.1"
    - "1.0.0" -> "1.0.0"

    Args:
        version_str: Version string in any format

    Returns:
        Version string in SemVer format for display

    Examples:
        >>> to_semver_display("0.1.0a1")
        '0.1.0-alpha.1'
        >>> to_semver_display("0.2.0b2")
        '0.2.0-beta.2'
        >>> to_semver_display("1.0.0rc1")
        '1.0.0-rc.1'
        >>> to_semver_display("0.1.0-alpha.1")
        '0.1.0-alpha.1'
        >>> to_semver_display("0.1.0a1.post20.dev0+4e7b776")
        '0.1.0-alpha.1+post20.4e7b776'
    """
    # Check if this is a development version with .post/.dev suffixes
    # Format: X.Y.Z[aN|bN|rcN].postN.devN+hash
    dev_match = re.match(
        r"^(\d+\.\d+\.\d+)((?:a|b|rc)\d+)?\.post(\d+)\.dev\d+\+([a-f0-9]+)$", version_str
    )
    if dev_match:
        base = dev_match.group(1)
        prerelease = dev_match.group(2)
        post_num = dev_match.group(3)
        git_hash = dev_match.group(4)
        build_metadata = f"post{post_num}.{git_hash}"
        if prerelease:
            # Convert PEP 440 prerelease to SemVer format
            converted = _convert_pep440_prerelease(base, prerelease)
            return f"{converted}+{build_metadata}"
        return f"{base}+{build_metadata}"

    # Check for PEP 440 pre-release format: 0.1.0a1, 0.2.0b2, 1.0.0rc1
    pep440_match = re.match(r"^(\d+\.\d+\.\d+)(a|b|rc)(\d+)$", version_str)
    if pep440_match:
        base = pep440_match.group(1)
        prerelease_type = pep440_match.group(2)
        prerelease_num = pep440_match.group(3)
        return _convert_pep440_prerelease(base, f"{prerelease_type}{prerelease_num}")

    # Already SemVer or stable version, return as-is
    return version_str


def _convert_pep440_prerelease(base: str, prerelease: str) -> str:
    """Convert PEP 440 prerelease suffix to SemVer format.

    Args:
        base: Base version (e.g., "0.1.0")
        prerelease: PEP 440 prerelease suffix (e.g., "a1", "b2", "rc1")

    Returns:
        SemVer formatted version string
    """
    # Map PEP 440 suffixes to SemVer names
    suffix_map = {"a": "alpha", "b": "beta", "rc": "rc"}

    match = re.match(r"^(a|b|rc)(\d+)$", prerelease)
    if match:
        suffix_type = match.group(1)
        suffix_num = match.group(2)
        semver_name = suffix_map.get(suffix_type, suffix_type)
        return f"{base}-{semver_name}.{suffix_num}"

    # Unknown format, return base with prerelease as-is
    return f"{base}-{prerelease}"
