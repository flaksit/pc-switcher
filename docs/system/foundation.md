# Foundation Infrastructure

This document is the **Living Truth** for pc-switcher's foundation infrastructure. It consolidates specifications from SpecKit runs into the authoritative, current definition of the system.

**Domain Code**: `FND` (Foundation)

## User Scenarios & Testing

### Job Architecture and Integration Contract (FND-US-JOB-ARCH)

Lineage: 001-US-1

Priority: P1

The system defines a precise contract for how sync jobs integrate with the core orchestration system. Each job (representing a discrete sync capability like package sync, Docker sync, or user data sync) implements a standardized interface covering configuration, validation, execution, logging, progress reporting, and error handling. This contract is detailed enough that all feature jobs can be developed independently and concurrently once the core infrastructure exists.

**Why this priority**: This is P1 because it's the architectural foundation. Without a clear, detailed job contract, subsequent features cannot be developed independently or correctly. This user story serves as the specification document for all future job developers. All sync-features (packages, Docker, VMs, k3s, user data) will be implemented as jobs. The btrfs snapshots safety infrastructure (FND-US-BTRFS) is orchestrator-level infrastructure (not configurable via sync_jobs). Self-installation (FND-US-SELF-INSTALL) is NOT a job—it is pre-job orchestrator logic that runs before any job execution.

**Independent Test**: Can be fully tested by:
1. Defining the job interface contract
2. Implementing a minimal test job that satisfies the contract
3. Registering it with the core orchestrator
4. Running sync and verifying the orchestrator correctly:
   - Loads the job configuration
   - Calls lifecycle methods in correct order (validate -> execute)
   - Handles job logging at all six levels
   - Processes progress updates
   - Handles job errors (exceptions)
   - Requests job termination on interrupts
5. Demonstrating that a developer can implement a new job by only implementing the contract

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

### Self-Installing Sync Orchestrator (FND-US-SELF-INSTALL)

Lineage: 001-US-2

Priority: P1

When a user initiates sync from source to target, the orchestrator ensures the pc-switcher package on the target machine is at the same version as the source machine. If versions differ or pc-switcher is not installed on target, the system automatically installs or upgrades the target installation. Additionally, the orchestrator ensures the target has the source's configuration file, prompting the user for confirmation before applying it. This installation/upgrade happens after pre-sync snapshots are created, providing rollback capability if installation fails.

**Why this priority**: This is P1 because version consistency is required for reliable sync operations. Without matching versions, the target-side helper scripts may be incompatible with source-side orchestration logic, causing unpredictable failures. Self-installation also eliminates manual setup steps, aligning with "Frictionless Command UX". Configuration sync ensures both machines operate with the same settings.

**Independent Test**: Can be fully tested by:
1. Setting up a target machine without pc-switcher installed
2. Running sync from source
3. Verifying orchestrator detects missing installation
4. Validating orchestrator installs pc-switcher on target
5. Checking that versions now match
6. Repeating with version mismatch (older version on target) to test upgrade path
7. Verifying config is copied to target after user confirmation
8. Testing config diff display when target has existing different config

**Constitution Alignment**:
- Frictionless Command UX (automated installation, no manual setup)
- Reliability Without Compromise (version consistency prevents compatibility issues, config consistency ensures predictable behavior)
- Deliberate Simplicity (self-contained deployment, no separate install process)

**Acceptance Scenarios**:

1. **Given** source machine has pc-switcher version 0.3.2 installed and target machine has no pc-switcher installed, **When** user runs `pc-switcher sync <target>`, **Then** the orchestrator detects missing installation, installs pc-switcher version 0.3.2 on target from GitHub repository using `uv tool install git+https://github.com/.../pc-switcher@v0.3.2`, verifies installation succeeded, and proceeds with sync

2. **Given** source has version 0.4.0 and target has version 0.3.2, **When** sync begins, **Then** orchestrator detects version mismatch, logs "Target pc-switcher version 0.3.2 is outdated, upgrading to 0.4.0", upgrades pc-switcher on target from GitHub repository using Git URL installation, and verifies upgrade completed

3. **Given** source and target both have version 0.4.0, **When** sync begins, **Then** orchestrator logs "Target pc-switcher version matches source (0.4.0), skipping installation" and proceeds immediately to next phase

4. **Given** installation/upgrade fails on target (e.g., disk full, permissions issue), **When** the failure occurs, **Then** orchestrator logs CRITICAL error and does not proceed with sync

5. **Given** target has no config file (`~/.config/pc-switcher/config.yaml`), **When** sync reaches config sync phase, **Then** orchestrator displays the source config content to the user, prompts "Apply this config to target? [y/N]", and if user confirms copies the config to target; if user declines, orchestrator aborts sync with message "Sync aborted: config required on target"

6. **Given** target has existing config that differs from source, **When** sync reaches config sync phase, **Then** orchestrator displays a diff between source and target configs, prompts user with three options: (a) Accept config from source, (b) Keep current config on target, (c) Abort sync; user selects the desired action

