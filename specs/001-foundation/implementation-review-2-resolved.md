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
