---
phase: 01-home-sync-mvp-user-data-sync
plan: "07"
subsystem: sync-jobs
tags: [btrfs, divergence-guard, rsync, sync-history, folder-sync]

requires:
  - phase: 01-home-sync-mvp-user-data-sync
    provides: FolderSyncJob with baseline-capture (plans 05-06), config_sync module (plan 08 prerequisite)

provides:
  - UNKNOWN_GENERATION sentinel in sync_history (WR-02 write path and CR-02 read path)
  - CONFIG_REMOTE_DIR / CONFIG_REMOTE_PATH constants in config_sync as single source of truth
  - DivergenceStatus enum (CLEAN/DIVERGED/UNVERIFIABLE) replacing bool return from _target_diverged_since
  - Tool-state filter in _target_diverged_since for empty-prefix subvolume-root syncs (CR-01)
  - Fail-closed divergence guard when subvolume is unverifiable and a baseline exists (CR-02)
  - Non-fatal baseline capture in execute() with UNKNOWN_GENERATION sentinel write (WR-02)
  - Offline unit tests proving all three fixes plus the Codex HIGH #2 non-regression
  - Integration test Step-5 docstring strengthened with explicit out-of-prefix explanation

affects: [folder-sync-integration-tests, config-sync, default-home-config-usability]

tech-stack:
  added: []
  patterns:
    - "Sentinel integer (-1) used to distinguish 'baseline uncertain' from 'never synced' (None) — callers treat them differently: former fail-closed, latter fail-open"
    - "Enum-based state machine return (DivergenceStatus) instead of bool — allows UNVERIFIABLE as a third state without overloading None"
    - "Scoped tool-state filter: logic that applies only to empty-prefix (subvolume root) case is explicitly gated on prefix == '' with inline comment explaining why"

key-files:
  created: []
  modified:
    - src/pcswitcher/sync_history.py
    - src/pcswitcher/config_sync.py
    - src/pcswitcher/jobs/folder_sync.py
    - tests/unit/test_sync_history.py
    - tests/unit/jobs/test_folder_sync.py
    - tests/integration/test_folder_sync.py

key-decisions:
  - "UNKNOWN_GENERATION = -1 stored (not None) so the next run's _check_divergence can distinguish 'baseline uncertain' (fail closed) from 'never synced' (fail open)"
  - "Tool-state filter scoped to prefix == '' only: a pc-switcher-looking path under a non-empty synced root is real user divergence (Codex HIGH #2 respected)"
  - "No fake-tool-state VM test added for CR-01: the filter is scoped to empty prefix, so a write under the non-empty test-dir prefix IS divergence; the offline unit tests are the authoritative CR-01 proof"
  - "UNKNOWN_GENERATION short-circuits _check_divergence before any target query — fail-closed without needing to contact the target"

requirements-completed: [REQ-sync-scope-user-data, REQ-machine-specific-exclusions, REQ-sync-scope-file-metadata, REQ-manual-sync-workflow]