7. **Given** target config matches source config exactly, **When** sync reaches config sync phase, **Then** orchestrator logs "Target config matches source, skipping config sync" and proceeds without prompting

### Safety Infrastructure with Btrfs Snapshots (FND-US-BTRFS)

Lineage: 001-US-3

Priority: P1

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

7. **Given** snapshots accumulate over multiple sync runs, **When** user runs `pc-switcher cleanup-snapshots --older-than 7d`, **Then** the system deletes pre-sync and post-sync snapshots older than 7 days on that machine only, retaining the most recent 3 (configurable) sync sessions regardless of age (`--older-than` is optional; default is configurable); this command operates locally and does not affect snapshots on the remote machine

8. **Given** orchestrator configuration includes `disk_space_monitor.preflight_minimum` (percentage like "20%" or absolute value like "50GiB", default: "20%") for source and target, **When** sync begins, **Then** orchestrator MUST check free disk space on both source and target and log CRITICAL and abort if free space is below configured threshold

9. **Given** orchestrator configuration includes `disk_space_monitor.check_interval` (seconds, default: 30) and `disk_space_monitor.runtime_minimum` (percentage like "15%" or absolute value like "40GiB", default: "15%"), **When** sync is running, **Then** orchestrator MUST periodically check free disk space at the configured interval and log CRITICAL and abort if available free space falls below the configured runtime minimum on either side

### Graceful Interrupt Handling (FND-US-INTERRUPT)

Lineage: 001-US-5

Priority: P1

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

**Constitution Alignment**:
- Frictionless Command UX (user maintains control)
- Reliability Without Compromise (clean shutdown prevents inconsistent state)

**Acceptance Scenarios**:

1. **Given** sync operation is in progress with a job executing on target machine, **When** user presses Ctrl+C, **Then** the orchestrator catches SIGINT, logs "Sync interrupted by user" at WARNING level, requests termination of the current job, sends termination signal to target-side processes, waits up to the cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py) for graceful cleanup, then closes connection and exits with code 130

2. **Given** sync is in the orchestrator phase between jobs (no job actively running), **When** user presses Ctrl+C, **Then** orchestrator logs interruption, skips remaining jobs, and exits cleanly

3. **Given** user presses Ctrl+C multiple times rapidly, **When** the second SIGINT arrives before cleanup completes, **Then** orchestrator immediately force-terminates (kills connection, exits with code 130) without waiting for graceful cleanup

### Configuration System (FND-US-CONFIG)

Lineage: 001-US-6

Priority: P1

The system loads configuration from `~/.config/pc-switcher/config.yaml` covering global settings (log levels, enabled jobs) and job-specific settings. Each job declares its configuration schema; the core validates job configs against schemas and provides validated settings to jobs. Configuration supports enabling/disabling optional jobs, setting separate log levels for file and CLI, and job-specific parameters.

**Why this priority**: This is P1 because jobs need configuration to function, and users need the ability to customize behavior (especially disabling expensive or irrelevant jobs like k3s sync).

**Independent Test**: Can be fully tested by:
1. Creating a config file with various settings
2. Running sync and verifying jobs receive correct configuration
3. Testing invalid config triggers validation errors
4. Confirming log levels are applied correctly
5. Disabling an optional job and verifying it's skipped

**Constitution Alignment**:
- Frictionless Command UX (reasonable defaults, easy customization)
- Deliberate Simplicity (single config file, clear structure)

**Acceptance Scenarios**:

1. **Given** config file contains global settings and job sections, **When** orchestrator starts, **Then** it loads config, validates structure, applies defaults for missing values, and makes settings available to jobs via `job.config` accessor

2. **Given** config includes `logging: { file: DEBUG, tui: INFO, external: WARNING }`, **When** sync runs, **Then** file logging captures all events at DEBUG and above, while terminal UI shows only INFO and above, and external library logs are filtered at WARNING

3. **Given** config includes `sync_jobs: { dummy_success: true, dummy_fail: false }`, **When** sync runs, **Then** dummy_success job executes and dummy_fail job is skipped (with INFO log: "dummy_fail job disabled by configuration")

4. **Given** a job declares required config parameters (e.g., Docker job requires `docker_preserve_cache: bool`), **When** config is missing this parameter and no default exists, **Then** orchestrator logs CRITICAL error during startup and refuses to run

5. **Given** config file has invalid YAML syntax, **When** orchestrator attempts to load it, **Then** the system displays clear parse error with line number and exits before attempting sync

**Example Configuration**:
```yaml
# ~/.config/pc-switcher/config.yaml
# Logging configuration (3-setting model)
logging:
  file: FULL      # Floor level for log file output
  tui: INFO       # Floor level for terminal output
  external: WARNING  # Floor for third-party libraries

# Jobs implemented in 001-foundation:
sync_jobs:
  dummy_success: true   # Test job that completes successfully
  dummy_fail: false     # Test job that fails at configurable time

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
  source_duration: 10   # Seconds to run on source
  target_duration: 10   # Seconds to run on target
  fail_at: 12           # Elapsed seconds at which to fail
```

