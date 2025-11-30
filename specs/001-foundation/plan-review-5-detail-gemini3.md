# Plan Review 5 (Detail)

**Reviewer**: GitHub Copilot (Gemini 3 Pro)
**Date**: 2025-11-30
**Scope**: Completeness, Correctness, Consistency of the Foundation Plan

## Summary

The plan is high-quality, comprehensive, and well-aligned with the specification. The architecture is robust, and the data model is clear. However, there are a few specific areas where implementation details are missing or ambiguous, particularly regarding safety mechanisms (locking, rollback) and the installation workflow.

## 1. Completeness

### 1.1 Target Locking Mechanism (FR-047)
**Issue**: The plan mentions a target lock file (`~/.local/share/pc-switcher/target.lock`), but the architecture does not specify *how* this lock is acquired and maintained over an SSH connection.
**Impact**: If the lock is merely a file created by a command, a crash or network disconnect could leave a stale lock that requires manual intervention.
**Recommendation**: Specify the mechanism for holding the target lock. Since we have a persistent SSH connection, the `Connection` component or `InstallOnTargetJob` should likely start a background process (e.g., `flock` or a Python script) that holds the lock and dies (releasing it) if the SSH connection is lost.

### 1.2 Rollback Architecture (FR-013)
**Issue**: The CLI component mentions a `rollback` command, and FR-013 requires rollback capability. However, the Architecture document focuses almost exclusively on the `sync` workflow. The `rollback` flow (how it interacts with snapshots, whether it uses the Orchestrator, etc.) is not described.
**Impact**: Developers might implement `rollback` in an ad-hoc manner inconsistent with the rest of the system.
**Recommendation**: Add a brief section or sequence diagram to `architecture.md` describing the `rollback` workflow. Does it use `BtrfsSnapshotJob`? Does it require a full Orchestrator session?

### 1.3 Installation Entry Point (User Story 7)
**Issue**: User Story 7 requires a "setup script" for initial installation. The plan lists `src/pcswitcher/installation.py` (which likely contains the logic), but does not identify the user-facing entry point (e.g., a `scripts/install.sh` or a `pc-switcher setup` CLI command).
**Impact**: It's unclear how a new user initiates the installation process on the source machine.
**Recommendation**: Explicitly define the entry point for the installation/setup process in the Plan's file structure or CLI definition.

## 2. Correctness

### 2.1 UV Bootstrap on Target (FR-005)
**Issue**: FR-005 requires installing `uv` on the target if missing. The `InstallOnTargetJob` description mentions "Install/upgrade" of pc-switcher, but doesn't explicitly confirm it handles the bootstrapping of `uv` itself (which is a prerequisite for installing pc-switcher via `uv tool`).
**Impact**: Sync might fail on a fresh target machine if `uv` is not present.
**Recommendation**: Verify and explicitly state in `architecture.md` (under `InstallOnTargetJob`) that it includes logic to install `uv` if missing (e.g., via `curl | sh` or package manager).

## 3. Consistency

### 3.1 Required Job Protection (FR-034)
**Issue**: FR-034 requires the system to error if a user attempts to disable a required job in `config.yaml`. The Architecture describes "Phase 1" and "Phase 2" validation, but doesn't explicitly list this check.
**Impact**: Users might accidentally disable critical safety jobs (like snapshots) if this check isn't enforced.
**Recommendation**: Add an explicit check in `Orchestrator._validate_configs()` (or Phase 1/2 description) to verify that no job with `required=True` is set to `false` in the configuration.

### 3.2 Snapshot Cleanup Responsibility (FR-014)
**Issue**: FR-014 requires a snapshot cleanup command. The Architecture defines `BtrfsSnapshotJob` (for pre/post sync) and a `cleanup-snapshots` CLI command. It's unclear if the CLI command reuses `BtrfsSnapshotJob` or uses `snapshots.py` directly.
**Impact**: Potential code duplication or unclear separation of concerns.
**Recommendation**: Clarify in `architecture.md` that `cleanup-snapshots` is a standalone CLI operation that utilizes the `snapshots.py` module, distinct from the `BtrfsSnapshotJob` used during sync.

## 4. Minor Observations

- **Job Interface Return Types**: `job-interface.md` correctly defines `validate_config` returning `list[ConfigError]` and `validate` returning `list[ValidationError]`. Ensure the implementation adheres strictly to these distinct error types as defined in the Data Model.
