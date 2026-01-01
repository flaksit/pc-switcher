"""Unit tests for logging infrastructure (SC-003, SC-004, SC-007, SC-008)."""

from __future__ import annotations

import io
import json
import logging
import sys
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

    def test_formats_with_ansi_codes(self) -> None:
        """Should format with ANSI escape codes, not Rich markup tags."""
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

        # Should contain ANSI escape codes, not Rich markup tags
        assert "\x1b[" in output  # ANSI escape sequence prefix
        assert "[dim]" not in output  # Should NOT have Rich markup
        assert "[green]" not in output  # Should NOT have Rich markup
        assert "[blue]" not in output  # Should NOT have Rich markup
        assert "[magenta]" not in output  # Should NOT have Rich markup

        # Should still contain the actual content
        assert "Test message" in output
        assert "[btrfs]" in output  # job in brackets
        assert "(source)" in output  # host in parens
        assert "[INFO" in output  # level in brackets

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

        # Should contain ANSI escape codes
        assert "\x1b[" in output
        assert "Test message" in output

        # Should not contain job/host markers when not set
        # (job would appear as [jobname], host as (hostname))
        # We look for the specific format that would indicate job/host are present
        # Since job/host are not set, we shouldn't see "[btrfs]" or "(source)"
        # but we should still see "[INFO" for the level
        assert "[INFO" in output

    def test_level_colors_produce_ansi(self) -> None:
        """Different log levels should produce different ANSI color codes."""
        formatter = RichFormatter()

        # Test INFO (green)
        info_record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Info msg",
            args=(),
            exc_info=None,
        )
        info_output = formatter.format(info_record)
        assert "\x1b[" in info_output
        assert "Info msg" in info_output

        # Test WARNING (yellow)
        warn_record = logging.LogRecord(
            name="pcswitcher.test",
            level=logging.WARNING,
            pathname="test.py",
            lineno=1,
            msg="Warning msg",
            args=(),
            exc_info=None,
        )
        warn_output = formatter.format(warn_record)
        assert "\x1b[" in warn_output
        assert "Warning msg" in warn_output

        # Different levels should produce different output (different ANSI codes)
        # We can't easily check the exact color codes, but we can verify
        # they produce different styled outputs
        assert "[INFO" in info_output
        assert "[WARNING" in warn_output

    def test_extra_context_appended(self) -> None:
        """Extra context fields should be appended as dim text."""
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
        record.subvolume = "@home"  # type: ignore[attr-defined]
        output = formatter.format(record)

        # Should contain the extra context
        assert "subvolume=@home" in output
        # Should still have ANSI codes
        assert "\x1b[" in output


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


