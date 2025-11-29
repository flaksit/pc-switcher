# Foundation Architecture

This document describes the architecture for the pc-switcher foundation infrastructure, covering the core components, their relationships, and key interaction patterns.

## Design Principles

- **Asyncio-native**: All I/O operations are async; cancellation uses native `asyncio.CancelledError`
- **Single SSH connection**: Multiplexed sessions over one connection for efficiency
- **Job autonomy**: Jobs own their resources and are responsible for cleanup on cancellation
- **Clear separation**: Jobs are isolated units; orchestrator handles coordination
- **Fail-safe**: Graceful degradation and proper cleanup on errors/interrupts

---

## Terminology: Host vs Hostname

Throughout this document, two related but distinct concepts are used:

| Term | Type | Values | Description |
|------|------|--------|-------------|
| **host** | `Host` (enum) | `SOURCE`, `TARGET` | The logical role of a machine in the sync operation |
| **hostname** | `str` | e.g., `"laptop-work"`, `"desktop-home"` | The actual machine name |

**Resolution:**
- Source hostname: obtained from local machine (e.g., `socket.gethostname()`)
- Target hostname: provided via CLI argument `sync <target>`, resolved from SSH config if alias

**Usage:**
- All internal code uses `host` (role enum) exclusively
- Logger resolves `host` → `hostname` internally for output (UI and log files)

---

## Component Architecture

```mermaid
graph TD
    CLI["<b>CLI</b><br/>- Parses arguments sync &lt;target&gt;<br/>- Loads config file (YAML)<br/>- Creates and runs Orchestrator"]

    Orchestrator["<b>Orchestrator</b><br/>- Manages sync session lifecycle<br/>- Handles SIGINT via asyncio cancellation<br/>- Coordinates job validation/execution<br/>- Manages background tasks via TaskGroup<br/>- Aggregates results & summary"]

    Config["<b>Config</b><br/>- Validated config dataclass<br/>- Global settings<br/>- Job settings<br/>- Defaults applied"]

    Connection["<b>Connection</b><br/>- SSH via asyncssh<br/>- Multiplexed sessions<br/>- Health check"]

    Logger["<b>Logger</b><br/>- structlog<br/>- File output JSON<br/>- CLI output formatted<br/>- 6 levels"]

    TerminalUI["<b>TerminalUI</b><br/>- Rich Live<br/>- Progress bars<br/>- Log messages<br/>- Status"]

    LocalExecutor["<b>LocalExecutor</b><br/>- Implements Executor<br/>- Async subprocess"]

    RemoteExecutor["<b>RemoteExecutor</b><br/>- Implements Executor<br/>- + send/get file<br/>- + get_hostname()"]

    InstallOnTargetJob["<b>InstallOnTargetJob</b><br/>- Check version<br/>- Install/upgrade<br/>- Verify<br/>[required]"]

    BtrfsSnapshotJob["<b>BtrfsSnapshotJob</b><br/>- pre/post mode<br/>- One instance per host<br/>- Direct btrfs commands<br/>- No pc-switcher dependency<br/>[required]"]

    SyncJobs["<b>SyncJobs</b><br/>- User data<br/>- Packages<br/>- Docker<br/>- VMs<br/>- k3s<br/>[configurable]"]

    DiskSpaceMonitorJob["<b>DiskSpaceMonitorJob</b><br/>- Periodic check<br/>- One instance per host<br/>- Raises exception if low<br/>[required, background]"]

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
| **CLI** | Entry point. Parses commands (`sync`, `logs`, `cleanup-snapshots`, `rollback`), loads config file (YAML), instantiates and runs Orchestrator |
| **Orchestrator** | Central coordinator. Validates config (schema + general config + (delegated) job configs), manages job lifecycle via TaskGroup, handles SIGINT via asyncio cancellation, produces final sync summary |
| **Config** | Validated configuration dataclass. Holds global settings, job enable/disable flags, and per-job settings after validation |
| **Connection** | Manages SSH connection via asyncssh. Provides multiplexed sessions (multiple concurrent commands over single connection) |
| **LocalExecutor** | Implements `Executor` interface for local async subprocess execution. Used by source-side jobs |
| **RemoteExecutor** | Implements `Executor` interface via Connection. Adds file transfer (`send_file`, `get_file`) and `get_hostname()` |
| **Logger** | Unified logging with 6 levels. Routes to file (JSON) and terminal (formatted). Resolves host→hostname internally |
| **TerminalUI** | Rich-based live display. Shows progress bars, log messages (filtered by cli_level), overall status |
| **Jobs** | Encapsulated sync operations. Each job validates their specific config, validates the system state, executes operations, reports progress, cleans up own resources on cancellation |

---

## Event Bus Architecture

All logging and progress events flow through an event bus with per-consumer queues for guaranteed delivery. This decouples producers from consumers and ensures the UI never blocks job execution.

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
| `ProgressEvent` | job, update (ProgressUpdate), timestamp | Job progress update (update contains percent, current, total, item, heartbeat) |
| `ConnectionEvent` | status, latency | SSH connection status change |

### Properties

- **Fan-out delivery**: Each event is copied to all consumer queues
- **Non-blocking puts**: Producers use `put_nowait()`, never wait
- **Guaranteed delivery**: Per-consumer queues ensure no event loss even if one consumer is slow
- **Graceful shutdown**: `close()` signals consumers to drain queues and exit

---

## Class Diagram

### Job Classes

```mermaid
classDiagram
    class Job {
        <<abstract>>
        +name: str
        +required: bool = False
        +validate_config(config)$ list~ConfigError~
        +validate() list~ValidationError~
        +execute() None
        #log(level, message, **context) None
        #report_progress(progress: ProgressUpdate) None
    }

    class SystemJob {
        <<abstract>>
        required = True
    }

    class SyncJob {
        <<abstract>>
        required = False
    }

    class BackgroundJob {
        <<abstract>>
        required = True
    }

    class InstallOnTargetJob {
        +execute() None
    }

    class BtrfsSnapshotJob {
        +phase: str
        +execute() None
    }

    class DummySuccessJob {
    }

    class DummyFailJob {
    }

    class PackagesJob {
    }

    class UserDataJob {
    }

    class DockerJob {
    }

    class VMsJob {
    }

    class K3sJob {
    }

    class DiskSpaceMonitorJob {
        +host: Host
        +interval: float
        +execute() None
        Note: host = source or target
    }

    Job <|-- SystemJob
    Job <|-- SyncJob
    Job <|-- BackgroundJob
    SystemJob <|-- InstallOnTargetJob
    SystemJob <|-- BtrfsSnapshotJob
    SyncJob <|-- DummySuccessJob
    SyncJob <|-- DummyFailJob
    SyncJob <|-- PackagesJob
    SyncJob <|-- UserDataJob
    SyncJob <|-- DockerJob
    SyncJob <|-- VMsJob
    SyncJob <|-- K3sJob
    BackgroundJob <|-- DiskSpaceMonitorJob
