---
phase: 01-home-sync-mvp-user-data-sync
plan: 15
subsystem: orchestrator
tags: [rsync, first-sync, adr-015, job-architecture]

requires:
  - phase: 01-home-sync-mvp-user-data-sync
    provides: FolderSyncJob, the orchestrator's topology out-of-order/first-sync pre-flight check, and the shared Confirmer abstraction (plan 01-13)
provides:
  - FirstSyncScope dataclass (job_name, scope_items, mechanism)
  - SyncJob.describe_first_sync_scope() hook (defaults to None)
  - FolderSyncJob.describe_first_sync_scope() override naming its own scope and mechanism
  - Orchestrator._first_sync_scopes() + shared _resolve_sync_job_class() helper
  - Job-agnostic _confirm_first_sync() warning composition with generic fallback
affects: [future non-rsync sync jobs (e.g. packages/docker) added to first-sync scope]

tech-stack:
  added: []
  patterns:
    - "SyncJob self-description hook: a job describing its own scope/behavior via a classmethod, called before instantiation, so orchestrator-level messaging never hardcodes a specific job's config shape"

key-files:
  created:
    - tests/unit/orchestrator/test_first_sync_scope.py
  modified:
    - src/pcswitcher/models.py
    - src/pcswitcher/jobs/base.py
    - src/pcswitcher/jobs/folder_sync.py
    - src/pcswitcher/orchestrator.py
    - tests/unit/jobs/test_folder_sync.py

key-decisions:
  - "FirstSyncScope.mechanism for FolderSyncJob is the literal phrase 'rsync --delete', matching what the orchestrator previously hardcoded, but now owned by the job itself"
  - "_resolve_sync_job_class() factored out of _discover_and_validate_jobs so the dynamic-import + class-scan logic used by both Phase 4 job discovery and pre-Phase-4 first-sync scope collection lives in exactly one place"
  - "Generic fallback phrasing is '(all data configured for sync)' when no enabled job describes a first-sync scope"

patterns-established:
  - "Pattern: orchestrator-level generic messaging is composed from per-job self-description hooks (classmethods returning None by default) rather than reading a specific job's config dict, keeping the orchestrator job-agnostic per ADR-015"

requirements-completed:
  - REQ-manual-sync-workflow
  - REQ-terminal-ux

coverage:
  - id: D1
    description: "FirstSyncScope contract and SyncJob.describe_first_sync_scope hook (defaults to None) added to models.py/jobs/base.py"
    requirement: "REQ-manual-sync-workflow"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestDescribeFirstSyncScope"
        status: pass
    human_judgment: false
  - id: D2
    description: "FolderSyncJob.describe_first_sync_scope() returns enabled folder paths + mechanism, None when nothing is in scope"
    requirement: "REQ-manual-sync-workflow"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestDescribeFirstSyncScope"
        status: pass
    human_judgment: false
  - id: D3
    description: "Orchestrator composes the first-sync warning from _first_sync_scopes() instead of reading folder_sync's config dict or naming rsync directly; falls back to generic phrasing when no job is in scope"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_first_sync_scope.py::TestFirstSyncScopesFolderSync"
        status: pass
      - kind: unit
        ref: "tests/unit/orchestrator/test_first_sync_scope.py::TestFirstSyncScopesEmptyFallback"
        status: pass
      - kind: other
        ref: "grep -nE 'folder_sync|rsync --delete' src/pcswitcher/orchestrator.py (exit 1, no matches)"
        status: pass
    human_judgment: false
  - id: D4
    description: "Extensibility proven: a stub non-rsync SyncJob's FirstSyncScope flows through the composed warning unchanged, with no orchestrator code change"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_first_sync_scope.py::TestFirstSyncScopesExtensibility::test_stub_non_rsync_job_surfaces_in_warning"
        status: pass
    human_judgment: false
  - id: D5
    description: "Pre-existing first-sync/out-of-order flow (_check_out_of_order, W1/W2/W3) is unbroken by the refactor"
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_consecutive_sync.py (23 tests, all pass)"
        status: pass
    human_judgment: false

duration: 6min
completed: 2026-07-03
status: complete
---

# Phase 01 Plan 15: Job-Agnostic First-Sync Warning Summary

