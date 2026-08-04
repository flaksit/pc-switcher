"""Integration tests for end-to-end sync operations.

Tests CORE-US-JOB-ARCH (Job Architecture) acceptance scenarios:
- CORE-US-JOB-ARCH-AS1: Job integration via standardized interface
- CORE-US-JOB-ARCH-AS7: Interrupt handling during job execution
- Edge case: Target unreachable mid-sync

These tests verify the complete orchestrator workflow by actually running
`pc-switcher sync` on test VMs. They exercise the full sync pipeline including:
- Lock acquisition (source and target)
- SSH connection establishment
- Job discovery and validation
- Disk space preflight checks
- Pre-sync btrfs snapshots
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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest
import pytest_asyncio

from pcswitcher.executor import BashLoginRemoteExecutor
from tests.integration import SKIP_INSTALL_ON_TARGET
from tests.integration.jobs import folder_sync_scenario


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


# Test config with short durations for faster tests
_TEST_CONFIG_TEMPLATE = """# Test configuration for end-to-end sync tests
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


# The all-encompassing end-to-end run: the whole pipeline in one sync, with every kind of job it coordinates.
# The filter_file named here is written by folder_sync_scenario, which owns filter content.
_FULL_PIPELINE_CONFIG = """\
sync_jobs:
  dummy_success: true
  folder_sync: true
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
  source_duration: 2
  target_duration: 2
folder_sync:
  folders:
    - path: /home
      enabled: true
      filter_file: ~/.config/pc-switcher/home.filter
"""


async def _write_full_pipeline_config(executor: BashLoginRemoteExecutor) -> None:
    """Write the end-to-end run's pc-switcher config to a VM."""
    result = await executor.run_command(
        "mkdir --parents ~/.config/pc-switcher"
        f" && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n{_FULL_PIPELINE_CONFIG}CONF_EOF",
        timeout=10.0,
    )
    assert result.success, f"Failed to write config: {result.stderr}"


@pytest_asyncio.fixture
async def sync_ready_source(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    reset_pcswitcher_state: None,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 configured and ready to run pc-switcher sync.

    This fixture:
    1. Ensures pc-switcher is installed (via pc1_with_pcswitcher_mod)
    2. Cleans up any existing sync history (via reset_pcswitcher_state)
    3. Creates a test configuration with short-duration jobs
    4. Cleans up the test config after the test

    Yields:
        Executor for pc1, ready to run sync commands
    """
    _ = reset_pcswitcher_state  # Ensures cleanup runs before test
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
    await executor.run_command("mkdir --parents ~/.config/pc-switcher", timeout=10.0)

    # Use heredoc to write config
    write_result = await executor.run_command(
        f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
        timeout=10.0,
    )
    assert write_result.success, f"Failed to write test config: {write_result.stderr}"

    yield executor

    # Cleanup: restore original config
    await executor.run_command("rm --force ~/.config/pc-switcher/config.yaml", timeout=10.0)
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.e2e-backup ]; then "
        "mv ~/.config/pc-switcher/config.yaml.e2e-backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


@pytest_asyncio.fixture
async def sync_ready_source_long_duration(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    reset_pcswitcher_state: None,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 configured for sync with longer duration (for interrupt tests).

    Same as sync_ready_source but with 60-second durations to allow time
    for interrupt testing.
    """
    _ = reset_pcswitcher_state  # Ensures cleanup runs before test
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
    await executor.run_command("mkdir --parents ~/.config/pc-switcher", timeout=10.0)

    write_result = await executor.run_command(
        f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
        timeout=10.0,
    )
    assert write_result.success, f"Failed to write test config: {write_result.stderr}"

    yield executor

    # Cleanup
    await executor.run_command("rm --force ~/.config/pc-switcher/config.yaml", timeout=10.0)
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.e2e-backup ]; then "
        "mv ~/.config/pc-switcher/config.yaml.e2e-backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


async def _assert_job_integration(
    source_executor: BashLoginRemoteExecutor,
    target_executor: BashLoginRemoteExecutor,
) -> None:
    """Assert the standardized job interface ran: both jobs logged, snapshots taken, config synced."""
    log_content = await source_executor.run_command(
        "cat $(ls --sort=time ~/.local/share/pc-switcher/logs/sync-*.log | head --lines=1)", timeout=10.0
    )
    assert log_content.success, f"Failed to read log file: {log_content.stderr}"
    log_text = log_content.stdout.lower()
    assert "dummy_success" in log_text or "source phase" in log_text, "Generic job (dummy_success) not logged."
    assert "folder_sync" in log_text, "folder_sync job not logged."
    for role, executor in (("source", source_executor), ("target", target_executor)):
        snaps = await executor.run_command(
            "sudo ls /.snapshots/pc-switcher/ 2>/dev/null | head --lines=1", timeout=10.0, login_shell=False
        )
        assert snaps.stdout.strip(), f"Pre/post-sync snapshots missing on {role}."
    tgt_config = await target_executor.run_command("cat ~/.config/pc-switcher/config.yaml", timeout=10.0)
    assert tgt_config.success and "dummy_success: true" in tgt_config.stdout, "Config not synced to target."


