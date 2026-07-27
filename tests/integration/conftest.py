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
- pc2_with_pcswitcher: pc2 executor with pc-switcher installed from current branch (for back-sync tests)
- pc2_without_pcswitcher_fn: pc2 executor with pc-switcher uninstalled (clean target)
- pc2_with_old_pcswitcher_fn: pc2 executor with old pc-switcher version (upgrade testing)
- reset_pcswitcher_state: resets pc-switcher state on both VMs (config + data, for test isolation)
- vm_test_fixtures: both VMs carry the package-manager subjects the package-sync tests operate on
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import overload

import asyncssh
import pytest

from pcswitcher.btrfs_snapshots import delete_all_snapshots
from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.install import get_install_with_script_command_line
from pcswitcher.models import CommandResult
from pcswitcher.version import Release, Version, find_one_version, get_releases, get_this_version

REQUIRED_ENV_VARS = [
    "HCLOUD_TOKEN",
    "PC_SWITCHER_TEST_PC1_HOST",
    "PC_SWITCHER_TEST_PC2_HOST",
    "PC_SWITCHER_TEST_USER",
]


# Every integration test must declare where it belongs in CI's topic-based selection
# (tests/integration/scripts/select-ci-tests.sh): an area marker, `smoke`, or
# `area_core` for core behavior with no topic mapping (full-suite runs only).
# Enforced at collection so a new test file cannot silently fall outside topic runs.
_CI_SELECTION_MARKERS = {"smoke", "area_package", "area_install", "area_btrfs", "area_folder", "area_core"}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-apply integration marker and require a CI-selection marker on each test."""
    integration_marker = pytest.mark.integration

    unmapped: list[str] = []
    for item in items:
        if "/integration/" not in str(item.fspath):
            continue
        # Auto-apply integration marker to all tests in tests/integration/
        item.add_marker(integration_marker)

        markers = {marker.name for marker in item.iter_markers()}
        if "benchmark" not in markers and not (markers & _CI_SELECTION_MARKERS):
            unmapped.append(item.nodeid)

    if unmapped:
        raise pytest.UsageError(
            "Integration tests without a CI-selection marker "
            f"({', '.join(sorted(_CI_SELECTION_MARKERS))}); "
            'see "CI test selection" in docs/dev/testing-guide.md:\n  ' + "\n  ".join(unmapped)
        )


# ---------------------------------------------------------------------------------
# Live progress and failure reporting.
#
# This suite runs inside a CI step with a wall-clock timeout, and pytest writes its
# FAILURES section and its durations table only when the whole session ENDS. A step killed
# mid-suite therefore loses the diagnosis of every failure that had already happened --
# which is how the #208 D9 verdict was lost: the test had failed, but the uploaded log held
# only the live progress tail. The hooks below emit each failure, and each test's start and
# elapsed time, at the moment they exist, so whatever the log holds when the process dies
# is already the full story up to that point.
#
# stderr, not stdout: the workflow merges the two (`2>&1 | tee`), and stderr is unbuffered
# in Python regardless of whether the destination is a pipe, so nothing sits in a buffer
# waiting for a flush that a SIGKILL will never allow. (run-integration-tests.sh also sets
# PYTHONUNBUFFERED=1, which covers pytest's own stdout writes.)
# ---------------------------------------------------------------------------------

_TEST_START_MONOTONIC: dict[str, float] = {}


def _clock() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S")


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    """Name the test that is STARTING, timestamped, so a killed run still identifies
    exactly which test was in flight when the clock ran out.
    """
    _ = location
    _TEST_START_MONOTONIC[nodeid] = time.monotonic()
    # Leading newline: pytest's own `-v` progress line is written without one and would
    # otherwise be glued to this.
    print(f"\n[it {_clock()}] START {nodeid}", file=sys.stderr, flush=True)


def pytest_runtest_logfinish(nodeid: str, location: tuple[str, int | None, str]) -> None:
    """Per-test elapsed time as it completes -- the same information `--durations` gives,
    except this survives a session that never reaches its summary.
    """
    _ = location
    started = _TEST_START_MONOTONIC.pop(nodeid, None)
    elapsed = f"{time.monotonic() - started:.1f}s" if started is not None else "?"
    print(f"\n[it {_clock()}] END   {nodeid} ({elapsed})", file=sys.stderr, flush=True)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Dump a failure's full detail immediately instead of waiting for the FAILURES
    section. Covers setup, call and teardown phases; duplicating the end-of-run summary
    when the session does finish is a deliberate, cheap trade for never losing it.
    """
    if not report.failed:
        return
    banner = "=" * 30
    print(f"\n{banner} FAILURE ({report.when}) {banner}", file=sys.stderr)
    print(f"{report.nodeid}", file=sys.stderr)
    print(report.longreprtext or "<no traceback available>", file=sys.stderr)
    print("=" * 79, file=sys.stderr, flush=True)


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
    Uses keepalive to prevent connection going stale during long-running operations.
    """
    host = os.environ["PC_SWITCHER_TEST_PC1_HOST"]
    user = os.environ["PC_SWITCHER_TEST_USER"]

    async with asyncssh.connect(
        host,
        username=user,
        keepalive_interval=15,
        keepalive_count_max=3,
    ) as conn:
        yield conn


@pytest.fixture(scope="module")
async def _pc2_connection() -> AsyncIterator[asyncssh.SSHClientConnection]:  # pyright: ignore[reportUnusedFunction]
    """SSH connection to pc2 test VM.

    Module-scoped: shared across all tests in a module for efficiency.
    Each test module gets its own connection instance.

    Uses default ~/.ssh/known_hosts - key is established by reset-vm.sh via ssh_accept_new.
    Uses keepalive to prevent connection going stale during long-running operations.
    """
    host = os.environ["PC_SWITCHER_TEST_PC2_HOST"]
    user = os.environ["PC_SWITCHER_TEST_USER"]

    async with asyncssh.connect(
        host,
        username=user,
        keepalive_interval=15,
        keepalive_count_max=3,
    ) as conn:
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

    Also sets up GITHUB_TOKEN on pc1 if available to avoid GitHub API rate limiting.
    """
    executor = BashLoginRemoteExecutor(_pc1_connection)
    await set_github_token_env_var(executor)
    return executor


