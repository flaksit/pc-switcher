"""Shared package-sync pipeline: `PackageSyncJob`'s plan()/apply() split (D-15, D-16, ADR-020).

D-15 wants three independent jobs (`apt_sync`, `snap_sync`, `flatpak_sync`) with their own
config, enable flag and failure isolation. D-24 wants ONE batched review across all of them
before any change is applied. The orchestrator runs jobs sequentially, so a job that reviews
and converges inside its own `execute()` would let `apt_sync` finish mutating the target
before `snap_sync` had even looked at its own diff — exactly the defect the cross-AI review
found (see ADR-020). Those two decisions only reconcile if a job's planning is separable
from its applying, which is what this module's `plan()`/`apply()` split provides:

- `plan()` captures the source manifest, queries the target, diffs, and builds this job's
  own `ReviewGroup`s. It issues READ commands only — nothing here may mutate either
  machine, because every enabled manager's `plan()` runs before the user has approved
  anything.
- `accept_review()` is how `PackagePhaseCoordinator` (plan 02-03 task 2) hands this job
  back its own slice of the one cross-manager review.
- `apply()` converges the `APPLY`-decided diffs, one item at a time, catching and
  collecting per-item failures (D-27) so one bad item never stops the rest.
- `execute()` — the `SyncJob` entry point the orchestrator's existing sequential loop
  calls — refuses to run without a coordinator-accepted plan, which makes the
  plan-before-apply ordering a structural property of the code, not a convention the next
  job author has to remember.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from pcswitcher.jobs.base import SyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.package_items import AptPackageItem, DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.package_review import Decision, ReviewEntry, ReviewGroup, ReviewOutcome
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
    """The read-only product of one job's `plan()` — the only thing the coordinator needs.

    `groups` are pre-built `ReviewGroup`s (one per action, removals in their own group,
    per D-07/D-24) so the coordinator only has to concatenate every job's groups, never
    re-derive them.
    """

    manager: str
    diffs: tuple[ItemDiff, ...]
    groups: tuple[ReviewGroup, ...]


# Verb + review-group title per action, in the fixed order groups are emitted. Install
# before change before remove keeps the most common/least-destructive action first;
# report_only trails since it needs a decision but implies no direct converge verb.
_ACTION_VERBS: dict[DiffAction, str] = {
    DiffAction.INSTALL: "install",
    DiffAction.CHANGE: "change",
    DiffAction.REMOVE: "remove",
    DiffAction.REPORT_ONLY: "report",
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
        self._plan_failure: Exception | None = None

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
        """
        ...

    # -- Shared diff -----------------------------------------------------------------

    def diff_items(
        self, source_items: Sequence[AptPackageItem], target_items: Sequence[AptPackageItem]
    ) -> tuple[ItemDiff, ...]:
        """Diff source against target into `ItemDiff`s.

        This slice produces only `MISSING_ON_TARGET`/`INSTALL` (a name present on the
        source but absent from the target). It is structured as a per-item-class
        dispatch (`_diff_apt_packages` today) so a later plan adds new item classes and
        diff directions (extra-on-target, version-mismatch, ...) by adding more private
        helpers here rather than reshaping this method.
        """
        diffs: list[ItemDiff] = []
        diffs.extend(self._diff_apt_packages(source_items, target_items))
        return tuple(diffs)

    @staticmethod
    def _diff_apt_packages(
        source_items: Sequence[AptPackageItem], target_items: Sequence[AptPackageItem]
    ) -> list[ItemDiff]:
        target_ids = {item.item_id for item in target_items}
        return [
            ItemDiff(
                item_class=ItemClass.APT_PACKAGE,
                diff_class=DiffClass.MISSING_ON_TARGET,
                action=DiffAction.INSTALL,
                item_id=item.item_id,
                label=item.label(),
                detail=None,
            )
            for item in source_items
            if item.item_id not in target_ids
        ]

    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """One `ReviewGroup` per action present in `diffs`, removals in their own group."""
        by_action: dict[DiffAction, list[ItemDiff]] = {}
        for diff in diffs:
            by_action.setdefault(diff.action, []).append(diff)

        groups: list[ReviewGroup] = []
        for action, verb in _ACTION_VERBS.items():
            entries = by_action.get(action)
            if not entries:
                continue
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
        """Capture -> query -> diff -> build review groups. Read-only.

        Nothing here may mutate either machine: every enabled manager's `plan()` runs
        (via `PackagePhaseCoordinator`) before the user has approved anything.
        """
        source_items = await self.capture_source_items()
        target_items = await self.query_target_items()
        diffs = self.diff_items(source_items, target_items)
        groups = self._build_review_groups(diffs)
        return PackagePlan(manager=self.manager_id, diffs=diffs, groups=groups)

    def record_plan_failure(self, exc: Exception) -> None:
        """Record that `plan()` raised, for `execute()` to re-raise later.

        Called by `PackagePhaseCoordinator` when this job's `plan()` fails; keeps the
        failure attributed to THIS job's `JobResult` even though the coordinator collects
        every job's `plan()` before any of them get a review outcome.
        """
        self._plan_failure = exc

    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Store this job's plan plus its slice of the coordinator's review outcome."""
        self._accepted_plan = plan
        self._accepted_outcome = outcome

    async def apply(self) -> None:
        """Converge every APPLY-decided diff from the accepted plan, one item at a time.

        Per-item detail at `LogLevel.FULL`, one `LogLevel.INFO` summary line per job
        (ADR-010). A per-item failure (`ConvergeItemFailed`, or a converge command that
        exits non-zero) is caught, logged with its stderr as structured context, and
        collected — the loop always completes (D-27) — then `PackageItemFailures` is
        raised once, after the loop, if anything failed.

        Dry-run (ADR-014): each intended action is logged at FULL with a `[dry-run] `
        prefix and no converge command is ever issued.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        plan = self._accepted_plan
        decisions = self._accepted_outcome.decisions

        apply_diffs = [diff for diff in plan.diffs if decisions.get(diff.item_id) == Decision.APPLY]
        prefix = "[dry-run] " if self.context.dry_run else ""
        total = len(apply_diffs)

        if total == 0:
            self._log(Host.TARGET, LogLevel.INFO, f"{prefix}No {self.manager_id} changes to apply")
            self._report_progress(ProgressUpdate(percent=100))
            return

        self._log(Host.TARGET, LogLevel.INFO, f"{prefix}Applying {total} {self.manager_id} change(s)")

        failures: list[tuple[ItemDiff, str]] = []
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

        if failures:
            summary = "; ".join(f"{diff.label}: {stderr.strip()}" for diff, stderr in failures)
            self._log(
                Host.TARGET,
                LogLevel.INFO,
                f"{len(failures)} {self.manager_id} item(s) failed: {summary}",
            )
            raise PackageItemFailures(self.manager_id, failures)

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

        Refuses to run without a coordinator-accepted plan — this is the structural
        enforcement of the plan-before-apply ordering (ADR-020, T-02-33). NO fallback
        exists that plans inline when no plan was accepted: a convenience path re-running
        the review per job would silently reintroduce the exact per-job-self-contained-
        review bug this split exists to remove, invisible until three managers ran at
        once. If `plan()` itself failed, re-raise that stored failure so it is attributed
        to this job's `JobResult` even though the coordinator already moved on to plan
        the other enabled managers.
        """
        if self._plan_failure is not None:
            raise self._plan_failure
        if self._accepted_plan is None or self._accepted_outcome is None:
            raise RuntimeError(
                f"{self.manager_id} sync has no coordinator-accepted plan; "
                "PackagePhaseCoordinator must run plan()/review_items()/accept_review() "
                "before execute()."
            )
        await self.apply()