coverage:
  - id: D1
    description: "UNKNOWN_GENERATION sentinel round-trips through set/get_target_generation and is exported in __all__"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/test_sync_history.py#TestUnknownGenerationSentinel::test_unknown_generation_sentinel_round_trips
        status: pass
      - kind: unit
        ref: tests/unit/test_sync_history.py#TestUnknownGenerationSentinel::test_unknown_generation_is_negative_one_and_exported
        status: pass
    human_judgment: false

  - id: D2
    description: "Empty-prefix sync with only .local/share/pc-switcher/ changed is NOT divergence (CR-01 history/state dir)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestDivergenceGuard::test_toolstate_write_under_empty_prefix_not_divergence
        status: pass
    human_judgment: false

  - id: D3
    description: "Empty-prefix sync with only .config/pc-switcher/config.yaml changed is NOT divergence (CR-01 config-sync write)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestDivergenceGuard::test_config_write_under_empty_prefix_not_divergence
        status: pass
    human_judgment: false

  - id: D4
    description: "Non-empty prefix: a .local/share/pc-switcher/ path under the synced prefix IS divergence (Codex HIGH #2)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestDivergenceGuard::test_toolstate_path_under_nonempty_prefix_is_divergence
        status: pass
    human_judgment: false

  - id: D5
    description: "Baseline present + find-new fails → UNVERIFIABLE → blocking ValidationError (CR-02)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestDivergenceGuard::test_unverifiable_with_baseline_fails_closed
        status: pass
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestDivergenceGuard::test_no_btrfs_with_stored_baseline_fails_closed
        status: pass
    human_judgment: false

  - id: D6
    description: "UNVERIFIABLE with allow_divergence or dry_run logs WARNING and does not block"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestDivergenceGuard::test_unverifiable_under_allow_divergence_proceeds
        status: pass
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestDivergenceGuard::test_unverifiable_under_dry_run_proceeds
        status: pass
    human_judgment: false

  - id: D7
    description: "UNKNOWN_GENERATION stored baseline short-circuits without querying target → blocking error (CR-02/WR-02)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestDivergenceGuard::test_unknown_generation_baseline_fails_closed
        status: pass
    human_judgment: false

  - id: D8
    description: "execute() does not raise when post-transfer btrfs generation capture fails; writes UNKNOWN_GENERATION (WR-02)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: unit
        ref: tests/unit/jobs/test_folder_sync.py#TestExecuteNormalMode::test_baseline_capture_failure_records_sentinel_and_does_not_raise
        status: pass
    human_judgment: false

  - id: D9
    description: "Default /home empty-prefix two-consecutive-sync behavior is not blocked by false divergence (MANUAL/LIVE)"
    requirement: REQ-manual-sync-workflow
    verification: []
    human_judgment: true
    rationale: "Requires destructive --delete mirror of real /home on a real machine pair (pc1/pc2). Cannot run in automated CI. Offline proof: D2 and D3 above. VM round-trip test guards the OUT-OF-prefix case via existing prefix-scoping."

duration: 7min
completed: "2026-07-01"
status: complete
---

# Phase 01 Plan 07: Divergence Guard Fix (CR-01, CR-02, WR-02) Summary

**Closed three data-loss-linchpin bugs: false divergence on repeated default /home sync (CR-01), fail-open guard when btrfs is unqueryable (CR-02), and abort-on-successful-sync from baseline-capture failure (WR-02)**

## Performance

- **Duration:** 7 min
- **Started:** 2026-07-01T09:11:54Z
- **Completed:** 2026-07-01T09:25:14Z
- **Tasks:** 3
- **Files modified:** 6

## Accomplishments

- Added `UNKNOWN_GENERATION = -1` sentinel to `sync_history` to distinguish "baseline uncertain" from "never synced", enabling the next run to fail closed rather than silently skipping the divergence guard (WR-02 sentinel write/read)
- Fixed `_target_diverged_since` to return a `DivergenceStatus` enum (CLEAN/DIVERGED/UNVERIFIABLE) and added a scoped tool-state filter for empty-prefix (subvolume-root) syncs — `.local/share/pc-switcher/` and `.config/pc-switcher/` writes no longer trigger false divergence in the default `/home` config (CR-01)
- Made `_check_divergence` fail closed on UNVERIFIABLE (baseline present + btrfs unqueryable) instead of the previous fail-open (CR-02); `UNKNOWN_GENERATION` sentinel short-circuits to UNVERIFIABLE without querying the target
- Made `execute()` non-fatal on baseline-capture failure (RuntimeError or ValueError from `_get_subvolume_generation`): logs WARNING, writes UNKNOWN_GENERATION sentinel, and continues so the successful rsync transfer is not aborted (WR-02)
- Added `CONFIG_REMOTE_DIR` / `CONFIG_REMOTE_PATH` constants to `config_sync` as single source of truth; refactored duplicated literals in `_get_target_config` and `_copy_config_to_target`
- Strengthened `test_round_trip_and_no_false_divergence` Step-5 comment to explicitly document WHY out-of-prefix pc-switcher writes don't false-positive (prefix-scoping, NOT the CR-01 filter)

