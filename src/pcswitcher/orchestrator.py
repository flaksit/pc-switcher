"""Core orchestrator coordinating the complete sync workflow."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import secrets
import shlex
from datetime import UTC, datetime
from enum import IntEnum
from logging.handlers import QueueListener
from typing import Any

from rich.console import Console
from rich.text import Text

from pcswitcher.btrfs_snapshots import session_folder_name
from pcswitcher.config import Configuration
from pcswitcher.config_sync import sync_config_to_target
from pcswitcher.confirmer import Confirmer, TerminalUIConfirmer
from pcswitcher.connection import Connection
from pcswitcher.disk import DiskSpace, check_disk_space, format_bytes, parse_threshold
from pcswitcher.events import EventBus
from pcswitcher.executor import LocalExecutor, RemoteExecutor, RemoteProcess, active_job
from pcswitcher.jobs.base import Job, SyncJob
from pcswitcher.jobs.btrfs import BtrfsSnapshotJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.disk_space_monitor import DiskSpaceMonitorJob
from pcswitcher.jobs.install_on_target import InstallOnTargetJob
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import Reviewer, TerminalUIReviewer
from pcswitcher.jobs.packages.sync_core import PackageItemFailures, PackageSyncJob
from pcswitcher.lock import (
    SyncLock,
    get_hostname_command,
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
    CommandResult,
    ConfigError,
    FirstSyncScope,
    Host,
    JobResult,
    JobSkipped,
    JobStatus,
    SessionStatus,
    SnapshotPhase,
    SyncAborted,
    SyncAbortedByUser,
    SyncLockedError,
    SyncSession,
    ValidationError,
)
from pcswitcher.step_gate import StepGate, TerminalUIStepGate
from pcswitcher.sync_history import (
    HISTORY_PATH,
    SyncRole,
    get_last_sync_state,
    get_record_role_command,
    hostnames_equal,
    parse_sync_state,
    record_role,
)
from pcswitcher.ui import TerminalUI

__all__ = ["Orchestrator"]

# Transient snapd auto-refresh hold applied around the RUN_JOBS window (decision 4,
# 02-UAT-REVIEW-FIXES). snapd auto-refreshes ~4x/day even for closed apps; an auto-refresh
# landing between snap_sync's revision convergence and folder_sync mirroring the matching
# ~/snap/<app>/<current-rev> data dir would desync the two machines. Pausing it closes that
# window on BOTH hosts. Mechanism: write the system-wide `refresh.hold` option (the same one
# snap_sync.validate reads read-only), which gates ONLY the auto-refresh manager — the manual
# `snap install/refresh --revision=N` convergence is unaffected (verified against snapd
# overlord/snapstate/autorefresh.go: EffectiveRefreshHold is consumed only by autoRefresh).
# It is written as a TIMED timestamp (now + _SNAP_AUTOREFRESH_HOLD_DURATION) so a crashed sync
# self-heals when the hold expires; the prior refresh.hold is captured and restored exactly in
# _cleanup (D-06: no standing block left behind). Deliberately NOT the `snap refresh --hold`
# verb, whose no-snap form sets an INDEFINITE global hold (snap_sync module docstring, Pitfall
# 1) and whose `--unhold` would also clear unrelated per-snap holds; `snap set system
# refresh.hold` touches only the general option and is fully symmetric with `snap get` —
# including its privilege, since snapd admin-gates reading snap config too (_capture_snap_hold).
_SNAP_AUTOREFRESH_HOLD_DURATION = "+6 hours"
# Shell snippet computing an RFC3339-UTC "now + hold duration" timestamp ON THE HOST (correct
# against each host's own clock, and parseable by snapd's refresh.hold RFC3339 validator).
_SNAP_HOLD_TIMESTAMP_CMD = f"date --utc --date='{_SNAP_AUTOREFRESH_HOLD_DURATION}' +%Y-%m-%dT%H:%M:%SZ"
# snapd's error for an option that was never set (`snap "core" has no "refresh.hold"
# configuration option`), as opposed to any other `snap get` failure. It is what separates
# "there is no hold" from "the hold could not be read" — a distinction the value alone cannot
# carry, and the one that decides whether the option may be cleared (`_restore_snap_hold`).
_SNAP_HOLD_UNSET_MARKER = 'has no "refresh.hold"'


class SyncStep(IntEnum):
    """The fixed, ordered sequence of top-level steps in a sync (see run()).

    Distinct from the smaller, local "step N" numbering inside individual jobs and
    functions (config loading, folder_sync validation): those are private to their
    routine, this is the one sequence the TUI counts and the docs refer to.

    Values are assigned explicitly (1..N) so the numbers are stable, referenceable
    anchors in the user-facing docs and UI ("Step 4", "Step 12/12"); when inserting
    a step, renumber the following members to match. `len(SyncStep)` is the total,
    which the orchestrator hands to the TUI — no separate magic number.

    `RUN_JOBS` is a single step; the TUI expands it into sub-steps (10a, 10b, …),
    one per enabled job, without inflating the total.
    """

    SOURCE_LOCK = 1
    CONNECT = 2
    TARGET_LOCK = 3
    OUT_OF_ORDER_CHECK = 4
    DISCOVER_JOBS = 5
    DISK_CHECK = 6
    PRE_SNAPSHOT = 7
    INSTALL_ON_TARGET = 8
    SYNC_CONFIG = 9
    RUN_JOBS = 10
    POST_SNAPSHOT = 11
    RECORD_HISTORY = 12


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
        f"terminating the holder process (e.g. `fuser --kill {lock_path}` or `pkill --full pc-switcher.lock`), "
        f"not by deleting the lock file."
    )


def _unwrap_taskgroup_error(exc: BaseException) -> BaseException:
    """Flatten an ``asyncio.TaskGroup`` ExceptionGroup to its primary cause.

    A job that fails inside the job-execution TaskGroup surfaces as an
    ExceptionGroup whose own message — "unhandled errors in a TaskGroup (N
    sub-exceptions)" — is meaningless to users and developers alike. Jobs run
    sequentially, so a failed sync normally has a single underlying cause; this
    returns it so callers can report the real reason. Expected control-flow
    exceptions (an abort or a lock conflict raised from within a job)
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
        if isinstance(leaf, (SyncAborted, SyncLockedError)):
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


