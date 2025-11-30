# Feature Specification: Foundation Infrastructure Complete

**Feature Branch**: `001-foundation`
**Created**: 2025-11-15
**Status**: Draft
**Input**: User description: "Write specs for features 1, 2 and 3 of the Feature breakdown.md: (1) Basic CLI & Infrastructure - Command parser, config system, connection, logging, terminal UI skeleton, architecture for modular features; (2) Safety Infrastructure - Pre-sync validation framework, btrfs snapshot management, rollback capability; (3) Installation & Setup - Deploy to machines, dependency installation"

## Navigation

**Documentation Hierarchy:**
- [High level requirements](../../docs/High%20level%20requirements.md) - Project vision and scope
- [Architecture Decision Records](../../docs/adr/_index.md) - Cross-cutting architectural decisions
- Specification (this document) - Detailed requirements for this feature
- [Architecture](architecture.md) - Component design and interactions
- [Data model](data-model.md) - Data structures and schemas
- [Implementation plan](plan.md) - Implementation approach and phases
- [Tasks](tasks.md) - Actionable implementation tasks

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Job Architecture and Integration Contract (Priority: P1)

The system defines a precise contract for how sync jobs integrate with the core orchestration system. Each job (representing a discrete sync capability like package sync, Docker sync, or user data sync) implements a standardized interface covering configuration, validation, execution, logging, progress reporting, and error handling. This contract is detailed enough that all feature jobs can be developed independently and concurrently once the core infrastructure exists.

**Why this priority**: This is P1 because it's the architectural foundation. Without a clear, detailed job contract, subsequent features cannot be developed independently or correctly. This user story serves as the specification document for all future job developers. All sync-features (packages, Docker, VMs, k3s, user data) will be implemented as jobs. The btrfs snapshots safety infrastructure (User Story 3) is orchestrator-level infrastructure (not configurable via sync_jobs). Self-installation (User Story 2) is NOT a job—it is pre-job orchestrator logic that runs before any job execution.

**Independent Test**: Can be fully tested by:
1. Defining the job interface contract
2. Implementing a minimal test job that satisfies the contract
3. Registering it with the core orchestrator
4. Running sync and verifying the orchestrator correctly:
   - Loads the job configuration
   - Calls lifecycle methods in correct order (validate → execute)
   - Handles job logging at all six levels
   - Processes progress updates
   - Handles job errors (exceptions)
   - Requests job termination on interrupts
5. Demonstrating that a developer can implement a new job by only implementing the contract

This delivers immediate value by establishing the development pattern for all 6 user-facing features (features 4-9 in the breakdown).

**Constitution Alignment**:
- Deliberate Simplicity (clear interface reduces complexity)
- Reliability Without Compromise (standardized contract ensures consistent behavior)
- Up-to-date Documentation (interface serves as implementation specification)

**Acceptance Scenarios**:

1. **Given** a developer implements a new sync job conforming to the job interface, **When** the job is registered with the orchestrator, **Then** the system automatically integrates it into the sync workflow, configuration system, logging infrastructure, and progress reporting without requiring changes to core code

2. **Given** a job defines configuration schema (enabled: bool, job-specific settings), **When** user loads configuration, **Then** the system validates job config against schema, applies defaults for missing values, and makes configuration available to the job via standardized accessor methods

3. **Given** a job emits log messages at any of six levels (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL), **When** logging occurs, **Then** the system routes logs to both file (with configured file log level) and terminal UI (with configured CLI log level), and formats them consistently with timestamp and job name

4. **Given** a job emits progress updates (percentage, current item, estimated remaining), **When** progress is reported, **Then** the orchestrator forwards this to the terminal UI system for display and to the log file at FULL level

5. **Given** a job's validate() method returns validation errors, **When** validation phase executes, **Then** the orchestrator collects all errors, displays them in terminal UI, and halts sync before any state changes occur (no termination request needed as jobs haven't started executing)

6. **Given** a job is executing and raises an exception, **When** the exception propagates, **Then** the orchestrator catches it, logs the error message at CRITICAL level, requests job termination with cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py), and halts the sync before any further jobs execute

7. **Given** user presses Ctrl+C during job execution, **When** signal is caught, **Then** orchestrator requests termination of the currently-executing job with cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py), logs interruption at WARNING level, and exits gracefully with code 130

---

### User Story 2 - Self-Installing Sync Orchestrator (Priority: P1)

When a user initiates sync from source to target, the very first operation the orchestrator performs (before any system validation or snapshots) is to ensure the pc-switcher package on the target machine is at the same version as the source machine. If versions differ or pc-switcher is not installed on target, the system automatically installs or upgrades the target installation.

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

