# Foundation Architecture

This document describes the architecture for the pc-switcher foundation infrastructure, covering the core components, their relationships, and key interaction patterns.

## Design Principles

- **Asyncio-native**: All I/O operations are async, enabling concurrent execution without threads
- **Single SSH connection**: Multiplexed sessions over one connection for efficiency
- **Structured communication**: JSONL protocol for target-side output parsing
- **Clear separation**: Jobs are isolated units; orchestrator handles coordination
- **Fail-safe**: Graceful degradation and proper cleanup on errors/interrupts

---

## Component Architecture

```mermaid
graph TD
    CLI["<b>CLI</b><br/>- Parses arguments sync &lt;target&gt;<br/>- Loads configuration<br/>- Creates and runs Orchestrator"]

    Orchestrator["<b>Orchestrator</b><br/>- Manages sync session lifecycle<br/>- Handles SIGINT Ctrl+C gracefully<br/>- Coordinates job validation/execution<br/>- Manages background tasks<br/>- Aggregates results & summary"]

    Config["<b>Config</b><br/>- Load YAML<br/>- Validate<br/>- Defaults<br/>- Schema"]

    Connection["<b>Connection</b><br/>- SSH via asyncssh<br/>- Multiplexed sessions<br/>- Health check"]

    Logger["<b>Logger</b><br/>- structlog<br/>- File output<br/>- CLI output<br/>- 6 levels"]

    TerminalUI["<b>TerminalUI</b><br/>- Rich Live<br/>- Progress bars<br/>- Log messages<br/>- Status"]

    RemoteExecutor["<b>RemoteExecutor</b><br/>- run_command()<br/>- stream output<br/>- send/get file<br/>- parse JSONL"]

    SelfInstallJob["<b>SelfInstallJob</b><br/>- Check version<br/>- Install/upgrade<br/>- Verify<br/>[required]"]

    BtrfsSnapshotJob["<b>BtrfsSnapshotJob</b><br/>- pre/post mode<br/>- Create RO snapshots<br/>- Rollback support<br/>[required]"]

    SyncJobs["<b>SyncJobs</b><br/>- User data<br/>- Packages<br/>- Docker<br/>- VMs<br/>- k3s<br/>[configurable]"]

    DiskSpaceMonitor["<b>DiskSpaceMonitor</b><br/>- Periodic check<br/>- Source+target<br/>- Abort if low<br/>[required]"]

    CLI --> Orchestrator
    Orchestrator --> Config
    Orchestrator --> Connection
    Orchestrator --> Logger
    Orchestrator --> TerminalUI
    Connection --> RemoteExecutor
    Orchestrator --> SelfInstallJob
    Orchestrator --> BtrfsSnapshotJob
    Orchestrator --> SyncJobs
    Orchestrator --> DiskSpaceMonitor

    style CLI fill:#e1f5ff
    style Orchestrator fill:#fff3e0
    style RemoteExecutor fill:#f3e5f5
    style SelfInstallJob fill:#e8f5e9
    style BtrfsSnapshotJob fill:#e8f5e9
    style SyncJobs fill:#e8f5e9
    style DiskSpaceMonitor fill:#fce4ec
```

### Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| **CLI** | Entry point. Parses commands (`sync`, `logs`, `cleanup-snapshots`), loads config, instantiates and runs Orchestrator |
| **Orchestrator** | Central coordinator. Manages job lifecycle, signal handling, background tasks, and produces final sync summary |
| **Config** | Loads `~/.config/pc-switcher/config.yaml`, validates against JSON schemas, applies defaults |
| **Connection** | Manages SSH connection via asyncssh. Provides multiplexed sessions (multiple concurrent commands over single connection) |
| **RemoteExecutor** | Job-facing interface to Connection. Runs commands, transfers files, parses JSONL output from target |
| **Logger** | Unified logging with 6 levels. Routes to file (JSON) and terminal (formatted). Aggregates source + target logs |
| **TerminalUI** | Rich-based live display. Shows progress bars, log messages (filtered by cli_level), overall status |
| **Jobs** | Encapsulated sync operations. Each job validates config, executes operations, reports progress, handles termination |

---

## Class Diagram

