# Feature Specification: Foundation Infrastructure Complete

**Feature Branch**: `002-foundation-complete`
**Created**: 2025-11-15
**Status**: Draft
**Input**: User description: "Write specs for features 1, 2 and 3 of the Feature breakdown.md: (1) Basic CLI & Infrastructure - Command parser, config system, connection, logging, terminal UI skeleton, architecture for modular features; (2) Safety Infrastructure - Pre-sync validation framework, btrfs snapshot management, rollback capability; (3) Installation & Setup - Deploy to machines, dependency installation"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Module Architecture and Integration Contract (Priority: P1)

The system defines a precise contract for how sync modules integrate with the core orchestration system. Each module (representing a discrete sync capability like package sync, Docker sync, or user data sync) implements a standardized interface covering configuration, validation, execution, logging, progress reporting, error handling, and rollback. This contract is detailed enough that all feature modules can be developed independently and concurrently once the core infrastructure exists.

**Why this priority**: This is P1 because it's the architectural foundation. Without a clear, detailed module contract, subsequent features cannot be developed independently or correctly. This user story serves as the specification document for all future module developers. All of the sync-features will be implemented as a module. Any of the other functionality in this spec that could use the module architecture (like User Story 2 and 3) will also be implemented as a module.

**Independent Test**: Can be fully tested by:
1. Defining the module interface contract
2. Implementing a minimal test module that satisfies the contract
3. Registering it with the core orchestrator
4. Running sync and verifying the orchestrator correctly:
   - Loads the module configuration
   - Calls lifecycle methods in correct order (validate → pre_sync → sync → post_sync)
   - Handles module logging at all six levels
   - Processes progress updates
   - Handles module errors (both exceptions and CRITICAL log events)
   - Calls cleanup on interrupts
5. Demonstrating that a developer can implement a new module by only implementing the contract

This delivers immediate value by establishing the development pattern for all 6 user-facing features (features 4-9 in the breakdown).

**Constitution Alignment**:
- Deliberate Simplicity (clear interface reduces complexity)
- Reliability Without Compromise (standardized contract ensures consistent behavior)
- Documentation As Runtime Contract (interface serves as implementation specification)

**Acceptance Scenarios**:

1. **Given** a developer implements a new sync module conforming to the module interface, **When** the module is registered with the orchestrator, **Then** the system automatically integrates it into the sync workflow, configuration system, logging infrastructure, and progress reporting without requiring changes to core code

2. **Given** a module defines configuration schema (enabled: bool, module-specific settings), **When** user loads configuration, **Then** the system validates module config against schema, applies defaults for missing values, and makes configuration available to the module via standardized accessor methods

3. **Given** a module emits log messages at any of six levels (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL), **When** logging occurs, **Then** the system routes logs to both file (with configured file log level) and terminal UI (with configured CLI log level), formats them consistently with timestamp and module name, and triggers sync abort if CRITICAL level is logged

4. **Given** a module emits progress updates (percentage, current item, estimated remaining), **When** progress is reported, **Then** the orchestrator forwards this to the terminal UI system for display and to the log file at FULL level

5. **Given** a module's validate() method returns validation errors, **When** validation phase executes, **Then** the orchestrator collects all errors, displays them in terminal UI, and halts sync before any state changes occur

6. **Given** a module is executing and raises an unhandled exception, **When** the exception propagates, **Then** the orchestrator catches it, logs it as CRITICAL, attempts to call the module's cleanup() method, and aborts the sync

7. **Given** a module declares dependencies on other modules (e.g., "must run after btrfs-snapshot-pre"), **When** orchestrator plans execution order, **Then** it topologically sorts modules by dependencies and executes them in correct order

8. **Given** user presses Ctrl+C during module execution, **When** signal is caught, **Then** orchestrator calls the currently-executing module's cleanup() method, logs interruption at WARNING level, and exits gracefully

---

### User Story 2 - Self-Installing Sync Orchestrator (Priority: P1)

When a user initiates sync from source to target, the very first operation the orchestrator performs (before any validation or snapshots) is to ensure the pc-switcher package on the target machine is at the same version as the source machine. If versions differ or pc-switcher is not installed on target, the system automatically installs or upgrades the target installation.

**Why this priority**: This is P1 because version consistency is required for reliable sync operations. Without matching versions, the target-side helper scripts may be incompatible with source-side orchestration logic, causing unpredictable failures. Self-installation also eliminates manual setup steps, aligning with "Frictionless Command UX".

