from typing import List
from pc_switcher.jobs.base import Job, JobContext


class InstallOnTargetJob(Job):
    name = "install_on_target"
    required = True

    async def validate(self, context: JobContext) -> List[str]:
        # Check if we can run git on target
        res = await context.target.run_command("git --version")
        if not res.success:
            return ["git is required on target machine but not found"]
        return []

    async def execute(self, context: JobContext) -> None:
        # Check version on target
        # We assume pc-switcher is installed via uv tool or pipx
        # Let's check version

        # Get local version
        # In real app, import version from package
        local_version = "0.1.0"

        context.logger.info(f"Checking pc-switcher version on target (source: {local_version})")

        # Check remote version
        res = await context.target.run_command("pc-switcher --version")
        remote_version = "0.0.0"
        if res.success:
            # Output format: "pc-switcher version 0.1.0" (typer default?)
            # Let's assume we can parse it
            import re

            match = re.search(r"(\d+\.\d+\.\d+)", res.stdout)
            if match:
                remote_version = match.group(1)

        if remote_version == local_version:
            context.logger.info("Target version matches source. Skipping install.")
            return

        context.logger.info(
            f"Target version {remote_version} differs from source {local_version}. Installing/Upgrading..."
        )

        # Install from git
        # We need the repo URL. For now, hardcode or get from config
        repo_url = "https://github.com/flaksit/pc-switcher-antigravity"  # Placeholder

        # Use uv tool install
        cmd = f"uv tool install --force git+{repo_url}@v{local_version}"
        # Fallback to pip if uv not present?
        # Requirement says "uv tool install"

        res = await context.target.run_command(cmd)
        if not res.success:
            raise RuntimeError(f"Failed to install pc-switcher on target: {res.stderr}")

        context.logger.info("Successfully installed pc-switcher on target")
