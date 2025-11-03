# btrfs Architecture Review and Opportunities

**Context**: Both laptops (P17 and XPS 13) run Ubuntu 24.04 with btrfs as the filesystem. This document explores how btrfs-specific capabilities could improve the pc-switcher architecture.

---

## Executive Summary

The current architecture uses:
- **Syncthing** for file-level sync
- **QCOW2 snapshots** for VM storage with overlay files
- **Git repository** for system state versioning (package lists, tracked `/etc` files)
- **Docker export/import** for container state
- **Manual scripts** for package and service state management

**Recommended changes after analysis:**
- **Syncthing for everything except VMs**: Use Syncthing for `/home` AND `/etc` (with `.stignore` for selective sync)
- **btrfs for VMs only**: Replace QCOW2 with btrfs subvolumes + `btrfs send/receive` for efficient VM sync
- **btrfs snapshots for safety**: Pre-travel snapshots of `/home`, `/etc`, VMs for rollback capability
- **etckeeper locally**: Keep etckeeper on each machine for local version history, don't sync git repos

---

## Key Opportunities

### 1. VM Storage: Eliminate QCOW2 Complexity

**Current approach (Plan.md:102-112)**:
- Base QCOW2 image (~50GB) created per machine
- Overlay/snapshot files contain only changes (1-5GB)
- Sync only overlay files, full base rarely
- 5-10% performance overhead vs raw disk

**btrfs alternative**:
- Store VM images in a **btrfs subvolume** (e.g., `/var/lib/libvirt/images` as subvolume)
- Before sync: `btrfs subvolume snapshot` of VM subvolume (instant, copy-on-write)
- Sync via `btrfs send/receive` (sends only changed blocks between snapshots)
- No performance overhead (raw disk image on btrfs with CoW)
- Simpler tooling, no QCOW2 layer management

**Benefits**:
- Near-instant snapshots (no VM shutdown delay to create snapshot)
- Native filesystem-level sync (no rsync of large files)
- No performance penalty
- Snapshot history on each machine (rollback capability)

**Trade-offs**:
- Requires btrfs on both machines (already true)
- `btrfs send/receive` over network requires piping through ssh
- Less mature tooling than QCOW2

### 2a. `/etc` Sync: Syncthing with `/etc/.stignore` (Recommended)

**Initial consideration: btrfs send/receive for `/etc`**:
- Problem: Can't selectively exclude files (all-or-nothing per subvolume)
- Would need post-processing to filter machine-specific files
- Defeats atomic benefits of btrfs

**Recommended approach: Syncthing for `/etc` with `/etc/.stignore`**:

**Implementation**:
```
# /etc/.stignore - Syncthing ignore patterns for /etc

# Exclude everything by default
*

# === Machine-specific (never sync) ===
machine-id
hostname
adjtime
fstab

# === etckeeper (local version control, not synced) ===
.git
.gitignore

# === Tracked system config (always sync) ===
!hosts
!environment
!default/grub
!apt/sources.list.d/**
```

**Workflow for interactive tracking**:
1. User runs `diff-state.sh` to check for `/etc` files without a rule in `/etc/.stignore`. For each such file: prompt "Track /etc/foo.conf? [always/never]"
   - "always" → append `!foo.conf` to `/etc/.stignore`
   - "never" → append `foo.conf` to `/etc/.stignore`
2. Syncthing automatically syncs tracked files
3. Commit updated `.stignore` to `~/system-state/etc.stignore` for history

**Benefits**:
- **Single sync mechanism**: Syncthing for both `/home` AND `/etc`
- **Selective sync built-in**: `.stignore` controls what syncs
- **Syncthing-native**: No custom scripts to generate ignore patterns
- **`.stignore` syncs itself**: Both machines have same tracking rules
- **Interactive tracking preserved**: `diff-state.sh` still prompts for new files

**etckeeper integration**:
- **etckeeper on each machine** (local git repo in `/etc/.git`, NOT synced)
  - Auto-commits all `/etc` changes locally (package installs, manual edits)
  - Provides per-machine version history and rollback
- **Syncthing syncs selected files** (via `/etc/.stignore`)
  - Files sync directly between machines
  - Each machine's etckeeper sees synced files as changes and commits them
- **Independent git histories**: Each machine tracks both local and synced changes

