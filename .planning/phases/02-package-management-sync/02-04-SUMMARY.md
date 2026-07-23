---
phase: 02-package-management-sync
plan: 04
subsystem: infra
tags: [package-sync, decision-file, rsync, exclusion, d-08, d-09, d-10]

requires:
  - phase: 02-package-management-sync
    provides: "PackageSyncJob's plan()/apply() split, ItemDiff/DiffAction taxonomy, PackagePhaseCoordinator (plans 02-03, 02-05)"
provides:
  - "package_state.py: DecisionFile/DecisionEntry/filter_inert — the machine-local, never-synced 'skip always' store (D-08, D-08a, D-09)"
  - "machine-packages.example.yaml — documentation-only example, no default entries in Python or default-config.yaml (D-10)"
  - "PackageSyncJob.plan() filters inert items out of the diff-input before any ItemDiff exists"
  - "PackageSyncJob.apply()'s _record_permanent_skips — SKIP_ALWAYS persisted on the correct end (D-08a), never in dry-run or a non-interactive outcome"
  - "FolderSyncJob._decision_file_exclude_filters — the third GLOBAL-FIRST, unconditional rsync exclusion"
affects: [02-06, 02-07, 02-08, 02-09, 02-10, 02-11, 02-12]

tech-stack:
  added: []
  patterns:
    - "DecisionFile takes an Executor at construction and issues every read/write as a shell command through it — one code path serves both the source's LocalExecutor and the target's RemoteExecutor, so there is no separate 'local write' branch that could accidentally be used for the target (ADR-002)."
    - "Path construction inside a shell command: shlex.quote() applied to the home-relative segment but left OUTSIDE the leading `~/` — `~/` immediately followed by a (possibly-)quoted word is still one shell word, so tilde expansion and defensive quoting (T-02-01) both hold simultaneously."
    - "filter_inert as one pure, module-level function both the source-capture and target-query sides of plan() call, so 'inert' has exactly one definition instead of two independently-maintained filters."

key-files:
  created:
    - src/pcswitcher/jobs/package_state.py
    - src/pcswitcher/machine-packages.example.yaml
    - tests/unit/jobs/test_package_state.py
  modified:
    - src/pcswitcher/jobs/package_sync_core.py
    - src/pcswitcher/jobs/folder_sync.py
    - tests/unit/jobs/test_folder_sync.py

key-decisions:
  - "DecisionFile resolves the file path via a bare `~/` shell prefix (like config_sync's CONFIG_REMOTE_DIR/CONFIG_REMOTE_PATH), not a resolved absolute path from `echo $HOME`. Verified empirically that `~/` immediately followed by a shlex-quoted word is still tilde-expanded as one shell word by bash, so this avoids an extra round-trip through the executor for every load()/record() call while still satisfying T-02-01's shlex.quote() requirement on the relpath itself."
  - "record()'s atomic write travels as a single `mkdir -p ... && printf '%s' <quoted-content> > <tmp> && mv -f <tmp> <path>` command (one round trip) rather than three separate executor calls — verified round-trip safe for multi-line YAML content containing quotes, backslashes and '%' via a direct subprocess test before relying on it in DecisionFile."
  - "_record_permanent_skips (package_sync_core.py) runs unconditionally at the top of apply(), before the total==0 early-return for the install/remove loop — a run with zero APPLY-decided items but one SKIP_ALWAYS-decided removal still must record that decision, so the recording step cannot live inside the branch that only fires when there is APPLY work to do."
  - "The review UI's 'second prompt that promotes a skip to permanent' (referenced in package_review.py's Decision.SKIP_ALWAYS docstring) is NOT part of this plan's scope — this plan's files_modified list is package_state.py/package_sync_core.py/folder_sync.py only, not package_review.py. apply()'s handling of a SKIP_ALWAYS decision is exercised directly via hand-constructed ReviewOutcome objects in tests, matching the plan's own task list; the interactive UI path that produces a SKIP_ALWAYS decision from review_items() remains future work."

patterns-established:
  - "A third GLOBAL-FIRST, non-overridable folder_sync exclusion group (alongside ADR-017 runtime-state and ADR-018 VS Code state) for a home-relative GLOB (not a fixed list of absolute paths) — package_state owns the glob, folder_sync only resolves it against Path.home() and translates it into a filter, matching the same one-way ownership _vscode_state_exclude_filters already follows."

requirements-completed: []