**Independent Test**: Can be fully tested by:
1. Setting up a target machine without pc-switcher installed
2. Running sync from source
3. Verifying orchestrator detects missing installation
4. Validating orchestrator installs pc-switcher on target
5. Checking that versions now match
6. Repeating with version mismatch (older version on target) to test upgrade path

This delivers value by enabling zero-touch target setup and ensuring version consistency.

**Constitution Alignment**:
- Frictionless Command UX (automated installation, no manual setup)
- Reliability Without Compromise (version consistency prevents compatibility issues)
- Deliberate Simplicity (self-contained deployment, no separate install process)

**Acceptance Scenarios**:

1. **Given** source machine has pc-switcher version 0.3.2 installed and target machine has no pc-switcher installed, **When** user runs `pc-switcher sync <target>`, **Then** the orchestrator detects missing installation, installs the pc-switcher version 0.3.2 on target (using the installer from User Story 7), verifies installation succeeded, and proceeds with sync

2. **Given** source has version 0.4.0 and target has version 0.3.2, **When** sync begins, **Then** orchestrator detects version mismatch, logs "Target pc-switcher version 0.3.2 is outdated, upgrading to 0.4.0", installs the pc-switcher version 0.4.0 on target (using the installer from User Story 7), and verifies upgrade completed

3. **Given** source and target both have version 0.4.0, **When** sync begins, **Then** orchestrator logs "Target pc-switcher version matches source (0.4.0), skipping installation" and proceeds immediately to next phase

4. **Given** installation/upgrade fails on target (e.g., disk full, permissions issue), **When** the failure occurs, **Then** orchestrator logs CRITICAL error with diagnostic details, does not proceed with sync, and suggests remediation steps

---

### User Story 3 - Safety Infrastructure with Btrfs Snapshots (Priority: P1)

Before any sync operations modify state, the system creates read-only btrfs snapshots of critical subvolumes on both source and target machines. These "pre-sync" snapshots serve as rollback points. After all sync modules complete successfully, the system creates "post-sync" snapshots capturing the final state. This safety mechanism is implemented as a special required module that cannot be disabled by users.

**Why this priority**: This is P1 because it's the primary safety mechanism protecting against data loss. Without snapshots, there's no reliable way to recover from failed sync operations. This directly enforces the top project principle: "Reliability Without Compromise".

**Independent Test**: Can be fully tested by:
1. Running sync on test machines with btrfs filesystems
2. Verifying snapshots are created before any module executes
3. Confirming snapshot naming includes timestamp and sync session ID
4. Checking that snapshots are read-only
5. Simulating sync failure and verifying rollback can restore from pre-sync snapshot
6. Confirming post-sync snapshots are created after successful completion
7. Attempting to disable the snapshot module via config and verifying it remains active

This delivers value by providing the foundation for all rollback and recovery operations.

**Constitution Alignment**:
- Reliability Without Compromise (transactional safety via snapshots)
- Solid-State Stewardship (btrfs snapshots use copy-on-write, minimizing write amplification)

**Acceptance Scenarios**:

1. **Given** a sync is requested with configured subvolumes, **When** orchestrator begins pre-sync checks, **Then** it MUST verify that all configured subvolumes exist on both source and target; if any configured subvolume is missing on either side the system MUST log a CRITICAL error and abort sync before creating snapshots

2. **Given** user initiates sync, **When** the snapshot module executes (after version check and subvolume existence checks, before any state-modifying modules), **Then** the system creates read-only btrfs snapshots on both source and target for all configured subvolumes (e.g., `/`, `/home`) with naming pattern `<subvol>-presync-<timestamp>-<session-id>`

3. **Given** all sync modules complete successfully, **When** the post-sync phase executes, **Then** the system creates read-only btrfs snapshots on both source and target with naming pattern `<subvol>-postsync-<timestamp>-<session-id>`

4. **Given** user configuration includes `sync_modules: { btrfs_snapshots: false }`, **When** configuration is loaded, **Then** the system displays a clear error message "Required module 'btrfs_snapshots' cannot be disabled" with config file location and exits before sync begins

5. **Given** snapshot creation fails on target (e.g., insufficient space), **When** the failure occurs, **Then** the snapshot module logs CRITICAL error with details, and the orchestrator aborts sync before any state changes occur

6. **Given** pre-sync snapshots exist and a sync module later fails with CRITICAL error, **When** the orchestrator detects the failure, **Then** it offers to rollback to the pre-sync snapshots (user confirmation required for rollback execution)

