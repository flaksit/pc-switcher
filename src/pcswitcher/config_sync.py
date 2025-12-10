"""Configuration sync between source and target machines."""

from __future__ import annotations

import difflib
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax

if TYPE_CHECKING:
    from pcswitcher.executor import RemoteExecutor
    from pcswitcher.ui import TerminalUI

__all__ = ["ConfigSyncAction", "sync_config_to_target"]


class ConfigSyncAction(Enum):
    """User's choice for config sync when configs differ."""

    ACCEPT_SOURCE = "accept_source"
    KEEP_TARGET = "keep_target"
    ABORT = "abort"


async def _get_target_config(target: RemoteExecutor) -> str | None:
    """Fetch config file content from target machine.

    Returns:
        Config file content as string, or None if file doesn't exist.
    """
    # Use ~ expansion on remote
    remote_path = "~/.config/pc-switcher/config.yaml"

    result = await target.run_command(f"cat {remote_path} 2>/dev/null")
    if result.success and result.stdout.strip():
        return result.stdout
    return None


def _generate_diff(source_content: str, target_content: str) -> str:
    """Generate a unified diff between source and target configs.

    Args:
        source_content: Source config file content
        target_content: Target config file content

    Returns:
        Unified diff string with color-friendly markers
    """
    source_lines = source_content.splitlines(keepends=True)
    target_lines = target_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        target_lines,
        source_lines,
        fromfile="target config",
        tofile="source config",
        lineterm="",
    )
    return "".join(diff)


def _prompt_new_config(console: Console, source_content: str) -> bool:
    """Prompt user to accept new config for target.

    Args:
        console: Rich console for display
        source_content: Source config content to display

    Returns:
        True if user accepts, False if user declines
    """
    console.print()
    console.print(
        Panel(
            "[yellow]Target has no configuration file.[/yellow]\n"
            "The following configuration from source will be applied:",
            title="Config Sync",
            border_style="yellow",
        )
    )
    console.print()

    # Display config with syntax highlighting
    syntax = Syntax(source_content, "yaml", theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title="Source Configuration", border_style="blue"))
    console.print()

    # Prompt for confirmation
    response = Prompt.ask(
        "[bold]Apply this config to target?[/bold]",
        choices=["y", "n"],
        default="n",
    )
    return response.lower() == "y"


def _prompt_config_diff(console: Console, source_content: str, target_content: str, diff: str) -> ConfigSyncAction:
    """Prompt user to choose action when configs differ.

    Args:
        console: Rich console for display
        source_content: Source config content
        target_content: Target config content
        diff: Unified diff between configs

    Returns:
        User's chosen action
    """
    console.print()
    console.print(
        Panel(
            "[yellow]Target configuration differs from source.[/yellow]\nReview the differences below:",
            title="Config Sync",
            border_style="yellow",
        )
    )
    console.print()

    # Display diff with syntax highlighting
    syntax = Syntax(diff, "diff", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, title="Configuration Diff", border_style="blue"))
    console.print()

    # Display options
    console.print("[bold]Choose an action:[/bold]")
    console.print("  [cyan]a[/cyan] - Accept config from source (overwrite target)")
    console.print("  [cyan]k[/cyan] - Keep current config on target")
    console.print("  [cyan]x[/cyan] - Abort sync")
    console.print()

    response = Prompt.ask(
        "[bold]Your choice[/bold]",
        choices=["a", "k", "x"],
        default="x",
    )

    if response == "a":
        return ConfigSyncAction.ACCEPT_SOURCE
    elif response == "k":
        return ConfigSyncAction.KEEP_TARGET
    else:
        return ConfigSyncAction.ABORT


async def sync_config_to_target(
    target: RemoteExecutor,
    source_config_path: Path,
    ui: TerminalUI | None,
    console: Console,
) -> bool:
    """Sync configuration from source to target machine.

    This function handles three scenarios:
    1. Target has no config: Display source config, prompt for confirmation
    2. Target config differs: Display diff, offer three choices
    3. Target config matches: Skip silently

    Args:
        target: RemoteExecutor for target machine
        source_config_path: Path to source config file
        ui: TerminalUI instance (will be paused during prompts)
        console: Rich console for display

    Returns:
        True if sync should continue, False if sync should abort

    Raises:
        RuntimeError: If config sync fails due to file operations
    """
    # Read source config
    if not source_config_path.exists():
        raise RuntimeError(f"Source config not found: {source_config_path}")

    source_content = source_config_path.read_text()

    # Fetch target config
    target_content = await _get_target_config(target)

    # Pause UI for user interaction
    if ui is not None:
        ui.stop()

    try:
        if target_content is None:
            # Scenario 1: No config on target
            if _prompt_new_config(console, source_content):
                await _copy_config_to_target(target, source_config_path)
                console.print("[green]Configuration copied to target.[/green]")
                return True
            else:
                console.print("[red]Sync aborted: configuration required on target.[/red]")
                return False

        elif source_content.strip() == target_content.strip():
            # Scenario 3: Configs match
            console.print("[dim]Target config matches source, skipping config sync.[/dim]")
            return True

        else:
            # Scenario 2: Configs differ
            diff = _generate_diff(source_content, target_content)
            action = _prompt_config_diff(console, source_content, target_content, diff)

            if action == ConfigSyncAction.ACCEPT_SOURCE:
                await _copy_config_to_target(target, source_config_path)
                console.print("[green]Configuration copied to target.[/green]")
                return True
            elif action == ConfigSyncAction.KEEP_TARGET:
                console.print("[yellow]Keeping existing target configuration.[/yellow]")
                return True
            else:  # ABORT
                console.print("[red]Sync aborted by user.[/red]")
                return False

    finally:
        # Resume UI
        if ui is not None:
            ui.start()


async def _copy_config_to_target(target: RemoteExecutor, source_path: Path) -> None:
    """Copy config file from source to target.

    Args:
        target: RemoteExecutor for target machine
        source_path: Local path to source config file

    Raises:
        RuntimeError: If copy fails
    """
    remote_dir = "~/.config/pc-switcher"

    # Ensure directory exists on target
    result = await target.run_command(f"mkdir -p {remote_dir}")
    if not result.success:
        raise RuntimeError(f"Failed to create config directory on target: {result.stderr}")

    # Copy file via SFTP
    # RemoteExecutor.send_file expects absolute remote path, so expand ~
    result = await target.run_command("echo $HOME")
    if not result.success:
        raise RuntimeError("Failed to get home directory on target")
    home_dir = result.stdout.strip()

    absolute_remote_path = f"{home_dir}/.config/pc-switcher/config.yaml"
    await target.send_file(source_path, absolute_remote_path)
