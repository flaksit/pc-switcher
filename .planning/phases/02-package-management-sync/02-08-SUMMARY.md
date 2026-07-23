---
phase: 02-package-management-sync
plan: 08
subsystem: infra
tags: [snap, snapd, package-sync, revision-convergence, diff-engine]

requires:
  - phase: 02-package-management-sync
    provides: "SnapItem item shape, PackageSyncJob plan()/apply() split, PackagePhaseCoordinator, DecisionFile/filter_inert (D-08), _build_review_groups (D-24) — plans 02-03/02-05/02-07"
provides:
  - "SnapSyncJob — snap name/channel/revision capture, header-driven `snap list --all` parsing, active revision+channel convergence (D-06)"
  - "snap_sync_exclude_paths() — the ADR-018-style export of `~/snap/<app>/<revision>` for folder_sync (D-29), consumed by plan 02-10"
  - "A local plan()/diff pattern for a manager whose convergence rule differs from apt's (revision/channel drift converges instead of only being reported), reusable by flatpak_sync if its own diff logic diverges from the shared apt-shaped dispatch too"
affects: [02-09, 02-10, 02-11, 02-13]

tech-stack:
  added: []
  patterns:
    - "A manager job whose convergence semantics differ from PackageSyncJob's shared apt-package-shaped diff_items()/_diff_apt_packages() overrides plan() entirely (capture -> DecisionFile/filter_inert -> its own diff -> _build_review_groups) rather than calling super().plan(); accept_review()/apply()/execute() stay inherited unchanged."
    - "When a converge() needs data beyond what ItemDiff.item_id encodes (snap needs the literal revision/channel strings, not just a name), the job caches plan()-time captured items in a private dict keyed by item_id for converge() to look up — the same shape AptSyncJob already uses for its key-digest caches."
    - "All CHANGE-action diffs for one manager share a single ItemClass tag even when the underlying facts differ (revision vs. channel-only drift), because PackageSyncJob._build_review_groups derives one action_label verb per REVIEW GROUP from its first entry's item_class — mixing two item classes under one DiffAction risks the wrong verb winning for some entries. The diff's `detail` text carries the specific fact instead."

key-files:
  created:
    - src/pcswitcher/jobs/snap_sync.py
    - tests/unit/jobs/test_snap_sync.py
  modified: []

key-decisions:
  - "SnapSyncJob overrides plan() instead of inheriting PackageSyncJob.plan() unchanged, contrary to the plan's literal instruction — the base plan() calls diff_items()/_diff_apt_packages(), which is hardcoded to ItemClass.APT_PACKAGE and accesses AptPackageItem.version (SnapItem has no such field); calling it with SnapItem sequences either mislabels every diff's item_class or crashes with AttributeError the moment a snap exists on both machines. Verified live via basedpyright and a throwaway crash trace before committing to this design. The override still reuses DecisionFile/filter_inert and _build_review_groups from the shared core, so only capture/diff/converge are genuinely snap-specific."
  - "capture_source_items()/query_target_items() are typed Sequence[SnapItem], widening PackageSyncJob's abstract-hook declaration (Sequence[AptPackageItem]); basedpyright's strict reportIncompatibleMethodOverride flags this (confirmed via a scratch reproduction), suppressed with a one-line `# pyright: ignore[reportIncompatibleMethodOverride]` plus a comment explaining why it's safe: SnapSyncJob overrides plan() and never routes through the base plan()'s call to these hooks, so no code holding a PackageSyncJob-typed reference ever calls them expecting an AptPackageItem back."
  - "Channel-only changes are NOT tagged ItemClass.SNAP_CHANNEL despite package_sync_core.py's pre-existing `_ACTION_VOCABULARY[(SNAP_CHANNEL, CHANGE)] = \"retrack\"` entry inviting that usage. Using SNAP_CHANNEL for channel-only diffs and SNAP for revision diffs would put both kinds of CHANGE diff in one review group whose SINGLE action_label verb is derived from the group's first entry's item_class (_build_review_groups's own docstring already flags this as unhandled for a manager mixing item classes under one action). Using ItemClass.SNAP uniformly for every snap CHANGE diff avoids that mislabeling risk entirely; the `detail` text (both revisions, or both channels) still satisfies the plan's 'names the concrete fact' requirement without depending on shared-core behavior this plan does not own."
  - "_converge_install always switches the channel after a successful install, rather than conditioning it on 'differs from snapd's default for that snap' as the plan's prose states. `snap list --all` only lists already-installed snaps, so there is no cheap way to learn a not-yet-installed snap's default channel to compare against. Always switching is a strict simplification: re-running `switch` to a channel the install already landed on is a harmless no-op, so unconditional switching converges to the same end state with less code and no extra round trip."

