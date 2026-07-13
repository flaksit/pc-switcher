"""Unit tests for the SyncAbortedByUser abort path (UAT gap 2, plan 01-16).

Proves:
- Both decline sites (config-sync, out-of-order check) raise SyncAbortedByUser,
  not a plain RuntimeError.
- Orchestrator.run() catches SyncAbortedByUser before the generic Exception
  handler: logs once at WARNING (never CRITICAL), sets SessionStatus.ABORTED,
  and re-raises so the CLI can set a non-zero exit code.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config import Configuration
from pcswitcher.models import SessionStatus, SyncAbortedByUser, SyncSession
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
        """User declines the config-sync prompt -> SyncAbortedByUser, not RuntimeError."""
        orchestrator = Orchestrator(target="target-host", config=mock_config)
        orchestrator._remote_executor = AsyncMock()  # pyright: ignore[reportPrivateUsage]
        orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]

        with (
            patch(
                "pcswitcher.orchestrator.sync_config_to_target",
                AsyncMock(return_value=False),
            ),
            pytest.raises(SyncAbortedByUser, match="Config sync aborted by user"),
        ):
            await orchestrator._sync_config_to_target()  # pyright: ignore[reportPrivateUsage]


class TestRunCatchesSyncAbortedByUser:
    """run() must catch SyncAbortedByUser before the generic Exception handler."""

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
        inline `raise SyncAbortedByUser(...)` at that decline site is exercised
        together with run()'s except SyncAbortedByUser handler, without needing
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
            pytest.raises(SyncAbortedByUser),
        ):
            await orchestrator.run()

        assert len(sessions) == 1
        assert sessions[0].status == SessionStatus.ABORTED
        assert sessions[0].ended_at is not None

        logger = orchestrator._logger  # pyright: ignore[reportPrivateUsage]
        logger.warning.assert_called_once()
        logger.critical.assert_not_called()
