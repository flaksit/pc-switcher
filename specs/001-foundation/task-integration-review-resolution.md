# Task Integration Review Resolution - 001-foundation

This document captures the resolution process for feedback from the Codex integration review.

## Overview

**Input**: `task-integration-review-codex.md` containing Codex's review findings
**Goal**: Address all non-ignored issues and ensure the implementation matches `spec.md`

---

## Clarifying Questions

### 1. Target-side lock (lock.py:87-118)

**Codex feedback**: "Target-side lock is not actually held: `acquire_target_lock` runs `flock` in a short-lived command and releases the lock immediately afterward, so concurrent syncs on the target are not prevented."

**Your comment**: "IS THIS REALLY A PROBLEM? OR DOES THE REVIEWER NOT UNDERSTAND HOW IT WORKS?"

**My analysis**: After reviewing the code, I believe the reviewer is **correct**. Here's why:

```python
result = await executor.run_command(
    f"mkdir -p ~/.local/share/pc-switcher && "
    f'exec 9>>"{lock_path}" && '
    f"flock -n 9 && "
    f'truncate -s 0 "{lock_path}" && '
    f'echo "{holder_info}" > "{lock_path}"'
)
```

This runs as a single shell command that:
1. Opens FD 9 with `exec 9>>`
2. Acquires flock on FD 9
3. Writes holder info
4. **The shell command completes and terminates**
5. **When the shell exits, FD 9 is closed, releasing the flock**

The lock is only held for the duration of the command execution (milliseconds), not for the entire sync session. A second sync could start immediately after this command returns.

**Suggestion**: We need to keep a long-lived SSH process with the lock held open for the entire session. One approach:
- Start a background process on target: `flock -n path -c "sleep infinity"`
- Keep track of its PID and kill it during cleanup

**Question**: Do you agree with this analysis? Should I implement a persistent target lock?

---

### 2. Self-installation order (orchestrator.py:178-203)

**Your comment**: "DOES IT DIVERGE FROM THE SPEC? THEN THE SPEC IS NOT UP TO DATE and should be fixed"

**Current implementation order**:
1. Phase 1: Acquire source lock
2. Phase 2: SSH connection
3. Phase 3: Acquire target lock
4. Phase 4: Job discovery and validation
5. Phase 5: Disk space preflight
6. Phase 6: **Pre-sync snapshots**
7. Phase 7: **Install/upgrade pc-switcher on target**
8. Phase 8: Execute sync jobs

**Spec says (US2)**: "the very first operation the orchestrator performs (before any system validation or snapshots) is to ensure the pc-switcher package on the target machine is at the same version"

**However**, the code comment says: "Runs AFTER pre-sync snapshots for rollback safety if installation fails."

**Question**: Which is correct? The current implementation (install after snapshots) or the spec (install first)? I recommend keeping the current order (install after snapshots) for rollback safety and updating the spec accordingly.

---

### 3. get_this_version() failing from source

**Your comment**: "DOES get_this_version() FAIL when running from source? This is annoying. What can we do about it?"

**Current behavior**: Uses `importlib.metadata.version("pcswitcher")` which fails with `PackageNotFoundError` when:
- Running directly from source without installation
- Running via `uv run` in development without proper metadata

**Options**:
1. **Read from pyproject.toml** as fallback (reliable but requires parsing)
2. **Add `__version__` to `__init__.py`** kept in sync with pyproject.toml (simple)
3. **Return "0.0.0-dev"** as fallback (acceptable for dev)
4. **Use hatch-vcs or setuptools-scm** for git-based versioning (already using uv-dynamic-versioning)
5. **Let it fail** (force proper installation, even for dev)

**My recommendation**: Since we're using `uv-dynamic-versioning` per ADR-004, the version should be available when installed via `uv pip install -e .`. If running directly with `uv run`, we could:
- Add a fallback that returns "0.0.0-dev" for development
- Or require developers to always use editable install

**Question**: Which approach do you prefer?

---

## Conversation Log

### Q&A Session 1

**Q1: Target Lock - Should I implement the fix?**
> User: OK

**Q2: Self-Installation Order - Keep current order and update spec?**
> User: OK

**Q3: get_this_version() Fallback - Which approach?**
> User: I lean to option 2, but doesn't uv have any similar real `uv` command instead of using `uv pip`? I know `uv sync` by default install the local project as well but I don't know if that is what we want and need.

**My Response**: You're right - `uv sync` is the preferred command. It installs both dependencies AND the local project in development mode (editable). After running `uv sync`, the package metadata is available and `get_this_version()` works. No code changes needed - developers should use `uv sync` which they likely already do.

---

## Issues To Resolve (Confirmed)

