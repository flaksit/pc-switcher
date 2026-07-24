---
phase: 02-package-management-sync
plan: 21
subsystem: testing
tags: [integration-tests, package-sync, apt-repository-state, snippet-registry, per-manager-review, VM, broken-window]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: per-manager review inside execute() (02-15), manual_installs_sync + snippet push (02-17/02-18), jobs.packages.* module layout (02-19)
provides:
  - "VM integration suite retargeted to the four-job set and the per-manager review (no PackagePhaseCoordinator)"
  - "test_each_manager_reviews_before_its_own_mutation — per-manager review-before-own-mutation proven against pc2's own package managers"
  - "test_apt_repository_state_dry_run_reviews_source_and_key_separately — VM-level apt repository state (source + key as separate review entries, apt-get update reported), closing broken-window ledger #2"
  - "test_manual_installs_sync_pushes_registry_and_replays_snippet — send_file push + snippet replay proven against pc2's registry file and filesystem"
affects: [02-22 source same-run fix, 02-23 integration rework, verify-work]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Integration ordering witnessed by the target's own package-manager/filesystem state rather than pc-switcher log text (except the dry-run repo test, whose subject IS the review output)"
    - "Runtime repo+key pair discovery on the VMs (signed-by parsing kept independent of apt_sync's private regexes) so a divergence targets a real vendor repo"

key-files:
  created: []
  modified:
    - tests/integration/jobs/test_package_sync.py
    - .planning/phases/02-package-management-sync/02-VALIDATION.md

key-decisions:
  - "Reframed the cross-manager ordering test to a per-manager property (each manager reviews then converges its OWN diff); dropped the coordinator log witness entirely, asserting only pc2's own apt-mark showmanual / snap list end state"
  - "The apt-repository-state proof is a --dry-run test whose subject is legitimately the review output (ADR-014: a rehearsal makes no filesystem change to assert against); everything else asserts target state"
  - "config_sync integration test and jobs.packages.* imports were already single-file-correct / repointed by 02-18 and 02-19, so left untouched per Task 1's 'if it does not touch the registry, leave it'"

patterns-established:
  - "Per-manager review-before-own-mutation is witnessed by end state, not a run-log line, since an item converges only because its own manager's review approved it"

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "Integration suite imports jobs.packages.* and encodes the per-manager, four-job design with no PackagePhaseCoordinator symbol"
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: other
        ref: "! grep -q 'PackagePhaseCoordinator\\|coordinate_package_review\\|package manager(s) planned\\|jobs.package_items\\|jobs.package_review\\|jobs.package_state' tests/integration/jobs/test_package_sync.py"
        status: pass
    human_judgment: false
  - id: D2
    description: "VM-level apt repository state: a target missing one vendor repo shows the source file and its signing key as separate review entries and reports the intended apt-get update (closes broken-window ledger #2)"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestAptSyncEndToEnd::test_apt_repository_state_dry_run_reviews_source_and_key_separately"
        status: pass
    human_judgment: false
  - id: D3
    description: "manual_installs_sync pushes package-snippets.yaml to the target with its own send_file and the snippet is replayed there, proven against pc2's registry file and filesystem marker"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestManualInstallsSyncEndToEnd::test_manual_installs_sync_pushes_registry_and_replays_snippet"
        status: pass
    human_judgment: false

# Metrics
duration: 40min
completed: 2026-07-24
status: complete
---

# Phase 2 Plan 21: Integration Suite Retarget + apt-repository-state and manual-installs VM Coverage Summary

**Reworked the VM integration suite onto the corrected four-job / per-manager-review design (no coordinator) and added the two VM tests that close broken-window ledger #2 (apt repository state) and prove the manual_installs_sync snippet push+replay — now fully green in CI on PR #206.**

## Performance

- **Duration:** ~40 min (executor work; then a blocking checkpoint gated on a ~23 min CI run)
- **Completed:** 2026-07-24
- **Tasks:** 2 auto tasks executed + 1 blocking checkpoint (Task 3), now resolved
- **Files modified:** 2

## Accomplishments

- Retargeted `tests/integration/jobs/test_package_sync.py` to the corrected D-24: rewrote the module/class docstrings to describe each manager reviewing its OWN diffs inside its own `execute()`, with no cross-manager coordinator; the `jobs.packages.*` imports were already in place from 02-19.
- Reframed `test_all_managers_diff_before_any_applies` → `test_each_manager_reviews_before_its_own_mutation`: it now asserts each enabled manager converges its own approved diff against pc2's own `apt-mark showmanual` / `snap list`, and drops the coordinator log-scraping witness (`"N package manager(s) planned"`, `"planned; review covers"`). No inter-manager ordering is asserted.
- Added `test_apt_repository_state_dry_run_reviews_source_and_key_separately`: diverges pc2 by one vendor repo (source file + its keyring), runs `--dry-run`, and asserts the source file and its signing key appear as two SEPARATE review entries and the intended `apt-get update` metadata refresh is reported — ledger #2's literal contract. Restores pc2's `/etc/apt` in a `finally`.
- Added `test_manual_installs_sync_pushes_registry_and_replays_snippet`: proves the job's own `send_file()` push places the source registry on the target and the snippet is replayed, asserted against pc2's own registry file and a filesystem marker. Restores in a `finally`.
- Updated `02-VALIDATION.md`: two new integration rows (02-21.1, 02-21.2), corrected the coordinator (02-03.2) and cross-manager-ordering (02-11.1) rows to the per-manager/four-job reality, and refreshed the Pending CI note (10 tests).

