# Orchestrator-Module Protocol

**Purpose**: Define the precise interaction contract between the orchestrator and sync modules.

**Requirements**: FR-001 through FR-002, User Story 1

**Key Architectural Decisions**:
- Simple module lifecycle: validate() → execute() → abort() (removed unnecessary pre_sync/sync/post_sync)
- SyncModules defined in config (no auto-discovery)
- Infrastructure modules hardcoded by orchestrator:
  - BtrfsSnapshotModule: instantiated twice (phase="pre"/"post"), executes sequentially
  - DiskSpaceMonitorModule: runs in parallel throughout entire sync operation
- SyncModules execute sequentially in config order (no dependency resolution)
- Exception-based error handling (modules raise, not log CRITICAL)
- Orchestrator tracks ERROR logs for final state determination
- abort(timeout) called on currently-running module and parallel monitoring module
- CLEANUP state before terminal states
- Btrfs verification: / is btrfs, subvolumes exist in top-level

## Lifecycle Sequence

The orchestrator executes modules in a strict sequence:

```text
┌──────────────────────────────────────────────────────────────┐
│ 1. INITIALIZATION PHASE (State: INITIALIZING)               │
├──────────────────────────────────────────────────────────────┤
│ - Load config from ~/.config/pc-switcher/config.yaml         │
│ - Validate config structure and syntax                       │
│ - Check lock file ($XDG_RUNTIME_DIR/pc-switcher/*.lock)      │
│   - If stale: warn user, ask confirmation to proceed         │
│   - If active: display PID, error and abort                  │
│ - Create lock file                                            │
│ - Get enabled SyncModules from sync_modules config (in order)│
│ - Establish SSH connection to target                         │
│ - Check/install pc-switcher version on target                │
│ - Verify btrfs on both source and target:                    │
│   - Check / is btrfs filesystem                              │
│   - Check configured subvolumes exist in top-level           │
│     (visible in "btrfs subvolume list /")                    │
│ - Create SyncSession with session ID                         │
│ - Instantiate infrastructure modules:                        │
│   - BtrfsSnapshotModule twice (phase="pre"/"post")           │
│   - DiskSpaceMonitorModule (for parallel execution)          │
│ - Instantiate SyncModules with validated config + RemoteExec │
│ - Inject log() and emit_progress() methods into all modules  │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 2. VALIDATION PHASE (State: VALIDATING)                     │
├──────────────────────────────────────────────────────────────┤
│ For each module (in config order):                           │
│   errors = module.validate()                                 │
│   if errors:                                                  │
│     collect all errors                                        │
│ if any_errors:                                                │
│   display all errors in terminal                             │
│   ABORT before any state changes                             │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 3. EXECUTING PHASE (State: EXECUTING)                       │
├──────────────────────────────────────────────────────────────┤
│ 0. Start DiskSpaceMonitorModule in parallel (thread/async):  │
│   disk_space_monitor = start_parallel(disk_space_mon.exec()) │
│   # Runs continuously, monitoring disk space every interval  │
│   # Raises DiskSpaceError if space critically low            │
│                                                               │
│ 1. Execute BtrfsSnapshotModule(phase="pre"):                 │
│   try:                                                        │
│     pre_snapshot_module.execute()  # creates pre-sync snaps  │
│   except Exception as e:                                      │
│     log exception as CRITICAL, goto CLEANUP PHASE            │
│                                                               │
│ 2. For each SyncModule (in config order):                    │
│   try:                                                        │
│     module.execute()                                          │
│     mark module as SUCCESS                                    │
│   except SyncError as e:                                      │
│     log exception as CRITICAL (orchestrator does this)        │
│     session.abort_requested = True                           │
│     goto CLEANUP PHASE                                        │
│   except Exception as e:                                      │
│     log exception as CRITICAL                                 │
│     session.abort_requested = True                           │
│     goto CLEANUP PHASE                                        │
│   except DiskSpaceError as e:  # From disk space monitor     │
│     log exception as CRITICAL                                 │
│     session.abort_requested = True                           │
│     goto CLEANUP PHASE                                        │
│   if user pressed Ctrl+C:                                     │
│     log "Sync interrupted by user" at WARNING                │
│     session.abort_requested = True                           │
│     goto CLEANUP PHASE                                        │
│                                                               │
│ 3. Execute BtrfsSnapshotModule(phase="post"):                │
│   try:                                                        │
│     post_snapshot_module.execute()  # creates post-sync snaps│
│   except Exception as e:                                      │
│     log exception as CRITICAL, goto CLEANUP PHASE            │
│                                                               │
│ if all modules completed and NO ERROR logs:                  │
│   stop disk_space_monitor (call abort), goto COMPLETED       │
│ if all modules completed but ERROR logs exist:               │
│   stop disk_space_monitor (call abort), goto CLEANUP→FAILED  │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 4. CLEANUP PHASE (State: CLEANUP)                           │
├──────────────────────────────────────────────────────────────┤
│ Stop parallel monitoring:                                    │
│   try:                                                        │
│     disk_space_monitor.abort(timeout=2.0)                    │
│     wait for disk_space_monitor thread/task to finish        │
│   except Exception as e:                                      │
│     log ERROR (abort is best-effort)                         │
│                                                               │
│ If currently-running module exists:                          │
│   try:                                                        │
│     current_module.abort(timeout=5.0)                        │
│   except Exception as e:                                      │
│     log ERROR (abort is best-effort)                         │
│                                                               │
│ Do NOT call abort() on completed modules                     │
│   (abort = stop processes, not undo work)                    │
│                                                               │
│ Close SSH connection                                          │
│ Release sync lock                                             │
│ Log final session summary                                    │
│                                                               │
│ Determine final state:                                       │
│   if user_interrupt (Ctrl+C): → ABORTED                     │
│   elif exception or session.has_errors: → FAILED            │
│   else: should not reach here (completed goes direct)        │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 5. TERMINAL STATES                                           │
├──────────────────────────────────────────────────────────────┤
│ - COMPLETED: All modules succeeded, no ERROR logs            │
│ - ABORTED: User interrupt (Ctrl+C)                          │
│ - FAILED: Exception raised or ERROR logs emitted            │
└──────────────────────────────────────────────────────────────┘
```

