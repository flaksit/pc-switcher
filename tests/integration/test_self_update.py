"""Integration tests for pc-switcher self update command.

These tests install pc-switcher on a pristine VM and verify the update workflow against real GitHub releases.
"""

from __future__ import annotations

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.version import Release, Version

from .conftest import get_installed_version, install_pcswitcher_with_script, install_pcswitcher_with_uv

_MIN_VERSION_WITH_SELF_UPDATE = Version.parse("0.1.0-alpha.4")


@pytest.fixture(scope="session")
def new_release(highest_release: Release) -> Release:
    """The highest GitHub release version.

    Skips any tests depending on this fixture if the the release predates self-update
    and GITHUB_TOKEN propagation requirements.
    """
    if highest_release < _MIN_VERSION_WITH_SELF_UPDATE:
        pytest.skip(
            f"Highest release ({highest_release}) is < {_MIN_VERSION_WITH_SELF_UPDATE};"
            "skipping self-update tests"
        )
    return highest_release


@pytest.fixture(scope="session")
def old_release(next_highest_release: Release) -> Release:
    """The next-highest GitHub release version.

    Skips any tests depending on this fixture if the the release predates self-update
    and GITHUB_TOKEN propagation requirements.
    """
    if next_highest_release < _MIN_VERSION_WITH_SELF_UPDATE:
        pytest.skip(
            f"Next-highest release ({next_highest_release}) is < {_MIN_VERSION_WITH_SELF_UPDATE}; "
            "skipping self-update tests"
        )
    return next_highest_release


@pytest.fixture(scope="module")
async def pc2_executor_with_prerequisites(
    pc2_executor: BashLoginRemoteExecutor,
) -> BashLoginRemoteExecutor:
    """Install the prerequisites for pc-switcher."""

    await install_pcswitcher_with_script(pc2_executor)
    # await _uninstall_via_uv(pc1_executor)

    return pc2_executor


@pytest.fixture
async def pc2_executor_with_old(
    pc2_executor_with_prerequisites: BashLoginRemoteExecutor,
    old_release: Release,
) -> BashLoginRemoteExecutor:
    """Install the current pc-switcher version before each test."""
    await install_pcswitcher_with_uv(pc2_executor_with_prerequisites, release=old_release)

    return pc2_executor_with_prerequisites


@pytest.fixture
async def pc2_executor_with_new(
    pc2_executor_with_prerequisites: BashLoginRemoteExecutor,
    new_release: Release,
) -> BashLoginRemoteExecutor:
    """Install the current pc-switcher version before each test."""
    await install_pcswitcher_with_uv(pc2_executor_with_prerequisites, release=new_release)

    return pc2_executor_with_prerequisites


async def _run_self_update(
    executor: BashLoginRemoteExecutor,
    version: Release | Version | str | None = None,
    prerelease: bool = False,
) -> tuple[bool, str, str]:
    """Run pc-switcher self update and return (success, stdout, stderr)."""
    cmd = "pc-switcher self update"
    if prerelease:
        cmd += " --prerelease"
    if isinstance(version, Release):
        version = version.version
    if version:
        # Use SemVer format for version argument
        # If it is a Version, it will convert to SemVer string
        cmd += f" {version}"

    result = await executor.run_command(cmd, timeout=120.0)
    return result.success, result.stdout, result.stderr


class TestSelfUpdateCommandExists:
    """Tests verifying which versions have the self-update command."""

    async def test_old_version_has_self_update(
        self,
        pc2_executor_with_old: BashLoginRemoteExecutor,
    ) -> None:
        """Test that the next-highest release has the self update command."""
        # Self update command should exist
        result = await pc2_executor_with_old.run_command("pc-switcher self update --help", timeout=10.0)
        assert result.success, f"Self update help failed: {result.stderr}"
        assert "update" in result.stdout.lower()

    async def test_has_self_update(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    ) -> None:
        # Self update command should exist
        result = await pc1_with_pcswitcher_mod.run_command("pc-switcher self update --help", timeout=10.0)
        assert result.success, f"Self update help failed: {result.stderr}"
        assert "update" in result.stdout.lower()

    async def test_self_command_group_help(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    ) -> None:
        """Test that 'pc-switcher self --help' shows the command group."""
        result = await pc1_with_pcswitcher_mod.run_command("pc-switcher self --help", timeout=10.0)
        assert result.success, f"Self help failed: {result.stderr}"
        # Should show "self" command group and list subcommands
        assert "update" in result.stdout.lower()
        assert "manage" in result.stdout.lower() and "self" in result.stdout.lower()

    async def test_self_update_help_shows_prerelease_flag(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    ) -> None:
        """Test that 'pc-switcher self update --help' documents --prerelease flag."""
        result = await pc1_with_pcswitcher_mod.run_command("pc-switcher self update --help", timeout=10.0)
        assert result.success, f"Self update help failed: {result.stderr}"
        # Should document the --prerelease option
        assert "--prerelease" in result.stdout


