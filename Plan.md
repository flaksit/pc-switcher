# Design and Implementation Plan

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│ Syncthing Layer (Automatic File Sync)                       │
├─────────────────────────────────────────────────────────────┤
│ • /home/user (with selective .cache)                        │
│ • /etc (with .stignore for selective tracking)              │
│ • ~/system-state/ (git repo)                                │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ btrfs Snapshot Layer (Rollback Safety)                      │
├─────────────────────────────────────────────────────────────┤
│ • Pre-sync snapshots of /home, /etc, VM subvolumes          │
│ • Local rollback capability per machine                     │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ System State Management (Manual Trigger)                    │
├─────────────────────────────────────────────────────────────┤
│ • capture-state.sh → Export packages, services              │
│ • diff-state.sh → Interactive review, manage /etc/.stignore │
│ • apply-state.sh → Install packages, update configs         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ Special Handlers (Separate Workflows)                       │
├─────────────────────────────────────────────────────────────┤
│ • VM: btrfs send/receive (block-level incremental sync)     │
│ • Docker: Volume backup/restore                             │
│ • k3s: Manifest apply + PV sync via hostPath                │
│ • VS Code: Settings Sync (automatic) + script backup        │
└─────────────────────────────────────────────────────────────┘
```

## Sync Safety Requirements

⚠️ **CRITICAL**: The target machine (the one receiving synced files) **must have the user fully logged out** before Syncthing syncs files and before running `apply-state.sh`.

**Why logout is essential**:

When a user is logged in with running applications, the following corruption risks exist:

1. **Database Corruption** (Critical)
   - Firefox, Chrome, VS Code, and many system services use SQLite databases
   - These apps hold file handles open while they're running
   - If Syncthing modifies a database file while the app has it open, the database can corrupt
   - Corrupted databases lead to app crashes, lost settings, or loss of bookmarks/history

2. **Application Crashes & Data Loss** (High)
   - Config files changing during runtime can cause crashes
   - Applications cache file contents in memory; they may overwrite synced changes when they exit
   - Example: Syncthing syncs updated `.bashrc`, but shell doesn't re-read it; user modifications in memory overwrite the sync on exit
   - Example: GNOME settings daemon crashes if dconf files change underneath it

3. **Container/Service Data Loss** (High)
   - Docker containers writing to volumes while sync happens
   - k3s pods writing to PersistentVolumes while sync happens
   - Systemd services writing to config files during sync

4. **Race Conditions** (Medium)
   - Syncthing writes conflicting with app writes can create partial/invalid files
   - Files in an inconsistent state between Syncthing and app reads

**Safe Workflow**:
1. **Before sync begins**: Log out of the target machine (no user sessions, no running applications)
2. **During sync**: Syncthing safely syncs files to filesystem
3. **After sync completes**: User logs back in
4. **Then run state scripts**: `apply-state.sh` makes package/service changes with no competing applications

**How to log out** (on XPS):
```bash
# Option 1: From GNOME Desktop
# Use system menu → Log Out

# Option 2: From terminal (if SSH'd in)
sudo systemctl isolate multi-user.target  # Drops to console login (no X session)

# Option 3: Check if anyone is logged in
who    # Shows active user sessions