7. **Given** snapshots accumulate over multiple sync runs, **When** user runs `pc-switcher cleanup-snapshots --older-than 7d`, **Then** the system deletes pre-sync and post-sync snapshots older than 7 days, retaining the most recent 3 (configurable) sync sessions regardless of age (`--older-than` is optional; default is configurable)

8. **Given** orchestrator configuration includes `disk.min_free` (absolute bytes or percentage) for source and target, **When** sync begins, **Then** orchestrator MUST check free disk space on both source and target and log CRITICAL and abort if free space is below configured threshold

9. **Given** orchestrator configuration includes `disk.check_interval` and `disk.reserve_minimum`, **When** sync is running, **Then** orchestrator MUST periodically check free disk space at the configured interval and log CRITICAL and abort if available free space falls below the reserved minimum on either side

---

### User Story 4 - Comprehensive Logging System (Priority: P1)

The system implements a six-level logging hierarchy (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL) with independent level configuration for file logging and terminal UI display. Log levels follow the ordering DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL: DEBUG is the most verbose and includes all messages, while FULL is a high-verbosity operational level that does NOT include DEBUG-level diagnostics. Logs are written to timestamped files in `~/.local/share/pc-switcher/logs/` on the source machine. All operations (core orchestrator, individual modules, target-side scripts) contribute to the unified log stream. CRITICAL log events automatically trigger sync abortion.

**Why this priority**: This is P1 because comprehensive logging is essential for development, troubleshooting, and reliability verification. The distinction between CRITICAL (abort sync) and ERROR (log but continue) is fundamental to error handling throughout the system.

**Independent Test**: Can be fully tested by:
1. Running sync with various log level configurations
2. Verifying file contains events at configured level and above
3. Confirming terminal shows only events at CLI log level and above
4. Testing that CRITICAL log triggers immediate sync abort
5. Checking log format includes timestamp, level, module name, and message
6. Validating that both source and target operations contribute to unified log

This delivers value by enabling developers to diagnose issues and providing audit trails for sync operations.

**Constitution Alignment**:
- Reliability Without Compromise (detailed audit trail)
- Documentation As Runtime Contract (logs document actual system behavior)

**Acceptance Scenarios**:

1. **Given** user configures `log_file_level: FULL` and `log_cli_level: INFO`, **When** sync runs and a module logs at DEBUG level, **Then** the message does NOT appear in the log file nor in the terminal UI (DEBUG is excluded by FULL)

2. **Given** user configures `log_file_level: INFO`, **When** sync runs and a module logs at FULL level (e.g., "Copying /home/user/file.txt"), **Then** the message does not appear in either log file or terminal UI

3. **Given** sync is running, **When** any module (or orchestrator core) logs at CRITICAL level, **Then** the logging system immediately signals the orchestrator to abort sync, the current module's cleanup() is called, no further modules execute, and the terminal displays "CRITICAL error encountered, aborting sync: [error message]"

4. **Given** sync operation completes, **When** user inspects log file at `~/.local/share/pc-switcher/logs/sync-<timestamp>.log`, **Then** the file contains structured log entries with format `[TIMESTAMP] [LEVEL] [MODULE] [HOSTNAME] message` for all operations from both source and target machines

5. **Given** a target-side helper script emits log output to stdout, **When** the source orchestrator receives this output, **Then** it parses the log level prefix (if present) and routes the message through the unified logging system

6. **Given** user runs `pc-switcher logs --last`, **When** command executes, **Then** the system displays the most recent sync log file in the terminal

**Log Level Definitions**:
- **CRITICAL**: Unrecoverable error requiring immediate sync abort (e.g., snapshot creation failed, target unreachable mid-sync, data corruption detected)
- **ERROR**: Recoverable error that may impact sync quality but doesn't require abort (e.g., individual file copy failed, optional feature unavailable)
- **WARNING**: Unexpected condition that should be reviewed but doesn't indicate failure (e.g., config value using deprecated format, unusually large transfer size)
- **INFO**: High-level operation reporting for normal user visibility (e.g., "Starting module X", "Module X completed successfully", "Connection established")
- **FULL**: File-level operation details (e.g., "Copying /home/user/document.txt", "Created snapshot @home-presync-20251115-abc123"). FULL captures operational detail but explicitly excludes DEBUG-level diagnostic messages.
- **DEBUG**: Verbose diagnostic information (e.g., command outputs, detailed timings, internal state transitions). DEBUG includes all messages (FULL, INFO, etc.) and is intended for deep troubleshooting.

