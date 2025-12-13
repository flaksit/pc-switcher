# Data Model: Requirement-to-Test Mapping

**Feature**: Retroactive Tests for 001-Foundation
**Date**: 2025-12-11
**Purpose**: Complete mapping of spec requirements to test coverage

## Overview

This document provides explicit traceability from every requirement in specs/001-foundation/spec.md to the specific test(s) that validate it. This ensures 100% spec coverage per FR-001 through FR-003 of specs/003-foundation-tests/spec.md.

## Coverage Summary

| Category | Total in 001-Spec | Removed | Active | Tests Planned | Coverage |
|----------|-------------------|---------|--------|---------------|----------|
| User Stories | 9 | 0 | 9 | 9 | 100% |
| Acceptance Scenarios | 47 | 3 | 44 | 44 | 100% |
| Functional Requirements | 48 | 4 | 44 | 44 | 100% |
| Edge Cases | 9 | 0 | 9 | 9 | 100% |

**Removed items from 001-spec**:
- Acceptance Scenarios: US4-AS3, US4-AS5, US8-AS2 (marked "Removed" in spec)
- Functional Requirements: FR-013 (rollback deferred), FR-034, FR-037 (numbering gaps), FR-040 (removed)

## User Story Coverage

### US-1: Job Architecture and Integration Contract

**Spec Location**: specs/001-foundation/spec.md lines 21-63

**Test Files**:
- `tests/contract/test_job_interface.py` - Job interface contract tests (existing, needs expansion)
- `tests/unit/orchestrator/test_job_lifecycle.py` - Job lifecycle orchestration tests (new)
- `tests/integration/test_end_to_end_sync.py` - Full workflow verification (new)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US1-AS1 | Developer implements job conforming to interface | `test_001_us1_as1_job_integration_via_interface` | integration |
| US1-AS2 | Job defines config schema, system validates | `test_001_us1_as2_config_schema_validation` | unit |
| US1-AS3 | Job emits logs at six levels | `test_001_us1_as3_job_logging_at_all_levels` | unit |
| US1-AS4 | Job emits progress updates | `test_001_us1_as4_job_progress_reporting` | unit |
| US1-AS5 | Job validate() returns errors, sync halts | `test_001_us1_as5_validation_errors_halt_sync` | unit |
| US1-AS6 | Job raises exception, orchestrator catches | `test_001_us1_as6_exception_handling` | unit |
| US1-AS7 | User presses Ctrl+C, job receives termination | `test_001_us1_as7_interrupt_terminates_job` | integration |

### US-2: Self-Installing Sync Orchestrator

**Spec Location**: specs/001-foundation/spec.md lines 65-103

**Test Files**:
- `tests/unit/jobs/test_install_job.py` - InstallOnTargetJob logic tests (new)
- `tests/integration/test_self_installation.py` - Real installation workflows (new)
- `tests/unit/test_config_sync.py` - Config sync logic (existing, needs expansion)
- `tests/integration/test_config_sync.py` - Config sync workflows (existing, needs expansion)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US2-AS1 | Target missing pc-switcher, orchestrator installs from GitHub | `test_001_us2_as1_install_missing_pcswitcher` | integration |
| US2-AS2 | Version mismatch, orchestrator upgrades target | `test_001_us2_as2_upgrade_outdated_target` | integration |
| US2-AS3 | Versions match, skip installation | `test_001_us2_as3_skip_when_versions_match` | unit |
| US2-AS4 | Installation fails, abort sync | `test_001_us2_as4_abort_on_install_failure` | unit |
| US2-AS5 | Target has no config, prompt user and copy | `test_001_fr007a_config_sync_prompt_if_missing` | unit |
| US2-AS6 | Target config differs, show diff with options | `test_001_fr007b_config_diff_and_prompt` | unit |
| US2-AS7 | Configs match, skip config sync | `test_001_us2_as7_skip_when_configs_match` | unit |