```

### Supporting Classes

```mermaid
classDiagram
    class JobContext {
        +config: JobConfig
        +source: LocalExecutor
        +target: RemoteExecutor
        +logger: JobLogger
        +ui: TerminalUI
        +session_id: str
        +source_hostname: str
        +target_hostname: str
    }

    class ProgressUpdate {
        +percent: int | None
        +current: int | None
        +total: int | None
        +item: str | None
        +heartbeat: bool = False
    }

    class CommandResult {
        +exit_code: int
        +stdout: str
        +stderr: str
        +success: bool
    }

    class EventBus {
        -_consumers: list~asyncio.Queue~
        +subscribe() asyncio.Queue
        +publish(event: Event) None
        +close() None
    }

    class LogEvent {
        +level: LogLevel
        +job: str
        +host: Host
        +message: str
        +context: dict
    }

    class ProgressEvent {
        +job: str
        +update: ProgressUpdate
        +timestamp: datetime
    }

    class ConnectionEvent {
        +status: str
        +latency: float | None
    }

    class FileLogger {
        -_queue: asyncio.Queue
        -_file_level: LogLevel
        -_log_file: Path
        +consume() None
    }

    class Executor {
        <<interface>>
        +run_command(cmd, timeout) CommandResult
        +start_process(cmd) Process
        +terminate_all_processes() None
    }

    class Process {
        +stdout() AsyncIterator~str~
        +stderr() AsyncIterator~str~
        +wait() CommandResult
        +terminate() None
    }

    class LocalExecutor {
        +run_command(cmd, timeout) CommandResult
        +start_process(cmd) Process
        +terminate_all_processes() None
    }

    class RemoteExecutor {
        -_connection: Connection
        +run_command(cmd, timeout) CommandResult
        +start_process(cmd) Process
        +terminate_all_processes() None
        +send_file(local, remote) None
        +get_file(remote, local) None
        +get_hostname() str
    }

    Executor <|-- LocalExecutor : implements
    Executor <|-- RemoteExecutor : implements

    class Connection {
        -_conn: SSHClientConnection
        -_session_semaphore: Semaphore
        -_target: str
        -_connected: bool
        -_keepalive_interval: int = 15
        -_keepalive_count_max: int = 3
        -_event_bus: EventBus
        +connect() None
        +disconnect() None
        +create_process(cmd) SSHClientProcess
        +sftp() SFTPClient
        +check_health() bool
        +kill_all_remote_processes() None
    }

    class Orchestrator {
        -_config: Config
        -_connection: Connection
        -_logger: Logger
        -_ui: TerminalUI
        -_jobs: list~Job~
        -_task_group: TaskGroup
        +run() SyncResult
        -_handle_sigint() None
        -_validate_configs() list~ConfigError~
        -_validate_systems() list~ValidationError~
        -_execute_job(job) JobResult
    }

    class Logger {
        -_file_level: LogLevel
        -_cli_level: LogLevel
        -_event_bus: EventBus
        -_hostnames: dict~Host, str~
        +log(level, job, host, message, **ctx) None
        +get_job_logger(job_name, host) JobLogger
    }

    class JobLogger {
        -_logger: Logger
        -_job_name: str
        -_host: Host
        +log(level, message, **ctx) None
    }

    class TerminalUI {
        -_console: Console
        -_live: Live
        -_progress: Progress
        -_job_tasks: dict~str, TaskID~
        -_log_panel: deque~LogEntry~
        +start() None
        +stop() None
        +update_job_progress(job, progress: ProgressUpdate) None
        +add_log_message(level, job, message) None
        +set_overall_progress(step, total, description) None
        +set_connection_status(connected, latency) None
    }

    class DiskSpaceCriticalError {
        +host: Host
        +message: str
        +free_space: str
        +threshold: str
    }

    JobContext --> LocalExecutor : source
    JobContext --> RemoteExecutor : target
    JobContext --> JobLogger : uses
    JobContext --> ProgressUpdate : creates
    LocalExecutor --> CommandResult : returns
    RemoteExecutor --> Connection : wraps
    RemoteExecutor --> CommandResult : returns
    Orchestrator --> Connection : owns
    Orchestrator --> LocalExecutor : creates
    Orchestrator --> Logger : uses
    Orchestrator --> TerminalUI : uses
    Orchestrator --> Job : manages
    Logger --> JobLogger : creates
    JobLogger --> Logger : delegates to
    Logger --> EventBus : publishes events
    EventBus --> TerminalUI : delivers to queue
    EventBus --> FileLogger : delivers to queue
    Connection --> EventBus : publishes status
    DiskSpaceMonitorJob --> DiskSpaceCriticalError : raises
