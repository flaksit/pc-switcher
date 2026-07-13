---
phase: 01-home-sync-mvp-user-data-sync
plan: "09"
subsystem: sync-jobs
tags: [rsync, folder-sync, progress-parsing, divergence-guard, toctou]

requires:
  - phase: 01-home-sync-mvp-user-data-sync
    provides: "01-07's broadened+scoped tool-state filter (empty-prefix CR-01) and _check_divergence returning ValidationError|None"

provides:
  - "_parse_size_to_bytes() static method converting rsync size tokens to integer bytes"
  - "Updated _PROGRESS2_RE with capturing group 1 for the size token"
  - "_stream_rsync returning real bytes_transferred from last progress2 line (WR-01)"
  - "_stream_rsync recognising 'c' (created) and 'h' (hard link) change types for FULL logging (IN-03)"
  - "execute() pre-transfer divergence re-check calling _check_divergence(folder) before any --delete runs (WR-03)"
  - "Unit tests: TestStreamRsync + 3 new, TestExecuteDivergenceRecheck with 4 tests covering WR-03 + Codex HIGH #1"

affects: [folder-sync-integration-tests, audit-trail, data-loss-guard]

tech-stack:
  added: []
  patterns:
    - "Last-progress-line-wins for bytes_transferred: rsync progress2 emits running totals so the final captured value is the best approximation of cumulative bytes"
    - "Pre-transfer divergence re-check delegates all override logic to _check_divergence: execute() does not duplicate dry_run/allow_divergence semantics"

key-files:
  created: []
  modified:
    - src/pcswitcher/jobs/folder_sync.py
    - tests/unit/jobs/test_folder_sync.py

key-decisions:
  - "Last-progress-line-wins for bytes_transferred: best-effort cumulative, consistent with existing docstring and rsync's running-total semantics"
  - "Change-type set extended inline with comment naming 'c' and 'h' so the set is self-documenting for future readers"
  - "Pre-transfer re-check raises RuntimeError (not ValidationError) because execute() is post-validate; the CRITICAL log + raise abort the per-folder transfer before any --delete runs"
  - "execute() re-check does NOT duplicate override logic — delegates entirely to _check_divergence which already returns None under dry_run/allow_divergence"

requirements-completed: [REQ-manual-sync-workflow, REQ-sync-scope-user-data, REQ-terminal-ux]

coverage:
  - id: D1
    description: "Per-folder INFO summary reports real transferred bytes parsed from rsync --info=progress2 (WR-01 closed)"
    requirement: REQ-terminal-ux
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestStreamRsync::test_progress_line_reports_transferred_bytes
        status: pass
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestStreamRsync::test_parse_size_to_bytes_units
        status: pass
    human_judgment: false

  - id: D2
    description: "FULL logs include 'c' (created) and 'h' (hard link) rsync itemize change types (IN-03 closed)"
    requirement: REQ-terminal-ux
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestStreamRsync::test_created_and_hardlink_change_types_logged_at_full
        status: pass
    human_judgment: false

  - id: D3
    description: "execute() re-checks divergence before spawning rsync; blocking result raises before any --delete (WR-03 closed)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestExecuteDivergenceRecheck::test_recheck_blocks_destructive_rsync_when_target_diverged
        status: pass
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestExecuteDivergenceRecheck::test_recheck_allows_when_clean
        status: pass
    human_judgment: false

  - id: D4
    description: "dry_run and allow_divergence paths still proceed through execute() re-check (override semantics preserved)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestExecuteDivergenceRecheck::test_recheck_does_not_block_under_allow_divergence
        status: pass
    human_judgment: false

  - id: D5
    description: "Phase-8 config.yaml write for default /home (empty prefix) does NOT block execute() re-check (Codex HIGH #1 closed)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestExecuteDivergenceRecheck::test_recheck_ignores_phase8_config_write_for_empty_prefix
        status: pass
    human_judgment: false

  - id: D6
    description: "End-to-end behavioral proof: real bytes in INFO logs, all change types captured, re-check on live VM (MANUAL/LIVE)"
    requirement: REQ-sync-scope-user-data
    verification: []
    human_judgment: true
    rationale: "Requires a real rsync run between pc1/pc2. Offline proofs: D1-D5 above. VM behavioral check shared with 01-07 integration suite."

duration: 4min
completed: "2026-07-01"
status: complete
---

# Phase 01 Plan 09: Folder Sync Observability and TOCTOU Fix (WR-01, IN-03, WR-03) Summary

**Real transferred-byte reporting via _parse_size_to_bytes, complete rsync itemize coverage (c/h change types), and a pre-transfer divergence re-check in execute() that gates --delete on a fresh divergence check taken immediately before the destructive transfer**

