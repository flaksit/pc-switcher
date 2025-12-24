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
- pc1_with_pcswitcher_mod: pc1 executor with pc-switcher installed from current branch
- pc2_without_pcswitcher_fn: pc2 executor with pc-switcher uninstalled (clean target)
- pc2_with_old_pcswitcher_fn: pc2 executor with old pc-switcher version (upgrade testing)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from typing import overload

import asyncssh
import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.install import get_install_with_script_command_line
from pcswitcher.models import CommandResult
from pcswitcher.version import Release, Version, find_one_version, get_releases

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
def _check_integration_env_vars() -> None:  # pyright: ignore[reportUnusedFunction]
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


@pytest.fixture(scope="session")
def current_git_branch() -> str:
    """Get the current git branch name, falling back to 'main' if not in a git repo."""

    try:
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        branch = branch_result.stdout.strip()
        if branch == "HEAD":
            pytest.fail("Detached HEAD state detected; please run tests from a branch.")
            # commit_result = subprocess.run(
            #     ["git", "rev-parse", "HEAD"],
            #     capture_output=True,
            #     text=True,
            #     check=True,
            # )
            # return commit_result.stdout.strip()
        return branch
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Not a git repository or git not installed; default to 'main'
        return "main"


async def get_installed_version(executor: BashLoginRemoteExecutor) -> Version:
    """Get the currently installed pc-switcher version."""
    result = await executor.run_command("pc-switcher --version", timeout=10.0)
    assert result.success, f"Failed to get version: {result.stderr}"
    # Parse version from CLI output (handles both PEP440 and SemVer formats)
    return find_one_version(result.stdout)


@pytest.fixture(scope="session")
def github_releases_desc() -> list[Release]:
    """All non-draft GitHub releases, sorted highest-to-lowest."""
    return sorted(get_releases(include_prereleases=True), key=lambda r: r.version, reverse=True)


@pytest.fixture(scope="session")
def highest_release(github_releases_desc: list[Release]) -> Release:
    """The highest GitHub release version."""
    try:
        return github_releases_desc[0]
    except IndexError:
        pytest.skip("No GitHub releases found")


@pytest.fixture(scope="session")
def next_highest_release(github_releases_desc: list[Release]) -> Release:
    """The next-highest GitHub release version."""
    try:
        return github_releases_desc[1]
    except IndexError:
        pytest.skip("Need at least two GitHub releases")


async def set_github_token_env_var(executor: BashLoginRemoteExecutor) -> None:
    """Set GITHUB_TOKEN environment variable on remote executor if available locally.

    This helps avoid GitHub API rate limiting during version checks.
    """
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        # Add GITHUB_TOKEN to ~/.profile so it's available in login shells
        await executor.run_command(
            f'grep -q "export GITHUB_TOKEN=" ~/.profile 2>/dev/null || '
            f"echo 'export GITHUB_TOKEN=\"{github_token}\"' >> ~/.profile",
            timeout=10.0,
            login_shell=False,
        )


async def install_pcswitcher_with_script(
    executor: BashLoginRemoteExecutor,
    v: Release | Version | str | None = None,
) -> CommandResult:
    """Install a specific version of pc-switcher using the install script.

    Args:
        v: Release, Version, or branch name to install.
    """
    cmd = get_install_with_script_command_line(v)

    result = await executor.run_command(
        cmd,
        timeout=120.0,
        login_shell=False,
    )
    assert result.success, f"Failed to install version {v or '(main)'}: {result.stderr}"
    return result


@overload
async def install_pcswitcher_with_uv(executor: BashLoginRemoteExecutor) -> CommandResult: ...


@overload
async def install_pcswitcher_with_uv(executor: BashLoginRemoteExecutor, *, release: Release) -> CommandResult: ...


@overload
async def install_pcswitcher_with_uv(executor: BashLoginRemoteExecutor, *, version: Version) -> CommandResult: ...


@overload
async def install_pcswitcher_with_uv(executor: BashLoginRemoteExecutor, *, ref: str) -> CommandResult: ...


async def install_pcswitcher_with_uv(
    executor: BashLoginRemoteExecutor,
    *,
    release: Release | None = None,
    version: Version | None = None,
    ref: str | None = None,
) -> CommandResult:
    """Install a specific version of pc-switcher using uv tool."""
    if release:
        version_arg = f"@{release.tag}"
    elif version:
        version_arg = f"@v{version.semver_str()}"
    elif ref:
        version_arg = f"@{ref}"
    else:
        version_arg = ""

    result = await executor.run_command(
        f"uv tool install --quiet --quiet git+https://github.com/flaksit/pc-switcher{version_arg}",
        timeout=120.0,
    )
    assert result.success, f"Failed to install version {release} via uv: {result.stderr}"
    return result


