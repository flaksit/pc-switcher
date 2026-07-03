---
status: diagnosed
phase: 01-home-sync-mvp-user-data-sync
source: [01-VERIFICATION.md]
started: "2026-07-02T13:04:39Z"
updated: "2026-07-03T19:20:00Z"
---

## Current Test

number: 1

name: Install pc-switcher on machine A (first-run install UX)

expected: Following the documented install path, pc-switcher installs on A; `pc-switcher --version` works; `pc-switcher init` writes a default config.

awaiting: user response (clean-reinstall result not yet confirmed — first attempt hit a pre-existing uv-tool install left over from prior session state; uninstalled and re-run requested, result pending)

## Tests

Scope: this UAT covers only what automation cannot — the interactive UX and a whole-flow walkthrough for maintainer confidence. The data-level guarantees (byte-identical content, numeric uid/gid, permissions incl. special bits, ACLs, mtime, hard/sym links across user/root/other-user files and dirs, config exclusions, ADR-016 runtime-file exclusion, directory-deletion propagation, the non-interactive `--yes`/`--allow-first-sync`/`--allow-out-of-order` paths) are covered by the automated suite (`test_end_to_end_sync.py::TestEndToEndSync::test_core_us_job_arch_as1_job_integration_via_interface`, green in CI) and are exercised implicitly as the walkthrough edits and syncs files.

### 1. Install pc-switcher on machine A (first-run install UX)

expected: Following the documented install path (install script / `uv tool install` from a release), pc-switcher installs on a real machine A. `pc-switcher --version` reports the installed version; `pc-switcher init` creates `~/.config/pc-switcher/config.yaml` with the default `/home` + `/root` folder-sync config. Docs are sufficient to complete the install without guessing.

why_human: Validates the real install experience and documentation on a real machine — a UX/docs check, not a scriptable assertion.

result: pass

note: First attempt found pc-switcher already registered via `uv tool list` (leftover from prior session state, masked by a non-interactive-SSH PATH check appearing to show it absent). After `uv tool uninstall pcswitcher` + `rm -rf ~/.config/pc-switcher ~/.local/share/pc-switcher` + re-running the install script, the clean first-run install passed.

### 2. First-sync warning — abort (interactive)

expected: With target B having no prior sync history, run `pc-switcher sync <B>` from A on an interactive terminal (no `--yes`, no `--allow-first-sync`). A first-sync "target will be overwritten" warning appears and asks for confirmation. Answer NO. The sync aborts cleanly; B is not modified (no files transferred); A's sync-history is not written.

why_human: Interactive confirmation over a TTY; the non-interactive suite cannot exercise the prompt or the human "no".

result: issue

reported: "Answered n at the first-sync warning: sync aborted cleanly (pc2 confirmed untouched: `ls ~` empty of new files, only the pre-existing lock file present), no history written. But: (1) layout showed two stacked 'Recent Logs' panels with a skipped step counter (1/11 then 3/11, no 2/11); (2) the decline is logged at [CRITICAL] and the 'Sync failed: ...' message is printed twice; (3) the warning text names /home, /root and '(rsync --delete)', which are FolderSync-specific details baked into orchestrator-level messaging."

severity: minor

### 3. First-sync warning — confirm (interactive)

expected: Re-run `pc-switcher sync <B>` from A. The same first-sync warning appears. Answer YES. The sync proceeds: pc-switcher installs/updates on B, config syncs, the folder transfer runs, and B ends up matching A. A's sync-history records A as source, B as target.

why_human: Interactive confirmation prompt.

result: [pending]

### 4. Clean round-trip alternation (whole-flow walkthrough)

expected: Edit a file under the synced tree on A, then `pc-switcher sync <B>` — change lands on B. Edit a file on B, then `pc-switcher sync <A>` from B — change lands on A. Edit again on A, then `pc-switcher sync <B>`. A clean A→B / B→A / A→B alternation proceeds each time WITHOUT any out-of-order/consecutive-push warning (the clean case is silent). This is the maintainer's confidence pass over the whole feature with real edits.

why_human: End-to-end confidence walkthrough on a real machine pair with real edits and real timing.

result: [pending]

### 5. Consecutive-push heads-up — leave it waiting (interactive)

expected: Create a consecutive-push topology — e.g. from B, `pc-switcher sync <A>` twice in a row without A pushing back in between. The second sync surfaces the out-of-order / consecutive-push heads-up and waits at the confirmation prompt. Leave this sync sitting at the prompt (do not answer yet) for the next test.

why_human: Interactive prompt; the "waiting for the human" state is itself the UX being validated, and it sets up the concurrent-lock test.

result: [pending]

### 6. Concurrent sync blocked + how-to-unblock, then confirm the waiting one