## Module Instantiation and Loading

**No Auto-Discovery**: Modules are NOT auto-discovered from the codebase.

**SyncModule Loading**:
1. Read `sync_modules` section from config (dict with module names as keys, bool as values)
2. SyncModules execute in the order they appear in the config YAML
3. For each entry where value is `true`:
   - Import the module class (e.g., `from pcswitcher.modules.packages import PackagesModule`)
   - Get config schema via `module_class.get_config_schema()`
   - Validate module-specific config section against schema
   - Create RemoteExecutor wrapper around TargetConnection
   - Instantiate: `module = ModuleClass(validated_config, remote_executor)`
   - Inject `log()` and `emit_progress()` methods into module instance

**Infrastructure Module Loading**:

1. **BtrfsSnapshotModule** (sequential execution):
   - Hardcoded by orchestrator (not in sync_modules config)
   - Instantiated twice with different phase parameters:
     - `pre_snapshot = BtrfsSnapshotModule(btrfs_config, remote, phase="pre")`
     - `post_snapshot = BtrfsSnapshotModule(btrfs_config, remote, phase="post")`
   - Config comes from separate `btrfs_snapshots` section (not sync_modules)
   - Cannot be disabled by user

2. **DiskSpaceMonitorModule** (parallel execution):
   - Hardcoded by orchestrator (not in sync_modules config)
   - Instantiated once: `disk_space_monitor = DiskSpaceMonitorModule(disk_config, remote)`
   - Config comes from separate `disk_space_monitor` section
   - Runs in parallel thread/task throughout entire sync operation
   - Cannot be disabled by user

**Example config**:
```yaml
sync_modules:
  user_data: true
  packages: true
  docker: false  # Disabled
  k3s: false

btrfs_snapshots:  # Infrastructure module config (separate from sync_modules)
  subvolumes:
    - "@"
    - "@home"

disk_space_monitor:  # Infrastructure module config (separate from sync_modules)
  check_interval: 1.0  # seconds
  min_free: "10GB"  # or "5%" for percentage
  paths:
    - "/"
    - "/home"
```

Execution order (parallel ║ for disk space monitor):
```
DiskSpaceMonitor.execute() ║════════════════════════════════════║
BtrfsSnapshot(pre).execute() → user_data.execute() → packages.execute() → BtrfsSnapshot(post).execute()
```

## Configuration Injection

**Validation**:
1. For each enabled module:
   - Get schema from `ModuleClass.get_config_schema()` (class method or instance)
   - Extract config section from user config (e.g., `config['btrfs_snapshots']`)
   - Validate section against schema (using jsonschema library or manual validation)
   - Apply defaults from schema for missing values
   - If validation fails → log ERROR with details and abort

