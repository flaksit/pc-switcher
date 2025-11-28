# Architecture (001-foundation)

Scope: foundational orchestration, job contract, logging/progress plumbing, safety jobs (disk space monitor, btrfs snapshots), and SSH-based remote execution for sync. Optimized for reliability first, with explicit interrupt handling and clear job lifecycle.

## Component architecture

```mermaid
flowchart LR
  subgraph Source Machine
    CLI["CLI (pc-switcher sync <target>)"]
    Config["Config Loader & Validator"]
    Orchestrator["Orchestrator (async)"]
    JobRunner["Job Runner (sequential pipeline)"]
    DiskMon["DiskSpaceMonitorJob (parallel)"]
    SnapshotPre["BtrfsSnapshotJob (pre)"]
    SyncJobs["Sync Jobs (from config order)"]
    SnapshotPost["BtrfsSnapshotJob (post)"]
    LogRouter["Log Router (structlog)"]
    UISink["UI Sink (level-filtered)"]
    FileSink["File Sink (~/.local/share/pc-switcher/logs)"]
    UI["Terminal UI (progress + log view)"]
    EventBus["UI/Event Bus (async queues)"]
  end

  subgraph Target Machine
    TargetConn["TargetConnection (persistent SSH)"]
    RemoteExec["Remote commands/scripts\n(line-buffered stdout)"]
  end

  CLI --> Config --> Orchestrator
  Orchestrator --> TargetConn
  Orchestrator --> JobRunner
  JobRunner --> DiskMon
  JobRunner --> SnapshotPre
  JobRunner --> SyncJobs
  JobRunner --> SnapshotPost
  DiskMon -.runtime checks.-> Orchestrator
  JobRunner -.progress/logs.-> LogRouter
  TargetConn <-->|stdout/stderr streams| RemoteExec
  TargetConn -.remote results.-> JobRunner
  LogRouter --> FileSink
  LogRouter --> UISink
  UISink --> EventBus --> UI
  Orchestrator -.health/summary.-> EventBus
```

**Responsibilities & relationships**
- **CLI**: parses commands/flags, resolves target host, starts `Orchestrator`.
- **Config Loader**: reads `~/.config/pc-switcher/config.yaml`, applies defaults, validates against job-declared schemas.
- **Orchestrator**: owns session lifecycle, installs SIGINT handler, holds `TargetConnection`, wires log/progress buses, builds ordered job list (disk monitor parallel, snapshots fixed, sync jobs from config).
- **Job Runner**: validates all jobs first, then executes flow: start `DiskSpaceMonitorJob` (background), run pre-snapshot, sync jobs sequentially, post-snapshot, stop disk monitor. Handles abort/failure/cleanup.
- **TargetConnection**: single multiplexed SSH channel per ADR-002; runs remote commands, streams stdout/stderr line-buffered to log/progress parser, supports cancellation/timeout.
- **Log Router**: structlog core; routes structured events from orchestrator/jobs/remote streams to file sink (JSON) and UI sink (level-filtered human-readable).
- **Event Bus**: asyncio queues delivering logs/progress/health to UI renderer without blocking job execution.
- **DiskSpaceMonitorJob & BtrfsSnapshotJob**: infrastructure jobs (non-disableable, fixed order). Sync jobs are user-configurable and strictly sequential.

## Class model

