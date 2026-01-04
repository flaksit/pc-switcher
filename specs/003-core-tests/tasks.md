# Tasks: Retroactive Tests for 001-Core

**Input**: Design documents from `/specs/003-core-tests/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md

**Purpose**: This feature IS test implementation - all tasks are test creation tasks.

**Organization**: Tasks are grouped by 001-core user story to enable independent implementation and testing. Each phase delivers complete test coverage for one user story from the 001-core spec.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which 001-core user story these tests cover (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- **Unit tests**: `tests/unit/<component>/test_<module>.py`
- **Integration tests**: `tests/integration/test_<user_story>.py`
- **Contract tests**: `tests/contract/test_<contract>.py`

---

## Phase 1: Setup (Test Infrastructure)

**Purpose**: Ensure test infrastructure is ready and create common fixtures

- [X] T001 Verify pytest, pytest-asyncio dependencies are available via `uv run pytest --version`
- [X] T002 [P] Create unit test directory structure: `tests/unit/orchestrator/`, `tests/unit/jobs/`, `tests/unit/cli/`
- [X] T003 [P] Add shared fixtures for mock JobContext in `tests/unit/conftest.py`
- [X] T004 [P] Add time-freezing fixtures for deterministic timestamp tests in `tests/unit/conftest.py`

---

## Phase 2: Core (Expand Existing Test Files)

**Purpose**: Expand existing test files that provide core for multiple user stories

**These files already exist and need additions per data-model.md**

- [X] T005 [P] Expand `tests/contract/test_job_interface.py` with US1-AS2 (config schema validation), US1-AS3 (job logging at all levels), US1-AS4 (job progress reporting) — Note: T010 also adds FR-001 test to this file; both tasks target same file intentionally
- [X] T006 [P] Expand `tests/unit/test_config_sync.py` with FR-007a (config sync prompt if missing), FR-007b (config diff and prompt), FR-007c (skip if configs match), US2-AS7 (skip when configs match)
- [X] T007 [P] Expand `tests/unit/test_lock.py` with FR-047 verification (lock prevents concurrent sync), edge case: concurrent sync attempts
- [X] T008 [P] Expand `tests/unit/jobs/test_disk_space_monitor.py` with FR-016 (preflight disk space check), FR-017 (runtime disk space monitoring) comprehensive coverage

**Checkpoint**: Core test files expanded - new test file creation can proceed

---

## Phase 3: Tests for 001-Core US-1 (Job Architecture) - Priority: P1

**Goal**: Complete test coverage for 001-core User Story 1 - Job Architecture and Integration Contract

**Independent Test**: Run `uv run pytest tests/unit/orchestrator/test_job_lifecycle.py tests/contract/test_job_interface.py -v`

### Unit Tests for US-1

- [X] T009 Create `tests/unit/orchestrator/test_job_lifecycle.py` with:
  - `test_001_fr002_lifecycle_validate_then_execute` (FR-002: validate then execute order)
  - `test_001_fr019_critical_on_exception` (FR-019: CRITICAL log and halt on exception)
  - `test_001_fr044_orchestrator_forwards_progress` (FR-044: progress forwarded to UI)
  - `test_001_fr048_log_sync_summary` (FR-048: overall result and job summary)
  - `test_001_us1_as5_validation_errors_halt_sync` (US1-AS5: validation errors halt sync)
  - `test_001_us1_as6_exception_handling` (US1-AS6: orchestrator catches exception)
  - `test_001_edge_cleanup_exception` (edge: job cleanup raises exception)
  - `test_001_edge_partial_job_failures` (edge: some jobs succeed, some fail)

- [X] T010 [P] Add to `tests/contract/test_job_interface.py` (same file as T005, no conflict):
  - `test_001_fr001_job_interface_contract` (FR-001: job interface defines standardized methods)

### Integration Tests for US-1

- [X] T011 Create `tests/integration/test_end_to_end_sync.py` with:
  - `test_001_us1_as1_job_integration_via_interface` (US1-AS1: job integrates via interface)
  - `test_001_us1_as7_interrupt_terminates_job` (US1-AS7: Ctrl+C terminates job)
  - `test_001_edge_target_unreachable_mid_sync` (edge: target unreachable mid-sync)

**Checkpoint**: US-1 (Job Architecture) fully tested

---

## Phase 4: Tests for 001-Core US-2 (Self-Installing Orchestrator) - Priority: P1

**Goal**: Complete test coverage for 001-core User Story 2 - Self-Installing Sync Orchestrator

**Independent Test**: Run `uv run pytest tests/unit/jobs/test_install_job.py tests/integration/test_self_installation.py -v`

### Unit Tests for US-2

- [X] T012 Create `tests/unit/jobs/test_install_job.py` with:
  - `test_001_fr005_version_check_and_install` (FR-005: check version, install from GitHub)
  - `test_001_fr006_abort_on_newer_target_version` (FR-006: abort if target newer)
  - `test_001_fr007_abort_on_install_failure` (FR-007: abort on installation failure)
  - `test_001_us2_as3_skip_when_versions_match` (US2-AS3: skip when versions match)
  - `test_001_us2_as4_abort_on_install_failure` (US2-AS4: abort on install failure)
  - `test_001_edge_target_newer_version` (edge: target has newer version)

### Integration Tests for US-2

- [X] T013 Create `tests/integration/jobs/test_install_on_target_job.py` with:
  - `test_001_us2_as1_install_missing_pcswitcher` (US2-AS1: install missing pc-switcher)
  - `test_001_us2_as2_upgrade_outdated_target` (US2-AS2: upgrade outdated target)
  - Note: US2-AS5 and US2-AS6 (config sync) covered by tests in `tests/unit/cli/test_config_sync.py` and `tests/integration/test_config_sync.py`

**Checkpoint**: US-2 (Self-Installing Orchestrator) fully tested

---

## Phase 5: Tests for 001-Core US-3 (Btrfs Snapshots) - Priority: P1

**Goal**: Complete test coverage for 001-core User Story 3 - Safety Infrastructure with Btrfs Snapshots

**Independent Test**: Run `uv run pytest tests/unit/jobs/test_snapshot_job.py tests/integration/test_snapshot_infrastructure.py -v`

### Unit Tests for US-3

- [X] T014 Create `tests/unit/jobs/test_snapshot_job.py` with:
  - `test_001_fr008_create_presync_snapshots` (FR-008: create pre-sync snapshots)
  - `test_001_fr009_create_postsync_snapshots` (FR-009: create post-sync snapshots)
  - `test_001_fr010_snapshot_naming_pattern` (FR-010: snapshot naming pattern)
  - `test_001_fr011_snapshots_always_active` (FR-011: snapshots always active)
  - `test_001_fr012_abort_on_snapshot_failure` (FR-012: abort if snapshot fails)
  - `test_001_fr014_cleanup_with_retention` (FR-014: cleanup with retention policy)
  - `test_001_fr015_validate_subvolumes_exist` (FR-015: validate subvolumes exist)
  - `test_001_fr015b_validate_snapshots_is_subvolume` (FR-015b: validate /.snapshots/ is subvolume)
  - `test_001_us3_as1_validate_subvolumes_exist` (US3-AS1: validate subvolumes)
  - `test_001_us3_as5_abort_if_snapshots_not_subvolume` (US3-AS5: abort if not subvolume)
  - `test_001_us3_as6_abort_on_snapshot_failure` (US3-AS6: abort on snapshot failure)
  - `test_001_us3_as8_preflight_disk_space_check` (US3-AS8: preflight disk space)
  - `test_001_edge_insufficient_space_snapshots` (edge: insufficient space)

### Integration Tests for US-3

- [X] T015 Create `tests/integration/test_snapshot_infrastructure.py` with:
  - `test_001_us3_as2_create_presync_snapshots` (US3-AS2: create pre-sync snapshots)
  - `test_001_us3_as3_create_postsync_snapshots` (US3-AS3: create post-sync snapshots)
  - `test_001_us3_as4_create_snapshots_subvolume` (US3-AS4: create /.snapshots/ subvolume)
  - `test_001_us3_as7_cleanup_snapshots_with_retention` (US3-AS7: cleanup with retention)
  - `test_001_us3_as9_runtime_disk_space_monitoring` (US3-AS9: runtime disk space monitoring)
  - `test_001_edge_btrfs_not_available` (edge: btrfs not available)

**Checkpoint**: US-3 (Btrfs Snapshots) fully tested

---

## Phase 6: Tests for 001-Core US-4 (Logging System) - Priority: P1

**Goal**: Complete test coverage for 001-core User Story 4 - Comprehensive Logging System

**Independent Test**: Run `uv run pytest tests/unit/orchestrator/test_logging_system.py tests/integration/test_logging_integration.py -v`

### Unit Tests for US-4

- [X] T016 Create `tests/unit/orchestrator/test_logging_system.py` with:
  - `test_001_fr018_log_level_ordering` (FR-018: six log levels with correct ordering)
  - `test_001_fr020_independent_log_levels` (FR-020: independent file and CLI log levels)
  - `test_001_fr021_timestamped_log_file` (FR-021: write logs to timestamped file)
  - `test_001_fr022_log_format_json_and_console` (FR-022: JSON Lines for file, console for terminal)
  - `test_001_fr045_progress_logged_at_full` (FR-045: progress logged at FULL level)
  - `test_001_us4_as1_debug_excluded_at_full_level` (US4-AS1: DEBUG excluded at FULL level)
  - `test_001_us4_as2_full_excluded_at_info_level` (US4-AS2: FULL excluded at INFO level)
  - `test_001_us4_as4_log_file_json_lines_format` (US4-AS4: JSON Lines format)

### Integration Tests for US-4

- [X] T017 Create `tests/integration/test_logging_integration.py` with:
  - `test_001_fr023_aggregate_source_target_logs` (FR-023: aggregate source and target logs)
  - `test_001_us4_as6_logs_command_displays_last_log` (US4-AS6: logs command displays recent log)

**Checkpoint**: US-4 (Logging System) fully tested

---

## Phase 7: Tests for 001-Core US-5 (Interrupt Handling) - Priority: P1

**Goal**: Complete test coverage for 001-core User Story 5 - Graceful Interrupt Handling

**Independent Test**: Run `uv run pytest tests/unit/orchestrator/test_interrupt_handling.py tests/integration/test_interrupt_integration.py -v`

### Unit Tests for US-5

- [X] T018 Create `tests/unit/orchestrator/test_interrupt_handling.py` with:
  - `test_001_fr003_termination_request_on_interrupt` (FR-003: termination request with cleanup timeout)
  - `test_001_fr024_sigint_handler_exit_130` (FR-024: SIGINT handler, log, exit 130)
  - `test_001_us5_as2_interrupt_between_jobs_skips_remaining` (US5-AS2: interrupt between jobs skips remaining)

### Integration Tests for US-5

- [X] T019 Create `tests/integration/test_interrupt_integration.py` with:
  - `test_001_fr025_terminate_target_processes` (FR-025: send termination to target processes)
  - `test_001_fr026_second_sigint_force_terminate` (FR-026: force-terminate on second SIGINT)
  - `test_001_fr027_no_orphaned_processes` (FR-027: no orphaned processes)
  - `test_001_us5_as1_interrupt_requests_job_termination` (US5-AS1: Ctrl+C requests job termination)
  - `test_001_us5_as3_second_interrupt_forces_termination` (US5-AS3: second Ctrl+C forces termination)
  - `test_001_edge_source_crash_timeout` (edge: source crashes mid-sync)

**Checkpoint**: US-5 (Interrupt Handling) fully tested

---

## Phase 8: Tests for 001-Core US-6 (Configuration System) - Priority: P1

**Goal**: Complete test coverage for 001-core User Story 6 - Configuration System

**Independent Test**: Run `uv run pytest tests/unit/orchestrator/test_config_system.py -v`

### Unit Tests for US-6

- [X] T020 Create `tests/unit/orchestrator/test_config_system.py` with:
  - `test_001_fr004_jobs_loaded_in_config_order` (FR-004: jobs loaded in config order)
  - `test_001_fr011_snapshots_always_active` (FR-011: snapshots always active - config aspect; T014 tests snapshot behavior aspect)
  - `test_001_fr028_load_from_config_path` (FR-028: load from ~/.config/pc-switcher/config.yaml)
  - `test_001_fr029_config_structure` (FR-029: YAML structure - global, sync_jobs, per-job)
  - `test_001_fr030_validate_job_configs` (FR-030: validate against job schemas)
  - `test_001_fr031_apply_config_defaults` (FR-031: apply defaults for missing values)
  - `test_001_fr032_enable_disable_via_sync_jobs` (FR-032: enable/disable via sync_jobs)
  - `test_001_fr033_config_error_messages` (FR-033: clear error on syntax/validation failure)
  - `test_001_us6_as1_load_and_validate_config` (US6-AS1: load, validate, apply defaults)
  - `test_001_us6_as2_independent_log_levels` (US6-AS2: independent file and CLI log levels)
  - `test_001_us6_as3_enable_disable_jobs` (US6-AS3: enable/disable jobs via sync_jobs)
  - `test_001_us6_as4_abort_on_missing_required_param` (US6-AS4: abort on missing required param)
  - `test_001_us6_as5_yaml_syntax_error_handling` (US6-AS5: YAML syntax error handling)
  - `test_001_edge_unknown_job_in_config` (edge: unknown job in config)

**Checkpoint**: US-6 (Configuration System) fully tested

---

## Phase 9: Tests for 001-Core US-7 (Installation Script) - Priority: P2

**Goal**: Complete test coverage for 001-core User Story 7 - Installation and Setup Infrastructure

**Independent Test**: Run `uv run pytest tests/integration/test_installation_script.py -v -m integration`

### Unit Tests for US-7

- [X] T021 Add to `tests/unit/jobs/test_install_job.py`:
  - `test_001_us7_as2_target_install_shared_logic` (US7-AS2: target install uses shared logic)

### Integration Tests for US-7

- [X] T022 Create `tests/integration/test_installation_script.py` with:
  - `test_001_fr035_install_script_no_prereqs` (FR-035: install.sh without prerequisites)
  - `test_001_fr036_default_config_with_comments` (FR-036: default config with inline comments)
  - `test_001_us7_as1_install_script_fresh_machine` (US7-AS1: curl install.sh on fresh machine)
  - `test_001_us7_as3_preserve_existing_config` (US7-AS3: preserve existing config)

**Checkpoint**: US-7 (Installation Script) fully tested

---

## Phase 10: Tests for 001-Core US-8 (Dummy Jobs) - Priority: P1

**Goal**: Complete test coverage for 001-core User Story 8 - Dummy Test Jobs

**Independent Test**: Run `uv run pytest tests/unit/jobs/test_dummy_jobs.py -v`

### Unit Tests for US-8

- [X] T023 Create `tests/unit/jobs/test_dummy_jobs.py` with:
  - `test_001_fr038_dummy_jobs_exist` (FR-038: two dummy jobs exist)
  - `test_001_fr039_dummy_success_behavior` (FR-039: dummy_success behavior - 20s, logs, progress)
  - `test_001_fr041_dummy_fail_exception` (FR-041: dummy_fail raises exception at 60%)
  - `test_001_fr042_dummy_jobs_termination` (FR-042: dummy jobs handle termination)
  - `test_001_fr043_job_progress_emission` (FR-043: jobs emit progress updates)
  - `test_001_us8_as1_dummy_success_completes` (US8-AS1: dummy_success completes with logs/progress)
  - `test_001_us8_as3_dummy_fail_raises_exception` (US8-AS3: dummy_fail raises exception at 60%)
  - `test_001_us8_as4_dummy_job_termination` (US8-AS4: dummy job handles termination request)

**Checkpoint**: US-8 (Dummy Jobs) fully tested

---

## Phase 11: Tests for 001-Core US-9 (Terminal UI) - Priority: P2

**Goal**: Complete test coverage for 001-core User Story 9 - Terminal UI with Progress Reporting

**Independent Test**: Run `uv run pytest tests/integration/test_terminal_ui.py -v -m integration`

### Integration Tests for US-9

- [X] T024 Create `tests/integration/test_terminal_ui.py` with:
  - `test_001_us9_as1_progress_display` (US9-AS1: display progress bar, percentage, job name)
  - `test_001_us9_as2_multi_job_progress` (US9-AS2: overall and individual job progress)
  - `test_001_us9_as3_logs_with_progress` (US9-AS3: logs displayed below progress indicators)

**Note**: These tests verify behavior exists. Visual appearance may require manual playbook verification per 002-testing-framework spec.

**Checkpoint**: US-9 (Terminal UI) fully tested

---

## Phase 12: Tests for CLI Commands

**Goal**: Complete test coverage for CLI commands (FR-046)

**Independent Test**: Run `uv run pytest tests/unit/cli/test_commands.py -v`

### Unit Tests for CLI

- [X] T025 Create `tests/unit/cli/test_commands.py` with:
  - `test_001_fr046_sync_command` (FR-046: single command `pc-switcher sync <target>`)

**Checkpoint**: CLI commands fully tested

---

## Phase 13: Config Sync Integration Tests

**Goal**: Complete integration test coverage for config sync (US2-AS5, US2-AS6, US7-AS2)

**Independent Test**: Run `uv run pytest tests/integration/test_config_sync.py -v -m integration`

### Integration Tests for Config Sync

- [X] T026 Expand `tests/integration/test_config_sync.py` with:
  - `test_001_us7_as2_target_install_shared_logic_integration` (US7-AS2: integration test for shared install logic)

**Checkpoint**: Config sync integration tests complete

---

## Phase 14: Polish & Verification

**Purpose**: Final validation and coverage verification

### Performance Verification

- [X] T027 Run full unit test suite: `uv run pytest tests/unit tests/contract -v` and verify <30s completion (FR-013 of 003 spec). If fails: optimize slow tests or split into separate test file. **Baseline**: 4-core CPU, 16GB RAM, SSD storage. **Result**: 214 tests pass in 1.17s
- [ ] T028 [P] Run full integration test suite: `uv run pytest tests/integration -v -m integration` and verify <15 minutes completion (SC-008 of 003 spec). If fails: identify bottleneck tests and optimize. **Baseline**: Same as T027 plus test VM with 2 vCPUs, 4GB RAM.

### Test Quality Verification

- [X] T029 [P] Run tests in random order: `uv run pytest tests/unit -v --randomly-seed=12345` to verify test independence (FR-009 of 003 spec). If fails: fix order-dependent tests by isolating state. **Result**: Tests pass with random seed 3809626195
- [ ] T036 Verify unit tests use mock executors, not real system operations (FR-011 of 003 spec). Grep for direct subprocess/os calls in `tests/unit/` - should only use mocks. If violations found: refactor to use mocks
- [ ] T037 [P] Verify integration tests execute real operations on test VMs (FR-012 of 003 spec). Review `tests/integration/` to confirm real btrfs, SSH, file operations. If mocked: refactor to use real operations
- [ ] T038 [P] Verify tests use fixtures from testing framework for VM access, event buses, cleanup (FR-010 of 003 spec). Check imports from `tests/conftest.py` and framework fixtures. If missing: add fixture usage

### Traceability Verification

- [ ] T030 Generate `contracts/coverage-map.yaml` from implemented tests for traceability verification. If coverage gaps found: add missing tests as new tasks
- [ ] T035 Verify all tests include docstring with spec reference (FR-007 of 003 spec). If missing: add docstrings before marking complete
- [ ] T039 Verify test function names include requirement ID (FR-008 of 003 spec). Pattern: `test_001_<req-id>_<description>`. Grep for non-conforming names. If found: rename to include requirement ID
- [ ] T040 Verify test failure output includes spec requirement ID for navigation (US-2-AS-2 of 003 spec). Run a failing test and confirm output shows which spec requirement failed. If not: add requirement ID to assertion messages

### Coverage Verification

- [ ] T031 Verify all 9 user stories from 001-core spec have corresponding tests. If gaps found: create tasks to add missing coverage
- [ ] T032 Verify all 44 active acceptance scenarios have corresponding test cases (3 removed: US4-AS3, US4-AS5, US8-AS2). If gaps found: create tasks to add missing coverage
- [ ] T033 Verify all 44 active functional requirements have corresponding test assertions (4 removed: FR-013, FR-034, FR-037, FR-040). If gaps found: create tasks to add missing coverage
- [ ] T034 Verify all 9 edge cases have test coverage. If gaps found: create tasks to add missing coverage
- [ ] T041 Verify each requirement has both success and failure path tests (FR-004, SC-004 of 003 spec). Create checklist mapping each requirement to its success/failure test functions. If gaps found: add missing path coverage

**Checkpoint**: All tests complete, verified, and traceable

---

## Test Writing Guidelines

These guidelines address edge-case policies from spec.md. Developers MUST follow these when writing tests:

### Gap Between Spec and Implementation
When tests find that spec requirements are not implemented or implemented incorrectly:
- Test MUST fail with clear assertion message indicating which spec requirement is not met
- Assertion message SHOULD include the spec ID (e.g., "FR-001 violation: job interface missing validate() method")

### Ambiguous Spec Interpretation
When a spec requirement is ambiguous:
- Test docstring MUST document the interpretation used
- If implementation differs from interpretation, test fails and forces clarification
- Example: `"""Tests FR-015. Interprets 'validate subvolumes exist' as checking both source and target."""`

### Extra Functionality Not in Spec
When implementation has functionality beyond what's specified:
- Such functionality SHOULD still be tested for correctness
- Test docstring SHOULD note this is implementation-specific, not spec-driven
- Log a warning during test discovery or in test output to prompt spec update consideration

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Core (Phase 2)**: Depends on Setup completion
- **US Phases (Phase 3-11)**: All depend on Core phase completion
  - User story phases can proceed in parallel (if staffed)
  - Or sequentially in priority order (US-1 through US-9)
- **CLI Tests (Phase 12)**: Can proceed in parallel with US phases
- **Config Sync Integration (Phase 13)**: Depends on US-2 and US-7 phases
- **Polish (Phase 14)**: Depends on all test implementation phases being complete

### Within Each User Story Phase

- Unit tests before integration tests (unit tests run faster, catch issues earlier)
- Tests for core functionality before edge cases
- Phase complete when all tests in that phase pass

### Parallel Opportunities

- All Setup tasks marked [P] can run in parallel
- All Core tasks marked [P] can run in parallel
- Once Core phase completes, user story phases can start in parallel (if team capacity allows)
- Unit and integration tests within a phase can be written in parallel (different files)

---

## Parallel Example: Phase 3 (US-1)

```bash
# Launch unit and integration test file creation in parallel:
Task: "Create tests/unit/orchestrator/test_job_lifecycle.py"
Task: "Create tests/integration/test_end_to_end_sync.py"

