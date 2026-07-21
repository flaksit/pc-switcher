"""Unit tests for VscodeStateSyncJob (#195).

Two layers:
- Merge semantics: build real ItemTable DBs with stdlib sqlite3 and run the pure SQL
  builders (source_strip_sql / target_inject_sql) in-process, asserting row contents.
  This directly exercises the RESEARCH §6 verified sequence without any executor.
- execute() orchestration: mock the source/target executors and assert the editor loop,
  first-sync skip, absent-DB skip, and dry-run no-write behavior.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.vscode_state_sync import (
    EDITOR_STATE_DB_RELPATHS,
    VscodeStateSyncJob,
    editor_state_exclude_paths,
    source_strip_sql,
    target_inject_sql,
)
from pcswitcher.models import CommandResult

# ---------------------------------------------------------------------------
# DB helpers (real stdlib sqlite3 against the locked ItemTable schema)
# ---------------------------------------------------------------------------


def _make_db(path: Path, rows: Sequence[tuple[str, Any]]) -> None:
    """Create a state.vscdb with the locked ItemTable schema and the given rows."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
        conn.executemany("INSERT INTO ItemTable (key, value) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _run_script(path: Path, sql: str) -> None:
    """Execute a SQL script (may contain ATTACH) against the DB at ``path``."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(sql)
        conn.commit()
    finally:
        conn.close()


def _read(path: Path) -> dict[str, Any]:
    """Return the DB's ItemTable as a ``{key: value}`` dict."""
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT key, value FROM ItemTable").fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestExcludePaths:
    """`editor_state_exclude_paths()` is the seam folder_sync consumes: absolute paths
    for the invoking user, single source of truth with the merge set."""

    def test_absolute_paths_under_invoking_home_main_and_backup(self) -> None:
        """Eight absolute paths under the invoking user's home: main + `.backup` per editor."""
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=Path("/home/alice")):
            paths = editor_state_exclude_paths()
        assert len(paths) == 2 * len(EDITOR_STATE_DB_RELPATHS) == 8
        assert all(p.startswith("/home/alice/") for p in paths)

    def test_main_then_backup_order(self) -> None:
        """Each main DB is immediately followed by its `.backup` sidecar."""
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=Path("/home/alice")):
            paths = editor_state_exclude_paths()
        for rel in EDITOR_STATE_DB_RELPATHS:
            main = f"/home/alice/{rel}"
            assert paths[paths.index(main) + 1] == main + ".backup"

    def test_resolves_against_the_invoking_user(self) -> None:
        """Paths track whoever runs the tool — root invoker resolves under /root."""
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=Path("/root")):
            paths = editor_state_exclude_paths()
        assert all(p.startswith("/root/.config/") for p in paths)

    def test_covers_the_four_editors(self) -> None:
        """Code, Antigravity, Cursor, VSCodium are all covered."""
        joined = "\n".join(EDITOR_STATE_DB_RELPATHS)
        for editor in ("Code", "Antigravity", "Cursor", "VSCodium"):
            assert f".config/{editor}/User/globalStorage/state.vscdb" in joined


# ---------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------


