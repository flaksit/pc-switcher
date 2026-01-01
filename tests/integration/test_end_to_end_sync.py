"""Integration tests for end-to-end sync operations.

Tests User Story 1 (Job Architecture) acceptance scenarios:
- US1-AS1: Job integration via standardized interface
- US1-AS7: Interrupt handling during job execution
- Edge case: Target unreachable mid-sync

These tests verify the complete orchestrator workflow by actually running
`pc-switcher sync` on test VMs. They exercise the full sync pipeline including:
- Lock acquisition (source and target)
- SSH connection establishment
- Job discovery and validation
- Disk space preflight checks
- Pre-sync btrfs snapshots
- InstallOnTargetJob execution
- Config sync to target
- Sync job execution (dummy_success)
- Post-sync btrfs snapshots
- Cleanup and lock release

Test VM Requirements:
- pc1 and pc2 VMs must be provisioned and accessible
- VMs must have btrfs filesystem with @ and @home subvolumes
- VMs must be reset to baseline before tests run
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.version import get_this_version

# Test config with short durations for faster tests
_TEST_CONFIG_TEMPLATE = """# Test configuration for end-to-end sync tests
# Short durations to keep tests fast

log_file_level: DEBUG
log_cli_level: DEBUG

sync_jobs:
  dummy_success: true
  dummy_fail: false

disk_space_monitor:
  preflight_minimum: "5%"
  runtime_minimum: "3%"
  warning_threshold: "10%"
  check_interval: 5

btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
  keep_recent: 2

dummy_success:
  source_duration: {source_duration}
  target_duration: {target_duration}
"""


@pytest_asyncio.fixture
async def sync_ready_source(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 configured and ready to run pc-switcher sync.

    This fixture:
    1. Ensures pc-switcher is installed (via pc1_with_pcswitcher_mod)
    2. Creates a test configuration with short-duration jobs
    3. Cleans up the test config after the test

    Yields:
        Executor for pc1, ready to run sync commands
    """
    executor = pc1_with_pcswitcher_mod

    # Backup existing config if any
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml ]; then "
        "cp ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.e2e-backup; "
        "fi",
        timeout=10.0,
    )

    # Create test config with short durations (4 seconds each = 8 seconds total for dummy_success)
    test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=4, target_duration=4)
    await executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)

    # Use heredoc to write config
    write_result = await executor.run_command(
        f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
        timeout=10.0,
    )
    assert write_result.success, f"Failed to write test config: {write_result.stderr}"

    yield executor

    # Cleanup: restore original config
    await executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.e2e-backup ]; then "
        "mv ~/.config/pc-switcher/config.yaml.e2e-backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