async def test_upgrade_to_specific_version(
    pc2_executor_with_old: BashLoginRemoteExecutor,
    new_release: Release,
) -> None:
    """Test upgrading from the next-highest release to the highest release."""
    # Use self update to upgrade
    success, stdout, stderr = await _run_self_update(pc2_executor_with_old, new_release)
    assert success, f"Self update failed: {stderr}"
    assert "Successfully updated" in stdout or new_release.version.semver_str() in stdout

    # Verify new version
    new_version = await get_installed_version(pc2_executor_with_old)
    assert new_version == new_release


async def test_upgrade_with_prerelease_flag(
    pc2_executor_with_old: BashLoginRemoteExecutor,
    new_release: Release,
) -> None:
    """Test using --prerelease flag to find latest prerelease."""
    # Use self update with --prerelease (should find latest prerelease)
    success, stdout, stderr = await _run_self_update(pc2_executor_with_old, prerelease=True)
    assert success, f"Self update --prerelease failed:\n--- STDOUT ---\n{stdout}\n\n--- STDERR ---\n{stderr}"

    # Should have upgraded to at least the highest release
    new_version = await get_installed_version(pc2_executor_with_old)
    assert new_version >= new_release


async def test_downgrade_to_specific_version(
    pc2_executor_with_new: BashLoginRemoteExecutor,
    old_release: Release,
) -> None:
    """Test downgrading from the highest release to the next-highest release."""
    # Use self update to downgrade
    success, stdout, stderr = await _run_self_update(pc2_executor_with_new, old_release)
    assert success, f"Self update (downgrade) failed: {stderr}"
    assert "downgrading" in stdout.lower() and "warning" in stdout.lower()

    # Verify downgraded version
    new_version = await get_installed_version(pc2_executor_with_new)
    assert new_version == old_release


class TestSelfUpdateSameVersion:
    """Tests for self update when already at target version."""

    async def test_already_at_version(
        self,
        pc2_executor_with_new: BashLoginRemoteExecutor,
        new_release: Release,
    ) -> None:
        """Test self update when already at the target version."""
        # Try to update to same version
        success, stdout, stderr = await _run_self_update(pc2_executor_with_new, new_release)
        assert success, f"Self update to same version failed: {stderr}"
        assert "Already at version" in stdout

        # Version should be unchanged
        version = await get_installed_version(pc2_executor_with_new)
        assert version == new_release


class TestSelfUpdateVersionFormats:
    """Tests for version format acceptance (SemVer vs PEP 440)."""

    async def test_semver_format(
        self,
        pc2_executor_with_new: BashLoginRemoteExecutor,
        old_release: Release,
    ) -> None:
        """Test that SemVer format is accepted (e.g., 0.1.0-alpha.N)."""
        # Use SemVer format for downgrade (passing raw string, not Version).
        success, _stdout, stderr = await _run_self_update(pc2_executor_with_new, old_release.version.semver_str())
        assert success, f"SemVer format failed: {stderr}"

        version = await get_installed_version(pc2_executor_with_new)
        assert version == old_release

    async def test_pep440_format(
        self,
        pc2_executor_with_new: BashLoginRemoteExecutor,
        old_release: Release,
    ) -> None:
        """Test that PEP 440 format is accepted (e.g., 0.1.0aN)."""
        # Use PEP 440 format for downgrade (passing raw string, not Version)
        success, _stdout, stderr = await _run_self_update(pc2_executor_with_new, old_release.version.pep440_str())
        assert success, f"PEP 440 format failed: {stderr}"

        version = await get_installed_version(pc2_executor_with_new)
        # Version comparison handles format differences
        assert version == old_release


class TestSelfUpdateErrorHandling:
    """Tests for error handling in self update command."""

    async def test_invalid_version_format(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    ) -> None:
        """Test error handling for invalid version format."""
        success, stdout, _stderr = await _run_self_update(pc1_with_pcswitcher_mod, "not-a-version")
        assert not success, "Should fail with invalid version"
        assert "invalid version format" in stdout.lower() or "error" in stdout.lower()

    async def test_nonexistent_version(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    ) -> None:
        """Test error handling for non-existent version."""
        success, stdout, stderr = await _run_self_update(pc1_with_pcswitcher_mod, "99.99.99")
        assert not success, "Should fail with non-existent version"
        # Error could come from uv or from GitHub API
        assert len(stdout) > 0 or len(stderr) > 0


class TestSelfUpdateNoStableRelease:
    """Tests for behavior when no stable releases exist."""

    async def test_no_stable_release_error(
        self,
        pc2_executor_with_new: BashLoginRemoteExecutor,
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

        await install_pcswitcher_with_script(pc2_executor_with_new, release=old_release)

        # Without --prerelease, should fail (only prereleases exist)
        success, stdout, _stderr = await _run_self_update(pc2_executor_with_new)
        assert not success, "Self update should fail when no stable releases exist"
        assert "error" in stdout.lower(), "Should show error message about no stable releases"
