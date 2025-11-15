# Feature Specification: Basic CLI & Infrastructure

**Feature Branch**: `001-cli-infrastructure`
**Created**: 2025-11-13
**Status**: Draft
**Input**: User description: "Basic CLI & Infrastructure - Command parser, config system, SSH connection, logging, terminal UI skeleton, architecture for modular features"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Modular Architecture for Feature Extensions (Priority: P1)

The system provides a plugin/module architecture that allows each sync feature (user data, packages, Docker, VMs, k3s, etc.) to be implemented as independent, self-contained modules that can be enabled/disabled, tested in isolation, and developed independently.

**Why this priority**: This is P1 because without it, all subsequent features would be tightly coupled, making development, testing, and maintenance extremely difficult. The architecture must be established before implementing any feature-specific sync logic.

**Independent Test**: Can be fully tested by:
1. Creating a minimal "hello world" sync module
2. Registering it with the system
3. Running sync with that module enabled
4. Verifying the module's execute method is called
5. Disabling the module and confirming it's skipped

This delivers value by establishing the pattern that all other features will follow.

**Constitution Alignment**:
- Deliberate Simplicity (modules encapsulate complexity)
- Reliability Without Compromise (modules can be tested independently)
- Proven Tooling Only (modules use standard interfaces)

**Acceptance Scenarios**:

1. **Given** a new sync module is defined with required interface methods (validate, sync, rollback), **When** the system loads modules at startup, **Then** the module is registered and available for execution during sync operations

2. **Given** operator disables a specific module via config (`sync_modules: { docker: false }`), **When** running sync, **Then** the system skips that module entirely, logs "Docker sync disabled by configuration", and continues with other enabled modules

3. **Given** a module's validate method returns errors (e.g., "Docker containers are running"), **When** the validation phase executes, **Then** the system collects all validation errors, displays them to the operator, and refuses to proceed with sync until issues are resolved

4. **Given** modules have dependencies (e.g., "user data" must complete before "Docker"), **When** the system plans sync operations, **Then** it executes modules in correct dependency order automatically based on their declared dependencies

---

### User Story 2 - Single Command Sync Execution (Priority: P1)

An operator initiates a full system sync from their current machine (source) to another machine (target) using a single terminal command. The system parses the command, validates the configuration, establishes an SSH connection to the target, and prepares the infrastructure for executing sync operations while providing clear terminal feedback throughout.

**Why this priority**: This is the core user interaction for the entire PC-switcher system. Without this foundational workflow, no sync operations can occur. It directly enables the "single command to launch entire sync process" requirement from the high-level vision.

**Dependencies**: Requires User Story 1 (Modular Architecture) to be implemented first, as the sync workflow hands off to feature modules.

**Independent Test**: Can be fully tested by running the main command and verifying that:
1. The command is parsed correctly
2. Configuration is loaded without errors
3. SSH connection to target is established successfully
4. Terminal displays connection status and readiness
5. The system is ready to hand off to feature-specific sync modules

This delivers immediate value by proving the basic infrastructure works end-to-end.

**Constitution Alignment**:
- Frictionless Command UX (single command workflow)
- Reliability Without Compromise (connection validation before proceeding)
- Deliberate Simplicity (straightforward command invocation)

**Acceptance Scenarios**:

1. **Given** both machines are on the same LAN, **When** user runs `pc-switcher sync <target>` from the source machine (where `<target>` is a simple IP address, hostname or SSH config alias), **Then** the system parses the command, establishes SSH to target using standard SSH semantics, displays connection status, and executes all configured sync operations in sequence
2. **Given** a sync operation is in progress, **When** user presses Ctrl+C, **Then** system gracefully stops current operation and reports status

---

### User Story 3 - Dummy Test Feature Modules (Priority: P1)

Two dummy feature modules ("dummy-success" and "dummy-fail") exist that simulate a realistic sync operation by performing timed operations on both source and target machines with regular progress updates. These modules serve as testing infrastructure for validating terminal UI progress feedback and will be reusable for testing other user stories.

