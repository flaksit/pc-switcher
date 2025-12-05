# Task Integration Review – 001-foundation

## Missing deliverables
- T025: Disk threshold parsing is not implemented in the configuration loader; thresholds are left as raw strings with no parsing/normalization (src/pcswitcher/config.py:149-167).  
  --> IGNORE
- Phase 5 logging: JobLogger/structlog-based pipeline and the specified log file format are absent; FileLogger only serializes LogEvents to JSON and never captures progress events, so FULL-level progress is missing from logs (src/pcswitcher/logger.py:60-97).
- T080–T083: The installation helper module described in tasks (installation.py with get_this_version/get_target_version/version comparison/install_on_target) does not exist; logic is split between version.py and jobs/install_on_target.py (src/pcswitcher/jobs/install_on_target.py:36-79), diverging from the task deliverable.  
  --> IGNORE

## Missing wiring/integration
- Background monitors block completion: DiskSpaceMonitorJob instances are started inside a TaskGroup but never cancelled after sync jobs finish, so `_execute_jobs` never exits and `orchestrator.run()` cannot complete successfully (src/pcswitcher/orchestrator.py:486-575; src/pcswitcher/jobs/disk_space_monitor.py:139-204).
- DiskSpaceMonitor validation is never executed before monitors start, so invalid thresholds or missing mount points are not caught pre-run (src/pcswitcher/orchestrator.py:500-575).  
  --> NOT A PROBLEM ANYMORE  
  --> in DiskSpaceMonitorJob.validate(), check_disk_space() is called. However, this could raise errors for other reasons than missing mount points. Implement a specific validation that only checks that the mount points exist.
- Target-side lock is not actually held: `acquire_target_lock` runs `flock` in a short-lived command and releases the lock immediately afterward, so concurrent syncs on the target are not prevented (src/pcswitcher/lock.py:87-118).  
  --> IS THIS REALLY A PROBLEM? OR DOES THE REVIEWER NOT UNDERSTAND HOW IT WORKS?
- Self-installation order and robustness diverge from the spec: installation runs after pre-snapshot work and validation (not first), relies on `get_this_version()` which fails when running from source without package metadata, and fetches install.sh from a branch path that may not exist (`refs/heads/v<version>`) (src/pcswitcher/orchestrator.py:178-191; src/pcswitcher/jobs/install_on_target.py:36-79).  
  --> DOES IT DIVERGE FROM THE SPEC? THEN THE SPEC IS NOT UP TO DATE and should be fixed  
  --> DOES get_this_version() FAIL when running from source? This is annoying. What can we do about it?
- Interrupt handling gaps: the SIGINT path only cancels the main task; it does not request job termination, wait for cleanup using `CLEANUP_TIMEOUT_SECONDS`, or kill remote processes, so cleanup guarantees and the double-SIGINT behavior from US5 are missing (src/pcswitcher/cli.py:189-247; src/pcswitcher/connection.py:87-110 unused).
- DummySuccessJob ignores its configuration schema (`source_duration`/`target_duration`), so config knobs have no effect and acceptance scenarios relying on configurable timings cannot be exercised (src/pcswitcher/jobs/dummy.py:22-90).
- Terminal UI overall progress is unwired: `total_steps` is set but `set_current_step` is never called, so the “Step N/M” indicator never advances and system phases (locks, install, snapshots) are invisible in the UI (src/pcswitcher/ui.py:60-115; src/pcswitcher/orchestrator.py).
- Snapshot cleanup command only operates on the local host and the dry-run mode explicitly skips deletion logic, so cross-host cleanup and preview behavior from US3 acceptance #7 are missing (src/pcswitcher/cli.py:270-305).  
  --> SNAPSHOT CLEANUP MUST ONLY DELETE SNAPSHOTS ON THE LOCAL HOST. Update the spec accordingly if it says otherwise.
- Logging gaps: progress events are not persisted to log files and log output does not follow the `[TIMESTAMP] [LEVEL] [MODULE] [HOSTNAME] message` format described in the spec, reducing auditability of progress reporting.  
  --> PROGRESS EVENTS MUST BE EMITTED TO THE LOG FILE.  
  --> LOG FILE FORMAT SHOULD BE JSON. Update the spec accordingly if it says otherwise.

## Suggestions
- Cancel or scope the DiskSpaceMonitor tasks so they stop when sync jobs finish (or on error), and run `DiskSpaceMonitorJob.validate` before starting them to catch bad thresholds early.
- Hold the target lock for the entire session (e.g., keep a long-lived flock FD/process) and release it explicitly during cleanup.
- Move version check/installation to the start of the workflow, make it work when running from source (fallback version source), and use a stable install source (tagged install.sh or `uv tool install git+ssh...@<version>`).
- Implement the SIGINT flow per US5: request job termination, wait for cleanup up to `CLEANUP_TIMEOUT_SECONDS`, force-kill remote processes on second SIGINT, and record the interrupt in logs/UI.
- Honor job configuration in DummySuccessJob and apply schema defaults for missing `sync_jobs` entries so defaults match the schema.
- Emit progress events into the log file (gated by log level) and align log formatting with the spec; add a per-run summary log entry after all jobs complete.
- Wire TerminalUI overall steps (pre-snapshot → install → each job → post-snapshot) and surface progress/logs for system jobs so US9 scenarios are visible.
- Extend `cleanup-snapshots` to support target cleanup and implement a meaningful dry-run preview based on retention rules.
