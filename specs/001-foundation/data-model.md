# Data Model: Foundation Infrastructure Complete

**Feature**: Foundation Infrastructure Complete
**Date**: 2025-11-15
**Phase**: Phase 1 - Design & Contracts

## Overview

This document defines the key entities, their fields, relationships, and state transitions for the foundation infrastructure.

## Entities

### 1. Module (Abstract Base Class)

Base class for all pc-switcher modules (sync modules and infrastructure modules).

**Fields**:
- `name: str` - Unique module identifier (e.g., "btrfs-snapshots", "dummy-success", "packages")
- `required: bool` - Whether module can be disabled via config (False for optional modules)
- `config: dict[str, Any]` - Module-specific configuration (validated against schema)
- `remote: RemoteExecutor` - Interface for executing commands on target machine (injected by orchestrator)

**Methods** (all abstract, must be implemented by subclasses):
- `get_config_schema() -> dict[str, Any]` - Returns JSON schema for module config validation
- `validate() -> list[str]` - Pre-sync validation; returns list of error messages (empty if valid)
- `execute() -> None` - Execute the module's operation; raise exception on critical failure
- `abort(timeout: float) -> None` - Stop running processes, free resources (best-effort, limited by timeout)

**Injected Methods** (provided by orchestrator, not implemented by module):
- `emit_progress(percentage: float | None, item: str, eta: timedelta | None) -> None` - Report progress to orchestrator
- `log(level: LogLevel, message: str, **context) -> None` - Log message with structured context

**Subclasses**:
- `SyncModule`: User-configurable sync modules (packages, docker, VMs, k3s, user data)
- Infrastructure modules inherit directly from `Module` (e.g., `BtrfsSnapshotModule`)

**Validation Rules**:
- `name` must be unique across all registered modules
- SyncModules execute sequentially in the order defined in config file
- Infrastructure modules are hardcoded by orchestrator:
  - `BtrfsSnapshotModule(phase="pre")` executes before all SyncModules (sequential)
  - `BtrfsSnapshotModule(phase="post")` executes after all SyncModules (sequential)
  - `DiskMonitorModule` runs in parallel throughout entire sync operation

**State Transitions**: None (stateless, only method execution sequence matters)

**Error Handling**:
- Modules raise exceptions (e.g., `SyncError`) for unrecoverable failures
- Orchestrator catches exceptions, logs them as CRITICAL, and initiates cleanup
- Modules do NOT log at CRITICAL level themselves

**Relationships**:
- Many-to-one with `Orchestrator` (orchestrator manages all modules)
- Many-to-one with `SyncSession` (session tracks module execution)
- One-to-one with `RemoteExecutor` (module uses to communicate with target)
- Zero-to-many with `ProgressUpdate` (module emits progress updates)
- Zero-to-many with `LogEntry` (module emits log entries via injected log method)

---

### 2. SyncSession

Represents a single sync operation from source to target.

**Purpose**: Tracks the state and progress of a sync operation. Owned and managed by the Orchestrator.

**Fields**:
- `id: str` - Unique session identifier (8-char hex from UUID)
- `timestamp: datetime` - Session start time (UTC, ISO8601)
- `source_hostname: str` - Source machine hostname
- `target_hostname: str` - Target machine hostname
- `enabled_modules: list[str]` - Module names enabled for this session (from config, in execution order)
- `state: SessionState` - Current session state (enum)
- `module_results: dict[str, ModuleResult]` - Execution results per module
- `has_errors: bool` - Whether any ERROR-level logs were emitted (determines COMPLETED vs FAILED)
- `abort_requested: bool` - Whether abort has been signaled (exception or Ctrl+C)
- `lock_path: Path` - Lockfile path (default: `$XDG_RUNTIME_DIR/pc-switcher/pc-switcher.lock`)

**Enums**:
- `SessionState`: `INITIALIZING`, `VALIDATING`, `EXECUTING`, `CLEANUP`, `COMPLETED`, `ABORTED`, `FAILED`
- `ModuleResult`: `SUCCESS`, `SKIPPED`, `FAILED`

