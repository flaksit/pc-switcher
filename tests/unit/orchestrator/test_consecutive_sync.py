"""Unit tests for the orchestrator target-state pre-flight check (_check_out_of_order).

Covers both gates the pre-flight dispatches to (ADR-015):
- Clean case (target_peer == source, no consecutive push) → silent True
- W1 (no readable target history = FIRST SYNC): confirmed via --allow-first-sync
- W2: machine-C (target last synced with a different peer) → confirmed via --allow-out-of-order
- W3: consecutive push to a clean target → confirmed via --allow-out-of-order
- --allow-out-of-order → bypass W2/W3 (still reads history to detect first sync)
- dry-run with any warn condition → True without prompting (ADR-014)

Per the ADR-015 refinement, first-sync (W1) and out-of-order (W2/W3) are distinct
gates with distinct flags, but BOTH are orchestrator-level pre-flight checks — the
"first sync overwrites the target" question is asked once centrally, not per-job.
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
    allow_first_sync: bool = False,
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
        allow_first_sync=allow_first_sync,
        dry_run=dry_run,
    )
    orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    # Wire the real shared confirmer to the mock console/ui so the interactive path
    # exercises Panel/Prompt and the ui.pause()/resume() pause exactly as in production.
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
        cast(MagicMock, orchestrator._ui).pause.assert_not_called()  # pyright: ignore[reportPrivateUsage]


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
        cast(MagicMock, orchestrator._ui).pause.assert_called_once()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._ui).resume.assert_called_once()  # pyright: ignore[reportPrivateUsage]

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
        # UI must NOT be paused in non-interactive path
        cast(MagicMock, orchestrator._ui).pause.assert_not_called()  # pyright: ignore[reportPrivateUsage]
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
        cast(MagicMock, orchestrator._ui).pause.assert_called_once()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# W1: no / unreadable target history → first sync (orchestrator-level, --allow-first-sync)
# ---------------------------------------------------------------------------


class TestCheckFirstSync:
    """W1: a target with no readable sync history is a first-ever sync.

    Handled by the orchestrator pre-flight and gated by --allow-first-sync (distinct
    from the W2/W3 --allow-out-of-order gate). The overwrite question is asked once
    centrally, not per-job (ADR-015).
    """

    @pytest.mark.asyncio
    async def test_first_sync_interactive_accepts(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing target history, interactive user answers 'y' → True, prompt shown."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            remote_stdout="",  # empty → no readable history → first sync
            remote_exit_code=1,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with (
            patch("rich.prompt.Prompt.ask", return_value="y"),
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._ui).pause.assert_called_once()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._ui).resume.assert_called_once()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_first_sync_interactive_declines(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing target history, interactive user answers 'n' → False."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            remote_stdout="",
            remote_exit_code=1,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with (
            patch("rich.prompt.Prompt.ask", return_value="n"),
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is False

    @pytest.mark.asyncio
    async def test_first_sync_corrupt_json_prompts(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Corrupt target history (parse → None) is treated as a first sync and prompts."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            remote_stdout="not valid json",
            remote_exit_code=0,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with (
            patch("rich.prompt.Prompt.ask", return_value="y"),
            patch.object(sys, "stdin", _mock_isatty(True)),
        ):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._ui).pause.assert_called_once()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_first_sync_non_interactive_without_flag_returns_false(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First sync, no TTY, no --allow-first-sync → False, message printed."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            remote_stdout="",
            remote_exit_code=1,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is False
        cast(MagicMock, orchestrator._ui).pause.assert_not_called()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._console).print.assert_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_first_sync_non_interactive_with_flag_returns_true(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First sync, no TTY, --allow-first-sync set → auto-approved (True)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            allow_first_sync=True,
            remote_stdout="",
            remote_exit_code=1,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._ui).pause.assert_not_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_first_sync_not_gated_by_allow_out_of_order(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--allow-out-of-order does NOT bypass the first-sync gate (distinct flag)."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            allow_out_of_order=True,  # must not auto-approve a first sync
            remote_stdout="",
            remote_exit_code=1,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is False

    @pytest.mark.asyncio
    async def test_first_sync_dry_run_returns_true(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First sync under --dry-run logs a warning and proceeds without prompting."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            dry_run=True,
            remote_stdout="",
            remote_exit_code=1,
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        cast(MagicMock, orchestrator._logger).warning.assert_called()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._ui).pause.assert_not_called()  # pyright: ignore[reportPrivateUsage]


# ---------------------------------------------------------------------------
# --allow-out-of-order bypass (W2/W3 only; history is still read for first-sync)
# ---------------------------------------------------------------------------


class TestCheckOutOfOrderBypass:
    """--allow-out-of-order bypasses the W2/W3 prompt but not the first-sync gate."""

    @pytest.mark.asyncio
    async def test_allow_out_of_order_bypasses_w2_prompt(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With allow_out_of_order=True a W2 (machine-C) history proceeds without prompting."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = _make_orchestrator(
            mock_config,
            target="target-host",
            allow_out_of_order=True,
            remote_stdout=_history_json("source", "other-host"),  # W2, readable history
        )
        orchestrator._source_hostname = "source-host"  # pyright: ignore[reportPrivateUsage]

        result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        # History IS read (needed to distinguish first-sync from out-of-order),
        # but no prompt is shown for the bypassed W2/W3 case.
        cast(MagicMock, orchestrator._remote_executor).run_command.assert_called()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._ui).pause.assert_not_called()  # pyright: ignore[reportPrivateUsage]

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

    def test_orchestrator_accepts_allow_first_sync(self, mock_config: MagicMock) -> None:
        """Orchestrator.__init__ stores allow_first_sync."""
        orchestrator = Orchestrator(
            target="test-target",
            config=mock_config,
            allow_first_sync=True,
        )
        assert orchestrator._allow_first_sync is True  # pyright: ignore[reportPrivateUsage]

    def test_orchestrator_defaults_allow_first_sync_to_false(self, mock_config: MagicMock) -> None:
        """allow_first_sync defaults to False."""
        orchestrator = Orchestrator(target="test-target", config=mock_config)
        assert orchestrator._allow_first_sync is False  # pyright: ignore[reportPrivateUsage]


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
        cast(MagicMock, orchestrator._ui).pause.assert_not_called()  # pyright: ignore[reportPrivateUsage]

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


class TestCheckOutOfOrderHostnameCasing:
    """A clean back-sync must be recognised regardless of hostname casing.

    Regression for the spurious "Target Last Synced with a Different Machine"
    warning: the target recorded this machine's peer under a differently-cased
    name (e.g. the user typed `sync fleksi` on the target, so it stored `fleksi`,
    while this machine's own hostname is `Fleksi`). The topology comparison is
    case-insensitive, so the clean A→B / B→A pattern still proceeds silently.
    """

    @pytest.mark.asyncio
    async def test_case_mismatched_back_sync_is_clean(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """target_peer differs from source only in case → suppressed, no prompt."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # This machine's real hostname (capitalised).
        source_name = "Fleksi"
        target_name = "p17"

        # Local: this machine last synced as TARGET, peer recorded as the target's
        # own hostname "P17" (capitalised) — differs in case from the CLI arg "p17".
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("target", "P17"))

        # Target (p17): last synced as SOURCE, peer recorded as "fleksi" (lowercase,
        # the CLI arg the user typed on p17) — differs in case from "Fleksi".
        orchestrator = _make_orchestrator(
            mock_config,
            target=target_name,
            remote_stdout=_history_json("source", "fleksi"),
        )
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        # No warning, no prompt: the case-only difference is a clean back-sync.
        cast(MagicMock, orchestrator._logger).warning.assert_not_called()  # pyright: ignore[reportPrivateUsage]
        cast(MagicMock, orchestrator._ui).pause.assert_not_called()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_consecutive_push_detected_across_casing(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Consecutive push (W3) is still detected when the resolved target hostname
        differs in case from the local peer record — case-insensitive match must not
        let a genuine repeat push slip through as clean."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        source_name = "source-host"

        # Local: this machine was SOURCE, peer recorded as "P17".
        history_path = tmp_path / ".local/share/pc-switcher/sync-history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(_history_json("source", "P17"))

        # Target resolves its own hostname as "p17" (lowercase); target history shows
        # it last synced with this source (clean-looking), but we are pushing again.
        orchestrator = _make_orchestrator(
            mock_config,
            target="p17",
            remote_stdout=_history_json("target", source_name),
        )
        orchestrator._source_hostname = source_name  # pyright: ignore[reportPrivateUsage]
        orchestrator._target_canonical_hostname = "p17"  # pyright: ignore[reportPrivateUsage]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            result = await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]

        # W3 fires → non-interactive → False, warning shown.
        assert result is False
        cast(MagicMock, orchestrator._console).print.assert_called()  # pyright: ignore[reportPrivateUsage]


class TestResolveTargetCanonicalHostname:
    """_resolve_target_canonical_hostname queries the target the same way the source
    resolves its own hostname, and falls back to the CLI argument on failure."""

    @pytest.mark.asyncio
    async def test_resolves_hostname_over_ssh(self, mock_config: MagicMock) -> None:
        """A successful query overwrites the CLI-argument fallback with the real hostname."""
        orchestrator = _make_orchestrator(
            mock_config,
            target="p17",
            remote_stdout="P17\n",
        )
        assert orchestrator._target_canonical_hostname == "p17"  # pyright: ignore[reportPrivateUsage]

        await orchestrator._resolve_target_canonical_hostname()  # pyright: ignore[reportPrivateUsage]

        assert orchestrator._target_canonical_hostname == "P17"  # pyright: ignore[reportPrivateUsage]
        # The SSH-connectable name is untouched (rsync/SSH destination).
        assert orchestrator._target_hostname == "p17"  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_falls_back_to_cli_arg_on_failure(self, mock_config: MagicMock) -> None:
        """A failed/empty query keeps the CLI-argument fallback."""
        orchestrator = _make_orchestrator(
            mock_config,
            target="p17",
            remote_stdout="",
            remote_exit_code=1,
        )

        await orchestrator._resolve_target_canonical_hostname()  # pyright: ignore[reportPrivateUsage]

        assert orchestrator._target_canonical_hostname == "p17"  # pyright: ignore[reportPrivateUsage]


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
    async def test_local_history_records_resolved_hostname_not_cli_arg(
        self,
        mock_config: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """last_peer is the target's resolved hostname, not the (possibly aliased) CLI arg."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="p17.lan", config=mock_config)
        orchestrator._target_canonical_hostname = "P17"  # pyright: ignore[reportPrivateUsage]
        orchestrator._remote_executor = None  # pyright: ignore[reportPrivateUsage]
        orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]

        await orchestrator._update_sync_history()  # pyright: ignore[reportPrivateUsage]

        data = json.loads((tmp_path / ".local/share/pc-switcher/sync-history.json").read_text())
        assert data["last_peer"] == "P17"

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
