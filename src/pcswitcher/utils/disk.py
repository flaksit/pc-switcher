"""Disk space monitoring utilities for pc-switcher."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path


class DiskMonitor:
    """Monitor disk space and trigger callbacks when space is low.

    Provides both one-time checks and continuous monitoring with
    configurable thresholds and callbacks.
    """

    def __init__(self) -> None:
        """Initialize DiskMonitor."""
        self._monitor_thread: threading.Thread | None = None
        self._stop_monitoring = threading.Event()

    @staticmethod
    def check_free_space(path: Path, min_free: float | str) -> tuple[bool, float, float]:
        """Check if free space meets minimum requirement.

        Args:
            path: Path to check (any path on the filesystem)
            min_free: Minimum free space as:
                - float 0.0-1.0: fraction of total space (e.g., 0.20 = 20%)
                - str percentage: "20%" = 20% of total space
                - int/float bytes: absolute bytes (e.g., 1073741824 = 1GB)

        Returns:
            Tuple of (is_sufficient, free_bytes, required_bytes)

        Raises:
            ValueError: If min_free format is invalid
            OSError: If path doesn't exist or is inaccessible
        """
        # Get disk usage
        stat = os.statvfs(path)
        total_bytes = stat.f_blocks * stat.f_frsize
        free_bytes = stat.f_bavail * stat.f_frsize

        # Parse min_free requirement
        if isinstance(min_free, str):
            # Handle percentage string like "20%"
            if min_free.endswith("%"):
                try:
                    percentage = float(min_free[:-1]) / 100.0
                    if not 0.0 <= percentage <= 1.0:
                        raise ValueError(f"Percentage {min_free} must be between 0% and 100%")
                    required_bytes = total_bytes * percentage
                except ValueError as e:
                    raise ValueError(f"Invalid percentage format '{min_free}': {e}") from e
            else:
                raise ValueError(
                    f"String min_free must be percentage (e.g., '20%'), got '{min_free}'"
                )
        elif isinstance(min_free, float):
            # Float between 0.0 and 1.0 is treated as fraction
            required_bytes = total_bytes * min_free if 0.0 <= min_free <= 1.0 else min_free
        elif isinstance(min_free, int):
            # Integer treated as absolute bytes
            required_bytes = float(min_free)
        else:
            raise ValueError(f"Invalid min_free type: {type(min_free)}")

        is_sufficient = free_bytes >= required_bytes
        return is_sufficient, free_bytes, required_bytes

    def monitor_continuously(
        self,
        path: Path,
        interval: int,
        reserve_minimum: float | str,
        callback: Callable[[float, float], None],
    ) -> None:
        """Start continuous disk space monitoring in background thread.

        Monitors disk space at regular intervals and calls callback when
        space falls below threshold.

        Args:
            path: Path to monitor (any path on the filesystem)
            interval: Check interval in seconds
            reserve_minimum: Minimum free space (same format as check_free_space)
            callback: Function called with (free_bytes, required_bytes) when space is low

        The callback will be called once when space drops below threshold,
        and again each time it drops further below.
        """
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            raise RuntimeError("Monitor is already running. Stop it first.")

        self._stop_monitoring.clear()

        def monitor_loop() -> None:
            """Background monitoring loop."""
            last_warning = False

            while not self._stop_monitoring.is_set():
                try:
                    is_sufficient, free_bytes, required_bytes = self.check_free_space(path, reserve_minimum)

                    # Trigger callback when space becomes insufficient
                    if not is_sufficient and not last_warning:
                        callback(free_bytes, required_bytes)
                        last_warning = True
                    elif is_sufficient:
                        last_warning = False

                except Exception:
                    # Silently ignore errors in monitoring loop to avoid crashing
                    pass

                # Sleep with ability to be interrupted
                self._stop_monitoring.wait(interval)

        self._monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self._monitor_thread.start()

    def stop_monitoring(self) -> None:
        """Stop continuous monitoring if running."""
        if self._monitor_thread is not None:
            self._stop_monitoring.set()
            self._monitor_thread.join(timeout=5.0)
            self._monitor_thread = None

    def is_monitoring(self) -> bool:
        """Check if continuous monitoring is active.

        Returns:
            True if monitoring thread is running, False otherwise
        """
        return self._monitor_thread is not None and self._monitor_thread.is_alive()


def format_bytes(bytes_value: float) -> str:
    """Format byte count as human-readable string.

    Args:
        bytes_value: Number of bytes

    Returns:
        Formatted string like "1.5 GB", "256 MB", etc.
    """
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(bytes_value)
    unit_index = 0

    while value >= 1024.0 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1

    return f"{value:.1f} {units[unit_index]}"
