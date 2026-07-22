"""SQLite-aware selective sync of the VS Code family's ``state.vscdb`` (ADR-018, #195).

Covers VS Code and its forks (Code, Antigravity, Cursor, VSCodium — ``VSCODE_BASED_EDITORS``);
"editor" below always means one of those, never a generic editor. Each such editor's global
``state.vscdb`` mixes wanted global state (settings-adjacent, MRU) with machine-bound
SecretStorage blobs under ``secret://`` keys — ciphertext encrypted with a per-machine
OS-keyring key that is never synced. A file-granular rsync mirror would clobber the target's
own keyring-decryptable secrets and force auth-backed extensions to re-login after every sync.

SCOPE: this job covers ONLY the invoking user (whoever runs ``pc-switcher``) — the
one whose ``Path.home()`` this process resolves. Other users' VS Code state DBs under a
synced ``/home`` are deliberately NOT handled (YAGNI; multi-user selective merge would
need root on both ends, like ``folder_sync``, and is out of scope). This is a property
of THIS job, not a system-wide single-user assumption — user-data sync itself spans all
users.

This job (a normal-user ``SyncJob``, no sudo) rebuilds each target DB by mirroring
every key EXCEPT ``PRESERVE_KEY_GLOBS`` matches (``secret://%``), which keep the
target's own value:

1. Source-strip: copy the source DB to a source-local temp, ``DELETE`` preserved
   rows -> a "neutral" DB carrying no secrets.
2. Transfer the neutral DB to a target temp path in the live DB's directory.
3. Target-inject: ``ATTACH`` the target's live DB and copy its preserved rows into
   the neutral DB, then atomically ``mv`` it over the live DB. When the target DB is
   absent (first sync) the inject is skipped and the neutral DB becomes the target DB.

Each editor has TWO state DB files handled identically: the main ``state.vscdb`` and its
``state.vscdb.backup`` sidecar (both SQLite DBs with the same schema). INVARIANT: the set
of files ``folder_sync`` excludes is exactly the set this job merges — no file is hidden
from the mirror without being handled, and vice versa. Both sides iterate the single
``VSCODE_STATE_HANDLED_RELPATHS`` tuple.

``folder_sync`` excludes the editor DBs from its mirror non-overridably (global-first, so
no user rule can re-expose them) so the mirror never touches them and the target's secrets
are still in place at job time. This module OWNS which absolute paths those are and exposes
them via ``vscode_state_exclude_paths()`` (a function, not a constant — the paths are
dynamic, resolved against the invoking user's home at call time); ``folder_sync`` imports it
one-way (avoiding an import cycle) and only translates each absolute path into an rsync
filter for the folder being synced. folder_sync holds no knowledge of editors or home
layout, and does not hardcode these paths (unlike its own runtime-state exclude, ADR-017).
"""

from __future__ import annotations

import shlex
import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, override

from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import FirstSyncScope, Host, LogLevel, ProgressUpdate, ValidationError

# Home-relative paths of each covered editor's global state.vscdb. Code and Antigravity
# are confirmed on-disk; Cursor and VSCodium directory casing is [ASSUMED] from the
# standard ~/.config/<Editor>/User/globalStorage/ layout (RESEARCH §5 / Assumption A1).
# The job skips editors whose DB is absent on the source, so an unused entry is harmless.
VSCODE_BASED_EDITORS: tuple[str, ...] = ("Code", "Antigravity", "Cursor", "VSCodium")
VSCODE_STATE_DB_RELPATHS: tuple[str, ...] = tuple(
    f".config/{editor}/User/globalStorage/state.vscdb" for editor in VSCODE_BASED_EDITORS
)

# Keys whose TARGET value is kept instead of mirrored, as SQLite LIKE patterns. Hardcoded,
# not configurable: ``secret://`` is the VS Code SecretStorage namespace — a VS Code
# internal on the same footing as VSCODE_BASED_EDITORS and the DB layout above. The module
# owns every VS-Code-specific fact so the job is off-the-shelf with nothing to tune; a user
# has no basis to widen this without knowing VS Code's key scheme, and widening it wrongly
# would leak machine-bound secrets across the fleet.
PRESERVE_KEY_GLOBS: tuple[str, ...] = ("secret://%",)


