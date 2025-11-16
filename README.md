# PC-switcher

A synchronization system for seamless switching between Linux desktop machines. Keep your laptops and workstations in sync with near-complete system-state replication.

## Overview

PC-switcher enables a simple workflow: work on one machine, sync before switching, resume on the other machine with all your state preserved.

```text
Work on source machine  ->  Trigger sync  ->  Resume on target machine
```

**Key Features:**
- Btrfs snapshot-based safety with automatic rollback capability
- Modular architecture for syncing different system components
- Conflict detection for unsupported concurrent use
- Terminal UI with progress reporting
- Structured logging with configurable levels

## Requirements

- Ubuntu 24.04 LTS on all machines
- btrfs filesystem (required for snapshot support)
- Machines connected to same LAN during sync
- SSH key-based authentication configured between machines
- Python 3.13 or later (managed automatically by uv)

## Installation

### Prerequisites

Install [uv](https://github.com/astral-sh/uv) for Python package management:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Install from GitHub Release

Install a specific version directly from GitHub:

```bash
# Install specific version (replace 1.0.0 with desired version)
uv tool install git+https://github.com/flaksit/pc-switcher@v1.0.0

# Verify installation
pc-switcher --version
```

### Install from Source

```bash
# Clone repository
git clone git@github.com:flaksit/pc-switcher.git
cd pc-switcher

# Install as a tool (system-wide access)
uv tool install .

# Verify installation
pc-switcher --version
```

### Install for Development

```bash
# Clone repository
git clone git@github.com:flaksit/pc-switcher.git
cd pc-switcher

# Sync dependencies
uv sync

# Run directly via uv
uv run pc-switcher --version
```

## Quick Start

### 1. Configure SSH Access

Ensure SSH key-based authentication is set up between your machines:

```bash
# On source machine, copy SSH key to target
ssh-copy-id user@target-hostname

# Test connection
ssh user@target-hostname hostname
```

### 2. Create Configuration

Create the configuration directory and copy the default config:

```bash
mkdir -p ~/.config/pc-switcher
curl -fsSL https://raw.githubusercontent.com/flaksit/pc-switcher/main/config/default.yaml \
  -o ~/.config/pc-switcher/config.yaml
```

Alternatively, if you've cloned the repository:

```bash
mkdir -p ~/.config/pc-switcher
cp config/default.yaml ~/.config/pc-switcher/config.yaml
```

See [`config/default.yaml`](config/default.yaml) for all available configuration options with documentation.

A minimal configuration is also available at [`config/minimal.yaml`](config/minimal.yaml).

### 3. Verify Btrfs Setup

Check that your system uses btrfs and has the expected subvolumes:

```bash
# Verify filesystem type
stat -f -c "%T" /
# Should output: btrfs

# List available subvolumes
sudo btrfs subvolume list /
# Should show @, @home, etc.
```

Ensure the snapshot directory exists:

```bash
sudo mkdir -p /.snapshots
sudo chmod 755 /.snapshots
```

### 4. Run Your First Sync

```bash
pc-switcher sync target-hostname
```

The sync will:
1. Create pre-sync snapshots of configured subvolumes
2. Execute enabled modules in order
3. Create post-sync snapshots
4. Report final status

## Usage

### Sync to Target Machine

```bash
pc-switcher sync <target-hostname>

# With custom config file
pc-switcher sync target-hostname --config /path/to/custom.yaml

# Examples
pc-switcher sync workstation-02
pc-switcher sync 192.168.1.100
```

### View Logs

```bash
# List all log files
pc-switcher logs

# Show most recent log
pc-switcher logs --last

# Show logs for specific session
pc-switcher logs --session abc12345
```

Logs are stored in `~/.local/share/pc-switcher/logs/`.

### Cleanup Old Snapshots

```bash
# Delete snapshots older than 7 days, keeping at least 3 recent
pc-switcher cleanup-snapshots

# Custom settings
pc-switcher cleanup-snapshots --older-than 14d --keep-recent 5
```

### Version Information

```bash
pc-switcher --version
```

## Configuration

Configuration file location: `~/.config/pc-switcher/config.yaml`

### Logging Levels

| Level    | Description                                       |
|----------|---------------------------------------------------|
| DEBUG    | Verbose diagnostics (development use)             |
| FULL     | File-level operations and detailed progress       |
| INFO     | High-level operations and milestones              |
| WARNING  | Potential issues that don't stop sync             |
| ERROR    | Recoverable errors (sync continues)               |
| CRITICAL | Unrecoverable errors (sync aborts)                |

**Recommendations:**
- `log_file_level: FULL` for troubleshooting
- `log_cli_level: INFO` for normal operation

### Disk Space Monitoring

```yaml
disk:
  min_free: 0.20          # 20% or absolute bytes (e.g., 10737418240 for 10GB)
  reserve_minimum: 0.15   # Abort if crossed during sync
  check_interval: 30      # Seconds between checks
```

The sync will:
- Abort before starting if free space < `min_free`
- Abort during sync if free space drops below `reserve_minimum`

### Module Configuration

Modules execute sequentially in the order listed in `sync_modules`.

**Important constraints:**
- `btrfs_snapshots` must be first and cannot be disabled
- Module order determines execution sequence
- Each module can have its own configuration section

```yaml
sync_modules:
  btrfs_snapshots: true   # Required
  # future_module: true   # Add more modules as they become available

btrfs_snapshots:
  subvolumes:
    - "@"                  # Root filesystem (mounted at /)
    - "@home"              # Home directory (mounted at /home)
    - "@root"              # Root user home (mounted at /root, if exists)
  snapshot_dir: "/.snapshots"
  keep_recent: 3           # Always keep 3 most recent snapshots
  max_age_days: 7          # Delete older snapshots during cleanup
```

## What Gets Synced

**Current implementation (foundation):**
- Btrfs snapshots for safety and rollback capability

**Planned modules:**
- User data: `/home`, `/root` with documents, code, configs
- Packages: apt, snap, flatpak, PPAs
- Application configurations: GNOME, systemd services
- System configurations: Machine-independent `/etc` files
- Containers: Docker images, containers, volumes
- VMs: KVM/virt-manager
- k3s: Local single-node cluster state

**Never synced (machine-specific):**
- SSH keys (`.ssh/id_*`)
- Tailscale configuration
- GPU/hardware caches
- Machine-specific packages

## Exit Codes

| Code | Meaning                                           |
|------|---------------------------------------------------|
| 0    | Success - sync completed without errors           |
| 1    | Error - sync failed or configuration invalid      |
| 130  | Interrupted - user cancelled with Ctrl+C          |

## Troubleshooting

### Configuration File Not Found

```text
Configuration error: Configuration file not found: /home/user/.config/pc-switcher/config.yaml
```

**Solution:** Create the configuration file as shown in Quick Start section 2.

### Required Module Missing

```text
ConfigError: Required module 'btrfs_snapshots' is missing from sync_modules
```

**Solution:** Ensure `btrfs_snapshots: true` is the first entry in `sync_modules`.

### Filesystem Not Btrfs

```text
SyncError: Root filesystem is ext4, not btrfs. PC-switcher requires btrfs.
```

**Solution:** PC-switcher requires btrfs filesystem for snapshot support. You need to either:
- Reinstall Ubuntu with btrfs filesystem
- Migrate existing filesystem to btrfs

### Subvolume Not Found

```text
Subvolume '@home' not found in top-level btrfs subvolumes
```

**Solution:** Check available subvolumes with `sudo btrfs subvolume list /` and update your config to match actual subvolume names.

### Insufficient Disk Space

```text
SyncError: Insufficient disk space. Free: 5.2 GB, Required: 10.7 GB
```

**Solution:** Free up disk space before starting sync, or adjust `disk.min_free` threshold in config (not recommended to lower it too much).

### SSH Connection Failed

```text
ConnectionError: Failed to connect to root@target: [Errno 111] Connection refused
```

**Solution:**
1. Verify target machine is reachable: `ping target-hostname`
2. Ensure SSH service is running on target: `systemctl status ssh`
3. Check SSH key authentication: `ssh user@target-hostname`
4. Verify firewall allows SSH (port 22)

### Permission Denied for Btrfs Operations

```text
Failed to list btrfs subvolumes: error: permission denied
```

**Solution:** Ensure you have sudo access and passwordless sudo configured for btrfs commands:

```bash
# Add to /etc/sudoers.d/pc-switcher
your_username ALL=(ALL) NOPASSWD: /usr/bin/btrfs
```

### Log File Location

All logs are stored in `~/.local/share/pc-switcher/logs/`. Each sync session creates a new log file:
- Filename format: `sync-YYYYMMDD-HHMMSS.log`
- JSON-formatted structured logs
- Contains all operations at configured file log level

To view recent activity:
```bash
ls -lt ~/.local/share/pc-switcher/logs/ | head
pc-switcher logs --last
```

## Safety Features

### Rollback Capability

If sync fails with critical errors, PC-switcher offers automatic rollback to pre-sync state:

1. Pre-sync snapshots are created before any changes
2. On failure, you're prompted to rollback
3. Rollback restores all configured subvolumes
4. Reboot required after rollback

Manual rollback is also possible:
```bash
pc-switcher rollback <session-id>
```

### Concurrent Use Prevention

- Only one sync operation can run at a time (enforced by lock file)
- Lock file location: `$XDG_RUNTIME_DIR/pc-switcher/sync.lock`
- Automatically released on completion or interruption

### Data Safety Priorities

1. **Reliability:** No data loss, conflict detection
2. **Auditability:** Complete logging of all operations
3. **Reversibility:** Snapshot-based rollback capability
4. **Minimal disk wear:** Copy-on-write snapshots, efficient transfers

## Architecture

For detailed architecture documentation, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and contribution guidelines.

## Documentation

- **[High level requirements](docs/High%20level%20requirements.md)** - Project vision, scope, workflow
- **[Feature breakdown](docs/Feature%20breakdown.md)** - Implementation phases
- **[Architecture Decision Records](docs/adr/_index.md)** - Design decisions and rationale
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - Module interfaces and workflow diagrams
- **[CONTRIBUTING.md](CONTRIBUTING.md)** - Development and contribution guide

## License

MIT License - see LICENSE file for details.
