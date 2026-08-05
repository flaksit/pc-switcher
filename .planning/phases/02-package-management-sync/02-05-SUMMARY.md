---
phase: 02-package-management-sync
plan: 05
subsystem: infra
tags: [apt, package-sync, tdd, diff-engine, review, removal, dpkg]

requires:
  - phase: 02-package-management-sync
    provides: "AptPackageItem/ItemDiff/DiffClass/DiffAction and the PackageSyncJob plan()/apply() split, PackagePhaseCoordinator (plan 02-03)"
provides:
  - "compare_deb_versions — dpkg-delegated Debian version ordering (epoch/tilde/revision correct)"
  - "HoldPinFact + build_held_or_pinned_detail/build_version_mismatch_detail/build_repo_unavailable_detail"
  - "SnapItem, FlatpakItem, FlatpakRemoteItem, UnreproducibleItem — the complete manager item-shape registry"
  - "PackageSyncJob._diff_apt_packages producing all six D-25 diff classes (missing/extra/version-mismatch/held-or-pinned/repo-unavailable/unreproducible-capable)"
  - "PackageSyncJob.collect_hold_pin_facts/collect_unavailable_item_ids hooks"
  - "(ItemClass, DiffAction) review-group action vocabulary; groups keyed by (manager, action)"
  - "AptSyncJob removal converge (apt-get remove), removal guard, downgrade guard, plan-time batched apt-get -s collateral simulation"
affects: [02-06, 02-07, 02-08, 02-09, 02-10, 02-11, 02-12, 02-13]

tech-stack:
  added: []
  patterns:
    - "PackageSyncJob hooks that default to a no-op (collect_hold_pin_facts/collect_unavailable_item_ids) let apt-only concepts stay in AptSyncJob without snap_sync/flatpak_sync implementing them."
    - "(ItemClass, DiffAction) -> verb vocabulary as module-level data (not per-job string formatting) is what makes 'the review names the concrete action, never apply' mechanically checkable and extensible to future item classes."
    - "Plan-time batched simulation (whole candidate set, at most two apt-get -s calls) + apply-time per-item simulation are two different guarantees that legitimately disagree when a user approves only part of a reviewed set — both are needed, neither substitutes for the other."

key-files:
  created:
    - tests/unit/jobs/test_package_items.py
    - tests/unit/jobs/test_package_sync_core.py
  modified:
    - src/pcswitcher/jobs/package_items.py
    - src/pcswitcher/jobs/package_sync_core.py
    - src/pcswitcher/jobs/apt_sync.py
    - tests/unit/jobs/test_apt_sync.py

key-decisions:
  - "HELD_OR_PINNED precedence: an item present on the target and named by a hold/pin fact always reports as HELD_OR_PINNED, even when its versions also differ or it would otherwise be a removal candidate — the hold/pin fact is itself the more informative, review-worthy thing to show. Documented as a precedence rule in _diff_apt_packages's docstring; not covered by an explicit plan bullet, so recorded here as the resolution."
  - "collect_unavailable_item_ids takes the missing-item-id set as a parameter (not zero-arg per the plan's literal 'hooks that default to returning nothing' phrasing) because REPO_UNAVAILABLE only makes sense relative to what's actually missing on the target — plan() computes that set once and passes it in, avoiding a second full capture/query round-trip inside the hook."
  - "AptSyncJob overrides plan() (rather than a third generic hook) to layer plan-time collateral simulation on top of the base diff: collateral simulation is apt-only machinery (apt-get -s), not something snap_sync/flatpak_sync need, so it did not warrant widening the shared hook surface the way hold/pin and unavailable-id detection did."
  - "AptTransactionPreview gained install_versions (a name -> (old_version | None, new_version) map), additive and backward-compatible with the tracer's installs/removals fields, so the downgrade guard can compare real versions via compare_deb_versions instead of re-parsing apt-get -s output a second time."

