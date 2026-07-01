---
phase: 01-home-sync-mvp-user-data-sync
plan: "02"
subsystem: config
tags: [yaml, jsonschema, folder-sync, rsync, config-schema]

requires: []
provides:
  - "folder_sync boolean flag in sync_jobs schema and default config (enables orchestrator job discovery)"
  - "top-level folder_sync object schema with folders array (path, enabled, excludes per entry)"
  - "default-config.yaml ships /home and /root folder entries with D-11 exclusions"
affects:
  - 01-03-folder-sync-job
  - 01-04-divergence-detection
  - 01-05-rsync-transport
  - 01-06-integration-test

tech-stack:
  added: []
  patterns:
    - "Job config shape: top-level YAML key matching job name -> dict routed via get_job_config()"
    - "Folder entry schema: {path: string, enabled: bool, excludes: list[str]} with additionalProperties:false"
    - "Default excludes in YAML (not Python): machine-specific keys, GPU caches, VS Code cache dirs"

key-files:
  created: []
  modified:
    - src/pcswitcher/schemas/config-schema.yaml
    - src/pcswitcher/default-config.yaml

key-decisions:
  - "folder_sync (not user_data) is the canonical job name, replacing the placeholder comment in default-config.yaml"
  - "Dev-tool caches (uv, pip, cargo, npm) and VS Code Code/User/ state intentionally NOT excluded per D-11"
  - "Exclude patterns live in default-config.yaml, not hardcoded in Python, so users can override them"

patterns-established:
  - "Pattern: folder entry schema - {path, enabled, excludes} with additionalProperties:false on item objects"
  - "Pattern: default excludes in YAML with inline comments explaining machine-specific vs. regenerable rationale"

requirements-completed:
  - REQ-sync-scope-user-data
  - REQ-machine-specific-exclusions

coverage:
  - id: D1
    description: "config-schema.yaml accepts folder_sync flag under sync_jobs.properties (additionalProperties:false guard passed)"
    requirement: REQ-sync-scope-user-data
    verification:
      - kind: unit
        ref: "python -c \"import yaml; s=yaml.safe_load(open('src/pcswitcher/schemas/config-schema.yaml')); assert 'folder_sync' in s['properties']['sync_jobs']['properties']\""
        status: pass
    human_judgment: false
  - id: D2
    description: "config-schema.yaml top-level folder_sync object with required folders array and additionalProperties:false"
    requirement: REQ-sync-scope-user-data
    verification:
      - kind: unit
        ref: "python -c \"import yaml; s=yaml.safe_load(open('src/pcswitcher/schemas/config-schema.yaml')); assert s['properties']['folder_sync']['required']==['folders']\""
        status: pass
    human_judgment: false
  - id: D3
    description: "default-config.yaml loads cleanly via Configuration.from_yaml with folder_sync: true and /home + /root entries"
    requirement: REQ-sync-scope-user-data
    verification:
      - kind: unit
        ref: "python -c \"from pathlib import Path; from pcswitcher.config import Configuration; c=Configuration.from_yaml(Path('src/pcswitcher/default-config.yaml')); assert c.sync_jobs['folder_sync'] is True\""
        status: pass
    human_judgment: false
  - id: D4
    description: "Default excludes include .ssh/id_* and .config/tailscale for both /home and /root; VS Code cache dirs excluded for /home"
    requirement: REQ-machine-specific-exclusions
    verification:
      - kind: unit
        ref: "python -c \"from pathlib import Path; from pcswitcher.config import Configuration; c=Configuration.from_yaml(Path('src/pcswitcher/default-config.yaml')); home=c.get_job_config('folder_sync')['folders'][0]['excludes']; assert '.ssh/id_*' in home and '.config/tailscale' in home and '.config/Code/Cache' in home\""
        status: pass
    human_judgment: false

duration: 2min
completed: "2026-06-30"
status: complete
---

# Phase 01 Plan 02: Folder Sync Config Schema and Defaults Summary

**folder_sync schema registered in config-schema.yaml and default-config.yaml ships /home + /root with D-11 exclusions, enabling orchestrator job discovery without additionalProperties:false validation errors**

## Performance

- **Duration:** 2 min
- **Started:** 2026-06-30T13:26:25Z
- **Completed:** 2026-06-30T13:28:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Added `folder_sync: boolean` to `sync_jobs.properties` in `config-schema.yaml` (resolves RESEARCH Pitfall 6 — configurations naming `folder_sync` are no longer rejected by the `additionalProperties: false` guard)
- Added top-level `folder_sync` object property to `config-schema.yaml` with a `folders` array schema: each entry requires `path` (string) and allows `enabled` (bool) and `excludes` (list of strings)
- Updated `default-config.yaml` to enable `folder_sync: true` under `sync_jobs` (replacing the `# user_data: true` placeholder)
- Shipped `/home` and `/root` folder entries in `default-config.yaml` with the full D-11 exclusion list: `.ssh/id_*`, `.config/tailscale`, GPU/shader caches, fontconfig cache, and VS Code regenerable cache dirs — without excluding dev-tool caches or `Code/User/` state

## Task Commits

Each task was committed atomically:

1. **Task 1: Register folder_sync in config-schema.yaml** - `5d3380a` (feat)
2. **Task 2: Ship folder_sync defaults in default-config.yaml** - `efc07d3` (feat)

## Files Created/Modified

- `src/pcswitcher/schemas/config-schema.yaml` - Added `folder_sync` boolean to `sync_jobs.properties`; added top-level `folder_sync` object with `folders` array schema
- `src/pcswitcher/default-config.yaml` - Enabled `folder_sync: true` under `sync_jobs`; added `folder_sync:` section with `/home` and `/root` default folder entries and D-11 exclusion lists

## Decisions Made

None beyond what the plan specified. The job name `folder_sync` (not `user_data`) was mandated by D-01 and simply implemented as the plan directed.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Plan 03 (FolderSyncJob implementation) can now reference `folder_sync` in config without schema rejections
- `Configuration.from_yaml` already routes the top-level `folder_sync` dict to `get_job_config('folder_sync')` via the existing `global_keys` exclusion pattern in `config.py` — no change to `config.py` needed
- All 410 existing unit and contract tests remain green

---
*Phase: 01-home-sync-mvp-user-data-sync — Completed: 2026-06-30*
