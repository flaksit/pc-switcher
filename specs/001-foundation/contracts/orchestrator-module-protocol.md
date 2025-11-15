# Orchestrator-Module Protocol

**Purpose**: Define the precise interaction contract between the orchestrator and sync modules.

**Requirements**: FR-001 through FR-005, User Story 1

## Lifecycle Sequence

The orchestrator executes modules in a strict sequence:

```
┌──────────────────────────────────────────────────────────────┐
│ 1. INITIALIZATION PHASE                                      │
├──────────────────────────────────────────────────────────────┤
│ - Load config from ~/.config/pc-switcher/config.yaml         │
│ - Discover registered modules                                │
│ - Filter enabled modules (sync_modules config)               │
│ - Instantiate each module with validated config              │
│ - Topologically sort modules by dependencies                 │
│ - Establish SSH connection to target                         │
│ - Check/install pc-switcher version on target                │
│ - Create SyncSession                                          │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 2. VALIDATION PHASE                                          │
├──────────────────────────────────────────────────────────────┤
│ For each enabled module (in dependency order):               │
│   errors = module.validate()                                 │
│   if errors:                                                  │
│     collect and display all errors                           │
│ if any_errors:                                                │
│   ABORT before any state changes                             │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 3. PRE-SYNC PHASE                                            │
├──────────────────────────────────────────────────────────────┤
│ For each enabled module (in dependency order):               │
│   try:                                                        │
│     module.pre_sync()                                         │
│   except Exception as e:                                      │
│     log CRITICAL, call module.cleanup(), ABORT               │
│   if abort_requested:  # CRITICAL log detected               │
│     call module.cleanup(), ABORT                             │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 4. SYNC PHASE                                                │
├──────────────────────────────────────────────────────────────┤
│ For each enabled module (in dependency order):               │
│   try:                                                        │
│     module.sync()                                             │
│   except Exception as e:                                      │
│     log CRITICAL, call module.cleanup(), ABORT               │
│   if abort_requested:  # CRITICAL log or Ctrl+C              │
│     call module.cleanup(), ABORT                             │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 5. POST-SYNC PHASE                                           │
├──────────────────────────────────────────────────────────────┤
│ For each enabled module (in dependency order):               │
│   try:                                                        │
│     module.post_sync()                                        │
│   except Exception as e:                                      │
│     log CRITICAL, call module.cleanup(), ABORT               │
│   if abort_requested:                                         │
│     call module.cleanup(), ABORT                             │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ 6. CLEANUP PHASE                                             │
├──────────────────────────────────────────────────────────────┤
│ For each module (reverse order for cleanup):                 │
│   try:                                                        │
│     module.cleanup()                                          │
│   except Exception as e:                                      │
│     log ERROR (cleanup is best-effort, don't abort)          │
│ Close SSH connection                                          │
│ Release sync lock                                             │
│ Log final session summary                                    │
└──────────────────────────────────────────────────────────────┘
```

## Module Discovery and Registration

**Discovery**:
1. Orchestrator scans `pcswitcher.modules` package for `SyncModule` subclasses
2. Each module is registered by `name` property
3. Duplicate names → ERROR and abort

**Filtering**:
1. Load `sync_modules` section from config
2. For each registered module:
   - If `sync_modules[module.name]` exists, use that value (true/false)
   - If not in config, default to module's schema default (or false)
   - If module.required == True, ignore disable attempts (always enabled)

