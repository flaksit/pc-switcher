"""Unit tests for `packages/state.py`'s machine-local decision store (plan 02-04).

Task 1 covers `DecisionFile`/`DecisionEntry`/`filter_inert` as standalone units, using
stub/fake `Executor`s — no real shell/SSH. Task 2 (`TestPipelineWiring` and
`TestConfigSyncScope` below) extends this file with pipeline-level assertions: inert
items absent from `PackageSyncJob.plan()`'s diffs, skip-always recorded on the correct
end of the connection in `apply()`, nothing recorded in dry-run or non-interactive runs,
and confirmation that `config_sync` never transfers a decision file.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from pcswitcher.config_sync import CONFIG_REMOTE_PATH, _copy_config_to_target  # pyright: ignore[reportPrivateUsage]
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages import state as package_state
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
)
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import COLLATERAL_REVIEW_ACTION, Decision, MarkSide, ReviewOutcome
from pcswitcher.jobs.packages.state import (
    DECISION_FILE_GLOB_RELPATH,
    DECISION_FILE_RELPATH_TEMPLATE,
    SNIPPET_REGISTRY_RELPATH,
    DecisionEntry,
    DecisionFile,
    Snippet,
    SnippetRegistry,
    VersionedSnippet,
    filter_inert,
)
from pcswitcher.jobs.packages.sync_core import PackagePlan
from pcswitcher.models import CommandResult, Host, SyncAborted
from tests.unit.jobs.test_package_sync_core import FakeItem, FakeSyncJob

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_context(*, dry_run: bool = False) -> JobContext:
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
    target = MagicMock()
    target.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
    return JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
    )


class FakeShellExecutor:
    """Interprets the small, fixed vocabulary of shell commands `DecisionFile` issues
    (`cat ... 2>/dev/null`, and the `mkdir --parents ... && printf '%s' ... > ... && mv --force ...`
    atomic-write shape), backed by an in-memory dict. Good enough to prove a genuine
    load()/record() round trip without shelling out to a real subprocess.
    """

    host: ClassVar[Host] = Host.SOURCE

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    async def run_command(self, cmd: str, timeout: float | None = None, **_: object) -> CommandResult:
        self.commands.append(cmd)
        if cmd.startswith("cat "):
            path = shlex.split(cmd.removeprefix("cat ").removesuffix(" 2>/dev/null"))[0]
            if path in self.files:
                return CommandResult(0, self.files[path], "")
            return CommandResult(1, "", "")

        tokens = shlex.split(cmd)
        printf_idx = tokens.index("printf")
        content = tokens[printf_idx + 2]  # tokens[printf_idx + 1] == "%s"
        redirect_idx = tokens.index(">", printf_idx)
        mv_idx = tokens.index("mv", redirect_idx)
        final_path = tokens[mv_idx + 3]  # "mv", "-f", <tmp>, <final>
        self.files[final_path] = content
        return CommandResult(0, "", "")

    async def terminate_all_processes(self) -> None:
        return None


def _entry(item_id: str = "fake:brscan3", reason: str | None = "printer driver") -> DecisionEntry:
    return DecisionEntry(
        item_id=item_id,
        item_class=ItemClass.APT_PACKAGE,
        label="brscan3 (0.4.11-2)",
        reason=reason,
        recorded_at="2026-07-22T09:14:03+00:00",
    )


def _decision_file_contents(item_id: str) -> str:
    return (
        f"machine_specific:\n  {item_id}:\n    item_class: apt_package\n"
        f"    label: {item_id}\n    reason: null\n    recorded_at: '2026-07-22T09:14:03+00:00'\n"
    )


def _respond_cat_with(content: str) -> Callable[..., CommandResult]:
    """A `run_command` side_effect returning `content` for any `cat ...` decision-file
    read and an empty success for everything else."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if cmd.startswith("cat "):
            return CommandResult(0, content, "")
        return CommandResult(0, "", "")

    return _side_effect


