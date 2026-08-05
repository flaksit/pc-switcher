# PC-switcher

A synchronization system for seamless switching between Linux desktop machines. Keep your laptops and workstations in sync with near-complete system-state replication.

## Overview

PC-switcher enables a simple workflow: work on one machine, sync before switching, resume on the other—without manual file management or cloud sync overhead.

```plain
Work on source machine → Sync → Resume on target machine
```

**Status**: Core infrastructure complete. Package sync (apt, snap, flatpak, hand-installed `.deb`s, manual installs) and folder sync are implemented and under active hardening. Application and system-configuration sync are on the roadmap.

## What Gets Synced

### Implemented

- **User data**: `/home`, `/root` with all documents, code, configs, and selective caches
- **Packages**: apt, snap, flatpak, PPAs, hand-installed `.deb`s, manual installs under `/usr/local` and `/opt`

### Roadmap

- **Application configurations**: GNOME, cloud mounts, systemd services
- **System configurations**: Machine-independent `/etc` files, users, groups
- **File metadata**: Owner, permissions, ACLs, timestamps
- **Containers & VMs**: Docker images/containers/volumes, KVM/virt-manager VMs
- **k3s**: Local single-node cluster state and PVCs

**Never synced**: SSH keys, Tailscale config, GPU/hardware caches, machine-specific packages

## Installation

