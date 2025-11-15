# Tasks: Foundation Infrastructure Complete

**Feature**: Foundation Infrastructure Complete
**Branch**: `001-foundation`
**Input**: Design documents from `/specs/001-foundation/`

**Tests**: Tests are NOT requested in this specification. All test tasks are excluded.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions and tag the dominant principle in parentheses

## Path Conventions

Repository structure (single Python package):
- `src/pcswitcher/` - Main package code
- `tests/` - Test suite
- `scripts/` - Bundled scripts
- `.github/workflows/` - CI/CD

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [ ] T001 Create .tool-versions file with uv 0.9.9 (Proven Tooling Only)
- [ ] T002 Initialize Python 3.13 environment with uv and create pyproject.toml with dependencies: fabric, structlog, rich, typer, pyyaml, hatchling, uv-dynamic-versioning (Proven Tooling Only)
- [ ] T003 [P] Create project directory structure: src/pcswitcher/{cli,core,remote,modules,utils}/, tests/{unit,integration,e2e}/, scripts/target/ (Deliberate Simplicity)
- [ ] T004 [P] Configure pyproject.toml with CLI entry point pc-switcher=pcswitcher.cli.main:app and dynamic versioning (Frictionless Command UX)
- [ ] T005 [P] Add dev dependencies: pytest, basedpyright, ruff, codespell to pyproject.toml (Proven Tooling Only)
- [ ] T006 [P] Configure ruff in pyproject.toml with line-length=119, target-version=py313 (Deliberate Simplicity)
- [ ] T007 [P] Configure basedpyright in pyproject.toml with typeCheckingMode=standard, pythonVersion=3.13 (Deliberate Simplicity)
- [ ] T008 Create README.md with project overview, installation instructions, and quick start (Documentation As Runtime Contract)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [ ] T009 Create src/pcswitcher/__init__.py with package version exposure (Deliberate Simplicity)
- [ ] T010 Create src/pcswitcher/__main__.py for python -m pcswitcher entry point (Frictionless Command UX)
- [ ] T011 Define LogLevel enum (DEBUG=10, FULL=15, INFO=20, WARNING=30, ERROR=40, CRITICAL=50) in src/pcswitcher/core/logging.py (Documentation As Runtime Contract)
- [ ] T012 Define SyncError exception in src/pcswitcher/core/module.py (Reliability Without Compromise)
- [ ] T013 Define RemoteExecutor interface class with run(), send_file_to_target(), get_hostname() methods in src/pcswitcher/core/module.py (Deliberate Simplicity)
- [ ] T014 Create SessionState enum (INITIALIZING, VALIDATING, EXECUTING, CLEANUP, COMPLETED, ABORTED, FAILED) in src/pcswitcher/core/session.py (Reliability Without Compromise)
- [ ] T015 Create ModuleResult enum (SUCCESS, SKIPPED, FAILED) in src/pcswitcher/core/session.py (Reliability Without Compromise)

**Checkpoint**: Foundation types and enums ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 4 - Comprehensive Logging System (Priority: P1) 🎯 MVP Component 1/9

**Goal**: Six-level logging (DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL) with independent file/CLI configuration, unified log stream from source and target, structured logging with context

**Independent Test**: Configure different log levels for file and CLI, run sync with dummy modules, verify file contains all events at configured level and above, terminal shows only CLI level events, both source and target operations appear in unified log with correct timestamps and structured context

### Implementation for User Story 4

- [ ] T016 [US4] Add custom FULL log level (15) to Python logging module using logging.addLevelName(15, 'FULL') in src/pcswitcher/core/logging.py (Documentation As Runtime Contract)
- [ ] T017 [US4] Configure structlog with dual output: file (JSONRenderer with keys: timestamp, level, module, hostname, event, context) and terminal (ConsoleRenderer with human-readable format) in src/pcswitcher/core/logging.py (Documentation As Runtime Contract)
- [ ] T018 [US4] Create custom structlog processor track_error_logs() that sets session.has_errors=True when level >= ERROR in src/pcswitcher/core/logging.py (Reliability Without Compromise)
- [ ] T019 [US4] Implement get_logger(name) factory that binds context (module name, session ID, hostname) in src/pcswitcher/core/logging.py (Documentation As Runtime Contract)
- [ ] T020 [US4] Create configure_logging(log_file_level, log_cli_level, log_file_path, session) function that sets up structlog with processors and handlers in src/pcswitcher/core/logging.py (Documentation As Runtime Contract)
- [ ] T021 [US4] Add BoundLogger.full() custom method for FULL level logging in src/pcswitcher/core/logging.py (Documentation As Runtime Contract)
- [ ] T022 [US4] Implement log file creation in ~/.local/share/pc-switcher/logs/sync-<timestamp>.log with directory creation in src/pcswitcher/core/logging.py (Solid-State Stewardship)

