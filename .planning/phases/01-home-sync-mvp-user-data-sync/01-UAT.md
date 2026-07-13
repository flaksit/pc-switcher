---
status: complete
phase: 01-home-sync-mvp-user-data-sync
source: [01-VERIFICATION.md]
started: "2026-07-04T16:03:13Z"
updated: "2026-07-13T12:17:28Z"
---

## Current Test

[testing complete]

## Tests

Scope: this UAT covers only what automation cannot — the interactive UX and a whole-flow walkthrough for maintainer confidence. The data-level guarantees (byte-identical content, numeric uid/gid, permissions incl. special bits, ACLs, mtime, hard/sym links across user/root/other-user files and dirs, config exclusions, ADR-016 runtime-file exclusion, directory-deletion propagation, the non-interactive `--yes`/`--allow-first-sync`/`--allow-out-of-order` paths) are covered by the automated suite and are proven by Test 8 (the VM integration run). This is a fresh restart re-testing against the fixed code from gap-closure round 01-15..01-18 + code-review.

### 1. Install pc-switcher on machine A (first-run install UX)

expected: Following the documented install path (install script / `uv tool install` from a release), pc-switcher installs on a real machine A. `pc-switcher --version` reports the installed version; `pc-switcher init` creates `~/.config/pc-switcher/config.yaml` with the default `/home` + `/root` folder-sync config. Docs are sufficient to complete the install without guessing.

why_human: Validates the real install experience and documentation on a real machine — a UX/docs check, not a scriptable assertion.

result: pass

