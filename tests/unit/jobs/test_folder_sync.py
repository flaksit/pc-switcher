"""Unit tests for FolderSyncJob.

Tests cover: active-folder selection, validate() preflight (sudo rsync, acl, folder
existence), rsync command construction, and transfer streaming/exit-code handling.

All executor interactions are mocked; no real SSH connections are made.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import sys
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.confirmer import Confirmer, TerminalUIConfirmer
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.folder_sync import FolderEntry, FolderSyncJob
from pcswitcher.models import CommandResult, Host, LogLevel, ProgressUpdate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fail_when(substring: str, stderr: str) -> Callable[..., CommandResult]:
    """Return a run_command side_effect that fails (exit 1) when `substring` is in the command."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if substring in cmd:
            return CommandResult(exit_code=1, stdout="", stderr=stderr)
        return CommandResult(exit_code=0, stdout="", stderr="")

    return _side_effect


def _history_json(role: str = "target", peer: str = "source-host") -> str:
    """Produce a target sync-history JSON payload (non-first-sync marker)."""
    return json.dumps({"last_role": role, "last_peer": peer})


def _mock_isatty(interactive: bool) -> MagicMock:
    """Create a mock for sys.stdin whose isatty() returns `interactive`."""
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = interactive
    return mock_stdin


class FakeConfirmer:
    """A Confirmer stub that records calls and returns a fixed answer.

    Satisfies the Confirmer protocol without touching the console/TUI, so first-sync
    tests can assert whether the job prompted and control the yes/no outcome.
    """

    def __init__(self, response: bool = True) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def confirm(
        self,
        *,
        title: str,
        message: str,
        allow: bool,
        allow_flag: str,
        log_extra: dict[str, Any] | None = None,
    ) -> bool:
        self.calls.append({"title": title, "message": message, "allow": allow, "allow_flag": allow_flag})
        return self.response


