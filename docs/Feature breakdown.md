## Foundation (cross-cutting)

1. Basic CLI & Infrastructure - Command parser, config system, SSH connection, logging, terminal UI skeleton, architecture for modular features
2. Safety Infrastructure - Pre-sync validation framework, btrfs snapshot management, rollback capability
3. Installation & Setup - Deploy to machines, dependency installation, btrfs subvolume structure setup

## User Features

Each includes sync logic, conflict detection, and validation. They should be implemented as modular components using the foundation infrastructure.

4. User Data Sync - /home and /root with metadata, selective caches, exclusions, conflict detection for concurrent modifications
5. Package Management Sync - apt/snap/flatpak/PPAs/manual installs, detect package conflicts or version mismatches
6. System Configuration Sync - /etc files, users/groups, systemd services, machine-specific exclusions, detect conflicting system changes
7. Docker State Sync - Images, containers, volumes, cache, detect running containers or incompatible states
8. VM State Sync - KVM/virt-manager VMs, validate VMs are suspended/off, detect concurrent VM usage
9. k3s Cluster Sync - Cluster backup/restore, PVCs, validate cluster state, detect active workloads

## Advantages of This Breakdown

- Installation (3) comes early so you can deploy and test on real machines
- Each user feature (4-9) is self-contained with its own conflict detection
- Foundation provides reusable infrastructure for all features
