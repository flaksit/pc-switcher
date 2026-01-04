# Research: Core Infrastructure

**Feature**: 001-core
**Date**: 2025-11-29

This document consolidates research findings for implementation decisions. All technology choices are informed by ADRs and the architecture defined in `architecture.md`.

## 1. SSH Connection Management with asyncssh

### Decision
Use asyncssh with a single persistent connection, session semaphore for multiplexing, and SSH keepalive for connection health.

### Rationale
- asyncssh is the mature async SSH library for Python, actively maintained with Python 3.14 support
- Multiplexing via semaphore prevents overwhelming the SSH server with concurrent sessions
- Keepalive packets detect connection loss proactively rather than waiting for command timeout

### Implementation Pattern

```python
from __future__ import annotations

import asyncio
import asyncssh

class Connection:
    def __init__(
        self,
        target: str,
        max_sessions: int = 10,
        keepalive_interval: int = 15,
        keepalive_count_max: int = 3,
    ) -> None:
        self._target = target
        self._conn: asyncssh.SSHClientConnection | None = None
        self._session_semaphore = asyncio.Semaphore(max_sessions)
        self._keepalive_interval = keepalive_interval
        self._keepalive_count_max = keepalive_count_max

    async def connect(self) -> None:
        # asyncssh respects ~/.ssh/config automatically
        self._conn = await asyncssh.connect(
            self._target,
            keepalive_interval=self._keepalive_interval,
            keepalive_count_max=self._keepalive_count_max,
        )

    async def create_process(self, cmd: str) -> asyncssh.SSHClientProcess:
        async with self._session_semaphore:
            assert self._conn is not None
            return await self._conn.create_process(cmd)

    async def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            await self._conn.wait_closed()
```

### Alternatives Considered
- **paramiko**: Synchronous API, would require threading for async operations
- **fabric**: Built on paramiko, adds complexity without async benefits
- **subprocess + ssh binary**: Less control over connection lifecycle, harder to multiplex

## 2. Structured Logging with structlog

### Decision
Use structlog with dual processor chains: JSONRenderer for file output, ConsoleRenderer for terminal. EventBus decouples log production from consumption.

### Rationale
- structlog provides context-rich structured logging with minimal boilerplate
- JSON output enables log analysis tools; console output provides human readability
- Separate processor chains allow different formatting without code duplication

### Implementation Pattern

```python
from __future__ import annotations

import structlog
from pathlib import Path

def configure_logging(log_file: Path, file_level: str, cli_level: str) -> None:
    # Shared processors for both outputs
    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    # File output: JSON lines
    file_processors = [
        *shared_processors,
        structlog.processors.JSONRenderer(),
    ]

    # Console output: colored, human-readable
    console_processors = [
        *shared_processors,
        structlog.dev.ConsoleRenderer(colors=True),
    ]
```

The actual implementation routes through EventBus rather than direct structlog output, allowing:
- Non-blocking log delivery to file and UI
- Per-consumer level filtering
- Graceful shutdown with queue draining

### Log Level Ordering
```
DEBUG (0) > FULL (1) > INFO (2) > WARNING (3) > ERROR (4) > CRITICAL (5)
```
Lower numeric value = more verbose. A level includes all messages at that level and higher (less verbose).

### Alternatives Considered
- **logging stdlib**: Works but lacks structured context and processor pipeline
- **loguru**: Good but less flexible processor chains; structlog better for dual output

## 3. Terminal UI with Rich Live Display

### Decision
Use Rich Live with a custom layout combining Progress (job progress bars), Panel (scrolling log messages), and status indicators.

### Rationale
- Rich Live provides smooth, flicker-free terminal updates
- Layout composability allows combining progress + logs + status in single view
- Console protocol enables direct rendering without intermediate strings

### Implementation Pattern