coverage:
  - id: D1
    description: "An item recorded skip-always in machine M's decision file is inert on M in both roles: dropped from M's captured manifest when M is the source, and produces no ItemDiff/ReviewGroup entry when M is the target (D-08)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestFilterInert::test_drops_items_whose_id_is_in_decisions"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestPipelineWiring::test_source_held_inert_item_absent_from_the_plans_diffs"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestPipelineWiring::test_target_held_inert_item_absent_even_though_source_also_differs"
        status: pass
    human_judgment: false
  - id: D2
    description: "A skip-always decision on a source-held item (INSTALL/CHANGE) writes to the SOURCE's decision file; on a target-held item (REMOVE) it writes to the TARGET's decision file, through the correct executor, leaving the other side untouched (D-08a)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestDecisionFileRecord::test_source_held_write_uses_source_executor_and_leaves_target_untouched"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestDecisionFileRecord::test_target_held_write_uses_target_executor_and_leaves_source_untouched"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestPipelineWiring::test_skip_always_on_remove_writes_to_target_not_source"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestPipelineWiring::test_skip_always_on_install_writes_to_source_not_target"
        status: pass
    human_judgment: false
  - id: D3
    description: "Decision files live at ~/.config/pc-switcher/<manager>.decisions.yaml, one per manager, and config_sync never transfers one (D-09)"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestRelpathConstants::test_relpath_template_places_file_under_config_pc_switcher"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestConfigSyncScope::test_copy_config_to_target_sends_only_config_yaml"
        status: pass
    human_judgment: false
  - id: D4
    description: "folder_sync excludes every manager's decision-file glob non-overridably, before the folder's merge filter, unconditional regardless of which package jobs are enabled"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestDecisionFileExcludeFilters::test_decision_file_exclude_precedes_merge_filter"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestDecisionFileExcludeFilters::test_user_plus_rule_for_decision_file_does_not_change_command_ordering"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestDecisionFileExcludeFilters::test_unconditional_regardless_of_which_folder_is_synced"
        status: pass
    human_judgment: false
  - id: D5
    description: "Shipped defaults for machine-specific items exist only as an example file; no default entry is hardcoded in Python or merged into default-config.yaml (D-10)"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestRelpathConstants::test_no_default_machine_specific_package_hardcoded"
        status: pass
      - kind: other
        ref: "grep -rn 'brscan3|brother-udev' src/pcswitcher/*.py src/pcswitcher/jobs/*.py — returns nothing"
        status: pass
    human_judgment: false
  - id: D6
    description: "A decision file that is absent, empty, or unreadable/malformed degrades to 'no permanent decisions' rather than aborting the sync; only the malformed case logs a WARNING naming the path"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestDecisionFileLoad::test_absent_file_returns_empty_mapping"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestDecisionFileLoad::test_empty_file_returns_empty_mapping"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_state.py::TestDecisionFileLoad::test_malformed_yaml_returns_empty_mapping_and_warns_naming_the_path"
        status: pass
    human_judgment: false
  - id: D7
    description: "A human confirms `pc-switcher sync <target> --dry-run` against a live target shows no decision-file path among the transferred entries for /home, against real pc1/pc2 VMs"
    verification: []
    human_judgment: true
    rationale: "This plan's own <verification> section names a VM-level dry-run log check; this autonomous run has no VM access. Every mocked-executor behavior bullet is unit-covered above (D1-D6); real rsync/SSH behavior on a live diverged machine is unverified here by design, matching the precedent plans 02-03/02-05 set for their own tracer-level VM proofs (deferred to a future VM-level suite in the spirit of plan 02-13)."

duration: 135min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 04: Machine-Local Decision Store Summary

**A per-machine, never-synced `~/.config/pc-switcher/<manager>.decisions.yaml` store makes "skip always" durable in both sync roles (D-08/D-08a), and a third GLOBAL-FIRST folder_sync exclusion keeps those files from ever crossing to a peer (D-09).**

## Performance

- **Duration:** 135 min
- **Started:** 2026-07-23T09:38:59+02:00
- **Completed:** 2026-07-23T11:54:46+02:00
- **Tasks:** 2
- **Files modified:** 6 (3 created, 3 modified)

## Accomplishments