class TestMergeSemantics:
    """The source-strip and target-inject SQL reproduce the RESEARCH §6 verified merge."""

    def test_source_strip_removes_only_matched_rows(self, tmp_path: Path) -> None:
        """DELETE removes only preserve-matched keys; everything else stays."""
        db = tmp_path / "neutral.vscdb"
        _make_db(db, [("secret://a", b"x"), ("keep.me", "v"), ("secret://b", b"y")])
        _run_script(db, source_strip_sql(["secret://%"]))
        assert set(_read(db)) == {"keep.me"}

    def test_end_to_end_merge_matches_research_table(self, tmp_path: Path) -> None:
        """Reproduce the verified end-to-end table (RESEARCH §6), binary BLOBs included.

        Preserved keys keep the TARGET's value (incl. a target-only secret); non-matched
        keys take the source's value; target-only non-matched keys are dropped.
        """
        source = tmp_path / "source.vscdb"
        target = tmp_path / "target.vscdb"
        neutral = tmp_path / "neutral.vscdb"
        _make_db(
            source,
            [
                ("secret://src-cred", b"\x00\xff\x01\x02"),
                ("settings.theme", "dark"),
                ("mru.files", b"\xde\xad\xbe\xef"),
            ],
        )
        _make_db(
            target,
            [
                ("secret://src-cred", b"\xaa\xbb\xcc\xdd"),
                ("secret://tgt-only", b"\x11\x22\x33\x44"),
                ("settings.theme", "light-STALE"),
                ("target.only.key", "should-be-dropped"),
            ],
        )
        # Step A (source-strip on a copy) then Step C (target-inject).
        shutil.copyfile(source, neutral)
        _run_script(neutral, source_strip_sql(["secret://%"]))
        _run_script(neutral, target_inject_sql(str(target), ["secret://%"]))

        assert _read(neutral) == {
            "mru.files": b"\xde\xad\xbe\xef",  # source value, binary intact
            "secret://src-cred": b"\xaa\xbb\xcc\xdd",  # TARGET value preserved
            "secret://tgt-only": b"\x11\x22\x33\x44",  # target-only secret preserved
            "settings.theme": "dark",  # source value; stale target overwritten
        }
        # target.only.key (non-preserved, target-only) was dropped.
        assert "target.only.key" not in _read(neutral)

    def test_source_db_is_never_mutated(self, tmp_path: Path) -> None:
        """The merge operates on a copy; the live source DB keeps its secret rows."""
        source = tmp_path / "source.vscdb"
        neutral = tmp_path / "neutral.vscdb"
        _make_db(source, [("secret://a", b"x"), ("keep", "v")])
        shutil.copyfile(source, neutral)
        _run_script(neutral, source_strip_sql(["secret://%"]))
        assert set(_read(source)) == {"secret://a", "keep"}

    def test_multi_pattern_preserves_both_families(self, tmp_path: Path) -> None:
        """Two globs preserve both key families' target values simultaneously."""
        globs = ["secret://%", "vscode.auth://%"]
        source = tmp_path / "source.vscdb"
        target = tmp_path / "target.vscdb"
        neutral = tmp_path / "neutral.vscdb"
        _make_db(source, [("secret://a", b"S"), ("vscode.auth://b", b"A"), ("keep", "src")])
        _make_db(target, [("secret://a", b"TS"), ("vscode.auth://b", b"TA"), ("keep", "tgt")])
        shutil.copyfile(source, neutral)
        _run_script(neutral, source_strip_sql(globs))
        _run_script(neutral, target_inject_sql(str(target), globs))
        result = _read(neutral)
        assert result["secret://a"] == b"TS"
        assert result["vscode.auth://b"] == b"TA"
        assert result["keep"] == "src"

    def test_single_quote_in_glob_is_escaped(self, tmp_path: Path) -> None:
        """A glob containing a single quote is doubled, so the SQL runs (no injection/crash)."""
        db = tmp_path / "neutral.vscdb"
        _make_db(db, [("secret://o'brien", b"x"), ("keep", "v")])
        _run_script(db, source_strip_sql(["secret://o'brien%"]))
        assert set(_read(db)) == {"keep"}

    def test_single_quote_in_attach_path_is_escaped(self, tmp_path: Path) -> None:
        """An ATTACH path containing a single quote is doubled, so the inject runs."""
        weird = tmp_path / "o'brien.vscdb"
        neutral = tmp_path / "neutral.vscdb"
        _make_db(weird, [("secret://x", b"zz")])
        _make_db(neutral, [("keep", "v")])
        _run_script(neutral, target_inject_sql(str(weird), ["secret://%"]))
        assert _read(neutral)["secret://x"] == b"zz"


# ---------------------------------------------------------------------------
# execute() orchestration
# ---------------------------------------------------------------------------

_CODE_DB = ".config/Code/User/globalStorage/state.vscdb"
_ANTIGRAVITY_DB = ".config/Antigravity/User/globalStorage/state.vscdb"


def _setup_home(tmp_path: Path, editors: Sequence[str] = (_CODE_DB,)) -> Path:
    """Create a home tree with an (empty) live DB file for each named editor."""
    home = tmp_path / "home" / "alice"
    for rel in editors:
        db = home / rel
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_bytes(b"")
    return home