def _respond_echo_home(home: str) -> Callable[..., CommandResult]:
    """A `run_command` side_effect answering `echo $HOME` and succeeding empty otherwise."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if cmd == "echo $HOME":
            return CommandResult(0, home, "")
        return CommandResult(0, "", "")

    return _side_effect


# ---------------------------------------------------------------------------
# filter_inert
# ---------------------------------------------------------------------------


class TestFilterInert:
    @pytest.mark.asyncio
    async def test_drops_items_whose_id_is_in_decisions(self) -> None:
        items = [FakeItem(name="brscan3"), FakeItem(name="vim")]
        decisions = {"fake:brscan3": _entry()}

        result = await filter_inert(items, decisions)

        assert [item.name for item in result] == ["vim"]

    @pytest.mark.asyncio
    async def test_empty_decisions_keeps_every_item(self) -> None:
        items = [FakeItem(name="vim")]

        result = await filter_inert(items, {})

        assert result == items

    @pytest.mark.asyncio
    async def test_no_items_match_returns_all_unchanged_in_order(self) -> None:
        items = [FakeItem(name="a"), FakeItem(name="b")]
        decisions = {"fake:unrelated": _entry(item_id="fake:unrelated")}

        result = await filter_inert(items, decisions)

        assert result == items


# ---------------------------------------------------------------------------
# DecisionFile.load()
# ---------------------------------------------------------------------------


class TestDecisionFileLoad:
    @pytest.mark.asyncio
    async def test_absent_file_returns_empty_mapping(self) -> None:
        """H147, H133 — an absent file reads as "no permanent decisions", which is what a machine
        that has never been synced holds.
        """
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(1, "", ""))
        store = DecisionFile("apt", executor)

        entries = await store.load()

        assert entries == {}

    @pytest.mark.asyncio
    async def test_absent_file_logs_nothing_above_full(self, caplog: pytest.LogCaptureFixture) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(1, "", ""))
        store = DecisionFile("apt", executor)

        with caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.packages.state"):
            await store.load()

        assert caplog.records == []

    @pytest.mark.asyncio
    async def test_empty_file_returns_empty_mapping(self) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        store = DecisionFile("apt", executor)

        assert await store.load() == {}

    @pytest.mark.asyncio
    async def test_malformed_yaml_returns_empty_mapping_and_warns_naming_the_path(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(
            return_value=CommandResult(0, "machine_specific: [\n  - unterminated: true\n", "")
        )
        store = DecisionFile("apt", executor)

        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.packages.state"):
            entries = await store.load()

        assert entries == {}
        assert len(caplog.records) == 1
        assert "apt.decisions.yaml" in caplog.records[0].message

    @pytest.mark.asyncio
    async def test_missing_machine_specific_key_treated_as_malformed(self) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "unrelated: true\n", ""))
        store = DecisionFile("apt", executor)

        assert await store.load() == {}

    @pytest.mark.asyncio
    async def test_well_formed_file_round_trips_item_id_and_reason(self) -> None:
        shell = FakeShellExecutor()
        writer = DecisionFile("apt", shell)
        await writer.record(_entry())

        reader = DecisionFile("apt", shell)
        entries = await reader.load()

        assert set(entries) == {"fake:brscan3"}
        assert entries["fake:brscan3"].reason == "printer driver"
        assert entries["fake:brscan3"].item_class == ItemClass.APT_PACKAGE


# ---------------------------------------------------------------------------
# DecisionFile.record()
# ---------------------------------------------------------------------------


class TestDecisionFileRecord:
    @pytest.mark.asyncio
    async def test_write_is_atomic_temp_then_move(self) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        store = DecisionFile("apt", executor)

        await store.record(_entry())

        cmd = executor.run_command.call_args.args[0]
        assert "mkdir --parents" in cmd
        assert ".pcswitcher-tmp" in cmd
        assert "mv --force" in cmd
        assert cmd.index("mkdir --parents") < cmd.index(".pcswitcher-tmp") < cmd.index("mv --force")

    @pytest.mark.asyncio
    async def test_source_held_write_uses_source_executor_and_leaves_target_untouched(self) -> None:
        source_executor = MagicMock()
        source_executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        target_executor = MagicMock()
        target_executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))

        await DecisionFile("apt", source_executor).record(_entry())

        assert source_executor.run_command.call_count >= 1
        assert target_executor.run_command.call_count == 0

    @pytest.mark.asyncio
    async def test_target_held_write_uses_target_executor_and_leaves_source_untouched(self) -> None:
        source_executor = MagicMock()
        source_executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        target_executor = MagicMock()
        target_executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))

        await DecisionFile("apt", target_executor).record(_entry())

        assert target_executor.run_command.call_count >= 1
        assert source_executor.run_command.call_count == 0

    @pytest.mark.asyncio
    async def test_target_side_write_issues_no_local_filesystem_write(self) -> None:
        """The write travels entirely through the executor; nothing here ever opens a
        local file, which is what makes this method correct for BOTH roles (`PKG-FR-MACHINE-SPECIFIC`)."""
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        store = DecisionFile("apt", executor)

        with patch("builtins.open", side_effect=AssertionError("record() must not touch the local filesystem")):
            await store.record(_entry())

        # record() reads (load()) then writes: two run_command calls, zero local opens.
        assert executor.run_command.await_count == 2

    @pytest.mark.asyncio
    async def test_header_is_prose_the_user_can_read(self) -> None:
        """The header ships to every user's config directory and is read by people, not by
        the tool, so a module symbol running into a sentence there is a defect (`filter_inert`
        once did, mid-word).
        """
        shell = FakeShellExecutor()

        await DecisionFile("apt", shell).record(_entry())

        comments = [line for line in next(iter(shell.files.values())).splitlines() if line.startswith("#")]
        assert "# This file is machine-local and is never synced to any peer. Remove" in comments
        assert not any("filter_inert" in line for line in comments)

    @pytest.mark.asyncio
    async def test_recording_same_item_id_twice_does_not_duplicate(self) -> None:
        shell = FakeShellExecutor()
        store = DecisionFile("apt", shell)

        await store.record(_entry(reason="first reason"))
        await store.record(_entry(reason="second reason"))

        entries = await DecisionFile("apt", shell).load()
        assert len(entries) == 1
        assert entries["fake:brscan3"].reason == "second reason"

    @pytest.mark.asyncio
    async def test_recording_a_second_distinct_item_preserves_the_first(self) -> None:
        shell = FakeShellExecutor()
        store = DecisionFile("apt", shell)

        await store.record(_entry(item_id="fake:brscan3"))
        await store.record(_entry(item_id="apt:package:some-vendor-tool"))

        entries = await DecisionFile("apt", shell).load()
        assert set(entries) == {"fake:brscan3", "apt:package:some-vendor-tool"}


# ---------------------------------------------------------------------------
# Path/glob relpath construction — the store and folder_sync's exclusion share this.
# ---------------------------------------------------------------------------


class TestRelpathConstants:
    def test_relpath_template_places_file_under_config_pc_switcher(self) -> None:
        """H132."""
        assert DECISION_FILE_RELPATH_TEMPLATE.format(manager="apt") == ".config/pc-switcher/apt.decisions.yaml"

    def test_glob_relpath_covers_every_manager_with_one_pattern(self) -> None:
        assert DECISION_FILE_GLOB_RELPATH == ".config/pc-switcher/*.decisions.yaml"

    def test_no_default_machine_specific_package_hardcoded(self) -> None:
        """`PKG-FR-MARK-SIDE`: no default entry lives in Python — grep-verifiable, mirrors the plan's
        own acceptance criterion."""
        source = package_state.__file__
        assert source is not None
        content = Path(source).read_text(encoding="utf-8")
        assert "brscan3" not in content
        assert "brother-udev" not in content


# ---------------------------------------------------------------------------
# Task 2: pipeline wiring — inert items never reach the review, skip-always is
# recorded on the correct end, never in dry-run or a non-interactive outcome.
# ---------------------------------------------------------------------------


def _remove_diff(item_id: str) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REMOVE,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


# A private repository's address carries its credential inside itself, so the URL IS the
# secret (`PKG-FR-CREDENTIAL-PRIVACY`).
_CREDENTIALED_URL = "https://bearer:s3cr3t-token@packages.example.com/apt"


def _install_diff(item_id: str) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.INSTALL,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


def _change_diff(item_id: str) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.VERSION_MISMATCH,
        action=DiffAction.CHANGE,
        item_id=item_id,
        label=item_id,
        detail="1.0 -> 2.0",
    )


class TestPipelineWiring:
    @pytest.mark.asyncio
    async def test_source_held_inert_item_absent_from_the_plans_diffs(self) -> None:
        """H125."""
        context = make_context()
        source = context.source
        source.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_decision_file_contents("fake:brscan3"))
        )
        job = FakeSyncJob(
            context,
            source_items=[FakeItem(name="brscan3"), FakeItem(name="vim")],
        )

        plan = await job.plan()

        assert {d.item_id for d in plan.diffs} == {"fake:vim"}
        all_group_item_ids = {entry.item_id for group in plan.groups for entry in group.entries}
        assert "fake:brscan3" not in all_group_item_ids

    @pytest.mark.asyncio
    async def test_target_held_inert_item_absent_even_though_source_also_differs(self) -> None:
        """H126, N3."""
        context = make_context()
        target = context.target
        target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_decision_file_contents("fake:legacy-tool"))
        )
        job = FakeSyncJob(context, target_items=[FakeItem(name="legacy-tool")])

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_plan_issues_no_decision_file_write(self) -> None:
        """H6."""
        context = make_context()
        job = FakeSyncJob(context, source_items=[FakeItem(name="vim")])

        await job.plan()

        for cmd in [call.args[0] for call in context.source.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv --force" not in cmd
        for cmd in [call.args[0] for call in context.target.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv --force" not in cmd

    @pytest.mark.asyncio
    async def test_every_record_call_originates_from_apply_not_plan(self) -> None:
        context = make_context()
        job = FakeSyncJob(context, source_items=[FakeItem(name="vim")])
        plan = await job.plan()
        job.accept_review(plan, ReviewOutcome(decisions={"fake:vim": Decision.APPLY}, was_interactive=True))

        with patch.object(DecisionFile, "record", new=AsyncMock()) as record_mock:
            await job.apply()

        record_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skip_always_on_remove_writes_to_target_not_source(self) -> None:
        """H120, N3."""
        context = make_context()
        job = FakeSyncJob(context)
        diff = _remove_diff("fake:legacy-tool")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        target_cmds = [call.args[0] for call in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        source_cmds = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv --force" in cmd for cmd in target_cmds)
        assert not any("mv --force" in cmd for cmd in source_cmds)

    @pytest.mark.asyncio
    async def test_skip_always_on_install_writes_to_source_not_target(self) -> None:
        """H118, J146, N1."""
        context = make_context()
        job = FakeSyncJob(context)
        diff = _install_diff("fake:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        target_cmds = [call.args[0] for call in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        source_cmds = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv --force" in cmd for cmd in source_cmds)
        assert not any("mv --force" in cmd for cmd in target_cmds)

    @pytest.mark.asyncio
    async def test_skip_always_on_change_writes_to_target_not_source(self) -> None:
        """H119, J4 — an outcome carrying no side answer records on the TARGET, which is the
        copy the batch screen's own permanent answer names. Sibling of the INSTALL and
        REMOVE cases above; the answered sides are the three tests below.
        """
        context = make_context()
        job = FakeSyncJob(context)
        diff = _change_diff("fake:drifting-tool")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        target_cmds = [call.args[0] for call in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        source_cmds = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv --force" in cmd for cmd in target_cmds)
        assert not any("mv --force" in cmd for cmd in source_cmds)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("side", "writes_on_source", "writes_on_target"),
        [
            (MarkSide.SOURCE, True, False),
            (MarkSide.TARGET, False, True),
            (MarkSide.BOTH, True, True),
        ],
    )
    async def test_a_conflicting_items_mark_lands_on_the_side_the_user_named(
        self, side: MarkSide, writes_on_source: bool, writes_on_target: bool
    ) -> None:
        """H250, H251, H252 — both machines have the item, so the review's follow-up decides
        whose file gets the entry; "both" writes one on each, and each dies with its own
        machine's copy (`PKG-FR-MARK-LIFETIME`).
        """
        context = make_context()
        job = FakeSyncJob(context)
        diff = _change_diff("fake:drifting-tool")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={diff.item_id: Decision.SKIP_ALWAYS},
                was_interactive=True,
                mark_sides={diff.item_id: side},
            ),
        )

        await job.apply()

        target_cmds = [call.args[0] for call in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        source_cmds = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv --force" in cmd for cmd in source_cmds) is writes_on_source
        assert any("mv --force" in cmd for cmd in target_cmds) is writes_on_target

    @pytest.mark.asyncio
    async def test_a_side_answer_cannot_move_an_installs_mark(self) -> None:
        """H253 — only the source has an install's item, so no answer relocates its mark; a
        side reaching one is an id the follow-up never offered.
        """
        context = make_context()
        job = FakeSyncJob(context)
        diff = _install_diff("fake:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={diff.item_id: Decision.SKIP_ALWAYS},
                was_interactive=True,
                mark_sides={diff.item_id: MarkSide.BOTH},
            ),
        )

        await job.apply()

        target_cmds = [call.args[0] for call in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        source_cmds = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv --force" in cmd for cmd in source_cmds)
        assert not any("mv --force" in cmd for cmd in target_cmds)

    @pytest.mark.asyncio
    async def test_a_marked_change_is_inert_whichever_machine_the_next_run_reads(self) -> None:
        """H178 — the mark sits on ONE machine and the roles swap with the direction of the
        run, so a change is read back from either machine's file.

        Both halves in one test, because a read that consults one file passes half of it
        whichever file it picks: the same recorded content is put on the source in one run
        and on the target in the other, and the diff must be dropped both times.
        """
        diff = _change_diff("fake:drifting-tool")
        recorded = _decision_file_contents(diff.item_id)

        for held_by_source in (True, False):
            context = make_context()
            executor = context.source if held_by_source else context.target
            executor.run_command = AsyncMock(side_effect=_respond_cat_with(recorded))  # pyright: ignore[reportAttributeAccessIssue]
            job = FakeSyncJob(context)

            kept = job._drop_inert_diffs(  # pyright: ignore[reportPrivateUsage]
                [diff],
                await DecisionFile(job.manager_id, context.source).load(),
                await DecisionFile(job.manager_id, context.target).load(),
            )

            assert kept == (), f"the mark was ignored when it sat on the {'source' if held_by_source else 'target'}"

    @pytest.mark.asyncio
    async def test_an_item_both_machines_have_is_filtered_off_both_manifests(self) -> None:
        """H179 — a mark on one machine must take that item out of BOTH inventories.

        Dropping it from its own machine's alone leaves the other machine's copy unmatched,
        and an unmatched copy is a one-sided item: an install of what the target already
        has, or a removal of what the source still has. The mark is placed on the TARGET,
        which is the holding machine for the change it was given on.
        """
        context = make_context()
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_decision_file_contents("fake:drifting-tool"))
        )
        job = FakeSyncJob(
            context,
            source_items=[FakeItem(name="drifting-tool")],
            target_items=[FakeItem(name="drifting-tool")],
        )

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_credentialed_label_is_written_to_the_file_withheld(self) -> None:
        """J127 — `PKG-FR-CREDENTIAL-PRIVACY`: the label a permanent decision keeps on disk is a
        string the user reads back, so the file gets the address without its userinfo.

        Followed into the payload of the write itself rather than stopping at the `ItemDiff`
        it is built from: the decision file is the one place a redacted string outlives the
        run that produced it.
        """
        context = make_context()
        job = FakeSyncJob(context)
        diff = _install_diff("fake:vendor-tool")
        labelled = ItemDiff(
            item_class=diff.item_class,
            diff_class=diff.diff_class,
            action=diff.action,
            item_id=diff.item_id,
            label=f"vendor-tool ({_CREDENTIALED_URL})",
        )
        plan = PackagePlan(manager="fake", diffs=(labelled,), groups=())
        job.accept_review(
            plan, ReviewOutcome(decisions={labelled.item_id: Decision.SKIP_ALWAYS}, was_interactive=True)
        )

        await job.apply()

        writes = [
            call.args[0]
            for call in context.source.run_command.call_args_list  # pyright: ignore[reportAttributeAccessIssue]
            if "mv --force" in call.args[0]
        ]
        assert len(writes) == 1
        assert "s3cr3t-token" not in writes[0]
        assert "https://***@packages.example.com/apt" in writes[0]

    @pytest.mark.asyncio
    async def test_no_record_call_when_dry_run(self) -> None:
        """H134, J55."""
        context = make_context(dry_run=True)
        job = FakeSyncJob(context)
        diff = _remove_diff("fake:legacy-tool")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        for cmd in [call.args[0] for call in context.target.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv --force" not in cmd

    @pytest.mark.asyncio
    async def test_no_record_call_when_outcome_was_not_interactive(self) -> None:
        """J12, J44."""
        context = make_context()
        job = FakeSyncJob(context)
        diff = _remove_diff("fake:legacy-tool")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=False))

        await job.apply()

        for cmd in [call.args[0] for call in context.target.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv --force" not in cmd


class TestHandEditedDecisionFile:
    """H18: the un-mark workflow docs/jobs/package-sync.md promises — "delete its entry
    from the decision file (or delete the whole file) ... the next sync treats the item as
    live again". The file IS the state: `plan()` re-reads it every run and feeds it to
    `filter_inert`, so nothing remembers an entry the user removed by hand.
    """

    @pytest.mark.asyncio
    async def test_entry_deleted_by_hand_makes_that_item_live_again_next_run(self) -> None:
        """H145."""
        shell = FakeShellExecutor()
        # The manager name only picks the path; the fake read below answers any `cat`.
        store = DecisionFile("apt", shell)
        await store.record(_entry(item_id="fake:brscan3"))
        await store.record(_entry(item_id="fake:legacy-tool"))
        recorded = next(iter(shell.files.values()))

        # The user opens the file and deletes ONE entry, leaving the other in place.
        data: dict[str, dict[str, object]] = yaml.safe_load(recorded)
        del data["machine_specific"]["fake:brscan3"]
        hand_edited = yaml.safe_dump(data)

        context = make_context()
        context.source.run_command = AsyncMock(side_effect=_respond_cat_with(hand_edited))  # pyright: ignore[reportAttributeAccessIssue]
        job = FakeSyncJob(
            context,
            source_items=[
                FakeItem(name="brscan3"),
                FakeItem(name="legacy-tool"),
                FakeItem(name="vim"),
            ],
        )

        plan = await job.plan()

        # brscan3 is live again; the entry the user kept stays inert.
        assert {d.item_id for d in plan.diffs} == {"fake:brscan3", "fake:vim"}

    @pytest.mark.asyncio
    async def test_deleting_the_whole_file_makes_every_item_live_again(self) -> None:
        """H146 — The coarse half of the same workflow: an absent file degrades to "no decisions"
        (H13), so removing it re-offers every previously-recorded item."""
        context = make_context()
        context.source.run_command = AsyncMock(return_value=CommandResult(1, "", ""))  # pyright: ignore[reportAttributeAccessIssue]
        job = FakeSyncJob(
            context,
            source_items=[FakeItem(name="brscan3"), FakeItem(name="vim")],
        )

        plan = await job.plan()

        assert {d.item_id for d in plan.diffs} == {"fake:brscan3", "fake:vim"}


def _respond_by_substring(mapping: dict[str, CommandResult]) -> Callable[..., CommandResult]:
    """A `run_command` side_effect matching commands by substring, first match wins, with
    an empty success as the fallback (the shape `test_apt_sync.py` uses)."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return CommandResult(0, "", "")

    return _side_effect


