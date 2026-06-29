# Requirements (from PRDs)

Synthesized from PRD-tier docs: `docs/planning/High level requirements.md` (prec 1), `docs/planning/Feature breakdown.md` (prec 1), and SpecKit specs `specs/001-core`, `specs/002-testing-framework`, `specs/003-core-tests`, `specs/004-python-logging` (prec 3). Per ADR-011, where `specs/00x` content overlaps `docs/system/*.md` (SPEC tier, prec 2), the living Golden Copy is authoritative for current-state truth — see INGEST-CONFLICTS.md.

## Vision / Product-level (source: docs/planning/High level requirements.md)

### REQ-near-full-state-replication
- source: /home/janfr/dev/pc-switcher/docs/planning/High level requirements.md
- description: Keep multiple Linux desktop machines in sync with minimal friction, aiming at near-full system-state replication (not just user-data sync). Generic to N machines; concrete case is two laptops (P17 primary, XPS13 mobile). Only one machine actively used at a time; no bi-directional sync.

### REQ-sync-scope-user-data
- source: docs/planning/High level requirements.md
- description: Sync `/home` and `/root` — documents, code, configs, application data. Selective caches: include dev tool caches (uv, pip, cargo, npm); exclude browser/IDE caches (VS Code specifically mentioned).

### REQ-sync-scope-packages
- source: docs/planning/High level requirements.md
- description: Sync installed packages across apt, snap, flatpak, manual .debs, custom PPAs, and packages from install scripts; detect package conflicts / version mismatches.

### REQ-sync-scope-app-and-system-config
- source: docs/planning/High level requirements.md
- description: Sync application configurations (GNOME desktop, cloud mounts, systemd services) and machine-independent system configs (`/etc`, startup services, users/groups). Detect conflicting system changes.

### REQ-sync-scope-file-metadata
- source: docs/planning/High level requirements.md
- description: Preserve file metadata — owner, permissions, ACLs, timestamps.

### REQ-sync-scope-vms
- source: docs/planning/High level requirements.md
- description: Sync KVM/virt-manager VMs. Constraint: VMs must be suspended/powered off before sync; detect concurrent VM usage.

### REQ-sync-scope-docker
- source: docs/planning/High level requirements.md
- description: Sync Docker images, containers, volumes, cache; detect running containers or incompatible states.

### REQ-sync-scope-k3s
- source: docs/planning/High level requirements.md
- description: Sync local single-node k3s cluster state including PVCs; validate cluster state and detect active workloads.

### REQ-machine-specific-exclusions
- source: docs/planning/High level requirements.md
- description: Never sync machine-specific items: SSH keys (`.ssh/id_*`), Tailscale config (`.config/tailscale`), hardware caches (GPU shaders, fontconfig), machine-specific packages and configuration.

### REQ-environment-constraints
- source: docs/planning/High level requirements.md
- description: Target environment is Ubuntu 24.04 LTS, single flat-layout btrfs filesystem per machine, machines reachable via SSH during sync (LAN / VPN such as Tailscale). Documented acceptable constraints allowed (e.g. apps not running during sync, logout required, subvolume creation) provided they do not significantly hinder usability; everything automatable should be automated.

### REQ-manual-sync-workflow
- source: docs/planning/High level requirements.md
- description: Manual single-command trigger from source to target; user works on source, wakes target, triggers sync, waits, resumes. No real-time/cloud syncing.

### REQ-conflict-detection-no-resolution
- source: docs/planning/High level requirements.md
- description: Detect conflicts arising from unsupported concurrent use; report conflicts but resolution is manual (no automatic resolution).

### REQ-terminal-ux
- source: docs/planning/High level requirements.md
- description: Single command launches the full sync across both machines; terminal-based UI; simple, intuitive experience for sync, conflict, and error handling.

### REQ-reliability-principles
- source: docs/planning/High level requirements.md
- description: Project principles in priority order: (1) Reliability — no data loss, conflict detection, consistent post-sync state, detailed logging, audit + rollback; (2) smooth UX; (3) well-supported tools/best practices; (4) minimize disk wear (NVMe); (5) sync speed; (6) maintainability/simplicity; (7) always-current navigable documentation.

