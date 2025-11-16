"""Btrfs snapshot management module for pc-switcher.

This module handles creation, cleanup, and rollback of btrfs snapshots
to ensure data safety during sync operations.

Btrfs snapshots are copy-on-write (COW), meaning they are instantaneous and
consume no disk space initially. Only blocks that change after snapshot creation
consume additional space. This makes snapshots ideal for backup/rollback without
significant disk wear.

Key operations:
- pre_sync: Create read-only snapshots before sync starts
- post_sync: Create read-only snapshots after sync completes
- rollback_to_presync: Restore system to pre-sync state on failure
- cleanup_old_snapshots: Remove old snapshots to free disk space
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, override

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.module import RemoteExecutor, SyncError, SyncModule


class BtrfsSnapshotsModule(SyncModule):
    """Manages btrfs snapshots for sync safety and rollback capability.

    This module is required and must run first in the sync pipeline.
    It creates read-only snapshots before and after sync operations,
    provides rollback functionality, and manages snapshot cleanup.

    Snapshot naming convention:
        {snapshot_dir}/{subvol}-{presync|postsync}-{timestamp}-{session_id}
        Example: /.snapshots/@-presync-20250116T123045Z-a1b2c3d4

    The naming includes:
    - subvol: The flat subvolume name (e.g., "@", "@home")
    - presync/postsync: Indicates when snapshot was taken
    - timestamp: ISO 8601 UTC timestamp for ordering
    - session_id: Links snapshot to specific sync session for rollback
    """

    def __init__(self, config: dict[str, Any], remote: RemoteExecutor) -> None:
        """Initialize BtrfsSnapshotsModule.

        Args:
            config: Module configuration with subvolumes, snapshot_dir, etc.
            remote: Remote executor interface
        """
        super().__init__(config, remote)
        self._session_id: str | None = None

    @property
    @override
    def name(self) -> str:
        """Module identifier."""
        return "btrfs_snapshots"

    @property
    @override
    def required(self) -> bool:
        """This is a required module for data safety."""
        return True

    @override
    def get_config_schema(self) -> dict[str, Any]:
        """Return JSON Schema for configuration validation.

        Returns:
            Schema requiring subvolumes array, snapshot_dir, keep_recent, max_age_days
        """
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
    def validate(self) -> list[str]:
        """Validate btrfs filesystem and subvolumes exist on both source and target.

        This validation is read-only and makes no state changes. It verifies:
        1. Root filesystem is btrfs on source (required for snapshot operations)
        2. All configured subvolumes exist in btrfs subvolume list on source
        3. Target machine has btrfs filesystem
        4. All configured subvolumes exist on target

        Returns:
            List of validation errors (empty if valid). Each error message
            should be user-actionable with clear remediation steps.
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
                    f"PC-switcher requires btrfs for snapshot support. "
                    f"Please install Ubuntu with btrfs filesystem."
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
            # Parse btrfs output format: "ID 256 gen 123 top level 5 path @"
            # We extract the path field which contains the flat subvolume name
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
                        f"Run 'sudo btrfs subvolume list /' to see available subvolumes, "
                        f"then update config to match."
                    )
        except subprocess.CalledProcessError as e:
            errors.append(
                f"Failed to list source btrfs subvolumes: {e.stderr}. Ensure sudo access is configured for btrfs commands."
            )

        # Check target filesystem and subvolumes
        try:
            result = self._remote.run("stat -f -c '%T' /", timeout=10.0)
            if result.returncode != 0:
                errors.append(f"Failed to check target filesystem type: {result.stderr}")
            else:
                fs_type = result.stdout.strip()
                if fs_type != "btrfs":
                    errors.append(
                        f"Target root filesystem is {fs_type}, not btrfs. "
                        f"Target machine must have btrfs filesystem."
                    )

            # Verify subvolumes on target
            result = self._remote.run("sudo btrfs subvolume list /", timeout=10.0)
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
    def pre_sync(self) -> None:
        """Create pre-sync read-only snapshots of all configured subvolumes on both machines.

        These snapshots serve as the rollback point if sync fails. They are:
        - Read-only: Prevents accidental modification
        - Copy-on-write: Instantaneous creation, no initial disk space
        - Session-tagged: Can be correlated with sync session for rollback
        - Dual-target: Created on both source and target machines

        Raises:
            SyncError: If snapshot creation fails for any subvolume.
                       Failure here aborts the entire sync operation.
        """
        from pcswitcher.core.session import generate_session_id

        self._session_id = generate_session_id()
        self.log(LogLevel.INFO, "Creating pre-sync snapshots", session_id=self._session_id)

        snapshot_dir = Path(self.config["snapshot_dir"])
        subvolumes: list[str] = self.config["subvolumes"]

        # Ensure snapshot directory exists on source with proper permissions
        try:
            snapshot_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.log(LogLevel.CRITICAL, f"Failed to create snapshot directory on source: {e}")
            raise SyncError(
                f"Failed to create snapshot directory {snapshot_dir}: {e}. "
                f"Ensure directory is writable or create with: sudo mkdir -p {snapshot_dir}"
            ) from e

        # Ensure snapshot directory exists on target
        try:
            result = self._remote.run(f"mkdir -p {snapshot_dir}", timeout=10.0)
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
            # Build snapshot name with all correlation metadata
            snapshot_name = f"{subvol}-presync-{timestamp}-{self._session_id}"
            snapshot_path = snapshot_dir / snapshot_name

            self.log(
                LogLevel.FULL,
                f"Creating pre-sync snapshot for {subvol} on source",
                snapshot_path=str(snapshot_path),
            )

            try:
                # Resolve flat subvolume name to actual mount point
                source_path = self._find_subvolume_path(subvol)

                # Create read-only snapshot using btrfs command
                # -r flag makes snapshot read-only (safer for backup purposes)
                subprocess.run(
                    ["sudo", "btrfs", "subvolume", "snapshot", "-r", str(source_path), str(snapshot_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.log(
                    LogLevel.INFO,
                    f"Created pre-sync snapshot on source: {snapshot_name}",
                    subvolume=subvol,
                    location="source",
                )
            except subprocess.CalledProcessError as e:
                error_msg = (
                    f"Failed to create pre-sync snapshot for {subvol} on source: {e.stderr}. "
                    f"Check sudo permissions for btrfs commands."
                )
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

        # Create snapshots on target
        for subvol in subvolumes:
            snapshot_name = f"{subvol}-presync-{timestamp}-{self._session_id}"
            snapshot_path = snapshot_dir / snapshot_name

            self.log(
                LogLevel.FULL,
                f"Creating pre-sync snapshot for {subvol} on target",
                snapshot_path=str(snapshot_path),
            )

            try:
                # Use remote executor to create snapshot on target
                mount_point = self._find_subvolume_path(subvol)
                command = f"sudo btrfs subvolume snapshot -r {mount_point} {snapshot_path}"
                result = self._remote.run(command, timeout=30.0)

                if result.returncode != 0:
                    error_msg = f"Failed to create pre-sync snapshot for {subvol} on target: {result.stderr}"
                    self.log(LogLevel.CRITICAL, error_msg)
                    raise SyncError(error_msg)

                self.log(
                    LogLevel.INFO,
                    f"Created pre-sync snapshot on target: {snapshot_name}",
                    subvolume=subvol,
                    location="target",
                )
            except SyncError:
                raise
            except Exception as e:
                error_msg = f"Failed to create pre-sync snapshot for {subvol} on target: {e}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

    @override
    def sync(self) -> None:
        """No sync operation for snapshot module.

        Snapshots are created in pre_sync and post_sync phases.
        """
        pass

    @override
    def post_sync(self) -> None:
        """Create post-sync read-only snapshots of all configured subvolumes on both machines.

        Raises:
            SyncError: If snapshot creation fails
        """
        self.log(LogLevel.INFO, "Creating post-sync snapshots", session_id=self._session_id)

        snapshot_dir = Path(self.config["snapshot_dir"])
        subvolumes: list[str] = self.config["subvolumes"]
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

        # Create post-sync snapshots on source
        for subvol in subvolumes:
            # Snapshot naming: {subvol}-postsync-{timestamp}-{session_id}
            snapshot_name = f"{subvol}-postsync-{timestamp}-{self._session_id}"
            snapshot_path = snapshot_dir / snapshot_name

            self.log(
                LogLevel.FULL,
                f"Creating post-sync snapshot for {subvol} on source",
                snapshot_path=str(snapshot_path),
            )

            try:
                # Find the mount point for this subvolume
                source_path = self._find_subvolume_path(subvol)

                # Create read-only snapshot
                subprocess.run(
                    ["sudo", "btrfs", "subvolume", "snapshot", "-r", str(source_path), str(snapshot_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.log(
                    LogLevel.INFO,
                    f"Created post-sync snapshot on source: {snapshot_name}",
                    subvolume=subvol,
                    location="source",
                )
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to create post-sync snapshot for {subvol} on source: {e.stderr}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

        # Create post-sync snapshots on target
        for subvol in subvolumes:
            snapshot_name = f"{subvol}-postsync-{timestamp}-{self._session_id}"
            snapshot_path = snapshot_dir / snapshot_name

            self.log(
                LogLevel.FULL,
                f"Creating post-sync snapshot for {subvol} on target",
                snapshot_path=str(snapshot_path),
            )

            try:
                # Use remote executor to create snapshot on target
                mount_point = self._find_subvolume_path(subvol)
                command = f"sudo btrfs subvolume snapshot -r {mount_point} {snapshot_path}"
                result = self._remote.run(command, timeout=30.0)

                if result.returncode != 0:
                    error_msg = f"Failed to create post-sync snapshot for {subvol} on target: {result.stderr}"
                    self.log(LogLevel.CRITICAL, error_msg)
                    raise SyncError(error_msg)

                self.log(
                    LogLevel.INFO,
                    f"Created post-sync snapshot on target: {snapshot_name}",
                    subvolume=subvol,
                    location="target",
                )
            except SyncError:
                raise
            except Exception as e:
                error_msg = f"Failed to create post-sync snapshot for {subvol} on target: {e}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

    @override
    def abort(self, timeout: float) -> None:
        """Stop snapshot operations.

        Args:
            timeout: Maximum time to spend in cleanup (seconds)

        Snapshot creation is atomic at btrfs level, so no cleanup needed.
        """
        self.log(LogLevel.INFO, "Snapshot module abort called", timeout=timeout)

    def rollback_to_presync(self, session_id: str) -> None:
        """Restore system state from pre-sync snapshots.

        This is a destructive operation that replaces current subvolumes
        with their pre-sync snapshots.

        Args:
            session_id: Session ID to rollback to

        Raises:
            SyncError: If rollback fails
        """
        self.log(LogLevel.INFO, "Starting rollback to pre-sync state", session_id=session_id)

        snapshot_dir = Path(self.config["snapshot_dir"])
        subvolumes: list[str] = self.config["subvolumes"]

        # First, verify all pre-sync snapshots exist
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

            self.log(
                LogLevel.INFO,
                f"Rolling back {subvol} from {snapshot_path.name}",
                subvolume=subvol,
            )

            try:
                # Get the current subvolume path
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

                self.log(LogLevel.INFO, f"Successfully rolled back {subvol}", subvolume=subvol)
            except subprocess.CalledProcessError as e:
                error_msg = f"Failed to rollback {subvol}: {e.stderr}"
                self.log(LogLevel.CRITICAL, error_msg)
                raise SyncError(error_msg) from e

        self.log(LogLevel.INFO, "Rollback completed successfully", session_id=session_id)

    def cleanup_old_snapshots(self, older_than_days: int | None = None, keep_recent: int | None = None) -> None:
        """Delete old snapshots to free disk space.

        Args:
            older_than_days: Delete snapshots older than this (uses config default if None)
            keep_recent: Keep this many recent snapshots per subvolume (uses config default if None)
        """
        if older_than_days is None:
            older_than_days = self.config["max_age_days"]
        if keep_recent is None:
            keep_recent = self.config["keep_recent"]

        self.log(
            LogLevel.INFO,
            "Starting snapshot cleanup",
            older_than_days=older_than_days,
            keep_recent=keep_recent,
        )

        snapshot_dir = Path(self.config["snapshot_dir"])
        if not snapshot_dir.exists():
            self.log(LogLevel.INFO, "Snapshot directory does not exist, nothing to clean")
            return

        # Ensure older_than_days is not None (already set from config if None)
        assert older_than_days is not None
        cutoff_date = datetime.now(UTC) - timedelta(days=older_than_days)
        subvolumes: list[str] = self.config["subvolumes"]

        for subvol in subvolumes:
            # Find all snapshots for this subvolume
            all_snapshots = sorted(
                snapshot_dir.glob(f"{subvol}-*-*-*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )

            # Keep most recent N snapshots
            snapshots_to_keep = set(all_snapshots[:keep_recent])

            # Check age for remaining snapshots
            for snapshot_path in all_snapshots:
                if snapshot_path in snapshots_to_keep:
                    continue

                # Get modification time
                mtime = datetime.fromtimestamp(snapshot_path.stat().st_mtime, tz=UTC)

                if mtime < cutoff_date:
                    self.log(
                        LogLevel.FULL,
                        f"Deleting old snapshot: {snapshot_path.name}",
                        age_days=(datetime.now(UTC) - mtime).days,
                    )

                    try:
                        subprocess.run(
                            ["sudo", "btrfs", "subvolume", "delete", str(snapshot_path)],
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        self.log(LogLevel.INFO, f"Deleted snapshot: {snapshot_path.name}")
                    except subprocess.CalledProcessError as e:
                        self.log(
                            LogLevel.ERROR,
                            f"Failed to delete snapshot {snapshot_path.name}: {e.stderr}",
                        )

        self.log(LogLevel.INFO, "Snapshot cleanup completed")

    def _find_subvolume_path(self, subvol: str) -> Path:
        """Find the mount point for a subvolume.

        This maps flat btrfs subvolume names to their actual mount points.
        Ubuntu's standard subvolume layout mounts @ at /, @home at /home, etc.

        Args:
            subvol: Flat subvolume name (e.g., "@", "@home")

        Returns:
            Path to mounted subvolume

        Raises:
            SyncError: If subvolume mount point cannot be determined.
                       This indicates a non-standard subvolume layout.
        """
        # Standard Ubuntu btrfs subvolume to mount point mappings
        # These follow the common Ubuntu installer defaults
        common_mounts = {
            "@": Path("/"),
            "@home": Path("/home"),
            "@root": Path("/root"),
        }

        if subvol in common_mounts:
            return common_mounts[subvol]

        # Non-standard subvolume requires manual configuration
        # Future enhancement: parse /etc/fstab to determine mount points automatically
        raise SyncError(
            f"Cannot determine mount point for subvolume '{subvol}'. "
            f"Standard subvolumes are: {', '.join(common_mounts.keys())}. "
            f"For custom subvolumes, please add mapping to configuration."
        )