expected: While test 5's sync is still waiting at its prompt (and therefore holding B's lock), launch a SECOND `pc-switcher sync <A>` from B in another terminal. It fails fast (no hang) with "This machine is already involved in a sync (held by: …)" and how-to-unblock guidance (wait for the other sync; the lock auto-releases when its process exits; force-clear a genuinely stuck lock by terminating the holder, not by deleting the lock file). Then return to test 5's sync and answer YES; it completes normally. A follow-up sync afterwards succeeds.

why_human: Requires two concurrent interactive invocations racing on the same machine; the messaging/timing UX is not covered by the automated single-conflict test (which pre-holds the lock out-of-band).

result: [pending]

## Summary

total: 6

passed: 1

issues: 1

pending: 4

skipped: 0

blocked: 0

## Gaps

- SC3 dev-tool-cache INCLUSION is not asserted by automation. The default config deliberately SYNCS dev-tool caches (`~/.cargo`, `~/.npm`, `~/.cache/uv`, `~/.local/share/uv`) and VS Code user state (`~/.config/Code/User/`), but the consolidated integration test excludes `.cache` and `.local/share/uv/python` for speed, so it only verifies the EXCLUSION side of SC3, not that included caches actually transfer. This is automatable and should be added to the integration test (seed a small marker under a non-excluded dev-tool-cache path and assert it reaches the target), not verified by hand here.

- truth: "First-sync and config-sync confirmation messages are orchestrator-level and job-agnostic, per ADR-015's stated intent that the first-sync question is common to all jobs"
  status: failed
  reason: "orchestrator.py's _first_sync_scope() (lines 432-446) reads the folder_sync job config by name (job_configs.get('folder_sync', {})) and _confirm_first_sync() (lines 448-493) hardcodes the literal mechanism phrase '(rsync --delete)' into the warning text (line 472) alongside the /home /root path listing. This is FolderSync-specific detail leaking into code ADR-015 intends to be job-agnostic; a future non-rsync job (packages/docker, listed as future jobs in default-config.yaml) added to first-sync scope would inherit an incorrect 'rsync --delete' description."
  severity: minor
  test: 2
  root_cause: "orchestrator hardcodes a single job's config-shape and transport mechanism into generic messaging instead of each job describing its own scope/mechanism"
  artifacts:
    - path: "src/pcswitcher/orchestrator.py"
      issue: "_first_sync_scope() (432-446) and _confirm_first_sync() (448-493) hardcode folder_sync's config shape and rsync-specific wording"
  missing:
    - "A job-agnostic way for whichever job(s) are in first-sync scope to describe their own scope/mechanism to the orchestrator, instead of the orchestrator reading folder_sync's config dict and rsync wording directly"
  debug_session: ""

- truth: "Declining a confirmation prompt (first-sync, config-sync) aborts cleanly with an accurate, single, non-alarming message"
  status: failed
  reason: "User reported two 'Sync failed' messages (once as a [CRITICAL] log line, once as a bare red console line) when answering n to both the first-sync warning and the config-sync prompt. Declining raises a plain RuntimeError (orchestrator.py:268, and orchestrator.py:427-428 for config-sync) that falls into the generic `except Exception` handler (orchestrator.py:330-334), which logs every exception at CRITICAL and re-raises; cli.py's outer handler (cli.py:349-351) then prints the same message again. CRITICAL is documented as 'Unrecoverable errors, sync must abort' (models.py:47) but a user declining a prompt is expected control flow, not an unrecoverable error."
  severity: minor
  test: 2
  root_cause: "user-declined-confirmation and genuine unrecoverable failures share the same generic except-Exception handling path, at both the orchestrator and CLI layers"
  artifacts:
    - path: "src/pcswitcher/orchestrator.py"
      issue: "generic except Exception handler (lines 330-334) logs user-declined-confirmation at CRITICAL, same as genuine failures"
    - path: "src/pcswitcher/cli.py"
      issue: "outer except Exception (lines 349-351) reprints the same message the orchestrator already logged"
  missing:
    - "A distinct 'aborted by user choice' outcome (or exception subtype) that logs once at INFO/WARNING instead of flowing through the generic CRITICAL failure path, and that the CLI's outer handler recognizes to avoid re-printing"
  debug_session: ""

