---
phase: 02-package-management-sync
verified: 2026-07-23T12:52:10Z
status: human_needed
score: 1/3 must-haves verified
behavior_unverified: 2
overrides_applied: 0
behavior_unverified_items:
  - truth: "After sync, the target has the same apt, snap, and flatpak packages installed as the source (SC1)"
    test: "Run tests/integration/jobs/test_package_sync.py::TestAptSyncEndToEnd (and the equivalent snap/flatpak whole-run contracts) against the real pc1/pc2 test VMs"
    expected: "A package removed from pc2 before sync is reinstalled by a real sync and is present in pc2's own apt-mark showmanual / snap list / flatpak list output afterward"
    why_human: "No VM access in this environment (HCLOUD_TOKEN, PC_SWITCHER_TEST_PC1_HOST/PC2_HOST all unset). All unit tests mock the executor, so the real apt-get/snap/flatpak command output is never parsed against a live system — only against hand-built fixture strings."
  - truth: "Manually-installed .debs, custom PPAs, and install-script-sourced packages are reproduced on the target (SC2)"
    test: "Run a real sync with an apt-no-candidate package or unowned /usr/local install on the source; author a snippet in the review; confirm it replays successfully on a real target and the item disappears from the next sync's diff"
    expected: "The snippet executes via the real executor (no stdin) and the item converges; a custom PPA's source/key/pin files land byte-for-byte on the real target and apt-get update succeeds against it"
    why_human: "Same VM-access gap as above — snippet replay and repo-item convergence are unit-tested against a mocked executor only, never against a real /etc/apt or a real dpkg -i"
human_verification:
  - test: "Run tests/integration/jobs/test_package_sync.py (8 tests: 2 tracer + 6 whole-run contracts) via tests/run-integration-tests.sh against the pc1/pc2 test VMs, or let the next non-draft PR to main run it in CI"
    expected: "All 8 pass, proving real apt/snap/flatpak convergence, non-interactive skip-all, continue-on-failure, snap hold-free convergence, flatpak remote-before-ref, skip-always inertness in both roles, and cross-manager batched-review ordering against live package managers"
    why_human: "No VM access in this environment; this is the project's established ADR-008 pattern (unit-tested locally, VM-proven in CI)"
  - test: "Interactive batched review screen (D-24): run a real sync with packages diverged in both directions; confirm every diff class (missing, extra, version mismatch, held, pinned) renders distinguishably and apply/skip/skip-always each produce the recorded outcome"
    expected: "The checkbox list groups installs and removals separately, removal items start unticked, and the resulting decisions match what was ticked"
    why_human: "Unit tests stub questionary.checkbox()/questionary.select() and never exercise real prompt_toolkit rendering, keybindings, or terminal-mode handoff with a live TTY (RESEARCH Assumption A2, deferred in 02-02-SUMMARY.md Task 3 and restated in 02-VALIDATION.md's Manual-Only Verifications table)"
  - test: "On-the-fly install-snippet capture during the review (02-07 Task 2): trigger an apt-no-candidate or unowned /usr/local install on the source, run a real sync, and author a snippet interactively"
    expected: "The three-way prompt appears, the multi-line snippet editor accepts a worked dpkg -i / apt-get install -f shape, and the authored snippet appears in ~/.config/pc-switcher/package-snippets.yaml on the next read"
    why_human: "Unit tests stub both questionary.select and questionary.text; the real multi-line capture ergonomics are never exercised (02-VALIDATION.md Manual-Only Verifications table)"
  - test: "End-to-end real-machine confirmation of all three roadmap success criteria (02-12 Task 3, checkpoint:human-verify, gate=blocking, explicitly deferred for this autonomous run)"
    expected: "On two real machines, package replication, before-any-change conflict/version-mismatch reporting, and machine-specific-package non-forcing all hold as described"
    why_human: "Requires two physical machines and interactive TUI access; 02-12-SUMMARY.md records this as deferred per explicit orchestrator directive, not performed"
---

# Phase 2: Package Management Sync Verification Report

- **Phase Goal:** A user can replicate installed packages from source to target across all package sources, with conflicts and version mismatches detected and reported rather than silently overwritten.
- **Verified:** 2026-07-23T12:52:10Z
- **Status:** human_needed
- **Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP §Phase 2 success criteria)

