<!-- refreshed: 2026-06-29 -->
# Architecture

**Analysis Date:** 2026-06-29

## System Overview

```text
┌─────────────────────────────────────────────────────────────────┐
│                      CLI Layer (Typer)                          │
│                    `src/pcswitcher/cli.py`                      │
│  sync, init, logs, cleanup-snapshots, self update commands      │
└─────────────────┬───────────────────────────────────────────────┘
                  │ async orchestrator.run()
                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Orchestrator Layer                            │
│            `src/pcswitcher/orchestrator.py`                     │
│  Coordinates: lock → SSH → validation → disk check → jobs       │
│  Manages: connections, locks, job execution, snapshots          │
└──────────────────┬──────────────────────────────────────────────┘
                   │ Phases 1-10: lock, SSH, validate, snapshot, jobs
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│              Infrastructure & Support                            │
├──────────────────┬──────────────────┬─────────────┬──────────────┤
│  Connection SSH  │  Executor Layer  │   Lock      │  Config      │
│  (asyncssh)      │  (Local/Remote)  │  (fcntl)    │  (YAML)      │
│  `connection.py` │  `executor.py`   │ `lock.py`   │ `config.py`  │
├──────────────────┼──────────────────┼─────────────┼──────────────┤
│ Event Bus (pub)  │  Logger (stdlib) │   Models    │   UI (Rich)  │
│  `events.py`     │  `logger.py`     │ `models.py` │ `ui.py`      │
└──────────────────┴──────────────────┴─────────────┴──────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Job Layer (Pluggable)                         │
│              `src/pcswitcher/jobs/`                             │
│  - BtrfsSnapshotJob (pre/post snapshots)                        │
│  - InstallOnTargetJob (ensure pc-switcher on target)            │
│  - DiskSpaceMonitorJob (runtime monitoring)                     │
│  - [Custom sync jobs loaded dynamically]                        │
└─────────────────────────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│               Target Machine (via SSH)                           │
│  Runs commands, maintains persistent lock, creates snapshots    │
└─────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| CLI | Parse arguments, load config, invoke orchestrator | `cli.py` |
| Orchestrator | Orchestrate 10-phase workflow (lock, SSH, validation, jobs, snapshots) | `orchestrator.py` |
| Connection | Manage SSH connection with keepalive and session multiplexing | `connection.py` |
| LocalExecutor | Run commands locally (asyncio subprocess) | `executor.py` |
| RemoteExecutor | Run commands remotely via SSH | `executor.py` |
| Job (base) | Abstract job interface with validation and execution | `jobs/base.py` |
| BtrfsSnapshotJob | Create pre/post btrfs snapshots on source and target | `jobs/btrfs.py` |
| InstallOnTargetJob | Install/upgrade pc-switcher on target machine | `jobs/install_on_target.py` |
| DiskSpaceMonitorJob | Monitor disk space on both hosts during sync | `jobs/disk_space_monitor.py` |
| Configuration | Parse and validate YAML config with schema | `config.py` |
| EventBus | Pub/sub for logging and progress events | `events.py` |
| Logger | Stdlib logging with JSON file output and custom FULL level | `logger.py` |
| TerminalUI | Rich UI with progress bars, logs, and connection status | `ui.py` |
| SyncLock | File-based lock (fcntl) for preventing concurrent syncs | `lock.py` |
| Models | Core dataclasses (CommandResult, SyncSession, JobResult, etc.) | `models.py` |

## Pattern Overview

**Overall:** Async orchestrator with pluggable job system and event-driven UI

**Key Characteristics:**
- Async/await throughout (Python 3.14+ asyncio)
- Protocol-based executor abstraction (local/remote interchangeable)
- YAML schema validation (jsonschema) for config and per-job settings
- Pluggable jobs discovered dynamically from module names
- Event bus for decoupled logging and progress (pub/sub)
- Stdlib logging with JSON lines file output (ADR-010)
- Rich terminal UI with live progress display
- File-based locking (fcntl) for preventing concurrent syncs

## Layers

**CLI Layer:**
- Purpose: Parse command-line arguments, load/validate config, invoke orchestrator
- Location: `src/pcswitcher/cli.py`
- Contains: Typer app definition, command handlers (sync, init, logs, cleanup-snapshots, self update)
- Depends on: Configuration, Orchestrator, Version management
- Used by: End users via `pc-switcher` command

**Orchestrator Layer:**
- Purpose: Coordinate the complete 10-phase sync workflow
- Location: `src/pcswitcher/orchestrator.py`
- Contains: Phase execution (lock, SSH, validation, snapshots, job execution)
- Depends on: Connection, Executor, Job, Configuration, Lock, Logger, UI, EventBus
- Used by: CLI (sync command)

**Job Layer:**
- Purpose: Execute individual sync operations (pluggable architecture)
- Location: `src/pcswitcher/jobs/`
- Contains: Base Job class, BtrfsSnapshotJob, InstallOnTargetJob, DiskSpaceMonitorJob, custom jobs
- Depends on: JobContext (provides executors, config, event bus)
- Used by: Orchestrator (discovers and executes jobs)

**Infrastructure Layer:**
- Connection: SSH connection management with asyncssh
- Executor: Protocol-based command execution (local subprocess, remote SSH)
- Lock: File-based process locking with fcntl
- Configuration: YAML parsing and jsonschema validation
- Logger: Stdlib logging infrastructure with JSON output
- EventBus: Pub/sub event delivery (ProgressEvent, ConnectionEvent, LogEvent)
- UI: Rich terminal UI with live display
- Models: Core dataclasses

## Data Flow

### Primary Request Path (Sync Operation)

1. **CLI** → Parse args, load config (`cli.py:sync()`)
2. **Orchestrator** → Create instance with target, config, options (`orchestrator.py:__init__()`)
3. **Phase 1: Source Lock** → Acquire fcntl lock on source machine (`orchestrator.py:_acquire_source_lock()`)
4. **Phase 2: SSH Connection** → Connect to target with asyncssh (`orchestrator.py:_establish_connection()`)
5. **Phase 3: Target Lock** → Acquire fcntl lock on target via SSH (`orchestrator.py:_acquire_target_lock()`)
6. **Phase 4: Discover Jobs** → Dynamically import enabled jobs, validate config and system state (`orchestrator.py:_discover_and_validate_jobs()`)
7. **Phase 5: Disk Check** → Verify both hosts have sufficient free space (`orchestrator.py:_check_disk_space_preflight()`)
8. **Phase 6: Pre-snapshots** → Create btrfs snapshots on both hosts (`orchestrator.py:_create_snapshots(PRE)`)
9. **Phase 7: Install on Target** → Ensure pc-switcher is installed/upgraded on target (`orchestrator.py:_install_on_target_job()`)
10. **Phase 8: Config Sync** → Sync configuration from source to target (`orchestrator.py:_sync_config_to_target()`)
11. **Phase 9: Execute Jobs** → Run sync jobs sequentially with background disk monitoring (`orchestrator.py:_execute_jobs()`)
12. **Phase 10: Post-snapshots** → Create post-sync snapshots (`orchestrator.py:_create_snapshots(POST)`)
13. **Cleanup** → Release locks, close connections, update sync history (`orchestrator.py:_cleanup()`)

### State Management

- **Configuration**: Immutable after loading (Configuration dataclass)
- **Session**: SyncSession tracks state from start to completion (session_id, status, job_results)
- **Locks**: SyncLock holds fcntl file descriptor; released in cleanup
- **Connection**: SSH connection open for entire sync; closed in cleanup
- **Events**: EventBus collects subscribers (UI task) and publishes events until closed
- **Logging**: Stdlib logging with queue-based handler; writes to JSON file in real-time
- **UI**: Live display updated by consuming ProgressEvent and ConnectionEvent from queue

## Key Abstractions

**Job (abstract base class):**
- Purpose: Define interface for pluggable sync operations
- Examples: `BtrfsSnapshotJob`, `InstallOnTargetJob`, `DiskSpaceMonitorJob`, custom jobs in `jobs/*.py`
- Pattern: Subclass implements `validate()` and `execute()` async methods; owns `CONFIG_SCHEMA` for jsonschema validation

**Executor (protocol):**
- Purpose: Abstract command execution for local vs. remote
- Examples: `LocalExecutor` (asyncio subprocess), `RemoteExecutor` (asyncssh)
- Pattern: Both implement `async run_command()`, `async terminate_all_processes()`; jobs use without knowing which

**Configuration (dataclass with validation):**
- Purpose: Represent validated YAML config with nested sections
- Pattern: `from_yaml()` loads and validates schema; nested dataclasses (DiskConfig, BtrfsConfig, LogConfig)

**EventBus (pub/sub):**
- Purpose: Decouple logging and progress from orchestrator and jobs
- Pattern: Subscribers get own queues; publishers send to all; None sentinel on close

**SyncSession (dataclass):**
- Purpose: Track complete sync execution state and results
- Contains: session_id, hostnames, status, job_results, timestamps, error_message

**Host (StrEnum):**
- Purpose: Represent logical machine role in sync (SOURCE or TARGET)
- Used in: JobContext, ValidationError, ProgressUpdate, logging extra fields

## Entry Points

**CLI Command Handler (pc-switcher sync):**
- Location: `src/pcswitcher/cli.py:sync()`
- Triggers: User runs `pc-switcher sync <target>`
- Responsibilities: Parse args, load config, invoke `_run_sync()` → asyncio.run()

**Orchestrator.run():**
- Location: `src/pcswitcher/orchestrator.py:run()` (async entry point)
- Triggers: Called by CLI via `asyncio.run()`
- Responsibilities: Execute 10 phases, return SyncSession

**Job.execute():**
- Location: `src/pcswitcher/jobs/base.py:Job.execute()` (abstract)
- Triggers: Called by Orchestrator for each discovered job
- Responsibilities: Implement sync logic for that job

## Architectural Constraints

- **Async Model:** Entire sync is async (asyncio); no blocking I/O. Executors must be async-compatible.
- **Session Locking:** File-based fcntl lock prevents concurrent syncs on any machine (source or target).
- **Single-threaded:** No background threads; all concurrency via asyncio tasks. UI task runs in background consuming event queue.
- **No stdin:** Executor does not support stdin; all commands must be non-interactive (prevents prompts).
- **SSH Connection Lifetime:** Single SSH connection per sync; multiplexed with semaphore (max 10 concurrent sessions).
- **Configuration Immutable:** Configuration loaded once at start; not modified during sync.
- **Job Execution Sequential:** Sync jobs run one at a time (not parallel); ensures predictable ordering and easier error recovery.
- **Disk Space Monitoring:** DiskSpaceMonitorJob runs in background during job execution; raises exception if threshold breached.

## Error Handling

**Strategy:** Fail fast with clear logging; catch and log all exceptions; re-raise critical errors.

**Patterns:**
- **Configuration Errors:** Caught in CLI, displayed with helpful messages, exit code 1
- **Validation Errors:** Collected from jobs; if any found, raise RuntimeError with formatted list
- **Execution Errors:** Caught per job; logged as CRITICAL; added to JobResult with error_message; re-raised to abort sync
- **Connection Errors:** Propagate from asyncssh; caught in orchestrator, logged, cleanup triggered
- **Lock Errors:** If lock already held, RuntimeError raised immediately (prevents concurrent syncs)
- **Cancellation:** SIGINT triggers asyncio.CancelledError; caught, cleanup performed, status=INTERRUPTED

## Cross-Cutting Concerns

**Logging:** Stdlib logging with queue-based handler; JSON lines file output; extra dict for structured context (job, host, source_hostname, target_hostname). No output to console in normal flow (UI handles progress).

**Validation:** Three-phase approach:
1. Phase 1: Schema validation (jsonschema on config)
2. Phase 2: Job config validation (jsonschema on job-specific config)
3. Phase 3: System state validation (jobs check preconditions: directories, files, permissions, etc.)

**Authentication:** SSH via asyncssh; respects ~/.ssh/config and agent for key handling.

**Progress Reporting:** Jobs call `_report_progress(ProgressUpdate)` to publish events; UI consumes from EventBus queue and renders.

**Dry-run Mode:** When enabled, orchestrator logs "[DRY-RUN]" banner; jobs must respect and not make changes (responsibility of each job).

---

*Architecture analysis: 2026-06-29*