**Example workflow**:
```bash
# On P17: Install package that modifies /etc
sudo apt install foo
# → etckeeper auto-commits to /etc/.git (local on P17)

# apply-state.sh installs foo on XPS
# → etckeeper auto-commits to /etc/.git (local on XPS)

# Syncthing syncs /etc/foo.conf to XPS (via .stignore allowlist)

# On XPS: etckeeper sees the change
sudo etckeeper commit "synced from P17"
# → XPS git log shows the file appeared from sync

# Later on P17: Review what changed
git -C /etc log --since="1 week ago"
git -C /etc diff HEAD~5 hosts

# Rollback a bad change locally
sudo git -C /etc revert abc123
```

**Verdict**: Syncthing for `/etc` sync + etckeeper for local history is simpler and safer than trying to sync git repos or use btrfs send/receive.

### 2b. Package State Management: Manifests in `~/system-state/`

**Current approach (Plan.md:147-193)**:
- `~/system-state/` git repository with package lists and service states
- `capture-state.sh` exports package manifests to text files
- `diff-state.sh` compares package states between machines
- `apply-state.sh` installs/removes packages based on manifests

**No change needed**: This workflow works well and doesn't benefit from btrfs

**What `capture-state.sh` does**:
- Export package lists: `dpkg --get-selections`, `apt-mark showmanual`, `snap list`, `flatpak list`
- Export enabled services: `systemctl list-unit-files --state=enabled`
- Export PPA list: `ls /etc/apt/sources.list.d/`
- Commit all manifests to `~/system-state/.git`

**What `diff-state.sh` does**:
- Compare package lists between machines
- Show which packages are added/removed
- Compare enabled services

**What `apply-state.sh` does**:
- Install missing packages: `dpkg --set-selections`, `apt-get dselect-upgrade`
- Install missing snaps and flatpaks
- Enable/disable services to match source machine

**Note**: Package manifests are synced via Syncthing (as part of `~/system-state/` directory). `/etc` files are synced directly by Syncthing (via `/etc/.stignore`). These are separate concerns.  
`/etc` should be synced AFTER packages are installed to avoid config drift: the `/etc` files from source should override the default `/etc` files created by package installs.

### 3. Docker: Native btrfs Storage Driver

**Current approach (Plan.md:73-87)**:
- `/var/lib/docker/` explicitly excluded from sync (overlay2 corruption risk)
- Manual `docker save/load` for images
- Tarball backup/restore for volumes

**btrfs alternative**:
- Configure Docker to use `btrfs` storage driver (native CoW support)
- Store Docker data in btrfs subvolume: `/var/lib/docker` as subvolume
- Snapshot before travel: `btrfs subvolume snapshot /var/lib/docker /var/lib/docker-snapshot`
- Sync via `btrfs send/receive`

**Benefits**:
- Eliminates manual export/import workflow
- Faster container startup (CoW vs overlay2)
- Consistent state (all containers, images, volumes in one atomic snapshot)

**Trade-offs**:
- Docker btrfs driver has known issues (slower than overlay2 for some workloads)
- Requires Docker daemon restart to change storage driver
- Still risks syncing machine-specific state (container IDs, network config)
- **Critical**: Even with btrfs, runtime state (iptables rules, cgroups, veth pairs) doesn't transfer between machines

**Verdict**: Unlikely to improve on manual export/import. Docker's runtime state is inherently machine-specific.

### 4. `/home` Sync: Syncthing (Not btrfs send/receive)

**Why not btrfs send/receive for `/home`?**

**Problem**: Can't selectively exclude files during `btrfs send/receive`
- Need to exclude: `.ssh/id_*`, `.config/tailscale`, `.cache/*` (except dev caches)
- btrfs transfers entire subvolume atomically (all-or-nothing)
- Would require post-receive filtering (messy, defeats atomic benefits)

**Alternative considered: Nested subvolumes**
- Make `.ssh/`, `.config/tailscale/` separate child subvolumes (excluded from send)
- Problem: Requires restructuring `/home`, complex setup, fragile

**Recommended: Keep Syncthing for `/home`**

**Benefits of Syncthing for `/home`**:
- **Selective sync built-in**: `.stignore` handles exclusions elegantly
- **Bidirectional**: Handles offline changes on both machines
- **Conflict resolution**: Creates `.sync-conflict-*` files automatically
- **Incremental**: Only syncs changed files/blocks
- **Simple**: No scripting required, GUI available

