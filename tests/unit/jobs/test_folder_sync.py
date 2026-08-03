"""Unit tests for FolderSyncJob.

Tests cover: active-folder selection, validate() preflight (sudo rsync, acl, folder
existence), rsync command construction, and transfer streaming/exit-code handling.

All executor interactions are mocked; no real SSH connections are made.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.folder_sync import PASS_COPY, PASS_DELETE, PASS_MIRROR, FolderEntry, FolderSyncJob
from pcswitcher.jobs.vscode_state_sync import VSCODE_STATE_DB_RELPATHS
from pcswitcher.models import CommandResult, FirstSyncScope, Host, JobSkipped, LogLevel, ProgressUpdate

# VS Code state-DB exclude relpaths (main + .backup) as folder_sync emits them, derived the
# same way vscode_state_sync's vscode_state_exclude_paths() does — single source of truth.
_VSCODE_EXCLUDE_RELPATHS = tuple(rel + suffix for rel in VSCODE_STATE_DB_RELPATHS for suffix in ("", ".backup"))

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


def arch_reports(machine: str) -> Callable[..., CommandResult]:
    """Return a run_command side_effect that reports `machine` for `uname --machine`, success otherwise."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if "uname --machine" in cmd:
            return CommandResult(exit_code=0, stdout=machine, stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    return _side_effect


def make_context(
    config: dict[str, Any] | None = None,
    dry_run: bool = False,
    target_username: str | None = None,
    enabled_sync_jobs: dict[str, bool] | None = None,
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
        enabled_sync_jobs=enabled_sync_jobs,
    )


def _snap_listing(revision: str) -> str:
    """What the target's `snap list --all` prints for one snap `firefox` at `revision`."""
    return (
        "Name     Version  Rev   Tracking       Publisher  Notes\n"
        f"firefox  1.0      {revision}  latest/stable  moz        -\n"
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

    async def test_a_job_with_no_active_folders_is_skipped(self) -> None:
        """The schema's minItems: 1 forces an entry, not an ENABLED entry, so every folder
        can be disabled. Mirroring nothing is not a successful mirror.
        """
        ctx = make_context(config={"folders": [{"path": "/home", "enabled": False}]})
        job = FolderSyncJob(ctx)

        with pytest.raises(JobSkipped) as exc_info:
            await job.execute()

        assert exc_info.value.job_name == "folder_sync"
        ctx.source.start_process.assert_not_called()  # pyright: ignore[reportAttributeAccessIssue]


class TestValidateConfig:
    """validate_config() rejects folder paths rsync would resolve against the cwd."""

    def test_absolute_paths_accepted(self) -> None:
        config = {"folders": [{"path": "/home"}, {"path": "/root", "enabled": False}]}
        assert FolderSyncJob.validate_config(config) == []

    def test_relative_path_rejected(self) -> None:
        config = {"folders": [{"path": "/home"}, {"path": "home/janfr"}]}
        errors = FolderSyncJob.validate_config(config)
        assert len(errors) == 1
        assert errors[0].path == "folders.1.path"
        assert "must be absolute" in errors[0].message

    def test_unexpanded_tilde_rejected(self) -> None:
        """`path` is passed to rsync verbatim, so a leading ~ is not a valid path."""
        errors = FolderSyncJob.validate_config({"folders": [{"path": "~/dev"}]})
        assert len(errors) == 1
        assert "~/dev" in errors[0].message

    def test_schema_errors_short_circuit(self) -> None:
        """A path of the wrong type is reported by the schema, not the absolute check."""
        errors = FolderSyncJob.validate_config({"folders": [{"path": 12345}]})
        assert len(errors) == 1
        assert "must be absolute" not in errors[0].message


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
    """validate() enforces CPU-arch match, sudo rsync availability, acl package, and folder existence."""

    async def test_all_preflight_checks_pass(self) -> None:
        """When all preflight commands succeed, validate() returns no errors."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        # source and target run_command already return success by default
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert errors == []

    async def test_arch_mismatch_between_source_and_target(self) -> None:
        """Differing CPU architecture on source vs target is a validation error."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.source.run_command = AsyncMock(side_effect=arch_reports("x86_64"))
        ctx.target.run_command = AsyncMock(side_effect=arch_reports("aarch64"))
        job = FolderSyncJob(ctx)
        errors = await job.validate()
        assert any(e.host == Host.TARGET and "architecture" in e.message.lower() for e in errors)

    async def test_matching_arch_yields_no_error(self) -> None:
        """Identical CPU architecture on both hosts yields no validation error."""
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.source.run_command = AsyncMock(side_effect=arch_reports("x86_64"))
        ctx.target.run_command = AsyncMock(side_effect=arch_reports("x86_64"))
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
        for flag in ("--archive", "--acls", "--xattrs", "--hard-links", "--sparse"):
            assert flag in cmd
        assert "--numeric-ids" in cmd
        assert "--delete" in cmd
        assert "--info=progress2" in cmd
        assert "--partial" in cmd
        assert "--mkpath" in cmd

    def test_no_inc_recursive_present(self) -> None:
        """The file list is built up front so progress2 reports against the real total.

        Without it rsync interleaves the walk with the transfer, `to-chk` appears only
        for the last handful of files, and the percentage before that is computed
        against a growing denominator (#198).
        """
        assert "--no-inc-recursive" in self._build()
        assert "--no-inc-recursive" in self._build(dry_run=True)

    def test_no_locale_forcing(self) -> None:
        """No locale is forced on rsync — the progress2 parser tolerates any locale's
        thousands separator, so the counter's grouping does not need pinning (WR-01)."""
        cmd = self._build()
        assert "LC_ALL" not in cmd

    def test_root_via_sudo_and_ssh_transport(self) -> None:
        """Command uses --rsync-path='sudo rsync' for remote root and an --rsh ssh option with -T and -l."""
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


class TestBuildRsyncCmdDeleteToggle:
    """_build_rsync_cmd's `delete` flag drives the no-delete copy pass vs the deleting mirror.

    The copy pass (delete=False) is the same command minus `--delete`, so it applies the
    identical filter chain (central merge + dir-merge) and thus respects every filter exactly.
    """

    def _build(self, delete: bool, filter_file: str | None = "/abs/home.filter") -> str:
        ctx = make_context(config={"folders": [{"path": "/home"}]}, target_username="testuser")
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path="/home", filter_file=filter_file)
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path("/nonhome")):
            return job._build_rsync_cmd(folder, dry_run=False, delete=delete)

    def test_delete_present_by_default(self) -> None:
        """The mirror pass includes --delete."""
        assert "--delete" in self._build(delete=True)

    def test_delete_omitted_when_false(self) -> None:
        """The copy pass omits --delete (it transfers without removing anything)."""
        assert "--delete" not in self._build(delete=False)

    def test_copy_pass_keeps_the_full_filter_chain(self) -> None:
        """delete=False still emits the central merge and dir-merge, so filters are respected."""
        cmd = self._build(delete=False)
        assert "merge /abs/home.filter" in cmd
        assert "dir-merge /.pcswitcher-filter" in cmd


