"""Btrfs snapshot job for pre/post sync safety."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pcswitcher.btrfs_snapshots import (
    create_snapshot,
    snapshot_name,
    validate_snapshots_directory,
    validate_subvolume_exists,
)
from pcswitcher.jobs.base import SystemJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel, SnapshotPhase

if TYPE_CHECKING:
    from pcswitcher.models import ValidationError


def subvolume_to_mount_point(subvol_name: str) -> str:
    """Derive mount point from subvolume name using common btrfs conventions.

    Common mapping:
    - @ -> /
    - @home -> /home
    - @var -> /var
    - @opt -> /opt
    - @tmp -> /tmp
    - @srv -> /srv
    - etc.

    Args:
        subvol_name: Subvolume name (e.g., "@home")

    Returns:
        Mount point path (e.g., "/home")
    """
    if subvol_name == "@":
        return "/"
    if subvol_name.startswith("@"):
        return "/" + subvol_name[1:]
    raise ValueError(f"Invalid subvolume name: {subvol_name} (must start with @)")


class BtrfsSnapshotJob(SystemJob):
    """Create btrfs snapshots before or after sync operations.

    This job creates read-only snapshots of configured subvolumes to enable
    rollback if something goes wrong during sync.
    """

    name: ClassVar[str] = "btrfs_snapshots"

    def __init__(self, context: JobContext) -> None:
        """Initialize snapshot job with context.

        Args:
            context: JobContext with executors, config, and event bus
        """
        super().__init__(context)

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
                    "type": "string",
                    "pattern": "^@",
                    "description": "Subvolume name (e.g., '@home'). Mount point is derived automatically.",
                },
                "minItems": 1,
                "description": "List of subvolumes to snapshot",
            },
            "session_folder": {
                "type": "string",
                "description": "Session folder name for organizing PRE and POST snapshots together",
            },
        },
        "required": ["phase", "subvolumes", "session_folder"],
    }

    async def validate(self) -> list[ValidationError]:
        """Validate that snapshots directory exists and subvolumes are valid."""
        errors: list[ValidationError] = []
        subvolumes: list[str] = self.context.config["subvolumes"]

        # Validate snapshots directory on source
        success, error_msg = await validate_snapshots_directory(self.source, Host.SOURCE)
        if not success:
            errors.append(self._validation_error(Host.SOURCE, error_msg or "Unknown error"))

        # Validate snapshots directory on target
        success, error_msg = await validate_snapshots_directory(self.target, Host.TARGET)
        if not success:
            errors.append(self._validation_error(Host.TARGET, error_msg or "Unknown error"))

        # Validate each subvolume exists on both source and target
        for subvol_name in subvolumes:
            mount_point = subvolume_to_mount_point(subvol_name)

            # Check source
            success, error_msg = await validate_subvolume_exists(self.source, subvol_name, mount_point, Host.SOURCE)
            if not success:
                errors.append(self._validation_error(Host.SOURCE, error_msg or "Unknown error"))

            # Check target
            success, error_msg = await validate_subvolume_exists(self.target, subvol_name, mount_point, Host.TARGET)
            if not success:
                errors.append(self._validation_error(Host.TARGET, error_msg or "Unknown error"))

        return errors

    async def execute(self) -> None:
        """Create snapshots for all configured subvolumes."""
        phase = SnapshotPhase(self.context.config["phase"])
        subvolumes: list[str] = self.context.config["subvolumes"]
        session_folder: str = self.context.config["session_folder"]

        self._log(
            Host.SOURCE,
            LogLevel.INFO,
            f"Creating {phase.value}-sync snapshots",
            session_id=self.context.session_id,
        )

        # Create snapshots on source
        for subvol_name in subvolumes:
            mount_point = subvolume_to_mount_point(subvol_name)
            snap_name = snapshot_name(subvol_name, phase)
            snap_path = f"/.snapshots/pc-switcher/{session_folder}/{snap_name}"

            self._log(
                Host.SOURCE,
                LogLevel.FULL,
                f"Creating snapshot {snap_name}",
                subvolume=subvol_name,
                mount_point=mount_point,
            )

            if not self.context.dry_run:
                # Create session folder if it doesn't exist
                await self.source.run_command(f"sudo mkdir -p /.snapshots/pc-switcher/{session_folder}")

                result = await create_snapshot(self.source, mount_point, snap_path)

                if result.exit_code != 0:
                    self._log(
                        Host.SOURCE,
                        LogLevel.CRITICAL,
                        f"Failed to create snapshot {snap_name}",
                        error=result.stderr,
                    )
                    raise RuntimeError(f"Snapshot creation failed: {result.stderr}")

            self._log(
                Host.SOURCE,
                LogLevel.FULL,
                f"Successfully created snapshot {snap_name}",
            )

        # Create snapshots on target
        for subvol_name in subvolumes:
            mount_point = subvolume_to_mount_point(subvol_name)
            snap_name = snapshot_name(subvol_name, phase)
            snap_path = f"/.snapshots/pc-switcher/{session_folder}/{snap_name}"

            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"Creating snapshot {snap_name}",
                subvolume=subvol_name,
                mount_point=mount_point,
            )

            if not self.context.dry_run:
                # Create session folder if it doesn't exist
                await self.target.run_command(f"sudo mkdir -p /.snapshots/pc-switcher/{session_folder}")

                result = await create_snapshot(self.target, mount_point, snap_path)

                if result.exit_code != 0:
                    self._log(
                        Host.TARGET,
                        LogLevel.CRITICAL,
                        f"Failed to create snapshot {snap_name}",
                        error=result.stderr,
                    )
                    raise RuntimeError(f"Snapshot creation failed: {result.stderr}")

            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"Successfully created snapshot {snap_name}",
            )

        self._log(
            Host.SOURCE,
            LogLevel.INFO,
            f"Completed {phase.value}-sync snapshots",
            session_id=self.context.session_id,
        )
