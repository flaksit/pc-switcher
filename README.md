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

## Configuration

Run `pc-switcher init` to create the default configuration file at `~/.config/pc-switcher/config.yaml`, or create it manually:

```yaml
# Logging configuration (see Logging Configuration section below for details)
logging:
  file: DEBUG      # Floor level for file output
  tui: INFO        # Floor level for terminal output
  external: WARNING  # Floor for third-party libraries

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

## Logging Configuration

Configure log levels in your `~/.config/pc-switcher/config.yaml`:

```yaml
logging:
  file: DEBUG      # Log level floor for file output (default: DEBUG)
  tui: INFO        # Log level floor for TUI output (default: INFO)
  external: WARNING  # Log level floor for external libraries (default: WARNING)
```

### Log Levels

| Level | Value | Description |
|-------|-------|-------------|
| DEBUG | 10 | Internal diagnostics |
| FULL | 15 | Operational details (file-level sync info) |
| INFO | 20 | High-level operations (job start/complete) |
| WARNING | 30 | Unexpected but non-fatal conditions |
| ERROR | 40 | Recoverable errors |
| CRITICAL | 50 | Unrecoverable errors, sync must abort |

### Default Behavior

- `file: DEBUG` - All log levels written to file
- `tui: INFO` - Only INFO and above shown in terminal
- `external: WARNING` - External library logs (asyncssh, etc.) filtered to WARNING+

Log files are written to `~/.local/share/pc-switcher/logs/` in JSON lines format.

### Common Configurations

**Debug SSH connection issues:**
```yaml
logging:
  file: DEBUG
  tui: INFO
  external: DEBUG  # Show asyncssh debug logs
```

**Quiet mode (errors only):**
```yaml
logging:
  file: DEBUG     # Still log everything to file
  tui: ERROR      # Only show errors in terminal
  external: ERROR
```

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

## Troubleshooting

### GitHub API Rate Limits

When running `pc-switcher --version`, `self update`, or sync (which installs pc-switcher on target), you may see rate limit errors like:

```
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
