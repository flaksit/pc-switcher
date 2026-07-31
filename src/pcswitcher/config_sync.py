"""Configuration sync between the two machines.

Every line this module prints goes on screen at a question the user answers, so it names
both machines by hostname and never by the role this run gave them
(`PKG-FR-NAME-THE-MACHINES`). The hostnames are threaded in as two parameters, the same
pair `StepGate` takes, rather than re-derived here.
"""

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

# Single source of truth for the remote pc-switcher config directory and file path.
# folder_sync derives its tool-state filter token from CONFIG_REMOTE_DIR rather than
# hardcoding a second copy of the literal (CR-01 empty-prefix tool-state filter);
# packages/state.py derives its decision-file and snippet-registry relpaths from
# CONFIG_REMOTE_DIR the same way.
#
# config_sync carries exactly ONE file, config.yaml (D-23): it is the single required
# config a first sync needs. The shared install-snippet registry is NOT carried here —
# it travels by `manual_installs_sync`'s own post-review `send_file` push, because
# config_sync runs before any review (sync step 9) and so cannot carry a snippet the
# user authored during that review.
CONFIG_REMOTE_DIR: str = "~/.config/pc-switcher"
CONFIG_REMOTE_PATH: str = f"{CONFIG_REMOTE_DIR}/config.yaml"

__all__ = ["CONFIG_REMOTE_DIR", "CONFIG_REMOTE_PATH", "ConfigSyncAction", "sync_config_to_target"]


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
    result = await target.run_command(f"cat {CONFIG_REMOTE_PATH} 2>/dev/null")
    if result.success and result.stdout.strip():
        return result.stdout
    return None


def _generate_diff(source_content: str, target_content: str, source_hostname: str, target_hostname: str) -> str:
    """Generate a unified diff between the two machines' configs.

    Returns:
        Unified diff string with color-friendly markers
    """
    source_lines = source_content.splitlines(keepends=True)
    target_lines = target_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        target_lines,
        source_lines,
        fromfile=f"{target_hostname} config",
        tofile=f"{source_hostname} config",
        lineterm="",
    )
    return "".join(diff)


def _prompt_new_config(console: Console, source_content: str, source_hostname: str, target_hostname: str) -> bool:
    """Prompt the user to apply this machine's config to the machine being synced to.

    Returns:
        True if user accepts, False if user declines
    """
    console.print()
    console.print(
        Panel(
            f"[yellow]{target_hostname} has no configuration file.[/yellow]\n"
            f"This configuration from {source_hostname} will be applied:",
            title="Config Sync",
            border_style="yellow",
        )
    )
    console.print()

    # Display config with syntax highlighting
    syntax = Syntax(source_content, "yaml", theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title=f"Configuration on {source_hostname}", border_style="blue"))
    console.print()

    # Prompt for confirmation. Spell out that declining aborts the whole sync —
    # a first sync needs the config applied, so "n" is not "skip config and
    # continue" but "abort". The bare y/n default hid this (a footgun).
    console.print(f"[bold]Apply this config to {target_hostname}?[/bold]")
    console.print("  [cyan]y[/cyan] - Apply the config and continue the sync")
    console.print("  [cyan]n[/cyan] - Abort the sync (nothing is transferred)")
    console.print()
    response = Prompt.ask("Choice", choices=["y", "n"], default="n")
    return response.lower() == "y"


def _display_config_diff(console: Console, diff: str, source_hostname: str, target_hostname: str) -> None:
    """Print the config-differs warning panel and the diff itself.

    Shared by `_prompt_config_diff` (interactive) and the dry-run preview path
    (read-only, no action prompt), so the diff rendering isn't duplicated.

    Args:
        console: Rich console for display
        diff: Unified diff between configs
    """
    console.print()
    console.print(
        Panel(
            f"[yellow]{target_hostname}'s configuration differs from {source_hostname}'s.[/yellow]\n"
            "Review the differences below:",
            title="Config Sync",
            border_style="yellow",
        )
    )
    console.print()

    # Display diff with syntax highlighting
    syntax = Syntax(diff, "diff", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, title="Configuration Diff", border_style="blue"))
    console.print()


