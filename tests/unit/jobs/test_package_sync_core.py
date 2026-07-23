"""Unit tests for `PackageSyncJob`'s shared diff engine, review grouping, and the
converge dispatch across all four `DiffAction`s (plan 02-05, D-07/D-24/D-25).

`FakeSyncJob` is a minimal concrete `PackageSyncJob` (empty capture/query, a
converge() that just records calls) so these tests exercise the SHARED pipeline —
`_diff_apt_packages`, `_build_review_groups`, `apply()`'s action-based dispatch —
independent of `AptSyncJob`'s apt-specific machinery, which `test_apt_sync.py` covers.
"""

from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.package_items import (
    AptPackageItem,
    DiffAction,
    DiffClass,
    HoldPinFact,
    ItemClass,
    ItemDiff,
)
from pcswitcher.jobs.package_review import Decision, ReviewOutcome
from pcswitcher.jobs.package_sync_core import PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult


def make_context(*, dry_run: bool = False) -> JobContext:
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
    target = MagicMock()
    target.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
    return JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
    )


def _diff(
    item_id: str, action: DiffAction, diff_class: DiffClass = DiffClass.MISSING_ON_TARGET, detail: str | None = None
) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=diff_class,
        action=action,
        item_id=item_id,
        label=item_id,
        detail=detail,
    )


class FakeSyncJob(PackageSyncJob):
    """Minimal concrete `PackageSyncJob`: empty capture/query, a recording converge()."""

    name: ClassVar[str] = "fake_sync"
    manager_id: ClassVar[str] = "fake"
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {}

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        self.converge_calls: list[ItemDiff] = []

    async def capture_source_items(self) -> list[AptPackageItem]:
        return []

    async def query_target_items(self) -> list[AptPackageItem]:
        return []

    async def validate(self) -> list[Any]:
        return []

    async def converge(self, diff: ItemDiff) -> CommandResult:
        self.converge_calls.append(diff)
        return CommandResult(0, "", "")


class FakeSyncJobWithFacts(FakeSyncJob):
    """`FakeSyncJob` with configurable source/target items and hook return values —
    exercises `plan()`'s wiring of `collect_hold_pin_facts`/`collect_unavailable_item_ids`
    into `diff_items` without any apt-specific machinery.
    """

    def __init__(
        self,
        context: JobContext,
        *,
        source_items: list[AptPackageItem] = [],  # noqa: B006 (test-only, never mutated)
        target_items: list[AptPackageItem] = [],  # noqa: B006
        hold_pin_facts: list[HoldPinFact] = [],  # noqa: B006
        unavailable_ids: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(context)
        self._source_items = source_items
        self._target_items = target_items
        self._hold_pin_facts = hold_pin_facts
        self._unavailable_ids = unavailable_ids

    async def capture_source_items(self) -> list[AptPackageItem]:
        return self._source_items

    async def query_target_items(self) -> list[AptPackageItem]:
        return self._target_items

    async def collect_hold_pin_facts(self) -> list[HoldPinFact]:
        return self._hold_pin_facts

    async def collect_unavailable_item_ids(self, missing_item_ids: frozenset[str]) -> frozenset[str]:
        return self._unavailable_ids & missing_item_ids


def _accept(job: PackageSyncJob, diffs: tuple[ItemDiff, ...], decisions: dict[str, Decision]) -> PackagePlan:
    plan = PackagePlan(manager=job.manager_id, diffs=diffs, groups=job._build_review_groups(diffs))
    job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))
    return plan


