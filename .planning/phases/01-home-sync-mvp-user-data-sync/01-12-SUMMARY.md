---
phase: 01-home-sync-mvp-user-data-sync
plan: 12
subsystem: sync-history
tags: [sync-history, topology, last-peer, adr-015]
dependency_graph:
  requires: [01-11]
  provides: [parse_sync_state, get_last_sync_state, last_peer persistence]
  affects: [src/pcswitcher/sync_history.py, tests/unit/test_sync_history.py]
tech_stack:
  added: []
  patterns: [merge-preserving atomic write, repr()-escaped peer literal injection]
key_files:
  created: []
  modified:
    - src/pcswitcher/sync_history.py
    - tests/unit/test_sync_history.py
decisions:
  - record_role/get_record_role_command take optional peer; back-compatible default None
  - peer injected into remote python3 script via repr() for safe shell quoting (T-01-12-02)
  - parse_sync_state uses bare except to guarantee never-raise on untrusted remote input (T-01-12-01)
metrics:
  duration: 8min
  completed: 2026-07-02
  tasks_completed: 3
  files_modified: 2
status: complete
---

# Phase 01 Plan 12: Sync History Topology Simplification Summary

sync_history simplified to the {last_role, last_peer} topology model: peer persistence added, pure JSON parser and local reader provided, btrfs per-target generation store deleted.

## Tasks Completed

| Task | Description | Commit |
|------|-------------|--------|
| 1 | Add last_peer support and (role, peer) readers | 8b45531 |
| 2 | Remove btrfs per-target generation store | 2ad8228 |
| 3 | Rework sync_history tests for {last_role, last_peer} schema | 590962c |

## What Was Built

### Task 1 — last_peer support and (role, peer) readers

`record_role(role, peer=None)` now accepts an optional peer hostname and writes `last_peer` into the history JSON alongside `last_role` when provided. Signature is back-compatible: callers that omit `peer` continue to work unchanged.

`get_record_role_command(role, peer=None)` produces the same merge-preserving remote python3 script but, when `peer` is given, injects a `d['last_peer']=repr(peer)` line. `repr()` produces a proper Python string literal; hostnames are plain ASCII so the result never contains double-quotes or backslashes that could break the shell `-c` argument (T-01-12-02 mitigation).

Two new public functions:
- `parse_sync_state(content: str) -> tuple[SyncRole | None, str | None]`: pure parser for an arbitrary JSON string; returns `(role, peer)` or `(None, None)` on any bad input and never raises. Used by the orchestrator (plan 01-13) to interpret the target's sync-history.json fetched over SSH without touching the local file (T-01-12-01 mitigation).
- `get_last_sync_state() -> tuple[SyncRole | None, str | None]`: reads the local history file and delegates to `parse_sync_state`. Returns `(None, None)` if the file is missing or unreadable.

Both added to `__all__`. Module docstring updated to describe the `{last_role, last_peer}` schema and reference ADR-015.

### Task 2 — Generation store removed

`UNKNOWN_GENERATION`, `get_target_generation`, and `set_target_generation` deleted entirely. Their entries removed from `__all__`. ADR-015 explicitly forbids per-target btrfs generation markers; the generation store existed only to support the `find-new` content guard removed in plan 01-11.

### Task 3 — Tests reworked

Removed 4 generation-focused test classes: `TestGetTargetGeneration`, `TestSetTargetGeneration`, `TestUnknownGenerationSentinel`, `TestRecordRoleMergePreserving`. Added:
- `TestParseSyncState` (10 cases): valid JSON with/without peer, malformed JSON, non-dict JSON, missing key, invalid role, non-string peer value, empty string.
- `TestGetLastSyncState` (5 cases): missing file, valid file, corrupt file, `__all__` export, round-trip with `record_role`.
- Extended `TestRecordRole` with peer persistence, peer omission, and merge-preserving of unrelated keys.
- Extended `TestGetRecordRoleCommand` with peer inclusion/omission, remote execution writing `last_peer`, and full merge-preserving execution test.

Result: 40 tests, all passing.

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None. The new functions provide full behavior; plan 01-13 will wire real peer values at call sites.

## Threat Flags

No new threat surface beyond what the plan's threat model covers:
- T-01-12-01 (remote-fetched JSON parsed by `parse_sync_state`): mitigated — bare `except Exception` guarantees `(None, None)` on any malformed/hostile input.
- T-01-12-02 (peer hostname injected into remote script): mitigated — `repr()` produces a safe Python string literal.

## Self-Check: PASSED

- src/pcswitcher/sync_history.py: FOUND
- tests/unit/test_sync_history.py: FOUND
- 01-12-SUMMARY.md: FOUND
- Commit 8b45531 (Task 1): FOUND
- Commit 2ad8228 (Task 2): FOUND
- Commit 590962c (Task 3): FOUND
