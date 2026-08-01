"""Converging one package, one removal, one hold — and the guard chain that decides whether
each may run at all.

The chain's ORDER is the load-bearing part, so it is written once, here, in the order it must
run: a derived file that never landed refuses the install before any command at all (D-39,
cheapest and most specific); then the run's single `apt-get update`; then the origin read-back
(D-35, one cached lookup); then the transaction simulation and the collateral guard (D-30, one
command). Each step is stated as its own early return so the sequence is readable as a
sequence rather than inferred from nesting.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.apt_sync.collateral import Collateral, CollateralEffect, LateCollateral
from pcswitcher.jobs.apt_sync.commands import (
    candidate_version,
    install_args,
    policy_command,
    remove_args,
    simulate_apt_transaction,
)
from pcswitcher.jobs.apt_sync.derived import DerivedWrites
from pcswitcher.jobs.apt_sync.items import APT_PACKAGE_ID_PREFIX, hold_name, package_name
from pcswitcher.jobs.apt_sync.origins import OriginClassifier
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.review import Decision
from pcswitcher.jobs.packages.sync_core import ConvergeItemDeclined, ConvergeItemFailed
from pcswitcher.models import CommandResult


class MetadataRefresh:
    """The run's single `apt-get update`, whoever issues it.

    At most ONE runs per run across both paths: the repository unit's own refresh and the
    pre-install refresh set the same flag, so the second path becomes a no-op. A failure on the
    install path is cached so every remaining install aborts on the same error WITHOUT issuing
    a second `apt-get update` — the "at most one" guarantee holds even on the failure path.
    """

    def __init__(self) -> None:
        self._refreshed = False
        self._error: str | None = None

    @property
    def done(self) -> bool:
        return self._refreshed

    def mark_done(self) -> None:
        """Record that a refresh has already happened this run — called by the repository
        unit, whose own `apt-get update` IS the run's single refresh."""
        self._refreshed = True

    async def ensure(self, target: RemoteExecutor, manager_id: str) -> None:
        """Run exactly one `apt-get update` before the first package install of a run that
        approves an INSTALL but changes no repository-group item — resolving installs against
        a stale package list can pick candidates the target can no longer fetch. A no-op once
        metadata has already been refreshed this run, INCLUDING by the repository unit's own
        `apt-get update`, so the two refresh paths never both fire.

        Aborts the install by raising `ConvergeItemFailed` if the refresh fails: unlike the
        repository-unit path — which has `/etc/apt` writes to roll back and owns that
        behaviour — this path made no changes, so failing the item (installing nothing) is
        its whole safe response. Never reached under dry-run: the base `apply()` loop does not
        call `converge()` when `self.context.dry_run` is set.
        """
        if self._refreshed:
            return
        if self._error is not None:
            raise ConvergeItemFailed(self._error)

        result = await target.run_command(
            "sudo apt-get update",
            login_shell=False,
            mutates="refresh apt package lists before the first install of this run",
        )
        if not result.success:
            self._error = (
                f"apt-get update failed before installing {manager_id} packages; refusing to install "
                f"against a stale package list (decision 1): {result.stderr.strip()}"
            )
            raise ConvergeItemFailed(self._error)
        self._refreshed = True


