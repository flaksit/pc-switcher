# Implementation review — 001-foundation

Branch: `001-foundation`

Date: 2025-11-16

Reviewer: automated review (summary and actionable items)

## Summary

I reviewed the feature specification `specs/001-foundation/spec.md` and the current implementation in `src/pcswitcher/` (core modules, configuration, remote installer, btrfs snapshots module, and dummy test modules).

Overall status: mostly implemented, but there are several correctness and robustness issues to address before the feature can be considered a faithful and complete implementation of the spec.

Below I list what is implemented well, what is partially implemented, and what is missing or incorrect with specific, actionable fixes.

---

## What is implemented correctly (PASS)

- Module contract (FR-001): There is a clear `SyncModule` abstract base class in `src/pcswitcher/core/module.py` with the expected lifecycle methods and properties (validate(), pre_sync(), sync(), post_sync(), abort(timeout), get_config_schema(), name, required). `emit_progress()` and `log()` are present as methods that orchestrator injects. This matches the intended contract.

- Required snapshot module present (FR-008..FR-013): `src/pcswitcher/modules/btrfs_snapshots.py` implements a `BtrfsSnapshotsModule` with `required = True`, validate(), pre_sync(), post_sync(), rollback, and cleanup logic. Snapshot naming uses ISO-like timestamps and includes a session id, and snapshots are created read-only via btrfs commands. The module also offers rollback and cleanup helpers.

- Dummy test modules (FR-008/FR-009): `dummy_success`, `dummy_critical`, and `dummy_fail` modules exist and implement progress reporting, logging at different levels, abort semantics, and the failure scenarios described in the spec. These are well-aligned with the acceptance scenarios for test modules.

- Configuration system basics (FR-006/FR-012/FR-013): `src/pcswitcher/core/config.py` implements loading YAML config, validation of structure, applying defaults, enforcing that `btrfs_snapshots` is present and first, and JSON-schema module config validation helper `validate_module_config()` is present.

- Logging (FR-004): A six-level logging system is implemented in `src/pcswitcher/core/logging.py`. `LogLevel` includes FULL between DEBUG and INFO and `structlog` is configured with file + console outputs. `logger.full(...)` exists and is used by orchestrator and modules.

- Orchestrator lifecycle (FR-002, FR-003, FR-005 partly): The `Orchestrator` class in `src/pcswitcher/core/orchestrator.py` contains the high-level state machine and phase structure (INITIALIZING, VALIDATING, EXECUTING, etc.), and calls during initialization include version check, btrfs verification, loading modules, validation and execution phases. It injects log/progress into modules and intends to call abort() on interrupts/failures.

---

## Partial / Problematic implementations (needs fixes)

These items are implemented but with issues that need attention to satisfy the spec precisely.

1) Version synchronization and installation (FR-005, FR-006)
- Observed: `src/pcswitcher/remote/installer.py` implements `VersionManager` that checks versions using `pip show pcswitcher` and installs by transferring and running `scripts/setup.sh` on the target. The orchestrator calls this via `_ensure_version_sync()`.
- Spec requires installation/upgrades to be performed using `uv tool install pc-switcher==<version>` from the GitHub Package Registry (ghcr.io) and specifically expects the orchestrator to use that mechanism and to abort on target > source versions. The implementation currently uses the setup script and pip, and `VersionManager.ensure_version_sync()` prints messages rather than using the logging system. This is a functional mismatch.
- Actionable fix: Update `VersionManager` to either (a) call `uv tool install pc-switcher==<version>` on the target (transfer/install uv if missing), or (b) ensure `scripts/setup.sh` uses `uv tool install` internally and that `VersionManager` documents and uses that path. Also change prints to use the orchestrator logger or raise `InstallationError` with informative messages. Ensure `VersionManager` exposes an interface that the orchestrator consumes (see also point 2 below).

2) Tight coupling to remote connection internals
- Observed: Orchestrator constructs `VersionManager(self.remote._connection)` — it references a private `_connection` attribute that is not part of the `RemoteExecutor` ABC. The `RemoteExecutor` interface exposes `run()`, `send_file_to_target()`, and `get_hostname()` but not `_connection`.
- This introduces a coupling/brittleness and violates the module abstraction. On many remote executor implementations the underlying connection field will have a different name or not exist.
- Actionable fix: Modify `VersionManager` API to accept a `RemoteExecutor` instance, or add helper methods to `RemoteExecutor` to perform higher-level tasks (like send file and run a remote command). Update `_ensure_version_sync()` to pass `self.remote` (the `RemoteExecutor`) to `VersionManager` rather than a private attribute.

