"""Core orchestrator coordinating the complete sync workflow."""

from __future__ import annotations

import asyncio
import importlib
import logging
import secrets
import sys
from datetime import UTC, datetime
from logging.handlers import QueueListener
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from pcswitcher.btrfs_snapshots import session_folder_name
from pcswitcher.config import Configuration
from pcswitcher.config_sync import sync_config_to_target
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
    Host,
    JobResult,
    JobStatus,
    SessionStatus,
    SnapshotPhase,
    SyncSession,
    ValidationError,
)
from pcswitcher.sync_history import (
    SyncRole,
    get_last_role_with_error,
    get_record_role_command,
    record_role,
)
from pcswitcher.ui import TerminalUI

__all__ = ["Orchestrator"]


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
        allow_consecutive: bool = False,
        dry_run: bool = False,
    ) -> None:
        """Initialize orchestrator with target and validated configuration.

        Args:
            target: Target hostname or SSH alias
            config: Validated configuration from YAML file
            auto_accept: If True, auto-accept prompts (e.g., config sync)
            allow_consecutive: If True, skip warning about consecutive syncs
            dry_run: If True, preview sync without making changes
        """
        self._config = config
        self._auto_accept = auto_accept
        self._allow_consecutive = allow_consecutive
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

        # Create log file path and set up stdlib logging infrastructure
        log_file_path = get_logs_directory() / generate_log_filename(self._session_id)
        self._queue_listener, _ = setup_logging(log_file_path, self._config.logging)

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

        # Calculate total steps: 8 system phases + sync jobs + 1 post-snapshot
        # System phases: 1=source lock, 2=SSH, 3=target lock, 4=validation,
        # 5=disk check, 6=pre-snapshots, 7=install on target, 8=config sync
        total_steps = 8 + len(self._config.sync_jobs) + 1
        self._console = Console()
        self._ui = TerminalUI(
            console=self._console,
            total_steps=total_steps,
        )

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
            # Pre-Phase: Check for consecutive sync (before any operations)
            if not self._allow_consecutive:
                should_continue = await self._check_consecutive_sync()
                if not should_continue:
                    raise RuntimeError("Sync aborted: consecutive sync without receiving a sync back first")

            # Phase 1: Acquire source lock
            self._logger.info("Acquiring source lock", extra={"job": "orchestrator", "host": "source"})
            await self._acquire_source_lock()
            self._ui.set_current_step(1)

            # Phase 2: Establish SSH connection
            self._logger.info("Connecting to target", extra={"job": "orchestrator", "host": "source"})
            await self._establish_connection()
            assert self._remote_executor is not None
            self._ui.set_current_step(2)

            # Phase 3: Acquire target lock
            self._logger.info("Acquiring target lock", extra={"job": "orchestrator", "host": "target"})
            await self._acquire_target_lock()
            self._ui.set_current_step(3)

            # Phase 4: Job discovery and validation
            self._logger.info("Discovering and validating jobs", extra={"job": "orchestrator", "host": "source"})
            jobs = await self._discover_and_validate_jobs()
            self._ui.set_current_step(4)

            # Phase 5: Disk space preflight check
            await self._check_disk_space_preflight()
            self._ui.set_current_step(5)

            # Phase 6: Pre-sync snapshots
            self._logger.info("Creating pre-sync snapshots", extra={"job": "orchestrator", "host": "source"})
            await self._create_snapshots(SnapshotPhase.PRE)
            self._ui.set_current_step(6)

            # Phase 7: Install/upgrade pc-switcher on target (after snapshots for rollback safety)
            self._logger.info(
                "Ensuring pc-switcher is installed on target",
                extra={"job": "orchestrator", "host": "target"},
            )
            await self._install_on_target_job()
            self._ui.set_current_step(7)

            # Phase 8: Sync config from source to target
            self._logger.info("Syncing configuration to target", extra={"job": "orchestrator", "host": "target"})
            await self._sync_config_to_target()
            self._ui.set_current_step(8)

            # Phase 9: Execute sync jobs with background monitoring
            self._logger.info("Starting sync operations", extra={"job": "orchestrator", "host": "source"})
            job_results = await self._execute_jobs(jobs)
            session.job_results = job_results

            # Phase 10: Post-sync snapshots
            self._logger.info("Creating post-sync snapshots", extra={"job": "orchestrator", "host": "source"})
            await self._create_snapshots(SnapshotPhase.POST)
            self._ui.set_current_step(8 + len(jobs) + 1)

            # Success - update sync history on both machines
            session.status = SessionStatus.COMPLETED
            session.ended_at = datetime.now(UTC)
            self._logger.info("Sync completed successfully", extra={"job": "orchestrator", "host": "source"})

            # Update sync history: this machine was SOURCE, target was TARGET
            await self._update_sync_history()

            return session

        except asyncio.CancelledError:
            session.status = SessionStatus.INTERRUPTED
            session.ended_at = datetime.now(UTC)
            session.error_message = "Sync interrupted by user (SIGINT)"
            self._logger.warning("Sync interrupted by user", extra={"job": "orchestrator", "host": "source"})
            raise

        except Exception as e:
            session.status = SessionStatus.FAILED
            session.ended_at = datetime.now(UTC)
            session.error_message = str(e)
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

        holder_info = f"source:{self._source_hostname}:{self._session_id}"
        if not self._source_lock.acquire(holder_info):
            existing_holder = self._source_lock.get_holder_info()
            raise RuntimeError(f"This machine is already involved in a sync (held by: {existing_holder})")

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
            raise RuntimeError(f"Target {self._target_hostname} is already involved in a sync")

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
            RuntimeError: If user aborts or config sync fails
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
            raise RuntimeError("Config sync aborted by user")

        self._logger.info("Configuration sync completed", extra={"job": "orchestrator", "host": "target"})

    async def _check_consecutive_sync(self) -> bool:
        """Check if this is a consecutive sync and prompt user if so.

        A consecutive sync is when this machine tries to be a SOURCE again
        without having been a TARGET first. This usually means the user forgot
        to sync back from the other machine.

        Returns:
            True if sync should continue, False if user aborted.
        """
        assert self._console is not None
        assert self._ui is not None

        last_role, had_error = get_last_role_with_error()

        # No warning needed if:
        # - No history exists (first sync)
        # - Last role was TARGET (received a sync, now sending - normal flow)
        if last_role is None and not had_error:
            return True
        if last_role == SyncRole.TARGET:
            return True

        # Warning needed if:
        # - Last role was SOURCE (consecutive sync from same machine)
        # - History file was corrupted (safety-first: treat as consecutive)

        # In non-interactive mode (no TTY), use the default "n" response
        # to avoid hanging on Prompt.ask()
        if not sys.stdin.isatty():
            self._console.print(
                "[yellow]Warning: Consecutive sync detected (no back-sync received).[/yellow]\n"
                "Use --allow-consecutive to override in non-interactive mode."
            )
            return False

        self._ui.stop()

        try:
            self._console.print()
            self._console.print(
                Panel(
                    "[yellow]Warning: You are syncing FROM this machine again "
                    "without receiving a sync back first.[/yellow]\n\n"
                    "The normal workflow is:\n"
                    "  1. Sync FROM this machine TO another\n"
                    "  2. Work on the other machine\n"
                    "  3. Sync FROM the other machine BACK to this one\n"
                    "  4. Then sync FROM this machine again\n\n"
                    "You appear to be at step 4 without completing step 3.\n"
                    "Continuing may overwrite changes made on the target machine.",
                    title="Consecutive Sync Warning",
                    border_style="yellow",
                )
            )
            self._console.print()

            response = Prompt.ask(
                "[bold]Continue anyway?[/bold]",
                choices=["y", "n"],
                default="n",
            )

            return response.lower() == "y"
        finally:
            self._ui.start()

    async def _update_sync_history(self) -> None:
        """Update sync history on both source and target machines.

        After a successful sync:
        - Source machine's history: last_role = SOURCE
        - Target machine's history: last_role = TARGET

        This enables the consecutive sync warning to work correctly.

        Raises:
            RuntimeError: If history update fails on either machine.
        """
        # Update local (source) history
        record_role(SyncRole.SOURCE)
        self._logger.debug("Updated sync history: role=source", extra={"job": "orchestrator", "host": "source"})

        # Update remote (target) history via SSH
        if self._remote_executor is not None:
            cmd = get_record_role_command(SyncRole.TARGET)
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

            # Dynamic import: pcswitcher.jobs.{job_name}
            try:
                module = importlib.import_module(f"pcswitcher.jobs.{job_name}")
            except ModuleNotFoundError:
                self._logger.warning(
                    "Job module pcswitcher.jobs.%s not found",
                    job_name,
                    extra={"job": "orchestrator", "host": "source"},
                )
                continue

            # Find the SyncJob class in the module with matching name
            job_class: type[SyncJob] | None = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, SyncJob)
                    and attr is not SyncJob
                    and getattr(attr, "name", None) == job_name
                ):
                    job_class = attr
                    break

            if job_class is None:
                self._logger.warning(
                    "No SyncJob with name=%s found in module pcswitcher.jobs.%s",
                    job_name,
                    job_name,
                    extra={"job": "orchestrator", "host": "source"},
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

        Per FND-FR-DISK-PRE, verifies both hosts have sufficient free disk space
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
        assert self._ui is not None

        results: list[JobResult] = []

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
                    # Update step counter (base 8 system steps + current job index)
                    self._ui.set_current_step(8 + job_index + 1)
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
                        raise
            finally:
                # Cancel monitor tasks so TaskGroup can exit
                # Monitors run forever (while True loop), so they must be cancelled
                source_monitor_task.cancel()
                target_monitor_task.cancel()

        return results

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
