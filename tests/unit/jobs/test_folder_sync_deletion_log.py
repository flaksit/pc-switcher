"""Unit tests proving rsync deletion lines are persisted at FULL in the JSON log.

Drives FolderSyncJob._stream_rsync with a fake async byte-chunk generator
containing a `*deleting` line and reads the written JSON-lines log file to
assert a FULL-level record is present — for both dry-run and real-run modes.

ADR-015 (T-01-14-01): the deletion audit trail must be durable so destructive
actions are always auditable, replacing the removed btrfs find-new guard.

Key path: _stream_rsync `*deleting` line → `self._log(FULL, ...)` →
`logging.getLogger("pcswitcher.jobs.base").log(15, ...)` → propagates to
`pcswitcher` logger → QueueHandler → QueueListener → FileHandler (JSON lines).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pcswitcher.config import LogConfig
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.folder_sync import PASS_FULL, FolderEntry, FolderSyncJob
from pcswitcher.logger import setup_logging
from pcswitcher.models import LogLevel

# The deleted path used across all test cases.
_DELETED_PATH = "/home/user/old_secret.txt"

# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------


def _make_context(dry_run: bool = False) -> JobContext:
    """Minimal JobContext with mock executors — no network connections made."""
    return JobContext(
        config={"folders": [{"path": "/home"}]},
        source=MagicMock(),
        target=MagicMock(),
        event_bus=MagicMock(),
        session_id="del-log-test",
        source_hostname="src",
        target_hostname="tgt",
        dry_run=dry_run,
    )


async def _deletion_chunks() -> AsyncGenerator[bytes]:
    """Single-chunk async generator: one `*deleting` line for a known path."""
    yield f"*deleting {_DELETED_PATH}\n".encode()


async def _drive_stream_rsync_to_log(log_file: Path, *, dry_run: bool) -> None:
    """Set up real logging, drive _stream_rsync with a deletion line, flush to disk.

    Uses a file log floor of DEBUG (10) which is strictly below FULL (15), so
    FULL records reach the file handler by default.  Restores the pcswitcher
    and root logger state on exit to prevent handler accumulation between tests.
    """
    pcs_logger = logging.getLogger("pcswitcher")
    root_logger = logging.getLogger()

    # Snapshot pre-test state so we can restore it afterward.
    pre_pcs_handlers = list(pcs_logger.handlers)
    pre_pcs_level = pcs_logger.level
    pre_pcs_propagate = pcs_logger.propagate
    pre_root_handlers = list(root_logger.handlers)
    pre_root_level = root_logger.level

    log_config = LogConfig(file=logging.DEBUG, tui=logging.DEBUG, external=logging.WARNING)
    listener, _ = setup_logging(log_file, log_config)

    try:
        ctx = _make_context(dry_run=dry_run)
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path="/home")
        await job._stream_rsync(_deletion_chunks(), folder, PASS_FULL)
    finally:
        # Stop listener first so the QueueListener drains and FileHandler flushes.
        listener.stop()
        # Remove only the handlers that setup_logging added; leave any pre-existing ones.
        for h in list(pcs_logger.handlers):
            if h not in pre_pcs_handlers:
                pcs_logger.removeHandler(h)
        for h in list(root_logger.handlers):
            if h not in pre_root_handlers:
                root_logger.removeHandler(h)
        pcs_logger.setLevel(pre_pcs_level)
        pcs_logger.propagate = pre_pcs_propagate
        root_logger.setLevel(pre_root_level)


def _find_full_deletion_record(log_file: Path) -> dict[str, object] | None:
    """Return the first JSON record at FULL level containing `_DELETED_PATH`, or None."""
    for raw_line in log_file.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if record.get("level") == "FULL" and _DELETED_PATH in str(record.get("event", "")):
            return record
    return None


# -------------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDeletionLogPersistence:
    """Deletions logged at FULL persist in the JSON-lines file for both run modes."""

    async def test_deletion_persisted_at_full_in_real_run(self, tmp_path: Path) -> None:
        """A *deleting line in real-run mode produces a FULL JSON record in the log file.

        Proves that live rsync deletions are always auditable (T-01-14-01, ADR-015).
        """
        log_file = tmp_path / "real_run.log"
        await _drive_stream_rsync_to_log(log_file, dry_run=False)

        assert log_file.exists(), "Log file must be created by setup_logging"
        record = _find_full_deletion_record(log_file)
        assert record is not None, (
            f"Expected a FULL-level JSON record containing {_DELETED_PATH!r} in {log_file}.\n"
            f"Log contents:\n{log_file.read_text()}"
        )
        assert record["level"] == "FULL"
        assert _DELETED_PATH in str(record["event"])

    async def test_deletion_persisted_at_full_in_dry_run(self, tmp_path: Path) -> None:
        """A *deleting line in dry-run mode produces a FULL JSON record in the log file.

        Proves that the dry-run deletion preview is equally auditable: --dry-run
        streams rsync output through the same _stream_rsync path so deletions
        that WOULD happen are recorded at FULL whether or not changes are made.
        """
        log_file = tmp_path / "dry_run.log"
        await _drive_stream_rsync_to_log(log_file, dry_run=True)

        assert log_file.exists(), "Log file must be created by setup_logging"
        record = _find_full_deletion_record(log_file)
        assert record is not None, (
            f"Expected a FULL-level JSON record containing {_DELETED_PATH!r} in {log_file}.\n"
            f"Log contents:\n{log_file.read_text()}"
        )
        assert record["level"] == "FULL"
        assert _DELETED_PATH in str(record["event"])


class TestDefaultLogFloor:
    """The default file log floor captures FULL-level records without configuration."""

    def test_default_file_log_floor_is_at_or_below_full(self) -> None:
        """LogConfig().file defaults to DEBUG (10), which is <= FULL (15).

        This means operators get deletion audit records in the log file out of
        the box without any explicit configuration change.
        """
        default_floor = LogConfig().file
        assert default_floor <= LogLevel.FULL, (
            f"Default file log floor {default_floor} is above FULL ({LogLevel.FULL}); "
            "deletions would be silently dropped from the log file."
        )
