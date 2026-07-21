# ADR-018: SQLite-aware selective sync of VS Code editor state.vscdb

Status: Accepted

Date: 2026-07-20

Amends: ADR-017 (extends its hardcoded-exclude set to the editor state DBs)

## TL;DR
Sync each editor's global `state.vscdb` by mirroring every key except machine-bound `secret://` matches, which keep the target's own value, via a normal-user SQLite merge instead of the file-granular rsync mirror.

## Scope

This feature covers ONLY the invoking user (whoever runs `pc-switcher`), on both the exclude side and the merge side. Other users' editor DBs under a synced `/home` are deliberately not handled: a multi-user selective merge would need root on both ends (like `folder_sync`) to read/write files it does not own, which is out of scope (YAGNI). This is a property of this job, not a system-wide single-user assumption (see ADR-017 "Scope of the invoking user").

INVARIANT: the set of files `folder_sync` excludes from the mirror is EXACTLY the set this job merges — for the invoking user, each covered editor's `state.vscdb` AND its `state.vscdb.backup` (both are SQLite DBs with the same schema and are merged identically). No file is excluded without being merged, and no handled file is left to the wholesale mirror. Both sides derive from one tuple (`EDITOR_STATE_HANDLED_RELPATHS`), so they cannot drift.

## Implementation Rules
- `folder_sync` MUST hardcode-exclude the invoking user's covered editors' `state.vscdb` and `state.vscdb.backup`, folding them into the same global-first, non-overridable exclude tier as the runtime state, emitted before both filter surfaces so no user `+` rule can re-expose them.
- `vscode_state_sync` OWNS which absolute paths are the editor DBs and exposes them via `editor_state_exclude_paths()` — a function (the paths are dynamic, resolved against the invoking user's home at call time), not a constant. `folder_sync` imports it one-way (`vscode_state_sync` MUST NOT import from `folder_sync` — avoids an import cycle) and only translates each absolute path into a root-anchored rsync filter for the folder being synced (skipping paths outside that folder). `folder_sync` MUST hold no knowledge of editors or home layout. The exclude set and the merge set share one source of truth (`EDITOR_STATE_DB_RELPATHS`) so they cannot drift.
- `vscode_state_sync` MUST rebuild each target DB by mirroring all keys except `preserve_key_globs` matches (default `secret://%`, SQLite `LIKE` patterns), which keep the target's own value: source-strip (copy source DB, `DELETE` matched rows) -> transfer the neutral DB to a temp path in the target live DB's directory -> target-inject (`ATTACH` the live DB, `INSERT` its matched rows into the neutral DB) -> atomic `mv` over the live DB.
- The merge MUST run as the invoking normal user (no sudo), skip editors whose DB is absent on the source, and skip the target-inject step when the target DB is absent (first sync). It MUST honor dry-run per ADR-014 (detect and log, no target writes).
- Every SQL string literal (glob values, the `ATTACH` path) MUST double embedded single quotes; every shell path/arg MUST be `shlex.quote`d.

## Context
`~/.config/<Editor>/User/globalStorage/state.vscdb` holds wanted global state (settings-adjacent, MRU) alongside VS Code SecretStorage session blobs under `secret://` keys — ciphertext encrypted with a per-machine OS-keyring key (gnome-libsecret) that is never synced. rsync is file-granular, so mirroring the whole file clobbers the target's own keyring-decryptable secrets and forces auth-backed extensions (GitHub, DB extensions) to re-login after every sync. This is a sub-issue of #190; the other half (workspace-trust loss, which is VS Code version skew) is out of scope. Per-workspace `workspaceStorage/*/state.vscdb` carry no `secret://` keys and are not touched. The DB is quiescent during a sync (nothing open), so no live-writer handling is needed. ADR-017 stated its runtime-state directory was the "only hardcoded exclude"; that claim is amended here.

## Decision
- Add a toggleable `vscode_state_sync` `SyncJob` (default on) that runs after `folder_sync` and performs the SQLite merge above, preserving keys matched by the configurable `preserve_key_globs`.
- Hand the editor DBs to that job by hardcode-excluding them from the `folder_sync` mirror, so the target's live DB still holds its own secrets at merge time and no pre-step is needed.
- Preserve fidelity: non-preserved keys take the source's value; target-only non-preserved keys are dropped (consistent with the rsync `--delete` mirror); preserved keys keep the target's value.
- Rejected: injecting a filter file into the user's config tree (invasive; collides with the user's `.pcswitcher-filter` surface and trips the seeding-pass detection); a btrfs-snapshot dependency (couples a functional feature to the rollback subsystem).
- The hardcoded-exclude tier now contains (a) `.local/share/pc-switcher/` (ADR-017) and (b) the editor state DBs (this ADR); ADR-017's "only hardcoded exclude" claim is extended, not replaced — its core decision (mirror the install, exclude only runtime state) still stands.

## Consequences
**Positive**:
- Machine-bound secrets never leave their home machine; auth-backed extensions no longer re-login after every sync.
- The merge runs as the normal user on the user's own files, so it needs no sudo and no snapshot coupling.
- One owner (the `editor_state_exclude_paths()` function, deriving from `EDITOR_STATE_DB_RELPATHS`) keeps the `folder_sync` exclude set and the merge set from drifting; `folder_sync` stays free of editor/home knowledge and just translates absolute paths to filters.

**Negative**:
- A first sync (target DB absent) still causes a one-time re-login, since the neutral DB carries no secret rows.
- Correctness depends on the DB staying quiescent during a sync (the operating rule); a live writer during the merge is unsupported.
- The covered-editor list and their `~/.config/<Editor>/` directory casing are fixed in code; a new editor or a non-standard layout needs a code change.
- Only the invoking user's editor DBs are covered. A second human user's `state.vscdb` under a synced `/home` is NEITHER excluded NOR merged — it falls through to `folder_sync`'s normal wholesale mirror (its secrets are clobbered, as before this feature); this preserves the exclude-set == merge-set invariant for every user (acceptable per Scope — revisit if multi-user is needed).

## References
- ADR-017: Mirror pc-switcher's own install; hardcode-exclude only its runtime state (amended by this ADR)
- ADR-015: Topology-based sync-safety model (first-sync overwrite scope)
- ADR-014: Unified dry-run contract for all SyncJobs
- ADR-013: rsync-over-SSH as user-data transport
- Issue #195 (this change); issue #190 (parent — secret loss half)
- `src/pcswitcher/jobs/vscode_state_sync.py` (`editor_state_exclude_paths`, `EDITOR_STATE_DB_RELPATHS`), `src/pcswitcher/jobs/folder_sync.py` (`_editor_state_exclude_filters`)