patterns-established:
  - "_ACTION_VOCABULARY.get((item_class, action), action.value) is the review's own backstop: a missing vocabulary entry degrades to the bare DiffAction word rather than dropping the group, so no diff class the engine produces can be silently absent from the review."

requirements-completed: []

coverage:
  - id: D1
    description: "compare_deb_versions delegates version ordering to dpkg --compare-versions (never hand-rolled epoch/tilde parsing); an epoch-bearing version like 2:1.0 correctly ranks above 10.0"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_items.py::TestCompareDebVersions::test_gt_for_epoch_beats_larger_upstream_number"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_items.py::TestCompareDebVersions::test_real_dpkg_confirms_epoch_and_revision_ordering"
        status: pass
    human_judgment: false
  - id: D2
    description: "HoldPinFact + build_held_or_pinned_detail keep a hold (apt-mark showhold) and a pin (preferences.d) distinguishable facts even though both surface under one HELD_OR_PINNED category"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_items.py::TestHoldPinFactAndBuildDetail::test_hold_and_pin_diffs_carry_different_mechanism_values"
        status: pass
    human_judgment: false
  - id: D3
    description: "SnapItem, FlatpakItem, FlatpakRemoteItem and UnreproducibleItem complete the item-shape registry; scope/origin fold into item_id so the same name in a different context yields a distinct item_id with no special-casing in a manager job"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_items.py::TestFlatpakItem::test_same_application_different_scope_yields_distinct_item_ids"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_items.py::TestFlatpakRemoteItem::test_same_remote_name_byte_identical_url_different_scope_yields_distinct_item_ids"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_items.py::TestUnreproducibleItem::test_same_identifier_different_origin_yields_distinct_item_ids"
        status: pass
    human_judgment: false
  - id: D4
    description: "The apt diff engine produces every D-25 class: missing-on-target/INSTALL, extra-on-target/REMOVE, version-mismatch/REPORT_ONLY (both versions in detail), held-or-pinned/REPORT_ONLY, repo-unavailable/REPORT_ONLY instead of a proposed install"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestDiffEngine::test_missing_on_target_yields_install"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestDiffEngine::test_extra_on_target_yields_remove"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestDiffEngine::test_version_mismatch_yields_report_only_with_both_versions"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestDiffEngine::test_hold_fact_yields_held_or_pinned_naming_the_hold_mechanism"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestUnavailableCapture::test_no_candidate_package_is_reported_not_installed"
        status: pass
    human_judgment: false
  - id: D5
    description: "Hold-vs-pin end to end through AptSyncJob.plan(): apt-mark showhold read on both machines, preferences.d Package: stanzas parsed on the target, both surfacing as HELD_OR_PINNED with distinguishable detail in the same review"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestHoldPinCapture::test_hold_and_pin_wired_end_to_end_both_held_or_pinned_and_distinguishable"
        status: pass
    human_judgment: false
  - id: D6
    description: "Review groups are keyed by (manager, action); a removal group's title names the concrete verb and the word 'apply' never appears in any group title"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestReviewGroupsByAction::test_removal_group_title_names_a_removal_verb_never_apply"
        status: pass
    human_judgment: false
  - id: D7
    description: "apply() routes INSTALL/REMOVE/CHANGE diffs to converge(); a REPORT_ONLY diff never reaches converge() even if its decision is APPLY"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestConvergeDispatchByAction::test_report_only_diff_produces_zero_target_commands"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestConvergeDispatchByAction::test_remove_diff_produces_exactly_one_target_converge_call"
        status: pass
    human_judgment: false
  - id: D8
    description: "apt removal converges via apt-get remove (not purge) for exactly the one approved package"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestRemovalConverge::test_remove_diff_issues_real_apt_get_remove_for_that_package_alone"
        status: pass
    human_judgment: false
  - id: D9
    description: "Removal guard: an approved removal whose apt-get -s simulation also removes an unapproved package is refused with a failure naming that package and issues no real apt-get remove; when every collateral removal was itself approved, the real removal proceeds"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestRemovalGuard::test_unapproved_collateral_removal_refuses_and_names_the_package"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestRemovalGuard::test_both_removals_approved_the_first_proceeds"
        status: pass
    human_judgment: false
  - id: D10
    description: "Downgrade guard: an approved install whose simulation would reinstall an already-present package at a version compare_deb_versions ranks lower is refused, naming the downgrade"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestDowngradeGuard::test_downgrade_in_install_simulation_refuses_and_names_the_downgrade"
        status: pass
    human_judgment: false
  - id: D11
    description: "Plan-time collateral: two BATCHED apt-get -s simulations (at most, regardless of candidate-set size) surface every package apt would additionally remove or downgrade as its own REPORT_ONLY review entry in its own group, before any decision; a clean simulation adds no collateral entry"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestPlanTimeCollateral::test_collateral_removal_surfaces_as_report_only_in_its_own_group"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestPlanTimeCollateral::test_clean_simulation_produces_no_collateral_entry"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestPlanTimeCollateral::test_at_most_two_apt_get_dash_s_commands_regardless_of_package_count"
        status: pass
    human_judgment: false
  - id: D12
    description: "dry_run=True produces zero mutating target commands across all four action types; ticking only the install group results in zero removal commands"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestConvergeDispatchByAction::test_dry_run_zero_mutating_commands_across_all_four_action_types"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py::TestConvergeDispatchByAction::test_ticking_only_install_group_yields_zero_removal_commands"
        status: pass
    human_judgment: false
  - id: D13
    description: "A human confirms a dry-run against a target with a genuinely diverged package set shows all applicable diff classes in the review with removals separately grouped, against real pc1/pc2 VMs"
    verification: []
    human_judgment: true
    rationale: "The plan's own <verification> section names this as VM-level end-to-end proof; this autonomous run has no VM access. Every mocked-executor behavior bullet is unit-covered above; real apt/dpkg/sudo behavior on a live diverged machine is unverified here by design, matching the precedent plan 02-03 set (deferred to plan 02-13's VM-level suite)."

