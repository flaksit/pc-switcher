"""SQLite-aware selective sync of VS Code editor ``state.vscdb`` (ADR-018, #195).

Each editor's global ``state.vscdb`` mixes wanted global state (settings-adjacent,
MRU) with machine-bound SecretStorage blobs under ``secret://`` keys — ciphertext
encrypted with a per-machine OS-keyring key that is never synced. A file-granular
rsync mirror would clobber the target's own keyring-decryptable secrets and force
auth-backed extensions to re-login after every sync.

SCOPE: this job covers ONLY the invoking user (whoever runs ``pc-switcher``) — the
one whose ``Path.home()`` this process resolves. Other users' editor DBs under a
synced ``/home`` are deliberately NOT handled (YAGNI; multi-user selective merge would
need root on both ends, like ``folder_sync``, and is out of scope). This is a property
of THIS job, not a system-wide single-user assumption — user-data sync itself spans all
users.

This job (a normal-user ``SyncJob``, no sudo) rebuilds each target DB by mirroring
every key EXCEPT ``preserve_key_globs`` matches (default ``secret://%``), which keep
the target's own value:

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
``EDITOR_STATE_HANDLED_RELPATHS`` tuple.

``folder_sync`` excludes the editor DBs from its mirror non-overridably (global-first, so
no user rule can re-expose them) so the mirror never touches them and the target's secrets
are still in place at job time. This module OWNS which absolute paths those are and exposes
them via ``editor_state_exclude_paths()`` (a function, not a constant — the paths are
dynamic, resolved against the invoking user's home at call time); ``folder_sync`` imports it
one-way (avoiding an import cycle) and only translates each absolute path into an rsync
filter for the folder being synced. folder_sync holds no knowledge of editors or home
layout, and does not hardcode these paths (unlike its own runtime-state exclude, ADR-017).
"""

from __future__ import annotations

import getpass
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, override

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.models import FirstSyncScope, Host, LogLevel, ProgressUpdate, ValidationError

# Home-relative paths of each covered editor's global state.vscdb. Code and Antigravity
# are confirmed on-disk; Cursor and VSCodium directory casing is [ASSUMED] from the
# standard ~/.config/<Editor>/User/globalStorage/ layout (RESEARCH §5 / Assumption A1).
# The job skips editors whose DB is absent on the source, so an unused entry is harmless.
VSCODE_BASED_EDITORS: tuple[str, ...] = ("Code", "Antigravity", "Cursor", "VSCodium")
EDITOR_STATE_DB_RELPATHS: tuple[str, ...] = tuple(
    f".config/{editor}/User/globalStorage/state.vscdb" for editor in VSCODE_BASED_EDITORS
)


# The full set of state-DB files handled per editor: the main ``state.vscdb`` and its
# ``.backup`` sidecar (both are SQLite DBs with the same ItemTable schema). This is the
# SINGLE source of truth enforcing the invariant that the folder_sync exclude set and the
# merge set are IDENTICAL — every excluded file is merged and every merged file is
# excluded, so nothing is ever hidden from the mirror without being handled. Both
# `editor_state_exclude_paths()` (the exclude side) and `execute()` (the merge side)
# iterate exactly this tuple.
EDITOR_STATE_HANDLED_RELPATHS: tuple[str, ...] = tuple(
    relpath + suffix for relpath in EDITOR_STATE_DB_RELPATHS for suffix in ("", ".backup")
)


def editor_state_exclude_paths() -> list[Path]:
    """Absolute paths of the INVOKING user's editor state DBs to exclude from the mirror.

    Every file this job merges (``EDITOR_STATE_HANDLED_RELPATHS`` — each editor's
    ``state.vscdb`` and its ``.backup``), resolved against ``Path.home()`` at call time —
    hence a function, not a constant: the paths depend on whoever runs ``pc-switcher``.

    This is the seam ``folder_sync`` consumes: this module owns WHICH absolute paths are
    the editor DBs, and ``folder_sync`` only translates each into a root-anchored rsync
    filter for the folder it is syncing. The set is identical to the merge set (both
    iterate ``EDITOR_STATE_HANDLED_RELPATHS``), so no file is excluded without being
    merged. Scope is the invoking user only (see module docstring); paths are returned
    whether or not the file exists (an absent path is a harmless no-op filter, and
    excluding it still protects a DB created later).
    """
    home = Path.home()
    return [home / relpath for relpath in EDITOR_STATE_HANDLED_RELPATHS]


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


