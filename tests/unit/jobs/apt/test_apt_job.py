"""`AptSyncJob`'s own contract: the plan/apply split, the review groups it carves, and validate().

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import dataclasses
import re
from unittest.mock import MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.apt_sync.commands import TARGET_SUDO_COMMANDS
from pcswitcher.jobs.packages.items import DiffAction, DiffClass
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import (
    _REMOVAL_ACTIONS,
    REPO_CONFLICT_REVIEW_ACTION,
    REPO_REMOVAL_REVIEW_ACTION,
    Decision,
    _is_promotable_group,
    _is_removal_direction,
)
from pcswitcher.jobs.packages.sync_core import PackageItemFailures
from pcswitcher.models import CommandResult, Host
from pcswitcher.orchestrator import Orchestrator
from tests.unit.jobs.apt.helpers import (
    _CHANGED_VENDOR,
    _DEB822_FOO,
    _NO_PACKAGES,
    _PIN_DIGEST_CMD,
    _POLICY_NO_CANDIDATE,
    _RIVAL_LIST,
    _SOURCE_SCAN_CMD,
    _VENDOR_LIST,
    DPKG_QUERY_3,
    SHOWMANUAL_3,
    CountingReviewer,
    _policy_block,
    _policy_candidate,
    _repo_context,
    _scan_line,
    actionable_entry_ids,
    all_calls,
    decision_file,
    differing_repo_context,
    install_reviewer,
    installed_on_target,
    make_context,
    real_installs,
    respond_to,
    sha256_line,
    target_offers,
)


class TestPlanApplySplit:
    """plan() issues only read commands; execute() refuses without an accepted plan."""

    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_command(self) -> None:
        """H2."""
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 3
        for cmd in all_calls(target):
            # `apt-get --dry-run` (simulate) IS expected during plan() — plan 02-05's
            # plan-time collateral simulation is read-only by design (D-24/T-02-32).
            # `sudo find ... sha256sum` IS also expected — plan 02-06's repo-state
            # capture reads `/etc/apt/*` via sudo to guarantee access regardless of
            # file permissions; it is a read, never a write (D-11/D-12/D-13).
            assert "apt-get install" not in cmd
            assert "sudo install" not in cmd
            assert "sudo rm" not in cmd
            assert "sudo apt-get" not in cmd
            assert "sudo cp" not in cmd

    @pytest.mark.asyncio
    async def test_execute_without_a_reviewer_raises_and_issues_no_command(self) -> None:
        context, _source, target = make_context()
        job = AptSyncJob(context)  # context.reviewer defaults to None

        with pytest.raises(AssertionError, match="no reviewer"):
            await job.execute()

        target.run_command.assert_not_called()


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_issues_no_mutating_command(self) -> None:
        """J50."""
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
            dry_run=True,
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        # `apt-get --dry-run` (read-only plan-time collateral simulation) still runs even
        # under dry_run — dry_run only suppresses the REAL mutating command.
        for cmd in all_calls(target):
            assert "apt-get install" not in cmd


class TestContinueOnFailure:
    @pytest.mark.asyncio
    async def test_second_of_three_fails_all_attempted_one_failure_raised(self) -> None:
        """J19, J20."""
        clean_preview = CommandResult(0, "Inst dummy (1.0)\n", "")
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, target_offers("pkg-a", "pkg-b", "pkg-c"), ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": clean_preview,
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-b": clean_preview,
                "apt-get --dry-run install --assume-yes --no-install-recommends pkg-c": clean_preview,
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-b": (
                    CommandResult(1, "", "dpkg error for pkg-b")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-c": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:package:pkg-a": Decision.APPLY,
                "apt:package:pkg-b": Decision.APPLY,
                "apt:package:pkg-c": Decision.APPLY,
            },
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        assert exc_info.value.failures[0][0].item_id == "apt:package:pkg-b"

        commands = all_calls(target)
        real_installs = [c for c in commands if "sudo" in c and "apt-get install" in c]
        assert len(real_installs) == 3
        simulations = [c for c in commands if "apt-get --dry-run" in c]
        # 1 batched plan-time simulation (all three candidates) + 1 apply-time
        # simulation per approved item (D-24/T-02-32's two-layer guard).
        assert len(simulations) == 4


class TestHoldReviewVerbs:
    """#208 D3 — the single behavioural promise of hold replication: a hold item reads
    "hold"/"unhold" in its group title AND in every entry's `action_label`, and never
    appears under an install/remove packages group, even when ordinary package installs
    and removals share the same `DiffAction` in the same plan.
    """

    @pytest.mark.asyncio
    async def test_hold_items_get_their_own_group_with_hold_and_unhold_verbs(self) -> None:
        """B5, H84."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-install\npkg-common\n", ""),
                "dpkg-query": CommandResult(0, "pkg-install\t1.0\npkg-common\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "hold-add\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-extra\npkg-common\n", ""),
                "dpkg-query": CommandResult(0, "pkg-extra\t9.9\npkg-common\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "hold-drop\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        group_of = {entry.item_id: group for group in plan.groups for entry in group.entries}
        label_of = {entry.item_id: entry.action_label for group in plan.groups for entry in group.entries}

        # The package diffs still read as install/remove — the hold verbs are not a
        # blanket rename, they are per item class.
        assert group_of["apt:package:pkg-install"].title == "Install apt packages"
        assert group_of["apt:package:pkg-extra"].title == "Remove apt packages"

        assert group_of["apt:hold:hold-add"].title == "Hold apt packages"
        assert group_of["apt:hold:hold-drop"].title == "Unhold apt packages"
        assert label_of["apt:hold:hold-add"] == "hold"
        assert label_of["apt:hold:hold-drop"] == "unhold"

    @pytest.mark.asyncio
    async def test_unhold_group_is_removal_direction_and_the_hold_group_is_not(self) -> None:
        """B6 — `ReviewGroup.action` is what `review._REMOVAL_ACTIONS` tests to decide whether a
        group's checkboxes default to unticked. Undoing a block the user deliberately set
        needs that friction; adding one does not (#208 D3).
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "hold-add\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "hold-drop\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        group_of = {entry.item_id: group for group in plan.groups for entry in group.entries}
        assert group_of["apt:hold:hold-drop"].action in _REMOVAL_ACTIONS
        assert group_of["apt:hold:hold-add"].action not in _REMOVAL_ACTIONS


class TestValidate:
    @pytest.mark.asyncio
    async def test_all_checks_pass_returns_no_errors(self) -> None:
        # fuser exits 1 (not 0) when the lock file is NOT held (man fuser EXIT CODES) —
        # the "all clear" baseline, unlike every other check here where 0 means success.
        """K45, K70."""
        context, _source, _target = make_context(
            target_responses={"fuser /var/lib/dpkg/lock-frontend": CommandResult(1, "", "")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert errors == []

    @pytest.mark.asyncio
    async def test_apt_mark_unavailable_yields_validation_error(self) -> None:
        """K43, K44."""
        context, _source, _target = make_context(
            target_responses={"apt-mark --version": CommandResult(127, "", "not found")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert any("apt-mark" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_dpkg_lock_held_yields_distinct_validation_error(self) -> None:
        """K69."""
        context, _source, _target = make_context(
            target_responses={"fuser /var/lib/dpkg/lock-frontend": CommandResult(0, "1234", "")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert any("lock" in e.message.lower() for e in errors)

    @pytest.mark.asyncio
    async def test_source_without_passwordless_sudo_yields_validation_error(self) -> None:
        """K41, K46 — Capturing /etc/apt state needs `sudo find` on the SOURCE.

        Without this check the capture degrades to empty digest maps and the sync
        reports success having replicated no repository state at all.
        """
        context, _source, _target = make_context(
            source_responses={"sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required")},
            target_responses={"fuser /var/lib/dpkg/lock-frontend": CommandResult(1, "", "")},
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_target_without_passwordless_sudo_yields_validation_error_naming_the_binaries(self) -> None:
        """K42 — The target error must carry the sudoers remediation, not just a diagnosis:
        every binary the job escalates for has to appear so the user can paste one
        working grant rather than discover the missing paths one failed run at a time.
        """
        context, _source, _target = make_context(
            target_responses={
                "sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required"),
                "fuser /var/lib/dpkg/lock-frontend": CommandResult(1, "", ""),
            },
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        target_sudo_errors = [e for e in errors if e.host is Host.TARGET and "sudo" in e.message]
        assert len(target_sudo_errors) == 1
        assert all(command in target_sudo_errors[0].message for command in TARGET_SUDO_COMMANDS)


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_apt_sync_to_apt_sync_job(self) -> None:
        """K35."""
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("apt_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is AptSyncJob


_POLICY_AVAILABLE = _policy_candidate("https://example.com")
_VENDOR_SOURCES = (
    "Types: deb\nURIs: https://vendor.example.com/apt/\nSuites: stable\n"
    "Components: main\nSigned-By: /etc/apt/keyrings/vendor.gpg\n"
)


class TestRepoRemovalWithheldWhileInUse:
    """`PKG-FR-REPO-DELETE` — a repository the target still gets software from is not a
    review item at all, rather than an item disclosing what its deletion would strand.

    Usage counts every package installed on the target plus the ones it marked
    machine-specific, minus this run's own removal candidates. A marked package is why the
    rule cannot be left to the user's judgement: `filter_inert` drops it from the target
    manifest, so it produces no `ItemDiff` in any run and nothing else in the review would
    connect it to the file about to go.
    """

    @staticmethod
    def _target_responses(
        *,
        source_files: dict[str, str],
        source_digests: str,
        key_digests: str = "",
        decisions: str,
        policy: str,
    ) -> dict[str, CommandResult]:
        """Target responses for a run whose `/etc/apt` state is entirely target-only.

        `_SOURCE_SCAN_CMD` is listed FIRST: `respond_to` matches by substring and first
        match wins, and the scan command contains `find /etc/apt/sources.list.d` too.
        """
        scan = "".join(_scan_line(name, content) for name, content in source_files.items())
        return {
            **_NO_PACKAGES,
            _SOURCE_SCAN_CMD: CommandResult(0, scan, ""),
            "find /etc/apt/sources.list.d": CommandResult(0, source_digests, ""),
            "find /etc/apt/keyrings": CommandResult(0, key_digests, ""),
            "apt.decisions.yaml": CommandResult(0, decisions, ""),
            "apt-cache policy": CommandResult(0, policy, ""),
            **{
                f"cat /etc/apt/sources.list.d/{name}": CommandResult(0, content, "")
                for name, content in source_files.items()
            },
        }

    @pytest.mark.asyncio
    async def test_a_repository_a_machine_specific_package_uses_is_not_raised_at_all(self) -> None:
        """C47, N14 — a repository feeding a package the target marked as its own is not raised
        as an item at all, rather than offered with a warning."""
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions=decision_file("apt:package:vendor-tool"),
                policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert "apt:source:vendor.list" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_a_repository_an_ordinary_target_package_uses_is_withheld_too(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """C48, C170 — usage is not only about marks: a package present on both machines is
        invisible to the review for a different reason (nothing about it differs) and still
        needs its repository. A file withheld from the review reaches the user nowhere else,
        so the log names it and what keeps it.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
            },
            target_responses={
                **self._target_responses(
                    source_files={"vendor.list": _VENDOR_LIST},
                    source_digests=sha256_line("d1", "vendor.list"),
                    decisions="machine_specific: {}\n",
                    policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
                ),
                "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        with caplog.at_level(1):
            plan = await job.plan()

        assert "apt:source:vendor.list" not in {d.item_id for d in plan.diffs}
        assert (
            "keeping repository vendor.list: target-host still installs vendor-tool from it, "
            "so its deletion is not offered" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_a_repository_only_an_automatic_package_uses_is_withheld(self) -> None:
        """C49 — counting the manual set alone offered this file for deletion. Nothing here takes
        `auto-dep` away with it — `remove_args` runs `apt-get remove`, never `autoremove` —
        and a manual package the user keeps can require it, so deleting its only repository
        strands an installed package.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **self._target_responses(
                    source_files={"vendor.list": _VENDOR_LIST},
                    source_digests=sha256_line("d1", "vendor.list"),
                    decisions="machine_specific: {}\n",
                    policy=_policy_block("auto-dep", "https://vendor.example.com/apt"),
                ),
                "db:Status-Status": installed_on_target("auto-dep"),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert "apt:source:vendor.list" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_a_repository_only_this_runs_removals_use_is_offered(self) -> None:
        """N13 — Removing a repository together with the packages it feeds is the legitimate case
        the withholding rule must not swallow, so usage is counted after this run's own
        removal candidates.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **self._target_responses(
                    source_files={"vendor.list": _VENDOR_LIST},
                    source_digests=sha256_line("d1", "vendor.list"),
                    decisions="machine_specific: {}\n",
                    policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
                ),
                "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        assert by_id["apt:package:vendor-tool"].action == DiffAction.REMOVE
        assert by_id["apt:source:vendor.list"].action == DiffAction.REMOVE

    @staticmethod
    def _one_removal_context(*, decisions: dict[str, Decision]) -> tuple[AptSyncJob, MagicMock]:
        """`vendor.list` on the target only, feeding exactly one target package the source
        does not have — so this run proposes to remove `vendor-tool` AND offers the
        repository it is the last user of, in one review.
        """
        target_responses = {
            **TestRepoRemovalWithheldWhileInUse._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions="machine_specific: {}\n",
                policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
            ),
            "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
            "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
        }
        context, _source, target = _repo_context(source_responses=_NO_PACKAGES, target_responses=target_responses)
        job = AptSyncJob(context)
        install_reviewer(job, decisions)
        return job, target

    @pytest.mark.asyncio
    async def test_a_repository_goes_with_the_removal_the_user_approved(self) -> None:
        """C50, N13 — removing a repository together with the packages it feeds is the case the
        withholding rule must not swallow: both were approved, so by the time the file goes
        nothing on the target installs from it.
        """
        job, target = self._one_removal_context(
            decisions={"apt:source:vendor.list": Decision.APPLY, "apt:package:vendor-tool": Decision.APPLY}
        )

        await job.execute()

        assert "sudo rm --force /etc/apt/sources.list.d/vendor.list" in all_calls(target)
        assert any("apt-get remove" in c and "vendor-tool" in c and "--dry-run" not in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_repository_whose_packages_removal_was_declined_is_not_deleted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """C172, N12 — the answer the plan-time reading cannot know: the user approves the
        repository's deletion and declines its package's removal. `vendor-tool` stays, so
        something on the target still installs from the file, and deleting it would strand an
        installed package with no origin. The approval is taken back, not failed, and the
        file is offered again next run.
        """
        job, target = self._one_removal_context(decisions={"apt:source:vendor.list": Decision.APPLY})

        with caplog.at_level(1):
            await job.execute()

        assert "sudo rm --force /etc/apt/sources.list.d/vendor.list" not in all_calls(target)
        assert not any("apt-get remove" in c for c in all_calls(target))
        assert (
            "keeping repository vendor.list: target-host still installs vendor-tool from it, "
            "so its approved deletion is not applied" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_the_machine_specific_package_itself_still_produces_no_diff(self) -> None:
        """C52 — the inertness this detail exists to compensate for must not regress: naming
        the package in a removal's detail is NOT the same as re-proposing it (D-08).
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **self._target_responses(
                    source_files={"vendor.list": _VENDOR_LIST},
                    source_digests=sha256_line("d1", "vendor.list"),
                    decisions=decision_file("apt:package:vendor-tool"),
                    policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
                ),
                "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert "apt:package:vendor-tool" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_deb822_uris_match_the_policy_origin_despite_the_trailing_slash(self) -> None:
        """A20, C51 — a `.sources` file writes `URIs: https://.../apt/` while `apt-cache policy`
        prints the origin without the trailing slash. Verbatim comparison would find no
        link at all, and every repository written the first way would be offered for
        deletion with its packages still installed from it.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.sources": _VENDOR_SOURCES},
                source_digests=sha256_line("d1", "vendor.sources"),
                decisions=decision_file("apt:package:vendor-tool"),
                policy=_policy_block("vendor-tool", "https://vendor.example.com/apt"),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert "apt:source:vendor.sources" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_a_repository_nothing_installs_from_is_offered_with_its_urls(self) -> None:
        """C43, H58 — `other-tool` is machine-specific but was installed from a local `.deb`, so its
        only origin is dpkg's own record: nothing uses the repository, so it is offered —
        and the URLs are the whole detail.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions=decision_file("apt:package:other-tool"),
                policy=_policy_block("other-tool", None),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:source:vendor.list")
        assert diff.action == DiffAction.REMOVE
        assert diff.detail == "target-host would stop getting software from https://vendor.example.com/apt"

    @pytest.mark.asyncio
    async def test_detail_reaches_the_user_through_the_review_entry(self) -> None:
        """C44 — the plan's `ItemDiff` is not what the user reads — `ReviewGroup`/`ReviewEntry`
        is. The removal lands in its own unticked removal group carrying the same text.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions=decision_file("apt:package:other-tool"),
                policy=_policy_block("other-tool", None),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        group = next(g for g in plan.groups if g.action in _REMOVAL_ACTIONS)
        entry = next(e for e in group.entries if e.item_id == "apt:source:vendor.list")
        assert entry.detail is not None and "https://vendor.example.com/apt" in entry.detail

    @pytest.mark.asyncio
    async def test_one_apt_cache_policy_call_regardless_of_package_count(self) -> None:
        """C53 — the phase-wide batching rule: origins for every counted package come from ONE
        `apt-cache policy` run, never one per package.
        """
        names = [f"vendor-tool-{i}" for i in range(12)]
        context, _source, target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses=self._target_responses(
                source_files={"vendor.list": _VENDOR_LIST},
                source_digests=sha256_line("d1", "vendor.list"),
                decisions=decision_file(*(f"apt:package:{name}" for name in names)),
                policy="".join(_policy_block(name, "https://vendor.example.com/apt") for name in names),
            ),
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        policy_calls = [cmd for cmd in all_calls(target) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        assert all(name in policy_calls[0] for name in names)
        assert "apt:source:vendor.list" not in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_no_policy_call_when_nothing_is_offered_for_removal(self) -> None:
        """C54 — nothing extra on the target: the run does not pay for the `apt-cache policy`
        origin lookup. Machine-specific packages exist, so only the removal gate can be
        what stops it.

        The source-file SCAN is not gated the same way and is expected here: which keyrings
        the target's repositories point at is what keeps keys correct on every run, not
        only on a run that offers a removal.
        """
        context, _source, target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                # The target holds a package, so the manifest/origin call (A76) DOES run —
                # which is what makes the absence below about the usage probe rather than
                # about a machine with nothing to ask.
                "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                "apt.decisions.yaml": CommandResult(0, decision_file("apt:package:vendor-tool"), ""),
            },
        )
        job = AptSyncJob(context)

        await job.plan()

        commands = all_calls(target)
        assert len([cmd for cmd in commands if "apt-cache policy" in cmd]) == 1
        assert sum(1 for cmd in commands if _SOURCE_SCAN_CMD in cmd) == 1


_CURL_PIN_FILE = "Package: curl\nPin: version 8.0\nPin-Priority: 1001\n"
_VENDOR_PIN = "Package: *\nPin: origin vendor.example.com\nPin-Priority: 900\n"
_PIN_STANZA_SCAN_CMD = "-exec awk '/^Package:/"
_CURL_PIN_STANZAS = "/etc/apt/preferences.d/curl-pin\tPackage: curl\n"


def _pinned_target_only_package_context(
    **extra_decisions: Decision,
) -> tuple[AptSyncJob, MagicMock, CountingReviewer]:
    """`curl` exists only on the TARGET, and the target's `preferences.d/curl-pin` names it.
    This is the exact shape the retired echo made unremovable and unsilenceable.

    The target answers BOTH `preferences.d` reads — the digest listing this code issues and
    the `Package:` stanza scan it no longer does — and the stanza scan empties once the pin
    file is actually deleted. Answering only the read the current code makes would let an
    implementation that still consults the stanzas pass these tests by accident.
    """
    responses = {
        "echo $HOME": CommandResult(0, "/home/target-user", ""),
        "apt-mark showmanual": CommandResult(0, "curl\n", ""),
        "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
        _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p1", "curl-pin"), ""),
        "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, _CURL_PIN_FILE, ""),
        "apt-get --dry-run remove --assume-yes curl": CommandResult(0, "Remv curl [8.0]\n", ""),
    }
    state = {"pin_deleted": False}

    def _target(cmd: str, **_: object) -> CommandResult:
        if cmd.startswith("sudo rm --force") and "curl-pin" in cmd:
            state["pin_deleted"] = True
        if _PIN_STANZA_SCAN_CMD in cmd:
            return CommandResult(0, "" if state["pin_deleted"] else _CURL_PIN_STANZAS, "")
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        return CommandResult(0, "", "")

    context, _source, target = _repo_context(target_side_effect=_target)
    job = AptSyncJob(context)
    reviewer = CountingReviewer({"apt:package:curl": Decision.APPLY, **extra_decisions})
    job.context = dataclasses.replace(job.context, reviewer=reviewer)
    return job, target, reviewer


class TestAPinNeverSpeaksForAPackage:
    """The defect ADR-020 D-25 closes: a package present only on the target and named by
    any pin stanza produced a `REPORT_ONLY` echo instead of its own removal diff — so it
    could neither be removed nor marked machine-specific, and came back every run.
    """

    @pytest.mark.asyncio
    async def test_a_target_only_package_named_by_a_pin_is_offered_for_removal(self) -> None:
        """C117 — the pin says nothing about the package: it is an ordinary removal item."""
        job, _target, _reviewer = _pinned_target_only_package_context()

        plan = await job.plan()

        curl = next(d for d in plan.diffs if d.item_id == "apt:package:curl")
        assert (curl.diff_class, curl.action) == (DiffClass.EXTRA_ON_TARGET, DiffAction.REMOVE)

    @pytest.mark.asyncio
    async def test_the_removal_reaches_the_user_as_an_actionable_review_entry(self) -> None:
        """C117 — `REPORT_ONLY` was the whole problem: it is shown but carries no verb, so it can
        be neither applied nor recorded skip-always.
        """
        job, _target, reviewer = _pinned_target_only_package_context()

        await job.execute()

        assert "apt:package:curl" in actionable_entry_ids(reviewer.calls[0])

    @pytest.mark.asyncio
    async def test_approving_it_actually_removes_the_package(self) -> None:
        """C117 — and approving it really removes it."""
        job, target, _reviewer = _pinned_target_only_package_context()

        await job.execute()

        assert any(c.startswith("sudo DEBIAN") and "apt-get remove" in c and "curl" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_no_command_asks_the_target_which_packages_its_pins_name(self) -> None:
        """C118 — the stanza scan is gone with the echo. A pin file still travels as a FILE — its
        digest is captured — but nothing parses package names out of it any more.
        """
        job, target, _reviewer = _pinned_target_only_package_context()

        await job.execute()

        assert not any("/^Package:/" in cmd for cmd in all_calls(target))
        assert any(_PIN_DIGEST_CMD in cmd for cmd in all_calls(target))


class TestTwoAnswerRemovals:
    """Rulings 5 and 12: a repository or pin the source no longer has is still reviewed —
    nothing derives a deletion — but with two answers, on its own screen, and with no
    machine-local registry behind it.
    """

    @staticmethod
    def _target_only_repo_state() -> tuple[JobContext, MagicMock, MagicMock]:
        """A target carrying one repository and one pin the source does not have, plus an
        apt-config file that must NOT be swept into the same shape (ruling 11)."""
        return make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "vendor.list"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
                "cat /etc/apt/preferences.d/vendor-pin": CommandResult(0, _VENDOR_PIN, ""),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c9", "99extra"), ""),
            },
        )

    @pytest.mark.asyncio
    async def test_repository_and_pin_removals_get_two_separate_two_answer_screens(self) -> None:
        """C110, C122 — one sentinel, two groups: `_build_review_groups` keys on the item class, so a
        repository deletion and a pin deletion never share a list. Apt config keeps the
        ordinary action value and therefore the ordinary three-way path.
        """
        context, _source, _target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        by_action = {(group.action, group.entries[0].item_id.split(":")[1]): group for group in plan.groups}
        assert (REPO_REMOVAL_REVIEW_ACTION, "source") in by_action
        assert (REPO_REMOVAL_REVIEW_ACTION, "pin") in by_action
        # Both read "remove" in the decision column; what is being removed is the group
        # title's job, which is why the two classes still get two screens.
        assert by_action[(REPO_REMOVAL_REVIEW_ACTION, "source")].entries[0].action_label == "remove"
        assert by_action[(REPO_REMOVAL_REVIEW_ACTION, "pin")].entries[0].action_label == "remove"
        assert "repositories" in by_action[(REPO_REMOVAL_REVIEW_ACTION, "source")].title
        assert "pin files" in by_action[(REPO_REMOVAL_REVIEW_ACTION, "pin")].title
        # Ruling 11: the config file is an ordinary removal, in an ordinary group.
        assert (DiffAction.REMOVE.value, "config") in by_action

    @pytest.mark.asyncio
    async def test_each_two_answer_screen_is_titled_in_correct_english(self) -> None:
        """C57, C110 — the title names the plural of the OBJECT, not a verb phrase with an `s` glued on
        the end — "repositorys" is what the latter produces.
        """
        context, _source, _target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        titles = {group.title for group in plan.groups if group.action == REPO_REMOVAL_REVIEW_ACTION}
        assert titles == {
            "Delete repositories source-host no longer has (apt)",
            "Delete pin files source-host no longer has (apt)",
        }

    @pytest.mark.asyncio
    async def test_a_pin_offered_for_deletion_carries_its_whole_content(self) -> None:
        """C111, H59 — a pin filename says nothing about which vendor it favours or by how much, and the
        filename is all a decision row can show. The file itself is what the answer needs.
        """
        context, _source, target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        pins = next(
            group
            for group in plan.groups
            if group.action == REPO_REMOVAL_REVIEW_ACTION and group.entries[0].item_id.startswith("apt:pin:")
        )
        assert pins.entries[0].content == _VENDOR_PIN
        assert "sudo cat /etc/apt/preferences.d/vendor-pin" in all_calls(target)

    @pytest.mark.asyncio
    async def test_a_pin_read_that_did_not_answer_fails_the_job(self) -> None:
        """C113 — ADR-022: silence from `cat` is not an empty pin file. An empty block on a deletion
        screen is an approval given off nothing at all."""
        context, _source, target = self._target_only_repo_state()
        target.run_command.side_effect = respond_to(
            {
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "vendor.list"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
                "cat /etc/apt/preferences.d/vendor-pin": CommandResult(1, "", "cat: Permission denied"),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c9", "99extra"), ""),
            }
        )

        with pytest.raises(ProbeFailed, match=re.escape("cat /etc/apt/preferences.d/vendor-pin")):
            await AptSyncJob(context).plan()

    @pytest.mark.asyncio
    async def test_a_repository_offered_for_deletion_carries_no_content_block(self) -> None:
        """C58 — its URLs are in the detail line; a second whole-file block would be the same fact
        twice, and a `.sources` body is mostly fields the user is not deciding on."""
        context, _source, _target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        repos = next(
            group
            for group in plan.groups
            if group.action == REPO_REMOVAL_REVIEW_ACTION and group.entries[0].item_id.startswith("apt:source:")
        )
        assert repos.entries[0].content is None
        assert repos.entries[0].detail == (
            "target-host would stop getting software from https://vendor.example.com/apt"
        )

    @pytest.mark.asyncio
    async def test_a_two_answer_group_is_unticked_and_never_offered_permanence(self) -> None:
        """C57, C112, H137 — both halves of the sentinel's contract, read off the real groups this job builds
        rather than a hand-made one: unticked because it is a removal direction, never
        promoted because it is not promotable.
        """
        context, _source, _target = self._target_only_repo_state()

        plan = await AptSyncJob(context).plan()

        two_answer = [group for group in plan.groups if group.action == REPO_REMOVAL_REVIEW_ACTION]
        assert len(two_answer) == 2, "the repository and the pin deletion each need their own screen"
        for group in two_answer:
            assert _is_removal_direction(group.action)
            assert not _is_promotable_group(group.action)

    @pytest.mark.asyncio
    async def test_approving_a_pin_removal_deletes_the_file(self) -> None:
        """C114 — the answer that acts still acts: two answers, not one."""
        context, _source, target = _repo_context(
            target_responses={
                **_NO_PACKAGES,
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
            }
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:pin:vendor-pin": Decision.APPLY})

        await job.execute()

        removals = [c for c in all_calls(target) if c.startswith("sudo rm --force")]
        assert removals == ["sudo rm --force /etc/apt/preferences.d/vendor-pin"]

    @pytest.mark.asyncio
    async def test_a_declined_pin_deletion_is_offered_again_on_the_next_run(self) -> None:
        """C119 — the two-answer screen has no permanent answer, so a pin the user wants on
        the target only is impossible: declining records nothing, and a second run over the
        same two machines offers it again.
        """
        responses = {
            **_NO_PACKAGES,
            _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
            "cat /etc/apt/preferences.d/vendor-pin": CommandResult(0, _VENDOR_PIN, ""),
        }
        context, _source, target = _repo_context(target_responses=responses)
        first = AptSyncJob(context)
        install_reviewer(first, {})

        await first.execute()

        assert not any("mv --force" in cmd and "apt.decisions" in cmd for cmd in all_calls(target))
        assert not any(cmd.startswith("sudo rm --force") for cmd in all_calls(target))

        context, _source, _target = _repo_context(target_responses=responses)
        second_plan = await AptSyncJob(context).plan()

        assert "apt:pin:vendor-pin" in {diff.item_id for diff in second_plan.diffs}

    @pytest.mark.asyncio
    async def test_the_repository_goes_before_the_pin_that_prefers_it(self) -> None:
        """C60 — deletion order is the reverse of the write order (§3.3 step 5): a pin naming an
        origin apt no longer has is a worse intermediate state than a repository nothing
        prefers.
        """
        context, _source, target = _repo_context(
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "vendor.list"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p9", "vendor-pin"), ""),
            }
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:vendor.list": Decision.APPLY, "apt:pin:vendor-pin": Decision.APPLY})

        await job.execute()

        removals = [c for c in all_calls(target) if c.startswith("sudo rm --force") and "/etc/apt/" in c]
        assert removals == [
            "sudo rm --force /etc/apt/sources.list.d/vendor.list",
            "sudo rm --force /etc/apt/preferences.d/vendor-pin",
        ]


class TestAptConfigVocabulary:
    """Ruling 11's other half: `/etc/apt/apt.conf.d` is the one reviewed class that is not
    a package, so every one of its three directions needs its own verb AND its own noun.
    Without both, a config file is announced as "Install/Change/Remove apt packages".
    """

    @staticmethod
    def _all_three_directions() -> JobContext:
        """C120, C121, C122 — one apt-config file per direction: `10add` only on the source, `20update` on
        both with different bytes, `30delete` only on the target."""
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("a1", "10add") + sha256_line("u-new", "20update"), ""
                ),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("u-old", "20update") + sha256_line("d1", "30delete"), ""
                ),
            },
        )
        return context

    @pytest.mark.asyncio
    async def test_each_direction_names_the_config_file_not_a_package(self) -> None:
        """C120."""
        context = self._all_three_directions()

        plan = await AptSyncJob(context).plan()

        by_action = {group.action: group for group in plan.groups if group.entries[0].item_id.startswith("apt:config")}
        assert [(group.title, group.entries[0].action_label) for _action, group in sorted(by_action.items())] == [
            ("Update apt configuration files", "update"),
            ("Add apt configuration files", "add"),
            ("Delete apt configuration files", "delete"),
        ]

    @pytest.mark.asyncio
    async def test_no_apt_config_group_claims_to_be_about_packages(self) -> None:
        """C123, H85 — the measured defect, pinned so it cannot come back through the fallback verb."""
        context = self._all_three_directions()

        plan = await AptSyncJob(context).plan()

        config_groups = [group for group in plan.groups if group.entries[0].item_id.startswith("apt:config")]
        assert len(config_groups) == 3
        assert not any("packages" in group.title for group in config_groups)


