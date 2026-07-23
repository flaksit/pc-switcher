---
phase: 02-package-management-sync
plan: 15
subsystem: infra
tags: [package-sync, review, dependency-injection, asyncio, refactor]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: PackageSyncJob plan()/apply() split, review_items, PackagePhaseCoordinator (now removed), JobContext.confirmer injection precedent
provides:
  - "Reviewer protocol + TerminalUIReviewer adapter injected through JobContext.reviewer"
  - "Self-contained PackageSyncJob.execute() that plans, reviews and applies per manager"
  - "Removal of PackagePhaseCoordinator, coordinate_package_review, record_plan_failure and the package_phase module"
affects: [02-20 docs cleanup, 02-21 integration suite rework, snap_sync, flatpak_sync, apt_sync]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-job review injection mirroring the Confirmer/TerminalUIConfirmer seam (JobContext.reviewer, constructed once by the orchestrator)"
    - "Self-contained SyncJob.execute(): plan -> review -> accept_review -> apply, with plan() failures propagating to the job's own JobResult"

key-files:
  created: []
  modified:
    - src/pcswitcher/jobs/package_review.py
    - src/pcswitcher/jobs/context.py
    - src/pcswitcher/jobs/package_sync_core.py
    - src/pcswitcher/orchestrator.py
    - tests/unit/jobs/test_package_sync_core.py
    - tests/unit/jobs/test_package_review.py
    - tests/unit/jobs/test_apt_sync.py
    - tests/unit/jobs/test_snap_sync.py
    - tests/unit/jobs/test_flatpak_sync.py

key-decisions:
  - "Corrected D-24: batching is per manager, not across managers. A cross-manager review coordinator contradicts the D-15 job independence and is removed, never to be reintroduced."
  - "execute() always calls reviewer.review(plan.groups) once, even for zero diffs (empty group tuple), rather than short-circuiting — the behaviour is pinned by test."
  - "A missing reviewer fails loudly via assert at execute() (T-02-38), never silently applying unreviewed diffs."

patterns-established:
  - "Reviewer injection seam: JobContext.reviewer is optional (None default) so lightweight test contexts omit it; jobs that review assert it is set."
  - "plan() failures propagate naturally out of execute() into the orchestrator's per-job exception handling — no stored-and-re-raised failure state."

requirements-completed: [REQ-conflict-detection-no-resolution, REQ-sync-scope-packages]

coverage:
  - id: D1
    description: "PackageSyncJob.execute() is self-contained: plan -> review -> accept_review -> apply, reached through the injected reviewer"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestExecuteSelfContained::test_call_order_is_plan_review_accept_review_apply"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestExecuteSelfContained::test_zero_diff_run_still_calls_review_once"
        status: pass
    human_judgment: false
  - id: D2
    description: "A job constructed without a reviewer fails loudly at execute() and issues no converge command (T-02-38)"
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestExecuteSelfContained::test_missing_reviewer_raises_and_issues_no_converge"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestPlanApplySplit::test_execute_without_a_reviewer_raises_and_issues_no_command"
        status: pass
    human_judgment: false
  - id: D3
    description: "A plan() failure propagates unchanged out of execute() to this job's own JobResult"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestExecuteSelfContained::test_plan_failure_propagates_out_of_execute_unchanged"
        status: pass
    human_judgment: false
  - id: D4
    description: "PackagePhaseCoordinator, coordinate_package_review, record_plan_failure and the package_phase module are gone from src and every import"
    verification:
      - kind: other
        ref: "grep -rn 'package_phase|coordinate_package_review|PackagePhaseCoordinator|record_plan_failure' src/ (returns nothing)"
        status: pass
    human_judgment: false
  - id: D5
    description: "TerminalUIReviewer forwards console/ui/logger to review_items and preserves the pause/resume finally when the prompt raises"
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestTerminalUIReviewer::test_review_forwards_console_ui_logger_and_returns_outcome_unchanged"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestTerminalUIReviewer::test_pause_and_resume_both_run_when_the_underlying_prompt_raises"
        status: pass
    human_judgment: false
  - id: D6
    description: "Review groups emit in fixed order install, change, remove, report — stable across runs over the same diff set"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestReviewGroupsByAction::test_group_emission_order_is_install_change_remove_report"
        status: pass
    human_judgment: false

# Metrics
duration: 45min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 15: Per-Manager Review Inside execute() Summary

**Removed the cross-manager PackagePhaseCoordinator and gave every package job its own batched review inside its own `execute()` via a `Reviewer` protocol injected through `JobContext`, per the corrected D-24.**

## Performance

- **Duration:** ~45 min
- **Completed:** 2026-07-23
- **Tasks:** 2
- **Files modified:** 9 modified, 2 deleted

