# Coding Conventions

**Analysis Date:** 2026-06-29

## Naming Patterns

**Files:**
- Lowercase with underscores: `executor.py`, `disk_space_monitor.py`, `config_sync.py`
- Module-level `__all__` export lists to declare public API

**Functions:**
- snake_case: `run_command()`, `create_subprocess_shell()`, `get_lock_path()`
- Async functions: same convention, `async def run_command()`
- Private functions: `_prefix_name()` for module-private helpers

**Classes:**
- PascalCase: `LocalExecutor`, `CommandResult`, `BtrfsSnapshotJob`
- StrEnum/IntEnum subclasses: `Host`, `LogLevel`, `SessionStatus`
- Protocol definitions for interfaces: `Executor`, `Process`

**Variables:**
- snake_case for all variables and attributes
- Private attributes: `_prefix` (e.g., `_processes`, `_connection`, `_session_id`)
- Constants: UPPERCASE (e.g., `CLEANUP_TIMEOUT_SECONDS`, `FULL = 15`)
- Type hints in modern syntax: `str | None` not `Optional[str]`, `list[str]` not `List[str]`

**Types:**
- Use modern type syntax: `dict[str, int]`, `list[T]`, `str | None`
- Import from `collections.abc` for protocols: `AsyncIterator`, `Callable`, `Generator`
- Use `@override` decorator when overriding methods
- Frozen dataclasses for immutable data: `@dataclass(frozen=True)`

## Code Style

**Formatting:**
- Tool: ruff (via `uv run ruff format .`)
- Line length: 119 characters (per `ruff.toml`)
- Quote style: double quotes (`"string"`)
- Indentation: 4 spaces

**Linting:**
- Tool: ruff check
- Configured in `ruff.toml`
- Selected rules: E, W, F, I (isort), B, C4, UP, RUF, SIM, PTH, PL, PERF, FURB
- Ignored: PLR0913 (too many arguments), PLR2004 (magic value comparison)

**Type Checking:**
- Tool: basedpyright
- Full type annotations on all function signatures
- Use `# pyright: ignore` sparingly with explanation

## Import Organization

**Order:**
1. `from __future__ import annotations` (first import, always)
2. Standard library: `import asyncio`, `from pathlib import Path`, `from datetime import UTC, datetime`
3. Third-party: `import asyncssh`, `from rich.console import Console`, `import yaml`
4. Local: `from pcswitcher.config import Configuration`, `from pcswitcher.models import CommandResult`

**Path Aliases:**
- First-party module: `pcswitcher` (defined in `ruff.toml` as known-first-party)
- No relative imports

**Barrel Files:**
- Public APIs exported via `__all__` in module `__init__.py` and main modules
- Example from `src/pcswitcher/connection.py`: `__all__ = ["Connection"]`
- Example from `src/pcswitcher/models.py`: large `__all__` list of exported types

## Error Handling

**Patterns:**
- Raise specific exception types with descriptive messages
- Example: `raise RuntimeError("Not connected to target")` in `connection.py:51`
- Custom exceptions inherit from base types: `DiskSpaceCriticalError(Exception)` with `__init__` storing context
- Configuration errors wrapped: `ConfigurationError(errors)` aggregates `ConfigError` instances
- Handle TimeoutError explicitly: catch `asyncio.wait_for()` timeout, terminate process, re-raise

**Assertions:**
- Use `AssertionError` with descriptive message for invariant violations
- Example: `assert self._conn is not None` with clear context

**Error Messages:**
- Include context: variable values, paths, expected vs actual
- Example: `f"Configuration file not found: {path}"` provides the actual path

## Logging

**Framework:** stdlib `logging`

**Patterns:**
- Get logger: `logging.getLogger("pcswitcher.orchestrator")`
- Use levels: DEBUG, FULL (15, custom), INFO, WARNING, ERROR, CRITICAL
- Log with context using `extra` dict: `logger.info("msg", extra={"job": "job_name", "host": host})`
- JSON-formatted logs to file via `JsonFormatter` in `logger.py`
- TUI/console logs via `RichFormatter`

