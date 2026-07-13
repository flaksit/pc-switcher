---
phase: 01-home-sync-mvp-user-data-sync
plan: 17
subsystem: ui
tags: [rich, terminal-ui, confirmer, config-sync, dry-run]

# Dependency graph
requires:
  - phase: 01-home-sync-mvp-user-data-sync
    provides: TerminalUI live display, TerminalUIConfirmer confirmation gate, config_sync interactive prompts
provides:
  - Single persistent rich.live.Live instance across the whole sync run, paused/resumed (not rebuilt) around confirmation prompts
  - config_sync dry-run parity with the orchestrator's other confirmations (no prompt, read-only diff preview, no write)
affects: [orchestrator, folder_sync, config_sync, ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "PausableUI Protocol exposes pause()/resume() instead of start()/stop() for confirmation callers; TerminalUI keeps start()/stop() only for orchestrator create/teardown lifecycle"
    - "resume() forces an immediate self._live.update(self._render()) so state mutated while paused (guarded by is_started) renders on the very next frame"

key-files:
  created: []
  modified:
    - src/pcswitcher/ui.py
    - src/pcswitcher/confirmer.py
    - src/pcswitcher/config_sync.py
    - tests/unit/ui/test_terminal_ui.py
    - tests/unit/test_confirmer.py
    - tests/unit/orchestrator/test_consecutive_sync.py
    - tests/unit/cli/test_config_sync.py

key-decisions:
  - "TerminalUI.start() now constructs the Live only when self._live is None; a resume via start() (or the new resume()) reuses the existing instance instead of stacking a fresh Live region"
  - "stop() remains the sole teardown path (nulls self._live); pause()/resume() never discard the instance"
  - "sync_config_to_target computes should_pause = ui is not None and not auto_accept and not dry_run once, and pairs the finally-block resume with that same flag, fixing a latent bug where the old code called ui.start() unconditionally even when auto_accept meant it was never paused"
  - "_handle_config_diff's diff rendering was factored into _display_config_diff so the dry-run read-only preview and the interactive _prompt_config_diff share one implementation"

requirements-completed: [REQ-terminal-ux, REQ-manual-sync-workflow]

coverage:
  - id: D1
    description: "TerminalUI keeps a single Live instance across confirmation pause/resume; resume() forces an immediate redraw of state mutated while paused"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/ui/test_terminal_ui.py#test_pause_resume_reuses_same_live_instance"
        status: pass
      - kind: unit
        ref: "tests/unit/ui/test_terminal_ui.py#test_resume_forces_redraw_of_state_mutated_while_paused"
        status: pass
    human_judgment: false
  - id: D2
    description: "Confirmer and orchestrator confirmation paths use ui.pause()/ui.resume() instead of ui.stop()/ui.start()"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/test_confirmer.py::TestInteractive"
        status: pass
      - kind: unit
        ref: "tests/unit/orchestrator/test_consecutive_sync.py"
        status: pass
    human_judgment: false
  - id: D3
    description: "config sync under --dry-run does not prompt interactively; it shows a read-only preview (including the diff when configs differ) and proceeds without pausing the UI or writing"
    requirement: "REQ-manual-sync-workflow"
    verification:
      - kind: unit
        ref: "tests/unit/cli/test_config_sync.py::TestDryRunSkipsPrompting"
        status: pass
    human_judgment: false

duration: 8min
completed: 2026-07-03
status: complete
---

# Phase 01 Plan 17: Confirmation-Flow Gap Closure Summary

**Single persistent rich.live.Live with pause()/resume() replaces the stop()/start() rebuild-on-every-prompt pattern, and config_sync now matches the orchestrator's dry-run-never-prompts contract.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-07-03T23:43:18+02:00
- **Completed:** 2026-07-03T23:51:26+02:00
- **Tasks:** 3
- **Files modified:** 7

## Accomplishments

- `TerminalUI.start()` builds the `Live` instance only once; `pause()`/`resume()` stop/restart that same instance instead of a confirmation prompt tearing it down and a fresh `Live` region getting stacked on top with a skipped step number. `resume()` forces an immediate `self._live.update(self._render())` so state mutated while paused renders on the next frame rather than waiting for an unrelated future update.
- The five UI mutator methods (`update_job_progress`, `add_log_message`, `set_connection_status`, `set_current_step`, `set_total_steps`) now guard on `self._live is not None and self._live.is_started` — writes made while paused are stored but not sent to the terminal, then flushed by `resume()`'s forced redraw.
- `PausableUI` (confirmer.py) and `TerminalUIConfirmer.confirm` switched from `start()`/`stop()` to `pause()`/`resume()`; the orchestrator's initial `start()` and final teardown `stop()` are untouched.
- `config_sync.sync_config_to_target` now pauses/resumes the same single `Live` and — mirroring `_confirm_first_sync`'s existing dry-run short-circuit — skips pausing entirely under `--dry-run`. `_handle_no_target_config` and `_handle_config_diff` each short-circuit under `dry_run` before reaching their respective prompt functions, printing a read-only preview (including the unified diff for the "configs differ" case, via the new shared `_display_config_diff` helper) and returning `True` without writing.

## Task Commits

Each task was committed atomically:

1. **Task 1: Make TerminalUI hold one persistent Live with pause/resume** - `334c1ef` (fix)
2. **Task 2: config_sync uses pause/resume and skips prompting under --dry-run** - `56f13ad` (fix)
3. **Task 3: Tests for single-Live pause/resume and dry-run config sync** - `6b25e59` (test)

_No TDD gate applies — plan `type: execute`, not `type: tdd`._

## Files Created/Modified

- `src/pcswitcher/ui.py` - `start()` builds the `Live` once; added `pause()`/`resume()`; `stop()` remains final teardown; five update methods guard on `is_started`
- `src/pcswitcher/confirmer.py` - `PausableUI` Protocol declares `pause()`/`resume()`; `TerminalUIConfirmer.confirm` calls them around the blocking prompt
- `src/pcswitcher/config_sync.py` - `sync_config_to_target` pauses/resumes conditionally (never under dry-run); `_handle_no_target_config`/`_handle_config_diff` skip prompting and log a preview under `dry_run`; new `_display_config_diff` helper shared by the interactive prompt and the dry-run preview
- `tests/unit/ui/test_terminal_ui.py` - two new tests proving Live-instance reuse across pause/resume and that `resume()` itself forces the redraw of state mutated while paused
- `tests/unit/test_confirmer.py` - interactive-path assertions updated from `ui.stop`/`ui.start` to `ui.pause`/`ui.resume`
- `tests/unit/orchestrator/test_consecutive_sync.py` - every confirmer-path assertion (`assert_called_once`/`assert_not_called`) updated from `stop`/`start` to `pause`/`resume`
- `tests/unit/cli/test_config_sync.py` - existing pause/resume assertions renamed; new `TestDryRunSkipsPrompting` class covers `_handle_no_target_config`, `_handle_config_diff`, and `sync_config_to_target` under `dry_run=True`

## Decisions Made

- `sync_config_to_target` now computes `should_pause` once and pairs the `finally`-block resume with that same condition, rather than resuming unconditionally as the old code did — this incidentally fixes a latent bug where `ui.start()` was called even when the UI was never paused (`auto_accept=True` path).
- The diff-rendering block inside `_prompt_config_diff` was extracted into `_display_config_diff(console, diff)` so the new dry-run preview path (which must show the diff without prompting) reuses the exact same rendering rather than duplicating it.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Both UAT gaps (3: stale duplicate TUI frames on confirmation resume; 5: config sync prompting under --dry-run) are closed and covered by unit tests.
- `uv run pytest` (full suite: 551 passed), `uv run ruff check .`, and `uv run basedpyright` are all clean.
- No blockers for subsequent phase-01 gap-closure or milestone-close work.

---
*Phase: 01-home-sync-mvp-user-data-sync*
*Completed: 2026-07-03*

## Self-Check: PASSED

All 7 files_modified paths verified present on disk; all 3 task commit hashes (334c1ef, 56f13ad, 6b25e59) verified present in git log.
