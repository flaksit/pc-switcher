"""Core orchestrator coordinating the complete sync workflow."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import secrets
from datetime import UTC, datetime
from logging.handlers import QueueListener
from typing import Any

from rich.console import Console
from rich.text import Text

from pcswitcher.btrfs_snapshots import session_folder_name
from pcswitcher.config import Configuration
from pcswitcher.config_sync import sync_config_to_target
from pcswitcher.confirmer import Confirmer, TerminalUIConfirmer
from pcswitcher.connection import Connection
from pcswitcher.disk import DiskSpace, check_disk_space, parse_threshold
from pcswitcher.events import EventBus
from pcswitcher.executor import LocalExecutor, RemoteExecutor, RemoteProcess
from pcswitcher.jobs.base import Job, SyncJob
from pcswitcher.jobs.btrfs import BtrfsSnapshotJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.disk_space_monitor import DiskSpaceMonitorJob
from pcswitcher.jobs.install_on_target import InstallOnTargetJob
from pcswitcher.lock import (
    SyncLock,
    get_local_hostname,
    get_lock_path,
    release_remote_lock,
    start_persistent_remote_lock,
)
from pcswitcher.logger import (
    generate_log_filename,
    get_logs_directory,
    setup_logging,
)
from pcswitcher.models import (
    ConfigError,
    FirstSyncScope,
    Host,
    JobResult,
    JobStatus,
    SessionStatus,
    SnapshotPhase,
    SyncAbortedByUser,
    SyncLockedError,
    SyncSession,
    ValidationError,
)
from pcswitcher.sync_history import (
    HISTORY_PATH,
    SyncRole,
    get_last_sync_state,
    get_record_role_command,
    parse_sync_state,
    record_role,
)
from pcswitcher.ui import TerminalUI

__all__ = ["Orchestrator"]


def _stuck_lock_hint(machine: str, lock_path: str) -> str:
    """How-to-unblock guidance appended to lock-conflict errors.

    The lock is an fcntl advisory lock on the open fd, not the file's existence,
    so a leftover lock *file* never blocks a future sync — deleting it does not
    help. The lock is released automatically when the holding process exits or
    its SSH connection closes. The only way a lock genuinely stays held with no
    running sync is an orphaned holder process, which must be terminated (not
    rm'd) to clear.
    """
    return (
        f"Wait for the other sync to finish — the lock releases automatically when it exits. "
        f"If no sync is running, a previous run left a stuck lock on {machine}; clear it by "
        f"terminating the holder process (e.g. `fuser -k {lock_path}` or `pkill -f pc-switcher.lock`), "
        f"not by deleting the lock file."
    )


def _unwrap_taskgroup_error(exc: BaseException) -> BaseException:
    """Flatten an ``asyncio.TaskGroup`` ExceptionGroup to its primary cause.

    A job that fails inside the job-execution TaskGroup surfaces as an
    ExceptionGroup whose own message — "unhandled errors in a TaskGroup (N
    sub-exceptions)" — is meaningless to users and developers alike. Jobs run
    sequentially, so a failed sync normally has a single underlying cause; this
    returns it so callers can report the real reason. Expected control-flow
    exceptions (an aborted-by-user or lock conflict raised from within a job)
    are preferred over other leaves so they still reach their dedicated WARNING
    handlers instead of the generic CRITICAL "Sync failed" path. A non-group
    exception is returned unchanged.
    """
    if not isinstance(exc, BaseExceptionGroup):
        return exc

    leaves: list[BaseException] = []
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if isinstance(current, BaseExceptionGroup):
            stack.extend(reversed(current.exceptions))
        else:
            leaves.append(current)

    for leaf in leaves:
        if isinstance(leaf, (SyncAbortedByUser, SyncLockedError)):
            return leaf
    return leaves[0] if leaves else exc


_FAILURE_LOGGED_ATTR = "_pcswitcher_failure_logged"


def _mark_failure_logged(exc: BaseException) -> None:
    """Flag that this exception's failure was already logged with job context.

    Set by the per-job failure handler so run()'s top-level handler does not log
    the same cause a second time. The flag rides on the exception object, so it
    survives being wrapped by the TaskGroup and unwrapped by
    ``_unwrap_taskgroup_error`` (which returns the same leaf instance). A failure
    that never passes a job handler — every non-job phase, and jobs once they run
    in parallel via ``create_task`` rather than the sequential loop — stays
    unflagged and is still logged at the top level.
    """
    setattr(exc, _FAILURE_LOGGED_ATTR, True)


def _failure_already_logged(exc: BaseException) -> bool:
    """Whether ``_mark_failure_logged`` already reported this exception's cause."""
    return getattr(exc, _FAILURE_LOGGED_ATTR, False)


