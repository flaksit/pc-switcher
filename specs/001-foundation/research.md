# Research: Foundation Infrastructure

**Feature**: 001-foundation
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

def snapshot_name(subvolume: str, phase: str, session_id: str) -> str:
    """Generate snapshot name per FR-010.

    Example: @home-presync-20251129T143022-abc12345
    """
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{subvolume}-{phase}-{timestamp}-{session_id}"

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
Snapshots created in the btrfs root (alongside subvolumes), not inside the subvolume being snapshotted. This requires:
1. Knowing the btrfs mount point (typically `/` for root filesystem)
2. Creating snapshots at `/<btrfs-root>/<snapshot-name>`

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
- Global config schema: `specs/001-foundation/contracts/config-schema.yaml`
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
Use fcntl file locking on `~/.local/share/pc-switcher/sync.lock` to prevent concurrent sync operations.

### Rationale
- fcntl locks are advisory but sufficient for same-user processes
- Lock file contains PID for diagnostic messages
- Lock is automatically released on process exit (including crashes)

### Implementation Pattern

```python
from __future__ import annotations

import fcntl
import os
from pathlib import Path

class SyncLock:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._lock_file: int | None = None

    def acquire(self) -> bool:
        """Acquire lock. Returns False if another sync is running."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID for diagnostics
            os.ftruncate(self._lock_file, 0)
            os.write(self._lock_file, str(os.getpid()).encode())
            return True
        except BlockingIOError:
            # Lock held by another process
            os.close(self._lock_file)
            self._lock_file = None
            return False

    def get_holder_pid(self) -> int | None:
        """Read PID of process holding the lock."""
        try:
            return int(self._lock_path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def release(self) -> None:
        if self._lock_file is not None:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            os.close(self._lock_file)
            self._lock_file = None
```

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

## Summary of Technology Decisions

| Component | Technology | Rationale |
|-----------|------------|-----------|
| SSH | asyncssh | Async-native, mature, respects SSH config |
| Logging | structlog | Dual output, structured context, processors |
| Terminal UI | Rich Live | Smooth updates, layout composition |
| Snapshots | btrfs CLI | Standard tool, no extra dependencies |
| Config validation | jsonschema | Standard schema language, clear errors |
| Cancellation | asyncio.CancelledError | Native pattern, TaskGroup integration |
| Locking | fcntl | Simple, automatic release on crash |
| Disk space | df parsing | Universal availability, straightforward |
