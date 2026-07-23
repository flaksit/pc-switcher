---
phase: 02-package-management-sync
plan: 07
subsystem: infra
tags: [apt, package-sync, snippet-registry, config-sync, unreproducible-items, d-18, d-19, d-20, d-21, d-23, tdd]

requires:
  - phase: 02-package-management-sync
    provides: "PackageSyncJob plan()/apply() split, DecisionFile/filter_inert (D-08/D-08a/D-09), PackagePhaseCoordinator, review_items()'s batched checkbox flow, UnreproducibleItem shape (plans 02-03, 02-04, 02-05, 02-06)"
provides:
  - "package_state.py: SnippetRegistry/Snippet/SNIPPET_REGISTRY_RELPATH — the shared, synced counterpart to DecisionFile; opaque blob storage and replay (D-20, D-23)"
  - "config_sync.py: SYNCED_CONFIG_FILENAMES generalises config sync from one hardcoded config.yaml to a tuple config sync iterates; the registry now actually reaches the target"
  - "apt_sync.py: scan_unowned_installs() and _scan_no_candidate_apt_packages() feed DiffClass.UNREPRODUCIBLE diffs through the normal pipeline; AptSyncJob._build_review_groups carves unresolved ones into their own review group; converge() replays snippets"
  - "package_review.py: UNREPRODUCIBLE_REVIEW_ACTION marks a ReviewGroup for the three-way per-entry resolution flow (add snippet / skip-always / skip-once); ReviewOutcome.snippets/unresolved"
  - "package_sync_core.py: PackageSyncJob.apply() writes authored snippets + unreproducible skip-always decisions (_finalize_unreproducible) and fails the job when anything is left unresolved after an interactive review (_unresolved_as_failures, D-21/D-27)"
  - "package_phase.py: PackagePhaseCoordinator._slice_for propagates snippets/unresolved per job, not just decisions"
affects: [02-08, 02-09, 02-10, 02-11, 02-12, 02-13]

tech-stack:
  added: []
  patterns:
    - "SnippetRegistry mirrors DecisionFile's one-Executor-per-instance shape (construct with self.source to write, self.target to read/replay) but is NOT machine-scoped data — both machines may hold different copies of the same file until config_sync reconciles them, unlike a decision file which is deliberately never synced."
    - "A ReviewGroup's `action` string is package_review's own interaction-kind sentinel, not always a DiffAction value: UNREPRODUCIBLE_REVIEW_ACTION routes a group through the three-way per-entry resolution flow instead of the checkbox tick, entirely independent of the underlying diff's real action (REPORT_ONLY/INSTALL)."
    - "AptSyncJob overrides _build_review_groups (not package_sync_core.py's _ACTION_VOCABULARY/_ACTION_ORDER) to carve out just-discovered UNREPRODUCIBLE diffs into their own group — an item that already has a snippet from a prior run flows through the ordinary install-direction grouping instead, since it is already resolved."

key-files:
  created: []
  modified:
    - src/pcswitcher/jobs/package_state.py
    - src/pcswitcher/jobs/apt_sync.py
    - src/pcswitcher/config_sync.py
    - src/pcswitcher/jobs/package_review.py
    - src/pcswitcher/jobs/package_sync_core.py
    - src/pcswitcher/jobs/package_phase.py
    - tests/unit/jobs/test_package_state.py
    - tests/unit/cli/test_config_sync.py
    - tests/unit/jobs/test_package_review.py
    - tests/unit/jobs/test_package_sync_core.py
    - tests/unit/jobs/test_package_phase.py
    - tests/integration/test_config_sync.py