---

### User Story 5 - Graceful Interrupt Handling (Priority: P1)

When the user presses Ctrl+C during sync, the system catches the SIGINT signal, notifies the currently-executing module, logs the interruption, sends cleanup commands to the target machine, closes the connection cleanly, and exits with appropriate status code. The system does not leave orphaned processes. This does not issue a rollback automatically; that is a separate user action. How rollback can be initiated after an interrupt is covered in User Story 3.

**Why this priority**: This is P1 because users must be able to safely interrupt long-running operations. Without graceful handling, interrupts could leave systems in inconsistent states or with orphaned processes on target machines.

**Independent Test**: Can be fully tested by:
1. Starting a sync operation with a long-running dummy module
2. Pressing Ctrl+C mid-execution
3. Verifying terminal displays "Sync interrupted by user"
4. Confirming currently-executing module's cleanup() was called
5. Checking that connection to target was closed
6. Validating no orphaned processes remain on source or target
7. Ensuring log file contains interruption event

This delivers value by giving users confidence they can safely stop operations.

**Constitution Alignment**:
- Frictionless Command UX (user maintains control)
- Reliability Without Compromise (clean shutdown prevents inconsistent state)

**Acceptance Scenarios**:

1. **Given** sync operation is in progress with a module executing on target machine, **When** user presses Ctrl+C, **Then** the orchestrator catches SIGINT, logs "Sync interrupted by user" at WARNING level, calls the current module's cleanup() method, sends termination signal to target-side process, waits up to 5 seconds for graceful shutdown, then closes connection and exits with code 130

2. **Given** sync is in the orchestrator phase between modules (no module actively running), **When** user presses Ctrl+C, **Then** orchestrator logs interruption, skips remaining modules, and exits cleanly

3. **Given** user presses Ctrl+C multiple times rapidly, **When** the second SIGINT arrives before cleanup completes, **Then** orchestrator immediately force-terminates (kills connection, exits with code 130) without waiting for graceful cleanup
---

### User Story 6 - Configuration System (Priority: P1)

The system loads configuration from `~/.config/pc-switcher/config.yaml` covering global settings (log levels, enabled modules) and module-specific settings. Each module declares its configuration schema; the core validates module configs against schemas and provides validated settings to modules. Configuration supports enabling/disabling optional modules, setting separate log levels for file and CLI, and module-specific parameters.

**Why this priority**: This is P1 because modules need configuration to function, and users need the ability to customize behavior (especially disabling expensive or irrelevant modules like k3s sync).

**Independent Test**: Can be fully tested by:
1. Creating a config file with various settings
2. Running sync and verifying modules receive correct configuration
3. Testing invalid config triggers validation errors
4. Confirming log levels are applied correctly
5. Disabling an optional module and verifying it's skipped
6. Attempting to disable a required module and verifying it remains active

This delivers value by enabling user customization and module parameterization.

**Constitution Alignment**:
- Frictionless Command UX (reasonable defaults, easy customization)
- Deliberate Simplicity (single config file, clear structure)

**Acceptance Scenarios**:

1. **Given** config file contains global settings and module sections, **When** orchestrator starts, **Then** it loads config, validates structure, applies defaults for missing values, and makes settings available to modules via `module.config` accessor

2. **Given** config includes `log_file_level: DEBUG` and `log_cli_level: INFO`, **When** sync runs, **Then** file logging captures all events at DEBUG and above, while terminal UI shows only INFO and above

3. **Given** config includes `sync_modules: { user_data: true, k3s: false }`, **When** sync runs, **Then** user data module executes and k3s module is skipped (with INFO log: "k3s module disabled by configuration")

4. **Given** a module declares required config parameters (e.g., Docker module requires `docker_preserve_cache: bool`), **When** config is missing this parameter and no default exists, **Then** orchestrator logs CRITICAL error during startup and refuses to run

5. **Given** config file has invalid YAML syntax, **When** orchestrator attempts to load it, **Then** the system displays clear parse error with line number and exits before attempting sync

