"""Integration tests for VM connectivity and command execution.

Tests User Story 2 acceptance scenarios:
- Basic command execution on both VMs
- Command failure handling
- Inter-VM SSH connectivity
"""

from __future__ import annotations

import pytest

from pcswitcher.executor import RemoteExecutor


async def test_command_with_stdout_and_stderr(pc1_executor: RemoteExecutor) -> None:
    """Test that stdout and stderr are captured separately."""
    result = await pc1_executor.run_command("echo 'to stdout' && echo 'to stderr' >&2")

    assert result.success
    assert "to stdout" in result.stdout
    assert "to stderr" in result.stderr


async def test_command_failure_nonzero_exit(pc1_executor: RemoteExecutor) -> None:
    """Test command failure handling with non-zero exit code.

    Per spec TST-FR-CONTRACT, we must verify failure paths. This ensures that
    commands returning non-zero exit codes are properly reported.
    """
    result = await pc1_executor.run_command("exit 42")

    assert not result.success
    assert result.exit_code == 42


async def test_command_failure_invalid_command(pc1_executor: RemoteExecutor) -> None:
    """Test command failure with invalid command.

    Verifies that attempting to run a non-existent command results in
    a failure with appropriate error message.
    """
    result = await pc1_executor.run_command("/nonexistent/command/that/does/not/exist")

    assert not result.success
    assert result.exit_code != 0
    # Stderr should contain error information (e.g., "not found", "No such file")
    assert len(result.stderr) > 0


async def test_command_timeout(pc1_executor: RemoteExecutor) -> None:
    """Test that command timeout is enforced.

    Verifies that long-running commands can be terminated via timeout.
    """
    with pytest.raises(TimeoutError):
        await pc1_executor.run_command("sleep 10", timeout=0.5)


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


async def test_working_directory_isolation(pc1_executor: RemoteExecutor) -> None:
    """Test that each command runs in the user's home directory.

    Verifies that commands start in a predictable location.
    """
    result = await pc1_executor.run_command("pwd")

    assert result.success
    # Should be in home directory
    assert "/home/" in result.stdout or result.stdout.strip() == "~"


async def test_environment_variables(pc1_executor: RemoteExecutor) -> None:
    """Test that standard environment variables are available."""
    result = await pc1_executor.run_command("echo $USER:$HOME")

    assert result.success
    output = result.stdout.strip()
    # Should have both USER and HOME set
    assert ":" in output
    parts = output.split(":")
    assert len(parts[0]) > 0  # USER should be set
    assert len(parts[1]) > 0  # HOME should be set


async def test_multiline_output(pc1_executor: RemoteExecutor) -> None:
    """Test that multi-line output is captured correctly."""
    result = await pc1_executor.run_command("echo 'line1' && echo 'line2' && echo 'line3'")

    assert result.success
    assert "line1" in result.stdout
    assert "line2" in result.stdout
    assert "line3" in result.stdout
    # Verify order is preserved
    line1_pos = result.stdout.index("line1")
    line2_pos = result.stdout.index("line2")
    line3_pos = result.stdout.index("line3")
    assert line1_pos < line2_pos < line3_pos
