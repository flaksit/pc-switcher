# Tasks: Foundation Infrastructure Complete

**Input**: Design documents from `/specs/001-foundation/`
**Prerequisites**: plan.md (required), spec.md (required), architecture.md (required), research.md, data-model.md, contracts/

**Tests**: No explicit test-first workflow requested in specification. Tests are included where they provide contract verification value.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- **Single project**: `src/pcswitcher/`, `tests/` at repository root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization and basic structure

- [ ] T001 Create project structure per plan.md layout in src/pcswitcher/
- [ ] T002 Initialize Python 3.14 project with uv and add dependencies (asyncssh, rich, typer, structlog, pyyaml, jsonschema, packaging, pytimeparse2)
- [ ] T003 [P] Add dev dependencies (pytest, pytest-asyncio, basedpyright, ruff, codespell)
- [ ] T004 [P] Configure pyproject.toml with package metadata, entry points (pc-switcher CLI), and dynamic versioning (hatchling + uv-dynamic-versioning per ADR-004)
- [ ] T005 [P] Create src/pcswitcher/__init__.py with dynamic version export from package metadata
- [ ] T006 [P] Setup ruff.toml and basedpyright configuration
- [ ] T007a [P] Create GitHub Actions workflow for release builds (.github/workflows/release.yml): trigger on release published, build package with uv build, attach wheel to release assets (no PyPI publish)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**CRITICAL**: No user story work can begin until this phase is complete

- [ ] T007 Implement core enums and types (Host, LogLevel) in src/pcswitcher/models.py
- [ ] T008 [P] Implement CommandResult dataclass in src/pcswitcher/models.py
- [ ] T009 [P] Implement ProgressUpdate dataclass with validation in src/pcswitcher/models.py
- [ ] T010 [P] Implement ConfigError and ValidationError dataclasses in src/pcswitcher/models.py
- [ ] T011 Implement EventBus with subscribe/publish/close in src/pcswitcher/events.py
- [ ] T012 [P] Implement LogEvent, ProgressEvent, ConnectionEvent dataclasses in src/pcswitcher/events.py
- [ ] T013 Implement LocalExecutor with run_command, start_process, terminate_all_processes in src/pcswitcher/executor.py
- [ ] T014 Implement Process protocol wrapper for local subprocess in src/pcswitcher/executor.py
- [ ] T015 Implement Connection class with asyncssh (connect, disconnect, create_process, sftp) in src/pcswitcher/connection.py
- [ ] T016 Implement RemoteExecutor wrapping Connection (run_command, start_process, send_file, get_file, get_hostname) in src/pcswitcher/executor.py
- [ ] T017 Implement Process protocol wrapper for SSH process in src/pcswitcher/executor.py
- [ ] T018 Create shared test fixtures (mock_connection, mock_executor, mock_event_bus) in tests/conftest.py

**Checkpoint**: Foundation ready - user story implementation can now begin

---

## Phase 3: User Story 6 - Configuration System (Priority: P1)

**Goal**: Load and validate YAML configuration with job-specific schema validation

**Why first**: All other user stories depend on configuration being loaded and validated. This is the true foundational story.

**Independent Test**: Create config file, run orchestrator, verify jobs receive correct config and validation errors are reported

### Implementation for User Story 6

- [ ] T019 [US6] Implement DiskConfig and BtrfsConfig dataclasses in src/pcswitcher/config.py
- [ ] T020 [US6] Implement Configuration dataclass with from_yaml classmethod in src/pcswitcher/config.py
- [ ] T021 [US6] Implement YAML loading with pyyaml in Configuration.from_yaml in src/pcswitcher/config.py
- [ ] T022 [US6] Implement schema validation using jsonschema (load config-schema.yaml) in src/pcswitcher/config.py
- [ ] T023 [US6] Implement default value application for missing config fields in src/pcswitcher/config.py
- [ ] T024 [US6] Implement log level parsing (string to LogLevel enum) in src/pcswitcher/config.py
- [ ] T025 [US6] Implement disk threshold parsing (percentage or absolute) in src/pcswitcher/config.py
- [ ] T026 [US6] Add error handling for invalid YAML syntax with line numbers in src/pcswitcher/config.py
- [ ] T027 [US6] Copy config-schema.yaml from specs/001-foundation/contracts/ to src/pcswitcher/schemas/

**Checkpoint**: Configuration system is fully functional and testable

---

## Phase 4: User Story 1 - Job Architecture and Integration Contract (Priority: P1)

**Goal**: Define the job interface contract and base classes for all sync jobs