**Example Configuration**:
```yaml
# ~/.config/pc-switcher/config.yaml
log_file_level: FULL
log_cli_level: INFO

sync_modules:
  user_data: true
  packages: true
  docker: false
  vms: false
  k3s: false

# Module-specific configuration
btrfs_snapshots:
  subvolumes:
    - "/"
    - "/home"
    - "/root"

user_data:
  exclude_patterns:
    - "**/.cache/*"
    - "**/node_modules/*"
  preserve_timestamps: true

packages:
  sync_ppa: true
  sync_flatpak: true
```

---

### User Story 7 - Installation and Setup Infrastructure (Priority: P2)

The system provides installation and setup tooling to deploy pc-switcher to new machines and configure required infrastructure (packages, configuration). A setup script handles initial installation, dependency checking, and subvolume creation guidance.

**Why this priority**: This is P2 because while essential for new users, developers can manually install during early development. Once the core sync system works, this becomes P1 for usability.

**Independent Test**: Can be fully tested by:
1. Running setup script on a fresh Ubuntu 24.04 machine
2. Verifying all dependencies are installed
3. Confirming pc-switcher package is installed
4. Checking that config directory is created with default config
5. Validating btrfs subvolume structure guidance is provided

This delivers value by streamlining initial deployment.

**Constitution Alignment**:
- Frictionless Command UX (simple installation process)
- Proven Tooling Only (uses standard package managers)

**Acceptance Scenarios**:

1. **Given** a fresh Ubuntu 24.04 machine, **When** user runs the installation script, **Then** the script checks that the filesystem is btrfs, creates `~/.config/pc-switcher/` with default config, installs any software/packages and configuration necessary to run pc-switcher (if not installed/configured yet), and displays "pc-switcher installed successfully"

2. **Given** user's system uses btrfs but does not have recommended subvolume structure, **When** setup runs, **Then** it displays guidance on creating subvolumes (e.g., "@home for /home", "@root for /root") and warns that some features require specific subvolumes

3. **Given** user runs setup on a non-btrfs filesystem, **When** the script detects this, **Then** it logs CRITICAL error "pc-switcher requires btrfs filesystem for safety features" and exits without making changes

---

### User Story 8 - Dummy Test Modules (Priority: P1)

Three dummy modules exist for testing infrastructure: `dummy-success` (completes successfully with INFO/WARNING/ERROR logs), `dummy-critical` (emits CRITICAL error mid-execution to test abort handling), and `dummy-fail` (raises unhandled exception to test exception handling). Each simulates long-running operations on both source and target with progress reporting.

**Why this priority**: This is P1 because these modules are essential for testing the orchestrator, logging, progress UI, error handling, and interrupt handling during development. They serve as reference implementations of the module contract.

**Independent Test**: Each dummy module can be independently tested by enabling it in config and running sync, then verifying expected behavior.

**Constitution Alignment**:
- Deliberate Simplicity (provides clear reference implementation)
- Reliability Without Compromise (enables thorough testing)

**Acceptance Scenarios**:

1. **Given** `dummy-success` module is enabled, **When** sync runs, **Then** the module performs 20-second busy-wait on source (logging INFO message every 2s), emits WARNING at 6s, performs 20-second busy-wait on target (logging INFO message every 2s), emits ERROR at 8s, reports progress updates (0%, 25%, 50%, 75%, 100%), and completes successfully

2. **Given** `dummy-critical` module is enabled, **When** sync runs and module reaches 50% progress, **Then** the module logs CRITICAL error "Simulated critical failure for testing", continues its internal loop (does not self-abort), but the orchestrator detects the CRITICAL log, calls module's cleanup(), aborts sync, and no subsequent modules execute

3. **Given** `dummy-fail` module is enabled, **When** sync runs and module reaches 60% progress, **Then** the module raises and unhandled exception, the orchestrator catches the exception, logs it as CRITICAL, attempts cleanup(), and aborts sync

4. **Given** any dummy module is running, **When** user presses Ctrl+C, **Then** the module's cleanup() method is called, it logs "Dummy module cleanup called", stops its busy-wait loop, and returns control to orchestrator

---

### User Story 9 - Terminal UI with Progress Reporting (Priority: P2)

The terminal displays real-time sync progress including current module, operation phase (validate/sync/cleanup), progress percentage, current item being processed, and log messages at configured CLI log level. UI updates smoothly without excessive redraws and gracefully handles terminal resize.

**Why this priority**: This is P2 because basic sync can work with simple log output to terminal. Rich progress UI significantly improves UX but isn't blocking for core functionality testing.

**Independent Test**: Can be tested by running sync with dummy modules and verifying terminal shows progress bars, module names, log messages, and updates smoothly.

