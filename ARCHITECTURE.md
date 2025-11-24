# PC-switcher Architecture

This document describes the high-level architecture of PC-switcher, including module interfaces, orchestrator workflow, and state transitions.

## System Overview

PC-switcher uses a modular architecture where each synchronization concern (snapshots, user data, packages, etc.) is implemented as a separate module. The orchestrator coordinates module execution and manages the sync lifecycle.

```mermaid
graph TB
    CLI[CLI - main.py] --> Config[Configuration]
    CLI --> Session[Session Manager]
    CLI --> Orchestrator[Orchestrator]

    Orchestrator --> ModuleLoader[Module Loader]
    Orchestrator --> DiskMonitor[Disk Monitor]
    Orchestrator --> SignalHandler[Signal Handler]

    ModuleLoader --> BtrfsModule[Btrfs Snapshots]
    ModuleLoader --> FutureModules[Future Modules...]

    BtrfsModule --> RemoteExec[Remote Executor]
    FutureModules --> RemoteExec

    RemoteExec --> SSH[SSH Connection]
    SSH --> TargetMachine[Target Machine]

    Session --> LockFile[Lock File]
    Config --> YAML[config.yaml]
```

## Core Components

### Module Interface

All sync modules implement the `SyncModule` abstract base class defined in `src/pcswitcher/core/module.py`:

```mermaid
classDiagram
    class SyncModule {
        <<abstract>>
        +config: dict
        +remote: RemoteExecutor
        +name: str*
        +required: bool*
        +get_config_schema() dict*
        +validate() list~str~*
        +pre_sync() None*
        +sync() None*
        +post_sync() None*
        +abort(timeout: float) None*
        +log(level, message, **context)
        +emit_progress(percentage, item, eta)
    }

    class BtrfsSnapshotsModule {
        +name: "btrfs-snapshots"
        +required: true
        +rollback_to_presync(session_id)
        +cleanup_old_snapshots(days, keep)
    }

    class DummySuccessModule {
        +name: "dummy-success"
        +required: false
    }

    class RemoteExecutor {
        <<abstract>>
        +run(command, sudo, timeout)*
        +send_file_to_target(local, remote)*
        +get_hostname()*
    }

    class SSHRemoteExecutor {
        +connection: TargetConnection
    }

    SyncModule <|-- BtrfsSnapshotsModule
    SyncModule <|-- DummySuccessModule
    RemoteExecutor <|-- SSHRemoteExecutor
    SyncModule --> RemoteExecutor
```

### Module Lifecycle

Each module progresses through a defined lifecycle managed by the orchestrator:

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant M as Module
    participant R as RemoteExecutor

    Note over O,M: VALIDATING Phase
    O->>M: validate()
    M-->>O: list[str] (errors)

    Note over O,M: EXECUTING Phase - Pre-sync
    O->>M: pre_sync()
    M->>R: run("btrfs snapshot...", sudo=True)
    R-->>M: CompletedProcess

    Note over O,M: EXECUTING Phase - Sync
    O->>M: sync()
    M->>M: emit_progress(0.5, "Syncing files")
    M->>R: send_file_to_target(...)

    Note over O,M: EXECUTING Phase - Post-sync
    O->>M: post_sync()
    M->>R: run("verify...", sudo=True)

    Note over O,M: On Error/Interrupt
    O->>M: abort(timeout=5.0)
```

## Orchestrator Workflow

The orchestrator implements a state machine that coordinates the entire sync operation:

```mermaid
stateDiagram-v2
    [*] --> INITIALIZING

    INITIALIZING: Verify btrfs
    INITIALIZING: Load modules
    INITIALIZING: Start disk monitoring

    VALIDATING: All modules validate()
    VALIDATING: No state changes yet

    EXECUTING: pre_sync()
    EXECUTING: sync()
    EXECUTING: post_sync()

    CLEANUP: abort() on current module
    CLEANUP: Best-effort cleanup

    INITIALIZING --> VALIDATING : Success
    INITIALIZING --> FAILED : Error

    VALIDATING --> EXECUTING : All valid
    VALIDATING --> FAILED : Validation error

    EXECUTING --> COMPLETED : All modules succeed
    EXECUTING --> CLEANUP : Module error
    EXECUTING --> CLEANUP : User interrupt

    CLEANUP --> FAILED : Error occurred
    CLEANUP --> ABORTED : User interrupt

    COMPLETED --> [*]
    ABORTED --> [*]
    FAILED --> [*] : Offer rollback
