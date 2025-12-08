"""Integration test fixtures for pc-switcher.

These tests require VM infrastructure. They are skipped if environment
variables are not configured.

Fixtures provided:
- pc1_connection: SSH connection to pc1 test VM
- pc2_connection: SSH connection to pc2 test VM
- pc1_executor: RemoteExecutor for pc1
- pc2_executor: RemoteExecutor for pc2
- integration_lock: Session-scoped lock for test isolation
- integration_session: Session-scoped fixture for VM provisioning and reset
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import asyncssh
import pytest
import pytest_asyncio

from pcswitcher.executor import RemoteExecutor

REQUIRED_ENV_VARS = [
    "PC_SWITCHER_TEST_PC1_HOST",
    "PC_SWITCHER_TEST_PC2_HOST",
]

SCRIPTS_DIR = Path(__file__).parent.parent / "infrastructure" / "scripts"


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
    return f"local-{os.getenv('USER', 'unknown')}"


def _run_script(script_name: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run an infrastructure script synchronously."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        pytest.fail(f"Infrastructure script not found: {script_path}")
    return subprocess.run(
        [str(script_path), *args],
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, "HCLOUD_TOKEN": os.getenv("HCLOUD_TOKEN", "")},
    )


def _reset_vms_parallel(pc1_host: str, pc2_host: str) -> None:
    """Reset both VMs in parallel for faster test setup.

    Uses ThreadPoolExecutor to run reset scripts concurrently without
    interfering with pytest-asyncio's event loop management.
    """
    with ThreadPoolExecutor(max_workers=2) as executor:
        future1 = executor.submit(_run_script, "reset-vm.sh", pc1_host, check=False)
        future2 = executor.submit(_run_script, "reset-vm.sh", pc2_host, check=False)
        # Wait for both to complete
        future1.result()
        future2.result()


def _check_vms_exist() -> bool:
    """Check if test VMs exist in Hetzner Cloud."""
    hcloud_token = os.getenv("HCLOUD_TOKEN")
    if not hcloud_token:
        return False

    try:
        result = subprocess.run(
            ["hcloud", "server", "describe", "pc1"],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "HCLOUD_TOKEN": hcloud_token},
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


@pytest.fixture(scope="session")
def integration_lock() -> Iterator[None]:
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
    result = _run_script("lock.sh", holder, "acquire", check=False)
    if result.returncode != 0:
        pytest.fail(f"Failed to acquire integration test lock: {result.stderr}")

    yield

    # Release lock
    _run_script("lock.sh", holder, "release", check=False)


@pytest.fixture(scope="session")
def integration_session(integration_lock: None) -> Iterator[None]:
    """Session-scoped fixture for VM provisioning and reset.

    This fixture:
    1. Acquires the integration test lock (via integration_lock dependency)
    2. Checks if VMs exist, provisions them if not
    3. Resets VMs to baseline before tests run
    """
    hcloud_token = os.getenv("HCLOUD_TOKEN")

    # Check if VMs exist
    if not _check_vms_exist():
        if not hcloud_token:
            pytest.fail(
                "Test VMs not found and HCLOUD_TOKEN not set. "
                "Cannot auto-provision. See docs/testing-ops-guide.md for setup."
            )

        # Auto-provision VMs
        provision_script = SCRIPTS_DIR / "provision-test-infra.sh"
        if not provision_script.exists():
            pytest.fail(f"Provision script not found: {provision_script}")

        result = _run_script("provision-test-infra.sh", check=False)
        if result.returncode != 0:
            pytest.fail(f"Failed to provision test VMs: {result.stderr}")

    # Reset VMs to baseline (in parallel for faster setup)
    pc1_host = os.getenv("PC_SWITCHER_TEST_PC1_HOST", "")
    pc2_host = os.getenv("PC_SWITCHER_TEST_PC2_HOST", "")

    reset_script = SCRIPTS_DIR / "reset-vm.sh"
    if reset_script.exists() and pc1_host and pc2_host:
        _reset_vms_parallel(pc1_host, pc2_host)

    yield


@pytest_asyncio.fixture(scope="module")
async def pc1_connection(integration_session: None) -> AsyncIterator[asyncssh.SSHClientConnection]:
    """SSH connection to pc1 test VM.

    Module-scoped: shared across all tests in a module for efficiency.
    Each test module gets its own connection instance.

    Uses default ~/.ssh/known_hosts - key is established by reset-vm.sh via ssh_accept_new.
    """
    host = os.environ["PC_SWITCHER_TEST_PC1_HOST"]
    user = os.environ.get("PC_SWITCHER_TEST_USER", "testuser")

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
    user = os.environ.get("PC_SWITCHER_TEST_USER", "testuser")

    async with asyncssh.connect(host, username=user) as conn:
        yield conn


@pytest_asyncio.fixture(scope="module")
async def pc1_executor(pc1_connection: asyncssh.SSHClientConnection) -> RemoteExecutor:
    """RemoteExecutor for running commands on pc1.

    Module-scoped: shared across all tests in a module.
    Tests must clean up their own artifacts and not modify executor state.
    """
    return RemoteExecutor(pc1_connection)


@pytest_asyncio.fixture(scope="module")
async def pc2_executor(pc2_connection: asyncssh.SSHClientConnection) -> RemoteExecutor:
    """RemoteExecutor for running commands on pc2.

    Module-scoped: shared across all tests in a module.
    Tests must clean up their own artifacts and not modify executor state.
    """
    return RemoteExecutor(pc2_connection)
