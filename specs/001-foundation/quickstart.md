# Quick Start: Foundation Infrastructure Development

**Feature**: Foundation Infrastructure Complete
**Date**: 2025-11-15
**Phase**: Phase 1 - Design & Contracts

## Overview

This guide provides developers with quick-start instructions for understanding and implementing the foundation infrastructure. It assumes you've read the spec.md and are ready to start coding.

## Prerequisites

- Ubuntu 24.04 LTS with btrfs filesystem
- `uv 0.9.9` installed (version pinned in `.tool-versions` is the single source of truth)
  - Install with: `curl -LsSf https://astral.sh/uv/0.9.9/install.sh | sh`
  - Verify version: `uv --version` should show `0.9.9`
- uv will install Python 3.13 automatically (no system Python needed)
- SSH access to a test target machine
- Basic familiarity with Python, SSH, and btrfs

## Project Setup

### 1. Initialize Project Structure

```bash
# Clone repository (or create new one)
cd pc-switcher

# Ensure Python 3.13 is available (uv installs it automatically)
uv python install 3.13

# Initialize uv project (already in directory)
uv init --lib .

# Add dependencies
uv add fabric structlog rich typer pyyaml

# Add dev dependencies
uv add --dev pytest basedpyright ruff codespell

# Sync dependencies
uv sync
```

### 2. Create Source Layout

Follow the structure in `plan.md` → Project Structure → Source Code:

```bash
mkdir -p src/pcswitcher/{cli,core,remote,modules,utils}
mkdir -p tests/{unit,integration,e2e}
mkdir -p scripts/target
touch src/pcswitcher/__init__.py
# ... create other __init__.py files
```

### 3. Configure pyproject.toml

```toml
[project]
name = "pc-switcher"
dynamic = ["version"]  # Version from Git tags via uv-dynamic-versioning
description = "Synchronization system for seamless switching between Linux desktop machines"
authors = [{name = "Your Name", email = "your.email@example.com"}]
requires-python = ">=3.13"
dependencies = [
    "fabric>=3.2",
    "structlog>=24.1",
    "rich>=13.7",
    "typer>=0.12",
    "pyyaml>=6.0",
]
readme = "README.md"
license = {text = "MIT"}

[project.scripts]
pc-switcher = "pcswitcher.cli.main:app"

[build-system]
requires = ["hatchling", "uv-dynamic-versioning"]
build-backend = "hatchling.build"

[tool.uv-dynamic-versioning]
enable = true
vcs = "git"
style = "pep440"

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "basedpyright>=1.15",
    "ruff>=0.5",
    "codespell>=2.3",
]

[tool.ruff]
line-length = 119
target-version = "py313"

[tool.basedpyright]
typeCheckingMode = "standard"
pythonVersion = "3.13"
```

## Key Contracts and Interfaces

### Module Interface

The `SyncModule` ABC is defined in `contracts/module-interface.py`. All sync features implement this interface.

**Constructor**:
- `__init__(config: dict[str, Any], remote: RemoteExecutor)` - Receives validated config and remote executor

**Required methods**:
- `validate() -> list[str]` - Pre-sync checks (read-only, no state changes)
- `pre_sync() -> None` - Setup operations (e.g., create snapshots)
- `sync() -> None` - Main sync logic (transfer data, install packages)
- `post_sync() -> None` - Finalization (e.g., post-snapshots, verification)
- `abort(timeout: float) -> None` - Stop processes, free resources (best-effort)
- `get_config_schema() -> dict[str, Any]` - JSON Schema for config validation

**Required properties**:
- `name: str` - Unique identifier (e.g., "btrfs-snapshots")
- `required: bool` - Can be disabled? (False for optional modules)

**Injected methods** (orchestrator provides after instantiation):
- `log(level: LogLevel, message: str, **context)` - Structured logging
- `emit_progress(percentage: float | None, item: str, eta: timedelta | None)` - Progress reporting (0.0-1.0)

**Key Changes from Original Design**:
- Modules raise `SyncError` for critical failures (orchestrator logs as CRITICAL)
- `RemoteExecutor` injected for target communication (abstracts SSH)
- No `version` or `dependencies` properties (sequential execution in config order)
- `abort()` replaces `cleanup()` - only called on running module, means "stop" not "undo"
- Progress is optional float 0.0-1.0 (not int 0-100)

See `contracts/module-interface.py` for complete interface and `DummySuccessModule` reference implementation.

### Configuration Schema

Config lives at `~/.config/pc-switcher/config.yaml`. See `contracts/config-schema.yaml` for complete structure.

