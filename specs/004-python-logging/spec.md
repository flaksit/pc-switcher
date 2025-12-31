# Feature Specification: Standard Python Logging Integration

**Feature Branch**: `004-python-logging`
**Created**: 2025-12-31
**Status**: Draft

**Input**: User description: "Migrate pc-switcher to use standard Python logging facility instead of custom logging infrastructure. Enable configurable log levels for pcswitcher modules and external libraries, with distinct settings for file and TUI handlers."

**Related Issues**: #102, #103, #104

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Configure Log Levels in Config File (Priority: P1)

As a pc-switcher user, I want to specify log levels in my configuration file so that I can control the verbosity of log output without modifying code.

**Why this priority**: This is the core configuration need that enables all other logging customization. Without config file support, users cannot customize log levels at all.

**Independent Test**: Can be fully tested by creating a config file with specific log levels and verifying that log output respects those levels.

**Acceptance Scenarios**:

1. **Given** a config file with `pcswitcher` log level set to `DEBUG`, **When** I run a sync, **Then** I see debug-level messages from pcswitcher modules in both file and TUI output.
2. **Given** a config file with `external` log level set to `WARNING`, **When** an external library (e.g., asyncssh) logs an INFO message, **Then** that message is not displayed or written to the log file.
3. **Given** a config file with different levels for file (`DEBUG`) and TUI (`INFO`), **When** I run a sync, **Then** the log file contains debug messages but the TUI only shows info and above.

---

### User Story 2 - View External Library Logs (Priority: P1)

As a pc-switcher user, I want to see log messages from external libraries (e.g. asyncssh) in the same log output (file and TUI) so that I can diagnose connection or third-party issues.

**Why this priority**: External library logs are essential for debugging SSH connection issues and other integration problems. This is equally critical as P1 since it's useless to configure levels if external logs aren't captured.

**Independent Test**: Can be tested by triggering an SSH connection warning and verifying it appears in both file and TUI output.

**Acceptance Scenarios**:

1. **Given** asyncssh emits a WARNING log, **When** the external log level is set to WARNING or lower, **Then** the message appears in the TUI and log file.
2. **Given** external log level is set to ERROR, **When** asyncssh emits a WARNING, **Then** the message does not appear in the TUI or log file.

---

### User Story 3 - Migrate Internal Logging to Standard Library (Priority: P2)

As a developer, I want pc-switcher's internal logging to use Python's standard `logging` module so that log level configuration per module, handler filtering, and log routing work consistently with external libraries.

**Why this priority**: This is the architectural foundation that enables the other stories to work. It's P2 because it's implementation-focused rather than directly user-facing.

**Independent Test**: Can be tested by verifying that all log messages from pcswitcher modules flow through the standard logging infrastructure and respect configured log levels.

**Acceptance Scenarios**:

1. **Given** the current custom `Logger` class is replaced with standard logging, **When** a module logs a message, **Then** it goes through Python's logging infrastructure.
2. **Given** a module is configured with a specific log level, **When** that module logs a message below its threshold, **Then** the message is filtered out.
3. **Given** different handlers (file, console/TUI) have different levels, **When** a message is logged, **Then** each handler applies its own filter.

---

### User Story 4 - Preserve Current Log Format and Features (Priority: P2)

As a pc-switcher user, I want the TUI and file log output to maintain the current format (timestamps, colors, structured context) so that the migration doesn't degrade my user experience.

**Why this priority**: Preserving existing UX is important but secondary to getting the core functionality working.

**Independent Test**: Can be tested by comparing log output before and after migration for visual consistency.

**Acceptance Scenarios**:

1. **Given** a log event with structured context (e.g., `file=/path/to/file`), **When** written to file, **Then** it includes the same JSON structure as before.
2. **Given** a log event at ERROR level, **When** displayed in TUI, **Then** it has the same red coloring as before.
3. **Given** a log event with host/job context, **When** displayed, **Then** the format remains `HH:MM:SS [LEVEL   ] [job] (host) message`.

---

### Edge Cases

- What happens when the config file contains an invalid log level string? System should log a warning and use a sensible default (INFO).
- What happens when a third-party library uses print() instead of logging? Print statements should be captured and included in log output.
- What happens when log levels are omitted from config? System should use sensible defaults (pcswitcher: INFO, external: WARNING, file: DEBUG, tui: INFO).
- What happens when log volume is very high (e.g., FULL level during large sync)? Performance should not degrade significantly; the logging pipeline should remain async.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST allow configuring log levels for the `pcswitcher` namespace in the config file.
- **FR-002**: System MUST allow configuring log levels for external libraries (non-pcswitcher) in the config file.
- **FR-003**: System MUST support distinct log levels for file output vs TUI output.
- **FR-004**: System MUST capture log messages from external libraries (asyncssh, etc.) and route them through the configured handlers.
- **FR-005**: System MUST use Python's standard `logging` module as the foundation for all logging.
- **FR-006**: System MUST maintain the six-level logging hierarchy (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL) with the custom FULL level between DEBUG and INFO.
- **FR-007**: System MUST preserve the current log file format (JSON lines) for machine-readability.
- **FR-008**: System MUST preserve the current TUI format (colored, timestamped, structured) for human-readability.
- **FR-009**: System MUST apply sensible defaults when log levels are not specified in config (pcswitcher: INFO, external: WARNING).
- **FR-010**: System MUST log a warning and use defaults when invalid log level strings are provided in config.
- **FR-011**: System MUST preserve structured context (key=value pairs) in log output.
- **FR-012**: System SHOULD capture print() statements and include them in log output.

### Key Entities

- **LogConfig**: Configuration entity holding log level settings for pcswitcher, external libraries, file handler, and TUI handler.
- **LogHandler**: Abstraction for output destinations (file, TUI) with their own level filtering.
- **LogRecord**: Standard Python logging record with additional pc-switcher context (host, job, structured data).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users can configure all four log level settings (pcswitcher, external, file, TUI) through the config file without code changes.
- **SC-002**: Log messages from asyncssh and other external libraries appear in log output when their level meets the configured threshold.
- **SC-003**: Setting file level to DEBUG and TUI level to INFO results in file containing debug messages that don't appear in TUI.
- **SC-004**: TUI log output maintains identical visual format (colors, layout, timestamps) before and after migration.
- **SC-005**: File log output maintains identical JSON structure before and after migration.
- **SC-006**: No regression in existing test suite after migration.
- **SC-007**: Invalid log level in config results in warning message and system continues with defaults.
