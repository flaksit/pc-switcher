"""What else apt would do, classified by origin at plan time (D-30).

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.apt_sync.commands import install_args
from pcswitcher.jobs.packages.items import DiffAction, ItemDiff
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    Decision,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.models import CommandResult, LogLevel, SyncAbortedByUser
from tests.unit.jobs.apt.helpers import (
    _APPROVE_PKG_A,
    CountingReviewer,
    _policy_block,
    _repo_context,
    all_calls,
    decision_file,
    foo_source_responses,
    foo_target_side_effect,
    install_reviewer,
    make_context,
    real_installs,
    respond_to,
    respond_with_policy_sequence,
    review_rounds,
    sha256_line,
    target_offers,
)


def finding(diff: ItemDiff) -> str:
    """The detail's first line — what the approved change would do to this package. The
    reason line under it has its own tests in `TestTheReasonNamesTheGroundThatApplies`.
    """
    assert diff.detail is not None
    return diff.detail.split("\n")[0]


class TestPlanTimeCollateral:
    """D-30: batched-simulation collateral is split by provenance against the target
    manual set — manual becomes a three-way review item, auto produces nothing.
    """

    @pytest.mark.asyncio
    async def test_manual_collateral_removal_becomes_a_collateral_review_item(self) -> None:
        """D10, D15, H55 — a package the install simulation would remove that IS in the target manual set
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

        collateral = [diff for diff in plan.diffs if diff.item_id == "apt:collateral:install:remove:other-manual"]
        assert len(collateral) == 1
        assert collateral[0].action == DiffAction.REPORT_ONLY
        assert collateral[0].label == "other-manual"
        assert finding(collateral[0]) == "Installing pkg-a on target-host would remove other-manual"

        collateral_group = next(g for g in plan.groups if g.action == COLLATERAL_REVIEW_ACTION)
        assert "apt:collateral:install:remove:other-manual" in {entry.item_id for entry in collateral_group.entries}
        install_group = next(g for g in plan.groups if g.action == "install")
        assert "apt:collateral:install:remove:other-manual" not in {entry.item_id for entry in install_group.entries}
        # pkg-a stays a normal, approvable install candidate.
        assert "apt:package:pkg-a" in {entry.item_id for entry in install_group.entries}

    @pytest.mark.asyncio
    async def test_auto_collateral_removal_produces_no_review_item(self) -> None:
        """D1, H62 — a package the simulation would remove that is NOT in the target manual set is
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
        """D6, D11 — a downgrade of a manually-installed package produces a collateral item the same
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
        assert "apt:collateral:install:downgrade:manual-dg" in collateral_ids
        assert "apt:collateral:install:downgrade:auto-dg" not in collateral_ids
        manual_dg = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:install:downgrade:manual-dg")
        assert manual_dg.detail is not None and "downgrade" in manual_dg.detail.lower()

    @pytest.mark.asyncio
    async def test_a_version_change_whose_two_versions_compare_equal_is_not_an_item(self) -> None:
        """D14 — apt reports an `Inst` line for `manual-x`, but dpkg ranks `1.0-0` and `1.0`
        the same: nothing is actually moved, so there is nothing to ask about.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "manual-x\n", ""),
                "dpkg-query": CommandResult(0, "manual-x\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst manual-x [1.0] (1.0-0)\n", ""
                ),
                "dpkg --compare-versions 1.0-0 lt 1.0": CommandResult(1, "", ""),
                "dpkg --compare-versions 1.0-0 gt 1.0": CommandResult(1, "", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        comparisons = [cmd for cmd in all_calls(target) if cmd.startswith("dpkg --compare-versions")]
        assert len(comparisons) == 2, "the two versions really were compared, both ways"
        assert not any(diff.item_id.startswith("apt:collateral:") for diff in plan.diffs)
        assert not any(group.action == COLLATERAL_REVIEW_ACTION for group in plan.groups)

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
        """D52 — ten resolvable candidates rehearse in one batch, not one command each."""
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


class TestNoRehearsalEverAsksToMoveHeldPackages:
    """`--allow-change-held-packages` appears in no command a run issues, rehearsal included.

    apt refusing to move a held package is the only thing protecting a held package apt
    installed automatically on the target. The one case that used to need the flag — a name
    the target held without having it installed, which apt refuses the whole batch over — can
    no longer reach a rehearsal: `PKG-FR-HOLD-WITHOUT-PACKAGE` ends the run over such a hold
    while planning.
    """

    @pytest.mark.asyncio
    async def test_an_ordinary_run_never_asks_for_it(self) -> None:
        """B21 — no command in a run carrying an install and a real hold asks for the flag."""
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "pkg-b\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-b\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a", "pkg-b"), ""),
            },
        )

        await AptSyncJob(context).plan()

        assert not any("--allow-change-held-packages" in cmd for cmd in all_calls(target))

    def test_the_install_arguments_cannot_express_it(self) -> None:
        """B20 — the flag is unreachable by construction: `install_args` has no parameter that
        could reintroduce it for the rehearsal alone.
        """
        assert "--allow-change-held-packages" not in install_args(["pkg-a"])
        assert list(inspect.signature(install_args).parameters) == ["names"]


class TestCollateralFlow:
    """D-30 three-way outcome, end to end through execute()."""

    @pytest.mark.asyncio
    async def test_install_anyway_proceeds_and_guard_allows_the_collateral_removal(self) -> None:
        """D23 — going ahead runs the causing install and the guard lets the removal through."""
        context, _source, target = _manual_collateral_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:install:remove:other-manual": Decision.APPLY},
        )

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_skip_leaves_the_triggering_install_unapproved(self) -> None:
        """D24, H29 — keeping the package leaves the causing install unapplied: no command runs."""
        context, _source, target = _manual_collateral_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:install:remove:other-manual": Decision.SKIP_ONCE},
        )

        await job.execute()

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_the_withdrawn_install_is_reported_skipped_rather_than_failed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D25 — the article's own remedy is leaving the change unapplied, not failing later:
        the run ends without a failed item and the log says the install was skipped.
        """
        context, _source, _target = _manual_collateral_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:install:remove:other-manual": Decision.SKIP_ONCE},
        )

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        messages = [record.message for record in caplog.records]
        assert "reviewed pkg-a (1.0) (install): skipped this run" in messages
        assert "No apt changes to apply" in messages
        assert not any(record.levelno >= LogLevel.ERROR.value for record in caplog.records)