3) Disk space configuration and checks (FR-008 / disk thresholds)
- Observed: Orchestrator `_check_disk_space()` obtains min_free_threshold via `self.config.module_configs.get("btrfs_snapshots", {}).get("min_free_threshold", 0.20)` while the configuration system uses top-level `disk` settings (e.g., `disk.min_free` or `disk.min_free` as seen in defaults). This is inconsistent: disk thresholds are defined under `disk` in `config.py` but the orchestrator reads them from `btrfs_snapshots` module config. The default keys also differ (`min_free` vs `min_free_threshold` vs `reserve_minimum` vs `reserve_minimum` names across spec/code). This will produce surprising behaviour.
- Observed: The remote disk space check uses a fragile awk command and then attempts to parse stdout into a float and compare to a computed required size. This parsing is brittle and not well-documented.
- Actionable fix: Decide on a single canonical configuration location and keys for disk thresholds (I recommend `config.disk.min_free` and `config.disk.reserve_minimum` matching the spec). Update `_check_disk_space()` to read from `self.config.disk` and provide clear parsing and robust remote disk checks (e.g., run `python -c 'import shutil,os; st=shutil.disk_usage("/"); print(st.free)'` on remote and parse integers safely). Add unit tests for parsing.

4) Signal handling / interrupt lock bug
- Observed: Orchestrator sets `self._interrupt_lock = signal.lock()` — there is no `signal.lock()` function in the Python standard library. This will raise an AttributeError at runtime during Orchestrator initialization on most Python versions.
- Actionable fix: Use threading.Lock() or a simple boolean flag with proper synchronization instead of `signal.lock()`. Example: `import threading; self._interrupt_lock = threading.Lock()` and use safe locking in the handler.

5) Orchestrator referencing methods not shown or fragile implementations
- Observed: Orchestrator uses `self._start_disk_monitoring()` and `_inject_module_methods()` etc. I didn't read all of these methods in full, but where present they attempt to call remote internals or operate with assumptions (see points above). Please ensure `_inject_module_methods()` binds the module to the orchestrator logging via `module.log = self.logger` wrappers and `module.emit_progress = ...` in a safe manner.

6) Logging / prints mismatch
- Observed: `VersionManager` uses `print()` for status messages (INFO/DEBUG/CRITICAL) rather than using the project's logging system. This produces inconsistent log streams and will bypass structlog's session error tracking.
- Actionable fix: Replace prints in `VersionManager` with logger calls (use `get_logger()` from core.logging or accept a logger dependency). Ensure all target-side outputs are channeled through orchestrator log aggregation methods (e.g., `log_remote_output`).

7) Orchestrator uses `self.remote.run(...)` and expects `CompletedProcess` like object with `stdout`, `stderr` and `returncode` — RemoteExecutor.run's docstring declares `CompletedProcess[str]` return, but some remote implementations may return a custom result object. Ensure the `RemoteExecutor` contract is explicit and implementations return a standardized result type. Add type hints or a small `RemoteResult` dataclass if helpful.

---

## Fail / Missing items (must-fix to meet acceptance criteria)

These are items that do not meet the spec's acceptance criteria or are absent.

1) Installation mechanism mismatch (FR-005 acceptance scenarios)
- Spec specifically requires `uv tool install pc-switcher==<version>` from ghcr.io. Current implementation uses `scripts/setup.sh` and `pip show`, which is not the same. This fails the acceptance scenario where the orchestrator must install/upgrade via the package registry toolchain.
- Required change: Implement the `uv tool` install flow (or make `setup.sh` a thin wrapper that runs `uv tool install` and ensure `VersionManager` calls `uv tool install` on the target). Provide explicit tests that simulate a missing pc-switcher on target and verify installation method and success.

2) Orchestrator uses private connection attribute (major API mismatch)
- As noted above, `VersionManager(self.remote._connection)` is a design bug; orchestrator should not reach into remote executor internals. This is a functional bug that will break with different remote executor implementations and should be corrected.

3) Signal locking bug will crash during init
- The use of `signal.lock()` must be replaced before this code runs in real environments.

4) Disk threshold config mismatch
- Orchestrator reads disk thresholds from module configs while `config.py` stores them under top-level `disk`. This makes pre-sync disk checks potentially use wrong defaults and not respect user config.

5) Some acceptance scenarios rely on logging and behavior that need verification via tests
- For example, acceptance scenario: "CRITICAL log triggers immediate sync abort" — there are places where CRITICAL is logged and they raise SyncError, but we should add automated tests that simulate module SyncError and verify orchestrator calls abort(timeout=5.0) and exits as expected. The dummy modules are useful here, but tests should assert orchestrator behavior (unit/integration tests).

