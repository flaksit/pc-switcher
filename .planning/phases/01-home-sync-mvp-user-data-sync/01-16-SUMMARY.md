---
phase: 01-home-sync-mvp-user-data-sync
plan: 16
subsystem: cli
tags: [exception-handling, logging, orchestrator, cli, ux]

# Dependency graph
requires:
  - phase: 01-home-sync-mvp-user-data-sync
    provides: FirstSyncScope / describe_first_sync_scope and the orchestrator out-of-order check (plan 01-15)
provides:
  - SyncAbortedByUser exception distinguishing a declined confirmation from a genuine failure
  - SessionStatus.ABORTED as a distinct outcome from FAILED/INTERRUPTED
  - Single WARNING-level log for a user abort (never CRITICAL)
  - Single calm CLI message for a user abort (never the red "Sync failed" line)
affects: [cli, orchestrator, logging]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Expected-control-flow exceptions get their own type and their own except clause positioned before the generic except Exception, so they never fall through to the CRITICAL/failure path."

key-files:
  created:
    - tests/unit/orchestrator/test_user_abort.py
  modified:
    - src/pcswitcher/models.py
    - src/pcswitcher/orchestrator.py
    - src/pcswitcher/cli.py
    - tests/unit/cli/test_commands.py

key-decisions:
  - "SyncAbortedByUser is a plain Exception (not a RuntimeError subclass) carrying a human-readable reason, so isinstance checks in run() and the CLI unambiguously distinguish it from every other failure path."
  - "The CLI abort message reuses exit code 1 (same as generic failure) — the distinction the user sees is the calm yellow wording and single line, not a different exit code."

requirements-completed: [REQ-terminal-ux, REQ-manual-sync-workflow]

coverage:
  - id: D1
    description: "Both decline sites (out-of-order/target-state check, config-sync) raise SyncAbortedByUser instead of RuntimeError"
    requirement: "REQ-manual-sync-workflow"
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_user_abort.py::TestConfigSyncDeclineRaisesSyncAbortedByUser::test_config_sync_decline_raises_sync_aborted_by_user"
        status: pass
      - kind: unit
        ref: "tests/unit/orchestrator/test_user_abort.py::TestRunCatchesSyncAbortedByUser::test_out_of_order_decline_logs_warning_never_critical_and_reraises"
        status: pass
    human_judgment: false
  - id: D2
    description: "run() catches SyncAbortedByUser before the generic Exception handler, logs once at WARNING (never CRITICAL), sets SessionStatus.ABORTED, and re-raises"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/orchestrator/test_user_abort.py::TestRunCatchesSyncAbortedByUser::test_out_of_order_decline_logs_warning_never_critical_and_reraises"
        status: pass
    human_judgment: false
  - id: D3
    description: "CLI's _async_run_sync catches SyncAbortedByUser before the generic Exception, prints one calm yellow 'aborted' line (not the red 'Sync failed' line), and returns non-zero"
    requirement: "REQ-terminal-ux"
    verification:
      - kind: unit
        ref: "tests/unit/cli/test_commands.py::TestSyncAbortedByUserHandling::test_user_abort_prints_single_calm_message_and_nonzero_exit"
        status: pass
    human_judgment: false

duration: 8min
completed: 2026-07-04
status: complete
---

# Phase 01 Plan 16: User-decline is now a WARNING-once ABORTED outcome, not a duplicated CRITICAL failure

**Introduced `SyncAbortedByUser` (models.py) so declining a confirmation prompt is logged once at WARNING and surfaced once by the CLI, instead of falling through the generic CRITICAL/"Sync failed" path twice.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-07-03T23:56:35+02:00
- **Completed:** 2026-07-04T00:01:25+02:00
- **Tasks:** 3
- **Files modified:** 5

## Accomplishments
- `SyncAbortedByUser` exception and `SessionStatus.ABORTED` added to models.py, documented as expected control flow that must never be logged at CRITICAL.
- Both decline sites in orchestrator.py (the out-of-order/target-state check and the config-sync confirmation) now raise `SyncAbortedByUser` instead of a plain `RuntimeError`; `run()` catches it before the generic `except Exception`, logging once at WARNING and setting `session.status = SessionStatus.ABORTED`.
- CLI's `_async_run_sync` catches `SyncAbortedByUser` before the generic `except Exception`, printing a single calm yellow "Sync aborted: ..." line instead of the red "Sync failed" line, and still returns a non-zero exit code (1).
- Tests prove: both decline sites raise the new exception type, `run()`'s WARNING/ABORTED handling fires without ever touching CRITICAL, and the CLI prints exactly one calm message with no "failed" text.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add SyncAbortedByUser and route the decline sites through it** - `93530c7` (feat)
2. **Task 2: Make the CLI recognize a user abort and print one calm message** - `3091903` (feat)
3. **Task 3: Tests for the single, non-alarming abort path** - `7a8262c` (test)

**Plan metadata:** (this commit)

## Files Created/Modified
- `src/pcswitcher/models.py` - `SyncAbortedByUser` exception, `SessionStatus.ABORTED`, both added to `__all__`
- `src/pcswitcher/orchestrator.py` - decline sites raise `SyncAbortedByUser`; `run()` has a new `except SyncAbortedByUser` handler before `except Exception`
- `src/pcswitcher/cli.py` - `_async_run_sync` catches `SyncAbortedByUser` before the generic `except Exception`
- `tests/unit/orchestrator/test_user_abort.py` (new) - proves both decline sites raise the new type and `run()`'s handler logs WARNING/never CRITICAL and sets ABORTED
- `tests/unit/cli/test_commands.py` - proves the CLI prints one calm "aborted" message with no "failed" text and a non-zero exit code

## Decisions Made
- `SyncAbortedByUser` is a plain `Exception` (not a `RuntimeError` subclass), so it can never be accidentally caught by a bare `except RuntimeError` elsewhere in the codebase and misrouted back to a failure path.
- The CLI abort message keeps exit code 1 (same numeric value as a generic failure) — callers scripting against exit codes see no behavior change; the fix is purely about log level and message tone, per the plan's explicit note that "the distinction the user sees is the calm wording, not the number."

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

Testing `Orchestrator.run()`'s exception handler required driving the real `run()` method through Phase 1-3 (source lock, SSH connection, target lock) before reaching the out-of-order check where the decline is raised. Resolved by stubbing those three phase methods as no-op `AsyncMock`s and patching `setup_logging`/`TerminalUI` (both otherwise touch real logging infrastructure and a live Rich display) so the test exercises the actual `except SyncAbortedByUser` handler in `run()` without any real I/O. The `SyncSession` instance created inside `run()` is captured via a `side_effect` wrapper on the patched constructor, since the exception's re-raise means the caller never receives the session object back through a normal return.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- UAT gap 2 (duplicate/CRITICAL-logged decline) is closed; the abort path is proven end-to-end from decline site through orchestrator logging to CLI output.
- No blockers for subsequent phase-01 gap-closure plans.

---
*Phase: 01-home-sync-mvp-user-data-sync*
*Completed: 2026-07-04*

## Self-Check: PASSED

All created/modified files verified present on disk; all three task commit hashes (93530c7, 3091903, 7a8262c) verified present in git log.
