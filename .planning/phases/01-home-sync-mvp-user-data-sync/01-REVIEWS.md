---
phase: 01
reviewers: [antigravity, codex]
reviewers_attempted: [gemini, codex, antigravity]
reviewers_failed:
  gemini: "auth — IneligibleTierError: Gemini Code Assist individual tier no longer supported by gemini-cli"
reviewed_at: 2026-07-01T08:27:45Z
plans_reviewed: [01-07-PLAN.md, 01-08-PLAN.md, 01-09-PLAN.md]
scope: gap_closure
actionable_concerns: true
---

# Cross-AI Plan Review — Phase 01 (Gap Closure)

Reviewed the three gap-closure plans (01-07, 01-08, 01-09) with two external reviewers plus the in-pipeline gsd-plan-checker. **Gemini** failed authentication (individual tier no longer supported by gemini-cli). **Antigravity** approved all three plans with no concerns. **Codex** (re-run focused, converged) raised two HIGH concerns with `file:line` evidence — both point at the divergence filter being too narrow/too broad in ways that could either mask real target changes or reintroduce false divergence. The two external reviewers genuinely diverge, and codex's key mechanism was independently confirmed against live source (see the verification note under Codex).

---

## Antigravity Review

# Cross-AI Plan Review: Phase 01 GAP CLOSURE

I have thoroughly reviewed the three gap-closure plans (`01-07`, `01-08`, and `01-09`). The proposed changes directly and effectively address the findings from `01-VERIFICATION.md` while remaining strictly aligned with the project's architectural constraints (such as asyncio concurrency, Python 3.14 via `uv`, custom stdlib logging, and the immutable ADR foundation).

Here is my detailed assessment of each plan:

### **Plan 01-07: Divergence Guard Correctness (CR-01, CR-02, WR-02)**
**Status:** **APPROVED**

* **CR-01 (False Divergence Blocker):** Filtering out `.local/share/pc-switcher/` from the `btrfs find-new` output is the correct systemic fix. It addresses the empty-prefix over-match problem for the `/home` subvolume without compromising the guard's integrity for actual user data. Deriving the match string from `sync_history.HISTORY_DIR` keeps it DRY.
* **CR-02 (Fail-Closed on Unverifiable):** Introducing the `DivergenceStatus` enum (`CLEAN`, `DIVERGED`, `UNVERIFIABLE`) is a robust way to model the state machine. Emitting a `ValidationError` when a baseline exists but the state cannot be verified rightfully enforces the data-loss-linchpin requirement. It properly respects the fail-open fallback for the initial never-synced state (RESEARCH Open Q3).
* **WR-02 (Non-fatal baseline capture):** Defining `sync_history.UNKNOWN_GENERATION = -1` as a sentinel value is an elegant solution. By wrapping the capture in a `try/except` and recording the sentinel on failure, you ensure that a successful data transfer isn't erroneously flagged as a job failure, while simultaneously ensuring the *next* sync run knows to fail-closed rather than bypass the guard.
* **Testing:** The addition of `test_toolstate_change_under_synced_root_not_divergence` (the VM-executable scenario) provides a solid regression guard against future over-matches.

### **Plan 01-08: Interrupt & Progress UX (IN-01, IN-02)**
**Status:** **APPROVED**

* **IN-01 (Dead SIGINT code):** Removing the unreachable `asyncio.wait_for(asyncio.shield(asyncio.sleep(0)), ...)` construct is correct. Since the orchestrator's cancellation handles cleanup internally within the `finally` block of the aborted task, the CLI loop doesn't need to spoof a grace period. Updating the terminal messaging to truthfully reflect this behavior improves the UX.
* **IN-02 (Progress bar total):** Adding `TerminalUI.set_total_steps` allows the orchestrator to correct the denominator after Phase 4 job discovery. Adjusting the initial estimate to only count `enabled` jobs, and then cementing the true total of *valid* and *enabled* jobs, ensures the progress bar cleanly hits 100%.

### **Plan 01-09: Observability & TOCTOU (WR-01, IN-03, WR-03)**
**Status:** **APPROVED**