def make_context(
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
    *,
    first_sync: bool = False,
    allow_first_sync: bool = False,
    confirmer: Confirmer | None = None,
) -> JobContext:
    """Create a JobContext with mocked source/target executors.

    By default the target reports a readable sync-history (non-first sync), so
    execute() skips the first-sync confirmation. Pass ``first_sync=True`` to make the
    target's history read come back empty (a first-ever sync).
    """
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    target = MagicMock()
    if first_sync:
        # Empty stdout from the sync-history read → treated as a first-ever sync.
        target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    else:

        def _target_side_effect(cmd: str, **_: object) -> CommandResult:
            # The first-sync probe reads sync-history.json; return valid history so the
            # job classifies the target as already-synced. Everything else succeeds.
            if "sync-history.json" in cmd:
                return CommandResult(exit_code=0, stdout=_history_json(), stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        target.run_command = AsyncMock(side_effect=_target_side_effect)
    return JobContext(
        config=config if config is not None else {"folders": [{"path": "/home"}]},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
        allow_first_sync=allow_first_sync,
        confirmer=confirmer,
    )


def all_success_source(cmd: str, **_: object) -> CommandResult:
    """Default source side_effect: all commands succeed."""
    return CommandResult(exit_code=0, stdout="", stderr="")


def all_success_target(cmd: str, **_: object) -> CommandResult:
    """Default target side_effect: all commands succeed."""
    return CommandResult(exit_code=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# FolderEntry
# ---------------------------------------------------------------------------


class TestFolderEntry:
    """Tests for the FolderEntry dataclass."""

    def test_defaults(self) -> None:
        """FolderEntry defaults enabled=True and excludes=[]."""
        entry = FolderEntry(path="/home")
        assert entry.enabled is True
        assert entry.excludes == []

    def test_to_rsync_filter_args_empty(self) -> None:
        """No excludes → empty filter arg list."""
        entry = FolderEntry(path="/home", excludes=[])
        assert entry.to_rsync_filter_args() == []

    def test_to_rsync_filter_args_preserves_order(self) -> None:
        """Filter args are generated in config order (first-match-wins)."""
        entry = FolderEntry(path="/home", excludes=[".ssh/id_*", ".config/tailscale"])
        args = entry.to_rsync_filter_args()
        assert args == ["--filter=- .ssh/id_*", "--filter=- .config/tailscale"]


# ---------------------------------------------------------------------------
# Active-folder selection
# ---------------------------------------------------------------------------


class TestActiveFolderSelection:
    """validate() only operates on enabled folder entries."""

    def test_disabled_entries_are_skipped(self) -> None:
        """enabled=false entries are excluded from _active_folders()."""
        ctx = make_context(
            config={
                "folders": [
                    {"path": "/home"},
                    {"path": "/root", "enabled": False},
                ]
            }
        )
        job = FolderSyncJob(ctx)
        active = job._active_folders()
        assert [f.path for f in active] == ["/home"]

    def test_all_enabled_by_default(self) -> None:
        """Entries without 'enabled' key default to enabled=True."""
        ctx = make_context(config={"folders": [{"path": "/home"}, {"path": "/root"}]})
        job = FolderSyncJob(ctx)
        active = job._active_folders()
        assert {f.path for f in active} == {"/home", "/root"}

    def test_explicitly_enabled_entries_included(self) -> None:
        """enabled=true entries are included."""
        ctx = make_context(config={"folders": [{"path": "/home", "enabled": True}]})
        job = FolderSyncJob(ctx)
        assert len(job._active_folders()) == 1


# ---------------------------------------------------------------------------
# Task 1: validate() preflight checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestValidatePreflight:
    """validate() enforces sudo rsync availability, acl package, and folder existence."""

    async def test_all_preflight_checks_pass(self) -> None:
        """When all preflight commands succeed, validate() returns no errors."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        # source and target run_command already return success by default
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert errors == []

    async def test_missing_sudo_rsync_on_target(self) -> None:
        """validate() returns a ValidationError for HOST.TARGET when sudo rsync is unavailable on target."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(side_effect=fail_when("rsync", "rsync not found"))
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert any(e.host == Host.TARGET and "rsync" in e.message.lower() for e in errors)

    async def test_missing_sudo_rsync_on_source(self) -> None:
        """validate() returns a ValidationError for HOST.SOURCE when sudo rsync is unavailable on source."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.source.run_command = AsyncMock(side_effect=fail_when("rsync", "rsync not found"))
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert any(e.host == Host.SOURCE and "rsync" in e.message.lower() for e in errors)

    async def test_missing_acl_on_source(self) -> None:
        """validate() returns a ValidationError for HOST.SOURCE when acl package is absent on source."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.source.run_command = AsyncMock(side_effect=fail_when("acl", "no packages found"))
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert any(e.host == Host.SOURCE and "acl" in e.message.lower() for e in errors)

    async def test_missing_acl_on_target(self) -> None:
        """validate() returns a ValidationError for HOST.TARGET when acl package is absent on target."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(side_effect=fail_when("acl", "no packages found"))
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert any(e.host == Host.TARGET and "acl" in e.message.lower() for e in errors)

    async def test_missing_source_folder(self) -> None:
        """validate() returns a ValidationError naming the path when an enabled folder is absent on source."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.source.run_command = AsyncMock(side_effect=fail_when("test -d", "no such file"))
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert any(e.host == Host.SOURCE and "/home" in e.message for e in errors)

    async def test_disabled_folder_not_checked(self) -> None:
        """Disabled folders are not checked for existence."""
        ctx = make_context(
            config={
                "folders": [
                    {"path": "/home"},
                    {"path": "/root", "enabled": False},
                ]
            }
        )
        job = FolderSyncJob(ctx)
        # Record which commands were run on source
        source_cmds: list[str] = []

        async def record_source(cmd: str, **kw: object) -> CommandResult:
            source_cmds.append(cmd)
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.source.run_command = AsyncMock(side_effect=record_source)
        await job.validate()
        # /root should never appear in source commands
        assert not any("/root" in c for c in source_cmds)

    async def test_folder_path_is_shell_quoted(self) -> None:
        """Folder paths in preflight commands are shell-quoted (T-04-01 injection guard)."""
        ctx = make_context(config={"folders": [{"path": "/home/user name"}]})
        source_cmds: list[str] = []

        async def record(cmd: str, **kw: object) -> CommandResult:
            source_cmds.append(cmd)
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.source.run_command = AsyncMock(side_effect=record)
        ctx.target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        job = FolderSyncJob(ctx)
        await job.validate()
        # shlex.quote wraps the path in single quotes: '/home/user name'
        # Verify that exact quoted form appears in the test -d command (not the bare path).
        expected_quoted = shlex.quote("/home/user name")  # -> "'/home/user name'"
        folder_checks = [c for c in source_cmds if "test -d" in c]
        assert folder_checks, "expected at least one test -d call"
        assert all(expected_quoted in c for c in folder_checks), (
            f"Expected shell-quoted path {expected_quoted!r} in folder check commands, got: {folder_checks}"
        )

    async def test_execute_stub_no_longer_raises_not_implemented(self) -> None:
        """execute() no longer raises NotImplementedError — it is implemented."""
        ctx = make_context()

        async def fake_chunks(*_: object, **__: object):  # type: ignore[no-untyped-def]
            return
            yield b""  # make it an async generator

        fake_proc = MagicMock()
        fake_proc.read_stdout_chunks = fake_chunks
        fake_proc.wait_result = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        # Must NOT raise NotImplementedError; must complete without error.
        await job.execute()


# ---------------------------------------------------------------------------
# Task 1 (plan 05): _build_rsync_cmd
# ---------------------------------------------------------------------------


class TestBuildRsyncCmd:
    """Tests for FolderSyncJob._build_rsync_cmd (plan 05).

    Each test directly inspects the returned shell command string for the
    presence or absence of specific flags and arguments (injection-safe per
    T-05-01, correct flag baseline per D-13, D-05, D-14).
    """

    def _build(
        self,
        path: str = "/home",
        excludes: list[str] | None = None,
        dry_run: bool = False,
    ) -> str:
        ctx = make_context(config={"folders": [{"path": path}]})
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path=path, excludes=excludes or [])
        return job._build_rsync_cmd(folder, dry_run)

    def test_base_flags_present(self) -> None:
        """Command contains the full D-13 flag baseline."""
        cmd = self._build()
        assert "-aAXHS" in cmd
        assert "--numeric-ids" in cmd
        assert "--delete" in cmd
        assert "--info=progress2" in cmd
        assert "--partial" in cmd
        assert "--mkpath" in cmd

    def test_root_via_sudo_and_ssh_transport(self) -> None:
        """Command uses --rsync-path='sudo rsync' for remote root and an -e ssh option with -T."""
        cmd = self._build()
        # Remote root via sudo (target side)
        assert "--rsync-path='sudo rsync'" in cmd
        # SSH transport with -T (no pseudo-tty)
        assert "-T" in cmd

    def test_no_forbidden_flags(self) -> None:
        """Command never includes --delete-excluded or --checksum (D-06, D-14)."""
        cmd = self._build(excludes=[".ssh/id_*"])
        assert "--delete-excluded" not in cmd
        assert "--checksum" not in cmd

    def test_filter_args_count_equals_excludes(self) -> None:
        """Number of --filter args in the command equals the number of excludes."""

        excludes = [".ssh/id_*", ".config/tailscale"]
        cmd = self._build(excludes=excludes)
        matches = re.findall(r"--filter", cmd)
        assert len(matches) == len(excludes)

    def test_filter_args_preserve_order(self) -> None:
        """Filter args appear in config order (first-match-wins preserved for rsync)."""
        cmd = self._build(excludes=[".ssh/id_*", ".config/tailscale"])
        idx_ssh = cmd.index(".ssh/id_*")
        idx_tailscale = cmd.index(".config/tailscale")
        assert idx_ssh < idx_tailscale

    def test_no_filter_args_when_no_excludes(self) -> None:
        """A folder with no excludes produces no --filter args."""
        cmd = self._build(excludes=[])
        assert "--filter" not in cmd

    def test_dry_run_true_adds_flag(self) -> None:
        """dry_run=True includes --dry-run in the command."""
        cmd = self._build(dry_run=True)
        assert "--dry-run" in cmd

    def test_dry_run_false_omits_flag(self) -> None:
        """dry_run=False does not include --dry-run in the command."""
        cmd = self._build(dry_run=False)
        assert "--dry-run" not in cmd

    def test_source_path_has_trailing_slash(self) -> None:
        """Source argument ends with a trailing slash (sync contents, not directory)."""
        cmd = self._build(path="/home")
        assert "/home/" in cmd

    def test_destination_format(self) -> None:
        """Destination is <target_hostname>:<path>/ form."""
        cmd = self._build(path="/home")
        # target_hostname is "target-host" in make_context()
        assert "target-host" in cmd
        assert "/home/" in cmd

    def test_config_derived_values_are_shell_quoted(self) -> None:
        """Paths and exclude patterns with special characters are shell-quoted (T-05-01)."""
        excludes = [".ssh/id_*"]
        cmd = self._build(path="/home/user name", excludes=excludes)
        # The path with a space must be quoted in the command
        assert "/home/user name/" not in cmd or "'/home/user name/'" in cmd


# ---------------------------------------------------------------------------
# Task 2 (plan 05): _stream_rsync and execute()
# ---------------------------------------------------------------------------


def make_fake_process(
    *,
    exit_code: int = 0,
    stdout_chunks: list[bytes] | None = None,
    stderr: str = "",
) -> MagicMock:
    """Create a fake LocalProcess stub for use in execute() tests.

    Provides `read_stdout_chunks` (async generator) and `wait_result` (AsyncMock)
    without spawning a real subprocess.
    """
    proc = MagicMock()
    chunks = stdout_chunks or []

    async def fake_read_stdout_chunks(*_: object, **__: object):  # type: ignore[no-untyped-def]
        for chunk in chunks:
            yield chunk

    proc.read_stdout_chunks = fake_read_stdout_chunks
    proc.wait_result = AsyncMock(return_value=CommandResult(exit_code=exit_code, stdout="", stderr=stderr))
    return proc


# Fake rsync stdout: a progress2 line then two per-file lines (one transfer, one deletion).
_RSYNC_STDOUT_SAMPLE = (
    b"9.53G 21% 317.26MB/s 0:00:28 (xfr#83, to-chk=444/538)\r>f+++++++++ path/to/file.txt\n*deleting path/to/old.txt\n"
)


@pytest.mark.asyncio
class TestStreamRsync:
    """Tests for FolderSyncJob._stream_rsync (decoupled from subprocess).

    _stream_rsync consumes an async byte-chunk source so it is testable
    with a fake async generator instead of a real rsync subprocess.
    """

    async def _run_stream(
        self,
        chunks: list[bytes],
        folder_path: str = "/home",
    ) -> tuple[tuple[int, int, int], list[tuple[object, object, str]], list[object]]:
        """Helper: run _stream_rsync with fake chunks; capture log calls and progress calls."""
        ctx = make_context()
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path=folder_path)

        log_calls: list[tuple[object, object, str]] = []
        progress_calls: list[object] = []

        def fake_log(host: object, level: object, message: str, **kw: object) -> None:
            log_calls.append((host, level, message))

        def fake_progress(update: object) -> None:
            progress_calls.append(update)

        job._log = fake_log  # type: ignore[method-assign]
        job._report_progress = fake_progress  # type: ignore[method-assign]

        async def gen_chunks():  # type: ignore[no-untyped-def]
            for chunk in chunks:
                yield chunk

        result = await job._stream_rsync(gen_chunks(), folder)
        return result, log_calls, progress_calls

    async def test_progress_line_emits_report_progress(self) -> None:
        """A progress2 line triggers a _report_progress call with parsed percent."""
        progress_line = b"9.53G 21% 317.26MB/s 0:00:28 (xfr#83, to-chk=444/538)\r"
        _, _, progress_calls = await self._run_stream([progress_line])

        assert len(progress_calls) >= 1
        update = progress_calls[0]

        assert isinstance(update, ProgressUpdate)
        assert update.percent == 21
        assert update.current == 83

    async def test_per_file_line_logged_at_full(self) -> None:
        """An --out-format per-file line is logged at LogLevel.FULL."""
        file_line = b">f+++++++++ path/to/file.txt\n"
        _, log_calls, _ = await self._run_stream([file_line])

        full_logs = [msg for _, level, msg in log_calls if level == LogLevel.FULL]
        assert any("path/to/file.txt" in msg for msg in full_logs), (
            f"Expected FULL log with filename; got: {full_logs}"
        )

    async def test_deletion_line_increments_count(self) -> None:
        """*deleting lines increment the files_deleted counter."""
        del_line = b"*deleting path/to/old.txt\n"
        (_, _, files_deleted), _, _ = await self._run_stream([del_line])
        assert files_deleted == 1

    async def test_multiple_deletions_counted(self) -> None:
        """Each *deleting line increments the counter independently."""
        data = b"*deleting a.txt\n*deleting b.txt\n"
        (_, _, files_deleted), _, _ = await self._run_stream([data])
        assert files_deleted == 2

    async def test_combined_sample_produces_progress_and_file_logs(self) -> None:
        """Full sample (progress2 + per-file lines) emits progress and FULL logs."""
        (_, _, files_deleted), log_calls, progress_calls = await self._run_stream([_RSYNC_STDOUT_SAMPLE])

        # At least one progress update
        assert len(progress_calls) >= 1
        # At least one FULL log (for >f... line)
        full_logs = [msg for _, level, msg in log_calls if level == LogLevel.FULL]
        assert full_logs
        # Deletion counted
        assert files_deleted == 1

    async def test_carriage_return_delimited_progress_handled(self) -> None:
        """Progress lines separated by \\r (not \\n) are still parsed."""
        data = (
            b"9.53G 10% 300.00MB/s 0:00:10 (xfr#10, to-chk=90/100)\r"
            b"9.53G 50% 300.00MB/s 0:00:05 (xfr#50, to-chk=50/100)\r"
        )
        _, _, progress_calls = await self._run_stream([data])
        assert len(progress_calls) >= 2

    async def test_returns_counts_tuple(self) -> None:
        """_stream_rsync returns a 3-tuple (files_xfr, bytes_xfr, files_deleted)."""
        result, _, _ = await self._run_stream([_RSYNC_STDOUT_SAMPLE])
        assert isinstance(result, tuple)
        assert len(result) == 3

    async def test_progress_line_reports_transferred_bytes(self) -> None:
        """_stream_rsync returns a non-zero bytes_transferred from the progress2 size token (WR-01)."""
        # 9.53G in the progress line → bytes_transferred must be > 0 and match _parse_size_to_bytes
        progress_line = b"9.53G 21% 317.26MB/s 0:00:28 (xfr#83, to-chk=444/538)\r"
        (_, bytes_transferred, _), _, _ = await self._run_stream([progress_line])

        expected = FolderSyncJob._parse_size_to_bytes("9.53G")
        assert bytes_transferred > 0, "bytes_transferred must be non-zero when rsync reports progress"
        assert bytes_transferred == expected

    async def test_parse_size_to_bytes_units(self) -> None:
        """_parse_size_to_bytes converts K/M/G/T suffixes and bare integers correctly (WR-01)."""
        assert FolderSyncJob._parse_size_to_bytes("1.00K") == 1024
        assert FolderSyncJob._parse_size_to_bytes("512") == 512
        assert FolderSyncJob._parse_size_to_bytes("1M") == 1024**2
        assert FolderSyncJob._parse_size_to_bytes("1G") == 1024**3
        assert FolderSyncJob._parse_size_to_bytes("1T") == 1024**4

    async def test_created_and_hardlink_change_types_logged_at_full(self) -> None:
        """Per-file lines beginning with 'c' (created) or 'h' (hard link) are logged at FULL (IN-03)."""
        # rsync %i format: 'c' = created dir/symlink/device, 'h' = hard link
        c_line = b"cd+++++++++ subdir/\n"
        h_line = b"hf. . . . . . . path/to/hardlink\n"
        _, log_calls, _ = await self._run_stream([c_line + h_line])

        full_logs = [msg for _, level, msg in log_calls if level == LogLevel.FULL]
        assert any("subdir/" in msg for msg in full_logs), "Created-type ('c') line must be logged at FULL"
        assert any("hardlink" in msg for msg in full_logs), "Hard-link-type ('h') line must be logged at FULL"


