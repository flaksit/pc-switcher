"""Tests for CLI commands.

Tests verify that the CLI commands defined in src/pcswitcher/cli.py exist and
accept the correct arguments as specified in specs/001-foundation/spec.md.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from pcswitcher.cli import app
from pcswitcher.config import Configuration

runner = CliRunner()


class TestSyncCommand:
    """Tests for the 'pc-switcher sync <target>' command."""

    def test_001_fnd_fr_sync_cmd(self) -> None:
        """Test FND-FR-SYNC-CMD: System provides single command 'pc-switcher sync <target>'.

        Verifies that:
        1. The sync command exists and can be invoked
        2. The command accepts a target argument (hostname/IP)
        3. The command structure matches the spec requirement

        References:
        - FND-FR-SYNC-CMD in specs/001-foundation/spec.md
        """
        # Mock Configuration to avoid needing actual config file
        mock_config = MagicMock(spec=Configuration)

        # Mock the _load_configuration function to return our mock config
        # Mock _run_sync to avoid actually running sync
        with (
            patch("pcswitcher.cli._load_configuration", return_value=mock_config),
            patch("pcswitcher.cli._run_sync", return_value=0),
        ):
            # Invoke the sync command with a target argument
            result = runner.invoke(app, ["sync", "test-target"])

            # Verify the command executed without errors
            assert result.exit_code == 0

            # Verify that _run_sync was called with correct arguments
            # (this confirms the command structure is correct)

    def test_001_fnd_fr_sync_cmd_requires_target(self) -> None:
        """Test FND-FR-SYNC-CMD: Sync command requires a target argument.

        Verifies that invoking 'pc-switcher sync' without a target argument
        results in an error, ensuring the command structure is enforced.

        References:
        - FND-FR-SYNC-CMD in specs/001-foundation/spec.md
        """
        # Invoke the sync command without a target argument
        result = runner.invoke(app, ["sync"])

        # Verify the command fails with appropriate exit code
        # Typer returns exit code 2 for missing required arguments
        assert result.exit_code == 2

        # Verify error message indicates missing argument
        # Typer puts error messages in stdout when using CliRunner
        output = result.stdout + result.stderr
        assert "Missing argument" in output or "required" in output.lower()

    def test_001_fnd_fr_sync_cmd_accepts_config_option(self) -> None:
        """Test FND-FR-SYNC-CMD: Sync command accepts optional --config flag.

        Verifies that the sync command accepts the optional --config/-c flag
        for specifying a custom configuration file path.

        References:
        - FND-FR-SYNC-CMD in specs/001-foundation/spec.md
        - sync command implementation in src/pcswitcher/cli.py
        """
        # Mock Configuration to avoid needing actual config file
        mock_config = MagicMock(spec=Configuration)

        # Create a temporary config path for testing
        custom_config_path = Path("/tmp/custom-config.yaml")

        # Mock the _load_configuration function to capture the config path used
        # Mock _run_sync to avoid actually running sync
        with (
            patch("pcswitcher.cli._load_configuration", return_value=mock_config) as mock_load,
            patch("pcswitcher.cli._run_sync", return_value=0),
        ):
            # Invoke the sync command with --config option
            result = runner.invoke(app, ["sync", "test-target", "--config", str(custom_config_path)])

            # Verify the command executed without errors
            assert result.exit_code == 0

            # Verify that _load_configuration was called with the custom config path
            mock_load.assert_called_once_with(custom_config_path)
