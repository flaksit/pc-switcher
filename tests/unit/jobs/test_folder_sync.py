"""Unit tests for FolderSyncJob.

Tests cover: active-folder selection, validate() preflight (sudo rsync, acl, folder
existence), and the target-divergence guard (first-sync, untouched, diverged, dry-run,
allow_divergence, consecutive-source-sync cases).

All executor interactions are mocked; no real SSH connections are made.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.folder_sync import FolderEntry, FolderSyncJob
from pcswitcher.models import CommandResult, Host

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

    async def test_execute_stub_raises(self) -> None:
        """execute() raises NotImplementedError as a clear stub marker."""
        ctx = make_context()
        job = FolderSyncJob(ctx)
        with pytest.raises(NotImplementedError):
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
        import re

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


@pytest.mark.asyncio
class TestDivergenceGuard:
    """validate() target-divergence guard (D-06/D-07/D-08/D-18)."""

    def _make_target_cmds(
        self,
        *,
        findmnt_output: str = _FINDMNT_HOME,
        btrfs_show_output: str = _BTRFS_SHOW_HOME,
        find_new_output: str = _FIND_NEW_EMPTY,
    ):
        """Build a target run_command side_effect for divergence tests."""

        async def side_effect(cmd: str, **kw: object) -> CommandResult:
            if "findmnt" in cmd:
                return CommandResult(exit_code=0, stdout=findmnt_output, stderr="")
            if "btrfs subvolume show" in cmd:
                return CommandResult(exit_code=0, stdout=btrfs_show_output, stderr="")
            if "find-new" in cmd:
                return CommandResult(exit_code=0, stdout=find_new_output, stderr="")
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

    async def test_no_btrfs_subvolume_skips_divergence_check(self) -> None:
        """When findmnt cannot find a btrfs subvolume, divergence check is skipped with a WARNING (Open Q3)."""
        ctx = make_context(config={"folders": [{"path": "/root"}]})

        async def target_side_effect(cmd: str, **kw: object) -> CommandResult:
            if "findmnt" in cmd:
                # Simulate non-btrfs or unmounted path
                return CommandResult(exit_code=1, stdout="", stderr="not found")
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.target.run_command = AsyncMock(side_effect=target_side_effect)

        with patch("pcswitcher.jobs.folder_sync.sync_history.get_target_generation", return_value=900):
            job = FolderSyncJob(ctx)
            # Should not crash and should not emit a divergence error
            errors = await job.validate()

        assert not any("diverge" in e.message.lower() for e in errors)
