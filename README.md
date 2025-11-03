# PC-switcher

A two-laptop synchronization system for seamless switching between Ubuntu machines with different roles.

## Vision

Keep two Ubuntu 24.04 pc's in sync with minimal friction:
- **P17** (primary, stationary): Heavy laptop at home, primary work machine
- **XPS 13** (secondary, mobile): Light laptop for travel, offline capable

Enable a simple workflow: work on one machine, sync before switching, resume on the other—all without manual file management or real-time cloud syncing overhead.

## Why This Matters

Maintaining two machines in sync is non-trivial. Naive approaches fail:
- **Direct `/var/lib/docker/` copy**: Corrupts overlay2 filesystems and binary databases
- **Real-time cloud sync**: Expensive, unreliable, poor for offline use
- **Manual file management**: Tedious, error-prone
- **Unidirectional rsync**: Loses data when both machines diverge offline

This project solves it with:
- **Syncthing**: Efficient, bidirectional, conflict-aware file sync over LAN
- **System state management**: Interactive scripts for packages, services, `/etc` configs
- **Container-aware handling**: Docker and k3s workflows that respect their architecture
- **VM snapshot sync**: Efficient overlay-based VM synchronization

## What Gets Synced

### Always Synced (via Syncthing)
- **User home directory** (`/home/<user>`)
  - All documents, code, configs, application data
  - Selective caches: dev tool caches (`pip`, `cargo`, `npm`) synced; browser/IDE caches excluded
- **System state repository** (`~/system-state/`)
  - Package lists, service configurations, selected `/etc` files
  - Git-tracked for audit and rollback

### Manual Sync (Special Workflows)
- **Docker**: Export/import via `docker save/load` (never copy `/var/lib/docker/` directly)
- **k3s**: Manifests synced automatically, PersistentVolumes via hostPath to `~/k3s-volumes/`
- **VMs**: QCOW2 snapshots with overlay-based sync (efficient, only syncs changes)
- **VS Code**: Settings Sync (cloud-based) + backup script for extensions

### Never Synced (Machine-Specific)
- SSH keys (`.ssh/id_*`)
- Tailscale config (`.config/tailscale`)
- Machine hardware cache (GPU shaders, fontconfig)

## Project Structure

This repository contains the complete implementation framework:

```
pc-switcher/
├── README.md                          # This file
├── CLAUDE.md                          # Architecture guide for Claude Code
├── Context.md                         # Detailed decision rationale
├── Plan.md                            # Implementation roadmap (7 phases)
│
├── scripts/                           # User-facing workflows (TODO: implement)
│   ├── prepare-for-travel.sh          # Pre-sync automation (capture → diff → apply)
│   └── post-travel-sync.sh            # Return-home automation
│
└── system-state/                      # State repository structure (TODO: create)
    ├── packages/                      # Package lists (apt, snap, flatpak, PPAs)
    ├── services/                      # Systemd service states
    ├── users/                         # User and group information
    ├── etc-tracked/                   # Selected /etc files
    ├── .track-rules                   # Interactive tracking decisions
    ├── scripts/                       # Core state management
    │   ├── capture-state.sh           # Export current system state
    │   ├── diff-state.sh              # Interactive diff and tracking updates
    │   └── apply-state.sh             # Apply state changes to target machine
    ├── docker/                        # Docker export/import workflows
    ├── k3s/                           # k3s manifest and PVC utilities
    ├── vm/                            # VM snapshot sync scripts
    └── vscode/                        # VS Code extension backup
```

## Implementation Status

**Current Phase**: Planning and architecture design (complete)

**Next Steps**:
1. **Phase 0**: Pre-flight checks and conflict avoidance strategy
2. **Phase 1**: Syncthing setup and `.stignore` configuration
3. **Phase 2**: System state repository initialization
4. **Phase 3**: Core state management scripts (`capture`, `diff`, `apply`)
5. **Phase 4**: Container workflows (Docker, k3s)
6. **Phase 5**: VM snapshot sync
7. **Phase 6**: VS Code integration
8. **Phase 7**: User-facing workflow scripts and testing

See [Plan.md](Plan.md) for detailed implementation roadmap.

## Usage (Once Implemented)

### First Sync (One-Time Setup)
```bash
# On both machines:
sudo apt install syncthing
systemctl --user enable --now syncthing

# Configure via Web UI (http://localhost:8384):
# - Add other machine as device
# - Create shared folders: /home/<user>, ~/system-state

# Review Plan.md Phase 0 for conflict avoidance strategy
```

### Pre-Travel Workflow
```bash
./scripts/prepare-for-travel.sh
# 1. Capture P17 state → export packages, services, /etc
# 2. Wait for Syncthing sync
# 3. Review diff → interactive decisions on /etc tracking
# 4. Apply changes → install packages, update configs on XPS
# Ready to travel!
```

### Post-Travel Workflow
```bash
./scripts/post-travel-sync.sh
# Same flow in reverse (XPS → P17)
```

## Key Design Principles

1. **Manual Triggers Only**: No real-time sync, user controls when to proceed
2. **Offline-Friendly**: Both machines can diverge offline; Syncthing handles conflicts
3. **Interactive Decisions**: Tracking rules for `/etc` files are persistent, learn from user choices
4. **Audit Trail**: Git history in `~/system-state/` enables rollback
5. **Container-Aware**: Respects Docker/k3s architecture; no direct filesystem copies
6. **LAN-Only**: Efficient, private sync on trusted home network

## Documentation

- **[CLAUDE.md](CLAUDE.md)**: Architecture and development guide for Claude Code
- **[Context.md](Context.md)**: Detailed rationale behind each design decision
- **[Plan.md](Plan.md)**: Phase-by-phase implementation roadmap with test cases

## Network Requirements

- Both machines on same LAN (1Gb ethernet)
- No internet sync required (machines never online simultaneously)
- Syncthing discovery on local network only

## Current Limitations (By Design)

- **Machines must be on same LAN** for efficient sync (no cloud relay)
- **Machines should not run simultaneously** in normal workflow (avoid constant conflicts)
- **Docker/k3s require manual export/import** (filesystem architecture incompatibilities)
- **VMs must be shut down** before syncing (QCOW2 consistency requirement)

## Future Enhancements

- Automated Syncthing discovery and device pairing
- Web UI for tracking rule management
- Scheduled pre-travel sync reminders
- Docker/k3s integration improvements
- Support for additional container runtimes

## License

MIT (to be added)

## Contributing

This is a personal project but architectural discussions and improvements are welcome.
