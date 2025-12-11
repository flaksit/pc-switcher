# Quickstart: Running and Writing Tests for 001-Foundation

**Audience**: pc-switcher developers writing or running tests
**Last Updated**: 2025-12-11

## Running Tests

### Run All Unit Tests (Fast, <30 seconds)

```bash
uv run pytest tests/unit tests/contract -v
```

Unit tests use mocks and don't require VMs. Safe to run on any development machine.

### Run All Integration Tests (Requires VMs, <15 minutes)

```bash
# Requires HCLOUD_TOKEN and PC_INTEGRATION_ENABLED environment variables
uv run pytest tests/integration -v -m integration
```

Integration tests provision VMs automatically if not present and reset them to baseline before running.

### Run Tests for Specific Component

```bash
# Tests for logging system only
uv run pytest tests/unit/orchestrator/test_logging_system.py -v

# Tests for btrfs snapshots only
uv run pytest tests/unit/jobs/test_snapshot_job.py tests/integration/test_snapshot_infrastructure.py -v

# Tests for specific requirement
uv run pytest -k "test_001_fr018" -v
```

### Run Tests for Specific User Story

```bash
# All tests for US-3 (Safety Infrastructure)
uv run pytest -k "us3" -v

# All tests for US-2 (Self-Installation)
uv run pytest -k "us2" -v
```

### Run in Parallel (Faster)

```bash
# Run unit tests in parallel (one process per CPU core)
uv run pytest tests/unit tests/contract -v -n auto
```

