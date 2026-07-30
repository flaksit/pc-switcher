"""What else apt would do (D-30): the packages an approved change would remove or downgrade
without anybody having asked for it.

Split by origin, which is the whole ruling: a collateral package apt installed
automatically is apt resolving its own dependencies and proceeds silently, while one the
user installed by hand on the TARGET is something they chose to have, so it becomes its own
three-way review item — go ahead, keep the package, or stop the sync.

Two batched simulations per run, not one per package: a per-package rehearsal over a
150-package manual set would cost more than the sync itself. Attribution is what costs extra,
and only on a run that actually found manual collateral.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.apt_sync.commands import (
    AptTransactionPreview,
    compare_deb_versions,
    install_args,
    remove_args,
    simulate_apt_transaction,
)
from pcswitcher.jobs.apt_sync.diffing import collateral_diff
from pcswitcher.jobs.apt_sync.items import (
    APT_PACKAGE_ID_PREFIX,
    collateral_name,
    is_collateral_diff,
    package_name,
)
from pcswitcher.jobs.apt_sync.messages import build_trigger_phrase
from pcswitcher.jobs.apt_sync.origins import OriginClassifier
from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.review import Decision, ReviewOutcome


class CollateralEffect(NamedTuple):
    """One package the pending transaction would take out from under the user, and the three
    ways the review has to say so: the verb for the decision column, the phrase that goes
    inside the finding sentence, and the clause the act answer ends with.
    """

    package: str
    act_word: str
    phrase: str
    sentence: str


class Collateral:
    """D-30, plan time and apply time.

    The same `protected` set answers both, which is the point: a package classified manual at
    plan time and a package the apply-time guard refuses to lose must be the same package, or
    the review asked about something other than what the guard enforces.
    """

    def __init__(
        self,
        *,
        target: RemoteExecutor,
        machines: Machines,
        target_manual_set: frozenset[str],
        origins: OriginClassifier,
    ) -> None:
        self._target = target
        self._machines = machines
        # The target's `apt-mark showmanual` set: the single source of the auto-versus-manual
        # split (D-30). A collateral package the simulation would remove or downgrade is
        # manual (the user chose it -> a review item) if it is in this set, auto (apt's own
        # dependency -> proceed silently) if it is not. Consulted at plan time by `classify`
        # and at apply time by the converge guards, which must agree.
        self._target_manual_set = target_manual_set
        self._origins = origins
        # Package names of every manual-collateral item the user let go ahead, computed from
        # the collateral group's decisions. The apply-time guard lets a removal/downgrade of
        # one of these through; every other manual collateral stays refused (D-30 — the last
        # line of defence behind plan-time classification).
        self._approved: frozenset[str] = frozenset()
        # Each collateral item's `item_id` -> the install/remove item_ids whose OWN
        # transaction reproduces it (`for_direction` narrows the batch down to them).
        # Used to translate a `skip` decision on a collateral item into `SKIP_ONCE` on exactly
        # those, so a declined collateral leaves them unapproved rather than failing them at
        # the apply-time guard — and cancels nothing else.
        self._trigger_ids: dict[str, frozenset[str]] = {}

    @property
    def approved(self) -> frozenset[str]:
        return self._approved

    def protected(self) -> frozenset[str]:
        """Packages a collateral removal/downgrade must not silently touch: the TARGET's
        `apt-mark showmanual` set alone (ADR-020 D-40).

        The source's manual set is deliberately NOT unioned in, and the case that gives up
        is knowingly accepted rather than overlooked: a package the user installed by hand
        on the source, which arrives on the target as an automatic dependency, can now be
        removed as collateral without a prompt. If the target's apt installed it
        automatically, the target's apt owns it, and reclaiming it as a user choice on the
        strength of the OTHER machine's bookkeeping is a guess. The narrower set is also
        the set apt itself consults, so "manually installed" means the same thing to
        pc-switcher and to apt on the machine being changed.

        The machine-specific decision list is still not consulted (D-30, accepted
        limitation, unchanged).
        """
        return self._target_manual_set

    async def plan_time(self, diffs: Sequence[ItemDiff]) -> list[ItemDiff]:
        """One BATCHED simulation per direction — the whole install candidate set, the whole
        removal candidate set — not one per package (D-30).

        Each simulation's would-remove/would-downgrade collateral is split by `classify`
        against the target's manual set: auto collateral produces nothing (apt's own
        business, D-30), manual collateral becomes a review item.

        The install rehearsal covers only the candidates the target's apt can resolve TODAY
        (`OriginClassifier.target_resolvable`); see there for what that costs and what still
        covers it.
        """
        # APT_PACKAGE only: a hold item (`apt:hold:`) shares the INSTALL/REMOVE actions
        # but is dpkg selection state, not an apt-get transaction, so it drives no
        # collateral simulation and its id is not a package id (#208).
        pkg = [d for d in diffs if d.item_class == ItemClass.APT_PACKAGE]
        install_diffs = [d for d in pkg if d.action == DiffAction.INSTALL]
        install_names = [package_name(d.item_id) for d in install_diffs]
        remove_names = [package_name(d.item_id) for d in pkg if d.action == DiffAction.REMOVE]
        # Every package this run already asks about, resolvable or not: one of them turning up
        # in another's transaction is a decision the user is taking anyway, not collateral.
        reviewed_names = frozenset(install_names) | frozenset(remove_names)
        rehearsed = [package_name(d.item_id) for d in install_diffs if self._origins.target_resolvable(d.item_id)]

        # A removal candidate is by definition installed on the target, so apt can always
        # resolve it and that set is never narrowed.
        collateral: list[ItemDiff] = []
        if rehearsed:
            collateral.extend(await self.for_direction(rehearsed, reviewed_names, install_args, verb="Installing"))
        if remove_names:
            collateral.extend(await self.for_direction(remove_names, reviewed_names, remove_args, verb="Removing"))
        return collateral

    async def for_direction(
        self,
        candidates: Sequence[str],
        reviewed_names: frozenset[str],
        args_for: Callable[[Sequence[str]], str],
        *,
        verb: str,
    ) -> list[ItemDiff]:
        """One direction's collateral: the batched rehearsal, then — only if it found
        manual collateral — the narrowing that says WHICH candidates cause each item.

        Attribution matters because `skip` cancels the candidates recorded against the item:
        blaming the whole batch would make one collateral question cancel every package in it,
        including packages whose own transaction never touches the collateral. The batch alone
        cannot say — apt reports the transaction, not its causes — so each candidate is
        rehearsed on its own and blamed for the collateral its own transaction reproduces.

        The cost is one extra `apt-get --dry-run` per candidate, paid ONLY by a run whose
        batch found manual collateral, and never by the common clean run. A single candidate
        is its own answer and is not rehearsed twice.

        Collateral that no single candidate reproduces is jointly caused (apt removes what
        depends on `a | b` only once BOTH go) and is attributed to the whole batch — the
        conservative answer, and the only true one.
        """
        preview = await simulate_apt_transaction(self._target, args_for(candidates), login_shell=False)
        found = await self.classify(preview, reviewed_names)
        if not found:
            return []

        triggers = {item.package: frozenset(candidates) for item in found}
        if len(candidates) > 1:
            narrowed: dict[str, set[str]] = {item.package: set() for item in found}
            for candidate in candidates:
                alone = await simulate_apt_transaction(self._target, args_for([candidate]), login_shell=False)
                for item in await self.classify(alone, reviewed_names):
                    if item.package in narrowed:
                        narrowed[item.package].add(candidate)
            triggers = {name: frozenset(blamed) or frozenset(candidates) for name, blamed in narrowed.items()}

        return [
            self._item(
                item,
                build_trigger_phrase(triggers[item.package], candidates),
                verb,
                frozenset(f"{APT_PACKAGE_ID_PREFIX}{trigger}" for trigger in triggers[item.package]),
            )
            for item in found
        ]

    async def classify(self, preview: AptTransactionPreview, reviewed_names: frozenset[str]) -> list[CollateralEffect]:
        """Partition a simulation's would-remove/would-downgrade packages by origin
        (D-30): a package in the TARGET's manual set becomes a manual-collateral review
        item (ADR-020 D-40); one outside it is auto-installed — apt's own dependency — and
        produces nothing, not even a report line the user cannot act on.

        A downgrade is detected exactly as before: an `install_versions` entry with a
        non-`None` old version and `compare_deb_versions(target, new, old) < 0`.

        Returns `CollateralEffect`s rather than `ItemDiff`s because the caller must
        attribute them before any of it can be phrased: only the caller knows which
        candidates to put in front of the effect.
        """
        protected = self.protected()
        collateral: list[CollateralEffect] = []

        for pkg in preview.removals:
            if pkg in reviewed_names or pkg not in protected:
                continue
            collateral.append(CollateralEffect(pkg, "remove", f"remove {pkg}", f"{pkg} is removed as well"))

        for pkg, (old_version, new_version) in preview.install_versions.items():
            if pkg in reviewed_names or old_version is None or pkg not in protected:
                continue
            if await compare_deb_versions(self._target, new_version, old_version) < 0:
                collateral.append(
                    CollateralEffect(
                        pkg,
                        "downgrade",
                        f"downgrade {pkg} from {old_version} to {new_version}",
                        f"{pkg} is downgraded from {old_version} to {new_version} as well",
                    )
                )

        return collateral

    def _item(self, effect: CollateralEffect, trigger: str, verb: str, trigger_ids: frozenset[str]) -> ItemDiff:
        """Build one manual-collateral `ItemDiff` and record the candidates it gates.

        The screen's two answers are phrased HERE because this is the only layer that knows
        what causes the collateral: the act answer reads "install sl on nomad, so fortunes
        is removed as well", and the next item's cause may be a removal instead. `verb` is
        the direction of the change under review ("Installing"/"Removing"), not what happens
        to the collateral package.
        """
        target = self._machines.target
        causing = "install" if verb == "Installing" else "remove"
        diff = collateral_diff(
            effect.package,
            f"{verb} {trigger} on {target} would {effect.phrase}",
            act_word=effect.act_word,
            answer_hints=(
                f"{causing} {trigger} {'on' if causing == 'install' else 'from'} {target}, so {effect.sentence}",
                f"keep {effect.package} on {target}; {trigger} will not be "
                f"{'installed' if causing == 'install' else 'removed'}; will be asked again next sync",
            ),
        )
        self._trigger_ids[diff.item_id] = trigger_ids
        return diff

    def approved_removals(self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]) -> frozenset[str]:
        """Package names of every `REMOVE`-action diff this run's decisions approved.

        The removal guard's rule is "removes nothing the user did not approve", not
        "removes nothing else" — removing a package legitimately removes things that
        depend on it, so the guard needs to know the full approved-removal set, not
        just the one item currently converging.
        """
        return frozenset(
            package_name(diff.item_id)
            for diff in diffs
            if diff.item_class == ItemClass.APT_PACKAGE
            and diff.action == DiffAction.REMOVE
            and decisions.get(diff.item_id) == Decision.APPLY
        )

    def resolve(self, diffs: Sequence[ItemDiff], outcome: ReviewOutcome) -> ReviewOutcome:
        """Translate the manual-collateral group's decisions (D-30) into the guard's
        approved set and the triggering installs' decisions.

        For each collateral item (`apt:collateral:<pkg>`): an `APPLY` (go ahead)
        marks `<pkg>` approved, so the install/remove guards let its removal
        or downgrade through; a `SKIP_ONCE` (skip) is propagated to the packages whose own
        transaction causes that collateral (`for_direction`'s attribution), so each is cleanly
        left unapproved rather than attempted and refused at the guard. Abort never reaches
        here — it raised `SyncAbortedByUser` inside the review.

        Only an `APPLY` is overridden. A trigger the user already declined needs no
        cancelling, and overriding a `SKIP_ALWAYS` would silently discard the "never offer
        again on this machine" mark it carries: `_record_permanent_skips` reads this same
        decisions map, so the mark would never be written and the user would be asked again
        next run having been told otherwise.

        Returns the outcome with any triggering decisions overridden; leaves the
        decisions map untouched when there is no collateral to resolve.
        """
        approved: set[str] = set()
        overrides: dict[str, Decision] = {}
        for diff in diffs:
            if not is_collateral_diff(diff):
                continue
            decision = outcome.decisions.get(diff.item_id)
            if decision == Decision.APPLY:
                approved.add(collateral_name(diff.item_id))
            elif decision == Decision.SKIP_ONCE:
                for trigger_id in self._trigger_ids.get(diff.item_id, frozenset()):
                    if outcome.decisions.get(trigger_id) == Decision.APPLY:
                        overrides[trigger_id] = Decision.SKIP_ONCE

        self._approved = frozenset(approved)
        if not overrides:
            return outcome
        return ReviewOutcome(
            decisions={**outcome.decisions, **overrides},
            was_interactive=outcome.was_interactive,
            snippets=outcome.snippets,
            unresolved=outcome.unresolved,
        )
