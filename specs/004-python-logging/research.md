# Research: structlog vs stdlib Logging for pc-switcher

**Date**: 2025-12-31
**Branch**: `004-python-logging`
**Context**: Evaluate logging approaches for pc-switcher's Python logging migration

## Decision

**Use structlog with stdlib integration (hybrid approach)**

## Rationale

structlog provides significant value for pc-switcher's requirements while stdlib provides the foundation for external library integration. The hybrid approach delivers:

1. **Simpler structured logging** - JSON output via `JSONRenderer` without manual serialization
2. **Built-in Rich integration** - `ConsoleRenderer` automatically uses Rich for colored output when installed
3. **External library capture** - `ProcessorFormatter` routes stdlib logs through structlog processors
4. **Processor-based filtering** - Clean abstraction for 3-setting model (file/tui/external)
5. **Already approved** - Listed in ADR-003 as an approved library; already a dependency (v25.5.0)

## Alternatives Considered

### 1. stdlib-only Approach

**What it would involve:**
- Custom handlers with `logging.Filter` subclasses for the 3-setting model
- Manual JSON formatting in a custom `Formatter`
- Direct Rich integration for TUI output formatting
- Custom `addLevelName()` for FULL level

**Rejected because:**
- More boilerplate code (~50-100 more lines for equivalent functionality)
- No built-in structured context handling (must manually merge context dicts)
- Rich integration requires custom implementation vs. structlog's built-in support
- The current codebase already imports structlog in dependencies

### 2. Pure structlog (no stdlib)

**What it would involve:**
- Use `structlog.make_filtering_bound_logger()` for filtering
- Native `BytesLoggerFactory` for performance
- Ignore stdlib logging from external libraries

**Rejected because:**
- Cannot capture asyncssh/external library logs without stdlib integration
- Requirement FR-004 mandates capturing external library logs
- Would lose logs from any library using stdlib logging

### 3. Custom Implementation (status quo evolution)

**What it would involve:**
- Extend current ~200-line EventBus/Logger pattern
- Add level filtering logic manually
- Keep separate from stdlib logging

**Rejected because:**
- Already committed to migration in spec (FR-005)
- Current implementation doesn't capture external library logs
- Increases maintenance burden vs. leveraging well-tested library

## Implementation Approach

### Key Patterns to Use

#### 1. Custom FULL Level Registration

Register FULL=15 with stdlib before configuring structlog:

```python
import logging
logging.addLevelName(15, "FULL")
logging.FULL = 15  # type: ignore[attr-defined]
```

Then add a method to BoundLogger for FULL-level logging. structlog's `make_filtering_bound_logger()` accepts integer level values, so FULL=15 works automatically in filtering.

#### 2. Shared Processor Chain

Use identical processors for structlog and stdlib entries:

```python
shared_processors = [
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    add_job_and_host,  # Custom processor for pc-switcher context
]
```

Apply to both:
- structlog's processor chain (with `wrap_for_formatter` at end)
- `ProcessorFormatter`'s `foreign_pre_chain` (for stdlib entries)

#### 3. ProcessorFormatter for Dual Output

Configure two handlers, each with ProcessorFormatter but different renderers:

**File Handler (JSON Lines):**
```python
file_handler.setFormatter(ProcessorFormatter(
    processor=structlog.processors.JSONRenderer(),
    foreign_pre_chain=shared_processors,
))
```

**TUI Handler (Rich Console):**
```python
console_handler.setFormatter(ProcessorFormatter(
    processor=structlog.dev.ConsoleRenderer(colors=True),
    foreign_pre_chain=shared_processors,
))
```

#### 4. 3-Setting Filter Model

Implement as stdlib `logging.Filter` instances attached to handlers:

```python
class ExternalLibraryFilter(logging.Filter):
    """Applies external level floor to non-pcswitcher loggers."""

    def __init__(self, external_level: int):
        self.external_level = external_level

    def filter(self, record: logging.LogRecord) -> bool:
        # pcswitcher logs pass through (governed by handler level)
        if record.name.startswith("pcswitcher"):
            return True
        # External logs must meet external threshold
        return record.levelno >= self.external_level
```