| # | Truth | Status | Evidence |
| - | ----- | ------ | -------- |
| 1 | After sync, the target has the same apt, snap, and flatpak packages installed as the source (verifiable by querying each package manager) | ⚠️ PRESENT_BEHAVIOR_UNVERIFIED | `apt_sync.py`/`snap_sync.py`/`flatpak_sync.py` fully implement capture→diff→converge for all three managers (1434/414/591 lines respectively); 974 unit tests pass, all against a mocked `Executor`. The only tests that would exercise real package-manager state — `tests/integration/jobs/test_package_sync.py` (8 tests, correctly collected and deselected by default under `-m integration`) — require `HCLOUD_TOKEN`/`PC_SWITCHER_TEST_PC1_HOST`/`PC_SWITCHER_TEST_PC2_HOST`, all unset in this environment. Unexecuted, not passing. |
| 2 | Manually-installed .debs, custom PPAs, and install-script-sourced packages are reproduced on the target | ⚠️ PRESENT_BEHAVIOR_UNVERIFIED | Unreproducible-item detection (`scan_unowned_installs`/apt-no-candidate scan, `apt_sync.py:634-681`) and the install-snippet registry (`package_state.py` `SnippetRegistry`, wired into `config_sync.py`'s `SYNCED_CONFIG_FILENAMES`) are implemented and unit-tested against a mocked executor and mocked `questionary` prompts. Real snippet replay on a live target and real `/etc/apt` repo-item convergence are VM-integration-test territory (same 8 tests as truth 1), unexecuted here. |
| 3 | Package conflicts and version mismatches between source and target are detected and reported before any destructive change; machine-specific packages are not forced onto the target | ✓ VERIFIED | See "Key Link Verification" and "Behavioral Spot-Checks" below — every sub-claim is proven by a real behavioral unit test that exercises pc-switcher's own control-flow invariants (not real package-manager output), which is sufficient since the invariant lives entirely in pc-switcher's code, not in apt/snap/flatpak's behavior. |

**Score:** 1/3 truths verified (2 present, behavior-unverified — both gated on VM access this environment does not have)

### Required Artifacts

| Artifact | Expected | Status | Details |
| -------- | -------- | ------ | ------- |
| `src/pcswitcher/jobs/package_phase.py` | `PackagePhaseCoordinator`: plan-all → review-once → distribute | ✓ VERIFIED | 139 lines; `run()` collects every job's `plan()`, calls `review_items()` exactly once, slices the outcome per job via `accept_review()` |
| `src/pcswitcher/jobs/package_sync_core.py` | Shared `PackageSyncJob` plan()/apply()/execute() pipeline | ✓ VERIFIED | 636 lines; `execute()` structurally refuses to run without `accept_review()` having been called first (raises `RuntimeError` naming the coordinator) |
| `src/pcswitcher/jobs/package_review.py` | Batched checkbox review, removals separated and unchecked | ✓ VERIFIED | 339 lines; `_is_removal_direction`, group-by-action, `checked=not removal` |
| `src/pcswitcher/jobs/package_state.py` | Machine-local `DecisionFile`, `filter_inert`, `SnippetRegistry` | ✓ VERIFIED | 402 lines; atomic temp-then-mv writes, malformed-file degrade-to-empty |
| `src/pcswitcher/jobs/package_items.py` | `DiffClass`/`DiffAction` taxonomy, item shapes | ✓ VERIFIED | 464 lines; `DiffAction.REMOVE`/`INSTALL`/`CHANGE`/`REPORT_ONLY` |
| `src/pcswitcher/jobs/apt_sync.py` | apt manifest, repo/key/pin/config items, transaction guard | ✓ VERIFIED | 1434 lines; `simulate_apt_transaction`, staged-then-`sudo install` repo writes, backup/rollback on `apt-get update` failure |
| `src/pcswitcher/jobs/snap_sync.py` | Snap revision/channel convergence, no-hold guarantee | ✓ VERIFIED | 414 lines; `test_install_change_retrack_and_removal_never_set_a_hold` passes |
| `src/pcswitcher/jobs/flatpak_sync.py` | Flatpak ref/remote convergence, scoped, remote-before-ref | ✓ VERIFIED | 591 lines; `test_remotes_converge_before_refs_that_depend_on_them` passes |
| `src/pcswitcher/config_sync.py` | Snippet registry (`package-snippets.yaml`) travels to target | ✓ VERIFIED | `SYNCED_CONFIG_FILENAMES = ("config.yaml", "package-snippets.yaml")` — the review's HIGH finding is fixed |
| `docs/configuration.md` §Package Sync | User docs for all three jobs, batched review, machine-specific packages, snippets | ✓ VERIFIED | Substantive prose (not a stub), matches the actual implemented behavior line-for-line (verified by reading) |

### Key Link Verification

| From | To | Via | Status | Details |
| ---- | --- | --- | ------ | ------- |
| `orchestrator._execute_jobs` | `PackagePhaseCoordinator.run` | `coordinate_package_review(package_jobs, ...)` called **outside and before** the `TaskGroup` that runs each job's `execute()` | ✓ WIRED | `orchestrator.py:1074-1079`; confirmed by reading — the coordinator call precedes `_run_jobs_in_task_group` |
| `PackageSyncJob.execute()` | `PackagePhaseCoordinator.accept_review()` | Structural guard: `execute()` raises `RuntimeError` if `_accepted_plan`/`_accepted_outcome` is `None` | ✓ WIRED | `package_sync_core.py:616-636`; no fallback path that plans inline exists — confirmed by reading |
| `apt_sync._write_or_remove_repo_item` | `RemoteExecutor.send_file` | Stages to `~/.cache/pc-switcher/apt-staging`, never `/etc/apt` directly; promotes with `sudo install -o root -g root -m 0644` | ✓ WIRED | `apt_sync.py:1181-1290`; the one `send_file(` call in the file targets `staged_dest`, not a `/etc` path — confirmed by grep across all three jobs (only one `send_file` call total, in `apt_sync.py`) |
| `apt_sync._ensure_repo_group_converged` | rollback-on-`apt-get update`-failure | Backs up every touched dest before writing; restores + re-probes on failure | ✓ WIRED | `apt_sync.py:1148-1240` |
| `package_review.review_items` | removal groups | `_is_removal_direction` / `checked=not removal` | ✓ WIRED | Confirmed by test: `test_install_group_defaults_checked_removal_group_defaults_unchecked`, `test_no_group_mixes_install_and_removal_entries_in_one_prompt` |
| `_diff_apt_packages` (VERSION_MISMATCH) | `apply()` | `REPORT_ONLY` diffs excluded from `apply_diffs` regardless of decision (`decisions.get(...) == APPLY and diff.action != REPORT_ONLY`) | ✓ WIRED | `package_sync_core.py:427-431`; a version mismatch can never be converged even if somehow ticked |
| `PackageSyncJob._record_permanent_skips` | `DecisionFile` (source vs target) | D-08a routing: INSTALL/CHANGE → `self.source`; REMOVE → `self.target` | ✓ WIRED | `package_sync_core.py:554-591` |
| `folder_sync._decision_file_exclude_filters` | rsync `--filter` | Unconditional (not gated on any job enable flag), GLOBAL-FIRST | ✓ WIRED | `folder_sync.py:438-464`; confirmed by test `test_decision_file_exclude_precedes_merge_filter` |
| `orchestrator._summarize_job_outcomes` | `SessionStatus` | Derives FAILED from `JobResult.status`, not from "nothing raised" | ✓ WIRED | `orchestrator.py:174-188`; `PackageItemFailures` caught per-job (`orchestrator.py:1154-1176`), recorded as `JobStatus.FAILED`, does NOT re-raise (other package managers keep running per D-27) |
| `cli._run_sync` | process exit code | `if session.status is SessionStatus.FAILED: return 1` | ✓ WIRED | `cli.py:358-361` |

### Behavioral Spot-Checks

All single-named-test runs (never a full-suite filter), each proving a real control-flow/state-transition invariant, not mere presence:

| Behavior | Command | Result | Status |
| -------- | ------- | ------ | ------ |
| Both jobs plan before the one review runs, decisions distributed by item-id membership | `pytest tests/unit/jobs/test_package_phase.py -k test_both_jobs_plan_before_review_which_runs_once_then_accept_review` | 1 passed | ✓ PASS |
| Removal group defaults unchecked; install group defaults checked | `pytest tests/unit/jobs/test_package_review.py -k test_install_group_defaults_checked_removal_group_defaults_unchecked` | 1 passed | ✓ PASS |
| No group ever mixes install and removal entries | `pytest tests/unit/jobs/test_package_review.py -k test_no_group_mixes_install_and_removal_entries_in_one_prompt` | 1 passed | ✓ PASS |
| Install-side downgrade refused, names the downgrade | `pytest tests/unit/jobs/test_apt_sync.py -k test_downgrade_in_install_simulation_refuses_and_names_the_downgrade` | 1 passed (logged: "refused: apt-get -s would install pkg-a at 1.0, a downgrade from the currently installed 2.0") | ✓ PASS |
| Collateral removal from an apt transaction refuses the install and names the collateral package | `pytest tests/unit/jobs/test_apt_sync.py -k test_collateral_removal_refuses_install_and_names_the_package` | 1 passed | ✓ PASS |
| Unapproved collateral removal on the remove path also refuses | `pytest tests/unit/jobs/test_apt_sync.py -k test_unapproved_collateral_removal_refuses_and_names_the_package` | 1 passed | ✓ PASS |
| snap install/change/retrack/removal never issues `--hold` | `pytest tests/unit/jobs/test_snap_sync.py -k test_install_change_retrack_and_removal_never_set_a_hold` | 1 passed | ✓ PASS |
| flatpak remotes converge before refs that depend on them | `pytest tests/unit/jobs/test_flatpak_sync.py -k test_remotes_converge_before_refs_that_depend_on_them` | 1 passed | ✓ PASS |
| apt/snap/flatpak run before folder_sync in discovery order | `pytest tests/unit/orchestrator/test_config_system.py -k test_package_jobs_precede_folder_sync` | 1 passed | ✓ PASS |
| `PackageItemFailures` marks the job FAILED, session FAILED, exit code non-zero, without aborting the run | `pytest tests/unit/orchestrator/test_session_status_from_job_results.py -q` (7 tests) | 7 passed | ✓ PASS |
| Full unit suite | `uv run pytest -q` | 974 passed, 69 deselected in 8.26s | ✓ PASS |
| Lint / format / types | `uv run ruff check .` / `uv run ruff format --check .` / `uv run basedpyright` | all clean, 0 errors | ✓ PASS |
| VM integration suite exists and collects correctly | `uv run pytest --collect-only -q -m integration tests/integration/jobs/test_package_sync.py` | 8 tests collected (2 tracer + 6 whole-run contracts) | ? SKIP — cannot execute, no VM access (`HCLOUD_TOKEN` and `PC_SWITCHER_TEST_PC1_HOST`/`PC2_HOST` confirmed unset via `env | grep`) |

### Probe Execution

N/A — no `scripts/*/tests/probe-*.sh` convention exists in this project and none is declared by any Phase 2 plan or SUMMARY.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
| ----------- | ----------- | ------------ | ------ | -------- |
| REQ-sync-scope-packages | 02-01 .. 02-13 | apt/snap/flatpak/.deb/PPA/install-script package sync + `/etc/apt` state | Marked Complete in REQUIREMENTS.md — **justified for code delivery**, code-level truths verified; **the live-system claim (SC1/SC2) is not yet proven**, pending VM-integration CI run | All jobs implemented, unit-tested; VM test exists but unexecuted (see behavior_unverified_items) |
| REQ-conflict-detection-no-resolution | 02-02, 02-03, 02-05, 02-06 | Conflict/version-mismatch detection, reported not auto-resolved | Marked Complete — **justified**, this requirement is inherently about pc-switcher's own decision logic (not live package-manager state) and is comprehensively proven by real behavioral unit tests | Coordinator ordering, transaction guard, removal-group unchecked-by-default all behaviorally tested and passing |

No orphaned requirements found (both IDs REQUIREMENTS.md maps to "Phase 2" appear in at least one plan's `requirements` field).

### Anti-Patterns Found

None in any Phase-2-touched file. Checked every `src/pcswitcher/jobs/apt_sync.py`, `snap_sync.py`, `flatpak_sync.py`, `package_items.py`, `package_phase.py`, `package_review.py`, `package_state.py`, `package_sync_core.py`, `folder_sync.py`, `config_sync.py`, `orchestrator.py`, `cli.py` for `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER` — zero hits in phase-2 code. One pre-existing `# TODO: Add config snapshot` in `orchestrator.py:318` predates this phase (git blame: 2025-11-30, Phase 1 authorship) and is out of Phase 2's scope; not a Phase 2 debt marker.

### Cross-AI Review Findings — Fix Verification (02-REVIEWS.md, 9 findings)

All nine confirmed-real findings from the Codex review were checked directly against the current code (not the SUMMARY narrative):

| Finding | Fixed? | Evidence |
| ------- | ------ | -------- |
| HIGH — no true cross-manager batched review | ✓ Fixed | `PackagePhaseCoordinator` (see Key Link Verification, spot-check 1) |
| HIGH — `send_file()` into `/etc/apt` impossible | ✓ Fixed | Stage-then-`sudo install` (see Key Link Verification) |
| HIGH — snippet registry not wired into `config_sync.py` | ✓ Fixed | `SYNCED_CONFIG_FILENAMES` includes `package-snippets.yaml` |
| HIGH — apt install/remove lack transaction-collateral enforcement | ✓ Fixed | `simulate_apt_transaction` + collateral-refusal tests (spot-checks 5, 6) |
| HIGH — failed `apt-get update` can leave target broken, no rollback | ✓ Fixed | `_ensure_repo_group_converged` backup/restore/reprobe |
| MEDIUM — `folder_sync` cannot see sibling job enablement | ✓ Fixed | `JobContext.enabled_sync_jobs` populated from full `sync_jobs` map |
| MEDIUM — "mandatory registration" self-contradictory | ✓ Fixed | `_unresolved_as_failures` fails the job when an unreproducible item is left unresolved after an interactive review |
| MEDIUM — continue-on-failure test couldn't exercise D-27 | Not directly re-verifiable here (VM-test content) — `02-11-PLAN.md`/`02-VALIDATION.md` record the fix (switched to a failing-snippet exit-code case); unexecuted pending VM access | — |
| LOW — undeclared TUI spike driver artifact | ✓ Fixed (per 02-02-SUMMARY.md; not independently re-checked, low severity, non-blocking) | — |

### Human Verification Required

See frontmatter `human_verification` (4 items) and `behavior_unverified_items` (2 items) — both reproduced here for readability:

1. **Run the VM integration suite** (`tests/integration/jobs/test_package_sync.py`, 8 tests) against real pc1/pc2 test VMs, or via the next non-draft PR's CI run.
   Expected: all 8 pass, proving real apt/snap/flatpak convergence and the six whole-run contracts.
   Why human: no VM access in this environment (`HCLOUD_TOKEN`, `PC_SWITCHER_TEST_PC1_HOST`, `PC_SWITCHER_TEST_PC2_HOST` all unset).

2. **Interactive batched review screen** (D-24): confirm real terminal rendering, grouping, and tick/untick behavior with a live TTY and a paused Rich Live panel.
   Why human: unit tests stub `questionary.checkbox()`; real `prompt_toolkit` rendering is never exercised.

3. **On-the-fly snippet capture** (02-07 Task 2): confirm the interactive multi-line snippet editor's real ergonomics.
   Why human: unit tests stub `questionary.text()`/`questionary.select()`.

4. **End-to-end real-machine confirmation of all three roadmap success criteria** (02-12 Task 3, explicitly deferred).
   Why human: requires two physical machines and interactive access; recorded as deferred, not performed, in 02-12-SUMMARY.md.

## Gaps Summary

No code-level gaps: every artifact this phase claims exists, is substantive, and is wired — including the six load-bearing promises singled out for scrutiny (batched cross-manager review before any change; removals visibly separate and unchecked by default; version mismatches reported, never force-downgraded; machine-specific items inert in both roles via a never-synced decision file; `/etc/apt` writes staged through a user-writable path and promoted with sudo; a failed run derives its exit code from job results, not from "nothing raised"). All nine cross-AI review findings that triggered the replan are independently confirmed fixed in the current code, not merely claimed fixed in a SUMMARY.

The phase is NOT fully closed, however: two of the three roadmap success criteria (SC1 — package presence replication; SC2 — .deb/PPA/install-script reproduction) assert outcomes on *real* package-manager state, and the only tests that exercise real apt/snap/flatpak state (the 8 VM-integration tests) are unexecuted in this environment — no VM access. This is a genuine environment limitation, not a code defect: the tests exist, are correctly collected, and are deselected by default per this project's established ADR-008 pattern. Marking REQ-sync-scope-packages/REQ-conflict-detection-no-resolution "Complete" in REQUIREMENTS.md is a reasonable characterization of code delivery, but the phase's live-system promise remains provisional until the VM suite runs (CI on the next PR to main) and the two deferred human checkpoints (02-02 Task 3, 02-12 Task 3) are performed.

---

*Verified: 2026-07-23T12:52:10Z — Verifier: Claude (gsd-verifier)*

## Post-Verification Update: VM Suite Executed

The two success criteria recorded above as `PRESENT_BEHAVIOR_UNVERIFIED` were unverifiable at verification time only because this environment has no VM access. They have since been executed in CI on PR #206.

Result: **60 passed, 5 skipped** in 20m47s against real Hetzner VMs (`Integration Tests` job 89232547304), including all 8 package-sync integration tests. Both criteria are now verified against real apt/snap/flatpak state.

Two defects surfaced only by that run, both fixed with regression tests:

- `sync_config_to_target` read `<parent>/config.yaml` instead of the path its caller passed, breaking every caller whose config is not literally named `config.yaml` (7 pre-existing tests). Introduced when config sync was generalised to carry the snippet registry; invisible to the unit suite because every unit fixture happens to be named `config.yaml`.
- `_find_removable_candidates` probed reverse dependencies for the whole shared-package set, one `apt-cache rdepends` process each, exceeding its command timeout and failing all 6 package-sync tests in setup.

Remaining unverified: the two deferred human checkpoints (02-02 questionary/Live TUI rendering, 02-12 documentation walkthrough). Neither is machine-checkable.
