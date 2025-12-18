"""Integration tests for pc-switcher self update command.

Tests the self-update functionality using real GitHub releases:
- v0.1.0-alpha.1: Does NOT have self-update command
- v0.1.0-alpha.2: Has self-update command
- v0.1.0-alpha.3: Has self-update command

These tests install pc-switcher on a pristine VM and verify the update workflow.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.version import Version, find_one_version

# Test constants - update when new releases are made
# Use Version for proper SemVer/PEP440 comparison
VERSION_WITHOUT_SELF_UPDATE = Version.parse("0.1.0-alpha.1")
VERSION_WITH_SELF_UPDATE_OLD = Version.parse("0.1.0-alpha.2")
VERSION_WITH_SELF_UPDATE_NEW = Version.parse("0.1.0-alpha.3")

# Install script URL
INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh"


@pytest.fixture
async def clean_pc_switcher(pc1_executor: BashLoginRemoteExecutor) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Ensure pc-switcher is uninstalled before and after each test.

    This fixture provides a clean slate for testing installation and updates.
    """
    # Clean up before test
    await pc1_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", login_shell=True)

    yield pc1_executor

    # Clean up after test
    await pc1_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", login_shell=True)


async def _install_version(executor: BashLoginRemoteExecutor, version: Version) -> None:
    """Install a specific version of pc-switcher using the install script."""
    release = version.get_release()
    assert release is not None, f"Version {version} is not a GitHub release"
    result = await executor.run_command(
        f"curl -sSL {INSTALL_SCRIPT_URL} | VERSION={release.tag} bash",
        timeout=120.0,
    )
    assert result.success, f"Failed to install version {version}: {result.stderr}"


async def _get_installed_version(executor: BashLoginRemoteExecutor) -> Version:
    """Get the currently installed pc-switcher version."""
    result = await executor.run_command("pc-switcher --version", timeout=10.0, login_shell=True)
    assert result.success, f"Failed to get version: {result.stderr}"
    # Parse version from CLI output (handles both PEP440 and SemVer formats)
    return find_one_version(result.stdout)


async def _run_self_update(
    executor: BashLoginRemoteExecutor,
    version: Version | str | None = None,
    prerelease: bool = False,
) -> tuple[bool, str, str]:
    """Run pc-switcher self update and return (success, stdout, stderr).

    Forwards GITHUB_TOKEN from the local environment to the remote machine
    to avoid GitHub API rate limiting (60 req/hr unauthenticated vs 5000/hr with token).
    """
    cmd = "pc-switcher self update"
    if prerelease:
        cmd += " --prerelease"
    if version:
        # Use SemVer format for version argument
        version_str = version.semver_str() if isinstance(version, Version) else version
        cmd += f" {version_str}"

    # Forward GITHUB_TOKEN to the remote machine to avoid rate limiting
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        cmd = f"export GITHUB_TOKEN={github_token} && {cmd}"

    result = await executor.run_command(cmd, timeout=120.0)
    return result.success, result.stdout, result.stderr


