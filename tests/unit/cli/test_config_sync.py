"""Unit tests for the config_sync module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config_sync import (
    ConfigSyncAction,
    _copy_config_to_target,
    _generate_diff,
    _get_target_config,
    _prompt_config_diff,
    _prompt_new_config,
    sync_config_to_target,
)
from pcswitcher.models import CommandResult


class TestConfigSyncAction:
    """Tests for ConfigSyncAction enum."""

    def test_enum_values(self) -> None:
        """ConfigSyncAction should have expected values."""
        assert ConfigSyncAction.ACCEPT_SOURCE.value == "accept_source"
        assert ConfigSyncAction.KEEP_TARGET.value == "keep_target"
        assert ConfigSyncAction.ABORT.value == "abort"


class TestGetTargetConfig:
    """Tests for _get_target_config function."""

    async def test_returns_content_when_file_exists(self, mock_remote_executor: MagicMock) -> None:
        """Should return config content when file exists on target."""
        config_content = "log_level: INFO\nsync_jobs:\n  dummy: true"
        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout=config_content, stderr="")
        )

        result = await _get_target_config(mock_remote_executor)

        assert result == config_content
        mock_remote_executor.run_command.assert_called_once_with("cat ~/.config/pc-switcher/config.yaml 2>/dev/null")

    async def test_returns_none_when_file_missing(self, mock_remote_executor: MagicMock) -> None:
        """Should return None when config file doesn't exist on target."""
        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=1, stdout="", stderr="No such file")
        )

        result = await _get_target_config(mock_remote_executor)

        assert result is None

    async def test_returns_none_when_file_empty(self, mock_remote_executor: MagicMock) -> None:
        """Should return None when config file is empty."""
        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="   \n  ", stderr="")
        )

        result = await _get_target_config(mock_remote_executor)

        assert result is None


class TestGenerateDiff:
    """Tests for _generate_diff function."""

    def test_generates_unified_diff(self) -> None:
        """Should generate unified diff format."""
        source = "line1\nline2\nline3"
        target = "line1\nmodified\nline3"

        diff = _generate_diff(source, target)

        assert "--- target config" in diff
        assert "+++ source config" in diff
        assert "-modified" in diff
        assert "+line2" in diff

    def test_empty_diff_for_identical_content(self) -> None:
        """Should generate minimal diff for identical content."""
        content = "same\ncontent"

        diff = _generate_diff(content, content)

        # Identical content produces empty diff (no changes)
        assert diff == ""

    def test_handles_multiline_changes(self) -> None:
        """Should handle multiple line changes."""
        source = "a\nb\nc\nd"
        target = "a\nx\ny\nd"

        diff = _generate_diff(source, target)

        assert "-x" in diff
        assert "-y" in diff
        assert "+b" in diff
        assert "+c" in diff


class TestPromptNewConfig:
    """Tests for _prompt_new_config function."""

    def test_returns_true_on_yes(self) -> None:
        """Should return True when user enters 'y'."""
        console = MagicMock()
        with patch("pcswitcher.config_sync.Prompt.ask", return_value="y"):
            result = _prompt_new_config(console, "config: value")

        assert result is True

    def test_returns_false_on_no(self) -> None:
        """Should return False when user enters 'n'."""
        console = MagicMock()
        with patch("pcswitcher.config_sync.Prompt.ask", return_value="n"):
            result = _prompt_new_config(console, "config: value")

        assert result is False

    def test_displays_config_content(self) -> None:
        """Should display the config content to the user."""
        console = MagicMock()
        config_content = "log_level: DEBUG"

        with patch("pcswitcher.config_sync.Prompt.ask", return_value="n"):
            _prompt_new_config(console, config_content)

        # Verify console.print was called multiple times
        assert console.print.call_count >= 3


