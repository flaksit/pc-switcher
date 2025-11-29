# Plan Review: Foundation Infrastructure Complete

**Reviewer**: Claude (Plan Reviewer)
**Date**: 2025-11-29
**Status**: Review complete

## Overall Assessment

The plan is comprehensive and well-structured. The architecture design is solid, with clear separation of concerns, good use of modern Python async patterns, and thoughtful handling of edge cases. However, I've identified several issues and opportunities for improvement.

---

## Critical Issues

### 1. Missing Logger in JobContext

**Location**: `data-model.md` lines 319-332, `contracts/job-interface.md` lines 216-225

The `JobContext` dataclass includes `logger: JobLogger` in `data-model.md` but the contract in `job-interface.md` shows:

```python
@dataclass(frozen=True)
class JobContext:
    config: dict[str, Any]
    source: LocalExecutor
    target: RemoteExecutor
    event_bus: EventBus
    session_id: str
    source_hostname: str
    target_hostname: str
```

The `logger` field is missing. Meanwhile, the Job helper methods `_log()` and `_report_progress()` in the contract use `context.event_bus` directly rather than `context.logger`. This inconsistency needs resolution:

- Either remove `logger` from `data-model.md` and confirm jobs use `event_bus` directly
- Or add `logger: JobLogger` to the contract's JobContext and update helper methods

### 2. Host Parameter Missing in Log/Progress Helper Methods

**Location**: `data-model.md` lines 446-462, `contracts/job-interface.md` lines 124-138

The `_log()` helper method signature doesn't include a `host` parameter, but `LogEvent` requires `host: Host`. How does a job specify which host (SOURCE or TARGET) the log message relates to?

The architecture shows the JobLogger is bound to a specific host (line 383-385 in architecture.md), but the Job base class helper methods don't show this binding.

**Suggestion**: Clarify how host is determined. Options:
1. Jobs call `self._log(context, HOST.SOURCE, level, message)` explicitly
2. JobContext provides two loggers: `context.source_logger` and `context.target_logger`
3. Helper method infers host from context (but this is ambiguous for jobs operating on both)

---

## Consistency Issues

### 3. Validation Phase Terminology Mismatch

**Location**: `architecture.md` lines 454-487 vs `plan.md` line 15

Architecture describes three phases:
1. Schema Validation (Orchestrator)
2. Job Config Validation (`Job.validate_config()`)
3. System State Validation (`Job.validate()`)

But plan.md line 15 says "Three validation phases (Schema → Job Config → System State)" which matches.

However, the spec (FR-030) says:
> "System MUST validate configuration structure and job-specific settings against job-declared schemas"

This conflates Phase 1 (schema) and Phase 2 (job config). The plan correctly separates them, but ensure the implementation matches this separation.

### 4. Event Type Definition Inconsistency

**Location**: `data-model.md` lines 489-514 vs `architecture.md` lines 142-148

In `data-model.md`, the EventBus uses:
```python
Event = TypeVar("Event", LogEvent, ProgressEvent, ConnectionEvent)
```

But this TypeVar usage is incorrect for a union of event types. It should be:
```python
type Event = LogEvent | ProgressEvent | ConnectionEvent
```

Or using `Union` for compatibility.

### 5. ProgressEvent vs ProgressUpdate Naming

**Location**: `data-model.md` lines 149-156 vs `architecture.md` lines 148

Architecture defines `ProgressEvent` with fields directly, but data-model wraps `ProgressUpdate` inside `ProgressEvent`:

```python
@dataclass(frozen=True)
class ProgressEvent:
    job: str
    update: ProgressUpdate
    timestamp: datetime
```

This is cleaner and should be consistent. Verify architecture diagrams match this structure.

---

## Missing Details

### 6. Rollback Command Not Specified

**Location**: `spec.md` User Story 3, FR-013

The spec requires:
> "System MUST provide rollback capability to restore from pre-sync snapshots (requires user confirmation)"

Neither the plan nor architecture describes:
- CLI command syntax (`pc-switcher rollback`?)
- Which snapshots to rollback to (selection UI?)
- Rollback process steps
- What happens to post-sync snapshots during rollback

**Suggestion**: Add a section to architecture.md or create a separate rollback-workflow document.

### 7. Double SIGINT Handling Not Fully Specified

