"""`manual_deb_sync`: hand-installed `.deb` packages — the software `dpkg --install` put
on a machine and no repository can put on the other one (D-15, D-18, `PKG-FR-DEB-OWNERSHIP`).

Detection is one question asked of the whole INSTALLED set: which packages' INSTALLED
version comes from no repository the SOURCE has configured. Not the `apt-mark showmanual`
set — apt's manual/automatic mark says how a package got there, not whether any repository
can supply it, so a `.deb` pulled in to satisfy another one is outside that set and is
still software no package manager can put on the other machine.

The target is read twice over, from one `dpkg-query`: every name it reports installed is
software that is there, whatever origin put it there, so the source's copy of that name is
never offered for install — and among the names the source does NOT have installed at all,
the same no-repository question is asked of the target's own apt, so a hand-installed `.deb`
the source has since dropped becomes a removal (`PKG-FR-MANUAL-REMOVE`). Narrowing that
second question to the names the source lacks is what keeps it affordable: the full
`apt-cache policy` over an installed set costs about three seconds and 700KB, and every
name the source still has is answered by the source's own read already.

Its own job, on its own enable flag, for the reason D-15 gives every package job one: an
independent failure surface, an independent review and an independent switch. It sits
beside `manual_installs_sync`, which owns the OTHER thing no package manager can reproduce
— unowned software under `/usr/local` and `/opt`, which is not a package at all. Both
subclass `UnreproducibleSyncJob` and share one install-snippet registry; neither imports
the other, and neither imports `apt_sync` (D-18).

The apt handoff is capture-time exclusion, not a message: `apt_sync` drops the same
packages from both its manifests using the shared `packages/apt_policy.py` predicate, and
this job independently re-runs it. Two jobs, one predicate, no result passed between them
(D-15/D-16). The consequence the user must know: this job's enable flag is its own, so
enabling `apt_sync` while disabling this one leaves hand-installed `.deb` packages
replicated by nobody.
"""

from __future__ import annotations

import shlex
from collections.abc import Collection, Mapping, Sequence
from typing import Any, ClassVar, override

from pcswitcher.executor import Executor
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.apt_policy import installed_origins_by_package, packages_installed_from_no_repository
from pcswitcher.jobs.packages.probes import require_answer
from pcswitcher.jobs.packages.state import DecisionEntry
from pcswitcher.jobs.packages.unreproducible import UnreproducibleItem, UnreproducibleSyncJob, lines_of
from pcswitcher.models import FirstSyncScope, Host, ValidationError

__all__ = ["ManualDebSyncJob"]

# The origin every item this job produces carries, and so the slice of an `item_id` space
# that belongs to it. Named once: detection and the mark reconciliation key on the same
# string.
_ORIGIN = "apt-no-candidate"