class TestPromptConfigDiff:
    """Tests for _prompt_config_diff function."""

    def test_returns_accept_source_on_a(self) -> None:
        """Should return ACCEPT_SOURCE when user enters 'a'."""
        console = MagicMock()
        with patch("pcswitcher.config_sync.Prompt.ask", return_value="a"):
            result = _prompt_config_diff(console, "source", "target", "diff")

        assert result == ConfigSyncAction.ACCEPT_SOURCE

    def test_returns_keep_target_on_k(self) -> None:
        """Should return KEEP_TARGET when user enters 'k'."""
        console = MagicMock()
        with patch("pcswitcher.config_sync.Prompt.ask", return_value="k"):
            result = _prompt_config_diff(console, "source", "target", "diff")

        assert result == ConfigSyncAction.KEEP_TARGET

    def test_returns_abort_on_x(self) -> None:
        """Should return ABORT when user enters 'x'."""
        console = MagicMock()
        with patch("pcswitcher.config_sync.Prompt.ask", return_value="x"):
            result = _prompt_config_diff(console, "source", "target", "diff")

        assert result == ConfigSyncAction.ABORT

    def test_displays_diff_content(self) -> None:
        """Should display the diff to the user."""
        console = MagicMock()

        with patch("pcswitcher.config_sync.Prompt.ask", return_value="x"):
            _prompt_config_diff(console, "source", "target", "--- diff ---")

        # Verify console.print was called with options
        assert console.print.call_count >= 5  # Panel, diff, options