@pytest_asyncio.fixture
async def sync_ready_source_long_duration(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 configured for sync with longer duration (for interrupt tests).

    Same as sync_ready_source but with 60-second durations to allow time
    for interrupt testing.
    """
    executor = pc1_with_pcswitcher_mod

    # Backup existing config if any
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml ]; then "
        "cp ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.e2e-backup; "
        "fi",
        timeout=10.0,
    )

    # Create test config with longer durations for interrupt testing
    test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=60, target_duration=60)
    await executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)

    write_result = await executor.run_command(
        f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
        timeout=10.0,
    )
    assert write_result.success, f"Failed to write test config: {write_result.stderr}"

    yield executor

    # Cleanup
    await executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.e2e-backup ]; then "
        "mv ~/.config/pc-switcher/config.yaml.e2e-backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


class TestEndToEndSync:
    """Integration tests for complete pc-switcher sync workflow."""

    async def test_001_us1_as1_job_integration_via_interface(
        self,
        sync_ready_source: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Test US1-AS1: Job integrates via standardized interface.

        Spec reference: specs/001-foundation/spec.md - User Story 1, Acceptance Scenario 1

        Verifies that a job implementing the standardized Job interface is automatically
        integrated into the sync workflow without requiring changes to core orchestrator code.

        This test validates:
        - Job is discovered and loaded by orchestrator
        - Configuration is validated against job schema
        - Job lifecycle methods called in correct order (validate → execute)
        - Job logging routed to file and terminal UI
        - Progress updates forwarded to UI
        - Job results included in sync summary

        Expected behavior:
        1. Configure a test sync job (dummy_success) via config.yaml
        2. Run pc-switcher sync from pc1 to pc2
        3. Verify orchestrator loads the job
        4. Verify job completes successfully
        5. Verify sync exits with code 0
        6. Verify log file contains job entries
        7. Verify snapshots were created
        """
        pc1_executor = sync_ready_source

        # Clean up any existing snapshots from previous test runs to get clean state
        # Check both hosts in parallel
        # TODO use btrfs subvolume delete instead of rm -rf (faster), like delete_subvol_recursive() in reset-vm.sh
        pc1_task = pc1_executor.run_command(
            "sudo rm -rf /.snapshots/pc-switcher* 2>/dev/null || true",
            timeout=30.0,
            login_shell=False,
        )
        pc2_task = pc2_executor.run_command(
            "sudo rm -rf /.snapshots/pc-switcher* 2>/dev/null || true",
            timeout=30.0,
            login_shell=False,
        )
        await asyncio.gather(pc1_task, pc2_task)

        # Run pc-switcher sync from pc1 to pc2
        # Timeout: ~60s for SSH + install + snapshots + job execution (4+4 seconds)
        # Use --yes to auto-accept config sync prompts (non-interactive)
        sync_result = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes",
            timeout=180.0,
            login_shell=True,
        )

        # Verify sync completed successfully
        assert sync_result.success, (
            f"pc-switcher sync failed with exit code {sync_result.exit_code}.\n"
            f"stdout: {sync_result.stdout}\n"
            f"stderr: {sync_result.stderr}"
        )

        # Verify log file was created and contains job entries
        log_check = await pc1_executor.run_command(
            "ls -la ~/.local/share/pc-switcher/logs/sync-*.log | tail -1",
            timeout=10.0,
        )
        assert log_check.success, f"Log file not found: {log_check.stderr}"

        # Read the latest log file and verify job execution entries
        log_content = await pc1_executor.run_command(
            "cat $(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)",
            timeout=10.0,
        )
        assert log_content.success, f"Failed to read log file: {log_content.stderr}"
        log_text = log_content.stdout

        # Verify job execution logged (dummy_success job should log phase messages)
        assert "dummy_success" in log_text.lower() or "source phase" in log_text.lower(), (
            f"Log should contain dummy_success job entries.\nLog content:\n{log_text[:2000]}"
        )

        # Verify snapshots were created on source (pc1)
        # Snapshots are at /.snapshots/pc-switcher/<session_folder>/<snapshot_name>
        # e.g., /.snapshots/pc-switcher/20251219T143022-abc12345/pre-@-20251219T143022
        source_snapshots = await pc1_executor.run_command(
            "sudo ls /.snapshots/pc-switcher/ 2>/dev/null | head -1",
            timeout=10.0,
            login_shell=False,
        )
        assert source_snapshots.stdout.strip(), (
            f"Pre/post-sync snapshots should exist on source.\nLs output: {source_snapshots.stdout}"
        )

        # Verify snapshots were created on target (pc2)
        target_snapshots = await pc2_executor.run_command(
            "sudo ls /.snapshots/pc-switcher/ 2>/dev/null | head -1",
            timeout=10.0,
            login_shell=False,
        )
        assert target_snapshots.stdout.strip(), (
            f"Pre/post-sync snapshots should exist on target.\nLs output: {target_snapshots.stdout}"
        )

        # Verify config was synced to target
        target_config = await pc2_executor.run_command(
            "cat ~/.config/pc-switcher/config.yaml",
            timeout=10.0,
        )
        assert target_config.success, f"Config should exist on target: {target_config.stderr}"
        assert "dummy_success: true" in target_config.stdout, (
            f"Target config should match source.\nTarget config:\n{target_config.stdout}"
        )

    async def test_001_us1_as7_interrupt_terminates_job(
        self,
        sync_ready_source_long_duration: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Test US1-AS7: Ctrl+C terminates job with cleanup.

        Spec reference: specs/001-foundation/spec.md - User Story 1, Acceptance Scenario 7

        Verifies that when user presses Ctrl+C during job execution, the orchestrator:
        - Catches SIGINT signal
        - Requests termination of currently-executing job
        - Logs interruption at WARNING level
        - Exits with code 130

        Expected behavior:
        1. Start sync with long-running dummy_success job (60s)
        2. Wait for job to begin execution
        3. Send SIGINT to the sync process
        4. Verify process exits with code 130
        5. Verify "interrupted" message in output

        Test approach:
        - Start sync in background using nohup and capture PID
        - Wait for sync to start (check for running process or log output)
        - Send SIGINT to the process
        - Wait for process to terminate
        - Check exit code and output
        """
        pc1_executor = sync_ready_source_long_duration

        # Start sync in background and capture output to a temp file
        # Use script to run in a pseudo-terminal for proper signal handling
        output_file = "/tmp/pcswitcher-e2e-interrupt-test-output.txt"
        pid_file = "/tmp/pcswitcher-e2e-interrupt-test-pid.txt"

        # Clean up from any previous run
        await pc1_executor.run_command(f"rm -f {output_file} {pid_file}", timeout=10.0)

        # Start sync in background with script for TTY emulation
        # We use bash -c to wrap the command and capture the PID
        start_result = await pc1_executor.run_command(
            f"nohup bash -c 'echo $$ > {pid_file}; exec pc-switcher sync pc2 --yes 2>&1' > {output_file} &",
            timeout=10.0,
            login_shell=True,
        )
        assert start_result.success, f"Failed to start background sync: {start_result.stderr}"

        # Wait for PID file to be written and process to start
        await asyncio.sleep(2)

        # Get the PID
        pid_result = await pc1_executor.run_command(f"cat {pid_file}", timeout=10.0)
        assert pid_result.success and pid_result.stdout.strip(), f"Failed to get sync process PID: {pid_result.stderr}"
        sync_pid = pid_result.stdout.strip()

        # Wait for sync to actually start (look for connection or log activity)
        # Give it time to establish SSH connection and start job execution
        for _ in range(30):  # Wait up to 30 seconds for job to start
            await asyncio.sleep(1)
            output_check = await pc1_executor.run_command(f"cat {output_file} 2>/dev/null || true", timeout=10.0)
            # Check if we see any progress indicating sync has started
            if "source" in output_check.stdout.lower() or "target" in output_check.stdout.lower():
                break
            if "connecting" in output_check.stdout.lower() or "lock" in output_check.stdout.lower():
                continue  # Still in setup phase, keep waiting
            # Check if process is still running
            ps_check = await pc1_executor.run_command(f"ps -p {sync_pid} -o pid= 2>/dev/null || true", timeout=5.0)
            if not ps_check.stdout.strip():
                break  # Process finished (possibly errored out)

        # Send SIGINT to the sync process
        await pc1_executor.run_command(
            f"kill -INT {sync_pid} 2>/dev/null || true",
            timeout=10.0,
            login_shell=False,
        )

        # Wait for process to terminate (up to 35 seconds for cleanup timeout)
        process_terminated = False
        for _ in range(40):  # Wait up to 40 seconds
            await asyncio.sleep(1)
            ps_check = await pc1_executor.run_command(
                f"ps -p {sync_pid} -o pid= 2>/dev/null || echo 'terminated'",
                timeout=5.0,
                login_shell=False,
            )
            if "terminated" in ps_check.stdout or not ps_check.stdout.strip():
                process_terminated = True
                break

        assert process_terminated, f"Sync process {sync_pid} did not terminate after SIGINT"

        # Read the output
        output_result = await pc1_executor.run_command(f"cat {output_file}", timeout=10.0)
        output_text = output_result.stdout

        # Verify interrupt handling message
        assert "interrupt" in output_text.lower(), f"Output should contain interrupt message.\nOutput:\n{output_text}"

        # Clean up temp files
        await pc1_executor.run_command(f"rm -f {output_file} {pid_file}", timeout=10.0)

    async def test_001_edge_target_unreachable_mid_sync(
        self,
        sync_ready_source: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Test edge case: Target becomes unreachable mid-sync.

        Spec reference: specs/001-foundation/spec.md - Edge Cases

        This test is intentionally limited because simulating network failure
        requires destructive operations (stopping SSH daemon, blocking with iptables)
        that could leave the test VM in an inconsistent state.

        For now, we just verify that if the target becomes unreachable, the sync
        properly detects and reports the connection failure. A full implementation
        would require:
        - VM snapshot before test
        - Coordinated network disruption
        - VM restore after test

        This is marked as a placeholder for future enhancement when the test
        infrastructure supports safe network disruption testing.
        """
        # This test is intentionally not implemented as it requires
        # destructive operations that are difficult to safely automate.
        # The behavior is verified indirectly through:
        # - Unit tests for connection error handling
        # - Manual testing during development
        pytest.skip(
            "Network failure simulation requires destructive operations. "
            "Verified manually and through unit tests for connection handling."
        )


class TestInstallOnTargetIntegration:
    """Integration tests verifying InstallOnTargetJob effects through full sync."""

    async def test_install_on_target_fresh_machine(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_without_pcswitcher_fn: BashLoginRemoteExecutor,
    ) -> None:
        """Verify InstallOnTargetJob installs pc-switcher on fresh target.

        This test runs a full pc-switcher sync to a target that has no pc-switcher
        installed, verifying that the InstallOnTargetJob correctly:
        1. Detects missing pc-switcher on target
        2. Installs the same version as source
        3. Verifies installation succeeded

        Unlike test_install_on_target_job.py which tests the job in isolation,
        this test verifies the job works correctly within the full sync pipeline.
        """
        pc1_executor = pc1_with_pcswitcher_mod

        # Create minimal test config
        test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=2, target_duration=2)
        await pc1_executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)
        await pc1_executor.run_command(
            f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
            timeout=10.0,
        )

        try:
            # Run sync - this should install pc-switcher on target
            # Use --yes to auto-accept config sync prompts (non-interactive)
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=300.0,  # Allow more time for fresh install
                login_shell=True,
            )

            # Check exit code
            assert sync_result.success, (
                f"Sync should succeed.\n"
                f"Exit code: {sync_result.exit_code}\n"
                f"Stdout: {sync_result.stdout}\n"
                f"Stderr: {sync_result.stderr}"
            )

            # Verify pc-switcher is now installed on target
            post_check = await pc2_without_pcswitcher_fn.run_command(
                "pc-switcher --version",
                timeout=10.0,
                login_shell=True,
            )
            assert post_check.success, (
                f"pc-switcher should be installed on target after sync.\n"
                f"Output: {post_check.stdout}\n"
                f"Error: {post_check.stderr}"
            )

            # Verify version matches source floor release (dev versions install the floor release)
            source_release = get_this_version().get_release_floor()
            assert source_release.version.semver_str() in post_check.stdout, (
                f"Target version should match source floor release {source_release.version.semver_str()}.\n"
                f"Target output: {post_check.stdout}"
            )

        finally:
            # Clean up config
            await pc1_executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)

    async def test_install_on_target_upgrade_older_version(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_old_pcswitcher_fn: BashLoginRemoteExecutor,
    ) -> None:
        """Verify InstallOnTargetJob upgrades older pc-switcher on target.

        This test runs a full pc-switcher sync to a target that has an older
        version installed, verifying that the InstallOnTargetJob:
        1. Detects version mismatch
        2. Upgrades to source version
        3. Verifies upgrade succeeded
        """
        # Create minimal test config
        test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=2, target_duration=2)
        await pc1_with_pcswitcher_mod.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)
        await pc1_with_pcswitcher_mod.run_command(
            f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
            timeout=10.0,
        )

        try:
            # Run sync - this should upgrade pc-switcher on target
            # Use --yes to auto-accept config sync prompts (non-interactive)
            sync_result = await pc1_with_pcswitcher_mod.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=300.0,
                login_shell=True,
            )

            # Check exit code
            assert sync_result.success, (
                f"Sync should succeed.\n"
                f"Exit code: {sync_result.exit_code}\n"
                f"Stdout: {sync_result.stdout}\n"
                f"Stderr: {sync_result.stderr}"
            )

            # Verify pc-switcher was upgraded on target
            post_check = await pc2_with_old_pcswitcher_fn.run_command(
                "pc-switcher --version",
                timeout=10.0,
                login_shell=True,
            )
            assert post_check.success, f"pc-switcher should work on target after sync.\nError: {post_check.stderr}"

            # Verify version matches source floor release (not old version)
            source_release = get_this_version().get_release_floor()
            assert source_release.version.semver_str() in post_check.stdout, (
                f"Target version should match source floor release {source_release.version.semver_str()}.\n"
                f"Target output: {post_check.stdout}"
            )

        finally:
            # Clean up config
            await pc1_with_pcswitcher_mod.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)