1. **Given** source machine has pc-switcher version 0.3.2 installed and target machine has no pc-switcher installed, **When** user runs `pc-switcher sync <target>`, **Then** the orchestrator detects missing installation, installs pc-switcher version 0.3.2 on target from GitHub repository using `uv tool install git+https://github.com/.../pc-switcher@v0.3.2`, verifies installation succeeded, and proceeds with sync

2. **Given** source has version 0.4.0 and target has version 0.3.2, **When** sync begins, **Then** orchestrator detects version mismatch, logs "Target pc-switcher version 0.3.2 is outdated, upgrading to 0.4.0", upgrades pc-switcher on target from GitHub repository using Git URL installation, and verifies upgrade completed

3. **Given** source and target both have version 0.4.0, **When** sync begins, **Then** orchestrator logs "Target pc-switcher version matches source (0.4.0), skipping installation" and proceeds immediately to next phase

4. **Given** installation/upgrade fails on target (e.g., disk full, permissions issue), **When** the failure occurs, **Then** orchestrator logs CRITICAL error and does not proceed with sync

---

### User Story 3 - Safety Infrastructure with Btrfs Snapshots (Priority: P1)

Before any sync operations modify state, the system creates read-only btrfs snapshots of critical subvolumes on both source and target machines. These "pre-sync" snapshots serve as recovery points. After all sync jobs complete successfully, the system creates "post-sync" snapshots capturing the final state. This safety mechanism is implemented as orchestrator-level infrastructure (not a SyncJob) that is always active and cannot be disabled by users.

**Why this priority**: This is P1 because it's the primary safety mechanism protecting against data loss. Without snapshots, there's no reliable way to recover from failed sync operations. This directly enforces the top project principle: "Reliability Without Compromise".

**Independent Test**: Can be fully tested by:
1. Running sync on test machines with btrfs filesystems
2. Verifying orchestrator validates subvolume existence during pre-sync checks
3. Verifying snapshots are created before any SyncJob executes
4. Confirming snapshot naming includes timestamp and sync session ID
5. Checking that snapshots are read-only
6. Simulating sync failure and verifying pre-sync snapshot can be used for manual recovery
7. Confirming post-sync snapshots are created after successful completion of all SyncJobs
8. Verifying that snapshot infrastructure is always active (no config option to disable)

This delivers value by providing the foundation for all recovery operations.

**Constitution Alignment**:
- Reliability Without Compromise (transactional safety via snapshots)
- Minimize SSD Wear (btrfs snapshots use copy-on-write, minimizing write amplification)

**Acceptance Scenarios**:

1. **Given** a sync is requested with configured subvolumes, **When** orchestrator begins pre-sync checks, **Then** it MUST verify that all configured subvolumes exist on both source and target; if any configured subvolume is missing on either side the system MUST log a CRITICAL error and abort sync before creating snapshots

2. **Given** user initiates sync, **When** the orchestrator begins the pre-sync phase (after version check and subvolume existence checks, before any SyncJobs execute), **Then** the system creates read-only btrfs snapshots in `/.snapshots/pc-switcher/<timestamp>-<session-id>/` on both source and target for all configured subvolumes (e.g., `@`, `@home`) with naming pattern `pre-<subvol>-<timestamp>` (e.g., `pre-@home-20251129T143022`)

3. **Given** all sync jobs complete successfully, **When** the orchestrator begins the post-sync phase, **Then** the system creates read-only btrfs snapshots in the same session folder with naming pattern `post-<subvol>-<timestamp>`

4. **Given** `/.snapshots/` does not exist on source or target, **When** orchestrator validates snapshot infrastructure, **Then** it creates `/.snapshots/` as a btrfs subvolume and logs INFO to inform the user

5. **Given** `/.snapshots/` exists but is a regular directory (not a subvolume), **When** orchestrator validates snapshot infrastructure, **Then** it logs CRITICAL error explaining the problem (snapshots would be recursive) and aborts sync

6. **Given** snapshot creation fails on target (e.g., insufficient space), **When** the failure occurs, **Then** the orchestrator logs CRITICAL error and aborts sync before any state changes occur

7. **Given** snapshots accumulate over multiple sync runs, **When** user runs `pc-switcher cleanup-snapshots --older-than 7d`, **Then** the system deletes pre-sync and post-sync snapshots older than 7 days, retaining the most recent 3 (configurable) sync sessions regardless of age (`--older-than` is optional; default is configurable)

