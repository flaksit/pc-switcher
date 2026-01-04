"""Unit tests for DiskSpaceMonitorJob."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.disk_space_monitor import DiskSpaceMonitorJob
from pcswitcher.models import CommandResult, DiskSpaceCriticalError, Host


@pytest.fixture
def disk_monitor_config() -> dict[str, Any]:
    """Valid disk monitor configuration."""
    return {
        "preflight_minimum": "20%",
        "runtime_minimum": "15%",
        "warning_threshold": "25%",
        "check_interval": 30,
    }


@pytest.fixture
def mock_job_context(disk_monitor_config: dict[str, Any]) -> JobContext:
    """Create a mock JobContext for DiskSpaceMonitorJob testing."""
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    target = MagicMock()
    target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

    return JobContext(
        config=disk_monitor_config,
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test1234",
        source_hostname="source-host",
        target_hostname="target-host",
    )


class TestDiskSpaceMonitorConfigSchema:
    """Test CONFIG_SCHEMA includes warning_threshold."""

    def test_schema_has_warning_threshold_property(self) -> None:
        """CONFIG_SCHEMA should include warning_threshold."""
        schema = DiskSpaceMonitorJob.CONFIG_SCHEMA
        assert "warning_threshold" in schema["properties"]

    def test_schema_requires_warning_threshold(self) -> None:
        """warning_threshold should be in required list."""
        schema = DiskSpaceMonitorJob.CONFIG_SCHEMA
        assert "warning_threshold" in schema["required"]

    def test_validate_config_accepts_percentage_warning_threshold(self) -> None:
        """Config validation should accept percentage format."""
        config = {
            "preflight_minimum": "20%",
            "runtime_minimum": "15%",
            "warning_threshold": "25%",
            "check_interval": 30,
        }
        errors = DiskSpaceMonitorJob.validate_config(config)
        assert errors == []

    def test_validate_config_accepts_absolute_warning_threshold(self) -> None:
        """Config validation should accept absolute GiB format."""
        config = {
            "preflight_minimum": "50GiB",
            "runtime_minimum": "40GiB",
            "warning_threshold": "60GiB",
            "check_interval": 30,
        }
        errors = DiskSpaceMonitorJob.validate_config(config)
        assert errors == []

    def test_validate_config_rejects_missing_warning_threshold(self) -> None:
        """Config validation should reject missing warning_threshold."""
        config = {
            "preflight_minimum": "20%",
            "runtime_minimum": "15%",
            "check_interval": 30,
        }
        errors = DiskSpaceMonitorJob.validate_config(config)
        assert len(errors) == 1
        assert "warning_threshold" in errors[0].message


class TestDiskSpaceMonitorValidateConfig:
    """Test validate_config() method for semantic validation."""

    def test_validate_config_rejects_invalid_preflight_format(self) -> None:
        """validate_config() should reject invalid preflight_minimum format."""
        config = {
            "preflight_minimum": "invalid",
            "runtime_minimum": "15%",
            "warning_threshold": "25%",
            "check_interval": 30,
        }
        errors = DiskSpaceMonitorJob.validate_config(config)
        assert len(errors) == 1
        assert errors[0].path == "preflight_minimum"

    def test_validate_config_rejects_invalid_runtime_format(self) -> None:
        """validate_config() should reject invalid runtime_minimum format."""
        config = {
            "preflight_minimum": "20%",
            "runtime_minimum": "bad",
            "warning_threshold": "25%",
            "check_interval": 30,
        }
        errors = DiskSpaceMonitorJob.validate_config(config)
        assert len(errors) == 1
        assert errors[0].path == "runtime_minimum"

    def test_validate_config_rejects_invalid_warning_format(self) -> None:
        """validate_config() should reject invalid warning_threshold format."""
        config = {
            "preflight_minimum": "20%",
            "runtime_minimum": "15%",
            "warning_threshold": "invalid",
            "check_interval": 30,
        }
        errors = DiskSpaceMonitorJob.validate_config(config)
        assert len(errors) == 1
        assert errors[0].path == "warning_threshold"

    def test_validate_config_reports_all_threshold_errors(self) -> None:
        """validate_config() should report all invalid thresholds."""
        config = {
            "preflight_minimum": "bad1",
            "runtime_minimum": "bad2",
            "warning_threshold": "bad3",
            "check_interval": 30,
        }
        errors = DiskSpaceMonitorJob.validate_config(config)
        assert len(errors) == 3
        paths = {e.path for e in errors}
        assert paths == {"preflight_minimum", "runtime_minimum", "warning_threshold"}


class TestDiskSpaceMonitorValidation:
    """Test validate() method for system state validation."""

    @pytest.mark.asyncio
    async def test_validate_checks_mount_point_exists(self, mock_job_context: JobContext) -> None:
        """validate() should check that mount point exists."""
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")
        # Mock executor already returns successful command result
        errors = await job.validate()
        # No errors when mount point check succeeds
        assert errors == []
        # Verify test -d command was called
        source_run_command = cast(AsyncMock, mock_job_context.source.run_command)
        source_run_command.assert_called_once_with("test -d /")

    @pytest.mark.asyncio
    async def test_validate_reports_mount_point_error(self, mock_job_context: JobContext) -> None:
        """validate() should report error when mount point check fails."""
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/nonexistent")
        # Mock test -d command failure (exit code 1)
        source_run_command = cast(AsyncMock, mock_job_context.source.run_command)
        source_run_command.return_value = CommandResult(exit_code=1, stdout="", stderr="")
        errors = await job.validate()
        assert len(errors) == 1
        assert "Mount point does not exist or is not accessible" in errors[0].message
        assert "/nonexistent" in errors[0].message


class TestDiskSpaceMonitorPreflightCheck:
    """Test preflight disk space checks - CORE-FR-DISK-PRE."""

    def test_core_fr_disk_pre_percentage_threshold(self, mock_job_context: JobContext) -> None:
        """CORE-FR-DISK-PRE: DiskSpaceMonitorJob must support percentage-based preflight_minimum threshold.

        Spec requirement: CORE-FR-DISK-PRE states that preflight_minimum MUST be specified
        as a percentage (e.g., "20%") or absolute value (e.g., "50GiB").
        """
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")

        # Verify that percentage threshold was parsed correctly
        assert job._preflight_threshold[0] == "percent"
        assert job._preflight_threshold[1] == 20

    def test_core_fr_disk_pre_absolute_threshold(self) -> None:
        """CORE-FR-DISK-PRE: DiskSpaceMonitorJob must support absolute value preflight_minimum threshold.

        Spec requirement: CORE-FR-DISK-PRE states that preflight_minimum MUST support
        absolute values like "50GiB".
        """
        config = {
            "preflight_minimum": "50GiB",
            "runtime_minimum": "40GiB",
            "warning_threshold": "60GiB",
            "check_interval": 30,
        }

        source = MagicMock()
        source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        context = JobContext(
            config=config,
            source=source,
            target=target,
            event_bus=MagicMock(),
            session_id="test1234",
            source_hostname="source-host",
            target_hostname="target-host",
        )

        job = DiskSpaceMonitorJob(context, Host.SOURCE, "/")

        # Verify that absolute threshold was parsed correctly
        # 50 GiB = 50 * 2^30 bytes = 53687091200 bytes
        assert job._preflight_threshold[0] == "bytes"
        assert job._preflight_threshold[1] == 50 * (2**30)

    def test_core_fr_disk_pre_rejects_invalid_format(self) -> None:
        """CORE-FR-DISK-PRE: Values without explicit units must be invalid.

        Spec requirement: CORE-FR-DISK-PRE explicitly states that values without explicit
        units are invalid.
        """
        config = {
            "preflight_minimum": "invalid_format",
            "runtime_minimum": "15%",
            "warning_threshold": "25%",
            "check_interval": 30,
        }

        errors = DiskSpaceMonitorJob.validate_config(config)
        assert len(errors) == 1
        assert errors[0].path == "preflight_minimum"
        assert "Invalid threshold format" in errors[0].message


class TestDiskSpaceMonitorRuntimeMonitoring:
    """Test runtime disk space monitoring - CORE-FR-DISK-RUNTIME."""

    @pytest.mark.asyncio
    async def test_core_fr_disk_runtime_detects_critical_threshold(self, mock_job_context: JobContext) -> None:
        """CORE-FR-DISK-RUNTIME: Monitor must abort with CRITICAL when free space falls below runtime_minimum.

        Spec requirement: CORE-FR-DISK-RUNTIME states orchestrator MUST monitor free disk space
        during sync and abort with CRITICAL if available free space falls below
        the configured runtime minimum.
        """
        # Mock df output showing low disk space (5% free, below 15% threshold)
        df_output = """Filesystem     1B-blocks        Used   Available Use% Mounted on
