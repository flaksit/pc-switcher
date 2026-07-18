"""Unit tests for FolderSyncJob.

Tests cover: active-folder selection, validate() preflight (sudo rsync, acl, folder
existence), rsync command construction, and transfer streaming/exit-code handling.

All executor interactions are mocked; no real SSH connections are made.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.folder_sync import FolderEntry, FolderSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, LogLevel, ProgressUpdate

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
    target_username: str | None = None,
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
        target_username=target_username,
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
        """FolderEntry defaults enabled=True and filter_file=None."""
        entry = FolderEntry(path="/home")
        assert entry.enabled is True
        assert entry.filter_file is None

    def test_expanded_filter_file_none_when_unset(self) -> None:
        """expanded_filter_file() returns None when filter_file is unset."""
        entry = FolderEntry(path="/home")
        assert entry.expanded_filter_file() is None

    def test_expanded_filter_file_expands_home_and_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """expanded_filter_file() ~-expands and env-var-expands the configured path."""
        monkeypatch.setenv("HOME", "/fake/home")
        monkeypatch.setenv("MY_FILTER_DIR", "/fake/filters")
        entry = FolderEntry(path="/home", filter_file="~/x/home.filter")
        assert entry.expanded_filter_file() == "/fake/home/x/home.filter"

        entry2 = FolderEntry(path="/home", filter_file="$MY_FILTER_DIR/home.filter")
        assert entry2.expanded_filter_file() == "/fake/filters/home.filter"


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
# describe_first_sync_scope (ADR-015, gap-closure 01-15)
# ---------------------------------------------------------------------------


class TestDescribeFirstSyncScope:
    """FolderSyncJob.describe_first_sync_scope() self-describes its overwrite scope."""

    def test_populated_config_returns_scope(self) -> None:
        """Enabled folder paths + a mechanism phrase are returned for a populated config."""
        config = {"folders": [{"path": "/home"}, {"path": "/root"}]}

        scope = FolderSyncJob.describe_first_sync_scope(config)

        assert isinstance(scope, FirstSyncScope)
        assert scope.job_name == "folder_sync"
        assert scope.scope_items == ["/home", "/root"]
        assert scope.mechanism

    def test_disabled_folders_excluded(self) -> None:
        """A folder entry with enabled=False is excluded from scope_items."""
        config = {"folders": [{"path": "/home"}, {"path": "/root", "enabled": False}]}

        scope = FolderSyncJob.describe_first_sync_scope(config)

        assert scope is not None
        assert scope.scope_items == ["/home"]

    def test_empty_folders_returns_none(self) -> None:
        """No folders configured → None (nothing in scope)."""
        assert FolderSyncJob.describe_first_sync_scope({"folders": []}) is None

    def test_all_disabled_folders_returns_none(self) -> None:
        """Every folder disabled → None (nothing in scope)."""
        config = {"folders": [{"path": "/home", "enabled": False}]}
        assert FolderSyncJob.describe_first_sync_scope(config) is None

    def test_missing_folders_key_returns_none(self) -> None:
        """A config dict with no 'folders' key at all → None."""
        assert FolderSyncJob.describe_first_sync_scope({}) is None


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

    async def test_missing_filter_file(self) -> None:
        """validate() returns a Host.SOURCE ValidationError naming the filter file when it is absent."""
        ctx = make_context(config={"folders": [{"path": "/home", "filter_file": "/abs/home.filter"}]})
        ctx.source.run_command = AsyncMock(side_effect=fail_when("test -f", "no such file"))
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert any(e.host == Host.SOURCE and "home.filter" in e.message for e in errors)

    async def test_existing_filter_file_produces_no_error(self) -> None:
        """validate() returns no filter_file error when the file exists on source."""
        ctx = make_context(config={"folders": [{"path": "/home", "filter_file": "/abs/home.filter"}]})
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert not any("filter_file" in e.message for e in errors)

    async def test_filter_file_check_uses_expanded_path(self) -> None:
        """The test -f command for filter_file uses the expanded path, not a literal ~."""
        ctx = make_context(config={"folders": [{"path": "/home", "filter_file": "~/x.filter"}]})
        source_cmds: list[str] = []

        async def record(cmd: str, **kw: object) -> CommandResult:
            source_cmds.append(cmd)
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.source.run_command = AsyncMock(side_effect=record)
        job = FolderSyncJob(ctx)
        await job.validate()
        filter_checks = [c for c in source_cmds if "test -f" in c]
        assert filter_checks, "expected at least one test -f call"
        assert not any("~/x.filter" in c for c in filter_checks)
        expanded = FolderEntry(path="/home", filter_file="~/x.filter").expanded_filter_file()
        assert expanded is not None
        assert any(expanded in c for c in filter_checks)

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
        filter_file: str | None = None,
        dry_run: bool = False,
        target_username: str | None = "testuser",
        home: str = "/nonhome",
    ) -> str:
        # Default home is OUTSIDE any typical sync path, so the hardcoded runtime
        # excludes (which anchor to the invoking user's home) are absent unless a
        # test opts in by passing a `home` under `path`. This keeps the user-filter
        # assertions below deterministic regardless of the machine running them.
        ctx = make_context(config={"folders": [{"path": path}]}, target_username=target_username)
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path=path, filter_file=filter_file)
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path(home)):
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

    def test_rsync_forced_to_c_locale(self) -> None:
        """rsync runs under `env LC_ALL=C` so its progress2 byte counter is ungrouped."""
        cmd = self._build()
        assert "env LC_ALL=C rsync" in cmd

    def test_root_via_sudo_and_ssh_transport(self) -> None:
        """Command uses --rsync-path='sudo rsync' for remote root and an -e ssh option with -T and -l."""
        cmd = self._build()
        # Remote root via sudo (target side)
        assert "--rsync-path='sudo rsync'" in cmd
        # SSH transport with -T (no pseudo-tty) and explicit login user
        assert "-T" in cmd
        assert "-l testuser" in cmd

    def test_no_forbidden_flags(self) -> None:
        """Command never includes --delete-excluded or --checksum (D-06, D-14)."""
        cmd = self._build(filter_file="/abs/path with space/home.filter")
        assert "--delete-excluded" not in cmd
        assert "--checksum" not in cmd

    def test_no_built_in_per_dir_flags(self) -> None:
        """Command never enables rsync's own per-dir mechanisms (-F/-FF/-C/--cvs-exclude).

        ssh's own -F (config file flag) only appears when ~/.ssh/config exists; the
        default `home="/nonhome"` fixture has no such file, so it is absent here too.
        """
        cmd = self._build()
        tokens = cmd.split()
        assert "-F" not in tokens
        assert "-FF" not in tokens
        assert "-C" not in tokens
        assert "--cvs-exclude" not in cmd

    def test_merge_arg_ordering(self) -> None:
        """merge appears after runtime excludes and before dir-merge (GLOBAL-FIRST)."""
        cmd = self._build(
            path="/home", filter_file="/abs/home.filter", home="/home/alice"
        )  # home under path -> runtime excludes present
        idx_runtime = cmd.index(".local/share/pc-switcher")
        idx_merge = cmd.index("merge /abs/home.filter")
        idx_dir_merge = cmd.index("dir-merge /.pcswitcher-filter")
        assert idx_runtime < idx_merge < idx_dir_merge

    def test_no_merge_arg_when_no_filter_file_but_dir_merge_present(self) -> None:
        """No filter_file -> no central `merge` arg, but `dir-merge /.pcswitcher-filter` still present."""
        cmd = self._build(filter_file=None)
        assert "--filter='merge " not in cmd
        assert "--filter='dir-merge /.pcswitcher-filter'" in cmd

    def test_merge_arg_present_when_filter_file_set(self) -> None:
        """filter_file set -> `merge <expanded>` present in the command."""
        cmd = self._build(filter_file="/abs/home.filter")
        assert "merge /abs/home.filter" in cmd

    def test_dir_merge_always_present(self) -> None:
        """dir-merge /.pcswitcher-filter is present whether or not filter_file is set."""
        cmd_without = self._build(filter_file=None)
        cmd_with = self._build(filter_file="/abs/home.filter")
        assert "--filter='dir-merge /.pcswitcher-filter'" in cmd_without
        assert "--filter='dir-merge /.pcswitcher-filter'" in cmd_with

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
        """Paths and filter_file with special characters are shell-quoted (T-05-01)."""
        cmd = self._build(path="/home/user name", filter_file="/abs/path with space/home.filter")
        # The path with a space must be quoted in the command
        assert "/home/user name/" not in cmd or "'/home/user name/'" in cmd
        # A filter_file path with a space is shlex-quoted as a single argv token
        assert "'merge /abs/path with space/home.filter'" in cmd


# ---------------------------------------------------------------------------
# Hardcoded runtime-file excludes (ADR-016)
# ---------------------------------------------------------------------------


class TestRuntimeExcludeFilters:
    """pc-switcher's own runtime files are always excluded from folder sync.

    These are the ONLY hardcoded excludes; they anchor to the invoking user's
    home and only apply when that home is inside the synced folder.
    """

    _RELPATHS = (
        ".local/share/pc-switcher",
        ".local/share/uv/tools/pcswitcher",
        ".local/bin/pc-switcher",
    )

    def _filters(self, folder_path: str, home: str) -> list[str]:
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path(home)):
            return FolderSyncJob._runtime_exclude_filters(folder_path)  # pyright: ignore[reportPrivateUsage]

    def test_home_under_synced_folder_anchors_to_user_subdir(self) -> None:
        """Syncing /home anchors each runtime path under the user's subdir."""
        filters = self._filters("/home", "/home/alice")
        assert filters == [f"--filter={shlex.quote(f'- /alice/{rel}')}" for rel in self._RELPATHS]

    def test_folder_equals_home_anchors_to_root(self) -> None:
        """Syncing the home directory itself anchors runtime paths at the transfer root."""
        filters = self._filters("/home/alice", "/home/alice")
        assert filters == [f"--filter={shlex.quote(f'- /{rel}')}" for rel in self._RELPATHS]

    def test_trailing_slash_on_folder_is_ignored(self) -> None:
        """A trailing slash on the folder path does not change anchoring."""
        assert self._filters("/home/", "/home/alice") == self._filters("/home", "/home/alice")

    def test_home_outside_synced_folder_yields_no_filters(self) -> None:
        """Syncing /root as a normal user (home under /home) adds no runtime excludes."""
        assert self._filters("/root", "/home/alice") == []

    def test_runtime_excludes_precede_user_excludes_in_command(self) -> None:
        """Protective excludes appear before the central merge filter so an include can't re-expose them."""
        ctx = make_context(config={"folders": [{"path": "/home"}]}, target_username="testuser")
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path="/home", filter_file="/abs/home.filter")
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path("/home/alice")):
            cmd = job._build_rsync_cmd(folder, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        assert cmd.index("/alice/.local/share/pc-switcher") < cmd.index("merge /abs/home.filter")


# ---------------------------------------------------------------------------
# SSH transport credential tests
# ---------------------------------------------------------------------------


class TestBuildRsyncCmdSSHTransport:
    """Verify explicit SSH credentials in the -e transport of _build_rsync_cmd.

    When sudo launches rsync as root, the spawned ssh binary resolves ~/.ssh
    from root's passwd entry (/root/.ssh), ignoring $HOME.  The fix passes
    the invoking user's credentials explicitly via -l, -i, -o UserKnownHostsFile=,
    and optionally -F.  These tests control HOME and create fake ~/.ssh files so
    assertions are deterministic regardless of the test runner's actual dotfiles.
    """

    def _build_in_fake_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        target_username: str | None = "alice",
        ssh_files: list[str] | None = None,
    ) -> str:
        """Create a controlled fake ~/.ssh, then build the rsync command."""
        # Redirect Path.home() to tmp_path via $HOME (Path.home() reads $HOME on Linux).
        monkeypatch.setenv("HOME", str(tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        for fname in ssh_files or []:
            (ssh_dir / fname).write_text("placeholder")

        ctx = make_context(
            config={"folders": [{"path": "/home"}]},
            target_username=target_username,
        )
        job = FolderSyncJob(ctx)
        return job._build_rsync_cmd(FolderEntry(path="/home"), False)

    def test_target_username_from_context_used_as_l_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """target_username from context appears as -l <user> in the ssh command."""
        cmd = self._build_in_fake_home(tmp_path, monkeypatch, target_username="alice")
        assert "-l alice" in cmd

    def test_falls_back_to_getpass_when_target_username_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When context.target_username is None, getpass.getuser() fills the -l flag."""
        monkeypatch.setattr("getpass.getuser", lambda: "fallbackuser")
        cmd = self._build_in_fake_home(tmp_path, monkeypatch, target_username=None)
        assert "-l fallbackuser" in cmd

    def test_identity_file_included_when_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A present ~/.ssh/id_ed25519 produces -i <path> in the ssh command."""
        cmd = self._build_in_fake_home(tmp_path, monkeypatch, target_username="alice", ssh_files=["id_ed25519"])
        assert "-i" in cmd
        assert "id_ed25519" in cmd

    def test_no_identity_flag_when_no_keys_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no default key files exist under ~/.ssh, no -i key paths appear."""
        cmd = self._build_in_fake_home(tmp_path, monkeypatch, target_username="alice", ssh_files=[])
        # Assert by key name, not by the bare flag substring, because --numeric-ids
        # and --info in the rsync flags also contain "-i" as a substring.
        assert "id_ed25519" not in cmd
        assert "id_ecdsa" not in cmd
        assert "id_rsa" not in cmd

    def test_known_hosts_option_when_file_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A present ~/.ssh/known_hosts produces -o UserKnownHostsFile=<path>."""
        cmd = self._build_in_fake_home(tmp_path, monkeypatch, target_username="alice", ssh_files=["known_hosts"])
        assert "UserKnownHostsFile=" in cmd
        assert "known_hosts" in cmd

    def test_no_known_hosts_option_when_file_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ~/.ssh/known_hosts is absent, UserKnownHostsFile does not appear."""
        cmd = self._build_in_fake_home(tmp_path, monkeypatch, target_username="alice", ssh_files=[])
        assert "UserKnownHostsFile" not in cmd

    def test_ssh_config_flag_when_config_present(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A present ~/.ssh/config produces -F <path> in the ssh command."""
        cmd = self._build_in_fake_home(tmp_path, monkeypatch, target_username="alice", ssh_files=["config"])
        assert "-F" in cmd
        assert "config" in cmd

    def test_no_ssh_config_flag_when_config_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """-F is absent when ~/.ssh/config does not exist (ssh errors on -F <missing>)."""
        cmd = self._build_in_fake_home(tmp_path, monkeypatch, target_username="alice", ssh_files=[])
        assert "-F" not in cmd


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

    async def test_parse_size_to_bytes_tolerates_thousands_separators(self) -> None:
        """A locale-grouped byte counter parses instead of aborting the sync (WR-01).

        Under a grouping locale (e.g. LC_NUMERIC=nl_BE) rsync's progress2 counter
        is printed with thousands separators, e.g. '80.153.795.479'.  The parser
        must strip the grouping rather than raise on float().
        """
        assert FolderSyncJob._parse_size_to_bytes("80.153.795.479") == 80153795479
        assert FolderSyncJob._parse_size_to_bytes("80,153,795,479") == 80153795479

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
