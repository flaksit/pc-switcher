# Research: Foundation Infrastructure Complete

**Feature**: Foundation Infrastructure Complete
**Date**: 2025-11-15
**Phase**: Phase 0 - Outline & Research

## Overview

This document resolves all NEEDS CLARIFICATION items identified in the Technical Context of plan.md and provides best practices research for the selected technology stack.

## Research Tasks

### 1. Terminal UI Library: rich vs textual

**Decision**: Use **rich** for terminal UI

**Rationale**:
- **rich** is simpler and better suited for progress bars, status display, and formatted logging output
- **textual** is a full TUI framework (event loops, widgets, layouts) - overkill for our needs
- Our UI requirements are straightforward: progress bars, log output, status messages
- **rich** has better integration with structlog via `rich.logging.RichHandler`
- Lower complexity aligns with "Deliberate Simplicity" principle

**Alternatives Considered**:
- **textual**: Full-featured TUI with event-driven architecture, but adds unnecessary complexity for simple progress reporting
- **tqdm**: Good for progress bars but doesn't handle structured log output or multi-line status displays as elegantly as rich

**Implementation Approach**:
- Use `rich.progress.Progress` for progress bars with multiple tasks (one per module)
- Use `rich.console.Console` for formatted output
- Use `rich.logging.RichHandler` as structlog processor for CLI output
- Use `rich.live.Live` for real-time updates without flicker

### 2. CLI Framework: click vs typer

**Decision**: Use **typer**

**Rationale**:
- **typer** is built on click but provides modern Python type hints for arguments/options
- Better alignment with our type-first approach (basedpyright, full annotations)
- Cleaner code: `def sync(target: str)` vs click decorators
- Automatic help generation from docstrings and type hints
- Same underlying robustness as click (battle-tested)
- Still maintained by the Textualize team (creators of rich/textual)

**Alternatives Considered**:
- **click**: Industry standard, mature, but requires more boilerplate decorators
- **argparse**: Standard library, but less ergonomic than modern alternatives

**Implementation Approach**:
- Use `typer.Typer()` app instance
- Define commands as typed functions: `def sync(target: str, config: Path | None = None)`
- Leverage automatic validation from type hints
- Use typer's rich integration for formatted help output

### 3. Best Practices: Fabric SSH Library

**Key Patterns**:

1. **Connection Management**:
   - Use `fabric.Connection(host)` with context manager: `with Connection(host) as conn:`
   - Enable ControlMaster for persistent connection: `connect_kwargs={'control_path': '/tmp/ssh-%r@%h:%p'}`
   - Single connection reused across all operations (per ADR-002)

2. **Command Execution**:
   - Use `conn.run(command, hide=False)` for streaming output
   - Set `pty=False` for clean stdout/stderr separation
   - Use `warn=True` to capture failures without exceptions (check exit code manually)
   - For sudo: `conn.sudo(command)` or `conn.run(command, sudo=True)`

3. **Output Streaming**:
   - Fabric streams stdout/stderr by default when `hide=False`
   - Parse line-by-line for progress updates: `for line in result.stdout.splitlines()`
   - Target scripts must flush stdout after each progress message: `print(..., flush=True)`

4. **Error Handling**:
   - Check `result.ok` or `result.failed` for command success/failure
   - Inspect `result.exited` for exit code
   - Catch `fabric.exceptions.UnexpectedExit` for critical failures
   - Use `result.stderr` for error messages

5. **File Transfer**:
   - Use `conn.put(local, remote)` for file uploads (e.g., installer package)
   - Use `conn.get(remote, local)` for file downloads (if needed)

**Security Best Practices**:
- Respect `~/.ssh/config` for host aliases, keys, ports (Fabric does this automatically)
- Never embed credentials in code
- Use SSH agent forwarding carefully (not needed for our use case)
- Verify host keys (Fabric uses default known_hosts)

### 4. Best Practices: structlog Structured Logging

**Key Patterns**:

1. **Setup**:
   ```python
   import structlog

   structlog.configure(
       processors=[
           structlog.stdlib.add_log_level,
           structlog.stdlib.add_logger_name,
           structlog.processors.TimeStamper(fmt="iso", utc=True),
           structlog.processors.StackInfoRenderer(),
           structlog.dev.ConsoleRenderer()  # or JSONRenderer for files
       ],
       wrapper_class=structlog.stdlib.BoundLogger,
       logger_factory=structlog.stdlib.LoggerFactory(),
   )
   ```

