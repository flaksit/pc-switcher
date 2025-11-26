"""Main CLI entry point for pc-switcher."""

from __future__ import annotations

import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from pcswitcher import __version__  # type: ignore[attr-defined]
from pcswitcher.cli.ui import TerminalUI
from pcswitcher.core.config import ConfigError, load_config
from pcswitcher.core.logging import configure_logging, create_log_file_path
from pcswitcher.core.job import SyncError
from pcswitcher.core.orchestrator import Orchestrator
from pcswitcher.core.session import SessionState, SyncSession, generate_session_id
from pcswitcher.remote.connection import SSHRemoteExecutor, TargetConnection
from pcswitcher.utils.lock import LockError, LockManager

console = Console()

# Record CLI invocation time for startup performance measurement (T125)
_cli_invocation_time = time.time()

app = typer.Typer(
    name="pc-switcher",
    help="Synchronization system for seamless switching between Linux desktop machines",
    add_completion=False,
    rich_markup_mode="rich",
)


def _render_sync_summary(
    session: SyncSession,
    final_state: SessionState,
    log_path: Path,
    timestamp: datetime,
) -> None:
    """Render the sync summary table to console.

    Args:
        session: Completed sync session
        final_state: Final session state
        log_path: Path to log file
        timestamp: Session start timestamp
    """
    from rich.table import Table

    duration = datetime.now(UTC) - timestamp
    duration_str = str(duration).split(".")[0]  # Remove microseconds

    console.print("\n[bold]Sync Summary[/bold]\n")

    summary_table = Table(show_header=True, header_style="bold magenta")
    summary_table.add_column("Metric", style="cyan", width=20)
    summary_table.add_column("Value", width=30)

    summary_table.add_row("Session ID", session.id)

    status_color = "green" if final_state == SessionState.COMPLETED else "red"
    summary_table.add_row(
        "Status",
        f"[bold {status_color}]{final_state.value.upper()}[/bold]",
    )
    summary_table.add_row("Duration", duration_str)
    summary_table.add_row("Jobs Executed", str(len(session.job_results)))

    jobs_succeeded = sum(1 for r in session.job_results.values() if r.value == "SUCCESS")
    jobs_failed = sum(1 for r in session.job_results.values() if r.value == "FAILED")

    summary_table.add_row("Jobs Succeeded", f"[green]{jobs_succeeded}[/green]")
    if jobs_failed > 0:
        summary_table.add_row("Jobs Failed", f"[red]{jobs_failed}[/red]")

    summary_table.add_row("Log File", str(log_path))

    console.print(summary_table)
    console.print()

    # Show job details if there are failures
    if jobs_failed > 0:
        console.print("[bold red]Failed Jobs:[/bold red]")
        for job_name, result in session.job_results.items():
            if result.value == "FAILED":
                console.print(f"  • {job_name}")
        console.print()


def _determine_exit_code(final_state: SessionState) -> int:
    """Determine CLI exit code based on final session state.

    Args:
        final_state: Final session state

    Returns:
        Exit code: 0 for success, 130 for abort (SIGINT), 1 for failure
    """
    if final_state == SessionState.COMPLETED:
        return 0
    elif final_state == SessionState.ABORTED:
        return 130  # SIGINT
    else:
        return 1