**Configuration Schema**: The formal configuration schema structure (global settings, sync_jobs section, and per-job settings) is defined in `specs/001-foundation/contracts/config-schema.yaml`. Job-specific settings appear as top-level keys (e.g., `btrfs_snapshots`, `user_data`) outside of the `sync_jobs` section.

### Installation and Setup Infrastructure (FND-US-INSTALL)

Lineage: 001-US-7

Priority: P2

The system provides installation and setup tooling to deploy pc-switcher to new machines and configure required infrastructure (packages, configuration). A setup script handles initial installation, dependency checking (including `uv` and `btrfs-progs`), and subvolume creation guidance.

**Installation Pattern**: Initial installation works without any prerequisites on the target machine. A simple `curl | sh` command (like many modern tools) downloads and runs the installation script, which installs prerequisites like `uv` if needed.

**Why this priority**: This is P2 because while essential for new users, developers can manually install during early development. Once the core sync system works, this becomes P1 for usability.

**Independent Test**: Can be fully tested by:
1. Running `curl -LsSf https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash` on a fresh Ubuntu 24.04 machine (without uv installed)
2. Verifying uv is installed if it was missing
3. Verifying all other dependencies are installed
4. Confirming pc-switcher package is installed
5. Checking that config directory is created with default config
6. Validating btrfs subvolume structure guidance is provided

**Constitution Alignment**:
- Frictionless Command UX (simple installation process, no prerequisites)
- Proven Tooling Only (uses standard package managers)
- Deliberate Simplicity (shared installation logic between initial setup and target deployment)

**Acceptance Scenarios**:

1. **Given** a fresh Ubuntu 24.04 machine without uv installed, **When** user runs `curl -LsSf https://...install.sh | bash`, **Then** the script installs uv (if not present), installs btrfs-progs (if not present), installs pc-switcher via `uv tool install`, creates `~/.config/pc-switcher/` with default config, and displays "pc-switcher installed successfully"

2. **Given** pc-switcher sync installs on target (InstallOnTargetJob), **When** the target is missing uv, **Then** the same installation logic installs uv first, then installs/upgrades pc-switcher

3. **Given** `~/.config/pc-switcher/config.yaml` already exists, **When** user runs the installation script, **Then** the script prompts "Configuration file already exists. Overwrite? [y/N]" and preserves the existing file unless user confirms overwrite

### Dummy Test Jobs (FND-US-DUMMY)

Lineage: 001-US-8

Priority: P1

Two dummy jobs exist for testing infrastructure: `dummy_success` (completes successfully with INFO/WARNING/ERROR logs) and `dummy_fail` (raises unhandled exception to test exception handling). Each simulates long-running operations on both source and target with progress reporting.

**Why this priority**: This is P1 because these jobs are essential for testing the orchestrator, logging, progress UI, error handling, and interrupt handling during development. They serve as reference implementations of the job contract.

**Independent Test**: Each dummy job can be independently tested by enabling it in config and running sync, then verifying expected behavior.

**Constitution Alignment**:
- Deliberate Simplicity (provides clear reference implementation)
- Reliability Without Compromise (enables thorough testing)

**Acceptance Scenarios**:

1. **Given** `dummy_success` job is enabled, **When** sync runs, **Then** the job performs 20-second busy-wait on source (logging INFO message every 2s), emits WARNING at 6s, performs 20-second busy-wait on target (logging INFO message every 2s), emits ERROR at 8s, reports progress updates (0%, 25%, 50%, 75%, 100%), and completes successfully

2. **Given** `dummy_fail` job is enabled, **When** sync runs and job reaches the configured `fail_at` time, **Then** the job raises a RuntimeError, the orchestrator catches the exception, logs it at CRITICAL level, requests job termination with cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py), and halts sync

3. **Given** any dummy job is running, **When** user presses Ctrl+C, **Then** the job receives termination request, it logs "Dummy job termination requested", stops its busy-wait loop within the grace period, and returns control to orchestrator

### Terminal UI with Progress Reporting (FND-US-TUI)

Lineage: 001-US-9

Priority: P2

The terminal displays real-time sync progress including current job, operation phase (validate/sync/cleanup), progress percentage, current item being processed, and log messages at configured CLI log level. UI updates smoothly without excessive redraws and gracefully handles terminal resize.

**Why this priority**: This is P2 because basic sync can work with simple log output to terminal. Rich progress UI significantly improves UX but isn't blocking for core functionality testing.

**Independent Test**: Can be tested by running sync with dummy jobs and verifying terminal shows progress bars, job names, log messages, and updates smoothly.

**Constitution Alignment**:
- Frictionless Command UX (clear feedback reduces uncertainty)

**Acceptance Scenarios**:

1. **Given** sync is running, **When** a job reports progress, **Then** terminal displays progress bar, percentage, current job name, and current operation (e.g., "Docker Sync: 45% - Copying image nginx:latest")

2. **Given** multiple jobs execute sequentially, **When** each completes, **Then** terminal shows overall progress (e.g., "Step 3/7: Package Sync") and individual job progress

3. **Given** a job emits log at INFO level or higher, **When** log reaches terminal UI, **Then** it's displayed below progress indicators with appropriate formatting (color-coded by level if terminal supports colors)

### Spec-Driven Test Coverage for Foundation (FND-US-TEST-COVERAGE)

Lineage: 003-US-1

Priority: P1

As a pc-switcher developer, I have comprehensive tests that verify 100% of the specifications defined in the foundation specification. Tests are written based on the spec (user stories, acceptance scenarios, functional requirements), not the implementation code. If any part of the spec was not implemented or implemented incorrectly, the tests fail.

**Why this priority**: P1 because the existing foundation code is critical infrastructure. Bugs could break entire systems. Spec-driven tests ensure the implementation matches the documented requirements and catch gaps or deviations.

**Independent Test**: Can be verified by running the full test suite and confirming tests exist for every user story, acceptance scenario, and functional requirement in the foundation spec.

**Acceptance Scenarios**:

1. **Given** tests are implemented based on foundation spec, **When** I run the full test suite, **Then** 100% of user stories have corresponding test coverage

2. **Given** tests are implemented based on foundation spec, **When** I run the full test suite, **Then** 100% of acceptance scenarios have corresponding test cases

3. **Given** tests are implemented based on foundation spec, **When** I run the full test suite, **Then** 100% of functional requirements have corresponding test assertions

4. **Given** a part of the spec was not implemented or implemented incorrectly, **When** I run the tests, **Then** the relevant tests fail, exposing the gap or bug

5. **Given** tests cover both success and failure paths, **When** I run the full test suite, **Then** error handling, edge cases, and boundary conditions from the spec are all verified

### Traceability from Tests to Spec (FND-US-TEST-TRACE)

Lineage: 003-US-2

Priority: P2

As a pc-switcher developer, I can trace each test back to the specific requirement it validates. When a test fails, I can quickly identify which part of the foundation spec is affected.

**Why this priority**: P2 because traceability improves debugging and maintenance but the tests themselves are more critical.

**Independent Test**: Can be verified by examining test names/docstrings and confirming they reference specific requirements from foundation spec.

**Acceptance Scenarios**:

1. **Given** I look at any test for foundation, **When** I read the test name or docstring, **Then** I can identify the specific user story, acceptance scenario, or FR being tested

2. **Given** a test fails in CI, **When** I review the failure output, **Then** I can immediately navigate to the corresponding spec requirement

### Edge Cases

Lineage: 001-foundation edge cases, 003-foundation-tests edge cases

- **What happens when target machine becomes unreachable mid-sync?**
  - Orchestrator detects connection loss, logs CRITICAL error with diagnostic information, and aborts sync (no reconnection attempt)

- **What happens when source machine crashes or powers off?**
  - Target-side operations should timeout and cleanup after 5 minutes of no communication; next sync will detect inconsistent state via validation

- **What happens when btrfs snapshots cannot be created due to insufficient space?**
  - Snapshot job logs CRITICAL error with space usage details, orchestrator aborts before any state modification

- **What happens when a job's cleanup logic raises an exception during termination?**
  - Orchestrator logs the exception, continues with shutdown sequence (cleanup is best-effort)

- **What happens when user runs multiple sync commands concurrently?**
  - Second invocation detects lock, displays "Another sync is in progress (PID: 12345)", gives instructions on how to remove a stale lock and exits

- **What happens when config file contains unknown job names?**
  - Orchestrator logs ERROR "Unknown job 'xyz' in configuration, aborting" and aborts sync

- **How does the system handle partial failures (some jobs succeed, some fail)?**
  - Each job's success/failure is tracked independently; orchestrator logs summary at end showing which jobs succeeded/failed; overall sync is considered failed if any job fails

- **What happens when target has newer pc-switcher version than source?**
  - Orchestrator detects version mismatch, logs CRITICAL "Target version 0.5.0 is newer than source 0.4.0, this is unusual", and aborts sync to prevent accidental downgrade

- **What happens when tests find a gap between spec and implementation?**
  - Tests fail with clear assertion messages indicating which spec requirement is not met

- **What happens when a spec requirement is ambiguous?**
  - Test documents the interpretation used; if implementation differs, test fails and forces clarification

- **What happens when implementation has functionality not in spec?**
  - Such functionality should be tested as well, but a warning should be raised to the user to consider updating the spec

## Requirements

### Functional Requirements

#### Job Architecture