---

## Concrete fixes and prioritized action list (developer-friendly)

1. High priority (must fix before merge)
  - Replace `signal.lock()` with `threading.Lock()` or other valid synchronization primitive (in `orchestrator.__init__`). Add unit test to ensure Orchestrator initializes successfully.
  - Change `orchestrator._ensure_version_sync()` to call `VersionManager` with a `RemoteExecutor` instance (not `self.remote._connection`) or update the `VersionManager` constructor to accept `RemoteExecutor`. Remove direct private-field access.
  - Implement version installation according to the spec: ensure target installation uses `uv tool install pc-switcher==<version>` via `VersionManager` (or make `scripts/setup.sh` explicitly delegate to that command). Replace prints with logging.
  - Fix disk configuration lookup: use `self.config.disk` to read `min_free`/`reserve_minimum`/`check_interval`. Remove duplicate/conflicting keys and document canonical config schema. Update `_check_disk_space()` to use robust remote commands to fetch free bytes (prefer `python -c 'import shutil; print(shutil.disk_usage("/").free)'`) or `statvfs` approach and parse integers reliably.

2. Medium priority (should be fixed before release)
  - Ensure `RemoteExecutor.run()` returns a standard, documented result object (use subprocess.CompletedProcess-like or define `RemoteResult` dataclass). Add type hints and tests for remote executors.
  - Replace `VersionManager` print() with logger usage (accept logger dependency or use `get_logger`). Ensure messages propagate to unified log stream and session error tracking.
  - Add unit tests for orchestrator lifecycle: validate all modules run validate() before pre_sync tasks; pre_sync snapshots created first and are session-tagged; abort() called on Ctrl+C.

3. Low priority / polish
  - Harden parsing of btrfs subvolume list and remote outputs; document expected formats and add defensive parsing.
  - Add tests for snapshot naming and snapshot cleanup behaviors.
  - Add integration test to verify rollback prompts and rollback behavior using the `BtrfsSnapshotsModule.rollback_to_presync()` method (this will be integration-style and can be optional to run in CI if environment supports btrfs).

---

## Suggested test cases to add (minimum set)

1. Unit: Orchestrator initialization does not raise (fix signal.lock bug first).
2. Unit: VersionManager.compare_versions covers typical and edge version strings.
3. Unit: Config validation raises if `btrfs_snapshots` missing/disabled/first not satisfied.
4. Integration (mocked remote): When `dummy_critical` raises SyncError, orchestrator logs CRITICAL, calls abort(timeout=5.0) on the module, and stops executing further modules.
5. Integration (mocked remote): When target pc-switcher missing, orchestrator calls install method and proceeds only when install succeeded; when install fails, orchestrator logs CRITICAL and aborts.
6. Integration (local system or container with btrfs): `btrfs_snapshots.pre_sync()` creates snapshots with correct names and makes them read-only.

---

## Files inspected

- `specs/001-foundation/spec.md` (source of truth)
- `src/pcswitcher/core/module.py`
- `src/pcswitcher/core/orchestrator.py`
- `src/pcswitcher/core/session.py`
- `src/pcswitcher/core/config.py`
- `src/pcswitcher/core/logging.py`
- `src/pcswitcher/remote/installer.py`
- `src/pcswitcher/modules/btrfs_snapshots.py`
- `src/pcswitcher/modules/dummy_success.py`
- `src/pcswitcher/modules/dummy_critical.py`
- `src/pcswitcher/modules/dummy_fail.py`

---

## Conclusion / Acceptance checklist (quick)

- FR-001 Module contract: PASS
- FR-002 Module lifecycle order: PARTIAL (orchestrator contains lifecycle but requires tests; review of full lifecycle methods left to developer to verify `_validate_all_modules()` and `_execute_all_modules()` implementation details)
- FR-003 Abort-on-Ctrl+C semantics: PARTIAL (intention present; but `signal.lock()` bug and need to confirm abort(timeout=5.0) call path)
- FR-004 Logging: PASS
- FR-005 Self-install via `uv tool install` from ghcr.io: FAIL (current implementation uses setup.sh / pip; needs to follow spec)
- FR-006 Prevent downgrade: PARTIAL (VersionManager compares versions and raises on target > source, but messages are via print and orchestrator coupling to internals needs fix)
- FR-008..FR-013 Snapshots safety: PASS (module implements behavior), but tests needed to fully verify on real btrfs hosts
- Config system (FR-006/FR-012): PARTIAL (core functionality in place, but inconsistent key names and configuration access paths must be aligned)