NOTE — competing-variants check: "Ideas for later" (parallel jobs, partial sync, multi-user parallel use, bi-directional sync, WiFi/internet sync, GUI) are explicitly out-of-scope future ideas, not current requirements. Not treated as competing acceptance variants.

## Feature decomposition (source: docs/planning/Feature breakdown.md)

### REQ-feature-modular-architecture
- source: /home/janfr/dev/pc-switcher/docs/planning/Feature breakdown.md
- description: Decompose into core cross-cutting infrastructure (features 1-4) plus modular user-facing sync features (5-10). Each user feature is a self-contained modular component built on core infrastructure, with its own sync logic, conflict detection, and validation.
- core features: (1) Basic CLI & Infrastructure — parser, config, connection, logging, TUI skeleton, modular-feature architecture; (2) Safety Infrastructure — pre-sync validation framework, btrfs snapshot management (pre/post snapshots, cleanup); (3) Installation & Setup — deploy to machines, dependency install (early, to enable real-machine testing); (4) Rollback Capability — restore from pre-sync snapshots via `pc-switcher rollback` (deferred until core stable).
- user features (5-10): User Data Sync; Package Management Sync; System Configuration Sync; Docker State Sync; VM State Sync; k3s Cluster Sync. Each with conflict detection and validation as listed in REQ-sync-scope-* above.

## Core infrastructure spec — 001-core (source: specs/001-core/spec.md)

### REQ-core-001-infrastructure
- source: /home/janfr/dev/pc-switcher/specs/001-core/spec.md
- description: Core sync orchestration infrastructure. Job interface contract and lifecycle (validate/execute); SystemJob/SyncJob/BackgroundJob types; self-installing orchestrator with version matching; config sync to target; btrfs pre/post-sync snapshot safety infrastructure; snapshot cleanup command and retention; disk-space monitoring (preflight + runtime); six-level logging hierarchy (DEBUG/FULL/INFO/WARNING/ERROR/CRITICAL); JSON Lines log files; graceful SIGINT interrupt handling; YAML config system with schema validation; `dummy_success`/`dummy_fail` test jobs; terminal UI progress reporting; `pc-switcher sync` CLI with locking; `install.sh` installation script.
- status: Core infrastructure reported complete (per CLAUDE.md project stage). Living/current authority is `docs/system/core.md`; this spec.md is frozen history per ADR-011.

## Testing framework spec — 002-testing-framework (source: specs/002-testing-framework/spec.md)

### REQ-testing-framework-002
- source: /home/janfr/dev/pc-switcher/specs/002-testing-framework/spec.md
- description: Three-tier test framework (unit / integration with VM isolation / manual playbook) plus CI/CD and supporting docs. Fast dependency-free unit tests; integration tests with isolated VM provisioning and btrfs baseline-snapshot reset; concurrent-run locking; contract tests (MockExecutor vs real executors); two test VMs (pc1/pc2); CI/CD workflows (push, PR, manual trigger, concurrency); fork-PR secret handling; manual playbook; developer/operational/architecture docs; pytest markers and VM fixtures; cost constraint under EUR 10/month.
- relation: implements ADR-006. Living/current authority is `docs/system/testing.md`.

## Retroactive core tests spec — 003-core-tests (source: specs/003-core-tests/spec.md)

### REQ-core-tests-003
- source: /home/janfr/dev/pc-switcher/specs/003-core-tests/spec.md
- description: Spec-driven retroactive test coverage of 001-core. 100% coverage of user stories, acceptance scenarios, and functional requirements; success + failure path testing; test-to-spec traceability; unit tests in `tests/unit` (mock executors), integration tests in `tests/integration` (real VM operations); test independence and performance budgets.
- relation: depends on 001-core and 002-testing-framework; not a competing variant — it tests 001-core rather than redefining it.

## Python logging migration spec — 004-python-logging (source: specs/004-python-logging/spec.md)

### REQ-python-logging-004
- source: /home/janfr/dev/pc-switcher/specs/004-python-logging/spec.md
- description: Migrate to Python's standard logging module with configurable file/TUI/external library log-level floors. Capture and route external library logs (asyncssh); six-level hierarchy with custom FULL level; preserve JSON-lines file format and colored TUI format; sensible default levels; fail on invalid log-level config; preserve structured context.
- relation: implements ADR-010. Living/current authority is `docs/system/logging.md`.
