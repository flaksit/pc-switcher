---
phase: 01-home-sync-mvp-user-data-sync
verified: 2026-07-04T00:30:00Z
status: passed
score: 15/22 truths verified
behavior_unverified: 7
overrides_applied: 0
re_verification:
  previous_status: human_needed
  previous_score: 15/20
  gaps_closed:

    - "UAT gap 1 (Test 2): first-sync warning leaked folder_sync/rsync-specific wording into orchestrator-level messaging — CLOSED by 01-15 (FirstSyncScope contract + describe_first_sync_scope hook; orchestrator.py contains no folder_sync/rsync literal, grep-verified)."
    - "UAT gap 2 (Test 2): declined confirmation was logged CRITICAL and printed twice ('Sync failed' x2) — CLOSED by 01-16 (SyncAbortedByUser + SessionStatus.ABORTED; single WARNING log, single calm CLI message; caught before the generic except Exception in both orchestrator.run() and cli._async_run_sync)."
    - "UAT gap 3 (Test 2): TUI showed duplicate stacked 'Recent Logs' panels and a skipped step number around a confirmation prompt — CLOSED at the mechanism level by 01-17 (single persistent rich.live.Live with pause()/resume(); resume() forces an immediate redraw). Actual on-terminal appearance still needs a human re-test (see human_verification)."
    - "UAT gap 5: config-sync prompted interactively even under --dry-run, inconsistent with _confirm_first_sync — CLOSED by 01-17 (_handle_no_target_config/_handle_config_diff skip prompting and show a read-only preview under dry_run; sync_config_to_target does not pause the UI in dry-run)."
    - "UAT gap 4 (major): uncoordinated stderr StreamHandler desynced Live's redraw bookkeeping, producing hundreds of duplicate panel/frame writes — CLOSED at the mechanism level by 01-18 (UILogHandler routes all TUI-floor log records through the single event-loop Live.update path; plain stderr StreamHandler only for non-TTY runs). Actual absence of flooding on a live terminal still needs a human re-test (see human_verification) — the plan's own coverage explicitly marks this human_judgment: true, deferred to UAT re-test."
    - "Code-review BLOCKER CR-01: UILogHandler (01-18) fed arbitrary log content (rsync paths/stderr) into Panel(str), which Rich parses as console markup — '[/old]'-style content raised MarkupError on the Live auto-refresh thread and again during Live.stop() teardown, freezing/crashing the display. CLOSED by rendering the log-panel body as a literal rich.text.Text instead of a bare str; regression test drives the real TerminalUI render + stop() with markup-breaking content and asserts no exception."
    - "Code-review WARNING WR-01: a job-level SyncAbortedByUser would have been caught by _execute_jobs's generic except Exception (FAILED result + duplicate CRITICAL log) instead of passing through to run()'s WARNING/ABORTED handler. CLOSED by adding 'except SyncAbortedByUser: raise' ahead of the generic handler. Latent path (no job currently raises this) — verified by code reading only, no automated test exercises it."
    - "Code-review WARNING WR-02: --allow-first-sync CLI help text still hardcoded 'folder_sync' / 'rsync --delete', re-introducing the coupling 01-15 removed from the orchestrator body. CLOSED by rewording to job-agnostic phrasing (grep-clean)."
    - "Code-review WARNING WR-03: config-sync decline path printed its own red abort line, then the CLI printed a second, differently-worded abort line — two messages for one decline, contradicting 01-16's single-message contract. CLOSED by removing the in-module prints; the single CLI except SyncAbortedByUser handler owns the one user-facing line."
    - "Code-review INFO IN-01: resume()'s docstring claimed an immediate forced redraw, but the call omitted refresh=True, so the redraw only happened via the next 10 Hz auto-refresh tick; the existing test slept 0.15s and passed regardless of whether resume() actually forced anything. CLOSED by passing refresh=True and tightening the test to assert the new state with no intervening sleep."
    - "Code-review INFO IN-02: setup_logging used console.is_terminal (stdout) while the confirmer used sys.stdin.isatty() (stdin) to decide interactivity — under mixed redirection the UI/logging and prompt behavior could disagree. CLOSED by a shared is_interactive(console) requiring both stdout and stdin to be TTYs, used by both setup_logging and TerminalUIConfirmer.confirm."
  gaps_remaining: []
  regressions: []