class TestASkippedRemovalKeepsItsProtection:
    """Only a removal the user APPROVED exempts its package (`PKG-FR-COLLATERAL-MANUAL`), so
    a candidate answered any other way has to be ASKED about before another candidate's
    approved cascade carries it off.

    Every test here reads the SECOND round, because the question cannot exist before it: at
    plan time the removal batch is every candidate's own transaction and no answer yet
    distinguishes one from another (`Collateral.after_answers`).
    """

    @staticmethod
    def _context() -> tuple[JobContext, MagicMock, MagicMock]:
        """`pkg-x` and `pkg-y` are both on the target alone and manually installed there, so
        both are removal candidates and both are protected. The batch exempts both; `pkg-x`'s
        own transaction turns out to take `pkg-y` with it.
        """
        return make_context(
            source_responses={"apt-mark showmanual": CommandResult(0, "", ""), "dpkg-query": CommandResult(0, "", "")},
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\npkg-y\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\npkg-y\t1.0\n", ""),
                # Longest first: `respond_to` matches by substring, first match wins.
                "apt-get --dry-run remove --assume-yes pkg-x pkg-y": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\n", ""
                ),
                "apt-get --dry-run remove --assume-yes pkg-x": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv pkg-y [1.0]\n", ""
                ),
            },
        )

    @pytest.mark.asyncio
    async def test_a_skipped_candidate_is_asked_about_rather_than_told_about(self) -> None:
        """D37 — `pkg-y`'s own removal was skipped for this run, so it keeps its protection and
        the approved removal of `pkg-x` may not carry it off unasked. The question is absent
        from the first round and put in the second, and its reason names the ground: being
        offered for removal is not consent to be removed. Keeping `pkg-y` (the unanswered
        default) withdraws `pkg-x`'s removal, so neither package goes and nothing fails.
        """
        context, _source, target = self._context()
        job = AptSyncJob(context)
        plan = await job.plan()
        assert not any(diff.item_id.startswith("apt:collateral:") for diff in plan.diffs), (
            "the batch is every candidate's own transaction, so no answer distinguishes them yet"
        )

        rounds = await review_rounds(
            job, {"apt:package:pkg-x": Decision.APPLY, "apt:package:pkg-y": Decision.SKIP_ONCE}
        )

        assert [group.action for group in rounds[1]] == [COLLATERAL_REVIEW_ACTION]
        entries = [entry for group in rounds[1] for entry in group.entries]
        assert [entry.label for entry in entries] == ["pkg-y"]
        assert entries[0].detail == (
            "Removing pkg-x on target-host would remove pkg-y\n"
            "apt on target-host has pkg-y marked as manually installed, and its own removal was not "
            "approved in this review — being offered for removal is not consent to be removed."
        )
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_going_ahead_at_that_question_runs_the_approved_removal(self) -> None:
        """D72 — the answer the old refusal could never offer: letting the cascade go ahead
        removes `pkg-x`, and the apply-time guard lets `pkg-y` go with it. `pkg-y`'s own
        removal item stays skipped — it is not what was approved.
        """
        context, _source, target = self._context()
        job = AptSyncJob(context)

        await review_rounds(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.SKIP_ONCE,
                "apt:collateral:remove:remove:pkg-y": Decision.APPLY,
            },
        )

        removals = [cmd for cmd in all_calls(target) if "sudo" in cmd and "apt-get remove" in cmd]
        assert removals == ["sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes pkg-x"]

    @pytest.mark.asyncio
    async def test_a_mark_made_in_this_same_review_protects_its_package_from_the_cascade(self) -> None:
        """D46 — "never offer again" answered minutes earlier counts from that moment: keeping
        `pkg-y` leaves `pkg-x`'s removal unapplied, so nothing takes the marked package, and
        the run reports no failure — the answer withdrew the change rather than breaking it.
        """
        context, _source, target = self._context()
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-x": Decision.APPLY, "apt:package:pkg-y": Decision.SKIP_ALWAYS})

        await job.execute()

        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_the_question_names_the_mark_given_earlier_in_the_same_review(self) -> None:
        """D47 — the mark counts as a QUESTION and not only as a guard: `pkg-y` was exempt from
        the plan-time removal batch, so the second round is the only place it can be asked
        about, and the detail says the package was marked as the target's own in this review.
        """
        context, _source, _target = self._context()
        job = AptSyncJob(context)
        reviewer = CountingReviewer({"apt:package:pkg-x": Decision.APPLY, "apt:package:pkg-y": Decision.SKIP_ALWAYS})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert len(reviewer.calls) == 2, "the question exists only once the marks do"
        entries = [entry for group in reviewer.calls[1] for entry in group.entries]
        assert [group.action for group in reviewer.calls[1]] == [COLLATERAL_REVIEW_ACTION]
        assert [entry.label for entry in entries] == ["pkg-y"]
        assert entries[0].detail == (
            "Removing pkg-x on target-host would remove pkg-y\n"
            "apt on target-host has pkg-y marked as manually installed, and it was marked as "
            "target-host's own earlier in this review — either ground alone would protect it."
        )


