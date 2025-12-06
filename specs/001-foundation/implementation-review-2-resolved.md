# Implementation Review 2 Resolution Report

**Review file**: `implementation-review-2-codex.md`
**Resolved by**: Claude (Opus 4.5)
**Date**: 2025-12-01

## Overriding Instructions

The user provided the following clarification before resolution began:

> We don't use PyPi. Always install from GitHub.
> I see the implementation did not follow the clear instructions given in chapter "Installation Script" of @specs/001-foundation/architecture.md. Fix this. That document is the bible.

This clarified the approach for Finding 5: installation should always use GitHub via `install.sh`, never PyPI.

## Conversation Log

### My Question

Before I proceed to fix the implementation, I have one question:

**Regarding Finding 5 (installation from GitHub):**

The spec (FR-005, User Story 2 Scenario 1) states:
> "installs pc-switcher version X on target from GitHub repository using `uv tool install git+https://github.com/.../pc-switcher@v<version>`"

However, `install.sh` currently installs from PyPI using `uv tool install pcswitcher`. The review asks me to install from GitHub release URL to match FR-005.

**Question:** Should I:
1. Change both `install.sh` AND `install_on_target()` to install from GitHub (e.g., `uv tool install git+https://github.com/flaksit/pc-switcher@v{version}`)?
2. Or keep `install.sh` installing from PyPI (for initial install) and only use GitHub for target installs during sync?

### User's Answer

> We don't use PyPi. Always install from GitHub.
> I see the implementation did not follow the clear instructions given in chapter "Installation Script" of @specs/001-foundation/architecture.md. Fix this. That document is the bible.

### Follow-up Correction

After initial fixes, the user pointed out that the architecture.md defines `InstallOnTargetJob` as a proper `SystemJob` class, but the implementation was using functions. The user requested:

> I can't find a InstallOnTargetJob anywhere. Refactor the code for the installation on target so it follows strictly the architecture that was defined.

This led to a complete refactoring to create the `InstallOnTargetJob` class per the architecture specification.

---

## Findings Resolution Summary

All 6 findings from the review have been addressed:

### Finding 1 (Critical) - Logging & progress infrastructure not wired up

**Status**: RESOLVED

**Changes made to `src/pcswitcher/orchestrator.py`**:
- Added imports for `ConsoleLogger`, `FileLogger`, `generate_log_filename`, `get_logs_directory`
- Added instance fields for logger objects and their background tasks
- In `run()` method: create log file path, subscribe to EventBus, instantiate FileLogger and ConsoleLogger, start consumer tasks
- In `_cleanup()` method: await logger tasks after EventBus.close() to ensure all events are flushed

**Result**: Log messages now appear in terminal (at CLI log level) and log files are created at `~/.local/share/pc-switcher/logs/sync-{timestamp}-{session_id}.log`.

---

### Finding 2 (Critical) - Btrfs snapshot configuration mismatch

**Status**: RESOLVED

**Problem**: Config defined `subvolumes: list[str]` but job expected objects with `name` and `mount_point` fields.

**Changes made to `src/pcswitcher/jobs/btrfs.py`**:
- Added helper function `subvolume_to_mount_point(subvol_name: str) -> str` that derives mount points from subvolume names:
  - `@` → `/`
  - `@home` → `/home`
  - `@var` → `/var`
  - etc.
- Updated `CONFIG_SCHEMA` to expect `list[str]` items
- Updated `validate()` and `execute()` to use simple strings and derive mount points

**Result**: Btrfs snapshots now work with config format `subvolumes: ["@", "@home"]`.

---

### Finding 3 (Major) - Snapshot session organization & cleanup

**Status**: RESOLVED

**Problem**: `session_folder_name()` generated fresh timestamp each call, causing PRE and POST snapshots to land in different folders.

**Changes made**:

1. `src/pcswitcher/orchestrator.py`:
   - Generate session folder name ONCE in `__init__` and store as `self._session_folder`
   - Pass session folder to BtrfsSnapshotJob via config

2. `src/pcswitcher/jobs/btrfs.py`:
   - Accept `session_folder` from config instead of generating new one

3. `src/pcswitcher/cli.py`:
   - Implemented `cleanup_snapshots` command to actually call `snapshots.cleanup_snapshots()`
   - Added config loading for retention settings
   - Added proper output formatting

**Result**: PRE and POST snapshots go into the same session folder; cleanup-snapshots CLI command works.

---

### Finding 4 (Major) - Disk-space safeguards omit preflight check

**Status**: RESOLVED

**Changes made to `src/pcswitcher/orchestrator.py`**:
- Added `_check_disk_space_preflight()` method that:
  - Checks both source and target in parallel
  - Parses threshold (percentage or absolute)
  - Compares against `preflight_minimum` from config
  - Logs CRITICAL and raises RuntimeError if below threshold
- Called this method AFTER validation, BEFORE creating snapshots (Phase 5.5)

**Result**: Sync aborts with CRITICAL error if disk space is below preflight_minimum on either host.

---

### Finding 5 (Major) - Version parity and installation

**Status**: RESOLVED

**Changes made**:

1. `src/pcswitcher/cli.py`:
   - Added `--version` / `-v` flag using Typer callback
   - Prints `pc-switcher {version}` and exits

2. **Created `src/pcswitcher/jobs/install_on_target.py`** (per architecture.md):
   - `InstallOnTargetJob` class extending `SystemJob`
   - `validate()` method (minimal, returns empty list)
   - `execute()` method implementing exact logic from architecture.md:
     - Checks target version via `pc-switcher --version`
     - Returns early if versions match
     - Raises error if target > source
     - Installs/upgrades using GitHub `install.sh`:
       - URL: `https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/v{version}/install.sh`
       - Command: `curl -LsSf {url} | sh -s -- --version {version}`
     - Verifies installation succeeded

3. **Refactored `src/pcswitcher/orchestrator.py`**:
   - Removed `_check_version_compatibility()` method (logic moved to job)
   - Removed `_install_on_target()` method (logic moved to job)
   - Removed `self._source_version`, `self._target_version`, `self._install_needed` fields
   - Added `_install_on_target_job()` method that creates job context and executes the job
   - InstallOnTargetJob runs AFTER pre-sync snapshots (Phase 7) for rollback safety

4. Updated `src/pcswitcher/jobs/__init__.py`:
   - Added `InstallOnTargetJob` to exports

**Result**: `pc-switcher --version` works, installation uses proper `InstallOnTargetJob` SystemJob class per architecture.md, installs from GitHub via `install.sh`.

---

