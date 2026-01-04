# Testing Guide for AI Agents

Instructions for writing tests in pc-switcher. Target audience: AI agents implementing features or fixing bugs.

**For deeper understanding**, see:
- [testing-architecture.md](../ops/testing-architecture.md) - Architecture overview
- [testing-ops.md](../ops/testing-ops.md) - Operational procedures and troubleshooting

## Test Tiers: When to Use Each

| Tier | Use When | Location |
|------|----------|----------|
| **Unit Tests** | Testing business logic, validation, configuration parsing, models, utilities. Use mocked executors. | `tests/unit/` |
| **Contract Tests** | Verifying jobs implement required interfaces and class attributes. | `tests/contract/` |
| **Integration Tests** | Testing real SSH, btrfs operations, full workflows. Requires VMs. | `tests/integration/` |
| **Manual Playbook** | Adding/changing TUI elements, progress bars, colors, visual feedback. Document in playbook. | `tests/manual-playbook.md` |

**Rule of thumb**: Most requirements need BOTH unit tests (fast, mocked) AND integration tests (real VMs). Unit tests verify logic; integration tests verify real-world behavior.

## Test Naming Conventions

### Spec-Driven Tests (SpecKit Features)

For tests validating requirements from `specs/*/spec.md`:

**Format**: `test_<feature>_<req-id>_<description>()`

```python
def test_001_fr028_load_from_config_path() -> None:
    """CORE-FR-CONFIG-LOAD: System MUST load configuration from ~/.config/pc-switcher/config.yaml."""

async def test_001_us2_as1_install_missing_pcswitcher() -> None:
    """CORE-US-SELF-INSTALL-AS1: Target missing pc-switcher, orchestrator installs from GitHub."""
```

**Components**:
- `<feature>`: Feature number (e.g., `001` for core)
- `<req-id>`: `fr###` for functional requirements, `us#_as#` for acceptance scenarios
- `<description>`: Snake_case description

### General Tests (Non-SpecKit Code)

For helper functions, edge cases, infrastructure tests:

```python
def test_acquire_creates_lock_file(self, tmp_path: Path) -> None:
    """acquire() should create the lock file."""

def test_validate_config_rejects_invalid_format(self) -> None:
    """validate_config() should reject invalid format."""
```

## Writing Unit Tests

### Directory Structure

```text
tests/unit/
├── test_lock.py
├── test_config.py
├── test_jobs/
│   ├── test_disk_space_monitor.py
│   └── test_btrfs.py
```

### Available Fixtures

From `tests/conftest.py`:

```python
mock_connection      # Mock asyncssh connection
mock_executor        # Mock executor with run_command() and start_process()
mock_remote_executor # Mock remote executor with file transfer methods
mock_event_bus       # Mock EventBus for event publishing
sample_command_result # Sample successful CommandResult
failed_command_result # Sample failed CommandResult
```

### Mocking Patterns

**Basic command mocking**:

```python
from unittest.mock import AsyncMock
from pcswitcher.models import CommandResult

# Success case
mock_executor.run_command = AsyncMock(
    return_value=CommandResult(exit_code=0, stdout="output", stderr="")
)

# Failure case
mock_executor.run_command = AsyncMock(
    return_value=CommandResult(exit_code=1, stdout="", stderr="error")
)

# Different responses per command
async def mock_run_command(cmd: str) -> CommandResult:
    if "test -d" in cmd:
        return CommandResult(exit_code=0, stdout="", stderr="")
    return CommandResult(exit_code=1, stdout="", stderr="not found")

mock_executor.run_command = AsyncMock(side_effect=mock_run_command)
```

**Creating JobContext for job tests**:

```python
from pcswitcher.jobs import JobContext
from pcswitcher.models import Host

@pytest.fixture
def mock_job_context() -> JobContext:
    source = MagicMock()
    source.run_command = AsyncMock(
        return_value=CommandResult(exit_code=0, stdout="", stderr="")
    )
    target = MagicMock()
    target.run_command = AsyncMock(
        return_value=CommandResult(exit_code=0, stdout="", stderr="")
    )
    return JobContext(
        config={"key": "value"},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test1234",
        source_hostname="source-host",
        target_hostname="target-host",
    )
```

### Unit Test Example

```python
class TestDiskSpaceMonitorValidation:
    """Test validate() method for system state validation."""

    @pytest.mark.asyncio
    async def test_validate_checks_mount_point_exists(
        self, mock_job_context: JobContext
    ) -> None:
        """validate() should check that mount point exists."""
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")
        errors = await job.validate()

        assert errors == []
        mock_job_context.source.run_command.assert_called_once_with("test -d /")
```

## Writing Integration Tests

### Key Facts

- VMs reset to baseline **once per pytest session** (not between tests)
- Baseline does NOT include pc-switcher - tests must install it if needed
- Tests share SSH connections within a module (module-scoped fixtures)
- **All tests MUST clean up after themselves**

### Available Fixtures

From `tests/integration/conftest.py`:

```python
pc1_connection   # Async SSH connection to pc1 (scope=module)
pc2_connection   # Async SSH connection to pc2 (scope=module)
pc1_executor     # RemoteExecutor for pc1 (scope=module)
pc2_executor     # RemoteExecutor for pc2 (scope=module)
test_volume      # Btrfs test subvolume at /test-vol (scope=module)
```

### Integration Test Marker

Tests in `tests/integration/` are **automatically** marked with `@pytest.mark.integration`. No need to add the marker manually.

### Cleanup Pattern (Required)

**Always clean up test artifacts in try/finally**:

```python
async def test_create_snapshot(pc1_executor):
    """Test creating a btrfs snapshot with proper cleanup."""
    snapshot_name = "/.snapshots/test-my-unique-snapshot"

    try:
        result = await pc1_executor.run_command(
            f"sudo btrfs subvolume snapshot -r / {snapshot_name}"
        )
        assert result.success
        # ... test assertions ...
    finally:
        # Always clean up, even if test fails
        await pc1_executor.run_command(
            f"sudo btrfs subvolume delete {snapshot_name} 2>/dev/null || true"
        )
```

### Use Unique Names

Avoid name collisions between tests:

```python
# Good: unique snapshot name per test
snapshot_name = "/.snapshots/test-readonly-snapshot"

# Bad: generic name that might conflict
snapshot_name = "/.snapshots/snapshot"
```

### Command Grouping for Performance

Each `run_command()` has ~70-80ms SSH overhead. Group commands when possible:

```python
# Bad: 30 separate calls = 2.3 seconds overhead
for file in files:
    await executor.run_command(f"stat {file}")

# Good: Single call with command chaining
result = await executor.run_command(
    " && ".join(f"stat {file}" for file in files)
)
```

**Rule**: If you need more than 3-5 sequential commands, group them into a single `run_command()` call.

## Writing Contract Tests

Contract tests verify jobs comply with the interface:

```python
class TestJobContract:
    """Test that jobs follow the interface contract."""

    def test_job_has_name_attribute(self) -> None:
        """Jobs must have a name class attribute."""
        assert hasattr(ExampleTestJob, "name")

    def test_validate_config_returns_errors_for_invalid_config(self) -> None:
        """validate_config should return ConfigError list for invalid config."""
        errors = InvalidSchemaJob.validate_config({})
        assert len(errors) == 1
        assert isinstance(errors[0], ConfigError)
```

**Contract tests verify**:
1. Jobs have required class attributes (`name`, `CONFIG_SCHEMA`)
2. `validate_config()` returns empty list for valid configs
3. `validate_config()` returns `ConfigError` list for invalid configs
4. `validate()` returns list of `ValidationError`
5. `execute()` completes without error

## Markers

```python
@pytest.mark.integration  # Requires VM infrastructure (auto-applied in tests/integration/)
@pytest.mark.slow         # Takes >5 seconds
@pytest.mark.benchmark    # Performance benchmarks (not run by default)
```

## Common Pitfalls

### Forgetting Cleanup

**Problem**: Test leaves files/snapshots on VM, causing failures in subsequent tests.

**Solution**: Always use try/finally. Clean up in the finally block even if test fails.

### Not Using `uv run`

**Problem**: `No module named 'pcswitcher'` errors.

**Solution**: Always use `uv run pytest`, never `python -m pytest` or `pytest` directly.

### Hardcoding Paths or IPs

**Problem**: Tests fail on other machines or in CI.

**Solution**: Use environment variables and fixtures. Never hardcode VM IPs.

### Mock Not Called As Expected

**Problem**: `AssertionError` when checking mock calls.

**Solution**: Debug by printing mock calls:
```python
print(mock_executor.run_command.call_args_list)
```

### Module-Scoped Fixtures with Function-Scoped Tests

**Problem**: Event loop errors, async object sharing issues.

**Solution**: Both fixtures and tests use module scope by default (configured in pyproject.toml). Don't override without good reason.

### Generic Artifact Names

**Problem**: Test `test_foo` creates `/.snapshots/snapshot`, test `test_bar` does too. They collide.

**Solution**: Use unique, descriptive names: `/.snapshots/test-foo-verify-readonly`.

## Running Tests

```bash
# Unit tests only (fast, no VMs)
uv run pytest tests/unit tests/contract -v

# Integration tests (requires VMs and env vars)
uv run pytest tests/integration -v -m "integration and not benchmark"

# Specific test
uv run pytest tests/unit/test_config.py::TestConfig::test_load_default -v

# With coverage
uv run pytest tests/unit --cov=src/pcswitcher --cov-report=html
```

## AI Agent Checklist

When writing tests:

- [ ] Used correct tier (unit vs integration) for what you're testing
- [ ] Used spec-driven naming for SpecKit features (`test_<feature>_<req>_<desc>`)
- [ ] Added docstring with requirement ID reference
- [ ] Unit tests use mocked executors, not real SSH
- [ ] Integration tests clean up all artifacts in finally block
- [ ] Used unique names for test artifacts
- [ ] Grouped commands when making >3 sequential SSH calls
- [ ] Verified tests pass with `uv run pytest`
- [ ] Updated `specs/<feature>/contracts/coverage-map.yaml` if testing a spec requirement
