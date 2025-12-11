# Research: Testing Approach for 001-Foundation

**Feature**: Retroactive Tests for 001-Foundation
**Date**: 2025-12-11
**Status**: Complete

## Overview

This document captures research findings for implementing comprehensive spec-driven tests for all 001-foundation functionality. The tests validate 100% of user stories, acceptance scenarios, and functional requirements from specs/001-foundation/spec.md.

## Key Decisions

### Decision 1: Spec-Driven vs Implementation-Driven Testing

**Chosen**: Spec-driven testing approach

**Rationale**:
- Tests validate **what** the system should do (from spec), not **how** it does it (from implementation)
- If spec and implementation disagree, tests fail, forcing alignment
- Tests remain valid even when implementation changes
- Directly supports FR-001 through FR-004 from 003-foundation-tests spec

**Alternatives Considered**:
- **Implementation-driven**: Write tests by reading code → Rejected because this validates existing behavior without checking if it matches requirements. Can miss gaps where spec requirements weren't implemented.
- **Hybrid approach**: Mix of spec and implementation → Rejected for complexity; creates ambiguity about which tests validate spec vs implementation details.

### Decision 2: Test Organization Strategy

**Chosen**: Dual organization - unit tests by component, integration tests by user story

**Rationale**:
- Unit tests organized by component (orchestrator/, jobs/, cli/) for developer convenience when working on specific modules
- Integration tests organized by user story (test_self_installation.py for US-2, etc.) for direct traceability to spec requirements
- Supports FR-005 through FR-008 from 003-foundation-tests spec
- Matches existing pytest structure from 002-testing-framework

**Alternatives Considered**:
- **All tests by user story**: Every test file maps to one US → Rejected because unit tests cross-cut multiple components; would create artificial file boundaries
- **All tests by component**: Every test file maps to one source module → Rejected because integration tests verify end-to-end workflows spanning multiple components

### Decision 3: Test Naming Convention

**Chosen**: Test function names include requirement IDs

**Pattern**:
```python
# Unit tests reference FR (Functional Requirement):
def test_fr001_job_interface_defines_validate_method() -> None: ...
def test_fr018_debug_level_includes_all_messages() -> None: ...

# Integration tests reference US (User Story) and AS (Acceptance Scenario):
async def test_us2_as1_install_missing_pcswitcher_on_target() -> None: ...
async def test_us3_as2_create_pre_sync_snapshots() -> None: ...

# Edge case tests reference US and note edge case:
async def test_us3_edge_insufficient_space_for_snapshots() -> None: ...
```

**Rationale**:
- Immediate traceability from failing test to specific spec requirement
- Supports FR-008 from 003-foundation-tests spec
- Test name alone tells developer which requirement is failing
- Grep-able: `grep "test_us2" tests/` shows all tests for User Story 2

**Alternatives Considered**:
- **Descriptive names only**: `test_job_interface_has_validate()` → Rejected because requires reading test docstring to find spec requirement; not grep-able
- **Docstring-only traceability**: Names without IDs, rely on docstrings → Rejected because pytest output shows function names, not docstrings; harder to trace failures in CI

### Decision 4: Mocking Strategy for Unit Tests

**Chosen**: Mock at the executor boundary

**Pattern**:
```python
# Mock RemoteExecutor and LocalExecutor, not individual SSH operations
mock_executor = AsyncMock(spec=RemoteExecutor)
mock_executor.run_command = AsyncMock(return_value=CommandResult(...))

# Jobs receive mocked executors via JobContext
context = JobContext(
    source=mock_local_executor,
    target=mock_remote_executor,
    ...
)
```

**Rationale**:
- Matches 001-foundation architecture (FR-001 defines job contract with executor injection)
- Unit tests validate job logic without real system operations (FR-011 from 003 spec)
- Consistent with existing test patterns (see tests/unit/test_jobs/test_disk_space_monitor.py)
- Executors are the clean architectural boundary between business logic and system operations

**Alternatives Considered**:
- **Mock SSH library directly**: Patch asyncssh methods → Rejected because too low-level; creates brittle tests that break when SSH implementation changes
- **No mocking, use real VMs for all tests**: → Rejected because violates FR-011 (unit tests must avoid real operations) and makes test suite too slow (>30s requirement)

