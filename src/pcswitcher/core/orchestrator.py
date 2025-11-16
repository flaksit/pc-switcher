"""Core orchestration logic for pc-switcher sync operations.

The orchestrator is the central coordinator for all sync operations. It implements
a state machine that progresses through phases: INITIALIZING -> VALIDATING ->
EXECUTING -> terminal state (COMPLETED/ABORTED/FAILED).

Key responsibilities:
- Module lifecycle management (load, validate, execute, abort)
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

from pcswitcher.core.config import Configuration, validate_module_config
from pcswitcher.core.logging import LogLevel, get_logger
from pcswitcher.core.module import RemoteExecutor, SyncError, SyncModule
from pcswitcher.core.session import ModuleResult, SessionState, SyncSession
from pcswitcher.remote.installer import InstallationError, VersionManager
from pcswitcher.utils.disk import DiskMonitor, format_bytes

if TYPE_CHECKING:
    from pcswitcher.cli.ui import TerminalUI


class InterruptHandler:
    """Handles interrupt signals (SIGINT/SIGTERM) with double-interrupt detection.

    First interrupt initiates graceful shutdown with module abort.
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


class _ModuleCallbacks:
    """Implementation of SyncModuleCallbacks for a specific module.

    Created by Orchestrator for each module to provide logging and progress
    reporting capabilities without tight coupling.
    """

    def __init__(
        self,
        module_name: str,
        logger: Any,
        ui: TerminalUI | None,
    ) -> None:
        """Initialize callbacks for a module.

        Args:
            module_name: Name of the module these callbacks are for
            logger: Structlog logger for this module
            ui: Optional terminal UI for progress display
        """
        self._module_name = module_name
        self._logger = logger
        self._ui = ui

    def emit_progress(
        self,
        percentage: float | None = None,
        item: str = "",
        eta: timedelta | None = None,
    ) -> None:
        """Module progress reporting method."""
        context: dict[str, Any] = {"module": self._module_name, "item": item}
        if percentage is not None:
            context["percentage"] = f"{percentage * 100:.1f}%"
        if eta is not None:
            context["eta"] = str(eta)

        self._logger.log(LogLevel.FULL, f"Progress: {item}", **context)

        # Forward to terminal UI if available for real-time progress bars
        if self._ui is not None and percentage is not None:
            self._ui.update_progress(self._module_name, percentage, item)

    def log(self, level: LogLevel, message: str, **context: Any) -> None:
        """Module logging method that forwards to structlog."""
        if level == LogLevel.DEBUG:
            self._logger.debug(message, **context)
        elif level == LogLevel.FULL:
            self._logger.log(LogLevel.FULL, message, **context)
        elif level == LogLevel.INFO:
            self._logger.info(message, **context)
        elif level == LogLevel.WARNING:
            self._logger.warning(message, **context)
        elif level == LogLevel.ERROR:
            self._logger.error(message, **context)
        elif level == LogLevel.CRITICAL:
            self._logger.critical(message, **context)

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
            self._logger.log(
                level,
                f"... ({len(lines) - max_lines} more lines in {stream})",
                hostname=hostname,
                source="remote",
            )


