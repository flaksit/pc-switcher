"""What a decision file may and may not hold (ADR-020 D-08/D-08a).

Two halves. The first is what CAN be recorded and must then stay inert: `apt:config:`,
whose identity is a directory digest rather than an input item, and whose overwrite
direction is where the holder rule stops following from the run's own direction — the
file is on BOTH machines, the mark keeps the target's copy, and the next run may be
launched the other way round.

The second is what can NEVER be recorded: a repository, a pin, a signing key, a snap's
revision, and every block — an apt hold, a snap refresh hold, a flatpak mask
(`PKG-FR-NO-MARK-ON-ORIGIN`, `PKG-FR-NO-MARK-ON-SNAP-REVISION`, `PKG-FR-BLOCKS-DERIVED`).
No screen offers the permanent answer for any of them, but "no entry can exist" is a
property of the model rather than of one prompt's wiring, so each is forced through
`SKIP_ALWAYS` here and the decision files are asserted untouched. A block gets the
converse test too: an entry naming one, left by an older version of the tool or written by
hand, must not silence a replication nobody declined.

A recordable case runs TWO rounds against the same stubbed state: round 1 records the
decision and round 2 replays the exact file round 1 wrote back through the decision-file
`cat`, then asserts the item is absent from both `plan.diffs` and every review group. All
executor interactions are mocked; no real apt/snap/flatpak commands run.
"""

from __future__ import annotations

import contextlib
import shlex
from unittest.mock import MagicMock

import pytest

from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.flatpak_sync import FlatpakSyncJob
from pcswitcher.jobs.packages.review import Decision, ReviewOutcome
from pcswitcher.jobs.packages.sync_core import PackageItemFailures, PackagePlan, PackageSyncJob
from pcswitcher.jobs.snap_sync import SnapSyncJob
from pcswitcher.models import CommandResult
from tests.unit.jobs.apt.helpers import (
    all_calls,
    decision_file,
    differing_repo_context,
    make_context,
    sha256_line,
)

_SNAP_HEADER = "Name      Version    Rev    Tracking        Publisher    Notes\n"
SNAP_ALPHA_HELD = _SNAP_HEADER + "alpha     1.0        10     latest/stable   pub✓         held\n"
SNAP_ALPHA_UNHELD = _SNAP_HEADER + "alpha     1.0        10     latest/stable   pub✓         -\n"

# A deb822 repo file naming the keyring it is signed by, so the source item captures a
# resolvable `keyring_refs` entry and lands as a plain install rather than a dangling one.
DEB822_FOO = (
    "Types: deb\nURIs: https://example.com\nSuites: stable\nComponents: main\nSigned-By: /etc/apt/keyrings/foo.gpg\n"
)

# `apt-mark showhold` on a machine with no holds, so a hold stub only has to name the
# machine that DOES hold something.
NO_HOLDS = CommandResult(0, "", "")


def decision_cat(manager: str) -> str:
    """The `DecisionFile.load` command for `manager`, as a `respond_to` match pattern."""
    return f"cat ~/.config/pc-switcher/{manager}.decisions.yaml"


def recorded_decision_file(executor: MagicMock) -> str:
    """The decision-file content this executor was asked to write.

    `DecisionFile.record` passes the whole serialised file as one shlex-quoted argument
    to `printf '%s'`, so splitting the command recovers the exact bytes — round 2 then
    replays what round 1 actually wrote, not a hand-built copy of the YAML shape.
    """
    cmd = next(c for c in all_calls(executor) if ".decisions.yaml.pcswitcher-tmp" in c)
    tokens = shlex.split(cmd)
    return tokens[tokens.index("printf") + 2]


def wrote_decision_file(executor: MagicMock) -> bool:
    return any(".decisions.yaml.pcswitcher-tmp" in cmd for cmd in all_calls(executor))


def review_item_ids(plan: PackagePlan) -> set[str]:
    return {entry.item_id for group in plan.groups for entry in group.entries}


async def record_skip_always(job: PackageSyncJob, item_id: str) -> PackagePlan:
    """Round 1: plan, decide SKIP_ALWAYS on `item_id` (SKIP_ONCE on everything else so
    nothing converges), apply. Asserts the item was actually offered."""
    plan = await job.plan()
    assert item_id in {diff.item_id for diff in plan.diffs}, f"{item_id} was never diffed"

    decisions = {
        diff.item_id: (Decision.SKIP_ALWAYS if diff.item_id == item_id else Decision.SKIP_ONCE) for diff in plan.diffs
    }
    job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))
    await job.apply()
    return plan


