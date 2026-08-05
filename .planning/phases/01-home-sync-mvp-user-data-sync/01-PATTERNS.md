# Phase 1: Home-Sync MVP (User Data Sync) - Pattern Map

**Mapped:** 2026-06-30 | **Files analyzed:** 7 new/modified files | **Analogs found:** 7 / 7

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
| ----------------- | ---- | --------- | -------------- | ------------- |
| `src/pcswitcher/jobs/folder_sync.py` | service / sync-job | streaming + request-response | `src/pcswitcher/jobs/btrfs.py` (BtrfsSnapshotJob) + `src/pcswitcher/jobs/install_on_target.py` | exact (same base class, same validate/execute contract) |
| `src/pcswitcher/sync_history.py` | utility / state | file-I/O | self (existing module, extend in-place) | self |
| `src/pcswitcher/config.py` | config | transform | self (existing module, extend in-place) | self |
| `src/pcswitcher/schemas/config-schema.yaml` | config | transform | self (existing schema, extend in-place) | self |
| `src/pcswitcher/default-config.yaml` | config | transform | self (existing file, extend in-place) | self |
| `tests/unit/jobs/test_folder_sync.py` | test | request-response | `tests/contract/test_job_interface.py` + `tests/unit/jobs/test_snapshot_job.py` | role-match |
| `tests/integration/test_folder_sync.py` | test | streaming + file-I/O | `tests/integration/test_end_to_end_sync.py` + `tests/integration/test_btrfs_operations.py` | role-match |

## Pattern Assignments

### `src/pcswitcher/jobs/folder_sync.py` (SyncJob, streaming)

**Primary analog:** `src/pcswitcher/jobs/btrfs.py` (BtrfsSnapshotJob) — shows the complete validate/execute pattern with both-host checks and dry_run guard.

**Secondary analog:** `src/pcswitcher/jobs/install_on_target.py` — shows how a job uses `self.target.run_command(...)` for remote validation and how `self.context.dry_run` gates state changes.

**Imports pattern** (`jobs/btrfs.py` lines 1–16):
```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from pcswitcher.jobs.base import SyncJob  # use SyncJob, not SystemJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import Host, LogLevel, ValidationError

if TYPE_CHECKING:
    from pcswitcher.models import ValidationError
```

**CLASS_SCHEMA pattern** (`jobs/btrfs.py` lines 65–89) — note `additionalProperties` absent at top level to allow schema extension, but present on nested objects:
```python
CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "folders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "enabled": {"type": "boolean", "default": True},
                    "excludes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "minItems": 1,
        }
    },
    "required": ["folders"],
    "additionalProperties": False,
}
```
This matches the style in `BtrfsSnapshotJob.CONFIG_SCHEMA` exactly (Draft7Validator, `additionalProperties: false` on leaf objects).

**validate() pattern** (`jobs/btrfs.py` lines 91–120) — accumulate errors via `self._validation_error()`, check both hosts, check both local and remote state:
```python
async def validate(self) -> list[ValidationError]:
    errors: list[ValidationError] = []

    # Check on source (LocalExecutor)
    result = await self.source.run_command("sudo rsync --version")
    if result.exit_code != 0:
        errors.append(self._validation_error(Host.SOURCE, "sudo rsync not available"))

    # Check on target (RemoteExecutor)
    result = await self.target.run_command("sudo rsync --version")
    if result.exit_code != 0:
        errors.append(self._validation_error(Host.TARGET, "sudo rsync not available on target"))

    # Early return avoids running deeper checks against broken state
    if errors:
        return errors

    # ... divergence check, acl check, subvolume existence check ...
    return errors
```
Pattern: accumulate, return early on hard failures, use `self._validation_error(Host.SOURCE/TARGET, msg)`.

**dry_run gate pattern** (`jobs/btrfs.py` lines 149–168 and `jobs/install_on_target.py` lines 91–95):
```python
async def execute(self) -> None:
    # Log what WILL happen (always, even in dry_run)
    self._log(Host.SOURCE, LogLevel.INFO, "Syncing /home to target")

    if not self.context.dry_run:
        # State-modifying operations: start rsync, update divergence markers
        proc = await self.source.start_process(rsync_cmd)
        ...
    else:
        # Dry-run: log what would transfer without actually running rsync
        self._log(Host.SOURCE, LogLevel.INFO, "[dry-run] would run: " + rsync_cmd)
```