### US-3: Safety Infrastructure with Btrfs Snapshots

**Spec Location**: specs/001-foundation/spec.md lines 105-147

**Test Files**:
- `tests/unit/jobs/test_snapshot_job.py` - Btrfs job logic tests (new)
- `tests/integration/test_snapshot_infrastructure.py` - Real btrfs operations (new)
- `tests/integration/test_btrfs_operations.py` - Low-level btrfs primitives (existing)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US3-AS1 | Validate subvolumes exist before snapshots | `test_001_us3_as1_validate_subvolumes_exist` | unit |
| US3-AS2 | Create pre-sync snapshots before jobs execute | `test_001_us3_as2_create_presync_snapshots` | integration |
| US3-AS3 | Create post-sync snapshots after success | `test_001_us3_as3_create_postsync_snapshots` | integration |
| US3-AS4 | Create /.snapshots/ as subvolume if missing | `test_001_us3_as4_create_snapshots_subvolume` | integration |
| US3-AS5 | Abort if /.snapshots/ is directory not subvolume | `test_001_us3_as5_abort_if_snapshots_not_subvolume` | unit |
| US3-AS6 | Abort if snapshot creation fails | `test_001_us3_as6_abort_on_snapshot_failure` | unit |
| US3-AS7 | Cleanup old snapshots with retention policy | `test_001_us3_as7_cleanup_snapshots_with_retention` | integration |
| US3-AS8 | Check preflight disk space minimum | `test_001_us3_as8_preflight_disk_space_check` | unit |
| US3-AS9 | Monitor disk space during sync | `test_001_us3_as9_runtime_disk_space_monitoring` | integration |

### US-4: Comprehensive Logging System

**Spec Location**: specs/001-foundation/spec.md lines 149-189

**Test Files**:
- `tests/unit/orchestrator/test_logging_system.py` - Log level filtering and routing (new)
- `tests/integration/test_logging_integration.py` - Unified logging from source+target (new)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US4-AS1 | DEBUG excluded when file level is FULL | `test_001_us4_as1_debug_excluded_at_full_level` | unit |
| US4-AS2 | FULL excluded when file level is INFO | `test_001_us4_as2_full_excluded_at_info_level` | unit |
| US4-AS4 | Log file contains JSON Lines format | `test_001_us4_as4_log_file_json_lines_format` | unit |
| US4-AS6 | `pc-switcher logs --last` displays recent log | `test_001_us4_as6_logs_command_displays_last_log` | integration |

**Note**: US4-AS3 and US4-AS5 were removed from 001-foundation spec (marked as "Removed").

### US-5: Graceful Interrupt Handling

**Spec Location**: specs/001-foundation/spec.md lines 191-220

**Test Files**:
- `tests/unit/orchestrator/test_interrupt_handling.py` - SIGINT handler logic (new)
- `tests/integration/test_interrupt_integration.py` - Real Ctrl+C simulation (new)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US5-AS1 | Ctrl+C during job, orchestrator requests termination | `test_001_us5_as1_interrupt_requests_job_termination` | integration |
| US5-AS2 | Ctrl+C between jobs, orchestrator skips remaining | `test_001_us5_as2_interrupt_between_jobs_skips_remaining` | unit |
| US5-AS3 | Second Ctrl+C forces immediate termination | `test_001_us5_as3_second_interrupt_forces_termination` | integration |

### US-6: Configuration System

**Spec Location**: specs/001-foundation/spec.md lines 222-280

**Test Files**:
- `tests/unit/orchestrator/test_config_system.py` - Config loading and validation (new)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US6-AS1 | Load config, validate, apply defaults | `test_001_us6_as1_load_and_validate_config` | unit |
| US6-AS2 | Independent file and CLI log levels | `test_001_us6_as2_independent_log_levels` | unit |
| US6-AS3 | Enable/disable jobs via sync_jobs section | `test_001_us6_as3_enable_disable_jobs` | unit |
| US6-AS4 | Missing required config parameter, abort | `test_001_us6_as4_abort_on_missing_required_param` | unit |
| US6-AS5 | Invalid YAML syntax, clear error | `test_001_us6_as5_yaml_syntax_error_handling` | unit |

