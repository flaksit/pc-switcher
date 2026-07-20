"""Sync history tracking for the topology-based sync-safety model (ADR-015).

Tracks this machine's last sync role (SOURCE or TARGET) and the peer hostname
(the other machine involved in that sync).

State file: ~/.local/share/pc-switcher/sync-history.json
Format (backward-compatible; old files with only `last_role` still work):
    {
        "last_role": "source" | "target",
        "last_peer": "<hostname>",
        "timestamp": "<ISO-8601>"    # optional; not written by this module
    }

Every write to this file is merge-preserving — record_role reads existing
keys, updates only `last_role` and `last_peer`, and leaves any unrecognised
keys intact. Writes are atomic (temp file + rename) to prevent corruption.
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
    "get_last_role_with_error",
    "get_last_sync_state",
    "get_record_role_command",
    "hostnames_equal",
    "parse_sync_state",
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

    except json.JSONDecodeError, OSError, KeyError:
        return None, True


def record_role(role: SyncRole, peer: str | None = None) -> None:
    """Record this machine's role in the most recent sync.

    Merge-preserving: reads any existing data and keeps it intact, updating
    only `last_role` and (when provided) `last_peer`.

    Uses atomic write (temp file + rename) to prevent corruption.

    Args:
        role: The role this machine played (SOURCE or TARGET).
        peer: Hostname of the other machine in the sync, if known.
    """
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing data to preserve any other keys.
    # json.loads returns Any, so isinstance is needed to narrow the type.
    try:
        raw = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
        existing: dict[str, object] = raw if isinstance(raw, dict) else {}
    except json.JSONDecodeError, OSError:
        existing = {}

    data = {**existing, "last_role": role.value}
    if peer is not None:
        data["last_peer"] = peer
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


def get_record_role_command(role: SyncRole, peer: str | None = None) -> str:
    """Get the shell command to record a role on a remote machine.

    The returned command is merge-preserving: it reads any existing
    sync-history.json on the remote, updates `last_role` and (when provided)
    `last_peer`, preserves all other keys, and writes atomically via a
    temp-file rename.

    Args:
        role: The role to record (SOURCE or TARGET).
        peer: Hostname of the other machine in the sync, if known. Injected
            as a `repr()`-escaped Python string literal so it cannot break
            out of the script or the shell -c argument (hostnames are ASCII
            identifiers, but repr() handles any edge case defensively).

    Returns:
        Shell command string executable on the remote via SSH.
        Requires python3 on the remote (available on all Ubuntu 24.04 targets).
    """
    role_val = role.value
    # The script uses single-quoted Python string literals throughout so it can
    # be safely wrapped in double quotes for the shell -c argument.
    # Actual newline characters (not \n) separate statements so python3 receives
    # a valid multi-line script via -c.
    lines = [
        "import json,os,tempfile",
        "from pathlib import Path",
        "p=Path.home()/'.local/share/pc-switcher/sync-history.json'",
        "p.parent.mkdir(parents=True,exist_ok=True)",
        "d={}",
        "try:",
        "    tmp=json.loads(p.read_text()) if p.exists() else {}",
        "    if isinstance(tmp,dict):d=tmp",
        "except Exception:pass",
        f"d['last_role']='{role_val}'",
    ]
    if peer is not None:
        # repr() produces a valid Python string literal with proper escaping;
        # hostnames are plain ASCII so this will always yield a single-quoted literal.
        peer_lit = repr(peer)
        lines.append(f"d['last_peer']={peer_lit}")
    lines.extend(
        [
            "c=json.dumps(d)",
            "fd,t=tempfile.mkstemp(dir=p.parent,prefix='.sync-history-',suffix='.tmp')",
            "os.write(fd,c.encode());os.close(fd)",
            "Path(t).rename(p)",
        ]
    )
    script = "\n".join(lines)
    return f'mkdir -p {HISTORY_DIR} && python3 -c "{script}"'


def hostnames_equal(a: str | None, b: str | None) -> bool:
    """Compare two hostnames case-insensitively for the topology safety checks.

    DNS hostnames are case-insensitive, so `P17` and `p17` denote the same machine.
    History recorded before hostnames were acquired uniformly (or a target reached
    via a differently-cased SSH alias) can hold either casing; matching case-folded
    prevents a spurious "synced with a different machine" warning on a clean
    back-sync.

    A ``None`` peer never matches — including another ``None`` — because an absent
    peer is not evidence that the topology is clean.
    """
    if a is None or b is None:
        return False
    return a.casefold() == b.casefold()


def parse_sync_state(content: str) -> tuple[SyncRole | None, str | None]:
    """Parse a sync-history JSON string and return (role, peer).

    Used to interpret a remote machine's sync-history.json fetched over SSH
    without touching the local file. The source is untrusted (T-01-12-01):
    any malformed, non-dict, or invalid-role input is treated as (None, None)
    and never raises.

    Args:
        content: JSON string (e.g. the raw text of a remote sync-history.json).

    Returns:
        (SyncRole, peer_hostname) on valid input, (None, None) otherwise.
    """
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            return None, None
        last_role_str = data.get("last_role")
        if last_role_str == "source":
            role: SyncRole | None = SyncRole.SOURCE
        elif last_role_str == "target":
            role = SyncRole.TARGET
        else:
            return None, None
        peer_raw = data.get("last_peer")
        peer: str | None = peer_raw if isinstance(peer_raw, str) else None
        return role, peer
    except Exception:
        return None, None


def get_last_sync_state() -> tuple[SyncRole | None, str | None]:
    """Get the last sync role and peer of this machine from the local history file.

    Returns:
        (SyncRole, peer_hostname) on valid history, (None, None) if the
        history file is missing, unreadable, or corrupt.
    """
    history_path = get_history_path()
    if not history_path.exists():
        return None, None
    try:
        content = history_path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    return parse_sync_state(content)
