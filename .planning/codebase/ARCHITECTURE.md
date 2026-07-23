<!-- refreshed: 2026-07-23 -->
# Architecture

**Analysis Date:** 2026-07-23

## System Overview

```text
┌─────────────────────────────────────────────────────────────┐
│                       CLI (Typer)                            │
│  `src/pcswitcher/cli.py` — sync / init / logs /              │
│  cleanup-snapshots / update                                  │
└────────────────────────────┬────────────────────────────────┘
                             │ Configuration (`config.py`)
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                       Orchestrator                           │
│  `src/pcswitcher/orchestrator.py`                            │
│  10-step sync session: locks → SSH → gates → validate →      │
│  snapshots → install → config sync → jobs → snapshots        │
└───┬──────────────┬───────────────┬───────────────┬──────────┘
    │              │               │               │
    ▼              ▼               ▼               ▼
┌────────┐  ┌────────────┐  ┌────────────┐  ┌───────────────┐
│ Lock   │  │ Connection │  │  EventBus  │  │ Jobs (registry│
│`lock.  │  │`connection │  │ `events.py`│  │ by module name│
│ py`    │  │ .py`       │  │            │  │ `jobs/`)      │
└────────┘  └─────┬──────┘  └─────┬──────┘  └───────┬───────┘
                  │               │                  │
                  ▼               ▼                  ▼
┌──────────────────────┐  ┌───────────────┐  ┌─────────────────┐
│ Local/RemoteExecutor │  │ TerminalUI    │  │ PackagePhase-   │
│ `executor.py`        │  │ `ui.py` +     │  │ Coordinator     │
│ (Executor Protocol)  │  │ `logger.py`   │  │`jobs/package_   │
│                      │  │               │  │ phase.py`       │
└──────────┬───────────┘  └──────┬────────┘  └─────────────────┘
           │                     │
           ▼                     ▼
┌──────────────────────┐  ┌──────────────────────────────────┐
│ Local shell + remote │  │ JSON log file `sync-*.log` +     │
│ machine over SSH     │  │ Rich Live terminal               │
└──────────────────────┘  └──────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| CLI | Typer app; parses commands, loads config, runs orchestrator, self-update | `src/pcswitcher/cli.py` |
| Orchestrator | Session lifecycle, phase sequencing, job discovery/validation, SIGINT handling, result aggregation | `src/pcswitcher/orchestrator.py` |
| Configuration | YAML load with strict loader + JSON-schema validation into dataclasses | `src/pcswitcher/config.py`, `src/pcswitcher/schemas/config-schema.yaml` |
| Connection | asyncssh connection to target, health check | `src/pcswitcher/connection.py` |
| Executors | `Executor`/`Process` Protocols with `LocalExecutor`, `RemoteExecutor`, `BashLoginRemoteExecutor` | `src/pcswitcher/executor.py` |
| EventBus | Per-consumer `asyncio.Queue` fan-out of `LogEvent`/`ProgressEvent`/`ConnectionEvent` | `src/pcswitcher/events.py` |
| Logger | stdlib logging setup: `JsonFormatter` file handler, `UILogHandler`, `WarningCaptureHandler` | `src/pcswitcher/logger.py` |
| TerminalUI | Rich `Live` display: progress bars, log panel, status | `src/pcswitcher/ui.py` |
| Confirmer | Interactive destructive-action gate (`Confirmer` Protocol, `TerminalUIConfirmer`) | `src/pcswitcher/confirmer.py` |
| Jobs | Self-contained sync units under a `Job` ABC hierarchy | `src/pcswitcher/jobs/` |
| PackagePhaseCoordinator | Runs all package jobs' `plan()`, merges into one batched review, distributes outcome before any `apply()` | `src/pcswitcher/jobs/package_phase.py` |
| Lock | fcntl advisory lock locally, remote `flock` process over SSH | `src/pcswitcher/lock.py` |
| Sync history | Records source/target role + peer per machine; input to topology gates (ADR-015) | `src/pcswitcher/sync_history.py` |
| Btrfs snapshots | Pre/post snapshot creation and cleanup | `src/pcswitcher/btrfs_snapshots.py`, `src/pcswitcher/jobs/btrfs.py` |
| Version | Release/Version parsing, GitHub release lookup, update checks | `src/pcswitcher/version.py` |

## Pattern Overview

**Overall:** Async orchestrator + plugin-style job registry, with an event bus decoupling producers from log/UI consumers.

**Key Characteristics:**
- Asyncio-native end to end; cancellation via native `asyncio.CancelledError`, no manual flag polling
- Single multiplexed SSH connection; the target is stateless and only executes discrete commands (ADR-002)
- Jobs are autonomous: own config schema, own validation, own cleanup
- Sync jobs run sequentially; background jobs run concurrently in an `asyncio.TaskGroup`
- Two-step package convergence: `plan()` (read-only) → coordinator review → `apply()` (converge)

## Layers

**CLI layer:**
- Purpose: command surface, config load, exit codes, self-update
- Location: `src/pcswitcher/cli.py`
- Depends on: `config.py`, `orchestrator.py`, `logger.py`, `version.py`
- Used by: console script entry point `pc-switcher = "pcswitcher.cli:app"`

**Orchestration layer:**
- Purpose: session lifecycle and phase sequencing
- Location: `src/pcswitcher/orchestrator.py`
- Depends on: lock, connection, executors, event bus, jobs, sync history, disk
- Used by: CLI only

**Job layer:**
- Purpose: the actual sync work
- Location: `src/pcswitcher/jobs/`
- Depends on: `JobContext` (executors, event bus, confirmer, config slice)
- Used by: orchestrator via dynamic import

**Infrastructure layer:**
- Purpose: transport, process execution, events, logging, UI, locking, state files
- Location: `connection.py`, `executor.py`, `events.py`, `logger.py`, `ui.py`, `lock.py`, `sync_history.py`, `disk.py`, `sudoers.py`, `terminal.py`
- Used by: orchestrator and jobs

## Data Flow

### Primary sync path

1. `pc-switcher sync <target>` enters Typer command (`src/pcswitcher/cli.py:191`)
2. Config loaded and validated (`src/pcswitcher/cli.py:47` → `src/pcswitcher/config.py:89`)
3. `_async_run_sync` builds logging, UI, orchestrator (`src/pcswitcher/cli.py:298`)
4. `Orchestrator.run()` executes the ordered phases (`src/pcswitcher/orchestrator.py:304`)
5. Source lock (`orchestrator.py:502`) → SSH connect (`:518`) → target hostname resolve (`:531`) → target lock (`:559`)
6. Topology gates: first-sync confirm (`:702`) and out-of-order check (`:756`), both rehearsed under `--dry-run` (ADR-014/015)
7. Job discovery + validation (`:883`); ordering constraint enforced by `_check_package_jobs_precede_folder_sync` (`:961`)
8. Disk-space preflight (`:992`), pre-snapshots (`:1066`), install on target (`:576`), config sync (`:593`)
9. Job execution loop inside a TaskGroup with background monitors (`:1089`, `:1126`)
10. Post-snapshots (`:1066`), sync-history update (`:855`), `_cleanup` in `finally` (`:1237`), warning summary (`:1286`)

### Package convergence flow

1. `PackagePhaseCoordinator` calls each enabled package job's `plan()` (read-only diff of source manifest vs target state)
2. Review groups from `apt_sync`, `snap_sync`, `flatpak_sync` are merged by manager and action
3. One batched interactive review runs via `coordinate_package_review` (`src/pcswitcher/jobs/package_phase.py:129`)
4. Each job receives only its own accepted slice; `execute()` refuses to run without a coordinator-supplied accepted plan
5. `apply()` converges item by item, collecting `ConvergeItemFailed` into `PackageItemFailures` rather than stopping at the first failure

### Event/logging flow

1. Jobs and orchestrator call stdlib `logging` with `extra={"job", "host"}`
2. `UILogHandler` (`src/pcswitcher/logger.py:257`) forwards records to the UI sink; `JsonFormatter` (`:92`) writes JSON lines to `sync-*.log`
3. Progress goes through `EventBus.publish(ProgressEvent)` (`src/pcswitcher/events.py:68`) into per-consumer queues so the UI never blocks jobs

**State Management:**
- No in-memory global session state; per-run state lives on the `Orchestrator` instance
- Cross-run state is on disk: `~/.local/share/pc-switcher/` (lock file, sync history, logs) and btrfs snapshots at `/mnt/btrfs-snapshots/pc-switcher`

## Key Abstractions

**Job (ABC):**
- Purpose: one self-contained sync operation
- File: `src/pcswitcher/jobs/base.py:22`
- Subtypes: `SystemJob` (required infra), `SyncJob` (config-enabled), `BackgroundJob` (concurrent)
- Contract: `validate_config()` classmethod against `CONFIG_SCHEMA`, `async validate()`, `async execute()`

**PackageSyncJob:**
- Purpose: shared plan/apply skeleton for apt/snap/flatpak
- File: `src/pcswitcher/jobs/package_sync_core.py:136`, plan dataclass at `:92`

**Executor / Process (Protocols):**
- Purpose: uniform local and remote command execution
- File: `src/pcswitcher/executor.py:26`, `:46`
- Implementations: `LocalExecutor` (`:148`), `RemoteExecutor` (`:260`), `BashLoginRemoteExecutor` (`:383`)

**JobContext (frozen dataclass):**
- Purpose: everything a job is allowed to touch — config slice, both executors, event bus, session id, hostnames, dry-run flag, confirmer, sibling enablement map
- File: `src/pcswitcher/jobs/context.py`

**Package item model:**
- Purpose: typed representation of every syncable package artifact and its diff
- File: `src/pcswitcher/jobs/package_items.py` (`ItemClass`, `DiffClass`, `DiffAction`, `AptPackageItem`, `AptSourceItem`, `AptKeyItem`, `AptPinItem`, `AptConfigItem`, `SnapItem`, `FlatpakItem`, `FlatpakRemoteItem`, `UnreproducibleItem`)

**Domain models:**
- `Host`, `LogLevel`, `CommandResult`, `ProgressUpdate`, `ConfigError`, `ValidationError`, `SnapshotPhase`, `Snapshot`, `SessionStatus`, `JobStatus`, `JobResult`, `SyncSession` — all in `src/pcswitcher/models.py`

## Entry Points

**Console script `pc-switcher`:**
- Location: `src/pcswitcher/cli.py` (`app`)
- Commands: `sync`, `init`, `logs`, `cleanup-snapshots`, `update`

**Installer:**
- Location: `install.sh`
- Triggers: bootstrap install/upgrade via `uv tool install`; also invoked remotely by `InstallOnTargetJob` (`src/pcswitcher/jobs/install_on_target.py`)

**Remote target invocations:**
- The target is driven purely through SSH commands issued by `RemoteExecutor`; there is no daemon on the target.

## Architectural Constraints

- **Python version:** `requires-python = ">=3.14"`; `.python-version` pins the toolchain
- **Threading:** single asyncio event loop; no threads. Background jobs use `asyncio.TaskGroup`; sync jobs run sequentially by design
- **Job discovery is convention-bound:** job name MUST equal its module name under `pcswitcher.jobs` (`orchestrator.py:624`). A mismatch is only a logged warning, not an error
- **Job ordering:** package jobs must precede `folder_sync`; enforced as a config error (`orchestrator.py:961`)
- **Target statelessness:** the target never decides anything; capture and all decisions happen on the source (ADR-002, ADR-020)
- **Locking:** one lock file per machine regardless of role, `~/.local/share/pc-switcher/pc-switcher.lock`; fcntl locally, remote `flock` process held for the session
- **Deferred imports:** `TYPE_CHECKING` guards are used broadly for executor/UI types to avoid import cycles between `jobs/`, `executor.py`, and `events.py`
- **Self-upgrade:** an in-place upgrade must re-exec or exit once disk is touched; never continue running the old process

## Anti-Patterns

### Job that reviews and converges inside its own `execute()`

- **What happens:** A package job prompts the user and mutates the target within `execute()`.
- **Why it's wrong:** The orchestrator's job loop is sequential, so `apt_sync` would finish mutating the target before `snap_sync` had even diffed, breaking the "one batched review before any change" guarantee.
- **Do this instead:** Split into `plan()` / `apply()` and let `PackagePhaseCoordinator` own the review step (`src/pcswitcher/jobs/package_phase.py`).

### Discovering an environment assumption inside `execute()`

- **What happens:** A job checks for a binary, sudo right, or filesystem feature mid-execution.
- **Why it's wrong:** The failure lands against a half-modified target; some (a missing sudo right) degrade silently to an empty capture and the sync reports success having replicated nothing.
- **Do this instead:** Check every assumption in `async validate()` and return a `ValidationError` carrying a concrete remediation command (`src/pcswitcher/jobs/base.py:84`).

### Raising on the first validation problem

- **What happens:** `validate()` raises as soon as it finds an issue.
- **Why it's wrong:** The user fixes problems one SSH round-trip at a time.
- **Do this instead:** Append to a list and return all errors at once.

### Calling the logger or UI directly from a job

- **What happens:** A job writes to the terminal or a file handler itself.
- **Why it's wrong:** Blocks job execution on rendering and bypasses level filtering per consumer.
- **Do this instead:** Use `Job._log(...)` and `Job._report_progress(...)` (`src/pcswitcher/jobs/base.py:125`, `:146`).

### Passing raw log content into a Rich `Panel`

- **What happens:** `Panel(str)` parses arbitrary content as Rich markup.
- **Why it's wrong:** Untrusted content raises `MarkupError` and kills the display.
- **Do this instead:** Wrap untrusted content in `rich.text.Text` before rendering (`src/pcswitcher/ui.py`).

### Widening `JobContext.config` to the full config

- **What happens:** A job needs to know about a sibling job and reaches for the whole config.
- **Why it's wrong:** `config` is job-specific; widening it silently changes what every existing job sees.
- **Do this instead:** Use `JobContext.enabled_sync_jobs` for sibling enablement questions.

## Error Handling

**Strategy:** Exceptions propagate to the orchestrator, which converts them into `JobResult`/`SessionStatus` and a single logged failure.

**Patterns:**
- TaskGroup `ExceptionGroup`s are unwrapped to the originating error (`orchestrator.py:121`)
- Failures are marked as already-logged to avoid duplicate CRITICAL entries (`orchestrator.py:155`, `:169`)
- `asyncio.CancelledError` is caught for cleanup and re-raised
- Domain exceptions in `models.py`: `DiskSpaceCriticalError`, `SyncAbortedByUser`, `SyncLockedError`
- Per-item package failures accumulate (`ConvergeItemFailed` → `PackageItemFailures`) instead of aborting the job
- `finally`-block cleanup always terminates the remote lock process and disconnects

## Cross-Cutting Concerns

**Logging:** stdlib `logging` with structured `extra` fields; JSON file sink plus Rich terminal sink; `WarningCaptureHandler` accumulates warnings for an end-of-run summary.
**Validation:** three phases — YAML/JSON-schema, `Job.validate_config()`, `Job.validate()`.
**Dry run:** unified contract (ADR-014) — gates are rehearsed, no state is modified; `JobContext.dry_run` propagates to every job.
**Confirmation:** destructive actions go through the `Confirmer` protocol, which pauses the Rich `Live` UI before prompting.
**Interactivity detection:** `terminal.is_interactive()` requires a TTY on both stdin and stdout so the UI and the confirmer never disagree.
