---
phase: 02-package-management-sync
plan: 20
subsystem: docs
tags: [documentation, configuration, living-specs, package-sync, adr-020]

requires:
  - phase: 02-19
    provides: relocated jobs/packages/ module paths and the manual_installs_sync job the docs must reference
provides:
  - docs/jobs/ per-job behaviour documents (package-sync, folder-sync, vscode-state-sync)
  - configuration.md restricted to configuration only (D-33)
  - docs/system/ living specs corrected to the per-manager, four-job, self-pushed-snippet design
affects: [package-sync, documentation, 02-21]

tech-stack:
  added: []
  patterns:
    - "D-33 documentation layout: configuration.md/default-config.yaml explain configuration; per-job behaviour lives in docs/jobs/"

key-files:
  created:
    - docs/jobs/package-sync.md
    - docs/jobs/folder-sync.md
    - docs/jobs/vscode-state-sync.md
  modified:
    - docs/configuration.md
    - docs/README.md
    - docs/system/architecture.md
    - docs/system/core.md
    - docs/system/data-model.md

key-decisions:
  - "Per-job docs live in a new docs/jobs/ directory (no prior per-job location existed); linked from docs/README.md under a new 'Job behaviour' group, not from docs/system/_index.md (those are user-facing, not living specs)."
  - "The four package jobs are documented together in one docs/jobs/package-sync.md, matching their shared item -> diff -> review -> converge model (D-33)."

patterns-established:
  - "Configuration reference names config keys only and links out to the job's document for behaviour."

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "Job behaviour split out of configuration.md into per-job docs (package-sync, folder-sync, vscode-state-sync); configuration.md restricted to config keys"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: automated
        ref: "test -e docs/jobs/package-sync.md && grep manual_installs_sync && ! grep coordinator/mandatory/SYNCED_CONFIG_FILENAMES => DOCS_SPLIT_OK"
        status: pass
    human_judgment: false
  - id: D2
    description: "docs/system living specs corrected to the per-manager, four-job, self-pushed-snippet design against jobs/packages/ paths; no coordinator or stale module path survives"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: automated
        ref: "! grep -rni PackagePhaseCoordinator|SYNCED_CONFIG_FILENAMES docs/system && ! grep jobs/package_sync_core|package_items|package_review|package_state && grep manual_installs_sync architecture.md => SPECS_OK"
        status: pass
    human_judgment: false

# Metrics
duration: 8min
completed: 2026-07-23
status: complete
---

# Phase 02 Plan 20: Documentation delta — per-job docs and corrected living specs Summary

**Migrated job behaviour out of the configuration reference into per-job `docs/jobs/` documents (D-33) and purged every trace of the superseded PackagePhaseCoordinator, one-cross-manager-review, mandatory-registration and config_sync-snippet-transport claims from the docs and living specs.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-07-23T20:04:34Z
- **Completed:** 2026-07-23T20:12:13Z
- **Tasks:** 2
- **Files modified:** 8 (3 created, 5 modified)

## Accomplishments

- Created `docs/jobs/package-sync.md` documenting all four package jobs (including `manual_installs_sync`), the per-manager batched review, apt collateral auto/manual classification, machine-specific packages, install snippets with self-pushed transport, skip-once-is-valid resolution, and versions.
- Created `docs/jobs/folder-sync.md` and `docs/jobs/vscode-state-sync.md`, moving the behavioural prose (filter-rule mechanics, `authorized_keys` guidance, always-excluded groups, VS Code selective merge) out of `configuration.md`.
- Restricted `configuration.md` to configuration only: job sections now name config keys and link out to the job document; the package jobs reduce to their `sync_jobs` enable flags with no per-job config sections (D-32).
- Corrected the `docs/system/` living specs: removed `PackagePhaseCoordinator` and its Mermaid subgraph from `architecture.md`, described each job's own plan/review/apply pipeline, repointed module paths to `jobs/packages/*`, and rewrote the snippet-registry entry in `data-model.md` to the `manual_installs_sync` `send_file()` push.

## Task Commits

Each task was committed atomically:

1. **Task 1: Split job behaviour out of configuration.md into per-job docs** - `4ecb317` (docs)
2. **Task 2: Correct the docs/system living specs** - `e57bd89` (docs)

## Files Created/Modified

- `docs/jobs/package-sync.md` (created) - Combined behavioural doc for the four package jobs.
- `docs/jobs/folder-sync.md` (created) - `folder_sync` filter-rule semantics, `authorized_keys`, always-excluded paths.
- `docs/jobs/vscode-state-sync.md` (created) - `vscode_state_sync` selective SQLite-aware merge.
- `docs/configuration.md` (modified) - Trimmed to configuration only; job sections link out to job docs; all four package enable flags listed under `sync_jobs`.
- `docs/README.md` (modified) - Added a "Job behaviour (`jobs/`)" group linking the three new docs.
- `docs/system/architecture.md` (modified) - Rewrote the Package Sync Subsystem section, replaced the coordinator subgraph with a per-job pipeline diagram, fixed the step-9 sync-sequence line.
- `docs/system/core.md` (modified) - Repointed to `jobs/packages/sync_core.py`, dropped the coordinator-supplied-plan assertion, named all four jobs, added a `manual_installs_sync` subsection.
- `docs/system/data-model.md` (modified) - Snippet registry pushed by `manual_installs_sync` via `send_file()`; removed `SYNCED_CONFIG_FILENAMES`; named the fourth job.

## Decisions Made

- Per-job docs live in a new `docs/jobs/` directory. No prior per-job doc location existed (confirmed against `docs/README.md` and `docs/system/_index.md`), so `docs/jobs/` was created as the plan's `files_modified` anticipated. They are linked from `docs/README.md` under a new "Job behaviour" group and deliberately not from `docs/system/_index.md`, which indexes only the `system/` golden-copy specs.
- The four package jobs are documented together in one `docs/jobs/package-sync.md`, matching their shared item -> diff -> review -> converge model (D-33).

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

One legitimate corrected-negation sentence in `docs/jobs/package-sync.md` used the word "coordinator" ("no coordinator sits between the jobs"), which the acceptance criterion `grep -ci 'coordinator' … returns 0` counts regardless of negation. Reworded to "nothing sits between the jobs merging their reviews into one" before committing, so the grep gate is clean while the corrected meaning is preserved.

## Known Stubs

None. Documentation-only plan; no code stubs introduced.

## Verification

- Task 1 automated verify: `DOCS_SPLIT_OK` (package-sync.md exists, names `manual_installs_sync`, no coordinator/mandatory/config-transport claims in package-sync.md or configuration.md).
- Task 2 automated verify: `SPECS_OK` (no `PackagePhaseCoordinator`/`SYNCED_CONFIG_FILENAMES` in `docs/system/`, no old `jobs/package_*` module paths, `manual_installs_sync` present in `architecture.md`).
- Whole-tree residual grep for `PackagePhaseCoordinator|SYNCED_CONFIG_FILENAMES|jobs/package_sync_core|one review covering every` outside `adr-020`: CLEAN.
- Markdown rules honoured in all created/edited docs: no `---` separators, no bold-as-heading introduced, Mermaid used for the pipeline diagram.

## Self-Check: PASSED

All 9 created/modified files present on disk; both task commits (`4ecb317`, `e57bd89`) found in git history.
