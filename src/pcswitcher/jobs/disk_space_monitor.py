"""Disk space monitoring background job."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from pcswitcher.disk import check_disk_space, parse_threshold
from pcswitcher.models import (
    DiskSpaceCriticalError,
    Host,
    LogLevel,
    ProgressUpdate,
    ValidationError,
)

from .base import BackgroundJob
from .context import JobContext


class DiskSpaceMonitorJob(BackgroundJob):
    """Background job for continuous disk space monitoring.

    Monitors a single host (source or target) at regular intervals.
    Raises DiskSpaceCriticalError if available space drops below threshold.
    """

    name: ClassVar[str] = "disk_space_monitor"

    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "preflight_minimum": {
                "type": "string",
                "description": "Minimum free space before starting sync (e.g., '20%' or '50GiB')",
            },
            "runtime_minimum": {
                "type": "string",
                "description": "Minimum free space during sync (e.g., '15%' or '40GiB')",
            },
            "check_interval": {
                "type": "integer",
                "minimum": 1,
                "description": "Seconds between disk space checks",
            },
        },
        "required": ["preflight_minimum", "runtime_minimum", "check_interval"],
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext, host: Host, mount_point: str) -> None:
        """Initialize disk space monitor for a specific host.

        Args:
            context: JobContext with executors, config, and event bus
            host: Which machine to monitor (SOURCE or TARGET)
            mount_point: Mount point to monitor (e.g., "/home")
        """
        super().__init__(context)
        self.host = host
        self.mount_point = mount_point

    async def validate(self) -> list[ValidationError]:
        """Validate that mount point exists and threshold format is valid.

        Returns:
            List of ValidationError if validation fails, empty list otherwise
        """
        errors: list[ValidationError] = []

        # Validate threshold formats
        try:
            parse_threshold(self._context.config["preflight_minimum"])
        except ValueError as e:
            errors.append(
                ValidationError(
                    job=self.name,
                    host=self.host,
                    message=f"Invalid preflight_minimum: {e}",
                )
            )

        try:
            parse_threshold(self._context.config["runtime_minimum"])
        except ValueError as e:
            errors.append(
                ValidationError(
                    job=self.name,
                    host=self.host,
                    message=f"Invalid runtime_minimum: {e}",
                )
            )

        # Validate that mount point exists
        executor = self._context.source if self.host == Host.SOURCE else self._context.target
        try:
            await check_disk_space(executor, self.mount_point)
        except RuntimeError as e:
            errors.append(
                ValidationError(
                    job=self.name,
                    host=self.host,
                    message=f"Mount point validation failed: {e}",
                )
            )

        return errors

    async def execute(self) -> None:
        """Run continuous disk space monitoring.

        Checks disk space at configured interval and raises DiskSpaceCriticalError
        if available space drops below runtime_minimum threshold.

        Raises:
            DiskSpaceCriticalError: When disk space falls below threshold
            asyncio.CancelledError: When monitoring is cancelled
        """
        executor = self._context.source if self.host == Host.SOURCE else self._context.target
        hostname = self._context.source_hostname if self.host == Host.SOURCE else self._context.target_hostname
        check_interval: int = self._context.config["check_interval"]
        runtime_minimum: str = self._context.config["runtime_minimum"]

        threshold_type, threshold_value = parse_threshold(runtime_minimum)

        self._log(
            self.host,
            LogLevel.DEBUG,
            f"Starting disk space monitoring for {self.mount_point}",
            interval=check_interval,
            threshold=runtime_minimum,
        )

        try:
            while True:
                # Check disk space
                disk_space = await check_disk_space(executor, self.mount_point)

                # Report heartbeat
                self._report_progress(
                    ProgressUpdate(heartbeat=True),
                )

                # Check against threshold
                is_critical = False
                free_space_str = ""
                if threshold_type == "percent":
                    # threshold_value is percentage of total to keep free
                    # use_percent is percentage used
                    free_percent = 100 - disk_space.use_percent
                    if free_percent < threshold_value:
                        is_critical = True
                        free_space_str = f"{free_percent}%"
                elif disk_space.available_bytes < threshold_value:
                    is_critical = True
                    free_space_str = self._format_bytes(disk_space.available_bytes)

                if is_critical:
                    self._log(
                        self.host,
                        LogLevel.CRITICAL,
                        f"Disk space critically low on {hostname}",
                        mount_point=self.mount_point,
                        available_bytes=disk_space.available_bytes,
                        threshold=runtime_minimum,
                    )
                    raise DiskSpaceCriticalError(
                        host=self.host,
                        hostname=hostname,
                        free_space=free_space_str,
                        threshold=runtime_minimum,
                    )

                # Log warning if getting close (within 5% or 10GiB of threshold)
                warning_triggered = False
                if threshold_type == "percent":
                    free_percent = 100 - disk_space.use_percent
                    if free_percent < threshold_value + 5:
                        warning_triggered = True
                elif disk_space.available_bytes < threshold_value + (10 * 2**30):  # +10GiB
                    warning_triggered = True

                if warning_triggered:
                    self._log(
                        self.host,
                        LogLevel.WARNING,
                        f"Disk space getting low on {hostname}",
                        mount_point=self.mount_point,
                        available_bytes=disk_space.available_bytes,
                        available_formatted=self._format_bytes(disk_space.available_bytes),
                        threshold=runtime_minimum,
                    )

                # Wait before next check
                await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            self._log(
                self.host,
                LogLevel.DEBUG,
                f"Disk space monitoring cancelled for {self.mount_point}",
            )
            raise

    def _format_bytes(self, bytes_value: int) -> str:
        """Format bytes as human-readable string.

        Args:
            bytes_value: Number of bytes

        Returns:
            Formatted string like "45.2 GiB"
        """
        if bytes_value >= 2**30:
            return f"{bytes_value / 2**30:.1f} GiB"
        if bytes_value >= 2**20:
            return f"{bytes_value / 2**20:.1f} MiB"
        if bytes_value >= 2**10:
            return f"{bytes_value / 2**10:.1f} KiB"
        return f"{bytes_value} B"
