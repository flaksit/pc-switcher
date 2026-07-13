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


async def test_set_total_steps_updates_total() -> None:
    """Test that set_total_steps updates _total_steps and refreshes the live render.

    Mirrors set_current_step: assigns the value and calls self._live.update(self._render())
    when a live display is active, so a mid-run total correction is reflected immediately.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=10)

    # Without live display: only the stored value changes
    ui.set_total_steps(15)
    assert ui._total_steps == 15, "set_total_steps should update _total_steps without live"

    # With live display active: stored value updates and render is refreshed immediately
    ui.start()
    try:
        ui.set_current_step(5)
        ui.set_total_steps(20)
        assert ui._total_steps == 20, "set_total_steps should update _total_steps with live active"

        # Allow the live display to flush the render to the output buffer
        await asyncio.sleep(0.1)
        rendered = output.getvalue()

        # The step display shows "Step 5/20" — the updated total must appear
        assert "20" in rendered, "Updated total from set_total_steps should appear in rendered output"
    finally:
        ui.stop()


async def test_set_current_step_renders_name() -> None:
    """set_current_step's optional name is shown next to the number, and clears when omitted."""
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=10)

    ui.start()
    try:
        ui.set_current_step(7, "Install on target")
        await asyncio.sleep(0.1)
        assert "Step 7/10" in output.getvalue()
        assert "Install on target" in output.getvalue(), "step name must render next to the number"

        # A subsequent step with no name clears the previous label.
        output.truncate(0)
        output.seek(0)
        ui.set_current_step(8)
        await asyncio.sleep(0.1)
        assert "Install on target" not in output.getvalue(), "omitting the name must clear the previous label"
    finally:
        ui.stop()


async def test_pause_resume_rebuilds_fresh_live_instance() -> None:
    """resume() must rebuild a fresh Live, not restart the pre-pause instance.

    A reused Live retains the shape of its pre-pause frame, so its first
    post-resume refresh moves the cursor up by that stale height and overwrites
    the confirmation warning printed while paused. pause() therefore stops
    transiently (erasing the region) and discards the instance; resume() builds
    a new one that anchors at the current cursor, below the printed prompt.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=5)

    ui.start()
    try:
        live_after_start = ui._live  # pyright: ignore[reportPrivateUsage]
        assert live_after_start is not None

        ui.pause()
        # pause() stops the old instance and discards it.
        assert live_after_start.is_started is False, "pause() must stop rendering"
        assert ui._live is None, "pause() must discard the instance"  # pyright: ignore[reportPrivateUsage]

        ui.resume()
        assert ui._live is not None, "resume() must build a live instance"  # pyright: ignore[reportPrivateUsage]
        assert ui._live is not live_after_start, "resume() must build a FRESH instance"  # pyright: ignore[reportPrivateUsage]
        assert ui._live.is_started is True, "resume() must start the fresh instance"  # pyright: ignore[reportPrivateUsage]
    finally:
        ui.stop()

    assert ui._live is None, "stop() remains the final teardown and nulls the instance"  # pyright: ignore[reportPrivateUsage]


async def test_resume_without_prior_pause_is_silent() -> None:
    """resume() must stay a no-op when no pause() actually stopped a live region.

    pause() discards the instance, so resume() can no longer key off _live to
    tell "was live, rebuild" from "never started". The _paused flag guards it:
    a resume() on a UI that never started (or was already resumed) must not
    spring a Live into existence.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=5)

    # Never started: pause() is a no-op, so resume() must not create a Live.
    ui.pause()
    ui.resume()
    assert ui._live is None, "resume() must not build a Live when nothing was paused"  # pyright: ignore[reportPrivateUsage]


