"""Unit tests for logging system.

Tests verify the logging infrastructure including log levels, filtering,
formatting, and integration with the event bus system.

References:
- specs/001-foundation/spec.md - User Story 4 (Logging System)
- FR-018: Six log levels with correct ordering
- FR-020: Independent file and CLI log levels
- FR-021: Timestamped log files
- FR-022: JSON Lines for file, console for terminal
- FR-045: Progress logged at FULL level
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from pcswitcher.events import EventBus, LogEvent, ProgressEvent
from pcswitcher.logger import ConsoleLogger, FileLogger, Logger, generate_log_filename
from pcswitcher.models import Host, LogLevel, ProgressUpdate


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

        # Verify exact numeric values match spec
        assert LogLevel.DEBUG == 0
        assert LogLevel.FULL == 1
        assert LogLevel.INFO == 2
        assert LogLevel.WARNING == 3
        assert LogLevel.ERROR == 4
        assert LogLevel.CRITICAL == 5


class TestLoggerEventPublishing:
    """Test Logger publishes events to EventBus."""

    def test_logger_publishes_log_event(self) -> None:
        """Logger.log() should publish LogEvent to EventBus."""
        event_bus = MagicMock(spec=EventBus)
        logger = Logger(event_bus, job_name="test-job")

        logger.log(
            level=LogLevel.INFO,
            host=Host.SOURCE,
            message="Test message",
            extra_field="value",
        )

        # Verify publish was called once
        event_bus.publish.assert_called_once()

        # Verify the published event
        published_event = event_bus.publish.call_args[0][0]
        assert isinstance(published_event, LogEvent)
        assert published_event.level == LogLevel.INFO
        assert published_event.job == "test-job"
        assert published_event.host == Host.SOURCE
        assert published_event.message == "Test message"
        assert published_event.context["extra_field"] == "value"


class TestFileLoggerFiltering:
    """Test FR-020: Independent file log level filtering."""

    @pytest.mark.asyncio
    async def test_001_fr020_independent_log_levels(self, tmp_path: Path) -> None:
        """FR-020: File logger filters logs independently of CLI logger.

        Verifies that FileLogger respects its configured log level and only
        writes events at that level or above (more severe).
        """
        log_file = tmp_path / "test.log"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        hostname_map = {Host.SOURCE: "source-host", Host.TARGET: "target-host"}

        # Create FileLogger at INFO level
        file_logger = FileLogger(
            log_file=log_file,
            level=LogLevel.INFO,
            queue=queue,
            hostname_map=hostname_map,
        )

        # Queue events at different levels
        await queue.put(
            LogEvent(
                level=LogLevel.DEBUG,
                job="test",
                host=Host.SOURCE,
                message="Debug message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.FULL,
                job="test",
                host=Host.SOURCE,
                message="Full message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.INFO,
                job="test",
                host=Host.SOURCE,
                message="Info message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.WARNING,
                job="test",
                host=Host.SOURCE,
                message="Warning message",
            )
        )
        await queue.put(None)  # Shutdown sentinel

        # Consume events
        await file_logger.consume()

        # Read log file
        log_content = log_file.read_text()
        log_lines = [line for line in log_content.strip().split("\n") if line]

        # Should have 2 entries (INFO and WARNING, not DEBUG or FULL)
        assert len(log_lines) == 2

        # Parse JSON lines
        entries = [json.loads(line) for line in log_lines]

        # Verify only INFO and above were logged
        assert entries[0]["level"] == "INFO"
        assert entries[0]["event"] == "Info message"
        assert entries[1]["level"] == "WARNING"
        assert entries[1]["event"] == "Warning message"


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


class TestFileLoggerJSONFormat:
    """Test FR-022 and US4-AS4: JSON Lines format for file output."""

    @pytest.mark.asyncio
    async def test_001_fr022_log_format_json_and_console(self, tmp_path: Path) -> None:
        """FR-022: Log file uses JSON Lines format.

        Verifies that FileLogger writes logs in JSON Lines format with
        required fields: timestamp, level, job, host, hostname, event.
        """
        log_file = tmp_path / "test.log"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        hostname_map = {Host.SOURCE: "source-host", Host.TARGET: "target-host"}

        file_logger = FileLogger(
            log_file=log_file,
            level=LogLevel.DEBUG,
            queue=queue,
            hostname_map=hostname_map,
        )

        # Queue a log event
        test_event = LogEvent(
            level=LogLevel.INFO,
            job="test-job",
            host=Host.SOURCE,
            message="Test message",
            context={"extra_key": "extra_value"},
        )
        await queue.put(test_event)
        await queue.put(None)  # Shutdown sentinel

        # Consume events
        await file_logger.consume()

        # Read and parse log file
        log_content = log_file.read_text().strip()
        log_entry = json.loads(log_content)

        # Verify required fields
        assert "timestamp" in log_entry
        assert "level" in log_entry
        assert "job" in log_entry
        assert "host" in log_entry
        assert "hostname" in log_entry
        assert "event" in log_entry

        # Verify values
        assert log_entry["level"] == "INFO"
        assert log_entry["job"] == "test-job"
        assert log_entry["host"] == "source"
        assert log_entry["hostname"] == "source-host"
        assert log_entry["event"] == "Test message"
        assert log_entry["extra_key"] == "extra_value"

        # Verify timestamp is ISO8601 format
        datetime.fromisoformat(log_entry["timestamp"])

    @pytest.mark.asyncio
    async def test_001_us4_as4_log_file_json_lines_format(self, tmp_path: Path) -> None:
        """US4-AS4: Log file contains JSON Lines (one JSON object per line).

        Verifies that multiple log entries are written as separate lines,
        each containing a complete JSON object.
        """
        log_file = tmp_path / "test.log"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        hostname_map = {Host.SOURCE: "source-host"}

        file_logger = FileLogger(
            log_file=log_file,
            level=LogLevel.DEBUG,
            queue=queue,
            hostname_map=hostname_map,
        )

        # Queue multiple events
        await queue.put(
            LogEvent(
                level=LogLevel.INFO,
                job="job1",
                host=Host.SOURCE,
                message="First message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.WARNING,
                job="job2",
                host=Host.SOURCE,
                message="Second message",
            )
        )
        await queue.put(None)  # Shutdown sentinel

        # Consume events
        await file_logger.consume()

        # Read log file
        log_lines = log_file.read_text().strip().split("\n")

        # Verify we have exactly 2 lines
        assert len(log_lines) == 2

        # Verify each line is valid JSON
        entry1 = json.loads(log_lines[0])
        entry2 = json.loads(log_lines[1])

        assert entry1["event"] == "First message"
        assert entry2["event"] == "Second message"


class TestProgressLogging:
    """Test FR-045: Progress updates logged at FULL level."""

    @pytest.mark.asyncio
    async def test_001_fr045_progress_logged_at_full(self, tmp_path: Path) -> None:
        """FR-045: Progress updates are written to log file at FULL level.

        Verifies that ProgressEvent is written to the log file when file
        log level is FULL or DEBUG.
        """
        log_file = tmp_path / "test.log"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        hostname_map = {Host.SOURCE: "source-host"}

        # Create FileLogger at FULL level
        file_logger = FileLogger(
            log_file=log_file,
            level=LogLevel.FULL,
            queue=queue,
            hostname_map=hostname_map,
        )

        # Queue a progress event
        progress = ProgressUpdate(percent=50, current=10, total=20, item="file.txt")
        await queue.put(ProgressEvent(job="sync-job", update=progress))
        await queue.put(None)  # Shutdown sentinel

        # Consume events
        await file_logger.consume()

        # Read and parse log file
        log_content = log_file.read_text().strip()
        log_entry = json.loads(log_content)

        # Verify progress was logged with FULL level
        assert log_entry["level"] == "FULL"
        assert log_entry["job"] == "sync-job"
        assert log_entry["event"] == "progress_update"
        assert log_entry["percent"] == 50
        assert log_entry["current"] == 10
        assert log_entry["total"] == 20
        assert log_entry["item"] == "file.txt"



class TestLogLevelFiltering:
    """Test US4-AS1 and US4-AS2: Log level filtering behavior."""

    @pytest.mark.asyncio
    async def test_001_us4_as1_debug_excluded_at_full_level(self, tmp_path: Path) -> None:
        """US4-AS1: DEBUG messages are excluded when log level is FULL.

        When file level is FULL, DEBUG messages should not appear in log file.
        FULL and above should appear.
        """
        log_file = tmp_path / "test.log"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        hostname_map = {Host.SOURCE: "source-host"}

        # Create FileLogger at FULL level
        file_logger = FileLogger(
            log_file=log_file,
            level=LogLevel.FULL,
            queue=queue,
            hostname_map=hostname_map,
        )

        # Queue events at DEBUG and FULL levels
        await queue.put(
            LogEvent(
                level=LogLevel.DEBUG,
                job="test",
                host=Host.SOURCE,
                message="Debug message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.FULL,
                job="test",
                host=Host.SOURCE,
                message="Full message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.INFO,
                job="test",
                host=Host.SOURCE,
                message="Info message",
            )
        )
        await queue.put(None)  # Shutdown sentinel

        # Consume events
        await file_logger.consume()

        # Read log file
        log_content = log_file.read_text().strip()
        log_lines = [line for line in log_content.split("\n") if line]

        # Should have 2 entries (FULL and INFO, not DEBUG)
        assert len(log_lines) == 2

        entries = [json.loads(line) for line in log_lines]
        assert entries[0]["level"] == "FULL"
        assert entries[0]["event"] == "Full message"
        assert entries[1]["level"] == "INFO"
        assert entries[1]["event"] == "Info message"

    @pytest.mark.asyncio
    async def test_001_us4_as2_full_excluded_at_info_level(self, tmp_path: Path) -> None:
        """US4-AS2: FULL messages are excluded when log level is INFO.

        When file level is INFO, FULL messages should not appear in log file.
        INFO and above should appear.
        """
        log_file = tmp_path / "test.log"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        hostname_map = {Host.SOURCE: "source-host"}

        # Create FileLogger at INFO level
        file_logger = FileLogger(
            log_file=log_file,
            level=LogLevel.INFO,
            queue=queue,
            hostname_map=hostname_map,
        )

        # Queue events at FULL and INFO levels
        await queue.put(
            LogEvent(
                level=LogLevel.FULL,
                job="test",
                host=Host.SOURCE,
                message="Full message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.INFO,
                job="test",
                host=Host.SOURCE,
                message="Info message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.WARNING,
                job="test",
                host=Host.SOURCE,
                message="Warning message",
            )
        )
        await queue.put(None)  # Shutdown sentinel

        # Consume events
        await file_logger.consume()

        # Read log file
        log_content = log_file.read_text().strip()
        log_lines = [line for line in log_content.split("\n") if line]

        # Should have 2 entries (INFO and WARNING, not FULL)
        assert len(log_lines) == 2

        entries = [json.loads(line) for line in log_lines]
        assert entries[0]["level"] == "INFO"
        assert entries[0]["event"] == "Info message"
        assert entries[1]["level"] == "WARNING"
        assert entries[1]["event"] == "Warning message"


class TestConsoleLoggerFiltering:
    """Test ConsoleLogger respects independent CLI log level."""

    @pytest.mark.asyncio
    async def test_console_logger_filters_by_level(self) -> None:
        """ConsoleLogger filters events based on configured CLI level.

        Verifies that terminal output respects the CLI log level setting,
        independently of file log level.
        """
        console = MagicMock(spec=Console)
        queue: asyncio.Queue[Any] = asyncio.Queue()
        hostname_map = {Host.SOURCE: "source-host"}

        # Create ConsoleLogger at WARNING level
        console_logger = ConsoleLogger(
            console=console,
            level=LogLevel.WARNING,
            queue=queue,
            hostname_map=hostname_map,
        )

        # Queue events at different levels
        await queue.put(
            LogEvent(
                level=LogLevel.INFO,
                job="test",
                host=Host.SOURCE,
                message="Info message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.WARNING,
                job="test",
                host=Host.SOURCE,
                message="Warning message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.ERROR,
                job="test",
                host=Host.SOURCE,
                message="Error message",
            )
        )
        await queue.put(None)  # Shutdown sentinel

        # Consume events
        await console_logger.consume()

        # ConsoleLogger should have printed WARNING and ERROR (not INFO)
        assert console.print.call_count == 2


class TestHostnameResolution:
    """Test that hostname resolution works correctly in loggers."""

    @pytest.mark.asyncio
    async def test_file_logger_resolves_hostname(self, tmp_path: Path) -> None:
        """FileLogger should resolve Host enum to actual hostname."""
        log_file = tmp_path / "test.log"
        queue: asyncio.Queue[Any] = asyncio.Queue()
        hostname_map = {
            Host.SOURCE: "my-laptop",
            Host.TARGET: "my-workstation",
        }

        file_logger = FileLogger(
            log_file=log_file,
            level=LogLevel.DEBUG,
            queue=queue,
            hostname_map=hostname_map,
        )

        # Queue events for both hosts
        await queue.put(
            LogEvent(
                level=LogLevel.INFO,
                job="test",
                host=Host.SOURCE,
                message="Source message",
            )
        )
        await queue.put(
            LogEvent(
                level=LogLevel.INFO,
                job="test",
                host=Host.TARGET,
                message="Target message",
            )
        )
        await queue.put(None)  # Shutdown sentinel

        # Consume events
        await file_logger.consume()

        # Read log file
        log_lines = log_file.read_text().strip().split("\n")
        entries = [json.loads(line) for line in log_lines]

        # Verify hostname resolution
        assert entries[0]["hostname"] == "my-laptop"
        assert entries[0]["host"] == "source"
        assert entries[1]["hostname"] == "my-workstation"
        assert entries[1]["host"] == "target"