def _apt_context(
    *, source_responses: dict[str, CommandResult], target_responses: dict[str, CommandResult]
) -> JobContext:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=_respond_by_substring(source_responses))
    target = MagicMock()
    target.run_command = AsyncMock(side_effect=_respond_by_substring(target_responses))
    return JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
    )


# The source's own `apt-cache policy` answer about its manual set. A source apt that
# prints nothing is a broken probe, not a machine with unusual packages (`ProbeFailed`),
# so these fixtures state the answer a real source gives. The origin is the distribution
# archive, which keeps every package exempt from the `PKG-FR-APT-ORIGIN-VERIFY` origin check.
_SOURCE_SCAN_CMD = "-exec awk"
# The `ubuntu.sources` that makes the archive above a DISTRIBUTION origin, so `pkg-a` stays
# exempt from the `PKG-FR-APT-ORIGIN-VERIFY` origin check and remains an ordinary install.
_SOURCE_SCAN_UBUNTU = "/etc/apt/sources.list.d/ubuntu.sources\tURIs: http://ftp.belnet.be/ubuntu\n"

_SOURCE_POLICY_PKG_A = (
    "pkg-a:\n  Installed: 1.0\n  Candidate: 1.0\n  Version table:\n *** 1.0 500\n"
    "        500 http://ftp.belnet.be/ubuntu noble/main amd64 Packages\n"
    "        100 /var/lib/dpkg/status\n"
)

