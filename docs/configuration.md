# Configuration Reference

Complete reference for `~/.config/pc-switcher/config.yaml`. Run `pc-switcher init` to write the annotated default file (`src/pcswitcher/default-config.yaml`), then edit it.

The config is validated against a JSON Schema on load (`src/pcswitcher/schemas/config-schema.yaml`). Unknown top-level keys and unknown job names are rejected. Both source and target run with the same config: it is copied to the target during sync (step 9 of the sync sequence).

## `logging`

Controls where log messages appear, based on severity. Three independent floors — a message is emitted to a destination when its level is at or above that destination's floor.

```yaml
logging:
  file: DEBUG        # floor for the log file (~/.local/share/pc-switcher/logs/)
  tui: INFO          # floor for terminal output during sync
  external: WARNING  # additional floor for third-party libraries (asyncssh, etc.)
```

### Log levels

| Level | Value | Description |
| ----- | ----- | ----------- |
| DEBUG | 10 | Internal diagnostics, very verbose |
| FULL | 15 | Operational details (file-level sync info) |
| INFO | 20 | High-level progress (job start/complete) |
| WARNING | 30 | Unexpected but non-fatal conditions |
| ERROR | 40 | Recoverable errors |
| CRITICAL | 50 | Unrecoverable errors, sync must abort |

`file` and `tui` set the floor for their destination. `external` is an *additional* floor applied only to third-party library logs: an external log must clear **both** `external` and the destination floor (`file` or `tui`) to appear. Use `external: DEBUG` to surface asyncssh connection details; keep it at `WARNING` to suppress library noise.

Log files are written in JSON-lines format to `~/.local/share/pc-switcher/logs/`.

Debug SSH connection issues:

```yaml
logging:
  file: DEBUG
  tui: INFO
  external: DEBUG  # show asyncssh debug logs
```

Quiet terminal, full log file:

```yaml
logging:
  file: DEBUG   # still record everything to the file
  tui: ERROR    # show only errors in the terminal
  external: ERROR
```

## `sync_jobs`

Enables or disables optional sync jobs. Only job names listed here are discovered and run; unknown names fail validation.

```yaml
sync_jobs:
  dummy_success: true       # test job that completes successfully
  dummy_fail: false         # test job that fails at a configurable time
  folder_sync: true         # sync /home and /root via rsync
  vscode_state_sync: true   # selective, SQLite-aware sync of VS Code state.vscdb
```

## `disk_space_monitor`

Disk-space checks before and during a sync. Thresholds are either a percentage (`"20%"`) or an absolute size (`"50GiB"`, `"500MiB"`, `"50GB"`, `"500MB"`).

```yaml
disk_space_monitor:
  preflight_minimum: "20%"   # required free space before sync starts
  runtime_minimum: "15%"     # required free space during sync — CRITICAL, aborts if crossed
  warning_threshold: "25%"   # logs a WARNING while free space is below this
  check_interval: 30         # seconds between checks during sync (5–300)
```

`preflight_minimum` is checked on both hosts before any mutation; a shortfall aborts the sync. `runtime_minimum` is enforced by a background monitor while jobs run and aborts the sync if crossed. `warning_threshold` only logs.

## `btrfs_snapshots`

Automatic btrfs snapshots taken before and after a sync (the rollback point). Subvolumes **must** match your machine's layout — check with `sudo btrfs subvolume list /`.

```yaml
btrfs_snapshots:
  subvolumes:        # subvolume names, each must start with @
    - "@"
    - "@home"
  keep_recent: 3     # always retain snapshots for the N most recent sync sessions
  # max_age_days: 7  # optional: also delete snapshots older than N days
```

`keep_recent` takes precedence over `max_age_days`: the N most recent sessions are always kept regardless of age. `max_age_days` is optional; omit it to disable age-based cleanup.

## `dummy_success` / `dummy_fail`

Per-job config for the test jobs (development only).

```yaml
dummy_success:
  source_duration: 20   # seconds to run on source
  target_duration: 20   # seconds to run on target

dummy_fail:
  source_duration: 10
  target_duration: 10
  fail_at: 12           # elapsed seconds at which to fail
```

## `folder_sync`

Folders to sync via rsync-over-SSH, running as root on both ends to preserve ownership, permissions, ACLs, and xattrs.

```yaml
folder_sync:
  folders:
    - path: /home
      enabled: true
      filter_file: ~/.config/pc-switcher/home.filter
    - path: /root
      enabled: true
      filter_file: ~/.config/pc-switcher/root.filter
```

`path` must be absolute: it is handed to rsync verbatim, with no `~` or environment-variable expansion (unlike `filter_file`), so a relative path would resolve against each side's working directory. A relative or `~`-prefixed path aborts the sync during config validation.

