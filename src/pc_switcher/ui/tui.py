import asyncio
from typing import Optional, Dict, Deque
from collections import deque
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.text import Text
from rich import box

from pc_switcher.core.events import EventBus, EventType, LogEvent, ProgressEvent, ConnectionEvent


class TerminalUI:
    def __init__(self, event_bus: EventBus, cli_level: str = "INFO"):
        self._event_bus = event_bus
        self._cli_level = cli_level
        self._console = Console()
        self._layout = Layout()
        self._live = Live(self._layout, console=self._console, refresh_per_second=4)

        self._log_messages: Deque[LogEvent] = deque(maxlen=50)
        self._job_progress: Dict[str, ProgressEvent] = {}
        self._connection_status = "Disconnected"
        self._connection_latency = None

        self._queue = asyncio.Queue()
        self._running = False

    def start(self) -> None:
        """Start the UI."""
        self._setup_layout()
        self._live.start()
        self._running = True

    def stop(self) -> None:
        """Stop the UI."""
        self._running = False
        self._live.stop()

    def _setup_layout(self) -> None:
        self._layout.split(Layout(name="header", size=3), Layout(name="main", ratio=1), Layout(name="footer", size=10))
        self._layout["main"].split_row(Layout(name="jobs", ratio=1), Layout(name="logs", ratio=1))

    async def run(self) -> None:
        """Main UI loop."""
        while self._running:
            # Process events
            while not self._queue.empty():
                event = await self._queue.get()
                self._process_event(event)
                self._queue.task_done()

            # Update display
            self._update_display()
            await asyncio.sleep(0.1)

    def _process_event(self, event) -> None:
        if event.type == EventType.LOG:
            # Filter by level (simplified check)
            # In real impl, map levels to ints
            self._log_messages.append(event)
        elif event.type == EventType.PROGRESS:
            self._job_progress[event.job] = event
        elif event.type == EventType.CONNECTION:
            self._connection_status = event.status
            self._connection_latency = event.latency

    def _update_display(self) -> None:
        # Header
        self._layout["header"].update(
            Panel(f"PC-Switcher Sync | Status: {self._connection_status}", style="bold white on blue")
        )

        # Jobs
        job_table = Table(box=box.SIMPLE)
        job_table.add_column("Job")
        job_table.add_column("Progress")
        job_table.add_column("Status")

        for job, progress in self._job_progress.items():
            percent = f"{progress.percent}%" if progress.percent is not None else "..."
            status = progress.item or "Running..."
            job_table.add_row(job, percent, status)

        self._layout["jobs"].update(Panel(job_table, title="Jobs"))

        # Logs
        log_text = Text()
        for log in self._log_messages:
            log_text.append(f"{log.timestamp.strftime('%H:%M:%S')} [{log.level}] {log.message}\n")

        self._layout["logs"].update(Panel(log_text, title="Logs"))

    def get_queue(self) -> asyncio.Queue:
        return self._queue
