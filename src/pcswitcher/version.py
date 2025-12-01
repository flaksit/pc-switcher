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

    Args:
        output: Command output (e.g., "pc-switcher 0.4.0" or "0.4.0")

    Returns:
        Version string (e.g., "0.4.0")

    Raises:
        ValueError: If version string cannot be parsed
    """
    match = re.search(r"(\d+\.\d+\.\d+(?:\.[\w-+.]+)?)", output)
    if not match:
        raise ValueError(f"Cannot parse version from output: {output}")
    return match.group(1)
