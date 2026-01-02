# Logging System

This document defines the current authoritative specification for pc-switcher's logging system.

## User Scenarios & Testing

### User Story 1 - Comprehensive Logging System (LOG-US-SYSTEM)

The system implements a six-level logging hierarchy (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL) with independent level configuration for file logging, terminal UI display, and external library filtering. Log levels follow the ordering DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL: DEBUG is the most verbose and includes all messages, while FULL is a high-verbosity operational level that does NOT include DEBUG-level diagnostics. Logs are written to timestamped files in `~/.local/share/pc-switcher/logs/` on the source machine. All operations (core orchestrator, individual jobs, target-side scripts) contribute to the unified log stream.

Lineage: 001-US-4

**Independent Test**: Can be fully tested by:
1. Running sync with various log level configurations
2. Verifying file contains events at configured level and above
3. Confirming terminal shows only events at CLI log level and above
4. Checking log format includes timestamp, level, job name, and message
5. Validating that both source and target operations contribute to unified log

**Acceptance Scenarios**:

1. **Given** user configures `log.file: FULL` and `log.tui: INFO`, **When** sync runs and a job logs at DEBUG level, **Then** the message does NOT appear in the log file nor in the terminal UI (DEBUG is excluded by FULL)

2. **Given** user configures `log.file: INFO`, **When** sync runs and a job logs at FULL level (e.g., "Copying /home/user/file.txt"), **Then** the message does not appear in either log file or terminal UI

3. *(Removed)*

4. **Given** sync operation completes, **When** user inspects log file at `~/.local/share/pc-switcher/logs/sync-<timestamp>.log`, **Then** the file contains structured log entries in JSON Lines format (one JSON object per line) with fields: timestamp (ISO8601), level, job, host (enum: "source"/"target"), hostname (resolved machine name), event, plus additional context fields as needed, for all operations from both source and target machines

5. *(Removed - target-side logging is Job implementation detail, not a spec-level concern)*

6. **Given** user runs `pc-switcher logs --last`, **When** command executes, **Then** the system displays the most recent sync log file in the terminal using rich console with syntax highlighting for improved readability

---

### User Story 2 - Configure Log Levels in Config File (LOG-US-CONFIG)

As a pc-switcher user, I want to specify log levels in my configuration file so that I can control the verbosity of file output, TUI output, and external library noise independently.

Lineage: 004-US-1

**Independent Test**: Can be fully tested by creating a config file with specific log levels and verifying that log output respects those levels.

**Acceptance Scenarios**:

1. **Given** a config file with `log.file` level set to `DEBUG` and `log.tui` level set to `INFO`, **When** I run a sync, **Then** the log file contains debug messages but the TUI only shows info and above.
2. **Given** a config file with `log.external` level set to `WARNING`, **When** asyncssh logs an INFO message, **Then** that message is not displayed in TUI or written to the log file (regardless of `file`/`tui` settings).
3. **Given** `log.file: DEBUG`, `log.tui: INFO`, `log.external: WARNING`, **When** pcswitcher logs a DEBUG message, **Then** it appears in the file but not in the TUI.

---

### User Story 3 - View External Library Logs (LOG-US-EXTERNAL)

As a pc-switcher user, I want to see log messages from external libraries (e.g. asyncssh) in the same log output (file and TUI) so that I can diagnose connection or third-party issues.

Lineage: 004-US-2

**Independent Test**: Can be tested by triggering an SSH connection warning and verifying it appears in both file and TUI output when levels permit.

**Acceptance Scenarios**:

1. **Given** asyncssh emits a WARNING log, **When** `log.external` is set to WARNING or lower, **Then** the message appears in outputs that meet both the `external` level and their respective `file`/`tui` levels.
2. **Given** `log.external` is set to ERROR, **When** asyncssh emits a WARNING, **Then** the message does not appear in either file or TUI.
3. **Given** `log.external: INFO`, `log.file: DEBUG`, `log.tui: WARNING`, **When** asyncssh emits an INFO, **Then** it appears in the file but not in the TUI.

---

### User Story 4 - Migrate Internal Logging to Standard Library (LOG-US-STDLIB)

As a developer, I want pc-switcher's internal logging to use Python's standard `logging` module so that log level configuration per module, handler filtering, and log routing work consistently with external libraries.

