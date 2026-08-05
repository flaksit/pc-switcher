---
phase: 01-home-sync-mvp-user-data-sync
plan: "05"
subsystem: folder-sync-job
tags: [folder-sync, rsync, execute, progress-streaming, dry-run, divergence-baseline, tdd]
dependency_graph:
  requires:
    - FolderSyncJob class and validate() (plan 04)
    - sync_history.set_target_generation (plan 03)
    - _resolve_subvolume/_get_subvolume_generation (plan 04)
    - LocalExecutor.start_process (existing)
  provides:
    - FolderSyncJob._build_rsync_cmd(folder, dry_run) -> str
    - FolderSyncJob._stream_rsync(chunks, folder) -> tuple[int, int, int]
    - FolderSyncJob.execute() — full rsync transfer, progress streaming, dry-run, marker recording
    - LocalProcess.read_stdout_chunks(size) -> AsyncGenerator[bytes] — public chunk reader
    - LocalProcess.wait_result() -> CommandResult — exit code + stderr after stdout consumed
    - FolderSyncJob in the job contract test suite
  affects:
    - src/pcswitcher/jobs/folder_sync.py
    - src/pcswitcher/executor.py
    - tests/unit/jobs/test_folder_sync.py
    - tests/contract/test_job_interface.py
tech_stack:
  added: []
  patterns:
    - "sudo -E rsync preserves SSH_AUTH_SOCK and $HOME for the rsync subprocess (Pitfall 1)"
    - "shlex.quote on all config-derived values: paths, exclude patterns, hostname (T-05-01)"
    - "AsyncGenerator chunk reader on LocalProcess avoids readline deadlock on \\r-delimited progress2 output (Pitfall 2)"
    - "re.split(rb'[\\r\\n]', buf) handles carriage-return-delimited progress2 and newline-delimited per-file lines in the same byte stream"
    - "wait_result() reads stderr then waits for exit without re-reading stdout (avoids communicate() after stream is drained)"
    - "Divergence baseline recorded only after all folders succeed and only in non-dry-run mode (D-06/D-08/D-12)"
key_files:
  created: []
  modified:
    - src/pcswitcher/jobs/folder_sync.py
    - src/pcswitcher/executor.py
    - tests/unit/jobs/test_folder_sync.py
    - tests/contract/test_job_interface.py
decisions:
  - "Used sudo -E rsync (preserve environment) rather than plain sudo rsync, so root's rsync subprocess inherits SSH_AUTH_SOCK and $HOME and can read ~/.ssh/config (Pitfall 1 fix)"
  - "_stream_rsync decoupled from LocalProcess: takes AsyncIterator[bytes] so tests pass a fake async generator without spawning a real subprocess"
  - "wait_result() as a separate LocalProcess method instead of calling communicate() after stdout drain — communicate() would re-read stdout (empty at that point) but that API contract is fragile; explicit stderr.read() + wait() is clear"
  - "Divergence baseline recorded after ALL folders complete (not folder-by-folder) so a partial failure leaves no false baseline"
  - "Deletion lines detected by startswith('*deleting') matching rsync --out-format '%i %n%L' output where %i='*deleting' for deletions"
metrics:
  duration: "6 minutes"
  completed: "2026-06-30"
  tasks_completed: 2
  tasks_total: 2
  files_changed: 4
status: complete
---

# Phase 01 Plan 05: FolderSyncJob execute() — Data Plane Summary

One-liner: FolderSyncJob.execute() with rsync -aAXHS + sudo-E + async progress streaming via LocalProcess.read_stdout_chunks; dry-run passes --dry-run to rsync and skips marker; post-sync set_target_generation records divergence baseline.

## What Was Built

### Task 1 (TDD): _build_rsync_cmd

Added `FolderSyncJob._build_rsync_cmd(folder: FolderEntry, dry_run: bool) -> str` to `src/pcswitcher/jobs/folder_sync.py`.

The method builds an injection-safe rsync shell command string:

