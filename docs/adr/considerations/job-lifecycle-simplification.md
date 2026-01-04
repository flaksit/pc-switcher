# Job Lifecycle Simplification: From Three-Phase to Single-Execute

**Date**: 2025-11-26
**Status**: Architectural Decision Documented
**Context**: Core Infrastructure (001-core)

## Summary

The job interface was simplified from a three-phase lifecycle (`pre_sync()` → `sync()` → `post_sync()`) to a single `execute()` method. This change follows YAGNI (You Aren't Gonna Need It) and Deliberate Simplicity principles.

## Problem Statement

The original design had jobs implement three lifecycle methods:
- `validate()` - Pre-execution validation
- `pre_sync()` - Preparation phase
- `sync()` - Main work
- `post_sync()` - Finalization phase
- `abort(timeout)` - Cleanup on interruption

However, jobs execute **sequentially**, not in coordinated phases:
```
Job1: pre_sync → sync → post_sync
Job2: pre_sync → sync → post_sync
Job3: pre_sync → sync → post_sync
```

This meant the three-phase structure provided **no orchestration benefit**—it was just internal structure for each job.

## Specific Problem: BtrfsSnapshot Timing

The three-phase lifecycle created a critical problem for snapshot infrastructure:

**Original approach (snapshots as first job)**:
- BtrfsSnapshotsJob would be the first job in config
- Its lifecycle: `pre_sync()` (create pre-sync snapshots) → `sync()` → `post_sync()` (create post-sync snapshots)
- Problem: `post_sync()` runs immediately after the job's own `sync()`, **before** other jobs execute
- Result: Post-sync snapshots captured state BEFORE actual sync work happened

**Why two separate jobs wouldn't solve it**:
You could split into `BtrfsPreSyncJob` and `BtrfsPostSyncJob`, but:
1. Tight coupling: They need to share session ID, subvolume list, configuration
2. User configuration risk: Users could disable one, reorder them, or add jobs between them incorrectly
3. Enforcement complexity: Need special validation to ensure both are present, ordered correctly, and enabled
4. Conceptual mismatch: Snapshots aren't a "sync operation"—they're safety infrastructure that wraps operations
5. Code duplication: Both would need similar logging, progress, abort, RemoteExecutor patterns

## Solution

### 1. Simplified Job Lifecycle

**New Base Class: `Job`**
```python
class Job(ABC):
    @abstractmethod
    def validate(self) -> list[str]:
        """Pre-execution validation"""

    @abstractmethod
    def execute(self) -> None:
        """Execute the job's operation"""
```

**Benefits**:
- Simpler contract (one execution method instead of three)
- More flexible (jobs structure work internally as needed)
- Easier to understand and implement
- Still allows orchestrator to control execution order

### 2. Infrastructure vs. User-Configurable Jobs

**Conceptual Hierarchy**:
```plain
Job (base abstraction)
├─ SyncJob (user-configurable, in config.yaml)
│   ├─ PackagesJob
│   ├─ DockerJob
│   └─ ...
│
└─ Infrastructure jobs (orchestrator-managed, hardcoded)
    ├─ BtrfsSnapshotJob (sequential execution)
    └─ DiskSpaceMonitorJob (parallel execution)
```

**SyncJob**: Marker subclass for user-configurable jobs in `config.yaml` `sync_jobs` section.

**BtrfsSnapshotJob**: Inherits directly from `Job`, managed by orchestrator:
- Instantiated **twice** with `phase` parameter: `phase="pre"` and `phase="post"`
- Executed at specific points: before and after all SyncJobs
- Cannot be disabled or reordered by users
- Gets all Job infrastructure (logging, progress, abort, RemoteExecutor) for free

**DiskSpaceMonitorJob**: Inherits directly from `Job`, runs in parallel:
- Instantiated once, runs in separate thread/task throughout entire sync
- Continuously monitors disk space at configured intervals
- Raises `DiskSpaceError` if space critically low, triggering abort of all jobs
- Gets all Job infrastructure (logging, progress, abort, RemoteExecutor) for free

### 3. Execution Flow

```
1. Validate all jobs (disk_space_monitor, pre_snapshot, sync jobs, post_snapshot)
2. Start parallel: DiskSpaceMonitorJob.execute() ║════════════════════║
3. Execute sequential: BtrfsSnapshotJob(phase="pre").execute()
4. Execute sequential: SyncJob1.execute()
5. Execute sequential: SyncJob2.execute()
6. Execute sequential: BtrfsSnapshotJob(phase="post").execute()
7. Stop parallel: DiskSpaceMonitorJob.cancel()
```

## Advantages

### DRY (Don't Repeat Yourself)
- All jobs (sync and infrastructure, sequential and parallel) share same base infrastructure
- No duplication of logging, progress reporting, abort handling, RemoteExecutor patterns
- If Job infrastructure improves, all jobs (snapshots, monitoring, sync) benefit automatically
- Disk space monitoring gets proper logging, progress, abort just by inheriting from Job

### Conceptual Clarity
- "Job" = any operation needing infrastructure (base abstraction)
- "SyncJob" = user-configurable sync operations (policy)
- Infrastructure jobs are just jobs that orchestrator manages
- Parallel vs sequential execution is an orchestrator concern, not a Job concern

### Safety Preserved
- Snapshots hardcoded in orchestrator, users can't disable/reorder
- Still bracket all operations (before/after)
- Still get proper timing (post-snapshots after ALL work completes)

### Simplicity
- One execution method per job
- No artificial three-phase structure
- Jobs structure their own work internally
- YAGNI: Don't add complexity until you need it

## Rationale for "Two Jobs" Alternative Not Chosen

While splitting BtrfsSnapshot into two separate jobs (Pre and Post) would solve the timing issue, it was rejected because:

1. **Artificial split**: The pre and post operations are logically a single safety mechanism (create snapshot pair)
2. **Configuration exposure risk**: Making them separate config entries exposes users to misconfiguration
3. **Enforcement burden**: Requires special validation code to ensure both are present, first/last, and enabled together
4. **Missed DRY opportunity**: Infrastructure operations need same capabilities as sync jobs—why duplicate the patterns?

The chosen approach (single job instantiated twice by orchestrator) maintains logical cohesion while achieving correct timing.

## Configuration Examples

**Before**:
```yaml
sync_jobs:
  btrfs_snapshots: true
  packages: true
  docker: true
```

**After**:
```yaml
sync_jobs:
  packages: true  # User-configurable only
  docker: true

# Infrastructure jobs (separate sections, cannot be disabled)
btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"

disk_space_monitor:
  check_interval: 30.0  # seconds
  min_free: "10GB"
  paths:
    - "/"
    - "/home"
```

## Migration Path

This is a design-phase decision before implementation was completed, so migration involves:
1. Update job interface contracts (job-interface.py)
2. Update orchestrator protocol documentation
3. Update data model documentation
4. Implement: Orchestrator hardcodes BtrfsSnapshotJob instantiation
5. Implement: Simplified Job ABC with single execute() method
6. Update all job implementations to use execute() instead of three-phase lifecycle

## Related Documents

- `specs/001-core/contracts/job-interface.py` - Updated interface definition
- `specs/001-core/contracts/orchestrator-job-protocol.md` - Updated protocol
- `specs/001-core/data-model.md` - Updated entity definitions
- `specs/001-core/spec.md` - Functional requirements (will need update)

## Principles Aligned

- **Deliberate Simplicity**: Removed unnecessary complexity (three-phase lifecycle)
- **YAGNI**: Don't add structure until you need it
- **DRY**: Reuse infrastructure across all jobs (sync and infrastructure)
- **Reliability Without Compromise**: Snapshots still bracket operations correctly
