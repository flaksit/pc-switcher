"""Integration tests for btrfs snapshot operations.

Tests real btrfs filesystem operations including:
- Snapshot creation, listing, and deletion
- Both success and failure paths
- Proper cleanup of test artifacts
"""

from __future__ import annotations

import pytest

from pcswitcher.executor import RemoteExecutor


@pytest.mark.integration
async def test_btrfs_filesystem_present(pc1_executor: RemoteExecutor) -> None:
    """Test that btrfs filesystem is available on the test VM.

    This is a prerequisite for all other btrfs tests. Verifies that the
    VM has been provisioned with a btrfs filesystem.
    """
    result = await pc1_executor.run_command("btrfs --version")

    assert result.success, f"btrfs not available: {result.stderr}"
    assert "btrfs-progs" in result.stdout.lower()


@pytest.mark.integration
async def test_btrfs_test_volume_exists(pc1_executor: RemoteExecutor) -> None:
    """Test that the designated test subvolume exists.

    The VM provisioning should have created a test subvolume for
    integration tests to use.
    """
    # Check if /test-vol exists and is a btrfs subvolume
    result = await pc1_executor.run_command("sudo btrfs subvolume show /test-vol 2>/dev/null")

    assert result.success, "Test volume /test-vol not found - VM may not be fully provisioned"


@pytest.mark.integration
async def test_create_readonly_snapshot(pc1_executor: RemoteExecutor) -> None:
    """Test creating a read-only btrfs snapshot.

    This is a core operation for pc-switcher. Verifies that we can create
    snapshots and that they are properly marked as read-only.
    """
    snapshot_name = "/test-vol/.snapshots/test-snapshot-readonly"

    try:
        # Ensure snapshot directory exists
        await pc1_executor.run_command("sudo mkdir -p /test-vol/.snapshots")

        # Create read-only snapshot
        result = await pc1_executor.run_command(f"sudo btrfs subvolume snapshot -r /test-vol {snapshot_name}")

        assert result.success, f"Snapshot creation failed: {result.stderr}"
        assert "Create a readonly snapshot" in result.stdout or snapshot_name in result.stdout

        # Verify snapshot exists
        verify_result = await pc1_executor.run_command(f"sudo btrfs subvolume show {snapshot_name}")
        assert verify_result.success

        # Verify it's read-only
        check_readonly = await pc1_executor.run_command(f"sudo btrfs property get {snapshot_name} ro")
        assert check_readonly.success
        assert "ro=true" in check_readonly.stdout

    finally:
        # Cleanup: delete the snapshot
        await pc1_executor.run_command(
            f"sudo btrfs subvolume delete {snapshot_name}",
            timeout=10.0,
        )


@pytest.mark.integration
async def test_create_writable_snapshot(pc1_executor: RemoteExecutor) -> None:
    """Test creating a writable (non-readonly) btrfs snapshot.

    While pc-switcher primarily uses read-only snapshots, writable snapshots
    may be needed for restoration operations.
    """
    snapshot_name = "/test-vol/.snapshots/test-snapshot-writable"

    try:
        # Ensure snapshot directory exists
        await pc1_executor.run_command("sudo mkdir -p /test-vol/.snapshots")

        # Create writable snapshot (no -r flag)
        result = await pc1_executor.run_command(f"sudo btrfs subvolume snapshot /test-vol {snapshot_name}")

        assert result.success, f"Snapshot creation failed: {result.stderr}"

        # Verify snapshot exists and is writable
        check_readonly = await pc1_executor.run_command(f"sudo btrfs property get {snapshot_name} ro")
        assert check_readonly.success
        assert "ro=false" in check_readonly.stdout

    finally:
        # Cleanup: delete the snapshot
        await pc1_executor.run_command(
            f"sudo btrfs subvolume delete {snapshot_name}",
            timeout=10.0,
        )


