"""Logging infrastructure for pc-switcher."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.console import Console
from rich.text import Text

from pcswitcher.events import EventBus, LogEvent
from pcswitcher.models import Host, LogLevel

__all__ = [
    "ConsoleLogger",
    "FileLogger",
    "Logger",
    "generate_log_filename",
    "get_latest_log_file",
    "get_logs_directory",
]


class Logger:
    """Main logger that publishes to EventBus."""

    def __init__(self, event_bus: EventBus, job_name: str = "orchestrator") -> None:
        self._event_bus = event_bus
        self._job_name = job_name

    def log(
        self,
        level: LogLevel,
        host: Host,
        message: str,
        **context: Any,
    ) -> None:
        """Log a message at the specified level.

        Args:
            level: Log level
            host: Which machine this log relates to
            message: Human-readable message
            **context: Additional structured context
        """
        self._event_bus.publish(
            LogEvent(
                level=level,
                job=self._job_name,
                host=host,
                message=message,
                context=context,
            )
        )


class FileLogger:
    """Consumes LogEvents and writes JSON lines to file.

    Uses JSON serialization for consistent JSON output format (FR-022).
    Each line is a complete JSON object with no nesting of context fields.
    """

    def __init__(
        self,
        log_file: Path,
        level: LogLevel,
        queue: asyncio.Queue[Any],
        hostname_map: dict[Host, str],
    ) -> None:
        self._log_file = log_file
        self._level = level
        self._queue = queue
        self._hostname_map = hostname_map
        # Ensure log directory exists
        self._log_file.parent.mkdir(parents=True, exist_ok=True)

    async def consume(self) -> None:
        """Run as background task to consume and write log events."""
        with self._log_file.open("a", encoding="utf-8") as f:
            while True:
                event = await self._queue.get()
                if event is None:  # Shutdown sentinel
                    break
                if isinstance(event, LogEvent) and event.level >= self._level:
                    # Convert to dict and add resolved hostname
                    event_dict = event.to_dict()
                    event_dict["hostname"] = self._hostname_map.get(
                        event.host, event.host.value
                    )
                    json_line = json.dumps(event_dict, default=str)
                    f.write(json_line + "\n")
                    f.flush()


class ConsoleLogger:
    """Consumes LogEvents and writes colored output to terminal.

    Uses Rich for colored console output.
    """

    # Color mapping for log levels
    LEVEL_COLORS: ClassVar[dict[LogLevel, str]] = {
        LogLevel.DEBUG: "dim",
        LogLevel.FULL: "cyan",
        LogLevel.INFO: "green",
        LogLevel.WARNING: "yellow",
        LogLevel.ERROR: "red",
        LogLevel.CRITICAL: "bold red",
    }

    def __init__(
        self,
        console: Console,
        level: LogLevel,
        queue: asyncio.Queue[Any],
        hostname_map: dict[Host, str] | None = None,
    ) -> None:
        self._console = console
        self._level = level
        self._queue = queue
        self._hostname_map = hostname_map or {}

    async def consume(self) -> None:
        """Run as background task to consume and display log events."""
        while True:
            event = await self._queue.get()
            if event is None:  # Shutdown sentinel
                break
            if isinstance(event, LogEvent) and event.level >= self._level:
                self._render_event(event)

    def _render_event(self, event: LogEvent) -> None:
        """Render a log event to the console with colors."""
        # Resolve hostname from Host enum
        hostname = self._hostname_map.get(event.host, event.host.value)

        # Format timestamp
        timestamp = event.timestamp.strftime("%H:%M:%S")

        # Get color for level
        color = self.LEVEL_COLORS.get(event.level, "white")

        # Build formatted line
        text = Text()
        text.append(f"{timestamp} ", style="dim")
        text.append(f"[{event.level.name:8}]", style=color)
        text.append(f" [{event.job}]", style="blue")
        text.append(f" ({hostname})", style="magenta")
        text.append(f" {event.message}")

        # Add context if present
        if event.context:
            ctx_str = " ".join(f"{k}={v}" for k, v in event.context.items())
            text.append(f" {ctx_str}", style="dim")

        self._console.print(text)


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