```

### Class Relationships

| Relationship | Description |
|--------------|-------------|
| Orchestrator → Connection | Owns and manages the SSH connection lifecycle |
| Orchestrator → LocalExecutor | Creates for local command execution |
| Orchestrator → Job[] | Creates, validates, and executes jobs; uses TaskGroup for background jobs |
| Orchestrator → EventBus | Creates and owns the event bus for logging/progress |
| Job → JobContext | Receives context at execution time (config, source, target, logger, ui, session_id) |
| JobContext → LocalExecutor | `source` field - for running commands on source machine |
| JobContext → RemoteExecutor | `target` field - for running commands on target machine + file transfers |
| JobContext → JobLogger | Jobs use this to log messages with pre-bound job name and host (role) |
| RemoteExecutor → Connection | Wraps Connection with job-friendly interface |
| RemoteExecutor → CommandResult | Returns structured result; Job interprets and logs |
| Logger → JobLogger | Creates bound logger instances for each job |
| Logger → EventBus | Publishes LogEvent for each log call |
| EventBus → FileLogger | Delivers events to FileLogger's dedicated queue |
| EventBus → TerminalUI | Delivers events to TerminalUI's dedicated queue |
| Connection → EventBus | Publishes ConnectionEvent on status changes |
| DiskSpaceMonitorJob → DiskSpaceCriticalError | Raises exception (with host and hostname) when space low; TaskGroup propagates |

---

## Validation Phases

Configuration and system validation happen in distinct phases with different error semantics:

```mermaid
graph TD
    Phase1["<b>Phase 1: Schema Validation</b><br/>Orchestrator<br/>- YAML syntax valid?<br/>- Required fields present?<br/>- Types correct?"]

    Phase2["<b>Phase 2: Job Config Validation</b><br/>Job.validate_config() classmethod<br/>- Are values sensible?<br/>- Paths exist?<br/>- Ranges valid?"]

    Phase3["<b>Phase 3: System State Validation</b><br/>Job.validate() instance method<br/>- Is system ready?<br/>- Subvolumes exist?<br/>- Connectivity OK?"]

    Error1["Error: Config file invalid<br/>line 12: missing 'sync_jobs'"]
    Error2["Error: Invalid config for 'packages'<br/>'sync_ppa' must be boolean"]
    Error3["Error: Cannot sync<br/>target subvolume '@home' missing"]

    Phase1 -->|fail| Error1
    Phase1 -->|pass| Phase2
    Phase2 -->|fail| Error2
    Phase2 -->|pass| Phase3
    Phase3 -->|fail| Error3
    Phase3 -->|pass| Execute["Proceed to execution"]

    style Phase1 fill:#e1f5ff
    style Phase2 fill:#fff3e0
    style Phase3 fill:#e8f5e9
    style Error1 fill:#ffcdd2
    style Error2 fill:#ffcdd2
    style Error3 fill:#ffcdd2