class Orchestrator:
    """Main orchestrator coordinating the complete sync workflow.

    Responsibilities:
    - Schema and job config validation
    - SSH connection management
    - Lock acquisition (source and target)
    - Version check and self-installation
    - System state validation (delegated to jobs)
    - Sequential job execution
    - Background job management (DiskSpaceMonitor)
    - Sync summary and session tracking
    """

    def __init__(
        self,
        target: str,
        config: Configuration,
        *,
        auto_accept: bool = False,
        allow_out_of_order: bool = False,
        allow_first_sync: bool = False,
        dry_run: bool = False,
    ) -> None:
        """Initialize orchestrator with target and validated configuration.

        Args:
            target: Target hostname or SSH alias
            config: Validated configuration from YAML file
            auto_accept: If True, auto-accept prompts (e.g., config sync)
            allow_out_of_order: If True, bypass the out-of-order topology confirmation (W2/W3)
            allow_first_sync: If True, auto-approve the first-sync overwrite confirmation
                issued by FolderSyncJob when the target has no sync history (ADR-015)
            dry_run: If True, preview sync without making changes
        """
        self._config = config
        self._auto_accept = auto_accept
        self._allow_out_of_order = allow_out_of_order
        self._allow_first_sync = allow_first_sync
        self._dry_run = dry_run
        self._session_id = secrets.token_hex(4)
        self._session_folder = session_folder_name(self._session_id)
        self._source_hostname = get_local_hostname()
        self._target_hostname = target

        # Core components
        self._event_bus = EventBus()
        self._logger = logging.getLogger("pcswitcher.orchestrator")
        self._connection: Connection | None = None
        self._local_executor: LocalExecutor | None = None
        self._remote_executor: RemoteExecutor | None = None

        # Locks
        self._source_lock: SyncLock | None = None
        self._target_lock_process: RemoteProcess | None = None

        # Background tasks
        self._task_group: asyncio.TaskGroup | None = None
        self._cleanup_in_progress = False

        # Logging infrastructure (initialized in run())
        self._queue_listener: QueueListener | None = None
        self._ui: TerminalUI | None = None
        self._console: Console | None = None
        self._ui_task: asyncio.Task[None] | None = None
        self._confirmer: Confirmer | None = None

    def _create_job_context(self, config: dict[str, Any]) -> JobContext:
        """Create JobContext with current orchestrator state.

        Must only be called after SSH connection is established (Phase 2+).
        """
        assert self._local_executor is not None
        assert self._remote_executor is not None

        return JobContext(
            config=config,
            source=self._local_executor,
            target=self._remote_executor,
            event_bus=self._event_bus,
            session_id=self._session_id,
            source_hostname=self._source_hostname,
            target_hostname=self._target_hostname,
            dry_run=self._dry_run,
            allow_first_sync=self._allow_first_sync,
            confirmer=self._confirmer,
            # Connection is always set when _create_job_context is called in
            # production (Phase 2+), but unit tests mock executors without a
            # real connection, so fall back to None (JobContext accepts it).
            target_username=self._connection.username if self._connection is not None else None,
        )

    async def run(self) -> SyncSession:  # noqa: PLR0915
        """Execute the complete sync workflow.

        Returns:
            SyncSession with results and status

        Raises:
            Various exceptions for critical failures (connection, locks, validation, etc.)
        """
        session = SyncSession(
            session_id=self._session_id,
            started_at=datetime.now(UTC),
            source_hostname=self._source_hostname,
            target_hostname=self._target_hostname,
            config={},  # TODO: Add config snapshot
            status=SessionStatus.RUNNING,
            job_results=[],
        )

        # Initialize logging infrastructure BEFORE any operations
        # Both hostnames are known: source from local hostname, target from CLI argument
        if not self._source_hostname:
            raise RuntimeError("Source hostname is not set")
        if not self._target_hostname:
            raise RuntimeError("Target hostname is not set")

        # Create the UI before logging so setup_logging can route the TUI-floor
        # handler through the UI's Recent Logs panel instead of a raw stderr
        # write (both share the same terminal region as Live). Constructing
        # TerminalUI does not start the Live (start() is still called below),
        # so creating it early is safe.
        #
        # Calculate total steps: 8 system phases + sync jobs + 1 post-snapshot
        # System phases: 1=source lock, 2=SSH, 3=target lock, 4=validation,
        # 5=disk check, 6=pre-snapshots, 7=install on target, 8=config sync
        # Count only enabled jobs for the initial estimate; Phase 4 discovery may
        # reduce this further (e.g. module not found), so we correct via
        # set_total_steps() after _discover_and_validate_jobs() returns.
        total_steps = 8 + sum(1 for enabled in self._config.sync_jobs.values() if enabled) + 1
        self._console = Console()
        self._ui = TerminalUI(
            console=self._console,
            total_steps=total_steps,
        )
        # Shared interactive confirmation gate for the orchestrator's out-of-order check
        # and any job-level prompt (e.g. FolderSyncJob first-sync overwrite, ADR-015).
        self._confirmer = TerminalUIConfirmer(self._console, self._ui, logger=self._logger)

        # Create log file path and set up stdlib logging infrastructure.
        # Passing ui + console lets setup_logging pick the UI-routed TUI
        # handler when the console is a real terminal, falling back to plain
        # stderr otherwise (CI/non-TTY runs).
        log_file_path = get_logs_directory() / generate_log_filename(self._session_id)
        self._queue_listener, _ = setup_logging(
            log_file_path, self._config.logging, ui=self._ui, console=self._console
        )

        # Log session start with hostname mapping (LOG-FR-SESSION-HOSTNAMES)
        self._logger.info(
            "Starting sync session",
            extra={
                "job": "orchestrator",
                "host": "source",
                "source_hostname": self._source_hostname,
                "target_hostname": self._target_hostname,
                "session_id": self._session_id,
            },
        )

        # Subscribe to event bus for UI (ProgressEvent, ConnectionEvent only)
        ui_queue = self._event_bus.subscribe()

        # Start UI event consumer as background task (ProgressEvent, ConnectionEvent)
        self._ui_task = asyncio.create_task(self._ui.consume_events(queue=ui_queue))

        # Start UI live display
        self._ui.start()

        # Log dry-run mode banner
        if self._dry_run:
            self._logger.info(
                "[DRY-RUN] Preview mode - no changes will be made",
                extra={"job": "orchestrator", "host": "source"},
            )

        try:
            # Phase 1: Acquire source lock
            self._logger.info("Acquiring source lock", extra={"job": "orchestrator", "host": "source"})
            await self._acquire_source_lock()
            self._ui.set_current_step(1, "Source lock")

            # Phase 2: Establish SSH connection
            self._logger.info("Connecting to target", extra={"job": "orchestrator", "host": "source"})
            await self._establish_connection()
            assert self._remote_executor is not None
            self._ui.set_current_step(2, "Connect to target")

            # Phase 3: Acquire target lock
            self._logger.info("Acquiring target lock", extra={"job": "orchestrator", "host": "target"})
            await self._acquire_target_lock()
            self._ui.set_current_step(3, "Target lock")

            # Topology out-of-order / target-state check (between Phase 3 and 4):
            # runs after the target lock so we can read the target's sync-history over SSH.
            if not await self._check_out_of_order():
                raise SyncAbortedByUser("Sync aborted at the out-of-order / target-state check")

            # Phase 4: Job discovery and validation
            self._logger.info("Discovering and validating jobs", extra={"job": "orchestrator", "host": "source"})
            jobs = await self._discover_and_validate_jobs()
            # Correct the total now that we know the exact jobs that will run.
            # Disabled jobs and undiscoverable jobs must not inflate the denominator,
            # so the final set_current_step(8 + len(jobs) + 1) reaches exactly 100%.
            self._ui.set_total_steps(8 + len(jobs) + 1)
            self._ui.set_current_step(4, "Discover jobs")

            # Phase 5: Disk space preflight check
            await self._check_disk_space_preflight()
            self._ui.set_current_step(5, "Disk check")

            # Phase 6: Pre-sync snapshots
            self._logger.info("Creating pre-sync snapshots", extra={"job": "orchestrator", "host": "source"})
            await self._create_snapshots(SnapshotPhase.PRE)
            self._ui.set_current_step(6, "Pre-sync snapshots")

            # Phase 7: Install/upgrade pc-switcher on target (after snapshots for rollback safety)
            self._logger.info(
                "Ensuring pc-switcher is installed on target",
                extra={"job": "orchestrator", "host": "target"},
            )
            await self._install_on_target_job()
            self._ui.set_current_step(7, "Install on target")

            # Phase 8: Sync config from source to target
            self._logger.info("Syncing configuration to target", extra={"job": "orchestrator", "host": "target"})
            await self._sync_config_to_target()
            self._ui.set_current_step(8, "Sync config")

            # Phase 9: Execute sync jobs with background monitoring
            self._logger.info("Starting sync operations", extra={"job": "orchestrator", "host": "source"})
            job_results = await self._execute_jobs(jobs)
            session.job_results = job_results

            # Phase 10: Post-sync snapshots
            self._logger.info("Creating post-sync snapshots", extra={"job": "orchestrator", "host": "source"})
            await self._create_snapshots(SnapshotPhase.POST)
            self._ui.set_current_step(8 + len(jobs) + 1, "Post-sync snapshots")

            # Success - update sync history on both machines
            session.status = SessionStatus.COMPLETED
            session.ended_at = datetime.now(UTC)
            self._logger.info("Sync completed successfully", extra={"job": "orchestrator", "host": "source"})

            # Update sync history: this machine was SOURCE, target was TARGET.
            # Skipped in dry-run mode (D-12: dry-run must not write any state).
            if not self._dry_run:
                await self._update_sync_history()

            return session

        except asyncio.CancelledError:
            session.status = SessionStatus.INTERRUPTED
            session.ended_at = datetime.now(UTC)
            session.error_message = "Sync interrupted by user (SIGINT)"
            self._logger.warning("Sync interrupted by user", extra={"job": "orchestrator", "host": "source"})
            raise

        except SyncAbortedByUser as e:
            # A declined confirmation is expected control flow, not a failure:
            # log once at WARNING (never CRITICAL) and re-raise so the CLI can
            # set a non-zero exit code without re-printing a "failed" message.
            session.status = SessionStatus.ABORTED
            session.ended_at = datetime.now(UTC)
            session.error_message = str(e)
            self._logger.warning("Sync aborted by user: %s", e, extra={"job": "orchestrator", "host": "source"})
            raise

        except SyncLockedError as e:
            # A lock conflict is an expected, retryable condition (another sync is
            # running), not an unrecoverable failure: log once at WARNING (never
            # CRITICAL) and re-raise so the CLI reports it once with its unblock hint.
            session.status = SessionStatus.ABORTED
            session.ended_at = datetime.now(UTC)
            session.error_message = str(e)
            self._logger.warning("Sync blocked: %s", e, extra={"job": "orchestrator", "host": "source"})
            raise

        except Exception as e:
            session.status = SessionStatus.FAILED
            session.ended_at = datetime.now(UTC)
            session.error_message = str(e)
            # A job failure is already logged with its job name by the per-job
            # handler; only log here for causes not yet reported (every non-job
            # phase, and — once jobs run in parallel — job failures that bypass
            # the sequential per-job handler), so the same cause isn't doubled.
            if not _failure_already_logged(e):
                self._logger.critical("Sync failed: %s", e, extra={"job": "orchestrator", "host": "source"})
            raise

        finally:
            # Cleanup
            await self._cleanup()

    async def _acquire_source_lock(self) -> None:
        """Acquire exclusive lock on source machine.

        Uses unified lock file that prevents this machine from participating
        in any other sync (as source or target) while this sync is running.
        """
        self._source_lock = SyncLock(get_lock_path())

        holder_info = f"source:{self._source_hostname}:{self._session_id}:pid={os.getpid()}"
        if not self._source_lock.acquire(holder_info):
            existing_holder = self._source_lock.get_holder_info()
            raise SyncLockedError(
                f"This machine is already involved in a sync (held by: {existing_holder}).\n"
                f"{_stuck_lock_hint('this machine', str(get_lock_path()))}"
            )

    async def _establish_connection(self) -> None:
        """Establish SSH connection to target machine."""
        self._connection = Connection(self._target_hostname, event_bus=self._event_bus)
        await self._connection.connect()

        # Create executors
        self._local_executor = LocalExecutor()
        self._remote_executor = RemoteExecutor(self._connection.ssh_connection)

        self._logger.info("Connected to target", extra={"job": "orchestrator", "host": "target"})

    async def _acquire_target_lock(self) -> None:
        """Acquire exclusive lock on target machine via SSH.

        Uses the same unified lock file as the source, ensuring the target
        machine cannot participate in any other sync while this one runs.
        """
        assert self._remote_executor is not None

        self._target_lock_process = await start_persistent_remote_lock(
            self._remote_executor, self._source_hostname, self._session_id
        )
        if self._target_lock_process is None:
            raise SyncLockedError(
                f"Target {self._target_hostname} is already involved in a sync.\n"
                f"{_stuck_lock_hint(self._target_hostname, '~/.local/share/pc-switcher/pc-switcher.lock')}"
            )

    async def _install_on_target_job(self) -> None:
        """Execute InstallOnTargetJob to ensure pc-switcher is on target.

        Runs AFTER pre-sync snapshots for rollback safety if installation fails.
        """
        context = self._create_job_context({})
        install_job = InstallOnTargetJob(context)

        # Validate first (though it just returns empty list)
        errors = await install_job.validate()
        if errors:
            error_msgs = [f"  - {e.host.value}: {e.message}" for e in errors]
            raise RuntimeError("Installation validation failed:\n" + "\n".join(error_msgs))

        # Execute
        await install_job.execute()

    async def _sync_config_to_target(self) -> None:
        """Sync configuration from source to target machine.

        Handles three scenarios:
        1. Target has no config: Display source config, prompt for confirmation
        2. Target config differs: Display diff, offer three choices
        3. Target config matches: Skip silently

        Raises:
            SyncAbortedByUser: If the user declines the config sync confirmation.
            RuntimeError: If config sync fails for a reason other than user decline.
        """
        assert self._remote_executor is not None
        assert self._console is not None

        source_config_path = Configuration.get_default_config_path()

        should_continue = await sync_config_to_target(
            target=self._remote_executor,
            source_config_path=source_config_path,
            ui=self._ui,
            console=self._console,
            auto_accept=self._auto_accept,
            dry_run=self._dry_run,
        )

        if not should_continue:
            raise SyncAbortedByUser("Config sync aborted by user")

        self._logger.info("Configuration sync completed", extra={"job": "orchestrator", "host": "target"})

    def _resolve_sync_job_class(self, job_name: str) -> type[SyncJob] | None:
        """Resolve the SyncJob subclass registered for `job_name`.

        Convention: job_name == module_name (e.g., "dummy_success" → pcswitcher.jobs.dummy_success).
        Dynamically imports the module and scans its attributes for a SyncJob subclass whose
        `name` ClassVar matches. Shared by `_discover_and_validate_jobs` (Phase 4 job discovery)
        and `_first_sync_scopes` (pre-Phase-4 first-sync messaging) so the import/scan logic
        lives in exactly one place.

        Returns:
            The matching SyncJob subclass, or None if the module doesn't exist or no matching
            class is found (a warning is logged in either case).
        """
        try:
            module = importlib.import_module(f"pcswitcher.jobs.{job_name}")
        except ModuleNotFoundError:
            self._logger.warning(
                "Job module pcswitcher.jobs.%s not found",
                job_name,
                extra={"job": "orchestrator", "host": "source"},
            )
            return None

        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, SyncJob)
                and attr is not SyncJob
                and getattr(attr, "name", None) == job_name
            ):
                return attr

        self._logger.warning(
            "No SyncJob with name=%s found in module pcswitcher.jobs.%s",
            job_name,
            job_name,
            extra={"job": "orchestrator", "host": "source"},
        )
        return None

    def _first_sync_scopes(self) -> list[FirstSyncScope]:
        """Collect each enabled sync job's self-described first-sync overwrite scope (ADR-015).

        Resolves every enabled job in `self._config.sync_jobs` (config order) to its SyncJob
        class via `_resolve_sync_job_class`, then calls `describe_first_sync_scope()` on each —
        this runs before Phase 4 job discovery, so classes (not instances) are used. Jobs that
        return None (no overwrite scope, or nothing in scope for their config) contribute
        nothing; the orchestrator's warning falls back to generic phrasing when this is empty.
        """
        scopes: list[FirstSyncScope] = []
        for job_name, enabled in self._config.sync_jobs.items():
            if not enabled:
                continue
            job_class = self._resolve_sync_job_class(job_name)
            if job_class is None:
                continue
            scope = job_class.describe_first_sync_scope(self._config.job_configs.get(job_name, {}))
            if scope is not None:
                scopes.append(scope)
        return scopes

    @staticmethod
    def _dry_run_preview_hint(tgt: str) -> str:
        """Job-agnostic dry-run guidance shared by the first-sync and out-of-order warnings.

        Stays job-neutral ("what would change", not "deleted") because the orchestrator
        coordinates jobs beyond folder-sync (packages, system-config, …) where a change
        is not a file deletion. Points at the log file because the live TUI only shows
        summary counts — the per-item detail (files copied/removed, etc.) is written to
        the log, not the Recent Logs panel.
        """
        return (
            "Run [bold]pc-switcher sync --dry-run[/bold] first to preview what would change on "
            f"[bold]{tgt}[/bold]; the per-item detail is written to the log file "
            "([bold]pc-switcher logs[/bold] shows the log directory)."
        )

    async def _confirm_first_sync(self) -> bool:
        """Confirm the overwrite of a target that has never been synced (first sync).

        A first sync (no readable target sync-history) is semantically distinct from an
        out-of-order sync: there is no prior topology to reconcile, the destructive transfer
        simply replaces everything in scope of the configured sync jobs on the target. Because
        this question is common to all jobs, it is asked once here (after the target lock)
        rather than per-job — and each in-scope job describes its own scope and overwrite
        mechanism (ADR-015), so this method names no job and no transport mechanism directly.

        Gated by --allow-first-sync (distinct from the W2/W3 --allow-out-of-order gate).
        Under --dry-run the warning is logged but never aborts (ADR-014).

        Returns:
            True if the sync should proceed, False if the user declined.
        """
        assert self._confirmer is not None

        tgt = self._target_hostname
        scopes = self._first_sync_scopes()
        if scopes:
            scope_line = "\n\n".join(
                f"  {scope.job_name} ({scope.mechanism}):\n" + "\n".join(f"    {item}" for item in scope.scope_items)
                for scope in scopes
            )
        else:
            scope_line = "  (all data configured for sync)"
        warn_title = "First Sync — Target Will Be Overwritten"
        warning = (
            f"[bold]{tgt}[/bold] has never been synced by pc-switcher (no sync history).\n\n"
            "This first-ever sync will overwrite everything on the target that is in scope of "
            "the configured sync jobs, except configured exclusions. In scope:\n\n"
            f"{scope_line}\n\n"
            f"Any independent data on [bold]{tgt}[/bold] within that scope will be lost.\n\n"
            + self._dry_run_preview_hint(tgt)
        )

        if self._dry_run:
            # ADR-014: dry-run is a read-only rehearsal — log the warning, never abort.
            self._logger.warning(
                "%s — skipping confirmation in dry-run mode",
                warn_title,
                extra={"job": "orchestrator", "host": "target"},
            )
            return True

        return await self._confirmer.confirm(
            title=warn_title,
            message=warning,
            allow=self._allow_first_sync,
            allow_flag="--allow-first-sync",
            log_extra={"job": "orchestrator", "host": "target"},
        )

    async def _check_out_of_order(self) -> bool:
        """Pre-flight target-state check run after the target lock (reads target sync-history over SSH).

        Reads `last_role`/`last_peer` from both machines once, then dispatches to the
        confirmation appropriate to the situation. Two independent gates:

        - W1 (first sync): target has no readable sync-history. Overwriting an untracked
          target is a distinct question with its own flag — handled by
          `_confirm_first_sync`, gated by --allow-first-sync.
        - W2/W3 (out-of-order): target last synced with a different machine (machine-C),
          or this source is pushing again without a back-sync (GitHub #159). Gated by
          --allow-out-of-order.

        The clean A→B / work / B→A / A→B pattern always proceeds silently. All prompts go
        through the shared Confirmer; under --dry-run every gate logs and proceeds
        (ADR-014). Both checks live in the orchestrator so the overwrite question is asked
        once centrally rather than per-job (ADR-015).

        Returns:
            True if sync should proceed, False if aborted.
        """
        assert self._remote_executor is not None
        assert self._confirmer is not None

        src = self._source_hostname
        tgt = self._target_hostname

        # Read local sync state (role + peer from this machine's sync-history.json)
        local_role, local_peer = get_last_sync_state()

        # Read target sync state over SSH; failure or empty output → no readable history
        cat_result = await self._remote_executor.run_command(f"cat {HISTORY_PATH} 2>/dev/null")
        target_stdout = cat_result.stdout.strip()
        target_role, target_peer = (
            parse_sync_state(target_stdout) if cat_result.success and target_stdout else (None, None)
        )

        # W1: no readable/parseable target history → first-ever sync (own flag).
        if target_role is None:
            return await self._confirm_first_sync()

        # W2/W3 (out-of-order) — bypassed by --allow-out-of-order.
        if self._allow_out_of_order:
            self._logger.info(
                "Out-of-order topology check bypassed by --allow-out-of-order",
                extra={"job": "orchestrator", "host": "source"},
            )
            return True

        # Consecutive push: this source most recently synced TO this same target
        consecutive_push = local_role == SyncRole.SOURCE and local_peer == tgt

        # Suppress (clean case): target last synced with this source, and this is
        # not a repeat push from the same source without a back-sync.
        if target_peer == src and not consecutive_push:
            return True

        # Determine warning type and compose message
        if target_peer is not None and target_peer != src:
            # W2: machine-C — target last synced with a third machine
            direction = "received a sync from" if target_role == SyncRole.TARGET else "sent a sync to"
            warn_title = "Target Last Synced with a Different Machine"
            warning = (
                f"[bold]{tgt}[/bold] most recently {direction} [bold]{target_peer}[/bold], "
                f"not this machine ([bold]{src}[/bold]).\n\n"
                f"Proceeding will overwrite that state. If [bold]{target_peer}[/bold] "
                f"pushed independent changes to [bold]{tgt}[/bold], those changes will be lost.\n\n"
                + self._dry_run_preview_hint(tgt)
            )
        else:
            # W3: consecutive push — target looks clean but this source is pushing again
            warn_title = "Consecutive Sync — No Back-Sync Received"
            warning = (
                f"You are syncing from [bold]{src}[/bold] to [bold]{tgt}[/bold] again "
                "without receiving a sync back first.\n\n"
                f"[bold]{tgt}[/bold] shows it last synced with this machine. "
                f"If you made changes on [bold]{tgt}[/bold] since then and have not "
                "synced them back, those changes will be lost.\n\n" + self._dry_run_preview_hint(tgt)
            )

        if self._dry_run:
            # ADR-014: dry-run is a read-only rehearsal — log the warning, never abort
            self._logger.warning(
                "%s — skipping confirmation in dry-run mode",
                warn_title,
                extra={"job": "orchestrator", "host": "source"},
            )
            return True

        return await self._confirmer.confirm(
            title=warn_title,
            message=warning,
            allow=self._allow_out_of_order,
            allow_flag="--allow-out-of-order",
            log_extra={"job": "orchestrator", "host": "source"},
        )

    async def _update_sync_history(self) -> None:
        """Update sync history on both source and target machines.

        After a successful sync:
        - Source machine's history: last_role = SOURCE, last_peer = target hostname
        - Target machine's history: last_role = TARGET, last_peer = source hostname

        Recording `last_peer` on both ends enables the topology out-of-order check
        to distinguish the clean A→B / B→A pattern from the machine-C and
        consecutive-push cases on the next sync.

        Raises:
            RuntimeError: If history update fails on either machine.
        """
        # Update local (source) history
        record_role(SyncRole.SOURCE, peer=self._target_hostname)
        self._logger.debug("Updated sync history: role=source", extra={"job": "orchestrator", "host": "source"})

        # Update remote (target) history via SSH
        if self._remote_executor is not None:
            cmd = get_record_role_command(SyncRole.TARGET, peer=self._source_hostname)
            result = await self._remote_executor.run_command(cmd)
            if not result.success:
                raise RuntimeError(f"Failed to update sync history on target: {result.stderr}")
            self._logger.debug("Updated sync history: role=target", extra={"job": "orchestrator", "host": "target"})

    async def _discover_and_validate_jobs(self) -> list[Job]:
        """Discover enabled jobs from config and validate their configuration.

        Dynamically imports job modules based on enabled jobs in config.
        Convention: job_name == module_name (e.g., "dummy_success" → pcswitcher.jobs.dummy_success)

        Returns:
            List of job instances ready for execution

        Raises:
            RuntimeError: If any job config validation fails
        """
        jobs: list[Job] = []
        config_errors: list[ConfigError] = []

        # Log entire config at DEBUG level
        self._logger.debug(
            "Configuration loaded",
            extra={
                "job": "orchestrator",
                "host": "source",
                "logging_file": self._config.logging.file,
                "logging_tui": self._config.logging.tui,
                "logging_external": self._config.logging.external,
                "sync_jobs": self._config.sync_jobs,
                "disk_preflight_minimum": self._config.disk.preflight_minimum,
                "disk_runtime_minimum": self._config.disk.runtime_minimum,
                "disk_warning_threshold": self._config.disk.warning_threshold,
                "disk_check_interval": self._config.disk.check_interval,
                "btrfs_subvolumes": self._config.btrfs_snapshots.subvolumes,
                "btrfs_keep_recent": self._config.btrfs_snapshots.keep_recent,
                "btrfs_max_age_days": self._config.btrfs_snapshots.max_age_days,
            },
        )

        # Lazy load only enabled jobs (job_name == module_name)
        for job_name, enabled in self._config.sync_jobs.items():
            if not enabled:
                self._logger.debug(
                    "Job %s is disabled in config",
                    job_name,
                    extra={"job": "orchestrator", "host": "source"},
                )
                continue

            job_class = self._resolve_sync_job_class(job_name)
            if job_class is None:
                continue

            # Validate job config (Phase 2)
            job_config = self._config.job_configs.get(job_name, {})
            errors = job_class.validate_config(job_config)
            if errors:
                config_errors.extend(errors)
            else:
                context = self._create_job_context(job_config)
                jobs.append(job_class(context))

        # Check for config errors
        if config_errors:
            error_msgs = [f"  - {e.job}: {e.path} - {e.message}" for e in config_errors]
            raise RuntimeError("Job configuration validation failed:\n" + "\n".join(error_msgs))

        # Validate system state for all jobs (Phase 3)
        validation_errors: list[ValidationError] = []
        for job in jobs:
            errors = await job.validate()
            if errors:
                validation_errors.extend(errors)

        if validation_errors:
            error_msgs = [f"  - {e.job} ({e.host.value}): {e.message}" for e in validation_errors]
            raise RuntimeError("System state validation failed:\n" + "\n".join(error_msgs))

        return jobs

    async def _check_disk_space_preflight(self) -> None:
        """Check disk space on both source and target before creating snapshots.

        Per CORE-FR-DISK-PRE, verifies both hosts have sufficient free disk space
        based on the configured preflight_minimum threshold.

        Raises:
            RuntimeError: If either host has insufficient disk space
        """
        assert self._local_executor is not None
        assert self._remote_executor is not None

        self._logger.info("Checking disk space on both hosts", extra={"job": "orchestrator", "host": "source"})

        # Parse threshold once (same for both hosts)
        threshold_type, threshold_value = parse_threshold(self._config.disk.preflight_minimum)

        # Check both hosts in parallel
        source_task = check_disk_space(self._local_executor, "/")
        target_task = check_disk_space(self._remote_executor, "/")
        source_disk, target_disk = await asyncio.gather(source_task, target_task)

        # Helper to format bytes in human-readable form
        def format_bytes(bytes_value: int) -> str:
            value = float(bytes_value)
            for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
                if value < 1024:
                    return f"{value:.1f}{unit}"
                value /= 1024
            return f"{value:.1f}PiB"

        # Helper to check if disk space is sufficient
        def is_sufficient(disk_space: DiskSpace, threshold_type: str, threshold_value: int) -> bool:
            if threshold_type == "percent":
                # Threshold is percentage of total disk that must be free
                free_percent = (disk_space.available_bytes / disk_space.total_bytes) * 100
                return free_percent >= threshold_value
            else:  # bytes
                return disk_space.available_bytes >= threshold_value

        # Helper to format free space description
        def format_free_space(disk_space: DiskSpace) -> str:
            free_bytes = format_bytes(disk_space.available_bytes)
            free_percent = (disk_space.available_bytes / disk_space.total_bytes) * 100
            return f"{free_bytes} ({free_percent:.1f}%)"

        # Helper to format threshold description
        def format_threshold(threshold_type: str, threshold_value: int) -> str:
            if threshold_type == "percent":
                return f"{threshold_value}%"
            else:  # bytes
                return format_bytes(threshold_value)

        # Check source
        if not is_sufficient(source_disk, threshold_type, threshold_value):
            free_space_desc = format_free_space(source_disk)
            threshold_desc = format_threshold(threshold_type, threshold_value)
            error_msg = f"Source disk space {free_space_desc} below threshold {threshold_desc}"
            self._logger.critical(error_msg, extra={"job": "orchestrator", "host": "source"})
            raise RuntimeError(error_msg)

        # Check target
        if not is_sufficient(target_disk, threshold_type, threshold_value):
            free_space_desc = format_free_space(target_disk)
            threshold_desc = format_threshold(threshold_type, threshold_value)
            error_msg = f"Target disk space {free_space_desc} below threshold {threshold_desc}"
            self._logger.critical(error_msg, extra={"job": "orchestrator", "host": "target"})
            raise RuntimeError(error_msg)

        # Both checks passed - log success
        source_free = format_free_space(source_disk)
        target_free = format_free_space(target_disk)
        self._logger.info(
            "Source disk space check passed: %s free",
            source_free,
            extra={"job": "orchestrator", "host": "source"},
        )
        self._logger.info(
            "Target disk space check passed: %s free",
            target_free,
            extra={"job": "orchestrator", "host": "target"},
        )

    async def _create_snapshots(self, phase: SnapshotPhase) -> None:
        """Create btrfs snapshots on both source and target.

        Args:
            phase: PRE or POST snapshot phase
        """
        snapshot_config = {
            "phase": phase.value,
            "subvolumes": self._config.btrfs_snapshots.subvolumes,
            "session_folder": self._session_folder,
        }
        context = self._create_job_context(snapshot_config)
        snapshot_job = BtrfsSnapshotJob(context)

        # Validate first
        errors = await snapshot_job.validate()
        if errors:
            error_msgs = [f"  - {e.host.value}: {e.message}" for e in errors]
            raise RuntimeError("Snapshot validation failed:\n" + "\n".join(error_msgs))

        # Execute
        await snapshot_job.execute()

    async def _execute_jobs(self, jobs: list[Job]) -> list[JobResult]:
        """Execute sync jobs sequentially with background disk monitoring.

        Args:
            jobs: List of validated jobs to execute

        Returns:
            List of JobResult for each executed job
        """
        results: list[JobResult] = []

        try:
            await self._run_jobs_in_task_group(jobs, results)
        except BaseExceptionGroup as eg:
            # A job failing in the TaskGroup body raises an ExceptionGroup whose
            # own message ("unhandled errors in a TaskGroup (N sub-exceptions)")
            # is useless. Re-raise the underlying cause so run()'s handlers and
            # the CLI report the real reason — and so a job-raised
            # SyncAbortedByUser/SyncLockedError still reaches its WARNING handler.
            raise _unwrap_taskgroup_error(eg) from None

        return results

    async def _run_jobs_in_task_group(self, jobs: list[Job], results: list[JobResult]) -> None:
        """Run the disk-space monitors and sync jobs inside a single TaskGroup.

        Extracted from ``_execute_jobs`` so the caller can unwrap the
        ExceptionGroup this raises when a job fails (see ``_unwrap_taskgroup_error``).
        """
        assert self._ui is not None

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg

            # Start background disk space monitors for root filesystem
            monitor_config = {
                "preflight_minimum": self._config.disk.preflight_minimum,
                "runtime_minimum": self._config.disk.runtime_minimum,
                "warning_threshold": self._config.disk.warning_threshold,
                "check_interval": self._config.disk.check_interval,
            }
            monitor_context = self._create_job_context(monitor_config)
            source_monitor = DiskSpaceMonitorJob(monitor_context, host=Host.SOURCE, mount_point="/")
            target_monitor = DiskSpaceMonitorJob(monitor_context, host=Host.TARGET, mount_point="/")

            # Start monitors and save tasks for later cancellation
            source_monitor_task = tg.create_task(source_monitor.execute())
            target_monitor_task = tg.create_task(target_monitor.execute())

            try:
                # Execute sync jobs sequentially
                for job_index, job in enumerate(jobs):
                    # Update step counter (base 8 system steps + current job index),
                    # labelled with the job name so the TUI shows what is running.
                    self._ui.set_current_step(8 + job_index + 1, job.name)
                    started_at = datetime.now(UTC)
                    try:
                        await job.execute()
                        ended_at = datetime.now(UTC)
                        results.append(
                            JobResult(
                                job_name=job.name,
                                status=JobStatus.SUCCESS,
                                started_at=started_at,
                                ended_at=ended_at,
                            )
                        )
                        self._logger.info(
                            "Job %s completed successfully",
                            job.name,
                            extra={"job": "orchestrator", "host": "source"},
                        )

                    except SyncAbortedByUser:
                        # A job-level declined confirmation (e.g. FolderSyncJob's
                        # first-sync overwrite gate via the shared confirmer) is
                        # expected control flow, not a job failure. Let it pass
                        # through untouched so run() logs it once at WARNING and
                        # records an ABORTED session, rather than a spurious
                        # FAILED job result plus a duplicate CRITICAL log.
                        raise
                    except Exception as e:
                        ended_at = datetime.now(UTC)
                        results.append(
                            JobResult(
                                job_name=job.name,
                                status=JobStatus.FAILED,
                                started_at=started_at,
                                ended_at=ended_at,
                                error_message=str(e),
                            )
                        )
                        self._logger.critical(
                            "Job %s failed: %s",
                            job.name,
                            e,
                            extra={"job": "orchestrator", "host": "source"},
                        )
                        # Already reported with the job name; stop run()'s top-level
                        # handler from logging the identical cause a second time.
                        _mark_failure_logged(e)
                        raise
            finally:
                # Cancel monitor tasks so TaskGroup can exit
                # Monitors run forever (while True loop), so they must be cancelled
                source_monitor_task.cancel()
                target_monitor_task.cancel()

    async def _cleanup(self) -> None:
        """Clean up resources (connection, locks, executors)."""
        self._cleanup_in_progress = True

        # Release target lock first (before terminating other processes)
        if self._target_lock_process is not None:
            await release_remote_lock(self._target_lock_process)

        # Terminate all processes
        if self._local_executor is not None:
            await self._local_executor.terminate_all_processes()
        if self._remote_executor is not None:
            await self._remote_executor.terminate_all_processes()

        # Kill remote processes (critical for SIGINT handling)
        if self._connection is not None:
            await self._connection.kill_all_remote_processes()

        # Close connection
        if self._connection is not None:
            await self._connection.disconnect()

        # Release source lock
        if self._source_lock is not None:
            self._source_lock.release()

        # Close event bus (sends None sentinel to all consumers)
        self._event_bus.close()

        # Stop QueueListener for stdlib logging (flushes pending log records)
        if self._queue_listener is not None:
            self._queue_listener.stop()

        # Wait for UI task to finish draining its queue
        if self._ui_task is not None:
            await self._ui_task

        # Stop UI live display
        if self._ui is not None:
            self._ui.stop()

        # Resurface captured warnings into scrollback, after the Live has fully
        # stopped so the block cannot be overwritten by a later refresh. This is
        # the load-bearing guarantee that warnings which scrolled past in the
        # rolling Recent Logs panel are still seen — on success as well as
        # failure. Naturally a no-op outside the interactive path (nothing is
        # captured there; warnings already went to stderr).
        self._print_warning_summary()

    def _print_warning_summary(self) -> None:
        """Print a static end-of-run block listing every captured `>=WARNING` line.

        Each message is wrapped in a Rich `Text` (not markup) so arbitrary log
        content — rsync paths, stderr containing `[...]`/`[/...]` — renders
        literally instead of raising MarkupError, mirroring the Recent Logs panel.
        """
        if self._ui is None or self._console is None:
            return
        warnings = self._ui.collected_warnings()
        if not warnings:
            return

        count = len(warnings)
        self._console.print()
        self._console.print(Text(f"⚠ {count} warning(s) this run:", style="bold yellow"))
        for line in warnings:
            self._console.print(Text(f"  {line}", style="yellow"))
        self._console.print("[dim]Run [bold]pc-switcher logs[/bold] for the full log.[/dim]")
