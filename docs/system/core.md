# Core System Specification

The authoritative, current definition of pc-switcher's core: CLI, orchestration, locking, snapshots, configuration, and the job contract. Every claim here is verified against `src/pcswitcher/`.

**Domain Code**: `CORE` (Core)

## User Scenarios & Testing

### Job Architecture and Integration Contract (CORE-US-JOB-ARCH)

Lineage: 001-US-1

Every sync capability is a job. `Job` (`jobs/base.py`) defines the whole contract — config schema, validation, execution, logging, progress, error handling — so a new job is written against the base class alone with no orchestrator change. Snapshots and self-installation are `SystemJob`s the orchestrator runs directly, not entries in `sync_jobs`.

**Acceptance Scenarios**:

1. **Given** a new `SyncJob` subclass in `pcswitcher/jobs/<name>.py` whose `name` ClassVar equals its module name, **When** `<name>: true` appears in `sync_jobs`, **Then** `Orchestrator._resolve_sync_job_class` imports the module, finds the class, validates its config and runs it in the job loop — no core change

2. **Given** a job declares `CONFIG_SCHEMA` (a JSON Schema draft-07 dict), **When** config is loaded, **Then** `Job.validate_config()` validates the top-level config section named after the job, and the job reads the validated dict through `self.context.config`

3. **Given** a job logs via `self._log(host, level, message, **extra)` at any of the six levels (DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL), **Then** the record is routed to the log file at or above `logging.file` and to the terminal at or above `logging.tui`, tagged with the job name and host

4. **Given** a job calls `self._report_progress(ProgressUpdate(...))`, **Then** the event reaches the terminal UI through the event bus and updates that job's progress bar

5. **Given** any job's `validate()` returns errors, **When** the validation phase runs, **Then** the orchestrator collects every job's errors, raises `RuntimeError` listing all of them, and halts before any state changes

