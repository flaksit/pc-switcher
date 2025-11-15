# Implementation Plan: Foundation Infrastructure Complete

**Branch**: `001-foundation` | **Date**: 2025-11-15 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-foundation/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/commands/plan.md` for the execution workflow.

## Summary

This feature establishes the complete foundation infrastructure for PC-switcher, a synchronization system for seamless switching between Linux desktop machines. The foundation includes:

1. **Module Architecture**: Standardized contract for all sync features with lifecycle methods (validate, pre_sync, sync, post_sync, abort), config schemas, logging, progress reporting, and sequential execution (config-based order, no dependency resolution)
2. **Self-Installation**: Automatic version-matching installation/upgrade of pc-switcher on target machines from GitHub Package Registry (ghcr.io)
3. **Safety Infrastructure**: Btrfs snapshot creation (pre/post-sync) with rollback capability and disk space monitoring (configurable thresholds: float 0.0-1.0 or percentage string, defaults: min_free=0.20, reserve_minimum=0.15, check_interval=30s)
4. **Logging System**: Six-level logging (DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL) with file/CLI separation and exception-based abort (modules raise SyncError exception, orchestrator catches, logs as CRITICAL, calls abort(timeout))
5. **Interrupt Handling**: Graceful Ctrl+C handling with module abort, target termination, and no orphaned processes
6. **Configuration System**: YAML-based config with module enable/disable, schema validation, and defaults
7. **Installation & Setup**: Deployment tooling with dependency checking and btrfs verification
8. **Dummy Test Modules**: Three reference implementations (success, critical, fail) for testing infrastructure
9. **Terminal UI**: Real-time progress reporting with module status, log messages, and progress bars

Technical approach: Python 3.13 orchestrator using Fabric for SSH communication, structlog for logging, rich for terminal UI, with modular plugin architecture enabling independent feature development. Dynamic versioning via GitHub releases (uv-dynamic-versioning + hatchling). uv 0.9.9 for package management (.tool-versions).

## Technical Context

**Language/Version**: Python 3.13 (per ADR-003; Paramiko not yet supporting 3.14)

**Primary Dependencies**:
- `fabric` (SSH orchestration, built on Paramiko)
- `structlog` (structured logging with custom ERROR tracking processor)
- `rich` (terminal UI - chosen over textual for simplicity, better for progress bars)
- `typer` (CLI framework - chosen over click for modern type hints)
- `pyyaml` (config parsing)
- `uv 0.9.9` (package/dependency management, Python installation - see `.tool-versions`)
- `hatchling` + `uv-dynamic-versioning` (build system, dynamic versioning from Git tags)

**Storage**:
- YAML config files (`~/.config/pc-switcher/config.yaml`)
- Log files (`~/.local/share/pc-switcher/logs/sync-<timestamp>.log`)
- Btrfs snapshots (managed via `btrfs` CLI)

**Testing**:
- `pytest` (unit/integration tests)
- `basedpyright` (type checking)
- `ruff` (linting/formatting)
- `codespell` (typo checking)

**Target Platform**: Ubuntu 24.04 LTS with btrfs, Python 3.13 available

**Project Type**: Single Python package (`pc-switcher`) with CLI entry point

**Performance Goals**:
- Version check and installation: <30 seconds (SC-004)
- Snapshot creation: minimal write amplification via COW (SC-008)
- Graceful interrupt: <5 seconds cleanup (SC-006)
- Logging overhead: negligible impact on sync operations

**Constraints**:
- Single persistent SSH connection (no reconnects per operation)
- No orphaned processes after Ctrl+C (FR-027)
- Module exceptions (SyncError) trigger immediate abort with abort(timeout) call on currently-running module only
- Module lifecycle must follow strict ordering: validate → pre_sync → sync → post_sync → abort(timeout) (if error/interrupt)
- Disk space thresholds configurable as float (0.0-1.0) or percentage string (e.g., "20%")

**Scale/Scope**:
- ~10-15 core modules (btrfs-snapshots, user-data, packages, docker, vms, k3s, etc.)
- ~3000-5000 lines Python for core orchestrator
- ~500-1000 lines per feature module
- Support for 2-10 configured subvolumes for snapshots

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### Reliability Without Compromise ✅

**Data Integrity**:
- Btrfs snapshots (read-only, COW) before any state modification (FR-008)
- Snapshot creation failure → CRITICAL abort before changes (FR-012)
- Rollback capability to pre-sync snapshots (FR-013)
- Subvolume existence verification before snapshot attempts (FR-015)
- Disk space checks: pre-sync threshold and continuous monitoring (FR-016, FR-017)

**Conflict Detection**:
- Lock mechanism prevents concurrent sync executions (FR-047)
- Version mismatch detection (newer target → CRITICAL abort to prevent downgrade) (FR-006)
- Validation phase (all modules) before any state changes (FR-002)

**Transactional Safety**:
- Module lifecycle enforces validate → execute → abort ordering (FR-002)
- Exception-based error handling: modules raise SyncError, orchestrator logs as CRITICAL and aborts (FR-019)
- Orchestrator-managed cleanup via abort(timeout) on currently-running module (User Story 5, FR-003, FR-024)
- ERROR log tracking during execution determines final state (COMPLETED vs FAILED)

### Frictionless Command UX ✅

**Single Command**: `pc-switcher sync <target>` executes entire workflow (FR-046, SC-001)

**Automation**:
- Auto-installation/upgrade of target pc-switcher to match source version (FR-005, SC-004)
- Module loading from config in sequential order (no dependency resolution, FR-004)
- Default config generation on installation (FR-037)
- No manual steps except initial setup and sync trigger

**Progressive Feedback**:
- Real-time progress reporting (module, percentage, current item) (FR-043, FR-044, User Story 9)
- Terminal UI with structured progress and log display (User Story 9)
- Clear error messages with remediation guidance (FR-007, FR-033)

### Proven Tooling Only ✅

**Core Dependencies** (all actively maintained, widely adopted):
- **Fabric** (3.2+): SSH orchestration, ~10k GitHub stars, active development, used in production deployments
- **structlog** (24.1+): Structured logging, industry standard for Python logging
- **rich** (13.7+): Terminal UI from Textualize, 48k+ stars - chosen for simplicity and progress bar capabilities
- **typer** (0.12+): CLI framework built on click, 15k+ stars - chosen for modern type hints
- **PyYAML** (6.x): Standard YAML library for Python
- **uv** (0.9.9): Modern Python package manager by Astral (creators of Ruff), version pinned in .tool-versions

**Security Posture**:
- All dependencies available via PyPI with package signing
- Fabric/Paramiko: mature SSH implementations with security track record
- No experimental or unmaintained dependencies

**Support Horizon**: All libraries support Python 3.13; migration path to 3.14 clear once Paramiko adds support

### Solid-State Stewardship ✅

**Write Amplification Mitigation**:
- Btrfs snapshots use COW → zero initial write amplification (SC-008)
- Structured logging minimizes redundant writes (single log file, buffered I/O)
- Snapshot metadata managed by btrfs (COW-optimized)

**Monitoring**:
- Disk space checks before and during sync (FR-016, FR-017)
- Snapshot cleanup command to manage old snapshots (FR-014)
- Configurable retention (keep N most recent, delete older than X days)

**Estimated Write Load** (foundation only, no data sync):
- Config file: <10 KB one-time
- Log file per sync: ~100-500 KB (varies with log level)
- Snapshot metadata: minimal (btrfs internal, COW)
- Total foundation overhead: <1 MB per sync

### Throughput-Focused Syncing ✅

**Duration Targets** (foundation infrastructure):
- Version check + installation: <30 seconds (SC-004)
- Snapshot creation: <5 seconds per subvolume (btrfs COW)
- Graceful interrupt cleanup: <5 seconds (SC-006)
- Foundation overhead per sync: <60 seconds total

**Measurement Plan**:
- Log timestamps (ISO8601) for all operations (FR-021)
- Phase timing captured in logs (validate, pre_sync, sync, post_sync)
- Progress percentage and ETA from modules (FR-043)

**Parallelization**: Not applicable to foundation (sequential by design for safety); future modules can parallelize internally

### Deliberate Simplicity ✅

**Minimal Components**:
- Single Python package (no microservices, no databases, no message queues)
- SSH as the only orchestration/control channel for target machine communication (ADR-002)
- YAML for config (human-readable, standard)
- File-based logging (no centralized log aggregation)

**Maintainability**:
- Clear module interface contract enables independent development (FR-001, SC-007)
- Config-based module loading with sequential execution (simple, predictable)
- Lifecycle methods enforce consistent patterns
- Python type hints + basedpyright for early error detection

**Reversibility**:
- Btrfs snapshots enable full rollback (FR-013)
- Module cleanup methods ensure graceful degradation
- No persistent state outside of config and logs

### Documentation As Runtime Contract ✅

**Artifacts to Update** (in this plan):
- `specs/001-foundation/plan.md` (this file) - owner: plan generation
- `specs/001-foundation/research.md` (Phase 0) - owner: research agents
- `specs/001-foundation/data-model.md` (Phase 1) - owner: design phase
- `specs/001-foundation/contracts/` (Phase 1) - owner: design phase
- `specs/001-foundation/quickstart.md` (Phase 1) - owner: design phase
- Code docstrings and inline comments - owner: implementation tasks
- README.md updates - owner: implementation tasks

**Cross-References**:
- ADRs remain authoritative for architectural decisions
- Feature spec remains authoritative for requirements
- Implementation must reference spec sections (e.g., "implements FR-001")

**Synchronization**:
- All docs updated within same PR as code changes
- Spec → plan → tasks → implementation → docs flow enforced by SpecKit workflow

---

### Post-Design Re-Evaluation ✅

After completing Phase 0 (research) and Phase 1 (design):

**All constitution principles remain satisfied**:

1. **Reliability Without Compromise** ✅: Design preserves all safety mechanisms (snapshots, validation, abort handling)

2. **Frictionless Command UX** ✅: Single command design maintained, auto-installation/upgrade confirmed feasible with Fabric

3. **Proven Tooling Only** ✅: All NEEDS CLARIFICATION items resolved with proven libraries (rich, typer)

4. **Solid-State Stewardship** ✅: Design minimizes writes (single SSH connection, COW snapshots, buffered logging)

5. **Throughput-Focused Syncing** ✅: Foundation overhead remains <60 seconds (version check, snapshot creation, orchestration)

6. **Deliberate Simplicity** ✅: Design maintains simplicity (no additional components, clear module contract, sequential execution)

7. **Documentation As Runtime Contract** ✅: All design artifacts generated (research, data-model, contracts, quickstart)

**Design Validation**:
- Module interface contract is concrete and implementable (see `contracts/module-interface.py`)
- Orchestrator-module protocol is complete and unambiguous (see `contracts/orchestrator-module-protocol.md`)
- Configuration schema is well-defined (see `contracts/config-schema.yaml`)
- Data model entities have clear relationships and state transitions (see `data-model.md`)
- Implementation path is clear with defined phases (see `quickstart.md`)

**No violations or compromises introduced during design phase**.

## Project Structure

### Documentation (this feature)

```text
specs/[###-feature]/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
pc-switcher/
├── .tool-versions           # Tool version pinning (uv 0.9.9)
├── pyproject.toml           # uv project config, package metadata, CLI entry point, dynamic versioning
├── uv.lock                  # Locked dependencies
├── README.md                # Project overview, installation, quick start
├── .github/
│   └── workflows/
│       ├── ci.yml           # Linting, type checking, tests
│       └── release.yml      # Publish to GitHub Package Registry on release
├── src/
│   └── pcswitcher/          # Installable Python package
│       ├── __init__.py      # Package version, public API
│       ├── __main__.py      # Entry point for `python -m pcswitcher`
│       ├── cli/
│       │   ├── __init__.py
│       │   ├── main.py      # CLI entry point (sync, logs, cleanup-snapshots, etc.)
│       │   └── ui.py        # Terminal UI (rich integration)
│       ├── core/
│       │   ├── __init__.py
│       │   ├── orchestrator.py    # Main sync orchestration, module lifecycle
│       │   ├── module.py          # Module interface (ABC), base class, SyncError exception
│       │   ├── session.py         # SyncSession state management
│       │   ├── config.py          # Configuration loading, validation
│       │   ├── logging.py         # Logging setup (structlog integration)
│       │   └── signals.py         # SIGINT handling
│       ├── installer/
│       │   ├── __init__.py
│       │   └── setup.py           # Local installation utilities (btrfs detection, default config generation)
│       ├── remote/
│       │   ├── __init__.py
│       │   ├── connection.py      # SSH connection management (Fabric, ControlMaster)
│       │   └── installer.py       # Remote installation orchestration (source detects version, installs on target)
│       ├── modules/
│       │   ├── __init__.py
│       │   ├── btrfs_snapshots.py # Snapshot creation/rollback (required)
│       │   ├── dummy_success.py   # Test module: success path
│       │   ├── dummy_critical.py  # Test module: CRITICAL abort
│       │   └── dummy_fail.py      # Test module: exception handling
│       └── utils/
│           ├── __init__.py
│           ├── disk.py            # Disk space monitoring
│           └── lock.py            # Sync lock mechanism
├── scripts/
│   ├── setup.sh                   # Initial installation script
│   └── target/
│       └── remote_helpers.py      # Target-side helper scripts (bundled with package)
└── tests/
    ├── conftest.py                # pytest fixtures (mock SSH, config, etc.)
    ├── unit/
    │   ├── test_orchestrator.py
    │   ├── test_module.py
    │   ├── test_config.py
    │   └── test_logging.py
    ├── integration/
    │   ├── test_ssh_connection.py
    │   ├── test_module_lifecycle.py
    │   └── test_installer.py
    └── e2e/
        └── test_sync_flow.py      # End-to-end with test machines
```

**Structure Decision**: Single Python project (Option 1 from template). This is a CLI orchestration tool, not a web/mobile application. The structure follows Python packaging best practices:
- `src/pcswitcher/` layout for installable package
- Clear separation: CLI, core orchestration, remote operations, modules, utilities
- Test structure mirrors source for discoverability
- Scripts bundled via package data for target deployment

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
