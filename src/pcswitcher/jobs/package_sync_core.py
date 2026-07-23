"""Shared package-sync pipeline: `PackageSyncJob`'s plan()/review/apply() split (D-15, D-16, D-24).

Every package job (`apt_sync`, `snap_sync`, `flatpak_sync`) is independent (D-15): its own
config, enable flag, failure isolation and progress. D-24 requires each job to present its
own batched review before that job's own first mutating command — the batching is per
manager, never across managers. The split of `plan()` from `apply()` exists to make that
review-before-any-change ordering checkable and testable per job:

- `plan()` captures the source manifest, queries the target, diffs, and builds this job's
  own `ReviewGroup`s. It issues READ commands only — nothing here may mutate either
  machine, because a job plans and reviews before it converges. Before capturing anything,
  it loads both machines' machine-local decision files (D-08, D-09, `package_state.py`) and
  filters an inert item out of the side that holds it, so an item recorded "skip always"
  never becomes an `ItemDiff` in the first place — D-08's "inert on M in both roles" made
  real at the diff-input boundary rather than as a later filter on the review.
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
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.package_items import (
    AptPackageItem,
    DiffAction,
    DiffClass,
    HoldPinFact,
    ItemClass,
    ItemDiff,
    build_held_or_pinned_detail,
    build_repo_unavailable_detail,
    build_version_mismatch_detail,
)
from pcswitcher.jobs.package_review import Decision, ReviewEntry, ReviewGroup, ReviewOutcome
from pcswitcher.jobs.package_state import DecisionEntry, DecisionFile, filter_inert
from pcswitcher.models import CommandResult, Host, LogLevel, ProgressUpdate

__all__ = [
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
# additive one. An apt package REMOVE reads as "remove"; a future apt source REMOVE
# reads as "delete repository"; a future snap channel CHANGE reads as "retrack". Data,
# not per-job string formatting, is what makes "the review names the concrete action"
# checkable rather than left to each job's own wording. Entries beyond APT_PACKAGE are
# illustrative for item classes this plan defines but does not yet diff (SNAP_CHANNEL,
# APT_SOURCE) — `_build_review_groups` falls back to the bare `DiffAction` value for any
# (item_class, action) pair not listed here, so a missing vocabulary entry degrades to a
# plain verb instead of silently dropping the group (the backstop this plan requires:
# every diff class the engine produces gets SOME review presentation).
_ACTION_VOCABULARY: dict[tuple[ItemClass, DiffAction], str] = {
    (ItemClass.APT_PACKAGE, DiffAction.INSTALL): "install",
    (ItemClass.APT_PACKAGE, DiffAction.CHANGE): "change",
    (ItemClass.APT_PACKAGE, DiffAction.REMOVE): "remove",
    (ItemClass.APT_PACKAGE, DiffAction.REPORT_ONLY): "report",
    (ItemClass.APT_SOURCE, DiffAction.REMOVE): "delete repository",
    (ItemClass.SNAP_CHANNEL, DiffAction.CHANGE): "retrack",
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

    # -- Abstract hooks subclasses implement -------------------------------------------

    @abstractmethod
    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        """Read this manager's manifest from the source. Read-only."""
        ...

    @abstractmethod
    async def query_target_items(self) -> Sequence[AptPackageItem]:
        """Read this manager's current state from the target. Read-only."""
        ...

    @abstractmethod
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Apply one approved diff on the target.

        May raise `ConvergeItemFailed` to refuse the item without even attempting the
        mutating command (e.g. a transaction-safety guard); otherwise returns the
        `CommandResult` of the converge command, whose `.success` decides pass/fail.
        Called for every APPLY-decided diff whose action is `INSTALL`, `REMOVE` or
        `CHANGE` — `REPORT_ONLY` diffs never reach this hook (see `apply()`).
        """
        ...

    async def collect_hold_pin_facts(self) -> Sequence[HoldPinFact]:
        """Hold/pin facts (D-25, RESEARCH Pitfall 2) this manager's diff should treat
        as `HELD_OR_PINNED` rather than proposing a normal install/remove/change.

        Default: none. Only apt has a hold-vs-pin concept (`apt-mark showhold` and
        `preferences.d` are two distinct mechanisms); `snap_sync`/`flatpak_sync` need
        not override this.
        """
        return ()

    async def collect_unavailable_item_ids(self, missing_item_ids: frozenset[str]) -> frozenset[str]:
        """Of `missing_item_ids` (items missing on the target), which have no
        installable candidate — `REPO_UNAVAILABLE` rather than `MISSING_ON_TARGET`.

        Default: none. Only managers whose ecosystem can report "no candidate" (apt
        via `apt-cache policy`) need override this.
        """
        return frozenset()

    # -- Shared diff -----------------------------------------------------------------

    def diff_items(
        self,
        source_items: Sequence[AptPackageItem],
        target_items: Sequence[AptPackageItem],
        *,
        hold_pin_facts: Sequence[HoldPinFact] = (),
        unavailable_item_ids: frozenset[str] = frozenset(),
    ) -> tuple[ItemDiff, ...]:
        """Diff source against target into every D-25 `ItemDiff` class.

        Structured as a per-item-class dispatch (`_diff_apt_packages` today) so a
        later plan adds new item classes by adding more private helpers here rather
        than reshaping this method. `hold_pin_facts` and `unavailable_item_ids` come
        from the two hooks above (`collect_hold_pin_facts`/`collect_unavailable_item_ids`)
        so this method stays manager-agnostic — it never shells out itself.
        """
        diffs: list[ItemDiff] = []
        diffs.extend(self._diff_apt_packages(source_items, target_items, hold_pin_facts, unavailable_item_ids))
        return tuple(diffs)

    @staticmethod
    def _diff_apt_packages(
        source_items: Sequence[AptPackageItem],
        target_items: Sequence[AptPackageItem],
        hold_pin_facts: Sequence[HoldPinFact],
        unavailable_item_ids: frozenset[str],
    ) -> list[ItemDiff]:
        """One diff per item id present on either side, source-then-target order.

        Precedence per item id: `HELD_OR_PINNED` (present on the target and named by
        a hold/pin fact) beats every other outcome — a hold/pin is itself the
        review-worthy fact, more informative than the install/remove/change it would
        otherwise imply. Otherwise: missing-on-target -> `REPO_UNAVAILABLE` if apt
        reports no candidate, else `MISSING_ON_TARGET`/`INSTALL`; extra-on-target ->
        `EXTRA_ON_TARGET`/`REMOVE`; present on both with differing versions ->
        `VERSION_MISMATCH`/`REPORT_ONLY` (D-04: reported, never force-downgraded);
        present on both with equal versions -> no diff at all.
        """
        source_by_id = {item.item_id: item for item in source_items}
        target_by_id = {item.item_id: item for item in target_items}

        held_or_pinned: dict[str, HoldPinFact] = {}
        for fact in hold_pin_facts:
            held_or_pinned.setdefault(fact.package, fact)

        seen: dict[str, None] = {}
        for item in (*source_items, *target_items):
            seen.setdefault(item.item_id, None)

        diffs: list[ItemDiff] = []
        for item_id in seen:
            source_item = source_by_id.get(item_id)
            target_item = target_by_id.get(item_id)

            if target_item is not None and target_item.name in held_or_pinned:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_PACKAGE,
                        diff_class=DiffClass.HELD_OR_PINNED,
                        action=DiffAction.REPORT_ONLY,
                        item_id=item_id,
                        label=target_item.label(),
                        detail=build_held_or_pinned_detail(held_or_pinned[target_item.name]),
                    )
                )
            elif source_item is not None and target_item is None:
                if item_id in unavailable_item_ids:
                    diffs.append(
                        ItemDiff(
                            item_class=ItemClass.APT_PACKAGE,
                            diff_class=DiffClass.REPO_UNAVAILABLE,
                            action=DiffAction.REPORT_ONLY,
                            item_id=item_id,
                            label=source_item.label(),
                            detail=build_repo_unavailable_detail(source_item.name),
                        )
                    )
                else:
                    diffs.append(
                        ItemDiff(
                            item_class=ItemClass.APT_PACKAGE,
                            diff_class=DiffClass.MISSING_ON_TARGET,
                            action=DiffAction.INSTALL,
                            item_id=item_id,
                            label=source_item.label(),
                            detail=None,
                        )
                    )
            elif target_item is not None and source_item is None:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_PACKAGE,
                        diff_class=DiffClass.EXTRA_ON_TARGET,
                        action=DiffAction.REMOVE,
                        item_id=item_id,
                        label=target_item.label(),
                        detail=None,
                    )
                )
            elif source_item is not None and target_item is not None and source_item.version != target_item.version:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_PACKAGE,
                        diff_class=DiffClass.VERSION_MISMATCH,
                        action=DiffAction.REPORT_ONLY,
                        item_id=item_id,
                        label=target_item.label(),
                        detail=build_version_mismatch_detail(source_item.version, target_item.version),
                    )
                )
            # else: present on both, equal versions, not held/pinned -> no diff.

        return diffs

    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """One `ReviewGroup` per action present in `diffs`, keyed by `(manager, action)`
        (D-24) so removals never share a group with installs. The title's verb comes
        from `_ACTION_VOCABULARY`, keyed by the group's item class — today every diff
        this job produces shares one item class per action, so the first entry's
        `item_class` is unambiguous; a manager mixing item classes under one action
        would need this revisited.
        """
        by_action: dict[DiffAction, list[ItemDiff]] = {}
        for diff in diffs:
            by_action.setdefault(diff.action, []).append(diff)

        groups: list[ReviewGroup] = []
        for action in _ACTION_ORDER:
            entries = by_action.get(action)
            if not entries:
                continue
            # REPORT_ONLY has no more-specific per-item-class meaning for any current
            # manager (IN-01): fall back to "report" rather than the raw enum value
            # ("report_only"), which read awkwardly in review text like "Report_only
            # flatpak packages". Every other action still falls back to its own
            # `action.value`, unchanged.
            default_verb = "report" if action == DiffAction.REPORT_ONLY else action.value
            verb = _ACTION_VOCABULARY.get((entries[0].item_class, action), default_verb)
            groups.append(
                ReviewGroup(
                    manager=self.manager_id,
                    action=action.value,
                    title=f"{verb.capitalize()} {self.manager_id} packages",
                    entries=tuple(
                        ReviewEntry(item_id=diff.item_id, label=diff.label, action_label=verb, detail=diff.detail)
                        for diff in entries
                    ),
                )
            )
        return tuple(groups)

    # -- plan() / accept_review() / apply() / execute() -------------------------------

    async def plan(self) -> PackagePlan:
        """Load decision files -> capture -> query -> diff -> build review groups. Read-only.

        Nothing here may mutate either machine: a job plans and reviews before it
        converges, so `plan()` runs before the user has approved anything. Both
        machines' decision files are loaded first (a read, like everything else here)
        and each side's captured/queried items are filtered through its OWN file before
        diffing (D-08): an item recorded on the source is dropped from the source
        manifest so it is never pushed to a peer again; an item recorded on the target
        is dropped from the target query so it is never proposed for
        install/remove/change again — either way it produces no `ItemDiff` and never
        reaches the review.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        target_decisions = await DecisionFile(self.manager_id, self.target).load()

        source_items = await filter_inert(await self.capture_source_items(), source_decisions)
        target_items = await filter_inert(await self.query_target_items(), target_decisions)
        hold_pin_facts = await self.collect_hold_pin_facts()
        missing_item_ids = frozenset(item.item_id for item in source_items) - frozenset(
            item.item_id for item in target_items
        )
        unavailable_item_ids = await self.collect_unavailable_item_ids(missing_item_ids)
        diffs = self.diff_items(
            source_items,
            target_items,
            hold_pin_facts=hold_pin_facts,
            unavailable_item_ids=unavailable_item_ids,
        )
        groups = self._build_review_groups(diffs)
        return PackagePlan(manager=self.manager_id, diffs=diffs, groups=groups)

    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Store this job's plan plus the outcome its own review returned."""
        self._accepted_plan = plan
        self._accepted_outcome = outcome

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

        Dry-run (ADR-014): each intended action is logged at FULL with a `[dry-run] `
        prefix and no converge command is ever issued.

        `REPORT_ONLY` diffs are excluded here regardless of decision: they imply no
        converge verb (D-25's held/pinned, version-mismatch, repo-unavailable,
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
        if total == 0:
            self._log(Host.TARGET, LogLevel.INFO, f"{prefix}No {self.manager_id} changes to apply")
            self._report_progress(ProgressUpdate(percent=100))
        else:
            self._log(Host.TARGET, LogLevel.INFO, f"{prefix}Applying {total} {self.manager_id} change(s)")

            for index, diff in enumerate(apply_diffs):
                if self.context.dry_run:
                    self._log(Host.TARGET, LogLevel.FULL, f"{prefix}Would {diff.action.value} {diff.label}")
                else:
                    await self._converge_one(diff, failures)
                self._report_progress(ProgressUpdate(percent=int((index + 1) / total * 100)))

            succeeded = total - len(failures)
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"{prefix}{succeeded}/{total} {self.manager_id} change(s) applied",
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
        held/pinned, repo-unavailable, unreproducible are informational only), so there
        is no "holder" for D-08a to record against.

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

            executor = self.source if diff.action in (DiffAction.INSTALL, DiffAction.CHANGE) else self.target
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

    async def _converge_one(self, diff: ItemDiff, failures: list[tuple[ItemDiff, str]]) -> None:
        try:
            result = await self.converge(diff)
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
        injected `JobContext.reviewer`, accept the outcome, then apply. No component
        outside the job owns its review, and no fallback applies diffs that never came
        back from one — a missing reviewer fails loudly here rather than silently skipping
        the review and converging unreviewed diffs (T-02-38).

        A `plan()` failure propagates unchanged, so the orchestrator's per-job exception
        handling attributes it to this job's own `JobResult`.
        """
        assert self.context.reviewer is not None, (
            f"{self.manager_id} sync has no reviewer; the orchestrator must inject one "
            "through JobContext.reviewer before execute()."
        )
        plan = await self.plan()
        outcome = await self.context.reviewer.review(plan.groups)
        self.accept_review(plan, outcome)
        await self.apply()