**Validation Rules**:
- `source_hostname` and `target_hostname` must be resolvable or valid SSH config aliases
- `enabled_modules` must be non-empty list in execution order
- `btrfs_snapshots` must be first in `enabled_modules` and cannot be disabled
- Only one active session allowed (enforced via lock file with stale detection)

**State Transitions**:
```text
INITIALIZING → VALIDATING → EXECUTING ─────────────────→ CLEANUP → COMPLETED
             ↓             ↓           ↘                     ↓
             ↓             ↓            Exception/Ctrl+C     ↓
             ↓             ↓                 ↓               ↓
             └─────────────┴─────────────→ CLEANUP ───────→ ABORTED (if Ctrl+C)
                                                       ↘
                                                        → FAILED (if exception/ERROR logs)
```

**State Descriptions**:
- `INITIALIZING`: Loading config, checking lock, establishing SSH connection, checking/installing target version
- `VALIDATING`: Running all module `validate()` methods (including disk_monitor); abort if any validation errors
- `EXECUTING`: Start disk monitor in parallel, then run modules sequentially (pre-snapshots, sync modules, post-snapshots)
- `CLEANUP`: Stop disk monitor, call `abort(timeout)` on currently-running module (if any); triggered by exception or Ctrl+C
- `COMPLETED`: All modules succeeded, no ERROR logs emitted
- `ABORTED`: User requested abort (Ctrl+C)
- `FAILED`: Module raised exception (including DiskSpaceError from monitor), or ERROR logs were emitted during execution

**State Transition Rules**:
- Always pass through CLEANUP before reaching ABORTED or FAILED
- Exception: Can go EXECUTING → COMPLETED if all modules finish successfully
- From CLEANUP: → ABORTED if user requested abort (Ctrl+C)
- From CLEANUP: → FAILED if module raised exception or ERROR logs were emitted

**Relationships**:
- Owned by `Orchestrator` (orchestrator creates and manages session)
- One-to-many with `SyncModule` (session executes multiple modules)
- One-to-many with `LogEntry` (session generates log entries)
- One-to-many with `ProgressUpdate` (session receives progress updates from modules)
- One-to-one with `TargetConnection` (session manages SSH connection)
- Zero-to-many with `Snapshot` (session may create snapshots)

---

### 3. Snapshot

Represents a btrfs snapshot created during sync.

**Fields**:
- `subvolume: str` - Flat subvolume name from `btrfs subvolume list /` (e.g., "@", "@home", "@root")
- `snapshot_path: str` - Full path to snapshot (e.g., "/.snapshots/@-presync-20251115T120000Z-abc123")
- `timestamp: datetime` - Snapshot creation time (UTC, ISO8601)
- `session_id: str` - Associated sync session ID
- `type: SnapshotType` - Pre-sync or post-sync (enum)
- `hostname: str` - Actual hostname where snapshot was created (e.g., "laptop-home", "workstation")
- `readonly: bool` - Whether snapshot is read-only (always True for our use case)

**Enums**:
- `SnapshotType`: `PRE_SYNC`, `POST_SYNC`

**Validation Rules**:
- `subvolume` must be a flat name (not a path) as shown in `btrfs subvolume list /` output
- `subvolume` must exist in the top-level of the btrfs filesystem on the machine where snapshot is created
- `snapshot_path` must follow naming pattern: `{snapshot_dir}/{subvolume}-{presync|postsync}-{timestamp}-{session_id}`
- Default `snapshot_dir` is `/.snapshots` (configurable)
- `timestamp` must be ISO8601 format with UTC timezone
- `session_id` must be 8-char hex string
- `hostname` must be actual hostname (not "source" or "target"), used for logging and tracking

