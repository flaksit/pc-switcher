---
phase: 02-package-management-sync
verified: 2026-07-24T11:13:19Z
status: human_needed
score: 3/3 must-haves verified
behavior_unverified: 0
overrides_applied: 0
re_verification:
  previous_status: human_needed
  previous_score: 1/3
  gaps_closed:
    - "SC1: apt/snap/flatpak whole-run convergence — now VM-integration-proven GREEN in CI on PR #206 (Integration Tests job SUCCESS, ran 10:38→11:01Z)"
    - "SC2: manual/PPA/install-script reproduction — test_manual_installs_sync_pushes_registry_and_replays_snippet and test_apt_repository_state_dry_run_reviews_source_and_key_separately (un-skipped, closes broken-window ledger #2) both GREEN in CI"
  gaps_remaining: []
  regressions: []
  notes:
    - "Architecture corrected between the two verifications: the cross-manager PackagePhaseCoordinator (package_phase.py) was removed (D-15/02-15); review now happens per-manager inside each job's execute() via accept_review(). package_*.py moved into jobs/packages/ (D-31/02-19). New fourth job manual_installs_sync.py owns unreproducible detection + snippet registry, self-pushes package-snippets.yaml, and applies authored snippets the SAME run (corrected D-23/02-22, 02-23). config_sync reverted to config.yaml only. The prior VERIFICATION.md referenced the pre-delta paths."
human_verification:
  - test: "Real-TTY interactive batched review (02-02 Task 3): run a real sync with packages diverged in both directions on an actual terminal; confirm the questionary checkbox list composes cleanly with the paused Rich Live display, groups installs and removals separately, starts removal items unticked, and that ticking/unticking then apply/skip/skip-always each produce the recorded outcome. Also exercise the on-the-fly multi-line snippet capture editor (02-07 Task 2)."
    expected: "Prompt renders and hands the terminal back to Live cleanly; decisions match what was ticked; an authored snippet lands in ~/.config/pc-switcher/package-snippets.yaml"
    why_human: "CI answers reviews via PCSWITCHER_PACKAGE_REVIEW_AUTOMATION and unit tests stub questionary.checkbox()/select()/text(); real prompt_toolkit rendering, keybindings, and terminal-mode handoff with a live TTY are never exercised by any automated or CI check (RESEARCH Assumption A2; 02-02-SUMMARY.md Task 3 partly deferred to human; 02-VALIDATION.md Manual-Only Verifications)"
  - test: "Physical two-machine end-to-end walkthrough (02-12 Task 3, type=checkpoint:human-verify, gate=blocking, deferred for the autonomous run): on two real machines, confirm all three roadmap success criteria hold end-to-end — package replication, before-any-change conflict/version-mismatch reporting, and machine-specific packages never forced — and manually inspect ~/.config/pc-switcher/*.decisions.yaml and package-snippets.yaml on both ends"
    expected: "Packages replicate; conflicts/version mismatches are reported (never silently converged); machine-specific/skip-always items stay inert; decision and snippet files on both machines reflect the run"
    why_human: "Requires two physical machines plus interactive TUI access; the VM CI suite proves the mechanical convergence and control-flow but drives reviews non-interactively, so a human-driven physical walkthrough with real interactive decisions is not covered by CI (02-12-SUMMARY.md 'Deferred Human Verification')"
---

# Phase 2: Package Management Sync Verification Report

**Phase Goal:** A user can replicate installed packages from source to target across all package sources (apt, snap, flatpak, and manual/unreproducible installs), with conflicts and version mismatches detected and reported rather than silently overwritten.

**Verified:** 2026-07-24T11:13:19Z

**Status:** human_needed

**Re-verification:** Yes — supersedes the 2026-07-23 report (1/3 human_needed), which predated the green VM integration run and the delta/correction replans.

## Goal Achievement

### Observable Truths (ROADMAP §Phase 2 Success Criteria)