Lineage: 004-US-3

**Independent Test**: Can be tested by verifying that all log messages from pcswitcher modules flow through the standard logging infrastructure and respect configured log levels.

**Acceptance Scenarios**:

1. **Given** the current custom `Logger` class is replaced with standard logging, **When** a module logs a message, **Then** it goes through Python's logging infrastructure.
2. **Given** a module is configured with a specific log level, **When** that module logs a message below its threshold, **Then** the message is filtered out.
3. **Given** different handlers (file, TUI) have different levels, **When** a message is logged, **Then** each handler applies its own filter.

---

### User Story 5 - Preserve Current Log Format and Features (LOG-US-PRESERVE)

As a pc-switcher user, I want the TUI and file log output to maintain the current format (timestamps, colors, structured context) so that the migration doesn't degrade my user experience.

Lineage: 004-US-4

**Independent Test**: Can be tested by comparing log output before and after migration for visual consistency.

**Acceptance Scenarios**:

1. **Given** a log event with structured context (e.g., `file=/path/to/file`), **When** written to file, **Then** it includes the same JSON structure as before.
2. **Given** a log event at ERROR level, **When** displayed in TUI, **Then** it has the same red coloring as before.
3. **Given** a log event with host/job context, **When** displayed, **Then** the format remains `HH:MM:SS [LEVEL   ] [job] (host) message`.

---

### Edge Cases

- What happens when the config file contains an invalid log level string?
  - System MUST fail with a configuration error (consistent with other config validation).
  - Lineage: 004-edge-cases

- What happens when a third-party library uses print() instead of logging?
  - Ignored. Well-maintained libraries use proper logging. No stdout/stderr capture (YAGNI for an interactive CLI tool).
  - Lineage: 004-edge-cases

- What happens when log levels are omitted from config?
  - System should use sensible defaults (file: DEBUG, tui: INFO, external: WARNING).
  - Lineage: 004-edge-cases

- What happens when log volume is very high (e.g., FULL level during large sync)?
  - Performance should not degrade significantly; the logging pipeline should remain async.
  - Lineage: 004-edge-cases

## Requirements

### Functional Requirements

#### Log Level Hierarchy

- **LOG-FR-HIERARCHY**: System MUST implement six log levels with the following ordering and semantics: DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL, where DEBUG is the most verbose. DEBUG includes all messages (FULL, INFO, WARNING, ERROR, CRITICAL, plus internal diagnostics). FULL includes all messages from INFO and below plus operational details, but excludes DEBUG-level internal diagnostics.
  Lineage: 001-FR-018 -> 004-FR-006

**Log Level Definitions** (from most to least verbose):
- **DEBUG**: Most verbose level for internal diagnostics, including command outputs, detailed timings, internal state transitions, and all messages from lower levels (FULL, INFO, WARNING, ERROR, CRITICAL). Intended for deep troubleshooting and development.
- **FULL**: High-verbosity operational details including file-level operations (e.g., "Copying /home/user/document.txt", "Created snapshot pre-@home-20251115T143022") and all messages from lower levels (INFO, WARNING, ERROR, CRITICAL). Excludes DEBUG-level internal diagnostics.
- **INFO**: High-level operation reporting for normal user visibility (e.g., "Starting job X", "Job X completed successfully", "Connection established") and all messages from lower levels (WARNING, ERROR, CRITICAL).
- **WARNING**: Unexpected conditions that should be reviewed but don't indicate failure (e.g., config value using deprecated format, unusually large transfer size) and all messages from lower levels (ERROR, CRITICAL).
- **ERROR**: Recoverable errors that may impact sync quality but don't require abort (e.g., individual file copy failed, optional feature unavailable) and CRITICAL messages.
- **CRITICAL**: Unrecoverable errors requiring immediate sync abort (e.g., snapshot creation failed, target unreachable mid-sync, data corruption detected). Triggered when jobs raise an unhandled exception.

#### Level Configuration

- **LOG-FR-FILE-LEVEL**: System MUST allow configuring the log level floor for file output (`log.file`).
  Lineage: 001-FR-020 -> 004-FR-001

- **LOG-FR-TUI-LEVEL**: System MUST allow configuring the log level floor for TUI output (`log.tui`).
  Lineage: 001-FR-020 -> 004-FR-002