**Examples**:
- Subvolume: `"@"` (root filesystem, mounted at `/`)
- Subvolume: `"@home"` (home directory, mounted at `/home`)
- Subvolume: `"@root"` (root user home, mounted at `/root`)
- Snapshot path: `"/.snapshots/@-presync-20251115T120000Z-abc12345"`
- Snapshot path: `"/.snapshots/@home-postsync-20251115T120500Z-abc12345"`

**State Transitions**: None (immutable once created; deletion is external operation)

**Relationships**:
- Many-to-one with `SyncSession` (session creates multiple snapshots)
- Created by `BtrfsSnapshotsModule` (implementation detail)

---

### 4. LogEntry

Represents a logged event with structured context.

**Fields**:
- `timestamp: datetime` - Event time (UTC, ISO8601)
- `level: LogLevel` - Severity level (enum)
- `module: str` - Module name that emitted the log (or "core" for orchestrator)
- `hostname: str` - Actual hostname where event occurred (e.g., "laptop-home", "workstation")
- `message: str` - Log message (human-readable)
- `context: dict[str, Any]` - Structured context data (e.g., file paths, error codes)
- `session_id: str` - Associated sync session ID

**Enums**:
- `LogLevel`: `DEBUG` (10), `FULL` (15), `INFO` (20), `WARNING` (30), `ERROR` (40), `CRITICAL` (50)

**Validation Rules**:
- `timestamp` must be ISO8601 with UTC timezone
- `level` must be one of the six defined levels
- `module` should reference a registered module or "core" for orchestrator
- `message` must be non-empty
- `hostname` must be actual hostname (not "source" or "target")
- If `level == ERROR`, orchestrator sets `session.has_errors = True` (final state will be FAILED)
- CRITICAL logs only emitted by orchestrator when catching module exceptions

**State Transitions**: None (immutable once created)

**Relationships**:
- Many-to-one with `SyncSession` (session generates log entries)
- Emitted by `SyncModule` (via injected log method) or `Orchestrator` directly
- Consumed by `FileLogger` and `TerminalUI`

---

### 5. ProgressUpdate

Represents module progress for display and logging.

**Fields**:
- `module: str` - Module name reporting progress
- `percentage: float | None` - Progress as fraction (0.0-1.0) of **total module work**, or None if unknown
- `current_item: str` - Description of current operation (e.g., "Copying /home/user/file.txt")
- `eta: timedelta | None` - Estimated time to completion (optional)
- `timestamp: datetime` - Update time (UTC)
- `session_id: str` - Associated sync session ID

**Validation Rules**:
- `percentage` must be in range [0.0, 1.0] if not None
- `percentage` represents progress of **all module work** (validation + execution), not just current subtask
- `current_item` should be concise (<100 chars for terminal display)
- `eta` can be None if module doesn't estimate completion time

**Important**: The percentage field represents the overall progress of the entire module's operation, not just the current item or subtask. Modules structure their work internally as needed and report overall progress.

**State Transitions**: None (ephemeral, only current update matters)

**Relationships**:
- Many-to-one with `SyncModule` (module emits progress updates via injected method)
- Many-to-one with `SyncSession` (session receives updates)
- Consumed by `TerminalUI` for display
- Logged at FULL level by orchestrator

---

### 6. Configuration

Represents parsed and validated configuration.

**Fields**:
- `log_file_level: LogLevel` - Minimum level for file logging
- `log_cli_level: LogLevel` - Minimum level for terminal display
- `sync_modules: dict[str, bool]` - Module enable/disable flags
- `module_configs: dict[str, dict[str, Any]]` - Per-module configuration sections
- `disk: dict` - Disk space monitoring configuration with keys:
  - `min_free: int | float` - Minimum free disk space (bytes or percentage)
  - `reserve_minimum: int | float` - Reserved space during sync
  - `check_interval: int` - Seconds between disk space checks
- `config_path: Path` - Path to loaded config file

