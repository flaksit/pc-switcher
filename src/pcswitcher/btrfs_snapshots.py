"""Btrfs snapshot management for pc-switcher.

This module handles snapshot creation, validation, and cleanup operations
for btrfs subvolumes during the sync process.
"""

from __future__ import annotations

import asyncio
import shlex
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pytimeparse2 import parse as parse_duration_seconds

from pcswitcher.executor import Executor, LocalExecutor
from pcswitcher.models import CommandResult, Host, Snapshot, SnapshotPhase

if TYPE_CHECKING:
    from collections.abc import Callable

# Type alias for the console print function
type PrintFunction = Callable[[str], None]

__all__ = [
    "cleanup_snapshots",
    "create_snapshot",
    "delete_all_snapshots",
    "list_snapshots",
    "parse_older_than",
    "run_snapshot_cleanup",
    "session_folder_name",
    "snapshot_name",
    "validate_snapshots_directory",
    "validate_subvolume_exists",
]


def snapshot_name(subvolume: str, phase: SnapshotPhase) -> str:
    """Generate snapshot name per CORE-FR-SNAP-NAME.

    Args:
        subvolume: Subvolume name (e.g., "@home")
        phase: Snapshot phase (PRE or POST)

    Returns:
        Snapshot name like "pre-@home-20251129T143022"
    """
    return f"{phase.value}-{subvolume}-{_snapshot_timestamp()}"


def session_folder_name(session_id: str) -> str:
    """Generate session folder name for organizing snapshots.

    Args:
        session_id: Unique session identifier

    Returns:
        Folder name like "20251129T143022-abc12345"
    """
    return f"{_snapshot_timestamp()}-{session_id}"


def _snapshot_timestamp() -> str:
    """Now, as the `%Y%m%dT%H%M%S` stamp every snapshot and session folder is named by.

    UTC, not local time. The stamp is the only ordering key retention has — `list_snapshots`
    parses it back out of the path and `cleanup_snapshots` ranks sessions by it — so it has
    to mean the same instant wherever it was written. Local time makes two machines in
    different zones, or one machine either side of a DST change, produce names that sort
    against each other by their offset instead of by when they happened, and the older
    session then outranks the newer one.
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S")


async def create_snapshot(
    executor: Executor,
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
    return await executor.run_command(cmd, mutates=f"create read-only snapshot {snapshot_path} of {source_path}")


async def validate_snapshots_directory(
    executor: Executor,
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
        "sudo btrfs subvolume create /.snapshots && sudo mkdir -p /.snapshots/pc-switcher",
        mutates="create the /.snapshots subvolume and its pc-switcher folder on this machine",
    )

    if create_result.exit_code != 0:
        return (
            False,
            f"Failed to create /.snapshots subvolume on {host.value}: {create_result.stderr}",
        )

    return (True, None)


async def validate_subvolume_exists(
    executor: Executor,
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
    executor: Executor,
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

    # One command for the whole tree, not one per session folder: each command is a
    # full SSH round trip, and session folders can number in the hundreds (empty ones
    # included), which once turned this listing into minutes of wall time.
    list_result = await executor.run_command(
        "find /.snapshots/pc-switcher -mindepth 2 -maxdepth 2 2>/dev/null || true"
    )

    for line in list_result.stdout.splitlines():
        snap_path = line.strip()
        if not snap_path:
            continue
        try:
            snapshots.append(Snapshot.from_path(snap_path, host))
        except ValueError:
            # Skip snapshots that don't match our naming convention
            continue

    # Sort by timestamp, newest first
    snapshots.sort(key=lambda s: s.timestamp, reverse=True)
    return snapshots


async def cleanup_snapshots(
    executor: Executor,
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
        cutoff_date = datetime.now(UTC) - timedelta(days=max_age_days)

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
        delete_result = await executor.run_command(
            f"sudo btrfs subvolume delete {snap.path}",
            mutates=f"permanently delete snapshot {snap.path} and everything it preserves",
        )
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
            await executor.run_command(
                f"rmdir {folder_path} 2>/dev/null || true",
                mutates=f"remove the emptied snapshot session folder {folder_path}",
            )

    return deleted


# Shell script to delete all pc-switcher btrfs subvolumes recursively.
# Uses btrfs subvolume delete (fast) instead of rm -rf (slow for subvolumes).
# Must delete children before parents since btrfs subvolume delete is not recursive
# in btrfs-progs < 6.12.
# Based on pattern from tests/integration/scripts/reset-vm.sh
_DELETE_ALL_SNAPSHOTS_SCRIPT = r"""
delete_subvol_recursive() {
    local path="$1"
    local child
    # btrfs subvolume list -o shows child subvolumes
    # Output paths like @snapshots/pc-switcher/..., convert to /.snapshots/...
    btrfs subvolume list -o "$path" 2>/dev/null | awk '{print $NF}' \
        | sed 's/^@snapshots/\/.snapshots/' \
        | while read -r child; do
        # Safety: only delete paths under /.snapshots/pc-switcher
        if [[ "$child" != /.snapshots/pc-switcher* ]]; then
            echo "ERROR: Unexpected subvolume path: '$child', skipping" >&2
            continue
        fi
        delete_subvol_recursive "$child"
    done
    # Verify it's still a subvolume before deleting (may have been deleted already)
    if btrfs subvolume show "$path" >/dev/null 2>&1; then
        btrfs subvolume delete "$path" 2>/dev/null || true
    fi
}

