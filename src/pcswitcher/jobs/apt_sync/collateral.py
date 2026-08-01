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

A candidate is exempt from its OWN transaction, and the removal batch IS every candidate's
own transaction — so a candidate carried off by another approved removal's cascade is
invisible there. `after_answers` is where it becomes visible: the second review round, which
has the answers the first round could not, re-rehearses the removals this run really APPROVED
and keeps the candidates it did not. Being offered for removal is not consent to be removed,
so only an approved removal exempts a package (`PKG-FR-COLLATERAL-MANUAL`), and everything
else the cascade would take gets the same three-way question the batch would have given it.

Two further rounds belong to `LateCollateral`, both licensed by `PKG-FR-ASK-AGAIN`: an
install whose repository this run writes cannot be simulated while the review is being
built, so its question is asked once `/etc/apt` has converged; and a real transaction that
has drifted onto a protected package since plan time is asked about immediately before it
runs. Neither fact could be established earlier, which is what separates them from a late
refusal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import NamedTuple

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.apt_sync.commands import (
    AptTransactionPreview,
    compare_deb_versions,
    install_args,
    remove_args,
    simulate_apt_transaction,
)
from pcswitcher.jobs.apt_sync.derived import DerivedWrites
from pcswitcher.jobs.apt_sync.diffing import collateral_diff
from pcswitcher.jobs.apt_sync.items import (
    APT_PACKAGE_ID_PREFIX,
    collateral_item_id,
    is_collateral_diff,
    package_name,
)
from pcswitcher.jobs.apt_sync.messages import (
    build_collateral_group_title,
    build_stranded_repository_line,
    build_trigger_phrase,
)
from pcswitcher.jobs.apt_sync.origins import OriginClassifier
from pcswitcher.jobs.apt_sync.reporting import Log
from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    SKIP_NOW_WORD,
    Decision,
    ReviewEntry,
    Reviewer,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.models import Host, LogLevel