class TestCopyConfigToTarget:
    """Tests for _copy_config_to_target function."""

    async def test_creates_directory_and_copies_file(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """Should create directory and copy file via SFTP."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("test: config")

        mock_remote_executor.run_command = AsyncMock(
            side_effect=[
                CommandResult(exit_code=0, stdout="", stderr=""),  # mkdir
                CommandResult(exit_code=0, stdout="/home/user\n", stderr=""),  # echo $HOME
            ]
        )
        mock_remote_executor.send_file = AsyncMock()

        await _copy_config_to_target(mock_remote_executor, config_file)

        # Verify mkdir was called
        mkdir_call = mock_remote_executor.run_command.call_args_list[0]
        assert "mkdir -p ~/.config/pc-switcher" in mkdir_call[0][0]

        # Verify send_file was called with correct paths
        mock_remote_executor.send_file.assert_called_once_with(
            config_file, "/home/user/.config/pc-switcher/config.yaml"
        )

    async def test_raises_on_mkdir_failure(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """Should raise RuntimeError if mkdir fails."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("test: config")

        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=1, stdout="", stderr="Permission denied")
        )

        with pytest.raises(RuntimeError, match="Failed to create config directory"):
            await _copy_config_to_target(mock_remote_executor, config_file)

    async def test_raises_on_home_dir_failure(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """Should raise RuntimeError if getting home directory fails."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("test: config")

        mock_remote_executor.run_command = AsyncMock(
            side_effect=[
                CommandResult(exit_code=0, stdout="", stderr=""),  # mkdir succeeds
                CommandResult(exit_code=1, stdout="", stderr="error"),  # echo $HOME fails
            ]
        )

        with pytest.raises(RuntimeError, match="Failed to get home directory"):
            await _copy_config_to_target(mock_remote_executor, config_file)


class TestSyncConfigToTarget:
    """Tests for sync_config_to_target function."""

    async def test_raises_if_source_config_missing(self, mock_remote_executor: MagicMock) -> None:
        """Should raise RuntimeError if source config doesn't exist."""
        console = MagicMock()
        non_existent_path = Path("/nonexistent/config.yaml")

        with pytest.raises(RuntimeError, match="Source config not found"):
            await sync_config_to_target(mock_remote_executor, non_existent_path, None, console)

    async def test_scenario_configs_match_skips_silently(
        self, mock_remote_executor: MagicMock, tmp_path: Path
    ) -> None:
        """Scenario 3: When configs match, should skip without prompting."""
        config_content = "log_level: INFO\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)

        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout=config_content, stderr="")
        )

        console = MagicMock()

        result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True
        # Verify "skipping" message was printed
        console.print.assert_called()
        call_args = [str(call) for call in console.print.call_args_list]
        assert any("skipping" in str(arg).lower() for arg in call_args)

    async def test_scenario_no_target_config_user_accepts(
        self, mock_remote_executor: MagicMock, tmp_path: Path
    ) -> None:
        """Scenario 1: No config on target, user accepts."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO")

        # First call: get target config (none exists)
        # Second call: mkdir
        # Third call: echo $HOME
        mock_remote_executor.run_command = AsyncMock(
            side_effect=[
                CommandResult(exit_code=1, stdout="", stderr=""),  # cat fails
                CommandResult(exit_code=0, stdout="", stderr=""),  # mkdir
                CommandResult(exit_code=0, stdout="/home/user\n", stderr=""),  # echo $HOME
            ]
        )
        mock_remote_executor.send_file = AsyncMock()

        console = MagicMock()

        with patch("pcswitcher.config_sync._prompt_new_config", return_value=True):
            result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True
        mock_remote_executor.send_file.assert_called_once()

    async def test_scenario_no_target_config_user_declines(
        self, mock_remote_executor: MagicMock, tmp_path: Path
    ) -> None:
        """Scenario 1: No config on target, user declines."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO")

        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=1, stdout="", stderr="")  # cat fails
        )

        console = MagicMock()

        with patch("pcswitcher.config_sync._prompt_new_config", return_value=False):
            result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is False

    async def test_scenario_configs_differ_user_accepts_source(
        self, mock_remote_executor: MagicMock, tmp_path: Path
    ) -> None:
        """Scenario 2: Configs differ, user chooses to accept source."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: DEBUG")

        mock_remote_executor.run_command = AsyncMock(
            side_effect=[
                CommandResult(exit_code=0, stdout="log_level: INFO", stderr=""),  # cat
                CommandResult(exit_code=0, stdout="", stderr=""),  # mkdir
                CommandResult(exit_code=0, stdout="/home/user\n", stderr=""),  # echo $HOME
            ]
        )
        mock_remote_executor.send_file = AsyncMock()

        console = MagicMock()

        with patch(
            "pcswitcher.config_sync._prompt_config_diff",
            return_value=ConfigSyncAction.ACCEPT_SOURCE,
        ):
            result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True
        mock_remote_executor.send_file.assert_called_once()

    async def test_scenario_configs_differ_user_keeps_target(
        self, mock_remote_executor: MagicMock, tmp_path: Path
    ) -> None:
        """Scenario 2: Configs differ, user chooses to keep target."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: DEBUG")

        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="log_level: INFO", stderr="")
        )

        console = MagicMock()

        with patch(
            "pcswitcher.config_sync._prompt_config_diff",
            return_value=ConfigSyncAction.KEEP_TARGET,
        ):
            result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True

    async def test_scenario_configs_differ_user_aborts(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """Scenario 2: Configs differ, user chooses to abort."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: DEBUG")

        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="log_level: INFO", stderr="")
        )

        console = MagicMock()

        with patch("pcswitcher.config_sync._prompt_config_diff", return_value=ConfigSyncAction.ABORT):
            result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is False

    async def test_ui_paused_and_resumed(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """Should pause UI during prompts and resume after."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO\n")

        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="log_level: INFO\n", stderr="")
        )

        console = MagicMock()
        ui = MagicMock()

        await sync_config_to_target(mock_remote_executor, config_file, ui, console)

        ui.stop.assert_called_once()
        ui.start.assert_called_once()

    async def test_ui_resumed_even_on_exception(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """Should resume UI even if an exception occurs."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO")

        mock_remote_executor.run_command = AsyncMock(
            side_effect=[
                CommandResult(exit_code=1, stdout="", stderr=""),  # cat fails (no config)
            ]
        )

        console = MagicMock()
        ui = MagicMock()

        # Make _prompt_new_config raise an exception
        with (
            patch("pcswitcher.config_sync._prompt_new_config", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            await sync_config_to_target(mock_remote_executor, config_file, ui, console)

        # UI should still be resumed in finally block
        ui.stop.assert_called_once()
        ui.start.assert_called_once()

    async def test_whitespace_differences_ignored(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """Should treat configs as equal if only whitespace differs."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: INFO\n\n")

        # Target has same content but with different trailing whitespace
        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="log_level: INFO  \n", stderr="")
        )

        console = MagicMock()

        result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True
        # Should skip without prompting
        call_args = [str(call) for call in console.print.call_args_list]
        assert any("skipping" in str(arg).lower() for arg in call_args)

    async def test_001_fr007a_config_sync_prompt_if_missing(
        self, mock_remote_executor: MagicMock, tmp_path: Path
    ) -> None:
        """FR-007a: Sync config after install, prompt if missing.

        Verifies that when no config exists on target, user is prompted
        to accept the source config for initial setup.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: DEBUG\nsync_jobs:\n  dummy_success: true")

        # No config on target
        mock_remote_executor.run_command = AsyncMock(
            side_effect=[
                CommandResult(exit_code=1, stdout="", stderr="No such file"),  # cat fails
                CommandResult(exit_code=0, stdout="", stderr=""),  # mkdir
                CommandResult(exit_code=0, stdout="/home/user\n", stderr=""),  # echo $HOME
            ]
        )
        mock_remote_executor.send_file = AsyncMock()

        console = MagicMock()

        # User accepts config
        with patch("pcswitcher.config_sync._prompt_new_config", return_value=True):
            result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True
        mock_remote_executor.send_file.assert_called_once()

    async def test_001_fr007b_config_diff_and_prompt(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """FR-007b: Show diff and prompt if configs differ.

        Verifies that when target config differs from source, a diff
        is shown and user is prompted to choose an action.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text("log_level: DEBUG\n")

        # Target has different config
        mock_remote_executor.run_command = AsyncMock(
            side_effect=[
                CommandResult(exit_code=0, stdout="log_level: INFO\n", stderr=""),  # cat
                CommandResult(exit_code=0, stdout="", stderr=""),  # mkdir
                CommandResult(exit_code=0, stdout="/home/user\n", stderr=""),  # echo $HOME
            ]
        )
        mock_remote_executor.send_file = AsyncMock()

        console = MagicMock()

        # User chooses to accept source
        with patch(
            "pcswitcher.config_sync._prompt_config_diff",
            return_value=ConfigSyncAction.ACCEPT_SOURCE,
        ):
            result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True
        mock_remote_executor.send_file.assert_called_once()

    async def test_001_fr007c_skip_if_configs_match(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """FR-007c: Skip config sync if configs match.

        Verifies that when source and target configs are identical,
        config sync is skipped without prompting.
        """
        config_content = "log_level: INFO\nsync_jobs:\n  dummy_success: true\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)

        # Target has identical config
        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout=config_content, stderr="")
        )

        console = MagicMock()

        result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True
        # Verify skipping message was printed
        console.print.assert_called()
        call_args = [str(call) for call in console.print.call_args_list]
        assert any("skipping" in str(arg).lower() for arg in call_args)
        # Verify send_file was NOT called (config not copied)
        mock_remote_executor.send_file.assert_not_called()

    async def test_001_us2_as7_skip_when_configs_match(self, mock_remote_executor: MagicMock, tmp_path: Path) -> None:
        """US2-AS7: Configs match, skip config sync.

        User Story 2, Acceptance Scenario 7: When target already has
        the same config as source, sync proceeds without config prompts.
        """
        config_content = "log_level: INFO\n"
        config_file = tmp_path / "config.yaml"
        config_file.write_text(config_content)

        # Target has matching config
        mock_remote_executor.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout=config_content, stderr="")
        )

        console = MagicMock()

        result = await sync_config_to_target(mock_remote_executor, config_file, None, console)

        assert result is True
        # Should not copy config when it matches
        mock_remote_executor.send_file.assert_not_called()