- truth: "The live TUI updates in place through confirmation pauses, without leaving duplicate stale panels in the terminal or skipping step numbers"
  status: failed
  reason: "User reported duplicated 'Recent Logs'/status blocks and a skipped step number (1/11 then 3/11, no 2/11) around confirmation prompts. TerminalUI.start() (ui.py:114-122) always constructs a brand-new rich.live.Live object rather than resuming the previous one; Confirmer.confirm() (confirmer.py:117,129) calls ui.stop() then ui.start() around every prompt, so each confirmation permanently leaves the prior Live's last frame in the scrollback (transient=False) and begins a fresh Live region below it."
  severity: minor
  test: 2
  root_cause: "TerminalUI.start() re-instantiates Live() on every resume instead of resuming a single persistent Live instance"
  artifacts:
    - path: "src/pcswitcher/ui.py"
      issue: "start() (114-122) always creates a new Live() instance instead of resuming"
    - path: "src/pcswitcher/confirmer.py"
      issue: "stop()/start() around every confirm (117, 129) compounds the duplicate-frame effect"
  missing:
    - "TerminalUI should support pause/resume of a single Live instance instead of re-instantiating a new one on every resume"
  debug_session: ""

- truth: "The live progress display updates job progress bars in place at 10Hz without visual corruption"
  status: failed
  reason: "User reported the screen flickering on every refresh with 'Connection: connected... Step 9/10 / dummy_success ...' lines repeatedly stacking underneath each other rather than being overwritten, escalating through the dummy_success job's 0%->100% progress with dozens of duplicate blocks and multiple duplicate 'Recent Logs' panels appearing. Observed on pc1 running `pc-switcher sync pc2 --dry-run` with folder_sync disabled (only dummy_success + disk_space_monitor jobs active), unrelated to any confirmation pause. NOT yet root-caused via static code reading -- ui.py's Live is configured with refresh_per_second=10 and update_job_progress()/set_connection_status() call self._live.update(self._render()) directly; why this produces growing duplicate frames instead of an in-place redraw needs live reproduction/debugging, not just code reading."
  severity: major
  test: null
  root_cause: "setup_logging() (logger.py:268-271) installs a logging.StreamHandler(sys.stderr) that writes formatted log records directly to the terminal, uncoordinated with TerminalUI's rich.live.Live instance. Any INFO+ log emitted while Live is active (routine orchestrator phase logs, dummy_success's periodic progress logs) prints a raw line into the region Live believes it exclusively owns, desyncing Live's 'how many lines did I render last time' cursor bookkeeping. Confirmed via live raw-byte capture on pc1 (script + ssh -tt): the 'Recent Logs' panel header appeared 761 times and the dummy_success 0% frame 326 times in one run, with orchestrator INFO log lines visibly interleaved as plain \\r\\n-terminated text ahead of Live's own erase/redraw sequences."
  artifacts:
    - path: "src/pcswitcher/logger.py"
      issue: "setup_logging() (268-271): stream_handler = logging.StreamHandler(sys.stderr) writes directly to the terminal instead of routing through TerminalUI's Live-managed render"
    - path: "src/pcswitcher/ui.py"
      issue: "TerminalUI already has an unused add_log_message()/log-panel mechanism designed for exactly this purpose (populated by nothing since ADR-010's migration); its Live cursor bookkeeping is the victim, corrupted by the independent stderr writes"
    - path: "src/pcswitcher/config.py"
      issue: "LogConfig.tui defaults to INFO (20), which is what makes the corruption trigger on routine orchestrator logs, not just dummy_success's frequent ones"
  missing:
    - "Route TUI-destined log output exclusively through TerminalUI's add_log_message()/log-panel mechanism (e.g. a custom logging.Handler whose emit() posts into the UI's event queue) instead of a separate stderr StreamHandler, so all terminal output funnels through one Live.update() path"
    - "Fall back to a plain stderr StreamHandler only when no TerminalUI/Live is active (non-interactive or non-TTY runs)"
  debug_session: ".planning/debug/tui-live-progress-flooding.md"

- truth: "Confirmation prompts behave consistently under --dry-run across the codebase"
  status: failed
  reason: "User found it confusing that pc-switcher still asks 'Apply this config to target? [y/n]' during --dry-run. Confirmed the answer is a safe no-op (config_sync.py:184-201 only calls the real write _copy_config_to_target when dry_run is False, matching ADR-014), so there is no data-safety violation. But this is inconsistent with _confirm_first_sync (orchestrator.py:478-485), which checks dry_run BEFORE prompting and skips the question entirely. _prompt_new_config (config_sync.py:71-103) prompts unconditionally regardless of dry_run, only gating the write afterward."
  severity: minor
  test: null
  root_cause: "two confirmation call sites in the same codebase handle --dry-run inconsistently: one skips prompting, the other prompts and only gates the resulting write"
  artifacts:
    - path: "src/pcswitcher/config_sync.py"
      issue: "_handle_no_target_config/_prompt_new_config (71-103, 184-201) prompt interactively even under --dry-run, unlike orchestrator's other confirmations"
  missing:
    - "Skip the config-sync prompt under --dry-run (log-and-proceed, matching _confirm_first_sync's pattern) for consistency"
  debug_session: ""
