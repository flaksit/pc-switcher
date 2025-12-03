"""CLI entry point for pc-switcher using Typer."""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from importlib.metadata import PackageNotFoundError
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.text import Text

from pcswitcher.btrfs_snapshots import parse_older_than, run_snapshot_cleanup
from pcswitcher.config import Configuration, ConfigurationError
from pcswitcher.logger import get_latest_log_file, get_logs_directory
from pcswitcher.orchestrator import Orchestrator
from pcswitcher.version import get_this_version

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


def _version_callback(value: bool) -> None:
    """Print version and exit if --version flag is provided."""
    if value:
        try:
            pkg_version = get_this_version()
            # If you change this format, also update the parsing in version.py:parse_version_from_cli_output()
            console.print(f"pc-switcher {pkg_version}")
        except PackageNotFoundError:
            console.print("[bold red]Error:[/bold red] Cannot determine pc-switcher version")
            sys.exit(1)
        raise typer.Exit()


@app.callback()
def main(
    version_flag: Annotated[
        bool,
        typer.Option("--version", "-v", callback=_version_callback, is_eager=True, help="Show version and exit"),
    ] = False,
) -> None:
    """PC-switcher synchronization system."""


def _display_log_file(log_file: Path) -> None:
    """Display log file content with Rich formatting.

    Args:
        log_file: Path to log file to display
    """
    # Color mapping for log levels (matches ConsoleLogger.LEVEL_COLORS)
    level_colors = {
        "DEBUG": "dim",
        "FULL": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold red",
    }

    console.print(f"\n[bold]Log file:[/bold] {log_file}\n")

    try:
        with log_file.open("r", encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line:
                    continue

                try:
                    # Parse JSON log entry
                    entry = json.loads(line)

                    # Extract fields
                    timestamp = entry.get("timestamp", "")
                    level = entry.get("level", "INFO")
                    job = entry.get("job", "")
                    host = entry.get("host", "")
                    message = entry.get("event", "")

                    # Get color for level
                    color = level_colors.get(level, "white")

                    # Format timestamp (just time portion if full ISO format)
                    time_part = (
                        timestamp.split("T")[1].split(".")[0] if "T" in timestamp else timestamp
                    )

                    # Build formatted output
                    text = Text()
                    text.append(f"{time_part} ", style="dim")
                    text.append(f"[{level:8}]", style=color)
                    text.append(f" [{job}]", style="blue")
                    text.append(f" ({host})", style="magenta")
                    text.append(f" {message}")

                    # Add context (all other fields besides the standard ones)
                    context_fields = {
                        k: v
                        for k, v in entry.items()
                        if k not in {"timestamp", "level", "job", "host", "event"}
                    }
                    if context_fields:
                        ctx_str = " ".join(f"{k}={v}" for k, v in context_fields.items())
                        text.append(f" {ctx_str}", style="dim")

                    console.print(text)

                except json.JSONDecodeError:
                    # Handle malformed lines gracefully
                    console.print(f"[dim]Line {line_num}:[/dim] {line}")

    except OSError as e:
        console.print(f"[bold red]Error reading log file:[/bold red] {e}")
        sys.exit(1)


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
    from pcswitcher.models import SyncSession  # noqa: PLC0415

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
            # Cancel all tasks immediately (no cleanup, force quit)
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
        typer.Option("--last", "-l", help="Display the most recent log file"),
    ] = False,
) -> None:
    """View log files.

    By default, shows the logs directory. Use --last to display the most recent log file.
    """
    if last:
        log_file = get_latest_log_file()
        if log_file is None:
            console.print("[yellow]No log files found[/yellow]")
            console.print(f"Logs directory: {get_logs_directory()}")
            sys.exit(1)
        else:
            _display_log_file(log_file)
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
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            help="Path to config file (default: ~/.config/pc-switcher/config.yaml)",
        ),
    ] = None,
) -> None:
    """Clean up old snapshots on the local machine.

    Uses --older-than to specify age threshold, or uses config default if not specified.
    Use --dry-run to preview what would be deleted.
    """
    # Load configuration
    config_path = config or Configuration.get_default_config_path()
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

    # Parse duration (use config default if not specified)
    if older_than is not None:
        try:
            max_age_days = parse_older_than(older_than)
        except ValueError as e:
            console.print(f"[bold red]Error:[/bold red] {e}")
            sys.exit(1)
    else:
        max_age_days = cfg.btrfs_snapshots.max_age_days

    # Run cleanup
    exit_code = run_snapshot_cleanup(
        cfg.btrfs_snapshots.keep_recent,
        max_age_days,
        dry_run,
        console.print,
    )
    sys.exit(exit_code)


@app.command()
def init(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing configuration file"),
    ] = False,
) -> None:
    """Initialize default configuration file.

    Creates ~/.config/pc-switcher/config.yaml with default settings.
    Use --force to overwrite an existing configuration.
    """
    config_path = Configuration.get_default_config_path()

    if config_path.exists() and not force:
        console.print(f"[yellow]Configuration file already exists:[/yellow] {config_path}")
        console.print("Use --force to overwrite")
        raise typer.Exit(1)

    # Create parent directory if needed
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Read default config from package resources
    default_config = files("pcswitcher").joinpath("default-config.yaml").read_text()

    # Write to config file
    config_path.write_text(default_config)

    console.print(f"[green]Created configuration file:[/green] {config_path}")
    console.print("\n[dim]Please review and customize the configuration, especially:[/dim]")
    console.print("[dim]  - btrfs_snapshots.subvolumes (must match your system)[/dim]")


if __name__ == "__main__":
    app()
