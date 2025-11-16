"""Main CLI entry point for pc-switcher."""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from pcswitcher import __version__  # type: ignore[attr-defined]
from pcswitcher.cli.ui import TerminalUI
from pcswitcher.core.config import ConfigError, load_config
from pcswitcher.core.logging import configure_logging, create_log_file_path
from pcswitcher.core.module import SyncError
from pcswitcher.core.orchestrator import Orchestrator
from pcswitcher.core.session import SessionState, SyncSession, generate_session_id
from pcswitcher.remote.connection import SSHRemoteExecutor, TargetConnection

console = Console()

app = typer.Typer(
    name="pc-switcher",
    help="Synchronization system for seamless switching between Linux desktop machines",
    add_completion=False,
    rich_markup_mode="rich",
)


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
    modules in the configured order. The sync is uni-directional from this
    machine (source) to the target.

    Example:
        pc-switcher sync workstation-02
        pc-switcher sync 192.168.1.100 --config custom.yaml
    """
    exit_code = 0

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
        session = SyncSession(
            id=session_id,
            timestamp=timestamp,
            source_hostname=socket.gethostname(),
            target_hostname=target,
            enabled_modules=list(cfg.sync_modules.keys()),
            state=SessionState.INITIALIZING,
        )

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

        # Run sync
        console.print(f"\n[bold blue]Starting sync session {session_id}[/bold blue]")
        console.print(f"Source: [cyan]{session.source_hostname}[/cyan]")
        console.print(f"Target: [cyan]{target}[/cyan]")
        console.print(f"Log: [dim]{log_path}[/dim]\n")

        final_state = orchestrator.run()

        # Determine exit code
        if final_state == SessionState.COMPLETED:
            exit_code = 0
        elif final_state == SessionState.ABORTED:
            exit_code = 130  # SIGINT
        else:
            exit_code = 1

    except KeyboardInterrupt:
        console.print("\n[yellow]Sync interrupted by user[/yellow]")
        exit_code = 130
    except SyncError as e:
        console.print(f"\n[red]Sync error:[/red] {e}")
        exit_code = 1
    except Exception as e:
        console.print(f"\n[red]Unexpected error:[/red] {e}")
        exit_code = 1

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
def cleanup_snapshots(
    older_than: Annotated[
        str,
        typer.Option("--older-than", help="Delete snapshots older than this (e.g., '7d', '30d')"),
    ] = "7d",
    keep_recent: Annotated[
        int,
        typer.Option("--keep-recent", help="Minimum number of recent snapshots to keep"),
    ] = 3,
) -> None:
    """Clean up old btrfs snapshots.

    This command removes old snapshots while keeping a minimum number of
    recent snapshots. Use with caution.

    Example:
        pc-switcher cleanup-snapshots
        pc-switcher cleanup-snapshots --older-than 14d --keep-recent 5
    """
    console.print("[yellow]Not implemented yet[/yellow]")
    console.print("This command will clean up snapshots older than the specified time")
    console.print(f"Parameters: older_than={older_than}, keep_recent={keep_recent}")
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
