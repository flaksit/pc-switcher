"""Logging types and utilities for pc-switcher."""

from __future__ import annotations

import logging
import socket
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from structlog.typing import EventDict, WrappedLogger

if TYPE_CHECKING:
    from pcswitcher.core.session import SyncSession


class LogLevel(IntEnum):
    """Logging levels for pc-switcher operations.

    Uses integer values compatible with standard logging levels,
    with custom FULL level between DEBUG and INFO for detailed output.
    """

    DEBUG = logging.DEBUG
    FULL = logging.DEBUG + 5  # Custom level between DEBUG and INFO
    INFO = logging.INFO
    WARNING = logging.WARNING
    ERROR = logging.ERROR
    CRITICAL = logging.CRITICAL


# Register custom FULL log level with Python's logging module
logging.addLevelName(LogLevel.FULL, "FULL")


def _track_error_logs(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Processor that sets session.has_errors=True when level >= ERROR.

    This is used to determine final session state (COMPLETED vs FAILED).
    """
    # Get the session from the context if available
    session = event_dict.get("_session")
    if session is not None:
        level = event_dict.get("level")
        if level and (level == "error" or level == "critical"):
            session.has_errors = True
    return event_dict


def _add_hostname(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    """Add hostname to log context if not already present."""
    if "hostname" not in event_dict:
        event_dict["hostname"] = socket.gethostname()
    return event_dict


def configure_logging(
    log_file_level: LogLevel,
    log_cli_level: LogLevel,
    log_file_path: Path,
    session: SyncSession | None = None,
) -> None:
    """Configure structlog with dual output: file (JSON) and terminal (Console).

    Args:
        log_file_level: Minimum level for file logging
        log_cli_level: Minimum level for terminal display
        log_file_path: Path to log file
        session: Optional session for error tracking

    The function sets up:
    - File output: JSON format with timestamp, level, job, hostname, event, context
    - Terminal output: Human-readable format with colors via rich
    - Custom FULL log level
    - ERROR tracking for session.has_errors flag
    """
    # Ensure log directory exists
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    # Configure stdlib logging first
    logging.basicConfig(
        format="%(message)s",
        level=min(log_file_level, log_cli_level),
        handlers=[],  # We'll add handlers via structlog
    )

    # Set up file handler for JSON logs
    file_handler = logging.FileHandler(log_file_path)
    file_handler.setLevel(log_file_level)

    # Set up console handler for terminal
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_cli_level)

    # Configure structlog processors
    shared_processors: list[Any] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_hostname,
        structlog.processors.StackInfoRenderer(),
    ]

    # Add session context if provided
    if session is not None:
        shared_processors.insert(0, structlog.contextvars.merge_contextvars)
        structlog.contextvars.bind_contextvars(_session=session)

    structlog.configure(
        processors=shared_processors
        + [
            _track_error_logs,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure formatters
    formatter_file = structlog.stdlib.ProcessorFormatter(
        processor=structlog.processors.JSONRenderer(),
        foreign_pre_chain=shared_processors,
    )
    file_handler.setFormatter(formatter_file)

    formatter_console = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(formatter_console)

    # Add handlers to root logger
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


def get_logger(name: str, **context: Any) -> structlog.stdlib.BoundLogger:
    """Get a logger with bound context.

    Args:
        name: Logger name (typically job name)
        **context: Additional context to bind (e.g., session_id, hostname)

    Returns:
        BoundLogger with context
    """
    logger = structlog.get_logger(name)
    if context:
        logger = logger.bind(**context)
    return logger


def _full_log_method(self: structlog.stdlib.BoundLogger, event: str, **kw: Any) -> Any:
    """Custom method for FULL level logging."""
    return self._log(LogLevel.FULL, event, **kw)  # type: ignore[attr-defined]


# Add custom full() method to BoundLogger
structlog.stdlib.BoundLogger.full = _full_log_method  # type: ignore[attr-defined]


def create_log_file_path(timestamp: datetime | None = None) -> Path:
    """Create log file path in ~/.local/share/pc-switcher/logs/sync-<timestamp>.log.

    Args:
        timestamp: Optional timestamp for log filename. Defaults to current time.

    Returns:
        Path to log file
    """
    if timestamp is None:
        timestamp = datetime.now()

    log_dir = Path.home() / ".local" / "share" / "pc-switcher" / "logs"
    log_filename = f"sync-{timestamp.strftime('%Y%m%d-%H%M%S')}.log"
    return log_dir / log_filename
