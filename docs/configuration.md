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
      excludes:
        - .ssh/id_*
        - .cache/nvidia
    - path: /root
      enabled: true
      excludes:
        - .ssh/id_*
```

Each folder is a full mirror (`rsync --delete`): **everything under `path` is synced except the paths matched by that folder's `excludes`.** There is no include mechanism — a folder is synced in its entirety minus its excludes; there is no way to sync only selected sub-paths of a folder. To narrow what is synced, add a folder with a deeper `path` or add excludes.

Setting `enabled: false` skips a folder. Excluded files are never deleted on the target (`--delete-excluded` is deliberately not used), so a machine-specific file such as `.ssh/id_ed25519` survives on both ends.

### Exclude pattern syntax

Each entry in `excludes` is an rsync filter pattern, applied relative to the folder's transfer root (for `path: /home`, the root is the *contents* of `/home`, so `.ssh/id_*` matches `<user>/.ssh/id_*` for every user). Patterns are evaluated top-to-bottom and the first matching rule wins.

Key rules:

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

### Always-excluded runtime files

pc-switcher's own runtime files are excluded automatically, before any user rule, and **cannot** be re-included (ADR-016). These are the only hardcoded excludes:

- `~/.local/share/pc-switcher/` — sync state, lock file, logs
- `~/.local/share/uv/tools/pcswitcher` — the uv-tool install
- `~/.local/bin/pc-switcher` — the entry-point shim

Mirroring these would clobber the target's own sync-history state mid-sync or overwrite the running install, so they stay machine-local regardless of config.