**Checkpoint**: At this point, logging system should be fully functional with all six levels, dual output, and ERROR tracking

---

## Phase 4: User Story 6 - Configuration System (Priority: P1) 🎯 MVP Component 2/9

**Goal**: YAML-based config loading from ~/.config/pc-switcher/config.yaml with global settings (log levels, enabled modules) and module-specific settings, schema validation, defaults application, required module enforcement

**Independent Test**: Create config file with various settings, run sync, verify modules receive correct configuration, test invalid config triggers validation errors, confirm log levels are applied correctly, disable optional module and verify it's skipped, attempt to disable required module and verify error

**Dependencies**: US4 (needs logging for error reporting)

### Implementation for User Story 6

- [ ] T023 [US6] Create Configuration dataclass with fields: log_file_level, log_cli_level, sync_modules, module_configs, disk, config_path in src/pcswitcher/core/config.py (Deliberate Simplicity)
- [ ] T024 [US6] Implement load_config(path: Path) -> Configuration that uses yaml.safe_load() to load YAML in src/pcswitcher/core/config.py (Reliability Without Compromise)
- [ ] T025 [US6] Implement validate_config_structure(config_dict) that checks required fields exist and types are correct in src/pcswitcher/core/config.py (Reliability Without Compromise)
- [ ] T026 [US6] Implement validate_required_modules(sync_modules_dict) that verifies btrfs_snapshots is first and enabled in src/pcswitcher/core/config.py (Reliability Without Compromise)
- [ ] T027 [US6] Implement apply_defaults(config_dict, schema) that fills missing values with defaults from config-schema.yaml in src/pcswitcher/core/config.py (Frictionless Command UX)
- [ ] T028 [US6] Implement validate_module_config(module_name, module_config, schema) using jsonschema library (JSON Schema draft-07) in src/pcswitcher/core/config.py (Reliability Without Compromise)
- [ ] T029 [US6] Create generate_default_config() -> str that produces default YAML with inline comments from config-schema.yaml in src/pcswitcher/core/config.py (Frictionless Command UX)
- [ ] T030 [US6] Implement get_enabled_modules(sync_modules_dict) -> list[str] that returns module names in config order in src/pcswitcher/core/config.py (Deliberate Simplicity)

**Checkpoint**: At this point, configuration system should be fully functional with loading, validation, defaults, and module management

---

## Phase 5: User Story 1 - Module Architecture and Integration Contract (Priority: P1) 🎯 MVP Component 3/9

**Goal**: Standardized module interface (SyncModule ABC) with lifecycle methods (validate, pre_sync, sync, post_sync, abort), config schemas, logging/progress injection, sequential execution in config order

**Independent Test**: Define minimal test module, register with orchestrator, run sync and verify orchestrator calls lifecycle methods in order, handles module logging at all levels, processes progress updates, handles errors (exceptions and CRITICAL), calls cleanup on interrupts

**Dependencies**: US4 (needs logging), US6 (needs config)

### Implementation for User Story 1

- [ ] T031 [P] [US1] Create SyncModule ABC with abstract methods validate(), pre_sync(), sync(), post_sync(), abort(timeout), get_config_schema() in src/pcswitcher/core/module.py (Deliberate Simplicity)
- [ ] T032 [P] [US1] Add abstract properties name: str and required: bool to SyncModule in src/pcswitcher/core/module.py (Deliberate Simplicity)
- [ ] T033 [US1] Add __init__(config, remote) to SyncModule that stores config and remote executor in src/pcswitcher/core/module.py (Deliberate Simplicity)
- [ ] T034 [US1] Add emit_progress(percentage, item, eta) and log(level, message, **context) method signatures to SyncModule for orchestrator injection in src/pcswitcher/core/module.py (Documentation As Runtime Contract)
- [ ] T035 [US1] Create RemoteExecutor implementation class that wraps TargetConnection in src/pcswitcher/remote/connection.py (Deliberate Simplicity)
- [ ] T036 [US1] Implement RemoteExecutor.run(command, sudo, timeout) that delegates to TargetConnection in src/pcswitcher/remote/connection.py (Deliberate Simplicity)
- [ ] T037 [US1] Implement RemoteExecutor.send_file_to_target(local, remote) that delegates to TargetConnection in src/pcswitcher/remote/connection.py (Deliberate Simplicity)
- [ ] T038 [US1] Implement RemoteExecutor.get_hostname() that returns target hostname in src/pcswitcher/remote/connection.py (Deliberate Simplicity)

**Checkpoint**: At this point, module interface and remote executor should be fully defined and ready for module implementations

---

## Phase 6: User Story 8 - Dummy Test Modules (Priority: P1) 🎯 MVP Component 4/9

**Goal**: Three reference modules (dummy-success, dummy-critical, dummy-fail) demonstrating successful execution, CRITICAL abort, and exception handling with progress reporting

