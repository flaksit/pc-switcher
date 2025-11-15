# Data Model: Foundation Infrastructure Complete

**Feature**: Foundation Infrastructure Complete
**Date**: 2025-11-15
**Phase**: Phase 1 - Design & Contracts

## Overview

This document defines the key entities, their fields, relationships, and state transitions for the foundation infrastructure.

## Entities

### 1. SyncModule (Abstract Base Class)

Represents a sync component implementing the standardized module interface.

**Fields**:
- `name: str` - Unique module identifier (e.g., "btrfs-snapshots", "dummy-success")
- `version: str` - Module version (semantic versioning)
- `dependencies: list[str]` - Module names this module must run after (topological ordering)
- `required: bool` - Whether module can be disabled via config (default: False, except btrfs-snapshots)
- `config: dict[str, Any]` - Module-specific configuration (validated against schema)
- `logger: structlog.BoundLogger` - Logger instance with bound context (module name, session ID)

**Methods** (all abstract, must be implemented by subclasses):
- `get_config_schema() -> dict[str, Any]` - Returns JSON schema for module config validation
- `validate() -> list[str]` - Pre-sync validation; returns list of error messages (empty if valid)
- `pre_sync() -> None` - Pre-sync operations (e.g., create snapshots)
- `sync() -> None` - Main sync operation
- `post_sync() -> None` - Post-sync operations (e.g., create post-snapshots)
- `cleanup() -> None` - Cleanup on shutdown/error (best-effort)
- `emit_progress(percentage: int, item: str, eta: timedelta | None) -> None` - Report progress to orchestrator

**Validation Rules**:
- `name` must be unique across all registered modules
- `version` must follow semantic versioning (regex: `\d+\.\d+\.\d+`)
- `dependencies` must reference existing module names (checked at registration)
- Circular dependencies are invalid (detected via topological sort)

**State Transitions**: None (stateless, only method execution sequence matters)

**Relationships**:
- Many-to-one with `Orchestrator` (orchestrator manages all modules)
- Many-to-one with `SyncSession` (session tracks module execution)
- Zero-to-many with `ProgressUpdate` (module emits progress updates)
- Zero-to-many with `LogEntry` (module emits log entries)

---

### 2. SyncSession

Represents a single sync operation from source to target.

**Fields**:
- `id: str` - Unique session identifier (8-char hex from UUID)
- `timestamp: datetime` - Session start time (UTC, ISO8601)
- `source_hostname: str` - Source machine hostname
- `target_hostname: str` - Target machine hostname
- `enabled_modules: list[str]` - Module names enabled for this session (from config)
- `state: SessionState` - Current session state (enum)
- `module_results: dict[str, ModuleResult]` - Execution results per module
- `abort_requested: bool` - Whether abort has been signaled (CRITICAL log or Ctrl+C)
- `lock_path: Path` - Lockfile path (`/tmp/pc-switcher-sync.lock` or configurable)

**Enums**:
- `SessionState`: `INITIALIZING`, `VALIDATING`, `EXECUTING`, `COMPLETED`, `ABORTED`, `FAILED`
- `ModuleResult`: `SUCCESS`, `SKIPPED`, `FAILED`

**Validation Rules**:
- `source_hostname` and `target_hostname` must be resolvable or valid SSH config aliases
- `enabled_modules` must reference registered module names
- Required modules (e.g., btrfs-snapshots) cannot be excluded from `enabled_modules`
- Only one active session allowed (enforced via lock file)

**State Transitions**:
```
INITIALIZING → VALIDATING → EXECUTING → COMPLETED
             ↓             ↓           ↓
             └─────────→ ABORTED ←────┘
             ↓             ↓           ↓
             └─────────→ FAILED ←─────┘
```

- `INITIALIZING`: Loading config, registering modules, establishing SSH connection
- `VALIDATING`: Running all module `validate()` methods
- `EXECUTING`: Running module lifecycle (pre_sync → sync → post_sync)
- `COMPLETED`: All modules succeeded
- `ABORTED`: User Ctrl+C or CRITICAL log event triggered abort
- `FAILED`: One or more modules failed (ERROR level, not CRITICAL)

