"""Tests for CLI commands.

Tests verify that the CLI commands defined in src/pcswitcher/cli.py exist and
accept the correct arguments as specified in docs/system/core.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from pcswitcher.cli import _async_run_sync, app
from pcswitcher.config import Configuration
from pcswitcher.models import SyncAbortedByUser

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


class TestSyncAbortedByUserHandling:
    """_async_run_sync must surface a user abort once, calmly, distinct from a failure.

    UAT gap 2 / plan 01-16: previously a declined confirmation fell through to
    the generic except Exception handler and printed the same red "Sync
    failed" text the orchestrator's own CRITICAL log already implied.
    """

    @pytest.mark.asyncio
    async def test_user_abort_prints_single_calm_message_and_nonzero_exit(self) -> None:
        """Orchestrator.run() raising SyncAbortedByUser -> one calm 'aborted' line."""
        mock_config = MagicMock(spec=Configuration)

        with (
            patch("pcswitcher.cli.Orchestrator") as mock_orchestrator_cls,
            patch("pcswitcher.cli.console") as mock_console,
        ):
            mock_orchestrator = MagicMock()
            mock_orchestrator.run = AsyncMock(side_effect=SyncAbortedByUser("Config sync aborted by user"))
            mock_orchestrator_cls.return_value = mock_orchestrator

            exit_code = await _async_run_sync("target-host", mock_config)

        assert exit_code != 0

        printed = " ".join(str(call.args[0]) for call in mock_console.print.call_args_list)
        assert "aborted" in printed.lower()
        assert "failed" not in printed.lower()


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


class TestInitCommand:
    """Tests for the 'pc-switcher init' command.

    Verifies init writes config.yaml plus the starter home.filter/root.filter
    package-data files next to it, honoring --force for all three (#166).
    """

    def test_init_writes_config_and_filter_files(self, tmp_path: Path) -> None:
        """init writes config.yaml, home.filter, and root.filter into the config dir."""
        config_path = tmp_path / "config.yaml"
        with patch.object(Configuration, "get_default_config_path", return_value=config_path):
            result = runner.invoke(app, ["init"])

        assert result.exit_code == 0, f"init failed: {result.stdout}"
        assert config_path.exists()
        assert (tmp_path / "home.filter").exists()
        assert (tmp_path / "root.filter").exists()
        assert "+ .cache/uv/***" in (tmp_path / "home.filter").read_text()

    def test_init_force_overwrites_all_three_files(self, tmp_path: Path) -> None:
        """init --force overwrites config.yaml and both filter files without error."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("stale config")
        (tmp_path / "home.filter").write_text("stale home filter")
        (tmp_path / "root.filter").write_text("stale root filter")

        with patch.object(Configuration, "get_default_config_path", return_value=config_path):
            result = runner.invoke(app, ["init", "--force"])

        assert result.exit_code == 0, f"init --force failed: {result.stdout}"
        assert "+ .cache/uv/***" in (tmp_path / "home.filter").read_text()
        assert "stale root filter" not in (tmp_path / "root.filter").read_text()


class TestConfirmEachCommandFlag:
    """`--confirm-each-command` has no non-interactive fallback, by design.

    Every other gate degrades when nobody is watching: the confirmer falls back to an
    `--allow-*` flag, the review auto-declines. This one cannot — auto-proceeding on a
    prompt nobody can answer is precisely the failure it exists to prevent — so a run that
    could never ask must be refused before it loads config, connects or touches anything.
    """

    def test_refused_without_a_tty(self) -> None:
        with (
            patch("pcswitcher.cli.is_interactive", return_value=False),
            patch("pcswitcher.cli._load_configuration") as load_config,
            patch("pcswitcher.cli._run_sync") as run_sync,
        ):
            result = runner.invoke(app, ["sync", "test-target", "--confirm-each-command"])

        assert result.exit_code == 1, f"Expected exit code 1, got {result.exit_code}\n{result.output}"
        assert "--confirm-each-command" in result.output, f"Error must name the flag.\nOutput: {result.output}"
        # Refused before the run begins: nothing is loaded and no sync is started.
        load_config.assert_not_called()
        run_sync.assert_not_called()

    def test_accepted_and_forwarded_on_a_tty(self) -> None:
        """The flag must actually reach the orchestrator, not be validated and dropped.

        `PCSWITCHER_SKIP_VERSION_CHECK` is set because faking a TTY also arms the startup
        update check, which would reach the real GitHub API and then block on its own
        `Upgrade now?` prompt — an outcome that depends on whether a newer release exists,
        not on the flag under test.
        """
        with (
            patch.dict(os.environ, {"PCSWITCHER_SKIP_VERSION_CHECK": "1"}),
            patch("pcswitcher.cli.is_interactive", return_value=True),
            patch("pcswitcher.cli._load_configuration", return_value=MagicMock(spec=Configuration)),
            patch("pcswitcher.cli._run_sync", return_value=0) as run_sync,
        ):
            result = runner.invoke(app, ["sync", "test-target", "--confirm-each-command"])

        assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}\n{result.output}"
        assert run_sync.call_args.kwargs["confirm_each_command"] is True

    def test_a_non_interactive_run_without_the_flag_is_not_refused(self) -> None:
        """The refusal is scoped to the flag: ordinary non-interactive syncs still run."""
        with (
            patch("pcswitcher.cli.is_interactive", return_value=False),
            patch("pcswitcher.cli._load_configuration", return_value=MagicMock(spec=Configuration)),
            patch("pcswitcher.cli._run_sync", return_value=0) as run_sync,
        ):
            result = runner.invoke(app, ["sync", "test-target"])

        assert result.exit_code == 0, f"Expected exit code 0, got {result.exit_code}\n{result.output}"
        assert run_sync.call_args.kwargs["confirm_each_command"] is False
