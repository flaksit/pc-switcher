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
  `apply()` and the apt guards read a consistent pair.
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
        """
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
                            ReviewEntry(item_id=diff.item_id, label=diff.label, action_label=verb, detail=diff.detail)
                            for diff in entries
                        ),
                    )
                )
        return tuple(groups)

    # -- plan() / accept_review() / apply() / execute() -------------------------------

    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Store this job's plan plus the outcome its own review returned."""
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

        apply_diffs = [
            diff
            for diff in plan.diffs
            if decisions.get(diff.item_id) == Decision.APPLY and diff.action != DiffAction.REPORT_ONLY
        ]
        prefix = "[dry-run] " if self.context.dry_run else ""
        total = len(apply_diffs)

        failures: list[tuple[ItemDiff, str]] = []
        declined: list[tuple[ItemDiff, str]] = []
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
            if declined:
                summary = "; ".join(f"{diff.label}: {reason}" for diff, reason in declined)
                self._log(
                    Host.TARGET,
                    LogLevel.INFO,
                    f"{len(declined)} {self.manager_id} change(s) not applied, by the user's answer: {summary}",
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
