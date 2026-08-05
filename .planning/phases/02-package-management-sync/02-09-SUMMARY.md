---
phase: 02-package-management-sync
plan: 09
subsystem: infra
tags: [flatpak, ostree, package-sync, scope-as-identity, diff-engine]

requires:
  - phase: 02-package-management-sync
    provides: "FlatpakItem/FlatpakRemoteItem item shapes, PackageSyncJob plan()/apply() split, PackagePhaseCoordinator, DecisionFile/filter_inert (D-08), _build_review_groups (D-24) — plans 02-03/02-05/02-07"
provides:
  - "FlatpakSyncJob — flatpak ref/remote capture via --columns output, scope-as-identity diff, remote-before-ref converge ordering (D-06, D-14)"
  - "flatpak_sync_exclude_paths() — the ADR-018-style export of ~/.local/share/flatpak for folder_sync (D-29), consumed by plan 02-10"
  - "A converge-time missing-origin-remote guard (T-02-24) reusable as the pattern for any future manager whose install verb depends on a second, ordered item class"
affects: [02-10, 02-11, 02-13]

tech-stack:
  added: []
  patterns:
    - "A manager job whose convergence needs TWO item classes with an ordering dependency between them (flatpak: remotes before refs, same class of reason as apt_sync's key-before-source) overrides plan() entirely, like SnapSyncJob, rather than inheriting PackageSyncJob.plan(); the job-local diffs tuple is built as (*ordered-first-class-diffs, *ordered-second-class-diffs) rather than a shared-core sort key, since only two classes are in play."
    - "A converge()-time readiness guard (checking a plan-time-captured set plus a same-run-converged set) is how a job enforces 'X must exist before Y installs' when X and Y converge in the same apply() loop pass — remote-add succeeding populates the converged set converge() itself consults for the next ref install, so no second plan() pass or explicit dependency graph is needed."
    - "Scope-as-identity (scope folded into item_id, not just a field) makes 'same name, different scope on each machine' fall out of the generic source-vs-target diff as an independent install + independent removal with zero special-case code — the diff functions never look at scope at all, only at item_id equality."

key-files:
  created:
    - src/pcswitcher/jobs/flatpak_sync.py
    - tests/unit/jobs/test_flatpak_sync.py
  modified: []

key-decisions:
  - "FlatpakSyncJob overrides plan() instead of inheriting PackageSyncJob.plan() unchanged, contrary to the plan's literal 'implement only the abstract hooks and inherit plan() unchanged' instruction — the base plan() routes through diff_items()/_diff_apt_packages(), which is hardcoded to one item class (APT_PACKAGE) with no notion of a second item class (FLATPAK_REMOTE) that must converge before the first (FLATPAK_REF). This is the identical structural gap 02-08's SnapSyncJob hit and documented; the same fix applies here for a related-but-distinct reason (ordering between two classes, not just a converge-vs-report-only mismatch)."
  - "System-scope flatpak commands (install/uninstall/remote-add/remote-delete) are prefixed with `sudo `, user-scope commands are not — this line is in the plan's own <action> block ('--system needs sudo, --user does not') but is easy to read past since it appears only in the install-ref bullet; applied uniformly to all four converge verbs via one _sudo_prefix(scope) helper so the rule can never drift per-verb."
  - "validate()'s sudo check only runs when _system_scope_in_play() finds a system-scope ref or remote on either machine (ref presence checked via the same capture_source_items()/query_target_items() hooks plan() uses; remote presence via a --system-scoped flatpak remotes probe on both machines) — matching the plan's 'sudo -n true on the target only when at least one system-scope item is in play (ASVS V4)' instruction literally, at the cost of re-running the flatpak list/remotes probes a second time (once in validate(), again in plan()) since the two phases don't share state across a job instance's lifecycle."
  - "Remote URL differences (same name + scope, different URL) are not diffed at all — only presence (missing-on-target -> add, extra-on-target -> delete). Neither the plan's behavior bullets nor D-11 require detecting a URL edit on an existing remote, and flatpak remote-add --if-not-exists is a no-op against an existing name regardless of URL, so there is no converge verb this plan defines that would act on such a diff anyway."