class TestARunOfPureDeclines:
    """`PKG-FR-SKIP-ONCE`: declining for this run records nothing, so a run whose EVERY
    answer was a decline must leave both machines' decision files untouched — not merely
    the machine that does not hold the item.

    The other tests here always mark one item, so each of them proves only that the OTHER
    machine wrote nothing. This one removes the mark entirely.
    """

    @pytest.mark.asyncio
    async def test_a_run_whose_every_answer_is_a_decline_writes_no_decision_file(self) -> None:
        """H113 — every item the run puts to the user, across three directions and two
        machines, declined for this run.
        """
        source_responses = {
            "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
            "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99recommends"), ""),
        }
        target_responses = {
            "apt-mark showhold": NO_HOLDS,
            "apt-mark showmanual": CommandResult(0, "", ""),
            "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "vendor.list"), ""),
            "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, "deb https://vendor.example.com x y\n", ""),
        }

        context, source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = AptSyncJob(context)
        plan = await job.plan()

        assert {"apt:package:pkg-a", "apt:config:99recommends", "apt:source:vendor.list"} <= {
            diff.item_id for diff in plan.diffs
        }
        job.accept_review(
            plan,
            ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ONCE for diff in plan.diffs}, was_interactive=True),
        )
        await job.apply()

        assert not wrote_decision_file(source)
        assert not wrote_decision_file(target)


