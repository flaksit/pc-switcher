"""Unit tests for logging system.

Tests verify the logging infrastructure including log levels, filtering,
formatting, and integration with stdlib logging.

Note: The legacy Logger, FileLogger, and ConsoleLogger classes were removed
as part of the logging infrastructure migration (ADR-010). Tests for the new
stdlib-based logging are in tests/unit/test_logging.py and
tests/contract/test_logging_contract.py.

References:
- docs/system/logging.md - LOG-US-SYSTEM (Comprehensive Logging System)
- specs/004-python-logging - Standard Python Logging Integration
- LOG-FR-HIERARCHY: Six log levels with correct ordering
- LOG-FR-FILE-PATH: Timestamped log files
- LOG-FR-TUI-FORMAT: Interactive records go to the UI's log panel
- LOG-FR-CONTEXT: Structured context (job, host) preserved on each record
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.install_on_target import INSTALL_ON_TARGET_SKIP_ENV
from pcswitcher.logger import generate_log_filename
from pcswitcher.models import JobSkipped, LogLevel, SyncAborted, ValidationError
from pcswitcher.orchestrator import Orchestrator
from tests.unit.jobs.test_package_sync_core import make_context


class TestLogLevelOrdering:
    """Test LOG-FR-HIERARCHY: Six log levels with correct ordering."""

    def test_log_fr_hierarchy(self) -> None:
        """LOG-FR-HIERARCHY: Six log levels with correct ordering.

        Verifies that LogLevel enum has exactly six levels with the ordering:
        DEBUG > FULL > INFO > WARNING > ERROR > CRITICAL
        (where DEBUG is most verbose, CRITICAL is least verbose)
        """
        # Verify all six levels exist
        assert hasattr(LogLevel, "DEBUG")
        assert hasattr(LogLevel, "FULL")
        assert hasattr(LogLevel, "INFO")
        assert hasattr(LogLevel, "WARNING")
        assert hasattr(LogLevel, "ERROR")
        assert hasattr(LogLevel, "CRITICAL")

        # Verify ordering (lower numeric value = more verbose)
        assert LogLevel.DEBUG < LogLevel.FULL
        assert LogLevel.FULL < LogLevel.INFO
        assert LogLevel.INFO < LogLevel.WARNING
        assert LogLevel.WARNING < LogLevel.ERROR
        assert LogLevel.ERROR < LogLevel.CRITICAL

        # Verify exact numeric values match stdlib logging levels
        # (10-50 range aligns with Python's logging module)
        assert LogLevel.DEBUG == 10
        assert LogLevel.FULL == 15  # Custom level between DEBUG and INFO
        assert LogLevel.INFO == 20
        assert LogLevel.WARNING == 30
        assert LogLevel.ERROR == 40
        assert LogLevel.CRITICAL == 50


class TestFileLoggerTimestampedFile:
    """Test LOG-FR-FILE-PATH: Logs written to timestamped file."""

    def test_log_fr_file_path(self) -> None:
        """LOG-FR-FILE-PATH: Log filename includes timestamp and session ID.

        Verifies generate_log_filename() creates filenames with format:
        sync-<timestamp>-<session_id>.log
        """
        session_id = "abc12345"
        filename = generate_log_filename(session_id)

        # Verify filename format
        assert filename.startswith("sync-")
        assert filename.endswith(f"-{session_id}.log")

        # Verify timestamp format (YYYYMMDDTHHMMSS)
        parts = filename.split("-")
        assert len(parts) == 3  # sync, timestamp, session_id.log
        timestamp_part = parts[1]
        assert len(timestamp_part) == 15  # YYYYMMDDThhmmss

        # Verify it can be parsed as datetime
        datetime.strptime(timestamp_part, "%Y%m%dT%H%M%S")


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock Configuration for orchestrator initialization."""
    config = MagicMock(spec=Configuration)
    config.logging = MagicMock()
    config.logging.file = 10  # DEBUG
    config.logging.tui = 20  # INFO
    config.logging.external = 30  # WARNING
    config.sync_jobs = {}
    config.job_configs = {}
    config.btrfs_snapshots = MagicMock()
    config.btrfs_snapshots.subvolumes = ["@", "@home"]
    config.disk = MagicMock()
    config.disk.preflight_minimum = "10%"
    return config