# The run's single `apt-get update`, as a bound call rather than the object that owns it
# (`MetadataRefresh.ensure`): the converger imports this module, so this module cannot import
# the converger back.
Refresh = Callable[[], Awaitable[None]]


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
        log: Log | None = None,
    ) -> None:
        self._target = target
        self._machines = machines
        # Package names the TARGET has marked machine-specific (`PKG-FR-COLLATERAL-MARKED`).
        # They are filtered out before any diff is computed, so no review line anywhere else
        # in the run mentions them — which makes the collateral question the only place the
        # user can be told the mark is about to be overrun.
        self._marked = marked
        self._log = log
        # Marks this run's own review recorded (`note_run_marks`), so both the second round's
        # question and the apply-time guard honour a "never offer again" answer given minutes
        # earlier in the same review.
        self._run_marked: frozenset[str] = frozenset()
        # The removal candidates this run's answers did NOT approve (`note_declined`). They
        # keep their protection (`PKG-FR-COLLATERAL-MANUAL`), so they are what the second
        # round asks about — and the ground `_reason` gives for asking.
        self._run_declined: frozenset[str] = frozenset()
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

    def allow(self, item_ids: frozenset[str]) -> None:
        """Add consequences let go ahead by a question asked AFTER `resolve` ran, so the
        apply-time guard honours an answer given minutes into the converge loop
        (`LateCollateral`, `PKG-FR-ASK-AGAIN`).
        """
        self._approved |= item_ids

    def triggers_of(self, item_id: str) -> frozenset[str]:
        """The install/remove item_ids whose own transaction reproduces this collateral —
        what a "keep the package" answer cancels, and nothing else
        (`PKG-FR-COLLATERAL-ATTRIBUTION`).
        """
        return self._trigger_ids.get(item_id, frozenset())

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
        # That exemption hides one question the article requires — a candidate carried off by
        # ANOTHER candidate's approved removal — and hiding it here is what makes it askable
        # at all: only once the answers exist can the run tell an approved removal, which
        # exempts its package, from a declined one, which keeps its protection.
        # `after_answers` puts that question in the second round.
        #
        # The install candidates need no exemption at all: a package this run installs is
        # absent from the target, so it is outside `protected()` and cannot be collateral.
        collateral: list[ItemDiff] = []
        if rehearsed:
            collateral.extend(await self.for_direction(rehearsed, frozenset(), install_args, verb="Installing"))
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
        restrict_to: frozenset[str] | None = None,
    ) -> list[ItemDiff]:
        """One direction's collateral: the batched rehearsal, then — only if it found
        manual collateral — the narrowing that says WHICH candidates cause each item.

        `restrict_to` keeps only the named packages, for a pass that is re-reading a
        transaction an earlier pass already reported on (`after_answers`): everything else it
        finds was found there. It also silences the auto-collateral log, which that earlier
        pass has already written for the same transaction — `PKG-FR-COLLATERAL-AUTO` wants
        each such change named, not named twice.

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
        if restrict_to is None:
            self._log_auto(split.auto, verb, candidates)
        found = split.manual if restrict_to is None else [item for item in split.manual if item.package in restrict_to]
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

    def items_for(self, effects: Sequence[CollateralEffect], verb: str, subject: str) -> list[ItemDiff]:
        """The review items for collateral a REAL transaction revealed, attributed to the one
        candidate whose transaction it is.

        `for_direction`'s apply-time counterpart, and the phrasing step alone: there is
        nothing to simulate — the preview IS the transaction about to run — and nothing to
        narrow, since `subject` is its only cause.
        """
        return [
            self._item(effect, subject, verb, frozenset({f"{APT_PACKAGE_ID_PREFIX}{subject}"})) for effect in effects
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
        about a package a mark alone protects. A mark given in THIS review is its own
        sentence — "nothing else in this review mentions it" is untrue of a package whose own
        removal row is where the mark was just given.
        """
        target = self._machines.target
        manual = f"apt on {target} has {package} marked as manually installed"
        if package in self._run_marked:
            return (
                f"{manual}, and it was marked as {target}'s own earlier in this review — "
                "either ground alone would protect it."
            )
        if package in self._run_declined:
            return (
                f"{manual}, and its own removal was not approved in this review — being offered for "
                "removal is not consent to be removed."
            )
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

    def note_run_marks(self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]) -> frozenset[str]:
        """Record the packages this run's own answers marked machine-specific, and return
        them (`PKG-FR-COLLATERAL-MARKED`: "a mark recorded earlier in the same run MUST
        count").

        Only the removal direction can produce one: a mark on an INSTALL says the package is
        the SOURCE's own and the target does not have it, so there is nothing on the machine
        being changed for a transaction to take.

        Idempotent, and called twice on purpose — once before the second review round is
        built, so the question can name the mark, and again from `resolve`, so the apply-time
        guard reads it whether or not a second round happened.
        """
        self._run_marked = frozenset(
            package_name(diff.item_id)
            for diff in diffs
            if diff.item_class == ItemClass.APT_PACKAGE
            and diff.action == DiffAction.REMOVE
            and decisions.get(diff.item_id) == Decision.SKIP_ALWAYS
        )
        return self._run_marked

    def note_declined(self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]) -> frozenset[str]:
        """Record the removal candidates this run's answers did NOT approve, and return them.

        Every ground the article gives for keeping a protection is one answer short of an
        approval — skipped for this run, marked as the target's own, or left undecided
        because nobody was there — so the set is defined by what is missing rather than by
        enumerating the answers (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-NO-TERMINAL`).

        Idempotent, and called from both rounds that need it: the second round asks about
        exactly this set, and `resolve` records it again so the apply-time guard's question
        can name the same ground on a path where no second round ran.
        """
        self._run_declined = frozenset(
            package_name(diff.item_id)
            for diff in diffs
            if diff.item_class == ItemClass.APT_PACKAGE
            and diff.action == DiffAction.REMOVE
            and decisions.get(diff.item_id) != Decision.APPLY
        )
        return self._run_declined

    async def after_answers(self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]) -> list[ItemDiff]:
        """The collateral questions the first round's own answers bring into being
        (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-COLLATERAL-MARKED`, `PKG-FR-MACHINE-SPECIFIC`).

        At plan time every removal candidate is exempt from the removal batch, because the
        batch IS each of their transactions and no answer exists to tell one apart from
        another (`plan_time`). Once the answers do exist, they divide: a candidate whose
        removal the user APPROVED consented to losing it, and every other candidate — skipped
        for this run, marked as the target's own, or never answered — keeps its protection and
        has to be asked about before another candidate's cascade carries it off. This is where
        it is asked, over the removals this run really approved, against the transaction that
        will really run.

        Costs one `apt-get --dry-run` on a run that approved a removal and left another
        candidate standing, plus one per approved removal when that rehearsal finds something
        to attribute, and no command at all on every other run.
        """
        self.note_run_marks(diffs, decisions)
        kept = self.note_declined(diffs, decisions)
        approved = sorted(self.approved_removals(diffs, decisions))
        if not kept or not approved:
            return []
        return await self.for_direction(approved, frozenset(approved), remove_args, verb="Removing", restrict_to=kept)

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
        # `after_answers` has usually recorded both sets already; this covers the paths that
        # reach `resolve` without a second review round.
        self.note_run_marks(diffs, outcome.decisions)
        self.note_declined(diffs, outcome.decisions)

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