note: Installed on clean pc1 via `curl .../refs/tags/v0.1.0-alpha.14/install.sh | VERSION=v0.1.0-alpha.14 bash`; `pc-switcher --version` → 0.1.0-alpha.14 (clean release tag); `init` created config.yaml. Release must be an ANNOTATED tag (see issue #161 / memory) — lightweight tags install as a wrong dev version. Known first-run UX gap: installer puts binary in ~/.local/bin without adding it to the current shell or telling the user (fresh login shell fixes it) — user accepted Test 1 as pass.

### 2. First-sync warning — abort (interactive, fixed code)

expected: With target B having no prior sync history, run `pc-switcher sync <B>` from A on an interactive terminal (no `--yes`, no `--allow-first-sync`). A first-sync "target will be overwritten" warning appears and asks for confirmation. The warning's "In scope" block is built from the FolderSync job's own self-description — it names no transport mechanism directly (no hardcoded "rsync --delete" leaking from the orchestrator). Answer NO. The sync aborts with exactly one calm yellow "Sync aborted: ..." line — no red "Sync failed", no duplicate CRITICAL log line, logged at WARNING not CRITICAL. The terminal shows one continuous Live region throughout: no duplicate stacked "Recent Logs" panels, no skipped step number. B is not modified (no files transferred); A's sync-history is not written.

why_human: Interactive TTY behavior + Rich Live terminal cursor rendering; the fix is unit-tested at the mechanism level (Live-instance reuse, single WARNING log, single CLI message) but not observed on a real terminal since these commits landed.

result: pass

note: Ran on pc1 → freshly-reset pc2, answered n. Core abort correct: single [WARNING] "Sync aborted by user", one calm yellow "Sync aborted: ..." CLI line, no red "Sync failed", no double-CRITICAL, no flooding, step counter 1→2→3 (no skip). Residual TUI artifact (accepted as pass, logged as minor gap): two stacked "Recent Logs" panels remain and the First Sync warning panel is truncated where the resumed Connection status line overwrote it — the same class as the original Test-2 complaint. 01-17 fixed flooding/step-skip/double-CRITICAL but not this confirm-boundary duplication.

### 3. First-sync warning — confirm (interactive)

expected: Re-run `pc-switcher sync <B>` from A. The same first-sync warning appears. Answer YES. The sync proceeds: pc-switcher installs/updates on B, config syncs, the folder transfer runs, and B ends up matching A. A's sync-history records A as source, B as target.

why_human: Interactive confirmation prompt; never run since the phase's original UAT pass. Requires a live machine pair.

result: pass

note: Ran on pc1 → freshly-reset pc2, answered y (config prompt appeared, answered y). Sync completed; pc-switcher installed on pc2 (0.1.0a14 via release-floor fallback), config applied, /home + /root transferred. Data-level copy/delete verified hands-on via planted fixtures + a 2nd (consecutive) sync: new files/nested dirs copied to pc2, executable perms preserved, symlink preserved as a symlink, and pc2-only files/dirs removed by rsync --delete. Findings from this test logged in Gaps (all minor): dry-run fidelity + log pointer, disk_space_monitor 0% bar, folder_sync 2% bar, install_on_target noisy WARNING, config-prompt decline aborts whole sync, and orchestrator dry-run-preview wording (job-specific "deleted" + log pointer).

### 4. Clean round-trip alternation (whole-flow walkthrough)

expected: Edit a file under the synced tree on A, then `pc-switcher sync <B>` — change lands on B. Edit a file on B, then `pc-switcher sync <A>` from B — change lands on A. Edit again on A, then `pc-switcher sync <B>`. A clean A→B / B→A / A→B alternation proceeds each time WITHOUT any out-of-order/consecutive-push warning (the clean case is silent). The live display stays coherent across all three runs — this is the scenario most likely to reproduce (or definitively disprove) the original flooding bug under sustained job activity. This is the maintainer's confidence pass over the whole feature with real edits.

why_human: End-to-end confidence walkthrough on a real machine pair with real edits and real timing.

result: pass

note: Reverse direction pc2→pc1 ran SILENTLY — no out-of-order/consecutive warning, no first-sync warning, no config prompt (the clean alternation case). pc2's change (from-pc2.txt + appended line to hello.txt) landed on pc1, confirmed by user. Data alternation A→B (Test 3) and B→A verified in both directions. Cosmetic finding logged: config_sync pauses/resumes the Live unconditionally on every interactive sync, leaving a stale 'Recent Logs' panel even with no prompt.

### 5. Consecutive-push heads-up — leave it waiting (interactive)

expected: Create a consecutive-push topology — e.g. from B, `pc-switcher sync <A>` twice in a row without A pushing back in between. The second sync surfaces the out-of-order / consecutive-push heads-up and waits at the confirmation prompt. Leave this sync sitting at the prompt (do not answer yet) for the next test.

why_human: Interactive prompt; the "waiting for the human" state is itself the UX being validated, and it sets up the concurrent-lock test.

result: pass

note: From pc2, a 2nd consecutive pc2→pc1 surfaced the W3 "Consecutive Sync — No Back-Sync Received" heads-up and waited patiently at the prompt (held the lock). Answering y afterward completed the sync normally.

### 6. Concurrent sync blocked + how-to-unblock, then confirm the waiting one

expected: While test 5's sync is still waiting at its prompt (and therefore holding B's lock), launch a SECOND `pc-switcher sync <A>` from B in another terminal. It fails fast (no hang) with "This machine is already involved in a sync (held by: …)" and how-to-unblock guidance (wait for the other sync; the lock auto-releases when its process exits; force-clear a genuinely stuck lock by terminating the holder, not by deleting the lock file). Then return to test 5's sync and answer YES; it completes normally. A follow-up sync afterwards succeeds.

why_human: Requires two concurrent interactive invocations racing on the same machine; the messaging/timing UX is not covered by the automated single-conflict test (which pre-holds the lock out-of-band).

result: pass

