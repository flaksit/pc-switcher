# Testing Patterns

**Analysis Date:** 2026-07-23

Full authoring rules: `docs/dev/testing-guide.md`. This document records the framework, layout, and patterns as they exist.

## Test Framework

**Runner:**
- `pytest >= 9.1.1` with `pytest-asyncio >= 1.4.0` and `pytest-randomly >= 4.1.0` (random test ordering — tests must be order-independent)
- `freezegun >= 1.5.5` for deterministic timestamps
- Config: `[tool.pytest]` table in `pyproject.toml`

**Config highlights (`pyproject.toml`):**
- `addopts = ["--strict-markers", "-v", "-m", "not integration"]` — integration tests are excluded by default
- `asyncio_mode = "auto"`, module-scoped default fixture and test loops
- `log_cli = true`, `log_cli_level = "WARNING"` (third-party noise suppressed; `tests/conftest.py` raises `pcswitcher` and `tests` loggers to DEBUG)

**Assertion library:** plain `assert` + `unittest.mock` (`AsyncMock`, `MagicMock`, `patch`).

**Run Commands:**
```bash
uv run pytest                                   # unit + contract (integration deselected)
uv run pytest tests/unit tests/contract -v      # what CI runs
uv run pytest tests/unit/test_lock.py::TestSyncLock::test_release_is_idempotent
tests/run-integration-tests.sh                  # integration, provisions/reset VMs
tests/run-integration-tests.sh --skip-reset -k test_ssh
uv run pytest tests/local_rsync                 # real local rsync, no VMs
```

Always `uv run pytest`; bare `pytest` or `python -m pytest` fails with `No module named 'pcswitcher'`.

## Test File Organization

```text
tests/
├── conftest.py              # global fixtures: logging reset, mock connection/executors, event bus
├── unit/                    # mocked, fast — mirrors src layout
│   ├── conftest.py          # mock JobContext factory, frozen_time
│   ├── cli/ executor/ jobs/ orchestrator/ ui/
│   └── test_lock.py, test_logging.py, test_version.py, ...
├── unit_jobs/               # legacy job unit tests (prefer tests/unit/jobs/)
├── contract/                # job-interface conformance
├── local_rsync/             # shells out to a real local rsync binary
├── integration/             # real SSH against pc1/pc2 VMs
│   ├── conftest.py          # VM connections, executors, env-var gate, auto marker
│   ├── scripts/             # VM reset/provisioning helpers
│   └── benchmarks/          # perf, excluded from CI
├── run-integration-tests.sh
├── manual-playbook.md       # manual TUI verification steps
└── self-update-test-playbook.md
```

**Tiers:** unit (logic, mocked executors) → contract (job interface) → local_rsync (real binary, no VM) → integration (real SSH/btrfs) → manual playbook (TUI visuals). Most requirements need both a unit and an integration test.

## Naming

**Spec-driven tests** (requirements from `specs/*/spec.md`): `test_<feature>_<req-id>_<description>`, docstring opening with the requirement ID.

```python
async def test_core_fr_version_check(self, mock_install_context: JobContext) -> None:
    """CORE-FR-VERSION-CHECK: System must check target version and install from GitHub."""
```

**General tests:** `test_<subject>_<expected_behavior>`, e.g. `test_acquire_creates_lock_file`.

**Grouping:** behavior-scoped classes named `Test<Subject><Aspect>` — `TestInstallOnTargetJobVersionCheck`, `TestSyncLock`, `TestStartPersistentRemoteLock`. No `unittest.TestCase`; plain classes with fixture-injected args.

## Test Structure

```python
class TestDiskSpaceMonitorValidation:
    """Test validate() method for system state validation."""

    @pytest.mark.asyncio
    async def test_validate_checks_mount_point_exists(self, mock_job_context: JobContext) -> None:
        """validate() should check that mount point exists."""
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")
        errors = await job.validate()

        assert errors == []
        mock_job_context.source.run_command.assert_called_once_with("test -d /")
```

Module-level marker application for a whole file:

```python
pytestmark = [
    pytest.mark.local_rsync,
    pytest.mark.skipif(shutil.which("rsync") is None, reason="requires local rsync binary"),
]
```

`@pytest.mark.parametrize` is used sparingly (`tests/unit/test_version.py`, `tests/unit/test_disk_format.py`) — preferred for pure format/parse tables, not for behavioral variants.

## Fixtures

From `tests/conftest.py`:
- `_configure_test_logging` (session, autouse) — root at WARNING, `pcswitcher`/`tests` at DEBUG
- `_reset_logging_after_test` (autouse) — clears handlers/propagate so `setup_logging()` calls don't leak into `caplog` assertions
- `mock_connection`, `mock_executor`, `mock_remote_executor`, `mock_event_bus`, `sample_command_result`, `failed_command_result`

