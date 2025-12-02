# Job Interface Contract

**Version**: 1.0.0
**Date**: 2025-11-29
**Reference**: User Story 1, FR-001, FR-002

This document specifies the contract that all pc-switcher jobs must implement. Implementing this contract allows jobs to integrate automatically with the orchestration system.

## Overview

A Job is a discrete sync operation (e.g., package sync, Docker sync, user data sync). Jobs are:
- **Self-contained**: Own their logic, configuration schema, and validation
- **Isolated**: Don't directly interact with other jobs
- **Observable**: Emit logs and progress via EventBus
- **Cancellable**: Handle `asyncio.CancelledError` gracefully

## Class Hierarchy

```text
Job (ABC)
├── SystemJob   → Infrastructure jobs (snapshots, installation) - orchestrator-managed
├── SyncJob     → User data sync jobs (configurable via sync_jobs)
└── BackgroundJob → Concurrent monitoring jobs - orchestrator-managed
```

## Required Class Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `name` | `ClassVar[str]` | Unique identifier (e.g., `"packages"`, `"docker"`) |
| `CONFIG_SCHEMA` | `ClassVar[dict[str, Any]]` | JSON Schema (draft-07) for job-specific config |

**Note on CONFIG_SCHEMA**: The orchestrator accesses job configuration schemas via the `CONFIG_SCHEMA` class attribute directly (not via a method). This is the canonical API for schema access. Jobs may define an empty schema (`{}`) if they have no configuration options.

## Required Methods

### `validate_config(config: dict) -> list[ConfigError]` (classmethod)

**Phase**: 2 (Job Config Validation)
**Called**: Before SSH connection established

Validates job-specific configuration against `CONFIG_SCHEMA`.

```python
@classmethod
def validate_config(cls, config: dict[str, Any]) -> list[ConfigError]:
    """Validate job configuration.

    Args:
        config: Job-specific config from config.yaml

    Returns:
        List of ConfigError for any validation failures.
        Empty list if config is valid.
    """
```

**Implementation Notes**:
- Base class provides default implementation using `jsonschema`
- Override only if additional validation logic needed beyond schema

### `validate() -> list[ValidationError]` (async)

**Phase**: 3 (System State Validation)
**Called**: After SSH connection established, before any state modifications

Validates that system state allows job execution.

```python
async def validate(self) -> list[ValidationError]:
    """Validate system state before execution.

    Returns:
        List of ValidationError for any issues found.
        Empty list if system state is valid.

    Examples:
        - Check required directories exist
        - Verify sufficient disk space
        - Confirm required services running
    """
```

