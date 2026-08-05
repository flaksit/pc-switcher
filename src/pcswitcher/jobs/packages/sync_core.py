"""Shared package-sync pipeline: `PackageSyncJob`'s plan()/review/apply() split (D-15, D-16, D-24).

Every package job (`apt_sync`, `snap_sync`, `flatpak_sync`, `manual_deb_sync`,
`manual_installs_sync`) is independent (D-15): its own config, enable flag, failure
isolation and progress. What is here is what they all genuinely share — the
plan/review/apply ORDER, the decision-file rules, the review grouping and the converge
loop. A manager's own item shapes, its own diff and the facts only it can collect live in
that manager's module, not here: a base class holding one manager's logic makes every
other job inherit a surface it never uses and cannot change. D-24 requires each job to present its
own batched review before that job's own first mutating command — the batching is per
manager, never across managers. The split of `plan()` from `apply()` exists to make that
review-before-any-change ordering checkable and testable per job:

- `plan()` is ABSTRACT: each manager captures, queries and diffs its own way, and no diff
  shape is common to all four. What is common lives here as pieces an implementation
  calls — `filter_inert` (`packages/state.py`) on the way in, `_drop_inert_diffs` on the
  way out for identities no input item carries (`apt:hold:`, `snap:hold:`), and
  `_build_review_groups` at the end. Every implementation issues READ commands only.
- `plan_second_round()` is the seam for a question an article scopes to APPROVED work: it
  runs after the first round's answers exist and before anything is written, and returns
  the run's final item set plus the groups of a second review round. No-op on the base;
  only `apt_sync` has such questions today.
- `accept_review()` stores this job's plan plus the outcome its own review returned, so
  `apply()` and the apt guards read a consistent pair. It is also where every block diff
  takes `Decision.APPLY` (`PKG-FR-BLOCKS-DERIVED`): a block is derived, so no review row
  exists for it and the reviewer returns no answer, and every consumer downstream — the
  converge loop, the dry-run preview — reads the same decision from the same place.
  Whether the block actually lands is then its own manager's business: a freeze block
  whose software this run did not put on the target is refused by that manager's converger.
- `apply()` converges the `APPLY`-decided diffs, one item at a time, catching and
  collecting per-item failures (D-27) so one bad item never stops the rest. It also
  persists a permanent decision (D-08a) for every `SKIP_ALWAYS`-decided item, on
  whichever machine holds it, and — after the loop, so this run's own removals count —
  drops the marks whose item has left the machine holding them (`_prune_dead_marks`).
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
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import Decision, ReviewEntry, ReviewGroup, ReviewOutcome, asks_for_a_decision
from pcswitcher.jobs.packages.state import DecisionEntry, DecisionFile
from pcswitcher.models import CommandResult, Host, JobSkipped, LogLevel, ProgressUpdate

__all__ = [
    "SNAP_CHANGE_REVIEW_ACTION",
    "ConvergeItemDeclined",
    "ConvergeItemFailed",
    "PackageItemFailures",
    "PackagePlan",
    "PackageSyncJob",
]

# The `ReviewGroup.action` a snap's revision/channel convergence carries
# (`PKG-FR-NO-MARK-ON-SNAP-REVISION`). It is deliberately not `DiffAction.CHANGE.value`:
# `packages.review` offers the permanent answer only for the actions in its own promotable
# set, and "change" is in it because an `/etc/apt/apt.conf.d` overwrite genuinely is a
# standing per-machine preference. A snap's revision is not one — nobody keeps a revision as
# a preference about one machine, and the mark left the two machines' records disagreeing
# about a snap neither would raise again — so this group asks with two answers.
#
# Not in `review`'s removal set either, so the rows still start applied: converging a
# revision the user asked for overwrites nothing they authored (`PKG-FR-HARMLESS-DEFAULT`).
SNAP_CHANGE_REVIEW_ACTION = "snap_change"


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
    them — the FIRST round's groups, since a question scoped to approved work has no
    answers to be built from yet.

    `plan_second_round()` returns the same shape with the two halves read differently: the
    run's FINAL item set as `diffs` (an item the answers withdrew is gone from it, one the
    answers brought into being is in it), and the SECOND round's groups alone.
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
    # Without this the verb fell through to the bare `DiffAction` value, and the one snap
    # question in a run read "Change snap packages" / "<y> change" — which says nothing
    # about what is changed. What the run does is put the target's copy on the source's
    # revision and channel, and "align" is that.
    (ItemClass.SNAP, DiffAction.CHANGE): "align",
}

# What a group's title calls the things it lists, when the ACTION changes the noun too.
# `_ITEM_CLASS_NOUN` cannot say this: the same class's install and removal groups ARE about
# the packages, and only the change group is about their versions.
_ACTION_CLASS_NOUN: dict[tuple[ItemClass, DiffAction], str] = {
    (ItemClass.SNAP, DiffAction.CHANGE): "snap package versions",
}

# What a group's title calls the things it lists, when they are not packages. A verb alone
# cannot make an apt-config title true: the title reads "<verb> <manager> packages", so
# every `/etc/apt/apt.conf.d` group would still end in "apt packages".
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

# The `ReviewGroup.action` a given (item_class, action) pair asks under, where the raw
# `DiffAction` value would offer the wrong set of answers. One entry today.
_GROUP_ACTION: dict[tuple[ItemClass, DiffAction], str] = {
    (ItemClass.SNAP, DiffAction.CHANGE): SNAP_CHANGE_REVIEW_ACTION,
}

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

# The block item classes: what stops software moving — an apt hold, a snap refresh hold, a
# flatpak mask. All three are DERIVED (`PKG-FR-BLOCKS-DERIVED`): they replicate from the
# source without review, they reach no review group, no decision file may record one, and
# `accept_review` gives every one of them `Decision.APPLY` because nobody was asked.
BLOCK_ITEM_CLASSES: frozenset[ItemClass] = frozenset({ItemClass.APT_HOLD, ItemClass.SNAP_HOLD, ItemClass.FLATPAK_MASK})


def _merge_rounds(first: ReviewOutcome, second: ReviewOutcome) -> ReviewOutcome:
    """One outcome from a job's two review rounds.

    The second round's answers win on a shared id, which never arises today — a question is
    put in exactly one round — but a later id decided twice means the later answer is the
    user's current one. Interactivity is the AND: a round nobody answered leaves its items
    undecided, and `_record_permanent_skips` must not write a mark off that.
    """
    return ReviewOutcome(
        decisions={**first.decisions, **second.decisions},
        was_interactive=first.was_interactive and second.was_interactive,
        snippets={**first.snippets, **second.snippets},
        unresolved=(*first.unresolved, *second.unresolved),
    )


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
        converges, so `plan()` runs before the user has approved anything — including the
        decision files, which `_load_live_decisions` only reads and filters in memory.
        An implementation MUST take both machines' decision files from
        `_load_live_decisions` and filter each side's items through its OWN file (D-08),
        then run the resulting diffs through
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

    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """Hook: which of `entries` name something ONE machine — the one holding this file —
        no longer has. Read-only, and asked of that machine alone.

        A mark keeps the holding machine's own copy of an item (D-08a), so the item is on
        that machine when the mark is written; an answer of "absent" therefore means the
        mark has outlived what it was given to protect and `_prune_dead_marks` takes it out.
        `entries` is that machine's whole file, so an implementation picks out the item
        classes it can actually check and says nothing about the rest — an id it does not
        recognise, or one of a class no answer can produce (`apt:hold:`, `flatpak:remote:`,
        reachable only by a hand edit), stays exactly where it is.

        Two rules bind every implementation, because both failure directions delete a mark
        the user still wants:

        - Answer from a POSITIVE check that the machine does not have the item — dpkg's
          installed set, the manager's own listing, the path itself. Never from the
          inventory the diff was built out of, which is narrower on purpose: apt's manifest
          is `apt-mark showmanual`, and a package that flips to automatically-installed
          leaves that set while staying installed.
        - Say nothing when the check did not answer. `ProbeFailed` is caught for you
          (`_absent_marks`) and prunes nothing; an implementation that degrades on its own
          must degrade to the empty set rather than to "absent".

        No-op on the base, so a manager that has not implemented a presence check keeps
        every mark rather than guessing at one.
        """
        return frozenset()

    async def _absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """`observe_absent_marks` narrowed to ids the file actually holds, and never
        raising: a read that went dark leaves every mark in place.

        A dead read must not fail the job here, unlike everywhere else ADR-022 applies.
        This one answers a bookkeeping question — is a mark still about anything — and at
        the `apply()` call site it is asked AFTER the run's changes have landed, where
        failing would report a run that did its work as a failed one. Nothing downstream
        depends on the answer either: the marks simply stay, which is what they did before.
        """
        if not entries:
            return frozenset()
        try:
            absent = await self.observe_absent_marks(entries, on_source=on_source)
        except ProbeFailed as exc:
            self._log(
                Host.SOURCE if on_source else Host.TARGET,
                LogLevel.FULL,
                f"could not check whether the {self.manager_id} marked items are still here ({exc}); "
                "every mark is left as it is",
            )
            return frozenset()
        return absent & frozenset(entries)

    async def _load_live_decisions_on(self, *, on_source: bool) -> dict[str, DecisionEntry]:
        """One machine's decision file with the entries that machine no longer has anything
        to say about left out. Read-only — the file itself is rewritten by
        `_prune_dead_marks` at `apply()` time, never here.

        Filtering in memory at plan time is what makes a dead mark stop acting in the SAME
        run that notices it: the id is gone from the mapping every `filter_inert` and
        `_drop_inert_diffs` call consults, so the item it named is diffed and reviewed
        normally instead of being silenced by an entry nothing stands behind.
        """
        entries = await DecisionFile(self.manager_id, self.source if on_source else self.target).load()
        absent = await self._absent_marks(entries, on_source=on_source)
        return {item_id: entry for item_id, entry in entries.items() if item_id not in absent}

    async def _load_live_decisions(self) -> tuple[dict[str, DecisionEntry], dict[str, DecisionEntry]]:
        """Both machines' live decisions, stored in `_plan_decisions` for the subclasses
        that diff further after `plan()` returns. What every `plan()` opens with.
        """
        source_decisions = await self._load_live_decisions_on(on_source=True)
        target_decisions = await self._load_live_decisions_on(on_source=False)
        self._plan_decisions = (source_decisions, target_decisions)
        return source_decisions, target_decisions

    async def _prune_dead_marks(self) -> None:
        """Take every mark whose item is gone out of both machines' files, and say so.

        Runs at the END of `apply()`, after the converge loop, which is what makes the
        run's own removals count: an approved apt transaction that takes a marked package
        with it as collateral (`PKG-FR-COLLATERAL-MARKED` — the user is asked, and may say
        yes) leaves a mark about software the machine no longer has, and by the time this
        runs the machine itself says so. The same pass covers every other way an item
        disappears, none of which pc-switcher is involved in: a hand `apt remove`, a
        deleted file, a reinstall.

        Never during a dry run (ADR-014 — a rehearsal leaves no trace). The cost on an
        ordinary run is the two `cat`s that read the files; a file with nothing dead in it
        is not rewritten, and a machine with no marks at all is not asked what it holds.

        One INFO line per dropped mark (`PKG-FR-LOG-DECISIONS`'s reason applies to it: a
        mark is the user's own answer, so it does not evaporate silently). The write itself
        is `mutates=`-gated inside `DecisionFile`, so `--confirm-each-command` shows it like
        every other change.
        """
        if self.context.dry_run:
            return

        for on_source in (True, False):
            decision_file = DecisionFile(self.manager_id, self.source if on_source else self.target)
            entries = await decision_file.load()
            absent = await self._absent_marks(entries, on_source=on_source)
            for item_id in sorted(absent):
                self._log(
                    Host.SOURCE if on_source else Host.TARGET,
                    LogLevel.INFO,
                    f"dropped the machine-specific mark on {entries[item_id].label}: "
                    "the item is no longer on this machine",
                )
            await decision_file.drop(absent)

    @property
    def machines(self) -> Machines:
        """The two machines' own names, for every string this job puts in front of the user."""
        return Machines(source=self.context.source_hostname, target=self.context.target_hostname)

    @staticmethod
    def _mark_holders(action: DiffAction) -> tuple[bool, ...]:
        """The machines a machine-specific mark on a diff of this action can sit on, as
        "is it the source" flags, THE RECORDING MACHINE FIRST (D-08a,
        `PKG-FR-MACHINE-SPECIFIC`).

        One definition serving both halves, and the ordering is what makes them agree by
        construction: the WRITE path (`_record_permanent_skips`) takes the first flag to
        pick the executor whose file gets the entry, and the READ path
        (`_drop_inert_diffs`) looks in every file the tuple names, which always includes
        the one the write used.

        The holding machine is "the one whose state the mark describes", and the action
        states which machines have the item at all:

        - `INSTALL` — only the source has it, so only the source can describe it.
        - `REMOVE` — only the target has it.
        - `CHANGE` — BOTH have it, with different content, and the answer keeps the
          TARGET's copy: what the user refused permanently is the overwrite of the machine
          they are syncing TO, which is the machine the review names in the same words
          ("it is <target>'s own", `review._hints`). But which machine that was depends on
          the direction the run that recorded the mark was launched in, and the direction
          of a later run says nothing about it — so a change is read back from either
          machine's file. Reading only one of them makes the mark hold in the direction it
          was given and evaporate in the other.
        """
        if action is DiffAction.CHANGE:
            return (False, True)
        return (action is DiffAction.INSTALL,)

    def _drop_inert_diffs(
        self,
        diffs: Sequence[ItemDiff],
        source_decisions: Mapping[str, DecisionEntry],
        target_decisions: Mapping[str, DecisionEntry],
    ) -> tuple[ItemDiff, ...]:
        """Drop every diff whose `item_id` is recorded "skip always" on a machine that could
        hold it (`_mark_holders`, D-08/D-08a) — the post-diff counterpart to `filter_inert`.

        Required for any diff whose identity does not exist on an input item and so cannot
        be filtered at the diff-input boundary: the block-state membership items
        (`apt:hold:<name>`, `snap:hold:<name>`) are derived from hold-set membership, and
        apt's repo/collateral diffs are derived from directory digests. Without this pass
        a permanently-declined hold is re-emitted every run — and in the add direction it
        comes back default-checked, so a bulk accept applies the very hold the user
        declined.

        Reading the holder off the ACTION is only correct while the action is a true
        statement of which machines have the item, which is why `filter_inert` drops a
        marked item from BOTH inventories rather than from its own machine's alone: a
        surviving copy on the other machine turns "no item" into a one-sided item pointing
        the wrong way, and this pass would then look the mark up in the wrong file.

        `REPORT_ONLY` diffs pass through untouched: they carry no converge verb, so
        `_record_permanent_skips` never records one and there is no holder to match. So do
        block diffs, in every direction: a block is derived and no answer about one can be
        recorded (`PKG-FR-BLOCKS-DERIVED`), so an entry naming one — left by an older
        version of the tool, or written by hand — must not silence a replication the user
        never declined. A mark on the SOFTWARE still reaches its blocks, in each manager's
        own diff, because that is a mark the user did give.
        """
        kept: list[ItemDiff] = []
        for diff in diffs:
            if diff.item_class in BLOCK_ITEM_CLASSES:
                kept.append(diff)
                continue
            if diff.action not in (DiffAction.INSTALL, DiffAction.CHANGE, DiffAction.REMOVE):
                kept.append(diff)
                continue
            holders = [
                source_decisions if is_source else target_decisions for is_source in self._mark_holders(diff.action)
            ]
            if not any(diff.item_id in holder for holder in holders):
                kept.append(diff)
        return tuple(kept)

    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """One `ReviewGroup` per `(action, item_class)` present in `diffs`, keyed on
        `(manager, action)` for the reviewer's removal-direction test (D-24) so removals
        never share a group with installs. The title's verb and every entry's
        `action_label` come from `_ACTION_VOCABULARY`, keyed by the group's own item class.
        `_ITEM_CLASS_NOUN` does the same for the title's OBJECT, so the one reviewed class
        that is not a package — `/etc/apt/apt.conf.d` — is not announced as one, and
        `_ACTION_CLASS_NOUN` overrides it where one action's group is about something
        narrower than the class itself (a snap CHANGE moves versions, not packages). Grouping by
        item class as well as action is what keeps that verb correct when one action mixes
        item classes; the group's `action` value is normally the raw `DiffAction`, so
        add-direction stays default-checked and remove-direction lands in its own unticked
        group. `_GROUP_ACTION` overrides it where a class must not be offered the permanent
        answer — a snap's revision or channel (`PKG-FR-NO-MARK-ON-SNAP-REVISION`), whose
        group action is a sentinel the reviewer's promotable set does not contain.

        Emission order: `_ACTION_ORDER` (install, change, remove, report) outer, and
        within one action the item classes in first-seen order.

        Blocks are absent from every group, whichever direction they are in
        (`PKG-FR-BLOCKS-DERIVED`): a hold and a mask replicate because the software they
        apply to does, exactly as a pin does, so there is nothing here for the user to
        answer. They stay in `PackagePlan.diffs`, where `accept_review` gives them
        `Decision.APPLY` and `apply()` converges and reports them by name.
        """
        diffs = [diff for diff in diffs if diff.item_class not in BLOCK_ITEM_CLASSES]

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
                noun = _ACTION_CLASS_NOUN.get((item_class, action)) or _ITEM_CLASS_NOUN.get(item_class, default_noun)
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
                        action=_GROUP_ACTION.get((item_class, action), action.value),
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
                                detail=diff.detail,
                            )
                            for diff in entries
                        ),
                    )
                )
        return tuple(groups)

    # -- plan() / accept_review() / apply() / execute() -------------------------------

    async def plan_second_round(self, plan: PackagePlan, outcome: ReviewOutcome) -> PackagePlan:
        """Hook: the questions this job can only put once the first round's answers exist.

        Returns the run's FINAL item set as `diffs` and the SECOND round's groups as
        `groups` — nothing on the base, since only `apt_sync` has such a question today.
        `execute()` puts those groups to the same reviewer immediately after the first round
        returns and merges the two answer sets, so both rounds precede every change this job
        makes (`PKG-FR-REVIEW-FIRST`, `PKG-FR-CONSENT-BEFORE-CHANGE`).

        It exists because an article can scope its question to the work this run APPROVES,
        which is a fact no plan-time computation holds: `PKG-FR-REPO-CONFLICT` raises its
        question "only for a repository this run writes because an approved package comes
        from it", `PKG-FR-REPO-DELETE` counts a repository's users "after this run's approved
        removals" and forbids raising the item at all while any remain, and
        `PKG-FR-COLLATERAL-MARKED` requires a mark recorded earlier in the same run to count.
        Building every item before any answer exists is what made those three questions
        approximations of what their articles ask for.

        `PKG-FR-BATCHED` is what licenses the second round: the questions still come one
        after another with no work between them — an implementation may issue READS here,
        never a change — and each recurring kind of decision is still settled in one pass.
        `PKG-FR-ASK-AGAIN` is a different permission for a different case, asking after the
        target has already been changed, which is `LateCollateral`'s round rather than this
        one.
        """
        return PackagePlan(manager=plan.manager, diffs=plan.diffs, groups=())

    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Store this job's plan plus the outcome its own review returned, with every block
        decided `APPLY` (`PKG-FR-BLOCKS-DERIVED`).

        A block reached no review group, so the reviewer returned no answer for it, and the
        default for a missing answer is skip-once — which would silently drop the very
        replication the article requires without review. It is decided here rather than in
        `apply()` so every consumer of the accepted outcome sees the same thing: the converge
        loop and the dry-run preview alike.

        This is not a decision about whether the block LANDS. A freeze block whose software
        this run did not put on the target is refused by its own manager's converger, which
        is the only place that knows whether the install ran (`PKG-FR-APT-HOLD-INERT`).

        Deliberately after any subclass has finished rewriting the outcome (`apt_sync`'s
        collateral resolution downgrades an approved install to skip-once), so nothing here
        overwrites an answer the user gave about software.
        """
        blocks = [diff.item_id for diff in plan.diffs if diff.item_class in BLOCK_ITEM_CLASSES]
        if blocks:
            outcome = ReviewOutcome(
                decisions={**outcome.decisions, **dict.fromkeys(blocks, Decision.APPLY)},
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
        snap, flatpak) need nothing between review and converge. Only
        `packages.unreproducible.UnreproducibleSyncJob` overrides it (D-23): it pushes the
        freshly reconciled install-snippet registry to the target here, so a snippet the
        user authored during THIS run's review is on the target before `apply()` replays
        it. Keeping the hook on the base leaves `execute()`
        the single source of the plan/review/apply order rather than each manager
        re-deriving it.
        """
        return

    async def apply(self) -> None:
        """Converge every APPLY-decided diff from the accepted plan, one item at a time.

        Per-item detail at `LogLevel.FULL`, one `LogLevel.INFO` summary line per job
        (ADR-010). Progress is reported BEFORE each item rather than after it, and carries
        that item's label: the UI creates a job's bar lazily on its first update, so a job
        applying a single long item used to show no bar at all while it ran (#235), and an
        anonymous bar could not say which item it was waiting on. The completed bar is the
        one report the loop makes after its last item.

        A per-item failure (`ConvergeItemFailed`, or a converge command that
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
        decisions (D-20/D-21/D-23); it is a no-op on the base and only the unreproducible
        jobs implement it (D-18), but the call site stays here so both run before any
        converge, independent of whether this run applies anything else (a run with zero
        installs but one newly-authored snippet still records it). The
        `_unresolved_as_failures` hook (also no-op on the base) supplies the
        genuinely-undecided items that fail an interactive run — which is why `total == 0`
        can still raise `PackageItemFailures`.

        After the loop, `_prune_dead_marks` reconciles both machines' decision files with
        what those machines actually hold. It runs whatever this job applied, `total == 0`
        included: the ways a marked item disappears are mostly nothing to do with a sync.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        plan = self._accepted_plan
        outcome = self._accepted_outcome
        decisions = outcome.decisions

        self._log_decisions(plan, decisions)
        await self._record_permanent_skips(plan, decisions)
        await self._finalize_unreproducible(plan, outcome)

        prefix = "[dry-run] " if self.context.dry_run else ""
        failures: list[tuple[ItemDiff, str]] = []
        declined: list[tuple[ItemDiff, str]] = []
        apply_diffs = [
            diff
            for diff in plan.diffs
            if decisions.get(diff.item_id) == Decision.APPLY and diff.action != DiffAction.REPORT_ONLY
        ]

        total = len(apply_diffs)

        if total == 0:
            self._log(Host.TARGET, LogLevel.INFO, f"{prefix}No {self.manager_id} changes to apply")
            self._report_progress(ProgressUpdate(percent=100))
        else:
            self._log(Host.TARGET, LogLevel.INFO, f"{prefix}Applying {total} {self.manager_id} change(s)")

            for index, diff in enumerate(apply_diffs):
                self._report_progress(ProgressUpdate(percent=int(index / total * 100), item=diff.label))
                if self.context.dry_run:
                    # The detail belongs on the line, not only in the review panel: a
                    # dry run never renders that panel, and ADR-014 makes the preview
                    # the whole report. Without it the preview says strictly less about
                    # an item than the interactive review does.
                    detail = f" — {diff.detail}" if diff.detail else ""
                    self._log(Host.TARGET, LogLevel.FULL, f"{prefix}Would {diff.action.value} {diff.label}{detail}")
                else:
                    await self._converge_one(diff, failures, declined)
            self._report_progress(ProgressUpdate(percent=100))

            succeeded = total - len(failures) - len(declined)
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"{prefix}{succeeded}/{total} {self.manager_id} change(s) applied",
            )

        # Outside the branch above: a run whose every converge declined still has to say so.
        if declined:
            summary = "; ".join(f"{diff.label}: {reason}" for diff, reason in declined)
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"{len(declined)} {self.manager_id} change(s) not applied, by the user's answer: {summary}",
            )

        # After the converge loop, so a mark this run's own changes emptied out is gone by
        # the time the run ends — and before the failure raise, because a mark whose item
        # left the machine is dead whether or not some other item failed to converge.
        await self._prune_dead_marks()

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

        Blocks are excluded: nobody was asked about one (`PKG-FR-BLOCKS-DERIVED`), so a line
        saying it was "reviewed" would name a question the run never put. What a block gets
        instead is `apply()`'s own converge line, which is what every other derived write
        gets (`PKG-FR-DERIVED-VISIBLE`).
        """
        for diff in plan.diffs:
            if diff.item_class in BLOCK_ITEM_CLASSES:
                continue
            decision = decisions.get(diff.item_id, Decision.SKIP_ONCE)
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"reviewed {diff.label} ({diff.action.value}): {_DECISION_WORDS[decision]}",
            )

    def _unresolved_as_failures(self, plan: PackagePlan, outcome: ReviewOutcome) -> list[tuple[ItemDiff, str]]:
        """Hook: this job's genuinely-undecided items that fail an interactive run (D-27).

        No-op on the base — it returns an empty list, so the three managers that produce
        no unreproducible items (apt, snap, flatpak) never fail on this basis. The
        unreproducible jobs do not override it either (D-21 decision 10): an interactive
        review leaves no item genuinely undecided. The D-27 converge-failure contract in
        `apply()` is unchanged — converge failures fail the job regardless of what this
        hook returns.
        """
        return []

    async def _finalize_unreproducible(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Hook: persist this job's unreproducible-item snippet authoring and skip-always
        decisions (D-20/D-21/D-23).

        No-op on the base — only the unreproducible jobs produce such items (D-18), and
        `packages.unreproducible.UnreproducibleSyncJob` overrides this hook with the real
        persistence; the three managers that never do inherit this no-op so the base
        `apply()` stays generic.
        """
        return

    async def _record_permanent_skips(self, plan: PackagePlan, decisions: Mapping[str, Decision]) -> None:
        """Persist a `DecisionEntry` for every `SKIP_ALWAYS`-decided, actionable diff.

        D-08a decides WHICH machine's file gets the entry by which machine HOLDS the item
        (`_mark_holders`, whose first flag is exactly this choice): an `INSTALL` diff is
        source-held, since only the source has the item, so it records on `self.source`;
        `REMOVE` and `CHANGE` diffs are target-held — the target is the only machine that
        has a removal's item, and a change's answer keeps the target's own copy of an item
        both machines have — so they record on `self.target`, through the remote executor,
        never a local write (ADR-002).

        `REPORT_ONLY` diffs are skipped: they carry no converge verb (version-mismatch,
        repo-unavailable, origin-mismatch and unreproducible are informational only), so
        there is no "holder" for D-08a to record against.

        Blocks and a snap's revision change are excluded whatever the decisions say
        (`PKG-FR-BLOCKS-DERIVED`, `PKG-FR-NO-MARK-ON-SNAP-REVISION`): neither is offered the
        permanent answer by any screen, but "no entry can exist for it" is a property of the
        model rather than of one prompt's wiring, and a decision can also arrive from the
        review's automation hook or from a caller assembling a `ReviewOutcome` by hand.

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
            if diff.item_class in BLOCK_ITEM_CLASSES:
                continue
            if diff.item_class is ItemClass.SNAP and diff.action is DiffAction.CHANGE:
                continue
            if diff.action not in (DiffAction.INSTALL, DiffAction.CHANGE, DiffAction.REMOVE):
                continue

            executor = self.source if self._mark_holders(diff.action)[0] else self.target
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
        """Converge one diff and record what became of it (`PKG-FR-LOG-ACTIONS`).

        An applied item's line names the act, the item, the manager and the machine the act
        happened on, because none of those is recoverable from the rest of the log: the
        counts say how many changes landed and not which, the decision line says what was
        answered rather than what was done, and the verbatim command trace is the package
        manager's words rather than the tool's. A reader looking for one package should find
        it by its own name, not by recognising a manager's command line.

        The machine is always the target: every converge in every manager acts there, which
        is why the host on all three of these records is `Host.TARGET`.
        """
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
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"{self.manager_id}: {diff.action.value} {diff.label} on {self.machines.target}",
            )
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
        injected `JobContext.reviewer`, put the second round `plan_second_round()` builds out
        of those answers, accept the merged outcome, run the `after_review()` hook (the seam
        where an unreproducible job pushes its snippet registry, D-23), then apply. No
        component outside the job owns its review, and no fallback applies diffs that never
        came back from one — a missing reviewer fails loudly here rather than silently
        skipping the review and converging unreviewed diffs (T-02-38).

        Both rounds run here rather than inside a job, so the plan/review/apply order has one
        definition: every question precedes every change, and the second round's own groups
        are reviewed through the same injected reviewer as the first's.

        A `plan()` failure propagates unchanged, so the orchestrator's per-job exception
        handling attributes it to this job's own `JobResult`.

        A non-interactive run whose review held something to DECIDE raises `JobSkipped`:
        D-26 forces every such item to SKIP_ONCE with nobody present to answer, so
        continuing would converge nothing and report SUCCESS. It is raised before any
        mutating command, as `JobSkipped` requires, and before the second round is put:
        asking a screen nobody can answer would print the same items twice for nothing.
        What counts is a group that asks (`review.asks_for_a_decision`), in EITHER round,
        not a group that prints: a plan of nothing but report-only findings — two machines
        differing only in versions — was never answerable in either direction, so nobody's
        absence changed its outcome and it stays SUCCESS, exactly as an empty plan does
        (`PKG-FR-NO-TERMINAL`).

        `after_review()` runs only when a human answered (`PKG-FR-NO-TERMINAL`: a
        non-interactive run transfers no registry). The two SUCCESS cases above are
        exactly where that matters: an unreproducible job's hook pushes the SOURCE's
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
        second = await self.plan_second_round(plan, outcome)
        groups = (*plan.groups, *second.groups)
        if any(asks_for_a_decision(group) for group in groups) and not outcome.was_interactive:
            raise JobSkipped(
                self.name,
                f"non-interactive run left every {self.manager_id} review item undecided",
            )
        if second.groups:
            outcome = _merge_rounds(outcome, await self.context.reviewer.review(second.groups))
        plan = PackagePlan(manager=plan.manager, diffs=second.diffs, groups=groups)
        self.accept_review(plan, outcome)
        if outcome.was_interactive:
            await self.after_review()
        await self.apply()
