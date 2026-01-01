# Data Model: Standard Python Logging Integration

**Branch**: `004-python-logging`
**Date**: 2025-12-31
**Related ADR**: [ADR-010](../../docs/adr/adr-010-logging-infrastructure.md)

## Overview

This document defines the key entities for pc-switcher's logging infrastructure migration to Python's standard `logging` module.

## Entities

### LogConfig

Configuration entity holding log level settings from config file.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file` | `int` | `10` (DEBUG) | Floor log level for file output. |
| `tui` | `int` | `20` (INFO) | Floor log level for TUI output. |
| `external` | `int` | `30` (WARNING) | Additional floor for non-pcswitcher loggers, applies to both file and TUI. |

**Validation Rules**:
- All values must be valid log levels: `0` (NOTSET), `10` (DEBUG), `15` (FULL), `20` (INFO), `30` (WARNING), `40` (ERROR), `50` (CRITICAL)
- String aliases accepted: `"DEBUG"`, `"FULL"`, `"INFO"`, `"WARNING"`, `"ERROR"`, `"CRITICAL"` (case-insensitive)
- Invalid values cause startup failure with `ConfigurationError`

**Config File Representation** (`config.yaml`):
```yaml
logging:
  file: DEBUG      # Or integer 10
  tui: INFO        # Or integer 20
  external: WARNING  # Or integer 30
```

---

### LogLevel (Extended)

The existing `LogLevel` IntEnum from `models.py` aligned with stdlib logging values.

| Level | Value | stdlib Equivalent | Description |
|-------|-------|-------------------|-------------|
| `DEBUG` | `10` | `logging.DEBUG` | Internal diagnostics |
| `FULL` | `15` | Custom | Operational details (file-level) |
| `INFO` | `20` | `logging.INFO` | High-level operations |
| `WARNING` | `30` | `logging.WARNING` | Unexpected but non-fatal |
| `ERROR` | `40` | `logging.ERROR` | Recoverable errors |
| `CRITICAL` | `50` | `logging.CRITICAL` | Unrecoverable, sync must abort |

**Mapping to stdlib**:
```python
# Current (models.py - IntEnum values 0-5)
class LogLevel(IntEnum):
    DEBUG = 0
    FULL = 1
    INFO = 2
    WARNING = 3
    ERROR = 4
    CRITICAL = 5

# After migration (align with stdlib values)
class LogLevel(IntEnum):
    DEBUG = 10
    FULL = 15
    INFO = 20
    WARNING = 30
    ERROR = 40
    CRITICAL = 50
```

**Registration**:
```python
import logging
logging.addLevelName(15, "FULL")
```

---

### LogContext

Structured context added to log records from pcswitcher code via `extra` dict.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job` | `str` | No | Job name (e.g., `"btrfs"`, `"orchestrator"`). Omitted during startup/shutdown. |
| `host` | `str` | No | Logical role (`"source"` or `"target"`). Omitted during startup/shutdown. |
| `**context` | `dict[str, Any]` | No | Additional key=value pairs |

**Fallback Behavior**: When `job` or `host` are missing (e.g., during startup, configuration loading, shutdown):
- `JsonFormatter`: Omits the field from JSON output (no empty string or null)
- `RichFormatter`: Omits the bracketed segment from output (e.g., `10:30:45 [INFO    ] Starting up` instead of `10:30:45 [INFO    ] [] () Starting up`)

**Implementation**: Added via `logging.LoggerAdapter` or `extra` parameter:
```python
# Option 1: LoggerAdapter (recommended for bound context during sync)
adapter = logging.LoggerAdapter(logger, {"job": "btrfs", "host": "source"})
adapter.info("Starting sync", extra={"subvolume": "@home"})

# Option 2: extra dict (for one-off context)
logger.info("Starting sync", extra={"job": "btrfs", "host": "source", "subvolume": "@home"})

# Option 3: No context (for startup/shutdown logs)
logger.info("Configuration loaded")
```

---

### JsonFormatter

Custom `logging.Formatter` for JSON lines file output.

