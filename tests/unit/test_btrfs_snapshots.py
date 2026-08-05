"""Unit tests for `pcswitcher.btrfs_snapshots`: the shell commands it hands to an
executor, and the timestamps it names snapshots by.

Both are places a defect stays invisible. The delete-all sweep is a multi-line bash
script passed as one `bash -c` argument and nothing checks its exit code, so a quoting
mistake there does not fail -- it silently stops deleting. A snapshot name's timestamp is
the only ordering key retention has, so a wrong zone does not raise either; it just
deletes the wrong session.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.btrfs_snapshots import (  # pyright: ignore[reportPrivateUsage]
    _DELETE_ALL_SNAPSHOTS_SCRIPT,
    delete_all_snapshots,
    session_folder_name,
    snapshot_name,
)
from pcswitcher.models import CommandResult, Host, Snapshot, SnapshotPhase

# The one stamp format every snapshot path carries (CORE-FR-SNAP-NAME).
_STAMP_FORMAT = "%Y%m%dT%H%M%S"


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


def test_snapshot_and_session_names_are_stamped_in_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stamp is the instant, not the machine's idea of the hour.

    Two machines in different zones write into the same retention ordering (and one
    machine does, either side of a DST change). A local-time stamp sorts by offset
    instead of by age, which promotes the older session and deletes the newer one.

    The process's real zone is moved rather than its clock frozen: `freeze_time`'s
    `tz_offset` shifts `datetime.now()` and `datetime.now(UTC)` by the same amount, so
    under it both spellings agree and this test could not fail. `Asia/Kolkata` is 5:30
    ahead year-round — no DST to make the expectation seasonal, and an offset that moves
    the minutes field as well as the hour, which no rounding could imitate.
    """
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        before = datetime.now(UTC).replace(microsecond=0)
        session_stamp = session_folder_name("abc12345").removesuffix("-abc12345")
        snapshot_stamp = snapshot_name("@home", SnapshotPhase.PRE).removeprefix("pre-@home-")
        after = datetime.now(UTC).replace(microsecond=0)
        local_stamp = datetime.now().strftime(_STAMP_FORMAT)

        # A window rather than an equality, so crossing a second boundary mid-call is not
        # a flake; UTC is a point in it, local time (5:30 away) never is.
        for stamp in (session_stamp, snapshot_stamp):
            assert before <= datetime.strptime(stamp, _STAMP_FORMAT).replace(tzinfo=UTC) <= after
            assert stamp != local_stamp
    finally:
        monkeypatch.undo()
        time.tzset()


def test_parsed_snapshot_timestamp_is_utc_aware() -> None:
    """What `_snapshot_timestamp` writes, `Snapshot.from_path` must read back as the same
    instant — aware, so a comparison against a local-time `now()` raises instead of
    silently ranking sessions by an offset.
    """
    snapshot = Snapshot.from_path(
        "/.snapshots/pc-switcher/20260727T153429-abc12345/pre-@home-20260727T153429", Host.SOURCE
    )

    assert snapshot.timestamp.tzinfo is not None
    assert snapshot.timestamp.utcoffset() == UTC.utcoffset(None)
    assert snapshot.name == "pre-@home-20260727T153429"