```mermaid
classDiagram
    class Job {
        <<abstract>>
        +name: str
        +required: bool = False
        #_termination_requested: bool = False
        #_context: JobContext | None = None
        +get_config_schema()* dict
        +validate()* list~ValidationError~
        +execute()* None
        +request_termination() None
        +termination_requested* bool
        #log(level, message, **context) None
        #report_progress(pct, item, remaining) None
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

    class SelfInstallJob {
        +execute() None
        - check version
        - uv install
        - verify
    }

    class BtrfsSnapshotJob {
        +phase: str
        +execute() None
    }

    class DummySuccess {
    }

    class DummyFail {
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
        +execute() None
        - loop:
          - check source
          - check target
          - sleep
    }

    Job <|-- SystemJob
    Job <|-- SyncJob
    Job <|-- BackgroundJob
    SystemJob <|-- SelfInstallJob
    SystemJob <|-- BtrfsSnapshotJob
    SyncJob <|-- DummySuccess
    SyncJob <|-- DummyFail
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
        +remote: RemoteExecutor
        +session_id: str
    }

    class RemoteExecutor {
        -_connection: Connection
        -_logger: Logger
        -_ui: TerminalUI
        +run_command(cmd, timeout) CommandResult
        +run_command_stream(cmd) AsyncIterator~OutputLine~
        +send_file(local, remote) None
        +get_file(remote, local) None
        +get_hostname() str
        -_parse_output_line(line) ParsedLine
        -_route_log(parsed) None
        -_route_progress(parsed) None
    }

    class Connection {
        -_conn: SSHClientConnection
        -_session_semaphore: Semaphore
        -_target: str
        -_connected: bool
        +connect() None
        +disconnect() None
        +create_process(cmd) SSHClientProcess
        +sftp() SFTPClient
        +check_health() bool
        +__aenter__()
        +__aexit__()
    }

    class Orchestrator {
        -_config: Config
        -_connection: Connection
        -_logger: Logger
        -_ui: TerminalUI
        -_jobs: list~Job~
        -_current_job: Job | None
        -_background_tasks: list~Task~
        -_shutdown_requested: bool
        +run() SyncResult
        -_setup_signal_handlers() None
        -_handle_sigint() None
        -_validate_all_jobs() list~ValidationError~
        -_execute_job(job) JobResult
        -_start_background_job(job) Task
        -_request_shutdown(reason) None
        -_cleanup() None
    }

    class Logger {
        -_file_level: LogLevel
        -_cli_level: LogLevel
        -_structlog_logger: BoundLogger
        -_ui: TerminalUI
        -_log_file: Path
        +log(level, job, hostname, message, **ctx) None
        +debug() None
        +full() None
        +info() None
        +warning() None
        +error() None
        +critical() None
        +get_job_logger(job_name, hostname) JobLogger
    }

    class TerminalUI {
        -_console: Console
        -_live: Live
        -_progress: Progress
        -_job_tasks: dict~str, TaskID~
        -_log_panel: deque~LogEntry~
        +start() None
        +stop() None
        +update_job_progress(job, pct, item, remaining) None
        +add_log_message(level, job, message) None
        +set_overall_progress(step, total, description) None
        +set_connection_status(connected, latency) None
        +__aenter__()
        +__aexit__()
    }

    JobContext --> RemoteExecutor : uses
    RemoteExecutor --> Connection : wraps
    RemoteExecutor --> Logger : routes logs
    RemoteExecutor --> TerminalUI : routes progress
    Orchestrator --> Connection : owns
    Orchestrator --> Logger : uses
    Orchestrator --> TerminalUI : uses
    Orchestrator --> Job : manages
    Logger --> TerminalUI : sends messages
```

### Class Relationships

| Relationship | Description |
|--------------|-------------|
| Orchestrator → Connection | Owns and manages the SSH connection lifecycle |
| Orchestrator → Job[] | Creates, validates, and executes jobs in order |
| Job → JobContext | Receives context at execution time (config, remote, session_id) |
| JobContext → RemoteExecutor | Jobs use this to run commands on target |
| RemoteExecutor → Connection | Wraps Connection with job-friendly interface |
| RemoteExecutor → Logger | Routes parsed target output to logging |
| RemoteExecutor → TerminalUI | Routes progress updates to UI |
| Logger → TerminalUI | Sends log messages for display (if level >= cli_level) |

---

## Sequence Diagrams

### 1. User Aborts with Ctrl+C

When the user presses Ctrl+C, the orchestrator catches SIGINT, requests the current job to terminate gracefully, and performs cleanup. A second Ctrl+C forces immediate termination.