class PackageConverger:
    """One package per invocation (D-27) so a single bad package cannot fail the whole batch,
    and so each package's simulation corresponds exactly to the command that follows it. The
    target resolves dependencies and downloads from its own repos (D-28) — no source cache is
    consulted.
    """

    def __init__(
        self,
        *,
        target: RemoteExecutor,
        manager_id: str,
        machines: Machines,
        collateral: Collateral,
        derived: DerivedWrites,
        origins: OriginClassifier,
        refresh: MetadataRefresh,
        late: LateCollateral | None = None,
        held_versions: Mapping[str, str] | None = None,
    ) -> None:
        self._target = target
        self._manager_id = manager_id
        self._machines = machines
        self._collateral = collateral
        self._derived = derived
        self._origins = origins
        self._refresh = refresh
        # The mid-apply collateral question for the installs plan time could not simulate
        # (`PKG-FR-ASK-AGAIN`). Optional so a test converging one package by hand needs no
        # reviewer; a run without one simply has no late question to put.
        self._late = late
        # Package names whose install a kept collateral package withdrew. Read by `hold`,
        # which may not register a hold for a package that never landed and must not call
        # that a failure either.
        self._declined_installs: set[str] = set()
        # `{package name: the version the SOURCE holds it at}` (`PKG-FR-APT-HOLD-VERSION`).
        self._held_versions = dict(held_versions or {})
        # `{package name: why its install failed}`, and `None` for one that succeeded. Read
        # by `hold`, which converges after every install (`accept_review` orders holds last)
        # and may not register a hold for a package that never landed.
        self._install_outcome: dict[str, str | None] = {}

    async def install(
        self, diff: ItemDiff, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> CommandResult:
        """Simulate, then apply, one apt install — the last line of defence behind the
        plan-time collateral classification (D-30). Auto-installed collateral (a package
        apt pulls in that `Collateral.protected` does not cover) proceeds silently but is
        logged. A protected package's collateral removal, downgrade or upgrade is refused
        unless the user let it go ahead in the review; the decision was made at plan time,
        and this guard only verifies the real transaction has not drifted to touch a
        protected package nobody saw.

        Every outcome is recorded against the package name, because the hold that may follow
        it must not be registered for a package that never landed
        (`PKG-FR-APT-HOLD-INERT`).

        The mid-apply collateral question comes first, and comes once: before this run's
        first install command rather than between two of them, so every question is put
        before any of the transactions they are about (`PKG-FR-BATCHED`,
        `PKG-FR-CONSENT-BEFORE-CHANGE`). An install a kept package withdrew is DECLINED, not
        failed — that is the article's own remedy for keeping a package
        (`PKG-FR-COLLATERAL-MANUAL`), and it holds however late the answer came: the same
        outcome covers a question this simulation's own drift raises.
        """
        name = package_name(diff.item_id)
        if self._late is not None:
            await self._late.ensure_asked(diffs, decisions)
            withdrawn = self._late.declined(diff.item_id)
            if withdrawn is not None:
                self._declined_installs.add(name)
                raise ConvergeItemDeclined(f"install of {name} withdrawn: {withdrawn}")
        try:
            result = await self._install(name, diff, diffs, decisions)
        except ConvergeItemDeclined:
            self._declined_installs.add(name)
            raise
        except ConvergeItemFailed as exc:
            self._install_outcome[name] = str(exc)
            raise
        self._install_outcome[name] = None if result.success else result.stderr.strip()
        return result

    async def _install(
        self, name: str, diff: ItemDiff, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> CommandResult:
        """`install`'s guard chain and command, without the outcome bookkeeping."""
        # A derived `/etc/apt` write this package needed and that failed refuses it first
        # (D-39), before any command at all: the file is named, which the origin check could
        # only say the consequence of.
        blocked = self._derived.install_refusal(diff.item_id, name)
        if blocked is not None:
            raise ConvergeItemFailed(blocked)

        await self._refresh.ensure(self._target, self._manager_id)

        # The origin check runs immediately after the refresh and before the collateral
        # simulation: refusing an install whose origin is wrong costs one cached lookup,
        # while simulating it costs a command.
        refusal = await self._origins.refusal(name, diffs=diffs, decisions=decisions, target=self._target)
        if refusal is not None:
            raise ConvergeItemFailed(refusal)

        # A held package is requested as `<name>=<version>` — apt's own way of asking for one
        # version and refusing rather than substituting another (`PKG-FR-APT-HOLD-VERSION`).
        # An entry with no version is a held candidate whose version the source's own
        # `dpkg-query` did not supply: there is nothing to ask apt for, and the article
        # forbids falling back to whatever the target offers, so the install is refused. The
        # capture guards make that state unreachable today (`AptProbe._resolve_versions`
        # fails the job on a non-zero exit, and every name it queries comes from
        # `apt-mark showmanual`, so it is installed and has a version); the branch exists so
        # a future capture that does return an empty version cannot float the install.
        held = self._held_versions.get(name)
        if held == "":
            raise ConvergeItemFailed(
                f"install of {name} refused: {self._machines.source} holds it and no installed version was "
                f"captured there, so {self._machines.target} cannot be given {self._machines.source}'s version. "
                "A held package is installed at that version or not at all, because a hold freezes whatever "
                "version lands"
            )
        args = install_args([name if held is None else f"{name}={held}"])
        try:
            preview = await simulate_apt_transaction(self._target, args, login_shell=False)
        except ConvergeItemFailed as exc:
            if held is None:
                raise
            raise ConvergeItemFailed(await self._held_version_refusal(name, held, exc)) from exc

        refused = await self._collateral.unapproved(preview, exempt=frozenset(), verb="Installing", subject=name)
        if refused:
            effects = ", ".join(effect.phrase for effect in refused)
            await self._settle_drift(
                refused,
                verb="Installing",
                name=name,
                refusal=(
                    f"install of {name} refused: apt-get --dry-run would {effects}, "
                    "which was not approved as collateral in this run (D-30)"
                ),
            )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {args}"
        return await self._target.run_command(
            real_cmd, login_shell=False, mutates=f"install apt package {name} on {self._machines.target}"
        )

    async def _settle_drift(self, refused: Sequence[CollateralEffect], *, verb: str, name: str, refusal: str) -> None:
        """Put the drifted transaction's collateral to the user, and return only where they
        let it go ahead (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-ASK-AGAIN`).

        The guard found a protected package no review saw, which is a fact this run's own
        earlier changes created: the article gives the user three answers to it, and telling
        them instead is what this replaces. Keeping the package leaves the change unapplied
        rather than failed, which is the article's own remedy; stopping raises out of the
        reviewer. `refusal` is the last resort for a converger built without the late round
        at all — with nobody to put the question to, refusing is still better than losing the
        package.
        """
        if self._late is None:
            raise ConvergeItemFailed(refusal)
        withdrawn = await self._late.ask_about_drift(subject=name, verb=verb, effects=refused)
        if withdrawn is not None:
            raise ConvergeItemDeclined(withdrawn)

    async def _held_version_refusal(self, name: str, held: str, exc: ConvergeItemFailed) -> str:
        """Why a held package could not be installed at the source's version, naming BOTH
        versions (`PKG-FR-APT-HOLD-VERSION`).

        Naming only the version that was asked for tells the user nothing they can act on:
        the whole finding is that the target offers a DIFFERENT one, and whether the remedy
        is a missing repository or a version the vendor has withdrawn turns on which one it
        offers. Costs one `apt-cache policy` on the refusal path alone — the version is not
        worth a command on the runs that succeed, and apt's own error does not carry it.
        """
        command = policy_command([name])
        result = await self._target.run_command(command, login_shell=False)
        offered = candidate_version(result.stdout, name) if result.success else None
        instead = (
            f"{self._machines.target} offers {offered}" if offered else f"{self._machines.target} offers no other"
        )
        return (
            f"install of {name} refused: {self._machines.source} holds it at {held} and "
            f"{instead}. A held package is installed at {self._machines.source}'s version or not at all, "
            f"because a hold freezes whatever version lands ({exc})"
        )

    async def remove(
        self, diff: ItemDiff, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> CommandResult:
        """Simulate, then apply, one apt remove — the same last line of defence the
        install guard is (D-30). A collateral removal of an auto-installed package proceeds
        and is logged: removing a package legitimately removes the now-orphaned dependencies
        apt pulled in for it. A collateral change to a package `Collateral.protected` covers
        goes through only where it was itself an approved removal this run or was let go ahead
        as collateral; anything else gets the three-way question here, before the command
        (`_settle_drift`).

        Nothing the guard sees at this point is older than the review. A casualty that is
        itself a removal CANDIDATE was asked about in the second round, over the removals
        this run approved (`Collateral.after_answers`) — a go-ahead there is in the approved
        set and never reaches here, and keeping the package withdrew this very removal, so it
        never converges. What survives to this point is a transaction that has DRIFTED since
        that round, which is a fact this run's own earlier changes created and exactly what
        `PKG-FR-ASK-AGAIN` licenses a late question for.
        """
        name = package_name(diff.item_id)
        args = remove_args([name])

        preview = await simulate_apt_transaction(self._target, args, login_shell=False)
        approved = self._collateral.approved_removals(diffs, decisions)
        refused = await self._collateral.unapproved(preview, exempt=approved | {name}, verb="Removing", subject=name)
        if refused:
            effects = ", ".join(effect.phrase for effect in refused)
            await self._settle_drift(
                refused,
                verb="Removing",
                name=name,
                refusal=(
                    f"removal of {name} refused: apt-get --dry-run would also {effects}, "
                    "which was neither an approved removal nor approved as collateral in this run (D-30)"
                ),
            )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {args}"
        return await self._target.run_command(
            real_cmd, login_shell=False, mutates=f"remove apt package {name} from {self._machines.target}"
        )

    async def hold(
        self, diff: ItemDiff, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> CommandResult:
        """Converge one `apt:hold:<name>` membership item (#208, D4/D5): `apt-mark hold`
        for the add direction (INSTALL), `apt-mark unhold` for the remove direction
        (REMOVE). Selection state only — no `apt-get --dry-run` simulation and no transaction
        guard (a hold changes nothing about the installed package set, D4). The command's
        exit code alone decides pass/fail (D-27); a hold on an unknown package that
        `apt-mark` rejects is a normal per-item failure (D6), not a gated abort.

        A hold whose package this run did not put on the target is refused before any command
        (`PKG-FR-APT-HOLD-INERT`). apt-mark cannot be relied on for this: measured on
        `ubuntu:24.04`, `apt-mark hold` exits 100 only for a name apt has never heard of and
        exits 0 for a package that is merely NOT INSTALLED, recording the hold in dpkg's
        selections. So a skipped or failed install would otherwise leave the target holding a
        package it does not have, which then blocks every later attempt to install it.
        """
        name = hold_name(diff.item_id)
        if diff.action == DiffAction.INSTALL:
            blocked = self._hold_refusal(name, diffs, decisions)
            if blocked is not None:
                raise blocked
        verb = "hold" if diff.action == DiffAction.INSTALL else "unhold"
        return await self._target.run_command(
            f"sudo apt-mark {verb} {shlex.quote(name)}", login_shell=False, mutates=f"{verb} apt package {name}"
        )

    def _hold_refusal(
        self, name: str, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> ConvergeItemDeclined | ConvergeItemFailed | None:
        """Why this hold may not be registered, or `None` when the package is on the target.

        Judged from the package's OWN item in this run, which is the only thing that can say
        it. No item means the target already has the package and the hold is the whole
        change. An item that installs it must have been approved and must have succeeded; an
        item that only reports the package missing (its origin cannot be reproduced) never
        put it there at all.

        Which exception carries the answer is the whole point of returning one rather than a
        string: a hold pins a package to a version, so a package nobody installed has no
        version to pin, and calling that breakage would report a fault for a deliberate
        answer. Every ground that is the user's own answer therefore declines. A plain skip
        in the review and a collateral answer that kept some other package are ONE case here
        — either way the user declined the install, and the plan-time collateral answer
        reaches this branch as that same skip, `Collateral.resolve` having rewritten the
        decision. `_declined_installs` is the late version of it, arriving after the review
        instead of in it, and it is checked first because the decision map still reads APPLY
        for those. Only an install that was approved and then BROKE fails its hold with it.
        """
        if name in self._declined_installs:
            return ConvergeItemDeclined(
                f"hold on {name} not applied: its install was withdrawn to keep a package on {self._machines.target}"
            )
        package_diff = next(
            (
                candidate
                for candidate in diffs
                if candidate.item_class is ItemClass.APT_PACKAGE
                and candidate.item_id == f"{APT_PACKAGE_ID_PREFIX}{name}"
            ),
            None,
        )
        if package_diff is None:
            return None
        why = f"hold on {name} refused"
        if package_diff.diff_class is DiffClass.REPO_UNAVAILABLE:
            # Nobody declined this one: the source has a package the run cannot deliver at
            # all, which is a finding about the two machines rather than an answer.
            return ConvergeItemFailed(
                f"{why}: {name} is not on {self._machines.target} and this run cannot reproduce the repository "
                "it comes from"
            )
        if package_diff.action is not DiffAction.INSTALL:
            return None
        if decisions.get(package_diff.item_id) != Decision.APPLY:
            return ConvergeItemDeclined(
                f"hold on {name} not applied: its install was not approved, and holding a package "
                f"{self._machines.target} lacks blocks installing it"
            )
        failure = self._install_outcome.get(name, "the install did not run")
        return None if failure is None else ConvergeItemFailed(f"{why}: its install failed ({failure})")