def _prompt_config_diff(console: Console, diff: str, source_hostname: str, target_hostname: str) -> ConfigSyncAction:
    """Prompt the user to choose an action when the two configs differ.

    Returns:
        User's chosen action
    """
    _display_config_diff(console, diff, source_hostname, target_hostname)

    # Display options
    console.print("[bold]Choose an action:[/bold]")
    console.print(f"  [cyan]a[/cyan] - Take {source_hostname}'s config (overwrites {target_hostname}'s)")
    console.print(f"  [cyan]k[/cyan] - Keep {target_hostname}'s current config")
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


async def _handle_config_sync(
    target: RemoteExecutor,
    source_config_path: Path,
    source_content: str,
    target_content: str | None,
    console: Console,
    auto_accept: bool,
    dry_run: bool,
    source_hostname: str,
    target_hostname: str,
) -> bool:
    """Handle config sync logic based on the other machine's state.

    Returns True if sync should continue, False if aborted.
    """
    # Scenario 1: no config there yet
    if target_content is None:
        return await _handle_no_target_config(
            target, source_config_path, source_content, console, auto_accept, dry_run, source_hostname, target_hostname
        )

    # Scenario 2: Configs match
    if source_content.strip() == target_content.strip():
        console.print(f"[dim]{target_hostname}'s config matches {source_hostname}'s, skipping config sync.[/dim]")
        return True

    # Scenario 3: Configs differ
    return await _handle_config_diff(
        target,
        source_config_path,
        source_content,
        target_content,
        console,
        auto_accept,
        dry_run,
        source_hostname,
        target_hostname,
    )


async def _handle_no_target_config(
    target: RemoteExecutor,
    source_config_path: Path,
    source_content: str,
    console: Console,
    auto_accept: bool,
    dry_run: bool,
    source_hostname: str,
    target_hostname: str,
) -> bool:
    """Handle the case where the machine being synced to has no config."""
    if dry_run:
        # ADR-014: a rehearsal never prompts; log the preview and proceed.
        console.print(
            f"[dim][dry-run] {target_hostname} has no config; {source_hostname}'s would be applied "
            "(no changes made).[/dim]"
        )
        return True

    if auto_accept or _prompt_new_config(console, source_content, source_hostname, target_hostname):
        await _copy_config_to_target(target, source_config_path, target_hostname)
        console.print(f"[green]Configuration copied to {target_hostname}.[/green]")
        return True
    # Decline silently: _sync_config_to_target raises SyncAbortedByUser and the
    # single CLI `except SyncAbortedByUser` handler prints the one abort line
    # (01-16 single-message decline contract). Printing here would duplicate it.
    return False


async def _handle_config_diff(
    target: RemoteExecutor,
    source_config_path: Path,
    source_content: str,
    target_content: str,
    console: Console,
    auto_accept: bool,
    dry_run: bool,
    source_hostname: str,
    target_hostname: str,
) -> bool:
    """Handle the case where the two machines' configs differ."""
    if auto_accept:
        if dry_run:
            console.print(f"[dim]Configuration would be copied to {target_hostname} (auto-accepted).[/dim]")
        else:
            await _copy_config_to_target(target, source_config_path, target_hostname)
            console.print(f"[green]Configuration copied to {target_hostname} (auto-accepted).[/green]")
        return True

    diff = _generate_diff(source_content, target_content, source_hostname, target_hostname)

    if dry_run:
        # ADR-014: a rehearsal never prompts; show the diff as a read-only preview.
        _display_config_diff(console, diff, source_hostname, target_hostname)
        console.print(f"[dim][dry-run] Configs differ; {source_hostname}'s would be applied (no changes made).[/dim]")
        return True

    action = _prompt_config_diff(console, diff, source_hostname, target_hostname)

    if action == ConfigSyncAction.ACCEPT_SOURCE:
        await _copy_config_to_target(target, source_config_path, target_hostname)
        console.print(f"[green]Configuration copied to {target_hostname}.[/green]")
        return True
    if action == ConfigSyncAction.KEEP_TARGET:
        console.print(f"[yellow]Keeping {target_hostname}'s existing configuration.[/yellow]")
        return True
    # ABORT: decline silently — the single CLI `except SyncAbortedByUser`
    # handler owns the one user-facing abort line (01-16 single-message
    # decline contract). Printing here would emit a second, conflicting line.
    return False