# These are different files with no dependencies between them
```

---

## Implementation Strategy

### MVP First (Core User Stories)

1. Complete Phase 1: Setup
2. Complete Phase 2: Core
3. Complete Phase 3: US-1 (Job Architecture) - most critical
4. **STOP and VALIDATE**: Verify unit tests pass, <30s
5. Complete Phase 8: US-6 (Configuration System) - needed for most other tests
6. Proceed with remaining P1 user stories (US-2, US-3, US-4, US-5, US-8)

### Incremental Delivery

1. Setup + Core -> Test infrastructure ready
2. Add US-1 tests -> Job architecture verified
3. Add US-6 tests -> Configuration system verified
4. Add US-3 tests -> Safety infrastructure verified
5. Add US-4 tests -> Logging system verified
6. Add US-5 tests -> Interrupt handling verified
7. Add US-2 tests -> Self-installation verified
8. Add US-8 tests -> Dummy jobs verified
9. Add US-7, US-9 tests (P2) -> Installation and UI verified
10. Polish phase -> Full coverage verified

---

## Summary

| Category | Count |
|----------|-------|
| Total Tasks | 41 |
| Setup Tasks | 4 |
| Core Tasks | 4 |
| US-1 Tests | 3 tasks (11 test functions) |
| US-2 Tests | 2 tasks (10 test functions) |
| US-3 Tests | 2 tasks (19 test functions) |
| US-4 Tests | 2 tasks (10 test functions) |
| US-5 Tests | 2 tasks (9 test functions) |
| US-6 Tests | 1 task (14 test functions) |
| US-7 Tests | 2 tasks (5 test functions) |
| US-8 Tests | 1 task (8 test functions) |
| US-9 Tests | 1 task (3 test functions) |
| CLI Tests | 1 task (1 test function) |
| Config Sync Integration | 1 task (1 test function) |
| Polish/Verification | 15 tasks |

**Coverage Summary**:
- User Stories: 9/9 (100%)
- Acceptance Scenarios: 44/44 (100%) — 3 removed from 001-spec: US4-AS3, US4-AS5, US8-AS2
- Functional Requirements: 44/44 (100%) — 4 removed/skipped from 001-spec: FR-013, FR-034, FR-037, FR-040
- Edge Cases: 9/9 (100%)

**Suggested MVP Scope**: Phases 1-3 (Setup, Core, US-1) - establishes test infrastructure and verifies job architecture

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific 001-core user story being tested
- Each user story phase can be independently completed and verified
- Test naming convention: `test_001_<req-id>_<description>` (001 = core feature) — verified by T039
- All tests must include docstrings referencing spec requirements — verified by T035
- Unit tests use mock executors (FR-011 of 003 spec) — verified by T036
- Integration tests use real VM operations (FR-012 of 003 spec) — verified by T037
- Tests use framework fixtures (FR-010 of 003 spec) — verified by T038
- Each requirement has success and failure path coverage (FR-004, SC-004) — verified by T041
- Test failure output includes spec ID for navigation (US-2-AS-2) — verified by T040
