"""Btrfs snapshot job for pre/post sync safety."""

from __future__ import annotations

from typing import Any, ClassVar

from pcswitcher.jobs.base import SystemJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel, SnapshotPhase, ValidationError
from pcswitcher.snapshots import (
    create_snapshot,
    session_folder_name,
    snapshot_name,
    validate_snapshots_directory,
    validate_subvolume_exists,
)


class BtrfsSnapshotJob(SystemJob):
    """Create btrfs snapshots before or after sync operations.

    This job creates read-only snapshots of configured subvolumes to enable
    rollback if something goes wrong during sync.
    """

    name: ClassVar[str] = "btrfs_snapshots"

    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "phase": {
                "type": "string",
                "enum": ["pre", "post"],
                "description": "When to create snapshots (pre-sync or post-sync)",
            },
            "subvolumes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "pattern": "^@",
                            "description": "Subvolume name (e.g., '@home')",
                        },
                        "mount_point": {
                            "type": "string",
                            "description": "Mount point path (e.g., '/home')",
                        },
                    },
                    "required": ["name", "mount_point"],
                },
                "minItems": 1,
                "description": "List of subvolumes to snapshot",
            },
        },
        "required": ["phase", "subvolumes"],
    }

    async def validate(self, context: JobContext) -> list[ValidationError]:
        """Validate that snapshots directory exists and subvolumes are valid."""
        errors: list[ValidationError] = []
        subvolumes = context.config["subvolumes"]

        # Validate snapshots directory on source
        success, error_msg = await validate_snapshots_directory(context.source, Host.SOURCE)
        if not success:
            errors.append(ValidationError(job=self.name, host=Host.SOURCE, message=error_msg or "Unknown error"))

        # Validate snapshots directory on target
        success, error_msg = await validate_snapshots_directory(context.target, Host.TARGET)
        if not success:
            errors.append(ValidationError(job=self.name, host=Host.TARGET, message=error_msg or "Unknown error"))

        # Validate each subvolume exists on both source and target
        for subvol in subvolumes:
            name = subvol["name"]
            mount_point = subvol["mount_point"]

            # Check source
            success, error_msg = await validate_subvolume_exists(context.source, name, mount_point, Host.SOURCE)
            if not success:
                errors.append(ValidationError(job=self.name, host=Host.SOURCE, message=error_msg or "Unknown error"))

            # Check target
            success, error_msg = await validate_subvolume_exists(context.target, name, mount_point, Host.TARGET)
            if not success:
                errors.append(ValidationError(job=self.name, host=Host.TARGET, message=error_msg or "Unknown error"))

        return errors

    async def execute(self, context: JobContext) -> None:
        """Create snapshots for all configured subvolumes."""
        phase = SnapshotPhase(context.config["phase"])
        subvolumes = context.config["subvolumes"]

        # Create session folder name
        session_folder = session_folder_name(context.session_id)

        self._log(
            context,
            Host.SOURCE,
            LogLevel.INFO,
            f"Creating {phase.value}-sync snapshots",
            session_id=context.session_id,
        )

        # Create snapshots on source
        for subvol in subvolumes:
            name = subvol["name"]
            mount_point = subvol["mount_point"]
            snap_name = snapshot_name(name, phase)
            snap_path = f"/.snapshots/pc-switcher/{session_folder}/{snap_name}"

            # Create session folder if it doesn't exist
            await context.source.run_command(f"sudo mkdir -p /.snapshots/pc-switcher/{session_folder}")

            self._log(
                context,
                Host.SOURCE,
                LogLevel.FULL,
                f"Creating snapshot {snap_name}",
                subvolume=name,
                mount_point=mount_point,
            )

            result = await create_snapshot(context.source, mount_point, snap_path)

            if result.exit_code != 0:
                self._log(
                    context,
                    Host.SOURCE,
                    LogLevel.CRITICAL,
                    f"Failed to create snapshot {snap_name}",
                    error=result.stderr,
                )
                raise RuntimeError(f"Snapshot creation failed: {result.stderr}")

            self._log(
                context,
                Host.SOURCE,
                LogLevel.FULL,
                f"Successfully created snapshot {snap_name}",
            )

        # Create snapshots on target
        for subvol in subvolumes:
            name = subvol["name"]
            mount_point = subvol["mount_point"]
            snap_name = snapshot_name(name, phase)
            snap_path = f"/.snapshots/pc-switcher/{session_folder}/{snap_name}"

            # Create session folder if it doesn't exist
            await context.target.run_command(f"sudo mkdir -p /.snapshots/pc-switcher/{session_folder}")

            self._log(
                context,
                Host.TARGET,
                LogLevel.FULL,
                f"Creating snapshot {snap_name}",
                subvolume=name,
                mount_point=mount_point,
            )

            result = await create_snapshot(context.target, mount_point, snap_path)

            if result.exit_code != 0:
                self._log(
                    context,
                    Host.TARGET,
                    LogLevel.CRITICAL,
                    f"Failed to create snapshot {snap_name}",
                    error=result.stderr,
                )
                raise RuntimeError(f"Snapshot creation failed: {result.stderr}")

            self._log(
                context,
                Host.TARGET,
                LogLevel.FULL,
                f"Successfully created snapshot {snap_name}",
            )

        self._log(
            context,
            Host.SOURCE,
            LogLevel.INFO,
            f"Completed {phase.value}-sync snapshots",
            session_id=context.session_id,
        )
