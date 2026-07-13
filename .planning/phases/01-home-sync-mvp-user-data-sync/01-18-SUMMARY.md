---
phase: 01-home-sync-mvp-user-data-sync
plan: 18
subsystem: logging
tags: [logging, rich, tui, asyncio, live-display, gap-closure]

requires:
  - phase: 01-home-sync-mvp-user-data-sync (plan 17)
    provides: single rich.live.Live instance with pause()/resume() around confirmation prompts
provides:
  - UILogHandler routing log records into TerminalUI's Recent Logs panel via the event loop
  - TTY-aware setup_logging that picks the UI sink vs. plain stderr StreamHandler by console.is_terminal
  - Orchestrator wiring that creates console/UI/confirmer before setup_logging so the TUI sink can be selected
affects: [phase 01 UAT re-test, any future phase touching src/pcswitcher/logger.py or orchestrator startup ordering]

tech-stack:
  added: []
  patterns:
    - "Cross-thread-to-event-loop handoff: a logging.Handler running on the QueueListener background thread schedules UI mutation via loop.call_soon_threadsafe instead of calling the UI directly, keeping all Live.update calls on one thread"
    - "Interactivity-gated handler selection: setup_logging chooses its TUI-floor handler by console.is_terminal, defaulting to the pre-existing stderr behavior when ui/console are omitted"

key-files:
  created: []
  modified:
    - src/pcswitcher/logger.py
    - src/pcswitcher/orchestrator.py
    - tests/unit/test_logging.py
    - tests/unit/orchestrator/test_logging_system.py

key-decisions:
  - "UILogHandler emits plain text (no ANSI, no Rich markup) — the Recent Logs panel renders strings as-is, so ANSI would show literally and markup would risk injection from arbitrary log message content"
  - "The event loop is captured once at UILogHandler construction via asyncio.get_running_loop(), not re-resolved per emit() — the handler is only ever built from within the running orchestrator loop"
  - "A closed/stopped loop during shutdown is swallowed via self.handleError(record) rather than raised from the QueueListener's background thread"
  - "setup_logging's ui/console params are keyword-only with None defaults, keeping the pre-existing call signature (and its stderr behavior) fully backward compatible for existing callers/tests"

patterns-established:
  - "LogPanelSink Protocol (single add_log_message(message: str) -> None method) decouples logger.py from importing TerminalUI directly"

requirements-completed: [REQ-terminal-ux]

coverage:
  - id: D1
    description: "UILogHandler routes log records into the UI's Recent Logs panel through loop.call_soon_threadsafe, formatting a plain-text line with no ANSI/markup"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/test_logging.py#TestUILogHandlerRouting::test_interactive_setup_routes_to_ui_handler_no_stderr"
        status: pass
    human_judgment: false
  - id: D2
    description: "setup_logging selects UILogHandler when ui is given and console.is_terminal is True; otherwise falls back to the stderr StreamHandler, unchanged for non-interactive/CI runs"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/test_logging.py#TestUILogHandlerRouting::test_non_interactive_setup_falls_back_to_stderr"
        status: pass
      - kind: unit
        ref: "tests/unit/test_logging.py#TestUILogHandlerRouting::test_no_ui_argument_keeps_default_stderr_behavior"
        status: pass
    human_judgment: false
  - id: D3
    description: "Orchestrator creates console/UI/confirmer before calling setup_logging and passes them in, so the TUI sink choice is possible at all"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_logging_system.py#TestOrchestratorCreatesUiBeforeLogging::test_setup_logging_receives_ui_and_console"
        status: pass
    human_judgment: false
  - id: D4
    description: "Manual VM re-test confirming the live-progress flooding (761 duplicate panel headers / 326 duplicate 0% frames observed in UAT) no longer occurs during an interactive dry-run sync"
    verification: []
    human_judgment: true
    rationale: "Requires observing real terminal rendering over an interactive SSH pty on the test VMs; not reproducible in a unit-test harness. Deferred to UAT re-test per the plan's <verification> section."

duration: 10min
completed: 2026-07-04
status: complete
---

# Phase 01 Plan 18: UI-Routed Logging Summary

**Log records now funnel through the single rich.live.Live path via a UILogHandler that hands lines to TerminalUI's Recent Logs panel over the event loop, replacing the uncoordinated stderr StreamHandler that caused the UAT-diagnosed frame-duplication flooding.**

## Performance

- **Duration:** 10 min
- **Started:** 2026-07-03T22:04:00Z (approx, from prior file-read context)
- **Completed:** 2026-07-04T00:14:09+02:00
- **Tasks:** 3
- **Files modified:** 4