@pytest.mark.asyncio
class TestExecuteDryRun:
    """execute() in dry-run mode: rsync runs with --dry-run."""

    async def test_dry_run_rsync_command_includes_dry_run_flag(self) -> None:
        """In dry-run mode, the rsync command passed to start_process contains --dry-run."""
        ctx = make_context(dry_run=True)
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        await job.execute()

        called_cmd: str = ctx.source.start_process.call_args[0][0]
        assert "--dry-run" in called_cmd


@pytest.mark.asyncio
class TestExecuteNormalMode:
    """execute() in normal mode: rsync transfer and exit-code handling."""

    async def test_normal_mode_does_not_add_dry_run_flag(self) -> None:
        """In normal mode, the rsync command does NOT contain --dry-run."""
        ctx = make_context()
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        await job.execute()

        called_cmd: str = ctx.source.start_process.call_args[0][0]
        assert "--dry-run" not in called_cmd

    async def test_non_zero_rsync_exit_raises(self) -> None:
        """A non-zero rsync exit code causes execute() to raise RuntimeError."""
        ctx = make_context()
        fake_proc = make_fake_process(exit_code=23, stderr="partial transfer due to error")
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        with pytest.raises(RuntimeError):
            await job.execute()

    async def test_non_zero_rsync_exit_logs_critical(self, caplog: pytest.LogCaptureFixture) -> None:
        """A non-zero rsync exit causes a CRITICAL log (level 50) before raising."""
        ctx = make_context()
        fake_proc = make_fake_process(exit_code=23, stderr="partial transfer due to error")
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        with caplog.at_level(logging.CRITICAL, logger="pcswitcher.jobs.base"), pytest.raises(RuntimeError):
            await job.execute()

        # A CRITICAL-level record must have been emitted
        assert any(r.levelno == LogLevel.CRITICAL for r in caplog.records)


