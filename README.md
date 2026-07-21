# PC-switcher

A synchronization system for seamless switching between Linux desktop machines. Keep your laptops and workstations in sync with near-complete system-state replication.

## Overview

PC-switcher enables a simple workflow: work on one machine, sync before switching, resume on the other—without manual file management or cloud sync overhead.

```plain
Work on source machine → Trigger sync → Resume on target machine
```

**Status**: Core infrastructure complete. Core sync functionality in development.

## What Gets Synced

- **User data**: `/home`, `/root` with all documents, code, configs, and selective caches
- **Packages**: apt, snap, flatpak, PPAs, manual installs
- **Application configurations**: GNOME, cloud mounts, systemd services
- **System configurations**: Machine-independent `/etc` files, users, groups
- **File metadata**: Owner, permissions, ACLs, timestamps
- **Containers & VMs**: Docker images/containers/volumes, KVM/virt-manager VMs
- **k3s**: Local single-node cluster state and PVCs

**Never synced**: SSH keys, Tailscale config, GPU/hardware caches, machine-specific packages

## Installation

Install using the installation script:
```bash
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash
```

To install a specific version:
```bash
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | VERSION=0.2.0 bash
```

Test installation:
```bash
pc-switcher --help
pc-switcher --version
```

**Uninstalling**:
```bash
uv tool uninstall pcswitcher
```
(Note: Use the Python package name `pcswitcher`, not the command name `pc-switcher`)

After installation, create the default configuration:
```bash
pc-switcher init
```

## Quick Start

Before syncing, ensure:
- Target machine is powered on and reachable via SSH (LAN, VPN, etc.)
- You're logged out from the desktop (or close all apps)

Trigger a sync:
```bash
pc-switcher sync <target-hostname>
```

Monitor sync progress with:
```bash
pc-switcher logs
```

After sync completes, power off the source machine and resume work on target.

## What Happens During a Sync

`pc-switcher sync <target>` runs a fixed sequence of steps. The order matters: each step sets up the environment the next one depends on. All steps run on the **source** machine, acting on the **target** over SSH.

The sequence stops at the first failure, and cleanup always runs: release locks, kill remote processes, close the connection.

The twelve steps are listed below. The live UI expands step 10 (run jobs) to one entry per enabled sync job, so the on-screen "Step X/Y" count runs higher than twelve. With the default two jobs (`folder_sync`, then `vscode_state_sync`), a sync runs as 13 UI steps, ending by recording sync history.

1. **Acquire source lock.** Local lock file; this machine cannot join any other sync (as source or target) while this one runs.
2. **Establish SSH connection.** Creates the local and remote executors every later step uses. Nothing touches the target before this point.
3. **Acquire target lock.** A persistent remote process holds the same unified lock on the target; released during cleanup.
4. **Out-of-order / target-state check.** Runs after the target lock so it can read the target's sync-history over SSH. Detects cases where the target may hold independent state — no prior sync history, the target last synced with a different machine, or this machine pushing again without a back-sync first. Warns and asks for confirmation (never a hard abort, since re-syncing the same direction is a legitimate workflow). Skip with `--allow-out-of-order`; in `--dry-run` the warning is logged and the sync continues.
5. **Discover & validate jobs.**
   - Load enabled jobs from config
   - Validate their config
   - Run each job's `validate()` against live system state: checks `sudo rsync` availability, `acl` package installation, and source folder existence. Nothing has been mutated yet.
6. **Disk-space preflight.** Check free space on both hosts in parallel against `preflight_minimum`; abort if either is short — so snapshots and rsync don't run a disk into ENOSPC.
7. **Pre-sync snapshots.** Create btrfs snapshots on both hosts. This is the rollback point; every mutating step below happens after it.
8. **Install/upgrade pc-switcher on target.** Ensures the target has a compatible version to run its side of later jobs. After snapshots, so a bad install is recoverable.
9. **Sync config to target.** Copy this machine's config to the target (prompting on diff unless `--yes`), so both ends run jobs with identical settings.
10. **Run sync jobs sequentially.** The actual data movement, one UI step per enabled job. By default `folder_sync` (rsync-over-SSH as root on both ends) runs first, then `vscode_state_sync` (a selective, SQLite-aware merge of each editor's `state.vscdb` that keeps the target's machine-bound `secret://` keys). A background disk-space monitor runs concurrently and aborts the sync if free space crosses `runtime_minimum`. First job failure stops the run.
11. **Post-sync snapshots.** Snapshot both hosts again, capturing the synced state.
12. **Record sync history.** Write the sync-history record on both machines (source: `last_role=SOURCE`, target: `last_role=TARGET`), enabling step 4's out-of-order check next time. The write is skipped in `--dry-run`, but the step still runs. This is the last step — Step 13 in the UI with the default job set.

With `--dry-run`, the workflow previews without writing state (no history update, no snapshots, no mutations). rsync `--dry-run` lists the exact files and deletions that would occur; deletions are recorded in the FULL-level log so you can audit what would be destroyed before committing to a live sync. `--allow-out-of-order` skips the out-of-order / target-state confirmation.

## Configuration

Run `pc-switcher init` to write the default configuration to `~/.config/pc-switcher/config.yaml`. The generated file is annotated with inline comments for every setting.

