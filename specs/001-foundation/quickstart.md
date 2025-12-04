# Quickstart: Foundation Infrastructure Development

**Feature**: 001-foundation
**Date**: 2025-11-29

This guide provides the essential commands and patterns for developing the foundation infrastructure.

## Prerequisites

- Python 3.14 (install via `uv python install 3.14`)
- `uv` package manager
- SSH access to target machine configured in `~/.ssh/config`
- btrfs filesystem on both source and target

## Project Setup

```bash
# Clone and setup
cd pc-switcher

# Initialize Python project (if not exists)
uv init --python 3.14

# Add dependencies
uv add asyncssh rich typer structlog pyyaml jsonschema packaging pytimeparse2

# Add dev dependencies
uv add --dev pytest pytest-asyncio basedpyright ruff codespell
```

## Development Commands

```bash
# Run all quality checks
uv run ruff format --check .
uv run ruff check .
uv run basedpyright
uv run pytest

# Format and fix
uv run ruff format .
uv run ruff check --fix .

# Run specific tests
uv run pytest tests/unit/test_config.py -v
uv run pytest -k "test_job" -v

# Type check single file
uv run basedpyright src/pcswitcher/orchestrator.py
```

## Source Structure

```
src/pcswitcher/
├── __init__.py          # Package version, exports
├── cli.py               # Typer CLI commands
├── orchestrator.py      # Main sync coordination
├── config.py            # YAML loading, validation
├── connection.py        # asyncssh wrapper
├── executor.py          # LocalExecutor, RemoteExecutor
├── events.py            # EventBus, event types
├── logger.py            # Logger, JobLogger, FileLogger
├── ui.py                # Rich terminal UI
├── snapshots.py         # btrfs snapshot operations
├── installation.py      # Target pc-switcher install
├── models.py            # Core types (enums, dataclasses)
├── disk.py              # DiskSpace, parse_threshold, check_disk_space
└── jobs/
    ├── __init__.py
    ├── base.py          # Job ABC hierarchy
    ├── context.py       # JobContext
    ├── disk_space_monitor.py  # DiskSpaceMonitorJob
    ├── dummy.py         # Test jobs
    └── btrfs.py         # BtrfsSnapshotJob
```

## Key Implementation Patterns

### 1. Async Entry Point

```python
# src/pcswitcher/cli.py
from __future__ import annotations

import asyncio
import typer

app = typer.Typer()

@app.command()
def sync(target: str) -> None:
    """Sync to target machine."""
    asyncio.run(_sync(target))

async def _sync(target: str) -> None:
    orchestrator = Orchestrator(target)
    await orchestrator.run()
```

### 2. EventBus Consumer

```python
# src/pcswitcher/logger.py
from __future__ import annotations

import asyncio
from pathlib import Path

import structlog

from pcswitcher.events import LogEvent
from pcswitcher.models import LogLevel

class FileLogger:
    """Consumes LogEvents and writes JSON lines to file (FR-022).

    Uses structlog's JSONRenderer for consistent JSON output format.
    """

    def __init__(self, log_file: Path, level: LogLevel, queue: asyncio.Queue) -> None:
        self._log_file = log_file
        self._level = level
        self._queue = queue
        # Configure structlog processor for JSON output
        self._json_renderer = structlog.processors.JSONRenderer()

    async def consume(self) -> None:
        """Run as background task."""
        with self._log_file.open("a") as f:
            while True:
                event = await self._queue.get()
                if event is None:  # Shutdown sentinel
                    break
                if isinstance(event, LogEvent) and event.level >= self._level:
                    # Convert LogEvent to dict and serialize to JSON via structlog
                    event_dict = event.to_dict()
                    # JSONRenderer returns a JSON string when called as processor
                    json_line = self._json_renderer(None, None, event_dict)
                    f.write(json_line + "\n")
                    f.flush()
```

### 3. Job Implementation

```python
# src/pcswitcher/jobs/dummy.py
from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import Host, JobContext, LogLevel, ProgressUpdate, ValidationError


class DummySuccessJob(SyncJob):
    """Dummy job for testing infrastructure (FR-039).

    Simulates 20s operation on source (log every 2s, WARNING at 6s)
    and 20s on target (log every 2s, ERROR at 8s).
    Progress milestones: 0% (start) → 25% (10s source) → 50% (20s, end source)
                       → 75% (30s, 10s target) → 100% (40s, end target)
    """

    name: ClassVar[str] = "dummy_success"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        # Context stored as self._context by base class

    async def validate(self) -> list[ValidationError]:
        return []  # No prerequisites

    async def execute(self) -> None:
        try:
            self._report_progress(ProgressUpdate(percent=0))

            # Source phase: 20s with 2s intervals (10 iterations)
            await self._run_source_phase()
            self._report_progress(ProgressUpdate(percent=50))

            # Target phase: 20s with 2s intervals (10 iterations)
            await self._run_target_phase()
            self._report_progress(ProgressUpdate(percent=100))

        except asyncio.CancelledError:
            self._log(Host.SOURCE, LogLevel.WARNING, "Dummy job termination requested")
            raise

    async def _run_source_phase(self) -> None:
        """Source phase: 20s total, log every 2s, WARNING at 6s."""
        for tick in range(10):  # 10 iterations × 2s = 20s
            elapsed = (tick + 1) * 2  # 2, 4, 6, ..., 20
            self._log(
                Host.SOURCE, LogLevel.INFO,
                f"Source phase: {elapsed}s elapsed"
            )

            # WARNING at 6s (after tick 2, when elapsed=6)
            if elapsed == 6:
                self._log(Host.SOURCE, LogLevel.WARNING, "Test warning at 6s")

            # Progress: 25% at halfway (10s)
            if elapsed == 10:
                self._report_progress(ProgressUpdate(percent=25))

            await asyncio.sleep(2)

    async def _run_target_phase(self) -> None:
        """Target phase: 20s total, log every 2s, ERROR at 8s."""
        for tick in range(10):  # 10 iterations × 2s = 20s
            elapsed = (tick + 1) * 2  # 2, 4, 6, ..., 20
            self._log(
                Host.TARGET, LogLevel.INFO,
                f"Target phase: {elapsed}s elapsed"
            )

            # ERROR at 8s (after tick 3, when elapsed=8)
            if elapsed == 8:
                self._log(Host.TARGET, LogLevel.ERROR, "Test error at 8s")

            # Progress: 75% at halfway (10s into target = 30s total)
            if elapsed == 10:
                self._report_progress(ProgressUpdate(percent=75))

            await asyncio.sleep(2)
```

