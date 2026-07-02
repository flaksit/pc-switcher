---
phase: 01-home-sync-mvp-user-data-sync
plan: 14
subsystem: docs-and-tests
tags: [adr-015, topology-model, deletion-audit, readme, integration-tests, unit-tests]
dependency_graph:
  requires: [01-11, 01-13]
  provides: [deletion-audit-test, topology-integration-test, readme-pivot-docs]
  affects: [README.md, tests/unit/jobs/test_folder_sync_deletion_log.py, tests/integration/test_folder_sync.py]
tech_stack:
  added: []
  patterns: [QueueListener flush before read, AsyncGenerator fake for unit tests, logger state restore in test teardown]
key_files:
  created:
    - tests/unit/jobs/test_folder_sync_deletion_log.py
  modified:
    - README.md
    - tests/integration/test_folder_sync.py
decisions:
  - "T-01-14-01 closed: _stream_rsync *deleting line → pcswitcher.jobs.base logger → QueueHandler → QueueListener → FileHandler(JSON lines) — proven by test driving real logging"
  - "Integration test round-trip uses --allow-out-of-order only on the first A→B (W1: no prior history); B→A and second A→B proceed without override (ADR-015 clean case)"
  - "LogConfig().file defaults to 10 (DEBUG) which is <= 15 (FULL); assertion added to catch future regressions"
metrics:
  duration: "6 minutes"
  completed_date: "2026-07-02"
  tasks_completed: 3
  files_modified: 3
status: complete
---

# Phase 01 Plan 14: Documentation and Test Pivot Closure Summary

Pivot closure for ADR-015: accurate README, proven deletion audit trail, and topology-aligned integration test.

## What Was Built

### Task 1 — README sync sequence corrected (commit e13bb63)

Updated "## What Happens During a Sync" to match the post-pivot orchestrator:

- Removed step 2 "Consecutive-sync check" and all `--allow-consecutive` references.
- Inserted step 5 "Out-of-order / target-state check" (after target lock): describes W1/W2/W3 scenarios, `--allow-out-of-order`, dry-run behaviour, and GitHub #159 guarantee.
- Renumbered subsequent steps 5-13 → 6-13.
- Removed the `validate()` btrfs `find-new` paragraph; now describes the three actual checks: `sudo rsync`, `acl`, folder existence.
- Updated step 13 to describe `last_role`/`last_peer` recording instead of SOURCE/TARGET marker.
- Replaced closing note: `--allow-divergence` → `--allow-out-of-order`; added dry-run deletion log mention.

### Task 2 — Deletion audit trail proven (commit 2b8a4ee)

New file `tests/unit/jobs/test_folder_sync_deletion_log.py`:

- `TestDeletionLogPersistence::test_deletion_persisted_at_full_in_real_run` — drives `_stream_rsync` with `dry_run=False` and asserts a JSON record `{"level": "FULL", "event": "... /home/user/old_secret.txt"}` is present in the log file.
- `TestDeletionLogPersistence::test_deletion_persisted_at_full_in_dry_run` — same with `dry_run=True`, proving persistence is mode-independent.
- `TestDefaultLogFloor::test_default_file_log_floor_is_at_or_below_full` — asserts `LogConfig().file` (default 10/DEBUG) ≤ 15 (FULL), catching any future regression that would silently drop deletion records.

Key design: uses real `setup_logging` / `QueueListener`, stops the listener before reading the file (ensures flush), and restores logger state after each test to prevent handler accumulation.

### Task 3 — Integration test aligned to topology model (commit d6f4b1f)

Updated `tests/integration/test_folder_sync.py`:

- Module docstring: replaced btrfs divergence guard criterion 5 with topology model (ADR-015, D-12) description.
- `test_a_to_b_content_metadata_and_exclusions`: added `--allow-out-of-order` to the A→B sync command with inline comment explaining W1 (no prior history); content/metadata assertions unchanged.
- `test_round_trip_and_no_false_divergence`: added `--allow-out-of-order` to first A→B only; B→A and second A→B proceed without override; replaced the old btrfs/prefix-scoping comment with a topology explanation; updated step 5 assertion text.
- Replaced `test_divergence_guard_and_dry_run` with `test_out_of_order_and_dry_run`: covers (1) W1 non-interactive returns non-zero + "out-of-order"/"target" in output, (2) `--dry-run` proceeds without mutations or history update, (3) `--allow-out-of-order` populates target.
- Removed unused `import json` (was used by removed `_get_gen` helper).

## Deviations from Plan

None — plan executed exactly as written.

## Verification

- `grep -q "allow-out-of-order" README.md && ! grep -nE "allow-divergence|allow-consecutive|Consecutive-sync check|find-new" README.md` — PASS
- `uv run pytest tests/unit/jobs/test_folder_sync_deletion_log.py -q` — 3 passed
- `uv run basedpyright tests/unit/jobs/test_folder_sync_deletion_log.py` — 0 errors
- `uv run pytest tests/integration/test_folder_sync.py --collect-only -q -o "addopts="` — 3 tests collected
- `! grep -nE "allow-divergence|allow_divergence|find-new" tests/integration/test_folder_sync.py` — PASS
- `uv run pytest tests/unit/ -q` — 436 passed

## Self-Check

All 3 commits verified in git log. All 3 key files on disk.
