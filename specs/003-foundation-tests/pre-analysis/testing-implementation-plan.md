# Testing Implementation Plan for 001-Foundation

This document provides the detailed implementation plan for comprehensive testing of the foundation feature.

For testing framework infrastructure (conftest fixtures, VM provisioning scripts, GitHub Actions workflow), see `specs/002-testing-framework/pre-analysis/testing-implementation-plan.md`.

## Test Files to Create

### Unit Tests (`tests/unit/`)

- `test_config.py`
- `test_models.py`
- `test_events.py`
- `test_disk.py`
- `test_btrfs_snapshots.py`
- `test_version.py` (extend existing)
- `test_lock.py`
- `test_logger.py`
- `test_executor.py`
- `test_ui.py`
- `test_jobs/test_base.py`
- `test_jobs/test_btrfs.py`
- `test_jobs/test_disk_space_monitor.py` (extend existing)
- `test_jobs/test_install_on_target.py`
- `test_jobs/test_dummy_success.py`
- `test_jobs/test_dummy_fail.py`

### Integration Tests (`tests/integration/`)

- `test_connection.py`
- `test_executor.py`
- `test_lock.py`
- `test_disk.py`
- `test_btrfs_snapshots.py`
- `test_logger.py`
- `test_jobs/test_btrfs.py`
- `test_jobs/test_install_on_target.py`
- `test_jobs/test_disk_space_monitor.py`
- `test_jobs/test_dummy_success.py`
- `test_jobs/test_dummy_fail.py`
- `test_orchestrator.py`
- `test_cli.py`
- `test_cleanup_snapshots.py`
- `test_install_script.py`

## Unit Test Specifications

### tests/unit/test_config.py

| Test Class | Test Methods |
|------------|--------------|
| `TestConfigurationFromYaml` | `test_load_valid_minimal_config`, `test_load_valid_full_config`, `test_file_not_found_raises`, `test_yaml_syntax_error`, `test_invalid_schema_rejects`, `test_invalid_log_level`, `test_disk_config_defaults`, `test_btrfs_config_defaults`, `test_job_configs_extracted`, `test_unknown_sync_job_rejected` |
| `TestDiskConfig` | `test_default_values`, `test_custom_values` |
| `TestBtrfsConfig` | `test_default_subvolumes`, `test_custom_subvolumes` |

### tests/unit/test_models.py

| Test Class | Test Methods |
|------------|--------------|
| `TestHost` | `test_source_value`, `test_target_value` |
| `TestLogLevel` | `test_ordering`, `test_comparison` |
| `TestCommandResult` | `test_success_true_on_zero`, `test_success_false_on_nonzero` |
| `TestProgressUpdate` | `test_valid_percent`, `test_heartbeat_default` |
| `TestSnapshot` | `test_name_property_format`, `test_from_path_valid`, `test_from_path_invalid_raises` |
| `TestJobResult` | `test_creation`, `test_duration_calculation` |
| `TestSyncSession` | `test_creation`, `test_status_values` |

### tests/unit/test_events.py

| Test Class | Test Methods |
|------------|--------------|
| `TestEventBus` | `test_subscribe_returns_queue`, `test_publish_to_all_subscribers`, `test_close_sends_sentinel`, `test_publish_after_close_ignored`, `test_multiple_subscribers_isolated` |
| `TestLogEvent` | `test_creation`, `test_frozen_immutable` |
| `TestProgressEvent` | `test_creation` |

### tests/unit/test_disk.py

| Test Class | Test Methods |
|------------|--------------|
| `TestParseThreshold` | `test_percentage_format`, `test_gib_format`, `test_mib_format`, `test_gb_format`, `test_mb_format`, `test_invalid_format_raises`, `test_zero_percent`, `test_large_value` |
| `TestParseDfOutput` | `test_parses_valid_output`, `test_returns_none_for_missing_mount`, `test_handles_multiline_output`, `test_handles_long_device_names` |
| `TestDiskSpace` | `test_frozen_immutable`, `test_all_fields_populated` |
| `TestCheckDiskSpaceLocal` | `test_success_with_local_executor`, `test_failure_raises_runtime_error` |

### tests/unit/test_btrfs_snapshots.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSnapshotName` | `test_pre_phase_format`, `test_post_phase_format`, `test_includes_timestamp` |
| `TestSessionFolderName` | `test_format`, `test_includes_session_id` |
| `TestParseOlderThan` | `test_days_format`, `test_weeks_format`, `test_hours_format`, `test_invalid_raises` |

### tests/unit/test_version.py (extend existing)