async def uninstall_pcswitcher(executor: BashLoginRemoteExecutor) -> None:
    """Uninstall pc-switcher."""
    result = await executor.run_command("command -v uv && uv tool list | grep '^pcswitcher '", timeout=10.0)
    if result.success:
        result = await executor.run_command("uv tool uninstall pcswitcher", timeout=10.0)
        assert result.success, f"Failed to uninstall pc-switcher:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    # Verify pc-switcher is actually gone
    result = await executor.run_command(
        "command -v pc-switcher",
        timeout=1.0,
    )
    assert not result.success, (
        f"pc-switcher should be uninstalled but is still found.\n"
        f"Try running: uv tool list; ls -la ~/.local/bin/pc-switcher\n"
        f"stdout: {result.stdout}"
    )


async def remove_config_and_data(executor: BashLoginRemoteExecutor) -> None:
    """Remove pc-switcher configuration and data directories."""
    await executor.run_command(
        "rm -rf ~/.config/pc-switcher ~/.local/share/pc-switcher",
        timeout=10.0,
    )


async def uninstall_pcswitcher_and_config(executor: BashLoginRemoteExecutor) -> None:
    """Uninstall pc-switcher and remove its configuration."""
    await asyncio.gather(
        uninstall_pcswitcher(executor),
        remove_config_and_data(executor),
    )


@pytest.fixture(scope="module")
async def pc1_with_pcswitcher_mod(
    pc1_executor: BashLoginRemoteExecutor, current_git_branch: str
) -> BashLoginRemoteExecutor:
    """Ensure pc-switcher is installed on pc1 from current branch.

    Module-scoped: installs pc-switcher once per test module if not already present.
    Does NOT uninstall after tests - leaves pc-switcher installed for efficiency.

    Use this fixture when tests require pc-switcher to be available on pc1.

    NOTE: This fixture installs from the current git branch to test in-development code.
    The branch must be pushed to origin for this to work.

    Also sets up GITHUB_TOKEN on pc1 if available on the test runner to avoid
    GitHub API rate limiting during version checks.
    """
    branch = current_git_branch

    await set_github_token_env_var(pc1_executor)

    # Always reinstall to ensure we have the latest code from current branch
    await install_pcswitcher_with_script(pc1_executor, branch)

    # Verify installation
    verify = await pc1_executor.run_command("pc-switcher --version", timeout=10.0)
    assert verify.success, f"pc-switcher not accessible after install: {verify.stderr}"

    return pc1_executor


@pytest.fixture
async def pc2_without_pcswitcher_fn(
    pc2_executor: BashLoginRemoteExecutor,
) -> BashLoginRemoteExecutor:
    """Provide a clean environment on pc2 without pc-switcher installed.

    WARNING: This fixture wraps pc2_executor and modifies VM state by uninstalling
    pc-switcher. Tests using this fixture MUST NOT use pc2_executor directly in
    parallel, as both operate on the same VM and will interfere with each other.

    Removes pc-switcher installation but keeps test infrastructure intact.
    Useful for testing fresh installs on a clean target.

    Cleanup: Captures initial state and restores it after the test to avoid affecting
    other tests in the same test session.
    """
    await uninstall_pcswitcher_and_config(pc2_executor)
    return pc2_executor


@pytest.fixture
async def pc2_with_old_pcswitcher_fn(
    pc2_without_pcswitcher_fn: BashLoginRemoteExecutor,
    next_highest_release: Release,
) -> BashLoginRemoteExecutor:
    """Provide pc2 with an older version of pc-switcher.

    WARNING: This fixture wraps pc2_executor and modifies VM state by installing
    an older version of pc-switcher. Tests using this fixture MUST NOT use pc2_executor
    directly in parallel, as both operate on the same VM and will interfere with each
    other.

    Uninstalls current pc-switcher and installs an older release.
    Useful for testing upgrade scenarios.

    Cleanup: Captures initial state and restores it after the test to avoid affecting
    other tests in the same test session.
    """
    await install_pcswitcher_with_script(pc2_without_pcswitcher_fn, next_highest_release)

    return pc2_without_pcswitcher_fn