**Independent Test**: Enable each dummy module in config and run sync, verify expected behavior for each (success completes, critical raises exception at 50%, fail raises unhandled exception), check abort() is called on interrupts

**Dependencies**: US1 (needs module interface), US4 (needs logging), US6 (needs config)

### Implementation for User Story 8

- [ ] T039 [P] [US8] Create DummySuccessModule extending SyncModule in src/pcswitcher/modules/dummy_success.py with 20s simulation, INFO logs every 2s, WARNING at 6s, ERROR at 8s, progress 0-100% (Deliberate Simplicity)
- [ ] T040 [P] [US8] Implement DummySuccessModule.validate() that returns empty list in src/pcswitcher/modules/dummy_success.py (Deliberate Simplicity)
- [ ] T041 [P] [US8] Implement DummySuccessModule.sync() with time.sleep loop, progress emission, and log calls in src/pcswitcher/modules/dummy_success.py (Deliberate Simplicity)
- [ ] T042 [P] [US8] Implement DummySuccessModule.abort(timeout) that logs and stops execution in src/pcswitcher/modules/dummy_success.py (Reliability Without Compromise)
- [ ] T043 [P] [US8] Create DummyCriticalModule in src/pcswitcher/modules/dummy_critical.py that raises SyncError at 50% progress (Reliability Without Compromise)
- [ ] T044 [P] [US8] Create DummyFailModule in src/pcswitcher/modules/dummy_fail.py that raises unhandled exception at 60% progress (Reliability Without Compromise)
- [ ] T045 [US8] Implement get_config_schema() for all three dummy modules with duration_seconds parameter in respective module files (Documentation As Runtime Contract)

**Checkpoint**: At this point, all three dummy modules should be fully functional for infrastructure testing

---

## Phase 7: User Story 5 - Graceful Interrupt Handling (Priority: P1) 🎯 MVP Component 5/9

**Goal**: SIGINT (Ctrl+C) handler that calls abort() on current module, logs interruption, sends cleanup to target, closes connection cleanly, exits with code 130, no orphaned processes

**Independent Test**: Start sync with long-running dummy module, press Ctrl+C mid-execution, verify terminal displays "Sync interrupted by user", currently-executing module's abort() was called, connection to target was closed, no orphaned processes on source or target, log file contains interruption event

**Dependencies**: US1 (needs module interface), US4 (needs logging)

### Implementation for User Story 5

- [ ] T046 [US5] Create SIGINT handler function handle_interrupt(signal_num, frame) in src/pcswitcher/core/signals.py (Reliability Without Compromise)
- [ ] T047 [US5] Implement interrupt flag tracking with threading.Event for graceful vs forced shutdown in src/pcswitcher/core/signals.py (Reliability Without Compromise)
- [ ] T048 [US5] Add signal handler installation install_signal_handlers(session, current_module_ref) in src/pcswitcher/core/signals.py (Reliability Without Compromise)
- [ ] T049 [US5] Implement double-SIGINT detection for immediate force-terminate (second Ctrl+C within 2 seconds) in src/pcswitcher/core/signals.py (Reliability Without Compromise)
- [ ] T050 [US5] Add cleanup logic: call current_module.abort(timeout=5.0), log interruption at WARNING, set session.abort_requested=True in src/pcswitcher/core/signals.py (Reliability Without Compromise)

**Checkpoint**: At this point, interrupt handling should gracefully stop operations and prevent orphaned processes

---

## Phase 8: User Story 2 - Self-Installing Sync Orchestrator (Priority: P1) 🎯 MVP Component 6/9

**Goal**: Auto-detect pc-switcher version on target, install/upgrade to match source version before any validation or snapshots, abort if target newer than source

**Independent Test**: Setup target without pc-switcher, run sync from source, verify orchestrator detects missing installation, installs pc-switcher on target, versions match, repeat with version mismatch to test upgrade path

**Dependencies**: US1 (needs RemoteExecutor), US4 (needs logging), US6 (needs config)

### Implementation for User Story 2