- **FND-FR-JOB-IFACE** `[Deliberate Simplicity]` `[Reliability Without Compromise]`: System MUST define a standardized Job interface specifying how the orchestrator interacts with jobs, including how logging is done, how a job reports progress to the user, methods, error handling, termination request handling, timeout handling; the interface MUST include at least `validate()` and `execute()` methods, a mechanism to declare configuration schema, and property: `name: str`  
  Lineage: 001-FR-001

- **FND-FR-LIFECYCLE** `[Reliability Without Compromise]`: System MUST call job lifecycle methods in order: `validate()` (all jobs), then `execute()` for each job in the specified order; on shutdown, errors, or user interrupt the system requests termination of the currently-executing job  
  Lineage: 001-FR-002

- **FND-FR-TERM-CTRLC** `[Reliability Without Compromise]`: System MUST request termination of currently-executing job when Ctrl+C is pressed, allowing cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py) for graceful cleanup; if job does not complete cleanup within timeout, orchestrator MUST force-terminate connections and the job, then proceed with exit  
  Lineage: 001-FR-003

- **FND-FR-JOB-LOAD** `[Deliberate Simplicity]`: Jobs MUST be loaded from the configuration file section `sync_jobs` in the order they appear and instantiated by the orchestrator; job execution order is strictly sequential from config (no dependency resolution is provided—simplicity over flexibility)  
  Lineage: 001-FR-004

#### Self-Installation

- **FND-FR-VERSION-CHECK** `[Frictionless Command UX]`: System MUST check target machine's pc-switcher version before any other operations; if missing or mismatched, MUST install/upgrade to source version from public GitHub repository using `uv tool install git+https://github.com/[owner]/pc-switcher@v<version>` (no authentication required for public repository)  
  Lineage: 001-FR-005

- **FND-FR-VERSION-NEWER** `[Reliability Without Compromise]`: System MUST abort sync with CRITICAL log if the target machine's pc-switcher version is newer than the source version (preventing accidental downgrades)  
  Lineage: 001-FR-006

- **FND-FR-INSTALL-FAIL** `[Frictionless Command UX]`: If installation/upgrade fails, system MUST log CRITICAL error and abort sync  
  Lineage: 001-FR-007

- **FND-FR-CONFIG-SYNC** `[Reliability Without Compromise]`: After pc-switcher installation/upgrade, system MUST sync configuration from source to target; if target has no config, system MUST display source config and prompt user for confirmation before copying; if user declines, system MUST abort sync  
  Lineage: 001-FR-007a

- **FND-FR-CONFIG-DIFF** `[Frictionless Command UX]`: If target has existing config that differs from source, system MUST display a diff and prompt user with three options: (a) Accept config from source, (b) Keep current config on target, (c) Abort sync  
  Lineage: 001-FR-007b

- **FND-FR-CONFIG-MATCH** `[Frictionless Command UX]`: If target config matches source config exactly, system MUST skip config sync with INFO log and proceed without prompting  
  Lineage: 001-FR-007c

#### Safety Infrastructure (Btrfs Snapshots)

- **FND-FR-SNAP-PRE** `[Reliability Without Compromise]`: System MUST create read-only btrfs snapshots of configured subvolumes on both source and target before any job executes (after version check and pre-checks)  
  Lineage: 001-FR-008

- **FND-FR-SNAP-POST** `[Reliability Without Compromise]`: System MUST create post-sync snapshots after all jobs complete successfully  
  Lineage: 001-FR-009

- **FND-FR-SNAP-NAME** `[Minimize SSD Wear]`: Snapshot naming MUST follow pattern `{pre|post}-<subvolume>-<timestamp>` for clear identification and cleanup (e.g., `pre-@home-20251116T143022`); session folder provides session context  
  Lineage: 001-FR-010

- **FND-FR-SNAP-ALWAYS** `[Reliability Without Compromise]`: Snapshot management MUST be implemented as orchestrator-level infrastructure (not a SyncJob) that is always active; there is no configuration option to disable snapshot creation  
  Lineage: 001-FR-011

- **FND-FR-SNAP-FAIL** `[Frictionless Command UX]`: If pre-sync snapshot creation fails, system MUST log CRITICAL error and abort before any state modifications occur  
  Lineage: 001-FR-012

- **FND-FR-SNAP-CLEANUP** `[Minimize SSD Wear]`: System MUST provide snapshot cleanup command to delete old snapshots while retaining most recent N syncs; default retention policy (keep_recent count and max_age_days) MUST be configurable in the btrfs_snapshots job section of config.yaml  
  Lineage: 001-FR-014

- **FND-FR-SUBVOL-EXIST** `[Reliability Without Compromise]`: System MUST verify that all configured subvolumes exist on both source and target before attempting snapshots; if any are missing, system MUST log CRITICAL and abort  
  Lineage: 001-FR-015

- **FND-FR-SNAPDIR** `[Reliability Without Compromise]`: System MUST verify that `/.snapshots/` is a btrfs subvolume (not a regular directory); if it does not exist, system MUST create it as a subvolume and inform the user; if it exists but is not a subvolume, system MUST log CRITICAL error and abort (to prevent recursive snapshots)  
  Lineage: 001-FR-015b