**Injection**:
```python
# Create RemoteExecutor wrapper
remote = RemoteExecutor(target_connection)

# Instantiate module with validated config
module = ModuleClass(validated_config, remote)

# Inject logging method
module.log = lambda level, msg, **ctx: orchestrator.log_for_module(module.name, level, msg, **ctx)

# Inject progress method
module.emit_progress = lambda pct, item, eta: orchestrator.handle_progress(module.name, pct, item, eta)
```

## RemoteExecutor Interface

Modules receive a `RemoteExecutor` instance for target communication:

```python
class RemoteExecutor:
    def run(
        self,
        command: str,
        sudo: bool = False,
        timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Execute command on target, return CompletedProcess"""

    def send_file_to_target(self, local: Path, remote: Path) -> None:
        """Upload file from source to target"""

    def get_hostname(self) -> str:
        """Get target hostname (actual name, not 'target')"""
```

**Module Usage**:
```python
def sync(self):
    # Run command on target
    result = self.remote.run("btrfs subvolume list /", sudo=True)
    if result.returncode != 0:
        raise SyncError(f"Failed to list subvolumes: {result.stderr}")

    # Send file to target
    self.remote.send_file_to_target(Path("local.conf"), Path("/etc/app.conf"))

    # Get target hostname for logging
    target_host = self.remote.get_hostname()
    self.log(LogLevel.INFO, f"Syncing to {target_host}")
```

## Progress Reporting Protocol

**Module Side**:
```python
# During sync() execution - report as fraction of TOTAL module work
self.emit_progress(
    percentage=0.5,  # 50% of ALL module work (float 0.0-1.0)
    item="Copying /home/user/documents/file.txt",
    eta=timedelta(seconds=120)  # Optional
)

# percentage can be None if unknown
self.emit_progress(percentage=None, item="Processing...")
```

**Orchestrator Side**:
1. Orchestrator injects `emit_progress` method into module after instantiation
2. Method forwards to orchestrator's progress handler
3. Orchestrator:
   - Logs progress at FULL level: `[FULL] [module] Progress: 50% - Copying file.txt`
   - Updates terminal UI with progress bar
   - Stores last progress for each module

**Requirements**:
- Modules should emit progress at reasonable intervals (every 1-10 seconds)
- Percentage is float 0.0-1.0 (or None if unknown)
- Percentage represents progress of ENTIRE module (validate + pre + sync + post)
- current_item should be concise (<100 chars for terminal)
- eta can be None if unknown

## Logging Protocol

**Module Side**:
```python
# Modules use injected log() method
self.log(LogLevel.INFO, "Starting operation", file_count=42)
self.log(LogLevel.ERROR, "File copy failed", path="/some/file")
self.log(LogLevel.WARNING, "Unexpected condition", details="...")

# Modules do NOT log CRITICAL - they raise exceptions instead
# DO NOT: self.log(LogLevel.CRITICAL, "Fatal error")
# DO: raise SyncError("Fatal error occurred")
```

**Orchestrator Side**:
1. Configure structlog with dual output: file + terminal
2. Inject log method into module: `module.log = orchestrator.log_for_module(module.name, ...)`
3. Install custom processor to track ERROR events:
   ```python
   def track_errors(logger, log_method, event_dict):
       if event_dict['level'] >= 40:  # ERROR or CRITICAL
           session.has_errors = True
       return event_dict
   ```
4. Module exceptions are caught and logged by orchestrator as CRITICAL

**Log Levels** (FR-002):
- DEBUG (10): Verbose diagnostics (command outputs, state dumps)
- FULL (15): File-level operations (e.g., "Copying /home/user/file.txt")
- INFO (20): High-level operations (e.g., "Module started", "Module completed")
- WARNING (30): Unexpected but non-failing (e.g., "Config uses deprecated format")
- ERROR (40): Recoverable errors (e.g., "File copy failed, skipping")
- CRITICAL (50): Unrecoverable errors (only logged by orchestrator when catching exceptions)

## Error Handling

**Module Exceptions**:
- Modules raise `SyncError` (or subclasses) for unrecoverable failures
- Orchestrator catches ALL exceptions during module execution
- On exception:
  1. Log exception as CRITICAL with full traceback
  2. Set `session.abort_requested = True`
  3. Enter CLEANUP phase
  4. Call `current_module.abort(timeout)` if it was executing
  5. Determine final state: ABORTED (if Ctrl+C) or FAILED (if exception)

