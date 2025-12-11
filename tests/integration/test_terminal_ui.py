"""Integration tests for Terminal UI with progress reporting.

Tests the Rich-based terminal UI system that displays:
- Job progress bars with percentages
- Overall sync progress (Step N/M)
- Log messages below progress indicators

These tests verify that UI components exist and respond to events correctly.
Visual appearance is verified via manual playbook per 002-testing-framework spec.

NOTE: These tests do not require VM infrastructure. They test the integration
of UI components with the event system in isolation. To run without VMs, set
dummy environment variables:

    export HCLOUD_TOKEN=dummy
    export PC_SWITCHER_TEST_PC1_HOST=dummy
    export PC_SWITCHER_TEST_PC2_HOST=dummy
    export PC_SWITCHER_TEST_USER=dummy
    uv run pytest tests/integration/test_terminal_ui.py -v

References:
- US9-AS1: Display progress bar, percentage, job name
- US9-AS2: Overall and individual job progress
- US9-AS3: Logs displayed below progress indicators
"""

from __future__ import annotations

import asyncio
from io import StringIO

import pytest
from rich.console import Console

from pcswitcher.events import ConnectionEvent, EventBus, LogEvent, ProgressEvent
from pcswitcher.models import Host, LogLevel, ProgressUpdate
from pcswitcher.ui import TerminalUI


async def test_001_us9_as1_progress_display() -> None:
    """Test US9-AS1: Display progress bar, percentage, and job name.

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


async def test_001_us9_as2_multi_job_progress() -> None:
    """Test US9-AS2: Overall and individual job progress display.

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


async def test_001_us9_as3_logs_with_progress() -> None:
    """Test US9-AS3: Logs displayed below progress indicators.

    Verifies that when a job emits log messages:
    - Logs appear below progress bars
    - Logs are formatted with level, job name, host
    - Logs respect the configured CLI log level
    - Log panel scrolls (only shows recent N messages)

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
        consume_task = asyncio.create_task(
            ui.consume_events(
                queue,
                hostname_map={Host.SOURCE: "pc1.local", Host.TARGET: "pc2.local"},
                log_level=LogLevel.INFO,
            )
        )

        # Simulate job execution with both progress and log events
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

        # Log events at various levels
        event_bus.publish(
            LogEvent(
                level=LogLevel.INFO,
                job=job_name,
                host=Host.TARGET,
                message="Installing package nginx",
            )
        )

        event_bus.publish(
            LogEvent(
                level=LogLevel.WARNING,
                job=job_name,
                host=Host.TARGET,
                message="Package nginx has pending updates",
            )
        )

        # Progress event
        event_bus.publish(
            ProgressEvent(
                job=job_name,
                update=ProgressUpdate(percent=75, item="Configuring packages"),
            )
        )

        # More log events (to test scrolling - max_log_lines=3)
        event_bus.publish(
            LogEvent(
                level=LogLevel.INFO,
                job=job_name,
                host=Host.TARGET,
                message="Configuring package nginx",
            )
        )

        event_bus.publish(
            LogEvent(
                level=LogLevel.INFO,
                job=job_name,
                host=Host.TARGET,
                message="Package sync complete",
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

        # Verify log messages are present
        # At least one of the log messages should appear
        log_fragments = ["Installing", "package", "nginx", "complete"]
        logs_found = sum(1 for fragment in log_fragments if fragment.lower() in rendered.lower())
        assert logs_found >= 1, "Log messages should appear in output"

        # Verify connection status appears
        assert "connect" in rendered.lower(), "Connection status should appear"

        # Verify log panel exists and has content
        assert len(ui._log_panel) > 0, "Log panel should contain messages"

        # Verify log panel scrolling (max 3 lines)
        assert len(ui._log_panel) <= 3, "Log panel should respect max_log_lines limit"

    finally:
        ui.stop()
