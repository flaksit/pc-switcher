# Testing Patterns

**Analysis Date:** 2026-06-29

## Test Framework

**Runner:**
- Framework: pytest 9.0.1+
- Async support: pytest-asyncio 1.3.0+
- Config: `pyproject.toml` under `[tool.pytest]`

**Assertion Library:**
- Built-in pytest assertions
- Exception testing: `pytest.raises()`

**Run Commands:**
```bash
uv run pytest                                    # Run all tests (excludes integration)
uv run pytest tests/unit tests/contract         # Unit + contract tests only
uv run pytest tests/integration                 # Integration tests (requires VM env)
uv run pytest -k "test_name"                    # Run tests matching pattern
uv run pytest tests/unit/test_version.py        # Specific test file
uv run pytest tests/unit/test_version.py::TestVersionStr::test_str  # Specific test
uv run pytest -v                                # Verbose output
uv run pytest --tb=short                        # Shorter traceback format
```

## Test File Organization

**Location:**
- Unit tests: `tests/unit/` - no external dependencies, fast
- Contract tests: `tests/contract/` - verify interface compliance
- Integration tests: `tests/integration/` - require VM infrastructure
- Tests for jobs: `tests/unit/jobs/`, `tests/unit_jobs/` (legacy pattern)
- Tests for orchestrator: `tests/unit/orchestrator/`
- Tests for CLI: `tests/unit/cli/`

**Naming:**
- Test files: `test_<module>.py` (e.g., `test_version.py`, `test_lock.py`)
- Test classes: `Test<Feature>` (e.g., `TestVersionStr`, `TestGetCurrentVersion`)
- Test functions: `test_<what_is_tested>_<expected_result>()` or `test_<behavior>_<condition>()`

**Structure:**
```
tests/
├── conftest.py                 # Shared fixtures (session scope)
├── unit/
│   ├── conftest.py            # Unit test fixtures
│   ├── test_version.py
│   ├── test_lock.py
│   ├── jobs/
│   │   ├── test_snapshot_job.py
│   │   └── conftest.py        # Job-specific fixtures if needed
│   └── orchestrator/
│       ├── test_job_lifecycle.py
│       └── test_lock_conflicts.py
├── contract/
│   ├── test_executor_contract.py
│   └── test_job_interface.py
└── integration/
    ├── conftest.py            # Integration fixtures (VM connections, etc.)
    ├── test_end_to_end_sync.py
    └── jobs/
        └── test_install_on_target_job.py
```

## Test Structure

**Suite Organization:** Class-based grouping with related test methods:

```python
class TestGetCurrentVersion:
    """Tests for get_this_version()."""

    def test_get_this_version_success(self) -> None:
        """Should return Version object from package metadata."""
        # Arrange
        with patch("pcswitcher.version.get_pkg_version") as mock_version:
            mock_version.return_value = "1.2.3"

            # Act
            result = get_this_version()

            # Assert
            assert isinstance(result, Version)
            assert result.pep440_str() == "1.2.3"
            mock_version.assert_called_once_with("pcswitcher")
```

**Docstring Pattern:** One sentence explaining the test expectation (what SHOULD happen). Example: "Should return Version object from package metadata." May reference requirements: "CORE-FR-LOCK: Locking prevents concurrent execution."

**Patterns:**

### Synchronous Tests

```python
class TestLockPath:
    """Tests for lock path utilities."""

    def test_lock_file_name_is_unified(self) -> None:
        """LOCK_FILE_NAME should be a single unified name."""
        assert LOCK_FILE_NAME == "pc-switcher.lock"
```

### Async Tests

```python
@pytest.mark.asyncio
class TestBtrfsSnapshotJobDryRun:
    """Tests for BtrfsSnapshotJob dry-run behavior."""

    async def test_btrfs_snapshot_job_dry_run_logs_without_creating(
        self,
        mock_job_context_factory: Any,
    ) -> None:
        """Verify dry-run mode logs snapshot names but doesn't create them."""
        context = mock_job_context_factory(config={...}, dry_run=True)
        job = BtrfsSnapshotJob(context)
        await job.execute()
        # assertions...
```