Each folder is mirrored to the target, minus the paths its filter rules exclude. Set `enabled: false` to skip a folder. A filtered-out file is left untouched on the target if it already exists there, so machine-specific files (SSH keys, Tailscale config) can stay independent on each machine.

### Filter rules

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

### Keep a subfolder inside an excluded one

To drop most of a directory but keep selected children — for example drop `~/.cache` but keep the dev-tool caches so offline work doesn't re-download them — re-include the children before excluding the rest:

```text
+ .cache/
+ .cache/uv/***
+ .cache/pip/***
- .cache/*
```

The `/***` (not `/**`) matters here: it re-includes the `uv` and `pip` directory entries themselves, so the following `- .cache/*` can't drop them and rsync descends to copy their contents. The shipped `home.filter` already does this; add more `+ .cache/<tool>/***` lines for other caches you want to keep.

### `authorized_keys`

pc-switcher connects to the target **as the normal login user** (root SSH login is never used — only the `rsync` binary is elevated with `sudo` on each end; see ADR-013), authenticating with that user's SSH identity. The target therefore checks the connection against **that user's** `~/.ssh/authorized_keys`, which must contain the source machine's public key.

Because `~/.ssh/authorized_keys` is under a synced folder, a full mirror overwrites the target's copy with the source's. If the source's copy does not list the source machine's own public key, the mirror deletes the key the target was using to authorize the sync — and the **next** sync's SSH connection is rejected. The sync locks itself out of its own target.

Pick one of two approaches:

1. **Exclude it (safest default).** Each machine keeps its own independent access list. The shipped `home.filter` and `root.filter` do this:

   ```text
   - .ssh/authorized_keys
   ```

2. **Sync it as one shared fleet access list.** Only safe if the file lists **every** pc-switcher machine's public key — including the machine's own — so a mirror can never drop the key the next sync needs. Since the file is identical everywhere once synced, it must be the union of all machines' keys.

   To set this up, before the first sync build that union in `~/.ssh/authorized_keys` on one machine, then let the sync propagate it. On each machine the key to add is the public half of the SSH identity pc-switcher connects with (the one your agent offers, e.g. `~/.ssh/id_ed25519.pub`):

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

### Coming from .gitignore

The syntax resembles `.gitignore`, with three differences worth knowing:

- **Signs, not `!`** — `- pattern` excludes, `+ pattern` re-includes.
- **First match wins** (the opposite of gitignore) — order rules from specific to general.
- **Only a leading `/` anchors** — a middle slash does not, so for `/home` keep patterns unanchored to match under every user's home.

Otherwise it matches gitignore (basenames, a trailing `/` for directories, `*`/`**`/`[…]`), and a `.pcswitcher-filter` behaves like a committed `.gitignore`.

### Always excluded