**Independent Test**: Implement DummySuccessJob, register with orchestrator, verify lifecycle methods called correctly

### Implementation for User Story 1

- [ ] T028 [US1] Create src/pcswitcher/jobs/__init__.py with job exports
- [ ] T029 [US1] Implement Job ABC with name, CONFIG_SCHEMA, validate_config, validate, execute in src/pcswitcher/jobs/base.py
- [ ] T030 [US1] Implement _log helper method on Job base class in src/pcswitcher/jobs/base.py
- [ ] T031 [US1] Implement _report_progress helper method on Job base class in src/pcswitcher/jobs/base.py
- [ ] T032 [US1] Implement SystemJob subclass (required=True) in src/pcswitcher/jobs/base.py
- [ ] T033 [US1] Implement SyncJob subclass (required=False) in src/pcswitcher/jobs/base.py
- [ ] T034 [US1] Implement BackgroundJob subclass (required=True) in src/pcswitcher/jobs/base.py
- [ ] T035 [US1] Implement JobContext dataclass in src/pcswitcher/jobs/context.py
- [ ] T036 [US1] Implement default validate_config using jsonschema in Job base class in src/pcswitcher/jobs/base.py
- [ ] T037 [P] [US1] Create tests/contract/test_job_interface.py to verify job contract compliance

**Checkpoint**: Job architecture contract is defined and verified

---

## Phase 5: User Story 4 - Comprehensive Logging System (Priority: P1)

**Goal**: Implement six-level logging with file (JSON) and terminal output

**Independent Test**: Run sync, verify log file contains JSON events, terminal shows colored output at correct levels

### Implementation for User Story 4

- [ ] T038 [US4] Implement Logger class with log method and level filtering in src/pcswitcher/logger.py
- [ ] T039 [US4] Implement JobLogger for job-bound logging in src/pcswitcher/logger.py
- [ ] T040 [US4] Implement FileLogger consuming from EventBus queue in src/pcswitcher/logger.py
- [ ] T041 [US4] Configure structlog JSONRenderer for file output in src/pcswitcher/logger.py
- [ ] T042 [US4] Configure structlog ConsoleRenderer for terminal output in src/pcswitcher/logger.py
- [ ] T043 [US4] Implement log file path generation (sync-<timestamp>.log) in src/pcswitcher/logger.py
- [ ] T044 [US4] Implement hostname resolution (Host enum to actual hostname) in src/pcswitcher/logger.py
- [ ] T045 [P] [US4] Add logs command to CLI (--last flag) in src/pcswitcher/cli.py

**Checkpoint**: Logging system is fully functional with file and console output

---

## Phase 6: User Story 9 - Terminal UI with Progress Reporting (Priority: P2)

**Goal**: Rich terminal UI with progress bars, log panel, and status display

**Independent Test**: Run sync with dummy jobs, verify progress bars update, logs appear in panel

### Implementation for User Story 9

- [ ] T046 [US9] Implement TerminalUI class with Rich Live display in src/pcswitcher/ui.py
- [ ] T047 [US9] Implement progress bar rendering with Progress widget in src/pcswitcher/ui.py
- [ ] T048 [US9] Implement log panel with scrolling message deque in src/pcswitcher/ui.py
- [ ] T049 [US9] Implement connection status display in src/pcswitcher/ui.py
- [ ] T050 [US9] Implement overall progress display (Step N/M) in src/pcswitcher/ui.py
- [ ] T051 [US9] Implement UI consumer task for EventBus queue in src/pcswitcher/ui.py
- [ ] T052 [US9] Add update_job_progress, add_log_message, set_connection_status methods in src/pcswitcher/ui.py
- [ ] T053 [US9] Implement ProgressUpdate rendering (percent, current/total, heartbeat) in src/pcswitcher/ui.py

**Checkpoint**: Terminal UI displays real-time progress and logs

---

## Phase 7: User Story 5 - Graceful Interrupt Handling (Priority: P1)

**Goal**: Handle Ctrl+C with graceful cleanup and proper exit codes

**Independent Test**: Start sync, press Ctrl+C, verify cleanup runs, no orphaned processes, exit 130

### Implementation for User Story 5

- [ ] T054 [US5] Implement SIGINT handler in orchestrator in src/pcswitcher/orchestrator.py
- [ ] T055 [US5] Implement task cancellation via asyncio.CancelledError in src/pcswitcher/orchestrator.py
- [ ] T056 [US5] Implement 5-second cleanup timeout in orchestrator in src/pcswitcher/orchestrator.py
- [ ] T057 [US5] Implement double-SIGINT force-terminate (cleanup_in_progress flag) in src/pcswitcher/orchestrator.py
- [ ] T058 [US5] Implement kill_all_remote_processes in Connection class in src/pcswitcher/connection.py
- [ ] T059 [US5] Implement exit code 130 for SIGINT termination in src/pcswitcher/cli.py

