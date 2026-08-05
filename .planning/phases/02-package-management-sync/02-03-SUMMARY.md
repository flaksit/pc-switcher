---
phase: 02-package-management-sync
plan: 03
subsystem: infra
tags: [apt, package-sync, tdd, coordinator, dry-run, adr-020]

requires:
  - phase: 02-package-management-sync
    provides: "ADR-020 (convergence model + coordinator design, plan 02-01), review_items/ReviewGroup/ReviewOutcome/Decision (package_review.py, plan 02-02)"
provides:
  - "The item model (ItemClass, DiffClass, DiffAction, AptPackageItem, ItemDiff)"
  - "PackageSyncJob: the shared plan()/accept_review()/apply()/execute() pipeline every future package job subclasses"
  - "AptSyncJob: installs apt packages missing on the target, with an apt-get -s transaction guard, dry-run, continue-on-failure, and validate()"
  - "PackagePhaseCoordinator: one batched review across every enabled package job before any of them applies"
  - "JobContext.enabled_sync_jobs"
  - "apt_sync registered in config-schema.yaml/default-config.yaml, ordered before folder_sync (D-17)"
affects: [02-04, 02-05, 02-06, 02-07, 02-08, 02-09, 02-10, 02-11, 02-12, 02-13]

tech-stack:
  added: []
  patterns:
    - "Two-phase SyncJob convergence (plan() capture+diff+review-build, apply() converge), designed in ADR-020/02-01, now actually implemented and load-bearing — every future package job (snap_sync, flatpak_sync) subclasses PackageSyncJob and inherits this split rather than reimplementing it."
    - "PackagePhaseCoordinator: plan every enabled job -> one review -> distribute decisions by item-id membership, run once by the orchestrator before its per-job execute() loop."

key-files:
  created:
    - src/pcswitcher/jobs/package_items.py
    - src/pcswitcher/jobs/package_sync_core.py
    - src/pcswitcher/jobs/apt_sync.py
    - src/pcswitcher/jobs/package_phase.py
    - tests/unit/jobs/test_apt_sync.py
    - tests/unit/jobs/test_package_phase.py
  modified:
    - src/pcswitcher/jobs/context.py
    - src/pcswitcher/orchestrator.py
    - src/pcswitcher/schemas/config-schema.yaml
    - src/pcswitcher/default-config.yaml

key-decisions:
  - "PackageSyncJob's manager ClassVar is named `manager_id`, not `manager_name` as the plan's prose describes. `manager_name: ClassVar[str]` contains the literal substring `name: ClassVar` (manager_NAME: ClassVar), so the plan's own acceptance criterion `grep -n 'name: ClassVar' package_sync_core.py returns nothing` would fail with that name. Renamed to `manager_id` everywhere (package_sync_core.py, apt_sync.py, package_phase.py) to satisfy the literal, mechanically-verifiable criterion rather than the descriptive prose name."
  - "converge() failures use two signaling paths, both caught by apply(): a new ConvergeItemFailed exception for a converge step that refuses to even attempt the command (the apt transaction guard), and a non-zero-exit CommandResult for a command that ran and failed. Both are collected into apply()'s failures list identically."
  - "PackageSyncJob.record_plan_failure()/the `_plan_failure` field is the mechanism that lets a job whose plan() raised still surface that failure through its own execute() (and therefore its own JobResult), even though the coordinator has already moved on to plan the other enabled managers."
  - "Discovered fact (verified, not fixed): SessionStatus/CLI exit code are derived purely from whether an exception propagated out of orchestrator.run(), never from job_results content — see WINDOWS.md entry #1 and 'Deviations' below."

patterns-established:
  - "diff_items()/_build_review_groups() live on PackageSyncJob as concrete (non-abstract) methods, structured as per-item-class dispatch, so snap_sync/flatpak_sync reuse the diff->group pipeline and only implement capture/query/converge."

requirements-completed: []