```

| Phase | Responsibility | Method | Error Message Style |
|-------|----------------|--------|---------------------|
| 1. Schema | Orchestrator | JSON Schema | Config file invalid: ... |
| 2. Job Config | Orchestrator | Job.validate_config() | Invalid config for 'job': ... |
| 3. System State | Jobs | Job.validate() | Cannot sync: ... |

### BtrfsSnapshotJob Validation (Phase 3)

Per FR-015, `BtrfsSnapshotJob.validate()` MUST verify subvolumes exist on **both** source and target:

```python
async def validate(self, context: JobContext) -> list[ValidationError]:
    errors = []
    subvolumes = context.config.get("subvolumes", ["@", "@home"])

    for subvol in subvolumes:
        # Check source
        result = await context.source.run_command(
            f"sudo btrfs subvolume show /{subvol} 2>/dev/null"
        )
        if not result.success:
            errors.append(ValidationError(
                job=self.name,
                host=Host.SOURCE,
                message=f"Subvolume '{subvol}' not found on source",
            ))

        # Check target (identical subvolume structure assumed)
        result = await context.target.run_command(
            f"sudo btrfs subvolume show /{subvol} 2>/dev/null"
        )
        if not result.success:
            errors.append(ValidationError(
                job=self.name,
                host=Host.TARGET,
                message=f"Subvolume '{subvol}' not found on target",
            ))

    return errors
```

**Important**: The configuration assumes identical subvolume names on source and target. If target has different subvolume structure, validation will fail with clear error messages before any sync operations begin.

---

## Sequence Diagrams

### 1. User Aborts with Ctrl+C

When the user presses Ctrl+C, asyncio's signal handler cancels the current task. The Job catches `CancelledError`, cleans up its own resources (including remote processes), and re-raises. After timeout, Orchestrator does a final safety sweep.

```mermaid
sequenceDiagram
    actor User
    participant Orchestrator
    participant TaskGroup
    participant CurrentJob
    participant RemoteExecutor
    participant TerminalUI

    User->>Orchestrator: Ctrl+C (SIGINT)
    Note over Orchestrator: asyncio signal handler
    Orchestrator->>TaskGroup: cancel all tasks
    TaskGroup->>CurrentJob: CancelledError raised

    Note over CurrentJob: except CancelledError:
    CurrentJob->>RemoteExecutor: terminate my processes
    CurrentJob->>CurrentJob: cleanup local state
    CurrentJob-->>TaskGroup: re-raise CancelledError

    Orchestrator->>Orchestrator: wait up to 5s

    alt timeout exceeded
        Orchestrator->>Orchestrator: log WARNING "cleanup timeout"
        Orchestrator->>RemoteExecutor: kill_all_remote_processes()
    end

    Orchestrator->>TerminalUI: log(WARNING, "Sync interrupted")
    Orchestrator->>TerminalUI: stop()
    Orchestrator->>User: exit(130)
```

**Key points:**
- Uses native `asyncio.CancelledError` - no polling of flags
- Job owns cleanup of its own remote processes in its `except CancelledError` handler
- Orchestrator only does final safety sweep after timeout (belt-and-suspenders)
- Exit code 130 indicates SIGINT termination (128 + signal number 2)

### Double SIGINT (Force Terminate)

Per FR-026, if a second SIGINT arrives during cleanup, the system force-terminates immediately:

```mermaid
sequenceDiagram
    actor User
    participant Orchestrator
    participant CurrentJob
    participant Connection

    User->>Orchestrator: Ctrl+C (first SIGINT)
    Note over Orchestrator: Set cleanup_in_progress = True
    Orchestrator->>CurrentJob: CancelledError raised

    Note over CurrentJob: Cleanup starting...

    User->>Orchestrator: Ctrl+C (second SIGINT)
    Note over Orchestrator: cleanup_in_progress already True

    Orchestrator->>Connection: force close (no wait)
    Orchestrator->>Orchestrator: log WARNING "Force terminated"
    Orchestrator->>User: exit(130) immediately
```

**Implementation note**: The signal handler checks a `_cleanup_in_progress` flag. On second SIGINT:
- Skip graceful cleanup entirely
- Close SSH connection immediately (kills remote processes)
- Exit with code 130

---

### 2. Job Raises Exception (Critical Failure)

When a job raises an unhandled exception, the TaskGroup catches it and cancels other tasks. The Orchestrator logs at CRITICAL level and offers rollback.

```mermaid
sequenceDiagram
    participant Orchestrator
    participant TaskGroup
    participant Job
    participant DiskSpaceMonitor
    participant Logger
    participant TerminalUI

    Orchestrator->>Job: execute()
    Job->>Job: raises RuntimeError
    Job-->>TaskGroup: exception propagates

    Note over TaskGroup: Exception in task group
    TaskGroup->>DiskSpaceMonitor: CancelledError
    DiskSpaceMonitor->>DiskSpaceMonitor: cleanup

    TaskGroup-->>Orchestrator: ExceptionGroup

    Orchestrator->>Logger: log(CRITICAL, error_msg)
    Logger->>TerminalUI: add_log(CRITICAL)

    Note over Orchestrator: skip remaining jobs

    alt pre-sync snapshots exist
        Orchestrator->>TerminalUI: offer rollback
    end

    Orchestrator->>Logger: log summary (FAILED)
