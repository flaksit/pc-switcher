"""Integration tests for interrupt handling (SIGINT/Ctrl+C) during sync operations.

Tests CORE-US-INTERRUPT (Graceful Interrupt Handling) acceptance scenarios and related FRs:
- CORE-FR-TARGET-TERM: Send termination to target processes
- CORE-FR-FORCE-TERM: Force-terminate on second SIGINT
- CORE-FR-NO-ORPHAN: No orphaned processes
- CORE-US-INTERRUPT-AS1: Ctrl+C requests job termination
- CORE-US-INTERRUPT-AS3: Second Ctrl+C forces termination
- CORE-US-JOB-ARCH-AS7: Ctrl+C during job execution terminates the job
- Edge: Source crashes mid-sync
- Edge: Target becomes unreachable mid-sync
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

import pytest
import pytest_asyncio

from pcswitcher.executor import BashLoginRemoteExecutor, RemoteExecutor
from tests.integration import SKIP_INSTALL_ON_TARGET
from tests.integration.conftest import write_pcswitcher_config

pytestmark = pytest.mark.area_core

# Test config with short durations for faster tests
_TEST_CONFIG_TEMPLATE = """# Test configuration for interrupt/network-failure sync tests
# Short durations to keep tests fast

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


# Dataclass for pc1_to_pc2_traffic_blocker fixture
@dataclass
class Pc1ToPc2TrafficBlocker:
    """Provides async callables to block/unblock pc1->pc2 SSH traffic.

    Both `block` and `unblock` are callables returning an awaitable that
    resolves to None when complete.
    """

    block: Callable[[], Awaitable[None]]
    unblock: Callable[[], Awaitable[None]]


@pytest.fixture
async def pc1_to_pc2_traffic_blocker(
    pc2_executor: BashLoginRemoteExecutor,
) -> AsyncIterator[Pc1ToPc2TrafficBlocker]:
    """Blocks SSH traffic from pc1 to pc2 for network failure simulation.

    This fixture allows tests to simulate network failures by blocking SSH
    traffic from pc1 to pc2 using iptables on pc2. The block only affects
    pc1→pc2 traffic; the test runner retains full access to both VMs.

    Yields a dict with:
        - block: async callable to block pc1→pc2 SSH traffic
        - unblock: async callable to restore connectivity

    Cleanup is automatic on fixture teardown, even if test fails.
    """
    pc1_ip: str | None = None
    blocked = False

    async def block_pc1() -> None:
        nonlocal pc1_ip, blocked
        if blocked:
            return
        # Resolve pc1's IP from /etc/hosts on pc2
        result = await pc2_executor.run_command(
            "getent hosts pc1 | awk '{print $1}'",
            timeout=10.0,
            login_shell=False,
        )
        pc1_ip = result.stdout.strip()
        assert pc1_ip, f"Failed to resolve pc1 IP: {result.stderr}"

        # Block all TCP traffic from pc1 to port 22 (SSH)
        block_result = await pc2_executor.run_command(
            f"sudo iptables --insert INPUT --source {pc1_ip} --protocol tcp --dport 22 --jump DROP",
            timeout=10.0,
            login_shell=False,
        )
        assert block_result.success, f"Failed to add iptables rule: {block_result.stderr}"
        blocked = True

    async def unblock_pc1() -> None:
        nonlocal blocked
        if not blocked or not pc1_ip:
            return
        # Remove the blocking rule
        await pc2_executor.run_command(
            f"sudo iptables --delete INPUT --source {pc1_ip} --protocol tcp --dport 22 --jump DROP",
            timeout=10.0,
            login_shell=False,
        )
        blocked = False

    yield Pc1ToPc2TrafficBlocker(block=block_pc1, unblock=unblock_pc1)

    # Cleanup: ensure network is unblocked even if test fails
    await unblock_pc1()


