"""Installation job for ensuring pc-switcher is installed on target."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from packaging.version import Version

from pcswitcher.jobs.base import SystemJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel
from pcswitcher.version import get_this_version, parse_version_from_cli_output

if TYPE_CHECKING:
    from pcswitcher.models import ValidationError


class InstallOnTargetJob(SystemJob):
    """Ensure pc-switcher is installed on target machine.

    This job verifies version compatibility and installs or upgrades pc-switcher
    if needed. Version validation occurs during the validate phase to detect
    incompatibilities early. It runs AFTER pre-sync snapshots to enable rollback
    if installation fails.
    """

    name: ClassVar[str] = "install_on_target"

    def __init__(self, context: JobContext) -> None:
        """Initialize install job with context.

        Args:
            context: JobContext with executors, config, and event bus
        """
        super().__init__(context)

    async def validate(self) -> list[ValidationError]:
        """Validate version compatibility between source and target.

        Checks that target pc-switcher version is not newer than source,
        which would indicate a version conflict that cannot be resolved.

        Returns:
            List of ValidationError if version check fails, empty list otherwise
        """
        source_version = Version(get_this_version())

        # Check target version
        result = await self._context.target.run_command("pc-switcher --version 2>/dev/null")
        if result.success:
            # Parse version string from output (e.g., "pc-switcher 0.4.0" -> "0.4.0")
            target_version = Version(parse_version_from_cli_output(result.stdout))
            if target_version > source_version:
                return [
                    self._validation_error(
                        Host.TARGET,
                        f"Target version {target_version} is newer than source {source_version}",
                    )
                ]

        return []

    async def execute(self) -> None:
        """Install or upgrade pc-switcher on target if needed."""
        source_version = Version(get_this_version())  # e.g., "0.4.0"

        # Check target version (already validated in validate phase)
        result = await self._context.target.run_command("pc-switcher --version 2>/dev/null")
        if result.success:
            # Parse version string from output (e.g., "pc-switcher 0.4.0" -> "0.4.0")
            target_version = Version(parse_version_from_cli_output(result.stdout))
            if target_version == source_version:
                self._log(
                    Host.TARGET,
                    LogLevel.INFO,
                    f"Target pc-switcher version matches source ({source_version})",
                )
                return
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Upgrading pc-switcher on target from {target_version} to {source_version}",
            )
        else:
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Installing pc-switcher {source_version} on target",
            )

        # Run the same install.sh script used for initial installation
        # The script handles: uv bootstrap, dependencies, pc-switcher install
        install_url = (
            f"https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/"
            f"v{source_version}/install.sh"
        )
        cmd = f"curl -LsSf {install_url} | sh -s -- --version {source_version}"
        result = await self._context.target.run_command(cmd)
        if not result.success:
            raise RuntimeError(f"Failed to install pc-switcher on target: {result.stderr}")

        # Verify installation
        result = await self._context.target.run_command("pc-switcher --version")
        if not result.success or str(source_version) not in result.stdout:
            raise RuntimeError("Installation verification failed")

        self._log(
            Host.TARGET,
            LogLevel.INFO,
            f"Target pc-switcher installed/upgraded to {source_version}",
        )
