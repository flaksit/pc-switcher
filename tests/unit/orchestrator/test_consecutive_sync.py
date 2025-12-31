"""Unit tests for orchestrator consecutive sync warning feature.

These tests verify that the orchestrator correctly detects and warns about
consecutive syncs from the same source machine without receiving a sync back.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.panel import Panel

from pcswitcher.config import Configuration
from pcswitcher.logger import Logger
from pcswitcher.orchestrator import Orchestrator


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock Configuration for orchestrator initialization."""
    config = MagicMock(spec=Configuration)
    config.log_file_level = MagicMock()
    config.log_cli_level = MagicMock()
    config.sync_jobs = {}
    config.job_configs = {}
    config.btrfs_snapshots = MagicMock()
    config.btrfs_snapshots.subvolumes = ["@", "@home"]
    config.disk = MagicMock()
    config.disk.preflight_minimum = "10%"
    return config


class TestCheckConsecutiveSync:
    """Test the _check_consecutive_sync method."""

    @pytest.mark.asyncio
    async def test_no_history_continues_without_warning(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no sync history exists, sync should continue without warning."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="test-target", config=mock_config)
        # Mock console and UI
        orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]

        result = await orchestrator._check_consecutive_sync()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        # UI should not have been stopped (no warning shown)
        orchestrator._ui.stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_last_role_target_continues_without_warning(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When last role was TARGET, sync should continue without warning."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Create history file showing last role was target
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "target"}')

        orchestrator = Orchestrator(target="test-target", config=mock_config)
        orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]

        result = await orchestrator._check_consecutive_sync()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        orchestrator._ui.stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_last_role_source_shows_warning(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When last role was SOURCE, warning should be shown and user prompted."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Create history file showing last role was source
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "source"}')

        orchestrator = Orchestrator(target="test-target", config=mock_config)
        orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]

        # Mock user input to decline
        with patch("rich.prompt.Prompt.ask", return_value="n"):
            result = await orchestrator._check_consecutive_sync()  # pyright: ignore[reportPrivateUsage]

        assert result is False
        # UI should have been stopped and started for the warning
        orchestrator._ui.stop.assert_called_once()  # pyright: ignore[reportPrivateUsage]
        orchestrator._ui.start.assert_called_once()  # pyright: ignore[reportPrivateUsage]
        # Warning panel should have been printed
        orchestrator._console.print.assert_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_user_accepts_warning_continues(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When user accepts the warning, sync should continue."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "source"}')

        orchestrator = Orchestrator(target="test-target", config=mock_config)
        orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]

        # Mock user input to accept
        with patch("rich.prompt.Prompt.ask", return_value="y"):
            result = await orchestrator._check_consecutive_sync()  # pyright: ignore[reportPrivateUsage]

        assert result is True

    @pytest.mark.asyncio
    async def test_corrupted_history_shows_warning(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When history file is corrupted, warning should be shown (safety-first)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Create corrupted history file
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text("not valid json")

        orchestrator = Orchestrator(target="test-target", config=mock_config)
        orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]

        with patch("rich.prompt.Prompt.ask", return_value="n"):
            result = await orchestrator._check_consecutive_sync()  # pyright: ignore[reportPrivateUsage]

        assert result is False
        orchestrator._ui.stop.assert_called_once()  # pyright: ignore[reportPrivateUsage]


class TestAllowConsecutiveFlag:
    """Test the --allow-consecutive flag behavior."""

    def test_orchestrator_accepts_allow_consecutive_flag(self, mock_config: MagicMock) -> None:
        """Orchestrator should accept allow_consecutive parameter."""
        orchestrator = Orchestrator(
            target="test-target",
            config=mock_config,
            allow_consecutive=True,
        )
        assert orchestrator._allow_consecutive is True  # pyright: ignore[reportPrivateUsage]

    def test_orchestrator_defaults_allow_consecutive_to_false(self, mock_config: MagicMock) -> None:
        """Orchestrator should default allow_consecutive to False."""
        orchestrator = Orchestrator(target="test-target", config=mock_config)
        assert orchestrator._allow_consecutive is False  # pyright: ignore[reportPrivateUsage]