**Custom Level:**
- FULL (15): between DEBUG (10) and INFO (20) for operational details
- Added via `logging.addLevelName(FULL, "FULL")`

## Comments

**When to Comment:**
- Non-obvious design decisions: "Connection uses keepalive to detect failures proactively"
- Workarounds and their reasoning: reference GitHub issues or ADRs
- Complex logic or subtle edge cases: explain the reasoning
- Changes to other files: "If you change this format, also update version.py:find_one_version()"

**JSDoc/TSDoc:**
- Module-level docstring explains purpose
- Class docstring: responsibilities and key behavior
- Function docstring (Google style):
  - One-line summary
  - Args: parameter names and types (redundant with signature, but document semantics)
  - Returns: what is returned and its type
  - Raises: exceptions that may be raised

**Example** from `connection.py:29-37`:
```python
def __init__(
    self,
    target: str,
    event_bus: EventBus,
    max_sessions: int = 10,
    keepalive_interval: int = 15,
    keepalive_count_max: int = 3,
) -> None:
    """Initialize connection parameters.

    Args:
        target: Hostname or SSH config alias for target machine
        event_bus: EventBus for publishing connection events
        max_sessions: Maximum concurrent SSH sessions (default 10)
        keepalive_interval: Seconds between keepalive packets (default 15)
        keepalive_count_max: Max missed keepalives before disconnect (default 3)
    """
```

## Function Design

**Size:** Focus on single responsibility; typical functions 10-50 lines

**Parameters:**
- Name descriptively: `target` not `t`, `hostname` not `h`
- Type hint all parameters: `cmd: str`, `timeout: float | None = None`
- Keyword-only for optional parameters: `def method(required, *, optional: bool = False)`
- Use dataclass instead of many parameters when groups are related

**Return Values:**
- Always type hint return: `-> CommandResult`, `-> None`, `-> AsyncIterator[str]`
- Return objects (dataclasses) instead of tuples when multiple values
- async functions return `Coroutine`: `async def method() -> ResultType`

## Module Design

**Exports:**
- Define `__all__` at module level listing public API
- Place after imports, before implementations
- Example: `__all__ = ["Connection"]` in `connection.py:11`

**Organization:**
- Module docstring describing purpose
- Protocol definitions first (if any)
- Main class definitions
- Helper functions and utilities
- Implementation of protocols at end

**File-Module Boundaries:**
- One primary class per file is typical: `connection.py` exports `Connection`
- Related types may coexist: `models.py` exports many dataclasses/enums
- Large features → subdirectory: `src/pcswitcher/jobs/` contains job implementations

## Additional Patterns

**Frozen Dataclasses:**
- Use `@dataclass(frozen=True)` for immutable value objects
- Example: `CommandResult`, `ConfigError`, `ProgressUpdate`, `Snapshot`
- Frozen dataclasses are hashable and can be used in sets/dicts

**Protocol Classes:**
- Define implicit interfaces as Protocols (no ABC inheritance needed)
- Example: `Executor` protocol in `executor.py:26` defines contract for local and remote executors
- Both `LocalExecutor` and `RemoteExecutor` implement without explicit inheritance

**String Enums:**
- Use `StrEnum` for string-based enums: `class Host(StrEnum): SOURCE = "source"`
- Use `IntEnum` for integer-based enums: `class LogLevel(IntEnum): DEBUG = 10`
- Enums are comparable and can be serialized directly

**Type: Context Managers:**
- Use async context managers for resource cleanup: `async with connection.start_sftp_client() as sftp:`
- Implement via `__aenter__` and `__aexit__` or use `asynccontextmanager` decorator

**Private vs Public:**
- Module-private: prefix with `_` (e.g., `_load_schema()`, `_parse_log_config()`)
- Do not expose in `__all__`
- Underscore-prefixed attributes: implementation details clients should not access

---

*Convention analysis: 2026-06-29*