The installation will install the [uv package manager](https://docs.astral.sh/uv/) if not already present.

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
pc-switcher sync <hostname>
```

Monitor sync progress with:
```bash
pc-switcher logs
```

After sync completes, power off the source machine and resume work on target.

## What Happens During a Sync

`pc-switcher sync <hostname>` runs a fixed sequence of steps. The order matters: each step sets up the environment the next one may depend on. All steps run on the **source** machine, acting on the **target** over SSH.

The sequence stops at the first failure, and cleanup always runs: release locks, kill remote processes, close the connection.

The twelve steps are listed below. Step 10 (run jobs) is a single logical step. When several jobs run, they are sub-labeled `10a`, `10b`, …

1. **Acquire source lock.** Local lock file; this machine cannot join any other sync (as source or target) while this one runs.
2. **Establish SSH connection.** Creates the local and remote executors every later step uses. Nothing touches the target before this point.
3. **Acquire target lock.** A persistent remote process holds the same unified lock on the target; released during cleanup.
4. **Out-of-order / target-state check.** Read the target's sync-history over SSH. Detects cases where the target may hold independent state — no prior sync history, the target last synced with a different machine, or this machine pushing again without a back-sync first. Warns and prompts for proceed or abort. Skip with `--allow-out-of-order`. In `--dry-run` the warning is logged and the sync continues.
5. **Discover & validate jobs.**
   - Load enabled jobs from config
   - Validate their config
   - Run each job's validation checks against live system state: are all prerequisites for that job met? Nothing has been mutated yet.
6. **Disk-space preflight.** Check free space on both hosts against `preflight_minimum`; abort if either is short.
7. **Pre-sync snapshots.** Create btrfs snapshots on both hosts. This is the rollback point; every mutating step below happens after it.
8. **Install/upgrade pc-switcher on target.** Ensures the target has a compatible version to back-sync later.
9. **Sync config to target.** Copy this machine's config to the target (prompting on diff unless `--yes`), so both ends run jobs and future back-sync with identical settings.
10. **Run sync jobs sequentially.** The actual data movement. Which jobs are run is defined in `config.yaml`. See [Configuration Reference](docs/configuration.md#package-sync). A background disk-space monitor runs concurrently and aborts the sync if free space crosses `runtime_minimum`. A job failure stops the run, except in a package job, where failed items or an unreadable package manager fail that job alone and the remaining jobs still run.
11. **Post-sync snapshots.** Snapshot both hosts again, capturing the synced state.
12. **Record sync history.** Write the sync-history record on both machines, enabling step 4's out-of-order check next time.

Non-blocking errors and warnings are logged but do not stop the sync. They are reported during the run and listed again at the end of the run.

With `--dry-run`, the workflow previews without writing state (no history update, no snapshots, no mutations). rsync `--dry-run` lists the exact files and deletions that would occur; deletions are recorded in the FULL-level log so you can audit what would be destroyed before committing to a live sync. `--allow-out-of-order` skips the out-of-order / target-state confirmation.

## Configuration

Run `pc-switcher init` to write the default configuration to `~/.config/pc-switcher/config.yaml`. The generated file is annotated with inline comments for every setting.

Top-level sections:

- `logging` — per-destination log-level floors (file, terminal, third-party libraries)
- `sync_jobs` — which sync jobs are enabled
- `disk_space_monitor` — free-space thresholds checked before and during a sync
- `btrfs_snapshots` — subvolumes to snapshot and retention policy
- `folder_sync` — folders to mirror via rsync, filtered by a per-folder filter file (native rsync `+`/`-` rules) plus optional per-directory `.pcswitcher-filter` files. Filter rules can exclude a subtree and re-include selected children (e.g. drop `~/.cache` but keep `~/.cache/uv`)

The **[Package Syncs](docs/jobs/package-sync.md)** and **[VS Code State Sync](docs/jobs/vscode-state-sync.md)** have no configurable options except for enabling/disabling them.

See the **[Configuration Reference](docs/configuration.md)** for every option, defaults, the folder-sync filter-rule syntax, ...

## Most used commands

```bash
pc-switcher --help            # Show help and list commands

# Initialize configuration file
pc-switcher init [--force]    # Create default config at ~/.config/pc-switcher/config.yaml

# Sync to target machine
pc-switcher sync <hostname> [--config PATH]

# Step through every individual modification, confirming each one (needs a terminal)
pc-switcher sync <hostname> --confirm-each-command

# View logs
pc-switcher logs              # Show logs directory and list recent logs
pc-switcher logs --last       # Show path to most recent log file

# Clean up old btrfs snapshots
pc-switcher cleanup-snapshots --older-than 7d [--dry-run]

# Update pc-switcher
pc-switcher self update [VERSION] [--prerelease]
```

### Startup version check

In an interactive terminal, pc-switcher checks for a newer release and offers to upgrade before running your command. Disable with `--no-version-check` or `PCSWITCHER_SKIP_VERSION_CHECK`.

## Requirements

- Ubuntu 24.04 LTS on all machines
- Single btrfs filesystem (all synced data on one filesystem per machine), possibly with multiple subvolumes
- SSH access between machines
- Only one machine actively used at a time

## Troubleshooting

### GitHub API Rate Limits

When running `pc-switcher --version`, `self update`, sync (which installs pc-switcher on target), or the startup version check (runs on every command; see above), you may see rate limit errors like:

```text
RuntimeError: Failed to fetch GitHub releases: 403 {"message": "API rate limit exceeded..."}
```

This happens because pc-switcher queries the GitHub API to check for releases. Unauthenticated requests are limited to 60/hour. GitHub's primary rate limit returns HTTP 403 as shown above; the secondary (abuse) limit may instead appear as HTTP 429.

**Solution**: Add a GitHub personal access token with public read-only permissions to your `~/.profile` on both source and target machines:

```bash
echo 'export GITHUB_TOKEN=ghp_your_token_here' >> ~/.profile
source ~/.profile
```

With a token, the rate limit increases to 5,000 requests/hour.

## Documentation

See [docs/README.md](docs/README.md) for the documentation index.

Key documents:
- **[High level requirements](docs/planning/high-level-requirements.md)** - Project vision, scope, workflow
- **[Architecture](docs/system/architecture.md)** - System architecture and design
- **[Architecture Decision Records](docs/adr/_index.md)** - Design decisions and rationale
- **[Configuration Reference](docs/configuration.md)** - Every config option, defaults, filter-rule syntax
- **[Package Sync](docs/jobs/package-sync.md)** - How apt, snap, flatpak, hand-installed `.deb`s and manual installs are synced
- **[Folder Sync](docs/jobs/folder-sync.md)** - How folders are mirrored via rsync and filtered
- **[VS Code State Sync](docs/jobs/vscode-state-sync.md)** - How VS Code state is synced
- **[Reading Sync Logs](docs/reading-sync-logs.md)** - How to read and interpret sync logs
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

### AI Agent workflow

This project used **[SpecKit](https://github.com/github/spec-kit)**—a specification-driven workflow via custom slash commands:

```bash
/speckit.specify "feature description"  # Create feature spec
/speckit.clarify                        # Refine spec details
/speckit.plan                           # Generate design artifacts
/speckit.tasks                          # Create actionable tasks
/speckit.analyze                        # Check consistency specs-plan-tasks
/speckit.implement                      # Execute implementation
```

After that, we switched to **[GSD (Get Shit Done)](https://github.com/open-gsd/gsd-core)**, a bit more lightweight and with stricter validation that the implementation matches the spec. However, still much overhead, with research, planning and execution steps taking much more time and not necessarily more reliable than pure claude code with Opus or Fable.

Now, considering more simple workflow: **[Matt Pocock's Skills](https://github.com/mattpocock/skills)**: keeping the essentials spec, TDD, validation, but leaving more to the AI agents that are really capable now.
