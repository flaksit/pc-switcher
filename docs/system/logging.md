# Logging System

This document defines the current authoritative specification for pc-switcher's logging system.

## User Scenarios & Testing

### Comprehensive Logging System (LOG-US-SYSTEM)

Lineage: 001-US-4

The system implements a six-level logging hierarchy (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL) with independent level configuration for file logging, terminal UI display, and external library filtering. Log levels follow the ordering DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL: DEBUG is the most verbose and includes all messages, while FULL is a high-verbosity operational level that does NOT include DEBUG-level diagnostics. Logs are written to timestamped files in `~/.local/share/pc-switcher/logs/` on the source machine. All operations (core orchestrator, individual jobs, target-side scripts) contribute to the unified log stream.

**Independent Test**: Can be fully tested by:
1. Running sync with various log level configurations
2. Verifying file contains events at configured level and above
3. Confirming terminal shows only events at CLI log level and above
4. Checking log format includes timestamp, level, job name, and message
5. Validating that both source and target operations contribute to unified log

**Acceptance Scenarios**:

1. **Given** user configures `logging.file: FULL` and `logging.tui: INFO`, **When** sync runs and a job logs at DEBUG level, **Then** the message does NOT appear in the log file nor in the terminal UI (DEBUG is excluded by FULL)

2. **Given** user configures `logging.file: INFO`, **When** sync runs and a job logs at FULL level (e.g., "Copying /home/user/file.txt"), **Then** the message does not appear in either log file or terminal UI

3. *(Removed)*

4. **Given** sync operation completes, **When** user inspects log file at `~/.local/share/pc-switcher/logs/sync-<timestamp>-<session_id>.log`, **Then** the file contains structured log entries in JSON Lines format (one JSON object per line) with fields: timestamp (ISO8601), level, event, plus `job` and `host` (`"source"`/`"target"`) when the call supplied them, plus additional context fields as needed, for all operations from both source and target machines. The hostname mapping (source hostname and target hostname) is logged once at session start, not in every entry.  
   Lineage: 001-US-4-AS-4 → #140

5. *(Removed - target-side logging is Job implementation detail, not a spec-level concern)*

6. **Given** user runs `pc-switcher logs --last`, **When** command executes, **Then** the system displays the most recent sync log file in the terminal using rich console with syntax highlighting for improved readability

### Configure Log Levels in Config File (LOG-US-CONFIG)

Lineage: 004-US-1

As a pc-switcher user, I want to specify log levels in my configuration file so that I can control the verbosity of file output, TUI output, and external library noise independently.

**Independent Test**: Can be fully tested by creating a config file with specific log levels and verifying that log output respects those levels.

**Acceptance Scenarios**:

1. **Given** a config file with `logging.file` level set to `DEBUG` and `logging.tui` level set to `INFO`, **When** I run a sync, **Then** the log file contains debug messages but the TUI only shows info and above.
2. **Given** a config file with `logging.external` level set to `WARNING`, **When** asyncssh logs an INFO message, **Then** that message is not displayed in TUI or written to the log file (regardless of `file`/`tui` settings).
3. **Given** `logging.file: DEBUG`, `logging.tui: INFO`, `logging.external: WARNING`, **When** pcswitcher logs a DEBUG message, **Then** it appears in the file but not in the TUI.

### View External Library Logs (LOG-US-EXTERNAL)

Lineage: 004-US-2

As a pc-switcher user, I want to see log messages from external libraries (e.g. asyncssh) in the same log output (file and TUI) so that I can diagnose connection or third-party issues.

**Independent Test**: Can be tested by triggering an SSH connection warning and verifying it appears in both file and TUI output when levels permit.

**Acceptance Scenarios**:

1. **Given** asyncssh emits a WARNING log, **When** `logging.external` is set to WARNING or lower, **Then** the message appears in outputs that meet both the `external` level and their respective `file`/`tui` levels.
2. **Given** `logging.external` is set to ERROR, **When** asyncssh emits a WARNING, **Then** the message does not appear in either file or TUI.
3. **Given** `logging.external: INFO`, `logging.file: DEBUG`, `logging.tui: WARNING`, **When** asyncssh emits an INFO, **Then** it appears in the file but not in the TUI.

### Migrate Internal Logging to Standard Library (LOG-US-STDLIB)

Lineage: 004-US-3

As a developer, I want pc-switcher's internal logging to use Python's standard `logging` module so that log level configuration per module, handler filtering, and log routing work consistently with external libraries.

**Independent Test**: Can be tested by verifying that all log messages from pcswitcher modules flow through the standard logging infrastructure and respect configured log levels.

