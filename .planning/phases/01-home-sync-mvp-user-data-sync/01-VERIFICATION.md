---
phase: 01-home-sync-mvp-user-data-sync
verified: 2026-07-01T12:00:00Z
status: gaps_found
score: 9/16
behavior_unverified: 5
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 8/14
  gaps_closed:
    - "T6 (D-07): False divergence from depth-1 pc-switcher tool-state writes (sync-history.json, config.yaml) — closed by plan 07's tool-state filter (depth-1 case verified by unit tests D2/D3)"
    - "Guard fails open when find-new fails and baseline exists (old CR-02) — closed by DivergenceStatus.UNVERIFIABLE + fail-closed path in plan 07"
    - "Baseline capture failure aborts successful sync and disables next run's guard (WR-02) — closed by UNKNOWN_GENERATION sentinel in plan 07"
    - "Dead asyncio.wait_for SIGINT code and dishonest interrupt messaging (IN-01) — closed by plan 08"
    - "Progress bar never reaches 100% with disabled jobs (IN-02) — closed by plan 08"
    - "bytes_transferred always 0 in per-folder summary (WR-01) — closed by plan 09"
    - "c/h rsync itemize change types silently dropped from FULL logging (IN-03) — closed by plan 09"
    - "Phase-4 to Phase-9 TOCTOU window in divergence guard (WR-03) — pre-transfer re-check added in plan 09"
  gaps_remaining: []
  regressions:
    - "CR-01 (new): tool-state filter in _target_diverged_since uses unanchored substring test; user files nested under any .config/pc-switcher/ or .local/share/pc-switcher/ subpath at depth > 1 are silently excluded from divergence detection — false-negative data-loss path"
    - "CR-02 (new): pre-transfer re-check (plan 09/WR-03) runs in Phase 9 AFTER Phase 7 install_on_target; install.sh writes ~/.local/bin/uv, ~/.local/share/uv/tools/pcswitcher/, ~/.local/bin/pc-switcher under the synced @home subvolume; these paths are not excluded by the tool-state filter, so every upgrade-then-sync run is falsely blocked with a DIVERGED error"