# To fully verify logout from remote
ssh xps "who | grep -v root"  # Should return nothing (or only root)
```

## Implementation Phases

### Phase 0: Initial Sync Strategy (Hybrid Approach)
**Prepare for first sync to avoid conflicts**

⚠️ **CRITICAL SAFETY REQUIREMENT**: Before any sync, the target machine (XPS) **must be fully logged out**. Active user sessions with running applications can lead to database corruption, application crashes, and data loss when Syncthing modifies files. See "Sync Safety" section below for details.

1. **Pre-flight checks (on both machines)**:
   ```bash
   # Identify what will conflict
   rsync -avnc --delete p17:/home/<user>/ /home/<user>/ > ~/sync-preview.txt
   # Review preview - look for important files
   grep -E '\.(bashrc|gitconfig|ssh)' ~/sync-preview.txt
   ```

2. **btrfs pre-sync snapshot of XPS** (covers all critical configs):
   - See Phase 7: `./scripts/create-sync-snapshot.sh` creates snapshots of `/home`, `/etc`, and VMs
   - This handles rollback for all critical configs (`.bashrc`, `.gitconfig`, `.ssh/`, VS Code settings, etc.)
   - No separate manual backup needed

3. **Review `.stignore` patterns** before first sync (see Phase 1 for full list)
   - Add machine-specific files that should NOT sync:
     ```
     .ssh/id_*        # SSH keys
     .config/tailscale
     ```

4. **Configure Syncthing carefully**:
   - Install on both machines (see Phase 1)
   - On P17: Configure folder as "Send & Receive"
   - On XPS: Configure folder as "Receive Only" initially
     - In Web UI: Folder → Edit → Advanced → Folder Type: "Receive Only"
   - Enable "Ignore Delete" during initial sync (both machines)

5. **Let P17 content populate XPS** and wait for sync completion

6. **Review conflicts on XPS**:
   ```bash
   # Count conflicts
   find ~ -name "*.sync-conflict-*" -type f | wc -l

   # Review important ones
   find ~ -name ".bashrc.sync-conflict-*"
   diff ~/.bashrc ~/.bashrc.sync-conflict-*

   # Merge desired content from conflict files
   # Delete conflicts you don't need
   ```

7. **Switch XPS to bidirectional**:
   - Change folder type from "Receive Only" to "Send & Receive"
   - Disable "Ignore Delete" on both machines
   - Remaining conflict files will sync to P17 as backups

### Phase 1: Syncthing Setup
**Install and configure on both machines**

1. Install: `sudo apt install syncthing`
2. Enable user service: `systemctl --user enable --now syncthing`
3. Configure via Web UI (http://localhost:8384):
   - Add other machine as device
   - Create shared folders:
     - `/home/<user>` (with folder type per Phase 0)
     - `/etc` (synced for selective files via `.stignore`)
     - `~/system-state/` (new folder to create)
   - Exclude from Syncthing: VM subvolume at `/var/lib/libvirt/images` (synced separately via btrfs send/receive)

4. Create `.stignore` for `/home`:
```
# Exclude all caches by default
.cache

# Include valuable dev caches
!.cache/pip
!.cache/uv
!.cache/pypoetry
!.npm
!.cargo/registry
!.cargo/git
!.m2/repository
!.gradle/caches

# Exclude browser caches
.mozilla/firefox/*/Cache*
.mozilla/firefox/*/cache2
.config/google-chrome/*/Cache*
.config/chromium/*/Cache*

# Exclude system caches
.local/share/Trash
.thumbnails
.cache/thumbnails
.cache/mesa_shader_cache
.cache/fontconfig
.local/share/gvfs-metadata

# IDE caches
.config/Code/Cache*
.config/Code/CachedData
.config/Code/GPUCache
.config/Code/logs

# Temp files
*.tmp
*.temp

# Machine-specific files (do not sync)
.ssh/id_*
.ssh/authorized_keys
.config/tailscale