| Output Field | Source | Description |
|--------------|--------|-------------|
| `timestamp` | `record.created` | ISO format timestamp |
| `level` | `record.levelname` | Log level name |
| `job` | `record.job` | Job name from extra |
| `host` | `record.host` | Host role from extra |
| `message` | `record.getMessage()` | Log message |
| `*context` | `record.__dict__` | Additional context fields |

---

### RichFormatter

Custom `logging.Formatter` for Rich-colored TUI output.

**Output Format**: `HH:MM:SS [LEVEL   ] [job] (host) message context`

**Implementation**: Uses `rich.text.Text` to build styled output, then exports to ANSI escape codes. This ensures colors render correctly when written to stderr via `StreamHandler`.

| Component | Source | Style |
|-----------|--------|-------|
| Timestamp | `record.created` | `dim` |
| Level | `record.levelname` | Level-specific color |
| Job | `record.job` | `blue` |
| Host | `record.host` | `magenta` |
| Message | `record.getMessage()` | default |
| Context | Extra fields | `dim` |

**Level Colors**:
| Level | Color |
|-------|-------|
| DEBUG | `dim` |
| FULL | `cyan` |
| INFO | `green` |
| WARNING | `yellow` |
| ERROR | `red` |
| CRITICAL | `bold red` |

---

### HandlerConfig

Configuration for each log output handler.

| Handler | Type | Level | Formatter |
|---------|------|-------|-----------|
| File | `FileHandler` | `LogConfig.file` | `JsonFormatter` |
| TUI | `StreamHandler` | `LogConfig.tui` | `RichFormatter` |

**Logger hierarchy** (handles 3-setting model without custom filters):
```python
# pcswitcher logger - separate handler, bypasses root filter
pcswitcher = logging.getLogger("pcswitcher")
pcswitcher.setLevel(min(file, tui))
pcswitcher.addHandler(QueueHandler(queue))
pcswitcher.propagate = False  # Critical: don't propagate to root

# Root logger - external libs only
logging.getLogger().setLevel(external)
logging.getLogger().addHandler(QueueHandler(queue))
```

---

## State Transitions

### Logging Pipeline Flow

```
                    ┌─────────────────────────────────────────┐
                    │         Application Code                │
                    │  logging.getLogger("pcswitcher.xxx")    │
                    └───────────────────┬─────────────────────┘
                                        │
┌───────────────────────────────────────┼───────────────────────────────────────┐
│                                       ▼                                       │
│                          ┌─────────────────────────┐                          │
│                          │  stdlib logging.Logger  │◄─── External libs        │
│                          │     (root logger)       │     (asyncssh, etc.)     │
│                          └───────────┬─────────────┘                          │
│                                      │                                        │
│                                      ▼                                        │
│                          ┌─────────────────────────┐                          │
│                          │     QueueHandler        │                          │
│                          │    (non-blocking)       │                          │
│                          └───────────┬─────────────┘                          │
│                                      │                                        │
│                                      ▼                                        │
│                          ┌─────────────────────────┐                          │
│                          │    QueueListener        │                          │
│                          │  (background thread)    │                          │
│                          └─────┬───────────┬───────┘                          │
│                                │           │                                  │
│              ┌─────────────────┘           └─────────────────┐                │
│              ▼                                               ▼                │
│    ┌──────────────────┐                           ┌──────────────────┐        │
│    │  FileHandler     │                           │  StreamHandler   │        │
│    │ level=file_level │                           │ level=tui_level  │        │
│    └────────┬─────────┘                           └────────┬─────────┘        │
│             │                                              │                  │
│             ▼                                              ▼                  │
│    ┌──────────────────┐                           ┌──────────────────┐        │
│    │  JsonFormatter   │                           │  RichFormatter   │        │
│    └────────┬─────────┘                           └────────┬─────────┘        │
│             │                                              │                  │
│             ▼                                              ▼                  │
│      JSON Lines File                                 Rich TUI Output          │
│                                                                               │
└───────────────────────────────────────────────────────────────────────────────┘
```

