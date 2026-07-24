"""Unit tests for FlatpakSyncJob: tab-separated `flatpak list`/`flatpak remotes`
parsing, the flatpak-specific plan()/diff pipeline, scope-as-identity, remote-before-
ref convergence ordering, and the missing-origin-remote skip guard.

All executor interactions are mocked; no real flatpak commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.flatpak_sync import FlatpakSyncJob, _parse_flatpak_masks, flatpak_sync_exclude_paths
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed
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


class TestRemoteUrlChange:
    """Decision 7: a remote present on both sides with the same name+scope but a
    DIFFERING URL is a CHANGE diff that converges the target to the source's URL via
    `flatpak remote-modify --url`, not a REMOVE+INSTALL churn and not silently ignored.
    """

    _SRC_URL = "https://dl.flathub.org/repo/"
    _TGT_URL = "https://old.mirror.example.org/repo/"

    def _responses(self, *, src_url: str, tgt_url: str) -> tuple[dict[str, CommandResult], dict[str, CommandResult]]:
        source = {
            "flatpak list --app": CommandResult(0, "", ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, f"flathub\t{src_url}\n", ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, "", ""),
        }
        target = {
            "flatpak list --app": CommandResult(0, "", ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, f"flathub\t{tgt_url}\n", ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, "", ""),
        }
        return source, target

    @pytest.mark.asyncio
    async def test_changed_url_yields_one_change_diff(self) -> None:
        source_responses, target_responses = self._responses(src_url=self._SRC_URL, tgt_url=self._TGT_URL)
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        remote_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE]
        assert len(remote_diffs) == 1
        change = remote_diffs[0]
        assert change.item_id == "flatpak:remote:user:flathub"
        assert change.action == DiffAction.CHANGE
        assert change.diff_class == DiffClass.VERSION_MISMATCH
        assert change.detail is not None
        assert self._SRC_URL in change.detail
        assert self._TGT_URL in change.detail

    @pytest.mark.asyncio
    async def test_changed_url_lands_in_default_ticked_change_group(self) -> None:
        source_responses, target_responses = self._responses(src_url=self._SRC_URL, tgt_url=self._TGT_URL)
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        change_group = next(g for g in plan.groups if g.action == "change")
        assert "flatpak:remote:user:flathub" in {e.item_id for e in change_group.entries}
        # A change is install-direction, not removal — it shares no group with removals.
        assert not any(g.action == "remove" for g in plan.groups)

    @pytest.mark.asyncio
    async def test_identical_url_yields_no_diff(self) -> None:
        source_responses, target_responses = self._responses(src_url=self._SRC_URL, tgt_url=self._SRC_URL)
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.FLATPAK_REMOTE for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_converge_uses_remote_modify_with_source_url_and_scope_flag(self) -> None:
        source_responses, target_responses = self._responses(src_url=self._SRC_URL, tgt_url=self._TGT_URL)
        context, _source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        change = next(d for d in plan.diffs if d.action == DiffAction.CHANGE)

        await job.converge(change)

        modify_cmd = next(c for c in all_calls(target) if "remote-modify" in c)
        assert "--user" in modify_cmd
        assert "sudo" not in modify_cmd
        assert f"--url={self._SRC_URL}" in modify_cmd
        assert modify_cmd.rstrip().endswith("flathub")
        # No delete+add churn: remote-modify is the only remote-mutating verb issued.
        assert not any("remote-delete" in c for c in all_calls(target))
        assert not any("remote-add" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_system_scope_url_change_uses_sudo_and_system_flag(self) -> None:
        source_responses = {
            "flatpak list --app": CommandResult(0, "", ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, "", ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, f"flathub\t{self._SRC_URL}\n", ""),
        }
        target_responses = {
            "flatpak list --app": CommandResult(0, "", ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, "", ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, f"flathub\t{self._TGT_URL}\n", ""),
        }
        context, _source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        change = next(d for d in plan.diffs if d.action == DiffAction.CHANGE)

        await job.converge(change)

        modify_cmd = next(c for c in all_calls(target) if "remote-modify" in c)
        assert modify_cmd.startswith("sudo ")
        assert "--system" in modify_cmd


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


class TestMaskParse:
    """`flatpak {--user|--system} mask` prints one pattern per line, each prefixed with
    two leading spaces and no header (RESEARCH: verified live, Flatpak 1.14.6) — parsed
    by stripping leading whitespace, unlike the tab-separated list commands.
    """

    def test_parses_two_leading_space_format_and_wildcard_patterns(self) -> None:
        output = (
            "  org.freedesktop.Platform.ffmpeg-full\n"
            "  app/com.example.Blocked/x86_64/*\n"
            "  runtime/org.gnome.*/x86_64/45\n"
        )

        items = _parse_flatpak_masks(output, "user")

        assert [item.pattern for item in items] == [
            "org.freedesktop.Platform.ffmpeg-full",
            "app/com.example.Blocked/x86_64/*",
            "runtime/org.gnome.*/x86_64/45",
        ]
        assert all(item.scope == "user" for item in items)

    def test_blank_lines_skipped_and_scope_is_the_passed_argument(self) -> None:
        output = "\n  org.example.Blocked\n\n"

        items = _parse_flatpak_masks(output, "system")

        assert [item.pattern for item in items] == ["org.example.Blocked"]
        assert items[0].scope == "system"
        assert items[0].item_id == "flatpak:mask:system:org.example.Blocked"

    def test_no_masks_yields_empty_list(self) -> None:
        assert _parse_flatpak_masks("", "user") == []


class TestMaskDiff:
    """Pure membership diff (#208, D-10): source-only -> INSTALL (mask), target-only ->
    REMOVE (unmask), present-both -> no diff. No CHANGE — a mask has no value to change.
    """

    @pytest.mark.asyncio
    async def test_source_user_mask_absent_on_target_yields_install(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, "  org.freedesktop.Platform.ffmpeg-full\n", "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        mask_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK]
        assert len(mask_diffs) == 1
        diff = mask_diffs[0]
        assert diff.item_id == "flatpak:mask:user:org.freedesktop.Platform.ffmpeg-full"
        assert diff.action == DiffAction.INSTALL
        assert diff.diff_class == DiffClass.MISSING_ON_TARGET

    @pytest.mark.asyncio
    async def test_target_only_system_mask_yields_removal(self) -> None:
        context, _source, _target = make_context(
            target_responses={"flatpak --system mask": CommandResult(0, "  org.example.Blocked\n", "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        mask_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK]
        assert len(mask_diffs) == 1
        diff = mask_diffs[0]
        assert diff.item_id == "flatpak:mask:system:org.example.Blocked"
        assert diff.action == DiffAction.REMOVE
        assert diff.diff_class == DiffClass.EXTRA_ON_TARGET
        # A removal lands in its own unticked removal group, never the install group.
        remove_group = next(g for g in plan.groups if g.action == "remove")
        assert "flatpak:mask:system:org.example.Blocked" in {e.item_id for e in remove_group.entries}

    @pytest.mark.asyncio
    async def test_mask_present_on_both_yields_no_diff(self) -> None:
        mask_line = "  org.example.Both\n"
        context, _source, _target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, mask_line, "")},
            target_responses={"flatpak --user mask": CommandResult(0, mask_line, "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.FLATPAK_MASK for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_masks_ordered_after_refs_in_diffs_tuple(self) -> None:
        # A ref install (source-only app) plus a mask install (source-only mask): the
        # mask diff must come AFTER the ref diff so it cannot suppress an auto-pulled
        # dependency of the ref being installed the same run (D-08).
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, "org.example.App\t1.0\tflathub\tuser\n", ""),
                "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                "flatpak --user mask": CommandResult(0, "  org.example.Blocked\n", ""),
            },
            target_responses={
                "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
            },
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        ref_indices = [i for i, d in enumerate(plan.diffs) if d.item_class == ItemClass.FLATPAK_REF]
        mask_indices = [i for i, d in enumerate(plan.diffs) if d.item_class == ItemClass.FLATPAK_MASK]
        assert ref_indices
        assert mask_indices
        assert max(ref_indices) < min(mask_indices)


class TestMaskConverge:
    """`[sudo] flatpak {--user|--system} mask [--remove] <pattern>` (#208, D-10): scope +
    pattern recovered from the item_id (no source-side lookup), sudo iff system scope.
    """

    @pytest.mark.asyncio
    async def test_user_scope_mask_install_runs_mask_without_sudo(self) -> None:
        pattern = "org.freedesktop.Platform.ffmpeg-full"
        context, _source, target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, f"  {pattern}\n", "")},
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK)

        await job.converge(diff)

        # The capture call is `flatpak --user mask` (no pattern); only converge carries
        # the pattern, so filtering by it uniquely selects the mutating command.
        mask_cmd = next(c for c in all_calls(target) if pattern in c)
        assert "--user" in mask_cmd
        assert "sudo" not in mask_cmd
        assert "--remove" not in mask_cmd
        assert mask_cmd.rstrip().endswith(pattern)

    @pytest.mark.asyncio
    async def test_system_scope_mask_removal_uses_sudo_and_remove_flag(self) -> None:
        pattern = "org.example.Blocked"
        context, _source, target = make_context(
            target_responses={"flatpak --system mask": CommandResult(0, f"  {pattern}\n", "")},
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK)

        await job.converge(diff)

        mask_cmd = next(c for c in all_calls(target) if pattern in c and "--remove" in c)
        assert mask_cmd.startswith("sudo ")
        assert "--system" in mask_cmd
        assert "--remove" in mask_cmd
        assert mask_cmd.rstrip().endswith(pattern)


class TestMaskSystemScopeGate:
    """A system-scope mask on either machine (#208, D-07) writes into `/var/lib/flatpak`
    just like a system remote, so it flips `_system_scope_in_play` and requires target
    sudo; a user-scope-only mask never does.
    """

    @pytest.mark.asyncio
    async def test_system_scope_mask_requires_target_sudo(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak --system mask": CommandResult(0, "  org.example.Blocked\n", "")},
            target_responses={"sudo -n true": CommandResult(1, "", "sudo: a password is required")},
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_user_scope_only_mask_never_checks_sudo(self) -> None:
        context, _source, target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, "  org.example.UserOnly\n", "")},
        )
        job = FlatpakSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []
        assert not any("sudo -n true" in c for c in all_calls(target))


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