**Checkpoint**: Interrupt handling works with graceful cleanup

---

## Phase 8: User Story 3 - Safety Infrastructure with Btrfs Snapshots (Priority: P1)

**Goal**: Create pre/post snapshots, validate subvolumes, manage snapshot directory

**Independent Test**: Run sync, verify snapshots created in /.snapshots/pc-switcher/, verify subvolume validation

### Implementation for User Story 3

- [ ] T060 [US3] Implement Snapshot dataclass and SnapshotPhase enum in src/pcswitcher/models.py
- [ ] T061 [US3] Implement snapshot name generation (pre/post-<subvol>-<timestamp>) in src/pcswitcher/snapshots.py
- [ ] T062 [US3] Implement session folder creation (<timestamp>-<session-id>) in src/pcswitcher/snapshots.py
- [ ] T063 [US3] Implement create_snapshot function (btrfs subvolume snapshot -r) in src/pcswitcher/snapshots.py
- [ ] T064 [US3] Implement validate_snapshots_directory (check/create /.snapshots subvolume) in src/pcswitcher/snapshots.py
- [ ] T065 [US3] Implement subvolume existence validation (check on source and target) in src/pcswitcher/snapshots.py
- [ ] T066 [US3] Implement BtrfsSnapshotJob with pre/post phase parameter in src/pcswitcher/jobs/btrfs.py
- [ ] T067 [US3] Implement pre-sync snapshot creation in orchestrator in src/pcswitcher/orchestrator.py
- [ ] T068 [US3] Implement post-sync snapshot creation in orchestrator in src/pcswitcher/orchestrator.py
- [ ] T069 [US3] Implement snapshot cleanup algorithm (keep_recent, max_age_days) in src/pcswitcher/snapshots.py
- [ ] T070 [US3] Add cleanup-snapshots command to CLI (--older-than, --dry-run) in src/pcswitcher/cli.py
- [ ] T071 [US3] Implement duration parsing for --older-than using pytimeparse2 in src/pcswitcher/snapshots.py

**Checkpoint**: Snapshot safety system fully operational

---

## Phase 9: User Story 3 (cont.) - Disk Space Monitoring (Priority: P1)

**Goal**: Preflight and runtime disk space checks on source and target

**Independent Test**: Set low threshold, verify sync aborts when space insufficient

### Implementation for Disk Space Monitoring

- [ ] T072 [US3] Implement DiskSpace dataclass and parse_df_output in src/pcswitcher/disk.py
- [ ] T073 [US3] Implement parse_threshold (percentage and absolute) in src/pcswitcher/disk.py
- [ ] T074 [US3] Implement check_disk_space function for single host in src/pcswitcher/disk.py
- [ ] T075 [US3] Implement DiskSpaceCriticalError exception in src/pcswitcher/models.py
- [ ] T076 [US3] Implement DiskSpaceMonitorJob as BackgroundJob in src/pcswitcher/jobs/disk_space_monitor.py
- [ ] T077 [US3] Implement preflight check in orchestrator (before snapshots) in src/pcswitcher/orchestrator.py
- [ ] T078 [US3] Implement runtime monitoring with check_interval in src/pcswitcher/jobs/disk_space_monitor.py
- [ ] T079 [US3] Spawn two DiskSpaceMonitorJob instances (source, target) in orchestrator in src/pcswitcher/orchestrator.py

**Checkpoint**: Disk space safety monitoring operational

---

## Phase 10: User Story 2 - Self-Installing Sync Orchestrator (Priority: P1)

**Goal**: Detect, install, or upgrade pc-switcher on target machine

**Independent Test**: Run sync to target without pc-switcher, verify installation, verify version match

### Implementation for User Story 2

- [ ] T080 [US2] Implement get_current_version (read from package metadata) in src/pcswitcher/installation.py
- [ ] T081 [US2] Implement get_target_version via RemoteExecutor in src/pcswitcher/installation.py
- [ ] T082 [US2] Implement version comparison using packaging.version in src/pcswitcher/installation.py
- [ ] T083 [US2] Implement install_on_target (run install.sh on target via SSH; install.sh handles uv bootstrap and uv tool install) in src/pcswitcher/installation.py
- [ ] T084 [US2] Implement version mismatch detection (abort if target newer) in src/pcswitcher/installation.py
- [ ] T085 [US2] Integrate version check/install into orchestrator (before snapshots) in src/pcswitcher/orchestrator.py