**Logging pattern** (`jobs/btrfs.py` lines 128–133, 140–147):
```python
# INFO: high-level progress (folder summary, phase start/end)
self._log(Host.SOURCE, LogLevel.INFO, f"Starting folder sync: {folder.path}", session_id=self.context.session_id)

# FULL: per-file / per-operation detail (per-file rsync lines)
self._log(Host.SOURCE, LogLevel.FULL, f"{folder.path}: >f..t...... path/to/file.txt", subvolume=folder.path)

# CRITICAL + raise: unrecoverable error
self._log(Host.SOURCE, LogLevel.CRITICAL, "rsync failed", error=result.stderr)
raise RuntimeError(f"rsync failed: {result.stderr}")
```

**Progress reporting pattern** (`src/pcswitcher/jobs/base.py` lines 126–140, contract test lines 34, 131–135):
```python
from pcswitcher.models import ProgressUpdate

# Inside execute():
self._report_progress(ProgressUpdate(percent=pct, current=files_done, total=files_total, item=current_file))
```
`_report_progress` publishes a `ProgressEvent` to `self.context.event_bus` which the `TerminalUI` subscribes to.

**start_process for streaming** (`src/pcswitcher/executor.py` lines 146–161):
```python
# LocalExecutor.start_process returns a LocalProcess with .stdout() async iterator
# But .stdout() is line-based (\n). For rsync --info=progress2 use .read(N) on the
# underlying proc directly (see RESEARCH.md Pattern 2).
proc_wrapper = await self.source.start_process(rsync_cmd)
# Access raw proc: proc_wrapper._proc  (asyncio.subprocess.Process)
# Or: drive via run_command for non-streaming (validate steps, btrfs show, etc.)
result = await self.source.run_command("sudo btrfs subvolume show /home")
```

**run_command for remote validation** (`jobs/install_on_target.py` lines 48–50):
```python
result = await self.target.run_command("sudo rsync --version", login_shell=False)
if result.exit_code != 0:
    ...
# login_shell=False is correct for system commands (sudo, btrfs, rsync).
# login_shell=True only needed when user-installed tools (uv, pc-switcher) are required.
```

### `src/pcswitcher/sync_history.py` (utility, file-I/O)

**Analog:** self — existing module with atomic-write pattern.

**Current read/write shape** (lines 102–133) — atomic write to JSON via temp+rename:
```python
def record_role(role: SyncRole) -> None:
    history_path = get_history_path()
    history_path.parent.mkdir(parents=True, exist_ok=True)

    data = {"last_role": role.value}
    content = json.dumps(data)

    fd, temp_path = tempfile.mkstemp(dir=history_path.parent, prefix=".sync-history-", suffix=".tmp")
    try:
        os.write(fd, content.encode())
        os.close(fd)
        Path(temp_path).rename(history_path)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            Path(temp_path).unlink()
        raise
```

**Extension target** — add `target_generations` key additively. The new read function must handle files that pre-date the extension (missing key → `None`). The new write function must preserve `last_role` when updating generations. Use the same temp+rename atomic write pattern. New JSON shape (backward-compatible):
```json
{
  "last_role": "source",
  "target_generations": {
    "laptop-b": {"/home": 54321, "/root": 1234}
  }
}
```

**Existing read helper pattern** (lines 69–99) — handles missing file (`None, False`), corrupted file (`None, True`), valid file (`SyncRole, False`). New `get_generation` / `set_generation` helpers should follow the same "return `None` for missing" convention.

### `src/pcswitcher/config.py` (config, transform)

**Analog:** self — extend `Configuration` dataclass and `from_yaml()`.

**How job configs are extracted** (lines 163–170):
```python
global_keys = {"logging", "sync_jobs", "disk_space_monitor", "btrfs_snapshots"}
job_configs = {key: value for key, value in data.items() if key not in global_keys and isinstance(value, dict)}
```
The `folder_sync` top-level key will be picked up automatically by this pattern — no change needed in `config.py` unless a typed `FolderSyncConfig` dataclass is added. The orchestrator already routes `job_configs["folder_sync"]` to `FolderSyncJob` via `get_job_config("folder_sync")` (line 185–187).

**How jobs are enabled** (line 143):
```python
sync_jobs = data.get("sync_jobs", {})  # dict[str, bool]
```
The orchestrator uses `sync_jobs["folder_sync"]` to determine if the job runs. The key must be added to `sync_jobs:` in `default-config.yaml` AND to `sync_jobs.properties` in `config-schema.yaml` (or schema rejects it — see Pitfall 6 in RESEARCH.md).

### `src/pcswitcher/schemas/config-schema.yaml` (config schema, transform)

**Analog:** self — existing schema at `src/pcswitcher/schemas/config-schema.yaml`.

**Two changes required:**

