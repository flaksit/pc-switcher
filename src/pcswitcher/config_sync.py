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

# Single source of truth for the remote pc-switcher config directory and file path.
# folder_sync derives its tool-state filter token from CONFIG_REMOTE_DIR rather than
# hardcoding a second copy of the literal (CR-01 empty-prefix tool-state filter).
# CONFIG_REMOTE_PATH stays pointing at config.yaml specifically — package_state.py's
# decision-file relpath and other single-file callers derive from it — even though
# `sync_config_to_target` itself now iterates SYNCED_CONFIG_FILENAMES below.
CONFIG_REMOTE_DIR: str = "~/.config/pc-switcher"
CONFIG_REMOTE_PATH: str = f"{CONFIG_REMOTE_DIR}/config.yaml"

# Every filename config sync carries from the source's `~/.config/pc-switcher/` to the
# target's (D-23). `config.yaml` is REQUIRED on the source — a first sync needs it, and
# a missing one is a RuntimeError exactly as before this tuple existed. Every other name
# is OPTIONAL: absent on the source is skipped silently, not an error, since a user who
# has never authored an install snippet simply has no registry file yet.
#
# Do NOT add a `*.decisions.yaml` file here. Those are machine-local (D-09) and must
# NEVER travel between machines — see `pcswitcher.jobs.package_state.DecisionFile`. This
# tuple is the one thing config sync consults to decide what crosses the wire, so a
# decision file added here would silently start being synced.
SYNCED_CONFIG_FILENAMES: tuple[str, ...] = ("config.yaml", "package-snippets.yaml")

__all__ = [
    "CONFIG_REMOTE_DIR",
    "CONFIG_REMOTE_PATH",
    "SYNCED_CONFIG_FILENAMES",
    "ConfigSyncAction",
    "sync_config_to_target",
]


class ConfigSyncAction(Enum):
    """User's choice for config sync when configs differ."""

    ACCEPT_SOURCE = "accept_source"
    KEEP_TARGET = "keep_target"
    ABORT = "abort"


def _remote_path(filename: str) -> str:
    """The remote, home-relative path for one of `SYNCED_CONFIG_FILENAMES`."""
    return f"{CONFIG_REMOTE_DIR}/{filename}"


async def _get_target_config(target: RemoteExecutor, filename: str) -> str | None:
    """Fetch one synced config file's content from the target machine.

    Args:
        target: RemoteExecutor for target machine
        filename: One of `SYNCED_CONFIG_FILENAMES`

    Returns:
        File content as string, or None if it doesn't exist on the target.
    """
    result = await target.run_command(f"cat {_remote_path(filename)} 2>/dev/null")
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


def _prompt_new_config(console: Console, filename: str, source_content: str) -> bool:
    """Prompt user to accept a new file for the target.

    Args:
        console: Rich console for display
        filename: One of `SYNCED_CONFIG_FILENAMES`, named in the panel title so a user
            answering more than one prompt in a row knows which file they are answering.
        source_content: Source file content to display

    Returns:
        True if user accepts, False if user declines
    """
    console.print()
    console.print(
        Panel(
            f"[yellow]Target has no {filename}.[/yellow]\nThe following content from source will be applied:",
            title=f"Config Sync: {filename}",
            border_style="yellow",
        )
    )
    console.print()

    # Display config with syntax highlighting
    syntax = Syntax(source_content, "yaml", theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title=f"Source {filename}", border_style="blue"))
    console.print()

    # Prompt for confirmation. Spell out that declining aborts the whole sync —
    # a first sync needs the config applied, so "n" is not "skip config and
    # continue" but "abort". The bare y/n default hid this (a footgun).
    console.print(f"[bold]Apply this {filename} to the target?[/bold]")
    console.print("  [cyan]y[/cyan] - Apply and continue the sync")
    console.print("  [cyan]n[/cyan] - Abort the sync (nothing is transferred)")
    console.print()
    response = Prompt.ask("Choice", choices=["y", "n"], default="n")
    return response.lower() == "y"