**Checkpoint**: Self-installation works for fresh and outdated targets

---

## Phase 11: User Story 7 - Installation and Setup Infrastructure (Priority: P2)

**Goal**: Provide install.sh script for initial deployment

**Independent Test**: Run curl | sh on fresh Ubuntu 24.04, verify pc-switcher installed and config created

### Implementation for User Story 7

- [ ] T086 [US7] Create install.sh script (check/install uv, btrfs-progs, pc-switcher) in install.sh
- [ ] T087 [US7] Implement --version parameter for specific version installation in install.sh
- [ ] T088 [US7] Implement default config generation with inline comments in install.sh; prompt before overwriting existing config
- [ ] T089 [US7] Create ~/.config/pc-switcher/ and ~/.local/share/pc-switcher/logs/ directories in install.sh

**Checkpoint**: Installation script fully functional

---

## Phase 12: User Story 8 - Dummy Test Jobs (Priority: P1)

**Goal**: Implement dummy jobs for infrastructure testing

**Independent Test**: Enable dummy_success, run sync, verify 40s execution with logs and progress

### Implementation for User Story 8

- [ ] T090 [US8] Implement DummySuccessJob in src/pcswitcher/jobs/dummy.py
- [ ] T091 [US8] Implement source phase (20s, log every 2s, WARNING at 6s) in src/pcswitcher/jobs/dummy.py
- [ ] T092 [US8] Implement target phase (20s, log every 2s, ERROR at 8s) in src/pcswitcher/jobs/dummy.py
- [ ] T093 [US8] Implement progress reporting (0%, 25%, 50%, 75%, 100%) in src/pcswitcher/jobs/dummy.py
- [ ] T094 [US8] Implement DummyFailJob in src/pcswitcher/jobs/dummy.py
- [ ] T095 [US8] Implement exception at 60% progress in DummyFailJob in src/pcswitcher/jobs/dummy.py
- [ ] T096 [US8] Implement cancellation handling in both dummy jobs in src/pcswitcher/jobs/dummy.py
- [ ] T097 [P] [US8] Register dummy jobs in job discovery in src/pcswitcher/jobs/__init__.py

**Checkpoint**: Dummy jobs demonstrate all infrastructure features

---

## Phase 13: Core Orchestration (Priority: P1)

**Goal**: Implement main orchestrator coordinating all components

**Independent Test**: Run pc-switcher sync <target>, verify complete workflow

### Implementation for Core Orchestration

- [ ] T098 Implement SyncSession and SessionStatus in src/pcswitcher/models.py
- [ ] T099 Implement JobResult and JobStatus in src/pcswitcher/models.py
- [ ] T100 Implement Orchestrator class skeleton in src/pcswitcher/orchestrator.py
- [ ] T101 Implement job discovery and instantiation from sync_jobs config in src/pcswitcher/orchestrator.py
- [ ] T102 Implement three-phase validation (schema, job config, system state) in src/pcswitcher/orchestrator.py
- [ ] T103 Implement sequential job execution loop in src/pcswitcher/orchestrator.py
- [ ] T104 Implement TaskGroup for background jobs (DiskSpaceMonitor) in src/pcswitcher/orchestrator.py
- [ ] T105 Implement sync summary logging (success/failure per job) in src/pcswitcher/orchestrator.py
- [ ] T106 Implement session ID generation (secrets.token_hex(4)) in src/pcswitcher/orchestrator.py

**Checkpoint**: Orchestrator coordinates complete sync workflow

---

## Phase 14: Locking Mechanism (Priority: P1)

**Goal**: Prevent concurrent sync executions

**Independent Test**: Run two syncs simultaneously, verify second is rejected

### Implementation for Locking

- [ ] T107 Implement SyncLock class with fcntl.flock in src/pcswitcher/lock.py
- [ ] T108 Implement acquire with holder_info (PID) in src/pcswitcher/lock.py
- [ ] T109 Implement get_holder_info for error messages in src/pcswitcher/lock.py
- [ ] T110 Implement target lock via SSH (flock command) in src/pcswitcher/lock.py
- [ ] T111 Integrate source lock acquisition in orchestrator in src/pcswitcher/orchestrator.py
- [ ] T112 Integrate target lock acquisition in orchestrator (after SSH connect) in src/pcswitcher/orchestrator.py

**Checkpoint**: Concurrent execution prevention working

---

