"""Unit tests for the abort path (UAT gap 2, plan 01-16; the split of #224).

Proves:
- Each decline site raises the abort class that matches who decided: the config-sync
  prompt a human answered raises SyncAbortedByUser, the out-of-order check — which the
  confirmer refuses without asking anyone in a non-interactive run — raises SyncAborted.
- Orchestrator.run() catches SyncAborted before the generic Exception handler: logs once
  at WARNING (never CRITICAL), sets SessionStatus.ABORTED, and re-raises so the CLI can
  set a non-zero exit code — and only says "by user" for the subclass.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config import Configuration
from pcswitcher.models import SessionStatus, SyncAborted, SyncAbortedByUser, SyncSession
from pcswitcher.orchestrator import Orchestrator


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


def _make_no_op_ui() -> MagicMock:
    """A TerminalUI stand-in: sync methods no-op, consume_events is awaitable."""
    ui = MagicMock()
    ui.consume_events = AsyncMock()
    return ui


class TestConfigSyncDeclineRaisesSyncAbortedByUser:
    """The config-sync decline site raises SyncAbortedByUser, not RuntimeError."""

    @pytest.mark.asyncio
    async def test_config_sync_decline_raises_sync_aborted_by_user(self, mock_config: MagicMock) -> None:
        """User declines the config-sync prompt -> SyncAbortedByUser, not RuntimeError.

        The user kind, because `--yes` and `--dry-run` both continue without asking: the
        only way to reach this branch is a human answering the prompt.
        """
        orchestrator = Orchestrator(target="target-host", config=mock_config)
        orchestrator._remote_executor = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]

        with (
            patch(
                "pcswitcher.orchestrator.sync_config_to_target",
                AsyncMock(return_value=False),
            ),
            pytest.raises(SyncAbortedByUser, match="config sync was declined"),
        ):
            await orchestrator._sync_config_to_target()  # pyright: ignore[reportPrivateUsage]


class TestRunCatchesSyncAborted:
    """run() must catch SyncAborted before the generic Exception handler."""

    @pytest.mark.asyncio
    async def test_out_of_order_decline_logs_warning_never_critical_and_reraises(
        self,
        mock_config: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A declined out-of-order confirmation is ABORTED and logged once at WARNING.

        Drives the real run() with the source/target lock and connection phases
        stubbed (no-op) and _check_out_of_order patched to return False, so the
        inline `raise SyncAborted(...)` at that decline site is exercised
        together with run()'s except SyncAborted handler, without needing
        a real SSH connection, snapshots, or jobs.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        orchestrator = Orchestrator(target="target-host", config=mock_config)
        orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._remote_executor = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._acquire_source_lock = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._establish_connection = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._acquire_target_lock = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._check_out_of_order = AsyncMock(return_value=False)  # pyright: ignore[reportPrivateUsage]

        sessions: list[SyncSession] = []

        def _capture_session(*args: object, **kwargs: object) -> SyncSession:
            session = SyncSession(*args, **kwargs)  # type: ignore[arg-type]
            sessions.append(session)
            return session

        with (
            patch("pcswitcher.orchestrator.setup_logging", return_value=(MagicMock(), MagicMock())),
            patch("pcswitcher.orchestrator.TerminalUI", return_value=_make_no_op_ui()),
            patch("pcswitcher.orchestrator.SyncSession", side_effect=_capture_session),
            pytest.raises(SyncAborted) as caught,
        ):
            await orchestrator.run()

        # The confirmer answers False both for a declined prompt and for a
        # non-interactive run nobody was asked in, so this site claims neither (#224).
        assert not isinstance(caught.value, SyncAbortedByUser)
        assert len(sessions) == 1
        assert sessions[0].status == SessionStatus.ABORTED
        assert sessions[0].ended_at is not None

        logger = orchestrator._logger  # pyright: ignore[reportPrivateUsage]
        logger.warning.assert_called_once()
        logger.critical.assert_not_called()


class TestTheAbortLineSaysWhoDecided:
    """#224 — pc-switcher's own decision to stop the run must not be reported as the
    user's. The exception class carries which it was; the WARNING line renders it.
    """

    @staticmethod
    async def _abort_line(abort: SyncAborted, mock_config: MagicMock) -> str:
        """The one WARNING `run()` logs for `abort`, formatted as it reaches the log file.

        Raised from `_check_out_of_order` because that is the first step inside `run()`'s
        try block that needs nothing stubbed beyond the locks — the handler under test is
        the same one every abort site reaches.
        """
        orchestrator = Orchestrator(target="target-host", config=mock_config)
        orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._remote_executor = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._acquire_source_lock = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._establish_connection = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._acquire_target_lock = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._check_out_of_order = AsyncMock(side_effect=abort)  # pyright: ignore[reportPrivateUsage]

        with (
            patch("pcswitcher.orchestrator.setup_logging", return_value=(MagicMock(), MagicMock())),
            patch("pcswitcher.orchestrator.TerminalUI", return_value=_make_no_op_ui()),
            pytest.raises(SyncAborted),
        ):
            await orchestrator.run()

        logger = orchestrator._logger  # pyright: ignore[reportPrivateUsage]
        logger.warning.assert_called_once()
        template, *args = logger.warning.call_args.args
        return str(template) % tuple(str(arg) for arg in args)

    @pytest.mark.asyncio
    async def test_a_user_chosen_abort_says_the_user_stopped_it(
        self, mock_config: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        abort = SyncAbortedByUser("package review aborted at 'fortunes' (Ctrl-C)")

        line = await self._abort_line(abort, mock_config)

        assert line == "Sync aborted by user: package review aborted at 'fortunes' (Ctrl-C)"

    @pytest.mark.asyncio
    async def test_a_tool_decided_abort_never_says_the_user_stopped_it(
        self, mock_config: MagicMock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The registry nobody can parse is the tool's own decision — the user was not
        asked anything, so the line must not put it on them."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        abort = SyncAborted("the install-snippet registry cannot be read as a registry")

        line = await self._abort_line(abort, mock_config)

        assert line == "Sync aborted: the install-snippet registry cannot be read as a registry"
        assert "user" not in line