@app.command()
def sync(
    target: Annotated[str, typer.Argument(help="Target hostname or IP address")],
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to configuration file"),
    ] = None,
) -> None:
    """Synchronize this machine to the target machine.

    This command performs a complete sync operation, executing all enabled
    jobs in the configured order. The sync is uni-directional from this
    machine (source) to the target.

    Example:
        pc-switcher sync workstation-02
        pc-switcher sync 192.168.1.100 --config custom.yaml
    """
    exit_code = 0
    lock_manager: LockManager | None = None

    try:
        # Load configuration
        try:
            cfg = load_config(config)
        except (FileNotFoundError, ConfigError) as e:
            console.print(f"[red]Configuration error:[/red] {e}")
            raise typer.Exit(1) from e

        # Create log file
        timestamp = datetime.now(UTC)
        log_path = create_log_file_path(timestamp)

        # Create session
        session_id = generate_session_id()
        # Only include actually enabled jobs (not btrfs_snapshots which is now infrastructure)
        enabled_job_list = [
            name
            for name, enabled in cfg.sync_jobs.items()
            if enabled and name != "btrfs_snapshots"
        ]
        session = SyncSession(
            id=session_id,
            timestamp=timestamp,
            source_hostname=socket.gethostname(),
            target_hostname=target,
            enabled_jobs=enabled_job_list,
            state=SessionState.INITIALIZING,
        )

        # Acquire lock to prevent concurrent syncs
        lock_manager = LockManager(session_id)
        try:
            lock_manager.acquire_lock(interactive=True)
        except LockError as e:
            console.print(f"[red]Lock error:[/red] {e}")
            raise typer.Exit(1) from e

        # Configure logging
        configure_logging(
            log_file_level=cfg.log_file_level,
            log_cli_level=cfg.log_cli_level,
            log_file_path=log_path,
            session=session,
        )

        # Create UI
        ui = TerminalUI()

        # Create remote connection and executor
        connection = TargetConnection(target)
        connection.connect()
        remote = SSHRemoteExecutor(connection)

        # Create orchestrator
        orchestrator = Orchestrator(
            config=cfg,
            remote=remote,
            session=session,
            ui=ui,
        )

        # Pass CLI invocation time for startup performance measurement (T125)
        orchestrator.set_cli_invocation_time(_cli_invocation_time)

        # Run sync
        console.print(f"\n[bold blue]Starting sync session {session_id}[/bold blue]")
        console.print(f"Source: [cyan]{session.source_hostname}[/cyan]")
        console.print(f"Target: [cyan]{target}[/cyan]")
        console.print(f"Log: [dim]{log_path}[/dim]\n")

        final_state = orchestrator.run()

        # Display summary and determine exit code
        _render_sync_summary(session, final_state, log_path, timestamp)
        exit_code = _determine_exit_code(final_state)

    except KeyboardInterrupt:
        console.print("\n[yellow]Sync interrupted by user[/yellow]")
        exit_code = 130
    except SyncError as e:
        console.print(f"\n[red]Sync error:[/red] {e}")
        exit_code = 1
    except Exception as e:
        console.print(f"\n[red]Unexpected error:[/red] {e}")
        exit_code = 1
    finally:
        # Release lock
        if lock_manager is not None:
            lock_manager.release_lock()

    raise typer.Exit(exit_code)


@app.command()
def logs(
    last: Annotated[
        bool,
        typer.Option("--last", "-l", help="Show the most recent log file"),
    ] = False,
    session_id: Annotated[
        str | None,
        typer.Option("--session", "-s", help="Show logs for specific session ID"),
    ] = None,
) -> None:
    """Display sync operation logs.

    By default, lists all available log files. Use --last to display the most
    recent log, or --session to display logs for a specific session ID.

    Example:
        pc-switcher logs --last
        pc-switcher logs --session abc12345
    """
    log_dir = Path.home() / ".local" / "share" / "pc-switcher" / "logs"

    if not log_dir.exists():
        console.print("[yellow]No logs found[/yellow]")
        raise typer.Exit(0)

    log_files = sorted(log_dir.glob("sync-*.log"), reverse=True)

    if not log_files:
        console.print("[yellow]No sync logs found[/yellow]")
        raise typer.Exit(0)

    if session_id:
        # Find log file containing session ID
        matching_logs = [f for f in log_files if session_id in f.read_text()]
        if not matching_logs:
            console.print(f"[yellow]No logs found for session {session_id}[/yellow]")
            raise typer.Exit(0)
        log_file = matching_logs[0]
    elif last:
        log_file = log_files[0]
    else:
        # List all log files
        console.print("\n[bold]Available log files:[/bold]\n")
        for log_file in log_files:
            console.print(f"  {log_file.name}")
        console.print(f"\n[dim]Log directory: {log_dir}[/dim]\n")
        raise typer.Exit(0)

    # Display log file
    console.print(f"\n[bold]Log file: {log_file.name}[/bold]\n")
    console.print(log_file.read_text())