# ---------------------------------------------------------------------------
# Hardcoded global-first excludes: runtime state (ADR-017) + VS Code state DBs (ADR-018)
# ---------------------------------------------------------------------------


class TestRuntimeExcludeFilters:
    """pc-switcher's own runtime state is a global-first, non-overridable exclude.

    It anchors to the invoking user's home and only applies when that home is inside the
    synced folder (ADR-017). The VS Code state DBs are a SEPARATE exclude group owned by
    `vscode_state_sync` (see `TestVscodeStateExcludeFilters`).
    """

    def _filters(self, folder_path: str, home: str) -> list[str]:
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path(home)):
            return FolderSyncJob._runtime_exclude_filters(folder_path)  # pyright: ignore[reportPrivateUsage]

    def test_home_under_synced_folder_anchors_to_user_subdir(self) -> None:
        """Syncing /home anchors the runtime path under the user's subdir."""
        assert self._filters("/home", "/home/alice") == [
            f"--filter={shlex.quote('- /alice/.local/share/pc-switcher')}"
        ]

    def test_folder_equals_home_anchors_to_root(self) -> None:
        """Syncing the home directory itself anchors the runtime path at the transfer root."""
        assert self._filters("/home/alice", "/home/alice") == [
            f"--filter={shlex.quote('- /.local/share/pc-switcher')}"
        ]

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