patterns-established:
  - "Header-driven `snap list --all` parsing: read the header row, build a name-to-index map, pull fields by name — verified against a column-reordered fixture (header AND body swapped) so a real snapd column reorder cannot silently corrupt the manifest."

requirements-completed: []

coverage:
  - id: D1
    description: "SnapSyncJob captures each installed snap's name, channel and revision from `snap list --all`, parsed by header column name rather than fixed offsets, and skips a disabled older-revision line so only the active revision becomes an item"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestCapture::test_capture_source_items_parses_name_rev_tracking_by_header"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestCapture::test_column_reordered_header_still_parses_correctly"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestCapture::test_disabled_revision_line_produces_no_item"
        status: pass
    human_judgment: false
  - id: D2
    description: "A snap missing on the target yields an install diff naming the source's revision; a snap on both at a different revision yields a change diff naming both revisions and converging via `snap refresh --revision=N`; a snap at the same revision but a different channel yields a change diff naming both channels; a snap extra on the target yields a removal diff in its own review group"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestDiff::test_missing_on_target_yields_install_diff"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestDiff::test_revision_change_yields_change_diff_naming_both_revisions"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestDiff::test_same_revision_different_channel_yields_change_diff_naming_both_channels"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestDiff::test_extra_on_target_yields_remove_diff_in_its_own_group"
        status: pass
    human_judgment: false
  - id: D3
    description: "No command this job issues across an install, a revision change, a channel-only retrack or a removal ever sets a snap hold; the install command always names an explicit --revision; removal never passes --purge, preserving snapd's own pre-removal snapshot"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestNoHold::test_install_change_retrack_and_removal_never_set_a_hold"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestNoHold::test_install_command_contains_an_explicit_revision"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestConvergeRemoval::test_removal_never_passes_purge"
        status: pass
    human_judgment: false
  - id: D4
    description: "plan() issues only read commands (snap list --all on both machines, a decision-file cat) — no snap install/refresh/switch/remove runs before the plan is returned"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestPlanReadOnly::test_plan_issues_no_mutating_snap_command"
        status: pass
    human_judgment: false
  - id: D5
    description: "snap_sync_exclude_paths() returns the per-revision ~/snap/<app>/<revision> directories and excludes ~/snap/<app>/common and ~/snap/<app>/current"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestExcludePaths::test_returns_revision_dirs_excludes_common_and_current"
        status: pass
    human_judgment: false
  - id: D6
    description: "Orchestrator._resolve_sync_job_class('snap_sync') resolves to SnapSyncJob; the job carries no review of its own (grep -c 'review_items' src/pcswitcher/jobs/snap_sync.py == 0) and, driven through PackagePhaseCoordinator alongside a stub apt_sync-shaped sibling, its accepted outcome contains only snap:-prefixed item ids"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestJobDiscovery::test_orchestrator_resolves_snap_sync_to_snap_sync_job"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestCoordinatorIntegration::test_accepted_outcome_contains_only_snap_prefixed_item_ids"
        status: pass
      - kind: other
        ref: "grep -c 'review_items' src/pcswitcher/jobs/snap_sync.py"
        status: pass
    human_judgment: false
  - id: D7
    description: "validate() reports distinct ValidationErrors when `snap version` fails on source or target and when passwordless sudo is unavailable on target; a read-only snap get system refresh.hold check on both machines is logged only, never acted on"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestValidate::test_snap_unavailable_on_source_yields_validation_error"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestValidate::test_snap_unavailable_on_target_yields_validation_error"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_snap_sync.py::TestValidate::test_target_without_passwordless_sudo_yields_validation_error"
        status: pass
    human_judgment: false
  - id: D8
    description: "A human confirms `snap get system refresh.hold` on both real test VMs returns the same value before and after a snap_sync run, and that a dry-run against genuinely diverged snap state shows every applicable diff class with removals separately grouped"
    verification: []
    human_judgment: true
    rationale: "The plan's own <verification> section names this as a VM-level end-to-end check; this autonomous run has no VM access. Every mocked-executor behavior bullet is unit-covered above (D1-D7); real snapd behavior on a live diverged machine is unverified here by design, matching the precedent set by plans 02-03/02-05/02-06/02-07 (deferred to plan 02-13's VM-level suite)."

duration: 35min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 08: Snap Revision/Channel Convergence Summary