async def test_resume_forces_redraw_of_state_mutated_while_paused() -> None:
    """resume() must redraw immediately, not wait for an unrelated future update.

    Isolates resume()'s own redraw: mutate state while paused (stored via the
    is_started guard but not rendered), call resume() with no further mutator
    calls, and assert the rendered output already reflects the new state.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=5)

    ui.start()
    try:
        ui.set_current_step(2)

        ui.pause()
        # Mutated while paused: stored, but the is_started guard prevents rendering.
        ui.set_current_step(3)

        # Capture only the output produced by resume() itself, with no
        # auto-refresh tick (no sleep) in between, so the assertion proves the
        # redraw is forced immediately by resume() rather than masked by the
        # 10 Hz auto-refresh thread.
        output.truncate(0)
        output.seek(0)
        ui.resume()

        rendered = output.getvalue()
        assert "Step 3/5" in rendered, "resume() must force an immediate redraw reflecting state mutated while paused"
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


async def test_log_panel_renders_markup_like_content_literally() -> None:
    """Log lines containing markup-like sequences must not crash the panel render.

    Regression for the Recent Logs panel interpreting arbitrary log content as
    Rich console markup: a real rsync deletion path such as `[/old]` raised
    MarkupError when the joined log text was passed to Panel as a bare str,
    killing the Live auto-refresh thread and the Live.stop() teardown frame.
    Drives the real TerminalUI render (and stop) and asserts no exception plus
    literal path/stderr text in the output.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=1)

    ui.start()
    try:
        # Unbalanced closing tag: this is the sequence that raised MarkupError.
        ui.add_log_message("12:00:00 [FULL] [folder_sync] *deleting home/user/[/old]/cache")
        # rsync-stderr-style CRITICAL line with bracketed tokens.
        ui.add_log_message("12:00:01 [CRITICAL] [folder_sync] rsync failed: rsync: [sender] link_stat failed")

        # Force a full render + final teardown frame; neither may raise.
        ui.stop()

        rendered = output.getvalue()
    finally:
        # stop() already ran in the happy path; guard against a mid-test raise
        # leaving the Live active without masking the original exception.
        ui.stop()

    assert "home/user/[/old]/cache" in rendered, "literal deletion path must render verbatim"
    assert "[sender] link_stat failed" in rendered, "literal rsync stderr must render verbatim"


def _render_to_str(ui: TerminalUI) -> str:
    """Render the UI's current frame to a string, independent of Live mechanics.

    Rich Live redraws its stored renderable on auto-refresh rather than calling
    _render() afresh, so asserting on Live's own output would not reflect a
    just-captured warning without an intervening update. Rendering _render()
    directly tests the frame's content deterministically.
    """
    buf = StringIO()
    Console(file=buf, force_terminal=True, width=120).print(ui._render())
    return buf.getvalue()


async def test_warning_counter_renders_in_status_bar() -> None:
    """Captured warnings surface as a persistent ⚠ N cell in the status bar.

    The counter lives in the render (status bar), not the rolling log panel, so
    it survives every refresh and cannot be scrolled away — the live cue that
    warnings occurred this run.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=3)
    ui.add_warning("12:00:00 [WARNING] [folder_sync] partial transfer")
    ui.add_warning("12:00:01 [ERROR] [disk] low space")

    assert "⚠ 2" in _render_to_str(ui), "status bar must show the persistent warning count"


async def test_no_warning_counter_when_none_captured() -> None:
    """With no warnings captured, no ⚠ indicator is rendered."""
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=3)

    assert "⚠" not in _render_to_str(ui)


async def test_collected_warnings_returns_captured_lines_in_order() -> None:
    """collected_warnings returns every captured line verbatim, in capture order.

    This is what the orchestrator reads to print the end-of-run summary; the
    lines (arbitrary log content) are reprinted wrapped in Text there, so the
    buffer itself just preserves them literally.
    """
    output = StringIO()
    console = Console(file=output, force_terminal=True, width=120)

    ui = TerminalUI(console=console, max_log_lines=5, total_steps=1)
    first = "12:00:00 [WARNING] [folder_sync] deleting home/user/[/old]/cache"
    second = "12:00:01 [ERROR] [folder_sync] rsync: [sender] link_stat failed"
    ui.add_warning(first)
    ui.add_warning(second)

    assert ui.collected_warnings() == [first, second]
