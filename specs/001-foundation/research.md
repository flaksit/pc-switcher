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

5. **ERROR Level Tracking**:
   - Orchestrator must track if any ERROR-level logs were emitted during sync
   - Use custom processor to set error flag: `if level >= ERROR: session.has_errors = True`
   - Final session state: COMPLETED (no errors) vs FAILED (has errors)
   - CRITICAL logs are no longer used by modules (they raise exceptions instead)

**Implementation Approach**:
- Define 6 log levels with numeric values: DEBUG=10, FULL=15, INFO=20, WARNING=30, ERROR=40, CRITICAL=50
- Configure structlog with dual output: file (JSON, ISO timestamps) + terminal (rich, colored)
- Create logger factory that binds context (module name, session ID, hostname)
- Track ERROR-level logs via custom processor to determine final session state
- Modules raise exceptions for critical failures; orchestrator logs them as CRITICAL

### 5. Best Practices: uv Package Management and GitHub Actions CI/CD

**Important: uv manages Python installation**
- Do NOT assume system Python is available
- uv will install Python into its managed environment
- Never use system Python directly
- All Python operations go through uv

**uv Version Management**:
- Use latest stable version (no pinning required)
- Reproducibility comes from uv.lock file, not uv version
- uv maintains excellent backwards compatibility
- GitHub Actions use latest version from astral-sh/setup-uv

**uv Project Setup**:

1. **Project Initialization** (from within project directory):
   ```bash
   # Install latest stable uv
   curl -LsSf https://astral.sh/uv/install.sh | sh

   # Initialize project (already in pc-switcher directory)
   uv init --lib .

   # uv will install Python 3.13 into managed environment
   uv python install 3.13

   # Add dependencies
   uv add fabric structlog rich typer pyyaml
   uv add --dev pytest basedpyright ruff codespell
   ```

2. **pyproject.toml Configuration** (Dynamic Versioning via GitHub Releases):
   ```toml
   [project]
   name = "pc-switcher"
   dynamic = ["version"]  # Version comes from Git tags, not hardcoded
   description = "Synchronization system for seamless switching between Linux desktop machines"
   authors = [{name = "Flaksit", email = "info@flaksit.org"}]
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

   [tool.uv]
   dev-dependencies = [
       "pytest>=8.0",
       "basedpyright>=1.15",
       "ruff>=0.5",
       "codespell>=2.3",
   ]

   [tool.uv-dynamic-versioning]
   enable = true
   vcs = "git"
   style = "pep440"

   [tool.ruff]
   line-length = 119
   target-version = "py313"

   [tool.basedpyright]
   typeCheckingMode = "standard"
   pythonVersion = "3.13"
   ```

3. **Running Commands**:
   - Install/sync dependencies: `uv sync`
   - Run as tool: `uv tool run pc-switcher sync <target>` or just `pc-switcher sync <target>` if installed
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
         - uses: astral-sh/setup-uv@v5  # Uses latest stable uv
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
       types: [published]
   jobs:
     publish:
       runs-on: ubuntu-24.04
       permissions:
         contents: read
         packages: write
       steps:
         - uses: actions/checkout@v4
           with:
             fetch-depth: 0  # Required to fetch tags for dynamic versioning!
         - uses: astral-sh/setup-uv@v5  # Uses latest stable uv
         - run: uv build  # uv-dynamic-versioning pulls version from git tag
         # No publishing to registry - install directly from Git URL
   ```

**GitHub Repository Distribution** (Updated - NOT using ghcr.io):
- Install directly from GitHub repository using Git URL
- Installation: `uv tool install git+https://github.com/flaksit/pc-switcher@v1.0.0`
- No package registry required - Git tags define versions
- Build artifacts optionally attached to GitHub Releases

**Version Management Workflow**:
1. Develop features, commit code (no version bump in code)
2. When ready to release: Create GitHub Release with tag (e.g., `v1.0.0`)
3. GitHub Actions automatically validates and builds with version extracted from tag
4. Users install directly from Git URL referencing the tag

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
# Read-only snapshot (source is mount point, destination uses flat name)
# Config stores flat name "@home", which maps to mount point /home
sudo btrfs subvolume snapshot -r /home /.snapshots/@home-presync-20251115T120000Z-abc123

# Check if subvolume exists in top-level (use flat name)
btrfs subvolume list / | grep -q '@home' && echo "exists" || echo "missing"
```

**Snapshot Listing**:
```bash
# List all subvolumes (shows flat names like "ID 256 gen 123 top level 5 path @home")
sudo btrfs subvolume list /

# Filter by snapshot pattern
sudo btrfs subvolume list / | grep 'presync'
```

**Snapshot Deletion**:
```bash
# Delete snapshot (path in snapshot directory)
sudo btrfs subvolume delete /.snapshots/@home-presync-20251115T120000Z-abc123
```

**Disk Space Check**:
```bash
# Show filesystem usage
df -h /

# Show btrfs-specific usage (unallocated space)
sudo btrfs filesystem usage /
```

**Implementation Approach**:
- Config stores flat subvolume names (e.g., `"@"`, `"@home"`, `"@root"`) from `btrfs subvolume list /`
- Map flat names to mount points at runtime (e.g., `"@home"` → `/home`)
- Snapshot source uses mount point, destination uses flat name in path
- Use `subprocess.run()` with `capture_output=True` for all btrfs commands
- Parse output for errors (check returncode and stderr)
- Use ISO8601 timestamps with timezone (UTC): `datetime.now(UTC).isoformat()`
- Generate session ID: `uuid.uuid4().hex[:8]`
- Snapshot naming: `{snapshot_dir}/{flat_subvolume_name}-{presync|postsync}-{timestamp}-{session_id}`
- Example: `/.snapshots/@home-presync-20251115T120000Z-abc12345`

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
            'subvolumes': ['@', '@home', '@root'],  # Flat names from "btrfs subvolume list /"
            'snapshot_dir': '/.snapshots',
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
- structlog logging (custom levels, dual output, ERROR tracking for session state)
- uv package management (project setup, dynamic versioning, CI/CD with GitHub releases)
- basedpyright type checking (modern syntax, patterns)
- Btrfs operations (snapshots, disk space checks)
- PyYAML configuration (safe loading, validation approach)

**Key architectural decisions**:
- uv (latest stable, no version pinning)
- No system Python usage - uv manages Python installation
- Dynamic versioning via GitHub releases (no version in code)
- Exception-based error handling (modules raise exceptions, not log CRITICAL)
- ERROR-level logging tracked for final session state determination

All decisions align with project constitution principles:
- **Reliability**: Proven libraries, robust error handling patterns, exception-based critical failures
- **Simplicity**: Minimal dependencies, clear patterns, single source of truth for versions
- **Proven Tooling**: All libraries widely adopted, actively maintained
- **Documentation**: Clear implementation guidance for each technology