- **LOG-FR-EXT-LEVEL**: System MUST allow configuring an additional log level floor for external libraries (`log.external`) that applies to both file and TUI output.
  Lineage: 004-FR-003

- **LOG-FR-DEFAULTS**: System MUST apply sensible defaults when log levels are not specified in config (file: DEBUG, tui: INFO, external: WARNING).
  Lineage: 004-FR-009

- **LOG-FR-INVALID**: System MUST fail with a configuration error when invalid log level strings are provided in config.
  Lineage: 004-FR-010

#### External Library Logging

- **LOG-FR-CAPTURE-EXT**: System MUST capture log messages from external libraries (asyncssh, etc.) and route them through the configured handlers.
  Lineage: 004-FR-004

#### Logging Infrastructure

- **LOG-FR-STDLIB**: System MUST use Python's standard `logging` module as the foundation for all logging.
  Lineage: 004-FR-005

- **LOG-FR-EXCEPTION**: When a job raises an exception, the orchestrator MUST log the error at CRITICAL level, request termination of the currently-executing job (queued jobs never execute and do not receive termination requests), and halt sync immediately.
  Lineage: 001-FR-019

#### Output Format

- **LOG-FR-FILE-PATH**: System MUST write all logs at configured file level or above to timestamped file in `~/.local/share/pc-switcher/logs/sync-<timestamp>.log`.
  Lineage: 001-FR-021

- **LOG-FR-JSON**: System MUST preserve the current log file format: JSON Lines format (one JSON object per line with keys: timestamp in ISO8601 format, level, job, host, hostname, event, plus any additional context fields) for machine-readability.
  Lineage: 001-FR-022 -> 004-FR-007

- **LOG-FR-TUI-FORMAT**: System MUST preserve the current TUI format (colored, timestamped, structured) for human-readability. Format: `HH:MM:SS [LEVEL   ] [job] (host) message`.
  Lineage: 001-FR-022 -> 004-FR-008

- **LOG-FR-CONTEXT**: System MUST preserve structured context (key=value pairs) in log output.
  Lineage: 004-FR-011

#### Log Aggregation

- **LOG-FR-AGGREGATE**: System MUST aggregate logs from both source-side orchestrator and target-side operations into unified log stream.
  Lineage: 001-FR-023

### Key Entities

- **LogConfig**: Configuration entity holding three log level settings: `file`, `tui`, `external`.
  Lineage: 004-entities

- **LogHandler**: Abstraction for output destinations (file, TUI) with their own level filtering.
  Lineage: 004-entities

- **LogRecord**: Standard Python logging record with additional pc-switcher context (host, job, structured data).
  Lineage: 004-entities

- **LogEntry**: Represents a logged event with timestamp, level, job name, message, and structured context data.
  Lineage: 001-entities

## Success Criteria

- **LOG-SC-CONFIG**: Users can configure all three log level settings (file, tui, external) through the config file without code changes.
  Lineage: 004-SC-001

- **LOG-SC-EXT-APPEAR**: Log messages from asyncssh and other external libraries appear in log output when their level meets both the `external` threshold and the destination's (`file`/`tui`) threshold.
  Lineage: 004-SC-002

- **LOG-SC-FILE-DEBUG**: Setting `log.file` to DEBUG and `log.tui` to INFO results in file containing debug messages that don't appear in TUI.
  Lineage: 004-SC-003

- **LOG-SC-EXT-FILTER**: Setting `log.external` to WARNING filters out INFO/DEBUG messages from external libraries regardless of `file`/`tui` settings.
  Lineage: 004-SC-004

- **LOG-SC-TUI-VISUAL**: TUI log output maintains identical visual format (colors, layout, timestamps) before and after migration.
  Lineage: 004-SC-005

- **LOG-SC-JSON-STRUCT**: File log output maintains identical JSON structure before and after migration.
  Lineage: 004-SC-006

- **LOG-SC-NO-REGRESS**: No regression in existing test suite after migration.
  Lineage: 004-SC-007

- **LOG-SC-INVALID-FAIL**: Invalid log level in config causes startup failure with clear error message (consistent with other config errors).
  Lineage: 004-SC-008