class TestABlockCanNeverBeRecorded:
    """`PKG-FR-BLOCKS-DERIVED`: an apt hold, a snap refresh hold and a flatpak mask reach no
    review group, so no answer about one exists to record — and an entry naming one, however
    it got there, must not silence the replication.

    Both directions matter. A recorded entry that silenced a block would stop a hold or a
    mask travelling on the strength of a decision nobody made; a recordable block would
    leave the two machines' files disagreeing about software neither would raise again.
    """

    @staticmethod
    async def _force_permanent(job: PackageSyncJob, item_id: str) -> None:
        """Decide `SKIP_ALWAYS` on `item_id` the way the automation hook can, and apply."""
        plan = await job.plan()
        assert item_id in {diff.item_id for diff in plan.diffs}, f"{item_id} was never diffed"
        job.accept_review(
            plan,
            ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS for diff in plan.diffs}, was_interactive=True),
        )
        with contextlib.suppress(PackageItemFailures):
            await job.apply()

    @pytest.mark.asyncio
    async def test_a_forced_permanent_answer_on_an_apt_hold_records_nothing(self) -> None:
        """B42, B43, H121, N4 — neither the add nor the removal direction can reach a file."""
        for source_holds, target_holds in (("pkg-a\n", ""), ("", "pkg-a\n")):
            context, source, target = make_context(
                source_responses={
                    "apt-mark showhold": CommandResult(0, source_holds, ""),
                    "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                    "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                },
                target_responses={
                    "apt-mark showhold": CommandResult(0, target_holds, ""),
                    "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                    "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                },
            )
            await self._force_permanent(AptSyncJob(context), "apt:hold:pkg-a")

            assert not wrote_decision_file(source)
            assert not wrote_decision_file(target)

    @pytest.mark.asyncio
    async def test_a_recorded_apt_hold_decision_does_not_silence_the_replication(self) -> None:
        """B44, H127 — an entry naming a hold is not an answer anybody gave, so the hold
        still replicates."""
        recorded = decision_file("apt:hold:pkg-a")
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                decision_cat("apt"): CommandResult(0, recorded, ""),
            },
            target_responses={
                "apt-mark showhold": NO_HOLDS,
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert "apt:hold:pkg-a" in {diff.item_id for diff in plan.diffs}

    @pytest.mark.asyncio
    async def test_a_forced_permanent_answer_on_a_snap_hold_records_nothing(self) -> None:
        """E68, E69, H122, N5 — the snap analogue, in both directions."""
        for source_list, target_list in (
            (SNAP_ALPHA_HELD, SNAP_ALPHA_UNHELD),
            (SNAP_ALPHA_UNHELD, SNAP_ALPHA_HELD),
        ):
            context, source, target = make_context(
                source_responses={"snap list --all": CommandResult(0, source_list, "")},
                target_responses={"snap list --all": CommandResult(0, target_list, "")},
            )
            await self._force_permanent(SnapSyncJob(context), "snap:hold:alpha")

            assert not wrote_decision_file(source)
            assert not wrote_decision_file(target)

    @pytest.mark.asyncio
    async def test_a_recorded_snap_hold_decision_does_not_silence_the_replication(self) -> None:
        """E70, H128 — and it silences nothing about the snap itself either."""
        recorded = decision_file("snap:hold:alpha")
        context, _source, _target = make_context(
            source_responses={
                "snap list --all": CommandResult(0, SNAP_ALPHA_HELD, ""),
                decision_cat("snap"): CommandResult(0, recorded, ""),
            },
            target_responses={"snap list --all": CommandResult(0, SNAP_ALPHA_UNHELD, "")},
        )

        plan = await SnapSyncJob(context).plan()

        assert "snap:hold:alpha" in {diff.item_id for diff in plan.diffs}

    @pytest.mark.asyncio
    async def test_a_forced_permanent_answer_on_a_flatpak_mask_records_nothing(self) -> None:
        """F130, H123, N6 — the mask analogue, in both directions."""
        for responses in (
            {"source_responses": {"flatpak --user mask": CommandResult(0, "  org.example.Blocked\n", "")}},
            {"target_responses": {"flatpak --system mask": CommandResult(0, "  org.example.Blocked\n", "")}},
        ):
            scope = "user" if "source_responses" in responses else "system"
            context, source, target = make_context(**responses)  # pyright: ignore[reportArgumentType]
            await self._force_permanent(FlatpakSyncJob(context), f"flatpak:mask:{scope}:org.example.Blocked")

            assert not wrote_decision_file(source)
            assert not wrote_decision_file(target)


class TestAptRepoItemDecisions:
    """`apt:config:` — digest-derived like the block-state items, so it reaches `plan()`
    with no input item to filter and depends on the same post-diff pass.

    Apt config is the ONE `/etc/apt` class that keeps the full three-way decision and the
    machine-local registry, in all three directions (ADR-020 D-37): a proxy or a
    `no-install-recommends` policy is a standing machine-local preference no approved
    package implies. Repositories and pins are mechanism and have no registry at all, and a
    signing key is not even an item — none of the three can ever be offered, declined, or
    recorded.
    """

    @pytest.mark.asyncio
    async def test_declined_config_install_is_recorded_on_source_and_never_re_offered(self) -> None:
        """C124, H53, H124, H127 —
        an `/etc/apt/apt.conf.d` file is markable, recorded on its holder and never re-offered."""
        source_responses = {
            "apt-mark showmanual": CommandResult(0, "", ""),
            "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99recommends"), ""),
        }

        context, source, target = make_context(source_responses=source_responses)
        await record_skip_always(AptSyncJob(context), "apt:config:99recommends")
        assert wrote_decision_file(source)
        assert not wrote_decision_file(target)
        recorded = recorded_decision_file(source)

        context, _source, _target = make_context(
            source_responses={**source_responses, decision_cat("apt"): CommandResult(0, recorded, "")}
        )
        plan = await AptSyncJob(context).plan()

        assert "apt:config:99recommends" not in {diff.item_id for diff in plan.diffs}
        assert "apt:config:99recommends" not in review_item_ids(plan)

    @pytest.mark.asyncio
    async def test_no_repository_or_pin_id_can_reach_a_decision_file(self) -> None:
        """C61, C115, C125, H116, H139 —
        Rulings 5 and 12: a repository or pin DELETION takes two answers, so there is no
        third state to persist — and the model says "no registry entry", not "the prompt
        happens not to offer one". Asserted the hard way, with `SKIP_ALWAYS` forced onto
        every diff the plan produced, which is what an automation hook or a hand-built
        outcome could do.

        The `apt:config:` line in the same run must still be recorded: it is the one
        `/etc/apt` class that keeps the registry (D-37).
        """
        target_responses = {
            "apt-mark showmanual": CommandResult(0, "", ""),
            "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "vendor.list"), ""),
            "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, "deb https://vendor.example.com x y\n", ""),
            "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "vendor-pin"), ""),
            "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99extra"), ""),
        }
        context, source, target = make_context(target_responses=target_responses)
        job = AptSyncJob(context)
        plan = await job.plan()

        offered = {diff.item_id for diff in plan.diffs}
        assert {"apt:source:vendor.list", "apt:pin:vendor-pin", "apt:config:99extra"} <= offered

        job.accept_review(
            plan,
            ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS for diff in plan.diffs}, was_interactive=True),
        )
        await job.apply()

        assert not wrote_decision_file(source)
        recorded = recorded_decision_file(target)
        assert "apt:config:99extra" in recorded
        assert "apt:source:" not in recorded
        assert "apt:pin:" not in recorded

    @pytest.mark.asyncio
    async def test_no_repository_conflict_answer_can_reach_a_decision_file(self) -> None:
        """C33 — `PKG-FR-REPO-CONFLICT` says the answer "MUST NOT" be recorded, in EITHER
        direction: overwriting is a one-off, and skipping leaves the two machines disagreeing
        about where software comes from, which the next sync has to raise again.

        Asserted the hard way like the row above it, with `SKIP_ALWAYS` forced onto the
        conflict entry through a hand-built outcome — the shape an automation hook could
        produce, and the only one the interactive prompt cannot.
        """
        context, source, target = differing_repo_context(recorded=decision_file("apt:package:curl"))
        job = AptSyncJob(context)
        plan = await job.plan()

        approvals = {diff.item_id: Decision.APPLY for diff in plan.diffs}
        second = await job.plan_second_round(plan, ReviewOutcome(decisions=approvals, was_interactive=True))
        conflicts = {
            entry.item_id
            for group in second.groups
            for entry in group.entries
            if entry.item_id.startswith("apt:conflict:")
        }
        assert conflicts == {"apt:conflict:vendor.list"}

        job.accept_review(
            PackagePlan(manager=plan.manager, diffs=second.diffs, groups=(*plan.groups, *second.groups)),
            ReviewOutcome(
                decisions={**approvals, **dict.fromkeys(conflicts, Decision.SKIP_ALWAYS)}, was_interactive=True
            ),
        )
        # Skipping the conflict fails the package whose origin needed the file (C35); the
        # decision records are written before any of that, which is what this is about.
        with contextlib.suppress(PackageItemFailures):
            await job.apply()

        # The conflict was this run's only permanent answer, so "nothing is recorded" is
        # literally that neither machine's decision file was touched — and with nothing
        # recorded, the next sync finds the same two versions and asks again.
        assert not wrote_decision_file(source)
        assert not wrote_decision_file(target)
        assert not any("apt:conflict:" in cmd for cmd in all_calls(source) + all_calls(target))

    @pytest.mark.asyncio
    async def test_a_signing_key_is_never_offered_and_so_can_never_be_recorded(self) -> None:
        """H140 — `orphan.gpg` exists only on the target and no repository references it — the
        strongest candidate a key removal could ever have. It reaches neither `plan.diffs`
        nor a review group, so there is nothing for the user to decline and nothing
        `_record_permanent_skips` could ever write.
        """
        context, source, target = make_context(
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k9", "orphan.gpg"), ""),
            }
        )
        job = AptSyncJob(context)
        plan = await job.plan()

        assert not any(diff.item_id.startswith("apt:key:") for diff in plan.diffs)
        assert not any(item_id.startswith("apt:key:") for item_id in review_item_ids(plan))

        job.accept_review(
            plan,
            ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ALWAYS for diff in plan.diffs}, was_interactive=True),
        )
        await job.apply()

        assert not wrote_decision_file(source)
        assert not wrote_decision_file(target)