```python
from __future__ import annotations

from collections import deque
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import Progress, TaskID
from rich.table import Table

class TerminalUI:
    def __init__(self, console: Console, max_log_lines: int = 10) -> None:
        self._console = console
        self._progress = Progress(console=console)
        self._log_panel: deque[str] = deque(maxlen=max_log_lines)
        self._job_tasks: dict[str, TaskID] = {}
        self._live: Live | None = None
        self._connection_status = "disconnected"
        self._connection_latency: float | None = None

    def _render(self) -> Group:
        # Status bar
        status = Table.grid()
        status.add_row(
            f"Connection: {self._connection_status}",
            f"Latency: {self._connection_latency or 'N/A'}ms",
        )

        # Log panel
        log_text = "\n".join(self._log_panel)
        log_panel = Panel(log_text, title="Logs", height=12)

        return Group(status, self._progress, log_panel)

    def start(self) -> None:
        self._live = Live(self._render(), console=self._console, refresh_per_second=10)
        self._live.start()

    def update_job_progress(self, job: str, percent: int, item: str | None) -> None:
        if job not in self._job_tasks:
            self._job_tasks[job] = self._progress.add_task(job, total=100)
        self._progress.update(
            self._job_tasks[job],
            completed=percent,
            description=f"{job}: {item or ''}",
        )
        if self._live:
            self._live.update(self._render())
```

### UI Update Rate
- Refresh rate: 10 Hz (100ms interval) provides smooth updates without CPU overhead
- Progress updates batched if arriving faster than refresh rate

### Alternatives Considered
- **textual**: Full TUI framework, overkill for progress + log display
- **tqdm**: Progress bars only, no layout composition
- **curses**: Low-level, requires manual rendering logic

## 4. Btrfs Snapshot Management

### Decision
Use direct btrfs commands via subprocess/SSH for snapshot operations. Parse command output for error detection.

### Rationale
- btrfs-progs CLI is the standard interface; no Python bindings needed
- Read-only snapshots are atomic and use CoW (zero initial space)
- Snapshot cleanup uses standard `btrfs subvolume delete`

### Implementation Pattern

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

@dataclass
class SnapshotConfig:
    subvolume: str  # e.g., "@home"
    mount_point: str  # e.g., "/home"

def snapshot_name(subvolume: str, phase: str) -> str:
    """Generate snapshot name per FR-010.

    Example: pre-@home-20251129T143022
    """
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{phase}-{subvolume}-{timestamp}"

async def create_snapshot(
    executor: Executor,
    source_path: str,
    snapshot_path: str,
) -> CommandResult:
    """Create read-only btrfs snapshot."""
    cmd = f"sudo btrfs subvolume snapshot -r {source_path} {snapshot_path}"
    return await executor.run_command(cmd)
```

### Snapshot Location
Snapshots are stored in `/.snapshots/pc-switcher/<timestamp>-<session-id>/` which MUST be a separate btrfs subvolume:

1. **Path structure**: `/.snapshots/pc-switcher/20251129T143022-abc12345/pre-@home-20251129T143022`
2. **`/.snapshots/` MUST be a subvolume** - prevents recursive snapshots when snapshotting `/`
3. **Session folders** use `<timestamp>-<session-id>` format for chronological sorting
4. **Snapshot names** use `{pre|post}-<subvolume>-<timestamp>` format
5. **Auto-creation**: If `/.snapshots/` doesn't exist, create it as a subvolume

```bash
# Check if /.snapshots is a subvolume
sudo btrfs subvolume show /.snapshots >/dev/null 2>&1

# Create /.snapshots as subvolume if needed
sudo btrfs subvolume create /.snapshots
mkdir -p /.snapshots/pc-switcher
```

### Error Conditions
| Exit Code | Meaning | Action |
|-----------|---------|--------|
| 0 | Success | Continue |
| 1 | General error (e.g., path not found) | Log CRITICAL, abort |
| 28 | No space left | Log CRITICAL with df output, abort |

### Alternatives Considered
- **btrfsutil Python bindings**: Not commonly installed, adds dependency
- **snapper**: Too opinionated about snapshot management; we need direct control

## 5. Configuration Validation with jsonschema

### Decision
Jobs declare config schemas as Python dicts (JSON Schema draft-07). Orchestrator validates using jsonschema library.

### Rationale
- JSON Schema is a well-established standard with Python support
- Jobs own their schema definitions (decoupled from orchestrator)
- Validation errors provide clear paths to invalid values

### Implementation Pattern

```python
from __future__ import annotations