**Constitution Alignment**:
- Frictionless Command UX (clear feedback reduces uncertainty)

**Acceptance Scenarios**:

1. **Given** sync is running, **When** a module reports progress, **Then** terminal displays progress bar, percentage, current module name, and current operation (e.g., "Docker Sync: 45% - Copying image nginx:latest")

2. **Given** multiple modules execute sequentially, **When** each completes, **Then** terminal shows overall progress (e.g., "Step 3/7: Package Sync") and individual module progress

3. **Given** a module emits log at INFO level or higher, **When** log reaches terminal UI, **Then** it's displayed below progress indicators with appropriate formatting (color-coded by level if terminal supports colors)

---

### Edge Cases

- What happens when target machine becomes unreachable mid-sync?
  - Orchestrator detects connection loss, logs CRITICAL error, attempts to reconnect once, if reconnection fails logs diagnostic information and aborts
- What happens when source machine crashes or powers off?
  - Target-side operations should timeout and cleanup after 5 minutes of no communication; next sync will detect inconsistent state via validation
- What happens when btrfs snapshots cannot be created due to insufficient space?
  - Snapshot module logs CRITICAL error with space usage details, orchestrator aborts before any state modification
- What happens when a module's cleanup() method raises an exception?
  - Orchestrator logs the exception, continues with shutdown sequence (cleanup is best-effort)
- What happens when user runs multiple sync commands concurrently?
  - Second invocation detects lock, displays "Another sync is in progress (PID: 12345)", gives instructions on how to remove a stale lock and exits
- What happens when config file contains unknown module names?
  - Orchestrator logs ERROR "Unknown module 'xyz' in configuration, aborting" and aborts sync
- How does the system handle partial failures (some modules succeed, some fail)?
  - Each module's success/failure is tracked independently; orchestrator logs summary at end showing which modules succeeded/failed; overall sync is considered failed if any module fails
- What happens when target has newer pc-switcher version than source?
  - Orchestrator detects version mismatch, logs CRITICAL "Target version 0.5.0 is newer than source 0.4.0, this is unusual", and aborts sync to prevent accidental downgrade

## Requirements *(mandatory)*

### Functional Requirements

#### Module Architecture

- **FR-001** `[Deliberate Simplicity]` `[Reliability Without Compromise]`: System MUST define a standardized module interface specifying methods: `validate()`, `pre_sync()`, `sync()`, `post_sync()`, `cleanup()`, `get_config_schema()`, and properties: `name`, `version`, `dependencies`

- **FR-002** `[Deliberate Simplicity]`: Each module MUST declare dependencies as list of module names it must run after; orchestrator MUST topologically sort modules by dependencies before execution

- **FR-003** `[Reliability Without Compromise]`: System MUST call module lifecycle methods in order: `validate()` (all modules), `pre_sync()`, `sync()`, `post_sync()` for each module, then `cleanup()` on shutdown or errors

- **FR-004** `[Reliability Without Compromise]`: System MUST call currently-executing module's `cleanup()` method when Ctrl+C is pressed, waiting for a configurable timeout, before exiting

- **FR-005** `[Deliberate Simplicity]`: Modules MUST be auto-discovered from the configuration file section `sync_modules` and instantiated by the orchestrator

#### Self-Installation

- **FR-006** `[Frictionless Command UX]`: System MUST check target machine's pc-switcher version before any other operations; if missing or mismatched, MUST install/upgrade to source version

- **FR-007** `[Reliability Without Compromise]`: System MUST abort sync with CRITICAL log if the target machine's pc-switcher version is newer than the source version (preventing accidental downgrades)

- **FR-008** `[Frictionless Command UX]`: If installation/upgrade fails, system MUST log CRITICAL error with diagnostic details and abort sync

#### Safety Infrastructure

- **FR-009** `[Reliability Without Compromise]`: System MUST create read-only btrfs snapshots of configured subvolumes on both source and target before any module executes (after version check and pre-checks)

- **FR-010** `[Reliability Without Compromise]`: System MUST create post-sync snapshots after all modules complete successfully

- **FR-011** `[Solid-State Stewardship]`: Snapshot naming MUST follow pattern `@<subvolume>-{presync|postsync}-<ISO8601-timestamp>-<session-id>` for clear identification and cleanup

- **FR-012** `[Reliability Without Compromise]`: Snapshot module MUST be marked as required and system MUST ignore attempts to disable it via configuration