behavior_unverified_items:

  - truth: "On a real interactive terminal, a confirmation pause/resume (first-sync, out-of-order, config-sync) does not leave duplicate stale 'Recent Logs'/status panels in scrollback and does not skip a step number (the visual symptom UAT Test 2 originally reported)"
    test: "Re-run UAT Test 2 (decline) and Test 3 (confirm) on the VM pair: pc-switcher sync <B> from A over a real interactive SSH session, answer n then y, and visually inspect the terminal for stacked panels or a skipped step counter (e.g. '1/11 then 3/11, no 2/11')."
    expected: "Exactly one live region is visible throughout; the step counter increments without skipping; only one 'Sync aborted: ...' line prints on decline (yellow, not red 'Sync failed'); the decline is not attributed to a specific job/transport mechanism."
    why_human: "rich.live.Live's terminal cursor bookkeeping and the visual absence of stacked frames can only be observed on a real TTY; the unit tests (test_pause_resume_reuses_same_live_instance, test_resume_forces_redraw_of_state_mutated_while_paused) prove the internal Live-instance-reuse and forced-redraw mechanism but do not render to a real terminal."

  - truth: "During an interactive sync with routine INFO-level job logs active (e.g. dummy_success + folder_sync), the live display no longer floods with duplicated 'Recent Logs' panel headers and duplicated 0%->100% progress frames (the major bug: 761 duplicate panel headers / 326 duplicate 0% frames observed pre-fix in one run)"
    test: "On pc1, run `pc-switcher sync pc2 --dry-run` with dummy_success enabled (per the debug session's reproduction recipe in .planning/debug/tui-live-progress-flooding.md) and observe the raw terminal output for the duration of the sync."
    expected: "No duplicate 'Recent Logs' panel headers or duplicate progress frames; the display updates in place at 10 Hz with a single coherent frame per refresh."
    why_human: "This is the exact scenario 01-18's own <verification> section and SUMMARY (coverage item D4) explicitly defer to a manual/VM re-test — the fix (UILogHandler routes all TUI-floor logs through the single event-loop Live.update path) is proven at the unit level (no stray stderr StreamHandler while the UI sink is active) but the absence of a live-terminal cursor-desync artifact cannot be reproduced in a unit-test harness."

  - truth: "A→B sync copies configured folders byte-identically with every included file present (ROADMAP SC1)"
    test: "Run pc-switcher sync <target> on machine A with the default /home and /root config; compare md5sum of all included files on both machines after sync."
    expected: "Every included file exists on the target and has the same md5sum as the source."
    why_human: "Requires real rsync-as-root execution over SSH to live VMs; mechanism unchanged by this round's gap-closure plans. The relevant integration test (tests/integration/test_end_to_end_sync.py::TestEndToEndSync::test_core_us_job_arch_as1_job_integration_via_interface) exists and asserts this, but the current branch (29 commits ahead of origin, unpushed) has not triggered a CI/VM run since these commits landed."

  - truth: "File metadata preserved: owner, group, permissions, ACLs, timestamps (ROADMAP SC2)"
    test: "After A->B sync, compare stat/getfacl output on source and target for the same files."
    expected: "Numeric uid/gid, permissions, mtime, and POSIX ACL entries are identical on source and target."
    why_human: "Requires real rsync with --numeric-ids on btrfs VMs; unchanged by this round. Same unpushed-branch caveat as SC1."

  - truth: "Machine-specific items excluded; dev-tool caches included (ROADMAP SC3)"
    test: "After A->B sync, verify .ssh/id_*, .config/tailscale, VS Code cache dirs are absent on target; .config/Code/User/ and .cargo/ are present."
    expected: "Excluded paths absent; explicitly synced dev caches present."
    why_human: "Requires live VM execution. Note: 01-UAT.md's own Gaps section claims 'SC3 dev-tool-cache INCLUSION is not asserted by automation' — this verification found that claim is now stale: commit 9d14f54 ('restore SC3 dev-tool-cache / VS Code inclusion coverage', 2026-07-03 17:33, before 01-UAT.md's last update at 19:20) added inclusion assertions for .cargo/pcsw-cache-marker.txt and .config/Code/User/pcsw-user-marker.json in test_end_to_end_sync.py (lines 596-613), alongside the pre-existing exclusion assertion. The test coverage exists in code; it has simply never been executed against the current HEAD (unpushed branch)."

  - truth: "B->A round-trip propagates all changes byte-identically, exclusions hold in reverse (ROADMAP SC4)"
    test: "After A->B, mutate B (add/modify/delete), run pc-switcher sync A from B, compare A and B state."
    expected: "Additions present on A, modifications byte-identical, deletions absent on A, exclusions honored in reverse."
    why_human: "Requires bidirectional VM execution; unchanged by this round. Same unpushed-branch caveat."

  - truth: "VM integration test automates A->B/mutate/B->A round-trip and asserts criteria 1-4 (ROADMAP SC5)"
    test: "Run: tests/run-integration-tests.sh tests/integration/test_end_to_end_sync.py on the Hetzner pc1/pc2 VMs with the current branch pushed to origin."
    expected: "test_core_us_job_arch_as1_job_integration_via_interface passes end-to-end (job discovery, snapshots, config sync, folder-sync content/metadata/ACL/hardlink/exclusion/SC3-inclusion checks, out-of-order gate, round-trip)."
    why_human: "Requires live Hetzner VMs, branch pushed to origin, and a fresh CI run — the last green integration run (PR #160, 2026-07-03 15:33-15:45) predates all four gap-closure plans (01-15..01-18) and all six code-review-fix commits (dated 2026-07-03 23:xx - 2026-07-04 00:xx); the local branch is 29 commits ahead of origin and unpushed."