### Log Record Lifecycle

1. **Creation**: Application calls `logger.info("msg", extra={...})`
2. **Context**: LoggerAdapter adds bound context (job, host) or via `extra`
3. **Routing**: stdlib Logger dispatches to QueueHandler
4. **Queueing**: QueueHandler enqueues (non-blocking)
5. **Dispatch**: QueueListener dequeues in background thread
6. **Filtering**: Each handler applies level check + ExternalLibraryFilter
7. **Formatting**: JsonFormatter or RichFormatter renders
8. **Output**: Written to file or console

### QueueListener Lifecycle Management

The `QueueListener` runs in a background thread and must be properly stopped on application exit to ensure all pending log records are flushed. Without explicit teardown, the last few log lines (often critical error details) may be lost.

**Lifecycle Strategy**: `atexit` handler registered during logging setup.

```python
import atexit
from logging.handlers import QueueHandler, QueueListener

def setup_logging(config: LogConfig) -> None:
    """Set up logging infrastructure with guaranteed cleanup."""
    queue: Queue[logging.LogRecord] = Queue(-1)

    # Create handlers
    file_handler = FileHandler(log_path)
    tui_handler = StreamHandler(sys.stderr)

    # Create and start listener
    listener = QueueListener(queue, file_handler, tui_handler, respect_handler_level=True)
    listener.start()

    # Register cleanup - guaranteed to run on normal exit, sys.exit(), or unhandled exception
    atexit.register(listener.stop)

    # Attach QueueHandler to root logger
    root = logging.getLogger()
    root.addHandler(QueueHandler(queue))
```

**Why `atexit` over alternatives**:
- **`try...finally` in main()**: Doesn't cover all exit paths (e.g., `sys.exit()` from deep call stack)
- **Context manager**: Would require restructuring CLI entry point; less idiomatic for logging setup
- **`atexit`**: Simple, reliable, covers normal exit, `sys.exit()`, and unhandled exceptions. Standard pattern for logging cleanup per Python Logging Cookbook.

**Edge Cases**:
- **SIGTERM/SIGKILL**: `atexit` handlers do NOT run on forced signals. This is acceptable—hard kills will always lose buffered data. Users can configure signal handlers separately if needed.
- **Multiple `setup_logging()` calls**: Each call registers a new handler. The design should either prevent multiple calls or track/cleanup previous listeners.

---

## Relationships

```
LogConfig
    ├── file ──────────────► FileHandler.level
    ├── tui ───────────────► StreamHandler.level
    └── external ──────────► Root logger level

LogLevel ←──────────────── LogConfig.file, .tui, .external (values)

Logger hierarchy (separate handlers, no propagation):
    Root logger (level=external) ◄── asyncssh, other external
         └─► QueueHandler ─► queue

    pcswitcher (level=min(file,tui), propagate=False) ◄── pcswitcher.*
         └─► QueueHandler ─► queue (same queue)

JsonFormatter ────────────► FileHandler
RichFormatter ────────────► StreamHandler (TUI)

LogContext ───────────────► Attached to LogRecord via extra dict
```

---

## Integration with Existing Models

### Modified Entities

| Entity | File | Change |
|--------|------|--------|
| `LogLevel` | `models.py` | Update values from 0-5 to 10-50 scale |
| `Configuration` | `config.py` | Add `logging: LogConfig` field |
| `LogEvent` | `events.py` | Deprecated (replaced by stdlib LogRecord) |
| `Logger` | `logger.py` | Replace with stdlib logging setup |
| `FileLogger` | `logger.py` | Replace with JsonFormatter + FileHandler |
| `ConsoleLogger` | `logger.py` | Replace with RichFormatter + StreamHandler |

### Preserved Entities

| Entity | File | Reason |
|--------|------|--------|
| `EventBus` | `events.py` | Still used for ProgressEvent, ConnectionEvent (non-logging) |
| `ProgressEvent` | `events.py` | Unchanged - separate from logging |
| `ConnectionEvent` | `events.py` | Unchanged - separate from logging |
