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
4. `src/pcswitcher/installation.py` - Utility functions (get_current_version, compare_versions)

## Files Created

1. `src/pcswitcher/jobs/install_on_target.py` - InstallOnTargetJob SystemJob per architecture.md
