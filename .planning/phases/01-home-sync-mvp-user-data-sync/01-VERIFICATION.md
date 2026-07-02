---
phase: 01-home-sync-mvp-user-data-sync
verified: 2026-07-02T15:30:00Z
status: human_needed
score: 15/20
behavior_unverified: 5
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 9/16
  gaps_closed:
    - "T15 (CR-01): Unanchored tool-state filter false-negative — CLOSED. Entire btrfs divergence guard removed from folder_sync.py by plan 01-11."
    - "T16 (CR-02): Pre-transfer re-check false-positive after Phase-7 install artifacts — CLOSED. Same removal resolves this."
  gaps_remaining: []
  regressions: []
behavior_unverified_items:
  - truth: "A→B sync copies configured folders byte-identically with every included file present (ROADMAP SC1)"
    test: "Run pc-switcher sync <target> on machine A with the default /home and /root config; compare md5sum of all included files on both machines after sync"
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
  - test: "Two-consecutive-sync live check (D-07 — now unblocked): On a real machine pair with default /home config, run pc-switcher sync <target> twice in a row without --allow-out-of-order"
    expected: "First sync: succeeds (or prompts W1/W3 if no prior history — answer y); second sync: exits 0; back-and-forth A→B / B→A / A→B proceeds silently in the clean case"
    why_human: "Requires destructive --delete mirror of real /home on a real machine pair; blocked on VMs"
  - test: "Full VM Integration Test Suite: tests/run-integration-tests.sh tests/integration/test_folder_sync.py"
    expected: "All three tests pass: TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions, TestFolderSyncRoundTrip::test_round_trip_and_no_false_divergence, TestFolderSyncRoundTrip::test_divergence_guard_and_dry_run"
    why_human: "Requires live Hetzner VM infrastructure and SSH access"
---

# Phase 1: Home-Sync MVP Verification Report

**Phase Goal:** A user can run one command to replicate configured folders (`/home` and `/root` by default) from the source machine to the target over rsync-over-SSH, with machine-specific items excluded and file metadata preserved — and the result is proven correct in both directions. The job is a generic folder-sync mechanism (per-folder include/exclude), usable for any path.

**Verified:** 2026-07-02

**Status:** human_needed — all technical truths VERIFIED including the topology-based safety pivot (plans 10-14); 5 ROADMAP success criteria require live VM execution for behavioral proof

**Re-verification:** Yes — after gap-closure design pivot (plans 01-10 through 01-14, ADR-015)

## Goal Achievement

### What The Design Pivot Fixed

Plans 01-10 through 01-14 replaced the btrfs content-based divergence guard with a topology-based safety model (ADR-015), closing both previous blockers:

| Previous Gap | Plan | Resolution |
|---|---|---|
| CR-01: Unanchored tool-state filter — false-negative data loss | 01-11 | Entire btrfs divergence guard removed from `folder_sync.py`; no filter to misapply |
| CR-02: Phase-7 install artifacts in pre-transfer re-check — false-positive on every upgrade+sync | 01-11 | Pre-transfer re-check removed with the guard; no check to false-positive |
| WR-01: `lstrip('~/')` in config_sync.py | 01-11 | Replaced with `removeprefix("~/")` |
| Old --allow-divergence/--allow-consecutive fragmentation | 01-13 | Consolidated to single `--allow-out-of-order` flag |

### Observable Truths

#### Topology Pivot Truths (Plans 10-14 — all new in this re-verification)