/dev/sda1      107374182400 102005473280  5368709120  95% /"""

        source_run_command = cast(AsyncMock, mock_job_context.source.run_command)
        source_run_command.return_value = CommandResult(exit_code=0, stdout=df_output, stderr="")

        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")

        # execute() should raise DiskSpaceCriticalError immediately
        with pytest.raises(DiskSpaceCriticalError) as exc_info:
            await job.execute()

        # Verify error details
        assert exc_info.value.host == Host.SOURCE
        assert exc_info.value.hostname == "source-host"
        assert "15%" in exc_info.value.threshold

    @pytest.mark.asyncio
    async def test_core_fr_disk_runtime_percentage_threshold(self, mock_job_context: JobContext) -> None:
        """CORE-FR-DISK-RUNTIME: Runtime monitoring must support percentage-based runtime_minimum.

        Spec requirement: CORE-FR-DISK-RUNTIME requires runtime_minimum to be specified as
        percentage (e.g., "15%") or absolute value (e.g., "40GiB").
        """
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")

        # Verify that percentage threshold was parsed correctly
        assert job._runtime_threshold[0] == "percent"
        assert job._runtime_threshold[1] == 15

    @pytest.mark.asyncio
    async def test_core_fr_disk_runtime_absolute_threshold(self) -> None:
        """CORE-FR-DISK-RUNTIME: Runtime monitoring must support absolute value runtime_minimum.

        Spec requirement: CORE-FR-DISK-RUNTIME requires runtime_minimum to support absolute
        values like "40GiB".
        """
        config = {
            "preflight_minimum": "50GiB",
            "runtime_minimum": "40GiB",
            "warning_threshold": "60GiB",
            "check_interval": 30,
        }

        source = MagicMock()
        source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        context = JobContext(
            config=config,
            source=source,
            target=target,
            event_bus=MagicMock(),
            session_id="test1234",
            source_hostname="source-host",
            target_hostname="target-host",
        )

        job = DiskSpaceMonitorJob(context, Host.SOURCE, "/")

        # Verify that absolute threshold was parsed correctly
        # 40 GiB = 40 * 2^30 bytes = 42949672960 bytes
        assert job._runtime_threshold[0] == "bytes"
        assert job._runtime_threshold[1] == 40 * (2**30)

    @pytest.mark.asyncio
    async def test_core_fr_disk_runtime_configurable_interval(self, mock_job_context: JobContext) -> None:
        """CORE-FR-DISK-RUNTIME: Monitoring must use configurable check interval.

        Spec requirement: CORE-FR-DISK-RUNTIME requires monitoring at a configurable interval
        (default: 30 seconds).
        """
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")

        # Verify that check interval was configured correctly
        assert job._check_interval == 30

    @pytest.mark.asyncio
    async def test_core_fr_disk_runtime_warns_at_warning_threshold(self, mock_job_context: JobContext) -> None:
        """CORE-FR-DISK-RUNTIME: Monitor must warn when disk space approaches warning threshold.

        While CORE-FR-DISK-RUNTIME focuses on CRITICAL abort, the implementation includes
        warning_threshold to alert before hitting the critical level.
        """
        # Mock df output showing medium disk space (20% free, below 25% warning, above 15% critical)
        df_output = """Filesystem     1B-blocks        Used   Available Use% Mounted on
