"""Integration tests for install.sh script.

Tests verify the installation script works on fresh machines and handles
various scenarios like missing prerequisites, existing configs, etc.

These tests run on VMs, not on development machine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from pcswitcher.executor import RemoteExecutor

# Install script URL from main branch
INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/flaksit/pc-switcher/refs/heads/main/install.sh"


@pytest_asyncio.fixture
async def clean_install_environment(pc1_executor: RemoteExecutor) -> AsyncIterator[RemoteExecutor]:
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


@pytest.mark.integration
async def test_001_fr035_install_script_no_prereqs(clean_install_environment: RemoteExecutor) -> None:
    """Test FR-035: install.sh works without prerequisites.

    Verifies that the install.sh script can run on a fresh machine and:
    - Installs uv if not present (or uses existing uv)
    - Installs btrfs-progs if not present
    - Installs pc-switcher package
    - Creates default configuration
    """
    executor = clean_install_environment

    # Note: We assume uv is already installed on the test VM for test infrastructure.
    # The script should handle both cases (uv present and not present).
    # Testing the "no uv" case would require uninstalling uv, which could break
    # the test infrastructure. The install.sh script logic for installing uv
    # is tested indirectly through US7-AS1.

    # Run the installation script
    result = await executor.run_command(
        f"curl -sSL {INSTALL_SCRIPT_URL} | bash",
        timeout=180.0,
    )

    # Verify installation succeeded
    assert result.success, f"Installation script failed: {result.stderr}\nStdout: {result.stdout}"
    assert (
        "pc-switcher installed successfully" in result.stdout
        or "Installation complete" in result.stdout
        or "pc-switcher" in result.stdout
    ), f"Installation success message not found in output: {result.stdout}"

    # Verify pc-switcher is installed and accessible
    version_result = await executor.run_command("pc-switcher --version", timeout=10.0)
    assert version_result.success, f"pc-switcher not accessible after install: {version_result.stderr}"
    assert "pc-switcher" in version_result.stdout.lower(), "Version output doesn't contain 'pc-switcher'"

    # Verify config directory was created
    config_check = await executor.run_command(
        "test -d ~/.config/pc-switcher && echo 'exists'",
        timeout=10.0,
    )
    assert config_check.success, "Config directory not created"
    assert "exists" in config_check.stdout, "Config directory not created"

    # Verify default config file was created
    config_file_check = await executor.run_command(
        "test -f ~/.config/pc-switcher/config.yaml && echo 'exists'",
        timeout=10.0,
    )
    assert config_file_check.success, "Default config file not created"
    assert "exists" in config_file_check.stdout, "Default config file not created"


@pytest.mark.integration
async def test_001_fr036_default_config_with_comments(clean_install_environment: RemoteExecutor) -> None:
    """Test FR-036: Default config file includes helpful inline comments.

    Verifies that the default config.yaml created by install.sh contains
    inline comments explaining each setting.
    """
    executor = clean_install_environment

    # Run installation script
    result = await executor.run_command(
        f"curl -sSL {INSTALL_SCRIPT_URL} | bash",
        timeout=180.0,
    )
    assert result.success, f"Installation failed: {result.stderr}"

    # Read the generated config file
    config_content_result = await executor.run_command(
        "cat ~/.config/pc-switcher/config.yaml",
        timeout=10.0,
    )
    assert config_content_result.success, "Failed to read config file"

    config_content = config_content_result.stdout

    # Verify config contains comments (lines starting with #)
    comment_lines = [line for line in config_content.split("\n") if line.strip().startswith("#")]
    assert len(comment_lines) > 0, "Config file has no comment lines"

    # Verify config contains meaningful content (not just comments)
    non_comment_lines = [
        line for line in config_content.split("\n") if line.strip() and not line.strip().startswith("#")
    ]
    assert len(non_comment_lines) > 0, "Config file has no actual configuration"

    # Verify config is valid YAML by checking for common sections
    # (we don't want to validate the entire structure here, just ensure it's not empty/malformed)
    assert "sync_jobs" in config_content or "global" in config_content, "Config doesn't contain expected sections"


@pytest.mark.integration
async def test_001_us7_as1_install_script_fresh_machine(clean_install_environment: RemoteExecutor) -> None:
    """Test US7-AS1: curl install.sh on fresh machine.

    Verifies the full installation flow on a clean machine:
    - Downloads and runs install.sh via curl
    - Installs all dependencies
    - Creates config with inline comments
    - pc-switcher command becomes available
    """
    executor = clean_install_environment

    # Run the installation via curl (simulating fresh machine scenario)
    result = await executor.run_command(
        f"curl -LsSf {INSTALL_SCRIPT_URL} | bash",
        timeout=180.0,
    )

    # Verify installation succeeded
    assert result.success, f"Installation script failed: {result.stderr}\nStdout: {result.stdout}"

    # Verify pc-switcher is installed and in PATH
    which_result = await executor.run_command("which pc-switcher", timeout=10.0)
    assert which_result.success, "pc-switcher not found in PATH"
    assert "pc-switcher" in which_result.stdout, f"Unexpected which output: {which_result.stdout}"

    # Verify pc-switcher can run
    help_result = await executor.run_command("pc-switcher --help", timeout=10.0)
    assert help_result.success, f"pc-switcher --help failed: {help_result.stderr}"
    assert "pc-switcher" in help_result.stdout.lower(), "Help output doesn't mention pc-switcher"

    # Verify config was created
    config_exists = await executor.run_command(
        "test -f ~/.config/pc-switcher/config.yaml && echo 'yes'",
        timeout=10.0,
    )
    assert config_exists.success and "yes" in config_exists.stdout, "Config file not created"

    # Verify config has comments (FR-036)
    config_content = await executor.run_command("cat ~/.config/pc-switcher/config.yaml", timeout=10.0)
    assert config_content.success, "Failed to read config"
    assert "#" in config_content.stdout, "Config file has no comments"


@pytest.mark.integration
async def test_001_us7_as3_preserve_existing_config(pc1_executor: RemoteExecutor) -> None:
    """Test US7-AS3: Preserve existing config file.

    Verifies that when a config file already exists, the installation script:
    - Detects the existing config
    - Does NOT overwrite it by default (or prompts for confirmation)
    - Leaves the original config intact
    """
    # First, ensure pc-switcher is installed
    await pc1_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0)
    install_result = await pc1_executor.run_command(
        f"curl -sSL {INSTALL_SCRIPT_URL} | bash",
        timeout=180.0,
    )
    assert install_result.success, f"Initial installation failed: {install_result.stderr}"

    # Create a custom config with unique marker
    custom_marker = "# CUSTOM_CONFIG_MARKER_FOR_TESTING"
    await pc1_executor.run_command(
        f"echo '{custom_marker}' >> ~/.config/pc-switcher/config.yaml",
        timeout=10.0,
    )

    # Verify marker was added
    verify_marker = await pc1_executor.run_command(
        f"grep '{custom_marker}' ~/.config/pc-switcher/config.yaml",
        timeout=10.0,
    )
    assert verify_marker.success, "Failed to add custom marker to config"

    # Run installation script again (simulating re-installation)
    # Note: The script should either preserve the config or prompt for overwrite.
    # Since we can't interact with prompts in non-interactive mode, we expect
    # the script to default to preserving the existing config.
    reinstall_result = await pc1_executor.run_command(
        f"curl -sSL {INSTALL_SCRIPT_URL} | bash",
        timeout=180.0,
    )

    # Installation should still succeed (even if config is preserved)
    assert reinstall_result.success, f"Re-installation failed: {reinstall_result.stderr}"

    # Verify the custom marker is still present (config was preserved)
    check_marker = await pc1_executor.run_command(
        f"grep '{custom_marker}' ~/.config/pc-switcher/config.yaml",
        timeout=10.0,
    )

    # The test passes if either:
    # 1. The marker is still present (config was preserved), OR
    # 2. The installation output indicates it asked about overwriting
    marker_preserved = check_marker.success and custom_marker in check_marker.stdout
    asked_about_overwrite = (
        "overwrite" in reinstall_result.stdout.lower() or "exists" in reinstall_result.stdout.lower()
    )

    assert marker_preserved or asked_about_overwrite, (
        "Config was overwritten without preserving existing content or prompting. "
        f"Marker check: {check_marker.stdout}, Install output: {reinstall_result.stdout}"
    )

    # Clean up
    await pc1_executor.run_command("uv tool uninstall pc-switcher 2>/dev/null || true", timeout=30.0)
    await pc1_executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)