# The TARGET's answer for the same name: not installed, but offered. A target that answers
# nothing has never heard of `pkg-a`, and a real `apt-get --dry-run install pkg-a` on it
# exits 100 rather than rehearsing the transaction these tests are about.
_TARGET_POLICY_PKG_A = (
    "pkg-a:\n  Installed: (none)\n  Candidate: 1.0\n  Version table:\n     1.0 500\n"
    "        500 http://ftp.belnet.be/ubuntu noble/main amd64 Packages\n"
)


class TestDecisionScopeReachesCollateral:
    """D20 / decision 8: a machine-local decision makes an item inert in the DIFF — and,
    per `PKG-FR-COLLATERAL-MARKED`, protects it from collateral as well. The two inputs are
    independent: the TARGET's `apt-mark showmanual` set (`PKG-FR-COLLATERAL-MANUAL`) and that machine's
    own marks, either of which alone protects a package.

    Both tests share one decision file and differ only in the target's manual set, which is
    what makes that independence visible.
    """

    _DECISIONS = _decision_file_contents("apt:package:ghost-tool")

    @pytest.mark.asyncio
    async def test_a_mark_protects_a_package_apt_considers_auto_installed(self) -> None:
        """D43, H129."""
        context = _apt_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _SOURCE_POLICY_PKG_A, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _SOURCE_SCAN_UBUNTU, ""),
            },
            target_responses={
                # ghost-tool is recorded machine-specific below and is manual on NEITHER
                # machine — apt considers it an auto-installed package, so the mark is the
                # only thing standing between it and a silent removal.
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _TARGET_POLICY_PKG_A, ""),
                "apt.decisions.yaml": CommandResult(0, self._DECISIONS, ""),
                "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\nRemv ghost-tool [1.0]\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = [d for d in plan.diffs if d.item_id == "apt:collateral:install:remove:ghost-tool"]
        assert len(collateral) == 1
        assert collateral[0].detail is not None
        assert "marked as target-host's own" in collateral[0].detail
        assert [g for g in plan.groups if g.action == COLLATERAL_REVIEW_ACTION]
        assert "apt:package:pkg-a" in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_manual_set_membership_protects_the_same_item_on_its_own(self) -> None:
        """D45 — The other input, alone: the SAME recorded item is also protected by being in the
        target's manual set. Either source of protection is sufficient.
        """
        context = _apt_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _SOURCE_POLICY_PKG_A, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _SOURCE_SCAN_UBUNTU, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "ghost-tool\n", ""),
                "dpkg-query": CommandResult(0, "ghost-tool\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _TARGET_POLICY_PKG_A, ""),
                "apt.decisions.yaml": CommandResult(0, self._DECISIONS, ""),
                "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\nRemv ghost-tool [1.0]\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs if d.item_id == "apt:collateral:install:remove:ghost-tool"] == [
            "apt:collateral:install:remove:ghost-tool"
        ]
        # The decision file still does its own job: no removal diff for the inert item.
        assert not [d for d in plan.diffs if d.item_id == "apt:package:ghost-tool"]


