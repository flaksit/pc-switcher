---
phase: 02-package-management-sync
plan: 01
subsystem: infra
tags: [adr, apt, snap, flatpak, package-convergence, requirements, roadmap]

requires:
  - phase: 01-home-sync-mvp-user-data-sync
    provides: SyncJob base, ADR-018 path-export mechanism, ADR-014 dry-run contract, ADR-015 warn-and-confirm precedent
provides:
  - ADR-020, the durable record of the Phase 2 convergence model, item model, three-job split and PackagePhaseCoordinator plan()/apply() split
  - "/etc/apt" scope boundary moved from Phase 3 into Phase 2 in REQUIREMENTS.md and ROADMAP.md
affects: [02-02, 02-03, 02-04, 02-05, 02-06, 02-07, 02-08, 02-09, 02-10]

tech-stack:
  added: []
  patterns: []

key-files:
  created:
    - docs/adr/adr-020-declarative-package-convergence.md
  modified:
    - docs/adr/_index.md
    - .planning/REQUIREMENTS.md
    - .planning/ROADMAP.md

key-decisions:
  - "PackagePhaseCoordinator's plan()/apply() split is the structural fix for the cross-AI review's most severe finding (per-job self-contained review would let apt_sync mutate the target before snap_sync had diffed)."
  - "D-21/D-26 reconciled explicitly in the ADR: interactive runs fail the job result on any unresolved unreproducible item; non-interactive runs report but do not fail on unresolved items alone, since the user was never offered a chance to resolve them."

patterns-established:
  - "Two-phase SyncJob convergence (plan() capture+diff+review-build, apply() converge) for any future cross-manager batched-review job."

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "ADR-020 exists, is Accepted, and records the manifest-replay convergence model, item model, three-job split, plan()/apply() coordinator, and the machine-local decision file / snippet registry locations"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: other
        ref: "uv run codespell docs/adr/adr-020-declarative-package-convergence.md"
        status: pass
      - kind: manual_procedural
        ref: "grep for apt_sync, snap_sync, flatpak_sync, apt-mark showmanual, snap install --revision, /etc/apt/preferences.d, /etc/apt/trusted.gpg.d, ~/.config/pc-switcher, plan(), apply(), PackagePhaseCoordinator, sudo install, apt-get -s, apt-get update — all present"
        status: pass
    human_judgment: false
  - id: D2
    description: "REQUIREMENTS.md and ROADMAP.md both state the /etc/apt boundary move into Phase 2 without losing any phase or requirement entry"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: other
        ref: "grep -q '/etc/apt/sources.list.d' .planning/REQUIREMENTS.md; grep -c '^### Phase' .planning/ROADMAP.md == 7; grep -c '^- \\[' .planning/REQUIREMENTS.md == 19"
        status: pass
    human_judgment: false

duration: 9min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 01: ADR-020 and the /etc/apt Scope Boundary Summary

**ADR-020 records the declarative manifest-replay convergence model and the PackagePhaseCoordinator plan()/apply() split that fixes the cross-AI review's core defect; `/etc/apt` moves from Phase 3 into Phase 2 in REQUIREMENTS.md and ROADMAP.md.**

## Performance

- **Duration:** 9 min
- **Started:** 2026-07-23T00:56:00Z
- **Completed:** 2026-07-23T00:59:46Z
- **Tasks:** 2
- **Files modified:** 4 (1 created, 3 modified)

## Accomplishments

- ADR-020 written: convergence model (D-01), item model (D-02), three-job split with shared core (D-15/D-16/D-17), the `PackagePhaseCoordinator` `plan()`/`apply()` split (D-15+D-24) with the rejected per-job-self-contained-review alternative named explicitly, the three convergence-safety positions (staged `/etc/apt` writes via `sudo install`, transactional repo-group rollback, `apt-get -s` simulate-before-execute), and every other D-01..D-29 position.
- ADR-020 indexed in `docs/adr/_index.md` under Active Decisions, numeric order after ADR-019.
- `REQ-sync-scope-packages` now names the `/etc/apt` repository state it covers; `REQ-sync-scope-app-and-system-config`'s parenthetical excludes `/etc/apt` the same way it already excludes `/root`.
- `ROADMAP.md` Phase 2 and Phase 3 sections each carry a scope note stating where the `/etc/apt` boundary sits.

## Task Commits

1. **Task 1: Write ADR-020 — declarative package convergence** - `d288c1d` (docs)
2. **Task 2: Index ADR-020 and move the /etc/apt scope boundary into Phase 2** - `eeeec4a` (docs)

**Plan metadata:** (this commit)

## Files Created/Modified

- `docs/adr/adr-020-declarative-package-convergence.md` - New ADR recording the Phase 2 convergence model and coordinator decision
- `docs/adr/_index.md` - ADR-020 added to Active Decisions; Last-updated date refreshed
- `.planning/REQUIREMENTS.md` - REQ-sync-scope-packages and REQ-sync-scope-app-and-system-config text updated for the `/etc/apt` boundary move
- `.planning/ROADMAP.md` - Phase 2 and Phase 3 sections each gained a Scope note line

## Decisions Made

- Structured ADR-020's Decision section as one subsection per CONTEXT decision id, per the plan's explicit instruction, rather than the flatter prose the ADR-001 template's example implies — the plan's `<action>` block specified this level of traceability so a fresh implementer can trace any position back to its D-NN.
- Added an explicit "Alternatives Considered" section (precedented by ADR-002/ADR-003) rather than folding rejections inline into Decision subsections, since the plan's acceptance criteria required a distinct alternatives/rejected section naming three specific rejected approaches.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

ADR-020 is the durable reference every subsequent Phase 2 plan (02-02 through 02-13) must build on for the item model, job split, and coordinator mechanism. No blockers for 02-02 (`questionary` legitimacy checkpoint + batched-review primitive).

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: docs/adr/adr-020-declarative-package-convergence.md
- FOUND: .planning/phases/02-package-management-sync/02-01-SUMMARY.md
- FOUND: commit d288c1d
- FOUND: commit eeeec4a
