# Coding Conventions

**Analysis Date:** 2026-07-23

Authoritative project rules live in `docs/dev/development-guide.md` and `docs/dev/testing-guide.md`. This document records the conventions actually observed in `src/pcswitcher/` and `tests/`.

## Naming Patterns

**Files:**
- Modules: `snake_case.py`, one domain concept per module — `src/pcswitcher/btrfs_snapshots.py`, `src/pcswitcher/sync_history.py`
- Jobs live in `src/pcswitcher/jobs/` and are named after the sync unit: `apt_sync.py`, `folder_sync.py`, `install_on_target.py`
- Tests mirror the module: `tests/unit/jobs/test_folder_sync.py` for `src/pcswitcher/jobs/folder_sync.py`

**Functions:**
- `snake_case`. Async I/O functions are `async def` and named as verbs: `run_command`, `start_process`, `create_snapshot`
- Module-private helpers are prefixed with `_`: `_write`, `_full` (`src/pcswitcher/logger.py:28`)

**Variables:**
- `snake_case` for locals; `UPPER_SNAKE` for module constants (`FULL = 15` in `src/pcswitcher/logger.py:25`)
- Module-level loggers: either `logger` or `_logger`. Both appear — prefer `logger` for new modules (`src/pcswitcher/version.py:31`, `src/pcswitcher/jobs/base.py:16`)

**Types:**
- `PascalCase` classes. Job classes end in `Job` (`InstallOnTargetJob`, `DiskSpaceMonitorJob`)
- Exceptions end in `Error` (`ConfigurationError`, `SyncLockedError`, `UpdateFailedError`) except intentional control-flow signals (`SyncAbortedByUser` in `src/pcswitcher/models.py:131`)
- Class-level job metadata uses `ClassVar`: `name`, `required`, `CONFIG_SCHEMA` (`src/pcswitcher/jobs/base.py:32`)

## Code Style

**Formatting:**
- `ruff format` (config: `ruff.toml`)
- `line-length = 119`, double quotes, space indent, magic trailing comma respected
- Run: `uv run ruff check . && uv run ruff format .`

**Linting:**
- `ruff` with rule sets `E, W, F, I, B, C4, UP, RUF, SIM, PTH, PL, PERF, FURB`
- Ignored: `PLR0913` (too many args), `PLR2004` (magic value comparison)
- `PTH` is on: use `pathlib.Path`, never `os.path`

**Type checking:**
- `basedpyright` in **strict** mode over `src` and `tests` (`pyrightconfig.json`)
- `pythonVersion = 3.14`, `pythonPlatform = Linux`
- `reportUnknown*` relaxed; `reportPrivateUsage` off inside `tests/`
- Every function must be annotated, including `-> None`. Avoid `Any`; where an escape hatch is needed use a targeted `# pyright: ignore[ruleName]` with the rule named (`src/pcswitcher/logger.py:34`, `tests/conftest.py:16`)

**Spell check:**
- `uv run codespell` runs in CI (`.github/workflows/ci.yml`)

## Import Organization

**Order** (enforced by ruff `I` with `known-first-party = ["pcswitcher"]`):
1. `from __future__ import annotations` — first line of every module, no exceptions
2. Standard library
3. Third-party (`asyncssh`, `rich`, `typer`, `jsonschema`, `yaml`)
4. First-party `pcswitcher.*` absolute imports
5. Relative imports (used only inside the `jobs` package: `from .context import JobContext`)

**Type-only imports:**
- Guard import cycles and heavy modules with `if TYPE_CHECKING:` (`src/pcswitcher/jobs/base.py:19`)

**Path aliases:**
- None. Source layout is `src/`, package installed via `uv`; always use absolute `pcswitcher.*` imports outside the package-internal relative case above.

## Public API Declaration

Modules with a public surface declare `__all__` sorted alphabetically — `src/pcswitcher/models.py:10`, `src/pcswitcher/logger.py:36`. Add new public names there when extending those modules.