| Test Class | Test Methods |
|------------|--------------|
| `TestGetThisVersion` | (existing tests) |
| `TestParseVersionFromCliOutput` | `test_parse_simple_version`, `test_parse_prefixed_version`, `test_parse_dev_version`, `test_parse_with_newline`, `test_invalid_format_raises` |

### tests/unit/test_lock.py

| Test Class | Test Methods |
|------------|--------------|
| `TestGetLocalHostname` | `test_returns_string`, `test_returns_socket_gethostname` |
| `TestSyncLock` | `test_acquire_creates_file`, `test_release_removes_file`, `test_get_holder_info` |

### tests/unit/test_logger.py

| Test Class | Test Methods |
|------------|--------------|
| `TestGenerateLogFilename` | `test_format_includes_session_id`, `test_format_includes_timestamp` |
| `TestGetLogsDirectory` | `test_returns_correct_path` |
| `TestLogger` | `test_log_publishes_event`, `test_log_with_context` |

### tests/unit/test_executor.py

| Test Class | Test Methods |
|------------|--------------|
| `TestLocalExecutor` | `test_run_command_success`, `test_run_command_failure`, `test_run_command_timeout`, `test_start_process_returns_handle`, `test_terminate_all_processes` |

### tests/unit/test_ui.py

| Test Class | Test Methods |
|------------|--------------|
| `TestTerminalUI` | `test_set_current_step`, `test_start_and_stop` |
| `TestUIEventConsumption` | `test_consumes_log_events`, `test_consumes_progress_events`, `test_consumes_connection_events`, `test_respects_log_level_filter`, `test_stops_on_sentinel` |

### tests/unit/test_jobs/test_base.py

| Test Class | Test Methods |
|------------|--------------|
| `TestJobValidateConfig` | `test_empty_schema_accepts_any`, `test_schema_validates_required`, `test_schema_validates_types`, `test_errors_include_job_name` |
| `TestJobHelpers` | `test_validation_error_creates_correct_type`, `test_log_publishes_to_event_bus`, `test_report_progress_publishes_event` |

### tests/unit/test_jobs/test_btrfs.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSubvolumeToMountPoint` | `test_root_subvolume`, `test_home_subvolume`, `test_var_subvolume`, `test_invalid_name_raises` |
| `TestBtrfsSnapshotJobConfigSchema` | `test_requires_phase`, `test_requires_subvolumes`, `test_requires_session_folder`, `test_valid_config_passes`, `test_invalid_phase_rejected` |

### tests/unit/test_jobs/test_disk_space_monitor.py (extend existing)

| Test Class | Test Methods |
|------------|--------------|
| `TestDiskSpaceMonitorConfigSchema` | (existing tests) |
| `TestDiskSpaceMonitorValidateConfig` | (existing tests) |
| `TestDiskSpaceMonitorValidation` | (existing tests) |
| `TestDiskSpaceMonitorExecution` | `test_monitors_at_interval`, `test_logs_warning_at_threshold`, `test_raises_critical_below_minimum` |

### tests/unit/test_jobs/test_install_on_target.py

| Test Class | Test Methods |
|------------|--------------|
| `TestInstallOnTargetJobValidate` | `test_returns_empty_when_target_older`, `test_returns_empty_when_target_missing`, `test_returns_error_when_target_newer` |
| `TestInstallOnTargetJobExecute` | `test_skips_when_versions_match`, `test_installs_when_missing`, `test_upgrades_when_older` |

### tests/unit/test_jobs/test_dummy_success.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDummySuccessJobConfigSchema` | `test_schema_has_duration_fields`, `test_valid_config_passes`, `test_default_durations` |

### tests/unit/test_jobs/test_dummy_fail.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDummyFailJobConfigSchema` | `test_schema_has_duration_and_fail_at`, `test_valid_config_passes`, `test_default_values` |

### tests/unit/test_jobs/test_context.py

| Test Class | Test Methods |
|------------|--------------|
| `TestJobContext` | `test_frozen_immutable`, `test_all_fields_populated`, `test_type_annotations_correct` |

## Integration Test Specifications

### tests/integration/test_connection.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSSHConnection` | `test_connect_success`, `test_disconnect`, `test_run_command_on_target`, `test_keepalive_works`, `test_connection_loss_detection` |

### tests/integration/test_executor.py

| Test Class | Test Methods |
|------------|--------------|
| `TestLocalExecutorReal` | `test_run_command_real_success`, `test_run_command_real_failure`, `test_run_command_with_timeout`, `test_process_tracking` |
| `TestRemoteExecutorReal` | `test_run_command_on_target`, `test_run_command_real_failure`, `test_run_command_with_timeout`, `test_send_file`, `test_get_file`, `test_terminate_all_processes` |

