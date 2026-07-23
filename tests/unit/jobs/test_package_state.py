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
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config_sync import CONFIG_REMOTE_PATH, _copy_config_to_target  # pyright: ignore[reportPrivateUsage]
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages import state as package_state
from pcswitcher.jobs.packages.items import (
    AptPackageItem,
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
)
from pcswitcher.jobs.packages.review import Decision, ReviewOutcome
from pcswitcher.jobs.packages.state import (
    DECISION_FILE_GLOB_RELPATH,
    DECISION_FILE_RELPATH_TEMPLATE,
    SNIPPET_REGISTRY_RELPATH,
    DecisionEntry,
    DecisionFile,
    Snippet,
    SnippetRegistry,
    filter_inert,
)
from pcswitcher.jobs.packages.sync_core import PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, ValidationError

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
    (`cat ... 2>/dev/null`, and the `mkdir -p ... && printf '%s' ... > ... && mv -f ...`
    atomic-write shape), backed by an in-memory dict. Good enough to prove a genuine
    load()/record() round trip without shelling out to a real subprocess.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    async def run_command(self, cmd: str, timeout: float | None = None) -> CommandResult:
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


def _entry(item_id: str = "apt:package:brscan3", reason: str | None = "printer driver") -> DecisionEntry:
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
        items = [AptPackageItem(name="brscan3", version="0.4.11-2"), AptPackageItem(name="vim", version="9.0")]
        decisions = {"apt:package:brscan3": _entry()}

        result = await filter_inert(items, decisions)

        assert [item.name for item in result] == ["vim"]

    @pytest.mark.asyncio
    async def test_empty_decisions_keeps_every_item(self) -> None:
        items = [AptPackageItem(name="vim", version="9.0")]

        result = await filter_inert(items, {})

        assert result == items

    @pytest.mark.asyncio
    async def test_no_items_match_returns_all_unchanged_in_order(self) -> None:
        items = [AptPackageItem(name="a", version="1"), AptPackageItem(name="b", version="1")]
        decisions = {"apt:package:unrelated": _entry(item_id="apt:package:unrelated")}

        result = await filter_inert(items, decisions)

        assert result == items


# ---------------------------------------------------------------------------
# DecisionFile.load()
# ---------------------------------------------------------------------------


class TestDecisionFileLoad:
    @pytest.mark.asyncio
    async def test_absent_file_returns_empty_mapping(self) -> None:
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

        assert set(entries) == {"apt:package:brscan3"}
        assert entries["apt:package:brscan3"].reason == "printer driver"
        assert entries["apt:package:brscan3"].item_class == ItemClass.APT_PACKAGE


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
        assert "mkdir -p" in cmd
        assert ".pcswitcher-tmp" in cmd
        assert "mv -f" in cmd
        assert cmd.index("mkdir -p") < cmd.index(".pcswitcher-tmp") < cmd.index("mv -f")

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
        local file, which is what makes this method correct for BOTH roles (D-08a)."""
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        store = DecisionFile("apt", executor)

        with patch("builtins.open", side_effect=AssertionError("record() must not touch the local filesystem")):
            await store.record(_entry())

        # record() reads (load()) then writes: two run_command calls, zero local opens.
        assert executor.run_command.await_count == 2

    @pytest.mark.asyncio
    async def test_recording_same_item_id_twice_does_not_duplicate(self) -> None:
        shell = FakeShellExecutor()
        store = DecisionFile("apt", shell)

        await store.record(_entry(reason="first reason"))
        await store.record(_entry(reason="second reason"))

        entries = await DecisionFile("apt", shell).load()
        assert len(entries) == 1
        assert entries["apt:package:brscan3"].reason == "second reason"

    @pytest.mark.asyncio
    async def test_recording_a_second_distinct_item_preserves_the_first(self) -> None:
        shell = FakeShellExecutor()
        store = DecisionFile("apt", shell)

        await store.record(_entry(item_id="apt:package:brscan3"))
        await store.record(_entry(item_id="apt:package:some-vendor-tool"))

        entries = await DecisionFile("apt", shell).load()
        assert set(entries) == {"apt:package:brscan3", "apt:package:some-vendor-tool"}


# ---------------------------------------------------------------------------
# Path/glob relpath construction — the store and folder_sync's exclusion share this.
# ---------------------------------------------------------------------------


class TestRelpathConstants:
    def test_relpath_template_places_file_under_config_pc_switcher(self) -> None:
        assert DECISION_FILE_RELPATH_TEMPLATE.format(manager="apt") == ".config/pc-switcher/apt.decisions.yaml"

    def test_glob_relpath_covers_every_manager_with_one_pattern(self) -> None:
        assert DECISION_FILE_GLOB_RELPATH == ".config/pc-switcher/*.decisions.yaml"

    def test_no_default_machine_specific_package_hardcoded(self) -> None:
        """D-10: no default entry lives in Python — grep-verifiable, mirrors the plan's
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