## Phase 15: CLI Entry Point (Priority: P1)

**Goal**: Implement pc-switcher sync <target> command

**Independent Test**: Run pc-switcher sync laptop-work, verify complete workflow

### Implementation for CLI

- [ ] T113 Implement Typer app with sync, logs, cleanup-snapshots commands in src/pcswitcher/cli.py
- [ ] T114 Implement sync command (target argument, config loading, orchestrator run) in src/pcswitcher/cli.py
- [ ] T115 Implement asyncio.run wrapper for sync command in src/pcswitcher/cli.py
- [ ] T116 Implement error display with clear messages and exit codes in src/pcswitcher/cli.py
- [ ] T117 Wire entry point in pyproject.toml [project.scripts] section

**Checkpoint**: CLI is fully functional

---

## Phase 16: Polish & Cross-Cutting Concerns

**Purpose**: Improvements that affect multiple user stories

- [ ] T118 [P] Run ruff format and ruff check --fix across all source files
- [ ] T119 [P] Run basedpyright and fix all type errors
- [ ] T120 [P] Run codespell and fix any typos
- [ ] T121 [P] Update README.md with installation and usage instructions
- [ ] T122 [P] Update CLAUDE.md with active technologies section
- [ ] T123 Run quickstart.md validation (verify all commands work)
- [ ] T124 End-to-end test: full sync workflow with dummy jobs

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **US6 Configuration (Phase 3)**: First user story - all others depend on config loading
- **US1 Job Architecture (Phase 4)**: Depends on US6 for config validation
- **US4 Logging (Phase 5)**: Depends on US1 for job logging helpers
- **US9 Terminal UI (Phase 6)**: Depends on US4 for log event consumption
- **US5 Interrupt Handling (Phase 7)**: Can run in parallel with US9
- **US3 Snapshots (Phase 8-9)**: Depends on US1, US4, US5
- **US2 Self-Installation (Phase 10)**: Depends on foundational only
- **US7 Install Script (Phase 11)**: Depends on US2 (shared logic)
- **US8 Dummy Jobs (Phase 12)**: Depends on US1, US4
- **Core Orchestration (Phase 13)**: Depends on all prior user stories
- **Locking (Phase 14)**: Can run in parallel with Phase 13
- **CLI (Phase 15)**: Depends on Phase 13 (orchestrator)
- **Polish (Phase 16)**: Depends on all prior phases

### User Story Dependencies

```plain
US6 (Config) ─┬─> US1 (Job Architecture) ─┬─> US4 (Logging) ─┬─> US9 (UI)
              │                           │                  └─> US5 (Interrupt)
              │                           └─> US8 (Dummy Jobs)
              │                           └─> US3 (Snapshots, Disk Monitor)
              │
              └─> US2 (Self-Install) ─> US7 (Install Script)
```

### Parallel Opportunities

Within each phase, tasks marked [P] can run in parallel:
- Phase 1: T003, T004, T005, T006, T007a
- Phase 2: T008, T009, T010, T012
- Phase 12: T097
- Phase 16: T118, T119, T120, T121, T122

---

## Parallel Example: Phase 2 Foundational

```bash
# Launch all parallel-safe foundational tasks together:
Task: "Implement CommandResult dataclass in src/pcswitcher/models.py"
Task: "Implement ProgressUpdate dataclass with validation in src/pcswitcher/models.py"
Task: "Implement ConfigError and ValidationError dataclasses in src/pcswitcher/models.py"
Task: "Implement LogEvent, ProgressEvent, ConnectionEvent dataclasses in src/pcswitcher/events.py"
```

---

## Implementation Strategy

### MVP First (Core Sync with Dummy Jobs)

1. Complete Phase 1-2: Setup + Foundational
2. Complete Phase 3: Configuration System (US6)
3. Complete Phase 4: Job Architecture (US1)
4. Complete Phase 5: Logging (US4)
5. Complete Phase 12: Dummy Jobs (US8)
6. Complete Phase 13-15: Orchestrator + Locking + CLI
7. **STOP and VALIDATE**: Test sync with dummy_success job
8. Deploy/demo minimal working sync

### Incremental Delivery

1. MVP (above) → Working sync command with dummy jobs
2. Add US9 (Terminal UI) → Rich progress display
3. Add US3 (Snapshots) → Safety infrastructure
4. Add US2 + US7 (Installation) → Self-deployment
5. Add US5 (Interrupt) → Graceful shutdown
6. Polish → Documentation, type checks, formatting

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story should be independently completable and testable
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- architecture.md is the canonical reference for component design
