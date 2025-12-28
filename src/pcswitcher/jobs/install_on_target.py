"""Installation job for ensuring pc-switcher is installed on target."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pcswitcher.install import get_install_with_script_command_line
from pcswitcher.jobs.base import SystemJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import CommandResult, Host, LogLevel
from pcswitcher.version import Release, Version, find_one_version, get_this_version

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
        self.source_version: Version = get_this_version()
        self.target_version: Version | None = None

    async def validate(self) -> list[ValidationError]:
        """Validate version compatibility between source and target.

        Checks that target pc-switcher version is not newer than source,
        which would indicate a version conflict that cannot be resolved.

        Returns:
            List of ValidationError if version check fails, empty list otherwise
        """
        # Check target version (login_shell ensures PATH includes ~/.local/bin and GITHUB_TOKEN is available)
        result = await self.target.run_command("pc-switcher --version 2>/dev/null", login_shell=True)
        if result.success:
            # Parse version string from output (e.g., "pc-switcher 0.4.0" -> "0.4.0")
            self.target_version = find_one_version(result.stdout)
            if self.target_version > self.source_version:
                return [
                    self._validation_error(
                        Host.TARGET,
                        f"Target version {self.target_version} is newer than source {self.source_version}",
                    )
                ]

        return []

    async def _run_install(self, v: Release | Version | str | None = None, /) -> CommandResult:
        """Run install script on target."""
        cmd = get_install_with_script_command_line(v)
        return await self.target.run_command(cmd, login_shell=False)

    async def execute(self) -> None:
        """Install or upgrade pc-switcher on target if needed."""
        # Check target version (already validated in validate phase)
        if self.target_version:
            if self.target_version == self.source_version:  # , source_release.version):
                self._log(
                    Host.TARGET,
                    LogLevel.INFO,
                    f"Target pc-switcher version matches source: {self.source_version}, no install needed",
                )
                return
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Upgrading pc-switcher on target from {self.target_version} to {self.source_version}",
            )
        else:
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Installing pc-switcher {self.source_version} on target",
            )

        # Run the same install.sh script used for initial installation
        # The script handles: uv bootstrap, dependencies, pc-switcher install

        # To minimize github lookups, we first try to install directly using the exact source version.
        # If that fails, we fall back to the release floor, which needs a GitHub API call to lookup the releases.
        result = await self._run_install(self.source_version)
        source_release = None
        if result.success:
            installed_version_str = f"v{self.source_version}"
        else:
            # Get the GitHub floor release for this version (the highest release <= this version).
            # For dev versions like 0.1.0a3.post23.dev0, this returns the base release (e.g., 0.1.0a3).
            source_release = self.source_version.get_release_floor()
            self._log(
                Host.TARGET,
                LogLevel.WARNING,
                f"Installation with exact version v{self.source_version} failed, "
                f"falling back to release floor {source_release.tag}",
            )
            result = await self._run_install(source_release)
            if not result.success:
                raise RuntimeError(f"Failed to install pc-switcher on target: {result.stderr}")
            installed_version_str = source_release.tag

        # Verify installation (login_shell ensures PATH includes ~/.local/bin)
        result = await self.target.run_command("pc-switcher --version", login_shell=True)
        if not result.success:
            raise RuntimeError("Installation verification failed: pc-switcher not found")
        # Parse and compare versions properly (handles both PEP440 and SemVer formats)
        installed_version = find_one_version(result.stdout)
        if installed_version != (source_release or self.source_version):
            raise RuntimeError(
                f"Installation verification failed: expected {installed_version_str}, got v{installed_version}"
            )

        self._log(
            Host.TARGET,
            LogLevel.INFO,
            f"Target pc-switcher installed/upgraded to {installed_version_str}",
        )