**Performance consideration**:
- Initial sync is slow (must scan all files)
- Incremental syncs are fast (only checks mtimes)
- `btrfs send/receive` would be faster for initial sync of large directories with many files, but loses selective exclusion capability

**Bonus: Hybrid approach for large subdirectories**:
If `/home` contains subdirectories with tens of thousands of files that don't need selective exclusion:
- Create btrfs subvolume for that subdirectory (e.g., `~/.local/share/SomeApp/` or `~/dev/`)
- Add to `/home/.stignore` (exclude from Syncthing)
- Sync with `btrfs send/receive` separately

**Example**:
```bash
# If ~/.local/share/AppWithManyFiles/ has 50k files
sudo btrfs subvolume create ~/.local/share/AppWithManyFiles

# Add to /home/.stignore:
.local/share/AppWithManyFiles

# Sync separately with btrfs send/receive
```

**Verdict**: Syncthing for `/home` is the right choice due to selective sync requirements. Consider hybrid approach only for specific large subdirectories.

### 5. k3s: Subvolume-Based PersistentVolumes

**Current approach (Plan.md:88-100)**:
- Manifests in `~/projects/k3s-manifests/` (synced by Syncthing)
- PersistentVolumes use `hostPath` pointing to `~/k3s-volumes/` (synced by Syncthing)
- `/var/lib/rancher/k3s/` explicitly excluded (machine-specific cluster state)

**btrfs alternative**:
- PersistentVolumes as btrfs subvolumes: `/home/<user>/k3s-volumes/<pvc-name>` as individual subvolumes
- Snapshot each PVC independently before travel
- Sync via `btrfs send/receive` (only changed PVCs)

**Benefits**:
- Snapshot individual PVCs (granular versioning)
- Faster sync for large PVCs (block-level changes only)
- Rollback per-PVC (not entire `~/k3s-volumes/`)

**Trade-offs**:
- Requires scripting to create subvolumes per PVC (not automatic)
- Syncthing already handles `~/k3s-volumes/` efficiently (if PVCs are small)
- Adds complexity for marginal benefit (unless PVCs are very large)

**Verdict**: Only valuable if PVCs are multi-GB and change frequently. Otherwise, Syncthing is simpler.

---

## Final Recommended Architecture

### Summary: Syncthing + btrfs Hybrid

**Architecture Components:**

```
┌─────────────────────────────────────────────────────────────┐
│ Syncthing Layer (Primary Sync Mechanism)                    │
├─────────────────────────────────────────────────────────────┤
│ • /home with .stignore (machine-specific exclusions)        │
│ • /etc with .stignore (selective tracking, managed by       │
│   diff-state.sh based on interactive decisions)             │
│ • ~/system-state/ (git repo with scripts, manifests)        │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ btrfs Snapshot Layer (Rollback Safety)                      │
├─────────────────────────────────────────────────────────────┤
│ • Pre-sync snapshot of /home (local rollback)               │
│ • Pre-sync snapshot of /etc (local rollback)                │
│ • Pre-sync snapshot of VM subvolumes                        │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ btrfs send/receive Layer (VMs Only)                         │
├─────────────────────────────────────────────────────────────┤
│ • /var/lib/libvirt/images as subvolume                      │
│ • Efficient block-level transfer of VM disk images          │
│ • Incremental send with parent snapshots                    │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ Local Version Control (Per-Machine History)                 │
├─────────────────────────────────────────────────────────────┤
│ • etckeeper on each machine (/etc/.git, NOT synced)         │
│ • ~/system-state/.git (synced via Syncthing)                │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ System State Scripts                                        │
├─────────────────────────────────────────────────────────────┤
│ • capture-state.sh → package lists, service states          │
│ • diff-state.sh → detect /etc changes, update .stignore     │
│ • apply-state.sh → install packages, enable services        │
│ • sync-vm-btrfs.sh → btrfs send/receive for VMs             │
│ • create-sync-snapshot.sh → safety snapshots                │
└─────────────────────────────────────────────────────────────┘
```

### High-Priority Changes to Context.md and Plan.md

#### 1. **VM Storage: Replace QCOW2 with btrfs Snapshots**

