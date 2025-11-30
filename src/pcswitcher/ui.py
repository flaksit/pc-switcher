"""Terminal UI with Rich Live display for progress and logs."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text

from pcswitcher.events import ConnectionEvent, LogEvent, ProgressEvent
from pcswitcher.models import Host, LogLevel, ProgressUpdate

__all__ = ["TerminalUI"]


class TerminalUI:
    """Rich terminal UI with progress bars, log panel, and status display.

    Provides a live-updating terminal interface showing:
    - Connection status and latency
    - Overall sync progress (Step N/M)
    - Per-job progress bars
    - Scrolling log panel with recent messages

    Updates at 10 Hz refresh rate for smooth visual updates.
    """

    def __init__(
        self,
        console: Console,
        max_log_lines: int = 10,
        total_steps: int | None = None,
    ) -> None:
        """Initialize the terminal UI.

        Args:
            console: Rich console for rendering
            max_log_lines: Maximum number of log lines to display in panel
            total_steps: Total number of sync steps/jobs (for overall progress)
        """
        self._console = console
        self._max_log_lines = max_log_lines
        self._total_steps = total_steps
        self._current_step = 0

        # Progress tracking
        self._progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
            expand=True,
        )
        self._job_tasks: dict[str, TaskID] = {}

        # Log panel
        self._log_panel: deque[str] = deque(maxlen=max_log_lines)

        # Connection status
        self._connection_status = "disconnected"
        self._connection_latency: float | None = None

        # Live display
        self._live: Live | None = None

    def _render(self) -> RenderableType:
        """Render the complete UI layout.

        Returns a Group containing:
        1. Status bar (connection, step progress)
        2. Job progress bars
        3. Log panel
        """
        # Status bar
        status = Table.grid(padding=(0, 2))
        status.add_column(justify="left")
        status.add_column(justify="right")

        # Connection status
        conn_text = Text()
        if self._connection_status == "connected":
            conn_text.append("Connection: ", style="dim")
            conn_text.append("connected", style="green")
            if self._connection_latency is not None:
                conn_text.append(f" ({self._connection_latency:.1f}ms)", style="dim")
        else:
            conn_text.append("Connection: ", style="dim")
            conn_text.append("disconnected", style="red")

        # Overall progress
        step_text = Text()
        if self._total_steps is not None:
            step_text.append(f"Step {self._current_step}/{self._total_steps}", style="cyan")

        status.add_row(conn_text, step_text)

        # Log panel with scrolling messages
        log_text = "\n".join(self._log_panel) if self._log_panel else "[dim]No logs yet[/dim]"
        log_panel = Panel(
            log_text,
            title="Recent Logs",
            border_style="blue",
            height=self._max_log_lines + 2,  # +2 for borders
        )

        return Group(status, self._progress, log_panel)

    def start(self) -> None:
        """Start the live display."""
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=10,  # 10 Hz refresh rate
            transient=False,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop the live display."""
        if self._live:
            self._live.stop()
            self._live = None

    def update_job_progress(
        self,
        job: str,
        update: ProgressUpdate,
    ) -> None:
        """Update progress for a specific job.

        Args:
            job: Job name
            update: Progress information to display
        """
        # Create task if it doesn't exist
        if job not in self._job_tasks:
            # Determine total based on update type
            total = 100 if update.percent is not None else (update.total or 100)
            self._job_tasks[job] = self._progress.add_task(
                f"[cyan]{job}[/cyan]",
                total=total,
            )

        task_id = self._job_tasks[job]

        # Update task based on ProgressUpdate type
        if update.percent is not None:
            # Percent-based progress
            description = f"[cyan]{job}[/cyan]"
            if update.item:
                description += f": {update.item}"
            self._progress.update(
                task_id,
                completed=update.percent,
                description=description,
            )
        elif update.current is not None and update.total is not None:
            # Count-based progress (current/total)
            description = f"[cyan]{job}[/cyan]: {update.current}/{update.total} items"
            if update.item:
                description += f" - {update.item}"
            self._progress.update(
                task_id,
                completed=update.current,
                total=update.total,
                description=description,
            )
        elif update.current is not None:
            # Current only (no total)
            description = f"[cyan]{job}[/cyan]: {update.current} items"
            if update.item:
                description += f" - {update.item}"
            self._progress.update(
                task_id,
                description=description,
            )
        elif update.heartbeat:
            # Heartbeat/activity indicator
            description = f"[cyan]{job}[/cyan]"
            if update.item:
                description += f": {update.item}"
            self._progress.update(
                task_id,
                description=description,
            )

        # Refresh display
        if self._live:
            self._live.update(self._render())

    def add_log_message(self, message: str) -> None:
        """Add a message to the log panel.

        Messages are automatically scrolled - only the most recent N messages
        (where N = max_log_lines) are displayed.

        Args:
            message: Formatted log message to display
        """
        self._log_panel.append(message)
        if self._live:
            self._live.update(self._render())

    def set_connection_status(
        self,
        status: str,
        latency: float | None = None,
    ) -> None:
        """Update connection status display.

        Args:
            status: "connected" or "disconnected"
            latency: Round-trip time in milliseconds (None if disconnected)
        """
        self._connection_status = status
        self._connection_latency = latency
        if self._live:
            self._live.update(self._render())

    def set_current_step(self, step: int) -> None:
        """Update current step number for overall progress.

        Args:
            step: Current step number (1-indexed)
        """
        self._current_step = step
        if self._live:
            self._live.update(self._render())

    async def consume_events(
        self,
        queue: asyncio.Queue[Any],
        hostname_map: dict[Host, str] | None = None,
        log_level: LogLevel = LogLevel.INFO,
    ) -> None:
        """Consume events from EventBus queue and update UI.

        This runs as a background task, processing events and updating
        the display accordingly.

        Args:
            queue: EventBus queue to consume from
            hostname_map: Mapping from Host enum to actual hostnames
            log_level: Minimum log level to display in UI
        """
        hostname_map = hostname_map or {}

        while True:
            event = await queue.get()
            if event is None:  # Shutdown sentinel
                break

            if isinstance(event, LogEvent):
                # Filter by log level
                if event.level >= log_level:
                    message = self._format_log_event(event, hostname_map)
                    self.add_log_message(message)

            elif isinstance(event, ProgressEvent):
                self.update_job_progress(event.job, event.update)

            elif isinstance(event, ConnectionEvent):
                self.set_connection_status(event.status, event.latency)

    def _format_log_event(
        self,
        event: LogEvent,
        hostname_map: dict[Host, str],
    ) -> str:
        """Format a log event for display in the log panel.

        Args:
            event: Log event to format
            hostname_map: Mapping from Host enum to actual hostnames

        Returns:
            Formatted string with Rich markup
        """
        # Resolve hostname
        hostname = hostname_map.get(event.host, event.host.value)

        # Color based on level
        level_colors = {
            LogLevel.DEBUG: "dim",
            LogLevel.FULL: "cyan",
            LogLevel.INFO: "green",
            LogLevel.WARNING: "yellow",
            LogLevel.ERROR: "red",
            LogLevel.CRITICAL: "bold red",
        }
        color = level_colors.get(event.level, "white")

        # Format timestamp
        timestamp = event.timestamp.strftime("%H:%M:%S")

        # Build message
        parts = [
            f"[dim]{timestamp}[/dim]",
            f"[{color}]{event.level.name:8}[/{color}]",
            f"[blue]{event.job}[/blue]",
            f"[magenta]({hostname})[/magenta]",
            event.message,
        ]

        # Add context if present
        if event.context:
            ctx = " ".join(f"{k}={v}" for k, v in event.context.items())
            parts.append(f"[dim]{ctx}[/dim]")

        return " ".join(parts)
