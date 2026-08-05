---
phase: 02-package-management-sync
plan: 13
subsystem: infra
tags: [apt, package-sync, integration-test, tracer, dry-run, adr-014, d-26]

requires:
  - phase: 02-package-management-sync
    provides: "AptSyncJob/PackagePhaseCoordinator tracer + diff taxonomy (plans 02-03, 02-05)"
provides:
  - "tests/integration/jobs/test_package_sync.py: the phase's integration-test module, seeded with the tracer's end-to-end scenario"
  - "Pure candidate-selection/parsing helpers (nonblank_lines, parse_dpkg_installed, parse_reverse_depends, parse_batched_rdepends, pick_safe_removal_candidate), unit-tested independent of VM access"
affects: [02-11]

tech-stack:
  added: []
  patterns:
    - "VM-level test asserts exclusively against the target's own package manager (apt-mark showmanual), never pc-switcher's log text -- the one exception being apt-cache rdepends output used only for safety-selecting the test's subject package before either machine's state is touched."
    - "Review automation driven via PCSWITCHER_PACKAGE_REVIEW_AUTOMATION as a shell env-var prefix on the remote sync command, not a CLI flag -- keeps D-26's hidden-hook contract intact."
    - "Pure logic extracted from an integration-only test module into free functions, unit-tested by importing them directly from the integration test file (no VM dependency at import time; conftest.py's session-scoped VM-check fixture never executes unless a test in that package actually runs, and -m 'not integration' already deselects those by default)."

key-files:
  created:
    - tests/integration/jobs/test_package_sync.py
    - tests/unit/jobs/test_package_sync_candidate_selection.py
  modified: []

key-decisions:
  - "Candidate selection queries pc1's apt-mark showmanual, intersects with pc2's installed set (dpkg-query, `install ok installed` status only), then filters to packages whose `apt-cache rdepends --installed` names no manually-installed package on pc2 -- an installed-but-only-automatically-installed reverse dependency does not disqualify a candidate, matching the plan's literal 'manually-installed packages' wording (T-02-28)."
  - "The pure parsing/selection functions (nonblank_lines, parse_dpkg_installed, parse_reverse_depends, parse_batched_rdepends, pick_safe_removal_candidate) are module-level, non-underscore-prefixed names in the integration test file specifically so a companion unit test file can import them directly without tripping a private-import lint rule, and so they get fast VM-independent coverage per the orchestrator's directive."
  - "sync_jobs in the test config lists only `apt_sync: true` -- Configuration.sync_jobs is iterated as-is from the YAML dict (config.py) with no schema-default injection, so folder_sync/dummy_success (defaulting true/true in the schema) are never instantiated when absent from the dict. No explicit `false` needed, verified against config.py/orchestrator.py source."
  - "The real sync command uses --allow-first-sync (mutating test) and --dry-run (read-only test) rather than pre-seeding sync-history: reset_pcswitcher_state wipes sync-history before each test, which would otherwise trip the W1 first-sync gate non-interactively; --dry-run independently bypasses that gate per ADR-014, matching test_end_to_end_sync.py's own established precedent for both flags."

requirements-completed: []