8. **Given** orchestrator configuration includes `disk_space_monitor.preflight_minimum` (percentage like "20%" or absolute value like "50GiB", default: "20%") for source and target, **When** sync begins, **Then** orchestrator MUST check free disk space on both source and target and log CRITICAL and abort if free space is below configured threshold

9. **Given** orchestrator configuration includes `disk_space_monitor.check_interval` (seconds, default: 30) and `disk_space_monitor.runtime_minimum` (percentage like "15%" or absolute value like "40GiB", default: "15%"), **When** sync is running, **Then** orchestrator MUST periodically check free disk space at the configured interval and log CRITICAL and abort if available free space falls below the configured runtime minimum on either side

---

### User Story 4 - Comprehensive Logging System (Priority: P1)

The system implements a six-level logging hierarchy (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL) with independent level configuration for file logging and terminal UI display. Log levels follow the ordering DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL: DEBUG is the most verbose and includes all messages, while FULL is a high-verbosity operational level that does NOT include DEBUG-level diagnostics. Logs are written to timestamped files in `~/.local/share/pc-switcher/logs/` on the source machine. All operations (core orchestrator, individual jobs, target-side scripts) contribute to the unified log stream.

**Why this priority**: This is P1 because comprehensive logging is essential for development, troubleshooting, and reliability verification.

**Independent Test**: Can be fully tested by:
1. Running sync with various log level configurations
2. Verifying file contains events at configured level and above
3. Confirming terminal shows only events at CLI log level and above
4. Checking log format includes timestamp, level, job name, and message
5. Validating that both source and target operations contribute to unified log

This delivers value by enabling developers to diagnose issues and providing audit trails for sync operations.

**Constitution Alignment**:
- Reliability Without Compromise (detailed audit trail)

**Acceptance Scenarios**:

1. **Given** user configures `log_file_level: FULL` and `log_cli_level: INFO`, **When** sync runs and a job logs at DEBUG level, **Then** the message does NOT appear in the log file nor in the terminal UI (DEBUG is excluded by FULL)

2. **Given** user configures `log_file_level: INFO`, **When** sync runs and a job logs at FULL level (e.g., "Copying /home/user/file.txt"), **Then** the message does not appear in either log file or terminal UI

3. *(Removed)*

4. **Given** sync operation completes, **When** user inspects log file at `~/.local/share/pc-switcher/logs/sync-<timestamp>.log`, **Then** the file contains structured log entries with format `[TIMESTAMP] [LEVEL] [MODULE] [HOSTNAME] message` for all operations from both source and target machines

5. *(Removed - target-side logging is Job implementation detail, not a spec-level concern)*

6. **Given** user runs `pc-switcher logs --last`, **When** command executes, **Then** the system displays the most recent sync log file in the terminal using rich console with syntax highlighting for improved readability

**Log Level Definitions** (from most to least verbose):
- **DEBUG**: Most verbose level for internal diagnostics, including command outputs, detailed timings, internal state transitions, and all messages from lower levels (FULL, INFO, WARNING, ERROR, CRITICAL). Intended for deep troubleshooting and development.
- **FULL**: High-verbosity operational details including file-level operations (e.g., "Copying /home/user/document.txt", "Created snapshot pre-@home-20251115T143022") and all messages from lower levels (INFO, WARNING, ERROR, CRITICAL). Excludes DEBUG-level internal diagnostics.
- **INFO**: High-level operation reporting for normal user visibility (e.g., "Starting job X", "Job X completed successfully", "Connection established") and all messages from lower levels (WARNING, ERROR, CRITICAL).
- **WARNING**: Unexpected conditions that should be reviewed but don't indicate failure (e.g., config value using deprecated format, unusually large transfer size) and all messages from lower levels (ERROR, CRITICAL).
- **ERROR**: Recoverable errors that may impact sync quality but don't require abort (e.g., individual file copy failed, optional feature unavailable) and CRITICAL messages.
- **CRITICAL**: Unrecoverable errors requiring immediate sync abort (e.g., snapshot creation failed, target unreachable mid-sync, data corruption detected). Triggered when jobs raise an unhandled exception.

---

### User Story 5 - Graceful Interrupt Handling (Priority: P1)

When the user presses Ctrl+C during sync, the system catches the SIGINT signal, notifies the currently-executing job, logs the interruption, sends cleanup commands to the target machine, closes the connection cleanly, and exits with appropriate status code. The system does not leave orphaned processes. This does not issue a rollback automatically; rollback capability is a separate feature.

**Why this priority**: This is P1 because users must be able to safely interrupt long-running operations. Without graceful handling, interrupts could leave systems in inconsistent states or with orphaned processes on target machines.

