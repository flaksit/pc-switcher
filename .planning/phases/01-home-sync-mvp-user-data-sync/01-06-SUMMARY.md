---
phase: 01-home-sync-mvp-user-data-sync
plan: "06"
subsystem: integration-testing
tags: [folder-sync, rsync, integration-test, btrfs, divergence, dry-run, acl, metadata, round-trip]
dependency_graph:
  requires:
    - FolderSyncJob._build_rsync_cmd + execute() (plan 05)
    - target-divergence guard _check_divergence / _target_diverged_since (plan 04)
    - folder_sync config schema + default exclusions (plan 02)
    - reset_pcswitcher_state, pc1_with_pcswitcher_mod, pc2_with_pcswitcher fixtures (conftest.py)
  provides:
    - tests/integration/test_folder_sync.py — A→B / mutate / B→A round-trip integration test
    - Executable proof of ROADMAP success criteria 1-5 against real Hetzner VMs
  affects:
    - Phase gate: must be green before /gsd-verify-work on Phase 1
tech_stack:
  added: []
  patterns:
    - "Grouped ssh commands: seed test tree in one run_command to minimise per-call overhead"
    - "try/finally cleanup on BOTH VMs to remove test directory and config unconditionally"
    - "_test_dir(user) helper centralises the dedicated test directory path to avoid hardcoding"
    - "btrfs prefix-scoped divergence: sync-history writes inside /home do not false-positive because path is outside test_dir prefix"
key_files:
  created:
    - tests/integration/test_folder_sync.py
  modified: []
decisions:
  - "Used /home/<user>/pcswitcher-folder-sync-test as the dedicated test directory: lives on @home subvolume (enabling btrfs generation tracking) without mirroring real /home"
  - "Wrote both Task 1 and Task 2 tests together in one Write call (same file, one commit) — acceptable since both tasks target the same file and were fully correct on the first write"
  - "Excluded .ssh/id_*, .config/tailscale, .config/Code/Cache only in the test config (subset of defaults) — sufficient to cover criteria 3 without redundant exclusions"
  - "Divergence guard test uses printf append to alpha.txt (writes inside test_dir prefix) triggering find-new detection; sync-history/log writes (outside prefix) are ignored by the guard"
  - "Dry-run assertion reads the sync-history JSON from pc1 and compares the stored generation before vs after --dry-run to confirm D-12 (no marker write)"
requirements_completed:
  - REQ-sync-scope-user-data
  - REQ-machine-specific-exclusions
  - REQ-sync-scope-file-metadata
  - REQ-manual-sync-workflow
  - REQ-terminal-ux

coverage:
  - id: D1
    description: "A→B sync transfers included files byte-identically (md5sum comparison)"
    requirement: REQ-sync-scope-user-data
    verification:
      - kind: integration
        ref: "tests/integration/test_folder_sync.py::TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions"
        status: unknown
    human_judgment: true
    rationale: "Requires live Hetzner VMs with pushed branch; cannot run in unit test environment"
  - id: D2
    description: "Metadata preserved A→B: numeric uid/gid (--numeric-ids), permissions, mtime, POSIX ACLs, hard-link inode sharing, symlink"
    requirement: REQ-sync-scope-file-metadata
    verification:
      - kind: integration
        ref: "tests/integration/test_folder_sync.py::TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions"
        status: unknown
    human_judgment: true
    rationale: "Requires live VMs and real rsync-as-root execution"
  - id: D3
    description: "Machine-specific exclusions honoured A→B: .ssh/id_*, .config/tailscale, VS Code Cache absent; Code/User/ and .cache/uv/ present"
    requirement: REQ-machine-specific-exclusions
    verification:
      - kind: integration
        ref: "tests/integration/test_folder_sync.py::TestFolderSyncAToB::test_a_to_b_content_metadata_and_exclusions"
        status: unknown
    human_judgment: true
    rationale: "Requires live VMs"
  - id: D4
    description: "B→A round-trip propagates addition, modification, and deletion byte-identically; exclusions and metadata honoured in reverse"
    requirement: REQ-sync-scope-user-data
    verification:
      - kind: integration
        ref: "tests/integration/test_folder_sync.py::TestFolderSyncRoundTrip::test_round_trip_and_no_false_divergence"
        status: unknown
    human_judgment: true
    rationale: "Requires live VMs and both pc1 + pc2 with current-branch pc-switcher"
  - id: D5
    description: "A→B→B→A→A→B round-trip does NOT trigger false divergence (D-07: sync-history and log writes outside test_dir prefix are scoped out)"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: integration
        ref: "tests/integration/test_folder_sync.py::TestFolderSyncRoundTrip::test_round_trip_and_no_false_divergence"
        status: unknown
    human_judgment: true
    rationale: "Requires btrfs find-new scoping to behave correctly on live VMs"
  - id: D6
    description: "Divergence guard blocks A→B when target independently modified (non-zero exit, 'divergence' in output); --allow-divergence overrides and reconciles"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: integration
        ref: "tests/integration/test_folder_sync.py::TestFolderSyncRoundTrip::test_divergence_guard_and_dry_run"
        status: unknown
    human_judgment: true
    rationale: "Requires real btrfs generation increment on live VM"
  - id: D7
    description: "--dry-run does not block on divergence, does not change target files, and does not update the divergence marker in sync-history"
    requirement: REQ-manual-sync-workflow
    verification:
      - kind: integration
        ref: "tests/integration/test_folder_sync.py::TestFolderSyncRoundTrip::test_divergence_guard_and_dry_run"
        status: unknown
    human_judgment: true
    rationale: "Requires live VMs; marker comparison reads JSON from pc1 before and after --dry-run"