```

**Key points:**
- TaskGroup automatically cancels sibling tasks when one fails
- No manual `request_termination()` needed
- CRITICAL log entry written with full exception details
- Rollback offered if pre-sync snapshots exist

---

### 3. Remote Command Fails

When a command executed on the target machine fails, RemoteExecutor returns a `CommandResult`. The Job interprets the result and decides how to handle it, including what to log.

```mermaid
sequenceDiagram
    participant Job
    participant RemoteExecutor
    participant Connection
    participant Logger

    Job->>RemoteExecutor: run_command("apt install pkg")
    RemoteExecutor->>Connection: create_process
    Connection-->>RemoteExecutor: process handle

    loop read stdout/stderr async
        Connection-->>RemoteExecutor: output chunks
    end

    Note over Connection: process exits with code 1
    Connection-->>RemoteExecutor: exit_code=1

    RemoteExecutor-->>Job: CommandResult(success=False, exit_code=1, stdout=..., stderr=...)

    Note over Job: Job interprets result

    alt recoverable error
        Job->>Logger: log(ERROR, "apt failed, continuing...")
        Note over Job: continue execution
    else unrecoverable error
        Job->>Logger: log(CRITICAL, "apt failed fatally")
        Job->>Job: raise RuntimeError
    end
```

**Key points:**
- `CommandResult` contains: `success`, `exit_code`, `stdout`, `stderr`
- Job has full control over interpretation and logging
- Job decides log level based on context (same failure might be ERROR or CRITICAL)
- No mandatory output protocol - Job parses stdout/stderr as needed

---

### 4. Job Logs a Message

Jobs call the logger, which publishes events to the EventBus. Each consumer (FileLogger, TerminalUI) receives events in its own queue.

```mermaid
sequenceDiagram
    participant Job
    participant Logger
    participant EventBus
    participant FileLoggerQueue as FileLogger Queue
    participant TUIQueue as TerminalUI Queue
    participant FileLogger
    participant TerminalUI

    Job->>Logger: log(INFO, "Installing package X")
    Logger->>EventBus: publish(LogEvent(...))

    par Fan-out to consumers
        EventBus->>FileLoggerQueue: put(event)
    and
        EventBus->>TUIQueue: put(event)
    end

    par Async consumption
        FileLoggerQueue-->>FileLogger: get()
        Note over FileLogger: check file_level
        alt file_level <= INFO
            FileLogger->>FileLogger: write JSON line
        end
    and
        TUIQueue-->>TerminalUI: get()
        Note over TerminalUI: check cli_level
        alt cli_level <= INFO
            TerminalUI->>TerminalUI: render in log panel
        end
    end
```

**Key points:**
- Jobs call `self.log(level, message, **context)` which publishes to EventBus
- EventBus fans out to per-consumer queues (guaranteed delivery)
- FileLogger and TerminalUI consume independently, never blocking each other
- Each consumer applies its own level filter (`file_level` / `cli_level`)
- File output uses structlog JSONRenderer (one JSON object per line)
- Terminal output uses Rich formatting with color-coded levels

---

### 5. Job Reports Progress

Progress updates support multiple formats: percentage, count-based, or heartbeat.

```mermaid
sequenceDiagram
    participant Job
    participant TerminalUI
    participant Logger

    Note over Job: Percentage-based progress
    Job->>TerminalUI: report_progress(ProgressUpdate(percent=45, item="file.txt"))
    Note over TerminalUI: [████░░░░] 45% file.txt

    Note over Job: Count-based progress (total known)
    Job->>TerminalUI: report_progress(ProgressUpdate(current=45, total=100, item="packages"))
    Note over TerminalUI: 45/100 packages

    Note over Job: Count-based progress (total unknown)
    Job->>TerminalUI: report_progress(ProgressUpdate(current=45, item="files synced"))
    Note over TerminalUI: 45 files synced

    Note over Job: Heartbeat only
    Job->>TerminalUI: report_progress(ProgressUpdate(heartbeat=True))
    Note over TerminalUI: [spinner] still running...

    TerminalUI->>Logger: log(FULL, progress details)
