# Requirements: PC-switcher

**Defined:** 2026-06-30

**Core Value:** After a single sync command, the target machine is a faithful, reliable replica of the source — no data loss, conflicts detected and reported, file metadata intact.

These requirements carry the full project vision (all sync scopes plus workflow, UX, and reliability). They use the semantic IDs established during ingest so traceability back to the source ADRs/PRDs/SPECs is preserved. Foundation requirements are already shipped; the remainder are sequenced across the roadmap milestones.

## v1 Requirements

### Vision

- [ ] **REQ-near-full-state-replication**: Keep multiple Linux desktop machines in sync with minimal friction, aiming at near-full system-state replication (not just user data). Generic to N machines; concrete case is two laptops. Only one machine is active at a time; no simultaneous bi-directional sync.

### Sync Scope

- [x] **REQ-sync-scope-user-data**: Sync `/home` and `/root` (documents, code, configs, application data) via a generic per-folder include/exclude mechanism usable for any path. Include dev-tool caches (uv, pip, cargo, npm) and VS Code `User/` state; exclude regenerable caches (VS Code cache dirs, browser caches). (`/root` is included here because rsync must run as root to preserve cross-owner files; `/etc` and other system config remain REQ-sync-scope-app-and-system-config, Phase 3.)
- [ ] **REQ-sync-scope-packages**: Sync installed packages across apt, snap, flatpak, manual .debs, custom PPAs, and install-script packages; detect package conflicts / version mismatches. Includes the repository state those installs depend on: `/etc/apt/sources.list.d`, `/etc/apt/keyrings`, `/etc/apt/trusted.gpg.d`, `/etc/apt/preferences.d`, `/etc/apt/apt.conf.d`, flatpak remotes, and snap channels. (`/etc/apt` moved here from REQ-sync-scope-app-and-system-config, Phase 2.)
- [ ] **REQ-sync-scope-app-and-system-config**: Sync application configuration (GNOME desktop, cloud mounts, systemd services) and machine-independent system config (`/etc`, startup services, users/groups); detect conflicting system changes. (`/root` moved to REQ-sync-scope-user-data, Phase 1; `/etc/apt` is delivered by REQ-sync-scope-packages, Phase 2 — the rest of `/etc` remains here, Phase 3.)
- [x] **REQ-sync-scope-file-metadata**: Preserve file metadata — owner, group, permissions, POSIX ACLs, timestamps.
- [ ] **REQ-sync-scope-docker**: Sync Docker images, containers, volumes, and cache; detect running containers or incompatible states.
- [ ] **REQ-sync-scope-vms**: Sync KVM/virt-manager VMs; VMs must be suspended/powered off before sync; detect concurrent VM usage.
- [ ] **REQ-sync-scope-k3s**: Sync local single-node k3s cluster state including PVCs; validate cluster state and detect active workloads.

### Exclusions and Safety

- [x] **REQ-machine-specific-exclusions**: Never sync machine-specific items — SSH keys (`.ssh/id_*`), Tailscale config (`.config/tailscale`), hardware caches (GPU shaders, fontconfig), machine-specific packages and configuration.
- [ ] **REQ-conflict-detection-no-resolution**: Detect conflicts arising from unsupported concurrent use and report them; resolution is manual (no automatic resolution).

### Workflow and UX

- [x] **REQ-manual-sync-workflow**: Manual single-command trigger from source to target. User works on source, wakes target, triggers sync, waits, resumes. No real-time/cloud syncing.
- [x] **REQ-terminal-ux**: A single command launches the full sync across both machines; terminal-based UI; simple, intuitive experience for sync, conflict, and error handling.

### Reliability and Architecture

- [ ] **REQ-reliability-principles**: Project principles in priority order — (1) reliability (no data loss, conflict detection, consistent post-sync state, detailed logging, audit + rollback); (2) smooth UX; (3) well-supported tools/best practices; (4) minimize disk wear (NVMe); (5) sync speed; (6) maintainability/simplicity; (7) always-current documentation.
- [ ] **REQ-feature-modular-architecture**: Each user-facing sync feature is a self-contained modular Job built on core infrastructure, with its own sync logic, conflict detection, and validation.
- [ ] **REQ-environment-constraints**: Target is Ubuntu 24.04 LTS, single flat-layout btrfs per machine, machines reachable via SSH (LAN/VPN) during sync. Documented acceptable constraints allowed if they do not significantly hinder usability; everything automatable should be automated.