### US-7: Installation and Setup Infrastructure

**Spec Location**: specs/001-foundation/spec.md lines 282-314

**Test Files**:
- `tests/integration/test_installation_script.py` - Real install.sh execution (new)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US7-AS1 | curl install.sh installs uv and pc-switcher | `test_001_us7_as1_install_script_fresh_machine` | integration |
| US7-AS2 | InstallOnTargetJob uses same installation logic | `test_001_us7_as2_target_install_shared_logic` | unit |
| US7-AS3 | Preserve existing config unless overwrite confirmed | `test_001_us7_as3_preserve_existing_config` | integration |

### US-8: Dummy Test Jobs

**Spec Location**: specs/001-foundation/spec.md lines 316-338

**Test Files**:
- `tests/unit/jobs/test_dummy_jobs.py` - Dummy job behaviors (new)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US8-AS1 | dummy_success completes with logs and progress | `test_001_us8_as1_dummy_success_completes` | unit |
| US8-AS3 | dummy_fail raises exception at 60% | `test_001_us8_as3_dummy_fail_raises_exception` | unit |
| US8-AS4 | Dummy job handles termination request | `test_001_us8_as4_dummy_job_termination` | unit |

**Note**: US8-AS2 was removed from 001-foundation spec (marked as "Removed").

### US-9: Terminal UI with Progress Reporting

**Spec Location**: specs/001-foundation/spec.md lines 340-359

**Test Files**:
- `tests/integration/test_terminal_ui.py` - Progress bar rendering (new, visual verification)

**Acceptance Scenarios**:

| ID | Scenario | Test Function | Type |
|----|----------|---------------|------|
| US9-AS1 | Display progress bar, percentage, job name | `test_001_us9_as1_progress_display` | integration |
| US9-AS2 | Show overall and individual job progress | `test_001_us9_as2_multi_job_progress` | integration |
| US9-AS3 | Display logs below progress indicators | `test_001_us9_as3_logs_with_progress` | integration |

**Note**: These tests verify behavior exists but may require manual playbook for visual verification per US-4 from 002-testing-framework spec.

## Functional Requirement Coverage

### Job Architecture (FR-001 through FR-004)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-001 | Job interface defines standardized methods | `test_001_fr001_job_interface_contract` | tests/contract/test_job_interface.py |
| FR-002 | Job lifecycle: validate() then execute() | `test_001_fr002_lifecycle_validate_then_execute` | tests/unit/orchestrator/test_job_lifecycle.py |
| FR-003 | Termination request with cleanup timeout | `test_001_fr003_termination_request_on_interrupt` | tests/unit/orchestrator/test_interrupt_handling.py |
| FR-004 | Jobs loaded from config in order | `test_001_fr004_jobs_loaded_in_config_order` | tests/unit/orchestrator/test_config_system.py |

### Self-Installation (FR-005 through FR-007c)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-005 | Check target version, install from GitHub | `test_001_fr005_version_check_and_install` | tests/unit/jobs/test_install_job.py |
| FR-006 | Abort if target version newer | `test_001_fr006_abort_on_newer_target_version` | tests/unit/jobs/test_install_job.py |
| FR-007 | Abort on installation failure | `test_001_fr007_abort_on_install_failure` | tests/unit/jobs/test_install_job.py |
| FR-007a | Sync config after install, prompt if missing | `test_001_fr007a_config_sync_prompt_if_missing` | tests/unit/test_config_sync.py |
| FR-007b | Show diff and prompt if configs differ | `test_001_fr007b_config_diff_and_prompt` | tests/unit/test_config_sync.py |
| FR-007c | Skip config sync if configs match | `test_001_fr007c_skip_if_configs_match` | tests/unit/test_config_sync.py |