class TestVscodeStateExcludeFilters:
    """The VS Code state DBs are excluded from the mirror via absolute paths that
    `vscode_state_sync` owns; folder_sync only translates each into a root-anchored,
    first-match filter for the folder being synced (ADR-018). Scope: the invoking user.
    """

    def _filters(self, folder_path: str, home: str) -> list[str]:
        # vscode_state_exclude_paths() reads Path.home() in the vscode module; patching
        # the shared pathlib.Path.home covers it (same class object).
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=Path(home)):
            return FolderSyncJob._vscode_state_exclude_filters(folder_path)  # pyright: ignore[reportPrivateUsage]

    def test_home_under_synced_folder_anchors_each_db_under_user_subdir(self) -> None:
        """Syncing /home anchors every VS Code state DB (main + .backup) under the user's subdir."""
        assert self._filters("/home", "/home/alice") == [
            f"--filter={shlex.quote(f'- /alice/{rel}')}" for rel in _VSCODE_EXCLUDE_RELPATHS
        ]

    def test_db_outside_synced_folder_is_skipped(self) -> None:
        """Syncing /root while the invoking user's home is /home/alice excludes nothing."""
        assert self._filters("/root", "/home/alice") == []

    def test_root_invoker_excludes_under_root(self) -> None:
        """Invoked as root (home /root): the /root sync excludes root's own VS Code state DBs."""
        assert self._filters("/root", "/root") == [
            f"--filter={shlex.quote(f'- /{rel}')}" for rel in _VSCODE_EXCLUDE_RELPATHS
        ]

    def test_each_editor_db_and_backup_excluded_before_merge(self) -> None:
        """Both state.vscdb and its .backup are excluded for each editor, before the merge filter."""
        ctx = make_context(config={"folders": [{"path": "/home"}]}, target_username="testuser")
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path="/home", filter_file="/abs/home.filter")
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path("/home/alice")):
            cmd = job._build_rsync_cmd(folder, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        for editor in ("Code", "Antigravity", "Cursor", "VSCodium"):
            main = f"/alice/.config/{editor}/User/globalStorage/state.vscdb"
            backup = main + ".backup"
            assert main in cmd
            assert backup in cmd
            assert cmd.index(main) < cmd.index("merge /abs/home.filter")
            assert cmd.index(backup) < cmd.index("merge /abs/home.filter")


class TestDecisionFileExcludeFilters:
    """Every manager's machine-local decision file (`~/.config/pc-switcher/*.decisions.yaml`)
    is excluded from the mirror via a home-relative GLOB that `packages.state` owns;
    folder_sync only translates it into a root-anchored, first-match filter for the
    folder being synced (D-08, D-09). Unconditional — not gated on any package job.
    """

    def _filters(self, folder_path: str, home: str) -> list[str]:
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path(home)):
            return FolderSyncJob._decision_file_exclude_filters(folder_path)  # pyright: ignore[reportPrivateUsage]

    def test_home_under_synced_folder_anchors_the_glob_under_user_subdir(self) -> None:
        """H130, H133, K73 — a machine's own "always skip" list never reaches the other machine, so
        a machine synced for the first time has none.
        """
        assert self._filters("/home", "/home/alice") == [
            f"--filter={shlex.quote('- /alice/.config/pc-switcher/*.decisions.yaml')}"
        ]

    def test_glob_outside_synced_folder_is_skipped(self) -> None:
        """K74 — syncing /root while the invoking user's home is /home/alice excludes nothing."""
        assert self._filters("/root", "/home/alice") == []

    def test_root_invoker_excludes_under_root(self) -> None:
        """K74 — the exclusion follows the invoking user's home."""
        assert self._filters("/root", "/root") == [
            f"--filter={shlex.quote('- /.config/pc-switcher/*.decisions.yaml')}"
        ]

    def test_decision_file_exclude_precedes_merge_filter(self) -> None:
        """K75 — emitted GLOBAL-FIRST, before the folder's central merge filter (first-match-wins)."""
        ctx = make_context(config={"folders": [{"path": "/home"}]}, target_username="testuser")
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path="/home", filter_file="/abs/home.filter")
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path("/home/alice")):
            cmd = job._build_rsync_cmd(folder, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        assert cmd.index(".config/pc-switcher/*.decisions.yaml") < cmd.index("merge /abs/home.filter")

    def test_user_plus_rule_for_decision_file_does_not_change_command_ordering(self) -> None:
        """K76 — a `+` rule in the user's filter_file cannot re-expose the decision file: the
        GLOBAL-FIRST exclude is already emitted before that filter file is ever merged,
        so rsync's first-match-wins semantics keep it excluded regardless of the
        filter_file's own contents (which this unit test does not need to read)."""
        ctx = make_context(config={"folders": [{"path": "/home"}]}, target_username="testuser")
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path="/home", filter_file="/abs/home-with-plus-rule.filter")
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path("/home/alice")):
            cmd = job._build_rsync_cmd(folder, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        decision_exclude = f"--filter={shlex.quote('- /alice/.config/pc-switcher/*.decisions.yaml')}"
        assert decision_exclude in cmd
        assert cmd.index(decision_exclude) < cmd.index("merge /abs/home-with-plus-rule.filter")

    def test_unconditional_regardless_of_which_folder_is_synced(self) -> None:
        """K73 — not gated on any package job's enable flag: present for /root too when the
        invoking user's home is under it."""
        assert self._filters("/root", "/root") != []


class TestSnippetRegistryExcludeFilters:
    """The install-snippet registry (`~/.config/pc-switcher/package-snippets.yaml`) never
    travels in the mirror: it reaches the target only through `manual_installs_sync`'s own
    consented push (`PKG-FR-REGISTRY-CONSENT`). Unconditional, like the decision files.
    """

    def _filters(self, folder_path: str, home: str) -> list[str]:
        with patch("pcswitcher.jobs.folder_sync.Path.home", return_value=Path(home)):
            return FolderSyncJob._snippet_registry_exclude_filters(folder_path)  # pyright: ignore[reportPrivateUsage]

    def test_the_registry_is_excluded_under_the_invoking_users_home(self) -> None:
        """K88 — the file the `*.decisions.yaml` glob does not match gets its own rule."""
        assert self._filters("/home", "/home/alice") == [
            f"--filter={shlex.quote('- /alice/.config/pc-switcher/package-snippets.yaml')}"
        ]

    def test_registry_outside_synced_folder_is_skipped(self) -> None:
        """K88 — syncing /root while the invoking user's home is /home/alice excludes nothing."""
        assert self._filters("/root", "/home/alice") == []

    def test_excluded_even_with_every_package_job_off(self, tmp_path: Path) -> None:
        """K88 — a skipped or disabled `manual_installs_sync` is the case where nothing gates the
        transfer at all, so the mirror must not become the transport."""
        home = tmp_path / "alice"
        ctx = make_context(config={"folders": [{"path": str(tmp_path)}]}, enabled_sync_jobs={})
        job = FolderSyncJob(ctx)
        with (
            patch("pcswitcher.jobs.folder_sync.Path.home", return_value=home),
            patch("pcswitcher.jobs.snap_sync.Path.home", return_value=home),
            patch("pcswitcher.jobs.flatpak_sync.Path.home", return_value=home),
        ):
            cmd = job._build_rsync_cmd(FolderEntry(path=str(tmp_path)), dry_run=False)  # pyright: ignore[reportPrivateUsage]
        assert "/alice/.config/pc-switcher/package-snippets.yaml" in cmd

    def test_the_registry_exclusion_precedes_the_merge_filter(self, tmp_path: Path) -> None:
        """K88 — GLOBAL-FIRST, so no user `+` rule can re-expose it."""
        home = tmp_path / "alice"
        ctx = make_context(config={"folders": [{"path": str(tmp_path)}]})
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path=str(tmp_path), filter_file="/abs/home.filter")
        with (
            patch("pcswitcher.jobs.folder_sync.Path.home", return_value=home),
            patch("pcswitcher.jobs.snap_sync.Path.home", return_value=home),
            patch("pcswitcher.jobs.flatpak_sync.Path.home", return_value=home),
        ):
            cmd = job._build_rsync_cmd(folder, dry_run=False)  # pyright: ignore[reportPrivateUsage]
        assert cmd.index("package-snippets.yaml") < cmd.index("merge /abs/home.filter")


