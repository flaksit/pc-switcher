"""Integration tests for pc-switcher self update command.

These tests install pc-switcher on a pristine VM and verify the update workflow against real GitHub releases.
"""

from __future__ import annotations

from typing import overload

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.version import Release, Version, find_one_version, get_releases

_MIN_VERSION_WITH_SELF_UPDATE = Version.parse("0.1.0-alpha.4")

# Install script URL
INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh"


@pytest.fixture(scope="session")
def github_releases_desc() -> list[Release]:
    """All non-draft GitHub releases, sorted highest-to-lowest."""
    releases = sorted(get_releases(include_prereleases=True), key=lambda r: r.version, reverse=True)
    if not releases:
        pytest.skip("No GitHub releases found for flaksit/pc-switcher")
    return releases


@pytest.fixture(scope="session")
def new_release(github_releases_desc: list[Release]) -> Release:
    """The highest GitHub release version.

    Skips any tests depending on this fixture if the the release predates self-update
    and GITHUB_TOKEN propagation requirements.
    """
    new_release = github_releases_desc[0]
    if new_release.version < _MIN_VERSION_WITH_SELF_UPDATE:
        pytest.skip(
            f"Highest release ({new_release.version}) is < {_MIN_VERSION_WITH_SELF_UPDATE}; skipping self-update tests"
        )
    return new_release


@pytest.fixture(scope="session")
def old_release(github_releases_desc: list[Release]) -> Release:
    """The next-highest GitHub release version.

    Skips any tests depending on this fixture if the the release predates self-update
    and GITHUB_TOKEN propagation requirements.
    """
    if len(github_releases_desc) < 2:
        pytest.skip("Need at least two GitHub releases to test self-update")

    old_release = github_releases_desc[1]
    if old_release.version < _MIN_VERSION_WITH_SELF_UPDATE:
        pytest.skip(
            f"Next-highest release ({old_release.version}) is < {_MIN_VERSION_WITH_SELF_UPDATE}; "
            "skipping self-update tests"
        )
    return old_release


@pytest.fixture(scope="module")
async def executor_with_prerequisites(
    pc1_executor: BashLoginRemoteExecutor,
) -> BashLoginRemoteExecutor:
    """Install the prerequisites for pc-switcher."""

    await _install_with_script(pc1_executor)
    # await _uninstall_via_uv(pc1_executor)

    return pc1_executor


@pytest.fixture
async def executor_with_old(
    executor_with_prerequisites: BashLoginRemoteExecutor,
    old_release: Release,
) -> BashLoginRemoteExecutor:
    """Install the current pc-switcher version before each test."""
    await _install_with_uv(executor_with_prerequisites, release=old_release)

    return executor_with_prerequisites


@pytest.fixture
async def executor_with_new(
    executor_with_prerequisites: BashLoginRemoteExecutor,
    new_release: Release,
) -> BashLoginRemoteExecutor:
    """Install the current pc-switcher version before each test."""
    await _install_with_uv(executor_with_prerequisites, release=new_release)

    return executor_with_prerequisites


@pytest.fixture
async def executor_with_current(
    executor_with_prerequisites: BashLoginRemoteExecutor,
    current_git_branch: str,
) -> BashLoginRemoteExecutor:
    """Install the current pc-switcher version before each test."""
    await _install_with_uv(executor_with_prerequisites, ref=current_git_branch)

    return executor_with_prerequisites


# async def _uninstall_with_uv(executor: BashLoginRemoteExecutor) -> None:
#     """Uninstall pc-switcher using uv tool."""
#     result = await executor.run_command(
#         "uv tool uninstall pc-switcher",
#         timeout=60.0,
#     )
#     assert result.success, f"Failed to uninstall pc-switcher via uv: {result.stderr}"


async def _install_with_script(executor: BashLoginRemoteExecutor, release: Release | None = None) -> None:
    """Install a specific version of pc-switcher using the install script."""
    set_version = f"VERSION='{release.tag}'" if release else ""
    result = await executor.run_command(
        f"curl -sSL {INSTALL_SCRIPT_URL} | {set_version} bash",
        timeout=120.0,
    )
    assert result.success, f"Failed to install version {release}: {result.stderr}"


@overload
async def _install_with_uv(executor: BashLoginRemoteExecutor) -> None: ...


