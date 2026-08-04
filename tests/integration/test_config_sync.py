"""Integration tests for config sync functionality."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pcswitcher.config_sync import (
    ConfigSyncAction,
    _copy_config_to_target,
    _get_target_config,
    sync_config_to_target,
)
from pcswitcher.executor import RemoteExecutor
from tests.integration.conftest import write_pcswitcher_config

# The two machines are named by hostname in everything config sync prints
# (`PKG-FR-NAME-THE-MACHINES`), so every call has to supply them.
ATLAS = "Atlas"
NOMAD = "Nomad"

pytestmark = pytest.mark.smoke


class TestConfigSyncIntegration:
    """Integration tests for config sync with real VMs."""

    async def test_get_target_config_returns_none_when_missing(self, pc1_executor: RemoteExecutor) -> None:
        """Should return None when config file doesn't exist on target."""
        # Ensure config doesn't exist
        await pc1_executor.run_command("rm --force ~/.config/pc-switcher/config.yaml")

        result = await _get_target_config(pc1_executor)

        assert result is None

    async def test_get_target_config_returns_content(self, pc1_executor: RemoteExecutor) -> None:
        """Should return config content when file exists."""
        config_content = "# Test config\nlog_level: DEBUG\n"

        # Create config file on target
        await write_pcswitcher_config(pc1_executor, config_content)

        try:
            result = await _get_target_config(pc1_executor)

            assert result is not None
            assert "log_level: DEBUG" in result
        finally:
            # Cleanup
            await pc1_executor.run_command("rm --force ~/.config/pc-switcher/config.yaml")

    async def test_copy_config_to_target_creates_file(self, pc1_executor: RemoteExecutor) -> None:
        """Should create config file on target via SFTP."""
        # Ensure clean state
        await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

        # Create local config file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("# Integration test config\nlog_level: INFO\n")
            local_path = Path(f.name)

        try:
            await _copy_config_to_target(pc1_executor, local_path, NOMAD)

            # Verify file was created
            result = await pc1_executor.run_command("cat ~/.config/pc-switcher/config.yaml")
            assert result.success
            assert "log_level: INFO" in result.stdout
        finally:
            # Cleanup
            local_path.unlink()
            await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

    async def test_copy_config_to_target_creates_directory(self, pc1_executor: RemoteExecutor) -> None:
        """Should create the .config/pc-switcher directory if missing."""
        # Ensure directory doesn't exist
        await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as f:
            f.write("test: value\n")
            f.flush()
            local_path = Path(f.name)

            try:
                await _copy_config_to_target(pc1_executor, local_path, NOMAD)

                # Verify directory was created
                result = await pc1_executor.run_command("test -d ~/.config/pc-switcher")
                assert result.success
            finally:
                await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

    async def test_sync_config_when_configs_match(self, pc1_executor: RemoteExecutor) -> None:
        """Should skip silently when configs match exactly."""
        config_content = "log_level: INFO\n"

        # Create matching configs on source and target
        await write_pcswitcher_config(pc1_executor, config_content)

        console = MagicMock()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as f:
            f.write(config_content)
            f.flush()
            local_path = Path(f.name)

            try:
                result = await sync_config_to_target(
                    pc1_executor, local_path, None, console, source_hostname=ATLAS, target_hostname=NOMAD
                )

                assert result is True
                # Verify "skipping" message was printed
                console.print.assert_called()
                call_args_str = str(console.print.call_args_list)
                assert "skipping" in call_args_str.lower()
            finally:
                await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

    async def test_sync_config_no_target_config_accepts(self, pc1_executor: RemoteExecutor) -> None:
        """Should copy config when target has none and user accepts."""
        # Ensure no config on target
        await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml") as f:
            f.write("log_level: DEBUG\nsync_jobs:\n  dummy: true\n")
            f.flush()
            local_path = Path(f.name)

            console = MagicMock()

            try:
                with patch("pcswitcher.config_sync._prompt_new_config", return_value=True):
                    result = await sync_config_to_target(
                        pc1_executor, local_path, None, console, source_hostname=ATLAS, target_hostname=NOMAD
                    )

                assert result is True

                # Verify config was copied
                read_result = await pc1_executor.run_command("cat ~/.config/pc-switcher/config.yaml")
                assert read_result.success
                assert "log_level: DEBUG" in read_result.stdout
            finally:
                await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

    async def test_sync_config_no_target_config_declines(self, pc1_executor: RemoteExecutor) -> None:
        """Should abort when target has no config and user declines."""
        # Ensure no config on target
        await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("log_level: DEBUG\n")
            local_path = Path(f.name)

        console = MagicMock()

        try:
            with patch("pcswitcher.config_sync._prompt_new_config", return_value=False):
                result = await sync_config_to_target(
                    pc1_executor, local_path, None, console, source_hostname=ATLAS, target_hostname=NOMAD
                )

            assert result is False

            # Verify no config was created
            read_result = await pc1_executor.run_command("test -f ~/.config/pc-switcher/config.yaml")
            assert not read_result.success
        finally:
            local_path.unlink()

    async def test_sync_config_differs_accepts_source(self, pc1_executor: RemoteExecutor) -> None:
        """Should overwrite target config when user accepts source."""
        # Create different config on target
        await write_pcswitcher_config(pc1_executor, "log_level: WARNING\n")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("log_level: DEBUG\n")
            local_path = Path(f.name)

        console = MagicMock()

        try:
            with patch(
                "pcswitcher.config_sync._prompt_config_diff",
                return_value=ConfigSyncAction.ACCEPT_SOURCE,
            ):
                result = await sync_config_to_target(
                    pc1_executor, local_path, None, console, source_hostname=ATLAS, target_hostname=NOMAD
                )

            assert result is True

            # Verify config was overwritten
            read_result = await pc1_executor.run_command("cat ~/.config/pc-switcher/config.yaml")
            assert "log_level: DEBUG" in read_result.stdout
        finally:
            local_path.unlink()
            await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

    async def test_sync_config_differs_keeps_target(self, pc1_executor: RemoteExecutor) -> None:
        """Should keep target config when user chooses to keep."""
        # Create different config on target
        await write_pcswitcher_config(pc1_executor, "log_level: WARNING\n")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("log_level: DEBUG\n")
            local_path = Path(f.name)

        console = MagicMock()

        try:
            with patch(
                "pcswitcher.config_sync._prompt_config_diff",
                return_value=ConfigSyncAction.KEEP_TARGET,
            ):
                result = await sync_config_to_target(
                    pc1_executor, local_path, None, console, source_hostname=ATLAS, target_hostname=NOMAD
                )

            assert result is True

            # Verify target config was NOT modified
            read_result = await pc1_executor.run_command("cat ~/.config/pc-switcher/config.yaml")
            assert "log_level: WARNING" in read_result.stdout
        finally:
            local_path.unlink()
            await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

    async def test_sync_config_differs_aborts(self, pc1_executor: RemoteExecutor) -> None:
        """Should abort sync when user chooses abort."""
        # Create different config on target
        await write_pcswitcher_config(pc1_executor, "log_level: WARNING\n")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("log_level: DEBUG\n")
            local_path = Path(f.name)

        console = MagicMock()

        try:
            with patch(
                "pcswitcher.config_sync._prompt_config_diff",
                return_value=ConfigSyncAction.ABORT,
            ):
                result = await sync_config_to_target(
                    pc1_executor, local_path, None, console, source_hostname=ATLAS, target_hostname=NOMAD
                )

            assert result is False
        finally:
            local_path.unlink()
            await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

    async def test_ui_lifecycle_during_sync(self, pc1_executor: RemoteExecutor) -> None:
        """Should pause and resume the UI around a config-sync prompt.

        The pause is gated on a prompt actually being shown (a matching config
        needs none), so drive the no-target-config path with the prompt mocked
        and assert the live display is paused then resumed around it.
        """
        config_content = "log_level: INFO\n"

        # Ensure the target has no config so a prompt is required.
        await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(config_content)
            local_path = Path(f.name)

        console = MagicMock()
        ui = MagicMock()

        try:
            with patch("pcswitcher.config_sync._prompt_new_config", return_value=False):
                await sync_config_to_target(
                    pc1_executor, local_path, ui, console, source_hostname=ATLAS, target_hostname=NOMAD
                )

            ui.pause.assert_called_once()
            ui.resume.assert_called_once()
        finally:
            local_path.unlink()
            await pc1_executor.run_command("rm --recursive --force ~/.config/pc-switcher")