### 4. Command Execution (Local and Remote)

```python
# src/pcswitcher/executor.py
from __future__ import annotations

import asyncio
from pathlib import Path

import asyncssh

from pcswitcher.models import CommandResult


class LocalExecutor:
    """Executes commands on the source machine via async subprocess."""

    def __init__(self) -> None:
        self._processes: list[asyncio.subprocess.Process] = []

    async def run_command(
        self, cmd: str, timeout: float | None = None
    ) -> CommandResult:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
        return CommandResult(
            exit_code=proc.returncode or 0,
            stdout=stdout.decode() if stdout else "",
            stderr=stderr.decode() if stderr else "",
        )

    async def start_process(self, cmd: str) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes.append(proc)
        return proc

    async def terminate_all_processes(self) -> None:
        for proc in self._processes:
            proc.terminate()
        self._processes.clear()


class RemoteExecutor:
    def __init__(self, conn: asyncssh.SSHClientConnection) -> None:
        self._conn = conn
        self._processes: list[asyncssh.SSHClientProcess] = []

    async def run_command(
        self, cmd: str, timeout: float | None = None
    ) -> CommandResult:
        result = await asyncio.wait_for(
            self._conn.run(cmd),
            timeout=timeout,
        )
        return CommandResult(
            exit_code=result.exit_status or 0,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    async def start_process(self, cmd: str) -> asyncssh.SSHClientProcess:
        process = await self._conn.create_process(cmd)
        self._processes.append(process)
        return process

    async def terminate_all_processes(self) -> None:
        for process in self._processes:
            process.terminate()
        self._processes.clear()
```

## Testing Patterns

### Mock SSH Connection

```python
# tests/conftest.py
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.models import CommandResult


@pytest.fixture
def mock_connection():
    conn = MagicMock()
    conn.run = AsyncMock(return_value=MagicMock(
        exit_status=0,
        stdout="output",
        stderr="",
    ))
    return conn


@pytest.fixture
def mock_executor(mock_connection):
    from pcswitcher.executor import RemoteExecutor
    return RemoteExecutor(mock_connection)
```

### Test Job Lifecycle

```python
# tests/contract/test_job_interface.py
from __future__ import annotations

import pytest

from pcswitcher.jobs.dummy import DummySuccessJob
from pcswitcher.models import JobContext


@pytest.fixture
def job_context(mock_executor):
    return JobContext(
        config={},
        source=mock_executor,
        target=mock_executor,
        event_bus=MagicMock(),
        session_id="test1234",
        source_hostname="source-host",
        target_hostname="target-host",
    )


@pytest.mark.asyncio
async def test_dummy_success_completes(job_context):
    job = DummySuccessJob(job_context)
    errors = await job.validate()
    assert errors == []
    # Execute would take 40s, mock time or reduce durations
```

## Configuration Example

```yaml
# ~/.config/pc-switcher/config.yaml
log_file_level: FULL
log_cli_level: INFO

sync_jobs:
  dummy_success: true   # Test job that completes successfully
  dummy_fail: false     # Test job that fails at configurable progress %
  # Future sync jobs (not yet implemented - features 5-10):
  # user_data: false
  # packages: false

disk_space_monitor:
  preflight_minimum: "20%"
  runtime_minimum: "15%"    # CRITICAL abort if below
  warning_threshold: "25%"  # WARNING log if below
  check_interval: 30

btrfs_snapshots:
  # Configure these to match YOUR system's btrfs subvolume layout
  subvolumes:
    - "@"       # Example: root filesystem
    - "@home"   # Example: home directories
  keep_recent: 3
```

## Running pc-switcher

```bash
# Development: sync dependencies and run via uv
uv sync
uv run pc-switcher sync target-hostname

# View recent logs
uv run pc-switcher logs --last

# Cleanup old snapshots
uv run pc-switcher cleanup-snapshots --older-than 7d

# Production: install as a tool (globally available)
uv tool install .
pc-switcher sync target-hostname
```

## Debugging Tips

1. **SSH Issues**: Test connection manually with `ssh target-hostname 'echo hello'`
2. **Log Output**: Set `log_cli_level: DEBUG` to see all messages
3. **Snapshot Issues**: Run `sudo btrfs subvolume list /` to verify subvolume structure
4. **Type Errors**: Run `uv run basedpyright` frequently during development

## Next Steps

After foundation is complete:
1. Implement actual sync jobs (user_data, packages, docker, etc.)
2. Add integration tests with real SSH connections
3. Set up CI/CD with GitHub Actions
