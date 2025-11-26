"""Core orchestration logic for pc-switcher sync operations.

The orchestrator is the central coordinator for all sync operations. It implements
a state machine that progresses through phases: INITIALIZING -> VALIDATING ->
EXECUTING -> terminal state (COMPLETED/ABORTED/FAILED).

Key responsibilities:
- Job lifecycle management (load, validate, execute, abort)
- Error handling and graceful shutdown
- Disk space monitoring during sync
- Signal handling (SIGINT/SIGTERM)
- Progress reporting to terminal UI
- Session state tracking and rollback coordination
"""

from __future__ import annotations

import importlib
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pcswitcher.core.config import Configuration, validate_job_config
from pcswitcher.core.logging import LogLevel, get_logger
from pcswitcher.core.job import RemoteExecutor, SyncError, SyncJob
from pcswitcher.core.session import JobResult, SessionState, SyncSession
from pcswitcher.core.snapshot import SnapshotCallbacks, SnapshotManager
from pcswitcher.remote.installer import InstallationError, VersionManager
from pcswitcher.utils.disk import DiskMonitor, format_bytes, parse_disk_threshold

if TYPE_CHECKING:
    from pcswitcher.cli.ui import TerminalUI


class InterruptHandler:
    """Handles interrupt signals (SIGINT/SIGTERM) with double-interrupt detection.

    First interrupt initiates graceful shutdown with job abort.
    Second interrupt within 2 seconds force terminates immediately.
    """

    def __init__(self, session: SyncSession, logger: Any) -> None:
        """Initialize interrupt handler.

        Args:
            session: Sync session to mark as aborted
            logger: Logger for interrupt events
        """
        self._session = session
        self._logger = logger
        self._first_interrupt_time: float | None = None
        self._interrupt_lock = threading.Lock()

    def register_handlers(self) -> None:
        """Register signal handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self._handle_interrupt)
        signal.signal(signal.SIGTERM, self._handle_interrupt)

    def _handle_interrupt(self, signum: int, frame: Any) -> None:
        """Handle interrupt signals with double-SIGINT detection.

        Args:
            signum: Signal number
            frame: Current stack frame (unused but required by signal API)
        """
        current_time = time.time()

        with self._interrupt_lock:
            # Check for double-SIGINT (force terminate)
            if self._first_interrupt_time is not None:
                elapsed = current_time - self._first_interrupt_time
                if elapsed <= 2.0:
                    # Force terminate immediately
                    self._logger.critical("Second interrupt received within 2 seconds - force terminating")
                    import sys

                    sys.exit(130)  # SIGINT exit code

            # First interrupt or after 2-second window
            self._first_interrupt_time = current_time
            self._logger.warning("Sync interrupted by user")
            self._session.abort_requested = True

        # Raise KeyboardInterrupt to trigger cleanup
        raise KeyboardInterrupt


class _JobCallbacks:
    """Implementation of SyncJobCallbacks for a specific job.

    Created by Orchestrator for each job to provide logging and progress
    reporting capabilities without tight coupling.
    """

    def __init__(
        self,
        job_name: str,
        logger: Any,
        ui: TerminalUI | None,
    ) -> None:
        """Initialize callbacks for a job.

        Args:
            job_name: Name of the job these callbacks are for
            logger: Structlog logger for this job
            ui: Optional terminal UI for progress display
        """
        self._job_name = job_name
        self._logger = logger
        self._ui = ui

    def emit_progress(
        self,
        percentage: float | None = None,
        item: str = "",
        eta: timedelta | None = None,
    ) -> None:
        """Job progress reporting method."""
        context: dict[str, Any] = {"job": self._job_name, "item": item}
        if percentage is not None:
            context["percentage"] = f"{percentage * 100:.1f}%"
        if eta is not None:
            context["eta"] = str(eta)

        self.log(LogLevel.FULL, f"Progress: {item}", **context)

        # Forward to terminal UI if available for real-time progress bars
        if self._ui is not None and percentage is not None:
            self._ui.update_progress(self._job_name, percentage, item)

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """Job logging method that forwards to structlog."""
        self._logger.log(level, message, **context)

    def log_remote_output(
        self,
        hostname: str,
        output: str,
        stream: str = "stdout",
        level: LogLevel = LogLevel.FULL,
    ) -> None:
        """Log remote command output for cross-host log aggregation."""
        if not output.strip():
            return

        # Split output into lines and process
        lines = output.rstrip().split("\n")
        max_lines = 10
        displayed_lines = lines[:max_lines]

        for line in displayed_lines:
            if line.strip():
                self._logger.log(
                    level,
                    f"[{stream.upper()}] {line}",
                    hostname=hostname,
                    source="remote",
                )

        # Show truncation notice if there are more lines
        if len(lines) > max_lines:
            self.log(
                level,
                f"... ({len(lines) - max_lines} more lines in {stream})",
                hostname=hostname,
                source="remote",
            )


class JobLifecycleManager:
    """Manages the lifecycle of sync jobs: loading, validation, execution, and abort.

    Handles job instantiation, dependency injection, and orchestrating the
    job lifecycle phases (validate → pre_sync → sync → post_sync → abort).
    """

    def __init__(
        self,
        config: Configuration,
        remote: RemoteExecutor,
        session: SyncSession,
        ui: TerminalUI | None,
        logger: Any,
    ) -> None:
        """Initialize job lifecycle manager.

        Args:
            config: Validated configuration
            remote: Remote executor for target machine
            session: Sync session tracking state and results
            ui: Optional terminal UI for progress display
            logger: Logger for job operations
        """
        self._config = config
        self._remote = remote
        self._session = session
        self._ui = ui
        self._logger = logger
        self._jobs: list[SyncJob] = []
        self._current_job: SyncJob | None = None
        self._snapshot_manager: SnapshotManager | None = None

    @property
    def jobs(self) -> list[SyncJob]:
        """Get loaded jobs."""
        return self._jobs

    @property
    def current_job(self) -> SyncJob | None:
        """Get currently executing job."""
        return self._current_job

    @property
    def snapshot_manager(self) -> SnapshotManager | None:
        """Get snapshot manager if loaded."""
        return self._snapshot_manager

    def load_snapshot_manager(self) -> None:
        """Load and instantiate the snapshot manager (btrfs_snapshots).

        This is orchestrator-level infrastructure, not a SyncJob.

        Raises:
            SyncError: If snapshot manager loading fails
        """
        try:
            from pcswitcher.jobs.btrfs_snapshots import BtrfsSnapshotsJob

            # Get btrfs_snapshots config
            snapshot_config = self._config.job_configs.get("btrfs_snapshots", {})

            # Validate config against schema
            temp_instance = BtrfsSnapshotsJob({}, self._remote)
            schema = temp_instance.get_config_schema()
            validate_job_config("btrfs_snapshots", snapshot_config, schema)

            # Instantiate snapshot manager
            self._snapshot_manager = BtrfsSnapshotsJob(snapshot_config, self._remote)

            # Inject callbacks for logging
            snapshot_logger = get_logger("snapshot_manager", session_id=self._session.id)
            callbacks = SnapshotCallbacks(snapshot_logger, self._ui)
            self._snapshot_manager.set_callbacks(callbacks)

            self._logger.log(LogLevel.FULL, "Loaded snapshot manager")

        except Exception as e:
            raise SyncError(f"Failed to load snapshot manager: {e}") from e

    def load_jobs(self) -> None:
        """Load and instantiate enabled sync jobs.

        Note: btrfs_snapshots is no longer a SyncJob. It's now orchestrator-level
        infrastructure and is loaded separately via load_snapshot_manager().

        Raises:
            SyncError: If job loading or validation fails
        """
        enabled_jobs = [name for name, enabled in self._config.sync_jobs.items() if enabled]

        for job_name in enabled_jobs:
            # Skip btrfs_snapshots - it's now orchestrator-level infrastructure
            if job_name == "btrfs_snapshots":
                continue

            try:
                # Import job class
                job_class = self._import_job_class(job_name)

                # Get job config
                job_config = self._config.job_configs.get(job_name, {})

                # Validate config against schema
                temp_instance = job_class({}, self._remote)
                schema = temp_instance.get_config_schema()
                validate_job_config(job_name, job_config, schema)

                # Instantiate job
                job = job_class(job_config, self._remote)

                # Inject callbacks
                self._inject_job_callbacks(job)

                self._jobs.append(job)
                self._logger.log(LogLevel.FULL, f"Loaded job: {job.name}")

            except Exception as e:
                raise SyncError(f"Failed to load job '{job_name}': {e}") from e

        self._logger.info(f"Loaded {len(self._jobs)} jobs", jobs=[j.name for j in self._jobs])

    def _import_job_class(self, job_name: str) -> type[SyncJob]:
        """Import job class by name.

        Args:
            job_name: Job name with underscores (e.g., "btrfs_snapshots")

        Returns:
            Job class

        Raises:
            ImportError: If job cannot be imported
        """
        # Convert job name: btrfs_snapshots -> BtrfsSnapshotsJob
        class_name = "".join(word.capitalize() for word in job_name.split("_")) + "Job"
        module_path = f"pcswitcher.jobs.{job_name}"

        try:
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise ImportError(f"Cannot import {class_name} from {module_path}: {e}") from e

    def _inject_job_callbacks(self, job: SyncJob) -> None:
        """Inject callbacks into job via composition pattern.

        Args:
            job: Job to inject callbacks into
        """
        job_logger = get_logger(f"job.{job.name}", session_id=self._session.id)
        callbacks = _JobCallbacks(job.name, job_logger, self._ui)
        job.set_callbacks(callbacks)

    def validate_all_jobs(self) -> None:
        """Run validate() on all jobs.

        Raises:
            SyncError: If any job validation fails
        """
        all_errors: list[str] = []

        for job in self._jobs:
            self._logger.log(LogLevel.FULL, f"Validating job: {job.name}")
            errors = job.validate()

            if errors:
                all_errors.extend([f"[{job.name}] {error}" for error in errors])

        if all_errors:
            error_msg = "Validation failed:\n  " + "\n  ".join(all_errors)
            self._logger.critical(error_msg)
            raise SyncError(error_msg)

        self._logger.info("All jobs validated successfully")

    def execute_all_jobs(self) -> None:
        """Execute all jobs in sequence.

        Each job goes through: pre_sync -> sync -> post_sync
        Job execution stops immediately on first failure, and abort() is called
        on the failing job to allow cleanup of partial state.
        """
        total_jobs = len(self._jobs)

        for idx, job in enumerate(self._jobs):
            # Check for abort request
            if self._session.abort_requested:
                self._logger.warning("Abort requested, stopping job execution")
                break

            self._current_job = job
            self._logger.info(f"Executing job: {job.name}")

            # Create UI task for this job
            if self._ui is not None:
                self._ui.create_job_task(job.name)
                self._ui.show_overall_progress(idx, total_jobs)

            try:
                self._execute_job_lifecycle(job)
                self._session.job_results[job.name] = JobResult.SUCCESS
                self._logger.info(f"Job completed: {job.name}")

                # Mark job as 100% complete in UI
                if self._ui is not None:
                    self._ui.update_progress(job.name, 1.0, "Complete")

            except SyncError as e:
                self._logger.critical(f"Job failed: {job.name}", error=str(e))
                self._session.job_results[job.name] = JobResult.FAILED
                self.cleanup_current_job()
                self._session.abort_requested = True
                break

            except Exception as e:
                self._logger.critical(f"Unexpected error in job: {job.name}", error=str(e), exc_info=True)
                self._session.job_results[job.name] = JobResult.FAILED
                self.cleanup_current_job()
                self._session.abort_requested = True
                break

        # Update overall progress
        if self._ui is not None:
            completed_jobs = len([r for r in self._session.job_results.values() if r == JobResult.SUCCESS])
            self._ui.show_overall_progress(completed_jobs, total_jobs)

        self._current_job = None

    def _execute_job_lifecycle(self, job: SyncJob) -> None:
        """Execute complete lifecycle for a single job.

        Args:
            job: Job to execute

        Raises:
            SyncError: If any phase fails
        """
        self._logger.log(LogLevel.FULL, f"Running pre_sync: {job.name}")
        job.pre_sync()

        self._logger.log(LogLevel.FULL, f"Running sync: {job.name}")
        job.sync()

        self._logger.log(LogLevel.FULL, f"Running post_sync: {job.name}")
        job.post_sync()

    def cleanup_current_job(self) -> None:
        """Call abort() on current job with timeout."""
        if self._current_job is not None:
            self._logger.info(f"Calling abort on job: {self._current_job.name}")
            try:
                self._current_job.abort(timeout=5.0)
            except Exception as e:
                self._logger.error(f"Error during abort: {e}")


class Orchestrator:
    """Coordinates sync operation lifecycle and job execution.

    The orchestrator manages the complete sync workflow:
    1. INITIALIZING - Setup logging, verify btrfs, load jobs
    2. VALIDATING - Run all job validate() methods
    3. EXECUTING - Execute jobs: pre_sync → sync → post_sync
    4. CLEANUP - Call abort() on current job if needed
    5. Terminal state - COMPLETED, ABORTED, or FAILED

    Handles errors, user interrupts, and provides rollback capability.

    The orchestrator enforces several invariants:
    - btrfs_snapshots job must be first and enabled
    - Jobs execute sequentially in configuration order
    - Exception-based error propagation (jobs raise SyncError)
    - Session.has_errors tracks whether ERROR/CRITICAL logs occurred
    """

    def __init__(
        self,
        config: Configuration,
        remote: RemoteExecutor,
        session: SyncSession,
        ui: TerminalUI | None = None,
    ) -> None:
        """Initialize Orchestrator.

        Args:
            config: Validated configuration
            remote: Remote executor for target machine
            session: Sync session tracking state and results
            ui: Optional terminal UI for progress display
        """
        self.config = config
        self.remote = remote
        self.session = session
        self.ui = ui
        self.logger = get_logger("orchestrator", session_id=session.id)

        self._disk_monitor = DiskMonitor()
        self._start_time: datetime | None = None
        self._cli_invocation_time: float | None = None  # For startup performance tracking

        # Composed components
        self._interrupt_handler = InterruptHandler(session, self.logger)
        self._interrupt_handler.register_handlers()

        self._job_manager = JobLifecycleManager(config, remote, session, ui, self.logger)

    def set_cli_invocation_time(self, invocation_time: float) -> None:
        """Set the CLI invocation time for startup performance measurement.

        Args:
            invocation_time: Unix timestamp when CLI was invoked (from time.time())
        """
        self._cli_invocation_time = invocation_time

    def run(self) -> SessionState:
        """Execute the complete sync workflow.

        Returns:
            Final session state (COMPLETED, ABORTED, or FAILED)
        """
        self._start_time = datetime.now(UTC)

        # Log startup performance if CLI invocation time was set
        if self._cli_invocation_time is not None:
            startup_duration_ms = (time.time() - self._cli_invocation_time) * 1000
            self.logger.info(
                "Startup performance",
                startup_ms=f"{startup_duration_ms:.2f}",
                phase="orchestrator.run() entered",
            )

        # Start UI if available
        if self.ui is not None:
            self.ui.start()

        try:
            # Phase 1: INITIALIZING
            # This phase sets up the sync environment: verify prerequisites,
            # load jobs, and start continuous monitoring
            self.session.set_state(SessionState.INITIALIZING)
            self.logger.info("Initializing sync session", target=self.remote.get_hostname())
            self._verify_btrfs_filesystem()
            self._ensure_version_sync()
            self._check_disk_space()
            self._job_manager.load_snapshot_manager()
            self._job_manager.load_jobs()
            self._start_disk_monitoring()

            # Phase 2: VALIDATING
            # Validate snapshot infrastructure and all jobs before any state changes.
            # This fail-fast approach prevents partial sync from corrupting state.
            self.session.set_state(SessionState.VALIDATING)
            self.logger.info("Validating configuration")

            # Validate snapshot subvolumes first
            if self._job_manager.snapshot_manager is not None:
                self.logger.info("Validating btrfs subvolumes")
                snapshot_errors = self._job_manager.snapshot_manager.validate_subvolumes()
                if snapshot_errors:
                    for error in snapshot_errors:
                        self.logger.critical(f"Snapshot validation error: {error}")
                    raise SyncError(f"Subvolume validation failed: {snapshot_errors[0]}")

            self.logger.info("Validating jobs")
            self._job_manager.validate_all_jobs()

            # Phase 3: EXECUTING
            # Create pre-sync snapshots, execute jobs, create post-sync snapshots.
            # If any job fails, abort is called and execution stops.
            self.session.set_state(SessionState.EXECUTING)

            # Create pre-sync snapshots before any job runs
            if self._job_manager.snapshot_manager is not None:
                self.logger.info("Creating pre-sync snapshots")
                self._job_manager.snapshot_manager.create_presync_snapshots(self.session.id)

            # Execute all sync jobs
            self.logger.info("Starting job execution")
            self._job_manager.execute_all_jobs()

            # Create post-sync snapshots after all jobs complete successfully
            if self._job_manager.snapshot_manager is not None:
                self.logger.info("Creating post-sync snapshots")
                self._job_manager.snapshot_manager.create_postsync_snapshots(self.session.id)

            # Phase 4: Determine final state based on execution results
            # COMPLETED = all jobs succeeded without errors
            # FAILED = at least one job failed or ERROR/CRITICAL logged
            # ABORTED = user interrupt or explicit abort request
            final_state = self._determine_final_state()
            self.session.set_state(final_state)

        except KeyboardInterrupt:
            self.logger.warning("User interrupted sync operation")
            self.session.set_state(SessionState.ABORTED)
            self.session.abort_requested = True
            self._job_manager.cleanup_current_job()
            return SessionState.ABORTED

        except Exception as e:
            self.logger.critical(f"Unexpected error in orchestrator: {e}", exc_info=True)
            self.session.set_state(SessionState.FAILED)
            self._job_manager.cleanup_current_job()
            return SessionState.FAILED

        finally:
            self._stop_disk_monitoring()
            self._log_session_summary()
            self._show_ui_summary()

        # Offer rollback if FAILED with CRITICAL errors
        if self.session.state == SessionState.FAILED and self.session.has_errors:
            self._offer_rollback()

        return self.session.state

    def _determine_final_state(self) -> SessionState:
        """Determine final session state based on execution results.

        Returns:
            COMPLETED if successful, ABORTED if interrupted, FAILED if errors occurred
        """
        if self.session.abort_requested:
            return SessionState.ABORTED

        if self.session.has_errors:
            return SessionState.FAILED

        # Check if all jobs completed successfully
        for job_name in self.session.enabled_jobs:
            result = self.session.job_results.get(job_name)
            if result != JobResult.SUCCESS:
                return SessionState.FAILED

        return SessionState.COMPLETED

    def _verify_btrfs_filesystem(self) -> None:
        """Verify that root filesystem is btrfs.

        Raises:
            SyncError: If filesystem is not btrfs
        """
        try:
            result = subprocess.run(
                ["stat", "-f", "-c", "%T", "/"],
                capture_output=True,
                text=True,
                check=True,
            )
            fs_type = result.stdout.strip()
            if fs_type != "btrfs":
                raise SyncError(f"Root filesystem is {fs_type}, not btrfs. PC-switcher requires btrfs.")
            self.logger.log(LogLevel.FULL, "Btrfs filesystem verification passed")
        except subprocess.CalledProcessError as e:
            raise SyncError(f"Failed to check filesystem type: {e.stderr}") from e

    def _check_disk_space(self) -> None:
        """Check disk space on both source and target machines.

        Verifies minimum free space requirements before sync starts.
        Configuration from disk section in config (self.config.disk).

        Raises:
            SyncError: If disk space is insufficient on either machine
        """
        # Get disk thresholds from canonical config location (self.config.disk)
        min_free_threshold = self.config.disk.get("preflight_minimum", "20%")  # Default 20%

        try:
            from pcswitcher.utils.disk import DiskMonitor

            # Check local (source) disk space
            is_sufficient, free_bytes, required_bytes = DiskMonitor.check_free_space(
                Path("/"), min_free_threshold
            )

            if not is_sufficient:
                error_msg = (
                    f"Insufficient disk space on source machine. "
                    f"Required: {format_bytes(required_bytes)}, Available: {format_bytes(free_bytes)}"
                )
                self.logger.critical(error_msg)
                raise SyncError(error_msg)

            self.logger.info(
                "Source disk space check passed",
                free_space=format_bytes(free_bytes),
                required=format_bytes(required_bytes),
            )

            # Check remote (target) disk space via SSH
            # Use Python on remote for robust parsing (avoids awk parsing issues)
            try:
                result = self.remote.run(
                    "python3 -c 'import shutil; st=shutil.disk_usage(\"/\"); print(st.free, st.total)'",
                    timeout=10.0,
                )
                if result.returncode == 0:
                    try:
                        parts = result.stdout.strip().split()
                        if len(parts) >= 2:
                            target_free_bytes = int(parts[0])
                            target_total_bytes = int(parts[1])
                            # Parse threshold with units (e.g., "20%" or "50GiB")
                            required_target_bytes = parse_disk_threshold(
                                min_free_threshold, target_total_bytes
                            )

                            if target_free_bytes < required_target_bytes:
                                error_msg = (
                                    f"Insufficient disk space on target machine. "
                                    f"Required: {format_bytes(required_target_bytes)}, "
                                    f"Available: {format_bytes(target_free_bytes)}"
                                )
                                self.logger.critical(error_msg)
                                raise SyncError(error_msg)

                            self.logger.info(
                                "Target disk space check passed",
                                free_space=format_bytes(target_free_bytes),
                                required=format_bytes(required_target_bytes),
                            )
                        else:
                            self.logger.warning(f"Unexpected output format from target disk check: {result.stdout}")
                    except (ValueError, IndexError) as e:
                        self.logger.warning(f"Failed to parse target disk space: {e}")
                else:
                    self.logger.warning(f"Target disk space check failed: {result.stderr}")

            except Exception as e:
                self.logger.warning(f"Failed to check target disk space: {e}")

        except SyncError:
            raise
        except Exception as e:
            error_msg = f"Disk space check failed: {e}"
            self.logger.critical(error_msg)
            raise SyncError(error_msg) from e

    def _ensure_version_sync(self) -> None:
        """Ensure target machine has matching pc-switcher version.

        Checks target version and installs/upgrades if needed. Aborts if
        target version is newer than source (downgrade prevention).

        Uses VersionManager which installs via `uv tool install pc-switcher==<version>`
        from GitHub Package Registry (ghcr.io).

        Raises:
            SyncError: If version check fails or installation fails
        """
        try:
            # Pass RemoteExecutor interface (not private connection) to VersionManager
            version_manager = VersionManager(self.remote, session_id=self.session.id)

            # Get versions
            local_version = version_manager.get_local_version()
            target_version = version_manager.get_target_version()

            # Ensure version sync (VersionManager handles logging internally)
            version_manager.ensure_version_sync(local_version, target_version)

        except InstallationError as e:
            # Log the error at CRITICAL level to trigger abort
            error_msg = f"Version synchronization failed: {e}"
            self.logger.critical(error_msg)
            raise SyncError(error_msg) from e
        except Exception as e:
            # Catch unexpected errors during version management
            error_msg = f"Unexpected error during version check: {e}"
            self.logger.critical(error_msg)
            raise SyncError(error_msg) from e

    def _offer_rollback(self) -> None:
        """Offer user the option to rollback to pre-sync state.

        Only offered if snapshot manager is available and
        sync failed with CRITICAL errors.
        """
        snapshot_mgr = self._job_manager.snapshot_manager
        if snapshot_mgr is None:
            self.logger.warning("Cannot offer rollback: snapshot manager not available")
            return

        self.logger.warning("Sync failed. Rollback to pre-sync snapshots is available.")
        print("\n" + "=" * 60)
        print("SYNC FAILED")
        print("=" * 60)
        print("\nThe sync operation failed. You can rollback to the pre-sync state.")
        print("This will restore all configured subvolumes to their state before sync started.")
        print("\nWARNING: This is a destructive operation. All changes since sync started will be lost.")

        # Prompt user for confirmation
        response = input("\nDo you want to rollback? (yes/no): ").strip().lower()

        if response in ("yes", "y"):
            self.logger.info("User confirmed rollback")
            self._execute_rollback()
        else:
            self.logger.info("User declined rollback")
            print("\nRollback cancelled. System remains in current state.")
            print(f"To rollback later, use: pc-switcher rollback {self.session.id}")

    def _execute_rollback(self) -> None:
        """Execute rollback to pre-sync state.

        Raises:
            SyncError: If rollback fails
        """
        snapshot_mgr = self._job_manager.snapshot_manager
        if snapshot_mgr is None:
            raise SyncError("Cannot rollback: snapshot manager not available")

        print("\nExecuting rollback...")
        self.logger.info("Starting rollback", session_id=self.session.id)

        try:
            # Call rollback method on snapshot manager
            snapshot_mgr.rollback_to_presync(self.session.id)
            print("\nRollback completed successfully!")
            print("IMPORTANT: Reboot required for changes to take effect.")
            self.logger.info("Rollback completed successfully")

        except SyncError as e:
            print(f"\nRollback failed: {e}")
            self.logger.critical(f"Rollback failed: {e}")
            raise

    def _log_session_summary(self) -> None:
        """Log final session summary with results and statistics."""
        duration = timedelta(0) if self._start_time is None else datetime.now(UTC) - self._start_time

        summary = {
            "session_id": self.session.id,
            "final_state": self.session.state.value,
            "duration": str(duration),
            "jobs_executed": len(self.session.job_results),
            "jobs_succeeded": sum(1 for r in self.session.job_results.values() if r == JobResult.SUCCESS),
            "jobs_failed": sum(1 for r in self.session.job_results.values() if r == JobResult.FAILED),
            "has_errors": self.session.has_errors,
        }

        self.logger.info("Session summary", **summary)

        # Log per-job results
        for job_name, result in self.session.job_results.items():
            self.logger.info(f"Job result: {job_name} = {result.value}")

    def _show_ui_summary(self) -> None:
        """Display session summary in terminal UI."""
        if self.ui is None:
            return

        duration = timedelta(0) if self._start_time is None else datetime.now(UTC) - self._start_time
        jobs_succeeded = sum(1 for r in self.session.job_results.values() if r == JobResult.SUCCESS)
        jobs_failed = sum(1 for r in self.session.job_results.values() if r == JobResult.FAILED)
        total_jobs = len(self.session.job_results)

        self.ui.show_session_summary(
            state=self.session.state,
            duration=str(duration).split(".")[0],  # Remove microseconds
            jobs_succeeded=jobs_succeeded,
            jobs_failed=jobs_failed,
            total_jobs=total_jobs,
        )

    def _start_disk_monitoring(self) -> None:
        """Start continuous disk space monitoring."""
        disk_config = self.config.disk
        runtime_minimum = disk_config.get("runtime_minimum", "15%")
        check_interval = disk_config.get("check_interval", 30)

        def disk_warning_callback(free_bytes: float, required_bytes: float) -> None:
            """Called when disk space drops below threshold."""
            self.logger.warning(
                "Low disk space detected",
                free=format_bytes(free_bytes),
                required=format_bytes(required_bytes),
            )
            print(
                f"\nWARNING: Low disk space! Free: {format_bytes(free_bytes)}, "
                f"Required: {format_bytes(required_bytes)}"
            )

        self._disk_monitor.monitor_continuously(
            path=Path("/"),
            interval=check_interval,
            reserve_minimum=runtime_minimum,
            callback=disk_warning_callback,
        )

        # Pre-flight check
        preflight_minimum = disk_config.get("preflight_minimum", "20%")
        is_sufficient, free_bytes, required_bytes = DiskMonitor.check_free_space(Path("/"), preflight_minimum)

        if not is_sufficient:
            raise SyncError(
                f"Insufficient disk space. Free: {format_bytes(free_bytes)}, Required: {format_bytes(required_bytes)}"
            )

        self.logger.log(
            LogLevel.FULL,
            "Disk space check passed",
            free=format_bytes(free_bytes),
            required=format_bytes(required_bytes),
        )

    def _stop_disk_monitoring(self) -> None:
        """Stop continuous disk space monitoring."""
        self._disk_monitor.stop_monitoring()
