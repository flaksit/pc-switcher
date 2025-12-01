# Implementation Review – 001-foundation (GPT-5.1-Codex)

## Findings

1. **Critical – Logging & progress infrastructure is not wired up (User Stories 4 & 9, FR-018→FR-023, FR-043→FR-045)**
   - `EventBus.subscribe()` is never called anywhere in `src/pcswitcher`, so the `Logger` instances created by the orchestrator only publish into a void. Neither `FileLogger` nor `ConsoleLogger` is instantiated, and `TerminalUI` is never started (`src/pcswitcher/orchestrator.py`). As a result, no log files are created, nothing is printed at the configured CLI log level, and progress updates are dropped entirely.
   - `pc-switcher logs --last` (`src/pcswitcher/cli.py`, lines 96-130) just prints directory listings rather than showing the latest log file with the Rich formatting described in Acceptance Scenario 6.
   - Because the structlog JSON renderer is never used and there is no consumer for progress events, none of the acceptance scenarios for the logging and terminal UI stories can currently pass.
   - **Fix**: instantiate file + console log consumers (using structlog JSONRenderer/ConsoleRenderer) and a `TerminalUI.consume_events()` task as soon as the orchestrator starts, wire them to `EventBus.subscribe()`, and make `pc-switcher logs --last` stream the newest log file via Rich.

2. **Critical – Btrfs snapshot configuration cannot work with today’s job contract (FR-008, FR-010, FR-015)**
   - `Configuration.BtrfsConfig.subvolumes` is defined as `list[str]` (see `src/pcswitcher/config.py`, lines 24-35 & 150-168) and the default config + schema expect entries such as `"@"` and `"@home"` (matching the spec’s example).
   - `BtrfsSnapshotJob` (`src/pcswitcher/jobs/btrfs.py`, lines 24-102) instead expects each entry to be a mapping with `name` and `mount_point`, immediately indexing `subvol["name"]` / `subvol["mount_point"]`. With the current config structure this raises `TypeError: 'str' object is not subscriptable` before any validation or snapshotting can happen, and the job still lacks the mount-point information it needs to run `btrfs subvolume` commands.
   - **Impact**: the mandatory pre/post snapshot phases never run, so Story 3’s safety guarantees and rollback points are not delivered.
   - **Fix**: align the schema, dataclass, defaults, and job implementation so they agree on the structure (either expand the config to name+mount_point objects or change the job to accept simple subvolume names and derive paths). Add tests that cover both validation and execution to prevent regressions.

3. **Major – Snapshot session organization & cleanup diverge from the spec (FR-010 & FR-014, Story 3 Scenario 7)**
   - `session_folder_name()` (`src/pcswitcher/snapshots.py`, lines 32-42) generates a fresh timestamp every call. `_create_snapshots()` (`src/pcswitcher/orchestrator.py`, lines 248-298) calls it independently for PRE and POST phases, so the two sets of snapshots land in different folders instead of the single `/.snapshots/pc-switcher/<timestamp>-<session-id>/` directory mandated by Story 3.
   - `pc-switcher cleanup-snapshots` (`src/pcswitcher/cli.py`, lines 133-166) requires `--older-than`, prints “Not yet implemented”, and never invokes `snapshots.cleanup_snapshots()`. The retention knobs `keep_recent` and `max_age_days` from config are ignored, so Acceptance Scenario 7 and FR-014 have no implementation.
   - **Fix**: persist the session folder name on the orchestrator so the same directory is reused for both snapshot phases, and wire the CLI cleanup command to `snapshots.cleanup_snapshots()` (defaulting to the config retention policy when `--older-than` is omitted). Perform deletion on both source and target.

4. **Major – Disk-space safeguards omit the mandated preflight check (FR-016)**
   - `DiskSpaceMonitorJob.execute()` (`src/pcswitcher/jobs/disk_space_monitor.py`, lines 103-177) enforces `runtime_minimum` only; `preflight_minimum` is never compared to actual free space anywhere in the codebase. The job’s `validate()` merely parses the strings (`parse_threshold`) without aborting when the current disk usage already violates the preflight limit.
   - **Impact**: sync jobs may start even when free space is already below the configured preflight threshold, violating FR-016 and Story 3’s reliability requirements.
   - **Fix**: add an explicit preflight step (for both hosts) that calls `check_disk_space()` and compares the result to `preflight_minimum` before any snapshots or job execution. Abort with a CRITICAL log when the threshold is not met, and cover both percentage and absolute formats in tests.

5. **Major – Version parity and on-target installation flow do not satisfy User Story 2 / FR-005→FR-007 / FR-035**
   - `get_target_version()` (`src/pcswitcher/installation.py`, lines 41-70) shells out to `pc-switcher --version`, but the Typer app in `src/pcswitcher/cli.py` never defines a `--version` option, so the command exits with an error and the function always returns `None`. `_check_version_compatibility()` therefore always assumes the target is missing pc-switcher and never detects “target newer than source”, so FR-005/FR-006 cannot pass.
   - `_check_version_compatibility()` (`src/pcswitcher/orchestrator.py`, lines 150-195) compares raw strings (`self._target_version > self._source_version`) instead of using `packaging.Version`/`compare_versions`, which yields incorrect ordering for values like `0.10.0` vs `0.9.0`.
   - `install_on_target()` (`src/pcswitcher/installation.py`, lines 88-115) assumes `uv` already exists on the target and installs from PyPI (`uv tool install pcswitcher=={version}`), while FR-005 and FR-035 require installing from the GitHub release URL and reusing the same bootstrap logic as `install.sh` (including installing `uv`/`btrfs-progs` if missing). The orchestrator also postpones installation until after pre-sync snapshots (Phase 7), contradicting Story 2’s “very first operation” requirement.
   - **Fix**: expose a `--version` flag that reports `pcswitcher.__version__`, reuse `compare_versions()` for compatibility checks, ensure the orchestrator installs/upgrades before validation/snapshots, bootstrap `uv` (and other prerequisites) on the target using the same logic as `install.sh`, and install from the GitHub release specified in FR-005.

6. **Minor – `pc-switcher logs --last` does not meet Acceptance Scenario 6**
   - The `logs` command (`src/pcswitcher/cli.py`, lines 96-130) only prints directory listings, even when `--last` is supplied. The spec requires displaying the most recent log file in the terminal using Rich formatting. Once the logging pipeline is wired up (Finding 1), this command still needs to open the log file, render it with highlighting, and optionally support paging.
   - **Fix**: read the newest `sync-*.log` file, stream it through Rich (or Typer’s pager) with color-coding, and exit with a non-zero status when no logs exist.

## Summary
The current codebase still lacks the core foundation pieces described in the specification. Logging/progress output, snapshot safety, disk-space safeguards, and self-installation/version parity all have blocking gaps. Addressing the findings above should be prioritized before moving on to user-facing sync features.