**Validation Errors**:
- `validate()` returns `list[str]` (error messages)
- Empty list = valid
- Non-empty list = errors (don't raise exception)
- Orchestrator collects all validation errors, displays them, then aborts

**ERROR Log Tracking**:
- Orchestrator tracks if any ERROR-level logs were emitted
- Uses custom structlog processor to set `session.has_errors = True`
- Final session state:
  - All modules completed + no ERROR logs → COMPLETED
  - All modules completed + ERROR logs present → CLEANUP → FAILED

## Abort Handling

**Abort Sources**:
1. **Module exception**: Module raises SyncError during lifecycle method
2. **Unhandled exception**: Module raises any exception
3. **User interrupt**: Ctrl+C (SIGINT)

**Orchestrator Response**:
1. Set `session.abort_requested = True`
2. Enter CLEANUP phase (change session.state to CLEANUP)
3. If module is currently executing:
   - Call `module.abort(timeout=5.0)`
   - Wait for abort to complete (up to timeout)
4. Do NOT call abort() on completed modules
   - Semantics: abort = stop running processes, NOT undo work
   - Rollback is a separate manual operation via snapshots
5. Close SSH connection
6. Release sync lock
7. **Offer rollback if pre-sync snapshots exist** (see Rollback Offer Workflow below)
8. Determine final state:
   - User interrupt (Ctrl+C) → ABORTED
   - Exception or ERROR logs → FAILED

## Rollback Offer Workflow

When a sync fails with a CRITICAL error and pre-sync snapshots exist, the orchestrator offers to roll back to the pre-sync state.

**Requirements**: User Story 3, Scenario 6

**When to Offer Rollback**:
- A module raised an exception (logged as CRITICAL by orchestrator)
- Pre-sync snapshots were successfully created before the failure
- Cleanup phase has completed (module abort() called if needed, SSH closed, lock released)

**Workflow**:

1. **Check for Pre-Sync Snapshots**:
   - Query btrfs for snapshots matching pattern `*-presync-<timestamp>-<session-id>`
   - If no pre-sync snapshots found → skip rollback offer, proceed to final state

2. **Display Rollback Prompt**:
   ```text
   CRITICAL ERROR: Sync failed during <module-name> module.
   Pre-sync snapshots are available for rollback.

   Available snapshots:
   - @-presync-20251115T120000Z-abc12345 (root filesystem)
   - @home-presync-20251115T120000Z-abc12345 (home directory)

   Would you like to restore these snapshots to undo changes? [y/N]
   ```

3. **User Response**:
   - **If user enters 'y' or 'yes'** (case-insensitive):
     - Execute rollback procedure (see below)
   - **If user enters 'N', 'no', or presses Enter** (default):
     - Log INFO: "User declined rollback. Pre-sync snapshots retained at <paths>"
     - Proceed to final state (FAILED)
   - **If user presses Ctrl+C**:
     - Log WARNING: "Rollback prompt interrupted. Pre-sync snapshots retained"
     - Proceed to final state (FAILED)

**Rollback Execution Procedure**:

1. **Verify Snapshots Exist**:
   - Re-check that all pre-sync snapshots still exist
   - If any are missing → ERROR "Cannot proceed with rollback, some snapshots missing" and abort rollback

2. **Perform Rollback**:
   - For each subvolume with a pre-sync snapshot:
     - Delete current subvolume: `btrfs subvolume delete /<subvolume>`
     - Restore from snapshot: `btrfs subvolume snapshot <snapshot-path> /<subvolume>`
     - Make writable (snapshots are read-only): handled by snapshot restoration
   - Log each rollback operation at INFO level

3. **Verify Rollback**:
   - Check that all subvolumes were restored successfully
   - If any restoration failed → CRITICAL "Partial rollback occurred, manual intervention required"

4. **Cleanup**:
   - Keep pre-sync snapshots (do not delete - user may need them)
   - Delete any post-sync snapshots if they were created
   - Log INFO: "Rollback completed. System restored to pre-sync state"

5. **Final State**:
   - Set session state to FAILED (rollback doesn't change the fact that sync failed)
   - Log session summary including rollback status
   - Exit with code 1

**Important Notes**:
- Rollback is ONLY offered after CRITICAL failures, not after user interrupt (Ctrl+C)
- Rollback requires user confirmation - it is NEVER automatic
- If user declines rollback, pre-sync snapshots are retained for manual recovery
- Rollback operates on the source machine where the orchestrator runs
- Target machine rollback is NOT automatic - user must manually restore target if needed

**Module abort() Requirements**:
- Must stop running processes/threads
- Must release locks and file handles
- Must be idempotent (can be called multiple times)
- Must handle partial state gracefully
- Should NOT raise exceptions (best-effort)
- Should complete within timeout seconds

**Example abort() implementation**:
```python
def abort(self, timeout: float) -> None:
    if self.subprocess:
        try:
            self.subprocess.terminate()
            self.subprocess.wait(timeout=min(timeout, 2.0))
        except Exception:
            pass  # Best-effort

    if self.file_handle:
        try:
            self.file_handle.close()
        except Exception:
            pass
```

## Concurrency and Locking

**Lock Mechanism** (FR-002):
1. **Lock location**:
   - Primary: `$XDG_RUNTIME_DIR/pc-switcher/pc-switcher.lock`
   - Fallback: `/var/lock/pc-switcher.lock` (if XDG_RUNTIME_DIR not set)

2. **Lock file format**:
   ```json
   {
     "pid": 12345,
     "timestamp": "2025-11-15T12:00:00Z",
     "session_id": "abc12345"
   }
   ```

3. **Lock acquisition**:
   - Check if lock file exists
   - If exists:
     - Read PID from lock file
     - Check if process is running: `ps -p <PID>`
     - If running: ERROR "Another sync is in progress (PID: 12345)" and abort
     - If stale (process not running):
       - WARN user: "Found stale lock file from previous sync"
       - ASK confirmation: "Previous sync may have crashed. Proceed? [y/N]"
       - If confirmed: delete stale lock, create new lock
       - If declined: abort
   - If not exists: create lock file

4. **Lock release**:
   - Always release in CLEANUP phase (even on abort/error)
   - Delete lock file
   - If delete fails: log ERROR (best-effort)

**Module Execution**:
- Modules execute **sequentially** (one at a time, in config order)
- No parallel module execution (for simplicity and safety)
- Future optimization: Allow parallel execution within a module (module's responsibility)

## Btrfs Verification

**Pre-Sync Checks** (during INITIALIZATION):

1. **Root filesystem is btrfs**:
   ```bash
   # On both source and target
   stat -f -c %T /
   # Must output: btrfs
   ```
   If not btrfs → ERROR "Root filesystem is not btrfs" and abort

2. **Configured subvolumes exist in top-level**:
   ```bash
   # Get list of top-level subvolumes
   btrfs subvolume list /
   ```
   For each subvolume in config (e.g., "@", "@home", "@root"):
   - Check it appears in the output
   - If missing → ERROR "Configured subvolume '@home' not found in top-level" and abort

**Example**:
```yaml
btrfs_snapshots:
  subvolumes:
    - "@"      # Must exist in "btrfs subvolume list /" output
    - "@home"  # Must exist
    - "@root"  # Must exist
```

## SSH Connection Management

**Lifecycle**:
1. Establish connection during INITIALIZATION
2. Reuse single connection for all operations (ControlMaster)
3. Close connection during CLEANUP

**Module Access**:
- Modules do NOT access connection directly
- Modules receive `RemoteExecutor` wrapper
- RemoteExecutor methods:
  - `run(command, sudo, timeout) -> CompletedProcess`
  - `send_file_to_target(local, remote)`
  - `get_hostname() -> str`

**Error Handling**:
- Connection loss during sync → log CRITICAL, enter CLEANUP, FAILED
- Failed commands → module decides (raise SyncError or log ERROR)

## Example: Complete Sync Flow

1. User runs: `pc-switcher sync workstation`
2. Orchestrator:
   - Loads config
   - Checks lock (none exists, creates lock)
   - Connects to "workstation" via SSH
   - Checks/installs matching pc-switcher version on target
   - Verifies btrfs on both machines
   - Creates session (ID: a1b2c3d4)
   - Instantiates infrastructure modules:
     - BtrfsSnapshotModule twice (phase="pre" and phase="post")
     - DiskSpaceMonitorModule
   - Loads SyncModules from config: [user_data, packages]
   - Instantiates each with config + RemoteExecutor
3. VALIDATING:
   - Calls validate() on all modules (disk_space_monitor, pre_snapshot, user_data, packages, post_snapshot)
   - All return empty lists (no errors)
4. EXECUTING:
   - Start disk_space_monitor.execute() in parallel thread/task
   - pre_snapshot.execute() → creates pre-sync snapshots on both machines
   - user_data.execute() → syncs /home, emits progress
   - packages.execute() → syncs packages, emits progress
   - post_snapshot.execute() → creates post-sync snapshots on both machines
   - disk_space_monitor runs throughout, checking space every 1 second
5. All complete, no ERROR logs
6. Stop disk_space_monitor (call abort), go directly to COMPLETED
7. Log session summary
8. Release lock
9. Exit with code 0
