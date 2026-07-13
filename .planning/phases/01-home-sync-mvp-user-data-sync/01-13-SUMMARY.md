---
phase: 01-home-sync-mvp-user-data-sync
plan: 13
subsystem: orchestrator / CLI / sync-safety
tags: [topology, sync-safety, adr-015, cli, out-of-order]
dependency_graph:
  requires: [01-11, 01-12]
  provides: [topology-out-of-order-check, allow-out-of-order-flag, last-peer-recording]
  affects: [orchestrator, cli, job-context, sync-history]
tech_stack:
  added: []
  patterns: [topology-based-warn-confirm, rich-panel-prompt, ssh-cat-sync-history]
key_files:
  created: []
  modified:
    - src/pcswitcher/cli.py
    - src/pcswitcher/jobs/context.py
    - src/pcswitcher/orchestrator.py
    - tests/unit/orchestrator/test_consecutive_sync.py
    - tests/unit/test_dry_run.py
    - tests/unit/jobs/test_folder_sync.py
decisions:
  - "Single --allow-out-of-order flag replaces two old flags (--allow-consecutive + --allow-divergence): simpler mental model aligned with ADR-015 topology check"
  - "_check_out_of_order() inserted between Phase 3 (target lock) and Phase 4 (job discovery): SSH connection already up, no extra step counter added, 8-phase progress formula unchanged"
  - "W1/W2/W3 warn+confirm not hard-abort: preserves GitHub #159 A->B/work/A->B legitimate workflow"
  - "dry-run logs warning but never aborts per ADR-014: read-only rehearsal contract"
  - "last_peer recorded on both ends via record_role(peer=...) and get_record_role_command(peer=...): enables clean-case suppression on next sync"
metrics:
  duration: ~20min
  completed: 2026-07-02
  tasks_completed: 3
  files_modified: 6
status: complete
---

# Phase 01 Plan 13: Topology Out-of-Order Step Summary

Single --allow-out-of-order flag and topology warn+confirm step replace the consecutive-sync check and all allow_divergence plumbing, recording last_peer on both machines for future runs.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Consolidate CLI + context flags onto --allow-out-of-order | f3cc45d | cli.py, jobs/context.py |
| 2 | Replace consecutive check with out-of-order/target-state step | efe1f07 | orchestrator.py, test_folder_sync.py |
| 3 | Rework orchestrator and dry-run tests | 87b5092 | test_consecutive_sync.py, test_dry_run.py |

## What Was Built

### CLI (cli.py)

`--allow-out-of-order` replaces the two old overrides (`--allow-consecutive` and `--allow-divergence`). The flag is threaded from `sync()` → `_run_sync()` → `_async_run_sync()` → `Orchestrator(allow_out_of_order=...)`. Docstrings updated to reflect the new flag and drop the removed ones.

### JobContext (jobs/context.py)

`allow_divergence` field removed. The divergence concept is superseded by the topology check in the orchestrator; nothing in the job layer needs it.

### Orchestrator (orchestrator.py)

`_check_consecutive_sync()` and the pre-phase consecutive-sync block are gone. In their place:

- `__init__` now takes `allow_out_of_order: bool = False`; stores it as `self._allow_out_of_order`
- A single call to `await self._check_out_of_order()` is inserted between Phase 3 (target lock, `set_current_step(3)`) and Phase 4 (job discovery). No new `set_current_step` added; the 8-phase formula and 100%-progress invariant are unchanged.
- `_check_out_of_order()` implements the ADR-015 truth table:
  - Reads local state via `get_last_sync_state()`
  - Fetches target state via `cat ~/.local/share/pc-switcher/sync-history.json 2>/dev/null` over the established SSH connection; treats empty/non-zero-exit as unreadable
  - **SUPPRESS** (silent): target_readable AND target_peer == source AND NOT consecutive_push
  - **W1 warn**: no readable target history
  - **W2 warn**: target last synced with a different machine (machine-C case)
  - **W3 warn**: consecutive push from this source to same target
  - dry-run: logs warning at WARNING, returns True (ADR-014 read-only rehearsal)
  - non-interactive: prints warning, returns False
  - interactive: stops UI, shows Rich Panel, asks y/n (default n), restarts UI
  - `--allow-out-of-order`: logs INFO bypass, returns True immediately without SSH read
- `_update_sync_history()` now records `last_peer` on both ends:
  - Local: `record_role(SyncRole.SOURCE, peer=self._target_hostname)`
  - Remote: `get_record_role_command(SyncRole.TARGET, peer=self._source_hostname)`
- Imports updated: removed `get_last_role_with_error`, added `HISTORY_PATH`, `get_last_sync_state`, `parse_sync_state`

### Tests

`test_consecutive_sync.py` is fully rewritten to cover `_check_out_of_order` across all cases:
- Clean case: no warning, no prompt
- W3 (consecutive push): interactive accept/decline, non-interactive → False
- W2 (machine-C): non-interactive → False, interactive → prompts
- W1 (no/unreadable target history): warns in all modes
- `--allow-out-of-order`: returns True without SSH read or prompt
- dry-run + warn condition: returns True, logger.warning called

`TestUpdateSyncHistoryWithPeer` added to verify `last_peer` is present in both local file and remote command string.

`test_dry_run.py`: removed `TestJobContextAllowDivergenceField` and `test_orchestrator_accepts_allow_divergence_parameter`; added `test_orchestrator_accepts_allow_out_of_order_parameter`.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Remove allow_divergence from test_folder_sync.py make_context helper**
- **Found during:** Task 2 — after removing `allow_divergence` from `JobContext`, basedpyright errored at `tests/unit/jobs/test_folder_sync.py:59`
- **Issue:** `make_context()` accepted `allow_divergence: bool = False` and passed it to `JobContext(allow_divergence=...)`, which no longer has that field
- **Fix:** Removed `allow_divergence` parameter from `make_context()`; no call-sites used it with `True` so the change is safe
- **Files modified:** `tests/unit/jobs/test_folder_sync.py`
- **Commit:** efe1f07 (included in Task 2 commit)

**2. [Post-review fix] Resolve basedpyright regressions in reworked test file**
- **Found during:** Coordinator review after Task 3 — `test_consecutive_sync.py` raised 26 `reportOptionalMemberAccess` + `reportAttributeAccessIssue` errors
- **Issue:** Mock-assertion calls like `orchestrator._ui.stop.assert_not_called()` accessed attributes on `orchestrator._ui` (typed `TerminalUI | None`) and `orchestrator._remote_executor` (typed `RemoteExecutor | None`). The existing `# pyright: ignore[reportPrivateUsage]` did not cover the None-access and MethodType-attribute errors.
- **Fix:** Wrapped each private mock access in `cast(MagicMock, orchestrator._X)` so the type narrows to `MagicMock` (mock-assertion methods resolve via `__getattr__`, and `None` is excluded). The `reportPrivateUsage` ignore remains valid for the still-private attribute read.
- **Files modified:** `tests/unit/orchestrator/test_consecutive_sync.py`
- **Verification:** `uv run basedpyright` → 0 errors; `uv run pytest` → 505 passed

## Known Stubs

None — all topology check code is fully wired.

## Threat Flags

No new network endpoints, auth paths, or trust boundaries beyond those documented in the plan's threat model (T-01-13-01 through T-01-13-03).

## Self-Check: PASSED

All files verified to exist. All commits verified in git log.