**Why this priority**: This is P1 because it's essential testing infrastructure. User Story 4 (Terminal UI Progress Feedback) depends on having an actual module that produces progress events to display. Without this, we cannot properly test the terminal UI or demonstrate the modular feature architecture.

**Dependencies**: Requires User Story 1 (Modular Architecture).

**Independent Test**: Can be fully tested by:
1. Running the sync command with the dummy module enabled
2. Verifying operations execute on source machine for expected duration
3. Verifying operations execute on target machine for expected duration
4. Confirming log messages are produced at expected intervals
5. Verifying successful completion

This delivers value by enabling testing of progress UI and providing a reference implementation for the modular feature architecture.

**Constitution Alignment**:
- Deliberate Simplicity (provides clear example of module structure)
- Reliability Without Compromise (enables thorough testing of infrastructure)

**Acceptance Scenarios**:

1. **Given** a dummy module is enabled, **When** sync operation runs, **Then** the module performs busy waiting on the source machine for 10 seconds, outputting a log message every 2 seconds (5 messages total). One of these messages is a WARNING log at the 6-second mark.

2. **Given** a dummy module completes source operations, **When** it begins target operations, **Then** the module performs busy waiting on the target machine for 10 seconds, outputting a log message every 2 seconds (5 messages total). One of these messages is an ERROR log at the 8-second mark.

3. **Given** the dummy-success module executes, **When** all operations complete, **Then** the module reports successful completion with appropriate status

4. **Given** the dummy-fail module executes, **When** all operations complete, **Then** the module reports failure with appropriate status

5. **Given** a dummy module is running, **When** operations are in progress, **Then** the module emits progress events that other components (logging, terminal UI) can consume

6. **Given** a dummy module is running, **When** user presses Ctrl+C, **Then** the module stops current operation and reports status
---

### User Story 4 - Terminal UI Progress Feedback (Priority: P1)

During sync operations, the terminal displays real-time progress information including current operation, percentage complete, estimated time remaining, and success/error status using a clean, text-based interface.

**Why this priority**: This is P1 because operator experience would be poor without progress feedback. For a tool that may run for several minutes, progress feedback is essential for usability and to reduce uncertainty about whether the tool is working.

**Dependencies**: Requires User Story 1 (Modular Architecture) and User Story 3 (Dummy Test Feature Modules) for testing.

**Independent Test**: Can be fully tested by:
1. Running sync operations with the dummy modules
2. Observing terminal output shows progress indicators
3. Testing with different terminal widths

This delivers value by keeping operators informed during long-running syncs and reducing anxiety about whether the tool is working.

**Constitution Alignment**:
- Frictionless Command UX (clear feedback reduces cognitive load)
- Reliability Without Compromise (clear status helps detect stalls or issues)

**Acceptance Scenarios**:

1. **Given** a sync operation is in progress, **When** the system begins each major phase (connecting, validating, syncing user data, syncing packages, etc.), **Then** the terminal displays the phase name and updates a progress indicator showing X/Y steps completed

2. **Given** a long-running operation is executing, **When** no progress updates are available, **Then** the system displays a spinner or heartbeat indicator (updating every 2-3 seconds) to show the process hasn't hung

3. **Given** an operation completes successfully, **When** the terminal updates, **Then** it displays a clear success indicator (e.g., "✓ User data sync completed in 45s") and moves to the next operation

4. **Given** sync is running, **When** multiple operations execute, **Then** terminal displays current operation, completed operations, and remaining operations

5. **Given** an operation fails, **When** error occurs, **Then** terminal shows error message with context and suggests next steps

6. **Given** sync is complete, **When** all operations finish, **Then** terminal displays summary of what was synced and any warnings

---

### User Story 5 - Detailed Operation Logging (Priority: P2)

A user or administrator wants to review detailed logs of sync operations for auditing, troubleshooting, or understanding what changed. Detailed logging is always written to persistent log files with complete operation history, independent of terminal UI output.