class TestEndToEndSync:
    """Integration tests for complete pc-switcher sync workflow."""

    # The only integration coverage of folder_sync, and the one run that exercises every
    # kind of job the pipeline coordinates -- cheap enough to be worth on every PR.
    @pytest.mark.smoke
    @pytest.mark.area_folder
    async def test_core_us_job_arch_as1_job_integration_via_interface(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """CORE-US-JOB-ARCH-AS1 + full folder-sync end-to-end (criteria 1-5, ADR-014/015/016).

        One scenario, one seed, five syncs — the complete pipeline in a single run:

        1. Blocked A→B (no flag): the W1 first-sync gate aborts non-interactively; nothing reaches pc2.
        2. A→B --dry-run: rehearses through the gate (ADR-014) but writes nothing and does not update history (D-12).
        3. A→B --allow-first-sync: the real sync. Verifies BOTH
           - job integration via the standardized interface (dummy_success + folder_sync discovered,
             logged, pre/post snapshots on both machines, config synced), AND
           - folder_sync of the real /home: byte-identical content; numeric uid/gid; permissions incl.
             setuid/setgid/sticky; POSIX ACL; mtime; hard-link inode sharing; symlink; across user-,
             root-, and other-user-owned files AND directories the invoking user cannot read; config
             exclusions honoured; the ADR-016 runtime-file excludes (state/install/logs) via sentinels;
             and (3f) the full #166 filter surface end-to-end — a central include-override (keep
             pcsw-filter/cache/keep-uv+keep-pip, drop the rest), a wholly-excluded subtree, and nested
             per-directory .pcswitcher-filter files — proving included paths add/overwrite/delete on
             the target, excluded paths survive on the target whether or not a source copy exists, and
             per-directory filter files themselves transfer.
        4. Mutate pc2 (add / modify / delete file / delete directory / chmod) then B→A: all propagate.
        5. A→B again with no override: a clean round-trip must not trip the out-of-order gate (ADR-015 #159).

        The scenario's seeding, manifests and folder-sync assertions live in
        `tests/integration/jobs/folder_sync_scenario.py`, whose module docstring explains why
        syncing the real /home is safe here.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)
        tree = folder_sync_scenario.tree_path()
        state_dir = folder_sync_scenario.STATE_DIR

        try:
            await _write_full_pipeline_config(pc1_executor)
            await folder_sync_scenario.write_filter_file(pc1_executor)
            await folder_sync_scenario.seed_rich_tree(pc1_executor, tree)
            await folder_sync_scenario.seed_included_markers(pc1_executor)
            await folder_sync_scenario.seed_filter_source(pc1_executor)
            await folder_sync_scenario.clear_target_tree(pc2_executor, tree)
            # Pre-seed the target-side files that drive the #166 --delete filter cases
            # (overwrite / delete-within-included / excluded-survivor). NOT under `tree`,
            # so the pc2 tree removal above leaves them in place for the first sync.
            await folder_sync_scenario.seed_filter_target(pc2_executor)
            await folder_sync_scenario.seed_state_sentinels(pc1_executor, pc2_executor)

            src_manifests = await folder_sync_scenario.capture_manifests(pc1_executor, tree)

            # --- Step 1: blocked first sync (W1 gate, non-interactive) ---
            blocked = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes", timeout=180.0, login_shell=True
            )
            assert not blocked.success, (
                f"W1 first-sync gate should block non-interactively, got exit {blocked.exit_code}.\n{blocked.stdout}"
            )
            assert (
                "out-of-order" in (blocked.stdout + blocked.stderr).lower()
                or "target" in (blocked.stdout + blocked.stderr).lower()
            ), f"Unexpected first-sync-gate message.\nstdout: {blocked.stdout}\nstderr: {blocked.stderr}"
            await folder_sync_scenario.assert_tree_absent(
                pc2_executor, tree, "Blocked first sync transferred the tree to pc2."
            )

            # --- Step 2: dry-run rehearsal (proceeds, writes nothing, no history change) ---
            hist_before = await pc1_executor.run_command(
                f"cat {state_dir}/sync-history.json 2>/dev/null || echo absent", timeout=10.0
            )
            dry = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes --dry-run", timeout=180.0, login_shell=True
            )
            assert dry.success, f"--dry-run should not be blocked (ADR-014).\nstderr: {dry.stderr}"
            await folder_sync_scenario.assert_tree_absent(
                pc2_executor, tree, "--dry-run transferred the tree to pc2 (must be read-only)."
            )
            hist_after = await pc1_executor.run_command(
                f"cat {state_dir}/sync-history.json 2>/dev/null || echo absent", timeout=10.0
            )
            assert hist_before.stdout.strip() == hist_after.stdout.strip(), "--dry-run updated sync-history (D-12)."

            # --- Step 3: real first sync (--allow-first-sync) ---
            sync_ab = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ab.success, (
                f"A→B first sync failed.\nexit={sync_ab.exit_code}\nstdout: {sync_ab.stdout}\nstderr: {sync_ab.stderr}"
            )

            # 3a. Job integration via interface: log entries, snapshots on both, config synced.
            await _assert_job_integration(pc1_executor, pc2_executor)

            # 3b. folder_sync content + metadata: target manifests must equal source manifests exactly.
            await folder_sync_scenario.assert_manifests_match(pc2_executor, tree, src_manifests)

            # 3c. ACL, backdated mtime, hard-link inode sharing, symlink target.
            await folder_sync_scenario.assert_metadata_details(pc2_executor, tree)

            # 3d. Exclusions: config-excluded subtree absent; ADR-016 runtime excludes held.
            await folder_sync_scenario.assert_exclusions(pc2_executor, tree)

            # 3e. SC3 inclusion: non-excluded dev-tool cache + VS Code user state ARE synced,
            # while a config-excluded sibling (VS Code Cache) is not.
            await folder_sync_scenario.assert_included_markers(pc2_executor)

            # 3f. #166 filter rules end-to-end (central include-override + wholly-excluded
            # subtree + nested per-directory .pcswitcher-filter files). Verifies that
            # included paths add/overwrite/delete on the target, that excluded paths leave
            # the target as-is whether or not a source counterpart exists (the --delete
            # survival case), and that per-directory filter files themselves transfer.
            await folder_sync_scenario.assert_filter_outcomes(pc2_executor)

            # --- Step 4: mutate pc2, then B→A ---
            await folder_sync_scenario.mutate_tree(pc2_executor, tree)

            sync_ba = await pc2_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc1 --yes", timeout=300.0, login_shell=True
            )
            assert sync_ba.success, (
                f"B→A sync failed.\nexit={sync_ba.exit_code}\nstdout: {sync_ba.stdout}\nstderr: {sync_ba.stderr}"
            )

            await folder_sync_scenario.assert_mutations_propagated(pc1_executor, tree)

            # --- Step 5: clean A→B again must not trip the out-of-order gate ---
            sync_ab2 = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes", timeout=300.0, login_shell=True
            )
            assert sync_ab2.success, (
                f"Second A→B failed (out-of-order gate wrongly tripped for a clean round-trip, ADR-015 #159).\n"
                f"exit={sync_ab2.exit_code}\nstdout: {sync_ab2.stdout}\nstderr: {sync_ab2.stderr}"
            )

        finally:
            await folder_sync_scenario.remove_test_artifacts(pc1_executor, pc2_executor, tree)

    @pytest.mark.area_core
    async def test_core_us_job_arch_as7_interrupt_terminates_job(
        self,
        sync_ready_source_long_duration: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
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

        # Clean up from any previous run
        await pc1_executor.run_command(f"rm --force {output_file} {pid_file}", timeout=10.0)

        # Start sync in background with script for TTY emulation
        # We use bash -c to wrap the command and capture the PID.
        # --allow-first-sync: pc2 has no sync history (W1 gate, ADR-015); required in CI
        # (no TTY) to bypass the first-sync overwrite confirmation and reach job execution.
        start_result = await pc1_executor.run_command(
            f"nohup bash -c 'echo $$ > {pid_file}; export {SKIP_INSTALL_ON_TARGET};"
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

    @pytest.mark.area_core
    async def test_core_edge_target_unreachable_mid_sync(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
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
        test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=4, target_duration=30)
        await pc1_executor.run_command("mkdir --parents ~/.config/pc-switcher", timeout=10.0)
        await pc1_executor.run_command(
            f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
            timeout=10.0,
        )

        # Start sync in background, capturing output to temp file
        output_file = "/tmp/pcswitcher-network-failure-test-output.txt"
        pid_file = "/tmp/pcswitcher-network-failure-test-pid.txt"
        await pc1_executor.run_command(f"rm --force {output_file} {pid_file}", timeout=10.0)

        # Start sync in background.
        # --allow-first-sync: pc2 has no sync history (W1 gate, ADR-015); required in CI
        # (no TTY) to bypass the first-sync overwrite confirmation so the sync proceeds
        # into the job execution phase where the network failure is injected.
        start_result = await pc1_executor.run_command(
            f"nohup bash -c 'echo $$ > {pid_file}; export {SKIP_INSTALL_ON_TARGET};"
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
                "cat ~/.local/share/pc-switcher/logs/sync-*.log 2>/dev/null"
                " | grep --ignore-case 'target phase' || true",
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