- [ ] T051 [US2] Create TargetConnection class with Fabric Connection wrapper in src/pcswitcher/remote/connection.py (Proven Tooling Only)
- [ ] T052 [US2] Implement TargetConnection.connect() with ControlMaster socket path configuration in src/pcswitcher/remote/connection.py (Throughput-Focused Syncing)
- [ ] T053 [US2] Implement TargetConnection.disconnect() with graceful connection closure in src/pcswitcher/remote/connection.py (Reliability Without Compromise)
- [ ] T054 [US2] Implement TargetConnection.run(command, sudo, timeout) using Fabric conn.run() with result handling in src/pcswitcher/remote/connection.py (Deliberate Simplicity)
- [ ] T055 [US2] Implement TargetConnection.check_version() that detects pc-switcher version on target via pip show pc-switcher in src/pcswitcher/remote/connection.py (Frictionless Command UX)
- [ ] T056 [US2] Implement TargetConnection.install_version(version) that installs from GitHub Package Registry (ghcr.io) using uv tool install pc-switcher==<version> in src/pcswitcher/remote/installer.py (Frictionless Command UX)
- [ ] T057 [US2] Implement version comparison logic: abort if target > source, upgrade if target < source, skip if equal in src/pcswitcher/remote/installer.py (Reliability Without Compromise)
- [ ] T058 [US2] Add error handling for installation failures with CRITICAL logging in src/pcswitcher/remote/installer.py (Reliability Without Compromise)
- [ ] T059 [US2] Implement TargetConnection.send_file_to_target(local, remote) using Fabric conn.put() in src/pcswitcher/remote/connection.py (Deliberate Simplicity)
- [ ] T060 [US2] Implement TargetConnection.terminate_processes() for target cleanup on abort in src/pcswitcher/remote/connection.py (Reliability Without Compromise)

**Checkpoint**: At this point, self-installation should work end-to-end with version detection, installation, and upgrade

---

## Phase 9: User Story 3 - Safety Infrastructure with Btrfs Snapshots (Priority: P1) 🎯 MVP Component 7/9

**Goal**: Pre-sync and post-sync btrfs snapshots for configured subvolumes on source and target, read-only snapshots with timestamp+session-id naming, rollback capability, cleanup command, disk space monitoring, required module that cannot be disabled

**Independent Test**: Run sync on btrfs machines, verify pre-sync snapshots created before any module executes, snapshot naming includes timestamp and session ID, snapshots are read-only, simulate failure and verify rollback restores from pre-sync snapshot, confirm post-sync snapshots created after success, attempt to disable snapshot module and verify error

**Dependencies**: US1 (needs module interface), US4 (needs logging), US6 (needs config)

### Implementation for User Story 3

- [ ] T061 [US3] Create BtrfsSnapshotsModule extending SyncModule in src/pcswitcher/modules/btrfs_snapshots.py with name="btrfs-snapshots", required=True (Reliability Without Compromise)
- [ ] T062 [US3] Implement get_config_schema() for btrfs_snapshots with subvolumes (array), snapshot_dir, keep_recent, max_age_days in src/pcswitcher/modules/btrfs_snapshots.py (Documentation As Runtime Contract)
- [ ] T063 [US3] Implement validate() that checks btrfs filesystem exists (stat -f -c %T / == btrfs) on both source and target in src/pcswitcher/modules/btrfs_snapshots.py (Reliability Without Compromise)
- [ ] T064 [US3] Implement validate() check that all configured subvolumes exist in top-level (btrfs subvolume list /) on both source and target in src/pcswitcher/modules/btrfs_snapshots.py (Reliability Without Compromise)
- [ ] T065 [US3] Implement pre_sync() that creates read-only snapshots with naming {snapshot_dir}/@{subvol}-presync-{timestamp}-{session_id} using btrfs subvolume snapshot -r in src/pcswitcher/modules/btrfs_snapshots.py (Solid-State Stewardship)
- [ ] T066 [US3] Implement post_sync() that creates post-sync snapshots with naming {snapshot_dir}/@{subvol}-postsync-{timestamp}-{session_id} in src/pcswitcher/modules/btrfs_snapshots.py (Solid-State Stewardship)
- [ ] T067 [US3] Add snapshot creation error handling: log CRITICAL and raise SyncError if snapshot fails in src/pcswitcher/modules/btrfs_snapshots.py (Reliability Without Compromise)
- [ ] T068 [US3] Implement rollback_to_presync(session_id) that restores from pre-sync snapshots in src/pcswitcher/modules/btrfs_snapshots.py (Reliability Without Compromise)
- [ ] T069 [US3] Implement cleanup_old_snapshots(older_than_days, keep_recent) using btrfs subvolume delete in src/pcswitcher/modules/btrfs_snapshots.py (Solid-State Stewardship)
- [ ] T070 [US3] Create DiskMonitor utility class with check_free_space(path, min_free) that accepts float (0.0-1.0) or percentage string (e.g., "20%"), default 0.20, in src/pcswitcher/utils/disk.py (Reliability Without Compromise)
- [ ] T071 [US3] Implement DiskMonitor.monitor_continuously(interval, reserve_minimum, callback) with interval default 30s, reserve_minimum default 0.15 or "15%", for periodic checks during sync in src/pcswitcher/utils/disk.py (Reliability Without Compromise)
- [ ] T072 [US3] Add orchestration pre-flight disk check that runs before any modules execute, aborting if disk.min_free falls below threshold in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T073 [US3] Wire continuous disk monitoring into orchestrator run loop so reserve_minimum breaches trigger CRITICAL abort and user messaging in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)

**Checkpoint**: At this point, btrfs snapshot safety infrastructure should be fully functional with pre/post snapshots, rollback, and disk monitoring

