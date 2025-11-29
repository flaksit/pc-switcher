# Implementation Plan: Foundation Infrastructure Complete

**Branch**: `001-foundation` | **Date**: 2025-11-29 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-foundation/spec.md`

## Summary

Implement the complete foundation infrastructure for pc-switcher, establishing the job architecture contract, self-installation mechanism, btrfs snapshot safety system, logging infrastructure, configuration system, terminal UI, and interrupt handling. This foundation enables all subsequent feature jobs to be developed independently against a well-defined interface.

The architecture is fully designed in [architecture.md](./architecture.md), which defines:
- Component relationships (CLI, Orchestrator, Jobs, Executors, Connection, EventBus, Logger, TerminalUI)
- Event-driven logging/progress via EventBus with per-consumer queues
- Job lifecycle (validate_config → validate → execute) with cancellation via asyncio.CancelledError
- SSH multiplexing via asyncssh with persistent connection
- Three validation phases (Schema → Job Config → System State)

## Technical Context

**Language/Version**: Python 3.14 (per ADR-003)
**Primary Dependencies**:
- `asyncssh` - SSH connection and multiplexing
- `rich` - Terminal UI, progress bars, live display
- `typer` - CLI framework
- `structlog` - Structured logging (JSON file output, console rendering)
- `pyyaml` - Configuration file parsing
- `jsonschema` - Configuration validation against job-declared schemas

**Storage**:
- Config: `~/.config/pc-switcher/config.yaml`
- Logs: `~/.local/share/pc-switcher/logs/sync-<timestamp>.log`
- Lock (source): `~/.local/share/pc-switcher/sync.lock`
- Lock (target): `~/.local/share/pc-switcher/target.lock`

**Testing**: pytest with asyncio support (`pytest-asyncio`), mocking for SSH operations

**Target Platform**: Ubuntu 24.04 LTS with btrfs filesystem, 1Gb LAN

**Project Type**: Single installable Python package

**Performance Goals**:
- Version check/install on target: < 30 seconds (SC-004)
- Abort on CRITICAL within 5 seconds (SC-003)
- Graceful interrupt with no orphaned processes (SC-006)

**Constraints**:
- Machines connected via LAN during sync
- User has sudo privileges on both machines
- btrfs filesystem required for snapshot safety
- Single concurrent sync (locking mechanism)

**Scale/Scope**:
- 9 user stories, 48 functional requirements
- < 200 lines for basic job implementation (SC-007)

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### Reliability Without Compromise
**Data integrity**: Btrfs snapshots created before any state modification (FR-008, FR-012). Pre-sync snapshots provide rollback points. Post-sync snapshots capture successful state (FR-009).

**Conflict detection**: Subvolume existence validated before sync (FR-015). Lock files on both source and target prevent concurrent executions (FR-047), including A→B and C→B scenarios. Version mismatch detection prevents accidental downgrades (FR-006).

**Rollback strategy**: Pre-sync snapshots enable manual rollback via `pc-switcher rollback` (FR-013). Snapshot naming includes session ID for clear identification (FR-010).

### Frictionless Command UX
**Single command**: `pc-switcher sync <target>` executes complete workflow (FR-046).

**Minimal intervention**: Self-installation handles target setup (FR-005). Configuration defaults reduce required setup (FR-031). Clear error messages with recovery guidance (FR-033).

**Progressive feedback**: Terminal UI shows progress bars, current operation, log messages (User Story 9). EventBus delivers real-time updates.

### Well-supported Tools and Best Practices
| Tool | Purpose | Support Status |
|------|---------|----------------|
| asyncssh | SSH/SFTP | Mature, async-native, Python 3.14 compatible |
| rich | Terminal UI | Very active, widely used |
| typer | CLI | Built on click, well-maintained |
| structlog | Logging | Production-grade, active development |
| pyyaml | YAML parsing | Industry standard |
| jsonschema | Config validation | Mature, draft-07 support |

### Minimize SSD Wear
**Btrfs CoW**: Snapshots use copy-on-write with zero initial write amplification (SC-008).

**Log rotation**: Logs written to single file per session, not continuous appends.

**Minimal temp files**: EventBus queues in memory, no intermediate disk staging.

### Throughput-Focused Syncing
**Foundation scope**: This feature establishes infrastructure; actual sync throughput measured in subsequent job implementations.

**SSH multiplexing**: Single persistent connection for efficiency (ADR-002).

**Async architecture**: asyncio-native design enables concurrent operations where appropriate.

### Deliberate Simplicity
**Architecture choices**:
- Sequential job execution (no dependency graph complexity) (FR-004)
- Single SSH connection per sync session
- EventBus with simple fan-out (no routing complexity)
- Jobs are isolated units; orchestrator handles coordination
- Three clear validation phases with distinct responsibilities

**Maintainability**: Architecture documented in Mermaid diagrams. Job interface < 200 lines demonstrates simplicity (SC-007).

### Up-to-date Documentation
**Artifacts to update**:
| Document | Owner | Update Required |
|----------|-------|-----------------|
| architecture.md | Plan phase | Already complete |
| README.md | Implementation | Installation instructions |
| CLAUDE.md | Implementation | Active technologies section |
| ADR index | If new decisions | Update _index.md |
| User guide | Implementation | Basic usage documentation |

## Project Structure

### Documentation (this feature)

```text
specs/001-foundation/
├── spec.md              # Feature specification
├── architecture.md      # Component architecture (COMPLETE)
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks command)
```

### Source Code (repository root)

```text
src/
└── pcswitcher/
    ├── __init__.py
    ├── cli.py              # Typer CLI entry point (sync, logs, cleanup-snapshots)
    ├── orchestrator.py     # Main sync orchestration, signal handling, TaskGroup
    ├── config.py           # Config loading, schema validation, defaults
    ├── connection.py       # SSH connection management via asyncssh
    ├── executor.py         # LocalExecutor, RemoteExecutor implementations
    ├── events.py           # EventBus, LogEvent, ProgressEvent, ConnectionEvent
    ├── logger.py           # Logger, JobLogger, FileLogger
    ├── ui.py               # TerminalUI with Rich Live display
    ├── snapshots.py        # Btrfs snapshot management (pre/post/cleanup)
    ├── installation.py     # Target pc-switcher installation/upgrade
    ├── models.py           # Core types (Host, LogLevel, CommandResult, etc.)
    └── jobs/
        ├── __init__.py
        ├── base.py         # Job, SystemJob, SyncJob, BackgroundJob ABCs
        ├── context.py      # JobContext dataclass
        ├── disk_monitor.py # DiskSpaceMonitorJob
        ├── dummy.py        # DummySuccessJob, DummyFailJob
        └── btrfs.py        # BtrfsSnapshotJob (pre/post phases)

