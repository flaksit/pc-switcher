# Context and Decisions

## System Overview
Two Ubuntu 24.04 laptops need synchronization:
- **P17**: Heavy laptop, stays at home, primary work machine
- **XPS 13**: Light laptop, for travel (once a week), used offline when on the road

**Sync Pattern**: Uni-directional workflow (work on ONE machine at a time, sync between uses). Manual trigger, no real-time sync required. Both machines on same LAN (1Gb ethernet) during sync.

## Scope Requirements

### What Must Sync
1. **User data**: Entire `/home` directory
2. **Installed packages**: apt, snap, flatpak, manual .debs, custom PPAs, install scripts
3. **Application configurations**: All app settings including GNOME Desktop, cloud mounts, systemd services
4. **System configurations**: Selected `/etc` files, startup services, users/groups
5. **File metadata**: Owner, permissions, ACLs must be preserved
6. **VMs**: KVM/virt-manager VMs (~50GB Windows VM, occasional use)
7. **Containers**: Docker (dev containers, image building), k3s cluster with PVCs

### Installation Patterns
- Mix of system and user-space installations
- User prefers user-space when possible (`~/.local`)
- Custom PPAs in use
- Manual .deb installations
- Direct install scripts (primarily to `~/.local`)
- GNOME features: Dropbox (gvfs), Cloud Accounts for Google Drive (could switch to rclone if easier)

## Key Design Decisions

### File Synchronization: Syncthing
**Chosen approach**: Syncthing for all file-based sync (bidirectional capability, manual workflow)

**Rationale**:
- Handles offline changes elegantly with conflict resolution
- Automatic when both machines on LAN, but waitable (user controls when to proceed)
- Efficient block-level sync
- Built-in conflict handling (creates `.sync-conflict` files)
- Resumes interrupted transfers

**Syncthing syncs**:
- `/home` directory (with selective `.stignore` rules)
- `/etc` directory (with selective `.stignore` rules managed by `diff-state.sh`)
- `~/system-state/` git repository

**Deletion handling**: Syncthing syncs deletions silently (no prompts or approval needed). Files deleted on source are deleted on target. Review Syncthing logs after applying state to see what was removed.

**Snapshot strategy**: Take pre-sync snapshots on both source and target machines before running anything else. Use the snapshots as base for further actions (rollback insurance for unexpected deletions).

**Workflow**: Power on XPS → wait for Syncthing "Up to Date" → create target snapshot → apply system state → review deletion logs → travel

### Cache Strategy: Selective Sync (Option A)
**Include** (high value, low churn):
- `.cache/pip/`, `.cache/uv/`, `.cache/pypoetry/` - Python dev tools
- `.npm/`, `.cargo/registry/`, `.cargo/git/` - Node/Rust
- `.m2/repository/`, `.gradle/caches/` - Java tools

**Exclude** (low value or harmful):
- Browser caches (Firefox, Chrome) - too much churn, marginal benefit
- System caches (thumbnails, GPU shaders, fontconfig) - hardware-specific, can cause corruption
- IDE caches (VS Code Cache directories) - can cause crashes during sync
- `.local/share/Trash/`

**Rationale**: Dev tool caches save GBs over cellular (high ROI), browser caches not worth sync overhead. Reduces sync from 20GB to ~12GB and 500k to ~350k files.

### System State Management: Custom Scripts + etckeeper
**Approach**: Git-tracked state repository synced via Syncthing, `/etc` synced via Syncthing with selective rules

**Components**:
1. **etckeeper**: Version control for `/etc` locally on each machine (NOT synced, excluded in `/etc/.stignore`)
2. **Custom scripts**: `capture-state.sh`, `diff-state.sh`, `apply-state.sh` manage packages and services
3. **Git repository**: `~/system-state/` containing manifests, scripts
4. **Syncthing**: Syncs selected `/etc` files via `/etc/.stignore` (managed by `diff-state.sh`)

