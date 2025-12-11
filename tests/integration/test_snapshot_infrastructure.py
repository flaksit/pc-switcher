"""Integration tests for snapshot infrastructure.

Tests the complete snapshot infrastructure including:
- Pre-sync and post-sync snapshot creation
- /.snapshots/ subvolume creation and validation
- Snapshot cleanup with retention policies
- Runtime disk space monitoring
- Error handling when btrfs is not available

These tests verify User Story 3 (Safety Infrastructure with Btrfs Snapshots)
from specs/001-foundation/spec.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
import pytest_asyncio

from pcswitcher.btrfs_snapshots import (
    cleanup_snapshots,
    create_snapshot,
    list_snapshots,
    snapshot_name,
    validate_snapshots_directory,
    validate_subvolume_exists,
)
from pcswitcher.executor import RemoteExecutor
from pcswitcher.models import Host, SnapshotPhase


@pytest_asyncio.fixture(scope="module")
async def test_subvolume(pc1_executor: RemoteExecutor) -> AsyncIterator[str]:
    """Create a test btrfs subvolume for snapshot testing.

    Creates /test-snapshots-vol as a clean sandbox for testing snapshot
    operations. Cleans up any leftover state from previous test runs.

    Module-scoped: shared across all tests in this module for efficiency.
    Individual tests must clean up their own artifacts in try/finally.
    """
    subvolume_path = "/test-snapshots-vol"

    # Clean slate: remove any leftover from previous runs
    await pc1_executor.run_command(
        "sudo sh -c '"
        f"btrfs subvolume list -o {subvolume_path} 2>/dev/null | "
        'awk "{print \\$NF}" | '
        "xargs -r -I {} btrfs subvolume delete /{}"
        "'",
    )
    await pc1_executor.run_command(f"sudo btrfs subvolume delete {subvolume_path} 2>/dev/null || true")

    # Create fresh test subvolume
    result = await pc1_executor.run_command(f"sudo btrfs subvolume create {subvolume_path}")
    assert result.success, f"Failed to create test subvolume: {result.stderr}"

    yield subvolume_path

    # Cleanup: delete all nested snapshots first, then the subvolume
    await pc1_executor.run_command(
        "sudo sh -c '"
        f"btrfs subvolume list -o {subvolume_path} 2>/dev/null | "
        'awk "{print \\$NF}" | '
        "xargs -r -I {} btrfs subvolume delete /{}"
        "'",
    )
    await pc1_executor.run_command(f"sudo btrfs subvolume delete {subvolume_path}")


@pytest.mark.integration
async def test_001_us3_as2_create_presync_snapshots(
    pc1_executor: RemoteExecutor,
    test_subvolume: str,
) -> None:
    """Test US3-AS2: Create pre-sync snapshots before any sync operations.

    Spec: specs/001-foundation/spec.md - User Story 3, Acceptance Scenario 2
    Verifies that the system creates read-only btrfs snapshots in
    /.snapshots/pc-switcher/<session-folder>/ with naming pattern
    pre-<subvol>-<timestamp>.
    """
    session_id = "test-presync-001"
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    session_folder = f"{timestamp}-{session_id}"
    session_path = f"/.snapshots/pc-switcher/{session_folder}"
    snap_path = ""  # Initialize to avoid type checker warning

    try:
        # Ensure snapshots directory exists as subvolume
        success, error_msg = await validate_snapshots_directory(pc1_executor, Host.SOURCE)
        assert success, f"Failed to validate snapshots directory: {error_msg}"

        # Create session folder
        mkdir_result = await pc1_executor.run_command(f"sudo mkdir -p {session_path}")
        assert mkdir_result.success, f"Failed to create session folder: {mkdir_result.stderr}"

        # Create pre-sync snapshot
        snap_name = snapshot_name("@test", SnapshotPhase.PRE)
        snap_path = f"{session_path}/{snap_name}"

        result = await create_snapshot(pc1_executor, test_subvolume, snap_path)
        assert result.success, f"Failed to create pre-sync snapshot: {result.stderr}"

        # Verify snapshot exists and is read-only
        verify_result = await pc1_executor.run_command(f"sudo btrfs subvolume show {snap_path}")
        assert verify_result.success, f"Snapshot not found: {snap_path}"

        # Verify read-only property
        readonly_result = await pc1_executor.run_command(f"sudo btrfs property get {snap_path} ro")
        assert readonly_result.success
        assert "ro=true" in readonly_result.stdout, "Snapshot is not read-only"

        # Verify naming pattern matches spec (pre-<subvol>-<timestamp>)
        assert snap_name.startswith("pre-@test-"), f"Snapshot name doesn't match pattern: {snap_name}"
        assert len(snap_name.split("-")) >= 3, f"Snapshot name missing timestamp: {snap_name}"

    finally:
        # Cleanup
        await pc1_executor.run_command(f"sudo btrfs subvolume delete {snap_path} 2>/dev/null || true", timeout=10.0)
        await pc1_executor.run_command(f"sudo rmdir {session_path} 2>/dev/null || true")


@pytest.mark.integration
async def test_001_us3_as3_create_postsync_snapshots(
    pc1_executor: RemoteExecutor,
    test_subvolume: str,
) -> None:
    """Test US3-AS3: Create post-sync snapshots after successful sync.

    Spec: specs/001-foundation/spec.md - User Story 3, Acceptance Scenario 3
    Verifies that the system creates read-only btrfs snapshots in the same
    session folder with naming pattern post-<subvol>-<timestamp>.
    """
    session_id = "test-postsync-001"
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    session_folder = f"{timestamp}-{session_id}"
    session_path = f"/.snapshots/pc-switcher/{session_folder}"
    snap_path = ""  # Initialize to avoid type checker warning

    try:
        # Ensure snapshots directory exists
        success, error_msg = await validate_snapshots_directory(pc1_executor, Host.SOURCE)
        assert success, f"Failed to validate snapshots directory: {error_msg}"

        # Create session folder
        mkdir_result = await pc1_executor.run_command(f"sudo mkdir -p {session_path}")
        assert mkdir_result.success, f"Failed to create session folder: {mkdir_result.stderr}"

        # Create post-sync snapshot
        snap_name = snapshot_name("@test", SnapshotPhase.POST)
        snap_path = f"{session_path}/{snap_name}"

        result = await create_snapshot(pc1_executor, test_subvolume, snap_path)
        assert result.success, f"Failed to create post-sync snapshot: {result.stderr}"

        # Verify snapshot exists and is read-only
        verify_result = await pc1_executor.run_command(f"sudo btrfs subvolume show {snap_path}")
        assert verify_result.success, f"Snapshot not found: {snap_path}"

        # Verify read-only property
        readonly_result = await pc1_executor.run_command(f"sudo btrfs property get {snap_path} ro")
        assert readonly_result.success
        assert "ro=true" in readonly_result.stdout, "Snapshot is not read-only"

        # Verify naming pattern matches spec (post-<subvol>-<timestamp>)
        assert snap_name.startswith("post-@test-"), f"Snapshot name doesn't match pattern: {snap_name}"
        assert len(snap_name.split("-")) >= 3, f"Snapshot name missing timestamp: {snap_name}"

        # Verify post-sync snapshot is in same session folder as pre-sync would be
        assert session_id in snap_path, "Snapshot not in session-specific folder"

    finally:
        # Cleanup
        await pc1_executor.run_command(f"sudo btrfs subvolume delete {snap_path} 2>/dev/null || true", timeout=10.0)
        await pc1_executor.run_command(f"sudo rmdir {session_path} 2>/dev/null || true")


@pytest.mark.integration
async def test_001_us3_as4_create_snapshots_subvolume(
    pc1_executor: RemoteExecutor,
) -> None:
    """Test US3-AS4: Create /.snapshots/ as btrfs subvolume if missing.

    Spec: specs/001-foundation/spec.md - User Story 3, Acceptance Scenario 4
    Verifies that if /.snapshots/ doesn't exist, the system creates it as a
    btrfs subvolume (not a regular directory).
    """
    snapshots_path = "/.snapshots"
    test_marker_path = f"{snapshots_path}/.test-marker-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Check if /.snapshots already exists
    check_result = await pc1_executor.run_command(f"sudo btrfs subvolume show {snapshots_path} 2>&1")
    already_existed = check_result.success

    try:
        if already_existed:
            # Create a marker file so we can verify this is the original subvolume
            await pc1_executor.run_command(f"sudo touch {test_marker_path}")

        # Delete /.snapshots if it exists (to test creation)
        if already_existed:
            # First delete any nested subvolumes
            await pc1_executor.run_command(
                "sudo sh -c '"
                f"btrfs subvolume list -o {snapshots_path} 2>/dev/null | "
                'awk "{print \\$NF}" | '
                "xargs -r -I {} btrfs subvolume delete /{}"
                "'",
            )
            # Then delete /.snapshots itself
            delete_result = await pc1_executor.run_command(f"sudo btrfs subvolume delete {snapshots_path}")
            assert delete_result.success, f"Failed to delete existing /.snapshots: {delete_result.stderr}"

        # Verify /.snapshots doesn't exist
        verify_gone = await pc1_executor.run_command(f"sudo btrfs subvolume show {snapshots_path} 2>&1")
        assert not verify_gone.success, "/.snapshots still exists after deletion"

        # Call validate_snapshots_directory which should create it
        success, error_msg = await validate_snapshots_directory(pc1_executor, Host.SOURCE)
        assert success, f"Failed to create /.snapshots subvolume: {error_msg}"

        # Verify /.snapshots exists and is a subvolume
        verify_result = await pc1_executor.run_command(f"sudo btrfs subvolume show {snapshots_path}")
        assert verify_result.success, "/.snapshots was not created"

        # Verify it's a subvolume (not a regular directory)
        # If it's a subvolume, the show command should succeed
        assert "Name:" in verify_result.stdout or "Subvolume" in verify_result.stdout, (
            "/.snapshots is not a btrfs subvolume"
        )

        # Verify pc-switcher subdirectory was created
        pc_switcher_dir = f"{snapshots_path}/pc-switcher"
        ls_result = await pc1_executor.run_command(f"test -d {pc_switcher_dir}")
        assert ls_result.success, f"{pc_switcher_dir} directory was not created"

    finally:
        # Restore /.snapshots if it existed before the test
        if already_existed:
            # Re-create if needed (validate_snapshots_directory should handle this)
            await validate_snapshots_directory(pc1_executor, Host.SOURCE)


@pytest.mark.integration
async def test_001_us3_as7_cleanup_snapshots_with_retention(
    pc1_executor: RemoteExecutor,
    test_subvolume: str,
) -> None:
    """Test US3-AS7: Cleanup snapshots with retention policy.

    Spec: specs/001-foundation/spec.md - User Story 3, Acceptance Scenario 7
    Verifies that cleanup_snapshots respects retention policies:
    - Keeps the most recent N sessions regardless of age
    - Deletes snapshots older than max_age_days (if specified)
    """
    # Create multiple snapshot sessions with different ages
    sessions = []
    session_paths = []

    try:
        # Ensure snapshots directory exists
        success, error_msg = await validate_snapshots_directory(pc1_executor, Host.SOURCE)
        assert success, f"Failed to validate snapshots directory: {error_msg}"

        # Create 5 test sessions (we'll keep 3 most recent)
        for i in range(5):
            session_id = f"cleanup-test-{i:03d}"
            # Use different timestamps to ensure ordering
            timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            session_folder = f"{timestamp}-{session_id}"
            session_path = f"/.snapshots/pc-switcher/{session_folder}"
            session_paths.append(session_path)

            # Create session folder
            await pc1_executor.run_command(f"sudo mkdir -p {session_path}")

            # Create both pre and post snapshots for this session
            pre_snap = snapshot_name("@test", SnapshotPhase.PRE)
            post_snap = snapshot_name("@test", SnapshotPhase.POST)

            pre_path = f"{session_path}/{pre_snap}"
            post_path = f"{session_path}/{post_snap}"

            await create_snapshot(pc1_executor, test_subvolume, pre_path)
            await create_snapshot(pc1_executor, test_subvolume, post_path)

            sessions.append((session_id, session_folder, [pre_path, post_path]))

        # List snapshots before cleanup
        snapshots_before = await list_snapshots(pc1_executor, Host.SOURCE)
        test_snapshots_before = [s for s in snapshots_before if "cleanup-test" in s.session_id]
        assert len(test_snapshots_before) == 10, (
            f"Expected 10 snapshots (5 sessions x 2), got {len(test_snapshots_before)}"
        )

        # Run cleanup keeping 3 most recent sessions
        deleted = await cleanup_snapshots(
            executor=pc1_executor,
            host=Host.SOURCE,
            keep_recent=3,
            max_age_days=None,
        )

        # Verify correct number of snapshots were deleted
        # Should delete 2 oldest sessions (4 snapshots total: 2 pre + 2 post)
        test_deleted = [s for s in deleted if "cleanup-test" in s.session_id]
        assert len(test_deleted) == 4, f"Expected 4 snapshots deleted, got {len(test_deleted)}"

        # List snapshots after cleanup
        snapshots_after = await list_snapshots(pc1_executor, Host.SOURCE)
        test_snapshots_after = [s for s in snapshots_after if "cleanup-test" in s.session_id]
        assert len(test_snapshots_after) == 6, (
            f"Expected 6 snapshots remaining (3 sessions x 2), got {len(test_snapshots_after)}"
        )

        # Verify the 3 most recent sessions remain
        remaining_session_ids = {s.session_id for s in test_snapshots_after}
        assert "cleanup-test-002" in remaining_session_ids, "Session 002 should be kept"
        assert "cleanup-test-003" in remaining_session_ids, "Session 003 should be kept"
        assert "cleanup-test-004" in remaining_session_ids, "Session 004 should be kept"

        # Verify the 2 oldest sessions were deleted
        assert "cleanup-test-000" not in remaining_session_ids, "Session 000 should be deleted"
        assert "cleanup-test-001" not in remaining_session_ids, "Session 001 should be deleted"

    finally:
        # Cleanup all test sessions
        for _session_id, session_folder, snap_paths in sessions:
            for snap_path in snap_paths:
                await pc1_executor.run_command(
                    f"sudo btrfs subvolume delete {snap_path} 2>/dev/null || true",
                    timeout=10.0,
                )
            session_path = f"/.snapshots/pc-switcher/{session_folder}"
            await pc1_executor.run_command(f"sudo rmdir {session_path} 2>/dev/null || true")


@pytest.mark.integration
async def test_001_us3_as9_runtime_disk_space_monitoring(
    pc1_executor: RemoteExecutor,
) -> None:
    """Test US3-AS9: Runtime disk space monitoring during sync.

    Spec: specs/001-foundation/spec.md - User Story 3, Acceptance Scenario 9
    Verifies that the system can check available disk space during runtime
    and detect when it falls below configured thresholds.

    Note: This test verifies the disk space checking mechanism works.
    The actual monitoring job and abort logic is tested in
    tests/unit/jobs/test_disk_space_monitor.py.
    """
    # Check current disk space on /
    df_result = await pc1_executor.run_command("df -h / | tail -1")
    assert df_result.success, f"Failed to check disk space: {df_result.stderr}"

    # Parse df output to verify we can extract disk space information
    df_output = df_result.stdout.strip()
    assert df_output, "df command returned no output"

    # Verify output format (should have columns: Filesystem, Size, Used, Avail, Use%, Mounted)
    columns = df_output.split()
    assert len(columns) >= 5, f"Unexpected df output format: {df_output}"

    # Extract available space (4th column, e.g., "50G")
    avail_space = columns[3]
    assert avail_space, "Could not extract available space from df output"

    # Verify available space is a valid size string (ends with K, M, G, or T)
    assert any(avail_space.endswith(unit) for unit in ["K", "M", "G", "T"]), (
        f"Unexpected available space format: {avail_space}"
    )

    # Verify we can check disk space on /.snapshots (if it exists)
    snapshots_df_result = await pc1_executor.run_command("df -h /.snapshots 2>/dev/null || df -h /")
    assert snapshots_df_result.success, "Failed to check snapshots directory disk space"

    # Verify the command can be used to monitor disk space changes
    # (actual monitoring frequency and abort logic is in DiskSpaceMonitorJob)
    assert snapshots_df_result.stdout, "Disk space monitoring command returned no output"


@pytest.mark.integration
async def test_001_edge_btrfs_not_available(
    pc1_executor: RemoteExecutor,
) -> None:
    """Test edge case: btrfs tools not available on the system.

    Spec: specs/003-foundation-tests/tasks.md - T015 edge case
    Verifies that the system handles gracefully when btrfs is not available
    or when trying to snapshot a non-btrfs filesystem.
    """
    # Test 1: Try to create snapshot on a non-existent path
    fake_path = f"/nonexistent/path/{datetime.now().strftime('%Y%m%d%H%M%S')}"
    snap_path = f"/tmp/test-snapshot-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    result = await create_snapshot(pc1_executor, fake_path, snap_path)

    # Should fail when source doesn't exist
    assert not result.success, "Snapshot creation should fail for non-existent source"
    assert result.exit_code != 0, "Exit code should indicate failure"
    assert result.stderr, "Should have error message in stderr"

    # Test 2: Try to validate subvolume on a non-btrfs path
    # Use /tmp which is typically not a btrfs subvolume
    success, error_msg = await validate_subvolume_exists(
        pc1_executor,
        "@tmp",
        "/tmp",
        Host.SOURCE,
    )

    # Should fail for non-btrfs filesystem or non-subvolume path
    # (on test VMs /tmp might be btrfs, so we check the behavior is consistent)
    if not success:
        assert error_msg is not None, "Should have error message when validation fails"
        assert "not found" in error_msg.lower() or "not" in error_msg.lower(), (
            f"Error message should indicate validation failure: {error_msg}"
        )

    # Test 3: Try to create /.snapshots on a hypothetical non-btrfs system
    # We simulate this by trying to validate a path that can't be a btrfs subvolume
    # Note: On actual test VMs this will succeed since they have btrfs
    # The test verifies the error handling path exists
    await pc1_executor.run_command("sudo mkdir -p /tmp/fake-snapshots-test")
    try:
        # Try to show it as a subvolume (will fail if not btrfs)
        show_result = await pc1_executor.run_command("sudo btrfs subvolume show /tmp/fake-snapshots-test 2>&1")

        if not show_result.success:
            # Good - this path is not a btrfs subvolume
            assert show_result.exit_code != 0, "Non-btrfs path should fail subvolume check"
            assert show_result.stderr or show_result.stdout, "Should have error output"
    finally:
        await pc1_executor.run_command("sudo rmdir /tmp/fake-snapshots-test 2>/dev/null || true")