# ---------------------------------------------------------------------------
# config_sync never transfers a decision file (`PKG-FR-MACHINE-SPECIFIC`) — verified, not assumed.
# ---------------------------------------------------------------------------


class TestConfigSyncScope:
    @pytest.mark.asyncio
    async def test_copy_config_to_target_sends_only_config_yaml(self, tmp_path: Path) -> None:
        """H131, H133 — the other route between machines carries no decision file either."""
        source_path = tmp_path / "config.yaml"
        source_path.write_text("logging: {}\n")

        target = MagicMock()
        target.run_command = AsyncMock(side_effect=_respond_echo_home("/home/alice"))
        target.send_file = AsyncMock()

        await _copy_config_to_target(target, source_path, "Nomad")

        assert target.send_file.await_count == 1
        remote_path = target.send_file.call_args.args[1]
        assert remote_path.endswith("config.yaml")
        assert "decisions" not in remote_path
        assert CONFIG_REMOTE_PATH.endswith("/config.yaml")


# ---------------------------------------------------------------------------
# SnippetRegistry — the shared, synced counterpart to DecisionFile (`PKG-FR-SNIPPET-VERBATIM`,
# `PKG-FR-MANUAL-SAME-RUN`).
# ---------------------------------------------------------------------------


class TestSnippetRegistry:
    def test_relpath_places_file_under_config_pc_switcher(self) -> None:
        assert SNIPPET_REGISTRY_RELPATH == ".config/pc-switcher/package-snippets.yaml"

    @pytest.mark.asyncio
    async def test_absent_file_returns_empty_mapping(self) -> None:
        """G64 — an absent registry reads as "no snippets"."""
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(1, "", ""))
        registry = SnippetRegistry(executor)

        assert await registry.load() == {}

    @pytest.mark.asyncio
    async def test_empty_file_returns_empty_mapping(self) -> None:
        """G64 — an empty registry reads as "no snippets", with no warning."""
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        registry = SnippetRegistry(executor)

        assert await registry.load() == {}

    @pytest.mark.asyncio
    async def test_a_registry_that_cannot_be_parsed_ends_the_run_naming_the_file(self) -> None:
        """G95 — a registry that is there and unreadable is not "no snippets": the run ends,
        naming the file, the machine holding it and what to do about it."""
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "snippets: [\n  - broken\n", ""))
        registry = SnippetRegistry(executor, "nomad")

        with pytest.raises(SyncAborted) as exc_info:
            await registry.load()

        message = str(exc_info.value)
        assert "package-snippets.yaml" in message
        assert "nomad" in message
        assert "start a new sync" in message

    @pytest.mark.asyncio
    async def test_every_malformed_entry_in_one_registry_is_named_at_once(self) -> None:
        """G115, G181, G190 — the repair is a hand edit of one file, so the ending names every
        entry that edit has to cover: stopping at the first would have the user fix it, start
        a new sync, and only then be shown the next. Every way an entry can be malformed
        counts, including both ways the wrong set of bodies does.
        """
        raw = (
            "snippets:\n"
            "  unreproducible:apt-no-candidate:one:\n"
            "    label: One\n"
            "  unreproducible:apt-no-candidate:two: not-a-mapping\n"
            "  unreproducible:apt-no-candidate:three:\n"
            "    label: Three\n"
            "    install_body: echo three\n"
            "    version_body: echo v\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: atlas\n"
            "  unreproducible:unowned-path:/opt/four:\n"
            "    label: Four\n"
            "    install_body: echo four\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: atlas\n"
            "  unreproducible:apt-no-candidate:five:\n"
            "    label: Five\n"
            "    install_body: echo five\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: atlas\n"
        )
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, raw, ""))

        with pytest.raises(SyncAborted) as exc_info:
            await SnippetRegistry(executor, "nomad").load()

        message = str(exc_info.value)
        assert "unreproducible:apt-no-candidate:one (missing field 'install_body')" in message
        assert "unreproducible:apt-no-candidate:two (" in message
        # A version body on a kind whose package manager reports the version.
        assert "unreproducible:apt-no-candidate:three (has a version_body" in message
        # And an unowned path without the one body only it can answer with.
        assert "unreproducible:unowned-path:/opt/four (missing field 'version_body')" in message
        # The one entry that parses is never named as a problem.
        assert "unreproducible:apt-no-candidate:five" not in message

    @pytest.mark.asyncio
    async def test_add_then_get_round_trips_body_verbatim_including_whitespace(self) -> None:
        """G56 — a body written with leading indentation and blank lines between commands
        is stored and read back byte for byte, for both entry types."""
        shell = FakeShellExecutor()
        package_backed = Snippet(
            item_id="unreproducible:apt-no-candidate:brscan3",
            label="brscan3 (no apt candidate)",
            install_body="  sudo dpkg --install /tmp/brscan3.deb\n\nsudo apt-get install --fix-broken --assume-yes\n",
            authored_at="2026-07-23T09:00:00+00:00",
            authored_on="laptop",
        )
        unowned_path = VersionedSnippet(
            item_id="unreproducible:unowned-path:/opt/az",
            label="az (unowned in /opt)",
            install_body="  sudo /opt/az/install.sh\n\nsudo ln --symbolic /opt/az/bin/az /usr/local/bin/az\n",
            version_body="  /opt/az/bin/az --version\n",
            authored_at="2026-07-23T09:00:00+00:00",
            authored_on="laptop",
        )

        registry = SnippetRegistry(shell)
        await registry.add(package_backed)
        await registry.add(unowned_path)

        assert await registry.get(package_backed.item_id) == package_backed
        assert await registry.get(unowned_path.item_id) == unowned_path

    @pytest.mark.asyncio
    async def test_a_body_of_shell_metacharacters_is_stored_and_replayed_uninterpreted(self) -> None:
        """G60 — brackets, command substitution, backticks and quotes are the author's
        bytes, not something the tool reads: the body round-trips exactly and reaches the
        target as one quoted argument, with nothing expanded on the way."""
        shell = FakeShellExecutor()
        body = "sudo /opt/[bold]tool/install.sh --note=\"$(date)\" --tag=`hostname` --path='/opt/a b'"
        snippet = VersionedSnippet(
            item_id="unreproducible:unowned-path:/opt/tool",
            label="tool [red]v2 (unowned in /opt)",
            install_body=body,
            version_body="echo v",
            authored_at="2026-07-23T09:00:00+00:00",
            authored_on="laptop",
        )

        await SnippetRegistry(shell).add(snippet)
        reloaded = await SnippetRegistry(shell).get(snippet.item_id)

        assert reloaded is not None
        assert reloaded.install_body == body
        assert reloaded.label == snippet.label

        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        await SnippetRegistry(shell).replay(snippet.item_id, target)

        assert target.run_command.call_args.args[0] == f"bash -c {shlex.quote(body)}"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_an_unregistered_item(self) -> None:
        shell = FakeShellExecutor()

        assert await SnippetRegistry(shell).get("unreproducible:apt-no-candidate:missing") is None

    @pytest.mark.asyncio
    async def test_write_is_atomic_temp_then_move(self) -> None:
        """G63 — written aside, then moved into place, so a machine that dies mid-write
        never leaves the registry half written."""
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        registry = SnippetRegistry(executor)

        await registry.add(Snippet(item_id="x", label="x", install_body="echo hi", authored_at="t", authored_on="h"))

        cmd = executor.run_command.call_args.args[0]
        assert "mkdir --parents" in cmd
        assert ".pcswitcher-tmp" in cmd
        assert "mv --force" in cmd
        assert cmd.index("mkdir --parents") < cmd.index(".pcswitcher-tmp") < cmd.index("mv --force")

    @pytest.mark.asyncio
    async def test_add_preserves_an_unrelated_pre_existing_entry(self) -> None:
        """G62 — a second snippet for a different item accumulates rather than replacing."""
        shell = FakeShellExecutor()
        first = Snippet(item_id="a", label="a", install_body="echo a", authored_at="t", authored_on="h")
        second = Snippet(item_id="b", label="b", install_body="echo b", authored_at="t", authored_on="h")

        await SnippetRegistry(shell).add(first)
        await SnippetRegistry(shell).add(second)

        entries = await SnippetRegistry(shell).load()
        assert set(entries) == {"a", "b"}
        assert entries["a"].install_body == "echo a"

    @pytest.mark.asyncio
    async def test_replay_passes_body_as_one_quoted_argument_with_login_shell_false(self) -> None:
        """G57, J155 — replayed as one unit, as the SSH user, with nothing added around it: no
        elevation and no login shell."""
        shell = FakeShellExecutor()
        snippet = Snippet(
            item_id="x",
            label="x",
            install_body="echo hello world",
            authored_at="t",
            authored_on="h",
        )
        await SnippetRegistry(shell).add(snippet)

        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(0, "", ""))

        result = await SnippetRegistry(shell).replay("x", target)

        target.run_command.assert_called_once_with(
            "bash -c 'echo hello world'", login_shell=False, mutates="replay install snippet for x"
        )
        assert result.success

    @pytest.mark.asyncio
    async def test_replay_with_no_registered_snippet_returns_a_failed_result_not_a_raise(self) -> None:
        shell = FakeShellExecutor()
        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(0, "", ""))

        result = await SnippetRegistry(shell).replay("unreproducible:apt-no-candidate:missing", target)

        assert result.success is False
        target.run_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_exit_code_alone_decides_success(self) -> None:
        """G59 — a snippet that exits non-zero while printing nothing recognisable failed."""
        shell = FakeShellExecutor()
        snippet = Snippet(item_id="x", label="x", install_body="false", authored_at="t", authored_on="h")
        await SnippetRegistry(shell).add(snippet)

        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(1, "", "boom"))

        result = await SnippetRegistry(shell).replay("x", target)

        assert result.success is False
        assert result.stderr == "boom"