**Independent Test**: Can be fully tested by:
1. Starting a sync operation with a long-running dummy job
2. Pressing Ctrl+C mid-execution
3. Verifying terminal displays "Sync interrupted by user"
4. Confirming currently-executing job received termination request and performed cleanup
5. Checking that connection to target was closed
6. Validating no orphaned processes remain on source or target
7. Ensuring log file contains interruption event

This delivers value by giving users confidence they can safely stop operations.

**Constitution Alignment**:
- Frictionless Command UX (user maintains control)
- Reliability Without Compromise (clean shutdown prevents inconsistent state)

**Acceptance Scenarios**:

1. **Given** sync operation is in progress with a job executing on target machine, **When** user presses Ctrl+C, **Then** the orchestrator catches SIGINT, logs "Sync interrupted by user" at WARNING level, requests termination of the current job, sends termination signal to target-side processes, waits up to the cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py) for graceful cleanup, then closes connection and exits with code 130

2. **Given** sync is in the orchestrator phase between jobs (no job actively running), **When** user presses Ctrl+C, **Then** orchestrator logs interruption, skips remaining jobs, and exits cleanly

3. **Given** user presses Ctrl+C multiple times rapidly, **When** the second SIGINT arrives before cleanup completes, **Then** orchestrator immediately force-terminates (kills connection, exits with code 130) without waiting for graceful cleanup
---

### User Story 6 - Configuration System (Priority: P1)

The system loads configuration from `~/.config/pc-switcher/config.yaml` covering global settings (log levels, enabled jobs) and job-specific settings. Each job declares its configuration schema; the core validates job configs against schemas and provides validated settings to jobs. Configuration supports enabling/disabling optional jobs, setting separate log levels for file and CLI, and job-specific parameters.

**Why this priority**: This is P1 because jobs need configuration to function, and users need the ability to customize behavior (especially disabling expensive or irrelevant jobs like k3s sync).

**Independent Test**: Can be fully tested by:
1. Creating a config file with various settings
2. Running sync and verifying jobs receive correct configuration
3. Testing invalid config triggers validation errors
4. Confirming log levels are applied correctly
5. Disabling an optional job and verifying it's skipped

This delivers value by enabling user customization and job parameterization.

**Constitution Alignment**:
- Frictionless Command UX (reasonable defaults, easy customization)
- Deliberate Simplicity (single config file, clear structure)

**Acceptance Scenarios**:

1. **Given** config file contains global settings and job sections, **When** orchestrator starts, **Then** it loads config, validates structure, applies defaults for missing values, and makes settings available to jobs via `job.config` accessor

2. **Given** config includes `log_file_level: DEBUG` and `log_cli_level: INFO`, **When** sync runs, **Then** file logging captures all events at DEBUG and above, while terminal UI shows only INFO and above

3. **Given** config includes `sync_jobs: { dummy_success: true, dummy_fail: false }`, **When** sync runs, **Then** dummy_success job executes and dummy_fail job is skipped (with INFO log: "dummy_fail job disabled by configuration")

4. **Given** a job declares required config parameters (e.g., Docker job requires `docker_preserve_cache: bool`), **When** config is missing this parameter and no default exists, **Then** orchestrator logs CRITICAL error during startup and refuses to run

5. **Given** config file has invalid YAML syntax, **When** orchestrator attempts to load it, **Then** the system displays clear parse error with line number and exits before attempting sync

**Example Configuration**:
```yaml
# ~/.config/pc-switcher/config.yaml
log_file_level: FULL
log_cli_level: INFO

# Jobs implemented in 001-foundation:
sync_jobs:
  dummy_success: true   # Test job that completes successfully
  dummy_fail: false     # Test job that fails at configurable progress %

# Job-specific configuration
btrfs_snapshots:
  # Configure these to match YOUR system's btrfs subvolume layout
  subvolumes:
    - "@"       # Example: root filesystem
    - "@home"   # Example: home directories

# Dummy job configuration (optional - defaults shown)
dummy_success:
  source_duration: 20   # Seconds to run on source
  target_duration: 20   # Seconds to run on target

dummy_fail:
  fail_at_percent: 60   # Progress % at which to fail
```

**Configuration Schema**: The formal configuration schema structure (global settings, sync_jobs section, and per-job settings) is defined in `specs/001-foundation/contracts/config-schema.yaml`. Job-specific settings appear as top-level keys (e.g., `btrfs_snapshots`, `user_data`) outside of the `sync_jobs` section.

---

### User Story 7 - Installation and Setup Infrastructure (Priority: P2)

