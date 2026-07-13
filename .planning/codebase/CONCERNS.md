<!-- refreshed: 2026-06-29 -->
# Codebase Concerns

**Analysis Date:** 2026-06-29

## Tech Debt

### Session Config Snapshot Missing
- Issue: Session config is initialized as empty dict `{}` and never populated with the actual configuration used for the sync
- Files: `src/pcswitcher/orchestrator.py:158`
- Impact: Audit trail incomplete — cannot later review what config was active when a sync occurred; limits forensics and rollback safety
- Fix approach: Capture full config snapshot during session initialization; store in `SyncSession.config`

### Hardcoded Subvolume-to-Mount-Point Mapping
- Issue: `subvolume_to_mount_point()` uses a fixed mapping (@→/, @home→/home, etc.) instead of querying live from the system
- Files: `src/pcswitcher/jobs/btrfs.py:39`
- Impact: Breaks on custom btrfs layouts; snapshots created at wrong paths if user has non-standard mount scheme
- Fix approach: Query `btrfs subvolume list` or mount table at runtime to derive actual mount points for each subvolume

### Exception Masking in Lock Acquisition
- Issue: Broad `except Exception:` swallows all errors from `start_persistent_remote_lock()`, returning None instead of reporting actual failure
- Files: `src/pcswitcher/lock.py:155`
- Impact: Network timeouts, permission errors, and SSH failures silently treated as "lock already held"; user gets misleading "machine already in sync" error instead of real cause
- Fix approach: Catch specific exceptions (TimeoutError, SSHException, etc.); re-raise unexpected ones; log actual error before returning None

## Known Bugs

### Command Execution Quoting Risk in Config Sync
- Symptoms: If config file path contains spaces or special characters, SFTP copy may fail silently
- Files: `src/pcswitcher/config_sync.py:39` (cat command), `src/pcswitcher/config_sync.py:310-313` (home directory expansion)
- Trigger: Config file at path like `/home/user name/config.yaml` or home dir with special chars
- Workaround: Ensure config path has no spaces; use standard home directories

## Security Considerations

### File Ownership Not Verified on SFTP Transfer
- Risk: Config file copied to target without verifying ownership/permissions on either side; could allow privilege escalation if source config is world-readable or target directory is writable by other users
- Files: `src/pcswitcher/executor.py:312-320` (send_file), `src/pcswitcher/config_sync.py:314-316` (copy_config_to_target)
- Current mitigation: Config directory created with `mkdir -p` (default umask); file contents validated by user before sync
- Recommendations: (1) Verify source config is 0600 (owner-read-write only) before copying; (2) On target, create config dir with explicit mode 0700 and verify after transfer; (3) Document config file security requirements

### Subprocess Shell Injection in Btrfs Commands
- Risk: Subvolume names in snapshot paths are user-controlled via config; no escaping applied before shell execution
- Files: `src/pcswitcher/jobs/btrfs.py:151, 186` (mkdir with user-supplied session_folder), `src/pcswitcher/jobs/btrfs.py:153, 188` (create_snapshot)
- Current mitigation: Config schema restricts subvolume names to pattern `^@`; session_folder from orchestrator is random hex (secrets.token_hex)
- Recommendations: (1) Validate session_folder is hex-only before use; (2) Use subprocess args array instead of shell strings; (3) Add integration test with pathological session_folder names

### SSH Connection Not Re-validated on Reconnect
- Risk: Connection may be established but later drop silently if keepalive fails; next command hangs or times out unexpectedly
- Files: `src/pcswitcher/connection.py:70-76` (connect), `src/pcswitcher/executor.py:264-275` (run_command with timeout)
- Current mitigation: keepalive_interval=15s, keepalive_count_max=3 (45s before disconnect detected); command-level timeouts
- Recommendations: (1) Log all keepalive failures; (2) Add health check before critical phases; (3) Test keepalive behavior with simulated network interruptions

