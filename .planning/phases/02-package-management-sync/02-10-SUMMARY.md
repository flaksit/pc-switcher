---
phase: 02-package-management-sync
plan: 10
subsystem: infra
tags: [config-schema, orchestrator-ordering, rsync-filters, snap, flatpak, folder_sync]

requires:
  - phase: 02-package-management-sync
    provides: "JobContext.enabled_sync_jobs (02-03), snap_sync_exclude_paths()/SnapSyncJob (02-08), flatpak_sync_exclude_paths()/FlatpakSyncJob (02-09)"
provides:
  - "sync_jobs.snap_sync / sync_jobs.flatpak_sync config keys, shipped disabled, ordered above folder_sync (D-17)"
  - "FolderSyncJob._snap_sync_exclude_filters / _flatpak_sync_exclude_filters — the D-29 translation of snap_sync/flatpak_sync owned absolute paths into GLOBAL-FIRST rsync excludes"
  - "FolderSyncJob._package_job_enabled(job_name) — the single helper both call sites use to read JobContext.enabled_sync_jobs"
affects: [02-11, 02-12, 02-13]

tech-stack:
  added: []
  patterns:
    - "A package job's owned paths reach folder_sync via a one-way export function (snap_sync_exclude_paths/flatpak_sync_exclude_paths), consumed by a folder_sync static method with the exact shape of _vscode_state_exclude_filters — folder_sync gains no knowledge of either ecosystem's layout (ADR-018 precedent, D-29)."
    - "Gating on a sibling job's enablement happens at the call site in _build_rsync_cmd, not inside the filter-builder itself — the builder stays a pure, unconditional translation (matching _vscode_state_exclude_filters's own shape); only the caller decides whether to invoke it, via one shared _package_job_enabled(name) helper reading JobContext.enabled_sync_jobs."

key-files:
  created: []
  modified:
    - src/pcswitcher/schemas/config-schema.yaml
    - src/pcswitcher/default-config.yaml
    - src/pcswitcher/jobs/folder_sync.py
    - src/pcswitcher/home.filter
    - tests/unit/orchestrator/test_config_system.py
    - tests/unit/orchestrator/test_first_sync_scope.py
    - tests/unit/jobs/test_folder_sync.py

key-decisions:
  - "The two new exclusion methods are unconditional, pure translations (like _vscode_state_exclude_filters); the enable/disable gate lives entirely in _build_rsync_cmd via one _package_job_enabled() helper, not inside the methods themselves — keeps the asymmetry with VS Code's unconditional exclusion legible at a single call site instead of duplicated inside two method bodies."
  - "home.filter carried no flatpak or snap rule already (verified by reading the file, not assumed from the CONTEXT note) — nothing to retire; only the explanatory comment was added, on its own line per the file's own trailing-comment warning."

patterns-established: []

requirements-completed: []

coverage:
  - id: D1
    description: "snap_sync and flatpak_sync are valid sync_jobs config keys (schema + shipped default), both ship disabled, and all three package jobs (apt_sync, snap_sync, flatpak_sync) precede folder_sync in sync_jobs order, which the orchestrator iterates directly for both job discovery and _first_sync_scopes (D-17)"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_config_system.py::TestShippedDefaultConfig::test_snap_and_flatpak_sync_ship_disabled"
        status: pass
      - kind: unit
        ref: "tests/unit/orchestrator/test_config_system.py::TestShippedDefaultConfig::test_package_jobs_precede_folder_sync"
        status: pass
      - kind: unit
        ref: "tests/unit/orchestrator/test_config_system.py::TestJobEnableDisable::test_core_edge_unknown_job_in_config"
        status: pass
      - kind: unit
        ref: "tests/unit/orchestrator/test_first_sync_scope.py::TestFirstSyncScopesPackageJobsOrdering::test_all_package_jobs_and_folder_sync_each_contribute_one_scope_in_order"
        status: pass
    human_judgment: false
  - id: D2
    description: "Enabling snap_sync excludes each ~/snap/<app>/<revision> directory (never common or current) from folder_sync's rsync command; enabling flatpak_sync excludes ~/.local/share/flatpak (never ~/.var/app); disabling either omits its exclusion entirely; both exclusions precede the folder's merge filter (GLOBAL-FIRST); a JobContext without enabled_sync_jobs emits neither and does not raise"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestSnapSyncExcludeFilters::test_revision_dir_included_common_and_current_excluded"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestFlatpakSyncExcludeFilters::test_flatpak_data_dir_included_var_app_never_mentioned"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestPackageJobExcludeFiltersGating::test_snap_sync_enabled_includes_revision_exclusion"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestPackageJobExcludeFiltersGating::test_snap_sync_disabled_excludes_nothing"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestPackageJobExcludeFiltersGating::test_flatpak_sync_enabled_includes_data_dir_exclusion_not_var_app"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestPackageJobExcludeFiltersGating::test_flatpak_sync_disabled_excludes_nothing"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestPackageJobExcludeFiltersGating::test_both_package_exclusions_precede_merge_filter"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_folder_sync.py::TestPackageJobExcludeFiltersGating::test_missing_enabled_sync_jobs_omits_both_exclusions_without_raising"
        status: pass
      - kind: other
        ref: "grep -v '^#' src/pcswitcher/home.filter | grep -c 'flatpak\\|snap'"
        status: pass
      - kind: other
        ref: "grep -c 'enabled_sync_jobs' src/pcswitcher/jobs/folder_sync.py"
        status: pass
    human_judgment: false
  - id: D3
    description: "A live-machine dry-run with all three package jobs enabled shows no ~/.local/share/flatpak or ~/snap/<app>/<revision> entries among folder_sync's transfers, and still shows ~/.var/app"
    verification: []
    human_judgment: true
    rationale: "The plan's own <verification> section names this as a live/VM-level dry-run check; this autonomous run has no VM access. Every mocked-executor behavior bullet is unit-covered above (D1-D2), matching the precedent plans 02-03/02-05/02-06/02-07/02-08/02-09 set for their own live-machine proofs — deferred to plan 02-13's end-to-end suite."

