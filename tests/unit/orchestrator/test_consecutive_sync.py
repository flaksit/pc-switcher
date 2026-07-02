"""Unit tests for the orchestrator out-of-order / target-state topology check.

Covers _check_out_of_order() across all truth-table cases:
- Clean case (target_peer == source, no consecutive push) → silent True
- W3: consecutive push to a clean target → interactive prompt / non-interactive False
- W2: machine-C (target last synced with a different peer) → warns
- No/unreadable target history → first sync, deferred to FolderSyncJob (silent True here)
- --allow-out-of-order → bypass (always True, no reading/prompting)
- dry-run with a warn condition → True without prompting (ADR-014)

Per the ADR-015 refinement, a target with no readable sync history is a first-ever
sync, not an out-of-order sync. The orchestrator no longer warns on it; the
first-sync overwrite confirmation is owned by FolderSyncJob (see
tests/unit/jobs/test_folder_sync.py).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config import Configuration
from pcswitcher.confirmer import TerminalUIConfirmer
from pcswitcher.models import CommandResult
from pcswitcher.orchestrator import Orchestrator


def _mock_isatty(interactive: bool = True) -> MagicMock:
    """Create a mock for sys.stdin that returns `interactive` for isatty()."""
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = interactive
    return mock_stdin


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock Configuration for orchestrator initialization."""
    config = MagicMock(spec=Configuration)
    config.logging = MagicMock()
    config.logging.file = 10  # DEBUG
    config.logging.tui = 20  # INFO
    config.logging.external = 30  # WARNING
    config.sync_jobs = {}
    config.job_configs = {}
    config.btrfs_snapshots = MagicMock()
    config.btrfs_snapshots.subvolumes = ["@", "@home"]
    config.disk = MagicMock()
    config.disk.preflight_minimum = "10%"
    return config


def _make_orchestrator(
    mock_config: MagicMock,
    *,
    target: str = "target-host",
    allow_out_of_order: bool = False,
    dry_run: bool = False,
    remote_stdout: str = "",
    remote_exit_code: int = 0,
) -> Orchestrator:
    """Create an Orchestrator wired for _check_out_of_order tests.

    Sets _console, _ui, _logger, and _remote_executor to mocks.
    The remote executor's run_command returns a CommandResult with
    `remote_stdout` and `remote_exit_code` so callers can inject a
    target sync-history JSON payload.
    """
    orchestrator = Orchestrator(
        target=target,
        config=mock_config,
        allow_out_of_order=allow_out_of_order,
        dry_run=dry_run,
    )
    orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    # Wire the real shared confirmer to the mock console/ui so the interactive path
    # exercises Panel/Prompt and the ui.stop()/start() pause exactly as in production.
    orchestrator._confirmer = TerminalUIConfirmer(  # pyright: ignore[reportPrivateUsage]
        orchestrator._console,  # pyright: ignore[reportPrivateUsage, reportArgumentType]
        orchestrator._ui,  # pyright: ignore[reportPrivateUsage, reportArgumentType]
        logger=orchestrator._logger,  # pyright: ignore[reportPrivateUsage]
    )

    mock_result = CommandResult(exit_code=remote_exit_code, stdout=remote_stdout, stderr="")
    mock_executor = AsyncMock()
    mock_executor.run_command.return_value = mock_result
    orchestrator._remote_executor = mock_executor  # pyright: ignore[reportPrivateUsage]

    return orchestrator


def _history_json(role: str, peer: str) -> str:
    """Produce a sync-history JSON payload."""
    return json.dumps({"last_role": role, "last_peer": peer})


# ---------------------------------------------------------------------------
# Clean case: no prompt, returns True
# ---------------------------------------------------------------------------


