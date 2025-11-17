# Implementation Review — Feature 001 Foundation (Round 4)

## Verdict
**Status: ❌ Not ready for sign-off.** The CLI, locking, and module/orchestrator scaffolding are in place, but several P1 requirements from the foundation spec remain unmet. Snapshot safety/rollback is currently non-functional, disk-guard rails are incomplete, optional modules can’t be disabled without breaking the run, and the dummy/UX infrastructure required for future work diverges from the contract.

## What’s working
- `SyncModule`/`RemoteExecutor` provide the required lifecycle hooks and the orchestrator injects progress/logging callbacks so new modules can plug in with minimal glue (US1).
- The Typer CLI exposes the required entry points (`sync`, `logs`, `rollback`, `cleanup-snapshots`) and wires in locking plus structlog-based dual log sinks (US4/US6).
- Remote version matching is performed up front inside `_ensure_version_sync()` using `uv tool install git+https://github.com/flaksit/pc-switcher@vX`, so the self-installation plumbing exists (US2).

## Blocking issues

1. **Snapshot lifecycle & rollback break the core safety story (US3 – FR-008/009/010/013).**
   - `BtrfsSnapshotsModule` generates its own random session id every time `pre_sync()` runs (`self._session_id = generate_session_id()` in `src/pcswitcher/modules/btrfs_snapshots.py`), but rollback and the log summaries key off `SyncSession.id`. As a result, `pc-switcher rollback --session <real-id>` can never find the pre-sync snapshots it just created.
   - Because `btrfs_snapshots` is enforced as the *first* module (see `validate_required_modules` in `core/config.py`), `ModuleLifecycleManager.execute_all_modules()` drives its entire lifecycle before anything else. That means `post_sync()` fires immediately after `pre_sync()` and before the other modules mutate any state, so the so-called “post-sync” snapshots capture the same state as the pre-sync snapshots instead of the final state required by FR-009.
   - Rollback only touches the source machine: `rollback_to_presync()` shells local `btrfs` commands via `subprocess`, and the `pc-switcher rollback` CLI hard-codes a `TargetConnection("localhost")`. Target-side snapshots are never restored, violating FR-013 acceptance scenario 6.

2. **Disk-space safety checks are incomplete and can’t enforce the configured thresholds (US3 – FR-016/FR-017).**
   - `_check_disk_space()` tries to compute `required_target_bytes = target_total_bytes * float(min_free_threshold)` (`src/pcswitcher/core/orchestrator.py`, around line 585). Config values are strings like "20%"/"50GiB", so `float()` raises `ValueError`, gets caught, and the function simply logs a warning. In practice the target-side pre-flight check never aborts even when space is critically low.
   - `_start_disk_monitoring()` only watches `Path("/")` on the source and merely emits a WARNING plus `print()` output when the reserve threshold is crossed. There is no monitoring of the target host and no CRITICAL log/abort when runtime thresholds are violated, which is explicitly required by FR-017.

3. **Optional modules can’t be disabled and module summaries can’t represent SKIPPED modules (US6 – FR-032/FR-048).**
   - The CLI seeds `SyncSession.enabled_modules` with **every** entry in `cfg.sync_modules`, regardless of whether the value is `true` or `false` (`src/pcswitcher/cli/main.py`, session creation). `_determine_final_state()` later iterates over this list and expects each module to have a `ModuleResult.SUCCESS`. Any module set to `false` therefore causes the run to conclude in `FAILED` even though it never ran. Users cannot express `sync_modules: { user_data: true, k3s: false }` as mandated by the spec, and the summary/logs never emit `SKIPPED` entries.