**Validation Rules**:
- `log_file_level` and `log_cli_level` must be valid LogLevel enum values
- `sync_modules` keys must reference registered module names
- Required modules (btrfs-snapshots) cannot be disabled (sync_modules value ignored)
- Each entry in `module_configs` must validate against module's `get_config_schema()`
- `disk.min_free`: if float, must be in (0.0, 1.0) for percentage; if int, must be positive bytes
- `disk.reserve_minimum`: if float, must be in (0.0, 1.0) for percentage; if int, must be positive bytes
- `disk.check_interval` must be positive integer (seconds)
- Subvolume names in `btrfs_snapshots.subvolumes` must be flat names (e.g., "@", "@home") not paths

**State Transitions**: None (immutable once loaded; reload requires new session)

**Relationships**:
- One-to-many with `SyncModule` (provides config to each module)
- One-to-one with `SyncSession` (session uses one config)

---

### 7. TargetConnection

Represents SSH connection to target machine.

**Fields**:
- `hostname: str` - Target hostname or SSH config alias
- `connection: fabric.Connection` - Fabric connection instance
- `pc_switcher_version: str | None` - Detected pc-switcher version on target (None if not installed)
- `control_path: str` - SSH ControlMaster socket path for connection reuse
- `connected: bool` - Connection state

**Methods**:
- `connect() -> None` - Establish SSH connection
- `disconnect() -> None` - Close SSH connection gracefully
- `run(command: str, sudo: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess` - Execute command on target
- `check_version() -> str | None` - Detect pc-switcher version on target
- `install_version(version: str, installer_path: Path) -> None` - Install/upgrade pc-switcher
- `send_file_to_target(local: Path, remote: Path) -> None` - Upload file to target
- `terminate_processes() -> None` - Send SIGTERM to target-side processes

**Result Type**: `subprocess.CompletedProcess`
- Contains: `returncode`, `stdout`, `stderr`, `args`
- Access: `result.returncode`, `result.stdout`, etc.
- Check success: `result.returncode == 0`

**Notes**:
- `send_file_to_target()` currently does not set permissions/ownership (future feature)
- Future enhancement: streaming stdout/stderr line-by-line with callback
- Future enhancement: run remote Python tasks with progress/logging streaming

**Design Decision: Synchronous Methods with Callbacks**:
- Module lifecycle methods (`validate()`, `pre_sync()`, `sync()`, `post_sync()`, `abort()`) are **synchronous** (not `async`)
- Progress and log reporting use **injected callback methods** (`emit_progress()`, `log()`)
- Background operations within modules can use **threading** if needed (module responsibility)
- **Rationale**: Synchronous methods with callbacks are simpler to implement and test than async/await patterns. This approach is sufficient for the current requirements and avoids the complexity of async context management, event loops, and async-compatible libraries. If continuous streaming becomes a requirement in the future, individual modules can use threading internally while maintaining the synchronous interface contract.

**Validation Rules**:
- `hostname` must be resolvable or valid SSH config alias
- Connection must succeed before any operations (raises exception if SSH fails)
- `control_path` should use ControlMaster to enable multiplexing

**State Transitions**:
```text
DISCONNECTED → CONNECTING → CONNECTED → DISCONNECTING → DISCONNECTED
               ↓                        ↓
               └────────→ ERROR ←───────┘
```

**Relationships**:
- One-to-one with `SyncSession` (session manages one connection)
- Wrapped by `RemoteExecutor` for module access

---

### 8. RemoteExecutor

Interface provided to modules for executing commands on target machine.

**Purpose**: Abstracts SSH details from modules, enabling easier testing and cleaner module code.

**Fields**:
- `connection: TargetConnection` - Underlying SSH connection (private)

**Methods** (injected into SyncModule):
- `run(command: str, sudo: bool = False, timeout: float | None = None) -> subprocess.CompletedProcess` - Execute command on target
- `send_file_to_target(local: Path, remote: Path) -> None` - Upload file to target
- `get_hostname() -> str` - Get target hostname

