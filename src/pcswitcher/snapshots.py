"""Btrfs snapshot management for pc-switcher.

This module handles snapshot creation, validation, and cleanup operations
for btrfs subvolumes during the sync process.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pytimeparse2 import parse as parse_duration_seconds

from pcswitcher.models import CommandResult, Host, Snapshot, SnapshotPhase

if TYPE_CHECKING:
    from pcswitcher.executor import LocalExecutor, RemoteExecutor

__all__ = [
    "cleanup_snapshots",
    "create_snapshot",
    "list_snapshots",
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


async def list_snapshots(
    executor: LocalExecutor | RemoteExecutor,
    host: Host,
) -> list[Snapshot]:
    """List all pc-switcher snapshots on a machine.

    Args:
        executor: Executor for the target machine
        host: Which machine is being queried (SOURCE or TARGET)

    Returns:
        List of Snapshot objects, sorted by timestamp (newest first)
    """
    snapshots: list[Snapshot] = []

    # List all session folders
    list_result = await executor.run_command("ls -1 /.snapshots/pc-switcher/ 2>/dev/null || true")
    if not list_result.stdout.strip():
        return []

    session_folders = [name.strip() for name in list_result.stdout.strip().split("\n") if name.strip()]

    for folder_name in session_folders:
        folder_path = f"/.snapshots/pc-switcher/{folder_name}"

        # List snapshots in this folder
        snap_result = await executor.run_command(f"ls -1 {folder_path} 2>/dev/null || true")
        if not snap_result.stdout.strip():
            continue

        snap_names = [name.strip() for name in snap_result.stdout.strip().split("\n") if name.strip()]

        for snap_name in snap_names:
            snap_path = f"{folder_path}/{snap_name}"
            try:
                snapshot = Snapshot.from_path(snap_path, host)
                snapshots.append(snapshot)
            except ValueError:
                # Skip snapshots that don't match our naming convention
                continue

    # Sort by timestamp, newest first
    snapshots.sort(key=lambda s: s.timestamp, reverse=True)
    return snapshots


async def cleanup_snapshots(
    executor: LocalExecutor | RemoteExecutor,
    host: Host,
    keep_recent: int,
    max_age_days: int | None = None,
) -> list[Snapshot]:
    """Clean up old snapshots based on retention policy.

    Applies two retention policies:
    1. keep_recent: Always keep the N most recent sync sessions (by session_id)
    2. max_age_days: Delete snapshots older than N days (optional)

    A session is protected by keep_recent even if it's older than max_age_days.

    Args:
        executor: Executor for the target machine
        host: Which machine to clean up (SOURCE or TARGET)
        keep_recent: Number of recent sync sessions to keep
        max_age_days: Delete snapshots older than this many days (optional)

    Returns:
        List of deleted Snapshot objects
    """
    # Get all snapshots using list_snapshots()
    all_snapshots = await list_snapshots(executor, host)
    if not all_snapshots:
        return []

    # Group snapshots by session_id
    sessions: dict[str, list[Snapshot]] = {}
    for snap in all_snapshots:
        sessions.setdefault(snap.session_id, []).append(snap)

    # Sort sessions by newest snapshot timestamp (newest first)
    sorted_sessions = sorted(
        sessions.items(),
        key=lambda x: max(s.timestamp for s in x[1]),
        reverse=True,
    )

    # Identify protected sessions (keep_recent most recent)
    protected_session_ids = {sid for sid, _ in sorted_sessions[:keep_recent]}

    # Determine which snapshots to delete
    snapshots_to_delete: list[Snapshot] = []

    if max_age_days is not None:
        cutoff_date = datetime.now() - timedelta(days=max_age_days)

        for session_id, session_snaps in sorted_sessions:
            if session_id in protected_session_ids:
                continue  # Protected by keep_recent

            # Check if session is older than max_age_days
            newest_in_session = max(s.timestamp for s in session_snaps)
            if newest_in_session < cutoff_date:
                snapshots_to_delete.extend(session_snaps)
    else:
        # No age limit - just delete sessions beyond keep_recent
        for _session_id, session_snaps in sorted_sessions[keep_recent:]:
            snapshots_to_delete.extend(session_snaps)

    # Delete the snapshots
    deleted: list[Snapshot] = []
    deleted_session_ids: set[str] = set()

    for snap in snapshots_to_delete:
        delete_result = await executor.run_command(f"sudo btrfs subvolume delete {snap.path}")
        if delete_result.exit_code == 0:
            deleted.append(snap)
            deleted_session_ids.add(snap.session_id)

    # Clean up empty session folders
    for session_id in deleted_session_ids:
        # Check if all snapshots in session were deleted
        session_snaps = sessions[session_id]
        if all(s in deleted for s in session_snaps):
            # Extract folder path from first snapshot path
            folder_path = "/".join(session_snaps[0].path.split("/")[:-1])
            await executor.run_command(f"rmdir {folder_path} 2>/dev/null || true")

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
