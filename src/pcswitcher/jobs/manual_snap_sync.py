"""`manual_snap_sync`: sideloaded snaps — the snaps installed from a local `.snap` file
rather than from the store, which no store can serve to the other machine (`PKG-FR-JOB-INDEPENDENCE`,
`PKG-FR-MANUAL-SCOPE`,
`PKG-FR-SNAP-SIDELOAD`).

Detection is one question asked of `snap list --all`: which snaps snapd renders at an
`x`-prefixed revision, the store-less revision it assigns to a `snap install --dangerous`
or a `snap try`. On the target the question is only whether snapd reports the NAME
installed at all: software that is there is there, whatever put it there.

Its own job, on its own enable flag, for the reason `PKG-FR-JOB-INDEPENDENCE` gives every package job one: an
independent failure surface, an independent review and an independent switch. It sits
beside `manual_deb_sync` (hand-installed `.deb` packages) and `manual_installs_sync`
(unowned software under `/usr/local` and `/opt`). All three subclass
`UnreproducibleSyncJob` and share one install-snippet registry; none imports another, and
none imports the package-manager job it is paired with (`PKG-FR-MANUAL-SCOPE`).

The snap handoff is capture-time exclusion, not a message: `snap_sync` withholds the same
names from both its manifests using the shared `packages/snap_listing.py` predicate, and
this job independently re-runs it. Two jobs, one predicate, no result passed between them
(`PKG-FR-JOB-INDEPENDENCE`). The consequence the user must know: this job's enable flag is its own, so
enabling `snap_sync` while disabling this one leaves sideloaded snaps replicated by nobody
— which is what the whole run did before this job existed.
"""

from __future__ import annotations

import shlex
from collections.abc import Collection, Mapping, Sequence
from typing import Any, ClassVar, override

from pcswitcher.executor import Executor
from pcswitcher.jobs.packages.probes import require_answer
from pcswitcher.jobs.packages.snap_listing import SnapItem, is_sideloaded, parse_snap_list
from pcswitcher.jobs.packages.state import DecisionEntry
from pcswitcher.jobs.packages.unreproducible import UnreproducibleItem, UnreproducibleSyncJob
from pcswitcher.models import FirstSyncScope, Host, ValidationError

__all__ = ["ManualSnapSyncJob"]

# The origin every item this job produces carries, and so the slice of an `item_id` space
# that belongs to it. Named once: detection and the mark reconciliation key on the same
# string.
_ORIGIN = "snap-sideload"

# This job reads its own `manual_snap.decisions.yaml` and nothing else (`PKG-FR-MACHINE-SPECIFIC`: one file per
# manager). A `snap:<name>` mark in `snap.decisions.yaml` names the same snap but answers
# `snap_sync`'s question — "do not converge this snap's revision" — not this one, so it is
# left where it is.