### Decision 5: Integration Test VM Usage

**Chosen**: Use existing VM provisioning from 002-testing-framework

**Rationale**:
- Testing framework already provides pc1_executor and pc2_executor fixtures
- VMs automatically reset to baseline before each test session
- VM isolation ensures integration tests can't damage development machine
- Matches existing pattern (see tests/integration/test_btrfs_operations.py)
- Supports FR-012 from 003 spec (integration tests execute real operations on test VMs)

**Alternatives Considered**:
- **Docker containers instead of VMs**: → Rejected because btrfs operations require kernel features not available in containers; requires real VM with btrfs filesystem
- **Manual VM setup**: → Rejected because testing framework already automates this; would create maintenance burden

### Decision 6: Async Test Patterns

**Chosen**: Use pytest-asyncio with `@pytest.mark.asyncio` decorator

**Pattern**:
```python
@pytest.mark.asyncio
async def test_fr002_job_lifecycle_calls_validate_then_execute() -> None:
    """FR-002: System calls validate() then execute() in order."""
    mock_job = AsyncMock(spec=SyncJob)
    await orchestrator.run_jobs([mock_job])

    # Verify call order
    mock_job.validate.assert_called_once()
    mock_job.execute.assert_called_once()
    assert mock_job.validate.call_count == 1
    assert mock_job.execute.call_count == 1
```

**Rationale**:
- pc-switcher uses asyncio throughout (asyncssh, async job execution)
- pytest-asyncio is already a project dependency (002-testing-framework)
- Matches existing async test patterns in codebase
- Allows testing of async workflows (job execution, SSH operations, progress reporting)

**Alternatives Considered**:
- **Sync wrappers around async code**: Use `asyncio.run()` in sync tests → Rejected because obscures async behavior and makes tests harder to understand
- **Different async test framework**: trio, anyio → Rejected because pytest-asyncio is already in use and sufficient

## Testing Patterns by Component

### Pattern 1: Testing Job Interface Compliance (US-1)

**Component**: jobs/base.py (Job, SyncJob, SystemJob, BackgroundJob)

**Test Strategy**:
- Contract tests verify interface compliance (validate_config, validate, execute methods)
- Mock JobContext to isolate job logic from orchestrator
- Verify logging and progress reporting via EventBus mocks

**Example** (from existing tests/contract/test_job_interface.py):
```python
def test_job_has_name_attribute() -> None:
    """Jobs must have a name class attribute."""
    assert hasattr(ExampleTestJob, "name")
    assert ExampleTestJob.name == "example_test"

@pytest.mark.asyncio
async def test_validate_returns_validation_error_list(mock_job_context: JobContext) -> None:
    """validate() must return a list of ValidationError."""
    job = ExampleTestJob(mock_job_context)
    errors = await job.validate()
    assert isinstance(errors, list)
```

**Gap Analysis**: Need additional tests for:
- FR-003: Termination request handling
- FR-004: Job loading from config order
- Progress reporting patterns
- Error propagation

### Pattern 2: Testing Configuration System (US-6)

**Component**: config.py, jobs/base.py (CONFIG_SCHEMA validation)

**Test Strategy**:
- Unit tests verify YAML parsing, schema validation, default application
- Test both valid and invalid configs (success and failure paths per FR-004 from 003 spec)
- Verify ConfigError messages are actionable

**Example Pattern**:
```python
def test_fr030_config_validates_against_job_schema() -> None:
    """FR-030: Validate config against job-declared schemas."""
    # Invalid config should return ConfigError
    errors = DummyJob.validate_config({"invalid_field": "value"})
    assert len(errors) > 0
    assert isinstance(errors[0], ConfigError)

def test_fr031_config_applies_defaults_for_missing_values() -> None:
    """FR-031: Apply defaults for missing configuration values."""
    config = Config.load({})  # Empty config
    assert config.log_file_level == LogLevel.INFO  # Default
```

**Gap Analysis**: Need tests for:
- FR-028: Load from ~/.config/pc-switcher/config.yaml
- FR-029: YAML structure (global, sync_jobs, per-job sections)
- FR-032: Enable/disable optional jobs
- FR-033: Clear error messages for syntax errors