@pytest.fixture(scope="module")
async def pc2_executor(_pc2_connection: asyncssh.SSHClientConnection) -> BashLoginRemoteExecutor:
    """Executor for running commands on pc2 with login shell enabled by default.

    Module-scoped: shared across all tests in a module.
    Tests must clean up their own artifacts and not modify executor state.

    Returns BashLoginRemoteExecutor which wraps all commands in bash login shell,
    ensuring PATH includes ~/.local/bin for user-installed tools (uv, pc-switcher).
    Commands use login_shell=True by default but can be overridden with login_shell=False
    for system commands.

    Also sets up GITHUB_TOKEN on pc2 if available to avoid GitHub API rate limiting.
    """
    executor = BashLoginRemoteExecutor(_pc2_connection)
    await set_github_token_env_var(executor)
    return executor


_VM_TEST_FIXTURES_SCRIPT = Path(__file__).parent / "scripts" / "internal" / "vm-test-fixtures.sh"
_VM_TEST_FIXTURES_REMOTE_PATH = "/tmp/pcswitcher-vm-test-fixtures.sh"


async def ensure_vm_test_fixtures(executor: BashLoginRemoteExecutor, *, install_app: bool) -> None:
    """Create the package-manager subjects the suite operates on (`vm-test-fixtures.sh`).

    Tests that need a snap or a flatpak to hold, diverge, remove or reinstall must own
    one; a stock Ubuntu 24.04 VM owns neither. Provisioning bakes these into the baseline
    snapshot, so on a current baseline this is a handful of presence checks. Running it
    from here as well is what makes the suite independent of when the baseline was last
    built: against an older one it creates the subjects itself rather than leaving tests
    without a subject to work on.

    `install_app` maps to the script's `--with-app` and belongs to the SOURCE machine
    only: the flatpak application is what makes source and target genuinely diverge, so
    the target must not carry it (the script actively removes it there).
    """
    await executor.send_file(_VM_TEST_FIXTURES_SCRIPT, _VM_TEST_FIXTURES_REMOTE_PATH)
    args = " --with-app" if install_app else ""
    result = await executor.run_command(
        f"bash {_VM_TEST_FIXTURES_REMOTE_PATH}{args}",
        login_shell=False,
        # Generous: on a baseline that predates the current fixture version this installs
        # snaps and a Flathub runtime (~2.8 GB deployed) from scratch. On a current
        # baseline it is a handful of local queries and returns in under a second.
        timeout=1800.0,
    )
    assert result.success, (
        f"Failed to create the VM test fixtures ({_VM_TEST_FIXTURES_SCRIPT.name}).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


@pytest.fixture(scope="module")
async def vm_test_fixtures(
    pc1_executor: BashLoginRemoteExecutor,
    pc2_executor: BashLoginRemoteExecutor,
) -> None:
    """Both VMs carry the current package-manager test fixtures before the module runs."""
    await asyncio.gather(
        ensure_vm_test_fixtures(pc1_executor, install_app=True),
        ensure_vm_test_fixtures(pc2_executor, install_app=False),
    )


@pytest.fixture(scope="session")
def current_git_branch() -> str:
    """Get the current git branch name, falling back to 'main' if not in a git repo."""

    head_ref = os.environ.get("GITHUB_HEAD_REF")
    if head_ref:
        return head_ref

    ref_name = os.environ.get("GITHUB_REF_NAME")
    if ref_name:
        return ref_name

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
    except subprocess.CalledProcessError, FileNotFoundError:
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
def this_release_floor(github_releases_desc: list[Release]) -> Release:
    """The highest GitHub release version."""
    this_version = get_this_version()
    for release in github_releases_desc:
        if release.version <= this_version:
            return release
    pytest.skip("No GitHub release found for this version")


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


@pytest.fixture(scope="session")
def branch_head_commit(current_git_branch: str) -> str:
    """Commit the remote branch points at — the code install.sh installs from.

    Resolved once per session so ensure_pcswitcher_at_branch_head can tell whether a
    VM's installed build already is the branch tip.
    """
    result = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/heads/{current_git_branch}"],
        capture_output=True,
        text=True,
        check=True,
    )
    if not result.stdout.strip():
        pytest.fail(
            f"Branch {current_git_branch!r} does not exist on origin. Push it first: "
            "install fixtures install pc-switcher from the remote branch."
        )
    return result.stdout.split()[0]