**Key sections**:
```yaml
log_file_level: FULL
log_cli_level: INFO

sync_modules:
  btrfs_snapshots: true  # Required, must be first
  dummy_success: false

btrfs_snapshots:
  subvolumes: ["@", "@home", "@root"]  # Flat names from "btrfs subvolume list /"
  snapshot_dir: "/.snapshots"
  keep_recent: 3
  max_age_days: 7
```

### Orchestrator-Module Protocol

See `contracts/orchestrator-module-protocol.md` for complete lifecycle sequence, error handling, logging protocol, and progress reporting.

**Key points**:
- Modules execute sequentially in config file order (no dependency resolution)
- Lifecycle: validate → pre_sync → sync → post_sync → [abort if error/interrupt]
- Exception-based errors: modules raise `SyncError`, orchestrator logs as CRITICAL and aborts
- Orchestrator watches ERROR logs to set `session.has_errors` flag (determines COMPLETED vs FAILED)
- Single SSH connection with ControlMaster reused across all operations
- Lock file prevents concurrent syncs: `$XDG_RUNTIME_DIR/pc-switcher/pc-switcher.lock`

## Implementation Order

Based on task dependencies and risk reduction:

### Phase 1: Core Infrastructure (Week 1-2)

1. **Logging system** (`core/logging.py`)
   - Configure structlog with dual output (file + terminal)
   - Define 6 custom log levels (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL)
   - Implement ERROR tracking processor (for session.has_errors flag)
   - Write unit tests

2. **Configuration system** (`core/config.py`)
   - YAML loading with PyYAML
   - Schema validation (manual or jsonschema)
   - Default value application
   - Write unit tests

3. **Module interface** (`core/module.py`)
   - Define `SyncModule` ABC
   - Implement base class with logging and progress reporting
   - Write unit tests for base class

4. **Session management** (`core/session.py`)
   - Implement `SyncSession` state machine
   - Lock mechanism (file-based)
   - Write unit tests

### Phase 2: Remote Operations (Week 2-3)

5. **SSH connection** (`remote/connection.py`)
   - Fabric connection wrapper with ControlMaster
   - Command execution with streaming output
   - Error handling and reconnect logic
   - Write integration tests (requires test target)

6. **Target installer** (`remote/installer.py`)
   - Version detection on target
   - Package installation/upgrade logic
   - Write integration tests

### Phase 3: Orchestration (Week 3-4)

7. **Orchestrator** (`core/orchestrator.py`)
   - Module loading from config (sequential execution in order)
   - RemoteExecutor injection (wraps TargetConnection)
   - Lifecycle execution (validate → pre → sync → post → abort if error)
   - Exception catching and CRITICAL logging
   - SIGINT handling (`core/signals.py`)
   - Write integration tests with dummy modules

### Phase 4: Modules (Week 4-5)

8. **Btrfs snapshots module** (`modules/btrfs_snapshots.py`)
   - Snapshot creation (pre/post)
   - Rollback capability
   - Disk space monitoring (`utils/disk.py`)
   - Cleanup command
   - Write integration tests (requires btrfs)

9. **Dummy modules** (`modules/dummy_*.py`)
   - `dummy_success`: Complete reference implementation
   - `dummy_critical`: CRITICAL abort testing
   - `dummy_fail`: Exception handling testing
   - Write unit tests

### Phase 5: CLI & UI (Week 5-6)

10. **CLI commands** (`cli/main.py`)
    - `pc-switcher sync <target>`
    - `pc-switcher logs --last`
    - `pc-switcher cleanup-snapshots --older-than 7d`
    - Write integration tests

11. **Terminal UI** (`cli/ui.py`)
    - Rich integration for progress bars
    - Real-time log display
    - Write UI tests (manual or automated)

### Phase 6: Testing & Polish (Week 6-7)

12. **End-to-end tests** (`tests/e2e/`)
    - Complete sync flow with test machines
    - Interrupt handling (Ctrl+C simulation)
    - Error scenarios (CRITICAL, exceptions)

13. **CI/CD setup** (`.github/workflows/`)
    - CI workflow: lint, type check, test
    - Release workflow: build, publish to GitHub Package Registry

14. **Documentation** (`README.md`, user docs)
    - Installation instructions
    - Configuration guide
    - Troubleshooting

## Development Commands

```bash
# Run type checker
uv run basedpyright

# Run linter
uv run ruff check

# Auto-fix linter issues
uv run ruff check --fix

# Format code
uv run ruff format

# Run tests
uv run pytest

# Run specific test
uv run pytest tests/unit/test_logging.py

# Check for typos
uv run codespell

# Run CLI locally (uv runs in project's virtual environment)
uv run pc-switcher sync test-target

# Install as a tool for system-wide access
uv tool install --editable .
```