```

**Key points:**
- `ProgressUpdate` supports: `percent`, `current`, `total`, `item`, `heartbeat`
- UI renders appropriately based on which fields are set
- Progress logged at FULL level for audit trail
- UI updates are batched/throttled to prevent excessive redraws

---

### 6. DiskSpaceMonitor Detects Low Space

Two `DiskSpaceMonitorJob` instances run as background tasks: one for source (local), one for target (remote). When space falls below threshold on either host, the job raises `DiskSpaceCriticalError` with both `host` (role) and `hostname` (actual name). The TaskGroup catches this and cancels other tasks.

```mermaid
sequenceDiagram
    participant SourceMonitor as DiskSpaceMonitorJob (source)
    participant LocalExecutor
    participant TargetMonitor as DiskSpaceMonitorJob (target)
    participant RemoteExecutor
    participant TaskGroup
    participant CurrentJob
    participant Logger

    par background monitoring
        loop source monitoring
            SourceMonitor->>LocalExecutor: run_command("df -h /")
            LocalExecutor-->>SourceMonitor: CommandResult
            Note over SourceMonitor: check free space
        end
    and
        loop target monitoring
            TargetMonitor->>RemoteExecutor: run_command("df -h /")
            RemoteExecutor-->>TargetMonitor: CommandResult
            Note over TargetMonitor: check free space
        end
    end

    Note over TargetMonitor: desktop-home: 12% free < 15% threshold

    TargetMonitor->>Logger: log(CRITICAL, "desktop-home: Disk space below threshold")
    TargetMonitor->>TargetMonitor: raise DiskSpaceCriticalError(host=TARGET, hostname="desktop-home")

    DiskSpaceCriticalError-->>TaskGroup: exception propagates
    TaskGroup->>SourceMonitor: CancelledError
    TaskGroup->>CurrentJob: CancelledError
    CurrentJob->>CurrentJob: cleanup in except handler

    Note over TaskGroup: All tasks cancelled/completed
    TaskGroup-->>Orchestrator: ExceptionGroup with DiskSpaceCriticalError
```

**Key points:**
- Two instances of same class, each with different `host` parameter
- Both instances receive same `JobContext` with `source` and `target` executors
- Job selects executor based on its `host` field (see code example in "Job Approaches")
- `DiskSpaceCriticalError` includes both `host` (role) and `hostname` (actual name)
- Either monitor can trigger sync abort - TaskGroup cancels all other tasks

---

## Streaming Output Architecture

Multiple concurrent sources produce output that flows through the EventBus to consumers.

```mermaid
graph TD
    subgraph Producers ["Producers (asyncio tasks)"]
        OrchestratorTask["Orchestrator"]
        JobTask["Current Job"]
        DiskMonSourceTask["DiskSpaceMonitor<br/>(source)"]
        DiskMonTargetTask["DiskSpaceMonitor<br/>(target)"]
        ConnTask["Connection"]
    end

    subgraph EventBus ["Event Bus"]
        Publish["publish()"]
        FLQ["FileLogger Queue"]
        UIQ["TerminalUI Queue"]
    end

    subgraph Consumers ["Consumers (asyncio tasks)"]
        FileLogger["FileLogger"]
        TerminalUI["TerminalUI"]
    end

    OrchestratorTask -->|LogEvent, ProgressEvent| Publish
    JobTask -->|LogEvent, ProgressEvent| Publish
    DiskMonSourceTask -->|LogEvent| Publish
    DiskMonTargetTask -->|LogEvent| Publish
    ConnTask -->|ConnectionEvent| Publish

    Publish --> FLQ
    Publish --> UIQ

    FLQ --> FileLogger
    UIQ --> TerminalUI

    FileLogger --> LogFile["sync-*.log"]
    TerminalUI --> Terminal["Terminal Display"]

    style EventBus fill:#fff3e0
    style Producers fill:#e8f5e9
    style Consumers fill:#e1f5ff
```

### Data Flow via EventBus

| Producer | Event Type | Fields |
|----------|------------|--------|
| Orchestrator | ProgressEvent | Overall progress (step N/M) |
| Jobs | LogEvent | Log messages at any level |
| Jobs | ProgressEvent | Job progress (%, count, heartbeat) |
| Connection | ConnectionEvent | Status changes, latency |

### Concurrency Model

- **Per-consumer queues**: Each consumer has dedicated queue, no blocking between consumers
- **Non-blocking puts**: Producers never wait; `put_nowait()` fans out to all consumer queues
- **UI Refresh Task**: TerminalUI consumes from its queue, renders at fixed interval (e.g., 100ms)
- **FileLogger Task**: FileLogger consumes from its queue, writes JSON lines to disk

### Target Log Aggregation (FR-023)

All logging happens on the **source machine**. Target-side operations do not run independent logging processes—instead:

1. **Command output**: Jobs run commands on target via `RemoteExecutor.run_command()`. The job receives `CommandResult` with stdout/stderr and decides what to log.

2. **Background processes**: Jobs can stream output via `RemoteExecutor.start_process()` with async iteration over stdout. The job parses output and emits LogEvents to the source-side EventBus.

3. **DiskSpaceMonitor (target)**: Runs on source, executes commands on target via RemoteExecutor. Logs are emitted from source-side code with `host=TARGET`.

```mermaid
graph LR
    subgraph Source Machine
        Job["Job (source-side code)"]
        EventBus["EventBus"]
        FileLogger["FileLogger"]
        TerminalUI["TerminalUI"]
    end

    subgraph Target Machine
        RemoteCmd["Remote Command"]
    end

    Job -->|run_command| RemoteCmd
    RemoteCmd -->|stdout/stderr| Job
    Job -->|LogEvent host=TARGET| EventBus
    EventBus --> FileLogger
    EventBus --> TerminalUI