class ModuleLifecycleManager:
    """Manages the lifecycle of sync modules: loading, validation, execution, and abort.

    Handles module instantiation, dependency injection, and orchestrating the
    module lifecycle phases (validate → pre_sync → sync → post_sync → abort).
    """

    def __init__(
        self,
        config: Configuration,
        remote: RemoteExecutor,
        session: SyncSession,
        ui: TerminalUI | None,
        logger: Any,
    ) -> None:
        """Initialize module lifecycle manager.

        Args:
            config: Validated configuration
            remote: Remote executor for target machine
            session: Sync session tracking state and results
            ui: Optional terminal UI for progress display
            logger: Logger for module operations
        """
        self._config = config
        self._remote = remote
        self._session = session
        self._ui = ui
        self._logger = logger
        self._modules: list[SyncModule] = []
        self._current_module: SyncModule | None = None
        self._btrfs_snapshots_module: SyncModule | None = None

    @property
    def modules(self) -> list[SyncModule]:
        """Get loaded modules."""
        return self._modules

    @property
    def current_module(self) -> SyncModule | None:
        """Get currently executing module."""
        return self._current_module

    @property
    def btrfs_snapshots_module(self) -> SyncModule | None:
        """Get btrfs_snapshots module if loaded."""
        return self._btrfs_snapshots_module

    def load_modules(self) -> None:
        """Load and instantiate enabled sync modules.

        Raises:
            SyncError: If module loading or validation fails
        """
        enabled_modules = [name for name, enabled in self._config.sync_modules.items() if enabled]

        for module_name in enabled_modules:
            try:
                # Import module class
                module_class = self._import_module_class(module_name)

                # Get module config
                module_config = self._config.module_configs.get(module_name, {})

                # Validate config against schema
                temp_instance = module_class({}, self._remote)
                schema = temp_instance.get_config_schema()
                validate_module_config(module_name, module_config, schema)

                # Instantiate module
                module = module_class(module_config, self._remote)

                # Inject callbacks
                self._inject_module_callbacks(module)

                self._modules.append(module)
                self._logger.log(LogLevel.FULL, f"Loaded module: {module.name}")

                # Store reference to btrfs_snapshots module
                if module.name == "btrfs_snapshots":
                    self._btrfs_snapshots_module = module

            except Exception as e:
                raise SyncError(f"Failed to load module '{module_name}': {e}") from e

        self._logger.info(f"Loaded {len(self._modules)} modules", modules=[m.name for m in self._modules])

    def _import_module_class(self, module_name: str) -> type[SyncModule]:
        """Import module class by name.

        Args:
            module_name: Module name with underscores (e.g., "btrfs_snapshots")

        Returns:
            Module class

        Raises:
            ImportError: If module cannot be imported
        """
        # Convert module name: btrfs_snapshots -> BtrfsSnapshotsModule
        class_name = "".join(word.capitalize() for word in module_name.split("_")) + "Module"
        module_path = f"pcswitcher.modules.{module_name}"

        try:
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise ImportError(f"Cannot import {class_name} from {module_path}: {e}") from e

    def _inject_module_callbacks(self, module: SyncModule) -> None:
        """Inject callbacks into module via composition pattern.

        Args:
            module: Module to inject callbacks into
        """
        module_logger = get_logger(f"module.{module.name}", session_id=self._session.id)
        callbacks = _ModuleCallbacks(module.name, module_logger, self._ui)
        module.set_callbacks(callbacks)

    def validate_all_modules(self) -> None:
        """Run validate() on all modules.

        Raises:
            SyncError: If any module validation fails
        """
        all_errors: list[str] = []

        for module in self._modules:
            self._logger.log(LogLevel.FULL, f"Validating module: {module.name}")
            errors = module.validate()

            if errors:
                all_errors.extend([f"[{module.name}] {error}" for error in errors])

        if all_errors:
            error_msg = "Validation failed:\n  " + "\n  ".join(all_errors)
            self._logger.critical(error_msg)
            raise SyncError(error_msg)

        self._logger.info("All modules validated successfully")

    def execute_all_modules(self) -> None:
        """Execute all modules in sequence.

        Each module goes through: pre_sync -> sync -> post_sync
        Module execution stops immediately on first failure, and abort() is called
        on the failing module to allow cleanup of partial state.
        """
        total_modules = len(self._modules)

        for idx, module in enumerate(self._modules):
            # Check for abort request
            if self._session.abort_requested:
                self._logger.warning("Abort requested, stopping module execution")
                break

            self._current_module = module
            self._logger.info(f"Executing module: {module.name}")

            # Create UI task for this module
            if self._ui is not None:
                self._ui.create_module_task(module.name)
                self._ui.show_overall_progress(idx, total_modules)

            try:
                self._execute_module_lifecycle(module)
                self._session.module_results[module.name] = ModuleResult.SUCCESS
                self._logger.info(f"Module completed: {module.name}")

                # Mark module as 100% complete in UI
                if self._ui is not None:
                    self._ui.update_progress(module.name, 1.0, "Complete")

            except SyncError as e:
                self._logger.critical(f"Module failed: {module.name}", error=str(e))
                self._session.module_results[module.name] = ModuleResult.FAILED
                self.cleanup_current_module()
                self._session.abort_requested = True
                break

            except Exception as e:
                self._logger.critical(f"Unexpected error in module: {module.name}", error=str(e), exc_info=True)
                self._session.module_results[module.name] = ModuleResult.FAILED
                self.cleanup_current_module()
                self._session.abort_requested = True
                break

        # Update overall progress
        if self._ui is not None:
            completed_modules = len([r for r in self._session.module_results.values() if r == ModuleResult.SUCCESS])
            self._ui.show_overall_progress(completed_modules, total_modules)

        self._current_module = None

    def _execute_module_lifecycle(self, module: SyncModule) -> None:
        """Execute complete lifecycle for a single module.

        Args:
            module: Module to execute

        Raises:
            SyncError: If any phase fails
        """
        self._logger.log(LogLevel.FULL, f"Running pre_sync: {module.name}")
        module.pre_sync()

        self._logger.log(LogLevel.FULL, f"Running sync: {module.name}")
        module.sync()

        self._logger.log(LogLevel.FULL, f"Running post_sync: {module.name}")
        module.post_sync()

    def cleanup_current_module(self) -> None:
        """Call abort() on current module with timeout."""
        if self._current_module is not None:
            self._logger.info(f"Calling abort on module: {self._current_module.name}")
            try:
                self._current_module.abort(timeout=5.0)
            except Exception as e:
                self._logger.error(f"Error during abort: {e}")