**Changes needed**:
- Context.md line 102-112: Update VM handling section
- Plan.md Phase 5: Rewrite VM sync workflow

**New implementation**:
```bash
# Setup (one-time)
sudo btrfs subvolume create /var/lib/libvirt/images

# Before sync (on P17)
virsh shutdown windows-vm
sudo btrfs subvolume snapshot -r /var/lib/libvirt/images \
  /var/lib/libvirt/snapshots/images-2025-11-03

# Sync to XPS (incremental if parent exists)
sudo btrfs send -p /var/lib/libvirt/snapshots/images-2025-11-02 \
  /var/lib/libvirt/snapshots/images-2025-11-03 | \
  ssh xps sudo btrfs receive /var/lib/libvirt/snapshots/

# On XPS: Use latest snapshot
sudo btrfs subvolume snapshot /var/lib/libvirt/snapshots/images-2025-11-03 \
  /var/lib/libvirt/images
```

**Benefits over QCOW2**:
- No performance overhead (0% vs 5-10%)
- Near-instant snapshots
- Incremental block-level sync (faster than rsync of overlay files)

#### 2. **`/etc` Sync: Syncthing with `/etc/.stignore`**

**Changes needed**:
- Context.md line 59-72: Update system state management section
- Plan.md Phase 2: Remove `etc-tracked/` directory, update to `/etc/.stignore`
- Plan.md Phase 3: Update `diff-state.sh` to manage `.stignore` directly

**New `/etc/.stignore` format**:
```
# Exclude everything by default
*

# Machine-specific (never sync)
machine-id
hostname
adjtime
fstab

# etckeeper (local version control, not synced)
.git
.gitignore

# Tracked system config (always sync)
!hosts
!environment
!default/grub
!apt/sources.list.d/**
```

**Script changes**:
- `diff-state.sh`: Check files on both source and target `/etc` against `/etc/.stignore`
- For files without a rule: prompt and append pattern to `/etc/.stignore`
- Note: Only handles `/etc` tracking. Package management is separate (uses `capture-state.sh`)

#### 3. **etckeeper Integration**

**Changes needed**:
- Context.md line 59-72: Add etckeeper local usage
- Plan.md Phase 2: Update etckeeper section (local only, NOT synced)

**Implementation**:
- Install etckeeper on both machines (already in plan)
- etckeeper commits to `/etc/.git` locally (NOT synced, excluded in `/etc/.stignore`)
- Syncthing syncs selected files via `/etc/.stignore`
- Each machine's etckeeper tracks both local changes and synced files independently

**Benefits**:
- Per-machine version history
- Rollback capability (`git -C /etc revert <commit>`)
- No git repo sync risks (separate repos per machine)

#### 4. **Pre-Sync Snapshots for Rollback Safety**

**Changes needed**:
- Plan.md Phase 7: Add snapshot step to `prepare-for-sync.sh` (formerly `prepare-for-travel.sh`)

**New script: `create-sync-snapshot.sh`**:
```bash
#!/bin/bash
TIMESTAMP=$(date +%Y-%m-%d)

# Snapshot /home
sudo btrfs subvolume snapshot /home /snapshots/home-$TIMESTAMP

# Snapshot /etc
sudo btrfs subvolume snapshot /etc /snapshots/etc-$TIMESTAMP

# Snapshot VMs
sudo btrfs subvolume snapshot -r /var/lib/libvirt/images \
  /var/lib/libvirt/snapshots/images-$TIMESTAMP

echo "Snapshots created for $TIMESTAMP"
echo "Rollback: sudo btrfs subvolume delete /home && sudo btrfs subvolume snapshot /snapshots/home-$TIMESTAMP /home"
```

### Low-Priority (Keep Existing Approach)

#### 5. **Docker with btrfs Storage Driver**
- Known performance issues
- Doesn't solve machine-specific runtime state problem
- Manual export/import is safer and more explicit

#### 6. **k3s PVCs as Subvolumes**
- Adds complexity for marginal benefit
- Only valuable if PVCs are very large (multi-GB)

---

## Summary of Decisions

### What Changed from Original Plan