**Note**: Integration tests run sequentially (VM operations are I/O bound, parallelization doesn't help).

### Check Which Tests Cover a Requirement

```bash
# Find all tests for FR-018 (log level ordering)
grep -r "FR-018" tests/

# Find all tests for US-3-AS2 (create pre-sync snapshots)
grep -r "US3-AS2" tests/
```

Or consult `specs/003-foundation-tests/contracts/coverage-map.yaml` for machine-readable mapping.

## Writing New Tests

### 1. Determine Test Type

**Unit Test** if:
- Testing pure logic without external dependencies
- Can mock executors/connections
- Needs to run fast (<1 second per test)

**Integration Test** if:
- Testing real system operations (btrfs, SSH, file I/O)
- Needs VMs with btrfs filesystem
- Tests end-to-end workflows

**Contract Test** if:
- Verifying interface compliance (job interface, executor contract)
- Testing architectural boundaries

### 2. Choose Test File Location

Follow the structure from `specs/003-foundation-tests/data-model.md`:

- **Unit tests**: `tests/unit/<component>/test_<module>.py`
  - Example: `tests/unit/orchestrator/test_job_lifecycle.py`
- **Integration tests**: `tests/integration/test_<user_story>.py`
  - Example: `tests/integration/test_self_installation.py`
- **Contract tests**: `tests/contract/test_<contract>.py`
  - Example: `tests/contract/test_job_interface.py`

### 3. Name Test Function with Feature + Requirement ID

**Pattern**: `test_<feature>_<req-id>_<description>()`

**Format Rules**:
- `<feature>`: Always `001` for foundation tests (ensures uniqueness across SpecKit features)
- `<req-id>`: `fr###` for functional requirements, `us#_as#` for acceptance scenarios
- `<description>`: Descriptive name in snake_case

**Examples**:
```python
def test_001_fr018_log_level_ordering() -> None:
    """FR-018: DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL."""
    assert LogLevel.DEBUG > LogLevel.FULL
    assert LogLevel.FULL > LogLevel.INFO

async def test_001_us2_as1_install_missing_pcswitcher() -> None:
    """US-2 AS-1: Target missing pc-switcher, orchestrator installs from GitHub."""
    # Test implementation

def test_001_fr028_load_from_config_path() -> None:
    """FR-028: System MUST load configuration from ~/.config/pc-switcher/config.yaml."""
    # Test implementation
```

**Benefits**:
- Unique across all SpecKit features (001, 002, 003, etc.)
- Grep-able: `pytest -k "001_fr028"` runs all FR-028 tests
- CI output shows feature + requirement: `test_001_us3_as2_create_presync_snapshots FAILED`

### 4. Unit Test Template

```python
"""Unit tests for [component name]."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.models import CommandResult, LogLevel
from pcswitcher.[module] import [Component]


@pytest.fixture
def mock_job_context() -> JobContext:
    """Create a mock JobContext for testing."""
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    target = MagicMock()
    target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

    return JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test1234",
        source_hostname="source-host",
        target_hostname="target-host",
    )


class TestComponentBehavior:
    """Test [component] behavior per spec requirements."""

    @pytest.mark.asyncio
    async def test_001_fr###_requirement_description(self, mock_job_context: JobContext) -> None:
        """FR-###: Brief requirement description from spec."""
        # Arrange
        component = Component(mock_job_context)

        # Act
        result = await component.do_something()

        # Assert
        assert result.success
        mock_job_context.source.run_command.assert_called_once()
```

### 5. Integration Test Template

```python
"""Integration tests for [feature/user story]."""

from __future__ import annotations

import pytest

from pcswitcher.executor import RemoteExecutor


@pytest.mark.integration
async def test_001_us#_as#_scenario_description(
    pc1_executor: RemoteExecutor,
    pc2_executor: RemoteExecutor,
) -> None:
    """US-# AS-#: Brief scenario description from spec.

    This test verifies [what is being tested] by performing real
    operations on test VMs with btrfs filesystems.
    """
    # Arrange: Set up initial state on VMs
    setup_result = await pc1_executor.run_command("sudo btrfs subvolume create /test")
    assert setup_result.success

    try:
        # Act: Perform the operation being tested
        result = await pc1_executor.run_command("sudo btrfs subvolume snapshot /test /test-snap")

        # Assert: Verify expected behavior
        assert result.success
        verify = await pc1_executor.run_command("sudo btrfs subvolume show /test-snap")
        assert verify.success

    finally:
        # Cleanup: Remove test artifacts
        await pc1_executor.run_command("sudo btrfs subvolume delete /test-snap", timeout=10.0)
        await pc1_executor.run_command("sudo btrfs subvolume delete /test", timeout=10.0)
```

### 6. Testing Async Code

All jobs and executors use `async`/`await`. Mark async tests with `@pytest.mark.asyncio`:

```python
@pytest.mark.asyncio
async def test_async_operation() -> None:
    """Test asynchronous operation."""
    executor = AsyncMock(spec=RemoteExecutor)
    executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="ok"))

    result = await executor.run_command("test")

    assert result.success
    executor.run_command.assert_awaited_once()
```

### 7. Mocking at the Right Level

**Good** - Mock at executor boundary:
```python
mock_executor = AsyncMock(spec=RemoteExecutor)
mock_executor.run_command = AsyncMock(return_value=CommandResult(...))
```

**Bad** - Mock low-level SSH details:
```python
# Don't do this - too brittle
mock_asyncssh = MagicMock()
mock_asyncssh.connect = AsyncMock()
```

**Why**: Executor is the architectural boundary. Jobs depend on executors, not SSH implementation.

### 8. Test Both Success and Failure Paths

Per FR-004 from 003-foundation-tests spec, every requirement must have tests covering both success and failure paths.

**Approach is flexible** - choose what's cleaner for each case:

**Option A: Separate functions** (when success/failure have different setup or are complex):
```python
def test_001_fr030_validate_job_configs_success() -> None:
    """FR-030: Valid config passes validation."""
    errors = Job.validate_config({"valid": "config"})
    assert errors == []

def test_001_fr030_validate_job_configs_failure() -> None:
    """FR-030: Invalid config returns ConfigError."""
    errors = Job.validate_config({"invalid": 123})
    assert len(errors) > 0
    assert isinstance(errors[0], ConfigError)
```

**Option B: Combined function** (when both paths are simple and related):
```python
def test_001_fr030_validate_job_configs() -> None:
    """FR-030: Config validation accepts valid configs and rejects invalid ones."""
    # Success path
    errors = Job.validate_config({"valid": "config"})
    assert errors == []

    # Failure path
    errors = Job.validate_config({"invalid": 123})
    assert len(errors) > 0
    assert isinstance(errors[0], ConfigError)
```

### 9. Use Fixtures from conftest.py

**Available fixtures** (from `tests/conftest.py` and `tests/integration/conftest.py`):

- `pc1_executor`: RemoteExecutor connected to test VM 1
- `pc2_executor`: RemoteExecutor connected to test VM 2
- `tmp_path`: Temporary directory (pytest built-in)

**Example**:
```python
async def test_something(pc1_executor: RemoteExecutor) -> None:
    """Test using VM fixture."""
    result = await pc1_executor.run_command("ls /")
    assert result.success
```

### 10. Ensure Test Independence (FR-009)

Tests MUST be independent and not rely on execution order or shared mutable state:

**Rules**:
- Each test sets up its own state via fixtures/mocks
- No global variables modified between tests
- No test should depend on another test running first
- Integration tests get clean VM state via btrfs snapshot reset

**Verification**: Run tests in random order to catch hidden dependencies:
```bash
# Install pytest-randomly if needed
uv add --dev pytest-randomly

# Run with random order
uv run pytest tests/unit -v --randomly-seed=12345

# Run again with different seed to verify
uv run pytest tests/unit -v --randomly-seed=67890
```

If tests pass with one seed but fail with another, there's an order dependency to fix.

**Common violations to avoid**:
```python
# BAD: Module-level mutable state
_cache = {}  # Shared between tests!

def test_first():
    _cache["key"] = "value"

def test_second():
    assert _cache["key"] == "value"  # Depends on test_first running first!

# GOOD: Use fixtures for isolation
@pytest.fixture
def cache():
    return {}

def test_first(cache):
    cache["key"] = "value"
    assert cache["key"] == "value"

def test_second(cache):
    # Gets fresh empty cache
    assert "key" not in cache
```

### 11. Verify Test Coverage

After writing tests, verify they appear in the coverage map:

```bash
# Check coverage-map.yaml includes your test
grep "test_001_fr###" specs/003-foundation-tests/contracts/coverage-map.yaml
```

## Common Patterns

### Pattern: Test Job Lifecycle

```python
@pytest.mark.asyncio
async def test_001_fr002_lifecycle_validate_then_execute() -> None:
    """FR-002: System calls validate() then execute() in order."""
    mock_job = AsyncMock(spec=SyncJob)
    mock_job.validate = AsyncMock(return_value=[])
    mock_job.execute = AsyncMock()

    orchestrator = Orchestrator(...)
    await orchestrator.run_jobs([mock_job])

    # Verify call order
    mock_job.validate.assert_awaited_once()
    mock_job.execute.assert_awaited_once()

    # Verify validate called before execute
    call_order = [call[0] for call in mock_job.method_calls]
    assert call_order.index("validate") < call_order.index("execute")
```

### Pattern: Test Configuration Validation

```python
def test_001_fr030_validate_against_job_schema() -> None:
    """FR-030: Validate config against job-declared schemas."""
    # Test with invalid config
    errors = DummyJob.validate_config({"unknown_field": "value"})

    assert len(errors) > 0
    assert isinstance(errors[0], ConfigError)
    assert errors[0].job == "dummy_test"
    assert "unknown_field" in errors[0].message or "not allowed" in errors[0].message
```

### Pattern: Test Log Level Filtering

```python
def test_001_fr020_independent_file_and_cli_levels() -> None:
    """FR-020: File and CLI log levels configured independently."""
    config = Config.load({
        "log_file_level": "FULL",
        "log_cli_level": "INFO",
    })

    assert config.log_file_level == LogLevel.FULL
    assert config.log_cli_level == LogLevel.INFO
```

### Pattern: Test Btrfs Snapshot Creation

```python
@pytest.mark.integration
async def test_001_us3_as2_create_presync_snapshots(
    pc1_executor: RemoteExecutor,
) -> None:
    """US-3 AS-2: Create pre-sync snapshots before jobs execute."""
    snapshot_path = "/.snapshots/pc-switcher/test-session/pre-@-20251211T120000"

    try:
        # Create snapshot via orchestrator
        orchestrator = Orchestrator(...)
        await orchestrator.create_presync_snapshots()

        # Verify snapshot exists and is read-only
        verify = await pc1_executor.run_command(f"sudo btrfs property get {snapshot_path} ro")
        assert verify.success
        assert "ro=true" in verify.stdout

    finally:
        # Cleanup
        await pc1_executor.run_command(f"sudo btrfs subvolume delete {snapshot_path}", timeout=10.0)
```

### Pattern: Test Exception Handling

```python
@pytest.mark.asyncio
async def test_001_fr019_critical_on_exception() -> None:
    """FR-019: Log CRITICAL and halt when job raises exception."""
    mock_job = AsyncMock(spec=SyncJob)
    mock_job.validate = AsyncMock(return_value=[])
    mock_job.execute = AsyncMock(side_effect=RuntimeError("Test error"))

    orchestrator = Orchestrator(...)

    with pytest.raises(RuntimeError):
        await orchestrator.run_jobs([mock_job])

    # Verify CRITICAL log was emitted
    # (Check event_bus.publish was called with LogEvent at CRITICAL level)
```

## Debugging Tests

### Run Single Test with Verbose Output

```bash
uv run pytest tests/unit/orchestrator/test_logging_system.py::test_001_fr018_log_level_ordering -v -s
```

The `-s` flag shows print statements and logging output.

### Run with Debugger

```bash
uv run pytest tests/unit/test_something.py::test_name --pdb
```

This drops into pdb debugger on failure.

### Check Integration Test VM State

```bash
# SSH into test VM manually (if VMs are running)
ssh pc1  # Requires SSH config from testing framework

# Check btrfs state
sudo btrfs subvolume list /

# Check logs
tail -f ~/.local/share/pc-switcher/logs/*.log
```

### View Integration Test Logs

Integration tests write to VM logs. After test failure:

```bash
# Check pc-switcher logs on VM
uv run pytest tests/integration/test_something.py -v -s  # -s shows stdout

# Or SSH to VM and check logs
ssh pc1
tail -f ~/.local/share/pc-switcher/logs/sync-*.log
```

## Test Performance

### Measure Test Execution Time

```bash
# Show duration of each test
uv run pytest tests/unit -v --durations=10
```

### Ensure Unit Tests Meet <30s Budget

```bash
# Time the full unit test suite
time uv run pytest tests/unit tests/contract -v

# Should complete in <30 seconds per FR-013 of 003-foundation-tests spec
```

### Profile Slow Tests

```bash
# Show slowest 20 tests
uv run pytest tests/unit -v --durations=20
```

If unit tests exceed 30s budget, consider:
- Moving slow tests to integration suite
- Using faster mocks instead of real objects
- Simplifying test setup

## Safety and Risk Assessment

### High-Risk Tests (Require VM Isolation)

**CRITICAL**: These tests must ONLY run on dedicated test VMs, never on development machines.

| Test | Risk | Potential Damage | Mitigation |
|------|------|------------------|------------|
| `test_snapshot_infrastructure.py` | **HIGH** | Could corrupt btrfs filesystem if snapshot commands fail unexpectedly | Run only on dedicated test VMs; reset VMs after each session |
| `test_cleanup_snapshots.py` | **HIGH** | Deletes btrfs subvolumes; could delete wrong snapshots if path logic is buggy | Use unique test session IDs; verify paths before deletion; test only on VMs |
| `test_installation_script.py` | **MEDIUM** | Installs packages via apt; could leave system in inconsistent state | Run only on VMs; reset after tests |
| `test_interrupt_integration.py` | **MEDIUM** | Spawns processes that might not terminate; could leave orphaned processes | Hard timeout fallback; process cleanup in fixtures; VM reset |
| `test_end_to_end_sync.py` | **MEDIUM** | Runs full sync workflow including snapshots | VM isolation; test session cleanup |
| Integration `test_lock.py` | **LOW** | Creates lock files that could interfere with real syncs | Use test-specific lock paths; cleanup fixtures |

### Safety Measures

The testing framework implements multiple layers of protection:

1. **VM Isolation**: All destructive tests run only on dedicated Hetzner Cloud VMs, never on developer machines or CI runners.

2. **Session ID Isolation**: Each test run uses a unique session ID (`test-<random>`) to prevent conflicts with real data.

3. **Cleanup Fixtures**: pytest fixtures clean up test artifacts. Note: Per-test cleanup is optional for many tests since the VM is reset to a clean btrfs snapshot before each test session.

4. **Lock-Based Exclusion**: Integration test lock prevents concurrent runs that could interfere with each other.

5. **Snapshot Reset**: VMs are reset to baseline snapshot before each test session.

6. **Timeout Protection**: Long-running tests have timeouts to prevent hangs.

7. **CI Concurrency Control**: GitHub Actions `concurrency.group` prevents parallel integration test runs.

### Test Execution Guidelines

**DO**:
- Run integration tests only on dedicated test VMs
- Review test output for unexpected errors or warnings
- Monitor VM state after test runs for leftover artifacts
- Reset VMs periodically even if tests pass

**DON'T**:
- Never run integration tests on production machines
- Never run integration tests on your development laptop
- Never skip VM reset between test sessions
- Never use test VMs for real pc-switcher usage

## Test Limitations and Assumptions

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

### Assumptions

Integration tests assume:

1. **Python 3.14+**: Tests assume Python 3.14 or later is available (handled automatically by `uv run`).

2. **btrfs filesystem**: Integration tests assume VMs have btrfs root filesystem with `@` and `@home` subvolumes. *Automatically configured by OpenTofu provisioning scripts.*

3. **SSH access**: Integration tests assume SSH key-based authentication to VMs is configured. *Automatically configured by OpenTofu - SSH keys are deployed during VM creation, and `~/.ssh/config` is set up with pc1/pc2 hostnames.*

4. **sudo access**: Tests assume `testuser` has passwordless sudo on VMs. *Automatically configured by cloud-init during VM provisioning.*

5. **Network access**: Tests assume VMs can reach GitHub for install.sh tests. *Hetzner VMs have public IPs with unrestricted outbound access by default.*

6. **No concurrent modifications**: Tests assume no other processes are modifying the test VMs during test execution. Enforced by lock-based isolation.

## Next Steps

After writing tests:

1. **Verify coverage**: Check `contracts/coverage-map.yaml` includes your tests
2. **Run locally**: Ensure tests pass on your machine
3. **Update data-model.md**: If adding new requirements, update the mapping
4. **Submit PR**: CI will run all tests automatically

For detailed requirement-to-test mapping, see `specs/003-foundation-tests/data-model.md`.