class TestUpdateSyncHistory:
    """Test the _update_sync_history method."""

    @pytest.mark.asyncio
    async def test_updates_local_history_to_source(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After sync, local history should be updated to SOURCE."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="test-target", config=mock_config)
        orchestrator._remote_executor = None  # pyright: ignore[reportPrivateUsage]

        # Create mock event_bus and logger
        orchestrator._event_bus = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._logger = Logger(orchestrator._event_bus, "test")  # pyright: ignore[reportPrivateUsage]

        await orchestrator._update_sync_history()  # pyright: ignore[reportPrivateUsage]

        # Verify local history was updated
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        assert history_path.exists()
        content = history_path.read_text()
        assert '"last_role": "source"' in content

    @pytest.mark.asyncio
    async def test_updates_remote_history_to_target(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After sync, remote history should be updated to TARGET via SSH."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="test-target", config=mock_config)

        # Mock remote executor
        mock_result = MagicMock()
        mock_result.success = True
        mock_executor = AsyncMock()
        mock_executor.run_command.return_value = mock_result
        orchestrator._remote_executor = mock_executor  # pyright: ignore[reportPrivateUsage]

        # Create mock event_bus and logger
        orchestrator._event_bus = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._logger = Logger(orchestrator._event_bus, "test")  # pyright: ignore[reportPrivateUsage]

        await orchestrator._update_sync_history()  # pyright: ignore[reportPrivateUsage]

        # Verify remote command was called
        mock_executor.run_command.assert_called_once()
        cmd = mock_executor.run_command.call_args[0][0]
        assert "mkdir -p ~/.local/share/pc-switcher" in cmd
        assert '"last_role": "target"' in cmd

    @pytest.mark.asyncio
    async def test_remote_history_failure_raises_error(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remote history update failure should raise RuntimeError."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="test-target", config=mock_config)

        # Mock remote executor to fail
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.stderr = "Permission denied"
        mock_executor = AsyncMock()
        mock_executor.run_command.return_value = mock_result
        orchestrator._remote_executor = mock_executor  # pyright: ignore[reportPrivateUsage]

        # Create mock event_bus and logger
        mock_event_bus = MagicMock()
        orchestrator._event_bus = mock_event_bus  # pyright: ignore[reportPrivateUsage]
        orchestrator._logger = Logger(mock_event_bus, "test")  # pyright: ignore[reportPrivateUsage]

        # Should raise RuntimeError
        with pytest.raises(RuntimeError, match="Failed to update sync history on target"):
            await orchestrator._update_sync_history()  # pyright: ignore[reportPrivateUsage]


class TestWarningMessageContent:
    """Test the content of the consecutive sync warning message."""

    @pytest.mark.asyncio
    async def test_warning_message_includes_workflow_explanation(
        self, mock_config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Warning message should explain the normal workflow."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text('{"last_role": "source"}')

        orchestrator = Orchestrator(target="test-target", config=mock_config)
        mock_console = MagicMock()
        orchestrator._console = mock_console  # pyright: ignore[reportPrivateUsage]
        orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]

        with patch("rich.prompt.Prompt.ask", return_value="n"):
            await orchestrator._check_consecutive_sync()  # pyright: ignore[reportPrivateUsage]

        # Check that the warning message was printed
        # The Panel is passed to console.print()
        assert mock_console.print.call_count >= 1

        # Find the Panel in the call args
        panel_found = False
        for call in mock_console.print.call_args_list:
            if call.args and isinstance(call.args[0], Panel):
                panel = call.args[0]
                # The renderable inside the Panel contains the warning text
                renderable_str = str(panel.renderable)
                if "without receiving a sync back first" in renderable_str:
                    panel_found = True
                    break

        assert panel_found, "Warning panel with expected message not found"
