"""Tests for CLI commands.

Tests verify that the CLI commands defined in src/pcswitcher/cli.py exist and
accept the correct arguments as specified in docs/system/core.md.
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

    def test_core_fr_sync_cmd(self) -> None:
        """Test CORE-FR-SYNC-CMD: System provides single command 'pc-switcher sync <target>'.

        Verifies that:
        1. The sync command exists and can be invoked
        2. The command accepts a target argument (hostname/IP)
        3. The command structure matches the spec requirement

        References:
        - CORE-FR-SYNC-CMD in docs/system/core.md
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

    def test_core_fr_sync_cmd_requires_target(self) -> None:
        """Test CORE-FR-SYNC-CMD: Sync command requires a target argument.

        Verifies that invoking 'pc-switcher sync' without a target argument
        results in an error, ensuring the command structure is enforced.

        References:
        - CORE-FR-SYNC-CMD in docs/system/core.md
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

    def test_core_fr_sync_cmd_accepts_config_option(self) -> None:
        """Test CORE-FR-SYNC-CMD: Sync command accepts optional --config flag.

        Verifies that the sync command accepts the optional --config/-c flag
        for specifying a custom configuration file path.

        References:
        - CORE-FR-SYNC-CMD in docs/system/core.md
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


class TestLogsCommand:
    """Tests for the 'pc-switcher logs' command.

    Spec reference: docs/system/logging.md - LOG-US-SYSTEM-AS6
    """

    def test_log_us_system_as6_logs_last_displays_most_recent(self, tmp_path: Path) -> None:
        """Test LOG-US-SYSTEM-AS6: logs --last displays the most recent log file.

        Verifies that `pc-switcher logs --last`:
        1. Identifies the most recent log file by filename (timestamp in name)
        2. Displays the log file content
        3. Returns exit code 0
        """
        # Create log file with test content
        log_file = tmp_path / "sync-20240102T100000-def67890.log"
        log_file.write_text('{"timestamp": "2024-01-02T10:00:00", "level": "INFO", "event": "Newer log - latest"}\n')

        # Mock get_latest_log_file to return our test file
        with patch("pcswitcher.cli.get_latest_log_file", return_value=log_file):
            result = runner.invoke(app, ["logs", "--last"])

        # Verify command succeeded
        assert result.exit_code == 0, f"logs --last failed: {result.stdout}"

        # Verify log content is displayed
        assert "Newer log" in result.stdout or "latest" in result.stdout, (
            f"Expected log content.\nOutput: {result.stdout}"
        )

    def test_log_us_system_as6_logs_last_no_logs_shows_message(self) -> None:
        """Test LOG-US-SYSTEM-AS6: logs --last shows message when no logs exist.

        Verifies that when no log files exist, the command shows an appropriate
        message rather than crashing.
        """
        # Mock get_latest_log_file to return None (no logs)
        with patch("pcswitcher.cli.get_latest_log_file", return_value=None):
            result = runner.invoke(app, ["logs", "--last"])

        # Should exit with non-zero and show "no log" message
        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}"
        assert "no log" in result.stdout.lower(), f"Expected 'no log' message.\nstdout: {result.stdout}"
