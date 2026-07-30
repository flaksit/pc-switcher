"""What else apt would do, classified by origin at plan time (D-30).

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.packages.items import DiffAction
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    Decision,
)
from pcswitcher.models import CommandResult, LogLevel
from tests.unit.jobs.apt.helpers import (
    all_calls,
    decision_file,
    install_reviewer,
    make_context,
    target_offers,
)


class TestPlanTimeCollateral:
    """D-30: batched-simulation collateral is split by provenance against the target
    manual set — manual becomes a three-way review item, auto produces nothing.
    """

    @pytest.mark.asyncio
    async def test_manual_collateral_removal_becomes_a_collateral_review_item(self) -> None:
        """A package the install simulation would remove that IS in the target manual set
        becomes exactly one collateral review item, in a COLLATERAL_REVIEW_ACTION group,
        naming the triggering install (`other-manual` distinct from `pkg-a`).
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nother-manual\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = [diff for diff in plan.diffs if diff.item_id == "apt:collateral:other-manual"]
        assert len(collateral) == 1
        assert collateral[0].action == DiffAction.REPORT_ONLY
        assert collateral[0].label == "other-manual"
        assert collateral[0].detail == "Installing pkg-a on target-host would remove other-manual"

        collateral_group = next(g for g in plan.groups if g.action == COLLATERAL_REVIEW_ACTION)
        assert "apt:collateral:other-manual" in {entry.item_id for entry in collateral_group.entries}
        install_group = next(g for g in plan.groups if g.action == "install")
        assert "apt:collateral:other-manual" not in {entry.item_id for entry in install_group.entries}
        # pkg-a stays a normal, approvable install candidate.
        assert "apt:package:pkg-a" in {entry.item_id for entry in install_group.entries}

    @pytest.mark.asyncio
    async def test_auto_collateral_removal_produces_no_review_item(self) -> None:
        """A package the simulation would remove that is NOT in the target manual set is
        auto-installed — apt's own business (D-30) — so no review item is emitted and the
        install remains approvable.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv auto-dep [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(diff.item_id.startswith("apt:collateral:") for diff in plan.diffs)
        assert not any(group.action == COLLATERAL_REVIEW_ACTION for group in plan.groups)
        install_group = next(g for g in plan.groups if g.action == "install")
        assert "apt:package:pkg-a" in {entry.item_id for entry in install_group.entries}

    @pytest.mark.asyncio
    async def test_manual_downgrade_becomes_item_auto_downgrade_does_not(self) -> None:
        """A downgrade of a manually-installed package produces a collateral item the same
        way a removal does; a downgrade of an auto-installed package produces nothing.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nmanual-dg\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nmanual-dg\t2.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "manual-dg\n", ""),
                "dpkg-query": CommandResult(0, "manual-dg\t2.0\n", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst manual-dg [2.0] (1.0)\nInst auto-dg [2.0] (1.0)\n", ""
                ),
                "dpkg --compare-versions 1.0 lt 2.0": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral_ids = {diff.item_id for diff in plan.diffs if diff.item_id.startswith("apt:collateral:")}
        assert "apt:collateral:manual-dg" in collateral_ids
        assert "apt:collateral:auto-dg" not in collateral_ids
        manual_dg = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:manual-dg")
        assert manual_dg.detail is not None and "downgrade" in manual_dg.detail.lower()

    @pytest.mark.asyncio
    async def test_clean_simulation_produces_no_collateral_entry(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 1
        assert plan.diffs[0].item_id == "apt:package:pkg-a"

    @pytest.mark.asyncio
    async def test_at_most_two_apt_get_dash_s_commands_regardless_of_package_count(self) -> None:
        names = [f"pkg-{i}" for i in range(10)]
        showmanual = "\n".join(names) + "\n"
        dpkg_query = "\n".join(f"{name}\t1.0" for name in names) + "\n"
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, showmanual, ""),
                "dpkg-query": CommandResult(0, dpkg_query, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, target_offers(*names), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 10
        simulations = [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        # One, not zero: ten resolvable candidates rehearse in a single batch.
        assert len(simulations) == 1
        assert all(name in simulations[0] for name in names)


def _manual_collateral_context() -> tuple[JobContext, MagicMock, MagicMock]:
    """A job whose only install candidate (`pkg-a`) would, per the simulation, remove the
    manually-installed `other-manual` — the shared fixture for the go-ahead / keep-the-package
    flow tests. `other-manual` is manual and identical on both machines, so it is not a
    diff, only collateral. Its name is deliberately distinct from `pkg-a` so a bug that
    conflated the collateral package with its triggering install would be caught.
    """
    return make_context(
        source_responses={
            "apt-mark showmanual": CommandResult(0, "pkg-a\nother-manual\n", ""),
            "dpkg-query": CommandResult(0, "pkg-a\t1.0\nother-manual\t1.0\n", ""),
        },
        target_responses={
            "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
            "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
            "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
            "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""
            ),
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                CommandResult(0, "", "")
            ),
        },
    )


