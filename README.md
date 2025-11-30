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

Clone the repository:
```bash
git clone git@github.com:yourusername/pc-switcher.git
cd pc-switcher
```

Install using the installation script:
```bash
curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash
```

Or run directly from source:
```bash
uv sync
uv run pc-switcher --help
```

## Quick Start

Before syncing, ensure:
- Target machine is powered on and reachable via SSH
- Both machines are on the same LAN
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

Create `~/.config/pc-switcher/config.yaml`:

```yaml
# Logging configuration
log_file_level: FULL      # FULL | INFO | WARNING | ERROR
log_cli_level: INFO       # FULL | INFO | WARNING | ERROR

# Sync jobs (true = enabled, false = disabled)
sync_jobs:
  dummy_success: true
  dummy_fail: false

# Disk space monitoring
disk_space_monitor:
  preflight_minimum: "20%"  # Or absolute like "50GiB"
  runtime_minimum: "15%"
  check_interval: 30        # Seconds

# Btrfs snapshots
btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
  keep_recent: 3
  max_age_days: 30  # Optional
```

See example configs in `config/` directory.

## Available Commands

```bash
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
- SSH access between machines
- Machines connected to same LAN (1Gb) during sync
- Only one machine actively used at a time

## Key Design Principles

1. **Reliability**: No data loss, conflict detection, consistent state, full auditability
2. **Smooth UX**: Single command to launch entire sync; minimal manual intervention
3. **Use standard tools**: Well-supported, maintainable approach
4. **Minimize disk wear**: NVMe SSDs—avoid unnecessary writes
5. **Simplicity**: Easy to understand, modify, and maintain

## Documentation

- **[High level requirements](docs/High%20level%20requirements.md)** - Project vision, scope, workflow
- **[Architecture](docs/architecture.md)** - High-level architecture and design
- **[Implementation](docs/implementation.md)** - Implementation details and patterns
- **[Architecture Decision Records](docs/adr/_index.md)** - Design decisions and rationale
- **[Feature specifications](specs/001-foundation/)** - Detailed feature specs and plans

## Development

Install dependencies:
```bash
uv sync
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
