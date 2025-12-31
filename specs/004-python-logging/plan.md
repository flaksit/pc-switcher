# Implementation Plan: Standard Python Logging Integration

**Branch**: `004-python-logging` | **Date**: 2025-12-31 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/004-python-logging/spec.md`

## Summary

Migrate pc-switcher from its custom `Logger`/`EventBus` logging infrastructure to Python's standard `logging` module. The 3-setting model (`file`, `tui`, `external`) will control log level floors for file output, TUI output, and external library filtering. The migration uses `QueueHandler`/`QueueListener` for async handling and custom formatters for JSON and Rich output. The `QueueListener` lifecycle is managed via `atexit` handler to ensure log flushing on exit. See [ADR-010](../../docs/adr/adr-010-logging-infrastructure.md) for the decision rationale.

## Technical Context

**Language/Version**: Python 3.14
**Primary Dependencies**: stdlib logging, rich (14.2.0+), asyncssh (2.21.1+), typer (0.20.0+)
**Storage**: JSON lines log files in `~/.local/share/pc-switcher/logs/`
**Testing**: pytest + pytest-asyncio
**Target Platform**: Ubuntu 24.04 LTS
**Project Type**: Single CLI application
**Performance Goals**: Logging pipeline must remain async; high-volume FULL-level logging during large syncs must not degrade performance
**Constraints**: Custom FULL log level (value 15) between DEBUG (10) and INFO (20) must integrate with stdlib logging
**Scale/Scope**: Single-user CLI tool with 2-3 log handlers

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **Reliability Without Compromise**: ✅ No impact on data integrity. Logging is observability-only. Log file writes use append-mode with explicit flush to ensure durability. `QueueListener.stop()` called via `atexit` handler to flush remaining log records on exit. Invalid config causes startup failure (fail-fast).
- **Frictionless Command UX**: ✅ No change to CLI commands. Configuration adds 3 optional settings to existing config file. Sensible defaults (file: DEBUG, tui: INFO, external: WARNING) ensure zero-config works.
- **Well-supported tools and best practices**: ✅ Uses Python's stdlib `logging` module exclusively. `QueueHandler`/`QueueListener` is the recommended async pattern per Python Logging Cookbook. See [ADR-010](../../docs/adr/adr-010-logging-infrastructure.md).
- **Minimize SSD Wear**: ✅ No change to logging frequency or volume. Existing async queue-based write pattern preserved. File writes are append-only (no rewrites).
- **Throughput-Focused Syncing**: ✅ Logging overhead unchanged. Async handler pattern from existing implementation preserved.
- **Deliberate Simplicity**: ✅ Replaces ~180 lines of custom logging code with ~105 lines of stdlib logging. Simpler mental model (standard Python patterns, no custom filters). See [ADR-010 analysis](../../docs/adr/considerations/adr-010-logging-infrastructure-analysis.md).
- **Up-to-date Documentation**:
  - Update: `README.md` (configuration section)
  - New: ADR-010 (logging infrastructure decision)
  - New: docstrings in logging module

## Project Structure

### Documentation (this feature)

```text
specs/004-python-logging/
├── plan.md              # This file
├── research.md          # Phase 0: logging approach analysis
├── data-model.md        # Phase 1: LogConfig, handler, filter entities
├── quickstart.md        # Phase 1: Quick setup guide
└── tasks.md             # Phase 2 output (created by /speckit.tasks)
```

### Source Code (repository root)

```text
src/pcswitcher/
├── logger.py            # MODIFY: Replace custom Logger with stdlib logging setup
├── config.py            # MODIFY: Add logging section parsing (file/tui/external)
├── models.py            # MODIFY: Align LogLevel values with stdlib (10-50 scale)
├── events.py            # MODIFY: Deprecate LogEvent (replaced by LogRecord)
├── ui.py                # MODIFY: TerminalUI log consumption
├── orchestrator.py      # MODIFY: Logger instantiation
├── cli.py               # MODIFY: Logging setup on startup
└── schemas/
    └── config-schema.yaml  # MODIFY: Add logging config schema

tests/
├── unit/
│   └── test_logging.py  # NEW: Unit tests for logging setup and filtering
├── contract/
│   └── test_logging_contract.py  # NEW: Contract tests for log format
└── integration/
    └── test_logging_integration.py  # NEW: Integration tests with asyncssh
```

**Structure Decision**: Single CLI application. Logging changes primarily affect `logger.py` (major refactor), `config.py` (add settings), and `cli.py` (setup). No new modules required—migration is in-place.

## Complexity Tracking

> No Constitution Check violations. All principles satisfied.
