"""Unit tests for `package_state.py`'s machine-local decision store (plan 02-04, task 1).

Covers `DecisionFile`/`DecisionEntry`/`filter_inert` as standalone units, using
stub/fake `Executor`s — no real shell/SSH. Task 2 extends this file with
pipeline-level assertions (inert items absent from `PackageSyncJob.plan()`'s diffs,
skip-always recorded on the correct end of the connection in `apply()`).
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.jobs import package_state
from pcswitcher.jobs.package_items import AptPackageItem, ItemClass
from pcswitcher.jobs.package_state import (
    DECISION_FILE_GLOB_RELPATH,
    DECISION_FILE_RELPATH_TEMPLATE,
    DecisionEntry,
    DecisionFile,
    filter_inert,
)
from pcswitcher.models import CommandResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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

        with caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.package_state"):
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

        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.package_state"):
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
