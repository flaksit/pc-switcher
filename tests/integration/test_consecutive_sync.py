"""Integration tests for consecutive sync warning feature.

These tests verify that the shell commands used to update sync history
work correctly on real remote machines via SSH.
"""

from __future__ import annotations

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.sync_history import (
    HISTORY_DIR,
    HISTORY_PATH,
    SyncRole,
    get_record_role_command,
)


class TestSyncHistoryCommands:
    """Test that sync history shell commands work on remote machines."""

    async def test_record_role_command_works_on_remote(
        self, pc1_executor: BashLoginRemoteExecutor
    ) -> None:
        """The get_record_role_command output should work on a real remote machine."""
        try:
            # Write history using the command the orchestrator uses
            cmd = get_record_role_command(SyncRole.TARGET)
            result = await pc1_executor.run_command(cmd)
            assert result.success, f"Command failed: {result.stderr}"

            # Verify the file contains valid JSON with correct role
            verify = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            assert verify.success
            assert '"last_role": "target"' in verify.stdout
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")

    async def test_role_can_be_updated(
        self, pc1_executor: BashLoginRemoteExecutor
    ) -> None:
        """A machine's role can be changed (supports the back-sync workflow)."""
        try:
            # Initially set as source
            await pc1_executor.run_command(get_record_role_command(SyncRole.SOURCE))

            # Change to target (simulating receiving a back-sync)
            await pc1_executor.run_command(get_record_role_command(SyncRole.TARGET))

            # Verify it changed
            result = await pc1_executor.run_command(f"cat {HISTORY_PATH}")
            assert '"last_role": "target"' in result.stdout
            assert '"last_role": "source"' not in result.stdout
        finally:
            await pc1_executor.run_command(f"rm -rf {HISTORY_DIR}")