duration: 25min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 05: Apt Diff Taxonomy and Removal Path Summary

**`PackageSyncJob._diff_apt_packages` now produces all six D-25 diff classes (missing/extra/version-mismatch/held-or-pinned/repo-unavailable/unreproducible-capable), apt packages converge in both the install and remove directions with a two-layer apt-get -s guard (plan-time batched collateral simulation + apply-time per-item removal/downgrade guard), and `package_items.py` gained the complete SnapItem/FlatpakItem/FlatpakRemoteItem/UnreproducibleItem shape registry for the manager jobs still to come.**

## Performance

- **Duration:** 25 min
- **Started:** 2026-07-23T08:33:19+02:00
- **Completed:** 2026-07-23T08:58:48+02:00
- **Tasks:** 2
- **Files modified:** 6 (2 created, 4 modified)

## Accomplishments

- `package_items.py`: `compare_deb_versions` (shells out to `dpkg --compare-versions`, short-circuits identical strings, `shlex.quote`d operands), `HoldPinFact` plus the three `build_*_detail` helpers, and the four remaining item dataclasses (`SnapItem`, `FlatpakItem`, `FlatpakRemoteItem`, `UnreproducibleItem`) completing the phase's single item-shape registry.
- `package_sync_core.py`: `_diff_apt_packages` extended from the tracer's one direction to all five apt-relevant D-25 classes, with `HELD_OR_PINNED` taking precedence over any other outcome for a target-held/pinned name. Two new hooks (`collect_hold_pin_facts`, `collect_unavailable_item_ids`) default to no-op so `snap_sync`/`flatpak_sync` inherit them for free. Review groups are keyed by `(manager, action)` with titles sourced from a module-level `(ItemClass, DiffAction) -> verb` vocabulary (falling back to the bare action word for any pair not yet listed, so no diff class can be silently dropped from the review). `apply()` now excludes `REPORT_ONLY` diffs from ever reaching `converge()` and routes `REMOVE`/`CHANGE` alongside `INSTALL`.
- `apt_sync.py`: a real removal path (`apt-get remove`, not purge) with the same simulate-then-guard shape as installs; the removal guard's rule is "removes nothing the user did not approve" (checked against every approved removal in the run, not just the one item), and the install guard is widened to also refuse a collateral downgrade via `compare_deb_versions`. `plan()` is overridden to run two BATCHED `apt-get -s` simulations (whole install candidate set, whole removal candidate set — never one per package) and fold any collateral removal/downgrade into its own `REPORT_ONLY` review entry before the user decides anything. `collect_hold_pin_facts` reads `apt-mark showhold` on both machines plus the target's `/etc/apt/preferences.d/*` `Package:` stanzas via one `find -exec awk` call; `collect_unavailable_item_ids` runs one batched `apt-cache policy` call over the missing-on-target set.
- 27 new unit tests across `test_package_items.py` (20), `test_package_sync_core.py` (15, new file isolating the shared diff/grouping/converge-dispatch pipeline from apt-specific mechanics), and 12 additions to `test_apt_sync.py`; 2 pre-existing `test_apt_sync.py` tests updated where the tracer's own documented boundary (extra-on-target producing nothing) was superseded by this plan's design.

