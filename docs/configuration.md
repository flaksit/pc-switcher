# Configuration Reference

Complete reference for `~/.config/pc-switcher/config.yaml`. Run `pc-switcher init` to write the annotated default file (`src/pcswitcher/default-config.yaml`), then edit it.

The config is validated against a JSON Schema on load (`src/pcswitcher/schemas/config-schema.yaml`). Unknown top-level keys and unknown job names are rejected. Both source and target run with the same config: it is copied to the target during sync (step 10 of the sync sequence).

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
|-------|-------|-------------|
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
  dummy_success: true   # test job that completes successfully
  dummy_fail: false     # test job that fails at a configurable time
  folder_sync: true     # sync /home and /root via rsync
```

Future jobs (`packages`, `docker`, `vms`, `k3s`) are added here as they are implemented.

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

Each folder mirrors `path` (`rsync --delete`) minus the paths matched by its filter rules. Include-override is possible: you can exclude a subtree and re-include selected children of it (e.g. drop all of `~/.cache` but keep `~/.cache/uv`), because pc-switcher passes native rsync filter syntax straight through.

Setting `enabled: false` skips a folder. Excluded files are never deleted on the target (`--delete-excluded` is deliberately not used), so a machine-specific file such as `.ssh/id_ed25519` survives on both ends.

### Filter rules

Two authoring surfaces are available per folder, both native rsync filter syntax (`+ pattern` include, `- pattern` exclude, first-match-wins):

- **`filter_file`** — a per-folder central filter file, spliced in via rsync `merge`. `pc-switcher init` ships `home.filter` and `root.filter` next to `config.yaml`; edit them directly.
- **`.pcswitcher-filter`** — a per-directory file, spliced in tree-wide via rsync `dir-merge`. Drop one anywhere in the synced tree and it takes effect for that subtree, inheriting into deeper directories. `.pcswitcher-filter` files themselves sync to the target, like a committed `.gitignore`. This is pc-switcher's own filename — it is **not** rsync's built-in `.rsync-filter`, which pc-switcher never activates (no `-F`/`-FF`/`-C`/`--cvs-exclude` are ever passed, so a stray `.rsync-filter` in a synced tree has no effect).

Precedence is **GLOBAL-FIRST**: pc-switcher's own runtime-file excludes (below) are evaluated first and can never be overridden; the folder's central `filter_file` is evaluated next, before any `.pcswitcher-filter`. Because rsync is first-match-wins, a central `-` rule always beats a later per-directory `+` rule — central policy is authoritative and a per-directory file can never re-expose something the central filter excluded.

A configured `filter_file` that does not exist on the source fails validation before any transfer runs (fail-fast) — a missing filter file never silently degrades to a full unfiltered mirror.

On a `--delete` mirror an exclude rule also protects a matching file that already exists on the target from being deleted or overwritten (rsync never deletes an excluded path — `--delete-excluded` is deliberately not used). This holds for both surfaces, but the two get there differently. The central `filter_file` is passed to rsync on every run, so its protection is immediate and unconditional. A per-directory `.pcswitcher-filter` is read by rsync from whichever side it is scanning, so it can only protect the target once the file is present *on the target* — and rsync's `--delete` would otherwise delete (not protect) a target file that a source-side rule names. pc-switcher closes that gap: before the `--delete` mirror it runs a preliminary pass that copies every `.pcswitcher-filter` onto the target (transferring nothing else), so per-directory rules protect the target on the same sync, including the first sync and whenever you add a new `.pcswitcher-filter`. One consequence: a `--dry-run` preview is taken without that pre-seed (dry-run writes nothing), so it can *over*-report deletions for per-directory-protected paths — the real sync deletes fewer files than the preview shows, never more. For rules that must never delete or expose something (secrets, machine-specific state), the central `filter_file` remains the authoritative place under GLOBAL-FIRST.

Core syntax, applied relative to the folder's transfer root (for `path: /home`, the root is the *contents* of `/home`, so `.ssh/id_*` matches `<user>/.ssh/id_*` for every user):

- A pattern **containing a `/`** (other than a trailing one) or `**` is matched against the full path; a pattern with **no `/`** is matched against the final path component only. So `nvidia` excludes any file or directory named `nvidia` at any depth, while `.cache/nvidia` matches the two-element path ending.
- A pattern **without a leading `/`** floats — it matches at the *end* of the path, i.e. at any depth. `.cache/nvidia` therefore matches both `.cache/nvidia` and `alice/.cache/nvidia`.
- A pattern **with a leading `/`** is anchored to the transfer root. `/lost+found` matches only at the top of the synced folder.
- A **trailing `/`** restricts the match to directories: `foo/` excludes only a directory named `foo`.
- Wildcards: `?` matches one non-slash character; `*` matches zero or more non-slash characters (stops at `/`); `**` matches across `/`; `[…]` is a character class; a trailing `/***` matches a directory and all its contents.

Examples:

| Pattern | Matches |
|---------|---------|
| `.ssh/id_*` | `id_*` files inside any `.ssh` directory at any depth |
| `.config/tailscale` | a `tailscale` entry inside any `.config` directory |
| `.cache/nvidia` | a `nvidia` entry inside any `.cache` directory |
| `/lost+found` | `lost+found` only at the folder root |
| `*.tmp` | any file ending in `.tmp` at any depth |
| `node_modules/` | any directory named `node_modules` |

For the full specification (all wildcard forms, anchoring, merge files, modifiers), see the rsync manpage "FILTER RULES" / "INCLUDE/EXCLUDE PATTERN RULES" section: https://download.samba.org/pub/rsync/rsync.1

### Coming from .gitignore

rsync's filter rules resemble `.gitignore` but differ in a few load-bearing ways:

- **Signs, not `!`**: rsync uses `+`/`-` prefixes instead of gitignore's bare pattern / `!`-negated pattern. A `.pcswitcher-filter` with only `-` lines behaves like an ordinary `.gitignore`.
- **First-match-wins, the opposite of gitignore's last-match-wins.** In rsync, the *first* rule that matches a path decides it; later rules never override an earlier match. In gitignore, later lines override earlier ones.
- **A leading `/` anchors to the transfer root; a middle slash does not anchor.** `/a/direct` matches only the root's `a/direct`, while `a/direct` (slash present but not leading) matches `a/direct` at *any* depth. This differs from gitignore, where a middle slash anchors the pattern to the `.gitignore`'s own directory.
- **A trailing `/` means directory-only** — same as gitignore.
- **A no-slash pattern matches the final path component at any depth** — like a gitignore basename pattern (e.g. `nvidia` matches both `.cache/nvidia` and `alice/.cache/nvidia`).
- **`a/**/b` does not match `a/b`.** `**` requires at least the slash-structure it spans, so the zero-intermediate-directory case is not covered; use `a/b` (or `a/**b`) if you also need that case.
- In `dir-merge /.pcswitcher-filter`, the leading `/` only means the filename has no path component (it is a bare name) — it does **not** restrict the file to the root. The dir-merge rule is honored in *any* directory of the tree, exactly like `.gitignore` is honored in any directory that has one.

### The ancestor-descent idiom

To re-include a subtree (e.g. `~/.cache/uv`) while dropping the rest of its parent (`~/.cache`), rsync must be able to *descend into* the parent first, so list the ancestor, the leaf, and the trailing exclude together, in that order:

```
+ /.cache/
+ /.cache/uv/***
- /.cache/*
```

Patterns anchor to the transfer root. Because pc-switcher syncs `/home` with the folder's *contents* as the transfer root, each user's home sits one level below that root (`alice/.cache/uv`, not `/.cache/uv`), so the shipped `home.filter` uses **floating** (non-leading-slash) patterns — the leading-slash form above is the transfer-root-relative template; drop the leading `/` (`+ .cache/`, `+ .cache/uv/***`, `- .cache/*`) when editing a filter file meant to apply under `/home`, and keep it when the transfer root itself is the directory you mean (e.g. a `path:` that is already the user's own home).

### Always-excluded runtime files

pc-switcher's own runtime files are excluded automatically, before any user rule, and **cannot** be re-included (ADR-016). These are the only hardcoded excludes:

- `~/.local/share/pc-switcher/` — sync state, lock file, logs
- `~/.local/share/uv/tools/pcswitcher` — the uv-tool install
- `~/.local/bin/pc-switcher` — the entry-point shim

Mirroring these would clobber the target's own sync-history state mid-sync or overwrite the running install, so they stay machine-local regardless of config.
