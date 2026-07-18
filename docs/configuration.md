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

Each folder is mirrored to the target, minus the paths its filter rules exclude. Set `enabled: false` to skip a folder. A filtered-out file is left untouched on the target if it already exists there, so machine-specific files (SSH keys, Tailscale config) can stay independent on each machine.

### Filter rules

Filter rules decide what is and isn't synced. They live in two kinds of file:

- **`filter_file`** — the folder's main rule list. Optional: omit it for a folder that needs no central rule list, and only the runtime excludes and any `.pcswitcher-filter` files apply. `pc-switcher init` writes `home.filter` and `root.filter` next to `config.yaml`; edit them to taste. If a folder *does* configure a `filter_file` but the file is missing, the sync stops with an error.
- **`.pcswitcher-filter`** — a per-directory rule file that works like `.gitignore`: drop one into any directory and its rules apply to that directory and everything below. These files sync to the target too.

Each rule is `- pattern` to exclude or `+ pattern` to re-include. The **first** matching rule wins, so put a `+` re-include before the broader `-` it carves out of. If a path is matched in both a `filter_file` and a `.pcswitcher-filter`, the `filter_file` wins. This allows for central policy that can not be overridden by a per-directory file.

Pattern syntax:

- names match in full, never as substrings: each wildcard-free component must match a whole file or directory name — `foo` matches an entry named exactly `foo`, not `fool` or `afoo`. Use wildcards for partial names (`foo*`, `*foo*`).
- a pattern with no `/` is matched against the final path component (a single file or directory name); a pattern with a `/` is matched against the whole path below the folder root
- a leading `/` anchors to the folder root; without it a pattern matches at any depth
- a trailing `/` matches directories only; without it a pattern matches either a file or a directory of that name
- `?` matches one character, `*` matches within a path segment, `**` matches across `/`, and `[…]` is a character class
- `dir/**` matches a directory's contents but **not** the directory entry itself; `dir/***` matches the directory *and* everything in it. Reach for `/***` when a later exclude (like `- .cache/*`) would otherwise drop the directory and stop rsync from descending into it

| Pattern | Matches |
|---------|---------|
| `.ssh/id_*` | `id_*` inside any `.ssh` directory |
| `.config/tailscale` | a `tailscale` file or directory inside any `.config` |
| `/lost+found` | `lost+found` only at the folder root |
| `*.tmp` | any `.tmp` file, at any depth |
| `node_modules/` | any directory named `node_modules` |

Full pattern reference: rsync's manpage, "FILTER RULES" section — https://download.samba.org/pub/rsync/rsync.1

### Keep a subfolder inside an excluded one

To drop most of a directory but keep selected children — for example drop `~/.cache` but keep the dev-tool caches so offline work doesn't re-download them — re-include the children before excluding the rest:

```
+ .cache/
+ .cache/uv/***
+ .cache/pip/***
- .cache/*
```

The `/***` (not `/**`) matters here: it re-includes the `uv` and `pip` directory entries themselves, so the following `- .cache/*` can't drop them and rsync descends to copy their contents. The shipped `home.filter` already does this; add more `+ .cache/<tool>/***` lines for other caches you want to keep.

### Coming from .gitignore

The syntax resembles `.gitignore`, with three differences worth knowing:

- **Signs, not `!`** — `- pattern` excludes, `+ pattern` re-includes.
- **First match wins** (the opposite of gitignore) — order rules from specific to general.
- **Only a leading `/` anchors** — a middle slash does not, so for `/home` keep patterns unanchored to match under every user's home.

Otherwise it matches gitignore (basenames, a trailing `/` for directories, `*`/`**`/`[…]`), and a `.pcswitcher-filter` behaves like a committed `.gitignore`.

### Always excluded

pc-switcher's own files are always excluded and cannot be re-included, so a sync never disturbs the target's sync state or the running install: `~/.local/share/pc-switcher/`, `~/.local/share/uv/tools/pcswitcher`, and `~/.local/bin/pc-switcher`.