def _make_context(
    dry_run: bool = False,
    target_username: str | None = "alice",
    target_db_present: bool = True,
) -> JobContext:
    """JobContext with mocked executors; target `test -f` reports ``target_db_present``."""
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(exit_code=0, stdout="", stderr=""))

    def _target_side_effect(cmd: str, **_: object) -> CommandResult:
        if "test -f" in cmd:
            return CommandResult(exit_code=0 if target_db_present else 1, stdout="", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    target = MagicMock()
    target.run_command = AsyncMock(side_effect=_target_side_effect)
    target.send_file = AsyncMock()
    return JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
        target_username=target_username,
    )


def _target_cmds(ctx: JobContext) -> list[str]:
    """The shell commands passed to the target executor's run_command."""
    return [call.args[0] for call in ctx.target.run_command.call_args_list]  # type: ignore[union-attr]


class TestExecuteOrchestration:
    """execute() drives the editor loop with first-sync, absent-DB, and dry-run handling."""

    async def test_merge_path_injects_then_moves(self, tmp_path: Path) -> None:
        """With a live target DB, the neutral DB is transferred, injected, then atomically moved."""
        home = _setup_home(tmp_path, editors=[_CODE_DB])
        ctx = _make_context(target_db_present=True)
        job = VscodeStateSyncJob(ctx)
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=home):
            await job.execute()

        ctx.target.send_file.assert_awaited_once()  # type: ignore[union-attr]
        # Neutral DB transferred to a temp path in the live DB's directory.
        _local, remote = ctx.target.send_file.call_args.args  # type: ignore[union-attr]
        target_db = str(home / _CODE_DB)
        assert remote == target_db + ".pcswitcher-tmp"

        cmds = _target_cmds(ctx)
        assert any("INSERT INTO ItemTable" in c and "ATTACH" in c for c in cmds)
        # The mv atomically replaces the live DB from the temp path.
        assert any("mv -f" in c and target_db in c and ".pcswitcher-tmp" in c for c in cmds)

    async def test_creates_target_dir_before_transfer(self, tmp_path: Path) -> None:
        """The target globalStorage dir is created (mkdir -p) before the SFTP put.

        Guards the folder_sync-disabled path: with jobs toggled independently the
        parent dir may not exist, so send_file would otherwise fail.
        """
        home = _setup_home(tmp_path, editors=[_CODE_DB])
        ctx = _make_context(target_db_present=False)
        job = VscodeStateSyncJob(ctx)
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=home):
            await job.execute()

        cmds = _target_cmds(ctx)
        assert any("mkdir -p" in c and "globalStorage" in c for c in cmds)

    async def test_first_sync_skips_inject_but_places_db(self, tmp_path: Path) -> None:
        """Absent target DB: skip the ATTACH inject, still transfer and mv the neutral DB."""
        home = _setup_home(tmp_path, editors=[_CODE_DB])
        ctx = _make_context(target_db_present=False)
        job = VscodeStateSyncJob(ctx)
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=home):
            await job.execute()

        ctx.target.send_file.assert_awaited_once()  # type: ignore[union-attr]
        cmds = _target_cmds(ctx)
        assert not any("ATTACH" in c or "INSERT" in c for c in cmds)
        assert any("mv -f" in c for c in cmds)

    async def test_absent_source_editor_is_skipped(self, tmp_path: Path) -> None:
        """Only editors whose DB exists on the source are processed."""
        home = _setup_home(tmp_path, editors=[_CODE_DB])  # Antigravity/Cursor/VSCodium absent
        ctx = _make_context(target_db_present=True)
        job = VscodeStateSyncJob(ctx)
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=home):
            await job.execute()

        # Exactly one editor synced -> exactly one file transfer.
        ctx.target.send_file.assert_awaited_once()  # type: ignore[union-attr]
        _local, remote = ctx.target.send_file.call_args.args  # type: ignore[union-attr]
        assert "Code" in remote and "Antigravity" not in remote

    async def test_two_present_editors_both_synced(self, tmp_path: Path) -> None:
        """Every present editor gets its own transfer."""
        home = _setup_home(tmp_path, editors=[_CODE_DB, _ANTIGRAVITY_DB])
        ctx = _make_context(target_db_present=True)
        job = VscodeStateSyncJob(ctx)
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=home):
            await job.execute()
        assert ctx.target.send_file.await_count == 2  # type: ignore[union-attr]

    async def test_dry_run_performs_no_writes(self, tmp_path: Path) -> None:
        """Dry-run: no send_file, no target inject, no mv, and the source DB is untouched."""
        home = _setup_home(tmp_path, editors=[_CODE_DB])
        source_db = home / _CODE_DB
        # Seed a real source DB with a secret row to prove it is not mutated.
        source_db.unlink()
        _make_db(source_db, [("secret://a", b"x"), ("keep", "v")])

        ctx = _make_context(dry_run=True, target_db_present=True)
        job = VscodeStateSyncJob(ctx)
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=home):
            await job.execute()

        ctx.target.send_file.assert_not_awaited()  # type: ignore[union-attr]
        cmds = _target_cmds(ctx)
        assert not any("ATTACH" in c or "INSERT" in c or "mv -f" in c for c in cmds)
        # Source live DB is unchanged (source-strip never ran against it).
        assert set(_read(source_db)) == {"secret://a", "keep"}
        # No sqlite3 command ran on the source in dry-run.
        source_cmds = [call.args[0] for call in ctx.source.run_command.call_args_list]  # type: ignore[union-attr]
        assert not any("sqlite3" in c for c in source_cmds)

    async def test_no_editors_present_is_a_noop(self, tmp_path: Path) -> None:
        """A home with no editor DBs performs no transfers."""
        home = tmp_path / "home" / "alice"
        home.mkdir(parents=True)
        ctx = _make_context(target_db_present=True)
        job = VscodeStateSyncJob(ctx)
        with patch("pcswitcher.jobs.vscode_state_sync.Path.home", return_value=home):
            await job.execute()
        ctx.target.send_file.assert_not_awaited()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


