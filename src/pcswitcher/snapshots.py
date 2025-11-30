"""Btrfs snapshot management for pc-switcher.

This module handles snapshot creation, validation, and cleanup operations
for btrfs subvolumes during the sync process.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pytimeparse2 import parse as parse_duration_seconds

from pcswitcher.models import CommandResult, Host, SnapshotPhase

if TYPE_CHECKING:
    from pcswitcher.executor import LocalExecutor, RemoteExecutor

__all__ = [
    "cleanup_snapshots",
    "create_snapshot",
    "parse_older_than",
    "session_folder_name",
    "snapshot_name",
    "validate_snapshots_directory",
    "validate_subvolume_exists",
]


def snapshot_name(subvolume: str, phase: SnapshotPhase) -> str:
    """Generate snapshot name per FR-010.

    Args:
        subvolume: Subvolume name (e.g., "@home")
        phase: Snapshot phase (PRE or POST)

    Returns:
        Snapshot name like "pre-@home-20251129T143022"
    """
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{phase.value}-{subvolume}-{timestamp}"


def session_folder_name(session_id: str) -> str:
    """Generate session folder name for organizing snapshots.

    Args:
        session_id: Unique session identifier

    Returns:
        Folder name like "20251129T143022-abc12345"
    """
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{timestamp}-{session_id}"


async def create_snapshot(
    executor: LocalExecutor | RemoteExecutor,
    source_path: str,
    snapshot_path: str,
) -> CommandResult:
    """Create read-only btrfs snapshot.

    Args:
        executor: Executor for the target machine (local or remote)
        source_path: Path to source subvolume
        snapshot_path: Path for snapshot destination

    Returns:
        CommandResult with exit code, stdout, stderr
    """
    cmd = f"sudo btrfs subvolume snapshot -r {source_path} {snapshot_path}"
    return await executor.run_command(cmd)


async def validate_snapshots_directory(
    executor: LocalExecutor | RemoteExecutor,
    host: Host,
) -> tuple[bool, str | None]:
    """Check if /.snapshots exists and is a subvolume, create if missing.

    Args:
        executor: Executor for the target machine
        host: Which machine is being checked (for error messages)

    Returns:
        Tuple of (success, error_message). error_message is None on success.
    """
    # Check if /.snapshots exists and is a subvolume
    result = await executor.run_command("sudo btrfs subvolume show /.snapshots 2>&1")

    if result.exit_code == 0:
        # /.snapshots exists and is a subvolume
        return (True, None)

    # /.snapshots doesn't exist or isn't a subvolume - try to create it
    create_result = await executor.run_command(
        "sudo btrfs subvolume create /.snapshots && sudo mkdir -p /.snapshots/pc-switcher"
    )

    if create_result.exit_code != 0:
        return (
            False,
            f"Failed to create /.snapshots subvolume on {host.value}: {create_result.stderr}",
        )

    return (True, None)


async def validate_subvolume_exists(
    executor: LocalExecutor | RemoteExecutor,
    subvolume: str,
    mount_point: str,
    host: Host,
) -> tuple[bool, str | None]:
    """Validate that a subvolume exists on the specified host.

    Args:
        executor: Executor for the target machine
        subvolume: Subvolume name (e.g., "@home")
        mount_point: Expected mount point (e.g., "/home")
        host: Which machine is being checked

    Returns:
        Tuple of (success, error_message). error_message is None on success.
    """
    result = await executor.run_command(f"sudo btrfs subvolume show {mount_point} 2>&1")

    if result.exit_code != 0:
        return (
            False,
            f"Subvolume {subvolume} not found at {mount_point} on {host.value}: {result.stderr}",
        )

    # Verify the name matches
    if subvolume not in result.stdout:
        return (
            False,
            f"Path {mount_point} on {host.value} is not subvolume {subvolume}",
        )

    return (True, None)


async def cleanup_snapshots(
    executor: LocalExecutor | RemoteExecutor,
    session_folder: str,
    keep_recent: int,
    max_age_days: int | None = None,
) -> list[str]:
    """Clean up old snapshots based on retention policy.

    Args:
        executor: Executor for the target machine
        session_folder: Session folder path (e.g., "/.snapshots/pc-switcher/20251129T143022-abc12345")
        keep_recent: Number of recent snapshots to keep
        max_age_days: Delete snapshots older than this many days (optional)

    Returns:
        List of deleted snapshot paths
    """
    # List all session folders in /.snapshots/pc-switcher/
    list_result = await executor.run_command("ls -1t /.snapshots/pc-switcher/ 2>/dev/null || true")

    if not list_result.stdout.strip():
        return []

    # Parse session folders (sorted newest first due to -t flag)
    session_folders = [
        f"/.snapshots/pc-switcher/{name.strip()}" for name in list_result.stdout.strip().split("\n") if name.strip()
    ]

    deleted: list[str] = []

    # Apply keep_recent policy
    folders_to_delete = session_folders[keep_recent:]

    # Apply max_age_days policy if specified
    if max_age_days is not None:
        cutoff_date = datetime.now() - timedelta(days=max_age_days)
        cutoff_timestamp = cutoff_date.strftime("%Y%m%dT%H%M%S")

        for folder in session_folders:
            # Extract timestamp from folder name (format: YYYYMMDDTHHMMSS-sessionid)
            match = re.match(r".*/(\d{8}T\d{6})-", folder)
            if match:
                folder_timestamp = match.group(1)
                if folder_timestamp < cutoff_timestamp and folder not in folders_to_delete:
                    folders_to_delete.append(folder)

    # Delete selected folders
    for folder in folders_to_delete:
        # List snapshots in this folder
        snapshot_list = await executor.run_command(f"ls -1 {folder} 2>/dev/null || true")
        if snapshot_list.stdout.strip():
            snapshots = [
                f"{folder}/{name.strip()}" for name in snapshot_list.stdout.strip().split("\n") if name.strip()
            ]

            # Delete each snapshot subvolume
            for snapshot in snapshots:
                delete_result = await executor.run_command(f"sudo btrfs subvolume delete {snapshot}")
                if delete_result.exit_code == 0:
                    deleted.append(snapshot)

        # Delete the session folder itself
        await executor.run_command(f"rmdir {folder} 2>/dev/null || true")

    return deleted


def parse_older_than(value: str) -> int:
    """Parse human-readable duration to days.

    Args:
        value: Duration string (e.g., "7d", "2w", "1m", "30 days")

    Returns:
        Number of days

    Raises:
        ValueError: If the duration format is invalid

    Examples:
        >>> parse_older_than("7d")
        7
        >>> parse_older_than("2w")
        14
        >>> parse_older_than("1m")
        30
    """
    seconds = parse_duration_seconds(value)
    if seconds is None:
        raise ValueError(f"Invalid duration format: {value}")
    # pytimeparse2 returns int, float, or timedelta - convert to total seconds first
    total_seconds = seconds.total_seconds() if isinstance(seconds, timedelta) else float(seconds)
    return int(total_seconds // 86400)  # Convert to days