4. **Snapshot cleanup/retention ignores half of the data and the user-configured policy (US3 – FR-014).**
   - The `cleanup_snapshots` Typer command only globs `*-presync-*-*` (see `src/pcswitcher/cli/main.py`, lines ~280-320) so post-sync snapshots are never considered, contradicting acceptance scenario 7.
   - The defaults come exclusively from CLI flags (`--older-than`, `--keep-recent`). The retention knobs specified in `btrfs_snapshots.keep_recent` / `max_age_days` are ignored, so operators cannot change the policy in `config.yaml` as FR-014 requires. (Oddly, `BtrfsSnapshotsModule.cleanup_old_snapshots()` already implements the desired behavior but is never invoked.)

5. **Custom subvolumes pass validation but can’t be snapshotted (US3 – FR-015).**
   - `_find_subvolume_path()` only knows about `@`, `@home`, and `@root` (`src/pcswitcher/modules/btrfs_snapshots.py`, lines ~525-565). Any other subvolume that *does* exist (and even passes `validate()`) raises a `SyncError` the moment `pre_sync()` tries to snapshot it. The spec explicitly calls for handling the configured subvolumes as reported by `btrfs subvolume list /`.

6. **CRITICAL log events don’t halt execution (US4 acceptance scenario 3).**
   - The logging processor `_track_error_logs()` merely toggles `session.has_errors` (`src/pcswitcher/core/logging.py`). `_module_manager.execute_all_modules()` keeps marching through the module list unless a module raises `SyncError`. This violates the requirement that a CRITICAL log "immediately signals the orchestrator to abort sync, call abort(timeout), and run no further modules."

7. **Dummy test modules don’t follow the documented behavior (US8 – FR-038…FR-042).**
   - `DummySuccessModule` performs a single tight loop, does not separate “source” vs “target” phases, emits progress every second instead of the required 0/25/50/75/100 milestones, and logs WARNING/ERROR at the wrong offsets.
   - `DummyCriticalModule` raises `SyncError` when `i == duration // 2`, which (because `i` is 0-indexed) ends up at ~55% instead of the specified 50% mark, and it never emits the CRITICAL log itself.
   - All three dummy modules log "abort() called" instead of the mandated "Dummy module abort called" message, so you can’t validate abort-handling via the log stream.
   These modules are supposed to be the reference implementation for module authors and infrastructure tests, so diverging from the spec makes it harder to validate the system.

## Additional observations
- The Terminal UI never displays the log stream at the configured CLI log level: `TerminalUI.display_log()` is unused and structlog writes directly to the console handler, so acceptance scenario 3 of User Story 9 (logs rendered below the progress indicators) isn’t met.
- `pc-switcher cleanup-snapshots` and `pc-switcher rollback` operate only on the source machine; nothing ever touches the target-side snapshots even though the spec stresses symmetry across both hosts.

## Recommended next steps
1. Rework the snapshot module so it consumes the real `SyncSession.id`, delays post-sync snapshot creation until *after* every other module finishes, and teaches rollback/cleanup to operate on both source and target using the captured session id.
2. Fix disk-threshold parsing/monitoring: normalize `%`/`GiB` inputs once, enforce pre-flight checks on both hosts, monitor both hosts during runtime, and raise CRITICAL + abort when the runtime minimum is breached.
3. Track enabled vs disabled modules separately (or record `ModuleResult.SKIPPED`) so `sync_modules: {... false}` behaves as documented and summaries remain truthful.
4. Wire the existing `BtrfsSnapshotsModule.cleanup_old_snapshots()` (or equivalent logic) into the CLI, extend it to cover `*-postsync-*`, and respect the retention defaults stored in `config.yaml`.
5. Allow arbitrary subvolume names by deriving mount points dynamically (from `btrfs subvolume list` or an explicit config map) instead of hard-coding three names.
6. Add a structlog processor or orchestrator hook that converts any CRITICAL log into an immediate abort (calling `abort()` on the current module and preventing later modules from running).
7. Bring the dummy modules back in line with FR-038–FR-042 so the infrastructure exercises reflect reality, and hook the Terminal UI up to the log stream so the UX matches User Story 9.

Once these items are addressed and validated (unit tests + manual acceptance scenarios), the foundation feature should be ready for another review pass.
