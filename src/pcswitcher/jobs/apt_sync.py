"""`apt_sync`: apt package convergence — install, remove, and the full diff taxonomy
(D-01, D-03, D-04, D-07, D-24, D-25, ADR-020).

Captures the source's `apt-mark showmanual` set with `dpkg-query`-sourced versions
(never `apt list --installed` — its own manpage says the output has no stable scripting
contract), diffs it against the same query on the target into every D-25 class
(`PackageSyncJob._diff_apt_packages`), and converges the approved `INSTALL`/`REMOVE`
items via `apt-get install`/`apt-get remove`.

Every approved item's transaction is simulated with `apt-get -s` before the real
command runs, guarding against apt silently doing more than the review showed
(T-02-32): an install refuses if the simulation would remove anything or install an
already-present package at a lower version (a downgrade); a removal refuses if the
simulation would remove any package beyond the item itself that was not also an
approved removal in this run. `plan()` additionally runs two BATCHED simulations
(the whole install candidate set, the whole removal candidate set — not one
per-package, which would cost more than the sync itself for 150 packages) so the
review shows collateral effects before the user decides, not only when an item is
later converged.

Apt sources/keys/pins/config, and the other two managers (snap, flatpak), are later
Phase 2 plans.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, override

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.package_items import (
    AptPackageItem,
    DiffAction,
    DiffClass,
    HoldPinFact,
    ItemClass,
    ItemDiff,
    compare_deb_versions,
)
from pcswitcher.jobs.package_review import Decision
from pcswitcher.jobs.package_sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, ValidationError

__all__ = ["AptSyncJob", "AptTransactionPreview", "simulate_apt_transaction"]

# `AptPackageItem.item_id` is always this prefix + the package name (package_items.py).
# Parsing the name back out of the id is a legitimate use of a stable identity string,
# not string-matching on manager-specific content.
_APT_PACKAGE_ID_PREFIX = "apt:package:"

# Matches one `apt-get -s` transaction line: `Inst <name> [<old>] (<new> ...)` for an
# install/upgrade (the `[<old>]` bracket only appears when a version is already
# installed), or `Remv <name> [<old>]` for a removal. Parsed by leading verb token and
# named groups rather than fixed column positions — the rest of an apt-get -s line's
# shape varies with the package and its dependency resolution.
_TRANSACTION_LINE_RE = re.compile(
    r"^(?P<verb>Inst|Remv)\s+(?P<name>\S+)"
    r"(?:\s+\[(?P<old_version>[^\]]+)\])?"
    r"(?:\s+\((?P<new_version>[^\s)]+)\)?)?"
)


def _package_name(item_id: str) -> str:
    if not item_id.startswith(_APT_PACKAGE_ID_PREFIX):
        raise ValueError(f"Not an apt package item id: {item_id!r}")
    return item_id.removeprefix(_APT_PACKAGE_ID_PREFIX)


def _lines(output: str) -> list[str]:
    """Non-blank, stripped lines — the shape every `apt-mark`/`find` list command in
    this module produces."""
    return [line.strip() for line in output.splitlines() if line.strip()]


def _packages_with_no_candidate(policy_output: str) -> set[str]:
    """Parse a multi-package `apt-cache policy <name...>` run: names whose `Candidate:`
    line reads `(none)`. Each package's block starts with an unindented `<name>:`
    header line, per `apt-cache policy`'s documented output shape.
    """
    no_candidate: set[str] = set()
    current: str | None = None
    for line in policy_output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            current = line[:-1]
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            if stripped.removeprefix("Candidate:").strip() == "(none)":
                no_candidate.add(current)
            current = None
    return no_candidate


@dataclass(frozen=True)
class AptTransactionPreview:
    """The parsed result of `apt-get -s <args>` — what apt says it WOULD do.

    `apt-get -s` is the only honest answer to "what will this command do": apt resolves
    dependencies and conflicts at run time, so the package the user ticked and the
    transaction apt actually runs are not necessarily the same thing.

    `install_versions` maps a package apt would `Inst` to `(currently_installed_version
    | None, candidate_version)` — the currently-installed version is `None` for a fresh
    install (no `[...]` bracket in the line), present for an upgrade/downgrade. This is
    what the downgrade guard compares via `compare_deb_versions` rather than assuming
    every `Inst` line is a new install.
    """

    installs: tuple[str, ...]
    removals: tuple[str, ...]
    raw: str
    install_versions: Mapping[str, tuple[str | None, str]] = field(default_factory=dict)


async def simulate_apt_transaction(
    executor: RemoteExecutor, apt_args: str, *, login_shell: bool | None = False
) -> AptTransactionPreview:
    """Run `apt-get -s <apt_args>` on `executor` and parse its Inst/Remv action lines.

    No `sudo` is needed: simulation is read-only.
    """
    result = await executor.run_command(f"apt-get -s {apt_args}", login_shell=login_shell)
    installs: list[str] = []
    removals: list[str] = []
    install_versions: dict[str, tuple[str | None, str]] = {}
    for line in result.stdout.splitlines():
        match = _TRANSACTION_LINE_RE.match(line)
        if match is None:
            continue
        verb, name = match.group("verb"), match.group("name")
        if verb == "Inst":
            installs.append(name)
            new_version = match.group("new_version")
            if new_version is not None:
                install_versions[name] = (match.group("old_version"), new_version)
        elif verb == "Remv":
            removals.append(name)
    return AptTransactionPreview(
        installs=tuple(installs), removals=tuple(removals), raw=result.stdout, install_versions=install_versions
    )


class AptSyncJob(PackageSyncJob):
    """Converge apt packages (install missing, remove extra) after the coordinator's
    batched review, guarded by plan-time and apply-time apt transaction simulation.
    """

    name: ClassVar[str] = "apt_sync"
    manager_id: ClassVar[str] = "apt"

    # No configurable properties yet: this slice needs nothing beyond the enable flag in
    # sync_jobs. `enabled_item_classes`-style filtering is premature until more item
    # classes than APT_PACKAGE exist, so the schema is an empty object on purpose rather
    # than inventing keys with no consumer.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    @override
    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        """Manually-installed apt packages on the source, with versions (D-03)."""
        manual = await self.source.run_command("apt-mark showmanual")
        return await self._resolve_versions(manual.stdout, self.source.run_command)

    @override
    async def query_target_items(self) -> Sequence[AptPackageItem]:
        """The target's own manually-installed apt packages, with versions."""
        manual = await self.target.run_command("apt-mark showmanual", login_shell=False)

        async def run(cmd: str) -> CommandResult:
            return await self.target.run_command(cmd, login_shell=False)

        return await self._resolve_versions(manual.stdout, run)

    @staticmethod
    async def _resolve_versions(
        showmanual_output: str, run: Callable[[str], Awaitable[CommandResult]]
    ) -> list[AptPackageItem]:
        """Resolve every name's version with ONE `dpkg-query` call (RESEARCH.md)."""
        names = _lines(showmanual_output)
        if not names:
            return []

        quoted = " ".join(shlex.quote(name) for name in names)
        # dpkg-query, not `apt list --installed`: apt's own manpage warns the latter's
        # output has no stable contract for scripting. The literal \t/\n below are
        # dpkg-query's OWN format-string escapes (interpreted by dpkg-query, not the
        # shell) — hence a plain (non-f) string so Python leaves them as two-char
        # backslash sequences for dpkg-query to expand into real tab/newline.
        versions_result = await run("dpkg-query -W -f='${Package}\\t${Version}\\n' " + quoted)

        versions: dict[str, str] = {}
        for line in versions_result.stdout.splitlines():
            if not line.strip():
                continue
            pkg_name, _, version = line.partition("\t")
            versions[pkg_name] = version

        return [AptPackageItem(name=name, version=versions.get(name, "")) for name in names]

    @override
    async def collect_hold_pin_facts(self) -> Sequence[HoldPinFact]:
        """Hold facts from `apt-mark showhold` on BOTH machines (RESEARCH: "either
        machine" — a hold recorded on either end is worth surfacing), pin facts from
        the target's `/etc/apt/preferences.d/*` `Package:` stanzas.

        Reading only one of the two mechanisms would silently miss half the held
        packages (RESEARCH Pitfall 2): a hold is dpkg selection state, a pin is an apt
        priority preference, and neither implies the other.
        """
        facts: list[HoldPinFact] = []

        source_hold = await self.source.run_command("apt-mark showhold")
        facts.extend(
            HoldPinFact(mechanism="hold", package=name, source_ref="source: apt-mark showhold")
            for name in _lines(source_hold.stdout)
        )

        target_hold = await self.target.run_command("apt-mark showhold", login_shell=False)
        facts.extend(
            HoldPinFact(mechanism="hold", package=name, source_ref="target: apt-mark showhold")
            for name in _lines(target_hold.stdout)
        )

        # `find ... -exec ... {} +` passes every matching file to one awk invocation
        # (not a per-file command); if the directory has no files, -exec never runs,
        # so an empty preferences.d produces empty output rather than a shell error.
        pins = await self.target.run_command(
            "find /etc/apt/preferences.d -maxdepth 1 -type f -exec awk '/^Package:/{print FILENAME \"\\t\" $2}' {} +",
            login_shell=False,
        )
        for line in _lines(pins.stdout):
            filename, _, package = line.partition("\t")
            if package:
                facts.append(HoldPinFact(mechanism="pin", package=package, source_ref=filename))

        return facts

    @override
    async def collect_unavailable_item_ids(self, missing_item_ids: frozenset[str]) -> frozenset[str]:
        """Batched `apt-cache policy` over every missing-on-target name (one call, not
        one per package): a `Candidate: (none)` means the target's repositories have
        nothing to install from (D-25's REPO_UNAVAILABLE, not a proposable INSTALL).
        """
        if not missing_item_ids:
            return frozenset()

        names = sorted(_package_name(item_id) for item_id in missing_item_ids)
        quoted = " ".join(shlex.quote(name) for name in names)
        result = await self.target.run_command(f"apt-cache policy {quoted}", login_shell=False)
        no_candidate = _packages_with_no_candidate(result.stdout)
        return frozenset(f"{_APT_PACKAGE_ID_PREFIX}{name}" for name in names if name in no_candidate)

    @override
    async def plan(self) -> PackagePlan:
        """Extends the base diff (missing/extra/mismatch/held/unavailable) with
        plan-time apt transaction-collateral simulation (D-24, D-25, T-02-32).

        Runs AFTER the base diff and BEFORE review groups are (re)built, so collateral
        effects apt-get -s reveals appear as their own REPORT_ONLY facts in the SAME
        review the user approves from — visible before any decision, not discovered
        only when an item is later converged.
        """
        base_plan = await super().plan()
        collateral_diffs = await self._collect_plan_time_collateral(base_plan.diffs)
        if not collateral_diffs:
            return base_plan

        all_diffs = (*base_plan.diffs, *collateral_diffs)
        groups = self._build_review_groups(all_diffs)
        return PackagePlan(manager=self.manager_id, diffs=all_diffs, groups=groups)

    async def _collect_plan_time_collateral(self, diffs: Sequence[ItemDiff]) -> list[ItemDiff]:
        """Two BATCHED simulations — the whole install candidate set, the whole
        removal candidate set — not one per package: a per-package simulation over a
        150-package manual set would cost more than the sync itself.
        """
        install_names = [_package_name(d.item_id) for d in diffs if d.action == DiffAction.INSTALL]
        remove_names = [_package_name(d.item_id) for d in diffs if d.action == DiffAction.REMOVE]
        reviewed_names = frozenset(install_names) | frozenset(remove_names)

        collateral: list[ItemDiff] = []
        if install_names:
            quoted = " ".join(shlex.quote(name) for name in install_names)
            preview = await simulate_apt_transaction(
                self.target, f"install -y --no-install-recommends {quoted}", login_shell=False
            )
            collateral.extend(await self._collateral_from_preview(preview, reviewed_names))
        if remove_names:
            quoted = " ".join(shlex.quote(name) for name in remove_names)
            preview = await simulate_apt_transaction(self.target, f"remove -y {quoted}", login_shell=False)
            collateral.extend(await self._collateral_from_preview(preview, reviewed_names))
        return collateral

    async def _collateral_from_preview(
        self, preview: AptTransactionPreview, reviewed_names: frozenset[str]
    ) -> list[ItemDiff]:
        """Every package the simulation would remove or downgrade that is not itself
        one of the reviewed candidates — apt's own words for "this will also happen",
        surfaced as a REPORT_ONLY fact rather than silently included in the batch.
        """
        collateral: list[ItemDiff] = [
            _collateral_diff(pkg, "would also be removed") for pkg in preview.removals if pkg not in reviewed_names
        ]
        for pkg, (old_version, new_version) in preview.install_versions.items():
            if pkg in reviewed_names or old_version is None:
                continue
            if await compare_deb_versions(self.target, new_version, old_version) < 0:
                collateral.append(_collateral_diff(pkg, f"would be downgraded from {old_version} to {new_version}"))
        return collateral

    def _approved_removal_names(self) -> frozenset[str]:
        """Package names of every `REMOVE`-action diff this run's decisions approved.

        The removal guard's rule is "removes nothing the user did not approve", not
        "removes nothing else" — removing a package legitimately removes things that
        depend on it, so the guard needs to know the full approved-removal set, not
        just the one item currently converging.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        decisions = self._accepted_outcome.decisions
        return frozenset(
            _package_name(diff.item_id)
            for diff in self._accepted_plan.diffs
            if diff.action == DiffAction.REMOVE and decisions.get(diff.item_id) == Decision.APPLY
        )

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Simulate the exact apt transaction, guard it, then run the real command.

        One package per invocation (D-27) so a single bad package cannot fail the
        whole batch, and so each package's simulation corresponds exactly to the
        command that follows it. The target resolves dependencies and downloads from
        its own repos (D-28) — no source cache is consulted.
        """
        if diff.action == DiffAction.INSTALL:
            return await self._converge_install(diff)
        if diff.action == DiffAction.REMOVE:
            return await self._converge_remove(diff)
        raise ConvergeItemFailed(
            f"AptSyncJob.converge: unsupported action {diff.action.value!r} for {diff.label} "
            "(only 'install' and 'remove' exist for apt packages)"
        )

    async def _converge_install(self, diff: ItemDiff) -> CommandResult:
        name = _package_name(diff.item_id)
        quoted = shlex.quote(name)
        install_args = f"install -y --no-install-recommends {quoted}"

        preview = await simulate_apt_transaction(self.target, install_args, login_shell=False)
        if preview.removals:
            removed = ", ".join(preview.removals)
            # Enforcement of this plan's own prohibition that an install-direction item
            # removes nothing as a side effect (D-24, D-25, T-02-32): without this check
            # the prohibition is a sentence in a document no code verifies.
            raise ConvergeItemFailed(
                f"install of {name} refused: apt-get -s would also remove {removed}, "
                "which was never reviewed (D-24/D-25)"
            )

        for pkg, (old_version, new_version) in preview.install_versions.items():
            if old_version is None:
                continue
            if await compare_deb_versions(self.target, new_version, old_version) < 0:
                raise ConvergeItemFailed(
                    f"install of {name} refused: apt-get -s would install {pkg} at {new_version}, "
                    f"a downgrade from the currently installed {old_version} (D-04, T-02-32)"
                )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {install_args}"
        return await self.target.run_command(real_cmd, login_shell=False)

    async def _converge_remove(self, diff: ItemDiff) -> CommandResult:
        name = _package_name(diff.item_id)
        quoted = shlex.quote(name)
        remove_args = f"remove -y {quoted}"

        preview = await simulate_apt_transaction(self.target, remove_args, login_shell=False)
        approved = self._approved_removal_names()
        unapproved = [pkg for pkg in preview.removals if pkg != name and pkg not in approved]
        if unapproved:
            removed = ", ".join(unapproved)
            # "removes nothing the user did not approve", not "removes nothing else":
            # removing a package legitimately removes things that depend on it.
            raise ConvergeItemFailed(
                f"removal of {name} refused: apt-get -s would also remove {removed}, "
                "which was not itself an approved removal item in this run (D-24/D-25)"
            )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {remove_args}"
        return await self.target.run_command(real_cmd, login_shell=False)

    @override
    async def validate(self) -> list[ValidationError]:
        """apt-mark availability on both ends, sudo on target, dpkg lock free on target.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `folder_sync.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        source_check = await self.source.run_command("apt-mark --version")
        if not source_check.success:
            errors.append(self._validation_error(Host.SOURCE, "apt-mark is not available on source"))

        target_check = await self.target.run_command("apt-mark --version", login_shell=False)
        if not target_check.success:
            errors.append(self._validation_error(Host.TARGET, "apt-mark is not available on target"))

        sudo_check = await self.target.run_command("sudo -n true", login_shell=False)
        if not sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET, "passwordless sudo is not available on target (required for apt-get install)"
                )
            )

        # fuser exits 0 when the file IS held by at least one process, non-zero when
        # free (man fuser EXIT CODES) — read-only probe, no lock is acquired or released.
        lock_check = await self.target.run_command("sudo fuser /var/lib/dpkg/lock-frontend", login_shell=False)
        if lock_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "dpkg frontend lock is held on target (likely unattended-upgrades); "
                    "retry once it finishes (RESEARCH Pitfall 5)",
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): the manual-install set."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["apt packages (manually-installed set)"],
            mechanism="apt-get install/remove per item, after review",
        )


def _collateral_diff(name: str, effect: str) -> ItemDiff:
    """One plan-time collateral fact — apt's own simulation, not a per-item guard."""
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REPORT_ONLY,
        item_id=f"apt:collateral:{name}",
        label=name,
        detail=f"apt's own simulation says this package {effect}",
    )