| # | Truth | Status | Evidence |
|---|---|---|---|
| N1 | ADR-015 accepted, indexed in `docs/adr/_index.md`, and `01-CONTEXT.md` D-06/D-07/D-08 marked superseded | VERIFIED | ADR-015 exists, `Status: Accepted`; `_index.md` line 19 links it; CONTEXT.md lines 54-70 add supersession note and new-model summary |
| N2 | Btrfs content-divergence machinery removed from `folder_sync.py`: no `DivergenceStatus`, `_target_diverged_since`, `_check_divergence`, `_resolve_subvolume`, `_get_subvolume_generation`, `find-new` invocation, `allow_divergence` reference | VERIFIED | `grep -nE "DivergenceStatus|_target_diverged_since|_check_divergence|find-new|allow_divergence" folder_sync.py` returns no matches; basedpyright clean |
| N3 | Per-target btrfs generation store removed from `sync_history.py`: no `get_target_generation`, `set_target_generation`, `UNKNOWN_GENERATION` | VERIFIED | `grep -nE "get_target_generation|set_target_generation|UNKNOWN_GENERATION" sync_history.py` returns no matches; `__all__` contains none of these symbols |
| N4 | `sync_history` exposes `parse_sync_state` (pure JSON parser returning `(role, peer)`) and `get_last_sync_state` (reads local history file); `record_role` and `get_record_role_command` accept optional `peer` argument; writes remain merge-preserving and atomic | VERIFIED | Both functions in `__all__`; `record_role(role, peer=None)` and `get_record_role_command(role, peer=None)` signatures confirmed; `parse_sync_state('{"last_role":"target","last_peer":"pc1"}')` returns `(SyncRole.TARGET, "pc1")`; bad input returns `(None, None)` |
| N5 | CLI exposes `--allow-out-of-order` only; `--allow-divergence` and `--allow-consecutive` no longer exist; `allow_divergence` removed from `JobContext` | VERIFIED | `grep -nE "allow_divergence|allow_consecutive" cli.py jobs/context.py` returns no matches; `--allow-out-of-order` confirmed at cli.py:203-206 threaded through `_run_sync`/`_async_run_sync` to `Orchestrator` |
| N6 | `_check_out_of_order()` implements the W1/W2/W3/clean truth table: W1 (no readable target history) → warn; W2 (target_peer ≠ source) → warn; W3 (consecutive push to same target) → warn; clean (target_readable AND target_peer == src AND not consecutive_push) → silent return True; `--dry-run` logs warning but returns True; `--allow-out-of-order` returns True immediately; non-interactive returns False | VERIFIED | Method at orchestrator.py:395-520 reviewed; truth table matches; W1/W2/W3 branch logic correct; dry-run path at line 487-494; non-interactive at 496-500; all 6 test-table cases covered by `test_consecutive_sync.py` |
| N7 | `_check_out_of_order()` called AFTER `_acquire_target_lock()` (step 3) and BEFORE `_discover_and_validate_jobs()` (step 4) in `orchestrator.run()` | VERIFIED | orchestrator.py line 231: `_acquire_target_lock()`; line 232: `set_current_step(3)`; line 236: `_check_out_of_order()`; line 241: `_discover_and_validate_jobs()` |
| N8 | `_update_sync_history()` records `last_peer=target_hostname` on source and `last_peer=source_hostname` on target after success | VERIFIED | orchestrator.py:537-547: `record_role(SyncRole.SOURCE, peer=self._target_hostname)` local; `get_record_role_command(SyncRole.TARGET, peer=self._source_hostname)` remote |
| N9 | Deletion audit trail proven: `*deleting <path>` lines from `_stream_rsync` persist to JSON-lines log at `FULL` level for both `--dry-run` and real runs; default file log floor ≤ `FULL` | VERIFIED | `tests/unit/jobs/test_folder_sync_deletion_log.py` 3/3 tests pass: `test_deletion_persisted_at_full_in_real_run`, `test_deletion_persisted_at_full_in_dry_run`, `test_default_file_log_floor_is_at_or_below_full` |
| N10 | Integration test uses `--allow-out-of-order`, contains no `--allow-divergence`/`find-new`; README "What Happens During a Sync" documents topology check and `--allow-out-of-order`, omits consecutive/find-new guards; `config_sync.py` uses `removeprefix("~/")` | VERIFIED | Integration test: grep confirms; README: step 5 at line 84 describes topology check; `--allow-out-of-order` at line 84, 97; config_sync.py:317 uses `removeprefix` |

#### Carried-Forward Verified Truths (Regression Checks)

| # | Truth | Status | Evidence |
|---|---|---|---|
| R1 | ADR-013 (rsync-over-SSH) and ADR-014 (dry-run contract) remain `Status: Accepted`; indexed | VERIFIED | Both ADR files unchanged |
| R2 | Config schema accepts `folder_sync`; default-config ships `/home`+`/root` with D-11 exclusions | VERIFIED | Unchanged by pivot |
| R3 | `validate()` checks only sudo rsync, acl, and active-folder existence — no divergence check | VERIFIED | folder_sync.py:118-183: 3 steps only; no btrfs call, no error for divergence |
| R4 | `_build_rsync_cmd` produces `-aAXHS --numeric-ids --delete --rsync-path='sudo rsync' --info=progress2 --partial --mkpath`; `execute()` streams via async subprocess | VERIFIED | Unchanged by pivot |
| R5 | Dry-run flag passed to rsync as `--dry-run`; sync-history update skipped in dry-run | VERIFIED | Unchanged by pivot; orchestrator dry-run guard at line 285 confirmed |