class TestValidate:
    """validate() requires sqlite3 on both hosts and has no btrfs dependency."""

    async def test_both_hosts_have_sqlite3(self) -> None:
        ctx = _make_context()
        errors = await VscodeStateSyncJob(ctx).validate()
        assert errors == []

    async def test_missing_sqlite3_on_source(self) -> None:
        ctx = _make_context()

        def _side(cmd: str, **_: object) -> CommandResult:
            return CommandResult(exit_code=1, stdout="", stderr="")

        ctx.source.run_command = AsyncMock(side_effect=_side)  # type: ignore[union-attr]
        errors = await VscodeStateSyncJob(ctx).validate()
        assert len(errors) == 1
        assert errors[0].host.value == "source"

    async def test_missing_sqlite3_on_target(self) -> None:
        ctx = _make_context()

        def _side(cmd: str, **_: object) -> CommandResult:
            if "command -v sqlite3" in cmd:
                return CommandResult(exit_code=1, stdout="", stderr="")
            return CommandResult(exit_code=0, stdout="", stderr="")

        ctx.target.run_command = AsyncMock(side_effect=_side)  # type: ignore[union-attr]
        errors = await VscodeStateSyncJob(ctx).validate()
        assert len(errors) == 1
        assert errors[0].host.value == "target"


# ---------------------------------------------------------------------------
# Config guard (CONFIG_SCHEMA)
# ---------------------------------------------------------------------------


class TestConfigSchema:
    """The job's own CONFIG_SCHEMA guards preserve_key_globs shape."""

    def test_valid_config(self) -> None:
        assert VscodeStateSyncJob.validate_config({"preserve_key_globs": ["secret://%"]}) == []

    def test_wrong_typed_preserve_globs_is_rejected(self) -> None:
        errors = VscodeStateSyncJob.validate_config({"preserve_key_globs": "secret://%"})
        assert len(errors) == 1
        assert errors[0].job == "vscode_state_sync"

    def test_unknown_key_rejected(self) -> None:
        errors = VscodeStateSyncJob.validate_config({"unknown": True})
        assert len(errors) == 1
