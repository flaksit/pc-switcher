# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**pc-switcher** is a two-laptop synchronization system for Ubuntu 24.04 machines (P17 and XPS 13). It implements a uni-directional, manual-trigger workflow to keep both machines in sync:
- **Primary machine (P17)**: Heavy laptop, stays at home, primary work machine
- **Secondary machine (XPS 13)**: Light laptop for travel (offline capable)
- **Sync trigger**: Manual, initiated before travel and after returning home
- **Transport**: Syncthing over local LAN (1Gb ethernet), no internet sync required

## Architecture

The system consists of four synchronized layers:

### 1. **File Sync Layer (Syncthing)**
- Bidirectional synchronization of `/home/<user>`, `/etc`, and `~/system-state/`
- Selective cache inclusion (dev tool caches synced, browser/IDE caches excluded)
- Selective `/etc` sync via `/etc/.stignore` (machine-specific files excluded)
- Conflict resolution via `.sync-conflict-*` files
- `.stignore` rules prevent syncing SSH keys, machine-specific configs (Tailscale), VMs, and containers

### 2. **System State Layer (Git-tracked repo in ~/system-state/)**
- **etckeeper**: Version control for `/etc` locally on each machine (NOT synced, excluded via `/etc/.stignore`)
- **Custom scripts**: `capture-state.sh`, `diff-state.sh`, `apply-state.sh` manage packages, services, and users
- **Syncthing integration**: `/etc` files sync via `/etc/.stignore` (no separate tracking file needed)
- **Git history**: Enables rollback and audit trail of all state changes

### 3. **Container Workflows (Special case handling)**
- **Docker**: Export/import via `docker save/load` and volume tarball backup (see `docker-export.sh`, `docker-import.sh`)
  - **Critical**: `/var/lib/docker/` must NEVER be copied directly (overlay2 filesystem corruption, binary database issues)
  - Only manual export/import or Compose configs (already synced via `/home`)
- **k3s**: Manifests only via `~/projects/k3s-manifests/`, PersistentVolumes use hostPath to `~/k3s-volumes/`
  - **Critical**: `/var/lib/rancher/k3s/` must NEVER be copied (machine-specific certificates, cluster identity)

### 4. **Special Handlers**
- **VM**: QCOW2 backing files with rsync (base template + overlay, only overlay syncs, NOCOW btrfs subvolume for performance)
- **VS Code**: Settings Sync (cloud-based, automatic) + backup script for extension list recovery

## Key Design Decisions

### Why Syncthing?
- Handles offline changes with automatic conflict resolution
- Efficient block-level sync (resume capability)
- Manual workflow compatible (user controls when to sync)
- Bidirectional support for flexible workflows

### Cache Strategy
**Include**: `.cache/pip/`, `.cache/uv/`, `.cache/pypoetry/`, `.npm/`, `.cargo/registry/`, `.cargo/git/`, `.m2/repository/`, `.gradle/caches/`
- High value: dev tool caches save GBs when syncing over cellular
- Low churn: stable references to dependencies

**Exclude**: Browser caches (Firefox, Chrome), system caches (thumbnails, GPU shaders), IDE caches (VS Code)
- Low value or corrupts on sync (hardware-specific)
- Excessive churn

**Result**: Reduces sync from 20GB to ~12GB, 500k to ~350k files

### Why Not Direct `/var/lib/docker/` or `/var/lib/rancher/k3s/` Copy?
- **Docker**: Overlay2 symlink structure, binary databases, and runtime state don't survive copying
- **k3s**: Machine-specific certs/IPs, cluster identity, CNI state; conflicts would break both machines
- **Correct approach**: Export/import workflows for Docker, manifest-only sync for k3s

## Development and Testing

### Running Scripts

**Capture current machine state**:
```bash
cd ~/system-state && ./scripts/capture-state.sh
```
Exports: package lists (apt, snap, flatpak), enabled services, users/groups manifests

**Review differences before syncing**:
```bash
cd ~/system-state && ./scripts/diff-state.sh
```
Interactive mode: shows new/changed packages and services. Checks `/etc` for files without tracking rules, prompts user to track new files with decisions saved to `/etc/.stignore`

**Apply state on target machine**:
```bash
cd ~/system-state && ./scripts/apply-state.sh
```
Installs missing packages, enables/disables services, updates user/groups. Note: `/etc` files sync automatically via Syncthing

### Docker Workflow

**Export images and volumes** (on source machine):
```bash
cd ~/system-state/docker && ./export-docker.sh
```

**Import on target machine**:
```bash
cd ~/system-state/docker && ./import-docker.sh
```

### k3s Workflow

**Manifests** (already synced via Syncthing):
```bash
kubectl apply -f ~/projects/k3s-manifests/
```

**PersistentVolume data** (optional backup/restore):
```bash
kubectl exec <pod> -- tar czf - /data > ~/system-state/k3s/volumes/<pvc>.tar.gz
```

### VM Sync

**Shutdown VM before sync**:
```bash
virsh shutdown <vm-name>
# Wait for shutdown to complete
virsh list --all  # Verify "shut off" state
```

**Sync VM images with rsync**:
```bash
cd ~/system-state/vm && ./sync-vm-to-xps.sh  # or sync-vm-to-p17.sh
```
Uses rsync to transfer QCOW2 backing files (base template + overlay). Only the overlay file transfers on each sync (~1-5GB), base image is skipped automatically as it rarely changes

