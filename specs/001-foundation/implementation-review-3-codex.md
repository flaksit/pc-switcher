# Implementation Review — Feature 001 Foundation

## Verdict
**Status: ❌ Not ready for sign-off.** The core scaffolding (module contract, config loader, Typer CLI, snapshot module skeleton, locking) is in place, but multiple P1 requirements from the spec remain unmet. The gaps below block safe self-installation, disk safety guarantees, logging semantics, and rollback/cleanup behavior.

## Highlights
- `SyncModule` and `RemoteExecutor` (see `src/pcswitcher/core/module.py`) expose the required lifecycle and injection hooks, so future modules can be built independently.
- Config handling (`src/pcswitcher/core/config.py`) enforces `btrfs_snapshots` ordering/enabling, applies sensible defaults, and feeds JSON-schema validation per module.
- The orchestrator does create/read-only btrfs snapshots before and after sync via `BtrfsSnapshotsModule`, and the CLI already exposes `sync`, `logs`, `rollback`, and `cleanup-snapshots` commands through Typer.

## Blocking issues

1. **Self-installation targets the wrong package/registry (US2, FR-005/FR-007).** `VersionManager` hard-codes `PACKAGE_NAME = "pc-switcher"` and runs commands such as `uv tool install pc-switcher=={version}` (see `src/pcswitcher/remote/installer.py` lines ~23, 140, 191). The published package is `pcswitcher` (per `pyproject.toml`), so these commands fail before reaching the target. In addition, the spec requires installing from GHCR (`uv tool install --from ghcr.io/...`), but the implementation always pulls from the default index. **Fix:** use the actual package name (`pcswitcher`), pass the `--from ghcr.io/flaksit/pc-switcher` source, and update log messages to match the acceptance scenarios.

2. **Disk safety requirements aren’t implemented as specified (US3 FR-016/FR-017).**
   - Defaults are wrong: `apply_defaults()` sets `disk.min_free` to `0.20` and `reserve_minimum` to `0.15` (file `src/pcswitcher/core/config.py`, lines ~146-169), but the spec mandates 5 % and 2 % respectively.
   - Percentage strings are rejected when checking the target: `_check_disk_space()` multiplies `target_total_bytes * float(min_free_threshold)` (line ~120), so a valid config value like `"5%"` raises `ValueError` and silently downgrades to the warning path.
   - Continuous monitoring only watches the source (`Path("/")`) and merely logs a warning/prints to stdout (see `_start_disk_monitoring()` in `core/orchestrator.py`), whereas FR-017 requires monitoring *both* source and target and aborting with a CRITICAL log when `reserve_minimum` is breached.
   **Fix:** align defaults with the spec, normalize thresholds (support floats, raw percentages, absolute bytes) for both hosts, monitor the target via the `RemoteExecutor`, and escalate to CRITICAL + abort when the reserve is crossed.

3. **CRITICAL logs do not halt the run (US1/US4 acceptance scenario 3).** The logging processor `_track_error_logs()` only toggles `session.has_errors` (`src/pcswitcher/core/logging.py`), and `_execute_all_modules()` stops only when a module raises `SyncError` or an exception. If any module (or the orchestrator) logs a CRITICAL event, the sync keeps running, violating the requirement that "CRITICAL log events automatically trigger sync abortion". **Fix:** propagate a signal (e.g., set `session.abort_requested` and interrupt the run loop) when structlog sees a CRITICAL log so `_cleanup_phase()` runs immediately and no later modules execute.

## Major gaps


- **Module results never report SKIPPED entries (FR-048).** `session.enabled_modules` is initialized with every module listed in config, but `Orchestrator._execute_all_modules()` only writes SUCCESS/FAILED for modules that actually run. Disabled modules are omitted entirely, so the final summary/logs can’t list `SKIPPED` modules with their status as required. **Fix:** record `ModuleResult.SKIPPED` for every disabled module (and include them in `_log_session_summary()` / UI summary).