### Finding 6 (Minor) - `pc-switcher logs --last` doesn't display content

**Status**: RESOLVED

**Changes made to `src/pcswitcher/cli.py`**:
- Added `_display_log_file()` helper function that:
  - Parses JSON log lines
  - Formats entries with Rich (colored by log level)
  - Handles malformed lines gracefully
- Updated `logs --last` to call `_display_log_file()` instead of just printing path

**Result**: `pc-switcher logs --last` displays formatted, color-coded log content.

---

## Verification

- **Type checking**: `uv run basedpyright src/pcswitcher/` → 0 errors, 0 warnings
- **Linting**: `uv run ruff check src/pcswitcher/` → 1 pre-existing stylistic issue (late import)
- **CLI test**: `uv run pc-switcher --version` → `pc-switcher 0.0.0.post125.dev0+09ab5f5`

## Files Modified

1. `src/pcswitcher/orchestrator.py` - Logging wiring, session folder, preflight check, InstallOnTargetJob integration
2. `src/pcswitcher/jobs/btrfs.py` - Config structure alignment, session folder from config
3. `src/pcswitcher/cli.py` - Version flag, logs --last display, cleanup-snapshots command
4. `src/pcswitcher/version.py` - Version utilities: `get_this_version()` and `parse_version_from_cli_output()`

## Files Created

1. `src/pcswitcher/jobs/install_on_target.py` - InstallOnTargetJob SystemJob per architecture.md (contains all installation logic)

## Code Consolidation

After initial fixes, duplicated code was identified and refactored:
- Created `version.py` with `get_this_version()` and `parse_version_from_cli_output()` utilities
- All installation logic moved to `InstallOnTargetJob`
- All installation/upgrade logic lives in `InstallOnTargetJob` (architecture-compliant)
- Removed dead code: `get_target_version()`, `compare_versions()`, `install_on_target()`, `check_and_install()`, `InstallationError`

---

## Follow-up: Dead Code Cleanup and Snapshot Refactoring

**Date**: 2025-12-01 (continued session)

### User Request

> Find all dead code. E.g. unused imports, unused functions, unused variables, etc. First show me the list of dead code you found. Then, after I confirm, remove all dead code from the codebase.

### Initial Analysis

I identified the following potential dead code:

| Item | Location | Type | Status |
|------|----------|------|--------|
| `TerminalUI` | `ui.py:22-316` | Class | Fully implemented but never wired to orchestrator |
| `JobLogger` | `logger.py:61-84` | Class | Never instantiated - jobs use `Job._log()` directly |
| `Snapshot` | `models.py:128-134` | Dataclass | Defined but never instantiated |
| `get_target_lock_holder` | `lock.py:121-134` | Function | Defined but never called |
| `parse_threshold` | `config.py:225-237` | Function | Duplicate of `disk.py` version, never used |

### User Feedback on Analysis

1. **TerminalUI**: User said "This must be a mistake! It is all over the place in architecture.md. Is the rich terminal implemented somewhere else maybe?" → After investigation, confirmed it's **NOT dead code** but incomplete integration - needs to be wired later in a separate task.

2. **JobLogger**: User said "Could be ok. Verify that logging works without it." → Confirmed dead code, logging works via `Job._log()` method.

3. **Snapshot**: User said "Snapshot is still documented in data-model.md... Either this class is not necessary anymore, in which case these two documents need to be updated. Or the class is needed but not wired to the rest of the code." → User decided: "Implement properly and ensure it is used by the current code."

4. **get_target_lock_holder**: User confirmed "Ok" to remove.

5. **parse_threshold (config.py)**: User confirmed "OK" to remove.

### Snapshot Refactoring Decision

User requested the Snapshot dataclass be properly implemented and used:

> Implement properly and ensure it is used by the current code. It seems a useful class for all logic about the snapshots. Refactor existing code about btrfs snapshots that benefit from it, e.g. the btrfs snapshot cleanup. Tell me if you estimate that some parts of the code about snapshots would NOT benefit of this refactoring and why.

### Analysis: What Benefits vs What Doesn't

**Would BENEFIT from Snapshot dataclass:**
1. `cleanup_snapshots()` - Currently parses folder names with regex, works with strings. Refactored to use `Snapshot.from_path()` and work with typed objects.
2. New `list_snapshots()` function - Parse existing snapshots from filesystem into `list[Snapshot]`.

**Would NOT benefit (kept as-is):**
1. `snapshot_name()` - Generates a name string before we have all metadata. Keep as utility.
2. `create_snapshot()` - Just runs btrfs command with two path strings. No benefit from Snapshot objects.
3. `BtrfsSnapshotJob.execute()` - Creates snapshots, logs, moves on. Doesn't need to track metadata.
4. Validation functions - Just check existence, don't need Snapshot objects.

### Changes Made

#### 1. Fixed Snapshot Dataclass (`models.py`)

Updated to match spec with proper fields and methods:

```python
@dataclass(frozen=True)
class Snapshot:
    subvolume: str         # e.g., "@home"
    phase: SnapshotPhase   # PRE or POST
    timestamp: datetime    # When created
    session_id: str        # 8-char hex session identifier
    host: Host             # SOURCE or TARGET
    path: str              # Full filesystem path

    @property
    def name(self) -> str:
        """Snapshot name per FR-010: pre-@home-20251129T143022."""
        ts = self.timestamp.strftime("%Y%m%dT%H%M%S")
        return f"{self.phase.value}-{self.subvolume}-{ts}"

    @classmethod
    def from_path(cls, path: str, host: Host) -> "Snapshot":
        """Parse a Snapshot from its filesystem path."""
        ...
```

#### 2. Added `list_snapshots()` (`snapshots.py`)

```python
async def list_snapshots(
    executor: LocalExecutor | RemoteExecutor,
    host: Host,
) -> list[Snapshot]:
    """List all pc-switcher snapshots on a machine."""
    ...
```

#### 3. Refactored `cleanup_snapshots()` (`snapshots.py`)

- Changed signature: now takes `host: Host` instead of `session_folder: str`
- Uses `list_snapshots()` internally
- Returns `list[Snapshot]` instead of `list[str]`
- Groups by session_id for proper retention policy

#### 4. Updated CLI (`cli.py`)

- Added `Host` import
- Updated `cleanup_snapshots` call to use new signature
- Updated output to use `snapshot.path`

#### 5. Updated Data Model Doc (`data-model.md`)

- Fixed `SnapshotPhase` values: `PRE`/`POST` (not `PRESYNC`/`POSTSYNC`)
- Added `from_path()` classmethod documentation

#### 6. Removed Dead Code