### Safety Infrastructure (FR-008 through FR-017)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-008 | Create pre-sync snapshots | `test_001_fr008_create_presync_snapshots` | tests/unit/jobs/test_snapshot_job.py |
| FR-009 | Create post-sync snapshots | `test_001_fr009_create_postsync_snapshots` | tests/unit/jobs/test_snapshot_job.py |
| FR-010 | Snapshot naming pattern | `test_001_fr010_snapshot_naming_pattern` | tests/unit/jobs/test_snapshot_job.py |
| FR-011 | Snapshots always active (not configurable) | `test_001_fr011_snapshots_always_active` | tests/unit/orchestrator/test_config_system.py (config aspect) + tests/unit/jobs/test_snapshot_job.py (snapshot behavior aspect) |
| FR-012 | Abort if pre-sync snapshot fails | `test_001_fr012_abort_on_snapshot_failure` | tests/unit/jobs/test_snapshot_job.py |
| FR-014 | Snapshot cleanup with retention policy | `test_001_fr014_cleanup_with_retention` | tests/unit/jobs/test_snapshot_job.py |
| FR-015 | Validate subvolumes exist | `test_001_fr015_validate_subvolumes_exist` | tests/unit/jobs/test_snapshot_job.py |
| FR-015b | Validate /.snapshots/ is subvolume | `test_001_fr015b_validate_snapshots_is_subvolume` | tests/unit/jobs/test_snapshot_job.py |
| FR-016 | Preflight disk space check | `test_001_fr016_preflight_disk_space_check` | tests/unit/jobs/test_disk_space_monitor.py (existing) |
| FR-017 | Runtime disk space monitoring | `test_001_fr017_runtime_disk_space_monitoring` | tests/unit/jobs/test_disk_space_monitor.py (existing) |

### Logging System (FR-018 through FR-023)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-018 | Six log levels with correct ordering | `test_001_fr018_log_level_ordering` | tests/unit/orchestrator/test_logging_system.py |
| FR-019 | CRITICAL log and halt on exception | `test_001_fr019_critical_on_exception` | tests/unit/orchestrator/test_job_lifecycle.py |
| FR-020 | Independent file and CLI log levels | `test_001_fr020_independent_log_levels` | tests/unit/orchestrator/test_logging_system.py |
| FR-021 | Write logs to timestamped file | `test_001_fr021_timestamped_log_file` | tests/unit/orchestrator/test_logging_system.py |
| FR-022 | JSON Lines for file, console for terminal | `test_001_fr022_log_format_json_and_console` | tests/unit/orchestrator/test_logging_system.py |
| FR-023 | Aggregate source and target logs | `test_001_fr023_aggregate_source_target_logs` | tests/integration/test_logging_integration.py |

### Interrupt Handling (FR-024 through FR-027)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-024 | SIGINT handler, log, exit 130 | `test_001_fr024_sigint_handler_exit_130` | tests/unit/orchestrator/test_interrupt_handling.py |
| FR-025 | Send termination to target processes | `test_001_fr025_terminate_target_processes` | tests/integration/test_interrupt_integration.py |
| FR-026 | Force-terminate on second SIGINT | `test_001_fr026_second_sigint_force_terminate` | tests/integration/test_interrupt_integration.py |
| FR-027 | No orphaned processes | `test_001_fr027_no_orphaned_processes` | tests/integration/test_interrupt_integration.py |

