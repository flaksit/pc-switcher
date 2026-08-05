---
phase: 02-package-management-sync
plan: 22
subsystem: api
tags: [manual_installs_sync, snippet-registry, package-sync, D-23, apt]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: manual_installs_sync + snippet registry push (02-17/02-18)
provides:
  - "plan() judges reproducibility from the SOURCE snippet registry (corrected D-23)"
  - "after_review() promotes on-the-fly-authored snippets REPORT_ONLY->INSTALL/APPLY so they converge the same run"
  - "base sync_core apply()/execute() left byte-unchanged (D-18); no coordinator reintroduced"
affects: [manual_installs_sync, package-sync, verify-phase-02]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "In-memory promotion via dataclasses.replace on frozen PackagePlan/ReviewOutcome, inside the subclass, keeping the base pipeline generic"

key-files:
  created: []
  modified:
    - src/pcswitcher/jobs/manual_installs_sync.py
    - src/pcswitcher/jobs/packages/state.py
    - tests/unit/jobs/test_manual_installs_sync.py
    - docs/adr/adr-020-declarative-package-convergence.md
    - docs/jobs/package-sync.md

key-decisions:
  - "Reproducibility classified from SnippetRegistry(self.source), not the target — the source is the machine being replicated (corrected D-23)"
  - "On-the-fly snippet promotion runs AFTER the send_file() push, so the target holds the snippet before the promoted diff converges"
  - "Promotion forces Decision.APPLY because the add-snippet review path records no decision for the item"

patterns-established:
  - "Same-run correction lives entirely in the manual_installs_sync subclass; base apply()/execute()/after_review contract untouched (D-18)"

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "plan() classifies an unreproducible item from the SOURCE snippet registry — a source snippet plans INSTALL, a target-only snippet stays REPORT_ONLY"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestClassificationAuthority"
        status: pass
    human_judgment: false
  - id: D2
    description: "A snippet authored on the fly during review is replayed (applied) on the target the same run via after_review() promotion, driven end-to-end through execute()"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestSameRunApplication::test_on_the_fly_snippet_is_replayed_the_same_run"
        status: pass
    human_judgment: false
  - id: D3
    description: "Dry-run promotes and previews an on-the-fly install but issues no replay and no source registry write (ADR-014); D-27/D-21/D-26 preserved"
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestClassificationAuthority::test_dry_run_previews_on_the_fly_install_without_replay_or_write"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestContinueOnFailure"
        status: pass
    human_judgment: false
  - id: D4
    description: "ADR-020 and package-sync.md state snippets are applied/replayed the same run and name the source as the reproducibility authority; base sync_core.py unchanged"
    verification:
      - kind: other
        ref: "grep -niE 'replay|applied' docs/adr/adr-020-declarative-package-convergence.md docs/jobs/package-sync.md; git diff f4528ec..HEAD --stat src/pcswitcher/jobs/packages/sync_core.py (empty)"
        status: pass
    human_judgment: false

# Metrics
duration: 6min
completed: 2026-07-24
status: complete
---

# Phase 2 Plan 22: manual_installs_sync same-run snippet application Summary

**plan() now judges reproducibility from the SOURCE snippet registry and after_review() promotes on-the-fly-authored snippets to INSTALL/APPLY, so a source-authored install snippet is applied the same run instead of one run too late — base pipeline byte-unchanged.**

## Performance

- **Duration:** 6 min
- **Started:** 2026-07-24T10:19:20Z
- **Completed:** 2026-07-24T10:25:03Z
- **Tasks:** 3
- **Files modified:** 5

## Accomplishments
- `plan()` classifies from `SnippetRegistry(self.source)` (corrected D-23): a source-side snippet plans `INSTALL`, a target-only snippet plans `REPORT_ONLY` — the root-cause fix for the one-run-too-late defect.
- New `_promote_authored_snippets_to_install()` runs in `after_review()` after the finalize+push, reclassifying each on-the-fly-authored item `REPORT_ONLY -> INSTALL` decided `APPLY` (via `dataclasses.replace` on the frozen plan/outcome) so the unchanged base `apply()` converges it this run.
- Unit coverage pins classification-from-source (`TestClassificationAuthority`), same-run replay end-to-end through `execute()` (`TestSameRunApplication`), and dry-run promote-without-replay-or-write (ADR-014).
- ADR-020 and package-sync.md corrected to say snippets are applied (replayed), not merely transported, with the source named as the reproducibility authority.

## Task Commits

Each task was committed atomically:

1. **Task 1: Source-classification + same-run application (tracer, TDD)** - `1ff5911` (fix)
2. **Task 2: Update classification unit tests and pin corrected behavior** - `f081c0f` (test)
3. **Task 3: Make ADR-020 and package-sync.md say applied not transported** - `7ba0b0b` (docs)

## Files Created/Modified
- `src/pcswitcher/jobs/manual_installs_sync.py` - `plan()` reads source registry; `after_review()` calls new `_promote_authored_snippets_to_install()` after the push; `replace` import added.
- `src/pcswitcher/jobs/packages/state.py` - corrected stale `SnippetRegistry` docstring (plan reads source, only converge reads target after the push).
- `tests/unit/jobs/test_manual_installs_sync.py` - `TestSameRunApplication`, `TestClassificationAuthority` (source vs target-only + dry-run pin); seeded the SOURCE registry in the classification/replay/push tests broken by the switch.
- `docs/adr/adr-020-declarative-package-convergence.md` - MUST line + snippet paragraph corrected for source authority and same-run application.
- `docs/jobs/package-sync.md` - "Install snippets" section reworded to applied/replayed + source-reproducibility sentence.

## Decisions Made
- Reproducibility judged from `SnippetRegistry(self.source)`, never the target (corrected D-23). The `send_file()` push places the snippet on the target before `converge()` reads it.
- Promotion runs after the push and forces `Decision.APPLY` because the review's add-snippet path records no decision for the item.
- Promotion mutates only in-memory accepted state, so it is safe under dry-run: `apply()`'s dry-run branch previews a would-install line and issues no converge (ADR-014).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `TestContinueOnFailure` also broken by the source-registry switch**
- **Found during:** Task 2 (running the full module)
- **Issue:** `TestContinueOnFailure::test_failed_snippet_replay_...` seeded the snippet registry only in `target_responses`; after `plan()` switched to reading the source, both items classified `REPORT_ONLY` and `apply()` raised nothing (DID NOT RAISE `PackageItemFailures`). Not in Task 2's explicit list but the same breakage class.
- **Fix:** Added the same registry YAML to `source_responses` so both items classify `INSTALL`, restoring the test's converge-failure intent.
- **Files modified:** tests/unit/jobs/test_manual_installs_sync.py
- **Verification:** Full module 27 passed; broader `tests/unit/jobs/` 450 passed.
- **Committed in:** `f081c0f` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug — in-scope test correction)
**Impact on plan:** Within scope of Task 2 (fixing tests broken by the classification switch). No source-code scope creep; base `sync_core.py` untouched.

## Issues Encountered
- One ruff E501 (line too long) in the new dry-run pin; refactored the comprehension into a named list. No functional impact.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Corrected same-run behavior is unit-proven and ready for the VM integration re-run (the suite that caught the original defect). Plan 02-23 follows.
- Preserved invariants: D-27 (failed converge still fails the job), D-21 (skip-once/skip-always), D-26 (non-interactive records nothing), ADR-014 (dry-run no trace), and the base pipeline (D-18/D-24) unchanged.

## Self-Check: PASSED

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-24*
