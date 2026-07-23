"""Unit tests for FlatpakSyncJob: tab-separated `flatpak list`/`flatpak remotes`
parsing, the flatpak-specific plan()/diff pipeline, scope-as-identity, remote-before-
ref convergence ordering, and the missing-origin-remote skip guard.

All executor interactions are mocked; no real flatpak commands run.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.flatpak_sync import FlatpakSyncJob, flatpak_sync_exclude_paths
from pcswitcher.jobs.package_items import AptPackageItem, DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.package_phase import PackagePhaseCoordinator
from pcswitcher.jobs.package_review import Decision, ReviewGroup, ReviewOutcome
from pcswitcher.jobs.package_sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator

# `flatpak list --app --columns=application,version,origin,installation` has NO
# header row (RESEARCH: verified live against Flatpak 1.14.6, unlike `snap list`) —
# the --columns flag itself names the columns, so output is exactly those four
# tab-separated fields per line.
FLATPAK_LIST_SOURCE = (
    "com.slack.Slack\t4.50.0\tflathub\tsystem\n"
    "org.gnome.Podcasts\t1.0\tflathub\tuser\n"
    "org.gimp.GIMP\t2.10\tflathub\tuser\n"
    "org.example.SplitScope\t1.0\tflathub\tuser\n"
    "org.example.NeedsRemote\t1.0\tcustomremote\tuser\n"
)

FLATPAK_LIST_TARGET = (
    "org.gnome.Podcasts\t1.0\tflathub\tuser\n"
    "org.gimp.GIMP\t2.9\tflathub\tuser\n"
    "com.spotify.Client\t1.0\tflathub\tuser\n"
    "org.example.SplitScope\t1.0\tflathub\tsystem\n"
)

FLATPAK_LIST_BOTH_SCOPES = "org.example.App\t1.0\tflathub\tuser\norg.example.App\t1.0\tflathub\tsystem\n"

_FLATHUB_REMOTE_LINE = "flathub\thttps://dl.flathub.org/repo/\n"

SOURCE_RESPONSES = {
    "flatpak list --app --columns=application,version,origin,installation": CommandResult(0, FLATPAK_LIST_SOURCE, ""),
    "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
    "flatpak remotes --system --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
}

TARGET_RESPONSES = {
    "flatpak list --app --columns=application,version,origin,installation": CommandResult(0, FLATPAK_LIST_TARGET, ""),
    "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
    "flatpak remotes --system --columns=name,url": CommandResult(0, "", ""),
}


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
    """Tab-separated capture (RESEARCH: `flatpak list`/`flatpak remotes` name their
    own columns via `--columns`, so there is no header row to parse).
    """

    @pytest.mark.asyncio
    async def test_capture_source_items_parses_application_version_origin_scope(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, FLATPAK_LIST_SOURCE, "")}
        )
        job = FlatpakSyncJob(context)

        items = await job.capture_source_items()

        assert [item.application for item in items] == [
            "com.slack.Slack",
            "org.gnome.Podcasts",
            "org.gimp.GIMP",
            "org.example.SplitScope",
            "org.example.NeedsRemote",
        ]
        slack = items[0]
        assert slack.version == "4.50.0"
        assert slack.origin == "flathub"
        assert slack.scope == "system"

    @pytest.mark.asyncio
    async def test_same_application_both_scopes_yields_two_distinct_identities(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, FLATPAK_LIST_BOTH_SCOPES, "")}
        )
        job = FlatpakSyncJob(context)

        items = await job.capture_source_items()

        assert len(items) == 2
        assert items[0].item_id != items[1].item_id
        assert {item.scope for item in items} == {"user", "system"}

    @pytest.mark.asyncio
    async def test_unrecognized_installation_value_is_skipped(self) -> None:
        weird = "org.example.Weird\t1.0\tflathub\tcustom-install\n"
        context, _source, _target = make_context(source_responses={"flatpak list --app": CommandResult(0, weird, "")})
        job = FlatpakSyncJob(context)

        assert await job.capture_source_items() == []

    @pytest.mark.asyncio
    async def test_no_apps_installed_yields_empty_list_not_a_crash(self) -> None:
        context, _source, _target = make_context(source_responses={"flatpak list --app": CommandResult(0, "", "")})
        job = FlatpakSyncJob(context)

        assert await job.capture_source_items() == []


class TestPlanDiff:
    """`plan()`'s flatpak-specific diff: install/remove/report_only for refs,
    install/remove for remotes, ordered remotes-before-refs (D-14).
    """

    @pytest.mark.asyncio
    async def test_full_diff_taxonomy(self) -> None:
        context, _source, _target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 7
        by_id = {diff.item_id: diff for diff in plan.diffs}

        # Missing on target -> install.
        assert by_id["flatpak:ref:system:com.slack.Slack"].action == DiffAction.INSTALL
        assert by_id["flatpak:ref:system:com.slack.Slack"].diff_class == DiffClass.MISSING_ON_TARGET

        # Version differs, same scope -> report_only, never a converge verb (D-04).
        gimp = by_id["flatpak:ref:user:org.gimp.GIMP"]
        assert gimp.action == DiffAction.REPORT_ONLY
        assert gimp.diff_class == DiffClass.VERSION_MISMATCH
        assert gimp.detail is not None
        assert "2.10" in gimp.detail
        assert "2.9" in gimp.detail

        # Same application, different scope on each machine -> one install, one
        # removal, never a single change (scope is identity, module docstring).
        assert by_id["flatpak:ref:user:org.example.SplitScope"].action == DiffAction.INSTALL
        assert by_id["flatpak:ref:system:org.example.SplitScope"].action == DiffAction.REMOVE

        # Extra on target -> removal, its own review group.
        assert by_id["flatpak:ref:user:com.spotify.Client"].action == DiffAction.REMOVE
        remove_group = next(g for g in plan.groups if g.action == "remove")
        install_group = next(g for g in plan.groups if g.action == "install")
        assert "flatpak:ref:user:com.spotify.Client" in {e.item_id for e in remove_group.entries}
        assert "flatpak:ref:user:com.spotify.Client" not in {e.item_id for e in install_group.entries}

        # Identical application/version/scope on both -> no diff at all.
        assert "flatpak:ref:user:org.gnome.Podcasts" not in by_id

        # Remote missing on target (system-scope flathub) -> its own add diff.
        assert by_id["flatpak:remote:system:flathub"].action == DiffAction.INSTALL
        assert "flatpak:remote:user:flathub" not in by_id  # identical on both -> no diff

    @pytest.mark.asyncio
    async def test_flathub_present_in_both_scopes_yields_two_remote_items(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, "", ""),
                "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                "flatpak remotes --system --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
            },
            target_responses={"flatpak list --app": CommandResult(0, "", "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        remote_ids = {diff.item_id for diff in plan.diffs if diff.item_class == ItemClass.FLATPAK_REMOTE}
        assert remote_ids == {"flatpak:remote:user:flathub", "flatpak:remote:system:flathub"}

    @pytest.mark.asyncio
    async def test_every_remote_diff_precedes_every_ref_diff(self) -> None:
        context, _source, _target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        remote_indices = [i for i, d in enumerate(plan.diffs) if d.item_class == ItemClass.FLATPAK_REMOTE]
        ref_indices = [i for i, d in enumerate(plan.diffs) if d.item_class == ItemClass.FLATPAK_REF]
        assert remote_indices
        assert ref_indices
        assert max(remote_indices) < min(ref_indices)


class TestPlanReadOnly:
    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_flatpak_command(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)

        await job.plan()

        for cmd in all_calls(target):
            assert "flatpak install" not in cmd
            assert "flatpak uninstall" not in cmd
            assert "remote-add" not in cmd
            assert "remote-delete" not in cmd


class TestConverge:
    @pytest.mark.asyncio
    async def test_remotes_converge_before_refs_that_depend_on_them(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        applicable = [
            diff
            for diff in plan.diffs
            if diff.action != DiffAction.REPORT_ONLY and diff.item_id != "flatpak:ref:user:org.example.NeedsRemote"
        ]
        for diff in applicable:
            await job.converge(diff)

        commands = all_calls(target)
        remote_add_idx = next(i for i, c in enumerate(commands) if "remote-add" in c)
        slack_install_idx = next(
            i for i, c in enumerate(commands) if "flatpak install" in c and "com.slack.Slack" in c
        )
        assert remote_add_idx < slack_install_idx

    @pytest.mark.asyncio
    async def test_user_scope_ref_install_has_no_sudo_and_carries_user_flag(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:org.example.SplitScope")

        await job.converge(diff)

        commands = all_calls(target)
        install_cmd = next(c for c in commands if "flatpak install" in c and "org.example.SplitScope" in c)
        assert "--user" in install_cmd
        assert "sudo" not in install_cmd

    @pytest.mark.asyncio
    async def test_system_scope_ref_install_uses_sudo_and_system_flag(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        remote_diff = next(d for d in plan.diffs if d.item_id == "flatpak:remote:system:flathub")
        ref_diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:system:com.slack.Slack")

        await job.converge(remote_diff)
        await job.converge(ref_diff)

        commands = all_calls(target)
        install_cmd = next(c for c in commands if "flatpak install" in c and "com.slack.Slack" in c)
        assert "--system" in install_cmd
        assert install_cmd.startswith("sudo ")

    @pytest.mark.asyncio
    async def test_ref_removal_never_needs_source_lookup(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:com.spotify.Client")

        await job.converge(diff)

        commands = all_calls(target)
        assert any("flatpak uninstall -y --user com.spotify.Client" in c for c in commands)

    @pytest.mark.asyncio
    async def test_ref_with_missing_origin_remote_is_skipped_with_named_failure(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:org.example.NeedsRemote")

        with pytest.raises(ConvergeItemFailed, match="customremote"):
            await job.converge(diff)

        assert not any("customremote" in c for c in all_calls(target) if "flatpak install" in c)


class TestExcludePaths:
    def test_returns_flatpak_data_dir_excludes_var_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        paths = flatpak_sync_exclude_paths()

        assert paths == [tmp_path / ".local" / "share" / "flatpak"]
        assert not any(p == tmp_path / ".var" / "app" for p in paths)


class TestValidate:
    @pytest.mark.asyncio
    async def test_flatpak_unavailable_on_source_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak --version": CommandResult(127, "", "not found")}
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "flatpak is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_flatpak_unavailable_on_target_yields_validation_error_and_does_not_raise(self) -> None:
        context, _source, _target = make_context(
            target_responses={"flatpak --version": CommandResult(127, "", "not found")}
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "flatpak is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_with_no_system_scope_items_yields_no_errors(self) -> None:
        context, _source, _target = make_context()
        job = FlatpakSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []

    @pytest.mark.asyncio
    async def test_system_scope_item_present_without_sudo_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, "com.slack.Slack\t1.0\tflathub\tsystem\n", "")},
            target_responses={"sudo -n true": CommandResult(1, "", "sudo: a password is required")},
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_user_scope_only_never_checks_sudo(self) -> None:
        context, _source, target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, "org.example.App\t1.0\tflathub\tuser\n", "")}
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert errors == []
        assert not any("sudo -n true" in c for c in all_calls(target))


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_flatpak_sync_to_flatpak_sync_job(self) -> None:
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("flatpak_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is FlatpakSyncJob


class _StubAptLikeSiblingJob(PackageSyncJob):
    """A minimal `apt_sync`-shaped sibling: enough to drive `PackagePhaseCoordinator`
    alongside a real `FlatpakSyncJob`, without depending on `AptSyncJob` itself.
    """

    name: ClassVar[str] = "stub_apt_like"
    manager_id: ClassVar[str] = "apt"

    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        return []

    async def query_target_items(self) -> Sequence[AptPackageItem]:
        return []

    async def converge(self, diff: ItemDiff) -> CommandResult:
        raise NotImplementedError

    async def validate(self) -> list[ValidationError]:
        return []

    async def plan(self) -> PackagePlan:
        diff = ItemDiff(
            item_class=ItemClass.APT_PACKAGE,
            diff_class=DiffClass.MISSING_ON_TARGET,
            action=DiffAction.INSTALL,
            item_id="apt:package:stub-pkg",
            label="stub-pkg",
            detail=None,
        )
        groups = self._build_review_groups((diff,))
        return PackagePlan(manager=self.manager_id, diffs=(diff,), groups=groups)


class TestCoordinatorIntegration:
    @pytest.mark.asyncio
    async def test_accepted_outcome_contains_only_flatpak_prefixed_item_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _auto_apply(
            groups: Sequence[ReviewGroup], *, console: object, ui: object, logger: object = None
        ) -> ReviewOutcome:
            decisions = {entry.item_id: Decision.APPLY for group in groups for entry in group.entries}
            return ReviewOutcome(decisions=decisions, was_interactive=True)

        monkeypatch.setattr("pcswitcher.jobs.package_phase.review_items", AsyncMock(side_effect=_auto_apply))

        context, _source, _target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, "com.slack.Slack\t1.0\tflathub\tuser\n", "")},
            target_responses={"flatpak list --app": CommandResult(0, "", "")},
        )
        flatpak_job = FlatpakSyncJob(context)
        apt_job = _StubAptLikeSiblingJob(context)
        coordinator = PackagePhaseCoordinator(Console(file=io.StringIO()), MagicMock())

        await coordinator.run([apt_job, flatpak_job])

        outcome = flatpak_job._accepted_outcome  # pyright: ignore[reportPrivateUsage]
        assert outcome is not None
        assert outcome.decisions
        assert all(item_id.startswith("flatpak:") for item_id in outcome.decisions)