### Configuration System (FR-028 through FR-033)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-028 | Load from ~/.config/pc-switcher/config.yaml | `test_001_fr028_load_from_config_path` | tests/unit/orchestrator/test_config_system.py |
| FR-029 | YAML structure: global, sync_jobs, per-job | `test_001_fr029_config_structure` | tests/unit/orchestrator/test_config_system.py |
| FR-030 | Validate against job schemas | `test_001_fr030_validate_job_configs` | tests/unit/orchestrator/test_config_system.py |
| FR-031 | Apply defaults for missing values | `test_001_fr031_apply_config_defaults` | tests/unit/orchestrator/test_config_system.py |
| FR-032 | Enable/disable jobs via sync_jobs | `test_001_fr032_enable_disable_via_sync_jobs` | tests/unit/orchestrator/test_config_system.py |
| FR-033 | Clear error on syntax/validation failure | `test_001_fr033_config_error_messages` | tests/unit/orchestrator/test_config_system.py |

### Installation & Setup (FR-035, FR-036)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-035 | install.sh without prerequisites | `test_001_fr035_install_script_no_prereqs` | tests/integration/test_installation_script.py |
| FR-036 | Default config with inline comments | `test_001_fr036_default_config_with_comments` | tests/integration/test_installation_script.py |

### Testing Infrastructure (FR-038, FR-039, FR-041, FR-042)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-038 | Two dummy jobs exist | `test_001_fr038_dummy_jobs_exist` | tests/unit/jobs/test_dummy_jobs.py |
| FR-039 | dummy_success behavior | `test_001_fr039_dummy_success_behavior` | tests/unit/jobs/test_dummy_jobs.py |
| FR-041 | dummy_fail raises exception at 60% | `test_001_fr041_dummy_fail_exception` | tests/unit/jobs/test_dummy_jobs.py |
| FR-042 | Dummy jobs handle termination | `test_001_fr042_dummy_jobs_termination` | tests/unit/jobs/test_dummy_jobs.py |

**Note**: FR-040 was removed from 001-foundation spec (marked as "Removed").

### Progress Reporting (FR-043 through FR-045)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-043 | Jobs can emit progress updates | `test_001_fr043_job_progress_emission` | tests/unit/jobs/test_dummy_jobs.py |
| FR-044 | Orchestrator forwards progress to UI | `test_001_fr044_orchestrator_forwards_progress` | tests/unit/orchestrator/test_job_lifecycle.py |
| FR-045 | Progress logged at FULL level | `test_001_fr045_progress_logged_at_full` | tests/unit/orchestrator/test_logging_system.py |

### Core Orchestration (FR-046 through FR-048)

| FR | Requirement | Test Function | File |
|----|-------------|---------------|------|
| FR-046 | Single command `pc-switcher sync <target>` | `test_001_fr046_sync_command` | tests/unit/cli/test_commands.py |
| FR-047 | Locking prevents concurrent execution | `test_001_fr047_lock_prevents_concurrent_sync` | tests/unit/test_lock.py (existing) |
| FR-048 | Log overall result and job summary | `test_001_fr048_log_sync_summary` | tests/unit/orchestrator/test_job_lifecycle.py |

## Edge Case Coverage

| Edge Case | Test Function | Type | File |
|-----------|---------------|------|------|
| Target unreachable mid-sync | `test_001_edge_target_unreachable_mid_sync` | integration | tests/integration/test_end_to_end_sync.py |
| Insufficient space for snapshots | `test_001_edge_insufficient_space_snapshots` | unit | tests/unit/jobs/test_snapshot_job.py |
| Job cleanup raises exception | `test_001_edge_cleanup_exception` | unit | tests/unit/orchestrator/test_job_lifecycle.py |
| Concurrent sync attempts | `test_001_edge_concurrent_sync_attempts` | unit | tests/unit/test_lock.py (existing) |
| Unknown job in config | `test_001_edge_unknown_job_in_config` | unit | tests/unit/orchestrator/test_config_system.py |
| Partial failures (some jobs succeed) | `test_001_edge_partial_job_failures` | unit | tests/unit/orchestrator/test_job_lifecycle.py |
| Target has newer version | `test_001_edge_target_newer_version` | unit | tests/unit/jobs/test_install_job.py |
| Source crashes mid-sync | `test_001_edge_source_crash_timeout` | integration | tests/integration/test_interrupt_integration.py |
| Btrfs not available | `test_001_edge_btrfs_not_available` | integration | tests/integration/test_snapshot_infrastructure.py |