async def sync_config_to_target(
    target: RemoteExecutor,
    source_config_path: Path,
    ui: TerminalUI | None,
    console: Console,
    *,
    source_hostname: str,
    target_hostname: str,
    auto_accept: bool = False,
    dry_run: bool = False,
) -> bool:
    """Sync configuration from source to target machine.

    This function handles three scenarios:
    1. Target has no config: Display source config, prompt for confirmation
    2. Target config differs: Display diff, offer three choices
    3. Target config matches: Skip silently

    Carries exactly one file, the caller-supplied `source_config_path` (config.yaml). The
    install-snippet registry is NOT transferred here — `manual_installs_sync` pushes it
    itself after its review (D-23).

    Args:
        target: RemoteExecutor for target machine
        source_config_path: Path to source config file; read exactly as given, never
            re-derived from its parent directory (a caller whose config is named something
            other than `config.yaml` must have that file transferred, not a sibling).
        ui: TerminalUI instance (will be paused during prompts)
        console: Rich console for display
        source_hostname: This machine's hostname, named in every line the user reads
        target_hostname: The other machine's hostname, named in every line the user reads
        auto_accept: If True, auto-accept source config without prompting
        dry_run: If True, show diff preview without copying file

    Returns:
        True if sync should continue, False if sync should abort

    Raises:
        RuntimeError: If config sync fails due to file operations
    """
    # Read source config from the path the caller passed, whatever its name.
    if not source_config_path.exists():
        raise RuntimeError(f"No pc-switcher config on {source_hostname} at {source_config_path}")

    source_content = source_config_path.read_text()

    # Fetch target config
    target_content = await _get_target_config(target)

    # Pause the live display only when a prompt will actually be shown. Previously it
    # paused for any interactive sync, so a config that matches (the common case) still
    # stopped+restarted the single Live instance, leaving a stale "Recent Logs" panel on
    # every sync. A prompt fires only when the target has no config or the configs differ
    # (and we're interactive, non-dry-run) — mirror _handle_config_sync's decision here.
    configs_match = target_content is not None and source_content.strip() == target_content.strip()
    should_pause = ui is not None and not auto_accept and not dry_run and not configs_match
    if should_pause:
        assert ui is not None
        ui.pause()

    try:
        should_continue = await _handle_config_sync(
            target,
            source_config_path,
            source_content,
            target_content,
            console,
            auto_accept,
            dry_run,
            source_hostname,
            target_hostname,
        )
        return should_continue
    finally:
        # Resume UI (paired with whatever pause occurred above)
        if should_pause:
            assert ui is not None
            ui.resume()


async def _copy_config_to_target(target: RemoteExecutor, source_path: Path, target_hostname: str) -> None:
    """Copy this machine's config file to the machine being synced to.

    Raises:
        RuntimeError: If copy fails
    """
    # Ensure the directory exists there
    result = await target.run_command(
        f"mkdir --parents {CONFIG_REMOTE_DIR}", mutates="create the pc-switcher config directory"
    )
    if not result.success:
        raise RuntimeError(f"Failed to create the config directory on {target_hostname}: {result.stderr}")

    # Copy file via SFTP
    # RemoteExecutor.send_file expects absolute remote path, so expand ~
    result = await target.run_command("echo $HOME")
    if not result.success:
        raise RuntimeError(f"Failed to read the home directory on {target_hostname}")
    home_dir = result.stdout.strip()

    # Derive the absolute path from CONFIG_REMOTE_PATH by expanding the ~ prefix
    config_remote_relpath = CONFIG_REMOTE_PATH.removeprefix("~/")
    absolute_remote_path = f"{home_dir}/{config_remote_relpath}"
    await target.send_file(
        source_path,
        absolute_remote_path,
        mutates=f"overwrite the pc-switcher config at {CONFIG_REMOTE_PATH} with this machine's copy",
    )
