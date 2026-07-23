# PC-switcher

## What This Is

PC-switcher is a synchronization system for seamlessly switching between Linux desktop machines (laptops, workstations) by replicating near-complete system state — not just user data — from the machine you were using onto the one you are moving to. It is a personal tool for a solo developer running Ubuntu 24.04 on btrfs across machines connected over SSH (LAN/VPN such as Tailscale). The core orchestration framework is already built; the work now is implementing the individual modular sync jobs on top of it.

## Core Value

After a single sync command, the target machine is a faithful, reliable replica of the source — no data loss, conflicts detected and reported, file metadata intact — so you can switch machines and just keep working. Reliability outranks every other goal (UX, speed, simplicity) when tradeoffs arise.

## Requirements

### Validated

Shipped before GSD adoption and relied upon as the foundation every sync job builds on. These are locked.

- ✓ Core sync-orchestration infrastructure — Job architecture contract, self-installing orchestrator with version matching, config sync, btrfs pre/post-sync snapshot safety, snapshot cleanup + retention, disk-space monitoring, six-level logging, graceful SIGINT handling, `pc-switcher sync` CLI with locking, `install.sh` — Foundation (spec 001-core)
- ✓ Three-tier testing framework — fast unit tests, VM-isolated integration tests on two VMs (pc1/pc2) with btrfs baseline-snapshot reset and concurrency locking, contract tests, manual playbook, CI/CD — Foundation (spec 002-testing-framework)
- ✓ Spec-driven retroactive core test coverage of 001-core — Foundation (spec 003-core-tests)
- ✓ Stdlib logging infrastructure — six-level hierarchy with custom FULL level, file/TUI/external floors, JSON Lines file format, Rich TUI format, external-library log capture — Foundation (spec 004-python-logging)
- ✓ Modular feature architecture — each sync feature is a self-contained Job (validate/pre_sync/sync/post_sync) wrapped by pre/post btrfs snapshots — Foundation

### Active

Current scope. Each item is a modular sync job built on the finished framework, sequenced as milestones.

- [ ] User-data sync of `/home` and `/root` over rsync-over-SSH (generic per-folder include/exclude) with file-metadata preservation and machine-specific exclusions — Phase 1 (Folder Sync MVP, current milestone)
- [ ] Machine-specific exclusions enforced on every sync, both directions — Phase 1 onward (introduced for user data, extended per scope)
- [ ] File-metadata preservation (owner, group, permissions, POSIX ACLs, timestamps) across all scopes — Phase 1 onward
- [ ] Package management sync (apt, snap, flatpak, manual .debs, custom PPAs, install-script packages) with conflict/version-mismatch detection — Phase 2
- [ ] System and application configuration sync (GNOME desktop, cloud mounts, systemd services, `/etc`, startup services, users/groups) with conflicting-change detection — Phase 3
- [ ] Docker state sync (images, containers, volumes, cache) with running-container / incompatible-state detection — Phase 4
- [ ] VM state sync (KVM/virt-manager) with powered-off guard and concurrent-use detection — Phase 5
- [ ] Local single-node k3s cluster sync including PVCs, with cluster-state validation and active-workload detection — Phase 6
- [ ] Rollback capability — restore a machine to its pre-sync state from automatic btrfs snapshots via `pc-switcher rollback` — Phase 7
- [ ] Conflict detection without auto-resolution — detect and report conflicts from unsupported concurrent use; resolution stays manual — cross-cutting, per job
- [ ] Manual single-command sync workflow with terminal UX — first delivered end-to-end in Phase 1

### Out of Scope

- Simultaneous / real-time bi-directional sync (both machines used at once with automatic merge) — only one machine is active at a time; each sync is one-directional. Note: the milestone "bidirectional round-trip" metric is two sequential one-directional syncs (A→B, then B→A), not a concurrent merge.
- Continuous / cloud / real-time syncing — by design the workflow is a manual single-command trigger from source to target.
- Automatic conflict resolution — detection only; the user resolves conflicts manually so reliability is never traded for a guessed merge.
- Parallel jobs, partial sync, multi-user parallel use, WiFi/internet-only sync, GUI — explicit "ideas for later" in the High level requirements, deferred to keep the MVP focused.

## Context