## Task Commits

Each task followed RED (test) then GREEN (feat), confirmed by temporarily reverting the implementation and observing the new tests fail before restoring it:

1. **Task 1: Deb version comparison, the full diff-class taxonomy, and the manager item shapes**
   - `479af29` (test) — RED: 20 tests against the not-yet-existing symbols.
   - `07af527` (feat) — GREEN: `compare_deb_versions`, `HoldPinFact`, `build_*_detail`, `SnapItem`, `FlatpakItem`, `FlatpakRemoteItem`, `UnreproducibleItem`.
2. **Task 2: Direction-aware review grouping and the removal path**
   - `7a40f54` (test) — RED: 15 new + 12 additional/updated tests against the tracer's one-directional implementation.
   - `16c942f` (feat) — GREEN: the full diff dispatch, review-group vocabulary, removal converge, removal/downgrade guards, plan-time collateral simulation.

**Plan metadata:** (this commit)

## TDD Gate Compliance

Both tasks confirm the RED gate genuinely failed (verified via `git stash` of the implementation file(s), running the new/extended tests, observing collection errors or assertion failures, then restoring and re-running to GREEN) rather than being reconstructed after the fact. `git log --oneline` shows `test(...)` immediately before each task's `feat(...)` commit.

## Files Created/Modified

- `src/pcswitcher/jobs/package_items.py` - `compare_deb_versions`, `HoldPinFact`, `build_held_or_pinned_detail`, `build_version_mismatch_detail`, `build_repo_unavailable_detail`, `SnapItem`, `FlatpakItem`, `FlatpakRemoteItem`, `UnreproducibleItem`
- `src/pcswitcher/jobs/package_sync_core.py` - `_diff_apt_packages` (all D-25 classes), `collect_hold_pin_facts`/`collect_unavailable_item_ids` hooks, `_ACTION_VOCABULARY`/`_ACTION_ORDER`, `apply()`'s REPORT_ONLY exclusion
- `src/pcswitcher/jobs/apt_sync.py` - `_converge_remove`, `_approved_removal_names`, `collect_hold_pin_facts`/`collect_unavailable_item_ids` overrides, `plan()` override with `_collect_plan_time_collateral`, widened `AptTransactionPreview` (`install_versions`), `_packages_with_no_candidate`
- `tests/unit/jobs/test_package_items.py` - 20 tests (new file) covering every Task 1 behavior bullet
- `tests/unit/jobs/test_package_sync_core.py` - 15 tests (new file) covering the shared diff engine, review grouping, and converge dispatch independent of apt-specific mechanics
- `tests/unit/jobs/test_apt_sync.py` - 12 new tests (hold/pin capture, repo-unavailable capture, removal converge, removal guard, downgrade guard, plan-time collateral) + 4 tests updated for behavior this plan intentionally changed (extra-on-target now produces a diff; `apt-get -s` now legitimately runs during `plan()`; a batched simulation now legitimately names not-yet-decided packages)

