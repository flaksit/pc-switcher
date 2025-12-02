"""Disk space monitoring background job."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

from pcswitcher.disk import check_disk_space, parse_threshold
from pcswitcher.models import (
    ConfigError,
    DiskSpaceCriticalError,
    Host,
    LogLevel,
    ProgressUpdate,
)

from .base import BackgroundJob
from .context import JobContext

if TYPE_CHECKING:
    from pcswitcher.models import ValidationError

# Type alias for parsed thresholds
ThresholdType = tuple[str, int | float]  # ("percent", 20.0) or ("absolute", bytes)


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
            "warning_threshold": {
                "type": "string",
                "description": "Free space threshold for warnings (e.g., '25%' or '50GiB')",
            },
            "check_interval": {
                "type": "integer",
                "minimum": 1,
                "description": "Seconds between disk space checks",
            },
        },
        "required": ["preflight_minimum", "runtime_minimum", "warning_threshold", "check_interval"],
        "additionalProperties": False,
    }

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> list[ConfigError]:
        """Validate config schema and threshold formats.

        Performs JSON schema validation first, then validates that threshold
        values are in valid format (percentage like "20%" or absolute like "50GiB").

        Args:
            config: Job configuration from config.yaml

        Returns:
            List of ConfigError for any validation failures.
        """
        errors = super().validate_config(config)
        if errors:
            return errors  # Don't continue if schema is invalid

        # Validate threshold formats (semantic validation)
        for key in ["preflight_minimum", "runtime_minimum", "warning_threshold"]:
            try:
                parse_threshold(config[key])
            except ValueError as e:
                errors.append(ConfigError(job=cls.name, path=key, message=str(e)))

        return errors

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

        # Parse thresholds once (validation already done in validate_config)
        self._preflight_threshold: ThresholdType = parse_threshold(
            context.config["preflight_minimum"]
        )
        self._runtime_threshold: ThresholdType = parse_threshold(
            context.config["runtime_minimum"]
        )
        self._warning_threshold: ThresholdType = parse_threshold(
            context.config["warning_threshold"]
        )
        self._check_interval: int = context.config["check_interval"]

    async def validate(self) -> list[ValidationError]:
        """Validate that mount point exists and is accessible.

        Returns:
            List of ValidationError if validation fails, empty list otherwise
        """
        errors: list[ValidationError] = []

        # Validate that mount point exists (threshold formats already validated in validate_config)
        executor = self.source if self.host == Host.SOURCE else self.target
        try:
            await check_disk_space(executor, self.mount_point)
        except RuntimeError as e:
            errors.append(self._validation_error(self.host, f"Mount point validation failed: {e}"))

        return errors

    async def execute(self) -> None:
        """Run continuous disk space monitoring.

        Checks disk space at configured interval and raises DiskSpaceCriticalError
        if available space drops below runtime_minimum threshold.

        Raises:
            DiskSpaceCriticalError: When disk space falls below threshold
            asyncio.CancelledError: When monitoring is cancelled
        """
        executor = self.source if self.host == Host.SOURCE else self.target
        hostname = self.context.source_hostname if self.host == Host.SOURCE else self.context.target_hostname

        # Use pre-parsed thresholds from __init__
        critical_type, critical_value = self._runtime_threshold
        warning_type, warning_value = self._warning_threshold
        runtime_minimum_str = self.context.config["runtime_minimum"]  # For error messages

        self._log(
            self.host,
            LogLevel.DEBUG,
            f"Starting disk space monitoring for {self.mount_point}",
            interval=self._check_interval,
            threshold=runtime_minimum_str,
        )

        try:
            while True:
                # Check disk space
                disk_space = await check_disk_space(executor, self.mount_point)

                # Report heartbeat
                self._report_progress(
                    ProgressUpdate(heartbeat=True),
                )

                # Check against critical threshold
                is_critical = False
                free_space_str = ""
                if critical_type == "percent":
                    # critical_value is percentage of total to keep free
                    # use_percent is percentage used
                    free_percent = 100 - disk_space.use_percent
                    if free_percent < critical_value:
                        is_critical = True
                        free_space_str = f"{free_percent:.1f}%"
                elif disk_space.available_bytes < critical_value:
                    is_critical = True
                    free_space_str = self._format_bytes(disk_space.available_bytes)

                if is_critical:
                    self._log(
                        self.host,
                        LogLevel.CRITICAL,
                        f"Disk space critically low on {hostname}",
                        mount_point=self.mount_point,
                        available_bytes=disk_space.available_bytes,
                        threshold=runtime_minimum_str,
                    )
                    raise DiskSpaceCriticalError(
                        host=self.host,
                        hostname=hostname,
                        free_space=free_space_str,
                        threshold=runtime_minimum_str,
                    )

                # Check against warning threshold
                is_warning = False
                if warning_type == "percent":
                    free_percent = 100 - disk_space.use_percent
                    if free_percent < warning_value:
                        is_warning = True
                elif disk_space.available_bytes < warning_value:
                    is_warning = True

                if is_warning:
                    self._log(
                        self.host,
                        LogLevel.WARNING,
                        f"Disk space getting low on {hostname}",
                        mount_point=self.mount_point,
                        available_bytes=disk_space.available_bytes,
                        available_formatted=self._format_bytes(disk_space.available_bytes),
                        warning_threshold=self.context.config["warning_threshold"],
                    )

                # Wait before next check
                await asyncio.sleep(self._check_interval)

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