@overload
async def _install_with_uv(executor: BashLoginRemoteExecutor, *, release: Release) -> None: ...


@overload
async def _install_with_uv(executor: BashLoginRemoteExecutor, *, version: Version) -> None: ...


@overload
async def _install_with_uv(executor: BashLoginRemoteExecutor, *, ref: str) -> None: ...


async def _install_with_uv(
    executor: BashLoginRemoteExecutor,
    *,
    release: Release | None = None,
    version: Version | None = None,
    ref: str | None = None,
) -> None:
    """Install a specific version of pc-switcher using uv tool."""
    if release:
        version_arg = f"@v{release.version.semver_str()}"
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


async def _get_installed_version(executor: BashLoginRemoteExecutor) -> Version:
    """Get the currently installed pc-switcher version."""
    result = await executor.run_command("pc-switcher --version", timeout=10.0)
    assert result.success, f"Failed to get version: {result.stderr}"
    # Parse version from CLI output (handles both PEP440 and SemVer formats)
    return find_one_version(result.stdout)


async def _run_self_update(
    executor: BashLoginRemoteExecutor,
    version: Version | str | None = None,
    prerelease: bool = False,
) -> tuple[bool, str, str]:
    """Run pc-switcher self update and return (success, stdout, stderr)."""
    cmd = "pc-switcher self update"
    if prerelease:
        cmd += " --prerelease"
    if version:
        # Use SemVer format for version argument
        version_str = version.semver_str() if isinstance(version, Version) else version
        cmd += f" {version_str}"

    result = await executor.run_command(cmd, timeout=120.0)
    return result.success, result.stdout, result.stderr


