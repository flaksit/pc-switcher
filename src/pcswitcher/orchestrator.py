"""Core orchestrator coordinating the complete sync workflow."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from pathlib import Path

from pcswitcher.config import Configuration
from pcswitcher.connection import Connection
from pcswitcher.events import EventBus
from pcswitcher.executor import LocalExecutor, RemoteExecutor
from pcswitcher.installation import (
    InstallationError,
    get_current_version,
    get_target_version,
    install_on_target,
)
from pcswitcher.jobs.base import Job, SyncJob
from pcswitcher.jobs.btrfs import BtrfsSnapshotJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.disk_space_monitor import DiskSpaceMonitorJob
from pcswitcher.jobs.dummy import DummyFailJob, DummySuccessJob
from pcswitcher.lock import SyncLock, acquire_target_lock, get_local_hostname
from pcswitcher.logger import Logger
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
        self._target = target
        self._config = config
        self._session_id = secrets.token_hex(4)
        self._source_hostname = get_local_hostname()
        self._target_hostname: str | None = None

        # Core components
        self._event_bus = EventBus()
        self._logger = Logger(self._event_bus, job_name="orchestrator")
        self._connection: Connection | None = None
        self._local_executor: LocalExecutor | None = None
        self._remote_executor: RemoteExecutor | None = None

        # Version info (populated during validation, used during install)
        self._source_version: str | None = None
        self._target_version: str | None = None
        self._install_needed: bool = False

        # Locks
        self._source_lock: SyncLock | None = None

        # Background tasks
        self._task_group: asyncio.TaskGroup | None = None
        self._cleanup_in_progress = False

    async def run(self) -> SyncSession:
        """Execute the complete sync workflow.

        Returns:
            SyncSession with results and status

        Raises:
            Various exceptions for critical failures (connection, locks, validation, etc.)
        """
        started_at = datetime.now().isoformat()
        session = SyncSession(
            session_id=self._session_id,
            started_at=started_at,
            source_hostname=self._source_hostname,
            target_hostname="",  # Will be filled in after connection
            config={},  # TODO: Add config snapshot
            status=SessionStatus.RUNNING,
            job_results=[],
        )

        try:
            # Phase 1: Acquire source lock
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Acquiring source lock")
            await self._acquire_source_lock()

            # Phase 2: Establish SSH connection
            self._logger.log(LogLevel.INFO, Host.SOURCE, f"Connecting to target: {self._target}")
            await self._establish_connection()
            assert self._remote_executor is not None
            assert self._target_hostname is not None

            # Update session with target hostname
            session.target_hostname = self._target_hostname

            # Phase 3: Acquire target lock
            self._logger.log(LogLevel.INFO, Host.TARGET, "Acquiring target lock")
            await self._acquire_target_lock()

            # Phase 4: Version compatibility check (error if target > source)
            self._logger.log(LogLevel.INFO, Host.TARGET, "Checking pc-switcher version compatibility")
            await self._check_version_compatibility()

            # Phase 5: Job discovery and validation
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Discovering and validating jobs")
            jobs = await self._discover_and_validate_jobs()

            # Phase 6: Pre-sync snapshots
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Creating pre-sync snapshots")
            await self._create_snapshots(SnapshotPhase.PRE)

            # Phase 7: Install/upgrade pc-switcher on target (after snapshots for rollback safety)
            if self._install_needed:
                self._logger.log(LogLevel.INFO, Host.TARGET, "Installing/upgrading pc-switcher on target")
                await self._install_on_target()

            # Phase 8: Execute sync jobs with background monitoring
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Starting sync operations")
            job_results = await self._execute_jobs(jobs)
            session.job_results = job_results

            # Phase 9: Post-sync snapshots
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Creating post-sync snapshots")
            await self._create_snapshots(SnapshotPhase.POST)

            # Success
            session.status = SessionStatus.COMPLETED
            session.ended_at = datetime.now().isoformat()
            self._logger.log(LogLevel.INFO, Host.SOURCE, "Sync completed successfully")

            return session

        except asyncio.CancelledError:
            session.status = SessionStatus.INTERRUPTED
            session.ended_at = datetime.now().isoformat()
            session.error_message = "Sync interrupted by user (SIGINT)"
            self._logger.log(LogLevel.WARNING, Host.SOURCE, "Sync interrupted by user")
            raise

        except Exception as e:
            session.status = SessionStatus.FAILED
            session.ended_at = datetime.now().isoformat()
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
        self._connection = Connection(self._target)
        await self._connection.connect()

        # Create executors
        self._local_executor = LocalExecutor()
        self._remote_executor = RemoteExecutor(self._connection.ssh_connection)

        # Get target hostname
        self._target_hostname = await self._remote_executor.get_hostname()
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

    async def _check_version_compatibility(self) -> None:
        """Check version compatibility - error if target is newer than source.

        Sets self._install_needed flag for later installation after snapshots.
        This separation ensures we can rollback if installation fails.
        """
        assert self._remote_executor is not None

        self._source_version = get_current_version()
        self._target_version = await get_target_version(self._remote_executor)

        if self._target_version is None:
            self._logger.log(
                LogLevel.INFO,
                Host.TARGET,
                f"pc-switcher not found on target, will install version {self._source_version}",
            )
            self._install_needed = True

        elif self._target_version > self._source_version:
            raise InstallationError(
                f"Target version {self._target_version} is newer than source {self._source_version}. "
                "This is unusual and may indicate a configuration issue."
            )

        elif self._target_version < self._source_version:
            self._logger.log(
                LogLevel.INFO,
                Host.TARGET,
                f"Target version {self._target_version} is outdated, will upgrade to {self._source_version}",
            )
            self._install_needed = True

        else:
            self._logger.log(
                LogLevel.INFO,
                Host.TARGET,
                f"Target version {self._target_version} matches source",
            )
            self._install_needed = False

    async def _install_on_target(self) -> None:
        """Install or upgrade pc-switcher on target machine.

        Called after pre-sync snapshots to ensure rollback capability.
        """
        assert self._remote_executor is not None
        assert self._source_version is not None

        if self._target_version is None:
            self._logger.log(
                LogLevel.INFO,
                Host.TARGET,
                f"Installing pc-switcher version {self._source_version}",
            )
        else:
            self._logger.log(
                LogLevel.INFO,
                Host.TARGET,
                f"Upgrading pc-switcher from {self._target_version} to {self._source_version}",
            )

        await install_on_target(self._remote_executor, self._source_version)

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
                jobs.append(job_class())

        # Check for config errors
        if config_errors:
            error_msgs = [f"  - {e.job}: {e.path} - {e.message}" for e in config_errors]
            raise RuntimeError("Job configuration validation failed:\n" + "\n".join(error_msgs))

        # Validate system state for all jobs (Phase 3)
        assert self._local_executor is not None
        assert self._remote_executor is not None

        context = JobContext(
            config={},  # Will be filled per job
            source=self._local_executor,
            target=self._remote_executor,
            event_bus=self._event_bus,
            session_id=self._session_id,
            source_hostname=self._source_hostname,
            target_hostname=self._target_hostname or "",
        )

        validation_errors: list[ValidationError] = []
        for job in jobs:
            job_config = self._config.job_configs.get(job.name, {})
            job_context = JobContext(
                config=job_config,
                source=context.source,
                target=context.target,
                event_bus=context.event_bus,
                session_id=context.session_id,
                source_hostname=context.source_hostname,
                target_hostname=context.target_hostname,
            )
            errors = await job.validate(job_context)
            if errors:
                validation_errors.extend(errors)

        if validation_errors:
            error_msgs = [f"  - {e.job} ({e.host.value}): {e.message}" for e in validation_errors]
            raise RuntimeError("System state validation failed:\n" + "\n".join(error_msgs))

        return jobs

    async def _create_snapshots(self, phase: SnapshotPhase) -> None:
        """Create btrfs snapshots on both source and target.

        Args:
            phase: PRE or POST snapshot phase
        """
        assert self._local_executor is not None
        assert self._remote_executor is not None

        snapshot_job = BtrfsSnapshotJob()
        snapshot_config = {
            "phase": phase.value,
            "subvolumes": self._config.btrfs_snapshots.subvolumes,
        }

        context = JobContext(
            config=snapshot_config,
            source=self._local_executor,
            target=self._remote_executor,
            event_bus=self._event_bus,
            session_id=self._session_id,
            source_hostname=self._source_hostname,
            target_hostname=self._target_hostname or "",
        )

        # Validate first
        errors = await snapshot_job.validate(context)
        if errors:
            error_msgs = [f"  - {e.host.value}: {e.message}" for e in errors]
            raise RuntimeError("Snapshot validation failed:\n" + "\n".join(error_msgs))

        # Execute
        await snapshot_job.execute(context)

    async def _execute_jobs(self, jobs: list[Job]) -> list[JobResult]:
        """Execute sync jobs sequentially with background disk monitoring.

        Args:
            jobs: List of validated jobs to execute

        Returns:
            List of JobResult for each executed job
        """
        assert self._local_executor is not None
        assert self._remote_executor is not None

        results: list[JobResult] = []

        async with asyncio.TaskGroup() as tg:
            self._task_group = tg

            # Start background disk space monitors for root filesystem
            source_monitor = DiskSpaceMonitorJob(host=Host.SOURCE, mount_point="/")
            target_monitor = DiskSpaceMonitorJob(host=Host.TARGET, mount_point="/")

            monitor_config = {
                "preflight_minimum": self._config.disk.preflight_minimum,
                "runtime_minimum": self._config.disk.runtime_minimum,
                "check_interval": self._config.disk.check_interval,
            }

            monitor_context = JobContext(
                config=monitor_config,
                source=self._local_executor,
                target=self._remote_executor,
                event_bus=self._event_bus,
                session_id=self._session_id,
                source_hostname=self._source_hostname,
                target_hostname=self._target_hostname or "",
            )

            tg.create_task(source_monitor.execute(monitor_context))
            tg.create_task(target_monitor.execute(monitor_context))

            # Execute sync jobs sequentially
            for job in jobs:
                job_config = self._config.job_configs.get(job.name, {})
                context = JobContext(
                    config=job_config,
                    source=self._local_executor,
                    target=self._remote_executor,
                    event_bus=self._event_bus,
                    session_id=self._session_id,
                    source_hostname=self._source_hostname,
                    target_hostname=self._target_hostname or "",
                )

                started_at = datetime.now().isoformat()
                try:
                    await job.execute(context)
                    ended_at = datetime.now().isoformat()
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
                    ended_at = datetime.now().isoformat()
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

        # Close event bus
        self._event_bus.close()
