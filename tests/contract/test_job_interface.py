"""Contract tests verifying job interface compliance."""

from __future__ import annotations

import logging
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
    async def test_log_helper_logs_to_stdlib(
        self, mock_job_context: JobContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_log() should log via stdlib logging."""
        job = ExampleTestJob(mock_job_context)
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"):
            job._log(Host.SOURCE, LogLevel.INFO, "Test message")  # pyright: ignore[reportPrivateUsage]
        assert len(caplog.records) == 1
        assert caplog.records[0].message == "Test message"
        assert caplog.records[0].levelno == LogLevel.INFO

    @pytest.mark.asyncio
    async def test_progress_helper_publishes_event(self, mock_job_context: JobContext) -> None:
        """_report_progress() should publish ProgressEvent to EventBus."""
        job = ExampleTestJob(mock_job_context)
        job._report_progress(ProgressUpdate(percent=50))  # pyright: ignore[reportPrivateUsage]
        mock_job_context.event_bus.publish.assert_called_once()  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_001_core_fr_job_iface(self, mock_job_context: JobContext) -> None:
        """CORE-FR-JOB-IFACE: Job interface defines standardized methods.

        Verifies that the Job interface includes:
        - validate() method
        - execute() method
        - CONFIG_SCHEMA for config declaration
        - name property
        """
        # Verify validate() method exists and returns list of ValidationError
        job = ExampleTestJob(mock_job_context)
        errors = await job.validate()
        assert isinstance(errors, list)

        # Verify execute() method exists
        await job.execute()

        # Verify CONFIG_SCHEMA class attribute exists
        assert hasattr(ExampleTestJob, "CONFIG_SCHEMA")
        assert isinstance(ExampleTestJob.CONFIG_SCHEMA, dict)

        # Verify name property exists
        assert hasattr(ExampleTestJob, "name")
        assert isinstance(ExampleTestJob.name, str)

    @pytest.mark.asyncio
    async def test_001_us1_as2_config_schema_validation(self) -> None:
        """US1-AS2: Job defines config schema, system validates.

        Verifies that:
        - Job can define configuration schema
        - System validates job config against schema
        - System applies defaults for missing values
        - Invalid config produces ConfigError
        """
        # Test valid config passes validation
        valid_config = {"test_value": "hello"}
        errors = ExampleTestJob.validate_config(valid_config)
        assert errors == []

        # Test invalid config produces ConfigError
        invalid_config = {"test_value": 123}  # Should be string
        errors = ExampleTestJob.validate_config(invalid_config)
        assert len(errors) == 1
        assert isinstance(errors[0], ConfigError)
        assert errors[0].job == "example_test"

        # Test missing required field produces ConfigError
        errors = InvalidSchemaJob.validate_config({})
        assert len(errors) == 1
        assert isinstance(errors[0], ConfigError)
        assert "required_field" in errors[0].message

    @pytest.mark.asyncio
    async def test_001_us1_as3_job_logging_at_all_levels(
        self, mock_job_context: JobContext, caplog: pytest.LogCaptureFixture
    ) -> None:
        """US1-AS3: Job emits log messages at six levels.

        Verifies that jobs can emit logs at all six levels:
        DEBUG, FULL, INFO, WARNING, ERROR, CRITICAL
        """
        job = ExampleTestJob(mock_job_context)

        # Test each log level
        with caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"):
            for level in [
                LogLevel.DEBUG,
                LogLevel.FULL,
                LogLevel.INFO,
                LogLevel.WARNING,
                LogLevel.ERROR,
                LogLevel.CRITICAL,
            ]:
                job._log(Host.SOURCE, level, f"Test message at {level.name}")  # pyright: ignore[reportPrivateUsage]

        # Verify all 6 log records were created
        assert len(caplog.records) == 6

        # Verify each level was logged correctly
        for i, level in enumerate(
            [LogLevel.DEBUG, LogLevel.FULL, LogLevel.INFO, LogLevel.WARNING, LogLevel.ERROR, LogLevel.CRITICAL]
        ):
            record = caplog.records[i]
            assert record.levelno == level.value
            assert record.message == f"Test message at {level.name}"
            assert record.__dict__.get("job") == "example_test"
            assert record.__dict__.get("host") == "source"

    @pytest.mark.asyncio
    async def test_001_us1_as4_job_progress_reporting(self, mock_job_context: JobContext) -> None:
        """US1-AS4: Job emits progress updates.

        Verifies that jobs can emit progress updates with:
        - Percentage
        - Current item
        - Total items
        """
        job = ExampleTestJob(mock_job_context)

        # Test progress with percentage
        progress = ProgressUpdate(percent=50)
        job._report_progress(progress)  # pyright: ignore[reportPrivateUsage]
        mock_job_context.event_bus.publish.assert_called()  # type: ignore[union-attr]

        # Test progress with current/total
        mock_job_context.event_bus.publish.reset_mock()  # type: ignore[union-attr]
        progress = ProgressUpdate(current=5, total=10, item="file.txt")
        job._report_progress(progress)  # pyright: ignore[reportPrivateUsage]
        mock_job_context.event_bus.publish.assert_called_once()  # type: ignore[union-attr]

        # Verify event structure
        call_args = mock_job_context.event_bus.publish.call_args  # type: ignore[union-attr]
        event = call_args[0][0]
        assert event.job == "example_test"
        assert event.update.current == 5
        assert event.update.total == 10


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