- **FR-013** `[Frictionless Command UX]`: If pre-sync snapshot creation fails, system MUST log CRITICAL error and abort before any state modifications occur

- **FR-014** `[Reliability Without Compromise]`: System MUST provide rollback capability to restore from pre-sync snapshots (requires user confirmation)

- **FR-015** `[Solid-State Stewardship]`: System MUST provide snapshot cleanup command to delete old snapshots while retaining most recent N syncs

- **FR-016** `[Reliability Without Compromise]`: System MUST verify that all configured subvolumes exist on both source and target before attempting snapshots; if any are missing, system MUST log CRITICAL and abort

- **FR-017** `[Reliability Without Compromise]`: Orchestrator MUST check free disk space on both source and target before starting a sync; the minimum free-space threshold MUST be configurable (absolute bytes or percentage)

- **FR-018** `[Reliability Without Compromise]`: Orchestrator MUST monitor free disk space on source and target at a configurable interval during sync and abort with CRITICAL if available free space falls below the configured reserved minimum

#### Logging System

- **FR-019** `[Documentation As Runtime Contract]`: System MUST implement six log levels with the following ordering and semantics: DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL. DEBUG is the most verbose (includes everything); FULL is high-verbosity but explicitly excludes DEBUG messages.

- **FR-020** `[Reliability Without Compromise]`: Any log event at CRITICAL level MUST trigger immediate sync abort after calling cleanup() on current module

- **FR-021** `[Frictionless Command UX]`: System MUST support independent log level configuration for file output (`log_file_level`) and terminal display (`log_cli_level`)

- **FR-022** `[Documentation As Runtime Contract]`: System MUST write all logs at configured file level or above to timestamped file in `~/.local/share/pc-switcher/logs/sync-<timestamp>.log`

- **FR-023** `[Documentation As Runtime Contract]`: Log entries MUST include timestamp (ISO8601) in UTC, level, module name, and message in structured format

- **FR-024** `[Reliability Without Compromise]`: System MUST aggregate logs from both source-side orchestrator and target-side operations into unified log stream

#### Interrupt Handling

- **FR-025** `[Reliability Without Compromise]`: System MUST install SIGINT handler that calls cleanup() on current module, logs interruption at WARNING level, and exits with code 130

- **FR-026** `[Reliability Without Compromise]`: On interrupt, system MUST send termination signal to any target-side processes and wait up to 5 seconds for graceful shutdown

- **FR-027** `[Reliability Without Compromise]`: If second SIGINT is received during cleanup, system MUST immediately force-terminate without waiting

- **FR-028** `[Reliability Without Compromise]`: System MUST ensure no orphaned processes remain on source or target after interrupt

#### Configuration System

- **FR-029** `[Frictionless Command UX]`: System MUST load configuration from `~/.config/pc-switcher/config.yaml` on startup

- **FR-030** `[Deliberate Simplicity]`: Configuration MUST use YAML format with sections: global settings, `sync_modules` (enable/disable), and per-module settings

- **FR-031** `[Reliability Without Compromise]`: System MUST validate configuration structure and module-specific settings against module-declared schemas before execution

- **FR-032** `[Frictionless Command UX]`: System MUST apply reasonable defaults for missing configuration values

- **FR-033** `[Frictionless Command UX]`: System MUST allow enabling/disabling optional modules via `sync_modules: { module_name: true/false }`

- **FR-034** `[Reliability Without Compromise]`: If configuration file has syntax errors or invalid values, system MUST display clear error message with location and exit before sync

- **FR-035** `[Reliability Without Compromise]`: If configuration file contains a disable attempt for a required module, system MUST display clear error message with location and exit before sync

#### Installation & Setup

- **FR-036** `[Frictionless Command UX]`: System MUST provide installation script that checks and installs/upgrades dependencies, installs pc-switcher package, and creates default configuration

- **FR-037** `[Frictionless Command UX]`: Setup script MUST detect btrfs filesystem presence and provide guidance on recommended subvolume structure if not present

- **FR-038** `[Documentation As Runtime Contract]`: Setup script MUST create default config file with inline comments explaining each setting

#### Testing Infrastructure

- **FR-039** `[Deliberate Simplicity]`: System MUST include three dummy modules: `dummy-success`, `dummy-critical`, `dummy-fail`

- **FR-040** `[Deliberate Simplicity]`: `dummy-success` MUST simulate 20s operation on source (log every 2s, WARNING at 6s) and 20s on target (log every 2s, ERROR at 8s), emit progress updates, and complete successfully

