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
    "RichFormatter",
    "UILogHandler",
    "UISink",
    "WarningCaptureHandler",
    "format_log_line",
    "generate_log_filename",
    "get_latest_log_file",
    "get_logs_directory",
    "setup_logging",
]


class UISink(Protocol):
    """Structural type for the UI component the logging handlers feed.

    Matches `TerminalUI` (src/pcswitcher/ui.py) without importing it into
    logger.py, keeping the logging module free of a UI dependency. Groups the
    two log-facing entry points the UI exposes: `add_log_message` (rolling
    Recent Logs panel) and `add_warning` (the resurfaced warning buffer that
    backs the live counter and the end-of-run summary).
    """

    def add_log_message(self, message: str) -> None:
        """Append a formatted line to the UI's rolling log panel."""
        ...

    def add_warning(self, line: str) -> None:
        """Append a formatted `>=WARNING` line to the UI's persistent warning buffer."""
        ...


def format_log_line(record: logging.LogRecord) -> str:
    """Build a compact plain-text line: HH:MM:SS [LEVEL] [job] (host) message.

    No ANSI escape codes or Rich markup: the destinations (Recent Logs panel,
    warning summary) render strings as-is, so raw ANSI would show literally and
    Rich markup would risk markup-injection from arbitrary message content.
    """
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

    def __init__(self, sink: UISink) -> None:
        """Initialize the handler, capturing the currently-running event loop.

        Must be constructed from within the running orchestrator loop —
        the loop is captured once here, not re-resolved per emit().
        """
        super().__init__()
        self._sink = sink
        self._loop = asyncio.get_running_loop()

    def emit(self, record: logging.LogRecord) -> None:
        """Format the record as plain text and hand it to the event loop."""
        try:
            line = format_log_line(record)
            self._loop.call_soon_threadsafe(self._sink.add_log_message, line)
        except RuntimeError:
            # Loop closed (late record during shutdown) -- swallow via the
            # standard handler error path instead of raising from the
            # QueueListener's background thread.
            self.handleError(record)


class WarningCaptureHandler(logging.Handler):
    """Tee every `>=WARNING` record into the UI's persistent warning buffer.

    Solves the "warnings scroll past unread" problem: the rolling Recent Logs
    panel overwrites a warning within a few frames, so a distracted user never
    sees it. This handler captures each `>=WARNING` line so the UI can (a) show
    a persistent `⚠ N` counter in the status bar and (b) reprint the full list
    into scrollback after the Live display stops (orchestrator `_cleanup`).

    Its level is fixed to WARNING independently of the display handler's level,
    so warnings are captured even when the TUI is configured to show only a
    higher floor. emit() runs on the QueueListener's background thread and
    appends synchronously — `list.append` is atomic under the GIL, and the
    status-bar counter surfaces via Live's own 10 Hz auto-refresh, so no
    cross-thread `Live.update` (and thus no cursor desync) is needed. By the
    time `QueueListener.stop()` returns, every record has been emitted, so the
    buffer is complete and safe to read for the end-of-run summary.
    """

    def __init__(self, sink: UISink) -> None:
        """Initialize the handler and pin its level to WARNING."""
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        """Append the formatted record to the UI's warning buffer."""
        # Never let a capture failure escape the listener thread; route it
        # through the standard handler-error path instead.
        try:
            self._sink.add_warning(format_log_line(record))
        except Exception:
            self.handleError(record)


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
    ui: UISink | None = None,
    console: Console | None = None,
) -> tuple[QueueListener, Queue[logging.LogRecord]]:
    """Set up stdlib logging infrastructure with QueueHandler/QueueListener.

    Creates a non-blocking logging setup using a queue to decouple log emission
    from log writing. This ensures logging calls don't block on I/O operations.

    The logger hierarchy implements a 3-setting model:
    - Root logger level = external (filters external libs at this level)
    - pcswitcher logger level = min(file, tui[, WARNING when capturing]) (allows
      pcswitcher logs, and always warnings, to reach the handlers)
    - Each handler applies its own level filter (file vs tui vs the WARNING capture)

    In the interactive UI path a WarningCaptureHandler (level WARNING) is added
    so every `>=WARNING` record is teed into the UI's persistent warning buffer,
    which backs the status-bar `⚠ N` counter and the end-of-run summary. Its
    fixed WARNING level is independent of `log_config.tui`; the pcswitcher logger
    floor is lowered to include WARNING so records still reach the queue even
    when the TUI display floor is set higher. `respect_handler_level` keeps the
    file/TUI display handlers filtering at their own levels regardless.

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
        ui: Sink for the UI's Recent Logs panel and warning buffer. None disables
            UI routing (stderr fallback), which also keeps existing callers unaffected.
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

    # In the interactive path also capture every >=WARNING record so the UI can
    # resurface it (persistent counter + end-of-run summary). Not installed in
    # the stderr fallback: there warnings already land in scrollback unerased.
    handlers: list[logging.Handler] = [file_handler, tui_handler]
    if use_ui:
        assert ui is not None  # narrowed by use_ui
        handlers.append(WarningCaptureHandler(ui))

    # Create and start listener with respect_handler_level=True
    # This ensures each handler's level is used as an additional filter
    listener = QueueListener(
        queue,
        *handlers,
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

    # pcswitcher logger - direct handler, no propagation to root.
    # When capturing warnings, floor at WARNING too so they reach the queue even
    # if both display levels are set above WARNING (the display handlers still
    # filter at their own levels via respect_handler_level).
    pcswitcher_logger = logging.getLogger("pcswitcher")
    logger_level = min(log_config.file, log_config.tui)
    if use_ui:
        logger_level = min(logger_level, logging.WARNING)
    pcswitcher_logger.setLevel(logger_level)
    pcswitcher_logger.addHandler(QueueHandler(queue))
    pcswitcher_logger.propagate = False  # Don't propagate to root (avoids external filter)

    # Root logger for external libs only (pcswitcher logs don't reach here)
    root = logging.getLogger()
    root.setLevel(log_config.external)
    root.addHandler(QueueHandler(queue))

    return listener, queue
