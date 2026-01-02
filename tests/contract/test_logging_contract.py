"""Contract tests for log format compatibility (LOG-SC-TUI-VISUAL, LOG-SC-JSON-STRUCT)."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from pcswitcher.logger import FULL, JsonFormatter, RichFormatter


class TestJsonLogContract:
    """Contract tests for JSON log format."""

    def test_json_line_is_valid_json(self) -> None:
        """Each log line must be valid JSON."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        # Should not raise
        json.loads(output)

    def test_required_fields_present(self) -> None:
        """JSON must include timestamp, level, and event."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "timestamp" in data
        assert "level" in data
        assert "event" in data

    def test_timestamp_is_iso_format(self) -> None:
        """Timestamp must be ISO 8601 format."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        # Should parse as ISO datetime
        datetime.fromisoformat(data["timestamp"])

    def test_extra_fields_included(self) -> None:
        """Extra dict fields must be included as top-level keys."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.subvolume = "@home"  # type: ignore[attr-defined]
        record.bytes_transferred = 1024  # type: ignore[attr-defined]
        output = formatter.format(record)
        data = json.loads(output)
        assert data["subvolume"] == "@home"
        assert data["bytes_transferred"] == 1024

    def test_message_format_args_substituted(self) -> None:
        """Message format args should be substituted in event field."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Processed %d files in %s",
            args=(42, "/home"),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["event"] == "Processed 42 files in /home"

    def test_full_level_formatted_correctly(self) -> None:
        """FULL (15) level should be represented as 'FULL' in output."""
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=FULL,
            pathname="test.py",
            lineno=1,
            msg="Full level message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "FULL"


class TestRichLogContract:
    """Contract tests for Rich TUI format."""

    def test_contains_timestamp(self) -> None:
        """Output must contain HH:MM:SS timestamp."""
        formatter = RichFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        # Should match HH:MM:SS pattern
        assert re.search(r"\d{2}:\d{2}:\d{2}", output)

    def test_contains_level_with_padding(self) -> None:
        """Level must be 8 chars wide with padding."""
        formatter = RichFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        # INFO should be padded to 8 chars
        assert "[INFO    ]" in output

    def test_level_colors_applied(self) -> None:
        """Each level should have ANSI color codes applied."""
        formatter = RichFormatter()

        levels = [
            logging.DEBUG,
            FULL,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        ]

        for level in levels:
            record = logging.LogRecord(
                name="test",
                level=level,
                pathname="",
                lineno=0,
                msg="test",
                args=(),
                exc_info=None,
            )
            output = formatter.format(record)
            # All levels should produce ANSI escape codes
            assert "\x1b[" in output, f"Level {logging.getLevelName(level)} should have ANSI escape codes"

    def test_job_host_formatting(self) -> None:
        """Job should be in brackets, host in parentheses with ANSI colors."""
        formatter = RichFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.job = "btrfs"  # type: ignore[attr-defined]
        record.host = "source"  # type: ignore[attr-defined]
        output = formatter.format(record)
        # Job should be in square brackets (with ANSI blue color)
        assert "[btrfs]" in output
        # Host should be in parentheses (with ANSI magenta color)
        assert "(source)" in output
        # Should have ANSI codes for coloring
        assert "\x1b[" in output

    def test_extra_context_appended_as_dim(self) -> None:
        """Extra context should be appended with ANSI dim styling."""
        formatter = RichFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Processing",
            args=(),
            exc_info=None,
        )
        record.subvolume = "@home"  # type: ignore[attr-defined]
        output = formatter.format(record)
        # Extra context should appear at the end
        assert "subvolume=@home" in output
        # Should have ANSI codes for dim styling
        assert "\x1b[" in output

    def test_message_format_args_substituted(self) -> None:
        """Message format args should be substituted in output."""
        formatter = RichFormatter()
        record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Copied %d files",
            args=(100,),
            exc_info=None,
        )
        output = formatter.format(record)
        assert "Copied 100 files" in output
