"""Integration test fixtures for pc-switcher.

These tests require VM infrastructure. They are skipped if environment
variables are not configured.

Fixtures provided:
- pc1_connection: SSH connection to pc1 test VM
- pc2_connection: SSH connection to pc2 test VM
- pc1_executor: BashLoginRemoteExecutor for pc1 (= RemoteExecutor with login shell environment)
- pc2_executor: BashLoginRemoteExecutor for pc2 (= RemoteExecutor with login shell environment)
- pc2_executor_without_pcswitcher_tool: pc2 executor with pc-switcher uninstalled (clean target)
- pc2_executor_with_old_pcswitcher_tool: pc2 executor with old pc-switcher version (upgrade testing)
- integration_session: Session-scoped fixture for VM provisioning and reset
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import socket
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import asyncssh
import pytest
import pytest_asyncio

from pcswitcher.executor import BashLoginRemoteExecutor, RemoteExecutor

REQUIRED_ENV_VARS = [
    "HCLOUD_TOKEN",
    "PC_SWITCHER_TEST_PC1_HOST",
    "PC_SWITCHER_TEST_PC2_HOST",
    "PC_SWITCHER_TEST_USER",
]

SCRIPTS_DIR = Path(__file__).parent / "scripts"
RESET_VM_SCRIPT = SCRIPTS_DIR / "reset-vm.sh"

BASELINE_AGE_WARNING_DAYS = 30


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-apply integration marker and check VM environment."""
    integration_marker = pytest.mark.integration
    integration_tests = []

    # Auto-apply integration marker to all tests in tests/integration/
    for item in items:
        if "/integration/" in str(item.fspath):
            item.add_marker(integration_marker)
            integration_tests.append(item)

    # Fail if integration tests are collected but VM environment not configured
    missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing_vars and integration_tests:
        pytest.exit(
            f"Integration tests require VM environment. "
            f"Missing: {', '.join(missing_vars)}. "
            f"Run unit tests only with: uv run pytest tests/unit tests/contract",
            1,
        )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def lock() -> AsyncIterator[None]:
    """Session-scoped fixture to manage integration test lock.

    Acquires the lock before tests and releases it after all tests complete.
    """
    if os.environ.get("PCSWITCHER_LOCK_HOLDER"):
        print("Integration test lock already acquired or acquired by parent process")
        yield
        return

    if not os.getenv("HCLOUD_TOKEN"):
        pytest.exit("HCLOUD_TOKEN not set, cannot acquire integration test lock", 1)

    holder = _generate_lock_holder()
    await _run_script("internal/lock.sh", "acquire", holder, check=True)
    os.environ["PCSWITCHER_LOCK_HOLDER"] = holder
    print(f"Acquired integration test lock with holder ID: {holder}:")

    try:
        yield
    finally:
        await _run_script("internal/lock.sh", "release", holder, check=True)
        if os.environ["PCSWITCHER_LOCK_HOLDER"] == holder:
            del os.environ["PCSWITCHER_LOCK_HOLDER"]
        print(f"Released integration test lock with holder ID {holder}")


def _generate_lock_holder() -> str:
    """Get a unique lock holder identifier for this test session.

    CRITICAL: Must be unique per invocation to prevent concurrent test runs
    on the same machine by the same user.

    The identifier is valid for Hetzner Cloud labels which only allow
    alphanumeric characters, underscores, and hyphens.

    Cached at module level to ensure consistency across fixture calls.
    """
    # Generate new ID (only once per pytest session)
    ci_job_id = os.getenv("CI_JOB_ID") or os.getenv("GITHUB_RUN_ID")
    if ci_job_id:
        holder_id = f"ci-{ci_job_id}-pytest"
    else:
        hostname = socket.gethostname()
        user = os.getenv("USER", "unknown")
        # Add random suffix to ensure uniqueness per invocation
        random_suffix = secrets.token_hex(3)  # 6 hex characters
        holder_id = f"{user}-{hostname}-pytest-{random_suffix}"

    return holder_id