### tests/integration/test_lock.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSyncLockReal` | `test_acquire_and_release`, `test_concurrent_access_blocked`, `test_holder_info_written`, `test_stale_lock_handling` |
| `TestTargetLock` | `test_acquire_target_lock`, `test_release_target_lock`, `test_concurrent_target_lock_blocked` |
| `TestLockChainBlocking` | `test_sync_a_to_b_blocks_sync_b_to_c` (while A→B sync is running, B→C sync should be blocked because B has source lock held) |

### tests/integration/test_disk.py

| Test Class | Test Methods |
|------------|--------------|
| `TestCheckDiskSpaceRemote` | `test_check_disk_space_on_target`, `test_returns_valid_disk_space` |

### tests/integration/test_btrfs_snapshots.py

| Test Class | Test Methods |
|------------|--------------|
| `TestCreateSnapshot` | `test_creates_readonly_snapshot`, `test_snapshot_path_correct` |
| `TestValidateSnapshotsDirectory` | `test_creates_if_missing`, `test_succeeds_if_exists` |
| `TestValidateSubvolumeExists` | `test_root_subvolume_exists`, `test_home_subvolume_exists`, `test_invalid_subvolume_fails` |
| `TestListSnapshots` | `test_lists_created_snapshots`, `test_empty_when_none` |
| `TestCleanupSnapshots` | `test_keeps_recent`, `test_deletes_old`, `test_respects_max_age` |

### tests/integration/test_logger.py

Note: These tests don't require VMs but are placed in integration/ because they test real file I/O and the full logging pipeline. They can run on any machine.

| Test Class | Test Methods |
|------------|--------------|
| `TestFileLoggerReal` | `test_creates_log_file`, `test_writes_json_lines`, `test_respects_log_level`, `test_aggregates_source_and_target_logs` |

### tests/integration/test_jobs/test_btrfs.py

| Test Class | Test Methods |
|------------|--------------|
| `TestBtrfsSnapshotJobReal` | `test_validate_success`, `test_validate_missing_subvolume`, `test_execute_creates_snapshots`, `test_execute_on_both_hosts` |

### tests/integration/test_jobs/test_install_on_target.py

| Test Class | Test Methods |
|------------|--------------|
| `TestInstallOnTargetJobReal` | `test_version_check_success`, `test_installs_when_missing`, `test_upgrades_when_older`, `test_skips_when_matching`, `test_errors_when_target_newer` (target version > source version should abort with CRITICAL) |

### tests/integration/test_jobs/test_disk_space_monitor.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDiskSpaceMonitorJobReal` | `test_monitors_source`, `test_monitors_target`, `test_logs_warning_at_threshold` |

### tests/integration/test_jobs/test_dummy_success.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDummySuccessJobReal` | `test_full_execution`, `test_logs_at_correct_levels`, `test_reports_progress`, `test_runs_on_both_hosts` |

### tests/integration/test_jobs/test_dummy_fail.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDummyFailJobReal` | `test_raises_at_configured_percent`, `test_orchestrator_catches_exception`, `test_logs_critical` |

### tests/integration/test_orchestrator.py

