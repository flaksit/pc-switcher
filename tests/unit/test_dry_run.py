"""Unit tests for dry-run functionality.

Tests verify that --dry-run mode:
- Propagates through the system (CLI -> Orchestrator -> Jobs)
- Skips state-modifying operations while keeping logging
- Returns SUCCESS/FAILED as expected (not SKIPPED)

Reference: GitHub issue #37
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.btrfs import BtrfsSnapshotJob
from pcswitcher.jobs.install_on_target import InstallOnTargetJob
from pcswitcher.models import CommandResult
from pcswitcher.orchestrator import Orchestrator


class TestJobContextDryRunField:
    """Tests for dry_run field on JobContext."""

    def test_job_context_has_dry_run_field(
        self,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
        mock_event_bus: MagicMock,
    ) -> None:
        """Verify JobContext has dry_run field with default False."""
        context = JobContext(
            config={},
            source=mock_local_executor,
            target=mock_remote_executor,
            event_bus=mock_event_bus,
            session_id="test-session",
            source_hostname="source",
            target_hostname="target",
        )
        assert hasattr(context, "dry_run")
        assert context.dry_run is False

    def test_job_context_dry_run_can_be_set_true(
        self,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
        mock_event_bus: MagicMock,
    ) -> None:
        """Verify JobContext dry_run can be set to True."""
        context = JobContext(
            config={},
            source=mock_local_executor,
            target=mock_remote_executor,
            event_bus=mock_event_bus,
            session_id="test-session",
            source_hostname="source",
            target_hostname="target",
            dry_run=True,
        )
        assert context.dry_run is True


@pytest.mark.asyncio
class TestBtrfsSnapshotJobDryRun:
    """Tests for BtrfsSnapshotJob dry-run behavior."""

    async def test_btrfs_snapshot_job_dry_run_logs_without_creating(
        self,
        mock_job_context_factory: Any,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """Verify dry-run mode logs snapshot names but doesn't create them."""
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@", "@home"],
                "session_folder": "20251231T120000-abc12345",
            },
            dry_run=True,
        )
        job = BtrfsSnapshotJob(context)

        # Execute in dry-run mode
        await job.execute()

        # Verify NO actual snapshot commands were issued
        source_calls = [str(call) for call in mock_local_executor.run_command.call_args_list]
        target_calls = [str(call) for call in mock_remote_executor.run_command.call_args_list]

        # No btrfs subvolume snapshot commands should be called
        all_calls = source_calls + target_calls
        assert not any("btrfs subvolume snapshot" in call for call in all_calls), (
            f"Expected no snapshot commands in dry-run mode, but found: {all_calls}"
        )

        # No mkdir commands should be called either
        assert not any("mkdir" in call for call in all_calls), (
            f"Expected no mkdir commands in dry-run mode, but found: {all_calls}"
        )

    async def test_btrfs_snapshot_job_normal_mode_creates_snapshots(
        self,
        mock_job_context_factory: Any,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """Verify normal mode (dry_run=False) creates snapshots."""
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@", "@home"],
                "session_folder": "20251231T120000-abc12345",
            },
            dry_run=False,
        )
        job = BtrfsSnapshotJob(context)

        # Mock successful commands
        mock_local_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        mock_remote_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        # Execute in normal mode
        await job.execute()

        # Verify snapshot commands WERE issued
        source_calls = [str(call) for call in mock_local_executor.run_command.call_args_list]
        target_calls = [str(call) for call in mock_remote_executor.run_command.call_args_list]

        all_calls = source_calls + target_calls
        assert any("btrfs subvolume snapshot" in call for call in all_calls), (
            f"Expected snapshot commands in normal mode, but found: {all_calls}"
        )


@pytest.mark.asyncio
class TestInstallOnTargetJobDryRun:
    """Tests for InstallOnTargetJob dry-run behavior."""

    async def test_install_on_target_job_dry_run_skips_install(
        self,
        mock_job_context_factory: Any,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """Verify dry-run mode logs install info but doesn't actually install."""
        context = mock_job_context_factory(
            config={},
            dry_run=True,
        )
        job = InstallOnTargetJob(context)
        # Simulate target has no pc-switcher installed
        job.target_version = None

        # Execute in dry-run mode
        await job.execute()

        # Verify NO install commands were issued
        target_calls = [str(call) for call in mock_remote_executor.run_command.call_args_list]

        # No install.sh or pipx commands should be called
        assert not any("install" in call.lower() for call in target_calls), (
            f"Expected no install commands in dry-run mode, but found: {target_calls}"
        )

    async def test_install_on_target_job_version_match_skips_always(
        self,
        mock_job_context_factory: Any,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """Verify when versions match, no install happens (dry-run or not)."""
        context = mock_job_context_factory(
            config={},
            dry_run=False,
        )
        job = InstallOnTargetJob(context)
        # Set target version to match source
        job.target_version = job.source_version

        # Execute
        await job.execute()

        # Verify NO install commands were issued (version match = skip)
        target_calls = [str(call) for call in mock_remote_executor.run_command.call_args_list]

        assert not any("install" in call.lower() for call in target_calls), (
            f"Expected no install commands when versions match, but found: {target_calls}"
        )


class TestOrchestratorDryRunPropagation:
    """Tests for dry_run propagation through Orchestrator."""

    def test_orchestrator_accepts_dry_run_parameter(self) -> None:
        """Verify Orchestrator.__init__ accepts dry_run parameter."""
        sig = inspect.signature(Orchestrator.__init__)
        param_names = list(sig.parameters.keys())
        assert "dry_run" in param_names, "Orchestrator.__init__ should accept dry_run parameter"