- Solo developer plus Claude workflow. Concrete case is two laptops (P17 primary, XPS13 mobile); the design is generic to N machines.
- The orchestration framework (specs 001-004) is complete with ZERO sync jobs implemented. This project builds the modular sync jobs, not the framework.
- Transport for user-data sync is rsync-over-SSH, chosen over btrfs send/receive. This direction is set but NOT yet captured in an ADR — formalize it as an ADR before or during Phase 1 (ADR-002 mandates SSH as the channel but does not fix the file-sync protocol choice).
- ADR-011 living-spec model is in force: `docs/system/*.md` is current-state authority; `specs/00x` folders are frozen history/provenance.
- btrfs subvolumes are `@` (root) and `@home`; the existing snapshot infrastructure wraps each sync with pre/post snapshots for safety and rollback.

## Constraints

- **Tech stack**: Python 3.14 via `uv` exclusively (never pip/system python); bash only for task-specific sync ops, never core orchestration — ADR-003.
- **Concurrency**: asyncio is the core model; all Job methods are `async def`; no blocking calls (`time.sleep`, `subprocess.run`, blocking socket I/O) in the event loop — ADR-005.
- **Transport/orchestration**: a single multiplexed SSH ControlMaster connection; source orchestrates, target exposes discrete stateless scripts; SIGINT triggers explicit target cleanup; target via `~/.ssh/config` hostname — ADR-002.
- **Filesystem/environment**: Ubuntu 24.04 LTS, one flat-layout btrfs filesystem per machine (`@`, `@home`), machines reachable via SSH (LAN/VPN/Tailscale) during sync; documented acceptable constraints allowed (apps closed, logout) if they do not significantly hinder usability — REQ-environment-constraints.
- **Testing**: three-tier (unit / VM-isolated integration on pc1+pc2 with btrfs baseline reset / manual playbook); TDD red-green-refactor for implementation; integration cost under EUR 10/month — ADR-006, ADR-007.
- **Logging**: Python stdlib `logging` only (no structlog); QueueHandler/QueueListener; six-level hierarchy; JSON Lines + Rich formatters; file/tui/external floors — ADR-010.
- **Versioning**: dynamic from GitHub Release tags (`dynamic = ["version"]`, hatchling + uv-dynamic-versioning); version parsing supports SemVer pre-releases — ADR-004.
- **CI**: lint + unit on all branches; integration on non-draft PRs; create PRs as draft so integration tests do not run prematurely; branch protection requires all three checks — ADR-008.
- **Documentation**: audience-based folders (dev/ops/planning/system/adr); ADRs immutable (supersede, never edit); living specs in `docs/system/`; DRY — ADR-001, ADR-011, ADR-012.

## Key Decisions

Locked architectural decisions are Accepted ADRs, immutable per ADR-001. They constrain all phases.

| Decision | Source | Status |
| -------- | ------ | ------ |
| Immutable ADR process; `_index.md` is the entry point | ADR-001 | Locked |
| SSH as source↔target channel — multiplexed ControlMaster, source orchestrates, target runs stateless scripts | ADR-002 | Locked |
| Python 3.14 orchestrator via uv; bash for task-specific ops | ADR-003 | Locked |
| Dynamic package versioning from GitHub Release tags | ADR-004 | Locked |
| asyncio concurrency; all Job methods async; cancellation on SIGINT/SIGTERM | ADR-005 | Locked |
| Three-tier testing (unit / VM-isolated integration / manual playbook) | ADR-006 | Locked |
| TDD red-green-refactor for implementation phases | ADR-007 | Locked |
| Draft-aware CI (unit on all branches; integration on non-draft PRs) | ADR-008 | Locked |
| Stdlib logging (no structlog); QueueHandler/Listener; six levels; JSON Lines + Rich | ADR-010 | Locked |
| SDD with living specs — `docs/system/` Golden Copy authoritative, `specs/00x` frozen | ADR-011 | Locked |
| Audience-based documentation structure | ADR-012 | Locked |
| rsync-over-SSH as the user-data transport (chosen over btrfs send/receive) | to formalize | Pending ADR |
| AI-readiness issue labels (ai:ready / ai:needs-speckit / ai:unclear) | ADR-009 | Proposed (not locked) |

*Last updated: 2026-06-30 after bootstrap-from-ingest.*
