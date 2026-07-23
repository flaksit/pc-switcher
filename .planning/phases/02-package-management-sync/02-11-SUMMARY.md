---
phase: 02-package-management-sync
plan: 11
subsystem: testing
tags: [integration-test, apt, snap, flatpak, package-sync, d-26, d-27, d-06, d-08, d-24, adr-020]

requires:
  - phase: 02-package-management-sync
    provides: "apt_sync/snap_sync/flatpak_sync, PackagePhaseCoordinator, package_review.py's automation hook, package_state.py's DecisionFile/SnippetRegistry (plans 02-02 through 02-10)"
  - phase: 02-package-management-sync
    provides: "tests/integration/jobs/test_package_sync.py's fixture, teardown and candidate-selection conventions (plan 02-13)"
provides:
  - "tests/integration/jobs/test_package_sync.py: TestPackageSyncWholeRunContracts, six VM-level tests proving the phase's whole-run behavioural contracts"
  - "Generalized candidate-selection/config-writing/automation helpers (plural variants) reused by all six tests without changing the tracer's existing single-item behavior"
  - "A truthfully-filled .planning/phases/02-package-management-sync/02-VALIDATION.md with real task IDs, measured runtime, and an honest nyquist_compliant=false pending CI"
affects: [02-12]

tech-stack:
  added: []
  patterns:
    - "The continue-on-failure item is deliberately an UNREPRODUCIBLE (unowned-install snippet) diff, never an AptPackageItem: AptSyncJob.plan()'s sorted() call gives every APT_PACKAGE diff a lower/equal rank than UNREPRODUCIBLE with a stable-sort tie-break that always places apt-package diffs first, so an apt-only failure could never sit between two other apt installs in convergence order. Three unowned-install snippets under /opt, named so scan_unowned_installs's alphabetical sort places the failing one strictly between the two succeeding ones, is what makes 'the item after the failure was still processed' a real ordered claim rather than merely 'some other item also ran'."
    - "Snippets are authored directly via SnippetRegistry(pc1_executor).add(Snippet(...)) in the test fixture, bypassing the interactive per-entry capture prompt entirely -- the production class's own read/write contract is exercised, not a hand-rolled YAML string."
    - "SKIP_ALWAYS on a regular (non-unreproducible) item has no interactive checkbox UI path yet (package_review.py's own docstring); PACKAGE_REVIEW_AUTOMATION_ENV accepts it anyway since the hook only pre-answers items the diff already produced. test_skip_always_is_inert_in_both_roles uses this to prove PackageSyncJob._record_permanent_skips/filter_inert's mechanism independent of that UI gap."
    - "test_all_managers_diff_before_any_applies is the one test whose primary claim is ordering, not end state, matching this plan's own prohibition's explicit carve-out; it locates PackagePhaseCoordinator's 'N package manager(s) planned; review covers' log line and asserts it precedes the first per-item converge success log from either manager."
    - "A skip-always decision on a regular item is driven through the same PACKAGE_REVIEW_AUTOMATION_ENV hook every other test in this module uses; forcing Decision.APPLY on a second/reversed-direction sync and asserting the package stays untouched is what proves the item never became a diff at all, not merely that the tool chose not to touch it."

key-files:
  created:
    - .planning/phases/02-package-management-sync/02-11-SUMMARY.md
  modified:
    - tests/integration/jobs/test_package_sync.py
    - .planning/phases/02-package-management-sync/02-VALIDATION.md

