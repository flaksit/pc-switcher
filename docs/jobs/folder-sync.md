# Folder Sync

`folder_sync` mirrors whole directory trees (by default `/home` and `/root`) from the source to the target via rsync-over-SSH, running as root on both ends to preserve ownership, permissions, ACLs, and xattrs.

Configuration for this job — the `folders` list and each folder's `filter_file` — lives in the [configuration reference](../configuration.md#folder_sync). This document covers what the job does with those settings.

Each folder is mirrored to the target, minus the paths its filter rules exclude. Set `enabled: false` to skip a folder. A filtered-out file is left untouched on the target if it already exists there, so machine-specific files (SSH keys, Tailscale config) can stay independent on each machine.

## Filter rules

Filter rules decide what is and isn't synced. They live in two kinds of file:

- **`filter_file`** — the folder's main rule list. Optional: omit it for a folder that needs no central rule list, and only the runtime excludes and any `.pcswitcher-filter` files apply. `pc-switcher init` writes `home.filter` and `root.filter` next to `config.yaml`; edit them to taste. If a folder *does* configure a `filter_file` but the file is missing, the sync stops with an error.
- **`.pcswitcher-filter`** — a per-directory rule file that works like `.gitignore`: drop one into any directory and its rules apply to that directory and everything below. These files sync to the target too.

Each rule is `- pattern` to exclude or `+ pattern` to re-include. The **first** matching rule wins, so put a `+` re-include before the broader `-` it carves out of. If a path is matched in both a `filter_file` and a `.pcswitcher-filter`, the `filter_file` wins. This allows for central policy that can not be overridden by a per-directory file.

A comment is a line whose first non-blank character is `#`. **End-of-line comments are not supported**: rsync does not strip a trailing `# …` from a rule, so `- .nv  # gpu cache` makes the pattern the literal `.nv  # gpu cache` — it matches nothing and the rule silently does nothing (no error). Trailing whitespace is likewise part of the pattern. Always put comments on their own line above the rule.

Pattern syntax:

- names match in full, never as substrings: each wildcard-free component must match a whole file or directory name — `foo` matches an entry named exactly `foo`, not `fool` or `afoo`. Use wildcards for partial names (`foo*`, `*foo*`).
- a pattern with no `/` is matched against the final path component (a single file or directory name); a pattern with a `/` is matched against the whole path below the folder root
- a leading `/` anchors to the folder root; without it a pattern matches at any depth
- a trailing `/` matches directories only; without it a pattern matches either a file or a directory of that name
- `?` matches one character, `*` matches within a path segment, `**` matches across `/`, and `[…]` is a character class
- `dir/**` matches a directory's contents but **not** the directory entry itself; `dir/***` matches the directory *and* everything in it. Reach for `/***` when a later exclude (like `- .cache/*`) would otherwise drop the directory and stop rsync from descending into it

| Pattern | Matches |
| ------- | ------- |
| `.ssh/id_*` | `id_*` inside any `.ssh` directory |
| `.config/tailscale` | a `tailscale` file or directory inside any `.config` |
| `/lost+found` | `lost+found` only at the folder root |
| `*.tmp` | any `.tmp` file, at any depth |
| `node_modules/` | any directory named `node_modules` |

Full pattern reference: rsync's manpage, "FILTER RULES" section [https://download.samba.org/pub/rsync/rsync.1](https://download.samba.org/pub/rsync/rsync.1).

## Keep a subfolder inside an excluded one

To drop most of a directory but keep selected children — for example drop `~/.cache` but keep the dev-tool caches so offline work doesn't re-download them — re-include the children before excluding the rest:

```text
+ .cache/
+ .cache/uv/***
+ .cache/pip/***
- .cache/*
```

The `/***` (not `/**`) matters here: it re-includes the `uv` and `pip` directory entries themselves, so the following `- .cache/*` can't drop them and rsync descends to copy their contents. The shipped `home.filter` already does this; add more `+ .cache/<tool>/***` lines for other caches you want to keep.

## `authorized_keys`

pc-switcher connects to the target **as the normal login user** (root SSH login is never used — only the `rsync` binary is elevated with `sudo` on each end; see ADR-013), authenticating with that user's SSH identity. The target therefore checks the connection against **that user's** `~/.ssh/authorized_keys`, which must contain the source machine's public key.