The system provides installation and setup tooling to deploy pc-switcher to new machines and configure required infrastructure (packages, configuration). A setup script handles initial installation, dependency checking (including `uv` and `btrfs-progs`), and subvolume creation guidance.

**Installation Pattern**: Initial installation works without any prerequisites on the target machine. A simple `curl | sh` command (like many modern tools) downloads and runs the installation script, which installs prerequisites like `uv` if needed.

**Why this priority**: This is P2 because while essential for new users, developers can manually install during early development. Once the core sync system works, this becomes P1 for usability.

**Independent Test**: Can be fully tested by:
1. Running `curl -LsSf https://raw.githubusercontent.com/[owner]/pc-switcher/main/install.sh | sh` on a fresh Ubuntu 24.04 machine (without uv installed)
2. Verifying uv is installed if it was missing
3. Verifying all other dependencies are installed
4. Confirming pc-switcher package is installed
5. Checking that config directory is created with default config
6. Validating btrfs subvolume structure guidance is provided

This delivers value by streamlining initial deployment.

**Constitution Alignment**:
- Frictionless Command UX (simple installation process, no prerequisites)
- Proven Tooling Only (uses standard package managers)
- Deliberate Simplicity (shared installation logic between initial setup and target deployment)

**Acceptance Scenarios**:

1. **Given** a fresh Ubuntu 24.04 machine without uv installed, **When** user runs `curl -LsSf https://...install.sh | sh`, **Then** the script installs uv (if not present), installs btrfs-progs (if not present), installs pc-switcher via `uv tool install`, creates `~/.config/pc-switcher/` with default config, and displays "pc-switcher installed successfully"

2. **Given** pc-switcher sync installs on target (InstallOnTargetJob), **When** the target is missing uv, **Then** the same installation logic installs uv first, then installs/upgrades pc-switcher

3. **Given** `~/.config/pc-switcher/config.yaml` already exists, **When** user runs the installation script, **Then** the script prompts "Configuration file already exists. Overwrite? [y/N]" and preserves the existing file unless user confirms overwrite

---

### User Story 8 - Dummy Test Jobs (Priority: P1)

Two dummy jobs exist for testing infrastructure: `dummy_success` (completes successfully with INFO/WARNING/ERROR logs) and `dummy_fail` (raises unhandled exception to test exception handling). Each simulates long-running operations on both source and target with progress reporting.

**Why this priority**: This is P1 because these jobs are essential for testing the orchestrator, logging, progress UI, error handling, and interrupt handling during development. They serve as reference implementations of the job contract.

**Independent Test**: Each dummy job can be independently tested by enabling it in config and running sync, then verifying expected behavior.

**Constitution Alignment**:
- Deliberate Simplicity (provides clear reference implementation)
- Reliability Without Compromise (enables thorough testing)

**Acceptance Scenarios**:

1. **Given** `dummy_success` job is enabled, **When** sync runs, **Then** the job performs 20-second busy-wait on source (logging INFO message every 2s), emits WARNING at 6s, performs 20-second busy-wait on target (logging INFO message every 2s), emits ERROR at 8s, reports progress updates (0%, 25%, 50%, 75%, 100%), and completes successfully

2. *(Removed)*

3. **Given** `dummy_fail` job is enabled, **When** sync runs and job reaches 60% progress, **Then** the job raises a RuntimeError, the orchestrator catches the exception, logs it at CRITICAL level, requests job termination with cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py), and halts sync

4. **Given** any dummy job is running, **When** user presses Ctrl+C, **Then** the job receives termination request, it logs "Dummy job termination requested", stops its busy-wait loop within the grace period, and returns control to orchestrator

---

### User Story 9 - Terminal UI with Progress Reporting (Priority: P2)

The terminal displays real-time sync progress including current job, operation phase (validate/sync/cleanup), progress percentage, current item being processed, and log messages at configured CLI log level. UI updates smoothly without excessive redraws and gracefully handles terminal resize.

**Why this priority**: This is P2 because basic sync can work with simple log output to terminal. Rich progress UI significantly improves UX but isn't blocking for core functionality testing.

**Independent Test**: Can be tested by running sync with dummy jobs and verifying terminal shows progress bars, job names, log messages, and updates smoothly.

**Constitution Alignment**:
- Frictionless Command UX (clear feedback reduces uncertainty)

**Acceptance Scenarios**:

1. **Given** sync is running, **When** a job reports progress, **Then** terminal displays progress bar, percentage, current job name, and current operation (e.g., "Docker Sync: 45% - Copying image nginx:latest")

2. **Given** multiple jobs execute sequentially, **When** each completes, **Then** terminal shows overall progress (e.g., "Step 3/7: Package Sync") and individual job progress