* **Dependency handling:** Appropriately sequenced as Wave 2, depending on `01-07` so it can reuse the repaired `_check_divergence` function.
* **WR-01 (Transferred bytes parsing):** Adding the `_parse_size_to_bytes` helper to convert rsync's K/M/G/T suffixes into exact byte integers correctly captures the payload sizes, making the `bytes_transferred` metric in the INFO audit log actually useful.
* **IN-03 (Itemize change types):** Adding `c` (created items) and `h` (hard links) to the `_stream_rsync` inclusion set accurately reflects rsync `%i` behavior and guarantees that these legitimate filesystem changes aren't silently dropped from the FULL logs.
* **WR-03 (Pre-transfer TOCTOU re-check):** Calling `await self._check_divergence(folder)` immediately prior to spawning the destructive `rsync --delete` process in Phase 9 successfully closes the Time-of-Check to Time-of-Use window from Phase 4. It intelligently inherits the `--allow-divergence` and `--dry-run` logic from `01-07` without duplicating logic.

### **Conclusion**
The three plans cleanly resolve all 1 BLOCKER, 4 WARNINGS, and 3 INFO findings from the Phase 01 verification report. They do not introduce any scope creep and respect the strict ADR mandates (no interactive prompts, zero external bash dependencies for orchestration, etc.).

**Recommendation:** Proceed with the execution of these plans.

---

## Codex Review

**Summary**

The three plans mostly target the right mechanisms and the wave ordering is sound: 01-07 and 01-08 do not overlap in production files, while 01-09 correctly waits for 01-07's divergence-state refactor before touching `folder_sync.py`. The main risk is that the proposed CR-01 filter is too broad, and 01-09's new execute-time re-check can introduce a new false-divergence path after Phase 8 config sync unless additional pc-switcher-owned target writes are accounted for.

**Strengths**

- 01-07 correctly identifies the existing fail-open behavior: `_check_divergence()` only calls `_target_diverged_since()` when a stored baseline exists at `src/pcswitcher/jobs/folder_sync.py:355-365`, but `_target_diverged_since()` currently returns `False` on unresolved subvolume and failed `find-new` at `src/pcswitcher/jobs/folder_sync.py:308-330`. The proposed `UNVERIFIABLE` state closes CR-02 while preserving the no-baseline branch at `src/pcswitcher/jobs/folder_sync.py:355-363`.
- 01-07's sentinel approach fits the current history API. `get_target_generation()` already preserves any integer value at `src/pcswitcher/sync_history.py:204-232`, and `set_target_generation()` writes nested integer markers without special casing at `src/pcswitcher/sync_history.py:237-269`, so `UNKNOWN_GENERATION = -1` can round-trip cleanly.
- 01-07 correctly targets the baseline-capture failure site. Today `execute()` records baselines only after all rsyncs succeed at `src/pcswitcher/jobs/folder_sync.py:507-522`, and `_get_subvolume_generation()` can raise after transfer at `src/pcswitcher/jobs/folder_sync.py:270-292`. Recording a sentinel instead of failing the job closes WR-02's "data copied but next run unguarded" outcome.
- 01-08 correctly fixes the progress denominator source. The UI total is currently computed from all configured jobs at `src/pcswitcher/orchestrator.py:193-200`, while discovery skips disabled jobs at `src/pcswitcher/orchestrator.py:517-525` and final progress uses only returned jobs at `src/pcswitcher/orchestrator.py:266-274`. Setting the total after `jobs = await _discover_and_validate_jobs()` matches the executed step count.
- 01-08 correctly removes dead SIGINT cleanup code. The CLI promises a timeout at `src/pcswitcher/cli.py:297-312`, but the bounded wait awaits only `asyncio.sleep(0)` at `src/pcswitcher/cli.py:340-352`. The real cleanup runs in `Orchestrator.run()`'s `finally` at `src/pcswitcher/orchestrator.py:288-304`.
- 01-09 correctly targets the byte-count bug. `_PROGRESS2_RE` currently does not capture the leading size token at `src/pcswitcher/jobs/folder_sync.py:25-27`, and `_stream_rsync()` initializes `bytes_xfr = 0` but never updates it at `src/pcswitcher/jobs/folder_sync.py:405-447`.
- 01-09 correctly targets dropped itemize types. `_stream_rsync()` logs only `>`, `<`, `*`, and `.` lines in both main and flush paths at `src/pcswitcher/jobs/folder_sync.py:433-445`; adding `c` and `h` in both places closes IN-03.
- The planned integration coverage is real VM coverage, not a fake unit replacement. `tests/integration/conftest.py:46-53` auto-applies the `integration` marker, and `tests/integration/conftest.py:56-69` exits without VM env vars. The existing folder sync suite uses actual pc1/pc2 executors and real `pc-switcher sync` commands, e.g. `tests/integration/test_folder_sync.py:241-250` and `tests/integration/test_folder_sync.py:489-498`.