---

## Phase 10: User Story Session Management and Locking (Priority: P1) 🎯 MVP Component 8/9

**Goal**: SyncSession state machine tracking sync progress, lock mechanism preventing concurrent syncs, session ID generation, state transitions (INITIALIZING → VALIDATING → EXECUTING → CLEANUP → COMPLETED/ABORTED/FAILED)

**Independent Test**: Run sync and verify session progresses through states correctly, attempt second sync while first is running and verify lock prevents it, simulate crash and verify stale lock detection on next run with user confirmation prompt

**Dependencies**: US4 (needs logging), US6 (needs config)

### Implementation for Session Management

- [ ] T074 [SESSION] Create SyncSession dataclass with fields: id, timestamp, source_hostname, target_hostname, enabled_modules, state, module_results, has_errors, abort_requested, lock_path in src/pcswitcher/core/session.py (Reliability Without Compromise)
- [ ] T075 [SESSION] Implement generate_session_id() that returns 8-char hex from uuid.uuid4() in src/pcswitcher/core/session.py (Deliberate Simplicity)
- [ ] T076 [SESSION] Implement state transition methods: set_state(new_state), is_terminal_state() in src/pcswitcher/core/session.py (Reliability Without Compromise)
- [ ] T077 [SESSION] Create LockManager class with acquire_lock(), release_lock(), check_lock_exists() in src/pcswitcher/utils/lock.py (Reliability Without Compromise)
- [ ] T078 [SESSION] Implement lock file format: JSON with pid, timestamp, session_id in $XDG_RUNTIME_DIR/pc-switcher/pc-switcher.lock in src/pcswitcher/utils/lock.py (Reliability Without Compromise)
- [ ] T079 [SESSION] Implement stale lock detection using ps -p <PID> check in src/pcswitcher/utils/lock.py (Reliability Without Compromise)
- [ ] T080 [SESSION] Add user confirmation prompt for stale lock removal in src/pcswitcher/utils/lock.py (Frictionless Command UX)
- [ ] T081 [SESSION] Implement lock release in cleanup phase with error handling if delete fails in src/pcswitcher/utils/lock.py (Reliability Without Compromise)

**Checkpoint**: At this point, session management and locking should prevent concurrent syncs and track state correctly

---

## Phase 11: User Story Core Orchestration (Priority: P1) 🎯 MVP Component 9/9

**Goal**: Main orchestrator that manages complete sync workflow: initialization → validation → execution → cleanup, module loading from config, lifecycle execution, exception handling, final state determination

**Independent Test**: Run complete sync flow with dummy modules, verify all phases execute in order, modules receive injected methods, exceptions are caught and logged as CRITICAL, ERROR logs tracked for final state, session summary logged

**Dependencies**: ALL previous user stories (this integrates everything)

### Implementation for Core Orchestration

- [ ] T082 [ORCH] Create Orchestrator class with __init__(config, target_hostname) in src/pcswitcher/core/orchestrator.py (Deliberate Simplicity)
- [ ] T083 [ORCH] Implement _load_modules() that imports module classes by name and instantiates with config + RemoteExecutor in src/pcswitcher/core/orchestrator.py (Deliberate Simplicity)
- [ ] T084 [ORCH] Implement _inject_module_methods(module) that sets module.log and module.emit_progress in src/pcswitcher/core/orchestrator.py (Deliberate Simplicity)
- [ ] T085 [ORCH] Implement _validate_all_modules() that calls validate() on each module and collects errors in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T086 [ORCH] Implement _execute_module_lifecycle(module) that calls pre_sync() → sync() → post_sync() with exception handling in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T087 [ORCH] Implement exception catching: catch SyncError specifically, log error message at CRITICAL level, call abort(timeout) on current module, set session.abort_requested=True, enter CLEANUP phase; catch other exceptions, wrap as SyncError in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T088 [ORCH] Implement _cleanup_phase() that calls current_module.abort(timeout=5.0) if module is running in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T089 [ORCH] Implement _determine_final_state() logic: Ctrl+C → ABORTED, exception/ERROR logs → FAILED, success → COMPLETED in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T090 [ORCH] Implement rollback offer workflow after CRITICAL failure if pre-sync snapshots exist in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T091 [ORCH] Add user confirmation prompt for rollback: "Would you like to restore snapshots? [y/N]" in src/pcswitcher/core/orchestrator.py (Frictionless Command UX)
- [ ] T092 [ORCH] Implement execute_rollback() that calls BtrfsSnapshotsModule.rollback_to_presync() on user confirmation in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T093 [ORCH] Implement log_session_summary() that reports final state (COMPLETED/ABORTED/FAILED), per-module results (SUCCESS/SKIPPED/FAILED), total duration, error count, and lists any modules that failed in src/pcswitcher/core/orchestrator.py (Documentation As Runtime Contract)
- [ ] T094 [ORCH] Implement run() method that orchestrates: INITIALIZING → VALIDATING → EXECUTING → CLEANUP → terminal state in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T095 [ORCH] Add btrfs filesystem verification during INITIALIZING: check / is btrfs (this is a fast sanity check before module validation; detailed subvolume checks are done by BtrfsSnapshotsModule.validate()) in src/pcswitcher/core/orchestrator.py (Reliability Without Compromise)
- [ ] T096 [ORCH] Implement progress forwarding: receive from module, log at FULL, forward to terminal UI in src/pcswitcher/core/orchestrator.py (Frictionless Command UX)