**The first-sync overwrite warning is now assembled from each in-scope SyncJob's own `FirstSyncScope` self-description instead of the orchestrator reading `folder_sync`'s config dict and hardcoding "rsync --delete" wording.**

## Performance

- **Duration:** 6 min
- **Tasks:** 3
- **Files modified:** 6 (4 source, 2 test; 1 new test file)

## Accomplishments
- Added `FirstSyncScope` (job_name, scope_items, mechanism) to `models.py` and a `SyncJob.describe_first_sync_scope()` hook that defaults to `None`
- `FolderSyncJob` overrides the hook, reproducing the enabled-folder filter as a classmethod (callable before job instances exist) and naming its own `rsync --delete` mechanism
- Removed `Orchestrator._first_sync_scope()` (which read `folder_sync`'s config directly); added `_first_sync_scopes()` which resolves each enabled job via a new shared `_resolve_sync_job_class()` helper — the same dynamic-import/class-scan logic `_discover_and_validate_jobs` already used, now factored out to avoid duplication
- `_confirm_first_sync()` composes its "In scope" block from each job's self-description and falls back to generic phrasing ("(all data configured for sync)") when no enabled job describes a scope; the orchestrator no longer contains the literal `folder_sync` or `rsync --delete` (grep-verified)
- New tests prove: the real `folder_sync` job's scope surfaces correctly, the empty-fallback path names no transport mechanism, and a stub non-rsync job's self-description flows through the composed warning unchanged — proving a future job (e.g. packages/docker) needs zero orchestrator changes

## Task Commits

1. **Task 1: Add the FirstSyncScope contract and the SyncJob self-description hook** - `fe38c7f` (feat)
2. **Task 2: Assemble the orchestrator first-sync warning from job self-descriptions** - `0c57104` (refactor)
3. **Task 3: Tests for the job-agnostic first-sync scope** - `a8e84ca` (test)

**Plan metadata:** (this commit)

## Files Created/Modified
- `src/pcswitcher/models.py` - Added `FirstSyncScope` frozen dataclass
- `src/pcswitcher/jobs/base.py` - Added `SyncJob.describe_first_sync_scope()` classmethod hook (default `None`)
- `src/pcswitcher/jobs/folder_sync.py` - Added `describe_first_sync_scope()` override naming enabled folder paths + `rsync --delete`
- `src/pcswitcher/orchestrator.py` - Removed `_first_sync_scope()`; added `_resolve_sync_job_class()` (shared with `_discover_and_validate_jobs`) and `_first_sync_scopes()`; rewrote `_confirm_first_sync()`'s warning composition
- `tests/unit/jobs/test_folder_sync.py` - Added `TestDescribeFirstSyncScope` (populated, disabled, empty, missing-key configs)
- `tests/unit/orchestrator/test_first_sync_scope.py` - New file: `_first_sync_scopes()` + composed-warning tests, including a hermetic stub non-rsync job

## Decisions Made
- Kept the mechanism phrase for `FolderSyncJob` as the literal `"rsync --delete"` — identical wording to what the orchestrator previously hardcoded, just relocated to the job that owns the behavior
- Factored `_resolve_sync_job_class()` out of `_discover_and_validate_jobs` rather than duplicating the dynamic-import/class-scan logic in `_first_sync_scopes()`, per the plan's explicit instruction to avoid duplication and keep `_discover_and_validate_jobs`'s existing log messages/behavior intact
- Chose `"(all data configured for sync)"` as the generic fallback line (reworded from the old `"(all folders configured for sync)"`) so it makes no reference to folders/files specifically, since a future non-folder job could be the one contributing (or not contributing) scope

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- UAT gap 1 (first-sync warning naming `folder_sync`-specific details) is closed; the warning is now verified job-agnostic by both a grep check and a hermetic extensibility test
- No blockers for the remaining UAT items (tests 3-6 in `01-UAT.md`, still pending human verification)

---
*Phase: 01-home-sync-mvp-user-data-sync*
*Completed: 2026-07-03*

## Self-Check: PASSED

- FOUND: `.planning/phases/01-home-sync-mvp-user-data-sync/01-15-SUMMARY.md`
- FOUND: `fe38c7f` (Task 1 commit)
- FOUND: `0c57104` (Task 2 commit)
- FOUND: `a8e84ca` (Task 3 commit)