# The full set of state-DB files handled per editor: the main ``state.vscdb`` and its
# ``.backup`` sidecar (both are SQLite DBs with the same ItemTable schema). This is the
# SINGLE source of truth enforcing the invariant that the folder_sync exclude set and the
# merge set are IDENTICAL — every excluded file is merged and every merged file is
# excluded, so nothing is ever hidden from the mirror without being handled. Both
# `vscode_state_exclude_paths()` (the exclude side) and `execute()` (the merge side)
# iterate exactly this tuple.
VSCODE_STATE_HANDLED_RELPATHS: tuple[str, ...] = tuple(
    relpath + suffix for relpath in VSCODE_STATE_DB_RELPATHS for suffix in ("", ".backup")
)


def vscode_state_exclude_paths() -> list[Path]:
    """Absolute paths of the INVOKING user's editor state DBs to exclude from the mirror.

    Every file this job merges (``VSCODE_STATE_HANDLED_RELPATHS`` — each editor's
    ``state.vscdb`` and its ``.backup``), resolved against ``Path.home()`` at call time —
    hence a function, not a constant: the paths depend on whoever runs ``pc-switcher``.

    This is the seam ``folder_sync`` consumes: this module owns WHICH absolute paths are
    the editor DBs, and ``folder_sync`` only translates each into a root-anchored rsync
    filter for the folder it is syncing. The set is identical to the merge set (both
    iterate ``VSCODE_STATE_HANDLED_RELPATHS``), so no file is excluded without being
    merged. Scope is the invoking user only (see module docstring); paths are returned
    whether or not the file exists (an absent path is a harmless no-op filter, and
    excluding it still protects a DB created later).
    """
    home = Path.home()
    return [home / relpath for relpath in VSCODE_STATE_HANDLED_RELPATHS]


def _sql_string_literal(value: str) -> str:
    """Return ``value`` as a SQLite single-quoted string literal.

    SQLite escapes a single quote inside a string literal by doubling it. Applied to
    every glob and to the ATTACH path so a value containing ``'`` cannot break out of
    the literal (shell-level quoting via ``shlex.quote`` is layered on top separately).
    """
    return "'" + value.replace("'", "''") + "'"


def _where_clause(globs: Sequence[str]) -> str:
    """Build ``key LIKE '<g1>' OR key LIKE '<g2>' ...`` for the preserve globs."""
    return " OR ".join(f"key LIKE {_sql_string_literal(glob)}" for glob in globs)


def source_strip_sql(globs: Sequence[str]) -> str:
    """SQL that deletes preserve-matched rows from the neutral DB (source-strip, Step A)."""
    return f"DELETE FROM ItemTable WHERE {_where_clause(globs)};"


def target_inject_sql(target_live: str, globs: Sequence[str]) -> str:
    """SQL that copies the target's preserved rows into the neutral DB (target-inject, Step C).

    No ``OR REPLACE`` is needed: after the source-strip the neutral DB holds no
    preserved-key rows, so there is no conflict to resolve.
    """
    return (
        f"ATTACH {_sql_string_literal(target_live)} AS live; "
        f"INSERT INTO ItemTable SELECT key, value FROM live.ItemTable WHERE {_where_clause(globs)};"
    )


def _run_sql(db_path: Path | str, sql: str) -> None:
    """Execute ``sql`` against ``db_path`` with the stdlib ``sqlite3`` module.

    ``isolation_level=None`` (autocommit) so ``ATTACH`` — which SQLite rejects inside a
    transaction — is never wrapped in the module's implicit DML transaction.
    """
    connection = sqlite3.connect(db_path, isolation_level=None)
    try:
        connection.executescript(sql)
    finally:
        connection.close()