## Accomplishments
- Added `UILogHandler` (logging.Handler) that formats records to a compact plain-text line and delivers them to a `LogPanelSink` via `loop.call_soon_threadsafe`, so every terminal write during an interactive sync goes through the same event-loop thread as progress updates
- Made `setup_logging` TTY-aware: it picks `UILogHandler` when both `ui` and a terminal `console` are supplied, otherwise it keeps the pre-existing plain stderr `StreamHandler` — fully backward compatible for existing callers
- Reordered `Orchestrator.run()` so `Console`/`TerminalUI`/`TerminalUIConfirmer` are constructed before `setup_logging` is called, and passed `ui=self._ui, console=self._console` into it
- Added unit tests proving: interactive setup selects `UILogHandler` with no stderr `StreamHandler` present and delivers a plain-text (no-ANSI) line to the sink; non-interactive setup keeps the stderr handler and JSON file output; the orchestrator's `setup_logging` call receives its own `ui`/`console` instances before any sync phase runs

## Task Commits

Each task was committed atomically:

1. **Task 1: Add a UI-routed log handler and make setup_logging TTY-aware** - `5da5673` (feat)
2. **Task 2: Wire the orchestrator to create the UI before logging and pass it in** - `29cf754` (feat)
3. **Task 3: Tests for UI-routed logging and the stderr fallback** - `562090a` (test)

_No TDD gate on this plan (tasks are typed `auto`, not `tdd`); tests were added in Task 3 after the implementation in Tasks 1-2, matching the plan's structure._

## Files Created/Modified
- `src/pcswitcher/logger.py` - Added `LogPanelSink` Protocol and `UILogHandler`; `setup_logging` gained keyword-only `ui`/`console` params and picks the TUI-floor handler by `console.is_terminal`
- `src/pcswitcher/orchestrator.py` - Moved `Console`/`TerminalUI`/`TerminalUIConfirmer` construction above the `setup_logging` call in `run()`; passes `ui`/`console` into `setup_logging`
- `tests/unit/test_logging.py` - Added `TestUILogHandlerRouting` (3 tests) and a `_FakeLogPanelSink` test double
- `tests/unit/orchestrator/test_logging_system.py` - Added `TestOrchestratorCreatesUiBeforeLogging` (1 test) with `mock_config` fixture and `_make_no_op_ui` helper, mirroring the pattern in `test_user_abort.py`

## Decisions Made
- Plain text over Rich markup/ANSI in `UILogHandler.emit()` — the Recent Logs panel renders whatever string it receives; ANSI would show as literal escape bytes, and Rich markup tags would let arbitrary log message content (e.g. a filename) inject console markup (threat T-01-18-02 in the plan's threat model)
- Event loop captured once at `UILogHandler.__init__` via `asyncio.get_running_loop()` rather than per-`emit()` — the handler is always constructed from within the running orchestrator loop (in `run()`, before any `await` that could suspend across a loop boundary)
- `RuntimeError` from `call_soon_threadsafe` on a closed loop is swallowed via `self.handleError(record)` instead of propagating from the QueueListener's background thread, matching the plan's late-shutdown-record guard
- `ui`/`console` are keyword-only with `None` defaults on `setup_logging`, so the existing test suite's `setup_logging(log_path, config)` calls and the two-tuple `(listener, queue)` return shape are untouched

## Deviations from Plan

None - plan executed exactly as written. `tests/unit/orchestrator/test_logging_system.py` had no pre-existing orchestrator-wiring assertions to extend (only `LogLevel`/`generate_log_filename` tests were present), so the new `TestOrchestratorCreatesUiBeforeLogging` class was added following the `test_user_abort.py` mocking pattern the plan's `<read_first>` pointed at, rather than editing an existing test.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All three tasks' automated verification commands pass: `uv run pytest tests/unit/test_logging.py tests/unit/orchestrator/test_logging_system.py tests/unit/orchestrator -q` (116 passed), `uv run basedpyright`, `uv run ruff check .` (both project-wide clean), and the full `uv run pytest tests/unit -q` suite (486 passed)
- This closes UAT gap 4 (live-progress flooding) at the code level. The plan's own `<verification>` section calls for a manual/VM re-test outside this plan (`pc-switcher sync <target> --dry-run` with `dummy_success` active) to confirm the fix on the real test VMs — tracked as coverage item D4 (human judgment) above
- All four gap-closure plans depending on 01-15/01-16/01-17 for this UAT round (01-15..01-18) are now complete; ready to return to Phase 01 UAT re-verification

---
*Phase: 01-home-sync-mvp-user-data-sync*
*Completed: 2026-07-04*

## Self-Check: PASSED

All 5 created/modified files found on disk; all 3 task commits (`5da5673`, `29cf754`, `562090a`) found in git history.
