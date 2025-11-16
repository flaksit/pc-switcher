"""Remote installation and version management for pc-switcher."""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcswitcher.remote.connection import TargetConnection


class InstallationError(Exception):
    """Raised when installation or version checking fails."""


class VersionManager:
    """Manages pc-switcher installation and version comparison on target machine.

    Handles version detection, comparison, and installation using uv tool.
    """

    def __init__(self, connection: TargetConnection) -> None:
        """Initialize version manager.

        Args:
            connection: Connection to target machine
        """
        self._connection = connection

    def get_local_version(self) -> str | None:
        """Get the version of pc-switcher on the local machine.

        Returns:
            Version string (e.g., "0.1.0") or None if not installed
        """
        try:
            result = subprocess.run(
                ["pip", "show", "pcswitcher"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return None

            # Parse version from output
            for line in result.stdout.splitlines():
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
            return None
        except Exception:
            return None

    def get_target_version(self) -> str | None:
        """Get the version of pc-switcher on the target machine.

        Returns:
            Version string or None if not installed

        Raises:
            InstallationError: If version check fails
        """
        try:
            result = self._connection.run(
                "pip show pcswitcher",
                timeout=10.0,
            )

            if result.returncode != 0:
                return None

            # Parse version from output
            for line in result.stdout.splitlines():
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
            return None
        except Exception as e:
            raise InstallationError(f"Failed to check target version: {e}") from e

    def compare_versions(self, version1: str, version2: str) -> int:
        """Compare two version strings.

        Args:
            version1: First version string (e.g., "0.1.0")
            version2: Second version string (e.g., "0.2.0")

        Returns:
            -1 if version1 < version2
             0 if version1 == version2
             1 if version1 > version2
        """
        # Parse version components
        v1_parts = [int(x) for x in re.split(r"[.-]", version1) if x.isdigit()]
        v2_parts = [int(x) for x in re.split(r"[.-]", version2) if x.isdigit()]

        # Pad shorter version with zeros
        max_len = max(len(v1_parts), len(v2_parts))
        v1_parts.extend([0] * (max_len - len(v1_parts)))
        v2_parts.extend([0] * (max_len - len(v2_parts)))

        # Compare component by component
        for p1, p2 in zip(v1_parts, v2_parts, strict=False):
            if p1 < p2:
                return -1
            if p1 > p2:
                return 1
        return 0

    def install_on_target(self, version: str | None = None) -> None:
        """Install pc-switcher on the target machine using uv tool.

        Args:
            version: Specific version to install, or None for latest

        Raises:
            InstallationError: If installation fails
        """
        # Build installation command
        package_spec = f"pcswitcher=={version}" if version else "pcswitcher"

        # Use uv tool install to install from ghcr.io or PyPI
        # Note: This assumes the package is available in a registry
        # For ghcr.io, we might need to use a different installation method
        command = f"uv tool install {package_spec}"

        try:
            result = self._connection.run(
                command,
                timeout=300.0,  # 5 minutes for installation
            )

            if result.returncode != 0:
                raise InstallationError(
                    f"Installation failed: {result.stderr or result.stdout}"
                )
        except Exception as e:
            raise InstallationError(f"Failed to install on target: {e}") from e

    def ensure_version_sync(
        self,
        local_version: str | None,
        target_version: str | None,
    ) -> None:
        """Ensure target has compatible version, upgrading if needed.

        Logs messages via print. CRITICAL if target > source (unsupported scenario).
        Upgrades target if target < source.

        Args:
            local_version: Version on source machine
            target_version: Version on target machine

        Raises:
            InstallationError: If version sync fails
        """
        # Handle missing versions
        if local_version is None:
            raise InstallationError("pc-switcher not installed on source machine")

        if target_version is None:
            # Target doesn't have pc-switcher - install it
            print(f"INFO: Installing pc-switcher {local_version} on target")
            self.install_on_target(local_version)
            return

        # Compare versions
        comparison = self.compare_versions(target_version, local_version)

        if comparison > 0:
            # Target version is newer than source - log CRITICAL
            print(
                f"CRITICAL: Target version ({target_version}) is newer than source ({local_version}). "
                "This is an unsupported configuration."
            )
            raise InstallationError(
                f"Target version ({target_version}) is newer than source ({local_version})"
            )
        elif comparison < 0:
            # Target version is older - upgrade
            print(f"INFO: Upgrading target from {target_version} to {local_version}")
            self.install_on_target(local_version)
        else:
            # Versions match - no action needed
            print(f"DEBUG: Target version matches source: {local_version}")