coverage:
  - id: D1
    description: "apt-mark showmanual + one batched dpkg-query call capture the source's manually-installed packages with versions (D-03); apt list --installed is never used"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestCapture::test_capture_source_items_returns_three_items_with_versions"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestCapture::test_dpkg_query_used_not_apt_list_installed"
        status: pass
    human_judgment: false
  - id: D2
    description: "Diffing the source's manifest against the target's own query yields MISSING_ON_TARGET items only for names absent on the target; a target-only name produces nothing (this slice)"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestDiff::test_diff_yields_exactly_two_missing_items"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestDiff::test_diff_is_symmetric_blind_extra_on_target_produces_no_item"
        status: pass
    human_judgment: false
  - id: D3
    description: "plan() issues only read commands; execute() refuses to run and names PackagePhaseCoordinator when no plan has been accepted"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestPlanApplySplit::test_plan_issues_no_mutating_command"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestPlanApplySplit::test_execute_without_accepted_plan_raises_naming_coordinator"
        status: pass
    human_judgment: false
  - id: D4
    description: "Only APPLY-decided items reach the target; SKIP_ONCE items reach no command; dry-run produces the plan/review but issues no mutating command"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestConverge::test_only_apply_decision_installs_skip_once_never_sent"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestDryRun::test_dry_run_issues_no_mutating_command"
        status: pass
    human_judgment: false
  - id: D5
    description: "A failing item does not stop the job: all approved items are attempted, the failure's stderr is collected, and PackageItemFailures is raised only after the loop completes (D-27)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestContinueOnFailure::test_second_of_three_fails_all_attempted_one_failure_raised"
        status: pass
    human_judgment: false
  - id: D6
    description: "Every approved install is simulated with apt-get -s first; a simulation that would remove an unreviewed package refuses the real install and reports a per-item failure naming the removed package (T-02-32)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestTransactionGuard::test_collateral_removal_refuses_install_and_names_the_package"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestTransactionGuard::test_clean_simulation_proceeds_to_real_install"
        status: pass
    human_judgment: false
  - id: D7
    description: "validate() detects apt-mark unavailability and dpkg frontend lock contention as distinct ValidationErrors"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestValidate::test_apt_mark_unavailable_yields_validation_error"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestValidate::test_dpkg_lock_held_yields_distinct_validation_error"
        status: pass
    human_judgment: false
  - id: D8
    description: "AptSyncJob is discoverable from config: Orchestrator._resolve_sync_job_class('apt_sync') resolves to AptSyncJob"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_apt_sync.py::TestJobDiscovery::test_orchestrator_resolves_apt_sync_to_apt_sync_job"
        status: pass
    human_judgment: false
  - id: D9
    description: "PackagePhaseCoordinator plans every enabled job before calling review_items exactly once, and no job's accept_review runs until review_items returns"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestPlanBeforeReview::test_both_jobs_plan_before_review_which_runs_once_then_accept_review"
        status: pass
    human_judgment: false
  - id: D10
    description: "Merged review groups preserve manager order (supplied job order) and each plan's own fixed action order (install before remove, in this test)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestMergedGroupOrder::test_manager_order_follows_supplied_job_order"
        status: pass
    human_judgment: false
  - id: D11
    description: "Each job's accepted outcome contains only item ids from its own plan"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestDecisionDistribution::test_each_job_receives_only_its_own_item_ids"
        status: pass
    human_judgment: false
  - id: D12
    description: "A job whose plan() raises does not block the other job's plan/review/accept_review; the failing job's own execute() re-raises the stored failure"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestPlanFailureIsolation::test_one_jobs_plan_failure_does_not_block_the_other"
        status: pass
    human_judgment: false
  - id: D13
    description: "With no enabled package jobs the coordinator returns without constructing a prompt or calling review_items; with dry_run=True it still plans and renders the review"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestEmptyJobList::test_no_jobs_returns_without_prompting"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestDryRunStillReviews::test_dry_run_context_still_plans_and_reviews"
        status: pass
    human_judgment: false
  - id: D14
    description: "JobContext.enabled_sync_jobs defaults to None without raising for lightweight test contexts, and can be populated with the full enablement map"
    requirement: REQ-sync-scope-packages
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestJobContextEnabledSyncJobs::test_defaults_to_none_and_does_not_raise"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestJobContextEnabledSyncJobs::test_can_be_populated_with_the_full_enablement_map"
        status: pass
    human_judgment: false
  - id: D15
    description: "A PackageItemFailures from one package job records a FAILED JobResult and still lets the remaining jobs run; every other exception type keeps today's abort-the-run behavior (regression guard)"
    requirement: REQ-conflict-detection-no-resolution
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestOrchestratorPackageItemFailuresContinuation::test_failing_package_job_does_not_cancel_remaining_jobs"
        status: pass
      - kind: unit
        ref: "tests/unit/jobs/test_package_phase.py::TestOrchestratorPackageItemFailuresContinuation::test_other_exception_types_still_abort_the_run"
        status: pass
    human_judgment: false
  - id: D16
    description: "A human confirms `pc-switcher sync <target> --dry-run` with apt_sync: true prints the review and leaves the target's apt-mark showmanual unchanged, against real pc1/pc2 VMs"
    verification: []
    human_judgment: true
    rationale: "The plan's own <verification> section defers the VM-level end-to-end proof of this tracer path to plan 02-13, which runs against the real two-VM test infrastructure. This autonomous run has no VM access; unit tests cover every mocked-executor behavior bullet, but real apt/dpkg/sudo behavior on a live machine is unverified here by design."