```mermaid
sequenceDiagram
    actor User
    participant Orchestrator
    participant CurrentJob
    participant Connection
    participant TerminalUI

    User->>Orchestrator: Ctrl+C
    Note over Orchestrator: SIGINT handler triggered
    Orchestrator->>CurrentJob: request_termination()
    Orchestrator->>TerminalUI: log(WARNING, "Sync interrupted")
    Orchestrator->>Connection: send SIGTERM to target processes
    Orchestrator->>CurrentJob: wait up to 5s for cleanup
    alt 2nd Ctrl+C before cleanup completes
        User->>Orchestrator: Ctrl+C
        Orchestrator->>CurrentJob: force terminate
        Orchestrator->>Connection: disconnect()
    end
    Orchestrator->>TerminalUI: stop()
    Orchestrator->>User: exit(130)
```

**Key points:**
- SIGINT handler sets `_shutdown_requested` flag and calls `request_termination()` on current job
- Job checks `termination_requested` property in its execution loop
- Grace period of 5 seconds for cleanup; force-kill after second SIGINT
- Exit code 130 indicates SIGINT termination (128 + signal number 2)

---

### 2. Job Raises Exception (Critical Failure)

When a job raises an unhandled exception, the orchestrator wraps it as a SyncError, logs at CRITICAL level, requests termination, and aborts the sync.

```mermaid
sequenceDiagram
    participant Orchestrator
    participant Job
    participant Logger
    participant TerminalUI
    participant Connection

    Orchestrator->>Job: execute()
    Job->>Job: [raises RuntimeError]
    Job-->>Orchestrator: exception
    Note over Orchestrator: wrap as SyncError
    Orchestrator->>Logger: log(CRITICAL, error_msg)
    Logger->>TerminalUI: add_log(CRIT)
    Orchestrator->>Job: request_termination()
    Orchestrator->>Job: wait up to 5s for cleanup
    Note over Orchestrator: skip remaining jobs
    alt snapshots exist
        Orchestrator->>TerminalUI: offer rollback
    end
    Orchestrator->>Logger: log summary (FAILED)
```

**Key points:**
- Any unhandled exception is caught and wrapped as `SyncError`
- CRITICAL log entry written with full exception details
- Job receives termination request for cleanup opportunity
- Remaining jobs are skipped; rollback offered if pre-sync snapshots exist
- Final summary shows which jobs succeeded/failed

---

### 3. Remote Command Fails

When a command executed on the target machine fails (non-zero exit code), RemoteExecutor returns a failure result. The job decides how to handle it.

```mermaid
sequenceDiagram
    participant Job
    participant RemoteExecutor
    participant Connection
    participant Logger
    participant Orchestrator

    Job->>RemoteExecutor: run_command(cmd)
    RemoteExecutor->>Connection: create_process
    Connection-->>RemoteExecutor: process
    loop read async
        RemoteExecutor->>Connection: receive output
        Connection-->>RemoteExecutor: output lines
    end
    Note over Connection: process exits with code != 0
    Connection-->>RemoteExecutor: exit_code=1
    RemoteExecutor-->>Job: CommandResult(success=False, exit_code=1, stderr=...)
    alt Recoverable error
        Job->>Logger: log(ERROR, message)
        Note over Job: continue execution
    else Unrecoverable error
        Job->>Orchestrator: raise SyncError
    end
```

**Key points:**
- `CommandResult` contains: `success`, `exit_code`, `stdout`, `stderr`
- Job has full control over error handling strategy
- Recoverable errors: log at ERROR level, continue execution
- Unrecoverable errors: raise `SyncError`, triggers critical failure flow

---

### 4. Job Logs a Message

Jobs log messages via their context. The Logger routes to file (JSON) and terminal (if level >= cli_level).

```mermaid
sequenceDiagram
    participant Job
    participant JobContext
    participant Logger
    participant TerminalUI
    participant File

    Job->>JobContext: log(INFO, "msg")
    JobContext->>Logger: log(INFO, job="myjob", host="source", msg="msg")
    Note over Logger: check file_level >= FULL
    Logger->>File: write JSON line
    Note over File: {"ts": "...", "level": "INFO",<br/>"job": "myjob", "host": "src",<br/>"event": "msg"}
    Note over Logger: check cli_level >= INFO
    Logger->>TerminalUI: add_log_message()
    Note over TerminalUI: render in log panel
```

**Key points:**
- Jobs call `self.log(level, message, **context)` which delegates to JobContext
- Logger applies two independent filters: `file_level` and `cli_level`
- File output uses structlog JSONRenderer (one JSON object per line)
- Terminal output uses Rich formatting with color-coded levels