class TestTheAnswersTheItemComposes:
    """`PKG-FR-EFFECT-NOT-MECHANISM`: each answer states its own effect, and only the layer
    that knows what causes the collateral can phrase them — so the sentences are asserted
    where they are composed, not where a screen renders hand-written ones.
    """

    @pytest.mark.asyncio
    async def test_an_install_cause_phrases_both_answers_around_the_install(self) -> None:
        """D20, D21 — the go-ahead names the causing change and the consequence; the keep
        answer names the package, the machine, the change that will not happen, and that the
        question comes back next sync.
        """
        context, _source, _target = _manual_collateral_context()

        plan = await AptSyncJob(context).plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:install:remove:other-manual")
        assert collateral.answer_hints == (
            "install pkg-a on target-host, so other-manual is removed as well",
            "keep other-manual on target-host; pkg-a will not be installed; will be asked again next sync",
        )

    @pytest.mark.asyncio
    async def test_a_removal_cause_flips_the_preposition_and_the_verb(self) -> None:
        """D21 — the same two answers about a removal read "remove … from" and "will not be
        removed": a sentence built for the install direction is wrong here in two places.
        """
        context, _source, _target = _two_independent_removals_context()

        plan = await AptSyncJob(context).plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:remove:remove:other-manual")
        assert collateral.answer_hints == (
            "remove pkg-x from target-host, so other-manual is removed as well",
            "keep other-manual on target-host; pkg-x will not be removed; will be asked again next sync",
        )


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
        """D48, H29 — `pkg-y` removes nothing but itself, so a skip on `other-manual` must leave it
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
                "apt:collateral:remove:remove:other-manual": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        real_removals = [cmd for cmd in all_calls(target) if "sudo" in cmd and "apt-get remove" in cmd]
        assert len(real_removals) == 1
        assert "pkg-y" in real_removals[0]
        assert "pkg-x" not in real_removals[0]

    @pytest.mark.asyncio
    async def test_a_collateral_skip_does_not_discard_a_trigger_own_skip_always(self) -> None:
        """D59, D61 — the permanent decision is the user's, not the collateral question's. Both
        candidates really do take `other-manual` with them, so both are cancelled by the
        skip — but `pkg-y`'s "always skip" must survive the
        cancellation and still be recorded (D-08a: a REMOVE is target-held).

        `pkg-y` is a declined trigger of the collateral, so this is also the whole of D61 that
        can be observed: the only other declined answer, `SKIP_ONCE`, would be overridden to
        the value it already holds.
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
                "apt:collateral:remove:remove:other-manual": Decision.SKIP_ONCE,
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
        """D60 — the same protection where attribution alone would also have saved it: `pkg-y` is
        no trigger of `other-manual`, so nothing may touch its permanent decision.
        """
        context, _source, target = _two_independent_removals_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-x": Decision.APPLY,
                "apt:package:pkg-y": Decision.SKIP_ALWAYS,
                "apt:collateral:remove:remove:other-manual": Decision.SKIP_ONCE,
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
        """D49 — attribution reaches the user, not just the decision map: the detail the review
        shows names `pkg-x` rather than "the selected packages".
        """
        context, _source, target = _two_independent_removals_context()

        plan = await AptSyncJob(context).plan()

        collateral = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:remove:remove:other-manual")
        assert finding(collateral) == "Removing pkg-x on target-host would remove other-manual"
        # The cost, pinned: one batched rehearsal plus one per candidate, and only because
        # the batch found manual collateral.
        rehearsals = [cmd for cmd in all_calls(target) if cmd.startswith("apt-get --dry-run remove")]
        assert len(rehearsals) == 3

    @pytest.mark.asyncio
    async def test_joint_causation_names_the_whole_batch_rather_than_one_package(self) -> None:
        """D51 — neither removal alone drops `other-manual`; only both together do. Declining then
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

        collateral = next(diff for diff in plan.diffs if diff.item_id == "apt:collateral:remove:remove:other-manual")
        assert finding(collateral) == "Removing the packages listed earlier on target-host would remove other-manual"

    @pytest.mark.asyncio
    async def test_a_clean_batch_costs_no_extra_rehearsal(self) -> None:
        """D52 — no manual collateral, no narrowing: the two-simulation budget is unchanged for
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
    async def test_a_single_candidate_is_its_own_answer_and_is_not_rehearsed_twice(self) -> None:
        """D53 — attribution costs one rehearsal per candidate, and a batch of one has
        nothing to narrow: the run that found manual collateral still pays for exactly one.
        """
        context, _source, target = _manual_collateral_context()

        plan = await AptSyncJob(context).plan()

        assert any(diff.item_id.startswith("apt:collateral:") for diff in plan.diffs), (
            "the batch must have found manual collateral, or the narrowing was never reachable"
        )
        assert len([cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]) == 1

    @pytest.mark.asyncio
    async def test_collateral_no_single_candidate_reproduces_is_blamed_on_the_whole_batch(self) -> None:
        """D50 — joint causation — removing either candidate alone leaves `other-manual` in place,
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
                "apt:collateral:remove:remove:other-manual": Decision.SKIP_ONCE,
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
        """D29, N15 — `src-only` is manual on the source but skip-recorded there, so it is filtered
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

        assert not any(d.item_id == "apt:collateral:install:remove:src-only" for d in plan.diffs)
        # src-only was filtered from the source manifest, so it is not a review candidate
        # in its own right either — the removal reaches the user in no form at all.
        assert "apt:package:src-only" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_apply_time_guard_allows_source_only_manual_collateral(self) -> None:
        """D30 — the apply-time install guard reads the same narrowed set: a drifted real
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
        """D33 — being offered for removal is not consent to be removed, and the removal
        item of its own is untouched."""
        context, _source, _target = self._context()
        job = AptSyncJob(context)

        plan = await job.plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:install:remove:old-tool")
        assert finding(collateral) == "Installing pkg-a on target-host would remove old-tool"
        # Its own removal item is untouched: the two questions are about different things.
        assert "apt:package:old-tool" in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_skipping_that_removal_leaves_the_install_unapplied(self) -> None:
        """D34 — the whole point: the user kept `old-tool`, so the install that would have taken
        it is cancelled rather than attempted and refused.
        """
        context, _source, target = self._context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-a": Decision.APPLY,
                "apt:package:old-tool": Decision.SKIP_ONCE,
                "apt:collateral:install:remove:old-tool": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        commands = all_calls(target)
        assert not any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in cmd for cmd in commands)
        assert not any("sudo DEBIAN_FRONTEND=noninteractive apt-get remove" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_a_removal_candidate_is_not_collateral_of_its_own_batch(self) -> None:
        """D35 — the removal batch's own candidates are what the batch is about: every one of them
        is in `preview.removals` by construction, and none is a question about itself.
        """
        context, _source, _target = self._context()
        job = AptSyncJob(context)

        plan = await job.plan()

        assert "apt:collateral:remove:remove:old-tool" not in {d.item_id for d in plan.diffs}


class TestTheReasonNamesTheGroundThatApplies:
    """`PKG-FR-COLLATERAL-MANUAL` wants the question to say why the package is protected;
    `PKG-FR-COLLATERAL-MARKED` wants the mark named where there is one. `protected()` is a
    union, so the sentence has to follow the ground that actually holds — a machine-specific
    package the target's apt pulled in automatically is protected by nothing apt knows about,
    and the question is the only place its mark is ever named.
    """

    @staticmethod
    def _marked_context(target_manual: str) -> tuple[JobContext, MagicMock, MagicMock]:
        """`vendor-tool` is marked machine-specific on the target; `target_manual` decides
        whether the target's `apt-mark showmanual` set protects it as well.
        """
        return make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, target_manual, ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
                "apt.decisions.yaml": CommandResult(0, decision_file("apt:package:vendor-tool"), ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv vendor-tool [1.0]\n", ""
                ),
            },
        )

    @pytest.mark.asyncio
    async def test_a_manually_installed_package_says_apt_has_it_marked_manual(self) -> None:
        """D15, D16 — the finding first, then the ground: apt on the target has it marked
        manually installed."""
        context, _source, _target = _manual_collateral_context()

        plan = await AptSyncJob(context).plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:install:remove:other-manual")
        assert collateral.detail == (
            "Installing pkg-a on target-host would remove other-manual\n"
            "apt on target-host has other-manual marked as manually installed: something asked for it there "
            "directly, rather than it arriving as another package's dependency."
        )

    @pytest.mark.asyncio
    async def test_a_package_only_a_mark_protects_says_so_and_claims_nothing_about_apt(self) -> None:
        """D42, H129 — a package apt considers automatic is protected by its mark alone, and the
        question says exactly that."""
        context, _source, _target = self._marked_context("")

        plan = await AptSyncJob(context).plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:install:remove:vendor-tool")
        assert collateral.detail == (
            "Installing pkg-a on target-host would remove vendor-tool\n"
            "vendor-tool is marked as target-host's own, so nothing else in this review mentions it."
        )

    @pytest.mark.asyncio
    async def test_the_group_title_names_both_grounds(self) -> None:
        """D17 — one group can hold packages protected on either ground, so the title cannot name
        one of them.
        """
        context, _source, _target = self._marked_context("")

        plan = await AptSyncJob(context).plan()

        group = next(g for g in plan.groups if g.action == COLLATERAL_REVIEW_ACTION)
        assert group.title == (
            "Packages you installed on target-host or marked as its own that this sync would remove, "
            "downgrade or upgrade (apt)"
        )

    @pytest.mark.asyncio
    async def test_a_package_both_grounds_cover_states_both(self) -> None:
        """D44 — both grounds hold, so the sentence states both and says either alone would
        protect it."""
        context, _source, _target = self._marked_context("vendor-tool\n")

        plan = await AptSyncJob(context).plan()

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:install:remove:vendor-tool")
        assert collateral.detail == (
            "Installing pkg-a on target-host would remove vendor-tool\n"
            "apt on target-host has vendor-tool marked as manually installed, and it is marked as "
            "target-host's own — either ground alone would protect it."
        )


class TestCollateralUpgrade:
    """`PKG-FR-COLLATERAL-MANUAL` covers an upgrade too: moving a package the user chose off
    the version it was on is the same imposition a downgrade is.
    """

    @pytest.mark.asyncio
    async def test_manual_upgrade_becomes_a_collateral_item(self) -> None:
        """D12 — an unasked-for upgrade is the same imposition a downgrade is."""
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

        collateral = next(d for d in plan.diffs if d.item_id == "apt:collateral:install:upgrade:manual-up")
        assert finding(collateral) == "Installing pkg-a on target-host would upgrade manual-up from 1.0 to 2.0"


class TestAutoCollateralIsLogged:
    """`PKG-FR-COLLATERAL-AUTO`: a change nobody is asked about still has to be a change
    somebody can see afterwards.
    """

    @pytest.mark.asyncio
    async def test_auto_collateral_removal_is_named_in_the_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """D3, H62, J104 — the line names the package, the change and why nobody was asked."""
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
    async def test_an_approved_removals_auto_casualty_is_named_in_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D5 — the removal direction owes the same line the install direction does: removing
        `pkg-x` takes `auto-dep`, nobody is asked, and the log is where that shows up.
        """
        context, _source, _target = make_context(
            source_responses={"apt-mark showmanual": CommandResult(0, "", ""), "dpkg-query": CommandResult(0, "", "")},
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\n", ""),
                "apt-get --dry-run remove --assume-yes pkg-x": CommandResult(
                    0, "Remv pkg-x [1.0]\nRemv auto-dep [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        with caplog.at_level(LogLevel.FULL):
            await job.plan()

        assert any(
            "Removing pkg-x on target-host would remove auto-dep" in record.message
            and "installed automatically" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_an_auto_version_change_is_logged_without_a_version_comparison(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D8, J105 — the log line names both versions, so nothing has to run `dpkg --compare-versions`
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


class TestAutoCollateralIsLoggedFromTheTransactionThatHappens:
    """`PKG-FR-COLLATERAL-AUTO` binds the apply-time transaction as well as the rehearsal: a
    change nobody is asked about still has to be a change somebody can see, and the one that
    actually ran is the one the log owes an account of.
    """

    @pytest.mark.asyncio
    async def test_an_auto_casualty_only_the_real_transaction_predicts_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """D9 — the plan-time rehearsal is clean and the real transaction takes `auto-dep`:
        the only line naming it comes from the transaction that happened."""
        sim = "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a"
        state = {"sim": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == sim:
                state["sim"] += 1
                return CommandResult(
                    0, "Inst pkg-a (1.0)\n" + ("Remv auto-dep [1.0]\n" if state["sim"] > 1 else ""), ""
                )
            if cmd.startswith("apt-cache policy"):
                return CommandResult(0, target_offers("pkg-a"), "")
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
        )
        target.run_command = AsyncMock(side_effect=target_side_effect)
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        with caplog.at_level(LogLevel.FULL):
            await job.execute()

        assert [record.message for record in caplog.records if "auto-dep" in record.message] == [
            "Installing pkg-a on target-host would remove auto-dep "
            "(auto-dep is installed automatically on target-host; not asked)"
        ]
        assert len(real_installs(target)) == 1

    @pytest.mark.asyncio
    async def test_the_same_casualty_is_logged_at_plan_time_and_again_from_the_real_transaction(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """J106 — two lines, not one: the rehearsal's names the whole batch it rehearsed, and
        the apply-time one names the single install whose transaction ran."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a", "pkg-b"), ""),
                # Longest first: `respond_to` matches by substring, first match wins.
                "--no-install-recommends pkg-a pkg-b": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst pkg-b (1.0)\nRemv auto-dep [1.0]\n", ""
                ),
                "--no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\nRemv auto-dep [1.0]\n", ""),
                "--no-install-recommends pkg-b": CommandResult(0, "Inst pkg-b (1.0)\n", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {**_APPROVE_PKG_A, "apt:package:pkg-b": Decision.APPLY})

        with caplog.at_level(LogLevel.FULL):
            await job.execute()

        assert [record.message for record in caplog.records if "auto-dep" in record.message] == [
            "Installing pkg-a, pkg-b on target-host would remove auto-dep "
            "(auto-dep is installed automatically on target-host; not asked)",
            "Installing pkg-a on target-host would remove auto-dep "
            "(auto-dep is installed automatically on target-host; not asked)",
        ]


class TestOnePackageTwoConsequences:
    """`PKG-FR-COLLATERAL-MANUAL` wants consent to the consequence, and one protected package
    can be the casualty of two of them in one run: an approved install's transaction and an
    approved removal's cascade. Keyed on the package alone, the two shared one item — either
    answer governed both, and the second overwrote the first's attribution.
    """

    @staticmethod
    def _context() -> tuple[JobContext, MagicMock, MagicMock]:
        """`victim` is manual and identical on both machines, so it is never a diff of its
        own. Installing `pkg-a` takes it, and so does removing `going`.
        """
        return make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nvictim\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nvictim\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "going\nvictim\n", ""),
                "dpkg-query": CommandResult(0, "going\t1.0\nvictim\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv victim [1.0]\n", ""
                ),
                "apt-get --dry-run remove --assume-yes going": CommandResult(
                    0, "Remv going [1.0]\nRemv victim [1.0]\n", ""
                ),
            },
        )

    @pytest.mark.asyncio
    async def test_each_consequence_is_its_own_item_with_its_own_cause(self) -> None:
        """D13, D54 — one package, two consequences: two items, each naming its own cause."""
        context, _source, _target = self._context()

        plan = await AptSyncJob(context).plan()

        by_id = {d.item_id: d for d in plan.diffs if d.item_id.startswith("apt:collateral:")}
        assert set(by_id) == {"apt:collateral:install:remove:victim", "apt:collateral:remove:remove:victim"}
        assert finding(by_id["apt:collateral:install:remove:victim"]) == (
            "Installing pkg-a on target-host would remove victim"
        )
        assert finding(by_id["apt:collateral:remove:remove:victim"]) == (
            "Removing going on target-host would remove victim"
        )

    @pytest.mark.asyncio
    async def test_letting_the_installs_casualty_go_ahead_does_not_release_the_removals(self) -> None:
        """D55, H48 — the whole consent model: a go-ahead on one consequence must leave the guard
        refusing the other, rather than exempting the package outright.
        """
        context, _source, target = self._context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-a": Decision.APPLY,
                "apt:package:going": Decision.APPLY,
                "apt:collateral:install:remove:victim": Decision.APPLY,
                # The removal's own casualty is answered separately, and this answer is
                # "keep victim" — which cancels the removal that causes it.
                "apt:collateral:remove:remove:victim": Decision.SKIP_ONCE,
            },
        )

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_the_apply_time_guard_matches_the_consequence_not_the_package(self) -> None:
        """D56 — the apply-time half of the same rule. The removal's transaction drifts after
        plan time to take `victim` as well, and the go-ahead the user gave for the INSTALL's
        casualty does not cover it: the removal raises its own question, which this run
        leaves unanswered, so the removal is withdrawn while the install still runs.
        """
        removal = "apt-get --dry-run remove --assume-yes going"
        state = {"removals": 0}

        def target_side_effect(cmd: str, **_: object) -> CommandResult:
            if cmd == removal:
                state["removals"] += 1
                if state["removals"] == 1:
                    return CommandResult(0, "Remv going [1.0]\n", "")
                return CommandResult(0, "Remv going [1.0]\nRemv victim [1.0]\n", "")
            return respond_to(
                {
                    "apt-mark showmanual": CommandResult(0, "going\nvictim\n", ""),
                    "dpkg-query": CommandResult(0, "going\t1.0\nvictim\t1.0\n", ""),
                    "apt-cache policy": CommandResult(0, target_offers("pkg-a"), ""),
                    "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                        0, "Inst pkg-a (1.0)\nRemv victim [1.0]\n", ""
                    ),
                }
            )(cmd)

        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nvictim\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nvictim\t1.0\n", ""),
            },
            target_side_effect=target_side_effect,
        )
        job = AptSyncJob(context)
        reviewer = CountingReviewer(
            {
                "apt:package:pkg-a": Decision.APPLY,
                "apt:package:going": Decision.APPLY,
                "apt:collateral:install:remove:victim": Decision.APPLY,
            }
        )
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert _collateral_entry_ids(reviewer)[-1] == {"apt:collateral:remove:remove:victim"}
        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)
        assert not any("sudo" in cmd and "apt-get remove" in cmd for cmd in commands)


def _late_collateral_context(
    *,
    simulation: str = "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n",
    source_extra: dict[str, CommandResult] | None = None,
    target_extra: dict[str, CommandResult] | None = None,
) -> tuple[JobContext, MagicMock, MagicMock]:
    """`pkg-a` comes from a repository this run writes, so the target's apt cannot resolve
    the name while the review is being built and the plan-time rehearsal leaves it out.

    Installing it would take `other-manual`, which the target has manually installed. That
    fact does not exist until `foo.sources` and the run's `apt-get update` have landed, which
    is exactly the case `PKG-FR-ASK-AGAIN` allows a second question for.
    """
    context, source, target = _repo_context(
        source_responses=foo_source_responses(
            **{
                "apt-mark showmanual": CommandResult(0, "pkg-a\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nother-manual\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://example.com")
                    + _policy_block("other-manual", "https://example.com"),
                    "",
                ),
                **(source_extra or {}),
            }
        )
    )
    target.run_command = AsyncMock(
        side_effect=foo_target_side_effect(
            {
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                "apt-get --dry-run install": CommandResult(0, simulation, ""),
                **(target_extra or {}),
            }
        )
    )
    return context, source, target


class AbortingReviewer(CountingReviewer):
    """Answers the plan review, then stops the sync at the collateral question — what
    `_review_collateral_group` does for the third answer.
    """

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        if any(group.action == COLLATERAL_REVIEW_ACTION for group in groups):
            raise SyncAbortedByUser("other-manual on target-host would have been removed")
        return await super().review(groups)


class SilentReviewer(CountingReviewer):
    """Answers the plan review, then reports that nobody was there for the collateral
    question (`PKG-FR-NO-TERMINAL`).

    Defence in depth rather than a production path: a run with no terminal and a non-empty
    plan is skipped before `apply()` ever runs, so this shape is only reachable if that
    guard is ever weakened.
    """

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        outcome = await super().review(groups)
        if any(group.action == COLLATERAL_REVIEW_ACTION for group in groups):
            return ReviewOutcome(decisions={}, was_interactive=False)
        return outcome


def _collateral_entry_ids(reviewer: CountingReviewer) -> list[set[str]]:
    """The collateral question's entries per review call — `[]` for a call that asked none."""
    return [
        {entry.item_id for group in groups if group.action == COLLATERAL_REVIEW_ACTION for entry in group.entries}
        for groups in reviewer.calls
    ]


class TestCollateralForARepositoryThisRunWrites:
    """`PKG-FR-ASK-AGAIN`: an install whose repository this run writes cannot be simulated
    at plan time, so its collateral question is put once `/etc/apt` has converged — the
    three answers `PKG-FR-COLLATERAL-MANUAL` requires, not a late refusal.
    """

    @pytest.mark.asyncio
    async def test_keeping_the_package_leaves_the_install_unapplied_and_unfailed(self) -> None:
        """D64, D66, H17, H42, H44 — the whole ruling in one run: the install does not run, `other-manual` survives,
        and the job reports no failed item — a change the user declined is not a change that
        broke.
        """
        context, _source, target = _late_collateral_context()
        job = AptSyncJob(context)
        reviewer = CountingReviewer(_APPROVE_PKG_A)
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert real_installs(target) == []
        assert not any("apt-get remove" in cmd for cmd in all_calls(target))
        assert _collateral_entry_ids(reviewer) == [set(), {"apt:collateral:install:remove:other-manual"}]

    @pytest.mark.asyncio
    async def test_the_question_is_absent_from_the_plan_time_review(self) -> None:
        """D63, H41 — the facts genuinely do not exist yet: the target's apt has never heard `pkg-a`,
        so plan time rehearses nothing and asks nothing.
        """
        context, _source, target = _late_collateral_context()

        plan = await AptSyncJob(context).plan()

        assert not any(diff.item_id.startswith("apt:collateral:") for diff in plan.diffs)
        assert not any("apt-get --dry-run" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_going_ahead_installs_and_the_guard_allows_the_collateral_removal(self) -> None:
        """D67 — going ahead at the late question installs, and the guard allows the removal."""
        context, _source, target = _late_collateral_context()
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {**_APPROVE_PKG_A, "apt:collateral:install:remove:other-manual": Decision.APPLY},
        )

        await job.execute()

        assert len(real_installs(target)) == 1

    @pytest.mark.asyncio
    async def test_stopping_ends_the_whole_sync(self) -> None:
        """D68 — the stopping answer reaches as far here as it does at plan time: the reviewer
        raises, and nothing catches it inside the job.
        """
        context, _source, target = _late_collateral_context()
        job = AptSyncJob(context)
        job.context = dataclasses.replace(job.context, reviewer=AbortingReviewer(_APPROVE_PKG_A))

        with pytest.raises(SyncAbortedByUser):
            await job.execute()

        assert real_installs(target) == []

    @pytest.mark.asyncio
    async def test_a_run_with_no_terminal_declines_it(self) -> None:
        """D69, H47, J42 — `PKG-FR-NO-TERMINAL`: nobody to ask means every item is declined for this run, so
        the install is withheld rather than pushed through or failed.
        """
        context, _source, target = _late_collateral_context()
        job = AptSyncJob(context)
        job.context = dataclasses.replace(job.context, reviewer=SilentReviewer(_APPROVE_PKG_A))

        await job.execute()

        assert real_installs(target) == []

    @pytest.mark.asyncio
    async def test_the_decision_is_named_in_the_log(self, caplog: pytest.LogCaptureFixture) -> None:
        """D70, H46, J5, J102 — `PKG-FR-LOG-DECISIONS`: this question is asked outside the plan, so the base
        per-item decision pass cannot name it and this layer must.
        """
        context, _source, _target = _late_collateral_context()
        job = AptSyncJob(context)
        job.context = dataclasses.replace(job.context, reviewer=CountingReviewer(_APPROVE_PKG_A))

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        messages = [record.message for record in caplog.records]
        assert "reviewed other-manual (collateral): skip now" in messages
        assert any("pkg-a" in message and "not applied" in message for message in messages)

    @pytest.mark.asyncio
    async def test_the_question_costs_nothing_on_a_run_with_no_late_install(self) -> None:
        """D62, H45 — every install the target can already resolve is settled at plan time, so the
        converge loop puts no question and issues no extra rehearsal.
        """
        context, _source, _target = _manual_collateral_context()
        job = AptSyncJob(context)
        reviewer = CountingReviewer(
            {"apt:package:pkg-a": Decision.APPLY, "apt:collateral:install:remove:other-manual": Decision.APPLY}
        )
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert len(reviewer.calls) == 1


def _no_candidate(*names: str) -> str:
    """`apt-cache policy` for names the target's apt cannot resolve yet — what a package whose
    repository this run has not written answers at plan time."""
    return "".join(f"{name}:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n" for name in names)


def _two_late_installs_context() -> tuple[JobContext, MagicMock, MagicMock]:
    """`pkg-a` and `pkg-b` both come from the repository `foo.sources` declares, and neither is
    resolvable on the target until this run writes it.

    Installing `pkg-a` would take `other-manual`; installing `pkg-b` takes nothing. So keeping
    `other-manual` withdraws `pkg-a` alone and leaves `foo.sources` with a package to serve.
    """
    context, source, target = _repo_context(
        source_responses=foo_source_responses(
            **{
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\nother-manual\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://example.com")
                    + _policy_block("pkg-b", "https://example.com")
                    + _policy_block("other-manual", "https://example.com"),
                    "",
                ),
            }
        )
    )
    target.run_command = AsyncMock(
        side_effect=respond_with_policy_sequence(
            {
                "echo $HOME": CommandResult(0, "/home/target-user", ""),
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                "test -f": CommandResult(1, "", ""),
                # Longest first: `respond_to` matches by substring, first match wins, and the
                # batch command contains both single-candidate patterns.
                "--no-install-recommends pkg-a pkg-b": CommandResult(
                    0, "Inst pkg-a (1.0)\nInst pkg-b (1.0)\nRemv other-manual [1.0]\n", ""
                ),
                "--no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""),
                "--no-install-recommends pkg-b": CommandResult(0, "Inst pkg-b (1.0)\n", ""),
            },
            [
                CommandResult(0, _no_candidate("pkg-a", "pkg-b"), ""),
                CommandResult(0, target_offers("pkg-a", "pkg-b", origin="https://example.com"), ""),
            ],
        )
    )
    return context, source, target


_STRANDED_FOO = (
    "/etc/apt/sources.list.d/foo.sources stays on target-host: it was written for pkg-a, whose install was "
    "declined, so nothing on target-host installs from https://example.com. Left in place — remove it by "
    "hand if it is not wanted."
)


class TestARepositoryWrittenForADeclinedInstall:
    """The `/etc/apt` files a late decline leaves behind are kept, and the run says so
    (`PKG-FR-REPO-DERIVED`, `PKG-FR-LOG-DECISIONS`).

    The write has already landed when the question is put — that is the whole reason the
    question is late — and undoing it would reverse a write on the strength of an answer
    about a package. So the run names the file, by URL as well as by filename, and leaves it.
    """

    @pytest.mark.asyncio
    async def test_the_repository_is_named_by_url_and_filename(self, caplog: pytest.LogCaptureFixture) -> None:
        """C64, C65, N11 — the file stays on the target, and one line names its path and its URL,
        says nothing installs from it, and says it was left in place."""
        context, _source, _target = _late_collateral_context()
        job = AptSyncJob(context)
        job.context = dataclasses.replace(job.context, reviewer=CountingReviewer(_APPROVE_PKG_A))

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        assert _STRANDED_FOO in [record.message for record in caplog.records]

    @pytest.mark.asyncio
    async def test_it_does_not_read_as_something_broken(self, caplog: pytest.LogCaptureFixture) -> None:
        """C66 — nothing failed: the user answered, and this is the consequence of their answer."""
        context, _source, _target = _late_collateral_context()
        job = AptSyncJob(context)
        job.context = dataclasses.replace(job.context, reviewer=CountingReviewer(_APPROVE_PKG_A))

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        stranded = next(record for record in caplog.records if record.message == _STRANDED_FOO)
        assert stranded.levelno == LogLevel.INFO.value
        assert not any(record.levelno >= LogLevel.WARNING.value for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_repository_a_surviving_install_still_needs_is_not_named(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """C67 — `pkg-b` comes from the same repository and nothing withdrew it, so `foo.sources` is
        doing exactly the job it was written for.
        """
        context, _source, target = _two_late_installs_context()
        job = AptSyncJob(context)
        job.context = dataclasses.replace(
            job.context, reviewer=CountingReviewer({**_APPROVE_PKG_A, "apt:package:pkg-b": Decision.APPLY})
        )

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        commands = all_calls(target)
        # The file really did land, so the negative below is about the rule and not about a
        # run that derived nothing.
        assert any("sudo install" in cmd and "sources.list.d/foo.sources" in cmd for cmd in commands)
        installs = real_installs(target)
        assert len(installs) == 1
        assert "pkg-b" in installs[0]
        assert not any("stays on target-host" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_repository_whose_own_write_failed_is_not_named(self, caplog: pytest.LogCaptureFixture) -> None:
        """C68 — nothing landed on the target, so there is nothing left in place to report.
        The user still answered the same question, and `pkg-a` is still withdrawn.
        """
        context, _source, _target = _late_collateral_context(
            target_extra={
                "sudo install --owner=root --group=root --mode=0644 "
                "/home/target-user/.cache/pc-switcher/apt-staging/etc_apt_sources.list.d_foo.sources": (
                    CommandResult(1, "", "Read-only file system")
                )
            }
        )
        job = AptSyncJob(context)
        reviewer = CountingReviewer(_APPROVE_PKG_A)
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        assert "/etc/apt/sources.list.d/foo.sources" in job._work.derived.failed  # pyright: ignore[reportPrivateUsage]
        assert _collateral_entry_ids(reviewer)[-1] == {"apt:collateral:install:remove:other-manual"}
        assert not any("stays on target-host" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_a_derived_pin_is_never_named_as_stranded(self, caplog: pytest.LogCaptureFixture) -> None:
        """C69 — a pin travels because the source has it, not because a package was approved,
        so no answer about a package can strand one. Only the repository is named.
        """
        context, _source, target = _late_collateral_context(
            source_extra={"find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "mozilla"), "")}
        )
        job = AptSyncJob(context)
        job.context = dataclasses.replace(job.context, reviewer=CountingReviewer(_APPROVE_PKG_A))

        with caplog.at_level(LogLevel.FULL.value):
            await job.execute()

        assert any(
            "sudo install" in cmd and cmd.endswith("/etc/apt/preferences.d/mozilla") for cmd in all_calls(target)
        ), "the pin really did land, or the negative below is about nothing"
        assert [record.message for record in caplog.records if "stays on target-host" in record.message] == [
            _STRANDED_FOO
        ]


def _two_late_casualties_context() -> tuple[JobContext, MagicMock, MagicMock]:
    """`_two_late_installs_context`, except that each unsimulatable install takes a protected
    package of its own — `pkg-a` takes `other-manual`, `pkg-b` takes `second-manual`.

    Two DIFFERENT casualties, because one shared casualty is one consequence and so one entry
    however many installs cause it: only two entries can show whether the round is one
    question or one per install.
    """
    context, source, target = _repo_context(
        source_responses=foo_source_responses(
            **{
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\nother-manual\nsecond-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\nother-manual\t1.0\nsecond-manual\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://example.com")
                    + _policy_block("pkg-b", "https://example.com")
                    + _policy_block("other-manual", "https://example.com")
                    + _policy_block("second-manual", "https://example.com"),
                    "",
                ),
            }
        )
    )
    target.run_command = AsyncMock(
        side_effect=respond_with_policy_sequence(
            {
                "echo $HOME": CommandResult(0, "/home/target-user", ""),
                "apt-mark showmanual": CommandResult(0, "other-manual\nsecond-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\nsecond-manual\t1.0\n", ""),
                "test -f": CommandResult(1, "", ""),
                # Longest first: `respond_to` matches by substring, first match wins, and the
                # batch command contains both single-candidate patterns.
                "--no-install-recommends pkg-a pkg-b": CommandResult(
                    0,
                    "Inst pkg-a (1.0)\nInst pkg-b (1.0)\nRemv other-manual [1.0]\nRemv second-manual [1.0]\n",
                    "",
                ),
                "--no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""),
                "--no-install-recommends pkg-b": CommandResult(0, "Inst pkg-b (1.0)\nRemv second-manual [1.0]\n", ""),
            },
            [
                CommandResult(0, _no_candidate("pkg-a", "pkg-b"), ""),
                CommandResult(0, target_offers("pkg-a", "pkg-b", origin="https://example.com"), ""),
            ],
        )
    )
    return context, source, target


class _CommandCountingReviewer(CountingReviewer):
    """`CountingReviewer` that also records how many commands the target had been given at
    the moment of each review — the only way to put a question and a command on one timeline.
    """

    def __init__(self, decisions: dict[str, Decision], target: MagicMock) -> None:
        super().__init__(decisions)
        self._target = target
        self.commands_before: list[int] = []

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self.commands_before.append(len(self._target.run_command.call_args_list))
        return await super().review(groups)


class TestTheLateQuestionIsPutOnceBeforeAnyTransaction:
    """`PKG-FR-BATCHED` and `PKG-FR-CONSENT-BEFORE-CHANGE` for the mid-apply question: two
    unsimulatable installs are asked about together, and the last answer comes before the
    first of the transactions they are about.
    """

    @pytest.mark.asyncio
    async def test_two_unsimulatable_installs_share_one_question(self) -> None:
        """H43 — each install takes a protected package of its own, so the round carries two
        entries; `PKG-FR-BATCHED` binds within it, so they come as one group in one call
        rather than a question per install.
        """
        context, _source, _target = _two_late_casualties_context()
        job = AptSyncJob(context)
        reviewer = CountingReviewer({**_APPROVE_PKG_A, "apt:package:pkg-b": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert len(reviewer.calls) == 2
        assert [group.action for group in reviewer.calls[1]] == [COLLATERAL_REVIEW_ACTION]
        assert {entry.item_id for entry in reviewer.calls[1][0].entries} == {
            "apt:collateral:install:remove:other-manual",
            "apt:collateral:install:remove:second-manual",
        }

    @pytest.mark.asyncio
    async def test_two_late_installs_are_asked_about_once_and_before_the_first_install(self) -> None:
        """D65 — one question over both, put before any package transaction has happened."""
        context, _source, target = _two_late_installs_context()
        job = AptSyncJob(context)
        reviewer = _CommandCountingReviewer(
            {
                **_APPROVE_PKG_A,
                "apt:package:pkg-b": Decision.APPLY,
                "apt:collateral:install:remove:other-manual": Decision.APPLY,
            },
            target,
        )
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert _collateral_entry_ids(reviewer) == [set(), {"apt:collateral:install:remove:other-manual"}]
        assert len(real_installs(target)) == 2
        commands_at_answer = all_calls(target)[: reviewer.commands_before[-1]]
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands_at_answer)


def _plan_time_and_late_collateral_context() -> tuple[JobContext, MagicMock, MagicMock]:
    """One protected package, `other-manual`, that two approved installs would each take:
    `pkg-c`, which the target's apt can already resolve, and `pkg-a`, which only becomes
    resolvable once this run has written `foo.sources`.

    So the same consequence — install / remove / `other-manual` — is reachable from the
    plan-time question and from the late one, which is the only shape in which "was this
    already answered?" means anything.
    """
    context, source, target = _repo_context(
        source_responses=foo_source_responses(
            **{
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-c\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-c\t1.0\nother-manual\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://example.com")
                    + _policy_block("pkg-c", "https://example.com")
                    + _policy_block("other-manual", "https://example.com"),
                    "",
                ),
            }
        )
    )
    target.run_command = AsyncMock(
        side_effect=respond_with_policy_sequence(
            {
                "echo $HOME": CommandResult(0, "/home/target-user", ""),
                "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                "test -f": CommandResult(1, "", ""),
                "--no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\nRemv other-manual [1.0]\n", ""),
                "--no-install-recommends pkg-c": CommandResult(0, "Inst pkg-c (1.0)\nRemv other-manual [1.0]\n", ""),
            },
            [
                CommandResult(
                    0,
                    _no_candidate("pkg-a") + target_offers("pkg-c", origin="https://example.com"),
                    "",
                ),
                CommandResult(0, target_offers("pkg-a", "pkg-c", origin="https://example.com"), ""),
            ],
        )
    )
    return context, source, target


class TestAConsequenceAlreadyAnsweredAtPlanTime:
    """`PKG-FR-COLLATERAL-ATTRIBUTION`: the id is the consequence, so what the late round owes
    turns on which answer the plan-time question got — not on whether it was asked.
    """

    @pytest.mark.asyncio
    async def test_a_go_ahead_given_at_plan_time_is_not_asked_for_again(self) -> None:
        """D57 — the earlier answer covers this cause too: the late round finds the same
        consequence, says nothing, and both installs run.
        """
        context, _source, target = _plan_time_and_late_collateral_context()
        job = AptSyncJob(context)
        reviewer = CountingReviewer(
            {
                **_APPROVE_PKG_A,
                "apt:package:pkg-c": Decision.APPLY,
                "apt:collateral:install:remove:other-manual": Decision.APPLY,
            }
        )
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert _collateral_entry_ids(reviewer) == [{"apt:collateral:install:remove:other-manual"}]
        assert len(real_installs(target)) == 2

    @pytest.mark.asyncio
    async def test_a_decline_given_at_plan_time_is_asked_again_about_the_other_changes(self) -> None:
        """D58 — that answer cancelled the changes it was about, and `pkg-a`'s install is a
        different change: it gets its own question rather than being withheld silently.
        """
        context, _source, target = _plan_time_and_late_collateral_context()
        job = AptSyncJob(context)
        reviewer = CountingReviewer(
            {
                **_APPROVE_PKG_A,
                "apt:package:pkg-c": Decision.APPLY,
                "apt:collateral:install:remove:other-manual": Decision.SKIP_ONCE,
            }
        )
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert _collateral_entry_ids(reviewer) == [
            {"apt:collateral:install:remove:other-manual"},
            {"apt:collateral:install:remove:other-manual"},
        ]
        assert real_installs(target) == []
