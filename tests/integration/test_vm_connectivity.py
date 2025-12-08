"""Integration tests for VM connectivity and command execution.

Tests User Story 2 acceptance scenarios:
- Basic command execution on both VMs
- Command failure handling
- Inter-VM SSH connectivity
"""

from __future__ import annotations

import pytest

from pcswitcher.executor import RemoteExecutor


async def test_basic_command_execution_pc1(pc1_executor: RemoteExecutor) -> None:
    """Test basic command execution on pc1.

    Verifies that we can execute a simple command and get the expected output.
    """
    result = await pc1_executor.run_command("echo 'hello from pc1'")

    assert result.success, f"Command failed: {result.stderr}"
    assert result.exit_code == 0
    assert "hello from pc1" in result.stdout
    assert result.stderr == ""


async def test_basic_command_execution_pc2(pc2_executor: RemoteExecutor) -> None:
    """Test basic command execution on pc2.

    Verifies that we can execute a simple command on pc2 independently.
    """
    result = await pc2_executor.run_command("echo 'hello from pc2'")

    assert result.success, f"Command failed: {result.stderr}"
    assert result.exit_code == 0
    assert "hello from pc2" in result.stdout
    assert result.stderr == ""


async def test_command_with_stdout_and_stderr(pc1_executor: RemoteExecutor) -> None:
    """Test that stdout and stderr are captured separately."""
    result = await pc1_executor.run_command("echo 'to stdout' && echo 'to stderr' >&2")

    assert result.success
    assert "to stdout" in result.stdout
    assert "to stderr" in result.stderr


async def test_command_failure_nonzero_exit(pc1_executor: RemoteExecutor) -> None:
    """Test command failure handling with non-zero exit code.

    Per spec FR-003a, we must verify failure paths. This ensures that
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


async def test_inter_vm_connectivity_pc1_to_pc2(
    pc1_executor: RemoteExecutor,
    pc2_executor: RemoteExecutor,
) -> None:
    """Test SSH connectivity from pc1 to pc2.

    This is a critical acceptance scenario from User Story 2. Verifies that
    pc1 can successfully SSH to pc2, which is required for the sync operation.
    """
    # First, get pc2's hostname/IP to connect to
    pc2_hostname_result = await pc2_executor.run_command("hostname -I | awk '{print $1}'")
    assert pc2_hostname_result.success
    pc2_ip = pc2_hostname_result.stdout.strip()
    assert len(pc2_ip) > 0

    # From pc1, SSH to pc2 and execute a simple command
    # Use StrictHostKeyChecking=no for test environment
    ssh_cmd = (
        f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null testuser@{pc2_ip} 'echo inter-vm-success'"
    )
    result = await pc1_executor.run_command(ssh_cmd, timeout=10.0)

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
