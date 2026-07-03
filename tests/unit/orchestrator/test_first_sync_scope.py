"""Unit tests for the job-agnostic first-sync scope (ADR-015, gap-closure 01-15).

Covers:
- `_first_sync_scopes()` resolving enabled jobs to their self-described
  `FirstSyncScope` (folder paths + mechanism), in config order.
- `_confirm_first_sync()` composing its warning message from those
  self-descriptions, with no job name or transport mechanism hardcoded in the
  orchestrator itself.
- The empty-scope fallback (no enabled job, or no job describes a scope).
- Extensibility: a stub non-rsync SyncJob's FirstSyncScope flows through the
  composed warning unchanged, proving a future job needs no orchestrator change.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import FirstSyncScope
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


class _RecordingConfirmer:
    """Fake Confirmer that records the composed title/message instead of prompting."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def confirm(
        self,
        *,
        title: str,
        message: str,
        allow: bool,
        allow_flag: str,
        log_extra: dict[str, Any] | None = None,
    ) -> bool:
        self.calls.append({"title": title, "message": message, "allow": allow, "allow_flag": allow_flag})
        return True


def _make_orchestrator(
    mock_config: MagicMock, *, target: str = "target-host"
) -> tuple[Orchestrator, _RecordingConfirmer]:
    """Create an Orchestrator wired with a recording confirmer (no real prompting)."""
    orchestrator = Orchestrator(target=target, config=mock_config)
    orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    confirmer = _RecordingConfirmer()
    orchestrator._confirmer = confirmer  # pyright: ignore[reportPrivateUsage]
    return orchestrator, confirmer


class TestFirstSyncScopesFolderSync:
    """_first_sync_scopes() resolves the real folder_sync job from config."""

    def test_returns_scope_with_enabled_folder_paths(self, mock_config: MagicMock) -> None:
        """Two enabled folders in folder_sync's config produce one FirstSyncScope."""
        mock_config.sync_jobs = {"folder_sync": True}
        mock_config.job_configs = {
            "folder_sync": {
                "folders": [
                    {"path": "/home"},
                    {"path": "/root"},
                ]
            }
        }
        orchestrator, _ = _make_orchestrator(mock_config)

        scopes = orchestrator._first_sync_scopes()  # pyright: ignore[reportPrivateUsage]

        assert len(scopes) == 1
        assert scopes[0].job_name == "folder_sync"
        assert scopes[0].scope_items == ["/home", "/root"]
        assert scopes[0].mechanism

    @pytest.mark.asyncio
    async def test_confirm_message_contains_folder_paths(self, mock_config: MagicMock) -> None:
        """The composed warning names each folder path from the job's self-description."""
        mock_config.sync_jobs = {"folder_sync": True}
        mock_config.job_configs = {"folder_sync": {"folders": [{"path": "/home"}, {"path": "/root"}]}}
        orchestrator, confirmer = _make_orchestrator(mock_config)

        result = await orchestrator._confirm_first_sync()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        assert len(confirmer.calls) == 1
        message = confirmer.calls[0]["message"]
        assert "/home" in message
        assert "/root" in message

    @pytest.mark.asyncio
    async def test_disabled_folder_sync_job_contributes_nothing(self, mock_config: MagicMock) -> None:
        """A disabled sync job is skipped entirely — no scope, no import attempted."""
        mock_config.sync_jobs = {"folder_sync": False}
        mock_config.job_configs = {"folder_sync": {"folders": [{"path": "/home"}]}}
        orchestrator, _ = _make_orchestrator(mock_config)

        scopes = orchestrator._first_sync_scopes()  # pyright: ignore[reportPrivateUsage]

        assert scopes == []


class TestFirstSyncScopesEmptyFallback:
    """With no enabled sync jobs, the warning falls back to generic phrasing."""

    def test_no_enabled_jobs_yields_empty_scopes(self, mock_config: MagicMock) -> None:
        """No enabled sync jobs → _first_sync_scopes() returns an empty list."""
        mock_config.sync_jobs = {}
        orchestrator, _ = _make_orchestrator(mock_config)

        assert orchestrator._first_sync_scopes() == []  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_confirm_message_uses_generic_fallback(self, mock_config: MagicMock) -> None:
        """The composed warning names no transport mechanism when no job describes a scope."""
        mock_config.sync_jobs = {}
        orchestrator, confirmer = _make_orchestrator(mock_config)

        result = await orchestrator._confirm_first_sync()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        message = confirmer.calls[0]["message"]
        assert "rsync" not in message.lower()
        assert "folder_sync" not in message


class _StubDockerLikeSyncJob(SyncJob):
    """A hermetic stub SyncJob standing in for a future non-rsync job (e.g. packages/docker)."""

    name = "stub_containers"

    @classmethod
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["my-app-container", "my-db-container"],
            mechanism="docker volume overwrite",
        )


class TestFirstSyncScopesExtensibility:
    """A non-rsync job's self-description flows through the warning unchanged."""

    @pytest.mark.asyncio
    async def test_stub_non_rsync_job_surfaces_in_warning(
        self, mock_config: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stub job resolved in place of a real module still composes correctly."""
        mock_config.sync_jobs = {"stub_containers": True}
        mock_config.job_configs = {"stub_containers": {}}
        orchestrator, confirmer = _make_orchestrator(mock_config)

        def fake_resolve(job_name: str) -> type[SyncJob] | None:
            return _StubDockerLikeSyncJob if job_name == "stub_containers" else None

        monkeypatch.setattr(orchestrator, "_resolve_sync_job_class", fake_resolve)

        result = await orchestrator._confirm_first_sync()  # pyright: ignore[reportPrivateUsage]

        assert result is True
        message = confirmer.calls[0]["message"]
        assert "my-app-container" in message
        assert "my-db-container" in message
        assert "docker volume overwrite" in message
        assert "stub_containers" in message