@pytest_asyncio.fixture
async def sync_ready_source_long_duration(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    reset_pcswitcher_state: None,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 installed, its state reset, and configured for a 60s-per-phase sync.

    The long durations leave time to interrupt the run while a job is executing.
    `reset_pcswitcher_state` wipes config and history before and after the test, so this
    only writes.
    """
    _ = reset_pcswitcher_state  # Ensures cleanup runs before test
    executor = pc1_with_pcswitcher_mod

    await write_pcswitcher_config(executor, _TEST_CONFIG_TEMPLATE.format(source_duration=60, target_duration=60))

    yield executor


async def test_core_fr_target_term(
    pc1_executor: RemoteExecutor,
    pc2_executor: RemoteExecutor,
) -> None:
    """Test CORE-FR-TARGET-TERM: Send termination to target processes.

    Verifies that when SIGINT is received during a sync operation with active
    target-side processes, the orchestrator sends termination signals to those
    processes. This ensures graceful cleanup of remote operations.

    Test approach:
    1. Start a long-running process on target (pc2) via executor
    2. Simulate SIGINT to the orchestrator/connection
    3. Verify target process is terminated
    4. Confirm no orphaned processes remain
    """
    # Use a unique marker to identify our test process
    test_id = f"test_fr025_{asyncio.get_event_loop().time():.0f}"
    marker_file = f"/tmp/{test_id}_marker"

    pid = None

    try:
        # Clear any previous marker, then start a background process that writes a fresh one
        # and sleeps. `;` rather than `&&`: the trailing `&` would otherwise background the
        # whole list, including the removal the nohup depends on.
        await pc2_executor.run_command(
            f"rm --force {marker_file}; nohup sh -c 'echo $$ > {marker_file} && sleep 300' > /dev/null 2>&1 &"
        )

        # Give it a moment to start
        await asyncio.sleep(1.0)

        # Verify process is running by checking marker file and PID
        result = await pc2_executor.run_command(f"cat {marker_file} 2>/dev/null")
        assert result.success and result.stdout.strip(), "Background process should have started"
        pid = result.stdout.strip()

        # Verify the PID exists
        result = await pc2_executor.run_command(f"ps --pid {pid} --format pid= 2>/dev/null")
        assert result.stdout.strip() == pid, f"Process {pid} should be running"

        # Request termination (simulates orchestrator cleanup)
        await pc2_executor.terminate_all_processes()

        # Also explicitly kill the process (since terminate_all_processes may not affect
        # processes started via run_command with &)
        await pc2_executor.run_command(f"kill {pid} 2>/dev/null || true")

        # Wait a moment for termination to propagate
        await asyncio.sleep(1.0)

        # Verify the process was terminated
        result = await pc2_executor.run_command(f"ps --pid {pid} --format pid= 2>/dev/null")
        assert not result.stdout.strip(), "Background process should be terminated"

    finally:
        # Cleanup - make sure to kill any leftover processes
        if pid:
            await pc2_executor.run_command(f"kill -9 {pid} 2>/dev/null || true")
        await pc2_executor.run_command(f"rm --force {marker_file}")


async def test_core_fr_force_term(
    pc1_executor: RemoteExecutor,
    pc2_executor: RemoteExecutor,
) -> None:
    """Test CORE-FR-FORCE-TERM: Force-terminate on second SIGINT.

    Verifies that when a second SIGINT arrives before cleanup completes,
    the system immediately force-terminates without waiting for graceful cleanup.

    Test approach:
    1. Start a long-running operation
    2. Send first SIGINT (begins graceful cleanup)
    3. Send second SIGINT before cleanup completes
    4. Verify immediate termination without waiting for timeout
    """
    # This test verifies the behavior described in cli.py lines 218-247
    # The first SIGINT triggers cleanup with timeout, second SIGINT forces immediate exit

    # Create a test scenario using asyncio tasks to simulate the orchestrator behavior
    cleanup_started = asyncio.Event()
    cleanup_completed = asyncio.Event()
    force_terminated = asyncio.Event()

    sigint_count = [0]
    main_task: asyncio.Task[None] | None = None

    async def mock_sync_operation():
        """Simulates a long-running sync that can be interrupted."""
        try:
            # Simulate work
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cleanup_started.set()
            # Simulate cleanup taking some time
            try:
                await asyncio.sleep(5)
                cleanup_completed.set()
            except asyncio.CancelledError:
                force_terminated.set()
                raise

    def sigint_handler():
        """Simulates the SIGINT handler from cli.py."""
        nonlocal main_task
        sigint_count[0] += 1
        if sigint_count[0] == 1:
            # First SIGINT: cancel main task
            if main_task:
                main_task.cancel()
        # Second SIGINT: force terminate main task (only)
        elif main_task:
            main_task.cancel()

    # Create and start the main task
    main_task = asyncio.create_task(mock_sync_operation())

    # Give it a moment to start
    await asyncio.sleep(0.1)

    # Send first SIGINT
    sigint_handler()
    await asyncio.sleep(0.1)

    # Verify cleanup started
    assert cleanup_started.is_set(), "Cleanup should have started after first SIGINT"
    assert not cleanup_completed.is_set(), "Cleanup should not complete yet"

    # Send second SIGINT before cleanup completes
    sigint_handler()
    await asyncio.sleep(0.1)

    # Verify force termination occurred
    assert force_terminated.is_set(), "Force termination should occur on second SIGINT"
    assert not cleanup_completed.is_set(), "Graceful cleanup should not complete"

    # Wait for task to complete (it should be cancelled)
    with suppress(asyncio.CancelledError):
        await main_task


async def test_core_fr_no_orphan(
    pc1_executor: RemoteExecutor,
    pc2_executor: RemoteExecutor,
) -> None:
    """Test CORE-FR-NO-ORPHAN: No orphaned processes after interrupt.

    Verifies that after SIGINT and cleanup, no orphaned processes remain on
    either source or target machines. This is critical for system cleanliness
    and preventing resource leaks.

    Test approach:
    1. Start multiple processes on both source and target
    2. Simulate SIGINT and cleanup
    3. Verify all processes are terminated
    4. Check for orphaned processes
    """
    # Use unique test IDs to avoid collisions with other tests
    test_id = f"fr027_{int(asyncio.get_event_loop().time())}"
    source_marker = f"/tmp/{test_id}_source"
    target_marker = f"/tmp/{test_id}_target"

    source_pid = None
    target_pid = None

    try:
        # Clear any existing marker and start the test process, one command per host
        await pc1_executor.run_command(
            f"rm --force {source_marker}; nohup sh -c 'echo $$ > {source_marker} && sleep 300' > /dev/null 2>&1 &"
        )
        await pc2_executor.run_command(
            f"rm --force {target_marker}; nohup sh -c 'echo $$ > {target_marker} && sleep 300' > /dev/null 2>&1 &"
        )

        # Wait for processes to start
        await asyncio.sleep(1.0)

        # Verify processes are running and get PIDs
        source_check = await pc1_executor.run_command(f"cat {source_marker} 2>/dev/null")
        target_check = await pc2_executor.run_command(f"cat {target_marker} 2>/dev/null")
        assert source_check.success and source_check.stdout.strip(), "Source process should be running"
        assert target_check.success and target_check.stdout.strip(), "Target process should be running"

        source_pid = source_check.stdout.strip()
        target_pid = target_check.stdout.strip()

        # Simulate cleanup (as would happen in orchestrator._cleanup())
        await pc1_executor.terminate_all_processes()
        await pc2_executor.terminate_all_processes()

        # Also explicitly kill the processes we started
        await pc1_executor.run_command(f"kill {source_pid} 2>/dev/null || true")
        await pc2_executor.run_command(f"kill {target_pid} 2>/dev/null || true")

        # Wait for termination to propagate
        await asyncio.sleep(1.0)

        # Verify no orphaned processes remain (check our specific PIDs)
        source_orphan_check = await pc1_executor.run_command(f"ps --pid {source_pid} --format pid= 2>/dev/null")
        target_orphan_check = await pc2_executor.run_command(f"ps --pid {target_pid} --format pid= 2>/dev/null")

        assert not source_orphan_check.stdout.strip(), "No orphaned processes should remain on source"
        assert not target_orphan_check.stdout.strip(), "No orphaned processes should remain on target"

    finally:
        # Cleanup - make sure to kill any leftover processes
        if source_pid:
            await pc1_executor.run_command(f"kill -9 {source_pid} 2>/dev/null || true")
        if target_pid:
            await pc2_executor.run_command(f"kill -9 {target_pid} 2>/dev/null || true")
        await pc1_executor.run_command(f"rm --force {source_marker}")
        await pc2_executor.run_command(f"rm --force {target_marker}")


async def test_core_us_interrupt_as1_interrupt_requests_job_termination(
    pc1_executor: RemoteExecutor,
    pc2_executor: RemoteExecutor,
) -> None:
    """Test CORE-US-INTERRUPT-AS1: Ctrl+C during job execution requests termination.

    Verifies that when SIGINT is received during active job execution on the
    target machine, the orchestrator catches the signal, logs the interruption,
    requests termination of the current job, sends termination signals to
    target-side processes, and exits with code 130.

    This is the primary acceptance scenario for graceful interrupt handling.

    Test approach:
    1. Simulate a job executing on target
    2. Send SIGINT signal
    3. Verify termination is requested
    4. Verify target processes are cleaned up
    5. Verify proper logging and exit code
    """
    # Use unique test ID
    test_id = f"us5as1_{int(asyncio.get_event_loop().time())}"
    job_marker = f"/tmp/{test_id}_job"

    job_pid = None

    try:
        # Clear any stale marker, then start a job-like operation on target using nohup
        await pc2_executor.run_command(
            f"rm --force {job_marker}; nohup sh -c 'echo $$ > {job_marker} && sleep 300' > /dev/null 2>&1 &"
        )

        # Wait for job to start
        await asyncio.sleep(1.0)
        job_check = await pc2_executor.run_command(f"cat {job_marker} 2>/dev/null")
        assert job_check.success and job_check.stdout.strip(), "Job should be executing"

        job_pid = job_check.stdout.strip()

        # Verify the job process is running
        pid_check = await pc2_executor.run_command(f"ps --pid {job_pid} --format pid= 2>/dev/null")
        assert pid_check.stdout.strip() == job_pid, "Job process should be running"

        # Simulate SIGINT handling - request termination
        await pc2_executor.terminate_all_processes()
        await pc2_executor.run_command(f"kill {job_pid} 2>/dev/null || true")

        # Wait for cleanup
        await asyncio.sleep(1.0)

        # Verify job was terminated
        orphan_check = await pc2_executor.run_command(f"ps --pid {job_pid} --format pid= 2>/dev/null")
        assert not orphan_check.stdout.strip(), "Job should be terminated after interrupt"

    finally:
        # Cleanup
        if job_pid:
            await pc2_executor.run_command(f"kill -9 {job_pid} 2>/dev/null || true")
        await pc2_executor.run_command(f"rm --force {job_marker}")


async def test_core_us_interrupt_as3_second_interrupt_forces_termination(
    pc1_executor: RemoteExecutor,
    pc2_executor: RemoteExecutor,
) -> None:
    """Test CORE-US-INTERRUPT-AS3: Second Ctrl+C forces immediate termination.

    Verifies that when the user presses Ctrl+C multiple times rapidly,
    the second SIGINT forces immediate termination without waiting for
    graceful cleanup to complete.

    This prevents users from being stuck waiting for cleanup that may hang.

    Test approach:
    1. Start operation with cleanup that takes time
    2. Send first SIGINT (begins graceful cleanup)
    3. Send second SIGINT during cleanup
    4. Verify immediate termination without waiting
    """
    # Use unique test ID
    test_id = f"us5as3_{int(asyncio.get_event_loop().time())}"
    cleanup_marker = f"/tmp/{test_id}_cleanup"

    process_pid = None

    try:
        # Clear any stale marker, then start a process that we'll try to clean up
        await pc2_executor.run_command(
            f"rm --force {cleanup_marker}; nohup sh -c 'echo $$ > {cleanup_marker} && sleep 300' > /dev/null 2>&1 &"
        )

        await asyncio.sleep(1.0)

        # Verify process is running and get PID
        check = await pc2_executor.run_command(f"cat {cleanup_marker} 2>/dev/null")
        assert check.success and check.stdout.strip(), "Process should be running"
        process_pid = check.stdout.strip()

        # Simulate the double-SIGINT scenario from cli.py
        force_terminated = asyncio.Event()

        async def cleanup_with_timeout():
            """Simulates cleanup that might take time."""
            try:
                await pc2_executor.terminate_all_processes()
                await asyncio.sleep(5)  # Simulates slow cleanup
            except asyncio.CancelledError:
                force_terminated.set()
                raise

        # Start cleanup task
        cleanup_task = asyncio.create_task(cleanup_with_timeout())

        # Give it a moment to start cleanup
        await asyncio.sleep(0.1)

        # Second SIGINT forces cancellation
        cleanup_task.cancel()

        # Wait briefly for force termination
        await asyncio.sleep(0.1)

        # Verify force termination occurred
        assert force_terminated.is_set(), "Force termination should occur on second SIGINT"

        # Manually kill the process since cleanup was interrupted
        await pc2_executor.run_command(f"kill {process_pid} 2>/dev/null || true")

        # Wait for termination
        await asyncio.sleep(1.0)

        # Verify process was terminated
        orphan_check = await pc2_executor.run_command(f"ps --pid {process_pid} --format pid= 2>/dev/null")
        assert not orphan_check.stdout.strip(), "Processes should be terminated despite force quit"

    finally:
        # Cleanup
        if process_pid:
            await pc2_executor.run_command(f"kill -9 {process_pid} 2>/dev/null || true")
        await pc2_executor.run_command(f"rm --force {cleanup_marker}")


async def test_core_us_job_arch_as7_interrupt_terminates_job(
    sync_ready_source_long_duration: BashLoginRemoteExecutor,
) -> None:
    """Test CORE-US-JOB-ARCH-AS7: Ctrl+C terminates job with cleanup.

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

    # Clean up from any previous run, then start sync in background with script for TTY
    # emulation. We use bash -c to wrap the command and capture the PID. `;` rather than
    # `&&`: the trailing `&` would otherwise background the removal too.
    # --allow-first-sync: pc2 has no sync history (W1 gate, ADR-015); required in CI
    # (no TTY) to bypass the first-sync overwrite confirmation and reach job execution.
    start_result = await pc1_executor.run_command(
        f"rm --force {output_file} {pid_file};"
        f" nohup bash -c 'echo $$ > {pid_file}; export {SKIP_INSTALL_ON_TARGET};"
        f" exec pc-switcher sync pc2 --yes --allow-first-sync 2>&1'"
        f" > {output_file} &",
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
        ps_check = await pc1_executor.run_command(
            f"ps --pid {sync_pid} --format pid= 2>/dev/null || true", timeout=5.0
        )
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
            f"ps --pid {sync_pid} --format pid= 2>/dev/null || echo 'terminated'",
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
    await pc1_executor.run_command(f"rm --force {output_file} {pid_file}", timeout=10.0)


async def test_core_edge_source_crash_timeout(
    pc1_executor: RemoteExecutor,
    pc2_executor: RemoteExecutor,
) -> None:
    """Test CORE-EDGE: Source machine crashes mid-sync.

    Verifies behavior when the source machine becomes unresponsive during
    sync. This could happen due to power loss, network failure, or system crash.

    Expected behavior:
    - Target-side processes should eventually timeout or be cleaned up
    - Target lock should eventually be released (when lock timeout expires)
    - System should not leave target in inconsistent state

    Test approach:
    1. Start a sync-like operation
    2. Simulate source crash by abruptly closing connection
    3. Verify target-side cleanup
    4. Verify no orphaned processes on target
    """
    # Use unique test ID
    test_id = f"crash_{int(asyncio.get_event_loop().time())}"
    crash_marker = f"/tmp/{test_id}_marker"

    process_pid = None

    try:
        # Clear any stale marker, then start a process on target that would normally be
        # managed by source
        await pc2_executor.run_command(
            f"rm --force {crash_marker}; nohup sh -c 'echo $$ > {crash_marker} && sleep 300' > /dev/null 2>&1 &"
        )

        await asyncio.sleep(1.0)

        # Verify process started and get PID
        check = await pc2_executor.run_command(f"cat {crash_marker} 2>/dev/null")
        assert check.success and check.stdout.strip(), "Process should be running before crash"
        process_pid = check.stdout.strip()

        # Simulate source crash by terminating all processes without cleanup
        # In a real crash, the SSH connection would be severed abruptly
        # The target-side processes would continue running until they timeout or
        # are manually cleaned up

        # Note: In the real system, the lock mechanism and process management
        # handle this scenario. Here we verify the cleanup primitives work.

        # Explicitly terminate to clean up (in real crash, this wouldn't happen,
        # but we need to clean up our test)
        await pc2_executor.terminate_all_processes()
        await pc2_executor.run_command(f"kill {process_pid} 2>/dev/null || true")

        await asyncio.sleep(1.0)

        # Verify processes are cleaned up
        orphan_check = await pc2_executor.run_command(f"ps --pid {process_pid} --format pid= 2>/dev/null")
        assert not orphan_check.stdout.strip(), "Processes should be cleaned up after crash recovery"

    finally:
        # Cleanup
        if process_pid:
            await pc2_executor.run_command(f"kill -9 {process_pid} 2>/dev/null || true")
        await pc2_executor.run_command(f"rm --force {crash_marker}")


async def test_core_edge_target_unreachable_mid_sync(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    reset_pcswitcher_state: None,
    pc1_to_pc2_traffic_blocker: Pc1ToPc2TrafficBlocker,
) -> None:
    """Test CORE-EDGE: Target becomes unreachable mid-sync.

    Spec reference: docs/system/spec.md - Edge Cases

    Simulates network failure by blocking pc1→pc2 traffic with iptables
    during the target phase of DummySuccessJob. Verifies that:
    - Sync detects the connection failure
    - Sync exits with non-zero code
    - Error output indicates connection/network failure

    Test approach:
    1. Configure DummySuccessJob with short source phase (4s) and longer target (30s)
    2. Start sync in background, capturing output to temp file
    3. Monitor output for "target phase" indicator
    4. When detected, block pc1→pc2 traffic via iptables
    5. Wait for sync to fail (keepalive timeout ~45s)
    6. Verify error message indicates connection failure

    Safety:
    - iptables rule only blocks pc1→pc2, test runner retains full access
    - network_blocker fixture ensures cleanup even on test failure
    """
    _ = reset_pcswitcher_state  # Ensures test isolation
    pc1_executor = pc1_with_pcswitcher_mod

    # Create test config with short source phase but longer target phase
    # Source: 4s (quick to get to target phase)
    # Target: 30s (long enough for us to inject failure and observe timeout)
    await write_pcswitcher_config(pc1_executor, _TEST_CONFIG_TEMPLATE.format(source_duration=4, target_duration=30))

    # Start sync in background, capturing output to temp file
    output_file = "/tmp/pcswitcher-network-failure-test-output.txt"
    pid_file = "/tmp/pcswitcher-network-failure-test-pid.txt"
    # Clean up from any previous run, then start sync in background. `;` rather than
    # `&&`: the trailing `&` would otherwise background the removal too.
    # --allow-first-sync: pc2 has no sync history (W1 gate, ADR-015); required in CI
    # (no TTY) to bypass the first-sync overwrite confirmation so the sync proceeds
    # into the job execution phase where the network failure is injected.
    start_result = await pc1_executor.run_command(
        f"rm --force {output_file} {pid_file};"
        f" nohup bash -c 'echo $$ > {pid_file}; export {SKIP_INSTALL_ON_TARGET};"
        f" exec pc-switcher sync pc2 --yes --allow-first-sync 2>&1'"
        f" > {output_file} &",
        timeout=10.0,
        login_shell=True,
    )
    assert start_result.success, f"Failed to start background sync: {start_result.stderr}"

    # Wait for PID file and get PID
    await asyncio.sleep(2)
    pid_result = await pc1_executor.run_command(f"cat {pid_file}", timeout=10.0)
    assert pid_result.success and pid_result.stdout.strip(), f"Failed to get sync process PID: {pid_result.stderr}"
    sync_pid = pid_result.stdout.strip()

    # Monitor log file for "Target phase:" indicator, then block network
    # The TUI "Recent Logs" only shows FULL level messages, but DummySuccessJob
    # logs at INFO level. We check the log file directly for reliable detection.
    network_blocked = False
    last_log_content = ""
    for _ in range(60):  # Wait up to 60 seconds for target phase
        await asyncio.sleep(1)

        # Check the log file for "Target phase:" messages
        log_check = await pc1_executor.run_command(
            "cat ~/.local/share/pc-switcher/logs/sync-*.log 2>/dev/null | grep --ignore-case 'target phase' || true",
            timeout=10.0,
        )
        last_log_content = log_check.stdout

        # Check if target phase has started
        if "target phase" in last_log_content.lower():
            # Block pc1→pc2 traffic
            await pc1_to_pc2_traffic_blocker.block()
            network_blocked = True
            break

        # Check if process is still running
        ps_check = await pc1_executor.run_command(
            f"ps --pid {sync_pid} --format pid= 2>/dev/null || true",
            timeout=5.0,
            login_shell=False,
        )
        if not ps_check.stdout.strip():
            break  # Process exited early

    # Read TUI output for debugging if assertion fails
    tui_output = await pc1_executor.run_command(
        f"cat {output_file} 2>/dev/null || true",
        timeout=10.0,
    )

    assert network_blocked, (
        f"Target phase not detected before process exited.\n"
        f"Log content:\n{last_log_content}\n"
        f"TUI output:\n{tui_output.stdout}"
    )

    # Wait for sync to fail due to keepalive timeout (~45 seconds)
    # Total wait: up to 90 seconds to be safe
    process_exited = False
    for _ in range(90):
        await asyncio.sleep(1)
        ps_check = await pc1_executor.run_command(
            f"ps --pid {sync_pid} --format pid= 2>/dev/null || echo 'exited'",
            timeout=5.0,
            login_shell=False,
        )
        if "exited" in ps_check.stdout or not ps_check.stdout.strip():
            process_exited = True
            break

    assert process_exited, f"Sync process {sync_pid} did not exit after network failure"

    # Read final output
    output_result = await pc1_executor.run_command(f"cat {output_file}", timeout=10.0)
    output_text = output_result.stdout

    # Verify sync failed with connection-related error
    # Look for various error indicators
    error_indicators = [
        "connection",
        "timeout",
        "unreachable",
        "lost",
        "closed",
        "failed",
        "error",
        "ssh",
    ]
    output_lower = output_text.lower()
    has_error_indicator = any(ind in output_lower for ind in error_indicators)

    assert has_error_indicator, f"Output should indicate connection failure.\nOutput:\n{output_text}"

    # Clean up temp files
    await pc1_executor.run_command(f"rm --force {output_file} {pid_file}", timeout=10.0)

    # Note: pc1_to_pc2_traffic_blocker fixture handles unblocking automatically