class TestCheckOutOfOrderCleanCase:
    """The clean A→B / B→A / A→B pattern must always proceed silently."""

    @pytest.mark.asyncio
    async def test_clean_case_proceeds_silently(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """target_peer == source AND no consecutive push → True, no prompt."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        source_name = "source-host"
        target_name = "target-host"

        # Local history: this machine last synced as SOURCE to target
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("target", source_name))

        # Target history: target last synced with source (clean case)
        orchestrator = _make_orchestrator(
            mock_config,
            target=target_name,
            remote_stdout=_history_json("source", source_name),
        )
        # Override source hostname for deterministic test
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]

        result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._ui).stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# W3: consecutive push warning
# ---------------------------------------------------------------------------


class TestCheckOutOfOrderW3ConsecutivePush:
    """W3: this source pushes to the same target again without a back-sync."""

    @pytest.mark.asyncio
    async def test_consecutive_push_interactive_accepts(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Interactive user answers 'y' → returns True."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        source_name = "source-host"
        target_name = "target-host"

        # Local: this machine was SOURCE to target-host (consecutive push setup)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("source", target_name))

        orchestrator = _make_orchestrator(
            mock_config,
            target=target_name,
            remote_stdout=_history_json("target", source_name),
        )
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]

        with (
            patch("rich.prompt.Prompt.ask", return_value="y"),
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._ui).stop.assert_called_once()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._ui).start.assert_called_once()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_consecutive_push_interactive_declines(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Interactive user answers 'n' → returns False."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        source_name = "source-host"
        target_name = "target-host"

        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("source", target_name))

        orchestrator = _make_orchestrator(
            mock_config,
            target=target_name,
            remote_stdout=_history_json("target", source_name),
        )
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]

        with (
            patch("rich.prompt.Prompt.ask", return_value="n"),
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is False

    @pytest.mark.asyncio
    async def test_consecutive_push_non_interactive_returns_false(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-interactive mode → returns False without prompting."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        source_name = "source-host"
        target_name = "target-host"

        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("source", target_name))

        orchestrator = _make_orchestrator(
            mock_config,
            target=target_name,
            remote_stdout=_history_json("target", source_name),
        )
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is False
        # UI must NOT be stopped in non-interactive path
        cast(MagicMock, orchestrator._ui).stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]
        # Warning message must be printed
        cast(MagicMock, orchestrator._console).print.assert_called()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# W2: machine-C warning
# ---------------------------------------------------------------------------


class TestCheckOutOfOrderW2MachineC:
    """W2: target last synced with a machine other than this source."""

    @pytest.mark.asyncio
    async def test_machine_c_non_interactive_returns_false(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Target synced with 'other-host', not this source → warns, returns False."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        source_name = "source-host"
        target_name = "target-host"
        other_machine = "other-host"

        # Local: no meaningful history on this source
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("target", target_name))

        # Target: last synced with 'other-host'
        orchestrator = _make_orchestrator(
            mock_config,
            target=target_name,
            remote_stdout=_history_json("source", other_machine),
        )
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is False
        cast(MagicMock, orchestrator._console).print.assert_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_machine_c_interactive_prompts_user(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Machine-C in interactive mode shows panel and prompts."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        source_name = "source-host"
        target_name = "target-host"
        other_machine = "other-host"

        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("target", target_name))

        orchestrator = _make_orchestrator(
            mock_config,
            target=target_name,
            remote_stdout=_history_json("source", other_machine),
        )
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]

        with (
            patch("rich.prompt.Prompt.ask", return_value="y"),
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._ui).stop.assert_called_once()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# No / unreadable target history → first sync, deferred to FolderSyncJob
# ---------------------------------------------------------------------------


class TestCheckOutOfOrderFirstSyncDeferred:
    """A target with no readable sync history is a first sync, not out-of-order.

    The orchestrator must proceed (return True) without prompting or warning; the
    first-sync overwrite confirmation is owned by FolderSyncJob (ADR-015 refinement).
    """

    @pytest.mark.asyncio
    async def test_target_history_missing_returns_true_without_prompt(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cat returns empty (file not found) → first sync → proceed, no prompt."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            remote_stdout="",  # empty → no readable history
            remote_exit_code=1,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        # No out-of-order prompt and no console warning: this is deferred to the job.
        cast(MagicMock, orchestrator._ui).stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._console).print.assert_not_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_target_history_corrupt_json_returns_true_without_prompt(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cat returns corrupted JSON → parse_sync_state (None, None) → first sync → proceed."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            remote_stdout="not valid json",
            remote_exit_code=0,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._ui).stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_target_history_missing_interactive_does_not_prompt(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even interactively, missing target history proceeds without an out-of-order prompt."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            remote_stdout="",
            remote_exit_code=1,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with (
            patch("rich.prompt.Prompt.ask", return_value="y") as mock_ask,
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        mock_ask.assert_not_called()
        cast(MagicMock, orchestrator._ui).stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# --allow-out-of-order bypass
# ---------------------------------------------------------------------------


class TestCheckOutOfOrderBypass:
    """--allow-out-of-order bypasses all reading and prompting."""

    @pytest.mark.asyncio
    async def test_allow_out_of_order_returns_true_without_reading(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With allow_out_of_order=True the check is bypassed entirely."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            allow_out_of_order=True,
            remote_stdout=_history_json("source", "other-host"),
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        # Remote executor must NOT have been called (check bypassed before SSH read)
        cast(MagicMock, orchestrator._remote_executor).run_command.assert_not_called()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._ui).stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]

    def test_orchestrator_accepts_allow_out_of_order(self, mock_config: MagicMock) -> None:
        """Orchestrator.__init__ stores allow_out_of_order."""
        orchestrator = Orchestrator(
            target="test-target",
            config=mock_config,
            allow_out_of_order=True,
        )
        assert orchestrator._allow_out_of_order is True  # pyright: ignore[reportPrivateUsage]

    def test_orchestrator_defaults_allow_out_of_order_to_false(self, mock_config: MagicMock) -> None:
        """allow_out_of_order defaults to False."""
        orchestrator = Orchestrator(target="test-target", config=mock_config)
        assert orchestrator._allow_out_of_order is False  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# dry-run: warns but never aborts
# ---------------------------------------------------------------------------


class TestCheckOutOfOrderDryRun:
    """dry-run mode: the warning is logged but _check_out_of_order returns True."""

    @pytest.mark.asyncio
    async def test_dry_run_with_consecutive_push_returns_true(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A W3 (consecutive push) condition under --dry-run must not abort; returns True."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        source_name = "source-host"
        target_name = "target-host"

        # Local: this machine was SOURCE to target-host (consecutive push setup)
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("source", target_name))

        orchestrator = _make_orchestrator(
            mock_config,
            target=target_name,
            dry_run=True,
            remote_stdout=_history_json("target", source_name),
        )
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]

        # Non-interactive mode (guarantees no Prompt.ask is reached)
        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        # Logger.warning must have been called (warning logged, not silenced)
        cast(MagicMock, orchestrator._logger).warning.assert_called()  # pyright: ignore[reportPrivateUsage]
        # Prompt must NOT have been shown
        cast(MagicMock, orchestrator._ui).stop.assert_not_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_dry_run_with_machine_c_returns_true(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A W2 condition under --dry-run returns True without prompting."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            dry_run=True,
            remote_stdout=_history_json("source", "other-host"),
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._logger).warning.assert_called()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# _update_sync_history: last_peer is recorded on both ends
# ---------------------------------------------------------------------------


class TestUpdateSyncHistoryWithPeer:
    """_update_sync_history records last_peer on both source and target."""

    @pytest.mark.asyncio
    async def test_local_history_records_peer(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After sync, local history includes last_peer = target hostname."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="target-host", config=mock_config)
        orchestrator._remote_executor = None  # pyright: ignore[reportPrivateUsage]
        orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]

        await orchestrator._update_sync_history()  # pyright: ignore[reportPrivateUsage]

        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        assert history_path.exists()
        data = json.loads(history_path.read_text())
        assert data["last_role"] == "source"
        assert data["last_peer"] == "target-host"

    @pytest.mark.asyncio
    async def test_remote_history_command_includes_peer(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Remote history command must include last_peer = source hostname."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="target-host", config=mock_config)
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]
        orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]

        mock_result = MagicMock()
        mock_result.success = True
        mock_executor = AsyncMock()
        mock_executor.run_command.return_value = mock_result
        orchestrator._remote_executor = mock_executor  # pyright: ignore[reportPrivateUsage]

        await orchestrator._update_sync_history()  # pyright: ignore[reportPrivateUsage]

        mock_executor.run_command.assert_called_once()
        cmd = mock_executor.run_command.call_args[0][0]
        assert "last_role" in cmd
        assert "target" in cmd
        assert "last_peer" in cmd
        assert "source-host" in cmd

    @pytest.mark.asyncio
    async def test_remote_history_failure_raises(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Remote history update failure raises RuntimeError."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="target-host", config=mock_config)
        orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.stderr = "Permission denied"
        mock_executor = AsyncMock()
        mock_executor.run_command.return_value = mock_result
        orchestrator._remote_executor = mock_executor  # pyright: ignore[reportPrivateUsage]

        with pytest.raises(RuntimeError, match="Failed to update sync history on target"):
            await orchestrator._update_sync_history()  # pyright: ignore[reportPrivateUsage]