def target_sql_command(db_path: str, sql: str) -> str:
    """Shell command running ``sql`` against ``db_path`` on the target via ``python3``.

    Uses the target's system Python rather than the ``sqlite3`` CLI: on Ubuntu the CLI is
    a separate, not-installed-by-default package, while ``python3`` is priority-important
    and its ``libpython3.x-stdlib`` dependency carries the ``sqlite3`` module. ``uv`` is
    deliberately not used as a fallback — it is not on ``PATH`` for the non-login shells
    this job runs, and ``uv run`` can reach for the network to provision an interpreter.

    The DB path and SQL travel as ``argv`` entries, so only the fixed driver script is
    ever interpolated into the code Python parses.
    """
    script = (
        "import sqlite3,sys\n"
        "con=sqlite3.connect(sys.argv[1],isolation_level=None)\n"
        "con.executescript(sys.argv[2])\n"
        "con.close()\n"
    )
    return f"python3 -c {shlex.quote(script)} {shlex.quote(db_path)} {shlex.quote(sql)}"


class VscodeStateSyncJob(SyncJob):
    """Selective, SQLite-aware sync of VS Code editor ``state.vscdb`` (ADR-018).

    Mirrors each editor's global state DB except machine-bound ``secret://`` rows
    (``PRESERVE_KEY_GLOBS``), which keep the target's own value. Runs as the invoking
    normal user (no sudo). The module owns every VS-Code-specific fact (editor list, DB
    layout, preserved-key namespace), so the job takes no configuration.
    """

    name: ClassVar[str] = "vscode_state_sync"

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name the editor state DBs this job destructively replaces on a first sync (ADR-015).

        Enumerates the covered editor DBs (home-relative). The job only touches the
        subset present on the source at run time, but the first-sync warning is composed
        before job discovery, so it lists the full covered set.
        """
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=list(VSCODE_STATE_DB_RELPATHS),
            mechanism="sqlite merge + atomic mv",
        )

    async def validate(self) -> list[ValidationError]:
        """Require a ``python3`` with the ``sqlite3`` module on the target.

        Only the target is checked: the source-side merge runs in this process, whose own
        ``sqlite3`` import would have failed at module load. One probe covers interpreter
        and module together, since a ``python3`` without ``sqlite3`` is as unusable as none.

        No btrfs dependency (CONTEXT decision 7): the job operates on the invoking
        user's own files and does not couple to the snapshot subsystem.
        """
        probe = await self.target.run_command('python3 -c "import sqlite3"', login_shell=False)
        if not probe.success:
            return [
                self._validation_error(
                    Host.TARGET,
                    "python3 with the sqlite3 module is not available on target "
                    "(required for state.vscdb selective merge)",
                )
            ]

        return []

    async def execute(self) -> None:
        """Selectively merge each present editor state DB (main + ``.backup``) onto the target.

        Iterates ``VSCODE_STATE_HANDLED_RELPATHS`` — every file folder_sync excludes, so
        the exclude set and the merge set are identical (no exclude without merge). Skips
        files absent on the source, and for each present one runs source-strip -> transfer
        -> target-inject -> atomic ``mv`` (Step C skipped when the target file is absent —
        first sync). In dry-run the job only detects source/target presence and logs the
        intended actions, performing no ``send_file``, no target inject, and no ``mv``
        (ADR-014).
        """
        # A DB's absolute path is identical on source and target: the invoking (real)
        # user has the same uid and home path on every machine, and pc-switcher does no
        # user/path mapping (ADR-019). So no target-home remapping here.
        home = Path.home()
        prefix = "[dry-run] " if self.context.dry_run else ""
        globs = PRESERVE_KEY_GLOBS

        present = [rel for rel in VSCODE_STATE_HANDLED_RELPATHS if (home / rel).exists()]
        if not present:
            self._log(Host.SOURCE, LogLevel.INFO, f"{prefix}No VS Code state DBs found on source; nothing to sync")
            self._report_progress(ProgressUpdate(percent=100))
            return

        total = len(present)
        for index, relpath in enumerate(present):
            source_db = home / relpath
            target_db = (home / relpath).as_posix()
            # Label the DB precisely, e.g. "Code state.vscdb" / "Code state.vscdb.backup".
            label = f"{Path(relpath).parts[1]} {Path(relpath).name}"

            target_exists = (
                await self.target.run_command(f"test -f {shlex.quote(target_db)}", login_shell=False)
            ).success
            mode = "merge" if target_exists else "first-sync"

            if self.context.dry_run:
                self._log(
                    Host.TARGET,
                    LogLevel.INFO,
                    f"{prefix}Would sync {label} ({mode}); preserving keys matching {globs}",
                )
                self._report_progress(ProgressUpdate(percent=int((index + 1) / total * 100)))
                continue

            await self._sync_editor(source_db, target_db, target_exists, globs, label)
            self._report_progress(ProgressUpdate(percent=int((index + 1) / total * 100)))

        self._report_progress(ProgressUpdate(percent=100))

    async def _sync_editor(
        self,
        source_db: Path,
        target_db: str,
        target_exists: bool,
        globs: Sequence[str],
        label: str,
    ) -> None:
        """Run the source-strip -> transfer -> inject -> atomic-mv sequence for one DB file.

        Handles either a main ``state.vscdb`` or its ``.backup`` sidecar (same schema);
        ``label`` names which. The neutral DB is transferred to
        ``<target_db>.pcswitcher-tmp`` — same directory as the live file — so the final
        ``mv -f`` is atomic and preserves ownership/perms.
        """
        remote_tmp = target_db + ".pcswitcher-tmp"
        tmp_dir = Path(tempfile.mkdtemp(prefix="pcswitcher-vscode-"))
        local_tmp = tmp_dir / "state.vscdb"
        try:
            # Step A — source-strip: copy the live source DB, then delete preserved rows.
            # In-process: the copy is source-local, so no subprocess or shell is involved.
            shutil.copyfile(source_db, local_tmp)
            try:
                _run_sql(local_tmp, source_strip_sql(globs))
            except sqlite3.Error as error:
                self._raise(Host.SOURCE, label, "source-strip", str(error))

            # Step B — transfer the neutral DB into the target live DB's directory.
            # Ensure that directory exists first: folder_sync normally creates it, but
            # jobs are independently toggleable, so `folder_sync: false` + this job on a
            # target that never ran the editor would otherwise leave the SFTP put with no
            # parent directory. mkdir -p is a no-op when it already exists.
            mkdir = await self.target.run_command(
                f"mkdir -p {shlex.quote(Path(target_db).parent.as_posix())}", login_shell=False
            )
            if not mkdir.success:
                self._raise(Host.TARGET, label, "mkdir target dir", mkdir.stderr)
            await self.target.send_file(local_tmp, remote_tmp)

            # Step C — target-inject the target's own preserved rows, only when it has a live DB.
            if target_exists:
                inject = await self.target.run_command(
                    target_sql_command(remote_tmp, target_inject_sql(target_db, globs)),
                    login_shell=False,
                )
                if not inject.success:
                    self._raise(Host.TARGET, label, "target-inject", inject.stderr)

            # Atomic replace within the same directory.
            move = await self.target.run_command(
                f"mv -f {shlex.quote(remote_tmp)} {shlex.quote(target_db)}", login_shell=False
            )
            if not move.success:
                self._raise(Host.TARGET, label, "atomic mv", move.stderr)

            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"Synced {label} ({'merge' if target_exists else 'first-sync'})",
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _raise(self, host: Host, label: str, step: str, stderr: str) -> None:
        """Log CRITICAL and raise; any exception halts the sync (orchestrator contract)."""
        message = f"{label} {step} failed: {stderr.strip()}"
        self._log(host, LogLevel.CRITICAL, message)
        raise RuntimeError(message)
