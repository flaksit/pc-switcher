"""Unit tests for `ManualFlatpakSyncJob`: the flatpak half of what no package manager can
reproduce (#252, D-18) — the no-remote detection, its validation, and the marks
`flatpak_sync` recorded about these refs before they were excluded from it.

The shared half every unreproducible job inherits is covered in `test_unreproducible_jobs.py`.
All executor interactions are mocked; no real flatpak commands run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.manual_flatpak_sync import ManualFlatpakSyncJob
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import ReviewOutcome
from pcswitcher.jobs.packages.sync_core import PackagePlan
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator
from tests.unit.jobs.unreproducible_harness import Answer, all_calls, make_context

# `flatpak list --app --columns=origin,installation,ref` prints no header row: the
# --columns flag names the columns, so a line is exactly those three tab-separated fields.
LIST_APPS = "flatpak list --app"
LIST_ALL_REFS = "flatpak list --columns"
REMOTES_USER = "flatpak remotes --user"
REMOTES_SYSTEM = "flatpak remotes --system"

# The two shapes issue #252 names, both measured on Flatpak 1.14.6. A bundle install's
# origin is a pseudo-remote `flatpak remotes` does not list (`bundle-origin`, url-less); a
# deleted remote leaves its name on the ref while the remote itself is gone (`localrepo`).
BUNDLE_REF = "org.pcswtest.Bundle/x86_64/stable"
DELETED_REMOTE_REF = "org.example.Gone/x86_64/stable"
FLATHUB_REF = "org.gimp.GIMP/x86_64/stable"


def ref_line(ref: str, origin: str, installation: str = "user") -> str:
    return f"{origin}\t{installation}\t{ref}\n"


def remotes(*names: str) -> CommandResult:
    """What `flatpak remotes --columns=name` prints for a scope configuring `names`."""
    return CommandResult(0, "".join(f"{name}\n" for name in names), "")


def source_with(*, apps: str, user_remotes: tuple[str, ...] = ("flathub",)) -> dict[str, Answer]:
    return {
        LIST_APPS: CommandResult(0, apps, ""),
        LIST_ALL_REFS: CommandResult(0, apps, ""),
        REMOTES_USER: remotes(*user_remotes),
        REMOTES_SYSTEM: remotes(*user_remotes),
    }


def item_id(ref: str, scope: str = "user") -> str:
    return f"unreproducible:flatpak-no-remote:{scope}:{ref}"


class TestNoRemoteDetection:
    """The shared predicate (`packages/flatpak_policy.py`): a ref whose `origin` names no
    remote configured in its own scope is software no package manager can put on the other
    machine.
    """

    @staticmethod
    def _unreproducible_ids(plan: PackagePlan) -> set[str]:
        return {d.item_id for d in plan.diffs if d.item_class == ItemClass.UNREPRODUCIBLE}

    @pytest.mark.asyncio
    async def test_a_bundle_installed_ref_is_unreproducible(self) -> None:
        """G125 — `flatpak install --bundle` leaves the ref pointing at a url-less
        pseudo-remote `flatpak remotes` does not list, so no remote can supply it."""
        context, _source, _target = make_context(
            source_responses=source_with(apps=ref_line(BUNDLE_REF, "bundle-origin"))
        )
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        unreproducible = [d for d in plan.diffs if d.item_class == ItemClass.UNREPRODUCIBLE]
        assert len(unreproducible) == 1
        assert unreproducible[0].item_id == item_id(BUNDLE_REF)
        assert unreproducible[0].diff_class == DiffClass.UNREPRODUCIBLE
        assert unreproducible[0].action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_a_ref_whose_remote_was_deleted_is_unreproducible(self) -> None:
        """G126 — the second shape, and the reason there is one predicate rather than two: a
        deleted remote leaves its name on the ref, which is a name matching no remote."""
        context, _source, _target = make_context(
            source_responses=source_with(apps=ref_line(DELETED_REMOTE_REF, "localrepo"))
        )
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == {item_id(DELETED_REMOTE_REF)}

    @pytest.mark.asyncio
    async def test_a_ref_from_a_configured_remote_is_not_presented(self) -> None:
        """G127 — the negative control: an ordinary flathub app is `flatpak_sync`'s to
        replicate, and presenting it here would ask for a snippet nobody needs."""
        context, _source, _target = make_context(source_responses=source_with(apps=ref_line(FLATHUB_REF, "flathub")))
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == set()

    @pytest.mark.asyncio
    async def test_the_same_ref_in_two_scopes_yields_one_item_per_scope(self) -> None:
        """G128 — user and system are separate installations, so scope is inside the
        identity: the same application in both, from a bundle in both, is two items."""
        context, _source, _target = make_context(
            source_responses=source_with(
                apps=ref_line(BUNDLE_REF, "bundle-origin", "user") + ref_line(BUNDLE_REF, "bundle-origin", "system")
            )
        )
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == {item_id(BUNDLE_REF, "user"), item_id(BUNDLE_REF, "system")}

    @pytest.mark.asyncio
    async def test_a_remote_configured_in_the_other_scope_does_not_reproduce_the_ref(self) -> None:
        """G129 — flatpak tracks remotes per installation, so each scope is asked its own
        question: a system-wide `flathub` says nothing about a user-scope ref naming it."""
        context, source, _target = make_context(
            source_responses={
                LIST_APPS: CommandResult(0, ref_line(FLATHUB_REF, "flathub", "user"), ""),
                LIST_ALL_REFS: CommandResult(0, ref_line(FLATHUB_REF, "flathub", "user"), ""),
                REMOTES_USER: remotes(),
                REMOTES_SYSTEM: remotes("flathub"),
            }
        )
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == {item_id(FLATHUB_REF)}
        assert [cmd for cmd in all_calls(source) if cmd.startswith("flatpak remotes")] == [
            "flatpak remotes --user --columns=name",
            "flatpak remotes --system --columns=name",
        ]

    @pytest.mark.asyncio
    async def test_a_runtime_is_never_an_item(self) -> None:
        """G140 — apps only, matching what `flatpak_sync` replicates: a runtime arrives with
        the app that needs it and is never installed on its own, so the detection listing is
        the `--app` one."""
        context, source, _target = make_context(
            source_responses=source_with(apps=ref_line(BUNDLE_REF, "bundle-origin"))
        )
        job = ManualFlatpakSyncJob(context)

        await job.plan()

        assert "flatpak list --app --columns=origin,installation,ref" in all_calls(source)

    @pytest.mark.asyncio
    async def test_a_machine_with_no_flatpak_apps_asks_no_remote_question(self) -> None:
        """G141 — nothing installed is an ordinary answer, and a machine with no app has no
        origin to judge, so the two `flatpak remotes` reads are not issued at all."""
        context, source, _target = make_context(source_responses={LIST_APPS: CommandResult(0, "", "")})
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert not [cmd for cmd in all_calls(source) if cmd.startswith("flatpak remotes")]


class TestAProbeThatDidNotAnswer:
    """ADR-022: a read that did not answer is never data. Both reads exit 0 on an ordinary
    machine with nothing installed and nothing configured, so the exit code is the whole
    discriminator.
    """

    @pytest.mark.asyncio
    async def test_a_list_that_did_not_answer_fails_the_job(self) -> None:
        """G131 — silence read as data would report "nothing on this machine was installed by
        hand", the one answer this job exists to be able to contradict."""
        context, _source, _target = make_context(
            source_responses={LIST_APPS: CommandResult(1, "", "error: unable to reach flatpak")}
        )
        job = ManualFlatpakSyncJob(context)

        with pytest.raises(ProbeFailed, match="flatpak list"):
            await job.plan()

    @pytest.mark.asyncio
    async def test_a_remotes_read_that_did_not_answer_fails_the_job(self) -> None:
        """G132 — the other half: a remotes read that died would make every installed ref
        look unreproducible and ask for a snippet for each."""
        context, _source, _target = make_context(
            source_responses={
                LIST_APPS: CommandResult(0, ref_line(FLATHUB_REF, "flathub"), ""),
                REMOTES_USER: CommandResult(1, "", "error: unable to open repo"),
            }
        )
        job = ManualFlatpakSyncJob(context)

        with pytest.raises(ProbeFailed, match="flatpak remotes"):
            await job.plan()

    @pytest.mark.asyncio
    async def test_a_scope_configuring_no_remote_at_all_is_data_not_a_failure(self) -> None:
        """G142 — a machine that configures no remote in one scope is ordinary; every ref in
        that scope really is unreproducible, and that is the answer, not a probe failure."""
        context, _source, _target = make_context(
            source_responses=source_with(apps=ref_line(FLATHUB_REF, "flathub"), user_remotes=())
        )
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == [item_id(FLATHUB_REF)]