**Location**: `architecture.md` line 526 mentions exit(130), but FR-026 requires:
> "If second SIGINT is received during cleanup, system MUST immediately force-terminate without waiting"

The sequence diagram doesn't show the double-SIGINT path. Add a note or separate diagram.

### 8. Target Lock Release on Abort

**Location**: `research.md` lines 450-473

The research explains acquiring target lock via SSH, but doesn't explain how the lock is released when:
- Sync completes normally
- Sync aborts due to error
- Connection is lost

Since flock locks are released when the file descriptor closes, and that happens when the SSH session ends, this should work automatically. But this should be explicitly documented.

### 9. Log File Aggregation from Target

**Location**: `spec.md` FR-023, `plan.md` line 23

FR-023 requires:
> "System MUST aggregate logs from both source-side orchestrator and target-side operations into unified log stream"

The architecture shows target commands returning output, but how are logs from background target processes (like disk monitor on target) streamed back?

The research shows RemoteExecutor uses `start_process()` with async stdout iteration, but the DiskSpaceMonitorJob sequence diagram (architecture.md lines 711-747) doesn't show how log messages from the target-side monitor reach the source-side EventBus.

**Suggestion**: Clarify that:
- Target DiskSpaceMonitor runs locally on target (via RemoteExecutor)
- Its `_log()` calls go through the same RemoteExecutor → source connection
- Or document that only exception propagation (not log streaming) happens for background jobs

---

## Potential Improvements

### 10. Consider Structured Error Types for CLI Output

The `ConfigError` and `ValidationError` dataclasses are good, but consider adding a `format_for_user()` method to produce consistent CLI error messages. This would ensure errors like:

```
Error: Invalid config for 'packages'
  'sync_ppa' must be boolean
  in ~/.config/pc-switcher/config.yaml:15
```

...are formatted consistently.

### 11. Session ID in Executor Context

`RemoteExecutor` doesn't have access to `session_id`, but this could be useful for:
- Naming temp files on target
- Correlating target-side logs

Consider whether `RemoteExecutor` should be aware of the session.

### 12. Snapshot Creation Order

**Location**: `architecture.md` line 982

The execution flow shows:
```
SnapPre → InstallOnTarget → StartDiskMon → SyncJobs → SnapPost
```

This creates pre-sync snapshots BEFORE installing pc-switcher on target. This is correct for safety, but:
- Pre-sync snapshot on target is created via direct SSH btrfs commands (good)
- But if target has NO pc-switcher, how does the orchestrator know the target's subvolume paths?

The config (source-side) specifies subvolumes like `["@", "@home"]`. These are assumed identical on target. If they differ, validation (Phase 3) should catch this. Ensure this is explicitly checked in BtrfsSnapshotJob.validate().

### 13. Default Configuration Generation

FR-037 requires:
> "Setup script MUST create default config file with inline comments explaining each setting"

The `config-schema.yaml` has descriptions, but there's no mention of how to generate a default config file with these as YAML comments. Consider adding a template or generation script.

---

## Minor Issues

### 14. Typo in Quickstart

**Location**: `quickstart.md` line 128

```python
from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import JobContext, LogLevel, ProgressUpdate, ValidationError
```

The import path inconsistency: `pcswitcher.models` should import `ConfigError` too if the job uses it, and `Host` is imported but may not be in models (it's defined in data-model.md).

### 15. Missing pyproject.toml Reference

The plan shows project structure and quickstart shows `uv add` commands, but there's no pyproject.toml template in the contracts. Consider adding one or referencing the expected structure.

### 16. Process Protocol Missing `stdin`

**Location**: `data-model.md` lines 391-395

The `Process` protocol only has `stdout()`, `stderr()`, `wait()`, `terminate()`. If any job needs to send input (e.g., answering a prompt), there's no `stdin` method. This may be intentional (avoid interactive commands), but should be documented as a constraint.

---

## Summary

| Category | Count |
|----------|-------|
| Critical Issues | 2 |
| Consistency Issues | 3 |
| Missing Details | 4 |
| Potential Improvements | 4 |
| Minor Issues | 3 |

**Recommendation**: Address critical issues 1 and 2 before proceeding to task generation. The consistency and missing details issues can potentially be addressed during implementation, but resolving them now will prevent confusion.

The overall architecture is sound and the design decisions are well-reasoned. The plan demonstrates good understanding of async Python patterns, proper separation of concerns, and thoughtful error handling.
