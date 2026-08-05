---
phase: 02-package-management-sync
fixed_at: 2026-07-23T13:35:15Z
review_path: .planning/phases/02-package-management-sync/02-REVIEW.md
iteration: 1
findings_in_scope: 7
fixed: 7
skipped: 1
status: partial
---

# Phase 02: Code Review Fix Report

Fixed at 2026-07-23T13:35:15Z, from .planning/phases/02-package-management-sync/02-REVIEW.md, iteration 1.

Findings in scope: 7 (2 critical, 4 warning, 1 info — IN-02 excluded from scope: reviewer explicitly recommended no fix). Fixed: 7. Skipped: 1 (IN-02, out of scope by the review's own recommendation, documented below for completeness).

All fixes were independently verified by writing a regression test that fails on the pre-fix code and passes after the fix (confirmed by stashing each source change and re-running its test). Full suite: 987 passed (baseline 974 + 13 new regression tests), `ruff check`, `ruff format --check`, and `basedpyright` all clean after every commit.

## Fixed Issues

### CR-01: A repo-group backup failure crashes the whole sync instead of failing one item (D-27 violation)

Files modified: `src/pcswitcher/jobs/apt_sync.py`, `tests/unit/jobs/test_apt_sync.py`. Commit: `8bfe96c`.

`_ensure_repo_group_converged`'s backup loop is now wrapped in `try`/`except ConvergeItemFailed`. On a backup failure, every `group_diffs` item (and the metadata-refresh marker, if approved) is recorded as failed via a new shared `_record_group_failure` helper before returning — instead of leaving `self._repo_group_outcome` at `{}`, which previously made every subsequent group item's `converge()` call raise a bare `KeyError` that escaped the per-item failure handler and crashed the whole job. The same helper now also backs the existing post-rollback failure path (`apt-get update` failure), removing duplicated logic. Regression test drives a `sudo cp -a` failure across a 2-item pin group and asserts the run raises `PackageItemFailures` (not `KeyError`) with both items reported failed.

### CR-02: Writing a per-repo key to a target with no `/etc/apt/keyrings` fails the whole promotion

Files modified: `src/pcswitcher/jobs/apt_sync.py`, `tests/unit/jobs/test_apt_sync.py`. Commit: `3cb4ee9`.

`_write_or_remove_repo_item` now runs `sudo mkdir -p -m 0755 <dest-dir>` before the `sudo install` promotion. Verified empirically (`install -m 0644 src dst` into a missing directory fails; `mkdir -p -m 0755` only chmods directories it actually creates, so this is a no-op for the four `/etc/apt/*` subdirectories that already ship with the `apt` package). A `mkdir` failure raises `ConvergeItemFailed`, keeping this a per-item failure. Regression tests assert the `mkdir` command precedes the `install` command, and that a `mkdir` failure fails the item cleanly without ever attempting the install.

### WR-01: `simulate_apt_transaction` never checks the simulation's own exit code

Files modified: `src/pcswitcher/jobs/apt_sync.py`, `tests/unit/jobs/test_apt_sync.py`. Commit: `4565db8`.

`simulate_apt_transaction` now raises `ConvergeItemFailed` when `result.success` is `False`, instead of parsing a failed `apt-get -s`'s (typically empty) stdout as an indistinguishable-from-clean preview. This fails closed at both call sites — plan-time collateral collection (caught by the coordinator's per-job `plan()` exception handling) and apply-time install/remove guards (caught by the existing per-item `converge()` handler). Regression tests cover the function directly and a job-level scenario where the plan-time simulation succeeds but the apply-time re-simulation fails, confirming the item fails cleanly rather than the real `apt-get install` running.

### WR-03: Dangling-keyring source files downgraded to REPORT_ONLY when missing, but not when changed

Files modified: `src/pcswitcher/jobs/apt_sync.py`, `tests/unit/jobs/test_apt_sync.py`. Commit: `f7fc712`.

`_diff_apt_sources`'s "changed" branch now downgrades `action` to `DiffAction.REPORT_ONLY` when the dangling-keyring check finds a dangling reference, mirroring the existing "missing" branch exactly (previously `action` stayed `CHANGE` regardless). Regression test drives a changed source file with a dangling keyring reference and asserts `action == REPORT_ONLY`.

### WR-04: `/etc/apt/*` content reads use plain `cat`, not the digest capture's `sudo` privilege

Files modified: `src/pcswitcher/jobs/apt_sync.py`, `tests/unit/jobs/test_apt_sync.py`. Commit: `fae91b8`.

`_read_file_content` now runs `sudo cat <path>` instead of a plain `cat`, matching `_capture_dir_digests`'s `sudo find ... sha256sum` privilege. Regression test asserts the source-side content read for a diff-implicated file issues `sudo cat ...`, not the unprivileged form.

### WR-02: D-17's job ordering enforced only by the shipped config file's key order, not by code

Files modified: `src/pcswitcher/orchestrator.py`, `tests/unit/orchestrator/test_config_system.py`. Commit: `4782443`.

Added `Orchestrator._check_package_jobs_precede_folder_sync()`, called from `_discover_and_validate_jobs` after building the job list. It inspects the resolved, enabled `sync_jobs` order (not just the shipped default file) and appends a `ConfigError` naming the offending job for any of `apt_sync`/`snap_sync`/`flatpak_sync` that resolves after an enabled `folder_sync`. This surfaces through the existing "Job configuration validation failed" `RuntimeError` path — a user who hand-edits their `config.yaml` into the wrong order now gets a loud, actionable error instead of a silent flatpak-data-race. Chose the review's "validate and raise" option over "sort jobs structurally" to keep the change narrowly scoped to the orchestrator's existing config-error surface, since a full priority-based re-sort would also need to touch `_first_sync_scopes` and other direct `sync_jobs.items()` iterators — a larger change than this fix session's blast radius should cover. Five regression tests cover: error raised for one/three misordered jobs, no error when correctly ordered, no error for a disabled misordered job, no error when `folder_sync` itself is disabled.

### IN-01: `REPORT_ONLY`'s fallback verb reads awkwardly in the review UI

Files modified: `src/pcswitcher/jobs/package_sync_core.py`, `tests/unit/jobs/test_package_sync_core.py`. Commit: `ffce756`.

`_build_review_groups`'s vocabulary lookup now falls back to the word "report" for any `REPORT_ONLY` diff without a more specific `_ACTION_VOCABULARY` entry, instead of the raw enum value `"report_only"`. Every other action's fallback (`action.value`) is unchanged. Regression test uses a `FLATPAK_REF`/`REPORT_ONLY` diff (no explicit vocabulary entry) and asserts the group title and each entry's `action_label` read "report", not "report_only".

## Skipped Issues

### IN-02: Repository-group rollback assumes `test -f` is sufficient for "existed before"

File: `src/pcswitcher/jobs/apt_sync.py:1242-1260` (`_backup_destination`).

Reason: out of `fix_context` scope by the review's own recommendation — REVIEW.md's Fix section reads "None required at this time; note only." The finding itself frames this as a narrow edge case (a dangling symlink or non-regular file at a `/etc/apt/*` path) with no realistic exploit path in this project's current scope, included for completeness rather than as an action item. No change made.

Original issue: `test -f` follows symlinks and only reports true for a regular file; if a target path is a dangling symlink or non-regular file, `existed_before[dest]` reports `False`, and a rollback would `rm -f` something that technically pre-existed rather than restoring it.

---

_Fixed: 2026-07-23T13:35:15Z_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
