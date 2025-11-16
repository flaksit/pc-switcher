"""Remote installation and version management for pc-switcher."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcswitcher.remote.connection import TargetConnection


class InstallationError(Exception):
    """Raised when installation or version checking fails."""


class VersionManager:
    """Manages pc-switcher installation and version comparison on target machine.

    Handles version detection, comparison, and installation using the setup script
    to ensure consistent installation behavior across machines.
    """

    def __init__(self, connection: TargetConnection) -> None:
        """Initialize version manager.

        Args:
            connection: Connection to target machine
        """
        self._connection = connection
        self._setup_script_path = Path(__file__).parent.parent.parent / "scripts" / "setup.sh"

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

    def install_on_target(
        self,
        version: str | None = None,
        config_content: str | None = None,
    ) -> None:
        """Install pc-switcher on the target machine using the setup script.

        The setup script handles all dependencies (uv, btrfs-progs) and manages
        configuration files appropriately based on installation mode.

        Args:
            version: Specific version to install, or None for latest
            config_content: Configuration file content to sync (implies sync mode).
                           If provided, setup script runs in sync mode and receives
                           config via stdin.

        Raises:
            InstallationError: If installation fails
        """
        import base64

        if not self._setup_script_path.exists():
            raise InstallationError(f"Setup script not found: {self._setup_script_path}")

        try:
            # Transfer setup script to target
            remote_script_path = Path("/tmp/pc-switcher-setup.sh")
            self._connection.send_file_to_target(self._setup_script_path, remote_script_path)

            # Build command with appropriate flags
            cmd_parts = [f"bash {remote_script_path}"]

            # Add version flag if specified
            if version:
                cmd_parts.append(f"--version={version}")

            # Add sync mode flag if config is provided
            if config_content:
                cmd_parts.append("--sync-mode")
                # Use base64 encoding to safely pass config through shell without quoting issues
                encoded_config = base64.b64encode(config_content.encode()).decode()
                command = f"echo '{encoded_config}' | base64 -d | bash {remote_script_path} --sync-mode --version={version or 'latest'}"
            else:
                command = " ".join(cmd_parts)

            result = self._connection.run(
                command,
                timeout=300.0,  # 5 minutes for installation
            )

            if result.returncode != 0:
                raise InstallationError(f"Installation failed: {result.stderr or result.stdout}")

            # Clean up remote script
            self._connection.run(f"rm -f {remote_script_path}", timeout=5.0)

        except InstallationError:
            raise
        except Exception as e:
            raise InstallationError(f"Failed to install on target: {e}") from e

    def ensure_version_sync(
        self,
        local_version: str | None,
        target_version: str | None,
        config_content: str | None = None,
    ) -> None:
        """Ensure target has compatible version, upgrading if needed.

        Logs messages via print. CRITICAL if target > source (unsupported scenario).
        Upgrades target if target < source. Synchronizes config if provided.

        Args:
            local_version: Version on source machine
            target_version: Version on target machine
            config_content: Configuration file content from source (optional).
                           If provided, will be synchronized to target during install.

        Raises:
            InstallationError: If version sync fails
        """
        # Handle missing versions
        if local_version is None:
            raise InstallationError("pc-switcher not installed on source machine")

        if target_version is None:
            # Target doesn't have pc-switcher - install it
            print(f"INFO: Installing pc-switcher {local_version} on target")
            self.install_on_target(local_version, config_content)
            return

        # Compare versions
        comparison = self.compare_versions(target_version, local_version)

        if comparison > 0:
            # Target version is newer than source - log CRITICAL
            print(
                f"CRITICAL: Target version ({target_version}) is newer than source ({local_version}). "
                "This is an unsupported configuration."
            )
            raise InstallationError(f"Target version ({target_version}) is newer than source ({local_version})")
        elif comparison < 0:
            # Target version is older - upgrade
            print(f"INFO: Upgrading target from {target_version} to {local_version}")
            self.install_on_target(local_version, config_content)
        else:
            # Versions match - no action needed
            print(f"DEBUG: Target version matches source: {local_version}")
            # Even if versions match, sync config if provided
            if config_content:
                print("INFO: Synchronizing configuration to target")
                self.install_on_target(local_version, config_content)
