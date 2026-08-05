---
phase: 02-package-management-sync
plan: 19
subsystem: infra
tags: [refactor, module-layout, jobs, config-schema, packages]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: the final package-sync module tree (items/review/state/sync_core helpers, four job modules) and per-job config schema
provides:
  - "jobs/packages/ Python package holding the shared helpers (items, review, state, sync_core) with the package_ prefix stripped"
  - "every importer in src/ and tests/ repointed to jobs.packages.* with no dangling old-path reference"
  - "default-config.yaml and config-schema.yaml free of empty apt_sync/snap_sync/flatpak_sync placeholder sections"
affects: [package-sync, config, future-package-jobs, docs]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Shared job helpers live in a jobs/<domain>/ subpackage; job modules stay flat in jobs/ because discovery maps a sync_jobs key to jobs/<name>.py"
    - "A job earns a top-level config section only when it has a real key (D-32); an absent section resolves to {} via get_job_config"

key-files:
  created:
    - src/pcswitcher/jobs/packages/__init__.py
    - src/pcswitcher/jobs/packages/items.py
    - src/pcswitcher/jobs/packages/review.py
    - src/pcswitcher/jobs/packages/state.py
    - src/pcswitcher/jobs/packages/sync_core.py
  modified:
    - src/pcswitcher/default-config.yaml
    - src/pcswitcher/schemas/config-schema.yaml
    - src/pcswitcher/orchestrator.py
    - src/pcswitcher/jobs/context.py
    - tests/unit/orchestrator/test_config_system.py

key-decisions:
  - "Kept test filenames (test_package_*.py) and only fixed their imports, per the plan's uniform choice — avoids churn on the from tests.unit.jobs.test_package_sync_core import FakeReviewer reference"
  - "test_package_state.py's module-level `from pcswitcher.jobs import package_state` became `from pcswitcher.jobs.packages import state as package_state`, preserving the package_state.__file__ usage with a minimal alias"
  - "context.py and the integration test test_package_sync.py were updated despite being outside files_modified — leaving their imports dangling would break discovery and the full suite (phase-guard mandate; Rule 3)"

patterns-established:
  - "jobs/packages/ subpackage for shared package-sync helpers; job modules remain discoverable in jobs/"
  - "No empty placeholder config sections — enable flag in sync_jobs is a job's only config presence until it has a real key"

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "Shared helpers relocated to jobs/packages/{items,review,state,sync_core}.py with prefix stripped; old jobs/package_*.py files gone; every importer repointed"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "uv run pytest tests/unit/jobs/ -x (446 passed) + grep gates (no jobs.package_* survive) + python -c 'import pcswitcher.orchestrator'"
        status: pass
    human_judgment: false
  - id: D2
    description: "Empty apt_sync/snap_sync/flatpak_sync config sections removed from default-config.yaml and config-schema.yaml; sync_jobs enable flags retained; section-omitting config still validates and resolves to {}"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_config_system.py::TestShippedDefaultConfig::test_config_omitting_package_sections_validates + ::test_shipped_config_omits_empty_package_sections"
        status: pass
    human_judgment: false

# Metrics
duration: 10min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 19: Package Helper Relocation and Empty-Config Cleanup Summary

**Moved the four package-sync helpers into a `jobs/packages/` subpackage (prefix stripped) and deleted the empty apt/snap/flatpak placeholder config sections — a pure D-31/D-32 layout tidy-up with no behaviour change.**

## Performance

- **Duration:** ~10 min
- **Started:** 2026-07-23T19:52Z
- **Completed:** 2026-07-23T20:01Z
- **Tasks:** 2
- **Files modified:** 26 (5 created incl. moved helpers, 21 modified)