2. **Custom Log Levels**:
   - structlog doesn't have built-in FULL level
   - We need to define custom level: `FULL = 15` (between DEBUG=10 and INFO=20)
   - Add to standard library: `logging.addLevelName(15, 'FULL')`
   - Create custom method: `structlog.stdlib.BoundLogger.full = lambda self, msg, **kw: self._log(15, msg, **kw)`

3. **Dual Output (File + Terminal)**:
   - Two separate processor chains: one for file (JSON), one for terminal (Console via rich)
   - Use `structlog.PrintLogger` for file output, `rich.logging.RichHandler` for terminal
   - Filter by level in each chain independently (`log_file_level` vs `log_cli_level`)

4. **Context Binding**:
   - Bind module name to logger: `log = log.bind(module="btrfs-snapshots")`
   - Bind session ID: `log = log.bind(session_id=session.id)`
   - Context carries through all subsequent log calls

5. **CRITICAL Abort Integration**:
   - Hook into logging to detect CRITICAL events
   - Use custom processor to set global abort flag: `if level >= CRITICAL: abort_signal.set()`
   - Orchestrator checks abort signal after each module operation

**Implementation Approach**:
- Define 6 log levels with numeric values: DEBUG=10, FULL=15, INFO=20, WARNING=30, ERROR=40, CRITICAL=50
- Configure structlog with dual output: file (JSON, ISO timestamps) + terminal (rich, colored)
- Create logger factory that binds context (module name, session ID, hostname)
- Implement CRITICAL abort via custom processor + threading.Event signal

### 5. Best Practices: uv Package Management and GitHub Actions CI/CD

**uv Project Setup**:

1. **Project Initialization**:
   ```bash
   uv init --lib pc-switcher
   cd pc-switcher
   uv add fabric structlog rich typer pyyaml
   uv add --dev pytest basedpyright ruff codespell
   ```

2. **pyproject.toml Configuration**:
   ```toml
   [project]
   name = "pc-switcher"
   version = "0.1.0"
   requires-python = ">=3.13"
   dependencies = ["fabric", "structlog", "rich", "typer", "pyyaml"]

   [project.scripts]
   pc-switcher = "pcswitcher.cli.main:app"

   [tool.uv]
   dev-dependencies = ["pytest", "basedpyright", "ruff", "codespell"]

   [tool.ruff]
   line-length = 119
   target-version = "py313"

   [tool.basedpyright]
   typeCheckingMode = "standard"
   pythonVersion = "3.13"
   ```

3. **Running Commands**:
   - Install/sync dependencies: `uv sync`
   - Run CLI: `uv run pc-switcher sync <target>`
   - Run tests: `uv run pytest`
   - Type check: `uv run basedpyright`
   - Lint/format: `uv run ruff check` / `uv run ruff format`

**GitHub Actions CI/CD**:

1. **CI Workflow** (`.github/workflows/ci.yml`):
   ```yaml
   name: CI
   on: [push, pull_request]
   jobs:
     test:
       runs-on: ubuntu-24.04
       steps:
         - uses: actions/checkout@v4
         - uses: astral-sh/setup-uv@v5
           with:
             version: "latest"
         - run: uv sync
         - run: uv run ruff check
         - run: uv run basedpyright
         - run: uv run pytest
         - run: uv run codespell
   ```

2. **Release Workflow** (`.github/workflows/release.yml`):
   ```yaml
   name: Release
   on:
     release:
       types: [created]
   jobs:
     publish:
       runs-on: ubuntu-24.04
       permissions:
         contents: read
         packages: write
       steps:
         - uses: actions/checkout@v4
         - uses: astral-sh/setup-uv@v5
         - run: uv build
         - run: uv publish --token ${{ secrets.GITHUB_TOKEN }} --publish-url https://ghcr.io
   ```

**GitHub Package Registry Setup**:
- Packages publish to `ghcr.io/yourusername/pc-switcher`
- Installation: `uv tool install --index-url https://ghcr.io/simple/ pc-switcher`
- Authentication handled via GITHUB_TOKEN in Actions
- Manual upload: `uv build && uv publish --token <PAT>`

### 6. Best Practices: Python Type Hints with basedpyright

**Type Annotation Patterns**:

1. **Module Interface (ABC)**:
   ```python
   from __future__ import annotations
   from abc import ABC, abstractmethod
   from typing import override

   class SyncModule(ABC):
       @abstractmethod
       def validate(self) -> list[str]:
           """Return list of validation errors (empty if valid)"""
           ...

       @abstractmethod
       def sync(self) -> None:
           """Execute sync operation"""
           ...
   ```