key-decisions:
  - "package_sync_core.py and package_phase.py were modified despite not being in this plan's declared files_modified list. The plan's own action text explicitly describes apply()'s D-21 enforcement (writing snippets, failing the job on unresolved items) and PackagePhaseCoordinator's per-job outcome distribution — both structurally must live in those two files (apply() and _slice_for already exist there), and without touching them the plan's must_haves are unreachable. Treated as Rule 2 (auto-add missing critical functionality); both files' companion test suites were extended in the same commit."
  - "The apt-no-candidate detector was implemented as a new private method (_scan_no_candidate_apt_packages), NOT by reusing the existing same-named collect_unavailable_item_ids hook the plan's action text names. That hook already exists from plan 02-06 with a different, narrower contract: it asks whether the TARGET's own repos can install something MISSING there (D-25's REPO_UNAVAILABLE). D-18's question is unrelated — whether the SOURCE's own apt-cache can reproduce something it itself installed, independent of the target's current state — so reusing the name would have silently collided two different semantics under one signature."
  - "apply()'s total==0 early return was removed (replaced with an if/else that still runs the post-loop unresolved check) so a run whose ONLY diffs are unreproducible items — zero INSTALL/CHANGE/REMOVE work — still fails when one of them is left unresolved after an interactive review."
  - "AptSyncJob.accept_review()'s synthetic-metadata-refresh-marker ReviewOutcome reconstruction was fixed to carry outcome.snippets/unresolved through verbatim; the pre-existing rebuild (adding the marker's decision) would otherwise have silently dropped both fields whenever a repository-group item was also approved in the same run."
  - "SnippetRegistry.replay never raises for 'no snippet registered' (a plan/apply-time race where the registry changed underneath the run) — it returns a failed CommandResult instead, so that case is a per-item failure like any other (D-27), never a crash that stops the whole job."

patterns-established:
  - "A shared, synced counterpart to a machine-local store (SnippetRegistry vs DecisionFile) reuses the identical atomic-write shape and Executor-per-instance construction, differing only in which machine's copy load()/add() target and in never being excluded from config_sync."

requirements-completed: []  # Per orchestrator directive: two more plans (02-08, 02-09) also serve REQ-sync-scope-packages/REQ-conflict-detection-no-resolution; not marked complete here.

coverage:
  - id: D1
    description: "Apt packages installed on the source that no configured repository offers a candidate for are detected and surfaced as unreproducible items rather than proposed as installs (D-18)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestUnreproducibleDetection::test_no_candidate_source_package_becomes_unreproducible_diff"
        status: pass
    human_judgment: false
  - id: D2
    description: "Files under /usr/local and /opt that no dpkg package owns are detected and surfaced as unreproducible items, bounded to a shallow scan that never walks a whole tree (D-18, D-19)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestUnreproducibleDetection::test_scan_unowned_installs_yields_two_items_from_four_candidates"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestUnreproducibleDetection::test_unowned_scan_queries_only_usr_local_and_opt"
        status: pass
    human_judgment: false
  - id: D3
    description: "After an interactive review, an unresolved unreproducible item makes the job's result a failure even when every converge succeeded, and it recurs every run until resolved (D-21, D-27)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestUnresolvedFailsTheJob::test_interactive_unresolved_raises_naming_the_item_even_with_no_converge_failure"
        status: pass
    human_judgment: false
  - id: D4
    description: "A non-interactive run follows D-26 exactly: nothing applied, nothing recorded, everything unresolved reported, and unresolved items alone do not fail the job; a dry-run behaves the same way for the same reason (ADR-014)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestUnresolvedFailsTheJob::test_non_interactive_unresolved_does_not_raise_on_that_basis_alone"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestUnresolvedFailsTheJob::test_dry_run_unresolved_does_not_raise_on_that_basis_alone"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestUnreproducibleGroupResolution::test_non_interactive_offers_no_capture_and_marks_every_item_unresolved"
        status: pass
    human_judgment: false
  - id: D5
    description: "The review offers adding a snippet on the fly, so resolving an unreproducible item never requires leaving the sync (D-21), and an authored body reaches the outcome verbatim (D-20)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestUnreproducibleGroupResolution::test_add_snippet_choice_captures_body_verbatim_including_whitespace"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_review.py::TestUnreproducibleGroupResolution::test_skip_always_choice_yields_skip_always_decision_and_no_snippet"
        status: pass
    human_judgment: false
  - id: D6
    description: "The snippet registry actually reaches the target: config sync carries package-snippets.yaml alongside config.yaml, a source with no registry transfers config.yaml only, and *.decisions.yaml never travels (D-09, D-23)"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/cli/test_config_sync.py::TestMultiFileSync::test_registry_present_on_source_absent_on_target_prompts_naming_it_and_copies"
        status: pass
      - kind: unit
        ref: "tests/unit/cli/test_config_sync.py::TestMultiFileSync::test_registry_absent_on_source_transfers_config_only_and_prompts_nothing_else"
        status: pass
      - kind: unit
        ref: "tests/unit/cli/test_config_sync.py::TestMultiFileSync::test_decisions_file_never_among_transferred_paths"
        status: pass
    human_judgment: false
  - id: D7
    description: "A snippet is replayed as an opaque text blob — one argv-quoted argument, login_shell=False, no stdin — and its exit code alone decides success; a non-zero replay is a per-item failure that does not stop the job (D-20, D-27)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestSnippetRegistry::test_replay_passes_body_as_one_quoted_argument_with_login_shell_false"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestUnreproducibleDetection::test_failed_snippet_replay_is_a_per_item_failure_and_does_not_stop_the_job"
        status: pass
    human_judgment: false
  - id: D8
    description: "Registry writes are atomic and preserve entries the current run did not touch; an absent registry loads as an empty mapping without raising"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestSnippetRegistry::test_add_preserves_an_unrelated_pre_existing_entry"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestSnippetRegistry::test_absent_file_returns_empty_mapping"
        status: pass
    human_judgment: false
  - id: D9
    description: "A dry-run on the reference machine surfaces the four no-candidate apt packages and the unowned trees under /usr/local and /opt as unreproducible items, against real pc1/pc2 VMs"
    verification: []
    human_judgment: true
    rationale: "This plan's own <verification> section names a VM-level dry-run check; this autonomous run has no VM access, matching the precedent every prior plan in this phase (02-03 through 02-06) set for its own VM-level proof (deferred to plan 02-13's end-to-end suite)."

