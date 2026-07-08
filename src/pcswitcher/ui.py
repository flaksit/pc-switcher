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

from pcswitcher.events import ConnectionEvent, ProgressEvent
from pcswitcher.models import ProgressUpdate

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
        # True only between a pause() that actually stopped a running Live and its
        # paired resume(); lets resume() distinguish "was live, rebuild" from
        # "UI never started, stay silent" now that pause() discards the instance.
        self._paused = False

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

        # Log panel with scrolling messages. The joined log lines are wrapped in
        # a Text object so Rich renders them literally: arbitrary log content
        # (rsync file paths, rsync stderr) can embed markup-like sequences such
        # as `[/old]`, which — if passed to Panel as a bare str — Rich would try
        # to parse as console markup, silently swallowing `[token]` and raising
        # MarkupError on `[/...]`. That crash fires on the Live auto-refresh
        # thread and again during Live.stop() teardown. The "No logs yet"
        # placeholder is trusted literal markup, so it is parsed via from_markup.
        log_body: RenderableType = (
            Text("\n".join(self._log_panel)) if self._log_panel else Text.from_markup("[dim]No logs yet[/dim]")
        )
        log_panel = Panel(
            log_body,
            title="Recent Logs",
            border_style="blue",
            height=self._max_log_lines + 2,  # +2 for borders
        )

        return Group(status, self._progress, log_panel)

    def _build_live(self) -> Live:
        """Construct a fresh Live bound to the current render.

        A brand-new instance is used on every (re)start rather than reused
        across a pause: a reused Live keeps the shape of its pre-pause frame in
        its internal render state, so its first post-resume refresh moves the
        cursor UP by that stale height — landing in the middle of whatever
        static content (e.g. a confirmation warning panel) was printed while
        paused and overwriting it. A fresh instance has no prior shape, so it
        anchors at the current cursor and draws below the printed prompt.
        """
        return Live(
            self._render(),
            console=self._console,
            refresh_per_second=10,  # 10 Hz refresh rate
            transient=False,
        )

    def start(self) -> None:
        """Start the live display for the first time."""
        if self._live is None:
            self._live = self._build_live()
        self._live.start()

    def pause(self) -> None:
        """Stop and erase the live region around a blocking prompt, discarding the instance.

        Used around confirmation prompts: the live region is handed back to a
        blocking `Prompt.ask()` call, then reclaimed by resume(). The stop is
        transient so the pre-pause frame is erased (not left behind as a stale
        duplicate panel) before the prompt's warning is printed into the freed
        space. The instance is then discarded so resume() rebuilds a fresh one —
        see _build_live for why reuse corrupts the post-resume cursor position.
        """
        if self._live is not None:
            self._live.transient = True
            self._live.stop()
            self._live = None
            self._paused = True

    def resume(self) -> None:
        """Rebuild a fresh live region below the printed prompt and redraw immediately.

        No-op unless a paired pause() actually stopped a running Live, so a
        resume() on a UI that was never started stays silent. State mutated
        while paused (job progress, connection status, step number) is stored by
        the update methods but not rendered per the is_started guard; the
        immediate refresh reflects it at once instead of waiting for an
        unrelated future auto-refresh tick.
        """
        if self._paused:
            self._paused = False
            self._live = self._build_live()
            self._live.start()
            self._live.update(self._render(), refresh=True)

    def stop(self) -> None:
        """Stop the live display and discard the instance (final teardown)."""
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
        if self._live is not None and self._live.is_started:
            self._live.update(self._render())

    def add_log_message(self, message: str) -> None:
        """Add a message to the log panel.

        Messages are automatically scrolled - only the most recent N messages
        (where N = max_log_lines) are displayed.

        Args:
            message: Formatted log message to display
        """
        self._log_panel.append(message)
        if self._live is not None and self._live.is_started:
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
        if self._live is not None and self._live.is_started:
            self._live.update(self._render())

    def set_current_step(self, step: int) -> None:
        """Update current step number for overall progress.

        Args:
            step: Current step number (1-indexed)
        """
        self._current_step = step
        if self._live is not None and self._live.is_started:
            self._live.update(self._render())

    def set_total_steps(self, total: int) -> None:
        """Correct the total step count once the actual number of jobs is known.

        Called by the orchestrator after Phase 4 job discovery to replace the
        initial estimate with the exact count of enabled, discoverable jobs.
        Refreshes the live render immediately so the display reflects the correction.

        Args:
            total: Corrected total step count
        """
        self._total_steps = total
        if self._live is not None and self._live.is_started:
            self._live.update(self._render())

    async def consume_events(
        self,
        queue: asyncio.Queue[Any],
    ) -> None:
        """Consume events from EventBus queue and update UI.

        This runs as a background task, processing ProgressEvent and
        ConnectionEvent and updating the display accordingly.

        Note: LogEvent is no longer processed here. After the logging
        infrastructure migration (ADR-010), logs go through stdlib logging
        to file handlers. The TUI log panel is not populated by this method.

        Args:
            queue: EventBus queue to consume from
        """
        while True:
            event = await queue.get()
            if event is None:  # Shutdown sentinel
                break

            if isinstance(event, ProgressEvent):
                self.update_job_progress(event.job, event.update)

            elif isinstance(event, ConnectionEvent):
                self.set_connection_status(event.status, event.latency)

            # LogEvent is intentionally not handled here.
            # See ADR-010 for the logging infrastructure migration.