# Exclude VM and container storage
.local/share/libvirt
.local/share/containers
```

5. Create `.stignore` for `/etc`:
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

6. Verify sync completes successfully

### Phase 2: System State Repository
**Create git-tracked state management**

1. Initialize on primary machine (e.g., P17):
```bash
mkdir -p ~/system-state/{packages,services,users,scripts,vscode,docker,k3s,vm}
cd ~/system-state
git init
```

2. Install etckeeper on both machines (local version control, NOT synced):
```bash
sudo apt install etckeeper
sudo etckeeper init
```
   - etckeeper commits to `/etc/.git` locally on each machine
   - Excluded from Syncthing via `/etc/.stignore`
   - Provides per-machine version history and rollback capability

### Phase 3: Core Scripts
**Implement in `~/system-state/scripts/`**

#### `capture-state.sh`
**Purpose**: Export current machine state (packages and services only)

**Tasks**:
- Package lists:
  - `dpkg --get-selections > packages/apt-selections.txt`
  - `apt-mark showmanual > packages/apt-manual.txt`
  - `snap list > packages/snap-list.txt`
  - `flatpak list --app > packages/flatpak-list.txt`
  - `ls /etc/apt/sources.list.d/ > packages/ppa-list.txt`
  - Copy actual PPA files to `packages/ppas/`
- Services:
  - `systemctl list-unit-files --state=enabled > services/system-enabled.txt`
  - `systemctl --user list-unit-files --state=enabled > services/user-enabled.txt`
- Users: Copy `/etc/{passwd,group,shadow}` to `users/`
- Git commit with timestamp and hostname

#### `diff-state.sh`
**Purpose**: Show differences and manage `/etc/.stignore` tracking rules

**Tasks**:
- Compare package lists (additions/removals)
- Compare service states
- Check both source and target `/etc`:
  - Files already in `/etc/.stignore`: show diffs
  - Files NOT in `/etc/.stignore`: prompt "track? [always/never]"
  - Append decisions directly to `/etc/.stignore`
- Generate summary report
- No changes applied (read-only)

#### `apply-state.sh`
**Purpose**: Apply changes to target machine (packages and services only)

**Tasks**:
- Install missing packages:
  - `dpkg --set-selections < packages/apt-selections.txt && apt-get dselect-upgrade`
  - Install snaps and flatpaks from lists
  - Add PPAs and install packages
- Optional: Remove extra packages (prompt user)
- Enable/disable services as needed
- Update users/groups (with safety checks)
- Note: `/etc` files sync automatically via Syncthing (based on `/etc/.stignore`)
- Git commit applied state

### Phase 4: Container Workflows

#### Docker
**Scripts in `~/system-state/docker/`**

`export-docker.sh`:
- List images: `docker images --format json > images-list.json`
- Export critical images: `docker save <image> | gzip > images/<image>.tar.gz`
- Backup volumes: `docker run --rm -v <vol>:/data -v ~/system-state/docker/volumes:/backup ubuntu tar czf /backup/<vol>.tar.gz -C /data .`
- Note: Most images can rebuild, only export critical/custom ones

`import-docker.sh`:
- Load images: `docker load < images/<image>.tar.gz`
- Restore volumes: create volume, then extract tar

#### k3s
**Structure in `~/system-state/k3s/`**

- Keep manifests in `~/projects/k3s-manifests/` (synced naturally)
- Create `~/k3s-volumes/` for PersistentVolumes using hostPath
- Script to backup/restore PVC data when needed:
```bash
kubectl exec <pod> -- tar czf - /data > k3s/volumes/<pvc>.tar.gz
```

### Phase 5: VM Snapshot Sync (btrfs send/receive)

**One-time setup**:
1. Create btrfs subvolume for VM images:
```bash
sudo btrfs subvolume create /var/lib/libvirt/images
```

**Before each sync**:
1. Shutdown VM (critical!)
2. Create read-only snapshot:
```bash
sudo btrfs subvolume snapshot -r /var/lib/libvirt/images \
  /var/lib/libvirt/snapshots/images-$(date +%Y-%m-%d)
```

**Sync scripts in `~/system-state/vm/`**:

`sync-vm-to-xps.sh`:
```bash
# Send incremental snapshot to XPS
sudo btrfs send -p /var/lib/libvirt/snapshots/images-PREVIOUS-DATE \
  /var/lib/libvirt/snapshots/images-CURRENT-DATE | \
  ssh xps sudo btrfs receive /var/lib/libvirt/snapshots/
```

`sync-vm-to-p17.sh`: Same in reverse

**On target machine**:
- Create writable snapshot from latest received snapshot for actual VM use

**Benefits**:
- Incremental block-level transfer (only changed blocks sent)
- No performance overhead
- Near-instant snapshots (read-only)

### Phase 6: VS Code Integration

**Enable Settings Sync**:
1. In VS Code: Ctrl+Shift+P → "Settings Sync: Turn On"
2. Sign in with GitHub or Microsoft account
3. Select what to sync (all options)

**Backup script in `~/system-state/vscode/`**:

`capture-vscode.sh`:
```bash
code --list-extensions > extensions-list.txt
cp ~/.config/Code/User/settings.json .
cp ~/.config/Code/User/keybindings.json .
```

`apply-vscode.sh`:
```bash
cat extensions-list.txt | xargs -L 1 code --install-extension
```

(Backup only, Settings Sync is primary method)

### Phase 7: User Workflow Scripts

**Create `~/scripts/create-sync-snapshot.sh`**:
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
```

