---
phase: 02-package-management-sync
plan: 14
subsystem: infra
tags: [adr, documentation, package-sync, architecture-record]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: shipped plans 02-01…02-13 (package-sync jobs, review, executor, /etc/apt convergence)
provides:
  - ADR-020 rewritten to record per-manager batched review as the design (no coordinator)
  - ADR-020 records four package jobs incl. manual_installs_sync with its own enable flag
  - ADR-020 records skip-once as a valid resolution and apt collateral auto/manual classification
  - ADR-020 records snippet transport via manual_installs_sync send_file(), jobs/packages/ layout, no-empty-config-section, per-job docs
  - docs/adr/_index.md ADR-020 summary realigned to the rewritten TL;DR
affects: [02-15, 02-16, 02-17, 02-18, 02-19, 02-20, 02-21]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ADR is corrected in place (not superseded) when it was written from decisions later found wrong; resulting state written as if always the decision"

key-files:
  created: []
  modified:
    - docs/adr/adr-020-declarative-package-convergence.md
    - docs/adr/_index.md

key-decisions:
  - "ADR-020 rewritten in place (Status unchanged, no ADR-021, no changelog) because the record was authored from corrected decisions, not superseded"
  - "PackagePhaseCoordinator removed from the durable record; each package job owns plan → review → apply inside its own execute()"

patterns-established:
  - "Per-manager batched review: no cross-manager coordinator; the review call stays inside each job's execute()"

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "ADR-020 records the corrected per-manager, four-job, self-pushing-snippet architecture with no coordinator"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: automated_ui
        ref: "grep -c 'PackagePhaseCoordinator\\|coordinate_package_review' docs/adr/adr-020-declarative-package-convergence.md == 0; literals manual_installs_sync, send_file, apt-mark showmanual, jobs/packages/ present"
        status: pass
    human_judgment: false
  - id: D2
    description: "docs/adr/_index.md ADR-020 summary agrees with the rewritten body (no coordinator/cross-manager-review wording)"
    verification:
      - kind: automated_ui
        ref: "grep -n 'adr-020' docs/adr/_index.md | grep -vqi 'coordinat'"
        status: pass
    human_judgment: false

# Metrics
duration: 3min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 14: Rewrite ADR-020 against corrected decisions Summary

**ADR-020 rewritten so the durable record encodes per-manager batched review, four package jobs, skip-once resolution, apt collateral auto/manual classification, and self-pushed snippet transport — with PackagePhaseCoordinator removed and the ADR index realigned.**

## Performance

- **Duration:** 3 min
- **Started:** 2026-07-23T18:15:52Z
- **Completed:** 2026-07-23T18:20:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Rewrote `docs/adr/adr-020-declarative-package-convergence.md` in place: deleted the coordinator subsection and both coordinator Implementation Rules; recorded that each package job runs plan → review → apply inside its own `execute()`, batched per manager and grouped by action, with no shared review phase and no coordinator.
- Applied all six corrections traceable to decision ids: per-manager review (D-24), four jobs incl. `manual_installs_sync` (D-15/D-18), skip-once as a third valid resolution (D-21), apt collateral auto-vs-manual classification at plan time (D-30), snippet transport via `manual_installs_sync`'s own `send_file()` (D-23), and the `jobs/packages/` layout / no-empty-config-section / per-job-docs rules (D-31/D-32/D-33).
- Replaced the `## Alternatives Considered` "per-job self-contained review — rejected" bullet with its inverse (a cross-manager coordinator, rejected); added the `job_results` exit-code Implementation Rule.
- Preserved every "Do Not Undo" invariant verbatim where possible (privileged `/etc/apt` staging + `sudo install` + `sudo mkdir`, `send_file` never outside home, transactional repo group around `apt-get update`, no `--delete` mirror, keys byte-for-byte, no `snap refresh --hold`, machine-local decision files unsynced, source-side sudo validation, simulate fails closed, `dpkg --compare-versions`, snap header-name parsing).
- Realigned the ADR-020 entry in `docs/adr/_index.md` to match the rewritten TL;DR, removing all coordinator / cross-manager-review wording.

## Task Commits

Each task was committed atomically:

1. **Task 1: Rewrite ADR-020 against the corrected decisions** - `0d86bb1` (docs)
2. **Task 2: Realign the ADR index summary** - `0f4f578` (docs)

## Files Created/Modified

- `docs/adr/adr-020-declarative-package-convergence.md` - Full rewrite recording the corrected per-manager, four-job architecture; coordinator removed; Do-Not-Undo invariants retained.
- `docs/adr/_index.md` - ADR-020 one-line summary realigned to the rewritten TL;DR.

## Decisions Made

- Corrected the ADR in place rather than superseding it: the record was written from decisions later found wrong, so `Status: Accepted`, the date and the section skeleton are unchanged and no changelog/"previously said" narration was added — the resulting state reads as if it had always been the decision (per plan action and ADR-001 immutability read as "correct the record that was never right").

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `state.advance-plan` recorded `Plan: 2 of 21` in STATE.md rather than an expected `15 of 21`. This is the canonical GSD handler's computed output for a delta-replan (plans 01-13 shipped, 14-21 are the delta); left as produced by the SDK handler rather than hand-edited, since STATE plan sequencing is owned by the handler and the execute-phase orchestrator. Flagged here for visibility; no artifact corruption — completion, progress, and session fields updated correctly.
- `requirements.mark-complete` reported both REQ-sync-scope-packages and REQ-conflict-detection-no-resolution as `already_complete` (marked by earlier shipped plans). No change needed; no corruption.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- The durable architecture record now matches `02-CONTEXT.md`. Plans 02-15 through 02-21 can implement against ADR-020 without encountering a contradicting coordinator record.
- No blockers.

## Self-Check: PASSED

All modified files exist on disk; both task commits (`0d86bb1`, `0f4f578`) are present in git history.

---
*Phase: 02-package-management-sync*

*Completed: 2026-07-23*