**Concerns**

- **HIGH**: 01-07's CR-01 filter is over-broad if implemented "regardless of prefix." Current divergence scoping counts a changed file only when `prefix == ""` or when the changed path is under the folder prefix at `src/pcswitcher/jobs/folder_sync.py:338-342`. `HISTORY_DIR` is specifically `~/.local/share/pc-switcher` at `src/pcswitcher/sync_history.py:45-48`. Ignoring any line containing `.local/share/pc-switcher/` even when it is inside a non-empty synced folder would mask real user data changes in arbitrary folder-sync roots. The proposed integration scenario deliberately writes `<tdir>/.local/share/pc-switcher/sync-history.json` under the dedicated sync root, but the current test config syncs only that dedicated directory at `tests/integration/test_folder_sync.py:48-77`; that path is not actual pc-switcher state. This would prove the dangerous over-filter, not the default `/home` bug.
- **HIGH**: 01-09's execute-time re-check can create a new false-divergence path after config sync. The orchestrator runs config sync in Phase 8 before job execution at `src/pcswitcher/orchestrator.py:261-268`. Config sync can write `~/.config/pc-switcher/config.yaml` on the target at `src/pcswitcher/config_sync.py:301-316`, via paths defined/read at `src/pcswitcher/config_sync.py:30-39`. For default `/home`, `_target_diverged_since()` treats any non-empty changed path as divergence when `prefix == ""` at `src/pcswitcher/jobs/folder_sync.py:338-342`. 01-07 filters only `.local/share/pc-switcher/`, so 01-09 may block a valid sync after pc-switcher itself updates target config.
- **MEDIUM**: The planned CR-01 VM test does not reproduce the actual default `/home` empty-prefix failure. Existing integration tests intentionally use a safe dedicated directory, not `/home`, at `tests/integration/test_folder_sync.py:19-21` and `tests/integration/test_folder_sync.py:43-77`. The current round-trip test notes pc-switcher metadata writes are outside that prefix at `tests/integration/test_folder_sync.py:485-488`. A new test that writes a fake `.local/share/pc-switcher` inside the test directory exercises the filter, but it does not prove actual pc-switcher state under `~/.local/share/pc-switcher` is ignored only for the default empty-prefix case.
- **LOW**: 01-07 says to catch `RuntimeError` from baseline capture, but `_get_subvolume_generation()` can also raise `ValueError` if the `Generation:` line exists but is not parseable at `src/pcswitcher/jobs/folder_sync.py:289-292`. This is unlikely with real `btrfs`, but the plan's "robust baseline" goal would be more complete if the capture wrapper handled that parse failure too.
- **LOW**: 01-08's `TerminalUI.set_total_steps()` should update the live render, not only assign the field. `set_current_step()` updates `_current_step` and refreshes live UI at `src/pcswitcher/ui.py:226-234`; a matching setter should do the same for consistency, even though the orchestrator will likely call `set_current_step(4)` immediately after correcting the total.

**Suggestions**

- Narrow the CR-01 filter. Prefer filtering pc-switcher-owned paths only when they are outside the folder prefix or when the folder root is the subvolume root (`prefix == ""`). For `/home`, ignore paths like `<user>/.local/share/pc-switcher/...`; do not ignore `<prefix>/.local/share/pc-switcher/...` for arbitrary synced folders.
- Update 01-07's integration scenario wording. Keep the empty-prefix `/home` behavior in unit tests, and for VM safety either assert the real target history path is outside the test-dir prefix and therefore ignored by existing prefix logic, or explicitly state that full default `/home` remains a manual/live verification item. Do not require a test that proves fake tool state under user data is ignored.
- Coordinate 01-09 with Phase 8 config sync. Include `~/.config/pc-switcher/config.yaml` (and any other pc-switcher-owned target write from Phases 7-8) in a narrowly scoped tool-state filter for the default `/home` (empty-prefix) case, since the execute-time re-check cannot run before Phase 8 config writes.
- Add unit coverage for 01-09's config-sync interaction: baseline present, `/home` empty prefix, `find-new` contains `<user>/.config/pc-switcher/config.yaml`, and execute-time re-check should not block if that write is considered tool state.
- Have `set_total_steps()` mirror `set_current_step()` by refreshing `_live` when active.

**Risk Assessment**