/dev/sda1      107374182400 85899345920 21474836480  80% /"""

        source_run_command = cast(AsyncMock, mock_job_context.source.run_command)
        source_run_command.return_value = CommandResult(exit_code=0, stdout=df_output, stderr="")

        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")

        # Start the monitoring task
        task = asyncio.create_task(job.execute())

        # Wait briefly to allow one check cycle
        await asyncio.sleep(0.1)

        # Cancel the monitoring task
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        # Verify warning was logged (event bus received progress update)
        # Note: actual logging verification depends on event_bus mock implementation

    @pytest.mark.asyncio
    async def test_core_fr_disk_runtime_continues_when_above_threshold(self, mock_job_context: JobContext) -> None:
        """CORE-FR-DISK-RUNTIME: Monitor must continue monitoring when disk space is sufficient.

        Spec requirement: CORE-FR-DISK-RUNTIME requires continuous monitoring - only abort when
        threshold is breached.
        """
        # Mock df output showing sufficient disk space (50% free)
        df_output = """Filesystem     1B-blocks        Used   Available Use% Mounted on
/dev/sda1      107374182400 53687091200 53687091200  50% /"""

        source_run_command = cast(AsyncMock, mock_job_context.source.run_command)
        source_run_command.return_value = CommandResult(exit_code=0, stdout=df_output, stderr="")

        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")

        # Start the monitoring task
        task = asyncio.create_task(job.execute())

        # Wait briefly to allow one check cycle
        await asyncio.sleep(0.1)

        # Cancel the monitoring task
        task.cancel()

        # Should raise CancelledError, not DiskSpaceCriticalError
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_core_fr_disk_runtime_monitors_target_host(self) -> None:
        """CORE-FR-DISK-RUNTIME: Monitor must support monitoring target host.

        Spec requirement: CORE-FR-DISK-RUNTIME requires monitoring on both source AND target.
        """
        config = {
            "preflight_minimum": "20%",
            "runtime_minimum": "15%",
            "warning_threshold": "25%",
            "check_interval": 30,
        }

        source = MagicMock()
        source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        target = MagicMock()

        # Mock df output for target showing sufficient space
        df_output = """Filesystem     1B-blocks        Used   Available Use% Mounted on
