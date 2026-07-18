# ADR-016: Hardcoded exclusion of pc-switcher's own runtime files from folder sync

Status: Accepted

Date: 2026-07-03

## TL;DR
Folder sync always excludes pc-switcher's own runtime state, install, and logs; these are the only hardcoded excludes — every other exclusion stays user-configurable.

## Implementation Rules
- `FolderSyncJob` MUST always exclude, regardless of user config, the invoking user's:
  - `.local/share/pc-switcher/` (lock file, `sync-history.json`, logs)
  - `.local/share/uv/tools/pcswitcher/` (uv tool install / virtualenv)
  - `.local/bin/pc-switcher` (entry-point shim)
- Excludes MUST be anchored relative to the invoking user's home and only apply when that home is inside the synced folder (no effect when syncing e.g. `/root` as a normal user).
- Hardcoded excludes MUST precede user excludes so a user include rule can never re-expose them.
- These are the ONLY hardcoded excludes. All other exclusions (machine-specific keys, caches, etc.) live in `default-config.yaml` and are user-editable.

## Context
The default folder-sync scope is the real `/home` (and `/root`) mirrored with `rsync --delete`. pc-switcher stores its own runtime state under the user's home: `sync-history.json` (the topology-safety state per ADR-015), the unified lock file, logs, its uv-tool install, and its `~/.local/bin` shim. A naive `/home` mirror would `--delete`-clobber the target's copy of these mid-sync — most damagingly overwriting the target's `sync-history.json` with the source's, corrupting the very state the ADR-015 gates rely on, and overwriting/deleting the running install and the target's own logs.

## Decision
- Exclude pc-switcher's runtime state, install, and logs in code (not merely in the default config), so the protection holds even if a user rewrites their exclude list.
- Keep every other exclusion user-configurable; hardcode nothing else.
- Logs are machine-local: each machine keeps its own; the whole `.local/share/pc-switcher/` directory is excluded rather than enumerating individual files.

## Interaction with user filter rules (#166)

Folder sync now emits user filter rules via two rsync surfaces: a per-folder central `merge <filter_file>` and a tree-wide `dir-merge /.pcswitcher-filter`. The runtime-protection excludes in this ADR remain FIRST of all three groups — ahead of both the central merge and the dir-merge (GLOBAL-FIRST: merge before dir-merge) — so no central `+` and no per-dir `+` rule can ever re-expose pc-switcher's own runtime files.

## Consequences
**Positive**:
- A `/home` mirror can never clobber the target's sync-history, lock, logs, or running install.
- The topology-safety model (ADR-015) stays intact across real-home syncs.
- Minimal, auditable hardcoding; users retain full control over everything else.

**Negative**:
- Only the invoking user's runtime files are protected; other users' pc-switcher state (if any) under `/home` is not specially handled (acceptable under the current single-user model).
- pc-switcher's own logs and history do not propagate between machines (intended: they are machine-local diagnostics/state).

## References
- ADR-013: rsync-over-SSH as user-data transport
- ADR-015: Topology-based sync-safety model
- `src/pcswitcher/jobs/folder_sync.py` (`_RUNTIME_EXCLUDE_RELPATHS`, `_runtime_exclude_filters`, and `_build_rsync_cmd` for the merge/dir-merge emission point)