```

### State Descriptions

| State | Description | Entry Conditions |
|-------|-------------|------------------|
| INITIALIZING | Setup phase: verify prerequisites, load modules, start monitoring | CLI invokes sync command |
| VALIDATING | All modules run validate() without making changes | Initialization complete |
| EXECUTING | Execute modules: pre_sync -> sync -> post_sync | All validations pass |
| CLEANUP | Best-effort cleanup via abort() on current module | Error or interrupt |
| COMPLETED | All modules succeeded without errors | Execution finished |
| ABORTED | User interrupt (SIGINT/SIGTERM) | User pressed Ctrl+C |
| FAILED | Critical error occurred | Module raised SyncError |

## Configuration Flow

Configuration is loaded and validated before module instantiation:

```mermaid
flowchart TD
    A[Load YAML file] --> B{Valid YAML?}
    B -- No --> E1[ConfigError: Parse error]
    B -- Yes --> C[Validate structure]
    C --> D{sync_modules present?}
    D -- No --> E2[ConfigError: Missing required field]
    D -- Yes --> F[Apply defaults]
    F --> G{btrfs_snapshots first and enabled?}
    G -- No --> E3[ConfigError: Required module constraint]
    G -- Yes --> H[Parse log levels]
    H --> I[Create Configuration object]
    I --> J[For each module]
    J --> K[Get module's config schema]
    K --> L[Validate module config against schema]
    L --> M{Valid?}
    M -- No --> E4[ConfigError: Invalid module config]
    M -- Yes --> N[Instantiate module]
    N --> O[Inject log and emit_progress]
```

## Remote Execution Architecture

SSH-based remote execution uses a layered approach:

```mermaid
graph LR
    subgraph "Source Machine"
        Module[Sync Module]
        SSHExec[SSHRemoteExecutor]
        Target[TargetConnection]
        Fabric[Fabric Library]
    end

    subgraph "Target Machine"
        SSHD[SSH Server]
        Shell[Bash Shell]
        Commands[System Commands]
    end

    Module -->|RemoteExecutor interface| SSHExec
    SSHExec -->|Delegates to| Target
    Target -->|Uses| Fabric
    Fabric -->|SSH over| SSHD
    SSHD -->|Executes| Shell
    Shell -->|Runs| Commands
```

### Connection Resilience

```mermaid
flowchart TD
    A[Execute command] --> B{Connection active?}
    B -- No --> E1[ConnectionError: Not connected]
    B -- Yes --> C[Try Fabric run/sudo]
    C --> D{Success?}
    D -- Yes --> F[Return CompletedProcess]
    D -- No --> G{Connection lost?}
    G -- No --> H[Command failed normally]
    H --> F
    G -- Yes --> I[Attempt reconnect]
    I --> J{Reconnect success?}
    J -- Yes --> K[Retry command]
    K --> F
    J -- No --> E2[ConnectionError: Reconnect failed]
```

## Session Management

Each sync operation is tracked by a `SyncSession` object:

```mermaid
classDiagram
    class SyncSession {
        +id: str (8-char hex)
        +timestamp: datetime
        +source_hostname: str
        +target_hostname: str
        +enabled_modules: list~str~
        +state: SessionState
        +module_results: dict~str, ModuleResult~
        +has_errors: bool
        +abort_requested: bool
        +lock_path: Path
        +set_state(new_state)
        +is_terminal_state() bool
    }

    class SessionState {
        <<enumeration>>
        INITIALIZING
        VALIDATING
        EXECUTING
        CLEANUP
        COMPLETED
        ABORTED
        FAILED
    }

    class ModuleResult {
        <<enumeration>>
        SUCCESS
        SKIPPED
        FAILED
    }

    SyncSession --> SessionState
    SyncSession --> ModuleResult
```

## Logging Architecture

Structured logging uses structlog with dual output:

```mermaid
flowchart LR
    subgraph "Module"
        M[module.log]
    end

    subgraph "Orchestrator"
        Inject[Injected log method]
        Structlog[structlog processor]
    end

    subgraph "Outputs"
        File[JSON file log]
        Console[Console output]
    end

    subgraph "Error Tracking"
        Track[ERROR/CRITICAL tracker]
        Session[session.has_errors]
    end

    M --> Inject
    Inject --> Structlog
    Structlog --> File
    Structlog --> Console
    Structlog --> Track
    Track --> Session
```

### Log Level Flow

```mermaid
flowchart TD
    A[Log event] --> B[Add context: timestamp, hostname, level]
    B --> C{Level >= file_level?}
    C -- Yes --> D[Write to JSON file]
    C -- No --> E[Skip file output]
    B --> F{Level >= cli_level?}
    F -- Yes --> G[Display in terminal]
    F -- No --> H[Skip terminal output]
    B --> I{Level is ERROR or CRITICAL?}
    I -- Yes --> J[Set session.has_errors = True]
```

## Error Handling Strategy

PC-switcher uses exception-based error propagation with three categories:

```mermaid
flowchart TD
    subgraph "Module Code"
        A1[Recoverable error] --> B1[Log ERROR and continue]
        A2[Critical failure] --> B2[Raise SyncError]
    end

    subgraph "Orchestrator"
        B1 --> C1[session.has_errors = true]
        B2 --> C2[Log CRITICAL]
        C2 --> D[Call abort on module]
        D --> E[Set session.abort_requested]
        E --> F[Stop execution]
    end

    subgraph "Final State"
        C1 --> G{All modules done?}
        G -- Yes --> H[State = FAILED]
        F --> H
    end
```

## Snapshot Safety Model

Btrfs snapshots provide data safety through copy-on-write:

```mermaid
sequenceDiagram
    participant S as Sync Start
    participant Pre as Pre-sync Snapshot
    participant Op as Sync Operations
    participant Post as Post-sync Snapshot
    participant R as Rollback (if failed)

    S->>Pre: Create read-only snapshot
    Note over Pre: Point-in-time backup
    Pre->>Op: Sync operations begin
    Op->>Op: Files modified, packages installed
    Op->>Post: Create read-only snapshot
    Note over Post: Capture final state

    alt Sync Failed
        Op->>R: Rollback to Pre-sync
        R->>R: Delete current subvolume
        R->>R: Create from pre-sync snapshot
        Note over R: System restored to known good state
    end
```

## Module Execution Order

Modules execute sequentially based on configuration order:

```mermaid
gantt
    title Module Execution Timeline
    dateFormat HH:mm
    axisFormat %H:%M

    section btrfs-snapshots
    validate       :v1, 00:00, 1m
    pre_sync       :p1, after v1, 2m
    sync           :s1, after p1, 1m
    post_sync      :o1, after s1, 2m

    section future-module-A
    validate       :v2, 00:00, 1m
    pre_sync       :p2, after o1, 3m
    sync           :s2, after p2, 10m
    post_sync      :o2, after s2, 2m

    section future-module-B
    validate       :v3, 00:00, 1m
    pre_sync       :p3, after o2, 1m
    sync           :s3, after p3, 5m
    post_sync      :o3, after s3, 1m
```

Key points:
- All `validate()` methods run during VALIDATING phase (no interleaving)
- Module lifecycle (pre_sync -> sync -> post_sync) runs completely before next module starts
- On failure, only current module's `abort()` is called
- Order defined in config file determines execution sequence

## Performance Considerations

### Startup Timing

The orchestrator measures startup performance (T125):

```plain
CLI invocation -> Config load -> Session create -> Orchestrator init -> run()
                   ~10ms           ~5ms              ~1ms              logged
```

### Minimal Disk Writes (T126)

- **Logging**: Buffered output, JSON file appends
- **Snapshots**: Btrfs COW means no data copying, only metadata
- **Progress**: In-memory tracking, UI updates only
- **State**: Session state tracked in memory, not persisted

### Memory Efficiency

- Modules execute sequentially (not parallel) to limit memory usage
- Progress reporting uses callbacks (no buffering large data)
- File transfers stream data (no in-memory storage)

## Future Extension Points

### Adding New Modules

1. Implement `SyncModule` interface
2. Define JSON Schema for configuration
3. Register in module loader (convention-based discovery)
4. Add to config schema documentation

### Adding New CLI Commands

1. Add Typer command in `cli/main.py`
2. Use existing orchestrator/session infrastructure
3. Follow established error handling patterns

### Adding New Remote Operations

1. Extend `RemoteExecutor` interface if needed
2. Implement in `SSHRemoteExecutor`
3. Ensure reconnection resilience
4. Handle timeout scenarios
