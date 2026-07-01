# Roadmap: PC-switcher

## Overview

The orchestration framework is already built (Job architecture, self-installing orchestrator, btrfs snapshot safety, config, logging, locking, terminal UI, three-tier testing, CI). What remains is the actual sync work: a series of self-contained modular sync Jobs layered on that framework. The journey starts with the smallest vertical slice that proves the end-to-end workflow — replicating user data between two machines and verifying it with a bidirectional round-trip (Phase 1) — then broadens scope job by job (packages, system config, Docker, VMs, k3s) until the machine is a near-complete replica, and closes with rollback so any sync is reversible.

## Milestones

- Foundation (Complete) — Core framework, testing, logging, CI. Shipped before GSD adoption.
- Milestone 1: Home-Sync MVP (current focus) — Phase 1. The first working sync job, proven by a bidirectional round-trip integration test.
- Milestone 2: Full System-State Replication (planned) — Phases 2-6. Packages, system/app config, Docker, VMs, k3s.
- Milestone 3: Reliability Hardening (planned) — Phase 7. Rollback from pre-sync snapshots.

## Phases

**Phase Numbering:**

- Integer phases (1, 2, 3): Planned milestone work.
- Decimal phases (2.1, 2.2): Urgent insertions (marked INSERTED), appearing between their surrounding integers in numeric order.

- [x] **Foundation: Core framework, testing, logging, CI** — Complete (pre-GSD; specs 001-004)
- [ ] **Phase 1: Home-Sync MVP (User Data Sync)** — Single-command replication of configured folders (`/home` and `/root` by default) over rsync-over-SSH, per-folder exclusions and metadata preserved, proven by a bidirectional round-trip
- [ ] **Phase 2: Package Management Sync** — Replicate apt/snap/flatpak/.deb/PPA/script packages with conflict and version-mismatch detection
- [ ] **Phase 3: System & Application Configuration Sync** — Replicate GNOME, cloud mounts, systemd, `/etc`, users/groups with conflicting-change detection
- [ ] **Phase 4: Docker State Sync** — Replicate images, containers, volumes, and cache with running-container / incompatible-state detection
- [ ] **Phase 5: VM State Sync** — Replicate KVM/virt-manager VMs with a powered-off guard and concurrent-use detection
- [ ] **Phase 6: k3s Cluster Sync** — Replicate single-node k3s state including PVCs with cluster validation and active-workload detection
- [ ] **Phase 7: Rollback Capability** — Restore a machine to its pre-sync state from automatic btrfs snapshots via `pc-switcher rollback`

## Phase Details

### Foundation (Complete)

**Goal**: Provide the orchestration framework every sync job builds on — Job architecture, self-installing orchestrator, btrfs snapshot safety, config, logging, locking, terminal UI, three-tier testing, and CI.
**Status**: Complete (shipped pre-GSD via specs 001-core, 002-testing-framework, 003-core-tests, 004-python-logging; living authority in `docs/system/`).
**Requirements**: REQ-core-001-infrastructure, REQ-testing-framework-002, REQ-core-tests-003, REQ-python-logging-004, REQ-feature-modular-architecture, REQ-environment-constraints
**Note**: Not a plannable GSD phase. Listed so the roadmap reflects what already exists; sync phases extend this framework rather than rebuild it.

### Phase 1: Home-Sync MVP (User Data Sync)

**Goal**: A user can run one command to replicate configured folders (`/home` and `/root` by default) from the source machine to the target over rsync-over-SSH, with machine-specific items excluded and file metadata preserved — and the result is proven correct in both directions. The job is a generic folder-sync mechanism (per-folder include/exclude), usable for any path; `/root` is included because rsync must run as root anyway to preserve cross-owner files. (`/etc` and other system config remain Phase 3.)
**Depends on**: Foundation
**Requirements**: REQ-sync-scope-user-data, REQ-machine-specific-exclusions, REQ-sync-scope-file-metadata, REQ-manual-sync-workflow, REQ-terminal-ux
**Success Criteria** (what must be TRUE):

  1. Running `pc-switcher sync <target>` on machine A copies the configured folders (`/home` and `/root` by default) to machine B; after sync, every included file exists on B and is byte-identical to A (verified by checksum).
  2. File metadata is preserved: owner, group, permissions, POSIX ACLs, and modification timestamps on B match A for every synced file.
  3. Machine-specific items are never copied — SSH private keys (`.ssh/id_*`), Tailscale config (`.config/tailscale`), GPU/shader and fontconfig caches, and excluded browser/IDE caches (VS Code) are absent from B's synced tree — while dev-tool caches (uv, pip, cargo, npm) ARE included.
  4. The sync is reversible and exclusions hold both directions: after modifying, adding, and deleting files on B and running `pc-switcher sync <A>` from B, A reflects B's changes byte-identically with metadata preserved, and the same machine-specific exclusions are honored on this reverse sync.
  5. A VM-isolated integration test automates the full A→B, mutate-on-B, B→A round-trip and asserts criteria 1-4 — the developer-facing milestone success metric.

**Plans**: 5/6 plans executed

Plans:
**Wave 1**