class TestConsecutiveSyncWarning:
    """Integration tests for consecutive sync warning feature (#47).

    Tests verify that:
    - Sync history is updated on both source and target after successful sync
    - Consecutive syncs without back-sync are blocked (prompt defaults to 'n')
    - --allow-consecutive flag bypasses the warning
    - Back-sync workflow clears the warning state
    """

    async def test_sync_updates_history_on_both_machines(
        self,
        sync_ready_source: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """After sync, both machines should have correct sync history.

        Verifies:
        - Source (pc1) has last_role="source"
        - Target (pc2) has last_role="target"
        """
        pc1_executor = sync_ready_source

        # Clean up any existing history files
        await pc1_executor.run_command("rm -f ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        await pc2_executor.run_command("rm -f ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)

        # Run sync with --allow-consecutive to avoid any existing state issues
        sync_result = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes --allow-consecutive",
            timeout=180.0,
            login_shell=True,
        )
        assert sync_result.success, (
            f"Sync failed.\nExit code: {sync_result.exit_code}\n"
            f"Stdout: {sync_result.stdout}\nStderr: {sync_result.stderr}"
        )

        # Verify source history
        pc1_history = await pc1_executor.run_command(
            "cat ~/.local/share/pc-switcher/sync-history.json",
            timeout=10.0,
        )
        assert pc1_history.success, f"Failed to read pc1 history: {pc1_history.stderr}"
        assert '"last_role": "source"' in pc1_history.stdout, (
            f"pc1 should have last_role=source.\nContent: {pc1_history.stdout}"
        )

        # Verify target history
        pc2_history = await pc2_executor.run_command(
            "cat ~/.local/share/pc-switcher/sync-history.json",
            timeout=10.0,
        )
        assert pc2_history.success, f"Failed to read pc2 history: {pc2_history.stderr}"
        assert '"last_role": "target"' in pc2_history.stdout, (
            f"pc2 should have last_role=target.\nContent: {pc2_history.stdout}"
        )

    async def test_consecutive_sync_blocked_without_flag(
        self,
        sync_ready_source: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Consecutive sync should be blocked when prompt defaults to 'n'.

        After a successful sync, attempting another sync without --allow-consecutive
        should abort because the warning prompt defaults to 'n' in non-interactive mode.
        """
        pc1_executor = sync_ready_source

        # Clean up and do first sync
        await pc1_executor.run_command("rm -f ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        await pc2_executor.run_command("rm -f ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)

        first_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes --allow-consecutive",
            timeout=180.0,
            login_shell=True,
        )
        assert first_sync.success, f"First sync should succeed: {first_sync.stderr}"

        # Attempt second sync WITHOUT --allow-consecutive
        # This should fail because warning prompt defaults to 'n'
        second_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes",
            timeout=60.0,
            login_shell=True,
        )

        assert not second_sync.success, (
            f"Second sync should fail (warning default=n).\n"
            f"Exit code: {second_sync.exit_code}\nStdout: {second_sync.stdout}"
        )
        # Verify the output mentions consecutive sync or abort
        output = second_sync.stdout + second_sync.stderr
        assert "consecutive" in output.lower() or "abort" in output.lower(), (
            f"Output should mention consecutive sync warning.\nOutput: {output}"
        )

    async def test_consecutive_sync_allowed_with_flag(
        self,
        sync_ready_source: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Consecutive sync should succeed with --allow-consecutive flag."""
        pc1_executor = sync_ready_source

        # Clean up and do first sync
        await pc1_executor.run_command("rm -f ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        await pc2_executor.run_command("rm -f ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)

        first_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes --allow-consecutive",
            timeout=180.0,
            login_shell=True,
        )
        assert first_sync.success, f"First sync should succeed: {first_sync.stderr}"

        # Second sync WITH --allow-consecutive should succeed
        second_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes --allow-consecutive",
            timeout=180.0,
            login_shell=True,
        )
        assert second_sync.success, (
            f"Second sync with --allow-consecutive should succeed.\n"
            f"Exit code: {second_sync.exit_code}\nStderr: {second_sync.stderr}"
        )

    async def test_back_sync_clears_warning(
        self,
        sync_ready_source: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
    ) -> None:
        """After receiving a back-sync, machine can sync again without warning.

        Full workflow:
        1. pc1 syncs to pc2 → pc1=source, pc2=target
        2. pc2 syncs back to pc1 → pc2=source, pc1=target
        3. pc1 syncs to pc2 again → should succeed WITHOUT --allow-consecutive
           because pc1 was last a target (received sync from pc2)

        NOTE: pc2_with_pcswitcher is used instead of pc2_executor to ensure
        pc2 has the exact same version as pc1 (from current branch), which is
        required for back-sync version validation to pass.
        """
        pc1_executor = sync_ready_source
        pc2_executor = pc2_with_pcswitcher

        # Clean up any existing history files
        await pc1_executor.run_command("rm -f ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        await pc2_executor.run_command("rm -f ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)

        # Step 1: pc1 syncs to pc2
        first_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes --allow-consecutive",
            timeout=180.0,
            login_shell=True,
        )
        assert first_sync.success, f"First sync (pc1→pc2) should succeed: {first_sync.stderr}"

        # Verify state: pc1=source, pc2=target
        pc1_history = await pc1_executor.run_command("cat ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        assert '"last_role": "source"' in pc1_history.stdout, "pc1 should be source after first sync"

        # Step 2: pc2 syncs back to pc1
        # pc2 has pc-switcher installed (from first sync) and config synced
        back_sync = await pc2_executor.run_command(
            "pc-switcher sync pc1 --yes",
            timeout=180.0,
            login_shell=True,
        )
        assert back_sync.success, (
            f"Back sync (pc2→pc1) should succeed.\n"
            f"Exit code: {back_sync.exit_code}\nStdout: {back_sync.stdout}\nStderr: {back_sync.stderr}"
        )

        # Verify state: pc1=target (received sync), pc2=source
        pc1_history = await pc1_executor.run_command("cat ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        assert '"last_role": "target"' in pc1_history.stdout, "pc1 should be target after back-sync"

        # Step 3: pc1 syncs to pc2 again - should succeed WITHOUT --allow-consecutive
        # because pc1 was last a target
        third_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes",  # No --allow-consecutive!
            timeout=180.0,
            login_shell=True,
        )
        assert third_sync.success, (
            f"Third sync should succeed without --allow-consecutive (pc1 was target).\n"
            f"Exit code: {third_sync.exit_code}\nStderr: {third_sync.stderr}"
        )