- **FND-FR-DISK-PRE** `[Reliability Without Compromise]`: Orchestrator MUST check free disk space on both source and target before starting a sync; the minimum free-space threshold is configured via `disk_space_monitor.preflight_minimum` and MUST be specified as a percentage (e.g., "20%") or absolute value (e.g., "50GiB"); values without explicit units are invalid; default is "20%"  
  Lineage: 001-FR-016

- **FND-FR-DISK-RUNTIME** `[Reliability Without Compromise]`: Orchestrator MUST monitor free disk space on source and target at a configurable interval (default: 30 seconds) during sync and abort with CRITICAL if available free space falls below the configured runtime minimum via `disk_space_monitor.runtime_minimum` (e.g., "15%" or "40GiB"); values without explicit units are invalid; default is "15%"  
  Lineage: 001-FR-017

#### Interrupt Handling

- **FND-FR-SIGINT** `[Reliability Without Compromise]`: System MUST install SIGINT handler that requests current job termination with cleanup timeout grace period (see `CLEANUP_TIMEOUT_SECONDS` in cli.py), logs "Sync interrupted by user" at WARNING level, and exits with code 130  
  Lineage: 001-FR-024

- **FND-FR-TARGET-TERM** `[Reliability Without Compromise]`: On interrupt, system MUST send termination signal to any target-side processes and wait up to the cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py) for graceful shutdown  
  Lineage: 001-FR-025

- **FND-FR-FORCE-TERM** `[Reliability Without Compromise]`: Force-terminate on second SIGINT - When a second SIGINT arrives before cleanup completes, the system immediately force-terminates without waiting for graceful cleanup.  
  Lineage: 001-FR-026

- **FND-FR-NO-ORPHAN** `[Reliability Without Compromise]`: System MUST ensure no orphaned processes remain on source or target after interrupt  
  Lineage: 001-FR-027

#### Configuration System

- **FND-FR-CONFIG-LOAD** `[Frictionless Command UX]`: System MUST load configuration from `~/.config/pc-switcher/config.yaml` on startup  
  Lineage: 001-FR-028

- **FND-FR-CONFIG-FORMAT** `[Deliberate Simplicity]`: Configuration MUST use YAML format with sections: global settings, `sync_jobs` (enable/disable), and per-job settings  
  Lineage: 001-FR-029

- **FND-FR-CONFIG-VALIDATE** `[Reliability Without Compromise]`: System MUST validate configuration structure and job-specific settings against job-declared schemas (Python dicts conforming to JSON Schema draft-07, validated using jsonschema library) before execution  
  Lineage: 001-FR-030

- **FND-FR-CONFIG-DEFAULTS** `[Frictionless Command UX]`: System MUST apply reasonable defaults for missing configuration values  
  Lineage: 001-FR-031

- **FND-FR-JOB-ENABLE** `[Frictionless Command UX]`: System MUST allow enabling/disabling optional jobs via `sync_jobs: { module_name: true/false }`  
  Lineage: 001-FR-032

- **FND-FR-CONFIG-ERROR** `[Reliability Without Compromise]`: If configuration file has syntax errors or invalid values, system MUST display clear error message with location and exit before sync  
  Lineage: 001-FR-033

#### Installation & Setup

- **FND-FR-INSTALL-SCRIPT** `[Frictionless Command UX]`: System MUST provide installation script (`install.sh`) that can be run via `curl | sh` without prerequisites; the script installs uv (if not present) via `curl -LsSf https://astral.sh/uv/install.sh | sh`, installs btrfs-progs via apt-get (if not present), installs pc-switcher package via `uv tool install`, and creates default configuration; the installation logic MUST be shared with `InstallOnTargetJob` to ensure DRY compliance (btrfs filesystem is a documented prerequisite checked at runtime, not during installation)  
  Lineage: 001-FR-035

- **FND-FR-DEFAULT-CONFIG** `[Up-to-date Documentation]`: Setup script MUST create default config file with inline comments explaining each setting  
  Lineage: 001-FR-036

#### Testing Infrastructure (Dummy Jobs)

- **FND-FR-DUMMY-JOBS**: System MUST include two dummy jobs: `dummy-success`, `dummy-fail`  
  Lineage: 001-FR-038

- **FND-FR-DUMMY-SIM**: `dummy-success` and `dummy-fail` MUST simulate an operation of configurable duration on source (log every 2s, log WARNING at 6s) and of configurable duration on target (log every 2s, log ERROR at 8s), emit progress updates, and complete successfully  
  Lineage: 001-FR-039

- **FND-FR-DUMMY-EXCEPTION** `[Reliability Without Compromise]`: `dummy-fail` MUST raise unhandled exception at configurable time to test orchestrator exception handling on both source and target  
  Lineage: 001-FR-041

