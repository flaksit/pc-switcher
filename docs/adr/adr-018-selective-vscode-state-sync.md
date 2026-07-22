# ADR-018: SQLite-aware selective sync of VS Code editor state.vscdb

Status: Accepted

Date: 2026-07-20

## TL;DR
Sync each VS Code-based editor's global `state.vscdb` (VS Code and its forks) by mirroring every key except machine-bound `secret://` matches, which keep the target's own value, via a SQLite merge instead of the file-granular rsync mirror.

## Scope
"VS Code-based editor" below means VS Code and its forks (Code, Antigravity, Cursor, VSCodium), never a generic editor.

This feature covers ONLY the invoking user (whoever runs `pc-switcher`), on both the exclude side and the merge side. Other users' VS Code state DBs under a synced `/home` are deliberately not handled: a multi-user selective merge would need root on both ends (like `folder_sync`) to read/write files it does not own, which is out of scope (YAGNI). This is a property of this job, not a system-wide single-user assumption (see ADR-017 "Scope of the invoking user").

INVARIANT: the set of files `folder_sync` excludes from the mirror is EXACTLY the set this job merges — for the invoking user, each covered VS Code-based editor's `state.vscdb` AND its `state.vscdb.backup` (both are SQLite DBs with the same schema and are merged identically). No file is excluded without being merged, and no handled file is left to the wholesale mirror. Both sides derive from one tuple (`VSCODE_STATE_HANDLED_RELPATHS`), so they cannot drift.

## Implementation Rules
- The covered VS Code state DB files (each editor's `state.vscdb` and its `state.vscdb.backup`) are handled outside the `folder_sync` mirror: excluded from it non-overridably (global-first — emitted before the user filter surfaces, so no user `+` rule can re-expose them) and merged by `vscode_state_sync`.
- The merge preserves the machine-bound SecretStorage keys by keeping the TARGET's value; every other key mirrors the source (target-only non-preserved keys dropped, matching the `--delete` fidelity).
- `vscode_state_sync` owns the VS Code state-DB path set; `folder_sync` receives those paths and only translates them into rsync filters, holding no VS Code/home knowledge of its own.
- The merge is driven through the `sqlite3` Python module, never the `sqlite3` CLI: in-process on the source, and as a `python3 -c` driver script on the target. The CLI is a separate Ubuntu package that is not installed by default, whereas `python3` is priority-important and its `libpython3.x-stdlib` dependency carries the `sqlite3` module — so the only target requirement, and the only thing `validate()` probes, is `python3 -c "import sqlite3"`. Rejected: installing the CLI on the target (validate must not mutate the target, and `package_sync` runs far too late — validation precedes every job); shipping a `sqlite3` binary from the source (needless arch/glibc coupling); an `uv run python` fallback (`uv` is not on `PATH` for the non-login shells this job uses, and `uv run` may reach for the network to provision an interpreter).

## Context
`~/.config/<Editor>/User/globalStorage/state.vscdb` holds wanted global state (settings-adjacent, MRU) alongside VS Code SecretStorage session blobs under `secret://` keys — ciphertext encrypted with a per-machine OS-keyring key (gnome-libsecret) that is never synced. rsync is file-granular, so mirroring the whole file clobbers the target's own keyring-decryptable secrets and forces auth-backed extensions (GitHub, DB extensions) to re-login after every sync. Per-workspace `workspaceStorage/*/state.vscdb` carry no `secret://` keys and are not touched. The DB is quiescent during a sync (nothing open), so no live-writer handling is needed. See #195.

## Decision
- Add a toggleable `vscode_state_sync` `SyncJob` (default on) that runs after `folder_sync` and performs the selective SQLite merge above.
- Handle these VS Code state DBs outside the `folder_sync` mirror (exclude them non-overridably; merge them in the dedicated job), so the target's live DB still holds its own secrets at merge time and no pre-step is needed. Keeping the VS Code specifics out of the generic mirror was a deliberate goal — `folder_sync` only translates the paths it is handed.
- Rejected: injecting a filter file into the user's config tree (invasive; collides with the user's `.pcswitcher-filter` surface and trips the seeding-pass detection); a btrfs-snapshot dependency (couples a functional feature to the rollback subsystem).

## Consequences
**Positive**:
- Machine-bound secrets never leave their home machine; auth-backed extensions no longer re-login after every sync.
- VS Code specifics stay out of the generic `folder_sync` mirror, which just translates provided paths to filters.

**Negative**:
- Correctness depends on the DB staying quiescent during a sync (the operating rule); a live writer during the merge is unsupported.
- The covered VS Code-based editor list, their `~/.config/<Editor>/` directory casing, and the preserved-key pattern are fixed in code; a new such editor, a non-standard layout, or a change in VS Code's key scheme needs a code change. Deliberate: these are VS Code internals, not user-facing tunables, so the job is off-the-shelf with nothing to configure.

## References
- ADR-017: Mirror pc-switcher's own install; hardcode-exclude only its runtime state (related — this ADR adds a separate, non-overridable exclude for the VS Code state DBs)
- ADR-015: Topology-based sync-safety model (first-sync overwrite scope)
- ADR-014: Unified dry-run contract for all SyncJobs
- ADR-013: rsync-over-SSH as user-data transport (the file-granular `folder_sync` mirror this job carves an exception out of; this job itself transfers via SFTP, not rsync)
- Issue #195
- `src/pcswitcher/jobs/vscode_state_sync.py`, `src/pcswitcher/jobs/folder_sync.py` (`_vscode_state_exclude_filters`)
