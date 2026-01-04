# Testing Report for 001-Core

This report provides a comprehensive overview of testing coverage for the foundation feature, including coverage mapping, how to run tests, limitations, and risk assessment.

## Coverage Summary

### Coverage by Module

| Module | Unit Tests | Integration Tests | Coverage |
|--------|------------|-------------------|----------|
| `cli.py` | Argument parsing | SIGINT handling, commands | Full |
| `orchestrator.py` | - | Full 9-phase workflow | Full |
| `config.py` | YAML parsing, validation, defaults | - | Full |
| `connection.py` | - | Connect/disconnect, keepalive | Full |
| `executor.py` | LocalExecutor (mocked) | LocalExecutor + RemoteExecutor (real) | Full |
| `events.py` | EventBus mechanics | - | Full |
| `logger.py` | Formatting, level filtering | Real file creation | Full |
| `lock.py` | Helper functions | Real fcntl locking | Full |
| `models.py` | All dataclasses, enums | - | Full |
| `btrfs_snapshots.py` | Pure functions | Real snapshot operations | Full |
| `disk.py` | Threshold parsing, df parsing | Real disk space checks | Full |
| `ui.py` | Event consumption | Progress/log event delivery | Full (visual layout is manual) |
| `version.py` | Version parsing | - | Full |
| `jobs/base.py` | Schema validation, helpers | - | Full |
| `jobs/btrfs.py` | Mount point mapping, schema | Full validate + execute | Full |
| `jobs/install_on_target.py` | Schema, version logic | Real install/upgrade | Full |
| `jobs/disk_space_monitor.py` | Schema, validation | Real monitoring | Full |
| `jobs/dummy_success.py` | Schema | Full execution | Full |
| `jobs/dummy_fail.py` | Schema | Exception handling | Full |
| `jobs/context.py` | JobContext dataclass | - | Full |
| `config_sync.py` | Config sync unit tests, integration tests | Full | Needs verification |
| `install.sh` | - | Fresh/upgrade/preserve | Full |

### Coverage by Spec Requirement

| Requirement | Test File(s) | Type |
|-------------|--------------|------|
| **FR-001** Job interface | `test_job_interface.py`, `test_base.py` | Contract, Unit |
| **FR-002** Job lifecycle | `test_orchestrator.py` | Integration |
| **FR-003** Termination handling | `test_cli.py` (SIGINT tests) | Integration |
| **FR-004** Job loading | `test_orchestrator.py` | Integration |
| **FR-005** Self-installation | `test_install_on_target.py` | Integration |
| **FR-006** Version mismatch abort | `test_install_on_target.py` | Unit, Integration |
| **FR-007** Install failure handling | `test_install_on_target.py` | Integration |
| **FR-008** Pre-sync snapshots | `test_btrfs.py`, `test_orchestrator.py` | Integration |
| **FR-009** Post-sync snapshots | `test_orchestrator.py` | Integration |
| **FR-010** Snapshot naming | `test_btrfs_snapshots.py` | Unit |
| **FR-011** Snapshot always active | `test_orchestrator.py` | Integration |
| **FR-012** Snapshot failure abort | `test_btrfs.py` | Integration |
| **FR-014** Snapshot cleanup | `test_cleanup_snapshots.py` | Integration |
| **FR-015** Subvolume validation | `test_btrfs.py` | Integration |
| **FR-015b** Snapshots dir validation | `test_btrfs_snapshots.py` | Integration |
| **FR-016** Preflight disk check | `test_orchestrator.py` | Integration |
| **FR-017** Runtime disk monitoring | `test_disk_space_monitor.py` | Integration |
| **FR-018** Log levels | `test_logger.py` | Unit |
| **FR-019** Exception → CRITICAL | `test_dummy_fail.py` | Integration |
| **FR-020** Independent log levels | `test_config.py`, `test_logger.py` | Unit |
| **FR-021** Log file creation | `test_logger.py` | Integration |
| **FR-022** JSON log format | `test_logger.py` | Unit, Integration |
| **FR-023** Unified log stream | `test_orchestrator.py` | Integration |
| **FR-024** SIGINT handler | `test_cli.py` | Integration |
| **FR-025** Target termination | `test_cli.py` | Integration |
| **FR-026** Double SIGINT force | `test_cli.py` | Integration |
| **FR-027** No orphaned processes | `test_cli.py` | Integration |
| **FR-028** Config file loading | `test_config.py` | Unit |
| **FR-029** YAML format | `test_config.py` | Unit |
| **FR-030** Schema validation | `test_config.py`, `test_base.py` | Unit |
| **FR-031** Default values | `test_config.py` | Unit |
| **FR-032** Job enable/disable | `test_config.py`, `test_orchestrator.py` | Unit, Integration |
| **FR-033** Config error messages | `test_config.py` | Unit |
| **FR-035** install.sh script | `test_install_script.py` | Integration |
| **FR-036** Default config comments | `test_install_script.py` | Integration |
| **FR-038** Dummy jobs exist | `test_dummy_success.py`, `test_dummy_fail.py` | Unit, Integration |
| **FR-039** dummy_success behavior | `test_dummy_success.py` | Integration |
| **FR-041** dummy_fail exception | `test_dummy_fail.py` | Integration |
| **FR-042** Termination handling | `test_dummy_success.py`, `test_dummy_fail.py` | Integration |
| **FR-043** Progress updates | `test_dummy_success.py` | Integration |
| **FR-044** Progress forwarding | `test_orchestrator.py` | Integration |
| **FR-045** Progress in FULL log | `test_logger.py` | Unit |
| **FR-046** Single sync command | `test_cli.py` | Integration |
| **FR-047** Locking mechanism | `test_lock.py` | Integration |
| **FR-048** Sync summary | `test_orchestrator.py` | Integration |