class TestWhatTheTargetAlreadyHolds:
    """`PKG-FR-MANUAL-DIFF`: both machines are read and only what the target lacks is
    presented.
    """

    @pytest.mark.asyncio
    async def test_a_ref_the_target_has_from_a_remote_counts_as_held(self) -> None:
        """G130 — whatever origin put it there: software that is on the machine is on the
        machine, and re-asking the reproducibility question on the target would cost two more
        reads to answer what its own installed set already answers."""
        context, _source, target = make_context(
            source_responses=source_with(apps=ref_line(BUNDLE_REF, "bundle-origin")),
            target_responses={LIST_APPS: CommandResult(0, ref_line(BUNDLE_REF, "flathub"), "")},
        )
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert not [cmd for cmd in all_calls(target) if cmd.startswith("flatpak remotes")]

    @pytest.mark.asyncio
    async def test_the_same_ref_in_the_other_scope_is_not_held(self) -> None:
        """G143 — identity carries the scope, so a user-scope finding is not answered by a
        system-scope copy: the two are separate installations."""
        context, _source, _target = make_context(
            source_responses=source_with(apps=ref_line(BUNDLE_REF, "bundle-origin", "user")),
            target_responses={LIST_APPS: CommandResult(0, ref_line(BUNDLE_REF, "flathub", "system"), "")},
        )
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == [item_id(BUNDLE_REF, "user")]


