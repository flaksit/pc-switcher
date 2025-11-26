janfrederik: Claude implemented #file:spec.md . Review. Verify if the current state of the project implements the spec completely and correctly.


Wrapped up the foundation-spec review—below I’ll summarize which requirement groups are satisfied, highlight the gaps (with FR references), and note the verification runs.

## compliance overview

| Area | Key FRs | Status | Evidence & notes |
| --- | --- | --- | --- |
| Module contract & orchestrator lifecycle | FR-001, FR-002, FR-004, FR-019 | ✅ Met | `SyncModule` defines the required API; orchestrator (orchestrator.py) loads modules in config order, injects logging/progress, runs validate → pre_sync → sync → post_sync, and handles `SyncError` abort semantics. |
| Configuration loading/validation | FR-028–FR-034 | ⚠️ Partial | config.py loads `~/.config/pc-switcher/config.yaml`, enforces `sync_modules`, log levels, and required `btrfs_snapshots`. However, defaults don’t match the spec’d schema (no global/module sections beyond btrfs), JSON Schema validation happens only per-module (and the schema must be retrieved via constructing a dummy instance). Config attempts to disable required modules do error, but the generated default config in setup.sh is unrelated to the new structure. |
| Logging subsystem | FR-018, FR-020–FR-022 | ✅ Met (source-side) | Structlog wiring (logging.py) implements DEBUG/FULL/INFO/WARN/ERROR/CRITICAL with independent CLI/file levels, JSONRenderer to `~/.local/share/...`, console renderer, and CRITICAL → abort semantics through orchestrator. |
| Cross-host log aggregation | FR-023, FR-045 | ❌ Missing | There’s no mechanism to capture/log structured output from target-side processes; modules never stream remote logs back, so unified log stream + progress-at-FULL-from target is absent. |
| Disk safety | FR-016, FR-017 | ⚠️ Partial | `DiskMonitor` checks the local `/` filesystem and monitors in a thread, but there’s no remote disk check via SSH and no parsing of percentage strings for `reserve_minimum`. Alerts merely print warnings; orchestrator doesn’t abort when space drops below reserved minimum as required. |
| Self-install / version sync | FR-005–FR-007 | ❌ Missing | Version management lives in installer.py but is unused. The CLI/orchestrator never checks target versions, installs, or aborts on downgrade scenarios. |
| Snapshot safety module | FR-008–FR-015 | ❌ Missing major parts | btrfs_snapshots.py creates pre/post snapshots locally only. It doesn’t verify target subvolumes, doesn’t create remote snapshots via `RemoteExecutor`, lacks rollback prompting integration (orchestrator prints but module only supports local), doesn’t enforce read-only remote snapshots, doesn’t implement cleanup command/API for `pc-switcher cleanup-snapshots`, and no disk-threshold enforcement before running modules. Attempted disabling is blocked, but config mismatch (`sync_modules` key uses `btrfs_snapshots` while module name is `btrfs-snapshots`) will prevent the orchestrator from matching the special-case `required` module flag. |
| Rollback & cleanup CLIs | FR-013, FR-014, FR-046 | ❌ Missing | `pc-switcher cleanup-snapshots` is a stub that prints “Not implemented yet”; no rollback command exists beyond ad-hoc prompt. |
| Interrupt handling | FR-024–FR-027 | ⚠️ Partial | Orchestrator installs SIGINT/SIGTERM handlers that raise `KeyboardInterrupt` and call `abort` on the current module during cleanup, but there’s no double-SIGINT fast-exit behavior, no explicit remote process termination, and no guarantees about orphaned target processes. |
| Locking | FR-047 | ❌ Missing | lock.py defines `LockManager`, yet the CLI/orchestrator never acquires or releases a lock, so concurrent sync prevention isn’t active. |
| Installation script | FR-035–FR-037 | ❌ Outdated | setup.sh checks for btrfs and installs `uv` + `btrfs-progs`, but generates an obsolete config schema (`remote`, `sync`, `logging`, `safety`) unrelated to the new YAML layout and doesn’t install pc-switcher from ghcr.io via pinned version or mention snapshot requirements. |
| Dummy modules | FR-038–FR-042 | ✅ Met | `dummy_success`, `dummy_critical`, `dummy_fail` implement the specified behaviors (duration, progress, warnings/errors, raising `SyncError` vs `RuntimeError`, `abort` logging). |
| Progress reporting & UI | FR-043–FR-045 | ⚠️ Partial | Modules can call `emit_progress`, orchestrator logs progress at FULL, and ui.py displays per-module/overall progress. However, CLI doesn’t automatically start UI in headless mode, progress updates aren’t forwarded to log files for remote modules (since there are none), and terminal log rendering doesn’t match the spec’d layout for target logs. |
| CLI commands | FR-046, FR-048 | ⚠️ Partial | `pc-switcher sync <target>` exists, but module summary (success/skipped/failed) only logs to structlog—not in a user-facing table or final summary message. |
| Remote install/upgrade failure handling | FR-006–FR-007 | ❌ Missing | With no invocation of `VersionManager`, there’s no CRITICAL log on downgrade attempt, no forced abort on install failure, and no 30-second completion guarantee. |

## gaps & recommendations

1. **Integrate target version management**: before orchestrator initialization, connect to the remote host, read source/target versions via `VersionManager`, enforce FR-005–FR-007 semantics, and abort on mismatches.
2. **Wire in locking**: wrap `pc-switcher sync` in `LockManager` to block concurrent runs and provide stale-lock guidance per FR-047.
3. **Complete the btrfs module**: extend it to operate on both source and target via the `RemoteExecutor`, implement disk threshold checks, ensure naming pattern `<subvolume>-{pre|post}sync-<ISO>-<session>`, create rollback + cleanup commands (`pc-switcher cleanup-snapshots`), and keep retention settings configurable.
4. **Strengthen disk safety**: convert `reserve_minimum`/`min_free` to accept percentages, run the same logic on the target path via SSH, and trigger a CRITICAL abort when thresholds aren’t met.
5. **Improve interrupt handling**: add double-SIGINT detection (force terminate), remote process cleanup, and ensure exit code 130 with “Sync interrupted by user” per FR-024–FR-027.
6. **Unify log stream**: capture remote stdout/stderr with level prefixes, re-log through structlog so both hosts appear in the JSON log, enabling FR-023.
7. **Refresh config/setup artifacts**: align setup.sh and any default configs with `sync_modules` + per-module schema; document required module names so `btrfs_snapshots` config matches the module class name string used for loading.

## quality gates

- **Build**: Not run (project ships as a CLI package; no build artifacts required for this review).
- **Lint**: PASS – `uv run ruff check`.
- **Tests**: PASS – `uv run pytest` (19 tests).

## verification artifacts

- Pytest log: all suites under tests green (disk, installer, lock, session, signals).
- Ruff linting: no issues reported.

## next steps

Prioritize wiring the missing infrastructure (version sync, lock manager, remote snapshot orchestration) since these are P1 spec items. Once those are in place, revisit the CLI commands (snapshot cleanup, logs UI) and setup tooling to ensure the end-to-end flow prescribed in the spec can actually run. Let me know if you’d like a more detailed implementation plan for any of the missing pieces.
