# VS Code State Sync

`vscode_state_sync` selectively syncs each VS Code-based editor's (VS Code and its forks) global `state.vscdb` — the SQLite database under `~/.config/<Editor>/User/globalStorage/` — and the install-shared DB under `~/.vscode-shared/sharedStorage/`. These mix wanted global state (settings-adjacent values, MRU lists) with VS Code SecretStorage session blobs. The secret blobs live under `secret://` keys and are encrypted with a per-machine OS-keyring key that is never synced, so a plain file mirror would clobber the target's own decryptable secrets and force auth-backed extensions (GitHub, database extensions) to re-login after every sync. `folder_sync` excludes these DBs from its mirror (non-overridably); this job rebuilds each one instead.

The job has no settings: the editor list, DB layout, and the preserved-key patterns are VS Code internals owned by the module, not things a user configures. Enable or disable it via `sync_jobs` like any other job; see the [configuration reference](../configuration.md#sync_jobs).

## What the merge preserves

The job mirrors every key from the source **except** the machine-bound account keys, whose value the **target keeps**. Two kinds are preserved: the `secret://` SecretStorage blobs, and the auth session preferences that point into them (`userDataSyncAccountPreference` and the per-extension `<extensionId>-<providerId>-<scopes>` keys). The pointers matter as much as the blobs — their value is a session id regenerated on every sign-in, so mirroring one hands the target a session it does not hold, and VS Code asks you to sign in again. Non-matched keys take the source's value; keys present only on the target and not matched are dropped (the same fidelity as the `folder_sync` `--delete` mirror).

The preserved set is deliberately narrow: sibling keys holding the account *name* keep syncing (they are the same on every machine), and an extension you authenticate for the first time will not be covered until its key is added — it then asks for sign-in on the target once, rather than something you wanted synced silently going stale.

`~/.config/Code/machineid` is **not** carved out. pc-switcher already mirrors Settings Sync's own bookkeeping, so keeping the machine id common makes the fleet one logical Settings Sync machine, which is self-consistent; splitting it would leave two identities sharing one machine's sync state. Consequence: the Settings Sync machine list shows a single entry for the fleet.

## Coverage and execution

Covered VS Code-based editors: Code, Antigravity, Cursor, VSCodium. Also covered is `~/.vscode-shared/sharedStorage/state.vscdb`, an install-shared state DB outside any editor's config directory (recent VS Code versions) holding cross-install state such as the workspace-trust list and recently-opened paths; it has the same schema and is merged the same way. For each covered DB, both the main `state.vscdb` and its `state.vscdb.backup` sidecar are handled identically — the exact set `folder_sync` excludes is the exact set this job merges, so no file is hidden from the mirror without being merged. A file absent on the source is skipped. On a first sync (the target has no such DB yet) the target simply receives the secret-stripped database, causing a one-time re-login. The job runs after `folder_sync`, as the invoking normal user (no `sudo`), and needs `sqlite3` on both machines.

Scope: this covers **only the invoking user** — whoever runs `pc-switcher`. If a synced `/home` contains other users, their VS Code state DBs are excluded from the mirror (so their secrets are never clobbered) but are not merged, so their VS Code global state does not sync. Multi-user coverage would require running the merge as root and is not currently supported.
