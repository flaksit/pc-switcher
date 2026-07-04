---
phase: 01-home-sync-mvp-user-data-sync
fixed_at: 2026-07-04T00:00:00Z
review_path: .planning/phases/01-home-sync-mvp-user-data-sync/01-REVIEW.md
iteration: 1
findings_in_scope: 6
fixed: 6
skipped: 0
status: all_fixed
---

# Phase 01: Code Review Fix Report

- Fixed at: 2026-07-04
- Source review: `.planning/phases/01-home-sync-mvp-user-data-sync/01-REVIEW.md`
- Iteration: 1

## Summary

- Findings in scope: 6 (fix_scope = all)
- Fixed: 6
- Skipped: 0

All fixes verified per finding (re-read + ruff format/check + basedpyright on the touched files). After all commits, the full project gate suite passed (see Gate Results).

## Fixed Issues

### CR-01: Log-panel renders arbitrary log content as Rich markup

Files modified: `src/pcswitcher/ui.py`, `tests/unit/ui/test_terminal_ui.py`

Commit: 235aa3a

Applied fix: In `TerminalUI._render()`, the joined log lines are now wrapped in a `rich.text.Text` object (`Text("\n".join(...))`) instead of being passed to `Panel(...)` as a bare `str`. `Text` renders verbatim and never parses console markup, so log content containing markup-like sequences (rsync deletion paths such as `[/old]`, rsync stderr with `[sender]`) no longer raises `MarkupError` on the Live auto-refresh thread or during `Live.stop()` teardown. The trusted "No logs yet" placeholder is preserved as styled markup via `Text.from_markup(...)`. Added `test_log_panel_renders_markup_like_content_literally`, which drives the real `TerminalUI` render and `stop()` with a markup-breaking `*deleting .../[/old]/cache` line plus a CRITICAL rsync-stderr-style line, asserting no exception and that the literal text appears in the rendered output (the prior `_FakeLogPanelSink`-based tests never rendered, which is why they missed this).

### WR-01: Job-raised `SyncAbortedByUser` mis-logged CRITICAL and recorded FAILED

Files modified: `src/pcswitcher/orchestrator.py`

Commit: 612ca98

Applied fix: Added an `except SyncAbortedByUser: raise` clause ahead of the generic `except Exception as e:` in `_execute_jobs`, so a job-level declined confirmation (e.g. FolderSyncJob's first-sync overwrite gate through the shared confirmer) passes through untouched to `run()`'s single WARNING abort path instead of producing a spurious `JobStatus.FAILED` result plus a duplicate CRITICAL log.

Note: requires human verification — this is a control-flow change on a currently-latent path (no job raises `SyncAbortedByUser` today), so its runtime effect is not exercised by an existing end-to-end job. The exception ordering and the existing `run()`-level handling are confirmed by reading; behavior is validated only at the type/lint/unit level.

### WR-02: `--allow-first-sync` help text hardcodes folder_sync / rsync specifics

Files modified: `src/pcswitcher/cli.py`

Commit: 5d4a7e6

Applied fix: Reworded the flag help to describe the overwrite scope job-agnostically ("everything on the target within the scope of the configured sync jobs will be overwritten, except configured exclusions. Run with --dry-run first to preview."), removing the `folder_sync` / `rsync --delete` coupling that 01-15 had already removed from the orchestrator's first-sync warning.

### WR-03: Config-sync decline prints two conflicting abort messages

Files modified: `src/pcswitcher/config_sync.py`

Commit: 0f7f927

Applied fix: Removed the two in-module red abort prints at the config-sync decline sites (`_handle_no_target_config` and `_handle_config_diff` ABORT), so both now `return False` silently. `_sync_config_to_target` still raises `SyncAbortedByUser`, and the single CLI `except SyncAbortedByUser` handler owns the one user-facing abort line, matching the other decline paths (01-16 single-message contract). Existing config-sync tests use a `MagicMock` console and assert only on the boolean result, so they remain green.

### IN-01: `resume()` did not force an immediate redraw as documented

Files modified: `src/pcswitcher/ui.py`, `tests/unit/ui/test_terminal_ui.py`

Commit: 35eaa8a

Applied fix: `TerminalUI.resume()` now calls `self._live.update(self._render(), refresh=True)` so state mutated while paused is flushed immediately rather than waiting for the 10 Hz auto-refresh tick, honoring the docstring. Tightened `test_resume_forces_redraw_of_state_mutated_while_paused` to truncate the output buffer immediately before `resume()` and assert the new state appears with no intervening `sleep`, so the test now actually verifies the forced redraw instead of passing via auto-refresh.

### IN-02: Split interactivity detection between logging setup and the confirmer

Files modified: `src/pcswitcher/logger.py`, `src/pcswitcher/confirmer.py`, `tests/unit/test_logging.py`

Commit: 70dec56

Applied fix: Added `is_interactive(console) -> bool` in `logger.py`, returning `console.is_terminal and sys.stdin.isatty()` (both stdout and stdin must be TTYs), with a docstring explaining the mixed-redirection rationale. `setup_logging` now selects the UI handler via `is_interactive(console)`, and `TerminalUIConfirmer.confirm` decides interactive-vs-flag via `is_interactive(self._console)` (replacing the bare `sys.stdin.isatty()`), so UI routing and prompt interactivity always agree. Updated the interactive logging routing test to patch `sys.stdin.isatty` to `True` (pytest's stdin is not a TTY), reflecting that interactivity now requires both ends.

## Gate Results

Run after all six commits, on branch `01-folder-sync`:

- `uv run ruff format --check .` — 85 files already formatted (pass)
- `uv run ruff check .` — All checks passed
- `uv run basedpyright` — 0 errors, 0 warnings, 0 notes
- `uv run codespell` — no findings
- `uv run pytest` — 559 passed, 61 deselected (integration tests, run separately via `tests/run-integration-tests.sh`)

---

- _Fixed: 2026-07-04_
- _Fixer: Claude (gsd-code-fixer)_
- _Iteration: 1_
