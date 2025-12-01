# Implementation Documentation

This document provides detailed implementation-level documentation for the pc-switcher codebase. For architectural overview, see `/home/janfr/dev/pc-switcher/docs/High level requirements.md`.

## Module Overview

All source code is located in `/home/janfr/dev/pc-switcher/src/pcswitcher/`.

| Module | Description |
|--------|-------------|
| `models.py` | Core data types (enums, dataclasses) used throughout the system |
| `executor.py` | Command execution on local and remote machines via async subprocess and SSH |
| `logger.py` | EventBus-based logging infrastructure with file and console outputs |
| `events.py` | Pub/sub event system for decoupled logging and progress reporting |
| `config.py` | YAML configuration loading with schema validation |
| `connection.py` | SSH connection management with keepalive and session multiplexing |
| `orchestrator.py` | Main workflow coordinator executing the complete sync pipeline |
| `lock.py` | File-based locking using fcntl to prevent concurrent syncs |
| `disk.py` | Disk space monitoring and threshold parsing utilities |
| `snapshots.py` | Btrfs snapshot creation, validation, and cleanup operations |
| `installation.py` | Version checking and self-installation on target machines |
| `ui.py` | Rich terminal UI with live progress bars and log panel |
| `cli.py` | Typer-based CLI entry point and command parsing |
| `jobs/` | Job system with base classes and concrete job implementations |

### Jobs Subsystem

| Module | Description |
|--------|-------------|
| `jobs/base.py` | Abstract base classes: Job, SystemJob, SyncJob, BackgroundJob |
| `jobs/context.py` | JobContext dataclass providing executors and config to jobs |
| `jobs/btrfs.py` | BtrfsSnapshotJob for creating pre/post sync snapshots |
| `jobs/disk_space_monitor.py` | DiskSpaceMonitorJob for runtime disk monitoring |
| `jobs/dummy.py` | DummySuccessJob and DummyFailJob for testing |

## Key Data Types

From `/home/janfr/dev/pc-switcher/src/pcswitcher/models.py`:

### Enums

**Host (StrEnum)**
- `SOURCE`: Source machine (where sync is initiated)
- `TARGET`: Target machine (receiving sync data)

**LogLevel (IntEnum)**
- `DEBUG = 0`: Most verbose, internal diagnostics
- `FULL = 1`: Operational details (file-level)
- `INFO = 2`: High-level operations
- `WARNING = 3`: Unexpected but non-fatal
- `ERROR = 4`: Recoverable errors
- `CRITICAL = 5`: Unrecoverable, sync must abort

Lower value = more verbose. Level N includes all messages at level N and above.

**SessionStatus (StrEnum)**
- `RUNNING`: Sync session in progress
- `COMPLETED`: Successfully finished
- `FAILED`: Error occurred
- `INTERRUPTED`: User cancelled (SIGINT)

**JobStatus (StrEnum)**
- `SUCCESS`: Job completed successfully
- `SKIPPED`: Job was skipped (not used currently)
- `FAILED`: Job encountered error

**SnapshotPhase (StrEnum)**
- `PRE`: Snapshot before sync operations
- `POST`: Snapshot after sync operations

### Core Dataclasses

**CommandResult (frozen)**
```python
exit_code: int
stdout: str
stderr: str

@property
def success(self) -> bool:  # True if exit_code == 0
```

**ProgressUpdate (frozen)**
```python
percent: int | None        # 0-100 if known
current: int | None        # Current item count
total: int | None          # Total items if known
item: str | None           # Current item description
heartbeat: bool = False    # True for activity indication only
```

Rendering logic:
- `percent` set: progress bar with percentage
- `current + total` set: "45/100 items"
- `current` only: "45 items processed"
- `heartbeat=True`: spinner/activity indicator

**Snapshot (frozen)**
```python
name: str              # e.g., "pre-@home-20251129T143022"
subvolume: str         # e.g., "@home"
phase: SnapshotPhase   # PRE or POST
timestamp: str         # ISO 8601 timestamp
```