### Pattern 3: Testing Logging System (US-4)

**Component**: logger.py, events.py (LogEvent)

**Test Strategy**:
- Unit tests verify log level filtering (DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL)
- Test independent file vs CLI log levels
- Verify JSON Lines format for file output
- Mock file I/O to avoid writing during tests

**Example Pattern**:
```python
def test_fr018_log_level_ordering() -> None:
    """FR-018: DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL."""
    assert LogLevel.DEBUG > LogLevel.FULL
    assert LogLevel.FULL > LogLevel.INFO
    assert LogLevel.INFO > LogLevel.WARNING

def test_fr020_independent_file_and_cli_levels() -> None:
    """FR-020: File and CLI log levels configured independently."""
    config = Config.load({
        "log_file_level": "FULL",
        "log_cli_level": "INFO",
    })
    assert config.log_file_level == LogLevel.FULL
    assert config.log_cli_level == LogLevel.INFO

def test_fr022_log_format_json_lines_for_file(tmp_path: Path) -> None:
    """FR-022: File logs use JSON Lines format."""
    log_file = tmp_path / "test.log"
    logger = setup_logger(log_file, LogLevel.INFO)
    logger.info("test_event", field="value")

    # Verify JSON Lines format
    content = log_file.read_text()
    log_entry = json.loads(content.splitlines()[0])
    assert "timestamp" in log_entry
    assert "level" in log_entry
    assert log_entry["event"] == "test_event"
```

**Gap Analysis**: Need tests for:
- FR-019: CRITICAL logging when job raises exception
- FR-021: Write to timestamped file in logs directory
- FR-023: Aggregate logs from source and target

### Pattern 4: Testing Interrupt Handling (US-5)

**Component**: cli.py (SIGINT handler), orchestrator.py (job termination)

**Test Strategy**:
- Unit tests use asyncio Event/Future to simulate interrupt
- Integration tests use real Ctrl+C simulation on test VMs
- Verify cleanup timeout handling
- Verify no orphaned processes

**Example Pattern**:
```python
@pytest.mark.asyncio
async def test_fr024_sigint_requests_job_termination() -> None:
    """FR-024: SIGINT handler requests current job termination."""
    mock_job = AsyncMock(spec=SyncJob)
    mock_job.execute = AsyncMock(side_effect=asyncio.sleep(10))  # Long-running

    orchestrator = Orchestrator(...)

    # Simulate SIGINT after 1 second
    async def send_interrupt():
        await asyncio.sleep(1)
        orchestrator.handle_interrupt()

    asyncio.create_task(send_interrupt())

    with pytest.raises(InterruptError):
        await orchestrator.run_jobs([mock_job])

    # Verify job received termination request
    assert mock_job.terminate_requested
```

**Gap Analysis**: Need tests for:
- FR-025: Send termination to target-side processes
- FR-026: Force-terminate on second SIGINT
- FR-027: No orphaned processes (integration test)

### Pattern 5: Testing Btrfs Snapshots (US-3)

**Component**: jobs/btrfs.py, btrfs_snapshots.py

**Test Strategy**:
- Unit tests mock btrfs commands via executor
- Integration tests create real snapshots on test VMs
- Test both success and failure paths (invalid paths, insufficient space)
- Verify snapshot naming pattern and read-only flag

**Example Pattern** (from existing tests/integration/test_btrfs_operations.py):
```python
async def test_create_readonly_snapshot(pc1_executor: RemoteExecutor, test_volume: str) -> None:
    """Test creating a read-only btrfs snapshot."""
    snapshot_name = "/test-vol/.snapshots/test-snapshot-readonly"

    try:
        result = await pc1_executor.run_command(
            f"sudo btrfs subvolume snapshot -r /test-vol {snapshot_name}"
        )
        assert result.success

        # Verify it's read-only
        check_readonly = await pc1_executor.run_command(
            f"sudo btrfs property get {snapshot_name} ro"
        )
        assert "ro=true" in check_readonly.stdout
    finally:
        await pc1_executor.run_command(f"sudo btrfs subvolume delete {snapshot_name}")
```