## Task Commits

1. **Task 1: Retarget the suite to per-manager review, four-job shape** — `21282d7` (test)
2. **Task 2: Add apt-repository-state and manual-installs VM coverage** — `27f0059` (test)

**Plan metadata:** this commit (SUMMARY + STATE/ROADMAP + WINDOWS ledger #2 closure).

## Files Created/Modified

- `tests/integration/jobs/test_package_sync.py` — docstrings retargeted; coordinator test reframed to a per-manager property; two new VM tests + repo/key pair discovery helpers (signed-by parsing, kept independent).
- `.planning/phases/02-package-management-sync/02-VALIDATION.md` — new integration rows, per-manager/four-job corrections, refreshed Pending CI note; `nyquist_compliant` left `false` (integration rows verified by CI, not local).

## Decisions Made

- Per-manager review-before-own-mutation is witnessed by end state (both items on pc2's own package managers), not a run-log line — an item converges only because its own manager's review approved it, so the coordinator log witness was unnecessary and is gone.
- The apt-repository-state proof is a `--dry-run` test whose subject is legitimately the review output (ADR-014), the one carve-out from "assert target state, never log text".
- Left `tests/integration/test_config_sync.py` untouched (already single-file-correct from 02-18) and the `jobs.packages.*` imports as-is (already repointed by 02-19), per Task 1's scope note.

## Deviations from Plan

None affecting deliverables. Two scope notes:

- The apt-repository-state test as first written used `pytest.skip` when no usable repo/key pair existed on the VMs, and the manual-installs test seeded the item on both machines (target OLD / source NEW bodies) to witness push+replay in one run under the then-current same-run constraint. Both were later reworked by plan 02-23 after CI surfaced the underlying same-run-apply defect (below).

## Issues Encountered

The first CI run on PR #206 surfaced a real defect that this plan's own `test_continue_on_item_failure` (retargeted here) helped expose: `manual_installs_sync` applied source-authored snippets one run too late, because `plan()` classified reproducibility from the TARGET registry while the push happens only after review. This was corrected by follow-up plans **02-22** (source fix: `plan()` now reads `SnippetRegistry(self.source)` plus a `_promote_authored_snippets_to_install()` in `after_review()`; base `sync_core.py` untouched) and **02-23** (integration rework: `test_continue_on_item_failure` now enables `manual_installs_sync`; the manual-installs test authors source-only; the apt-repository-state test synthesizes its own repo+key divergence instead of skipping). CONTEXT.md D-23/D-21 were corrected to require same-run application. Their own SUMMARYs cover the details.

## Task 3 Checkpoint Resolution

Task 3 was a BLOCKING `checkpoint:human-verify` whose only real proof was a green CI run (no local VM access). **Resolved:** CI on PR #206 is now fully green (Lint, Unit, Integration — Integration Tests job 22m57s). Confirmed: `test_continue_on_item_failure` PASSED, `test_apt_repository_state_dry_run_reviews_source_and_key_separately` PASSED (it RAN, not skipped — genuinely exercised the coverage), `test_manual_installs_sync_pushes_registry_and_replays_snippet` PASSED. Broken-window ledger entry #2 is marked fixed (`WINDOWS.md open_count: 0`).

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- The VM integration suite reflects the corrected four-job / per-manager-review architecture; apt repository state and the manual-installs snippet push are exercised at VM level and green in CI.
- Broken-window ledger #2 closed — `/gsd-ship` no longer blocked on it.
- The same-run-apply correction (02-22/02-23) landed on top of this rework; phase 02 execution is complete through plan 02-23.

## Self-Check: PASSED

- `tests/integration/jobs/test_package_sync.py` and `.planning/phases/02-package-management-sync/02-VALIDATION.md` confirmed modified.
- Commits `21282d7` (Task 1) and `27f0059` (Task 2) confirmed present in git history.
- Grep gate clean (no `PackagePhaseCoordinator` / `coordinate_package_review` / `package manager(s) planned` / `jobs.package_*`); `ruff check` and `basedpyright` clean on the changed file; module collects all 10 integration tests.

---

*Phase: 02-package-management-sync*

*Completed: 2026-07-24*