**Usage in Modules**:
```python
# Module receives RemoteExecutor in constructor
def __init__(self, config: dict[str, Any], remote: RemoteExecutor):
    self.config = config
    self.remote = remote

# Module uses it to communicate with target
def sync(self):
    result = self.remote.run("btrfs subvolume list /", sudo=True)
    if result.returncode != 0:
        raise SyncError(f"Failed to list subvolumes: {result.stderr}")

    self.remote.send_file_to_target(Path("local.txt"), Path("/tmp/remote.txt"))
```

**Relationships**:
- One-to-one with `SyncModule` (each module gets a RemoteExecutor)
- Wraps `TargetConnection` (orchestrator creates RemoteExecutor from connection)

---

## Orchestrator vs SyncSession: Separation of Concerns

**Orchestrator** (Core Orchestration Logic):
- **Responsibilities**:
  - Load configuration and validate structure
  - Create and manage SyncSession
  - Instantiate modules with validated config and RemoteExecutor
  - Execute module lifecycle in sequence (validate → pre_sync → sync → post_sync)
  - Catch module exceptions and log as CRITICAL
  - Track ERROR-level logs to determine final state (COMPLETED vs FAILED)
  - Handle SIGINT (Ctrl+C) and initiate cleanup
  - Call module abort() methods on cleanup
  - Provide injected methods to modules (emit_progress, log)
  - Close SSH connection and release lock on completion

- **Does NOT**:
  - Store state (that's SyncSession's job)
  - Execute commands directly (delegates to modules via RemoteExecutor)
  - Know about specific module implementations

**SyncSession** (State Tracking):
- **Responsibilities**:
  - Track current sync state (INITIALIZING, VALIDATING, EXECUTING, CLEANUP, COMPLETED, ABORTED, FAILED)
  - Store session metadata (ID, timestamp, hostnames)
  - Track which modules are enabled and execution order
  - Record module results (SUCCESS, SKIPPED, FAILED)
  - Flag ERROR logs (`has_errors`) to determine final state
  - Flag abort requests (`abort_requested`) from exceptions or Ctrl+C
  - Manage lock file creation and cleanup

- **Does NOT**:
  - Execute modules (that's Orchestrator's job)
  - Handle exceptions or signals
  - Manage SSH connections

**Key Pattern**: Orchestrator is the "engine" that executes the workflow. SyncSession is the "state container" that records what happened and current status. This separation enables:
- Clear testing boundaries (mock session for unit tests)
- State persistence (session can be serialized for resumption or reporting)
- Clean orchestration logic (no state management cluttering the execution flow)

---

## Entity Relationship Diagram (ERD)

```text
┌─────────────────┐
│   Orchestrator  │
│                 │
│ - modules: []   │
│ - session       │
└────────┬────────┘
         │ creates & manages
         ↓
┌───────────────────────────────────────────────────────┐
│              SyncSession                              │
│                                                       │
│ - id: str                                             │
│ - timestamp: datetime                                 │
│ - source_hostname: str                                │
│ - target_hostname: str                                │
│ - enabled_modules: list[str]                          │
│ - state: SessionState (INIT/VALID/EXEC/CLEANUP/...)  │
│ - module_results: dict[str, ModuleResult]             │
│ - has_errors: bool                                    │
│ - abort_requested: bool                               │
└────┬──────────────────┬──────────────┬───────────────┘
     │ 1:N              │ 1:1          │ 1:N
     │                  │              │
     ↓                  ↓              ↓
┌──────────────┐  ┌──────────────────┐  ┌─────────────┐
│  SyncModule  │  │ TargetConnection │  │  LogEntry   │
│              │  │                  │  │             │
│ - name       │  │ - hostname       │  │ - timestamp │
│ - required   │  │ - connection     │  │ - level     │
│ - config     │  │ - pc_sw_version  │  │ - module    │
│ - remote ────┼──┼─→RemoteExecutor  │  │ - hostname  │
└──────┬───────┘  └──────────────────┘  │ - message   │
       │ uses                            └─────────────┘
       │ 1:1
       ↓
┌──────────────────┐
│ RemoteExecutor   │
│                  │
│ - connection ────┼──> TargetConnection
│ + run()          │
│ + send_file...() │
│ + get_hostname() │
└──────────────────┘

┌──────────────────┐      ┌──────────────────┐
│ ProgressUpdate   │      │   Snapshot       │
│                  │      │                  │
│ - module         │      │ - subvolume      │
│ - percentage     │      │ - snapshot_path  │
│ - current_item   │      │ - timestamp      │
│ - eta            │      │ - session_id     │
└──────────────────┘      │ - type           │
        ↑                 │ - hostname       │
        │ emits N:1       └──────────────────┘
        │                         ↑
   SyncModule                     │ created by N:1
                                  │
                     ┌────────────────────────┐
                     │ BtrfsSnapshotsModule   │
                     │  (extends SyncModule)  │
                     └────────────────────────┘

┌─────────────────┐
│  Configuration  │
│                 │
│ - log_file_level│
│ - log_cli_level │
│ - sync_modules  │
│ - module_configs│
└─────┬───────────┘
      │ provides config 1:N
      ↓
┌─────────────┐
│ SyncModule  │
└─────────────┘
```