#### ROADMAP Success Criteria (Behavior-Unverified — Require VMs)

| # | Truth | Status | Evidence |
|---|---|---|---|
| SC1 | A→B sync copies configured folders byte-identically; every included file exists on B and is byte-identical to A | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism verified: `rsync -aAXHS --delete`. Behavioral proof requires live VM execution. |
| SC2 | File metadata preserved: owner, group, permissions, POSIX ACLs, and modification timestamps match A on B | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism verified: `-aAXHS --numeric-ids` flags. Actual metadata transfer requires rsync-as-root on VMs. |
| SC3 | Machine-specific items never copied (`.ssh/id_*`, `.config/tailscale`, GPU caches); dev-tool caches included | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism verified: `--filter` rules from config. Actual enforcement requires VM execution. |
| SC4 | Sync reversible; exclusions hold both directions; round-trip propagates additions, modifications, and deletions byte-identically | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: same `FolderSyncJob` code path; `--delete` propagates deletions. Full round-trip proof requires VMs. |
| SC5 | VM integration test automates full A→B/mutate-on-B/B→A round-trip and asserts criteria 1-4 | PRESENT_BEHAVIOR_UNVERIFIED | `tests/integration/test_folder_sync.py` exists (3 test methods), collects offline; updated to topology model. Never run against live VMs. |

**Score:** 15/20 truths verified (5 present, behavior-unverified — require VM execution)

### Required Artifacts

| Artifact | Expected | Status | Details |
|---|---|---|---|
| `docs/adr/adr-015-topology-based-sync-safety-model.md` | New ADR recording topology safety model | VERIFIED | Exists, `Status: Accepted`, correct Implementation Rules (no find-new, no generation markers) |
| `docs/adr/_index.md` | ADR-015 listed under Active Decisions | VERIFIED | Line 19 links `adr-015-topology-based-sync-safety-model.md` |
| `.planning/phases/01-home-sync-mvp-user-data-sync/01-CONTEXT.md` | D-06/D-07/D-08 marked superseded by ADR-015 | VERIFIED | Lines 54-70 add supersession markers and new-model summary |
| `src/pcswitcher/jobs/folder_sync.py` | Pure rsync mirror; divergence machinery gone | VERIFIED | All divergence symbols absent; basedpyright clean; 486 unit tests pass |
| `src/pcswitcher/sync_history.py` | `last_peer` persisted; `parse_sync_state`/`get_last_sync_state` added; generation store gone | VERIFIED | Both functions in `__all__`; generation symbols absent; basedpyright clean |
| `src/pcswitcher/orchestrator.py` | `_check_out_of_order()` step; old `_check_consecutive_sync` gone; `allow_divergence` gone; `last_peer` recorded | VERIFIED | Method at line 395; called at line 236; old symbols absent; basedpyright clean |
| `src/pcswitcher/cli.py` | `--allow-out-of-order` flag; old flags gone | VERIFIED | Flag at line 203; old flags absent |
| `src/pcswitcher/jobs/context.py` | `allow_divergence` field removed | VERIFIED | `grep allow_divergence context.py` returns no matches |
| `src/pcswitcher/config_sync.py` | `removeprefix("~/")` replacing `lstrip("~/")` | VERIFIED | Line 317 confirmed |
| `tests/unit/jobs/test_folder_sync_deletion_log.py` | Deletion audit trail persistence tests | VERIFIED | 3 tests, all pass |
| `tests/unit/orchestrator/test_consecutive_sync.py` | Reworked to `_check_out_of_order` topology coverage | VERIFIED | No old symbols; W1/W2/W3/clean/bypass/dry-run cases covered |
| `tests/unit/test_dry_run.py` | `allow_divergence` tests removed; `allow_out_of_order` test present | VERIFIED | Old symbols absent; `test_orchestrator_accepts_allow_out_of_order_parameter` present |
| `tests/unit/test_sync_history.py` | Generation tests removed; `last_peer`/`parse_sync_state`/`get_last_sync_state` tests added | VERIFIED | Old symbols absent; new functions tested |
| `tests/integration/test_folder_sync.py` | Updated to topology model; `--allow-out-of-order` used; no `--allow-divergence` | VERIFIED | Collects offline; topology references confirmed |
| `README.md` | Sync sequence describes topology check; omits find-new guard; documents `--allow-out-of-order` | VERIFIED | Step 5 at line 84; `--allow-out-of-order` documented |

### Key Link Verification