patterns-established:
  - "Column-flag-driven `flatpak list --app --columns=...`/`flatpak remotes --columns=...` parsing: no header row (unlike snap list --all), parsed by FIXED tab-separated position because the --columns flag itself is the source of truth for column order — verified against RESEARCH.md's live Flatpak 1.14.6 output."

requirements-completed: []

coverage:
  - id: D1
    description: "FlatpakSyncJob captures each installed ref's application, version, origin and user/system scope from flatpak list --app --columns=..., and the same application in two scopes produces two items with different identities; flatpak remotes --columns=name,url is captured once per scope so flathub present in both scopes yields two distinct remote items"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestCapture::test_capture_source_items_parses_application_version_origin_scope"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestCapture::test_same_application_both_scopes_yields_two_distinct_identities"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestPlanDiff::test_flathub_present_in_both_scopes_yields_two_remote_items"
        status: pass
    human_judgment: false
  - id: D2
    description: "A ref missing on target yields an install diff; a ref present on both in the SAME scope with a different version yields a report_only version-mismatch diff (D-04, never forced); a ref present in different scopes on each machine yields one install and one removal, never a single change; an extra ref on target yields a removal diff in its own review group; a remote missing on target yields an add diff scoped to that installation"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestPlanDiff::test_full_diff_taxonomy"
        status: pass
    human_judgment: false
  - id: D3
    description: "The job's ordering stage places every approved remote diff before every approved ref diff in plan.diffs, and converge() issues the remote-add before the ref install that depends on it"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestPlanDiff::test_every_remote_diff_precedes_every_ref_diff"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestConverge::test_remotes_converge_before_refs_that_depend_on_them"
        status: pass
    human_judgment: false
  - id: D4
    description: "A ref whose origin remote is neither already present on the target nor among the successfully-converged remotes in this run is skipped with a per-item failure naming the missing remote, rather than issuing an install flatpak will reject"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestConverge::test_ref_with_missing_origin_remote_is_skipped_with_named_failure"
        status: pass
    human_judgment: false
  - id: D5
    description: "converge() prefixes every command with sudo when and only when the item's own scope is system; a user-scope install/uninstall never runs as root and always carries --user, a system-scope one always carries --system and sudo"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestConverge::test_user_scope_ref_install_has_no_sudo_and_carries_user_flag"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestConverge::test_system_scope_ref_install_uses_sudo_and_system_flag"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestConverge::test_ref_removal_never_needs_source_lookup"
        status: pass
    human_judgment: false
  - id: D6
    description: "plan() issues only read commands (flatpak list/flatpak remotes on both machines, a decision-file cat) — no flatpak install/uninstall/remote-add/remote-delete runs before the plan is returned"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestPlanReadOnly::test_plan_issues_no_mutating_flatpak_command"
        status: pass
    human_judgment: false
  - id: D7
    description: "flatpak_sync_exclude_paths() returns ~/.local/share/flatpak only and never ~/.var/app"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestExcludePaths::test_returns_flatpak_data_dir_excludes_var_app"
        status: pass
    human_judgment: false
  - id: D8
    description: "validate() reports a missing flatpak binary on source or target as a ValidationError without raising, and requires target sudo only when a system-scope ref or remote actually exists on either machine (never when only user-scope items are in play)"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestValidate::test_flatpak_unavailable_on_source_yields_validation_error"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestValidate::test_flatpak_unavailable_on_target_yields_validation_error_and_does_not_raise"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestValidate::test_system_scope_item_present_without_sudo_yields_validation_error"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestValidate::test_user_scope_only_never_checks_sudo"
        status: pass
    human_judgment: false
  - id: D9
    description: "Orchestrator._resolve_sync_job_class('flatpak_sync') resolves to FlatpakSyncJob; the job carries no review of its own (grep -c 'review_items' src/pcswitcher/jobs/flatpak_sync.py == 0) and, driven through PackagePhaseCoordinator alongside a stub apt_sync-shaped sibling, its accepted outcome contains only flatpak:-prefixed item ids"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestJobDiscovery::test_orchestrator_resolves_flatpak_sync_to_flatpak_sync_job"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_flatpak_sync.py::TestCoordinatorIntegration::test_accepted_outcome_contains_only_flatpak_prefixed_item_ids"
        status: pass
      - kind: other
        ref: "grep -c 'review_items' src/pcswitcher/jobs/flatpak_sync.py"
        status: pass
    human_judgment: false
  - id: D10
    description: "On the reference machine, a dry-run reports the user/system scope split as found rather than proposing to normalise it, and a genuinely diverged flatpak state shows every applicable diff class with removals separately grouped"
    verification: []
    human_judgment: true
    rationale: "The plan's own <verification> section names this as a live-machine dry-run check; this autonomous run has no access to the reference machine's real flatpak state. Every mocked-executor behavior bullet is unit-covered above (D1-D9), matching the precedent set by plans 02-03/02-05/02-06/02-07/02-08 for their own live-machine proofs — deferred to plan 02-13's end-to-end suite."