human_verification:

  - test: "Re-run UAT Test 2 (First-sync warning — abort) on the VM pair with the fixed code: answer n; confirm a single calm yellow 'Sync aborted: ...' line (no red 'Sync failed', no duplicate CRITICAL log), the warning text names no job or transport mechanism, and the terminal shows one continuous Live region with no skipped step number."
    expected: "Exactly one abort message; log level WARNING not CRITICAL; no duplicate/stale panels; step counter does not skip."
    why_human: "Interactive TTY behavior and Rich Live terminal rendering; the fix is unit-tested at the mechanism level but not observed on a real terminal since these commits landed."

  - test: "Run UAT Test 3 (First-sync warning — confirm): answer y; sync proceeds, B ends up matching A, sync-history updated."
    expected: "Sync completes; the warning's In-scope block lists the configured folder paths and 'rsync --delete' under the folder_sync job's own self-description (not orchestrator-hardcoded)."
    why_human: "Never run since the phase's original UAT pass (marked [pending] in 01-UAT.md); requires a live machine pair."

  - test: "Run UAT Test 4 (clean round-trip alternation): edit-and-sync A->B, edit-and-sync B->A, edit-and-sync A->B again, all without any out-of-order warning."
    expected: "All three syncs proceed silently in the clean case; no confirmation prompt fires; live display remains coherent across all three runs (exercises 01-18's fix under sustained job activity)."
    why_human: "Whole-flow confidence walkthrough on a real machine pair with real edits and timing; never run since UAT started ([pending] in 01-UAT.md)."

  - test: "Run UAT Test 5 (consecutive-push heads-up) and Test 6 (concurrent-sync lock + resume the waiting prompt)."
    expected: "Test 5: the out-of-order/consecutive-push warning appears and waits; Test 6: a second concurrent sync fails fast with the lock-holder message and how-to-unblock guidance, then the waiting sync from Test 5 can be answered and completes normally."
    why_human: "Requires two concurrent interactive invocations racing on the same machine; never run since UAT started ([pending] in 01-UAT.md)."

  - test: "Full VM Integration Test Suite: tests/run-integration-tests.sh tests/integration/test_end_to_end_sync.py after pushing the current branch (or a fresh pre-release tag) to origin."
    expected: "test_core_us_job_arch_as1_job_integration_via_interface passes, exercising byte-identical content, numeric uid/gid, permission bits incl. setuid/setgid/sticky, POSIX ACL, mtime, hard-link/symlink handling, exclusions, ADR-016 runtime excludes, and the restored SC3 inclusion assertions (.cargo, VS Code User) end-to-end on real VMs."
    why_human: "Requires live Hetzner VM infrastructure, a push to origin, and a fresh CI run — the last green run predates all of this round's commits."
---

# Phase 1: Home-Sync MVP Verification Report (Gap-Closure Re-Verification)

**Phase Goal:** A user can run one command to replicate configured folders (`/home` and `/root` by default) from the source machine to the target over rsync-over-SSH, with machine-specific items excluded and file metadata preserved — and the result is proven correct in both directions.

**Verified:** 2026-07-04

**Status:** human_needed — all four UAT gap-closure plans (01-15..01-18) and all six code-review findings (01-REVIEW.md/01-REVIEW-FIX.md) are code-complete, correctly wired, and covered by passing unit/lint/type/spell gates. The remaining work is entirely hands-on: (a) confirming the fixed interactive/TUI behaviors on a real terminal against the exact scenarios UAT originally reported, and (b) the pre-existing ROADMAP SC1-5 VM-level behavioral proof, which additionally now requires the branch to be pushed to origin for a fresh CI/integration run (it has not run since these commits landed).

**Re-verification:** Yes — this is a GAPS-ONLY re-verification after closing the 5 UAT-diagnosed UX/logging gaps (plans 01-15..01-18) plus a full code review and fix cycle (01-REVIEW.md -> 01-REVIEW-FIX.md, 6/6 findings fixed).

## Goal Achievement

### What This Round Fixed (Code-Verified)