duration: 13min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 10: Package Job Config Registration and folder_sync Exclusion Wiring Summary

**snap_sync and flatpak_sync are now enable-able config keys ordered ahead of folder_sync (D-17), and enabling either automatically and non-overridably excludes the paths it owns from folder_sync's rsync mirror (D-29), replacing what would otherwise be hand-written filter rules.**

## Performance

- **Duration:** 13 min
- **Started:** 2026-07-23T13:47:33Z (approx., previous plan's completion commit)
- **Completed:** 2026-07-23T13:59:27Z
- **Tasks:** 2
- **Files modified:** 7 (4 source, 3 test)

## Accomplishments

- `config-schema.yaml` / `default-config.yaml`: `snap_sync` and `flatpak_sync` are now valid `sync_jobs` keys (both boolean, default `false`, `additionalProperties: false` still rejects unknown keys) with matching empty top-level job-config sections mirroring `apt_sync`'s. Both ship disabled and are listed above `folder_sync` in `default-config.yaml`, alongside a comment stating the ordering rule explicitly — reordering these entries changes execution order, since the orchestrator resolves `sync_jobs` in file order for both `_discover_and_validate_jobs` and `_first_sync_scopes`.
- `folder_sync.py`: two new static methods, `_snap_sync_exclude_filters` and `_flatpak_sync_exclude_filters`, each an unconditional, pure translation of the owning job's exported absolute paths into GLOBAL-FIRST rsync excludes — the exact shape of the existing `_vscode_state_exclude_filters`. A new `_package_job_enabled(job_name)` helper reads `JobContext.enabled_sync_jobs` (never `self.context.config`, which is folder_sync's own section and has never carried sibling state); `_build_rsync_cmd` calls both filter methods only when the corresponding helper call returns `True`. `None` (no sibling enablement info — the shape existing lightweight test contexts use) reproduces today's behavior of excluding nothing extra.
- The module's GLOBAL-FIRST comment block now documents all five non-overridable exclude groups and names the asymmetry explicitly: the VS Code exclusion is unconditional because `vscode_state_sync` merges those DBs and must always hide them, while the snap/flatpak exclusions are conditional because a disabled job means nobody is managing those paths — excluding them anyway would strand that data unmirrored.
- `home.filter`: verified (not assumed) to carry no flatpak or snap rule already, so nothing was retired; added a comment, on its own line per the file's own trailing-comment warning, stating that enabling `snap_sync`/`flatpak_sync` supplies the corresponding exclusion automatically, so a personal filter file's hand-written equivalent can be dropped.
- 12 new unit tests: 4 in `test_config_system.py`/`test_first_sync_scope.py` pinning schema registration and job/scope ordering, 8 in `test_folder_sync.py` covering both filter-builder methods in isolation and the enable/disable gating (including the `enabled_sync_jobs=None` no-raise case and exclusion-precedes-merge-filter ordering).

## Task Commits

Each task followed the TDD RED → GREEN cycle:

