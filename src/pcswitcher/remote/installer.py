"""Remote installation and version management for pc-switcher."""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

from pcswitcher.core.logging import get_logger

if TYPE_CHECKING:
    from pcswitcher.core.module import RemoteExecutor


class InstallationError(Exception):
    """Raised when installation or version checking fails."""


class VersionManager:
    """Manages pc-switcher installation and version comparison on target machine.

    Uses `uv tool install` to install/upgrade pc-switcher from GitHub Package Registry
    (ghcr.io) as specified in the project requirements.
    """

    # Package registry URL for pc-switcher
    PACKAGE_REGISTRY = "ghcr.io"
    PACKAGE_NAME = "pc-switcher"

    def __init__(self, remote: RemoteExecutor, session_id: str | None = None) -> None:
        """Initialize version manager.

        Args:
            remote: RemoteExecutor interface for target machine commands
            session_id: Optional session ID for logging context
        """
        self._remote = remote
        self._logger = get_logger("version_manager", session_id=session_id)

    def get_local_version(self) -> str | None:
        """Get the version of pc-switcher on the local machine.

        Returns:
            Version string (e.g., "0.1.0") or None if not installed
        """
        try:
            # Try uv tool list first (preferred method)
            result = subprocess.run(
                ["uv", "tool", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                # Parse output for pc-switcher entry
                # Format: "pc-switcher v0.1.0" or "pc-switcher 0.1.0"
                for line in result.stdout.splitlines():
                    if line.strip().startswith("pc-switcher"):
                        # Extract version from line
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            version = parts[1].lstrip("v")
                            self._logger.debug("Found local version via uv tool list", version=version)
                            return version

            # Fallback to importlib.metadata
            from importlib.metadata import version as get_version

            version = get_version("pcswitcher")
            self._logger.debug("Found local version via importlib.metadata", version=version)
            return version
        except Exception as e:
            self._logger.warning(f"Failed to get local version: {e}")
            return None

    def get_target_version(self) -> str | None:
        """Get the version of pc-switcher on the target machine.

        Returns:
            Version string or None if not installed

        Raises:
            InstallationError: If version check fails due to connection issues
        """
        try:
            # Check if uv is available on target
            uv_check = self._remote.run("command -v uv", timeout=10.0)
            if uv_check.returncode != 0:
                self._logger.debug("uv not found on target, pc-switcher likely not installed")
                return None

            # Try uv tool list
            result = self._remote.run("uv tool list", timeout=10.0)

            if result.returncode != 0:
                self._logger.debug("uv tool list failed on target", stderr=result.stderr)
                return None

            # Parse output for pc-switcher entry
            for line in result.stdout.splitlines():
                if line.strip().startswith("pc-switcher"):
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        version = parts[1].lstrip("v")
                        self._logger.debug("Found target version", version=version)
                        return version

            # pc-switcher not in tool list
            self._logger.debug("pc-switcher not found in uv tool list on target")
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

    def _ensure_uv_on_target(self) -> None:
        """Ensure uv is installed on target machine.

        Raises:
            InstallationError: If uv installation fails
        """
        # Check if uv is already available
        result = self._remote.run("command -v uv", timeout=10.0)
        if result.returncode == 0:
            self._logger.debug("uv already available on target")
            return

        self._logger.info("Installing uv on target machine")

        # Install uv using the official installer
        install_cmd = "curl -LsSf https://astral.sh/uv/install.sh | sh"
        result = self._remote.run(install_cmd, timeout=120.0)

        if result.returncode != 0:
            raise InstallationError(f"Failed to install uv on target: {result.stderr}")

        # Verify installation
        result = self._remote.run("~/.local/bin/uv --version", timeout=10.0)
        if result.returncode != 0:
            raise InstallationError("uv installation verification failed")

        self._logger.info("uv installed successfully on target")

    def install_on_target(self, version: str) -> None:
        """Install pc-switcher on the target machine using uv tool install.

        Uses `uv tool install pc-switcher==<version>` from GitHub Package Registry.

        Args:
            version: Specific version to install (e.g., "0.1.0")

        Raises:
            InstallationError: If installation fails
        """
        try:
            # Ensure uv is available on target
            self._ensure_uv_on_target()

            # Install pc-switcher using uv tool install
            # The package is published to ghcr.io (GitHub Package Registry)
            install_cmd = f"uv tool install pc-switcher=={version}"

            self._logger.info(
                f"Installing pc-switcher {version} on target",
                command=install_cmd,
            )

            result = self._remote.run(install_cmd, timeout=300.0)  # 5 minutes timeout

            if result.returncode != 0:
                error_msg = result.stderr or result.stdout
                raise InstallationError(f"Installation failed: {error_msg}")

            self._logger.info(f"Successfully installed pc-switcher {version} on target")

        except InstallationError:
            raise
        except Exception as e:
            raise InstallationError(f"Failed to install on target: {e}") from e

    def upgrade_on_target(self, version: str) -> None:
        """Upgrade pc-switcher on the target machine.

        Uses `uv tool upgrade pc-switcher --reinstall` to ensure exact version match.

        Args:
            version: Version to upgrade to (e.g., "0.4.0")

        Raises:
            InstallationError: If upgrade fails
        """
        try:
            # Ensure uv is available
            self._ensure_uv_on_target()

            # Upgrade using uv tool upgrade with reinstall to ensure exact version
            upgrade_cmd = f"uv tool upgrade pc-switcher=={version}"

            self._logger.info(
                f"Upgrading pc-switcher to {version} on target",
                command=upgrade_cmd,
            )

            result = self._remote.run(upgrade_cmd, timeout=300.0)

            if result.returncode != 0:
                # If upgrade fails, try uninstall + install
                self._logger.warning("Upgrade failed, trying uninstall + install")
                uninstall_result = self._remote.run("uv tool uninstall pc-switcher", timeout=60.0)
                if uninstall_result.returncode != 0:
                    self._logger.warning(f"Uninstall warning: {uninstall_result.stderr}")

                # Reinstall specific version
                self.install_on_target(version)
                return

            self._logger.info(f"Successfully upgraded pc-switcher to {version} on target")

        except InstallationError:
            raise
        except Exception as e:
            raise InstallationError(f"Failed to upgrade on target: {e}") from e

    def ensure_version_sync(
        self,
        local_version: str | None,
        target_version: str | None,
    ) -> None:
        """Ensure target has matching version, installing or upgrading if needed.

        Follows spec requirements:
        - If target has no pc-switcher: install matching version
        - If target version < source: upgrade to source version
        - If target version > source: raise InstallationError (prevent downgrade)
        - If versions match: no action needed

        Args:
            local_version: Version on source machine
            target_version: Version on target machine (None if not installed)

        Raises:
            InstallationError: If version sync fails or target is newer (downgrade prevention)
        """
        # Handle missing local version
        if local_version is None:
            raise InstallationError("pc-switcher not installed on source machine")

        # Handle missing target installation
        if target_version is None:
            self._logger.info(
                f"pc-switcher not installed on target, installing version {local_version}",
                action="install",
                version=local_version,
            )
            self.install_on_target(local_version)
            return

        # Compare versions
        comparison = self.compare_versions(target_version, local_version)

        if comparison > 0:
            # Target version is newer than source - this is unsupported
            error_msg = (
                f"Target version ({target_version}) is newer than source ({local_version}). "
                "This is an unsupported configuration that could cause compatibility issues."
            )
            self._logger.critical(error_msg)
            raise InstallationError(error_msg)

        elif comparison < 0:
            # Target version is older - upgrade
            self._logger.info(
                f"Target pc-switcher version {target_version} is outdated, upgrading to {local_version}",
                action="upgrade",
                from_version=target_version,
                to_version=local_version,
            )
            self.upgrade_on_target(local_version)

        else:
            # Versions match - no action needed
            self._logger.info(
                f"Target pc-switcher version matches source ({local_version}), skipping installation",
                action="skip",
                version=local_version,
            )
