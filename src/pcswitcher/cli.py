"""CLI entry point for pc-switcher using Typer."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import subprocess
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
from pcswitcher.models import SyncSession
from pcswitcher.orchestrator import Orchestrator
from pcswitcher.version import Release, Version, find_one_version, get_highest_release, get_this_version

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

# Create "self" subcommand group for self-management commands
self_app = typer.Typer(
    name="self",
    help="Manage the pc-switcher installation itself",
    no_args_is_help=True,
)
app.add_typer(self_app, name="self")

console = Console()


def _load_configuration(config_path: Path) -> Configuration:
    """Load configuration with helpful error messages.

    Args:
        config_path: Path to config file

    Returns:
        Loaded Configuration

    Raises:
        typer.Exit: On configuration error
    """
    try:
        return Configuration.from_yaml(config_path)
    except ConfigurationError as e:
        console.print("[bold red]Configuration error:[/bold red]")

        # Check if this is a missing config file error
        config_missing = any("not found" in error.message.lower() for error in e.errors)
        if config_missing:
            console.print(f"  {config_path}: Configuration file not found")
            console.print("\nTo initialize configuration, run:")
            console.print("  [cyan]pc-switcher init[/cyan]")
        else:
            for error in e.errors:
                if error.job:
                    console.print(f"  [yellow]{error.job}[/yellow].{error.path}: {error.message}")
                else:
                    console.print(f"  {error.path}: {error.message}")

        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[bold red]Error loading configuration:[/bold red] {e}")
        raise typer.Exit(1) from e


def _version_callback(value: bool) -> None:
    """Print version and exit if --version flag is provided."""
    if value:
        try:
            version = get_this_version()
            # If you change this format, also update version.py:find_one_version()
            # Display in SemVer format for user-facing output
            console.print(f"pc-switcher {version.semver_str()}")
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
    # Configure logging: WARNING for third-party libs, DEBUG for pcswitcher
    logging.basicConfig(
        level=logging.WARNING,
        format="%(name)s: %(message)s",
    )
    logging.getLogger("pcswitcher").setLevel(logging.DEBUG)


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
                    time_part = timestamp.split("T")[1].split(".")[0] if "T" in timestamp else timestamp

                    # Build formatted output
                    text = Text()
                    text.append(f"{time_part} ", style="dim")
                    text.append(f"[{level:8}]", style=color)
                    text.append(f" [{job}]", style="blue")
                    text.append(f" ({host})", style="magenta")
                    text.append(f" {message}")

                    # Add context (all other fields besides the standard ones)
                    context_fields = {
                        k: v for k, v in entry.items() if k not in {"timestamp", "level", "job", "host", "event"}
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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview sync without making changes",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Auto-accept prompts (e.g., config sync confirmation)",
        ),
    ] = False,
    allow_consecutive: Annotated[
        bool,
        typer.Option(
            "--allow-consecutive",
            help="Skip warning about consecutive syncs without receiving a sync back first",
        ),
    ] = False,
) -> None:
    """Sync to target machine.

    Loads configuration, creates orchestrator, and runs the complete sync workflow.
    """
    # Determine config path
    config_path = config or Configuration.get_default_config_path()

    # Load configuration
    cfg = _load_configuration(config_path)

    # Run the sync operation
    exit_code = _run_sync(target, cfg, auto_accept=yes, allow_consecutive=allow_consecutive, dry_run=dry_run)
    sys.exit(exit_code)


def _run_sync(
    target: str,
    cfg: Configuration,
    *,
    auto_accept: bool = False,
    allow_consecutive: bool = False,
    dry_run: bool = False,
) -> int:
    """Run the sync operation with asyncio and graceful interrupt handling.

    Args:
        target: Target hostname
        cfg: Loaded configuration
        auto_accept: If True, auto-accept prompts (e.g., config sync)
        allow_consecutive: If True, skip warning about consecutive syncs
        dry_run: If True, preview sync without making changes

    Returns:
        Exit code: 0=success, 1=error, 130=SIGINT
    """
    return asyncio.run(
        _async_run_sync(target, cfg, auto_accept=auto_accept, allow_consecutive=allow_consecutive, dry_run=dry_run)
    )


async def _async_run_sync(
    target: str,
    cfg: Configuration,
    *,
    auto_accept: bool = False,
    allow_consecutive: bool = False,
    dry_run: bool = False,
) -> int:
    """Async implementation of sync with interrupt handling.

    Args:
        target: Target hostname
        cfg: Loaded configuration
        auto_accept: If True, auto-accept prompts (e.g., config sync)
        dry_run: If True, preview sync without making changes

    Interrupt behavior:
    - First SIGINT: Cancel sync task, allow CLEANUP_TIMEOUT_SECONDS for cleanup
    - Second SIGINT or timeout: Force terminate immediately
    """
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
        orchestrator = Orchestrator(
            target=target, config=cfg, auto_accept=auto_accept, allow_consecutive=allow_consecutive, dry_run=dry_run
        )
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
                        f"[red]Cleanup timeout ({CLEANUP_TIMEOUT_SECONDS}s) exceeded, forcing termination[/red]"
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
    cfg = _load_configuration(config_path)

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


GITHUB_REPO_URL = "https://github.com/flaksit/pc-switcher"


def _run_uv_tool_install(release: Release) -> subprocess.CompletedProcess[str]:
    """Run uv tool install to install/upgrade pc-switcher.

    Args:
        release: Release to install (tag will be used for git URL)

    Returns:
        CompletedProcess with stdout/stderr
    """
    install_source = f"git+{GITHUB_REPO_URL}@{release.tag}"
    return subprocess.run(
        ["uv", "tool", "install", "--force", install_source],
        capture_output=True,
        text=True,
        check=False,
    )


def _verify_installed_version() -> Version | None:
    """Get the currently installed pc-switcher version.

    Returns:
        Version object if pc-switcher is installed and working, None otherwise
    """
    result = subprocess.run(
        ["pc-switcher", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        try:
            return find_one_version(result.stdout)
        except ValueError:
            return None
    return None


def _get_current_version_or_exit() -> Version:
    """Return the current pc-switcher version or exit on failure."""
    try:
        return get_this_version()
    except PackageNotFoundError:
        console.print("[bold red]Error:[/bold red] Cannot determine current pc-switcher version")
        sys.exit(1)


def _resolve_target_version(version: str | None, prerelease: bool) -> Release:
    """Resolve the target release to install based on CLI options."""
    if version is not None:
        try:
            parsed_version = Version.parse(version)
            release = parsed_version.get_release()
            if release is None:
                console.print(f"[bold red]Error:[/bold red] Version {version} is not a GitHub release")
                sys.exit(1)
            return release
        except ValueError:
            console.print(f"[bold red]Error:[/bold red] Invalid version format: {version}")
            sys.exit(1)

    console.print("[dim]Checking for latest version...[/dim]")
    try:
        return get_highest_release(include_prereleases=prerelease)
    except RuntimeError as e:
        if not prerelease and "No releases found" in str(e):
            console.print(
                "[bold red]Error:[/bold red] No stable releases found on GitHub. "
                "Use --prerelease to install a pre-release version."
            )
        else:
            console.print(f"[bold red]Error:[/bold red] {e}")
        sys.exit(1)


@self_app.command()
def update(
    version: Annotated[
        str | None,
        typer.Argument(help="Version to install (e.g., '0.4.0'). If not specified, installs latest stable release."),
    ] = None,
    prerelease: Annotated[
        bool,
        typer.Option("--prerelease", help="Include pre-release versions when finding latest."),
    ] = False,
) -> None:
    """Update pc-switcher to a specific version or latest.

    Downloads and installs the specified version from GitHub using uv tool.
    If no version is specified, fetches and installs the latest stable release.
    Use --prerelease to include alpha/beta/rc versions.
    """
    # Get current version
    current = _get_current_version_or_exit()
    target_release = _resolve_target_version(version, prerelease)

    # Check if update is needed
    # Use SemVer format for user-facing output
    current_display = current.semver_str()
    target_display = target_release.version.semver_str()

    if target_release.version == current:
        console.print(f"[green]Already at version {current_display}[/green]")
        sys.exit(0)

    if target_release.version < current:
        console.print(f"[yellow]Warning:[/yellow] Downgrading from {current_display} to {target_display}")

    # Perform the update
    console.print(f"Updating pc-switcher from {current_display} to {target_display}...")
    result = _run_uv_tool_install(target_release)

    if result.returncode != 0:
        console.print("[bold red]Error:[/bold red] Update failed")
        if result.stderr:
            console.print(f"[dim]{result.stderr.strip()}[/dim]")
        sys.exit(1)

    # Verify installation
    installed = _verify_installed_version()
    if installed is None:
        console.print("[bold red]Error:[/bold red] Verification failed - pc-switcher not working after update")
        sys.exit(1)

    if installed != target_release.version:
        console.print(
            f"[bold red]Error:[/bold red] Version mismatch after update. "
            f"Expected {target_display}, got {installed.semver_str()}"
        )
        sys.exit(1)

    console.print(f"[green]Successfully updated to version {target_display}[/green]")


if __name__ == "__main__":
    app()
