---
phase: 01-home-sync-mvp-user-data-sync
plan: "08"
subsystem: ui
tags: [asyncio, sigint, progress-bar, terminal-ui, rich]

requires: []
provides:
  - "TerminalUI.set_total_steps() setter for mid-run total correction"
  - "Honest SIGINT interrupt messaging without false numeric grace-period promise"
  - "Progress bar reaches 100% under default config (enabled-only job count)"
affects:
  - "orchestrator: progress display accuracy"
  - "cli: SIGINT UX"

tech-stack:
  added: []
  patterns:
    - "set_total_steps mirrors set_current_step: assign + live.update(render()) for immediate reflection"
    - "Orchestrator uses initial estimate from enabled jobs, then corrects via set_total_steps after Phase 4 discovery"

key-files:
  created: []
  modified:
    - src/pcswitcher/cli.py
    - src/pcswitcher/orchestrator.py
    - src/pcswitcher/ui.py
    - tests/unit/ui/test_terminal_ui.py

key-decisions:
  - "IN-01: Remove asyncio.wait_for(asyncio.shield(asyncio.sleep(0))) entirely — it returned immediately so cleanup time was zero; first-SIGINT message now says 'force quit immediately' without a numeric grace-period claim"
  - "IN-02: Two-phase total_steps approach — initial estimate counts enabled jobs, set_total_steps() corrects after Phase 4 discovery to match exactly what will run"

patterns-established:
  - "set_total_steps: mirrors set_current_step pattern (assign + conditional live.update) to ensure live render refreshes immediately on correction"

requirements-completed: [REQ-terminal-ux]

coverage:
  - id: D1
    description: "SIGINT cleanup path has no dead asyncio.wait_for/shield/sleep(0) construct and first-SIGINT message is truthful (IN-01 closed)"
    requirement: REQ-terminal-ux
    verification:
      - kind: unit
        ref: tests/unit/orchestrator/test_interrupt_handling.py#TestInterruptHandling::test_core_fr_sigint
        status: pass
      - kind: unit
        ref: tests/unit/orchestrator/test_interrupt_handling.py#TestInterruptHandling::test_core_fr_term_ctrlc
        status: pass
      - kind: unit
        ref: tests/unit/orchestrator/test_interrupt_handling.py#TestInterruptHandling::test_core_us_interrupt_as2
        status: pass
    human_judgment: false
  - id: D2
    description: "TerminalUI.set_total_steps() updates _total_steps and refreshes live render; Orchestrator sets correct total after Phase 4 job discovery (IN-02 closed)"
    requirement: REQ-terminal-ux
    verification:
      - kind: unit
        ref: tests/unit/ui/test_terminal_ui.py#test_set_total_steps_updates_total
        status: pass
      - kind: unit
        ref: tests/unit/ui/test_terminal_ui.py#test_core_us_tui_as2_multi_job_progress
        status: pass
    human_judgment: false

duration: 3min
completed: 2026-07-01
status: complete
---

# Phase 01 Plan 08: Terminal UX correctness — honest SIGINT messaging and 100% progress bar Summary

**Removed dead asyncio.wait_for no-op from SIGINT path and added TerminalUI.set_total_steps() so the progress bar reaches 100% under the default config**

## Performance

- **Duration:** 3 min
- **Started:** 2026-07-01T09:30:41Z
- **Completed:** 2026-07-01T09:33:46Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- IN-01 closed: deleted the `asyncio.wait_for(asyncio.shield(asyncio.sleep(0)), timeout=CLEANUP_TIMEOUT_SECONDS)` construct which returned immediately (sleep(0) completes in the same tick), making its `TimeoutError` branch unreachable; removed the `CLEANUP_TIMEOUT_SECONDS` constant and updated the first-SIGINT message to "Press Ctrl+C again to force quit immediately" without asserting a grace period the code does not implement
- IN-02 closed: added `TerminalUI.set_total_steps(total: int)` mirroring `set_current_step` (assigns `_total_steps`, refreshes live render when `_live` is active); Orchestrator now counts only enabled jobs in the initial estimate and calls `set_total_steps(8 + len(jobs) + 1)` after Phase 4 discovery so the denominator exactly matches the final `set_current_step` value
- Added `test_set_total_steps_updates_total` verifying the stored value and live render refresh for both inactive and active live display paths

## Task Commits

1. **Task 1: Remove dead SIGINT cleanup wait (IN-01)** - `89743a4` (fix)
2. **Task 2: Progress bar 100% with set_total_steps (IN-02)** - `de79791` (feat)
3. **Ruff format fix on cli.py** - `239a065` (style)

## Files Created/Modified

- `src/pcswitcher/cli.py` - removed CLEANUP_TIMEOUT_SECONDS constant, dead wait_for/shield/sleep(0) block, and unreachable TimeoutError branch; updated first-SIGINT message and docstring
- `src/pcswitcher/orchestrator.py` - initial total_steps counts only enabled jobs; calls set_total_steps after Phase 4 discovery
- `src/pcswitcher/ui.py` - added set_total_steps() setter after set_current_step()
- `tests/unit/ui/test_terminal_ui.py` - added test_set_total_steps_updates_total

## Decisions Made

- `CLEANUP_TIMEOUT_SECONDS` deleted entirely: was only referenced in the dead wait_for construct and the first-SIGINT message. Removing the timeout claim from the message made the constant unused; a dead constant is misleading documentation so it was removed (plan explicitly called for this).
- Two-phase total approach (initial estimate + correction) rather than deferring TerminalUI construction until after Phase 4: the display must start before Phase 4 to show progress during phases 1-3.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

Ruff format required an additional whitespace normalization commit on `cli.py` after the dead-code removal changed the block structure slightly. No logic change.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All INFO-level correctness/UX findings (IN-01, IN-02) are closed
- Phase 01 plan 09 is the final plan in the phase

## Self-Check: PASSED

- `src/pcswitcher/cli.py` exists on disk
- `src/pcswitcher/orchestrator.py` exists on disk
- `src/pcswitcher/ui.py` exists on disk
- `tests/unit/ui/test_terminal_ui.py` exists on disk
- Task commits exist: 89743a4, de79791, 239a065

---
*Phase: 01-home-sync-mvp-user-data-sync — Completed: 2026-07-01*
