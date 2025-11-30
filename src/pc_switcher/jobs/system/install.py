from typing import List
from pc_switcher.jobs.base import Job, JobContext


class InstallOnTargetJob(Job):
    name = "install_on_target"
    required = True

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> list[str]:
        errors = []
        # Example validation: check if repo_url is valid if provided
        # For now, just a placeholder
        return errors

    async def validate(self) -> List[str]:
        # We don't check for git here anymore, we'll install it if missing
        return []

    async def execute(self) -> None:
        # Check if git is installed on target
        res = await self.context.target.run_command("git --version")
        if not res.success:
            self.context.logger.info("Git not found on target. Installing...")
            # Assume apt for now (Debian/Ubuntu)
            # This requires sudo/root, which might be an issue if we are running as user
            # But let's try. If it fails, we fail.
            res = await self.context.target.run_command("sudo apt-get update && sudo apt-get install -y git")
            if not res.success:
                raise RuntimeError(f"Failed to install git on target: {res.stderr}")

        # Check if uv is installed on target
        res = await self.context.target.run_command("uv --version")
        if not res.success:
            self.context.logger.info("uv not found on target. Installing...")
            res = await self.context.target.run_command("curl -LsSf https://astral.sh/uv/install.sh | sh")
            if not res.success:
                raise RuntimeError(f"Failed to install uv on target: {res.stderr}")
            # Ensure uv is in path for subsequent commands
            # We might need to source env or use full path, but let's assume standard install location
            # ~/.local/bin/uv

        # Get local version
        from importlib.metadata import version
        from packaging.version import parse as parse_version

        try:
            local_version_str = version("pc-switcher")
        except Exception:
            # Fallback for dev environment
            local_version_str = "0.1.0"

        local_version = parse_version(local_version_str)

        self.context.logger.info(f"Checking pc-switcher version on target (source: {local_version})")

        # Check remote version
        # We use 'uv tool run' to ensure we are running the installed tool
        res = await self.context.target.run_command("uv tool run pc-switcher --version")
        remote_version_str = "0.0.0"

        if res.success:
            # Output format: "pc-switcher version 0.1.0" (typer default?)
            import re

            match = re.search(r"(\d+\.\d+\.\d+)", res.stdout)
            if match:
                remote_version_str = match.group(1)

        remote_version = parse_version(remote_version_str)

        if remote_version >= local_version:
            self.context.logger.info(
                f"Target version {remote_version} is up to date with source {local_version}. Skipping install."
            )
            return

        self.context.logger.info(
            f"Target version {remote_version} differs from source {local_version}. Installing/Upgrading..."
        )

        # Copy config file to target
        target_config_dir = ".config/pc-switcher"
        target_config_path = f"{target_config_dir}/config.yaml"

        # Ensure directory exists
        await self.context.target.run_command(f"mkdir -p {target_config_dir}")

        # Copy file
        if self.context.config_path:
            self.context.logger.info(f"Copying config from {self.context.config_path} to target...")
            success = await self.context.target.put_file(str(self.context.config_path), target_config_path)
            if not success:
                self.context.logger.warning("Failed to copy config file to target")

        # Install from git
        repo_url = "https://github.com/flaksit/pc-switcher-antigravity"

        # Use uv tool install
        # We install the specific version tag
        # Note: This assumes the tag exists on remote.
        # If we are running from a dev version that hasn't been pushed, this might fail.
        # But for a release workflow, this is correct.
        cmd = f"uv tool install --force git+{repo_url}@{local_version_str}"
        # If local version is 0.1.0 (dev), we might want to default to main or handle it gracefully
        if "dev" in local_version_str or local_version_str == "0.1.0":
            cmd = f"uv tool install --force git+{repo_url}"

        res = await self.context.target.run_command(cmd)
        if not res.success:
            raise RuntimeError(f"Failed to install pc-switcher on target: {res.stderr}")

        self.context.logger.info("Successfully installed pc-switcher on target")