- **Dummy test modules diverge from the contract (US8, FR-038–FR-042).** The current implementations (`src/pcswitcher/modules/dummy_*.py`) only spin on the source side, emit progress every second (not 0/25/50/75/100), never perform the target-phase busy waits, raise at ~55 % / 65 % instead of the specified 50 % / 60 %, and their `abort()` methods log generic "abort() called" instead of the required "Dummy module abort called" message. These modules no longer exercise the infrastructure per spec, reducing their usefulness as reference implementations. **Fix:** mirror the scripted behavior precisely (split source/target loops, emit the exact progress milestones and log levels, raise at the prescribed moments, and standardize the abort log).

- **Btrfs snapshots only support three hard-coded subvolumes (US3, FR-015).** `_find_subvolume_path()` in `src/pcswitcher/modules/btrfs_snapshots.py` raises `SyncError` for anything other than `@`, `@home`, or `@root`, even though the schema allows arbitrary subvolume names and validation already confirms they exist. As soon as a user adds `@var`, `@data`, etc., pre-sync fails before reaching the actual snapshot command. **Fix:** derive mount paths dynamically (e.g., from `btrfs subvolume list` output or an explicit config map) so all configured subvolumes—including custom ones—can be snapshotted on both hosts.

- **Snapshot cleanup ignores post-sync snapshots and configurable retention (FR-014).** The Typer command `cleanup_snapshots` (`src/pcswitcher/cli/main.py`, lines ~250-330) only globs `*-presync-*`, hard-codes `--older-than` to `7d`, and uses the CLI flag `--keep-recent` (default 3) instead of `btrfs_snapshots.keep_recent` / `max_age_days`. The spec requires deleting *both* pre- and post-sync snapshots while honoring the defaults that live in config. **Fix:** reuse `BtrfsSnapshotsModule.cleanup_old_snapshots()` (which already knows about both kinds of snapshots and the config defaults) or update the CLI to read the retention settings and consider postsync snapshots.

- **Rollback restores only the source machine (US3 acceptance scenario 6, FR-013).** `BtrfsSnapshotsModule.rollback_to_presync()` exclusively runs local `subprocess` commands and never touches the target via `RemoteExecutor`, and `_execute_rollback()` simply calls that local method. As a result, the "rollback" option leaves the target in the failed state even though pre-sync snapshots exist there. **Fix:** extend rollback to drive both hosts (e.g., add a remote counterpart or run the module twice—local and remote) so the system truly returns to the pre-sync state.

- **Unknown modules in config aren’t rejected (Edge case section).** `validate_module_names()` is defined but never invoked, so a typo like `sync_modules: { pkgs: true }` falls through to `_import_module_class()` and surfaces later as a generic import error. The spec explicitly calls out that unknown module names must be detected early with a clear error message. **Fix:** call `validate_module_names()` during `load_config()` with the repository’s module registry so the CLI can fail fast with the prescribed message.

## Minor observations

- **Terminal UI never renders the log stream (US1 scenario 3, US9).** `TerminalUI.display_log()` is never called; logging goes straight to structlog’s console handler while `Live` is running. This means INFO/WARN/ERROR messages are interleaved with the Rich progress output instead of being routed through the UI at the configured CLI log level, contrary to the UX described in the spec. Consider wiring the injected `module.log()` helper (or a structlog processor) to forward log entries into the UI so users see color-coded logs under the progress bars.

## Suggested next steps
1. Fix the self-install path (`VersionManager`) to resolve the package/registry mismatch and re-run the self-install acceptance scenarios.
2. Rework disk safety handling (defaults, threshold parsing, CRITICAL aborts, remote monitoring) and add regression tests for the FR-016/FR-017 cases.
3. Introduce a mechanism for CRITICAL logs to halt the orchestrator immediately.
4. Bring the dummy modules, snapshot cleanup, rollback, and btrfs path resolution back in line with the specification so the infrastructure exercises remain truthful.
5. Add config validation for unknown modules and integrate log streaming into the Terminal UI to match the documented UX.

Once these items are addressed (and validated via tests or manual scenarios), the feature should be ready for another review round.