class TestSnapSyncExcludeFilters:
    """The `~/snap/<app>/<revision>` directories `snap_sync` owns are excluded via
    absolute paths that module owns; folder_sync only translates each into a
    root-anchored, first-match filter for the folder being synced (D-29). Which revision
    dirs those are depends on the revisions the TARGET is on, which `execute` reads and
    passes down — see `TestPackageJobExcludeFiltersGating`.
    """

    def test_old_revision_excluded_current_kept(self, tmp_path: Path) -> None:
        """E109, K77 — the retained OLD revision dir is excluded; the CURRENT-revision data dir (what
        `current` resolves to) is mirrored where the target is on that revision, so it is
        absent from the filter list (decision 3).
        """
        home = tmp_path / "alice"
        firefox_dir = home / "snap" / "firefox"
        current_rev = firefox_dir / "2938"
        old_rev = firefox_dir / "2911"
        common_dir = firefox_dir / "common"
        current_rev.mkdir(parents=True)
        old_rev.mkdir(parents=True)
        common_dir.mkdir(parents=True)
        (firefox_dir / "current").symlink_to(current_rev, target_is_directory=True)

        with patch("pcswitcher.jobs.snap_sync.Path.home", return_value=home):
            filters = FolderSyncJob._snap_sync_exclude_filters(str(tmp_path), {"firefox": "2938"})  # pyright: ignore[reportPrivateUsage]

        assert filters == [f"--filter={shlex.quote('- /alice/snap/firefox/2911')}"]

    def test_no_snap_directory_yields_no_filters(self, tmp_path: Path) -> None:
        """K79 — nothing is excluded on snap's behalf."""
        with patch("pcswitcher.jobs.snap_sync.Path.home", return_value=tmp_path / "alice"):
            filters = FolderSyncJob._snap_sync_exclude_filters(str(tmp_path), {})  # pyright: ignore[reportPrivateUsage]
        assert filters == []

    def test_revision_dir_outside_synced_folder_is_skipped(self, tmp_path: Path) -> None:
        """E111, K85 — syncing /root while the invoking user's home is elsewhere excludes nothing."""
        home = tmp_path / "alice"
        (home / "snap" / "firefox" / "2938").mkdir(parents=True)
        with patch("pcswitcher.jobs.snap_sync.Path.home", return_value=home):
            filters = FolderSyncJob._snap_sync_exclude_filters("/root", {"firefox": "2938"})  # pyright: ignore[reportPrivateUsage]
        assert filters == []


class TestFlatpakSyncExcludeFilters:
    """`~/.local/share/flatpak`, which `flatpak_sync` owns, is excluded via the
    absolute path that module owns; folder_sync only translates it into a
    root-anchored, first-match filter for the folder being synced (D-29). Gating on
    `flatpak_sync` being enabled happens at the `_build_rsync_cmd` call site, not here
    — see `TestPackageJobExcludeFiltersGating`.
    """

    def test_flatpak_data_dir_included_var_app_never_mentioned(self, tmp_path: Path) -> None:
        """K83 — the flatpak store is excluded; `~/.var/app` is not."""
        home = tmp_path / "alice"
        with patch("pcswitcher.jobs.flatpak_sync.Path.home", return_value=home):
            filters = FolderSyncJob._flatpak_sync_exclude_filters(str(tmp_path))  # pyright: ignore[reportPrivateUsage]
        assert filters == [f"--filter={shlex.quote('- /alice/.local/share/flatpak')}"]
        assert not any(".var/app" in f for f in filters)

    def test_flatpak_data_dir_outside_synced_folder_is_skipped(self, tmp_path: Path) -> None:
        """K85 — syncing /root while the invoking user's home is elsewhere excludes nothing."""
        home = tmp_path / "alice"
        with patch("pcswitcher.jobs.flatpak_sync.Path.home", return_value=home):
            filters = FolderSyncJob._flatpak_sync_exclude_filters("/root")  # pyright: ignore[reportPrivateUsage]
        assert filters == []