coverage:
  - id: D1
    description: "A real pc-switcher sync from pc1 reinstalls on pc2 a package removed from pc2, proven by querying pc2's own apt-mark showmanual rather than reading pc-switcher's log output"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestAptSyncEndToEnd::test_apt_sync_installs_missing_package"
        status: pending_ci
    human_judgment: true
    rationale: "No VM access in this environment (HCLOUD_TOKEN and PC_SWITCHER_TEST_PC1_HOST/PC_SWITCHER_TEST_PC2_HOST unset). This project's established pattern runs integration tests in GitHub Actions CI on a PR targeting main. Test collection, ruff, basedpyright, and the full unit suite are verified in this session instead (see 'Pending CI verification' below)."
  - id: D2
    description: "The same run proves the coordinated pipeline end to end: the package job planned, the single review was rendered, the approved item was applied, and the sync exited 0"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestAptSyncEndToEnd::test_apt_sync_installs_missing_package"
        status: pending_ci
    human_judgment: true
    rationale: "Same VM-access constraint as D1 -- deferred to CI."
  - id: D3
    description: "A --dry-run variant of the same scenario changes nothing on pc2, proving ADR-014's read-only preview contract holds for a package job"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestAptSyncEndToEnd::test_apt_sync_dry_run_changes_nothing"
        status: pending_ci
    human_judgment: true
    rationale: "Same VM-access constraint as D1 -- deferred to CI."
  - id: D4
    description: "The test selects its subject package by querying the VMs rather than hardcoding a name, and skips with a message naming what it searched for when no safe candidate exists"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_candidate_selection.py::TestPickSafeRemovalCandidate::test_no_intersection_yields_none"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_candidate_selection.py::TestPickSafeRemovalCandidate::test_all_candidates_unsafe_yields_none"
        status: pass
    human_judgment: false
  - id: D5
    description: "Candidate-selection parsing helpers (dpkg-query status filtering, apt-cache rdepends block parsing, batched multi-candidate output splitting) are correct in isolation, independent of VM access"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_candidate_selection.py::TestParseDpkgInstalled::test_only_install_ok_installed_counts"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_candidate_selection.py::TestParseReverseDepends::test_takes_first_token_of_each_dependency_line"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_candidate_selection.py::TestParseBatchedRdepends::test_splits_multiple_candidate_blocks"
        status: pass
    human_judgment: false

duration: 22min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 13: VM-Level End-to-End Test for the apt_sync Tracer Path Summary

**`tests/integration/jobs/test_package_sync.py` proves plan 02-03's tracer claim against real pc1/pc2 VMs -- a package removed from the target is reinstalled by a real `pc-switcher sync`, asserted against the target's own `apt-mark showmanual`, with a `--dry-run` companion proving ADR-014's read-only contract -- while the candidate-selection logic gets independent, VM-free unit coverage.**

## Performance

- **Duration:** 22 min
- **Tasks:** 1
- **Files modified:** 2 (2 created, 0 modified)

## Accomplishments