Each handler has:
- Base level set to `file` or `tui` threshold
- `ExternalLibraryFilter` for additional external library filtering

#### 5. Async Queue Handler Pattern

Use stdlib's `QueueHandler` + `QueueListener` for non-blocking I/O:

```python
from logging.handlers import QueueHandler, QueueListener
import queue

log_queue = queue.Queue()

# Application uses QueueHandler (non-blocking)
queue_handler = QueueHandler(log_queue)
root_logger.addHandler(queue_handler)

# Listener runs in background thread with actual handlers
listener = QueueListener(
    log_queue,
    file_handler,  # ProcessorFormatter with JSONRenderer
    tui_handler,   # ProcessorFormatter with ConsoleRenderer
)
listener.start()
```

This preserves the current async queue pattern but uses stdlib's proven implementation.

#### 6. ConsoleRenderer Column Configuration

For custom TUI format `HH:MM:SS [LEVEL] [job] (host) message`:

```python
from structlog.dev import Column, KeyValueColumnFormatter

columns = [
    Column("timestamp", KeyValueColumnFormatter(
        key_style=None,
        value_style="dim",
        value_repr=lambda ts: ts.strftime("%H:%M:%S"),
    )),
    Column("level", ...),
    Column("job", KeyValueColumnFormatter(..., prefix="[", suffix="]")),
    Column("host", KeyValueColumnFormatter(..., prefix="(", suffix=")")),
    Column("event", ...),  # message
    Column("", ...),  # remaining context
]
```

### Architecture Summary

```
                    ┌─────────────────────────────────────────┐
                    │         Application Code                │
                    │  structlog.get_logger("pcswitcher.xxx") │
                    └───────────────────┬─────────────────────┘
                                        │
┌───────────────────────────────────────┼───────────────────────────────────────┐
│                                       ▼                                       │
│    ┌──────────────────────────────────────────────────────────────────────┐   │
│    │                    structlog Processor Chain                         │   │
│    │  [add_log_level, TimeStamper, add_job_host, wrap_for_formatter]     │   │
│    └───────────────────────────────────┬──────────────────────────────────┘   │
│                                        │                                       │
│                                        ▼                                       │
│                          ┌─────────────────────────┐                          │
│                          │  stdlib logging.Logger  │◄─── External libs        │
│                          │     (root logger)       │     (asyncssh, etc.)     │
│                          └───────────┬─────────────┘                          │
│                                      │                                         │
│                                      ▼                                         │
│                          ┌─────────────────────────┐                          │
│                          │     QueueHandler        │                          │
│                          │    (non-blocking)       │                          │
│                          └───────────┬─────────────┘                          │
│                                      │                                         │
│                                      ▼                                         │
│                          ┌─────────────────────────┐                          │
│                          │    QueueListener        │                          │
│                          │  (background thread)    │                          │
│                          └─────┬───────────┬───────┘                          │
│                                │           │                                   │
│              ┌─────────────────┴┐         ┌┴─────────────────┐                │
│              ▼                  │         │                  ▼                │
│    ┌──────────────────┐        │         │       ┌──────────────────┐        │
│    │  File Handler    │        │         │       │   TUI Handler    │        │
│    │ level=file_level │        │         │       │ level=tui_level  │        │
│    │+ExternalFilter   │        │         │       │+ExternalFilter   │        │
│    └────────┬─────────┘        │         │       └────────┬─────────┘        │
│             │                  │         │                │                   │
│             ▼                  │         │                ▼                   │
│    ┌──────────────────┐        │         │       ┌──────────────────┐        │
│    │ProcessorFormatter│        │         │       │ProcessorFormatter│        │
│    │ +JSONRenderer    │        │         │       │+ConsoleRenderer  │        │
│    └────────┬─────────┘        │         │       └────────┬─────────┘        │
│             │                  │         │                │                   │
│             ▼                  │         │                ▼                   │
│      JSON Lines File           │         │          Rich TUI Output          │
│                                │         │                                    │
└────────────────────────────────┴─────────┴────────────────────────────────────┘
```

