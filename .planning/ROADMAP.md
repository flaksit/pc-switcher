# Roadmap: PC-switcher

## Overview

The orchestration framework is already built (Job architecture, self-installing orchestrator, btrfs snapshot safety, config, logging, locking, terminal UI, three-tier testing, CI). What remains is the actual sync work: a series of self-contained modular sync Jobs layered on that framework. The journey starts with the smallest vertical slice that proves the end-to-end workflow — replicating user data between two machines and verifying it with a bidirectional round-trip (Phase 1) — then broadens scope job by job (packages, system config, Docker, VMs, k3s) until the machine is a near-complete replica, and closes with rollback so any sync is reversible.

## Phases

**Phase Numbering:**

- Integer phases (1, 2, 3): Planned milestone work.
- Decimal phases (2.1, 2.2): Urgent insertions (marked INSERTED), appearing between their surrounding integers in numeric order.

- [x] **Foundation: Core framework, testing, logging, CI** — Complete (pre-GSD; specs 001-004)
- [x] **Phase 1: Folder Sync MVP (User Data Sync)** — Single-command replication of configured folders (`/home` and `/root` by default) over rsync-over-SSH, per-folder exclusions and metadata preserved, proven by a bidirectional round-trip (completed 2026-06-30)
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

### Phase 1: Folder Sync MVP (User Data Sync)

**Goal**: A user can run one command to replicate configured folders (`/home` and `/root` by default) from the source machine to the target over rsync-over-SSH, with machine-specific items excluded and file metadata preserved — and the result is proven correct in both directions. The job is a generic folder-sync mechanism (per-folder include/exclude), usable for any path; `/root` is included because rsync must run as root anyway to preserve cross-owner files. (`/etc` and other system config remain Phase 3.)
**Depends on**: Foundation
**Requirements**: REQ-sync-scope-user-data, REQ-machine-specific-exclusions, REQ-sync-scope-file-metadata, REQ-manual-sync-workflow, REQ-terminal-ux
**Success Criteria** (what must be TRUE):

  1. Running `pc-switcher sync <target>` on machine A copies the configured folders (`/home` and `/root` by default) to machine B; after sync, every included file exists on B and is byte-identical to A (verified by checksum).
  2. File metadata is preserved: owner, group, permissions, POSIX ACLs, and modification timestamps on B match A for every synced file.
  3. Machine-specific items are never copied — SSH private keys (`.ssh/id_*`), Tailscale config (`.config/tailscale`), GPU/shader and fontconfig caches, and excluded browser/IDE caches (VS Code) are absent from B's synced tree — while dev-tool caches (uv, pip, cargo, npm) ARE included.
  4. The sync is reversible and exclusions hold both directions: after modifying, adding, and deleting files on B and running `pc-switcher sync <A>` from B, A reflects B's changes byte-identically with metadata preserved, and the same machine-specific exclusions are honored on this reverse sync.
  5. A VM-isolated integration test automates the full A→B, mutate-on-B, B→A round-trip and asserts criteria 1-4 — the developer-facing milestone success metric.

**Plans**: 18/18 plans complete

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

- [x] 01-06-PLAN.md — VM-isolated A→B/mutate/B→A round-trip integration test (success criterion 5)

**Gap Closure** *(from 01-VERIFICATION.md — closes 1 BLOCKER + 7 warnings/info findings)*

- [x] 01-07-PLAN.md — divergence-guard correctness: CR-01 false-divergence (tool-state filter), CR-02 fail-closed, WR-02 robust baseline + CR-01 unit/integration tests [wave 1]
- [x] 01-08-PLAN.md — IN-01 dead SIGINT cleanup removal, IN-02 progress-bar total counts only executed steps [wave 1]
- [x] 01-09-PLAN.md — WR-01 transferred-byte parsing, IN-03 `c`/`h` itemize change types, WR-03 pre-transfer divergence re-check (TOCTOU) [wave 2, depends on 01-07]

**Design Pivot — Gap Closure round 2** *(removes btrfs content-divergence detection; replaces it with the topology-based safety model — ADR-015, resolves CR-01/CR-02/WR-01/IN-01)*

- [x] 01-10-PLAN.md — decision record: ADR-015 (topology-based sync-safety model) + _index + CONTEXT supersession [wave 1]
- [x] 01-11-PLAN.md — remove content-divergence guard from FolderSyncJob + WR-01 config_sync removeprefix + prune obsolete tests [wave 1]
- [x] 01-12-PLAN.md — sync-history schema: add last_peer, remove btrfs generation store + tests [wave 2, depends on 01-11]
- [x] 01-13-PLAN.md — CLI --allow-out-of-order + orchestrator out-of-order/target-state step + last_peer recording; drop allow_divergence/allow_consecutive [wave 3, depends on 01-11, 01-12]
- [x] 01-14-PLAN.md — README sync-sequence fix + FULL deletion-log persistence test + integration test rework [wave 4, depends on 01-11, 01-13]

**UAT Gap Closure — round 3** *(from 01-UAT.md — 5 diagnosed UX/logging gaps found in hands-on verification)*

- [x] 01-15-PLAN.md — job-agnostic first-sync overwrite messaging: each SyncJob describes its own scope/mechanism (gap 1) [wave 1]
- [x] 01-16-PLAN.md — user-declined confirmations become a distinct SyncAbortedByUser outcome: logged once at WARNING, single calm CLI message, never CRITICAL/duplicated (gap 2) [wave 2, depends on 01-15]
- [x] 01-17-PLAN.md — single persistent Live with pause/resume around prompts + config-sync consistency under --dry-run (gaps 3, 5) [wave 1]
- [x] 01-18-PLAN.md — route interactive TUI log output through the single Live panel with a stderr fallback, fixing the live-progress flooding (gap 4) [wave 3, depends on 01-15, 01-16, 01-17]