async def _run_script(script_name: str | Path, *args: str, check: bool = True) -> tuple[int, str, str]:
    """Run an infrastructure script asynchronously."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        pytest.fail(f"Infrastructure script not found: {script_path}")

    proc = await asyncio.create_subprocess_exec(
        str(script_path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode(errors="ignore")
    stderr = stderr_bytes.decode(errors="ignore")

    if check and proc.returncode != 0:
        pytest.exit(
            f"\n{'=' * 70}\n"
            f"Script {script_name} failed\n"
            f"{'=' * 70}\n"
            f"stdout: {stdout if stdout else '-'}\n"
            f"stderr: {stderr if stderr else '-'}\n"
            f"{'=' * 70}\n",
            1,
        )

    return proc.returncode or 0, stdout, stderr


async def _reset_vms(pc1_host: str, pc2_host: str) -> None:
    """Reset both VMs in parallel for faster test setup.

    Both resets are allowed to complete before checking for errors. This prevents
    a failure on one VM from interrupting the other mid-reset (which could leave
    it in a corrupted state).

    Raises pytest.fail if either reset fails - this prevents tests from running
    against VMs in an inconsistent state.
    """
    # Create tasks without TaskGroup to prevent cancellation on exception
    pc1_task = asyncio.create_task(_run_script(RESET_VM_SCRIPT, pc1_host, check=False))
    pc2_task = asyncio.create_task(_run_script(RESET_VM_SCRIPT, pc2_host, check=False))

    # Wait for both tasks to complete, even if one raises an exception
    await asyncio.gather(pc1_task, pc2_task, return_exceptions=True)

    # Check results after both complete
    errors = []
    if pc1_task.exception():
        errors.append(f"pc1 reset failed with exception: {pc1_task.exception()}")
    else:
        pc1_rc, pc1_stdout, pc1_stderr = pc1_task.result()
        if pc1_rc != 0:
            errors.append(f"pc1 reset failed:\nstdout:\n{pc1_stdout}\nstderr:\n{pc1_stderr}")

    if pc2_task.exception():
        errors.append(f"pc2 reset failed with exception: {pc2_task.exception()}")
    else:
        pc2_rc, pc2_stdout, pc2_stderr = pc2_task.result()
        if pc2_rc != 0:
            errors.append(f"pc2 reset failed:\nstdout:\n{pc2_stdout}\nstderr:\n{pc2_stderr}")

    if errors:
        pytest.exit("\n".join(errors), 1)


@dataclass
class VMReadinessResult:
    """Result of VM readiness check."""

    ready: bool
    baseline_warnings: list[str]


async def _check_vms_ready(pc1_executor: RemoteExecutor, pc2_executor: RemoteExecutor) -> VMReadinessResult:
    """Check if test VMs are reachable and fully configured.

    Verifies:
    - SSH connectivity to both VMs
    - Baseline btrfs snapshots exist (implies user is configured)
    - Warns if baseline snapshots are older than 30 days
    """

    async def check_vm(vm_name: str, executor: RemoteExecutor) -> tuple[bool, str | None]:
        """Check single VM readiness and baseline age. Returns (ready, warning_or_none)."""
        try:
            result = await executor.run_command("sudo btrfs subvolume show /.snapshots/baseline/@")

            # Parse creation time from output (format: "Creation time: 2024-11-15 10:30:45")
            match = re.search(r"Creation time:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", result.stdout)
            if match:
                created_str = match.group(1)
                created_time = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                now = datetime.now(UTC)
                age_days = (now - created_time).days
                print(f"{vm_name} baseline snapshot age: {age_days} days")
                if age_days > BASELINE_AGE_WARNING_DAYS:
                    return (True, f"{vm_name} baseline is {age_days} days old")

            return (True, None)
        except Exception:
            return (False, None)

    # Check both VMs in parallel
    pc1_task = asyncio.create_task(check_vm("pc1", pc1_executor))
    pc2_task = asyncio.create_task(check_vm("pc2", pc2_executor))

    pc1_result, pc2_result = await asyncio.gather(pc1_task, pc2_task)

    ready = pc1_result[0] and pc2_result[0]
    baseline_warnings = [w for w in [pc1_result[1], pc2_result[1]] if w]

    return VMReadinessResult(ready=ready, baseline_warnings=baseline_warnings)


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def integration_session(lock: None):
    """Session-scoped fixture for VM provisioning and reset.

    This fixture:
    1. Checks if VMs are ready
    2. Resets VMs to baseline
    3. Checks baseline age and warns if outdated

    Lock acquisition is handled by pytest_sessionstart hook before this fixture runs.
    """
    pc1_host = os.environ["PC_SWITCHER_TEST_PC1_HOST"]
    pc2_host = os.environ["PC_SWITCHER_TEST_PC2_HOST"]
    user = os.environ["PC_SWITCHER_TEST_USER"]

    # Create connections and executors for VM readiness check and baseline age check
    async with (
        asyncssh.connect(pc1_host, username=user) as pc1_conn,
        asyncssh.connect(pc2_host, username=user) as pc2_conn,
    ):
        pc1_executor = RemoteExecutor(pc1_conn)
        pc2_executor = RemoteExecutor(pc2_conn)

        # Check if VMs are ready and baseline age
        print("Checking if test VMs are provisioned and ready...")
        result = await _check_vms_ready(pc1_executor, pc2_executor)
        if not result.ready:
            pytest.exit(
                "Test VMs are not provisioned yet. Run the GitHub workflow 'Integration Tests' to do this.",
                1,
            )
        print("Test VMs are ready.")

        # Warn if baseline snapshots are outdated
        if result.baseline_warnings:
            combined_warning = "; ".join(result.baseline_warnings)
            warnings.warn(
                f"Test VM baselines are outdated: {combined_warning}. "
                f"Consider running ./tests/integration/scripts/upgrade-vms.sh",
                UserWarning,
                stacklevel=1,
            )

        # Reset VMs to baseline (in parallel for faster setup)
        print("Resetting test VMs to baseline snapshots...")
        await _reset_vms(pc1_host, pc2_host)
        print("Test VMs reset complete.")


@pytest.fixture(scope="module")
async def _pc1_connection(integration_session: None) -> AsyncIterator[asyncssh.SSHClientConnection]:  # pyright: ignore[reportUnusedFunction]
    """SSH connection to pc1 test VM.

    Module-scoped: shared across all tests in a module for efficiency.
    Each test module gets its own connection instance.

    Uses default ~/.ssh/known_hosts - key is established by reset-vm.sh via ssh_accept_new.
    """
    host = os.environ["PC_SWITCHER_TEST_PC1_HOST"]
    user = os.environ["PC_SWITCHER_TEST_USER"]

    async with asyncssh.connect(host, username=user) as conn:
        yield conn


@pytest.fixture(scope="module")
async def _pc2_connection(integration_session: None) -> AsyncIterator[asyncssh.SSHClientConnection]:  # pyright: ignore[reportUnusedFunction]
    """SSH connection to pc2 test VM.

    Module-scoped: shared across all tests in a module for efficiency.
    Each test module gets its own connection instance.

    Uses default ~/.ssh/known_hosts - key is established by reset-vm.sh via ssh_accept_new.
    """
    host = os.environ["PC_SWITCHER_TEST_PC2_HOST"]
    user = os.environ["PC_SWITCHER_TEST_USER"]

    async with asyncssh.connect(host, username=user) as conn:
        yield conn


@pytest.fixture(scope="module")
async def pc1_executor(_pc1_connection: asyncssh.SSHClientConnection) -> BashLoginRemoteExecutor:
    """Executor for running commands on pc1 with login shell enabled by default.

    Module-scoped: shared across all tests in a module.
    Tests must clean up their own artifacts and not modify executor state.

    Returns BashLoginRemoteExecutor which wraps all commands in bash login shell,
    ensuring PATH includes ~/.local/bin for user-installed tools (uv, pc-switcher).
    Commands use login_shell=True by default but can be overridden with login_shell=False
    for system commands.
    """
    return BashLoginRemoteExecutor(_pc1_connection)


# Install script URL from main branch
_INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh"


@pytest.fixture(scope="module")
async def pc1_with_pcswitcher(pc1_executor: BashLoginRemoteExecutor) -> BashLoginRemoteExecutor:
    """Ensure pc-switcher is installed on pc1.

    Module-scoped: installs pc-switcher once per test module if not already present.
    Does NOT uninstall after tests - leaves pc-switcher installed for efficiency.

    Use this fixture when tests require pc-switcher to be available on pc1.
    """
    # Check if pc-switcher is already installed
    version_check = await pc1_executor.run_command("pc-switcher --version 2>/dev/null || true", timeout=10.0)

    if not version_check.success or "pc-switcher" not in version_check.stdout.lower():
        # Install pc-switcher
        result = await pc1_executor.run_command(
            f"curl -sSL {_INSTALL_SCRIPT_URL} | bash",
            timeout=120.0,
        )
        assert result.success, f"Failed to install pc-switcher on pc1: {result.stderr}"

        # Verify installation
        verify = await pc1_executor.run_command("pc-switcher --version", timeout=10.0)
        assert verify.success, f"pc-switcher not accessible after install: {verify.stderr}"

    return pc1_executor


@pytest.fixture(scope="module")
async def pc2_executor(_pc2_connection: asyncssh.SSHClientConnection) -> BashLoginRemoteExecutor:
    """Executor for running commands on pc2 with login shell enabled by default.

    Module-scoped: shared across all tests in a module.
    Tests must clean up their own artifacts and not modify executor state.

    Returns BashLoginRemoteExecutor which wraps all commands in bash login shell,
    ensuring PATH includes ~/.local/bin for user-installed tools (uv, pc-switcher).
    Commands use login_shell=True by default but can be overridden with login_shell=False
    for system commands.
    """
    return BashLoginRemoteExecutor(_pc2_connection)


@pytest.fixture
async def pc2_executor_without_pcswitcher_tool(
    pc2_executor: BashLoginRemoteExecutor,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide a clean environment on pc2 without pc-switcher installed.

    WARNING: This fixture wraps pc2_executor and modifies VM state by uninstalling
    pc-switcher. Tests using this fixture MUST NOT use pc2_executor directly in
    parallel, as both operate on the same VM and will interfere with each other.

    Removes pc-switcher installation but keeps test infrastructure intact.
    Useful for testing fresh installs on a clean target.

    Cleanup: Captures initial state and restores it after the test to avoid affecting
    other tests in the same test session.
    """
    # Check if pc-switcher was installed before we modify state
    version_check = await pc2_executor.run_command(
        "pc-switcher --version 2>/dev/null || true", timeout=10.0, login_shell=True
    )
    was_installed = version_check.success and "pc-switcher" in version_check.stdout.lower()

    # Uninstall pc-switcher if it exists
    await pc2_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0, login_shell=True)
    await pc2_executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)

    yield pc2_executor

    # Restore to initial state: if it was installed before, reinstall it
    if was_installed:
        install_script_url = "https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh"
        result = await pc2_executor.run_command(
            f"curl -sSL {install_script_url} | bash",
            timeout=120.0,
        )
        if not result.success:
            # Log but don't fail the test - cleanup issues shouldn't fail tests
            warnings.warn(f"Failed to restore pc-switcher on pc2: {result.stderr}", stacklevel=2)