def _display_config_diff(console: Console, filename: str, diff: str) -> None:
    """Print the config-differs warning panel and the diff itself.

    Shared by `_prompt_config_diff` (interactive) and the dry-run preview path
    (read-only, no action prompt), so the diff rendering isn't duplicated.

    Args:
        console: Rich console for display
        filename: One of `SYNCED_CONFIG_FILENAMES`, named in the panel title.
        diff: Unified diff between configs
    """
    console.print()
    console.print(
        Panel(
            f"[yellow]Target {filename} differs from source.[/yellow]\nReview the differences below:",
            title=f"Config Sync: {filename}",
            border_style="yellow",
        )
    )
    console.print()

    # Display diff with syntax highlighting
    syntax = Syntax(diff, "diff", theme="monokai", line_numbers=False)
    console.print(Panel(syntax, title=f"{filename} Diff", border_style="blue"))
    console.print()


def _prompt_config_diff(console: Console, filename: str, diff: str) -> ConfigSyncAction:
    """Prompt user to choose action when a synced file differs.

    Args:
        console: Rich console for display
        filename: One of `SYNCED_CONFIG_FILENAMES`, named in the panel title.
        diff: Unified diff between configs

    Returns:
        User's chosen action
    """
    _display_config_diff(console, filename, diff)

    # Display options
    console.print("[bold]Choose an action:[/bold]")
    console.print("  [cyan]a[/cyan] - Accept source (overwrite target)")
    console.print("  [cyan]k[/cyan] - Keep current target content")
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
    filename: str,
    source_path: Path,
    source_content: str,
    target_content: str | None,
    console: Console,
    auto_accept: bool,
    dry_run: bool,
) -> bool:
    """Handle one synced file's sync logic based on target state.

    Returns True if sync should continue, False if aborted.
    """
    # Scenario 1: No such file on target
    if target_content is None:
        return await _handle_no_target_config(
            target, filename, source_path, source_content, console, auto_accept, dry_run
        )

    # Scenario 2: Content matches
    if source_content.strip() == target_content.strip():
        console.print(f"[dim]Target {filename} matches source, skipping.[/dim]")
        return True

    # Scenario 3: Content differs
    return await _handle_config_diff(
        target, filename, source_path, source_content, target_content, console, auto_accept, dry_run
    )


async def _handle_no_target_config(
    target: RemoteExecutor,
    filename: str,
    source_path: Path,
    source_content: str,
    console: Console,
    auto_accept: bool,
    dry_run: bool,
) -> bool:
    """Handle the case when the target has no copy of this file yet."""
    if dry_run:
        # ADR-014: a rehearsal never prompts; log the preview and proceed.
        console.print(f"[dim][dry-run] Target has no {filename}; source would be applied (no changes made).[/dim]")
        return True

    if auto_accept or _prompt_new_config(console, filename, source_content):
        await _copy_config_to_target(target, filename, source_path)
        console.print(f"[green]{filename} copied to target.[/green]")
        return True
    # Decline silently: _sync_config_to_target raises SyncAbortedByUser and the
    # single CLI `except SyncAbortedByUser` handler prints the one abort line
    # (01-16 single-message decline contract). Printing here would duplicate it.
    return False


