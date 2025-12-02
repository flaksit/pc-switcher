"""Unit tests for DiskSpaceMonitorJob."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.disk_space_monitor import DiskSpaceMonitorJob
from pcswitcher.models import CommandResult, Host


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
    async def test_validate_checks_mount_point_exists(
        self, mock_job_context: JobContext
    ) -> None:
        """validate() should check that mount point exists."""
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")
        with patch("pcswitcher.jobs.disk_space_monitor.check_disk_space", new_callable=AsyncMock):
            errors = await job.validate()
        # No errors when mount point check succeeds
        assert errors == []

    @pytest.mark.asyncio
    async def test_validate_reports_mount_point_error(
        self, mock_job_context: JobContext
    ) -> None:
        """validate() should report error when mount point check fails."""
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/nonexistent")
        with patch(
            "pcswitcher.jobs.disk_space_monitor.check_disk_space",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Mount point not found"),
        ):
            errors = await job.validate()
        assert len(errors) == 1
        assert "Mount point validation failed" in errors[0].message