```

**Key point**: There is no target-side logging daemon. The `host` field in LogEvent indicates which machine the log *relates to*, not where the logging code runs. All logging code runs on source.

---

## Command Execution

Jobs have access to both `source` (LocalExecutor) and `target` (RemoteExecutor) via JobContext. They choose which to use based on the operation. There is **no mandatory output protocol** - Jobs interpret stdout/stderr as needed.

### Executor Interface

```python
class Executor(Protocol):
    """Common interface for command execution on source or target."""

    async def run_command(self, cmd: str, timeout: float | None = None) -> CommandResult:
        """Run command, wait for completion, return result."""

    async def start_process(self, cmd: str) -> Process:
        """Start command and return Process handle for streaming output."""

    async def terminate_all_processes(self) -> None:
        """Kill all processes started by this executor."""


class Process:
    """Handle for a running process with streaming output."""

    async def stdout(self) -> AsyncIterator[str]:
        """Yield stdout lines as they arrive."""

    async def stderr(self) -> AsyncIterator[str]:
        """Yield stderr lines as they arrive."""

    async def wait(self) -> CommandResult:
        """Wait for completion and return result."""

    async def terminate(self) -> None:
        """Send termination signal to process."""
```

### Implementations

| Implementation | Description |
|----------------|-------------|
| **LocalExecutor** | Runs commands via async subprocess on source machine |
| **RemoteExecutor** | Runs commands via SSH on target machine. Adds: `send_file()`, `get_file()`, `get_hostname()` |

### Job Approaches for Commands

Jobs choose their approach based on complexity:

**(a) Commands on source or target**:
```python
# Run on source machine
result = await self.source.run_command("df -h /")

# Run on target machine
result = await self.target.run_command("df -h /")
```

**(b) Jobs using both executors** (e.g., comparing source vs target):
```python
# Get package list from both machines
source_pkgs = await self.source.run_command("dpkg --get-selections")
target_pkgs = await self.target.run_command("dpkg --get-selections")
# Compare and sync differences...
```

**(c) Parameterized jobs** (DiskSpaceMonitorJob, BtrfsSnapshotJob):
```python
# Job has host: Host field, selects executor based on it
executor = self.source if self.host == Host.SOURCE else self.target
result = await executor.run_command("df -h /")
```

**(d) Streaming with AsyncIterator**: Process output as it arrives
```python
process = await self.target.start_process("btrfs subvolume snapshot ...")
lines = []
async for line in process.stdout():
    lines.append(line)
    self.report_progress(ProgressUpdate(current=len(lines), item=line))

result = await process.wait()
if not result.success:
    raise RuntimeError(f"snapshot failed: {result.stderr}")
```

**(e) File transfer + execution**:
```python
await self.target.send_file(local_script, "/tmp/sync-helper.py")
result = await self.target.run_command("python3 /tmp/sync-helper.py")
```

Jobs are responsible for:
- Interpreting command output and exit code
- Deciding appropriate log levels and error handling
- Reporting progress based on overall job state (not per-command)
- Checking `result.success` to determine next action

---

## Execution Flow Summary

```mermaid
graph TD
    Start["pc-switcher sync &lt;target&gt;"]

    CLI["<b>CLI</b><br/>- Parse arguments<br/>- Load config file (YAML)"]

    SchemaVal["<b>Schema Validation</b><br/>Orchestrator validates:<br/>- Required fields<br/>- Types"]

    JobConfigVal["<b>Job Config Validation</b><br/>Job.validate_config() for each job"]

    Connect["<b>Establish SSH Connection</b>"]

    SystemVal["<b>System State Validation</b><br/>Job.validate() for each job<br/>- Check subvolumes exist<br/>- Check connectivity"]

    SnapPre["<b>BtrfsSnapshotJob pre</b><br/>Direct btrfs commands<br/>No pc-switcher needed"]

    InstallTarget["<b>InstallOnTargetJob</b><br/>Check/install/upgrade<br/>pc-switcher on target"]

    StartDiskMon["<b>Start background</b><br/>DiskSpaceMonitorJob x2<br/>(source + target)"]

    SyncJobs["<b>Sequential job execution</b><br/>- SyncJob1.execute<br/>- SyncJob2.execute<br/>- ..."]

    SnapPost["<b>BtrfsSnapshotJob post</b><br/>Create post-sync snapshots"]

    CancelMon["<b>Cancel DiskSpaceMonitor</b>"]

    Disconnect["<b>Disconnect SSH</b>"]

    Result["<b>Return SyncResult</b><br/>success/failure,<br/>job summaries"]

    Start --> CLI
    CLI --> SchemaVal
    SchemaVal --> JobConfigVal
    JobConfigVal --> Connect
    Connect --> SystemVal
    SystemVal --> SnapPre
    SnapPre --> InstallTarget
    InstallTarget --> StartDiskMon
    StartDiskMon --> SyncJobs
    SyncJobs --> SnapPost
    SnapPost --> CancelMon
    CancelMon --> Disconnect
    Disconnect --> Result

    style Start fill:#fff3e0
    style CLI fill:#e1f5ff
    style SchemaVal fill:#fff3e0
    style JobConfigVal fill:#fff3e0
    style Connect fill:#f3e5f5
    style SystemVal fill:#e8f5e9
    style SnapPre fill:#e8f5e9
    style InstallTarget fill:#e8f5e9
    style StartDiskMon fill:#fce4ec
    style SyncJobs fill:#e8f5e9
    style SnapPost fill:#e8f5e9
    style CancelMon fill:#fce4ec
    style Disconnect fill:#f3e5f5
    style Result fill:#fff3e0