def _failure_stays_in_its_job(job: Job, exc: Exception) -> bool:
    """Whether `exc` fails only `job` and leaves the remaining jobs to run.

    What isolates a failure is the JOB it came out of, not the exception class. The rule
    package sync states is unqualified — one failed job does not stop the others
    (`PKG-FR-JOB-INDEPENDENCE`, `PKG-FR-OUTCOME-FAILED`) — and a package job can fail in
    ways that are not a converge failure or a dead read: a registry transfer, a filesystem
    error, a parser defect. Each package job plans, reviews and applies its own work with
    nothing coordinating them (D-15/D-16), so none of those say anything about the consent
    the user already gave to another manager; cancelling it would discard approved work for
    a job that is still fine. `PackageItemFailures` and `ProbeFailed` isolate wherever they
    are raised, because both are by construction one manager's trouble (D-27, ADR-022).

    A lock conflict is the exception to the exception: it means this machine is no longer
    entitled to be syncing at all, so it ends the run whichever job surfaced it.

    Jobs outside package sync — `folder_sync`, `vscode_state_sync`, the core jobs — still
    abort the run. Which of those may survive a failure is GitHub issue #220.
    """
    if isinstance(exc, SyncLockedError):
        return False
    return isinstance(job, PackageSyncJob) or isinstance(exc, (PackageItemFailures, ProbeFailed))