duration: 26min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 03: AptSyncJob Tracer + PackagePhaseCoordinator Summary

**One apt package missing on the target now travels source capture -> target query -> diff -> coordinator-batched review -> `apt-get install` with an `apt-get -s` transaction guard, dry-run, per-item continue-on-failure, and the `PackageSyncJob` plan()/apply() split every future package job (snap_sync, flatpak_sync) will subclass.**

## Performance

- **Duration:** 26 min
- **Started:** 2026-07-23T05:57:39Z
- **Completed:** 2026-07-23T06:24:16Z
- **Tasks:** 2
- **Files modified:** 10 (6 created, 4 modified)

## Accomplishments

- `package_items.py`: the D-02 item model (`ItemClass`, `DiffClass`, `DiffAction` with their full future-proof member sets; `AptPackageItem` and `ItemDiff` implemented for this slice).
- `package_sync_core.py`: `PackageSyncJob`'s `plan()`/`accept_review()`/`apply()`/`execute()` split — the structural fix for ADR-020's core defect (per-job self-contained review letting one manager mutate the target before another had diffed). `execute()` refuses to run without a coordinator-accepted plan. Carries no `name` ClassVar so `_resolve_sync_job_class` never mistakes it for a registered job.
- `apt_sync.py`: `AptSyncJob` — `apt-mark showmanual` + one batched `dpkg-query` call for capture, an `apt-get -s` transaction-simulation guard that refuses an install whose simulated transaction would remove an unreviewed package (`ConvergeItemFailed`), `validate()` covering apt-mark availability, sudo, and dpkg frontend lock contention, and `describe_first_sync_scope()`.
- `package_phase.py`: `PackagePhaseCoordinator` — plans every enabled package job (isolating one job's `plan()` failure from the rest), merges their review groups in supplied job order, calls `review_items()` exactly once, and distributes each job back only its own slice of the outcome by item-id membership.
- `orchestrator.py`: the coordinator runs from `_execute_jobs` before the per-job `TaskGroup` loop; a new `PackageItemFailures` branch in the per-job except chain records a FAILED `JobResult` without re-raising, so one package manager's item failures no longer cancel another manager's already-approved work.
- `context.py`: `JobContext.enabled_sync_jobs`, the full `sync_jobs` enablement map, optional with a `None` default so existing lightweight test contexts keep constructing.
- `apt_sync` registered in `config-schema.yaml`/`default-config.yaml`, positioned above `folder_sync` (D-17), default `false` (opt-in, destructive).
- 26 new unit tests (16 in `test_apt_sync.py`, 10 in `test_package_phase.py`), including one exercising the real `Orchestrator._execute_jobs`/`_run_jobs_in_task_group` path end-to-end with mocked executors.

## Task Commits

1. **Task 1: End-to-end "install a missing apt package on the target" — one path only** - `1cfb86e` (feat)
2. **Task 2: The package-phase coordinator — one review across every enabled manager** - `0c4688b` (feat)
   - Follow-up regression test added in `7fb7718` (test) — a companion test proving non-`PackageItemFailures` exceptions still abort the run, guarding the except-chain change task 2 made.

**Plan metadata:** (this commit)

## Files Created/Modified

- `src/pcswitcher/jobs/package_items.py` - `ItemClass`, `DiffClass`, `DiffAction`, `AptPackageItem`, `ItemDiff`
- `src/pcswitcher/jobs/package_sync_core.py` - `PackageSyncJob`, `PackagePlan`, `PackageItemFailures`, `ConvergeItemFailed`
- `src/pcswitcher/jobs/apt_sync.py` - `AptSyncJob`, `AptTransactionPreview`, `simulate_apt_transaction`
- `src/pcswitcher/jobs/package_phase.py` - `PackagePhaseCoordinator`, `coordinate_package_review`
- `src/pcswitcher/jobs/context.py` - `JobContext.enabled_sync_jobs` field
- `src/pcswitcher/orchestrator.py` - coordinator wiring in `_execute_jobs`, `PackageItemFailures` except branch, `enabled_sync_jobs` population
- `src/pcswitcher/schemas/config-schema.yaml` - `sync_jobs.apt_sync` + `apt_sync:` job-config section
- `src/pcswitcher/default-config.yaml` - `sync_jobs.apt_sync: false` (above `folder_sync`) + `apt_sync: {}` section
- `tests/unit/jobs/test_apt_sync.py` - 16 tests covering capture, diff, plan/apply split, converge, dry-run, continue-on-failure, transaction guard, validate, job discovery
- `tests/unit/jobs/test_package_phase.py` - 10 tests covering the coordinator's plan/review/distribute contract, `enabled_sync_jobs`, and the orchestrator's `PackageItemFailures` continuation (plus a regression guard for every other exception type)

## Decisions Made

- **`manager_name` renamed to `manager_id`.** The plan's task 1 action text names the shared `PackageSyncJob` ClassVar `manager_name`, but the plan's own acceptance criterion is `grep -n 'name: ClassVar' src/pcswitcher/jobs/package_sync_core.py` returning nothing — and `manager_name: ClassVar[str]` contains that exact substring (`manager_NAME: ClassVar`). The two requirements from the same plan directly contradicted each other; renamed the ClassVar to `manager_id` (which contains no `name` substring) throughout `package_sync_core.py`, `apt_sync.py`, and `package_phase.py` to satisfy the literal, mechanically-verifiable criterion.
- **`ConvergeItemFailed` as a distinct per-item signal.** `PackageSyncJob.apply()` needed to treat two different failure shapes identically (a converge step that refuses to even run a command, vs. one that runs and exits non-zero). Introduced `ConvergeItemFailed` (raised by `converge()`) alongside checking the returned `CommandResult.success`, both funneled into the same per-item failure collection.
- **`record_plan_failure()`/`_plan_failure`.** Per the plan's own suggested resolution ("add that small method to `PackageSyncJob`... or store the failure on the coordinator"), chose the job-side method so `execute()`'s "raise the stored failure" logic stays in one place (the base class), and the coordinator only needs to call it.
- **Session-status/exit-code gap left unfixed, recorded instead.** Verified this session: `Orchestrator.run()` sets `session.status = SessionStatus.COMPLETED` unconditionally once `_execute_jobs` returns without raising (no check of `job_results` contents), and the CLI's `_run_sync`/`_async_run_sync` return exit code 0 whenever `orchestrator.run()` doesn't raise. Before this plan, every `FAILED` `JobResult` was necessarily accompanied by a propagated exception (the only path that produced one), so this was never observable. Task 2's `PackageItemFailures` branch is the first case where a `FAILED` `JobResult` can exist without an exception propagating — meaning a sync with failed package items can now exit 0. Fixing this needs a change to `cli.py`'s exit-code logic and/or `orchestrator.py`'s `SessionStatus` computation, which is out of the plan's explicit "two narrow changes" scope for `orchestrator.py` and touches a file (`cli.py`) not in this plan's `files_modified` list. Recorded as `.planning/WINDOWS.md` entry #1 (kind: deviation) rather than silently expanded scope.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — naming conflict between two of the plan's own requirements] Renamed `manager_name` to `manager_id`**
- **Found during:** Task 1, immediately after writing `package_sync_core.py`
- **Issue:** The plan's action text specifies a `manager_name: ClassVar[str]`, but the plan's acceptance criteria require `grep -n 'name: ClassVar' src/pcswitcher/jobs/package_sync_core.py` to return nothing. `manager_name: ClassVar[str]` literally contains the substring `name: ClassVar`, so the two requirements from the same plan could not both hold with the prose-specified name.
- **Fix:** Renamed the ClassVar (and every reference to it) from `manager_name` to `manager_id`, which contains no `name` substring, satisfying the mechanically-verifiable grep while preserving the ClassVar's purpose and every other spec detail.
- **Files modified:** `src/pcswitcher/jobs/package_sync_core.py`, `src/pcswitcher/jobs/apt_sync.py`, `src/pcswitcher/jobs/package_phase.py`
- **Verification:** `grep -n 'name: ClassVar' src/pcswitcher/jobs/package_sync_core.py` returns nothing; full test suite green.
- **Committed in:** `1cfb86e` (task 1), `0c4688b` (task 2's `package_phase.py` usage)

---

**Total deviations:** 1 auto-fixed (naming conflict between two acceptance criteria in the same plan)
**Impact on plan:** Cosmetic rename only — no behavior, API surface, or test coverage was changed. No scope creep.

## Issues Encountered

None beyond the naming conflict documented above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

`PackageSyncJob`'s `plan()`/`accept_review()`/`apply()`/`execute()` split and `PackagePhaseCoordinator`'s plan-review-distribute contract are the load-bearing shapes every remaining Phase 2 plan builds on: `snap_sync`/`flatpak_sync` (later plans) subclass `PackageSyncJob` directly; `diff_items()`/`_build_review_groups()` are structured for per-item-class dispatch so adding removal/change directions and new item classes (apt sources, keys, pins, config) extends rather than reshapes this module. Plan 02-13's VM-level end-to-end proof of this exact tracer path (real `pc-switcher sync <target> --dry-run` against pc1/pc2) is still pending — this plan's own `<verification>` section defers it there by design.

One correctness gap was discovered and recorded rather than fixed (see Decisions Made and `.planning/WINDOWS.md` entry #1): `SessionStatus`/CLI exit code do not yet reflect `job_results` content, so a sync with failed package items can exit 0. This should be resolved before `apt_sync`/`snap_sync`/`flatpak_sync` ship enabled by default.

---
*Phase: 02-package-management-sync*
*Completed: 2026-07-23*

## Self-Check: PASSED

- FOUND: src/pcswitcher/jobs/package_items.py
- FOUND: src/pcswitcher/jobs/package_sync_core.py
- FOUND: src/pcswitcher/jobs/apt_sync.py
- FOUND: src/pcswitcher/jobs/package_phase.py
- FOUND: tests/unit/jobs/test_apt_sync.py
- FOUND: tests/unit/jobs/test_package_phase.py
- FOUND: commit 1cfb86e
- FOUND: commit 0c4688b
- FOUND: commit 7fb7718