class _FakePackageJob(PackageSyncJob):
    """Minimal concrete `PackageSyncJob` with configurable source/target items and a
    recording `converge()`, matching the shape `test_package_sync_core.py`'s
    `FakeSyncJob` uses — isolates the shared plan()/apply() pipeline from any
    apt-specific machinery.
    """

    name: ClassVar[str] = "fake_pkg"
    manager_id: ClassVar[str] = "fake"

    def __init__(
        self,
        context: JobContext,
        *,
        source_items: list[AptPackageItem] = [],  # noqa: B006 (test-only, never mutated)
        target_items: list[AptPackageItem] = [],  # noqa: B006
    ) -> None:
        super().__init__(context)
        self._source_items = source_items
        self._target_items = target_items
        self.converge_calls: list[ItemDiff] = []

    async def capture_source_items(self) -> list[AptPackageItem]:
        return self._source_items

    async def query_target_items(self) -> list[AptPackageItem]:
        return self._target_items

    async def validate(self) -> list[ValidationError]:
        return []

    async def converge(self, diff: ItemDiff) -> CommandResult:
        self.converge_calls.append(diff)
        return CommandResult(0, "", "")


def _remove_diff(item_id: str) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REMOVE,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


def _install_diff(item_id: str) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.INSTALL,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


class TestPipelineWiring:
    @pytest.mark.asyncio
    async def test_source_held_inert_item_absent_from_the_plans_diffs(self) -> None:
        context = make_context()
        source = context.source
        source.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_decision_file_contents("apt:package:brscan3"))
        )
        job = _FakePackageJob(
            context,
            source_items=[AptPackageItem(name="brscan3", version="1.0"), AptPackageItem(name="vim", version="9.0")],
        )

        plan = await job.plan()

        assert {d.item_id for d in plan.diffs} == {"apt:package:vim"}
        all_group_item_ids = {entry.item_id for group in plan.groups for entry in group.entries}
        assert "apt:package:brscan3" not in all_group_item_ids

    @pytest.mark.asyncio
    async def test_target_held_inert_item_absent_even_though_source_also_differs(self) -> None:
        context = make_context()
        target = context.target
        target.run_command = AsyncMock(  # pyright: ignore[reportAttributeAccessIssue]
            side_effect=_respond_cat_with(_decision_file_contents("apt:package:legacy-tool"))
        )
        job = _FakePackageJob(context, target_items=[AptPackageItem(name="legacy-tool", version="1.0")])

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_plan_issues_no_decision_file_write(self) -> None:
        context = make_context()
        job = _FakePackageJob(context, source_items=[AptPackageItem(name="vim", version="9.0")])

        await job.plan()

        for cmd in [call.args[0] for call in context.source.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv -f" not in cmd
        for cmd in [call.args[0] for call in context.target.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv -f" not in cmd

    @pytest.mark.asyncio
    async def test_every_record_call_originates_from_apply_not_plan(self) -> None:
        context = make_context()
        job = _FakePackageJob(context, source_items=[AptPackageItem(name="vim", version="9.0")])
        plan = await job.plan()
        job.accept_review(plan, ReviewOutcome(decisions={"apt:package:vim": Decision.APPLY}, was_interactive=True))

        with patch.object(DecisionFile, "record", new=AsyncMock()) as record_mock:
            await job.apply()

        record_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skip_always_on_remove_writes_to_target_not_source(self) -> None:
        context = make_context()
        job = _FakePackageJob(context)
        diff = _remove_diff("apt:package:legacy-tool")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        target_cmds = [call.args[0] for call in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        source_cmds = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv -f" in cmd for cmd in target_cmds)
        assert not any("mv -f" in cmd for cmd in source_cmds)

    @pytest.mark.asyncio
    async def test_skip_always_on_install_writes_to_source_not_target(self) -> None:
        context = make_context()
        job = _FakePackageJob(context)
        diff = _install_diff("apt:package:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        target_cmds = [call.args[0] for call in context.target.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        source_cmds = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv -f" in cmd for cmd in source_cmds)
        assert not any("mv -f" in cmd for cmd in target_cmds)

    @pytest.mark.asyncio
    async def test_no_record_call_when_dry_run(self) -> None:
        context = make_context(dry_run=True)
        job = _FakePackageJob(context)
        diff = _remove_diff("apt:package:legacy-tool")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=True))

        await job.apply()

        for cmd in [call.args[0] for call in context.target.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv -f" not in cmd

    @pytest.mark.asyncio
    async def test_no_record_call_when_outcome_was_not_interactive(self) -> None:
        context = make_context()
        job = _FakePackageJob(context)
        diff = _remove_diff("apt:package:legacy-tool")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS}, was_interactive=False))

        await job.apply()

        for cmd in [call.args[0] for call in context.target.run_command.call_args_list]:  # pyright: ignore[reportAttributeAccessIssue]
            assert "mv -f" not in cmd


# ---------------------------------------------------------------------------
# config_sync never transfers a decision file (D-09) — verified, not assumed.
# ---------------------------------------------------------------------------