## Task Commits

1. **Task 1: Add UNKNOWN_GENERATION sentinel to sync_history** - `f1825fb` (feat)
2. **Task 2: Fix the divergence state machine (CR-01 + CR-02 + WR-02)** - `3544f74` (feat)
3. **Task 3: Anchor CR-01 verification, strengthen round-trip guard** - `79bbe32` (docs)
4. **Formatting** - `3b7fd85` (style)

## Files Created/Modified

- `src/pcswitcher/sync_history.py` — added `UNKNOWN_GENERATION = -1` constant and module docstring section
- `src/pcswitcher/config_sync.py` — added `CONFIG_REMOTE_DIR`, `CONFIG_REMOTE_PATH` constants; refactored duplicated literals
- `src/pcswitcher/jobs/folder_sync.py` — `DivergenceStatus` enum; refactored `_target_diverged_since` (returns enum, tool-state filter); updated `_check_divergence` (fail-closed, UNKNOWN_GENERATION short-circuit); updated `execute()` (non-fatal WR-02 sentinel write)
- `tests/unit/test_sync_history.py` — `TestUnknownGenerationSentinel` class with 2 tests
- `tests/unit/jobs/test_folder_sync.py` — 9 new tests covering all three fixes and Codex HIGH #2 regression guard
- `tests/integration/test_folder_sync.py` — Step-5 comment in `test_round_trip_and_no_false_divergence` clarified

## Decisions Made

- **Scoped filter only** — the tool-state filter fires ONLY when `prefix == ""` (subvolume root). For a non-empty synced root, any change under a `.local/share/pc-switcher/` subpath is real user data. This respects the Codex HIGH #2 concern and is documented inline.
- **UNKNOWN_GENERATION short-circuits without target query** — once the sentinel is stored, the guard does not need to contact the target again; it treats the state as UNVERIFIABLE immediately. This is the correct behavior because the baseline is known to be unreliable.
- **No fake-tool-state VM test** — under the scoped filter, a `.local/share/pc-switcher/` write under the non-empty test-dir prefix IS divergence (correct behavior). The offline unit tests are the authoritative CR-01 proof; the VM round-trip test guards the out-of-prefix case.
- **Distinct UNVERIFIABLE message** — the error message for UNVERIFIABLE differs from DIVERGED so operators can distinguish "target modified" from "target state could not be checked."

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Known Stubs

None.

## Threat Surface Scan

No new network endpoints, auth paths, file access patterns, or schema changes introduced beyond the plan's stated scope. The guard change (fail-closed on UNVERIFIABLE) reduces the attack surface for T-07-01 (fail-open data loss path); T-07-02 and T-07-03 are mitigated as designed.

## Integration Verification Note

The integration test `test_round_trip_and_no_false_divergence` is the VM regression guard for the out-of-prefix case. It cannot be run offline. The CR-01 empty-prefix fix is proven by the two offline unit tests (D2, D3 above). The destructive default `/home` behavior (D9) requires a manual/live run on the Hetzner pc1/pc2 pair.

## Self-Check: PASSED

- `f1825fb` exists: `git log --oneline --all | grep f1825fb` ✓
- `3544f74` exists: `git log --oneline --all | grep 3544f74` ✓
- `79bbe32` exists: `git log --oneline --all | grep 79bbe32` ✓
- `src/pcswitcher/sync_history.py` exists and has `UNKNOWN_GENERATION` ✓
- `src/pcswitcher/config_sync.py` has `CONFIG_REMOTE_DIR` ✓
- `src/pcswitcher/jobs/folder_sync.py` has `DivergenceStatus` ✓
- 512 tests passed, 63 deselected ✓

## Next Phase Readiness

Ready for plan 08 (config-sync enhancements), which will use `config_sync.CONFIG_REMOTE_DIR` as its single source of truth for the remote config directory.

---
*Phase: 01-home-sync-mvp-user-data-sync* — *Completed: 2026-07-01*