- **`JobLogger`** class from `logger.py` + removed from `__all__`
- **`get_target_lock_holder()`** function from `lock.py`
- **`parse_threshold()`** function from `config.py` + removed from `__all__` + removed unused `re` import

### Verification

```
uv run basedpyright src/pcswitcher/ → 0 errors, 0 warnings, 0 notes
```

### Files Modified (This Session)

1. `src/pcswitcher/models.py` - Snapshot dataclass with proper fields and `from_path()` classmethod
2. `src/pcswitcher/snapshots.py` - Added `list_snapshots()`, refactored `cleanup_snapshots()`
3. `src/pcswitcher/cli.py` - Updated cleanup command to use new signature
4. `src/pcswitcher/logger.py` - Removed `JobLogger` class
5. `src/pcswitcher/lock.py` - Removed `get_target_lock_holder()` function
6. `src/pcswitcher/config.py` - Removed `parse_threshold()` function
7. `specs/001-foundation/data-model.md` - Updated Snapshot documentation

### Remaining Item (Deferred)

**TerminalUI** - NOT dead code, just incomplete integration. User confirmed: "Ok, we'll tackle this later in a separate conversation."

---

## Follow-up: FileLogger Hostname Fix

**Date**: 2025-12-01 (continued session)

### User Observation

> Orchestrator gives hostname_map to new task self._ui.consume_events().
> However, it is not given to the FileLogger consumer. How does the FileLogger know the hostnames of the remote machines? They should be in the log file as well for clarity.

### Analysis

Two issues were identified:

1. **FileLogger didn't receive hostname_map**: It used `LogEvent.to_dict()` which outputs `"host": "source"` or `"host": "target"` instead of actual hostnames like `"laptop"` or `"workstation"`.

2. **hostname_map created too early**: Created before SSH connection (Phase 2), so `self._target_hostname` was None, causing fallback to `"target"`.

### User Decision

> 1+2
> And verify upon creation of hostname_map that both hostnames are set. Do not use fallback values. An error should be raised if local (source) or remote (target) hostname are not known.

### Initial Implementation (Corrected)

Initial implementation moved consumer creation to after Phase 2 (SSH connection), but user pointed out:

> If FileLogger and UI consumers are only wired after SSH connection, this means the user doesn't get any feedback (not in UI, not in log file) during the first phase of the pc-switcher. This is not acceptable.
> We already know the target hostname at the very start of the program: it is given as command line argument! So there is no need to wait for the SSH connection.

### Final Changes Made

#### 1. Updated FileLogger (`logger.py`)

- Added `hostname_map: dict[Host, str]` parameter (required, no default)
- Added `hostname` field to JSON output in `consume()`:

```python
event_dict = event.to_dict()
event_dict["hostname"] = self._hostname_map.get(
    event.host, event.host.value
)
```

#### 2. Restructured Orchestrator (`orchestrator.py`)

- **Create hostname_map immediately** using `self._target` (CLI argument), not `self._target_hostname` (resolved after SSH):

```python
hostname_map = {
    Host.SOURCE: self._source_hostname,
    Host.TARGET: self._target,  # CLI argument, known from start
}
```

- **Added hostname validation** (no fallbacks allowed):

```python
if not self._source_hostname:
    raise RuntimeError("Source hostname is not set")
if not self._target:
    raise RuntimeError("Target hostname is not set")
```

- **Start consumers BEFORE Phase 1** so user gets immediate feedback
- **Pass hostname_map to FileLogger**

### Result

- User gets immediate feedback from Phase 1 onwards (UI and log file)
- Log files include actual hostnames:
```json
{"timestamp": "...", "level": "INFO", "job": "orchestrator", "host": "source", "hostname": "laptop", "event": "..."}
```

### Verification

```
uv run basedpyright src/pcswitcher/orchestrator.py → 0 errors, 0 warnings
```

### Files Modified

1. `src/pcswitcher/logger.py` - Added hostname_map to FileLogger, include hostname in JSON
2. `src/pcswitcher/orchestrator.py` - Create hostname_map at start using CLI target, validate hostnames

---

## Follow-up: Hostname & JobContext Cleanup

**Date**: 2025-12-02 (continued session)

### User Observations

1. Why is there both `self._target` and `self._target_hostname`? They seem redundant.
2. Line 277: `target_hostname=self._target_hostname or ""` - fallback not allowed, we always have a target hostname.
3. JobContext is created 6 times with identical boilerplate - violates DRY.

### Changes Made

#### 1. Unified Hostname Variables

Eliminated `self._target` and kept only `self._target_hostname` (set from CLI argument in constructor):

```python
# Before
self._target = target
self._target_hostname: str | None = None  # Set later via SSH

# After
self._target_hostname = target  # Known from CLI argument
```

Removed SSH hostname resolution (`get_hostname()` call) - the CLI argument is sufficient.

#### 2. Removed `get_hostname()` from RemoteExecutor

Per YAGNI principle, removed the now-unused method from `executor.py`.

#### 3. Created `_create_job_context()` Factory Method

Added factory method to eliminate 6 duplicated JobContext creations:

```python
def _create_job_context(self, config: dict[str, Any]) -> JobContext:
    """Create JobContext with current orchestrator state."""
    assert self._local_executor is not None
    assert self._remote_executor is not None

    return JobContext(
        config=config,
        source=self._local_executor,
        target=self._remote_executor,
        event_bus=self._event_bus,
        session_id=self._session_id,
        source_hostname=self._source_hostname,
        target_hostname=self._target_hostname,
    )
```

All JobContext creations now use: `self._create_job_context(config)`

#### 4. Removed Scattered Assertions

Assertions before JobContext creation moved into the factory method. Remaining assertions (lines 185, 267, 363-364) are legitimate for direct executor usage.

#### 5. Removed Unused JobContext

Removed the intermediate context created at line 343 with comment "Will be filled per job" - it was only used to copy fields into per-job contexts. Now each job gets context directly from factory.

### Verification

```
uv run basedpyright src/pcswitcher/orchestrator.py src/pcswitcher/executor.py → 0 errors
uv run ruff check src/pcswitcher/orchestrator.py src/pcswitcher/executor.py → All checks passed
```

### Files Modified

1. `src/pcswitcher/orchestrator.py` - Unified hostname, added factory method, removed redundant code
2. `src/pcswitcher/executor.py` - Removed `get_hostname()` method

---

## Follow-up: DateTime Fields with Timezone

**Date**: 2025-12-02 (continued session)

### User Request

Change `started_at` and `ended_at` fields from ISO 8601 strings to `datetime` objects with UTC timezone.

