# Testing Guide for AI Agents

Instructions for writing tests in pc-switcher. Target audience: AI agents implementing features or fixing bugs.

**For deeper understanding**, see:
- [testing-architecture.md](../ops/testing-architecture.md) - Architecture overview
- [testing-ops.md](../ops/testing-ops.md) - Operational procedures and troubleshooting
- [ci-setup.md](../ops/ci-setup.md) - Which workflow runs which tests, and when

## Test Tiers: When to Use Each

| Tier | Use When | Location |
| ---- | -------- | -------- |
| **Unit Tests** | Testing business logic, validation, configuration parsing, models, utilities. Use mocked executors. | `tests/unit/` |
| **Contract Tests** | Verifying jobs, executors and logging implement their required interfaces. | `tests/contract/` |
| **Integration Tests** | Testing real SSH, btrfs operations, full workflows. Requires VMs. | `tests/integration/` |
| **Real-rsync Tests** | Asserting what an rsync filter rule actually transfers. Local `rsync` binary, no VM. | `tests/local_rsync/` |
| **Manual Playbook** | Adding/changing TUI elements, progress bars, colors, visual feedback. Document in playbook. | `tests/manual-playbook.md` |

**Rule of thumb**: Most requirements need BOTH unit tests (fast, mocked) AND integration tests (real VMs). Unit tests verify logic; integration tests verify real-world behavior.

## Test Naming Conventions

The test **name** states the behavior, in plain snake_case. Requirement IDs go in the **docstring**, never in the name.

```python
def test_acquire_creates_lock_file(self, tmp_path: Path) -> None:
    """acquire() should create the lock file."""

async def test_install_missing_pcswitcher(self) -> None:
    """CORE-US-SELF-INSTALL-AS1: Target missing pc-switcher, orchestrator installs from GitHub."""
```

Requirement IDs come from the living specs in `docs/system/` (`CORE-FR-*`, `LOG-US-*`, `TST-FR-*`, …). Cite one whenever the test exists to pin a stated requirement.

## Writing Unit Tests

### Directory Structure

```text
tests/unit/
├── test_lock.py, test_logging.py, test_version.py, ...   # cross-cutting modules
├── cli/                         # CLI command tests
├── executor/                    # Executor behavior tests (mocked)
├── jobs/                        # One module per sync job
│   └── apt/                     # One module per src/pcswitcher/jobs/apt_sync/ module,
│                                # plus helpers.py for shared apt test builders
├── orchestrator/                # Config, job lifecycle, session, interrupt handling
└── ui/                          # Terminal UI tests (mocked)
```

Mirror the source layout: a test module for `src/pcswitcher/jobs/apt_sync/keyrings.py` belongs at `tests/unit/jobs/apt/test_apt_keyrings.py`. Put a new job's tests in `tests/unit/jobs/`; split into a package only once one file covers several source modules.

### Available Fixtures

From `tests/conftest.py` (all tests):

```python
mock_connection       # Mock asyncssh connection
mock_executor         # Mock executor with run_command() and start_process()
mock_remote_executor  # Mock remote executor with file transfer methods
mock_event_bus        # Mock EventBus for event publishing
sample_command_result # Sample successful CommandResult
failed_command_result # Sample failed CommandResult
```

From `tests/unit/conftest.py` (unit tests only):

```python
mock_local_executor        # Mock LocalExecutor
mock_job_context           # Ready-made JobContext wired to the mock executors
mock_job_context_factory   # Same, but takes config=... and dry_run=...
wired_orchestrator         # Orchestrator with enough wiring for the real job loop to run
frozen_time, frozen_datetime  # Deterministic timestamps (2025-01-15T10:30:00Z)
success_result, failed_result # Bare CommandResults
```

Use `mock_job_context` rather than building a `JobContext` by hand.

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

**JobContext for job tests**: take `mock_job_context`, or `mock_job_context_factory` when the job reads its config:

```python
def test_job_reads_its_config(
    mock_job_context_factory: Callable[..., JobContext]
) -> None:
    context = mock_job_context_factory(config={"key": "value"})
```

`JobContext` (`src/pcswitcher/jobs/context.py`) carries several optional collaborators — `confirmer`, `reviewer`, `target_username`, `enabled_sync_jobs` — that default to `None` precisely so lightweight test contexts can omit them. A job that needs one asserts it is set rather than silently doing nothing.

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

- VMs are reset to baseline by `tests/run-integration-tests.sh` **before pytest starts**, once per run — not by a fixture, and not between tests
- Baseline does NOT include pc-switcher - tests must install it if needed
- Tests share SSH connections within a module (module-scoped fixtures)
- **All tests MUST clean up after themselves**
- Launch with `./tests/run-integration-tests.sh`; running pytest directly skips the lock, the readiness check and the reset

### Available Fixtures

From `tests/integration/conftest.py`:

```python
pc1_executor, pc2_executor  # BashLoginRemoteExecutor per VM (scope=module)
pc1_with_pcswitcher_mod     # pc1 with pc-switcher at the current branch tip (scope=module)
pc2_with_pcswitcher         # pc2 at the branch tip, for back-sync tests
pc2_without_pcswitcher_fn   # pc2 with pc-switcher and its config removed
pc2_with_old_pcswitcher_fn  # pc2 with the previous release installed
reset_pcswitcher_state      # Wipes config, data and snapshots on both VMs, before and after
vm_test_fixtures            # Both VMs carry the current package-manager subjects (scope=module)
```

The SSH connections themselves (`_pc1_connection`, `_pc2_connection`) are private; go through the executors.