## Key Design Patterns

### 1. Abstract Base Class (Module Interface)

The `Module` ABC enforces the module contract (FR-001). All operations (sync features and infrastructure) implement this interface, enabling:
- Independent module development
- Uniform orchestration via simple lifecycle: validate() → execute() → abort()
- Consistent logging and progress reporting via injected methods
- Sequential execution for SyncModules in config-defined order (no complex dependency resolution)
- Infrastructure modules hardcoded by orchestrator:
  - BtrfsSnapshotModule brackets all operations (sequential execution)
  - DiskMonitorModule runs throughout entire operation (parallel execution)
- DRY: All modules (sync and infrastructure, sequential and parallel) reuse the same infrastructure (logging, progress, abort, RemoteExecutor)

### 2. State Machine (SyncSession)

Session state transitions enforce the sync workflow:
- INITIALIZING → establish connection, load config, check/install target version
- VALIDATING → all modules validate before any state changes (including disk_monitor)
- EXECUTING → start DiskMonitor (parallel), then sequential execution: BtrfsSnapshot(pre) → SyncModules → BtrfsSnapshot(post)
- CLEANUP → stop disk_monitor, call abort() on currently-running module (if any)
- COMPLETED / ABORTED / FAILED → terminal states (always through CLEANUP except EXECUTING → COMPLETED)

### 3. Exception-Based Error Handling

Modules raise exceptions (e.g., `SyncError`, `CriticalSyncError`) for unrecoverable failures. Orchestrator catches exceptions, logs them as CRITICAL, and initiates CLEANUP phase. This is cleaner than watching log streams for CRITICAL events.

ERROR-level logs are tracked via `session.has_errors` flag to determine final state (COMPLETED vs FAILED) for recoverable errors.

### 4. Method Injection Pattern

Modules receive functionality via injection rather than inheritance:
- `RemoteExecutor` injected in constructor → module communicates with target
- `emit_progress()` injected by orchestrator → module reports progress
- `log()` injected by orchestrator → module logs messages

This enables easier testing (mock injected dependencies) and cleaner module code.

### 5. Immutable Entities (Snapshot, LogEntry)

Snapshots and log entries are immutable once created. This simplifies reasoning and prevents accidental state corruption.

### 6. Configuration Validation and Injection

Configuration is loaded once, validated against module schemas, and injected into modules. Modules declare schemas via `get_config_schema()`; orchestrator validates and provides validated config. This separates concerns and enables testing with mock configs.

## Implementation Notes

- All entities use modern Python type hints (`str | None`, `dict[str, Any]`, etc.)
- Datetimes are always UTC with timezone info (`datetime.now(UTC)`)
- Enums use `StrEnum` for string-based enums (e.g., `SessionState`, `LogLevel`)
- Path handling uses `pathlib.Path`
- Validation errors return `list[str]` (empty = valid, non-empty = errors with messages)