### Parametrized Tests

```python
class TestVersionRoundTrip:
    """Tests for round-trip version conversions."""

    @pytest.mark.parametrize(
        "pep440",
        [
            "1.0.0",
            "1.0.0a1",
            "1.0.0.post1",
            "1.0.0a1.post2.dev3+local",
        ],
    )
    def test_pep440_to_semver_to_pep440(self, pep440: str) -> None:
        """PEP 440 → SemVer → PEP 440 should preserve meaning."""
        v = Version.parse_pep440(pep440)
        semver_str = v.semver_str()
        v2 = Version.parse_semver(semver_str)
        assert v == v2
```

## Mocking

**Framework:** `unittest.mock`

**Patterns:**

### Basic Mocking

```python
from unittest.mock import MagicMock, AsyncMock

mock_executor = MagicMock()
mock_executor.run_command = AsyncMock(
    return_value=CommandResult(exit_code=0, stdout="", stderr="")
)
```

### Patching

```python
with patch("pcswitcher.version.get_pkg_version") as mock_version:
    mock_version.return_value = "1.2.3"
    result = get_this_version()
    mock_version.assert_called_once_with("pcswitcher")
```

### Fixture-Based Mocks

Fixtures in `conftest.py` provide reusable mocks:
- `mock_connection`: asyncssh.SSHClientConnection mock
- `mock_executor`: Executor protocol mock (local or remote)
- `mock_remote_executor`: RemoteExecutor with file transfer methods
- `mock_event_bus`: EventBus mock
- `mock_job_context`: Fully mocked JobContext
- `mock_job_context_factory`: Factory for creating JobContext with custom config

**What to Mock:**
- External I/O: file system, network, SSH
- External services: GitHub API, cloud services
- Slow operations: file transfers, database queries
- Non-deterministic operations: system calls, timestamps

**What NOT to Mock:**
- Value objects: dataclasses, enums (use real instances)
- Data structures: dicts, lists (use real instances)
- Core business logic: let it run (create integration tests if needed)
- Time for deterministic behavior: use `freezegun` instead of mocking `datetime`

## Fixtures and Factories

**Test Data:** Fixtures for common results:

```python
@pytest.fixture
def success_result() -> CommandResult:
    """A successful command result with empty output."""
    return CommandResult(exit_code=0, stdout="", stderr="")

@pytest.fixture
def failed_result() -> CommandResult:
    """A failed command result with error message."""
    return CommandResult(exit_code=1, stdout="", stderr="error occurred")
```

**Location:**
- Shared fixtures: `tests/conftest.py` (session/module scope)
- Unit test fixtures: `tests/unit/conftest.py` (unit-specific)
- Integration fixtures: `tests/integration/conftest.py` (VM setup, SSH connections)
- Job-specific: `tests/unit/jobs/conftest.py` if needed

**Factory Fixture Pattern:**

```python
@pytest.fixture
def mock_job_context_factory(
    mock_local_executor: MagicMock,
    mock_remote_executor: MagicMock,
    mock_event_bus: MagicMock,
) -> Callable[[dict[str, Any] | None, bool], JobContext]:
    """Factory fixture to create JobContext with custom config."""

    def create_context(config: dict[str, Any] | None = None, dry_run: bool = False) -> JobContext:
        return JobContext(
            config=config or {},
            source=mock_local_executor,
            target=mock_remote_executor,
            event_bus=mock_event_bus,
            session_id="test-session-12345678",
            source_hostname="source-host",
            target_hostname="target-host",
            dry_run=dry_run,
        )

    return create_context
```

## Coverage

**Requirements:** No enforced minimum, but tests should cover happy path and error cases.

**View Coverage:**
```bash
uv run pytest --cov=src/pcswitcher --cov-report=term-missing
```

## Test Types

**Unit Tests** (`tests/unit/`): Scope: Single function or class in isolation. Mocks: All external dependencies. Speed: Milliseconds. Example: `test_version.py` tests Version parsing without network calls.

**Contract Tests** (`tests/contract/`): Scope: Verify interface implementations. Example: `test_executor_contract.py` verifies both LocalExecutor and RemoteExecutor implement Executor protocol. Mocks: Only truly external (network, FS for some).