### Changes Made

#### 1. Updated `JobResult` Dataclass

```python
# Before
started_at: str  # ISO 8601 timestamp
ended_at: str  # ISO 8601 timestamp

# After
started_at: datetime  # UTC timezone
ended_at: datetime  # UTC timezone
```

#### 2. Updated `SyncSession` Dataclass

```python
# Before
started_at: str  # ISO 8601 timestamp
ended_at: str | None  # ISO 8601 timestamp

# After
started_at: datetime  # UTC timezone
ended_at: datetime | None  # UTC timezone
```

#### 3. Updated All Datetime Creation

Changed all `datetime.now().isoformat()` calls to `datetime.now(UTC)`:

- `src/pcswitcher/orchestrator.py:124` - Session created with UTC timestamp
- `src/pcswitcher/orchestrator.py:216, 223, 230` - Session ended_at set with UTC timestamp
- `src/pcswitcher/orchestrator.py:484, 487, 503` - JobResult created with UTC timestamps

Uses Python 3.11+ `datetime.UTC` alias (imported via ruff auto-fix).

### Verification

```
uv run basedpyright src/pcswitcher/ → 0 errors
uv run ruff check src/pcswitcher/ → All checks passed
```

### Files Modified

1. `src/pcswitcher/models.py` - Updated JobResult and SyncSession dataclasses
2. `src/pcswitcher/orchestrator.py` - Updated all datetime creation to use UTC

---

## Follow-up: Version Check Integration into InstallOnTargetJob

**Date**: 2025-12-02 (continued session)

### User Request

Simplify the sync execution flow by consolidating version compatibility checking into the InstallOnTargetJob validation phase instead of as a separate execution phase.

### Context

The original architecture.md had a separate "Version Compatibility Check" phase (Phase 4) that only verified the target version wasn't newer than source, but performed no installation. Installation happened later in Phase 7 (Install/Upgrade on Target) after pre-sync snapshots.

This was inefficient - two separate phases were checking the same information. The solution: integrate version checking into InstallOnTargetJob's validate phase for fail-fast detection.

### Changes Made

#### 1. Refactored InstallOnTargetJob (`jobs/install_on_target.py`)

**Moved version check from execute() to validate():**

```python
async def validate(self, context: JobContext) -> list[ValidationError]:
    """Validate version compatibility between source and target."""
    source_version = Version(get_this_version())

    # Check target version
    result = await context.target.run_command("pc-switcher --version 2>/dev/null")
    if result.success:
        target_version = Version(parse_version_from_cli_output(result.stdout))
        if target_version > source_version:
            return [
                ValidationError(
                    job=self.name,
                    host=Host.TARGET,
                    message=f"Target version {target_version} is newer than source {source_version}",
                )
            ]

    return []
```

**Simplified execute() method:**

- No longer checks version compatibility (already validated)
- Still checks for version match (for log clarity)
- Removed the `raise RuntimeError` for newer target version (now returns ValidationError from validate phase)

#### 2. Updated Architecture Documentation (`architecture.md`)

**Removed "Version Compatibility Check" phase:**
- Deleted VersionCheck node from execution flow diagram
- Updated connection: `AcquireLocks → SubvolCheck` (was `AcquireLocks → VersionCheck → SubvolCheck`)
- Removed VersionCheck style definition

**Updated InstallTarget phase description:**
- Now includes: "Validate version compatibility", "If target version newer → CRITICAL abort"
- Clarified as integrated validation, not separate phase

**Updated key ordering notes:**
1. Changed from "Version check separated from installation" to "Version check integrated into installation job"
2. Updated ordering: "Locks → Subvolume validation → Disk preflight → Snapshots" (removed version check as separate step)
3. Clarified version compatibility is part of System state validation phase
4. Removed references to phase numbers 4 and 7 (now consolidated into single InstallOnTargetJob phase)

### Benefits

1. **Fail-fast**: Version incompatibilities detected during validation phase, before any modifications
2. **Simpler execution flow**: 11 phases instead of 12
3. **Better separation of concerns**: Each job owns all its validation logic
4. **Consistent error handling**: ValidationError objects returned from validate phase, not runtime exceptions

### Verification

```
uv run basedpyright src/pcswitcher/jobs/install_on_target.py → 0 errors
uv run ruff check src/pcswitcher/jobs/install_on_target.py → All checks passed
```

### Files Modified

1. `src/pcswitcher/jobs/install_on_target.py` - Moved version check to validate(), simplified execute()
2. `specs/001-foundation/architecture.md` - Removed Phase 4, updated flow diagram, updated ordering notes

---

## Follow-up: DRY Refactor for ValidationError Creation

**Date**: 2025-12-02 (continued session)

### User Request

> We want life of developers of Jobs to be as easy as possible and follow DRY.
> I see that currently, validate() should return a list of ValidationErrors. The implementer of this method needs to fill the field "job" in each ValidationError it creates. This should be done automatically by the base class, so the implementer of validate() only needs to set the host and the message.

### Analysis

Every job's `validate()` method was creating `ValidationError` with repetitive boilerplate:

```python
ValidationError(job=self.name, host=Host.SOURCE, message="...")
```

The `job=self.name` part is identical across all jobs - unnecessary repetition that violates DRY.

### Solution

Added a `_validation_error()` helper method to the `Job` base class, following the same pattern as `_log()`:

```python
def _validation_error(self, host: Host, message: str) -> ValidationError:
    """Create a ValidationError with job name filled in automatically."""
    return ValidationError(job=self.name, host=host, message=message)
```

### Changes Made

#### 1. Added helper to base class (`jobs/base.py`)

Added `_validation_error(host, message)` method that automatically fills in `job=self.name`.

#### 2. Updated all job implementations

**`jobs/install_on_target.py`**:
```python
# Before
ValidationError(job=self.name, host=Host.TARGET, message=f"...")

# After
self._validation_error(Host.TARGET, f"...")
```

**`jobs/btrfs.py`**: Updated 6 usages

**`jobs/disk_space_monitor.py`**: Updated 3 usages

**`jobs/dummy.py`**: No changes needed (returns empty lists)

#### 3. Import cleanup

Since `ValidationError` is now only used for type annotations (return type `list[ValidationError]`), moved imports to `TYPE_CHECKING` block in all job files:

```python
from typing import TYPE_CHECKING
...

if TYPE_CHECKING:
    from pcswitcher.models import ValidationError
```

### Benefits

1. **DRY**: Job implementers no longer repeat `job=self.name`
2. **Consistency**: Same pattern as `_log()` method
3. **Cleaner imports**: `ValidationError` only imported for type checking
4. **Less error-prone**: Impossible to accidentally use wrong job name