class TestCollateralFlow:
    """D-30 three-way outcome, end to end through execute()."""

    @pytest.mark.asyncio
    async def test_install_anyway_proceeds_and_guard_allows_the_collateral_removal(self) -> None:
        context, _source, target = _manual_collateral_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:other-manual": Decision.APPLY},
        )

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_skip_leaves_the_triggering_install_unapproved(self) -> None:
        context, _source, target = _manual_collateral_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:other-manual": Decision.SKIP_ONCE},
        )

        await job.execute()

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)


def _two_independent_removals_context() -> tuple[JobContext, MagicMock, MagicMock]:
    """Two removal candidates whose transactions are independent: removing `pkg-x` also
    removes the manually-installed `other-manual`, removing `pkg-y` removes nothing else.

    The batched rehearsal cannot tell those two apart — it names both candidates and one
    collateral package — so this fixture also answers the per-candidate rehearsals the
    attribution needs. `other-manual` is manual and identical on both machines, so it is
    collateral and never a diff of its own.
    """
    return make_context(
        source_responses={
            "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
            "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
        },
        target_responses={
            "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\nother-manual\n", ""),
            "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\nother-manual\t1.0\n", ""),
            # Longest first: `respond_to` matches by substring, first match wins.
            "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""
            ),
            "remove --assume-yes pkg-x": CommandResult(0, "Remv pkg-x [1.0]\nRemv other-manual [1.0]\n", ""),
            "remove --assume-yes pkg-y": CommandResult(0, "Remv pkg-y [1.0]\n", ""),
        },
    )


