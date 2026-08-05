---
phase: 01-home-sync-mvp-user-data-sync
plan: "04"
subsystem: folder-sync-job
tags: [folder-sync, divergence-guard, validate, rsync, btrfs, acl, dry-run, allow_divergence, tdd]
dependency_graph:
  requires:
    - sync_history.get_target_generation (plan 03)
    - sync_history.set_target_generation (plan 03)
    - JobContext.allow_divergence (plan 03)
  provides:
    - FolderSyncJob class with name="folder_sync"
    - FolderEntry dataclass with to_rsync_filter_args()
    - FolderSyncJob.validate() — sudo rsync, acl, folder-existence, divergence guard
    - FolderSyncJob.execute() stub (raises NotImplementedError, plan 05 fills it)
    - pcswitcher.jobs.FolderSyncJob export
  affects:
    - src/pcswitcher/jobs/folder_sync.py
    - src/pcswitcher/jobs/__init__.py
    - tests/unit/jobs/test_folder_sync.py
tech_stack:
  added: []
  patterns:
    - shlex.quote for all config-derived paths passed into shell commands (T-04-01)
    - findmnt + btrfs find-new for file-level divergence detection (D-06/D-08)
    - Conservative fallback: skip divergence when btrfs subvolume unresolvable (Open Q3)
    - ValidationError accumulation with early-return before divergence guard
    - sync_history module-level import enabling patch() in unit tests
key_files:
  created:
    - src/pcswitcher/jobs/folder_sync.py
    - tests/unit/jobs/test_folder_sync.py
  modified:
    - src/pcswitcher/jobs/__init__.py
decisions:
  - "Used findmnt + find-new for divergence detection (file-level, not generation-level comparison): find-new(<mount>, <stored_gen>) directly reports changed files, eliminating the need for a current-vs-stored generation compare step"
  - "_check_divergence returns ValidationError | None so validate() can accumulate divergence errors alongside preflight errors without exception flow control"
  - "Conservative divergence fallback: when findmnt fails (path not a btrfs subvolume root, e.g. /root on @), log WARNING and skip check — sync is allowed rather than blocked on uncertainty (RESEARCH Open Q3)"
metrics:
  duration: "8 minutes"
  completed: "2026-06-30"
  tasks_completed: 2
  tasks_total: 2
  files_changed: 3
status: complete
---

# Phase 01 Plan 04: FolderSyncJob Skeleton and Divergence Guard Summary

One-liner: FolderSyncJob(SyncJob) with name="folder_sync", CONFIG_SCHEMA, validate() enforcing sudo rsync + acl + folder existence + btrfs find-new divergence guard; execute() stub for plan 05.

## What Was Built

### Task 1: FolderSyncJob skeleton, CONFIG_SCHEMA, and validate() preflight checks

Created `src/pcswitcher/jobs/folder_sync.py` with:

- `@dataclass FolderEntry(path, enabled=True, excludes=[])` with `to_rsync_filter_args()` returning `["--filter=- <pattern>", ...]` in config order (first-match-wins preserved for rsync).
- `class FolderSyncJob(SyncJob)` with `name: ClassVar[str] = "folder_sync"` and `CONFIG_SCHEMA` matching the config-schema.yaml `folder_sync` section (folders array, required path, optional enabled/excludes, additionalProperties: false at item and root level).
- `_active_folders()`: parses `config["folders"]` into FolderEntry, filters `enabled=false` entries silently.
- `validate()`: accumulates errors across four ordered steps: (1) `sudo rsync --version` on source and target, (2) `dpkg -l acl | grep -q '^ii'` on source and target, (3) `test -d <quoted_path>` for each active folder on source. Early return before step 4 if structural errors exist. Step (4) is the divergence guard (Task 2).
- `execute()`: `raise NotImplementedError("FolderSyncJob.execute is implemented in plan 01-05")`.
- Exported `FolderEntry, FolderSyncJob` from `jobs/__init__.py`.

Tests written in `tests/unit/jobs/test_folder_sync.py` covering all behaviors with mocked executors (no real SSH). Used a module-local `make_context()` helper instead of the shared `mock_job_context_factory` fixture to support `allow_divergence=True` contexts that the shared factory doesn't accept.

### Task 2: Target-divergence guard in validate() (D-06/D-07/D-08/D-18)

Added divergence guard methods to `FolderSyncJob`:

- `_resolve_subvolume(path) -> tuple[str, str] | None`: calls `findmnt -no TARGET --target <quoted_path>` on the target to get the enclosing btrfs subvolume mount; computes the folder's relative prefix within that mount (`""` when the path IS the mount, e.g. `/home`→`/home`; `"root"` when `/root` lives on `/`). Returns `None` when findmnt fails (non-btrfs path, unmounted, Open Q3 fallback).

- `_get_subvolume_generation(mount) -> int`: parses `Generation:` line from `sudo btrfs subvolume show <mount>` on the target. Prepared for plan 05 (execute() will call this to record post-sync baseline via `set_target_generation`).

- `_target_diverged_since(folder, stored_gen) -> bool`: calls `_resolve_subvolume`; when `None` is returned, logs WARNING and returns `False` (conservative: allow sync). Otherwise runs `sudo btrfs subvolume find-new <mount> <stored_gen>`; scans output lines ignoring `"transid marker"` summary line; returns `True` if any non-empty line (with matching prefix filter) indicates a changed file. This is file-level detection — creating a read-only snapshot post-sync does not add files under the user data prefix, so it does not cause false positives (D-07).

- `_check_divergence(folder) -> ValidationError | None`: reads `stored = sync_history.get_target_generation(target_hostname, folder.path)`; returns `None` on `stored is None` (first sync, logged at INFO). Otherwise calls `_target_diverged_since`. When diverged: under `dry_run` or `allow_divergence`, logs WARNING including folder path (audit trail T-04-02b) and returns `None`. Otherwise returns `self._validation_error(Host.TARGET, msg)`.

The `validate()` method calls `_check_divergence` for each active folder and appends any returned `ValidationError` to the `errors` list, consistent with how preflight errors are accumulated.

## Verification Results

All plan verification commands green:

- `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract -q` — 70 passed (23 new folder_sync tests, 47 existing contract tests)
- `uv run ruff check src/pcswitcher/jobs/folder_sync.py` — no errors
- `uv run basedpyright src/pcswitcher/jobs/folder_sync.py` — 0 errors, 0 warnings, 0 notes
- `uv run python -c "from pcswitcher.jobs import FolderSyncJob; assert FolderSyncJob.name=='folder_sync'; print('ok')"` — ok
- `uv run pytest tests/unit tests/contract -q` — 450 passed (no regression)

## Commits

| Hash | Task | Description |
| ---- | ---- | ----------- |
| 6773f95 | RED (both tasks) | test(01-04): add failing tests for FolderSyncJob preflight and divergence guard |
| 8eb38cc | GREEN (both tasks) | feat(01-04): implement FolderSyncJob with validate() preflight and divergence guard |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed test assertion for shell-quoting check**
- **Found during:** Task 1 GREEN phase
- **Issue:** `test_folder_path_is_shell_quoted` asserted `'/home/user name' not in c` for the test -d commands. But `shlex.quote("/home/user name")` returns `'/home/user name'` (with surrounding single quotes), which does contain the substring `/home/user name`. The assertion was always false regardless of whether quoting was applied.
- **Fix:** Changed to the positive assertion: `expected_quoted in c` where `expected_quoted = shlex.quote("/home/user name")`. This verifies the shell-quoted form is present in the command.
- **Files modified:** `tests/unit/jobs/test_folder_sync.py`
- **Commit:** 8eb38cc

**2. [Scope note] Tasks 1 and 2 implemented together in a single commit**
- **Rationale:** The test file written in the RED phase covered both Task 1 (preflight) and Task 2 (divergence guard) behaviors together, since they are part of the same `validate()` method. The divergence guard tests import `pcswitcher.jobs.folder_sync` at the module level, so both feature sets needed to exist before any tests could pass. The RED commit covers the full test suite; the GREEN commit implements the full `validate()` including the divergence guard.

## Known Stubs

`FolderSyncJob.execute()` raises `NotImplementedError("FolderSyncJob.execute is implemented in plan 01-05")`. This is an intentional marked stub; it is not called by any test in this plan. Plan 05 fills the implementation.

## Threat Flags

None. No new network endpoints or auth paths introduced beyond what the plan's threat model already covers (T-04-01 shlex.quote applied; T-04-02 divergence guard implemented; T-04-02b WARNING audit trail implemented; T-04-03 acl validate() check implemented).

## Self-Check: PASSED

Files created/modified exist:
- src/pcswitcher/jobs/folder_sync.py — FOUND
- src/pcswitcher/jobs/__init__.py — FOUND
- tests/unit/jobs/test_folder_sync.py — FOUND

Task commits:
- 6773f95 — FOUND
- 8eb38cc — FOUND