## Error Handling

**Patterns:**
- Domain exceptions defined near their owner: `ConfigurationError` (`src/pcswitcher/config.py:221`), `SyncLockedError`/`SyncAbortedByUser`/`DiskSpaceCriticalError` (`src/pcswitcher/models.py`), `UpdateFailedError`/`UpgradeNotStartedError` (`src/pcswitcher/cli.py:531`), `ConvergeItemFailed`/`PackageItemFailures` (`src/pcswitcher/jobs/package_sync_core.py:64`)
- Commands return values, not exceptions: `CommandResult(exit_code, stdout, stderr)` (frozen dataclass, `src/pcswitcher/models.py:52`) with a `.success` property. Check the result; do not wrap every command in try/except
- Validation collects rather than raises: `Job.validate_config()` returns `list[ConfigError]`, `Job.validate()` returns `list[ValidationError]` (`src/pcswitcher/jobs/base.py`). Empty list = valid
- Re-raise with cause: `raise typer.Exit(1) from e` at the CLI boundary; never swallow the original
- `asyncio.CancelledError` is caught explicitly for graceful cancellation and re-raised or converted to an aborted job status — never treated as a generic failure
- Broad `except Exception` is reserved for top-level orchestration/CLI boundaries where a failure must be reported rather than crash the run

**Rule:** validate inputs up front in `validate()` with actionable, copy-pasteable remediation text rather than failing mid-execution.

## Logging

**Framework:** stdlib `logging`, configured in `src/pcswitcher/logger.py` (queue-based handler + Rich rendering + JSON file formatter).

**Patterns:**
- One module-level logger per module, named under the `pcswitcher.` hierarchy: `logging.getLogger("pcswitcher.jobs.package_state")`
- Custom level `FULL = 15` (between DEBUG and INFO) for file-level operational detail; call `logger.full(...)`
- Level semantics are defined by `LogLevel` in `src/pcswitcher/models.py:36` — DEBUG internals, FULL per-file detail, INFO high-level operations, WARNING non-fatal, ERROR recoverable, CRITICAL abort
- Never pass untrusted/log text straight into a Rich `Panel` or markup-parsing renderable — wrap in `rich.text.Text`

## Comments and Docstrings

**Docstrings:**
- Every module, class, and public function has one. Google style with `Args:` / `Returns:` sections (`src/pcswitcher/jobs/base.py:56-66`)
- Test docstrings state the expected behavior, and for spec-driven tests begin with the requirement ID: `"""CORE-FR-VERSION-CHECK: System must ..."""`

**Comments:**
- Explain non-obvious decisions and constraints only (e.g. why root logger stays at WARNING in `tests/conftest.py:22`)
- Never restate names or types; never narrate change history

## Function and Module Design

**Functions:**
- Small and single-purpose. `PLR0913` (arg count) is deliberately disabled because config-carrying constructors need many parameters
- Prefer frozen `@dataclass` for value objects (`CommandResult`, `Snapshot`, `ProgressUpdate` in `src/pcswitcher/models.py`)
- Enums: `StrEnum` for identifiers (`Host`), `IntEnum` where ordering matters (`LogLevel`)

**Jobs:**
- Subclass `Job` (`src/pcswitcher/jobs/base.py`) and implement `validate()` and `execute()`; declare `name`, optional `required`, and a JSON-Schema `CONFIG_SCHEMA` validated with `jsonschema.Draft7Validator`
- Use `self.source` / `self.target` executor shortcuts rather than reaching into `self.context`
- Publish progress via `ProgressEvent` on the event bus rather than printing

**Exports:**
- No barrel re-export sprawl; `src/pcswitcher/jobs/__init__.py` re-exports the job interface (`Job`, `JobContext`) only.

## Tooling Discipline

- Always `uv run <tool>`. Never bare `python`, `python3`, or `pip`
- Requires Python >= 3.14
- Conventional commit prefixes: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`
