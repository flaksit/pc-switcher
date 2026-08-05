---
phase: 02-package-management-sync
plan: 16
subsystem: package-sync
tags: [apt, collateral, apt-mark, review, questionary, transaction-guard]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: apt transaction simulation, batched TUI review, PackageSyncJob plan/apply split, AptSyncJob converge guards
provides:
  - Provenance-based apt collateral classification (D-30): auto proceeds silently, manual becomes a review item
  - COLLATERAL_REVIEW_ACTION review group with a three-way install-anyway / skip / abort resolution
  - Apply-time converge guards that honour an approved manual-collateral removal while still refusing unreviewed drift
affects: [apt_sync, package_review, verify-work, phase-02-verification]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Provenance split by apt-mark showmanual: a single target-side query is the source of the auto-vs-manual decision, matching apt's own notion of removable packages"
    - "Review-layer sentinel action (COLLATERAL_REVIEW_ACTION) drives a per-entry three-way select; the caller maps the recorded decision onto the triggering install"

key-files:
  created: []
  modified:
    - src/pcswitcher/jobs/apt_sync.py
    - src/pcswitcher/jobs/package_review.py
    - tests/unit/jobs/test_apt_sync.py
    - tests/unit/jobs/test_package_review.py

key-decisions:
  - "Collateral review entries carry a synthetic apt:collateral:<pkg> item_id; AptSyncJob.accept_review translates the recorded decision (install-anyway -> approved-collateral + install proceeds; skip -> SKIP_ONCE on the triggering installs). The batched simulation cannot attribute collateral to a single install, so skip un-approves the candidate set that produced it — conservative and clean, never a guard failure."
  - "Apply-time guards classify each removed/downgraded package against the target manual set: auto proceeds (D-30), manual is refused unless approved-collateral or an approved primary removal. This is what lets an install whose only collateral is auto deps finally proceed."
  - "_converge_remove relaxed symmetrically with _converge_install: auto reverse-deps proceed, manual unreviewed removals still refused — keeping plan-time classification and apply-time guard in agreement for the remove simulation too."

patterns-established:
  - "Drift tests use a call-count side_effect so the same apt-get -s command returns a clean plan-time preview and a collateral apply-time preview, proving the guard is a genuine last line of defence."

requirements-completed: [REQ-conflict-detection-no-resolution, REQ-sync-scope-packages]

coverage:
  - id: D1
    description: "A manual-collateral removal or downgrade (in the target apt-mark showmanual set) becomes exactly one COLLATERAL_REVIEW_ACTION review item; an auto-collateral one produces nothing and the install stays approvable."
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestPlanTimeCollateral"
        status: pass
    human_judgment: false
  - id: D2
    description: "Three-way resolution: install-anyway lets the triggering install proceed with the guard allowing the collateral removal; skip leaves the triggering install unapproved; abort raises SyncAbortedByUser naming the collateral package."
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestCollateralFlow"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestCollateralGroupResolution"
        status: pass
    human_judgment: false
  - id: D3
    description: "The apply-time guard still refuses an unreviewed manual removal or downgrade that drifted in after plan time; the D-30 win (install whose only collateral is auto deps) is no longer blocked."
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestTransactionGuard"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestDowngradeGuard"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestRemovalGuard"
        status: pass
    human_judgment: false

# Metrics
duration: 55min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 16: Classify apt collateral before refusing (D-30) Summary

**apt collateral is now split by provenance — auto-installed dependencies proceed silently, while a manually-installed package the transaction would remove or downgrade becomes a three-way install-anyway / skip / abort review item decided at plan time and honoured by the apply-time guard.**

## Performance