## Decisions Made

See `key-decisions` in frontmatter: `HELD_OR_PINNED` precedence over version-mismatch/removal, `collect_unavailable_item_ids`'s parameterized signature, `AptSyncJob.plan()` override (not a third generic hook) for collateral simulation, and the additive `AptTransactionPreview.install_versions` field.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — updated tests describing behavior this plan intentionally supersedes] Four `test_apt_sync.py` assertions updated**
- **Found during:** Task 2, after implementing the removal path and plan-time collateral simulation
- **Issue:** Four pre-existing tests asserted behavior the tracer plan (02-03) documented as its own explicit boundary, now superseded by this plan's design: (1) `test_diff_is_symmetric_blind_extra_on_target_produces_no_item` asserted an extra-on-target item produces no diff — now produces `EXTRA_ON_TARGET`/`REMOVE`, which is this plan's whole point; (2/3) two tests asserted `"apt-get -s" not in cmd` during `plan()`/dry-run — now false because plan-time collateral simulation deliberately runs `apt-get -s` (a read-only command) during `plan()`; (4) one test asserted `"pkg-b" not in any command` when only `pkg-a` was approved — now false because the batched plan-time simulation legitimately names every candidate in the set before any decision exists, and one asserted an exact simulation-call count of 3 that the new plan-time batched call raises to 4.
- **Fix:** Renamed/rewrote each assertion to check the actual invariant under test (no REAL mutating command, not "no substring anywhere"), with a comment explaining why the old assertion no longer holds.
- **Files modified:** `tests/unit/jobs/test_apt_sync.py`
- **Verification:** Full suite green (799 passed); each updated test's docstring/comment states the superseded assumption.
- **Committed in:** `7a40f54` (test), `16c942f` (feat)

---

**Total deviations:** 1 auto-fixed (test updates for intentionally-superseded tracer-slice boundaries)
**Impact on plan:** No scope creep — every changed assertion corresponds directly to a behavior this plan's own task list required.

## Issues Encountered

None beyond the test updates documented above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

Every apt-relevant D-25 diff class now exists, is guarded before it ever reaches the target, and is presented to the review with a concrete action verb. `package_items.py`'s registry is complete for every `ItemClass` a manager job will populate; plans 02-07 (snap_sync), 02-08/02-09 (flatpak_sync, unreproducible detection) import `SnapItem`/`FlatpakItem`/`FlatpakRemoteItem`/`UnreproducibleItem` directly rather than adding their own, and inherit `collect_hold_pin_facts`/`collect_unavailable_item_ids`'s no-op default without needing to know apt exists. Plan 02-06 (apt sources/keys/pins/config) is the next and last plan permitted to modify `package_items.py`, per this plan's own key-link note.

The plan's own `<verification>` section names one VM-level check (a dry-run against a genuinely diverged package set, real removals separately grouped) that this autonomous run has no VM access to perform — deferred to plan 02-13's end-to-end suite, the same precedent plan 02-03 set for its own tracer-level VM proof.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: src/pcswitcher/jobs/package_items.py
- FOUND: src/pcswitcher/jobs/package_sync_core.py
- FOUND: src/pcswitcher/jobs/apt_sync.py
- FOUND: tests/unit/jobs/test_package_items.py
- FOUND: tests/unit/jobs/test_package_sync_core.py
- FOUND: tests/unit/jobs/test_apt_sync.py
- FOUND: commit 479af29
- FOUND: commit 07af527
- FOUND: commit 7a40f54
- FOUND: commit 16c942f