2. **Union Types (Modern Syntax)**:
   ```python
   from pathlib import Path

   def load_config(path: Path | None = None) -> dict[str, Any]:
       ...
   ```

3. **Generic Collections**:
   ```python
   from collections.abc import Sequence, Mapping

   def process_modules(modules: Sequence[SyncModule]) -> Mapping[str, bool]:
       ...
   ```

4. **Optional Returns**:
   ```python
   def get_session() -> SyncSession | None:
       ...
   ```

5. **Type Aliases**:
   ```python
   type ModuleName = str
   type ValidationErrors = list[str]

   def validate_module(name: ModuleName) -> ValidationErrors:
       ...
   ```

**basedpyright Configuration**:
- Use `typeCheckingMode = "standard"` for good balance (not "strict" which can be overly pedantic)
- Enable `reportMissingTypeStubs = false` to avoid noise from untyped dependencies
- Use `# pyright: ignore[reportType]` sparingly with explanation

### 7. Best Practices: Btrfs Snapshot Operations

**Snapshot Creation**:
```bash
# Read-only snapshot
sudo btrfs subvolume snapshot -r /home /@home-presync-20251115T120000Z-abc123

# Check if subvolume exists
btrfs subvolume show /home &>/dev/null && echo "exists" || echo "missing"
```

**Snapshot Listing**:
```bash
# List all snapshots
sudo btrfs subvolume list /

# Filter by pattern
sudo btrfs subvolume list / | grep 'presync'
```

**Snapshot Deletion**:
```bash
sudo btrfs subvolume delete /@home-presync-20251115T120000Z-abc123
```

**Disk Space Check**:
```bash
# Show filesystem usage
df -h /

# Show btrfs-specific usage (unallocated space)
sudo btrfs filesystem usage /
```

**Implementation Approach**:
- Use `subprocess.run()` with `capture_output=True` for all btrfs commands
- Parse output for errors (check returncode and stderr)
- Use ISO8601 timestamps with timezone (UTC): `datetime.now(UTC).isoformat()`
- Generate session ID: `uuid.uuid4().hex[:8]`
- Naming pattern: `@{subvolume}-{presync|postsync}-{timestamp}-{session_id}`

### 8. Best Practices: YAML Configuration with PyYAML

**Safe Loading**:
```python
import yaml
from pathlib import Path

def load_config(path: Path) -> dict:
    with path.open('r') as f:
        return yaml.safe_load(f)  # Never use yaml.load() - security risk
```

**Schema Validation**:
- PyYAML doesn't include schema validation
- Manual validation against module-declared schemas
- Alternative: Use `pydantic` for validation (adds dependency but provides robust schema enforcement)

**Decision**: Use manual validation for now (aligns with "Deliberate Simplicity"), consider pydantic if validation logic becomes complex

**Default Config Generation**:
```python
def generate_default_config() -> str:
    config = {
        'log_file_level': 'FULL',
        'log_cli_level': 'INFO',
        'sync_modules': {
            'btrfs_snapshots': True,  # Required, cannot disable
            'dummy_success': False,
            'dummy_critical': False,
            'dummy_fail': False,
        },
        'btrfs_snapshots': {
            'subvolumes': ['/', '/home', '/root'],
            'keep_recent': 3,
            'max_age_days': 7,
        },
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)
```

## Summary

All NEEDS CLARIFICATION items have been resolved:

| Item | Decision | Rationale |
|------|----------|-----------|
| Terminal UI library | **rich** | Simpler than textual, sufficient for progress bars and logs |
| CLI framework | **typer** | Modern type hints, built on click, better ergonomics |

Best practices documented for:
- Fabric SSH operations (connection management, streaming, error handling)
- structlog logging (custom levels, dual output, CRITICAL abort integration)
- uv package management (project setup, commands, CI/CD)
- basedpyright type checking (modern syntax, patterns)
- Btrfs operations (snapshots, disk space checks)
- PyYAML configuration (safe loading, validation approach)

All decisions align with project constitution principles:
- **Reliability**: Proven libraries, robust error handling patterns
- **Simplicity**: Minimal dependencies, clear patterns
- **Proven Tooling**: All libraries widely adopted, actively maintained
- **Documentation**: Clear implementation guidance for each technology