# ---------------------------------------------------------------------------
# A mark lives exactly as long as its item
# ---------------------------------------------------------------------------


class _PruningJob(FakeSyncJob):
    """A `FakeSyncJob` whose presence check is whatever the test says it is.

    Every real implementation answers by reading its own manager (`snap list`, dpkg, a
    `test -e`); what the shared pipeline does with that answer is the same either way, so
    these tests state the answer directly and assert the pipeline around it.
    """

    def __init__(
        self,
        context: JobContext,
        *,
        absent_on_source: tuple[str, ...] = (),
        absent_on_target: tuple[str, ...] = (),
        probe_fails: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(context, **kwargs)  # pyright: ignore[reportArgumentType]
        self._absent_on_source = frozenset(absent_on_source)
        self._absent_on_target = frozenset(absent_on_target)
        self._probe_fails = probe_fails
        self.events: list[str] = []

    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        self.events.append(f"observe:{'source' if on_source else 'target'}")
        if self._probe_fails:
            raise ProbeFailed("dpkg-query did not answer")
        return self._absent_on_source if on_source else self._absent_on_target

    async def converge(self, diff: ItemDiff) -> CommandResult:
        self.events.append(f"converge:{diff.item_id}")
        return await super().converge(diff)


def _two_entry_file(*item_ids: str) -> str:
    """A decision file holding one entry per id, so a test can assert which SURVIVED a
    rewrite rather than only that a write happened."""
    entries = "".join(
        f"  {item_id}:\n    item_class: apt_package\n    label: {item_id}\n"
        f"    reason: null\n    recorded_at: '2026-07-22T09:14:03+00:00'\n"
        for item_id in item_ids
    )
    return f"machine_specific:\n{entries}"


def _written_decision_files(mock: MagicMock) -> list[str]:
    """The content of every decision-file rewrite issued through `mock`, in order."""
    written: list[str] = []
    for call in mock.run_command.call_args_list:
        cmd = call.args[0]
        if "mv --force" not in cmd or "decisions.yaml" not in cmd:
            continue
        tokens = shlex.split(cmd)
        written.append(tokens[tokens.index("printf") + 2])
    return written


def _apply_ready(job: FakeSyncJob, diffs: tuple[ItemDiff, ...] = ()) -> None:
    """Put `job` in the state `apply()` expects, with nothing to decide."""
    job.accept_review(
        PackagePlan(manager=job.manager_id, diffs=diffs, groups=()),
        ReviewOutcome(decisions={}, was_interactive=True),
    )


class TestDeadMarksAreDropped:
    """A mark keeps the holding machine's own copy of an item; once that machine has no
    copy, the mark is taken out rather than left to silence the item for good.
    """

    @pytest.mark.asyncio
    async def test_a_mark_whose_item_left_the_machine_is_dropped_and_the_others_kept(self) -> None:
        """H181 — the file is rewritten without the dead entry, and with every live one."""
        context = make_context()
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:gone", "fake:still-here"))
        )
        job = _PruningJob(context, absent_on_target=("fake:gone",))
        _apply_ready(job)

        await job.apply()

        written = _written_decision_files(context.target)  # pyright: ignore[reportArgumentType]
        assert len(written) == 1
        assert "fake:gone" not in written[0]
        assert "fake:still-here" in written[0]

    @pytest.mark.asyncio
    async def test_a_file_with_nothing_dead_in_it_is_not_written_at_all(self) -> None:
        """H182 — reconciliation costs no write on the ordinary run."""
        context = make_context()
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:still-here"))
        )
        job = _PruningJob(context)
        _apply_ready(job)

        await job.apply()

        assert _written_decision_files(context.target) == []  # pyright: ignore[reportArgumentType]

    @pytest.mark.asyncio
    async def test_a_presence_check_that_does_not_answer_keeps_every_mark(self) -> None:
        """H183 — silence is not absence: a probe that went dark drops nothing."""
        context = make_context()
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:gone"))
        )
        job = _PruningJob(context, absent_on_target=("fake:gone",), probe_fails=True)
        _apply_ready(job)

        await job.apply()

        assert _written_decision_files(context.target) == []  # pyright: ignore[reportArgumentType]

    @pytest.mark.asyncio
    async def test_a_dry_run_drops_nothing(self) -> None:
        """H184 — a rehearsal leaves no trace, this write included."""
        context = make_context(dry_run=True)
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:gone"))
        )
        job = _PruningJob(context, absent_on_target=("fake:gone",))
        _apply_ready(job)

        await job.apply()

        assert _written_decision_files(context.target) == []  # pyright: ignore[reportArgumentType]

    @pytest.mark.asyncio
    async def test_both_machines_files_are_reconciled(self) -> None:
        """H185 — either machine can be the holder, so both are asked and both rewritten."""
        context = make_context()
        context.source.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:source-gone"))
        )
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:target-gone"))
        )
        job = _PruningJob(context, absent_on_source=("fake:source-gone",), absent_on_target=("fake:target-gone",))
        _apply_ready(job)

        await job.apply()

        assert "fake:source-gone" not in _written_decision_files(context.source)[0]  # pyright: ignore[reportArgumentType]
        assert "fake:target-gone" not in _written_decision_files(context.target)[0]  # pyright: ignore[reportArgumentType]

    @pytest.mark.asyncio
    async def test_the_presence_check_runs_after_the_converge_loop(self) -> None:
        """H186 — what this run's own changes removed counts, so the machine is asked once
        those changes have landed and not before."""
        context = make_context()
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:gone"))
        )
        diff = ItemDiff(
            item_class=ItemClass.APT_PACKAGE,
            diff_class=DiffClass.EXTRA_ON_TARGET,
            action=DiffAction.REMOVE,
            item_id="fake:doomed",
            label="doomed",
            detail=None,
        )
        job = _PruningJob(context, absent_on_target=("fake:gone",))
        job.accept_review(
            PackagePlan(manager=job.manager_id, diffs=(diff,), groups=()),
            ReviewOutcome(decisions={diff.item_id: Decision.APPLY}, was_interactive=True),
        )

        await job.apply()

        assert job.events.index("converge:fake:doomed") < job.events.index("observe:target")

    @pytest.mark.asyncio
    async def test_a_dead_mark_stops_silencing_its_item_in_the_same_run(self) -> None:
        """H187 — the run that notices a mark is dead already plans the item it named: the
        mark is left out of the mapping every filter consults, so the item is diffed."""
        context = make_context()
        context.source.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:brscan3"))
        )
        job = _PruningJob(context, absent_on_source=("fake:brscan3",), source_items=[FakeItem(name="brscan3")])

        plan = await job.plan()

        assert {diff.item_id for diff in plan.diffs} == {"fake:brscan3"}

    @pytest.mark.asyncio
    async def test_planning_writes_no_decision_file_however_dead_the_marks_are(self) -> None:
        """H188 — the plan is read-only; the file itself is rewritten at apply time."""
        context = make_context()
        context.source.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:brscan3"))
        )
        job = _PruningJob(context, absent_on_source=("fake:brscan3",))

        await job.plan()

        assert _written_decision_files(context.source) == []  # pyright: ignore[reportArgumentType]

    @pytest.mark.asyncio
    async def test_the_drop_is_logged_naming_the_item(self, caplog: pytest.LogCaptureFixture) -> None:
        """H189 — a mark is the user's own answer; it does not evaporate silently."""
        context = make_context()
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:gone"))
        )
        job = _PruningJob(context, absent_on_target=("fake:gone",))
        _apply_ready(job)

        with caplog.at_level(logging.INFO):
            await job.apply()

        assert any("fake:gone" in record.message and "mark" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_the_write_that_drops_a_mark_is_gated(self) -> None:
        """H190 — the drop is a change on a machine, so `--confirm-each-command` shows it."""
        context = make_context()
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:gone"))
        )
        job = _PruningJob(context, absent_on_target=("fake:gone",))
        _apply_ready(job)

        await job.apply()

        gated = [
            call.kwargs.get("mutates")
            for call in context.target.run_command.call_args_list  # pyright: ignore[reportAttributeAccessIssue]
            if "mv --force" in call.args[0]
        ]
        assert gated and all(phrase for phrase in gated)

    @pytest.mark.asyncio
    async def test_the_base_job_prunes_nothing(self) -> None:
        """H191 — a manager with no presence check of its own keeps every mark rather than
        guessing at one."""
        context = make_context()
        context.target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_two_entry_file("fake:gone"))
        )
        job = FakeSyncJob(context)
        _apply_ready(job)

        await job.apply()

        assert _written_decision_files(context.target) == []  # pyright: ignore[reportArgumentType]


