"""The `/etc/apt` unit: back up everything it will touch, write in apt's required order, run
ONE `apt-get update`, and roll the whole thing back if that refresh fails (T-02-34).

Never partially, because a failed metadata refresh with some files written and others not
would leave `/etc/apt` in a configuration nobody reviewed.

The unit is a MIX (ADR-020 D-39): reviewed items — repository and pin removals, apt config in
all three directions — and derived writes, which have no item and so no per-item outcome. A
derived write that fails is recorded against its destination and charged to the packages that
needed it; a rollback marks every derived write failed, exactly as it marks every reviewed one.

Write order is apt's own (§3.3): keys, then pins and apt config, then the distribution's
sources, then the derived vendor repositories, then the approved removals, then unused-key
collection, then the single refresh.

The base `apply()` loop calls `converge()` once per approved diff, so rather than doing each
item's work in ITS OWN call — which would make this transactionality impossible to express
without the base loop knowing about units — the FIRST repository-group (or metadata-refresh)
diff `converge()` sees triggers `ensure_converged`, which does the WHOLE unit's work right
then. Every subsequent call is a cache lookup against the per-item outcome that eager run
recorded, including outcomes for diffs `converge()` has not been called for yet, and outcomes
for diffs a rollback retroactively marks as failed even though their own write succeeded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import uuid4

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.apt_sync.derived import DerivedWrites
from pcswitcher.jobs.apt_sync.files import TargetFiles, backup_path_for, staged_name_for
from pcswitcher.jobs.apt_sync.items import (
    METADATA_REFRESH_ITEM_ID,
    REMOVAL_CLASS_ORDER,
    REPO_GROUP_CLASSES,
    repo_item_destination,
)
from pcswitcher.jobs.apt_sync.keyrings import Keyrings
from pcswitcher.jobs.apt_sync.packages import MetadataRefresh
from pcswitcher.jobs.apt_sync.reporting import Log
from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import Decision
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed
from pcswitcher.models import CommandResult, Host, LogLevel


class EtcApt:
    """The repository unit, converged exactly once per run."""

    def __init__(
        self,
        *,
        target: RemoteExecutor,
        files: TargetFiles,
        keyrings: Keyrings,
        derived: DerivedWrites,
        refresh: MetadataRefresh,
        log: Log,
    ) -> None:
        self._target = target
        self._files = files
        self._keyrings = keyrings
        self._derived = derived
        self._refresh = refresh
        self._log = log
        # Lazily computed the first time `converge()` sees a repository-group item
        # (pin/config/source, or the synthetic metadata-refresh marker): maps each such diff's
        # item_id to (succeeded, message). Populated all at once so the required
        # key-before-source write order and the transactional backup/rollback happen exactly
        # once per run, regardless of which order the base `apply()` loop's per-diff
        # `converge()` calls visit them in.
        self._outcome: dict[str, tuple[bool, str]] | None = None

    async def converge_item(
        self, diff: ItemDiff, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> CommandResult:
        await self.ensure_converged(diffs, decisions)
        assert self._outcome is not None
        succeeded, message = self._outcome[diff.item_id]
        if succeeded:
            return CommandResult(exit_code=0, stdout=message, stderr="")
        raise ConvergeItemFailed(message)

    @staticmethod
    def approved_diffs(diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]) -> list[ItemDiff]:
        """Every repository-group (pin/config/source) diff this run's decisions approved,
        in `plan.diffs` order — already pin/config-before-source (`plan()`'s sort).
        Excludes the synthetic metadata-refresh marker itself, which is tracked separately
        since it names no `/etc/apt` file to back up or write.
        """
        return [
            diff
            for diff in diffs
            if diff.item_class in REPO_GROUP_CLASSES
            and diff.item_id != METADATA_REFRESH_ITEM_ID
            and diff.action in (DiffAction.INSTALL, DiffAction.REMOVE, DiffAction.CHANGE)
            and decisions.get(diff.item_id) == Decision.APPLY
        ]

    async def ensure_converged(self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]) -> None:
        """Do the unit's entire convergence exactly once per run.

        Idempotent: a no-op on every call after the first (`self._outcome` is `None` only until
        this method's first completion). Never called under dry-run — the base `apply()` loop
        never calls `converge()` at all when `self.context.dry_run` is set, so this method's own
        logic can assume real commands are safe to issue.
        """
        if self._outcome is not None:
            return

        group_diffs = self.approved_diffs(diffs, decisions)
        derived_writes = self._derived.all_writes()
        marker_present = decisions.get(METADATA_REFRESH_ITEM_ID) == Decision.APPLY

        # Every keyring write this run owes, decided from the decisions and derivations the
        # run already made — never from a decision about a key, which does not exist.
        keyring_writes = self._keyrings.writes(
            self._keyrings.surviving_refs(diffs, decisions, self._derived.written_source_filenames)
        )
        # "Remove keys after removing sources" is literal: with no source deletion in this
        # run nothing can have become unused, so the collection pass does not run at all.
        collect_unused = any(
            diff.item_class == ItemClass.APT_SOURCE and diff.action == DiffAction.REMOVE for diff in group_diffs
        )

        if not group_diffs and not keyring_writes and not derived_writes:
            self._outcome = (
                {METADATA_REFRESH_ITEM_ID: (True, "no repository changes to refresh for")} if marker_present else {}
            )
            return

        # Populated incrementally (not built up in a local dict and assigned at the
        # end) so a later diff in THIS SAME unit can consult an earlier diff's real
        # outcome while the unit is still being written.
        self._outcome = {}

        staging_dir = await self._files.staging_dir()
        backup_dir = f"{staging_dir}/backup-{uuid4().hex}"

        existed_before: dict[str, bool] = {}
        try:
            for _local, dest in keyring_writes:
                existed_before[dest] = await self._files.backup(dest, backup_dir)
            for dest in derived_writes:
                existed_before[dest] = await self._files.backup(dest, backup_dir)
            for diff in group_diffs:
                dest = repo_item_destination(diff)
                existed_before[dest] = await self._files.backup(dest, backup_dir)
        except ConvergeItemFailed as exc:
            # A backup failure aborts the whole unit before any write happens (T-02-34
            # never partially applies), but `self._outcome` must still end up populated for
            # every group item (D-27) — otherwise the idempotency guard at the top of this
            # method treats the unit as "already handled" on the next `converge()` call, and
            # `converge_item`'s `self._outcome[diff.item_id]` raises a bare `KeyError` for
            # every item after the first, escaping the per-item `ConvergeItemFailed` handler
            # and crashing the whole job instead of failing one item.
            self._record_failure(group_diffs, marker_present, f"repository group backup failed: {exc}")
            return

        # Keys FIRST, before any source file is written: a repository whose keyring has
        # not landed is a repository apt refuses on every subsequent operation, which
        # `Keyrings.gap` turns into a refusal to write that source at all.
        await self._keyrings.provision(keyring_writes, staging_dir)

        await self._write_files(group_diffs, staging_dir)

        # Keys LAST, after every source write and deletion: what a keyring is worth is a
        # reference count over the target's REAL source files, and only now is that count
        # taken against the state the run actually produced.
        if collect_unused:
            await self._keyrings.remove_unused(backup_dir, existed_before)

        update_result = await self._target.run_command(
            "sudo apt-get update",
            login_shell=False,
            mutates="refresh apt package lists against the newly written repository configuration",
        )
        if update_result.success:
            # This IS the run's single metadata refresh: flag it so the install path's
            # `MetadataRefresh.ensure` is a no-op and never issues a second `apt-get update`.
            self._refresh.mark_done()
            await self._files.discard_backup(
                backup_dir, mutates="discard the repository-group backup after a successful refresh"
            )
            if marker_present:
                self._outcome[METADATA_REFRESH_ITEM_ID] = (True, "apt-get update succeeded")
            return

        # Rollback (T-02-34): restore every file that existed before, delete every file
        # the unit created, discard the backup directory, then re-probe apt so the
        # failure summary can tell the user whether the target recovered rather than
        # leaving them to guess.
        recovery = await self._rollback(existed_before, backup_dir)
        self._log(
            Host.TARGET,
            LogLevel.ERROR,
            f"apt-get update failed after repository group writes; rolled back ({recovery}): "
            f"{update_result.stderr.strip()}",
            stderr=update_result.stderr,
        )

        # Every group item is recorded as a failure (D-27) — even ones whose own write
        # just succeeded above — because the rollback undid it: what actually landed on
        # the target is the pre-run state, not what this run intended.
        self._record_failure(
            group_diffs,
            marker_present,
            f"repository group rolled back after apt-get update failure ({recovery}): {update_result.stderr.strip()}",
        )

    async def _rollback(self, existed_before: dict[str, bool], backup_dir: str) -> str:
        """Undo the unit's writes and re-probe apt; return a short phrase describing how the
        target ended up, for the caller's failure summary (T-02-34).

        One command per file, each result inspected. A single `;`-joined command would
        present the `--confirm-each-command` gate one all-or-nothing prompt, but it
        collapses N exit codes into one and makes "which file failed to restore"
        unanswerable — and a failing rollback step is exactly when the user needs that
        file named. Every step is attempted regardless of earlier failures, so one
        unwritable destination cannot strand the remaining files in their post-run state.
        """
        rollback_failures: list[str] = []
        for dest, existed in existed_before.items():
            if existed:
                action = f"restore {dest} from {backup_path_for(backup_dir, dest)}"
                result = await self._files.restore(dest, backup_dir)
            else:
                action = f"delete {dest}, which this run created"
                result = await self._files.delete(dest, mutates=f"ROLLBACK: delete {dest}, which this run created")
            if not result.success:
                rollback_failures.append(f"could not {action}: {result.stderr.strip()}")
                self._log(
                    Host.TARGET,
                    LogLevel.WARNING,
                    f"Rollback step failed — could not {action}: {result.stderr.strip()}",
                    stderr=result.stderr,
                )

        if rollback_failures:
            # The backup directory is deliberately NOT discarded: a failed restore means it
            # holds the only remaining copy of that file's pre-run content, so deleting it
            # would destroy exactly what manual recovery depends on. Name the path — the
            # user has to finish this by hand.
            self._log(
                Host.TARGET,
                LogLevel.WARNING,
                f"Repository-group rollback incomplete; the backup is kept at {backup_dir} on the target "
                f"so the affected file(s) can be restored by hand: {'; '.join(rollback_failures)}",
            )
        else:
            await self._files.discard_backup(
                backup_dir, mutates="ROLLBACK: discard the repository-group backup directory"
            )

        reprobe = await self._target.run_command(
            "sudo apt-get update",
            login_shell=False,
            mutates="ROLLBACK: re-probe apt against the restored repository configuration",
        )
        if reprobe.success:
            # After rollback `/etc/apt` is the pre-run configuration and this reprobe refreshed
            # metadata for it; package installs that still run against that config (D-27 —
            # a repo-group rollback does not cancel package items) then need no further
            # `apt-get update`. If the reprobe itself failed, the flag stays unset and the
            # install path's own refresh attempt will surface the still-broken apt.
            self._refresh.mark_done()

        if rollback_failures:
            # Takes precedence over the reprobe's verdict: an incomplete rollback leaves
            # /etc/apt as neither the pre-run nor the post-run configuration, which a green
            # `apt-get update` would otherwise mask.
            return (
                f"ROLLBACK INCOMPLETE, {len(rollback_failures)} file(s) left unrestored "
                f"(backup kept at {backup_dir}): {'; '.join(rollback_failures)}"
            )
        return "target apt recovered after rollback" if reprobe.success else "target apt still broken after rollback"

    def _record_failure(self, group_diffs: Sequence[ItemDiff], marker_present: bool, message: str) -> None:
        """Mark every `group_diffs` item (and the metadata-refresh marker, if present)
        as failed with `message`, and every DERIVED write with it (D-39).

        Shared by the backup-failure short-circuit and the post-rollback failure path so
        `self._outcome` always ends up fully populated (D-27) — a partially-populated map
        makes a later `converge()` call for an un-recorded item raise `KeyError` instead of
        `ConvergeItemFailed`.
        """
        assert self._outcome is not None
        for diff in group_diffs:
            self._outcome[diff.item_id] = (False, message)
        self._derived.fail_all(message)
        if marker_present:
            self._outcome[METADATA_REFRESH_ITEM_ID] = (False, message)

    async def _write_files(self, group_diffs: Sequence[ItemDiff], staging_dir: str) -> None:
        """Every file operation the unit owes, in apt's own order (§3.3 steps 2-5): pins
        and apt config first, so a pin is in place the moment its origin becomes fetchable
        and an apt-config setting governs the refresh that follows; then the distribution's
        sources; then the derived vendor repositories; then the approved deletions.
        """
        for dest in self._derived.pin_writes:
            await self._write_derived(dest, staging_dir)
        for diff in group_diffs:
            if diff.action != DiffAction.REMOVE:
                await self._converge_write(diff, staging_dir)
        for dest in (*self._derived.distro_writes, *self._derived.repo_writes):
            await self._write_derived(dest, staging_dir)
        # Repository files before pin files before apt config, which is the reverse of the
        # write order and not the order `plan()` sorted the diffs into: a repository still
        # present while its pin is already gone is a fetchable origin nothing prefers,
        # whereas the reverse is a pin naming an origin apt no longer has.
        removals = sorted(
            (diff for diff in group_diffs if diff.action == DiffAction.REMOVE),
            key=lambda diff: REMOVAL_CLASS_ORDER.get(diff.item_class, 0),
        )
        for diff in removals:
            await self._converge_write(diff, staging_dir)

    async def _converge_write(self, diff: ItemDiff, staging_dir: str) -> None:
        """Run one REVIEWED item's file operation and record its per-item outcome
        (D-27), so a single failing file never stops the rest of the unit."""
        assert self._outcome is not None
        try:
            await self._write_or_remove(diff, staging_dir)
        except ConvergeItemFailed as exc:
            self._outcome[diff.item_id] = (False, str(exc))
        else:
            self._outcome[diff.item_id] = (True, "converged")

    async def _write_derived(self, dest: str, staging_dir: str) -> None:
        """Copy one DERIVED `/etc/apt` file from the source, logging what travelled and
        recording a failure against the destination rather than against an item (D-39).

        There is no item to fail: the user decided about a package, and
        `DerivedWrites.install_refusal` is what turns this destination's failure into that
        package's refusal. The FULL line is how a derived write stays visible at all — it has
        no review entry to appear on (ADR-014).
        """
        gap = self._keyrings.gap(dest)
        if gap is not None:
            self._derived.record_failure(dest, gap)
            self._log(Host.TARGET, LogLevel.ERROR, f"not writing {dest}: {gap}", stderr=gap)
            return
        try:
            await self._files.stage_and_promote(dest, dest, staging_dir, staged_name_for(dest))
        except ConvergeItemFailed as exc:
            self._derived.record_failure(dest, str(exc))
            self._log(Host.TARGET, LogLevel.ERROR, f"failed to write {dest}: {exc}", stderr=str(exc))
            return
        self._log(Host.TARGET, LogLevel.FULL, f"wrote {dest} from the source")

    async def _write_or_remove(self, diff: ItemDiff, staging_dir: str) -> None:
        """Converge one REVIEWED repository-group diff: `sudo rm --force` for a REMOVE, or
        `stage_and_promote` for an apt-config INSTALL/CHANGE (T-02-35).
        """
        dest = repo_item_destination(diff)

        if diff.action == DiffAction.REMOVE:
            result = await self._files.delete(dest, mutates=f"delete repository file {dest}")
            if not result.success:
                raise ConvergeItemFailed(f"failed to remove {dest}: {result.stderr.strip()}")
            return

        staged_name = diff.item_id.replace(":", "_").replace("/", "_")
        await self._files.stage_and_promote(dest, dest, staging_dir, staged_name)