class VscodeStateSyncJob(SyncJob):
    """Selective, SQLite-aware sync of VS Code editor ``state.vscdb`` (ADR-018).

    Mirrors each editor's global state DB except machine-bound ``secret://`` rows,
    which keep the target's own value. Runs as the invoking normal user (no sudo).

    Config shape (mirrors config-schema.yaml ``vscode_state_sync`` section)::

        vscode_state_sync:
          preserve_key_globs: ["secret://%"]   # SQLite LIKE patterns; matches keep the target's value
    """

    name: ClassVar[str] = "vscode_state_sync"

    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "preserve_key_globs": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["secret://%"],
                "description": "SQLite LIKE patterns for keys whose TARGET value is preserved",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        """Initialize the job, reading ``preserve_key_globs`` (default ``["secret://%"]``)."""
        super().__init__(context)
        self.preserve_key_globs: list[str] = context.config.get("preserve_key_globs", ["secret://%"])

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
            scope_items=list(EDITOR_STATE_DB_RELPATHS),
            mechanism="sqlite merge + atomic mv",
        )

    async def validate(self) -> list[ValidationError]:
        """Require ``sqlite3`` on both hosts; the merge drives the CLI on each end.

        No btrfs dependency (CONTEXT decision 7): the job operates on the invoking
        user's own files and does not couple to the snapshot subsystem.
        """
        errors: list[ValidationError] = []

        src = await self.source.run_command("command -v sqlite3")
        if not src.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "sqlite3 is not available on source (required for state.vscdb selective merge)",
                )
            )

        tgt = await self.target.run_command("command -v sqlite3", login_shell=False)
        if not tgt.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "sqlite3 is not available on target (required for state.vscdb selective merge)",
                )
            )

        return errors

    def _resolve_homes(self) -> tuple[Path, Path]:
        """Return ``(source_home, target_home)`` for the INVOKING user only.

        The source is local, so its home is ``Path.home()`` — the invoking user's home
        (this job covers only that user; see module docstring). The target home is
        derived from the resolved SSH username (``target_username`` with a
        ``getpass.getuser()`` fallback, as ``folder_sync`` resolves it) against the local
        home's parent. The fleet is homogeneous in layout (matching arch per ADR-017), so
        the two users' homes share a path shape; this avoids hardcoding a literal
        ``/home/<user>``.
        """
        source_home = Path.home()
        target_user = self.context.target_username or getpass.getuser()
        target_home = source_home.parent / target_user
        return source_home, target_home

    async def execute(self) -> None:
        """Selectively merge each present editor state DB (main + ``.backup``) onto the target.

        Iterates ``EDITOR_STATE_HANDLED_RELPATHS`` — every file folder_sync excludes, so
        the exclude set and the merge set are identical (no exclude without merge). Skips
        files absent on the source, and for each present one runs source-strip -> transfer
        -> target-inject -> atomic ``mv`` (Step C skipped when the target file is absent —
        first sync). In dry-run the job only detects source/target presence and logs the
        intended actions, performing no ``send_file``, no target inject, and no ``mv``
        (ADR-014).
        """
        source_home, target_home = self._resolve_homes()
        prefix = "[dry-run] " if self.context.dry_run else ""
        globs = self.preserve_key_globs

        present = [rel for rel in EDITOR_STATE_HANDLED_RELPATHS if (source_home / rel).exists()]
        if not present:
            self._log(Host.SOURCE, LogLevel.INFO, f"{prefix}No VS Code state DBs found on source; nothing to sync")
            self._report_progress(ProgressUpdate(percent=100))
            return

        total = len(present)
        for index, relpath in enumerate(present):
            source_db = source_home / relpath
            target_db = (target_home / relpath).as_posix()
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
            shutil.copyfile(source_db, local_tmp)
            strip = await self.source.run_command(
                f"sqlite3 {shlex.quote(str(local_tmp))} {shlex.quote(source_strip_sql(globs))}"
            )
            if not strip.success:
                self._raise(Host.SOURCE, label, "source-strip", strip.stderr)

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
                    f"sqlite3 {shlex.quote(remote_tmp)} {shlex.quote(target_inject_sql(target_db, globs))}",
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
