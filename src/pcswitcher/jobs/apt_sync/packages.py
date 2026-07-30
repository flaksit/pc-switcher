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
from pcswitcher.jobs.apt_sync.collateral import Collateral
from pcswitcher.jobs.apt_sync.commands import (
    compare_deb_versions,
    install_args,
    remove_args,
    simulate_apt_transaction,
)
from pcswitcher.jobs.apt_sync.derived import DerivedWrites
from pcswitcher.jobs.apt_sync.items import hold_name, package_name
from pcswitcher.jobs.apt_sync.origins import OriginClassifier
from pcswitcher.jobs.packages.items import DiffAction, ItemDiff
from pcswitcher.jobs.packages.review import Decision
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed
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
        collateral: Collateral,
        derived: DerivedWrites,
        origins: OriginClassifier,
        refresh: MetadataRefresh,
    ) -> None:
        self._target = target
        self._manager_id = manager_id
        self._collateral = collateral
        self._derived = derived
        self._origins = origins
        self._refresh = refresh

    async def install(
        self, diff: ItemDiff, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> CommandResult:
        """Simulate, then apply, one apt install — the last line of defence behind the
        plan-time collateral classification (D-30). Auto-installed collateral (a package
        apt pulls in that is outside the target's `apt-mark showmanual` set) proceeds
        silently — apt resolving its own dependencies. A manually-installed collateral
        removal or downgrade (manual on the TARGET, ADR-020 D-40) is refused unless the user
        let it go ahead in the review; the decision was made at plan time, and this guard only
        verifies the real transaction has not drifted to touch a manual package nobody saw.
        """
        name = package_name(diff.item_id)

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

        args = install_args([name])
        preview = await simulate_apt_transaction(self._target, args, login_shell=False)

        protected = self._collateral.protected()
        approved_collateral = self._collateral.approved
        refused = [pkg for pkg in preview.removals if pkg in protected and pkg not in approved_collateral]
        if refused:
            removed = ", ".join(refused)
            raise ConvergeItemFailed(
                f"install of {name} refused: apt-get --dry-run would remove manually-installed {removed}, "
                "which was not approved as collateral in this run (D-30)"
            )

        for pkg, (old_version, new_version) in preview.install_versions.items():
            if old_version is None or pkg not in protected or pkg in approved_collateral:
                continue
            if await compare_deb_versions(self._target, new_version, old_version) < 0:
                raise ConvergeItemFailed(
                    f"install of {name} refused: apt-get --dry-run would downgrade manually-installed {pkg} "
                    f"from {old_version} to {new_version}, which was not approved as collateral (D-30, D-04)"
                )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {args}"
        return await self._target.run_command(real_cmd, login_shell=False, mutates=f"install apt package {name}")

    async def remove(
        self, diff: ItemDiff, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> CommandResult:
        """Simulate, then apply, one apt remove — the same last line of defence the
        install guard is (D-30). A collateral removal of an auto-installed package (outside
        the target's `apt-mark showmanual` set) proceeds — removing a package legitimately
        removes the now-orphaned dependencies apt pulled in for it. A collateral removal of a
        manually-installed package (manual on the TARGET, ADR-020 D-40) is refused unless it
        was itself an approved removal this run or approved let go ahead as collateral; that
        decision was made at plan time, and this guard only catches a real transaction that
        drifted to touch a manual package nobody reviewed.
        """
        name = package_name(diff.item_id)
        args = remove_args([name])

        preview = await simulate_apt_transaction(self._target, args, login_shell=False)
        approved = self._collateral.approved_removals(diffs, decisions)
        protected = self._collateral.protected()
        approved_collateral = self._collateral.approved
        refused = [
            pkg
            for pkg in preview.removals
            if pkg != name and pkg not in approved and pkg not in approved_collateral and pkg in protected
        ]
        if refused:
            removed = ", ".join(refused)
            raise ConvergeItemFailed(
                f"removal of {name} refused: apt-get --dry-run would also remove manually-installed {removed}, "
                "which was neither an approved removal nor approved as collateral in this run (D-30)"
            )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {args}"
        return await self._target.run_command(real_cmd, login_shell=False, mutates=f"remove apt package {name}")

    async def hold(self, diff: ItemDiff) -> CommandResult:
        """Converge one `apt:hold:<name>` membership item (#208, D4/D5): `apt-mark hold`
        for the add direction (INSTALL), `apt-mark unhold` for the remove direction
        (REMOVE). Selection state only — no `apt-get --dry-run` simulation and no transaction
        guard (a hold changes nothing about the installed package set, D4). The command's
        exit code alone decides pass/fail (D-27); a hold on an absent or unknown package
        that `apt-mark` rejects is a normal per-item failure (D6), not a gated abort.
        """
        name = hold_name(diff.item_id)
        verb = "hold" if diff.action == DiffAction.INSTALL else "unhold"
        return await self._target.run_command(
            f"sudo apt-mark {verb} {shlex.quote(name)}", login_shell=False, mutates=f"{verb} apt package {name}"
        )