| From | To | Via | Status | Details |
|---|---|---|---|---|
| `cli.py --allow-out-of-order` | `Orchestrator(allow_out_of_order=...)` | `_run_sync`/`_async_run_sync` parameter | VERIFIED | Threaded at cli.py:231, 262, 315 |
| `Orchestrator.__init__(allow_out_of_order)` | `_check_out_of_order()` | `self._allow_out_of_order` field checked at orchestrator.py:418 | VERIFIED | Field stored at line 99; bypass path at line 418-423 |
| `_check_out_of_order()` | local sync-history | `sync_history.get_last_sync_state()` at line 429 | VERIFIED | Direct call; returns `(role, peer)` tuple |
| `_check_out_of_order()` | target sync-history | SSH `cat {HISTORY_PATH}` → `parse_sync_state(stdout)` at lines 432-440 | VERIFIED | HISTORY_PATH from sync_history constant; parse_sync_state at line 439 |
| `_update_sync_history()` | source history | `record_role(SyncRole.SOURCE, peer=target_hostname)` at line 538 | VERIFIED | Direct call with peer |
| `_update_sync_history()` | target history | `get_record_role_command(SyncRole.TARGET, peer=source_hostname)` via SSH at lines 543-546 | VERIFIED | Command construction with peer |
| `_stream_rsync *deleting line` | JSON log at FULL | `self._log(FULL, ...)` → `QueueHandler` → `FileHandler` | VERIFIED | Proven by `test_folder_sync_deletion_log.py` tests |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Full unit suite (486 tests) | `uv run pytest tests/unit tests/contract -q --tb=no` | 486 passed, 0 failed | PASS |
| Deletion log persistence (3 tests) | `uv run pytest tests/unit/jobs/test_folder_sync_deletion_log.py -v` | 3 passed | PASS |
| Topology truth table tests | `uv run pytest tests/unit/orchestrator/test_consecutive_sync.py -q` | All pass | PASS |
| `parse_sync_state` handles old-format history | `python -c "from pcswitcher import sync_history as s; r,p = s.parse_sync_state('{\"last_role\":\"target\"}'); assert r is s.SyncRole.TARGET and p is None"` | Returns `(SyncRole.TARGET, None)` | PASS |
| Integration tests collect offline (deselected by marker) | `uv run pytest tests/integration/test_folder_sync.py --collect-only -q` | 3 deselected (marker), no collection errors | PASS |
| Integration tests against real VMs | `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` | SKIPPED — requires live Hetzner VMs | SKIP |

### Requirements Coverage

| Requirement | Source Plans | Description | Status | Evidence |
|---|---|---|---|---|
| REQ-sync-scope-user-data | 01, 02, 04, 05, 06, 11, 14 | Sync `/home` + `/root` via generic per-folder mechanism | VERIFIED (mechanism) | rsync mirror intact; CR-01/CR-02 blockers eliminated by removing the guard; behavioral proof via VMs |
| REQ-machine-specific-exclusions | 02, 04, 05, 06, 11 | Never sync `.ssh/id_*`, tailscale, GPU/fontconfig caches | VERIFIED (mechanism) | Exclusion filter construction unchanged; `TestBuildRsyncCmd` asserts it; no `--delete-excluded` |
| REQ-sync-scope-file-metadata | 04, 05, 06 | Preserve owner, group, permissions, ACLs, timestamps | VERIFIED (mechanism) | `-aAXHS --numeric-ids` flags unchanged; behavioral proof via VMs |
| REQ-manual-sync-workflow | 01, 03, 04, 05, 06, 13 | Single-command trigger; safety check; dry-run preview | VERIFIED | `pc-switcher sync <target>`; topology warn+confirm replaces btrfs guard; dry-run deletion log; `--allow-out-of-order` bypass |
| REQ-terminal-ux | 05, 06, 08, 09, 13, 14 | Single command; terminal UI; progress; clear errors; truthful audit log | VERIFIED | CLI works; Rich UI; progress bar reaches 100%; bytes reported correctly; topology warning uses Rich Panel; deletion log at FULL |

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|---|---|---|---|---|
| `src/pcswitcher/orchestrator.py` | 160 | `config={},  # TODO: Add config snapshot` | INFO (pre-existing) | Deferred feature for session-log config snapshot; introduced in plan 01-03, unrelated to pivot; no functional impact |

No TBD, FIXME, or XXX markers found in any file modified by the pivot plans (01-10 through 01-14).

### Transitional Note: Post-Upgrade First Sync

