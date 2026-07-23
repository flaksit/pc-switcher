"""Unit tests for SnapSyncJob: header-based `snap list --all` parsing, the snap-specific
plan()/diff pipeline, revision+channel convergence, and the D-06 no-hold guarantee.

All executor interactions are mocked; no real snap/snapd commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.package_items import DiffAction, DiffClass
from pcswitcher.jobs.snap_sync import SnapSyncJob, snap_sync_exclude_paths
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator

# `Name Version Rev Tracking Publisher Notes` matches the live layout RESEARCH.md
# verified against real snapd 2.76.1 output.
_HEADER = "Name      Version    Rev    Tracking        Publisher    Notes\n"

SNAP_LIST_SOURCE = (
    _HEADER
    + "alpha     1.0        10     latest/stable   pub✓         -\n"
    + "beta      2.0        20     latest/stable   pub✓         -\n"
    + "gamma     3.0        30     latest/edge     pub✓         -\n"
)

SNAP_LIST_TARGET = (
    _HEADER
    + "beta      1.5        15     latest/stable   pub✓         -\n"
    + "gamma     3.0        30     latest/stable   pub✓         -\n"
    + "delta     4.0        40     latest/stable   pub✓         -\n"
)

SNAP_LIST_WITH_DISABLED_REVISION = (
    _HEADER
    + "firefox   118.0      2938   latest/stable   pub✓         -\n"
    + "firefox   117.0      2911   latest/stable   pub✓         disabled\n"
)

# Same rows as SNAP_LIST_SOURCE's `alpha` line, but header AND body columns swapped
# (Notes/Tracking/Name/Rev/Publisher) to prove parsing is header-driven, not positional.
SNAP_LIST_COLUMN_REORDERED = (
    "Notes    Tracking        Name      Rev    Publisher\n" + "-        latest/stable   alpha     10     pub✓\n"
)


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


def make_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target = MagicMock()
    target.run_command = AsyncMock(side_effect=respond_to(target_responses or {}))
    context = JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
    )
    return context, source, target


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


class TestCapture:
    """Header-based capture (RESEARCH Open Question 2): parses by column NAME, never
    fixed offsets or assumed order.
    """

    @pytest.mark.asyncio
    async def test_capture_source_items_parses_name_rev_tracking_by_header(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert [item.name for item in items] == ["alpha", "beta", "gamma"]
        assert [item.revision for item in items] == ["10", "20", "30"]
        assert [item.channel for item in items] == ["latest/stable", "latest/stable", "latest/edge"]

    @pytest.mark.asyncio
    async def test_column_reordered_header_still_parses_correctly(self) -> None:
        """Two columns swapped in BOTH header and body — parsing must still be correct,
        proving it is header-driven rather than positional.
        """
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_COLUMN_REORDERED, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert len(items) == 1
        assert items[0].name == "alpha"
        assert items[0].revision == "10"
        assert items[0].channel == "latest/stable"

    @pytest.mark.asyncio
    async def test_disabled_revision_line_produces_no_item(self) -> None:
        """A disabled older-revision line for a snap that also has an active line
        yields only the active revision as an item.
        """
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_WITH_DISABLED_REVISION, "")}
        )
        job = SnapSyncJob(context)

        items = await job.capture_source_items()

        assert len(items) == 1
        assert items[0].revision == "2938"

    @pytest.mark.asyncio
    async def test_no_snaps_installed_yields_empty_list_not_a_crash(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")}
        )
        job = SnapSyncJob(context)

        assert await job.capture_source_items() == []


class TestDiff:
    """`plan()`'s snap-specific diff: install/remove/change, D-06's active-converge rule."""

    @pytest.mark.asyncio
    async def test_missing_on_target_yields_install_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        alpha = next(d for d in plan.diffs if d.item_id == "snap:alpha")
        assert alpha.diff_class == DiffClass.MISSING_ON_TARGET
        assert alpha.action == DiffAction.INSTALL

    @pytest.mark.asyncio
    async def test_extra_on_target_yields_remove_diff_in_its_own_group(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        delta = next(d for d in plan.diffs if d.item_id == "snap:delta")
        assert delta.action == DiffAction.REMOVE
        remove_group = next(g for g in plan.groups if g.action == "remove")
        install_group = next(g for g in plan.groups if g.action == "install")
        assert {e.item_id for e in remove_group.entries} == {"snap:delta"}
        assert "snap:delta" not in {e.item_id for e in install_group.entries}

    @pytest.mark.asyncio
    async def test_revision_change_yields_change_diff_naming_both_revisions(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        beta = next(d for d in plan.diffs if d.item_id == "snap:beta")
        assert beta.action == DiffAction.CHANGE
        assert beta.detail is not None
        assert "20" in beta.detail
        assert "15" in beta.detail

    @pytest.mark.asyncio
    async def test_same_revision_different_channel_yields_change_diff_naming_both_channels(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        gamma = next(d for d in plan.diffs if d.item_id == "snap:gamma")
        assert gamma.action == DiffAction.CHANGE
        assert gamma.detail is not None
        assert "latest/edge" in gamma.detail
        assert "latest/stable" in gamma.detail

    @pytest.mark.asyncio
    async def test_identical_snap_yields_no_diff(self) -> None:
        identical = _HEADER + "epsilon   1.0   50   latest/stable   pub✓   -\n"
        context, _source, _target = make_context(
            source_responses={"snap list --all": CommandResult(0, identical, "")},
            target_responses={"snap list --all": CommandResult(0, identical, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()


class TestPlanReadOnly:
    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_snap_command(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 4  # alpha install, beta change, gamma change, delta remove
        for cmd in all_calls(target):
            assert "snap install" not in cmd
            assert "snap refresh" not in cmd
            assert "snap switch" not in cmd
            assert "snap remove" not in cmd


class TestNoHold:
    """The single most important guarantee (D-06, RESEARCH Pitfall 1): no command this
    job issues across install/change/channel-retrack/removal ever sets a snap hold.
    """

    @pytest.mark.asyncio
    async def test_install_change_retrack_and_removal_never_set_a_hold(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        assert len(plan.diffs) == 4

        for diff in plan.diffs:
            await job.converge(diff)

        commands = all_calls(target)
        assert commands
        assert not any("--hold" in cmd for cmd in commands)
        assert any("--revision=" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_install_command_contains_an_explicit_revision(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        alpha_diff = next(d for d in plan.diffs if d.item_id == "snap:alpha")

        await job.converge(alpha_diff)

        commands = all_calls(target)
        assert any("snap install --revision=10 alpha" in cmd for cmd in commands)


class TestConvergeRemoval:
    @pytest.mark.asyncio
    async def test_removal_never_passes_purge(self) -> None:
        context, _source, target = make_context(
            source_responses={"snap list --all": CommandResult(0, SNAP_LIST_SOURCE, "")},
            target_responses={"snap list --all": CommandResult(0, SNAP_LIST_TARGET, "")},
        )
        job = SnapSyncJob(context)
        plan = await job.plan()
        delta_diff = next(d for d in plan.diffs if d.item_id == "snap:delta")

        await job.converge(delta_diff)

        commands = all_calls(target)
        assert any("snap remove delta" in cmd for cmd in commands)
        assert not any("purge" in cmd for cmd in commands)


class TestExcludePaths:
    def test_returns_revision_dirs_excludes_common_and_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        firefox_dir = tmp_path / "snap" / "firefox"
        revision_dir = firefox_dir / "2938"
        common_dir = firefox_dir / "common"
        revision_dir.mkdir(parents=True)
        common_dir.mkdir(parents=True)
        (firefox_dir / "current").symlink_to(revision_dir, target_is_directory=True)

        paths = snap_sync_exclude_paths()

        assert revision_dir in paths
        assert not any(p.name == "common" for p in paths)
        assert not any(p.name == "current" for p in paths)

    def test_no_snap_directory_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        assert snap_sync_exclude_paths() == []


class TestValidate:
    @pytest.mark.asyncio
    async def test_snap_unavailable_on_source_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            source_responses={"snap version": CommandResult(127, "", "not found")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "snap is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_snap_unavailable_on_target_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            target_responses={"snap version": CommandResult(127, "", "not found")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "snap is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_target_without_passwordless_sudo_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            target_responses={"sudo -n true": CommandResult(1, "", "sudo: a password is required")}
        )
        job = SnapSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors(self) -> None:
        context, _source, _target = make_context()
        job = SnapSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_snap_sync_to_snap_sync_job(self) -> None:
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("snap_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is SnapSyncJob