---

### 5. Job Reports Progress

Progress updates are displayed in the terminal UI and logged at FULL level.

```mermaid
sequenceDiagram
    participant Job
    participant JobContext
    participant TerminalUI
    participant Logger

    Job->>JobContext: report_progress(pct=45, item="file.txt", remaining=30s)
    JobContext->>TerminalUI: update_job_progress(...)
    Note over TerminalUI: update progress bar<br/>show "45% - file.txt"<br/>show "~30s remaining"
    JobContext->>Logger: log(FULL, ...)
    Note over Logger: if file_level <= FULL<br/>write to log file
```

**Key points:**
- Progress is optional but recommended for long-running operations
- UI shows per-job progress bar with percentage, current item, and ETA
- Progress also logged at FULL level for audit trail
- UI updates are batched/throttled to prevent excessive redraws

---

### 6. DiskSpaceMonitor Detects Low Space

The DiskSpaceMonitor runs as a background task, periodically checking disk space. When space falls below threshold, it triggers shutdown.

```mermaid
sequenceDiagram
    participant DiskSpaceMonitor
    participant RemoteExecutor
    participant Orchestrator
    participant Logger
    participant TerminalUI

    loop background loop
        DiskSpaceMonitor->>RemoteExecutor: run_command("df")
        RemoteExecutor->>RemoteExecutor: [execute on target]
        RemoteExecutor-->>DiskSpaceMonitor: disk_info
        Note over DiskSpaceMonitor: parse: 12% free, threshold 15%
        alt space below runtime_minimum
            DiskSpaceMonitor->>Logger: log(CRITICAL, "Target disk space below threshold")
            Logger->>TerminalUI: add_log(CRITICAL)
            DiskSpaceMonitor->>Orchestrator: request_shutdown(reason="low_disk_space")
            Note over Orchestrator: _shutdown_requested = True
            Note over Orchestrator: Current job sees termination_requested
            Note over Orchestrator: Orchestrator aborts sync
        end
    end
```

**Key points:**
- DiskSpaceMonitor checks both source and target at configurable interval
- Thresholds: `disk.preflight_minimum` (before sync) and `disk.runtime_minimum` (during sync)
- Values support percentage ("15%") or absolute ("40GiB")
- On detection, logs CRITICAL and calls `orchestrator.request_shutdown()`
- Current job receives termination request; remaining jobs skipped

---

## Streaming Output Architecture

Multiple concurrent sources produce output that must be displayed coherently in the terminal.

```mermaid
graph TD
    TerminalUI["<b>TerminalUI</b><br/>Rich Live Display"]
    
    OverallProgress["<b>Overall Progress</b><br/>Step 3/5<br/>Packages"]
    JobProgress["<b>Job Progress Bars</b><br/>[████░░] 45%<br/>file.txt"]
    LogPanel["<b>Log Panel</b><br/>[INFO] ...<br/>[WARN] ..."]
    
    ProgressChannel["<b>Progress Channel</b><br/>asyncio.Queue"]
    
    Orchestrator["<b>Orchestrator</b><br/>set_overall_progress()"]
    Jobs["<b>Jobs</b><br/>report_progress()"]
    Logger["<b>Logger</b><br/>route to UI<br/>if cli_level >= level"]
    
    EventLoop["<b>asyncio Event Loop</b><br/>- Orchestrator Task<br/>- Current Job Task<br/>- DiskSpaceMonitor<br/>- UI Refresh Task<br/>- Connection Health Task<br/>- Signal Handler"]
    
    TerminalUI --> OverallProgress
    TerminalUI --> JobProgress
    TerminalUI --> LogPanel
    
    ProgressChannel --> TerminalUI
    
    Orchestrator --> ProgressChannel
    Jobs --> ProgressChannel
    Logger --> TerminalUI
    
    Orchestrator --> EventLoop
    Jobs --> EventLoop
    Logger --> EventLoop
    
    style TerminalUI fill:#e1f5ff
    style ProgressChannel fill:#fff3e0
    style EventLoop fill:#f3e5f5
    style Orchestrator fill:#e8f5e9
    style Jobs fill:#e8f5e9
    style Logger fill:#e8f5e9
```

### Data Flow

