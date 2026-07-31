"""Shared package-sync pipeline: `PackageSyncJob`'s plan()/review/apply() split (D-15, D-16, D-24).

Every package job (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_installs_sync`) is
independent (D-15): its own config, enable flag, failure isolation and progress. What is
here is what all four genuinely share — the plan/review/apply ORDER, the decision-file
rules, the review grouping and the converge loop. A manager's own item shapes, its own
diff and the facts only it can collect live in that manager's module, not here: a base
class holding one manager's logic makes the other three inherit a surface they never use
and cannot change. D-24 requires each job to present its
own batched review before that job's own first mutating command — the batching is per
manager, never across managers. The split of `plan()` from `apply()` exists to make that
review-before-any-change ordering checkable and testable per job:

- `plan()` is ABSTRACT: each manager captures, queries and diffs its own way, and no diff
  shape is common to all four. What is common lives here as pieces an implementation
  calls — `filter_inert` (`packages/state.py`) on the way in, `_drop_inert_diffs` on the
  way out for identities no input item carries (`apt:hold:`, `snap:hold:`), and
  `_build_review_groups` at the end. Every implementation issues READ commands only.
- `accept_review()` stores this job's plan plus the outcome its own review returned, so
  `apply()` and the apt guards read a consistent pair. It is also where a block-state item
  that rode its software's question takes that software's decision
  (`PKG-FR-BLOCKS-REPLICATE`): the reviewer never saw a row for it, so nothing else would
  give it one, and every consumer downstream — the log line, the machine-specific mark,
  the converge loop — reads the answer from the same place.
- `apply()` converges the `APPLY`-decided diffs, one item at a time, catching and
  collecting per-item failures (D-27) so one bad item never stops the rest. It also
  persists a permanent decision (D-08a) for every `SKIP_ALWAYS`-decided item, on
  whichever machine holds it.
- `execute()` — the `SyncJob` entry point the orchestrator's sequential loop calls — is
  self-contained: it plans, reviews through the injected `JobContext.reviewer`, accepts the
  outcome, then applies. A `plan()` failure propagates naturally out of `execute()` and
  lands in this job's own `JobResult` through the orchestrator's per-job exception handling.
  A non-interactive run whose review held anything to DECIDE raises `JobSkipped` there
  instead of applying nothing and reporting SUCCESS; a review holding nothing to decide —
  empty, or nothing but report-only findings — stays SUCCESS, and either way
  `after_review()` is skipped so nothing is transferred without an answer.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.review import Decision, ReviewEntry, ReviewGroup, ReviewOutcome, asks_for_a_decision
from pcswitcher.jobs.packages.state import DecisionEntry, DecisionFile
from pcswitcher.models import CommandResult, Host, JobSkipped, LogLevel, ProgressUpdate

__all__ = [
    "ConvergeItemDeclined",
    "ConvergeItemFailed",
    "PackageItemFailures",
    "PackagePlan",
    "PackageSyncJob",
]


class ConvergeItemFailed(RuntimeError):
    """Raised by a `converge()` implementation to fail exactly one item without stopping
    the loop (D-27) — e.g. `AptSyncJob.converge`'s apt-transaction guard refusing an item
    whose simulated transaction would remove an unreviewed package.

    Distinct from a converge command simply exiting non-zero (which `apply()` also treats
    as a per-item failure via the returned `CommandResult`): this exception is for a
    converge step that refuses to even attempt the command.
    """


class ConvergeItemDeclined(RuntimeError):
    """Raised by a `converge()` implementation for an item the user withdrew after the
    review, when a question only this run's own earlier changes made answerable came back
    "do not do this" (`PKG-FR-ASK-AGAIN`).

    Neither applied nor failed, which is the whole reason it is not `ConvergeItemFailed`:
    an approved install a mid-apply collateral question leaves unapproved is a change the
    user declined, and `PKG-FR-COLLATERAL-MANUAL` requires exactly that outcome — "leaving
    the changes that cause the loss unapplied rather than failing later". `apply()` logs it
    with its reason and counts it in neither the applied nor the failed total.

    Only `apt_sync` raises it today (`LateCollateral`). A converge step that refuses an item
    on its own authority still raises `ConvergeItemFailed`: nobody declined that one.
    """


class PackageItemFailures(RuntimeError):
    """Raised once, after `apply()`'s per-item loop completes, when 1+ items failed (D-27).

    A named type rather than a bare `RuntimeError` so the orchestrator's per-job except
    chain can distinguish "this job's items failed" (continue running the remaining
    package jobs — their diffs were already approved in the same review) from "this job
    crashed" (abort the whole run, today's existing behavior for every other exception).
    """

    def __init__(self, manager: str, failures: Sequence[tuple[ItemDiff, str]]) -> None:
        self.manager = manager
        self.failures = tuple(failures)
        names = ", ".join(diff.label for diff, _stderr in failures)
        super().__init__(f"{len(failures)} {manager} item(s) failed to converge: {names}")


@dataclass(frozen=True)
class PackagePlan:
    """The read-only product of one job's `plan()`, handed to that job's own review.

    `groups` are pre-built `ReviewGroup`s (one per action, removals in their own group,
    per D-07/D-24) so `execute()` passes them straight to the reviewer without re-deriving
    them.
    """

    manager: str
    diffs: tuple[ItemDiff, ...]
    groups: tuple[ReviewGroup, ...]


# The concrete converge verb for one (item_class, action) pair (D-07, D-24): "apply" is
# never shown to the user, because it is the destructive branch as often as the
# additive one. An apt package REMOVE reads as "remove"; a snap channel CHANGE reads as
# "retrack". Data, not per-job string formatting, is what makes "the review names the
# concrete action" checkable rather than left to each job's own wording.
# `_build_review_groups` falls back to the bare `DiffAction` value for any (item_class,
# action) pair not listed here, so a missing vocabulary entry degrades to a plain verb
# instead of silently dropping the group (the backstop this plan requires: every diff
# class the engine produces gets SOME review presentation).
#
# `APT_SOURCE`/`APT_PIN` are deliberately absent: their only surviving direction is
# removal, and ADR-020 D-07 route that through `AptSyncJob`'s own
# `REPO_REMOVAL_REVIEW_ACTION` groups, which supply their own title and verb before this
# table is ever consulted.
_ACTION_VOCABULARY: dict[tuple[ItemClass, DiffAction], str] = {
    (ItemClass.APT_PACKAGE, DiffAction.INSTALL): "install",
    (ItemClass.APT_PACKAGE, DiffAction.CHANGE): "change",
    (ItemClass.APT_PACKAGE, DiffAction.REMOVE): "remove",
    (ItemClass.APT_PACKAGE, DiffAction.REPORT_ONLY): "report",
    # `/etc/apt/apt.conf.d` is the one non-package class reviewed in all three directions
    # (ADR-020 D-37), so all three need a verb — without them a config file reads
    # "Install/Change/Remove apt packages", which is wrong about both the verb and the
    # thing. Paired with `_ITEM_CLASS_NOUN` below, which fixes the second half.
    (ItemClass.APT_CONFIG, DiffAction.INSTALL): "add",
    (ItemClass.APT_CONFIG, DiffAction.CHANGE): "update",
    (ItemClass.APT_CONFIG, DiffAction.REMOVE): "delete",
    (ItemClass.SNAP_CHANNEL, DiffAction.CHANGE): "retrack",
    # Block-state membership items (#208): the add direction reads "hold"/"mask" and the
    # remove direction "unhold"/"unmask", never "install"/"remove". `_build_review_groups`
    # keys the group title AND every entry's action_label off this table by the group's own
    # item class, so a hold/mask item never displays under an "Install/Remove packages"
    # group even when it shares a `DiffAction` with a package.
    (ItemClass.APT_HOLD, DiffAction.INSTALL): "hold",
    (ItemClass.APT_HOLD, DiffAction.REMOVE): "unhold",
    (ItemClass.SNAP_HOLD, DiffAction.INSTALL): "hold",
    (ItemClass.SNAP_HOLD, DiffAction.REMOVE): "unhold",
    (ItemClass.FLATPAK_MASK, DiffAction.INSTALL): "mask",
    (ItemClass.FLATPAK_MASK, DiffAction.REMOVE): "unmask",
}

# What a group's title calls the things it lists, when they are not packages. A verb alone
# cannot make an apt-config title true: the title reads "<verb> <manager> packages", so
# every `/etc/apt/apt.conf.d` group would still end in "apt packages". Hold and mask items
# are absent on purpose — they ARE about the software, so the manager's own noun is right.
_ITEM_CLASS_NOUN: dict[ItemClass, str] = {
    ItemClass.APT_CONFIG: "apt configuration files",
}

# What a manager calls the software it syncs, where "packages" is not that word. flatpak
# syncs applications (the narrative's own term) and apt and snap sync packages, so only
# flatpak is listed. Keyed on the manager rather than the item class so one entry covers
# every flatpak group — refs and masks alike.
_MANAGER_NOUN: dict[str, str] = {"flatpak": "applications"}

# What a manager calls the place software comes from, for the `ORIGIN_MISMATCH` title.
# Never "vendor" (the user's ruling): apt has repositories, flatpak has remotes.
_MANAGER_ORIGIN_NOUN: dict[str, str] = {"flatpak": "remotes"}

# Fixed emission order for review groups: install before change before remove keeps
# the most common/least-destructive action first; report_only trails since it needs a
# decision but implies no direct converge verb.
_ACTION_ORDER: tuple[DiffAction, ...] = (
    DiffAction.INSTALL,
    DiffAction.CHANGE,
    DiffAction.REMOVE,
    DiffAction.REPORT_ONLY,
)

# What a report group is called, per cause. `{origins}` is the manager's own word for where
# software comes from (`_MANAGER_ORIGIN_NOUN`), so a flatpak group says "remotes" where an
# apt one says "repositories".
_REPORT_TITLES: dict[DiffClass, str] = {
    DiffClass.VERSION_MISMATCH: "Version differences",
    DiffClass.ORIGIN_MISMATCH: "Installed from different {origins}",
    DiffClass.REPO_UNAVAILABLE: "Origins {target} cannot reproduce",
}

# The line under a report group, where the condition has a remedy that is not a decision.
# Only version drift has one today: it is the one reported condition that resolves itself.
_REPORT_NOTES: dict[DiffClass, str] = {
    DiffClass.VERSION_MISMATCH: "These converge on their own: run `{upgrade}` on {target}.",
}

# How the log names a decision (`PKG-FR-LOG-DECISIONS`). The enum's own values are the
# tool's internal words; these are what the user was offered.
_DECISION_WORDS: dict[Decision, str] = {
    Decision.APPLY: "applied",
    Decision.SKIP_ONCE: "skipped this run",
    Decision.SKIP_ALWAYS: "marked as this machine's own",
}

_UPGRADE_COMMANDS: dict[str, str] = {
    "apt": "sudo apt update && sudo apt upgrade",
    "snap": "sudo snap refresh",
    "flatpak": "flatpak update",
}

# The block-state item classes: what the user set to stop software moving — an apt hold, a
# snap refresh hold, a flatpak mask (`PKG-FR-BLOCKS-REPLICATE`).
BLOCK_ITEM_CLASSES: frozenset[ItemClass] = frozenset({ItemClass.APT_HOLD, ItemClass.SNAP_HOLD, ItemClass.FLATPAK_MASK})

# The block kinds that freeze an INSTALLED copy of the software, as against a standing rule
# about what may be installed. One of these is never registered for software the target does
# not end the run with: apt records a hold for a package that is merely absent, where it
# blocks every later install of it (`PKG-FR-APT-HELD-TARGET`). A flatpak mask is the other
# kind — it is a pattern, and masking software the machine does not have is its whole point.
FREEZE_BLOCK_CLASSES: frozenset[ItemClass] = frozenset({ItemClass.APT_HOLD, ItemClass.SNAP_HOLD})

# The actions that carry a converge verb, and so make an item something the user decides.
_DECIDED_ACTIONS: tuple[DiffAction, ...] = (DiffAction.INSTALL, DiffAction.CHANGE, DiffAction.REMOVE)

# What the merged question says about the block riding it (`PKG-FR-BLOCKS-REPLICATE`): the
# software's own row carries a clause naming the block's effect on the target, because the
# block has no row of its own to say it. `{name}` is the block's own name where the block
# does not name its software (a mask names a pattern); the hold clauses need none, since the
# row they join already names the package or snap.
_BLOCK_RIDER_CLAUSE: dict[tuple[ItemClass, DiffAction], str] = {
    (ItemClass.APT_HOLD, DiffAction.INSTALL): "{target} ends up holding it, as {source} does",
    (ItemClass.APT_HOLD, DiffAction.REMOVE): "{target}'s hold on it comes off with it",
    (ItemClass.SNAP_HOLD, DiffAction.INSTALL): "{target} ends up holding its refreshes, as {source} does",
    (ItemClass.SNAP_HOLD, DiffAction.REMOVE): "{target}'s refresh hold on it comes off with it",
    (ItemClass.FLATPAK_MASK, DiffAction.INSTALL): "{target} ends up masking {name}, as {source} does",
    (ItemClass.FLATPAK_MASK, DiffAction.REMOVE): "{target}'s mask on {name} comes off with it",
}

# The clause for a freeze block whose software is leaving the target: nothing is registered,
# because there would be nothing on that machine to freeze.
_FREEZE_BLOCK_DROPPED_CLAUSE = "{target} is left holding nothing for it"


def _with_rider_clauses(detail: str | None, clauses: Sequence[str]) -> str | None:
    """`detail` with each riding block's clause appended, or `detail` unchanged.

    Appended rather than prefixed: the detail says what the change itself does, and the
    block is a consequence of it.
    """
    if not clauses:
        return detail
    return "; ".join([*([detail] if detail else []), *clauses])


class PackageSyncJob(SyncJob):
    """Shared plan()/apply() pipeline every package-manager job subclasses.

    Deliberately carries NO `name` ClassVar: `Orchestrator._resolve_sync_job_class` scans
    a job module's attributes for a `SyncJob` subclass whose `name` matches the module
    name (`getattr(attr, "name", None) == job_name`), so an abstract base without `name`
    is invisible to job discovery even when a concrete subclass imports it into scope.
    """

    manager_id: ClassVar[str]

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        self._accepted_plan: PackagePlan | None = None
        self._accepted_outcome: ReviewOutcome | None = None
        # Both machines' decision files as the last `plan()` call read them, so a subclass
        # that EXTENDS the base plan (only `apt_sync`, with its collateral and repo diffs)
        # can run `_drop_inert_diffs` over its own extra diffs without a second pair of
        # remote reads. Re-assigned on every `plan()`, never cached across calls.
        self._plan_decisions: tuple[Mapping[str, DecisionEntry], Mapping[str, DecisionEntry]] = ({}, {})

    # -- Abstract hooks subclasses implement -------------------------------------------

    @abstractmethod
    async def plan(self) -> PackagePlan:
        """Capture, diff and build this job's review groups. Read-only.

        Every manager owns its own: what a diff even IS differs per ecosystem (apt has
        pins, holds and transaction collateral; snap converges a revision and a channel;
        flatpak carries scope and remote trust; a manual install has a snippet or has
        nothing). There is no diff shape all four share, so there is no base
        implementation to inherit — only the pieces below, which every implementation
        does use: `filter_inert` on the way in, `_drop_inert_diffs` on the way out, and
        `_build_review_groups` at the end.

        Nothing here may mutate either machine: a job plans and reviews before it
        converges, so `plan()` runs before the user has approved anything. An
        implementation MUST load both machines' decision files and filter each side's
        items through its OWN file (D-08), then run the resulting diffs through
        `_drop_inert_diffs` to catch the recorded items no input-side filter can see —
        the block-state membership items (`apt:hold:`, `snap:hold:`) and anything else
        whose identity is derived rather than carried on an input item.
        """
        ...

    @abstractmethod
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Apply one approved diff on the target.

        May raise `ConvergeItemFailed` to refuse the item without even attempting the
        mutating command (e.g. a transaction-safety guard), or `ConvergeItemDeclined` for
        one the user withdrew after the review; otherwise returns the
        `CommandResult` of the converge command, whose `.success` decides pass/fail.
        Called for every APPLY-decided diff whose action is `INSTALL`, `REMOVE` or
        `CHANGE` — `REPORT_ONLY` diffs never reach this hook (see `apply()`).
        """
        ...

    @property
    def machines(self) -> Machines:
        """The two machines' own names, for every string this job puts in front of the user."""
        return Machines(source=self.context.source_hostname, target=self.context.target_hostname)

    @staticmethod
    def _decision_holder_is_source(action: DiffAction) -> bool:
        """D-08a's holder rule: an `INSTALL`/`CHANGE` diff is source-held (the source has
        the item, or the version it should converge to), a `REMOVE` diff target-held.

        One definition shared by the WRITE path (`_record_permanent_skips` picks the
        executor whose file gets the entry) and the READ path (`_drop_inert_diffs` picks
        the file to look the item up in), so the machine a skip-always lands on is by
        construction the machine it is read back from.
        """
        return action in (DiffAction.INSTALL, DiffAction.CHANGE)

    def _drop_inert_diffs(
        self,
        diffs: Sequence[ItemDiff],
        source_decisions: Mapping[str, DecisionEntry],
        target_decisions: Mapping[str, DecisionEntry],
    ) -> tuple[ItemDiff, ...]:
        """Drop every diff whose `item_id` is recorded "skip always" on the machine that
        holds it (D-08/D-08a) — the post-diff counterpart to `filter_inert`.

        Required for any diff whose identity does not exist on an input item and so cannot
        be filtered at the diff-input boundary: the block-state membership items
        (`apt:hold:<name>`, `snap:hold:<name>`) are derived from hold-set membership, and
        apt's repo/collateral diffs are derived from directory digests. Without this pass
        a permanently-declined hold is re-emitted every run — and in the add direction it
        comes back default-checked, so a bulk accept applies the very hold the user
        declined.

        `REPORT_ONLY` diffs pass through untouched: they carry no converge verb, so
        `_record_permanent_skips` never records one and there is no holder to match.
        """
        kept: list[ItemDiff] = []
        for diff in diffs:
            if diff.action not in (DiffAction.INSTALL, DiffAction.CHANGE, DiffAction.REMOVE):
                kept.append(diff)
                continue
            holder = source_decisions if self._decision_holder_is_source(diff.action) else target_decisions
            if diff.item_id not in holder:
                kept.append(diff)
        return tuple(kept)

    def _software_for_block(self, block: ItemDiff, software: Mapping[str, ItemDiff]) -> ItemDiff | None:
        """The item in `software` that `block` applies to, or `None` where it has none here.

        `PKG-FR-BLOCKS-REPLICATE`: a block is its own item EXCEPT where the software it
        applies to is itself an item this run, and this is the hook that decides which case
        a given block is in. `software` holds only the items the user actually decides —
        installs, removals and changes, never a report-only finding — keyed by item id.

        No-op on the base and overridden by each manager, because the pairing is written in
        the manager's own identity strings: `apt:hold:<name>` names one package, a snap hold
        names one snap, and a flatpak mask names software by PATTERN and so has to be matched
        rather than looked up. A manager that does not override this keeps every block a
        separate item, which is the rule the exception is carved out of.
        """
        return None

    def _block_name(self, block: ItemDiff) -> str:
        """What the merged question calls this block, where the clause needs to name it.

        The block's label by default (`pkg-a (hold)`); a manager whose label carries more
        than the name — flatpak's `<pattern> (mask, <scope>)` — overrides it, because the
        clause already says which machine masks it and in what.
        """
        return block.label

    def _blocks_riding_software(self, diffs: Sequence[ItemDiff]) -> dict[str, ItemDiff]:
        """`{block item id: the software item it rides}` for this run's merged questions.

        Recomputed from the diffs wherever it is needed rather than carried on the plan:
        it is a pure function of them, and `apt_sync` rebuilds its `PackagePlan` twice
        between the review and the converge loop.
        """
        software = {
            diff.item_id: diff
            for diff in diffs
            if diff.item_class not in BLOCK_ITEM_CLASSES and diff.action in _DECIDED_ACTIONS
        }
        riders: dict[str, ItemDiff] = {}
        for diff in diffs:
            if diff.item_class not in BLOCK_ITEM_CLASSES or diff.action not in _DECIDED_ACTIONS:
                continue
            owner = self._software_for_block(diff, software)
            if owner is not None:
                riders[diff.item_id] = owner
        return riders

    def _rider_clause(self, block: ItemDiff, owner: ItemDiff) -> str:
        """The sentence the merged question adds to the software's row for `block`."""
        if block.item_class in FREEZE_BLOCK_CLASSES and owner.action is DiffAction.REMOVE:
            template = _FREEZE_BLOCK_DROPPED_CLAUSE
        else:
            template = _BLOCK_RIDER_CLAUSE.get((block.item_class, block.action), "{name} travels with it")
        return template.format(source=self.machines.source, target=self.machines.target, name=self._block_name(block))

    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """One `ReviewGroup` per `(action, item_class)` present in `diffs`, keyed on
        `(manager, action)` for the reviewer's removal-direction test (D-24) so removals
        never share a group with installs. The title's verb and every entry's
        `action_label` come from `_ACTION_VOCABULARY`, keyed by the group's own item
        class — so a block-state membership item (`apt:hold:`, `snap:hold:`,
        `flatpak:mask:`) whose add direction shares the `INSTALL` action with a package
        still reads "Hold/Mask ..." rather than displaying under "Install packages"
        (#208). `_ITEM_CLASS_NOUN` does the same for the title's OBJECT, so the one
        reviewed class that is not a package — `/etc/apt/apt.conf.d` — is not announced as
        one. Grouping by item class as well as action is what keeps that verb correct
        when one action mixes item classes (e.g. apt package INSTALL alongside apt hold
        INSTALL); the group's `action` value stays the raw `DiffAction` so add-direction
        stays default-checked and remove-direction lands in its own unticked group.

        Emission order: `_ACTION_ORDER` (install, change, remove, report) outer, and
        within one action the item classes in first-seen order — which, because
        `diff_apt_packages` emits package diffs before hold diffs, keeps a package group
        ahead of its hold group.

        A block whose software is itself an item this run gets no row of its own
        (`PKG-FR-BLOCKS-REPLICATE`): it is dropped here and its effect is stated as a clause
        on the software's own row, so the two are one question and one answer.
        """
        riders = self._blocks_riding_software(diffs)
        clauses: dict[str, list[str]] = {}
        for block_id, owner in riders.items():
            block = next(diff for diff in diffs if diff.item_id == block_id)
            clauses.setdefault(owner.item_id, []).append(self._rider_clause(block, owner))
        diffs = [diff for diff in diffs if diff.item_id not in riders]

        # A reported condition is keyed by its CAUSE as well (ruled by the user): version
        # drift, an origin the target cannot reproduce and a package installed from two
        # different repositories were one group called "Report apt packages", which named
        # none of them. `DiffClass` is that cause, and report-only is the one action whose
        # members carry different ones.
        by_key: dict[tuple[DiffAction, ItemClass, DiffClass | None], list[ItemDiff]] = {}
        class_order: dict[DiffAction, list[tuple[ItemClass, DiffClass | None]]] = {}
        for diff in diffs:
            cause = diff.diff_class if diff.action is DiffAction.REPORT_ONLY else None
            key = (diff.action, diff.item_class, cause)
            if key not in by_key:
                by_key[key] = []
                class_order.setdefault(diff.action, []).append((diff.item_class, cause))
            by_key[key].append(diff)

        groups: list[ReviewGroup] = []
        for action in _ACTION_ORDER:
            for item_class, cause in class_order.get(action, []):
                entries = by_key[(action, item_class, cause)]
                # REPORT_ONLY has no more-specific per-item-class meaning for any current
                # manager (IN-01): fall back to "report" rather than the raw enum value
                # ("report_only"), which read awkwardly in review text like "Report_only
                # flatpak packages". Every other action still falls back to its own
                # `action.value`, unchanged.
                default_verb = "report" if action == DiffAction.REPORT_ONLY else action.value
                verb = _ACTION_VOCABULARY.get((item_class, action), default_verb)
                default_noun = f"{self.manager_id} {_MANAGER_NOUN.get(self.manager_id, 'packages')}"
                noun = _ITEM_CLASS_NOUN.get(item_class, default_noun)
                title = f"{verb.capitalize()} {noun}"
                note = None
                if cause is not None:
                    cause_title = _REPORT_TITLES.get(cause, "Reported").format(
                        target=self.machines.target,
                        origins=_MANAGER_ORIGIN_NOUN.get(self.manager_id, "repositories"),
                    )
                    title = f"{cause_title} ({noun})"
                    # A manager with no upgrade command of its own gets no note rather than
                    # a sentence with a hole in it.
                    upgrade = _UPGRADE_COMMANDS.get(self.manager_id)
                    template = _REPORT_NOTES.get(cause)
                    if template and upgrade:
                        note = template.format(target=self.machines.target, upgrade=upgrade)
                groups.append(
                    ReviewGroup(
                        manager=self.manager_id,
                        action=action.value,
                        title=title,
                        note=note or None,
                        # `PKG-FR-HARMLESS-DEFAULT`: an `/etc/apt/apt.conf.d` file the target
                        # already holds says how the user's own apt behaves there, so
                        # replacing it is an overwrite of their work and starts at skip-once.
                        # A snap moved to another revision or channel does not: converging
                        # software the user asked for overwrites nothing they authored.
                        overwrites_authored_content=item_class is ItemClass.APT_CONFIG and action is DiffAction.CHANGE,
                        entries=tuple(
                            ReviewEntry(
                                item_id=diff.item_id,
                                label=diff.label,
                                action_label=verb,
                                detail=_with_rider_clauses(diff.detail, clauses.get(diff.item_id, [])),
                            )
                            for diff in entries
                        ),
                    )
                )
        return tuple(groups)

    # -- plan() / accept_review() / apply() / execute() -------------------------------

    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Store this job's plan plus the outcome its own review returned, with each riding
        block carrying the answer its software got (`PKG-FR-BLOCKS-REPLICATE`).

        The block had no row of its own, so the reviewer returned no decision for it; the
        answer to the one question they were asked together is what governs both. Done here
        rather than in `apply()` so every consumer of the accepted outcome sees the same
        decision — the log line the block gets, and the machine-specific mark
        `_record_permanent_skips` writes for it on its own holding machine.

        Deliberately after any subclass has finished rewriting the outcome (`apt_sync`'s
        collateral resolution downgrades an approved install to skip-once), so the block
        follows the decision that actually stands.
        """
        riders = self._blocks_riding_software(plan.diffs)
        if riders:
            decisions = dict(outcome.decisions)
            for block_id, owner in riders.items():
                decisions[block_id] = decisions.get(owner.item_id, Decision.SKIP_ONCE)
            outcome = ReviewOutcome(
                decisions=decisions,
                was_interactive=outcome.was_interactive,
                snippets=outcome.snippets,
                unresolved=outcome.unresolved,
            )
        self._accepted_plan = plan
        self._accepted_outcome = outcome

    async def after_review(self) -> None:
        """Hook: work that must run AFTER this job's review returns but BEFORE any
        mutation on the target (`apply()`). Called only when the review was interactive
        (`execute()`), since everything this seam exists for acts on an answer.

        No-op on the base — the three managers that produce no unreproducible items (apt,
        snap, flatpak) need nothing between review and converge. Only `manual_installs_sync`
        overrides it (D-23): it pushes the freshly reconciled install-snippet registry to
        the target here, so a snippet the user authored during THIS run's review is on the
        target before `apply()` replays it. Keeping the hook on the base leaves `execute()`
        the single source of the plan/review/apply order rather than each manager
        re-deriving it.
        """
        return

    async def apply(self) -> None:
        """Converge every APPLY-decided diff from the accepted plan, one item at a time.

        Per-item detail at `LogLevel.FULL`, one `LogLevel.INFO` summary line per job
        (ADR-010). A per-item failure (`ConvergeItemFailed`, or a converge command that
        exits non-zero) is caught, logged with its stderr as structured context, and
        collected — the loop always completes (D-27) — then `PackageItemFailures` is
        raised once, after the loop, if anything failed OR anything is left unresolved
        (D-21) — even when `total` is zero, since an interactive run whose ONLY diffs
        were unreproducible items never has any INSTALL/CHANGE/REMOVE work to do, and
        must still fail if one of them ended up unresolved.

        `ConvergeItemDeclined` is the third outcome: the item is neither applied nor
        failed, because the user withdrew it after the review (`PKG-FR-ASK-AGAIN`). It is
        named in its own summary line so the run says what happened to it, and it never
        reaches `PackageItemFailures`.

        Dry-run (ADR-014): each intended action is logged at FULL with a `[dry-run] `
        prefix, carrying the diff's own detail, and no converge command is ever issued.

        `REPORT_ONLY` diffs are excluded here regardless of decision: they imply no
        converge verb (D-25's version-mismatch, repo-unavailable, origin-mismatch and
        unreproducible classes are informational), so `converge()` is never called
        for one even if something recorded `APPLY` against it.

        Before converging anything, `_record_permanent_skips` persists a `DecisionEntry`
        for every `SKIP_ALWAYS`-decided item (D-08). The `_finalize_unreproducible` hook
        then persists this run's authored snippets and unreproducible-item skip-always
        decisions (D-20/D-21/D-23); it is a no-op on the base and only
        `manual_installs_sync` implements it (D-18), but the call site stays here so both
        run before any converge, independent of whether this run applies anything else
        (a run with zero installs but one newly-authored snippet still records it). The
        `_unresolved_as_failures` hook (also no-op on the base, overridden only by
        `manual_installs_sync`) supplies the genuinely-undecided items that fail an
        interactive run — which is why `total == 0` can still raise `PackageItemFailures`.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        plan = self._accepted_plan
        outcome = self._accepted_outcome
        decisions = outcome.decisions

        self._log_decisions(plan, decisions)
        await self._record_permanent_skips(plan, decisions)
        await self._finalize_unreproducible(plan, outcome)

        approved = [
            diff
            for diff in plan.diffs
            if decisions.get(diff.item_id) == Decision.APPLY and diff.action != DiffAction.REPORT_ONLY
        ]
        # A freeze block riding software that LEAVES the target is approved and still not
        # applied (`PKG-FR-BLOCKS-REPLICATE`): the answer approved the removal, and a hold
        # registered for a package the machine no longer has freezes nothing while blocking
        # every later install of it. Not a failure — the user's own answer is what withdrew
        # it — so it lands with the declined items rather than the failed ones.
        prefix = "[dry-run] " if self.context.dry_run else ""
        riders = self._blocks_riding_software(plan.diffs)
        failures: list[tuple[ItemDiff, str]] = []
        declined: list[tuple[ItemDiff, str]] = []
        withdrawn: list[tuple[ItemDiff, str]] = []
        apply_diffs: list[ItemDiff] = []
        for diff in approved:
            owner = riders.get(diff.item_id)
            if (
                owner is not None
                and diff.item_class in FREEZE_BLOCK_CLASSES
                and diff.action is DiffAction.INSTALL
                and owner.action is DiffAction.REMOVE
            ):
                reason = f"{owner.label} is being removed from {self.machines.target}, so there is nothing to hold"
                withdrawn.append((diff, reason))
                self._log(Host.TARGET, LogLevel.FULL, f"{prefix}{diff.label} not applied: {reason}")
                continue
            apply_diffs.append(diff)

        total = len(apply_diffs)

        if total == 0:
            self._log(Host.TARGET, LogLevel.INFO, f"{prefix}No {self.manager_id} changes to apply")
            self._report_progress(ProgressUpdate(percent=100))
        else:
            self._log(Host.TARGET, LogLevel.INFO, f"{prefix}Applying {total} {self.manager_id} change(s)")

            for index, diff in enumerate(apply_diffs):
                if self.context.dry_run:
                    # The detail belongs on the line, not only in the review panel: a
                    # dry run never renders that panel, and ADR-014 makes the preview
                    # the whole report. Without it the preview says strictly less about
                    # an item than the interactive review does.
                    detail = f" — {diff.detail}" if diff.detail else ""
                    self._log(Host.TARGET, LogLevel.FULL, f"{prefix}Would {diff.action.value} {diff.label}{detail}")
                else:
                    await self._converge_one(diff, failures, declined)
                self._report_progress(ProgressUpdate(percent=int((index + 1) / total * 100)))

            succeeded = total - len(failures) - len(declined)
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"{prefix}{succeeded}/{total} {self.manager_id} change(s) applied",
            )

        # Outside the branch above: a run whose only approved change was a block its
        # software's removal withdrew has nothing left to converge, and must still say what
        # became of it.
        not_applied = [*withdrawn, *declined]
        if not_applied:
            summary = "; ".join(f"{diff.label}: {reason}" for diff, reason in not_applied)
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"{len(not_applied)} {self.manager_id} change(s) not applied, by the user's answer: {summary}",
            )

        all_failures = [*failures, *self._unresolved_as_failures(plan, outcome)]
        if all_failures:
            summary = "; ".join(f"{diff.label}: {stderr.strip()}" for diff, stderr in all_failures)
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"{len(all_failures)} {self.manager_id} item(s) failed: {summary}",
            )
            raise PackageItemFailures(self.manager_id, all_failures)

    def _log_decisions(self, plan: PackagePlan, decisions: Mapping[str, Decision]) -> None:
        """One FULL line per item this job presented, naming the decision it received
        (`PKG-FR-LOG-DECISIONS`).

        Every item, not only the ones that were applied: an item the user skipped produces
        no converge line and no report entry, so without this the log has no record that it
        was ever offered. The decision is written in the words the answer used rather than
        the enum's — "skip this run", not `skip_once` — because the log is read by the same
        person who answered.

        Recorded on the machine the answer acts on, which for a removal is the target and
        for an install the source's intent landing on the target; both are the target, so
        one host label is correct for all of them.
        """
        for diff in plan.diffs:
            decision = decisions.get(diff.item_id, Decision.SKIP_ONCE)
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"reviewed {diff.label} ({diff.action.value}): {_DECISION_WORDS[decision]}",
            )

    def _unresolved_as_failures(self, plan: PackagePlan, outcome: ReviewOutcome) -> list[tuple[ItemDiff, str]]:
        """Hook: this job's genuinely-undecided items that fail an interactive run (D-27).

        No-op on the base — it returns an empty list, so the three managers that produce
        no unreproducible items (apt, snap, flatpak) never fail on this basis. Only
        `manual_installs_sync` overrides it (D-18/D-21): an unreproducible item left with
        neither a snippet nor a recorded decision after an interactive review fails the
        job. The D-27 converge-failure contract in `apply()` is unchanged — converge
        failures fail the job regardless of what this hook returns.
        """
        return []

    async def _finalize_unreproducible(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Hook: persist this job's unreproducible-item snippet authoring and skip-always
        decisions (D-20/D-21/D-23).

        No-op on the base — only `manual_installs_sync` produces unreproducible items
        (D-18), and it overrides this hook with the real persistence; the three managers
        that never do inherit this no-op so the base `apply()` stays generic.
        """
        return

    async def _record_permanent_skips(self, plan: PackagePlan, decisions: Mapping[str, Decision]) -> None:
        """Persist a `DecisionEntry` for every `SKIP_ALWAYS`-decided, actionable diff.

        D-08a decides WHICH machine's file gets the entry by which machine HOLDS the
        item: `INSTALL`/`CHANGE` diffs are source-held (the source has the item, or the
        version it should converge to), so they record on `self.source`; `REMOVE` diffs
        are target-held (only the target has the item), so they record on `self.target`
        — through the remote executor, never a local write (ADR-002).

        `REPORT_ONLY` diffs are skipped: they carry no converge verb (version-mismatch,
        repo-unavailable, origin-mismatch and unreproducible are informational only), so
        there is no "holder" for D-08a to record against.

        Two guards, both required before anything is ever written: never for a
        non-interactive outcome (D-26 — nothing is recorded permanently when nothing
        was actually decided by a human), and never during a dry run (ADR-014 — a
        rehearsal must leave no trace).
        """
        if self.context.dry_run or not self._accepted_outcome_was_interactive():
            return

        recorded_at = datetime.now(UTC).isoformat()
        for diff in plan.diffs:
            if decisions.get(diff.item_id) != Decision.SKIP_ALWAYS:
                continue
            if diff.action not in (DiffAction.INSTALL, DiffAction.CHANGE, DiffAction.REMOVE):
                continue

            executor = self.source if self._decision_holder_is_source(diff.action) else self.target
            await DecisionFile(self.manager_id, executor).record(
                DecisionEntry(
                    item_id=diff.item_id,
                    item_class=diff.item_class,
                    label=diff.label,
                    reason=None,
                    recorded_at=recorded_at,
                )
            )

    def _accepted_outcome_was_interactive(self) -> bool:
        assert self._accepted_outcome is not None
        return self._accepted_outcome.was_interactive

    async def _converge_one(
        self, diff: ItemDiff, failures: list[tuple[ItemDiff, str]], declined: list[tuple[ItemDiff, str]]
    ) -> None:
        try:
            result = await self.converge(diff)
        except ConvergeItemDeclined as exc:
            # Not a failure and not an application: the user answered a question this run's
            # own earlier changes made answerable, and the answer withdrew the item
            # (`PKG-FR-ASK-AGAIN`). Per-item detail at FULL like every other converge line,
            # never ERROR; the INFO summary after the loop names it and its reason.
            declined.append((diff, str(exc)))
            self._log(Host.TARGET, LogLevel.FULL, f"{diff.label} not applied: {exc}")
            return
        except ConvergeItemFailed as exc:
            failures.append((diff, str(exc)))
            self._log(Host.TARGET, LogLevel.ERROR, f"{diff.label} failed: {exc}", stderr=str(exc))
            return

        if result.success:
            self._log(Host.TARGET, LogLevel.FULL, f"{diff.action.value} {diff.label}")
        else:
            failures.append((diff, result.stderr))
            self._log(
                Host.TARGET,
                LogLevel.ERROR,
                f"{diff.label} failed: {result.stderr.strip()}",
                stderr=result.stderr,
            )

    async def execute(self) -> None:
        """The `SyncJob` entry point the orchestrator's sequential job loop calls.

        Self-contained (D-24): plan this job's diffs, review its own groups through the
        injected `JobContext.reviewer`, accept the outcome, run the `after_review()` hook
        (the seam where `manual_installs_sync` pushes its snippet registry, D-23), then
        apply. No component outside the job owns its review, and no fallback applies diffs
        that never came back from one — a missing reviewer fails loudly here rather than
        silently skipping the review and converging unreviewed diffs (T-02-38).

        A `plan()` failure propagates unchanged, so the orchestrator's per-job exception
        handling attributes it to this job's own `JobResult`.

        A non-interactive run whose review held something to DECIDE raises `JobSkipped`:
        D-26 forces every such item to SKIP_ONCE with nobody present to answer, so
        continuing would converge nothing and report SUCCESS. It is raised before any
        mutating command, as `JobSkipped` requires. What counts is a group that asks
        (`review.asks_for_a_decision`), not a group that prints: a plan of nothing but
        report-only findings — two machines differing only in versions — was never
        answerable in either direction, so nobody's absence changed its outcome and it
        stays SUCCESS, exactly as an empty plan does (`PKG-FR-NO-TERMINAL`).

        `after_review()` runs only when a human answered (`PKG-FR-NO-TERMINAL`: a
        non-interactive run transfers no registry). The two SUCCESS cases above are
        exactly where that matters: `manual_installs_sync`'s hook pushes the SOURCE's
        whole snippet registry, which carries entries from earlier runs, so "this run had
        nothing to decide" is not "this run has nothing to transfer". Gated here rather
        than in the hook so the rule holds for any job that ever needs the seam.
        """
        assert self.context.reviewer is not None, (
            f"{self.manager_id} sync has no reviewer; the orchestrator must inject one "
            "through JobContext.reviewer before execute()."
        )
        plan = await self.plan()
        outcome = await self.context.reviewer.review(plan.groups)
        if any(asks_for_a_decision(group) for group in plan.groups) and not outcome.was_interactive:
            raise JobSkipped(
                self.name,
                f"non-interactive run left every {self.manager_id} review item undecided",
            )
        self.accept_review(plan, outcome)
        if outcome.was_interactive:
            await self.after_review()
        await self.apply()
