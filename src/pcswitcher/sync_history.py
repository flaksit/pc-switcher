"""Sync history tracking to detect consecutive syncs from the same source.

Tracks whether this machine's last role in a sync was SOURCE or TARGET.
Used to warn users when they try to sync from the same machine twice
without receiving a sync back first.

State file: ~/.local/share/pc-switcher/sync-history.json
Format: {"last_role": "source" | "target"}
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from enum import Enum
from pathlib import Path

__all__ = [
    "HISTORY_DIR",
    "HISTORY_PATH",
    "SyncRole",
    "get_history_path",
    "get_last_role",
    "get_record_role_command",
    "record_role",
]

# Shell-expandable paths for use in SSH commands (remote execution).
# For local Python operations, use get_history_path() which returns a Path object.
HISTORY_DIR = "~/.local/share/pc-switcher"
HISTORY_PATH = f"{HISTORY_DIR}/sync-history.json"


class SyncRole(Enum):
    """Role of this machine in a sync operation."""

    SOURCE = "source"
    TARGET = "target"


def get_history_path() -> Path:
    """Get the path to the sync history file.

    Returns:
        Path to ~/.local/share/pc-switcher/sync-history.json
    """
    return Path.home() / ".local" / "share" / "pc-switcher" / "sync-history.json"


def get_last_role() -> SyncRole | None:
    """Get the last sync role of this machine.

    Returns:
        SyncRole.SOURCE if last sync was as source,
        SyncRole.TARGET if last sync was as target,
        None if no history exists or history is corrupted.

    Note:
        Returns None for missing files, but the caller should treat
        corrupted files the same as SyncRole.SOURCE (safety-first approach).
        Use get_last_role_with_error() if you need to distinguish these cases.
    """
    role, _ = get_last_role_with_error()
    return role


def get_last_role_with_error() -> tuple[SyncRole | None, bool]:
    """Get the last sync role with error information.

    Returns:
        Tuple of (role, had_error):
        - (SyncRole.SOURCE/TARGET, False) if valid history found
        - (None, False) if no history file exists
        - (None, True) if history file exists but is corrupted/invalid
    """
    history_path = get_history_path()

    if not history_path.exists():
        return None, False

    try:
        content = history_path.read_text(encoding="utf-8")
        data = json.loads(content)

        if not isinstance(data, dict):
            return None, True

        last_role_str = data.get("last_role")
        if last_role_str == "source":
            return SyncRole.SOURCE, False
        elif last_role_str == "target":
            return SyncRole.TARGET, False
        else:
            return None, True

    except (json.JSONDecodeError, OSError, KeyError):
        return None, True


def record_role(role: SyncRole) -> None:
    """Record this machine's role in the most recent sync.

    Uses atomic write (temp file + rename) to prevent corruption.

    Args:
        role: The role this machine played (SOURCE or TARGET)
    """
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    data = {"last_role": role.value}
    content = json.dumps(data)

    # Atomic write: write to temp file in same directory, then rename
    # This ensures the file is either fully written or not changed
    fd, temp_path = tempfile.mkstemp(
        dir=history_path.parent,
        prefix=".sync-history-",
        suffix=".tmp",
    )
    try:
        os.write(fd, content.encode())
        os.close(fd)
        Path(temp_path).rename(history_path)
    except Exception:
        # Clean up temp file on failure
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            Path(temp_path).unlink()
        raise


def get_record_role_command(role: SyncRole) -> str:
    """Get the shell command to record a role on a remote machine.

    This returns a shell command that can be executed via SSH to update
    the sync history on a remote machine.

    Args:
        role: The role to record (SOURCE or TARGET)

    Returns:
        Shell command string that creates the directory and writes the history file.
    """
    data = json.dumps({"last_role": role.value})
    return f"mkdir -p {HISTORY_DIR} && echo '{data}' > {HISTORY_PATH}"
