"""Terminal UI for pc-switcher using Rich library."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.text import Text

from pcswitcher.core.logging import LogLevel

if TYPE_CHECKING:
    from pcswitcher.core.session import SessionState


class TerminalUI:
    """Rich-based terminal UI for sync operation progress and logging.

    Provides real-time progress updates, job tracking, and color-coded
    log display without flicker. Uses rich.live.Live for smooth updates.
    """

    def __init__(self) -> None:
        """Initialize TerminalUI with console and progress components."""
        self.console = Console()
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("•"),
            TextColumn("{task.fields[item]}"),
            TimeRemainingColumn(),
            console=self.console,
        )

        # Track job tasks for progress updates
        self._job_tasks: dict[str, TaskID] = {}
        self._overall_task: TaskID | None = None
        self._live: Live | None = None
        self._is_started = False

    def start(self) -> None:
        """Start the live display for real-time updates without flicker."""
        if self._is_started:
            return

        # Create layout with progress
        self._live = Live(self.progress, console=self.console, refresh_per_second=10)
        self._live.start()
        self._is_started = True

    def stop(self) -> None:
        """Stop the live display."""
        if self._live is not None and self._is_started:
            self._live.stop()
            self._is_started = False

    def create_job_task(self, job_name: str) -> None:
        """Add a progress bar for a job.

        Args:
            job_name: Name of the job to track
        """
        if not self._is_started:
            self.start()

        task_id = self.progress.add_task(
            f"[cyan]{job_name}[/cyan]",
            total=100,
            item="Initializing...",
        )
        self._job_tasks[job_name] = task_id

    def update_progress(self, job_name: str, percentage: float, item: str = "") -> None:
        """Update progress bar for a job.

        Args:
            job_name: Name of the job to update
            percentage: Progress percentage (0.0 to 1.0)
            item: Current item being processed
        """
        task_id = self._job_tasks.get(job_name)
        if task_id is None:
            return

        # Convert percentage to 0-100 range
        completed = percentage * 100

        self.progress.update(
            task_id,
            completed=completed,
            item=item if item else "Processing...",
        )

    def display_log(self, level: LogLevel, message: str, **context: object) -> None:
        """Display a log message with color-coding by level.

        Args:
            level: Log level
            message: Log message
            **context: Additional context to display
        """
        # Map LogLevel to colors and styles
        level_styles = {
            LogLevel.DEBUG: ("dim", "DEBUG"),
            LogLevel.FULL: ("", "FULL"),
            LogLevel.INFO: ("blue", "INFO"),
            LogLevel.WARNING: ("yellow", "WARN"),
            LogLevel.ERROR: ("red", "ERROR"),
            LogLevel.CRITICAL: ("bold red", "CRIT"),
        }

        style, level_name = level_styles.get(level, ("", str(level)))

        # Format message with context
        formatted_message = message
        if context:
            context_str = " ".join(f"{k}={v}" for k, v in context.items())
            formatted_message = f"{message} [{context_str}]"

        # Create colored text
        text = Text()
        if style:
            text.append(f"[{level_name}] ", style=style)
            text.append(formatted_message, style=style if level >= LogLevel.WARNING else "")
        else:
            text.append(f"[{level_name}] {formatted_message}")

        # Print below progress bars
        self.console.print(text)

    def show_overall_progress(self, current_job_index: int, total_jobs: int) -> None:
        """Display overall progress across all jobs.

        Args:
            current_job_index: Index of current job (0-based)
            total_jobs: Total number of jobs to execute
        """
        if self._overall_task is None:
            self._overall_task = self.progress.add_task(
                "[bold green]Overall Progress[/bold green]",
                total=total_jobs,
                item=f"Job {current_job_index + 1}/{total_jobs}",
            )
        else:
            self.progress.update(
                self._overall_task,
                completed=current_job_index,
                item=f"Job {current_job_index + 1}/{total_jobs}",
            )

    def show_session_summary(
        self,
        state: SessionState,
        duration: str,
        jobs_succeeded: int,
        jobs_failed: int,
        total_jobs: int,
    ) -> None:
        """Display session summary table.

        Args:
            state: Final session state
            duration: Sync duration as string
            jobs_succeeded: Number of jobs that succeeded
            jobs_failed: Number of jobs that failed
            total_jobs: Total number of jobs
        """
        # Stop live display before showing summary
        self.stop()

        # Create summary table
        table = Table(title="Sync Session Summary", show_header=False, box=None)

        # State row with color
        state_colors = {
            "COMPLETED": "green",
            "ABORTED": "yellow",
            "FAILED": "red",
        }
        state_color = state_colors.get(str(state), "white")
        table.add_row("Status:", f"[bold {state_color}]{state}[/bold {state_color}]")

        # Duration
        table.add_row("Duration:", duration)

        # Job results
        table.add_row("Jobs:", f"{jobs_succeeded}/{total_jobs} succeeded")
        if jobs_failed > 0:
            table.add_row("", f"[red]{jobs_failed} failed[/red]")

        self.console.print()
        self.console.print(table)
        self.console.print()

    def show_error(self, message: str) -> None:
        """Display an error message.

        Args:
            message: Error message to display
        """
        self.console.print(f"[bold red]Error:[/bold red] {message}")

    def show_info(self, message: str) -> None:
        """Display an info message.

        Args:
            message: Info message to display
        """
        self.console.print(f"[blue]Info:[/blue] {message}")

    def show_warning(self, message: str) -> None:
        """Display a warning message.

        Args:
            message: Warning message to display
        """
        self.console.print(f"[yellow]Warning:[/yellow] {message}")

    def show_success(self, message: str) -> None:
        """Display a success message.

        Args:
            message: Success message to display
        """
        self.console.print(f"[green]Success:[/green] {message}")
