# ADR-017: Mirror pc-switcher's own install; hardcode-exclude only its runtime state

Status: Accepted

Date: 2026-07-20

Supersedes: ADR-016

Amended by: ADR-018 (hardcoded-exclude set extended to the editor state DBs)

## TL;DR
Folder sync mirrors pc-switcher's own install (uv tool venv + `~/.local/bin` shim) like any other file; only its runtime *state* directory stays hardcoded-excluded, so the install and the interpreter it depends on always travel together.

## Implementation Rules
- `FolderSyncJob` MUST hardcode-exclude exactly one path, the invoking user's `.local/share/pc-switcher/` (lock file, `sync-history.json`, logs), regardless of user config. This is the ONLY hardcoded exclude.
- The pc-switcher install MUST NOT be excluded: `.local/share/uv/tools/pcswitcher/` (the venv) and `.local/bin/pc-switcher` (the shim) mirror from source like every other uv tool, alongside the uv interpreter tree under `.local/share/uv/python/`.
- The hardcoded exclude MUST anchor relative to the invoking user's home and only apply when that home is inside the synced folder; it MUST precede user filter rules so no `+` rule can re-expose it (unchanged from ADR-016).
- `validate()` MUST abort the sync on a source/target CPU-architecture mismatch (`uname -m`), since the mirrored venv and interpreter are arch-specific binaries.

## Scope of "the invoking user"

The anchoring to "the invoking user's home" above scopes pc-switcher's OWN install and runtime state, which inherently live under whichever user runs the tool — one install, one runtime-state directory, per invoking user. This is NOT a statement that pc-switcher syncs only a single user's data: user-data sync (e.g. all of `/home`) can span many users. Do not generalize this per-invoking-user anchoring into a system-wide single-user assumption. The "single-**architecture** fleet" constraint below is about CPU arch (the mirrored binaries), also unrelated to how many users are synced.

## Context
ADR-016 hardcode-excluded the pc-switcher install (venv + shim) from the `/home` mirror to keep the "running install" machine-local. But the uv *interpreter* tree (`.local/share/uv/python/`) was not excluded, so it was `--delete`-mirrored to match the source. uv interpreters diverge per machine by patch version, so the frozen venv referenced an interpreter that existed only on the target — and the mirror deleted it, leaving a dangling shebang and a pc-switcher that could not execute on the target (#185). Nothing runs pc-switcher on the target during a sync except the install-on-target step, so the install does not need to be pinned machine-local; it only needs to stay internally consistent with its interpreter.

## Decision
- Hardcode-exclude only `.local/share/pc-switcher/` (runtime state); drop the venv and shim from the exclude set.
- Let the install mirror from source with its interpreter in the same pass, so both come from the source and stay consistent — the same way every other uv tool already syncs.
- Guard the new arch coupling with a `uname -m` source-vs-target check in `validate()` that aborts on mismatch.
- Keep the install-on-target step: it provisions a working pc-switcher on the target before the folder-sync step (relevant to #126), and its output being overwritten by the mirror is harmless.

## Consequences
**Positive**:
- pc-switcher's install and its interpreter can no longer desync; the #185 dangling-interpreter failure is structurally impossible.
- Uniform handling: pc-switcher is no longer a special-cased uv tool.
- The topology-safety state, lock, and logs (ADR-015) stay machine-local, as before.

**Negative**:
- The fleet must be single-architecture; the arch check turns an unsupported heterogeneous fleet into an explicit preflight abort rather than a cryptic exec failure.
- The target's pc-switcher install is overwritten by the source's on every sync (source wins); there is no downgrade guard on the mirrored install.

## References
- ADR-016: Hardcoded exclusion of pc-switcher's own runtime files (superseded by this ADR)
- ADR-015: Topology-based sync-safety model
- ADR-013: rsync-over-SSH as user-data transport
- Issue #185 (root cause and fix); issue #126 (why install-on-target is retained)
- `src/pcswitcher/jobs/folder_sync.py` (`_RUNTIME_EXCLUDE_RELPATHS`, `validate()` arch check)