async def ensure_pcswitcher_at_branch_head(
    executor: BashLoginRemoteExecutor,
    branch: str,
    head_commit: str,
) -> None:
    """Install pc-switcher from `branch` unless the installed build already is `head_commit`.

    Dev builds stamp the short commit into the version's local part (e.g.
    0.5.1.post167.dev0+43a52fa3), so a matching stamp proves the VM already runs the
    branch tip and the ~10s clone+build install can be skipped in favour of a ~1s
    version probe. Any mismatch — nothing installed, an older push, a self-update or
    install-script test having changed the installed version — falls through to a real
    install. A branch tip that is exactly a release tag carries no commit stamp and
    therefore always reinstalls (safe, just slower).
    """
    installed = await executor.run_command("pc-switcher --version", timeout=10.0)
    if installed.success and head_commit[:8] in installed.stdout:
        return

    await install_pcswitcher_with_script(executor, branch)

    verify = await executor.run_command("pc-switcher --version", timeout=10.0)
    assert verify.success, f"pc-switcher not accessible after install: {verify.stderr}"


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


async def _remove_config_and_data(executor: BashLoginRemoteExecutor) -> None:
    """Remove pc-switcher configuration and data directories."""
    await executor.run_command(
        "rm -rf ~/.config/pc-switcher ~/.local/share/pc-switcher",
        timeout=10.0,
    )


async def uninstall_pcswitcher_and_config(executor: BashLoginRemoteExecutor) -> None:
    """Uninstall pc-switcher and remove its configuration."""
    await asyncio.gather(
        uninstall_pcswitcher(executor),
        _remove_config_and_data(executor),
    )


@pytest.fixture(scope="module")
async def pc1_with_pcswitcher_mod(
    pc1_executor: BashLoginRemoteExecutor, current_git_branch: str, branch_head_commit: str
) -> BashLoginRemoteExecutor:
    """Ensure pc-switcher on pc1 is the current branch tip.

    Module-scoped. Skips the install when the VM already runs the branch tip (see
    ensure_pcswitcher_at_branch_head). Does NOT uninstall after tests.

    NOTE: installs from the current git branch to test in-development code.
    The branch must be pushed to origin for this to work.
    """
    await ensure_pcswitcher_at_branch_head(pc1_executor, current_git_branch, branch_head_commit)
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


@pytest.fixture
async def pc2_with_pcswitcher(
    pc2_executor: BashLoginRemoteExecutor, current_git_branch: str, branch_head_commit: str
) -> BashLoginRemoteExecutor:
    """Ensure pc-switcher on pc2 is the current branch tip — same version as pc1,
    which is required for back-sync tests.

    Function-scoped, but skips the install when the VM already runs the branch tip
    (see ensure_pcswitcher_at_branch_head), so only the first user per session — and
    any test after one that changed the installed version — pays for a real install.

    WARNING: This fixture wraps pc2_executor and modifies VM state.
    Tests using this fixture MUST NOT use pc2_executor directly in parallel,
    as both operate on the same VM and will interfere with each other.

    NOTE: installs from the current git branch to test in-development code.
    The branch must be pushed to origin for this to work.
    """
    await ensure_pcswitcher_at_branch_head(pc2_executor, current_git_branch, branch_head_commit)
    return pc2_executor


@pytest.fixture
async def reset_pcswitcher_state(
    pc1_executor: BashLoginRemoteExecutor,
    pc2_executor: BashLoginRemoteExecutor,
) -> AsyncIterator[None]:
    """Reset pc-switcher state on both VMs before and after each test.

    Function-scoped fixture that ensures test isolation by:
    - Removing config and data directories (sync-history.json, logs, etc.)
    - Deleting all pc-switcher btrfs snapshots

    This fixture should be used by all tests that run `pc-switcher sync`.
    Tests/fixtures that need config should create it after this runs.

    Cleanup runs both BEFORE (setup) and AFTER (teardown) the test to ensure
    clean state and proper cleanup even if tests fail.
    """

    async def cleanup() -> None:
        await asyncio.gather(
            _remove_config_and_data(pc1_executor),
            _remove_config_and_data(pc2_executor),
            delete_all_snapshots(pc1_executor),
            delete_all_snapshots(pc2_executor),
        )

    # Setup: clean before test
    await cleanup()

    yield

    # Teardown: clean after test
    await cleanup()
