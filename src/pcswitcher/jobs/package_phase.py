"""`PackagePhaseCoordinator`: one review across every enabled package manager (D-15+D-24, ADR-020).

This module exists to satisfy two decisions that pull against each other. D-15 wants
three independent jobs (`apt_sync`, `snap_sync`, `flatpak_sync`) with their own config,
enable flags, failure isolation and progress. D-24 wants one batched review across all of
them before any change is applied. The orchestrator runs jobs sequentially, so a job that
reviews and converges inside its own `execute()` would let `apt_sync` finish mutating the
target before `snap_sync` had even looked at its own diff. The coordinator resolves that
by owning the one thing that must be global — the review — and leaving everything else
(config, validation, progress, its own `JobResult`) with the jobs.

`run()` does exactly three things, in order:
1. Plan every enabled job (`PackageSyncJob.plan()`), each isolated so one job's failure
   doesn't block the others.
2. Review once: concatenate every plan's `ReviewGroup`s (already ordered by manager/config
   order and, within a manager, by a fixed action order) and call `review_items()` exactly
   once.
3. Distribute: hand each job back only the slice of the outcome whose item ids appeared in
   that job's own plan (item ids already carry their manager prefix, so the split is by
   membership, not string parsing) via `accept_review()`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Protocol

from rich.console import Console

from pcswitcher.jobs.package_review import Decision, ReviewGroup, ReviewOutcome, review_items
from pcswitcher.jobs.package_sync_core import PackagePlan, PackageSyncJob
from pcswitcher.models import LogLevel

__all__ = ["PackagePhaseCoordinator", "coordinate_package_review"]

_logger = logging.getLogger("pcswitcher.jobs.package_phase")


class PausableUI(Protocol):
    """The subset of `TerminalUI` the coordinator needs to pause/resume the live display."""

    def pause(self) -> None: ...

    def resume(self) -> None: ...


class PackagePhaseCoordinator:
    """Runs plan() for every enabled package job, one review, then distributes decisions."""

    def __init__(self, console: Console, ui: PausableUI, *, logger: logging.Logger | None = None) -> None:
        self._console = console
        self._ui = ui
        self._logger = logger if logger is not None else _logger

    async def run(self, jobs: Sequence[PackageSyncJob]) -> None:
        """Plan every job, review once, distribute decisions. No-op for an empty job list."""
        if not jobs:
            return

        plans: dict[PackageSyncJob, PackagePlan] = {}
        for job in jobs:
            try:
                plans[job] = await job.plan()
            except Exception as exc:
                # Deliberately broad: isolate one job's plan() failure from the rest
                # (see module docstring) — record it and keep planning the others.
                job.record_plan_failure(exc)
                self._logger.warning(
                    "%s plan() failed; other package managers still plan/review: %s",
                    job.manager_id,
                    exc,
                    extra={"job": "package_phase", "host": "source"},
                )

        if not plans:
            # Every enabled job failed to plan: nothing to review, and constructing a
            # prompt for zero items would just be noise.
            return

        groups = self._merge_groups(plans.values())
        item_count = sum(len(group.entries) for group in groups)
        self._logger.info(
            "%d package manager(s) planned; review covers %d item(s)",
            len(plans),
            item_count,
            extra={"job": "package_phase", "host": "source"},
        )
        for job, plan in plans.items():
            self._logger.log(
                LogLevel.FULL,
                "%s: %d item(s) in plan",
                job.manager_id,
                sum(len(group.entries) for group in plan.groups),
                extra={"job": "package_phase", "host": "source"},
            )

        outcome = await review_items(groups, console=self._console, ui=self._ui, logger=self._logger)

        for job, plan in plans.items():
            job.accept_review(plan, self._slice_for(plan, outcome))

    @staticmethod
    def _merge_groups(plans: Iterable[PackagePlan]) -> list[ReviewGroup]:
        """Concatenate every plan's groups, preserving manager order (config order, D-17)
        and, within a manager, each plan's own fixed action order.
        """
        merged: list[ReviewGroup] = []
        for plan in plans:
            merged.extend(plan.groups)
        return merged

    @staticmethod
    def _slice_for(plan: PackagePlan, outcome: ReviewOutcome) -> ReviewOutcome:
        """This job's own item ids only — the review is global, a decision is per-manager."""
        plan_item_ids = {entry.item_id for group in plan.groups for entry in group.entries}
        decisions: dict[str, Decision] = {
            item_id: decision for item_id, decision in outcome.decisions.items() if item_id in plan_item_ids
        }
        return ReviewOutcome(decisions=decisions, was_interactive=outcome.was_interactive)


async def coordinate_package_review(
    jobs: Sequence[PackageSyncJob],
    *,
    console: Console,
    ui: PausableUI,
    logger: logging.Logger | None = None,
) -> None:
    """Module-level convenience wrapper the orchestrator calls from `_execute_jobs`."""
    coordinator = PackagePhaseCoordinator(console, ui, logger=logger)
    await coordinator.run(jobs)
