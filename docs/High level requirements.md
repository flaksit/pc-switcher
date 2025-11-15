# PC-switcher

A synchronization system for seamless switching between Linux desktop machines (laptops, workstations).

## Vision

Keep pc's in sync with minimal friction. We aim at almost full system-state replication rather than as simple sync of user data.

The concrete use case involves two laptops:
- **P17** (primary, stationary): Heavy laptop at home, primary work machine
- **XPS13** (secondary, mobile): Light laptop for travel, offline capable
However, the solution should be generic to support any number of machines.

Enable a simple workflow: work on one machine, sync before switching, resume on the other — all without manual file management or real-time cloud syncing overhead.  
This must work

## Scope Requirements

The pc's are mainly used by a single user, with occasional multi-user scenarios (e.g., family members).

The machines are used for development (coding, building, testing), general productivity and office work, and occasional multimedia consumption (play music, video).

### What Must Sync

1. **User data** (`/home`, `/root`)
    - All documents, code, configs, application data
    - Selective caches: dev tool caches (`uv`, `pip`, `cargo`, `npm`) synced; browser/IDE caches excluded
    - Specific mention for VS Code
2. **Installed packages**: apt, snap, flatpak, manual .debs, custom PPAs, packages installed through install scripts
3. **Application configurations**: All app settings including GNOME Desktop, cloud mounts, systemd services
4. **System configurations**: The machine independent `/etc` files, startup services, users/groups, ...
5. **File metadata**: Owner, permissions, ACLs, timestamps must be preserved
6. **VMs**: KVM/virt-manager VMs
7. **Docker**: Images, containers, volumes, cache.
8. **k3s**: state of the local single-node k3s cluster, including PVCs

### Never Synced: Machine-Specific items

- SSH keys (`.ssh/id_*`)
- Tailscale config (`.config/tailscale`)
- Machine hardware cache (GPU shaders, fontconfig)
- Machine-specific packages and configuration

## Enviromnent

- All machines run Ubuntu 24.04 LTS
- Machines are connected to same LAN (1Gb ethernet) during sync
- All machines have a btrfs filesystem with a flat layout

## Workflow
Only one machine is actively used at a time.

Simple workflow:
1. Work on source machine
2. Startup/awaken target machine if not started yet
3. Manual trigger sync from source to target
4. Wait for sync to complete
5. Resume work
6. (Optional) Suspend/shutdown source machine

### Acceptable Constraints
It is acceptable to have some constraints on the environment, on the way the machines are used or on the sync operations, as long as they are clearly documented and do not significantly hinder usability.

A few examples:
- Way and location to install "bare" packages (manual .debs, install scripts)
- Certain applications cannot be running during sync
- VMs need to be suspended or powered off before sync
- User needs to logout from desktop environment before sync
- User needs to execute some commands in a specific program before/after sync
- Subvolumes need to be created for certain directories
- Sync can only be done when both machines are on the same LAN

However, everything that can be automated should be automated.

### Conflict Detection and Resolution
Even though PC-switcher is not designed for concurrent use of source and target machines (no bi-directional sync), it should **detect conflicts** that arise from (unsupported) concurrent use.

Conflicts must be reported, but resolution is manual.

## UX

- Single command to launch the entire sync process and performs all necessary operations on both source and target machine
- Simple, intuitive and clear user experience during synchronization and conflict and error handling
- Terminal based UI

## Project and development principles and attention points
More or less in order of importance, but trade-offs are possible:

1. Reliability: no data loss, conflict detection, consistent state after sync, detailed logging, ability to audit and roll back changes
2. Smooth UX with minimal manual intervention
3. Use well-supported tools and best practices where possible
4. Minimize disk wear (all disks are NVMe SSDs)
5. Sync speed
6. Maintainability, simplicity:
    - Simple approach and architecture
    - Easy to understand, modify and maintain the sync system
7. Documentation always up-to-date with the current state of the sync system.  
   Documentation on all levels, easily navigable between levels:
    - Rationale for decisions
    - High level architecture
    - Implementation details
    - Low-level scripts
    - User documentation


### Project structure
This repository will hold the complete implementation of the sync system:
- Scripts and programs implementing the sync logic and workflows
- Installation and upgrade script to setup the sync system on the machines, including the installation of dependencies

This repository will also hold all documentation:
- High-level architecture and design decisions
- Implementation details
- User documentation and usage instructions
- Development guidelines

This repository will NOT serve for configuration management. It should be possible for multiple users to use PC-switcher on their own machines with their own configurations.

## Ideas for later

- Parallel run of sync modules (e.g. user data and system state in parallel)
- Partial sync (e.g. only user data, no system state, skip docker/k3s/VMs)
- Machines can be in use in parallel, but only by different users. E.g. User A works on P17, User B on XPS13. Changes will be mainly in user data. For user data, uni-directional sync from one machine to the other is still possible. System state changes on the target machine must be avoided in this mode.
- Bi-directional sync with conflict resolution for user data
- Bi-directional sync with conflict resolution for system state
- Sync over WiFi
- Sync (maybe partial) while working on the source machine
- GUI / webui (for monitoring sync status, starting sync, making selections, resolving conflicts). Complication: where to host/access the GUI/webui if there is a constraint that the user is logged out of both machines?
- Sync over internet