**Acceptance Scenarios**:

1. **Given** a module holds a `logging.getLogger("pcswitcher...")`, **When** it logs a message, **Then** the message goes through Python's logging infrastructure — there is no pc-switcher logger class.
2. **Given** a module is configured with a specific log level, **When** that module logs a message below its threshold, **Then** the message is filtered out.
3. **Given** different handlers (file, TUI) have different levels, **When** a message is logged, **Then** each handler applies its own filter.

### Log Format and Features (LOG-US-PRESERVE)

Lineage: 004-US-4

As a pc-switcher user, I want TUI and file log output to carry timestamps, colours and structured context so that I can read a run without opening the JSON file.

**Independent Test**: Can be tested by running a sync and inspecting one terminal line and one file line for the fields below.

**Acceptance Scenarios**:

1. **Given** a log event with structured context (e.g., `file=/path/to/file`), **When** written to file, **Then** it includes the same JSON structure as before.
2. **Given** a log event at ERROR level, **When** displayed on the stderr fallback handler, **Then** it is coloured red.
3. **Given** a log event with host/job context, **When** displayed, **Then** the format is `HH:MM:SS [LEVEL   ] [job] (host) message key=value` on the stderr fallback and `HH:MM:SS [LEVEL] [job] (host) message` in the interactive UI's log panel.

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
- **INFO**: High-level operation reporting for normal user visibility (e.g., "Job X started", "Job X completed successfully", "Connection established") and all messages from lower levels (WARNING, ERROR, CRITICAL).
- **WARNING**: Unexpected conditions that should be reviewed but don't indicate failure (e.g., config value using deprecated format, unusually large transfer size) and all messages from lower levels (ERROR, CRITICAL).
- **ERROR**: Recoverable errors that may impact sync quality but don't require abort (e.g., individual file copy failed, optional feature unavailable) and CRITICAL messages.
- **CRITICAL**: Unrecoverable errors requiring immediate sync abort (e.g., snapshot creation failed, target unreachable mid-sync, data corruption detected). Triggered when jobs raise an unhandled exception.

#### Level Configuration

- **LOG-FR-FILE-LEVEL**: System MUST allow configuring the log level floor for file output (`logging.file`).  
  Lineage: 001-FR-020 -> 004-FR-001

- **LOG-FR-TUI-LEVEL**: System MUST allow configuring the log level floor for TUI output (`logging.tui`).  
  Lineage: 001-FR-020 -> 004-FR-002

- **LOG-FR-EXT-LEVEL**: System MUST allow configuring an additional log level floor for external libraries (`logging.external`) that applies to both file and TUI output.  
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

- **LOG-FR-EXCEPTION**: When a job raises an exception, the orchestrator MUST log the error at CRITICAL level with the job's name, record a FAILED `JobResult`, request termination of the currently-executing job (queued jobs never execute and do not receive termination requests), and halt sync immediately. These exceptions are excluded from that halt, each reported exactly once:
  - `PackageItemFailures` and `ProbeFailed` — logged CRITICAL and recorded FAILED as above, but NOT re-raised: each package job reviews and applies its own work, so one manager's failed items say nothing about another's already-approved work, and the remaining jobs still run.
  - `JobSkipped` — logged at WARNING, recorded SKIPPED, not re-raised.
  - `SyncAborted` (including its `SyncAbortedByUser` subclass) and `SyncLockedError` — passed through untouched to `Orchestrator.run()`, which logs each once at WARNING (never CRITICAL) and re-raises for the CLI. The abort line says "by user" only for the subclass.

  Lineage: 001-FR-019

#### Output Format

- **LOG-FR-FILE-PATH**: System MUST write all logs at configured file level or above to a per-session file at `~/.local/share/pc-switcher/logs/sync-<timestamp>-<session_id>.log`.  
  Lineage: 001-FR-021

- **LOG-FR-JSON**: System MUST preserve the current log file format: JSON Lines format (one JSON object per line with keys: timestamp in ISO8601 format, level, event, plus any additional context fields) for machine-readability. `job` and `host` are emitted when the call supplied them and omitted when it did not (e.g. during startup and shutdown). ~~hostname~~ removed from per-entry requirements; see LOG-FR-SESSION-HOSTNAMES.  
  Lineage: 001-FR-022 → 004-FR-007 → #140

- **LOG-FR-SESSION-HOSTNAMES**: System MUST log the source and target hostnames at session start, establishing the mapping between host roles ("source"/"target") and actual machine names.  
  Lineage: #140