3. **Given** a job emits log at INFO level or higher, **When** log reaches terminal UI, **Then** it's displayed below progress indicators with appropriate formatting (color-coded by level if terminal supports colors)

---

### Edge Cases

- What happens when target machine becomes unreachable mid-sync?
  - Orchestrator detects connection loss, logs CRITICAL error with diagnostic information, and aborts sync (no reconnection attempt)
- What happens when source machine crashes or powers off?
  - Target-side operations should timeout and cleanup after 5 minutes of no communication; next sync will detect inconsistent state via validation
- What happens when btrfs snapshots cannot be created due to insufficient space?
  - Snapshot job logs CRITICAL error with space usage details, orchestrator aborts before any state modification
- What happens when a job's cleanup logic raises an exception during termination?
  - Orchestrator logs the exception, continues with shutdown sequence (cleanup is best-effort)
- What happens when user runs multiple sync commands concurrently?
  - Second invocation detects lock, displays "Another sync is in progress (PID: 12345)", gives instructions on how to remove a stale lock and exits
- What happens when config file contains unknown job names?
  - Orchestrator logs ERROR "Unknown job 'xyz' in configuration, aborting" and aborts sync
- How does the system handle partial failures (some jobs succeed, some fail)?
  - Each job's success/failure is tracked independently; orchestrator logs summary at end showing which jobs succeeded/failed; overall sync is considered failed if any job fails
- What happens when target has newer pc-switcher version than source?
  - Orchestrator detects version mismatch, logs CRITICAL "Target version 0.5.0 is newer than source 0.4.0, this is unusual", and aborts sync to prevent accidental downgrade

## Requirements *(mandatory)*

### Functional Requirements

#### Job Architecture

- **FR-001** `[Deliberate Simplicity]` `[Reliability Without Compromise]`: System MUST define a standardized Job interface specifying how the orchestrator interacts with jobs, including how logging is done, how a job reports progress to the user, methods, error handling, termination request handling, timeout handling; the interface MUST include at least `validate()` and `execute()` methods, a mechanism to declare configuration schema, and property: `name: str`


- **FR-002** `[Reliability Without Compromise]`: System MUST call job lifecycle methods in order: `validate()` (all jobs), then `execute()` for each job in the specified order; on shutdown, errors, or user interrupt the system requests termination of the currently-executing job

- **FR-003** `[Reliability Without Compromise]`: System MUST request termination of currently-executing job when Ctrl+C is pressed, allowing cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py) for graceful cleanup; if job does not complete cleanup within timeout, orchestrator MUST force-terminate connections and the job, then proceed with exit

- **FR-004** `[Deliberate Simplicity]`: Jobs MUST be loaded from the configuration file section `sync_jobs` in the order they appear and instantiated by the orchestrator; job execution order is strictly sequential from config (no dependency resolution is provided—simplicity over flexibility)

#### Self-Installation

- **FR-005** `[Frictionless Command UX]`: System MUST check target machine's pc-switcher version before any other operations; if missing or mismatched, MUST install/upgrade to source version from public GitHub repository using `uv tool install git+https://github.com/[owner]/pc-switcher@v<version>` (no authentication required for public repository)

- **FR-006** `[Reliability Without Compromise]`: System MUST abort sync with CRITICAL log if the target machine's pc-switcher version is newer than the source version (preventing accidental downgrades)

- **FR-007** `[Frictionless Command UX]`: If installation/upgrade fails, system MUST log CRITICAL error and abort sync

#### Safety Infrastructure

- **FR-008** `[Reliability Without Compromise]`: System MUST create read-only btrfs snapshots of configured subvolumes on both source and target before any job executes (after version check and pre-checks)

- **FR-009** `[Reliability Without Compromise]`: System MUST create post-sync snapshots after all jobs complete successfully

- **FR-010** `[Minimize SSD Wear]`: Snapshot naming MUST follow pattern `{pre|post}-<subvolume>-<timestamp>` for clear identification and cleanup (e.g., `pre-@home-20251116T143022`); session folder provides session context

- **FR-011** `[Reliability Without Compromise]`: Snapshot management MUST be implemented as orchestrator-level infrastructure (not a SyncJob) that is always active; there is no configuration option to disable snapshot creation

- **FR-012** `[Frictionless Command UX]`: If pre-sync snapshot creation fails, system MUST log CRITICAL error and abort before any state modifications occur

- **FR-013** *(Removed - rollback capability is deferred to a separate feature after foundation infrastructure)*