@app.command()
def rollback(
    session_id: Annotated[
        str,
        typer.Option("--session", "-s", help="Session ID to rollback to"),
    ],
) -> None:
    """Rollback to a previous sync state using btrfs snapshots.

    This command restores the system from pre-sync snapshots of a specific session.
    This is a destructive operation that replaces current subvolumes with their
    pre-sync snapshots.

    Example:
        pc-switcher rollback --session abc12345
    """
    try:
        # Load configuration
        cfg = load_config(None)
    except (FileNotFoundError, ConfigError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from e

    # Confirm with user
    console.print(f"\n[bold red]WARNING: This will rollback to snapshot session {session_id}[/bold red]")
    console.print("This is a destructive operation that will replace current data with snapshots.")
    response = console.input("\nAre you sure you want to proceed? Type 'yes' to confirm: ")

    if response.lower() != "yes":
        console.print("[yellow]Rollback cancelled[/yellow]")
        raise typer.Exit(0)

    try:
        # Connect to target (for rollback on target)
        connection = TargetConnection("localhost")
        connection.connect()
        remote = SSHRemoteExecutor(connection)

        # Import btrfs job to use rollback method
        from pcswitcher.jobs.btrfs_snapshots import BtrfsSnapshotsJob

        # Get config for btrfs job
        btrfs_config = cfg.job_configs.get("btrfs_snapshots", {})
        if not btrfs_config:
            console.print("[red]Error: btrfs_snapshots not configured[/red]")
            raise typer.Exit(1)

        # Create job instance and perform rollback
        btrfs_job = BtrfsSnapshotsJob(btrfs_config, remote)

        console.print(f"\n[bold blue]Rolling back to session {session_id}...[/bold blue]\n")

        try:
            btrfs_job.rollback_to_presync(session_id)
            console.print("\n[green bold]Rollback completed successfully[/green bold]")
            console.print(f"System has been restored to snapshot session {session_id}\n")
        except SyncError as e:
            console.print(f"\n[red]Rollback failed: {e}[/red]\n")
            raise typer.Exit(1) from e

    except Exception as e:
        console.print(f"[red]Unexpected error during rollback: {e}[/red]")
        raise typer.Exit(1) from e

@app.command()
def cleanup_snapshots(
    older_than: Annotated[
        str | None,
        typer.Option("--older-than", help="Delete snapshots older than this (e.g., '7d', '30d')"),
    ] = None,
    keep_recent: Annotated[
        int | None,
        typer.Option("--keep-recent", help="Minimum number of recent snapshots to keep. Uses config default."),
    ] = None,
) -> None:
    """Clean up old btrfs snapshots.

    This command removes old snapshots while keeping a minimum number of
    recent snapshots. Default values come from btrfs_snapshots config section.
    Use with caution.

    Example:
        pc-switcher cleanup-snapshots
        pc-switcher cleanup-snapshots --older-than 14d --keep-recent 5
    """
    import re
    from pathlib import Path

    # Get default config for snapshot settings
    try:
        cfg = load_config(None)
    except (FileNotFoundError, ConfigError) as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1) from e

    # Get btrfs config section
    btrfs_config = cfg.job_configs.get("btrfs_snapshots", {})
    if not btrfs_config:
        # Try top-level btrfs_snapshots section (not in sync_jobs since it's infrastructure)
        # Re-read raw config to get btrfs_snapshots section
        import yaml

        config_path = cfg.config_path
        with config_path.open("r") as f:
            raw_config = yaml.safe_load(f) or {}
        btrfs_config = raw_config.get("btrfs_snapshots", {})

    # Get default values from config
    config_keep_recent = btrfs_config.get("keep_recent", 3)
    config_max_age_days = btrfs_config.get("max_age_days", 7)

    # Use CLI args or fall back to config defaults
    actual_keep_recent = keep_recent if keep_recent is not None else config_keep_recent
    if older_than is not None:
        # Parse time expression (e.g., "7d", "2w", "30d")
        match = re.match(r"(\d+)([dwh])", older_than)
        if not match:
            console.print(
                f"[red]Invalid time format: {older_than}[/red]\n"
                f"Use format like: 7d (7 days), 2w (2 weeks), 24h (24 hours)"
            )
            raise typer.Exit(1)

        value, unit = int(match.group(1)), match.group(2)
        if unit == "d":
            max_age_days: float = value
        elif unit == "w":
            max_age_days = value * 7
        elif unit == "h":
            max_age_days = value / 24
        else:
            max_age_days = value
    else:
        max_age_days = config_max_age_days

    # Get snapshot directory from config
    snapshot_dir = Path(btrfs_config.get("snapshot_dir", "/.snapshots"))

    if not snapshot_dir.exists():
        console.print(f"[yellow]Snapshot directory does not exist: {snapshot_dir}[/yellow]")
        raise typer.Exit(0)

    # Find and delete old snapshots
    console.print(f"\n[bold]Cleaning up snapshots older than {max_age_days} days[/bold]\n")
    console.print(f"Snapshot directory: {snapshot_dir}")
    console.print(f"Keep recent: {actual_keep_recent} syncs")
    console.print(f"Delete older than: {max_age_days} days\n")

    from datetime import UTC, datetime, timedelta

    cutoff_date = datetime.now(UTC) - timedelta(days=max_age_days)

    # Group snapshots by session (include both presync and postsync)
    snapshots_by_session: dict[str, list[Path]] = {}
    # Collect both presync AND postsync snapshots
    presync_snapshots = list(snapshot_dir.glob("*-presync-*-*"))
    postsync_snapshots = list(snapshot_dir.glob("*-postsync-*-*"))
    all_snapshots = sorted(presync_snapshots + postsync_snapshots, key=lambda p: p.stat().st_mtime, reverse=True)

    for snapshot_path in all_snapshots:
        # Extract session ID from name (last component after last dash)
        parts = snapshot_path.name.split("-")
        if len(parts) >= 4:
            session_id = parts[-1]
            if session_id not in snapshots_by_session:
                snapshots_by_session[session_id] = []
            snapshots_by_session[session_id].append(snapshot_path)

    # Keep recent sessions, delete old ones
    deleted_count = 0
    kept_count = 0

    for i, (session_id, snapshots) in enumerate(sorted(snapshots_by_session.items(), reverse=True)):
        if i < actual_keep_recent:
            # Keep this session
            kept_count += len(snapshots)
            console.print(f"[green]Keeping[/green] {len(snapshots)} snapshots from session {session_id}")
        else:
            # Check age and delete if old enough
            mtime = datetime.fromtimestamp(snapshots[0].stat().st_mtime, tz=UTC)
            if mtime < cutoff_date:
                for snapshot_path in snapshots:
                    try:
                        subprocess.run(
                            ["sudo", "btrfs", "subvolume", "delete", str(snapshot_path)],
                            capture_output=True,
                            check=True,
                        )
                        console.print(f"[red]Deleted[/red] {snapshot_path.name}")
                        deleted_count += 1
                    except subprocess.CalledProcessError as e:
                        console.print(f"[yellow]Failed to delete {snapshot_path.name}: {e.stderr}[/yellow]")
            else:
                kept_count += len(snapshots)
                console.print(f"[green]Keeping[/green] {len(snapshots)} recent snapshots from session {session_id}")

    console.print("\n[bold]Cleanup summary:[/bold]")
    console.print(f"  Deleted: {deleted_count} snapshots")
    console.print(f"  Kept: {kept_count} snapshots\n")
    raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def version_callback(
    version: Annotated[
        bool,
        typer.Option("--version", "-v", help="Show version and exit"),
    ] = False,
) -> None:
    """Show version information."""
    if version:
        console.print(f"pc-switcher version {__version__}")
        raise typer.Exit(0)


def main() -> None:
    """Entry point for the CLI application."""
    app()


if __name__ == "__main__":
    main()
