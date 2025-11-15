# Implementation Plan: Foundation Infrastructure Complete

**Branch**: `001-foundation` | **Date**: 2025-11-15 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-foundation/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/commands/plan.md` for the execution workflow.

## Summary

This feature establishes the complete foundation infrastructure for PC-switcher, a synchronization system for seamless switching between Linux desktop machines. The foundation includes:

1. **Module Architecture**: Standardized contract for all sync features with lifecycle methods, config schemas, logging, progress reporting, and dependency ordering
2. **Self-Installation**: Automatic version-matching installation/upgrade of pc-switcher on target machines
3. **Safety Infrastructure**: Btrfs snapshot creation (pre/post-sync) with rollback capability and disk space monitoring
4. **Logging System**: Six-level logging (DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL) with file/CLI separation and CRITICAL-triggered abort
5. **Interrupt Handling**: Graceful Ctrl+C handling with module cleanup, target termination, and no orphaned processes
6. **Configuration System**: YAML-based config with module enable/disable, schema validation, and defaults
7. **Installation & Setup**: Deployment tooling with dependency checking and btrfs verification
8. **Dummy Test Modules**: Three reference implementations (success, critical, fail) for testing infrastructure
9. **Terminal UI**: Real-time progress reporting with module status, log messages, and progress bars

Technical approach: Python 3.13 orchestrator using Fabric for SSH communication, structlog for logging, rich/textual for terminal UI, with modular plugin architecture enabling independent feature development.

## Technical Context

**Language/Version**: Python 3.13 (per ADR-003; Paramiko not yet supporting 3.14)

**Primary Dependencies**:
- `fabric` (SSH orchestration, built on Paramiko)
- `structlog` (structured logging)
- `rich` (terminal UI - chosen over textual for simplicity, better for progress bars)
- `typer` (CLI framework - chosen over click for modern type hints)
- `pyyaml` (config parsing)
- `uv` (package/dependency management)

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
- No orphaned processes after Ctrl+C (FR-028)
- CRITICAL log event must abort within current module execution
- Module lifecycle must follow strict ordering: validate → pre_sync → sync → post_sync → cleanup

**Scale/Scope**:
- ~10-15 core modules (btrfs-snapshots, user-data, packages, docker, vms, k3s, etc.)
- ~3000-5000 lines Python for core orchestrator
- ~500-1000 lines per feature module
- Support for 2-10 configured subvolumes for snapshots

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### Reliability Without Compromise ✅

**Data Integrity**:
- Btrfs snapshots (read-only, COW) before any state modification (FR-009, FR-010)
- Snapshot creation failure → CRITICAL abort before changes (FR-013)
- Rollback capability to pre-sync snapshots (FR-014)
- Subvolume existence verification before snapshot attempts (FR-016)
- Disk space checks: pre-sync threshold and continuous monitoring (FR-017, FR-018)

**Conflict Detection**:
- Lock mechanism prevents concurrent sync executions (FR-048)
- Version mismatch detection (newer target → CRITICAL abort to prevent downgrade) (FR-007)
- Validation phase (all modules) before any state changes (FR-003)

**Transactional Safety**:
- Module lifecycle enforces validate → execute → cleanup ordering (FR-003)
- CRITICAL log events trigger immediate abort with cleanup (FR-020, FR-025)
- Exception handling with orchestrator-managed cleanup (User Story 8, FR-042)

### Frictionless Command UX ✅

**Single Command**: `pc-switcher sync <target>` executes entire workflow (FR-047, SC-001)

**Automation**:
- Auto-installation/upgrade of target pc-switcher to match source version (FR-006, SC-004)
- Module auto-discovery from config (FR-005)
- Default config generation on installation (FR-038)
- No manual steps except initial setup and sync trigger

**Progressive Feedback**:
- Real-time progress reporting (module, percentage, current item) (FR-044, FR-045, User Story 9)
- Terminal UI with structured progress and log display (User Story 9)
- Clear error messages with remediation guidance (FR-008, FR-034)

### Proven Tooling Only ✅

**Core Dependencies** (all actively maintained, widely adopted):
- **Fabric** (latest): SSH orchestration, ~10k GitHub stars, active development, used in production deployments
- **structlog** (latest): Structured logging, industry standard for Python logging
- **rich** or **textual** (NEEDS CLARIFICATION): Both Textualize projects, active development, 48k+ / 25k+ stars
- **click** or **typer** (NEEDS CLARIFICATION): click is Flask ecosystem standard (15k+ stars); typer built on click (15k+ stars)
- **PyYAML** (6.x): Standard YAML library for Python
- **uv** (latest): Modern Python package manager by Astral (creators of Ruff), rapidly becoming industry standard

**Security Posture**:
- All dependencies available via PyPI with package signing
- Fabric/Paramiko: mature SSH implementations with security track record
- No experimental or unmaintained dependencies

**Support Horizon**: All libraries support Python 3.13; migration path to 3.14 clear once Paramiko adds support

### Solid-State Stewardship ✅

**Write Amplification Mitigation**:
- Btrfs snapshots use COW → zero initial write amplification (SC-008)
- Single persistent SSH connection prevents repeated handshake overhead (ADR-002)
- Structured logging minimizes redundant writes (single log file, buffered I/O)

**Monitoring**:
- Disk space checks before and during sync (FR-017, FR-018)
- Snapshot cleanup command to manage old snapshots (FR-015)
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
- Log timestamps (ISO8601) for all operations (FR-023)
- Phase timing captured in logs (validate, pre_sync, sync, post_sync)
- Progress percentage and ETA from modules (FR-044)

**Parallelization**: Not applicable to foundation (sequential by design for safety); future modules can parallelize internally

### Deliberate Simplicity ✅

**Minimal Components**:
- Single Python package (no microservices, no databases, no message queues)
- SSH as only network protocol (ADR-002)
- YAML for config (human-readable, standard)
- File-based logging (no centralized log aggregation)

**Maintainability**:
- Clear module interface contract enables independent development (FR-001, SC-007)
- Module auto-discovery removes manual registration
- Lifecycle methods enforce consistent patterns
- Python type hints + basedpyright for early error detection

**Reversibility**:
- Btrfs snapshots enable full rollback (FR-014)
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
├── pyproject.toml           # uv project config, package metadata, CLI entry point
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
│       │   └── ui.py        # Terminal UI (rich/textual integration)
│       ├── core/
│       │   ├── __init__.py
│       │   ├── orchestrator.py    # Main sync orchestration, module lifecycle
│       │   ├── module.py          # Module interface (ABC), base class
│       │   ├── session.py         # SyncSession state management
│       │   ├── config.py          # Configuration loading, validation
│       │   ├── logging.py         # Logging setup (structlog integration)
│       │   └── signals.py         # SIGINT handling
│       ├── remote/
│       │   ├── __init__.py
│       │   ├── connection.py      # SSH connection management (Fabric)
│       │   └── installer.py       # Target installation/upgrade logic
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
├── scripts/                       # Bundled with package, deployed to target
│   └── target/
│       └── remote_helpers.py      # Target-side helper scripts
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
