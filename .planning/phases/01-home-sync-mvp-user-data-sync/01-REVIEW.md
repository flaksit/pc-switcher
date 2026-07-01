---
phase: 01-home-sync-mvp-user-data-sync
reviewed: 2026-06-30T00:00:00Z
depth: standard
files_reviewed: 18
files_reviewed_list:
  - docs/adr/adr-013-rsync-over-ssh-user-data-transport.md
  - docs/adr/adr-014-unified-dry-run-contract.md
  - docs/adr/_index.md
  - src/pcswitcher/cli.py
  - src/pcswitcher/default-config.yaml
  - src/pcswitcher/executor.py
  - src/pcswitcher/jobs/context.py
  - src/pcswitcher/jobs/folder_sync.py
  - src/pcswitcher/jobs/__init__.py
  - src/pcswitcher/orchestrator.py
  - src/pcswitcher/schemas/config-schema.yaml
  - src/pcswitcher/sync_history.py
  - tests/contract/test_job_interface.py
  - tests/integration/test_folder_sync.py
  - tests/unit/jobs/test_folder_sync.py
  - tests/unit/orchestrator/test_consecutive_sync.py
  - tests/unit/test_dry_run.py
  - tests/unit/test_sync_history.py
findings:
  critical: 2
  warning: 3
  info: 5
  total: 10
status: issues_found
---

# Phase 1: Code Review Report

- **Reviewed:** 2026-06-30
- **Depth:** standard
- **Files Reviewed:** 18
- **Status:** issues_found

## Summary

The folder-sync slice is generally careful where the prompt flagged the highest risk: every config-derived value that reaches a shell is `shlex.quote`'d (folder paths, exclude patterns, the `ssh -T -q` transport, `--rsync-path='sudo rsync'`, and the `host:path` destination), and I could not construct a local shell-injection through `_build_rsync_cmd` or the `validate()` preflight/divergence commands. Dry-run side-effect freedom also holds: `execute()` adds `--dry-run` to rsync and skips the `set_target_generation` marker write under `dry_run`, and the orchestrator skips the post-sync history update under `dry_run`.

However, the divergence guard — explicitly called out as the data-loss safety linchpin — has two serious defects. First, with the **shipped default config** (which syncs `/home`, giving an empty subvolume prefix), the tool's own post-sync write of `sync-history.json` onto the target's `/home` lands *after* the divergence baseline is captured, which makes the guard report a false divergence and block essentially every second sync. The integration suite misses this because it deliberately syncs a subdirectory (non-empty prefix), not `/home`. Second, the guard **fails open**: when `btrfs find-new` or subvolume resolution fails, it logs a warning and proceeds with the destructive mirror+delete instead of blocking. For a safety linchpin, inability to verify the target should fail closed.

## Critical Issues

### CR-01: Default config (`/home`, empty prefix) self-triggers a false divergence and blocks every second sync

**File:** `src/pcswitcher/jobs/folder_sync.py:507-522`, `src/pcswitcher/orchestrator.py:266-284`

**Issue:**

The post-sync divergence baseline is recorded inside `FolderSyncJob.execute()` (orchestrator Phase 9) via `_get_subvolume_generation(mount)`. The orchestrator then continues, and in Phase 10+ calls `_update_sync_history()` (orchestrator.py:283-284), whose `get_record_role_command(SyncRole.TARGET)` writes `~/.local/share/pc-switcher/sync-history.json` **on the target**. Because the SSH user is a normal user, that path lives under `/home/<user>/.local`, i.e. inside the `@home` subvolume — and that write happens *after* the baseline generation was captured, so it bumps `@home`'s generation past the stored baseline.

On the next sync, `_target_diverged_since` runs `btrfs subvolume find-new /home <baseline>`. For the `/home` folder entry, `_resolve_subvolume("/home")` returns `mount="/home"`, `prefix=""`. With `prefix == ""` the matcher at folder_sync.py:341 treats *any* changed path as divergence:

```python
if prefix == "" or f" {prefix}/" in line or line.endswith(f" {prefix}"):
    return True
```

The tool's own `sync-history.json` (and any target-side log/install writes under `~/.local`) therefore register as a divergence, and the sync is blocked with "Target divergence detected" even though the user never touched the target. This makes the default `/home` sync unusable for repeated runs.

The integration test `tests/integration/test_folder_sync.py:341-503` does not catch this because `_make_config` syncs `/home/<user>/pcswitcher-folder-sync-test`, which yields a non-empty prefix that happens to exclude `~/.local`. The default `folders: [- path: /home]` is never exercised end-to-end.