Several groups are excluded from the mirror and cannot be re-included by any filter rule. Two are unconditional; two more are conditional on a package job being enabled:
- pc-switcher's own runtime state — `~/.local/share/pc-switcher/` (lock file, sync history, logs) — so a sync never disturbs the target's sync state or per-machine logs (ADR-017); its install itself (uv tool venv and `~/.local/bin` shim) mirrors like any other file, so it stays consistent with the interpreter it depends on.
- pc-switcher's machine-specific package decision files — `~/.config/pc-switcher/*.decisions.yaml` (one file per package manager) — excluded unconditionally, regardless of which package jobs are enabled, so a machine-specific package list is never accidentally pushed to a peer (D-09; see [Package Sync](#package-sync) below).
- If `vscode_state_sync` is enabled, the VS Code state DBs (`state.vscdb` and `state.vscdb.backup` for Code, Antigravity, Cursor, VSCodium, plus the install-shared `~/.vscode-shared/sharedStorage/`) — these are handed to `vscode_state_sync`, which merges them selectively so machine-bound account rows are never clobbered (ADR-018; see [`vscode_state_sync`](#vscode_state_sync) below).
- If `snap_sync` is enabled, every `~/snap/<app>/<revision>` directory (never `common` or `current`) — `snap_sync` itself converges these via `snap install`/`snap refresh --revision`, so folder_sync stops mirroring the ones it manages (see [Package Sync](#package-sync) below).
- If `flatpak_sync` is enabled, `~/.local/share/flatpak` (never `~/.var/app`, which stays folder_sync's territory) — `flatpak_sync` itself provisions this store (see [Package Sync](#package-sync) below).

Enabling `snap_sync` or `flatpak_sync` supplies its exclusion automatically; any hand-written filter rule for those paths in a personal filter file can be deleted once the corresponding job is enabled.

## `vscode_state_sync`

Selectively syncs each VS Code-based editor's (VS Code and its forks) global `state.vscdb` — the SQLite database under `~/.config/<Editor>/User/globalStorage/` — and the install-shared DB under `~/.vscode-shared/sharedStorage/`. These mix wanted global state (settings-adjacent values, MRU lists) with VS Code SecretStorage session blobs. The secret blobs live under `secret://` keys and are encrypted with a per-machine OS-keyring key that is never synced, so a plain file mirror would clobber the target's own decryptable secrets and force auth-backed extensions (GitHub, database extensions) to re-login after every sync. `folder_sync` excludes these DBs from its mirror (non-overridably); this job rebuilds each one instead.

```yaml
vscode_state_sync: true          # enable in sync_jobs (default true)
```

The job mirrors every key from the source **except** the machine-bound account keys, whose value the **target keeps**. Two kinds are preserved: the `secret://` SecretStorage blobs, and the auth session preferences that point into them (`userDataSyncAccountPreference` and the per-extension `<extensionId>-<providerId>-<scopes>` keys). The pointers matter as much as the blobs — their value is a session id regenerated on every sign-in, so mirroring one hands the target a session it does not hold, and VS Code asks you to sign in again. Non-matched keys take the source's value; keys present only on the target and not matched are dropped (the same fidelity as the `folder_sync` `--delete` mirror).

The preserved set is deliberately narrow: sibling keys holding the account *name* keep syncing (they are the same on every machine), and an extension you authenticate for the first time will not be covered until its key is added — it then asks for sign-in on the target once, rather than something you wanted synced silently going stale.

`~/.config/Code/machineid` is **not** carved out. pc-switcher already mirrors Settings Sync's own bookkeeping, so keeping the machine id common makes the fleet one logical Settings Sync machine, which is self-consistent; splitting it would leave two identities sharing one machine's sync state. Consequence: the Settings Sync machine list shows a single entry for the fleet.

The job has no settings: the editor list, DB layout, and the preserved-key patterns are VS Code internals owned by the module, not things a user configures. Enable or disable it via `sync_jobs` like any other job.

Covered VS Code-based editors: Code, Antigravity, Cursor, VSCodium. Also covered is `~/.vscode-shared/sharedStorage/state.vscdb`, an install-shared state DB outside any editor's config directory (recent VS Code versions) holding cross-install state such as the workspace-trust list and recently-opened paths; it has the same schema and is merged the same way. For each covered DB, both the main `state.vscdb` and its `state.vscdb.backup` sidecar are handled identically — the exact set `folder_sync` excludes is the exact set this job merges, so no file is hidden from the mirror without being merged. A file absent on the source is skipped. On a first sync (the target has no such DB yet) the target simply receives the secret-stripped database, causing a one-time re-login. The job runs after `folder_sync`, as the invoking normal user (no `sudo`), and needs `sqlite3` on both machines.

Scope: this covers **only the invoking user** — whoever runs `pc-switcher`. If a synced `/home` contains other users, their VS Code state DBs are excluded from the mirror (so their secrets are never clobbered) but are not merged, so their VS Code global state does not sync. Multi-user coverage would require running the merge as root and is not currently supported.

## Package Sync

Three jobs replicate *what is installed* — apt packages plus the repository state they depend on, snaps, and flatpaks:

```yaml
sync_jobs:
  apt_sync: false        # Install apt packages missing on the target, after a batched review
  snap_sync: false        # Converge installed snaps to the source's revision/channel, after a batched review
  flatpak_sync: false     # Converge installed flatpak refs and remotes, after a batched review
```

All three ship **disabled** — enabling any of them lets pc-switcher change installed software on the target, so it is opt-in. Each has its own top-level config section (`apt_sync:`, `snap_sync:`, `flatpak_sync:`), currently empty — there are no tunable keys yet, only the enable flag above.

All three run **before** `folder_sync`, so applications exist on the target before their data lands on top of them — decisive for flatpak, where `flatpak install` must create `~/.local/share/flatpak` before folder_sync would otherwise place `~/.var/app` content there.

### What each job covers

- **`apt_sync`** — the manually-installed apt package set (`apt-mark showmanual`, not the full dpkg selection — apt resolves dependencies on the target itself), plus the repository state that governs where packages come from: sources under `/etc/apt/sources.list.d`, signing keys (`/etc/apt/keyrings`, legacy `/etc/apt/trusted.gpg.d`), pins (`/etc/apt/preferences.d`) and apt config (`/etc/apt/apt.conf.d`).
- **`snap_sync`** — installed snaps, converged to the source's exact revision and tracking channel.
- **`flatpak_sync`** — installed flatpak refs and their remotes, per user/system installation scope.

### The batched review

Before anything is changed on any manager, pc-switcher shows **one review** covering every enabled package job at once — not one review per manager. If you enable all three, one screen answers for apt, snap and flatpak together. The review appears before any change and shows every difference, grouped by manager and by action.

Installs and removals are always separate groups: a group proposing to remove software is never mixed into a group proposing to install it, and its title names the removal explicitly (e.g. "Remove packages"), never the word "apply". Removal entries start **unticked**, so a bulk tick can never silently delete something. Every item offers a three-way choice: apply it, skip it for this run only, or skip it always (making it machine-specific — see below).

For apt specifically, the review shows more than the packages you named: apt may need to remove or change other packages to satisfy a request (a dependency resolution side effect), and those collateral changes are shown in the review as their own entries before you decide anything. If an approved item's real transaction would end up doing more than what was reviewed, pc-switcher refuses to run it and reports the refusal rather than executing a transaction nobody saw.

### Machine-specific packages

Some packages belong to one machine only — a hardware driver, a vendor tool tied to a peripheral. Marking a package **machine-specific** (choosing "skip always" in the review) is the way to tell pc-switcher: *don't touch that package on that machine*. This is deliberately not called an "exclusion" — the file records what belongs to this machine, not what to hide from it.

Marking an item machine-specific writes an entry to that machine's own decision file at `~/.config/pc-switcher/<manager>.decisions.yaml` (one file per manager, e.g. `apt.decisions.yaml`, `snap.decisions.yaml`, `flatpak.decisions.yaml`). This file is **never synced** — it stays local to the machine it describes, in both roles: the marked item is never pushed from this machine when it is the source, and never installed or removed on this machine when it is the target. An annotated example lives at [`src/pcswitcher/machine-packages.example.yaml`](../src/pcswitcher/machine-packages.example.yaml).

To un-mark something, delete its entry from the decision file (or delete the whole file to clear every machine-specific decision for that manager). The next sync treats the item as live again.

### Install snippets

Some installed things no package manager can reproduce — a bare `.deb` someone downloaded and installed by hand, or a manual install under `/usr/local` or `/opt`. pc-switcher detects these (apt packages with no repository candidate, and files under `/usr/local`/`/opt` that no package owns) and surfaces them in the review as items needing a resolution. For each one, the review offers three choices: add an install snippet, mark it machine-specific (see above), or skip for now.

An install snippet is a shell command that reproduces the item — the tool never parses, interprets, or reasons about it. It is **stored and replayed verbatim**, and it must run **non-interactively**: no stdin is supplied during replay, so a command that prompts (e.g. a debconf question) hangs the sync rather than failing cleanly. A typical shape:

```bash
sudo DEBIAN_FRONTEND=noninteractive dpkg -i /path/to/package.deb || \
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -f
```

Unlike the decision files above, the snippet registry (`~/.config/pc-switcher/package-snippets.yaml`) **is synced** — it travels alongside `config.yaml` during config sync, with its own diff-and-confirm prompt when the two machines' registries differ. This is because how to install something is knowledge about the *package*, not about the machine: a snippet authored on one machine should be usable to reproduce the same item on any peer.

Resolving an unreproducible item is mandatory, not optional: while any such item is left with neither a snippet nor a machine-specific mark, the package job reports the run as a **failure** at the end, every time, even if everything it did apply succeeded. This is deliberate — it is how the tool keeps asking rather than letting the gap quietly disappear.

### Versions

pc-switcher installs packages **by name** and lets versions float to whatever each machine's own repositories currently offer — it never pins the source's exact version. A version difference between source and target is detected and reported in the review, never silently forced. Deliberate version pinning does replicate, because `/etc/apt/preferences.d` pin files are synced as items like any other apt state.

### Non-interactive runs

A sync run without a TTY (e.g. from a script or CI) changes **nothing**: every item that needed a decision is treated as skipped for this run only, nothing is recorded permanently (no machine-specific marks, no snippets), and everything left unresolved is reported. Re-run interactively to actually apply or resolve anything.

### Prerequisites: passwordless sudo

Each enabled package job needs passwordless sudo for a handful of binaries — `apt_sync` on both source (to read `/etc/apt` state) and target (to install packages and write `/etc/apt` state); `snap_sync` and `flatpak_sync` on the target only, and only when the diff involves a system-scope item. `validate()` checks this before anything runs and, on failure, prints the exact `visudo` command and sudoers line to add. The binaries it names are a **lower bound** on what must be permitted, not an exact scope to lock the grant down to — a broader existing grant is fine.
