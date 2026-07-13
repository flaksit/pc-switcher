---
status: resolved
trigger: "tui-live-progress-flooding: During pc-switcher sync <target>, the live terminal UI does not redraw in place. Each refresh tick prints a brand new copy of the status bar + job progress lines underneath the previous one, producing dozens of duplicate stacked lines as dummy_success's progress climbs, with multiple duplicate 'Recent Logs' panel headers interspersed. NOT the confirm pause/resume issue (already diagnosed)."
created: 2026-07-03T00:00:00Z
updated: 2026-07-13T12:17:28Z
resolution: "Fixed in plan 01-18 — UILogHandler routes log records into TerminalUI's Recent Logs panel via loop.call_soon_threadsafe, replacing the stderr StreamHandler that bypassed Live's cursor bookkeeping. Confirmed absent on a real terminal in UAT Test 7 (01-UAT.md)."
---

## Current Focus

hypothesis: CONFIRMED. setup_logging() in src/pcswitcher/logger.py installs a `logging.StreamHandler(sys.stderr)` (RichFormatter) that writes formatted log lines directly to the terminal on an independent background thread (QueueListener), completely bypassing rich.live.Live's own cursor/erase bookkeeping in TerminalUI (src/pcswitcher/ui.py). Any INFO+ log record emitted while Live is active (orchestrator phase-transition logs, DummySuccessJob's every-2s INFO logs, etc.) punches an uncoordinated write into the same terminal Live thinks it exclusively controls, permanently desyncing Live's "how many lines did I last render" counter from the terminal's actual state. Root cause confirmed via raw byte-level capture (see Evidence).
test: DONE. Reproduced live via `script`-captured pty session on pc1 (ssh -tt, real interactive terminal) running `pc-switcher sync pc2 --dry-run --yes --allow-first-sync --allow-out-of-order` with folder_sync disabled.
expecting: MET. Raw capture showed direct stderr log lines interleaved between Live's erase/redraw escape sequences, with hundreds of duplicate full-frame prints as a result.
next_action: None -- diagnosis complete for find_root_cause_only mode. Suggested fix direction recorded in Resolution below for the follow-up fix task.

## Symptoms

expected: The live progress display (src/pcswitcher/ui.py, TerminalUI class) updates job progress bars in place at its configured 10Hz refresh rate, redrawing the same terminal region rather than scrolling/duplicating.
actual: User (real interactive SSH session to a test VM) observed the connection-status line + job-progress line repeating identically many times in a growing scroll, then progress increasing, then multiple "Recent Logs" panel headers appearing at different scroll positions. Happens continuously during ordinary job-progress updates (update_job_progress() / set_connection_status() calling self._live.update(self._render())), not during any Confirmer pause/resume cycle.
errors: None reported/logged -- purely a rendering/terminal-output defect, not a crash.
reproduction: On pc1 (91.99.178.190), run `pc-switcher sync pc2 --dry-run` over a real interactive pty, with `sync_jobs.folder_sync: false` in ~/.config/pc-switcher/config.yaml to keep repro fast (only dummy_success + disk_space_monitor run, ~20-40s).
started: Discovered during phase-01 human UAT verification (2026-07-03), branch HEAD bec5cd75.

## Eliminated

- hypothesis: Nested/duplicate rich.live.Live() instances (the confirmer pause/resume bug) causing this.
  evidence: Explicitly out of scope per task description -- confirmer.py's ui.stop()/ui.start() cycle around confirm() prompts is a separate, already-diagnosed issue. Symptoms describe flooding during ordinary progress updates, not confirmation prompts.
  timestamp: 2026-07-03T00:05:00Z

## Evidence

- timestamp: 2026-07-03T00:02:00Z
  checked: src/pcswitcher/ui.py (full file) -- TerminalUI.start()/update_job_progress()/set_connection_status()/etc.
  found: All UI mutators call `self._live.update(self._render())` with no explicit `refresh=` kwarg. Live is constructed once in start() with `refresh_per_second=10, transient=False`, bound to a single `Console()` instance passed in from orchestrator.py. No evidence of multiple Live instances being created during normal (non-confirm) operation.
  implication: The duplication is not caused by ui.py's own update pattern in isolation; something external must be writing to the same terminal outside Live's control.

