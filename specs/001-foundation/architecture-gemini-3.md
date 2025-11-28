Here is the architectural design document for **pc-switcher**.

This design leverages **Python 3.13**, **asyncio**, and **uv**, adhering to the principles of Deliberate Simplicity and Reliability. It decouples the UI from the execution logic using an event-driven approach for logging and progress, ensuring the UI remains responsive even during heavy I/O.

-----

# PC-Switcher Architecture & Design

## 1\. Architecture Overview

The system follows a **Layered Architecture** with a strong emphasis on **Dependency Injection** and **Asynchronous Control Flow**.

### Core Layers

1.  **Presentation Layer (UI):** Handles user input (CLI args) and rendering (TUI). It observes state changes but does not drive logic.
2.  **Orchestration Layer:** The "Brain." Manages the lifecycle of the sync session, error handling, and the sequential/parallel execution flow.
3.  **Job Layer:** Contains the specific business logic (User Data, BTRFS Snapshots, Disk Monitor). All units here adhere to a strict `SyncJob` protocol.
4.  **Infrastructure Layer:** Handles low-level I/O—SSH connections, file system operations, and raw process execution.

### Concurrency Model

We use `asyncio`.

  * **Main Event Loop:** driven by the CLI entry point.
  * **Orchestrator:** runs the sequential flow.
  * **DiskMonitor:** runs as a concurrent `asyncio.Task`.
  * **UI Updates:** Consumes a `Queue` of log/progress events to update the screen without blocking execution.

-----

## 2\. Component Diagram

```mermaid
graph TD
    subgraph "Presentation Layer"
        CLI[CLI Entry Point]
        TUI[Terminal UI Renderer]
    end

    subgraph "Orchestration Layer"
        Orch[Orchestrator]
        Config[Configuration Manager]
        LogSys[Logging & Event Bus]
    end

    subgraph "Job Layer"
        JobFactory[Job Factory]
        Proto[<<Protocol>>\nSyncJob]
        
        DiskMon[DiskSpaceMonitor Job]
        SnapJob[BtrfsSnapshot Job]
        FeatJob[Feature Sync Jobs]
        
        DiskMon -.-> Proto
        SnapJob -.-> Proto
        FeatJob -.-> Proto
    end

    subgraph "Infrastructure Layer"
        SSH[Async SSH Client]
        LocalFS[Local Filesystem]
    end

    %% Relationships
    CLI --> Config
    CLI --> LogSys
    CLI --> Orch
    
    Orch --> JobFactory
    Orch --> SSH
    Orch -- Executes --> Proto
    Orch -- Updates --> TUI
    
    JobFactory --> Config
    
    Proto --> SSH
    Proto --> LocalFS
    Proto -- Emits Logs/Progress --> LogSys
    
    LogSys -- Streams Events --> TUI
    LogSys -- Writes --> LocalFS
```

### Component Responsibilities

  * **CLI Entry Point:** Uses `typer` or `click`. Bootstraps the `uvloop`, loads config, and instantiates the Orchestrator.
  * **Logging & Event Bus:** A wrapper around `structlog`. It acts as a multiplexer: it writes JSON logs to the file system and pushes `LogEntry` objects into an `asyncio.Queue` for the TUI to consume.
  * **Orchestrator:** Maintains the `SyncSession` state. It handles the `try/except` blocks for the whole process, manages the "Pre" and "Post" phases, and controls the cancellation of the parallel DiskMonitor.
  * **Job Factory:** Reads the `config.yaml`, determines which jobs are enabled, and instantiates classes ensuring dependencies (SSH, Config) are injected.
  * **SyncJob (Protocol):** The standard interface. Ensures every component (monitor, snapshot, sync) looks the same to the Orchestrator.
  * **Async SSH Client:** A wrapper (likely around `asyncssh` or `asyncio.subprocess` with OpenSSH) that provides a simplified API (`run_command`, `stream_command`) and connection persistence.

-----

## 3\. Class Diagram