6. **Given** a job raises, **When** the exception reaches the job loop, **Then** the orchestrator records a FAILED `JobResult`, logs CRITICAL, and halts the run — except anything raised by a package job (plus `PackageItemFailures` and `ProbeFailed` wherever raised), which is recorded FAILED without cancelling the remaining jobs, and `JobSkipped`, which is recorded SKIPPED (see [Job outcomes](#job-outcomes))

7. **Given** the user presses Ctrl+C during job execution, **Then** the CLI cancels the sync task, the orchestrator's `finally` block runs `_cleanup()`, and the process exits with code 130

### Self-Installing Sync Orchestrator (CORE-US-SELF-INSTALL)

Lineage: 001-US-2

The orchestrator brings the target's pc-switcher to the source's version, then reconciles the config file. Both happen after pre-sync snapshots, so a bad install is recoverable.

**Acceptance Scenarios**:

1. **Given** the target has no pc-switcher, **When** `InstallOnTargetJob` runs, **Then** it pipes `install.sh` for the source's version into bash on the target (`install.get_install_with_script_command_line`), which bootstraps `uv` if missing and runs `uv tool install`

2. **Given** the target has an older version, **Then** the job logs `Upgrading pc-switcher on target from <old> to <new>` and installs; the exact-version tag is attempted first and, when no such tag exists, the run falls back to the highest release at or below the source version (`Version.get_release_floor`)

3. **Given** source and target versions match, **Then** the job logs `Target pc-switcher version matches source: <version>, no install needed` and returns immediately

4. **Given** the install or the post-install `pc-switcher --version` check fails, **Then** the job raises `RuntimeError` and the sync aborts

5. **Given** the target has no `~/.config/pc-switcher/config.yaml`, **When** the config-sync step runs, **Then** this machine's config is displayed and the user is asked `Apply this config to <hostname>?` — every line of the screen names both machines by hostname (`PKG-FR-NAME-THE-MACHINES`) (`y` applies and continues, `n` — the default — aborts the whole sync with `SyncAbortedByUser`)

6. **Given** the target config differs from the source's, **Then** a unified diff is shown with three choices: `a` take this machine's config, `k` keep the other machine's, `x` abort (the default) — each naming its machine by hostname

7. **Given** the configs match, **Then** the step prints `<hostname>'s config matches <hostname>'s, skipping config sync.` without prompting

`--yes` auto-accepts both prompts. `--dry-run` shows the same preview and never prompts or writes.

### Safety Infrastructure with Btrfs Snapshots (CORE-US-BTRFS)

Lineage: 001-US-3

Read-only btrfs snapshots of the configured subvolumes are taken on both machines before any sync job runs, and again after they all complete. This is orchestrator-level infrastructure with no configuration switch to disable it.

**Acceptance Scenarios**:

1. **Given** configured subvolumes, **When** `BtrfsSnapshotJob.validate()` runs, **Then** every subvolume must exist on both machines; any missing one is a `ValidationError` and the sync aborts before snapshots are created

2. **Given** the pre-snapshot step, **Then** read-only snapshots are created at `/.snapshots/pc-switcher/<timestamp>-<session-id>/<phase>-<subvolume>-<timestamp>` on both machines (e.g. `pre-@home-20251129T143022`); every timestamp is UTC

3. **Given** all sync jobs completed, **Then** post-sync snapshots are created in the same session folder with the `post-` prefix

4. **Given** `/.snapshots` does not exist, **Then** `validate_snapshots_directory` creates it as a btrfs subvolume along with its `pc-switcher` folder

5. **Given** `/.snapshots` exists but is not a subvolume, **Then** validation fails with an error explaining that snapshots would be recursive, and the sync aborts

6. **Given** snapshot creation fails, **Then** the job logs CRITICAL and raises, aborting the sync

7. **Given** accumulated snapshots, **When** the user runs `pc-switcher cleanup-snapshots --older-than 7d`, **Then** snapshots older than 7 days are deleted on that machine only, keeping the most recent `btrfs_snapshots.keep_recent` sessions (default 3) regardless of age; `--older-than` is optional and falls back to `btrfs_snapshots.max_age_days`, and `--dry-run` previews the deletions

8. **Given** `disk_space_monitor.preflight_minimum` (default `"20%"`), **When** the disk-check step runs, **Then** free space on `/` is checked on both machines and the sync aborts with a CRITICAL log if either is below the threshold

9. **Given** `disk_space_monitor.check_interval` (default 30) and `disk_space_monitor.runtime_minimum` (default `"15%"`), **When** jobs are running, **Then** a `DiskSpaceMonitorJob` per machine re-checks at that interval and raises `DiskSpaceCriticalError` if free space drops below the runtime minimum; `warning_threshold` (default `"25%"`) logs a WARNING without aborting

### Graceful Interrupt Handling (CORE-US-INTERRUPT)

Lineage: 001-US-5

Ctrl+C cancels the sync task and lets the orchestrator's cleanup run. No rollback is issued — that is a separate feature.

**Acceptance Scenarios**:

1. **Given** a sync in progress, **When** the first SIGINT arrives, **Then** the CLI prints `Interrupt received, cleaning up...` and cancels the sync task; the orchestrator logs `Sync interrupted by user` at WARNING, and its `finally` block restores the snapd hold, releases the target lock, terminates every tracked local and remote process, runs `pkill --full pc-switcher` on the target, disconnects, and releases the source lock. The CLI then prints `Sync interrupted by user` and exits 130

2. **Given** the interrupt arrives between jobs, **Then** the remaining jobs never start and cleanup runs the same way

3. **Given** a second SIGINT before cleanup completes, **Then** the CLI cancels every task in the loop immediately, without waiting for cleanup

There is no cleanup grace timeout: cleanup either completes or is cut short by the second SIGINT.

### Configuration System (CORE-US-CONFIG)

Lineage: 001-US-6

`Configuration.from_yaml` loads `~/.config/pc-switcher/config.yaml` (or `--config <path>`), validates it against `pcswitcher/schemas/config-schema.yaml` with `jsonschema` (draft-07), and applies defaults. Job sections are top-level keys named after the job.

**Acceptance Scenarios**:

1. **Given** a config with global and job sections, **Then** the loader validates the structure, applies defaults, and hands each job its own section via `JobContext.config`

2. **Given** `logging: { file: DEBUG, tui: INFO, external: WARNING }`, **Then** the file handler keeps everything at DEBUG and above, the terminal shows INFO and above, and non-`pcswitcher` loggers must additionally clear WARNING

3. **Given** `sync_jobs: { dummy_success: true, dummy_fail: false }`, **Then** `dummy_success` runs and `dummy_fail` is skipped with a DEBUG log (`Job dummy_fail is disabled in config`) and no `JobResult`

4. **Given** an unknown key anywhere in the config — including an unknown name under `sync_jobs` — **Then** schema validation fails (`additionalProperties: false` throughout) and the CLI exits before any sync work

5. **Given** invalid YAML, **Then** `ConfigurationError` reports the line and column and the CLI exits 1; a duplicate mapping key is a parse error too (`_StrictLoader`)

**Example Configuration**: `pcswitcher/default-config.yaml` is the shipped, fully commented example; `pc-switcher init` writes it verbatim to `~/.config/pc-switcher/config.yaml` together with the `home.filter` and `root.filter` starter files it references.

**Configuration Schema**: `src/pcswitcher/schemas/config-schema.yaml`. Job-specific settings are top-level keys outside `sync_jobs` (`btrfs_snapshots`, `disk_space_monitor`, `folder_sync`, `dummy_success`, `dummy_fail`). The seven package jobs and `vscode_state_sync` have no config section — a job earns one only once it has a real key, so these are enabled through `sync_jobs` alone.

### Installation and Setup Infrastructure (CORE-US-INSTALL)

Lineage: 001-US-7

`install.sh` deploys pc-switcher to a machine with no prerequisites, and is the same script `InstallOnTargetJob` runs on the target.

**Acceptance Scenarios**:

1. **Given** a fresh Ubuntu 24.04 machine, **When** the user runs `curl -sSL https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh | bash`, **Then** the script installs `uv` if missing, offers to install `btrfs-progs` if missing, installs pc-switcher via `uv tool install`, creates `~/.local/share/pc-switcher/logs`, and prints the next steps — the first of which is `pc-switcher init` to create the config

2. **Given** a sync installs on the target, **When** the target is missing `uv`, **Then** the same script bootstraps it before installing pc-switcher

3. **Given** `~/.config/pc-switcher/config.yaml` already exists, **When** the user runs `pc-switcher init`, **Then** it refuses with `Configuration file already exists` and exits 1; `--force` overwrites config.yaml and both filter files

`VERSION=<tag>` and `--ref <branch-or-sha>` select what the script installs.

### Dummy Test Jobs (CORE-US-DUMMY)

Lineage: 001-US-8

`dummy_success` and `dummy_fail` exercise the orchestrator, logging, progress UI, error handling and interrupt handling, and serve as reference implementations of the job contract.

**Acceptance Scenarios**:

1. **Given** `dummy_success` is enabled, **Then** it busy-waits `source_duration` seconds on the source (INFO every 2s, WARNING at 6s), then `target_duration` seconds on the target (INFO every 2s, ERROR at 8s), reporting progress at 0/25/50/75/100%, and completes successfully

2. **Given** `dummy_fail` is enabled, **When** elapsed time reaches `fail_at`, **Then** it raises `RuntimeError`; the orchestrator records a FAILED `JobResult`, logs CRITICAL and halts the sync

3. **Given** either dummy job is running, **When** the task is cancelled, **Then** it catches `CancelledError`, logs a termination message, and re-raises

### Terminal UI with Progress Reporting (CORE-US-TUI)

Lineage: 001-US-9

`TerminalUI` (Rich `Live`) shows the current step, per-job progress bars, a recent-logs panel and connection status.

**Acceptance Scenarios**:

1. **Given** a job reports progress, **Then** the display shows that job's bar with percentage or item counts, and a spinner for heartbeat-only updates

2. **Given** the job loop is running, **Then** the step counter reads `Step 10a/12`, `Step 10b/12`, … — one letter sub-step per job, labelled with the job's display name (`Step 10d/12 — Manual debs`). The denominator is `len(SyncStep)` and is fixed regardless of how many jobs are enabled

3. **Given** a record at or above `logging.tui`, **Then** it appears in the Recent Logs panel, colour-coded by level; every record at or above WARNING is also captured and reprinted as an end-of-run summary block after the Live display stops

### Spec-Driven Test Coverage for Core (CORE-US-TEST-COVERAGE)

Lineage: 003-US-1

Core's tests are written from this document — user stories, acceptance scenarios, functional requirements — not from the implementation, so a gap between the two fails a test rather than passing silently. Each test names the requirement it covers (e.g. `test_core_fr_lock`), and both success and failure paths are covered.

## Edge Cases

Lineage: 001-core edge cases, 003-core-tests edge cases

- **Target becomes unreachable mid-sync** — asyncssh keepalives (15 s interval, 3 missed) drop the connection, the failing operation raises, and the sync aborts. No reconnection is attempted.

- **Source crashes or powers off** — the target's lock is a `flock` held by a remote process attached to the SSH session, so it is released when the connection dies. Nothing else is left behind to time out.

- **Snapshots cannot be created** — `BtrfsSnapshotJob` logs CRITICAL with the btrfs error and raises, aborting before any job runs.

- **Cleanup itself raises** — `_cleanup` absorbs a failed snapd restore and continues; a declined confirmation at that gate is honoured, logged at WARNING, and the remaining cleanup (which only releases resources) still runs.

- **Two syncs at once** — the second run fails to take the fcntl lock and reports `This machine is already involved in a sync (held by: …)` (or, for the far end, `Target <host> is already involved in a sync`) with instructions to terminate the holder process rather than delete the lock file.

- **Unknown job name in `sync_jobs`** — rejected by the config schema at load time; the CLI exits before connecting. A *known* name whose module or class does not resolve logs a WARNING and records a SKIPPED `JobResult`.

- **Partial failures** — each job contributes its own `JobResult`; `_summarize_job_outcomes` marks the session FAILED and the CLI exits 1 if any job is FAILED, listing the names.

- **Target has a newer version than the source** — `InstallOnTargetJob.validate()` returns a `ValidationError` and the sync aborts, preventing an accidental downgrade.

## Requirements

### Functional Requirements

#### Job Architecture

- **CORE-FR-JOB-IFACE** `[Deliberate Simplicity]` `[Reliability Without Compromise]`: `Job` MUST define the whole orchestrator/job contract: the `name`, `display_name` and `required` ClassVars, the `CONFIG_SCHEMA` ClassVar, `validate_config()`, `validate()`, `execute()`, `_log()` and `_report_progress()`  
  Lineage: 001-FR-001

- **CORE-FR-JOB-DISPLAY-NAME** `[Frictionless Command UX]`: Everything a human reads — the step label, the progress bars, the `Job outcomes:` block, the first-sync warning, the failure summary and the job-lifecycle log messages — MUST name a job by its `display_name`, which defaults to `name` when a job declares none. Everything a machine matches on — the `sync_jobs` key, the module path, the `job` field of a log record, `ConfigError.job`, `ValidationError.job`, the `active_job` trace label — MUST keep `name`, so improving the wording never invalidates a config or a log query  
  Lineage: 02-UAT-02

- **CORE-FR-LIFECYCLE** `[Reliability Without Compromise]`: The orchestrator MUST call `validate_config()` for every enabled job, then `validate()` for every job that passed, then `execute()` one job at a time in config order  
  Lineage: 001-FR-002

- **CORE-FR-TERM-CTRLC** `[Reliability Without Compromise]`: Ctrl+C MUST cancel the sync task so the running job receives `CancelledError` and the orchestrator's `finally` block runs cleanup; a second SIGINT MUST cancel every task immediately without waiting for cleanup  
  Lineage: 001-FR-003

- **CORE-FR-JOB-LOAD** `[Deliberate Simplicity]`: Jobs MUST be imported from `pcswitcher.jobs.<job_name>` and run in the order their keys appear in `sync_jobs`; there is no dependency resolution  
  Lineage: 001-FR-004

- **CORE-FR-JOB-ORDER** `[Reliability Without Compromise]`: The orchestrator MUST reject a config in which `apt_sync`, `snap_sync`, `flatpak_sync`, `manual_deb_sync`, `manual_snap_sync`, `manual_flatpak_sync` or `manual_installs_sync` is enabled after `folder_sync` — apps are provisioned before their data lands on top (`PKG-FR-JOB-ORDER`)  
  Lineage: 02-WR-02

#### Self-Installation

- **CORE-FR-VERSION-CHECK** `[Frictionless Command UX]`: Before installing, the system MUST read the target's `pc-switcher --version`; if missing or different from the source's, it MUST install by piping the version's `install.sh` from the public GitHub repository into bash on the target (no authentication required)  
  Lineage: 001-FR-005

- **CORE-FR-VERSION-NEWER** `[Reliability Without Compromise]`: The system MUST abort if the target's version is newer than the source's, preventing an accidental downgrade  
  Lineage: 001-FR-006

- **CORE-FR-INSTALL-FAIL** `[Frictionless Command UX]`: A failed install, or a post-install version that does not match what was requested, MUST abort the sync  
  Lineage: 001-FR-007

- **CORE-FR-CONFIG-SYNC** `[Reliability Without Compromise]`: After installation, the system MUST reconcile `config.yaml` with the target; with no config on the target it MUST show the source's and prompt, and MUST abort if the user declines  
  Lineage: 001-FR-007a

- **CORE-FR-CONFIG-DIFF** `[Frictionless Command UX]`: If the target's config differs, the system MUST show a unified diff and offer accept-source, keep-target or abort  
  Lineage: 001-FR-007b

- **CORE-FR-CONFIG-MATCH** `[Frictionless Command UX]`: If the configs match, the system MUST skip the transfer without prompting  
  Lineage: 001-FR-007c

#### Safety Infrastructure (Btrfs Snapshots)

- **CORE-FR-SNAP-PRE** `[Reliability Without Compromise]`: Read-only snapshots of the configured subvolumes MUST be created on both machines before any job executes  
  Lineage: 001-FR-008

- **CORE-FR-SNAP-POST** `[Reliability Without Compromise]`: Post-sync snapshots MUST be created after the job loop finishes  
  Lineage: 001-FR-009

- **CORE-FR-SNAP-NAME** `[Minimize SSD Wear]`: Snapshot names MUST be `{pre|post}-<subvolume>-<UTC timestamp>` inside a per-session folder `<UTC timestamp>-<session id>`  
  Lineage: 001-FR-010

- **CORE-FR-SNAP-ALWAYS** `[Reliability Without Compromise]`: Snapshots MUST be orchestrator-level infrastructure (a `SystemJob`, not a `sync_jobs` entry) with no option to disable them  
  Lineage: 001-FR-011

- **CORE-FR-SNAP-FAIL** `[Frictionless Command UX]`: A failed pre-sync snapshot MUST abort before any state modification  
  Lineage: 001-FR-012

- **CORE-FR-SNAP-CLEANUP** `[Minimize SSD Wear]`: `pc-switcher cleanup-snapshots` MUST delete old snapshots while retaining the most recent N sessions; `keep_recent` and `max_age_days` MUST be configurable under `btrfs_snapshots`  
  Lineage: 001-FR-014

- **CORE-FR-SUBVOL-EXIST** `[Reliability Without Compromise]`: Every configured subvolume MUST be verified to exist on both machines before snapshots are attempted  
  Lineage: 001-FR-015

- **CORE-FR-SNAPDIR** `[Reliability Without Compromise]`: `/.snapshots` MUST be a btrfs subvolume; the system MUST create it when absent and MUST abort when it exists as a plain directory (which would make snapshots recursive)  
  Lineage: 001-FR-015b

- **CORE-FR-DISK-PRE** `[Reliability Without Compromise]`: Free space on `/` MUST be checked on both machines before snapshots; `disk_space_monitor.preflight_minimum` MUST be a percentage (`"20%"`) or an absolute value with a `GiB`/`MiB`/`GB`/`MB` unit; unitless values are rejected by the schema; default `"20%"`  
  Lineage: 001-FR-016

- **CORE-FR-DISK-RUNTIME** `[Reliability Without Compromise]`: A background monitor per machine MUST re-check free space every `disk_space_monitor.check_interval` seconds (default 30) and abort the run when free space falls below `runtime_minimum` (default `"15%"`); `warning_threshold` (default `"25%"`) warns without aborting  
  Lineage: 001-FR-017

#### Interrupt Handling

- **CORE-FR-SIGINT** `[Reliability Without Compromise]`: A SIGINT handler MUST cancel the sync task, log `Sync interrupted by user` at WARNING, and exit with code 130  
  Lineage: 001-FR-024

- **CORE-FR-TARGET-TERM** `[Reliability Without Compromise]`: Cleanup MUST terminate every tracked local and remote process and run `pkill --full pc-switcher` on the target  
  Lineage: 001-FR-025

- **CORE-FR-FORCE-TERM** `[Reliability Without Compromise]`: A second SIGINT MUST cancel every task immediately, without waiting for cleanup  
  Lineage: 001-FR-026

- **CORE-FR-NO-ORPHAN** `[Reliability Without Compromise]`: No orphaned processes may remain on either machine after an interrupt  
  Lineage: 001-FR-027

#### Configuration System

- **CORE-FR-CONFIG-LOAD** `[Frictionless Command UX]`: Configuration MUST be loaded from `~/.config/pc-switcher/config.yaml`, overridable per command with `--config`  
  Lineage: 001-FR-028

- **CORE-FR-CONFIG-FORMAT** `[Deliberate Simplicity]`: Configuration MUST be YAML with global sections, `sync_jobs` (enable/disable), and one top-level section per job named after the job  
  Lineage: 001-FR-029

- **CORE-FR-CONFIG-VALIDATE** `[Reliability Without Compromise]`: The file MUST be validated against the packaged JSON Schema (draft-07, via `jsonschema`) and each job section against that job's `CONFIG_SCHEMA`, both before execution  
  Lineage: 001-FR-030

- **CORE-FR-CONFIG-DEFAULTS** `[Frictionless Command UX]`: Missing values MUST take the defaults defined on the config dataclasses  
  Lineage: 001-FR-031

- **CORE-FR-JOB-ENABLE** `[Frictionless Command UX]`: Jobs MUST be enabled or disabled via `sync_jobs: { module_name: true|false }`  
  Lineage: 001-FR-032

- **CORE-FR-CONFIG-ERROR** `[Reliability Without Compromise]`: A syntax error, a duplicate key or a schema violation MUST be reported with its location and exit before the sync starts  
  Lineage: 001-FR-033

#### Installation & Setup

- **CORE-FR-INSTALL-SCRIPT** `[Frictionless Command UX]`: `install.sh` MUST run via `curl | bash` with no prerequisites — installing `uv` from `https://astral.sh/uv/install.sh` if absent, offering `btrfs-progs` via apt if absent, installing the package with `uv tool install`, and creating the log directory. `InstallOnTargetJob` MUST run this same script so there is one installation path  
  Lineage: 001-FR-035

- **CORE-FR-DEFAULT-CONFIG** `[Up-to-date Documentation]`: `pc-switcher init` MUST write the packaged `default-config.yaml`, whose inline comments explain every setting, plus the `home.filter` and `root.filter` files it references  
  Lineage: 001-FR-036

#### Testing Infrastructure (Dummy Jobs)

- **CORE-FR-DUMMY-JOBS**: The system MUST ship two dummy jobs: `dummy_success` and `dummy_fail`  
  Lineage: 001-FR-038

- **CORE-FR-DUMMY-SIM**: Both MUST simulate configurable-duration work on source (log every 2 s, WARNING at 6 s) and target (log every 2 s, ERROR at 8 s) and emit progress updates  
  Lineage: 001-FR-039

- **CORE-FR-DUMMY-EXCEPTION** `[Reliability Without Compromise]`: `dummy_fail` MUST raise at the configured `fail_at` elapsed second, on whichever phase that falls in, to exercise orchestrator exception handling  
  Lineage: 001-FR-041

- **CORE-FR-DUMMY-TERM** `[Reliability Without Compromise]`: Both MUST handle `CancelledError` by logging a termination message and re-raising  
  Lineage: 001-FR-042

#### Progress Reporting

- **CORE-FR-PROGRESS-EMIT** `[Frictionless Command UX]`: Jobs CAN emit `ProgressUpdate`s (percentage, item counts, item description, heartbeat, sub-bar track). Optional, but recommended for long operations  
  Lineage: 001-FR-043

- **CORE-FR-PROGRESS-FWD** `[Frictionless Command UX]`: The event bus MUST deliver `ProgressEvent`s to the terminal UI, which owns one bar per job and one per `track`  
  Lineage: 001-FR-044

#### Core Orchestration

- **CORE-FR-SYNC-CMD** `[Frictionless Command UX]`: `pc-switcher sync <hostname>` MUST run the complete workflow. Flags: `--config`, `--dry-run`, `--yes`, `--allow-out-of-order`, `--allow-first-sync`, `--confirm-each-command`, `--apply-package-installs`, `--apply-package-removals`  
  Lineage: 001-FR-046

- **CORE-FR-LOCK** `[Reliability Without Compromise]`: A single unified lock per machine MUST prevent it from taking part in two syncs at once, in either role  
  Lineage: 001-FR-047

- **CORE-FR-SUMMARY**: Every job that ran MUST contribute one `JobResult` with SUCCESS, SKIPPED or FAILED and its start/end timestamps; the session status MUST be derived from those results, the outcome message MUST name each failed job together with the reason it recorded, the run MUST end by printing one line per job giving its name, its status and — for SKIPPED and FAILED — the reason it recorded, and the exit code MUST be non-zero when any job failed  
  Lineage: 001-FR-048

### Core Test Requirements

- **CORE-FR-TEST-US** / **CORE-FR-TEST-AS** / **CORE-FR-TEST-FR**: Tests MUST cover every user story, acceptance scenario and functional requirement in this document  
  Lineage: 003-FR-001, 003-FR-002, 003-FR-003

- **CORE-FR-TEST-PATHS**: Tests MUST cover the failure path of each requirement, not only the success path  
  Lineage: 003-FR-004

- **CORE-FR-TEST-NAMING**: Test names MUST identify the requirement under test (e.g. `test_core_fr_lock`), and each test file MUST reference the requirements it covers in its docstring  
  Lineage: 003-FR-007, 003-FR-008

- **CORE-FR-TEST-INDEP**: Tests MUST NOT depend on execution order or shared mutable state  
  Lineage: 003-FR-009

- **CORE-FR-TEST-MOCK**: Unit tests MUST use mock executors; integration tests MUST run real operations on test VMs  
  Lineage: 003-FR-011, 003-FR-012

Test directory layout, markers, fixtures and runtime budgets are specified in the [Testing Framework](testing.md).

### Key Entities

Lineage: 001-core Key Entities, 003-core-tests Key Entities

Field-level definitions live in the [Data Model](data-model.md); this is the vocabulary.

- **Job** — abstract base for every sync component. Subclasses: **SystemJob** (required, orchestrator-run), **SyncJob** (configurable through `sync_jobs`), **BackgroundJob** (runs concurrently in the job TaskGroup). Named twice: `name` is the identifier, `display_name` the wording a user reads (`CORE-FR-JOB-DISPLAY-NAME`)
- **JobContext** — everything a job is given: its config section, both executors, the event bus, session id, both hostnames, `dry_run`, `allow_first_sync`, the confirmer, the reviewer, the target username, and the full `sync_jobs` enablement map
- **SyncSession** — one sync run: session id, timestamps, both hostnames, status and the collected `JobResult`s
- **JobResult** — one job's outcome: both spellings of its name, SUCCESS/SKIPPED/FAILED, start and end timestamps, and an error or skip reason (see [Job outcomes](#job-outcomes))
- **Snapshot** — a btrfs snapshot: subvolume, phase, UTC timestamp, session id, host and path; parseable back out of its path
- **ProgressUpdate** — percentage, item counts, item description, heartbeat flag and optional sub-bar track
- **Configuration** — the parsed and validated config: `logging`, `sync_jobs`, `disk`, `btrfs_snapshots` and the per-job sections
- **Connection** — the asyncssh connection to the target, with session multiplexing and keepalive-based failure detection
- **LocalExecutor / RemoteExecutor** — the only route to either machine: `run_command`, `start_process`, `terminate_all_processes`, `declare_modification`, plus `send_file` and `get_file` on the remote side

## Success Criteria

- **CORE-SC-SINGLE-CMD** `[Frictionless Command UX]`: A complete sync runs from `pc-switcher sync <hostname>` with no additional manual steps  
  Lineage: 001-SC-001

- **CORE-SC-SNAPSHOTS** `[Reliability Without Compromise]`: Pre- and post-sync snapshots exist for 100% of successful runs  
  Lineage: 001-SC-002

- **CORE-SC-ABORT** `[Reliability Without Compromise]`: A CRITICAL error aborts the run with no state modification after the abort  
  Lineage: 001-SC-003

- **CORE-SC-VERSION-TIME** `[Frictionless Command UX]`: Version check plus install/upgrade on the target completes within 30 seconds  
  Lineage: 001-SC-004

- **CORE-SC-AUDIT**: Log files carry a complete audit trail — timestamps, levels, job and host attribution — for 100% of runs  
  Lineage: 001-SC-005

- **CORE-SC-GRACEFUL** `[Reliability Without Compromise]`: Ctrl+C shuts down cleanly with no orphaned processes in 100% of tests  
  Lineage: 001-SC-006

- **CORE-SC-JOB-SIMPLE** `[Deliberate Simplicity]`: A basic new job needs only the job interface and no orchestrator change  
  Lineage: 001-SC-007

- **CORE-SC-COW** `[Minimize SSD Wear]`: Snapshots are copy-on-write with zero initial write amplification  
  Lineage: 001-SC-008

- **CORE-SC-INSTALL-TIME** `[Frictionless Command UX]`: `install.sh` completes on a fresh Ubuntu 24.04 machine in under 2 minutes with a network connection  
  Lineage: 001-SC-009

- **CORE-SC-DUMMY-DEMO** `[Reliability Without Compromise]`: The dummy jobs demonstrate success, exception handling and cancellation in 100% of test runs  
  Lineage: 001-SC-010

- **CORE-SC-TEST-GAPS**: Running the test suite surfaces any gap between this document and the implementation as a failing test  
  Lineage: 003-SC-006

## Per-action confirmation

`--confirm-each-command` inserts one prompt before every operation that is not purely read-only. It exists because what a job asks about is an outcome, not the commands that produce it: one approved line can expand into a simulation, the real command, a file backup, a staged transfer, a privileged promotion and a metadata refresh. The flag is for the run where that expansion itself needs auditing.

"Not purely read-only" is deliberately wider than "changes a file". An operation is exempt only when it can change no state on the machine at all — no file content, no process state, no lock or other advisory state, no package-manager database. Scoping the prompt to content alone leaves out the operations that seize control of a machine without writing anything: a `flock` that takes the sync lock, a background process left running. It is not wider than the command itself, though: running a read under `sudo` does not make it a write, so `sudo <read-only command>` is read-only and prompts for nothing.

What the prompt shows is the job and the hostname of the machine about to be changed, then what the change does, then the operation itself: the literal string handed to the shell, or `send_file <local> -> <remote>` for a transfer. Naming the machine once in the heading is why the `mutates=` phrase below it does not repeat it (`PKG-FR-NAME-THE-MACHINES`). Nothing is paraphrased, because a display string that can differ from what executes is worse than no display. The one thing withheld is the userinfo of any URL in it (`LOG-FR-CREDENTIAL-REDACTION`), since this is the one route out of the executor that never becomes a log record for the logging filter to catch.

Two outcomes, no default — one keypress decides, with no Enter after it (a run under this flag asks the question dozens of times):

- **proceed** — run this operation, then continue to the next prompt.
- **abort sync** — raise `SyncAbortedByUser` and stop the whole run.

Any other key is discarded and the prompt keeps waiting, so an accidental Enter picks neither outcome; so is anything typed before the prompt appeared, which stops a key pressed during the previous command from answering a question the user has not seen. There is deliberately no "skip this one". A single approved outcome can span several commands, so skipping one of them leaves it half-applied — worse than either finishing it or stopping. An unanswerable prompt (EOF, Ctrl-C) is an abort, never an approval — but a plain `SyncAborted`, since nobody answered it.

The flag requires a TTY on both stdin and stdout and is refused at startup without one: a gate with a non-interactive fallback would have to auto-proceed, which is exactly what it exists to prevent. It has no config-file equivalent — it is a per-run decision.

Coverage is every modification pc-switcher makes to either machine, with no exception: every job's converge commands and file writes, `folder_sync`'s rsync passes, the state files a job records on either machine, the orchestrator's snapd auto-refresh pause and its restoration, btrfs snapshot creation and deletion, the config push to the target, both machines' sync locks, the installer run on the target, and the VS Code state DB replacement. Plus what changes a machine without writing to it: every background process started.

A folder sync asks once per rsync pass, so a folder converged in one pass asks once and one whose per-directory filter files must land first asks twice — the copy pass, which deletes nothing, then the deleting mirror. The prompt is headed by the TARGET although rsync runs on the source: what a user is being asked to allow is the machine that loses files. A `--dry-run` pass asks nothing, and is the one background process the flag does not gate: `--dry-run` writes on neither machine and the call waits the process out rather than leaving it running, so there is no state for a prompt to be about — and a preview run that stopped to ask permission would read as one that might change something, which is the opposite of what a preview is for.

What it does not cover is the privileged reads a run makes — `fuser` on the dpkg lock, `btrfs subvolume show`, `rsync --version`, the filter-file digest `find`, `snap get system refresh.hold`, the `/etc/apt` capture, and each job's `sudo --non-interactive true` precondition check. Each prints an answer the run needs and changes nothing; `sudo` in front of a read does not turn it into one of the writes this flag exists to show. Two further exceptions are deliberate rather than pending: `flatpak remote-ls` populates flatpak's own `~/.cache/flatpak` metadata, which no part of pc-switcher reads, and releasing a lock is not announced — releases run in `_cleanup`, where an abort has nowhere to go and would leak the very lock it was declining to free. `tests/unit/test_mutates_audit.py` holds the full list with each reason.

One consequence of "no skip" shapes the code beyond the gate itself:

- **Declining the snapd restore is honoured, and stops exactly there.** `_restore_snap_hold` re-raises `SyncAborted` ahead of its best-effort handler, so an abort is not absorbed the way a failed restore is. `_cleanup` catches it around that one call and continues: everything after it releases resources (target lock, SSH connection, source lock, event bus, UI) rather than modifying a machine, and no confirmation prompt should be able to leak a lock. What was left in place is logged at `WARNING`, so the end-of-run summary resurfaces it. This matters because restoring is not merely lifting — when the machine already had a hold of its own, declining means pc-switcher's timed hold expires and that prior hold is gone with it, which is why the prompt names the value being written back.

The mechanism lives in `executor.py`, the one funnel every command, transfer and background process already passes through, so it is caller-agnostic rather than job-specific: any call that passes `mutates="<phrase>"` declares itself a modification and is gated, on either machine. In-process changes that are neither a command nor a transfer go through `declare_modification` so they reach the same funnel. Pure reads pass no `mutates` and are never gated — that is what keeps the prompts worth reading. The trade-off is that the marker is opt-in, so a forgotten `mutates=` is an unannounced change; `tests/unit/test_mutates_audit.py` enumerates every ungated call site, as a pure read, a deliberately tolerated side effect with its reason, or a tracked defect, so an omission fails a test instead of shipping.

The same seam carries the verbatim `DEBUG` trace of every executor operation — reads included, since a trace that omits them cannot answer "what did the tool actually do" — and, on the way back, each command's own stdout and stderr as separate records (`_trace_output`). The job a line belongs to comes from the `active_job` context variable the orchestrator sets around each job, which `asyncio` copies per task so a concurrently running background job cannot clobber the label.

## Job outcomes

Every job that ran contributes one `JobResult` with a status of SUCCESS, SKIPPED or FAILED (`CORE-FR-SUMMARY`). That includes the fixed install-on-target step, which runs on every sync and is a job like any other; only the phases that are not jobs at all — locking, snapshots, config sync, history — stay out of the record.

The dividing line between the first two is what the job's inaction means. "Nothing to do because the target already matches the source" is the goal met, so it is SUCCESS — an empty package plan, a mirror that finds nothing to transfer. "Nothing done because nobody could decide, or nothing was applicable" is SKIPPED. Per-item exclusions inside an otherwise-working job are neither: a job-level status cannot express them, and the review and the run's warnings already do.

Situations that produce SKIPPED:

- A package job (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_deb_sync`, `manual_snap_sync`, `manual_installs_sync`) whose review had items to offer on a run with no TTY. Nobody was present to answer anything, so every item is marked skip-once and the job converges nothing.
- `apt_sync` when the source carries Ubuntu Pro (ESM) packages, the target reports no attachment, and the ESM gate is either unanswerable or answered "skip".
- `vscode_state_sync` when the source has none of the state DBs it handles.
- `folder_sync` when every configured folder is `enabled: false`.
- An enabled `sync_jobs` name whose module or class does not resolve. There is no job instance in this case, so the orchestrator records the result at discovery time; the job the user enabled leaves a record rather than only a warning.

A skipped job does not fail the run: the remaining jobs still execute, the session still completes, and the exit code is unchanged. A job signals it by raising `JobSkipped`, which it may only do **before** its first mutating command — raised later, the partial state it already wrote would go unreported.

FAILED behaves the same way for every failure of a package job (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_deb_sync`, `manual_snap_sync`, `manual_installs_sync`): a FAILED `JobResult`, a CRITICAL log, and the remaining jobs still run. What isolates a failure is the job it came out of, not its exception class — a package job that dies on a registry transfer, a filesystem error or a parser defect says no more about another manager's already-approved work than one whose items failed to converge. `PackageItemFailures` and `ProbeFailed` isolate wherever they are raised, being by construction one manager's trouble.

Two things still end the run: a `SyncLockedError`, because the machine is no longer entitled to sync at all, and any failure of a job outside package sync (`folder_sync`, `vscode_state_sync`, the core jobs). Which of those may survive a failure is GitHub issue #220.

The end-of-run message names each failed job with the reason it recorded, not the job names alone — one line per failed job, so a job that names forty failed items still costs one line. That message is the session's own record: it is logged and stored on the session, and it is what the exit code is derived from.

What the user reads is the outcome block the run prints last, beside the warning summary and after the live display has stopped. It gives one line per job in execution order — a mark, the job's display name, its status, and for SKIPPED and FAILED the reason that job recorded. It is the only place outcomes are printed: the same failures rendered twice in two shapes read as two different things having gone wrong. It is printed for every ending, not only a clean one, because an aborted or interrupted run still did whatever it did before it stopped. Reasons quote text pc-switcher did not author — package-manager stderr, file paths — so they are rendered literally rather than as Rich markup, where `[installed]` would silently vanish and `[/usr/bin/apt]` would crash the run after all its work was done.

Dry-run is not a reason to report SKIPPED on its own: a rehearsal that completes did succeed. A rehearsal that hits one of the situations above is skipped like any other run.

## Assumptions

Lineage: 001-core Assumptions, 003-core-tests Assumptions

- Source and target run Ubuntu 24.04 LTS on btrfs
- The user has sudo on both machines for operations requiring elevation
- Both machines are reachable over SSH (LAN, VPN such as Tailscale, or other) for the whole sync
- `~/.ssh/config` holds the target's connection details when an alias is used
- The terminal supports ANSI escape codes for the progress UI
- The target has enough disk space for package installation
- Nothing else is modifying the same system state during a sync

## Out of Scope

Lineage: 001-core Out of Scope, 003-core-tests Out of Scope

- Bi-directional sync or conflict resolution between divergent states
- Automatic rollback from a snapshot
- Sync scheduling or daemon mode
- GUI or web interface
- Windows or macOS support, and non-btrfs filesystems
- Multi-user concurrent usage