**Create `~/scripts/prepare-for-travel.sh`**:
```bash
#!/bin/bash
echo "=== Pre-Travel Sync ==="

# SAFETY CHECK: Verify target machine is logged out
echo "Checking if XPS is logged out..."
if ssh xps "who | grep -q '.'"; then
    echo "❌ ERROR: User is still logged into XPS!"
    echo "Please log out of XPS completely before syncing."
    echo "Running the following on XPS will log out:"
    echo "  sudo systemctl isolate multi-user.target"
    exit 1
fi
echo "✓ XPS is logged out (safe to proceed)"

./scripts/create-sync-snapshot.sh
cd ~/system-state
./scripts/capture-state.sh
echo "Waiting for Syncthing sync..."
read -p "Syncthing complete? Press enter when ready..."
./scripts/diff-state.sh
read -p "Apply changes? [y/n] " -n 1 -r
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Verify XPS still logged out before final sync
    if ssh xps "who | grep -q '.'"; then
        echo "❌ ERROR: User logged into XPS during sync!"
        echo "Please log out before applying state."
        exit 1
    fi
    ./scripts/apply-state.sh
    echo "Review Syncthing log for deleted files/folders"
    echo ""
    echo "Now log into XPS to complete the sync workflow"
fi
echo "Ready for travel!"
```

**Create `~/scripts/post-travel-sync.sh`**: Similar script for returning home

## Testing and Validation Plan

### Test 1: Basic Syncthing
1. Create test file on P17: `echo "test" > ~/sync-test.txt`
2. Wait for sync, verify on XPS
3. Modify on XPS, verify syncs back
4. Test conflict: modify same file offline on both, verify `.sync-conflict` creation

### Test 2: Package Management
1. On P17: Install test package `sudo apt install htop`
2. Run `capture-state.sh`
3. Wait for Syncthing
4. On XPS: Run `diff-state.sh` (should show htop addition)
5. Run `apply-state.sh`, verify htop installs
6. Reverse test: remove on XPS, sync, verify diff shows removal

### Test 3: /etc Tracking
1. Modify `/etc/hosts` on P17
2. Capture state (should prompt to track if not already)
3. Sync to XPS
4. Diff and apply, verify `/etc/hosts` updates
5. Test "always" and "never" rules persist

### Test 4: Cache Sync
1. On P17: `pip install requests` (creates cache)
2. Wait for Syncthing
3. On XPS: Verify `.cache/pip/` contains cached packages
4. Install same package, should use cache (no download)

### Test 5: VM Snapshot Sync
1. Create/modify file in Windows VM on P17
2. Shutdown VM, run `sync-vm-to-xps.sh`
3. Start VM on XPS, verify file exists
4. Reverse sync back to P17

### Test 6: Docker Workflow
1. Create test volume on P17: `docker volume create test-vol`
2. Add data: `docker run --rm -v test-vol:/data ubuntu sh -c "echo hello > /data/test.txt"`
3. Export volume with script
4. Sync to XPS
5. Import volume, verify data

### Test 7: k3s Manifests
1. Deploy test app on P17 k3s
2. Manifests in `~/projects/k3s-manifests/` sync automatically
3. On XPS: `kubectl apply -f ~/projects/k3s-manifests/`
4. Verify app deploys

### Test 8: VS Code Settings Sync
1. Install extension on P17
2. Wait for Settings Sync
3. Open VS Code on XPS, verify extension auto-installs

## Success Criteria
- [ ] Files sync bidirectionally without data loss
- [ ] Package installations replicate correctly
- [ ] /etc tracking rules persist and apply correctly
- [ ] VM works on both machines after sync
- [ ] Docker volumes restore correctly
- [ ] k3s workloads deploy from synced manifests
- [ ] VS Code identical on both machines
- [ ] Dev tool caches work (no re-downloads)
- [ ] Sync completes in <5 minutes on LAN
- [ ] No permission/ownership issues on synced files

## Maintenance Notes
- Run `capture-state.sh` before leaving either machine
- Verify Syncthing "Up to Date" before proceeding
- On target machine: Create pre-sync snapshot before running `apply-state.sh` (rollback insurance for silent deletions)
- Review `diff-state.sh` output before applying
- After applying state: Check Syncthing logs for deleted files/folders (deletions sync without prompts; logs show what was removed)
- Git history in `~/system-state/` provides rollback capability
- Periodically review `.track-rules` and `.stignore` patterns
- Monitor Syncthing logs for conflicts or errors
