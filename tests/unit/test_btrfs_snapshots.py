"""Unit tests for the shell commands `pcswitcher.btrfs_snapshots` hands to an executor.

The delete-all sweep is a multi-line bash script passed as one `bash -c` argument, and
nothing checks its exit code -- a quoting mistake there does not fail, it silently stops
deleting. These tests read the command the way the remote shell will.
"""

from __future__ import annotations

import shlex
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.btrfs_snapshots import (  # pyright: ignore[reportPrivateUsage]
    _DELETE_ALL_SNAPSHOTS_SCRIPT,
    delete_all_snapshots,
)
from pcswitcher.models import CommandResult


async def _delete_all_command() -> str:
    """The single command string `delete_all_snapshots` issues."""
    executor = MagicMock()
    executor.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    await delete_all_snapshots(executor)
    return str(executor.run_command.call_args[0][0])


@pytest.mark.asyncio
async def test_delete_all_snapshots_passes_the_script_with_its_newlines_intact() -> None:
    """The script reaches bash as itself.

    `repr()` renders a newline as the two characters `\\` and `n`, which puts the whole
    script on one line; bash then fails on the first `{` and the sweep deletes nothing
    while still returning normally. Splitting the command the way a shell does is what
    makes that unrepresentable here.
    """
    command = await _delete_all_command()

    assert shlex.split(command) == ["sudo", "bash", "-c", _DELETE_ALL_SNAPSHOTS_SCRIPT]
    assert "\n" in shlex.split(command)[3]


@pytest.mark.asyncio
async def test_delete_all_snapshots_script_parses_as_bash() -> None:
    """`bash -n` accepts the script as the remote shell receives it -- syntax only, so
    nothing is executed and no btrfs filesystem is needed.
    """
    command = await _delete_all_command()
    script = shlex.split(command)[3]

    result = subprocess.run(["bash", "-n", "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
