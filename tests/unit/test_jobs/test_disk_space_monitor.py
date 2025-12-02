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


class TestDiskSpaceMonitorValidation:
    """Test validate() method handles warning_threshold."""

    @pytest.mark.asyncio
    async def test_validate_checks_warning_threshold_format(
        self, mock_job_context: JobContext
    ) -> None:
        """validate() should check warning_threshold format."""
        # Valid config should pass - patch check_disk_space to avoid mount point check
        mock_job_context.config["warning_threshold"] = "25%"
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")
        with patch("pcswitcher.jobs.disk_space_monitor.check_disk_space", new_callable=AsyncMock):
            errors = await job.validate()
        # No error for warning_threshold format
        warning_errors = [e for e in errors if "warning_threshold" in e.message]
        assert warning_errors == []

    @pytest.mark.asyncio
    async def test_validate_rejects_invalid_warning_threshold_format(
        self, mock_job_context: JobContext
    ) -> None:
        """validate() should reject invalid warning_threshold format."""
        mock_job_context.config["warning_threshold"] = "invalid"
        job = DiskSpaceMonitorJob(mock_job_context, Host.SOURCE, "/")
        with patch("pcswitcher.jobs.disk_space_monitor.check_disk_space", new_callable=AsyncMock):
            errors = await job.validate()
        warning_errors = [e for e in errors if "warning_threshold" in e.message.lower()]
        assert len(warning_errors) == 1
        assert job.name in warning_errors[0].job