class TestRepositoryConflicts:
    """Ruling 6: a repository file present on both machines with different content is
    overwritten silently — EXCEPT when it feeds a package the target recorded
    machine-specific, which is the one `/etc/apt` change the user is still asked about.
    """

    @pytest.mark.asyncio
    async def test_a_changed_repository_with_no_machine_specific_package_is_overwritten_silently(self) -> None:
        """C27 — the ordinary case, and the reason the trigger is narrow: two machines whose
        repository definitions have drifted are meant to converge, not to negotiate.
        """
        context, _source, _target = differing_repo_context(recorded="machine_specific: {}\n")

        plan = await AptSyncJob(context).plan()

        assert not any(group.action == REPO_CONFLICT_REVIEW_ACTION for group in plan.groups)

    @pytest.mark.asyncio
    async def test_a_changed_repository_feeding_a_machine_specific_package_asks_and_shows_both_versions(self) -> None:
        """C28, C29, H56 — the entry carries both whole files, the target's first — the user asked for the
        two versions, not a unified diff — and offers exactly the two answers.
        """
        context, _source, _target = differing_repo_context(recorded=decision_file("apt:package:curl"))

        plan = await AptSyncJob(context).plan()

        group = next(g for g in plan.groups if g.action == REPO_CONFLICT_REVIEW_ACTION)
        entry = group.entries[0]
        assert entry.label == "vendor.list"
        assert entry.versions == (_VENDOR_LIST, _CHANGED_VENDOR)
        assert entry.detail is not None and "curl" in entry.detail

    @pytest.mark.asyncio
    async def test_overwriting_a_conflict_writes_the_sources_version(self) -> None:
        """C34, H26 — answering "overwrite" writes the source's version of the file."""
        context, _source, target = differing_repo_context(recorded=decision_file("apt:package:curl"))
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:conflict:vendor.list": Decision.APPLY})

        await job.execute()

        assert any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/vendor.list") for c in all_calls(target)
        )

    @pytest.mark.asyncio
    async def test_skipping_a_conflict_writes_nothing_and_fails_the_package_that_needed_it(self) -> None:
        """C35, H27 — the coupling §4.3 requires: a skipped conflict is not the same as no conflict.
        The package the user ticked depends on that file for its origin, so installing it
        anyway would deliver the wrong vendor's software — exactly what D-34 exists to stop.
        """
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _CHANGED_VENDOR), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _CHANGED_VENDOR, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                "apt.decisions.yaml": CommandResult(0, decision_file("apt:package:curl"), ""),
                "apt-cache policy": CommandResult(0, _policy_block("curl", "https://vendor.example.com/apt"), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a"}
        assert "/etc/apt/sources.list.d/vendor.list" in failures["apt:package:pkg-a"]
        assert not any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/vendor.list") for c in all_calls(target)
        )
        assert not real_installs(target)

    @pytest.mark.asyncio
    async def test_skipping_a_conflict_fails_every_package_whose_origin_needed_it(self) -> None:
        """C171 — "every approved package" is a set, not one package: both installs took
        their origin from `vendor.list`, so declining the overwrite fails both, each naming
        the file, and neither is installed from the target's own version of the repository.
        """
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://vendor.example.com/apt")
                    + _policy_block("pkg-b", "https://vendor.example.com/apt"),
                    "",
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _CHANGED_VENDOR), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _CHANGED_VENDOR, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                "apt.decisions.yaml": CommandResult(0, decision_file("apt:package:curl"), ""),
                "apt-cache policy": CommandResult(0, _policy_block("curl", "https://vendor.example.com/apt"), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a", "apt:package:pkg-b"}
        for message in failures.values():
            assert "/etc/apt/sources.list.d/vendor.list" in message
        assert not real_installs(target)

    @pytest.mark.asyncio
    async def test_a_differing_repository_no_install_would_write_raises_no_question(self) -> None:
        """C36 — the gate D-37 puts in front of the question: `vendor-tool` is on both
        machines, so no install proposes to write `vendor.list`, and a file this run would
        not write is not a decision the user could act on. Nothing is asked, nothing is
        written, and neither copy is even read.
        """
        context, source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0, _policy_block("vendor-tool", "https://vendor.example.com/apt"), ""
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _CHANGED_VENDOR), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _CHANGED_VENDOR, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "vendor-tool\ncurl\n", ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\ncurl\t8.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                "apt.decisions.yaml": CommandResult(0, decision_file("apt:package:curl"), ""),
                "apt-cache policy": CommandResult(
                    0, _policy_block("vendor-tool", "https://vendor.example.com/apt"), ""
                ),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        assert job._accepted_plan is not None  # pyright: ignore[reportPrivateUsage]
        assert not any(  # pyright: ignore[reportPrivateUsage]
            group.action == REPO_CONFLICT_REVIEW_ACTION
            for group in job._accepted_plan.groups  # pyright: ignore[reportPrivateUsage]
        )
        assert not any(
            "sudo install" in cmd and cmd.endswith("/etc/apt/sources.list.d/vendor.list") for cmd in all_calls(target)
        )
        for machine in (source, target):
            assert not any("cat /etc/apt/sources.list.d/vendor.list" in cmd for cmd in all_calls(machine))

    @pytest.mark.asyncio
    async def test_the_conflict_computation_costs_one_batched_policy_call(self) -> None:
        """C39 — both `/etc/apt` follow-ups share one computation (§4.4): a run whose repository
        deletion has to be judged AND whose conflict has to be triggered asks the target's
        apt about its own packages once, not twice.
        """
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
                "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0, _policy_block("vendor-tool", "https://vendor.example.com/apt"), ""
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _CHANGED_VENDOR), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "vendor.list"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _CHANGED_VENDOR, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "curl\n", ""),
                "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(
                    0, _scan_line("vendor.list", _VENDOR_LIST) + _scan_line("gone.list", _RIVAL_LIST), ""
                ),
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("d-old", "vendor.list") + sha256_line("d9", "gone.list"), ""
                ),
                "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
                "cat /etc/apt/sources.list.d/gone.list": CommandResult(0, _RIVAL_LIST, ""),
                "apt.decisions.yaml": CommandResult(0, decision_file("apt:package:curl"), ""),
                "apt-cache policy": CommandResult(0, _policy_block("curl", "https://vendor.example.com/apt"), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert any(g.action == REPO_CONFLICT_REVIEW_ACTION for g in plan.groups)
        # Exactly two policy calls reach the target at plan time: the manifest/origin one
        # (`capture_target_items`), and ONE shared by both `/etc/apt` follow-ups — never one
        # follow-up call each.
        assert len([c for c in all_calls(target) if "apt-cache policy" in c]) == 2


class TestOneReviewPerRun:
    """Every apt prompt precedes the job's first mutating command, unconditionally."""

    @pytest.mark.asyncio
    async def test_a_package_the_target_had_no_candidate_for_is_installed_in_one_review(self) -> None:
        """A51 — At plan time the target's apt reports no candidate at all; the repository this
        run installs supplies one. The package is classified from the SOURCE's origin and
        the file declaring it, so its actionability never depended on a repository this run
        had not written yet — and one screen is enough.
        """
        policy_results = [CommandResult(0, _POLICY_NO_CANDIDATE, ""), CommandResult(0, _POLICY_AVAILABLE, "")]
        state = {"calls": 0}

        def _target(cmd: str, **_: object) -> CommandResult:
            if "apt-cache policy" in cmd:
                index = min(state["calls"], len(policy_results) - 1)
                state["calls"] += 1
                return policy_results[index]
            for pattern, result in {
                "echo $HOME": CommandResult(0, "/home/target-user", ""),
                "apt-mark showmanual": CommandResult(0, "", ""),
                "test -f": CommandResult(1, "", ""),
                "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
            }.items():
                if pattern in cmd:
                    return result
            return CommandResult(0, "", "")

        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://example.com"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_side_effect=_target,
        )
        job = AptSyncJob(context)
        reviewer = CountingReviewer({"apt:source:foo.sources": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert len(reviewer.calls) == 1
        assert "apt:package:pkg-a" in actionable_entry_ids(reviewer.calls[0])
        assert any(c.startswith("sudo DEBIAN") and "install" in c and "pkg-a" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_run_that_rewrites_etc_apt_still_reviews_exactly_once(self) -> None:
        """H13, H36 — The general property, asserted against the run shape that used to trigger the
        second screen: the pin the user is deleting really is deleted, `/etc/apt` really is
        refreshed, and the user is still asked exactly once.
        """
        job, target, reviewer = _pinned_target_only_package_context(**{"apt:pin:curl-pin": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any(c.startswith("sudo rm --force") and "curl-pin" in c for c in commands)
        assert any(c.startswith("sudo apt-get update") for c in commands)
        assert len(reviewer.calls) == 1