Anything named `*_pcswitcher*` mutates a VM. A test using one MUST NOT also use `pc2_executor` directly — both drive the same machine.

Install fixtures build from the **current branch as pushed to origin**. Push before running, or they install stale code.

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

`tests/contract/` holds three modules: `test_job_interface.py`, `test_executor_contract.py` (mock and real executors behave alike) and `test_logging_contract.py`. Job contract tests look like this:

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

## Integration Test Structure

```text
tests/integration/
├── conftest.py                  # VM fixtures, marker enforcement, live failure reporting
├── test_vm_connectivity.py      # VM infrastructure validation
├── test_end_to_end_sync.py      # Full sync workflow
├── test_sync_order_gates.py     # First-sync and consecutive-push gates (ADR-015)
├── test_dry_run.py              # The --dry-run no-write contract (ADR-014)
├── test_btrfs_operations.py, test_snapshot_infrastructure.py
├── test_config_sync.py, test_init_command.py, test_logging_integration.py
├── test_interrupt_integration.py, test_lock_integration.py
├── test_installation_script.py, test_self_update.py, test_version_resolution.py
├── jobs/                        # Per-job integration tests
│   ├── test_install_on_target_job.py
│   └── test_package_sync.py
├── benchmarks/                  # Performance benchmarks (excluded from CI)
└── scripts/                     # VM provisioning, reset, lock, CI selection
```

## Markers

Registered in `[tool.pytest]` in `pyproject.toml`; `--strict-markers` rejects anything not listed there.

```python
@pytest.mark.integration   # Requires VM infrastructure (auto-applied in tests/integration/)
@pytest.mark.local_rsync   # Needs a local rsync binary, no VM (tests/local_rsync/)
@pytest.mark.benchmark     # Performance benchmarks (in benchmarks/ folder, not run by default)
@pytest.mark.ci_skip       # Exception hatch: excluded from ALL CI runs (topic and full); local runs still execute it
@pytest.mark.smoke         # CI selection: fast sanity, part of every CI integration selection
@pytest.mark.area_package  # CI selection: package-manager sync tests
@pytest.mark.area_install  # CI selection: install / self-update tests
@pytest.mark.area_btrfs    # CI selection: btrfs snapshot tests
@pytest.mark.area_folder   # CI selection: folder-sync end-to-end tests
@pytest.mark.area_core     # CI selection: core sync spine (locking, sync history, logging, init, interrupts)
```

Every integration test MUST carry at least one CI-selection marker (`smoke` or an `area_*`), normally as a module-level `pytestmark` — collection fails otherwise (enforced in `tests/integration/conftest.py`). `ci_skip` is additive to the area marker, not a replacement.

A test that genuinely exercises two areas may carry both, and then runs whenever either is selected — CI selects with an `or` expression, so extra markers only widen when a test runs. Markers compose from all three levels, so a single class inside a module can join a second area:

```python
pytestmark = [pytest.mark.area_folder, pytest.mark.area_install]   # whole module in both areas

class TestInstallOnTarget:
    pytestmark = pytest.mark.area_install                          # this class only, on top of the module's
```

Use it for tests that really span areas, not to broaden coverage by default: every added marker costs VM minutes on every PR that touches the other area.

## CI test selection (topic-based)

On ordinary PR pushes, CI runs only the integration tests for the areas the PR touches, plus the `smoke` set, via a pytest `-m` expression (e.g. `integration and not benchmark and (smoke or area_package)`). `tests/integration/scripts/select-ci-tests.sh` builds the expression: product source files map to areas through its case patterns, and a changed test file contributes every area its own markers name. Any changed file outside the mapped areas selects the full suite — the mapping errs toward running too much, never too little.

Every PR run — including `ready_for_review` — is topic-scoped; the full suite runs on: the `ci: full` PR label (while present, every run is full; adding it triggers a run immediately), the nightly schedule on main, and manual `workflow_dispatch` (`gh workflow run "Integration Tests" --ref <branch>`). Pre-merge gating is opt-in: add `ci: full` as the last step before merging and let the run go green — a red run blocks the merge like any failing required check. When adding a new source module — or a helper module under `tests/integration/` that carries no markers of its own — map it in `select-ci-tests.sh`; new test files only need their marker.

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
# Unit and contract tests (fast, no VMs) — what CI runs
uv run pytest tests/unit tests/contract

# Real-rsync tests (needs a local rsync binary, no VM)
uv run pytest tests/local_rsync

# Integration tests — takes the lock and resets both VMs first
./tests/run-integration-tests.sh

# Integration, one file, without re-resetting the VMs
./tests/run-integration-tests.sh --skip-reset tests/integration/test_vm_connectivity.py

# Print the topic-scoped CI marker expression for the current branch
tests/integration/scripts/select-ci-tests.sh origin/main

# Specific test
uv run pytest tests/unit/orchestrator/test_config_system.py -k test_load
```

`-v` is already in `addopts`, so `--verbose` is redundant. There is no coverage plugin installed; `--cov` fails.

## AI Agent Checklist

When writing tests:

- [ ] Used correct tier (unit vs integration) for what you're testing
- [ ] Test name states the behavior; requirement ID, if any, is in the docstring
- [ ] Unit tests use mocked executors and the shared `mock_job_context`, not real SSH
- [ ] New unit test module mirrors the source layout
- [ ] Integration tests clean up all artifacts in finally block
- [ ] Integration test file carries a `smoke`/`area_*` `pytestmark` (more than one only if it truly spans areas)
- [ ] Used unique names for test artifacts
- [ ] Grouped commands when making >3 sequential SSH calls
- [ ] Verified tests pass with `uv run pytest tests/unit tests/contract`