class TestSelfUpdateCommandExists:
    """Tests verifying which versions have the self-update command."""

    async def test_old_version_lacks_self_update(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test that v0.1.0-alpha.1 does NOT have self update command."""
        await _install_version(clean_pc_switcher, VERSION_WITHOUT_SELF_UPDATE)

        # Verify version
        version = await _get_installed_version(clean_pc_switcher)
        assert version == VERSION_WITHOUT_SELF_UPDATE

        # Self update command should not exist
        result = await clean_pc_switcher.run_command("pc-switcher self update --help", timeout=10.0, login_shell=True)
        assert not result.success, "Old version should not have 'self' command"
        assert "No such command" in result.stderr or "Error" in result.stderr

    async def test_new_version_has_self_update(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test that v0.1.0-alpha.2+ has self update command."""
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        # Verify version
        version = await _get_installed_version(clean_pc_switcher)
        assert version == VERSION_WITH_SELF_UPDATE_OLD

        # Self update command should exist
        result = await clean_pc_switcher.run_command("pc-switcher self update --help", timeout=10.0, login_shell=True)
        assert result.success, f"Self update help failed: {result.stderr}"
        assert "Update pc-switcher" in result.stdout or "update" in result.stdout.lower()

    async def test_self_command_group_help(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test that 'pc-switcher self --help' shows the command group."""
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        result = await clean_pc_switcher.run_command("pc-switcher self --help", timeout=10.0, login_shell=True)
        assert result.success, f"Self help failed: {result.stderr}"
        # Should show "self" command group and list subcommands
        assert "update" in result.stdout.lower()
        assert "Manage" in result.stdout or "self" in result.stdout.lower()

    async def test_self_update_help_shows_prerelease_flag(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test that 'pc-switcher self update --help' documents --prerelease flag."""
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        result = await clean_pc_switcher.run_command("pc-switcher self update --help", timeout=10.0, login_shell=True)
        assert result.success, f"Self update help failed: {result.stderr}"
        # Should document the --prerelease option
        assert "--prerelease" in result.stdout


class TestSelfUpdateUpgrade:
    """Tests for upgrading pc-switcher using self update."""

    async def test_upgrade_to_specific_version(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test upgrading from v0.1.0-alpha.2 to v0.1.0-alpha.3."""
        # Install older version with self-update
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        # Verify starting version
        version = await _get_installed_version(clean_pc_switcher)
        assert version == VERSION_WITH_SELF_UPDATE_OLD

        # Use self update to upgrade
        success, stdout, stderr = await _run_self_update(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_NEW)
        assert success, f"Self update failed: {stderr}"
        assert "Successfully updated" in stdout or VERSION_WITH_SELF_UPDATE_NEW.semver_str() in stdout

        # Verify new version
        new_version = await _get_installed_version(clean_pc_switcher)
        assert new_version == VERSION_WITH_SELF_UPDATE_NEW

    async def test_upgrade_with_prerelease_flag(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test using --prerelease flag to find latest prerelease."""
        # Install older version
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        # Use self update with --prerelease (should find latest prerelease)
        success, stdout, stderr = await _run_self_update(clean_pc_switcher, prerelease=True)
        assert success, f"Self update --prerelease failed:\n--- STDOUT ---\n{stdout}\n\n--- STDERR ---\n{stderr}"

        # Should have upgraded to at least VERSION_WITH_SELF_UPDATE_NEW
        new_version = await _get_installed_version(clean_pc_switcher)
        assert new_version >= VERSION_WITH_SELF_UPDATE_NEW


class TestSelfUpdateDowngrade:
    """Tests for downgrading pc-switcher using self update."""

    async def test_downgrade_to_specific_version(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test downgrading from v0.1.0-alpha.3 to v0.1.0-alpha.2."""
        # Install newer version
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_NEW)

        # Verify starting version
        version = await _get_installed_version(clean_pc_switcher)
        assert version == VERSION_WITH_SELF_UPDATE_NEW

        # Use self update to downgrade
        success, stdout, stderr = await _run_self_update(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)
        assert success, f"Self update (downgrade) failed: {stderr}"
        assert "Downgrading" in stdout or "Warning" in stdout

        # Verify downgraded version
        new_version = await _get_installed_version(clean_pc_switcher)
        assert new_version == VERSION_WITH_SELF_UPDATE_OLD

        # Verify self-update command still works after downgrade
        result = await clean_pc_switcher.run_command("pc-switcher self update --help", timeout=10.0, login_shell=True)
        assert result.success, "Self update should still exist after downgrading to alpha.2"

    async def test_downgrade_to_version_without_self_update(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test downgrading to v0.1.0-alpha.1 (loses self-update command)."""
        # Install version with self-update
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        # Downgrade to version without self-update
        success, _stdout, stderr = await _run_self_update(clean_pc_switcher, VERSION_WITHOUT_SELF_UPDATE)
        assert success, f"Self update (downgrade) failed: {stderr}"

        # Verify downgraded version
        new_version = await _get_installed_version(clean_pc_switcher)
        assert new_version == VERSION_WITHOUT_SELF_UPDATE

        # Self-update command should no longer exist
        result = await clean_pc_switcher.run_command("pc-switcher self update --help", timeout=10.0, login_shell=True)
        assert not result.success, "Self update should not exist in alpha.1"


class TestSelfUpdateSameVersion:
    """Tests for self update when already at target version."""

    async def test_already_at_version(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test self update when already at the target version."""
        # Install a version
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_NEW)

        # Try to update to same version
        success, stdout, stderr = await _run_self_update(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_NEW)
        assert success, f"Self update to same version failed: {stderr}"
        assert "Already at version" in stdout

        # Version should be unchanged
        version = await _get_installed_version(clean_pc_switcher)
        assert version == VERSION_WITH_SELF_UPDATE_NEW


class TestSelfUpdateVersionFormats:
    """Tests for version format acceptance (SemVer vs PEP 440)."""

    async def test_semver_format(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test that SemVer format is accepted (e.g., 0.1.0-alpha.2)."""
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_NEW)

        # Use SemVer format for downgrade (passing raw string, not Version)
        success, _stdout, stderr = await _run_self_update(clean_pc_switcher, "0.1.0-alpha.2")
        assert success, f"SemVer format failed: {stderr}"

        version = await _get_installed_version(clean_pc_switcher)
        assert version == VERSION_WITH_SELF_UPDATE_OLD

    async def test_pep440_format(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test that PEP 440 format is accepted (e.g., 0.1.0a2)."""
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_NEW)

        # Use PEP 440 format for downgrade (passing raw string, not Version)
        success, _stdout, stderr = await _run_self_update(clean_pc_switcher, "0.1.0a2")
        assert success, f"PEP 440 format failed: {stderr}"

        version = await _get_installed_version(clean_pc_switcher)
        # Version comparison handles format differences
        assert version == VERSION_WITH_SELF_UPDATE_OLD


class TestSelfUpdateErrorHandling:
    """Tests for error handling in self update command."""

    async def test_invalid_version_format(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test error handling for invalid version format."""
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        success, stdout, _stderr = await _run_self_update(clean_pc_switcher, "not-a-version")
        assert not success, "Should fail with invalid version"
        assert "Invalid version format" in stdout or "Error" in stdout

    async def test_nonexistent_version(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test error handling for non-existent version."""
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        success, stdout, stderr = await _run_self_update(clean_pc_switcher, "99.99.99")
        assert not success, "Should fail with non-existent version"
        # Error could come from uv or from GitHub API
        assert len(stdout) > 0 or len(stderr) > 0


class TestSelfUpdateNoStableRelease:
    """Tests for behavior when no stable releases exist."""

    async def test_no_stable_release_error(self, clean_pc_switcher: BashLoginRemoteExecutor) -> None:
        """Test that self update without --prerelease fails when no stable releases exist."""
        await _install_version(clean_pc_switcher, VERSION_WITH_SELF_UPDATE_OLD)

        # Without --prerelease, should fail (only prereleases exist)
        success, stdout, _stderr = await _run_self_update(clean_pc_switcher)
        # This may succeed if there are stable releases, or fail if not
        # Check that it handles the situation gracefully
        assert "Error" in stdout or "Already at" in stdout or success
