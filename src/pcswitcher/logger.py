"""Logging infrastructure for pc-switcher."""

from __future__ import annotations

import atexit
import io
import json
import logging
import sys
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from queue import Queue
from typing import Any, ClassVar

from rich.console import Console
from rich.text import Text

from pcswitcher.config import LogConfig

# Register custom FULL level (15) with stdlib logging
FULL = 15
logging.addLevelName(FULL, "FULL")


def _full(self: logging.Logger, message: str, *args: object, **kwargs: Any) -> None:
    """Log a message at the FULL level (between DEBUG and INFO)."""
    if self.isEnabledFor(FULL):
        self._log(FULL, message, args, **kwargs)


logging.Logger.full = _full  # type: ignore[method-assign]

__all__ = [
    "FULL",
    "JsonFormatter",
    "RichFormatter",
    "generate_log_filename",
    "get_latest_log_file",
    "get_logs_directory",
    "setup_logging",
]


class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines for file output.

    Output format matches the existing FileLogger JSON structure for
    backwards compatibility. Additional context from the extra dict
    is included as top-level fields (FR-011).
    """

    # Standard LogRecord attributes to exclude from extra context
    _STANDARD_ATTRS: ClassVar[frozenset[str]] = frozenset(
        {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "exc_info",
            "exc_text",
            "thread",
            "threadName",
            "taskName",
            "message",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON line."""
        # Build base log dict
        log_dict: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
        }

        # Add optional job/host fields (omit when missing per data-model.md)
        job = getattr(record, "job", None)
        if job is not None:
            log_dict["job"] = job

        host = getattr(record, "host", None)
        if host is not None:
            log_dict["host"] = host

        # Add message
        log_dict["event"] = record.getMessage()

        # Add all extra context fields (FR-011)
        extra_fields = {
            key: value
            for key, value in record.__dict__.items()
            if key not in self._STANDARD_ATTRS and key not in {"job", "host"}
        }
        log_dict.update(extra_fields)

        return json.dumps(log_dict, default=str)


class RichFormatter(logging.Formatter):
    """Format log records with ANSI escape codes for TUI display.

    Output format: HH:MM:SS [LEVEL   ] [job] (host) message context

    Uses Rich Text objects to build styled output and exports to ANSI escape
    sequences for direct rendering by StreamHandler.

    Additional context from the extra dict is appended as dim text (FR-011).
    Job and host are omitted when missing (e.g., during startup/shutdown).
    """

    LEVEL_COLORS: ClassVar[dict[str, str]] = {
        "DEBUG": "dim",
        "FULL": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold red",
    }

    # Standard LogRecord attributes to exclude from extra context
    _STANDARD_ATTRS: ClassVar[frozenset[str]] = frozenset(
        {
            "name",
            "msg",
            "args",
            "created",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "exc_info",
            "exc_text",
            "thread",
            "threadName",
            "taskName",
            "message",
        }
    )

    def __init__(self) -> None:
        """Initialize formatter with a console for ANSI export."""
        super().__init__()
        # Console for exporting Text to ANSI. force_terminal=True ensures ANSI
        # codes are always emitted. The file is a StringIO we don't use.
        self._console = Console(file=io.StringIO(), force_terminal=True, width=200)

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record with ANSI escape codes."""
        # Format timestamp
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")

        # Get level color
        color = self.LEVEL_COLORS.get(record.levelname, "white")

        # Build Text object with styles (like original ConsoleLogger)
        text = Text()
        text.append(f"{timestamp} ", style="dim")
        text.append(f"[{record.levelname:8}]", style=color)

        # Add job/host only if present (omit during startup/shutdown)
        job = getattr(record, "job", None)
        if job is not None:
            text.append(f" [{job}]", style="blue")

        host = getattr(record, "host", None)
        if host is not None:
            text.append(f" ({host})", style="magenta")

        # Add message
        text.append(f" {record.getMessage()}")

        # Add extra context as dim text (FR-011)
        extra_context = []
        for key, value in record.__dict__.items():
            if key not in self._STANDARD_ATTRS and key not in {"job", "host"}:
                extra_context.append(f"{key}={value}")

        if extra_context:
            text.append(f" {' '.join(extra_context)}", style="dim")

        # Export Text object to ANSI string using Console
        # We need to render the text through the console to get ANSI codes
        with self._console.capture() as capture:
            self._console.print(text, end="")

        return capture.get()


def generate_log_filename(session_id: str) -> str:
    """Generate log filename for a sync session.

    Format: sync-<timestamp>-<session_id>.log
    """
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"sync-{timestamp}-{session_id}.log"


def get_logs_directory() -> Path:
    """Get the logs directory path."""
    return Path.home() / ".local" / "share" / "pc-switcher" / "logs"


def get_latest_log_file() -> Path | None:
    """Get the most recent log file, or None if no logs exist."""
    logs_dir = get_logs_directory()
    if not logs_dir.exists():
        return None

    log_files = sorted(logs_dir.glob("sync-*.log"), reverse=True)
    return log_files[0] if log_files else None


def setup_logging(
    log_file_path: Path,
    log_config: LogConfig,
) -> tuple[QueueListener, Queue[logging.LogRecord]]:
    """Set up stdlib logging infrastructure with QueueHandler/QueueListener.

    Creates a non-blocking logging setup using a queue to decouple log emission
    from log writing. This ensures logging calls don't block on I/O operations.

    The logger hierarchy implements a 3-setting model:
    - Root logger level = external (filters external libs at this level)
    - pcswitcher logger level = min(file, tui) (allows pcswitcher logs to handlers)
    - Each handler applies its own level filter (file vs tui)

    Args:
        log_file_path: Path to the JSON log file
        log_config: Logging level configuration with file, tui, and external settings

    Returns:
        Tuple of (QueueListener, queue) for lifecycle management.
        The listener is auto-stopped via atexit, but callers can stop it
        earlier for explicit cleanup.
    """
    # Ensure log directory exists
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    # Create unbounded queue for log records
    queue: Queue[logging.LogRecord] = Queue(-1)

    # Create file handler with JSON formatter for structured log output
    file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
    file_handler.setLevel(log_config.file)
    file_handler.setFormatter(JsonFormatter())

    # Create stream handler with Rich formatter for TUI output
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(log_config.tui)
    stream_handler.setFormatter(RichFormatter())

    # Create and start listener with respect_handler_level=True
    # This ensures each handler's level is used as an additional filter
    listener = QueueListener(
        queue,
        file_handler,
        stream_handler,
        respect_handler_level=True,
    )
    listener.start()

    # Register cleanup for guaranteed log flushing on exit
    atexit.register(listener.stop)

    # Configure logger hierarchy for 3-setting model
    #
    # The pcswitcher logger gets its own handler and propagate=False to bypass
    # the root logger's level filter. This allows pcswitcher logs at DEBUG/FULL/INFO
    # to reach the handlers even when external is set to WARNING.
    #
    # Root logger handles external library logs (asyncssh, etc.) and filters
    # them at the external level.

    # pcswitcher logger - direct handler, no propagation to root
    pcswitcher_logger = logging.getLogger("pcswitcher")
    pcswitcher_logger.setLevel(min(log_config.file, log_config.tui))
    pcswitcher_logger.addHandler(QueueHandler(queue))
    pcswitcher_logger.propagate = False  # Don't propagate to root (avoids external filter)

    # Root logger for external libs only (pcswitcher logs don't reach here)
    root = logging.getLogger()
    root.setLevel(log_config.external)
    root.addHandler(QueueHandler(queue))

    return listener, queue