## Test File Summary

### New Unit Test Files Required

1. **tests/unit/orchestrator/test_job_lifecycle.py**
   - FR-002, FR-019, FR-044, FR-048
   - US1-AS5, US1-AS6
   - Edge: cleanup exception, partial failures

2. **tests/unit/orchestrator/test_config_system.py**
   - FR-004, FR-011, FR-028 through FR-033
   - US6-AS1 through US6-AS5
   - Edge: unknown job, concurrent sync

3. **tests/unit/orchestrator/test_interrupt_handling.py**
   - FR-003, FR-024
   - US5-AS2

4. **tests/unit/orchestrator/test_logging_system.py**
   - FR-018, FR-020, FR-021, FR-022, FR-045
   - US4-AS1, US4-AS2, US4-AS4

5. **tests/unit/jobs/test_install_job.py**
   - FR-005 through FR-007
   - US2-AS3, US2-AS4
   - Edge: target newer version

6. **tests/unit/jobs/test_snapshot_job.py**
   - FR-008 through FR-015b
   - US3-AS1, US3-AS5, US3-AS6, US3-AS8
   - Edge: insufficient space, btrfs not available

7. **tests/unit/jobs/test_dummy_jobs.py**
   - FR-038, FR-039, FR-041, FR-042, FR-043
   - US8-AS1, US8-AS3, US8-AS4

8. **tests/unit/cli/test_commands.py**
   - FR-046

### New Integration Test Files Required

1. **tests/integration/test_end_to_end_sync.py**
   - US1-AS1, US1-AS7
   - Edge: target unreachable

2. **tests/integration/test_self_installation.py**
   - US2-AS1, US2-AS2, US2-AS5, US2-AS6

3. **tests/integration/test_snapshot_infrastructure.py**
   - US3-AS2, US3-AS3, US3-AS4, US3-AS7, US3-AS9

4. **tests/integration/test_logging_integration.py**
   - FR-023
   - US4-AS6

5. **tests/integration/test_interrupt_integration.py**
   - FR-025, FR-026, FR-027
   - US5-AS1, US5-AS3
   - Edge: source crash timeout

6. **tests/integration/test_terminal_ui.py**
   - US9-AS1, US9-AS2, US9-AS3

7. **tests/integration/test_installation_script.py**
   - FR-035, FR-036
   - US7-AS1, US7-AS3

### Existing Files to Expand

1. **tests/contract/test_job_interface.py** (existing)
   - Add: US1-AS2, US1-AS3, US1-AS4

2. **tests/unit/test_config_sync.py** (existing)
   - Add: FR-007a, FR-007b, FR-007c, US2-AS7

3. **tests/integration/test_config_sync.py** (existing)
   - Add: US7-AS2

4. **tests/unit/test_lock.py** (existing)
   - Add: FR-047 verification

5. **tests/unit/test_jobs/test_disk_space_monitor.py** (existing)
   - Add: FR-016, FR-017 comprehensive coverage

## Verification Checklist

Before marking tests complete:

- [ ] All 9 user stories have at least one test
- [ ] All 44 active acceptance scenarios map to specific test functions (3 removed: US4-AS3, US4-AS5, US8-AS2)
- [ ] All 44 active functional requirements have test assertions (4 removed: FR-013, FR-034, FR-037, FR-040)
- [ ] All 9 edge cases have test coverage
- [ ] Unit test suite runs in <30 seconds
- [ ] Integration test suite runs in <15 minutes
- [ ] All tests include docstring with spec reference
- [ ] contracts/coverage-map.yaml generated and validated

## Next Steps

Phase 2 (via `/speckit.tasks`) will generate actionable tasks from this mapping, organized by test file with clear acceptance criteria for each task.