### Verification

```
uv run basedpyright src/pcswitcher/ → 0 errors, 0 warnings, 0 notes
uv run ruff check src/pcswitcher/ → All checks passed!
```

### Files Modified

1. `src/pcswitcher/jobs/base.py` - Added `_validation_error()` helper method
2. `src/pcswitcher/jobs/install_on_target.py` - Use helper, TYPE_CHECKING import
3. `src/pcswitcher/jobs/btrfs.py` - Use helper, TYPE_CHECKING import
4. `src/pcswitcher/jobs/disk_space_monitor.py` - Use helper, TYPE_CHECKING import
5. `src/pcswitcher/jobs/dummy.py` - TYPE_CHECKING import for consistency

---

## Follow-up: Job Shortcut Properties and Context Rename

**Date**: 2025-12-02 (continued session)

### User Request

> Add shortcut fields source and target to Job, so that instead of writing self._context.source.run_command(...), the implementer can write self.source.run_command(...).
> Rename _context to context for simplicity, even though it is supposed to be protected.

### Changes Made

#### 1. Updated Job base class (`jobs/base.py`)

**Renamed `_context` to `context`:**
```python
# Before
self._context = context

# After
self.context = context
```

**Added shortcut properties:**
```python
@property
def source(self) -> LocalExecutor:
    """Shortcut to context.source executor."""
    return self.context.source

@property
def target(self) -> RemoteExecutor:
    """Shortcut to context.target executor."""
    return self.context.target
```

Added TYPE_CHECKING imports for `LocalExecutor` and `RemoteExecutor`.

#### 2. Updated all job implementations

**Before:**
```python
result = await self._context.target.run_command(...)
subvolumes = self._context.config["subvolumes"]
```

**After:**
```python
result = await self.target.run_command(...)
subvolumes = self.context.config["subvolumes"]
```

| File | Changes |
|------|---------|
| `jobs/btrfs.py` | `self.source`, `self.target`, `self.context.config`, `self.context.session_id` |
| `jobs/install_on_target.py` | `self.target` (4 usages) |
| `jobs/disk_space_monitor.py` | `self.source`, `self.target`, `self.context.config`, `self.context.source_hostname`, `self.context.target_hostname` |
| `jobs/dummy.py` | `self.context.config` |

### Benefits

1. **Shorter code**: `self.target.run_command()` vs `self._context.target.run_command()`
2. **Clearer intent**: Direct access to executors without intermediate object
3. **Consistent with other helpers**: Same pattern as `_log()`, `_validation_error()`

### Verification

```
uv run basedpyright src/pcswitcher/ → 0 errors, 0 warnings, 0 notes
uv run ruff check src/pcswitcher/ → All checks passed!
uv run pytest -v → 14 passed
```

### Files Modified

1. `src/pcswitcher/jobs/base.py` - Added properties, renamed context, TYPE_CHECKING imports
2. `src/pcswitcher/jobs/btrfs.py` - Updated all usages
3. `src/pcswitcher/jobs/install_on_target.py` - Updated all usages
4. `src/pcswitcher/jobs/disk_space_monitor.py` - Updated all usages
5. `src/pcswitcher/jobs/dummy.py` - Updated config usage

---

## Follow-up: Configurable Disk Space Warning Threshold

**Date**: 2025-12-02 (continued session)

### User Request

> I see you implemented a disk space WARNING check as well as required MINIMUM check that aborts.
> Make the warning level configurable, with the exact same logic as for the current disk_space_monitor.runtime_minimum config parameter.
> Ensure that all code, docs, config files, scripts, etc. are updated accordingly.

### Problem

The warning check was hardcoded at +5% or +10GiB above the `runtime_minimum` threshold:

```python
# Log warning if getting close (within 5% or 10GiB of threshold)
if threshold_type == "percent":
    if free_percent < threshold_value + 5:  # Hardcoded +5%
        warning_triggered = True
elif disk_space.available_bytes < threshold_value + (10 * 2**30):  # Hardcoded +10GiB
    warning_triggered = True
```

### Solution

Added configurable `warning_threshold` parameter that uses the same format as `runtime_minimum` (e.g., "25%" or "50GiB").

### Changes Made

#### 1. Tests First (TDD)

Created `tests/unit/test_jobs/test_disk_space_monitor.py` with 7 tests:
- `test_schema_has_warning_threshold_property`
- `test_schema_requires_warning_threshold`
- `test_validate_config_accepts_percentage_warning_threshold`
- `test_validate_config_accepts_absolute_warning_threshold`
- `test_validate_config_rejects_missing_warning_threshold`
- `test_validate_checks_warning_threshold_format`
- `test_validate_rejects_invalid_warning_threshold_format`

#### 2. Updated DiskSpaceMonitorJob (`jobs/disk_space_monitor.py`)

**CONFIG_SCHEMA**: Added `warning_threshold` property and to `required` list.

**validate()**: Added format validation for `warning_threshold`.

**execute()**: Replaced hardcoded warning logic with configurable threshold:
```python
warning_threshold: str = self.context.config["warning_threshold"]
warning_type, warning_value = parse_threshold(warning_threshold)

# Check against warning threshold
is_warning = False
if warning_type == "percent":
    free_percent = 100 - disk_space.use_percent
    if free_percent < warning_value:
        is_warning = True
elif disk_space.available_bytes < warning_value:
    is_warning = True
```

#### 3. Updated Config Schema YAML Files

Both files updated with `warning_threshold`:
- `src/pcswitcher/schemas/config-schema.yaml`
- `specs/001-foundation/contracts/config-schema.yaml`

```yaml
warning_threshold:
  $ref: "#/definitions/disk_threshold"
  default: "25%"
  description: "Free space threshold for warnings during sync"
```

#### 4. Updated Documentation

All config examples updated to include `warning_threshold`:
- `README.md`
- `docs/architecture.md`
- `docs/implementation.md`
- `specs/001-foundation/architecture.md` (2 occurrences)
- `specs/001-foundation/quickstart.md`
- `specs/001-foundation/data-model.md`

### Verification

```
uv run basedpyright src/pcswitcher/ → 0 errors, 0 warnings, 0 notes
uv run ruff check src/pcswitcher/ → All checks passed!
uv run pytest -v → 21 passed (7 new tests)
```

### Files Created

1. `tests/unit/test_jobs/test_disk_space_monitor.py` - New test file for DiskSpaceMonitorJob

### Files Modified