class TestDecisionFileDrop:
    @pytest.mark.asyncio
    async def test_drop_rewrites_the_file_without_the_named_entries(self) -> None:
        """H192 — the store's own half of the reconciliation."""
        shell = FakeShellExecutor()
        decisions = DecisionFile("fake", shell)  # pyright: ignore[reportArgumentType]
        await decisions.record(_entry(item_id="fake:a"))
        await decisions.record(_entry(item_id="fake:b"))

        removed = await decisions.drop(["fake:a"])

        assert removed == frozenset({"fake:a"})
        assert set(await decisions.load()) == {"fake:b"}

    @pytest.mark.asyncio
    async def test_dropping_the_last_entry_leaves_a_readable_empty_file(self) -> None:
        """H193 — an emptied file still parses, so the next run reads "nothing recorded"
        rather than warning about a malformed one."""
        shell = FakeShellExecutor()
        decisions = DecisionFile("fake", shell)  # pyright: ignore[reportArgumentType]
        await decisions.record(_entry(item_id="fake:a"))

        await decisions.drop(["fake:a"])

        assert await decisions.load() == {}

    @pytest.mark.asyncio
    async def test_dropping_ids_the_file_does_not_hold_writes_nothing(self) -> None:
        """H194 — nothing to remove is not a rewrite."""
        shell = FakeShellExecutor()
        decisions = DecisionFile("fake", shell)  # pyright: ignore[reportArgumentType]
        await decisions.record(_entry(item_id="fake:a"))
        shell.commands.clear()

        removed = await decisions.drop(["fake:absent"])

        assert removed == frozenset()
        assert not [cmd for cmd in shell.commands if "mv --force" in cmd]

    @pytest.mark.asyncio
    async def test_dropping_nothing_reads_nothing(self) -> None:
        """H195 — the empty case, which every run with no dead mark takes, issues no command."""
        shell = FakeShellExecutor()
        decisions = DecisionFile("fake", shell)  # pyright: ignore[reportArgumentType]

        removed = await decisions.drop([])

        assert removed == frozenset()
        assert shell.commands == []

    @pytest.mark.asyncio
    async def test_a_failed_write_raises_naming_the_file(self) -> None:
        """H196 — a drop that could not be written is not reported as a drop."""
        executor = MagicMock()
        executor.run_command = AsyncMock(
            side_effect=[
                CommandResult(0, _decision_file_contents("fake:a"), ""),
                CommandResult(1, "", "read-only file system"),
            ]
        )

        with pytest.raises(RuntimeError, match=re.escape("fake.decisions.yaml")):
            await DecisionFile("fake", executor).drop(["fake:a"])