**Checkpoint**: At this point, core orchestrator should execute complete sync workflow end-to-end

---

## Phase 12: User Story 9 - Terminal UI with Progress Reporting (Priority: P2)

**Goal**: Real-time terminal display with progress bars (rich library), current module name, operation phase, progress percentage, current item, log messages at CLI log level, smooth updates without flicker

**Independent Test**: Run sync with dummy modules, verify terminal shows progress bars, module names, log messages, updates smoothly, handles terminal resize gracefully

**Dependencies**: ORCH (needs orchestrator), US4 (needs logging)

### Implementation for User Story 9

- [ ] T097 [P] [US9] Create TerminalUI class with rich.console.Console and rich.progress.Progress in src/pcswitcher/cli/ui.py (Frictionless Command UX)
- [ ] T098 [US9] Implement TerminalUI.create_module_task(module_name) that adds progress bar for module in src/pcswitcher/cli/ui.py (Frictionless Command UX)
- [ ] T099 [US9] Implement TerminalUI.update_progress(module_name, percentage, item) that updates progress bar and description in src/pcswitcher/cli/ui.py (Frictionless Command UX)
- [ ] T100 [US9] Implement TerminalUI.display_log(level, message) with color-coding by level using rich.logging.RichHandler in src/pcswitcher/cli/ui.py (Frictionless Command UX)
- [ ] T101 [US9] Add rich.live.Live for real-time updates without flicker in src/pcswitcher/cli/ui.py (Frictionless Command UX)
- [ ] T102 [US9] Implement TerminalUI.show_overall_progress(current_module_index, total_modules) in src/pcswitcher/cli/ui.py (Frictionless Command UX)
- [ ] T103 [US9] Integrate TerminalUI with Orchestrator: create UI, update on progress events, display logs in src/pcswitcher/core/orchestrator.py (Frictionless Command UX)

**Checkpoint**: At this point, terminal UI should display real-time progress with bars and logs

---

## Phase 13: User Story CLI Commands (Priority: P2)

**Goal**: CLI commands using typer: pc-switcher sync <target>, pc-switcher logs --last, pc-switcher cleanup-snapshots --older-than, entry point integration

**Independent Test**: Run each command and verify expected behavior: sync starts orchestrator, logs displays last sync log, cleanup-snapshots deletes old snapshots

**Dependencies**: ORCH (needs orchestrator), US9 (needs terminal UI), US3 (needs snapshot cleanup)

### Implementation for CLI Commands

- [ ] T104 [P] [CLI] Create Typer app instance in src/pcswitcher/cli/main.py (Proven Tooling Only)
- [ ] T105 [P] [CLI] Implement sync(target: str, config: Path | None = None) command that loads config, creates orchestrator, runs sync in src/pcswitcher/cli/main.py (Frictionless Command UX)
- [ ] T106 [P] [CLI] Implement logs(last: bool = False, session_id: str | None = None) command that displays log file in src/pcswitcher/cli/main.py (Frictionless Command UX)
- [ ] T107 [P] [CLI] Implement cleanup_snapshots(older_than: str = "7d", keep_recent: int = 3) command in src/pcswitcher/cli/main.py (Solid-State Stewardship)
- [ ] T108 [CLI] Add error handling and exit codes: 0 for success, 1 for failure, 130 for interrupt in src/pcswitcher/cli/main.py (Reliability Without Compromise)
- [ ] T109 [CLI] Add --version flag that displays pc-switcher version in src/pcswitcher/cli/main.py (Frictionless Command UX)
- [ ] T110 [CLI] Configure typer rich integration for formatted help output in src/pcswitcher/cli/main.py (Frictionless Command UX)

**Checkpoint**: At this point, all CLI commands should be fully functional

---

## Phase 14: User Story 7 - Installation and Setup Infrastructure (Priority: P2)

**Goal**: Installation script for deploying pc-switcher to new machines: dependency checking, package installation, default config creation, and btrfs verification

**Independent Test**: Run setup script on fresh Ubuntu 24.04 machine, verify all dependencies installed, pc-switcher package installed, config directory created with default config, and btrfs detection aborts correctly on unsupported filesystems

**Dependencies**: US6 (needs generate_default_config), US3 (needs btrfs checks)

### Implementation for User Story 7

