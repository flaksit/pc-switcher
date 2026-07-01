"""Sync history tracking to detect consecutive syncs from the same source.

Tracks whether this machine's last role in a sync was SOURCE or TARGET,
and per-target btrfs subvolume generations used for the divergence guard.

State file: ~/.local/share/pc-switcher/sync-history.json
Format (backward-compatible; old files with only last_role still work):
    {
        "last_role": "source" | "target",
        "target_generations": {
            "<target_hostname>": {"<path>": <generation_int>, ...},
            ...
        }
    }

Every write to this file is merge-preserving — record_role only updates
last_role and leaves target_generations intact, and set_target_generation
only updates the relevant nested value and leaves last_role intact.
This prevents the Pitfall 4 false-divergence that would occur if a role
switch silently erased the stored generation markers.

Special generation values
-------------------------
- A positive integer is a real btrfs subvolume generation from a successful sync.
- None (absence of a key) means "never synced" — the divergence guard is skipped
  fail-open for the first sync (RESEARCH Open Q3).
- UNKNOWN_GENERATION (-1) means "a baseline could not be established during the
  last sync run" — the divergence guard must treat it as UNVERIFIABLE and fail
  closed (block the sync) rather than as a first sync. This sentinel is written by
  execute() when btrfs generation capture fails after a successful rsync transfer
  so that the data transfer is not rolled back while the next run is still guarded
  against undetected target divergence (WR-02 / CR-02).
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
    "UNKNOWN_GENERATION",
    "SyncRole",
    "get_history_path",
    "get_last_role",
    "get_last_role_with_error",
    "get_record_role_command",
    "get_target_generation",
    "record_role",
    "set_target_generation",
]

# Shell-expandable paths for use in SSH commands (remote execution).
# For local Python operations, use get_history_path() which returns a Path object.
HISTORY_DIR = "~/.local/share/pc-switcher"
HISTORY_PATH = f"{HISTORY_DIR}/sync-history.json"

# Sentinel stored when the post-sync btrfs generation could not be captured.
# Distinct from None (never synced / no key): UNKNOWN_GENERATION means the guard
# IS active but the baseline is uncertain — the next run must treat the target as
# UNVERIFIABLE and fail closed rather than proceeding as a first sync (WR-02/CR-02).
UNKNOWN_GENERATION: int = -1


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

    Merge-preserving: reads any existing data (including target_generations)
    and keeps it intact, only updating last_role. This prevents the Pitfall 4
    false-divergence that would occur if a role switch erased stored markers.

    Uses atomic write (temp file + rename) to prevent corruption.

    Args:
        role: The role this machine played (SOURCE or TARGET)
    """
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing data to preserve target_generations and any other keys.
    # json.loads returns Any, so isinstance is needed to narrow the type.
    try:
        raw = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
        existing: dict[str, object] = raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        existing = {}

    data = {**existing, "last_role": role.value}
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

    The returned command is merge-preserving: it reads any existing
    sync-history.json on the remote (including target_generations), updates
    only last_role, and writes atomically via a temp-file rename. This prevents
    the Pitfall 4 false-divergence that would occur if the remote role-record
    command erased markers written earlier in the same sync by FolderSyncJob.

    Args:
        role: The role to record (SOURCE or TARGET)

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
        "c=json.dumps(d)",
        "fd,t=tempfile.mkstemp(dir=p.parent,prefix='.sync-history-',suffix='.tmp')",
        "os.write(fd,c.encode());os.close(fd)",
        "Path(t).rename(p)",
    ]
    script = "\n".join(lines)
    return f'mkdir -p {HISTORY_DIR} && python3 -c "{script}"'


def get_target_generation(target_hostname: str, path: str) -> int | None:
    """Get the stored btrfs subvolume generation for a target host and path.

    Used by the divergence guard (D-08) to compare the current target
    subvolume generation against the post-sync baseline.

    Args:
        target_hostname: Hostname of the target machine
        path: Absolute folder path (e.g. "/home")

    Returns:
        The stored generation integer, or None if no marker exists for this
        (target_hostname, path) pair, if the file is missing, or if it is corrupt.
    """
    history_path = get_history_path()
    if not history_path.exists():
        return None
    try:
        data = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        target_gens = data.get("target_generations")
        if not isinstance(target_gens, dict):
            return None
        host_gens = target_gens.get(target_hostname)
        if not isinstance(host_gens, dict):
            return None
        value = host_gens.get(path)
        return value if isinstance(value, int) else None
    except (json.JSONDecodeError, OSError):
        return None


def set_target_generation(target_hostname: str, path: str, generation: int) -> None:
    """Store the btrfs subvolume generation for a target host and path.

    Merge-preserving read-modify-write: loads existing JSON (treating
    missing or corrupt as {}), sets the nested value for the given
    (target_hostname, path) pair, preserves last_role and all other
    targets/paths, and writes atomically via temp-file rename.

    Args:
        target_hostname: Hostname of the target machine
        path: Absolute folder path (e.g. "/home")
        generation: btrfs subvolume generation number to store
    """
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    # json.loads returns Any, so isinstance is needed to narrow the type.
    try:
        raw = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
        existing: dict[str, object] = raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        existing = {}

    # Merge: preserve last_role and all other targets; only update the target/path pair
    target_gens = existing.get("target_generations")
    if not isinstance(target_gens, dict):
        target_gens = {}
    host_gens = target_gens.get(target_hostname)
    if not isinstance(host_gens, dict):
        host_gens = {}
    host_gens[path] = generation
    target_gens[target_hostname] = host_gens
    data = {**existing, "target_generations": target_gens}

    content = json.dumps(data)

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
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            Path(temp_path).unlink()
        raise