# ---------------------------------------------------------------------------
# First-sync overwrite confirmation (ADR-015 refinement)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFirstSyncDetection:
    """_target_is_first_sync classifies the target from its sync-history read."""

    async def test_empty_history_is_first_sync(self) -> None:
        """Empty stdout from the history read → first sync."""
        ctx = make_context(first_sync=True)
        job = FolderSyncJob(ctx)
        assert await job._target_is_first_sync() is True

    async def test_failed_read_is_first_sync(self) -> None:
        """Non-zero exit (file absent) → first sync."""
        ctx = make_context()
        ctx.target.run_command = AsyncMock(return_value=CommandResult(exit_code=1, stdout="", stderr=""))
        job = FolderSyncJob(ctx)
        assert await job._target_is_first_sync() is True

    async def test_corrupt_history_is_first_sync(self) -> None:
        """Unparsable history JSON → first sync (safety-first)."""
        ctx = make_context()
        ctx.target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="not json", stderr=""))
        job = FolderSyncJob(ctx)
        assert await job._target_is_first_sync() is True

    async def test_readable_history_is_not_first_sync(self) -> None:
        """Valid history JSON → not a first sync."""
        ctx = make_context()  # default: readable history
        job = FolderSyncJob(ctx)
        assert await job._target_is_first_sync() is False


