"""Core orchestration logic for pc-switcher sync operations."""

from __future__ import annotations

import importlib
import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pcswitcher.core.config import Configuration, validate_module_config
from pcswitcher.core.logging import LogLevel, get_logger
from pcswitcher.core.module import RemoteExecutor, SyncError, SyncModule
from pcswitcher.core.session import ModuleResult, SessionState, SyncSession
from pcswitcher.utils.disk import DiskMonitor, format_bytes

if TYPE_CHECKING:
    from pcswitcher.cli.ui import TerminalUI


class Orchestrator:
    """Coordinates sync operation lifecycle and module execution.

    The orchestrator manages the complete sync workflow:
    1. INITIALIZING - Setup logging, verify btrfs, load modules
    2. VALIDATING - Run all module validate() methods
    3. EXECUTING - Execute modules: pre_sync → sync → post_sync
    4. CLEANUP - Call abort() on current module if needed
    5. Terminal state - COMPLETED, ABORTED, or FAILED

    Handles errors, user interrupts, and provides rollback capability.
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

        self._modules: list[SyncModule] = []
        self._current_module: SyncModule | None = None
        self._disk_monitor = DiskMonitor()
        self._btrfs_snapshots_module: SyncModule | None = None
        self._start_time: datetime | None = None

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_interrupt)
        signal.signal(signal.SIGTERM, self._handle_interrupt)

    def run(self) -> SessionState:
        """Execute the complete sync workflow.

        Returns:
            Final session state (COMPLETED, ABORTED, or FAILED)
        """
        self._start_time = datetime.now(UTC)

        # Start UI if available
        if self.ui is not None:
            self.ui.start()

        try:
            # Phase 1: INITIALIZING
            self.session.set_state(SessionState.INITIALIZING)
            self.logger.info("Initializing sync session", target=self.remote.get_hostname())
            self._verify_btrfs_filesystem()
            self._load_modules()
            self._start_disk_monitoring()

            # Phase 2: VALIDATING
            self.session.set_state(SessionState.VALIDATING)
            self.logger.info("Validating modules")
            self._validate_all_modules()

            # Phase 3: EXECUTING
            self.session.set_state(SessionState.EXECUTING)
            self.logger.info("Starting module execution")
            self._execute_all_modules()

            # Phase 4: Determine final state
            final_state = self._determine_final_state()
            self.session.set_state(final_state)

        except KeyboardInterrupt:
            self.logger.warning("User interrupted sync operation")
            self.session.set_state(SessionState.ABORTED)
            self.session.abort_requested = True
            self._cleanup_phase()
            return SessionState.ABORTED

        except Exception as e:
            self.logger.critical(f"Unexpected error in orchestrator: {e}", exc_info=True)
            self.session.set_state(SessionState.FAILED)
            self._cleanup_phase()
            return SessionState.FAILED

        finally:
            self._stop_disk_monitoring()
            self._log_session_summary()
            self._show_ui_summary()

        # Offer rollback if FAILED with CRITICAL errors
        if self.session.state == SessionState.FAILED and self.session.has_errors:
            self._offer_rollback()

        return self.session.state

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
            self.logger.full("Btrfs filesystem verification passed")  # type: ignore[attr-defined]
        except subprocess.CalledProcessError as e:
            raise SyncError(f"Failed to check filesystem type: {e.stderr}") from e

    def _load_modules(self) -> None:
        """Load and instantiate enabled sync modules.

        Raises:
            SyncError: If module loading or validation fails
        """
        enabled_modules = [name for name, enabled in self.config.sync_modules.items() if enabled]

        for module_name in enabled_modules:
            try:
                # Import module class
                module_class = self._import_module_class(module_name)

                # Get module config
                module_config = self.config.module_configs.get(module_name, {})

                # Validate config against schema (create dummy instance to get schema)
                dummy_remote = self.remote
                temp_instance = module_class({}, dummy_remote)
                schema = temp_instance.get_config_schema()
                validate_module_config(module_name, module_config, schema)

                # Instantiate module
                module = module_class(module_config, self.remote)

                # Inject logging and progress methods
                self._inject_module_methods(module)

                self._modules.append(module)
                self.logger.full(f"Loaded module: {module.name}")  # type: ignore[attr-defined]

                # Store reference to btrfs-snapshots module
                if module.name == "btrfs-snapshots":
                    self._btrfs_snapshots_module = module

            except Exception as e:
                raise SyncError(f"Failed to load module '{module_name}': {e}") from e

        self.logger.info(f"Loaded {len(self._modules)} modules", modules=[m.name for m in self._modules])

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
        # Special handling for dummy modules
        if module_name.startswith("dummy_"):
            # dummy_success -> DummySuccessModule
            class_name = "".join(word.capitalize() for word in module_name.split("_")) + "Module"
        else:
            # btrfs_snapshots -> BtrfsSnapshotsModule
            class_name = "".join(word.capitalize() for word in module_name.split("_")) + "Module"

        module_path = f"pcswitcher.modules.{module_name}"

        try:
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        except (ImportError, AttributeError) as e:
            raise ImportError(f"Cannot import {class_name} from {module_path}: {e}") from e

    def _inject_module_methods(self, module: SyncModule) -> None:
        """Inject log() and emit_progress() methods into module.

        Args:
            module: Module to inject methods into
        """
        module_logger = get_logger(f"module.{module.name}", session_id=self.session.id)

        def log_method(level: LogLevel, message: str, **context: Any) -> None:
            """Module logging method that forwards to structlog."""
            # Map LogLevel to structlog method
            if level == LogLevel.DEBUG:
                module_logger.debug(message, **context)
            elif level == LogLevel.FULL:
                module_logger.full(message, **context)  # type: ignore[attr-defined]
            elif level == LogLevel.INFO:
                module_logger.info(message, **context)
            elif level == LogLevel.WARNING:
                module_logger.warning(message, **context)
            elif level == LogLevel.ERROR:
                module_logger.error(message, **context)
            elif level == LogLevel.CRITICAL:
                module_logger.critical(message, **context)

        def progress_method(
            percentage: float | None = None,
            item: str = "",
            eta: timedelta | None = None,
        ) -> None:
            """Module progress reporting method."""
            # Log at FULL level
            context: dict[str, Any] = {"module": module.name, "item": item}
            if percentage is not None:
                context["percentage"] = f"{percentage * 100:.1f}%"
            if eta is not None:
                context["eta"] = str(eta)

            module_logger.full(f"Progress: {item}", **context)  # type: ignore[attr-defined]

            # Forward to terminal UI if available
            if self.ui is not None and percentage is not None:
                self.ui.update_progress(module.name, percentage, item)

        module.log = log_method  # type: ignore[method-assign]
        module.emit_progress = progress_method  # type: ignore[method-assign]

    def _validate_all_modules(self) -> None:
        """Run validate() on all modules.

        Raises:
            SyncError: If any module validation fails
        """
        all_errors: list[str] = []

        for module in self._modules:
            self.logger.full(f"Validating module: {module.name}")  # type: ignore[attr-defined]
            errors = module.validate()

            if errors:
                all_errors.extend([f"[{module.name}] {error}" for error in errors])

        if all_errors:
            error_msg = "Validation failed:\n  " + "\n  ".join(all_errors)
            self.logger.critical(error_msg)
            raise SyncError(error_msg)

        self.logger.info("All modules validated successfully")

    def _execute_all_modules(self) -> None:
        """Execute all modules in sequence.

        Each module goes through: pre_sync → sync → post_sync
        """
        total_modules = len(self._modules)

        for idx, module in enumerate(self._modules):
            if self.session.abort_requested:
                self.logger.warning("Abort requested, stopping module execution")
                break

            self._current_module = module
            self.logger.info(f"Executing module: {module.name}")

            # Create UI task for this module
            if self.ui is not None:
                self.ui.create_module_task(module.name)
                self.ui.show_overall_progress(idx, total_modules)

            try:
                self._execute_module_lifecycle(module)
                self.session.module_results[module.name] = ModuleResult.SUCCESS
                self.logger.info(f"Module completed: {module.name}")

                # Mark module as 100% complete
                if self.ui is not None:
                    self.ui.update_progress(module.name, 1.0, "Complete")

            except SyncError as e:
                self.logger.critical(f"Module failed: {module.name}", error=str(e))
                self.session.module_results[module.name] = ModuleResult.FAILED
                self._cleanup_phase()
                self.session.abort_requested = True
                break

            except Exception as e:
                self.logger.critical(f"Unexpected error in module: {module.name}", error=str(e), exc_info=True)
                self.session.module_results[module.name] = ModuleResult.FAILED
                self._cleanup_phase()
                self.session.abort_requested = True
                break

        # Update overall progress to show completion
        if self.ui is not None:
            completed_modules = len([r for r in self.session.module_results.values() if r == ModuleResult.SUCCESS])
            self.ui.show_overall_progress(completed_modules, total_modules)

        self._current_module = None

    def _execute_module_lifecycle(self, module: SyncModule) -> None:
        """Execute complete lifecycle for a single module.

        Args:
            module: Module to execute

        Raises:
            SyncError: If any phase fails
        """
        # Pre-sync phase
        self.logger.full(f"Running pre_sync: {module.name}")  # type: ignore[attr-defined]
        module.pre_sync()

        # Sync phase
        self.logger.full(f"Running sync: {module.name}")  # type: ignore[attr-defined]
        module.sync()

        # Post-sync phase
        self.logger.full(f"Running post_sync: {module.name}")  # type: ignore[attr-defined]
        module.post_sync()

    def _cleanup_phase(self) -> None:
        """Call abort() on current module with timeout."""
        if self._current_module is not None:
            self.logger.info(f"Calling abort on module: {self._current_module.name}")
            try:
                self._current_module.abort(timeout=5.0)
            except Exception as e:
                self.logger.error(f"Error during abort: {e}")

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

    def _offer_rollback(self) -> None:
        """Offer user the option to rollback to pre-sync state.

        Only offered if btrfs-snapshots module is available and
        sync failed with CRITICAL errors.
        """
        if self._btrfs_snapshots_module is None:
            self.logger.warning("Cannot offer rollback: btrfs-snapshots module not available")
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
        if self._btrfs_snapshots_module is None:
            raise SyncError("Cannot rollback: btrfs-snapshots module not available")

        print("\nExecuting rollback...")
        self.logger.info("Starting rollback", session_id=self.session.id)

        try:
            # Call rollback method on btrfs-snapshots module
            from pcswitcher.modules.btrfs_snapshots import BtrfsSnapshotsModule

            if isinstance(self._btrfs_snapshots_module, BtrfsSnapshotsModule):
                self._btrfs_snapshots_module.rollback_to_presync(self.session.id)
                print("\nRollback completed successfully!")
                print("IMPORTANT: Reboot required for changes to take effect.")
                self.logger.info("Rollback completed successfully")
            else:
                raise SyncError("Invalid btrfs-snapshots module type")

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
        reserve_minimum = disk_config.get("reserve_minimum", 0.15)
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
            reserve_minimum=reserve_minimum,
            callback=disk_warning_callback,
        )

        # Pre-flight check
        min_free = disk_config.get("min_free", 0.20)
        is_sufficient, free_bytes, required_bytes = DiskMonitor.check_free_space(Path("/"), min_free)

        if not is_sufficient:
            raise SyncError(
                f"Insufficient disk space. Free: {format_bytes(free_bytes)}, "
                f"Required: {format_bytes(required_bytes)}"
            )

        self.logger.full(  # type: ignore[attr-defined]
            "Disk space check passed",
            free=format_bytes(free_bytes),
            required=format_bytes(required_bytes),
        )

    def _stop_disk_monitoring(self) -> None:
        """Stop continuous disk space monitoring."""
        self._disk_monitor.stop_monitoring()

    def _handle_interrupt(self, signum: int, frame: Any) -> None:
        """Handle interrupt signals (SIGINT, SIGTERM).

        Args:
            signum: Signal number
            frame: Current stack frame
        """
        self.logger.warning(f"Received signal {signum}, requesting abort")
        self.session.abort_requested = True

        # Raise KeyboardInterrupt to trigger cleanup
        raise KeyboardInterrupt