- [ ] T111 [P] [US7] Create setup script scripts/setup.sh that detects btrfs filesystem using stat -f -c %T / and aborts with clear error if not btrfs (Frictionless Command UX)
- [ ] T112 [P] [US7] Add check for uv 0.9.9 installation in scripts/setup.sh, install if missing (Proven Tooling Only)
- [ ] T113 [US7] Implement pc-switcher package installation from GitHub Package Registry using uv tool install pc-switcher in scripts/setup.sh (Frictionless Command UX)
- [ ] T114 [US7] Create ~/.config/pc-switcher/ directory and generate default config.yaml with inline comments in scripts/setup.sh (Documentation As Runtime Contract)
- [ ] T115 [US7] Add success message "pc-switcher installed successfully" at end of setup in scripts/setup.sh (Frictionless Command UX)

**Checkpoint**: At this point, installation script should handle end-to-end setup on new machines

---

## Phase 15: CI/CD and Release Infrastructure (Priority: P2)

**Goal**: GitHub Actions workflows for CI (lint, type check, tests) and releases (build, publish to GitHub Package Registry), dynamic versioning from Git tags

**Independent Test**: Create pull request and verify CI runs all checks, create GitHub release with tag and verify package publishes to ghcr.io with correct version

**Dependencies**: All code complete

### Implementation for CI/CD

- [ ] T116 [P] [CICD] Create .github/workflows/ci.yml with jobs: checkout, setup-uv (from .tool-versions), uv sync, ruff check, basedpyright, pytest, codespell (Proven Tooling Only)
- [ ] T117 [P] [CICD] Create .github/workflows/release.yml triggered on release published with steps: checkout (fetch-depth: 0 for tags), setup-uv, uv build, authenticate to ghcr.io using GITHUB_TOKEN, uv publish --repository ghcr.io (Proven Tooling Only)
- [ ] T118 [CICD] Configure release workflow with GITHUB_TOKEN permissions: contents read, packages write, and configure package registry URL (ghcr.io/owner/pc-switcher) in .github/workflows/release.yml (Proven Tooling Only)
- [ ] T119 [CICD] Add workflow run verification: test CI on branch push, test release on test tag in .github/workflows/ (Reliability Without Compromise)

**Checkpoint**: At this point, CI/CD should automatically test code and publish releases

---

## Phase 16: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [ ] T120 [P] Update README.md with complete installation instructions, configuration guide, usage examples, troubleshooting section (Documentation As Runtime Contract)
- [ ] T121 [P] Add inline code comments for complex logic in orchestrator, btrfs module, remote connection (Documentation As Runtime Contract)
- [ ] T122 [P] Create CONTRIBUTING.md with development setup, testing guide, PR workflow (Documentation As Runtime Contract)
- [ ] T123 [P] Add example config files in examples/ directory: minimal.yaml, full-featured.yaml (Frictionless Command UX)
- [ ] T124 Review all error messages for clarity and actionability, ensure they guide user to resolution (Frictionless Command UX)
- [ ] T125 Add startup performance measurement: log timing from CLI invocation to sync start (Throughput-Focused Syncing)
- [ ] T126 Verify all file operations use minimal writes: structured logging buffer, snapshot COW verification (Solid-State Stewardship)
- [ ] T127 Run complete sync flow validation per quickstart.md test scenarios (Reliability Without Compromise)
- [ ] T128 Create ARCHITECTURE.md documenting module interface, orchestrator workflow, state transitions with diagrams (Documentation As Runtime Contract)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Stories (Phases 3-14)**: All depend on Foundational phase completion
  - US4 (Logging) → blocks US6, US1, US8, US5, US2, US3, SESSION, ORCH, US9, CLI
  - US6 (Config) → blocks US1, US8, US2, US3, SESSION, ORCH, CLI
  - US1 (Module Interface) → blocks US8, US2, US3, ORCH
  - US8 (Dummy Modules) → enables ORCH testing
  - US5 (Interrupt Handling) → independent, integrates with ORCH
  - US2 (Self-Install) → independent, integrates with ORCH
  - US3 (Btrfs Snapshots) → independent module, integrates with ORCH
  - SESSION → blocks ORCH
  - ORCH → blocks US9, CLI, US7
  - US9 (Terminal UI) → integrates with ORCH and CLI
  - CLI → final integration layer
  - US7 (Installation) → can be done anytime, uses US6 and US3
- **CI/CD (Phase 15)**: Depends on all code complete
- **Polish (Phase 16)**: Depends on all desired user stories being complete

### Critical Path (Minimum for Working System)

1. Phase 1 (Setup) → Phase 2 (Foundational)
1. US4 (Logging) → US6 (Config) → US1 (Module Interface)
1. US8 (Dummy Modules) + US5 (Interrupt) + US2 (Self-Install) + US3 (Btrfs)
1. SESSION → ORCH (Core Orchestration)
1. CLI → Working system!

