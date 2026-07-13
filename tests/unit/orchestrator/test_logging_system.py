"""Unit tests for logging system.

Tests verify the logging infrastructure including log levels, filtering,
formatting, and integration with stdlib logging.

Note: The legacy Logger, FileLogger, and ConsoleLogger classes were removed
as part of the logging infrastructure migration (ADR-010). Tests for the new
stdlib-based logging are in tests/unit/test_logging.py and
tests/contract/test_logging_contract.py.

References:
- docs/system/core.md - User Story 4 (Logging System)
- specs/004-python-logging - Standard Python Logging Integration
- LOG-FR-LEVELS: Six log levels with correct ordering
- LOG-FR-FILE-PATH: Timestamped log files
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config import Configuration
from pcswitcher.logger import generate_log_filename
from pcswitcher.models import LogLevel, SyncAbortedByUser
from pcswitcher.orchestrator import Orchestrator


class TestLogLevelOrdering:
    """Test LOG-FR-LEVELS: Six log levels with correct ordering."""

    def test_log_fr_levels(self) -> None:
        """LOG-FR-LEVELS: Six log levels with correct ordering.

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
        _check_out_of_order patched to decline, so the fast SyncAbortedByUser
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
            pytest.raises(SyncAbortedByUser),
        ):
            await orchestrator.run()

        # console/ui/confirmer must exist by the time setup_logging is called.
        setup_logging_mock.assert_called_once()
        _args, kwargs = setup_logging_mock.call_args
        assert kwargs["ui"] is no_op_ui
        assert kwargs["console"] is orchestrator._console  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._console is not None  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._confirmer is not None  # pyright: ignore[reportPrivateUsage]