key-decisions:
  - "Continue-on-failure's three items are all ItemClass.UNREPRODUCIBLE (unowned /opt markers with authored snippets), not a mix of AptPackageItem installs and one snippet, because AptSyncJob.plan()'s stable sort structurally cannot place an UNREPRODUCIBLE diff before any APT_PACKAGE diff -- the two succeeding 'installs' are unreproducible-item snippets whose bodies happen to run real apt-get install commands, verified via pc2's own apt-mark showmanual afterward."
  - "pick_safe_removal_candidate/_find_removable_candidate keep their exact original signatures and behavior, now implemented by delegating to new plural pick_safe_removal_candidates/_find_removable_candidates(count=N) helpers -- the companion unit test file (test_package_sync_candidate_selection.py) needed zero changes."
  - "_automation_env_assignment_multi(decisions_by_item_id) generalizes the tracer's single-item _automation_env_assignment(item_id), which now delegates to it -- lets tests pre-answer several items (or a non-APPLY decision like SKIP_ALWAYS) in one review without a second automation mechanism."
  - "Snap/flatpak name-and-revision/scope parsing is reimplemented locally (parse_snap_list_names_revisions, parse_flatpak_list_lines) rather than importing snap_sync.py's/flatpak_sync.py's private (underscore-prefixed) parsers, matching this module's existing convention of not reaching into another module's private names."
  - "02-VALIDATION.md's Per-Task Verification Map rows use the EXACT <verify><automated> command text from each plan's task, not a shortened/derived form, so the map's own acceptance criterion ('every row's automated command matches a <verify><automated> command in the plan it names') is a literal, checkable fact."
  - "nyquist_compliant stays false: the two VM-integration rows (02-13.1, 02-11.1) have a correct, existing automated command that has not been RUN against real VMs in this environment. This is 'pending CI', not 'pending existence' -- documented as an explicit unchecked sign-off item rather than silently flipped to true."

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "A VM-isolated integration test proves a non-interactive run applies nothing, records nothing permanently, and reports every unresolved item (D-26)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestPackageSyncWholeRunContracts::test_non_interactive_skip_all"
        status: pending_ci
    human_judgment: true
    rationale: "No VM access in this environment (HCLOUD_TOKEN/PC_SWITCHER_TEST_PC1_HOST/PC_SWITCHER_TEST_PC2_HOST unset). Runs in GitHub Actions CI on the next non-draft PR (ADR-008). Collection, ruff, basedpyright and the full unit suite verified locally instead (see Pending CI verification)."
  - id: D2
    description: "A VM-isolated integration test proves a failing item does not stop the job -- the item after it still converges, its stderr is in the summary, and the job result is a failure (D-27) -- using a deliberately failing install snippet, not a REPO_UNAVAILABLE package that would never reach converge"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestPackageSyncWholeRunContracts::test_continue_on_item_failure"
        status: pending_ci
    human_judgment: true
    rationale: "Same VM-access constraint as D1 -- deferred to CI."
  - id: D3
    description: "A VM-isolated integration test proves snap convergence lands the target on the source's revision and leaves snap get system refresh.hold unchanged on both machines (D-06)"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestPackageSyncWholeRunContracts::test_snap_revision_converges_without_hold"
        status: pending_ci
    human_judgment: true
    rationale: "Same VM-access constraint as D1 -- deferred to CI."
  - id: D4
    description: "A VM-isolated integration test proves flatpak convergence installs into the source item's scope and provisions the remote first (D-06, D-14), proven via the run's own per-item converge log for ordering"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestPackageSyncWholeRunContracts::test_flatpak_installs_into_source_scope_after_remote"
        status: pending_ci
    human_judgment: true
    rationale: "Same VM-access constraint as D1 -- deferred to CI."
  - id: D5
    description: "A VM-isolated integration test proves a skip-always decision recorded in one run makes the item produce no diff in the next run, in both source and target roles (D-08)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestPackageSyncWholeRunContracts::test_skip_always_is_inert_in_both_roles"
        status: pending_ci
    human_judgment: true
    rationale: "Same VM-access constraint as D1 -- deferred to CI."
  - id: D6
    description: "A VM-isolated integration test proves the cross-manager batched review: no manager's first mutating command runs before every enabled manager has produced its diff (D-24)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: integration
        ref: "tests/integration/jobs/test_package_sync.py::TestPackageSyncWholeRunContracts::test_all_managers_diff_before_any_applies"
        status: pending_ci
    human_judgment: true
    rationale: "Same VM-access constraint as D1 -- deferred to CI."
  - id: D7
    description: "02-VALIDATION.md's Per-Task Verification Map is filled with real task ids and its frontmatter records nyquist_compliant truthfully"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "manual review of .planning/phases/02-package-management-sync/02-VALIDATION.md against every named plan's <verify><automated> text; grep -c 'TBD' + uv run pytest tests/unit/jobs/ -x (974/974 unit suite green)"
        status: pass
    human_judgment: false

duration: 32min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 11: VM Integration Tests + Validation Record Summary

**Six VM-isolated integration tests proving phase 2's whole-run contracts (non-interactive skip-all, continue-on-failure, snap hold-free convergence, flatpak scoped remote-before-ref, skip-always inertness in both roles, cross-manager batched-review ordering), plus a truthfully-filled 02-VALIDATION.md with real task IDs and a measured feedback-latency ceiling.**

## Performance

- **Duration:** 32 min
- **Tasks:** 2
- **Files modified:** 2 (1 created, 2 modified — SUMMARY.md counted separately)

## Accomplishments

