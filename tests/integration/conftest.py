"""Integration test fixtures for pc-switcher.

These tests require VM infrastructure. They are skipped if environment
variables are not configured.

Fixtures provided:
- pc1_connection: SSH connection to pc1 test VM
- pc2_connection: SSH connection to pc2 test VM
- pc1_executor: RemoteExecutor for pc1 (with login shell environment)
- pc2_executor: RemoteExecutor for pc2 (with login shell environment)
- integration_lock: Session-scoped lock for test isolation
- integration_session: Session-scoped fixture for VM provisioning and reset
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import socket
import warnings
from asyncio import TaskGroup
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import asyncssh
import pytest
import pytest_asyncio

from pcswitcher.executor import RemoteExecutor
from pcswitcher.models import CommandResult


class RemoteLoginBashExecutor(RemoteExecutor):
    """RemoteExecutor subclass that runs commands in a bash login shell.

    Remote commands via SSH run in non-login, non-interactive shells by default,
    which means ~/.profile isn't sourced and PATH may not include ~/.local/bin.

    This subclass ensures commands run with the proper user environment by
    wrapping them in `bash -l -c "..."`, which:
    - Sources /etc/profile and ~/.profile (login shell behavior)
    - Ensures PATH includes user-installed tools like uv and pc-switcher
    - Simulates what a real user would experience when SSH'ing in
    """

    async def run_command(
        self,
        cmd: str,
        timeout: float | None = None,
    ) -> CommandResult:
        """Run a command in a bash login shell environment."""
        wrapped_cmd = f"bash -l -c {shlex.quote(cmd)}"
        return await super().run_command(wrapped_cmd, timeout=timeout)

REQUIRED_ENV_VARS = [
    "HCLOUD_TOKEN",
    "PC_SWITCHER_TEST_PC1_HOST",
    "PC_SWITCHER_TEST_PC2_HOST",
    "PC_SWITCHER_TEST_USER",
]

SCRIPTS_DIR = Path(__file__).parent / "scripts"


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
        pytest.fail(
            f"Integration tests require VM environment. "
            f"Missing: {', '.join(missing_vars)}. "
            f"Run unit tests only with: uv run pytest tests/unit tests/contract"
        )


def _get_lock_holder() -> str:
    """Get a unique lock holder identifier for this test session."""
    ci_job_id = os.getenv("CI_JOB_ID") or os.getenv("GITHUB_RUN_ID")
    if ci_job_id:
        return f"ci-{ci_job_id}"
    hostname = socket.gethostname()
    return f"local-{os.getenv('USER', 'unknown')}@{hostname}"


async def _run_script(script_name: str, *args: str, check: bool = True) -> tuple[int, str, str]:
    """Run an infrastructure script asynchronously."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        pytest.fail(f"Infrastructure script not found: {script_path}")

    proc = await asyncio.create_subprocess_exec(
        str(script_path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HCLOUD_TOKEN": os.getenv("HCLOUD_TOKEN", "")},
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode()
    stderr = stderr_bytes.decode()

    if check and proc.returncode != 0:
        pytest.fail(f"Script {script_name} failed: {stderr}")

    return proc.returncode or 0, stdout, stderr


async def _reset_vms(pc1_host: str, pc2_host: str) -> None:
    """Reset both VMs in parallel for faster test setup."""
    async with TaskGroup() as tg:
        tg.create_task(_run_script("reset-vm.sh", pc1_host, check=False))
        tg.create_task(_run_script("reset-vm.sh", pc2_host, check=False))


async def _check_baseline_age(executor: RemoteExecutor) -> None:
    """Warn if baseline snapshots are older than 30 days.

    This is a non-blocking check to remind maintainers to upgrade VMs periodically.
    Baseline snapshots should be refreshed regularly with the upgrade-vms.sh script.
    """
    try:
        # Get baseline snapshot creation time using btrfs
        result = await executor.run_command(
            "sudo btrfs subvolume show /.snapshots/baseline/@ 2>/dev/null | grep 'Creation time:' | head -1"
        )

        if not result.stdout:
            # Can't determine age, skip warning
            return

        # Parse creation time (format: "Creation time: 2024-11-15 10:30:45")
        match = re.search(r"Creation time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", result.stdout)
        if not match:
            return

        created_str = match.group(1)
        created_time = datetime.strptime(created_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        now = datetime.now(UTC)
        age_days = (now - created_time).days

        if age_days > 30:
            warnings.warn(
                f"Test VM baselines are {age_days} days old. "
                f"Consider running: ./tests/integration/scripts/upgrade-vms.sh",
                UserWarning,
                stacklevel=2,
            )
    except Exception:
        # Ignore errors in baseline age check - this is just a courtesy warning
        pass


async def _check_vms_ready(pc1_executor: RemoteExecutor, pc2_executor: RemoteExecutor) -> bool:
    """Check if test VMs are reachable and fully configured.

    Verifies:
    - SSH connectivity to both VMs
    - Baseline btrfs snapshots exist (implies user is configured)
    """
    try:
        # Check both VMs in parallel
        async with TaskGroup() as tg:
            tg.create_task(pc1_executor.run_command("sudo btrfs subvolume show /.snapshots/baseline/@"))
            tg.create_task(pc2_executor.run_command("sudo btrfs subvolume show /.snapshots/baseline/@"))

        # Both commands must succeed (no exception from TaskGroup means success)
        return True
    except Exception:
        return False


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def integration_lock() -> AsyncIterator[None]:
    """Acquire the integration test lock for the session.

    Uses Hetzner Server Labels to prevent concurrent test runs.
    The lock survives VM reboots and snapshot rollbacks.
    """
    hcloud_token = os.getenv("HCLOUD_TOKEN")
    if not hcloud_token:
        pytest.fail("HCLOUD_TOKEN not set, cannot acquire lock")

    holder = _get_lock_holder()
    lock_script = SCRIPTS_DIR / "lock.sh"

    if not lock_script.exists():
        pytest.fail(f"Lock script not found: {lock_script}")

    # Acquire lock
    returncode, _, stderr = await _run_script("lock.sh", holder, "acquire", check=False)
    if returncode != 0:
        pytest.fail(f"Failed to acquire integration test lock: {stderr}")

    yield

    # Release lock
    await _run_script("lock.sh", holder, "release", check=False)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def integration_session(integration_lock: None) -> AsyncIterator[None]:
    """Session-scoped fixture for VM provisioning and reset.

    This fixture:
    1. Acquires the integration test lock (via integration_lock dependency)
    2. Checks if VMs are ready, provisions them if not
    3. Resets VMs to baseline before tests run
    4. Checks baseline age and warns if outdated
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

        # Check if VMs are ready
        if not await _check_vms_ready(pc1_executor, pc2_executor):
            # Auto-provision VMs
            provision_script = SCRIPTS_DIR / "provision-test-infra.sh"
            if not provision_script.exists():
                pytest.fail(f"Provision script not found: {provision_script}")

            returncode, _, stderr = await _run_script("provision-test-infra.sh", check=False)
            if returncode != 0:
                pytest.fail(f"Failed to provision test VMs: {stderr}")

        # Reset VMs to baseline (in parallel for faster setup)
        reset_script = SCRIPTS_DIR / "reset-vm.sh"
        if reset_script.exists():
            await _reset_vms(pc1_host, pc2_host)

        # Check baseline age and warn if outdated
        await _check_baseline_age(pc1_executor)

    yield


@pytest_asyncio.fixture(scope="module")
async def pc1_connection(integration_session: None) -> AsyncIterator[asyncssh.SSHClientConnection]:
    """SSH connection to pc1 test VM.

    Module-scoped: shared across all tests in a module for efficiency.
    Each test module gets its own connection instance.

    Uses default ~/.ssh/known_hosts - key is established by reset-vm.sh via ssh_accept_new.
    """
    host = os.environ["PC_SWITCHER_TEST_PC1_HOST"]
    user = os.environ["PC_SWITCHER_TEST_USER"]

    async with asyncssh.connect(host, username=user) as conn:
        yield conn


@pytest_asyncio.fixture(scope="module")
async def pc2_connection(integration_session: None) -> AsyncIterator[asyncssh.SSHClientConnection]:
    """SSH connection to pc2 test VM.

    Module-scoped: shared across all tests in a module for efficiency.
    Each test module gets its own connection instance.

    Uses default ~/.ssh/known_hosts - key is established by reset-vm.sh via ssh_accept_new.
    """
    host = os.environ["PC_SWITCHER_TEST_PC2_HOST"]
    user = os.environ["PC_SWITCHER_TEST_USER"]

    async with asyncssh.connect(host, username=user) as conn:
        yield conn


@pytest_asyncio.fixture(scope="module")
async def pc1_executor(pc1_connection: asyncssh.SSHClientConnection) -> RemoteLoginBashExecutor:
    """Executor for running commands on pc1 in a bash login shell environment.

    Module-scoped: shared across all tests in a module.
    Tests must clean up their own artifacts and not modify executor state.

    Commands run via this executor have ~/.profile sourced, ensuring:
    - PATH includes ~/.local/bin (for uv-installed tools like pc-switcher)
    - User environment matches interactive SSH sessions
    """
    return RemoteLoginBashExecutor(pc1_connection)


@pytest_asyncio.fixture(scope="module")
async def pc2_executor(pc2_connection: asyncssh.SSHClientConnection) -> RemoteLoginBashExecutor:
    """Executor for running commands on pc2 in a bash login shell environment.

    Module-scoped: shared across all tests in a module.
    Tests must clean up their own artifacts and not modify executor state.

    Commands run via this executor have ~/.profile sourced, ensuring:
    - PATH includes ~/.local/bin (for uv-installed tools like pc-switcher)
    - User environment matches interactive SSH sessions
    """
    return RemoteLoginBashExecutor(pc2_connection)