Because `~/.ssh/authorized_keys` is under a synced folder, a full mirror overwrites the target's copy with the source's. If the source's copy does not list the source machine's own public key, the mirror deletes the key the target was using to authorize the sync — and the **next** sync's SSH connection is rejected. The sync locks itself out of its own target.

Pick one of two approaches:

1. **Exclude it (safest default).** Each machine keeps its own independent access list. The shipped `home.filter` and `root.filter` do this:

   ```text
   - .ssh/authorized_keys
   ```

2. **Sync it as one shared fleet access list.** Only safe if the file lists **every** pc-switcher machine's public key — including the machine's own — so a mirror can never drop the key the next sync needs. Since the file is identical everywhere once synced, it must be the union of all machines' keys.

   To set this up, before the first sync build that union in `~/.ssh/authorized_keys` on one machine, then let the sync propagate it. On each machine the key to add is the public half of the SSH identity pc-switcher connects with (the one your agent offers, for example `~/.ssh/id_ed25519.pub`):

   ```bash
   # Collect each machine's public key (run per machine, copy the output)
   cat ~/.ssh/id_ed25519.pub

   # On the machine you're seeding, append every machine's key (its own included),
   # deduplicate, and keep permissions tight:
   sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```

   Then remove the `- .ssh/authorized_keys` line from the relevant filter file so it syncs.

Note that `/root` has its own `~/.ssh/authorized_keys` (`/root/.ssh/authorized_keys`) — but the SSH session is the normal user's, not root's, so only the **user's** `authorized_keys` gates the connection. Apply the same reasoning to `root.filter` only if you have a workflow that logs into the fleet as root directly.

## Coming from .gitignore

The syntax resembles `.gitignore`, with three differences worth knowing:

- **Signs, not `!`** — `- pattern` excludes, `+ pattern` re-includes.
- **First match wins** (the opposite of gitignore) — order rules from specific to general.
- **Only a leading `/` anchors** — a middle slash does not, so for `/home` keep patterns unanchored to match under every user's home.

Otherwise it matches gitignore (basenames, a trailing `/` for directories, `*`/`**`/`[…]`), and a `.pcswitcher-filter` behaves like a committed `.gitignore`.

## Always excluded

Several groups are excluded from the mirror and cannot be re-included by any filter rule. Two are unconditional; three more are conditional on another job being enabled:

- pc-switcher's own runtime state — `~/.local/share/pc-switcher/` (lock file, sync history, logs) — so a sync never disturbs the target's sync state or per-machine logs (ADR-017); its install itself (uv tool venv and `~/.local/bin` shim) mirrors like any other file, so it stays consistent with the interpreter it depends on.
- pc-switcher's machine-specific package decision files — `~/.config/pc-switcher/*.decisions.yaml` (one file per package manager) — excluded unconditionally, regardless of which package jobs are enabled, so a machine-specific package list is never accidentally pushed to a peer (D-09; see [Package Sync](package-sync.md#machine-specific-packages)).
- If `vscode_state_sync` is enabled, the VS Code state DBs (`state.vscdb` and `state.vscdb.backup` for Code, Antigravity, Cursor, VSCodium, plus the install-shared `~/.vscode-shared/sharedStorage/`) — these are handed to `vscode_state_sync`, which merges them selectively so machine-bound account rows are never clobbered (ADR-018; see [VS Code state sync](vscode-state-sync.md)).
- If `snap_sync` is enabled, every `~/snap/<app>/<revision>` directory (never `common` or `current`) — `snap_sync` itself converges these via `snap install`/`snap refresh --revision`, so `folder_sync` stops mirroring the ones it manages (see [Package Sync](package-sync.md)).
- If `flatpak_sync` is enabled, `~/.local/share/flatpak` (never `~/.var/app`, which stays `folder_sync`'s territory) — `flatpak_sync` itself provisions this store (see [Package Sync](package-sync.md)).

Enabling `snap_sync` or `flatpak_sync` supplies its exclusion automatically; any hand-written filter rule for those paths in a personal filter file can be deleted once the corresponding job is enabled.