class ManualDebSyncJob(UnreproducibleSyncJob):
    """Detect, review and reproduce apt packages installed from no configured repository,
    on this job's own enable flag independent of `apt_sync`'s and of
    `manual_installs_sync`'s.

    Supplies the two detection hooks `UnreproducibleSyncJob` leaves abstract; everything
    from the diff onwards — the snippet registry, its push and consent question, the
    review grouping and the replay — is inherited.
    """

    name: ClassVar[str] = "manual_deb_sync"
    manager_id: ClassVar[str] = "manual_deb"

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        # The source's installed set, read once per run. Both detection hooks need it —
        # `capture_source_items` to ask apt about it, `query_target_items` to know which of
        # the target's names the source does not have — and the source is the machine a sync
        # never changes, so one read answers both whichever order they are called in.
        self._source_installed_cache: dict[str, str] | None = None

    # No configurable properties: mirrors AptSyncJob's empty schema — only the enable flag
    # in sync_jobs is needed. A job earns a config SECTION only when it has a real key, so
    # there is no `manual_deb_sync:` block in default-config.yaml, but the in-code CONFIG_SCHEMA
    # ClassVar still declares the empty object every job carries.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    # -- Detection (D-18), run on both machines (`PKG-FR-MANUAL-DIFF`) -------------------

    async def _scan_no_candidate_apt_packages(
        self, installed_names: Sequence[str], executor: Executor, machine: str
    ) -> frozenset[str]:
        """D-18: of `installed_names`, those whose INSTALLED version comes from no repository
        `machine` has configured — put there by `dpkg --install` of a bare `.deb`.

        Over the whole INSTALLED set, not `apt-mark showmanual`. `PKG-FR-MANUAL-SCOPE` draws
        the boundary at "every installed version no configured repository supplies", and
        apt's manual/automatic mark is a different fact: a `.deb` installed to satisfy
        another one, or one the user ran `apt-mark auto` over, is outside the manual set and
        is still software no package manager can put on the other machine. Narrowing to the
        manual set left it invisible to this job and — being automatic — invisible to
        `apt_sync` as well, so nothing named it anywhere.

        One batched `apt-cache policy` over that set (never one call per package), read
        through `packages_installed_from_no_repository`: a package's own `Candidate:` line
        cannot answer this, because dpkg's status entry makes apt report a hand-installed
        package's installed version as its candidate. Measured on the development machine
        (Ubuntu 24.04, apt 2.8.3): 2282 installed against 153 manual, 3.1s against 0.4s and
        718KB of output against 96KB — one command either way, and the wider set is what the
        article asks for.

        Guarded on the exit code AND on the block count (ADR-022 D-04), which is the guard
        `apt_sync._source_policy` puts on its own copy of this command: same host, same
        probe, so the same strictness. Its silence indicts nothing on its own — an
        unanswered probe reports no unreproducible packages, which proposes nothing — but
        it does not stay harmless in a whole run: `apt_sync.capture_source_items` DROPS the
        same bare-`.deb` packages from its own manifest off its own copy of this probe, so
        one probe answering and the other not makes a package vanish from the run with
        nothing said about it anywhere. Every name here is installed on this machine, so apt
        owes a block for each and no block at all is apt not answering rather than a machine
        with unusual packages.

        Run on the TARGET too, over the names the source does not have installed, so a `.deb`
        the source has dropped can become a removal (`PKG-FR-MANUAL-REMOVE`). The same guard
        holds there for the same reason: every name handed to it is installed on the machine
        being asked.
        """
        if not installed_names:
            return frozenset()

        quoted = " ".join(shlex.quote(name) for name in installed_names)
        command = f"apt-cache policy {quoted}"
        result = await executor.run_command(command)
        # A key per block apt printed, whatever it said inside it — so this counts blocks and
        # not packages, and a machine whose whole manual set is bare `.deb`s still answers.
        require_answer(
            command,
            result,
            machine,
            answers=len(installed_origins_by_package(result.stdout)),
            answer_noun="package block",
        )
        return frozenset(packages_installed_from_no_repository(result.stdout, installed_names))

    async def _installed(self, executor: Executor, machine: str) -> dict[str, str]:
        """`name -> installed version` for everything dpkg reports as INSTALLED on `machine`.

        The population `PKG-FR-MANUAL-SCOPE` draws the no-candidate scan from on the source,
        the whole of what the target holds (`PKG-FR-MANUAL-DIFF`), and the version each
        machine is compared on (`PKG-FR-MANUAL-VERSION`) — one read answering all three,
        since dpkg prints the version in the same record as the name.

        `${Package}`, not `${binary:Package}`: the arch-qualified form only appears for a
        foreign architecture, and `apt-cache policy` speaks the plain name. Two dpkg entries
        for one name (multi-arch) therefore collapse to one, which is what the batched policy
        call wants anyway.

        Guarded on the exit code AND on emptiness (ADR-022): a machine with no installed
        packages does not exist, so nothing here is a legitimate empty answer, and silence
        read as data would report "nothing on this machine was hand-installed" — the one
        answer this job exists to be able to contradict.
        """
        command = "dpkg-query --show --showformat='${Package}\\t${Version}\\t${db:Status-Status}\\n'"
        result = await executor.run_command(command)
        installed: dict[str, str] = {}
        for line in lines_of(result.stdout):
            fields = line.split("\t")
            if len(fields) == 3 and fields[2] == "installed":
                installed[fields[0]] = fields[1]
        require_answer(command, result, machine, answers=len(installed), answer_noun="installed package")
        return installed

    async def _source_installed(self) -> dict[str, str]:
        """The source's installed set, read once per run (see `__init__`)."""
        if self._source_installed_cache is None:
            self._source_installed_cache = await self._installed(self.source, self.machines.source)
        return self._source_installed_cache

    @override
    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:
        """The source's hand-installed `.deb` packages. One `dpkg-query` names the source's
        installed set here and its result feeds the no-candidate scan.
        """
        installed = await self._source_installed()
        no_repository = await self._scan_no_candidate_apt_packages(
            sorted(installed), self.source, self.machines.source
        )
        return [
            UnreproducibleItem(
                origin=_ORIGIN,
                identifier=name,
                label=f"{name} (installed from no configured repository)",
            )
            for name in sorted(no_repository)
        ]

    @override
    async def query_target_items(self) -> Sequence[UnreproducibleItem]:
        """What the TARGET holds, in the source's own identities (`PKG-FR-MANUAL-DIFF`).

        Every name dpkg reports installed is here, whatever origin put it there: software
        that is on the machine is on the machine, so the source's copy of that name is never
        offered for install and its two versions are compared instead.

        `own_finding` is the second reading, and it costs the one extra command this job
        makes: among the names the source does NOT have installed at all, the target's own
        apt is asked the same no-repository question, and a name it answers yes to is a
        hand-installed `.deb` the source has dropped — a removal (`PKG-FR-MANUAL-REMOVE`).
        Every other row is left unflagged, so nothing this job could not put back is ever
        offered for deletion. Narrowing to the names the source lacks is what keeps the cost
        proportionate: the same question over a whole installed set costs about three seconds
        and 700KB, and a name the source still has cannot be a removal whatever apt says
        about it.
        """
        installed = await self._installed(self.target, self.machines.target)
        source_installed = await self._source_installed()
        target_only = sorted(name for name in installed if name not in source_installed)
        no_repository = await self._scan_no_candidate_apt_packages(target_only, self.target, self.machines.target)
        return [
            UnreproducibleItem(
                origin=_ORIGIN,
                identifier=name,
                label=f"{name} (installed from no configured repository)" if name in no_repository else name,
                own_finding=name in no_repository,
            )
            for name in sorted(installed)
        ]

    @override
    async def installed_versions(self, item_ids: Collection[str], *, on_source: bool) -> Mapping[str, str | None]:
        """Each package's installed version on one machine, from one `dpkg-query`.

        Read fresh rather than taken from the capture: the converge loop asks this again
        after every replay, and an answer from before the change would report every
        convergence as successful. A record dpkg prints with an empty version answers `None`
        like a name it does not have at all — both mean this machine did not say.
        """
        executor = self.source if on_source else self.target
        machine = self.machines.source if on_source else self.machines.target
        installed = await self._installed(executor, machine)
        prefix = UnreproducibleItem.id_prefix(_ORIGIN)
        return {item_id: installed.get(item_id.removeprefix(prefix)) or None for item_id in item_ids}

    @override
    def removal_command(self, item: UnreproducibleItem) -> str:
        """`apt-get remove` for a hand-installed `.deb` the source no longer has.

        Never `purge`, for `PKG-FR-APT-REMOVE`'s reason: what apt leaves under `/etc` can be
        deleted by hand at any time, and a purge cannot be undone.
        """
        return f"sudo apt-get remove --assume-yes {shlex.quote(item.identifier)}"

    @override
    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """The marked packages one machine no longer has installed.

        Asked of BOTH machines, unlike `plan()`, which reads the source's file alone. The
        two questions are different: which marks silence a FINDING is the source's business,
        because a finding is something the source has and the target lacks, but whether a
        marked item is still on the machine holding the mark is a question about that machine
        and nothing else. Reconciling the source's file alone would leave a machine that is
        only ever synced TO carrying its dead marks for good.

        Asks dpkg's installed set directly rather than re-running the no-candidate analysis:
        a marked package that has since become reproducible from a repository is still
        installed, and dropping its mark on those grounds would re-offer software the user
        asked to be left alone.

        Entries this job cannot recognise are left exactly where they are.
        """
        executor = self.source if on_source else self.target
        machine = self.machines.source if on_source else self.machines.target

        prefix = UnreproducibleItem.id_prefix(_ORIGIN)
        packages = {item_id: item_id.removeprefix(prefix) for item_id in entries if item_id.startswith(prefix)}
        if not packages:
            return frozenset()

        installed = await self._installed(executor, machine)
        return frozenset(item_id for item_id, name in packages.items() if name not in installed)

    @override
    async def validate(self) -> list[ValidationError]:
        """The commands this job's own detection runs: `apt-cache` and `dpkg` on the source,
        and `apt-cache` and `dpkg` on the target, which is read too, for what it already
        holds and for the hand-installed `.deb`s the source has dropped
        (`PKG-FR-MANUAL-DIFF`, `PKG-FR-MANUAL-REMOVE`). Both machines are only ever read for
        detection, so no sudo is needed for it.

        Target sudo is deliberately still NOT pre-validated, although an approved removal
        needs it: a snippet's own privilege needs are unpredictable (an opaque blob, D-20),
        a run that approves no removal needs none, and failing validation up front would
        refuse the job to every user who only ever installs. A removal that lacks the
        privilege fails as a per-item converge failure (D-27), reported like any other.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `AptSyncJob.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        apt_cache_check = await self.source.run_command("apt-cache --version")
        if not apt_cache_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE, "apt-cache is not available on source (required to detect unreproducible packages)"
                )
            )

        target_apt_cache_check = await self.target.run_command("apt-cache --version")
        if not target_apt_cache_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "apt-cache is not available on target (required to tell which of its packages no repository "
                    "can supply)",
                )
            )

        dpkg_check = await self.source.run_command("dpkg --version")
        if not dpkg_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE, "dpkg is not available on source (required to read the installed package set)"
                )
            )

        target_dpkg_check = await self.target.run_command("dpkg --version")
        if not target_dpkg_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET, "dpkg is not available on target (required to tell what it already has)"
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): replaying install
        snippets for hand-installed `.deb` packages, and removing the ones the source has
        dropped."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=[
                "hand-installed .deb packages (via recorded install snippets)",
                "hand-installed .deb packages the source no longer has (apt-get remove)",
            ],
            mechanism="replay install snippet or remove, per item, after review",
        )
