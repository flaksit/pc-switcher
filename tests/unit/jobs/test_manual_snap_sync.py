"""Unit tests for `ManualSnapSyncJob`: the sideloaded-snap half of what no package manager
can reproduce (D-18) — the `x`-revision detection, the seam with `snap_sync`, what the
target counts as already holding, and this job's validation.

The shared half every unreproducible job inherits is covered in `test_unreproducible_jobs.py`.
All executor interactions are mocked; no real snap commands run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs.manual_snap_sync import ManualSnapSyncJob
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import Decision, ReviewOutcome
from pcswitcher.jobs.packages.sync_core import PackagePlan
from pcswitcher.jobs.snap_sync import SnapSyncJob
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator
from tests.unit.jobs.unreproducible_harness import (
    Answer,
    FakeReviewer,
    all_calls,
    make_context,
)

# The `snap list --all` both snap jobs read, matched by the command itself.
SNAP_LIST = "snap list --all"

# A registry holding one snippet for the sideloaded `mytool`, keyed on the id this job
# builds — the snap's name, with no revision in it.
MYTOOL_REGISTRY_YAML = (
    "snippets:\n"
    "  unreproducible:snap-sideload:mytool:\n"
    "    label: mytool (sideloaded snap, revision x1)\n"
    "    body: sudo snap install --dangerous /tmp/mytool.snap\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: laptop\n"
)


def snap_list(*rows: tuple[str, str, str]) -> CommandResult:
    """What `snap list --all` prints for `(name, revision, channel)` rows.

    The revision decides everything here: `x`-prefixed is a sideload, a plain integer is a
    store snap. Columns are rendered in snapd's own header order so the parser reads them
    by name, as it does against the real binary.
    """
    header = "Name  Version  Rev  Tracking  Publisher  Notes\n"
    body = "".join(f"{name}  1.0  {revision}  {channel}  canonical  -\n" for name, revision, channel in rows)
    return CommandResult(0, header + body, "")


def sideload(name: str, revision: str = "x1") -> tuple[str, str, str]:
    """A sideloaded row: a store-less revision, and no channel to track."""
    return (name, revision, "-")


def store_snap(name: str, revision: str = "42") -> tuple[str, str, str]:
    return (name, revision, "latest/stable")


class TestSideloadDetection:
    """The `x`-revision scan: a snap whose bytes came from a local file becomes an
    UNREPRODUCIBLE diff (D-18, `PKG-FR-SNAP-SIDELOAD`), and a store snap does not.
    """

    @pytest.mark.asyncio
    async def test_a_sideloaded_snap_is_presented_as_an_item(self) -> None:
        """G122 — a snap snapd reports at an `x`-prefixed revision is presented as an item no
        package manager can reproduce."""
        context, _source, _target = make_context(source_responses={SNAP_LIST: snap_list(sideload("mytool"))})

        plan = await ManualSnapSyncJob(context).plan()

        assert len(plan.diffs) == 1
        assert plan.diffs[0].item_id == "unreproducible:snap-sideload:mytool"
        assert plan.diffs[0].item_class == ItemClass.UNREPRODUCIBLE
        assert plan.diffs[0].diff_class == DiffClass.UNREPRODUCIBLE
        assert plan.diffs[0].action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_a_store_snap_is_not_presented(self) -> None:
        """G123 — a plain integer revision is a revision the store can serve, so `snap_sync`
        converges it and this job says nothing about it."""
        context, _source, _target = make_context(
            source_responses={SNAP_LIST: snap_list(store_snap("firefox"), sideload("mytool"))}
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert [d.item_id for d in plan.diffs] == ["unreproducible:snap-sideload:mytool"]

    @pytest.mark.asyncio
    async def test_the_item_label_names_the_revision_the_source_holds(self) -> None:
        """G124 — the revision is what the user needs to recognise the build being asked
        about, so it is in the label even though it is deliberately not in the identity."""
        context, _source, _target = make_context(source_responses={SNAP_LIST: snap_list(sideload("mytool", "x3"))})

        plan = await ManualSnapSyncJob(context).plan()

        assert plan.diffs[0].label == "mytool (sideloaded snap, revision x3)"

    @pytest.mark.asyncio
    async def test_reinstalling_a_sideload_at_a_new_revision_keeps_its_identity(self) -> None:
        """G125 — the identity is the snap's NAME. A sideload reinstalled from a newer
        `.snap` file moves `x1` -> `x2`, and the snippet the user wrote still resolves: a
        revision in the id would orphan it and re-ask the same question every reinstall."""
        context, _source, _target = make_context(
            source_responses={
                SNAP_LIST: snap_list(sideload("mytool", "x2")),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, MYTOOL_REGISTRY_YAML, ""),
            }
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert plan.diffs[0].item_id == "unreproducible:snap-sideload:mytool"
        assert plan.diffs[0].action == DiffAction.INSTALL

    @pytest.mark.asyncio
    async def test_a_listing_that_did_not_answer_fails_the_job(self) -> None:
        """G126, J189 — ADR-022: snapd unreachable exits non-zero, so the read said nothing about
        any snap. Reading that as "no sideloads here" would silently drop findings that
        `snap_sync` has meanwhile withheld from its own manifests off the same predicate."""
        context, _source, _target = make_context(
            source_responses={SNAP_LIST: CommandResult(1, "", "error: cannot communicate with server\n")}
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await ManualSnapSyncJob(context).plan()

        assert SNAP_LIST in str(excinfo.value)
        assert "cannot communicate with server" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_machine_with_no_snaps_at_all_is_an_ordinary_answer(self) -> None:
        """G127 — zero snaps installed exits 0 with an empty stdout, which is a real state of
        a real machine. Unlike the `.deb` job's installed-set read, emptiness here is never
        failed on."""
        context, _source, _target = make_context(
            source_responses={SNAP_LIST: CommandResult(0, "", "No snaps are installed yet.\n")}
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_the_snap_the_job_offers_is_the_one_snap_sync_withheld(self) -> None:
        """G128 — the seam: one listing, two jobs, one predicate. `snap_sync` converges the
        store snap and withholds the sideload; this job offers the sideload and says nothing
        about the store snap. No snap is owned by both jobs or by neither."""
        listing = snap_list(store_snap("firefox"), sideload("mytool"))
        context, _source, _target = make_context(
            source_responses={SNAP_LIST: listing}, target_responses={SNAP_LIST: snap_list()}
        )

        snap_plan = await SnapSyncJob(context).plan()
        manual_plan = await ManualSnapSyncJob(context).plan()

        assert [d.item_id for d in snap_plan.diffs] == ["snap:firefox"]
        assert [d.item_id for d in manual_plan.diffs] == ["unreproducible:snap-sideload:mytool"]


class TestInertFiltering:
    """An item recorded machine-specific on the source produces no diff (D-08/D-19)."""

    @pytest.mark.asyncio
    async def test_machine_specific_item_is_filtered_before_becoming_a_diff(self) -> None:
        """G129 — a mark from an earlier run for a still-present sideload keeps it out of
        every list."""
        context, _source, _target = make_context(
            source_responses={
                SNAP_LIST: snap_list(sideload("mytool")),
                "cat ~/.config/pc-switcher/manual_snap.decisions.yaml": CommandResult(
                    0, _marks("unreproducible:snap-sideload:mytool"), ""
                ),
            }
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_snap_syncs_own_mark_on_the_same_snap_is_not_read(self) -> None:
        """G130 — a `snap:<name>` mark is `snap_sync`'s answer about converging that snap's
        revision, in that job's own file and id space, so it neither silences this job's
        finding nor is read at all (D-09: one decision file per manager)."""
        context, source, _target = make_context(
            source_responses={
                SNAP_LIST: snap_list(sideload("mytool")),
                "cat ~/.config/pc-switcher/snap.decisions.yaml": CommandResult(0, _marks("snap:mytool"), ""),
            }
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert [d.item_id for d in plan.diffs] == ["unreproducible:snap-sideload:mytool"]
        assert not [cmd for cmd in all_calls(source) if "/snap.decisions.yaml" in cmd]


class TestWhatTheTargetAlreadyHolds:
    """`PKG-FR-MANUAL-DIFF`: both machines are read and only what the target lacks is
    presented. Presence of the NAME is the whole test — no revision is ever compared (#207).
    """

    @pytest.mark.asyncio
    async def test_a_snap_the_target_has_from_the_store_counts_as_held(self) -> None:
        """G135 — the same application, from a route needing no snippet. Replaying one would
        sideload over the store copy and take it off the update path it is on."""
        context, _source, _target = make_context(
            source_responses={SNAP_LIST: snap_list(sideload("mytool"))},
            target_responses={SNAP_LIST: snap_list(store_snap("mytool"))},
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_sideload_of_the_same_name_at_another_revision_counts_as_held(self) -> None:
        """G136 — two machines' `x<N>` counters are independent install counts, not two
        builds, so nothing here could read a difference between them. The target has the
        software; it is not offered."""
        context, _source, _target = make_context(
            source_responses={SNAP_LIST: snap_list(sideload("mytool", "x1"))},
            target_responses={SNAP_LIST: snap_list(sideload("mytool", "x7"))},
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_snap_only_the_target_holds_produces_nothing(self) -> None:
        """G137 — `PKG-NG-MANUAL-REMOVE`: what the target alone has is never an item, in any
        direction."""
        context, _source, _target = make_context(
            source_responses={SNAP_LIST: snap_list()},
            target_responses={SNAP_LIST: snap_list(sideload("theirs"))},
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert plan.diffs == ()


class TestExecuteIndependentOfSnapSync:
    """The job runs on its own enable flag, independent of snap_sync (D-15/D-18)."""

    @pytest.mark.asyncio
    async def test_plan_runs_with_snap_sync_absent_from_config(self) -> None:
        """G131 — the sideload is detected with snap sync absent from the configuration: this
        job asks snapd its own question."""
        context, _source, _target = make_context(
            source_responses={SNAP_LIST: snap_list(sideload("mytool"))},
            enabled_sync_jobs={"manual_snap_sync": True, "folder_sync": True},
        )

        plan = await ManualSnapSyncJob(context).plan()

        assert [d.item_id for d in plan.diffs] == ["unreproducible:snap-sideload:mytool"]

    @pytest.mark.asyncio
    async def test_execute_runs_plan_review_apply_through_injected_reviewer(self) -> None:
        item_id = "unreproducible:snap-sideload:mytool"
        reviewer = FakeReviewer(decisions={item_id: Decision.APPLY})
        context, _source, target = make_context(
            source_responses={
                SNAP_LIST: snap_list(sideload("mytool")),
                # plan() classifies INSTALL from the SOURCE registry (D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, MYTOOL_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, MYTOOL_REGISTRY_YAML, ""),
                "bash -c 'sudo snap install --dangerous /tmp/mytool.snap'": CommandResult(0, "installed\n", ""),
            },
            reviewer=reviewer,
        )

        await ManualSnapSyncJob(context).execute()

        assert reviewer.groups_seen is not None
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1


class TestValidate:
    @pytest.mark.asyncio
    async def test_snap_unavailable_on_source_yields_validation_error(self) -> None:
        """G132, K95 — validation fails before anything runs, naming the source and the missing
        tool."""
        context, _source, _target = make_context(
            source_responses={"snap version": CommandResult(127, "", "not found")}
        )

        errors = await ManualSnapSyncJob(context).validate()

        assert any(e.host is Host.SOURCE and "snap" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_snap_unavailable_on_target_yields_validation_error(self) -> None:
        """G133, K96 — the target is read to tell what it already has, so its missing tool is
        named before the run starts rather than as a dead probe halfway through."""
        context, _source, _target = make_context(
            target_responses={"snap version": CommandResult(127, "", "not found")}
        )

        errors = await ManualSnapSyncJob(context).validate()

        assert any(e.host is Host.TARGET and "snap" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors_and_asks_for_no_privilege(self) -> None:
        """G134, K94 — with snapd answering on both machines nothing fails, and no
        administrative-rights precondition is imposed on either: listing snaps needs none,
        and a snippet's own needs are unknowable."""
        context, source, target = make_context()

        errors: list[ValidationError] = await ManualSnapSyncJob(context).validate()

        assert errors == []
        assert not any("sudo" in cmd for cmd in all_calls(source) + all_calls(target))


def _marks(*item_ids: str) -> str:
    """A decision file recording each id skip-always."""
    body = "".join(
        f'  "{item_id}":\n    item_class: unreproducible\n    label: "{item_id}"\n'
        f"    reason: null\n    recorded_at: '2026-07-30T00:00:00+00:00'\n"
        for item_id in item_ids
    )
    return f"machine_specific:\n{body}"


class TestMarksFollowWhatTheMachineHolds:
    """A mark lives as long as the snap it names is on the machine holding it — asked of
    snapd's listing, never of whether the snap is still a sideload.
    """

    @staticmethod
    async def _run(*, source_responses: dict[str, Answer]) -> MagicMock:
        context, source, _target = make_context(source_responses=source_responses)
        job = ManualSnapSyncJob(context)
        job.accept_review(
            PackagePlan(manager="manual_snap", diffs=(), groups=()),
            ReviewOutcome(decisions={}, was_interactive=True),
        )
        await job.apply()
        return source

    @pytest.mark.asyncio
    async def test_a_marked_snap_now_installed_from_the_store_keeps_its_mark(self) -> None:
        """H247 — the check is whether the machine has it, not whether it is still a
        sideload: dropping the mark here would re-offer a snippet that overwrites the store
        copy the user chose."""
        source = await self._run(
            source_responses={
                SNAP_LIST: snap_list(store_snap("mytool")),
                "manual_snap.decisions.yaml": CommandResult(0, _marks("unreproducible:snap-sideload:mytool"), ""),
            }
        )

        assert not [cmd for cmd in all_calls(source) if "mv --force" in cmd]

    @pytest.mark.asyncio
    async def test_a_marked_snap_snapd_no_longer_reports_is_dropped(self) -> None:
        """H248 — the other answer: the machine no longer holds the snap, so the mark has
        nothing left to keep."""
        source = await self._run(
            source_responses={
                SNAP_LIST: snap_list(store_snap("firefox")),
                "manual_snap.decisions.yaml": CommandResult(0, _marks("unreproducible:snap-sideload:mytool"), ""),
            }
        )

        rewrites = [cmd for cmd in all_calls(source) if "mv --force" in cmd]
        assert len(rewrites) == 1
        assert "mytool" not in rewrites[0]


class TestSnapJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_manual_snap_sync_to_its_job(self) -> None:
        """Named in the configuration, the job resolves to its own class."""
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("manual_snap_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is ManualSnapSyncJob


class TestSnapFirstSyncScope:
    def test_the_announced_scope_names_sideloaded_snaps(self) -> None:
        """ADR-015's first-sync announcement names this job, what it would put on the target
        and the mechanism it uses to get it there."""
        scope = ManualSnapSyncJob.describe_first_sync_scope({})

        assert scope is not None
        assert scope.job_name == "manual_snap_sync"
        assert any("sideloaded snaps" in item for item in scope.scope_items)
        assert "replay install snippet" in scope.mechanism