- `tests/integration/jobs/test_package_sync.py`: `TestAptSyncEndToEnd` with `test_apt_sync_installs_missing_package` and `test_apt_sync_dry_run_changes_nothing`. Both select a subject package by querying pc1's `apt-mark showmanual` intersected with pc2's installed set (`dpkg-query`), filtered by a batched `apt-cache rdepends --installed` safety check (no manually-installed reverse dependency on pc2), skip with a message naming the search when nothing qualifies, drive the review via `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` (D-26's hidden hook), assert against pc2's own `apt-mark showmanual` output, and restore pc2's package state in a `finally` block regardless of outcome.
- Five pure helper functions (`nonblank_lines`, `parse_dpkg_installed`, `parse_reverse_depends`, `parse_batched_rdepends`, `pick_safe_removal_candidate`) factored out of the integration test's I/O so the selection logic is unit-testable.
- `tests/unit/jobs/test_package_sync_candidate_selection.py`: 16 new unit tests covering every pure helper directly (imported from the integration module; no VM dependency, no side effects at import time).
- A minimal apt_sync-only test config (`sync_jobs: {apt_sync: true}`), verified against `config.py`/`orchestrator.py` source to confirm job names absent from the dict are never instantiated -- no need to explicitly disable `folder_sync`.

## Task Commits

1. **Task 1: VM-isolated end-to-end test for the tracer path** - `d150bce` (test)

**Plan metadata:** (this commit)

## Files Created/Modified

- `tests/integration/jobs/test_package_sync.py` - `TestAptSyncEndToEnd` (2 tests), candidate-selection/parsing helpers, apt_sync-only test config, review-automation env-var builder, package-restore teardown
- `tests/unit/jobs/test_package_sync_candidate_selection.py` - 16 unit tests for the pure helpers above

## Decisions Made

See `key-decisions` in frontmatter: reverse-dependency filter scoped to manually-installed packages only (per the plan's literal wording), non-underscore-prefixed helper names for clean cross-module unit-test imports, minimal `sync_jobs` dict relying on the orchestrator's "absent = never instantiated" behavior, and `--allow-first-sync`/`--dry-run` flag usage matching `test_end_to_end_sync.py`'s established precedent for the W1 first-sync gate.

## Deviations from Plan

### Auto-fixed Issues

None - plan executed exactly as written for the one in-scope file. One addition beyond the plan's stated `files_modified` (which listed only `tests/integration/jobs/test_package_sync.py`):

**1. [Rule 2 - auto-add missing critical functionality, per orchestrator directive] Added a companion unit test file for the pure candidate-selection helpers**
- **Found during:** Task 1, while designing the candidate-selection logic
- **Issue:** The plan's `files_modified` names only the integration test file, but the orchestrator's explicit directive requires "any pure-logic helper you add for the tests is unit-testable and unit-tested." The candidate-selection/parsing logic (dpkg-query status filtering, apt-cache rdepends block parsing, safety-filtered candidate picking) is pure Python with no I/O of its own.
- **Fix:** Extracted five pure functions as non-underscore-prefixed module-level names in `test_package_sync.py` and added `tests/unit/jobs/test_package_sync_candidate_selection.py`, importing them directly for VM-independent coverage.
- **Files modified:** `tests/unit/jobs/test_package_sync_candidate_selection.py` (new)
- **Verification:** 16/16 new unit tests pass; full unit suite green (815 passed, up from 799).
- **Committed in:** `d150bce`

---

**Total deviations:** 1 auto-added file beyond the plan's stated scope, directed by the orchestrator's explicit instruction.
**Impact on plan:** No scope creep in behavior -- the addition is test-only coverage for logic the plan's own task already required ("The test selects its subject package by querying the VMs rather than hardcoding a name").

## Issues Encountered

None.

## Pending CI verification

This environment has no VM access (`HCLOUD_TOKEN` and `PC_SWITCHER_TEST_PC1_HOST`/`PC_SWITCHER_TEST_PC2_HOST` are unset), matching this project's established pattern: integration tests run in GitHub Actions CI on a PR targeting `main`, not on a developer machine (ADR-008). Per the orchestrator's explicit directive, no local fake-VM harness was built and no acceptance criterion requiring real VM execution was marked verified here.

What was verified in this session instead:
- `uv run pytest tests/integration/jobs/test_package_sync.py --collect-only -q -m integration` lists both tests (`test_apt_sync_installs_missing_package`, `test_apt_sync_dry_run_changes_nothing`).
- `uv run ruff check tests/integration/jobs/test_package_sync.py tests/unit/jobs/test_package_sync_candidate_selection.py` and `uv run ruff format --check` on both files are clean.
- `uv run basedpyright tests/integration/jobs/test_package_sync.py tests/unit/jobs/test_package_sync_candidate_selection.py` reports 0 errors/warnings/notes, and a full-project `basedpyright` run is also clean.
- `uv run pytest -q` (full suite, default `-m "not integration"`): 815 passed (up from 799), 63 deselected -- the two new integration tests are correctly deselected by default, confirming they don't run without explicit `-m integration` + VM env vars.

What remains genuinely unverified until CI runs `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py` against real pc1/pc2 VMs:
- That a real candidate package exists on the live VM fleet satisfying the selection criteria (or that the skip path fires correctly if not).
- That `apt-get remove`/`apt-get install` behave as expected over the real multiplexed SSH channel with real sudo.
- That the coordinator's batched review, driven via `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` inside the actual sync process spawned over SSH, reaches `AptSyncJob.apply()` and installs the approved item.
- That `--dry-run` genuinely issues zero mutating commands against a real target (unit-covered via mocked executors in plan 02-03/02-05; this is the first real-apt proof).

**Exact command CI runs:** `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py`

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

`tests/integration/jobs/test_package_sync.py` is now the phase's integration-test module with its fixture and teardown patterns established (module-scoped `pc1_with_pcswitcher_mod`, function-scoped `pc2_with_pcswitcher` + `reset_pcswitcher_state`, `finally`-block package-state restoration, VM-query-driven candidate selection with a named skip path). Plan 02-11 adds the whole-run non-interactive/continue-on-failure/snap/flatpak contract tests to this same file once `snap_sync`/`flatpak_sync` exist, reusing these conventions directly.

The tracer's own VM-level verification debt (deferred here from plans 02-03 and 02-05's summaries) is now structurally closed -- the test exists and is correctly wired -- but the actual real-hardware proof stays pending until CI executes it on the next non-draft PR.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: tests/integration/jobs/test_package_sync.py
- FOUND: tests/unit/jobs/test_package_sync_candidate_selection.py
- FOUND: commit d150bce
