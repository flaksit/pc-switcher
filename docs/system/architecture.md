# System Architecture

This document describes the architecture for the pc-switcher system, covering the core components, their relationships, and key interaction patterns.

## Navigation

- [System Documentation](_index.md)
- [Data Model](data-model.md)
- [Core Spec](core.md)
- [Logging Spec](logging.md)

## Design Principles

- **Asyncio-native**: All I/O operations are async; cancellation uses native `asyncio.CancelledError`
- **Single SSH connection**: Multiplexed sessions over one connection for efficiency
- **Job autonomy**: Jobs own their resources and are responsible for cleanup on cancellation
- **Clear separation**: Jobs are isolated units; orchestrator handles coordination
- **Fail-safe**: Graceful degradation and proper cleanup on errors/interrupts

---

## Terminology: Host vs Hostname

| Term | Type | Values | Description |
|------|------|--------|-------------|
| **host** | `Host` (enum) | `SOURCE`, `TARGET` | The logical role of a machine in the sync operation |
| **hostname** | `str` | e.g., `"laptop-work"` | The actual machine name |

**Resolution:**
- Source hostname: obtained from local machine (`socket.gethostname()`)
- Target connection address: the CLI argument `sync <target>` (hostname, SSH alias, or IP), used as the SSH/rsync destination
- Target hostname for sync-history and the topology check: the target's own `socket.gethostname()`, queried over SSH so both ends are acquired the same way. Peers are compared case-insensitively, so a differently-cased or aliased target still matches a clean back-sync (ADR-015)

---

## Component Architecture

```mermaid
graph TD
    CLI["<b>CLI</b><br/>- Parses arguments sync &lt;target&gt;<br/>- Loads config file (YAML)<br/>- Creates and runs Orchestrator"]

    Orchestrator["<b>Orchestrator</b><br/>- Manages sync session lifecycle<br/>- Handles SIGINT via asyncio cancellation<br/>- Coordinates job validation/execution<br/>- Manages background tasks via TaskGroup<br/>- Aggregates results & summary"]

    Config["<b>Config</b><br/>- Validated config dataclass<br/>- Global settings<br/>- Job settings<br/>- Defaults applied"]

    Connection["<b>Connection</b><br/>- SSH via asyncssh<br/>- Multiplexed sessions<br/>- Health check"]

    Logger["<b>Logger</b><br/>- stdlib logging<br/>- FileHandler (JSON)<br/>- TUIHandler (Rich)<br/>- Configurable levels"]

    TerminalUI["<b>TerminalUI</b><br/>- Rich Live<br/>- Progress bars<br/>- Log messages<br/>- Status"]

    LocalExecutor["<b>LocalExecutor</b><br/>- Implements Executor<br/>- Async subprocess"]

    RemoteExecutor["<b>RemoteExecutor</b><br/>- Implements Executor<br/>- + send/get file<br/>- + get_hostname()"]

    InstallOnTargetJob["<b>InstallOnTargetJob</b><br/>- Check version<br/>- Install/upgrade<br/>- Verify"]

    BtrfsSnapshotJob["<b>BtrfsSnapshotJob</b><br/>- pre/post mode<br/>- One instance per host<br/>- Direct btrfs commands<br/>- No pc-switcher dependency"]

    SyncJobs["<b>SyncJobs</b><br/>- User data<br/>- Packages<br/>- Docker<br/>- VMs<br/>- k3s<br/>[configurable]"]

    DiskSpaceMonitorJob["<b>DiskSpaceMonitorJob</b><br/>- Periodic check<br/>- One instance per host<br/>- Raises exception if low<br/>[background]"]

    CLI --> Orchestrator
    Orchestrator --> Config
    Orchestrator --> Connection
    Orchestrator --> Logger
    Orchestrator --> TerminalUI
    Orchestrator --> LocalExecutor
    Connection --> RemoteExecutor
    Orchestrator --> InstallOnTargetJob
    Orchestrator --> BtrfsSnapshotJob
    Orchestrator --> SyncJobs
    Orchestrator --> DiskSpaceMonitorJob

    style CLI fill:#e1f5ff
    style Orchestrator fill:#fff3e0
    style LocalExecutor fill:#f3e5f5
    style RemoteExecutor fill:#f3e5f5
    style InstallOnTargetJob fill:#e8f5e9
    style BtrfsSnapshotJob fill:#e8f5e9
    style SyncJobs fill:#e8f5e9
    style DiskSpaceMonitorJob fill:#fce4ec
```

### Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| **CLI** | Entry point. Parses commands, loads config, runs Orchestrator. |
| **Orchestrator** | Central coordinator. Validates config, manages job lifecycle, handles SIGINT. |
| **Config** | Validated configuration dataclass. |
| **Connection** | Manages SSH connection via asyncssh. |
| **LocalExecutor** | Implements `Executor` interface for local async subprocess execution. |
| **RemoteExecutor** | Implements `Executor` interface via Connection. |
| **Logger** | Unified logging using Python stdlib `logging`. Routes to file (JSON) and terminal (formatted). |
| **TerminalUI** | Rich-based live display. Shows progress bars, log messages, status. |
| **Jobs** | Encapsulated sync operations. |