def _make_no_op_ui() -> MagicMock:
    """A TerminalUI stand-in: sync methods no-op, consume_events is awaitable."""
    ui = MagicMock()
    ui.consume_events = AsyncMock()
    return ui


class TestOrchestratorCreatesUiBeforeLogging:
    """run() creates console/UI/confirmer before calling setup_logging (gap closure 01-18).

    Proves the fix for the live-progress flooding root cause: setup_logging
    must receive the UI/console so it can route the TUI-floor handler through
    the UI's Recent Logs panel instead of a raw stderr write that fights with
    rich.live.Live for the same terminal region (see
    .planning/debug/tui-live-progress-flooding.md).
    """

    @pytest.mark.asyncio
    async def test_setup_logging_receives_ui_and_console(
        self,
        mock_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """setup_logging is called with the orchestrator's own ui/console, not None.

        Drives the real run() with lock/connection phases stubbed and
        _check_out_of_order patched to decline, so the fast SyncAborted
        path is reached right after the UI-before-logging wiring runs,
        without needing SSH, snapshots, or jobs.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="target-host", config=mock_config)
        orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._remote_executor = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._acquire_source_lock = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._establish_connection = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._acquire_target_lock = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._check_out_of_order = AsyncMock(return_value=False)  # pyright: ignore[reportPrivateUsage]

        setup_logging_mock = MagicMock(return_value=(MagicMock(), MagicMock()))
        no_op_ui = _make_no_op_ui()

        with (
            patch("pcswitcher.orchestrator.setup_logging", setup_logging_mock),
            patch("pcswitcher.orchestrator.TerminalUI", return_value=no_op_ui),
            pytest.raises(SyncAborted),
        ):
            await orchestrator.run()

        # console/ui/confirmer must exist by the time setup_logging is called.
        setup_logging_mock.assert_called_once()
        _args, kwargs = setup_logging_mock.call_args
        assert kwargs["ui"] is no_op_ui
        assert kwargs["console"] is orchestrator._console  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._console is not None  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._confirmer is not None  # pyright: ignore[reportPrivateUsage]


class _StartLogRecordingJob(SyncJob):
    """Captures the orchestrator's logging calls made before its own execute() ran."""

    name: ClassVar[str] = "stub_start_log"

    def __init__(self, context: JobContext, logger: MagicMock) -> None:
        super().__init__(context)
        self._logger = logger
        self.calls_before_execute: list[Any] = []

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        self.calls_before_execute = list(self._logger.log.call_args_list)


class TestJobStartIsLogged:
    """#243 — every job announces its start, so the log shows where a job's output begins
    instead of only where it ended."""

    @pytest.mark.asyncio
    async def test_the_start_is_logged_before_the_job_runs(self, wired_orchestrator: Orchestrator) -> None:
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]
        job = _StartLogRecordingJob(make_context(), logger)

        await wired_orchestrator._execute_jobs([job])  # pyright: ignore[reportPrivateUsage]

        assert [call.args[1:] for call in job.calls_before_execute] == [("Job %s started", "stub_start_log")]

    @pytest.mark.asyncio
    async def test_the_start_line_is_tagged_with_the_job_and_no_side(self, wired_orchestrator: Orchestrator) -> None:
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]
        job = _StartLogRecordingJob(make_context(), logger)

        await wired_orchestrator._execute_jobs([job])  # pyright: ignore[reportPrivateUsage]

        assert logger.log.call_args_list[0].kwargs["extra"] == {"job": "stub_start_log"}


class _EndingJob(SyncJob):
    """A job that ends the way the test asks it to, to pin the tag on its closing line."""

    name: ClassVar[str] = "stub_ending"
    display_name: ClassVar[str] = "Ending job"

    def __init__(self, context: JobContext, ending: Exception | None = None) -> None:
        super().__init__(context)
        self._ending = ending

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        if self._ending is not None:
            raise self._ending