async def _handle_config_diff(
    target: RemoteExecutor,
    filename: str,
    source_path: Path,
    source_content: str,
    target_content: str,
    console: Console,
    auto_accept: bool,
    dry_run: bool,
) -> bool:
    """Handle the case when this file's content differs between source and target."""
    if auto_accept:
        if dry_run:
            console.print(f"[dim]{filename} would be copied to target (auto-accepted).[/dim]")
        else:
            await _copy_config_to_target(target, filename, source_path)
            console.print(f"[green]{filename} copied to target (auto-accepted).[/green]")
        return True

    diff = _generate_diff(source_content, target_content)

    if dry_run:
        # ADR-014: a rehearsal never prompts; show the diff as a read-only preview.
        _display_config_diff(console, filename, diff)
        console.print(f"[dim][dry-run] {filename} differs; source would be applied (no changes made).[/dim]")
        return True

    action = _prompt_config_diff(console, filename, diff)

    if action == ConfigSyncAction.ACCEPT_SOURCE:
        await _copy_config_to_target(target, filename, source_path)
        console.print(f"[green]{filename} copied to target.[/green]")
        return True
    if action == ConfigSyncAction.KEEP_TARGET:
        console.print(f"[yellow]Keeping existing target {filename}.[/yellow]")
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
    auto_accept: bool = False,
    dry_run: bool = False,
) -> bool:
    """Sync every file in `SYNCED_CONFIG_FILENAMES` from source to target machine.

    Each file goes through the same three scenarios `config.yaml` alone used to:
    1. No such file on target: display source content, prompt for confirmation
    2. Content differs: display diff, offer three choices
    3. Content matches: skip silently

    `config.yaml` is required on the source (a first sync needs it); every other file
    (currently just the install-snippet registry, D-23) is optional — absent on the
    source is skipped entirely, without even a target round trip.

    Args:
        target: RemoteExecutor for target machine
        source_config_path: Path to the source's config.yaml; its parent directory is
            where every other synced file (e.g. the snippet registry) is expected too.
        ui: TerminalUI instance (will be paused during prompts)
        console: Rich console for display
        auto_accept: If True, auto-accept source content without prompting
        dry_run: If True, show diff previews without copying anything

    Returns:
        True if sync should continue, False if sync should abort (any file's abort
        choice aborts the whole sync, matching the pre-existing single-file behavior).

    Raises:
        RuntimeError: If the source config.yaml is missing, or a copy fails.
    """
    source_dir = source_config_path.parent

    # Gather every file this run will actually consider, up front: required for the
    # single aggregate pause decision below, and because an optional file absent on the
    # source is skipped before ever touching the target.
    pending: list[tuple[str, Path, str]] = []
    for filename in SYNCED_CONFIG_FILENAMES:
        file_source_path = source_dir / filename
        if not file_source_path.exists():
            if filename == "config.yaml":
                raise RuntimeError(f"Source config not found: {file_source_path}")
            continue
        pending.append((filename, file_source_path, file_source_path.read_text()))

    target_contents: dict[str, str | None] = {}
    for filename, _file_source_path, _source_content in pending:
        target_contents[filename] = await _get_target_config(target, filename)

    # Pause the Live display at most ONCE for the whole loop, only if at least one file
    # will actually prompt — never once per file. Pausing/resuming between two prompts
    # would leave the stale-panel artefact this single aggregate pause exists to prevent
    # (mirrors the single-file precedent this function used to implement directly).
    def _will_prompt(filename: str, source_content: str) -> bool:
        target_content = target_contents[filename]
        configs_match = target_content is not None and source_content.strip() == target_content.strip()
        return not configs_match

    should_pause = (
        ui is not None
        and not auto_accept
        and not dry_run
        and any(_will_prompt(filename, source_content) for filename, _, source_content in pending)
    )
    if should_pause:
        assert ui is not None
        ui.pause()

    try:
        for filename, file_source_path, source_content in pending:
            should_continue = await _handle_config_sync(
                target,
                filename,
                file_source_path,
                source_content,
                target_contents[filename],
                console,
                auto_accept,
                dry_run,
            )
            if not should_continue:
                return False
        return True
    finally:
        if should_pause:
            assert ui is not None
            ui.resume()


async def _copy_config_to_target(target: RemoteExecutor, filename: str, source_path: Path) -> None:
    """Copy one synced file from source to target.

    Args:
        target: RemoteExecutor for target machine
        filename: One of `SYNCED_CONFIG_FILENAMES`
        source_path: Local path to the source file

    Raises:
        RuntimeError: If copy fails
    """
    # Ensure directory exists on target
    result = await target.run_command(f"mkdir -p {CONFIG_REMOTE_DIR}")
    if not result.success:
        raise RuntimeError(f"Failed to create config directory on target: {result.stderr}")

    # Copy file via SFTP
    # RemoteExecutor.send_file expects absolute remote path, so expand ~
    result = await target.run_command("echo $HOME")
    if not result.success:
        raise RuntimeError("Failed to get home directory on target")
    home_dir = result.stdout.strip()

    # Derive the absolute path from _remote_path(filename) by expanding the ~ prefix
    remote_relpath = _remote_path(filename).removeprefix("~/")
    absolute_remote_path = f"{home_dir}/{remote_relpath}"
    await target.send_file(source_path, absolute_remote_path)