- `tests/integration/jobs/test_package_sync.py`: `TestPackageSyncWholeRunContracts` with `test_non_interactive_skip_all`, `test_continue_on_item_failure`, `test_snap_revision_converges_without_hold`, `test_flatpak_installs_into_source_scope_after_remote`, `test_skip_always_is_inert_in_both_roles`, and `test_all_managers_diff_before_any_applies` — 6 new tests, 8 total in the module alongside the tracer's 2.
- `test_continue_on_item_failure` authors three install snippets directly into pc1's `SnippetRegistry` (D-18/D-20) on freshly-created unowned `/opt` marker directories — two run real `apt-get install` commands, one deliberately exits 42 — relying on `AptSyncJob.scan_unowned_installs`'s alphabetical sort to place the failing item strictly between the two succeeding ones in convergence order.
- Generalized helpers added alongside the tracer's originals without changing their behavior: `pick_safe_removal_candidates`/`_find_removable_candidates` (plural, `count` parameter), `_package_sync_test_config`/`_write_package_sync_config` (arbitrary job combinations), `_automation_env_assignment_multi` (multi-item/non-APPLY decisions), plus snap (`parse_snap_list_names_revisions`, `parse_snap_info_revisions`, `_find_divergeable_snap`, `_find_removable_snap_candidate`) and flatpak (`parse_flatpak_list_lines`, `_find_flatpak_ref_and_remote`) query helpers independent of those jobs' own private parsers.
- `.planning/phases/02-package-management-sync/02-VALIDATION.md`: Per-Task Verification Map replaced with 18 rows keyed by real task IDs (02-02.2 through 02-13.1), each row's automated command copied verbatim from its plan's `<verify><automated>` block; Wave 0 checklist fully ticked; three new Manual-Only rows (package-legitimacy checkpoint, Live-display composition checkpoint, on-the-fly snippet-capture UI); measured feedback latency (`tests/unit/jobs/ -x`: 407 tests, 3.07s) with a 10s ceiling.

## Task Commits

1. **Task 1: Integration tests for the whole-run contracts** - `ffd9a45` (test)
2. **Task 2: Fill the validation record truthfully** - `a3b8a00` (docs)

**Plan metadata:** (this commit)

## Files Created/Modified

- `tests/integration/jobs/test_package_sync.py` - `TestPackageSyncWholeRunContracts` (6 tests), plural candidate-selection/config/automation helpers, unowned-marker and snippet-authoring helpers, snap/flatpak query helpers
- `.planning/phases/02-package-management-sync/02-VALIDATION.md` - Per-Task Verification Map, Wave 0 checklist, Manual-Only Verifications, Validation Sign-Off, frontmatter (`wave_0_complete: true`, `nyquist_compliant: false` with rationale)

## Decisions Made