## Accomplishments
- `git mv`'d `package_{items,review,state,sync_core}.py` into `jobs/packages/{items,review,state,sync_core}.py` with git history preserved, and added `jobs/packages/__init__.py`.
- Repointed every importer across `src/` and `tests/` (imports, logger names, `patch()` targets, and prose module references) to `jobs.packages.*`; no old path survives the grep gate.
- Left the four job modules (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync`) plus `base.py`/`context.py` in `jobs/` so job discovery still resolves `sync_jobs.<name>` to `jobs/<name>.py`.
- Removed the empty `apt_sync: {}`/`snap_sync: {}`/`flatpak_sync: {}` sections (and their banner comments) from `default-config.yaml`, and their empty top-level object properties from `config-schema.yaml`; the `sync_jobs` enable flags for all four package jobs stay.
- Added tests proving a config that omits every package-job section validates and each job resolves to `{}`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Move the four helpers into jobs/packages/ and update every import** - `a0fd807` (refactor)
2. **Task 2: Delete the empty per-job config sections** - `2a3639d` (refactor)

## Files Created/Modified
- `src/pcswitcher/jobs/packages/__init__.py` - new subpackage docstring/init
- `src/pcswitcher/jobs/packages/{items,review,state,sync_core}.py` - relocated helpers (from `jobs/package_*.py`)
- `src/pcswitcher/jobs/{apt,snap,flatpak,manual_installs}_sync.py`, `folder_sync.py`, `context.py`, `orchestrator.py`, `config_sync.py` - imports/prose repointed to `jobs.packages.*`
- `tests/unit/jobs/test_*.py`, `tests/integration/jobs/test_package_sync.py` - imports/patch targets repointed
- `src/pcswitcher/default-config.yaml` - three empty package sections removed
- `src/pcswitcher/schemas/config-schema.yaml` - three empty top-level object properties removed; note added explaining the absence
- `tests/unit/orchestrator/test_config_system.py` - two tests added for section-omitting validation

## Decisions Made
- Kept test filenames and fixed only their imports (plan's uniform choice), so `from tests.unit.jobs.test_package_sync_core import FakeReviewer` stays valid and doesn't trip the `jobs.package_*` grep gate.
- Verified `config.py`'s `get_job_config` already defaults an absent section to `{}` (line 218) and `job_configs` only collects present top-level dicts, per the plan's key_link, before deleting the schema sections.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Repointed imports in two files outside `files_modified`**
- **Found during:** Task 1 (import survey via grep)
- **Issue:** `src/pcswitcher/jobs/context.py` (`from pcswitcher.jobs.package_review import Reviewer`, TYPE_CHECKING) and `tests/integration/jobs/test_package_sync.py` (three helper imports) referenced the moved modules but were not in the plan's `files_modified`. Leaving them dangling would break job discovery at runtime and fail the full suite / `import pcswitcher.orchestrator`.
- **Fix:** Rewrote their imports and prose references to `jobs.packages.*` — mandated by the phase guard ("update EVERY import across src/ AND tests/ … leave no dangling import").
- **Files modified:** src/pcswitcher/jobs/context.py, tests/integration/jobs/test_package_sync.py
- **Verification:** grep gate returns nothing; `python -c "import pcswitcher.orchestrator"` succeeds; full suite green.
- **Committed in:** a0fd807 (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)

**Impact on plan:** The extra two files are pure import repointing required by the relocation; no scope creep, no behaviour change.

## Issues Encountered
- `git add` of the old helper paths failed initially because `git mv` had already staged the renames; re-staged the destination paths instead.
- `ruff --fix` (run in Task 2's gate) reordered the aliased import in `test_package_state.py`; that lint fix was folded into the Task 2 commit.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- The package-sync module tree is in its final `jobs/packages/` layout; future package-job work imports from there.
- No empty config placeholders remain; a job gains a config section only when it has a real key.
- Full unit gate green (1011 passed), `ruff check` clean, `basedpyright` 0 errors. VM integration suite unchanged in behaviour (relocation only) — pending CI as usual.

## Self-Check: PASSED

All five created files exist (`jobs/packages/__init__.py`, `items.py`, `review.py`, `state.py`, `sync_core.py`); both task commits (`a0fd807`, `2a3639d`) are present in git history.