### Coverage by User Story

| User Story | Primary Test Files | Type |
|------------|-------------------|------|
| **US-1** Job Architecture | `test_job_interface.py`, `test_base.py`, `test_orchestrator.py` | Contract, Unit, Integration |
| **US-2** Self-Installation | `test_install_on_target.py`, `test_install_script.py` | Unit, Integration |
| **US-3** Safety Infrastructure | `test_btrfs.py`, `test_btrfs_snapshots.py`, `test_disk_space_monitor.py` | Unit, Integration |
| **US-4** Logging System | `test_logger.py`, `test_events.py` | Unit, Integration |
| **US-5** Interrupt Handling | `test_cli.py` (SIGINT tests) | Integration |
| **US-6** Configuration | `test_config.py` | Unit |
| **US-7** Installation/Setup | `test_install_script.py` | Integration |
| **US-8** Dummy Jobs | `test_dummy_success.py`, `test_dummy_fail.py` | Unit, Integration |
| **US-9** Terminal UI | `test_ui.py`, `test_orchestrator.py` | Unit (event delivery), Integration (full flow), Manual (visual layout/colors) |

## Implementation Status

### Tests Requiring Verification
These tests exist but need verification against spec.md standards (FR-007 through FR-012):

| Category | File | Tests | Covers | Verification Status |
|----------|------|-------|--------|---------------------|
| Contract | `test_job_interface.py` | 15 | FR-001 (job interface) | ⚠️ Needs review |
| Contract | `test_executor_contract.py` | 16 | Executor parity | ⚠️ Needs review |
| Unit | `test_lock.py` | 10 | FR-047 | ⚠️ Needs review |
| Unit | `test_jobs/test_disk_space_monitor.py` | 13 | FR-016, FR-017 | ⚠️ Needs review |
| Unit | `test_config_sync.py` | 20 | FR-007a, FR-007b, FR-007c | ⚠️ Needs review |
| Integration | `test_config_sync.py` | 9 | FR-007a, FR-007b, FR-007c | ⚠️ Needs review |

**Total tests requiring verification: 83**

### Out of Scope
| Category | File | Tests | Reason |
|----------|------|-------|--------|
| Integration | `test_vm_connectivity.py` | 14 | Framework smoke test |
| Integration | `test_btrfs_operations.py` | 11 | Framework smoke test |
| Unit | `test_cli_self_update.py` | 17 | PR #42, not 001-core |
| Top-level | `test_version.py` (PEP440/SemVer) | ~60 | PR #42, not 001-core |