- timestamp: 2026-07-03T00:04:00Z
  checked: src/pcswitcher/orchestrator.py lines 190-240, cli.py module-level `console = Console()`
  found: orchestrator.py creates its own `Console()` (line 226) and passes it consistently to both TerminalUI and TerminalUIConfirmer. cli.py has a separate module-level Console but its console.print() calls during `sync` (sigint handler, error paths) only fire outside/around the orchestrator.run() awaited call, not concurrently with normal per-tick progress updates.
  implication: Ruled out cli.py's console as a concurrent interleaving source for the routine flooding pattern (it could still interleave on SIGINT, but that's not what's reported).

- timestamp: 2026-07-03T00:06:00Z
  checked: src/pcswitcher/logger.py setup_logging(), src/pcswitcher/config.py LogConfig defaults
  found: 'setup_logging()' creates `stream_handler = logging.StreamHandler(sys.stderr)` formatted with `RichFormatter` (produces ANSI-styled text via an internal Console(file=io.StringIO(), force_terminal=True)), attached via QueueHandler/QueueListener background thread to the "pcswitcher" logger. Default `LogConfig.tui = 20` (INFO). This handler writes directly to `sys.stderr`, a file descriptor pointing at the SAME terminal Live renders to on stdout, entirely independent of Live's internal render/erase bookkeeping.
  implication: Any INFO+ log record from pcswitcher code emitted while Live is active gets printed as a raw new line on the terminal, bypassing Live's cursor-tracking. Rich's own documentation warns against writing to the console via any means other than Live while a Live display is active, precisely because Live can't know about lines it didn't put there itself.