**Why this priority**: While not immediately blocking basic functionality, logging is critical for the "reliability" principle and for debugging in early development. It's P2 because basic sync can work without perfect logging, but troubleshooting would be nearly impossible.

**Dependencies**: Requires User Story 1 (Modular Architecture).

**Independent Test**: Can be fully tested by:
1. Running various commands (successful and failing)
2. Checking log files contain expected entries
3. Verifying different log levels filter appropriately
4. Confirming timestamps and structured format

This delivers value by enabling developers and operators to diagnose problems and verify system behavior.

**Constitution Alignment**:
- Reliability Without Compromise (audit trail for all operations)
- Documentation As Runtime Contract (logs document actual runtime behavior)

**Acceptance Scenarios**:

1. **Given** sync operation completes, **When** user runs `pc-switcher logs`, **Then** system displays detailed log of last sync with timestamps
2. **Given** sync operations have occurred, **When** user runs `pc-switcher logs --date <date>`, **Then** system displays logs for specified date
3. **Given** logs are enabled, **When** any operation executes, **Then** system logs operation details, parameters, and outcomes to persistent log file with full detail regardless of terminal UI settings

---

### User Story 6 - Configuration Management (Priority: P3)

A user wants to configure sync behavior (exclusions, log levels, module selection) without editing raw config files.

**Why this priority**: Enables customization but basic sync can work with reasonable defaults. Can be deferred after core functionality is proven.

**Dependencies**: Requires User Story 1 (Modular Architecture).

**Independent Test**: Can be tested by running config commands and verifying they update configuration correctly and are reflected in subsequent sync operations.

**Constitution Alignment**: Frictionless Command UX, Deliberate Simplicity

**Acceptance Scenarios**:

1. **Given** user wants to view current config, **When** user runs `pc-switcher config show`, **Then** system displays current configuration in readable format
2. **Given** user wants to set sync options, **When** user runs `pc-switcher config set <key> <value>`, **Then** system updates configuration (e.g., log level, enabled modules)

---

### Edge Cases

- What happens when user specifies an invalid or non-existent SSH target?
- What happens when target machine is unreachable or SSH connection fails during sync?
- How does system handle corrupted or invalid configuration files?
- What happens when user runs multiple sync commands concurrently?
  - System should detect concurrent execution via some locking mechanism on both source and target, display error message, and refuse to proceed to prevent data corruption
- How does system handle log file rotation and disk space management?
- How does system handle SSH key authentication failures or permission issues?
- What happens when network connection drops mid-sync?
- What happens when operator presses Ctrl+C during sync?
  - System should catch the interrupt signal, display "Sync interrupted by operator", perform graceful cleanup (close SSH connections, flush logs), and exit with non-zero status code

## Requirements *(mandatory)*

> Tag each requirement with the principle(s) it enforces using square brackets (e.g., **FR-001** `[Reliability Without Compromise]`).

### Functional Requirements