---

## Event Bus Architecture

All logging and progress events flow through an event bus with per-consumer queues. This decouples producers from consumers and ensures the UI never blocks job execution.

*Note: With the migration to stdlib logging, log events are captured via a custom Handler that publishes to the Event Bus.*

```mermaid
graph LR
    subgraph Producers
        Orch["Orchestrator"]
        Jobs["Jobs"]
        Conn["Connection"]
    end

    subgraph EventBus ["Event Bus"]
        Publish["publish()"]
        FLQ["FileLogger Queue"]
        UIQ["TerminalUI Queue"]
    end

    subgraph Consumers
        FL["FileLogger"]
        UI["TerminalUI"]
    end

    Orch -->|LogEvent / ProgressEvent| Publish
    Jobs -->|LogEvent / ProgressEvent| Publish
    Conn -->|ConnectionEvent| Publish

    Publish --> FLQ
    Publish --> UIQ

    FLQ --> FL
    UIQ --> UI

    FL -->|JSON lines| LogFile["sync-*.log"]
    UI -->|Rich Live| Terminal["Terminal"]

    style EventBus fill:#fff3e0
    style Producers fill:#e8f5e9
    style Consumers fill:#e1f5ff
```

### Event Types

| Event | Fields | Description |
|-------|--------|-------------|
| `LogEvent` | level, job, host, message, context, timestamp | Log message from any component |
| `ProgressEvent` | job, update (ProgressUpdate), timestamp | Job progress update |
| `ConnectionEvent` | status, latency | SSH connection status change |

---

## Class Diagram

### Job Classes

```mermaid
classDiagram
    class Job {
        <<abstract>>
        +name: str
        +__init__(context: JobContext)
        +validate_config(config)$ list~ConfigError~
        +validate() list~ValidationError~
        +execute() None
        #_log(host, level, message, **extra) None
        #_report_progress(update: ProgressUpdate) None
    }

    class SystemJob {
        <<abstract>>
    }

    class SyncJob {
        <<abstract>>
    }

    class BackgroundJob {
        <<abstract>>
    }

    class InstallOnTargetJob {
        +execute() None
    }

    class BtrfsSnapshotJob {
        +phase: str
        +execute() None
    }

    class DiskSpaceMonitorJob {
        +host: Host
        +interval: float
        +execute() None
    }

    Job <|-- SystemJob
    Job <|-- SyncJob
    Job <|-- BackgroundJob
    SystemJob <|-- InstallOnTargetJob
    SystemJob <|-- BtrfsSnapshotJob
    BackgroundJob <|-- DiskSpaceMonitorJob
```

### Supporting Classes

```mermaid
classDiagram
    class JobContext {
        +config: JobConfig
        +source: LocalExecutor
        +target: RemoteExecutor
        +event_bus: EventBus
        +session_id: str
    }

    class CommandResult {
        +exit_code: int
        +stdout: str
        +stderr: str
        +success: bool
    }

    class EventBus {
        +subscribe() asyncio.Queue
        +publish(event: Event) None
    }

    class LocalExecutor {
        +run_command(cmd, timeout) CommandResult
        +start_process(cmd) Process
    }

    class RemoteExecutor {
        +run_command(cmd, timeout) CommandResult
        +start_process(cmd) Process
        +send_file(local, remote) None
        +get_file(remote, local) None
    }

    class Connection {
        +connect() None
        +disconnect() None
        +check_health() bool
    }

    class Orchestrator {
        +run() SyncResult
    }

    JobContext --> LocalExecutor : source
    JobContext --> RemoteExecutor : target
    JobContext --> EventBus : publishes to
    LocalExecutor --> CommandResult : returns
    RemoteExecutor --> Connection : wraps
    Orchestrator --> Connection : owns
    Orchestrator --> Job : manages
```

---

## Data Flow

### Sync Operation Flow

```mermaid
sequenceDiagram
    actor User
    participant CLI
    participant Orchestrator
    participant Connection
    participant Jobs
    participant EventBus
    participant TerminalUI

    User->>CLI: pc-switcher sync <target>
    CLI->>CLI: Load config.yaml
    CLI->>Orchestrator: create(target, config)
    CLI->>Orchestrator: run()

    Orchestrator->>Orchestrator: Validate config schema
    Orchestrator->>Orchestrator: Acquire source lock
    Orchestrator->>Connection: connect()
    Connection-->>Orchestrator: SSH established
    Orchestrator->>Connection: Acquire target lock

    Orchestrator->>Connection: Check pc-switcher version compatibility
    Orchestrator->>Jobs: validate_config() for each
    Orchestrator->>Jobs: validate() for each

    Orchestrator->>Jobs: Create pre-sync snapshots
    Orchestrator->>Connection: Install/upgrade pc-switcher on target
    Orchestrator->>Jobs: Start background monitors

    loop For each enabled sync job
        Orchestrator->>Jobs: execute()
        Jobs->>EventBus: LogEvent, ProgressEvent
        EventBus->>TerminalUI: Update display
    end

    Orchestrator->>Jobs: Create post-sync snapshots
    Orchestrator->>Connection: disconnect()
    Orchestrator->>Orchestrator: Release locks
    Orchestrator-->>CLI: SyncSession result
    CLI-->>User: Exit with status
```