- timestamp: 2026-07-03T00:07:00Z
  checked: src/pcswitcher/jobs/dummy_success.py _run_source_phase()/_run_target_phase()
  found: Logs `Host.SOURCE/TARGET, LogLevel.INFO, "Source/Target phase: Ns elapsed"` every 2 seconds for the duration of each phase (default 20s each => ~10 INFO log lines per phase, ~20 total over the job's ~40s run), each of which is INFO level, i.e. at/above the default tui floor and so IS emitted via the stderr StreamHandler while Live is active.
  implication: The frequency and timing of these direct-to-stderr writes (every 2s) lines up with the user's observation of duplicate frames appearing as dummy_success's progress climbs 0%->25%->50%->75%->100% -- each log write is a plausible trigger point for Live's bookkeeping to desync.

- timestamp: 2026-07-03T22:55:00Z
  checked: Live reproduction via `script`-captured pty session on pc1 test VM (ssh -tt, real terminal, TERM=xterm-256color), `pc-switcher sync pc2 --dry-run --yes --allow-first-sync --allow-out-of-order` with folder_sync disabled. Captured 1,374,540 raw bytes / 9,186 lines to /tmp/repro.typescript, pulled back and inspected byte-for-byte with a Python script (no text-mode reinterpretation).
  found: Confirmed massive duplication -- the "Recent Logs" panel header (part of the single Live-managed Group) appears 761 separate times in one run, when a correctly-functioning Live should print it once and then only overwrite in place. The dummy_success progress bar at "0%" alone appears 326 times (should render once until the value changes). Direct byte inspection at the very start of the capture (byte offset 206, before dummy_success even starts) shows: Live hides the cursor (`\x1b[?25l`), then SIX orchestrator INFO log lines ("Starting sync session", "[DRY-RUN] Preview mode...", "Acquiring source lock", "Connecting to target", "Connected to target", "Acquiring target lock") are printed as raw `\r\n`-terminated lines directly to the stream -- these come from `self._logger.info(...)` calls routed through the stderr StreamHandler, NOT through `self._live.update()` -- before Live prints its own first frame. At byte offset 51151 (first dummy_success log line, "Source phase: 2s elapsed"), the raw bytes show the log line printed via plain `\r\n`, immediately followed by Live's own erase sequence `\r\x1b[2K\x1b[1A` repeated 13 times, then a fresh frame printed. Because 3 fresh log lines (Configuration sync completed / Starting sync operations / Source phase: 2s elapsed) were appended to the terminal by the independent stderr handler between Live's previous frame and this refresh, Live's cursor-up-N erase assumed a terminal position that no longer matched reality -- it erased into the wrong lines, leaving stale copies of its own earlier frame behind while appending a new one below. This exact mechanism, repeated on every subsequent 10Hz auto-refresh tick and every explicit `self._live.update(self._render())` call (update_job_progress/set_connection_status/set_current_step/add_log_message), compounds across the run, producing the 761 duplicate frames counted above.
  implication: Root cause is empirically confirmed, not just theorized: the `logging.StreamHandler(sys.stderr)` configured in `setup_logging()` (src/pcswitcher/logger.py) writes formatted log records directly onto the same terminal `TerminalUI`'s `rich.live.Live` renders to (src/pcswitcher/ui.py), on an independent background thread (QueueListener), with zero coordination with Live's internal "how many lines did I last render" bookkeeping. Any INFO+ log record emitted while Live is active -- which includes routine orchestrator phase-transition logs (session start, phase logs) as well as DummySuccessJob's every-2s logging -- triggers this desync. Rich's own documentation warns against writing to the console via any means other than the Live object itself while a Live display is running, for exactly this reason.

- timestamp: 2026-07-03T22:58:00Z
  checked: docs/adr/adr-010-logging-infrastructure.md
  found: ADR-010 (accepted 2025-12-31) mandates "Custom `logging.Formatter` using Rich for TUI output" as part of the stdlib-logging migration, implemented as `stream_handler = logging.StreamHandler(sys.stderr)` with `RichFormatter`. The ADR only specifies the formatter/level-filtering model (3-setting file/tui/external floors); it does not address how "TUI output" should be reconciled with a concurrently-running `rich.live.Live` display, and TerminalUI's own log panel (`add_log_message()`, already part of the Live-rendered Group) was left unconnected to this log path -- the code comment at ui.py's `consume_events()` explicitly notes "LogEvent is no longer processed here... The TUI log panel is not populated by this method. See ADR-010."
  implication: The regression is a gap in ADR-010's implementation, not a Rich bug: the decision to route "TUI output" through a raw `logging.StreamHandler` on `sys.stderr` was never reconciled with the pre-existing `TerminalUI`/`Live` display that owns the same terminal region. The two independent output paths (direct stderr writes vs. Live-managed redraws) actively fight over the same terminal.

## Resolution

root_cause: "Confirmed via raw byte-level reproduction (see Evidence, 2026-07-03T22:55:00Z): `logging.StreamHandler(sys.stderr)` -- configured in `setup_logging()` (src/pcswitcher/logger.py:268-271) per ADR-010 -- writes RichFormatter-formatted log records directly to the terminal on an independent background thread (QueueListener), completely uncoordinated with rich.live.Live's own cursor/erase bookkeeping in TerminalUI (src/pcswitcher/ui.py). Because both stdout (Live's console) and stderr share the same physical terminal in an interactive session, every INFO+ log record emitted while Live is active (default tui floor is INFO; orchestrator phase-transition logs plus DummySuccessJob's every-2s logging supply frequent triggers) inserts an untracked line into the terminal. Live's next redraw then moves the cursor up by a stale line count and erases/overwrites the wrong region, leaving part of its previous frame behind as a visible duplicate while printing a new frame below. This compounds on every subsequent refresh (10Hz auto-refresh timer plus every explicit `self._live.update(self._render())` call), producing the reported dozens-to-hundreds of duplicate stacked frames. Confirmed empirically: 761 duplicate 'Recent Logs' panel headers and 326 duplicate '0%' progress-bar prints in a single ~1-minute run."
fix: ""
verification: ""
files_changed: []