@pytest.fixture
async def pc2_executor_with_old_pcswitcher_tool(
    pc2_executor: BashLoginRemoteExecutor,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc2 with an older version of pc-switcher (0.1.0-alpha.1).

    WARNING: This fixture wraps pc2_executor and modifies VM state by installing
    an older version of pc-switcher. Tests using this fixture MUST NOT use pc2_executor
    directly in parallel, as both operate on the same VM and will interfere with each
    other.

    Uninstalls current pc-switcher and installs version 0.1.0-alpha.1.
    Useful for testing upgrade scenarios.

    Cleanup: Captures initial state and restores it after the test to avoid affecting
    other tests in the same test session.
    """
    # Install script URL from main branch
    install_script_url = "https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh"

    # Check if pc-switcher was installed before we modify state
    version_check = await pc2_executor.run_command(
        "pc-switcher --version 2>/dev/null || true", timeout=10.0, login_shell=True
    )
    was_installed = version_check.success and "pc-switcher" in version_check.stdout.lower()

    # Uninstall and install older version
    await pc2_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0, login_shell=True)
    await pc2_executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)
    result = await pc2_executor.run_command(
        f"curl -sSL {install_script_url} | VERSION=0.1.0-alpha.1 bash",
        timeout=120.0,
    )
    assert result.success, f"Failed to install old version: {result.stderr}"

    yield pc2_executor

    # Restore to initial state
    await pc2_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0, login_shell=True)
    await pc2_executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)

    if was_installed:
        # Reinstall latest version
        result = await pc2_executor.run_command(
            f"curl -sSL {install_script_url} | bash",
            timeout=120.0,
        )
        if not result.success:
            # Log but don't fail the test - cleanup issues shouldn't fail tests
            warnings.warn(f"Failed to restore pc-switcher on pc2: {result.stderr}", stacklevel=2)