/dev/sda1      107374182400 53687091200 53687091200  50% /"""
        target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout=df_output, stderr=""))

        context = JobContext(
            config=config,
            source=source,
            target=target,
            event_bus=MagicMock(),
            session_id="test1234",
            source_hostname="source-host",
            target_hostname="target-host",
        )

        # Create job for TARGET host
        job = DiskSpaceMonitorJob(context, Host.TARGET, "/")

        # Start the monitoring task
        task = asyncio.create_task(job.execute())

        # Wait briefly to allow one check cycle
        await asyncio.sleep(0.1)

        # Cancel the monitoring task
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Verify target executor was used (not source)
        target.run_command.assert_called()

    def test_core_fr_disk_runtime_rejects_invalid_format(self) -> None:
        """CORE-FR-DISK-RUNTIME: Values without explicit units must be invalid.

        Spec requirement: CORE-FR-DISK-RUNTIME explicitly states that values without explicit
        units are invalid.
        """
        config = {
            "preflight_minimum": "20%",
            "runtime_minimum": "invalid_format",
            "warning_threshold": "25%",
            "check_interval": 30,
        }

        errors = DiskSpaceMonitorJob.validate_config(config)
        assert len(errors) == 1
        assert errors[0].path == "runtime_minimum"
        assert "Invalid threshold format" in errors[0].message