gaps:
  - truth: "T15: Tool-state filter excludes only pc-switcher-owned paths at the expected depth; user files nested under a .config/pc-switcher/ or .local/share/pc-switcher/ subpath at any greater depth are NOT silently masked"
    status: failed
    reason: >
      The empty-prefix branch of _target_diverged_since (folder_sync.py:395) uses
      `any(token in line for token in tool_state_tokens)` where tokens are
      `"/.local/share/pc-switcher/"` and `"/.config/pc-switcher/"` (lines 381-383).
      This is an unanchored substring test across the whole find-new output line.
      btrfs find-new emits lines like `inode ... flags UNKNOWN janfr/dotfiles/.config/pc-switcher/config.yaml`.
      A real user file at `janfr/dotfiles/.config/pc-switcher/config.yaml` (common with chezmoi/yadm/stow
      tracking a pc-switcher config copy) contains the substring `/.config/pc-switcher/` and is silently
      skipped — classified CLEAN — so the guard proceeds and `rsync --delete` destroys the user's diverged
      data. The existing tests only cover depth-1 paths (`janfr/.config/pc-switcher/config.yaml`).
    artifacts:
      - path: "src/pcswitcher/jobs/folder_sync.py"
        issue: "Lines 381-383: tokens built via lstrip; line 395: `any(token in line ...)` is unanchored substring check — must be an anchored path-prefix test on the last whitespace-delimited field of the find-new line"
    missing:
      - >
        Extract the path field from the find-new line (final whitespace-delimited token) and test
        with an anchored regex, e.g.: `_TOOL_STATE_RE = re.compile(r"^[^/]+/(?:\.local/share|\.config)/pc-switcher/")`.
        Use `path = line.rsplit(" ", 1)[-1]` and `_TOOL_STATE_RE.match(path)` instead of the
        unanchored `token in line` check. The match must still derive the state-dir segments
        from `sync_history.HISTORY_DIR` and `config_sync.CONFIG_REMOTE_DIR` (single source of truth).
      - >
        Add a unit test with a find-new line whose path is `janfr/dotfiles/.config/pc-switcher/config.yaml`
        (depth > 1) and assert it IS reported as DIVERGED, not CLEAN (proves the anchoring).

  - truth: "T16: The pre-transfer re-check in execute() does not false-positive after Phase-7 install_on_target writes upgrade artifacts under the synced @home subvolume"
    status: failed
    reason: >
      Plan 09's WR-03 pre-transfer re-check calls _check_divergence(folder) in execute()
      (folder_sync.py:577) — in orchestrator Phase 9. Phase 7 (_install_on_target_job(),
      orchestrator.py:265) runs BEFORE Phase 9 (line 275). install.sh writes:
        - `$HOME/.local/bin/uv` (uv bootstrap, install.sh:111-114)
        - `$HOME/.local/share/uv/tools/pcswitcher/...` (uv tool install, install.sh:191-200)
        - `$HOME/.local/bin/pc-switcher` (binary from uv tool install)
      All land under janfr/.local/bin/ and janfr/.local/share/uv/ on the @home subvolume.
      The tool-state filter (lines 381-383) only excludes `/.local/share/pc-switcher/`
      and `/.config/pc-switcher/`. It does NOT exclude .local/bin/ or .local/share/uv/.
      install_on_target runs whenever target_version != source_version (install_on_target.py:70-75).
      After Phase 7 upgrades the target, the Phase-9 re-check calls btrfs find-new with the
      pre-upgrade stored generation and sees the install artifacts as changed files, returns
      DIVERGED, logs CRITICAL, and raises RuntimeError — aborting the sync before rsync runs.
      Every upgrade-then-sync (every version bump in production; every sync during active
      development where the dev version changes between machines) is falsely blocked.
      The only escape is --allow-divergence, which disables the divergence guard entirely.
    artifacts:
      - path: "src/pcswitcher/jobs/folder_sync.py"
        issue: "Lines 569-586: pre-transfer re-check reuses stored gen from last sync (not from post-Phase-7 state); tool-state filter does not cover install artifacts"
      - path: "src/pcswitcher/orchestrator.py"
        issue: "Phase 7 (_install_on_target_job, line 265) writes to @home BEFORE Phase 9 (_execute_jobs, line 275); install artifacts are in the re-check window"
      - path: "install.sh"
        issue: "Lines 111-114, 191-200: writes ~/.local/bin/uv, ~/.local/share/uv/tools/pcswitcher/..., ~/.local/bin/pc-switcher under the user's @home subvolume"
    missing:
      - >
        Option A (preferred — pairs with CR-01 fix): Broaden the anchored tool-state regex to
        also exclude pc-switcher's install footprint at the expected depth for the empty-prefix case:
        `_TOOL_STATE_RE = re.compile(r"^[^/]+/(?:\.local/share/pc-switcher|\.config/pc-switcher|\.local/bin/(?:pc-switcher|uv)|\.local/share/uv)(?:/|$)")`.
        This allows future install footprint changes to be tracked in one place.
      - >
        Option B (more robust): After Phase-7 and Phase-8 complete but before Phase-9 executes,
        record the current @home generation as the re-check baseline (replacing stored gen from last sync
        as the re-check's starting point). Then only user writes in the narrow Phase-8-to-Phase-9 window
        trigger the re-check — all Phase-7/8 writes from pc-switcher's own pipeline are outside the window.
      - >
        Add a unit test with find-new output containing `janfr/.local/bin/pc-switcher` and
        `janfr/.local/share/uv/tools/pcswitcher/lib/python3.14/...` lines and assert the re-check
        in execute() does NOT block (rsync is still spawned).
behavior_unverified_items:
  - truth: "A→B sync copies configured folders byte-identically with every included file present (ROADMAP SC1)"
    test: "Run pc-switcher sync <target> on a machine with the default /home and /root config; compare md5sum of all included files on both machines after sync"
    expected: "Every included file exists on the target and has the same md5sum as the source"
    why_human: "Requires real rsync-as-root execution over SSH to live VMs; cannot be simulated with unit test mocks"
  - truth: "File metadata preserved: owner, group, permissions, ACLs, timestamps (ROADMAP SC2)"
    test: "After A→B sync, run stat -c '%u %g %a' + stat -c '%Y' + getfacl on source and target for the same files; compare outputs"
    expected: "Numeric uid/gid, permissions, mtime, and POSIX ACL entries are identical on source and target"
    why_human: "Requires real rsync with --numeric-ids on btrfs VMs with cross-owner files; unit tests only verify flag construction"
  - truth: "Machine-specific items excluded; dev-tool caches included (ROADMAP SC3)"
    test: "After A→B sync, verify .ssh/id_*, .config/tailscale, VS Code cache dirs are absent on target; .config/Code/User/ and .cache/uv/ are present"
    expected: "Excluded paths absent; explicitly synced dev caches present"
    why_human: "Requires live VM execution to verify rsync --filter rules actually suppress or pass the right files"
  - truth: "B→A round-trip propagates all changes byte-identically, exclusions hold in reverse (ROADMAP SC4)"
    test: "After A→B, mutate B (add/modify/delete), run pc-switcher sync A from B, compare A and B state"
    expected: "Additions present on A, modifications byte-identical (md5sum), deletions absent on A (propagated by --delete), exclusions honored in reverse"
    why_human: "Requires bidirectional VM execution and the same metadata checks as SC2"
  - truth: "VM integration test automates A→B/mutate/B→A round-trip and asserts criteria 1-4 (ROADMAP SC5)"
    test: "Run: tests/run-integration-tests.sh tests/integration/test_folder_sync.py on the Hetzner pc1/pc2 VMs with the current branch pushed to origin"
    expected: "All three integration tests pass: TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions, TestFolderSyncRoundTrip::test_round_trip_and_no_false_divergence, TestFolderSyncRoundTrip::test_divergence_guard_and_dry_run"
    why_human: "Requires live Hetzner VMs, HCLOUD_TOKEN + VM env vars set, branch pushed to origin; cannot run offline"
human_verification:
  - test: "A→B Byte-Identical Content Sync (ROADMAP SC1): Run pc-switcher sync <B>; compare md5sum of all included files"
    expected: "Every included file exists on B and has the same md5sum as A; excluded files (.ssh/id_*, .config/tailscale, VS Code cache dirs) absent from B"
    why_human: "Requires live rsync-as-root over SSH to real btrfs VMs"
  - test: "File Metadata Preservation (ROADMAP SC2): After A→B sync, compare stat outputs and getfacl on both machines"
    expected: "Numeric uid/gid, permissions, mtime, POSIX ACL entries, and hard-link inode sharing are identical between A and B for every synced file"
    why_human: "Requires real rsync with --numeric-ids and -aA on VMs with files owned by multiple users"
  - test: "Round-Trip Propagation (ROADMAP SC4): After A→B, mutate B (add/modify/delete), then run B→A; verify A reflects all mutation types"
    expected: "Addition present on A, modified file has new content, deleted file absent on A, excluded file absent on A, metadata preserved"
    why_human: "Requires bidirectional VM execution and deletion propagation via --delete across real SSH"
  - test: "Default /home two-consecutive-sync manual/live check (D-07): After CR-01 and CR-02 are fixed, on a real machine pair with default /home config, run pc-switcher sync <target> twice in a row without --allow-divergence"
    expected: "Second sync exits 0; no 'divergence' in output; note that this test must be re-run AFTER the CR-01 and CR-02 gap-closure plans complete"
    why_human: "Requires destructive --delete mirror of real /home on a real machine pair; CR-01 and CR-02 must be fixed first"
  - test: "Full VM Integration Test Suite: tests/run-integration-tests.sh tests/integration/test_folder_sync.py"
    expected: "All three tests pass: TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions, TestFolderSyncRoundTrip::test_round_trip_and_no_false_divergence, TestFolderSyncRoundTrip::test_divergence_guard_and_dry_run"
    why_human: "Requires live Hetzner VM infrastructure and SSH access"
---

# Phase 1: Home-Sync MVP Verification Report

**Phase Goal:** A user can run one command to replicate configured folders (`/home` and `/root` by default) from the source machine to the target over rsync-over-SSH, with machine-specific items excluded and file metadata preserved — and the result is proven correct in both directions. The job is a generic folder-sync mechanism (per-folder include/exclude), usable for any path; `/root` is included because rsync must run as root anyway to preserve cross-owner files.

**Verified:** 2026-07-01

**Status:** gaps_found — 2 NEW BLOCKERS (CR-01: unanchored tool-state filter → false-negative data loss; CR-02: Phase-7 install artifacts not excluded from pre-transfer re-check → false-positive on every upgrade), 5 runtime behaviors require VM execution

**Re-verification:** Yes — after gap-closure plans 07/08/09 (completed 2026-07-01)

## Goal Achievement

### What Gap-Closure Plans Fixed

Plans 07, 08, and 09 addressed all 8 findings from the previous verification and review cycle:

| Item | Closed By | Evidence |
|------|-----------|---------|
| T6 (D-07): false divergence from sync-history.json / config.yaml depth-1 writes | Plan 07 | `DivergenceStatus`, tool-state filter, unit tests D2/D3 pass |
| Old CR-02: guard fails open when find-new fails with existing baseline | Plan 07 | `UNVERIFIABLE` + fail-closed path, `test_unverifiable_with_baseline_fails_closed` passes |
| WR-02: baseline capture failure aborts successful sync | Plan 07 | `UNKNOWN_GENERATION` sentinel + non-fatal execute() loop |
| IN-01: dead SIGINT wait_for construct | Plan 08 | `asyncio.wait_for/shield/sleep(0)` removed from cli.py |
| IN-02: progress bar never 100% | Plan 08 | `set_total_steps()` added; orchestrator corrects total after Phase 4 |
| WR-01: bytes_transferred always 0 in summary | Plan 09 | `_parse_size_to_bytes()` + updated `_PROGRESS2_RE` group 1 |
| IN-03: c/h change types dropped from FULL logging | Plan 09 | Change-type set extended to include `c` and `h` in `_stream_rsync` |
| WR-03 (TOCTOU Phase-4 to Phase-9) | Plan 09 | Pre-transfer `_check_divergence` call in `execute()` before rsync spawn |

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| T1 | ADR-013 (rsync-over-SSH transport) and ADR-014 (dry-run contract) are Accepted; both in `docs/adr/_index.md` | VERIFIED | Both ADR files exist, `Status: Accepted`, correct Implementation Rules; both indexed |
| T2 | Config schema accepts `folder_sync`; default config ships `/home`+`/root` with D-11 exclusions; loads cleanly | VERIFIED | Schema has `folder_sync` object; `Configuration.from_yaml` returns correct values and excludes |
| T3 | `--allow-divergence` plumbed CLI→Orchestrator→JobContext; dry-run skips sync-history update; `sync_history` exposes `get/set_target_generation` with merge-preserving writes | VERIFIED | CLI help confirms; `Orchestrator.__init__` param confirmed; `if not self._dry_run:` guard at orchestrator.py:290 |
| T4 | `FolderSyncJob` with `name='folder_sync'` exported from `pcswitcher.jobs`; `validate()` checks sudo rsync, acl, and active folder existence | VERIFIED | Import succeeds; validate() steps confirmed; unit/contract tests pass |
| T5 | Divergence guard blocks when target changed since last sync; allows under `--allow-divergence` or `--dry-run` (WARNING + no error) | VERIFIED | `_check_divergence` returns `ValidationError` for DIVERGED/UNVERIFIABLE in normal mode; returns `None` + logs WARNING under overrides |
| T6 | D-07: when the target is unchanged between syncs, `validate()` allows the sync without false divergence (depth-1 tool-state writes excluded) | VERIFIED | Plan 07's tool-state filter correctly excludes `<user>/.local/share/pc-switcher/` and `<user>/.config/pc-switcher/` at depth 1; `test_toolstate_write_under_empty_prefix_not_divergence` and `test_config_write_under_empty_prefix_not_divergence` pass |
| T7 | `_build_rsync_cmd` produces correct flags (`-aAXHS --numeric-ids --delete --rsync-path='sudo rsync' --info=progress2 --partial --mkpath`); `execute()` uses async subprocess | VERIFIED | Command verified in plan 05 SUMMARY; `source.start_process(cmd)` uses asyncio subprocess |
| T8 | Dry-run executes rsync with `--dry-run`; no divergence marker written; no state changes (D-12) | VERIFIED | `execute()` adds `--dry-run` to command; `if not self.context.dry_run:` guard before baseline writes; orchestrator guard before sync-history update |
| T9 | Post-sync baseline recorded via `set_target_generation` after all folders succeed in non-dry-run mode; baseline-capture failure records sentinel and does not abort | VERIFIED | Non-fatal loop in `execute()` at lines 623-655; `UNKNOWN_GENERATION` sentinel written on RuntimeError/ValueError; `test_baseline_capture_failure_records_sentinel_and_does_not_raise` passes |
| T10 | A→B sync copies configured folders byte-identically with every included file present (ROADMAP SC1) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: `rsync -aAXHS --delete`; unit tests verify command construction. Runtime byte-identity requires VM execution. |
| T11 | File metadata preserved: owner, group, permissions, ACLs, timestamps (ROADMAP SC2) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: `-aAXHS --numeric-ids` flags. Actual metadata transfer requires live rsync-as-root on VMs. |
| T12 | Machine-specific items excluded; dev-tool caches and VS Code User/ state included (ROADMAP SC3) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: `--filter='- <pattern>'` rules from config excludes. Actual filter enforcement requires VM execution. |
| T13 | B→A round-trip propagates additions, modifications, and deletions byte-identically; exclusions hold in reverse (ROADMAP SC4) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: same FolderSyncJob code path; `--delete` propagates deletions. Full round-trip proof requires VM execution. |
| T14 | VM integration test automates A→B/mutate/B→A round-trip and asserts criteria 1-4 (ROADMAP SC5) | PRESENT_BEHAVIOR_UNVERIFIED | `tests/integration/test_folder_sync.py` exists, 3 test methods; collects without error offline. Never run against live VMs. |
| T15 | Tool-state filter excludes only pc-switcher-owned paths at the expected depth; user files nested under a `.config/pc-switcher/` or `.local/share/pc-switcher/` subpath at depth > 1 are NOT silently masked | FAILED — BLOCKER (CR-01) | `folder_sync.py:395` uses `any(token in line for token in tool_state_tokens)` — unanchored substring check. Token `/.config/pc-switcher/` matches any line containing that string, including `janfr/dotfiles/.config/pc-switcher/config.yaml`. No test for deep nesting exists. |
| T16 | The pre-transfer re-check in `execute()` (WR-03) does not false-positive after Phase-7 `install_on_target` writes upgrade artifacts under the synced @home subvolume | FAILED — BLOCKER (CR-02) | Orchestrator Phase 7 (`_install_on_target_job`, line 265) runs before Phase 9 (`_execute_jobs`, line 275). install.sh writes `$HOME/.local/bin/uv`, `$HOME/.local/share/uv/tools/pcswitcher/`, `$HOME/.local/bin/pc-switcher` under @home. Tool-state filter only excludes `/.local/share/pc-switcher/` and `/.config/pc-switcher/`. Phase-9 re-check at folder_sync.py:577 sees install artifacts as DIVERGED and raises before rsync. Happens whenever `target_version != source_version` (install_on_target.py:70-75). |

**Score:** 9/16 truths verified (1 old gap closed: T6; 5 present, behavior-unverified; 2 new FAILED BLOCKERS: T15/T16)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `docs/adr/adr-013-rsync-over-ssh-user-data-transport.md` | ADR recording rsync-over-SSH transport | VERIFIED | Exists, `Status: Accepted`, correct rules |
| `docs/adr/adr-014-unified-dry-run-contract.md` | ADR recording tool-wide dry-run contract | VERIFIED | Exists, `Status: Accepted` |
| `docs/adr/_index.md` | Updated with ADR-013 and ADR-014 | VERIFIED | Both entries present in Active Decisions |
| `src/pcswitcher/schemas/config-schema.yaml` | Schema accepts `folder_sync` | VERIFIED | `folder_sync` object with `required: [folders]`, `additionalProperties: false` |
| `src/pcswitcher/default-config.yaml` | Defaults: `/home`+`/root` with D-11 excludes | VERIFIED | Correct excludes; `folder_sync: true` |
| `src/pcswitcher/jobs/context.py` | `allow_divergence: bool = False` | VERIFIED | Field confirmed |
| `src/pcswitcher/sync_history.py` | `get/set_target_generation`, merge-preserving `record_role`, `UNKNOWN_GENERATION` sentinel | VERIFIED | All exported in `__all__`; `UNKNOWN_GENERATION = -1`; merge-preserving writes confirmed |
| `src/pcswitcher/config_sync.py` | `CONFIG_REMOTE_DIR` + `CONFIG_REMOTE_PATH` constants | VERIFIED | Both at lines 22-23, exported in `__all__` |
| `src/pcswitcher/jobs/folder_sync.py` | `FolderSyncJob` with `DivergenceStatus` enum, tool-state filter, `_check_divergence`, `execute()` with pre-transfer re-check and non-fatal baseline | VERIFIED (with 2 BLOCKERS) | File exists, substantive, wired. CR-01 bug makes unanchored filter mask nested user files. CR-02 bug makes pre-transfer re-check false-positive after Phase-7 install. |
| `src/pcswitcher/jobs/__init__.py` | Exports `FolderSyncJob` | VERIFIED | Import succeeds |
| `src/pcswitcher/executor.py` | `LocalProcess.read_stdout_chunks()` + `LocalProcess.wait_result()` | VERIFIED | Both methods present |
| `src/pcswitcher/ui.py` | `TerminalUI.set_total_steps()` setter | VERIFIED | Method added by plan 08, mirrors `set_current_step` pattern |
| `tests/integration/test_folder_sync.py` | VM integration test with A→B, round-trip, divergence scenarios | VERIFIED (existence), PRESENT_BEHAVIOR_UNVERIFIED (execution) | Exists, 3 test methods, collects offline; never run against live VMs |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `cli.py` → `Orchestrator` | `--allow-divergence` flag | Typer option through `_run_sync` / `_async_run_sync` | VERIFIED | Confirmed via `pc-switcher sync --help` and `Orchestrator.__init__` signature |
| `Orchestrator` → `JobContext` | `allow_divergence` param | `_create_job_context` passes it to `JobContext(...)` | VERIFIED | Parameter present in both signatures |
| `FolderSyncJob._check_divergence` → `sync_history` | Reads `get_target_generation`; UNKNOWN_GENERATION triggers UNVERIFIABLE short-circuit | `folder_sync.py:427,440` | VERIFIED | Direct call at line 427; sentinel check at line 440 |
| `FolderSyncJob.execute()` → `_check_divergence` | Pre-transfer re-check BEFORE rsync spawn | `folder_sync.py:577` | VERIFIED (with CR-02 BLOCKER) | Call at line 577; non-None result raises RuntimeError before `start_process`. Install artifacts not excluded — CR-02. |
| `FolderSyncJob.execute()` → `sync_history` | Writes baseline (or sentinel) after rsync succeeds | `folder_sync.py:643,655` | VERIFIED | Non-fatal loop; `UNKNOWN_GENERATION` sentinel on capture failure |
| `Orchestrator` Phase 7 → Phase 9 ordering | Phase 7 install runs BEFORE Phase 9 execute | `orchestrator.py:265,275` | VERIFIED (CR-02 root cause) | Lines 265 and 275 confirmed; install artifacts land in @home before re-check baseline |
| `config_sync.CONFIG_REMOTE_DIR` → `folder_sync` tool-state filter | Token derived from constant, not hardcoded literal | `folder_sync.py:382` | VERIFIED (with WR-01 caveat) | `lstrip('~/')` works for current constants but is character-set strip, not prefix strip — see Anti-Patterns |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full unit suite (520 tests) | `uv run pytest tests/unit tests/contract -q --tb=no` | 520 passed, 0 failed | PASS |
| Divergence guard tests (16 tests) | `uv run pytest tests/unit/jobs/test_folder_sync.py::TestDivergenceGuard -q --tb=no` | 16 passed | PASS |
| Integration tests collect without error | `uv run pytest tests/integration/test_folder_sync.py --collect-only -q` | 3 tests deselected by `not integration`; no collection errors | PASS |
| CR-01 deep-nesting case (depth > 1 path) | No test exists for this case | — | NOT COVERED — no test for `janfr/dotfiles/.config/pc-switcher/...` depth-2 path |
| CR-02 Phase-7 install artifacts in re-check | No test exercises install artifact paths in `TestExecuteDivergenceRecheck` | — | NOT COVERED — no test for `janfr/.local/bin/pc-switcher`, `janfr/.local/share/uv/...` in re-check |
| Integration tests against real VMs | `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` | SKIPPED — requires live Hetzner VMs | SKIP |

### Requirements Coverage

| Requirement | Source Plans | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| REQ-sync-scope-user-data | 01, 02, 04, 05, 06 | Sync `/home` + `/root` via generic per-folder mechanism | PARTIAL | Mechanism verified; **CR-01 and CR-02 mean the default config is still unreliable for upgrade+sync workflows and has a data-loss path for users with nested tool-state paths** |
| REQ-machine-specific-exclusions | 02, 04, 05, 06 | Never sync `.ssh/id_*`, tailscale, GPU/fontconfig caches | VERIFIED (mechanism) | Default excludes in YAML; `--filter` rules built correctly; no `--delete-excluded` |
| REQ-sync-scope-file-metadata | 04, 05, 06 | Preserve owner, group, permissions, ACLs, timestamps | VERIFIED (mechanism) | `-aAXHS --numeric-ids` flags; VM execution needed for behavioral proof |
| REQ-manual-sync-workflow | 01, 03, 04, 05, 06 | Single-command trigger; divergence guard; dry-run preview | PARTIAL | Command works; dry-run works; **CR-02 breaks upgrade+sync (the guard false-positives after Phase-7 install)**; **CR-01 is a false-negative data-loss path** |
| REQ-terminal-ux | 05, 06, 08, 09 | Single command; terminal UI; progress; clear errors; truthful audit log | VERIFIED (mechanism) | CLI works; `--info=progress2` streaming; Rich UI; progress bar reaches 100% (plan 08); bytes reported correctly (plan 09); `c`/`h` types in FULL logs |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/pcswitcher/jobs/folder_sync.py` | 395 | `any(token in line for token in tool_state_tokens)` — unanchored substring check across entire find-new output line | BLOCKER (CR-01) | Silently masks real user files at depth > 1 whose path contains `/.config/pc-switcher/` or `/.local/share/pc-switcher/` anywhere; `rsync --delete` proceeds and destroys diverged user data |
| `src/pcswitcher/jobs/folder_sync.py` | 577–586 | Pre-transfer re-check reads stored gen from last sync; Phase-7 install artifacts not excluded by tool-state filter | BLOCKER (CR-02) | Every upgrade-then-sync falsely blocked with DIVERGED; user forced to `--allow-divergence` (disables guard) |
| `src/pcswitcher/jobs/folder_sync.py` | 381–382 | `sync_history.HISTORY_DIR.lstrip('~/')` — character-set strip, not prefix strip | WARNING (WR-01) | Works for current constants; latent landmine if any future constant begins with `/` or extra `~`; feeds the security-relevant filter token derivation; should be `.removeprefix("~/")` |
| `src/pcswitcher/config_sync.py` | 317 | `CONFIG_REMOTE_PATH.lstrip("~/")` — same character-set strip issue | WARNING (WR-01) | Same latent bug; should be `.removeprefix("~/")` |
| `src/pcswitcher/jobs/folder_sync.py` | 385–409 | Duplicated per-line skip scaffold (`transid marker` + blank-line guards) in both the empty-prefix branch and the `else` branch | INFO (IN-01) | Duplication is what let CR-01/CR-02 diverge; a shared path-yielding helper would reduce future drift |

### Human Verification Required

#### 1. A→B Byte-Identical Content Sync (ROADMAP SC1)

**Test:** On machine A with the default config (or the test-dir config), run `pc-switcher sync <B>`. Compare `md5sum` of all included files on both machines.

**Expected:** Every included file exists on B and has the same md5sum as A. No excluded files (.ssh/id_*, .config/tailscale, VS Code cache dirs) appear on B.

**Why human:** Requires live rsync-as-root over SSH to real btrfs VMs. Unit tests only verify the rsync command construction.

#### 2. File Metadata Preservation (ROADMAP SC2)

**Test:** After A→B sync, compare `stat -c '%u %g %a'` (numeric uid/gid/perms), `stat -c '%Y'` (mtime), `getfacl` (ACLs), and `stat -c '%i'` (inode for hard-link pairs) on both machines.

**Expected:** Numeric uid/gid, permissions, mtime, POSIX ACL entries, and hard-link inode sharing are identical between A and B for every synced file.

**Why human:** Requires real rsync with `--numeric-ids` and `-aA` on VMs with files owned by multiple users. Unit tests only verify the flags are present.

#### 3. Round-Trip Propagation of Changes (ROADMAP SC4)

**Test:** After A→B, mutate B (add file, modify file content, delete a file, add excluded file), then run `pc-switcher sync <A>` from B. Verify A reflects all three mutation types byte-identically with metadata preserved and the excluded file absent.

**Expected:** Addition present on A (md5sum match), modified file has new content, deleted file absent on A (`--delete`), excluded file absent on A, mtime and uid/gid preserved.

**Why human:** Requires bidirectional VM execution and deletion propagation via `--delete` across real SSH.

#### 4. Default /home two-consecutive-sync check (D-07 — after CR-01 + CR-02 are fixed)

**Test:** After both CR-01 and CR-02 gap-closure plans are executed, on a real machine pair using the shipped default config (`/home`), run `pc-switcher sync <target>` twice in a row WITHOUT `--allow-divergence`.

**Expected:** Exit code 0 on both runs; no "divergence" in output. Note: this item is blocked pending CR-01 and CR-02 closure — running it now will fail due to CR-02 if the target version differs.

**Why human:** Requires destructive `--delete` mirror of real `/home` on a real machine pair; CR-01 and CR-02 must be fixed first.

#### 5. Full VM Integration Test Suite

**Test:** Run `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` with live Hetzner VMs (pc1, pc2), current branch pushed to origin, and all env vars set.

**Expected:** All three tests pass: `TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions`, `TestFolderSyncRoundTrip::test_round_trip_and_no_false_divergence`, `TestFolderSyncRoundTrip::test_divergence_guard_and_dry_run`.

**Why human:** Requires live Hetzner VM infrastructure and SSH access. Cannot run offline.

### Gaps Summary

**2 BLOCKERS — introduced by the gap-closure work (plans 07-09)**

**BLOCKER 1 — CR-01: Unanchored tool-state filter creates a false-negative data-loss path**

`folder_sync.py:381-397` (`_target_diverged_since`, empty-prefix branch). The fix for the original T6 gap used substring-matching to exclude pc-switcher's own state writes:

```python
# folder_sync.py:381-383
history_token = f"/{sync_history.HISTORY_DIR.lstrip('~/')}/"  # /.local/share/pc-switcher/
config_token = f"/{config_sync.CONFIG_REMOTE_DIR.lstrip('~/')}/"  # /.config/pc-switcher/
tool_state_tokens = (history_token, config_token)

# folder_sync.py:395
if any(token in line for token in tool_state_tokens):
    continue
```

A btrfs find-new line looks like: `inode 5678 file offset 0 len 4096 disk start 0 offset 0 gen 1001 flags UNKNOWN janfr/.config/pc-switcher/config.yaml`

The substring `"/.config/pc-switcher/"` is checked anywhere in this whole line. A file at `janfr/dotfiles/.config/pc-switcher/config.yaml` also contains the substring — because `dotfiles/.config/pc-switcher/` includes `/.config/pc-switcher/` — and is silently classified CLEAN. The divergence guard then proceeds with `rsync --delete`, overwriting the user's genuinely diverged data. The target audience (power users syncing Linux desktops, frequently with dotfile repos managed by chezmoi/yadm/stow) makes this trigger realistic. The existing unit tests only cover the depth-1 case.

**Fix direction:** Extract the path field (last whitespace-delimited token of the find-new line) and apply an anchored regex, e.g.:
```python
_TOOL_STATE_RE = re.compile(r"^[^/]+/(?:\.local/share|\.config)/pc-switcher/")
path = line.rsplit(" ", 1)[-1]
if _TOOL_STATE_RE.match(path):
    continue
```
This matches only `<user>/.config/pc-switcher/...` and `<user>/.local/share/pc-switcher/...` at the expected depth. Add a unit test for the depth-2 case.

**BLOCKER 2 — CR-02: Pre-transfer re-check flags Phase-7 install artifacts as divergence**

`folder_sync.py:569-586` (`execute()`, pre-transfer re-check added by plan 09/WR-03) + `orchestrator.py:265,275` (Phase 7 before Phase 9).

The re-check calls `_check_divergence(folder)` using the stored generation from the LAST sync. Phase 7 (`_install_on_target_job()`, orchestrator.py:265) runs BEFORE Phase 9 (`_execute_jobs()`, line 275). When `target_version != source_version` (install_on_target.py:70-75), Phase 7 executes `install.sh`, which writes:
- `$HOME/.local/bin/uv` (uv bootstrap, install.sh:111-114)
- `$HOME/.local/share/uv/tools/pcswitcher/...` (uv tool install, install.sh:191-200)
- `$HOME/.local/bin/pc-switcher` (binary link)

These land at `janfr/.local/bin/uv`, `janfr/.local/share/uv/...`, `janfr/.local/bin/pc-switcher` on the @home subvolume. The tool-state filter only excludes `/.local/share/pc-switcher/` and `/.config/pc-switcher/` — it does NOT exclude `.local/bin/` or `.local/share/uv/`. The Phase-9 re-check reports DIVERGED, logs CRITICAL, and raises RuntimeError before rsync runs.

This affects every sync where the source and target have different pc-switcher versions: every version bump in production; every sync during active development where dev versions diverge between machines. The only escape is `--allow-divergence`, which disables the guard entirely and reopens the data-loss window that CR-01/CR-02 were meant to close.

**Fix direction (two options):**
- **Option A** (preferred; pairs with CR-01 fix): Extend the anchored regex to also cover install artifacts at the expected depth: `r"^[^/]+/(?:\.local/share/pc-switcher|\.config/pc-switcher|\.local/bin/(?:pc-switcher|uv)|\.local/share/uv)(?:/|$)"`.
- **Option B** (more robust): After Phases 7 and 8 complete but before Phase 9 executes, record the current @home generation as the re-check baseline, so only user writes in the narrow Phase-8→Phase-9 window trigger the re-check.

Add a unit test with a find-new output containing `janfr/.local/bin/pc-switcher` and `janfr/.local/share/uv/tools/pcswitcher/...` paths and assert `execute()` does NOT raise (rsync is spawned).

**Secondary concerns (warnings — same root cause, block together):**

- **WR-01**: `lstrip('~/')` at folder_sync.py:381-382 and config_sync.py:317 is character-set stripping, not prefix stripping. Works for current constants. The CR-01 fix should simultaneously replace both with `.removeprefix("~/")`.
- **IN-01**: Duplicated per-line skip logic in the two branches of `_target_diverged_since` (folder_sync.py:385-409) — the structural cause that let CR-01/CR-02 diverge between branches. The CR-01 fix should consolidate into a shared path-yielding helper.

**5 runtime behaviors are PRESENT_BEHAVIOR_UNVERIFIED** — code is present and wired; behavioral proof requires VM execution: byte-identical content, metadata preservation, exclusion correctness, round-trip propagation, and the integration test itself.

---

_Verified: 2026-07-01_

_Verifier: Claude (gsd-verifier) — re-verification after gap-closure plans 07/08/09_
