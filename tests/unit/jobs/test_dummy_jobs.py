"""Unit tests for dummy jobs (US-8).

Tests verify:
- FR-038: Two dummy jobs exist (dummy_success, dummy_fail)
- FR-039: dummy_success behavior (20s, logs at levels, progress)
- FR-041: dummy_fail raises exception at 60%
- FR-042: dummy jobs handle termination
- FR-043: jobs emit progress updates
- US8-AS1: dummy_success completes with logs/progress
- US8-AS3: dummy_fail raises exception at 60%
- US8-AS4: dummy job handles termination request
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.events import LogEvent, ProgressEvent
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.dummy_fail import DummyFailJob
from pcswitcher.jobs.dummy_success import DummySuccessJob
from pcswitcher.models import Host, LogLevel, ProgressUpdate


class JobContextFactory(Protocol):
    """Protocol for JobContext factory function."""

    def __call__(self, config: dict[str, Any] | None = None) -> JobContext: ...


@pytest.fixture
def mock_job_context_factory(
    mock_local_executor: MagicMock,
    mock_remote_executor: MagicMock,
    mock_event_bus: MagicMock,
) -> JobContextFactory:
    """Factory fixture to create JobContext with custom config."""

    def create_context(config: dict[str, Any] | None = None) -> JobContext:
        return JobContext(
            config=config or {},
            source=mock_local_executor,
            target=mock_remote_executor,
            event_bus=mock_event_bus,
            session_id="test-session-12345678",
            source_hostname="source-host",
            target_hostname="target-host",
        )

    return create_context


class TestDummyJobsExist:
    """Test FR-038: two dummy jobs exist."""

    def test_001_fr038_dummy_jobs_exist(self) -> None:
        """FR-038: System MUST include two dummy jobs: dummy_success, dummy_fail.

        Verifies that both dummy job classes exist and can be imported.
        """
        # Import should succeed (already done at module level)
        assert DummySuccessJob is not None
        assert DummyFailJob is not None

        # Verify job names are correctly set
        assert DummySuccessJob.name == "dummy_success"
        assert DummyFailJob.name == "dummy_fail"


class TestDummySuccessBehavior:
    """Test FR-039, US8-AS1: dummy_success job behavior."""

    @pytest.mark.asyncio
    async def test_001_fr039_dummy_success_behavior(self, mock_job_context_factory: JobContextFactory) -> None:
        """FR-039: dummy_success simulates 20s operation with logs and progress.

        Tests with default 20s duration:
        - Logs INFO every 2s on source phase
        - Logs WARNING at 6s on source
        - Logs INFO every 2s on target phase
        - Logs ERROR at 8s on target
        - Emits progress: 0%, 25%, 50%, 75%, 100%
        """
        context = mock_job_context_factory(config={"source_duration": 20, "target_duration": 20})
        job = DummySuccessJob(context)

        # Mock asyncio.sleep to avoid waiting 20s
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await job.execute()

        # Verify asyncio.sleep was called multiple times (20s / 2s = 10 times per phase)
        assert mock_sleep.call_count == 20  # 10 source + 10 target

        # Verify progress updates: 0%, 25%, 50%, 75%, 100%
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        progress_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], ProgressEvent)]

        assert len(progress_events) >= 5
        progress_percents = [event.update.percent for event in progress_events]
        assert 0 in progress_percents
        assert 25 in progress_percents
        assert 50 in progress_percents
        assert 75 in progress_percents
        assert 100 in progress_percents

        # Verify log events
        log_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], LogEvent)]

        # Check for source logs (INFO every 2s, WARNING at 6s)
        source_logs = [e for e in log_events if e.host == Host.SOURCE]
        assert len(source_logs) >= 10  # At least 10 source logs

        source_info_logs = [e for e in source_logs if e.level == LogLevel.INFO]
        assert len(source_info_logs) >= 9  # INFO every 2s

        source_warning_logs = [e for e in source_logs if e.level == LogLevel.WARNING]
        assert len(source_warning_logs) >= 1  # WARNING at 6s
        assert any("6s" in e.message for e in source_warning_logs)

        # Check for target logs (INFO every 2s, ERROR at 8s)
        target_logs = [e for e in log_events if e.host == Host.TARGET]
        assert len(target_logs) >= 10  # At least 10 target logs

        target_info_logs = [e for e in target_logs if e.level == LogLevel.INFO]
        assert len(target_info_logs) >= 9  # INFO every 2s

        target_error_logs = [e for e in target_logs if e.level == LogLevel.ERROR]
        assert len(target_error_logs) >= 1  # ERROR at 8s
        assert any("8s" in e.message for e in target_error_logs)

    @pytest.mark.asyncio
    async def test_001_us8_as1_dummy_success_completes(self, mock_job_context_factory: JobContextFactory) -> None:
        """US8-AS1: dummy_success performs operations and completes successfully.

        Given: dummy_success job is enabled
        When: sync runs
        Then: job performs 20s on source, 20s on target, logs at all levels,
              reports progress (0%, 25%, 50%, 75%, 100%), and completes successfully
        """
        context = mock_job_context_factory(config={"source_duration": 20, "target_duration": 20})
        job = DummySuccessJob(context)

        # Mock asyncio.sleep to avoid waiting
        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Should complete without raising exception
            await job.execute()

        # Verify completion by checking 100% progress was emitted
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        progress_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], ProgressEvent)]
        progress_percents = [event.update.percent for event in progress_events]
        assert 100 in progress_percents

    @pytest.mark.asyncio
    async def test_001_fr039_dummy_success_configurable_duration(
        self, mock_job_context_factory: JobContextFactory
    ) -> None:
        """FR-039: dummy_success supports configurable duration.

        Tests with shorter duration to verify config is respected.
        """
        # Use 4s duration for faster test (2 iterations per phase)
        context = mock_job_context_factory(config={"source_duration": 4, "target_duration": 4})
        job = DummySuccessJob(context)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await job.execute()

        # Verify sleep was called fewer times (4s / 2s = 2 times per phase)
        assert mock_sleep.call_count == 4  # 2 source + 2 target


class TestDummyFailBehavior:
    """Test FR-041, US8-AS3: dummy_fail job behavior."""

    @pytest.mark.asyncio
    async def test_001_fr041_dummy_fail_exception(self, mock_job_context_factory: JobContextFactory) -> None:
        """FR-041: dummy_fail raises unhandled exception at 60% progress.

        Verifies that dummy_fail simulates progress to 60%, then raises RuntimeError.
        """
        context = mock_job_context_factory(config={"fail_at_percent": 60})
        job = DummyFailJob(context)

        # Mock asyncio.sleep to avoid waiting
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(RuntimeError) as exc_info:
            await job.execute()

        # Verify exception message mentions failure at 60%
        assert "60%" in str(exc_info.value)

        # Verify progress was reported up to 60%
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        progress_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], ProgressEvent)]
        progress_percents = [event.update.percent for event in progress_events]

        # Should have progress 0%, 10%, 20%, ..., 60%
        assert 0 in progress_percents
        assert 60 in progress_percents
        # Should not reach 100%
        assert 100 not in progress_percents

        # Verify CRITICAL log was emitted before exception
        log_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], LogEvent)]
        critical_logs = [e for e in log_events if e.level == LogLevel.CRITICAL]
        assert len(critical_logs) >= 1
        assert any("Simulated failure at 60%" in e.message for e in critical_logs)

    @pytest.mark.asyncio
    async def test_001_us8_as3_dummy_fail_raises_exception(self, mock_job_context_factory: JobContextFactory) -> None:
        """US8-AS3: dummy_fail raises exception at 60% to test error handling.

        Given: dummy_fail job is enabled
        When: sync runs and job reaches 60% progress
        Then: job raises RuntimeError, orchestrator should catch it,
              log at CRITICAL, and halt sync
        """
        context = mock_job_context_factory(config={})  # Default fail_at_percent=60
        job = DummyFailJob(context)

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(RuntimeError) as exc_info:
            await job.execute()

        assert "Dummy job failed at 60%" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_001_fr041_dummy_fail_configurable_percent(
        self, mock_job_context_factory: JobContextFactory
    ) -> None:
        """FR-041: dummy_fail supports configurable fail_at_percent.

        Tests with different failure percentage to verify config is respected.
        """
        # Fail at 30% instead of default 60%
        context = mock_job_context_factory(config={"fail_at_percent": 30})
        job = DummyFailJob(context)

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(RuntimeError) as exc_info:
            await job.execute()

        assert "30%" in str(exc_info.value)

        # Verify progress stopped at 30%
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        progress_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], ProgressEvent)]
        progress_percents = [event.update.percent for event in progress_events]
        assert 30 in progress_percents
        assert 40 not in progress_percents  # Should fail before 40%


class TestDummyJobsTermination:
    """Test FR-042, US8-AS4: dummy jobs handle termination."""

    @pytest.mark.asyncio
    async def test_001_fr042_dummy_jobs_termination(self, mock_job_context_factory: JobContextFactory) -> None:
        """FR-042: Dummy jobs handle termination requests.

        Verifies that when a dummy job receives CancelledError (termination request),
        it logs "Dummy job termination requested" and re-raises the error.
        """
        context = mock_job_context_factory(config={"source_duration": 20})
        job = DummySuccessJob(context)

        # Mock asyncio.sleep to raise CancelledError after first call
        call_count = 0

        async def sleep_then_cancel(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Cancel during execution
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=sleep_then_cancel), pytest.raises(asyncio.CancelledError):
            await job.execute()

        # Verify termination log was emitted
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        log_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], LogEvent)]
        termination_logs = [
            e for e in log_events if e.level == LogLevel.WARNING and "termination requested" in e.message.lower()
        ]
        assert len(termination_logs) >= 1

    @pytest.mark.asyncio
    async def test_001_us8_as4_dummy_job_termination(self, mock_job_context_factory: JobContextFactory) -> None:
        """US8-AS4: Dummy job handles termination request gracefully.

        Given: any dummy job is running
        When: user presses Ctrl+C (termination request)
        Then: job receives CancelledError, logs "Dummy job termination requested",
              stops execution, and returns control to orchestrator
        """
        context = mock_job_context_factory(config={})
        job = DummyFailJob(context)

        # Simulate termination during execution
        async def sleep_then_cancel(duration: float) -> None:
            raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=sleep_then_cancel), pytest.raises(asyncio.CancelledError):
            await job.execute()

        # Verify cancellation was caught and logged
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        log_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], LogEvent)]
        cancel_logs = [e for e in log_events if e.level == LogLevel.WARNING and "cancelled" in e.message.lower()]
        assert len(cancel_logs) >= 1

    @pytest.mark.asyncio
    async def test_001_fr042_dummy_success_termination_cleanup(
        self, mock_job_context_factory: JobContextFactory
    ) -> None:
        """FR-042: dummy_success cleans up on termination.

        Verifies that termination during source phase doesn't execute target phase.
        """
        context = mock_job_context_factory(config={"source_duration": 20, "target_duration": 20})
        job = DummySuccessJob(context)

        # Cancel after 1 source iteration
        call_count = 0

        async def sleep_then_cancel(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise asyncio.CancelledError()

        with patch("asyncio.sleep", side_effect=sleep_then_cancel), pytest.raises(asyncio.CancelledError):
            await job.execute()

        # Verify only source logs exist, no target logs
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        log_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], LogEvent)]
        target_logs = [e for e in log_events if e.host == Host.TARGET]
        # Should have no target logs since we cancelled during source phase
        assert len(target_logs) == 0


class TestJobProgressEmission:
    """Test FR-043: jobs emit progress updates."""

    @pytest.mark.asyncio
    async def test_001_fr043_job_progress_emission(self, mock_job_context_factory: JobContextFactory) -> None:
        """FR-043: Jobs can emit progress updates with percentage.

        Verifies that dummy jobs emit ProgressUpdate objects with valid percentages
        through the event bus.
        """
        context = mock_job_context_factory(config={"source_duration": 4})
        job = DummySuccessJob(context)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await job.execute()

        # Extract all progress events
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        progress_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], ProgressEvent)]

        # Verify progress events were emitted
        assert len(progress_events) >= 3  # At minimum: 0%, 50%, 100%

        # Verify all progress events have valid percentages
        for event in progress_events:
            assert isinstance(event.update, ProgressUpdate)
            assert event.update.percent is not None
            assert 0 <= event.update.percent <= 100

        # Verify job name is included in progress events
        for event in progress_events:
            assert event.job == "dummy_success"

    @pytest.mark.asyncio
    async def test_001_fr043_progress_update_validation(self, mock_job_context_factory: JobContextFactory) -> None:
        """FR-043: ProgressUpdate validates percent range.

        Verifies that ProgressUpdate enforces 0-100 range for percent.
        """
        # Valid percentages
        valid_update = ProgressUpdate(percent=50)
        assert valid_update.percent == 50

        # Invalid percentage should raise ValueError
        with pytest.raises(ValueError, match="percent must be 0-100"):
            ProgressUpdate(percent=150)

        with pytest.raises(ValueError, match="percent must be 0-100"):
            ProgressUpdate(percent=-10)


class TestDummyJobsValidation:
    """Test dummy jobs validation behavior."""

    @pytest.mark.asyncio
    async def test_001_dummy_success_no_validation_errors(self, mock_job_context_factory: JobContextFactory) -> None:
        """Dummy jobs have no prerequisites, validation returns empty list."""
        context = mock_job_context_factory(config={})
        job = DummySuccessJob(context)

        errors = await job.validate()
        assert errors == []

    @pytest.mark.asyncio
    async def test_001_dummy_fail_no_validation_errors(self, mock_job_context_factory: JobContextFactory) -> None:
        """Dummy jobs have no prerequisites, validation returns empty list."""
        context = mock_job_context_factory(config={})
        job = DummyFailJob(context)

        errors = await job.validate()
        assert errors == []


class TestDummyJobsConfigSchema:
    """Test dummy jobs config schema validation."""

    def test_001_dummy_success_config_schema(self) -> None:
        """DummySuccessJob CONFIG_SCHEMA includes source_duration and target_duration."""
        schema = DummySuccessJob.CONFIG_SCHEMA
        assert "properties" in schema
        assert "source_duration" in schema["properties"]
        assert "target_duration" in schema["properties"]

        # Verify defaults
        assert schema["properties"]["source_duration"]["default"] == 20
        assert schema["properties"]["target_duration"]["default"] == 20

        # Verify minimums
        assert schema["properties"]["source_duration"]["minimum"] == 1
        assert schema["properties"]["target_duration"]["minimum"] == 1

    def test_001_dummy_fail_config_schema(self) -> None:
        """DummyFailJob CONFIG_SCHEMA includes fail_at_percent."""
        schema = DummyFailJob.CONFIG_SCHEMA
        assert "properties" in schema
        assert "fail_at_percent" in schema["properties"]

        # Verify default
        assert schema["properties"]["fail_at_percent"]["default"] == 60

        # Verify range
        assert schema["properties"]["fail_at_percent"]["minimum"] == 0
        assert schema["properties"]["fail_at_percent"]["maximum"] == 100

    def test_001_dummy_success_config_validation(self) -> None:
        """DummySuccessJob validates config correctly."""
        # Valid config
        valid_config = {"source_duration": 10, "target_duration": 15}
        errors = DummySuccessJob.validate_config(valid_config)
        assert errors == []

        # Invalid: duration less than minimum
        invalid_config = {"source_duration": 0, "target_duration": 10}
        errors = DummySuccessJob.validate_config(invalid_config)
        assert len(errors) > 0

    def test_001_dummy_fail_config_validation(self) -> None:
        """DummyFailJob validates config correctly."""
        # Valid config
        valid_config = {"fail_at_percent": 50}
        errors = DummyFailJob.validate_config(valid_config)
        assert errors == []

        # Invalid: percent out of range
        invalid_config = {"fail_at_percent": 150}
        errors = DummyFailJob.validate_config(invalid_config)
        assert len(errors) > 0