- **Local root**: `sudo -E rsync` (preserves invoking user's `SSH_AUTH_SOCK` and `$HOME` so rsync's internal SSH subprocess can reach `~/.ssh/config` — RESEARCH Pitfall 1).
- **Flag baseline** (D-13): `-aAXHS --numeric-ids --delete --info=progress2 --out-format='%i %n%L' --partial --mkpath`.
- **Remote root** (D-05): `--rsync-path='sudo rsync'` over the normal-user SSH connection; root SSH login stays disabled.
- **SSH transport**: `-e 'ssh -T -q'` (no pseudo-tty, quiet).
- **Filter rules** (D-11): one `--filter='- <pattern>'` per exclude, in config order (first-match-wins preserved). No `--delete-excluded` — machine-specific files must survive on the target (D-06).
- **Source path**: trailing slash (sync contents).
- **Destination**: `<target_hostname>:<path>/` — rsync's own SSH transport, not the asyncssh connection.
- **Injection guard** (T-05-01): `shlex.quote` on all config-derived values (path, patterns, hostname).
- **Dry-run toggle**: `--dry-run` appended only when `dry_run=True` (D-12).

Tests: 11 unit tests in `TestBuildRsyncCmd` — flag baseline presence, forbidden flags absent, filter count/order, dry-run toggle, source/dest format, shell quoting.

### Task 2 (TDD): LocalProcess chunk reader + _stream_rsync + execute()

**`LocalProcess.read_stdout_chunks(size: int = 4096) -> AsyncGenerator[bytes]`** added to `src/pcswitcher/executor.py`:
- Yields raw byte chunks until EOF, avoiding the readline-based `stdout()` iterator that deadlocks on `\r`-delimited progress2 output (RESEARCH Pitfall 2).
- Production code does not reach into `self._proc` directly.

**`LocalProcess.wait_result() -> CommandResult`** added to `src/pcswitcher/executor.py`:
- Reads stderr to EOF then calls `await self._proc.wait()`.
- Returns `CommandResult(exit_code, stdout="", stderr=...)` — stdout is empty since already consumed by `read_stdout_chunks`.
- Enables callers to get the exit code without calling `communicate()` on an already-drained stdout stream.

**`FolderSyncJob._stream_rsync(chunks: AsyncIterator[bytes], folder: FolderEntry) -> tuple[int, int, int]`**:
- Accumulates bytes, splits on `[\r\n]` for mixed carriage-return/newline output.
- Progress2 lines (`xfr#N, to-chk=M/T`) parsed with `_PROGRESS2_RE`; calls `_report_progress(ProgressUpdate(percent, current, total))` for TUI (D-15).
- Per-file `--out-format` lines (starting with `>`, `<`, `*`, `.`) logged at `LogLevel.FULL` (D-16); `*deleting` lines increment the deletion counter.
- Returns `(files_transferred, bytes_transferred, files_deleted)`.
- Decoupled from subprocess for unit testability via fake async generators.

**`FolderSyncJob.execute()`**:
- For each active folder: logs INFO start; calls `_build_rsync_cmd(folder, context.dry_run)`.
- Spawns rsync via `self.source.start_process(cmd)` (non-blocking, ADR-005).
- Streams via `_stream_rsync(proc.read_stdout_chunks(), folder)`.
- After stdout consumed: `result = await proc.wait_result()`.
- Non-zero exit: logs CRITICAL with stderr and raises `RuntimeError` (sync aborts immediately).
- Logs per-folder INFO summary (files, bytes, deletions) (D-16).
- After ALL folders succeed, if `not context.dry_run`: resolves each folder's subvolume via `_resolve_subvolume`, queries generation via `_get_subvolume_generation`, calls `sync_history.set_target_generation(target_hostname, folder.path, gen)` (D-06/D-08).
- In dry-run: rsync still runs with `--dry-run` (real read-only preview per D-12); marker write is skipped.

Tests added to `tests/unit/jobs/test_folder_sync.py`:
- `TestStreamRsync` (7 tests): progress line → ProgressUpdate, per-file line → FULL log, deletion count, multiple deletions, combined sample, carriage-return split, return tuple shape.
- `TestExecuteDryRun` (2 tests): set_target_generation not called; command contains --dry-run.
- `TestExecuteNormalMode` (5 tests): set_target_generation called once per folder; no --dry-run in command; non-zero exit raises; non-zero exit logs CRITICAL; failure skips marker; correct generation value passed.

**`TestFolderSyncJobContract`** added to `tests/contract/test_job_interface.py`: 3 tests verifying FolderSyncJob.name, CONFIG_SCHEMA, and validate() returns a list.

## Verification Results

All plan verification commands green:

- `uv run pytest tests/unit/jobs/test_folder_sync.py tests/contract -q` — 99 passed
- `uv run ruff check src/pcswitcher && uv run basedpyright src/pcswitcher/jobs/folder_sync.py src/pcswitcher/executor.py` — 0 errors
- `uv run pytest tests/unit tests/contract -q` — 479 passed (no regression)
- `uv run python -c "from pcswitcher.jobs import FolderSyncJob"` — imports cleanly
- `_build_rsync_cmd` output manually inspected: `sudo -E rsync -aAXHS --numeric-ids --delete --info=progress2 --out-format='%i %n%L' --partial --mkpath -e 'ssh -T -q' --rsync-path='sudo rsync' --filter='- .ssh/id_*' --filter='- .config/tailscale' /home/ laptop-b:/home/`

## Commits

| Hash | Task | Description |
| ---- | ---- | ----------- |
| fea6dc1 | Task 1 RED | test(01-05): add failing tests for _build_rsync_cmd |
| f2dc285 | Task 1 GREEN | feat(01-05): implement _build_rsync_cmd in FolderSyncJob |
| 237a84e | Task 2 RED | test(01-05): add failing tests for execute(), _stream_rsync, and FolderSyncJob contract |
| 724148f | Task 2 GREEN | feat(01-05): implement execute(), _stream_rsync, and LocalProcess chunk reader |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed nested pytest.raises in test_non_zero_rsync_exit_logs_critical**
- **Found during:** Task 2 RED phase
- **Issue:** Initial test had nested `with pytest.raises(RuntimeError):` blocks, which meant the inner block would catch the exception and the outer block would see no exception and fail with "DID NOT RAISE".
- **Fix:** Rewrote the test using the `caplog` fixture to capture the CRITICAL log record directly, with a single `pytest.raises(RuntimeError)` block.
- **Files modified:** `tests/unit/jobs/test_folder_sync.py`

**2. [Rule 2 - Quality] Used `sudo -E rsync` instead of plain `sudo rsync`**
- **Found during:** Task 1 implementation (RESEARCH Pitfall 1)
- **Issue:** Plain `sudo rsync` runs as root with root's environment, losing `SSH_AUTH_SOCK` and `$HOME`; rsync's internal SSH subprocess then cannot find the user's `~/.ssh/config` or SSH agent keys.
- **Fix:** Used `sudo -E rsync` which preserves the invoking user's environment variables, giving root's rsync subprocess access to the user's SSH identity without enabling root SSH login.
- **Files modified:** `src/pcswitcher/jobs/folder_sync.py`

**3. [Rule 1 - Lint] Replaced `for pattern ... parts.append(...)` with `parts.extend(...)`**
- **Found during:** Task 1 GREEN ruff check (PERF401)
- **Fix:** Changed to generator expression passed to `parts.extend()`.
- **Files modified:** `src/pcswitcher/jobs/folder_sync.py`

**4. [Rule 1 - Lint] Removed unnecessary `AsyncGenerator[bytes, None]` default type arg**
- **Found during:** Task 2 GREEN ruff check (UP043)
- **Fix:** ruff auto-simplified to `AsyncGenerator[bytes]` in executor.py.
- **Files modified:** `src/pcswitcher/executor.py`

## Known Stubs

None. `FolderSyncJob.execute()` is fully implemented. No hardcoded empty values flow to callers.

## Threat Flags

No new network endpoints, auth paths, or schema changes beyond what the plan's threat model already covers:

| Mitigation | Status |
| --------- | ------ |
| T-05-01 (shell injection) | Applied: shlex.quote on all config-derived values |
| T-05-02 (rsync-as-root via sudo) | Applied: sudoers scoped to /usr/bin/rsync documented in user_setup |
| T-05-03 (--delete mirror) | Applied: gated by divergence guard (plan 04 validate()) |
| T-05-04 (secret exclusion) | Applied: --delete-excluded never passed; excluded files survive on target |

## Self-Check: PASSED

Files modified exist:
- src/pcswitcher/jobs/folder_sync.py — FOUND
- src/pcswitcher/executor.py — FOUND
- tests/unit/jobs/test_folder_sync.py — FOUND
- tests/contract/test_job_interface.py — FOUND

Task commits:
- fea6dc1 — FOUND
- f2dc285 — FOUND
- 237a84e — FOUND
- 724148f — FOUND