- **FR-041** `[Reliability Without Compromise]`: `dummy-critical` MUST log CRITICAL error at 50% progress and continue execution to test that orchestrator aborts sync

- **FR-042** `[Reliability Without Compromise]`: `dummy-fail` MUST raise unhandled exception at 60% progress to test orchestrator exception handling

- **FR-043** `[Reliability Without Compromise]`: All dummy modules MUST implement `cleanup()` that logs "Dummy module cleanup called" and stops execution

#### Progress Reporting

- **FR-044** `[Frictionless Command UX]`: Modules CAN emit progress updates including percentage (0-100), current item description, and estimated completion time

- **FR-045** `[Frictionless Command UX]`: Orchestrator MUST forward progress updates to terminal UI system for display

- **FR-046** `[Documentation As Runtime Contract]`: Progress updates MUST be written to log file at FULL log level

#### Core Orchestration

- **FR-047** `[Frictionless Command UX]`: System MUST provide single command `pc-switcher sync <target>` that executes complete workflow

- **FR-048** `[Reliability Without Compromise]`: System MUST implement locking mechanism to prevent concurrent sync executions

- **FR-049** `[Documentation As Runtime Contract]`: System MUST log overall sync result (success/failure) and summary of module outcomes

### Key Entities

- **Module**: Represents a sync component implementing the module interface; has name, version, dependencies, config schema, and lifecycle methods
- **SyncSession**: Represents a single sync operation including session ID, timestamp, source/target machines, enabled modules, and execution state
- **Snapshot**: Represents a btrfs snapshot including subvolume name, timestamp, session ID, type (pre/post), and location (source/target)
- **LogEntry**: Represents a logged event with timestamp, level, module name, message, and structured context data
- **ProgressUpdate**: Represents module progress including percentage, current item, estimated remaining time, and module name
- **Configuration**: Represents parsed and validated config including global settings, module enable/disable flags, and per-module settings
- **TargetConnection**: Represents connection to target machine with methods for command execution, file transfer, and process management

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** `[Frictionless Command UX]`: User executes complete sync with single command `pc-switcher sync <target>` without additional manual steps

- **SC-002** `[Reliability Without Compromise]`: System creates snapshots before and after sync in 100% of successful sync runs

- **SC-003** `[Reliability Without Compromise]`: System successfully aborts sync within 5 seconds when CRITICAL error occurs, with no state modifications after abort

- **SC-004** `[Frictionless Command UX]`: System completes version check and installation/upgrade on target within 30 seconds

- **SC-005** `[Documentation As Runtime Contract]`: Log files contain complete audit trail of all operations with timestamps, levels, and module attribution in 100% of sync runs

- **SC-006** `[Reliability Without Compromise]`: User interrupt (Ctrl+C) results in graceful shutdown with no orphaned processes in 100% of tests

- **SC-007** `[Deliberate Simplicity]`: New feature module implementation requires only implementing module interface (< 200 lines of code for basic module) with no changes to core orchestrator

- **SC-008** `[Solid-State Stewardship]`: Btrfs snapshots use copy-on-write with zero initial write amplification (verified via btrfs filesystem usage commands)

- **SC-009** `[Frictionless Command UX]`: Installation script completes setup on fresh Ubuntu 24.04 machine in under 2 minutes with network connection

- **SC-010** `[Reliability Without Compromise]`: All three dummy modules correctly demonstrate their expected behaviors (success, CRITICAL abort, exception handling) in 100% of test runs

## Assumptions

- Source and target machines run Ubuntu 24.04 LTS with btrfs filesystems
- User has sudo privileges on both machines for operations requiring elevation
- Machines are connected via LAN during sync operations
- Terminal emulator supports ANSI escape codes for progress UI
- User's `~/.ssh/config` contains target machine configurations if using aliases
- Sufficient disk space exists on target for package installation
- No other tools are simultaneously modifying the same system state during sync

## Out of Scope

- Implementation of user-facing sync features (user data, packages, Docker, VMs, k3s) - those are separate feature specs (features 4-9)
- Bi-directional sync or conflict resolution between divergent states
- Automatic sync scheduling or daemon mode
- GUI or web interface
- Sync over internet (only LAN supported)
- Windows or macOS support
- Non-btrfs filesystems
- Multi-user concurrent usage
- Automated testing infrastructure (CI/CD) - though dummy modules enable manual testing