class TestValidate:
    @pytest.mark.asyncio
    async def test_flatpak_unavailable_on_source_yields_validation_error(self) -> None:
        """G133 — validation fails before anything runs, naming the source and the missing tool."""
        context, _source, _target = make_context(source_responses={"flatpak --version": CommandResult(127, "", "")})
        job = ManualFlatpakSyncJob(context)

        errors = await job.validate()

        assert [e.host for e in errors] == [Host.SOURCE]
        assert "flatpak" in errors[0].message

    @pytest.mark.asyncio
    async def test_flatpak_unavailable_on_target_yields_validation_error(self) -> None:
        """G134 — the target is read to tell what it already has, so its flatpak is a
        precondition too."""
        context, _source, _target = make_context(target_responses={"flatpak --version": CommandResult(127, "", "")})
        job = ManualFlatpakSyncJob(context)

        errors = await job.validate()

        assert [e.host for e in errors] == [Host.TARGET]

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors_and_imposes_no_sudo_precondition(self) -> None:
        """G135 — both machines are only ever READ for detection, and a snippet's own
        administrative needs are unknowable, so no rights are demanded up front."""
        context, _source, target = make_context()
        job = ManualFlatpakSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []
        assert not [cmd for cmd in all_calls(target) if "sudo" in cmd]


def _decisions(*item_ids: str) -> str:
    """A decision file recording each id skip-always."""
    body = "".join(
        f'  "{item_id}":\n    item_class: unreproducible\n    label: "{item_id}"\n'
        f"    reason: null\n    recorded_at: '2026-07-30T00:00:00+00:00'\n"
        for item_id in item_ids
    )
    return f"machine_specific:\n{body}"


class TestMarksRecordedBeforeTheExclusion:
    """`flatpak_sync` had no exclusion, so a bundle-installed ref was an ordinary flatpak
    item and an answer about one went into `flatpak`'s decision file under a `flatpak:ref:`
    id. Adding the exclusion orphans exactly those entries unless they are adopted — and the
    two managers do not share an id namespace, so adoption renames as well as reads
    (`state.AdoptedMarks`).

    The stubs list this job's own file FIRST: `flatpak.decisions.yaml` is a substring of
    `manual_flatpak.decisions.yaml`, so the narrower key has to match first or both reads
    answer with the legacy content.
    """

    @staticmethod
    def _context(legacy_decisions: str) -> tuple[JobContext, MagicMock, MagicMock]:
        return make_context(
            source_responses={
                "manual_flatpak.decisions.yaml": CommandResult(0, "", ""),
                "flatpak.decisions.yaml": CommandResult(0, legacy_decisions, ""),
                **source_with(apps=ref_line(BUNDLE_REF, "bundle-origin")),
            }
        )

    @pytest.mark.asyncio
    async def test_a_mark_recorded_before_the_exclusion_still_silences_its_finding(self) -> None:
        """G136 — the answer was given while `flatpak_sync` owned the ref, so it sits in that
        job's file under that job's id; the finding it silenced stays silent rather than
        being put to the user again under a new job's name."""
        context, _source, _target = self._context(_decisions(f"flatpak:ref:user:{BUNDLE_REF}"))
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_the_adopted_mark_is_read_under_this_job_s_own_identity(self) -> None:
        """G137 — the rename is a prefix swap, which is why this job's identifier is
        `<scope>:<ref>`: the legacy id and this job's differ only in what precedes it."""
        context, _source, _target = self._context(_decisions(f"flatpak:ref:user:{BUNDLE_REF}"))
        job = ManualFlatpakSyncJob(context)

        entries = await job._decision_file(job.source).load()  # pyright: ignore[reportPrivateUsage]

        assert set(entries) == {item_id(BUNDLE_REF)}
        assert entries[item_id(BUNDLE_REF)].item_id == item_id(BUNDLE_REF)

    @pytest.mark.asyncio
    async def test_a_remote_mark_in_the_same_file_is_not_adopted(self) -> None:
        """G138 — that file can also hold `flatpak:remote:` and `flatpak:mask:` entries; this
        job takes the `flatpak:ref:` slice and nothing else."""
        context, _source, _target = self._context(
            _decisions(f"flatpak:ref:user:{BUNDLE_REF}", "flatpak:remote:user:flathub")
        )
        job = ManualFlatpakSyncJob(context)

        entries = await job._decision_file(job.source).load()  # pyright: ignore[reportPrivateUsage]

        assert set(entries) == {item_id(BUNDLE_REF)}