**JobResult (frozen)**
```python
job_name: str
status: JobStatus
started_at: str              # ISO 8601 timestamp
ended_at: str                # ISO 8601 timestamp
error_message: str | None
```

**SyncSession (mutable)**
```python
session_id: str
started_at: str              # ISO 8601 timestamp
source_hostname: str
target_hostname: str
config: dict[str, Any]       # Configuration snapshot
status: SessionStatus
ended_at: str | None
job_results: list[JobResult] | None
error_message: str | None
log_file: str | None         # Path to log file
```

### Error Types

**ConfigError (frozen)**
```python
job: str | None    # None for global config errors
path: str          # JSON path to invalid value
message: str
```

**ValidationError (frozen)**
```python
job: str
host: Host
message: str
```

**DiskSpaceCriticalError (Exception)**
```python
host: Host
hostname: str
free_space: str
threshold: str
```

## Command Execution

From `/home/janfr/dev/pc-switcher/src/pcswitcher/executor.py`:

### Process Protocol

Interface for running processes with streaming output:

```python
class Process(Protocol):
    async def stdout(self) -> AsyncIterator[str]:
        """Iterate over stdout lines as they arrive."""

    async def stderr(self) -> AsyncIterator[str]:
        """Iterate over stderr lines as they arrive."""

    async def wait(self) -> CommandResult:
        """Wait for process to complete and return result."""

    async def terminate(self) -> None:
        """Terminate the process."""
```

Design constraint: stdin is intentionally not supported. All commands must be non-interactive to ensure reliable automated execution.

### LocalExecutor

Executes commands on source machine via `asyncio.subprocess`:

```python
async def run_command(cmd: str, timeout: float | None = None) -> CommandResult
    # Execute command and wait for completion
    # Raises TimeoutError if timeout exceeded

async def start_process(cmd: str) -> LocalProcess
    # Start long-running process with streaming output
    # Returns LocalProcess wrapper

async def terminate_all_processes() -> None
    # Terminate all tracked processes (cleanup)
```

### RemoteExecutor

Executes commands on target machine via SSH (asyncssh):

```python
async def run_command(cmd: str, timeout: float | None = None) -> CommandResult
    # Execute command on remote machine

async def start_process(cmd: str) -> RemoteProcess
    # Start long-running remote process

async def terminate_all_processes() -> None
    # Terminate all tracked remote processes

async def send_file(local: Path, remote: str) -> None
    # Copy file to target via SFTP

async def get_file(remote: str, local: Path) -> None
    # Copy file from target via SFTP

async def get_hostname() -> str
    # Get target machine hostname
```

Both LocalProcess and RemoteProcess implement the Process protocol for uniform handling of streaming output.

## Event System

From `/home/janfr/dev/pc-switcher/src/pcswitcher/events.py`:

### EventBus

Pub/sub event bus with per-consumer queues for fan-out to multiple consumers:

```python
def subscribe(self) -> asyncio.Queue[Event | None]:
    # Create new consumer queue
    # Returns queue that receives all future events
    # None sentinel signals shutdown

def publish(event: Event) -> None:
    # Publish event to all consumer queues (non-blocking)
    # Events dropped silently if bus is closed

def close(self) -> None:
    # Signal consumers to drain and exit
    # Sends None sentinel to all queues
```

### Event Types

**LogEvent (frozen)**
```python
level: LogLevel
job: str                      # Job name or "orchestrator"
host: Host
message: str
context: dict[str, Any]       # Additional structured context
timestamp: datetime

def to_dict(self) -> dict[str, Any]:
    # Convert to flat dict for JSON serialization
```

**ProgressEvent (frozen)**
```python
job: str
update: ProgressUpdate
timestamp: datetime
```

**ConnectionEvent (frozen)**
```python
status: str                   # "connected", "disconnected"
latency: float | None         # Round-trip time in ms
```