### Phase 2: Package Management Sync

**Goal**: A user can replicate installed packages from source to target across all package sources, with conflicts and version mismatches detected and reported rather than silently overwritten.
**Depends on**: Phase 1
**Requirements**: REQ-sync-scope-packages, REQ-conflict-detection-no-resolution
**Scope note**: `/etc/apt` repository, keyring, pin, and apt-config state is Phase 2 territory (moved from Phase 3). Package data under `~/.var/app`, `~/snap/<app>/common`, and dotfiles stays Phase 1 `folder_sync` territory.
**Success Criteria** (what must be TRUE):

  1. After sync, the target has the same apt, snap, and flatpak packages installed as the source (verifiable by querying each package manager).
  2. Manually-installed .debs, custom PPAs, and install-script-sourced packages are reproduced on the target.
  3. Package conflicts and version mismatches between source and target are detected and reported before any destructive change; machine-specific packages are not forced onto the target.

**Plans**: 13/21 plans executed

Plans:
**Wave 1**

- [x] 02-01-PLAN.md — ADR-020 (declarative package convergence) + `/etc/apt` scope boundary move into Phase 2
- [x] 02-02-PLAN.md — `questionary` legitimacy checkpoint + batched-review primitive composed with the paused Live display

**Wave 2** *(blocked on Wave 1)*

- [x] 02-03-PLAN.md — TRACER: end-to-end apt-package install slice (capture → plan → review → apply) + the package-phase coordinator that gives every manager one batched review

**Wave 3** *(blocked on Wave 2)*

- [x] 02-05-PLAN.md — full diff-class taxonomy (removals, version mismatch, held/pinned, repo-unavailable) + `dpkg --compare-versions` + apt transaction simulation + the shared snap/flatpak/unreproducible item shapes
- [x] 02-13-PLAN.md — VM integration proof of the tracer path (install + dry-run)

**Wave 4** *(blocked on Wave 3)*

- [x] 02-04-PLAN.md — machine-local decision files, skip-always in both roles, non-overridable folder_sync exclusion
- [x] 02-06-PLAN.md — apt repository state as items: sources, keyrings, legacy trust keys, pins, apt config, in apt's required order, with staged privileged writes and transactional rollback

**Wave 5** *(blocked on Wave 4)*

- [x] 02-07-PLAN.md — unreproducible-item detection + snippet registry + on-the-fly snippet capture in the review
- [x] 02-08-PLAN.md — `snap_sync`: header-based capture, revision convergence without a hold, path export
- [x] 02-09-PLAN.md — `flatpak_sync`: scoped refs, per-scope remotes provisioned first, path export

**Wave 6** *(blocked on Wave 5)*

- [x] 02-10-PLAN.md — config registration, package-jobs-before-folder_sync ordering, automatic mirror exclusions

**Wave 7** *(blocked on Wave 6)*

- [x] 02-11-PLAN.md — VM integration tests for the whole-run contracts + validation record
- [x] 02-12-PLAN.md — user + living-spec documentation and end-to-end human verification

**Delta Replan** *(decision corrections after the executed phase was reviewed; the 13 shipped plans stand untouched)*

- [ ] 02-14-PLAN.md — rewrite ADR-020 to the corrected per-manager, four-job, self-pushed-snippet design [wave 1]
- [ ] 02-15-PLAN.md — remove the cross-manager coordinator; each package job reviews and applies inside its own execute() (D-24) [wave 1]
- [ ] 02-16-PLAN.md — classify apt collateral: auto proceeds, manual becomes a reviewable install-anyway/skip/abort item at plan time (D-30) [wave 2]
- [ ] 02-17-PLAN.md — new manual_installs_sync job owning unreproducible detection + snippet registry; skip-once is a valid resolution (D-15/D-18/D-21) [wave 3]
- [ ] 02-18-PLAN.md — manual_installs_sync pushes package-snippets.yaml itself via send_file(); config_sync reverts to config.yaml only (D-23) [wave 4]
- [ ] 02-19-PLAN.md — move jobs/package_*.py into jobs/packages/; delete empty apt/snap/flatpak config sections (D-31/D-32) [wave 5]
- [ ] 02-20-PLAN.md — migrate job behaviour docs out of configuration.md; correct docs/system living specs (D-33) [wave 6]
- [ ] 02-21-PLAN.md — rework VM integration for the new job/review shape; add apt-repository-state coverage closing broken-window #2 [wave 6]

### Phase 3: System & Application Configuration Sync

**Goal**: A user can replicate machine-independent application and system configuration from source to target, with conflicting system changes detected.
**Depends on**: Phase 2
**Requirements**: REQ-sync-scope-app-and-system-config
**Scope note**: The rest of `/etc` remains Phase 3 territory; `/etc/apt` is delivered in Phase 2.
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
| ----- | -------------- | ------ | --------- |
| 1. Folder Sync MVP (User Data Sync) | 18/18 | Complete    | 2026-07-03 |
| 2. Package Management Sync | 13/21 | In Progress|  |
| 3. System & Application Configuration Sync | 0/TBD | Not started | - |
| 4. Docker State Sync | 0/TBD | Not started | - |
| 5. VM State Sync | 0/TBD | Not started | - |
| 6. k3s Cluster Sync | 0/TBD | Not started | - |
| 7. Rollback Capability | 0/TBD | Not started | - |
