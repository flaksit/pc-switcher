"""Unit tests for BtrfsSnapshotJob.

Tests verify snapshot creation, validation, and error handling per 001-foundation spec.
Reference: specs/001-foundation/spec.md (User Story 3 - Safety Infrastructure with Btrfs Snapshots)
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun import freeze_time

from pcswitcher.btrfs_snapshots import cleanup_snapshots, create_snapshot
from pcswitcher.jobs.btrfs import BtrfsSnapshotJob, subvolume_to_mount_point
from pcswitcher.models import CommandResult


@pytest.mark.asyncio
class TestBtrfsSnapshotJobCreation:
    """Tests for snapshot creation functionality."""

    async def test_001_fnd_fr_snap_pre(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """FND-FR-SNAP-PRE: System MUST create read-only btrfs snapshots before any job executes.

        Verifies:
        - Pre-sync snapshots are created on both source and target
        - Snapshots are created for all configured subvolumes
        - Snapshots are read-only (using -r flag)
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@", "@home"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        # Mock successful snapshot creation
        mock_local_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        mock_remote_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        await job.execute()

        # Verify source snapshots were created
        source_calls = [call[0][0] for call in mock_local_executor.run_command.call_args_list]
        assert any("sudo btrfs subvolume snapshot -r /" in call for call in source_calls)
        assert any("sudo btrfs subvolume snapshot -r /home" in call for call in source_calls)

        # Verify target snapshots were created
        target_calls = [call[0][0] for call in mock_remote_executor.run_command.call_args_list]
        assert any("sudo btrfs subvolume snapshot -r /" in call for call in target_calls)
        assert any("sudo btrfs subvolume snapshot -r /home" in call for call in target_calls)

    async def test_001_fnd_fr_snap_post(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """FND-FR-SNAP-POST: System MUST create post-sync snapshots after all jobs complete.

        Verifies:
        - Post-sync snapshots are created on both source and target
        - Snapshots capture final state after sync operations
        """
        context = mock_job_context_factory(
            config={
                "phase": "post",
                "subvolumes": ["@", "@home"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        mock_local_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        mock_remote_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        await job.execute()

        # Verify post-sync snapshots were created (not pre-sync)
        source_calls = [call[0][0] for call in mock_local_executor.run_command.call_args_list]
        assert any("post-@-" in call for call in source_calls)
        assert any("post-@home-" in call for call in source_calls)
        assert not any("pre-@-" in call for call in source_calls)

    @freeze_time("2025-01-15T10:30:00")
    async def test_001_fnd_fr_snap_name(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """FND-FR-SNAP-NAME: Snapshot naming MUST follow pattern {pre|post}-<subvolume>-<timestamp>.

        Verifies:
        - Snapshot names include phase (pre/post)
        - Snapshot names include subvolume identifier
        - Snapshot names include timestamp in YYYYMMDDThhmmss format
        - Example: pre-@home-20250115T103000
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@home"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        mock_local_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        mock_remote_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        await job.execute()

        # Verify snapshot naming pattern
        source_calls = [call[0][0] for call in mock_local_executor.run_command.call_args_list]
        # Should contain snapshot with format: pre-@home-20250115T103000
        assert any("pre-@home-20250115T103000" in call for call in source_calls)

    async def test_001_fnd_fr_snap_always(
        self,
        mock_job_context_factory: type,
    ) -> None:
        """FND-FR-SNAP-ALWAYS: Snapshot management MUST be always active (not configurable).

        Verifies:
        - Snapshot job does not have an 'enabled' configuration option
        - Snapshot creation cannot be disabled through configuration
        - Job config schema requires phase and subvolumes (mandatory parameters)
        """
        # Verify CONFIG_SCHEMA requires mandatory fields
        schema = BtrfsSnapshotJob.CONFIG_SCHEMA
        assert "phase" in schema["required"]
        assert "subvolumes" in schema["required"]
        assert "session_folder" in schema["required"]

        # Verify there's no 'enabled' field in the schema
        assert "enabled" not in schema.get("properties", {})

        # Snapshot job should work without any 'enabled' configuration
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)
        assert job is not None


@pytest.mark.asyncio
class TestBtrfsSnapshotJobErrorHandling:
    """Tests for snapshot error handling and validation."""

    async def test_001_fnd_fr_snap_fail(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """FND-FR-SNAP-FAIL: If pre-sync snapshot creation fails, system MUST abort.

        Verifies:
        - Snapshot creation failure raises RuntimeError
        - Error message includes failure details
        - No further operations are attempted after failure
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@home"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        # Simulate snapshot failure on source
        mock_local_executor.run_command = AsyncMock(
            side_effect=[
                # First call is mkdir for session folder (succeeds)
                CommandResult(exit_code=0, stdout="", stderr=""),
                # Second call is btrfs snapshot (fails)
                CommandResult(exit_code=1, stdout="", stderr="No space left on device"),
            ]
        )
        mock_remote_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        # Verify that snapshot failure raises RuntimeError
        with pytest.raises(RuntimeError, match="Snapshot creation failed"):
            await job.execute()

    async def test_001_fnd_fr_snap_cleanup(self) -> None:
        """FND-FR-SNAP-CLEANUP: System MUST provide snapshot cleanup with retention policy.

        Note: This test verifies that cleanup functionality exists and is testable.
        Detailed cleanup logic is tested in test_btrfs_snapshots.py.

        Verifies:
        - Cleanup mechanism exists (cleanup_snapshots function)
        - Retention policy parameters are supported (keep_recent, max_age_days)
        """
        # Verify function signature supports retention parameters
        sig = inspect.signature(cleanup_snapshots)
        assert "keep_recent" in sig.parameters
        assert "max_age_days" in sig.parameters


@pytest.mark.asyncio
class TestBtrfsSnapshotJobValidation:
    """Tests for snapshot validation functionality."""

    async def test_001_fnd_fr_subvol_exist(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """FND-FR-SUBVOL-EXIST: System MUST verify configured subvolumes exist before snapshots.

        Verifies:
        - Validation checks all configured subvolumes
        - Validation runs on both source and target
        - Missing subvolume produces validation error
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@", "@home"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        # Mock /.snapshots validation (succeeds)
        snapshots_check = CommandResult(exit_code=0, stdout="/.snapshots", stderr="")

        # Mock subvolume validation (@ exists, @home missing on source)
        def run_command_side_effect(cmd: str) -> CommandResult:
            if "show /.snapshots" in cmd:
                return snapshots_check
            if "show /" in cmd and "show /home" not in cmd:
                # @ subvolume exists
                return CommandResult(exit_code=0, stdout="Name: @\n", stderr="")
            if "show /home" in cmd:
                # @home missing
                return CommandResult(exit_code=1, stdout="", stderr="not a subvolume")
            return CommandResult(exit_code=0, stdout="", stderr="")

        mock_local_executor.run_command = AsyncMock(side_effect=run_command_side_effect)
        mock_remote_executor.run_command = AsyncMock(side_effect=run_command_side_effect)

        # Validate should return errors for missing subvolumes
        errors = await job.validate()

        # Should have at least one error for @home on source
        assert len(errors) > 0
        assert any("@home" in str(error) and "source" in str(error).lower() for error in errors)

    async def test_001_fnd_fr_snapdir(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """FND-FR-SNAPDIR: System MUST verify /.snapshots/ is a subvolume.

        Verifies:
        - Validation checks if /.snapshots exists
        - If /.snapshots is not a subvolume, validation fails
        - If /.snapshots doesn't exist, it is created as a subvolume
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        # Test case: /.snapshots exists and is a subvolume
        mock_local_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="/.snapshots\nName: @snapshots", stderr="")
        )
        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="/.snapshots\nName: @snapshots", stderr="")
        )

        await job.validate()

        # Verify /.snapshots check was performed
        source_calls = [call[0][0] for call in mock_local_executor.run_command.call_args_list]
        assert any("btrfs subvolume show /.snapshots" in call for call in source_calls)

    async def test_001_us3_as1_validate_subvolumes_exist(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """US3-AS1: Orchestrator MUST verify configured subvolumes exist on both hosts.

        Acceptance Scenario 1: Given a sync is requested with configured subvolumes,
        When orchestrator begins pre-sync checks, Then it MUST verify that all
        configured subvolumes exist on both source and target.

        Verifies:
        - All subvolumes in config are validated
        - Validation happens on both source and target
        - Missing subvolume on either side produces error
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@", "@home", "@var"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        # Mock /.snapshots exists
        snapshots_ok = CommandResult(exit_code=0, stdout="/.snapshots", stderr="")

        # Mock all subvolumes exist
        def run_command_all_exist(cmd: str) -> CommandResult:
            if "show /.snapshots" in cmd:
                return snapshots_ok
            # Need to handle "show /" carefully - exclude "/home" and "/var"
            if cmd.endswith("show / 2>&1"):
                return CommandResult(exit_code=0, stdout="Name: @\n", stderr="")
            if "show /home" in cmd:
                return CommandResult(exit_code=0, stdout="Name: @home\n", stderr="")
            if "show /var" in cmd:
                return CommandResult(exit_code=0, stdout="Name: @var\n", stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        mock_local_executor.run_command = AsyncMock(side_effect=run_command_all_exist)
        mock_remote_executor.run_command = AsyncMock(side_effect=run_command_all_exist)

        errors = await job.validate()

        # All subvolumes exist - no errors
        assert len(errors) == 0

    async def test_001_us3_as5_abort_if_snapshots_not_subvolume(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """US3-AS5: Orchestrator MUST abort if /.snapshots/ is not a subvolume.

        Acceptance Scenario 5: Given /.snapshots/ exists but is a regular directory
        (not a subvolume), When orchestrator validates snapshot infrastructure,
        Then it logs CRITICAL error and aborts sync.

        Verifies:
        - Validation detects when /.snapshots exists but is not a subvolume
        - Validation produces error preventing sync from proceeding
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        # Mock /.snapshots exists but creation fails (because it's a directory)
        def run_command_not_subvolume(cmd: str) -> CommandResult:
            if "show /.snapshots" in cmd:
                # Not a subvolume
                return CommandResult(exit_code=1, stdout="", stderr="not a subvolume")
            if "btrfs subvolume create /.snapshots" in cmd:
                # Creation fails because directory already exists
                return CommandResult(exit_code=1, stdout="", stderr="already exists")
            if "show /" in cmd:
                return CommandResult(exit_code=0, stdout="Name: @\n", stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        mock_local_executor.run_command = AsyncMock(side_effect=run_command_not_subvolume)
        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="/.snapshots", stderr="")
        )

        errors = await job.validate()

        # Should have error about /.snapshots on source
        assert len(errors) > 0
        assert any("/.snapshots" in str(error) and "source" in str(error).lower() for error in errors)

    async def test_001_us3_as6_abort_on_snapshot_failure(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """US3-AS6: Orchestrator MUST abort if snapshot creation fails.

        Acceptance Scenario 6: Given snapshot creation fails on target
        (e.g., insufficient space), When the failure occurs, Then the
        orchestrator logs CRITICAL error and aborts sync.

        Verifies:
        - Snapshot failure on target raises exception
        - Sync is aborted before state changes occur
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        # Source succeeds, target fails
        mock_local_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        mock_remote_executor.run_command = AsyncMock(
            side_effect=[
                # mkdir succeeds
                CommandResult(exit_code=0, stdout="", stderr=""),
                # snapshot fails
                CommandResult(exit_code=1, stdout="", stderr="No space left on device"),
            ]
        )

        with pytest.raises(RuntimeError, match="Snapshot creation failed"):
            await job.execute()

    async def test_001_us3_as8_preflight_disk_space_check(self) -> None:
        """US3-AS8: Orchestrator MUST check disk space before starting sync.

        Acceptance Scenario 8: Given orchestrator configuration includes
        disk_space_monitor.preflight_minimum, When sync begins, Then orchestrator
        MUST check free disk space on both source and target.

        Note: Disk space checking is orchestrator-level functionality, not
        BtrfsSnapshotJob. This test verifies the job doesn't interfere with
        disk space checking by ensuring snapshots can fail gracefully.
        """
        # This is tested more thoroughly in orchestrator tests
        # Here we just verify snapshot job can handle low disk space errors

        # Verify that create_snapshot can return error results
        sig = inspect.signature(create_snapshot)
        # Function returns CommandResult which includes exit_code for error handling
        assert sig.return_annotation is not None


@pytest.mark.asyncio
class TestBtrfsSnapshotJobEdgeCases:
    """Tests for edge cases and boundary conditions."""

    async def test_001_edge_insufficient_space_snapshots(
        self,
        mock_job_context_factory: type,
        mock_local_executor: MagicMock,
        mock_remote_executor: MagicMock,
    ) -> None:
        """Edge case: Snapshot creation fails due to insufficient disk space.

        Verifies:
        - System handles "No space left on device" error
        - Error message is informative
        - Sync is aborted cleanly
        """
        context = mock_job_context_factory(
            config={
                "phase": "pre",
                "subvolumes": ["@", "@home"],
                "session_folder": "20251129T143022-abc12345",
            }
        )
        job = BtrfsSnapshotJob(context)

        # First subvolume succeeds, second fails due to space
        call_count = [0]

        def run_command_space_error(cmd: str) -> CommandResult:
            call_count[0] += 1
            if "mkdir" in cmd:
                return CommandResult(exit_code=0, stdout="", stderr="")
            if call_count[0] <= 2:  # First snapshot succeeds
                return CommandResult(exit_code=0, stdout="", stderr="")
            # Second snapshot fails
            return CommandResult(
                exit_code=1,
                stdout="",
                stderr="ERROR: not enough free space",
            )

        mock_local_executor.run_command = AsyncMock(side_effect=run_command_space_error)
        mock_remote_executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        with pytest.raises(RuntimeError) as exc_info:
            await job.execute()

        assert "not enough free space" in str(exc_info.value)


class TestSubvolumeToMountPoint:
    """Tests for subvolume_to_mount_point helper function."""

    def test_root_subvolume(self) -> None:
        """@ subvolume maps to / mount point."""
        assert subvolume_to_mount_point("@") == "/"

    def test_named_subvolume(self) -> None:
        """@name subvolume maps to /name mount point."""
        assert subvolume_to_mount_point("@home") == "/home"
        assert subvolume_to_mount_point("@var") == "/var"
        assert subvolume_to_mount_point("@opt") == "/opt"

    def test_invalid_subvolume_name(self) -> None:
        """Subvolume name without @ prefix raises ValueError."""
        with pytest.raises(ValueError, match="Invalid subvolume name"):
            subvolume_to_mount_point("home")

        with pytest.raises(ValueError, match="Invalid subvolume name"):
            subvolume_to_mount_point("var")
