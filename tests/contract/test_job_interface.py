"""Contract tests verifying job interface compliance."""

from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from pcswitcher.jobs import BackgroundJob, JobContext, SyncJob, SystemJob
from pcswitcher.models import ConfigError, Host, LogLevel, ProgressUpdate, ValidationError


class ExampleTestJob(SyncJob):
    """A minimal job implementation for testing the contract."""

    name: ClassVar[str] = "example_test"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "test_value": {"type": "string"},
        },
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        self._log(Host.SOURCE, LogLevel.INFO, "Test execution")
        self._report_progress(ProgressUpdate(percent=100))


class InvalidSchemaJob(SyncJob):
    """Job with schema that should catch validation errors."""

    name: ClassVar[str] = "invalid_schema_test"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "required_field": {"type": "integer"},
        },
        "required": ["required_field"],
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        pass


@pytest.fixture
def mock_job_context() -> JobContext:
    """Create a mock JobContext for testing."""
    return JobContext(
        config={},
        source=MagicMock(),
        target=MagicMock(),
        event_bus=MagicMock(),
        session_id="test1234",
        source_hostname="source-host",
        target_hostname="target-host",
    )


class TestJobContract:
    """Test that jobs follow the interface contract."""

    def test_job_has_name_attribute(self) -> None:
        """Jobs must have a name class attribute."""
        assert hasattr(ExampleTestJob, "name")
        assert ExampleTestJob.name == "example_test"

    def test_job_has_config_schema(self) -> None:
        """Jobs must have a CONFIG_SCHEMA class attribute."""
        assert hasattr(ExampleTestJob, "CONFIG_SCHEMA")
        assert isinstance(ExampleTestJob.CONFIG_SCHEMA, dict)

    def test_validate_config_returns_empty_list_for_valid_config(self) -> None:
        """validate_config should return empty list for valid config."""
        errors = ExampleTestJob.validate_config({"test_value": "hello"})
        assert errors == []

    def test_validate_config_returns_errors_for_invalid_config(self) -> None:
        """validate_config should return ConfigError list for invalid config."""
        errors = InvalidSchemaJob.validate_config({})
        assert len(errors) == 1
        assert isinstance(errors[0], ConfigError)
        assert errors[0].job == "invalid_schema_test"
        assert "required_field" in errors[0].message

    def test_validate_config_with_wrong_type(self) -> None:
        """validate_config should catch type mismatches."""
        errors = InvalidSchemaJob.validate_config({"required_field": "not_an_int"})
        assert len(errors) == 1
        assert "integer" in errors[0].message.lower()

    @pytest.mark.asyncio
    async def test_validate_returns_validation_error_list(self, mock_job_context: JobContext) -> None:
        """validate() must return a list of ValidationError."""
        job = ExampleTestJob(mock_job_context)
        errors = await job.validate()
        assert isinstance(errors, list)

    @pytest.mark.asyncio
    async def test_execute_completes_without_error(self, mock_job_context: JobContext) -> None:
        """execute() should complete without raising."""
        job = ExampleTestJob(mock_job_context)
        await job.execute()

    @pytest.mark.asyncio
    async def test_log_helper_publishes_event(self, mock_job_context: JobContext) -> None:
        """_log() should publish LogEvent to EventBus."""
        job = ExampleTestJob(mock_job_context)
        job._log(Host.SOURCE, LogLevel.INFO, "Test message")  # pyright: ignore[reportPrivateUsage]
        mock_job_context.event_bus.publish.assert_called_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_progress_helper_publishes_event(self, mock_job_context: JobContext) -> None:
        """_report_progress() should publish ProgressEvent to EventBus."""
        job = ExampleTestJob(mock_job_context)
        job._report_progress(ProgressUpdate(percent=50))  # pyright: ignore[reportPrivateUsage]
        mock_job_context.event_bus.publish.assert_called_once()  # type: ignore[union-attr]


class TestJobHierarchy:
    """Test job class hierarchy."""

    def test_sync_job_is_not_required(self) -> None:
        """SyncJob should have required=False."""
        assert SyncJob.required is False

    def test_system_job_is_required(self) -> None:
        """SystemJob should have required=True."""
        assert SystemJob.required is True

    def test_background_job_is_required(self) -> None:
        """BackgroundJob should have required=True."""
        assert BackgroundJob.required is True