duration: 27min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 09: Flatpak Scoped Ref/Remote Convergence Summary

**`FlatpakSyncJob` converges installed flatpak refs and remotes per user/system scope via `flatpak install/uninstall --user|--system` and `flatpak remote-add/remote-delete`, folding scope into item identity so the same app in two scopes yields an install plus a removal rather than a change, provisioning remotes before the refs that need them (D-14), and exporting `~/.local/share/flatpak` for folder_sync while leaving `~/.var/app` alone (D-29).**

## Performance

- **Duration:** 27 min
- **Started:** 2026-07-23T10:44:00Z (approx.)
- **Completed:** 2026-07-23T11:11:31Z
- **Tasks:** 1
- **Files modified:** 2 (both created)

## Accomplishments

- `flatpak_sync.py`: `FlatpakSyncJob(PackageSyncJob)` with tab-separated `flatpak list --app --columns=application,version,origin,installation` and per-scope `flatpak remotes --columns=name,url` parsing (no header row — the `--columns` flag itself names the columns), a flatpak-specific `plan()` override reusing `DecisionFile`/`filter_inert` (D-08) and `_build_review_groups` (D-24) from the shared core, and `converge()` issuing exactly the D-06/D-14-safe verbs: `flatpak remote-add --if-not-exists`/`remote-delete` for remotes, `flatpak install -y`/`uninstall -y` for refs, every command prefixed with `sudo` if and only if the item's own scope is `system`.
- Scope folded into `item_id` (`flatpak:ref:<scope>:<application>`, `flatpak:remote:<scope>:<name>`, both defined in `package_items.py` by plan 02-05) makes "same app, different scope on each machine" fall out of the generic source-vs-target diff as an independent install-side entry and an independent removal-side entry with zero special-casing in this module.
- A converge-time guard (`_remote_ready_on_target`) refuses a ref install whose origin remote is neither already on the target nor among this run's own successfully-added remotes, raising `ConvergeItemFailed` naming the remote rather than issuing an install flatpak will reject (T-02-24) — caught and collected by the base `apply()` loop (D-27), never stopping the batch.
- `flatpak_sync_exclude_paths()`: returns `~/.local/share/flatpak` only, resolved against `Path.home()` at call time.
- `validate()`: `flatpak --version` on both ends (a missing binary is a clean `ValidationError`, never an exception — flatpak ships in no default Ubuntu 24.04 install), and `sudo -n true` on the target only when `_system_scope_in_play()` finds a system-scope ref or remote on either machine.
- 21 new unit tests in `tests/unit/jobs/test_flatpak_sync.py` covering column-flag-driven capture, the full diff taxonomy (install/report_only/remove for refs, install for remotes, scope-split producing install+removal not change), remote-before-ref ordering in both `plan.diffs` and actual `converge()` call order, the sudo-per-scope rule, the missing-origin-remote skip, `plan()`'s read-only property, `flatpak_sync_exclude_paths()`, `validate()`'s conditional-sudo behavior, job discovery, and a `PackagePhaseCoordinator` integration test proving `FlatpakSyncJob`'s accepted outcome carries only `flatpak:`-prefixed item ids.