class TestMarksFollowWhatTheMachineHolds:
    """A mark lives as long as the ref it names is installed on the machine holding it —
    asked of `flatpak list`, never of whether a remote could now supply it.
    """

    @staticmethod
    async def _run(*, source_responses: dict[str, Answer]) -> MagicMock:
        context, source, _target = make_context(source_responses=source_responses)
        job = ManualFlatpakSyncJob(context)
        job.accept_review(
            PackagePlan(manager="manual_flatpak", diffs=(), groups=()),
            ReviewOutcome(decisions={}, was_interactive=True),
        )
        await job.apply()
        return source

    @pytest.mark.asyncio
    async def test_a_marked_ref_still_installed_keeps_its_mark(self) -> None:
        """G139 — presence answers this, not reproducibility: a marked ref whose remote the
        user re-added is still installed, and dropping its mark would re-offer software the
        user asked to be left alone."""
        source = await self._run(
            source_responses={
                "manual_flatpak.decisions.yaml": CommandResult(0, _decisions(item_id(BUNDLE_REF)), ""),
                "flatpak.decisions.yaml": CommandResult(0, "", ""),
                **source_with(apps=ref_line(BUNDLE_REF, "flathub")),
            }
        )

        assert not [cmd for cmd in all_calls(source) if "mv --force" in cmd]

    @pytest.mark.asyncio
    async def test_a_marked_ref_the_machine_no_longer_has_is_dropped(self) -> None:
        """G144 — the other answer: the mark keeps this machine's copy, and there is no copy
        left for it to keep."""
        source = await self._run(
            source_responses={
                "manual_flatpak.decisions.yaml": CommandResult(0, _decisions(item_id(BUNDLE_REF)), ""),
                "flatpak.decisions.yaml": CommandResult(0, "", ""),
                **source_with(apps=ref_line(FLATHUB_REF, "flathub")),
            }
        )

        rewrites = [cmd for cmd in all_calls(source) if "mv --force" in cmd]
        assert len(rewrites) == 1
        assert "manual_flatpak.decisions.yaml" in rewrites[0]

    @pytest.mark.asyncio
    async def test_a_dead_adopted_mark_leaves_the_file_that_holds_it(self) -> None:
        """G145 — an adopted entry lives in `flatpak`'s file, so dropping it from this job's
        file alone would leave `load()` adopting it again on the next run — the mark
        outliving its item."""
        source = await self._run(
            source_responses={
                "manual_flatpak.decisions.yaml": CommandResult(0, "", ""),
                "flatpak.decisions.yaml": CommandResult(0, _decisions(f"flatpak:ref:user:{BUNDLE_REF}"), ""),
                **source_with(apps=ref_line(FLATHUB_REF, "flathub")),
            }
        )

        rewrites = [cmd for cmd in all_calls(source) if "mv --force" in cmd]
        assert [cmd for cmd in rewrites if "manual_flatpak.decisions.yaml" not in cmd]


class TestExecuteIndependentOfFlatpakSync:
    """The job runs on its own enable flag, independent of `flatpak_sync` (D-15/D-18)."""

    @pytest.mark.asyncio
    async def test_plan_runs_with_flatpak_sync_absent_from_config(self) -> None:
        """G146 — the finding is still detected and presented; this job asks flatpak its own
        questions and imports nothing from `flatpak_sync`."""
        context, _source, _target = make_context(
            source_responses=source_with(apps=ref_line(BUNDLE_REF, "bundle-origin")),
            enabled_sync_jobs={"manual_flatpak_sync": True},
        )
        job = ManualFlatpakSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == [item_id(BUNDLE_REF)]


class TestFlatpakJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_manual_flatpak_sync_to_its_job(self) -> None:
        """G123 — named in the configuration, the job resolves to its own class."""
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("manual_flatpak_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is ManualFlatpakSyncJob


class TestFlatpakFirstSyncScope:
    def test_the_announced_scope_names_the_refs_no_remote_can_supply(self) -> None:
        """G124 — ADR-015's first-sync announcement names this job, what it would put on the
        target and the mechanism it uses to get it there."""
        scope = ManualFlatpakSyncJob.describe_first_sync_scope({})

        assert scope is not None
        assert scope.job_name == "manual_flatpak_sync"
        assert any("flatpak refs no remote can supply" in item for item in scope.scope_items)
        assert "replay install snippet" in scope.mechanism