On a machine pair that had sync-history.json files from before the pivot (containing only `last_role`, no `last_peer`), the first sync post-upgrade will see:
- local: `(role, peer=None)` from the old format
- target: `(role, peer=None)` from the old format

This causes the `_check_out_of_order` to fall into the W3 branch (consecutive-push warning) rather than W1 (no history), producing a slightly misleading warning message. The consequence is a spurious confirmation prompt requiring user input (the safe side — no silent data loss). After the first post-upgrade sync both machines have `last_peer` populated and subsequent syncs classify correctly. This self-heals after one sync and requires no code fix.

### Human Verification Required

#### 1. A→B Byte-Identical Content Sync (ROADMAP SC1)

**Test:** On machine A with the default config (or the test-dir config), run `pc-switcher sync <B>`. Compare `md5sum` of all included files on both machines.

**Expected:** Every included file exists on B and has the same md5sum as A. No excluded files (`.ssh/id_*`, `.config/tailscale`, VS Code cache dirs) appear on B.

**Why human:** Requires live rsync-as-root over SSH to real btrfs VMs. Unit tests only verify the rsync command construction.

#### 2. File Metadata Preservation (ROADMAP SC2)

**Test:** After A→B sync, compare `stat -c '%u %g %a'` (numeric uid/gid/perms), `stat -c '%Y'` (mtime), `getfacl` (ACLs), and `stat -c '%i'` (inode for hard-link pairs) on both machines.

**Expected:** Numeric uid/gid, permissions, mtime, POSIX ACL entries, and hard-link inode sharing are identical between A and B for every synced file.

**Why human:** Requires real rsync with `--numeric-ids` and `-aA` on VMs with files owned by multiple users. Unit tests only verify the flags are present.

#### 3. Round-Trip Propagation of Changes (ROADMAP SC4)

**Test:** After A→B, mutate B (add file, modify file content, delete a file, add excluded file), then run `pc-switcher sync <A>` from B. Verify A reflects all three mutation types byte-identically with metadata preserved and the excluded file absent.

**Expected:** Addition present on A (md5sum match), modified file has new content, deleted file absent on A (`--delete`), excluded file absent on A, mtime and uid/gid preserved.

**Why human:** Requires bidirectional VM execution and deletion propagation via `--delete` across real SSH.

#### 4. Two-Consecutive-Sync Live Check (D-07 — now unblocked)

**Test:** On a real machine pair using the shipped default config (`/home`), run `pc-switcher sync <target>` twice in a row WITHOUT `--allow-out-of-order` (the sync is interactive or use `--yes` if added). On the first sync, if W1 or W3 triggers, confirm with `y`. The second sync should proceed silently (clean A→B/B→A/A→B case).

**Expected:** Both runs succeed; the second sync produces no out-of-order warning; exit code 0. The topology check self-calibrates after the first sync.

**Why human:** Requires destructive `--delete` mirror of real `/home` on a real machine pair. CR-01 and CR-02 are now fixed — this check is unblocked.

#### 5. Full VM Integration Test Suite

**Test:** Run `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` with live Hetzner VMs (pc1, pc2), current branch pushed to origin, and all env vars set.

**Expected:** All three tests pass: `TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions`, `TestFolderSyncRoundTrip::test_round_trip_and_no_false_divergence`, `TestFolderSyncRoundTrip::test_divergence_guard_and_dry_run`.

**Why human:** Requires live Hetzner VM infrastructure and SSH access. Cannot run offline.

### Gaps Summary

No gaps. The two previous blockers (CR-01 and CR-02) are closed by the design pivot:

- **CR-01 (unanchored tool-state filter)** was a data-loss path in `_target_diverged_since`. Closed by removing the entire btrfs divergence guard from `folder_sync.py` (plan 01-11). No filter, no false-negative.
- **CR-02 (Phase-7 install artifacts in pre-transfer re-check)** was a false-positive that blocked every upgrade+sync run. Closed by removing the pre-transfer re-check along with the guard (plan 01-11). No check, no false-positive.

The replacement safety model (ADR-015): btrfs pre/post snapshots (rollback backstop) + rsync `--dry-run` deletion log at FULL (auditable preview) + topology out-of-order warn+confirm (reads `last_role`+`last_peer` from both machines' sync-history). All three pillars are verified in the codebase.

The only remaining work is behavioral proof via live VM execution — 5 items that have always been in the human-verification list and are unchanged in nature.

---

_Verified: 2026-07-02_

_Verifier: Claude (gsd-verifier) — re-verification after design pivot plans 01-10 through 01-14 (ADR-015, topology-based sync-safety model)_
