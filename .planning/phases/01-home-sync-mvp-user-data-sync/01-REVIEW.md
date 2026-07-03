---
phase: 01-home-sync-mvp-user-data-sync
reviewed: 2026-07-04T00:00:00Z
depth: standard
files_reviewed: 19
files_reviewed_list:
  - src/pcswitcher/cli.py
  - src/pcswitcher/config_sync.py
  - src/pcswitcher/confirmer.py
  - src/pcswitcher/jobs/base.py
  - src/pcswitcher/jobs/folder_sync.py
  - src/pcswitcher/logger.py
  - src/pcswitcher/models.py
  - src/pcswitcher/orchestrator.py
  - src/pcswitcher/ui.py
  - tests/unit/cli/test_commands.py
  - tests/unit/cli/test_config_sync.py
  - tests/unit/jobs/test_folder_sync.py
  - tests/unit/orchestrator/test_consecutive_sync.py
  - tests/unit/orchestrator/test_first_sync_scope.py
  - tests/unit/orchestrator/test_logging_system.py
  - tests/unit/orchestrator/test_user_abort.py
  - tests/unit/test_confirmer.py
  - tests/unit/test_logging.py
  - tests/unit/ui/test_terminal_ui.py
findings:
  critical: 1
  warning: 3
  info: 2
  total: 6
status: issues_found
---

# Phase 01: Code Review Report

Reviewed 2026-07-04, standard depth, 19 files. Status: issues_found.

## Narrative Findings (AI reviewer)

### Summary

Reviewed the four UAT gap-closure changes since `dd29e1e`: job-agnostic first-sync warning (01-15), `SyncAbortedByUser` distinct decline outcome (01-16), single-`rich.live.Live` pause/resume plus dry-run-consistent config prompting (01-17), and UI-routed logging via `UILogHandler` (01-18).

The `SyncAbortedByUser` exception ordering is correct in both `Orchestrator.run()` (caught before the generic `except Exception`, logged once at WARNING, re-raised) and the CLI (`except SyncAbortedByUser` before `except Exception`, single calm message). The `describe_first_sync_scope` composition is genuinely job-agnostic in the orchestrator's warning body. The single-`Live` pause/resume lifecycle is idempotent (`Live.start`/`stop` are guarded by `_started`) and the `is_started` guards correctly prevent re-entrancy into a stopped Live. `UILogHandler` correctly captures the running loop once and routes through `call_soon_threadsafe`, with a working stderr fallback for non-TTY runs.

However, the 01-18 UI-routing feature introduces a reproducible crash: arbitrary log content is rendered as Rich console markup by the Recent Logs `Panel`, so realistic message text (rsync file paths, rsync stderr, any string containing `[/...]`) raises `MarkupError` at render time. That is the one BLOCKER. Three WARNING-level control-flow/messaging gaps and two INFO items follow.

## Critical Issues

### CR-01: Log-panel renders arbitrary log content as Rich markup — `MarkupError` crashes the live display and teardown

**File:** `src/pcswitcher/ui.py:104-110` (`_render`), enabled by `src/pcswitcher/logger.py:248-278` (`UILogHandler.emit` / `_format_line`)

**Issue:** `UILogHandler` (01-18) now feeds every log record at/above the `tui` floor into `TerminalUI.add_log_message`, which appends the raw string to `self._log_panel`. `_render()` then does:

```python
log_text = "\n".join(self._log_panel) if self._log_panel else "[dim]No logs yet[/dim]"
log_panel = Panel(log_text, title="Recent Logs", ...)
```

Passing a bare `str` to `Panel(...)` makes Rich interpret console markup in that string (the `[dim]No logs yet[/dim]` fallback relies on exactly this). Log content is not markup-safe: `[token]` sequences are silently swallowed (verified: a line containing `[orchestrator]` renders with that token deleted), and `[/...]` sequences raise `rich.errors.MarkupError` (verified: `... *deleting home/user/[/old]/cache` raises `MarkupError: closing tag '[/old]' ... doesn't match any open tag`).

Rendering happens on the Live background auto-refresh thread and again during `Live.stop()` (`transient=False` forces a final frame). I reproduced the teardown crash end-to-end through the real `TerminalUI`: `ui.stop()` — which the orchestrator calls from `_cleanup()` inside `run()`'s `finally` block (`orchestrator.py:998-999`) — raised `MarkupError` on the bracketed line above. Consequences on any sync whose logs contain such content: (1) the auto-refresh thread dies mid-sync, freezing the live display; (2) `_cleanup()`'s `self._ui.stop()` raises during the `finally`, masking the real sync result with a `MarkupError` traceback.

Untrusted/arbitrary text reaches this path routinely: rsync stderr on failure (`folder_sync.py:454`, logged CRITICAL — reaches the panel at every `tui` level), the folder-sync summary/`INFO` lines embedding `folder.path!r`, and — when `tui` is set to `FULL`/`DEBUG` — every per-file rsync path (`folder_sync.py:400,408`). The `UILogHandler` docstring claims to defend against this ("Rich markup would risk markup-injection from arbitrary message content"), but the defense was applied to the wrong layer: it avoids emitting markup in the format prefix while the message body and the Panel render still interpret markup. Existing tests miss it because `_FakeLogPanelSink` (`test_logging.py:33-43`) records to a list and never renders, and the `TerminalUI` tests never push bracketed content through a Panel.

**Fix:** Render the panel body as markup-disabled text instead of letting Rich parse it. Either wrap the joined lines in a `Text` object:

```python
from rich.text import Text
...
if self._log_panel:
    log_body: RenderableType = Text("\n".join(self._log_panel))
else:
    log_body = Text.from_markup("[dim]No logs yet[/dim]")
log_panel = Panel(log_body, title="Recent Logs", border_style="blue", height=self._max_log_lines + 2)
```

or escape at ingestion in `add_log_message` (and in `UILogHandler._format_line`) via `rich.markup.escape(message)`. Add a regression test that pushes a line containing `[/x]` through the real `TerminalUI` render and calls `stop()` without raising.

## Warnings

### WR-01: `SyncAbortedByUser` raised inside a sync job is mis-logged CRITICAL and recorded as FAILED

**File:** `src/pcswitcher/orchestrator.py:934-951`

**Issue:** `JobContext` is constructed with `confirmer=self._confirmer` (`orchestrator.py:169`) and the confirmer is explicitly described as the "Shared interactive confirmation gate for the orchestrator's out-of-order check *and any job-level prompt* (e.g. FolderSyncJob first-sync overwrite)" (`orchestrator.py:220-221`). If any job uses that confirmer and raises `SyncAbortedByUser` on decline, `_execute_jobs`'s `except Exception as e` (line 934) catches it first: it records a `JobStatus.FAILED` result and logs `self._logger.critical("Job %s failed: %s", ...)` (line 945) before re-raising. `run()` then also logs it at WARNING. The result is a double log (one CRITICAL, one WARNING) plus a FAILED job/session status for what the abort contract (`models.py:125-133`) says must be "reported once, at WARNING." No job triggers this today, so it is latent — but the injected confirmer and its documented intent make it a realistic near-term trap.

**Fix:** In `_execute_jobs`, let user-abort pass through untouched:

```python
except SyncAbortedByUser:
    raise
except Exception as e:
    ...  # existing FAILED-result + CRITICAL path
```

### WR-02: CLI `--allow-first-sync` help text hardcodes folder_sync / rsync specifics, defeating the job-agnostic warning goal

**File:** `src/pcswitcher/cli.py:214-224`

**Issue:** 01-15 made the orchestrator's first-sync warning job-agnostic (`_confirm_first_sync` composes scope from each job's `describe_first_sync_scope`, and the body no longer names rsync). But the user-facing flag help still reads: "every folder configured for **folder_sync** will be overwritten on the target (**rsync --delete**), except configured exclusions." This re-introduces exactly the per-job coupling 01-15 removed and will be wrong the moment a non-folder job (or a job with a different mechanism) participates in a first sync.

**Fix:** Reword generically, e.g. "Proceed with a first-ever sync without interactive confirmation. WARNING: everything on the target within the scope of the configured sync jobs will be overwritten, except configured exclusions. Run with `--dry-run` first to preview."

### WR-03: Config-sync decline prints two conflicting abort messages, inconsistent with the single-message contract

**File:** `src/pcswitcher/config_sync.py:217,257` together with `src/pcswitcher/cli.py:349-354`

**Issue:** 01-16's goal was a single, non-alarming abort surface. For the out-of-order / first-sync decline paths that holds (the confirmer prints nothing on decline; only the CLI prints one yellow "Sync aborted:" line). But the config-sync decline path prints its own message inside `config_sync.py` — `"[red]Sync aborted: configuration required on target.[/red]"` (line 217) or `"[red]Sync aborted by user.[/red]"` (line 257) — and then `_sync_config_to_target` raises `SyncAbortedByUser`, so the CLI prints a *second*, differently-colored `"[yellow]Sync aborted:[/yellow] Config sync aborted by user"`. The user sees two abort lines with inconsistent wording/severity, and this path diverges from the other decline paths.

**Fix:** Remove the console prints at the config-sync decline sites (return `False` silently) and let the single CLI `except SyncAbortedByUser` handler own the user-facing message; or route config-sync decline through the same confirmer/message pathway so exactly one line is emitted.

## Info

### IN-01: `resume()` does not actually force an immediate redraw as its docstring claims

**File:** `src/pcswitcher/ui.py:139-150`

**Issue:** The docstring says resume "must redraw right away," but `self._live.update(self._render())` is called without `refresh=True`, so it only stores the renderable; the visible redraw is deferred to the 10 Hz auto-refresh thread. The behavior is functionally fine only because `auto_refresh` is always on. The test `test_resume_forces_redraw_of_state_mutated_while_paused` (`test_terminal_ui.py:242-269`) sleeps 0.15 s, so it passes via auto-refresh and does not actually verify an immediate redraw.

**Fix:** Either pass `refresh=True` to make the claim true (`self._live.refresh()` after `start()`, or `self._live.update(self._render(), refresh=True)`), or soften the docstring to state the redraw is picked up by the next auto-refresh tick.

### IN-02: Split interactivity detection between logging setup and the confirmer

**File:** `src/pcswitcher/logger.py:357` vs `src/pcswitcher/confirmer.py:100`

**Issue:** `setup_logging` decides UI-vs-stderr routing from `console.is_terminal` (stdout), while `TerminalUIConfirmer.confirm` decides interactive-vs-flag from `sys.stdin.isatty()` (stdin). Under mixed redirection (e.g. stdout is a TTY but stdin is `/dev/null`) these disagree: the Live UI and `UILogHandler` are active, yet confirmations silently fall back to `--allow-*` flags. No crash, but the interactivity model is split-brained and could surprise a user who sees a live UI but gets non-interactive confirmation behavior.

**Fix:** Pick one interactivity signal (or require both stdin and stdout to be TTYs) and share it between logging setup and the confirmer.

---

Reviewed 2026-07-04 by Claude (gsd-code-reviewer) at standard depth.