## Technical Details

### Custom Level (FULL=15)

**stdlib approach:**
```python
logging.addLevelName(15, "FULL")
```

**structlog integration:**
- `make_filtering_bound_logger(min_level)` accepts integer values
- `min_level=15` filters out DEBUG (10) but allows FULL (15) and above
- No special configuration needed for structlog processors

### External Library Capture

Key insight from [structlog stdlib documentation](https://www.structlog.org/en/stable/standard-library.html): `ProcessorFormatter` with `foreign_pre_chain` processes stdlib `LogRecord` objects through structlog processors, giving them identical treatment to structlog-native logs.

Critical requirement: Use `wrap_for_formatter` (not `render_to_log_kwargs`) to prevent double-encoding.

### Async Handler Compatibility

[Python Logging Cookbook](https://docs.python.org/3/howto/logging-cookbook.html) confirms `QueueHandler` + `QueueListener` is the standard pattern for non-blocking logging in async applications. The `QueueListener` runs in a background thread, consuming from the queue and dispatching to handlers.

This replaces pc-switcher's current custom async queue implementation with proven stdlib machinery.

### Rich Console Output

[structlog ConsoleRenderer](https://www.structlog.org/en/stable/console-output.html) automatically uses Rich when installed (already a dependency). Custom column configuration allows matching the existing format exactly.

## Lines of Code Estimate

| Component | Current | With structlog |
|-----------|---------|----------------|
| Logger class + EventBus pattern | ~100 lines | Replaced by structlog config (~30 lines) |
| FileLogger (JSON serialization) | ~50 lines | ProcessorFormatter + JSONRenderer (~5 lines) |
| ConsoleLogger (Rich formatting) | ~60 lines | ProcessorFormatter + ConsoleRenderer (~10 lines) |
| Level filtering | N/A | ExternalLibraryFilter (~15 lines) |
| Async queue handling | ~40 lines | QueueHandler/QueueListener (~10 lines) |
| **Total** | **~200 lines** | **~70 lines** |

**Net reduction: ~130 lines** (65% reduction)

## Maintenance Burden Assessment

| Aspect | Custom Implementation | structlog Hybrid |
|--------|----------------------|------------------|
| JSON format consistency | Manual; error-prone | JSONRenderer handles edge cases |
| Rich integration | Manual styling code | Built-in with ConsoleRenderer |
| External library capture | Not supported | ProcessorFormatter handles it |
| Custom level (FULL) | Works with both | Works with both |
| Testing | Custom test fixtures | Standard stdlib logging testing |
| Upgrade path | N/A | Active maintenance (v25.5.0, 2025) |

**Conclusion**: structlog reduces maintenance burden by delegating formatting, serialization, and Rich integration to a well-maintained library while preserving full control over filtering logic.

## References

- [structlog Standard Library Integration](https://www.structlog.org/en/stable/standard-library.html) - ProcessorFormatter, foreign_pre_chain, wrap_for_formatter
- [structlog Console Output](https://www.structlog.org/en/stable/console-output.html) - ConsoleRenderer columns configuration
- [structlog Performance](https://www.structlog.org/en/stable/performance.html) - Async logging considerations
- [Python Logging Cookbook](https://docs.python.org/3/howto/logging-cookbook.html) - QueueHandler/QueueListener pattern
- [Python logging.addLevelName](https://docs.python.org/3/library/logging.html) - Custom log level registration
- [BetterStack structlog Guide](https://betterstack.com/community/guides/logging/structlog/) - Comprehensive structlog overview
- [structlog with stdlib Gist](https://gist.github.com/sandipb/7ff119559dc7cf481527e117aea97052) - Integration example