- **FR-001** `[Frictionless Command UX]`: System MUST provide a single top-level command `pc-switcher sync` that accepts a target machine identifier and initiates the complete sync workflow without requiring additional commands or manual steps
- **FR-002** `[Frictionless Command UX]`: System MUST parse command-line arguments including target specification (using standard SSH syntax: `hostname` or SSH config alias), sync options, and operation modes
- **FR-003** `[Frictionless Command UX]`: System MUST load sync behavior configuration from a config file in user space for settings like exclusions, log levels, and module selection
- **FR-004** `[Proven Tooling Only]`: System MUST establish SSH connections to target machines using standard SSH protocol, respecting user's `~/.ssh/config` and standard SSH authentication mechanisms
- **FR-005** `[Reliability Without Compromise]`: System MUST verify SSH connection and target machine availability before beginning sync operations
- **FR-006** `[Documentation As Runtime Contract]`: System MUST log all operations, errors, and state changes to persistent log files with timestamps
- **FR-007** `[Reliability Without Compromise]`: System MUST support multiple log levels (debug, info, warning, error) and allow users to configure log verbosity
- **FR-008** `[Frictionless Command UX]`: System MUST display real-time progress in terminal UI including current operation, progress indicators, and error messages
- **FR-009** `[Deliberate Simplicity]`: System MUST provide an architecture where sync features (packages, docker, VMs, etc.) can be implemented as pluggable modules
- **FR-010** `[Deliberate Simplicity]`: System MUST allow modules to register their sync operations, validation steps, and rollback handlers with the core orchestrator
- **FR-011** `[Frictionless Command UX]`: System MUST handle common errors gracefully with clear error messages suggesting remediation steps
- **FR-012** `[Reliability Without Compromise]`: System MUST validate configuration on startup and report configuration errors before attempting sync
- **FR-013** `[Proven Tooling Only]`: System MUST use well-established configuration formats (YAML or TOML) for human-readable config files
- **FR-014** `[Documentation As Runtime Contract]`: System MUST provide command-line help text and usage examples for all commands

### Key Entities

- **SSH Target**: Runtime representation of the target machine derived from command-line argument and SSH configuration (hostname, resolved user, connection details)
- **Feature Module**: Represents a pluggable sync component that handles a specific aspect of system state (packages, docker, VMs) with its own sync logic, validation, and rollback capabilities
- **Sync Operation**: Represents a discrete sync task performed by a module (e.g., sync packages, sync docker) including status, progress, and error information
- **Log Entry**: Represents a logged event including timestamp, severity level, source module, message, and context data

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** `[Frictionless Command UX]`: User can execute complete sync workflow with a single command without additional manual intervention
- **SC-002** `[Reliability Without Compromise]`: System establishes SSH connection to target machine within 5 seconds on local LAN
- **SC-003** `[Frictionless Command UX]`: Terminal UI updates progress display within 1 second of operation state changes
- **SC-004** `[Reliability Without Compromise]`: System correctly validates configuration and reports all configuration errors before attempting sync in 100% of test cases
- **SC-005** `[Documentation As Runtime Contract]`: System logs all operations with timestamps and sufficient detail to reconstruct what occurred during sync
- **SC-006** `[Deliberate Simplicity]`: New sync features can be added as modules without modifying core orchestration code
- **SC-007** `[Reliability Without Compromise]`: System handles SSH connection failures, network interruptions, and invalid configurations with clear error messages and graceful degradation
- **SC-008** `[Frictionless Command UX]`: User can view sync status, logs, and configuration through CLI commands without accessing raw files

## Assumptions

- SSH key-based authentication is already configured between machines (not managed by this system)
- Users configure target machine connection details (hostnames, ports, keys, etc.) in their standard `~/.ssh/config` file, not in pc-switcher configuration
- Target machines are specified using standard SSH syntax (`hostname` or SSH config alias)
- uv (for creating Python environments) is available on all machines (use Python 3.12+)
- Standard terminal emulators support ANSI escape codes for terminal UI
- Users have familiarity with command-line tools and SSH
- Configuration files (for sync behavior only) will be stored in standard locations (`~/.config/pc-switcher/` or `/etc/pc-switcher/`)
- Log files will be stored in standard locations (`~/.local/share/pc-switcher/logs/` or `/var/log/pc-switcher/`)
- System will use sudo for operations requiring elevated privileges

## Out of Scope

- Implementation of specific sync features (packages, docker, VMs, etc.) - those are separate features
- Btrfs snapshot management and rollback - covered by Safety Infrastructure feature
- Pre-sync validation framework - covered by Safety Infrastructure feature
- Actual data transfer mechanisms - will be implemented by individual feature modules
- GUI or web interface - terminal-based only
- Conflict resolution logic - will be handled by individual feature modules
- Multi-user concurrent usage - single user workflow only
- Sync scheduling or automation - manual trigger only