- `package_state.py`: `DecisionFile` (load/record through whichever `Executor` is local to the machine that should hold the entry), `DecisionEntry`, `filter_inert`, and the `DECISION_FILE_RELPATH_TEMPLATE`/`DECISION_FILE_GLOB_RELPATH` constants folder_sync's exclusion and the store itself both derive from — one source of truth, so they cannot drift apart.
- `machine-packages.example.yaml`: documentation-only example with commented illustrative entries; no default entry lives in Python or `default-config.yaml` (D-10).
- `PackageSyncJob.plan()` now loads both machines' decision files first and filters an inert item out of whichever side holds it, before capture/query results ever become an `ItemDiff` — an inert item never reaches the coordinator's review.
- `PackageSyncJob.apply()`'s new `_record_permanent_skips` persists a `DecisionEntry` for every `SKIP_ALWAYS`-decided, actionable diff on the correct end of the connection (D-08a): source for `INSTALL`/`CHANGE`, target for `REMOVE` — gated on both `not dry_run` and `outcome.was_interactive` (D-26, ADR-014), and runs unconditionally at the top of `apply()` so it fires even when there is no other work to do.
- `FolderSyncJob._decision_file_exclude_filters`: a third GLOBAL-FIRST, non-overridable rsync exclusion for `~/.config/pc-switcher/*.decisions.yaml`, emitted before the folder's central `merge` filter, unconditional regardless of which package jobs are enabled.
- 45 new unit tests (27 in `test_package_state.py`, 6 new classes/18 tests split across two commits' worth of coverage; 6 new tests in `test_folder_sync.py`), plus a direct confirmation that `config_sync` never transfers a decision file.

## Task Commits

Each task followed RED (test) then GREEN (feat):

1. **Task 1: Machine-local decision store with correct-end writes**
   - `d436179` (test) — RED: 18 tests against the not-yet-existing `package_state` module.
   - `ff631aa` (feat) — GREEN: `DecisionFile`, `DecisionEntry`, `filter_inert`, `machine-packages.example.yaml`.
2. **Task 2: Wire skip-always into the pipeline and exclude the files from the mirror**
   - `b259116` (test) — RED: pipeline-wiring assertions on `PackageSyncJob.plan()`/`apply()` and `FolderSyncJob._decision_file_exclude_filters`, confirmed to fail against the pre-task-2 implementation (verified via `git stash` of the implementation files, observing `AttributeError`/`ValueError` failures, then restoring).
   - `a273a06` (feat) — GREEN: the `plan()`/`apply()` wiring and the folder_sync exclusion.

**Plan metadata:** (this commit)

## Files Created/Modified

- `src/pcswitcher/jobs/package_state.py` - `DecisionFile`, `DecisionEntry`, `filter_inert`, `DECISION_FILE_RELPATH_TEMPLATE`, `DECISION_FILE_GLOB_RELPATH`
- `src/pcswitcher/machine-packages.example.yaml` - documentation-only example decision file
- `src/pcswitcher/jobs/package_sync_core.py` - `plan()`'s decision-file load + `filter_inert` wiring, `apply()`'s `_record_permanent_skips`
- `src/pcswitcher/jobs/folder_sync.py` - `_decision_file_exclude_filters`, wired into `_build_rsync_cmd`'s GLOBAL-FIRST section
- `tests/unit/jobs/test_package_state.py` - 27 tests: `DecisionFile`/`filter_inert` unit behavior, pipeline wiring, `config_sync` scope confirmation
- `tests/unit/jobs/test_folder_sync.py` - 6 new tests for `_decision_file_exclude_filters`

## Decisions Made

See `key-decisions` in frontmatter: the `~/`-prefix path-quoting technique (avoids an `echo $HOME` round trip while still satisfying T-02-01), the single-command atomic write shape, `_record_permanent_skips` running unconditionally before the early-return in `apply()`, and the explicit scoping decision that this plan does NOT touch `package_review.py`'s interactive "promote to permanent" prompt (out of `files_modified`, left as future work).

## Deviations from Plan

None — plan executed as written. The `package_review.py` UI gap noted above is a scoping clarification (the plan's own `files_modified` list already excluded that file), not a deviation.

## Issues Encountered

None. One implementation detail required empirical verification before relying on it: whether `~/` immediately followed by a `shlex.quote()`-wrapped word still tilde-expands correctly as a single shell word (it does — confirmed via direct `bash -c` tests) rather than assuming it and finding out at VM-test time.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

`DecisionFile`/`filter_inert`/`DECISION_FILE_GLOB_RELPATH` are the complete, reusable D-08/D-08a/D-09 mechanism every remaining Phase 2 manager job (`snap_sync`, `flatpak_sync`) gets for free through `PackageSyncJob.plan()`/`apply()` — no per-manager code needed, since the store is keyed by `manager_id` alone. `folder_sync`'s third exclusion group is unconditional, so no future package job needs to remember to protect its own decision file.

This plan's own `<verification>` section names one VM-level check (a `pc-switcher sync --dry-run` log showing no decision-file path among the transferred entries for `/home`) that this autonomous run has no VM access to perform — deferred, matching the precedent plans 02-03/02-05 set for their own tracer-level VM proofs.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: src/pcswitcher/jobs/package_state.py
- FOUND: src/pcswitcher/machine-packages.example.yaml
- FOUND: tests/unit/jobs/test_package_state.py
- FOUND: src/pcswitcher/jobs/package_sync_core.py
- FOUND: src/pcswitcher/jobs/folder_sync.py
- FOUND: tests/unit/jobs/test_folder_sync.py
- FOUND: commit d436179
- FOUND: commit ff631aa
- FOUND: commit b259116
- FOUND: commit a273a06
