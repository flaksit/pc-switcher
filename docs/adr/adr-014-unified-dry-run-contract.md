# ADR-014: Unified Dry-Run Contract for All SyncJobs

Status: Accepted

Date: 2026-06-30

## TL;DR
`--dry-run` is a tool-wide policy: every SyncJob must perform a full read-only preview — connect, validate, detect divergence, and report what it would do — without writing files, taking snapshots, or updating any history or divergence markers.

## Implementation Rules

**Required:**
- Every SyncJob MUST implement a real preview when `JobContext.dry_run` is `True`; a no-op stub is non-compliant
- A dry run MUST perform all read-only steps: connect, acquire locks, validate, and run divergence/conflict detection
- Every SyncJob MUST report exactly what it would do; `folder_sync` satisfies this by passing `--dry-run` to rsync, which outputs the would-be transfers and deletions

**Forbidden in dry-run mode:**
- Writing, creating, or modifying any file on source or target
- Creating btrfs snapshots (pre or post)
- Updating the sync-history record
- Updating any per-target divergence marker

**Orchestrator enforcement:**
- The orchestrator MUST skip the post-sync sync-history update when `dry_run` is set — enforced in plan 01-03

## Context
Users expect `--dry-run` to mean "show me exactly what would happen, make no changes." For a multi-job orchestrator, this guarantee must be uniformly enforced across all jobs and at the orchestrator level (sync-history update), not left to each job's discretion.

`JobContext.dry_run` already exists as a boolean field on the execution context (`src/pcswitcher/jobs/context.py`). The orchestrator currently logs a dry-run banner but does not yet skip the post-sync sync-history update — that gap is closed in plan 01-03. This ADR formalizes the contract so all current and future SyncJobs have a clear, testable specification.

## Decision
- `--dry-run` is a tool-wide contract binding on ALL SyncJobs and the orchestrator, not a per-job optional feature (D-12)
- A dry run is a full read-only rehearsal: all pre-flight and validation steps run so the user sees an accurate preview
- `folder_sync` satisfies the contract by passing `--dry-run` to rsync; every future SyncJob must provide equivalent preview behavior
- The orchestrator skips the post-sync sync-history update when `dry_run` is set (enforcement point: plan 01-03)

## Consequences

**Positive:**
- Users can safely preview any sync — including first-time runs with `--delete` — without risk of data modification
- Divergence detection runs in dry-run mode, giving users a heads-up before committing to the sync
- The contract is testable: unit tests can assert no file writes, no snapshots, and no history updates occur during dry-run

**Negative:**
- Every SyncJob must implement a meaningful preview, not a no-op; this is an additional per-job implementation requirement
- The orchestrator's dry-run skip for sync-history must be maintained as new orchestration phases are added

## References
- D-12: Unified --dry-run contract (01-CONTEXT.md)
- `src/pcswitcher/jobs/context.py`: `JobContext.dry_run` boolean field
- Plan 01-03: Orchestrator sync-history dry-run enforcement point
- ADR-005: Asyncio concurrency (rsync subprocess; dry-run uses the same async path)