@pytest.mark.integration
async def test_list_snapshots(pc1_executor: RemoteExecutor) -> None:
    """Test listing btrfs subvolumes/snapshots.

    Verifies that we can enumerate snapshots, which is needed for
    snapshot management and cleanup operations.
    """
    snapshot_name = "/test-vol/.snapshots/test-list-snapshot"

    try:
        # Create a test snapshot
        await pc1_executor.run_command("sudo mkdir -p /test-vol/.snapshots")
        create_result = await pc1_executor.run_command(f"sudo btrfs subvolume snapshot -r /test-vol {snapshot_name}")
        assert create_result.success

        # List all subvolumes
        result = await pc1_executor.run_command("sudo btrfs subvolume list /test-vol")

        assert result.success, f"Listing snapshots failed: {result.stderr}"
        # Our test snapshot should appear in the list
        assert "test-list-snapshot" in result.stdout

    finally:
        # Cleanup
        await pc1_executor.run_command(
            f"sudo btrfs subvolume delete {snapshot_name}",
            timeout=10.0,
        )


@pytest.mark.integration
async def test_delete_snapshot(pc1_executor: RemoteExecutor) -> None:
    """Test deleting a btrfs snapshot.

    Verifies snapshot cleanup functionality, which is critical for
    managing disk space.
    """
    snapshot_name = "/test-vol/.snapshots/test-delete-snapshot"

    # Create a snapshot to delete
    await pc1_executor.run_command("sudo mkdir -p /test-vol/.snapshots")
    create_result = await pc1_executor.run_command(f"sudo btrfs subvolume snapshot -r /test-vol {snapshot_name}")
    assert create_result.success, "Setup failed: could not create test snapshot"

    # Delete the snapshot
    result = await pc1_executor.run_command(
        f"sudo btrfs subvolume delete {snapshot_name}",
        timeout=10.0,
    )

    assert result.success, f"Snapshot deletion failed: {result.stderr}"
    assert "Delete subvolume" in result.stdout or snapshot_name in result.stdout

    # Verify snapshot is gone
    verify_result = await pc1_executor.run_command(
        f"sudo btrfs subvolume show {snapshot_name}",
    )
    assert not verify_result.success, "Snapshot still exists after deletion"


@pytest.mark.integration
async def test_snapshot_creation_failure_invalid_source(
    pc1_executor: RemoteExecutor,
) -> None:
    """Test snapshot creation failure with invalid source path.

    Per spec FR-003a, we must verify failure paths. This tests that
    attempting to snapshot a non-existent path fails appropriately.
    """
    result = await pc1_executor.run_command("sudo btrfs subvolume snapshot -r /nonexistent/path /tmp/bad-snapshot")

    assert not result.success, "Should fail when source path doesn't exist"
    assert result.exit_code != 0
    # Error message should indicate the problem
    assert len(result.stderr) > 0 or "ERROR" in result.stdout


@pytest.mark.integration
async def test_snapshot_creation_failure_invalid_destination(
    pc1_executor: RemoteExecutor,
) -> None:
    """Test snapshot creation failure with invalid destination path.

    Tests that attempting to create a snapshot in a non-existent or
    non-btrfs directory fails appropriately.
    """
    result = await pc1_executor.run_command("sudo btrfs subvolume snapshot -r /test-vol /nonexistent/dir/snapshot")

    assert not result.success, "Should fail when destination path is invalid"
    assert result.exit_code != 0
    assert len(result.stderr) > 0 or "ERROR" in result.stdout


@pytest.mark.integration
async def test_delete_snapshot_failure_nonexistent(
    pc1_executor: RemoteExecutor,
) -> None:
    """Test snapshot deletion failure when snapshot doesn't exist.

    Per spec FR-003a, we must verify failure paths. Tests that attempting
    to delete a non-existent snapshot fails gracefully.
    """
    result = await pc1_executor.run_command(
        "sudo btrfs subvolume delete /test-vol/.snapshots/nonexistent-snapshot-12345",
        timeout=10.0,
    )

    assert not result.success, "Should fail when snapshot doesn't exist"
    assert result.exit_code != 0


@pytest.mark.integration
async def test_snapshot_preserves_content(pc1_executor: RemoteExecutor) -> None:
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


@pytest.mark.integration
async def test_multiple_snapshots_isolation(pc1_executor: RemoteExecutor) -> None:
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