# Find and delete all pc-switcher subvolumes
btrfs subvolume list / 2>/dev/null | awk '{print $NF}' | grep '^@snapshots/pc-switcher' \
    | sed 's/^@snapshots/\/.snapshots/' \
    | while read -r abs_path; do
    delete_subvol_recursive "$abs_path"
done

# Remove the now-empty session folders (plain directories, nested ones included:
# -delete implies depth-first, so a folder emptied during the same sweep goes
# too). Leaving them behind makes /.snapshots/pc-switcher grow without bound,
# and everything that scans it pays for the clutter.
find /.snapshots/pc-switcher -mindepth 1 -type d -empty -delete 2>/dev/null || true
"""


async def delete_all_snapshots(executor: Executor) -> CommandResult:
    """Delete all pc-switcher snapshots using btrfs subvolume delete.

    This function forcefully deletes ALL snapshots under /.snapshots/pc-switcher/,
    handling nested subvolumes by deleting children before parents. It uses
    `btrfs subvolume delete` which is much faster than `rm -rf` for subvolumes.
    Emptied session folders are removed as well so the directory does not
    accumulate stale entries across runs.

    Use cases:
    - Test cleanup between test runs
    - Emergency reset of snapshot state

    For normal cleanup based on retention policies, use cleanup_snapshots() instead.

    Args:
        executor: Executor for the target machine (local or remote)

    Returns:
        CommandResult with exit code, stdout, stderr
    """
    # `shlex.quote`, never `!r`: Python's repr escapes newlines to the two characters
    # `\` and `n`, which collapses the whole script onto one line and hands bash a
    # syntax error it swallows into `|| true`. That made this function a silent no-op.
    return await executor.run_command(
        f"sudo bash -c {shlex.quote(_DELETE_ALL_SNAPSHOTS_SCRIPT)}",
        mutates="permanently delete EVERY pc-switcher snapshot on this machine, losing all rollback points",
    )


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


def run_snapshot_cleanup(
    keep_recent: int,
    max_age_days: int | None,
    dry_run: bool,
    console_print: PrintFunction,
) -> int:
    """Run the snapshot cleanup operation.

    Note: Uses asyncio.run() because cleanup_snapshots() is async. The async
    implementation is required for reuse by the orchestrator which runs in an
    async context. The CLI cleanup command itself doesn't benefit from async
    (all operations are local and sequential), but we use the shared async
    implementation to avoid code duplication.

    Args:
        keep_recent: Number of recent session folders to keep
        max_age_days: Maximum age in days for snapshots (optional)
        dry_run: If True, show what would be deleted without deleting
        console_print: Function to print to the console

    Returns:
        Exit code: 0=success, 1=error
    """
    return asyncio.run(_async_snapshot_cleanup(keep_recent, max_age_days, dry_run, console_print))


async def _async_snapshot_cleanup(
    keep_recent: int,
    max_age_days: int | None,
    dry_run: bool,
    console_print: PrintFunction,
) -> int:
    """Async implementation of snapshot cleanup.

    Args:
        keep_recent: Number of recent session folders to keep
        max_age_days: Maximum age in days for snapshots (optional)
        dry_run: If True, show what would be deleted without deleting
        console_print: Function to print to the console

    Returns:
        Exit code: 0=success, 1=error
    """
    try:
        executor = LocalExecutor()

        if dry_run:
            console_print(f"[yellow]DRY RUN:[/yellow] Would delete snapshots keeping {keep_recent} most recent")
            if max_age_days is not None:
                console_print(f"[yellow]DRY RUN:[/yellow] Would also delete snapshots older than {max_age_days} days")
            console_print("\n[dim]Note: Actual deletion not implemented yet for dry-run mode[/dim]")
            return 0

        console_print(f"Cleaning up snapshots (keeping {keep_recent} most recent sessions)")
        if max_age_days is not None:
            console_print(f"Also deleting snapshots older than {max_age_days} days")

        deleted = await cleanup_snapshots(
            executor=executor,
            host=Host.SOURCE,
            keep_recent=keep_recent,
            max_age_days=max_age_days,
        )

        if deleted:
            console_print(f"\n[green]Successfully deleted {len(deleted)} snapshot(s):[/green]")
            for snapshot in deleted:
                console_print(f"  - {snapshot.path}")
        else:
            console_print("\n[yellow]No snapshots were deleted[/yellow]")

        return 0

    except Exception as e:
        console_print(f"\n[bold red]Cleanup failed:[/bold red] {e}")
        return 1
