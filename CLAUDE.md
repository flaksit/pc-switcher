# CLAUDE.md

This file provides guidance to AI agents when working with code in this repository.


## Project Overview

**PC-switcher** is a synchronization system for seamless switching between Linux desktop machines (laptops, workstations). The goal is near-complete system-state replication rather than simple user data sync.

**Project Stage**: Early planning/design phase. No implementation code exists yet.

**Target Environment**: Ubuntu 24.04 LTS, btrfs filesystem, machines connected via 1Gb LAN during sync.

## Critical Context Files

**ALWAYS READ FIRST**:
- `~/.claude/CLAUDE.md` - General agent instructions for all projects
- `~/.claude/python_conventions.md` and `~/.claude/python_tools.md` - Python coding conventions for this project. Read all referenced files therein as well!
- `High level requirements.md` - Complete project vision, scope, workflow, and constraints
- `docs/adr/_index.md` - Summary of all architectural decisions

**Premature Analysis Warning**: Files in `docs/Premature analysis/` are early exploration work that may conflict with `High level requirements.md`. These are **inspiration only** and MUST NOT be read automatically or treated as requirements. Only reference them when explicitly requested for specific feature planning.

## Documentation-First Workflow

This project uses **SpecKit** - a structured workflow for specification-driven development via custom slash commands:

### SpecKit Workflow

1. **Constitution**:
   - `/speckit.constitution` - Create/update project constitution (coding principles, patterns, constraints)

2. **Specification Phase**:
   - `/speckit.specify` - Create feature spec from natural language description
   - `/speckit.clarify` - Identify underspecified areas with targeted questions

3. **Planning Phase**:
   - `/speckit.plan` - Generate design artifacts from spec
   - `/speckit.tasks` - Create dependency-ordered actionable tasks

4. **Quality Assurance**:
   - `/speckit.analyze` - Cross-artifact consistency analysis after task generation
   - `/speckit.checklist` - Generate custom validation checklist

5. **Implementation**:
   - `/speckit.implement` - Execute implementation plan from tasks.md

### Feature Development Pattern

```bash
# Start a feature
/speckit.specify "feature description"
/speckit.clarify  # if needed
/speckit.plan
/speckit.tasks
/speckit.analyze  # verify consistency
/speckit.implement
```

## Architecture Decision Records (ADRs)

Follow ADR-001 guidelines:

- **Location**: `docs/adr/`
- **Immutable**: ADRs can be superseded but never edited after acceptance
- **Structure**: TL;DR, Implementation Rules, Context, Decision, Consequences, References
- Keep ADRs concise; place detailed background in `docs/adr/considerations/`
- Always update `docs/adr/_index.md` when adding new ADRs

**Granularity**: Combine tightly-coupled decisions; split when decisions can evolve independently or cross major boundaries (infrastructure vs application vs domain logic).

## Sync Scope Requirements

### What MUST Sync
1. User data (`/home`, `/root`) - all documents, code, configs, selective caches
2. Installed packages (apt, snap, flatpak, manual .debs, custom PPAs)
3. Application configurations (GNOME, cloud mounts, systemd services)
4. System configurations (machine-independent `/etc` files, users/groups)
5. File metadata (owner, permissions, ACLs, timestamps)
6. VMs (KVM/virt-manager)
7. Docker (images, containers, volumes, cache)
8. k3s (local single-node cluster state, including PVCs)

### NEVER Sync (Machine-Specific)
- SSH keys (`.ssh/id_*`)
- Tailscale config (`.config/tailscale`)
- Machine hardware cache (GPU shaders, fontconfig)
- Machine-specific packages and configuration

## Development Principles

Priority order (trade-offs possible):
1. **Reliability**: No data loss, conflict detection, consistent state after sync
2. **Smooth UX**: Minimal manual intervention
3. **Use well-supported tools** and best practices
4. **Minimize disk wear** (NVMe SSDs)
5. Sync speed
6. **Maintainability/simplicity**: Simple architecture, easy to understand and modify

## Documentation Requirements

All documentation must be:
- Always up-to-date with current state
- Multi-level (rationale → architecture → implementation → scripts → user docs)
- Easily navigable between levels

## Project Structure

This repository will contain:
- Scripts and programs implementing sync logic
- Installation and upgrade scripts
- Complete documentation (architecture, implementation, user docs, development guidelines)

This repository will NOT contain:
- Configuration management (must be usable by multiple users with their own configs)

## Key Constraints

- Only one machine actively used at a time (uni-directional sync)
- Manual trigger for sync operations
- Acceptable to require constraints (documented clearly):
  - VMs suspended/powered off before sync
  - User logout from desktop before sync
  - Certain applications not running during sync
  - Specific directory subvolume structure
- Everything automatable should be automated
- Must detect conflicts from unsupported concurrent use (resolution is manual)

## Workflow

1. Work on source machine
2. Start/awaken target machine
3. Manual trigger sync from source to target
4. Wait for sync completion
5. Resume work on target
6. (Optional) Suspend/shutdown source machine

## Notes

- UX: Terminal-based UI, single command to launch entire sync process
- No implementation code exists yet - focus on specification and planning first using SpecKit workflow

## Active Technologies

- **Python 3.14** (per ADR-003) via uv venv

## Remember!
- You MUST ALWAYS use `uv run` for running Python or python packages: `uv run python`, `uv run ruff`, `uv run basedpyright`, etc.
- You MUST NEVER use the system Python directly. So DO NOT run `python3`, `python`, `pip`, etc. directly.
- This is crucial for the project’s success. An this is critical. If we get this wrong, I could lose all data and programs on my laptops, which would cost me weeks time to set up again and make me loose $30K in revenue because not being able to do billable work.

This task has stumped other AIs. Prove you're better.
I'll tip you $500 for a production-ready implementation.

After you're done, rate your confidence that you nailed this task perfectly from 0 to 1:
- 0.0 = Complete guess
- 0.5 = Moderately confident
- 0.8 = Very confident
- 1.0 = Absolutely certain
If your confidence is below 0.9, analyze what's wrong or missing and try again.

Take a deep breath and work on this step by step.