class ManualSnapSyncJob(UnreproducibleSyncJob):
    """Detect, review and reproduce sideloaded snaps, on this job's own enable flag
    independent of `snap_sync`'s and of the other unreproducible jobs'.

    Supplies the two detection hooks `UnreproducibleSyncJob` leaves abstract; everything
    from the diff onwards — the snippet registry, its push and consent question, the
    review grouping and the replay — is inherited.
    """

    name: ClassVar[str] = "manual_snap_sync"
    manager_id: ClassVar[str] = "manual_snap"

    # No configurable properties: mirrors SnapSyncJob's empty schema — only the enable flag
    # in sync_jobs is needed. A job earns a config SECTION only when it has a real key, so
    # there is no `manual_snap_sync:` block in default-config.yaml, but the in-code CONFIG_SCHEMA
    # ClassVar still declares the empty object every job carries.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    # -- Detection (`PKG-FR-MANUAL-SCOPE`), run on both machines (`PKG-FR-MANUAL-DIFF`) -------------------

    async def _installed_snaps(self, executor: Executor, machine: str) -> list[SnapItem]:
        """Every snap snapd reports on `machine`, sideloaded or not.

        Guarded on the EXIT CODE ONLY (ADR-022), not additionally on emptiness the way
        `manual_deb_sync` guards its `dpkg-query`: a machine with no packages installed
        does not exist, but a machine with no snaps does, and `snap list --all` separates
        the two cleanly — snapd unreachable exits 1, while zero snaps installed exits 0
        with the hint on stderr and nothing on stdout (measured, see `packages/probes.py`).
        So empty stdout at exit 0 is data here, and reading it as a failure would fail the
        job on an ordinary machine.
        """
        command = "snap list --all"
        result = await executor.run_command(command)
        require_answer(command, result, machine)
        return parse_snap_list(result.stdout)

    @override
    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:
        """The source's sideloaded snaps (`PKG-FR-SNAP-SIDELOAD`).

        The identifier is the snap's NAME and never its revision, even though the revision
        is exactly what makes the snap a sideload. A sideload's revision moves on every
        reinstall from a newer `.snap` file (`x1` -> `x2`), so putting it in the identity
        would make each reinstall a brand-new item: the user's install snippet would stop
        resolving and their "never install this on the other machine" mark would be
        orphaned, both silently, and the snap would be put back in front of them as if it
        had never been answered about. The revision is carried in the LABEL instead, where
        it tells the user which build they are being asked about without becoming part of
        what the answer is filed under.
        """
        return [
            UnreproducibleItem(
                origin=_ORIGIN,
                identifier=item.name,
                label=f"{item.name} (sideloaded snap, revision {item.revision})",
            )
            for item in sorted(await self._installed_snaps(self.source, self.machines.source), key=lambda i: i.name)
            if is_sideloaded(item)
        ]

    @override
    async def query_target_items(self) -> Sequence[UnreproducibleItem]:
        """What the TARGET holds, in the source's own identities (`PKG-FR-MANUAL-DIFF`),
        from one `snap list --all`.

        A snap is HELD when snapd reports the NAME installed at all — at any revision,
        sideloaded or from the store — so the source's copy of that name is never offered
        for install. The two cases the target can present differ in what happens next, and
        the difference is the snap's declared VERSION, never its revision:

        - the target holds a SIDELOAD of that name. Which `.snap` file each machine was fed
          is not knowable from either listing and the two `x<N>` numbers are independent
          install counters, so the comparison is on the `Version` column: equal versions are
          convergence, and a difference is the drift `PKG-FR-MANUAL-VERSION` asks about.
        - the target holds a STORE snap of that name. It is the same application by a route
          needing no snippet, and `snap_sync` has withheld the name on both machines
          (`PKG-FR-SNAP-SIDELOAD`), so nothing else in the run touches it either. Its
          version is compared like any other: the source's sideload is what a run replicates,
          and a target left behind on an older store build is the same drift.

        `own_finding` is the sideload half alone, so only a snap the target itself
        sideloaded can become a removal once the source drops it
        (`PKG-FR-MANUAL-REMOVE`). A store snap the source lacks is `snap_sync`'s business,
        not this job's, and offering to delete one here would take software off the update
        path it is on.
        """
        return [
            UnreproducibleItem(
                origin=_ORIGIN,
                identifier=item.name,
                label=f"{item.name} (sideloaded snap, revision {item.revision})" if is_sideloaded(item) else item.name,
                own_finding=is_sideloaded(item),
            )
            for item in await self._installed_snaps(self.target, self.machines.target)
        ]

    @override
    async def installed_versions(self, item_ids: Collection[str], *, on_source: bool) -> Mapping[str, str | None]:
        """Each snap's declared version on one machine, from one `snap list --all`.

        The `Version` column, never `Rev`: a sideload's revision moves on every reinstall
        from a newer file, so comparing revisions would report a difference between two
        machines' install counters. A snap whose listing carries no version at all answers
        `None`, which produces no item rather than a claimed difference.
        """
        executor = self.source if on_source else self.target
        machine = self.machines.source if on_source else self.machines.target
        versions = {item.name: item.version for item in await self._installed_snaps(executor, machine)}
        prefix = UnreproducibleItem.id_prefix(_ORIGIN)
        return {item_id: versions.get(item_id.removeprefix(prefix)) or None for item_id in item_ids}

    @override
    def removal_command(self, item: UnreproducibleItem) -> str:
        """`snap remove` for a sideload the source no longer has.

        Plain `snap remove`, so snapd's own pre-removal snapshot is left in place — the one
        recovery path if the removal was a mistake, and the more valuable one here, since no
        store can serve the revision back (`PKG-FR-SNAP-REMOVE-SNAPSHOT`).
        """
        return f"sudo snap remove {shlex.quote(item.identifier)}"

    @override
    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """The marked snaps one machine no longer has installed.

        Asked of BOTH machines, unlike `plan()`, which reads the source's file alone. The
        two questions are different: which marks silence a FINDING is the source's business,
        because a finding is something the source has and the target lacks, but whether a
        marked item is still on the machine holding the mark is a question about that machine
        and nothing else. Reconciling the source's file alone would leave a machine that is
        only ever synced TO carrying its dead marks for good.

        Presence is the whole test, exactly as it is in `query_target_items`: a marked snap
        the user has since replaced with the store's copy of the same name is still
        installed, and dropping its mark on the grounds that it stopped being a sideload
        would re-offer software the user asked to be left alone — under a snippet that would
        overwrite the store copy.

        Entries this job cannot recognise are left exactly where they are. Its file holds
        only its own, so that is only ever a hand-edited one.
        """
        executor = self.source if on_source else self.target
        machine = self.machines.source if on_source else self.machines.target

        prefix = UnreproducibleItem.id_prefix(_ORIGIN)
        snaps = {item_id: item_id.removeprefix(prefix) for item_id in entries if item_id.startswith(prefix)}
        if not snaps:
            return frozenset()

        installed = {item.name for item in await self._installed_snaps(executor, machine)}
        return frozenset(item_id for item_id, name in snaps.items() if name not in installed)

    @override
    async def validate(self) -> list[ValidationError]:
        """`snap version` on both machines — the target is read too, since a finding it
        already holds is not presented (`PKG-FR-MANUAL-DIFF`).

        No sudo on either machine, unlike `snap_sync`, which needs it on both: detection
        here only lists, and `snap list --all` needs no privilege. A snippet's own sudo
        needs are unpredictable (an opaque blob, `PKG-FR-SNIPPET-VERBATIM`) — a sideload's snippet will usually
        want it, since `snap install --dangerous` does — so this job does NOT pre-validate
        target sudo either; a snippet that needs it and lacks it fails as a per-item
        converge failure (`PKG-FR-OUTCOME-FAILED`), reported like any other. An approved removal needs it too
        and is treated the same way: a run that approves none needs no privilege at all, and
        demanding it up front would refuse the job to every user who only ever installs.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `SnapSyncJob.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        source_check = await self.source.run_command("snap version")
        if not source_check.success:
            errors.append(
                self._validation_error(Host.SOURCE, "snap is not available on source (required to detect sideloads)")
            )

        target_check = await self.target.run_command("snap version", login_shell=False)
        if not target_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET, "snap is not available on target (required to tell what it already has)"
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): replaying install
        snippets for sideloaded snaps, and removing the ones the source has dropped."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=[
                "sideloaded snaps (via recorded install snippets)",
                "sideloaded snaps the source no longer has (snap remove)",
            ],
            mechanism="replay install snippet or remove, per item, after review",
        )
