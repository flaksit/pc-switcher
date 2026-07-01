---
phase: 01-home-sync-mvp-user-data-sync
verified: 2026-06-30T00:00:00Z
status: gaps_found
score: 8/14
behavior_unverified: 5
overrides_applied: 0
gaps:
  - truth: "D-07: when the target is unchanged between syncs, validate() allows the sync without false divergence"
    status: failed
    reason: >
      The default /home config self-triggers a false divergence on every second sync.
      FolderSyncJob.execute() captures the target's @home btrfs generation (line 521 of
      folder_sync.py) and stores it via set_target_generation — but then the orchestrator
      calls _update_sync_history() (orchestrator.py:284), which writes sync-history.json to
      the TARGET's /home/<user>/.local/share/pc-switcher/sync-history.json. That write bumps
      @home's generation after the baseline was captured. On the second sync, btrfs find-new
      /home <baseline_gen> sees sync-history.json as a changed file; since prefix="" for /home,
      ANY changed file triggers divergence (folder_sync.py:341), blocking the sync even though
      the user never modified the target. The default /home config is therefore unusable for
      repeated syncs without --allow-divergence.
    artifacts:
      - path: "src/pcswitcher/jobs/folder_sync.py"
        issue: >
          execute() captures baseline generation at line 521 before _update_sync_history() writes
          to the target's /home; the empty-prefix check at line 341 makes any changed file count
          as divergence when syncing /home directly.
      - path: "src/pcswitcher/orchestrator.py"
        issue: >
          _update_sync_history() at line 284 runs after execute() and writes
          sync-history.json to the target's /home, bumping @home generation past the stored
          baseline.
      - path: "tests/integration/test_folder_sync.py"
        issue: >
          Integration test uses /home/<user>/pcswitcher-folder-sync-test (non-empty prefix),
          so .local/share/pc-switcher/ writes are outside the prefix and don't trigger the
          divergence check. The default /home path (empty prefix) is never exercised end-to-end.
    missing:
      - >
        Move baseline recording out of execute() into a post-_update_sync_history()
        orchestrator step so the baseline is captured after all target-side writes complete.
        OR have _target_diverged_since ignore paths matching .local/share/pc-switcher/ when
        prefix is empty (tool state writes never constitute user divergence).
      - >
        Add a unit test that simulates the second sync with /home as the root folder, a
        non-empty find-new output containing only .local/share/pc-switcher/sync-history.json,
        and asserts that validate() does NOT return a divergence error.
      - >
        Add an integration test scenario that uses the default /home-root config (or at least
        a folder whose root is the same as the sync target's subvolume mount) and performs two
        consecutive A→B syncs, asserting the second is not blocked.
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
---

# Phase 1: Home-Sync MVP Verification Report

**Phase Goal:** A user can run one command to replicate configured folders (`/home` and `/root` by default) from the source machine to the target over rsync-over-SSH, with machine-specific items excluded and file metadata preserved — and the result is proven correct in both directions. The job is a generic folder-sync mechanism (per-folder include/exclude), usable for any path; `/root` is included because rsync must run as root anyway to preserve cross-owner files.

**Verified:** 2026-06-30

**Status:** gaps_found — 1 BLOCKER (false divergence on default /home config breaks repeated syncs), 5 runtime behaviors require VM execution

**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| T1 | ADR-013 records rsync-over-SSH transport (root via sudo, root SSH login forbidden); ADR-014 records tool-wide dry-run contract; both listed in `docs/adr/_index.md` Active Decisions | VERIFIED | `docs/adr/adr-013-*.md` and `adr-014-*.md` exist, both have `Status: Accepted`, correct Implementation Rules; `grep ADR-013 docs/adr/_index.md` and `grep ADR-014 docs/adr/_index.md` both match |
| T2 | Config schema accepts `folder_sync`; default config ships `/home` + `/root` with D-11 exclusions; loads cleanly via `Configuration.from_yaml` | VERIFIED | `config-schema.yaml` has `folder_sync` boolean under `sync_jobs.properties` and a top-level `folder_sync` object with `required: [folders]`; `from_yaml()` returns `sync_jobs['folder_sync'] is True`, paths `['/home', '/root']`, and excludes include `.ssh/id_*`, `.config/tailscale`, GPU caches, fontconfig cache, VS Code cache dirs — no `Code/User/` exclusion |
| T3 | `allow_divergence` plumbed CLI→Orchestrator→JobContext; dry-run skips orchestrator sync-history update; `sync_history` exposes `get/set_target_generation` with merge-preserving writes | VERIFIED | `--allow-divergence` listed in `pc-switcher sync --help`; `Orchestrator.__init__` param confirmed; `if not self._dry_run:` guard at orchestrator.py:283; `sync_history.__all__` exports both functions; `record_role` spreads `**existing` before overwriting `last_role` |
| T4 | `FolderSyncJob` with `name='folder_sync'` exported from `pcswitcher.jobs`; `validate()` checks sudo rsync on both ends, acl on both ends, and active folder existence on source | VERIFIED | `from pcswitcher.jobs import FolderSyncJob; FolderSyncJob.name == 'folder_sync'`; validate() steps 1-3 confirmed in code; 90 unit/contract tests pass |
| T5 | Divergence guard blocks when target changed since last sync; allows under `--allow-divergence` or `--dry-run` (WARNING + no error) | VERIFIED | `_check_divergence` returns `ValidationError` when diverged in default mode; returns `None` and logs WARNING under `dry_run` or `allow_divergence`; covered by unit tests |
| T6 | D-07: when the target is unchanged between syncs, `validate()` allows the sync without false divergence | FAILED — BLOCKER | Default `/home` config self-triggers false divergence on the second sync: `execute()` captures the target's @home generation (folder_sync.py:521) before `_update_sync_history()` writes `sync-history.json` to the target's `/home/<user>/.local/share/pc-switcher/sync-history.json` (orchestrator.py:284). That write bumps @home's generation after the baseline, so `btrfs find-new /home <baseline>` finds it on the next sync. Since `prefix=""` for `/home`, any changed file triggers divergence (folder_sync.py:341). The integration test misses this because it uses `/home/<user>/pcswitcher-folder-sync-test` (non-empty prefix). |
| T7 | `_build_rsync_cmd` produces `-aAXHS --numeric-ids --delete --rsync-path='sudo rsync' --info=progress2 --partial --mkpath` plus one `--filter` per exclude; `execute()` uses async subprocess (no blocking call) | VERIFIED | Command verified manually from SUMMARY: `sudo -E rsync -aAXHS --numeric-ids --delete --info=progress2 --out-format='%i %n%L' --partial --mkpath -e 'ssh -T -q' --rsync-path='sudo rsync' --filter='- .ssh/id_*' ...`; `source.start_process(cmd)` uses asyncio subprocess; no `--delete-excluded` or `--checksum` |
| T8 | Dry-run executes rsync with `--dry-run`; no divergence marker written; no state changes (D-12) | VERIFIED | `execute()` adds `--dry-run` to command when `context.dry_run`; `if not self.context.dry_run:` guard at folder_sync.py:509 before `set_target_generation`; orchestrator dry-run guard at orchestrator.py:283 before sync-history update |
| T9 | Post-sync baseline recorded via `set_target_generation` after all folders succeed in non-dry-run mode | VERIFIED | `execute()` loop at folder_sync.py:510-522 under `if not self.context.dry_run:` calls `set_target_generation` per folder after all rsync calls complete |
| T10 | A→B sync copies configured folders byte-identically with every included file present (ROADMAP SC1) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: `rsync -aAXHS --delete` with correct source/dest; unit tests verify command construction. Runtime byte-identity requires VM execution. |
| T11 | File metadata preserved: owner, group, permissions, ACLs, timestamps (ROADMAP SC2) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: `-aAXHS --numeric-ids` flags guarantee owner/group/perms/ACLs/xattrs/mtime preservation. Actual metadata transfer requires live rsync-as-root on VMs. |
| T12 | Machine-specific items excluded; dev-tool caches and VS Code User/ state included (ROADMAP SC3) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: `--filter='- <pattern>'` rules built from config excludes; no `--delete-excluded` (excluded files survive on target). Actual filter enforcement requires VM execution. |
| T13 | B→A round-trip propagates additions, modifications, and deletions byte-identically; exclusions hold in reverse (ROADMAP SC4) | PRESENT_BEHAVIOR_UNVERIFIED | Mechanism correct: same FolderSyncJob code path in both directions; `--delete` propagates deletions; config per-machine. Full round-trip proof requires VM execution. |
| T14 | VM integration test automates A→B/mutate/B→A round-trip and asserts criteria 1-4 (ROADMAP SC5) | PRESENT_BEHAVIOR_UNVERIFIED | `tests/integration/test_folder_sync.py` exists, contains 3 test methods covering all five ROADMAP success criteria, and collects without error (`--collect-only` deselects 3 tests via `not integration` marker). Tests have never been executed against live VMs; all plan 06 coverage items carry `status: unknown` and `human_judgment: true`. |

**Score:** 8/14 truths verified (5 present, behavior-unverified; 1 FAILED BLOCKER)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `docs/adr/adr-013-rsync-over-ssh-user-data-transport.md` | ADR recording rsync-over-SSH transport with root-via-sudo | VERIFIED | Exists, `Status: Accepted`, correct Implementation Rules (required: `--numeric-ids`, `-aAXHS`, sudo -E, async; forbidden: root SSH login) |
| `docs/adr/adr-014-unified-dry-run-contract.md` | ADR recording tool-wide dry-run contract | VERIFIED | Exists, `Status: Accepted`, lists all forbidden dry-run operations |
| `docs/adr/_index.md` | Updated with ADR-013 and ADR-014 | VERIFIED | Both entries present in Active Decisions section |
| `src/pcswitcher/schemas/config-schema.yaml` | Schema accepts `folder_sync` job flag and top-level config | VERIFIED | `sync_jobs.properties.folder_sync: boolean`; top-level `folder_sync` object with `required: [folders]`, items have `required: [path]`, `additionalProperties: false` |
| `src/pcswitcher/default-config.yaml` | Defaults: `folder_sync: true`, `/home` + `/root` with D-11 excludes | VERIFIED | Loads via `Configuration.from_yaml`; `sync_jobs['folder_sync'] is True`; `/home` excludes include `.ssh/id_*`, `.config/tailscale`, GPU caches, fontconfig cache, VS Code cache dirs; `Code/User/` not excluded |
| `src/pcswitcher/jobs/context.py` | `allow_divergence: bool = False` field added | VERIFIED | Field confirmed via `dataclasses.fields(JobContext)` |
| `src/pcswitcher/sync_history.py` | `get_target_generation` + `set_target_generation` + merge-preserving `record_role` + merge-preserving `get_record_role_command` | VERIFIED | Both functions exported in `__all__`; `record_role` uses `{**existing, "last_role": role.value}`; `get_record_role_command` returns a python3 script that reads, merges, and atomically writes |
| `src/pcswitcher/jobs/folder_sync.py` | `FolderSyncJob` with full `validate()` + `execute()` + divergence guard | VERIFIED (with BLOCKER) | File exists, substantive, wired. `validate()` has 4 steps; `execute()` implements streaming, dry-run, and baseline recording. CR-01 bug makes the guard false-positive for the default `/home` config (see T6). |
| `src/pcswitcher/jobs/__init__.py` | Exports `FolderSyncJob` | VERIFIED | `from pcswitcher.jobs import FolderSyncJob` succeeds |
| `src/pcswitcher/executor.py` | `LocalProcess.read_stdout_chunks()` + `LocalProcess.wait_result()` | VERIFIED | Both methods present at lines 100-140 |
| `tests/integration/test_folder_sync.py` | VM integration test with A→B, round-trip, divergence, dry-run scenarios | VERIFIED (existence), PRESENT_BEHAVIOR_UNVERIFIED (execution) | File exists, 3 test methods, collects without error; never run against live VMs |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `cli.py` → `Orchestrator` | `--allow-divergence` flag | Typer option threaded through `_run_sync` / `_async_run_sync` | VERIFIED | Confirmed via `pc-switcher sync --help` and `Orchestrator.__init__` parameter |
| `Orchestrator` → `JobContext` | `allow_divergence` param | `_create_job_context` passes it to `JobContext(...)` | VERIFIED | `'allow_divergence' in inspect.signature(Orchestrator.__init__).parameters` confirmed |
| `FolderSyncJob.validate()` → `sync_history` | Reads `get_target_generation` | `folder_sync.py:355` `sync_history.get_target_generation(...)` | VERIFIED | Direct call at line 355 |
| `FolderSyncJob.execute()` → `sync_history` | Writes `set_target_generation` | `folder_sync.py:522` after all rsync transfers succeed | VERIFIED | Call at line 522 under `if not self.context.dry_run:` |
| `Orchestrator._execute_jobs()` → `_update_sync_history()` | Writes target's sync-history.json after Phase 9 | `orchestrator.py:268` then `284` | VERIFIED (ordering gap) | This ordering is the root cause of CR-01: baseline captured at Phase 9, target written at Phase 10+ |
| `FolderSyncJob` → `pcswitcher.jobs` | Exported from `__init__.py` | `from pcswitcher.jobs import FolderSyncJob` | VERIFIED | Import succeeds |
| `sync_jobs.folder_sync` → `Orchestrator` job discovery | `importlib` dynamic import of `pcswitcher.jobs.folder_sync` | `orchestrator.py` job-discovery pattern | VERIFIED | Config flag drives job loading |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Unit + contract tests pass (479 tests) | `uv run pytest tests/unit tests/contract -q --tb=no` | 479 passed in 2.40s | PASS |
| Integration tests collect without error | `uv run pytest tests/integration/test_folder_sync.py --collect-only -q` | 3 tests deselected by `not integration` marker; no collection errors | PASS |
| Default config loads via `Configuration.from_yaml` | `uv run python -c "from pcswitcher.config import Configuration; ..."` | `folder_sync=True`, `/home` and `/root`, correct excludes | PASS |
| `--allow-divergence` exposed in CLI | `uv run pc-switcher sync --help \| grep allow-divergence` | `--allow-divergence` listed | PASS |
| `FolderSyncJob.name == 'folder_sync'` | `uv run python -c "from pcswitcher.jobs import FolderSyncJob; assert FolderSyncJob.name=='folder_sync'"` | OK | PASS |
| Integration tests against real VMs | `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` | SKIPPED — requires live Hetzner VMs + pushed branch + env vars | SKIP |

### Requirements Coverage

| Requirement | Source Plans | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| REQ-sync-scope-user-data | 01, 02, 04, 05, 06 | Sync `/home` + `/root` via generic per-folder mechanism | PARTIAL | Mechanism verified; first sync works; **second sync with default /home blocked by CR-01 false divergence** |
| REQ-machine-specific-exclusions | 02, 04, 05, 06 | Never sync `.ssh/id_*`, tailscale, GPU/fontconfig caches, VS Code cache dirs | VERIFIED (mechanism) | Default excludes in YAML; `--filter` rules built correctly; no `--delete-excluded` |
| REQ-sync-scope-file-metadata | 04, 05, 06 | Preserve owner, group, permissions, ACLs, timestamps | VERIFIED (mechanism) | `-aAXHS --numeric-ids` flags in rsync command; VM execution needed for behavioral proof |
| REQ-manual-sync-workflow | 01, 03, 04, 05, 06 | Single-command trigger; divergence guard; dry-run preview | PARTIAL | Command works; dry-run works; **default /home repeated sync blocked by CR-01** |
| REQ-terminal-ux | 05, 06 | Single command; terminal UI; progress; clear errors | VERIFIED (mechanism) | CLI works; `--info=progress2` streaming; Rich UI via existing framework |

### Anti-Patterns Found

| File | Pattern | Severity | Impact |
|------|---------|----------|--------|
| `src/pcswitcher/jobs/folder_sync.py:405-447` | `bytes_xfr = 0` never reassigned; `_PROGRESS2_RE` doesn't capture byte count; audit log always shows "0 bytes" | WARNING | Misleading per-folder summary (WR-01); cosmetic only — per-file FULL log and progress percentage are correct |
| `src/pcswitcher/jobs/folder_sync.py:519-522` | `_get_subvolume_generation` can raise `RuntimeError`; propagates as FAILED job even though rsync already completed | WARNING | State inconsistency: data transferred but baseline not recorded (WR-02); next run has no baseline → no divergence check |
| `src/pcswitcher/jobs/folder_sync.py:308-344` | `_target_diverged_since` returns `False` (proceeds) when `findmnt` or `find-new` fails, even when a baseline exists | WARNING | Divergence guard fails open after a transient tool failure when a prior baseline exists; contradicts "data-loss linchpin" claim (CR-02 from code review) |
| `src/pcswitcher/orchestrator.py:239-268 vs 268` | Divergence check runs Phase 4; destructive rsync runs Phase 9; ~5-phase TOCTOU window | WARNING | Target modifications between Phase 4 and Phase 9 are not caught by the guard (WR-03); target lock prevents concurrent pc-switcher syncs but not user writes |
| `src/pcswitcher/jobs/folder_sync.py:433` | `--out-format` per-file lines recognized only for `>`, `<`, `*`, `.`; rsync `%i` also emits `c` (created: dirs, symlinks) and `h` (hard links) | INFO | `c` and `h` change types silently dropped from FULL logging; deletion counting unaffected (IN-03) |
| `src/pcswitcher/cli.py:340-355` | `asyncio.wait_for(asyncio.shield(asyncio.sleep(0)), timeout=...)` waits on `sleep(0)`; returns immediately; `TimeoutError` branch unreachable | INFO | Dead code; no functional impact on SIGINT handling (IN-01) |
| `src/pcswitcher/orchestrator.py:196, 274` | `total_steps` counts all configured jobs including `enabled: false`; final step uses only enabled+valid jobs | INFO | Progress bar never reaches 100% in default config (IN-02) |

### Human Verification Required

#### 1. A→B Byte-Identical Content Sync (ROADMAP SC1)

**Test:** On machine A with the default config (or the test-dir config), run `pc-switcher sync <B>`. Compare `md5sum` of all included files on both machines.

**Expected:** Every included file exists on B and has the same md5sum as A. No excluded files (.ssh/id_*, .config/tailscale, VS Code cache dirs) appear on B.

**Why human:** Requires live rsync-as-root over SSH to real btrfs VMs. Unit tests only verify the rsync command construction.

#### 2. File Metadata Preservation (ROADMAP SC2)

**Test:** After A→B sync, compare `stat -c '%u %g %a'` (numeric uid/gid/perms), `stat -c '%Y'` (mtime), `getfacl` (ACLs), and `stat -c '%i'` (inode for hard-link pairs) on both machines for the same files.

**Expected:** Numeric uid/gid, permissions, mtime, POSIX ACL entries, and hard-link inode sharing are identical between A and B for every synced file. Symlinks preserved with correct targets.

**Why human:** Requires real rsync with `--numeric-ids` and `-aA` on VMs with files owned by multiple users and cross-owner metadata. Unit tests only verify the `-aAXHS --numeric-ids` flags are present in the command.

#### 3. Round-Trip Propagation of Changes (ROADMAP SC4)

**Test:** After A→B, mutate B (add file, modify file content, delete a file, add excluded file), then run `pc-switcher sync <A>` from B. Verify A reflects all three mutation types byte-identically with metadata preserved and the excluded file absent.

**Expected:** Addition present on A (md5sum match), modified file has new content (md5sum match), deleted file absent on A (propagated by `--delete`), excluded file absent on A. Mtime and uid/gid preserved for the addition.

**Why human:** Requires bidirectional VM execution and deletion propagation via `--delete` across real SSH. Unit tests mock the subprocess.

#### 4. Second A→B After Round-Trip (D-07 — not blocked by false divergence)

**Test:** After a full A→B → mutate → B→A round-trip, run A→B again WITHOUT `--allow-divergence`. The goal is the second A→B passes without a divergence error.

**Expected:** Exit code 0; no "divergence" in output. Note: with the DEFAULT `/home` config this is currently BLOCKED by CR-01 — see gap. With the test subdirectory config, this should pass because tool writes outside the test_dir prefix are not caught by the prefix check.

**Why human:** Requires real btrfs `find-new` on VMs; the CR-01 gap is only observable at runtime with the real /home subvolume (empty prefix). The integration test uses the test subdirectory (non-empty prefix) and will pass there, but the default /home config failure needs to be fixed and separately verified.

#### 5. Divergence Guard Blocks Independent Target Modification (D-06/D-12)

**Test:** After A→B sync, independently modify a file on B's test directory (without syncing), then attempt A→B. Verify non-zero exit with "divergence" in output. Then run with `--allow-divergence` and verify exit 0. Then run with `--dry-run` and verify: exit 0, target files unchanged (md5sum before == md5sum after), divergence marker not updated in sync-history.json.

**Expected:** Blocked sync (non-zero exit, divergence message), then override succeeds, then dry-run is a true no-op.

**Why human:** Requires real `btrfs subvolume find-new` to detect the change on a live btrfs subvolume. Cannot be verified without VMs.

#### 6. Full VM Integration Test Suite

**Test:** Run `tests/run-integration-tests.sh tests/integration/test_folder_sync.py` with live Hetzner VMs (pc1, pc2), current branch pushed to origin, and all env vars set.

**Expected:** All three tests pass: `TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions`, `TestFolderSyncRoundTrip::test_round_trip_and_no_false_divergence`, `TestFolderSyncRoundTrip::test_divergence_guard_and_dry_run`.

**Why human:** Requires live Hetzner VM infrastructure and SSH access. Cannot run in offline/local environment.

### Gaps Summary

**1 BLOCKER — default /home config self-triggers false divergence on repeated syncs (CR-01)**

The root cause is a sequencing bug: `FolderSyncJob.execute()` captures the target's @home btrfs generation for the divergence baseline (folder_sync.py:521) before the orchestrator's `_update_sync_history()` writes `sync-history.json` to the target's `/home/<user>/.local/share/pc-switcher/sync-history.json` (orchestrator.py:284). That write bumps @home's generation after the baseline was recorded. On the next sync, `btrfs find-new /home <baseline>` surfaces `sync-history.json` as a changed file, and because `prefix=""` for the `/home` folder entry (line 341 of folder_sync.py), any changed file is treated as divergence — blocking the sync even though the user never modified the target.

This makes the default configuration (`folders: [/home, /root]`) unusable for repeated syncs: every second run is blocked with a false divergence error. The only workaround is `--allow-divergence`, which defeats the guard entirely. The integration test doesn't catch this because it uses `/home/<user>/pcswitcher-folder-sync-test` (non-empty prefix), so tool writes under `~/.local/share/pc-switcher/` fall outside the prefix and are correctly ignored.

The fix is to move baseline recording to after `_update_sync_history()` completes (so the baseline captures all tool-side writes), or to filter out pc-switcher's own state directory from the `find-new` output when deciding divergence.

**Secondary Concerns (warnings, not blockers):**

- **CR-02:** When `findmnt` or `btrfs find-new` fails after a baseline exists, the guard returns `False` (proceeds with destructive mirror+delete) instead of blocking. This contradicts the "data-loss linchpin" framing. For the no-baseline / never-synced case the fail-open behavior is intended (per RESEARCH Open Q3). The dangerous case is a transient failure when a baseline is already established.

- **WR-02:** If `_get_subvolume_generation` raises after a successful rsync transfer (line 521-522), the exception propagates as a FAILED job even though data was fully transferred. The orchestrator marks the sync failed and skips the sync-history update. On the next run, no baseline exists → divergence guard skipped → the just-transferred (but "failed") sync is treated as a fresh first sync.

- **WR-01:** `bytes_transferred` is always 0 in the per-folder INFO summary because `_PROGRESS2_RE` doesn't capture the leading byte figure from `--info=progress2` output and `bytes_xfr` is never assigned. Cosmetic: per-file FULL logs and percent progress are correct.

**5 runtime behaviors are PRESENT_BEHAVIOR_UNVERIFIED** (code is present and wired; behavior requires VM execution to confirm): byte-identical content, metadata preservation, exclusion correctness, round-trip propagation, and the integration test itself.

---

_Verified: 2026-06-30_

_Verifier: Claude (gsd-verifier)_