## Performance Bottlenecks

### Login Shell Overhead on Every Remote Command
- Problem: RemoteExecutor wraps commands in `bash -l -c` to source ~/.profile, adding 10-50ms per command (noted in code)
- Files: `src/pcswitcher/executor.py:218-236` (login shell wrapping), `src/pcswitcher/executor.py:260-262` (default_login_shell flag)
- Cause: Profile sourcing required only for commands needing user env (uv, pc-switcher path lookup); but flag defaults to False, so overhead only incurred when explicitly requested
- Improvement path: (1) Profile commands needing login shell explicitly (install_on_target, version checks); (2) Batch related commands to amortize startup cost; (3) Cache user environment from first login check instead of re-sourcing

### Disk Space Check Polling
- Problem: DiskSpaceMonitorJob polls disk space every N seconds (configurable); may miss rapid depletion or waste time on fast syncs
- Files: `src/pcswitcher/jobs/disk_space_monitor.py:200-240`
- Cause: Polling is simpler than inotify-based monitoring but introduces lag
- Improvement path: (1) Adjust check_interval per job expected duration; (2) Hook disk writes to trigger checks on-demand; (3) Integrate with rsync --progress for better coupling

## Fragile Areas

### Config Validation Spread Across Multiple Modules
- Files: `src/pcswitcher/config.py` (schema validation), `src/pcswitcher/jobs/disk_space_monitor.py:62-85` (threshold parsing), individual Job CONFIG_SCHEMA definitions
- Why fragile: Schema validation is split between Jsonschema (config.py) and semantic validation (disk_space_monitor threshold format, job-specific rules); easy to miss validation or create inconsistent error messages
- Safe modification: (1) Centralize all validation in config.py; (2) Require all jobs to register schemas; (3) Add integration test that loads every possible bad config and verifies error message quality

### Job Discovery and Instantiation at Runtime
- Files: `src/pcswitcher/orchestrator.py:400-450` (discover_and_validate_jobs), `src/pcswitcher/jobs/` (dynamic import)
- Why fragile: Jobs are discovered by importing config["type"] as Python module; failure at runtime if job class missing, misnamed, or schema incompatible
- Safe modification: (1) Validate job names and schemas at config load time; (2) Add registry of known jobs; (3) Fail early with clear error if job not found

### Config Sync Decision Tree
- Files: `src/pcswitcher/config_sync.py:151-235` (three-way branching: no config, configs match, configs differ)
- Why fragile: Multiple code paths with similar logic (auto_accept, dry_run, user prompts); easy to miss a branch when adding features
- Safe modification: (1) Extract decision tree to table-driven logic; (2) Add parametric tests covering all branch combinations; (3) Clarify what "config differs" means (whitespace, semantic content, etc.)

### SSH Connection Semaphore
- Files: `src/pcswitcher/connection.py:41` (session semaphore with max_sessions=10)
- Why fragile: Hard limit on concurrent SSH sessions; if a job spawns many long-lived processes, later jobs block; no logging of semaphore wait time
- Safe modification: (1) Make max_sessions configurable; (2) Log when tasks wait on semaphore; (3) Add metrics for session utilization

## Scaling Limits

### Single SSH Connection Multiplexing
- Current capacity: 10 concurrent SSH sessions (semaphore limit in Connection)
- Limit: Jobs spawning >10 concurrent operations will queue and block; no graceful degradation
- Scaling path: (1) Increase max_sessions for parallel-capable hardware; (2) Pool multiple SSH connections; (3) Implement job-level semaphores to prevent runaway spawning

### Btrfs Snapshot Storage
- Current capacity: Snapshots stored in `/.snapshots/pc-switcher/` on source and target
- Limit: Unbounded growth if cleanup not run; large snapshots (>50GiB) consume disk space quickly
- Scaling path: (1) Implement automatic cleanup per `cleanup_older_than` config; (2) Add size-based retention; (3) Warn if snapshot directory grows >X% of filesystem

