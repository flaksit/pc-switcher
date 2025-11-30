from typing import List, Dict, Any
from datetime import datetime
from pc_switcher.jobs.base import Job, JobContext


class BtrfsSnapshotJob(Job):
    name = "btrfs_snapshots"
    required = True

    def __init__(self, context: JobContext, phase: str = "presync"):
        super().__init__(context)
        self.phase = phase  # "presync" or "postsync"
        # Update name to distinguish phases in logs/registry if needed,
        # but for config we use "btrfs_snapshots" section.
        # Let's keep name constant but use phase in logic.

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> list[str]:
        errors = []
        if "mount_point" in config and not isinstance(config["mount_point"], str):
            errors.append("mount_point must be a string")
        return errors

    async def validate(self) -> list[str]:
        # Check if subvolumes exist on both source and target
        subvolumes = self.context.config.get("subvolumes", ["@", "@home"])
        errors = []

        for subvol in subvolumes:
            # Check source
            res_src = await self.context.source.run_command(f"btrfs subvolume show /{subvol}")
            if not res_src.success:
                errors.append(f"Source subvolume /{subvol} not found or not btrfs")

            # Check target
            res_tgt = await self.context.target.run_command(f"btrfs subvolume show /{subvol}")
            if not res_tgt.success:
                errors.append(f"Target subvolume /{subvol} not found or not btrfs")

        return errors

    async def execute(self) -> None:
        subvolumes = self.context.config.get("subvolumes", ["@", "@home"])
        timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        session_id = self.context.session_id

        self.context.logger.info(f"Creating {self.phase} snapshots for {subvolumes}")

        for subvol in subvolumes:
            snapshot_name = f"{subvol}-{self.phase}-{timestamp}-{session_id}"

            # Create on source
            self.context.logger.full(f"Creating source snapshot: {snapshot_name}")
            cmd_src = f"sudo btrfs subvolume snapshot -r /{subvol} /{snapshot_name}"
            res_src = await self.context.source.run_command(cmd_src)
            if not res_src.success:
                raise RuntimeError(f"Failed to create source snapshot {snapshot_name}: {res_src.stderr}")

            # Create on target
            self.context.logger.full(f"Creating target snapshot: {snapshot_name}")
            cmd_tgt = f"sudo btrfs subvolume snapshot -r /{subvol} /{snapshot_name}"
            res_tgt = await self.context.target.run_command(cmd_tgt)
            if not res_tgt.success:
                raise RuntimeError(f"Failed to create target snapshot {snapshot_name}: {res_tgt.stderr}")

        self.context.logger.info(f"Successfully created {self.phase} snapshots")