class TestPackageJobExcludeFiltersGating:
    """What each package exclusion is conditioned on in the built rsync command (D-29).
    `flatpak_sync`'s store is excluded only when that job is enabled, read from
    `JobContext.enabled_sync_jobs` and never from `self.context.config` (that field is
    folder_sync's OWN config section). The snap revision dirs are conditioned on the
    TARGET's own revisions instead, which `execute` reads and passes in.
    """

    def _build_cmd(
        self,
        tmp_path: Path,
        enabled_sync_jobs: dict[str, bool] | None,
        target_snap_revisions: dict[str, str] | None = None,
    ) -> str:
        # The transfer root is tmp_path itself (not a literal "/home") so it is a real
        # ancestor of `home`, matching how _snap_sync_exclude_filters/
        # _flatpak_sync_exclude_filters resolve paths relative to the transfer root.
        home = tmp_path / "alice"
        firefox_dir = home / "snap" / "firefox"
        current_rev = firefox_dir / "2938"
        old_rev = firefox_dir / "2911"
        current_rev.mkdir(parents=True)
        old_rev.mkdir(parents=True)
        (firefox_dir / "current").symlink_to(current_rev, target_is_directory=True)
        ctx = make_context(
            config={"folders": [{"path": str(tmp_path)}]},
            target_username="testuser",
            enabled_sync_jobs=enabled_sync_jobs,
        )
        job = FolderSyncJob(ctx)
        folder = FolderEntry(path=str(tmp_path), filter_file="/abs/home.filter")
        with (
            patch("pcswitcher.jobs.folder_sync.Path.home", return_value=home),
            patch("pcswitcher.jobs.snap_sync.Path.home", return_value=home),
            patch("pcswitcher.jobs.flatpak_sync.Path.home", return_value=home),
        ):
            return job._build_rsync_cmd(  # pyright: ignore[reportPrivateUsage]
                folder, dry_run=False, target_snap_revisions=target_snap_revisions
            )

    def test_the_revision_the_target_holds_is_mirrored(self, tmp_path: Path) -> None:
        """E109, K77, N21 — the target is on 2938, so its data dir travels while the
        retained 2911 stays excluded."""
        cmd = self._build_cmd(tmp_path, {"snap_sync": True}, {"firefox": "2938"})
        assert "/alice/snap/firefox/2911" in cmd
        assert "/alice/snap/firefox/2938" not in cmd

    def test_a_revision_the_target_did_not_converge_to_is_excluded(self, tmp_path: Path) -> None:
        """K82 — the revision change was declined or failed, so the target is still on 2911 and
        no data dir of that app is mirrored."""
        cmd = self._build_cmd(tmp_path, {"snap_sync": True}, {"firefox": "2911"})
        assert "/alice/snap/firefox/2938" in cmd

    def test_a_snap_the_target_does_not_hold_has_its_data_dir_excluded(self, tmp_path: Path) -> None:
        """E115 — the install was declined or failed, so the target holds no revision of it."""
        cmd = self._build_cmd(tmp_path, {"snap_sync": True}, {"other": "1"})
        assert "/alice/snap/firefox/2938" in cmd

    def test_snap_exclusions_are_not_gated_on_snap_sync_being_enabled(self, tmp_path: Path) -> None:
        """K81, E110 — with snap_sync off nothing converged any revision, so no revision dir is
        mirrored; the exclusion follows the target's state, not the job's enable flag."""
        cmd = self._build_cmd(tmp_path, {"snap_sync": False}, target_snap_revisions=None)
        assert "/alice/snap/firefox/2938" in cmd
        assert "/alice/snap/firefox/2911" in cmd

    def test_flatpak_sync_enabled_includes_data_dir_exclusion_not_var_app(self, tmp_path: Path) -> None:
        """K23, K83 — with flatpak_sync on, the store is left to it and `~/.var/app` still travels."""
        cmd = self._build_cmd(tmp_path, {"flatpak_sync": True})
        assert "/alice/.local/share/flatpak" in cmd
        assert ".var/app" not in cmd

    def test_flatpak_sync_disabled_excludes_nothing(self, tmp_path: Path) -> None:
        """K23, K84 — with flatpak_sync off, the store is mirrored like any other data; folder_sync is
        free to read a package job's switch, which is what K23 rules on."""
        cmd = self._build_cmd(tmp_path, {"flatpak_sync": False})
        assert ".local/share/flatpak" not in cmd

    def test_both_package_exclusions_precede_merge_filter(self, tmp_path: Path) -> None:
        """E112, K86 — no user rule can re-expose either, both being emitted before the merge."""
        cmd = self._build_cmd(tmp_path, {"snap_sync": True, "flatpak_sync": True}, {"firefox": "2938"})
        assert cmd.index("/alice/snap/firefox/2911") < cmd.index("merge /abs/home.filter")
        assert cmd.index("/alice/.local/share/flatpak") < cmd.index("merge /abs/home.filter")

    def test_missing_enabled_sync_jobs_omits_the_flatpak_exclusion_without_raising(self, tmp_path: Path) -> None:
        """K87 — a JobContext built without enabled_sync_jobs (the existing lightweight test
        constructions' default) emits no flatpak exclusion and does not raise; the snap dirs
        never depended on that map."""
        cmd = self._build_cmd(tmp_path, enabled_sync_jobs=None, target_snap_revisions={"firefox": "2938"})
        assert ".local/share/flatpak" not in cmd
        assert "/alice/snap/firefox/2938" not in cmd


# ---------------------------------------------------------------------------
# SSH transport credential tests
# ---------------------------------------------------------------------------


