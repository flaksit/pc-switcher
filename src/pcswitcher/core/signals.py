"""Signal handling for graceful shutdown and interrupt detection."""

from __future__ import annotations

import signal
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pcswitcher.core.job import SyncJob
    from pcswitcher.core.session import SyncSession


class InterruptHandler:
    """Handles SIGINT signals with graceful shutdown and force-terminate detection.

    Tracks interrupt requests and supports double-SIGINT detection for immediate
    termination. Coordinates with SyncSession to request abort and calls the
    current job's abort() method.
    """

    def __init__(self, session: SyncSession) -> None:
        """Initialize interrupt handler.

        Args:
            session: Current sync session to update on interrupt
        """
        self._session = session
        self._interrupt_event = threading.Event()
        self._first_interrupt_time: float | None = None
        self._current_job: SyncJob | None = None
        self._original_handlers: dict[int, Any] = {}
        self._lock = threading.Lock()

    def set_current_job(self, job: SyncJob | None) -> None:
        """Update the currently executing job.

        Args:
            job: Job currently being executed, or None if between jobs
        """
        with self._lock:
            self._current_job = job

    def handle_interrupt(self, signal_num: int, frame: Any) -> None:
        """Handle SIGINT signal with graceful shutdown or force-terminate.

        First interrupt: sets interrupt flag and initiates graceful shutdown.
        Second interrupt within 2 seconds: forces immediate termination.

        Args:
            signal_num: Signal number (SIGINT)
            frame: Current stack frame (unused)
        """
        with self._lock:
            current_time = time.monotonic()

            # Check for double-SIGINT (force terminate)
            if self._first_interrupt_time is not None:
                elapsed = current_time - self._first_interrupt_time
                if elapsed <= 2.0:
                    # Force terminate
                    print("\n\nForce terminating immediately...")
                    # Restore original handlers and re-raise
                    self._restore_handlers()
                    signal.raise_signal(signal.SIGINT)
                    return

            # First interrupt or after 2-second window
            self._first_interrupt_time = current_time
            self._interrupt_event.set()

            # Set abort flag on session
            self._session.abort_requested = True

            # Log the interruption
            print("\n\nInterrupt received. Initiating graceful shutdown...")
            print("Press Ctrl+C again within 2 seconds to force terminate.\n")

            # Get current job and call abort
            current_job = self._current_job

        # Call abort outside the lock to avoid deadlock
        if current_job is not None:
            try:
                current_job.abort(timeout=5.0)
            except Exception as e:
                print(f"Warning: Error during job abort: {e}")

    def install_handlers(self) -> None:
        """Install signal handlers for graceful shutdown."""
        self._original_handlers[signal.SIGINT] = signal.signal(signal.SIGINT, self.handle_interrupt)

    def _restore_handlers(self) -> None:
        """Restore original signal handlers."""
        for sig, handler in self._original_handlers.items():
            signal.signal(sig, handler)

    def cleanup(self) -> None:
        """Cleanup and restore original signal handlers."""
        self._restore_handlers()
        self._original_handlers.clear()

    def is_interrupted(self) -> bool:
        """Check if an interrupt has been requested.

        Returns:
            True if interrupt was requested, False otherwise
        """
        return self._interrupt_event.is_set()


def install_signal_handlers(
    session: SyncSession,
    current_job_ref: list[SyncJob | None] | None = None,
) -> InterruptHandler:
    """Install signal handlers for the sync session.

    Creates an InterruptHandler and installs it for SIGINT. If current_job_ref
    is provided, it should be a single-element list that will be updated with the
    current job being executed.

    Args:
        session: Current sync session
        current_job_ref: Optional mutable reference to current job

    Returns:
        InterruptHandler instance for managing interrupts
    """
    handler = InterruptHandler(session)
    handler.install_handlers()
    return handler
