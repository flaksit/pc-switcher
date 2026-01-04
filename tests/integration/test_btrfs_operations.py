"""Integration tests for btrfs snapshot operations.

Tests real btrfs filesystem operations including:
- Snapshot data integrity (content preservation)
- Multiple snapshot isolation
- Failure paths for invalid operations

Note: Basic snapshot creation, listing, and deletion are tested in
test_snapshot_infrastructure.py with the actual pc-switcher snapshot functions.
This file focuses on unique behavior tests not covered elsewhere.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pcswitcher.executor import RemoteExecutor


@pytest.fixture(scope="module")
async def test_volume(pc1_executor: RemoteExecutor) -> AsyncIterator[str]:
    """Isolated btrfs subvolume for snapshot tests.

    Creates /test-vol as a clean sandbox for btrfs operations testing.
    Cleans up any leftover state from previous runs before creating.

    Module-scoped: shared across all tests in this module for efficiency.
    Individual tests must clean up their own artifacts (snapshots) in try/finally.
    """
    # Clean slate: remove any leftover from crashed previous run
    await pc1_executor.run_command(
        "sudo sh -c '"
        "btrfs subvolume list -o /test-vol 2>/dev/null | "
        'awk "{print \\$NF}" | '
        "xargs -r -I {} btrfs subvolume delete /{}"
        "'",
    )
    await pc1_executor.run_command("sudo btrfs subvolume delete /test-vol 2>/dev/null || true")

    # Create fresh test subvolume
    result = await pc1_executor.run_command("sudo btrfs subvolume create /test-vol")
    assert result.success, f"Failed to create test volume: {result.stderr}"

    yield "/test-vol"

    # Cleanup: delete all nested snapshots first, then the subvolume
    await pc1_executor.run_command(
        "sudo sh -c '"
        "btrfs subvolume list -o /test-vol 2>/dev/null | "
        'awk "{print \\$NF}" | '
        "xargs -r -I {} btrfs subvolume delete /{}"
        "'",
    )
    await pc1_executor.run_command("sudo btrfs subvolume delete /test-vol")


async def test_snapshot_preserves_content(pc1_executor: RemoteExecutor, test_volume: str) -> None:
    """Test that snapshot preserves file content.

    Verifies that files in a snapshot have the same content as the original.
    This is a fundamental requirement for data integrity.
    """
    test_file = "/test-vol/snapshot-content-test.txt"
    test_content = "test-content-for-snapshot-verification"
    snapshot_name = "/test-vol/.snapshots/test-content-snapshot"

    try:
        # Create a test file
        create_file = await pc1_executor.run_command(f"echo '{test_content}' | sudo tee {test_file}")
        assert create_file.success

        # Create snapshot
        await pc1_executor.run_command("sudo mkdir -p /test-vol/.snapshots")
        snapshot_result = await pc1_executor.run_command(f"sudo btrfs subvolume snapshot -r /test-vol {snapshot_name}")
        assert snapshot_result.success

        # Verify file exists in snapshot with same content
        read_snapshot_file = await pc1_executor.run_command(f"sudo cat {snapshot_name}/snapshot-content-test.txt")
        assert read_snapshot_file.success
        assert test_content in read_snapshot_file.stdout

        # Modify original file
        modify_file = await pc1_executor.run_command(f"echo 'modified' | sudo tee {test_file}")
        assert modify_file.success

        # Verify snapshot still has original content
        read_snapshot_again = await pc1_executor.run_command(f"sudo cat {snapshot_name}/snapshot-content-test.txt")
        assert read_snapshot_again.success
        assert test_content in read_snapshot_again.stdout
        assert "modified" not in read_snapshot_again.stdout

    finally:
        # Cleanup
        await pc1_executor.run_command(f"sudo rm -f {test_file}")
        await pc1_executor.run_command(
            f"sudo btrfs subvolume delete {snapshot_name}",
            timeout=10.0,
        )


async def test_multiple_snapshots_isolation(pc1_executor: RemoteExecutor, test_volume: str) -> None:
    """Test that multiple snapshots are isolated from each other.

    Verifies that creating multiple snapshots works correctly and that
    they are independent.
    """
    snapshot1 = "/test-vol/.snapshots/test-multi-1"
    snapshot2 = "/test-vol/.snapshots/test-multi-2"

    try:
        await pc1_executor.run_command("sudo mkdir -p /test-vol/.snapshots")

        # Create first snapshot
        result1 = await pc1_executor.run_command(f"sudo btrfs subvolume snapshot -r /test-vol {snapshot1}")
        assert result1.success

        # Create second snapshot
        result2 = await pc1_executor.run_command(f"sudo btrfs subvolume snapshot -r /test-vol {snapshot2}")
        assert result2.success

        # List snapshots - both should be present
        list_result = await pc1_executor.run_command("sudo btrfs subvolume list /test-vol")
        assert list_result.success
        assert "test-multi-1" in list_result.stdout
        assert "test-multi-2" in list_result.stdout

        # Delete first snapshot
        delete1 = await pc1_executor.run_command(
            f"sudo btrfs subvolume delete {snapshot1}",
            timeout=10.0,
        )
        assert delete1.success

        # Verify second snapshot still exists
        verify2 = await pc1_executor.run_command(f"sudo btrfs subvolume show {snapshot2}")
        assert verify2.success

    finally:
        # Cleanup both snapshots (in case test failed partway)
        await pc1_executor.run_command(
            f"sudo btrfs subvolume delete {snapshot1}",
            timeout=10.0,
        )
        await pc1_executor.run_command(
            f"sudo btrfs subvolume delete {snapshot2}",
            timeout=10.0,
        )


@pytest.mark.parametrize(
    ("scenario", "command"),
    [
        ("invalid_source", "sudo btrfs subvolume snapshot -r /nonexistent/path /tmp/bad-snapshot"),
        ("invalid_destination", "sudo btrfs subvolume snapshot -r /test-vol /nonexistent/dir/snapshot"),
        ("delete_nonexistent", "sudo btrfs subvolume delete /test-vol/.snapshots/nonexistent-snapshot-12345"),
    ],
)
async def test_snapshot_operation_failures(
    pc1_executor: RemoteExecutor,
    test_volume: str,
    scenario: str,
    command: str,
) -> None:
    """Test that snapshot operations fail gracefully with invalid paths.

    Per spec TST-FR-CONTRACT, we must verify failure paths.
    Tests:
    - Snapshot creation with invalid source path
    - Snapshot creation with invalid destination path
    - Snapshot deletion when snapshot doesn't exist
    """
    result = await pc1_executor.run_command(command, timeout=10.0)

    assert not result.success, f"{scenario} should fail"
    assert result.exit_code != 0, f"{scenario} should have non-zero exit code"