Overall risk: **MEDIUM-HIGH**. The plans address the right shipped gaps, and most mechanisms are well aligned with the current code. The elevated risk comes from divergence filtering: a too-broad `.local/share/pc-switcher` exclusion can hide real target changes, while 01-09's new re-check can observe Phase 8's target config write and reintroduce a false divergence for the default `/home` config. These are fixable in the plans before implementation.

### Orchestrator verification note (Claude, this session)

Codex's second HIGH concern was independently checked against live source and **confirmed**:

- `src/pcswitcher/orchestrator.py:261` runs "Phase 8: Sync config from source to target" *before* `src/pcswitcher/orchestrator.py:266-268` "Phase 9: Execute sync jobs" (`_execute_jobs`).
- `src/pcswitcher/config_sync.py:194` (`_copy_config_to_target`) writes the target's `~/.config/pc-switcher/config.yaml` (remote path at `config_sync.py:37`), gated on config difference / missing target config (`config_sync.py:171`).

So on a sync where the config differs, Phase 8 writes `/home/<user>/.config/pc-switcher/config.yaml`, and 01-09's Phase-9 re-check would see it via `find-new`. Because 01-07's filter covers only `.local/share/pc-switcher/`, this would be flagged as false divergence for the default `/home` config. This is a real gap the plans would introduce — the tool-state filter must also account for `.config/pc-switcher/` (and any other pc-switcher-owned Phase 7-8 target write). Codex's first HIGH (prefix-scoping) and the LOW findings (`ValueError` also raisable; `set_total_steps` should refresh `_live`) are also valid.

---

## Consensus Summary

Two external reviewers with **genuinely divergent verdicts**: Antigravity approved all three plans with no concerns; Codex (file:line-grounded) rated overall risk MEDIUM-HIGH with two HIGH concerns. Where they overlap, they agree the plans target the right mechanisms and the wave ordering is sound. The disagreement is about completeness of the divergence-filter design — and Codex's decisive concern was confirmed against source, so it carries the weight.

### Agreed Strengths (Antigravity + Codex + gsd-plan-checker)
- The plans map to the right code sites and mechanisms (CR-01/CR-02/WR-02 in `folder_sync.py`; IN-01/IN-02 in `cli.py`/`orchestrator.py`; WR-01/IN-03/WR-03 in `folder_sync.py`).
- `DivergenceStatus` enum (CLEAN/DIVERGED/UNVERIFIABLE) cleanly closes CR-02 while preserving fail-open for the never-synced case (RESEARCH Open Q3).
- `UNKNOWN_GENERATION = -1` sentinel round-trips cleanly through the existing `sync_history` API and closes WR-02.
- Wave ordering (01-07 ∥ 01-08; 01-09 depends on 01-07) is correct and conflict-free.
- The new integration coverage is real VM coverage (auto `integration` marker; real pc1/pc2 executors), not a faked unit substitute.

### Agreed Concerns / Actionable (raised by Codex, verified)
- **[HIGH] Tool-state filter is incomplete/over-broad.** It must (a) not fire for `<prefix>/.local/share/pc-switcher/...` inside arbitrary non-empty synced roots (would mask real user changes), and (b) also cover `~/.config/pc-switcher/config.yaml` written by Phase 8 config sync — otherwise 01-09's Phase-9 re-check reintroduces false divergence for default `/home`.
- **[MEDIUM] The CR-01 integration scenario proves the filter, not the real bug.** A test writing fake tool state under the dedicated test dir does not exercise the actual `~/.local/share/pc-switcher` empty-prefix `/home` failure; the plan should keep that as a unit test + explicit manual/live verification item.
- **[LOW] Baseline-capture catch should also handle `ValueError`** (unparseable `Generation:` line), not only `RuntimeError`.
- **[LOW] `set_total_steps()` should refresh `_live`** like `set_current_step()`.

### Divergent Views
- Antigravity: "proceed with execution." Codex: revise the filter design first (MEDIUM-HIGH risk). Resolution: Codex's config-sync concern is source-confirmed, so the plans should be revised before execution.

### Recommendation
Run `/gsd-plan-phase 01 --reviews` to fold these findings into 01-07 and 01-09 (broaden the tool-state filter to cover Phase 7-8 pc-switcher-owned target writes and scope it to the empty-prefix/subvolume-root case; adjust the CR-01 test framing; widen the exception catch; refresh `_live` in the new setter). The IN-01/IN-02/WR-01/IN-03 items and the overall approach are sound and unaffected.