| Gap | Plan/Finding | Root Cause | Fix | Verification |
| - | - | - | - |---|
| UAT gap 1 | 01-15 | Orchestrator's first-sync warning read `folder_sync`'s config dict and hardcoded `rsync --delete` | `FirstSyncScope` contract + `SyncJob.describe_first_sync_scope()` hook; orchestrator composes the warning from job self-descriptions | `grep -nE 'folder_sync\|rsync --delete' src/pcswitcher/orchestrator.py` empty; 3 new unit tests incl. a hermetic non-rsync stub job (extensibility) |
| UAT gap 2 | 01-16 | Declined confirmation raised a plain `RuntimeError` caught by the generic `except Exception` -> logged CRITICAL, re-raised, printed again by the CLI | `SyncAbortedByUser` exception + `SessionStatus.ABORTED`; caught before the generic handler in both `run()` and `_async_run_sync`; logs once at WARNING, prints once (calm, yellow) | Read `orchestrator.py:343-351`, `cli.py:350-355`; unit tests assert WARNING-not-CRITICAL and single "aborted"-not-"failed" message |
| UAT gap 3 | 01-17 | `TerminalUI.start()` always built a new `Live`; `Confirmer`/`config_sync` called `stop()`+`start()` around every prompt | Single persistent `Live` with `pause()`/`resume()`; `resume()` forces an immediate redraw | Read `ui.py:123-168`; unit tests prove same-instance reuse across pause/resume and a forced redraw with no intervening sleep |
| UAT gap 5 | 01-17 | `_prompt_new_config`/`_prompt_config_diff` prompted unconditionally even under `--dry-run` | Both short-circuit under `dry_run`, show a read-only preview, return `True`; `sync_config_to_target` only pauses the UI when a prompt will actually happen | Read `config_sync.py:206-259,301-317`; `TestDryRunSkipsPrompting` passes |
| UAT gap 4 (major) | 01-18 | `setup_logging` installed an uncoordinated `logging.StreamHandler(sys.stderr)` that desynced `Live`'s redraw cursor bookkeeping | `UILogHandler` routes TUI-floor log records into the UI's Recent Logs panel via `loop.call_soon_threadsafe`, keeping every `Live.update` on one thread; stderr fallback only for non-TTY | Read `logger.py:226-292,375-383`, `orchestrator.py:202-231`; unit tests prove no stray stderr `StreamHandler` when the UI sink is selected, and the stderr fallback for non-interactive runs |
| Review CR-01 (blocker) | fix `235aa3a` | `UILogHandler` (01-18) fed arbitrary log content into `Panel(str)`, which Rich parses as markup — `[/old]`-style rsync paths raised `MarkupError`, crashing the auto-refresh thread and `Live.stop()` teardown | Log-panel body rendered as a literal `rich.text.Text`, not a bare `str` | Read `ui.py:103-121`; new regression test drives the real `TerminalUI` render + `stop()` with a markup-breaking line, asserts no exception |
| Review WR-01 | fix `612ca98` | `_execute_jobs`'s generic `except Exception` would catch a job-level `SyncAbortedByUser` first, recording FAILED + duplicate CRITICAL | `except SyncAbortedByUser: raise` added before the generic handler | Read `orchestrator.py:934-941`. **No automated test** — latent path, no job currently uses the confirmer inside `execute()`; verified by reading only, matching the fix report's own disclosure |
| Review WR-02 | fix `5d4a7e6` | `--allow-first-sync` CLI help text still hardcoded `folder_sync`/`rsync --delete`, undoing 01-15's job-agnostic messaging at the flag-help layer | Reworded to generic scope language | `grep -nE 'folder_sync\|rsync --delete' src/pcswitcher/cli.py` empty |
| Review WR-03 | fix `0f7f927` | Config-sync decline printed its own red line, then the CLI printed a second, differently-worded line | In-module abort prints removed; the single CLI `except SyncAbortedByUser` handler owns the one line | Read `config_sync.py:215-218,256-259` |
| Review IN-01 | fix `35eaa8a` | `resume()`'s docstring claimed an immediate forced redraw but omitted `refresh=True`; the existing test passed via the 10 Hz auto-refresh tick regardless | `refresh=True` added; test tightened to assert with no intervening sleep | Read `ui.py:148-162`; `test_resume_forces_redraw_of_state_mutated_while_paused` passes and now actually exercises the claim |
| Review IN-02 | fix `70dec56` | `setup_logging` used `console.is_terminal` (stdout) while the confirmer used `sys.stdin.isatty()` (stdin) — could disagree under mixed redirection | Shared `is_interactive(console)` requiring both ends to be TTYs | Read `logger.py:281-292`, `confirmer.py:29,101`; both call sites updated |

### Observable Truths