def _tag_of(calls: list[Any], needle: str) -> dict[str, str]:
    """The `extra` of the one logged call whose format string contains `needle`.

    The template is the first argument of a level-named call and the second of `Logger.log`,
    which takes the level first.
    """
    matching = [call for call in calls if needle in (call.args[0] if isinstance(call.args[0], str) else call.args[1])]
    assert len(matching) == 1
    return matching[0].kwargs["extra"]


class TestJobLifecycleLinesAreTaggedWithTheJob:
    """#289 — the `job` field says which job a record is ABOUT, so scanning the Recent Logs
    column for one job must give its end as well as its start and its work. The orchestrator
    being the code that emits the closing line is an implementation detail.
    """

    @pytest.mark.asyncio
    async def test_the_completed_line_carries_the_job(self, wired_orchestrator: Orchestrator) -> None:
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]

        await wired_orchestrator._execute_jobs([_EndingJob(make_context())])  # pyright: ignore[reportPrivateUsage]

        assert _tag_of(logger.log.call_args_list, "completed successfully") == {"job": "stub_ending"}

    @pytest.mark.asyncio
    async def test_the_skipped_line_carries_the_job(self, wired_orchestrator: Orchestrator) -> None:
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]
        job = _EndingJob(make_context(), JobSkipped("stub_ending", "nothing to do"))

        await wired_orchestrator._execute_jobs([job])  # pyright: ignore[reportPrivateUsage]

        assert _tag_of(logger.warning.call_args_list, "skipped") == {"job": "stub_ending"}

    @pytest.mark.asyncio
    async def test_the_failed_line_carries_the_job(self, wired_orchestrator: Orchestrator) -> None:
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]
        job = _EndingJob(make_context(), RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await wired_orchestrator._execute_jobs([job])  # pyright: ignore[reportPrivateUsage]

        assert _tag_of(logger.critical.call_args_list, "failed") == {"job": "stub_ending"}

    @pytest.mark.asyncio
    async def test_the_install_on_target_skip_carries_the_job(
        self, wired_orchestrator: Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The step outside the job loop is tagged the same way."""
        monkeypatch.setenv(INSTALL_ON_TARGET_SKIP_ENV, "1")
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]

        await wired_orchestrator._install_on_target_job()  # pyright: ignore[reportPrivateUsage]

        assert _tag_of(logger.warning.call_args_list, "skipped") == {"job": "install_on_target"}


class TestTheRoutineLifecyclePairIsFull:
    """#289 — the bar the loop opens says a job started, how far it is and that it finished,
    and the outcome block says how it ended, so the pair repeating it at INFO cost two lines
    of a ten-line panel per job. It drops to FULL, which is in the log file and under the
    TUI's floor. A skip or a failure is not routine and keeps its level.
    """

    @pytest.mark.asyncio
    async def test_both_ends_of_a_successful_job_are_full(self, wired_orchestrator: Orchestrator) -> None:
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]

        await wired_orchestrator._execute_jobs([_EndingJob(make_context())])  # pyright: ignore[reportPrivateUsage]

        assert [(call.args[0], call.args[1]) for call in logger.log.call_args_list] == [
            (LogLevel.FULL, "Job %s started"),
            (LogLevel.FULL, "Job %s completed successfully"),
        ]
        assert not [call for call in logger.info.call_args_list if "Job %s" in call.args[0]]

    @pytest.mark.asyncio
    async def test_a_skip_keeps_its_warning(self, wired_orchestrator: Orchestrator) -> None:
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]
        job = _EndingJob(make_context(), JobSkipped("stub_ending", "nothing to do"))

        await wired_orchestrator._execute_jobs([job])  # pyright: ignore[reportPrivateUsage]

        assert [call.args[0] for call in logger.warning.call_args_list] == ["Job %s skipped: %s"]

    @pytest.mark.asyncio
    async def test_a_failure_keeps_its_critical(self, wired_orchestrator: Orchestrator) -> None:
        logger = cast(MagicMock, wired_orchestrator._logger)  # pyright: ignore[reportPrivateUsage]
        job = _EndingJob(make_context(), RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await wired_orchestrator._execute_jobs([job])  # pyright: ignore[reportPrivateUsage]

        assert [call.args[0] for call in logger.critical.call_args_list] == ["Job %s failed: %s"]