**Fix:**

Capture the divergence baseline only after *all* target-side writes for the session are complete (i.e. record `set_target_generation` after `_update_sync_history` and the post-sync snapshot, e.g. as a dedicated final orchestrator phase), or exclude the pc-switcher state directory from the divergence scope. Concretely, move baseline recording out of `execute()` into a post-history orchestrator step:

```python
# orchestrator.run(), after _update_sync_history() and post snapshots:
if not self._dry_run:
    await self._record_divergence_baselines()  # calls set_target_generation per folder
```

and have `_target_diverged_since` ignore changes under the pc-switcher state path (`.local/share/pc-switcher/`) so unavoidable tool writes never count as user divergence. Add an end-to-end test that uses the default `/home` entry (empty prefix) and performs two consecutive A→B syncs.

### CR-02: Divergence guard fails open — destructive mirror+delete proceeds when the target cannot be verified

**File:** `src/pcswitcher/jobs/folder_sync.py:308-344`

**Issue:**

The guard returns `False` ("not diverged", proceed) whenever it cannot actually determine the target's state:

```python
resolved = await self._resolve_subvolume(folder.path)
if resolved is None:
    self._log(... "skipping divergence check")
    return False                      # -> proceeds to mirror+delete
...
if result.exit_code != 0:             # find-new failed
    self._log(... "skipping divergence check")
    return False                      # -> proceeds to mirror+delete
```

After a baseline has been established, a *transient* failure of `findmnt` or `sudo btrfs subvolume find-new` on the target (sudo hiccup, btrfs error, command not found, unexpected output) causes the guard to silently allow the subsequent `rsync -aAXHS --delete`, which deletes any target-side changes. For the component the prompt identifies as the data-loss linchpin, "cannot verify" must fail **closed** (block, require `--allow-divergence`), not open.

`_resolve_subvolume` returning `None` for a genuinely non-btrfs path is a documented tradeoff (RESEARCH Open Q3), but the more dangerous case is a previously-working btrfs target where the query *fails*: the baseline only exists because a prior `btrfs subvolume show` succeeded, so a later failure is an anomaly that should stop the destructive operation, not wave it through.

**Fix:**

Distinguish "no baseline / known non-btrfs" (safe to proceed) from "verification command failed" (must block). When `stored is not None` and `find-new`/`findmnt` fails, return a blocking `ValidationError` unless `allow_divergence` is set, rather than `return False`.

## Warnings

### WR-01: `bytes_transferred` is always 0 in the per-folder audit summary

**File:** `src/pcswitcher/jobs/folder_sync.py:405-447, 498-505`

**Issue:**

`_stream_rsync` initializes `bytes_xfr = 0` and never reassigns it; `_PROGRESS2_RE` (folder_sync.py:27) captures only percent, `xfr#`, and the `to-chk` total — not the leading byte figure (`9.53G`). The docstring claims "bytes_transferred is a best-effort count based on the last progress line," but the returned value is constant 0, so `execute()` logs `"... 0 bytes ..."` for every transfer regardless of size. This is a misleading audit trail for an operation whose logging is otherwise emphasized (D-16).

**Fix:** Either capture the byte figure (add a leading group to the regex and convert the `K/M/G/T` suffix to bytes) and assign it to `bytes_xfr`, or drop the byte count from the summary line and the return tuple so the log does not assert a false "0 bytes".

### WR-02: Baseline-record failure after a successful destructive sync leaves inconsistent state

**File:** `src/pcswitcher/jobs/folder_sync.py:507-522`, `src/pcswitcher/jobs/folder_sync.py:287-292`

**Issue:**

`execute()` runs the destructive `rsync --delete` first, then records the baseline. If `_get_subvolume_generation` raises (`RuntimeError` when the `Generation:` line is absent or the command fails), the exception propagates and the job is marked FAILED (orchestrator.py:747-764) and the whole sync aborts — even though the target data was already fully mirrored. The post-sync history update is then skipped, and on the next run the *old* baseline is compared against a target that the just-completed (but "failed") sync already advanced, which will itself read as a divergence. The user sees a failed sync that actually mutated the target.

**Fix:** Treat baseline recording as best-effort and non-fatal (log a WARNING and continue, as is already done for the `resolved is None` branch at folder_sync.py:513-519) rather than letting it abort an otherwise-successful sync; or record the baseline before any externally observable failure point.

### WR-03: Divergence is checked at validate-time (Phase 4) but the destructive rsync runs at Phase 9