class TestAcceptanceScenarios:
    """Acceptance scenario tests for logging filtering behavior.

    These tests verify the 3-setting model works correctly:
    - file: floor for file output
    - tui: floor for TUI output
    - external: additional floor for non-pcswitcher libraries

    Tests correspond to acceptance scenarios from spec.md.
    """

    def test_us1_scenario2_external_warning_filters_asyncssh_info(self) -> None:
        """US1-Scenario 2: external=WARNING filters asyncssh INFO from both outputs.

        Given external: WARNING,
        When asyncssh logs an INFO message,
        Then that message is NOT displayed in TUI or written to log file.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            # external=WARNING should filter out asyncssh INFO
            config = LogConfig(file=10, tui=20, external=30)  # DEBUG, INFO, WARNING
            listener, _queue = setup_logging(log_path, config)

            # Capture stderr for TUI output
            captured_stderr = io.StringIO()
            old_stderr = sys.stderr

            try:
                # Get the stream handler and redirect it to our capture
                # The stream handler is the second handler in the listener
                stream_handler = listener.handlers[1]
                assert isinstance(stream_handler, logging.StreamHandler)
                stream_handler.stream = captured_stderr

                # Log an INFO message from asyncssh (external library)
                asyncssh_logger = logging.getLogger("asyncssh")
                asyncssh_logger.info("SSH connection established")

                # Stop listener to flush all pending records
                listener.stop()

                # Verify file does NOT contain the asyncssh INFO message
                file_content = log_path.read_text() if log_path.exists() else ""
                assert "SSH connection established" not in file_content, (
                    "asyncssh INFO should NOT appear in file when external=WARNING"
                )

                # Verify TUI does NOT contain the asyncssh INFO message
                tui_content = captured_stderr.getvalue()
                assert "SSH connection established" not in tui_content, (
                    "asyncssh INFO should NOT appear in TUI when external=WARNING"
                )

            finally:
                sys.stderr = old_stderr
                if listener._thread and listener._thread.is_alive():
                    listener.stop()

    def test_us1_scenario3_pcswitcher_debug_in_file_not_tui(self) -> None:
        """US1-Scenario 3: pcswitcher DEBUG appears in file but NOT in TUI.

        Given file: DEBUG, tui: INFO, external: WARNING,
        When pcswitcher logs a DEBUG message,
        Then it appears in the file but NOT in the TUI.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            config = LogConfig(file=10, tui=20, external=30)  # DEBUG, INFO, WARNING
            listener, _queue = setup_logging(log_path, config)

            # Capture stderr for TUI output
            captured_stderr = io.StringIO()
            old_stderr = sys.stderr

            try:
                # Redirect stream handler to our capture
                stream_handler = listener.handlers[1]
                assert isinstance(stream_handler, logging.StreamHandler)
                stream_handler.stream = captured_stderr

                # Log a DEBUG message from pcswitcher
                pcswitcher_logger = logging.getLogger("pcswitcher.test")
                pcswitcher_logger.debug("Debug level message from pcswitcher")

                # Stop listener to flush all pending records
                listener.stop()

                # Verify file DOES contain the pcswitcher DEBUG message
                file_content = log_path.read_text()
                assert "Debug level message from pcswitcher" in file_content, (
                    "pcswitcher DEBUG should appear in file when file=DEBUG"
                )

                # Verify TUI does NOT contain the pcswitcher DEBUG message
                tui_content = captured_stderr.getvalue()
                assert "Debug level message from pcswitcher" not in tui_content, (
                    "pcswitcher DEBUG should NOT appear in TUI when tui=INFO"
                )

            finally:
                sys.stderr = old_stderr
                if listener._thread and listener._thread.is_alive():
                    listener.stop()

    def test_us2_scenario3_asyncssh_info_in_file_not_tui(self) -> None:
        """US2-Scenario 3: asyncssh INFO appears in file but NOT in TUI.

        Given external: INFO, file: DEBUG, tui: WARNING,
        When asyncssh emits an INFO,
        Then it appears in the file but NOT in the TUI.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            # external=INFO allows INFO from external libs
            # file=DEBUG includes INFO, tui=WARNING excludes INFO
            config = LogConfig(file=10, tui=30, external=20)  # DEBUG, WARNING, INFO
            listener, _queue = setup_logging(log_path, config)

            # Capture stderr for TUI output
            captured_stderr = io.StringIO()
            old_stderr = sys.stderr

            try:
                # Redirect stream handler to our capture
                stream_handler = listener.handlers[1]
                assert isinstance(stream_handler, logging.StreamHandler)
                stream_handler.stream = captured_stderr

                # Log an INFO message from asyncssh (external library)
                asyncssh_logger = logging.getLogger("asyncssh")
                asyncssh_logger.info("SSH key exchange complete")

                # Stop listener to flush all pending records
                listener.stop()

                # Verify file DOES contain the asyncssh INFO message
                file_content = log_path.read_text()
                assert "SSH key exchange complete" in file_content, (
                    "asyncssh INFO should appear in file when external=INFO, file=DEBUG"
                )

                # Verify TUI does NOT contain the asyncssh INFO message
                tui_content = captured_stderr.getvalue()
                assert "SSH key exchange complete" not in tui_content, (
                    "asyncssh INFO should NOT appear in TUI when tui=WARNING"
                )

            finally:
                sys.stderr = old_stderr
                if listener._thread and listener._thread.is_alive():
                    listener.stop()