## Testing Strategy

### Unit Tests
- Mock external dependencies (SSH, file system, btrfs commands)
- Test individual components in isolation
- Fast execution (no network or disk I/O)

**Example**: `tests/unit/test_module.py`
```python
from pcswitcher.core.module import SyncModule
from pcswitcher.modules.dummy_success import DummySuccessModule
from unittest.mock import Mock

def test_module_validation():
    config = {"duration_seconds": 10}
    remote = Mock(spec=RemoteExecutor)  # Mock RemoteExecutor
    module = DummySuccessModule(config, remote)

    # Inject mocked methods (normally done by orchestrator)
    module.log = Mock()
    module.emit_progress = Mock()

    errors = module.validate()
    assert errors == []  # No validation errors
```

### Integration Tests
- Use real SSH connections to test machines
- Use real btrfs operations (on test subvolumes)
- Slower execution but validates actual behavior

**Example**: `tests/integration/test_ssh_connection.py`
```python
from pcswitcher.remote.connection import TargetConnection

def test_command_execution(test_target_hostname):
    conn = TargetConnection(test_target_hostname)
    conn.connect()

    result = conn.run("echo 'hello'")
    assert result.ok
    assert result.stdout.strip() == "hello"

    conn.disconnect()
```

### End-to-End Tests
- Full sync flow from source to target
- Requires two test machines (or VMs)
- Tests complete workflow including UI

**Example**: `tests/e2e/test_sync_flow.py`
```python
def test_complete_sync(source_machine, target_machine):
    # Setup: ensure clean state
    # Execute: run sync command
    # Verify: check snapshots created, logs written, target updated
    pass
```

## Common Patterns

### Logging
```python
from pcswitcher.core.logging import get_logger

logger = get_logger(__name__)
logger.info("Operation started", file_count=42)
logger.warning("Unexpected condition", path="/some/path")
logger.critical("Unrecoverable error", error=str(e))
```

### Progress Reporting (in modules)
```python
def sync(self):
    for i, item in enumerate(items):
        # Progress as float 0.0-1.0 representing total module work
        percentage = (i + 1) / len(items)
        self.emit_progress(percentage, f"Processing {item}")
        # ... do work
```

### Remote Execution (via RemoteExecutor)
```python
# Execute command on target (RemoteExecutor injected in constructor)
result = self.remote.run("btrfs subvolume list /", sudo=True)
if result.returncode != 0:
    self.log(LogLevel.ERROR, "Failed to list subvolumes", stderr=result.stderr)

# Send file to target
self.remote.send_file_to_target(Path("/local/file"), Path("/remote/file"))

# Get target hostname
hostname = self.remote.get_hostname()  # e.g., "workstation"
```

### Error Handling
```python
from pcswitcher.core.module import SyncError

# Recoverable errors: log ERROR and continue
try:
    process_file(file)
except FileNotFoundError as e:
    self.log(LogLevel.ERROR, "File missing, skipping", path=file, error=str(e))
    # Continue to next file

# Critical failures: raise SyncError (orchestrator logs as CRITICAL and aborts)
result = self.remote.run("critical-command", sudo=True)
if result.returncode != 0:
    raise SyncError(f"Critical command failed: {result.stderr}")
```

## Troubleshooting Development Issues

**Type checker errors**: Ensure `from __future__ import annotations` at top of file

**Import errors**: Run `uv sync` to ensure dependencies are installed

**SSH connection fails**: Check `~/.ssh/config` for target hostname, verify SSH key access

**Btrfs commands fail**: Ensure running on btrfs filesystem, check sudo permissions

**Tests fail**: Check test fixtures, ensure test machines are accessible

## Next Steps

1. Read `spec.md` for complete requirements
2. Review `contracts/` for interface definitions
3. Study `data-model.md` for entity relationships
4. Start implementing in order: logging → config → module interface → orchestrator
5. Write tests alongside implementation (TDD recommended)
6. Use dummy modules for testing orchestrator without real sync operations

## Resources

- **Fabric docs**: https://docs.fabfile.org/
- **structlog docs**: https://www.structlog.org/
- **rich docs**: https://rich.readthedocs.io/
- **typer docs**: https://typer.tiangolo.com/
- **btrfs man pages**: `man btrfs`, `man btrfs-subvolume`
- **Python type hints**: https://docs.python.org/3/library/typing.html