duration: 90min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 07: Unreproducible Items and the Snippet Registry Summary

**apt packages with no source-side install candidate and unowned files under `/usr/local`/`/opt` are detected and routed through a three-way review resolution (snippet / machine-specific / skip), snippets replay as opaque blobs via a registry that config sync now actually carries to the target, and an interactive review left with anything unresolved fails the job every run until it is resolved.**

## Performance

- **Duration:** ~90 min
- **Started:** 2026-07-23T~12:15:00+02:00 (approximate — session start time not captured)
- **Completed:** 2026-07-23T13:44:48+02:00
- **Tasks:** 2
- **Files modified:** 12 (6 source, 6 test)

## Accomplishments

- `package_state.py` gained `SnippetRegistry`/`Snippet`/`SNIPPET_REGISTRY_RELPATH`: the shared, synced counterpart to `DecisionFile`, reusing its atomic temp-then-move write shape. `replay()` builds `bash -c <shlex.quote(body)>` with `login_shell=False` and never raises for a missing snippet — it returns a failed `CommandResult` so a plan/apply-time race is a per-item failure, not a crash.
- `config_sync.py` generalised from one hardcoded `config.yaml` filename to `SYNCED_CONFIG_FILENAMES = ("config.yaml", "package-snippets.yaml")` — the fix for the plan's HIGH cross-AI review finding that the registry could not reach the target. `config.yaml`'s behavior is byte-identical (including the `RuntimeError` on a missing source file); every other file is optional, gets its own named three-scenario diff-and-confirm prompt, and the whole loop pauses the Live display at most once. `*.decisions.yaml` is explicitly excluded by a comment at the tuple's definition.
- `apt_sync.py`: `_scan_no_candidate_apt_packages` (one batched `apt-cache policy` over the source's own manually-installed set) and `scan_unowned_installs` (one batched `find` over `/usr/local`, `/opt`, plus the immediate children of `/usr/local/bin`/`/usr/local/lib`, checked against one batched `dpkg -S`) feed `_plan_unreproducible_diffs`, which filters items already recorded machine-specific on the source and sets `DiffAction.INSTALL` only when a target-side snippet already exists. `AptSyncJob._build_review_groups` carves still-unresolved diffs into their own group (`UNREPRODUCIBLE_REVIEW_ACTION`), presented after installs/removals; `converge()` replays a resolved item's snippet.
- `package_review.py`: `UNREPRODUCIBLE_REVIEW_ACTION` marks a group for a per-entry three-way `questionary.select` (add snippet / record machine-specific / skip for now) instead of the checkbox tick, with a printed authoring note (`DEBIAN_FRONTEND=noninteractive` worked shape) before the multi-line capture. `ReviewOutcome` gained `snippets`/`unresolved` fields with empty defaults, so every pre-existing hand-constructed `ReviewOutcome` in the codebase kept working unmodified.
- `package_sync_core.py`: `PackageSyncJob.apply()` now also calls `_finalize_unreproducible` (writes authored snippets and unreproducible-item skip-always decisions to the SOURCE — these items are always source-held) and `_unresolved_as_failures` (raises `PackageItemFailures` naming every still-unresolved item after an interactive, non-dry-run review, even when zero converge work happened this run). `package_phase.py`'s `PackagePhaseCoordinator._slice_for` now slices `snippets`/`unresolved` per job the same way it already sliced `decisions`.
- 91 new/modified unit tests across `test_package_state.py` (44, +18 new), `test_config_sync.py` (47, +10 new), `test_package_review.py` (22, +18 new), `test_package_sync_core.py` (19, +4 new), `test_package_phase.py` (11, +1 new); `tests/integration/test_config_sync.py`'s 4 direct private-function calls updated to the generalised signatures (compile-time fix only — no VM access to run them).

## Task Commits

Both tasks followed RED (test) then GREEN (feat):

1. **Task 1: Unreproducible-item detection and the snippet registry**
   - `29dc7e2` (test) — RED: verified failing via `git stash` of `package_state.py`/`apt_sync.py`/`config_sync.py` (`ImportError` on `Snippet`/`SnippetRegistry`/`SYNCED_CONFIG_FILENAMES`), then restored.
   - `d37726d` (feat) — GREEN: `SnippetRegistry`, apt-no-candidate + unowned-install detection, config sync generalisation.
2. **Task 2: Mandatory registration — resolve an unreproducible item inside the review**
   - `369a979` (test) — RED: verified failing via `git stash` of `package_review.py`/`package_sync_core.py`/`package_phase.py` (`ImportError` on `UNREPRODUCIBLE_REVIEW_ACTION`, `TypeError` on `ReviewOutcome`'s new fields), then restored.
   - `574f502` (feat) — GREEN: the three-way resolution flow, `apply()`'s D-21 enforcement, coordinator outcome slicing.

**Plan metadata:** (this commit)

## Files Created/Modified

- `src/pcswitcher/jobs/package_state.py` — `SnippetRegistry`, `Snippet`, `SNIPPET_REGISTRY_RELPATH`, `_serialize_snippets`/`_deserialize_snippets`
- `src/pcswitcher/config_sync.py` — `SYNCED_CONFIG_FILENAMES`, every private helper parameterised by `filename`, `sync_config_to_target`'s multi-file loop and single aggregate pause
- `src/pcswitcher/jobs/apt_sync.py` — `_scan_no_candidate_apt_packages`, `scan_unowned_installs`, `_plan_unreproducible_diffs`, `_build_review_groups` override, `_converge_unreproducible`, `accept_review`'s snippets/unresolved carry-through fix
- `src/pcswitcher/jobs/package_review.py` — `UNREPRODUCIBLE_REVIEW_ACTION`, `_review_unreproducible_group`, `_SNIPPET_AUTHORING_NOTE`, `ReviewOutcome.snippets`/`.unresolved`
- `src/pcswitcher/jobs/package_sync_core.py` — `_finalize_unreproducible`, `_unresolved_as_failures`, `apply()` restructured to always run the post-loop unresolved check
- `src/pcswitcher/jobs/package_phase.py` — `_slice_for` now also slices `snippets`/`unresolved`
- `tests/unit/jobs/test_package_state.py` — `TestSnippetRegistry` (11 tests), `TestUnreproducibleDetection` (6 tests)
- `tests/unit/cli/test_config_sync.py` — every existing private-function call updated to the new `filename`-parameterised signatures; `TestMultiFileSync` (8 new tests)
- `tests/unit/jobs/test_package_review.py` — `TestUnreproducibleGroupResolution` (7 tests), `TestUnresolvedFailsTheJob` (4 tests)
- `tests/unit/jobs/test_package_sync_core.py` — `TestFinalizeUnreproducible` (4 tests)
- `tests/unit/jobs/test_package_phase.py` — one new test in `TestDecisionDistribution` for per-job snippet/unresolved slicing
- `tests/integration/test_config_sync.py` — 4 call sites updated to the new `_get_target_config`/`_copy_config_to_target` signatures (no VM run performed)

## Decisions Made

See `key-decisions` in frontmatter: modifying `package_sync_core.py`/`package_phase.py` outside the plan's declared file scope (Rule 2), the apt-no-candidate detector's distinct method name (avoiding collision with the pre-existing `collect_unavailable_item_ids` hook's different contract), removing `apply()`'s `total==0` early return, `accept_review()`'s snippets/unresolved carry-through fix, and `replay()`'s never-raise contract.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 — missing critical functionality] `package_sync_core.py` and `package_phase.py` modified outside declared `files_modified`**
- **Found during:** Task 2, once `ReviewOutcome.snippets`/`.unresolved` existed and needed a consumer
- **Issue:** The plan's frontmatter `files_modified` lists only `package_review.py` for Task 2, but its own action text explicitly specifies behavior that can only live in `PackageSyncJob.apply()` (writing snippets, failing the job on unresolved items) and `PackagePhaseCoordinator._slice_for` (per-job outcome distribution) — both in files the frontmatter omits.
- **Fix:** Extended `apply()` with `_finalize_unreproducible`/`_unresolved_as_failures`, and `_slice_for` to also slice the two new `ReviewOutcome` fields.
- **Files modified:** `src/pcswitcher/jobs/package_sync_core.py`, `src/pcswitcher/jobs/package_phase.py`, plus their test files
- **Verification:** Full suite green (960 passed); dedicated new tests in `test_package_sync_core.py::TestFinalizeUnreproducible` and `test_package_phase.py::TestDecisionDistribution::test_snippets_and_unresolved_are_also_sliced_per_job`
- **Committed in:** `369a979` (test), `574f502` (feat)

**2. [Rule 3 — blocking, compile-time only] `tests/integration/test_config_sync.py` call sites updated**
- **Found during:** whole-project `basedpyright` run after Task 1
- **Issue:** Four direct calls to `_get_target_config`/`_copy_config_to_target` in the integration test file used the pre-generalisation signature and failed static type-checking against the new `filename`-parameterised signatures.
- **Fix:** Added `"config.yaml"` as the filename argument at each call site — no behavior change, no VM available to actually run these tests.
- **Files modified:** `tests/integration/test_config_sync.py`
- **Verification:** `uv run basedpyright` clean project-wide
- **Committed in:** `29dc7e2` (test)

---

**Total deviations:** 2 auto-fixed (1 missing-critical-functionality expanding the modified-file set, 1 blocking compile-time fix in an out-of-scope integration test file). **Impact on plan:** Both required for the plan's own must_haves and for a green project-wide `basedpyright`; no scope creep beyond what the plan's own action text and acceptance criteria already specified.

## Issues Encountered

None beyond the file-scope deviation documented above.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

`SnippetRegistry`/`UnreproducibleItem`/`UNREPRODUCIBLE_REVIEW_ACTION` complete D-18 through D-23's mechanism; `snap_sync`/`flatpak_sync` (already built in plans 02-07/02-08/02-09's sibling wave) need nothing further from this plan since D-22 excludes them from snippets entirely.

This plan's own `<verification>` section names one VM-level check (a dry-run against the reference machine surfacing the four real no-candidate apt packages and unowned trees) that this autonomous run has no VM access to perform — deferred to plan 02-13's end-to-end suite, the precedent every prior plan in this phase already set.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: src/pcswitcher/jobs/package_state.py
- FOUND: src/pcswitcher/jobs/apt_sync.py
- FOUND: src/pcswitcher/config_sync.py
- FOUND: src/pcswitcher/jobs/package_review.py
- FOUND: src/pcswitcher/jobs/package_sync_core.py
- FOUND: src/pcswitcher/jobs/package_phase.py
- FOUND: tests/unit/jobs/test_package_state.py
- FOUND: tests/unit/cli/test_config_sync.py
- FOUND: tests/unit/jobs/test_package_review.py
- FOUND: tests/unit/jobs/test_package_sync_core.py
- FOUND: tests/unit/jobs/test_package_phase.py
- FOUND: tests/integration/test_config_sync.py
- FOUND: commit 29dc7e2
- FOUND: commit d37726d
- FOUND: commit 369a979
- FOUND: commit 574f502