- [x] 01-01-PLAN.md — ADRs: rsync-over-SSH transport (D-04/D-05) and the unified dry-run contract (D-12)
- [x] 01-02-PLAN.md — folder_sync config schema + default /home,/root entries and exclusions
- [x] 01-03-PLAN.md — divergence-marker store, --allow-divergence flag, dry-run history skip

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 01-04-PLAN.md — FolderSyncJob validate() + target-divergence guard (the reliability linchpin)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 01-05-PLAN.md — FolderSyncJob execute(): rsync transfer, progress streaming, dry-run, marker recording

**Wave 4** *(blocked on Wave 3 completion)*

- [ ] 01-06-PLAN.md — VM-isolated A→B/mutate/B→A round-trip integration test (success criterion 5)

### Phase 2: Package Management Sync

**Goal**: A user can replicate installed packages from source to target across all package sources, with conflicts and version mismatches detected and reported rather than silently overwritten.
**Depends on**: Phase 1
**Requirements**: REQ-sync-scope-packages, REQ-conflict-detection-no-resolution
**Success Criteria** (what must be TRUE):

  1. After sync, the target has the same apt, snap, and flatpak packages installed as the source (verifiable by querying each package manager).
  2. Manually-installed .debs, custom PPAs, and install-script-sourced packages are reproduced on the target.
  3. Package conflicts and version mismatches between source and target are detected and reported before any destructive change; machine-specific packages are not forced onto the target.

**Plans**: TBD

### Phase 3: System & Application Configuration Sync

**Goal**: A user can replicate machine-independent application and system configuration from source to target, with conflicting system changes detected.
**Depends on**: Phase 2
**Requirements**: REQ-sync-scope-app-and-system-config
**Success Criteria** (what must be TRUE):

  1. After sync, GNOME desktop configuration, cloud mounts, and systemd service definitions on the target match the source.
  2. Machine-independent system config (`/etc` entries, startup services, users/groups) is replicated while machine-specific config is excluded.
  3. Conflicting system changes between the machines are detected and reported rather than silently overwritten.

**Plans**: TBD

### Phase 4: Docker State Sync

**Goal**: A user can replicate Docker state from source to target, with running containers or incompatible states detected so an unsafe sync is blocked.
**Depends on**: Phase 3
**Requirements**: REQ-sync-scope-docker
**Success Criteria** (what must be TRUE):

  1. After sync, the target has the same Docker images, container definitions, and named volumes as the source.
  2. Docker build cache / layer data is reproduced so target builds start warm.
  3. Running containers or incompatible Docker states are detected and reported, blocking an unsafe sync.

**Plans**: TBD

### Phase 5: VM State Sync

**Goal**: A user can replicate KVM/virt-manager VMs from source to target, with a guard that only suspended/powered-off VMs are synced and concurrent VM usage is detected.
**Depends on**: Phase 4
**Requirements**: REQ-sync-scope-vms
**Success Criteria** (what must be TRUE):

  1. After sync, KVM/virt-manager VM definitions and disk images on the target match the source and boot correctly.
  2. A sync is refused, with a clear report, when a VM is running or in use on either machine; only suspended or powered-off VMs are synced.
  3. VM metadata and storage are preserved so the migrated VM resumes or runs on the target without manual repair.

**Plans**: TBD

### Phase 6: k3s Cluster Sync

**Goal**: A user can replicate the local single-node k3s cluster state, including PVC-backed data, from source to target, with cluster validation and active-workload detection.
**Depends on**: Phase 5
**Requirements**: REQ-sync-scope-k3s
**Success Criteria** (what must be TRUE):

  1. After sync, the target's single-node k3s cluster state (resources and PVC data) matches the source.
  2. Cluster state is validated before sync, and active workloads are detected and reported to prevent an inconsistent copy.
  3. PVC-backed data is replicated so workloads on the target see the same persistent state as the source.

**Plans**: TBD

### Phase 7: Rollback Capability

**Goal**: A user can restore a machine to its pre-sync state from the btrfs snapshots taken automatically before each sync, via `pc-switcher rollback`.
**Depends on**: Phase 6
**Requirements**: REQ-reliability-principles
**Success Criteria** (what must be TRUE):

  1. `pc-switcher rollback` lists available pre-sync snapshots and restores a selected one, returning the `@` and `@home` subvolumes to their pre-sync state.
  2. After a rollback, the machine's user data and system state match the chosen pre-sync snapshot — no partial restore, and no data from the aborted or unwanted sync remains.
  3. Rollback is safe to run after an interrupted or failed sync and reports clearly what was restored (audit trail).

**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 7

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Home-Sync MVP (User Data Sync) | 5/6 | In Progress|  |
| 2. Package Management Sync | 0/TBD | Not started | - |
| 3. System & Application Configuration Sync | 0/TBD | Not started | - |
| 4. Docker State Sync | 0/TBD | Not started | - |
| 5. VM State Sync | 0/TBD | Not started | - |
| 6. k3s Cluster Sync | 0/TBD | Not started | - |
| 7. Rollback Capability | 0/TBD | Not started | - |
