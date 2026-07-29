## Core (cross-cutting)

1. Basic CLI & Infrastructure - Command parser, config system, connection, logging, terminal UI skeleton, architecture for modular features
2. Safety Infrastructure - Pre-sync validation framework, btrfs snapshot management (pre/post sync snapshots, cleanup command)
3. Installation & Setup - Deploy to machines, dependency installation
4. Rollback Capability - Restore from pre-sync snapshots via `pc-switcher rollback` command

## User Features (5-10)

Each includes sync logic, conflict detection, and validation. They should be implemented as modular components using the core infrastructure.

5. User Data Sync - /home and /root with metadata, selective caches, exclusions, conflict detection for concurrent modifications
6. Package Management Sync - apt/snap/flatpak/PPAs/manual installs, detect package conflicts or version mismatches
7. System Configuration Sync - /etc files, users/groups, systemd services, machine-specific exclusions, detect conflicting system changes
8. Docker State Sync - Images, containers, volumes, cache, detect running containers or incompatible states
9. VM State Sync - KVM/virt-manager VMs, validate VMs are suspended/off, detect concurrent VM usage
10. k3s Cluster Sync - Cluster backup/restore, PVCs, validate cluster state, detect active workloads

## Advantages of This Breakdown

- Installation (3) comes early so you can deploy and test on real machines
- Rollback (4) is deferred until after the core infrastructure is stable
- Each user feature (5-10) is self-contained with its own conflict detection
- Core provides reusable infrastructure for all features
