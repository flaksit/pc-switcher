"""Integration tests for install.sh script.

Tests verify the installation script works on fresh machines, handles various
scenarios like missing prerequisites, and can install/upgrade specific versions.

These tests run on VMs, not on development machine.

**What these tests cover:**
- install.sh works without prerequisites (fresh install)
- install.sh can install specific versions via VERSION parameter
- install.sh can upgrade from older to newer versions
- Version-specific installation matches expected behavior

**What these tests do NOT cover:**
- InstallOnTargetJob class methods (see jobs/test_install_on_target_job.py)
- Config synchronization (see jobs/test_install_on_target_job.py)
- Pre/post-sync operations (see test_snapshot_infrastructure.py, etc.)
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.version import Version, find_one_version, get_this_version

# Install script URL from main branch
INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh"


def _get_release_version() -> Version:
    """Get the release version from current version, stripping post/dev/local parts.

    E.g., "0.1.0-alpha.3+post.23.dev.0.da749fc" -> "0.1.0-alpha.3"
    This gives us a version that has a corresponding git tag on GitHub.
    """
    current = get_this_version()
    return current.release_version()


@pytest_asyncio.fixture
async def clean_install_environment(pc1_executor: BashLoginRemoteExecutor) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide a clean environment for testing installation.

    Removes pc-switcher, config, and optionally uv to simulate fresh machine.
    Restores the environment after test.
    """
    # Backup existing config if it exists
    await pc1_executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml ]; then "
        "cp ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.backup; "
        "fi",
        timeout=10.0,
    )

    # Clean up pc-switcher installation
    await pc1_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0)
    await pc1_executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)

    yield pc1_executor

    # Restore environment
    await pc1_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0)
    await pc1_executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)
    await pc1_executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.backup ]; then "
        "mkdir -p ~/.config/pc-switcher && "
        "mv ~/.config/pc-switcher/config.yaml.backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


async def test_001_fr035_install_script_no_prereqs(clean_install_environment: BashLoginRemoteExecutor) -> None:
    """Test FR-035: install.sh works without prerequisites.

    Verifies that the install.sh script can run on a fresh machine and:
    - Installs uv if not present (or uses existing uv)
    - Installs btrfs-progs if not present (prompts user)
    - Installs pc-switcher package
    - Does NOT create default config (user must run pc-switcher init)
    - Shows instructions to run pc-switcher init
    """
    executor = clean_install_environment

    # Note: We assume uv is already installed on the test VM for test infrastructure.
    # The script should handle both cases (uv present and not present).
    # Testing the "no uv" case would require uninstalling uv, which could break
    # the test infrastructure.

    # Run the installation script
    result = await executor.run_command(
        f"curl -sSL {INSTALL_SCRIPT_URL} | bash",
        timeout=180.0,
    )

    # Verify installation succeeded
    assert result.success, f"Installation script failed: {result.stderr}\nStdout: {result.stdout}"
    assert (
        "pc-switcher installed successfully" in result.stdout
        or "pc-switcher upgraded successfully" in result.stdout
        or "Installation complete" in result.stdout
    ), f"Installation success message not found in output: {result.stdout}"

    # Verify pc-switcher is installed and accessible
    version_result = await executor.run_command("pc-switcher --version", timeout=10.0)
    assert version_result.success, f"pc-switcher not accessible after install: {version_result.stderr}"
    assert "pc-switcher" in version_result.stdout.lower(), "Version output doesn't contain 'pc-switcher'"

    # Verify NO default config file was created (user must run pc-switcher init)
    config_file_check = await executor.run_command(
        "test -f ~/.config/pc-switcher/config.yaml && echo 'exists' || echo 'not_found'",
        timeout=10.0,
    )
    assert "not_found" in config_file_check.stdout, (
        "install.sh should NOT create default config - user must run 'pc-switcher init'"
    )

    # Verify installation output includes instructions to run pc-switcher init
    assert "pc-switcher init" in result.stdout, (
        "Installation output should include instructions to run 'pc-switcher init'"
    )


# Tests test_001_fr036_default_config_with_comments, test_001_us7_as1_install_script_fresh_machine,
# and test_001_us7_as3_preserve_existing_config were removed because:
# - install.sh does NOT create a default config file
# - Users must run "pc-switcher init" to create the config
# - Tests for "pc-switcher init" belong in tests/unit/test_cli.py or similar
# See specs/001-foundation/spec.md for the documented workflow.


class TestInstallationScriptVersionParameter:
    """Tests for install.sh with VERSION parameter.

    These tests verify the installation infrastructure works correctly by using
    the RELEASE VERSION derived from the current version (stripping post/dev/local
    parts). This allows the tests to run during development when the full source
    version doesn't have a corresponding release tag.

    This tests:
    - install.sh script works with VERSION parameter
    - Version-specific installation from GitHub releases
    - Upgrade path from one version to another
    """

    async def test_001_install_release_version_on_clean_target(
        self,
        pc2_executor_without_pcswitcher_tool: BashLoginRemoteExecutor,
    ) -> None:
        """Test installing the release version on a clean target.

        Verifies that the install.sh script can install a specific version
        when pc-switcher is not already installed.
        """
        release_version = _get_release_version()

        # Install the release version using install.sh
        cmd = f"curl -LsSf {INSTALL_SCRIPT_URL} | VERSION={release_version.semver_str()} bash"
        result = await pc2_executor_without_pcswitcher_tool.run_command(cmd, timeout=120.0)
        assert result.success, f"Installation failed: {result.stderr}"

        # Verify pc-switcher is now installed
        result = await pc2_executor_without_pcswitcher_tool.run_command("pc-switcher --version")
        assert result.success, f"pc-switcher should be installed: {result.stderr}"

        # Verify installed version matches expected
        installed_version = find_one_version(result.stdout)
        assert installed_version == release_version, (
            f"Installed version {installed_version} should match {release_version}"
        )

    async def test_001_upgrade_from_older_version(
        self,
        pc2_executor_with_old_pcswitcher_tool: BashLoginRemoteExecutor,
    ) -> None:
        """Test upgrading from an older version to the release version.

        Verifies that the install.sh script can upgrade pc-switcher from
        an older version to a newer version.

        Note: This uses the pc2_executor_with_old_pcswitcher_tool fixture which installs
        0.1.0-alpha.1, then we upgrade to the current release version.
        """
        release_version = _get_release_version()
        old_version = Version.parse("0.1.0-alpha.1")

        # Skip if release version is not newer than old version
        if release_version <= old_version:
            pytest.skip(f"Release version {release_version} is not newer than {old_version}")

        # Upgrade to the release version
        cmd = f"curl -LsSf {INSTALL_SCRIPT_URL} | VERSION={release_version.semver_str()} bash"
        result = await pc2_executor_with_old_pcswitcher_tool.run_command(cmd, timeout=120.0)
        assert result.success, f"Upgrade failed: {result.stderr}"

        # Verify version changed to release version
        result = await pc2_executor_with_old_pcswitcher_tool.run_command("pc-switcher --version")
        assert result.success, f"pc-switcher should be available: {result.stderr}"
        new_version = find_one_version(result.stdout)

        assert new_version == release_version, f"New version {new_version} should be {release_version}"
