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
cd /home/janfr/dev/pc-switcher

# Initialize Python project (if not exists)
uv init --python 3.14

# Add dependencies
uv add asyncssh rich typer structlog pyyaml jsonschema

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
└── jobs/
    ├── __init__.py
    ├── base.py          # Job ABC hierarchy
    ├── context.py       # JobContext
    ├── disk_monitor.py  # DiskSpaceMonitorJob
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

class FileLogger:
    def __init__(self, log_file: Path, level: LogLevel, queue: asyncio.Queue) -> None:
        self._log_file = log_file
        self._level = level
        self._queue = queue

    async def consume(self) -> None:
        """Run as background task."""
        with self._log_file.open("a") as f:
            while True:
                event = await self._queue.get()
                if event is None:  # Shutdown sentinel
                    break
                if isinstance(event, LogEvent) and event.level >= self._level:
                    f.write(event.to_json() + "\n")
                    f.flush()
```

### 3. Job Implementation

```python
# src/pcswitcher/jobs/dummy.py
from __future__ import annotations

import asyncio
from typing import ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import Host, JobContext, LogLevel, ProgressUpdate, ValidationError


class DummySuccessJob(SyncJob):
    name: ClassVar[str] = "dummy_success"
    CONFIG_SCHEMA: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "source_duration": {"type": "integer", "default": 20},
            "target_duration": {"type": "integer", "default": 20},
        },
    }

    async def validate(self, context: JobContext) -> list[ValidationError]:
        return []  # No prerequisites

    async def execute(self, context: JobContext) -> None:
        source_dur = context.config.get("source_duration", 20)
        target_dur = context.config.get("target_duration", 20)

        try:
            # Source phase
            await self._run_phase(context, Host.SOURCE, "source", source_dur)
            # Target phase
            await self._run_phase(context, Host.TARGET, "target", target_dur)
        except asyncio.CancelledError:
            self._log(context, Host.SOURCE, LogLevel.WARNING, "Dummy job termination requested")
            raise

    async def _run_phase(
        self, context: JobContext, host: Host, phase: str, duration: int
    ) -> None:
        for i in range(duration):
            percent = int((i / duration) * 50) + (50 if phase == "target" else 0)
            self._report_progress(context, ProgressUpdate(percent=percent))
            self._log(context, host, LogLevel.INFO, f"Phase {phase}: tick {i+1}/{duration}")

            if phase == "source" and i == 3:
                self._log(context, host, LogLevel.WARNING, "Test warning at 6s")
            if phase == "target" and i == 4:
                self._log(context, host, LogLevel.ERROR, "Test error at 8s")

            await asyncio.sleep(1)
```

### 4. SSH Command Execution

```python
# src/pcswitcher/executor.py
from __future__ import annotations

import asyncio
from pathlib import Path

import asyncssh

from pcswitcher.models import CommandResult


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
    job = DummySuccessJob()
    errors = await job.validate(job_context)
    assert errors == []
    # Execute would take 40s, mock time or reduce durations
```

## Configuration Example

```yaml
# ~/.config/pc-switcher/config.yaml
log_file_level: FULL
log_cli_level: INFO

sync_jobs:
  dummy_success: true
  dummy_fail: false
  user_data: false
  packages: false

disk:
  preflight_minimum: "20%"
  runtime_minimum: "15%"
  check_interval: 30

btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
  keep_recent: 3
```

## Running pc-switcher

```bash
# Install locally
uv pip install -e .

# Or run directly
uv run pc-switcher sync target-hostname

# View recent logs
uv run pc-switcher logs --last

# Cleanup old snapshots
uv run pc-switcher cleanup-snapshots --older-than 7d
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