From `tests/unit/conftest.py`:
- `mock_local_executor`, `mock_remote_executor`, `mock_event_bus`
- `mock_job_context` — fully mocked `JobContext`; plus a factory fixture for custom config
- `frozen_time` / its companion datetime fixture for deterministic timestamps

From `tests/integration/conftest.py`:
- `pc1_connection`, `pc2_connection`, `pc1_executor`, `pc2_executor`, `test_volume` — all module-scoped
- `pytest_collection_modifyitems` auto-applies `@pytest.mark.integration` to everything under `tests/integration/`
- session fixture asserts `PC_SWITCHER_TEST_PC1_HOST` / `PC2_HOST` / `TEST_USER` are set

Per-test-file fixtures build a specialized `JobContext` on top of the shared mocks (see `tests/unit/jobs/test_install_on_target_job.py:24`).

## Mocking

**What to mock:** SSH connections, executors, the event bus, network/GitHub lookups, and the clock.

```python
mock_executor.run_command = AsyncMock(
    return_value=CommandResult(exit_code=0, stdout="output", stderr="")
)

# ordered command sequence
mock_ctx.target.run_command = AsyncMock(side_effect=[
    CommandResult(exit_code=127, stdout="", stderr="command not found"),
    CommandResult(exit_code=0, stdout="", stderr=""),
])

# command-dependent dispatch
async def mock_run_command(cmd: str) -> CommandResult:
    return CommandResult(exit_code=0 if "test -d" in cmd else 1, stdout="", stderr="")
mock_executor.run_command = AsyncMock(side_effect=mock_run_command)

# patch module-level lookups where they are used, not where defined
with patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=mock_version):
    ...
```

**What NOT to mock:** pure logic under test, dataclasses/models, filesystem work that `tmp_path` can host, and rsync filter semantics (covered by real rsync in `tests/local_rsync/`).

Debug mismatches with `print(mock_executor.run_command.call_args_list)`.

## Integration Test Rules

- VMs reset to baseline once per pytest session, not between tests; the baseline has no pc-switcher installed
- Every test cleans its own artifacts in `try/finally`, tolerating failure (`... 2>/dev/null || true`)
- Artifact names must be unique and descriptive: `/.snapshots/test-foo-verify-readonly`
- Each `run_command()` costs ~70–80 ms SSH round trip; chain with `&&` when more than 3–5 sequential commands are needed
- Never hardcode VM IPs or paths — use env vars and fixtures
- Integration CI (`.github/workflows/integration-tests.yml`) runs only on non-draft PRs targeting `main`; stacked PRs based on another branch skip integration entirely

## Contract Tests

`tests/contract/` verifies every job satisfies the `Job` interface: presence of `name` and `CONFIG_SCHEMA`, `validate_config()` returning `[]` for valid config and `list[ConfigError]` for invalid, `validate()` returning `list[ValidationError]`, and `execute()` completing. Add a contract entry when introducing a new job class.

## Markers

```python
@pytest.mark.integration   # requires VMs (auto-applied in tests/integration/)
@pytest.mark.local_rsync   # shells out to real rsync; skipped if the binary is absent
@pytest.mark.slow          # >5 seconds
@pytest.mark.benchmark     # perf, not run by default
```
`--strict-markers` is on: register any new marker in `pyproject.toml` before using it.

## Coverage

No coverage threshold is configured or enforced in CI. Ad-hoc report:

```bash
uv run pytest tests/unit --cov=src/pcswitcher --cov-report=html
```

Spec-requirement coverage is tracked instead in `specs/<feature>/contracts/coverage-map.yaml` — update it when a test covers a spec requirement.

## Common Patterns

**Async testing:** `asyncio_mode = "auto"`, but existing tests still carry explicit `@pytest.mark.asyncio`; match the surrounding file. Fixtures and tests default to module-scoped loops — do not override the scope without a reason, or async objects leak across loops.

**Error testing:**
```python
with pytest.raises(SyncLockedError, match="held by"):
    await lock.acquire()
```

**Filesystem testing:** use `tmp_path`; write helper builders (`_write(path, content)`) instead of fixture files where the tree is small.

**TUI testing:** poll for a render marker instead of `sleep()` — rich 15's `Live` paints its first frame after the initial 10 Hz tick. Visual-only changes go into `tests/manual-playbook.md`.

## CI

`.github/workflows/ci.yml` on every branch push, gated by a paths filter (`src/**`, `tests/unit/**`, `tests/contract/**`, `pyproject.toml`, `uv.lock`, `ruff.toml`):
1. `uv run basedpyright`
2. `uv run ruff check` + `uv run ruff format --check`
3. `uv run codespell`
4. `uv run pytest tests/unit tests/contract -v`