1. `src/pcswitcher/jobs/disk_space_monitor.py` - Added warning_threshold config, validation, and logic
2. `src/pcswitcher/schemas/config-schema.yaml` - Added warning_threshold
3. `specs/001-foundation/contracts/config-schema.yaml` - Added warning_threshold
4. `install.sh` - Added warning_threshold to default config template
5. `README.md` - Updated config example
6. `docs/architecture.md` - Updated config example
7. `docs/implementation.md` - Updated DiskConfig
8. `specs/001-foundation/architecture.md` - Updated config examples (2 occurrences)
9. `specs/001-foundation/quickstart.md` - Updated config example
10. `specs/001-foundation/data-model.md` - Updated DiskConfig

---

## Follow-up: Job Config Validation, Dynamic Loading, and Default Config File

**Date**: 2025-12-02 (continued session)

### User Request

The Execution Flow Summary has a "Job Config Validation" phase (see @specs/001-foundation/architecture.md ). There is a class method validate_config(). However, this is never overridden by any Job subclass. E.g. disk_space_monitor.py does the validation of the thresholds in its validate() method, which is for SYSTEM validation, not for config validation. We want to catch all config errors early.
So refactor all Jobs to implement validate_config() if they need any further config validation than what is already specified in the JSON schema. Ensure they call the super().validate_config() as well to get the JSON schema validation from the base class.
Refactor so that the parsing of the config values is only done once. I see it is done multiple times in disk_space_monitor.py: both in validate() and in execute().

orchestrator.py has a **hardcoded** job_registry in method _discover_and_validate_jobs().
Only load the modules that are enabled in the config file. Load them dynamically. Auto-discover the corresponding Job classes that are in those modules.
Of course, always load the system Jobs (disk_space_monitor, btrfs_snapshots, install_on_target). These can be hardcoded/imported.
Ensure the entire config file is logged to DEBUG at the start.

The default config.yaml is hardcoded in install.sh. Put it somewhere as a separate file in the repo, so that it is easier to edit and so that it can server as documentation for both developers and users. Let install.sh download the file from the github repo (consistent version with the version we are installing!). Or the release process could integrate it in install.sh automatically, but this seems more complicated.

### Summary of user request
Three related refactoring tasks to improve the job system architecture:

1. **Job Config Validation** - Move config validation from `validate()` (Phase 3 - system validation) to `validate_config()` (Phase 2 - config validation), parse config values only once
2. **Dynamic Job Loading** - Replace hardcoded `job_registry` in orchestrator with dynamic module loading
3. **Default Config File** - Extract hardcoded config from `install.sh` to separate versioned file

### Task 1: Job Config Validation Refactoring

#### Problem

`DiskSpaceMonitorJob.validate()` was validating threshold formats (a config problem) in Phase 3 (system validation). This should be in `validate_config()` (Phase 2) for fail-fast behavior. Additionally, `parse_threshold()` was called twice: once in `validate()` and again in `execute()`, violating DRY.

#### Solution

**Added `validate_config()` classmethod to DiskSpaceMonitorJob:**
```python
@classmethod
def validate_config(cls, config: dict[str, Any]) -> list[ConfigError]:
    errors = super().validate_config(config)  # JSON schema validation first
    if errors:
        return errors

    # Validate threshold formats (semantic validation)
    for key in ["preflight_minimum", "runtime_minimum", "warning_threshold"]:
        try:
            parse_threshold(config[key])
        except ValueError as e:
            errors.append(ConfigError(job=cls.name, path=key, message=str(e)))

    return errors
```

**Stored parsed values in `__init__()`:**
```python
def __init__(self, context: JobContext, host: Host, mount_point: str) -> None:
    super().__init__(context)
    self.host = host
    self.mount_point = mount_point

    # Parse thresholds once (validation already done in validate_config)
    self._preflight_threshold = parse_threshold(context.config["preflight_minimum"])
    self._runtime_threshold = parse_threshold(context.config["runtime_minimum"])
    self._warning_threshold = parse_threshold(context.config["warning_threshold"])
    self._check_interval = context.config["check_interval"]
```

**Simplified `validate()`:**
- Removed all threshold format parsing (now in `validate_config`)
- Only checks mount point exists (true system validation)

**Simplified `execute()`:**
- Uses pre-parsed instance attributes instead of re-parsing config values

**Updated tests:**
- Added new `TestDiskSpaceMonitorValidateConfig` class testing `validate_config()` for invalid formats
- Simplified `TestDiskSpaceMonitorValidation` to only test mount point validation

### Task 2: Dynamic Job Loading

#### Problem

Hardcoded `job_registry` with only 2 jobs, no config logging at startup.

#### Solution

**User Decisions:**
- True lazy loading: only import modules for jobs enabled in config
- Convention: `job_name == module_name` (no mapping needed)
- Split `dummy.py` into separate modules

**Split dummy.py:**
- Created `src/pcswitcher/jobs/dummy_success.py` with `DummySuccessJob` (name="dummy_success")
- Created `src/pcswitcher/jobs/dummy_fail.py` with `DummyFailJob` (name="dummy_fail")
- Deleted `src/pcswitcher/jobs/dummy.py`

**Updated orchestrator for lazy loading:**
```python
# Log entire config at DEBUG level
self._logger.log(
    LogLevel.DEBUG, Host.SOURCE, "Configuration loaded",
    log_file_level=self._config.log_file_level.name,
    sync_jobs=self._config.sync_jobs,
    # ... other config fields
)

# Lazy load only enabled jobs (job_name == module_name)
for job_name, enabled in self._config.sync_jobs.items():
    if not enabled:
        self._logger.log(LogLevel.DEBUG, Host.SOURCE, f"Job {job_name} is disabled")
        continue

    # Dynamic import: pcswitcher.jobs.{job_name}
    try:
        module = importlib.import_module(f"pcswitcher.jobs.{job_name}")
    except ModuleNotFoundError:
        self._logger.log(LogLevel.WARNING, Host.SOURCE, f"Job module not found")
        continue

    # Find SyncJob class with matching name attribute
    # ... class discovery and validation
```

**Removed hardcoded imports** from orchestrator (no longer imports dummy jobs directly).

### Task 3: Default Config File

#### Problem

Default config was hardcoded in `install.sh` as a ~115 line heredoc - difficult to maintain and not versioned with package.

#### Solution

**User Decision:** Add `pc-switcher init` CLI command; `install.sh` tells user to run it (not automatic).

**Created `src/pcswitcher/default-config.yaml`:**
- Standalone config file bundled with Python package
- Contains full comments explaining each option
- Accessible via `importlib.resources` for programmatic use