## Logging System

From `/home/janfr/dev/pc-switcher/src/pcswitcher/logger.py`:

Architecture: EventBus-based decoupling allows multiple consumers (file logger, console logger, UI) to subscribe independently without coupling jobs to output formats.

### Logger

Main logger that publishes LogEvents to EventBus:

```python
def log(level: LogLevel, host: Host, message: str, **context: Any) -> None:
    # Publish LogEvent with structured context
```

### JobLogger

Logger bound to specific job name (avoids passing job name repeatedly):

```python
def log(level: LogLevel, host: Host, message: str, **context: Any) -> None:
    # Publish LogEvent with job name pre-filled
```

### FileLogger

Consumes LogEvents and writes JSON lines to file:

```python
async def consume(self) -> None:
    # Background task consuming events from queue
    # Writes one JSON object per line (no nesting)
    # Flushes after each write
    # Exits on None sentinel
```

File format: JSON lines (one complete JSON object per line). Each line contains flattened fields:
```json
{"timestamp": "2025-11-30T14:30:22", "level": "INFO", "job": "btrfs_snapshot", "host": "source", "event": "Creating snapshot"}
```

### ConsoleLogger

Consumes LogEvents and writes colored output to terminal via Rich:

```python
async def consume(self) -> None:
    # Background task consuming events from queue
    # Renders colored console output
    # Exits on None sentinel

LEVEL_COLORS: ClassVar[dict[LogLevel, str]] = {
    LogLevel.DEBUG: "dim",
    LogLevel.FULL: "cyan",
    LogLevel.INFO: "green",
    LogLevel.WARNING: "yellow",
    LogLevel.ERROR: "red",
    LogLevel.CRITICAL: "bold red",
}
```

Output format: `[timestamp] [LEVEL] [job] (hostname) message context_key=value`

### Utility Functions

```python
def generate_log_filename(session_id: str) -> str:
    # Returns: sync-<timestamp>-<session_id>.log

def get_logs_directory() -> Path:
    # Returns: ~/.local/share/pc-switcher/logs

def get_latest_log_file() -> Path | None:
    # Returns most recent log file or None
```

## Safety Mechanisms

### Locking

From `/home/janfr/dev/pc-switcher/src/pcswitcher/lock.py`:

**SyncLock** - File-based lock using `fcntl.flock()`:

```python
def acquire(holder_info: str | None = None) -> bool:
    # Acquire exclusive lock non-blocking
    # Returns True if acquired, False if held by another process
    # Lock automatically released on process exit (normal, crash, or kill)
    # holder_info written to file for diagnostics only

def get_holder_info(self) -> str | None:
    # Read info about process holding lock

def release(self) -> None:
    # Explicitly release lock (safe to call multiple times)
```

Lock file location: `~/.local/share/pc-switcher/sync.lock`

Design: Uses `fcntl.flock()` for atomic lock acquisition with automatic release. File contents contain holder info (PID/hostname) for diagnostic error messages, but the actual lock is the fcntl lock, not file existence. This prevents race conditions and stale locks.

**Remote Lock Acquisition**:

```python
async def acquire_target_lock(executor: RemoteExecutor, source_hostname: str) -> bool:
    # Acquire lock on target machine via SSH using flock command
    # Lock automatically released when SSH connection closes
    # Returns True if acquired, False if already held
```

Lock file location on target: `~/.local/share/pc-switcher/target.lock`

Implementation: Uses shell file descriptor 9 with `flock -n 9` for non-blocking acquisition.

### Btrfs Snapshots

From `/home/janfr/dev/pc-switcher/src/pcswitcher/snapshots.py`:

**Snapshot Creation**:

```python
async def create_snapshot(
    executor: LocalExecutor | RemoteExecutor,
    source_path: str,
    snapshot_path: str,
) -> CommandResult:
    # Create read-only btrfs snapshot
    # Command: sudo btrfs subvolume snapshot -r <source> <snapshot>
```

