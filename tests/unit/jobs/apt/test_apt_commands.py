"""The apt command strings this job builds, and what their output parses into.

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import shutil
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.executor import LocalExecutor
from pcswitcher.jobs.apt_sync.commands import compare_deb_versions
from pcswitcher.models import CommandResult


def _stub_executor(responses: dict[str, CommandResult]) -> MagicMock:
    """A minimal `Executor`-shaped stub matching by substring (first match wins)."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        raise AssertionError(f"no stub response configured for command: {cmd!r}")

    executor = MagicMock()
    executor.run_command = AsyncMock(side_effect=_side_effect)
    return executor


class TestCompareDebVersions:
    """`compare_deb_versions` delegates ordering to `dpkg --compare-versions`."""

    @pytest.mark.asyncio
    async def test_lt_for_debian_revision_ordering(self) -> None:
        executor = _stub_executor(
            {
                "1.0-1 lt 1.0-2": CommandResult(0, "", ""),
                "1.0-1 gt 1.0-2": CommandResult(1, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "1.0-1", "1.0-2")

        assert result < 0

    @pytest.mark.asyncio
    async def test_gt_for_epoch_beats_larger_upstream_number(self) -> None:
        """`2:1.0` outranks `10.0` — the epoch outranks the larger upstream number."""
        executor = _stub_executor(
            {
                "2:1.0 lt 10.0": CommandResult(1, "", ""),
                "2:1.0 gt 10.0": CommandResult(0, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "2:1.0", "10.0")

        assert result > 0

    @pytest.mark.asyncio
    async def test_equal_for_identical_strings_without_a_second_executor_call(self) -> None:
        executor = _stub_executor({})

        result = await compare_deb_versions(executor, "1.0-1", "1.0-1")

        assert result == 0
        executor.run_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_shells_out_with_shlex_quoted_operands(self) -> None:
        executor = _stub_executor(
            {
                "dpkg --compare-versions 'a b' lt 'c;d'": CommandResult(0, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "a b", "c;d")

        assert result < 0
        first_call = executor.run_command.call_args_list[0]
        assert "'a b'" in first_call.args[0]
        assert "'c;d'" in first_call.args[0]

    @pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg not available on this machine")
    @pytest.mark.asyncio
    async def test_real_dpkg_confirms_epoch_and_revision_ordering(self) -> None:
        """Cross-checks the stub-based tests above against the real binary."""
        executor = LocalExecutor()

        assert await compare_deb_versions(executor, "2:1.0", "10.0") > 0
        assert await compare_deb_versions(executor, "1.0-1", "1.0-2") < 0
        assert await compare_deb_versions(executor, "1.0-1", "1.0-1") == 0