- **FR-014** `[Minimize SSD Wear]`: System MUST provide snapshot cleanup command to delete old snapshots while retaining most recent N syncs; default retention policy (keep_recent count and max_age_days) MUST be configurable in the btrfs_snapshots job section of config.yaml

- **FR-015** `[Reliability Without Compromise]`: System MUST verify that all configured subvolumes exist on both source and target before attempting snapshots; if any are missing, system MUST log CRITICAL and abort

- **FR-015b** `[Reliability Without Compromise]`: System MUST verify that `/.snapshots/` is a btrfs subvolume (not a regular directory); if it does not exist, system MUST create it as a subvolume and inform the user; if it exists but is not a subvolume, system MUST log CRITICAL error and abort (to prevent recursive snapshots)

- **FR-016** `[Reliability Without Compromise]`: Orchestrator MUST check free disk space on both source and target before starting a sync; the minimum free-space threshold is configured via `disk_space_monitor.preflight_minimum` and MUST be specified as a percentage (e.g., "20%") or absolute value (e.g., "50GiB"); values without explicit units are invalid; default is "20%"

- **FR-017** `[Reliability Without Compromise]`: Orchestrator MUST monitor free disk space on source and target at a configurable interval (default: 30 seconds) during sync and abort with CRITICAL if available free space falls below the configured runtime minimum via `disk_space_monitor.runtime_minimum` (e.g., "15%" or "40GiB"); values without explicit units are invalid; default is "15%"

#### Logging System

- **FR-018**: System MUST implement six log levels with the following ordering and semantics: DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL, where DEBUG is the most verbose. DEBUG includes all messages (FULL, INFO, WARNING, ERROR, CRITICAL, plus internal diagnostics). FULL includes all messages from INFO and below plus operational details, but excludes DEBUG-level internal diagnostics.

- **FR-019** `[Reliability Without Compromise]`: When a job raises an exception, the orchestrator MUST log the error at CRITICAL level, request termination of the currently-executing job (queued jobs never execute and do not receive termination requests), and halt sync immediately

- **FR-020** `[Frictionless Command UX]`: System MUST support independent log level configuration for file output (`log_file_level`) and terminal display (`log_cli_level`)

- **FR-021**: System MUST write all logs at configured file level or above to timestamped file in `~/.local/share/pc-switcher/logs/sync-<timestamp>.log`

- **FR-022**: Log entries MUST use structlog's JSONRenderer for file output (one JSON object per line with keys: timestamp in ISO8601 format, level, job, hostname, event, plus any additional context fields) and ConsoleRenderer for terminal output (human-readable format with ISO8601 timestamp, level, job@hostname, and message)

- **FR-023** `[Reliability Without Compromise]`: System MUST aggregate logs from both source-side orchestrator and target-side operations into unified log stream

#### Interrupt Handling

- **FR-024** `[Reliability Without Compromise]`: System MUST install SIGINT handler that requests current job termination with cleanup timeout grace period (see `CLEANUP_TIMEOUT_SECONDS` in cli.py), logs "Sync interrupted by user" at WARNING level, and exits with code 130

- **FR-025** `[Reliability Without Compromise]`: On interrupt, system MUST send termination signal to any target-side processes and wait up to the cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py) for graceful shutdown

- **FR-026** `[Reliability Without Compromise]`: If second SIGINT is received during cleanup, system MUST immediately force-terminate without waiting

- **FR-027** `[Reliability Without Compromise]`: System MUST ensure no orphaned processes remain on source or target after interrupt

#### Configuration System

- **FR-028** `[Frictionless Command UX]`: System MUST load configuration from `~/.config/pc-switcher/config.yaml` on startup

- **FR-029** `[Deliberate Simplicity]`: Configuration MUST use YAML format with sections: global settings, `sync_jobs` (enable/disable), and per-job settings

- **FR-030** `[Reliability Without Compromise]`: System MUST validate configuration structure and job-specific settings against job-declared schemas (Python dicts conforming to JSON Schema draft-07, validated using jsonschema library) before execution

- **FR-031** `[Frictionless Command UX]`: System MUST apply reasonable defaults for missing configuration values

- **FR-032** `[Frictionless Command UX]`: System MUST allow enabling/disabling optional jobs via `sync_jobs: { module_name: true/false }`

- **FR-033** `[Reliability Without Compromise]`: If configuration file has syntax errors or invalid values, system MUST display clear error message with location and exit before sync

#### Installation & Setup

- **FR-035** `[Frictionless Command UX]`: System MUST provide installation script (`install.sh`) that can be run via `curl | sh` without prerequisites; the script installs uv (if not present) via `curl -LsSf https://astral.sh/uv/install.sh | sh`, installs btrfs-progs via apt-get (if not present), installs pc-switcher package via `uv tool install`, and creates default configuration; the installation logic MUST be shared with `InstallOnTargetJob` to ensure DRY compliance (btrfs filesystem is a documented prerequisite checked at runtime, not during installation)