**Added `pc-switcher init` command:**
```python
@app.command()
def init(
    force: Annotated[bool, typer.Option("--force", "-f", ...)] = False,
) -> None:
    """Initialize default configuration file."""
    config_path = Configuration.get_default_config_path()

    if config_path.exists() and not force:
        console.print(f"Config already exists: {config_path}")
        raise typer.Exit(1)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    default_config = files("pcswitcher").joinpath("default-config.yaml").read_text()
    config_path.write_text(default_config)
```

**Updated `install.sh`:**
- Removed 115-line heredoc
- Added instruction: "Run 'pc-switcher init' to create default configuration"

**Updated `README.md`:**
- Added `pc-switcher init` step after installation
- Added `init` command to Available Commands section

### Additional Fix: warning_threshold in DiskConfig

During type checking, discovered `DiskConfig` dataclass was missing `warning_threshold` field. Added:

```python
@dataclass
class DiskConfig:
    preflight_minimum: str = "20%"
    runtime_minimum: str = "15%"
    warning_threshold: str = "25%"  # Added
    check_interval: int = 30
```

Also updated the parsing logic in `Configuration.from_yaml()`.

### Verification

```
uv run basedpyright → 0 errors, 0 warnings, 0 notes
uv run ruff check src/ → All checks passed!
uv run pytest → 25 passed
```

### Files Created

1. `src/pcswitcher/default-config.yaml` - Standalone default config file
2. `src/pcswitcher/jobs/dummy_success.py` - DummySuccessJob (split from dummy.py)
3. `src/pcswitcher/jobs/dummy_fail.py` - DummyFailJob (split from dummy.py)

### Files Modified

1. `src/pcswitcher/jobs/disk_space_monitor.py` - Added validate_config(), stored parsed thresholds, simplified validate()/execute()
2. `src/pcswitcher/orchestrator.py` - Added config logging, lazy import for enabled jobs
3. `src/pcswitcher/jobs/__init__.py` - Updated exports for split dummy modules
4. `src/pcswitcher/cli.py` - Added `init` command
5. `src/pcswitcher/config.py` - Added warning_threshold to DiskConfig
6. `install.sh` - Removed heredoc, added init instruction
7. `README.md` - Added init instructions
8. `tests/unit/test_jobs/test_disk_space_monitor.py` - Updated tests for validate_config()

### Files Deleted

1. `src/pcswitcher/jobs/dummy.py` - Split into dummy_success.py and dummy_fail.py

---

## Follow-up: Move Btrfs Snapshot Cleanup Logic from CLI to Snapshots Module

**Date**: 2025-12-02 (continued session)
**GitHub Issue**: #25

### Feedback

> cli.py contains code for btrfs snapshot cleanup
> This logic should be in a separate module that provides the BtrfsSnapshotJob, because it is tightly linked with this. Moreover, the current names `_run_cleanup()` and `_async_run_cleanup()` are not explicit enough: the names should clearly state that it is about cleanup of btrfs snapshots.

### Conversation Log

No clarifying questions needed - the issue was clear.

### Changes Made

#### 1. Moved Functions to `snapshots.py`

Moved `_run_cleanup()` and `_async_run_cleanup()` from `cli.py` to `snapshots.py` with renamed functions:

- `_run_cleanup()` → `run_btrfs_snapshot_cleanup()`
- `_async_run_cleanup()` → `_async_btrfs_snapshot_cleanup()`

The new names clearly indicate these are specifically for btrfs snapshot cleanup operations.

#### 2. Added Console Print Callback

Since the moved functions need to print output to console, added a `console_print: PrintFunction` parameter to decouple the module from Rich console:

```python
type PrintFunction = Callable[[str], None]

def run_btrfs_snapshot_cleanup(
    keep_recent: int,
    max_age_days: int | None,
    dry_run: bool,
    console_print: PrintFunction,
) -> int:
    """Run the btrfs snapshot cleanup operation with asyncio."""
    return asyncio.run(_async_btrfs_snapshot_cleanup(keep_recent, max_age_days, dry_run, console_print))
```

#### 3. Updated CLI

Updated `cli.py` to use the new function:

```python
from pcswitcher.snapshots import parse_older_than, run_btrfs_snapshot_cleanup

# In cleanup_snapshots command:
exit_code = run_btrfs_snapshot_cleanup(
    cfg.btrfs_snapshots.keep_recent,
    max_age_days,
    dry_run,
    console.print,
)
```

Removed unused imports: `LocalExecutor`, `Host`, `cleanup_snapshots as cleanup_snapshots_impl`.

### Verification

```
uv run basedpyright src/pcswitcher/snapshots.py src/pcswitcher/cli.py → 0 errors
uv run ruff check src/pcswitcher/snapshots.py src/pcswitcher/cli.py → All checks passed!
uv run pytest -v → 25 passed
```

### Files Modified

1. `src/pcswitcher/snapshots.py` - Added `run_btrfs_snapshot_cleanup()` and `_async_btrfs_snapshot_cleanup()`
2. `src/pcswitcher/cli.py` - Removed old functions, updated imports and call site

### GitHub Issue

Issue #25 closed with comment explaining the fix.

---

## Follow-up: Rename Module from snapshots to btrfs_snapshots

**Date**: 2025-12-02 (continued session)

### Feedback

> Rename the module "snapshots" to "btrfs_snapshots" for clarity. Now we moved the cli functions to this module, the function names do not need to contain "btrfs": this is already clear from the module name.

### Conversation Log

No clarifying questions needed - the feedback was clear.

### Changes Made

#### 1. Renamed Module

Renamed `src/pcswitcher/snapshots.py` to `src/pcswitcher/btrfs_snapshots.py`.

#### 2. Renamed Functions (removed redundant "btrfs" prefix)

Since the module name now explicitly indicates "btrfs_snapshots", the function names no longer need the redundant prefix:

- `run_btrfs_snapshot_cleanup()` → `run_snapshot_cleanup()`
- `_async_btrfs_snapshot_cleanup()` → `_async_snapshot_cleanup()`

Updated `__all__` list accordingly.

#### 3. Updated All Imports

Updated imports in all files that reference the module:

| File | Import Change |
|------|---------------|
| `cli.py` | `from pcswitcher.btrfs_snapshots import parse_older_than, run_snapshot_cleanup` |
| `orchestrator.py` | `from pcswitcher.btrfs_snapshots import session_folder_name` |
| `jobs/btrfs.py` | `from pcswitcher.btrfs_snapshots import create_snapshot, snapshot_name, ...` |

#### 4. Updated Call Site

