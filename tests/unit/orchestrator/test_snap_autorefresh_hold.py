"""Unit tests for the transient snapd auto-refresh hold the orchestrator applies around
the RUN_JOBS window (decision 4, 02-UAT-REVIEW-FIXES).

Covers:
- The hold is set on BOTH hosts when `snap_sync` is enabled, and captures the prior
  `refresh.hold` first (read-only `snap get` before any `snap set`).
- The hold is NOT set when `snap_sync` is disabled, nor in dry-run.
- Cleanup restores an unset state when there was no prior hold, and restores the exact
  prior value when there was one (the "prior hold preserved" case).
- Restore is a no-op when no hold was engaged, and never blocks the manual `--revision`
  convergence (the hold command only writes the system-wide `refresh.hold` option).

All executor interactions are mocked; no real snap/snapd commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.models import CommandResult
from pcswitcher.orchestrator import Orchestrator


def respond_to(
    mapping: dict[str, CommandResult], default: CommandResult | None = None
) -> Callable[..., CommandResult]:
    """Build a run_command side_effect matching by substring (first match wins)."""
    fallback = default if default is not None else CommandResult(exit_code=0, stdout="", stderr="")

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


def make_executor(responses: dict[str, CommandResult] | None = None) -> MagicMock:
    ex = MagicMock()
    ex.run_command = AsyncMock(side_effect=respond_to(responses or {}))
    return ex


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


def make_orchestrator(
    *,
    snap_sync_enabled: bool,
    dry_run: bool = False,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
) -> tuple[Orchestrator, MagicMock, MagicMock]:
    config = MagicMock(spec=Configuration)
    config.sync_jobs = {"snap_sync": snap_sync_enabled}
    orchestrator = Orchestrator(target="target-host", config=config, dry_run=dry_run)
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    source = make_executor(source_responses)
    target = make_executor(target_responses)
    orchestrator._local_executor = source  # pyright: ignore[reportPrivateUsage]
    orchestrator._remote_executor = target  # pyright: ignore[reportPrivateUsage]
    return orchestrator, source, target


class TestHoldEngaged:
    @pytest.mark.asyncio
    async def test_hold_set_on_both_hosts_when_snap_sync_enabled(self) -> None:
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            cmds = all_calls(ex)
            assert any("snap set system refresh.hold=" in c for c in cmds)
            # Timed hold: the value is computed on the host from `date`, not indefinite.
            assert any("date -u -d" in c for c in cmds)
            # Never the indefinite `snap refresh --hold` verb (snap_sync Pitfall 1).
            assert not any("snap refresh --hold" in c for c in cmds)
        assert orchestrator._snap_hold_engaged is True  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_capture_is_read_only_and_precedes_the_set(self) -> None:
        orchestrator, source, _target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        cmds = all_calls(source)
        get_idx = next(i for i, c in enumerate(cmds) if "snap get system refresh.hold" in c)
        set_idx = next(i for i, c in enumerate(cmds) if "snap set system refresh.hold=" in c)
        assert get_idx < set_idx

    @pytest.mark.asyncio
    async def test_hold_not_set_when_snap_sync_disabled(self) -> None:
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=False)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert not any("refresh.hold" in c for c in all_calls(source))
        assert not any("refresh.hold" in c for c in all_calls(target))
        assert orchestrator._snap_hold_engaged is False  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_hold_skipped_in_dry_run(self) -> None:
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True, dry_run=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert not any("refresh.hold" in c for c in all_calls(source))
        assert not any("refresh.hold" in c for c in all_calls(target))
        assert orchestrator._snap_hold_engaged is False  # pyright: ignore[reportPrivateUsage]


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_unsets_when_no_prior_hold(self) -> None:
        # `snap get` returns non-zero for an unset option -> no prior hold captured.
        no_hold = {"snap get system refresh.hold": CommandResult(1, "", 'has no "refresh.hold"')}
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True, source_responses=no_hold, target_responses=no_hold
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert any('snap set system refresh.hold=""' in c for c in all_calls(ex))
        assert orchestrator._snap_hold_engaged is False  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_restore_preserves_prior_hold_per_host(self) -> None:
        """A hold the user already set is captured and restored EXACTLY — a timestamp on the
        source, the literal `forever` on the target (decision 4: do not clobber it).
        """
        prior_ts = "2026-07-24T18:00:00Z"
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True,
            source_responses={"snap get system refresh.hold": CommandResult(0, prior_ts + "\n", "")},
            target_responses={"snap get system refresh.hold": CommandResult(0, "forever\n", "")},
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._snap_hold_prior_source == prior_ts  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._snap_hold_prior_target == "forever"  # pyright: ignore[reportPrivateUsage]

        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert any(f"snap set system refresh.hold={prior_ts}" in c for c in all_calls(source))
        assert any("snap set system refresh.hold=forever" in c for c in all_calls(target))
        # The restore must NOT unset (that would clobber the user's prior hold).
        assert not any('snap set system refresh.hold=""' in c for c in all_calls(source))

    @pytest.mark.asyncio
    async def test_restore_is_noop_when_no_hold_engaged(self) -> None:
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=False)

        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert all_calls(source) == []
        assert all_calls(target) == []

    @pytest.mark.asyncio
    async def test_restore_is_idempotent(self) -> None:
        orchestrator, _source, target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        restore_count = sum(1 for c in all_calls(target) if "snap set system refresh.hold=" in c)
        # A second restore must issue no further commands.
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        assert sum(1 for c in all_calls(target) if "snap set system refresh.hold=" in c) == restore_count


class TestHoldDoesNotBlockConvergence:
    @pytest.mark.asyncio
    async def test_hold_only_writes_refresh_hold_never_a_snap_refresh_command(self) -> None:
        """The hold writes the auto-refresh gate (`refresh.hold`) only; it issues no
        `snap install/refresh --revision` command, so it cannot interfere with (nor
        substitute for) snap_sync's own manual convergence.
        """
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            for c in all_calls(ex):
                assert "snap install" not in c
                assert "snap refresh" not in c