**Snapshot Naming**:

```python
def snapshot_name(subvolume: str, phase: SnapshotPhase) -> str:
    # Format: "{phase}-{subvolume}-{timestamp}"
    # Example: "pre-@home-20251129T143022"

def session_folder_name(session_id: str) -> str:
    # Format: "{timestamp}-{session_id}"
    # Example: "20251129T143022-abc12345"
```

Snapshot directory structure:
```
/.snapshots/pc-switcher/
├── 20251129T143022-abc12345/
│   ├── pre-@-20251129T143022
│   ├── pre-@home-20251129T143022
│   ├── post-@-20251129T143030
│   └── post-@home-20251129T143030
└── 20251130T091500-def67890/
    └── ...
```

**Validation**:

```python
async def validate_snapshots_directory(
    executor: LocalExecutor | RemoteExecutor,
    host: Host,
) -> tuple[bool, str | None]:
    # Check if /.snapshots exists and is a subvolume
    # Creates it if missing
    # Returns (success, error_message)

async def validate_subvolume_exists(
    executor: LocalExecutor | RemoteExecutor,
    subvolume: str,
    mount_point: str,
    host: Host,
) -> tuple[bool, str | None]:
    # Validate subvolume exists at expected mount point
    # Returns (success, error_message)
```

**Cleanup**:

```python
async def cleanup_snapshots(
    executor: LocalExecutor | RemoteExecutor,
    session_folder: str,
    keep_recent: int,
    max_age_days: int | None = None,
) -> list[str]:
    # Delete old snapshots based on retention policy
    # keep_recent: Keep N most recent session folders
    # max_age_days: Delete folders older than N days
    # Returns list of deleted snapshot paths
```

### Disk Space Monitoring

From `/home/janfr/dev/pc-switcher/src/pcswitcher/disk.py`:

**DiskSpace** dataclass:
```python
total_bytes: int
used_bytes: int
available_bytes: int
use_percent: int
mount_point: str
```

**Functions**:

```python
async def check_disk_space(
    executor: LocalExecutor | RemoteExecutor,
    mount_point: str,
) -> DiskSpace:
    # Run `df -B1 <mount_point>` and parse output
    # Raises RuntimeError if command fails or mount not found

def parse_threshold(threshold: str) -> tuple[str, int]:
    # Parse "20%" -> ("percent", 20)
    # Parse "50GiB" -> ("bytes", 53687091200)
    # Supports: GiB, MiB, GB, MB
    # Raises ValueError if format invalid
```

## Job System

From `/home/janfr/dev/pc-switcher/src/pcswitcher/jobs/base.py`:

### Job Base Class

Abstract base class defining job interface:

```python
class Job(ABC):
    name: ClassVar[str]                    # Job identifier
    required: ClassVar[bool] = False       # Whether job is required
    CONFIG_SCHEMA: ClassVar[dict] = {}     # JSON Schema for job config

    @classmethod
    def validate_config(cls, config: dict) -> list[ConfigError]:
        # Phase 2 validation: Check config against CONFIG_SCHEMA
        # Returns list of ConfigError (empty if valid)

    @abstractmethod
    async def validate(self, context: JobContext) -> list[ValidationError]:
        # Phase 3 validation: Check system state before execution
        # Called after SSH connection, before state modifications
        # Returns list of ValidationError (empty if valid)

    @abstractmethod
    async def execute(self, context: JobContext) -> None:
        # Execute job logic
        # May raise Exception to halt sync
        # Must handle asyncio.CancelledError for cleanup

    def _log(context: JobContext, host: Host, level: LogLevel, message: str, **extra: Any):
        # Log message through EventBus

    def _report_progress(context: JobContext, update: ProgressUpdate):
        # Report progress through EventBus
```

### Job Types

**SystemJob** (required=True):
- Required infrastructure jobs (snapshots, installation)
- Run regardless of sync_jobs config
- Orchestrator-managed