- **FND-FR-DUMMY-TERM** `[Reliability Without Compromise]`: All dummy jobs MUST handle termination requests by logging "Dummy job termination requested" and stopping execution within the grace period  
  Lineage: 001-FR-042

#### Progress Reporting

- **FND-FR-PROGRESS-EMIT** `[Frictionless Command UX]`: Jobs CAN emit progress updates including percentage (0-100), current item description, and estimated completion time (progress updates are optional for jobs, but recommended for long-running operations; dummy test jobs emit progress for infrastructure testing)  
  Lineage: 001-FR-043

- **FND-FR-PROGRESS-FWD** `[Frictionless Command UX]`: Orchestrator MUST forward progress updates to terminal UI system for display  
  Lineage: 001-FR-044

- **FND-FR-PROGRESS-LOG**: Progress updates MUST be written to log file at FULL log level  
  Lineage: 001-FR-045

#### Core Orchestration

- **FND-FR-SYNC-CMD** `[Frictionless Command UX]`: System MUST provide single command `pc-switcher sync <target>` that executes complete workflow  
  Lineage: 001-FR-046

- **FND-FR-LOCK** `[Reliability Without Compromise]`: System MUST implement locking mechanism to prevent concurrent sync executions  
  Lineage: 001-FR-047

- **FND-FR-SUMMARY**: System MUST log overall sync result (success/failure) and summary of job outcomes; summary MUST list each job with its result (SUCCESS/SKIPPED/FAILED), total duration, error count, and names of any jobs that failed  
  Lineage: 001-FR-048

### Foundation Test Requirements

#### Test Coverage Requirements

- **FND-FR-TEST-US**: Tests MUST cover 100% of user stories defined in foundation specification  
  Lineage: 003-FR-001

- **FND-FR-TEST-AS**: Tests MUST cover 100% of acceptance scenarios defined in foundation specification  
  Lineage: 003-FR-002

- **FND-FR-TEST-FR**: Tests MUST cover 100% of functional requirements defined in foundation specification  
  Lineage: 003-FR-003

- **FND-FR-TEST-PATHS**: Tests MUST verify both success paths and failure paths (error handling, edge cases, boundary conditions) for each requirement  
  Lineage: 003-FR-004

#### Test Organization Requirements

- **FND-FR-TEST-UNIT-DIR**: Unit tests for foundation MUST be placed in `tests/unit/` directory following module structure  
  Lineage: 003-FR-005

- **FND-FR-TEST-INT-DIR**: Integration tests for foundation MUST be placed in `tests/integration/` directory  
  Lineage: 003-FR-006

- **FND-FR-TEST-DOCSTRING**: Each test file MUST include docstrings or comments referencing the spec requirements being tested  
  Lineage: 003-FR-007

- **FND-FR-TEST-NAMING**: Test function names MUST indicate the requirement being tested (e.g., `test_fr001_connection_ssh_authentication`)  
  Lineage: 003-FR-008

#### Test Quality Requirements

- **FND-FR-TEST-INDEP**: Tests MUST be independent and not rely on execution order or shared mutable state between tests  
  Lineage: 003-FR-009

- **FND-FR-TEST-FIXTURES**: Tests MUST use fixtures from the testing framework for VM access, event buses, and cleanup  
  Lineage: 003-FR-010

- **FND-FR-TEST-MOCK**: Unit tests MUST use mock executors to avoid real system operations  
  Lineage: 003-FR-011

- **FND-FR-TEST-REAL**: Integration tests MUST execute real operations on test VMs  
  Lineage: 003-FR-012

#### Test Performance Requirements

- **FND-FR-TEST-SPEED**: Unit tests MUST complete full suite execution in under 30 seconds  
  Lineage: 003-FR-013

### Key Entities

Lineage: 001-foundation Key Entities, 003-foundation-tests Key Entities

- **Job**: Abstract base class for all sync components implementing the job interface; has name, config schema, and lifecycle methods. Concrete subclasses: **SystemJob** (required, always runs), **SyncJob** (configurable via `sync_jobs`), **BackgroundJob** (runs concurrently)

- **SyncSession**: Represents a single sync operation including session ID, timestamp, source/target machines, enabled jobs, and execution state

- **Snapshot**: Represents a btrfs snapshot including subvolume name, timestamp, session ID, type (pre/post), and location (source/target)

- **LogEntry**: Represents a logged event with timestamp, level, job name, message, and structured context data

- **ProgressUpdate**: Represents job progress including percentage, current item, estimated remaining time, and job name

- **Configuration**: Represents parsed and validated config including global settings, job enable/disable flags, and per-job settings

- **TargetConnection**: Represents the connection with methods for command execution, file transfer, process management, and connection loss detection/recovery

- **RemoteExecutor**: Represents the interface injected into jobs wrapping TargetConnection with simplified run_command(), send_file(), and get_hostname() methods

- **SpecRequirement**: Represents a requirement from foundation spec; has ID (FR-xxx, US-xxx, AS-xxx), description, and test status

