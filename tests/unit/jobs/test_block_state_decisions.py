"""Skip-always durability for block-state items (#208 D3, ADR-020 D-08/D-08a).

A "skip always" recorded against a hold/mask item lands in the decision file of the
machine that HOLDS it (INSTALL/CHANGE -> source, REMOVE -> target) and must never be
re-emitted afterwards — least of all in the add direction, which comes back
default-checked and so would be re-applied by a bulk accept.

Every case runs TWO rounds against the same stubbed state: round 1 records the decision
and round 2 replays the exact file round 1 wrote back through the decision-file `cat`,
then asserts the item is absent from both `plan.diffs` and every review group. All
executor interactions are mocked; no real apt/snap/flatpak commands run.
"""

from __future__ import annotations

import shlex
from unittest.mock import MagicMock

import pytest

from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.flatpak_sync import FlatpakSyncJob
from pcswitcher.jobs.packages.items import ItemClass
from pcswitcher.jobs.packages.review import Decision, ReviewOutcome
from pcswitcher.jobs.packages.sync_core import PackagePlan, PackageSyncJob
from pcswitcher.jobs.snap_sync import SnapSyncJob
from pcswitcher.models import CommandResult
from tests.unit.jobs.apt.helpers import all_calls, installed_on_target, make_context, sha256_line

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
        """H113 — three items across three directions and two machines, all declined for
        this run.
        """
        source_responses = {
            "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            "apt-mark showmanual": CommandResult(0, "", ""),
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

        assert {"apt:hold:pkg-a", "apt:config:99recommends", "apt:source:vendor.list"} <= {
            diff.item_id for diff in plan.diffs
        }
        job.accept_review(
            plan,
            ReviewOutcome(decisions={diff.item_id: Decision.SKIP_ONCE for diff in plan.diffs}, was_interactive=True),
        )
        await job.apply()

        assert not wrote_decision_file(source)
        assert not wrote_decision_file(target)


class TestAptHoldDecisions:
    """`apt:hold:<name>` — recorded on the source for a hold, the target for an unhold."""

    @pytest.mark.asyncio
    async def test_declined_hold_is_recorded_on_source_and_never_re_offered(self) -> None:
        """H121, H127 — a marked apt hold is recorded on the machine that holds it and never diffed again."""
        source_responses = {"apt-mark showhold": CommandResult(0, "pkg-a\n", "")}
        target_responses = {"apt-mark showhold": NO_HOLDS}

        context, source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        await record_skip_always(AptSyncJob(context), "apt:hold:pkg-a")
        assert wrote_decision_file(source)
        assert not wrote_decision_file(target)
        recorded = recorded_decision_file(source)
        assert "apt:hold:pkg-a" in recorded

        context, _source, _target = make_context(
            source_responses={**source_responses, decision_cat("apt"): CommandResult(0, recorded, "")},
            target_responses=target_responses,
        )
        plan = await AptSyncJob(context).plan()

        assert "apt:hold:pkg-a" not in {diff.item_id for diff in plan.diffs}
        assert "apt:hold:pkg-a" not in review_item_ids(plan)

    @pytest.mark.asyncio
    async def test_declined_unhold_is_recorded_on_target_and_never_re_offered(self) -> None:
        """B43, H121, H127 — a marked unhold is recorded on the machine that holds the hold, and never diffed again."""
        source_responses = {"apt-mark showhold": NO_HOLDS}
        # The hold is a real one: pkg-a is installed on the target. A hold naming a package
        # the target lacks is a different item entirely (`PKG-FR-APT-HOLD-VERSION`).
        target_responses = {
            "apt-mark showhold": CommandResult(0, "pkg-a\n", ""),
            "db:Status-Status": installed_on_target("pkg-a"),
        }

        context, source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        await record_skip_always(AptSyncJob(context), "apt:hold:pkg-a")
        assert wrote_decision_file(target)
        assert not wrote_decision_file(source)
        recorded = recorded_decision_file(target)

        context, _source, _target = make_context(
            source_responses=source_responses,
            target_responses={**target_responses, decision_cat("apt"): CommandResult(0, recorded, "")},
        )
        plan = await AptSyncJob(context).plan()

        assert "apt:hold:pkg-a" not in {diff.item_id for diff in plan.diffs}
        assert "apt:hold:pkg-a" not in review_item_ids(plan)

    @pytest.mark.asyncio
    async def test_recorded_hold_is_read_back_from_the_machine_that_holds_it_only(self) -> None:
        """B44 — The decision is machine-local (D-08a): the same file on the WRONG machine must
        not silence the diff, or the read path would be looking at the wrong end."""
        source_responses = {"apt-mark showhold": CommandResult(0, "pkg-a\n", "")}
        context, source, _target = make_context(
            source_responses=source_responses, target_responses={"apt-mark showhold": NO_HOLDS}
        )
        await record_skip_always(AptSyncJob(context), "apt:hold:pkg-a")
        recorded = recorded_decision_file(source)

        context, _source, _target = make_context(
            source_responses=source_responses,
            target_responses={"apt-mark showhold": NO_HOLDS, decision_cat("apt"): CommandResult(0, recorded, "")},
        )
        plan = await AptSyncJob(context).plan()

        assert "apt:hold:pkg-a" in {diff.item_id for diff in plan.diffs}


class TestAptHeldPackageSuppression:
    """The target hold SET keeps suppressing a held package's own install/upgrade action
    (`diff_apt_packages`), whatever is recorded — which is why inertness is filtered on
    the resulting `ItemDiff`s and never on the hold-name sets feeding the diff."""

    @staticmethod
    def _held_package_responses() -> tuple[dict[str, CommandResult], dict[str, CommandResult]]:
        """pkg-b: newer on the source, held on the target and not on the source — so the
        upgrade must stay suppressed while the unhold is offered."""
        source_responses = {
            "apt-mark showhold": NO_HOLDS,
            "apt-mark showmanual": CommandResult(0, "pkg-b\n", ""),
            "dpkg-query": CommandResult(0, "pkg-b\t2.0\n", ""),
        }
        target_responses = {
            "apt-mark showhold": CommandResult(0, "pkg-b\n", ""),
            "apt-mark showmanual": CommandResult(0, "pkg-b\n", ""),
            "dpkg-query": CommandResult(0, "pkg-b\t1.0\n", ""),
        }
        return source_responses, target_responses

    @pytest.mark.asyncio
    async def test_declined_unhold_does_not_re_propose_the_held_packages_upgrade(self) -> None:
        """B45."""
        source_responses, target_responses = self._held_package_responses()

        context, _source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        await record_skip_always(AptSyncJob(context), "apt:hold:pkg-b")
        recorded = recorded_decision_file(target)

        context, _source, _target = make_context(
            source_responses=source_responses,
            target_responses={**target_responses, decision_cat("apt"): CommandResult(0, recorded, "")},
        )
        plan = await AptSyncJob(context).plan()

        item_ids = {diff.item_id for diff in plan.diffs}
        assert "apt:hold:pkg-b" not in item_ids
        assert "apt:package:pkg-b" not in item_ids

    @pytest.mark.asyncio
    async def test_unrelated_recorded_decision_leaves_the_hold_set_intact(self) -> None:
        """B46, N4."""
        source_responses, target_responses = self._held_package_responses()
        # pkg-c is held on the target too; its unhold is the one declined.
        source_responses = {**source_responses, "apt-mark showmanual": CommandResult(0, "pkg-b\n", "")}
        target_responses = {**target_responses, "apt-mark showhold": CommandResult(0, "pkg-b\npkg-c\n", "")}

        context, _source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        await record_skip_always(AptSyncJob(context), "apt:hold:pkg-c")
        recorded = recorded_decision_file(target)

        context, _source, _target = make_context(
            source_responses=source_responses,
            target_responses={**target_responses, decision_cat("apt"): CommandResult(0, recorded, "")},
        )
        plan = await AptSyncJob(context).plan()

        item_ids = {diff.item_id for diff in plan.diffs}
        assert "apt:hold:pkg-c" not in item_ids
        assert "apt:hold:pkg-b" in item_ids
        assert "apt:package:pkg-b" not in item_ids


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


class TestSnapHoldDecisions:
    """`snap:hold:<name>` — a distinct identity from the snap's own `snap:<name>`, so
    only a filter on the diff can match it."""

    @pytest.mark.asyncio
    async def test_declined_hold_is_recorded_on_source_and_never_re_offered(self) -> None:
        """H122 — a marked snap hold lands in snap's own decision file on the machine that holds it."""
        source_responses = {"snap list --all": CommandResult(0, SNAP_ALPHA_HELD, "")}
        target_responses = {"snap list --all": CommandResult(0, SNAP_ALPHA_UNHELD, "")}

        context, source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        await record_skip_always(SnapSyncJob(context), "snap:hold:alpha")
        assert wrote_decision_file(source)
        assert not wrote_decision_file(target)
        recorded = recorded_decision_file(source)
        assert "snap:hold:alpha" in recorded

        context, _source, _target = make_context(
            source_responses={**source_responses, decision_cat("snap"): CommandResult(0, recorded, "")},
            target_responses=target_responses,
        )
        plan = await SnapSyncJob(context).plan()

        assert "snap:hold:alpha" not in {diff.item_id for diff in plan.diffs}
        assert "snap:hold:alpha" not in review_item_ids(plan)

    @pytest.mark.asyncio
    async def test_declined_unhold_is_recorded_on_target_and_never_re_offered(self) -> None:
        """E69, H122 — a marked snap unhold lands on the machine that holds the hold."""
        source_responses = {"snap list --all": CommandResult(0, SNAP_ALPHA_UNHELD, "")}
        target_responses = {"snap list --all": CommandResult(0, SNAP_ALPHA_HELD, "")}

        context, source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        await record_skip_always(SnapSyncJob(context), "snap:hold:alpha")
        assert wrote_decision_file(target)
        assert not wrote_decision_file(source)
        recorded = recorded_decision_file(target)

        context, _source, _target = make_context(
            source_responses=source_responses,
            target_responses={**target_responses, decision_cat("snap"): CommandResult(0, recorded, "")},
        )
        plan = await SnapSyncJob(context).plan()

        assert "snap:hold:alpha" not in {diff.item_id for diff in plan.diffs}
        assert "snap:hold:alpha" not in review_item_ids(plan)

    @pytest.mark.asyncio
    async def test_recorded_hold_does_not_silence_the_snaps_own_presence_diff(self) -> None:
        """E70, H128 — A hold decision is about the hold, not the snap: `snap:alpha` must still be
        proposed for install when the target lacks it."""
        source_responses = {"snap list --all": CommandResult(0, SNAP_ALPHA_HELD, "")}
        context, source, _target = make_context(
            source_responses=source_responses,
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        await record_skip_always(SnapSyncJob(context), "snap:hold:alpha")
        recorded = recorded_decision_file(source)

        context, _source, _target = make_context(
            source_responses={**source_responses, decision_cat("snap"): CommandResult(0, recorded, "")},
            target_responses={"snap list --all": CommandResult(0, "No snaps are installed yet.\n", "")},
        )
        plan = await SnapSyncJob(context).plan()

        item_ids = {diff.item_id for diff in plan.diffs}
        assert "snap:hold:alpha" not in item_ids
        assert "snap:alpha" in item_ids


class TestFlatpakMaskDecisions:
    """Positive control: a mask is a real `FlatpakMaskItem` carrying its own id, so it was
    already filtered on the way in — and must stay filtered."""

    @pytest.mark.asyncio
    async def test_declined_mask_is_recorded_on_source_and_never_re_offered(self) -> None:
        """H123, N6 — a marked flatpak mask lands in flatpak's own decision file on the machine that holds it."""
        mask_id = "flatpak:mask:user:org.example.Blocked"
        source_responses = {"flatpak --user mask": CommandResult(0, "  org.example.Blocked\n", "")}

        context, source, target = make_context(source_responses=source_responses)
        await record_skip_always(FlatpakSyncJob(context), mask_id)
        assert wrote_decision_file(source)
        assert not wrote_decision_file(target)
        recorded = recorded_decision_file(source)

        context, _source, _target = make_context(
            source_responses={**source_responses, decision_cat("flatpak"): CommandResult(0, recorded, "")}
        )
        plan = await FlatpakSyncJob(context).plan()

        assert not [diff for diff in plan.diffs if diff.item_class == ItemClass.FLATPAK_MASK]
        assert mask_id not in review_item_ids(plan)

    @pytest.mark.asyncio
    async def test_declined_unmask_is_recorded_on_target_and_never_re_offered(self) -> None:
        """F131, H123, N6 — a marked flatpak unmask lands on the machine that holds the mask."""
        mask_id = "flatpak:mask:system:org.example.Blocked"
        target_responses = {"flatpak --system mask": CommandResult(0, "  org.example.Blocked\n", "")}

        context, source, target = make_context(target_responses=target_responses)
        await record_skip_always(FlatpakSyncJob(context), mask_id)
        assert wrote_decision_file(target)
        assert not wrote_decision_file(source)
        recorded = recorded_decision_file(target)

        context, _source, _target = make_context(
            target_responses={**target_responses, decision_cat("flatpak"): CommandResult(0, recorded, "")}
        )
        plan = await FlatpakSyncJob(context).plan()

        assert not [diff for diff in plan.diffs if diff.item_class == ItemClass.FLATPAK_MASK]
        assert mask_id not in review_item_ids(plan)