| # | Truth | Status | Evidence |
| - | - | - | - |
| G1 | First-sync warning is assembled from each in-scope `SyncJob`'s self-description; orchestrator names no job/transport mechanism directly (01-15) | VERIFIED | `grep -nE 'folder_sync\|rsync --delete' src/pcswitcher/orchestrator.py` empty; `_first_sync_scopes()`/`_confirm_first_sync()` at orchestrator.py:497-570 compose from `FirstSyncScope` objects |
| G2 | A future non-rsync job's `FirstSyncScope` flows through the composed warning with zero orchestrator change (extensibility) | VERIFIED | `tests/unit/orchestrator/test_first_sync_scope.py::TestFirstSyncScopesExtensibility::test_stub_non_rsync_job_surfaces_in_warning` passes |
| G3 | Generic fallback phrasing ("all data configured for sync") used when no enabled job describes a scope, naming no transport | VERIFIED | orchestrator.py:543-544; `TestFirstSyncScopesEmptyFallback` passes |
| G4 | Declining a confirmation (out-of-order, first-sync, config-sync — the orchestrator-level decline sites) is logged once at WARNING, never CRITICAL, never printed twice | VERIFIED | orchestrator.py:343-351 (`except SyncAbortedByUser` before generic `except Exception`); cli.py:350-355 (same ordering); `tests/unit/orchestrator/test_user_abort.py`, `tests/unit/cli/test_commands.py::TestSyncAbortedByUserHandling` pass |
| G5 | Genuine unrecoverable failures still log CRITICAL and fail the session (regression) | VERIFIED | orchestrator.py:353-358 unchanged generic handler; full suite green |
| G6 | `TerminalUI` keeps a single persistent `Live` instance across confirmation pause/resume (no fresh `Live` stacking a new region) | VERIFIED | ui.py:123-146; `test_pause_resume_reuses_same_live_instance` passes (behavioral test exercising the exact reuse invariant) |
| G7 | `resume()` forces an immediate redraw of state mutated while paused (fixes the skipped-step-number symptom) | VERIFIED | ui.py:148-162 (`refresh=True`); `test_resume_forces_redraw_of_state_mutated_while_paused` passes with no intervening sleep (tightened per IN-01 fix) |
| G8 | Config sync under `--dry-run` does not prompt interactively, does not pause the UI, shows a read-only preview, and writes nothing | VERIFIED | config_sync.py:206-218,241-245,301-317; `TestDryRunSkipsPrompting` passes |
| G9 | Interactive (both-TTY) runs route TUI-floor log records through `UILogHandler` into the single `Live.update` path; no independent `logging.StreamHandler(sys.stderr)` is attached while the UI sink is active | VERIFIED | logger.py:226-292,372-383; `tests/unit/test_logging.py::TestUILogHandlerRouting` passes, including the "no stderr handler present" guard |
| G10 | Non-interactive / non-TTY runs keep the plain stderr fallback unchanged | VERIFIED | logger.py:375-383 (`use_ui` gate); `test_non_interactive_setup_falls_back_to_stderr`, `test_no_ui_argument_keeps_default_stderr_behavior` pass |
| G11 | CR-01 blocker: log-panel content containing markup-like sequences (e.g. `[/old]`) does not crash the Live render or `Live.stop()` teardown | VERIFIED | ui.py:103-121 (`Text(...)` not bare `str`); `test_log_panel_renders_markup_like_content_literally` drives the real render+stop with a markup-breaking line and asserts no exception — a genuine regression test of the exact crash |
| G12 | WR-02: `--allow-first-sync` help text is job-agnostic | VERIFIED | `grep -nE 'folder_sync\|rsync --delete' src/pcswitcher/cli.py` empty; cli.py:214-224 read |
| G13 | WR-03: config-sync decline prints exactly one abort message, not two | VERIFIED | config_sync.py:215-218,256-259 return `False` silently; read confirms no console.print at decline sites |
| G14 | IN-02: logging setup and the confirmer share one interactivity signal (`is_interactive`, both stdout+stdin TTY) | VERIFIED | logger.py:281-292 (`is_interactive`); confirmer.py:29,101 uses it; logger.py:375 (`setup_logging`) uses it |
| G15 | REQUIREMENTS.md traceability: all 5 phase requirement IDs map to shipped, tested code | VERIFIED | See Requirements Coverage table below — no orphans |
| G16 | Visually, on a real terminal, a confirmation pause/resume leaves no duplicate stale panels and no skipped step number (the actual UAT Test 2 symptom) | PRESENT_BEHAVIOR_UNVERIFIED | The internal mechanism (G6/G7) is unit-tested; a real-TTY observation has not been repeated since the fix — routed to human verification |
| G17 | On a real terminal with routine INFO logs active, the live display no longer floods with duplicated panel headers/progress frames (the major bug) | PRESENT_BEHAVIOR_UNVERIFIED | The routing mechanism (G9/G10) is unit-tested; 01-18's own plan and SUMMARY explicitly defer the flooding-absence observation to a manual/VM re-test (coverage item D4, `human_judgment: true`) |
| SC1 | A->B sync copies configured folders byte-identically (ROADMAP) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism unchanged by this round; integration test exists but unpushed branch has not triggered a fresh CI/VM run |
| SC2 | File metadata preserved (ROADMAP) | PRESENT_BEHAVIOR_UNVERIFIED | Same as SC1 |
| SC3 | Machine-specific items excluded; dev-tool caches included (ROADMAP) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism unchanged; test coverage for the inclusion side exists (commit `9d14f54`, predates 01-UAT.md's own stale "not asserted" gap note) but has not run against current HEAD |
| SC4 | B->A round-trip propagates changes, exclusions hold in reverse (ROADMAP) | PRESENT_BEHAVIOR_UNVERIFIED | Same as SC1 |
| SC5 | VM integration test automates and asserts criteria 1-4 (ROADMAP) | PRESENT_BEHAVIOR_UNVERIFIED | Test exists (`test_core_us_job_arch_as1_job_integration_via_interface`); last green run (PR #160, 2026-07-03 15:33-15:45) predates all of this round's commits |

**Score:** 15/22 truths verified (7 present, behavior-unverified — 2 newly introduced by this round's TUI/logging fixes, 5 carried forward unchanged from the prior VERIFICATION.md as ROADMAP-level VM-execution proof)

### Required Artifacts

| Artifact | Expected | Status | Details |
| - | - | - | - |
| `src/pcswitcher/models.py` | `FirstSyncScope`, `SyncAbortedByUser`, `SessionStatus.ABORTED` | VERIFIED | All three present, exported in `__all__` (lines 11-27, 125-153, 227-234) |
| `src/pcswitcher/jobs/base.py` | `SyncJob.describe_first_sync_scope()` classmethod, defaults `None` | VERIFIED | jobs/base.py:172-195 |
| `src/pcswitcher/jobs/folder_sync.py` | `describe_first_sync_scope()` override, `@override` decorated | VERIFIED | jobs/folder_sync.py:133-155; returns `None` for empty/disabled config, scope for enabled folders |
| `src/pcswitcher/orchestrator.py` | `_first_sync_scopes()`, `_resolve_sync_job_class()` shared helper, `except SyncAbortedByUser` in `run()` and `_execute_jobs`, UI/console created before `setup_logging` | VERIFIED | Lines 202-231 (UI-before-logging), 343-351 (run() abort handler), 456-516 (scope collection), 934-941 (_execute_jobs abort passthrough) |
| `src/pcswitcher/cli.py` | `except SyncAbortedByUser` before generic `except Exception`; job-agnostic `--allow-first-sync` help | VERIFIED | Lines 214-224, 350-359 |
| `src/pcswitcher/ui.py` | Single persistent `Live`, `pause()`/`resume()`, `Text`-wrapped log-panel body, `is_started`-guarded mutators | VERIFIED | Lines 70,103-168,234-288 |
| `src/pcswitcher/confirmer.py` | `PausableUI` declares `pause()`/`resume()`; shared `is_interactive` | VERIFIED | Lines 29-39,101 |
| `src/pcswitcher/config_sync.py` | `pause()`/`resume()` conditional on `should_pause`; dry-run skips prompting; silent decline returns | VERIFIED | Lines 206-218,241-259,301-317 |
| `src/pcswitcher/logger.py` | `LogPanelSink` Protocol, `UILogHandler`, `is_interactive()`, TTY-aware `setup_logging` | VERIFIED | Lines 48-58,226-292,319-418 |
| `tests/unit/orchestrator/test_first_sync_scope.py` | New file, job-agnostic scope tests + extensibility stub | VERIFIED | Exists; 5 tests pass |
| `tests/unit/orchestrator/test_user_abort.py` | New file, abort-path tests | VERIFIED | Exists; tests pass (part of 537-test suite) |
| `tests/unit/test_logging.py` | `TestUILogHandlerRouting` + `_FakeLogPanelSink` | VERIFIED | 3 tests pass |
| `tests/unit/ui/test_terminal_ui.py` | Live-reuse, forced-redraw, and markup-regression tests | VERIFIED | 3 named tests pass (confirmed by direct pytest run in this verification) |

### Key Link Verification

| From | To | Via | Status | Details |
| - | - | - | - |---|
| `orchestrator._first_sync_scopes()` | `SyncJob.describe_first_sync_scope()` | `_resolve_sync_job_class()` dynamic import + class scan, per enabled job in config order | VERIFIED | orchestrator.py:497-516 |
| `orchestrator._execute_jobs` job-level `SyncAbortedByUser` | `run()`'s WARNING/ABORTED handler | `except SyncAbortedByUser: raise` before generic `except Exception` | VERIFIED (code) | orchestrator.py:934-941; **no runtime test** — latent path, no current job raises this |
| `orchestrator.run() except SyncAbortedByUser` | `cli._async_run_sync except SyncAbortedByUser` | re-raise propagates through `main_task` await | VERIFIED | orchestrator.py:343-351 -> cli.py:350-355; `TestSyncAbortedByUserHandling` passes |
| `Confirmer.confirm` | `TerminalUI.pause()/resume()` | `PausableUI` protocol, single `Live` instance | VERIFIED | confirmer.py:122,134 -> ui.py:139-162 |
| `config_sync.sync_config_to_target` | `TerminalUI.pause()/resume()` | `should_pause` flag (never true under `dry_run`) | VERIFIED | config_sync.py:301-317 |
| `logging.QueueListener` background thread | `TerminalUI.add_log_message` | `UILogHandler.emit` -> `loop.call_soon_threadsafe` | VERIFIED | logger.py:248-262; `TestUILogHandlerRouting` drives this under a running loop and asserts delivery |
| `Orchestrator.run()` | `setup_logging(ui=..., console=...)` | UI/console constructed before the `setup_logging` call | VERIFIED | orchestrator.py:202-231; `TestOrchestratorCreatesUiBeforeLogging` passes |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| - | - | - | - |
| Full unit suite | `uv run pytest tests/unit tests/contract -q` | 537 passed, 0 failed | PASS |
| Lint | `uv run ruff check .` | All checks passed | PASS |
| Format | `uv run ruff format --check .` | 85 files already formatted | PASS |
| Type check | `uv run basedpyright` | 0 errors, 0 warnings, 0 notes | PASS |
| Typo check | `uv run codespell` | No findings | PASS |
| CR-01 regression (real render+stop, markup-breaking content) | `uv run pytest tests/unit/ui/test_terminal_ui.py::test_log_panel_renders_markup_like_content_literally -q` | 1 passed | PASS |
| Live-instance reuse across pause/resume (state transition) | `uv run pytest tests/unit/ui/test_terminal_ui.py::test_pause_resume_reuses_same_live_instance -q` | 1 passed | PASS |
| resume() forces redraw with no intervening sleep (state transition) | `uv run pytest tests/unit/ui/test_terminal_ui.py::test_resume_forces_redraw_of_state_mutated_while_paused -q` | 1 passed | PASS |
| No debt markers (TBD/FIXME/XXX) in files touched by 01-15..01-18 or the review-fix commits | `grep -nE "TBD\|FIXME\|XXX" <touched files>` | 1 pre-existing `# TODO: Add config snapshot` at orchestrator.py:190, introduced by plan 01-03 (unrelated to this round, already flagged INFO in the prior VERIFICATION.md) | INFO, not a new blocker |
| VM integration test suite | `tests/run-integration-tests.sh tests/integration/test_end_to_end_sync.py` | SKIPPED — requires live Hetzner VMs; branch is 29 commits ahead of origin (unpushed), so CI has not run against this code | SKIP |

### Requirements Coverage

| Requirement | Source Plans | Description | Status | Evidence |
| - | - | - | - |---|
| REQ-sync-scope-user-data | 01-14, unaffected by 01-15..18 | Sync `/home` + `/root` via generic per-folder mechanism | VERIFIED (mechanism, unchanged) | rsync mirror + `describe_first_sync_scope` additions don't touch transfer logic; behavioral proof still via VMs (SC1) |
| REQ-machine-specific-exclusions | 01-14, unaffected | Never sync `.ssh/id_*`, tailscale, GPU/fontconfig caches | VERIFIED (mechanism, unchanged) | Unaffected by this round; behavioral proof via VMs (SC3) |
| REQ-sync-scope-file-metadata | 01-14, unaffected | Preserve owner/group/permissions/ACLs/timestamps | VERIFIED (mechanism, unchanged) | Unaffected; behavioral proof via VMs (SC2) |
| REQ-manual-sync-workflow | 01-15, 01-16, 01-17 | Single-command trigger; safety check; dry-run preview | VERIFIED | Job-agnostic first-sync warning (01-15); dry-run consistency across all confirmations (01-17) |
| REQ-terminal-ux | 01-15, 01-16, 01-17, 01-18 | Single command; terminal UI; progress; clear errors; truthful audit log | VERIFIED (code); human_needed (visual/live-terminal proof) | All 5 diagnosed UX/logging gaps closed at the code+unit-test level (G1-G14); the actual on-terminal appearance (no duplicate panels, no flooding) is the human_verification item |

No orphaned requirements — all 5 IDs declared across the phase's plans match REQUIREMENTS.md's Phase-1 mapping (lines 76-80), and REQUIREMENTS.md marks all 5 `[x]` complete.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
| - | - | - | - |---|
| `src/pcswitcher/orchestrator.py` | 190 | `config={},  # TODO: Add config snapshot` | INFO (pre-existing, unrelated) | Introduced by plan 01-03, carried forward from the prior VERIFICATION.md; unrelated to this round's gap closure |
| `src/pcswitcher/orchestrator.py` | 934-941 | WR-01 fix (`except SyncAbortedByUser: raise` in `_execute_jobs`) has no automated test and no current caller | INFO | Defensive fix for a latent path; verified by reading only. Documented as such in 01-REVIEW-FIX.md itself. Not a blocker — no job today raises `SyncAbortedByUser` from inside `execute()`, so there is nothing to exercise yet |

No unreferenced TBD/FIXME/XXX markers in any file touched by plans 01-15..01-18 or the six review-fix commits.

### Human Verification Required

#### 1. Re-run UAT Test 2 (First-sync warning — abort) with the fixed code

**Test:** From A, run `pc-switcher sync <B>` on a real interactive terminal against a target with no sync history; answer `n`.

**Expected:** Exactly one calm yellow "Sync aborted: ..." line (no red "Sync failed", no duplicate CRITICAL log line); the warning names no job or transport mechanism directly (the "In scope" block is built from `FolderSyncJob`'s own self-description); the terminal shows one continuous Live region throughout with no duplicate stacked panels and no skipped step number.

**Why human:** Rich `Live` terminal cursor bookkeeping and the visual absence of stacked frames can only be observed on a real TTY. The internal mechanism is unit-tested (Live-instance reuse, forced redraw, single WARNING log, single CLI message) but has not been observed live since the fix.

#### 2. Run UAT Test 3 (First-sync warning — confirm)

**Test:** Re-run `pc-switcher sync <B>` from A; answer `y`.

**Expected:** Sync proceeds; B ends up matching A; A's sync-history records A as source, B as target.

**Why human:** Never run since the phase's original UAT pass (still `[pending]` in 01-UAT.md); requires a live machine pair.

#### 3. Run UAT Test 4 (clean round-trip alternation, whole-flow walkthrough)

**Test:** Edit a file on A, sync to B; edit on B, sync to A; edit on A again, sync to B — three syncs in a row with real edits.

**Expected:** All three proceed silently (no out-of-order warning in the clean case); the live display stays coherent across all three runs — this is the scenario most likely to reproduce (or definitively disprove) the original flooding bug under sustained job activity.

**Why human:** End-to-end confidence walkthrough on a real machine pair with real edits and real timing; never run since UAT started (`[pending]` in 01-UAT.md).

#### 4. Run UAT Test 5 (consecutive-push heads-up) and Test 6 (concurrent-sync lock)

**Test:** From B, `pc-switcher sync <A>` twice without a back-sync (Test 5, leave it waiting at the prompt); while it waits, launch a second `pc-switcher sync <A>` from B in another terminal (Test 6), then return and confirm Test 5's prompt.

**Expected:** Test 5's second sync surfaces the out-of-order/consecutive-push warning and waits; Test 6's concurrent sync fails fast with the lock-holder message and how-to-unblock guidance; confirming Test 5's prompt then completes normally.

**Why human:** Requires two concurrent interactive invocations racing on the same machine; never run since UAT started (`[pending]` in 01-UAT.md).

#### 5. Manual/VM re-test of the live-progress flooding fix (01-18's own deferred check)

**Test:** On pc1, run `pc-switcher sync pc2 --dry-run` with `dummy_success` active (per `.planning/debug/tui-live-progress-flooding.md`'s reproduction recipe) and observe the raw terminal output.

**Expected:** No duplicate "Recent Logs" panel headers, no duplicate progress frames; the display updates in place at 10 Hz.

**Why human:** 01-18's own `<verification>` section and SUMMARY (coverage item D4) explicitly defer this to a manual/VM re-test — the routing mechanism is unit-tested but the absence of a live-terminal cursor-desync artifact needs direct observation.

#### 6. Full VM Integration Test Suite (ROADMAP SC1-5, after pushing to origin)

**Test:** Push the current branch (29 commits ahead of origin) or cut a fresh pre-release tag, then run `tests/run-integration-tests.sh tests/integration/test_end_to_end_sync.py` with live Hetzner VMs.

**Expected:** `test_core_us_job_arch_as1_job_integration_via_interface` passes — byte-identical content, numeric uid/gid, permission bits incl. setuid/setgid/sticky, POSIX ACL, mtime, hard-link/symlink handling, exclusions, ADR-016 runtime excludes, and the SC3 inclusion assertions (`.cargo`, VS Code `User/`) restored by commit `9d14f54`.

**Why human:** Requires live Hetzner VM infrastructure and a push to origin — the last green CI run (PR #160, 2026-07-03 15:33-15:45) predates every commit in this gap-closure round.

### Gaps Summary

No unaddressed code gaps. All 5 UAT-diagnosed UX/logging gaps (job-agnostic first-sync messaging, single-WARNING decline, single-Live pause/resume, dry-run-consistent config sync, UI-routed logging) are implemented, wired, and covered by passing unit tests — including two genuine behavioral regression tests (Live-instance reuse across pause/resume, and the CR-01 markup-crash fix exercised through a real render+`stop()`). The subsequent code review's 1 blocker + 3 warnings + 2 info findings are all fixed and verified by direct code reading (grep-clean for the two hardcoding regressions, silent-return reads for the double-message fix).

Two items remain structurally unverifiable by unit tests and are correctly routed to human verification rather than claimed as passed:

1. **The exact visual symptoms UAT originally reported** (duplicate stale panels, skipped step numbers, live-progress flooding) — the internal mechanisms are fixed and unit-tested, but Rich `Live`'s terminal cursor behavior can only be confirmed by watching a real terminal. 01-18's own plan explicitly defers this (coverage item D4, `human_judgment: true`).
2. **The pre-existing ROADMAP SC1-5 VM-level behavioral proof** — unchanged in nature from the prior VERIFICATION.md, but now additionally blocked on pushing the branch to origin: the local branch is 29 commits ahead of origin (unpushed), so no CI/integration run has exercised any of this round's code, including the review-fix commits.

One process note found during this verification: **01-UAT.md's own Gaps section claims "SC3 dev-tool-cache INCLUSION is not asserted by automation," but this is stale** — commit `9d14f54` ("restore SC3 dev-tool-cache / VS Code inclusion coverage," 2026-07-03 17:33) added exactly that assertion to `test_end_to_end_sync.py` (lines 596-613) before 01-UAT.md's own last update timestamp (19:20 the same day). The coverage exists in code; it has simply never been executed against current HEAD. Recommend reconciling 01-UAT.md's Gaps section when the phase is next touched, but this is not a new gap introduced by this round.

One latent, untested (but correctly implemented) code path: WR-01's `except SyncAbortedByUser: raise` in `_execute_jobs` guards against a job raising `SyncAbortedByUser` from inside `execute()` — no job does this today, so there is no automated test and no way to manually exercise it either. Not a blocker; flagged as INFO for future awareness (e.g. if a future job wires the confirmer into its own `execute()`).

---

_Verified: 2026-07-04_
_Verifier: Claude (gsd-verifier)_