| # | Truth | Status | Evidence |
| - | ----- | ------ | -------- |
| 1 | After sync, the target has the same apt, snap, and flatpak packages installed as the source (verifiable by querying each package manager). | ✓ VERIFIED | `apt_sync.py`, `snap_sync.py`, `flatpak_sync.py` each implement capture→diff→converge and inherit plan/accept_review/apply/execute from `packages/sync_core.py`. Behaviorally proven live: VM tests `test_apt_sync_installs_missing_package`, `test_snap_revision_converges_without_hold`, `test_flatpak_installs_into_source_scope_after_remote` ran GREEN in the "Integration Tests" job on PR #206 (`gh pr view 206`: job SUCCESS, 10:38:15→11:01:12Z). Per ADR-008 (unit-tested locally, VM-proven in CI) this is authoritative live-system evidence. 1015 unit tests pass locally. |
| 2 | Manually-installed .debs, custom PPAs, and install-script-sourced packages are reproduced on the target. | ✓ VERIFIED | New fourth job `manual_installs_sync.py`: `_scan_no_candidate_apt_packages` + `_scan_unowned_installs` detect unreproducible items; `SnippetRegistry` (`packages/state.py`) holds install snippets; `after_review()` runs finalize→`_push_snippet_registry` (self-pushes `package-snippets.yaml` via `send_file`)→`_promote_authored_snippets_to_install`, so an authored snippet is APPLIED (not merely transported) the SAME run (corrected D-23). Custom-PPA / repo state (sources/keyrings/pins/apt-config) delivered as apt items in `apt_sync.py`. Behaviorally proven live: `test_manual_installs_sync_pushes_registry_and_replays_snippet` and `test_apt_repository_state_dry_run_reviews_source_and_key_separately` (synthetic repo+key divergence, no longer skipped — closes ledger #2) both GREEN in CI on PR #206. |
| 3 | Package conflicts and version mismatches are detected and reported before any destructive change; machine-specific packages are not forced onto the target. | ✓ VERIFIED | Version mismatches plan as `DiffClass.VERSION_MISMATCH`/`DiffAction.REPORT_ONLY` (D-04, `sync_core.py:297-301`) and `apply()` structurally excludes every REPORT_ONLY diff regardless of decision (`sync_core.py:441`: `decisions[...] == APPLY and action != REPORT_ONLY`) — a mismatch can never be force-converged. apt transaction simulation refuses downgrades/collateral removals by name; machine-local `DecisionFile` + `filter_inert` keep skip-always items inert. Proven by real behavioral unit tests AND live in CI: `test_apt_sync_dry_run_changes_nothing`, `test_non_interactive_skip_all`, `test_skip_always_is_inert_in_both_roles`, `test_each_manager_reviews_before_its_own_mutation`, `test_continue_on_item_failure` (D-27 exit-code) all GREEN on PR #206. |

**Score:** 3/3 truths verified. `behavior_unverified: 0` — every success criterion is behaviorally proven, not present-only. Status is `human_needed` solely because two genuine human-only UAT checkpoints (interactive-TTY rendering and a physical two-machine walkthrough) remain that no automated or CI check can cover; these are distinct from the VM-behavior criteria CI already proves.

### Required Artifacts

| Artifact | Expected | Status | Details |
| -------- | -------- | ------ | ------- |
| `src/pcswitcher/jobs/apt_sync.py` | apt capture/diff/converge, repo+key+pin+config items, transaction guard, staged privileged writes + rollback | ✓ VERIFIED | `accept_review`, transaction guard, staged `send_file`→`sudo install` repo promotion, backup/rollback on `apt-get update` failure |
| `src/pcswitcher/jobs/snap_sync.py` | Snap revision/channel convergence, no-hold guarantee, path export | ✓ VERIFIED | inherits accept_review/apply/execute; hold-free convergence |
| `src/pcswitcher/jobs/flatpak_sync.py` | Flatpak scoped refs, per-scope remotes provisioned first, path export | ✓ VERIFIED | remote-before-ref ordering |
| `src/pcswitcher/jobs/manual_installs_sync.py` | 4th job: unreproducible detection + snippet registry + self-push + same-run promote (corrected D-23) | ✓ VERIFIED | `after_review()` = finalize→`_push_snippet_registry` (send_file)→`_promote_authored_snippets_to_install`; `ManualInstallsSyncJob` exported in `jobs/__init__.py` |
| `src/pcswitcher/jobs/packages/sync_core.py` | Shared `PackageSyncJob` plan/accept_review/apply/execute; REPORT_ONLY excluded from apply | ✓ VERIFIED | per-manager `accept_review()` (no coordinator); apply-diffs filter at line 441 |
| `src/pcswitcher/jobs/packages/review.py` | Batched checkbox review, removals separated + unchecked, unreproducible group | ✓ VERIFIED | `_review_unreproducible_group`, group-by-action |
| `src/pcswitcher/jobs/packages/state.py` | Machine-local `DecisionFile`, `filter_inert`, `SnippetRegistry` | ✓ VERIFIED | atomic writes, degrade-to-empty; `SNIPPET_REGISTRY_RELPATH` |
| `src/pcswitcher/jobs/packages/items.py` | `DiffClass`/`DiffAction` taxonomy incl. `VERSION_MISMATCH`/`REPORT_ONLY`, item shapes | ✓ VERIFIED | imported across all four jobs |
| `src/pcswitcher/config_sync.py` | Reverted to config.yaml only (D-23) — manual_installs_sync self-pushes snippets | ✓ VERIFIED | `CONFIG_REMOTE_PATH = .../config.yaml`; explicit comment "carries exactly ONE file, config.yaml (D-23)"; no `package-snippets` reference |
| `tests/integration/jobs/test_package_sync.py` | VM whole-run contracts for the 4-job/per-manager-review shape | ✓ VERIFIED | 11 tests across TestAptSyncEndToEnd / TestPackageSyncWholeRunContracts / TestManualInstallsSyncEndToEnd; GREEN in CI on PR #206 |

### Key Link Verification

| From | To | Via | Status | Details |
| ---- | --- | --- | ------ | ------- |
| each package job | its own `accept_review()` | Per-manager review inside `execute()` (no coordinator) | ✓ WIRED | `grep` confirms zero `PackagePhaseCoordinator`/`package_phase`/`coordinate_package_review` references remain in `src/` (D-15/02-15 removal) |
| `PackageSyncJob.apply()` | REPORT_ONLY exclusion | apply-diffs filter drops REPORT_ONLY regardless of decision | ✓ WIRED | `packages/sync_core.py:441` |
| `_diff_apt_packages` (VERSION_MISMATCH) | REPORT_ONLY | version mismatch planned as REPORT_ONLY, never CHANGE (D-04) | ✓ WIRED | `packages/sync_core.py:297-301` |
| `manual_installs_sync.after_review` | target `send_file` | `_push_snippet_registry` copies `~/.config/pc-switcher/package-snippets.yaml` to target home (never /etc, no sudo), then promotes authored snippets to INSTALL the same run | ✓ WIRED | `manual_installs_sync.py:186-254`; single `send_file` targets `~/.config`, dry-run/no-file no-ops |
| `config_sync` | config.yaml only | snippet transport removed from config_sync (D-23) | ✓ WIRED | `config_sync.py:25-31` |
| `jobs/__init__` | `ManualInstallsSyncJob` | 4th job exported/registered | ✓ WIRED | `jobs/__init__.py:13,27` |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
| -------- | ------- | ------ | ------ |
| Full unit suite | `uv run pytest -q` | 1015 passed, 71 deselected in 8.39s | ✓ PASS |
| Lint | `uv run ruff check .` | All checks passed | ✓ PASS |
| Types | `uv run basedpyright` | 0 errors, 0 warnings, 0 notes | ✓ PASS |
| No coordinator remains | `grep -rn PackagePhaseCoordinator\|package_phase\|coordinate_package_review src/` | 0 hits | ✓ PASS |
| VM whole-run contracts (SC1/SC2/SC3 live) | PR #206 "Integration Tests" job (`gh pr view 206`) | conclusion SUCCESS, ran 10:38:15→11:01:12Z (~23m) | ✓ PASS (CI, ADR-008) |

### Probe Execution

N/A — no `scripts/*/tests/probe-*.sh` convention exists in this project and none is declared by any Phase 2 plan or SUMMARY.

### Requirements Coverage

| Requirement | Source Plan(s) | Description | Status | Evidence |
| ----------- | ------------- | ----------- | ------ | -------- |
| REQ-sync-scope-packages | 02-01..02-23 | apt/snap/flatpak/.deb/PPA/install-script sync + `/etc/apt` repo state | ✓ SATISFIED | All four jobs implemented + unit-tested; SC1/SC2 VM-proven GREEN in CI on PR #206. REQUIREMENTS.md line 81 marks Complete. |
| REQ-conflict-detection-no-resolution | 02-02, 02-03, 02-05, 02-06, 02-16 | Conflict/version-mismatch detection, reported not auto-resolved | ✓ SATISFIED | REPORT_ONLY apply-exclusion + transaction-guard refusals proven by behavioral unit tests and live CI (`test_non_interactive_skip_all`, `test_continue_on_item_failure`). REQUIREMENTS.md line 82 marks Complete. |

No orphaned requirements — both IDs REQUIREMENTS.md maps to Phase 2 appear in Phase 2 plans and are covered.

### Anti-Patterns Found

None. Scanned `apt_sync.py`, `snap_sync.py`, `flatpak_sync.py`, `manual_installs_sync.py`, and `packages/{items,review,state,sync_core}.py` for `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER`/"not implemented" — zero hits. Broken-window ledger fully closed: `WINDOWS.md` `open_count: 0`, all three recorded entries `status: fixed`.

### Human Verification Required

Two genuinely human-only checkpoints remain. Both are UX/physical checks no automated or CI path can cover; neither reopens a success criterion (all three are CI-proven).

1. **Real-TTY interactive review (02-02 Task 3 + 02-07 Task 2)** — Run a real sync on an actual terminal with packages diverged both directions; confirm the questionary checkbox review composes with the paused Rich Live display, installs/removals grouped separately with removals unticked, decisions match ticks, and the multi-line snippet editor produces a snippet in `~/.config/pc-switcher/package-snippets.yaml`. CI answers reviews non-interactively via `PCSWITCHER_PACKAGE_REVIEW_AUTOMATION`, so live prompt_toolkit rendering/keybindings/terminal handoff are never exercised.

2. **Physical two-machine end-to-end walkthrough (02-12 Task 3, gate=blocking, deferred)** — On two real machines, confirm all three success criteria hold end-to-end and manually inspect `*.decisions.yaml` and `package-snippets.yaml` on both ends. Requires two physical machines + interactive TUI; the VM suite proves mechanical convergence but drives reviews non-interactively.

### Gaps Summary

No gaps. All three roadmap success criteria are behaviorally verified — apt/snap/flatpak convergence, manual/PPA/script reproduction (with same-run snippet application), and conflict/version-mismatch report-before-destroy — via passing unit tests locally and the green VM integration suite in CI on PR #206 (ADR-008 pattern). The delta replan (02-14..02-21) and the snippet-application correction (02-22, 02-23) are reflected in the current code: per-manager review with no coordinator, four jobs, `manual_installs_sync` self-pushing and applying snippets the same run, and `config_sync` reverted to `config.yaml` only. The phase goal is achieved in code and behavior; the two residual items are interactive/physical UAT sign-offs, not code gaps.

---

_Verified: 2026-07-24T11:13:19Z_

_Verifier: Claude (gsd-verifier)_