- **LOG-FR-TUI-FORMAT**: System MUST render each record for the terminal as `HH:MM:SS [LEVEL   ] [job] (host) message key=value`, level-coloured, on the stderr handler used for non-interactive runs. In an interactive run records go to the UI's log panel instead, as plain text (`HH:MM:SS [LEVEL] [job] (host) message`) with no ANSI codes and no Rich markup, so arbitrary message content can neither render as markup nor corrupt the Live display.  
  Lineage: 001-FR-022 -> 004-FR-008

- **LOG-FR-WARNING-RESURFACE**: In an interactive run the system MUST capture every `>=WARNING` record into a persistent buffer independently of the TUI level floor, backing a live counter in the status bar and a summary reprinted into scrollback once the Live display stops. Without it a warning is overwritten in the rolling panel within a few frames and never read.

- **LOG-FR-CONTEXT**: System MUST preserve structured context (key=value pairs) in log output.  
  Lineage: 004-FR-011

- **LOG-FR-CREDENTIAL-REDACTION**: System MUST withhold the userinfo component of every absolute URL from a log record's message, its formatting arguments and its structured context, applied once per record before any formatter sees it (`logger.CredentialRedactionFilter`, installed on both `QueueHandler`s). Three routes never become log records and carry the same rule at their own point: the `--confirm-each-command` confirmation (`executor._announce`), everything a review shows while the user decides, including the files it prints whole (`jobs.packages.review.ReviewEntry`), and the snippet bodies the registry-overwrite question displays (`jobs.packages.unreproducible.UnreproducibleSyncJob._render_overwrite_diff`), which are redacted where they are rendered and nowhere else — a snippet is stored and replayed verbatim.  
  Lineage: ADR-021, `PKG-FR-CREDENTIAL-PRIVACY`

#### Log Aggregation

- **LOG-FR-AGGREGATE**: System MUST aggregate logs from both source-side orchestrator and target-side operations into unified log stream.  
  Lineage: 001-FR-023

### Key Entities

All of these live in `src/pcswitcher/logger.py` unless another module is named.

- **LogConfig** (`config.py`): the three log level settings `file`, `tui`, `external`, parsed from the config file's `logging:` section.  
  Lineage: 004-entities

- **LogRecord**: standard Python logging record, carrying pc-switcher context (`job`, `host`, arbitrary structured data) in its `extra` dict.  
  Lineage: 004-entities

- **Queue pipeline**: one `Queue` plus a `QueueListener` on a background thread, fed by a `QueueHandler` on the `pcswitcher` logger (`propagate=False`, so the external floor never applies to it) and a second on the root logger (external libraries only). This is what keeps a log call from blocking on I/O.  
  Lineage: 004-entities

- **Handlers**: `FileHandler` + `JsonFormatter` at the `file` floor; at the `tui` floor either `UILogHandler` (interactive: hands each line to the UI's log panel on the event loop) or `StreamHandler(stderr)` + `RichFormatter`; and, interactive only, `WarningCaptureHandler` pinned to WARNING. `respect_handler_level=True` makes each apply its own floor.  
  Lineage: 004-entities

- **CredentialRedactionFilter**: installed on both `QueueHandler`s; see LOG-FR-CREDENTIAL-REDACTION.  
  Lineage: ADR-021

## Success Criteria

- **LOG-SC-CONFIG**: Users can configure all three log level settings (file, tui, external) through the config file without code changes.  
  Lineage: 004-SC-001

- **LOG-SC-EXT-APPEAR**: Log messages from asyncssh and other external libraries appear in log output when their level meets both the `external` threshold and the destination's (`file`/`tui`) threshold.  
  Lineage: 004-SC-002

- **LOG-SC-FILE-DEBUG**: Setting `logging.file` to DEBUG and `logging.tui` to INFO results in file containing debug messages that don't appear in TUI.  
  Lineage: 004-SC-003

- **LOG-SC-EXT-FILTER**: Setting `logging.external` to WARNING filters out INFO/DEBUG messages from external libraries regardless of `file`/`tui` settings.  
  Lineage: 004-SC-004

- **LOG-SC-TUI-VISUAL**: TUI log output carries the colours, layout and timestamps LOG-FR-TUI-FORMAT specifies, in both the interactive and the stderr path.  
  Lineage: 004-SC-005

- **LOG-SC-JSON-STRUCT**: Every file log line parses as one JSON object with the LOG-FR-JSON keys.  
  Lineage: 004-SC-006

- **LOG-SC-INVALID-FAIL**: Invalid log level in config causes startup failure with clear error message (consistent with other config errors).  
  Lineage: 004-SC-008