**Relationships**:
- One-to-many with `SyncModule` (session executes multiple modules)
- One-to-many with `LogEntry` (session generates log entries)
- One-to-many with `ProgressUpdate` (session receives progress updates from modules)
- One-to-one with `TargetConnection` (session manages SSH connection)
- Zero-to-many with `Snapshot` (session may create snapshots)

---

### 3. Snapshot

Represents a btrfs snapshot created during sync.

**Fields**:
- `subvolume: str` - Subvolume path being snapshotted (e.g., "/", "/home")
- `snapshot_path: str` - Full path to snapshot (e.g., "/@-presync-20251115T120000Z-abc123")
- `timestamp: datetime` - Snapshot creation time (UTC, ISO8601)
- `session_id: str` - Associated sync session ID
- `type: SnapshotType` - Pre-sync or post-sync (enum)
- `location: Location` - Source or target machine (enum)
- `readonly: bool` - Whether snapshot is read-only (always True for our use case)

**Enums**:
- `SnapshotType`: `PRE_SYNC`, `POST_SYNC`
- `Location`: `SOURCE`, `TARGET`

**Validation Rules**:
- `subvolume` must exist on the machine where snapshot is created
- `snapshot_path` must follow naming pattern: `@{subvolume}-{presync|postsync}-{timestamp}-{session_id}`
- `timestamp` must be ISO8601 format with UTC timezone
- `session_id` must be 8-char hex string

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
- `module: str` - Module name that emitted the log
- `hostname: str` - Machine where event occurred (source or target)
- `message: str` - Log message (human-readable)
- `context: dict[str, Any]` - Structured context data (e.g., file paths, error codes)
- `session_id: str` - Associated sync session ID

**Enums**:
- `LogLevel`: `DEBUG` (10), `FULL` (15), `INFO` (20), `WARNING` (30), `ERROR` (40), `CRITICAL` (50)

**Validation Rules**:
- `timestamp` must be ISO8601 with UTC timezone
- `level` must be one of the six defined levels
- `module` should reference a registered module (or "core" for orchestrator)
- `message` must be non-empty
- If `level == CRITICAL`, orchestrator must set abort signal

**State Transitions**: None (immutable once created)

**Relationships**:
- Many-to-one with `SyncSession` (session generates log entries)
- Emitted by `SyncModule` or `Orchestrator`
- Consumed by `FileLogger` and `TerminalUI`

---

### 5. ProgressUpdate

Represents module progress for display and logging.

**Fields**:
- `module: str` - Module name reporting progress
- `percentage: int` - Progress percentage (0-100)
- `current_item: str` - Description of current operation (e.g., "Copying /home/user/file.txt")
- `eta: timedelta | None` - Estimated time to completion (optional)
- `timestamp: datetime` - Update time (UTC)
- `session_id: str` - Associated sync session ID

**Validation Rules**:
- `percentage` must be in range [0, 100]
- `current_item` should be concise (<100 chars for terminal display)
- `eta` can be None if module doesn't estimate completion time

**State Transitions**: None (ephemeral, only current update matters)

**Relationships**:
- Many-to-one with `SyncModule` (module emits progress updates)
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
- `disk_min_free: int | float` - Minimum free disk space (bytes or percentage)
- `disk_reserve_minimum: int | float` - Reserved space during sync
- `disk_check_interval: int` - Seconds between disk space checks
- `config_path: Path` - Path to loaded config file

**Validation Rules**:
- `log_file_level` and `log_cli_level` must be valid LogLevel enum values
- `sync_modules` keys must reference registered module names
- Required modules (btrfs-snapshots) cannot be disabled (sync_modules value ignored)
- Each entry in `module_configs` must validate against module's `get_config_schema()`
- `disk_min_free`: if float, must be in (0.0, 1.0) for percentage; if int, must be positive bytes
- `disk_check_interval` must be positive integer (seconds)

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
- `run(command: str, sudo: bool = False) -> Result` - Execute command on target
- `check_version() -> str | None` - Detect pc-switcher version on target
- `install_version(version: str, installer_path: Path) -> None` - Install/upgrade pc-switcher
- `send_file(local: Path, remote: Path) -> None` - Upload file to target
- `terminate_processes() -> None` - Send SIGTERM to target-side processes