class LateCollateral:
    """Every collateral question that can only be put once the run has begun changing the
    target (`PKG-FR-ASK-AGAIN`, `PKG-FR-COLLATERAL-MANUAL`). Two of them.

    The first is the installs plan time could not simulate, asked together before the first
    of them converges (`ensure_asked`). The second is the transaction that DRIFTED: a real
    `apt-get --dry-run` issued moments before the command itself, reporting a protected
    package the plan-time rehearsal never saw (`ask_about_drift`). Both give the same three
    answers over the same wording, because they are the same question about the same kind of
    fact — one nobody could have been asked earlier.

    An install whose repository this run writes is a name the target's apt has never heard,
    so `OriginClassifier.target_resolvable` keeps it out of the plan-time simulation — apt
    refuses the whole simulated batch on one such name — and nothing can be said there about
    what installing it would cost. Once `/etc/apt` has converged and this run's `apt-get
    update` has run, apt can say, and the three answers the article requires exist from that
    moment. Being told afterwards is not one of them.

    Asked ONCE, over every such install together, before the first of them converges —
    never as each one comes up. That is `PKG-FR-BATCHED` (the questions come one after
    another with no work between them) and `PKG-FR-CONSENT-BEFORE-CHANGE` at the same time:
    no package transaction has happened when the last of them is answered, so the stopping
    answer stops the sync ahead of every transaction it is about.

    Keeping the package leaves the installs that cause it unapplied, which is the article's
    own remedy — "leaving the changes that cause the loss unapplied rather than failing
    later". A withdrawn install is therefore a `ConvergeItemDeclined`: not applied, not
    failed. The apply-time guard behind all of this is untouched and still refuses a real
    transaction that has drifted onto a protected package nobody saw.

    The `/etc/apt` files written for a withdrawn install have already landed by then and
    stay. `_report_stranded` names them instead of rolling them back.
    """

    def __init__(
        self,
        *,
        collateral: Collateral,
        origins: OriginClassifier,
        derived: DerivedWrites,
        machines: Machines,
        manager_id: str,
        reviewer: Reviewer | None,
        refresh: Refresh,
        log: Log | None = None,
    ) -> None:
        self._collateral = collateral
        self._origins = origins
        self._derived = derived
        self._machines = machines
        self._manager_id = manager_id
        self._reviewer = reviewer
        self._refresh = refresh
        self._log = log
        self._asked = False
        # `{install item_id: why it was not applied}` — the installs a kept package cancels,
        # and only those (`PKG-FR-COLLATERAL-ATTRIBUTION`).
        self._declined: dict[str, str] = {}
        # Destinations `_report_stranded` has already named. It reads the whole `_declined`
        # set each time it runs, and a drifted transaction can make it run more than once
        # per run, so without this the first answer's file is named again by the second's.
        self._reported_stranded: set[str] = set()

    def declined(self, item_id: str) -> str | None:
        """Why this approved install is not being run, or `None` when nothing withdrew it."""
        return self._declined.get(item_id)

    async def ensure_asked(self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]) -> None:
        """Put every outstanding collateral question to the user, once per run.

        Idempotent by design: the converger calls it before EVERY install, and the second
        call onwards is a no-op. That is what makes "before the first install command" and
        "all in one sitting" the same moment without the converge loop needing a phase of
        its own.

        Costs one `apt-get --dry-run` (plus attribution, only where collateral is found) on
        a run that installs a package from a repository it writes, and no command at all on
        every other run.
        """
        if self._asked:
            return
        self._asked = True

        names = [
            package_name(diff.item_id)
            for diff in diffs
            if diff.item_class is ItemClass.APT_PACKAGE
            and diff.action is DiffAction.INSTALL
            and decisions.get(diff.item_id) == Decision.APPLY
            and not self._origins.target_resolvable(diff.item_id)
        ]
        if not names:
            return

        # apt can only answer against the repository this run has just written, so the
        # metadata refresh comes first. It is the run's single one either way: a failure
        # here fails this install exactly as it does without a question to ask.
        await self._refresh()
        found = [
            item
            for item in await self._collateral.for_direction(names, frozenset(), install_args, verb="Installing")
            # A consequence the plan-time question already got a go-ahead for is not put
            # twice: the id is the consequence, so that answer covers this cause too. A
            # DECLINED one IS asked again — that answer cancelled the changes it was about,
            # and these are different changes (`PKG-FR-COLLATERAL-ATTRIBUTION`).
            if item.item_id not in self._collateral.approved
        ]
        if found:
            await self._settle(found)

    async def ask_about_drift(self, *, subject: str, verb: str, effects: Sequence[CollateralEffect]) -> str | None:
        """Put the three-way question for collateral the REAL transaction has just revealed,
        and answer whether that transaction may run: `None` to go ahead, otherwise why it was
        withdrawn (`PKG-FR-COLLATERAL-MANUAL`, `PKG-FR-ASK-AGAIN`).

        The fact does not exist until the transaction is simulated, which happens once this
        run's own `/etc/apt` writes and installs have landed — so it could not have been put
        at plan time, and refusing the change instead would tell the user about a loss the
        article says they get to decide about. Asked immediately before the command it is
        about, so the stopping answer still stops the sync ahead of it
        (`PKG-FR-CONSENT-BEFORE-CHANGE`).

        Every effect of the one transaction goes into a single question, which is
        `PKG-FR-BATCHED` as it binds here: the round is this transaction's, and the next
        transaction's drift is a different fact that nothing could have asked about sooner.
        """
        await self._settle(self._collateral.items_for(effects, verb, subject))
        return self._declined.get(f"{APT_PACKAGE_ID_PREFIX}{subject}")

    async def _settle(self, found: Sequence[ItemDiff]) -> None:
        """Ask, then turn the answers into the guard's approvals and the changes withdrawn.

        Stopping needs no branch: `_review_collateral_group` raises `SyncAbortedByUser`,
        which propagates out of `apply()` and through the orchestrator's per-job handler
        untouched, ending the whole run exactly as the plan-time answer does.
        """
        outcome = await self._ask(found)
        self._collateral.allow(
            frozenset(item.item_id for item in found if outcome.decisions.get(item.item_id) == Decision.APPLY)
        )
        approved = self._collateral.approved
        for item in found:
            if self._log is not None:
                # `PKG-FR-LOG-DECISIONS`: this question is not in the plan, so the base
                # `_log_decisions` pass cannot name it. The words are the answer's own.
                word = item.act_word if item.item_id in approved else SKIP_NOW_WORD
                self._log(Host.TARGET, LogLevel.FULL, f"reviewed {item.label} (collateral): {word}")
            if item.item_id in approved:
                continue
            finding = (item.detail or "").split("\n")[0]
            for trigger_id in self._collateral.triggers_of(item.item_id):
                self._declined[trigger_id] = f"{item.label} was kept on {self._machines.target}: {finding}"
        self._report_stranded()

    def _report_stranded(self) -> None:
        """Name every repository this run wrote for an install the answer has just withdrawn
        and that no surviving approved install needs (`PKG-FR-REPO-DERIVED`,
        `PKG-FR-LOG-DECISIONS`).

        The file is left where it is, and saying so is the whole of what the run owes here:
        the answer that withdrew its packages is the user's, and whether the repository goes
        with them is theirs to decide. Rolling it back would undo a write on the strength of
        an answer about a package.

        INFO rather than a warning, because nothing is broken — an `/etc/apt` file nothing
        installs from costs the machine one stanza of its next `apt-get update`, and this
        line is the run's whole account of it. A run whose answer stranded nothing says
        nothing.
        """
        if self._log is None:
            return
        for left in self._derived.stranded(frozenset(self._declined)):
            if left.dest in self._reported_stranded:
                continue
            self._reported_stranded.add(left.dest)
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                build_stranded_repository_line(left.dest, left.uris, left.packages, self._machines),
            )

    async def _ask(self, found: Sequence[ItemDiff]) -> ReviewOutcome:
        """One `COLLATERAL_REVIEW_ACTION` group through the job's own reviewer, so the
        wording, the three answers and the attribution are the ones the plan-time question
        already uses.

        No reviewer is `PKG-FR-NO-TERMINAL`'s case rather than an error: with nobody to ask,
        every item is declined for this run, which an outcome carrying no decision says.
        """
        if self._reviewer is None:
            return ReviewOutcome(decisions={}, was_interactive=False)
        group = ReviewGroup(
            manager=self._manager_id,
            action=COLLATERAL_REVIEW_ACTION,
            title=build_collateral_group_title(self._machines, self._manager_id),
            entries=tuple(
                ReviewEntry(
                    item_id=item.item_id,
                    label=item.label,
                    action_label=item.act_word or "resolve",
                    detail=item.detail,
                    answer_hints=item.answer_hints,
                )
                for item in found
            ),
        )
        return await self._reviewer.review((group,))
