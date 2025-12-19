"""Integration test fixtures for pc-switcher.

These tests require VM infrastructure. They are skipped if environment
variables are not configured.

VM provisioning (lock acquisition, readiness check, and reset) is handled
by the test launcher script (run-integration-tests.sh) before pytest runs.

Fixtures provided:
- pc1_connection: SSH connection to pc1 test VM
- pc2_connection: SSH connection to pc2 test VM
- pc1_executor: BashLoginRemoteExecutor for pc1 (= RemoteExecutor with login shell environment)
- pc2_executor: BashLoginRemoteExecutor for pc2 (= RemoteExecutor with login shell environment)
- pc1_with_pcswitcher: pc1 executor with pc-switcher installed from current branch
- pc2_without_pcswitcher: pc2 executor with pc-switcher uninstalled (clean target)
- pc2_with_old_pcswitcher: pc2 executor with old pc-switcher version (upgrade testing)
"""

from __future__ import annotations

import os
import subprocess
import warnings
from collections.abc import AsyncIterator

import asyncssh
import pytest

from pcswitcher.executor import BashLoginRemoteExecutor

REQUIRED_ENV_VARS = [
    "HCLOUD_TOKEN",
    "PC_SWITCHER_TEST_PC1_HOST",
    "PC_SWITCHER_TEST_PC2_HOST",
    "PC_SWITCHER_TEST_USER",
]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-apply integration marker and check VM environment."""
    integration_marker = pytest.mark.integration

    # Auto-apply integration marker to all tests in tests/integration/
    for item in items:
        if "/integration/" in str(item.fspath):
            item.add_marker(integration_marker)


@pytest.fixture(scope="session", autouse=True)
def check_integration_env_vars() -> None:
    """Session-scoped fixture to check integration test environment variables.

    Exit the test session if any required environment variable is missing.
    """
    missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing_vars:
        pytest.exit(
            f"Integration tests require VM environment. "
            f"Missing: {', '.join(missing_vars)}. "
            f"Run unit tests only with: uv run pytest tests/unit tests/contract",
            1,
        )


@pytest.fixture(scope="module")
async def _pc1_connection() -> AsyncIterator[asyncssh.SSHClientConnection]:  # pyright: ignore[reportUnusedFunction]
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
async def _pc2_connection() -> AsyncIterator[asyncssh.SSHClientConnection]:  # pyright: ignore[reportUnusedFunction]
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


@pytest.fixture(scope="session")
def current_git_branch() -> str:
    """Get the current git branch name, defaulting to 'main' if not in a git repo."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "main"


@pytest.fixture(scope="module")
async def pc1_with_pcswitcher(
    pc1_executor: BashLoginRemoteExecutor, current_git_branch: str
) -> BashLoginRemoteExecutor:
    """Ensure pc-switcher is installed on pc1 from current branch.

    Module-scoped: installs pc-switcher once per test module if not already present.
    Does NOT uninstall after tests - leaves pc-switcher installed for efficiency.

    Use this fixture when tests require pc-switcher to be available on pc1.

    NOTE: This fixture installs from the current git branch to test in-development code.
    The branch must be pushed to origin for this to work.
    """
    branch = current_git_branch

    # Always reinstall to ensure we have the latest code from current branch
    # This is important for testing in-development features
    result = await pc1_executor.run_command(
        f"curl -sSL {_INSTALL_SCRIPT_URL} | bash -s -- --ref {branch}",
        timeout=120.0,
    )
    assert result.success, f"Failed to install pc-switcher on pc1 from branch {branch}: {result.stderr}"

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
async def pc2_without_pcswitcher(
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
    version_check = await pc2_executor.run_command("pc-switcher --version 2>/dev/null || true", timeout=10.0)
    was_installed = version_check.success

    # Uninstall pc-switcher if it exists
    await pc2_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0)
    await pc2_executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)

    yield pc2_executor

    # TODO remove restore to initial state (see #68)
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
async def pc2_with_old_pcswitcher(
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
    version_check = await pc2_executor.run_command("pc-switcher --version 2>/dev/null || true", timeout=10.0)
    was_installed = version_check.success and "pc-switcher" in version_check.stdout.lower()

    # Uninstall and install older version
    await pc2_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0)
    await pc2_executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)
    result = await pc2_executor.run_command(
        f"curl -sSL {install_script_url} | VERSION=v0.1.0-alpha.1 bash",
        timeout=120.0,
    )
    assert result.success, f"Failed to install old version: {result.stderr}"

    yield pc2_executor

    # TODO remove restore to initial state (see #68)
    # Restore to initial state
    await pc2_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0)
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
