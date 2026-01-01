# Logging Infrastructure Analysis: structlog vs stdlib

**Date**: 2025-12-31
**Context**: Feature 004-python-logging migration analysis
**Related ADR**: ADR-010

## Background

pc-switcher has a custom logging infrastructure (~180 lines) that needs migration to Python's standard logging module to:
1. Capture external library logs (asyncssh, etc.)
2. Support configurable log levels per destination (file, TUI)
3. Filter external library verbosity independently

## Current Implementation

```text
src/pcswitcher/logger.py (199 lines):
├── Logger class (~30 lines) - publishes to EventBus
├── FileLogger class (~50 lines) - JSON lines output
├── ConsoleLogger class (~60 lines) - Rich colored output
└── Helper functions (~20 lines)

src/pcswitcher/events.py:
├── LogEvent dataclass - log record with context
└── EventBus class - pub/sub with async queues
```

Key characteristics:
- Custom `LogLevel` IntEnum (values 0-5, including FULL between DEBUG and INFO)
- Async queue-based handlers for non-blocking I/O
- Rich-based TUI formatting: `HH:MM:SS [LEVEL] [job] (host) message context`
- JSON lines file format with structured context

## Options Evaluated

### Option A: structlog with stdlib Integration (Hybrid)

**Approach**: Use structlog's bound loggers and processor chain, with `ProcessorFormatter` to capture stdlib logs.

**Claimed benefits**:
1. Built-in Rich integration via `ConsoleRenderer`
2. `JSONRenderer` for file output
3. Bound loggers with context (`.bind(job="btrfs")`)
4. Processor chain abstraction

**Actual analysis**:

1. **Rich integration**: `ConsoleRenderer` has a different default format than our `HH:MM:SS [LEVEL] [job] (host) message`. Matching our format requires custom column configuration that's equally complex as our current code.

2. **JSONRenderer**: Current implementation is 2 lines:
   ```python
   json_line = json.dumps(event_dict, default=str)
   f.write(json_line + "\n")
   ```
   No simplification from structlog.

3. **Bound loggers**: Convenient but achievable with stdlib's `LoggerAdapter` or extra dict.

4. **External library capture**: This is stdlib's native capability. structlog's `ProcessorFormatter` just routes stdlib logs through structlog processors - not a unique benefit.

**Code estimate**: ~90 lines (vs. current ~180)

**Downsides**:
- Additional abstraction layer to understand and maintain
- Configuration complexity (processors, formatters, foreign_pre_chain)
- ConsoleRenderer customization needed for our format
- Dependency on structlog's API stability

### Option B: stdlib-only

**Approach**: Use Python's standard `logging` module directly with custom handlers/formatters.

**Components needed**:
1. Custom `Formatter` for JSON lines output
2. Custom `Formatter` for Rich TUI output (reuse existing logic)
3. `QueueHandler` + `QueueListener` for async (stdlib, not custom)
4. Logger hierarchy setup (root level = external, pcswitcher level = min(file, tui))
5. `logging.addLevelName(15, "FULL")` for custom level

**Code estimate**: ~105 lines

**Benefits**:
- Simpler mental model - standard Python patterns
- No additional abstraction layer
- Direct control over formatting
- `QueueHandler`/`QueueListener` is stdlib (the real async win)
- Easier to debug - standard logging semantics

**Downsides**:
- Slightly more boilerplate than structlog
- No built-in bound context (use `LoggerAdapter` or extra dict)

### Option C: Evolve Current Implementation

**Approach**: Keep custom Logger/EventBus, add external library capture.

**Rejected because**:
- Cannot easily capture stdlib logs from external libraries
- Parallel infrastructure to maintain
- Doesn't leverage proven stdlib patterns

## Code Comparison

### JSON File Handler

**Current (FileLogger, ~50 lines)**:
```python
class FileLogger:
    def __init__(self, log_file, level, queue, hostname_map):
        self._log_file = log_file
        # ... setup

    async def consume(self):
        with self._log_file.open("a") as f:
            while True:
                event = await self._queue.get()
                if event is None:
                    break
                if isinstance(event, LogEvent) and event.level >= self._level:
                    event_dict = event.to_dict()
                    event_dict["hostname"] = self._hostname_map.get(event.host)
                    json_line = json.dumps(event_dict, default=str)
                    f.write(json_line + "\n")
                    f.flush()
```

**stdlib-only (~25 lines)**:
```python
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_dict = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "job": getattr(record, "job", "unknown"),
            "host": getattr(record, "host", "unknown"),
            "message": record.getMessage(),
        }
        # Add extra context
        for key, value in record.__dict__.items():
            if key not in STANDARD_RECORD_ATTRS:
                log_dict[key] = value
        return json.dumps(log_dict, default=str)
```

**structlog (~10 lines config but similar complexity)**:
```python
file_handler.setFormatter(ProcessorFormatter(
    processor=structlog.processors.JSONRenderer(),
    foreign_pre_chain=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        add_job_and_host,  # Still need custom processor
    ],
))
```

### Console Handler

**Current (ConsoleLogger, ~60 lines)**: Custom Rich Text building with level colors, timestamp formatting.

**stdlib-only (~40 lines)**: Same logic in a `Formatter.format()` method.

**structlog**: `ConsoleRenderer` doesn't match our format. Would need custom column config that's equally complex.

### Async Queue Handling

**Current (~40 lines across files)**: Custom async queue in EventBus, async consume loops.

**Both stdlib and structlog**: Use `QueueHandler` + `QueueListener` (~10 lines):
```python
from logging.handlers import QueueHandler, QueueListener
import queue

log_queue = queue.Queue()
queue_handler = QueueHandler(log_queue)
root_logger.addHandler(queue_handler)

listener = QueueListener(log_queue, file_handler, console_handler)
listener.start()
```

This is the main code reduction - available regardless of structlog choice.

## Decision Matrix

| Criterion               | structlog                               | stdlib-only               |
|-------------------------|------------------------------------------|---------------------------|
| Lines of code           | ~90                                      | ~105                      |
| Mental model complexity | Higher (processors, formatters, chains) | Lower (standard patterns) |
| External lib capture    | Via ProcessorFormatter                   | Native                    |
| Custom format support   | Requires custom processors               | Direct control            |
| Async handling          | QueueHandler (stdlib)                    | QueueHandler (stdlib)     |
| JSON output             | JSONRenderer                             | json.dumps (~equal)       |
| Rich integration        | ConsoleRenderer (doesn't match format)   | Direct (current approach) |
| Debugging               | structlog abstractions                   | Standard logging          |
| Dependencies            | structlog (already installed)            | None additional           |
| Learning curve          | Processor chain concepts                 | Standard Python           |

## Recommendation

**stdlib-only** is the better choice for pc-switcher because:

1. **Simpler mental model**: Standard Python logging patterns, no processor chain abstraction
2. **Direct format control**: Our TUI format doesn't match structlog's ConsoleRenderer defaults
3. **Same async benefit**: `QueueHandler`/`QueueListener` is stdlib - the main win
4. **JSONRenderer adds no value**: Current `json.dumps` is equally simple
5. **Fewer abstractions to debug**: Standard logging semantics
6. **Deliberate Simplicity principle**: Constitution requires minimal components

The ~30 line difference (90 vs 120) doesn't justify the added abstraction layer.

## structlog Consideration for Future

structlog could be reconsidered if:
- We need complex log processing pipelines
- We adopt structured logging across many services
- We need advanced features like log sampling or rate limiting

For a single CLI tool with two output destinations, stdlib is sufficient.
