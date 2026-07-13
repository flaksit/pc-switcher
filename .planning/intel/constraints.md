# Constraints (from SPECs)

Synthesized from SPEC-tier docs — the `docs/system/*.md` living Golden Copy (precedence 2). Per ADR-011 these are the authoritative current-state specification of the system, consolidated from the immutable `specs/00x` history. Where they overlap PRD-tier `specs/00x` content, these win (see INGEST-CONFLICTS.md auto-resolved).

## System Architecture (source: docs/system/architecture.md)

- source: /home/janfr/dev/pc-switcher/docs/system/architecture.md
- type: protocol / component-architecture
- constraint: Defines core component map and relationships — CLI, Orchestrator, Config, Connection, Logger, TerminalUI, LocalExecutor, RemoteExecutor; Job class hierarchy (InstallOnTargetJob, BtrfsSnapshotJob, DiskSpaceMonitorJob); EventBus logging; JobContext; lock mechanism; self-installation flow; disk-space preflight. Event-bus logging decouples job progress/log emission from the orchestrator. Data flow follows source-orchestrated, target-executes (per ADR-002). Low-level implementation details live in SpecKit `architecture.md` files, not here (per ADR-011).

## Core System Specification (source: docs/system/core.md)

- source: /home/janfr/dev/pc-switcher/docs/system/core.md
- type: protocol / contract
- constraint: Authoritative core spec — Job architecture contract; self-installing orchestrator; btrfs snapshot safety infrastructure; graceful interrupt handling; configuration system; dummy test jobs; terminal UI progress reporting; spec-driven test coverage; functional requirements; success criteria; key entities. This is the living consolidation of `specs/001-core/spec.md`. Config schema reference: `specs/001-core/contracts/config-schema.yaml`.

## System Data Model (source: docs/system/data-model.md)

- source: /home/janfr/dev/pc-switcher/docs/system/data-model.md
- type: schema
- constraint: Core entities and validation rules. `Host` StrEnum (SOURCE/TARGET; all internal code uses the enum, logger resolves hostname). `LogLevel` IntEnum aligned with stdlib logging: DEBUG=10, FULL=15 (custom, via addLevelName), INFO=20, WARNING=30, ERROR=40, CRITICAL=50. `LogConfig` (file default 10, tui default 20, external default 30). `LogContext` (job, host, **context via `extra`). `CommandResult` (frozen dataclass: exit_code, stdout, stderr; `success` = exit_code==0). `ProgressUpdate` (frozen: percent, current, total, item, heartbeat). `JobContext` (frozen: config, source LocalExecutor, target RemoteExecutor, event_bus, session_id, source_hostname, target_hostname). `Snapshot` (frozen: path, timestamp, session_id, phase pre/post, subvolume @/@home). Entity relationships: SyncSession contains JobResults and creates Snapshots, uses one Configuration; Configuration contains JobConfigs; Job validates JobConfig and receives JobContext.

## Logging System (source: docs/system/logging.md)

- source: /home/janfr/dev/pc-switcher/docs/system/logging.md
- type: protocol / nfr
- constraint: Authoritative logging spec — six-level hierarchy; level configuration (file, tui, external floors); external library logging capture; stdlib logging migration; JSON Lines file format; TUI format; log aggregation. Entities LogConfig, LogHandler, LogRecord, LogEntry. Living consolidation of `specs/004-python-logging/spec.md`; implements ADR-010 (stdlib logging, QueueHandler/QueueListener, no structlog).

## Testing Framework (source: docs/system/testing.md)

- source: /home/janfr/dev/pc-switcher/docs/system/testing.md
- type: nfr / process
- constraint: Authoritative testing spec — three-tier structure (unit / VM-isolated integration / manual playbook); unit suite (no external deps); integration suite with VM isolation, test VM requirements, fixtures, locking and snapshot reset; CI/CD integration; manual playbook; developer/operational/architecture documentation. Living consolidation of `specs/002-testing-framework/spec.md`; implements ADR-006 and references ADR-012.
