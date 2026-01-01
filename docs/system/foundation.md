# Foundation Specification

**Domain**: Foundation Infrastructure
**Source**: `specs/001-foundation`

## Overview

This document specifies the core infrastructure of pc-switcher, including the CLI, orchestration engine, job interface, and safety mechanisms.

## Requirements

### Job Architecture

- **FND-FR-001**: System MUST define a standardized Job interface specifying how the orchestrator interacts with jobs, including validation, execution, logging, and progress reporting.
- **FND-FR-002**: System MUST call job lifecycle methods in order: `validate()` (all jobs), then `execute()` for each job.
- **FND-FR-003**: System MUST request termination of currently-executing job when Ctrl+C is pressed, allowing a cleanup timeout.
- **FND-FR-004**: Jobs MUST be loaded from the configuration file section `sync_jobs` in sequential order.

### Self-Installation

- **FND-FR-005**: System MUST check target machine's pc-switcher version before operations; if missing/mismatched, MUST install/upgrade to source version.
- **FND-FR-006**: System MUST abort sync if target version is newer than source.
- **FND-FR-007**: If installation/upgrade fails, system MUST abort sync.
- **FND-FR-007a**: After installation, system MUST sync configuration from source to target (prompting if target has no config).
- **FND-FR-007b**: If target config differs, system MUST display diff and prompt user.
- **FND-FR-007c**: If target config matches, system MUST skip config sync silently.

### Safety Infrastructure

- **FND-FR-008**: System MUST create read-only btrfs snapshots of configured subvolumes on both source and target before any job executes.
- **FND-FR-009**: System MUST create post-sync snapshots after all jobs complete successfully.
- **FND-FR-010**: Snapshot naming MUST follow pattern `{pre|post}-<subvolume>-<timestamp>`.
- **FND-FR-011**: Snapshot management MUST be always active (cannot be disabled).
- **FND-FR-012**: If pre-sync snapshot creation fails, system MUST abort sync.
- **FND-FR-014**: System MUST provide snapshot cleanup command to delete old snapshots while retaining most recent N syncs.
- **FND-FR-015**: System MUST verify that all configured subvolumes exist on both source and target before attempting snapshots.
- **FND-FR-015b**: System MUST verify that `/.snapshots/` is a btrfs subvolume.
- **FND-FR-016**: Orchestrator MUST check free disk space on both source and target before starting sync (Preflight Check).
- **FND-FR-017**: Orchestrator MUST monitor free disk space during sync and abort if it falls below runtime minimum.

### Logging System

- **FND-FR-018**: System MUST implement six log levels: DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL.
- **FND-FR-019**: When a job raises an exception, orchestrator MUST log CRITICAL and abort.
- **FND-FR-020**: System MUST support independent log level configuration for file and TUI.
- **FND-FR-021**: System MUST write logs to timestamped file in `~/.local/share/pc-switcher/logs/`.
- **FND-FR-022**: Log entries MUST use JSON Lines for file and formatted text for terminal.
- **FND-FR-023**: System MUST aggregate logs from both source and target.

### Interrupt Handling

- **FND-FR-024**: System MUST handle SIGINT (Ctrl+C) by requesting job termination and exiting with code 130.
- **FND-FR-025**: On interrupt, system MUST terminate target-side processes.
- **FND-FR-026**: If second SIGINT is received, system MUST force-terminate immediately.
- **FND-FR-027**: System MUST ensure no orphaned processes remain.

### Configuration System

- **FND-FR-028**: System MUST load configuration from `~/.config/pc-switcher/config.yaml`.
- **FND-FR-029**: Configuration MUST use YAML format.
- **FND-FR-030**: System MUST validate configuration against schemas.
- **FND-FR-031**: System MUST apply defaults for missing values.
- **FND-FR-032**: System MUST allow enabling/disabling optional jobs.
- **FND-FR-033**: System MUST report config syntax errors clearly.

### Installation & Setup

- **FND-FR-035**: System MUST provide `install.sh` script runnable via `curl | sh`.
- **FND-FR-036**: Setup script MUST create default config file with comments.

### Testing Infrastructure

- **FND-FR-038**: System MUST include dummy jobs: `dummy-success`, `dummy-fail`.
- **FND-FR-039**: Dummy jobs MUST simulate operations and emit progress.
- **FND-FR-041**: `dummy-fail` MUST raise exception to test error handling.
- **FND-FR-042**: Dummy jobs MUST handle termination requests.

### Progress Reporting

- **FND-FR-043**: Jobs CAN emit progress updates (percent, item).
- **FND-FR-044**: Orchestrator MUST forward progress to TUI.
- **FND-FR-045**: Progress updates MUST be logged at FULL level.

### Core Orchestration

- **FND-FR-046**: System MUST provide single command `pc-switcher sync <target>`.
- **FND-FR-047**: System MUST implement locking to prevent concurrent syncs.
- **FND-FR-048**: System MUST log overall sync result and summary.