class TestSelfUpdateCommandExists:
    """Tests verifying which versions have the self-update command."""

    async def test_old_version_has_self_update(
        self,
        executor_with_old: BashLoginRemoteExecutor,
    ) -> None:
        """Test that the next-highest release has the self update command."""
        # Self update command should exist
        result = await executor_with_old.run_command("pc-switcher self update --help", timeout=10.0)
        assert result.success, f"Self update help failed: {result.stderr}"
        assert "update" in result.stdout.lower()

    async def test_has_self_update(
        self,
        pc1_with_executor: BashLoginRemoteExecutor,
    ) -> None:
        # Self update command should exist
        result = await pc1_with_executor.run_command("pc-switcher self update --help", timeout=10.0)
        assert result.success, f"Self update help failed: {result.stderr}"
        assert "update" in result.stdout.lower()

    async def test_self_command_group_help(
        self,
        pc1_with_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Test that 'pc-switcher self --help' shows the command group."""
        result = await pc1_with_executor.run_command("pc-switcher self --help", timeout=10.0)
        assert result.success, f"Self help failed: {result.stderr}"
        # Should show "self" command group and list subcommands
        assert "update" in result.stdout.lower()
        assert "manage" in result.stdout.lower() and "self" in result.stdout.lower()

    async def test_self_update_help_shows_prerelease_flag(
        self,
        pc1_with_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Test that 'pc-switcher self update --help' documents --prerelease flag."""
        result = await pc1_with_executor.run_command("pc-switcher self update --help", timeout=10.0)
        assert result.success, f"Self update help failed: {result.stderr}"
        # Should document the --prerelease option
        assert "--prerelease" in result.stdout


async def test_upgrade_to_specific_version(
    executor_with_old: BashLoginRemoteExecutor,
    new_release: Release,
) -> None:
    """Test upgrading from the next-highest release to the highest release."""
    # Use self update to upgrade
    success, stdout, stderr = await _run_self_update(executor_with_old, new_release.version)
    assert success, f"Self update failed: {stderr}"
    assert "Successfully updated" in stdout or new_release.version.semver_str() in stdout

    # Verify new version
    new_version = await _get_installed_version(executor_with_old)
    assert new_version == new_release.version


async def test_upgrade_with_prerelease_flag(
    executor_with_old: BashLoginRemoteExecutor,
    new_release: Release,
) -> None:
    """Test using --prerelease flag to find latest prerelease."""
    # Use self update with --prerelease (should find latest prerelease)
    success, stdout, stderr = await _run_self_update(executor_with_old, prerelease=True)
    assert success, f"Self update --prerelease failed:\n--- STDOUT ---\n{stdout}\n\n--- STDERR ---\n{stderr}"

    # Should have upgraded to at least the highest release
    new_version = await _get_installed_version(executor_with_old)
    assert new_version >= new_release.version


async def test_downgrade_to_specific_version(
    executor_with_new: BashLoginRemoteExecutor,
    old_release: Release,
) -> None:
    """Test downgrading from the highest release to the next-highest release."""
    # Use self update to downgrade
    success, stdout, stderr = await _run_self_update(executor_with_new, old_release.version)
    assert success, f"Self update (downgrade) failed: {stderr}"
    assert "downgrading" in stdout.lower() and "warning" in stdout.lower()

    # Verify downgraded version
    new_version = await _get_installed_version(executor_with_new)
    assert new_version == old_release.version


class TestSelfUpdateSameVersion:
    """Tests for self update when already at target version."""

    async def test_already_at_version(
        self,
        executor_with_new: BashLoginRemoteExecutor,
        new_release: Release,
    ) -> None:
        """Test self update when already at the target version."""
        # Try to update to same version
        success, stdout, stderr = await _run_self_update(executor_with_new, new_release.version)
        assert success, f"Self update to same version failed: {stderr}"
        assert "Already at version" in stdout

        # Version should be unchanged
        version = await _get_installed_version(executor_with_new)
        assert version == new_release.version


class TestSelfUpdateVersionFormats:
    """Tests for version format acceptance (SemVer vs PEP 440)."""

    async def test_semver_format(
        self,
        executor_with_new: BashLoginRemoteExecutor,
        old_release: Release,
    ) -> None:
        """Test that SemVer format is accepted (e.g., 0.1.0-alpha.N)."""
        # Use SemVer format for downgrade (passing raw string, not Version).
        success, _stdout, stderr = await _run_self_update(executor_with_new, old_release.version.semver_str())
        assert success, f"SemVer format failed: {stderr}"

        version = await _get_installed_version(executor_with_new)
        assert version == old_release.version

    async def test_pep440_format(
        self,
        executor_with_new: BashLoginRemoteExecutor,
        old_release: Release,
    ) -> None:
        """Test that PEP 440 format is accepted (e.g., 0.1.0aN)."""
        # Use PEP 440 format for downgrade (passing raw string, not Version)
        success, _stdout, stderr = await _run_self_update(executor_with_new, old_release.version.pep440_str())
        assert success, f"PEP 440 format failed: {stderr}"

        version = await _get_installed_version(executor_with_new)
        # Version comparison handles format differences
        assert version == old_release.version


class TestSelfUpdateErrorHandling:
    """Tests for error handling in self update command."""

    async def test_invalid_version_format(
        self,
        pc1_with_pcswitcher: BashLoginRemoteExecutor,
    ) -> None:
        """Test error handling for invalid version format."""
        success, stdout, _stderr = await _run_self_update(pc1_with_pcswitcher, "not-a-version")
        assert not success, "Should fail with invalid version"
        assert "invalid version format" in stdout.lower() or "error" in stdout.lower()

    async def test_nonexistent_version(
        self,
        pc1_with_pcswitcher: BashLoginRemoteExecutor,
    ) -> None:
        """Test error handling for non-existent version."""
        success, stdout, stderr = await _run_self_update(pc1_with_pcswitcher, "99.99.99")
        assert not success, "Should fail with non-existent version"
        # Error could come from uv or from GitHub API
        assert len(stdout) > 0 or len(stderr) > 0


class TestSelfUpdateNoStableRelease:
    """Tests for behavior when no stable releases exist."""

    async def test_no_stable_release_error(
        self,
        executor_with_new: BashLoginRemoteExecutor,
        old_release: Release,
        github_releases_desc: list[Release],
    ) -> None:
        """Test that self update without --prerelease fails when no stable releases exist."""
        stable_releases = [r for r in github_releases_desc if not r.is_prerelease]
        if stable_releases:
            pytest.skip(
                f"Test requires no stable releases, but found {len(stable_releases)}: "
                f"{[r.tag for r in stable_releases[:3]]}{'...' if len(stable_releases) > 3 else ''}"
            )

        await _install_with_script(executor_with_new, old_release)

        # Without --prerelease, should fail (only prereleases exist)
        success, stdout, _stderr = await _run_self_update(executor_with_new)
        assert not success, "Self update should fail when no stable releases exist"
        assert "error" in stdout.lower(), "Should show error message about no stable releases"
