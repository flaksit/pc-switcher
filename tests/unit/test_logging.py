"""Unit tests for logging infrastructure (SC-003, SC-004, SC-007, SC-008)."""

from __future__ import annotations

import json
import logging
import tempfile
from logging.handlers import QueueListener
from pathlib import Path
from queue import Queue

import pytest

from pcswitcher.config import Configuration, ConfigurationError, LogConfig
from pcswitcher.logger import (
    FULL,
    JsonFormatter,
    RichFormatter,
    setup_logging,
)


class TestLogLevelRegistration:
    """Test FULL level registration with stdlib logging (SC-003)."""

    def test_full_level_is_registered(self) -> None:
        """FULL level (15) should be registered with logging module."""
        # getLevelName(int) returns the name for that level
        assert logging.getLevelName(15) == "FULL"
        # getLevelNamesMapping() returns a dict of name -> level
        assert logging.getLevelNamesMapping()["FULL"] == 15

    def test_full_constant_value(self) -> None:
        """FULL constant should have value 15."""
        assert FULL == 15


class TestLogConfig:
    """Test LogConfig defaults and validation (SC-004)."""

    def test_default_values(self) -> None:
        """LogConfig should have sensible defaults."""
        config = LogConfig()
        assert config.file == 10  # DEBUG
        assert config.tui == 20  # INFO
        assert config.external == 30  # WARNING

    def test_custom_values(self) -> None:
        """LogConfig should accept custom values."""
        config = LogConfig(file=15, tui=30, external=40)
        assert config.file == 15
        assert config.tui == 30
        assert config.external == 40


class TestJsonFormatter:
    """Test JSON formatter output structure (SC-005)."""

    def test_formats_basic_record(self) -> None:
        """Should format log record as JSON line."""
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
        assert data["level"] == "INFO"
        assert data["event"] == "Test message"

    def test_includes_job_and_host(self) -> None:
        """Should include job and host from extra dict."""
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
        record.job = "orchestrator"  # type: ignore[attr-defined]
        record.host = "source"  # type: ignore[attr-defined]
        output = formatter.format(record)
        data = json.loads(output)
        assert data["job"] == "orchestrator"
        assert data["host"] == "source"

    def test_omits_missing_job_host(self) -> None:
        """Should omit job/host when not set."""
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
        assert "job" not in data
        assert "host" not in data

    def test_includes_extra_context_fields(self) -> None:
        """Should include extra context as top-level fields (FR-011)."""
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


class TestRichFormatter:
    """Test Rich formatter output format (SC-006)."""

    def test_formats_with_rich_markup(self) -> None:
        """Should format with Rich markup."""
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
        assert "[dim]" in output  # timestamp
        assert "[green]" in output  # INFO color
        assert "[blue]" in output  # job
        assert "[magenta]" in output  # host
        assert "Test message" in output

    def test_formats_without_job_host(self) -> None:
        """Should format correctly when job/host are not set."""
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
        # Should contain timestamp and level, no job/host
        assert "[dim]" in output  # timestamp
        assert "[green]" in output  # INFO color
        assert "Test message" in output
        # Should not contain job/host markers
        assert "[blue][" not in output
        assert "[magenta](" not in output


class TestSetupLogging:
    """Test logging infrastructure setup (SC-007, SC-008)."""

    def test_creates_queue_listener(self) -> None:
        """setup_logging should return a QueueListener."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            config = LogConfig()
            listener, queue = setup_logging(log_path, config)
            try:
                assert isinstance(listener, QueueListener)
                assert isinstance(queue, Queue)
            finally:
                listener.stop()

    def test_configures_logger_hierarchy(self) -> None:
        """Should configure root and pcswitcher logger levels."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            config = LogConfig(file=10, tui=20, external=30)
            listener, _queue = setup_logging(log_path, config)
            try:
                root = logging.getLogger()
                pcswitcher = logging.getLogger("pcswitcher")

                assert root.level == 30  # external
                assert pcswitcher.level == 10  # min(file, tui)
                # pcswitcher must not propagate to root to avoid external filter
                assert pcswitcher.propagate is False
                # pcswitcher must have its own handler
                assert len(pcswitcher.handlers) >= 1
            finally:
                listener.stop()

    def test_pcswitcher_logs_not_filtered_by_external_level(self) -> None:
        """pcswitcher INFO logs should reach handlers even when external=WARNING."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            # external=WARNING should NOT filter pcswitcher INFO logs
            config = LogConfig(file=10, tui=20, external=30)
            listener, _queue = setup_logging(log_path, config)
            try:
                logger = logging.getLogger("pcswitcher.test")
                logger.info("Test INFO message", extra={"job": "test", "host": "source"})

                # Stop listener to flush
                listener.stop()

                # Check file contains the INFO log
                content = log_path.read_text()
                assert "Test INFO message" in content
                assert "INFO" in content
            finally:
                if listener._thread and listener._thread.is_alive():
                    listener.stop()

    def test_creates_log_directory(self) -> None:
        """Should create parent directory for log file if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_path = Path(tmpdir) / "nested" / "dirs" / "test.log"
            config = LogConfig()
            listener, _queue = setup_logging(nested_path, config)
            try:
                assert nested_path.parent.exists()
            finally:
                listener.stop()

    def test_queue_listener_can_be_stopped(self) -> None:
        """QueueListener should be stoppable without error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            config = LogConfig()
            listener, _queue = setup_logging(log_path, config)
            # Should not raise
            listener.stop()


class TestInvalidLogLevel:
    """Test invalid log level handling (FR-010)."""

    def test_invalid_level_string_raises_error(self) -> None:
        """Invalid log level string should raise ConfigurationError."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("logging:\n  file: INVALID\n")
            f.flush()

            with pytest.raises(ConfigurationError) as exc_info:
                Configuration.from_yaml(Path(f.name))

            # Schema validation catches invalid level with enum constraint
            error_msg = str(exc_info.value)
            assert "INVALID" in error_msg
            assert "logging.file" in error_msg

    def test_valid_log_levels_accepted(self) -> None:
        """Valid log level strings should be accepted."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("logging:\n  file: DEBUG\n  tui: INFO\n  external: WARNING\n")
            f.flush()

            config = Configuration.from_yaml(Path(f.name))
            assert config.logging.file == 10  # DEBUG
            assert config.logging.tui == 20  # INFO
            assert config.logging.external == 30  # WARNING