from jsonschema import Draft7Validator, ValidationError
from typing import Any

# Job declares its schema as class attribute
class BtrfsSnapshotJob(SystemJob):
    CONFIG_SCHEMA = {
        "type": "object",
        "properties": {
            "subvolumes": {
                "type": "array",
                "items": {"type": "string", "pattern": "^@"},
                "minItems": 1,
                "description": "Btrfs subvolume names to snapshot",
            },
            "keep_recent": {
                "type": "integer",
                "minimum": 1,
                "default": 3,
                "description": "Number of recent snapshots to retain",
            },
        },
        "required": ["subvolumes"],
    }

def validate_job_config(
    job_name: str,
    schema: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    """Validate config against schema, return list of error messages."""
    validator = Draft7Validator(schema)
    errors = []
    for error in validator.iter_errors(config):
        path = ".".join(str(p) for p in error.absolute_path)
        errors.append(f"{job_name}.{path}: {error.message}")
    return errors
```

### Schema Location
- Global config schema: `specs/001-core/contracts/config-schema.yaml`
- Job schemas: Embedded in job classes as `CONFIG_SCHEMA` class attribute

### Alternatives Considered
- **pydantic**: More Pythonic but heavier; JSON Schema allows schema export
- **attrs + cattrs**: Good for dataclasses but lacks schema validation
- **cerberus**: Less common, JSON Schema more widely understood

## 6. Asyncio Cancellation Pattern

### Decision
Use native `asyncio.CancelledError` for job cancellation. Jobs catch the exception, clean up resources, and re-raise.

### Rationale
- Native asyncio pattern; no custom cancellation flags needed
- TaskGroup automatically cancels sibling tasks on exception
- Cleanup code runs in exception handler before re-raise

### Implementation Pattern

```python
from __future__ import annotations

import asyncio

class Job:
    async def execute(self, context: JobContext) -> None:
        try:
            await self._do_work(context)
        except asyncio.CancelledError:
            # Cleanup before re-raising
            await self._cleanup(context)
            raise

    async def _cleanup(self, context: JobContext) -> None:
        """Override in subclass for cleanup logic."""
        pass
```

### Signal Handling
SIGINT is handled at orchestrator level by cancelling the TaskGroup:

```python
import signal

class Orchestrator:
    def _setup_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, self._handle_sigint)

    def _handle_sigint(self) -> None:
        if self._task_group:
            # Cancel all tasks in the group
            for task in asyncio.all_tasks():
                task.cancel()
```

## 7. Lock File for Concurrent Execution Prevention

### Decision
Use fcntl file locking on both source and target machines to prevent concurrent sync operations.

### Rationale

**Why fcntl.flock() instead of normal file operations?**

A naive approach using file existence checking has critical flaws:
```python
# DON'T DO THIS - race condition and stale lock problems
if lock_file.exists():
    return False
lock_file.write_text(str(os.getpid()))  # Another process can sneak in here!
```

1. **Race condition**: Between `exists()` and `write_text()`, another process can create the file. Both think they have the lock.
2. **Stale locks**: If process crashes after creating the file, the lock file remains forever, blocking all future syncs.

**fcntl.flock() solves both:**
1. **Atomic operation**: Kernel guarantees "check and acquire" is indivisible. No race condition.
2. **Auto-release on exit**: When process exits (normal, crash, or killed), kernel releases all its flock locks automatically. No stale locks.
3. **Non-blocking mode**: `LOCK_NB` flag returns immediately if lock is held by another process.

**Locking strategy:**
- **Source lock**: Prevents same source from running multiple syncs simultaneously
- **Target lock**: Prevents multiple sources from syncing to the same target (e.g., A→B and C→B)
- Lock file contains PID (and source hostname for target lock) for diagnostic messages

### Lock Locations
- Source: `~/.local/share/pc-switcher/sync.lock`
- Target: `~/.local/share/pc-switcher/target.lock`

### Implementation Pattern

```python
from __future__ import annotations

