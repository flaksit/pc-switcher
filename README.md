# PC-switcher

A synchronization system for seamless switching between Linux desktop machines. Keep your laptops and workstations in sync with near-complete system-state replication.

## Overview

PC-switcher enables a simple workflow: work on one machine, sync before switching, resume on the other—without manual file management or cloud sync overhead.

```plain
Work on source machine → Trigger sync → Resume on target machine
```

**Status**: Early planning/design phase. No implementation code exists yet.

## What Gets Synced

- **User data**: `/home`, `/root` with all documents, code, configs, and selective caches
- **Packages**: apt, snap, flatpak, PPAs, manual installs
- **Application configurations**: GNOME, cloud mounts, systemd services
- **System configurations**: Machine-independent `/etc` files, users, groups
- **File metadata**: Owner, permissions, ACLs, timestamps
- **Containers & VMs**: Docker images/containers/volumes, KVM/virt-manager VMs
- **k3s**: Local single-node cluster state and PVCs

**Never synced**: SSH keys, Tailscale config, GPU/hardware caches, machine-specific packages

## Requirements

- Ubuntu 24.04 LTS on all machines
- Single btrfs filesystem (all synced data on one filesystem per machine)
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
- **[Feature breakdown](docs/Feature%20breakdown.md)** - Implementation phases from foundation to user features
- **[Architecture Decision Records](docs/adr/_index.md)** - Design decisions and rationale

## Development

This project uses **SpecKit**—a specification-driven workflow via custom slash commands:

```bash
/speckit.specify "feature description"  # Create feature spec
/speckit.plan                           # Generate design artifacts
/speckit.tasks                          # Create actionable tasks
/speckit.implement                      # Execute implementation
```

See [CLAUDE.md](CLAUDE.md) for development workflow details.