### Foundation (Shipped)

These shipped before GSD adoption and are the framework every sync job builds on.

- [x] **REQ-core-001-infrastructure**: Core sync-orchestration infrastructure — Job interface and lifecycle; SystemJob/SyncJob/BackgroundJob; self-installing orchestrator with version matching; config sync; btrfs pre/post-sync snapshot safety; snapshot cleanup + retention; disk-space monitoring; six-level logging; JSON Lines logs; graceful SIGINT; YAML config with schema validation; dummy test jobs; terminal UI progress; `pc-switcher sync` CLI with locking; `install.sh`.
- [x] **REQ-testing-framework-002**: Three-tier test framework (unit / VM-isolated integration with btrfs reset / manual playbook) plus CI/CD; contract tests; two test VMs (pc1/pc2); concurrency locking; cost under EUR 10/month.
- [x] **REQ-core-tests-003**: Spec-driven retroactive test coverage of 001-core — success and failure paths, test-to-spec traceability, unit + integration suites.
- [x] **REQ-python-logging-004**: Stdlib logging with configurable file/TUI/external floors, six-level hierarchy with custom FULL level, external-library log capture, JSON Lines + Rich formats.

## v2 Requirements

None deferred to a separate release. The full vision above is in scope across the milestones; items listed under Out of Scope are excluded by design rather than deferred.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Simultaneous / real-time bi-directional (merge) sync | Only one machine active at a time; each sync is one-directional. The round-trip metric is two sequential one-directional syncs, not a concurrent merge. |
| Continuous / cloud / real-time syncing | Workflow is a manual single-command trigger by design (REQ-manual-sync-workflow). |
| Automatic conflict resolution | Detection only; manual resolution preserves reliability over a guessed merge. |
| Parallel jobs, partial sync, multi-user parallel use, WiFi/internet-only sync, GUI | Explicit "ideas for later"; deferred to keep scope focused. |

## Traceability

Each requirement maps to the phase that first delivers it. Foundation requirements are already shipped; cross-cutting requirements are realized per modular job and anchored to the phase where they are first or most concretely delivered.

| Requirement | Phase | Status |
|-------------|-------|--------|
| REQ-core-001-infrastructure | Foundation | Complete |
| REQ-testing-framework-002 | Foundation | Complete |
| REQ-core-tests-003 | Foundation | Complete |
| REQ-python-logging-004 | Foundation | Complete |
| REQ-feature-modular-architecture | Foundation | Complete |
| REQ-environment-constraints | Foundation | Complete |
| REQ-near-full-state-replication | Cross-cutting (Phases 1-6) | Pending |
| REQ-sync-scope-user-data | Phase 1 | Complete |
| REQ-machine-specific-exclusions | Phase 1 | Complete |
| REQ-sync-scope-file-metadata | Phase 1 | Complete |
| REQ-manual-sync-workflow | Phase 1 | Complete |
| REQ-terminal-ux | Phase 1 | Complete |
| REQ-sync-scope-packages | Phase 2 | Pending |
| REQ-conflict-detection-no-resolution | Phase 2 (cross-cutting, per job) | Pending |
| REQ-sync-scope-app-and-system-config | Phase 3 | Pending |
| REQ-sync-scope-docker | Phase 4 | Pending |
| REQ-sync-scope-vms | Phase 5 | Pending |
| REQ-sync-scope-k3s | Phase 6 | Pending |
| REQ-reliability-principles | Phase 7 (cross-cutting, capstone) | Pending |

**Coverage:**

- v1 requirements: 19 total
- Mapped to phases: 19 (6 Foundation-complete, 8 phase-anchored, 5 cross-cutting/principle anchored to their primary phase)
- Unmapped: 0

*Requirements defined: 2026-06-30*

*Last updated: 2026-06-30 after bootstrap-from-ingest.*
