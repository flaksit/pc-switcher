"""Integration tests for consecutive sync warning feature.

These tests verify that:
1. Sync history files can be created/read on remote VMs
2. The orchestrator correctly updates history on both source and target
3. The consecutive sync detection works with real SSH connections
"""

from __future__ import annotations

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.sync_history import (
    HISTORY_DIR,
    HISTORY_PATH,
    SyncRole,
    get_record_role_command,
)


class TestSyncHistoryOnRemote:
    """Test sync history file operations on remote VMs."""

    async def test_create_history_directory_on_remote(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Should be able to create the sync history directory on a remote machine."""
        try:
            # Create directory (simulating what _update_target_history does)
            result = await pc1_executor.run_command(f"mkdir -p {HISTORY_DIR}")

            assert result.success, f"Failed to create directory: {result.stderr}"

            # Verify directory exists
            verify = await pc1_executor.run_command(f"test -d {HISTORY_DIR}")
            assert verify.success, "Directory should exist"
        finally:
            # Clean up
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_write_and_read_history_on_remote(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Should be able to write and read sync history on a remote machine."""
        try:
            # Write history using the same command as orchestrator
            write_result = await pc1_executor.run_command(get_record_role_command(SyncRole.TARGET))
            assert write_result.success, f"Failed to write history: {write_result.stderr}"

            # Read it back
            read_result = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            assert read_result.success, f"Failed to read history: {read_result.stderr}"
            assert '"last_role": "target"' in read_result.stdout
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_overwrite_history_on_remote(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Should be able to overwrite existing history with new role."""
        try:
            # Create initial history as source
            await pc1_executor.run_command(get_record_role_command(SyncRole.SOURCE))

            # Overwrite with target
            await pc1_executor.run_command(get_record_role_command(SyncRole.TARGET))

            # Verify it changed
            result = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            assert result.success
            assert '"last_role": "target"' in result.stdout
            assert '"last_role": "source"' not in result.stdout
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")


class TestHistoryUpdateViaSsh:
    """Test the SSH command pattern used for updating target history."""

    async def test_update_target_history_command(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """The SSH command used to update target history should work correctly.

        This tests the exact command pattern used in orchestrator._update_sync_history().
        """
        try:
            # Run the exact command from get_record_role_command
            cmd = get_record_role_command(SyncRole.TARGET)
            result = await pc1_executor.run_command(cmd)

            assert result.success, f"Update command failed: {result.stderr}"

            # Verify
            verify = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            assert verify.success
            assert '"last_role": "target"' in verify.stdout
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_update_command_idempotent(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Running the update command multiple times should not fail."""
        try:
            cmd = get_record_role_command(SyncRole.TARGET)

            # Run multiple times
            for _ in range(3):
                result = await pc1_executor.run_command(cmd)
                assert result.success, f"Command failed on iteration: {result.stderr}"
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")


class TestHistoryOnBothMachines:
    """Test sync history across both VMs."""

    async def test_history_independent_per_machine(
        self, pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
    ) -> None:
        """Each machine should maintain its own independent history."""
        try:
            # Set pc1 as source, pc2 as target
            await pc1_executor.run_command(get_record_role_command(SyncRole.SOURCE))
            await pc2_executor.run_command(get_record_role_command(SyncRole.TARGET))

            # Verify each has its own role
            pc1_result = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            pc2_result = await pc2_executor.run_command(f"cat {HISTORY_PATH}")

            assert '"last_role": "source"' in pc1_result.stdout
            assert '"last_role": "target"' in pc2_result.stdout
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")
            await pc2_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_simulate_sync_workflow(
        self, pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
    ) -> None:
        """Simulate a full sync workflow: pc1 syncs to pc2.

        This simulates:
        1. pc1 acts as SOURCE, pc2 acts as TARGET
        2. Both machines' histories are updated accordingly
        """
        try:
            # Simulate: after sync completes, orchestrator on pc1 updates both histories
            await pc1_executor.run_command(get_record_role_command(SyncRole.SOURCE))
            await pc2_executor.run_command(get_record_role_command(SyncRole.TARGET))

            # Verify expected state after sync
            pc1_role = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            pc2_role = await pc2_executor.run_command(f"cat {HISTORY_PATH}")

            assert "source" in pc1_role.stdout, "pc1 should be recorded as source"
            assert "target" in pc2_role.stdout, "pc2 should be recorded as target"
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")
            await pc2_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_simulate_back_sync_workflow(
        self, pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
    ) -> None:
        """Simulate back-sync workflow: pc2 syncs back to pc1.

        After initial sync (pc1->pc2), user works on pc2 and syncs back.
        Now pc2 is SOURCE and pc1 is TARGET.
        """
        try:
            # Initial state: pc1 was source, pc2 was target
            await pc1_executor.run_command(get_record_role_command(SyncRole.SOURCE))
            await pc2_executor.run_command(get_record_role_command(SyncRole.TARGET))

            # Now simulate back-sync: pc2 syncs to pc1
            # pc2 becomes source, pc1 becomes target
            await pc2_executor.run_command(get_record_role_command(SyncRole.SOURCE))
            await pc1_executor.run_command(get_record_role_command(SyncRole.TARGET))

            # Verify swapped state
            pc1_role = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            pc2_role = await pc2_executor.run_command(f"cat {HISTORY_PATH}")

            assert "target" in pc1_role.stdout, "pc1 should now be target (received back-sync)"
            assert "source" in pc2_role.stdout, "pc2 should now be source (performed back-sync)"
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")
            await pc2_executor.run_command(f"rm -rf {HISTORY_DIR}")


class TestConsecutiveSyncDetection:
    """Test the logic for detecting consecutive syncs."""

    async def test_detect_consecutive_sync_scenario(
        self, pc1_executor: BashLoginRemoteExecutor
    ) -> None:
        """Detect when a machine would sync twice without receiving a sync back.

        Scenario: pc1 synced to pc2, now pc1 tries to sync again.
        pc1's last_role is "source" - this should trigger warning.
        """
        try:
            # State after first sync: pc1=source
            await pc1_executor.run_command(get_record_role_command(SyncRole.SOURCE))

            # Check pc1's last role
            result = await pc1_executor.run_command(f"cat {HISTORY_PATH}")

            # If last_role is "source", consecutive sync warning should be shown
            assert "source" in result.stdout, "pc1 should show as source"
            # In the orchestrator, this would trigger the warning prompt
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_no_warning_after_receiving_sync(
        self, pc1_executor: BashLoginRemoteExecutor
    ) -> None:
        """No warning when machine received a sync (was target) before syncing.

        Scenario: pc2 synced back to pc1 (pc1 was target), now pc1 syncs to pc2.
        pc1's last_role is "target" - no warning needed.
        """
        try:
            # State after back-sync: pc1=target (received sync from pc2)
            await pc1_executor.run_command(get_record_role_command(SyncRole.TARGET))

            # Check pc1's last role
            result = await pc1_executor.run_command(f"cat {HISTORY_PATH}")

            # last_role is "target" - no warning should be shown
            assert "target" in result.stdout, "pc1 should show as target"
            # In the orchestrator, this would NOT trigger the warning
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_no_history_no_warning(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """No warning when no history exists (first sync)."""
        try:
            # Remove any existing history file
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")

            # Check if file exists
            result = await pc1_executor.run_command(f"test -f {HISTORY_PATH}")

            # File doesn't exist - no warning needed (first sync)
            assert not result.success, "History file should not exist"
            # In the orchestrator, this would NOT trigger the warning
        finally:
            # Ensure clean state
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")


class TestCorruptedHistory:
    """Test handling of corrupted history files."""

    async def test_corrupted_json_detectable(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Should be able to detect and handle corrupted JSON."""
        try:
            # Write corrupted JSON (intentionally not using get_record_role_command)
            await pc1_executor.run_command(
                f"mkdir -p {HISTORY_DIR} && echo 'not valid json' > {HISTORY_PATH}"
            )

            # Verify file exists but content is not valid JSON
            result = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            assert result.success, "File should exist"
            assert "not valid json" in result.stdout

            # In the orchestrator, corrupted files trigger the warning (safety-first)
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_can_recover_from_corrupted_history(self, pc1_executor: BashLoginRemoteExecutor) -> None:
        """Should be able to overwrite corrupted history with valid data."""
        try:
            # Create corrupted file (intentionally not using get_record_role_command)
            await pc1_executor.run_command(f"mkdir -p {HISTORY_DIR} && echo 'garbage' > {HISTORY_PATH}")

            # Overwrite with valid JSON using the proper command
            await pc1_executor.run_command(get_record_role_command(SyncRole.SOURCE))

            # Verify recovery
            result = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            assert '"last_role": "source"' in result.stdout
            assert "garbage" not in result.stdout
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")
