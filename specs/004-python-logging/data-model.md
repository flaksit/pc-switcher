# Data Model: Standard Python Logging Integration

**Branch**: `004-python-logging`
**Date**: 2025-12-31

## Overview

This document defines the key entities for pc-switcher's logging infrastructure migration to stdlib + structlog.

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

The existing `LogLevel` IntEnum from `models.py` extended with the custom FULL level.

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

### ExternalLibraryFilter

A `logging.Filter` subclass that implements the `external` level floor for non-pcswitcher loggers.

| Field | Type | Description |
|-------|------|-------------|
| `external_level` | `int` | Floor level for external libraries |

**Filter Logic**:
```
IF logger.name starts with "pcswitcher":
    PASS (defer to handler's base level)
ELSE:
    PASS only if record.levelno >= external_level
```

**Attachment**: Instance attached to each handler (file, TUI).

---

### LogContext

Structured context added to every log record from pcswitcher code.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `job` | `str` | Yes | Job name (e.g., `"btrfs"`, `"orchestrator"`) |
| `host` | `str` | Yes | Logical role (`"source"` or `"target"`) |
| `**context` | `dict[str, Any]` | No | Additional key=value pairs |

**Implementation**: Added via structlog processor.

---

### ProcessorChain

The shared processor chain applied to all log records (structlog and stdlib).

| Processor | Purpose |
|-----------|---------|
| `structlog.stdlib.add_log_level` | Add `level` key from record |
| `structlog.processors.TimeStamper(fmt="iso")` | Add ISO timestamp |
| `add_job_and_host` | Custom: Add job/host context |
| `structlog.processors.EventRenamer("message")` | Rename `event` to `message` |
| `structlog.processors.StackInfoRenderer()` | Format stack traces |
| `structlog.processors.ExceptionRenderer()` | Format exceptions |

**Final Processor** (structlog-originated logs):
- `structlog.stdlib.ProcessorFormatter.wrap_for_formatter`

**Foreign Pre-chain** (stdlib-originated logs):
- Same processors as above

---

### HandlerConfig

Configuration for each log output handler.

| Handler | Level Source | Filter | Formatter |
|---------|-------------|--------|-----------|
| File | `LogConfig.file` | `ExternalLibraryFilter(external_level)` | `ProcessorFormatter` + `JSONRenderer` |
| TUI | `LogConfig.tui` | `ExternalLibraryFilter(external_level)` | `ProcessorFormatter` + `ConsoleRenderer` |

---

## State Transitions

### Logging Pipeline Flow

```
                              ┌────────────────────────┐
                              │   pcswitcher code      │
                              │   log.info("msg", k=v) │
                              └──────────┬─────────────┘
                                         │
                                         ▼
                              ┌────────────────────────┐
                              │  structlog bound       │
                              │  logger (processing)   │
                              └──────────┬─────────────┘
                                         │
                    ┌────────────────────┼────────────────────┐
                    │                    │                    │
                    ▼                    ▼                    ▼
          ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
          │   asyncssh      │  │  other stdlib   │  │   stdlib root   │
          │    logger       │  │    loggers      │  │     logger      │
          └────────┬────────┘  └────────┬────────┘  └────────┬────────┘
                   │                    │                    │
                   └────────────────────┼────────────────────┘
                                        │
                                        ▼
                              ┌────────────────────────┐
                              │     QueueHandler       │
                              │     (non-blocking)     │
                              └──────────┬─────────────┘
                                         │
                                         ▼
                              ┌────────────────────────┐
                              │     QueueListener      │
                              │   (background thread)  │
                              └──────────┬─────────────┘
                                         │
                    ┌────────────────────┴────────────────────┐
                    │                                         │
                    ▼                                         ▼
          ┌─────────────────────┐               ┌─────────────────────┐
          │   FileHandler       │               │   StreamHandler     │
          │  level=file_level   │               │   level=tui_level   │
          │ +ExternalFilter     │               │  +ExternalFilter    │
          └──────────┬──────────┘               └──────────┬──────────┘
                     │                                      │
                     ▼                                      ▼
          ┌─────────────────────┐               ┌─────────────────────┐
          │ ProcessorFormatter  │               │ ProcessorFormatter  │
          │  + JSONRenderer     │               │ + ConsoleRenderer   │
          └──────────┬──────────┘               └──────────┬──────────┘
                     │                                      │
                     ▼                                      ▼
              JSON Lines File                         Rich TUI Output
```

### Log Record Lifecycle

1. **Creation**: Application calls `log.info("msg", key=value)`
2. **Binding**: structlog adds bound context (job, host)
3. **Processing**: Processor chain adds timestamp, formats exceptions
4. **Wrapping**: `wrap_for_formatter` prepares for stdlib
5. **Routing**: stdlib Logger dispatches to QueueHandler
6. **Queueing**: QueueHandler enqueues (non-blocking)
7. **Dispatch**: QueueListener dequeues in background thread
8. **Filtering**: Each handler applies level check + ExternalLibraryFilter
9. **Formatting**: ProcessorFormatter renders (JSON or Rich)
10. **Output**: Written to file or console

---

## Relationships

```
LogConfig ────1:N────► Handler (file, tui)
    │                      │
    │                      ├── level (from file/tui setting)
    │                      └── ExternalLibraryFilter (from external setting)
    │
    └── external ─────────► ExternalLibraryFilter.external_level

LogLevel ←──────────────── LogConfig.file, .tui, .external (values)

ProcessorChain ───────────► ProcessorFormatter (file handler)
                          └► ProcessorFormatter (tui handler)

LogContext ───────────────► Attached to LogRecord by processor
```

---

## Integration with Existing Models

### Modified Entities

| Entity | File | Change |
|--------|------|--------|
| `LogLevel` | `models.py` | Update values from 0-5 to 10-50 scale |
| `Configuration` | `config.py` | Add `logging: LogConfig` field |
| `LogEvent` | `events.py` | May be deprecated (replaced by stdlib LogRecord) |
| `Logger` | `logger.py` | Replace with structlog configuration |
| `FileLogger` | `logger.py` | Replace with ProcessorFormatter + JSONRenderer |
| `ConsoleLogger` | `logger.py` | Replace with ProcessorFormatter + ConsoleRenderer |

### Preserved Entities

| Entity | File | Reason |
|--------|------|--------|
| `EventBus` | `events.py` | Still used for ProgressEvent, ConnectionEvent (non-logging) |
| `ProgressEvent` | `events.py` | Unchanged - separate from logging |
| `ConnectionEvent` | `events.py` | Unchanged - separate from logging |
