# ADR-010: Standard Library Logging Infrastructure

Status: Accepted
Date: 2025-12-31

## TL;DR

Use Python's standard `logging` module (without structlog) for all logging infrastructure, leveraging `QueueHandler`/`QueueListener` for async handling and custom formatters for JSON and Rich output.

## Implementation Rules

**Required patterns**:
- Register custom FULL level: `logging.addLevelName(15, "FULL")`
- Use `QueueHandler` + `QueueListener` for non-blocking log output
- Custom `logging.Formatter` for JSON lines file output
- Custom `logging.Formatter` using Rich for TUI output
- Configure logger hierarchy for 3-setting filtering (see below)

**Configuration model** (3 settings):
- `file`: Floor level for file output (default: DEBUG)
- `tui`: Floor level for TUI output (default: INFO)
- `external`: Additional floor for non-pcswitcher loggers (default: WARNING)

**Logger hierarchy setup**:
```python
logging.getLogger().setLevel(external)                    # Root filters external libs
logging.getLogger("pcswitcher").setLevel(min(file, tui))  # Let pcswitcher through
file_handler.setLevel(file)
tui_handler.setLevel(tui)
```

**Forbidden approaches**:
- Do not use structlog processors or formatters
- Do not use custom async queue implementations (use stdlib `QueueHandler`)
- Do not create separate logging pipelines for internal vs external logs

## Context

pc-switcher's custom logging infrastructure (~180 lines) needs migration to capture external library logs (asyncssh, etc.) while maintaining configurable levels per destination. Initial analysis suggested structlog, but deeper evaluation revealed it adds abstraction without proportional benefit for this use case.

See [considerations/adr-010-logging-infrastructure-analysis.md](considerations/adr-010-logging-infrastructure-analysis.md) for the full structlog vs stdlib comparison.

## Decision

- **Use stdlib `logging`** as the sole logging foundation
- **Use `QueueHandler` + `QueueListener`** for async, non-blocking output (replaces custom EventBus queue pattern)
- **Implement custom formatters** for JSON (file) and Rich (TUI) output
- **Use logger hierarchy** for 3-setting model (no custom Filter needed)

**Why not structlog**:
1. `ConsoleRenderer` doesn't match our TUI format - custom work needed anyway
2. `JSONRenderer` adds no value over `json.dumps(dict, default=str)`
3. External library capture is native stdlib capability
4. Processor chain abstraction adds complexity without proportional benefit
5. ~15 line difference doesn't justify added abstraction layer

## Consequences

**Positive**:
- Simpler mental model - standard Python logging patterns
- Direct format control for TUI output
- `QueueHandler`/`QueueListener` provides proven async handling
- Easier debugging - standard logging semantics
- No additional abstractions to understand
- Aligns with Deliberate Simplicity principle

**Negative**:
- Slightly more boilerplate than structlog (~105 vs ~90 lines)
- No built-in bound context (use `LoggerAdapter` or `extra` dict)
- structlog is already a dependency (remains unused for logging, may be used elsewhere)

## References

- [Logging Infrastructure Analysis](considerations/adr-010-logging-infrastructure-analysis.md) - Full comparison
- [Python Logging Cookbook](https://docs.python.org/3/howto/logging-cookbook.html) - QueueHandler pattern
- [Feature Spec 004-python-logging](../../specs/004-python-logging/spec.md)