| Test Class | Test Methods |
|------------|--------------|
| `TestOrchestratorFullWorkflow` | `test_complete_sync_success`, `test_sync_with_validation_failure`, `test_sync_with_job_failure`, `test_all_phases_execute_in_order`, `test_cleanup_on_failure` |
| `TestOrchestratorJobDiscovery` | `test_discovers_enabled_jobs`, `test_skips_disabled_jobs`, `test_rejects_unknown_jobs` |
| `TestOrchestratorTermination` | `test_job_cleanup_timeout_triggers_force_kill` (when job doesn't cleanup within timeout, orchestrator force-kills processes) |
| `TestOrchestratorNetworkFailure` | `test_target_unreachable_mid_sync` (simulate network outage via iptables, verify CRITICAL log and abort) |

### tests/integration/test_cli.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSyncCommand` | `test_sync_success`, `test_sync_target_unreachable` |
| `TestSigintHandling` | `test_single_sigint_graceful`, `test_double_sigint_force`, `test_no_orphaned_processes` |
| `TestInitCommand` | `test_creates_config_file`, `test_preserves_existing` |
| `TestLogsCommand` | `test_list_logs`, `test_show_last_log` |

### tests/integration/test_cleanup_snapshots.py

| Test Class | Test Methods |
|------------|--------------|
| `TestCleanupSnapshotsCommand` | `test_cleanup_deletes_old`, `test_cleanup_keeps_recent`, `test_dry_run_no_changes` |

### tests/integration/test_install_script.py

| Test Class | Test Methods |
|------------|--------------|
| `TestInstallScript` | `test_fresh_install`, `test_upgrade_install`, `test_config_preserved_on_upgrade`, `test_installs_uv_if_missing`, `test_installs_btrfs_progs` |

## Implementation Order

Tasks within the same phase can be implemented **in parallel** by multiple agents.

**Prerequisite**: Testing framework infrastructure from `002-testing-framework` must be complete first.

### Phase 1: Unit Tests - Core Modules

**Can run in parallel (all independent):**
1. Implement `tests/unit/test_config.py`
2. Implement `tests/unit/test_models.py`
3. Implement `tests/unit/test_events.py`
4. Implement `tests/unit/test_disk.py`
5. Implement `tests/unit/test_btrfs_snapshots.py`
6. Extend `tests/unit/test_version.py`
7. Implement `tests/unit/test_lock.py`
8. Implement `tests/unit/test_logger.py`
9. Implement `tests/unit/test_executor.py`
10. Implement `tests/unit/test_ui.py`

### Phase 2: Unit Tests - Jobs

**Can run in parallel (all independent):**
11. Implement `tests/unit/test_jobs/test_base.py`
12. Implement `tests/unit/test_jobs/test_btrfs.py`
13. Extend `tests/unit/test_jobs/test_disk_space_monitor.py`
14. Implement `tests/unit/test_jobs/test_install_on_target.py`
15. Implement `tests/unit/test_jobs/test_dummy_success.py`
16. Implement `tests/unit/test_jobs/test_dummy_fail.py`
17. Implement `tests/unit/test_jobs/test_context.py`

### Phase 3: Integration Tests - Core Modules

**Can run in parallel (all independent):**
18. Implement `tests/integration/test_connection.py`
19. Implement `tests/integration/test_executor.py`
20. Implement `tests/integration/test_lock.py`
21. Implement `tests/integration/test_disk.py`
22. Implement `tests/integration/test_btrfs_snapshots.py`
23. Implement `tests/integration/test_logger.py`

### Phase 4: Integration Tests - Jobs

**Can run in parallel (all independent):**
24. Implement `tests/integration/test_jobs/test_btrfs.py`
25. Implement `tests/integration/test_jobs/test_install_on_target.py`
26. Implement `tests/integration/test_jobs/test_disk_space_monitor.py`
27. Implement `tests/integration/test_jobs/test_dummy_success.py`
28. Implement `tests/integration/test_jobs/test_dummy_fail.py`

### Phase 5: Full System Integration Tests

**Can run in parallel (all independent):**
29. Implement `tests/integration/test_orchestrator.py`
30. Implement `tests/integration/test_cli.py`
31. Implement `tests/integration/test_cleanup_snapshots.py`
32. Implement `tests/integration/test_install_script.py`

## Implementation Status

### Needs Review (verify completeness against spec.md standards)

These tests exist and need verification for spec compliance:

| Test File | Tests | Requirements to Verify | Status |
|-----------|-------|------------------------|--------|
| `tests/contract/test_job_interface.py` | 15 | FR-001 (job interface) | Needs review |
| `tests/contract/test_executor_contract.py` | 16 | Executor parity | Needs review |
| `tests/unit/test_lock.py` | 10 | FR-047 (locking mechanism) | Needs review |
| `tests/unit/test_jobs/test_disk_space_monitor.py` | 13 | FR-016, FR-017 (disk space checks) | Needs review |
| `tests/unit/test_config_sync.py` | 20 | FR-007a, FR-007b, FR-007c (config sync) | Needs review |
| `tests/integration/test_config_sync.py` | 9 | FR-007a, FR-007b, FR-007c (config sync) | Needs review |

**Verification criteria from spec.md:**
- FR-007: Test files MUST include docstrings/comments referencing spec requirements
- FR-008: Test function names MUST indicate the requirement being tested
- FR-009: Tests MUST be independent, no shared mutable state
- FR-010: Tests MUST use fixtures from testing framework
- FR-011: Unit tests MUST use mock executors
- FR-012: Integration tests MUST execute real operations on test VMs

### Out of Scope
- `tests/integration/test_vm_connectivity.py` - Framework smoke test
- `tests/integration/test_btrfs_operations.py` - Framework smoke test
- `tests/unit/test_cli_self_update.py` - PR #42, not 001-foundation scope
- `tests/test_version.py` (PEP 440/SemVer portion) - PR #42, not 001-foundation scope
- `tests/conftest.py`, `tests/unit/conftest.py`, `tests/integration/conftest.py` - Framework infrastructure

### Remaining Work
All other test files listed in this plan need to be implemented.