| Source | Data Type | Destination |
|--------|-----------|-------------|
| Orchestrator | Overall progress (step N/M) | TerminalUI.set_overall_progress() |
| Jobs | Progress updates (%, item, ETA) | TerminalUI.update_job_progress() |
| Logger | Log messages (level >= cli_level) | TerminalUI.add_log_message() |
| Connection | Health status | TerminalUI.set_connection_status() |

### Concurrency Model

- **UI Refresh Task**: Runs at fixed interval (e.g., 100ms), renders current state
- **No locks needed**: Each component updates its own state; UI reads atomically during render
- **Backpressure**: Progress updates can be dropped if UI can't keep up (latest value wins)

---

## Target Communication Protocol (JSONL)

Target-side scripts output structured JSON Lines to stdout. Each line is a complete JSON object.

### Message Types

```json
{"type": "log", "level": "INFO", "message": "Starting file copy", "ts": "2025-11-27T10:30:00Z"}
{"type": "progress", "percent": 25, "item": "/home/user/docs/file.txt", "remaining_seconds": 45}
{"type": "log", "level": "DEBUG", "message": "Copied 1024 bytes", "ts": "2025-11-27T10:30:01Z"}
{"type": "progress", "percent": 50, "item": "/home/user/docs/other.txt", "remaining_seconds": 30}
{"type": "result", "success": true, "files_copied": 42, "bytes_transferred": 1048576}
```

### Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | yes | One of: `log`, `progress`, `result` |
| `level` | string | for log | Log level: DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL |
| `message` | string | for log | Log message text |
| `ts` | string | for log | ISO 8601 timestamp |
| `percent` | int | for progress | 0-100 completion percentage |
| `item` | string | for progress | Current item being processed |
| `remaining_seconds` | int | for progress | Estimated seconds remaining |
| `success` | bool | for result | Whether operation succeeded |
| `*` | any | for result | Additional result data (job-specific) |

### Fallback Format

For simple shell scripts, a prefixed line format is also supported:

```text
[INFO] Starting file copy
[PROGRESS:25] /home/user/docs/file.txt
[DEBUG] Copied 1024 bytes
```

The RemoteExecutor parser handles both formats transparently.

---

## Execution Flow Summary

```mermaid
graph TD
    Start["pc-switcher sync &lt;target&gt;"]
    
    CLI["<b>CLI</b><br/>- Parse arguments<br/>- Load config<br/>- Validate config<br/>- Create Orchestrator"]
    
    OrchestraStart["<b>Orchestrator.run()</b><br/>- Setup signal handlers SIGINT<br/>- Establish SSH connection"]
    
    SelfInstall["<b>SelfInstallJob</b><br/>validate: Check source version<br/>execute: Check/install/upgrade<br/>target pc-switcher"]
    
    ValidateJobs["<b>Validate all jobs</b><br/>- DiskSpaceMonitor<br/>- BtrfsSnapshotJob<br/>- SyncJobs"]
    
    StartDiskMon["<b>Start background</b><br/>DiskSpaceMonitor.execute"]
    
    SnapPre["<b>BtrfsSnapshotJob pre</b><br/>Create read-only snapshots<br/>on source and target"]
    
    SyncJobs["<b>Sequential job execution</b><br/>- SyncJob1.execute<br/>- SyncJob2.execute<br/>- ..."]
    
    SnapPost["<b>BtrfsSnapshotJob post</b><br/>Create post-sync snapshots"]
    
    CancelMon["<b>Cancel DiskSpaceMonitor</b>"]
    
    Disconnect["<b>Disconnect SSH</b>"]
    
    Result["<b>Return SyncResult</b><br/>success/failure,<br/>job summaries"]
    
    Start --> CLI
    CLI --> OrchestraStart
    OrchestraStart --> SelfInstall
    SelfInstall --> ValidateJobs
    ValidateJobs --> StartDiskMon
    StartDiskMon --> SnapPre
    SnapPre --> SyncJobs
    SyncJobs --> SnapPost
    SnapPost --> CancelMon
    CancelMon --> Disconnect
    Disconnect --> Result
    
    style Start fill:#fff3e0
    style CLI fill:#e1f5ff
    style OrchestraStart fill:#fff3e0
    style SelfInstall fill:#e8f5e9
    style ValidateJobs fill:#e8f5e9
    style StartDiskMon fill:#fce4ec
    style SnapPre fill:#e8f5e9
    style SyncJobs fill:#e8f5e9
    style SnapPost fill:#e8f5e9
    style CancelMon fill:#fce4ec
    style Disconnect fill:#f3e5f5
    style Result fill:#fff3e0
```