## Performance

- **Duration:** 4 min
- **Started:** 2026-07-01T09:39:35Z
- **Completed:** 2026-07-01T09:43:39Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Fixed `bytes_transferred` always being 0: added capturing group to `_PROGRESS2_RE` and `_parse_size_to_bytes()` static method; `_stream_rsync` now sets `bytes_xfr` from the last progress2 size token (last-line-wins, best-effort cumulative) — the per-folder INFO summary now reports a real byte count (WR-01)
- Extended `_stream_rsync` change-type recognition from `>/</*./.` to also include `c` (created dirs/symlinks/devices) and `h` (hard links), with inline comments naming both types so the set is self-documenting (IN-03)
- Added pre-transfer divergence re-check in `execute()`: calls `_check_divergence(folder)` before `_build_rsync_cmd`/`start_process` for each folder; a non-None result logs CRITICAL and raises RuntimeError, aborting before any `--delete` runs; delegates all override logic (dry_run/allow_divergence) to `_check_divergence` unchanged (WR-03 / T-09-01)
- Added 7 new unit tests: 3 in `TestStreamRsync` (bytes parsing, unit conversions, c/h change types) and 4 in `TestExecuteDivergenceRecheck` (blocking, clean, allow_divergence, Phase-8 config.yaml interaction)

## Task Commits

1. **Task 1 RED: failing tests for WR-01+IN-03** - `2f20605` (test)
2. **Task 1 GREEN: _parse_size_to_bytes + c/h recognition** - `accfef1` (feat)
3. **Task 2 RED: failing test for WR-03 re-check** - `f2f4e62` (test)
4. **Task 2 GREEN: pre-transfer divergence re-check in execute()** - `62f478e` (feat)
5. **Formatting** - `51087b5` (style)

## Files Created/Modified

- `src/pcswitcher/jobs/folder_sync.py` — `_PROGRESS2_RE` size capture group; `_parse_size_to_bytes` static method; `_stream_rsync` `bytes_xfr` assignment + `c`/`h` change types; `execute()` pre-transfer re-check
- `tests/unit/jobs/test_folder_sync.py` — `TestStreamRsync`: 3 new tests; `TestExecuteDivergenceRecheck`: 4 new tests

## Decisions Made

- **Last-progress-line-wins for bytes_transferred:** rsync `--info=progress2` emits running totals on each carriage-return line; the final captured value is the closest approximation to the true cumulative byte count. Documented as best-effort in the existing docstring.
- **Change-type set extended inline with comment:** Added `c` and `h` alongside the existing characters, naming what each means so the set is self-documenting for readers unfamiliar with rsync `%i` semantics.
- **RuntimeError (not ValidationError) for execute() re-check abort:** `execute()` runs post-validate; `ValidationError` is the return type of `validate()`. Raising `RuntimeError` is consistent with existing rsync-failure handling in the same method.
- **No duplication of override logic:** `execute()` delegates entirely to `_check_divergence()` which already encodes dry_run/allow_divergence semantics. A non-None result always means "block" in the execute context.

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Known Stubs

None.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced. The re-check reuses the existing `_check_divergence` call (same SSH commands, same scope). T-09-01 (TOCTOU window) is now mitigated as designed; T-09-02 (regex DoS) remains accepted (rsync stdout is not attacker-controlled); T-09-03 (misleading audit log) is mitigated by D1 and D2 above.

## Self-Check: PASSED

- `2f20605` (test RED task 1): `git log --oneline --all | grep 2f20605` ✓
- `accfef1` (feat GREEN task 1): `git log --oneline --all | grep accfef1` ✓
- `f2f4e62` (test RED task 2): `git log --oneline --all | grep f2f4e62` ✓
- `62f478e` (feat GREEN task 2): `git log --oneline --all | grep 62f478e` ✓
- `src/pcswitcher/jobs/folder_sync.py` has `_parse_size_to_bytes` ✓
- `src/pcswitcher/jobs/folder_sync.py` has `c`, `h` in change-type set ✓
- `src/pcswitcher/jobs/folder_sync.py` has `recheck_error = await self._check_divergence(folder)` ✓
- 520 tests passed, 63 deselected ✓

## TDD Gate Compliance

- RED gate (test commit): `2f20605` and `f2f4e62` ✓
- GREEN gate (feat commit): `accfef1` and `62f478e` ✓

## Next Phase Readiness

Phase 01 complete — all 9 plans executed. Ready for /gsd-verify-work phase 01 or /gsd-complete-milestone.

---
*Phase: 01-home-sync-mvp-user-data-sync* — *Completed: 2026-07-01*
