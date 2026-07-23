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

Enables or disables optional sync jobs. Only job names listed here are discovered and run; unknown names fail validation. This flag is the only configuration the package jobs take; what each job does lives in its own document under [`jobs/`](jobs/).

```yaml
sync_jobs:
  dummy_success: true          # test job that completes successfully
  dummy_fail: false            # test job that fails at a configurable time
  folder_sync: true            # sync /home and /root via rsync — see jobs/folder-sync.md
  vscode_state_sync: true      # selective, SQLite-aware sync of VS Code state.vscdb — see jobs/vscode-state-sync.md
  apt_sync: false              # apt packages plus /etc/apt repository state — see jobs/package-sync.md
  snap_sync: false             # installed snaps, converged to the source's revision/channel — see jobs/package-sync.md
  flatpak_sync: false          # installed flatpak refs and remotes — see jobs/package-sync.md
  manual_installs_sync: false  # things no package manager can reproduce, plus the snippet registry — see jobs/package-sync.md
```

The package jobs (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync`) ship disabled: enabling any of them lets pc-switcher change installed software on the target, so it is opt-in. They have no per-job config sections — only the enable flags above. See [Package Sync](jobs/package-sync.md) for what they do.

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

Folders to sync via rsync-over-SSH. Each folder takes an absolute `path`, an `enabled` flag, and an optional `filter_file`.

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

`path` must be absolute: it is handed to rsync verbatim, with no `~` or environment-variable expansion (unlike `filter_file`), so a relative path would resolve against each side's working directory. A relative or `~`-prefixed path aborts the sync during config validation. Set `enabled: false` to skip a folder. `filter_file` is optional; if it is set but the file is missing, the sync stops with an error.

The filter-file syntax, the `.pcswitcher-filter` per-directory files, the `authorized_keys` guidance and the always-excluded paths are described in [Folder Sync](jobs/folder-sync.md).

## `vscode_state_sync`

This job has no configuration beyond its `sync_jobs` enable flag: the editor list, DB layout, and the preserved-key patterns are VS Code internals owned by the module, not things a user configures. Enable or disable it via `sync_jobs` like any other job. What it does — the selective, SQLite-aware merge that preserves machine-bound secrets — is described in [VS Code state sync](jobs/vscode-state-sync.md).

## Package Sync

The four package jobs (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync`) take no configuration beyond their `sync_jobs` enable flags, shown under [`sync_jobs`](#sync_jobs) above; there are no per-job config sections. What they do — the item -> diff -> review -> converge model, the per-manager batched review, machine-specific packages, install snippets and version handling — is described in [Package Sync](jobs/package-sync.md).