class Orchestrator:
    """Coordinates sync operation lifecycle and module execution.

    The orchestrator manages the complete sync workflow:
    1. INITIALIZING - Setup logging, verify btrfs, load modules
    2. VALIDATING - Run all module validate() methods
    3. EXECUTING - Execute modules: pre_sync → sync → post_sync
    4. CLEANUP - Call abort() on current module if needed
    5. Terminal state - COMPLETED, ABORTED, or FAILED

    Handles errors, user interrupts, and provides rollback capability.

    The orchestrator enforces several invariants:
    - btrfs_snapshots module must be first and enabled
    - Modules execute sequentially in configuration order
    - Exception-based error propagation (modules raise SyncError)
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

        self._module_manager = ModuleLifecycleManager(config, remote, session, ui, self.logger)

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
            # load modules, and start continuous monitoring
            self.session.set_state(SessionState.INITIALIZING)
            self.logger.info("Initializing sync session", target=self.remote.get_hostname())
            self._verify_btrfs_filesystem()
            self._ensure_version_sync()
            self._check_disk_space()
            self._module_manager.load_modules()
            self._start_disk_monitoring()

            # Phase 2: VALIDATING
            # All modules validate prerequisites before any state changes occur.
            # This fail-fast approach prevents partial sync from corrupting state.
            self.session.set_state(SessionState.VALIDATING)
            self.logger.info("Validating modules")
            self._module_manager.validate_all_modules()

            # Phase 3: EXECUTING
            # Execute each module's full lifecycle (pre_sync -> sync -> post_sync).
            # If any module fails, abort is called and execution stops.
            self.session.set_state(SessionState.EXECUTING)
            self.logger.info("Starting module execution")
            self._module_manager.execute_all_modules()

            # Phase 4: Determine final state based on execution results
            # COMPLETED = all modules succeeded without errors
            # FAILED = at least one module failed or ERROR/CRITICAL logged
            # ABORTED = user interrupt or explicit abort request
            final_state = self._determine_final_state()
            self.session.set_state(final_state)

        except KeyboardInterrupt:
            self.logger.warning("User interrupted sync operation")
            self.session.set_state(SessionState.ABORTED)
            self.session.abort_requested = True
            self._module_manager.cleanup_current_module()
            return SessionState.ABORTED

        except Exception as e:
            self.logger.critical(f"Unexpected error in orchestrator: {e}", exc_info=True)
            self.session.set_state(SessionState.FAILED)
            self._module_manager.cleanup_current_module()
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

        # Check if all modules completed successfully
        for module_name in self.session.enabled_modules:
            result = self.session.module_results.get(module_name)
            if result != ModuleResult.SUCCESS:
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
                            required_target_bytes = target_total_bytes * float(min_free_threshold)

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

        Only offered if btrfs_snapshots module is available and
        sync failed with CRITICAL errors.
        """
        btrfs_module = self._module_manager.btrfs_snapshots_module
        if btrfs_module is None:
            self.logger.warning("Cannot offer rollback: btrfs_snapshots module not available")
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
        btrfs_module = self._module_manager.btrfs_snapshots_module
        if btrfs_module is None:
            raise SyncError("Cannot rollback: btrfs_snapshots module not available")

        print("\nExecuting rollback...")
        self.logger.info("Starting rollback", session_id=self.session.id)

        try:
            # Call rollback method on btrfs_snapshots module
            from pcswitcher.modules.btrfs_snapshots import BtrfsSnapshotsModule

            if isinstance(btrfs_module, BtrfsSnapshotsModule):
                btrfs_module.rollback_to_presync(self.session.id)
                print("\nRollback completed successfully!")
                print("IMPORTANT: Reboot required for changes to take effect.")
                self.logger.info("Rollback completed successfully")
            else:
                raise SyncError("Invalid btrfs_snapshots module type")

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
            "modules_executed": len(self.session.module_results),
            "modules_succeeded": sum(1 for r in self.session.module_results.values() if r == ModuleResult.SUCCESS),
            "modules_failed": sum(1 for r in self.session.module_results.values() if r == ModuleResult.FAILED),
            "has_errors": self.session.has_errors,
        }

        self.logger.info("Session summary", **summary)

        # Log per-module results
        for module_name, result in self.session.module_results.items():
            self.logger.info(f"Module result: {module_name} = {result.value}")

    def _show_ui_summary(self) -> None:
        """Display session summary in terminal UI."""
        if self.ui is None:
            return

        duration = timedelta(0) if self._start_time is None else datetime.now(UTC) - self._start_time
        modules_succeeded = sum(1 for r in self.session.module_results.values() if r == ModuleResult.SUCCESS)
        modules_failed = sum(1 for r in self.session.module_results.values() if r == ModuleResult.FAILED)
        total_modules = len(self.session.module_results)

        self.ui.show_session_summary(
            state=self.session.state,
            duration=str(duration).split(".")[0],  # Remove microseconds
            modules_succeeded=modules_succeeded,
            modules_failed=modules_failed,
            total_modules=total_modules,
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