1. Add `folder_sync` to `sync_jobs.properties` (line 58–66 pattern):
```yaml
sync_jobs:
  properties:
    folder_sync:
      type: boolean
      default: true
      description: "Generic folder sync job (syncs /home and /root by default)"
    dummy_success:   # existing entry for reference
      type: boolean
      ...
  additionalProperties: false  # line 82 — must remain; folder_sync must be listed above
```

2. Add top-level `folder_sync:` property before line 200 (`additionalProperties: false`), following the pattern of `dummy_success:` (lines 139–153):
```yaml
folder_sync:
  type: object
  description: "Configuration for FolderSyncJob (generic folder sync via rsync)"
  properties:
    folders:
      type: array
      items:
        type: object
        properties:
          path:
            type: string
            description: "Absolute path to sync (e.g., /home)"
          enabled:
            type: boolean
            default: true
          excludes:
            type: array
            items:
              type: string
            default: []
            description: "rsync filter patterns to exclude (relative to folder path)"
        required: [path]
        additionalProperties: false
      minItems: 1
  required: [folders]
  additionalProperties: false
```

### `src/pcswitcher/default-config.yaml` (config, transform)

**Analog:** self — existing file at `src/pcswitcher/default-config.yaml`.

**Pattern:** Follow the same section style as `dummy_success:` (lines 103–110). The new section must add `folder_sync: true` under `sync_jobs:` (after line 43) and add the `folder_sync:` top-level config block with default folder entries and documented exclude patterns.

Style reference (lines 103–110):
```yaml
dummy_success:
  source_duration: 20    # Seconds to run on source machine
  target_duration: 20    # Seconds to run on target machine
```

### `tests/unit/jobs/test_folder_sync.py` (test, unit)

**Analog:** `tests/contract/test_job_interface.py` — shows `mock_job_context` fixture pattern (lines 59–70) and how to test `validate()`, `execute()`, `_log()`, `_report_progress()` with mocks.

**mock_job_context fixture** (contract test lines 59–70):
```python
@pytest.fixture
def mock_job_context() -> JobContext:
    return JobContext(
        config={},
        source=MagicMock(),
        target=MagicMock(),
        event_bus=MagicMock(),
        session_id="test1234",
        source_hostname="source-host",
        target_hostname="target-host",
    )
```
Unit tests for `FolderSyncJob` should use this same fixture pattern (or a variant with `config={"folders": [...]}` pre-populated).

**Async test pattern** (contract test lines 105–115):
```python
@pytest.mark.asyncio
async def test_validate_returns_validation_error_list(self, mock_job_context: JobContext) -> None:
    job = ExampleTestJob(mock_job_context)
    errors = await job.validate()
    assert isinstance(errors, list)
```
`asyncio_mode = "auto"` is set in `pyproject.toml`, so `@pytest.mark.asyncio` may be optional but is explicit in existing tests.

**MagicMock for executor** — mock `source.run_command` return to simulate success/failure:
```python
mock_job_context.source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="...", stderr=""))
mock_job_context.target.run_command = AsyncMock(return_value=CommandResult(exit_code=1, stdout="", stderr="rsync not found"))
```

### `tests/integration/test_folder_sync.py` (test, integration)

**Analog:** `tests/integration/test_end_to_end_sync.py` + `tests/integration/conftest.py`.

**Fixture chain** (conftest.py lines 116–151, 343–366, 441–473):
- Use `pc1_executor`, `pc2_executor` (module-scoped SSH connections to Hetzner VMs).
- Use `pc1_with_pcswitcher_mod` to ensure current-branch pc-switcher is installed on pc1.
- Use `reset_pcswitcher_state` for test isolation (cleans config + data + snapshots before/after each test).
- For folder_sync tests, create test file trees on the VMs via `pc1_executor.run_command("mkdir -p ...")`.

**reset_pcswitcher_state pattern** (conftest.py lines 441–473):
```python
@pytest.fixture
async def reset_pcswitcher_state(pc1_executor, pc2_executor) -> AsyncIterator[None]:
    async def cleanup() -> None:
        await asyncio.gather(
            _remove_config_and_data(pc1_executor),
            _remove_config_and_data(pc2_executor),
            delete_all_snapshots(pc1_executor),
            delete_all_snapshots(pc2_executor),
        )
    await cleanup()
    yield
    await cleanup()
```
The folder_sync integration test must also clean up test file trees (e.g., `rm -rf /tmp/test-home-*` or a dedicated test directory under `/home`).