- **FR-036** `[Up-to-date Documentation]`: Setup script MUST create default config file with inline comments explaining each setting

#### Testing Infrastructure

- **FR-038** `[Deliberate Simplicity]`: System MUST include two dummy jobs: `dummy-success`, `dummy-fail`

- **FR-039** `[Deliberate Simplicity]`: `dummy-success` MUST simulate 20s operation on source (log every 2s, WARNING at 6s) and 20s on target (log every 2s, ERROR at 8s), emit progress updates, and complete successfully

- **FR-040** *(Removed)*

- **FR-041** `[Reliability Without Compromise]`: `dummy-fail` MUST raise unhandled exception at 60% progress to test orchestrator exception handling

- **FR-042** `[Reliability Without Compromise]`: All dummy jobs MUST handle termination requests by logging "Dummy job termination requested" and stopping execution within the grace period

#### Progress Reporting

- **FR-043** `[Frictionless Command UX]`: Jobs CAN emit progress updates including percentage (0-100), current item description, and estimated completion time (progress updates are optional for jobs, but recommended for long-running operations; dummy test jobs emit progress for infrastructure testing)

- **FR-044** `[Frictionless Command UX]`: Orchestrator MUST forward progress updates to terminal UI system for display

- **FR-045**: Progress updates MUST be written to log file at FULL log level

#### Core Orchestration

- **FR-046** `[Frictionless Command UX]`: System MUST provide single command `pc-switcher sync <target>` that executes complete workflow

- **FR-047** `[Reliability Without Compromise]`: System MUST implement locking mechanism to prevent concurrent sync executions

- **FR-048**: System MUST log overall sync result (success/failure) and summary of job outcomes; summary MUST list each job with its result (SUCCESS/SKIPPED/FAILED), total duration, error count, and names of any jobs that failed

### Key Entities

- **Job**: Abstract base class for all sync components implementing the job interface; has name, config schema, and lifecycle methods. Concrete subclasses: **SystemJob** (required, always runs), **SyncJob** (configurable via `sync_jobs`), **BackgroundJob** (runs concurrently)
- **SyncSession**: Represents a single sync operation including session ID, timestamp, source/target machines, enabled jobs, and execution state
- **Snapshot**: Represents a btrfs snapshot including subvolume name, timestamp, session ID, type (pre/post), and location (source/target)
- **LogEntry**: Represents a logged event with timestamp, level, job name, message, and structured context data
- **ProgressUpdate**: Represents job progress including percentage, current item, estimated remaining time, and job name
- **Configuration**: Represents parsed and validated config including global settings, job enable/disable flags, and per-job settings
- **TargetConnection**: Represents the connection with methods for command execution, file transfer, process management, and connection loss detection/recovery
- **RemoteExecutor**: Represents the interface injected into jobs wrapping TargetConnection with simplified run_command(), send_file(), and get_hostname() methods

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** `[Frictionless Command UX]`: User executes complete sync with single command `pc-switcher sync <target>` without additional manual steps

- **SC-002** `[Reliability Without Compromise]`: System creates snapshots before and after sync in 100% of successful sync runs

- **SC-003** `[Reliability Without Compromise]`: System successfully aborts sync within the cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py) when CRITICAL error occurs, with no state modifications after abort

- **SC-004** `[Frictionless Command UX]`: System completes version check and installation/upgrade on target within 30 seconds

- **SC-005**: Log files contain complete audit trail of all operations with timestamps, levels, and job attribution in 100% of sync runs

- **SC-006** `[Reliability Without Compromise]`: User interrupt (Ctrl+C) results in graceful shutdown with no orphaned processes in 100% of tests

- **SC-007** `[Deliberate Simplicity]`: New feature job implementation requires only implementing job interface (< 200 lines of code for basic job) with no changes to core orchestrator

- **SC-008** `[Minimize SSD Wear]`: Btrfs snapshots use copy-on-write with zero initial write amplification (verified via btrfs filesystem usage commands)

- **SC-009** `[Frictionless Command UX]`: Installation script completes setup on fresh Ubuntu 24.04 machine in under 2 minutes with network connection

- **SC-010** `[Reliability Without Compromise]`: All three dummy jobs correctly demonstrate their expected behaviors (success, CRITICAL abort, exception handling) in 100% of test runs

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
- Automated testing infrastructure (CI/CD) - though dummy jobs enable manual testing
