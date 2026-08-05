---
phase: 02-package-management-sync
plan: 18
subsystem: package-sync
tags: [snippet-registry, config-sync, send_file, sync-jobs, D-23, python]

# Dependency graph
requires:
  - phase: 02-package-management-sync
    provides: manual_installs_sync (fourth package job, snippet registry owner), SnippetRegistry, config_sync single/multi-file transfer, PackageSyncJob plan/review/apply pipeline (02-15/02-17)
provides:
  - manual_installs_sync pushes package-snippets.yaml to the target itself, after its own review and before any replay (D-23)
  - PackageSyncJob.after_review() overridable no-op hook (seam between accept_review and apply)
  - config_sync reverted to config.yaml only; SYNCED_CONFIG_FILENAMES removed
affects: [package-sync, config-sync, verify-work]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "A job owns its own cross-machine transport (send_file) rather than depending on config_sync/folder_sync running — no job's correctness depends on another (D-23)"
    - "Idempotent-once hook: after_review() triggers _finalize_unreproducible before pushing; apply()'s later call is guarded to a no-op so timestamps stamp once"

key-files:
  created: []
  modified:
    - src/pcswitcher/jobs/package_sync_core.py
    - src/pcswitcher/jobs/manual_installs_sync.py
    - src/pcswitcher/config_sync.py
    - src/pcswitcher/jobs/package_state.py
    - tests/unit/jobs/test_manual_installs_sync.py
    - tests/unit/jobs/test_package_sync_core.py
    - tests/unit/cli/test_config_sync.py
    - tests/unit/jobs/test_package_state.py
    - tests/integration/test_config_sync.py

key-decisions:
  - "after_review() lives on the base as a no-op so execute() stays the single source of the plan/review/apply order; only manual_installs_sync overrides it"
  - "Finalize-then-push via an idempotency guard inside ManualInstallsSyncJob: after_review finalizes then pushes; apply's finalize is a no-op the second time, keeping source and pushed target registries byte-identical"
  - "config_sync reverted to the proven pre-multi-file (f045218) single-file shape rather than a hand-simplified loop removal — smallest, already-tested surface"

requirements-completed: [REQ-sync-scope-packages, REQ-conflict-detection-no-resolution]

coverage:
  - id: D1
    description: "manual_installs_sync pushes the source package-snippets.yaml to the target's ~/.config/pc-switcher/ via send_file after its review and before any converge replay"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestSnippetPush::test_push_runs_after_review_and_before_replay_in_execute,test_push_sends_source_registry_under_the_user_home_never_etc"
        status: pass
    human_judgment: false
  - id: D2
    description: "The push includes a snippet authored on the fly: finalize persists it to the source registry before the push sends that file"
    requirement: "REQ-conflict-detection-no-resolution"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestSnippetPush::test_snippet_authored_in_review_is_persisted_before_the_push"
        status: pass
    human_judgment: false
  - id: D3
    description: "Absent source registry and dry-run push nothing; the push reads no other job's state (T-02-47/T-02-48)"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_manual_installs_sync.py#TestSnippetPush::test_absent_source_registry_makes_push_a_noop,test_dry_run_pushes_nothing"
        status: pass
    human_judgment: false
  - id: D4
    description: "execute() calls after_review between accept_review and apply"
    verification:
      - kind: unit
        ref: "tests/unit/jobs/test_package_sync_core.py#TestExecuteSelfContained::test_call_order_is_plan_review_accept_review_apply"
        status: pass
    human_judgment: false
  - id: D5
    description: "config_sync carries exactly config.yaml (caller's path, whatever its name); SYNCED_CONFIG_FILENAMES removed and never reintroduced (T-02-49)"
    requirement: "REQ-sync-scope-packages"
    verification:
      - kind: unit
        ref: "tests/unit/cli/test_config_sync.py#TestCopyConfigToTarget::test_copies_the_caller_supplied_file_even_when_not_named_config_yaml"
        status: pass
    human_judgment: false

# Metrics
duration: 40min
completed: 2026-07-23
status: complete
---

# Phase 2 Plan 18: Snippet registry push off config_sync onto manual_installs_sync Summary

**The install-snippet registry now travels source-to-target by `manual_installs_sync`'s own post-review `send_file` push (D-23), so an on-the-fly snippet reaches the target the same run; `config_sync` is reverted to carrying `config.yaml` only, with both halves in one plan so no snippet-delivery gap exists.**

## Performance

- **Duration:** ~40 min
- **Completed:** 2026-07-23
- **Tasks:** 2 (tracer + auto, both tdd)
- **Files modified:** 9 (0 created)

## Accomplishments