| Component | Original Approach | New Approach with btrfs |
|-----------|------------------|-------------------------|
| `/home` sync | Syncthing | **Keep Syncthing** (selective sync requirement) |
| `/etc` sync | Git repo with `etc-tracked/` and `.track-rules` | **Syncthing with `/etc/.stignore`** (simpler, no `.track-rules`) |
| `/etc` versioning | Git repo in `~/system-state/` | **etckeeper locally** (per-machine, NOT synced) |
| Package management | `capture-state.sh` exports manifests | **Keep existing** (works well, synced via `~/system-state/`) |
| VM storage | QCOW2 snapshots (5-10% overhead) | **btrfs subvolumes + send/receive** (0% overhead) |
| Rollback | Git history in `~/system-state/` | **btrfs snapshots** before sync |
| Docker | Manual export/import | **Keep manual** (btrfs driver has issues) |
| k3s PVCs | Syncthing for `~/k3s-volumes/` | **Keep Syncthing** (simple, works well) |

### Key Architectural Changes

1. **Syncthing for all file sync** (except VMs)
   - `/home` with `.stignore` for machine-specific exclusions
   - `/etc` with `.stignore` for selective tracking (no more `etc-tracked/` directory or `.track-rules`)
   - `~/system-state/` git repo syncs naturally (contains package manifests, scripts)

2. **btrfs only for VMs and snapshots**
   - Replace QCOW2 with btrfs subvolumes for VM storage
   - Use `btrfs send/receive` for efficient VM sync
   - Pre-sync snapshots of `/home`, `/etc`, VMs for rollback safety

3. **etckeeper stays local**
   - Each machine has independent `/etc/.git` (NOT synced, excluded in `/etc/.stignore`)
   - Provides version history and rollback per machine
   - Syncthing handles file transport via `/etc/.stignore`

4. **Simplified /etc tracking workflow**
   - No more `.track-rules` file or translation scripts
   - `/etc/.stignore` is the single source of truth
   - `diff-state.sh` checks both source and target `/etc`, prompts for files without rules, appends patterns to `.stignore` directly

5. **Separate package management from /etc sync**
   - `/etc` files sync via Syncthing (based on `/etc/.stignore`)
   - Package lists managed separately: `capture-state.sh` creates manifests in `~/system-state/`, synced via Syncthing

### What to Update in Documentation

#### Context.md
- **Line 31-42 (File Synchronization)**: Add `/etc` as Syncthing-synced folder with `/etc/.stignore`
- **Line 59-72 (System State Management)**: Update to reflect `/etc/.stignore` approach (no `.track-rules`), etckeeper local usage, separate package management
- **Line 102-112 (VM Handling)**: Replace QCOW2 with btrfs subvolumes + send/receive

#### Plan.md
- **Architecture diagram (lines 6-28)**: Add btrfs snapshot layer, show Syncthing for both `/home` and `/etc`
- **Phase 1 (Syncthing Setup)**: Add `/etc` as shared folder, exclude VM subvolume from Syncthing
- **Phase 2 (System State Repository)**: Remove `etc-tracked/` directory, no more `.track-rules`, update etckeeper section (local only)
- **Phase 3 (Core Scripts)**:
  - Update `capture-state.sh`: only package lists/services (not `/etc` files)
  - Update `diff-state.sh`: manage `/etc/.stignore` directly, check files on both source and target
  - Update `apply-state.sh`: only packages/services (not `/etc` files)
- **Phase 5 (VM Snapshot Sync)**: Complete rewrite to use btrfs subvolumes and send/receive
- **Phase 7 (User Workflow Scripts)**: Add `create-sync-snapshot.sh` step (not `create-travel-snapshot.sh`)

### Answered Questions (from original Open Questions)

1. **Is `/home` already a btrfs subvolume?** → Yes (Ubuntu 24.04 default), enables snapshots
2. **Acceptable complexity?** → Minimal. Only VMs use `btrfs send/receive`. Rest is Syncthing (simpler).
3. **Diverged snapshots?** → Non-issue. One machine used at a time (uni-directional workflow from source to target and then back from target to source).

### Final Architecture: Syncthing + btrfs Hybrid

**Use Syncthing for**: `/home`, `/etc`, `~/system-state/` (selective sync, conflict resolution)
**Use btrfs for**: VMs (efficient large-file sync), pre-travel snapshots (rollback safety)
**Use etckeeper for**: Local `/etc` version history (per-machine, not synced)

**Key insight**: Don't replace Syncthing with btrfs. Use btrfs to **enhance** Syncthing where it makes sense (VMs, snapshots).