class TestConfigSyncScope:
    @pytest.mark.asyncio
    async def test_copy_config_to_target_sends_only_config_yaml(self, tmp_path: Path) -> None:
        source_path = tmp_path / "config.yaml"
        source_path.write_text("logging: {}\n")

        target = MagicMock()
        target.run_command = AsyncMock(side_effect=_respond_echo_home("/home/alice"))
        target.send_file = AsyncMock()

        await _copy_config_to_target(target, source_path)

        assert target.send_file.await_count == 1
        remote_path = target.send_file.call_args.args[1]
        assert remote_path.endswith("config.yaml")
        assert "decisions" not in remote_path
        assert CONFIG_REMOTE_PATH.endswith("/config.yaml")


# ---------------------------------------------------------------------------
# SnippetRegistry — the shared, synced counterpart to DecisionFile (D-20, D-23).
# ---------------------------------------------------------------------------


class TestSnippetRegistry:
    def test_relpath_places_file_under_config_pc_switcher(self) -> None:
        assert SNIPPET_REGISTRY_RELPATH == ".config/pc-switcher/package-snippets.yaml"

    @pytest.mark.asyncio
    async def test_absent_file_returns_empty_mapping(self) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(1, "", ""))
        registry = SnippetRegistry(executor)

        assert await registry.load() == {}

    @pytest.mark.asyncio
    async def test_empty_file_returns_empty_mapping(self) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        registry = SnippetRegistry(executor)

        assert await registry.load() == {}

    @pytest.mark.asyncio
    async def test_malformed_registry_returns_empty_mapping_and_warns_naming_the_path(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "snippets: [\n  - broken\n", ""))
        registry = SnippetRegistry(executor)

        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.packages.state"):
            entries = await registry.load()

        assert entries == {}
        assert len(caplog.records) == 1
        assert "package-snippets.yaml" in caplog.records[0].message

    @pytest.mark.asyncio
    async def test_add_then_get_round_trips_body_verbatim_including_whitespace(self) -> None:
        shell = FakeShellExecutor()
        snippet = Snippet(
            item_id="unreproducible:apt-no-candidate:brscan3",
            label="brscan3 (no apt candidate)",
            body="  sudo dpkg -i /tmp/brscan3.deb\n\nsudo apt-get install -f -y\n",
            authored_at="2026-07-23T09:00:00+00:00",
            authored_on="laptop",
        )

        await SnippetRegistry(shell).add(snippet)
        reloaded = await SnippetRegistry(shell).get(snippet.item_id)

        assert reloaded is not None
        assert reloaded.body == snippet.body

    @pytest.mark.asyncio
    async def test_get_returns_none_for_an_unregistered_item(self) -> None:
        shell = FakeShellExecutor()

        assert await SnippetRegistry(shell).get("unreproducible:apt-no-candidate:missing") is None

    @pytest.mark.asyncio
    async def test_write_is_atomic_temp_then_move(self) -> None:
        executor = MagicMock()
        executor.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        registry = SnippetRegistry(executor)

        await registry.add(Snippet(item_id="x", label="x", body="echo hi", authored_at="t", authored_on="h"))

        cmd = executor.run_command.call_args.args[0]
        assert "mkdir -p" in cmd
        assert ".pcswitcher-tmp" in cmd
        assert "mv -f" in cmd
        assert cmd.index("mkdir -p") < cmd.index(".pcswitcher-tmp") < cmd.index("mv -f")

    @pytest.mark.asyncio
    async def test_add_preserves_an_unrelated_pre_existing_entry(self) -> None:
        shell = FakeShellExecutor()
        first = Snippet(item_id="a", label="a", body="echo a", authored_at="t", authored_on="h")
        second = Snippet(item_id="b", label="b", body="echo b", authored_at="t", authored_on="h")

        await SnippetRegistry(shell).add(first)
        await SnippetRegistry(shell).add(second)

        entries = await SnippetRegistry(shell).load()
        assert set(entries) == {"a", "b"}
        assert entries["a"].body == "echo a"

    @pytest.mark.asyncio
    async def test_replay_passes_body_as_one_quoted_argument_with_login_shell_false(self) -> None:
        shell = FakeShellExecutor()
        snippet = Snippet(item_id="x", label="x", body="echo hello world", authored_at="t", authored_on="h")
        await SnippetRegistry(shell).add(snippet)

        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(0, "", ""))

        result = await SnippetRegistry(shell).replay("x", target)

        target.run_command.assert_called_once_with("bash -c 'echo hello world'", login_shell=False)
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
        shell = FakeShellExecutor()
        snippet = Snippet(item_id="x", label="x", body="false", authored_at="t", authored_on="h")
        await SnippetRegistry(shell).add(snippet)

        target = MagicMock()
        target.run_command = AsyncMock(return_value=CommandResult(1, "", "boom"))

        result = await SnippetRegistry(shell).replay("x", target)

        assert result.success is False
        assert result.stderr == "boom"
