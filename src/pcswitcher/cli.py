"""CLI entry point for pc-switcher using Typer."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from pcswitcher.config import Configuration, ConfigurationError
from pcswitcher.logger import get_latest_log_file, get_logs_directory
from pcswitcher.orchestrator import Orchestrator
from pcswitcher.snapshots import parse_older_than

# Cleanup timeout for graceful shutdown after SIGINT.
# After first SIGINT, cleanup has this many seconds to complete.
# Second SIGINT or timeout expiry forces immediate termination.
CLEANUP_TIMEOUT_SECONDS = 30

# Create Typer app
app = typer.Typer(
    name="pc-switcher",
    help="Synchronization system for seamless switching between Linux desktop machines",
    no_args_is_help=True,
)

console = Console()


@app.command()
def sync(
    target: Annotated[str, typer.Argument(help="Target hostname to sync to")],
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to config file (default: ~/.config/pc-switcher/config.yaml)",
        ),
    ] = None,
) -> None:
    """Sync to target machine.

    Loads configuration, creates orchestrator, and runs the complete sync workflow.
    """
    # Determine config path
    config_path = config or Configuration.get_default_config_path()

    # Load configuration
    try:
        cfg = Configuration.from_yaml(config_path)
    except ConfigurationError as e:
        console.print("[bold red]Configuration error:[/bold red]")
        for error in e.errors:
            if error.job:
                console.print(f"  [yellow]{error.job}[/yellow].{error.path}: {error.message}")
            else:
                console.print(f"  {error.path}: {error.message}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[bold red]Error loading configuration:[/bold red] {e}")
        sys.exit(1)

    # Run the sync operation
    exit_code = _run_sync(target, cfg)
    sys.exit(exit_code)


def _run_sync(target: str, cfg: Configuration) -> int:
    """Run the sync operation with asyncio and graceful interrupt handling.

    Args:
        target: Target hostname
        cfg: Loaded configuration

    Returns:
        Exit code: 0=success, 1=error, 130=SIGINT
    """
    return asyncio.run(_async_run_sync(target, cfg))


async def _async_run_sync(target: str, cfg: Configuration) -> int:
    """Async implementation of sync with interrupt handling.

    Interrupt behavior:
    - First SIGINT: Cancel sync task, allow CLEANUP_TIMEOUT_SECONDS for cleanup
    - Second SIGINT or timeout: Force terminate immediately
    """
    from pcswitcher.models import SyncSession

    loop = asyncio.get_running_loop()
    main_task: asyncio.Task[SyncSession] | None = None
    sigint_count = [0]  # Use list to allow mutation in nested function

    def sigint_handler() -> None:
        sigint_count[0] += 1

        if sigint_count[0] == 1:
            console.print("\n[yellow]Interrupt received, cleaning up...[/yellow]")
            console.print(
                f"[dim](Press Ctrl+C again to force quit, "
                f"or wait up to {CLEANUP_TIMEOUT_SECONDS}s for graceful cleanup)[/dim]"
            )
            if main_task is not None and not main_task.done():
                main_task.cancel()
        else:
            console.print("\n[red]Force terminating![/red]")
            # Cancel all tasks immediately
            for task in asyncio.all_tasks(loop):
                task.cancel()

    # Install SIGINT handler
    loop.add_signal_handler(signal.SIGINT, sigint_handler)

    try:
        orchestrator = Orchestrator(target=target, config=cfg)
        main_task = asyncio.create_task(orchestrator.run())

        try:
            await main_task
            return 0

        except asyncio.CancelledError:
            # First cancellation - wait for cleanup with timeout
            if sigint_count[0] == 1:
                try:
                    # Give orchestrator time to clean up (it has a finally block)
                    await asyncio.wait_for(
                        asyncio.shield(asyncio.sleep(0)),  # Allow pending cleanup
                        timeout=CLEANUP_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    console.print(
                        f"[red]Cleanup timeout ({CLEANUP_TIMEOUT_SECONDS}s) exceeded, "
                        "forcing termination[/red]"
                    )

            console.print("[yellow]Sync interrupted by user[/yellow]")
            return 130

    except Exception as e:
        console.print(f"\n[bold red]Sync failed:[/bold red] {e}")
        return 1

    finally:
        loop.remove_signal_handler(signal.SIGINT)


@app.command()
def logs(
    last: Annotated[
        bool,
        typer.Option("--last", "-l", help="Show path to most recent log file"),
    ] = False,
) -> None:
    """View log files.

    By default, shows the logs directory. Use --last to get the most recent log file path.
    """
    if last:
        log_file = get_latest_log_file()
        if log_file is None:
            console.print("[yellow]No log files found[/yellow]")
            console.print(f"Logs directory: {get_logs_directory()}")
            sys.exit(1)
        else:
            console.print(f"Latest log file: {log_file}")
    else:
        logs_dir = get_logs_directory()
        console.print(f"Logs directory: {logs_dir}")

        # List available log files
        if logs_dir.exists():
            log_files = sorted(logs_dir.glob("sync-*.log"), reverse=True)
            if log_files:
                console.print(f"\nFound {len(log_files)} log file(s):")
                for log_file in log_files[:10]:  # Show up to 10 most recent
                    console.print(f"  {log_file.name}")
                if len(log_files) > 10:
                    console.print(f"  ... and {len(log_files) - 10} more")
            else:
                console.print("\n[yellow]No log files found[/yellow]")
        else:
            console.print("\n[yellow]Logs directory does not exist yet[/yellow]")


@app.command()
def cleanup_snapshots(
    older_than: Annotated[
        str | None,
        typer.Option(
            "--older-than",
            help='Delete snapshots older than duration (e.g., "7d", "2w", "1m")',
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be deleted without deleting"),
    ] = False,
) -> None:
    """Clean up old snapshots.

    Uses --older-than to specify age threshold, or uses config default if not specified.
    Use --dry-run to preview what would be deleted.
    """
    if older_than is None:
        console.print(
            "[bold red]Error:[/bold red] --older-than is required\n"
            "Example: pc-switcher cleanup-snapshots --older-than 7d"
        )
        sys.exit(1)

    # Parse duration
    try:
        days = parse_older_than(older_than)
    except ValueError as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)

    # For now, just show what would be done
    # Actual implementation depends on orchestrator and executor infrastructure
    if dry_run:
        console.print(f"[yellow]DRY RUN:[/yellow] Would delete snapshots older than {days} days")
    else:
        console.print(
            f"[yellow]Not yet implemented:[/yellow] "
            f"Would delete snapshots older than {days} days\n"
            "This command requires the orchestrator infrastructure (Phase 13)."
        )


if __name__ == "__main__":
    app()