**Implementation Notes**:
- Access executors via `self._context.source` for local checks, `self._context.target` for remote
- Do NOT modify system state in validate()
- Return all errors found (don't short-circuit)

### `execute() -> None` (async)

**Phase**: Execution
**Called**: After all validation passes, in config order

Performs the actual sync operation.

```python
async def execute(self) -> None:
    """Execute sync operation.

    Raises:
        Exception: Any exception halts sync with CRITICAL log
        asyncio.CancelledError: Caught, cleanup performed, re-raised

    Notes:
        - Log progress at appropriate levels
        - Report progress updates for long operations
        - Clean up resources in except CancelledError handler
        - Raise meaningful exceptions (ValueError, RuntimeError, OSError, etc.)
    """
```

## Helper Methods (Provided by Base Class)

### `_log(host, level, message, **extra)`

Log a message through EventBus.

```python
def _log(
    self,
    host: Host,
    level: LogLevel,
    message: str,
    **extra: Any,
) -> None:
    """Emit LogEvent to EventBus.

    Args:
        host: Which machine this log relates to (SOURCE or TARGET)
        level: Log level (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL)
        message: Human-readable message
        **extra: Additional structured context
    """
```

**Usage**:
```python
self._log(Host.SOURCE, LogLevel.INFO, "Starting package comparison")
self._log(Host.TARGET, LogLevel.FULL, "Comparing package", package="nginx")
self._log(Host.TARGET, LogLevel.ERROR, "Package installation failed", error=str(e))
```

### `_report_progress(update)`

Report progress through EventBus.

```python
def _report_progress(
    self,
    update: ProgressUpdate,
) -> None:
    """Emit ProgressEvent to EventBus.

    Args:
        update: ProgressUpdate with percent/current/total/item
    """
```

**Usage**:
```python
# Percentage-based
self._report_progress(ProgressUpdate(percent=45, item="nginx:latest"))

# Count-based with total
self._report_progress(ProgressUpdate(current=45, total=100, item="packages"))

# Count-based without total
self._report_progress(ProgressUpdate(current=45, item="files synced"))

# Heartbeat only
self._report_progress(ProgressUpdate(heartbeat=True))
```

## Cancellation Handling

Jobs MUST handle `asyncio.CancelledError` to clean up resources:

```python
async def execute(self) -> None:
    try:
        # Main execution logic
        await self._do_work()
    except asyncio.CancelledError:
        # Cleanup: terminate remote processes, remove temp files
        self._log(Host.SOURCE, LogLevel.WARNING, f"{self.name} cancelled, cleaning up")
        await self._cleanup()
        raise  # MUST re-raise
```

**Cleanup Guidelines**:
- Terminate processes started via `context.target.start_process()`
- Remove partial/temporary files created during execution
- Release any locks or resources acquired
- Log cleanup actions at WARNING level
- Complete cleanup within the timeout defined by `CLEANUP_TIMEOUT_SECONDS` in cli.py

## Error Handling

| Exception Type | When to Raise | Orchestrator Action |
|---------------|---------------|---------------------|
| Any `Exception` | Sync failure | Log CRITICAL, halt sync |
| `asyncio.CancelledError` | User interrupt (Ctrl+C) | Wait for cleanup, exit 130 |

Jobs should raise meaningful exceptions (`ValueError`, `RuntimeError`, `OSError`, custom exceptions) rather than a generic wrapper. The orchestrator catches all exceptions uniformly at the top level.

## JobContext Reference

```python
@dataclass(frozen=True)
class JobContext:
    config: dict[str, Any]        # Validated job-specific config
    source: LocalExecutor         # Execute on source machine
    target: RemoteExecutor        # Execute on target machine
    event_bus: EventBus           # For logging and progress (use _log() helper)
    session_id: str               # 8-char hex session ID
    source_hostname: str          # Actual source machine name
    target_hostname: str          # Actual target machine name
```

## Example Implementation

```python
from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import (
    ConfigError,
    Host,
    JobContext,
    LogLevel,
    ProgressUpdate,
    ValidationError,
)


class ExampleSyncJob(SyncJob):
    """Example sync job demonstrating the interface contract."""

    name: ClassVar[str] = "example"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "items_to_sync": {
                "type": "array",
                "items": {"type": "string"},
                "default": [],
            },
        },
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__()
        self.context = context

    async def validate(self) -> list[ValidationError]:
        errors = []

        # Check source directory exists
        result = await self._context.source.run_command("test -d /data/example")
        if not result.success:
            errors.append(ValidationError(
                job=self.name,
                host=Host.SOURCE,
                message="/data/example directory not found on source",
            ))

        # Check target directory exists
        result = await self._context.target.run_command("test -d /data/example")
        if not result.success:
            errors.append(ValidationError(
                job=self.name,
                host=Host.TARGET,
                message="/data/example directory not found on target",
            ))

        return errors

    async def execute(self) -> None:
        items = self._context.config.get("items_to_sync", [])
        total = len(items)

        self._log(Host.SOURCE, LogLevel.INFO, f"Syncing {total} items")

        try:
            for i, item in enumerate(items):
                # Report progress
                self._report_progress(ProgressUpdate(
                    percent=int((i / total) * 100),
                    current=i,
                    total=total,
                    item=item,
                ))

                # Do the actual sync
                self._log(Host.TARGET, LogLevel.FULL, f"Syncing item", item=item)
                result = await self._context.target.run_command(f"sync-item {item}")

                if not result.success:
                    raise RuntimeError(f"Failed to sync {item}: {result.stderr}")

            self._report_progress(ProgressUpdate(percent=100))
            self._log(Host.TARGET, LogLevel.INFO, f"Synced {total} items successfully")

        except asyncio.CancelledError:
            self._log(Host.SOURCE, LogLevel.WARNING, "Sync cancelled, cleaning up")
            # Cleanup logic here
            raise
```

## Registration

Jobs are discovered from `sync_jobs` section of config.yaml. The orchestrator:
1. Reads `sync_jobs` to determine which jobs are enabled
2. Imports job classes from `pcswitcher.jobs` module
3. Instantiates enabled jobs
4. Runs lifecycle: `validate_config()` → `validate()` → `execute()`

**Required jobs** (SystemJob, BackgroundJob) run regardless of `sync_jobs` config.

## Testing Checklist

When implementing a new job, verify:

- [ ] `name` class attribute is unique
- [ ] `CONFIG_SCHEMA` validates expected config structure
- [ ] `validate()` checks all prerequisites
- [ ] `execute()` logs at appropriate levels
- [ ] `execute()` reports progress for long operations
- [ ] `CancelledError` handler cleans up resources
- [ ] Meaningful exceptions raised for failures (not bare `Exception`)
- [ ] Job can be tested independently with mocked executors
