---
phase: 01-home-sync-mvp-user-data-sync
plan: 11
subsystem: folder-sync
tags: [divergence-guard-removal, bug-fix, test-pruning, refactor]
dependency_graph:
  requires: []
  provides: [pure-rsync-mirror, wr-01-fix]
  affects: [folder_sync, config_sync, folder_sync_tests, contract_tests]
tech_stack:
  added: []
  patterns: [ADR-015 topology-based safety model]
key_files:
  created: []
  modified:
    - src/pcswitcher/jobs/folder_sync.py
    - src/pcswitcher/config_sync.py
    - tests/unit/jobs/test_folder_sync.py
    - tests/contract/test_job_interface.py
decisions:
  - "CR-01 and CR-02 resolved by removing the btrfs content-divergence guard entirely; safety now relies on snapshots + dry-run deletion log + orchestrator topology check (ADR-015)"
  - "WR-01 fixed: removeprefix('~/') correctly strips only the two-character prefix; lstrip stripped any leading run of ~ and / characters"
metrics:
  duration: 10min
  completed: 2026-07-02
  tasks: 3
  files: 4
status: complete
---

# Phase 01 Plan 11: Divergence Guard Removal and WR-01 Fix Summary

Removed the btrfs content-based target-divergence guard from FolderSyncJob and fixed the config_sync path-derivation bug (WR-01), pruning all related tests. FolderSyncJob is now a pure rsync mirror whose safety comes from the ADR-015 topology model.

## Tasks Completed

| # | Task | Commit | Files |
|---|------|--------|-------|
| 1 | Remove divergence guard from FolderSyncJob | a02db77 | src/pcswitcher/jobs/folder_sync.py |
| 2 | WR-01 config_sync prefix-strip fix | a1536a6 | src/pcswitcher/config_sync.py |
| 3 | Prune obsolete folder_sync and contract tests | d0678a8 | tests/unit/jobs/test_folder_sync.py, tests/contract/test_job_interface.py |

## Deviations from Plan

None — plan executed exactly as written.

## Verification Results

- `grep -nE "DivergenceStatus|_target_diverged_since|_check_divergence|..."` over `folder_sync.py` returns no matches.
- `basedpyright` reports 0 errors, 0 warnings on both modified source files.
- `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract/test_job_interface.py` — 59 passed.
- `config_sync.py` now uses `CONFIG_REMOTE_PATH.removeprefix("~/")` and has no `lstrip` for this path.

## Known Stubs

None.

## Threat Flags

None — no new network endpoints, auth paths, or schema changes introduced. The removal of the divergence guard is accounted for in the plan's threat model (T-01-11-01): residual risk is covered by btrfs snapshots, the dry-run deletion log (plan 01-14), and the topology warn+confirm (plan 01-13).

## Self-Check: PASSED

All 4 files exist on disk. All 3 task commits verified in git log.
