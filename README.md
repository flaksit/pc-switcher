# PC-switcher

A synchronization system for seamless switching between Linux desktop machines. Keep your laptops and workstations in sync with near-complete system-state replication.

## Overview

PC-switcher enables a simple workflow: work on one machine, sync before switching, resume on the other—without manual file management or cloud sync overhead.

```plain
Work on source machine → Trigger sync → Resume on target machine
```

**Status**: Foundation infrastructure complete. Core sync functionality in development.

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
(Note: Use the package name `pcswitcher`, not the command name `pc-switcher`)

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

**Note**: Sync jobs are currently placeholders for testing infrastructure. Real sync functionality is in development.

## Configuration

Run `pc-switcher init` to create the default configuration file at `~/.config/pc-switcher/config.yaml`, or create it manually:

```yaml
# Logging configuration
log_file_level: FULL      # DEBUG | FULL | INFO | WARNING | ERROR | CRITICAL
log_cli_level: INFO       # DEBUG | FULL | INFO | WARNING | ERROR | CRITICAL

# Sync jobs (true = enabled, false = disabled)
sync_jobs:
  dummy_success: true
  dummy_fail: false

# Disk space monitoring
disk_space_monitor:
  preflight_minimum: "20%"  # Or absolute like "50GiB"
  runtime_minimum: "15%"    # CRITICAL abort if below
  warning_threshold: "25%"  # WARNING log if below
  check_interval: 30        # Seconds

# Btrfs snapshots
btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
  keep_recent: 3
  # max_age_days: 30  # Optional - enables age-based cleanup
```

See default configuration in `src/pcswitcher/default-config.yaml`.

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
```

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

## Naming Convention

This project uses two different names depending on context:

- **Package name**: `pcswitcher` (no dash)
  - Used for: `uv tool install/uninstall`, Python imports (`from pcswitcher...`)
  - Why: Python package names cannot contain dashes (PEP 8 convention)

- **Command name**: `pc-switcher` (with dash)
  - Used for: Running commands in your terminal
  - Why: CLI tools commonly use dashes for readability (like `docker-compose`, `git-lfs`)

When you run `uv tool list`, you'll see:
```
pcswitcher v0.0.0.post159.dev0+3825b99
- pc-switcher
```

This shows the package name provides the `pc-switcher` executable.

## Documentation

- **[High level requirements](docs/High%20level%20requirements.md)** - Project vision, scope, workflow
- **[Architecture](docs/architecture.md)** - High-level architecture and design
- **[Implementation](docs/implementation.md)** - Implementation details and patterns
- **[Architecture Decision Records](docs/adr/_index.md)** - Design decisions and rationale
- **[Feature specifications](specs/001-foundation/)** - Detailed feature specs and plans

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
/speckit.plan                           # Generate design artifacts
/speckit.tasks                          # Create actionable tasks
/speckit.implement                      # Execute implementation
```

See [CLAUDE.md](CLAUDE.md) for complete development workflow details.
