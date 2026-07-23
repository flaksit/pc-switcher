---
phase: 02-package-management-sync
reviewed: 2026-07-23T00:00:00Z
depth: deep
files_reviewed: 21
files_reviewed_list:
  - src/pcswitcher/cli.py
  - src/pcswitcher/config_sync.py
  - src/pcswitcher/orchestrator.py
  - src/pcswitcher/sudoers.py
  - src/pcswitcher/jobs/apt_sync.py
  - src/pcswitcher/jobs/base.py
  - src/pcswitcher/jobs/context.py
  - src/pcswitcher/jobs/flatpak_sync.py
  - src/pcswitcher/jobs/folder_sync.py
  - src/pcswitcher/jobs/package_items.py
  - src/pcswitcher/jobs/package_phase.py
  - src/pcswitcher/jobs/package_review.py
  - src/pcswitcher/jobs/package_state.py
  - src/pcswitcher/jobs/package_sync_core.py
  - src/pcswitcher/jobs/snap_sync.py
  - src/pcswitcher/schemas/config-schema.yaml
  - src/pcswitcher/default-config.yaml
  - src/pcswitcher/executor.py (send_file/run_command contract only)
  - tests/unit/jobs/test_apt_sync.py
  - tests/unit/jobs/test_package_sync_core.py
  - tests/integration/jobs/test_package_sync.py (read for scope/coverage only, execution deferred)
findings:
  critical: 2
  warning: 4
  info: 2
  total: 8
status: issues_found
---

# Phase 02: Code Review Report

**Reviewed:** 2026-07-23T00:00:00Z
**Depth:** deep
**Files Reviewed:** 21
**Status:** issues_found

## Summary

The plan/apply split, the `PackagePhaseCoordinator`, the apt transaction-simulation guard, the `/etc/apt` staging-then-`sudo install` promotion, and the transactional backup/rollback for the repository group are all implemented and match ADR-020's design.

