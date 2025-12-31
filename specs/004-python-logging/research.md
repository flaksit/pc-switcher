# Research: Logging Infrastructure for pc-switcher

**Date**: 2025-12-31
**Branch**: `004-python-logging`
**Context**: Evaluate logging approaches for pc-switcher's Python logging migration
**Related ADR**: [ADR-010](../../docs/adr/adr-010-logging-infrastructure.md)

## Decision

**Use Python's standard `logging` module (without structlog)**

## Rationale

After detailed analysis, stdlib-only provides the best balance for pc-switcher:

1. **Simpler mental model** - Standard Python logging patterns, no processor chain abstraction
2. **Direct format control** - Our TUI format `HH:MM:SS [LEVEL] [job] (host) message` doesn't match structlog's ConsoleRenderer defaults
3. **Same async benefit** - `QueueHandler`/`QueueListener` is stdlib - the main code reduction
4. **JSONRenderer adds no value** - Current `json.dumps(dict, default=str)` is equally simple
5. **Fewer abstractions to debug** - Standard logging semantics
6. **Deliberate Simplicity principle** - Constitution requires minimal components

## Alternatives Considered

### 1. structlog with stdlib Integration (Hybrid)

**What it would involve:**
- Use structlog's bound loggers and processor chain
- `ProcessorFormatter` to route stdlib logs through structlog processors
- `ConsoleRenderer` for Rich output, `JSONRenderer` for file output

**Rejected because:**
- `ConsoleRenderer` doesn't match our TUI format - custom work needed anyway
- `JSONRenderer` adds no value over `json.dumps`
- External library capture is native stdlib capability (not a structlog benefit)
- Processor chain abstraction adds complexity without proportional benefit
- ~30 line difference (90 vs 120) doesn't justify added abstraction layer

See [ADR-010 considerations](../../docs/adr/considerations/adr-010-logging-infrastructure-analysis.md) for full comparison.

### 2. Custom Implementation (Status Quo Evolution)

**What it would involve:**
- Extend current ~180-line EventBus/Logger pattern
- Add level filtering logic manually
- Keep separate from stdlib logging

**Rejected because:**
- Cannot easily capture stdlib logs from external libraries (asyncssh, etc.)
- Parallel infrastructure to maintain
- Doesn't leverage proven stdlib patterns (`QueueHandler`/`QueueListener`)

## Implementation Approach

### Key Patterns

#### 1. Custom FULL Level Registration

Register FULL=15 with stdlib before any logging configuration:

```python
import logging

FULL = 15
logging.addLevelName(FULL, "FULL")

# Add method to Logger class for convenience
def full(self, message, *args, **kwargs):
    if self.isEnabledFor(FULL):
        self._log(FULL, message, args, **kwargs)

logging.Logger.full = full
```

#### 2. QueueHandler + QueueListener for Async

Replace custom async queue with stdlib's proven implementation:

```python
from logging.handlers import QueueHandler, QueueListener
import queue

log_queue = queue.Queue()

# Application logs to QueueHandler (non-blocking)
queue_handler = QueueHandler(log_queue)
root_logger = logging.getLogger()
root_logger.addHandler(queue_handler)

# Listener runs in background thread, dispatches to handlers
listener = QueueListener(
    log_queue,
    file_handler,
    console_handler,
    respect_handler_level=True,
)
listener.start()
```

#### 3. Logger Hierarchy for 3-Setting Model

Use standard logger levels instead of custom filters:

```python
# Root logger filters external libraries
logging.getLogger().setLevel(external_level)

# pcswitcher logger allows through to both handlers
logging.getLogger("pcswitcher").setLevel(min(file_level, tui_level))

# Each handler applies its own level
file_handler.setLevel(file_level)
tui_handler.setLevel(tui_level)
```

This works because:
- External libs inherit root level → filtered by `external`
- pcswitcher overrides root → filtered only by handler levels
- No custom Filter needed

#### 4. JSON Formatter for File Output

```python
class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_dict = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "job": getattr(record, "job", "unknown"),
            "host": getattr(record, "host", "unknown"),
            "message": record.getMessage(),
        }
        # Add extra context (exclude standard LogRecord attributes)
        for key in ("file", "path", "subvolume", "bytes", "latency_ms"):
            if hasattr(record, key):
                log_dict[key] = getattr(record, key)
        return json.dumps(log_dict, default=str)
```

#### 5. Rich Formatter for TUI Output

```python
class RichFormatter(logging.Formatter):
    """Format log records with Rich markup for TUI display."""

    LEVEL_COLORS = {
        "DEBUG": "dim",
        "FULL": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold red",
    }

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        color = self.LEVEL_COLORS.get(record.levelname, "white")
        job = getattr(record, "job", "unknown")
        host = getattr(record, "host", "unknown")
        message = record.getMessage()

        # Build Rich-markup string
        parts = [
            f"[dim]{timestamp}[/dim]",
            f"[{color}][{record.levelname:8}][/{color}]",
            f"[blue][{job}][/blue]",
            f"[magenta]({host})[/magenta]",
            message,
        ]
        return " ".join(parts)
```

### Architecture Summary

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

## Lines of Code Estimate

| Component | Current | With stdlib |
|-----------|---------|-------------|
| Logger class + EventBus pattern | ~100 lines | LoggerAdapter or extra dict (~20 lines) |
| FileLogger (JSON) | ~50 lines | JsonFormatter (~25 lines) |
| ConsoleLogger (Rich) | ~60 lines | RichFormatter (~30 lines) |
| Async queue handling | ~40 lines | QueueHandler/QueueListener (~10 lines) |
| Setup/configuration | ~10 lines | ~20 lines |
| **Total** | **~180 lines** | **~105 lines** |

**Net reduction: ~75 lines** (42% reduction)

The main win is replacing the custom async queue pattern with stdlib's `QueueHandler`/`QueueListener`.

## References

- [ADR-010: Standard Library Logging Infrastructure](../../docs/adr/adr-010-logging-infrastructure.md)
- [Full Analysis](../../docs/adr/considerations/adr-010-logging-infrastructure-analysis.md)
- [Python Logging Cookbook](https://docs.python.org/3/howto/logging-cookbook.html) - QueueHandler/QueueListener pattern
- [logging.addLevelName](https://docs.python.org/3/library/logging.html#logging.addLevelName) - Custom log level registration
