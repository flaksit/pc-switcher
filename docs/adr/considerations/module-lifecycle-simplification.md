# Module Lifecycle Simplification: From Three-Phase to Single-Execute

**Date**: 2025-11-26
**Status**: Architectural Decision Documented
**Context**: Foundation Infrastructure (001-foundation)

## Summary

The module interface was simplified from a three-phase lifecycle (`pre_sync()` → `sync()` → `post_sync()`) to a single `execute()` method. This change follows YAGNI (You Aren't Gonna Need It) and Deliberate Simplicity principles.

## Problem Statement

The original design had modules implement three lifecycle methods:
- `validate()` - Pre-execution validation
- `pre_sync()` - Preparation phase
- `sync()` - Main work
- `post_sync()` - Finalization phase
- `abort(timeout)` - Cleanup on interruption

However, modules execute **sequentially**, not in coordinated phases:
```
Module1: pre_sync → sync → post_sync
Module2: pre_sync → sync → post_sync
Module3: pre_sync → sync → post_sync
```

This meant the three-phase structure provided **no orchestration benefit**—it was just internal structure for each module.

## Specific Problem: BtrfsSnapshot Timing

The three-phase lifecycle created a critical problem for snapshot infrastructure:

**Original approach (snapshots as first module)**:
- BtrfsSnapshotsModule would be the first module in config
- Its lifecycle: `pre_sync()` (create pre-sync snapshots) → `sync()` → `post_sync()` (create post-sync snapshots)
- Problem: `post_sync()` runs immediately after the module's own `sync()`, **before** other modules execute
- Result: Post-sync snapshots captured state BEFORE actual sync work happened

**Why two separate modules wouldn't solve it**:
You could split into `BtrfsPreSyncModule` and `BtrfsPostSyncModule`, but:
1. Tight coupling: They need to share session ID, subvolume list, configuration
2. User configuration risk: Users could disable one, reorder them, or add modules between them incorrectly
3. Enforcement complexity: Need special validation to ensure both are present, ordered correctly, and enabled
4. Conceptual mismatch: Snapshots aren't a "sync operation"—they're safety infrastructure that wraps operations
5. Code duplication: Both would need similar logging, progress, abort, RemoteExecutor patterns

## Solution

### 1. Simplified Module Lifecycle

**New Base Class: `Module`**
```python
class Module(ABC):
    @abstractmethod
    def validate(self) -> list[str]:
        """Pre-execution validation"""

    @abstractmethod
    def execute(self) -> None:
        """Execute the module's operation"""

    @abstractmethod
    def abort(self, timeout: float) -> None:
        """Stop gracefully"""
```

**Benefits**:
- Simpler contract (one execution method instead of three)
- More flexible (modules structure work internally as needed)
- Easier to understand and implement
- Still allows orchestrator to control execution order

### 2. Infrastructure vs. User-Configurable Modules

**Conceptual Hierarchy**:
```
Module (base abstraction)
├─ SyncModule (user-configurable, in config.yaml)
│   ├─ PackagesModule
│   ├─ DockerModule
│   └─ ...
│
└─ Infrastructure modules (orchestrator-managed, hardcoded)
    ├─ BtrfsSnapshotModule (sequential execution)
    └─ DiskSpaceMonitorModule (parallel execution)
```

**SyncModule**: Marker subclass for user-configurable modules in `config.yaml` `sync_modules` section.

**BtrfsSnapshotModule**: Inherits directly from `Module`, managed by orchestrator:
- Instantiated **twice** with `phase` parameter: `phase="pre"` and `phase="post"`
- Executed at specific points: before and after all SyncModules
- Cannot be disabled or reordered by users
- Gets all Module infrastructure (logging, progress, abort, RemoteExecutor) for free

**DiskSpaceMonitorModule**: Inherits directly from `Module`, runs in parallel:
- Instantiated once, runs in separate thread/task throughout entire sync
- Continuously monitors disk space at configured intervals
- Raises `DiskSpaceError` if space critically low, triggering abort of all modules
- Gets all Module infrastructure (logging, progress, abort, RemoteExecutor) for free

### 3. Execution Flow

```
1. Validate all modules (disk_space_monitor, pre_snapshot, sync modules, post_snapshot)
2. Start parallel: DiskSpaceMonitorModule.execute() ║════════════════════║
3. Execute sequential: BtrfsSnapshotModule(phase="pre").execute()
4. Execute sequential: SyncModule1.execute()
5. Execute sequential: SyncModule2.execute()
6. Execute sequential: BtrfsSnapshotModule(phase="post").execute()
7. Stop parallel: DiskSpaceMonitorModule.abort()
```

## Advantages

### DRY (Don't Repeat Yourself)
- All modules (sync and infrastructure, sequential and parallel) share same base infrastructure
- No duplication of logging, progress reporting, abort handling, RemoteExecutor patterns
- If Module infrastructure improves, all modules (snapshots, monitoring, sync) benefit automatically
- Disk space monitoring gets proper logging, progress, abort just by inheriting from Module

### Conceptual Clarity
- "Module" = any operation needing infrastructure (base abstraction)
- "SyncModule" = user-configurable sync operations (policy)
- Infrastructure modules are just modules that orchestrator manages
- Parallel vs sequential execution is an orchestrator concern, not a Module concern

### Safety Preserved
- Snapshots hardcoded in orchestrator, users can't disable/reorder
- Still bracket all operations (before/after)
- Still get proper timing (post-snapshots after ALL work completes)

### Simplicity
- One execution method per module
- No artificial three-phase structure
- Modules structure their own work internally
- YAGNI: Don't add complexity until you need it

## Rationale for "Two Modules" Alternative Not Chosen

While splitting BtrfsSnapshot into two separate modules (Pre and Post) would solve the timing issue, it was rejected because:

1. **Artificial split**: The pre and post operations are logically a single safety mechanism (create snapshot pair)
2. **Configuration exposure risk**: Making them separate config entries exposes users to misconfiguration
3. **Enforcement burden**: Requires special validation code to ensure both are present, first/last, and enabled together
4. **Missed DRY opportunity**: Infrastructure operations need same capabilities as sync modules—why duplicate the patterns?

The chosen approach (single module instantiated twice by orchestrator) maintains logical cohesion while achieving correct timing.

## Configuration Examples

**Before** (hypothetical—snapshots could never work correctly as first module):
```yaml
sync_modules:
  btrfs_snapshots: true  # Would create post-snapshots too early!
  packages: true
  docker: true
```

**After**:
```yaml
sync_modules:
  packages: true  # User-configurable only
  docker: true

# Infrastructure modules (separate sections, cannot be disabled)
btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"

disk_space_monitor:
  check_interval: 1.0  # seconds
  min_free: "10GB"
  paths:
    - "/"
    - "/home"
```

## Migration Path

This is a design-phase decision before implementation was completed, so migration involves:
1. Update module interface contracts (module-interface.py)
2. Update orchestrator protocol documentation
3. Update data model documentation
4. Implement: Orchestrator hardcodes BtrfsSnapshotModule instantiation
5. Implement: Simplified Module ABC with single execute() method
6. Update all module implementations to use execute() instead of three-phase lifecycle

## Related Documents

- `specs/001-foundation/contracts/module-interface.py` - Updated interface definition
- `specs/001-foundation/contracts/orchestrator-module-protocol.md` - Updated protocol
- `specs/001-foundation/data-model.md` - Updated entity definitions
- `specs/001-foundation/spec.md` - Functional requirements (will need update)

## Principles Aligned

- **Deliberate Simplicity**: Removed unnecessary complexity (three-phase lifecycle)
- **YAGNI**: Don't add structure until you need it
- **DRY**: Reuse infrastructure across all modules (sync and infrastructure)
- **Reliability Without Compromise**: Snapshots still bracket operations correctly