**Integration Tests** (`tests/integration/`): Scope: Multiple components working together. Environment: Requires VM infrastructure (HCloud). Skipped: If environment variables `PC_SWITCHER_TEST_PC1_HOST`, `PC_SWITCHER_TEST_PC2_HOST`, `PC_SWITCHER_TEST_USER`, `HCLOUD_TOKEN` not set. Marked: `@pytest.mark.integration`. Run: `uv run pytest tests/integration` (if VMs available).

**Async Tests:** Marked: `@pytest.mark.asyncio`. Mode: `auto` (pytest-asyncio in auto mode, see `pyproject.toml`). Fixture scope: `module` default for connection fixtures (module scoped).

## Common Patterns

**Async Testing:**
```python
@pytest.mark.asyncio
async def test_run_command_success(mock_executor: MagicMock) -> None:
    """Should return CommandResult with exit code 0."""
    result = await mock_executor.run_command("echo test")
    assert result.exit_code == 0
```

**Error Testing:**
```python
def test_find_one_version_no_version_raises(self) -> None:
    """Should raise ValueError for invalid version string."""
    with pytest.raises(ValueError, match="No version string found"):
        find_one_version("no version here")
```

**Fixture Parametrization:**
```python
@pytest.fixture(params=["pep440", "semver"])
def version_format(request: pytest.FixtureRequest) -> str:
    """Parametrized fixture for testing both version formats."""
    return request.param

def test_parse_handles_both_formats(version_format: str) -> None:
    """Should parse both PEP 440 and SemVer formats."""
    if version_format == "pep440":
        v = Version.parse_pep440("1.0.0a1")
    else:
        v = Version.parse_semver("1.0.0-alpha.1")
    assert v.major == 1
```

**Time Freezing:**
```python
@pytest.fixture
def frozen_time():
    """Time-freezing fixture for deterministic tests."""
    return freeze_time("2025-01-15T10:30:00Z")

def test_timestamp_deterministic(frozen_time):
    """Should produce consistent timestamps."""
    with frozen_time:
        now = datetime.now(UTC)
        assert now.year == 2025
```

**Monkeypatching:**
```python
def test_lock_path_uses_home_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """get_lock_path() should return path in .local/share/pc-switcher."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    expected = tmp_path / ".local/share/pc-switcher" / LOCK_FILE_NAME
    assert get_lock_path() == expected
```

## Test Markers

**Defined Markers** (from `pyproject.toml`):
```python
markers = [
    "integration: Integration tests (require VM infrastructure)",
    "slow: Tests that take >5 seconds",
    "benchmark: Performance benchmarks (not run by default)",
]
```

**Usage:**
```python
@pytest.mark.slow
def test_long_operation() -> None:
    """This test takes several seconds."""
    pass

@pytest.mark.integration
async def test_full_sync_on_vms(pc1_executor, pc2_executor) -> None:
    """Run actual sync between test VMs."""
    pass
```

**Running by Marker:**
```bash
uv run pytest -m "not integration"      # Exclude integration tests
uv run pytest -m "slow"                 # Run only slow tests
uv run pytest -m "benchmark"            # Run benchmarks
```

## Pytest Configuration

**Config Location:** `pyproject.toml` under `[tool.pytest]`

**Key Settings:**
- `asyncio_mode = "auto"`: Automatically apply asyncio to test functions
- `asyncio_default_fixture_loop_scope = "module"`: Fixtures use module scope by default
- `asyncio_default_test_loop_scope = "module"`: Tests use module scope by default
- `testpaths = ["tests"]`: Search for tests in `tests/` directory
- `log_cli = true`: Print logs during test execution
- `log_cli_level = "WARNING"`: Suppress noisy third-party logs

**Markers Registration:**
```python
markers = [
    "integration: Integration tests (require VM infrastructure)",
    "slow: Tests that take >5 seconds",
    "benchmark: Performance benchmarks (not run by default)",
]
```

**Strict Markers:** `-m` flag with unknown marker fails: prevents typos in marker names.

---

*Testing analysis: 2026-06-29*