import fcntl
import os
from pathlib import Path

class SyncLock:
    """File-based lock using fcntl.

    Note: os.open() is required for fcntl.flock() which needs a file descriptor.
    Path is used for directory creation and reading lock contents.
    """

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._lock_fd: int | None = None

    def acquire(self, holder_info: str | None = None) -> bool:
        """Acquire lock. Returns False if already held.

        Args:
            holder_info: Info to write to lock file (e.g., PID, hostname)
        """
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write holder info for diagnostics
            info = holder_info or str(os.getpid())
            os.ftruncate(self._lock_fd, 0)
            os.write(self._lock_fd, info.encode())
            return True
        except BlockingIOError:
            os.close(self._lock_fd)
            self._lock_fd = None
            return False

    def get_holder_info(self) -> str | None:
        """Read info about process holding the lock."""
        try:
            return self._lock_path.read_text().strip() or None
        except FileNotFoundError:
            return None

    def release(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None
```

### Target Lock Acquisition

Target lock is acquired via SSH after connection is established:

```python
async def acquire_target_lock(
    executor: RemoteExecutor,
    source_hostname: str,
) -> bool:
    """Acquire lock on target machine."""
    lock_path = "~/.local/share/pc-switcher/target.lock"
    holder_info = f"{source_hostname}:{os.getpid()}"

    # Use flock command on target (simpler than transferring Python code)
    result = await executor.run_command(
        f'mkdir -p ~/.local/share/pc-switcher && '
        f'exec 9>"{lock_path}" && '
        f'flock -n 9 && '
        f'echo "{holder_info}" > "{lock_path}"'
    )
    return result.success

async def get_target_lock_holder(executor: RemoteExecutor) -> str | None:
    """Get info about who holds the target lock."""
    result = await executor.run_command(
        'cat ~/.local/share/pc-switcher/target.lock 2>/dev/null'
    )
    return result.stdout.strip() if result.success and result.stdout else None
```

### Target Lock Release

The target lock uses `flock` via the SSH session. Lock release happens automatically in these scenarios:

| Scenario | Release Mechanism |
|----------|-------------------|
| **Sync completes normally** | SSH session ends → file descriptor closes → `flock` releases |
| **Sync aborts (error)** | SSH connection closes → same as above |
| **Source crashes** | SSH keepalive timeout (45s max) → connection terminates → lock released |
| **Network loss** | SSH keepalive detects → connection terminates → lock released |
| **User Ctrl+C** | Orchestrator closes connection → lock released |

**Key insight**: Because `flock` locks are tied to file descriptors, and our file descriptor lives within the SSH session, the lock is automatically released when the SSH connection terminates for any reason. This is a critical safety property—there are no stale target locks to worry about.

**Caveat**: The lock file itself remains on disk (containing the last holder info). This is intentional—it provides diagnostic information and doesn't block future syncs since the `flock` lock is what matters, not file existence.

### Error Messages

- Source lock held: `"Another sync is in progress (PID: 12345)"`
- Target lock held: `"Target is being synced from another source (laptop-work:54321)"`

## 8. Disk Space Parsing

### Decision
Parse `df` output for disk space checks. Support both percentage and absolute thresholds.

### Rationale
- `df` is universally available on Linux
- Parsing is straightforward with consistent output format
- Both percentage and absolute values needed per FR-016/FR-017

### Implementation Pattern

```python
from __future__ import annotations

import re
from dataclasses import dataclass

@dataclass
class DiskSpace:
    total_bytes: int
    used_bytes: int
    available_bytes: int
    use_percent: int
    mount_point: str

def parse_df_output(output: str, mount_point: str) -> DiskSpace | None:
    """Parse df -B1 output for specific mount point."""
    for line in output.strip().split("\n")[1:]:  # Skip header
        parts = line.split()
        if len(parts) >= 6 and parts[5] == mount_point:
            return DiskSpace(
                total_bytes=int(parts[1]),
                used_bytes=int(parts[2]),
                available_bytes=int(parts[3]),
                use_percent=int(parts[4].rstrip("%")),
                mount_point=parts[5],
            )
    return None

def parse_threshold(threshold: str) -> tuple[str, int]:
    """Parse threshold like '20%' or '50GiB'.

    Returns: (type, value) where type is 'percent' or 'bytes'
    """
    if threshold.endswith("%"):
        return ("percent", int(threshold[:-1]))
    match = re.match(r"(\d+)(GiB|MiB|GB|MB)", threshold)
    if match:
        value, unit = match.groups()
        multipliers = {"GiB": 2**30, "MiB": 2**20, "GB": 10**9, "MB": 10**6}
        return ("bytes", int(value) * multipliers[unit])
    raise ValueError(f"Invalid threshold format: {threshold}")
```

## 9. Duration Parsing for Cleanup Command

### Decision
Use a duration parsing library (e.g., `pytimeparse2`) for the `--older-than` flag in `cleanup-snapshots`.

### Rationale
- Human-readable durations (`7d`, `2w`, `1m`) are more user-friendly than raw integers
- Avoid writing custom parsing logic
- Standard library format is well-tested and handles edge cases

### Implementation Pattern

```python
from pytimeparse2 import parse as parse_duration

def parse_older_than(value: str) -> int:
    """Parse human-readable duration to days.

    Examples: '7d' → 7, '2w' → 14, '1m' → 30
    """
    seconds = parse_duration(value)
    if seconds is None:
        raise ValueError(f"Invalid duration format: {value}")
    return seconds // 86400  # Convert to days
```

### Alternatives Considered
- **Custom regex parsing**: Error-prone, reinventing the wheel
- **Integer days only**: Less user-friendly
- **dateutil.relativedelta**: Overkill for simple duration parsing

## 10. Version Comparison for Self-Installation

### Decision
Use `packaging.version` for comparing pc-switcher versions between source and target.

### Rationale
- PEP 440 compliant version parsing and comparison
- Correctly handles pre-releases, dev versions, and post-releases
- Standard Python packaging library (already a dependency of pip/uv ecosystem)
- Avoids string comparison pitfalls (e.g., "0.10.0" > "0.9.0")

### Implementation Pattern

```python
from packaging.version import Version

def compare_versions(source: str, target: str) -> int:
    """Compare semantic versions.

    Returns:
        -1 if source < target
         0 if source == target
         1 if source > target
    """
    src = Version(source)
    tgt = Version(target)
    if src < tgt:
        return -1
    elif src > tgt:
        return 1
    return 0
```

### Alternatives Considered
- **String comparison**: Fails on multi-digit versions ("0.10.0" < "0.9.0" as strings)
- **Custom parsing with split/int**: Doesn't handle pre-releases or edge cases
- **semver library**: External dependency when `packaging` is already available

## Summary of Technology Decisions

| Component | Technology | Rationale |
|-----------|------------|-----------|
| SSH | asyncssh | Async-native, mature, respects SSH config |
| Logging | structlog | Dual output, structured context, processors |
| Terminal UI | Rich Live | Smooth updates, layout composition |
| Snapshots | btrfs CLI | Standard tool, no extra dependencies |
| Config validation | jsonschema | Standard schema language, clear errors |
| Cancellation | asyncio.CancelledError | Native pattern, TaskGroup integration |
| Locking | fcntl (source + target) | Simple, automatic release on crash, prevents A→B + C→B |
| Disk space | df parsing | Universal availability, straightforward |
| Duration parsing | pytimeparse2 | Human-readable input, well-tested |
| Version comparison | packaging.version | PEP 440 compliant, handles pre-releases correctly |