**`SnapSyncJob` converges installed snaps to the source's exact revision and tracking channel via `snap install/refresh --revision=N` and `snap switch --channel=X` (never `snap refresh --hold`), parsing `snap list --all` by header column name so a snapd column reorder can't silently corrupt the manifest, and exports the `~/snap/<app>/<revision>` directories it owns for folder_sync.**

## Performance

- **Duration:** 35 min
- **Started:** 2026-07-23T10:15:00Z (approx.)
- **Completed:** 2026-07-23T10:48:02Z
- **Tasks:** 1
- **Files modified:** 2 (both created)

## Accomplishments

- `snap_sync.py`: `SnapSyncJob(PackageSyncJob)` with header-driven `snap list --all` parsing (`Name`/`Rev`/`Tracking`/`Notes` looked up by column name, disabled-revision lines skipped), a snap-specific `plan()` override that reuses `DecisionFile`/`filter_inert` (D-08) and `_build_review_groups` (D-24) from the shared core, and `converge()` issuing exactly the D-06-safe verbs: `snap install --revision=N` / `snap refresh --revision=N` for install/change, a channel `snap switch` when the channel differs (or after every install), and `snap remove` (never `--purge`) for removal.
- `snap_sync_exclude_paths()`: enumerates `~/snap/<app>/<revision>` directories from the filesystem (dynamic, unlike VS Code's fixed relpath list), excluding `common` and `current` by name.
- `validate()`: `snap version` on both machines, `sudo -n true` on the target, and a read-only `snap get system refresh.hold` probe on both machines logged at FULL as informational context only — never a mutating `snap refresh --hold` call anywhere in the module.
- 21 new unit tests in `tests/unit/jobs/test_snap_sync.py` covering header-based capture (including a column-reordered fixture and a disabled-revision line), every diff class (install/change-revision/change-channel/remove), the D-06 no-hold guarantee across all four converge paths, `plan()`'s read-only property, `snap_sync_exclude_paths()`, job discovery, `validate()`, and a `PackagePhaseCoordinator` integration test with a stub apt-shaped sibling proving `SnapSyncJob`'s accepted outcome carries only `snap:`-prefixed item ids.

## Task Commits

1. **Task 1: SnapSyncJob — header-based capture and revision convergence** - `ec99d26` (feat)

**Plan metadata:** (this commit)

## Files Created/Modified

- `src/pcswitcher/jobs/snap_sync.py` - `SnapSyncJob`, `snap_sync_exclude_paths`, header-driven `snap list --all` parser, snap-specific `plan()`/diff/`converge()`
- `tests/unit/jobs/test_snap_sync.py` - 21 tests covering every behavior bullet in the plan

## Decisions Made

See `key-decisions` in frontmatter. In order of consequence:

1. **`plan()` is overridden, not inherited.** The base `PackageSyncJob.plan()` routes through `diff_items()`/`_diff_apt_packages()`, which is hardcoded to `ItemClass.APT_PACKAGE` and reads `AptPackageItem.version` — a field `SnapItem` does not have. Calling it with `SnapItem` sequences crashes with `AttributeError` the instant a snap exists on both source and target (the common case), and mislabels every diff's `item_class` even when it doesn't crash. Confirmed this would break basedpyright too (a throwaway reproduction in a scratch file reproduced the exact `reportIncompatibleMethodOverride` error before any real code was written). `SnapSyncJob.plan()` reimplements the capture -> diff -> review-groups pipeline locally, reusing every shared building block that IS manager-agnostic (`DecisionFile`, `filter_inert`, `_build_review_groups`).
2. **One `pyright: ignore` for the abstract hook widening**, justified inline: `capture_source_items`/`query_target_items` return `Sequence[SnapItem]`, which is not a subtype of the base's declared `Sequence[AptPackageItem]`. Since `SnapSyncJob` never calls the base `plan()` that would invoke these hooks polymorphically expecting an apt-shaped item, the LSP violation the type checker flags is real but inert for this subclass.
3. **All snap `CHANGE` diffs use `ItemClass.SNAP`, never `ItemClass.SNAP_CHANNEL`**, even for a same-revision, channel-only retrack — despite `package_sync_core.py` already carrying a `(SNAP_CHANNEL, CHANGE): "retrack"` vocabulary entry that invites the opposite choice. `_build_review_groups` derives one action_label verb per review GROUP from its first entry's `item_class`; mixing `SNAP` and `SNAP_CHANNEL` diffs under the shared `CHANGE` action bucket would risk one kind winning the wrong verb for the other whenever both occur in the same run (a scenario the shared helper's own docstring flags as unhandled). Using one item_class avoids the bug entirely; `detail` still names the specific revisions or channels.
4. **`_converge_install` always switches the channel** rather than conditioning it on "differs from snapd's default," which is unknowable from `snap list --all` (it only lists already-installed snaps). Always switching is a strict simplification — idempotent when unnecessary, correct when necessary.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - blocking type/runtime issue] `plan()` overridden instead of inherited unchanged**
- **Found during:** Task 1, while implementing `capture_source_items`/`query_target_items` per the plan's literal instruction to "inherit `plan()`... unchanged"
- **Issue:** The plan's action text says to implement only the three abstract hooks and inherit `plan()` unchanged. The inherited `plan()` calls `PackageSyncJob.diff_items()`, whose only dispatch (`_diff_apt_packages`) is apt-package-shaped: it hardcodes `item_class=ItemClass.APT_PACKAGE` and reads `.version` off every item, a field `SnapItem` doesn't carry. Verified via a scratch basedpyright reproduction that this also fails strict type-checking (`reportIncompatibleMethodOverride` on the abstract hooks) before writing any real code, and via manual trace that it would `AttributeError` at runtime the moment source and target both have the same snap installed.
- **Fix:** `SnapSyncJob.plan()` overrides the base, reimplementing capture -> `DecisionFile`/`filter_inert` -> local snap diff -> `_build_review_groups`, matching the same override pattern `AptSyncJob.plan()` already establishes (though `AptSyncJob` still calls `super().plan()` first since apt packages ARE `AptPackageItem`-shaped; snap items are not, so `SnapSyncJob` cannot).
- **Files modified:** `src/pcswitcher/jobs/snap_sync.py` (this is the plan's own file — no shared-core file touched)
- **Verification:** `uv run pytest tests/unit/jobs/test_snap_sync.py -x` (21/21 pass), `uv run pytest` (887/887 pass), `uv run basedpyright` (0 errors)
- **Committed in:** `ec99d26`

**2. [Rule 3 - blocking type issue] `# pyright: ignore[reportIncompatibleMethodOverride]` on the two abstract hooks**
- **Found during:** Task 1, immediately after deciding `capture_source_items`/`query_target_items` must return `Sequence[SnapItem]`
- **Issue:** `basedpyright --strict` (this project's mode) rejects a covariant-return override where the override's return type isn't a subtype of the base's declared type; `SnapItem` and `AptPackageItem` are unrelated dataclasses.
- **Fix:** One-line `# pyright: ignore[reportIncompatibleMethodOverride]` per hook, each with an inline comment stating why it's safe (deviation 1 above: `SnapSyncJob` never routes through the base `plan()` that would call these hooks expecting an `AptPackageItem`).
- **Files modified:** `src/pcswitcher/jobs/snap_sync.py`
- **Verification:** `uv run basedpyright` clean (0 errors) both for this file and the full project.
- **Committed in:** `ec99d26`

---

**Total deviations:** 2 auto-fixed (both Rule 3, both required to make the plan's own literal "inherit plan() unchanged" instruction produce correct, type-clean code — the underlying design intent, reusing the shared `DecisionFile`/`filter_inert`/`_build_review_groups` machinery, is preserved)
**Impact on plan:** No scope creep. Both deviations are contained entirely within `snap_sync.py`; `package_items.py` and `package_sync_core.py` are untouched (`git diff --stat` confirms zero changes to `package_items.py`), keeping this plan's own acceptance criterion intact and preserving wave-5 parallel-safety with plan 02-09 (`flatpak_sync`, disjoint files).

## Issues Encountered

None beyond the deviations documented above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

`SnapSyncJob` is a complete third-party-manager job on the proven `PackageSyncJob` core: capture, diff, converge and validate are all implemented and unit-tested, and `snap_sync_exclude_paths()` is ready for plan 02-10 to wire into `folder_sync`'s config-schema-driven exclusions. `SnapSyncJob` and `AptSyncJob` now demonstrably coexist under one `PackagePhaseCoordinator` review (D-24) with no per-job self-review, which is the cross-manager seam plan 02-09 (`flatpak_sync`) and plan 02-10 (config wiring) both depend on.

The plan's own `<verification>` section names one VM-level check (real `snap get system refresh.hold` unchanged across a real run, a genuinely diverged dry-run showing every diff class) that this autonomous run has no VM access to perform — deferred to plan 02-13's end-to-end suite, the same precedent plans 02-03/02-05/02-06/02-07 set for their own VM-level proofs.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: src/pcswitcher/jobs/snap_sync.py
- FOUND: tests/unit/jobs/test_snap_sync.py
- FOUND: commit ec99d26