## How to Run Tests

### Quick Start

```bash
# Run unit tests (fast, no VMs needed)
# Note: uv run automatically installs Python and syncs dependencies
uv run pytest tests/unit tests/contract -v

# Run with coverage report
uv run pytest tests/unit tests/contract --cov=src/pcswitcher --cov-report=html
open htmlcov/index.html
```

### Running Integration Tests

Integration tests require Hetzner Cloud VMs. See `docs/testing-framework.md` for full setup.

```bash
# Set environment variables
export PC_SWITCHER_TEST_PC1_HOST="<pc1-ip>"
export PC_SWITCHER_TEST_PC2_HOST="<pc2-ip>"
export PC_SWITCHER_TEST_USER="testuser"

# Run integration tests
uv run pytest tests/integration -v -m integration
```

### Running Specific Tests

```bash
# Run a specific test file
uv run pytest tests/unit/test_config.py -v

# Run a specific test class
uv run pytest tests/unit/test_config.py::TestConfigurationFromYaml -v

# Run a specific test method
uv run pytest tests/unit/test_config.py::TestConfigurationFromYaml::test_load_valid_minimal_config -v

# Run tests matching a pattern
uv run pytest -k "btrfs" -v
```

### Running in CI

Tests run automatically via GitHub Actions:
- **On push**: Unit tests + lint
- **On PR to main**: Unit tests + integration tests
- **On-demand**: Use workflow dispatch

## Limitations

### Unit Test Limitations

1. **Mocked I/O**: Unit tests use mocked subprocess and SSH connections. Bugs in the actual I/O layer will not be caught by unit tests alone.

2. **No real btrfs**: Unit tests for btrfs-related code test only pure logic (naming, path generation). Actual btrfs operations are tested only in integration tests.

3. **Time-dependent tests**: Tests involving timestamps (`snapshot_name`, `session_folder_name`) use `freezegun` library to freeze time, ensuring deterministic and reproducible results.

### Integration Test Limitations

1. **VM dependency**: Integration tests require Hetzner Cloud VMs. Cannot run locally without cloud access.

2. **Cost**: Persistent VMs cost ~€7/month total (2 CX23 VMs at €3.50 each). Tests can run anytime without per-hour costs.

3. **Network dependency**: Tests require stable network connectivity to Hetzner Cloud.

4. **Reset time**: Btrfs snapshot rollback requires VM reboot (~10-20 seconds). This is much faster than Hetzner's VM-level snapshot restore.

5. **Serial execution**: Integration tests must run serially due to shared VM state and locking.

### Not Covered by Automated Tests

1. **Terminal UI visual layout**: Progress bar appearance, colors, and rich formatting are verified only in manual playbook. However, automated tests verify that progress updates, log messages, and status changes are correctly delivered to the UI component.

2. **Intermittent network failures**: Small packet drops and high latency are difficult to simulate reliably. However, complete network outages are tested by temporarily blocking network access via iptables rules on the VM.

3. **Long-running stability**: Tests don't run for extended periods to catch memory leaks or resource exhaustion.

Note: **Disk space exhaustion** is tested by setting thresholds very high (e.g., "99%") to trigger low-space conditions without actually filling disks.

## Assumptions

1. **Python 3.14+**: Tests assume Python 3.14 or later is available (handled automatically by `uv run`).

2. **btrfs filesystem**: Integration tests assume VMs have btrfs root filesystem with `@` and `@home` subvolumes. *Automatically configured by OpenTofu provisioning scripts.*

3. **SSH access**: Integration tests assume SSH key-based authentication to VMs is configured. *Automatically configured by OpenTofu - SSH keys are deployed during VM creation, and `~/.ssh/config` is set up with pc1/pc2 hostnames.*

4. **sudo access**: Tests assume `testuser` has passwordless sudo on VMs. *Automatically configured by cloud-init during VM provisioning.*

5. **Network access**: Tests assume VMs can reach GitHub for install.sh tests. *Hetzner VMs have public IPs with unrestricted outbound access by default.*

