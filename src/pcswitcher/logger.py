"""Logging infrastructure for pc-switcher."""

from __future__ import annotations

import asyncio
import atexit
import io
import json
import logging
import sys
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener
from pathlib import Path
from queue import Queue
from typing import Any, ClassVar, Protocol

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
    "LogPanelSink",
    "RichFormatter",
    "UILogHandler",
    "generate_log_filename",
    "get_latest_log_file",
    "get_logs_directory",
    "setup_logging",
]


class LogPanelSink(Protocol):
    """Structural type for a UI component that accepts log-panel lines.

    Matches `TerminalUI.add_log_message` (src/pcswitcher/ui.py) without
    importing TerminalUI directly into logger.py, keeping the logging module
    free of a UI dependency.
    """

    def add_log_message(self, message: str) -> None:
        """Append a formatted line to the UI's log panel."""
        ...


class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines for file output.

    Output format matches the existing FileLogger JSON structure for
    backwards compatibility. Additional context from the extra dict
    is included as top-level fields (LOG-FR-CONTEXT).
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

        # Add all extra context fields (LOG-FR-CONTEXT)
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

    Additional context from the extra dict is appended as dim text (LOG-FR-CONTEXT).
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

        # Add extra context as dim text (LOG-FR-CONTEXT)
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


class UILogHandler(logging.Handler):
    """Route log records into a UI's Recent Logs panel via the event loop.

    emit() runs on the QueueListener's background thread. Handing the
    formatted line to `sink.add_log_message` through
    `loop.call_soon_threadsafe` — rather than calling it directly — keeps
    every Live.update call on the single event-loop thread (the same thread
    that drives progress updates), so there is exactly one Live-render path
    and no cursor desync between an uncoordinated stderr write and Live's own
    redraw bookkeeping (see .planning/debug/tui-live-progress-flooding.md).
    """

    def __init__(self, sink: LogPanelSink) -> None:
        """Initialize the handler, capturing the currently-running event loop.

        Must be constructed from within the running orchestrator loop —
        the loop is captured once here, not re-resolved per emit().
        """
        super().__init__()
        self._sink = sink
        self._loop = asyncio.get_running_loop()

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record as plain text and hand it to the event loop.

        No ANSI escape codes or Rich markup: the Recent Logs panel renders
        strings as-is, so raw ANSI would show literally and Rich markup
        would risk markup-injection from arbitrary message content.
        """
        try:
            line = self._format_line(record)
            self._loop.call_soon_threadsafe(self._sink.add_log_message, line)
        except RuntimeError:
            # Loop closed (late record during shutdown) -- swallow via the
            # standard handler error path instead of raising from the
            # QueueListener's background thread.
            self.handleError(record)

    def _format_line(self, record: logging.LogRecord) -> str:
        """Build a compact plain-text line: HH:MM:SS [LEVEL] [job] (host) message."""
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        parts = [timestamp, f"[{record.levelname}]"]

        job = getattr(record, "job", None)
        if job is not None:
            parts.append(f"[{job}]")

        host = getattr(record, "host", None)
        if host is not None:
            parts.append(f"({host})")

        parts.append(record.getMessage())
        return " ".join(parts)


def is_interactive(console: Console) -> bool:
    """Return True only when the run is fully interactive on both stdin and stdout.

    A run is interactive only if BOTH ends are a terminal: a real terminal on
    stdout (``console.is_terminal``) so a live UI / prompt is actually visible,
    AND a TTY on stdin (``sys.stdin.isatty()``) so the user can actually answer
    a prompt. Requiring both keeps logging setup and the confirmer in agreement
    under mixed redirection (e.g. stdout is a TTY but stdin is ``/dev/null``):
    a single split signal previously let the live UI + UILogHandler activate
    while confirmations silently fell back to ``--allow-*`` flags.
    """
    return console.is_terminal and sys.stdin.isatty()


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
    *,
    ui: LogPanelSink | None = None,
    console: Console | None = None,
) -> tuple[QueueListener, Queue[logging.LogRecord]]:
    """Set up stdlib logging infrastructure with QueueHandler/QueueListener.

    Creates a non-blocking logging setup using a queue to decouple log emission
    from log writing. This ensures logging calls don't block on I/O operations.

    The logger hierarchy implements a 3-setting model:
    - Root logger level = external (filters external libs at this level)
    - pcswitcher logger level = min(file, tui) (allows pcswitcher logs to handlers)
    - Each handler applies its own level filter (file vs tui)

    The TUI-floor handler is chosen by interactivity (`is_interactive`): when
    `ui` is given and both stdout and stdin are terminals, log records are
    routed into the UI's Recent Logs panel (UILogHandler) so they render
    through the same single Live.update path as progress updates, instead of
    writing independently to stderr and desyncing Live's cursor bookkeeping.
    Otherwise (ui/console omitted, or either end is not a terminal) the plain
    stderr StreamHandler is used, unchanged — this is the fallback for CI and
    piped/non-TTY output. Sharing `is_interactive` with the confirmer keeps
    UI routing and prompt interactivity from disagreeing under mixed
    redirection.

    Args:
        log_file_path: Path to the JSON log file
        log_config: Logging level configuration with file, tui, and external settings
        ui: Sink for the UI's Recent Logs panel. None disables UI routing
            (stderr fallback), which also keeps existing callers unaffected.
        console: Console whose terminal status (with stdin's, via
            `is_interactive`) decides UI vs stderr routing. Required (together
            with `ui`) to select the UI sink.

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

    # Create the TUI-floor handler: UI-routed when an interactive Live display
    # owns the terminal, otherwise the plain stderr StreamHandler (unchanged
    # behavior for CI / piped output).
    use_ui = ui is not None and console is not None and is_interactive(console)
    tui_handler: logging.Handler
    if use_ui:
        assert ui is not None  # narrowed by use_ui
        tui_handler = UILogHandler(ui)
    else:
        tui_handler = logging.StreamHandler(sys.stderr)
        tui_handler.setFormatter(RichFormatter())
    tui_handler.setLevel(log_config.tui)

    # Create and start listener with respect_handler_level=True
    # This ensures each handler's level is used as an additional filter
    listener = QueueListener(
        queue,
        file_handler,
        tui_handler,
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
