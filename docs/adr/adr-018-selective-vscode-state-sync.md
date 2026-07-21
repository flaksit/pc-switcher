# ADR-018: SQLite-aware selective sync of VS Code editor state.vscdb

Status: Accepted

Date: 2026-07-20

## TL;DR
Sync each editor's global `state.vscdb` by mirroring every key except machine-bound `secret://` matches, which keep the target's own value, via a SQLite merge instead of the file-granular rsync mirror.

## Scope
This feature covers ONLY the invoking user (whoever runs `pc-switcher`), on both the exclude side and the merge side. Other users' editor DBs under a synced `/home` are deliberately not handled: a multi-user selective merge would need root on both ends (like `folder_sync`) to read/write files it does not own, which is out of scope (YAGNI). This is a property of this job, not a system-wide single-user assumption (see ADR-017 "Scope of the invoking user").

INVARIANT: the set of files `folder_sync` excludes from the mirror is EXACTLY the set this job merges — for the invoking user, each covered editor's `state.vscdb` AND its `state.vscdb.backup` (both are SQLite DBs with the same schema and are merged identically). No file is excluded without being merged, and no handled file is left to the wholesale mirror. Both sides derive from one tuple (`EDITOR_STATE_HANDLED_RELPATHS`), so they cannot drift.

## Implementation Rules
- The covered editor state DB files (each editor's `state.vscdb` and its `state.vscdb.backup`) are handled outside the `folder_sync` mirror: excluded from it non-overridably (global-first — emitted before the user filter surfaces, so no user `+` rule can re-expose them) and merged by `vscode_state_sync`.
- The merge preserves keys matching the configurable `preserve_key_globs` (default `secret://%`) by keeping the TARGET's value; every other key mirrors the source (target-only non-preserved keys dropped, matching the `--delete` fidelity).
- `vscode_state_sync` owns the editor-DB path set; `folder_sync` receives those paths and only translates them into rsync filters, holding no editor/home knowledge of its own.

## Context
`~/.config/<Editor>/User/globalStorage/state.vscdb` holds wanted global state (settings-adjacent, MRU) alongside VS Code SecretStorage session blobs under `secret://` keys — ciphertext encrypted with a per-machine OS-keyring key (gnome-libsecret) that is never synced. rsync is file-granular, so mirroring the whole file clobbers the target's own keyring-decryptable secrets and forces auth-backed extensions (GitHub, DB extensions) to re-login after every sync. Per-workspace `workspaceStorage/*/state.vscdb` carry no `secret://` keys and are not touched. The DB is quiescent during a sync (nothing open), so no live-writer handling is needed. See #195.

## Decision
- Add a toggleable `vscode_state_sync` `SyncJob` (default on) that runs after `folder_sync` and performs the selective SQLite merge above.
- Handle the editor DBs outside the `folder_sync` mirror (exclude them non-overridably; merge them in the dedicated job), so the target's live DB still holds its own secrets at merge time and no pre-step is needed. Keeping the VS Code specifics out of the generic mirror was a deliberate goal — `folder_sync` only translates the paths it is handed.
- Rejected: injecting a filter file into the user's config tree (invasive; collides with the user's `.pcswitcher-filter` surface and trips the seeding-pass detection); a btrfs-snapshot dependency (couples a functional feature to the rollback subsystem).

## Consequences
**Positive**:
- Machine-bound secrets never leave their home machine; auth-backed extensions no longer re-login after every sync.
- VS Code specifics stay out of the generic `folder_sync` mirror, which just translates provided paths to filters.

**Negative**:
- A first sync (target DB absent) still causes a one-time re-login, since the transferred DB carries no secret rows.
- Correctness depends on the DB staying quiescent during a sync (the operating rule); a live writer during the merge is unsupported.
- The covered-editor list and their `~/.config/<Editor>/` directory casing are fixed in code; a new editor or a non-standard layout needs a code change.

## References
- ADR-017: Mirror pc-switcher's own install; hardcode-exclude only its runtime state (related — this ADR adds a separate, non-overridable exclude for the editor state DBs)
- ADR-015: Topology-based sync-safety model (first-sync overwrite scope)
- ADR-014: Unified dry-run contract for all SyncJobs
- ADR-013: rsync-over-SSH as user-data transport
- Issue #195
- `src/pcswitcher/jobs/vscode_state_sync.py`, `src/pcswitcher/jobs/folder_sync.py` (`_editor_state_exclude_filters`)
