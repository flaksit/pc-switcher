---
type: quick-context
quick_id: 260720-vhr
issue: 195
status: locked
---

# CONTEXT — Fix #195: Selective SQLite-aware sync of VS Code `state.vscdb`

The design below is **LOCKED** (settled with the user before planning). The planner and executor MUST treat these as given and MUST NOT re-open them.

## Problem

`~/.config/<Editor>/User/globalStorage/state.vscdb` holds wanted global state (settings-adjacent, MRU) **and** VS Code SecretStorage session blobs under `secret://…` keys — ciphertext encrypted with a per-machine OS keyring (gnome-libsecret) key that is never synced. rsync is file-granular, so mirroring the whole file clobbers the target's own keyring-decryptable secrets and forces auth-backed extensions (GitHub Pull Requests/Actions, DB extensions) to re-login after every sync. Sub-issue of #190; workspace-trust loss (the other half of #190) is VS Code version skew and is OUT of scope.

## Verified facts (measured on this machine)

- Real path: `~/.config/<Editor>/User/globalStorage/state.vscdb` (the issue text's `~/.config/Code/User/state.vscdb` is imprecise). A `state.vscdb.backup` sidecar sits beside it.
- Schema: single table `ItemTable(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)`. `journal_mode=delete`, no WAL.
- Operating rule: nothing is open during a sync, so the DB is quiescent (no live writer).
- Only `secret://%` keys are machine-bound and in scope. Per-workspace `workspaceStorage/*/state.vscdb` carry no `secret://` keys — only the per-editor globalStorage DB matters.

## Locked decisions

1. New job `vscode_state_sync` (`src/pcswitcher/jobs/vscode_state_sync.py`), a `SyncJob`. Togglable via `sync_jobs.vscode_state_sync` (default `true`). Runs AFTER `folder_sync`.
2. `folder_sync` hardcode-excludes the editor state DBs (both `state.vscdb` and `state.vscdb.backup`) via a shared constant OWNED BY the vscode module and imported by `folder_sync` — added to the same GLOBAL-FIRST, non-overridable exclude tier as `_RUNTIME_EXCLUDE_RELPATHS`/`_runtime_exclude_filters` (ADR-016), so a user `+` rule can never re-expose them and the user cannot forget to exclude them. Rejected alternatives: a filter-file injection into the user's config tree (invasive; collides with the user's `.pcswitcher-filter` surface; trips `_needs_seeding_pass`), and a btrfs-snapshot dependency (couples a functional feature to the rollback subsystem).
3. Editors covered: Code, Antigravity, Cursor, VSCodium. Iterate the list; skip editors whose DB does not exist on the source.
4. Merge mechanics — secrets never leave their home machine; no pre-step needed (the DB is excluded from the mirror, so the target's live DB still holds its original secrets at job time):
   a. Source-side: copy source DB to a temp file, `DELETE FROM ItemTable` for rows matching the preserve-pattern → a "neutral" DB (source secrets stripped).
   b. Transfer the neutral DB to a target temp path.
   c. Target-side: `INSERT` the preserve-matched rows FROM the target's live DB INTO the neutral DB, then atomically `mv` the neutral DB over the target's live DB.
   d. First sync / target DB absent: the neutral DB (no secret rows) becomes the target DB — clean, one-time re-login only.
   e. Operates on the invoking user's own files on both ends → runs as the NORMAL user, NO sudo/root (unlike `folder_sync`). Atomic `mv` within the same directory preserves ownership/perms.
   f. dry-run: perform no writes on the target; log intended actions.
5. Fidelity: full mirror except preserved keys. Non-preserved keys become source's values; target-only non-preserved keys are dropped (consistent with rsync `--delete`). Preserved (secret) keys keep the target's own value.
6. Configurable preserve pattern: config `vscode_state_sync.preserve_key_globs`, a list of SQLite `LIKE` patterns, default `["secret://%"]`. Matched keys keep the target's value.
7. Validation: `sqlite3` available on source and target. No btrfs dependency.

## Docs / consistency (in scope)

- New ADR (read ADR-001 first for conventions): SQLite-aware selective state sync; must amend ADR-016's "these are the ONLY hardcoded excludes" claim (now also the editor state DBs).
- Update `src/pcswitcher/default-config.yaml` (add `vscode_state_sync: true` under `sync_jobs` + a `vscode_state_sync:` block with `preserve_key_globs`), `src/pcswitcher/schemas/config-schema.yaml`, `docs/configuration.md`, and CLAUDE.md's job list. Reconcile any sync-step numbering if a new job changes counts.

## Tests (proportionate)

Unit-test the merge logic (source-strip, target-inject, first-sync/absent-DB, multi-pattern preserve config, full-mirror deletion of target-only keys) and `folder_sync`'s exclude emission for the editor DBs. Add integration coverage where practical.

## Constraints

- Branch `195-selective-vscode-db-sync` already exists, stacked on `fix-191-foldersync-progressbar` (deliberate — do NOT rebase onto main).
- Integration CI runs only on PRs targeting `main`, so this stacked PR skips integration CI.
- Create the PR as a DRAFT.