Top-level sections:

- `logging` — per-destination log-level floors (file, terminal, third-party libraries)
- `sync_jobs` — which sync jobs are enabled
- `disk_space_monitor` — free-space thresholds checked before and during a sync
- `btrfs_snapshots` — subvolumes to snapshot and retention policy
- `folder_sync` — folders to mirror via rsync, filtered by a per-folder filter file (native rsync `+`/`-` rules) plus optional per-directory `.pcswitcher-filter` files. Filter rules can exclude a subtree and re-include selected children (e.g. drop `~/.cache` but keep `~/.cache/uv`)
- `vscode_state_sync` — SQLite-aware selective sync of each editor's `state.vscdb`, preserving the target's machine-bound `secret://` keys (no settings; enable/disable only)

See the **[Configuration Reference](docs/configuration.md)** for every option, defaults, the folder-sync filter-rule syntax, and a "coming from `.gitignore`" guide.

## Available Commands

```bash
# Initialize configuration file
pc-switcher init [--force]    # Create default config at ~/.config/pc-switcher/config.yaml

# Sync to target machine
pc-switcher sync <target-hostname> [--config PATH]

# View logs
pc-switcher logs              # Show logs directory and list recent logs
pc-switcher logs --last       # Show path to most recent log file

# Clean up old snapshots
pc-switcher cleanup-snapshots --older-than 7d [--dry-run]

# Self-update pc-switcher
pc-switcher self update [VERSION] [--prerelease]

# Skip the startup version check (applies to any command, e.g. pc-switcher --no-version-check sync <target>)
pc-switcher --no-version-check <command>
```

### Startup version check

In an interactive terminal, pc-switcher checks for a newer release and offers to upgrade before running your command. Disable with `--no-version-check` or `PCSWITCHER_SKIP_VERSION_CHECK`.

## Requirements

- Ubuntu 24.04 LTS on all machines
- Single btrfs filesystem (all synced data on one filesystem per machine)
- SSH access between machines (LAN, VPN such as Tailscale, or other network)
- Only one machine actively used at a time

## Key Design Principles

1. **Reliability**: No data loss, conflict detection, consistent state, full auditability
2. **Smooth UX**: Single command to launch entire sync; minimal manual intervention
3. **Use standard tools**: Well-supported, maintainable approach
4. **Minimize disk wear**: NVMe SSDs—avoid unnecessary writes
5. **Simplicity**: Easy to understand, modify, and maintain

## Troubleshooting

### GitHub API Rate Limits

When running `pc-switcher --version`, `self update`, sync (which installs pc-switcher on target), or the startup version check (runs on every command; see above), you may see rate limit errors like:

```text
RuntimeError: Failed to fetch GitHub releases: 403 {"message": "API rate limit exceeded..."}
```

This happens because pc-switcher queries the GitHub API to check for releases. Unauthenticated requests are limited to 60/hour.

**Solution**: Add a GitHub personal access token with public read-only permissions to your `~/.profile` on both source and target machines:

```bash
echo 'export GITHUB_TOKEN=ghp_your_token_here' >> ~/.profile
source ~/.profile
```

With a token, the rate limit increases to 5,000 requests/hour.

## Documentation

See [docs/README.md](docs/README.md) for the documentation index.

Key documents:
- **[High level requirements](docs/planning/High%20level%20requirements.md)** - Project vision, scope, workflow
- **[Architecture](docs/system/architecture.md)** - System architecture and design
- **[Architecture Decision Records](docs/adr/_index.md)** - Design decisions and rationale
- **[Development Guide](docs/dev/development-guide.md)** - Development workflow and guidelines

## Development

Clone the repository:
```bash
git clone git@github.com:flaksit/pc-switcher.git
cd pc-switcher
```

Install dependencies:
```bash
uv sync
```

PR workflow:
- If a PR fixes an issue, include `Fixes #<issue>` (or `Closes` / `Resolves`) in the PR description/title so GitHub closes the issue on merge.
- See [docs/dev/development-guide.md](docs/dev/development-guide.md).

Install the tool from your local checkout (for testing):
```bash
./install.sh
```
This auto-detects the git workspace and runs `uv tool install .` from your local code.

If you want to install a specific version from GitHub, use one of the following commands:
```bash
# use the local script to install a specific package version from GitHub
VERSION=0.2.0 ./install.sh
./install.sh --ref abcd0123
./install.sh --ref my_feature_branch
# Use the install script from GitHub to install a specific package version
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash -s -- --ref my_feature_branch
```


Run code quality checks:
```bash
uv run ruff format .    # Format code
uv run ruff check .     # Lint
uv run basedpyright     # Type check
uv run pytest           # Run tests
```

This project uses **SpecKit**—a specification-driven workflow via custom slash commands:

```bash
/speckit.specify "feature description"  # Create feature spec
/speckit.clarify                        # Refine spec details
/speckit.plan                           # Generate design artifacts
/speckit.tasks                          # Create actionable tasks
/speckit.analyze                        # Check consistency specs-plan-tasks
/speckit.implement                      # Execute implementation
```

See [CLAUDE.md](CLAUDE.md) for complete development workflow details.
