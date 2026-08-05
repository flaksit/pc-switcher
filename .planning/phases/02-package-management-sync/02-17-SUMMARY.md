---
phase: 02-package-management-sync
plan: 17
subsystem: package-sync
tags: [apt, dpkg, unreproducible-installs, snippet-registry, sync-jobs, python]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: PackageSyncJob plan/apply pipeline, SnippetRegistry, DecisionFile, package_review three-way resolution, apt_sync unreproducible detection (02-07/02-08/02-15/02-16)
provides:
  - manual_installs_sync — fourth package job owning all unreproducible detection (D-18) on its own enable flag
  - Overridable no-op _finalize_unreproducible / _unresolved_as_failures hooks on PackageSyncJob
  - D-21 corrected: explicit skip-once is a valid resolution, not an unresolved state
affects: [package-sync, folder_sync-exclusions, verify-work]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Per-manager ownership: a job runs its OWN dpkg/apt-cache commands rather than importing a sibling job (D-18)"
    - "Base no-op hook + subclass override to keep a shared apply() generic across managers that do/don't produce a diff class"

key-files:
  created:
    - src/pcswitcher/jobs/manual_installs_sync.py
    - tests/unit/jobs/test_manual_installs_sync.py
  modified:
    - src/pcswitcher/jobs/apt_sync.py
    - src/pcswitcher/jobs/package_sync_core.py
    - src/pcswitcher/jobs/package_review.py
    - src/pcswitcher/jobs/__init__.py
    - src/pcswitcher/default-config.yaml
    - src/pcswitcher/schemas/config-schema.yaml

key-decisions:
  - "New job owns private copies of the pure parsers (_lines, _packages_with_no_candidate, _owned_paths_from_dpkg_s) rather than importing apt_sync — keeps D-18 ownership clean"
  - "capture_source_items() returns the union of both detectors; query_target_items() returns [] since unreproducible items are always source-held and convergence is registry-driven"
  - "validate() checks only source-side apt-cache/dpkg availability; snippet sudo needs are opaque (D-20) so target sudo is not pre-validated"

patterns-established:
  - "Base no-op hook, subclass override: _finalize_unreproducible / _unresolved_as_failures live on the base as no-ops and are implemented only by the manager that produces the diff class"

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "manual_installs_sync detects apt-no-candidate packages and unowned /usr/local,/opt installs on its own enable flag, independent of apt_sync"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestNoCandidateDetection/TestUnownedScan/TestExecuteIndependentOfApt"
        status: pass
    human_judgment: false
  - id: D2
    description: "An item with a registry snippet plans INSTALL and converges by replaying it verbatim; one without plans REPORT_ONLY into an UNREPRODUCIBLE_REVIEW_ACTION group"
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestSnippetResolution/TestTracerEndToEnd"
        status: pass
    human_judgment: false
  - id: D3
    description: "apt_sync no longer detects, reviews or converges any unreproducible item"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py#TestNoUnreproducibleDetectionInApt"
        status: pass
    human_judgment: false
  - id: D4
    description: "D-21: an explicit skip-once is a valid resolution (not unresolved); a run whose only items were skipped-once is clean, a genuinely undecided item still fails"
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py#TestUnreproducibleGroupResolution + test_manual_installs_sync.py#TestSkipOnceResolution"
        status: pass
    human_judgment: false
  - id: D5
    description: "Finalize/unresolved hooks are no-ops on the base so apt/snap/flatpak stay generic; the moved logic lives on manual_installs_sync"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py#TestBaseHooksAreNoOps/TestFinalizeUnreproducible"
        status: pass
    human_judgment: false
  - id: D6
    description: "manual_installs_sync registered before folder_sync (D-17) and accepted by the schema; no top-level config section (D-32); decision file matches the folder_sync *.decisions.yaml glob (T-02-45)"
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_config_system.py#test_package_jobs_precede_folder_sync,test_manual_installs_sync_is_an_accepted_job_name"
        status: pass
    human_judgment: false

# Metrics
duration: 45min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 17: manual_installs_sync + skip-once correction Summary

**A fourth package job (`manual_installs_sync`) that owns all unreproducible detection — apt-no-candidate packages and unowned /usr/local,/opt installs — on its own enable flag, with snippet replay for reproduction and skip-once corrected to a valid resolution (D-15/D-18/D-21).**

## Performance

- **Duration:** ~45 min
- **Started:** 2026-07-23T20:47:00Z (approx)
- **Completed:** 2026-07-23T21:31:39Z
- **Tasks:** 2 (tracer + auto, both tdd)
- **Files modified:** 13 (2 created, 11 modified)

## Accomplishments
- Created `ManualInstallsSyncJob` (`PackageSyncJob` subclass, `name=manual_installs_sync`, `manager_id=manual`) running its own `apt-mark`/`apt-cache policy`/`find`/`dpkg -S` detection — never importing `apt_sync` (D-18).
- Removed all unreproducible detection/review/replay from `apt_sync` (scans, `_plan_unreproducible_diffs`, `_converge_unreproducible`, the plan()/converge() branches, and the unreproducible half of `_build_review_groups` — the collateral carve-out stays).
- Turned `_finalize_unreproducible` and `_unresolved_as_failures` into overridable no-op hooks on `PackageSyncJob`; the real bodies now live on `ManualInstallsSyncJob`, keeping the base `apply()` generic for apt/snap/flatpak.
- Corrected D-21 in `package_review._review_unreproducible_group`: an explicit "Skip for now" records `SKIP_ONCE` and is NOT unresolved; only a cancelled select or an abandoned snippet capture is unresolved.
- Registered `manual_installs_sync: false` before `folder_sync` (D-17) in `default-config.yaml` and `config-schema.yaml` sync_jobs, with no top-level config section (D-32); exported from `jobs/__init__.py`.