1. **Background monitors block completion** - DiskSpaceMonitorJob runs forever in TaskGroup ✓ CONFIRMED
2. **DiskSpaceMonitor validation** - Implement mount point existence check only ✓ CONFIRMED
3. **Target lock** - Implement persistent lock mechanism ✓ CONFIRMED
4. **Self-installation order** - Update spec to match implementation (install after snapshots) ✓ CONFIRMED
5. **get_this_version() fallback** - NO CHANGE NEEDED (uv sync provides metadata)
6. **Interrupt handling gaps** - Implement proper SIGINT cleanup per US5 ✓ CONFIRMED
7. **DummySuccessJob config** - Honor source_duration/target_duration from config ✓ CONFIRMED
8. **Terminal UI step progress** - Call set_current_step during workflow ✓ CONFIRMED
9. **Progress events to log file** - Add ProgressEvent logging at FULL level ✓ CONFIRMED
10. **Update spec** - Clarify JSON log format, local-only snapshot cleanup, installation order ✓ CONFIRMED

---

## Resolution Progress

### ✅ All Issues Resolved

**Verification:**
- All 25 tests pass
- Ruff linting: All checks passed
- Basedpyright: 0 errors, 0 warnings, 0 notes

---

### Issue 1: Background Monitors Block Completion ✅ FIXED

**File**: `src/pcswitcher/orchestrator.py`

**Problem**: DiskSpaceMonitorJob instances ran forever in a TaskGroup and were never cancelled.

**Fix**: Wrapped sync job execution in try/finally, cancelling monitor tasks in the finally block:
```python
source_monitor_task = tg.create_task(source_monitor.execute())
target_monitor_task = tg.create_task(target_monitor.execute())
try:
    # Execute sync jobs
finally:
    source_monitor_task.cancel()
    target_monitor_task.cancel()
```

---

### Issue 2: DiskSpaceMonitor Validation ✅ FIXED

**File**: `src/pcswitcher/jobs/disk_space_monitor.py`

**Problem**: `validate()` called `check_disk_space()` which could fail for reasons unrelated to mount point existence.

**Fix**: Changed to use simple `test -d {mount_point}` command for existence check only:
```python
result = await executor.run_command(f"test -d {self.mount_point}")
if not result.success:
    errors.append(self._validation_error(self.host, "Mount point does not exist or is not accessible"))
```

---

### Issue 3: Target Lock ✅ FIXED

**File**: `src/pcswitcher/lock.py`, `src/pcswitcher/orchestrator.py`

**Problem**: Lock was released immediately when shell command terminated.

**Fix**: Implemented persistent lock using background process:
- New `start_persistent_target_lock()` starts: `flock -n {path} -c "read"`
- The `read` command blocks indefinitely, holding the lock
- New `release_target_lock()` terminates the process during cleanup
- Orchestrator stores lock process and releases in `_cleanup()`

---

### Issue 4: Spec Updates ✅ FIXED

**File**: `specs/001-foundation/spec.md`

**Changes**:
1. **US2**: Clarified installation happens AFTER pre-sync snapshots for rollback safety
2. **US4/FR-022**: Updated log format description to JSON Lines (was incorrectly showing text format)
3. **US3**: Added clarification that cleanup-snapshots operates locally only

---

### Issue 5: get_this_version() ✅ NO CHANGE NEEDED

**Resolution**: Developers should use `uv sync` which installs the project and makes package metadata available. No code change needed.

---

### Issue 6: Interrupt Handling ✅ FIXED

**Files**: `src/pcswitcher/orchestrator.py`, `src/pcswitcher/cli.py`

**Problem**: SIGINT path didn't call `kill_all_remote_processes()`.

**Fix**: Added call in `_cleanup()`:
```python
if self._connection is not None:
    await self._connection.kill_all_remote_processes()
```

This ensures remote pc-switcher processes are killed via `pkill -f 'pc-switcher'` on SIGINT.

---

### Issue 7: DummySuccessJob Config ✅ FIXED

**File**: `src/pcswitcher/jobs/dummy_success.py`

**Problem**: CONFIG_SCHEMA was empty, ignoring `source_duration`/`target_duration`.

**Fix**:
- Added `source_duration` and `target_duration` to CONFIG_SCHEMA (integer, min 1, default 20)
- Read values from config in `__init__`
- Use configured durations in phase methods instead of hardcoded 20s

---

### Issue 8: Terminal UI Step Progress ✅ FIXED

**File**: `src/pcswitcher/orchestrator.py`

**Problem**: `set_current_step()` was never called, progress stayed at 0.

**Fix**:
- Fixed `total_steps` calculation: `7 + len(sync_jobs) + 1` (was `len(sync_jobs) + 2`)
- Added `set_current_step()` calls at each phase:
  - Step 1: Source lock
  - Step 2: SSH connection
  - Step 3: Target lock
  - Step 4: Validation
  - Step 5: Disk space check
  - Step 6: Pre-snapshots
  - Step 7: Installation
  - Steps 8+: Each sync job
  - Final: Post-snapshots

---

### Issue 9: Progress Events to Log File ✅ FIXED

**File**: `src/pcswitcher/logger.py`

**Problem**: FileLogger only logged LogEvent, not ProgressEvent.

**Fix**: Added ProgressEvent handling in `consume()`:
```python
elif isinstance(event, ProgressEvent) and self._level >= LogLevel.FULL:
    # Convert to JSON and write
```

Progress events are now written at FULL level per FR-045.

---

## Summary

All 9 confirmed issues from the Codex review have been resolved. The implementation now fully complies with the specifications in `spec.md`.

