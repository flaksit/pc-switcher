"""Where a package comes from (D-34) and the post-refresh read-back that enforces it (D-35).

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import dataclasses
import shlex
from collections.abc import Callable, Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.apt_sync.origins import OriginOutcome, OriginPlan
from pcswitcher.jobs.packages.items import DiffAction, DiffClass
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import (
    Decision,
)
from pcswitcher.jobs.packages.sync_core import PackageItemFailures
from pcswitcher.models import CommandResult
from tests.unit.jobs.apt.helpers import (
    _BASELINE_ARCHIVE,
    _POLICY_FIXTURE_SCAN,
    _RIVAL_LIST,
    _SOURCE_SCAN_CMD,
    _UBUNTU_SOURCES_BELNET,
    _VENDOR_LIST,
    DPKG_QUERY_3,
    SHOWMANUAL_3,
    _policy_block,
    _scan_line,
    all_calls,
    index_of,
    install_reviewer,
    make_context,
    real_installs,
    respond_to,
    respond_with_policy_sequence,
    sha256_line,
)
from tests.unit.jobs.test_apt_policy import (
    POLICY_ARCHIVE_CANDIDATE_UNINSTALLED,
    POLICY_MOZILLA_FIREFOX_INSTALLED,
)
from tests.unit.jobs.test_manual_installs_sync import (
    _POLICY_REPO_INSTALLED,
)
from tests.unit.jobs.test_package_sync_core import FakeReviewer


def respond_to_target_apt(
    mapping: dict[str, CommandResult], *, cannot_locate: Sequence[str] = ()
) -> Callable[..., CommandResult]:
    """`respond_to`, plus the one target behaviour the substring fixtures cannot express: a
    real `apt-get --dry-run` exits 100 with `E: Unable to locate package` for a name the
    target's repositories do not carry, and takes the WHOLE batch down with it.

    Name-sensitive on purpose. A blanket `"apt-get --dry-run": CommandResult(100, ...)` entry
    would also fail a rehearsal of packages the target can resolve, so a test could pass
    because the simulation stopped happening rather than because it stopped naming the
    unlocatable package.
    """
    inner = respond_to(mapping)
    unknown = frozenset(cannot_locate)

    def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
        if cmd.startswith("apt-get --dry-run"):
            asked = sorted(unknown & frozenset(shlex.split(cmd)))
            if asked:
                return CommandResult(100, "", f"E: Unable to locate package {asked[0]}\n")
        return inner(cmd, **kwargs)

    return _side_effect


_TARGET_GH_NO_CANDIDATE = "gh:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"


class TestAPackageTheTargetCannotResolveYet:
    """ADR-020 D-34 class 3 at plan time: the repository that supplies the package is derived
    from the package's own approval and written during converge, so the target's apt has no
    candidate for the name while `plan()` runs and refuses to rehearse a transaction naming
    it — with the same exit 100 a held dpkg lock produces (ADR-022 D-01).
    """

    @pytest.mark.asyncio
    async def test_plan_survives_a_candidate_the_targets_apt_cannot_locate(self) -> None:
        """A30 — The phase's flagship scenario: `gh` comes from `cli.github.com` on the source, the
        target has never heard the name, and the batched rehearsal must not abort the run
        before the user is shown anything.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_side_effect=respond_to_target_apt(
                {"apt-mark showmanual": CommandResult(0, "", ""), "apt-cache policy": CommandResult(0, "", "")},
                cannot_locate=["gh"],
            ),
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)]
        assert not [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        # The premise, asserted rather than assumed: this target really does refuse `gh`, so
        # a fixture that quietly lost its exit 100 cannot carry the test on its own.
        assert not (await target.run_command("apt-get --dry-run install gh")).success

    @pytest.mark.asyncio
    async def test_an_explicit_no_candidate_is_excluded_on_the_same_evidence(self) -> None:
        """A31 — apt saying `Candidate: (none)` and apt printing no block at all are different
        answers everywhere else in this job, and the same one here: neither names a version
        the target could install, so neither can enter the rehearsal.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_side_effect=respond_to_target_apt(
                {
                    "apt-mark showmanual": CommandResult(0, "", ""),
                    "apt-cache policy": CommandResult(0, _TARGET_GH_NO_CANDIDATE, ""),
                },
                cannot_locate=["gh"],
            ),
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)]
        assert not [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        # The premise, asserted rather than assumed: this target really does refuse `gh`, so
        # a fixture that quietly lost its exit 100 cannot carry the test on its own.
        assert not (await target.run_command("apt-get --dry-run install gh")).success

    @pytest.mark.asyncio
    async def test_the_resolvable_candidates_are_still_rehearsed_and_still_protected(self) -> None:
        """A narrowing, not a shutdown. `pkg-b` is resolvable, stays in the one batched
        rehearsal alongside nothing else, and its manual collateral still reaches the review
        — which is what the run loses entirely if the whole simulation is skipped instead.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\npkg-b\nother-manual\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\npkg-b\t1.0\nother-manual\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _POLICY_REPO_INSTALLED
                    + _policy_block("pkg-b", _BASELINE_ARCHIVE)
                    + _policy_block("other-manual", _BASELINE_ARCHIVE),
                    "",
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_side_effect=respond_to_target_apt(
                {
                    "apt-mark showmanual": CommandResult(0, "other-manual\n", ""),
                    "dpkg-query": CommandResult(0, "other-manual\t1.0\n", ""),
                    "apt-cache policy": CommandResult(
                        0,
                        _policy_block("pkg-b", _BASELINE_ARCHIVE) + _policy_block("other-manual", _BASELINE_ARCHIVE),
                        "",
                    ),
                    "apt-get --dry-run install --assume-yes --no-install-recommends pkg-b": CommandResult(
                        0, "Inst pkg-b (1.0)\nRemv other-manual [1.0]\n", ""
                    ),
                },
                cannot_locate=["gh"],
            ),
        )

        plan = await AptSyncJob(context).plan()

        simulations = [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        assert simulations == ["apt-get --dry-run install --assume-yes --no-install-recommends pkg-b"]
        assert "apt:collateral:install:remove:other-manual" in {d.item_id for d in plan.diffs}
        assert {d.item_id for d in plan.diffs if d.action == DiffAction.INSTALL} == {
            "apt:package:gh",
            "apt:package:pkg-b",
        }
        # The premise, asserted rather than assumed: adding `gh` to that one command is what
        # a real target refuses, and it is the only reason the command may not name it.
        assert not (await target.run_command("apt-get --dry-run install gh pkg-b")).success
        assert (await target.run_command("apt-get --dry-run install --assume-yes pkg-b")).success


_MOZILLA_SOURCES = (
    "Types: deb\nURIs: https://packages.mozilla.org/apt\nSuites: mozilla\n"
    "Components: main\nSigned-By: /etc/apt/keyrings/packages.mozilla.org.asc\n"
)
_UBUNTU_SOURCES_ARCHIVE = "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu\nSuites: noble\nComponents: main\n"


class TestOriginClassification:
    """ADR-020 D-34 at plan time: a package replicates as (name, origin), so a name the
    target could satisfy from a different vendor is not "already available".
    """

    @pytest.mark.asyncio
    async def test_same_origin_install_derives_no_repository_write(self) -> None:
        """A26, A28, C21 — class 1. The target's own candidate already comes from a place the source uses,
        so nothing about `/etc/apt` has to change for the install to be faithful.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL
        assert job._work.origins.plans["apt:package:pkg-a"].derived_files == frozenset()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_different_origin_install_derives_the_sources_own_repository(self) -> None:
        """A29, C22 — class 2, the Firefox case. The target HAS a candidate for the name — Ubuntu's
        epoch-1 transitional package — and it is not the source's software. Name-only
        matching read this as an ordinary install and shipped the other vendor's package.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "firefox\n", ""),
                "dpkg-query": CommandResult(0, "firefox\t145.0\n", ""),
                "apt-cache policy": CommandResult(0, POLICY_MOZILLA_FIREFOX_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("mozilla.sources", _MOZILLA_SOURCES), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "packages.mozilla.org.asc"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, POLICY_ARCHIVE_CANDIDATE_UNINSTALLED, ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:firefox")
        assert (diff.diff_class, diff.action) == (DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)
        assert diff.detail == "from packages.mozilla.org/apt"
        # The keyring half of the write set is derived at write time from this file's own
        # `Signed-By:`; what the plan owes is the file.
        assert job._work.origins.plans["apt:package:firefox"].derived_files == frozenset({"mozilla.sources"})  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_unreplicable_origin_is_report_only_naming_the_origin(self) -> None:
        """A36, A64, C24 — class 4. The repository the package came from is gone from the source's own
        `/etc/apt`, so there is no file to hand the target and no honest install to offer.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://gone.example.com/apt"), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)
        job.context = dataclasses.replace(job.context, reviewer=FakeReviewer({"apt:package:pkg-a": Decision.APPLY}))

        await job.execute()

        diff = next(d for d in job._accepted_plan.diffs if d.item_id == "apt:package:pkg-a")  # pyright: ignore[reportPrivateUsage, reportOptionalMemberAccess]
        assert (diff.diff_class, diff.action) == (DiffClass.REPO_UNAVAILABLE, DiffAction.REPORT_ONLY)
        assert diff.detail is not None and "gone.example.com/apt" in diff.detail
        assert not any("apt-get install" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_dangling_keyring_makes_the_package_unavailable(self) -> None:
        """A34, C86 — class 4's other half. The source declares the repository but references a key it
        does not have, so the file cannot be written and the origin cannot be delivered —
        and it is the PACKAGE that says so, because the package is what the user decided.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert (diff.diff_class, diff.action) == (DiffClass.REPO_UNAVAILABLE, DiffAction.REPORT_ONLY)
        assert diff.detail is not None and "vendor.gpg" in diff.detail

    @pytest.mark.asyncio
    async def test_one_writable_serving_file_is_enough(self) -> None:
        """A35, C87 — a package served by both a sound repository file and a broken one is replicable:
        the origin only has to be declared once for the target to install from it, so a
        second file with a dangling key must not condemn the package.
        """
        broken = "deb [signed-by=/etc/apt/keyrings/missing.gpg] https://vendor.example.com/apt old main\n"
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(
                    0, _scan_line("broken.list", broken) + _scan_line("vendor.list", _VENDOR_LIST), ""
                ),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL

    @pytest.mark.asyncio
    async def test_a_distribution_origin_install_names_no_origin(self) -> None:
        """A27 — The unremarkable case earns no text: naming the mirror on every archive package
        would bury the two lines that matter under a hundred that do not.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://ftp.belnet.be/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_BELNET), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL
        assert diff.detail is None

    @pytest.mark.asyncio
    async def test_two_machines_on_different_ubuntu_mirrors_produce_no_origin_mismatch(self) -> None:
        """A23, A61 — The suppression that makes the provenance comparison usable at all: each machine's
        distribution origins are read from its OWN distribution files, so a Belgian mirror
        and the default archive are one vendor, not two.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://ftp.belnet.be/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_BELNET), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://archive.ubuntu.com/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_ARCHIVE), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert [d for d in plan.diffs if d.diff_class == DiffClass.ORIGIN_MISMATCH] == []

    @pytest.mark.asyncio
    async def test_divergent_vendor_provenance_reports_origin_mismatch(self) -> None:
        """A59 — The same name and the same version on both machines, from two vendors. A
        presence-and-version diff sees nothing here, which is why this class exists —
        report only, because converging it means a cross-vendor reinstall nobody asked for.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://rival.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("rival.list", _RIVAL_LIST), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert (diff.diff_class, diff.action) == (DiffClass.ORIGIN_MISMATCH, DiffAction.REPORT_ONLY)
        assert diff.detail is not None
        assert "vendor.example.com/apt" in diff.detail
        assert "rival.example.com/apt" in diff.detail

    @pytest.mark.asyncio
    async def test_an_origin_divergence_outranks_a_version_difference(self) -> None:
        """A60 — two vendors AND two versions. Builds from two origins share no version
        scale, so "1.0 against 2.0" would report a difference of degree where the real
        difference is of origin.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t2.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://rival.example.com/apt"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("rival.list", _RIVAL_LIST), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.diff_class == DiffClass.ORIGIN_MISMATCH
        assert diff.detail is not None
        assert "2.0" not in diff.detail

    @pytest.mark.asyncio
    async def test_a_vendor_build_against_a_distribution_build_is_an_origin_divergence(self) -> None:
        """A62 — the requirement's own worked example: `gh` from GitHub's own repository and
        `gh` from Ubuntu's archive are one name and two pieces of software. The distribution
        is ONE origin per machine, not no origin, so the split is a divergence naming both.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("gh", "https://cli.github.com/packages"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.45.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("gh", "http://archive.ubuntu.com/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_ARCHIVE), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:gh")
        assert (diff.diff_class, diff.action) == (DiffClass.ORIGIN_MISMATCH, DiffAction.REPORT_ONLY)
        assert diff.detail is not None
        assert "cli.github.com/packages" in diff.detail
        assert "distribution archive" in diff.detail

    @pytest.mark.asyncio
    async def test_the_same_version_from_a_vendor_and_from_the_distribution_still_diverges(self) -> None:
        """A63 — with the two versions equal there is nothing else left to report, so the
        whole finding rests on the origin comparison alone.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.45.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("gh", "https://cli.github.com/packages"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.45.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("gh", "http://archive.ubuntu.com/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_ARCHIVE), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:gh")
        assert (diff.diff_class, diff.action) == (DiffClass.ORIGIN_MISMATCH, DiffAction.REPORT_ONLY)

    @pytest.mark.asyncio
    async def test_a_credential_in_the_origin_is_withheld_from_the_review(self) -> None:
        """A40 — a private repository carries its password in its own address, and the
        install line names that address. The userinfo is withheld wherever the user reads it.
        """
        private = "deb [signed-by=/etc/apt/keyrings/private.gpg] https://user:pw@repo.example.test/apt stable main\n"
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0, _policy_block("pkg-a", "https://user:pw@repo.example.test/apt"), ""
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("private.list", private), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "private.gpg"), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL
        assert diff.detail is not None
        assert "***@repo.example.test/apt" in diff.detail
        assert "pw" not in diff.detail
        assert "user:" not in diff.detail


class TestOriginOutcome:
    """`OriginPlan.outcome` in isolation, for the branches a whole-plan test cannot reach
    cheaply."""

    def test_apt_silence_on_the_target_does_not_condemn_a_package(self) -> None:
        """`df48cd07`'s rule at the classification level: a policy call that produced no
        block for the name answered nothing, and a run whose probe failed must not report a
        repository problem it never established.
        """
        plan = OriginPlan(target_candidate_known=False)

        assert plan.outcome() is not OriginOutcome.UNREPLICABLE

    def test_an_explicit_no_candidate_with_no_origin_to_replicate_is_unreplicable(self) -> None:
        """A31 — The other half of the same distinction: apt answered, and its answer was no."""
        plan = OriginPlan(target_candidate_known=True)

        assert plan.outcome() is OriginOutcome.UNREPLICABLE

    def test_a_plan_with_no_origin_fact_at_all_still_installs(self) -> None:
        """The degenerate case: nothing captured, nothing to hold the install to."""
        assert OriginPlan().outcome() is OriginOutcome.SAME_ORIGIN


def _policy_calls_after_the_update(target: MagicMock) -> list[str]:
    commands = all_calls(target)
    update = index_of(commands, lambda cmd: "sudo apt-get update" in cmd)
    return [cmd for cmd in commands[update:] if "apt-cache policy" in cmd]


def _mozilla_source_responses() -> dict[str, CommandResult]:
    """A source machine running Mozilla's own `firefox`, with the repository file that
    declares it and the key that file names."""
    return {
        "apt-mark showmanual": CommandResult(0, "firefox\n", ""),
        "dpkg-query": CommandResult(0, "firefox\t145.0\n", ""),
        "apt-cache policy": CommandResult(0, POLICY_MOZILLA_FIREFOX_INSTALLED, ""),
        _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("mozilla.sources", _MOZILLA_SOURCES), ""),
        "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "packages.mozilla.org.asc"), ""),
    }


class TestOriginEnforcement:
    """ADR-020 D-35 at converge time: whatever plan-time classification concluded and
    whatever `/etc/apt` work this run derived, the target may not install a package from a
    vendor the source does not use. Checked against the real post-`apt-get update` state.
    """

    @pytest.mark.asyncio
    async def test_install_is_refused_when_the_post_update_candidate_is_from_the_wrong_origin(self) -> None:
        """A42 — The Firefox defect at its last possible catch point: the source runs Mozilla's
        build, the repository did not land (or did not win), and Ubuntu's epoch-1
        transitional package is still what apt would install. It fails as its own item.
        """
        context, _source, target = make_context(
            source_responses=_mozilla_source_responses(),
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, POLICY_ARCHIVE_CANDIDATE_UNINSTALLED, ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:firefox": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as excinfo:
            await job.execute()

        reasons = [reason for _diff, reason in excinfo.value.failures]
        assert len(reasons) == 1
        assert "packages.mozilla.org/apt" in reasons[0]
        assert "ftp.belnet.be/ubuntu" in reasons[0]
        assert not any("firefox" in cmd for cmd in real_installs(target))

    @pytest.mark.asyncio
    async def test_an_origin_the_converged_target_now_offers_lets_the_install_through(self) -> None:
        """A41 — The same run once the repository and its pin have landed: the verification re-reads
        the target and finds Mozilla's copy, so the install proceeds. This is why the check
        re-reads instead of reusing the plan's answer, which still said Ubuntu's archive.
        """
        context, _source, target = make_context(source_responses=_mozilla_source_responses())
        target.run_command = AsyncMock(
            side_effect=respond_with_policy_sequence(
                {"apt-mark showmanual": CommandResult(0, "", "")},
                [
                    CommandResult(0, POLICY_ARCHIVE_CANDIDATE_UNINSTALLED, ""),
                    CommandResult(0, POLICY_MOZILLA_FIREFOX_INSTALLED, ""),
                ],
            )
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:firefox": Decision.APPLY})

        await job.execute()

        assert [cmd for cmd in real_installs(target) if "firefox" in cmd]

    @pytest.mark.asyncio
    async def test_the_origin_verification_costs_one_batched_policy_call(self) -> None:
        """A45, A46 — Three approved vendor installs, one policy read — never one per package. The
        answer cannot change between two installs of the same run.
        """
        names = ("pkg-a", "pkg-b", "pkg-c")
        vendor_policy = "".join(_policy_block(name, "https://vendor.example.com/apt") for name in names)
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
                "apt-cache policy": CommandResult(0, vendor_policy, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, vendor_policy, ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {f"apt:package:{name}": Decision.APPLY for name in names})

        await job.execute()

        assert len(real_installs(target)) == 3
        assert len(_policy_calls_after_the_update(target)) == 1

    @pytest.mark.asyncio
    async def test_a_distribution_origin_package_is_not_origin_verified(self) -> None:
        """A48 — D-35's exemption. The source has this package from its own Ubuntu mirror, so
        whatever mirror the target answers with is the same vendor — and asking the question
        at all would refuse every package on a pair of machines with different mirrors.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://ftp.belnet.be/ubuntu"), ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_BELNET), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "http://archive.ubuntu.com/ubuntu"), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        assert [cmd for cmd in real_installs(target) if "pkg-a" in cmd]
        assert _policy_calls_after_the_update(target) == []

    @pytest.mark.asyncio
    async def test_a_name_the_answered_verification_skipped_refuses_only_that_install(self) -> None:
        """A43, A44 — Stricter than the plan-time rule on purpose: there, apt's silence leaves the
        install to report its own failure; here the install is the thing being guarded, and a
        guarantee that could not be evaluated has not been met. apt DID answer — it printed a
        block for `pkg-b` — so the silence about `pkg-a` is evidence about `pkg-a` alone, and
        `pkg-b` still installs.
        """
        vendor = "https://vendor.example.com/apt"
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0, _policy_block("pkg-a", vendor) + _policy_block("pkg-b", vendor), ""
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-b", vendor), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as excinfo:
            await job.execute()

        assert [diff.item_id for diff, _reason in excinfo.value.failures] == ["apt:package:pkg-a"]
        assert "no repository at all" in excinfo.value.failures[0][1]
        assert not any("pkg-a" in cmd for cmd in real_installs(target))
        assert [cmd for cmd in real_installs(target) if "pkg-b" in cmd]

    @pytest.mark.asyncio
    async def test_a_verification_probe_that_did_not_answer_fails_once_not_per_package(self) -> None:
        """A49 — The environment broke, not the request. Three approved vendor installs and a
        policy read that exited non-zero: one failure naming the command, never three
        failures blaming three packages' provenance for an apt that never ran.
        """
        names = ("pkg-a", "pkg-b", "pkg-c")
        vendor_policy = "".join(_policy_block(name, "https://vendor.example.com/apt") for name in names)
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
                "apt-cache policy": CommandResult(0, vendor_policy, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                # A complete, ORIGIN-MATCHING answer alongside the failure, so nothing but
                # the exit code can refuse these three: a guard that ignored it would let
                # all three install off output apt never stood behind.
                "apt-cache policy": CommandResult(
                    100, vendor_policy, "E: Could not get lock /var/lib/dpkg/lock-frontend\n"
                ),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {f"apt:package:{name}": Decision.APPLY for name in names})

        with pytest.raises(ProbeFailed) as excinfo:
            await job.execute()

        assert "apt-cache policy pkg-a pkg-b pkg-c" in str(excinfo.value)
        assert "exited 100" in str(excinfo.value)
        assert "lock-frontend" in str(excinfo.value)
        assert real_installs(target) == []

    @pytest.mark.asyncio
    async def test_a_verification_probe_that_printed_nothing_fails_once_not_per_package(self) -> None:
        """A50 — The ambiguous half, resolved toward failing fast: exit 0 and not one block over a
        set apt owes a block for. Indistinguishable from "apt knows none of these", and
        misattributing a broken probe to every package's provenance is the worse reading.
        """
        names = ("pkg-a", "pkg-b", "pkg-c")
        vendor_policy = "".join(_policy_block(name, "https://vendor.example.com/apt") for name in names)
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
                "apt-cache policy": CommandResult(0, vendor_policy, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {f"apt:package:{name}": Decision.APPLY for name in names})

        with pytest.raises(ProbeFailed) as excinfo:
            await job.execute()

        assert "printed no package block" in str(excinfo.value)
        assert real_installs(target) == []

    @pytest.mark.asyncio
    async def test_a_skipped_install_is_never_named_in_the_verification(self) -> None:
        """A47 — The batch is the APPROVED set, not the planned one: a package the user left
        unticked cannot be refused, and must not widen the command either.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t2.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://vendor.example.com/apt")
                    + _policy_block("pkg-b", "https://vendor.example.com/apt"),
                    "",
                ),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://vendor.example.com/apt"), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.SKIP_ONCE})

        await job.execute()

        verification = _policy_calls_after_the_update(target)
        assert len(verification) == 1
        assert "pkg-b" not in verification[0]