## Repository Structure

```plain
~/system-state/
├── packages/              # Package list exports
│   ├── apt-selections.txt
│   ├── apt-manual.txt
│   ├── snap-list.txt
│   ├── flatpak-list.txt
│   ├── ppa-list.txt
│   └── ppas/             # Actual PPA files
├── services/              # Service state
│   ├── system-enabled.txt
│   └── user-enabled.txt
├── users/                 # User/group info
├── scripts/               # Core state management
│   ├── capture-state.sh
│   ├── diff-state.sh
│   └── apply-state.sh
├── docker/                # Docker export/import
│   ├── export-docker.sh
│   ├── import-docker.sh
│   ├── images-list.json
│   └── volumes/           # Tarball backups
├── k3s/                   # k3s utilities
│   └── volumes/           # PVC backup tarballs
├── vm/                    # VM rsync scripts (QCOW2 backing files)
│   ├── sync-vm-to-p17.sh
│   └── sync-vm-to-xps.sh
└── vscode/                # VS Code backup
    ├── capture-vscode.sh
    └── extensions-list.txt
```

## Sync Safety Requirements

⚠️ **CRITICAL**: The target machine (receiving state changes) **must be logged out** before Syncthing syncs files and before running `apply-state.sh`. Running applications, browsers, and IDEs hold file locks and in-memory state that can be corrupted if files change underneath them.

**Why this matters**:
- **Database corruption**: Firefox, Chrome, VS Code, and other apps use SQLite databases that can corrupt if modified while the app holds an open file handle
- **Application crashes**: Config files changing during runtime can cause crashes; apps may overwrite synced changes with stale in-memory state
- **Container data loss**: Docker volumes syncing while containers run; k3s PVC data syncing while pods are active
- **Race conditions**: Syncthing writes may conflict with app writes, leading to partial/invalid files
- **Silent data loss**: Apps may cache old versions of files, ignoring on-disk updates

**Safe approach**:
1. Before initiating sync from source (P17): Target machine (XPS) must be fully logged out (no user session)
2. Scripts should verify target is not logged in before proceeding
3. Once sync completes, target user can log back in
4. Then run `apply-state.sh` on target (package/service changes, with no competing applications)

## Workflow

### Pre-travel (on P17)
```bash
~/scripts/prepare-for-travel.sh
# 1. Ensure XPS is logged out
# 2. On both machines: create pre-sync snapshot
# 3. capture-state.sh → export current state
# 4. Wait for Syncthing "Up to Date"
# 5. diff-state.sh → review changes
# 6. apply-state.sh → apply to XPS (already synced via Syncthing)
# 7. Review Syncthing logs for deleted files/folders (deletions sync silently)
# Optional: ./system-state/docker/export-docker.sh, VM sync
```

### Post-travel (on XPS)
```bash
~/scripts/post-travel-sync.sh
# Similar flow in reverse (log out of P17 before sync)
```

## Initial Setup (Phase 0)

Before first Syncthing sync:
1. **Backup critical XPS configs** to `~/pre-sync-backup/` (`.bashrc`, `.gitconfig`, `.ssh/config`, VS Code settings)
2. **Review `.stignore`** patterns to exclude machine-specific files (SSH keys, Tailscale, VMs, containers)
3. **Configure Syncthing**:
   - On P17: "Send & Receive" folder type
   - On XPS: "Receive Only" during initial sync (prevents conflicts)
   - Enable "Ignore Delete" on both during initial population
4. **Let P17 content populate XPS**, wait for sync completion
5. **Review conflicts**: `find ~ -name "*.sync-conflict-*"`, merge desired content
6. **Switch XPS to "Send & Receive"**, disable "Ignore Delete" on both

## Important Exclusions

- **SSH keys** (`.ssh/id_*`): Machine-specific credentials
- **Tailscale** (`.config/tailscale`): Each machine has distinct network identity
- **VM storage** (`.local/share/libvirt`): Synced separately via rsync (QCOW2 backing files at `/var/lib/libvirt/images`)
- **Container storage** (`.local/share/containers`): Use Docker export/import
- **Browser caches**: Too much churn, minimal benefit
- **IDE caches**: Can corrupt during sync

## Git Workflow

Use non-fast-forward, non-squash merges when integrating branches:
```bash
git merge --no-ff --no-squash <branch>
```

Always use SSH for cloning:
```bash
git clone git@github.com:username/repo.git
```

## Maintenance

- **Before each sync**: Run `./scripts/create-sync-snapshot.sh` on source for rollback safety, then `capture-state.sh` to export current state
- **Before applying state**: On target machine, run `./scripts/create-sync-snapshot.sh` (rollback insurance for silent deletions)
- **Review diffs**: Always run `diff-state.sh` before `apply-state.sh`
- **After applying state**: Check Syncthing logs for deleted files/folders (deletions sync without prompts; logs provide audit trail)
- **Monitor Syncthing**: Check logs for conflicts, errors, or stalled transfers
- **Periodic review**: Update `/etc/.stignore` and `.stignore` as needs evolve
- **Git history**: Full audit trail in `~/system-state/.git` enables rollback
- **etckeeper history**: Per-machine version history in `/etc/.git` enables rollback of `/etc` changes