```mermaid
classDiagram
  class SessionContext {
    +session_id: str
    +config: Config
    +logger: structlog.BoundLogger
    +progress_bus: ProgressBus
    +target: TargetConnection
    +grace_timeout_s: int
  }

  class Job {
    <<interface>>
    +name: str
    +required: bool
    +get_config_schema() dict
    +validate(ctx: SessionContext) -> list[ValidationError]
    +execute(ctx: SessionContext) -> None
    +request_termination(reason: str) -> None
  }

  class InfrastructureJob {
    <<abstract>>
    +phase: str
  }
  Job <|-- InfrastructureJob

  class BtrfsSnapshotJob {
    +phase: "pre"|"post"
    +snapshot_specs: list[str]
  }
  InfrastructureJob <|-- BtrfsSnapshotJob

  class DiskSpaceMonitorJob {
    +check_interval_s: int
    +minimum_free: Threshold
  }
  InfrastructureJob <|-- DiskSpaceMonitorJob

  class SyncJob {
    <<abstract>>
  }
  Job <|-- SyncJob
  SyncJob <|-- DummySuccessJob
  SyncJob <|-- DummyFailJob
  %% other feature jobs will subclass SyncJob

  class Orchestrator {
    +run(target: str)
    +handle_sigint()
    +abort(reason)
  }

  class JobRunner {
    +validate_all(jobs)
    +run_sequence(jobs)
    +terminate_active()
  }

  Orchestrator --> SessionContext
  Orchestrator --> JobRunner
  JobRunner --> Job

  class TargetConnection {
    +run(cmd, *, timeout, env) : RemoteProcess
    +close()
  }

  class RemoteProcess {
    +stdout_stream(): AsyncIterator[str]
    +stderr_stream(): AsyncIterator[str]
    +wait()
    +cancel()
  }

  Job --> TargetConnection : uses
  Job --> SessionContext : reads config/logger/progress

  class LogRouter {
    +emit(event)
  }
  class FileLogSink
  class UiLogSink
  LogRouter --> FileLogSink
  LogRouter --> UiLogSink
  Job --> LogRouter : emits logs
  Orchestrator --> LogRouter

  class ProgressBus {
    +publish(update)
    +subscribe()
  }
  Job --> ProgressBus
  Orchestrator --> ProgressBus
  class TerminalUI {
    +render_loop()
  }
  TerminalUI --> ProgressBus
  TerminalUI --> UiLogSink
```

**Notes**
- `Job` interface is the contract from the spec; `required` prevents disablement.
- `InfrastructureJob` captures hardcoded jobs; `SyncJob` covers user-configurable jobs.
- `SessionContext` is passed to every job for dependency injection (config, logger, progress bus, target connection, timeouts).
- `TargetConnection` / `RemoteProcess` provide cancellation and streaming control required for remote command management.
- `LogRouter` centralizes the six log levels and dual sinks; UI sink filters per `log_cli_level`.

## Execution flow (high level)
1. Load config, build `SessionContext`, set up structlog sinks and UI/event bus.
2. Validate all jobs (disk monitor, snapshots, sync jobs). Abort on any validation error before state changes.
3. Start disk-space monitor as background task (runs until orchestrator shutdown).
4. Run pre-snapshot job (phase="pre").
5. Run sync jobs sequentially in declared config order.
6. Run post-snapshot job (phase="post").
7. Cancel disk monitor task, flush logs/progress, emit summary, close SSH.

## Sequence diagrams

### User aborts (Ctrl+C)
```mermaid
sequenceDiagram
  participant User
  participant SigHandler as SIGINT Handler
  participant Orchestrator
  participant JobRunner
  participant Job
  participant Remote as RemoteProcess
  participant UI

  User->>SigHandler: Ctrl+C
  SigHandler->>Orchestrator: notify_interrupt()
  Orchestrator->>UI: log WARNING "Sync interrupted by user"
  Orchestrator->>JobRunner: request_termination(grace=5s)
  JobRunner->>Job: request_termination("SIGINT")
  Job->>Remote: cancel()
  Remote-->>Job: cancelled / timeout?
  Job-->>JobRunner: cleanup complete (or timeout)
  JobRunner-->>Orchestrator: terminated
  Orchestrator->>Remote: force close if needed
  Orchestrator->>UI: exit status 130
```

**Explanation**: SIGINT is trapped; orchestrator signals current job for graceful stop with 5s budget, then force-closes remote process/SSH if the job stalls. UI reflects interruption and exit code 130.