1. **Task 1: Register snap_sync and flatpak_sync, order package jobs first**
   - `test(02-10): add failing tests for snap_sync/flatpak_sync config registration` - `1fc6468`
   - `feat(02-10): register snap_sync and flatpak_sync, order package jobs before folder_sync` - `31a0ad1`
2. **Task 2: Hand package jobs' owned paths to folder_sync**
   - `test(02-10): add failing tests for package-job exclusions in folder_sync` - `a86c4c3`
   - `feat(02-10): hand snap/flatpak owned paths to folder_sync, retire filter comment (D-29)` - `b999b8b`

**Plan metadata:** (this commit)

## Files Created/Modified

- `src/pcswitcher/schemas/config-schema.yaml` - `snap_sync`/`flatpak_sync` sync_jobs properties + top-level job-config sections
- `src/pcswitcher/default-config.yaml` - both shipped disabled, ordered above `folder_sync`, ordering rule spelled out in comment
- `src/pcswitcher/jobs/folder_sync.py` - `_snap_sync_exclude_filters`, `_flatpak_sync_exclude_filters`, `_package_job_enabled`, wired into `_build_rsync_cmd`
- `src/pcswitcher/home.filter` - retirement comment only (no prior flatpak/snap rule existed to remove)
- `tests/unit/orchestrator/test_config_system.py` - schema/ordering tests for the shipped default config
- `tests/unit/orchestrator/test_first_sync_scope.py` - `_first_sync_scopes()` ordering test across all four jobs (also: incidental `ruff format` fix to a long line introduced by this plan's own Task 1 test commit)
- `tests/unit/jobs/test_folder_sync.py` - `make_context` extended with `enabled_sync_jobs`; new filter-builder and gating test classes

## Decisions Made

See `key-decisions` in frontmatter.

1. **Gating lives at the call site, not inside the filter-builder methods.** `_snap_sync_exclude_filters`/`_flatpak_sync_exclude_filters` are unconditional pure translations, matching `_vscode_state_exclude_filters`'s shape exactly; `_build_rsync_cmd` decides whether to call them via one shared `_package_job_enabled()` helper. This keeps the VS-Code-vs-package-job asymmetry visible at a single point (the two `if` guards in `_build_rsync_cmd`) instead of duplicated inside two method bodies.
2. **`home.filter` retirement was verified, not assumed.** The plan's own orchestrator directive flagged this as needing verification: the shipped file was read directly and confirmed to carry no flatpak/snap rule (those live only in the user's personal, unshipped `~/.config/pc-switcher/home-janfr.filter`). Only the explanatory comment was added.

## Deviations from Plan

None — plan executed as written. The one incidental fix (a `ruff format` line-length correction to a test file this plan's own Task 1 commit introduced) is a formatting-only change, not a deviation from behavior.

## Issues Encountered

An early version of `TestPackageJobExcludeFiltersGating._build_cmd` used a literal `"/home"` transfer-root path while patching `Path.home()` to a `tmp_path`-based fixture directory — the two were unrelated ancestors, so `Path.relative_to()` silently produced no match and every gating assertion failed with the exclusion missing. Fixed during the RED phase (before implementing Task 2's production code) by using `str(tmp_path)` as the transfer root so it is a real ancestor of the fixture `home` directory, matching the pattern the isolated `TestSnapSyncExcludeFilters`/`TestFlatpakSyncExcludeFilters` classes already used correctly.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Both `snap_sync` and `flatpak_sync` are now full participants in the orchestrator's job discovery, ordering, and first-sync-scope messaging, and folder_sync automatically stops mirroring what either job manages the moment it is enabled — closing the loop `02-08`/`02-09` opened when they exported their owned paths. `.planning/REQUIREMENTS.md` was left untouched per this plan's orchestrator directive — two plans remain in Phase 2 (`02-11`, `02-12`) before requirement completion is marked.

The plan's own `<verification>` section names one live-machine check (a real dry-run against a genuinely diverged machine, confirming the exclusion/inclusion split holds against real snapd/flatpak state) that this autonomous run has no VM access to perform — deferred to plan `02-13`'s end-to-end suite, the same precedent plans `02-03`/`02-05`/`02-06`/`02-07`/`02-08`/`02-09` set for their own live-machine proofs.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: src/pcswitcher/schemas/config-schema.yaml
- FOUND: src/pcswitcher/default-config.yaml
- FOUND: src/pcswitcher/jobs/folder_sync.py
- FOUND: src/pcswitcher/home.filter
- FOUND: tests/unit/jobs/test_folder_sync.py
- FOUND: commit 1fc6468
- FOUND: commit 31a0ad1
- FOUND: commit a86c4c3
- FOUND: commit b999b8b