- **Duration:** ~55 min
- **Completed:** 2026-07-23
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Replaced the blanket-refusal collateral behaviour with the corrected D-30 split: a package the batched `apt-get -s` simulation would remove or downgrade is classified against the target's `apt-mark showmanual` set. Auto-installed collateral (apt resolving its own dependencies) proceeds with no review item; manually-installed collateral becomes its own reviewable item.
- Added `COLLATERAL_REVIEW_ACTION` to `package_review.py` (in `__all__`) with `_is_collateral_group` and `_review_collateral_group`, giving manual-collateral entries a three-way `questionary.select` — install anyway, skip, abort — instead of a checkbox tick, with every untrusted label `Text`-wrapped against the Rich markup crash.
- Wired `AptSyncJob` to capture the target manual set once, classify collateral (`_classify_collateral`), carve manual-collateral items into their own review group, translate the recorded decision (`_resolve_collateral` in `accept_review`), and thread the approved-collateral set plus manual set into the `_converge_install`/`_converge_remove` guards.
- The guards now let an approved manual-collateral removal and any auto-collateral through, while still refusing an unreviewed manual removal or downgrade that drifted in between plan and apply — proven with call-count drift tests.

## Task Commits

1. **Task 1: Plan-time manual-collateral items, decided in the review** - `bcde6a1` (feat)
2. **Task 2: Regression coverage against re-emergence of the blanket refusal** - `dfcd889` (test)

_Task 1 is the tracer slice; source and its core behaviour tests landed together, and Task 2 extended the same suites with the D-30-win, drift, and review-layer coverage._

## Files Created/Modified
- `src/pcswitcher/jobs/apt_sync.py` - Target manual-set capture, `_classify_collateral`/`_collateral_item`, collateral carve-out in `_build_review_groups`, `_resolve_collateral` translation in `accept_review`, provenance-aware `_converge_install`/`_converge_remove` guards, updated docstrings.
- `src/pcswitcher/jobs/package_review.py` - `COLLATERAL_REVIEW_ACTION` sentinel, `_is_collateral_group`, `_review_collateral_group` (three-way select raising `SyncAbortedByUser` on abort), interactive-loop dispatch, `SyncAbortedByUser` import.
- `tests/unit/jobs/test_apt_sync.py` - Rewrote `TestPlanTimeCollateral`/`TestTransactionGuard`/`TestDowngradeGuard`/`TestRemovalGuard` for D-30; added `TestCollateralFlow` (install-anyway / skip end to end).
- `tests/unit/jobs/test_package_review.py` - Added `TestCollateralGroupResolution` (install-anyway->APPLY, skip->SKIP_ONCE, abort raises, bracketed label renders, non-interactive skip-once, never a checkbox).

## Decisions Made
- **Synthetic collateral id + accept_review translation.** The batched `apt-get -s` simulation cannot attribute a collateral removal to a single install, so each manual-collateral package gets one item (`apt:collateral:<pkg>`) whose decision is translated: install-anyway marks the package approved-collateral (the guard then allows its removal) and leaves the installs approved; skip is propagated to `SKIP_ONCE` on the triggering candidate set, so a declined collateral cleanly un-approves rather than failing at the guard. No third simulation was added — classification hangs off the two existing batched previews plus the manual set.
- **Symmetric remove guard.** `_converge_remove` was relaxed the same way as `_converge_install` (auto reverse-deps proceed, manual unreviewed refused) so plan-time classification and apply-time verification agree for the remove simulation as well, not only the install one.

## Deviations from Plan

None - plan executed exactly as written. The plan granted executor discretion on the abort representation (reused the existing `SyncAbortedByUser`, no new enum member) and the `_classify_collateral` helper name; both were taken within the pinned observable behaviour.

## Issues Encountered
- Four existing guard/collateral tests asserted the old blanket-refusal behaviour using auto packages (not in the manual set); under D-30 those cases now proceed. Per Task 2's "extend rather than start a parallel suite," they were rewritten in place to the corrected semantics (manual-drift refusal, auto-proceeds win) rather than deleted, keeping the guard's last-line-of-defence coverage intact.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
D-30 is fully delivered and the whole unit gate is green (998 passed), `ruff check`/`ruff format --check`/`basedpyright` all clean. The remaining wave-2 delta plans (02-17…02-21) can build on the corrected collateral behaviour. VM integration coverage of the collateral path remains to be exercised in CI as with the rest of phase 02.

## Self-Check: PASSED

---
*Phase: 02-package-management-sync*

*Completed: 2026-07-23*