```mermaid
classDiagram
    class SyncSession {
        +str session_id
        +Path log_file
        +Config config
        +abort_requested: bool
    }

    class RemoteExecutor {
        +connect() Awaitable
        +run_command(cmd: str) Awaitable[Result]
        +stream_command(cmd: str) AsyncIterator[Line]
        +close() Awaitable
    }

    class Orchestrator {
        -List[SyncJob] jobs
        -DiskSpaceMonitorJob monitor
        -RemoteExecutor ssh
        +run_sync() Awaitable
        -handle_critical_error(e: Exception)
        -cleanup()
    }

    class SyncJob {
        <<Protocol>>
        +str name
        +validate() Awaitable
        +execute() Awaitable
        +cleanup() Awaitable
    }

    class BaseJob {
        #RemoteExecutor remote
        #Logger logger
        #report_progress(percent, msg)
    }

    class DiskSpaceMonitorJob {
        +interval: int
        +execute() Awaitable
        +stop()
    }

    class BtrfsSnapshotJob {
        +phase: str
        +execute() Awaitable
    }

    class FeatureJob {
        +execute() Awaitable
    }

    Orchestrator *-- SyncSession
    Orchestrator o-- SyncJob
    Orchestrator o-- RemoteExecutor
    
    SyncJob <|.. BaseJob
    BaseJob <|-- DiskSpaceMonitorJob
    BaseJob <|-- BtrfsSnapshotJob
    BaseJob <|-- FeatureJob
```

### Class Explanations

  * **SyncSession:** A data class (Context Object) passed around to maintain state and configuration.
  * **RemoteExecutor:** Encapsulates the SSH complexity. It ensures that if we switch from `asyncssh` to wrapping `paramiko` later (or standard `ssh` binary via subprocess), the jobs don't change.
  * **Orchestrator:** Contains the high-level logic defined in your prompt (Validate -\> Start Monitor -\> Seq Jobs -\> Stop Monitor).
  * **BaseJob:** An abstract base class that provides helper methods for logging and parsing progress output from shell commands, adhering to DRY.

-----

## 4\. Sequence Diagram: User Aborts (Ctrl+C)

This demonstrates the graceful shutdown. We assume a `SIGINT` handler is registered in the main loop which triggers the `Orchestrator.abort()` method.

```mermaid
sequenceDiagram
    actor User
    participant Main
    participant Orch as Orchestrator
    participant Job as CurrentSyncJob
    participant Remote as RemoteExecutor

    User->>Main: Press Ctrl+C (SIGINT)
    Main->>Orch: cancel_all()
    
    Note over Orch: Set abort_flag = True
    
    par Cancel Current Job
        Orch->>Job: task.cancel() (Asyncio Cancel)
        Job->>Job: Catch CancelledError
        Job->>Remote: send_signal(SIGTERM)
        Remote-->>Job: Signal Sent
        Job->>Job: cleanup() (Best Effort)
        Job-->>Orch: Return/Raise Cancelled
    and Cancel Monitor
        Orch->>DiskMonitor: task.cancel()
        DiskMonitor-->>Orch: Stopped
    end

    Orch->>Orch: Log "Interrupted by User"
    Orch->>Remote: close_connection()
    Orch-->>Main: Exit Code 130
```

-----

## 5\. Sequence Diagram: Job Raises Critical Exception

This covers `FR-019`. A job fails, prompting a halt.

```mermaid
sequenceDiagram
    participant Orch as Orchestrator
    participant Job as FeatureJob
    participant Snap as BtrfsSnapshotJob(Post)
    
    Orch->>Job: execute()
    activate Job
    
    Note over Job: Internal Logic Fails
    Job-->>Orch: raise SyncError("Critical Failure")
    deactivate Job
    
    Note over Orch: Catch SyncError
    
    Orch->>Orch: Log CRITICAL "Job failed"
    
    Orch->>Job: cleanup()
    
    Note over Orch: Skip remaining SyncJobs
    Note over Orch: Skip Post-Sync Snapshots
    
    Note over Orch: Optional: Trigger Rollback\n(Depends on config/interaction)
    
    Orch->>RemoteExecutor: close()
    Orch-->>CLI: Exit Failure
```

-----

## 6\. Sequence Diagram: Remote Command Failure

This details how a shell script failure on the target propagates back to Python.

```mermaid
sequenceDiagram
    participant Job as FeatureJob
    participant SSH as RemoteExecutor
    participant Target as TargetMachine (Bash)

    Job->>SSH: run_command("apt-get update")
    SSH->>Target: Exec "apt-get update"
    
    Target-->>SSH: stderr: "Could not resolve host"
    Target-->>SSH: exit_code: 100
    
    SSH-->>Job: Result(code=100, stderr="...")
    
    Job->>Job: check_return_code(100)
    Note over Job: 100 != 0
    
    Job->>Job: raise SyncError("Remote command failed: ...")
    
    Note right of Job: This triggers the Exception\nflow (Diagram 5)
```

-----

## 7\. Sequence Diagram: Logging Flow

How a log message gets from deep code to the screen and file simultaneously.

