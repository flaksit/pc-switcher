"""Unit tests for dummy jobs (FND-US-DUMMY).

Tests verify:
- FND-FR-DUMMY-JOBS: Two dummy jobs exist (dummy_success, dummy_fail)
- FND-FR-DUMMY-SIM: dummy_success behavior (20s, logs at levels, progress)
- FND-FR-DUMMY-EXCEPTION: dummy_fail raises exception at configurable time
- FND-FR-DUMMY-TERM: dummy jobs handle termination
- FND-FR-PROGRESS-EMIT: jobs emit progress updates
- FND-US-DUMMY-AS1: dummy_success completes with logs/progress
- FND-US-DUMMY-AS3: dummy_fail raises exception at configured time
- FND-US-DUMMY-AS4: dummy job handles termination request
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.events import ProgressEvent
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.dummy_fail import DummyFailJob
from pcswitcher.jobs.dummy_success import DummySuccessJob
from pcswitcher.models import CommandResult, LogLevel, ProgressUpdate


class JobContextFactory(Protocol):
    """Protocol for JobContext factory function."""

    def __call__(self, config: dict[str, Any] | None = None) -> JobContext: ...


def create_mock_process(num_lines: int, terminate_at: int | None = None) -> MagicMock:
    """Create a mock Process with streaming stdout.

    Args:
        num_lines: Number of lines to yield from stdout
        terminate_at: If set, stdout will stop after this many lines (simulating termination)
    """
    mock_proc = MagicMock()
    lines_to_yield = terminate_at if terminate_at is not None else num_lines

    async def mock_stdout() -> AsyncIterator[str]:
        for i in range(1, lines_to_yield + 1):
            yield f"tick {i}\n"

    mock_proc.stdout = mock_stdout
    mock_proc.wait = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    mock_proc.terminate = AsyncMock()
    return mock_proc


class TestDummyJobsExist:
    """Test FND-FR-DUMMY-JOBS: two dummy jobs exist."""

    def test_001_fnd_fr_dummy_jobs(self) -> None:
        """FND-FR-DUMMY-JOBS: System MUST include two dummy jobs: dummy_success, dummy_fail.

        Verifies that both dummy job classes exist and can be imported.
        """
        # Import should succeed (already done at module level)
        assert DummySuccessJob is not None
        assert DummyFailJob is not None

        # Verify job names are correctly set
        assert DummySuccessJob.name == "dummy_success"
        assert DummyFailJob.name == "dummy_fail"


class TestDummySuccessBehavior:
    """Test FND-FR-DUMMY-SIM, FND-US-DUMMY-AS1: dummy_success job behavior."""

    @pytest.mark.asyncio
    async def test_001_fnd_fr_dummy_sim(
        self, mock_job_context_factory: JobContextFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FND-FR-DUMMY-SIM: dummy_success simulates 20s operation with logs and progress.

        Tests with default 20s duration:
        - Logs INFO every 2s on source phase
        - Logs WARNING at 6s on source
        - Logs INFO every 2s on target phase (via remote execution)
        - Logs ERROR at 8s on target
        - Emits progress: 0%, 25%, 50%, 75%, 100%
        """
        context = mock_job_context_factory(config={"source_duration": 20, "target_duration": 20})
        job = DummySuccessJob(context)

        # Mock target.start_process to return mock process with streaming output
        # target_duration=20 means 10 iterations (20/2)
        mock_proc = create_mock_process(num_lines=10)
        context.target.start_process = AsyncMock(return_value=mock_proc)

        # Mock asyncio.sleep to avoid waiting on source phase
        with (
            caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            await job.execute()

        # Verify asyncio.sleep was called for source phase only (20s / 2s = 10 times)
        assert mock_sleep.call_count == 10  # Source phase only

        # Verify target.start_process was called
        context.target.start_process.assert_called_once()

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

        # Verify log records (using caplog for stdlib logging)
        # Check for source logs (INFO every 2s, WARNING at 6s)
        source_logs = [r for r in caplog.records if r.__dict__.get("host") == "source"]
        assert len(source_logs) >= 10  # At least 10 source logs

        source_info_logs = [r for r in source_logs if r.levelno == LogLevel.INFO]
        assert len(source_info_logs) >= 9  # INFO every 2s

        source_warning_logs = [r for r in source_logs if r.levelno == LogLevel.WARNING]
        assert len(source_warning_logs) >= 1  # WARNING at 6s
        assert any("6s" in r.message for r in source_warning_logs)

        # Check for target logs (INFO every 2s, ERROR at 8s) - now via remote execution
        target_logs = [r for r in caplog.records if r.__dict__.get("host") == "target"]
        assert len(target_logs) >= 10  # At least 10 target logs

        target_info_logs = [r for r in target_logs if r.levelno == LogLevel.INFO]
        assert len(target_info_logs) >= 9  # INFO every 2s

        target_error_logs = [r for r in target_logs if r.levelno == LogLevel.ERROR]
        assert len(target_error_logs) >= 1  # ERROR at 8s
        assert any("8s" in r.message for r in target_error_logs)

    @pytest.mark.asyncio
    async def test_001_fnd_us_dummy_as1(self, mock_job_context_factory: JobContextFactory) -> None:
        """FND-US-DUMMY-AS1: dummy_success performs operations and completes successfully.

        Given: dummy_success job is enabled
        When: sync runs
        Then: job performs 20s on source, 20s on target, logs at all levels,
              reports progress (0%, 25%, 50%, 75%, 100%), and completes successfully
        """
        context = mock_job_context_factory(config={"source_duration": 20, "target_duration": 20})
        job = DummySuccessJob(context)

        # Mock target.start_process for target phase
        mock_proc = create_mock_process(num_lines=10)
        context.target.start_process = AsyncMock(return_value=mock_proc)

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
    async def test_001_fnd_fr_dummy_sim_configurable_duration(
        self, mock_job_context_factory: JobContextFactory
    ) -> None:
        """FND-FR-DUMMY-SIM: dummy_success supports configurable duration.

        Tests with shorter duration to verify config is respected.
        """
        # Use 4s duration for faster test (2 iterations per phase)
        context = mock_job_context_factory(config={"source_duration": 4, "target_duration": 4})
        job = DummySuccessJob(context)

        # Mock target.start_process for target phase (4s/2 = 2 iterations)
        mock_proc = create_mock_process(num_lines=2)
        context.target.start_process = AsyncMock(return_value=mock_proc)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await job.execute()

        # Verify sleep was called for source phase only (4s / 2s = 2 times)
        assert mock_sleep.call_count == 2  # Source phase only


class TestDummyFailBehavior:
    """Test FND-FR-DUMMY-EXCEPTION, FND-US-DUMMY-AS3: dummy_fail job behavior."""

    @pytest.mark.asyncio
    async def test_001_fnd_fr_dummy_exception(
        self, mock_job_context_factory: JobContextFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FND-FR-DUMMY-EXCEPTION: dummy_fail raises unhandled exception at configured time.

        Verifies that dummy_fail simulates progress, then raises RuntimeError.
        With source_duration=10, target_duration=10, fail_at=12:
        - Source phase runs for 10s (0-50% progress)
        - Target phase starts, fails at 12s (2s into target = 60% progress)
        """
        # source=10s, target=10s, fail_at=12s (2s into target phase)
        context = mock_job_context_factory(config={"source_duration": 10, "target_duration": 10, "fail_at": 12})
        job = DummyFailJob(context)

        # Mock target.start_process for target phase
        # target_duration=10 means 5 iterations, but we fail at 12s (1st tick = 12s)
        mock_proc = create_mock_process(num_lines=5, terminate_at=1)
        context.target.start_process = AsyncMock(return_value=mock_proc)

        # Mock asyncio.sleep to avoid waiting
        with (
            caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(RuntimeError) as exc_info,
        ):
            await job.execute()

        # Verify exception message mentions failure at 12s
        assert "12s" in str(exc_info.value)

        # Verify progress was reported
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        progress_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], ProgressEvent)]
        progress_percents = [event.update.percent for event in progress_events]

        # With source=10, target=10, total=20:
        # - At 10s: 50% (end of source)
        # - At 12s: 60% (first tick of target, where we fail)
        assert 0 in progress_percents
        assert 50 in progress_percents  # 10s / 20s = 50%
        assert 60 in progress_percents  # 12s / 20s = 60%
        # Should not reach 100%
        assert 100 not in progress_percents

        # Verify CRITICAL log was emitted before exception (using caplog for stdlib logging)
        critical_logs = [r for r in caplog.records if r.levelno == LogLevel.CRITICAL]
        assert len(critical_logs) >= 1
        assert any("Simulated failure at 12s" in r.message for r in critical_logs)

    @pytest.mark.asyncio
    async def test_001_fnd_us_dummy_as3(self, mock_job_context_factory: JobContextFactory) -> None:
        """FND-US-DUMMY-AS3: dummy_fail raises exception at configured time to test error handling.

        Given: dummy_fail job is enabled with defaults (fail_at=12s)
        When: sync runs and job reaches 12s elapsed
        Then: job raises RuntimeError, orchestrator should catch it,
              log at CRITICAL, and halt sync
        """
        context = mock_job_context_factory(config={})  # Default fail_at=12s
        job = DummyFailJob(context)

        # Mock target.start_process for target phase
        mock_proc = create_mock_process(num_lines=5, terminate_at=1)
        context.target.start_process = AsyncMock(return_value=mock_proc)

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(RuntimeError) as exc_info:
            await job.execute()

        assert "Dummy job failed at 12s" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_001_fnd_fr_dummy_exception_configurable(self, mock_job_context_factory: JobContextFactory) -> None:
        """FND-FR-DUMMY-EXCEPTION: dummy_fail supports configurable fail_at time.

        Tests with different failure time to verify config is respected.
        With fail_at=6, failure occurs in source phase (before target phase).
        """
        # Fail at 6s (during source phase which runs for 10s by default)
        context = mock_job_context_factory(config={"source_duration": 10, "target_duration": 10, "fail_at": 6})
        job = DummyFailJob(context)

        # No need to mock target.start_process as failure happens in source phase
        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(RuntimeError) as exc_info:
            await job.execute()

        assert "6s" in str(exc_info.value)

        # Verify progress stopped around 30% (6s out of 20s total)
        publish_calls = context.event_bus.publish.call_args_list  # type: ignore[attr-defined]
        progress_events = [call[0][0] for call in publish_calls if isinstance(call[0][0], ProgressEvent)]
        progress_percents = [event.update.percent for event in progress_events]
        assert 30 in progress_percents
        # Should not reach 50% (end of source phase)
        assert 50 not in progress_percents


class TestDummyJobsTermination:
    """Test FND-FR-DUMMY-TERM, FND-US-DUMMY-AS4: dummy jobs handle termination."""

    @pytest.mark.asyncio
    async def test_001_fnd_fr_dummy_term(
        self, mock_job_context_factory: JobContextFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FND-FR-DUMMY-TERM: Dummy jobs handle termination requests.

        Verifies that when a dummy job receives CancelledError (termination request),
        it logs "Dummy job termination requested" and re-raises the error.
        """
        context = mock_job_context_factory(config={"source_duration": 20, "target_duration": 20})
        job = DummySuccessJob(context)

        # Mock target.start_process in case we reach target phase (shouldn't happen)
        mock_proc = create_mock_process(num_lines=10)
        context.target.start_process = AsyncMock(return_value=mock_proc)

        # Mock asyncio.sleep to raise CancelledError after first call
        call_count = 0

        async def sleep_then_cancel(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Cancel during source phase
                raise asyncio.CancelledError()

        with (
            caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"),
            patch("asyncio.sleep", side_effect=sleep_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await job.execute()

        # Verify termination log was emitted (using caplog for stdlib logging)
        termination_logs = [
            r for r in caplog.records if r.levelno == LogLevel.WARNING and "termination requested" in r.message.lower()
        ]
        assert len(termination_logs) >= 1

    @pytest.mark.asyncio
    async def test_001_fnd_us_dummy_as4(
        self, mock_job_context_factory: JobContextFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FND-US-DUMMY-AS4: Dummy job handles termination request gracefully.

        Given: any dummy job is running
        When: user presses Ctrl+C (termination request)
        Then: job receives CancelledError, logs "Dummy job termination requested",
              stops execution, and returns control to orchestrator
        """
        context = mock_job_context_factory(config={})
        job = DummyFailJob(context)

        # Simulate termination during source phase execution
        async def sleep_then_cancel(duration: float) -> None:
            raise asyncio.CancelledError()

        with (
            caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"),
            patch("asyncio.sleep", side_effect=sleep_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await job.execute()

        # Verify cancellation was caught and logged (using caplog for stdlib logging)
        cancel_logs = [r for r in caplog.records if r.levelno == LogLevel.WARNING and "cancelled" in r.message.lower()]
        assert len(cancel_logs) >= 1

    @pytest.mark.asyncio
    async def test_001_fnd_fr_dummy_term_cleanup(
        self, mock_job_context_factory: JobContextFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FND-FR-DUMMY-TERM: dummy_success cleans up on termination.

        Verifies that termination during source phase doesn't execute target phase.
        """
        context = mock_job_context_factory(config={"source_duration": 20, "target_duration": 20})
        job = DummySuccessJob(context)

        # Mock target.start_process in case we reach target phase (shouldn't happen)
        mock_proc = create_mock_process(num_lines=10)
        context.target.start_process = AsyncMock(return_value=mock_proc)

        # Cancel after 1 source iteration
        call_count = 0

        async def sleep_then_cancel(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise asyncio.CancelledError()

        with (
            caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"),
            patch("asyncio.sleep", side_effect=sleep_then_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await job.execute()

        # Verify only source logs exist, no target logs (using caplog for stdlib logging)
        target_logs = [r for r in caplog.records if r.__dict__.get("host") == "target"]
        # Should have no target logs since we cancelled during source phase
        assert len(target_logs) == 0

        # Verify target.start_process was NOT called since we cancelled before target phase
        context.target.start_process.assert_not_called()


class TestJobProgressEmission:
    """Test FND-FR-PROGRESS-EMIT: jobs emit progress updates."""

    @pytest.mark.asyncio
    async def test_001_fnd_fr_progress_emit(self, mock_job_context_factory: JobContextFactory) -> None:
        """FND-FR-PROGRESS-EMIT: Jobs can emit progress updates with percentage.

        Verifies that dummy jobs emit ProgressUpdate objects with valid percentages
        through the event bus.
        """
        context = mock_job_context_factory(config={"source_duration": 4, "target_duration": 4})
        job = DummySuccessJob(context)

        # Mock target.start_process for target phase (4s/2 = 2 iterations)
        mock_proc = create_mock_process(num_lines=2)
        context.target.start_process = AsyncMock(return_value=mock_proc)

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
    async def test_001_fnd_fr_progress_emit_validation(self, mock_job_context_factory: JobContextFactory) -> None:
        """FND-FR-PROGRESS-EMIT: ProgressUpdate validates percent range.

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
        """DummyFailJob CONFIG_SCHEMA includes source_duration, target_duration, and fail_at."""
        schema = DummyFailJob.CONFIG_SCHEMA
        assert "properties" in schema
        assert "source_duration" in schema["properties"]
        assert "target_duration" in schema["properties"]
        assert "fail_at" in schema["properties"]

        # Verify defaults
        assert schema["properties"]["source_duration"]["default"] == 10
        assert schema["properties"]["target_duration"]["default"] == 10
        assert schema["properties"]["fail_at"]["default"] == 12

        # Verify minimums
        assert schema["properties"]["source_duration"]["minimum"] == 2
        assert schema["properties"]["target_duration"]["minimum"] == 2
        assert schema["properties"]["fail_at"]["minimum"] == 2

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
        valid_config = {"source_duration": 10, "target_duration": 10, "fail_at": 12}
        errors = DummyFailJob.validate_config(valid_config)
        assert errors == []

        # Invalid: duration less than minimum
        invalid_config = {"source_duration": 1, "target_duration": 10, "fail_at": 12}
        errors = DummyFailJob.validate_config(invalid_config)
        assert len(errors) > 0