@pytest.mark.asyncio
class TestFirstSyncConfirmation:
    """execute() confirms the overwrite before the first-ever destructive transfer."""

    async def test_interactive_yes_proceeds(self) -> None:
        """First sync + user confirms → rsync runs."""
        confirmer = FakeConfirmer(response=True)
        ctx = make_context(first_sync=True, confirmer=confirmer)
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        await job.execute()

        assert len(confirmer.calls) == 1
        assert confirmer.calls[0]["allow_flag"] == "--allow-first-sync"
        ctx.source.start_process.assert_called_once()

    async def test_interactive_no_aborts(self) -> None:
        """First sync + user declines → RuntimeError, no rsync."""
        confirmer = FakeConfirmer(response=False)
        ctx = make_context(first_sync=True, confirmer=confirmer)
        ctx.source.start_process = AsyncMock(return_value=make_fake_process())

        job = FolderSyncJob(ctx)
        with pytest.raises(RuntimeError, match="--allow-first-sync"):
            await job.execute()

        ctx.source.start_process.assert_not_called()

    async def test_non_interactive_without_flag_aborts(self) -> None:
        """First sync, no TTY, no --allow-first-sync → RuntimeError via the real confirmer."""
        console = MagicMock()
        ui = MagicMock()
        confirmer = TerminalUIConfirmer(console, ui)
        ctx = make_context(first_sync=True, allow_first_sync=False, confirmer=confirmer)
        ctx.source.start_process = AsyncMock(return_value=make_fake_process())

        job = FolderSyncJob(ctx)
        with patch.object(sys, "stdin", _mock_isatty(False)), pytest.raises(RuntimeError, match="--allow-first-sync"):
            await job.execute()

        ctx.source.start_process.assert_not_called()

    async def test_non_interactive_with_flag_proceeds(self) -> None:
        """First sync, no TTY, --allow-first-sync set → auto-approved, rsync runs."""
        console = MagicMock()
        ui = MagicMock()
        confirmer = TerminalUIConfirmer(console, ui)
        ctx = make_context(first_sync=True, allow_first_sync=True, confirmer=confirmer)
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        with patch.object(sys, "stdin", _mock_isatty(False)):
            await job.execute()

        ctx.source.start_process.assert_called_once()

    async def test_dry_run_first_sync_proceeds_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """First sync under --dry-run logs a WARNING and proceeds without prompting."""
        confirmer = FakeConfirmer(response=False)  # would abort if consulted
        ctx = make_context(first_sync=True, dry_run=True, confirmer=confirmer)
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.base"):
            await job.execute()

        # Confirmer must NOT be consulted in dry-run; rsync (with --dry-run) still runs.
        assert confirmer.calls == []
        ctx.source.start_process.assert_called_once()
        assert any(r.levelno == LogLevel.WARNING and "First sync" in r.getMessage() for r in caplog.records)

    async def test_non_first_sync_does_not_prompt(self) -> None:
        """A target with readable history is not a first sync → confirmer is never consulted."""
        confirmer = FakeConfirmer(response=False)  # would abort if wrongly consulted
        ctx = make_context(confirmer=confirmer)  # default: readable history
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        job = FolderSyncJob(ctx)
        await job.execute()

        assert confirmer.calls == []
        ctx.source.start_process.assert_called_once()