# One `apt.conf.d` filename both machines have, with different bytes: the C175 shape.
_CONFIG_ON_BOTH = {
    "apt-mark showmanual": CommandResult(0, "", ""),
    "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "20update"), ""),
}
_CONFIG_ON_BOTH_OTHER_BYTES = {
    "apt-mark showmanual": CommandResult(0, "", ""),
    "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c2", "20update"), ""),
}


class TestAptConfigOverwriteDecision:
    """The overwrite direction of `apt:config:` — the one apt item both machines have.

    An install and a deletion are on one machine each, so the run's own direction names
    their holder. A file both machines have and disagree about is not: the answer keeps the
    TARGET's copy, and the next run may be launched the other way round, which is the case
    the whole `_mark_holders` pair exists for.
    """

    @pytest.mark.asyncio
    async def test_a_marked_overwrite_is_recorded_on_the_target_and_inert_in_both_directions(self) -> None:
        """C175, C178, N24 — the mark keeps the target's copy, so it lands there; and because the
        roles swap with the direction the next run is launched in, the same mark must
        silence the file whichever end it is read from.
        """
        context, source, target = make_context(
            source_responses=_CONFIG_ON_BOTH, target_responses=_CONFIG_ON_BOTH_OTHER_BYTES
        )
        await record_skip_always(AptSyncJob(context), "apt:config:20update")
        assert wrote_decision_file(target)
        assert not wrote_decision_file(source)
        recorded = recorded_decision_file(target)

        # Run 2, same direction: the mark is on the target.
        context, _source, _target = make_context(
            source_responses=_CONFIG_ON_BOTH,
            target_responses={**_CONFIG_ON_BOTH_OTHER_BYTES, decision_cat("apt"): CommandResult(0, recorded, "")},
        )
        plan = await AptSyncJob(context).plan()
        assert "apt:config:20update" not in {diff.item_id for diff in plan.diffs}
        assert "apt:config:20update" not in review_item_ids(plan)

        # Run 3, launched from the machine holding the mark: it is now the SOURCE.
        context, _source, _target = make_context(
            source_responses={**_CONFIG_ON_BOTH_OTHER_BYTES, decision_cat("apt"): CommandResult(0, recorded, "")},
            target_responses=_CONFIG_ON_BOTH,
        )
        plan = await AptSyncJob(context).plan()
        assert "apt:config:20update" not in {diff.item_id for diff in plan.diffs}
        assert "apt:config:20update" not in review_item_ids(plan)