```mermaid
sequenceDiagram
    participant Job
    participant SL as StructLog (Logger)
    participant FH as FileHandler
    participant Q as AsyncQueue
    participant UI as TUI Renderer

    Note over Job: Config: File=FULL, CLI=INFO
    
    Job->>SL: log.info("Installing Package", pkg="vim")
    
    par Write to File
        SL->>FH: Write JSON\n{"ts":..., "lvl":"info", "msg":...}
    and Notify UI
        SL->>Q: put(LogEntry(INFO, "Installing Package"))
    end
    
    loop Every 50ms
        UI->>Q: get_nowait()
        Q-->>UI: LogEntry
        UI->>UI: Update Console Widget
    end
```

-----

## 8\. Sequence Diagram: Progress Reporting

Streaming progress (e.g., from `rsync` or a script) to the UI.

```mermaid
sequenceDiagram
    participant Job
    participant SSH as RemoteExecutor
    participant Target as Target Script
    participant Q as UI Event Queue
    
    Job->>SSH: stream_command("./sync_script.sh")
    activate SSH
    
    Target->>SSH: stdout: "PROGRESS: 10: Copying file A"
    SSH-->>Job: yield line
    
    Job->>Job: parse_progress_line(line)
    Note right of Job: Regex match allows separating\nlogs from progress metadata
    
    Job->>Q: put(ProgressEvent(job="Sync", pct=10, desc="Copying file A"))
    
    Target->>SSH: stdout: "PROGRESS: 20: Copying file B"
    SSH-->>Job: yield line
    Job->>Q: put(ProgressEvent(job="Sync", pct=20, desc="Copying file B"))
    
    deactivate SSH
```

-----

## 9\. Sequence Diagram: Disk Space Monitor (Parallel)

The monitor runs *while* other jobs are running.

```mermaid
sequenceDiagram
    participant Orch as Orchestrator
    participant Mon as DiskMonitorJob
    participant Sync as LongRunningSyncJob
    participant SSH as RemoteExecutor

    Orch->>Mon: asyncio.create_task(execute())
    Orch->>Sync: execute()
    
    loop Every 30s
        Mon->>SSH: run_command("df -h /")
        SSH-->>Mon: Output: "95% used"
        
        opt Space < Threshold
            Mon->>Mon: raise DiskSpaceError
            Mon-->>Orch: Propagate Exception
            
            Note over Orch: Handle Exception
            Orch->>Sync: task.cancel()
            Orch->>Orch: Log CRITICAL "Disk Full"
            Orch->>Orch: Abort Sequence
        end
    end
    
    Sync-->>Orch: Finished Success
    Orch->>Mon: task.cancel()
```

-----

## 10\. UI Streaming & State Illustration

The UI component is an **Observer**. It doesn't query jobs; it reacts to a stream of events. This ensures that if the SSH connection hangs, the UI doesn't freeze (it just stops updating progress, but the spinner/timer keeps moving).

```mermaid
graph LR
    subgraph Producers
        J1[SyncJob]
        M1[MonitorJob]
        S1[SSH Layer]
    end

    subgraph "Event Bus (Asyncio Queue)"
        Q[Queue]
    end

    subgraph "Consumer (Rich/Textual)"
        Render[Render Loop]
        
        subgraph Widgets
            PB[Progress Bar]
            Log[Log Window]
            Stat[Status Icons]
        end
    end

    J1 -- "Progress(50%)" --> Q
    J1 -- "Log(INFO)" --> Q
    M1 -- "Log(DEBUG: Disk OK)" --> Q
    S1 -- "ConnectionStatus(Connected)" --> Q

    Q -- "Pop Event" --> Render
    Render -- Updates --> PB
    Render -- Updates --> Log
    Render -- Updates --> Stat
```

-----

## Clarifying Questions

1.  **SSH Library:** The prompt mentions `asyncio`, but ADR-003 mentions monitoring Paramiko (blocking). I have assumed `asyncssh` or a functional equivalent wrapper to satisfy the architecture's non-blocking requirement. Is this acceptable, or must we wrap Paramiko in `run_in_executor`?
2.  **UI Library:** I am assuming `rich` (specifically `rich.live` and `rich.progress`) is sufficient. If you require complex interactive widgets (scrolling back logs *during* sync, clicking buttons), we should upgrade to `textual`.
3.  **Config Injection:** Are we using a DI container (like `dependency-injector`) or simple constructor injection? The design currently assumes simple constructor injection (simpler, fits "Deliberate Simplicity").
