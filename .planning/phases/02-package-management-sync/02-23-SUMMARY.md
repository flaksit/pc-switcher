---
phase: 02-package-management-sync
plan: 23
subsystem: testing
tags: [integration-tests, manual_installs_sync, apt_sync, snippet-registry, D-27, D-23, ledger-2]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: source-based classification + same-run snippet application (02-22)
provides:
  - "VM integration suite retargeted to the corrected same-run application (D-27 continue-on-failure genuinely exercised with source-authored snippets)"
  - "manual-installs push test authors source-only under corrected source-based classification"
  - "apt-repository-state dry-run test sets up a synthetic repo+key divergence (no more pytest.skip) — broken-window ledger #2 has real coverage"
affects: [verify-phase-02, ci-pr-206]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Integration tests synthesize their own /etc/apt divergence on the source instead of depending on a naturally-occurring vendor repo/key pair, then restore in a finally"

key-files:
  created: []
  modified:
    - tests/integration/jobs/test_package_sync.py

key-decisions:
  - "test_continue_on_item_failure enables manual_installs_sync (the D-18 snippet owner), not apt_sync alone: apt_sync leaves the three unowned-install snippets inert and the sync exits 0, which is the defect CI flagged on PR #206"
  - "manual-installs push test authors on the source only; the OLD-marker item-on-both-machines trick and its assertion are removed because classification is now source-based (02-22)"
  - "apt-repository-state test writes a uuid-suffixed synthetic deb822 .sources + keyring pair on pc1 that the fresh target lacks, replacing the perpetually-skipping natural-pair search"

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

# Metrics
duration: 4min
completed: 2026-07-24
status: complete
---

# Phase 2 Plan 23: Retarget VM integration suite to same-run application Summary

**The VM integration suite now exercises 02-22's corrected same-run behavior — continue-on-failure enables the D-18 snippet owner so the three source-authored snippets converge the same run, the manual push test authors source-only, and the apt-repository-state test synthesizes its own repo+key divergence instead of skipping — closing broken-window ledger #2, pending the definitive green CI run on PR #206.**

## Performance

- **Duration:** 4 min
- **Started:** 2026-07-24T10:28:08Z
- **Completed:** 2026-07-24T10:32:09Z
- **Tasks:** 2
- **Files modified:** 1

## Accomplishments

- `test_continue_on_item_failure` now enables `manual_installs_sync` via `_write_package_sync_config(..., manual_installs_sync=True)`. The three unowned-install snippets it authors are owned by `manual_installs_sync` (D-18); with `apt_sync` alone they were inert, the sync exited 0, and the `assert not sync_result.success` (D-27) is exactly what CI flagged on PR #206. They now converge the same run and the deliberately-failing middle item makes the sync exit non-zero.
- `test_manual_installs_sync_pushes_registry_and_replays_snippet` authors the snippet on the SOURCE (pc1) only. The OLD-marker item-on-both-machines trick (an OLD body on pc2 plus its `old_exists` assertion) — which only existed to satisfy the pre-02-22 target-registry classification — is gone. pc2 holding no registry before the run makes its post-run presence a clean push witness; the NEW marker witnesses the pushed source snippet being replayed the same run.
- `test_apt_repository_state_dry_run_reviews_source_and_key_separately` no longer short-circuits via `pytest.skip`. New helper `_create_synthetic_repo_and_key(pc1)` writes a uuid-suffixed deb822 `.sources` + `/etc/apt/keyrings` key pair on the source that the fresh target lacks (`mkdir -p /etc/apt/keyrings` first per the shipped invariant; `sudo tee` for the root-owned dirs; dummy key bytes, safe under `--dry-run` + D-12 verbatim copy). The two separate INSTALL review entries + the `apt-get update` marker assertions are unchanged; broken-window ledger #2 now has real, deterministic coverage.
- Corrected all stale `AptSyncJob.scan_unowned_installs` references to the current owner `ManualInstallsSyncJob._scan_unowned_installs` (module comment, `_unowned_item_id`/`_create_unowned_marker` docstrings, continue-test docstring).
- Removed the now-unused `parse_signed_by_refs`, `_list_apt_dir_files`, `_find_repo_and_key_pair`, the two signed-by regexes, and the `pathlib.Path` import — no dead helper left for ruff/basedpyright to flag.

## Task Commits

1. **Task 1: Retarget the manual-installs integration coverage to same-run application** - `fd2d2da` (test)
2. **Task 2: Un-skip the apt-repository-state test by setting up a synthetic divergence** - `fc57ff9d` (test)

## Files Created/Modified

- `tests/integration/jobs/test_package_sync.py` - continue-on-failure enables `manual_installs_sync` (+ stale-ref corrections); manual-installs push test authors source-only (OLD-marker mechanism dropped); apt-repository-state dry-run test creates a synthetic repo+key divergence via `_create_synthetic_repo_and_key` and restores in a finally; unused repo/key-pair helpers, regexes and `Path` import removed.

## Decisions Made

- The continue-on-failure test must enable `manual_installs_sync`, not `apt_sync`: the D-18 owner is what converges unowned-install snippets, so enabling only apt left them inert (the PR #206 defect).
- The manual push test asserts push + same-run replay from a source-only registry — no target seeding — matching 02-22's source-based classification.
- The apt-repository-state divergence is synthesized on the source rather than discovered, because fresh runner VMs never carry a `keyrings`-referenced vendor repo present on both machines; the synthetic pair makes ledger #2 deterministic.
- Every mutation (synthetic /etc/apt files, unowned markers, config) is reverted in a `finally`; the apt-repository-state sync is `--dry-run`, so pc2 is never mutated.

## Deviations from Plan

None - plan executed exactly as written. Both tasks stayed within the single declared file (`tests/integration/jobs/test_package_sync.py`).

## Verification Performed

- `uv run ruff check tests/integration/jobs/test_package_sync.py` — clean.
- `uv run basedpyright tests/integration/jobs/test_package_sync.py` — 0 errors, 0 warnings, 0 notes.
- `uv run pytest tests/integration/jobs/test_package_sync.py --collect-only -q` — module imports and collects (10 tests, deselected without VM env vars, as expected).
- Grep witnesses: `manual_installs_sync=True` present in the continue + push tests; `_create_synthetic_repo_and_key` defined; no remaining repo/key `pytest.skip`.
- No VM run attempted (no local VM access). The definitive proof is the green CI run on PR #206, gated by the existing 02-21 human checkpoint — this plan adds no new checkpoint.

## Known Stubs

None. All three edits are complete, collect-clean test code; the only pending item is their CI execution, which is the plan's declared definitive-proof path (not a stub).

## Next Phase Readiness

- The suite that caught the original one-run-too-late defect now genuinely exercises the corrected same-run behavior; ready for the CI re-run on PR #206 (02-21 checkpoint).
- Preserved invariants: restore discipline (every setup reverted in a finally), assert-against-target-state (not log text) except the dry-run repo test whose subject is the review output, and no leftover pc1/pc2 package/repo/marker/config state.

## Self-Check: PASSED

- `tests/integration/jobs/test_package_sync.py` exists and is modified (FOUND).
- Commit `fd2d2da` FOUND; commit `fc57ff9d` FOUND.

*Phase 02-package-management-sync — completed 2026-07-24*