## Task Commits

Each task was committed atomically (both tasks were tdd="true"; test and implementation were committed together per task given the extraction nature of the work):

1. **Task 1: ManualInstallsSyncJob — detect, review, replay end to end (tracer)** — `42193ec` (feat)
2. **Task 2: Move finalize/unresolved off the base; skip-once is a resolution (D-21)** — `a99f02c` (feat)

_Tracer feedback gate: after Task 1 the tracer `<verify>` (pytest on the two test files + basedpyright on both source modules) was re-run end-to-end and passed (75 passed, 0 pyright issues) before expanding to Task 2._

## Files Created/Modified
- `src/pcswitcher/jobs/manual_installs_sync.py` — new job: detection, plan(), converge() (snippet replay), validate(), the two moved hooks, describe_first_sync_scope().
- `src/pcswitcher/jobs/apt_sync.py` — removed all unreproducible detection; plan() and _build_review_groups() reduced to apt packages + repo group + collateral.
- `src/pcswitcher/jobs/package_sync_core.py` — _finalize_unreproducible / _unresolved_as_failures are now no-op base hooks; apply()/docstrings updated.
- `src/pcswitcher/jobs/package_review.py` — D-21 split of skip-once from cancelled/abandoned in _review_unreproducible_group.
- `src/pcswitcher/jobs/__init__.py` — export ManualInstallsSyncJob.
- `src/pcswitcher/default-config.yaml`, `src/pcswitcher/schemas/config-schema.yaml` — sync_jobs enable flag (before folder_sync, no top-level section).
- Tests: new `test_manual_installs_sync.py`; moved scan/unreproducible tests off `test_package_state.py`; retargeted finalize tests + added base-hook no-op tests in `test_package_sync_core.py`; updated D-21 and unresolved coverage in `test_package_review.py`; D-17 ordering + schema acceptance in `test_config_system.py`; a no-UNREPRODUCIBLE assertion in `test_apt_sync.py`.

## Decisions Made
- New job holds private copies of the three pure parsers rather than importing apt_sync or lifting them into shared core — the smallest change that keeps D-18 ownership clean.
- `query_target_items()` returns `[]`: unreproducible items are source-held and convergence is registry-driven, so there is no meaningful target manifest to diff.
- `validate()` checks only source-side `apt-cache`/`dpkg` availability; per D-20 a snippet's sudo needs are opaque, so target sudo is left to fail per-item at converge (D-27) rather than being pre-validated.

## Deviations from Plan

None affecting deliverables. One in-scope test-refactor consequence worth recording:

**1. [Rule 1 - Refactor fallout] Retargeted base-job finalize tests to the new hook owner**
- **Found during:** Task 2 (moving the hooks off the base)
- **Issue:** `TestFinalizeUnreproducible` and `TestUnresolvedFailsTheJob` exercised the base `FakeSyncJob`/`_FakeUnreproducibleJob`; once the hooks became base no-ops those assertions would break.
- **Fix:** Added thin `ManualInstallsSyncJob` subclasses (`_FakeManualJob`, `_FakeUnreproducibleJob`) so the moved hooks are exercised at their new home; added a `TestBaseHooksAreNoOps` proving the base no-ops. All within the plan's test files_modified.
- **Verification:** Full unit gate green.
- **Committed in:** `a99f02c`

## Issues Encountered
- The orchestrator's structural D-17 guard `_check_package_jobs_precede_folder_sync` (orchestrator.py) still hardcodes `("apt_sync", "snap_sync", "flatpak_sync")` and does not include `manual_installs_sync`. `orchestrator.py` is NOT in this plan's `files_modified` and the delta phase guard forbids scope expansion, so it was left unchanged. The D-17 truth still holds via the shipped `default-config.yaml` key order (which lists manual_installs_sync before folder_sync, asserted by `test_package_jobs_precede_folder_sync`); only the defense-in-depth hand-edit guard omits the fourth job. Recommend a follow-up one-line change to that tuple in a plan that owns orchestrator.py.

## Threat surface
- T-02-45 (decision-file exclusion) confirmed: `manual.decisions.yaml` matches `package_state.DECISION_FILE_GLOB_RELPATH` (`.config/pc-switcher/*.decisions.yaml`), so folder_sync excludes it non-overridably. No new trust boundaries beyond those in the plan's threat model.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Four package jobs now exist with independent enable flags; unreproducible detection is entirely inside `manual_installs_sync`; skip-once is a clean resolution; full unit gate green (1015 passed).
- Follow-up: add `manual_installs_sync` to the orchestrator's `_check_package_jobs_precede_folder_sync` tuple in a plan scoped to orchestrator.py.

## Self-Check: PASSED

All created files exist on disk (`manual_installs_sync.py`, `test_manual_installs_sync.py`, this SUMMARY) and both task commits (`42193ec`, `a99f02c`) are present in git history.

---

*Phase: 02-package-management-sync*

*Completed: 2026-07-23*