The nine defects from the prior cross-AI plan review (`02-REVIEWS.md`) are fixed and did not regress: `config_sync.py` now carries `package-snippets.yaml`, `send_file` never targets `/etc/apt` directly, the apt transaction is simulated before both install and remove, `folder_sync` reads `enabled_sync_jobs` for the D-29 gate, and the CLI now derives its exit code from `job_results` rather than from "nothing raised" (confirmed against `orchestrator.py:174-188` and `cli.py`'s `SessionStatus.FAILED` check).

Two BLOCKER-level defects remain, both in the repository-group convergence path (`apt_sync.py`), both reachable on realistic inputs and both invisible to the current mocked-executor unit tests because the tests never simulate a backup-step failure or a target missing `/etc/apt/keyrings`. Four WARNING-level robustness/consistency gaps and two INFO items round out the findings below.

## Critical Issues

### CR-01: A repo-group backup failure crashes the whole sync instead of failing one item (D-27 violation)

**File:** `src/pcswitcher/jobs/apt_sync.py:1148-1196` (`_ensure_repo_group_converged`), `:1122-1128` (`_converge_repo_group_item`), `:1242-1260` (`_backup_destination`)

**Issue:** `_ensure_repo_group_converged` sets `self._repo_group_outcome = {}` (line 1178) before the backup loop runs, then backs up every approved repo-group destination:

```python
existed_before: dict[str, bool] = {}
for diff in group_diffs:
    dest = _repo_item_destination(diff)
    existed_before[dest] = await self._backup_destination(dest, backup_dir)
```

`_backup_destination` raises `ConvergeItemFailed` when `sudo cp -a` fails (line 1257) — this is *not* caught anywhere inside `_ensure_repo_group_converged`. Trace the resulting call chain for the **first** repo-group item `apply()` processes:

1. `converge(diff1)` → `_converge_repo_group_item(diff1)` → `_ensure_repo_group_converged()` raises `ConvergeItemFailed` (e.g. backing up `diff2`'s destination failed, not even `diff1`'s own).
2. This propagates out of `_converge_repo_group_item` uncaught, then out of `converge()`, into `PackageSyncJob._converge_one`'s `except ConvergeItemFailed` — correctly recorded as diff1's failure. So far, so good.

But `self._repo_group_outcome` is left at `{}` — no item ever got an entry, and the idempotency guard (`if self._repo_group_outcome is not None: return`, line 1161-1162) now treats the group as "already handled". For the **second** repo-group item `apply()` processes in the same run:

1. `converge(diff2)` → `_converge_repo_group_item(diff2)` → `_ensure_repo_group_converged()` is now a no-op (short-circuits on the `is not None` check).
2. `succeeded, message = self._repo_group_outcome[diff.item_id]` (line 1125) raises a bare **`KeyError`** — `diff2.item_id` was never inserted into the empty dict.
3. `_converge_one`'s `except ConvergeItemFailed` does **not** catch `KeyError`. It propagates out of `apply()`'s per-diff loop (which has no wrapping try/except around `await self._converge_one(diff, failures)`), out of `execute()`, into the orchestrator's job loop's generic `except Exception as e:` handler (`orchestrator.py:1177-1197`) — which records the job FAILED **and re-raises**, aborting the whole `TaskGroup` (cancelling every later job, including any other already-approved package manager's `apply()` and `folder_sync`).

Concretely: any transient failure of a single `sudo cp -a` backup (disk pressure, a race where the file was deleted between `test -f` and `cp -a`, a read-only remount, etc.) during a run with **two or more** approved repo-group items (e.g. two changed `sources.list.d` files, or a key plus its referencing source) turns a "one item failed" situation into "the entire sync crashes", exactly the outcome D-27 and the coordinator's own "one manager's failures cannot cancel another manager's already-approved work" guarantee (ADR-020, `orchestrator.py:1154-1160`) exist to prevent. No existing test exercises a `_backup_destination` failure (`test_apt_sync.py`'s backup/rollback tests only cover `apt-get update` failing and the write step failing, never the backup step itself), so this is invisible to the current suite.

**Fix:** Populate `self._repo_group_outcome` for every `group_diffs` item as soon as a group-wide failure is known, instead of leaving it partially empty. For example, wrap the backup loop so a raised `ConvergeItemFailed` marks *every* `group_diffs` item (and the metadata-refresh marker, if present) as failed with that message before returning, the same way the `apt-get update` failure path already does at the bottom of the method:

```python
try:
    for diff in group_diffs:
        dest = _repo_item_destination(diff)
        existed_before[dest] = await self._backup_destination(dest, backup_dir)
except ConvergeItemFailed as exc:
    failure_message = f"repository group backup failed: {exc}"
    for diff in group_diffs:
        self._repo_group_outcome[diff.item_id] = (False, failure_message)
    if marker_present:
        self._repo_group_outcome[_METADATA_REFRESH_ITEM_ID] = (False, failure_message)
    return
```

This keeps the failure attributed to D-27's per-item mechanism instead of letting a `KeyError` escape, and matches the "every group item is recorded as a failure" pattern already used for the `apt-get update`-failure rollback path.

### CR-02: Writing a per-repo key to a target with no `/etc/apt/keyrings` directory fails the whole promotion (breaks the primary "sync a fresh machine" case)

**File:** `src/pcswitcher/jobs/apt_sync.py:1262-1296` (`_write_or_remove_repo_item`)

**Issue:** For an `APT_KEY` item with `scope="per-repo"`, `_repo_item_destination` (line 408-411) resolves the destination to `/etc/apt/keyrings/<filename>`. The promotion command is:

```python
promote = await self.target.run_command(
    f"sudo install -o root -g root -m 0644 {shlex.quote(staged_dest)} {shlex.quote(dest)}",
    login_shell=False,
)
```

GNU coreutils `install` does **not** create missing parent directories of `DEST` unless given `-D` (or `-d`/`-t`). `/etc/apt/sources.list.d`, `/etc/apt/preferences.d`, `/etc/apt/apt.conf.d` and `/etc/apt/trusted.gpg.d` all ship as part of the `apt` package on Ubuntu and are guaranteed to exist, but `/etc/apt/keyrings` is a convention several third-party vendors (and this project's own `_APT_KEYRINGS_DIR`) rely on — it is **not** created by a default Ubuntu 24.04 install and only exists on a machine where some prior tool (e.g. Docker's or a PPA's install instructions) created it with `sudo mkdir -p /etc/apt/keyrings`. Nothing in this codebase creates it: `grep mkdir apt_sync.py` shows only `staging_dir` and `backup_dir` are ever `mkdir -p`'d (lines 1183, 1251); the destination directory itself never is.

On a genuinely fresh target — the scenario ADR-015/D-17 and this whole subsystem exist to support — `sudo install ... /etc/apt/keyrings/<file>` fails with "No such file or directory". Because `_write_or_remove_repo_item` raises `ConvergeItemFailed` on a failed `promote` (correctly, per D-27), this becomes a per-item failure rather than a crash — but it means **every per-repo key sync to a fresh machine fails**, for the exact `/etc/apt` inventory this phase's own research calls out ("3 in `/etc/apt/keyrings`" on the reference machine, CONTEXT.md's live inventory). The referencing source file will then also be refused by `_require_keyrings_ready` (since the key never converged), so a fresh-machine sync of any vendor repo using the modern `/etc/apt/keyrings` convention silently degrades to "report failure, apply nothing" instead of the intended "reproduce this machine's repo state".

**Fix:** Either pass `-D` to `install` for the key-write case (creates the leading directory components, still sets the final file's owner/mode as given):

```python
f"sudo install -D -o root -g root -m 0644 {shlex.quote(staged_dest)} {shlex.quote(dest)}"
```

or explicitly `sudo mkdir -p /etc/apt/keyrings` (with `-m 0755` to match apt's own convention) before the first per-repo key promotion in a run. Add a unit test asserting the promote command (or a preceding `mkdir -p`) covers a target where the destination directory does not yet exist.

## Warnings

### WR-01: `simulate_apt_transaction` never checks the simulation's own exit code

**File:** `src/pcswitcher/jobs/apt_sync.py:450-475`

**Issue:** `simulate_apt_transaction` runs `apt-get -s {apt_args}` and parses `result.stdout` for `Inst`/`Remv` lines, but never inspects `result.success` / `result.exit_code` / `result.stderr`:

```python
result = await executor.run_command(f"apt-get -s {apt_args}", login_shell=login_shell)
installs: list[str] = []
removals: list[str] = []
...
for line in result.stdout.splitlines():
    ...
return AptTransactionPreview(installs=tuple(installs), removals=tuple(removals), ...)
```

If `apt-get -s` itself fails (dpkg lock contention that slipped past `validate()`'s point-in-time check, unmet dependencies, a transient apt-cache read error), it typically exits non-zero with no `Inst`/`Remv` lines printed at all. The parsed preview then looks identical to "nothing would happen" — `installs=()`, `removals=()`, `install_versions={}`. Both call sites that gate a real command on this preview (`_converge_install` line 1064-1082, `_converge_remove` line 1092-1102) treat an empty preview as "the transaction is clean" and proceed to run the real `apt-get install`/`apt-get remove`. The task this guard exists for — "refuse to run a real command whose simulation shows collateral effects" — is silently bypassed whenever the simulation itself cannot be trusted, rather than failing closed. No test in `test_apt_sync.py` covers a `success=False` `apt-get -s` result (`grep -n "success=False" tests/unit/jobs/test_apt_sync.py` returns nothing), so this gap is untested as well as unguarded.

**Fix:** Check `result.success` in `simulate_apt_transaction` and raise (or return a sentinel the two call sites treat as "refuse, do not proceed") when the simulation itself failed, rather than degrading to an empty, falsely-clean preview:

```python
if not result.success:
    raise ConvergeItemFailed(f"apt-get -s {apt_args} failed: {result.stderr.strip()}")
```

### WR-02: D-17's "package jobs run before folder_sync" is enforced only by the shipped default file's key order, not by code

**File:** `src/pcswitcher/orchestrator.py:919` (`_discover_and_validate_jobs`), `src/pcswitcher/default-config.yaml:41-57`, `src/pcswitcher/config.py:174`

**Issue:** Jobs are instantiated and later executed strictly in `self._config.sync_jobs.items()` order (`orchestrator.py:919`, `orchestrator.py:1121` `for job_index, job in enumerate(jobs)`), and `sync_jobs` is whatever dict order PyYAML produced from the user's own `config.yaml` (`config.py:174: sync_jobs = data.get("sync_jobs", {})` — no re-ordering, no explicit priority list). D-17 ("package jobs run before folder_sync — decisive for flatpak") is documented only as a comment in `default-config.yaml:46-51` ("reordering these entries changes execution order") and verified only against the *shipped* file (`tests/unit/orchestrator/test_config_system.py:759` `test_package_jobs_precede_folder_sync` loads `_default_config_path()`, not an arbitrary user ordering). Any user who hand-edits their own `config.yaml` — e.g. appending `flatpak_sync: true` after an existing `folder_sync: true` line, which is the natural way to "turn on a new feature" — silently gets `folder_sync` running first, contradicting the "decisive for flatpak" ordering requirement (D-17: `flatpak install` must create `~/.local/share/flatpak` before `folder_sync`'s `~/.var/app` data lands) with no warning, error, or test able to catch it.

**Fix:** Enforce the ordering structurally rather than by convention — e.g. sort resolved jobs by an explicit priority tuple (`SyncJob`-level `ClassVar[int]` ordering key, or a fixed `_JOB_PRIORITY` list the orchestrator consults) before building the execution list, independent of `sync_jobs` dict order. At minimum, validate at startup that `apt_sync`/`snap_sync`/`flatpak_sync` precede `folder_sync` in the *resolved* user config and raise a clear `ConfigurationError` if not, rather than silently reordering the user's intent.

### WR-03: Dangling-keyring source files are downgraded to `REPORT_ONLY` when missing, but not when changed

**File:** `src/pcswitcher/jobs/apt_sync.py:874-890` (`_diff_apt_sources`, "changed" branch)

**Issue:** For a `MISSING_ON_TARGET` source file whose keyring reference is dangling on the source, the action is explicitly downgraded to `REPORT_ONLY` (lines 836-846) — "not proposed for install on its own", per the module's own stated D-12 rule. For the `VERSION_MISMATCH` ("changed") branch, the same dangling check runs (`dangling = _dangling_keyring_ref(...)`) but only affects the `detail` text; `action` stays `DiffAction.CHANGE`:

```python
detail = build_version_mismatch_detail(source_digests[filename], target_digests[filename])
diffs.append(
    ItemDiff(
        item_class=ItemClass.APT_SOURCE,
        diff_class=DiffClass.VERSION_MISMATCH,
        action=DiffAction.CHANGE,
        ...
        detail=build_dangling_keyring_detail(filename, dangling) if dangling is not None else detail,
    )
)
```

A user reviewing this item sees it in the ordinary "change" group as something that will be applied, not carved out into an informational-only fact the way the missing-file case is. It is not exploitable — `_require_keyrings_ready` (line 1298) independently refuses the write at converge time and it becomes a per-item D-27 failure — but the review surface is inconsistent between the two code paths for what the module's own docstring describes as the same underlying condition, and a user who ticks "apply" on this item gets a guaranteed converge-time failure instead of the informational framing the missing-file branch gives them up front.

**Fix:** Downgrade `action` to `DiffAction.REPORT_ONLY` in the "changed" branch too when `dangling is not None`, mirroring the "missing" branch's handling exactly.

### WR-04: `/etc/apt/*` content reads for diff hydration use plain `cat`, not the `sudo find`-based digest capture's privilege

**File:** `src/pcswitcher/jobs/apt_sync.py:275-278` (`_read_file_content`), `:264-272` (`_capture_dir_digests`)

**Issue:** File *digests* for the five `/etc/apt/*` directories are captured via `sudo find ... -exec sha256sum {} +` (both source and target), but the *content* fetched to hydrate a diff-implicated file (`_read_file_content`, used to parse `Signed-By:`, `Package:` pin stanzas, and source file format) runs a plain `cat {path}` with no `sudo`, through `source_run`/`target_run` closures that just call `.run_command(cmd)`. If a source file under one of these directories is not world-readable (permission `0600` or similar — uncommon but not impossible, e.g. an apt auth-bearing `.list`/`.sources` entry someone locked down by hand), the digest capture (root, via `sudo find`) still sees it and proposes a diff, but `_read_file_content`'s unprivileged `cat` silently returns empty stdout instead of failing loudly. For an `AptSourceItem`, empty content means `_parse_source_file` finds zero `keyring_refs`, so `_dangling_keyring_ref` finds nothing to flag — the file installs with `action=INSTALL` even though its real content (never actually read) might reference a key this run never validated. This requires an unusually-permissioned source file to trigger, so it is a robustness gap rather than a routinely-reachable one.

**Fix:** Read file content with the same `sudo`-qualified path the digest capture uses (e.g. `sudo cat {path}`), or explicitly detect a permission-denied `cat` and surface it as a per-item failure instead of silently treating it as an empty file.

## Info

### IN-01: `REPORT_ONLY`'s fallback verb reads awkwardly in the review UI

**File:** `src/pcswitcher/jobs/package_sync_core.py:116-123` (`_ACTION_VOCABULARY`), `:312-341` (`_build_review_groups`)

**Issue:** `_ACTION_VOCABULARY` only maps `(ItemClass.APT_PACKAGE, DiffAction.REPORT_ONLY)` to the word "report". Every other item class's `REPORT_ONLY` diff (e.g. `FLATPAK_REF`/`REPORT_ONLY` version mismatches, `APT_PIN`/`APT_CONFIG` "held or pinned" if ever added) falls back to the raw enum value `"report_only"`, and `f"{verb.capitalize()} {self.manager_id} packages"` produces a group title like "Report_only flatpak packages", and each entry's `action_label` reads "report_only libreoffice (source has 1.0, target has 1.1)". Functionally harmless — nothing is silently dropped, matching the plan's own backstop requirement — but it is a rough edge in the exact review text D-07 says must "name the concrete action".

**Fix:** Add explicit vocabulary entries for every non-apt-package `REPORT_ONLY` case (or special-case `REPORT_ONLY` generically to the word "report" regardless of item class, since none of the current managers give it a more specific meaning).

### IN-02: Repository-group rollback assumes `test -f` is sufficient for "existed before"

**File:** `src/pcswitcher/jobs/apt_sync.py:1242-1260` (`_backup_destination`)

**Issue:** `_backup_destination` uses `test -f {dest}` to decide whether to back up (and, symmetrically, whether rollback should restore-from-backup vs. delete). `test -f` follows symlinks and only reports true for a regular file (or a symlink resolving to one). If a target ever has one of these `/etc/apt/*` paths as a dangling symlink or a non-regular file (e.g. a device node someone hand-placed, or a symlink to a file this user cannot read), `existed_before[dest]` reports `False`, and a rollback would `rm -f` something that technically pre-existed rather than restoring it. This is a narrow edge case with no realistic exploit path in this project's scope, included for completeness rather than as an action item.

**Fix:** None required at this time; note only.

---

_Reviewed: 2026-07-23T00:00:00Z_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: deep_