```

**Key ordering notes:**
1. **Snapshots BEFORE InstallOnTargetJob**: BtrfsSnapshotJob runs direct `btrfs` commands via SSH, no pc-switcher dependency. This ensures we have a rollback point before ANY system modifications.
2. **Three validation phases**: Schema → Job config → System state, with distinct error messages.
3. **DiskSpaceMonitor as background tasks**: Two instances run throughout sync - one monitors source (local commands), one monitors target (via `RemoteExecutor`). Either can abort sync on low space.

---

## Rollback Workflow

Per FR-013, the system provides rollback capability to restore from pre-sync snapshots. Rollback requires explicit user confirmation.

### CLI Command

```bash
# List available rollback points
pc-switcher rollback --list

# Rollback to most recent pre-sync snapshot
pc-switcher rollback

# Rollback to specific session
pc-switcher rollback --session abc12345
```

### Rollback Process

```mermaid
sequenceDiagram
    actor User
    participant CLI
    participant Orchestrator
    participant SourceBtrfs as Source btrfs
    participant TargetBtrfs as Target btrfs

    User->>CLI: pc-switcher rollback
    CLI->>CLI: List available pre-sync snapshots
    CLI->>User: Display snapshot options

    User->>CLI: Confirm selection
    Note over CLI: Requires explicit confirmation

    CLI->>Orchestrator: Execute rollback

    par Rollback source
        Orchestrator->>SourceBtrfs: btrfs subvolume snapshot (restore)
        Note over SourceBtrfs: For each subvolume:<br/>1. Rename current → .backup<br/>2. Snapshot presync → current<br/>3. Delete .backup
    and Rollback target
        Orchestrator->>TargetBtrfs: btrfs subvolume snapshot (restore)
        Note over TargetBtrfs: Same process via SSH
    end

    Orchestrator->>CLI: Rollback complete
    CLI->>User: "Rollback to session abc12345 complete"
```

### Rollback Steps (per subvolume)

1. **Validate**: Verify pre-sync snapshot exists
2. **Rename current**: `mv /btrfs-root/@home /btrfs-root/@home.rollback-backup`
3. **Restore snapshot**: `btrfs subvolume snapshot /btrfs-root/@home-presync-... /btrfs-root/@home`
4. **Delete backup**: `btrfs subvolume delete /btrfs-root/@home.rollback-backup`

### Post-Sync Snapshots During Rollback

Post-sync snapshots (if they exist from a successful sync) are **retained** during rollback. They can be manually deleted via `pc-switcher cleanup-snapshots` if no longer needed.

### Error Handling

- If rollback fails mid-process, the `.rollback-backup` subvolumes are preserved for manual recovery
- Rollback logs all operations at INFO level for audit trail
- User must have sudo privileges for btrfs operations

---

## Setup and Default Configuration (FR-037)

The setup script creates a default configuration file with inline comments explaining each setting:

```yaml
# ~/.config/pc-switcher/config.yaml
# PC-Switcher Configuration

# Logging levels: DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL
# DEBUG = most verbose (internal diagnostics)
# FULL = operational details (file-level operations)
# INFO = high-level operations (recommended for terminal)
log_file_level: FULL    # Written to ~/.local/share/pc-switcher/logs/
log_cli_level: INFO     # Displayed in terminal

# Enable/disable sync jobs (true = enabled, false = skipped)
# Required jobs (snapshots) cannot be disabled
sync_jobs:
  user_data: true       # Sync /home and /root
  packages: true        # Sync apt, snap, flatpak packages
  docker: false         # Sync Docker images, containers, volumes
  vms: false            # Sync KVM/virt-manager VMs
  k3s: false            # Sync k3s cluster state

# Disk space safety thresholds
disk:
  preflight_minimum: "20%"   # Minimum free space before sync starts
  runtime_minimum: "15%"     # Minimum during sync (abort if below)
  check_interval: 30         # Seconds between runtime checks

# Btrfs snapshot configuration (cannot be disabled)
btrfs_snapshots:
  subvolumes:           # Subvolume names to snapshot (must exist on both machines)
    - "@"               # Root filesystem
    - "@home"           # Home directories
  keep_recent: 3        # Always keep N most recent sync sessions
  max_age_days: null    # Delete snapshots older than N days (null = no age limit)
```

**Implementation**: The setup script generates this file by:
1. Loading the schema from `config-schema.yaml`
2. Extracting `description` fields from each property
3. Generating YAML with descriptions as inline comments
4. Writing to `~/.config/pc-switcher/config.yaml`