### Parallel Opportunities

After Foundational phase (T015) completes:
- US4 (Logging) must complete first
- Then US6 (Config) must complete
- Then US1 (Module Interface) must complete
- Then these can run in parallel:
  - US8 (Dummy Modules): T039-T045
  - US5 (Interrupt Handling): T046-T050
  - US2 (Self-Install): T051-T060
  - US3 (Btrfs Snapshots): T061-T073
  - SESSION: T074-T081

After those complete, ORCH can start (T082-T096), then:
- US9 (Terminal UI): T097-T103 in parallel with:
- CLI: T104-T110

Finally in parallel:
- US7 (Installation): T111-T117
- CI/CD: T116-T119
- Polish: T120-T128

---

## Parallel Example: After Foundational Phase

```bash
# After US1 (Module Interface) is complete, these can run in parallel:

# Team Member 1:
Task T039-T045: "Dummy test modules"

# Team Member 2:
Task T046-T050: "Interrupt handling"

# Team Member 3:
Task T051-T060: "Self-installation"

# Team Member 4:
Task T061-T073: "Btrfs snapshots module"

# Team Member 5:
Task T074-T081: "Session management and locking"
```

---

## Implementation Strategy

### MVP First (Core Working System)

**Target**: Minimal functional sync system for testing

1. Complete Phase 1: Setup (T001-T008)
1. Complete Phase 2: Foundational (T009-T015)
1. Complete US4: Logging (T016-T022)
1. Complete US6: Config (T023-T030)
1. Complete US1: Module Interface (T031-T038)
1. Complete US8: Dummy Modules (T039-T045)
1. Complete SESSION: Session Management (T074-T081)
1. Complete ORCH: Core Orchestration (T082-T096)
1. Complete CLI: Basic Commands (T104-T110)
1. **STOP and VALIDATE**: Test complete sync flow with dummy modules

**At this checkpoint**:
- Can run `pc-switcher sync <target>`
- Dummy modules demonstrate all infrastructure
- Logging works (file + terminal)
- Config loading works
- Module lifecycle executes correctly
- Can test error handling, interrupts, state transitions

**Timeline**: ~2-3 weeks for single developer

### MVP + Safety (Production-Ready Core)

Add safety infrastructure for real usage:

1. Complete US2: Self-Installation (T051-T060)
1. Complete US3: Btrfs Snapshots (T061-T073)
1. Complete US5: Interrupt Handling (T046-T050)
1. **STOP and VALIDATE**: Test complete sync with snapshots, test rollback, test interrupts

**At this checkpoint**:
- Full safety with snapshots
- Rollback capability
- Graceful interrupts
- Ready for real sync module development

**Timeline**: +1-2 weeks

### Full Feature Set

Add UX polish and deployment:

1. Complete US9: Terminal UI (T097-T103)
1. Complete US7: Installation (T111-T117)
1. Complete CI/CD (T116-T119)
1. Complete Polish (T120-T128)

**Timeline**: +1 week

**Total MVP to Full**: 4-6 weeks for single developer

### Incremental Delivery

1. **Week 1-2**: Setup → Foundational → Logging → Config → Module Interface
   - Deliverable: Architecture complete, can write modules
1. **Week 3**: Dummy Modules → Session → Orchestrator → CLI
   - Deliverable: Working sync system (dummy modules only)
   - Demo: Show sync flow, error handling, logging
1. **Week 4**: Self-Install → Btrfs Snapshots → Interrupt Handling
   - Deliverable: Production-ready foundation
   - Demo: Show snapshot creation, rollback, graceful interrupts
1. **Week 5**: Terminal UI → Installation → CI/CD
   - Deliverable: Complete foundation infrastructure
   - Demo: Full UX with progress bars, easy installation
1. **Week 6**: Polish and validation
   - Deliverable: Release-ready v1.0.0
   - Enable: Feature module development can begin

---

## Summary

- **Total tasks**: 128 (down from 129 after removing the btrfs subvolume guidance task)
- **User stories**: 9 (US1-US9) + Session + Orchestrator + CLI + Installation + CI/CD
- **Parallel opportunities**: After Foundational phase, US8/US5/US2/US3/SESSION can run in parallel (5-way parallelism)
- **MVP scope**: T001-T030, T039-T045, T074-T096, T104-T110 (approximately 60 tasks, ~2-3 weeks)
- **Critical path**: Setup → Foundational → US4 → US6 → US1 → ORCH → CLI
- **Independent test criteria**: Each user story phase includes clear test criteria for verification
- **Format validation**: All 129 tasks follow strict format: `- [ ] [ID] [P?] [Story] Description with file path`

**Suggested MVP**: Complete through US8 + SESSION + ORCH + CLI (checkpoints at T045, T081, T096, T110) for working system with dummy modules, then add US2/US3/US5 for production safety.