- Added an overridable no-op `after_review()` hook to `PackageSyncJob`, called from `execute()` between `accept_review()` and `apply()` — the seam for work that must run after the review but before any mutation. The base keeps the single plan/review/apply skeleton.
- `ManualInstallsSyncJob.after_review()` finalizes (persists this run's authored snippets to the SOURCE registry) then pushes `package-snippets.yaml` to the target's `~/.config/pc-switcher/` via `send_file`, mirroring `config_sync._copy_config_to_target`'s `mkdir -p` → `echo $HOME` → `send_file` shape. The push is a no-op with no source registry and under dry-run, and reads no other job's state.
- Guarded `_finalize_unreproducible` to run once per run (`self._unreproducible_finalized`): `after_review()` triggers it before the push, so `apply()`'s later call is a no-op and each snippet's `authored_at` is stamped once — source and pushed target registries stay byte-identical.
- Reverted `config_sync` to single-file operation: removed `SYNCED_CONFIG_FILENAMES` (and its `__all__` entry), the multi-file loop, and the per-file `filename` parameter on every helper. `sync_config_to_target` now transfers exactly the caller-supplied `source_config_path`, whatever its name.
- Corrected `package_state.py` docstrings that claimed the registry travels via `config_sync.SYNCED_CONFIG_FILENAMES` / the next sync: they now name `manual_installs_sync`'s own push and "this same run".

## Task Commits

Each task committed atomically (both tdd; test + implementation committed together per task):

1. **Task 1 (tracer): manual_installs_sync pushes the snippet registry after its own review** — `54d7f8f` (feat)
2. **Task 2 (auto): revert config_sync to config.yaml only** — `e26ea49` (feat)

_Tracer feedback gate: after Task 1 the tracer `<verify>` (pytest on the two test files + basedpyright on both source modules) was re-run end-to-end and passed (54 passed, 0 pyright issues) before expanding to Task 2._

## Files Modified

- `src/pcswitcher/jobs/package_sync_core.py` — `after_review()` no-op hook; `execute()` calls it between accept_review and apply; docstring updated.
- `src/pcswitcher/jobs/manual_installs_sync.py` — `after_review()` override (finalize-then-push), `_push_snippet_registry()`, idempotency guard + corrected docstring on `_finalize_unreproducible`, new imports (`Path`, `CONFIG_REMOTE_DIR`, `SNIPPET_REGISTRY_RELPATH`, `JobContext`).
- `src/pcswitcher/config_sync.py` — reverted to single-file; `SYNCED_CONFIG_FILENAMES` and `_remote_path` gone; helpers no longer take a filename; module comment explains the registry moved to manual_installs_sync's push.
- `src/pcswitcher/jobs/package_state.py` — three docstrings corrected (SnippetRegistry travels by the job's push, not config_sync).
- Tests: new `TestSnippetPush` (push order, home-scoped destination, absent/dry-run no-ops, finalize-then-push) in `test_manual_installs_sync.py`; call-order test extended to assert `after_review` in `test_package_sync_core.py`; `test_config_sync.py` reverted to single-file with a non-`config.yaml` transfer test (hard constraint 5); `test_package_state.py` single-file signature fix; `test_config_sync.py` (integration) signature fix.

## Decisions Made

- `after_review()` on the base is a no-op; only `manual_installs_sync` uses it — keeps `execute()` the one place the plan/review/apply order is defined.
- Finalize-then-push is enforced by an idempotency guard local to `ManualInstallsSyncJob`, not by moving `_finalize_unreproducible` out of `apply()`. This keeps `apply()`'s contract intact for the direct-`apply()` tests (`_FakeManualJob`, `TestSkipOnceResolution`) and avoids double-stamping timestamps.
- `config_sync` was reverted to the proven pre-multi-file shape (commit `f045218`) rather than hand-editing the loop out — the smallest, already-tested single-file surface. That version already reads the caller's own path, so the 02-07 caller-path bug does not reappear.

## Deviations from Plan

**1. [Rule 3 - Blocking] Mechanical signature fix to `tests/integration/test_config_sync.py`**
- **Found during:** Task 2 (running the `basedpyright` half of the verify gate).
- **Issue:** The integration test calls `_get_target_config(pc1_executor, "config.yaml")` and `_copy_config_to_target(pc1_executor, "config.yaml", local_path)` at four sites — the removed multi-file signatures — so `basedpyright` reported 4 `reportCallIssue` errors. Task 2's verify (`... && uv run basedpyright`) and acceptance criteria require a clean type gate.
- **Fix:** Removed the `"config.yaml"` positional argument at the four call sites. This integration test asserts only single-file `config.yaml` behavior (no `package-snippets.yaml` transfer assertions), so no behavioral assertion changed — the mechanical revert is exactly what the reverted single-file code expects.
- **Files modified:** `tests/integration/test_config_sync.py` (not in this plan's `files_modified`).
- **Note for 02-21:** the plan deferred integration-test changes to 02-21 for genuine multi-file behavior; this file had none, so 02-21 need not revisit these four call sites.
- **Committed in:** `e26ea49`

## Issues Encountered

None. The pre-existing orchestrator D-17 guard limitation noted in 02-17 is unchanged and out of scope here (orchestrator.py is not in this plan's `files_modified`).

## Threat surface

- T-02-47 (Tampering, push destination): the push destination is asserted to be under the SSH user's home (`.config/pc-switcher/package-snippets.yaml`) with no `/etc` — `test_push_sends_source_registry_under_the_user_home_never_etc`.
- T-02-48 (DoS, transport dependency): the job moves the file itself and reads no `config_sync`/`folder_sync` state — the push tests drive it in isolation.
- T-02-49 (Info disclosure, single-file revert): `SYNCED_CONFIG_FILENAMES` and its multi-file loop are gone (`grep` clean across `src/`, `tests/unit/`); config_sync provably carries one named file.

No new trust boundaries beyond the plan's threat model.

## User Setup Required

None.

## Next Phase Readiness

- Snippet delivery is self-contained in `manual_installs_sync`; `config_sync` carries `config.yaml` only; full unit gate green (1009 passed), `ruff` and `basedpyright` clean.
- 02-21 (integration rework) can proceed; the config_sync integration test is already single-file-correct.

## Self-Check: PASSED

## Known Stubs

None.