### Job raises a critical exception
```mermaid
sequenceDiagram
  participant Job
  participant JobRunner
  participant Orchestrator
  participant UI
  participant DiskMon as DiskSpaceMonitorJob

  Job->>JobRunner: raise SyncError("…")
  JobRunner->>Orchestrator: propagate failure
  Orchestrator->>UI: log CRITICAL, mark job failed
  Orchestrator->>Job: request_termination("error")
  Orchestrator->>DiskMon: cancel background monitor
  Orchestrator->>UI: render summary (failed job, aborted pipeline)
  Orchestrator->>Orchestrator: close SSH, stop loop
```

**Explanation**: Any `SyncError` short-circuits the pipeline; queued jobs do not start. Disk monitor is stopped, summary emitted, SSH closed cleanly.

### Remote command fails on target
```mermaid
sequenceDiagram
  participant Job
  participant Target as TargetConnection
  participant Remote as RemoteProcess
  participant Parser as Stream Parser
  participant LogRouter
  participant JobRunner

  Job->>Target: run("target-command", timeout=…)
  Target-->>Job: RemoteProcess handle
  Remote-->>Parser: stderr line "ERROR: disk full"
  Parser->>LogRouter: emit ERROR (job=…, host=target)
  Remote-->>Job: exit code != 0
  Job->>JobRunner: raise SyncError("target-command failed")
  JobRunner->>...: abort pipeline (per previous diagram)
```

**Explanation**: Remote stdout/stderr are streamed line-by-line; parser tags host/job and feeds LogRouter. Non-zero exit causes the job to raise `SyncError`, triggering orchestrator abort.

### Job logs a message
```mermaid
sequenceDiagram
  participant Job
  participant LogRouter
  participant FileSink
  participant UiSink
  participant UI

  Job->>LogRouter: emit(level=INFO, event="Starting copy", job=J)
  LogRouter->>FileSink: write JSON line (INFO)
  LogRouter->>UiSink: apply CLI level filter
  UiSink-->>UI: push log line for render
```

**Explanation**: Single emission fan-outs to both sinks. File sink always respects configured file level; UI sink filters via `log_cli_level`.

### Job logs progress
```mermaid
sequenceDiagram
  participant Job
  participant ProgressBus
  participant LogRouter
  participant UI

  Job->>ProgressBus: publish{job=J, percent=45, item="image nginx:latest"}
  ProgressBus-->>UI: update progress view
  ProgressBus->>LogRouter: emit FULL "progress" entry
  LogRouter->>...: sinks as usual
```

**Explanation**: Progress updates are UI-first, but also logged at FULL for audit, satisfying logging requirements.

### DiskSpaceMonitor detects low space
```mermaid
sequenceDiagram
  participant DiskMon as DiskSpaceMonitorJob
  participant Target as TargetConnection
  participant LogRouter
  participant Orchestrator
  participant JobRunner
  participant UI

  DiskMon->>Target: check_free_space()
  Target-->>DiskMon: free=12% (< runtime_minimum)
  DiskMon->>LogRouter: emit CRITICAL "Target free space 12% < 15%"
  DiskMon->>Orchestrator: signal_runtime_violation()
  Orchestrator->>JobRunner: request_termination("disk low")
  JobRunner->>CurrentJob: request_termination()
  Orchestrator->>UI: show abort reason
```

**Explanation**: Monitor runs continuously; on threshold breach it logs CRITICAL and triggers orchestrator-driven abort to prevent further writes.

### Streaming output fan-in to UI (parallel sources)
```mermaid
flowchart LR
  subgraph Producers
    O["Orchestrator events\n(state changes, health)"]
    J["Active Job\n(logs + progress)"]
    D["DiskSpaceMonitor\n(runtime alerts)"]
    H["TargetConnection health\n(keepalive, reconnect)"]
  end
  subgraph Transport
    Q["Async Event Bus\n(log queue + progress queue)"]
  end
  subgraph Consumer
    U["Terminal UI renderer\n(non-blocking)"]
  end

  O --> Q
  J --> Q
  D --> Q
  H --> Q
  Q --> U
```

**Explanation**: Multiple async producers push structured events into shared queues; the UI renderer consumes and paints in a single loop, ensuring streaming output remains responsive even with parallel sources.