class TestBuildRsyncCmdSSHTransport:
    """Verify explicit SSH credentials in the --rsh transport of _build_rsync_cmd.

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

        result = await job._stream_rsync(gen_chunks(), folder, PASS_MIRROR)
        return result, log_calls, progress_calls

    async def test_pass_opens_with_file_list_heartbeat(self) -> None:
        """A pass reports a heartbeat before any rsync output.

        With --no-inc-recursive rsync prints nothing while it builds the file list
        (measured on a 1.59M-entry /home: first output after 5.8s), so the bar must be
        put into its pulsing state up front instead of sitting at 0%.
        """
        _, _, progress_calls = await self._run_stream([])

        assert len(progress_calls) == 1
        update = progress_calls[0]
        assert isinstance(update, ProgressUpdate)
        assert update.heartbeat is True
        assert update.percent is None
        assert update.item == "/home — building file list"  # the mirror pass is unqualified
        assert update.track == "/home"

    async def test_every_update_tracks_the_folder_not_the_pass(self) -> None:
        """Updates track the folder path, so both passes share that folder's one bar."""
        data = (
            b"20.000   0%  0,00kB/s 0:00:00 (xfr#1, ir-chk=1039/1101)\r"
            b"9.53G   21%  317.26MB/s 0:00:28 (xfr#83, to-chk=444/538)\r"
        )
        _, _, progress_calls = await self._run_stream([data], folder_path="/root")

        tracks = {u.track for u in progress_calls if isinstance(u, ProgressUpdate)}
        assert tracks == {"/root"}

    async def test_to_chk_line_drives_bar_from_file_counts_not_rsync_percent(self) -> None:
        """The bar follows checked/total files; rsync's own percent is ignored.

        rsync's figure is bytes-sent over the size of the whole tree, so an incremental
        sync reads ~0% throughout (measured: 200 of 154,022 files re-sent → 0% on every
        line).  Here the line claims 21% while 94 of 538 files are checked — the bar
        must show 17%, not 21% (#198).
        """
        progress_line = b"9.53G 21% 317.26MB/s 0:00:28 (xfr#83, to-chk=444/538)\r"
        _, _, progress_calls = await self._run_stream([progress_line])

        update = progress_calls[-1]
        assert isinstance(update, ProgressUpdate)
        assert update.percent == 17  # 94/538 files, not rsync's byte-based 21%
        assert update.item == "/home — 94/538 files, 9.5 GiB"

    async def test_incremental_run_bar_advances_though_rsync_reports_zero_percent(self) -> None:
        """Real incremental-run lines (rsync stuck at 0%) still drive the bar 0→100%."""
        data = (
            b"7.631            0%    0,00kB/s 0:00:00 (xfr#1, to-chk=153943/154022)\r"
            b"13.621.297       0%   23,65MB/s 0:00:00 (xfr#102, to-chk=39397/154022)\r"
            b"14.432.278       0%   18,56MB/s 0:00:00 (xfr#202, to-chk=0/154022)\r"
        )
        _, _, progress_calls = await self._run_stream([data])

        updates = [u for u in progress_calls if isinstance(u, ProgressUpdate)]
        assert [u.percent for u in updates if u.percent is not None] == [0, 74, 100]

    async def test_ir_chk_fallback_reports_scanned_count_without_percent(self) -> None:
        """An ir-chk line reports the scanned count only — no percent, no denominator.

        --no-inc-recursive should stop rsync emitting these at all; if one appears, its
        percentage is bytes-done over bytes-known-*so far* (denominator grows, so the
        figure walks backwards) and its total is "entries discovered so far", not a
        total.  Neither may reach the bar (#198).
        """
        progress_line = b"20.000   0%    0,00kB/s    0:00:00 (xfr#1, ir-chk=1039/1101)\r"
        _, _, progress_calls = await self._run_stream([progress_line])

        update = progress_calls[-1]
        assert isinstance(update, ProgressUpdate)
        assert update.percent is None
        assert update.total is None
        assert update.current == 62  # 1101 listed - 1039 still to check
        assert update.item is not None and "scanning 62 files" in update.item

    async def test_ir_chk_scanned_count_never_decreases(self) -> None:
        """The scanned count is clamped monotonic as rsync's discovered total grows."""
        data = (
            b"171.667.736  18%  159,88GB/s 0:00:00 (xfr#1, ir-chk=54/1054)\r"
            b"20.000        5%    0,00kB/s 0:00:00 (xfr#2, ir-chk=1600/1683)\r"
            b"20.000        2%    0,00kB/s 0:00:00 (xfr#3, ir-chk=3900/4000)\r"
        )
        _, _, progress_calls = await self._run_stream([data])

        updates = [u for u in progress_calls if isinstance(u, ProgressUpdate) and u.current is not None]
        assert [u.current for u in updates] == [1000, 1000, 1000]
        assert all(u.percent is None for u in updates)

    async def test_percent_restarts_per_pass(self) -> None:
        """Each pass drives the bar 0-100% on its own — no job-wide slicing."""
        ctx = make_context()
        job = FolderSyncJob(ctx)
        progress_calls: list[ProgressUpdate] = []
        job._report_progress = progress_calls.append  # type: ignore[method-assign]

        async def gen_chunks():  # type: ignore[no-untyped-def]
            yield b"9.53G 40% 300.00MB/s 0:00:10 (xfr#10, to-chk=60/100)\r"
            yield b"9.53G 80% 300.00MB/s 0:00:05 (xfr#80, to-chk=20/100)\r"

        await job._stream_rsync(gen_chunks(), FolderEntry(path="/home"), PASS_COPY)
        await job._stream_rsync(gen_chunks(), FolderEntry(path="/home"), PASS_DELETE)

        percents = [u.percent for u in progress_calls if u.percent is not None]
        assert percents == [40, 80, 40, 80], "second pass must restart the bar, not continue the first"
        # Each pass opens with its own heartbeat, so the first three updates are the copy pass.
        # Both halves of a split run are qualified, so neither reads as the whole sync.
        assert all(u.item is not None and u.item.startswith(f"/home ({PASS_COPY})") for u in progress_calls[:3])
        assert all(u.item is not None and u.item.startswith(f"/home ({PASS_DELETE})") for u in progress_calls[3:])

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

    async def test_comma_grouped_progress_line_reports_full_bytes(self) -> None:
        """A comma-grouped size token is captured in full, not truncated at the last comma (WR-01).

        rsync thousands-groups its progress2 byte counter with a locale-dependent
        separator (',' on C/en_US, confirmed against a real rsync binary). Regression
        for a bug where `_PROGRESS2_RE`'s size group excluded ',', so
        `re.search` matched only the last 1-3 digits after the final comma
        (e.g. "29,958,458" -> "458"), producing a bytes_transferred wrong by
        orders of magnitude while files_transferred (comma-free) stayed correct.
        """
        progress_line = b"     29,958,458  99%   27.90GB/s    0:00:00 (xfr#298201, to-chk=1200/300501)\r"
        (files_transferred, bytes_transferred, _), _, _ = await self._run_stream([progress_line])

        assert files_transferred == 298201
        assert bytes_transferred == 29_958_458

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
        must strip the grouping rather than truncating or failing to parse.
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
class TestNeedsCopyPass:
    """_needs_copy_pass compares source vs target `.pcswitcher-filter` content hashes.

    Single pass is safe iff every source filter file is present & identical on the target
    (target-only extras are fine); otherwise a copy pass is needed to align them first.
    """

    def _job(self, source_out: str, target_out: str) -> tuple[FolderSyncJob, AsyncMock]:
        ctx = make_context(config={"folders": [{"path": "/home"}]})
        ctx.source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout=source_out, stderr=""))
        target_rc = AsyncMock(return_value=CommandResult(exit_code=0, stdout=target_out, stderr=""))
        ctx.target.run_command = target_rc
        return FolderSyncJob(ctx), target_rc

    async def test_no_source_filters_skips_target_round_trip(self) -> None:
        """No source filter files -> single pass, and the target is never queried."""
        job, target_rc = self._job(source_out="", target_out="ignored")
        assert await job._needs_copy_pass(FolderEntry(path="/home")) is False
        target_rc.assert_not_called()

    async def test_identical_manifests_need_no_copy_pass(self) -> None:
        line = "abc123  /home/alice/proj/.pcswitcher-filter"
        job, _ = self._job(source_out=line + "\n", target_out=line + "\n")
        assert await job._needs_copy_pass(FolderEntry(path="/home")) is False

    async def test_source_filter_absent_on_target_needs_copy_pass(self) -> None:
        job, _ = self._job(source_out="abc  /home/a/.pcswitcher-filter\n", target_out="")
        assert await job._needs_copy_pass(FolderEntry(path="/home")) is True

    async def test_source_filter_content_differs_needs_copy_pass(self) -> None:
        """Same path, different hash (edited filter) -> different line -> not a subset -> copy pass."""
        job, _ = self._job(
            source_out="NEWHASH  /home/a/.pcswitcher-filter\n",
            target_out="OLDHASH  /home/a/.pcswitcher-filter\n",
        )
        assert await job._needs_copy_pass(FolderEntry(path="/home")) is True

    async def test_target_only_extra_filters_need_no_copy_pass(self) -> None:
        """A filter present only on the target never forces a copy pass (it only adds protection)."""
        line = "abc  /home/a/.pcswitcher-filter"
        job, _ = self._job(source_out=line + "\n", target_out=line + "\nxyz  /home/b/.pcswitcher-filter\n")
        assert await job._needs_copy_pass(FolderEntry(path="/home")) is False


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

    async def test_dry_run_skips_the_copy_pass_and_the_manifest_check(self) -> None:
        """Dry-run must not write to the target: neither the manifest check nor the copy pass runs."""
        ctx = make_context(dry_run=True)
        # Even if the manifests would differ, dry-run short-circuits before the check.
        ctx.source.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="h  /home/alice/.pcswitcher-filter\n", stderr="")
        )
        ctx.source.start_process = AsyncMock(return_value=make_fake_process())

        job = FolderSyncJob(ctx)
        await job.execute()

        ctx.source.run_command.assert_not_called()  # manifest check skipped in dry-run
        assert ctx.source.start_process.call_count == 1  # only the read-only preview pass
        assert "--dry-run" in ctx.source.start_process.call_args[0][0]


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

    async def test_copy_pass_runs_before_the_mirror_when_filters_differ(self) -> None:
        """When a source per-dir filter isn't yet on the target, a no-delete copy pass precedes the mirror.

        Both passes carry the same dir-merge filter, so per-directory (and central) rules are
        respected in each — the copy pass just omits --delete so it can put the filter files
        onto the target before the deleting mirror runs.
        """
        ctx = make_context()
        # Source has a per-dir filter; the target's manifest (default empty) lacks it -> seed.
        ctx.source.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="hash  /home/alice/.pcswitcher-filter\n", stderr="")
        )
        ctx.source.start_process = AsyncMock(return_value=make_fake_process())

        job = FolderSyncJob(ctx)
        await job.execute()

        assert ctx.source.start_process.call_count == 2, "expected a copy pass then the mirror"
        copy_cmd = ctx.source.start_process.call_args_list[0].args[0]
        mirror_cmd = ctx.source.start_process.call_args_list[1].args[0]
        assert "--delete" not in copy_cmd, "the copy pass must not delete"
        assert "--delete" in mirror_cmd, "the mirror pass deletes"
        assert "dir-merge /.pcswitcher-filter" in copy_cmd
        assert "dir-merge /.pcswitcher-filter" in mirror_cmd

    async def test_copy_pass_skipped_when_no_source_filters(self) -> None:
        """With no .pcswitcher-filter on the source, only the single deleting mirror runs (common case)."""
        ctx = make_context()
        ctx.source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        target_rc = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        ctx.target.run_command = target_rc
        ctx.source.start_process = AsyncMock(return_value=make_fake_process())

        job = FolderSyncJob(ctx)
        await job.execute()

        assert ctx.source.start_process.call_count == 1
        assert "--delete" in ctx.source.start_process.call_args[0][0]
        # Short-circuit: no target manifest round-trip. The one target command a run always
        # issues is the snap revision listing the data boundary rests on.
        assert [call.args[0] for call in target_rc.call_args_list] == ["snap list --all"]

    async def _mirror_cmd_with_target_snaps(self, tmp_path: Path, target_snap_list: str) -> tuple[str, list[str]]:
        """Run `execute` over one folder holding `~/snap/firefox` at revision 2938, with the
        target answering `target_snap_list`. Returns (the mirror command, the target's commands).
        """
        home = tmp_path / "alice"
        firefox = home / "snap" / "firefox"
        (firefox / "2938").mkdir(parents=True)
        (firefox / "current").symlink_to(firefox / "2938", target_is_directory=True)
        ctx = make_context(config={"folders": [{"path": str(tmp_path)}]})
        ctx.source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))
        target_rc = AsyncMock(return_value=CommandResult(exit_code=0, stdout=target_snap_list, stderr=""))
        ctx.target.run_command = target_rc
        ctx.source.start_process = AsyncMock(return_value=make_fake_process())

        job = FolderSyncJob(ctx)
        with (
            patch("pcswitcher.jobs.folder_sync.Path.home", return_value=home),
            patch("pcswitcher.jobs.snap_sync.Path.home", return_value=home),
            patch("pcswitcher.jobs.flatpak_sync.Path.home", return_value=home),
        ):
            await job.execute()

        return ctx.source.start_process.call_args[0][0], [call.args[0] for call in target_rc.call_args_list]

    async def test_the_targets_own_revision_lets_the_data_dir_through(self, tmp_path: Path) -> None:
        """E109, N21 — the target reports revision 2938, so that data dir is mirrored, and the
        listing is read once for the whole job."""
        listing = _snap_listing("2938")
        cmd, target_cmds = await self._mirror_cmd_with_target_snaps(tmp_path, listing)

        assert "/alice/snap/firefox/2938" not in cmd
        assert target_cmds == ["snap list --all"]

    async def test_a_revision_the_target_is_not_on_is_excluded_by_execute(self, tmp_path: Path) -> None:
        """K82, E115 — the target is on another revision (a declined or failed convergence), so
        `execute` builds the transfer with that data dir excluded."""
        listing = _snap_listing("2911")
        cmd, _ = await self._mirror_cmd_with_target_snaps(tmp_path, listing)

        assert "/alice/snap/firefox/2938" in cmd

    async def test_copy_pass_failure_aborts_before_the_mirror(self) -> None:
        """A non-zero exit in the copy pass raises RuntimeError; the deleting mirror never runs."""
        ctx = make_context()
        ctx.source.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="hash  /home/alice/.pcswitcher-filter\n", stderr="")
        )
        # The first (copy) pass fails.
        ctx.source.start_process = AsyncMock(return_value=make_fake_process(exit_code=23, stderr="boom"))

        job = FolderSyncJob(ctx)
        with pytest.raises(RuntimeError):
            await job.execute()
        assert ctx.source.start_process.call_count == 1, "mirror must not run after a failed copy pass"