**Gap Analysis**: Need tests for:
- FR-008, FR-009: Pre-sync and post-sync snapshot creation
- FR-010: Snapshot naming pattern validation
- FR-014: Cleanup command with retention policy
- FR-015, FR-015b: Subvolume existence validation
- FR-016, FR-017: Disk space monitoring

### Pattern 6: Testing Self-Installation (US-2)

**Component**: jobs/install_on_target.py, config_sync.py

**Test Strategy**:
- Unit tests mock uv tool install, version checks, file operations
- Integration tests perform real installation on test VMs (start with clean VM, install pc-switcher, verify)
- Test version mismatch scenarios (older, newer, missing)
- Test config sync prompts and diffs

**Example Pattern**:
```python
@pytest.mark.asyncio
async def test_fr005_check_target_version() -> None:
    """FR-005: Check target pc-switcher version before operations."""
    mock_executor = AsyncMock(spec=RemoteExecutor)
    mock_executor.run_command = AsyncMock(
        return_value=CommandResult(exit_code=0, stdout="0.3.2")
    )

    install_job = InstallOnTargetJob(context)
    target_version = await install_job.get_target_version()

    assert target_version == "0.3.2"
    mock_executor.run_command.assert_called_with("pc-switcher --version")
```

**Gap Analysis**: Need tests for:
- FR-006: Abort on newer target version
- FR-007: Abort on installation failure
- FR-007a, FR-007b, FR-007c: Config sync with prompts

## Testing Anti-Patterns to Avoid

Based on constitution principle "Deliberate Simplicity" and existing codebase patterns:

1. **Don't test implementation details**: Test behavior from spec, not internal method names or private attributes
2. **Don't create test helpers prematurely**: Copy-paste is acceptable until 3+ duplications justify abstraction
3. **Don't mock the world**: Mock at architectural boundaries (executors), not every function call
4. **Don't skip failure paths**: Per FR-004 from 003 spec, every test must verify both success and failure
5. **Don't write flaky tests**: Integration tests must clean up properly; use try/finally for VM artifacts

## Coverage Measurement Approach

**Chosen**: Manual coverage tracking via data-model.md mapping

**Rationale**:
- Code coverage tools measure lines executed, not requirements validated
- Spec-driven testing requires requirement-to-test mapping, not line coverage
- data-model.md provides explicit traceability: each FR/US/AS lists covering tests

**Coverage Criteria** (from 003 spec):
- SC-001: 100% of user stories have corresponding tests
- SC-002: 100% of acceptance scenarios have corresponding test cases
- SC-003: 100% of functional requirements have corresponding test assertions

**Verification Method**:
- Phase 1 generates contracts/coverage-map.yaml: machine-readable requirement→test mapping
- CI can parse coverage-map.yaml to verify 100% coverage before merge

## Performance Budgets

From 003-foundation-tests spec:

- **Unit test suite**: Must complete in <30 seconds (FR-013 from 003 spec)
- **Integration test suite**: Should complete in <15 minutes (SC-008 from 003 spec)

**Strategies to meet budgets**:
- Use pytest marks to separate fast unit tests from slow integration tests
- Run unit tests in parallel with `pytest -n auto`
- Integration tests can run sequentially (VM operations are I/O bound, not CPU bound)
- Module-scoped fixtures for expensive VM setup (see test_btrfs_operations.py pattern)

## Tools and Libraries

All testing dependencies already present from 002-testing-framework:

- **pytest**: Test runner and assertion framework
- **pytest-asyncio**: Async test support
- **pytest-xdist**: Parallel test execution
- **unittest.mock**: Mocking and spy utilities
- **asyncssh**: SSH library (same as production code)

No new dependencies required for 003-foundation-tests.

## Next Steps

Phase 1 will generate:
- **data-model.md**: Requirement-to-test mapping for all 9 user stories, 50+ acceptance scenarios, 48 functional requirements
- **contracts/coverage-map.yaml**: Machine-readable mapping for CI verification
- **quickstart.md**: Developer guide for running and writing tests

These artifacts enable tasks.md generation in Phase 2.
