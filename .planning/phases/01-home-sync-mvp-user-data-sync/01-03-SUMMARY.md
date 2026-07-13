---
phase: 01-home-sync-mvp-user-data-sync
plan: "03"
subsystem: sync-state
tags: [allow_divergence, dry-run, sync-history, divergence-guard, merge-preserving]
dependency_graph:
  requires: []
  provides:
    - JobContext.allow_divergence field
    - CLI --allow-divergence flag
    - Orchestrator allow_divergence parameter
    - sync_history.get_target_generation
    - sync_history.set_target_generation
    - sync-history.json target_generations key (backward-compatible)
  affects:
    - src/pcswitcher/jobs/context.py
    - src/pcswitcher/cli.py
    - src/pcswitcher/orchestrator.py
    - src/pcswitcher/sync_history.py
tech_stack:
  added: []
  patterns:
    - merge-preserving read-modify-write (atomic temp+rename)
    - python3 -c remote script for merge-safe SSH-executed state update
key_files:
  created: []
  modified:
    - src/pcswitcher/jobs/context.py
    - src/pcswitcher/cli.py
    - src/pcswitcher/orchestrator.py
    - src/pcswitcher/sync_history.py
    - tests/unit/test_dry_run.py
    - tests/unit/test_sync_history.py
    - tests/unit/orchestrator/test_consecutive_sync.py
decisions:
  - "Remote role-record command uses python3 -c script instead of echo-overwrite so it can read-merge-write the file on the target rather than overwrite it"
metrics:
  duration: "11 minutes"
  completed: "2026-06-30"
  tasks_completed: 2
  tasks_total: 2
  files_changed: 7
status: complete
---

# Phase 01 Plan 03: Cross-Cutting State and Flags Summary

One-liner: allow_divergence flag plumbed CLI→Orchestrator→JobContext like dry_run; dry-run no longer writes sync-history; sync_history extended with merge-preserving target_generations store and remote command.

## What Was Built

### Task 1: allow_divergence plumbing + dry-run sync-history guard

Added `JobContext.allow_divergence: bool = False` (frozen field, after `dry_run`). Wired `--allow-divergence` as a Typer option on `pc-switcher sync`, threaded through `_run_sync` → `_async_run_sync` → `Orchestrator(...)` exactly as `--allow-consecutive` and `dry_run` are handled. Added `allow_divergence: bool = False` kwarg to `Orchestrator.__init__`, stored as `self._allow_divergence`, and forwarded to `JobContext(...)` in `_create_job_context`.

Guarded the success-path `await self._update_sync_history()` call with `if not self._dry_run:` — a dry run now completes the full validate/lock/snapshot/execute path but does not write sync-history.json on either machine (D-12).

Extended `test_dry_run.py` with `TestJobContextAllowDivergenceField` (two tests: default False, settable True) and `TestOrchestratorDryRunPropagation.test_orchestrator_accepts_allow_divergence_parameter`.

### Task 2: target_generations divergence-marker store

Extended `sync_history.py` with:

- `get_target_generation(target_hostname, path) -> int | None` — reads `target_generations[host][path]` from sync-history.json; returns None for missing file, old-format file (only `last_role`), missing keys, or corrupt data.
- `set_target_generation(target_hostname, path, generation) -> None` — read-modify-write: loads existing JSON (treat missing/corrupt as {}), sets nested value, preserves `last_role` and all other targets/paths, writes atomically via temp+rename.
- Made `record_role` merge-preserving: reads existing data first, spreads it into the new dict with `{**existing, "last_role": role.value}`, so `target_generations` (written earlier by FolderSyncJob) survives a role-record call. Prevents Pitfall 4 false-divergence after role switch.
- Made `get_record_role_command` merge-preserving: now returns a `python3 -c "..."` script instead of `echo '...' > file`. The script reads the existing remote file, updates only `last_role`, and writes atomically via temp+rename. Python's single-quoted string literals in the script are safely wrapped in shell double quotes.

Extended `test_sync_history.py` with `TestGetTargetGeneration` (6 tests), `TestSetTargetGeneration` (4 tests), `TestRecordRoleMergePreserving` (2 tests), and updated `TestGetRecordRoleCommand` with 2 new subprocess-executed tests that verify the remote command is merge-preserving.

## Verification Results

All plan verification commands green:

- `uv run pytest tests/unit/test_dry_run.py tests/unit/test_sync_history.py -q` — 41 passed
- `uv run basedpyright src/pcswitcher/sync_history.py src/pcswitcher/jobs/context.py src/pcswitcher/orchestrator.py src/pcswitcher/cli.py` — 0 errors
- `uv run pytest tests/unit tests/contract -q` — 427 passed (no regression)

## Commits

| Hash | Task | Description |
|------|------|-------------|
| bca397a | Task 1 | feat(01-03): plumb allow_divergence flag and dry-run sync-history guard |
| bbfe702 | Task 2 | feat(01-03): add target_generations store with merge-preserving writes |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated test_consecutive_sync.py to match new remote command format**
- **Found during:** Task 2 full-suite regression run
- **Issue:** `test_updates_remote_history_to_target` asserted `'"last_role": "target"' in cmd`, which matched the old `echo '{"last_role": "target"}' > ...` command but not the new python3-based merge-preserving command.
- **Fix:** Updated assertion to check `"python3" in cmd`, `"last_role" in cmd`, `"target" in cmd` — these all hold for the new command.
- **Files modified:** `tests/unit/orchestrator/test_consecutive_sync.py`
- **Commit:** bbfe702

None of the `must_haves.prohibitions` were violated: dry run does not write sync-history.json (guarded at orchestrator level); record_role and the remote role-record command both merge-preserve target_generations.

## Known Stubs

None. The new functions (`get_target_generation`, `set_target_generation`) are fully implemented and tested. No hardcoded empty values flow to callers.

## Threat Flags

None. No new network endpoints or auth paths introduced. The `--allow-divergence` flag is an explicit opt-in override (T-03-01 disposition: accept — plan 04 adds WARNING-level logging of the decision).

## Self-Check: PASSED

Files modified exist:
- src/pcswitcher/jobs/context.py — FOUND
- src/pcswitcher/cli.py — FOUND
- src/pcswitcher/orchestrator.py — FOUND
- src/pcswitcher/sync_history.py — FOUND
- tests/unit/test_dry_run.py — FOUND
- tests/unit/test_sync_history.py — FOUND
- tests/unit/orchestrator/test_consecutive_sync.py — FOUND

Task commits:
- bca397a — FOUND
- bbfe702 — FOUND
