"""Btrfs snapshot management module for pc-switcher.

This module implements orchestrator-level snapshot management infrastructure
(NOT a SyncModule). It handles creation, cleanup, and rollback of btrfs snapshots
to ensure data safety during sync operations.

Btrfs snapshots are copy-on-write (COW), meaning they are instantaneous and
consume no disk space initially. Only blocks that change after snapshot creation
consume additional space. This makes snapshots ideal for backup/rollback without
significant disk wear.

Key operations:
- validate_subvolumes: Verify subvolumes exist before sync starts
- create_presync_snapshots: Create read-only snapshots before SyncModules run
- create_postsync_snapshots: Create read-only snapshots after SyncModules complete
- rollback_to_presync: Restore system to pre-sync state on failure
- cleanup_old_snapshots: Remove old snapshots to free disk space
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.module import SyncError
from pcswitcher.core.snapshot import SnapshotManager


class BtrfsSnapshotsModule(SnapshotManager):
    """Manages btrfs snapshots for sync safety and rollback capability.

    This is orchestrator-level infrastructure (NOT a SyncModule) that:
    - Validates btrfs subvolumes before any sync operations
    - Creates read-only snapshots before and after sync operations
    - Provides rollback functionality
    - Manages snapshot cleanup and retention

    Snapshot naming convention:
        {snapshot_dir}/{subvol}-{presync|postsync}-{timestamp}-{session_id}
        Example: /.snapshots/@-presync-20250116T123045Z-a1b2c3d4
    """

    @override
    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON Schema for configuration validation."""
        return {
            "type": "object",
            "properties": {
                "subvolumes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Flat subvolume names from 'btrfs subvolume list /'",
                },
                "snapshot_dir": {
                    "type": "string",
                    "description": "Directory for storing snapshots",
                },
                "keep_recent": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Number of recent snapshots to keep per subvolume",
                },
                "max_age_days": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Delete snapshots older than this many days",
                },
            },
            "required": ["subvolumes", "snapshot_dir", "keep_recent", "max_age_days"],
            "additionalProperties": False,
        }

    @override
    def validate_subvolumes(self) -> list[str]:
        """Validate btrfs filesystem and subvolumes exist on both source and target.

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[str] = []

        # Check source filesystem
        try:
            result = subprocess.run(
                ["stat", "-f", "-c", "%T", "/"],
                capture_output=True,
                text=True,
                check=True,
            )
            fs_type = result.stdout.strip()
            if fs_type != "btrfs":
                errors.append(
                    f"Source root filesystem is {fs_type}, not btrfs. "
                    f"PC-switcher requires btrfs for snapshot support."
                )
        except subprocess.CalledProcessError as e:
            errors.append(f"Failed to check source filesystem type: {e.stderr}")

        # Verify all configured subvolumes exist on source
        subvolumes: list[str] = self.config["subvolumes"]
        try:
            result = subprocess.run(
                ["sudo", "btrfs", "subvolume", "list", "/"],
                capture_output=True,
                text=True,
                check=True,
            )
            existing_subvols = set()
            for line in result.stdout.splitlines():
                parts = line.split()
                if "path" in parts:
                    path_idx = parts.index("path")
                    if path_idx + 1 < len(parts):
                        existing_subvols.add(parts[path_idx + 1])

            for subvol in subvolumes:
                if subvol not in existing_subvols:
                    errors.append(
                        f"Subvolume '{subvol}' not found on source. "
                        f"Run 'sudo btrfs subvolume list /' to see available subvolumes."
                    )
        except subprocess.CalledProcessError as e:
            errors.append(f"Failed to list source btrfs subvolumes: {e.stderr}")

        # Check target filesystem and subvolumes
        try:
            result = self.remote.run("stat -f -c '%T' /", timeout=10.0)
            if result.returncode != 0:
                errors.append(f"Failed to check target filesystem type: {result.stderr}")
            else:
                fs_type = result.stdout.strip()
                if fs_type != "btrfs":
                    errors.append(f"Target root filesystem is {fs_type}, not btrfs.")

            # Verify subvolumes on target
            result = self.remote.run("sudo btrfs subvolume list /", timeout=10.0)
            if result.returncode == 0:
                existing_target_subvols = set()
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if "path" in parts:
                        path_idx = parts.index("path")
                        if path_idx + 1 < len(parts):
                            existing_target_subvols.add(parts[path_idx + 1])

                for subvol in subvolumes:
                    if subvol not in existing_target_subvols:
                        errors.append(
                            f"Subvolume '{subvol}' not found on target. "
                            f"Target must have matching btrfs subvolume layout."
                        )
            else:
                errors.append(f"Failed to list target btrfs subvolumes: {result.stderr}")

        except Exception as e:
            errors.append(f"Failed to validate target btrfs configuration: {e}")

        return errors

    @override
    def create_presync_snapshots(self, session_id: str) -> None:
        """Create pre-sync read-only snapshots of all configured subvolumes.

        Args:
            session_id: Sync session identifier

        Raises:
            SyncError: If snapshot creation fails
        """
        self.log(LogLevel.INFO, "Creating pre-sync snapshots", session_id=session_id)

        snapshot_dir = Path(self.config["snapshot_dir"])
        subvolumes: list[str] = self.config["subvolumes"]

        # Ensure snapshot directory exists on source
        try:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.log(LogLevel.CRITICAL, f"Failed to create snapshot directory on source: {e}")
            raise SyncError(f"Failed to create snapshot directory {snapshot_dir}: {e}") from e

        # Ensure snapshot directory exists on target
        try:
            result = self.remote.run(f"mkdir -p {snapshot_dir}", timeout=10.0)
            if result.returncode != 0:
                error_msg = f"Failed to create snapshot directory on target: {result.stderr}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg)
        except Exception as e:
            error_msg = f"Failed to create snapshot directory on target: {e}"
            self.log(LogLevel.CRITICAL, error_msg)
            raise SyncError(error_msg) from e

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

        # Create snapshots on source
        for subvol in subvolumes:
            snapshot_name = f"{subvol}-presync-{timestamp}-{session_id}"
            snapshot_path = snapshot_dir / snapshot_name

            self.log(LogLevel.FULL, f"Creating pre-sync snapshot for {subvol} on source")

            try:
                source_path = self._find_subvolume_path(subvol)
                subprocess.run(
                    ["sudo", "btrfs", "subvolume", "snapshot", "-r", str(source_path), str(snapshot_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.log(LogLevel.INFO, f"Created pre-sync snapshot on source: {snapshot_name}")
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to create pre-sync snapshot for {subvol} on source: {e.stderr}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

        # Create snapshots on target
        for subvol in subvolumes:
            snapshot_name = f"{subvol}-presync-{timestamp}-{session_id}"
            snapshot_path = snapshot_dir / snapshot_name

            self.log(LogLevel.FULL, f"Creating pre-sync snapshot for {subvol} on target")

            try:
                mount_point = self._find_subvolume_path(subvol)
                command = f"sudo btrfs subvolume snapshot -r {mount_point} {snapshot_path}"
                result = self.remote.run(command, timeout=30.0)

                if result.returncode != 0:
                    error_msg = f"Failed to create pre-sync snapshot for {subvol} on target: {result.stderr}"
                    self.log(LogLevel.CRITICAL, error_msg)
                    raise SyncError(error_msg)

                self.log(LogLevel.INFO, f"Created pre-sync snapshot on target: {snapshot_name}")
            except SyncError:
                raise
            except Exception as e:
                error_msg = f"Failed to create pre-sync snapshot for {subvol} on target: {e}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

    @override
    def create_postsync_snapshots(self, session_id: str) -> None:
        """Create post-sync read-only snapshots of all configured subvolumes.

        Args:
            session_id: Sync session identifier

        Raises:
            SyncError: If snapshot creation fails
        """
        self.log(LogLevel.INFO, "Creating post-sync snapshots", session_id=session_id)

        snapshot_dir = Path(self.config["snapshot_dir"])
        subvolumes: list[str] = self.config["subvolumes"]
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

        # Create post-sync snapshots on source
        for subvol in subvolumes:
            snapshot_name = f"{subvol}-postsync-{timestamp}-{session_id}"
            snapshot_path = snapshot_dir / snapshot_name

            self.log(LogLevel.FULL, f"Creating post-sync snapshot for {subvol} on source")

            try:
                source_path = self._find_subvolume_path(subvol)
                subprocess.run(
                    ["sudo", "btrfs", "subvolume", "snapshot", "-r", str(source_path), str(snapshot_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.log(LogLevel.INFO, f"Created post-sync snapshot on source: {snapshot_name}")
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to create post-sync snapshot for {subvol} on source: {e.stderr}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

        # Create post-sync snapshots on target
        for subvol in subvolumes:
            snapshot_name = f"{subvol}-postsync-{timestamp}-{session_id}"
            snapshot_path = snapshot_dir / snapshot_name

            self.log(LogLevel.FULL, f"Creating post-sync snapshot for {subvol} on target")

            try:
                mount_point = self._find_subvolume_path(subvol)
                command = f"sudo btrfs subvolume snapshot -r {mount_point} {snapshot_path}"
                result = self.remote.run(command, timeout=30.0)

                if result.returncode != 0:
                    error_msg = f"Failed to create post-sync snapshot for {subvol} on target: {result.stderr}"
                    self.log(LogLevel.CRITICAL, error_msg)
                    raise SyncError(error_msg)

                self.log(LogLevel.INFO, f"Created post-sync snapshot on target: {snapshot_name}")
            except SyncError:
                raise
            except Exception as e:
                error_msg = f"Failed to create post-sync snapshot for {subvol} on target: {e}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

    @override
    def cleanup_old_snapshots(
        self, keep_recent: int | None = None, older_than_days: int | None = None
    ) -> None:
        """Delete old snapshots to free disk space.

        Args:
            keep_recent: Keep this many recent snapshots (uses config if None)
            older_than_days: Delete snapshots older than this (uses config if None)
        """
        actual_older_than_days = older_than_days if older_than_days is not None else self.config["max_age_days"]
        actual_keep_recent = keep_recent if keep_recent is not None else self.config["keep_recent"]

        self.log(
            LogLevel.INFO,
            "Starting snapshot cleanup",
            older_than_days=actual_older_than_days,
            keep_recent=actual_keep_recent,
        )

        snapshot_dir = Path(self.config["snapshot_dir"])
        if not snapshot_dir.exists():
            self.log(LogLevel.INFO, "Snapshot directory does not exist, nothing to clean")
            return

        cutoff_date = datetime.now(UTC) - timedelta(days=actual_older_than_days)
        subvolumes: list[str] = self.config["subvolumes"]

        for subvol in subvolumes:
            # Find all snapshots for this subvolume (both presync and postsync)
            presync_snapshots = sorted(snapshot_dir.glob(f"{subvol}-presync-*"))
            postsync_snapshots = sorted(snapshot_dir.glob(f"{subvol}-postsync-*"))

            # Process presync snapshots
            snapshots_to_check = (
                presync_snapshots[:-actual_keep_recent] if actual_keep_recent > 0 else presync_snapshots
            )
            for snapshot_path in snapshots_to_check:
                try:
                    # Extract timestamp from snapshot name
                    name_parts = snapshot_path.name.split("-")
                    if len(name_parts) >= 3:
                        timestamp_str = name_parts[2]
                        snapshot_time = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                        if snapshot_time < cutoff_date:
                            self.log(LogLevel.FULL, f"Deleting old snapshot: {snapshot_path.name}")
                            subprocess.run(
                                ["sudo", "btrfs", "subvolume", "delete", str(snapshot_path)],
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            self.log(LogLevel.INFO, f"Deleted snapshot: {snapshot_path.name}")
                except (ValueError, subprocess.CalledProcessError) as e:
                    self.log(LogLevel.WARNING, f"Failed to process snapshot {snapshot_path.name}: {e}")

            # Process postsync snapshots
            snapshots_to_check = (
                postsync_snapshots[:-actual_keep_recent] if actual_keep_recent > 0 else postsync_snapshots
            )
            for snapshot_path in snapshots_to_check:
                try:
                    name_parts = snapshot_path.name.split("-")
                    if len(name_parts) >= 3:
                        timestamp_str = name_parts[2]
                        snapshot_time = datetime.strptime(timestamp_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
                        if snapshot_time < cutoff_date:
                            self.log(LogLevel.FULL, f"Deleting old snapshot: {snapshot_path.name}")
                            subprocess.run(
                                ["sudo", "btrfs", "subvolume", "delete", str(snapshot_path)],
                                capture_output=True,
                                text=True,
                                check=True,
                            )
                            self.log(LogLevel.INFO, f"Deleted snapshot: {snapshot_path.name}")
                except (ValueError, subprocess.CalledProcessError) as e:
                    self.log(LogLevel.WARNING, f"Failed to process snapshot {snapshot_path.name}: {e}")

        self.log(LogLevel.INFO, "Snapshot cleanup completed")

    @override
    def rollback_to_presync(self, session_id: str) -> None:
        """Restore system state from pre-sync snapshots.

        Args:
            session_id: Session ID to rollback to

        Raises:
            SyncError: If rollback fails
        """
        self.log(LogLevel.INFO, "Starting rollback to pre-sync state", session_id=session_id)

        snapshot_dir = Path(self.config["snapshot_dir"])
        subvolumes: list[str] = self.config["subvolumes"]

        # Verify all pre-sync snapshots exist
        for subvol in subvolumes:
            matching_snapshots = list(snapshot_dir.glob(f"{subvol}-presync-*-{session_id}"))
            if not matching_snapshots:
                error_msg = f"No pre-sync snapshot found for {subvol} with session {session_id}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg)

        # Perform rollback for each subvolume
        for subvol in subvolumes:
            matching_snapshots = list(snapshot_dir.glob(f"{subvol}-presync-*-{session_id}"))
            snapshot_path = matching_snapshots[0]

            self.log(LogLevel.INFO, f"Rolling back {subvol} from {snapshot_path.name}")

            try:
                current_path = self._find_subvolume_path(subvol)

                # Delete current subvolume
                self.log(LogLevel.FULL, f"Deleting current subvolume: {current_path}")
                subprocess.run(
                    ["sudo", "btrfs", "subvolume", "delete", str(current_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )

                # Create new read-write subvolume from snapshot
                self.log(LogLevel.FULL, f"Restoring subvolume from snapshot: {snapshot_path}")
                subprocess.run(
                    ["sudo", "btrfs", "subvolume", "snapshot", str(snapshot_path), str(current_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )

                self.log(LogLevel.INFO, f"Successfully rolled back {subvol}")
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to rollback {subvol}: {e.stderr}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

        self.log(LogLevel.INFO, "Rollback completed successfully", session_id=session_id)

    def _find_subvolume_path(self, subvol: str) -> Path:
        """Map flat subvolume name to mount point path.

        Args:
            subvol: Flat subvolume name (e.g., "@", "@home", "@root")

        Returns:
            Mount point path for the subvolume

        Note:
            This uses btrfs-specific mount point conventions. In future,
            this should use dynamic lookup from 'btrfs subvolume list /'.
        """
        # TODO: Use dynamic lookup from btrfs subvolume list instead of hardcoding
        # For now, use common Ubuntu btrfs layout conventions
        mount_map = {
            "@": Path("/"),
            "@home": Path("/home"),
            "@root": Path("/root"),
        }

        if subvol in mount_map:
            return mount_map[subvol]

        # For unknown subvolumes, try to find mount point dynamically
        # This is a placeholder - should be implemented properly
        raise SyncError(
            f"Unknown subvolume '{subvol}'. Currently only @, @home, @root are supported. "
            f"Custom subvolume support will be added in a future update."
        )
