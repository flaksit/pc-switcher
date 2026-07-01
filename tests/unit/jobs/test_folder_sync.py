"""Unit tests for FolderSyncJob.

Tests cover: active-folder selection, validate() preflight (sudo rsync, acl, folder
existence), and the target-divergence guard (first-sync, untouched, diverged, dry-run,
allow_divergence, consecutive-source-sync cases).

All executor interactions are mocked; no real SSH connections are made.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher import sync_history
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


def make_context(
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
    allow_divergence: bool = False,
) -> JobContext:
    """Create a JobContext with mocked source/target executors."""
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    target = MagicMock()
    target.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
    return JobContext(
        config=config if config is not None else {"folders": [{"path": "/home"}]},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
        allow_divergence=allow_divergence,
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
        """execute() no longer raises NotImplementedError — it is implemented in plan 05."""
        # This test documents the transition; once execute() is fully implemented it will
        # run rsync. For isolation we mock start_process to avoid real subprocesses.
        ctx = make_context()

        async def fake_chunks(*_: object, **__: object):  # type: ignore[no-untyped-def]
            return
            yield b""  # make it an async generator

        fake_proc = MagicMock()
        fake_proc.read_stdout_chunks = fake_chunks
        fake_proc.wait_result = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        # Mock subvolume resolution so the divergence marker write succeeds
        async def target_cmd_ok(cmd: str, **_: object) -> CommandResult:
            if "findmnt" in cmd:
                return CommandResult(exit_code=0, stdout="/home", stderr="")
            if "btrfs subvolume show" in cmd:
                return CommandResult(exit_code=0, stdout="Generation: 100\n", stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.target.run_command = AsyncMock(side_effect=target_cmd_ok)

        with patch("pcswitcher.jobs.folder_sync.sync_history.set_target_generation"):
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
# Task 2: Target-divergence guard
# ---------------------------------------------------------------------------

# btrfs subvolume show output for /home (own subvolume)
_BTRFS_SHOW_HOME = """\
Name: \t\t\thome
UUID: \t\t\tabc123
Parent UUID: \t\t-
Received UUID: \t\t-
Creation time: \t\t2024-01-01 00:00:00 +0000
Subvolume ID: \t\t256
Generation: \t\t1000
Gen at creation: \t1
Parent ID: \t\t5
Top level ID: \t\t5
Flags: \t\t\t-
Send transid: \t\t0
Send time: \t\t2024-01-01 00:00:00 +0000
Receive transid: \t0
Receive time: \t\t-
Snapshot(s):
"""

# findmnt output when /home is its own subvolume
_FINDMNT_HOME = "/home"

# find-new output with no changes (target untouched)
_FIND_NEW_EMPTY = "transid marker was 1000\n"

# find-new output showing a changed file
_FIND_NEW_WITH_CHANGES = (
    "inode 1234 file offset 0 len 4096 disk start 0 offset 0 gen 1001 flags UNKNOWN path/to/changed/file\n"
    "transid marker was 1000\n"
)

# find-new output where ONLY the pc-switcher sync-history/lock file changed.
# These are written by the post-sync baseline capture and role-record steps on the
# target's @home after the baseline is taken; they are not user divergence (CR-01).
_FIND_NEW_TOOLSTATE_ONLY = (
    "inode 5678 file offset 0 len 4096 disk start 0 offset 0 gen 1001 flags UNKNOWN "
    "janfr/.local/share/pc-switcher/sync-history.json\n"
    "transid marker was 1000\n"
)

# find-new output where ONLY the pc-switcher config.yaml changed.
# Written by Phase-8 config sync (_copy_config_to_target) before job execution;
# also not user divergence for the empty-prefix subvolume-root case (CR-01).
_FIND_NEW_CONFIG_ONLY = (
    "inode 9012 file offset 0 len 4096 disk start 0 offset 0 gen 1001 flags UNKNOWN "
    "janfr/.config/pc-switcher/config.yaml\n"
    "transid marker was 1000\n"
)


@pytest.mark.asyncio
class TestDivergenceGuard:
    """validate() target-divergence guard (D-06/D-07/D-08/D-18)."""

    def _make_target_cmds(
        self,
        *,
        findmnt_output: str = _FINDMNT_HOME,
        findmnt_exit: int = 0,
        btrfs_show_output: str = _BTRFS_SHOW_HOME,
        find_new_output: str = _FIND_NEW_EMPTY,
        find_new_exit: int = 0,
    ):
        """Build a target run_command side_effect for divergence tests."""

        async def side_effect(cmd: str, **kw: object) -> CommandResult:
            if "findmnt" in cmd:
                return CommandResult(exit_code=findmnt_exit, stdout=findmnt_output, stderr="")
            if "btrfs subvolume show" in cmd:
                return CommandResult(exit_code=0, stdout=btrfs_show_output, stderr="")
            if "find-new" in cmd:
                stderr = "btrfs error" if find_new_exit != 0 else ""
                return CommandResult(exit_code=find_new_exit, stdout=find_new_output, stderr=stderr)
            # Default: succeed (rsync version, acl checks)
            return CommandResult(exit_code=0, stdout="", stderr="")

        return side_effect

    async def test_first_sync_no_stored_baseline(self) -> None:
        """No divergence error on first sync (stored baseline is None)."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds())

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=None):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not any(e.host == Host.TARGET and "diverge" in e.message.lower() for e in errors)

    async def test_unmodified_target_no_error(self) -> None:
        """No divergence error when find-new reports no changed files."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_output=_FIND_NEW_EMPTY))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not any("diverge" in e.message.lower() for e in errors)

    async def test_diverged_target_raises_error(self) -> None:
        """ValidationError for HOST.TARGET when target has changed since last sync (default mode)."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_output=_FIND_NEW_WITH_CHANGES))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert any(e.host == Host.TARGET and "diverge" in e.message.lower() for e in errors)

    async def test_diverged_target_dry_run_warning_no_error(self) -> None:
        """Under dry_run=True, a detected divergence is logged at WARNING and does NOT block."""
        ctx = make_context(config={"folders": [{"path": "/home"}]}, dry_run=True)
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_output=_FIND_NEW_WITH_CHANGES))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not any("diverge" in e.message.lower() for e in errors)

    async def test_diverged_target_allow_divergence_no_error(self) -> None:
        """Under allow_divergence=True, a detected divergence is logged at WARNING and does NOT block."""
        ctx = make_context(config={"folders": [{"path": "/home"}]}, allow_divergence=True)
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_output=_FIND_NEW_WITH_CHANGES))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not any("diverge" in e.message.lower() for e in errors)

    async def test_consecutive_source_syncs_ok(self) -> None:
        """Two consecutive source→target syncs with untouched target produce no divergence error (D-07)."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_output=_FIND_NEW_EMPTY))

        # Simulate a stored baseline that matches current state (target untouched)
        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors_first = await job.validate()

        assert not any("diverge" in e.message.lower() for e in errors_first)

        # Second validate call (same target, still untouched)
        ctx2 = make_context(config={"folders": [{"path": "/home"}]})
        ctx2.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_output=_FIND_NEW_EMPTY))
        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job2 = FolderSyncJob(ctx2)
            errors_second = await job2.validate()

        assert not any("diverge" in e.message.lower() for e in errors_second)

    async def test_divergence_commands_shell_quote_folder_path(self) -> None:
        """Divergence guard commands that embed folder.path use shlex.quote (T-04-01)."""
        ctx = make_context(config={"folders": [{"path": "/home/user name"}]})
        target_cmds: list[str] = []

        async def record_target(cmd: str, **kw: object) -> CommandResult:
            target_cmds.append(cmd)
            if "findmnt" in cmd:
                return CommandResult(exit_code=0, stdout="/home/user name", stderr="")
            if "btrfs subvolume show" in cmd:
                return CommandResult(exit_code=0, stdout=_BTRFS_SHOW_HOME, stderr="")
            if "find-new" in cmd:
                return CommandResult(exit_code=0, stdout=_FIND_NEW_EMPTY, stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.target.run_command = AsyncMock(side_effect=record_target)

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            await job.validate()

        path_cmds = [c for c in target_cmds if "/home/user name" in c]
        assert all("'/home/user name'" in c or '"/home/user name"' in c for c in path_cmds), (
            f"Unquoted path found in: {path_cmds}"
        )

    async def test_no_btrfs_subvolume_no_baseline_proceeds(self) -> None:
        """When findmnt cannot find a btrfs subvolume AND there is no stored baseline, sync proceeds (Open Q3).

        No-baseline + unresolvable mount = never synced or first sync to a non-btrfs path.
        The guard is fail-open for this case per RESEARCH Open Q3.
        """
        ctx = make_context(config={"folders": [{"path": "/root"}]})

        async def target_side_effect(cmd: str, **kw: object) -> CommandResult:
            if "findmnt" in cmd:
                # Simulate non-btrfs or unmounted path
                return CommandResult(exit_code=1, stdout="", stderr="not found")
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.target.run_command = AsyncMock(side_effect=target_side_effect)

        # No stored baseline → first sync path → proceed regardless of subvolume resolution
        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=None):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not any("diverge" in e.message.lower() for e in errors)

    async def test_no_btrfs_with_stored_baseline_fails_closed(self) -> None:
        """When findmnt fails AND a baseline is stored, validate() must fail closed (CR-02).

        A stored baseline means a previous sync succeeded and the guard is active.
        If the subvolume cannot be resolved on the current run, the target state is
        UNVERIFIABLE — it is unsafe to let rsync --delete proceed.
        """
        ctx = make_context(config={"folders": [{"path": "/root"}]})
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(findmnt_exit=1))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert any(e.host == Host.TARGET for e in errors), (
            "Stored baseline + unresolvable subvolume must produce a blocking error (CR-02)"
        )

    async def test_toolstate_write_under_empty_prefix_not_divergence(self) -> None:
        """For empty prefix (subvolume root), a find-new line under .local/share/pc-switcher/ is NOT divergence.

        pc-switcher's own post-sync writes (sync-history.json, lock) bump @home after
        the baseline is captured.  For the default /home config these are tool state,
        not user divergence (CR-01).
        """
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(
            side_effect=self._make_target_cmds(find_new_output=_FIND_NEW_TOOLSTATE_ONLY)
        )

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not any("diverge" in e.message.lower() for e in errors), (
            "Tool-state write under empty prefix must not trigger divergence (CR-01)"
        )

    async def test_config_write_under_empty_prefix_not_divergence(self) -> None:
        """For empty prefix (subvolume root), a find-new line under .config/pc-switcher/ is NOT divergence.

        Phase-8 config sync writes ~/.config/pc-switcher/config.yaml on the target BEFORE
        the folder-sync job runs, bumping @home. For the default /home config this is
        also tool state, not user divergence (CR-01).
        """
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_output=_FIND_NEW_CONFIG_ONLY))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not any("diverge" in e.message.lower() for e in errors), (
            "Phase-8 config.yaml write under empty prefix must not trigger divergence (CR-01)"
        )

    async def test_toolstate_path_under_nonempty_prefix_is_divergence(self) -> None:
        """For a non-empty prefix, a pc-switcher-looking path IS real divergence (Codex HIGH #2).

        The tool-state filter is ONLY valid for the empty-prefix / subvolume-root case.
        For a non-empty synced root (e.g. /home/user/synced), a change under a
        .local/share/pc-switcher/ subpath is user data and must still block the sync.
        """
        # Synced folder is a subdirectory, not the subvolume root → non-empty prefix
        ctx = make_context(config={"folders": [{"path": "/home/janfr/synced"}]})

        # findmnt returns /home (the actual subvolume mount), so prefix = "janfr/synced"
        find_new_nonempty_toolstate = (
            "inode 5678 file offset 0 len 4096 disk start 0 offset 0 gen 1001 flags UNKNOWN "
            "janfr/synced/.local/share/pc-switcher/sync-history.json\n"
            "transid marker was 1000\n"
        )

        async def target_side_effect(cmd: str, **kw: object) -> CommandResult:
            if "findmnt" in cmd:
                return CommandResult(exit_code=0, stdout="/home", stderr="")
            if "find-new" in cmd:
                return CommandResult(exit_code=0, stdout=find_new_nonempty_toolstate, stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.target.run_command = AsyncMock(side_effect=target_side_effect)

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert any(e.host == Host.TARGET and "diverge" in e.message.lower() for e in errors), (
            "Non-empty-prefix pc-switcher-looking path must be treated as real divergence (Codex HIGH #2)"
        )

    async def test_unverifiable_with_baseline_fails_closed(self) -> None:
        """When find-new exits non-zero AND a baseline is stored, validate() must fail closed (CR-02)."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_exit=1))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert any(e.host == Host.TARGET for e in errors), (
            "Stored baseline + failed find-new must produce a blocking ValidationError (CR-02)"
        )

    async def test_unverifiable_under_allow_divergence_proceeds(self) -> None:
        """Under allow_divergence=True, an UNVERIFIABLE result is logged at WARNING and does not block."""
        ctx = make_context(config={"folders": [{"path": "/home"}]}, allow_divergence=True)
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_exit=1))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not errors, "allow_divergence must suppress the blocking error even for UNVERIFIABLE"

    async def test_unverifiable_under_dry_run_proceeds(self) -> None:
        """Under dry_run=True, an UNVERIFIABLE result is logged at WARNING and does not block."""
        ctx = make_context(config={"folders": [{"path": "/home"}]}, dry_run=True)
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_cmds(find_new_exit=1))

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert not errors, "dry_run must suppress the blocking error even for UNVERIFIABLE"

    async def test_unknown_generation_baseline_fails_closed(self) -> None:
        """When the stored generation is UNKNOWN_GENERATION, validate() must fail closed without querying target.

        UNKNOWN_GENERATION means the previous run captured no reliable generation.
        The guard short-circuits: it does NOT query the target (no find-new call)
        and immediately returns UNVERIFIABLE → blocking error (CR-02/WR-02 sentinel read path).
        """
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        target_cmds: list[str] = []

        async def recording_target(cmd: str, **kw: object) -> CommandResult:
            target_cmds.append(cmd)
            if "findmnt" in cmd:
                return CommandResult(exit_code=0, stdout="/home", stderr="")
            if "btrfs subvolume show" in cmd:
                return CommandResult(exit_code=0, stdout=_BTRFS_SHOW_HOME, stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.target.run_command = AsyncMock(side_effect=recording_target)

        with patch(
            "pcswitcher.jobs.folder_sync.sync_history.get_target_generation",
            return_value=sync_history.UNKNOWN_GENERATION,
        ):
            job = FolderSyncJob(ctx)
            errors = await job.validate()

        assert any(e.host == Host.TARGET for e in errors), (
            "UNKNOWN_GENERATION sentinel must produce a blocking error (CR-02/WR-02)"
        )
        # find-new must NOT be called — the sentinel short-circuits before any target query
        assert not any("find-new" in cmd for cmd in target_cmds), (
            "UNKNOWN_GENERATION sentinel must short-circuit without querying target via find-new"
        )


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
        assert any("subdir/" in msg for msg in full_logs), (
            "Created-type ('c') line must be logged at FULL"
        )
        assert any("hardlink" in msg for msg in full_logs), (
            "Hard-link-type ('h') line must be logged at FULL"
        )


@pytest.mark.asyncio
class TestExecuteDryRun:
    """execute() in dry-run mode: rsync runs with --dry-run; no marker is recorded."""

    async def test_dry_run_does_not_call_set_target_generation(self) -> None:
        """In dry-run mode, execute() never calls set_target_generation (D-12)."""
        ctx = make_context(dry_run=True)
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        with patch("pcswitcher.jobs.folder_sync.sync_history.set_target_generation") as mock_set:
            job = FolderSyncJob(ctx)
            await job.execute()

        mock_set.assert_not_called()

    async def test_dry_run_rsync_command_includes_dry_run_flag(self) -> None:
        """In dry-run mode, the rsync command passed to start_process contains --dry-run."""
        ctx = make_context(dry_run=True)
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        with patch("pcswitcher.jobs.folder_sync.sync_history.set_target_generation"):
            job = FolderSyncJob(ctx)
            await job.execute()

        called_cmd: str = ctx.source.start_process.call_args[0][0]
        assert "--dry-run" in called_cmd


@pytest.mark.asyncio
class TestExecuteNormalMode:
    """execute() in normal mode: rsync runs; divergence baseline is recorded per folder."""

    def _make_target_for_execute(
        self,
        *,
        home_mount: str = "/home",
        root_mount: str | None = None,
        btrfs_show_exit: int = 0,
    ):
        """Build a target run_command side_effect for execute() tests."""

        async def side_effect(cmd: str, **kw: object) -> CommandResult:
            if "findmnt" in cmd:
                if "/home" in cmd:
                    return CommandResult(exit_code=0, stdout=home_mount, stderr="")
                if root_mount and "/root" in cmd:
                    return CommandResult(exit_code=0, stdout=root_mount, stderr="")
            if "btrfs subvolume show" in cmd:
                if btrfs_show_exit != 0:
                    return CommandResult(exit_code=btrfs_show_exit, stdout="", stderr="btrfs error")
                return CommandResult(exit_code=0, stdout="Generation: 1234\n", stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        return side_effect

    async def test_normal_mode_calls_set_target_generation_once_per_folder(self) -> None:
        """After a successful sync, set_target_generation is called once per active folder."""
        ctx = make_context(config={"folders": [{"path": "/home"}, {"path": "/root"}]})
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)
        ctx.target.run_command = AsyncMock(
            side_effect=self._make_target_for_execute(home_mount="/home", root_mount="/")
        )

        with patch("pcswitcher.jobs.folder_sync.sync_history.set_target_generation") as mock_set:
            job = FolderSyncJob(ctx)
            await job.execute()

        # Two active folders → two set_target_generation calls
        assert mock_set.call_count == 2

    async def test_normal_mode_does_not_add_dry_run_flag(self) -> None:
        """In normal mode, the rsync command does NOT contain --dry-run."""
        ctx = make_context()
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_for_execute())

        with patch("pcswitcher.jobs.folder_sync.sync_history.set_target_generation"):
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

    async def test_set_target_generation_not_called_on_rsync_failure(self) -> None:
        """If rsync fails, set_target_generation is never called (sync aborted)."""
        ctx = make_context()
        fake_proc = make_fake_process(exit_code=1, stderr="error")
        ctx.source.start_process = AsyncMock(return_value=fake_proc)

        with patch("pcswitcher.jobs.folder_sync.sync_history.set_target_generation") as mock_set:
            job = FolderSyncJob(ctx)
            with pytest.raises(RuntimeError):
                await job.execute()

        mock_set.assert_not_called()

    async def test_divergence_baseline_uses_target_subvolume_generation(self) -> None:
        """set_target_generation is called with the queried target subvolume generation."""
        ctx = make_context()
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_for_execute(home_mount="/home"))

        with patch("pcswitcher.jobs.folder_sync.sync_history.set_target_generation") as mock_set:
            job = FolderSyncJob(ctx)
            await job.execute()

        # Check the generation value (parsed from "Generation: 1234\n")
        call_args = mock_set.call_args_list[0]
        assert call_args[0][2] == 1234  # third positional arg is generation

    async def test_baseline_capture_failure_records_sentinel_and_does_not_raise(self) -> None:
        """execute() must not raise when post-transfer baseline capture fails; must write UNKNOWN_GENERATION.

        The rsync transfer already completed successfully — failing the job at this point
        would discard transferred data for no benefit.  Instead, write UNKNOWN_GENERATION
        so the NEXT run's divergence guard fails closed rather than silently skipping (WR-02).
        """
        ctx = make_context()
        fake_proc = make_fake_process()
        ctx.source.start_process = AsyncMock(return_value=fake_proc)
        # btrfs subvolume show fails → _get_subvolume_generation raises RuntimeError
        ctx.target.run_command = AsyncMock(side_effect=self._make_target_for_execute(btrfs_show_exit=1))

        with patch("pcswitcher.jobs.folder_sync.sync_history.set_target_generation") as mock_set:
            job = FolderSyncJob(ctx)
            # Must NOT raise — the rsync data transfer already completed
            await job.execute()

        # Sentinel must have been written for the folder so the next run fails closed
        assert mock_set.call_count == 1, "Expected exactly one set_target_generation call (the sentinel)"
        call_args = mock_set.call_args_list[0]
        assert call_args[0][2] == sync_history.UNKNOWN_GENERATION, (
            f"Expected UNKNOWN_GENERATION ({sync_history.UNKNOWN_GENERATION}) but got {call_args[0][2]}"
        )