**SyncJob** (required=False):
- Optional user-facing sync jobs
- Can be enabled/disabled via sync_jobs config

**BackgroundJob** (required=True):
- Jobs that run concurrently with other jobs
- Example: DiskSpaceMonitorJob
- Spawned in TaskGroup alongside sync jobs

### JobContext

From `/home/janfr/dev/pc-switcher/src/pcswitcher/jobs/context.py`:

```python
@dataclass(frozen=True)
class JobContext:
    config: dict[str, Any]      # Job-specific config (validated)
    source: LocalExecutor       # For source machine commands
    target: RemoteExecutor      # For target machine commands
    event_bus: EventBus         # For logging and progress
    session_id: str             # Current sync session ID
    source_hostname: str        # Source machine hostname
    target_hostname: str        # Target machine hostname
```

### Job Lifecycle

1. **Discovery**: Orchestrator scans enabled jobs from config
2. **Phase 2 Validation**: `validate_config()` checks job-specific config against schema
3. **Phase 3 Validation**: `validate()` checks system state (SSH must be connected)
4. **Execution**: `execute()` performs sync operations
5. **Cleanup**: Jobs must handle `asyncio.CancelledError` for graceful shutdown

## Configuration System

From `/home/janfr/dev/pc-switcher/src/pcswitcher/config.py`:

### Configuration Dataclasses

**DiskConfig**:
```python
preflight_minimum: str = "20%"    # Percentage or absolute (e.g., "50GiB")
runtime_minimum: str = "15%"
check_interval: int = 30          # Seconds
```

**BtrfsConfig**:
```python
subvolumes: list[str] = ["@", "@home"]
keep_recent: int = 3
max_age_days: int | None = None   # None = no age limit
```

**Configuration**:
```python
log_file_level: LogLevel = LogLevel.FULL
log_cli_level: LogLevel = LogLevel.INFO
sync_jobs: dict[str, bool]                  # job_name -> enabled
disk: DiskConfig
btrfs_snapshots: BtrfsConfig
job_configs: dict[str, dict[str, Any]]      # job_name -> config

@classmethod
def from_yaml(cls, path: Path) -> Configuration:
    # Load and validate configuration from YAML
    # Raises ConfigurationError with list of errors

@classmethod
def get_default_config_path(cls) -> Path:
    # Returns: ~/.config/pc-switcher/config.yaml
```

### Validation Process

1. **YAML Parsing**: Load YAML file with syntax error handling
2. **Schema Validation**: Validate against JSON schema using jsonschema
3. **Log Level Parsing**: Convert string log levels to LogLevel enum
4. **Default Application**: Apply defaults for missing fields
5. **Job Config Extraction**: Extract job-specific configs from top-level keys

Error handling: All errors collected as `ConfigError` objects and raised together in `ConfigurationError` for comprehensive reporting.

## Orchestrator

From `/home/janfr/dev/pc-switcher/src/pcswitcher/orchestrator.py`:

Main workflow coordinator executing the complete sync pipeline.

### Responsibilities

1. Schema and job config validation
2. SSH connection management
3. Lock acquisition (source and target)
4. Version check and self-installation
5. System state validation (delegated to jobs)
6. Sequential job execution
7. Background job management (DiskSpaceMonitor)
8. Sync summary and session tracking

### Execution Phases

```python
async def run(self) -> SyncSession:
    # Phase 1: Acquire source lock
    # Phase 2: Establish SSH connection
    # Phase 3: Acquire target lock
    # Phase 4: Version compatibility check (error if target > source)
    # Phase 5: Job discovery and validation
    # Phase 6: Pre-sync snapshots
    # Phase 7: Install/upgrade pc-switcher on target (if needed)
    # Phase 8: Execute sync jobs with background monitoring
    # Phase 9: Post-sync snapshots
    # Finally: Cleanup (connection, locks, executors)
```

### Job Execution

Jobs execute sequentially within an `asyncio.TaskGroup` that also runs background monitoring jobs concurrently:

```python
async with asyncio.TaskGroup() as tg:
    # Start background disk monitors
    tg.create_task(source_monitor.execute(context))
    tg.create_task(target_monitor.execute(context))

    # Execute sync jobs sequentially
    for job in jobs:
        await job.execute(context)
```

If any job raises an exception, the TaskGroup cancels all other tasks and propagates the exception.

### Error Handling

- `asyncio.CancelledError`: Sets session status to INTERRUPTED
- Other exceptions: Sets session status to FAILED with error message
- All cases: Cleanup is guaranteed in finally block

## SSH Connection Management

From `/home/janfr/dev/pc-switcher/src/pcswitcher/connection.py`:

### Connection Class

Manages SSH connection with multiplexing and keepalive:

```python
def __init__(
    target: str,
    max_sessions: int = 10,              # Max concurrent SSH sessions
    keepalive_interval: int = 15,        # Seconds between keepalives
    keepalive_count_max: int = 3,        # Max missed keepalives
):
    # Initialize connection parameters

async def connect(self) -> None:
    # Establish SSH connection
    # Respects ~/.ssh/config automatically

async def disconnect(self) -> None:
    # Close connection gracefully

async def create_process(cmd: str) -> asyncssh.SSHClientProcess[str]:
    # Create remote process (uses semaphore for session limiting)

async def run(cmd: str) -> asyncssh.SSHCompletedProcess:
    # Run command and wait for completion
```

Design: Uses asyncssh with keepalive for connection health monitoring and a semaphore for session multiplexing to prevent overwhelming the SSH server.

## Version Management

From `/home/janfr/dev/pc-switcher/src/pcswitcher/installation.py`:

### Version Functions

```python
def get_this_version() -> str:
    # Get version from package metadata (source machine)
    # Raises InstallationError if metadata not found

async def get_target_version(executor: RemoteExecutor) -> str | None:
    # Run `pc-switcher --version` on target
    # Returns version string or None if not installed

def compare_versions(source: str, target: str) -> int:
    # Returns: -1 (source older), 0 (same), 1 (source newer)
    # Uses packaging.version.Version for PEP 440 compliance

async def install_on_target(executor: RemoteExecutor, version: str) -> None:
    # Install using: uv tool install pcswitcher=={version}
    # Timeout: 300 seconds
    # Raises InstallationError on failure
```

### Installation Policy

Orchestrator enforces these rules:
1. Target newer than source: **Error** (cannot sync from older to newer)
2. Target same as source: **No action**
3. Target older than source or not installed: **Install/upgrade**

## Terminal UI

From `/home/janfr/dev/pc-switcher/src/pcswitcher/ui.py`:

### TerminalUI Class

Rich-based live terminal interface with:
- Connection status and latency display
- Overall sync progress (Step N/M)
- Per-job progress bars
- Scrolling log panel with recent messages

```python
def start(self) -> None:
    # Start live display at 10 Hz refresh rate

def stop(self) -> None:
    # Stop live display

def update_job_progress(job: str, update: ProgressUpdate) -> None:
    # Update or create progress bar for job
    # Handles percent, count, current-only, and heartbeat modes

def add_log_message(message: str) -> None:
    # Add message to scrolling log panel
    # Auto-scrolls (keeps max_log_lines most recent)

def set_connection_status(status: str, latency: float | None) -> None:
    # Update connection status indicator

def set_current_step(step: int) -> None:
    # Update overall progress (Step N/M)

async def consume_events(
    queue: asyncio.Queue[Any],
    hostname_map: dict[Host, str] | None,
    log_level: LogLevel,
) -> None:
    # Background task consuming EventBus queue
    # Dispatches events to appropriate UI update methods
```