**Key insight on `/etc` sync**:
- Use etckeeper locally on each machine for per-machine version history (NOT synced)
- `/etc/.stignore` is single source of truth for which files sync between machines
- `diff-state.sh` checks both source and target `/etc`, prompts user for files without tracking rules
- User decisions appended directly to `/etc/.stignore`

### Docker: Export/Import, NOT Direct Copy
**Critical**: `/var/lib/docker/` must NOT be copied directly

**Why it breaks**:
- Overlay2 filesystem with symlinks and layer references breaks when copied
- Binary databases (repositories.json, local-kv.db) corrupt if copied while running or with timestamp changes
- Runtime state (network veth pairs, iptables, cgroups) doesn't transfer
- Hardware-specific networking configs

**Correct approach**:
- Docker images: `docker save/load` or let them rebuild
- Docker volumes: tar backup/restore or use bind mounts to `~/docker-volumes/`
- Compose files: Already in home directory, synced naturally
- Dev containers: Config in `.devcontainer/` (synced), images rebuild automatically

### k3s: Manifests Only, NOT Cluster State
**Critical**: `/var/lib/rancher/k3s/` must NOT be copied directly

**Why it breaks**:
- Certificates embed hostnames/IPs (machine-specific)
- etcd/SQLite database contains node identity and cluster state tied to specific machine
- CNI networking state (IP allocations, namespaces) is kernel-specific
- Both machines would have conflicting cluster identities

**Correct approach**:
- Sync manifests: `~/projects/k3s-manifests/` (YAML files)
- PersistentVolumes: Use `hostPath` pointing to `~/k3s-volumes/` (synced by Syncthing)
- Alternative: Backup/restore PVC data with kubectl exec + tar

### VM Handling: btrfs Subvolumes + send/receive
**Approach**: Native filesystem-level sync using btrfs snapshots and incremental transfer

**Strategy**:
- Store VM images in btrfs subvolume (e.g., `/var/lib/libvirt/images` as subvolume)
- Before sync: Create read-only `btrfs subvolume snapshot` of VM subvolume (instant, copy-on-write)
- Sync via `btrfs send/receive` piped through SSH (sends only changed blocks between snapshots)
- No performance overhead vs raw disk (runs on btrfs with copy-on-write)

**Benefits**:
- Near-instant snapshots (no VM shutdown delay)
- Incremental block-level sync (faster than rsync of overlay files)
- No performance penalty
- Snapshot history on each machine for rollback

**Rationale**: Eliminates QCOW2 complexity layer, uses native filesystem capabilities, improves performance

### VS Code: Built-in Settings Sync + Script Backup (Option A)
**Primary method**: VS Code Settings Sync (cloud-based, Microsoft/GitHub account)

**What it syncs**:
- Settings, keybindings, snippets ✅
- **Extensions** (the gap Syncthing doesn't cover) ✅
- UI state, opened folders ✅

**Backup method**: Script to export extension list to `~/system-state/vscode/`

**Rationale**: 
- Settings Sync is automatic, handles extensions seamlessly
- Syncthing syncs config files but NOT extensions (1-5GB, node_modules, platform-specific binaries)
- Combining both gives automatic daily sync + disaster recovery capability

### Additional Exclusions
**Tailscale**: Config/keys are machine-specific, must NOT sync (different identity per laptop)

**Future considerations**:
- Cron jobs: Not currently used, but system-state will track when added
- Systemd timers: Same as above (e.g., for backup triggers)
- Printer config: Not needed for sync

## Data Scale
- Current home: 20GB, 500k files (with full `.cache`)
- After selective cache: ~12GB, ~350k files
- VM: 50GB (separate sync)
- Expected sync time: 3-5 minutes initial, <1 minute incremental

## Backup Strategy
Treated separately from sync (Synology NAS target). Syncthing available on Synology if desired for integration.

## Network & Security
- Sync only on trusted home LAN
- No internet sync required (machines never online simultaneously)
- Tailscale for remote server access (machine-specific, excluded from sync)