note: While Test 5's sync waited (holding pc2's lock), a second pc2→pc1 in another terminal failed FAST (no hang) with "This machine is already involved in a sync (held by: source:pc2:…:pid=…)" plus genuinely useful how-to-unblock guidance (wait for it to finish / lock auto-releases on exit / fuser -k or pkill the holder, don't delete the lock file). Returning to Test 5's sync and answering y completed normally. Minor finding logged: that lock-conflict message prints twice (CRITICAL log + red CLI line) — user flagged very low prio.

### 7. Live-progress flooding fix — dry-run repro (01-18 deferred check)

expected: On pc1, run `pc-switcher sync pc2 --dry-run` with `dummy_success` active (per `.planning/debug/tui-live-progress-flooding.md`'s reproduction recipe) and observe the raw terminal output. No duplicate "Recent Logs" panel headers, no duplicate progress frames; the display updates in place at 10 Hz with no flicker or stacked frames.

why_human: 01-18's own verification defers this to a manual/VM re-test — the log-routing mechanism is unit-tested but the absence of a live-terminal cursor-desync artifact needs direct observation.

result: pass

note: Validated on accumulated evidence across the user's dry-run + multiple full runs: dummy_success's frequent "Source phase: Ns elapsed" logs (the original flooding trigger) render cleanly inside the Recent Logs panel with no flooding (original bug rendered the panel header 761× / 326 duplicate frames). The only residual TUI artifacts are the pause-boundary duplicates logged separately (confirm-boundary + config_sync unconditional pause), which are distinct from the flooding and much milder. Strict folder_sync-disabled repro not run (would only reduce activity, can't surface worse).

### 8. Full VM integration test suite (ROADMAP SC1–5)

expected: Push the current branch to origin (or cut a fresh pre-release tag), then run `tests/run-integration-tests.sh tests/integration/test_end_to_end_sync.py` against live VMs. `test_core_us_job_arch_as1_job_integration_via_interface` passes — byte-identical content, numeric uid/gid, permission bits incl. setuid/setgid/sticky, POSIX ACL, mtime, hard-link/symlink handling, exclusions, ADR-016 runtime excludes, and the SC3 dev-tool-cache / VS Code `User/` inclusion assertions restored by commit 9d14f54, all green end-to-end.

why_human: Requires live Hetzner VM infrastructure and a push to origin — the last green CI run predates every commit in this gap-closure round. Mark pass once CI is green on the pushed branch.

result: pass

note: Full VM integration suite GREEN on the fixed code — 57 passed, 0 failed (PR #160 run 28902021078, HEAD facad73, 13m09s). Covers byte-identical content, numeric uid/gid, permission bits incl. setuid/setgid/sticky, POSIX ACLs, mtime, hard/sym links, exclusions, ADR-016 runtime excludes, and SC3 dev-tool-cache/VS Code inclusion. First run (28896410635) had failed on: test_ui_lifecycle_during_sync (my conditional-pause change — fixed in facad73) and two install_on_target version-match tests (a race from cutting the alpha.15 tag mid-run — resolved by re-running with the tag stable).

### 9. Run warnings resurfaced — persistent counter + end-of-run summary

expected: During a sync that emits a WARNING (e.g. an invalid `GITHUB_TOKEN` so install-on-target warns, or a config/first-sync heads-up), a warning is not lost when it scrolls out of the rolling Recent Logs panel: the status bar shows a persistent `⚠ N` counter that survives pauses/scrolls, and after the Live region stops a static warning summary is reprinted into scrollback (on success as well as failure), ending with a `pc-switcher logs` pointer to the log file.

why_human: Closes the last open finding from the prior re-UAT (warnings scrolling past unread). The buffer/counter/summary mechanism is unit-tested (test_logging.py, test_terminal_ui.py) but the persistent counter and post-Live summary need direct terminal observation.

result: pass

note: Confirmed by user on a real terminal (feature landed in commit 7823112, released as v0.1.0-alpha.21). The `⚠ N` counter persists in the status bar across pauses and the end-of-run summary reprints the warning(s) into scrollback with the `pc-switcher logs` pointer.

## Summary

total: 9

passed: 9

issues: 0

pending: 0

skipped: 0

blocked: 0

<!-- All 8 tests passed. 9 minor findings (in Gaps) fixed in a gap-closure round
     (commits 74bbe2a, 92a0455, d7ca229, e3b477e, f045218; integration-test fix
     facad73), released as v0.1.0-alpha.15. Hands-on re-UAT of the fixes (esp. the
     TUI visual ones #5/#8) pending on a real terminal. -->

## Fix status (gap-closure round, 2026-07-07)

All 9 Gaps below are fixed and committed on 01-folder-sync; unit suite 489 green, VM integration suite 57 green (Test 8). Released as v0.1.0-alpha.15 for re-UAT.

- config-sync decline now explicit ("Abort the sync") — 74bbe2a
- job-agnostic dry-run hint pointing to the log — 92a0455
- install fallback WARNING→DEBUG; lock conflict → SyncLockedError single "Sync blocked" (not double CRITICAL) — d7ca229
- disk_space_monitor bar dropped; folder_sync labels folder + ends 100% — e3b477e
- TUI: transient-clear on pause; config-sync pauses only when prompting — f045218
- integration test_ui_lifecycle_during_sync updated for conditional pause — facad73

## Gaps

<!-- RESOLVED. All 9 minor findings below were fixed in the gap-closure round (see
     "Fix status" above: commits 74bbe2a, 92a0455, d7ca229, e3b477e, f045218; integration
     fix facad73) plus the confirm-boundary/dual-cause follow-ups (422e07f, 0274c1c,
     f7d6279, af8dce0, b4234a0) and hand-confirmed on the VMs through v0.1.0-alpha.20.
     The prior open finding (warnings scrolling past unread) is closed by Test 9
     (commit 7823112, v0.1.0-alpha.21). status flipped failed → fixed. No open gaps. -->

- truth: "`--dry-run` faithfully previews what a real sync would change"
  status: fixed
  reason: "Dry-run reported 0 deletions; the real run deleted 1890 files, all under ~/.cache/uv/{git-v0,archive-v0} — exactly the uv install artifacts that install_on_target (phase 7) creates on the target. Dry-run skips install_on_target (install_on_target.py:91), so it never sees the files that folder_sync --delete then reconciles. Not a data-safety issue (only ~/.cache/uv, regenerable, only files the install job itself created; the sync never even invokes pc-switcher on the target — target ops are raw rsync/btrfs over SSH), but the preview is inaccurate."
  severity: minor
  test: 3
  missing:
    - "The first-sync warning's 'Run pc-switcher sync --dry-run to preview' line should point to the log file (~/.local/share/pc-switcher/logs/, or `pc-switcher logs`) where the per-file/*deleting detail actually is — the TUI only shows summary counts"
    - "Optionally note (or avoid) that install_on_target's filesystem effects are not reflected in dry-run"

- truth: "disk_space_monitor presents itself sensibly in the TUI"
  status: fixed
  reason: "disk_space_monitor occupies a progress line pinned at 0%, which is meaningless — it's a check/monitor, not a unit of transfer progress."
  severity: minor
  test: 3
  missing:
    - "Don't render a progress bar for monitor-type jobs; stay silent and only surface output when relevant (e.g. warn — and stop — when free space is low)"

- truth: "folder_sync progress bar reflects real progress and completes at 100%"
  status: fixed
  reason: "The folder_sync bar flashes ~97% during /home, resets, and ends at 2% (from /root) instead of 100%. It is per-folder and never represents cumulative progress or reaches completion."
  severity: minor
  test: 3
  missing:
    - "Per-folder is acceptable, but show which folder is in progress, and the bar must end at 100% when the job completes"

- truth: "install_on_target logs a WARNING only for genuinely unexpected failures"
  status: fixed
  reason: "Every install-on-target logs '[WARNING] Installation with exact version v0.1.0a14 failed, falling back to release floor'. The exact-version attempt builds a PEP440 tag (0.1.0a14) that never matches the semver release tag (v0.1.0-alpha.14), so the fallback is the NORMAL path, not an error worth a WARNING."
  severity: minor
  test: 3
  missing:
    - "Treat the semver-tag path as the expected path (try it first, or demote the expected fallback to debug/info); only WARN on a genuinely unexpected install failure"

- truth: "A confirmation prompt leaves a clean TUI — no duplicate stacked panels, warning panel intact"
  status: fixed
  reason: "The confirm-around-Live flow leaves artifacts: ui.pause() = Live.stop() with transient=False (ui.py:135,146) leaves the pre-pause frame; the warning is printed as static text (confirmer.py:125); ui.resume() = Live.start() (ui.py:158) begins a fresh Live region below it and its Connection status line overwrites/truncates the warning panel's last line. Result: two stacked 'Recent Logs' panels + a truncated First Sync warning panel. Same class as the original Test-2 complaint; 01-17 fixed the flooding, step-skip and double-CRITICAL but not this confirmation-boundary duplication."
  severity: minor
  test: 2
  missing:
    - "Render the confirmation prompt inside the Live region (or clear/redraw the region on pause) instead of stop → static print → start, so the prompt neither leaves duplicate 'Recent Logs' panels nor truncates the warning panel"

- truth: "The 'Apply this config to target?' prompt makes clear what declining does"
  status: fixed
  reason: "Answering n — which is ALSO the default (config_sync.py:101 default=\"n\", so pressing Enter does the same) — makes _handle_no_target_config return False (config_sync.py:218), and the orchestrator raises SyncAbortedByUser (orchestrator.py:451-452), aborting the ENTIRE sync (no config, no folders transferred). The prompt 'Apply this config to target? [y/n] (n)' doesn't convey that declining aborts everything; a user reasonably reads n as 'skip config / keep target config and continue', and the safe-looking default (n) silently aborts the whole sync."
  severity: minor
  test: 3
  missing:
    - "Make the prompt state the consequence of declining (e.g. that it aborts the sync), and/or reconsider the n default; present 'apply config' vs 'abort sync' as an explicit, unambiguous choice"

- truth: "Orchestrator-level warnings use job-agnostic language and point users to the actual preview location"
  status: fixed
  reason: "The 'Run pc-switcher sync --dry-run to preview what would be deleted before committing to a live sync' line appears in the orchestrator's first-sync warning (orchestrator.py:552) and both out-of-order heads-ups — W2 (orchestrator.py:639-640) and W3 (orchestrator.py:651-652). (a) 'deleted' is FolderSync/rsync-specific, but these are orchestrator-level messages that will later coordinate packages/system-config jobs where a change isn't a file deletion — the wording should be generic (e.g. 'what would change on <target>'). (b) A --dry-run's per-item/deletion detail is only in the log file, not the TUI, so the line should point to ~/.local/share/pc-switcher/logs/ (or `pc-switcher logs`)."
  severity: minor
  test: 5
  missing:
    - "Make the dry-run-preview line job-agnostic ('preview what would change on <target>') and point to the log file for the per-item detail; applies to the first-sync warning and both W2/W3 out-of-order heads-ups"

- truth: "A silent sync (no prompts shown) leaves a single clean Live region"
  status: fixed
  reason: "config_sync pauses/resumes the Live even when no prompt is shown. sync_config_to_target sets should_pause = ui is not None and not auto_accept and not dry_run (config_sync.py:303) — true for ANY interactive sync — then ui.pause() (=Live.stop, leaves a stale frame) / ui.resume() (=Live.start, new region) around the whole config step (config_sync.py:306,317), regardless of whether a prompt actually fires. When the target config matches (common case, config_sync.py:188 just prints 'skipping'), no prompt occurs yet the Live is still stopped+restarted, leaving a stale 'Recent Logs' panel (observed frozen at Step 7/11) with a fresh region below. Same root mechanism as the Test-2 confirm-boundary finding, but this fires on every interactive sync with no user interaction at all."
  severity: minor
  test: 4
  missing:
    - "Pause the Live only when a prompt is actually about to be shown (pause lazily inside the prompt paths), not unconditionally around the config-sync step; combine with rendering prompts inside the Live / redraw-on-resume so no stale 'Recent Logs' panel is left"

- truth: "A lock-conflict failure shows a single, appropriately-leveled message"
  status: fixed
  reason: "The lock-conflict ('This machine is already involved in a sync (held by: …)') prints twice: once as a [CRITICAL] orchestrator log line in the panel (orchestrator.py:357 generic 'except Exception' → logger.critical('Sync failed: %s')) and once as the red 'Sync failed:' CLI line (cli.py generic exception handler re-print). The 01-16 single-message contract only covers SyncAbortedByUser (user decline, logged once at WARNING); a lock conflict is a different generic exception, so it still double-prints and at CRITICAL — even though 'another sync is already running' is an expected, retryable condition, not an unrecoverable crash. User: very low prio."
  severity: minor
  test: 6
  missing:
    - "Route lock-conflict (and similar expected 'try again later' failures) through the single-message path like SyncAbortedByUser — print once at an appropriate level (not CRITICAL); don't fire both the orchestrator CRITICAL log and the CLI reprint"
