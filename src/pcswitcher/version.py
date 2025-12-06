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