6. **No concurrent modifications**: Tests assume no other processes are modifying the test VMs during test execution. Enforced by lock-based isolation.

## Risk Assessment

### High-Risk Tests (Require VM Isolation)

| Test | Risk | Potential Damage | Mitigation |
|------|------|------------------|------------|
| `test_btrfs_snapshots.py` (integration) | **HIGH** | Could corrupt btrfs filesystem if snapshot commands fail unexpectedly | Run only on dedicated test VMs; reset VMs after each session |
| `test_cleanup_snapshots.py` | **HIGH** | Deletes btrfs subvolumes; could delete wrong snapshots if path logic is buggy | Use unique test session IDs; verify paths before deletion; test only on VMs |
| `test_install_script.py` | **MEDIUM** | Installs packages via apt; could leave system in inconsistent state | Run only on VMs; reset after tests |
| `test_cli.py` (SIGINT) | **MEDIUM** | Spawns processes that might not terminate; could leave orphaned processes | Hard timeout fallback; process cleanup in fixtures; VM reset |
| `test_orchestrator.py` | **MEDIUM** | Runs full sync workflow including snapshots | VM isolation; test session cleanup |
| `test_lock.py` (integration) | **LOW** | Creates lock files that could interfere with real syncs | Use test-specific lock paths; cleanup fixtures |

### Medium-Risk Tests (Careful Implementation Required)

| Test | Risk | Concern | Mitigation |
|------|------|---------|------------|
| `test_executor.py` (integration) | Command injection if test inputs aren't sanitized | Use fixed, safe test commands |
| `test_disk.py` (integration) | Incorrect parsing could cause false positives/negatives | Verify against known df output formats |
| `test_install_on_target.py` | Downloads from GitHub; could fail on network issues | Retry logic; clear error messages |

### Low-Risk Tests (Safe)

| Test Category | Why Low Risk |
|---------------|--------------|
| All unit tests | Mocked I/O, no external effects |
| `test_config.py` | Only reads/parses files, no writes |
| `test_models.py` | Pure dataclass tests |
| `test_events.py` | In-memory queue operations only |
| `test_version.py` | String parsing only |

### Safety Measures Implemented

1. **VM Isolation**: All destructive tests run only on dedicated Hetzner Cloud VMs, never on developer machines or CI runners.

2. **Session ID Isolation**: Each test run uses a unique session ID (`test-<random>`) to prevent conflicts with real data.

3. **Cleanup Fixtures**: pytest fixtures clean up test artifacts. Note: Per-test cleanup is optional for many tests since the VM is reset to a clean btrfs snapshot before each test session. Cleanup fixtures are primarily useful for tests that run multiple scenarios within a single session.

4. **Lock-Based Exclusion**: Integration test lock prevents concurrent runs that could interfere with each other.

5. **Snapshot Reset**: VMs are reset to baseline snapshot before each test session.

6. **Timeout Protection**: Long-running tests have timeouts to prevent hangs.

7. **CI Concurrency Control**: GitHub Actions `concurrency.group` prevents parallel integration test runs.

### What Could Go Wrong

| Scenario | Impact | Likelihood | Detection |
|----------|--------|------------|-----------|
| Bug in cleanup fixture leaves test snapshots | VM disk fills up over time | Low | Monitoring, manual inspection |
| Lock script fails to release lock | Subsequent test runs blocked | Low | Timeout in lock acquisition |
| VM reset fails | Tests run on dirty state | Low | Reset script validates success |
| Test creates snapshot at wrong path | Could snapshot wrong subvolume | Very Low | Path validation in test setup |
| SIGINT test doesn't kill process | Orphaned process on VM | Low | Process listing after test; VM reset |

### Recommendations for Test Execution

1. **Never run integration tests on production machines** - always use dedicated test VMs.

2. **Review test output** before relying on results - look for unexpected errors or warnings.

3. **Monitor VM state** after test runs - check for leftover snapshots, processes, or lock files.

4. **Reset VMs periodically** even if tests pass - prevents state accumulation.

5. **Keep test VMs separate** from any real pc-switcher usage.
