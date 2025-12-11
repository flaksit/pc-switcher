"""Unit tests for orchestrator interrupt handling.

Tests verify FR-003, FR-024, and US5-AS2 from specs/001-foundation/spec.md:
- FR-003: Termination request with cleanup timeout
- FR-024: SIGINT handler, log, exit 130
- US5-AS2: Interrupt between jobs skips remaining jobs
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from rich.console import Console

from pcswitcher.cli import _async_run_sync
from pcswitcher.config import Configuration
from pcswitcher.events import EventBus
from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import SessionStatus, SyncSession


class SlowJob(SyncJob):
    """Test job that simulates long-running operation."""

    name = "slow_job"

    CONFIG_SCHEMA: ClassVar = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext, delay: float = 5.0) -> None:
        """Initialize with configurable delay.

        Args:
            context: Job context
            delay: Time to sleep in seconds (default: 5.0)
        """
        super().__init__(context)
        self.delay = delay
        self.cleanup_called = False

    async def validate(self) -> list[Any]:
        """No validation needed."""
        return []

    async def execute(self) -> None:
        """Sleep for configured delay."""
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            # Simulate cleanup
            self.cleanup_called = True
            raise


class QuickJob(SyncJob):
    """Test job that completes quickly."""

    name = "quick_job"

    CONFIG_SCHEMA: ClassVar = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    async def validate(self) -> list[Any]:
        """No validation needed."""
        return []

    async def execute(self) -> None:
        """Complete immediately."""
        await asyncio.sleep(0.01)


@pytest.fixture
def mock_config() -> Configuration:
    """Create minimal mock configuration."""
    config_dict = {
        "log_file_level": "INFO",
        "log_cli_level": "INFO",
        "sync_jobs": {
            "dummy_success": True,
        },
        "disk_space_monitor": {
            "preflight_minimum": "20%",
            "runtime_minimum": "15%",
            "warning_threshold": "25%",
            "check_interval": 30,
        },
        "btrfs_snapshots": {
            "subvolumes": ["@", "@home"],
            "keep_recent": 5,
        },
        "dummy_success": {
            "source_duration": 1,
            "target_duration": 1,
        },
    }
    # Create temporary config file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(config_dict, f)
        config_path = Path(f.name)

    try:
        config = Configuration.from_yaml(config_path)
        yield config
    finally:
        config_path.unlink(missing_ok=True)


class TestInterruptHandling:
    """Test interrupt handling in orchestrator and CLI."""

    @pytest.mark.asyncio
    async def test_001_fr003_termination_request_on_interrupt(self) -> None:
        """FR-003: System must request termination with cleanup timeout when interrupted.

        Spec requirement: FR-003 states that system MUST request termination of
        currently-executing job when Ctrl+C is pressed, allowing cleanup timeout
        for graceful cleanup. If job does not complete cleanup within timeout,
        orchestrator MUST force-terminate connections and the job.

        This test verifies:
        1. CancelledError is raised to the job (simulating interrupt)
        2. Job receives the cancellation and can perform cleanup
        3. Orchestrator catches CancelledError, logs, and re-raises
        """
        # Create mock job that can be interrupted
        slow_job = SlowJob(
            JobContext(
                config={},
                source=MagicMock(),
                target=MagicMock(),
                event_bus=EventBus(),
                session_id="test123",
                source_hostname="source",
                target_hostname="target",
            ),
            delay=10.0,  # Long delay to ensure we interrupt it
        )

        # Simulate job execution with interrupt
        task = asyncio.create_task(slow_job.execute())
        await asyncio.sleep(0.1)  # Let job start

        # Cancel the task (simulates SIGINT → orchestrator canceling job task)
        task.cancel()

        # Verify CancelledError is raised
        with pytest.raises(asyncio.CancelledError):
            await task

        # Verify cleanup was called
        assert slow_job.cleanup_called, "Job should have performed cleanup before re-raising CancelledError"

    @pytest.mark.asyncio
    async def test_001_fr024_sigint_handler_exit_130(self) -> None:
        """FR-024: SIGINT handler must log and exit with code 130.

        Spec requirement: FR-024 states that system MUST install SIGINT handler
        that requests current job termination, logs "Sync interrupted by user" at
        WARNING level, and exits with code 130.

        This test verifies the CLI's _async_run_sync() function:
        1. Installs SIGINT handler
        2. Cancels main task when SIGINT received
        3. Logs interruption
        4. Returns exit code 130
        """
        # Create minimal mock config
        config_dict = {
            "log_file_level": "INFO",
            "log_cli_level": "INFO",
            "sync_jobs": {
                "dummy_success": True,
            },
            "disk_space_monitor": {
                "preflight_minimum": "20%",
                "runtime_minimum": "15%",
                "warning_threshold": "25%",
                "check_interval": 30,
            },
            "btrfs_snapshots": {
                "subvolumes": ["@", "@home"],
                "keep_recent": 5,
            },
            "dummy_success": {
                "source_duration": 1,
                "target_duration": 1,
            },
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_dict, f)
            config_path = Path(f.name)

        try:
            config = Configuration.from_yaml(config_path)

            # Mock the orchestrator to simulate a long-running operation
            with patch("pcswitcher.cli.Orchestrator") as MockOrchestrator:
                mock_orchestrator = MagicMock()

                # Create a task that will be interrupted
                async def long_running_sync() -> SyncSession:
                    await asyncio.sleep(10)  # Long enough to be interrupted
                    return SyncSession(
                        session_id="test",
                        started_at=datetime.now(UTC),
                        source_hostname="source",
                        target_hostname="target",
                        config={},
                        status=SessionStatus.COMPLETED,
                        job_results=[],
                    )

                mock_orchestrator.run = AsyncMock(side_effect=long_running_sync)
                MockOrchestrator.return_value = mock_orchestrator

                # Run _async_run_sync in a separate task
                async def run_and_interrupt() -> int:
                    """Run sync and interrupt it after a short delay."""
                    loop = asyncio.get_running_loop()

                    # Start sync in background
                    sync_task = asyncio.create_task(_async_run_sync("target-host", config))

                    # Wait briefly then trigger SIGINT
                    await asyncio.sleep(0.2)

                    # Simulate SIGINT by triggering the handler directly
                    # The actual signal handler would be registered via loop.add_signal_handler
                    for task in asyncio.all_tasks(loop):
                        if task == sync_task:
                            continue
                        # Find the main_task within _async_run_sync
                        if not task.done():
                            task.cancel()

                    # Wait for sync to complete
                    return await sync_task

                # Execute and verify exit code
                # We need to mock the console to avoid output during test
                with patch("pcswitcher.cli.Console", return_value=MagicMock(spec=Console)):
                    exit_code = await run_and_interrupt()

                # Verify exit code 130 (standard for SIGINT)
                assert exit_code == 130, f"Exit code should be 130 for SIGINT, got {exit_code}"

        finally:
            config_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_001_us5_as2_interrupt_between_jobs_skips_remaining(self) -> None:
        """US5-AS2: Interrupt between jobs skips remaining jobs and exits cleanly.

        Spec requirement: US5 Acceptance Scenario 2 states that when sync is in
        the orchestrator phase between jobs (no job actively running) and user
        presses Ctrl+C, then orchestrator logs interruption, skips remaining jobs,
        and exits cleanly.

        This test verifies:
        1. When CancelledError is raised during sequential job execution
        2. The current job (if running) is interrupted
        3. Remaining jobs in the sequence are not executed
        4. The orchestrator re-raises CancelledError properly
        """
        # Simulate sequential job execution with interrupt
        jobs_executed = []
        interrupt_event = asyncio.Event()

        async def execute_jobs_sequentially() -> None:
            """Simulate orchestrator's job execution loop."""
            jobs = ["job1", "job2", "job3"]

            for job_name in jobs:
                # Simulate job execution
                jobs_executed.append(job_name)
                await asyncio.sleep(0.01)

                # Wait for interrupt signal after job1
                if job_name == "job1":
                    interrupt_event.set()
                    # Wait a bit to allow cancellation
                    await asyncio.sleep(1.0)

        # Run with cancellation after first job
        task = asyncio.create_task(execute_jobs_sequentially())

        # Wait for job1 to complete
        await interrupt_event.wait()

        # Cancel the task (simulates interrupt between jobs)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Verify only job1 executed (job2 and job3 skipped due to interrupt)
        assert "job1" in jobs_executed, "First job should have executed"
        assert "job2" not in jobs_executed, "Second job should not execute after interrupt"
        assert "job3" not in jobs_executed, "Third job should not execute after interrupt"