UI Layout:
```
┌─────────────────────────────────────────────────┐
│ Connection: connected (45.2ms)  Step 3/8        │
├─────────────────────────────────────────────────┤
│ [job1         ] ████████░░░░░░░░ 50%            │
│ [job2         ] ██████████████░░ 75%            │
├─────────────────────────────────────────────────┤
│ Recent Logs                                     │
│ 14:30:22 [INFO    ] [btrfs] (source) Starting   │
│ 14:30:23 [FULL    ] [btrfs] (target) Created    │
│ ...                                             │
└─────────────────────────────────────────────────┘
```

## Testing

Tests are organized in `/home/janfr/dev/pc-switcher/tests/`:

```
tests/
├── unit/              # Unit tests for individual modules
├── integration/       # Integration tests with real components
├── contract/          # Contract tests for interfaces (e.g., Job protocol)
└── conftest.py        # Shared pytest fixtures
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_installation.py

# Run with coverage
uv run pytest --cov=pcswitcher
```

### Common Fixtures

From `/home/janfr/dev/pc-switcher/tests/conftest.py`:

```python
@pytest.fixture
def mock_executor() -> MagicMock:
    # Mock executor with run_command, start_process, terminate_all_processes

@pytest.fixture
def mock_remote_executor(mock_executor) -> MagicMock:
    # Mock remote executor with additional send_file, get_file, get_hostname

@pytest.fixture
def mock_event_bus() -> MagicMock:
    # Mock EventBus with subscribe, publish, close

@pytest.fixture
def sample_command_result() -> CommandResult:
    # Successful command result (exit_code=0)

@pytest.fixture
def failed_command_result() -> CommandResult:
    # Failed command result (exit_code=1)
```

## Development Tools

From `/home/janfr/dev/pc-switcher/pyproject.toml`:

All commands must use `uv run` prefix (never use system Python directly):

```bash
# Type checking
uv run basedpyright

# Linting and formatting
uv run ruff check
uv run ruff format

# Spell checking
uv run codespell

# Running the CLI
uv run pc-switcher --help
```

### Dependencies

**Runtime**:
- asyncssh: SSH connection and remote command execution
- jsonschema: Configuration validation
- packaging: Version comparison (PEP 440)
- pytimeparse2: Human-readable duration parsing
- pyyaml: Configuration file parsing
- rich: Terminal UI and colored output
- structlog: Structured logging
- typer: CLI framework

**Development**:
- basedpyright: Static type checking
- codespell: Spell checking
- pytest: Test framework
- pytest-asyncio: Async test support
- ruff: Fast Python linter and formatter

## Design Patterns

### EventBus Pattern

Decouples event producers (jobs, orchestrator) from consumers (file logger, console logger, UI). Each consumer gets its own queue, preventing blocking between consumers.

Benefits:
- Jobs don't know about output formats
- Easy to add new consumers (e.g., metrics collection)
- Non-blocking event publishing

### Executor Pattern

Unified interface (`run_command`, `start_process`) for local and remote command execution. Jobs use the same API regardless of target machine.

Benefits:
- Jobs don't care about local vs remote
- Easy to test with mock executors
- Consistent error handling

### Job Plugin System

Jobs are self-contained with:
- Own configuration schema (CONFIG_SCHEMA)
- System state validation (validate)
- Execution logic (execute)
- Progress reporting via EventBus

Benefits:
- Jobs are independently testable
- Easy to add new sync operations
- Clear separation of concerns

### Three-Phase Validation

1. **Phase 1**: YAML schema validation (early feedback)
2. **Phase 2**: Job config validation (before SSH connection)
3. **Phase 3**: System state validation (after SSH, before modifications)

Benefits:
- Fail fast with clear error messages
- All validation errors reported together
- No partial state modifications on validation failure

## Cross-References

For architectural context and high-level design decisions, see:
- `/home/janfr/dev/pc-switcher/docs/High level requirements.md` - Project vision, scope, workflow
- `/home/janfr/dev/pc-switcher/docs/adr/_index.md` - Architectural decision records

For user-facing documentation (when available):
- User manual
- Installation guide
- Configuration reference