**Validation Rules**:
- `hostname` must be resolvable or valid SSH config alias
- Connection must succeed before any operations (raises exception if SSH fails)
- `control_path` should use ControlMaster to enable multiplexing

**State Transitions**:
```
DISCONNECTED → CONNECTING → CONNECTED → DISCONNECTING → DISCONNECTED
               ↓                        ↓
               └────────→ ERROR ←───────┘
```

**Relationships**:
- One-to-one with `SyncSession` (session manages one connection)
- Used by all modules for target-side operations

---

## Entity Relationship Diagram (ERD)

```
┌─────────────────┐
│   Orchestrator  │
│                 │
│ - modules: []   │
│ - session       │
└────────┬────────┘
         │ manages
         ↓
┌─────────────────────────────────────────────────────┐
│              SyncSession                            │
│                                                     │
│ - id: str                                           │
│ - timestamp: datetime                               │
│ - source_hostname: str                              │
│ - target_hostname: str                              │
│ - enabled_modules: list[str]                        │
│ - state: SessionState                               │
│ - module_results: dict[str, ModuleResult]           │
│ - abort_requested: bool                             │
└─────┬──────────────────┬──────────────┬────────────┘
      │ 1:N              │ 1:1          │ 1:N
      │                  │              │
      ↓                  ↓              ↓
┌─────────────┐  ┌──────────────────┐  ┌─────────────┐
│ SyncModule  │  │ TargetConnection │  │  LogEntry   │
│             │  │                  │  │             │
│ - name      │  │ - hostname       │  │ - timestamp │
│ - version   │  │ - connection     │  │ - level     │
│ - deps      │  │ - version        │  │ - module    │
│ - required  │  │ - connected      │  │ - message   │
└─────┬───────┘  └──────────────────┘  └─────────────┘
      │ emits
      │ 1:N
      ↓
┌──────────────────┐
│ ProgressUpdate   │
│                  │
│ - module         │
│ - percentage     │
│ - current_item   │
│ - eta            │
└──────────────────┘

┌─────────────────┐
│   Snapshot      │
│                 │
│ - subvolume     │
│ - snapshot_path │
│ - timestamp     │
│ - session_id    │
│ - type          │
│ - location      │
└─────────────────┘
      ↑
      │ N:1
      │ created by
┌─────────────────────┐
│ BtrfsSnapshotsModule│
│  (extends           │
│   SyncModule)       │
└─────────────────────┘

┌─────────────────┐
│  Configuration  │
│                 │
│ - log_file_level│
│ - log_cli_level │
│ - sync_modules  │
│ - module_configs│
└─────┬───────────┘
      │ provides config to
      │ 1:N
      ↓
┌─────────────┐
│ SyncModule  │
└─────────────┘
```

## Key Design Patterns

### 1. Abstract Base Class (Module Interface)

The `SyncModule` ABC enforces the module contract (FR-001). All sync features implement this interface, enabling:
- Independent module development
- Uniform orchestration
- Consistent logging and progress reporting
- Topological dependency ordering

### 2. State Machine (SyncSession)

Session state transitions enforce the sync workflow:
- INITIALIZING → establish connection, load config
- VALIDATING → all modules validate before any state changes
- EXECUTING → sequential module execution (pre_sync → sync → post_sync)
- COMPLETED / ABORTED / FAILED → terminal states with cleanup

### 3. Signal-Based Abort (abort_requested flag + logging hook)

CRITICAL log events set `session.abort_requested = True` via logging hook. Orchestrator checks this flag after each module operation, enabling immediate abort without exception propagation.

### 4. Immutable Entities (Snapshot, LogEntry)

Snapshots and log entries are immutable once created. This simplifies reasoning and prevents accidental state corruption.

### 5. Dependency Injection (Configuration → Modules)

Configuration is loaded once and injected into modules. Modules declare schemas; orchestrator validates and provides validated config. This separates concerns and enables testing with mock configs.

## Implementation Notes

- All entities use modern Python type hints (`str | None`, `dict[str, Any]`, etc.)
- Datetimes are always UTC with timezone info (`datetime.now(UTC)`)
- Enums use `StrEnum` for string-based enums (e.g., `SessionState`, `LogLevel`)
- Path handling uses `pathlib.Path`
- Validation errors return `list[str]` (empty = valid, non-empty = errors with messages)
