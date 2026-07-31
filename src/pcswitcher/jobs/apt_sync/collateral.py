"""What else apt would do (D-30): the packages an approved change would remove, downgrade or
upgrade without anybody having asked for it.

Split by origin, which is the whole ruling: a collateral package apt installed
automatically is apt resolving its own dependencies and proceeds silently — named in the log
and nowhere else (`PKG-FR-COLLATERAL-AUTO`) — while one the user installed by hand on the
TARGET, or marked as that machine's own, is something they chose to have, so it becomes its
own three-way review item: go ahead, keep the package, or stop the sync.

Two batched simulations per run, not one per package: a per-package simulation over a
150-package manual set would cost more than the sync itself. Attribution is what costs extra,
and only on a run that actually found manual collateral.

Known gap, deliberately left: in the removal batch a candidate is exempt from its own
transaction, so a removal the user skips can still be carried off by ANOTHER approved
removal's cascade. The apply-time guard refuses that transaction and names the package, so
nothing is lost — the user is told rather than asked. Closing it needs a per-candidate
simulation on every run with removals, which is the cost this module exists to avoid.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
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
    collateral_item_id,
    is_collateral_diff,
    package_name,
)
from pcswitcher.jobs.apt_sync.messages import build_trigger_phrase
from pcswitcher.jobs.apt_sync.origins import OriginClassifier
from pcswitcher.jobs.apt_sync.reporting import Log
from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.review import Decision, ReviewOutcome
from pcswitcher.models import Host, LogLevel


class CollateralEffect(NamedTuple):
    """One package the pending transaction would take out from under the user, and the three
    ways the review has to say so: the verb for the decision column, the phrase that goes
    inside the finding sentence, and the clause the act answer ends with.
    """

    package: str
    act_word: str
    phrase: str
    sentence: str


class CollateralSplit(NamedTuple):
    """One simulation's collateral, split the way D-30 splits it.

    Both halves are returned because both have to be reported: `manual` becomes a review
    item, and `auto` becomes a log line (`PKG-FR-COLLATERAL-AUTO`) — a change nobody is
    asked about still has to be a change somebody can see afterwards.
    """

    manual: list[CollateralEffect]
    auto: list[CollateralEffect]


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
        marked: frozenset[str] = frozenset(),
        stale_holds: frozenset[str] = frozenset(),
        log: Log | None = None,
    ) -> None:
        self._target = target
        self._machines = machines
        # Package names the TARGET has marked machine-specific (`PKG-FR-COLLATERAL-MARKED`).
        # They are filtered out before any diff is computed, so no review line anywhere else
        # in the run mentions them — which makes the collateral question the only place the
        # user can be told the mark is about to be overrun.
        self._marked = marked
        # Names the TARGET holds without having them installed. apt refuses the whole
        # rehearsal batch over one of them, so the install direction asks for the flag that
        # models what the real install does — see `plan_time`.
        self._stale_holds = stale_holds
        self._log = log
        # Marks this run's own review recorded, added by `resolve` so the apply-time guard
        # honours a "never offer again" answer given minutes earlier in the same review.
        self._run_marked: frozenset[str] = frozenset()
        # The target's `apt-mark showmanual` set: the single source of the auto-versus-manual
        # split (D-30). A collateral package the simulation would remove or downgrade is
        # manual (the user chose it -> a review item) if it is in this set, auto (apt's own
        # dependency -> proceed silently) if it is not. Consulted at plan time by `classify`
        # and at apply time by the converge guards, which must agree.
        self._target_manual_set = target_manual_set
        self._origins = origins
        # The item id of every manual-collateral consequence the user let go ahead, computed
        # from the collateral group's decisions — the id and not the package name, because
        # one package can be collateral of two transactions and a go-ahead answers one of
        # them. The apply-time guard lets exactly those consequences through; every other
        # manual collateral stays refused (D-30 — the last line of defence behind plan-time
        # classification).
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
        """Packages a collateral removal, downgrade or upgrade must not silently touch: the
        TARGET's `apt-mark showmanual` set (ADR-020 D-40) plus the packages that machine
        marked machine-specific, this run's own marks included
        (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-COLLATERAL-MARKED`).

        The source's manual set is deliberately NOT unioned in, and the case that gives up
        is knowingly accepted rather than overlooked: a package the user installed by hand
        on the source, which arrives on the target as an automatic dependency, can now be
        removed as collateral without a prompt. If the target's apt installed it
        automatically, the target's apt owns it, and reclaiming it as a user choice on the
        strength of the OTHER machine's bookkeeping is a guess. The narrower set is also
        the set apt itself consults, so "manually installed" means the same thing to
        pc-switcher and to apt on the machine being changed.
        """
        return self._target_manual_set | self._marked | self._run_marked

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
        remove_names = [package_name(d.item_id) for d in pkg if d.action == DiffAction.REMOVE]
        rehearsed = [package_name(d.item_id) for d in install_diffs if self._origins.target_resolvable(d.item_id)]

        # What each direction may exempt from its own simulation, and nothing else
        # (`PKG-FR-COLLATERAL-MANUAL`). Being offered for removal is not consent to be
        # removed, and no answer exists yet at plan time — so the removal candidates are
        # exempt ONLY from the removal batch, where every one of them is in
        # `preview.removals` by construction and is the very thing under review. In the
        # install direction they are ordinary protected packages: an approved install whose
        # transaction takes one out has to be asked about, not silently allowed on the
        # strength of a removal the user may yet skip.
        #
        # The install candidates need no exemption at all: a package this run installs is
        # absent from the target, so it is outside `protected()` and cannot be collateral.
        collateral: list[ItemDiff] = []
        if rehearsed:
            # A candidate the target holds without having freezes nothing, and the real
            # install clears the selection before it runs (`PackageConverger._install`).
            # Without the flag apt refuses the whole batch over that one name and planning
            # ends. What it costs: apt may also report collateral to OTHER held packages,
            # which the real command refuses outright — the rehearsal over-asks rather than
            # under-asks, and never the reverse.
            install = (
                partial(install_args, allow_held=True)
                if any(name in self._stale_holds for name in rehearsed)
                else install_args
            )
            collateral.extend(await self.for_direction(rehearsed, frozenset(), install, verb="Installing"))
        if remove_names:
            # A removal candidate is by definition installed on the target, so apt can
            # always resolve it and that set is never narrowed.
            collateral.extend(
                await self.for_direction(remove_names, frozenset(remove_names), remove_args, verb="Removing")
            )
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
        split = await self.classify(preview, reviewed_names)
        self._log_auto(split.auto, verb, candidates)
        found = split.manual
        if not found:
            return []

        triggers = {item.package: frozenset(candidates) for item in found}
        if len(candidates) > 1:
            narrowed: dict[str, set[str]] = {item.package: set() for item in found}
            for candidate in candidates:
                alone = await simulate_apt_transaction(self._target, args_for([candidate]), login_shell=False)
                for item in (await self.classify(alone, reviewed_names)).manual:
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

    @staticmethod
    def cause_of(verb: str) -> str:
        """The id and approval segment for a direction's verb: `Installing` -> `install`.

        One definition for the plan-time item and the apply-time guard, because an approval
        recorded under one word and looked up under the other would exempt nothing.
        """
        return "install" if verb == "Installing" else "remove"

    async def classify(self, preview: AptTransactionPreview, reviewed_names: frozenset[str]) -> CollateralSplit:
        """Partition a simulation's would-remove, would-downgrade and would-upgrade packages
        by origin (D-30): a package `protected()` covers becomes a manual-collateral review
        item (ADR-020 D-40); one outside it is apt's own dependency and proceeds silently —
        but is still returned, so `PKG-FR-COLLATERAL-AUTO`'s log line can name it.

        A version change is an `install_versions` entry with a non-`None` old version; the
        `compare_deb_versions` sign says which way it goes. Both directions are collateral:
        an upgrade nobody asked for moves a package the user chose off the version it was on,
        which is the same imposition a downgrade is (`PKG-FR-COLLATERAL-MANUAL`).

        Returns `CollateralEffect`s rather than `ItemDiff`s because the caller must
        attribute them before any of it can be phrased: only the caller knows which
        candidates to put in front of the effect.
        """
        protected = self.protected()
        manual: list[CollateralEffect] = []
        auto: list[CollateralEffect] = []

        for pkg in preview.removals:
            if pkg in reviewed_names:
                continue
            effect = CollateralEffect(pkg, "remove", f"remove {pkg}", f"{pkg} is removed as well")
            (manual if pkg in protected else auto).append(effect)

        for pkg, (old_version, new_version) in preview.install_versions.items():
            if pkg in reviewed_names or old_version is None:
                continue
            if pkg not in protected:
                # No `dpkg --compare-versions` for an auto package: the log line names both
                # versions, so the direction is on the page without a command per package.
                # Only a protected package needs the word, because the word goes in a
                # question.
                auto.append(
                    CollateralEffect(
                        pkg,
                        "change",
                        f"change {pkg} from {old_version} to {new_version}",
                        f"{pkg} is changed from {old_version} to {new_version} as well",
                    )
                )
                continue
            order = await compare_deb_versions(self._target, new_version, old_version)
            if order == 0:
                continue
            word = "downgrade" if order < 0 else "upgrade"
            manual.append(
                CollateralEffect(
                    pkg,
                    word,
                    f"{word} {pkg} from {old_version} to {new_version}",
                    f"{pkg} is {word}d from {old_version} to {new_version} as well",
                )
            )

        return CollateralSplit(manual=manual, auto=auto)

    async def unapproved(
        self, preview: AptTransactionPreview, *, exempt: frozenset[str], verb: str, subject: str
    ) -> list[CollateralEffect]:
        """The apply-time half of the same split, and the last line of defence behind
        plan-time classification (D-30): the protected packages this real transaction would
        take that nobody let go ahead.

        Runs the identical `classify` the review ran, so the package the user was asked about
        and the package the guard enforces cannot drift apart. Auto collateral is logged here
        too, because this is the transaction that actually happens
        (`PKG-FR-COLLATERAL-AUTO`).

        Matched on the CONSEQUENCE — this direction, this effect, this package — and not on
        the package alone: a go-ahead given for the install batch's casualty is not consent
        to lose the same package to an approved removal's cascade
        (`PKG-FR-COLLATERAL-MANUAL`).
        """
        split = await self.classify(preview, exempt)
        self._log_auto(split.auto, verb, [subject])
        cause = self.cause_of(verb)
        return [
            effect
            for effect in split.manual
            if collateral_item_id(cause, effect.act_word, effect.package) not in self._approved
        ]

    def _log_auto(self, auto: Sequence[CollateralEffect], verb: str, candidates: Sequence[str]) -> None:
        """One line per collateral change nobody will be asked about
        (`PKG-FR-COLLATERAL-AUTO`).

        Logged from the BATCH simulation only — the per-candidate narrowing re-derives the
        same effects and would print each of them once per candidate.
        """
        if self._log is None:
            return
        trigger = ", ".join(sorted(candidates))
        for effect in auto:
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"{verb} {trigger} on {self._machines.target} would {effect.phrase} "
                f"({effect.package} is installed automatically on {self._machines.target}; not asked)",
            )

    def _reason(self, package: str) -> str:
        """Why this package gets a question when apt's other casualties do not
        (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-COLLATERAL-MARKED`).

        Composed here because `protected()` is a union and only this layer knows which of its
        grounds applies: a fixed "apt has it marked as manually installed" sentence is false
        about a package a mark alone protects. `_run_marked` is deliberately not consulted —
        items are built at plan time, before this run's own marks exist.
        """
        target = self._machines.target
        manual = f"apt on {target} has {package} marked as manually installed"
        if package not in self._marked:
            return (
                f"{manual}: something asked for it there directly, rather than it arriving as "
                "another package's dependency."
            )
        if package in self._target_manual_set:
            return f"{manual}, and it is marked as {target}'s own — either ground alone would protect it."
        return f"{package} is marked as {target}'s own, so nothing else in this review mentions it."

    def _item(self, effect: CollateralEffect, trigger: str, verb: str, trigger_ids: frozenset[str]) -> ItemDiff:
        """Build one manual-collateral `ItemDiff` and record the candidates it gates.

        The screen's two answers are phrased HERE because this is the only layer that knows
        what causes the collateral: the act answer reads "install sl on nomad, so fortunes
        is removed as well", and the next item's cause may be a removal instead. `verb` is
        the direction of the change under review ("Installing"/"Removing"), not what happens
        to the collateral package.

        The detail states the finding first and the reason second: what is about to happen to
        the package is what the answer is about, and why it is protected means nothing before
        the reader knows that.
        """
        target = self._machines.target
        causing = self.cause_of(verb)
        diff = collateral_diff(
            effect.package,
            f"{verb} {trigger} on {target} would {effect.phrase}\n{self._reason(effect.package)}",
            cause=causing,
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

        For each collateral item (`apt:collateral:<cause>:<effect>:<pkg>`): an `APPLY` (go
        ahead) approves that consequence, so the install/remove guard for THAT direction lets
        it through; a `SKIP_ONCE` (skip) is propagated to the packages whose own
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
        # A "never offer again" answer given in THIS review counts from here on
        # (`PKG-FR-COLLATERAL-MARKED`): the apply-time guard runs after `resolve`, so a mark
        # the user made minutes ago protects the package from the transactions that follow.
        self._run_marked = frozenset(
            package_name(diff.item_id)
            for diff in diffs
            if diff.item_class == ItemClass.APT_PACKAGE
            and diff.action == DiffAction.REMOVE
            and outcome.decisions.get(diff.item_id) == Decision.SKIP_ALWAYS
        )

        approved: set[str] = set()
        overrides: dict[str, Decision] = {}
        for diff in diffs:
            if not is_collateral_diff(diff):
                continue
            decision = outcome.decisions.get(diff.item_id)
            if decision == Decision.APPLY:
                approved.add(diff.item_id)
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