## Accomplishments
- Added a `Reviewer` protocol and `TerminalUIReviewer` adapter in `package_review.py`, mirroring the existing `Confirmer`/`TerminalUIConfirmer` injection shape.
- Added the `JobContext.reviewer` injection seam (optional, `None` default).
- Rewrote `PackageSyncJob.execute()` to be self-contained — plan, review through the injected reviewer, accept, apply — deleting the coordinator-accepted-plan guard, `record_plan_failure` and `_plan_failure`.
- Removed `PackagePhaseCoordinator`, `coordinate_package_review` and the `package_phase.py` module; the orchestrator now constructs one `TerminalUIReviewer` and its `_execute_jobs` is back to the plain job loop (the per-job `PackageItemFailures` continuation chain is kept).
- Reworked every package test onto the per-job path: apt tests drive `execute()` through an injected `FakeReviewer`; snap/flatpak drop their coordinator-integration tests; surviving-subject tests (group order, `PackageItemFailures` continuation, `enabled_sync_jobs`) were re-homed.

## Task Commits

1. **Task 1: Reviewer injection + self-contained execute() (tracer, tdd)** - `c800075` (refactor)
2. **Task 2: Rework every test that assumed a coordinator (tdd)** - `fa51ec6` (test, deletion) + `dc19aa7` (test, reworks)

_Task 2 landed as two commits: the `git rm` of `test_package_phase.py` (`fa51ec6`) and the reworks of the surviving test modules (`dc19aa7`). See Issues Encountered._

## Files Created/Modified
- `src/pcswitcher/jobs/package_review.py` - Added `Reviewer` protocol + `TerminalUIReviewer` adapter; refreshed the coordinator-referencing docstring.
- `src/pcswitcher/jobs/context.py` - Added `reviewer: Reviewer | None = None`.
- `src/pcswitcher/jobs/package_sync_core.py` - Self-contained `execute()`; removed `record_plan_failure`/`_plan_failure`; rewrote module/plan/accept_review docstrings.
- `src/pcswitcher/orchestrator.py` - Construct the single `TerminalUIReviewer`, pass `reviewer` into `JobContext`, drop the coordinator call and its import from `_execute_jobs`.
- `src/pcswitcher/jobs/package_phase.py` - Deleted.
- `tests/unit/jobs/test_package_sync_core.py` - `FakeReviewer`, execute() call-order/missing-reviewer/plan-propagation tests, fixed group-order test, re-homed continuation + `enabled_sync_jobs` tests.
- `tests/unit/jobs/test_package_review.py` - `TerminalUIReviewer` forwarding + pause/resume tests.
- `tests/unit/jobs/test_apt_sync.py` - Driven through injected `FakeReviewer`; missing-reviewer test replaces the coordinator guard.
- `tests/unit/jobs/test_snap_sync.py`, `test_flatpak_sync.py` - Removed `TestCoordinatorIntegration` and coordinator imports.
- `tests/unit/jobs/test_package_phase.py` - Deleted.

## Decisions Made
- Kept the review call unconditional (`reviewer.review(plan.groups)` even for zero diffs) rather than short-circuiting; the behaviour is pinned by `test_zero_diff_run_still_calls_review_once`.
- Re-homed the `PackageItemFailures` continuation and `enabled_sync_jobs` tests into `test_package_sync_core.py` (an in-scope `files_modified` file) rather than a new orchestrator test file, since those subjects survive the coordinator removal and the plan's scope forbids new files.

## Deviations from Plan

None - plan executed exactly as written. The stale `PackagePhaseCoordinator` references remaining in `docs/system/architecture.md`, `docs/system/core.md` and `tests/integration/jobs/test_package_sync.py` are deliberately owned by later plans in this phase (02-20 docs cleanup, 02-21 integration rework) and were left untouched per this plan's `files_modified` scope.

## Issues Encountered
- The Task 2 commit was intended as a single commit, but the initial `git add` listed the already-`git rm`-ed `test_package_phase.py` path, which aborted the stage; the commit therefore captured only the staged deletion (`fa51ec6`). Rather than rewrite history (per convention), the reworked test modules were committed as a companion follow-up (`dc19aa7`). Both are part of Task 2.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Unit gate fully green: `uv run pytest` (985 passed), `ruff check`, `ruff format --check`, `basedpyright` all clean.
- Ready for 02-16+ execution. Downstream: 02-20 must scrub the coordinator from `docs/system/*` and per-job docs; 02-21 must rework `tests/integration/jobs/test_package_sync.py`, which still references the removed coordinator.

## Self-Check: PASSED

- `src/pcswitcher/jobs/package_phase.py` and `tests/unit/jobs/test_package_phase.py` confirmed absent.
- Commits `c800075`, `fa51ec6`, `dc19aa7` confirmed present in git log.
- `from pcswitcher.jobs.package_review import Reviewer, TerminalUIReviewer` imports cleanly; `JobContext.reviewer` field present.
- Full unit gate green: 985 passed; `ruff check`, `ruff format --check`, `basedpyright` all clean.

---
*Phase: 02-package-management-sync*

*Completed: 2026-07-23*
