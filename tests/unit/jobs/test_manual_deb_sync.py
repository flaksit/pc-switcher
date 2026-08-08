"""Unit tests for `ManualDebSyncJob`: the hand-installed `.deb` half of what no package
manager can reproduce (`PKG-FR-MANUAL-SCOPE`) — the no-candidate detection, its diff and its validation.

The shared half every unreproducible job inherits is covered in `test_unreproducible_jobs.py`.
All executor interactions are mocked; no real dpkg/apt-cache/sudo commands run.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.manual_deb_sync import ManualDebSyncJob
from pcswitcher.jobs.packages.apt_policy import installed_origins_by_package
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import (
    SNIPPET_OWNERSHIP_REVIEW_ACTION,
    Decision,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.sync_core import PackagePlan
from pcswitcher.models import CommandResult, Host, JobSkipped, ValidationError
from pcswitcher.orchestrator import Orchestrator
from tests.unit.jobs.unreproducible_harness import (
    BRSCAN3_REGISTRY_YAML,
    POLICY_AUTO_DEP,
    POLICY_HAND_DEB,
    POLICY_NEWER_THAN_REPO,
    POLICY_PHASED_UPDATE_LAGGARD,
    POLICY_PINNED_NO_CANDIDATE,
    POLICY_REPO_INSTALLED,
    QEMU_REGISTRY_YAML,
    REGISTRY_QUERY,
    STATUS_QUERY,
    Answer,
    FakeReviewer,
    all_calls,
    decision_file_writes,
    hand_deb_policy,
    installed_on,
    make_context,
    registry_writes,
)


class TestNoCandidateDetection:
    """apt-no-candidate scan: a manually-installed package no configured repository can
    supply becomes an UNREPRODUCIBLE diff (`PKG-FR-MANUAL-SCOPE`).

    The predicate is the INSTALLED version's repository origins, never the `Candidate:`
    line: dpkg's own status entry makes apt report a hand-installed package's version as
    its candidate, while a negatively-pinned but fully repo-available package reports
    `Candidate: (none)`.
    """

    @staticmethod
    def _unreproducible_ids(plan: PackagePlan) -> set[str]:
        return {d.item_id for d in plan.diffs if d.item_class == ItemClass.UNREPRODUCIBLE}

    @pytest.mark.asyncio
    async def test_package_whose_only_origin_is_dpkg_status_is_unreproducible(self) -> None:
        """G1 — a hand-downloaded `.deb` whose installed version only dpkg's status file
        accounts for is presented as an item no package manager can reproduce."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code"),
                "apt-cache policy": CommandResult(0, POLICY_HAND_DEB, ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        unreproducible = [d for d in plan.diffs if d.item_class == ItemClass.UNREPRODUCIBLE]
        assert len(unreproducible) == 1
        assert unreproducible[0].item_id == "unreproducible:apt-no-candidate:code"
        assert unreproducible[0].diff_class == DiffClass.UNREPRODUCIBLE
        assert unreproducible[0].action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_repo_installed_package_is_not_unreproducible(self) -> None:
        """G2 — `gh` comes from its vendor repository and is reinstallable. Its block also
        carries a `/var/lib/dpkg/status` line — every installed package's does — so
        "the block mentions dpkg status" is not the predicate.
        """
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("gh"),
                "apt-cache policy": CommandResult(0, POLICY_REPO_INSTALLED, ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == set()

    @pytest.mark.asyncio
    async def test_a_package_apt_will_take_from_no_repository_is_unreproducible(self) -> None:
        """G3 — every repository version of `docker.io` is pinned below zero, so apt answers
        `Candidate: (none)`: there is no repository it will install the package from, and
        nothing but a snippet can put it on the other machine. The pin itself is never read
        (`PKG-FR-PIN-NOT-INVENTORY`) — apt's own conclusion is.
        """
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("docker.io"),
                "apt-cache policy": CommandResult(0, POLICY_PINNED_NO_CANDIDATE, ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:docker.io"}

    @pytest.mark.asyncio
    async def test_package_installed_from_a_repo_as_an_automatic_dependency_is_not_unreproducible(self) -> None:
        """G4 — the installed version comes from an ESM origin, so a repository supplies it."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("7zip"),
                "apt-cache policy": CommandResult(0, POLICY_AUTO_DEP, ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == set()

    @pytest.mark.asyncio
    async def test_a_package_whose_installed_version_the_archive_superseded_is_not_unreproducible(self) -> None:
        """G195 — the source sits on `1:8.2.2+ds-0ubuntu1.17` while the archive has moved to
        `.18`, which is what an Ubuntu phased update looks like from the machine it has not
        reached yet. Apt still has a repository for the name, so the package is apt's and no
        snippet is invented for it (#285).
        """
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("qemu-guest-agent"),
                "apt-cache policy": CommandResult(0, POLICY_PHASED_UPDATE_LAGGARD, ""),
            }
        )

        plan = await ManualDebSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == set()

    @pytest.mark.asyncio
    async def test_a_hand_deb_apt_marks_automatic_is_still_detected(self) -> None:
        """G5 — `code` came from a `.deb` and apt has it marked automatically installed, so
        it is outside `apt-mark showmanual`. The boundary the article draws is "no configured
        repository supplies the installed version", which this still is: `apt_sync` will not
        touch it either, so nothing else in the run would ever name it.
        """
        context, source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code"),
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, POLICY_HAND_DEB, ""),
            }
        )

        plan = await ManualDebSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:code"}
        # The manual/automatic mark is not consulted at all — asking for it and then
        # ignoring it would leave the boundary looking like a filter that happens to pass.
        assert not any("apt-mark" in cmd for cmd in all_calls(source))

    @pytest.mark.asyncio
    async def test_a_version_newer_than_any_repository_offers_is_unreproducible(self) -> None:
        """G6 — the installed version's own row names no repository while an older row
        does: replicating THIS machine's version needs the `.deb`, so the item is
        presented rather than left to apt."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("mytool"),
                "apt-cache policy": CommandResult(0, POLICY_NEWER_THAN_REPO, ""),
            }
        )

        plan = await ManualDebSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:mytool"}

    @pytest.mark.asyncio
    async def test_one_batched_scan_separates_the_hand_deb_from_the_repo_installed(self) -> None:
        """G7 — the whole manual set goes through a SINGLE `apt-cache policy` (never one
        call per package), and only the names apt will install from no repository come back
        unreproducible."""
        policy = POLICY_HAND_DEB + POLICY_REPO_INSTALLED + POLICY_PINNED_NO_CANDIDATE + POLICY_AUTO_DEP
        context, source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code", "gh", "docker.io", "7zip"),
                "apt-cache policy": CommandResult(0, policy, ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == {
            "unreproducible:apt-no-candidate:code",
            "unreproducible:apt-no-candidate:docker.io",
        }
        policy_calls = [cmd for cmd in all_calls(source) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        for name in ("code", "gh", "docker.io", "7zip"):
            assert name in policy_calls[0]

    @pytest.mark.asyncio
    async def test_no_block_inside_an_answered_policy_read_indicts_nothing(self) -> None:
        """G8 — no block for a queried name is silence, not evidence. Indicting on absence would
        declare a machine's whole manual set unreproducible, and hand `apt_sync`'s exclusion
        the same verdict. The probe ANSWERED here — exit 0, and a block for the other name —
        so nothing but `gh`'s missing block can decide this."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code", "gh"),
                "apt-cache policy": CommandResult(0, POLICY_HAND_DEB, ""),
            }
        )

        plan = await ManualDebSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:code"}

    @pytest.mark.asyncio
    async def test_a_policy_read_that_did_not_answer_fails_the_job(self) -> None:
        """G9, J81 — ADR-022: the detection probe exits non-zero, so it reported nothing about any
        package. Reading that as "no unreproducible packages here" silently drops findings
        that `apt_sync` has meanwhile excluded from its own manifest off the same predicate.
        """
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code", "gh"),
                "apt-cache policy": CommandResult(100, "", "E: could not read the package lists\n"),
            }
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await ManualDebSyncJob(context).plan()

        assert "apt-cache policy code gh" in str(excinfo.value)
        assert "could not read the package lists" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_policy_read_that_printed_no_block_at_all_fails_the_job(self) -> None:
        """G10, J82 — the `blocks` half of `PKG-FR-READ-FAILS-JOB`, which `apt_sync._source_policy` puts on the
        BYTE-IDENTICAL command — same names, same host, same probe. apt prints one block per
        name it knows and every name here is installed on this machine, so zero blocks at
        exit 0 is apt not answering. The two jobs disagreeing about that
        silence is the divergence this scan's guard exists to prevent: `apt_sync` would drop
        the same bare-`.deb` packages from its manifest while this job reports none.
        """
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code", "gh"),
                "apt-cache policy": CommandResult(0, "", ""),
            }
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await ManualDebSyncJob(context).plan()

        assert "apt-cache policy code gh" in str(excinfo.value)
        assert "printed no package block" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_policy_read_over_only_bare_deb_packages_still_answers(self) -> None:
        """G11 — the limit of the rule above, and the reason the count is of BLOCKS rather than of
        packages with an origin: a machine whose whole manual set was hand-installed from
        `.deb` files gets one origin-less block per name, which is apt answering.
        """
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code"),
                "apt-cache policy": CommandResult(0, POLICY_HAND_DEB, ""),
            }
        )

        plan = await ManualDebSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:code"}

    @pytest.mark.asyncio
    async def test_an_installed_set_read_that_did_not_answer_fails_the_job(self) -> None:
        """G12, J83 — the other end of the same detection: the `dpkg-query` naming the source's
        installed packages exits non-zero, so the run knows nothing about them. The policy
        probe below it is left answering normally, so only that read can fail this."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: CommandResult(100, "", "E: Problem opening /var/lib/dpkg/status\n"),
                "apt-cache policy": CommandResult(0, POLICY_HAND_DEB, ""),
            }
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await ManualDebSyncJob(context).plan()

        assert "dpkg-query" in str(excinfo.value)
        assert "exited 100" in str(excinfo.value)

    def test_only_the_installed_version_row_contributes_origins(self) -> None:
        """`gh`'s older version rows name three Ubuntu URIs that merely OFFER the package.
        Only the `***` row's origin is where the installed version actually came from."""
        assert installed_origins_by_package(POLICY_REPO_INSTALLED)["gh"] == frozenset(
            {"https://cli.github.com/packages"}
        )


class TestInertFiltering:
    """An item recorded machine-specific on the source produces no diff
    (`PKG-FR-MACHINE-SPECIFIC`/`PKG-FR-MANUAL-DIFF`)."""

    @pytest.mark.asyncio
    async def test_machine_specific_item_is_filtered_before_becoming_a_diff(self) -> None:
        """G37 — a mark from an earlier run for a still-present finding keeps it out of every
        list."""
        decisions_yaml = (
            "machine_specific:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    item_class: unreproducible\n"
            "    label: brscan3 (no apt candidate)\n"
            "    reason: null\n"
            "    recorded_at: '2026-01-01T00:00:00+00:00'\n"
        )
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/manual_deb.decisions.yaml": CommandResult(0, decisions_yaml, ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_mark_on_the_target_does_not_silence_a_source_held_finding(self) -> None:
        """G45 — only the SOURCE's marks silence a source-held finding: the same recorded
        item on the target leaves the finding presented, and the target's decision file is
        never even read."""
        decisions_yaml = (
            "machine_specific:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    item_class: unreproducible\n"
            "    label: brscan3 (no apt candidate)\n"
            "    reason: null\n"
            "    recorded_at: '2026-01-01T00:00:00+00:00'\n"
        )
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/manual_deb.decisions.yaml": CommandResult(0, decisions_yaml, "")
            },
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == ["unreproducible:apt-no-candidate:brscan3"]
        assert not [cmd for cmd in all_calls(target) if "manual.decisions.yaml" in cmd]


class TestExecuteIndependentOfApt:
    """The job runs on its own enable flag, independent of apt_sync
    (`PKG-FR-JOB-INDEPENDENCE`/`PKG-FR-MANUAL-SCOPE`)."""

    @pytest.mark.asyncio
    async def test_plan_runs_with_apt_absent_from_config_and_manual_enabled(self) -> None:
        """G26 — the hand-`.deb` finding is detected with apt sync absent from the
        configuration: this job asks apt and dpkg its own questions."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            },
            enabled_sync_jobs={"manual_installs_sync": True, "folder_sync": True},
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == ["unreproducible:apt-no-candidate:brscan3"]

    @pytest.mark.asyncio
    async def test_execute_runs_plan_review_apply_through_injected_reviewer(self) -> None:
        item_id = "unreproducible:apt-no-candidate:brscan3"
        reviewer = FakeReviewer(decisions={item_id: Decision.APPLY})
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                # plan() classifies INSTALL from the SOURCE registry (corrected `PKG-FR-MANUAL-SAME-RUN`).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualDebSyncJob(context)

        await job.execute()

        assert reviewer.groups_seen is not None
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1


class TestWhatTheTargetAlreadyHolds:
    """`PKG-FR-MANUAL-DIFF`: both machines are read and only what the target lacks is
    presented, which is what stops software already on the target from being asked about on
    every later run.
    """

    @pytest.mark.asyncio
    async def test_a_package_the_target_has_from_a_repository_counts_as_held(self) -> None:
        """G111 — the apt half of what the target holds is its installed set, whatever origin
        put each name there: software that is on the machine is on the machine."""
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("code"), ""),
            },
            target_responses={STATUS_QUERY: installed_on("code")},
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        # No second origin analysis on the target: its installed set answers the question.
        assert not [cmd for cmd in all_calls(target) if cmd.startswith("apt-cache policy")]


class TestWhoInstalledIt:
    """`PKG-FR-DEB-AMBIGUITY`: a snippet on record for a package the SOURCE's apt can install
    from a repository fits two readings — a hand install apt happens to carry the name for,
    or an ordinary apt package with a leftover snippet — and nothing on either machine
    settles it, so the run asks.
    """

    @staticmethod
    def _ownership_groups(plan: PackagePlan) -> list[ReviewGroup]:
        return [group for group in plan.groups if group.action == SNIPPET_OWNERSHIP_REVIEW_ACTION]

    @staticmethod
    def _context(
        *,
        registry: str = QEMU_REGISTRY_YAML,
        policy: str = POLICY_PHASED_UPDATE_LAGGARD,
        installed: str = "qemu-guest-agent",
        source_responses: dict[str, Answer] | None = None,
        **kwargs: Any,
    ) -> tuple[JobContext, MagicMock, MagicMock]:
        return make_context(
            source_responses={
                STATUS_QUERY: installed_on(installed),
                "apt-cache policy": CommandResult(0, policy, ""),
                REGISTRY_QUERY: CommandResult(0, registry, ""),
                **(source_responses or {}),
            },
            target_responses={REGISTRY_QUERY: CommandResult(0, registry, "")},
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_a_snippet_for_a_package_apt_can_install_is_asked_about(self) -> None:
        """G196, G197 — the question names apt's own answer, so the user is judging the two
        claims rather than a package name: the snippet on record, and the repository apt
        would take the package from."""
        context, _source, _target = self._context()

        plan = await ManualDebSyncJob(context).plan()

        groups = self._ownership_groups(plan)
        assert len(groups) == 1
        entry = groups[0].entries[0]
        assert entry.item_id == "unreproducible:apt-no-candidate:qemu-guest-agent"
        assert entry.detail is not None
        assert "ftp.belnet.be" in entry.detail
        assert groups[0].recorded_bodies is not None
        assert "dpkg --install" in groups[0].recorded_bodies[entry.item_id].install_body

    @pytest.mark.asyncio
    async def test_a_snippet_for_a_package_apt_cannot_install_is_not_ambiguous(self) -> None:
        """G203 — the ordinary hand-`.deb`: apt has no repository for the name, so the snippet
        contradicts nothing and the package is already this job's own item."""
        context, _source, _target = self._context(
            registry=BRSCAN3_REGISTRY_YAML, policy=hand_deb_policy("brscan3"), installed="brscan3"
        )

        plan = await ManualDebSyncJob(context).plan()

        assert self._ownership_groups(plan) == []

    @pytest.mark.asyncio
    async def test_a_snippet_for_a_package_the_source_does_not_have_is_not_asked_about(self) -> None:
        """G204 — the verdict is the source's (`PKG-FR-DEB-OWNERSHIP`), and a package the
        source does not have installed makes no claim about what put it there."""
        context, _source, _target = self._context(installed="coreutils", policy=hand_deb_policy("coreutils"))

        plan = await ManualDebSyncJob(context).plan()

        assert self._ownership_groups(plan) == []

    @pytest.mark.asyncio
    async def test_a_machine_specific_item_is_not_asked_about(self) -> None:
        """G205 — the mark already puts the item out of the run, so asking who installed it
        would be a question about software the user said not to touch."""
        decisions = _manual_decisions("unreproducible:apt-no-candidate:qemu-guest-agent")
        context, _source, _target = self._context(
            source_responses={"manual_deb.decisions.yaml": CommandResult(0, decisions, "")}
        )

        plan = await ManualDebSyncJob(context).plan()

        assert self._ownership_groups(plan) == []

    @pytest.mark.asyncio
    async def test_answering_apt_deletes_the_entry_from_both_registries(self) -> None:
        """G206 — the deletion goes to BOTH copies, not to the source's followed by a push: a
        source-only delete is what `_guard_registry_overwrite` calls a lost entry, so the push
        would ask the user to consent to the answer they had just given."""
        item_id = "unreproducible:apt-no-candidate:qemu-guest-agent"
        context, source, target = self._context(reviewer=FakeReviewer(decisions={item_id: Decision.APPLY}))

        await ManualDebSyncJob(context).execute()

        for machine in (source, target):
            writes = registry_writes(machine)
            assert len(writes) == 1
            assert "qemu-guest-agent" not in writes[0]

    @pytest.mark.asyncio
    async def test_keeping_the_snippet_writes_nothing_anywhere(self) -> None:
        """G207 — the answer that keeps it records nothing: no marker, no decision-file entry
        and no registry flag, which is what makes the same state ask again next run."""
        item_id = "unreproducible:apt-no-candidate:qemu-guest-agent"
        context, source, target = self._context(reviewer=FakeReviewer(decisions={item_id: Decision.SKIP_ONCE}))

        await ManualDebSyncJob(context).execute()

        assert registry_writes(source) == []
        assert registry_writes(target) == []
        assert decision_file_writes(source, "manual_deb") == []

    @pytest.mark.asyncio
    async def test_a_dry_run_asks_and_deletes_nothing(self) -> None:
        """G208 — ADR-014: a rehearsal leaves no trace, the registry included."""
        item_id = "unreproducible:apt-no-candidate:qemu-guest-agent"
        context, source, target = self._context(
            reviewer=FakeReviewer(decisions={item_id: Decision.APPLY}), dry_run=True
        )

        await ManualDebSyncJob(context).execute()

        assert registry_writes(source) == []
        assert registry_writes(target) == []

    @pytest.mark.asyncio
    async def test_a_run_nobody_answered_deletes_nothing(self) -> None:
        """G209 — `PKG-FR-NO-TERMINAL`: no flag answers this question either, so an outcome
        nobody gave carries only the default and the entry stays where it is."""
        item_id = "unreproducible:apt-no-candidate:qemu-guest-agent"
        context, source, target = self._context(
            reviewer=FakeReviewer(decisions={item_id: Decision.APPLY}, was_interactive=False)
        )

        with pytest.raises(JobSkipped):
            await ManualDebSyncJob(context).execute()

        assert registry_writes(source) == []
        assert registry_writes(target) == []


class TestValidate:
    @pytest.mark.asyncio
    async def test_apt_cache_unavailable_on_source_yields_validation_error(self) -> None:
        """G23, K63 — validation fails before anything runs, naming the source and the missing tool."""
        context, _source, _target = make_context(
            source_responses={"apt-cache --version": CommandResult(127, "", "not found")}
        )
        job = ManualDebSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "apt-cache" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_dpkg_unavailable_on_source_yields_validation_error(self) -> None:
        """G24, K64 — validation fails before anything runs, naming the source and the missing tool."""
        context, _source, _target = make_context(
            source_responses={"dpkg --version": CommandResult(127, "", "not found")}
        )
        job = ManualDebSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "dpkg" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_dpkg_unavailable_on_target_yields_validation_error(self) -> None:
        """G112 — the target is read too now that what it already holds decides what is
        presented, so its missing tool is named before the run starts rather than as a dead
        probe halfway through."""
        context, _source, _target = make_context(
            target_responses={"dpkg --version": CommandResult(127, "", "not found")}
        )
        job = ManualDebSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "dpkg" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors(self) -> None:
        """G25, K50, K51, K62 — with both tools present nothing fails, and no administrative-rights
        precondition is imposed on either machine: a snippet's own needs are unknowable,
        so there is nothing to probe for and passing is not conditional on sudo."""
        context, source, target = make_context()
        job = ManualDebSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []
        assert not any("sudo --non-interactive true" in cmd for cmd in all_calls(source) + all_calls(target))


def _manual_decisions(*item_ids: str) -> str:
    """A manual decision file recording each id skip-always."""
    body = "".join(
        f'  "{item_id}":\n    item_class: unreproducible\n    label: "{item_id}"\n'
        f"    reason: null\n    recorded_at: '2026-07-30T00:00:00+00:00'\n"
        for item_id in item_ids
    )
    return f"machine_specific:\n{body}"


class TestMarksFollowWhatTheMachineHolds:
    """An unreproducible mark lives as long as the item it names is on the machine holding
    it — asked of dpkg's installed set, never of what a repository could now supply.
    """

    @staticmethod
    async def _run(*, source_responses: dict[str, Answer]) -> MagicMock:
        context, source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("coreutils"),
                **source_responses,
            }
        )
        job = ManualDebSyncJob(context)
        job.accept_review(
            PackagePlan(manager="manual_deb", diffs=(), groups=()),
            ReviewOutcome(decisions={}, was_interactive=True),
        )
        await job.apply()
        return source

    @pytest.mark.asyncio
    async def test_a_marked_package_still_installed_keeps_its_mark(self) -> None:
        """H215 — dpkg's installed set answers the package half, whatever a repository can
        now supply: a marked package that became reproducible is still installed, and the
        mark still keeps it."""
        source = await self._run(
            source_responses={
                STATUS_QUERY: installed_on("coreutils", "brscan3"),
                "manual_deb.decisions.yaml": CommandResult(
                    0, _manual_decisions("unreproducible:apt-no-candidate:brscan3"), ""
                ),
            }
        )

        assert not [cmd for cmd in all_calls(source) if "mv --force" in cmd]

    @pytest.mark.asyncio
    async def test_a_marked_package_dpkg_no_longer_reports_is_dropped(self) -> None:
        """H216 — the package half's other answer."""
        source = await self._run(
            source_responses={
                STATUS_QUERY: installed_on("coreutils"),
                "manual_deb.decisions.yaml": CommandResult(
                    0, _manual_decisions("unreproducible:apt-no-candidate:brscan3"), ""
                ),
            }
        )

        rewrites = [cmd for cmd in all_calls(source) if "mv --force" in cmd]
        assert len(rewrites) == 1
        assert "brscan3" not in rewrites[0]


class TestDebJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_manual_deb_sync_to_its_job(self) -> None:
        """G185 — named in the configuration, the job resolves to its own class."""
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("manual_deb_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is ManualDebSyncJob


class TestDebFirstSyncScope:
    def test_the_announced_scope_names_hand_installed_deb_packages(self) -> None:
        """G186 — ADR-015's first-sync announcement names this job, what it would put on the
        target and the mechanism it uses to get it there."""
        scope = ManualDebSyncJob.describe_first_sync_scope({})

        assert scope is not None
        assert scope.job_name == "manual_deb_sync"
        assert any("hand-installed .deb" in item for item in scope.scope_items)
        assert "replay install snippet" in scope.mechanism