@pytest.mark.asyncio
class TestEveryPassIsAnnounced:
    """#209 — the mirror is a modification like any other, so a run under
    `--confirm-each-command` is asked about each pass, on the machine the pass changes.
    """

    async def _passes(self, ctx: JobContext) -> list[Mapping[str, Any]]:
        """Run `execute` and return the keyword arguments each rsync pass was spawned with."""
        ctx.source.start_process = AsyncMock(return_value=make_fake_process())  # pyright: ignore[reportAttributeAccessIssue]
        await FolderSyncJob(ctx).execute()
        return [call.kwargs for call in ctx.source.start_process.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]

    async def test_the_lone_mirror_pass_says_it_deletes_and_names_the_target(self) -> None:
        """J185 — the steady-state pass transfers and deletes, and the deletions land on the target."""
        ctx = make_context()
        ctx.source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

        (mirror,) = await self._passes(ctx)

        assert mirror["mutates"] == "mirror /home, deleting files the source does not have"
        assert mirror["changes"] is Host.TARGET

    async def test_a_split_folder_asks_twice_and_only_the_second_can_delete(self) -> None:
        """J186 — each half of a split run is its own question, and the copy pass says it deletes
        nothing: approving the transfer is not approving the deletions."""
        ctx = make_context()
        ctx.source.run_command = AsyncMock(
            return_value=CommandResult(exit_code=0, stdout="hash  /home/alice/.pcswitcher-filter\n", stderr="")
        )

        copy, delete = await self._passes(ctx)

        assert copy["mutates"] == "copy /home across, deleting nothing"
        assert delete["mutates"] == "mirror /home, deleting files the source does not have"

    async def test_the_dry_run_preview_is_not_gated_at_all(self) -> None:
        """J187 — a preview writes nothing, so it asks nothing: a run that stops to request
        permission reads as one that might change something, which is what --dry-run rules out."""
        ctx = make_context(dry_run=True)

        (preview,) = await self._passes(ctx)

        assert preview["mutates"] is None