class TestDiffEngine:
    """`PackageSyncJob._diff_apt_packages` produces every D-25 diff class."""

    def test_missing_on_target_yields_install(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = PackageSyncJob._diff_apt_packages(source_items, [], (), frozenset())

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.MISSING_ON_TARGET
        assert diffs[0].action == DiffAction.INSTALL

    def test_extra_on_target_yields_remove(self) -> None:
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = PackageSyncJob._diff_apt_packages([], target_items, (), frozenset())

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.EXTRA_ON_TARGET
        assert diffs[0].action == DiffAction.REMOVE

    def test_version_mismatch_yields_report_only_with_both_versions(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="2.0")]

        diffs = PackageSyncJob._diff_apt_packages(source_items, target_items, (), frozenset())

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.VERSION_MISMATCH
        assert diffs[0].action == DiffAction.REPORT_ONLY
        assert diffs[0].detail is not None
        assert "1.0" in diffs[0].detail
        assert "2.0" in diffs[0].detail

    def test_equal_versions_yields_no_diff(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = PackageSyncJob._diff_apt_packages(source_items, target_items, (), frozenset())

        assert diffs == []

    def test_hold_fact_yields_held_or_pinned_naming_the_hold_mechanism(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]
        hold = HoldPinFact(mechanism="hold", package="pkg-a", source_ref="apt-mark showhold")

        diffs = PackageSyncJob._diff_apt_packages(source_items, target_items, [hold], frozenset())

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.HELD_OR_PINNED
        assert diffs[0].detail is not None
        assert "hold" in diffs[0].detail

    def test_pin_fact_yields_held_or_pinned_distinguishable_from_a_hold(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]
        pin = HoldPinFact(mechanism="pin", package="pkg-a", source_ref="/etc/apt/preferences.d/pkg-a-pin")
        hold = HoldPinFact(mechanism="hold", package="pkg-a", source_ref="apt-mark showhold")

        pin_diffs = PackageSyncJob._diff_apt_packages(source_items, target_items, [pin], frozenset())
        hold_diffs = PackageSyncJob._diff_apt_packages(source_items, target_items, [hold], frozenset())

        assert pin_diffs[0].diff_class == DiffClass.HELD_OR_PINNED
        assert hold_diffs[0].diff_class == DiffClass.HELD_OR_PINNED
        assert pin_diffs[0].detail != hold_diffs[0].detail

    def test_missing_and_unavailable_yields_repo_unavailable_not_install(self) -> None:
        source_items = [AptPackageItem(name="brscan3", version="")]

        diffs = PackageSyncJob._diff_apt_packages(source_items, [], (), frozenset({"apt:package:brscan3"}))

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.REPO_UNAVAILABLE
        assert diffs[0].action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_plan_wires_hooks_into_the_diff(self) -> None:
        """`plan()` calls `collect_hold_pin_facts`/`collect_unavailable_item_ids` and
        passes their results into `diff_items` — the generic hook contract, exercised
        without any apt-specific machinery.
        """
        job = FakeSyncJobWithFacts(
            make_context(),
            source_items=[AptPackageItem(name="pkg-a", version="1.0")],
            target_items=[],
            unavailable_ids=frozenset({"apt:package:pkg-a"}),
        )

        plan = await job.plan()

        assert len(plan.diffs) == 1
        assert plan.diffs[0].diff_class == DiffClass.REPO_UNAVAILABLE


class TestReviewGroupsByAction:
    """`_build_review_groups`: one group per action, removal titles name the verb."""

    def test_four_diffs_produce_four_groups_keyed_by_action(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = [
            _diff("i1", DiffAction.INSTALL),
            _diff("c1", DiffAction.CHANGE, DiffClass.VERSION_MISMATCH),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
            _diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),
        ]

        groups = job._build_review_groups(diffs)

        assert len(groups) == 4
        assert {g.action for g in groups} == {"install", "change", "remove", "report_only"}

    def test_removal_group_title_names_a_removal_verb_never_apply(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = [_diff("i1", DiffAction.INSTALL), _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET)]

        groups = job._build_review_groups(diffs)

        install_group = next(g for g in groups if g.action == "install")
        remove_group = next(g for g in groups if g.action == "remove")
        assert install_group.title != remove_group.title
        assert "remove" in remove_group.title.lower()
        assert "apply" not in remove_group.title.lower()
        assert "apply" not in install_group.title.lower()


class TestConvergeDispatchByAction:
    """`apply()` routes INSTALL/REMOVE/CHANGE to `converge()`; REPORT_ONLY never reaches it."""

    @pytest.mark.asyncio
    async def test_remove_diff_produces_exactly_one_target_converge_call(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = (_diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),)
        _accept(job, diffs, {"r1": Decision.APPLY})

        await job.apply()

        assert len(job.converge_calls) == 1
        assert job.converge_calls[0].action == DiffAction.REMOVE

    @pytest.mark.asyncio
    async def test_change_diff_reaches_converge_alongside_install_and_remove(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = (_diff("c1", DiffAction.CHANGE, DiffClass.VERSION_MISMATCH),)
        _accept(job, diffs, {"c1": Decision.APPLY})

        await job.apply()

        assert [d.item_id for d in job.converge_calls] == ["c1"]

    @pytest.mark.asyncio
    async def test_report_only_diff_produces_zero_target_commands(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = (_diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),)
        _accept(job, diffs, {"p1": Decision.APPLY})

        await job.apply()

        assert job.converge_calls == []

    @pytest.mark.asyncio
    async def test_ticking_only_install_group_yields_zero_removal_commands(self) -> None:
        job = FakeSyncJob(make_context())
        diffs = (
            _diff("i1", DiffAction.INSTALL),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
        )
        _accept(job, diffs, {"i1": Decision.APPLY, "r1": Decision.SKIP_ONCE})

        await job.apply()

        assert [d.item_id for d in job.converge_calls] == ["i1"]

    @pytest.mark.asyncio
    async def test_dry_run_zero_mutating_commands_across_all_four_action_types(self) -> None:
        job = FakeSyncJob(make_context(dry_run=True))
        diffs = (
            _diff("i1", DiffAction.INSTALL),
            _diff("c1", DiffAction.CHANGE, DiffClass.VERSION_MISMATCH),
            _diff("r1", DiffAction.REMOVE, DiffClass.EXTRA_ON_TARGET),
            _diff("p1", DiffAction.REPORT_ONLY, DiffClass.VERSION_MISMATCH),
        )
        _accept(job, diffs, {d.item_id: Decision.APPLY for d in diffs})

        await job.apply()

        assert job.converge_calls == []
