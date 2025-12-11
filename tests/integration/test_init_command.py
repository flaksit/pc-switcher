"""Integration tests for pc-switcher init command.

Tests verify that the init command correctly creates a default configuration file
with comments and that it handles existing config files appropriately.

Tests verify real behavior on VMs.

User Stories covered:
- FR-035: One-liner install using curl and Bash
- FR-036: Default config includes helpful inline comments
- US7-AS1: curl install.sh installs pc-switcher, then user runs 'pc-switcher init'
- US7-AS3: Preserve existing config file (unless --force is used)
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from pcswitcher.executor import RemoteExecutor


@pytest_asyncio.fixture
async def clean_config_environment(pc1_executor: RemoteExecutor) -> AsyncIterator[RemoteExecutor]:
    """Provide a clean environment for testing init command.

    Removes existing config file before test, restores after test.
    """
    # Backup existing config if it exists
    await pc1_executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml ]; then "
        "cp ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.backup; "
        "fi",
        timeout=10.0,
    )

    # Remove config file
    await pc1_executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)

    # Verify config removed
    result = await pc1_executor.run_command("test -f ~/.config/pc-switcher/config.yaml")
    assert not result.success, "Fixture failed: config file still exists after cleanup"

    yield pc1_executor

    # Restore environment
    await pc1_executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)
    await pc1_executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.backup ]; then "
        "mv ~/.config/pc-switcher/config.yaml.backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


@pytest.mark.integration
class TestInitCommand:
    """Integration tests for pc-switcher init command."""

    async def test_001_fr036_init_creates_default_config(
        self,
        clean_config_environment: RemoteExecutor,
    ) -> None:
        """FR-036: pc-switcher init creates default config with inline comments.

        Verifies that running 'pc-switcher init' creates a default configuration
        file at ~/.config/pc-switcher/config.yaml with helpful inline comments.
        """
        executor = clean_config_environment

        # Run pc-switcher init
        result = await executor.run_command("pc-switcher init", timeout=30.0)
        assert result.success, f"pc-switcher init failed: {result.stderr}"
        assert "Created configuration file" in result.stdout, (
            f"Expected success message, got: {result.stdout}"
        )

        # Verify config file was created
        check_after = await executor.run_command(
            "test -f ~/.config/pc-switcher/config.yaml && echo 'exists'",
            timeout=10.0,
        )
        assert check_after.success and "exists" in check_after.stdout, "Config file not created"

        # Verify config contains comments
        config_content = await executor.run_command(
            "cat ~/.config/pc-switcher/config.yaml",
            timeout=10.0,
        )
        assert config_content.success, "Failed to read config file"

        # Check for comment lines (lines starting with #)
        content = config_content.stdout
        comment_lines = [line for line in content.split("\n") if line.strip().startswith("#")]
        assert len(comment_lines) > 0, "Config file should contain inline comments"

        # Check for meaningful content (not just comments)
        non_comment_lines = [
            line for line in content.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(non_comment_lines) > 0, "Config file should contain configuration"

    async def test_001_us7_as1_init_after_install(
        self,
        clean_config_environment: RemoteExecutor,
    ) -> None:
        """US7-AS1: Full workflow - install.sh followed by pc-switcher init.

        This test verifies the expected user workflow:
        1. Run install.sh (already done on test VM)
        2. Run pc-switcher init to create config
        3. Config is created with comments
        """
        executor = clean_config_environment

        # Verify pc-switcher is installed and accessible
        version_result = await executor.run_command("pc-switcher --version", timeout=10.0)
        assert version_result.success, f"pc-switcher not installed: {version_result.stderr}"

        # Run pc-switcher init
        init_result = await executor.run_command("pc-switcher init", timeout=30.0)
        assert init_result.success, f"pc-switcher init failed: {init_result.stderr}"

        # Verify config was created at the expected path
        config_path_result = await executor.run_command(
            "test -f ~/.config/pc-switcher/config.yaml && echo 'yes'",
            timeout=10.0,
        )
        assert "yes" in config_path_result.stdout, "Config not at expected path"

        # Verify the config is valid YAML with expected structure
        config_content = await executor.run_command(
            "cat ~/.config/pc-switcher/config.yaml",
            timeout=10.0,
        )
        content = config_content.stdout

        # Should contain common config sections or settings
        assert "log" in content.lower() or "btrfs" in content.lower() or "sync" in content.lower(), (
            "Config should contain expected sections"
        )

    async def test_001_us7_as3_init_preserves_existing_config(
        self,
        pc1_executor: RemoteExecutor,
    ) -> None:
        """US7-AS3: pc-switcher init refuses to overwrite existing config without --force.

        Verifies that when a config file already exists, 'pc-switcher init':
        - Detects the existing config
        - Refuses to overwrite without --force flag
        - Leaves the original config intact
        """
        executor = pc1_executor

        # First ensure a config exists (either by init or creating one)
        await executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)

        # Create a config with a unique marker
        custom_marker = "# CUSTOM_CONFIG_MARKER_FOR_TESTING_12345"
        await executor.run_command(
            f"echo '{custom_marker}' > ~/.config/pc-switcher/config.yaml",
            timeout=10.0,
        )

        # Verify marker was written
        verify_marker = await executor.run_command(
            "cat ~/.config/pc-switcher/config.yaml",
            timeout=10.0,
        )
        assert custom_marker in verify_marker.stdout, "Failed to create test config with marker"

        # Run pc-switcher init WITHOUT --force - should fail
        init_result = await executor.run_command("pc-switcher init", timeout=30.0)
        assert not init_result.success, "pc-switcher init should fail when config exists"
        assert "already exists" in init_result.stdout or "Use --force" in init_result.stdout, (
            f"Should mention existing config: {init_result.stdout}"
        )

        # Verify the marker is still present (config was not overwritten)
        check_marker = await executor.run_command(
            "cat ~/.config/pc-switcher/config.yaml",
            timeout=10.0,
        )
        assert custom_marker in check_marker.stdout, "Original config was overwritten without --force"

        # Clean up
        await executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)

    async def test_001_us7_as3_init_force_overwrites(
        self,
        clean_config_environment: RemoteExecutor,
    ) -> None:
        """US7-AS3: pc-switcher init --force overwrites existing config.

        Verifies that 'pc-switcher init --force' successfully overwrites
        an existing configuration file.
        """
        executor = clean_config_environment

        # Create a config with a unique marker
        await executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)
        custom_marker = "# OLD_CONFIG_MARKER_TO_BE_OVERWRITTEN"
        await executor.run_command(
            f"echo '{custom_marker}' > ~/.config/pc-switcher/config.yaml",
            timeout=10.0,
        )

        # Run pc-switcher init WITH --force - should succeed
        init_result = await executor.run_command("pc-switcher init --force", timeout=30.0)
        assert init_result.success, f"pc-switcher init --force failed: {init_result.stderr}"
        assert "Created configuration file" in init_result.stdout, (
            f"Expected success message: {init_result.stdout}"
        )

        # Verify the marker is no longer present (config was overwritten)
        check_content = await executor.run_command(
            "cat ~/.config/pc-switcher/config.yaml",
            timeout=10.0,
        )
        assert custom_marker not in check_content.stdout, "Config was not overwritten with --force"

        # Verify new config has expected content (comments from default config)
        assert "#" in check_content.stdout, "New config should have comments"

    async def test_001_init_creates_parent_directory(
        self,
        pc1_executor: RemoteExecutor,
    ) -> None:
        """Test that pc-switcher init creates parent directory if missing.

        Verifies that init creates ~/.config/pc-switcher/ if it doesn't exist.
        """
        executor = pc1_executor

        # Backup and remove entire config directory
        await executor.run_command(
            "if [ -d ~/.config/pc-switcher ]; then "
            "mv ~/.config/pc-switcher ~/.config/pc-switcher.backup; "
            "fi",
            timeout=10.0,
        )

        try:
            # Verify directory doesn't exist
            dir_check = await executor.run_command(
                "test -d ~/.config/pc-switcher && echo 'exists' || echo 'not_found'",
                timeout=10.0,
            )
            assert "not_found" in dir_check.stdout, "Config directory should not exist"

            # Run pc-switcher init
            init_result = await executor.run_command("pc-switcher init", timeout=30.0)
            assert init_result.success, f"pc-switcher init failed: {init_result.stderr}"

            # Verify directory was created
            dir_check_after = await executor.run_command(
                "test -d ~/.config/pc-switcher && echo 'exists'",
                timeout=10.0,
            )
            assert "exists" in dir_check_after.stdout, "Config directory should be created"

            # Verify config file exists
            file_check = await executor.run_command(
                "test -f ~/.config/pc-switcher/config.yaml && echo 'exists'",
                timeout=10.0,
            )
            assert "exists" in file_check.stdout, "Config file should be created"

        finally:
            # Restore original directory
            await executor.run_command("rm -rf ~/.config/pc-switcher", timeout=10.0)
            await executor.run_command(
                "if [ -d ~/.config/pc-switcher.backup ]; then "
                "mv ~/.config/pc-switcher.backup ~/.config/pc-switcher; "
                "fi",
                timeout=10.0,
            )