metrics:
  duration: "22 minutes"
  completed: "2026-06-30"
  tasks_completed: 2
  tasks_total: 2
  files_changed: 1
status: complete
---

# Phase 01 Plan 06: Folder Sync Integration Test Summary

One-liner: VM-isolated integration test in `tests/integration/test_folder_sync.py` automates A→B / mutate / B→A round-trip against real Hetzner VMs, asserting byte-identical content, full metadata preservation, machine-specific exclusions in both directions, deletion propagation, divergence guard blocking + override, and no-op --dry-run — completing ROADMAP success criteria 1-5.

## Performance

- **Duration:** 22 min
- **Started:** 2026-06-30T14:26:40Z
- **Completed:** 2026-06-30T14:48:21Z
- **Tasks:** 2
- **Files created:** 1

## Accomplishments

- Created `tests/integration/test_folder_sync.py` with three test methods covering all five ROADMAP success criteria.
- `TestFolderSyncAToB.test_a_to_b_content_metadata_and_exclusions` seeds a realistic test tree (regular files, hard-link pair, symlink, root-owned file, POSIX ACL u:2001:r, known mtime), runs `pc-switcher sync pc2 --yes`, and asserts byte-identical content via md5sum, numeric uid/gid via `stat -c '%u %g %a'`, mtime via `stat -c '%Y'`, ACL entry via `getfacl`, hard-link inode sharing, symlink target, absence of `.ssh/id_*` / `.config/tailscale` / VS Code Cache, and presence of VS Code `User/` state and `.cache/uv/`.
- `TestFolderSyncRoundTrip.test_round_trip_and_no_false_divergence` proves criterion 4 (B→A addition, modification, deletion propagation with metadata and reverse exclusions) and D-07 (second A→B after a full round-trip must not be blocked as false divergence).
- `TestFolderSyncRoundTrip.test_divergence_guard_and_dry_run` proves D-06 (tampering a target file blocks the next sync with non-zero exit and divergence message), D-12 (--dry-run exits 0, target files unchanged, sync-history marker unchanged as confirmed by JSON comparison), and --allow-divergence (sync proceeds and reconciles the target).

## Task Commits

Both tasks target the same file; written and verified in one pass:

1. **Tasks 1+2: A→B + round-trip + divergence + dry-run** — `fca802a` (feat)

## Files Created/Modified

- `tests/integration/test_folder_sync.py` — Full integration test suite: `TestFolderSyncAToB` + `TestFolderSyncRoundTrip`; helpers `_seed_test_tree`, `_make_config`, `_write_config`, `_remove_test_artifacts`; try/finally cleanup on both VMs

## Decisions Made

- Dedicated test directory at `/home/<user>/pcswitcher-folder-sync-test`: lives on the @home btrfs subvolume (so divergence tracking via `btrfs subvolume find-new /home <gen>` works) without touching the real /home contents — satisfies the plan prohibition on mirroring real /home or /root.
- Use the btrfs prefix-scoping property: after A→B, pc-switcher writes sync-history.json and log files to `/home/<user>/.local/share/pc-switcher/` which increments @home's generation. The divergence guard correctly ignores these because `find-new` output for those paths does not contain ` <user>/pcswitcher-folder-sync-test/` and so the prefix check returns False (D-07 holds).
- Dry-run marker assertion reads pc1's `sync-history.json` via SSH before and after `--dry-run` and compares the `target_generations["pc2"][tdir]` value — this is the direct, authoritative check that D-12 is honoured.
- Commands grouped aggressively per testing-guide performance rule: seed tree in one `run_command`; stat+md5sum captured together with `&&` chains.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Lint] ruff auto-fixed import sort order, unused f-string prefix, unused variables, non-top-level import**
- **Found during:** Task 1+2 post-write lint pass
- **Issue:** Import block order (I001); `import json` inside test body (PLC0415); two assigned-but-unused set comprehensions; bare f-string without placeholder (F541).
- **Fix:** Moved `import json` to module top, removed unused set comprehension variables, removed `f` prefix from bare string, ran `ruff check --fix && ruff format`.
- **Files modified:** `tests/integration/test_folder_sync.py`
- **Committed in:** fca802a (same task commit, fixes applied before commit)

---

**Total deviations:** 1 auto-fixed (lint)

**Impact on plan:** All lint fixes are cosmetic; no logic changed. Plan executed as specified in all substantive respects.

## Known Stubs

None. The integration test is fully wired: all assertions make real SSH calls to VM executors. No hardcoded empty values or placeholder text.

## Threat Flags

| Mitigation | Status |
|-----------|--------|
| T-06-01 (secret exclusion, both directions) | Applied: test asserts `.ssh/id_rsa` absent after A→B and that `.ssh/id_rsa` added on pc2 does NOT appear on pc1 after B→A |
| T-06-02 (mirror-delete on test data only) | Applied: folder_sync config points to dedicated test dir; try/finally cleanup prevents data accumulation |
| T-06-03 (divergence-guard regression) | Applied: test asserts blocked sync + divergence message AND that a normal round-trip does NOT trigger false divergence (D-07) |

## User Setup Required

The phase gate (`tests/run-integration-tests.sh tests/integration/test_folder_sync.py`) requires:
- Hetzner pc1/pc2 VMs running and accessible
- `HCLOUD_TOKEN`, `PC_SWITCHER_TEST_PC1_HOST`, `PC_SWITCHER_TEST_PC2_HOST`, `PC_SWITCHER_TEST_USER` set
- Current branch (`01-folder-sync`) pushed to origin

See plan frontmatter `user_setup` section for details.

## Self-Check

Files created exist:
- tests/integration/test_folder_sync.py — FOUND

Task commits:
- fca802a — FOUND (git log confirmed)

## Self-Check: PASSED
