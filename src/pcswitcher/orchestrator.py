"""Core orchestrator coordinating the complete sync workflow."""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from pcswitcher.config import Configuration
from pcswitcher.connection import Connection
from pcswitcher.disk import DiskSpace, check_disk_space, parse_threshold
from pcswitcher.events import EventBus
from pcswitcher.executor import LocalExecutor, RemoteExecutor
from pcswitcher.jobs.base import Job, SyncJob
from pcswitcher.jobs.btrfs import BtrfsSnapshotJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.disk_space_monitor import DiskSpaceMonitorJob
from pcswitcher.jobs.dummy import DummyFailJob, DummySuccessJob
from pcswitcher.jobs.install_on_target import InstallOnTargetJob
from pcswitcher.lock import SyncLock, acquire_target_lock, get_local_hostname
from pcswitcher.logger import (
    FileLogger,
    Logger,
    generate_log_filename,
    get_logs_directory,
)
from pcswitcher.models import (
    ConfigError,
    Host,
    JobResult,
    JobStatus,
    LogLevel,
    SessionStatus,
    SnapshotPhase,
    SyncSession,
    ValidationError,
)
from pcswitcher.snapshots import session_folder_name
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

    def __init__(self, target: str, config: Configuration) -> None:
        """Initialize orchestrator with target and validated configuration.

        Args:
            target: Target hostname or SSH alias
            config: Validated configuration from YAML file
        """
        self._config = config
        self._session_id = secrets.token_hex(4)
        self._session_folder = session_folder_name(self._session_id)
        self._source_hostname = get_local_hostname()
        self._target_hostname = target

        # Core components
        self._event_bus = EventBus()
        self._logger = Logger(self._event_bus, job_name="orchestrator")
        self._connection: Connection | None = None
        self._local_executor: LocalExecutor | None = None
        self._remote_executor: RemoteExecutor | None = None

        # Locks
        self._source_lock: SyncLock | None = None

        # Background tasks
        self._task_group: asyncio.TaskGroup | None = None
        self._cleanup_in_progress = False

        # Logging infrastructure (initialized in run())
        self._file_logger: FileLogger | None = None
        self._ui: TerminalUI | None = None
        self._file_logger_task: asyncio.Task[None] | None = None
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

        hostname_map = {
            Host.SOURCE: self._source_hostname,
            Host.TARGET: self._target_hostname,
        }

        # Create log file path
        log_file_path = get_logs_directory() / generate_log_filename(self._session_id)

        # Subscribe to event bus
        file_queue = self._event_bus.subscribe()
        ui_queue = self._event_bus.subscribe()

        # Instantiate loggers and UI
        self._file_logger = FileLogger(
            log_file=log_file_path,
            level=self._config.log_file_level,
            queue=file_queue,
            hostname_map=hostname_map,
        )
        self._ui = TerminalUI(
            console=Console(),
            total_steps=len(self._config.sync_jobs) + 2,  # +2 for pre/post snapshots
        )

        # Start consumers as background tasks
        self._file_logger_task = asyncio.create_task(self._file_logger.consume())
        self._ui_task = asyncio.create_task(
            self._ui.consume_events(
                queue=ui_queue,
                hostname_map=hostname_map,
                log_level=self._config.log_cli_level,
            )
        )

        # Start UI live display
        self._ui.start()

        try:
            # Phase 1: Acquire source lock
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Acquiring source lock")
            await self._acquire_source_lock()

            # Phase 2: Establish SSH connection
            self._logger.log(LogLevel.INFO, Host.SOURCE, f"Connecting to target: {self._target_hostname}")
            await self._establish_connection()
            assert self._remote_executor is not None

            # Phase 3: Acquire target lock
            self._logger.log(LogLevel.INFO, Host.TARGET, "Acquiring target lock")
            await self._acquire_target_lock()

            # Phase 4: Job discovery and validation
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Discovering and validating jobs")
            jobs = await self._discover_and_validate_jobs()

            # Phase 5: Disk space preflight check
            await self._check_disk_space_preflight()

            # Phase 6: Pre-sync snapshots
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Creating pre-sync snapshots")
            await self._create_snapshots(SnapshotPhase.PRE)

            # Phase 7: Install/upgrade pc-switcher on target (after snapshots for rollback safety)
            self._logger.log(LogLevel.INFO, Host.TARGET, "Ensuring pc-switcher is installed on target")
            await self._install_on_target_job()

            # Phase 8: Execute sync jobs with background monitoring
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Starting sync operations")
            job_results = await self._execute_jobs(jobs)
            session.job_results = job_results

            # Phase 9: Post-sync snapshots
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Creating post-sync snapshots")
            await self._create_snapshots(SnapshotPhase.POST)

            # Success
            session.status = SessionStatus.COMPLETED
            session.ended_at = datetime.now(UTC)
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Sync completed successfully")

            return session

        except asyncio.CancelledError:
            session.status = SessionStatus.INTERRUPTED
            session.ended_at = datetime.now(UTC)
            session.error_message = "Sync interrupted by user (SIGINT)"
            self._logger.log(LogLevel.WARNING, Host.SOURCE, "Sync interrupted by user")
            raise

        except Exception as e:
            session.status = SessionStatus.FAILED
            session.ended_at = datetime.now(UTC)
            session.error_message = str(e)
            self._logger.log(LogLevel.CRITICAL, Host.SOURCE, f"Sync failed: {e}")
            raise

        finally:
            # Cleanup
            await self._cleanup()

    async def _acquire_source_lock(self) -> None:
        """Acquire exclusive lock on source machine."""
        lock_path = Path.home() / ".local/share/pc-switcher/sync.lock"
        self._source_lock = SyncLock(lock_path)

        holder_info = f"{self._source_hostname}:{self._session_id}"
        if not self._source_lock.acquire(holder_info):
            existing_holder = self._source_lock.get_holder_info()
            raise RuntimeError(f"Another sync is already in progress on source (held by: {existing_holder})")

    async def _establish_connection(self) -> None:
        """Establish SSH connection to target machine."""
        self._connection = Connection(self._target_hostname, event_bus=self._event_bus)
        await self._connection.connect()

        # Create executors
        self._local_executor = LocalExecutor()
        self._remote_executor = RemoteExecutor(self._connection.ssh_connection)

        self._logger.log(
            LogLevel.INFO,
            Host.TARGET,
            f"Connected to {self._target_hostname}",
        )

    async def _acquire_target_lock(self) -> None:
        """Acquire exclusive lock on target machine via SSH."""
        assert self._remote_executor is not None

        if not await acquire_target_lock(self._remote_executor, self._source_hostname):
            raise RuntimeError(f"Another sync is already in progress on target {self._target_hostname}")

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

    async def _discover_and_validate_jobs(self) -> list[Job]:
        """Discover enabled jobs from config and validate their configuration.

        Returns:
            List of job instances ready for execution

        Raises:
            RuntimeError: If any job config validation fails
        """
        jobs: list[Job] = []
        config_errors: list[ConfigError] = []

        # Build registry of available sync jobs
        job_registry: dict[str, type[SyncJob]] = {
            "dummy_success": DummySuccessJob,
            "dummy_fail": DummyFailJob,
        }

        # Discover enabled jobs from sync_jobs config
        for job_name, enabled in self._config.sync_jobs.items():
            if not enabled:
                self._logger.log(
                    LogLevel.DEBUG,
                    Host.SOURCE,
                    f"Job {job_name} is disabled in config",
                )
                continue

            job_class = job_registry.get(job_name)
            if job_class is None:
                self._logger.log(
                    LogLevel.WARNING,
                    Host.SOURCE,
                    f"Job {job_name} is enabled but not found in registry",
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

        Per FR-016, verifies both hosts have sufficient free disk space
        based on the configured preflight_minimum threshold.

        Raises:
            RuntimeError: If either host has insufficient disk space
        """
        assert self._local_executor is not None
        assert self._remote_executor is not None

        self._logger.log(LogLevel.INFO, Host.SOURCE, "Checking disk space on both hosts")

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
            self._logger.log(LogLevel.CRITICAL, Host.SOURCE, error_msg)
            raise RuntimeError(error_msg)

        # Check target
        if not is_sufficient(target_disk, threshold_type, threshold_value):
            free_space_desc = format_free_space(target_disk)
            threshold_desc = format_threshold(threshold_type, threshold_value)
            error_msg = f"Target disk space {free_space_desc} below threshold {threshold_desc}"
            self._logger.log(LogLevel.CRITICAL, Host.TARGET, error_msg)
            raise RuntimeError(error_msg)

        # Both checks passed - log success
        source_free = format_free_space(source_disk)
        target_free = format_free_space(target_disk)
        self._logger.log(LogLevel.INFO, Host.SOURCE, f"Source disk space check passed: {source_free} free")
        self._logger.log(LogLevel.INFO, Host.TARGET, f"Target disk space check passed: {target_free} free")

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

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg

            # Start background disk space monitors for root filesystem
            monitor_config = {
                "preflight_minimum": self._config.disk.preflight_minimum,
                "runtime_minimum": self._config.disk.runtime_minimum,
                "check_interval": self._config.disk.check_interval,
            }
            monitor_context = self._create_job_context(monitor_config)
            source_monitor = DiskSpaceMonitorJob(monitor_context, host=Host.SOURCE, mount_point="/")
            target_monitor = DiskSpaceMonitorJob(monitor_context, host=Host.TARGET, mount_point="/")

            tg.create_task(source_monitor.execute())
            tg.create_task(target_monitor.execute())

            # Execute sync jobs sequentially
            for job in jobs:
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
                    self._logger.log(
                        LogLevel.INFO,
                        Host.SOURCE,
                        f"Job {job.name} completed successfully",
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
                    self._logger.log(
                        LogLevel.CRITICAL,
                        Host.SOURCE,
                        f"Job {job.name} failed: {e}",
                    )
                    raise

        return results

    async def _cleanup(self) -> None:
        """Clean up resources (connection, locks, executors)."""
        self._cleanup_in_progress = True

        # Terminate all processes
        if self._local_executor is not None:
            await self._local_executor.terminate_all_processes()
        if self._remote_executor is not None:
            await self._remote_executor.terminate_all_processes()

        # Close connection (also releases target lock automatically)
        if self._connection is not None:
            await self._connection.disconnect()

        # Release source lock
        if self._source_lock is not None:
            self._source_lock.release()

        # Close event bus (sends None sentinel to all consumers)
        self._event_bus.close()

        # Wait for logger tasks to finish draining their queues
        if self._file_logger_task is not None:
            await self._file_logger_task
        if self._ui_task is not None:
            await self._ui_task

        # Stop UI live display
        if self._ui is not None:
            self._ui.stop()