See `key-decisions` in frontmatter: unreproducible-snippet design for the continue-on-failure ordering proof (forced by `AptSyncJob.plan()`'s stable-sort tie-break, which always ranks `APT_PACKAGE` diffs ahead of `UNREPRODUCIBLE` ones), direct `SnippetRegistry`-based authoring bypassing the interactive capture path, plural-variant generalization of the tracer's helpers with zero behavior change to the originals, `PACKAGE_REVIEW_AUTOMATION_ENV` driving `SKIP_ALWAYS` on a regular item ahead of its still-nonexistent interactive UI path, exact-text automated-command copying in the validation map, and the honest `nyquist_compliant: false` pending real CI execution.

## Deviations from Plan

### Auto-fixed Issues

None — plan executed as written for both tasks' stated `files_modified`. One clarification worth recording as a discovered structural constraint, not a deviation from the plan's own acceptance criteria (the plan's action text already anticipated and required exactly this outcome):

**1. [Structural finding, not a deviation] `test_continue_on_item_failure`'s three items are all `ItemClass.UNREPRODUCIBLE`, never a mix with `AptPackageItem`**
- **Found during:** Task 1, while designing the test to satisfy "the failing item sits between two items that must both still be attempted"
- **Finding:** `AptSyncJob.plan()`'s `sorted(..., key=lambda diff: _ITEM_CLASS_ORDER.get(diff.item_class, 3))` gives every `APT_PACKAGE` diff and every `UNREPRODUCIBLE` diff the same default rank (3); Python's stable sort then preserves the tuple's construction order, which places `base_plan.diffs` (all `APT_PACKAGE`) before `unreproducible_diffs` unconditionally. An `AptPackageItem` install can therefore never be ordered after an `UNREPRODUCIBLE` diff in `apply_diffs`, so "sandwiching" the failure between two apt-package installs is structurally impossible with the current converge ordering.
- **Resolution:** All three items (the two succeeding "installs" and the failing one) are `UNREPRODUCIBLE` (unowned-install) items with names chosen so `scan_unowned_installs`'s alphabetical sort places them in the required order. The two succeeding items' snippet bodies literally run `apt-get install -y <pkg>`, so the test's assertion against pc2's own `apt-mark showmanual` still proves a real package install happened after the failure — matching the plan's literal acceptance criterion ("both viable packages are installed on pc2 afterwards") without depending on `AptPackageItem`'s own diff class.
- **Files modified:** `tests/integration/jobs/test_package_sync.py` (within the plan's own declared scope)
- **Verification:** Collection lists the test; ruff/basedpyright clean; full unit suite green (974/974). Real ordering behavior is pending CI (no local VM access).

---

**Total deviations:** 0 rule-triggered auto-fixes. One structural design finding, resolved within the plan's own stated scope and acceptance criteria.
**Impact on plan:** None on scope; the finding only affected which item class the test's three snippets use, not the plan's `files_modified`, task count, or acceptance criteria.

## Issues Encountered

None.

## Pending CI verification

This environment has no VM access (`HCLOUD_TOKEN`, `PC_SWITCHER_TEST_PC1_HOST`, `PC_SWITCHER_TEST_PC2_HOST` all unset), matching this project's established pattern (ADR-008): integration tests run in GitHub Actions CI on a PR targeting `main`, not on a developer machine. Per the orchestrator's explicit directive, no local fake-VM harness was built and no VM-dependent acceptance criterion was marked verified here.

What was verified in this session instead:
- `uv run pytest tests/integration/jobs/test_package_sync.py --collect-only -q -m integration` lists all 8 tests: the tracer's `test_apt_sync_installs_missing_package`/`test_apt_sync_dry_run_changes_nothing` plus this plan's six whole-run contract tests.
- `uv run ruff check tests/integration/jobs/test_package_sync.py` and `uv run ruff format --check` on the same file are clean.
- `uv run basedpyright tests/integration/jobs/test_package_sync.py` reports 0 errors/warnings/notes; a full-project `uv run basedpyright` run is also clean (0/0/0).
- `uv run pytest -q` (full suite, default `-m "not integration"`): 974 passed, 69 deselected — the eight integration tests (2 tracer + 6 new) are correctly deselected by default.
- `tests/unit/jobs/test_package_sync_candidate_selection.py` (16 tests) still passes unchanged, confirming the plural-variant refactor of `pick_safe_removal_candidate`/`_find_removable_candidate` preserved the original single-item behavior those unit tests exercise.
- Every unit-test row in `02-VALIDATION.md`'s Per-Task Verification Map was individually run and confirmed green in this session (18 rows total; 16 unit rows pass, 2 VM-integration rows pending CI as above).

What remains genuinely unverified until CI runs `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py` against real pc1/pc2 VMs:
- That every candidate-selection query (apt reverse-dependency safety, snap revision-alternate discovery, flatpak ref+remote discovery) finds a real subject on the live VM fleet, or that each skip path fires correctly if not.
- That the continue-on-failure snippet trio genuinely sorts and converges in the intended a→b(fail)→c order against real `find`/`dpkg -S` output, and that the failure's stderr text (`"deliberate integration-test failure"`) actually surfaces in the sync's captured stdout/stderr the way this session's log-routing analysis predicts.
- That `snap refresh --revision=<alternate>` and the flatpak remote/ref uninstall-then-reinstall sequences behave as expected over the real multiplexed SSH channel with real sudo.
- That the coordinator's "N package manager(s) planned; review covers" log line and each manager's per-item converge success log actually appear in the SSH-captured output in the predicted order and format.

**Exact command CI runs:** `tests/run-integration-tests.sh tests/integration/jobs/test_package_sync.py`

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

Phase 2's whole-run contracts now have both the code (plans 02-02 through 02-10) and the VM-level tests proving them (this plan, plus 02-13's tracer). `02-VALIDATION.md` names every requirement-bearing task's automated verification and is honest about what remains CI-pending. Plan 02-12 (documentation) is the phase's last remaining plan — it can now cite `02-VALIDATION.md`'s Per-Task Verification Map as the phase's test-coverage record without needing to re-derive it from RESEARCH.md. `.planning/REQUIREMENTS.md` was deliberately left untouched per the orchestrator's directive; the orchestrator marks requirements complete at phase end.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: tests/integration/jobs/test_package_sync.py
- FOUND: .planning/phases/02-package-management-sync/02-VALIDATION.md
- FOUND: .planning/phases/02-package-management-sync/02-11-SUMMARY.md
- FOUND: commit ffd9a45
- FOUND: commit a3b8a00
