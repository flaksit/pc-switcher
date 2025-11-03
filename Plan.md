# Design and Implementation Plan

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│ Syncthing Layer (Automatic File Sync)                       │
├─────────────────────────────────────────────────────────────┤
│ • /home/user (with selective .cache)                        │
│ • ~/system-state/ (git repo)                                │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ System State Management (Manual Trigger)                    │
├─────────────────────────────────────────────────────────────┤
│ • capture-state.sh → Export packages, services, /etc        │
│ • diff-state.sh → Interactive review with tracking rules    │
│ • apply-state.sh → Install packages, update configs         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ Special Handlers (Separate Workflows)                       │
├─────────────────────────────────────────────────────────────┤
│ • VM: QCOW2 snapshot sync                                   │
│ • Docker: Volume backup/restore                             │
│ • k3s: Manifest apply + PV sync via hostPath                │
│ • VS Code: Settings Sync (automatic) + script backup        │
└─────────────────────────────────────────────────────────────┘
```

## Implementation Phases

### Phase 1: Syncthing Setup
**Install and configure on both machines**

1. Install: `sudo apt install syncthing`
2. Enable user service: `systemctl --user enable --now syncthing`
3. Configure via Web UI (http://localhost:8384):
   - Add other machine as device
   - Create shared folders:
     - `/home/<user>` (excluding patterns below)
     - `~/system-state/` (new folder to create)

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

# Tailscale (machine-specific)
.config/tailscale

# Exclude VM and container storage
.local/share/libvirt
.local/share/containers
```

5. Verify sync completes successfully

### Phase 2: System State Repository
**Create git-tracked state management**

1. Initialize on primary machine (e.g., P17):
```bash
mkdir -p ~/system-state/{packages,etc-tracked,services,users,scripts,vscode,docker,k3s}
cd ~/system-state
git init
```

2. Install etckeeper on both machines:
```bash
sudo apt install etckeeper
sudo etckeeper init
```

3. Create `.track-rules` template:
```
# Format: include|exclude|always|never <path>
# Examples:
# always /etc/fstab
# never /etc/hostname
```

### Phase 3: Core Scripts
**Implement in `~/system-state/scripts/`**

#### `capture-state.sh`
**Purpose**: Export current machine state

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
- `/etc` tracking:
  - Generate manifest: `find /etc -type f -exec sha256sum {} \; > etc-manifest.txt`
  - Copy tracked files (per `.track-rules`) to `etc-tracked/`
- Git commit with timestamp and hostname

#### `diff-state.sh`
**Purpose**: Show differences and interactively manage tracking

**Tasks**:
- Compare package lists (additions/removals)
- Compare service states
- Compare `/etc` manifests:
  - Files in tracking rules: show diffs
  - Files NOT in tracking rules but changed: prompt "track? [y/n/always/never/diff]"
  - Update `.track-rules` based on responses
- Generate summary report
- No changes applied (read-only)

#### `apply-state.sh`
**Purpose**: Apply changes to target machine

**Tasks**:
- Install missing packages:
  - `dpkg --set-selections < packages/apt-selections.txt && apt-get dselect-upgrade`
  - Install snaps and flatpaks from lists
  - Add PPAs and install packages
- Optional: Remove extra packages (prompt user)
- Enable/disable services as needed
- For each tracked `/etc` file:
  - Show diff
  - Prompt: "apply? [y/n/diff/skip-all]"
  - Apply if approved
- Update users/groups (with safety checks)
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

### Phase 5: VM Snapshot Sync

**Setup QCOW2 snapshots**:
1. Convert existing VM to QCOW2 if needed
2. Create snapshot: `qemu-img create -f qcow2 -b base.qcow2 -F qcow2 overlay.qcow2`
3. Configure VM to use overlay file

**Scripts in `~/system-state/vm/`**:

`sync-vm-to-xps.sh`:
```bash
# Shutdown VM first (critical!)
virsh shutdown <vm-name>
# Wait for shutdown
# Sync overlay file only
rsync -avP --inplace /var/lib/libvirt/images/overlay.qcow2 xps:/var/lib/libvirt/images/
```

`sync-vm-to-p17.sh`: Same in reverse

**First-time**: Sync base image once (50GB), then only overlay (~1-5GB)

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

**Create `~/scripts/prepare-for-travel.sh`**:
```bash
#!/bin/bash
echo "=== Pre-Travel Sync ==="
cd ~/system-state
./scripts/capture-state.sh
echo "Waiting for Syncthing sync..."
# Could add: syncthing cli check or just manual verification
read -p "Syncthing complete? Press enter when ready..."
./scripts/diff-state.sh
read -p "Apply changes? [y/n] " -n 1 -r
if [[ $REPLY =~ ^[Yy]$ ]]; then
    ./scripts/apply-state.sh
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
- Review `diff-state.sh` output before applying
- Git history in `~/system-state/` provides rollback capability
- Periodically review `.track-rules` and `.stignore` patterns
- Monitor Syncthing logs for conflicts or errors
