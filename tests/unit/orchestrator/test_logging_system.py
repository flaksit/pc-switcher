"""Unit tests for logging system.

Tests verify the logging infrastructure including log levels, filtering,
formatting, and integration with stdlib logging.

Note: The legacy Logger, FileLogger, and ConsoleLogger classes were removed
as part of the logging infrastructure migration (ADR-010). Tests for the new
stdlib-based logging are in tests/unit/test_logging.py and
tests/contract/test_logging_contract.py.

References:
- specs/001-foundation/spec.md - User Story 4 (Logging System)
- specs/004-python-logging - Standard Python Logging Integration
- FR-018: Six log levels with correct ordering
- FR-021: Timestamped log files
"""

from __future__ import annotations

from datetime import datetime

from pcswitcher.logger import generate_log_filename
from pcswitcher.models import LogLevel


class TestLogLevelOrdering:
    """Test FR-018: Six log levels with correct ordering."""

    def test_001_fr018_log_level_ordering(self) -> None:
        """FR-018: Six log levels with correct ordering.

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
    """Test FR-021: Logs written to timestamped file."""

    def test_001_fr021_timestamped_log_file(self) -> None:
        """FR-021: Log filename includes timestamp and session ID.

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