- **TestMapping**: Represents the mapping between a spec requirement and its corresponding tests; enables traceability

- **CoverageReport**: Represents the summary of which spec requirements have tests and which are missing

## Success Criteria

### Core Infrastructure

- **FND-SC-SINGLE-CMD** `[Frictionless Command UX]`: User executes complete sync with single command `pc-switcher sync <target>` without additional manual steps
  Lineage: 001-SC-001

- **FND-SC-SNAPSHOTS** `[Reliability Without Compromise]`: System creates snapshots before and after sync in 100% of successful sync runs
  Lineage: 001-SC-002

- **FND-SC-ABORT** `[Reliability Without Compromise]`: System successfully aborts sync within the cleanup timeout (see `CLEANUP_TIMEOUT_SECONDS` in cli.py) when CRITICAL error occurs, with no state modifications after abort
  Lineage: 001-SC-003

- **FND-SC-VERSION-TIME** `[Frictionless Command UX]`: System completes version check and installation/upgrade on target within 30 seconds
  Lineage: 001-SC-004

- **FND-SC-AUDIT**: Log files contain complete audit trail of all operations with timestamps, levels, and job attribution in 100% of sync runs
  Lineage: 001-SC-005

- **FND-SC-GRACEFUL** `[Reliability Without Compromise]`: User interrupt (Ctrl+C) results in graceful shutdown with no orphaned processes in 100% of tests
  Lineage: 001-SC-006

- **FND-SC-JOB-SIMPLE** `[Deliberate Simplicity]`: New feature job implementation requires only implementing job interface (< 200 lines of code for basic job) with no changes to core orchestrator
  Lineage: 001-SC-007

- **FND-SC-COW** `[Minimize SSD Wear]`: Btrfs snapshots use copy-on-write with zero initial write amplification (verified via btrfs filesystem usage commands)
  Lineage: 001-SC-008

- **FND-SC-INSTALL-TIME** `[Frictionless Command UX]`: Installation script completes setup on fresh Ubuntu 24.04 machine in under 2 minutes with network connection
  Lineage: 001-SC-009

- **FND-SC-DUMMY-DEMO** `[Reliability Without Compromise]`: All dummy jobs correctly demonstrate their expected behaviors (success, CRITICAL abort, exception handling) in 100% of test runs
  Lineage: 001-SC-010

### Test Success Criteria

- **FND-SC-TEST-US**: 100% of user stories in foundation spec have corresponding test coverage
  Lineage: 003-SC-001

- **FND-SC-TEST-AS**: 100% of acceptance scenarios in foundation spec have corresponding test cases
  Lineage: 003-SC-002

- **FND-SC-TEST-FR**: 100% of functional requirements in foundation spec have corresponding test assertions
  Lineage: 003-SC-003

- **FND-SC-TEST-PATHS**: All tests verify both success and failure paths as specified in the requirements
  Lineage: 003-SC-004

- **FND-SC-TEST-TRACE**: All test files include traceability references to spec requirements
  Lineage: 003-SC-005

- **FND-SC-TEST-GAPS**: Running the test suite surfaces any gaps between spec and implementation through failing tests
  Lineage: 003-SC-006

- **FND-SC-TEST-UNIT-SPEED**: Unit test suite executes completely in under 30 seconds on a standard development machine
  Lineage: 003-SC-007

- **FND-SC-TEST-INT-SPEED**: Integration tests complete full VM-based testing in under 15 minutes
  Lineage: 003-SC-008

## Assumptions

Lineage: 001-foundation Assumptions, 003-foundation-tests Assumptions

- Source and target machines run Ubuntu 24.04 LTS with btrfs filesystems
- User has sudo privileges on both machines for operations requiring elevation
- Machines are reachable via SSH (LAN, VPN such as Tailscale, or other network) during sync operations
- Terminal emulator supports ANSI escape codes for progress UI
- User's `~/.ssh/config` contains target machine configurations if using aliases
- Sufficient disk space exists on target for package installation
- No other tools are simultaneously modifying the same system state during sync
- Testing framework infrastructure from specs/002-testing-framework/spec.md is implemented and operational
- Foundation implementation exists and is testable

## Out of Scope

Lineage: 001-foundation Out of Scope, 003-foundation-tests Out of Scope

- Implementation of user-facing sync features (user data, packages, Docker, VMs, k3s) - those are separate feature specs (features 4-9)
- Bi-directional sync or conflict resolution between divergent states
- Automatic sync scheduling or daemon mode
- GUI or web interface
- Windows or macOS support
- Non-btrfs filesystems
- Multi-user concurrent usage
- Automated testing infrastructure (CI/CD) - though dummy jobs enable manual testing
- Tests for features beyond foundation (those will have their own test specs)
- Testing implementation details not specified in foundation spec
- Fixing bugs found by these tests (separate bug fix tasks)
- Updating foundation spec if gaps are found (separate spec update task)
- Test coverage for third-party libraries (only test project code)