class TestCollateralAttribution:
    """D-30: a collateral item's triggers are the candidates whose OWN transaction causes
    it, so declining it cancels those and nothing else.
    """

    @pytest.mark.asyncio
    async def test_skip_cancels_only_the_candidate_whose_transaction_causes_it(self) -> None:
        """`pkg-y` removes nothing but itself, so a skip on `other-manual` must leave it
        approved and remove it — while `pkg-x`, which really would take `other-manual` with
        it, is the only candidate the skip cancels.
        """
        context, _source, target = _two_independent_removals_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.APPLY,
                "apt:collateral:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        real_removals = [cmd for cmd in all_calls(target) if "sudo" in cmd and "apt-get remove" in cmd]
        assert len(real_removals) == 1
        assert "pkg-y" in real_removals[0]
        assert "pkg-x" not in real_removals[0]

    @pytest.mark.asyncio
    async def test_a_collateral_skip_does_not_discard_a_trigger_own_skip_always(self) -> None:
        """The permanent decision is the user's, not the collateral question's. Both
        candidates really do take `other-manual` with them, so both are cancelled by the
        skip — but `pkg-y`'s "always skip" must survive the
        cancellation and still be recorded (D-08a: a REMOVE is target-held).
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\nother-manual\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""
                ),
                "remove --assume-yes pkg-x": CommandResult(0, "Remv pkg-x [1.0]\nRemv other-manual [1.0]\n", ""),
                "remove --assume-yes pkg-y": CommandResult(0, "Remv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.SKIP_ALWAYS,
                "apt:collateral:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        commands = all_calls(target)
        recorded = [cmd for cmd in commands if "mv --force" in cmd and "apt.decisions" in cmd]
        assert len(recorded) == 1
        assert "apt:package:pkg-y" in recorded[0]
        assert "apt:package:pkg-x" not in recorded[0]
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_a_collateral_skip_does_not_discard_an_unrelated_skip_always(self) -> None:
        """The same protection where attribution alone would also have saved it: `pkg-y` is
        no trigger of `other-manual`, so nothing may touch its permanent decision.
        """
        context, _source, target = _two_independent_removals_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.SKIP_ALWAYS,
                "apt:collateral:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        commands = all_calls(target)
        recorded = [cmd for cmd in commands if "mv --force" in cmd and "apt.decisions" in cmd]
        assert len(recorded) == 1
        assert "apt:package:pkg-y" in recorded[0]
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_the_narrowing_names_the_causing_candidate_in_the_question(self) -> None:
        """Attribution reaches the user, not just the decision map: the detail the review
        shows names `pkg-x` rather than "the selected packages".
        """
        context, _source, target = _two_independent_removals_context()

        plan = await AptSyncJob(context).plan()

        collateral = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:other-manual")
        assert collateral.detail == "Removing pkg-x on target-host would remove other-manual"
        # The cost, pinned: one batched rehearsal plus one per candidate, and only because
        # the batch found manual collateral.
        rehearsals = [cmd for cmd in all_calls(target) if cmd.startswith("apt-get --dry-run remove")]
        assert len(rehearsals) == 3

    @pytest.mark.asyncio
    async def test_joint_causation_names_the_whole_batch_rather_than_one_package(self) -> None:
        """Neither removal alone drops `other-manual`; only both together do. Declining then
        cancels both, so the sentence the user reads must not name just one of them.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\nother-manual\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""
                ),
                "remove --assume-yes pkg-x": CommandResult(0, "Remv pkg-x [1.0]\n", ""),
                "remove --assume-yes pkg-y": CommandResult(0, "Remv pkg-y [1.0]\n", ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        collateral = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:other-manual")
        assert collateral.detail == "Removing the packages listed earlier on target-host would remove other-manual"

    @pytest.mark.asyncio
    async def test_a_clean_batch_costs_no_extra_rehearsal(self) -> None:
        """No manual collateral, no narrowing: the two-simulation budget is unchanged for
        every run that has nothing to attribute.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\n", ""
                ),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert not any(diff.item_id.startswith("apt:collateral:") for diff in plan.diffs)
        rehearsals = [cmd for cmd in all_calls(target) if cmd.startswith("apt-get --dry-run remove")]
        assert len(rehearsals) == 1

    @pytest.mark.asyncio
    async def test_collateral_no_single_candidate_reproduces_is_blamed_on_the_whole_batch(self) -> None:
        """Joint causation — removing either candidate alone leaves `other-manual` in place,
        removing both takes it — is attributed to the whole batch, which is the honest
        answer and the only conservative one.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\nother-manual\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\nRemv other-manual [1.0]\n", ""
                ),
                "remove --assume-yes pkg-x": CommandResult(0, "Remv pkg-x [1.0]\n", ""),
                "remove --assume-yes pkg-y": CommandResult(0, "Remv pkg-y [1.0]\n", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.APPLY,
                "apt:collateral:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in all_calls(target))


_SOURCE_DECISION_SKIP_SRC_ONLY = (
    "machine_specific:\n"
    '  "apt:package:src-only":\n'
    "    item_class: apt_package\n"
    "    label: src-only\n"
    "    recorded_at: '2026-01-01T00:00:00Z'\n"
)


class TestSourceOnlyCollateral:
    """ADR-020 D-40: a package manual on the SOURCE alone is NOT protected from collateral
    removal/downgrade. The loss is deliberate — if the target's apt installed the package
    automatically, the target's apt owns it, and reclaiming it as a user choice on the
    strength of the other machine's bookkeeping is a guess. These two tests are kept
    inverted rather than deleted, as the record that the case was given up on purpose.
    """

    @pytest.mark.asyncio
    async def test_source_only_manual_collateral_removal_is_not_a_review_item(self) -> None:
        """`src-only` is manual on the source but skip-recorded there, so it is filtered
        out of the source manifest, and it is absent from the target manual set. Installing
        `pkg-a` would remove it, and that now happens silently: the target's own apt is the
        only bookkeeping consulted.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nsrc-only\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nsrc-only\t1.0\n", ""),
                "apt.decisions.yaml": CommandResult(0, _SOURCE_DECISION_SKIP_SRC_ONLY, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv src-only [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_id == "apt:collateral:src-only" for d in plan.diffs)
        # src-only was filtered from the source manifest, so it is not a review candidate
        # in its own right either — the removal reaches the user in no form at all.
        assert "apt:package:src-only" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_apply_time_guard_allows_source_only_manual_collateral(self) -> None:
        """The apply-time install guard reads the same narrowed set: a drifted real
        transaction that would remove a package manual on the SOURCE only proceeds.
        `src-only` is skip-recorded on the source so it is not a reviewed candidate.
        """
        sim_cmd = "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a"
        state = {"sim": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == "apt-mark showmanual":
                return CommandResult(0, "", "")
            if cmd == sim_cmd:
                state["sim"] += 1
                if state["sim"] == 1:
                    return CommandResult(0, "Inst pkg-a (1.0)\n", "")
                return CommandResult(0, "Inst pkg-a (1.0)\nRemv src-only [1.0]\n", "")
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nsrc-only\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nsrc-only\t1.0\n", ""),
                "apt.decisions.yaml": CommandResult(0, _SOURCE_DECISION_SKIP_SRC_ONLY, ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c for c in commands)


class TestRemovalCandidateKeepsItsProtection:
    """`PKG-FR-COLLATERAL-MANUAL`: being offered for removal is not consent to be removed.
    Only a removal the user APPROVED exempts a package, and no answer exists at plan time —
    so a removal candidate is protected from every OTHER transaction.
    """

    @staticmethod
    def _context() -> tuple[JobContext, MagicMock, MagicMock]:
        """`old-tool` is on the target alone, manually installed there, so it is both a
        removal candidate and a protected package. Installing `pkg-a` would take it.
        """
        return make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "old-tool\n", ""),
                "dpkg-query": CommandResult(0, "old-tool\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv old-tool [1.0]\n", ""
                ),
                "apt-get --dry-run remove --assume-yes old-tool": CommandResult(0, "Remv old-tool [1.0]\n", ""),
            },
        )

    @pytest.mark.asyncio
    async def test_a_removal_candidate_taken_by_an_install_is_still_asked_about(self) -> None:
        context, _source, _target = self._context()
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:old-tool")
        assert collateral.detail == "Installing pkg-a on target-host would remove old-tool"
        # Its own removal item is untouched: the two questions are about different things.
        assert "apt:package:old-tool" in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_skipping_that_removal_leaves_the_install_unapplied(self) -> None:
        """The whole point: the user kept `old-tool`, so the install that would have taken
        it is cancelled rather than attempted and refused.
        """
        context, _source, target = self._context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-a": Decision.APPLY,
                "apt:package:old-tool": Decision.SKIP_ONCE,
                "apt:collateral:old-tool": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        commands = all_calls(target)
        assert not any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in cmd for cmd in commands)
        assert not any("sudo DEBIAN_FRONTEND=noninteractive apt-get remove" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_a_removal_candidate_is_not_collateral_of_its_own_batch(self) -> None:
        """The removal batch's own candidates are what the batch is about: every one of them
        is in `preview.removals` by construction, and none is a question about itself.
        """
        context, _source, _target = self._context()
        job = AptSyncJob(context)

        plan = await job.plan()

        removal_batch_items = [
            d
            for d in plan.diffs
            if d.item_id == "apt:collateral:old-tool" and d.detail is not None and d.detail.startswith("Removing")
        ]
        assert removal_batch_items == []


class TestMarkedCollateral:
    """`PKG-FR-COLLATERAL-MARKED`: a machine-specific package is invisible in every other
    review line, so the collateral question is the only place the mark can be named.
    """

    @staticmethod
    def _context() -> tuple[JobContext, MagicMock, MagicMock]:
        """`vendor-tool` is marked machine-specific on the target and is NOT in the target's
        `apt-mark showmanual` set, so the mark alone is what protects it.
        """
        return make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt.decisions.yaml": CommandResult(0, decision_file("apt:package:vendor-tool"), ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv vendor-tool [1.0]\n", ""
                ),
            },
        )

    @pytest.mark.asyncio
    async def test_a_marked_package_is_protected_and_the_question_says_it_is_marked(self) -> None:
        context, _source, _target = self._context()
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:vendor-tool")
        assert collateral.detail == (
            "Installing pkg-a on target-host would remove vendor-tool — a package marked as target-host's own"
        )

    @pytest.mark.asyncio
    async def test_an_unmarked_manual_package_says_nothing_about_a_mark(self) -> None:
        context, _source, _target = _manual_collateral_context()
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:other-manual")
        assert collateral.detail is not None
        assert "marked as" not in collateral.detail


class TestCollateralUpgrade:
    """`PKG-FR-COLLATERAL-MANUAL` covers an upgrade too: moving a package the user chose off
    the version it was on is the same imposition a downgrade is.
    """

    @pytest.mark.asyncio
    async def test_manual_upgrade_becomes_a_collateral_item(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nmanual-up\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nmanual-up\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "manual-up\n", ""),
                "dpkg-query": CommandResult(0, "manual-up\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst manual-up [1.0] (2.0)\n", ""
                ),
                "dpkg --compare-versions 2.0 lt 1.0": CommandResult(1, "", ""),
                "dpkg --compare-versions 2.0 gt 1.0": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:manual-up")
        assert collateral.detail == "Installing pkg-a on target-host would upgrade manual-up from 1.0 to 2.0"


class TestAutoCollateralIsLogged:
    """`PKG-FR-COLLATERAL-AUTO`: a change nobody is asked about still has to be a change
    somebody can see afterwards.
    """

    @pytest.mark.asyncio
    async def test_auto_collateral_removal_is_named_in_the_log(self, caplog: pytest.LogCaptureFixture) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv auto-dep [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        with caplog.at_level(LogLevel.FULL):
            await job.plan()

        assert any(
            "would remove auto-dep" in record.message and "installed automatically" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_an_auto_version_change_is_logged_without_a_version_comparison(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The log line names both versions, so nothing has to run `dpkg --compare-versions`
        to say which way an unasked-about change goes.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst auto-dep [1.0] (2.0)\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        with caplog.at_level(LogLevel.FULL):
            await job.plan()

        assert any("change auto-dep from 1.0 to 2.0" in record.message for record in caplog.records)
        assert not any("dpkg --compare-versions" in cmd for cmd in all_calls(target))