Updated the call in `cli.py` from `run_btrfs_snapshot_cleanup(...)` to `run_snapshot_cleanup(...)`.

### Verification

```
uv run basedpyright src/pcswitcher/ → 0 errors, 0 warnings, 0 notes
uv run ruff check src/pcswitcher/ --fix → 3 import sorting fixes applied
uv run pytest tests/ -v → 25 passed
```

### Files Modified

1. `src/pcswitcher/snapshots.py` → `src/pcswitcher/btrfs_snapshots.py` (renamed + function renames)
2. `src/pcswitcher/cli.py` - Updated import and function call
3. `src/pcswitcher/orchestrator.py` - Updated import
4. `src/pcswitcher/jobs/btrfs.py` - Updated import

---

## Follow-up: Async vs Sync for CLI Cleanup Command

**Date**: 2025-12-02 (continued session)

### Feedback

> Does this cleanup cli command benefit from async? If not, make it simple sync.

### Conversation Log

**My clarifying question:**

> Should I create sync versions of cleanup_snapshots() and list_snapshots() for the CLI, or just simplify run_snapshot_cleanup() to use subprocess.run() directly without reusing those functions?

Options presented:
1. **Sync CLI only**: Create a simple sync `run_snapshot_cleanup()` using `subprocess.run()` directly, without reusing the async `cleanup_snapshots()` function
2. **Dual sync/async**: Keep async functions for orchestrator use, add sync wrappers or separate sync implementations for CLI use

**User's answer:**

> Keep as it is now, but document why (use of an async function deeper)

### Analysis

The CLI cleanup command does **NOT** benefit from async because:
1. It only runs on the **local machine** (no remote operations)
2. Operations are **sequential** - list snapshots, then delete one by one
3. There's **no parallelism** - we're just awaiting each command one after another
4. The async machinery (`asyncio.run`, `async def`, `await`) is just overhead

However, the async implementation is kept because `cleanup_snapshots()` is an async function that's reused by other parts of the codebase (orchestrator) that run in an async context. Creating a separate sync implementation would mean code duplication.

### Changes Made

Added documentation to `run_snapshot_cleanup()` explaining why async is used despite not benefiting the CLI:

```python
def run_snapshot_cleanup(...) -> int:
    """Run the snapshot cleanup operation.

    Note: Uses asyncio.run() because cleanup_snapshots() is async. The async
    implementation is required for reuse by the orchestrator which runs in an
    async context. The CLI cleanup command itself doesn't benefit from async
    (all operations are local and sequential), but we use the shared async
    implementation to avoid code duplication.
    ...
    """
```

### Verification

```
uv run basedpyright src/pcswitcher/btrfs_snapshots.py → 0 errors
uv run ruff check src/pcswitcher/btrfs_snapshots.py → All checks passed
```

### Files Modified

1. `src/pcswitcher/btrfs_snapshots.py` - Added documentation explaining async usage

---

## Follow-up: Executor Protocol for LocalExecutor | RemoteExecutor

**Date**: 2025-12-02 (continued session)

### Feedback

> I see "LocalExecutor | RemoteExecutor" in many places (e.g. in btrfs_snapshots.py). These classes have the same signature, on purpose. I think they should either derive from an ABC Executor or at least we should have a protocol Executor. This would clarify the intent and give simpler code.

### Conversation Log

**My clarifying question:**

> Should the Executor protocol include only the common methods (run_command, start_process, terminate_all_processes) or also keep send_file/get_file as RemoteExecutor-specific?

Options presented:
1. **Common only**: Protocol includes only run_command, start_process, terminate_all_processes (the methods both executors share)
2. **All methods**: Protocol includes all methods, with send_file/get_file raising NotImplementedError on LocalExecutor

**User's answer:**

> Common only. Look further for other possible variants of the pattern we want to avoid, e.g. "RemoteExecutor | LocalExecutor" or similar: we want to replace any usage of RemoteExecutor or LocalExecutor by just Executor in all cases where we don't need to explicitly know which one it is. In summary: use the new abstraction as much as possible, but not more.

### Analysis

Found 6 usages of `LocalExecutor | RemoteExecutor` union type:
- `btrfs_snapshots.py`: 5 occurrences (create_snapshot, validate_snapshots_directory, validate_subvolume_exists, list_snapshots, cleanup_snapshots)
- `disk.py`: 1 occurrence (check_disk_space)

The following should **NOT** use Executor (explicitly need specific type):
- `jobs/base.py`: `source` property returns `LocalExecutor`, `target` returns `RemoteExecutor` (intentionally specific)
- `jobs/context.py`: `source: LocalExecutor`, `target: RemoteExecutor` (intentionally specific)
- `orchestrator.py`: stores `_local_executor: LocalExecutor | None` and `_remote_executor: RemoteExecutor | None` (intentionally specific)
- `lock.py`: uses `RemoteExecutor` specifically (only operates on remote)

### Changes Made

#### 1. Created Executor Protocol (`executor.py`)

```python
class Executor(Protocol):
    """Protocol for command execution on local or remote machines.

    Both LocalExecutor and RemoteExecutor implement this protocol,
    allowing code to work with either without knowing which one it is.
    """

    async def run_command(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command and wait for completion."""
        ...

    async def terminate_all_processes(self) -> None:
        """Terminate all tracked processes."""
        ...
```

Note: `start_process` was NOT included because it returns different concrete types (`LocalProcess` vs `RemoteProcess`), which would break protocol compatibility.

#### 2. Updated btrfs_snapshots.py

- Changed import from `LocalExecutor` + `RemoteExecutor` (TYPE_CHECKING) to `Executor, LocalExecutor`
- Replaced all 5 `LocalExecutor | RemoteExecutor` with `Executor`

#### 3. Updated disk.py

- Changed import from `LocalExecutor, RemoteExecutor` to `Executor`
- Replaced `LocalExecutor | RemoteExecutor` with `Executor` in `check_disk_space()`
- Updated docstring: "LocalExecutor or RemoteExecutor to run df command" → "Executor to run df command"

### Verification

```
uv run basedpyright src/pcswitcher/ → 0 errors, 0 warnings, 0 notes
uv run ruff check src/pcswitcher/ → All checks passed!
uv run pytest tests/ -v → 25 passed
```

### Files Modified

1. `src/pcswitcher/executor.py` - Added `Executor` protocol, exported in `__all__`
2. `src/pcswitcher/btrfs_snapshots.py` - Updated imports, replaced 5 union types with `Executor`
3. `src/pcswitcher/disk.py` - Updated import, replaced 1 union type with `Executor`
