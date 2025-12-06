# Testing Developer Guide

This guide explains how to write tests for pc-switcher. Read this before writing your first test.

## Table of Contents

1. [Overview](#overview)
2. [Writing Unit Tests](#writing-unit-tests)
3. [Writing Contract Tests](#writing-contract-tests)
4. [Writing Integration Tests](#writing-integration-tests)
5. [VM Interaction Patterns](#vm-interaction-patterns)
6. [Test Organization](#test-organization)
7. [Local Development Setup](#local-development-setup)
8. [Troubleshooting](#troubleshooting)

## Overview

### Three-Tier Test Structure

PC-switcher uses a three-tier testing approach:

| Tier | Purpose | Speed | Environment | When to Run |
|------|---------|-------|-------------|-------------|
| **Unit Tests** | Test pure logic, business rules, mocked I/O | Fast (< 30s) | Any machine | Every commit |
| **Contract Tests** | Verify job interface compliance | Fast (< 30s) | Any machine | Every commit |
| **Integration Tests** | Test real SSH, btrfs operations, full workflows | Slow (5-15 min) | Dedicated VMs only | PRs to main, on-demand |

### When to Use Each Tier

**Unit Tests:**
- Testing business logic in jobs
- Testing validation logic
- Testing configuration parsing
- Testing lock mechanisms
- Testing models and utilities

**Contract Tests:**
- Verifying jobs implement required interfaces
- Ensuring mock/real executor behavior parity
- Testing job base class functionality

**Integration Tests:**
- Testing SSH connections between machines
- Testing btrfs snapshot operations
- Testing full sync workflows
- Testing inter-VM communication
- Testing install scripts

## Writing Unit Tests

### Directory Structure

Place unit tests in `tests/unit/`:

```text
tests/unit/
├── test_lock.py
├── test_config.py
├── test_jobs/
│   ├── test_disk_space_monitor.py
│   └── test_btrfs.py
```

### Available Fixtures

These fixtures are defined in `tests/conftest.py` and available to all tests:

```python
@pytest.fixture
def mock_connection() -> MagicMock:
    """Mock asyncssh connection for SSH operations."""

@pytest.fixture
def mock_executor() -> MagicMock:
    """Mock executor with run_command() and start_process()."""

@pytest.fixture
def mock_remote_executor(mock_executor: MagicMock) -> MagicMock:
    """Mock remote executor with file transfer methods."""

@pytest.fixture
def mock_event_bus() -> MagicMock:
    """Mock EventBus for event publishing."""

@pytest.fixture
def sample_command_result() -> CommandResult:
    """Sample successful CommandResult."""

@pytest.fixture
def failed_command_result() -> CommandResult:
    """Sample failed CommandResult."""
```

### Mocking Patterns for Executors

#### Mocking run_command()

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

# Different responses for different commands
async def mock_run_command(cmd: str) -> CommandResult:
    if "test -d" in cmd:
        return CommandResult(exit_code=0, stdout="", stderr="")
    return CommandResult(exit_code=1, stdout="", stderr="not found")

mock_executor.run_command = AsyncMock(side_effect=mock_run_command)
```

#### Creating a JobContext for Testing

```python
from pcswitcher.jobs import JobContext
from pcswitcher.models import Host

@pytest.fixture
def mock_job_context() -> JobContext:
    """Create a mock JobContext for job testing."""
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

### Example Unit Test

```python
"""Unit tests for DiskSpaceMonitorJob."""

from pcswitcher.jobs.disk_space_monitor import DiskSpaceMonitorJob
from pcswitcher.models import Host

class TestDiskSpaceMonitorValidation:
    """Test validate() method for system state validation."""

    @pytest.mark.asyncio
    async def test_validate_checks_mount_point_exists(
        self, mock_job_context: JobContext
    ) -> None:
        """validate() should check that mount point exists."""
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")

        # Mock executor already returns successful command result
        errors = await job.validate()

        # No errors when mount point check succeeds
        assert errors == []

        # Verify test -d command was called
        mock_job_context.source.run_command.assert_called_once_with("test -d /")
```

## Writing Contract Tests

### Purpose

Contract tests verify that all jobs comply with the job interface contract, ensuring consistency across the codebase.

### Directory Structure

Place contract tests in `tests/contract/`:

```text
tests/contract/
└── test_job_interface.py
```

### What to Test

Contract tests should verify:

1. Jobs have required class attributes (`name`, `CONFIG_SCHEMA`)
2. `validate_config()` returns empty list for valid configs
3. `validate_config()` returns `ConfigError` list for invalid configs
4. `validate()` returns list of `ValidationError`
5. `execute()` completes without error
6. Helper methods (`_log()`, `_report_progress()`) work correctly

### Example Contract Test

```python
"""Contract tests verifying job interface compliance."""

from pcswitcher.jobs import SyncJob, JobContext
from pcswitcher.models import ConfigError, ValidationError

class ExampleTestJob(SyncJob):
    """A minimal job implementation for testing the contract."""

    name: ClassVar[str] = "example_test"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "test_value": {"type": "string"},
        },
    }

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        self._log(Host.SOURCE, LogLevel.INFO, "Test execution")
        self._report_progress(ProgressUpdate(percent=100))

class TestJobContract:
    """Test that jobs follow the interface contract."""

    def test_job_has_name_attribute(self) -> None:
        """Jobs must have a name class attribute."""
        assert hasattr(ExampleTestJob, "name")
        assert ExampleTestJob.name == "example_test"

    def test_validate_config_returns_errors_for_invalid_config(self) -> None:
        """validate_config should return ConfigError list for invalid config."""
        errors = InvalidSchemaJob.validate_config({})
        assert len(errors) == 1
        assert isinstance(errors[0], ConfigError)
```

## Writing Integration Tests

Integration tests run on dedicated test VMs with real btrfs filesystems and SSH connections.

### Directory Structure

```text
tests/integration/
├── conftest.py
└── test_ssh_connection.py
```

### Marking Integration Tests

All integration tests MUST be marked with `@pytest.mark.integration`:

```python
import pytest

@pytest.mark.integration
async def test_ssh_connection(pc1_executor):
    """Test SSH connection to pc1."""
    result = await pc1_executor.run_command("echo hello")
    assert result.success
    assert "hello" in result.stdout
```

### Available Fixtures

Integration test fixtures are defined in `tests/integration/conftest.py`:

```python
@pytest.fixture
async def pc1_connection():
    """Async SSH connection to pc1 VM."""

@pytest.fixture
async def pc2_connection():
    """Async SSH connection to pc2 VM."""

@pytest.fixture
async def pc1_executor(pc1_connection):
    """RemoteExecutor for pc1 VM."""

@pytest.fixture
async def pc2_executor(pc2_connection):
    """RemoteExecutor for pc2 VM."""
```

### Environment Variables Required

Integration tests require these environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `PC_SWITCHER_TEST_PC1_HOST` | PC1 VM hostname/IP | `192.0.2.1` |
| `PC_SWITCHER_TEST_PC2_HOST` | PC2 VM hostname/IP | `192.0.2.2` |
| `PC_SWITCHER_TEST_USER` | SSH user on VMs | `root` |

If these variables are not set, integration tests are automatically skipped with a clear message.

## VM Interaction Patterns

### SSH Command Execution

Execute commands via `RemoteExecutor`:

```python
@pytest.mark.integration
async def test_command_execution(pc1_executor):
    """Test running a command on pc1."""
    result = await pc1_executor.run_command("uname -n")
    assert result.success
    assert result.exit_code == 0
    print(f"Hostname: {result.stdout.strip()}")
```

### File Transfer with asyncssh

Transfer files between local and remote machines:

```python
@pytest.mark.integration
async def test_file_upload(pc1_connection):
    """Test uploading a file to pc1."""
    async with pc1_connection.start_sftp_client() as sftp:
        await sftp.put("/local/path/file.txt", "/remote/path/file.txt")

        # Verify file exists
        stat = await sftp.stat("/remote/path/file.txt")
        assert stat is not None

@pytest.mark.integration
async def test_file_download(pc1_connection):
    """Test downloading a file from pc1."""
    async with pc1_connection.start_sftp_client() as sftp:
        await sftp.get("/remote/path/file.txt", "/local/path/file.txt")
```

### Btrfs Snapshot Operations

Create and manage btrfs snapshots:

```python
@pytest.mark.integration
async def test_create_snapshot(pc1_executor):
    """Test creating a btrfs snapshot."""
    # Create a read-only snapshot
    result = await pc1_executor.run_command(
        "sudo btrfs subvolume snapshot -r /mnt/btrfs/@ /mnt/btrfs/.snapshots/test-snapshot"
    )
    assert result.success

    # Verify snapshot exists
    result = await pc1_executor.run_command(
        "sudo btrfs subvolume show /mnt/btrfs/.snapshots/test-snapshot"
    )
    assert result.success

@pytest.mark.integration
async def test_delete_snapshot(pc1_executor):
    """Test deleting a btrfs snapshot."""
    # Delete snapshot
    result = await pc1_executor.run_command(
        "sudo btrfs subvolume delete /mnt/btrfs/.snapshots/test-snapshot"
    )
    assert result.success
```

### Inter-VM Communication

Test communication from pc1 to pc2:

```python
@pytest.mark.integration
async def test_pc1_to_pc2_ssh(pc1_executor, pc2_executor):
    """Test SSH from pc1 to pc2."""
    # Get pc2 hostname
    pc2_result = await pc2_executor.run_command("hostname")
    pc2_hostname = pc2_result.stdout.strip()

    # SSH from pc1 to pc2
    result = await pc1_executor.run_command(
        f"ssh -o StrictHostKeyChecking=no root@{pc2_hostname} 'echo test'"
    )
    assert result.success
    assert "test" in result.stdout

@pytest.mark.integration
async def test_rsync_between_vms(pc1_executor, pc2_executor):
    """Test rsync from pc1 to pc2."""
    # Create test file on pc1
    await pc1_executor.run_command("echo 'test data' > /tmp/test.txt")

    # Get pc2 IP
    pc2_result = await pc2_executor.run_command("hostname -I | awk '{print $1}'")
    pc2_ip = pc2_result.stdout.strip()

    # Rsync from pc1 to pc2
    result = await pc1_executor.run_command(
        f"rsync -av /tmp/test.txt root@{pc2_ip}:/tmp/test.txt"
    )
    assert result.success

    # Verify file on pc2
    result = await pc2_executor.run_command("cat /tmp/test.txt")
    assert "test data" in result.stdout
```

## Test Organization

### File Naming

All test files must start with `test_`:

```text
tests/unit/test_lock.py          ✓ Correct
tests/unit/test_jobs/test_disk_space_monitor.py  ✓ Correct
tests/integration/test_ssh.py    ✓ Correct

tests/unit/lock_test.py          ✗ Wrong
tests/unit/mytest.py             ✗ Wrong
```

### Test Class Naming

Group related tests in classes with descriptive names:

```python
class TestLockPath:
    """Tests for lock path utilities."""

class TestSyncLock:
    """Tests for SyncLock class."""

class TestDiskSpaceMonitorValidation:
    """Test validate() method for system state validation."""
```

### Test Function Naming

Use descriptive names that explain what is being tested:

```python
def test_acquire_creates_lock_file(self, tmp_path: Path) -> None:
    """acquire() should create the lock file."""

def test_validate_config_rejects_invalid_format(self) -> None:
    """validate_config() should reject invalid format."""
```

### Markers

Use pytest markers to categorize tests:

```python
@pytest.mark.integration  # Requires VM infrastructure
@pytest.mark.slow         # Takes >5 seconds
```

Markers are configured in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "integration: Integration tests (require VM infrastructure)",
    "slow: Tests that take >5 seconds",
]
```

### Fixture Scopes

Choose appropriate fixture scopes:

| Scope | When to Use | Example |
|-------|-------------|---------|
| `function` (default) | Most tests | `mock_executor` |
| `class` | Shared setup for test class | Database connection |
| `module` | Expensive setup shared across file | - |
| `session` | VM connections, one-time setup | `pc1_connection` |

Example:

```python
@pytest.fixture(scope="session")
async def pc1_connection():
    """SSH connection shared across all integration tests."""
    # ... connection setup
```

## Local Development Setup

### Prerequisites

Install required tools:

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install hcloud CLI (for VM management)
# On Ubuntu/Debian:
sudo apt-get install hcloud

# On macOS:
brew install hcloud
```

### Environment Variables

Set up environment variables for local integration testing. Create a file `~/.pc-switcher-test-env`:

```bash
# Hetzner Cloud API token (from https://console.hetzner.cloud/)
export HCLOUD_TOKEN="your-token-here"

# Test VM hostnames/IPs (after provisioning)
export PC_SWITCHER_TEST_PC1_HOST="192.0.2.1"
export PC_SWITCHER_TEST_PC2_HOST="192.0.2.2"
export PC_SWITCHER_TEST_USER="root"

# SSH key (optional, defaults to ~/.ssh/id_ed25519.pub)
export SSH_PUBLIC_KEY="$HOME/.ssh/id_ed25519.pub"
```

Source this file before running integration tests:

```bash
source ~/.pc-switcher-test-env
```

### Provisioning Local Test VMs

Provision test VMs once (they persist between test runs):

```bash
cd tests/infrastructure

# Provision VMs (creates pc-switcher-pc1 and pc-switcher-pc2)
./scripts/provision-test-infra.sh
```

This script:
1. Creates two VMs in Hetzner Cloud
2. Installs Ubuntu 24.04 with btrfs filesystem
3. Sets up SSH access
4. Configures inter-VM networking
5. Creates baseline snapshots

The VMs remain running and are reset using btrfs snapshot rollback before each test run.

### Running Tests Locally

Run unit tests (no VM required):

```bash
uv run pytest tests/unit tests/contract -v
```

Run integration tests (requires VMs):

```bash
# Make sure environment variables are set
source ~/.pc-switcher-test-env

# Run integration tests
uv run pytest tests/integration -v -m integration
```

Run all tests:

```bash
uv run pytest -v
```

### Resetting VMs to Baseline

Before running integration tests, reset VMs to clean state:

```bash
cd tests/infrastructure

# Reset both VMs (requires HCLOUD_TOKEN for hcloud CLI)
./scripts/reset-vm.sh $PC_SWITCHER_TEST_PC1_HOST
./scripts/reset-vm.sh $PC_SWITCHER_TEST_PC2_HOST
```

The `reset-vm.sh` script uses btrfs snapshot rollback and takes about 30 seconds per VM.

## Troubleshooting

### "VM environment not configured" errors

**Symptom:** Integration tests are skipped with message about missing environment variables.

**Cause:** Required environment variables are not set.

**Solution:**

```bash
# Check if variables are set
echo $PC_SWITCHER_TEST_PC1_HOST
echo $PC_SWITCHER_TEST_PC2_HOST

# Set them if missing
export PC_SWITCHER_TEST_PC1_HOST="192.0.2.1"
export PC_SWITCHER_TEST_PC2_HOST="192.0.2.2"
export PC_SWITCHER_TEST_USER="root"

# Or source your env file
source ~/.pc-switcher-test-env
```

### SSH connection failures

**Symptom:** Tests fail with "Connection refused" or "Host key verification failed".

**Cause:** VMs are not running, IP addresses changed, or SSH keys are not configured.

**Solution:**

1. Check VMs are running:
   ```bash
   hcloud server list
   ```

2. Get current IP addresses:
   ```bash
   hcloud server ip pc-switcher-pc1
   hcloud server ip pc-switcher-pc2
   ```

3. Update environment variables with correct IPs.

4. Verify SSH access manually:
   ```bash
   ssh root@$PC_SWITCHER_TEST_PC1_HOST
   ```

5. If host key verification fails, remove old keys:
   ```bash
   ssh-keygen -R $PC_SWITCHER_TEST_PC1_HOST
   ssh-keygen -R $PC_SWITCHER_TEST_PC2_HOST
   ```

### Lock acquisition timeouts

**Symptom:** Integration tests fail with "Failed to acquire test lock".

**Cause:** Another test run is in progress or a previous run didn't clean up the lock.

**Solution:**

1. Check lock status (requires `HCLOUD_TOKEN`):
   ```bash
   tests/infrastructure/scripts/lock.sh "" status
   ```

2. If lock is stale (holder is no longer running), manually remove it:
   ```bash
   # Remove lock labels from the server
   hcloud server remove-label pc-switcher-pc1 lock_holder
   hcloud server remove-label pc-switcher-pc1 lock_acquired
   ```

3. Wait for other test runs to complete (check CI pipeline).

### Baseline snapshot issues

**Symptom:** Tests fail with "Baseline snapshot not found" or btrfs errors.

**Cause:** Baseline snapshots were not created or were deleted.

**Solution:**

1. Check if baseline snapshots exist:
   ```bash
   ssh testuser@$PC_SWITCHER_TEST_PC1_HOST "sudo btrfs subvolume show /.snapshots/baseline/@"
   ssh testuser@$PC_SWITCHER_TEST_PC1_HOST "sudo btrfs subvolume show /.snapshots/baseline/@home"
   ```

2. Recreate baseline snapshots by re-provisioning the VMs:
   ```bash
   cd tests/infrastructure
   ./scripts/provision-test-infra.sh
   ```

   This will recreate the baseline snapshots at `/.snapshots/baseline/` on both VMs.

### Tests pass locally but fail in CI

**Cause:** Environment differences between local and CI.

**Common issues:**
- Different Python versions
- Different dependency versions
- Timing issues (CI may be slower)
- Environment variables not set in CI

**Solution:**

1. Check CI uses same Python version:
   ```yaml
   # In .github/workflows/test.yml
   - uses: astral-sh/setup-uv@v1
     with:
       python-version: "3.14"
   ```

2. Ensure CI has required secrets configured:
   - `HCLOUD_TOKEN`
   - `HETZNER_SSH_PRIVATE_KEY`

3. Add timeouts to flaky tests:
   ```python
   @pytest.mark.asyncio
   async def test_with_timeout(pc1_executor):
       result = await pc1_executor.run_command("sleep 1", timeout=5.0)
   ```

### Btrfs "Device or resource busy" errors

**Symptom:** Cannot delete or snapshot subvolumes during tests.

**Cause:** Subvolume is currently mounted or has active processes.

**Solution:**

1. Stop all processes accessing the subvolume:
   ```bash
   ssh root@$PC_SWITCHER_TEST_PC1_HOST "lsof +D /mnt/btrfs/@"
   ```

2. Unmount if necessary:
   ```bash
   ssh root@$PC_SWITCHER_TEST_PC1_HOST "sudo umount /mnt/btrfs/@"
   ```

3. Reset VM to clean state:
   ```bash
   cd tests/infrastructure
   ./scripts/reset-vm.sh $PC_SWITCHER_TEST_PC1_HOST
   ```

### "No module named 'pcswitcher'" errors

**Symptom:** Import errors when running tests.

**Cause:** Package not installed in editable mode or wrong Python environment.

**Solution:**

1. Always use `uv run` to execute tests:
   ```bash
   uv run pytest tests/unit -v
   ```

2. DO NOT use system Python directly:
   ```bash
   python -m pytest  # ✗ Wrong
   pytest            # ✗ Wrong
   uv run pytest     # ✓ Correct
   ```

3. If still failing, reinstall dependencies:
   ```bash
   uv sync
   ```

### Assertion errors with mock objects

**Symptom:** `AssertionError` when checking mock calls.

**Cause:** Mock was not called as expected, or called with different arguments.

**Solution:**

1. Print mock calls for debugging:
   ```python
   print(mock_executor.run_command.call_args_list)
   ```

2. Use `assert_called_with` for exact match:
   ```python
   mock_executor.run_command.assert_called_with("exact command")
   ```

3. Use `assert_any_call` if order doesn't matter:
   ```python
   mock_executor.run_command.assert_any_call("command1")
   mock_executor.run_command.assert_any_call("command2")
   ```

4. Check call count:
   ```python
   assert mock_executor.run_command.call_count == 2
   ```

---

## Summary

- **Unit tests**: Fast, safe, use mocks, test logic
- **Contract tests**: Verify interface compliance
- **Integration tests**: Real VMs, real btrfs, real SSH
- **Always mark integration tests** with `@pytest.mark.integration`
- **Always use `uv run pytest`** to execute tests
- **Set environment variables** before running integration tests
- **Reset VMs** to baseline before integration testing
- **Check troubleshooting section** when tests fail

For more information, see:
- [Testing Framework Overview](testing-framework.md)
- [ADR-006: Testing Framework](adr/adr-006-testing-framework.md)
