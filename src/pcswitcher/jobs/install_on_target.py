"""Installation job for ensuring pc-switcher is installed on target."""

from __future__ import annotations

from typing import ClassVar

from packaging.version import Version

from pcswitcher.installation import get_this_version
from pcswitcher.jobs.base import SystemJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel, ValidationError


def parse_version_string(output: str) -> str:
    """Parse version string from pc-switcher --version output.

    Args:
        output: Command output (e.g., "pc-switcher 0.4.0" or "0.4.0")

    Returns:
        Version string (e.g., "0.4.0")

    Raises:
        ValueError: If version string cannot be parsed
    """
    import re

    match = re.search(r"(\d+\.\d+\.\d+(?:\.\w+)?)", output)
    if not match:
        raise ValueError(f"Cannot parse version from output: {output}")
    return match.group(1)


class InstallOnTargetJob(SystemJob):
    """Ensure pc-switcher is installed on target machine.

    This job verifies that the target has the same version of pc-switcher
    as the source, installing or upgrading if needed. It runs AFTER pre-sync
    snapshots to enable rollback if installation fails.
    """

    name: ClassVar[str] = "install_on_target"

    async def validate(self, context: JobContext) -> list[ValidationError]:
        """Validate installation prerequisites.

        For this job, validation is minimal - we just need SSH connectivity,
        which is already verified by the orchestrator before jobs run.

        Returns:
            Empty list (no validation errors)
        """
        return []

    async def execute(self, context: JobContext) -> None:
        """Install or upgrade pc-switcher on target if needed."""
        source_version = Version(get_this_version())  # e.g., "0.4.0"

        # Check target version first
        result = await context.target.run_command("pc-switcher --version 2>/dev/null")
        if result.success:
            # Parse version string from output (e.g., "pc-switcher 0.4.0" -> "0.4.0")
            target_version = Version(parse_version_string(result.stdout))
            if target_version == source_version:
                self._log(
                    context,
                    Host.TARGET,
                    LogLevel.INFO,
                    f"Target pc-switcher version matches source ({source_version})",
                )
                return
            if target_version > source_version:
                raise RuntimeError(f"Target version {target_version} is newer than source {source_version}")
            self._log(
                context,
                Host.TARGET,
                LogLevel.INFO,
                f"Upgrading target from {target_version} to {source_version}",
            )
        else:
            self._log(
                context,
                Host.TARGET,
                LogLevel.INFO,
                f"Installing pc-switcher {source_version} on target",
            )

        # Run the same install.sh script used for initial installation
        # The script handles: uv bootstrap, dependencies, pc-switcher install
        install_url = f"https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/v{source_version}/install.sh"
        result = await context.target.run_command(f"curl -LsSf {install_url} | sh -s -- --version {source_version}")
        if not result.success:
            raise RuntimeError(f"Failed to install pc-switcher on target: {result.stderr}")

        # Verify installation
        result = await context.target.run_command("pc-switcher --version")
        if not result.success or str(source_version) not in result.stdout:
            raise RuntimeError("Installation verification failed")

        self._log(
            context,
            Host.TARGET,
            LogLevel.INFO,
            f"Target pc-switcher installed/upgraded to {source_version}",
        )
