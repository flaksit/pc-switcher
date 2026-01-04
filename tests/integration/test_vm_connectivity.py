"""Integration tests for VM connectivity verification.

Tests VM infrastructure validation:
- Hostname verification (ensure we're connected to correct VM)
- Inter-VM SSH connectivity (required for sync operations)

Note: Basic executor behavior tests (stdout/stderr, exit codes, timeouts,
environment variables) have been moved to tests/unit/executor/ as they
don't require VM infrastructure to validate.
"""

from __future__ import annotations

from pcswitcher.executor import RemoteExecutor


async def test_hostname_verification_pc1(pc1_executor: RemoteExecutor) -> None:
    """Test that we're connected to the correct VM (pc1).

    Verifies hostname to ensure we're talking to the right machine.
    """
    result = await pc1_executor.run_command("hostname")

    assert result.success
    hostname = result.stdout.strip()
    # Hostname should be set by VM provisioning, should not be empty
    assert len(hostname) > 0
    # Should not accidentally be pc2
    assert "pc2" not in hostname.lower()


async def test_hostname_verification_pc2(pc2_executor: RemoteExecutor) -> None:
    """Test that we're connected to the correct VM (pc2).

    Verifies hostname to ensure we're talking to the right machine.
    """
    result = await pc2_executor.run_command("hostname")

    assert result.success
    hostname = result.stdout.strip()
    # Hostname should be set by VM provisioning, should not be empty
    assert len(hostname) > 0
    # Should not accidentally be pc1
    assert "pc1" not in hostname.lower()


async def test_inter_vm_connectivity_pc1_to_pc2(pc1_executor: RemoteExecutor) -> None:
    """Test SSH connectivity from pc1 to pc2.

    This is a critical acceptance scenario from User Story 2. Verifies that
    pc1 can successfully SSH to pc2, which is required for the sync operation.
    """
    # Verify pc2 is reachable via hostname (configure-hosts.sh sets up /etc/hosts)
    pc2_ping_result = await pc1_executor.run_command("getent hosts pc2")
    assert pc2_ping_result.success, "pc2 hostname not resolvable from pc1"

    # From pc1, SSH to pc2 using hostname
    # VMs have /etc/hosts and known_hosts set up by configure-hosts.sh
    ssh_cmd = "ssh testuser@pc2 'echo inter-vm-success'"
    result = await pc1_executor.run_command(ssh_cmd, timeout=10.0)

    assert result.success, f"Inter-VM SSH failed: {result.stderr}"
    assert "inter-vm-success" in result.stdout


async def test_inter_vm_connectivity_pc2_to_pc1(pc2_executor: RemoteExecutor) -> None:
    """Test SSH connectivity from pc2 to pc1.

    Verifies that pc2 can successfully SSH to pc1, ensuring bidirectional
    connectivity between the VMs.
    """
    # Verify pc1 is reachable via hostname
    pc1_ping_result = await pc2_executor.run_command("getent hosts pc1")
    assert pc1_ping_result.success, "pc1 hostname not resolvable from pc2"

    # From pc2, SSH to pc1 using hostname
    ssh_cmd = "ssh testuser@pc1 'echo inter-vm-success'"
    result = await pc2_executor.run_command(ssh_cmd, timeout=10.0)

    assert result.success, f"Inter-VM SSH failed: {result.stderr}"
    assert "inter-vm-success" in result.stdout
