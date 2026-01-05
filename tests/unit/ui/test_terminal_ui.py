"""Unit tests for Terminal UI with progress reporting.

Tests the Rich-based terminal UI system that displays:
- Job progress bars with percentages
- Overall sync progress (Step N/M)
- Log messages below progress indicators

These tests verify that UI components exist and respond to events correctly.
Visual appearance is verified via manual playbook per 002-testing-framework spec.

These tests do not require VM infrastructure. They test the integration
of UI components with the event system in isolation.

References:
- CORE-US-TUI-AS1: Display progress bar, percentage, job name
- CORE-US-TUI-AS2: Overall and individual job progress
- CORE-US-TUI-AS3: Logs displayed below progress indicators
"""

from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from pcswitcher.events import ConnectionEvent, EventBus, ProgressEvent
from pcswitcher.models import ProgressUpdate
from pcswitcher.ui import TerminalUI


async def test_core_us_tui_as1_progress_display() -> None:
    """Test CORE-US-TUI-AS1: Display progress bar, percentage, and job name.

    Verifies that when a job reports progress, the terminal UI:
    - Creates a progress bar for the job
    - Updates the progress percentage
    - Displays the job name
    - Shows current item being processed

    This test verifies the UI behavior exists. Visual appearance is
    verified manually per playbook.
    """
    # Create a console that captures output to StringIO for verification
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    # Create UI with no total steps (not testing overall progress here)
    ui = TerminalUI(console=console, max_log_lines=5, total_steps=None)

    # Start the UI
    ui.start()

    try:
        # Simulate a job reporting progress at various stages
        job_name = "Docker Sync"

        # Initial progress - 0%
        ui.update_job_progress(
            job_name,
            ProgressUpdate(percent=0, item="Starting Docker sync"),
        )

        # Progress update - 45%
        ui.update_job_progress(
            job_name,
            ProgressUpdate(percent=45, item="Copying image nginx:latest"),
        )

        # Progress update - 100%
        ui.update_job_progress(
            job_name,
            ProgressUpdate(percent=100, item="Docker sync complete"),
        )

        # Give Rich time to render
        await asyncio.sleep(0.2)

        # Verify output contains expected elements
        rendered = output.getvalue()

        # Check that job name appears in output
        assert "Docker Sync" in rendered or "docker" in rendered.lower()

        # Check that progress indicators are present
        # Rich uses various characters for progress bars, we just verify something rendered
        assert len(rendered) > 0, "UI should produce output"

        # Verify the UI's internal state tracks the job
        assert job_name in ui._job_tasks, "UI should track job in internal state"

    finally:
        ui.stop()


async def test_core_us_tui_as2_multi_job_progress() -> None:
    """Test CORE-US-TUI-AS2: Overall and individual job progress display.

    Verifies that when multiple jobs execute sequentially:
    - Terminal shows overall progress (Step N/M)
    - Terminal shows individual job progress bars
    - Each job's progress is tracked independently

    This test verifies the UI behavior exists. Visual appearance is
    verified manually per playbook.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    # Create UI with total_steps to enable overall progress display
    total_jobs = 7
    ui = TerminalUI(console=console, max_log_lines=5, total_steps=total_jobs)

    ui.start()

    try:
        # Simulate multi-job sync workflow
        jobs = [
            "Install pc-switcher",
            "Btrfs Snapshots",
            "Package Sync",
            "Docker Sync",
            "VM Sync",
            "k3s Sync",
            "User Data Sync",
        ]

        for step_num, job_name in enumerate(jobs, start=1):
            # Update overall progress
            ui.set_current_step(step_num)

            # Simulate job execution with progress updates
            ui.update_job_progress(
                job_name,
                ProgressUpdate(percent=0, item=f"Starting {job_name}"),
            )

            ui.update_job_progress(
                job_name,
                ProgressUpdate(percent=50, item=f"Processing {job_name}"),
            )

            ui.update_job_progress(
                job_name,
                ProgressUpdate(percent=100, item=f"{job_name} complete"),
            )

            # Small delay to allow UI to render
            await asyncio.sleep(0.05)

        # Final render
        await asyncio.sleep(0.2)

        rendered = output.getvalue()

        # Verify overall progress appears in output
        # The UI should show "Step X/7" at various points
        assert "7" in rendered, "Total steps should appear in output"

        # Verify multiple job names appear (at least a few)
        jobs_found = sum(1 for job in jobs if job in rendered or job.lower() in rendered.lower())
        assert jobs_found >= 2, "Multiple job names should appear in output"

        # Verify all jobs are tracked internally
        for job_name in jobs:
            assert job_name in ui._job_tasks, f"UI should track {job_name} in internal state"

    finally:
        ui.stop()


async def test_core_us_tui_as3_progress_and_connection_events() -> None:
    """Test progress and connection event handling via EventBus.

    After ADR-010 logging migration, LogEvent is no longer processed by
    consume_events(). This test verifies that ProgressEvent and ConnectionEvent
    are still handled correctly.

    Note: CORE-US-TUI-AS3 (logs displayed below progress indicators) now applies to
    the log panel, which is populated via add_log_message() directly or
    through stdlib logging. This test focuses on EventBus event handling.

    This test verifies the UI behavior exists. Visual appearance is
    verified manually per playbook.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=3, total_steps=5)
    ui.start()

    try:
        # Set up event bus for event-driven UI updates
        event_bus = EventBus()
        queue = event_bus.subscribe()

        # Create background task to consume events
        consume_task = asyncio.create_task(ui.consume_events(queue))

        # Simulate job execution with progress and connection events
        job_name = "Package Sync"

        # Connection event
        event_bus.publish(ConnectionEvent(status="connected", latency=12.5))

        # Progress event
        event_bus.publish(
            ProgressEvent(
                job=job_name,
                update=ProgressUpdate(percent=25, item="Installing nginx"),
            )
        )

        # Progress event
        event_bus.publish(
            ProgressEvent(
                job=job_name,
                update=ProgressUpdate(percent=75, item="Configuring packages"),
            )
        )

        # Final progress
        event_bus.publish(
            ProgressEvent(
                job=job_name,
                update=ProgressUpdate(percent=100, item="Sync complete"),
            )
        )

        # Allow events to be processed
        await asyncio.sleep(0.3)

        # Close event bus and wait for consume task to finish
        event_bus.close()
        await consume_task

        rendered = output.getvalue()

        # Verify progress elements are present
        assert job_name in rendered or "package" in rendered.lower(), "Job name should appear"

        # Verify connection status appears
        assert "connect" in rendered.lower(), "Connection status should appear"

        # Verify job is tracked internally
        assert job_name in ui._job_tasks, "UI should track job in internal state"

    finally:
        ui.stop()