### Sync Lifecycle Phases

`Orchestrator.run()` executes a fixed, ordered sequence of phases. These names are the shared vocabulary for reasoning about the sync — the sequence diagram above shows the message flow; this list names each step and its role. The numbering matches the `# Phase N:` markers in `orchestrator.py`.

1. **Source lock** — acquire the local unified lock. Fail-fast (no wait, no retry): if this machine is already a source or target of another sync, abort immediately.
2. **SSH connection** — establish the asyncssh connection to the target.
3. **Target lock** — acquire the target's unified lock over SSH, held by a persistent remote `flock` process for the entire session (released in cleanup). Fail-fast if the target is already busy.
   - **Out-of-order / first-sync gate** (runs right after the target lock, once the target's sync-history is readable over SSH): the first-sync gate (bypass: `--allow-first-sync`) and the out-of-order gate (bypass: `--allow-out-of-order`) per ADR-015. A `--dry-run` rehearses both gates without aborting (ADR-014).
4. **Job discovery & validation** — discover enabled jobs; validate config schema and system prerequisites.
5. **Disk-space preflight** — verify sufficient free space before making changes.
6. **Pre-sync snapshots** — btrfs pre-snapshots on both machines (the rollback point).
7. **Install/upgrade on target** — install the matching pc-switcher version on the target (after snapshots, so a bad install is recoverable).
8. **Config sync** — copy the source config to the target.
9. **Job execution** — run each enabled sync job, with a background disk-space monitor.
10. **Post-sync snapshots** — btrfs post-snapshots on both machines.

On success, the orchestrator then records the source/target roles in each machine's sync-history (the topology-safety state read by the phase-3 gate on the next run). A `finally` block always runs cleanup: it terminates the remote lock process (releasing the target lock) and disconnects. Locks are fcntl advisory locks, so they are released automatically if a process exits or the SSH connection drops — a leftover lock *file* never blocks a future sync.

### Event-Driven Logging Flow

```mermaid
sequenceDiagram
    participant Job
    participant EventBus
    participant Logger
    participant TerminalUI
    participant LogFile

    Job->>EventBus: publish(LogEvent)

    par Fan-out to consumers
        EventBus->>Logger: Queue LogEvent
        EventBus->>TerminalUI: Queue LogEvent
    end

    par Independent consumption
        Logger->>Logger: Apply file_level filter
        Logger->>LogFile: Write JSON line
    and
        TerminalUI->>TerminalUI: Apply cli_level filter
        TerminalUI->>TerminalUI: Render in log panel
    end
```

---

## Key Design Patterns

### Async/Await Throughout
All I/O operations use asyncio for efficient concurrency:
- SSH operations via asyncssh
- Local subprocess execution
- Event queue processing
- Background monitoring tasks

### Cancellation via CancelledError
Uses native asyncio cancellation:
- Jobs catch `CancelledError` in exception handlers
- Clean up own resources (terminate processes)
- Re-raise for propagation
- No manual flag polling

### Job Autonomy
Each job is self-contained:
- Owns configuration schema
- Validates system state
- Manages own resources
- Reports progress independently
- Handles cleanup on cancellation

### Event-Driven Architecture
Producers publish events without knowing consumers:
- Jobs don't call logger directly - publish events
- UI updates don't block job execution
- Easy to add new consumers (e.g., metrics, webhooks)
- Per-consumer queues prevent blocking

### Sequential Execution
Jobs run one at a time (no dependency graph):
- Simpler reasoning about state
- Easier error recovery
- Clear progress tracking
- Reduced complexity vs parallel execution

Background tasks (disk monitoring) run concurrently using asyncio TaskGroup.

---

## Validation Phases

Configuration and system validation happen in distinct phases:

1. **Schema Validation**: Orchestrator checks YAML syntax and types.
2. **Job Config Validation**: `Job.validate_config()` checks values and paths.
3. **System State Validation**: `Job.validate()` checks system readiness (e.g., subvolumes exist).

---

## Lock Mechanism

A single lock file `~/.local/share/pc-switcher/pc-switcher.lock` is used on every machine. This ensures a machine can only participate in one sync at a time, regardless of role (source or target).

- **Source (local)**: `fcntl.flock()`
- **Target (remote)**: `flock` via SSH

---

## Self-Installation Flow

The orchestrator ensures version consistency before any sync operations.

1. Check target version.
2. If missing or outdated, install/upgrade using `uv tool install git+...`.
3. Verify installation.

---

## Disk Space Preflight Check

The orchestrator checks free disk space on both source and target before creating snapshots. Thresholds are configurable (e.g., "20%" or "50GiB").
