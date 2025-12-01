"""Version checking and self-installation for pc-switcher on target machines."""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, version

from packaging.version import Version

from pcswitcher.executor import RemoteExecutor
from pcswitcher.models import CommandResult

__all__ = [
    "InstallationError",
    "compare_versions",
    "get_this_version",
    "get_target_version",
    "install_on_target",
]


class InstallationError(Exception):
    """Error during installation or version checking."""


def get_this_version() -> str:
    """Get the version of pc-switcher currently running.

    Returns:
        Version string from package metadata

    Raises:
        InstallationError: If package metadata cannot be read
    """
    try:
        return version("pcswitcher")
    except PackageNotFoundError as e:
        raise InstallationError(
            "Cannot determine pc-switcher version. Package metadata not found. Is pc-switcher installed correctly?"
        ) from e


async def get_target_version(executor: RemoteExecutor) -> str | None:
    """Get the version of pc-switcher installed on target machine.

    Args:
        executor: RemoteExecutor connected to target machine

    Returns:
        Version string if pc-switcher is installed on target, None otherwise
    """
    result: CommandResult = await executor.run_command("pc-switcher --version")

    if result.exit_code != 0:
        return None

    # Parse version from output like "pc-switcher version 0.1.0" or just "0.1.0"
    output = result.stdout.strip()
    version_match = re.search(r"(\d+\.\d+\.\d+(?:\.\w+)?)", output)

    if version_match:
        return version_match.group(1)

    return None


def compare_versions(source: str, target: str) -> int:
    """Compare two version strings.

    Args:
        source: Version string of source machine
        target: Version string of target machine

    Returns:
        -1 if source < target (target is newer)
        0 if source == target (same version)
        1 if source > target (source is newer)

    Raises:
        InstallationError: If version strings are invalid
    """
    try:
        source_ver = Version(source)
        target_ver = Version(target)

        if source_ver < target_ver:
            return -1
        if source_ver > target_ver:
            return 1
        return 0
    except Exception as e:
        raise InstallationError(f"Invalid version string: source='{source}', target='{target}'") from e


async def install_on_target(executor: RemoteExecutor, version: str) -> None:
    """Install pc-switcher on target machine using GitHub install.sh script.

    Fetches install.sh from the versioned GitHub tag and runs it with --version parameter.
    This handles uv bootstrap and all dependencies automatically.

    Args:
        executor: RemoteExecutor connected to target machine
        version: Version string to install (e.g., "0.1.0")

    Raises:
        InstallationError: If installation fails
    """
    # Construct URL to versioned install.sh from GitHub
    install_url = f"https://raw.githubusercontent.com/flaksit/pc-switcher/v{version}/install.sh"

    # Run install.sh from GitHub with --version parameter
    # Using curl -LsSf (Location, Silent, Show errors, Fail on error)
    cmd = f"curl -LsSf {install_url} | sh -s -- --version {version}"
    result = await executor.run_command(cmd, timeout=300.0)

    if not result.success:
        raise InstallationError(
            f"Failed to install pc-switcher {version} on target. "
            f"Command: {cmd}\n"
            f"Exit code: {result.exit_code}\n"
            f"Stderr: {result.stderr}"
        )


async def check_and_install(executor: RemoteExecutor) -> None:
    """Check if target has same version as source, install if needed.

    Workflow:
    1. Get source (current) version
    2. Get target version (if installed)
    3. Compare versions:
       - If target is newer than source: abort with error
       - If target is same as source: do nothing
       - If target is older or not installed: install source version

    Args:
        executor: RemoteExecutor connected to target machine

    Raises:
        InstallationError: If version check or installation fails
    """
    source_ver = get_this_version()
    target_ver = await get_target_version(executor)

    # Target has no pc-switcher installed
    if target_ver is None:
        await install_on_target(executor, source_ver)
        return

    # Compare versions
    comparison = compare_versions(source_ver, target_ver)

    if comparison == -1:
        # Target is newer than source - this is an error condition
        raise InstallationError(
            f"Target machine has newer pc-switcher version ({target_ver}) "
            f"than source machine ({source_ver}). "
            f"Cannot sync from older source to newer target. "
            f"Please upgrade source machine first."
        )

    if comparison == 0:
        # Same version - nothing to do
        return

    # Source is newer - install on target
    await install_on_target(executor, source_ver)