**File:** `src/pcswitcher/orchestrator.py:239-268`, `src/pcswitcher/jobs/folder_sync.py:177-183`

**Issue:**

`_check_divergence` runs during `_discover_and_validate_jobs` (Phase 4), but the `rsync --delete` that acts on the result runs in `_execute_jobs` (Phase 9), after disk checks, snapshots, install, and config sync. The target lock prevents another *pc-switcher* sync but not arbitrary user/process writes to the target during that window. A target modification that arrives after Phase 4 will be deleted by the Phase 9 mirror without ever being seen by the guard. For a data-loss safety mechanism this TOCTOU gap is material.

**Fix:** Re-run the divergence check immediately before the destructive rsync (inside `execute()`, gated by the same `allow_divergence`/`dry_run` rules), or document and justify the accepted window explicitly with the lock guarantees it relies on.

## Info

### IN-01: SIGINT cleanup wait is a no-op; its timeout branch is unreachable

**File:** `src/pcswitcher/cli.py:340-355`

**Issue:** `await asyncio.wait_for(asyncio.shield(asyncio.sleep(0)), timeout=CLEANUP_TIMEOUT_SECONDS)` waits on `sleep(0)`, which returns immediately, so it neither grants the orchestrator any cleanup time nor can it ever raise `TimeoutError`. By the time this `except asyncio.CancelledError` block runs, the orchestrator's `finally`/`_cleanup` has already completed as part of the cancelled task. The block reads as if it bounds cleanup time but does nothing.

**Fix:** Remove the dead `wait_for`/`TimeoutError` handling, or implement an actual bounded wait on a real cleanup future if a timeout is desired.

### IN-02: `total_steps` over-counts when jobs are disabled, so the progress bar never reaches 100%

**File:** `src/pcswitcher/orchestrator.py:196, 274`

**Issue:** `total_steps = 8 + len(self._config.sync_jobs) + 1` counts *all* configured job entries (including `enabled: false` ones), while the final step is set to `8 + len(jobs) + 1` using only enabled+valid jobs. With the default config (`dummy_success: true`, `dummy_fail: false`, `folder_sync: true`) total is 12 but the last step set is 11, so the UI never completes.

**Fix:** Compute `total_steps` from the count of jobs that will actually run, or set it after `_discover_and_validate_jobs` returns `jobs`.

### IN-03: rsync itemize parsing misses `c` and `h` change types

**File:** `src/pcswitcher/jobs/folder_sync.py:433, 442`

**Issue:** Per-file `--out-format` lines are recognized only when the first char is in `(">", "<", "*", ".")`. rsync's `%i` also emits `c` (created items: directories, symlinks, devices) and `h` (hard links), so those changes are silently dropped from FULL logging. Deletion counting is unaffected (`*deleting`), so this is cosmetic/log-completeness only.

**Fix:** Include `c` and `h` (and any other valid leading itemize codes) in the recognized set.

### IN-04: `os.write` partial-write and missing fsync in atomic history writes

**File:** `src/pcswitcher/sync_history.py:151, 196-198, 279`

**Issue:** `os.write(fd, content.encode())` may write fewer bytes than requested and the result is not checked, and the temp file is renamed without an `fsync` of its contents. For the small JSON payloads here a partial write is unlikely, but on a crash between rename and writeback the marker file could be truncated/empty. The same pattern is embedded in the remote `python3 -c` script.

**Fix:** Use `os.fdopen(fd, "wb").write(...)` (which loops) or check the `os.write` return value, and `os.fsync(fd)` before the rename for durability of the state the divergence guard depends on.

### IN-05: Changes to *excluded* files on the target still trigger divergence (false positive, fails safe)

**File:** `src/pcswitcher/jobs/folder_sync.py:332-344`

**Issue:** `find-new` reports raw filesystem changes and is independent of rsync `--filter` excludes. With `prefix == ""` (e.g. `/home`), a target-side change to an excluded path (`.cache/nvidia`, `.config/tailscale`, etc.) counts as divergence and blocks the sync, even though that file would never be synced or deleted. This fails closed (safe) but can surprise users by blocking on files the tool explicitly ignores. Related to CR-01's root cause (empty-prefix over-matching).

**Fix:** Consider intersecting the divergence scope with the actual sync scope (apply the same exclude filters when deciding whether a changed path constitutes meaningful divergence), or document the intended conservatism.

## Footer

- _Reviewed: 2026-06-30_
- _Reviewer: Claude (gsd-code-reviewer)_
- _Depth: standard_