### Log File Storage
- Current capacity: Logs written to `~/.local/share/pc-switcher/logs/`
- Limit: No rotation or cleanup; if syncs run frequently, logs grow unbounded
- Scaling path: (1) Implement log rotation (e.g., keep last 30 days); (2) Add compaction for older logs; (3) Document cleanup procedure

## Dependencies at Risk

### Version Class Complexity
- Risk: `src/pcswitcher/version.py` (713 lines) handles both PEP 440 and SemVer formats with complex regex and version comparison; many edge cases (dev versions, local builds, epochs)
- Impact: Version comparison bugs could block installations or suggest wrong upgrade path; hard to test all combinations
- Migration plan: (1) Add comprehensive property-based tests for version ordering; (2) Test all edge cases from real released versions; (3) Consider simplifying to single version format if SemVer proves sufficient

### asyncssh Keepalive Defaults
- Risk: Hardcoded keepalive_interval=15s, keepalive_count_max=3 in Connection; may be too aggressive for high-latency networks or too lenient for fast failures
- Impact: Slow to detect dead connections on WAN; potential false timeouts on congested networks
- Migration plan: (1) Make keepalive params configurable; (2) Add metrics for keepalive activity; (3) Test with realistic network conditions (VPN, Tailscale, high latency)

## Missing Critical Features

### No Rollback Capability
- Problem: Pre- and post-sync snapshots are created but not exposed to user; no `pc-switcher rollback` command
- Blocks: Phase 7 and full reliability story; users must manually `btrfs subvolume snapshot` or use external tools
- Impact: If sync fails or causes issues, recovery is manual and error-prone

### No Conflict Detection
- Problem: No detection of concurrent machine use (both machines modified same files) or incompatible package versions
- Blocks: Phases 2-6; phase 1 assumes single-user alternating workflow
- Impact: Syncs could silently overwrite recent changes on target if user worked on target without syncing back first

### No Partial Sync
- Problem: All configured jobs run every sync; no way to skip categories (e.g., "just sync home, skip packages")
- Blocks: User feedback expectation; some phases may allow incremental sync
- Impact: Syncs slow; no fine-grained control

## Test Coverage Gaps

### Config Sync Error Paths
- What's not tested: Scenarios where SFTP fails mid-transfer, target home directory query fails, config file is unreadable on source
- Files: `src/pcswitcher/config_sync.py:291-316` (copy_config_to_target)
- Risk: Silent failures or misleading error messages if copy fails
- Priority: High

### Lock Acquisition Failures
- What's not tested: What happens when flock fails due to permission error, network timeout, or unexpected signal
- Files: `src/pcswitcher/lock.py:145-156` (start_persistent_remote_lock)
- Risk: Confusing "already in sync" error hides the real problem
- Priority: High

### Disk Space Preflight Edge Cases
- What's not tested: Mount point doesn't exist, df returns unexpected format, threshold parsing with unusual units (MiB vs MB)
- Files: `src/pcswitcher/jobs/disk_space_monitor.py`, `src/pcswitcher/disk.py`
- Risk: Preflight passes unexpectedly or fails with unclear error
- Priority: Medium

### Interrupt Handling During Critical Phases
- What's not tested: SIGINT during snapshot creation, lock release, or SSH session teardown
- Files: `src/pcswitcher/cli.py:280-332` (interrupt handler), `src/pcswitcher/orchestrator.py:282-298` (CancelledError handling)
- Risk: Dangling locks or incomplete snapshots if interrupt lands at wrong time
- Priority: Medium

### Job Validation Failures
- What's not tested: Job validation returning errors; does orchestrator halt early and cleanly or try to continue?
- Files: `src/pcswitcher/orchestrator.py:237` (discover_and_validate_jobs)
- Risk: Failed validation may not trigger cleanup or proper error reporting
- Priority: Medium

---

*Concerns audit: 2026-06-29*