**Dependency Resolution**:
1. Build dependency graph from `module.dependencies`
2. Topological sort (Kahn's algorithm or similar)
3. Circular dependency → ERROR and abort
4. Unknown dependency → ERROR and abort

## Configuration Injection

**Validation**:
1. For each enabled module:
   - Get schema from `module.get_config_schema()`
   - Extract config section from user config (e.g., `config['btrfs_snapshots']`)
   - Validate section against schema (using jsonschema library or manual)
   - Apply defaults from schema for missing values
   - If validation fails → ERROR with details and abort

**Injection**:
1. Create module instance: `module = ModuleClass(validated_config, logger)`
2. Logger is pre-bound with context: `logger.bind(module=module.name, session_id=session.id)`

## Progress Reporting Protocol

**Module Side**:
```python
# During sync() execution:
self.emit_progress(
    percentage=50,
    current_item="Copying /home/user/documents/file.txt",
    eta=timedelta(seconds=120)
)
```

**Orchestrator Side**:
1. Inject `emit_progress` callback into module base class during initialization
2. Callback forwards to orchestrator's progress handler
3. Orchestrator:
   - Logs progress at FULL level
   - Updates terminal UI (if enabled)
   - Stores last progress for each module (for UI display)

**Requirements**:
- Modules should emit progress at reasonable intervals (every 1-10 seconds)
- Percentage must be 0-100
- current_item should be concise (<100 chars for terminal)
- eta can be None if unknown

## Logging Protocol

**Module Side**:
```python
# Option 1: Use log() wrapper
self.log(LogLevel.INFO, "Starting operation", file_count=42)

# Option 2: Use logger directly
self.logger.info("Starting operation", file_count=42)
```

**Orchestrator Side**:
1. Configure structlog with dual output: file + terminal
2. Inject bound logger into module: `logger.bind(module=module.name, session_id=session.id, hostname=hostname)`
3. Install custom processor to detect CRITICAL events:
   ```python
   def abort_on_critical(logger, log_method, event_dict):
       if event_dict['level'] >= 50:  # CRITICAL
           session.abort_requested = True
       return event_dict
   ```
4. Orchestrator checks `session.abort_requested` after each module operation

**Log Levels** (FR-019):
- DEBUG (10): Verbose diagnostics (command outputs, state dumps)
- FULL (15): File-level operations (e.g., "Copying /home/user/file.txt")
- INFO (20): High-level operations (e.g., "Module started", "Module completed")
- WARNING (30): Unexpected but non-failing (e.g., "Config uses deprecated format")
- ERROR (40): Recoverable errors (e.g., "File copy failed, skipping")
- CRITICAL (50): Unrecoverable errors (sync abort)

## Abort Handling

**Abort Sources**:
1. **CRITICAL log**: Any module (or orchestrator) logs at CRITICAL level
2. **Unhandled exception**: Module raises exception during lifecycle method
3. **User interrupt**: Ctrl+C (SIGINT)

**Orchestrator Response**:
1. Set `session.abort_requested = True`
2. If module is executing, wait for it to complete current method
3. Call `module.cleanup()` on currently-executing module
4. Skip remaining modules
5. Call `cleanup()` on all previously-executed modules (reverse order)
6. Close SSH connection
7. Release sync lock
8. Exit with code 130 (interrupted) or 1 (error)

**Module Requirements**:
- `cleanup()` must be idempotent (can be called multiple times)
- `cleanup()` must handle partial state (module may have failed mid-operation)
- `cleanup()` should not raise exceptions (best-effort)

## Error Handling Contract

**Module Exceptions**:
- Modules MAY raise exceptions during validate/pre_sync/sync/post_sync
- Orchestrator CATCHES all exceptions
- On exception:
  1. Log exception as CRITICAL with full traceback
  2. Call module.cleanup()
  3. Abort sync
  4. No further modules execute

**Validation Errors**:
- `validate()` returns `list[str]` (error messages)
- Empty list = valid
- Non-empty list = errors (don't raise exception)
- Orchestrator collects all validation errors, displays them, then aborts

**Logging Errors**:
- ERROR level: Log but continue (module can recover)
- CRITICAL level: Log and abort (unrecoverable)

## Concurrency and Locking

**Lock Mechanism** (FR-048):
1. Before INITIALIZATION, acquire lock: `/tmp/pc-switcher-sync.lock` (or XDG_RUNTIME_DIR)
2. Lock file contains PID of running process
3. If lock exists and PID is active → ERROR "Another sync is in progress"
4. If lock exists and PID is stale → Remove lock and proceed
5. Release lock in CLEANUP phase (even on abort/error)

**Module Execution**:
- Modules execute **sequentially** (one at a time)
- No parallel module execution (for simplicity and safety)
- Future optimization: Allow parallel execution within a module (module's responsibility)

## SSH Connection Management

**Lifecycle**:
1. Establish connection during INITIALIZATION
2. Reuse single connection for all operations (ControlMaster)
3. Close connection during CLEANUP

**Module Access**:
- Modules receive connection via orchestrator (not injected into constructor)
- Orchestrator provides methods:
  ```python
  orchestrator.run_on_target(command: str, sudo: bool = False) -> Result
  orchestrator.send_file(local: Path, remote: Path) -> None
  ```
- Modules don't manage connection directly (orchestrator responsibility)

**Error Handling**:
- Connection loss during sync → CRITICAL log and abort
- Failed commands → module decides (ERROR or CRITICAL based on severity)

## Example: Complete Module Implementation

See `contracts/module-interface.py` for `DummySuccessModule` reference implementation demonstrating all contract requirements.