**Invoking `pc-switcher sync` on the VM** (seen in `test_end_to_end_sync.py` — command pattern):
```python
result = await pc1_executor.run_command(
    "pc-switcher sync pc2 --config ~/.config/pc-switcher/config.yaml",
    timeout=120.0,
    login_shell=True,
)
assert result.exit_code == 0, f"Sync failed:\n{result.stdout}\n{result.stderr}"
```

**Metadata / checksum verification** (pattern from RESEARCH.md integration test spec):
```python
# Checksum comparison
result = await pc2_executor.run_command("md5sum /home/testuser/testfile.txt")
assert result.stdout.split()[0] == expected_md5

# Permissions / owner check
result = await pc2_executor.run_command('stat -c "%a %U %G" /home/testuser/testfile.txt')
assert result.stdout.strip() == "644 testuser testuser"

# ACL check
result = await pc2_executor.run_command("getfacl /home/testuser/testfile.txt")
assert "user:alice:r--" in result.stdout
```

**Module-level `__doc__`** (conftest.py lines 1–19 style) — integration test file must document which acceptance scenarios it covers, VM requirements, and what it exercises.

## Shared Patterns

### dry_run Guard

**Source:** `src/pcswitcher/jobs/btrfs.py` lines 149–168, `src/pcswitcher/jobs/install_on_target.py` lines 91–95. **Apply to:** `FolderSyncJob.execute()` — all state-modifying operations (rsync invocation, divergence marker update) must be gated with `if not self.context.dry_run`.

```python
if not self.context.dry_run:
    result = await create_snapshot(self.source, mount_point, snap_path)
    ...
```

### Validation Error Collection

**Source:** `src/pcswitcher/jobs/btrfs.py` lines 91–120. **Apply to:** `FolderSyncJob.validate()` — collect all errors into `list[ValidationError]`, use `self._validation_error(Host.SOURCE/TARGET, msg)` factory, return early if structural errors prevent further checks.

### Atomic Write (sync_history)

**Source:** `src/pcswitcher/sync_history.py` lines 116–133. **Apply to:** any new write function added to `sync_history.py` for divergence markers.

Pattern: `tempfile.mkstemp(dir=same_dir)` → write → `Path(temp).rename(final)` → cleanup on failure.

### `run_command` for System Commands

**Source:** `src/pcswitcher/jobs/install_on_target.py` line 48, `src/pcswitcher/jobs/btrfs.py` lines 97–103. **Apply to:** all btrfs subvolume queries, sudo availability checks, acl package checks in `FolderSyncJob.validate()`.

Always pass `login_shell=False` for system commands (`sudo`, `btrfs`, `rsync`, `dpkg`). `login_shell=True` is only needed for user-installed tools.

### `start_process` for Long-Running Processes

**Source:** `src/pcswitcher/executor.py` lines 146–161 (LocalExecutor.start_process). **Apply to:** rsync subprocess in `FolderSyncJob.execute()`.

`start_process` returns a `LocalProcess` whose `.stdout()` is a line-based (`\n`) async iterator. For `--info=progress2` (carriage-return-delimited), use `proc_wrapper._proc.stdout.read(N)` in a chunk-reading loop instead of the `.stdout()` iterator. See RESEARCH.md Pattern 2 for the exact chunk-splitting approach.

### Btrfs Subvolume Query

**Source:** `src/pcswitcher/btrfs_snapshots.py` lines 84–115 (`validate_snapshots_directory`, `validate_subvolume_exists`). **Apply to:** divergence detection in `FolderSyncJob` — both "is this a subvolume?" check and "what is the generation?" query.

```python
result = await executor.run_command(f"sudo btrfs subvolume show {mount_point} 2>&1")
if result.exit_code != 0:
    # Not a subvolume — fall back to no divergence check
    ...
# Parse generation from stdout:
for line in result.stdout.splitlines():
    if "Generation:" in line:
        generation = int(line.split()[-1])
        break
```

### Integration Test Fixture Chain

**Source:** `tests/integration/conftest.py`. **Apply to:** `tests/integration/test_folder_sync.py`.

Always import and use:
- `pc1_with_pcswitcher_mod` (ensures current-branch code)
- `reset_pcswitcher_state` (test isolation — yields, cleans up both before and after)
- `pc1_executor` / `pc2_executor` (module-scoped SSH connections)

## No Analog Found

All files have close analogs in the codebase. No file requires falling back to RESEARCH.md patterns exclusively.

## Metadata

**Analog search scope:** `src/pcswitcher/jobs/`, `src/pcswitcher/`, `tests/integration/`, `tests/contract/`, `tests/unit/`. **Files scanned:** 12 source files + 3 test files. **Pattern extraction date:** 2026-06-30.