tests/
├── conftest.py             # Shared fixtures (mock SSH, mock config)
├── unit/
│   ├── test_config.py
│   ├── test_events.py
│   ├── test_logger.py
│   ├── test_orchestrator.py
│   └── test_jobs/
│       ├── test_base.py
│       ├── test_disk_monitor.py
│       └── test_dummy.py
├── integration/
│   ├── test_cli.py
│   ├── test_connection.py
│   └── test_snapshots.py
└── contract/
    └── test_job_interface.py  # Verify job contract compliance
```

**Structure Decision**: Single Python package (`pcswitcher`) with modular organization. Jobs are a subpackage allowing easy addition of new sync features. Tests follow standard pytest layout with unit/integration/contract separation.

## Complexity Tracking

> No constitution violations requiring justification. Architecture follows deliberate simplicity.

| Design Choice | Rationale | Simpler Alternative Considered |
|--------------|-----------|-------------------------------|
| EventBus pattern | Decouples producers from consumers, prevents UI blocking jobs | Direct calls - rejected because UI operations could block job execution |
| Three validation phases | Clear separation of concerns, distinct error messages | Single validate() - rejected because schema errors differ from runtime checks |
| TaskGroup for background jobs | Native asyncio pattern for managing concurrent tasks | Manual task tracking - rejected as more error-prone |

## Post-Design Constitution Re-Evaluation

*Re-checked after Phase 1 design completion.*

### Summary

All constitution principles are satisfied by the design. No violations requiring exemption.

| Principle | Status | Evidence |
|-----------|--------|----------|
| Reliability Without Compromise | ✅ Pass | Btrfs snapshots, validation phases, lock file, version checking |
| Frictionless Command UX | ✅ Pass | Single `sync` command, self-installation, defaults, progress UI |
| Well-supported Tools | ✅ Pass | All dependencies actively maintained, Python 3.14 compatible |
| Minimize SSD Wear | ✅ Pass | CoW snapshots, in-memory EventBus, single log file per session |
| Throughput-Focused Syncing | ✅ Pass | SSH multiplexing, async architecture (actual metrics in sync jobs) |
| Deliberate Simplicity | ✅ Pass | Sequential execution, single connection, clear phase separation |
| Up-to-date Documentation | ✅ Pass | Architecture, data model, contracts, quickstart all complete |

### Design Artifacts Produced

| Artifact | Location | Purpose |
|----------|----------|---------|
| research.md | `specs/001-foundation/research.md` | Technology decisions and patterns |
| data-model.md | `specs/001-foundation/data-model.md` | Entity definitions and relationships |
| config-schema.yaml | `specs/001-foundation/contracts/config-schema.yaml` | JSON Schema for config validation |
| job-interface.md | `specs/001-foundation/contracts/job-interface.md` | Job implementation contract |
| quickstart.md | `specs/001-foundation/quickstart.md` | Developer setup guide |

### Ready for Task Generation

The plan is complete and ready for `/speckit.tasks` to generate actionable implementation tasks.