## Task Commits

1. **Task 1: FlatpakSyncJob — scoped capture, remote provisioning and convergence** - `66fd26f` (feat)

**Plan metadata:** (this commit)

## Files Created/Modified

- `src/pcswitcher/jobs/flatpak_sync.py` - `FlatpakSyncJob`, `flatpak_sync_exclude_paths`, tab-separated `flatpak list`/`flatpak remotes` parsers, flatpak-specific `plan()`/diff/`converge()`
- `tests/unit/jobs/test_flatpak_sync.py` - 21 tests covering every behavior bullet in the plan

## Decisions Made

See `key-decisions` in frontmatter. In order of consequence:

1. **`plan()` is overridden, not inherited**, for the same structural reason 02-08's `SnapSyncJob` documented: the base `PackageSyncJob.plan()` routes through `diff_items()`/`_diff_apt_packages()`, which has no notion of a second item class (`FLATPAK_REMOTE`) that must converge before the first (`FLATPAK_REF`). `FlatpakSyncJob.plan()` reimplements capture -> `DecisionFile`/`filter_inert` -> local diff (two item classes, remotes-then-refs) -> `_build_review_groups`, reusing every manager-agnostic building block.
2. **`sudo` is applied per-scope, not per-verb** — one `_sudo_prefix(scope)` helper used identically by remote-add, remote-delete, ref-install and ref-uninstall, so the plan's `--system needs sudo, --user does not` rule (stated once, in the install-ref bullet) cannot silently apply to only some of the four converge verbs.
3. **`validate()`'s sudo check is genuinely conditional**, re-probing `flatpak list`/`flatpak remotes --system` rather than reusing any state from a prior `plan()` call (the two phases don't share cached state across a job instance's lifecycle) — the cost is a handful of extra read-only commands during `validate()` when a system-scope item exists, in exchange for the ASVS V4 property that a user-scope-only sync never has to ask for root at all.
4. **Remote URL drift (same name+scope, different URL) is not diffed.** Neither the plan's behavior bullets nor D-11 ask for it, and `flatpak remote-add --if-not-exists` is a no-op against an existing name regardless of URL — there is no converge verb this plan defines that would act on such a diff, so adding one would be speculative.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - blocking type/runtime issue] `plan()` overridden instead of inherited unchanged**
- **Found during:** Task 1, implementing `capture_source_items`/`query_target_items` per the plan's literal instruction to "implement only the abstract hooks and inherit `plan()`... unchanged"
- **Issue:** The inherited `plan()` calls `PackageSyncJob.diff_items()`, whose only dispatch (`_diff_apt_packages`) is single-item-class-shaped with no ordering concept between two classes. Flatpak needs exactly that: `FLATPAK_REMOTE` diffs must converge before `FLATPAK_REF` diffs in the same run (D-14). Calling the inherited `plan()` would either crash on `FlatpakItem`'s field shape (no `.version`-only field, extra `origin`/`scope`) or, if patched to tolerate it, still have no way to express the remote-before-ref ordering the base single-class dispatch was never designed for. This is the identical class of gap 02-08's `SnapSyncJob` deviation already documented for a related-but-distinct reason.
- **Fix:** `FlatpakSyncJob.plan()` overrides the base, reimplementing capture -> `DecisionFile`/`filter_inert` -> local two-item-class diff (remotes then refs) -> `_build_review_groups`, matching the override pattern `SnapSyncJob.plan()` and (partially) `AptSyncJob.plan()` already establish.
- **Files modified:** `src/pcswitcher/jobs/flatpak_sync.py` (this plan's own file — no shared-core file touched)
- **Verification:** `uv run pytest tests/unit/jobs/test_flatpak_sync.py -x` (21/21 pass), `uv run pytest` (915/915 pass), `uv run basedpyright` (0 errors)
- **Committed in:** `66fd26f`

**2. [Rule 3 - blocking type issue] `# pyright: ignore[reportIncompatibleMethodOverride]` on the two abstract hooks**
- **Found during:** Task 1, immediately after deciding `capture_source_items`/`query_target_items` must return `Sequence[FlatpakItem]`
- **Issue:** `basedpyright --strict` rejects a covariant-return override where the override's return type isn't a subtype of the base's declared `Sequence[AptPackageItem]`; `FlatpakItem` and `AptPackageItem` are unrelated dataclasses. Identical to 02-08's `SnapSyncJob` finding.
- **Fix:** One-line `# pyright: ignore[reportIncompatibleMethodOverride]` per hook, each with an inline comment stating why it's safe: `FlatpakSyncJob` never routes through the base `plan()` that would call these hooks polymorphically expecting an `AptPackageItem` back.
- **Files modified:** `src/pcswitcher/jobs/flatpak_sync.py`
- **Verification:** `uv run basedpyright` clean (0 errors) both for this file and the full project.
- **Committed in:** `66fd26f`

**3. [Rule 1 - bug] Self-referential `grep -c 'review_items'` mention in the module docstring broke its own acceptance criterion**
- **Found during:** Task 1, running the plan's own `grep -c 'review_items' src/pcswitcher/jobs/flatpak_sync.py` acceptance check
- **Issue:** A first draft of the module docstring explained the "no review of its own" property by literally quoting the grep command (`` `grep -c 'review_items'` on this file is 0 ``) — that quoted string is itself a match, making the check report 1 instead of 0.
- **Fix:** Reworded the sentence to state the same fact ("this module never calls that reviewing function directly") without embedding the string `review_items`.
- **Files modified:** `src/pcswitcher/jobs/flatpak_sync.py` (docstring only, no behavior change)
- **Verification:** `grep -c 'review_items' src/pcswitcher/jobs/flatpak_sync.py` now returns `0`; full suite re-run clean.
- **Committed in:** `66fd26f`

---

**Total deviations:** 3 auto-fixed (2 Rule 3 matching 02-08's precedent exactly, 1 Rule 1 self-inflicted docstring bug caught by the plan's own acceptance grep before commit)
**Impact on plan:** No scope creep. All three deviations are contained entirely within `flatpak_sync.py`; `package_items.py` is untouched (`git diff --stat` confirms zero changes), keeping this plan's own acceptance criterion intact.

## Issues Encountered

None beyond the deviations documented above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

`FlatpakSyncJob` is a complete third-manager job on the proven `PackageSyncJob` core: capture, diff (two ordered item classes), converge and validate are all implemented and unit-tested, and `flatpak_sync_exclude_paths()` is ready for plan 02-10 to wire into `folder_sync`'s config-schema-driven exclusions. `AptSyncJob`, `SnapSyncJob` and `FlatpakSyncJob` now each demonstrate a distinct shape of "why the base `plan()` doesn't fit" (apt still calls `super().plan()` and extends it; snap and flatpak both override entirely, snap for a converge-vs-report-only mismatch, flatpak for a two-ordered-item-class need) — all three coexist under one `PackagePhaseCoordinator` review (D-24) with no per-job self-review.

The plan's own `<verification>` section names one live-machine check (a real dry-run against the reference machine's actual scope split and a genuinely diverged flatpak state) that this autonomous run has no access to perform — deferred to plan 02-13's end-to-end suite, the same precedent plans 02-03/02-05/02-06/02-07/02-08 set for their own live-machine proofs.

`.planning/REQUIREMENTS.md` was left untouched per this plan's orchestrator directive — three plans remain in Phase 2 before requirement completion is marked.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: src/pcswitcher/jobs/flatpak_sync.py
- FOUND: tests/unit/jobs/test_flatpak_sync.py
- FOUND: commit 66fd26f
