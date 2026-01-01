# Logging Specification

**Domain**: Logging Infrastructure
**Source**: `specs/004-python-logging`

## Overview

This document specifies the logging infrastructure for pc-switcher, which uses Python's standard `logging` module to provide configurable, structured logging for both internal modules and external libraries.

## Requirements

### Configuration

- **LOG-FR-001**: System MUST allow configuring the log level floor for file output (`file`).
- **LOG-FR-002**: System MUST allow configuring the log level floor for TUI output (`tui`).
- **LOG-FR-003**: System MUST allow configuring an additional log level floor for external libraries (`external`) that applies to both file and TUI output.
- **LOG-FR-009**: System MUST apply sensible defaults when log levels are not specified in config (file: DEBUG, tui: INFO, external: WARNING).
- **LOG-FR-010**: System MUST fail with a configuration error when invalid log level strings are provided in config.

### Architecture

- **LOG-FR-004**: System MUST capture log messages from external libraries (asyncssh, etc.) and route them through the configured handlers.
- **LOG-FR-005**: System MUST use Python's standard `logging` module as the foundation for all logging.
- **LOG-FR-006**: System MUST maintain the six-level logging hierarchy (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL) with the custom FULL level between DEBUG and INFO.

### Output Format

- **LOG-FR-007**: System MUST preserve the current log file format (JSON lines) for machine-readability.
- **LOG-FR-008**: System MUST preserve the current TUI format (colored, timestamped, structured) for human-readability.
- **LOG-FR-011**: System MUST preserve structured context (key=value pairs) in log output.

## Data Model

### LogConfig

Configuration entity holding log level settings.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file` | `int` | `10` (DEBUG) | Floor log level for file output. |
| `tui` | `int` | `20` (INFO) | Floor log level for TUI output. |
| `external` | `int` | `30` (WARNING) | Additional floor for non-pcswitcher loggers. |

### LogLevel

Aligned with Python's standard `logging` module.

| Level | Value | Description |
|-------|-------|-------------|
| `DEBUG` | `10` | Internal diagnostics |
| `FULL` | `15` | Operational details (file-level) |
| `INFO` | `20` | High-level operations |
| `WARNING` | `30` | Unexpected but non-fatal |
| `ERROR` | `40` | Recoverable errors |
| `CRITICAL` | `50` | Unrecoverable, sync must abort |