def _summarize_job_outcomes(job_results: list[JobResult]) -> tuple[SessionStatus, str | None]:
    """Derive the session outcome from the collected job results.

    Reaching the end of the job loop without an exception is not the same as success:
    per-item package failures are collected and recorded as FAILED ``JobResult``s rather
    than raised, so that one manager's item failures cannot cancel another manager's
    already-approved work (D-27). The outcome therefore has to come from the results
    themselves, or a sync where every item failed would still exit 0.

    ``SKIPPED`` is a normal outcome for a disabled or not-applicable job, not a failure.

    Each failed job contributes its own recorded reason, not just its name: the end-of-run
    message is what the user reads once the review screens are gone, and a failure has to
    name the item, package or file it concerns wherever it is reported
    (``PKG-FR-OUTCOME-FAILED``, ``PKG-FR-FAIL-NAMED``). The reasons are already one line
    each — ``PackageItemFailures`` names every failed item on a single line — so a job with
    forty failed items adds one line here, not forty, and the message stays as long as the
    number of failed jobs.
    """
    failures = [r for r in job_results if r.status is JobStatus.FAILED]
    if not failures:
        return SessionStatus.COMPLETED, None
    lines = [f"{r.job_name} — {r.error_message or 'no reason recorded'}" for r in failures]
    return SessionStatus.FAILED, "\n".join(lines)


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
        confirm_each_command: bool = False,
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
            confirm_each_command: If True, prompt before every individual modification a job
                makes (`--confirm-each-command`). Requires a TTY; `cli.sync` refuses the flag
                without one, so the orchestrator can assume the prompt is answerable.
        """
        self._config = config
        self._auto_accept = auto_accept
        self._allow_out_of_order = allow_out_of_order
        self._allow_first_sync = allow_first_sync
        self._dry_run = dry_run
        self._confirm_each_command = confirm_each_command
        self._session_id = secrets.token_hex(4)
        self._session_folder = session_folder_name(self._session_id)
        self._source_hostname = get_local_hostname()
        # SSH-connectable target: the raw CLI argument (hostname, SSH alias, or IP).
        # Load-bearing as the rsync/SSH destination — never overwrite with a resolved name.
        self._target_hostname = target
        # Target's own hostname, resolved over SSH the same way the source resolves its
        # own (socket.gethostname()), so sync-history peers and the topology check compare
        # like-for-like instead of matching a real hostname against a typed CLI argument.
        # Falls back to the CLI argument until _establish_connection resolves it.
        self._target_canonical_hostname = target

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

        # Snap auto-refresh hold state (decision 4). Engaged only when snap_sync is enabled
        # on a non-dry-run; the captured prior `refresh.hold` per host is restored (or unset)
        # in _cleanup. `_snap_hold_engaged` gates the restore so it is a no-op on runs that
        # never set a hold, and idempotent if _cleanup were entered twice. `_readable_` records
        # whether the pre-sync READ succeeded, which "no prior hold" (None) alone cannot express;
        # it defaults False so an unread host is neither written (`_hold_snap_autorefresh`) nor
        # cleared (`_restore_snap_hold`).
        self._snap_hold_engaged = False
        self._snap_hold_prior_source: str | None = None
        self._snap_hold_prior_target: str | None = None
        self._snap_hold_readable_source = False
        self._snap_hold_readable_target = False

        # Logging infrastructure (initialized in run())
        self._queue_listener: QueueListener | None = None
        self._ui: TerminalUI | None = None
        self._console: Console | None = None
        self._ui_task: asyncio.Task[None] | None = None
        self._confirmer: Confirmer | None = None
        self._reviewer: Reviewer | None = None
        # Stays None unless --confirm-each-command was passed; a None gate on the executors
        # is what makes every `mutates=` call site a plain pass-through.
        self._step_gate: StepGate | None = None

    def _create_job_context(self, config: dict[str, Any]) -> JobContext:
        """Create JobContext with current orchestrator state.

        Must only be called after SSH connection is established (SyncStep.CONNECT onward).
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
            reviewer=self._reviewer,
            # Connection is always set when _create_job_context is called in
            # production (SyncStep.CONNECT onward), but unit tests mock executors
            # without a real connection, so fall back to None (JobContext accepts it).
            target_username=self._connection.username if self._connection is not None else None,
            # The full sync_jobs enablement map, not just this job's own section — see
            # JobContext.enabled_sync_jobs docstring.
            enabled_sync_jobs=dict(self._config.sync_jobs),
        )

    def _log_sync_outcome(self, session: SyncSession) -> None:
        """Log the end-of-run outcome at the severity the session status warrants."""
        if session.status is SessionStatus.FAILED:
            self._logger.warning(
                "Sync finished with job failures: %s",
                session.error_message,
                extra={"job": "orchestrator", "host": "source"},
            )
        else:
            self._logger.info("Sync completed successfully", extra={"job": "orchestrator", "host": "source"})

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
        self._console = Console()
        self._ui = TerminalUI(console=self._console)
        # The orchestrator owns the step sequence, so it tells the UI the total.
        # Derived from the enum (not a literal), and fixed regardless of job count.
        self._ui.set_total_steps(len(SyncStep))
        # Shared interactive confirmation gate for the orchestrator's out-of-order check
        # and any job-level prompt (e.g. FolderSyncJob first-sync overwrite, ADR-015).
        self._confirmer = TerminalUIConfirmer(self._console, self._ui, logger=self._logger)
        # Both machine names, because every review screen — and the per-command
        # confirmation, which is also a question the user answers — names the machine an
        # answer acts on rather than its source/target role. The target's is the CLI
        # argument, the same string `JobContext.target_hostname` carries.
        self._reviewer = TerminalUIReviewer(
            self._console,
            self._ui,
            source_hostname=self._source_hostname,
            target_hostname=self._target_hostname,
            logger=self._logger,
        )
        if self._confirm_each_command:
            self._step_gate = TerminalUIStepGate(
                self._console,
                self._ui,
                source_hostname=self._source_hostname,
                target_hostname=self._target_hostname,
                logger=self._logger,
            )

        # Built here rather than beside its remote counterpart in `_establish_connection`,
        # because it needs no connection and the very first thing the run does — taking the
        # source lock (SyncStep 1) — is a modification that has to reach the gate.
        self._local_executor = LocalExecutor(self._step_gate)

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
            # SyncStep 1: Acquire source lock
            self._logger.info("Acquiring source lock", extra={"job": "orchestrator", "host": "source"})
            await self._acquire_source_lock()
            self._ui.set_current_step(SyncStep.SOURCE_LOCK, f"Lock {self._source_hostname}")

            # SyncStep 2: Establish SSH connection
            self._logger.info("Connecting to target", extra={"job": "orchestrator", "host": "source"})
            await self._establish_connection()
            assert self._remote_executor is not None
            self._ui.set_current_step(SyncStep.CONNECT, f"Connect to {self._target_hostname}")

            # SyncStep 3: Acquire target lock
            self._logger.info("Acquiring target lock", extra={"job": "orchestrator", "host": "target"})
            await self._acquire_target_lock()
            self._ui.set_current_step(SyncStep.TARGET_LOCK, f"Lock {self._target_hostname}")

            # SyncStep 4: Out-of-order / target-state check. Runs after the target lock
            # so we can read the target's sync-history over SSH. Always executes;
            # --allow-out-of-order only bypasses the W2/W3 confirmation, not the read.
            if not await self._check_out_of_order():
                # Not `SyncAbortedByUser`: the confirmer returns False both for a user
                # typing "n" and for a non-interactive run it refused without asking
                # anyone, and this site cannot tell the two apart (#224).
                raise SyncAborted(f"Sync aborted at the sync-order check on {self._target_hostname}")
            self._ui.set_current_step(SyncStep.OUT_OF_ORDER_CHECK, "Out-of-order check")

            # SyncStep 5: Discover and validate jobs
            self._logger.info("Discovering and validating jobs", extra={"job": "orchestrator", "host": "source"})
            jobs, unresolved_job_results = await self._discover_and_validate_jobs()
            self._ui.set_current_step(SyncStep.DISCOVER_JOBS, "Discover jobs")

            # SyncStep 6: Disk-space preflight check
            await self._check_disk_space_preflight()
            self._ui.set_current_step(SyncStep.DISK_CHECK, "Disk check")

            # SyncStep 7: Pre-sync snapshots
            self._logger.info("Creating pre-sync snapshots", extra={"job": "orchestrator", "host": "source"})
            await self._create_snapshots(SnapshotPhase.PRE)
            self._ui.set_current_step(SyncStep.PRE_SNAPSHOT, "Pre-sync snapshots")

            # SyncStep 8: Install/upgrade pc-switcher on target — after snapshots so a bad install is recoverable
            self._logger.info(
                "Ensuring pc-switcher is installed on target",
                extra={"job": "orchestrator", "host": "target"},
            )
            await self._install_on_target_job()
            self._ui.set_current_step(SyncStep.INSTALL_ON_TARGET, f"Install on {self._target_hostname}")

            # SyncStep 9: Sync config from source to target
            self._logger.info("Syncing configuration to target", extra={"job": "orchestrator", "host": "target"})
            await self._sync_config_to_target()
            self._ui.set_current_step(SyncStep.SYNC_CONFIG, "Sync config")

            # SyncStep 10: Run sync jobs — _execute_jobs sets the 10a/10b sub-steps per job
            self._logger.info("Starting sync operations", extra={"job": "orchestrator", "host": "source"})
            # Pause snapd auto-refresh on both hosts across the whole RUN_JOBS window
            # (snap convergence → folder_sync); released in _cleanup (decision 4).
            await self._hold_snap_autorefresh()
            job_results = await self._execute_jobs(jobs, unresolved_job_results)
            session.job_results = job_results

            # SyncStep 11: Post-sync snapshots
            self._logger.info("Creating post-sync snapshots", extra={"job": "orchestrator", "host": "source"})
            await self._create_snapshots(SnapshotPhase.POST)
            self._ui.set_current_step(SyncStep.POST_SNAPSHOT, "Post-sync snapshots")

            # Reaching here only means nothing propagated, which is weaker than success —
            # see _summarize_job_outcomes for why the outcome comes from job_results.
            session.status, session.error_message = _summarize_job_outcomes(job_results)

            # SyncStep 12: Record sync history on both machines (this machine was SOURCE,
            # target was TARGET). The write is skipped in dry-run mode (D-12: dry-run must
            # not write any state), but the counter still advances so it reaches 100% on
            # both real and dry-run paths — matching the snapshot steps.
            session.ended_at = datetime.now(UTC)
            self._log_sync_outcome(session)
            if not self._dry_run:
                await self._update_sync_history()
            self._ui.set_current_step(SyncStep.RECORD_HISTORY, "Record sync history")

            return session

        except asyncio.CancelledError:
            session.status = SessionStatus.INTERRUPTED
            session.ended_at = datetime.now(UTC)
            session.error_message = "Sync interrupted by user (SIGINT)"
            self._logger.warning("Sync interrupted by user", extra={"job": "orchestrator", "host": "source"})
            raise

        except SyncAborted as e:
            # A deliberate stop is expected control flow, not a failure: log once at
            # WARNING (never CRITICAL) and re-raise so the CLI can set a non-zero exit
            # code without re-printing a "failed" message. Only the ByUser subclass may
            # say the user did it — pc-switcher stopping on its own (an unreadable
            # registry, a prompt nobody could answer) must not be reported as their
            # decision (#224).
            session.status = SessionStatus.ABORTED
            session.ended_at = datetime.now(UTC)
            session.error_message = str(e)
            what = "Sync aborted by user" if isinstance(e, SyncAbortedByUser) else "Sync aborted"
            self._logger.warning("%s: %s", what, e, extra={"job": "orchestrator", "host": "source"})
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

        Announced through the executor rather than gated inside `SyncLock`, which stays a
        plain synchronous primitive: the orchestrator owns the gate, and this is the
        counterpart of the target's lock, which travels as a command and is gated where it
        is issued. The RELEASE is deliberately not announced — it runs in `_cleanup`, where
        an abort has nowhere to go and would leak the very lock it was declining to free.
        """
        self._source_lock = SyncLock(get_lock_path())

        holder_info = f"source:{self._source_hostname}:{self._session_id}:pid={os.getpid()}"
        assert self._local_executor is not None
        await self._local_executor.declare_modification(
            f"flock {get_lock_path()}  (held for the whole run, holder record: {holder_info})",
            mutates="take the exclusive sync lock, so no other sync can run on this machine",
        )
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

        # The step gate rides on the executors (`executor.py`), which is what makes every
        # mutating call site — job, orchestrator or helper — gate through one funnel. The
        # local one already exists; this is the half that needed the connection.
        self._remote_executor = RemoteExecutor(self._connection.ssh_connection, self._step_gate)

        self._logger.info("Connected to target", extra={"job": "orchestrator", "host": "target"})

        await self._resolve_target_canonical_hostname()

    async def _resolve_target_canonical_hostname(self) -> None:
        """Resolve the target's own hostname over SSH (source-symmetric acquisition).

        The source records its peer using `get_local_hostname()`; without this the
        target would be recorded under the user-typed CLI argument instead, so the
        two ends store the same machine under different names (e.g. `atlas` vs `Atlas`)
        and the topology check misreads a clean back-sync as a foreign one. On any
        failure (non-zero exit, empty output) the CLI-argument fallback set in
        __init__ is kept — a resolved hostname is a refinement, not a hard gate.
        """
        assert self._remote_executor is not None

        result = await self._remote_executor.run_command(get_hostname_command())
        resolved = result.stdout.strip() if result.success else ""
        if resolved:
            self._target_canonical_hostname = resolved
            self._logger.debug(
                "Resolved target hostname: %s",
                resolved,
                extra={"job": "orchestrator", "host": "target"},
            )
        else:
            self._logger.debug(
                "Could not resolve target hostname; using CLI argument %r",
                self._target_hostname,
                extra={"job": "orchestrator", "host": "target"},
            )

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
                f"{self._target_hostname} is already involved in a sync.\n"
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
            SyncAbortedByUser: If the user declines the config sync confirmation. Only
                a human's answer reaches that branch, so it is never the plain
                `SyncAborted`.
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
            source_hostname=self._source_hostname,
            target_hostname=self._target_hostname,
            auto_accept=self._auto_accept,
            dry_run=self._dry_run,
        )

        if not should_continue:
            # Every path to False here is a prompt a human answered (`--yes` and
            # `--dry-run` both return True without asking), so this one IS the user's.
            # The sentence does not repeat that: the renderer prefixes "by user".
            raise SyncAbortedByUser("the config sync was declined at its prompt")

        self._logger.info("Configuration sync completed", extra={"job": "orchestrator", "host": "target"})

    def _resolve_sync_job_class(self, job_name: str) -> type[SyncJob] | None:
        """Resolve the SyncJob subclass registered for `job_name`.

        Convention: job_name == module_name (e.g., "dummy_success" → pcswitcher.jobs.dummy_success).
        Dynamically imports the module and scans its attributes for a SyncJob subclass whose
        `name` ClassVar matches. Shared by `_discover_and_validate_jobs` (job discovery)
        and `_first_sync_scopes` (pre-step-4 first-sync messaging) so the import/scan logic
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
        this runs before job discovery, so classes (not instances) are used. Jobs that
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
        warn_title = f"First Sync — {tgt} Will Be Overwritten"
        warning = (
            f"[bold]{tgt}[/bold] has never been synced by pc-switcher (no sync history).\n\n"
            f"This first-ever sync will overwrite everything on {tgt} that is in scope of "
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
        # Compare against the target's resolved own hostname, not the CLI argument, so
        # a differently-cased or aliased target still matches recorded peers.
        tgt = self._target_canonical_hostname

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
        consecutive_push = local_role == SyncRole.SOURCE and hostnames_equal(local_peer, tgt)

        # Suppress (clean case): target last synced with this source, and this is
        # not a repeat push from the same source without a back-sync.
        if hostnames_equal(target_peer, src) and not consecutive_push:
            return True

        # Determine warning type and compose message
        if target_peer is not None and not hostnames_equal(target_peer, src):
            # W2: machine-C — target last synced with a third machine
            direction = "received a sync from" if target_role == SyncRole.TARGET else "sent a sync to"
            warn_title = f"{tgt} Last Synced with a Different Machine"
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

        Both peers are the machines' own resolved hostnames (target via SSH, source
        via `get_local_hostname()`), never the user-typed CLI argument, so the next
        sync's topology check compares like-for-like. Recording `last_peer` on both
        ends lets that check distinguish the clean A→B / B→A pattern from the
        machine-C and consecutive-push cases.

        Raises:
            RuntimeError: If history update fails on either machine.
        """
        # Update local (source) history. The write itself is in-process (atomic temp +
        # rename in `sync_history`), so unlike its target twin below there is no command
        # for the executor to trace — `declare_modification` announces it explicitly so the
        # two ends of the same logical change are equally visible (#210) and equally gated.
        if self._local_executor is not None:
            await self._local_executor.declare_modification(
                f"write {HISTORY_PATH} (last_role=source, last_peer={self._target_canonical_hostname})",
                mutates="record this machine's role in the sync history",
            )
        record_role(SyncRole.SOURCE, peer=self._target_canonical_hostname)
        self._logger.debug("Updated sync history: role=source", extra={"job": "orchestrator", "host": "source"})

        # Update remote (target) history via SSH
        if self._remote_executor is not None:
            cmd = get_record_role_command(SyncRole.TARGET, peer=self._source_hostname)
            result = await self._remote_executor.run_command(
                cmd, mutates=f"record this run's role in {self._target_hostname}'s sync history"
            )
            if not result.success:
                raise RuntimeError(f"Failed to update the sync history on {self._target_hostname}: {result.stderr}")
            self._logger.debug("Updated sync history: role=target", extra={"job": "orchestrator", "host": "target"})

    async def _discover_and_validate_jobs(self) -> tuple[list[Job], list[JobResult]]:
        """Discover enabled jobs from config and validate their configuration.

        Dynamically imports job modules based on enabled jobs in config.
        Convention: job_name == module_name (e.g., "dummy_success" → pcswitcher.jobs.dummy_success)

        Returns:
            The job instances ready for execution, and a SKIPPED `JobResult` for every
            enabled job name that resolved to no class. Those never become job instances,
            so there is nothing to raise `JobSkipped` from; the result is built here
            instead and seeded into the run's results, rather than the job the user
            enabled leaving no record at all.

        Raises:
            RuntimeError: If any job config validation fails
        """
        jobs: list[Job] = []
        unresolved: list[JobResult] = []
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
                # _resolve_sync_job_class already logged why, at WARNING.
                now = datetime.now(UTC)
                unresolved.append(
                    JobResult(
                        job_name=job_name,
                        status=JobStatus.SKIPPED,
                        started_at=now,
                        ended_at=now,
                        error_message=f"No SyncJob class resolved for enabled job {job_name}",
                    )
                )
                continue

            # Validate job config (Phase 2)
            job_config = self._config.job_configs.get(job_name, {})
            errors = job_class.validate_config(job_config)
            if errors:
                config_errors.extend(errors)
            else:
                context = self._create_job_context(job_config)
                jobs.append(job_class(context))

        config_errors.extend(self._check_package_jobs_precede_folder_sync())

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

        return jobs, unresolved

    def _check_package_jobs_precede_folder_sync(self) -> list[ConfigError]:
        """D-17: all four package jobs must run before folder_sync — apps are provisioned
        first, then their data lands on top (decisive for flatpak, where `flatpak install`
        must create `~/.local/share/flatpak` before folder_sync would otherwise land
        `~/.var/app` on top).

        `manual_installs_sync` is in the rule for the same reason as the three package
        managers: replaying an install snippet puts software on the target, and that
        software writes its own stock defaults on first appearance exactly as a package's
        postinst does.

        The shipped `default-config.yaml` encodes this ordering only by key order
        (jobs run in `self._config.sync_jobs.items()` order) — a user who hand-edits
        their own `config.yaml`, e.g. appending a newly-enabled `flatpak_sync: true`
        after an existing `folder_sync: true` line, silently inverts it with no error.
        This validates the RESOLVED, enabled order and turns that silent inversion into
        a loud `ConfigError` instead (WR-02) — every other ordering (jobs disabled,
        jobs absent, folder_sync disabled) is unaffected.
        """
        enabled_order = [job_name for job_name, enabled in self._config.sync_jobs.items() if enabled]
        if "folder_sync" not in enabled_order:
            return []
        folder_sync_index = enabled_order.index("folder_sync")
        return [
            ConfigError(
                job=job_name,
                path="sync_jobs",
                message=(
                    f"{job_name} must be listed before folder_sync in sync_jobs (D-17): package jobs "
                    "provision apps before folder_sync lands their data on top. Move it above folder_sync."
                ),
            )
            for job_name in ("apt_sync", "snap_sync", "flatpak_sync", "manual_installs_sync")
            if job_name in enabled_order and enabled_order.index(job_name) > folder_sync_index
        ]

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
            error_msg = f"Disk space on {self._source_hostname} {free_space_desc} below threshold {threshold_desc}"
            self._logger.critical(error_msg, extra={"job": "orchestrator", "host": "source"})
            raise RuntimeError(error_msg)

        # Check target
        if not is_sufficient(target_disk, threshold_type, threshold_value):
            free_space_desc = format_free_space(target_disk)
            threshold_desc = format_threshold(threshold_type, threshold_value)
            error_msg = f"Disk space on {self._target_hostname} {free_space_desc} below threshold {threshold_desc}"
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

    async def _execute_jobs(self, jobs: list[Job], seed_results: list[JobResult] | None = None) -> list[JobResult]:
        """Execute sync jobs sequentially with background disk monitoring.

        Each package job reviews its own diffs inside its own ``execute()`` (D-24): it
        plans, prompts through the injected ``JobContext.reviewer``, then converges. The
        review's blocking prompt runs inside the job TaskGroup alongside the disk-space
        monitors — ``review_items`` pauses the Live display before prompting and resumes
        it in a ``finally``, the same mechanism ``TerminalUIConfirmer`` already uses from
        ``FolderSyncJob.execute()`` — so no coordination outside the TaskGroup is needed.

        Args:
            jobs: List of validated jobs to execute
            seed_results: Results decided before the loop — the SKIPPED entries discovery
                produced for enabled job names that resolved to no class.

        Returns:
            List of JobResult for each executed job, `seed_results` first
        """
        results: list[JobResult] = list(seed_results) if seed_results else []

        try:
            await self._run_jobs_in_task_group(jobs, results)
        except BaseExceptionGroup as eg:
            # A job failing in the TaskGroup body raises an ExceptionGroup whose
            # own message ("unhandled errors in a TaskGroup (N sub-exceptions)")
            # is useless. Re-raise the underlying cause so run()'s handlers and
            # the CLI report the real reason — and so a job-raised
            # SyncAborted/SyncLockedError still reaches its WARNING handler.
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
                    # Jobs are sub-steps of SyncStep.RUN_JOBS, sub-labelled 10a, 10b, …
                    # (letters suffice for any realistic job count; fall back to a
                    # numeric suffix past 'z'), labelled with the job name so the TUI
                    # shows what is running.
                    substep = chr(ord("a") + job_index) if job_index < 26 else str(job_index + 1)
                    self._ui.set_current_step(SyncStep.RUN_JOBS, job.name, substep=substep)
                    started_at = datetime.now(UTC)
                    try:
                        # Labels this job's executor traffic in the debug trace (#210) and
                        # in the --confirm-each-command prompt. Set per job rather than per
                        # executor because the executors are shared by every job.
                        with active_job(job.name):
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

                    except SyncAborted:
                        # A job-level declined confirmation (e.g. FolderSyncJob's
                        # first-sync overwrite gate via the shared confirmer) is
                        # expected control flow, not a job failure. Let it pass
                        # through untouched so run() logs it once at WARNING and
                        # records an ABORTED session, rather than a spurious
                        # FAILED job result plus a duplicate CRITICAL log.
                        raise
                    except JobSkipped as e:
                        # The job did nothing and said so before touching anything
                        # (see JobSkipped). Record the honest status and carry on with
                        # the next job — deliberately NOT re-raised, like an isolated failure
                        # below, because a skip is not a failure of the run.
                        ended_at = datetime.now(UTC)
                        results.append(
                            JobResult(
                                job_name=job.name,
                                status=JobStatus.SKIPPED,
                                started_at=started_at,
                                ended_at=ended_at,
                                error_message=e.reason,
                            )
                        )
                        self._logger.warning(
                            "Job %s skipped: %s",
                            job.name,
                            e.reason,
                            extra={"job": "orchestrator", "host": "source"},
                        )
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
                        if _failure_stays_in_its_job(job, e):
                            continue
                        # Already reported with the job name; stop run()'s top-level
                        # handler from logging the identical cause a second time.
                        _mark_failure_logged(e)
                        raise
            finally:
                # Cancel monitor tasks so TaskGroup can exit
                # Monitors run forever (while True loop), so they must be cancelled
                source_monitor_task.cancel()
                target_monitor_task.cancel()

    def _machine_name(self, host: Host) -> str:
        """Name `host` as the user knows it: its hostname, never its role in this run.

        The source is this machine's own hostname, the target the CLI argument — the same
        two strings `JobContext` carries and the confirmation heading prints, so one machine
        reads the same wherever it is named (`PKG-FR-NAME-THE-MACHINES`).

        For the message a user reads. The `host` field of a log record keeps the role, which
        is what the file's structured queries and the session-start hostname mapping expect.
        """
        return self._source_hostname if host is Host.SOURCE else self._target_hostname

    async def _run_snap_hold_command(self, host: Host, cmd: str, *, mutates: str | None = None) -> CommandResult:
        """Run a snap-hold command on `host`, honoring each executor's shell contract.

        The source is the local executor (no login-shell notion — its `run_command` takes
        no `login_shell`); the target is the remote executor, invoked with
        `login_shell=False` to match snap_sync's own target calls. Callers only reach here
        after the hold is engaged, which happens post-connect, so both executors are set.

        `mutates` is passed straight through: the capture and the post-apply read-back are
        reads and leave it None, while applying and restoring the hold are system writes on
        both machines and name themselves, so `--confirm-each-command` shows them like any
        other change. Privilege is orthogonal — the reads run under sudo too (see
        `_capture_snap_hold`) and still declare nothing, because they change nothing.
        """
        if host is Host.SOURCE:
            assert self._local_executor is not None
            return await self._local_executor.run_command(cmd, mutates=mutates)
        assert self._remote_executor is not None
        return await self._remote_executor.run_command(cmd, login_shell=False, mutates=mutates)

    async def _capture_snap_hold(self, host: Host) -> tuple[str | None, bool]:
        """Read `host`'s system-wide snap `refresh.hold` as `(value, readable)`.

        `value` is the raw hold — an RFC3339 timestamp or the literal `forever` — or None
        when no hold is set: snap exits 0 printing nothing for an option set empty, and
        non-zero with `_SNAP_HOLD_UNSET_MARKER` for one never set. `readable` is False when
        the READ ITSELF failed, a state None cannot express and which callers must not treat
        as "no hold" (`_restore_snap_hold`).

        Runs under sudo because reading snap configuration is admin-gated: snapd serves
        `/v2/snaps/{name}/conf` behind `io.snapcraft.snapd.manage-configuration`, which the
        shipped polkit policy sets to `auth_admin_keep`, so an unprivileged `snap get system
        refresh.hold` does not return empty — it fails with "access denied" on every machine.
        Still a read, so no `mutates=`: it inspects the option and changes nothing.
        """
        result = await self._run_snap_hold_command(host, "sudo snap get system refresh.hold")
        if result.success:
            return (result.stdout.strip() or None, True)
        return (None, _SNAP_HOLD_UNSET_MARKER in result.stderr)

    async def _apply_snap_hold(self, host: Host, prior: str | None) -> None:
        """Set a timed system-wide `refresh.hold` on `host` and confirm it took (best-effort).

        Writes `now + _SNAP_AUTOREFRESH_HOLD_DURATION` (computed on the host) via
        `sudo snap set system refresh.hold=...`. A failure is logged, not raised: pausing
        auto-refresh is a best-effort race guard, and failing the whole sync because it could
        not be set would be worse than proceeding without it (validate() has already
        confirmed snap + passwordless sudo on both hosts).
        """
        cmd = f'sudo snap set system refresh.hold="$({_SNAP_HOLD_TIMESTAMP_CMD})"'
        result = await self._run_snap_hold_command(host, cmd, mutates="pause snapd auto-refresh for the sync window")
        if not result.success:
            self._logger.warning(
                "Could not pause snapd auto-refresh on %s: %s",
                self._machine_name(host),
                result.stderr.strip(),
                extra={"job": "orchestrator", "host": host.value},
            )
            return
        await self._verify_snap_hold(host, prior)

    async def _verify_snap_hold(self, host: Host, prior: str | None) -> None:
        """Read the hold back on `host` and warn when it did not stick.

        `snap set` exiting 0 says the command ran, not that the option changed — so an exit
        code alone cannot catch a hold that was never applied. The read-back can: the value
        must now be present and different from `prior`, which the freshly computed
        "now + duration" timestamp always is.

        Diagnostic only. Every outcome is a WARNING and errors from the read are absorbed,
        because the sync is correct without the pause (it is a race guard against snapd
        auto-refreshing mid-run), and a check on a best-effort measure must not be able to
        end the run. The warning points at `snap refresh --time`, which works unprivileged
        and shows the hold to a human; its value is localized prose rather than RFC3339, so
        it is useful in a log line and useless as a capture value.
        """
        try:
            observed, readable = await self._capture_snap_hold(host)
        except Exception as e:
            self._logger.warning(
                "Could not confirm snapd auto-refresh is paused on %s: %s",
                self._machine_name(host),
                e,
                extra={"job": "orchestrator", "host": host.value},
            )
            return
        if not readable:
            self._logger.warning(
                "Could not confirm snapd auto-refresh is paused on %s: refresh.hold is unreadable "
                "(check `snap refresh --time` on that machine)",
                self._machine_name(host),
                extra={"job": "orchestrator", "host": host.value},
            )
        elif observed is None or observed == prior:
            self._logger.warning(
                "snapd auto-refresh is NOT paused on %s: refresh.hold still reads %s after being set "
                "(check `snap refresh --time` on that machine). The sync continues unpaused.",
                self._machine_name(host),
                observed if observed is not None else "(unset)",
                extra={"job": "orchestrator", "host": host.value},
            )

    async def _hold_snap_autorefresh(self) -> None:
        """Pause snapd AUTOMATIC refreshes on both hosts for the sync window (decision 4).

        Gated on `sync_jobs.snap_sync` being enabled and skipped in dry-run (writing
        `refresh.hold` is a system mutation; ADR-014/D-12). Captures each host's prior
        `refresh.hold` first so `_cleanup` can restore it exactly, marks the hold engaged,
        then applies a timed hold on each host WHOSE PRIOR POLICY IT COULD READ. Only touches
        the system-wide `refresh.hold` option — never per-snap holds — and never blocks the
        manual `--revision` convergence snap_sync performs.

        A host whose capture failed is left untouched (`PKG-FR-SNAP-REFRESH-PAUSE`). The
        capture is what makes the write reversible: without a pre-sync value there is nothing
        to put back, so setting the option would replace an unknown policy — possibly the
        user's own indefinite hold — with a timed one that expires into "no hold at all". The
        cost is running unpaused on that host, which risks a mid-run auto-refresh; that trade
        is the criterion's, and it is the same one `_restore_snap_hold` makes on the way out.
        """
        if self._dry_run or not self._config.sync_jobs.get("snap_sync", False):
            return

        self._snap_hold_prior_source, self._snap_hold_readable_source = await self._capture_snap_hold(Host.SOURCE)
        self._snap_hold_prior_target, self._snap_hold_readable_target = await self._capture_snap_hold(Host.TARGET)
        # Engage BEFORE applying so a partially-applied hold is still restored in _cleanup.
        self._snap_hold_engaged = True
        # Announced once, not per host: every log line already carries the machine it
        # happened on. The line states its owner and its span (#233), because the pause
        # fires before the first job and otherwise reads as an unexplained stop.
        self._logger.info(
            "Pausing snapd auto-refresh on both hosts for the whole run — held by the orchestrator, not by "
            "snap_sync alone, because snap_sync converges each snap's revision and folder_sync then mirrors "
            "that revision's data directory; a refresh between the two drops it from the mirror",
            extra={"job": "orchestrator", "host": "source"},
        )
        for host, prior, readable in (
            (Host.SOURCE, self._snap_hold_prior_source, self._snap_hold_readable_source),
            (Host.TARGET, self._snap_hold_prior_target, self._snap_hold_readable_target),
        ):
            if not readable:
                self._logger.warning(
                    "Not pausing snapd auto-refresh on %s: its refresh.hold could not be read, so a hold "
                    "written here could not be put back. The sync continues unpaused on that machine.",
                    self._machine_name(host),
                    extra={"job": "orchestrator", "host": host.value},
                )
                continue
            await self._apply_snap_hold(host, prior)

    async def _restore_snap_hold(self, host: Host, prior: str | None, *, readable: bool) -> None:
        """Restore `host`'s `refresh.hold` to its pre-sync value, or unset it (best-effort).

        When a prior hold was captured it is written back verbatim (an RFC3339 timestamp or
        `forever`); when there was none, `refresh.hold` is set empty, which snapd treats as
        no hold — leaving any unrelated per-snap holds untouched.

        A failed capture (`readable=False`) is NOT treated as "there was no hold": the option
        is left alone instead of cleared. `_hold_snap_autorefresh` never wrote it on such a
        host, so there is nothing to undo, and clearing an option whose pre-sync value is
        unknown would destroy a hold the user may have set (including `forever`).

        Gated by `--confirm-each-command` like every other modification. Restoring is not
        merely lifting: when a prior hold was captured, skipping this write means the timed
        hold pc-switcher set expires and the user's OWN hold is gone with it — which is why
        the gate description names the value being written back.

        Swallows a FAILED restore (teardown must complete every step regardless — the timed
        hold self-expires) but never a DECLINED one: `SyncAborted` is re-raised ahead
        of the broad handler, because a user answering "abort" is a decision to honor, not
        an error to absorb. `_cleanup` decides how far that abort travels.
        """
        if prior is not None:
            cmd = f"sudo snap set system refresh.hold={shlex.quote(prior)}"
            description = f"restore this machine's own snapd refresh.hold ({prior}), which this run overwrote"
        elif not readable:
            self._logger.warning(
                "Leaving snapd refresh.hold alone on %s: its pre-sync value could not be read, so this run "
                "never paused auto-refresh there and clearing it now could destroy a hold set on that machine.",
                self._machine_name(host),
                extra={"job": "orchestrator", "host": host.value},
            )
            return
        else:
            cmd = 'sudo snap set system refresh.hold=""'
            description = "clear the snapd refresh.hold this run set"
        try:
            result = await self._run_snap_hold_command(host, cmd, mutates=description)
            if not result.success:
                self._logger.warning(
                    "Could not restore snapd refresh.hold on %s: %s",
                    self._machine_name(host),
                    result.stderr.strip(),
                    extra={"job": "orchestrator", "host": host.value},
                )
        except SyncAborted:
            raise
        except Exception as e:
            # Teardown must not be derailed by a failed restore (e.g. the connection is
            # already tearing down). The timed hold self-expires regardless.
            self._logger.warning(
                "Error restoring snapd refresh.hold on %s: %s",
                self._machine_name(host),
                e,
                extra={"job": "orchestrator", "host": host.value},
            )

    async def _restore_snap_autorefresh(self) -> None:
        """Restore both hosts' snap auto-refresh policy captured by `_hold_snap_autorefresh`.

        A no-op when no hold was engaged, and idempotent (clears the engaged flag first) so a
        second `_cleanup` entry cannot double-restore. Runs early in `_cleanup`, before the
        SSH connection the target restore needs is torn down.
        """
        if not self._snap_hold_engaged:
            return
        self._snap_hold_engaged = False
        await self._restore_snap_hold(
            Host.SOURCE, self._snap_hold_prior_source, readable=self._snap_hold_readable_source
        )
        await self._restore_snap_hold(
            Host.TARGET, self._snap_hold_prior_target, readable=self._snap_hold_readable_target
        )

    async def _cleanup(self) -> None:
        """Clean up resources (connection, locks, executors)."""
        self._cleanup_in_progress = True

        # Restore snapd auto-refresh FIRST, while the connection the target restore needs is
        # still up (decision 4). Best-effort and idempotent — a no-op when no hold was set.
        try:
            await self._restore_snap_autorefresh()
        except SyncAborted as e:
            # Declining the restore at the --confirm-each-command gate is honored: the write
            # does not happen. It stops there and nowhere else — everything below this point
            # RELEASES resources (target lock, SSH connection, source lock, event bus, UI)
            # rather than modifying either machine, and no confirmation prompt should be
            # able to leak a lock or a connection. Logged at WARNING so the end-of-run
            # summary resurfaces what was left in place.
            self._logger.warning(
                "snapd auto-refresh not restored (%s). The timed hold set by this run expires %s "
                "after it was applied; any pre-existing hold of your own was not written back.",
                e,
                _SNAP_AUTOREFRESH_HOLD_DURATION.lstrip("+"),
                extra={"job": "orchestrator", "host": "source"},
            )

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
