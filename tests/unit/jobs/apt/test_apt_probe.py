"""Every read `apt_sync` issues, and what each answer parses into.

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.apt_sync.items import APT_PREFERENCES_DIR
from pcswitcher.jobs.apt_sync.probe import AptProbe, SourceFileRefs
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import (
    _REMOVAL_ACTIONS,
    Decision,
)
from pcswitcher.models import CommandResult
from tests.unit.jobs.apt.helpers import (
    _BASELINE_ARCHIVE,
    _DEB822_FOO,
    _LEGACY_BAR,
    _NO_PACKAGES,
    _POLICY_FIXTURE_SCAN,
    _SOURCE_SCAN_CMD,
    _VENDOR_LIST,
    DPKG_QUERY_3,
    SHOWMANUAL_3,
    _policy_block,
    _repo_context,
    _scan_line,
    all_calls,
    decision_file,
    differing_repo_context,
    install_reviewer,
    make_context,
    respond_to,
    sha256_line,
    target_offers,
)
from tests.unit.jobs.test_apt_policy import (
    POLICY_INSTALLED_AND_CANDIDATE_DIFFER,
)
from tests.unit.jobs.test_manual_installs_sync import (
    _POLICY_AUTO_DEP,
    _POLICY_HAND_DEB,
    _POLICY_PINNED_NO_CANDIDATE,
    _POLICY_REPO_INSTALLED,
)


class TestCapture:
    """Capture: apt-mark showmanual + one batched dpkg-query call for versions (D-03)."""

    @pytest.mark.asyncio
    async def test_capture_source_items_returns_three_items_with_versions(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            }
        )
        probe = AptProbe(context.source, context.target)

        items, _origins = await probe.capture_source_items()

        assert [item.name for item in items] == ["pkg-a", "pkg-b", "pkg-c"]
        assert [item.version for item in items] == ["1.0", "2.0", "3.0"]

    @pytest.mark.asyncio
    async def test_dpkg_query_used_not_apt_list_installed(self) -> None:
        """Backstop: versions come from dpkg-query, never `apt list --installed`."""
        context, source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            }
        )
        probe = AptProbe(context.source, context.target)

        await probe.capture_source_items()

        commands = all_calls(source)
        assert any("dpkg-query" in cmd for cmd in commands)
        assert not any("apt list" in cmd for cmd in commands)


class TestManifestIsShowmanualOnly:
    """A-10/A-12: the manifest is `apt-mark showmanual` and nothing else. Every other
    guarantee this job makes rests on that — an auto-installed dependency is invisible to
    the model, and an empty source manifest is a mass removal that must stay visible.
    """

    @pytest.mark.asyncio
    async def test_auto_installed_dependency_produces_no_diff_of_any_kind(self) -> None:
        """`libdep` is installed on the source (dpkg knows it) but is not in either
        machine's `showmanual` set, so it is never an item: never installed on the target,
        never removed, never reported. `_resolve_versions` builds items from the
        `showmanual` names alone, which is exactly the mechanism this pins.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nlibdep\t5.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert not any("libdep" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_empty_source_manifest_offers_every_target_package_as_an_unticked_removal(self) -> None:
        """An empty `apt-mark showmanual` on the source means every target package is
        extra. That mass removal must surface as ordinary EXTRA_ON_TARGET/REMOVE items in
        a removal-direction group (unticked by default, D-07), never silently and never
        pre-approved.
        """
        context, _source, _target = make_context(
            source_responses={"apt-mark showmanual": CommandResult(0, "", "")},
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t2.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert {d.item_id for d in plan.diffs} == {"apt:package:pkg-a", "apt:package:pkg-b"}
        assert all(d.diff_class == DiffClass.EXTRA_ON_TARGET and d.action == DiffAction.REMOVE for d in plan.diffs)
        assert len(plan.groups) == 1
        assert plan.groups[0].action in _REMOVAL_ACTIONS
        assert plan.groups[0].title == "Remove apt packages"


class TestHoldPinCapture:
    """collect_hold_sets: apt-mark showhold on BOTH machines. Pins are read no more: they
    are files, not facts about packages (ADR-020 D-25/D-36)."""

    @pytest.mark.asyncio
    async def test_hold_sets_from_both_machines_surface(self) -> None:
        context, _source, _target = make_context(
            source_responses={"apt-mark showhold": CommandResult(0, "pkg-src-held\n", "")},
            target_responses={"apt-mark showhold": CommandResult(0, "pkg-tgt-held\n", "")},
        )
        probe = AptProbe(context.source, context.target)

        source_holds, target_holds = await probe.collect_hold_sets()

        assert source_holds == frozenset({"pkg-src-held"})
        assert target_holds == frozenset({"pkg-tgt-held"})


class TestUnavailableCapture:
    """ONE batched `apt-cache policy` on the target answers every origin question this run
    asks of it, and a package whose origin cannot be provided there is reported rather than
    installed from somewhere else (ADR-020 D-34).
    """

    @pytest.mark.asyncio
    async def test_a_package_no_repository_can_supply_is_reported_not_installed(self) -> None:
        """The source's origin is declared by no file the source still has, and the target's
        apt says it will install nothing: two answers that agree, so the package is reported.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "dpkg-query": CommandResult(0, "brscan3\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("brscan3", "https://gone.example.com/apt"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(
                    0, "brscan3:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n", ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 1
        assert plan.diffs[0].diff_class == DiffClass.REPO_UNAVAILABLE
        assert plan.diffs[0].action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_one_batched_policy_call_covers_every_package(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://vendor.example.com/apt")
                    + _policy_block("pkg-b", "https://vendor.example.com/apt"),
                    "",
                ),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://vendor.example.com/apt")
                    + "pkg-b:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n",
                    "",
                ),
                "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\nInst pkg-b (1.0)\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        policy_calls = [cmd for cmd in all_calls(target) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        assert "pkg-a" in policy_calls[0]
        assert "pkg-b" in policy_calls[0]

        by_id = {diff.item_id: diff for diff in plan.diffs}
        # pkg-a: the target's candidate is already the source's origin -> ordinary install.
        assert by_id["apt:package:pkg-a"].diff_class == DiffClass.MISSING_ON_TARGET
        # pkg-b: the target has no candidate, but the source declares the origin in a file
        # that can travel -> still an install, with that repository derived from it.
        assert by_id["apt:package:pkg-b"].diff_class == DiffClass.MISSING_ON_TARGET
        assert job._work.origins.plans["apt:package:pkg-b"].derived_files == frozenset({"vendor.list"})  # pyright: ignore[reportPrivateUsage]


class TestBareDebPackagesAreNotAptSyncsBusiness:
    """A11/D-18: a package whose INSTALLED version comes from no configured repository was
    put there with `dpkg --install`, so apt cannot install it anywhere and the target's apt has
    never heard the name. `manual_installs_sync` offers it as an install snippet in the same
    run; `apt_sync` drops it at CAPTURE, so it is structurally absent from every downstream
    stage rather than filtered out of each one.

    Both jobs read the same real `apt-cache policy` blocks, imported rather than copied:
    the point of the ruling is that the two answer the predicate identically, which a
    paraphrased fixture would stop proving.
    """

    @pytest.mark.asyncio
    async def test_bare_deb_package_produces_no_diff_and_no_review_entry(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.129.1-1784303641\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert list(plan.diffs) == []
        assert not any("code" in entry.item_id for group in plan.groups for entry in group.entries)

    @pytest.mark.asyncio
    async def test_bare_deb_package_reaches_no_apt_get_install(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.129.1-1784303641\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:code": Decision.APPLY})

        await job.execute()

        assert not any("apt-get install" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_repo_installed_package_is_still_captured_and_diffed(self) -> None:
        """The guard against over-excluding: `gh`'s block also carries a
        `/var/lib/dpkg/status` line, as every installed package's does."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.item_id, d.diff_class, d.action) for d in plan.diffs] == [
            ("apt:package:gh", DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)
        ]

    @pytest.mark.asyncio
    async def test_one_source_policy_call_covers_the_whole_manual_set(self) -> None:
        policy = _POLICY_HAND_DEB + _POLICY_REPO_INSTALLED + _POLICY_PINNED_NO_CANDIDATE + _POLICY_AUTO_DEP
        context, source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\ngh\ndocker.io\n7zip\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.0\ngh\t2.96.0\ndocker.io\t29.1\n7zip\t23.01\n", ""),
                "apt-cache policy": CommandResult(0, policy, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        policy_calls = [cmd for cmd in all_calls(source) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        for name in ("code", "gh", "docker.io", "7zip"):
            assert name in policy_calls[0]
        # Only `code` is hand-installed; the negatively-pinned and auto-dependency packages
        # both have repository origins and stay apt_sync's to install.
        assert {d.item_id for d in plan.diffs} == {
            "apt:package:gh",
            "apt:package:docker.io",
            "apt:package:7zip",
        }

    @pytest.mark.asyncio
    async def test_excluded_package_reaches_neither_the_simulation_nor_the_availability_probe(self) -> None:
        """Both downstream target reads are built from the diffs, so a package excluded at
        capture cannot appear in the transaction `_collect_plan_time_collateral` asks apt to
        rehearse, nor in the target's origin probe."""
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\ngh\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.0\ngh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB + _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                # Both names offered by the target, so nothing but the capture-time exclusion
                # can keep `code` out of either downstream read.
                "apt-cache policy": CommandResult(0, target_offers("code", "gh"), ""),
            },
        )

        await AptSyncJob(context).plan()

        simulations = [cmd for cmd in all_calls(target) if "apt-get --dry-run" in cmd]
        assert simulations and all("code" not in cmd for cmd in simulations)
        assert any("gh" in cmd for cmd in simulations)

        probes = [cmd for cmd in all_calls(target) if "apt-cache policy" in cmd]
        assert probes and all("code" not in cmd for cmd in probes)
        assert any("gh" in cmd for cmd in probes)

    @pytest.mark.asyncio
    async def test_repo_installed_package_the_target_has_never_heard_of_is_still_offered(self) -> None:
        """The target half is untouched (`collect_target_policy`): the target's apt
        printing no block is still "no evidence against", because a repository this same run
        adds may be about to supply the package."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                _SOURCE_SCAN_CMD: CommandResult(0, _POLICY_FIXTURE_SCAN, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, "", "N: Unable to locate package gh\n"),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)]

    @pytest.mark.asyncio
    async def test_a_name_an_answered_policy_printed_no_block_for_is_not_excluded(self) -> None:
        """Silence inside an ANSWERED probe is not evidence: apt spoke, and it said nothing
        about `ghost-pkg`, which is not the same as saying it came from no repository.
        Indicting on that absence would drop the package from the sync without a word.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\nghost-pkg\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\nghost-pkg\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", _BASELINE_ARCHIVE), ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert [d.item_id for d in plan.diffs] == ["apt:package:pkg-a", "apt:package:ghost-pkg"]

    @pytest.mark.asyncio
    async def test_a_source_policy_that_did_not_run_fails_the_run_naming_the_command(self) -> None:
        """The other side of the same distinction, and a deliberate reversal: a policy read
        that EXITED NON-ZERO answered nothing about any package. Tolerating it silently
        exempted every package from the D-35 origin check and offered
        `manual_installs_sync`'s bare-`.deb` packages as apt installs, both without a word.

        The stdout is a COMPLETE, parseable block on purpose: it isolates the exit code as
        the only thing that can catch this, so the zero-block rule cannot pass the test on
        the exit code's behalf.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    100, _policy_block("pkg-a", _BASELINE_ARCHIVE), "E: could not read the package lists\n"
                ),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-cache policy pkg-a" in str(excinfo.value)
        assert "exited 100" in str(excinfo.value)
        assert "could not read the package lists" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_source_policy_that_printed_nothing_at_all_fails_the_run(self) -> None:
        """Exit 0 and no block for a single name apt must know. Measured: apt prints one
        block per installed name it is asked about, so this output is not apt's answer.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, "", ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "printed no package block" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_excluded_bare_deb_package_is_not_protected_from_collateral(self) -> None:
        """`code` is a bare `.deb` on the source, so it is dropped from the manifest, and it
        is auto on the target. Under ADR-020 D-40 the target's apt owns it: an install whose
        simulation would remove it proceeds with no collateral item and no prompt.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "code\ngh\n", ""),
                "dpkg-query": CommandResult(0, "code\t1.0\ngh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB + _POLICY_REPO_INSTALLED, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get --dry-run install --assume-yes --no-install-recommends gh": CommandResult(
                    0, "Inst gh (2.96.0)\nRemv code [1.0]\n", ""
                ),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert not any(d.item_id == "apt:collateral:code" for d in plan.diffs)


class TestRepoStateCapture:
    """AptSyncJob.plan() extended with the `/etc/apt` directions that still have a review
    line (D-11/D-13, ADR-020 D-37): repository and pin REMOVALS, apt config in all three.
    """

    @pytest.mark.asyncio
    async def test_deb822_and_legacy_source_each_record_own_format(self) -> None:
        """The format is still recorded, on the one direction that still shows a file to
        the user: a legacy `.list` and a deb822 `.sources` offered for deletion read as
        two distinguishable entries rather than two bare filenames.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("d1", "foo.sources") + sha256_line("d2", "bar.list"), ""
                ),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "cat /etc/apt/sources.list.d/bar.list": CommandResult(0, _LEGACY_BAR, ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        foo_diff = by_id["apt:source:foo.sources"]
        bar_diff = by_id["apt:source:bar.list"]
        assert "deb822" in foo_diff.label
        assert "list" in bar_diff.label
        assert (foo_diff.item_class, foo_diff.action) == (ItemClass.APT_SOURCE, DiffAction.REMOVE)
        assert (bar_diff.item_class, bar_diff.action) == (ItemClass.APT_SOURCE, DiffAction.REMOVE)

    @pytest.mark.asyncio
    async def test_content_hydration_reads_use_sudo_matching_the_digest_capture(self) -> None:
        """WR-04 regression: content reads for diff hydration must use the same
        `sudo`-qualified privilege as the digest capture (`sudo find ... sha256sum`),
        not a plain unprivileged `cat` — otherwise a source file locked down to
        `0600`-or-similar digests correctly (root) but reads back empty (unprivileged),
        and the entry the user is asked to delete claims the wrong format.
        """
        context, _source, target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
            },
        )
        job = AptSyncJob(context)

        await job.plan()

        commands = all_calls(target)
        assert any(cmd == "sudo cat /etc/apt/sources.list.d/foo.sources" for cmd in commands)
        assert not any(cmd == "cat /etc/apt/sources.list.d/foo.sources" for cmd in commands)

    @pytest.mark.asyncio
    async def test_a_repository_never_appears_as_a_review_entry_in_the_add_or_change_direction(self) -> None:
        """Ruling 4's property, in both directions at once and across both file classes:
        `new.sources` is missing on the target, `changed.sources` differs, `new-pin` and
        `changed-pin` likewise. Under the old model that is four review entries; under
        derivation the user is asked about none of them.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("s1", "new.sources") + sha256_line("s2-new", "changed.sources"), ""
                ),
                "find /etc/apt/preferences.d": CommandResult(
                    0, sha256_line("p1", "new-pin") + sha256_line("p2-new", "changed-pin"), ""
                ),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("s2-old", "changed.sources"), ""),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p2-old", "changed-pin"), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert plan.diffs == ()
        assert plan.groups == ()

    @pytest.mark.asyncio
    async def test_pin_and_config_diff_missing_extra_and_changed(self) -> None:
        """The split ruling 11 makes: a pin keeps only the removal direction, apt config
        keeps all three, and the two live side by side in one plan.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("c1", "99update") + sha256_line("c2-new", "80retain"), ""
                ),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(
                    0, sha256_line("p2", "curl-pin") + sha256_line("p3", "extra-pin"), ""
                ),
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("c2-old", "80retain") + sha256_line("c3", "99extra"), ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        assert "apt:pin:curl-pin" not in by_id, "a differing pin is overwritten, never reviewed"
        assert by_id["apt:pin:extra-pin"].diff_class == DiffClass.EXTRA_ON_TARGET
        assert by_id["apt:pin:extra-pin"].action == DiffAction.REMOVE
        assert by_id["apt:config:99update"].action == DiffAction.INSTALL
        assert by_id["apt:config:80retain"].action == DiffAction.CHANGE
        assert by_id["apt:config:99extra"].action == DiffAction.REMOVE


_FILTERED_SOURCES_FIND = "-name '*.list' -o -name '*.sources'"
_SOURCES_LIST_DIGEST_CMD = "sudo sha256sum /etc/apt/sources.list"


class TestWhatAptItselfReads:
    """The capture is scoped to the files apt reads, on both machines (ADR-020 D-11)."""

    @pytest.mark.asyncio
    async def test_a_save_file_in_sources_list_d_is_never_captured(self) -> None:
        """Ubuntu's own tooling leaves `.save`/`.curtin.orig` copies beside the real files.
        apt reads neither, so neither may reach the review — the target-only copy below
        would otherwise be offered for deletion as a repository the source lacks.
        """
        unfiltered = sha256_line("d1", "vendor.list") + sha256_line("d2", "vendor.list.save")
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                _FILTERED_SOURCES_FIND: CommandResult(0, sha256_line("d1", "vendor.list"), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, unfiltered, ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        item_ids = {d.item_id for d in plan.diffs}
        assert "apt:source:vendor.list.save" not in item_ids
        assert "apt:source:vendor.list" in item_ids

    @pytest.mark.asyncio
    async def test_preferences_d_and_apt_conf_d_keep_no_extension_filter(self) -> None:
        """apt reads extensionless files in both (six of them in `preferences.d` on the
        development machine), so the narrowing that is right for `sources.list.d` is wrong
        here — on either machine.
        """
        context, source, target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "no-esm-docker"), ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        unfiltered = [
            cmd
            for machine in (source, target)
            for cmd in all_calls(machine)
            if "-exec sha256sum" in cmd and ("/etc/apt/preferences.d" in cmd or "/etc/apt/apt.conf.d" in cmd)
        ]
        # Both directories, both machines.
        assert len(unfiltered) == 4
        assert not any("-name" in cmd for cmd in unfiltered)
        assert "apt:pin:no-esm-docker" in {d.item_id for d in plan.diffs}

    @pytest.mark.asyncio
    async def test_sources_list_is_digested_on_both_machines_and_is_still_not_an_item(self) -> None:
        """`/etc/apt/sources.list` is a file, not a directory, so it appears in no `find`
        listing and needs its own digest — which ADR-020 D-38's write-when-different rule
        compares. Capturing it must not turn it into a reviewable item.
        """
        context, source, target = make_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCES_LIST_DIGEST_CMD: CommandResult(0, sha256_line("s1", "/etc/apt/sources.list"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCES_LIST_DIGEST_CMD: CommandResult(0, sha256_line("s2", "/etc/apt/sources.list"), ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert job._work.source_facts.sources_list_digest == "s1"  # pyright: ignore[reportPrivateUsage]
        assert job._work.target_facts.sources_list_digest == "s2"  # pyright: ignore[reportPrivateUsage]
        assert sum(1 for cmd in all_calls(source) if _SOURCES_LIST_DIGEST_CMD in cmd) == 1
        assert sum(1 for cmd in all_calls(target) if _SOURCES_LIST_DIGEST_CMD in cmd) == 1
        assert not any(d.item_id.endswith(":sources.list") for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_an_absent_sources_list_yields_no_digest_rather_than_an_error(self) -> None:
        """Verified on the development machine: `sha256sum` on a missing path exits 1 and
        prints nothing to stdout, so absence falls out of the parse with no probe.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCES_LIST_DIGEST_CMD: CommandResult(1, "", "sha256sum: /etc/apt/sources.list: No such file\n"),
            },
            target_responses=_NO_PACKAGES,
        )
        job = AptSyncJob(context)

        await job.plan()

        assert job._work.source_facts.sources_list_digest is None  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_ubuntu_sources_is_never_offered_for_removal(self) -> None:
        """D-38: the distribution's own files are written and updated but never removed.
        A target holding `ubuntu.sources` and `ubuntu-esm-apps.sources` that the source does
        not have would otherwise be offered a deletion of its own archive, while a
        `.sources` file with a lookalike name is an ordinary repository and still is.
        """
        target_listing = (
            sha256_line("d1", "ubuntu.sources")
            + sha256_line("d2", "ubuntu-esm-apps.sources")
            + sha256_line("d3", "ubuntu-esm-mine.sources")
        )
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, target_listing, ""),
                "cat /etc/apt/sources.list.d/": CommandResult(0, "Types: deb\nURIs: http://x.example.com\n", ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert {d.item_id for d in plan.diffs} == {"apt:source:ubuntu-esm-mine.sources"}

    @pytest.mark.asyncio
    async def test_the_distribution_files_are_written_when_they_differ(self) -> None:
        """The other half of D-38's always-sync bucket, `/etc/apt/sources.list` included —
        it is a file rather than a directory entry and so travels on its own digest. An
        ordinary vendor repository that feeds no approved package stays put, which is
        ruling 4 working as intended.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(
                    0, sha256_line("d1", "ubuntu.sources") + sha256_line("d9", "vendor.list"), ""
                ),
                _SOURCES_LIST_DIGEST_CMD: CommandResult(0, sha256_line("s1", "/etc/apt/sources.list"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCES_LIST_DIGEST_CMD: CommandResult(0, sha256_line("s2", "/etc/apt/sources.list"), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        promoted = [c.rsplit(" ", 1)[1] for c in all_calls(target) if c.startswith("sudo install --owner=root")]
        assert promoted == ["/etc/apt/sources.list.d/ubuntu.sources", "/etc/apt/sources.list"]

    @pytest.mark.asyncio
    async def test_a_dry_run_previews_the_derived_writes_and_issues_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A derived write has no review entry, so without a preview line ADR-014's
        rehearsal would report an `apt-get update` and no reason for it.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "mozilla"), ""),
            },
            target_responses={**_NO_PACKAGES},
            dry_run=True,
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        with caplog.at_level(1):
            await job.execute()

        assert "[dry-run] Would write /etc/apt/preferences.d/mozilla from source-host" in caplog.text
        assert not any(c.startswith("sudo install") for c in all_calls(target))
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_source_file_scan_runs_against_both_machines(self) -> None:
        """The scan is machine-agnostic and both answers are load-bearing: the target's
        drives keyring reference counting and the removal impact, the source's is what maps
        a package's origin URIs back to the repository file that would have to travel
        (ADR-020 D-34).
        """
        context, source, target = make_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
            },
            target_responses=_NO_PACKAGES,
        )
        job = AptSyncJob(context)

        await job.plan()

        assert sum(1 for cmd in all_calls(source) if _SOURCE_SCAN_CMD in cmd) == 1
        assert sum(1 for cmd in all_calls(target) if _SOURCE_SCAN_CMD in cmd) == 1
        refs, uris = job._work.source_facts.refs.by_filename["vendor.list"]  # pyright: ignore[reportPrivateUsage]
        assert uris == ("https://vendor.example.com/apt",)
        assert refs == ("/etc/apt/keyrings/vendor.gpg",)


class TestOriginCapture:
    """ADR-020 D-34's origin facts: where the source installed each package from, which
    repository file on the source declares that place, and which places are the
    distribution's own.
    """

    def test_source_files_serving_is_the_union_of_every_file_declaring_an_origin(self) -> None:
        """A package's installed version can list several origins and each may be declared
        by a different file — every one of them served it, so none may be dropped.
        """
        refs = {
            "vendor.list": ((), ("https://vendor.example.com/apt",)),
            "mirror.sources": ((), ("https://mirror.example.com/apt",)),
            "unrelated.list": ((), ("https://elsewhere.example.com/apt",)),
        }

        serving = SourceFileRefs(by_filename=refs).files_serving(
            frozenset({"https://vendor.example.com/apt", "https://mirror.example.com/apt"})
        )

        assert serving == frozenset({"vendor.list", "mirror.sources"})

    def test_an_origin_no_file_declares_serves_from_nowhere(self) -> None:
        """The class-4 input: a repository deleted from the source while its packages stay
        installed leaves an origin with no file behind it.
        """
        refs = {"vendor.list": ((), ("https://vendor.example.com/apt",))}

        serving = SourceFileRefs(by_filename=refs).files_serving(frozenset({"https://gone.example.com/apt"}))

        assert serving == frozenset()

    def test_distribution_origins_come_from_the_machines_own_distribution_files(self) -> None:
        """Per machine, from that machine's `ubuntu.sources`/`sources.list`/ESM files — not
        from a list of known Ubuntu hostnames, which is what would make two machines on
        different mirrors disagree about every package.
        """
        refs = {
            "ubuntu.sources": ((), ("http://ftp.belnet.be/ubuntu", "http://security.ubuntu.com/ubuntu")),
            "ubuntu-esm-apps.sources": ((), ("https://esm.ubuntu.com/apps/ubuntu",)),
            "sources.list": ((), ("http://old.example.com/ubuntu",)),
            "vendor.list": ((), ("https://vendor.example.com/apt",)),
        }

        assert SourceFileRefs(by_filename=refs).distribution_origins() == frozenset(
            {
                "http://ftp.belnet.be/ubuntu",
                "http://security.ubuntu.com/ubuntu",
                "https://esm.ubuntu.com/apps/ubuntu",
                "http://old.example.com/ubuntu",
            }
        )

    def test_a_user_named_esm_lookalike_is_not_a_distribution_file(self) -> None:
        """Exact filenames, not a `ubuntu-esm-*` glob: a file the user named that way is
        theirs, and treating its URIs as the distribution's would suppress the origin from
        every review line it feeds.
        """
        refs = {"ubuntu-esm-mine.sources": ((), ("https://mine.example.com/apt",))}

        assert SourceFileRefs(by_filename=refs).distribution_origins() == frozenset()

    @pytest.mark.asyncio
    async def test_the_source_policy_call_answers_both_questions_asked_of_it(self) -> None:
        """One batched `apt-cache policy` on the source, parsed twice: the bare-`.deb`
        exclusion and the installed-origin map. A second call would re-run a full policy
        query to learn something already on screen.
        """
        context, source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\ncode\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\ncode\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0, _policy_block("pkg-a", "https://vendor.example.com/apt") + _policy_block("code", None), ""
                ),
            },
            target_responses=_NO_PACKAGES,
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert sum(1 for cmd in all_calls(source) if "apt-cache policy" in cmd) == 1
        assert job._work.origins.plans["apt:package:pkg-a"].source_origins == frozenset(
            {"https://vendor.example.com/apt"}
        )  # pyright: ignore[reportPrivateUsage]
        # `code` came from no repository, so it is dropped at capture and never diffed.
        assert [diff.item_id for diff in plan.diffs] == ["apt:package:pkg-a"]

    @pytest.mark.asyncio
    async def test_the_source_origin_map_holds_the_installed_row_not_the_candidate_one(self) -> None:
        """The distinction the whole classification rests on: what the source HAS, not what
        the source would install next.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "gh\n", ""),
                "dpkg-query": CommandResult(0, "gh\t2.96.0\n", ""),
                "apt-cache policy": CommandResult(0, POLICY_INSTALLED_AND_CANDIDATE_DIFFER, ""),
            },
            target_responses=_NO_PACKAGES,
        )
        job = AptSyncJob(context)

        await job.plan()

        assert job._work.origins.plans["apt:package:gh"].source_origins == frozenset(
            {"https://cli.github.com/packages"}
        )  # pyright: ignore[reportPrivateUsage]


class TestAReadThatDidNotAnswer:
    """ADR-022, applied to the reads that build the two manifests and the `/etc/apt`
    picture: a read that did not answer fails the job naming the command, a read that
    answered "nothing" is data.

    Which of the two an empty result is depends on the command, and every test here isolates
    exactly one read: everything else in the fixture answers normally, so nothing but the
    named read can produce the outcome.
    """

    @pytest.mark.asyncio
    async def test_a_source_manual_set_read_that_did_not_answer_fails_the_job(self) -> None:
        """Measured: `apt-mark showmanual` exits 100 when it cannot read `/var/lib/dpkg/
        status` or parse `apt.conf.d`. Reading that silence as data makes the source
        manifest empty, which offers every package on the target for removal.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(100, "", "E: Problem opening /var/lib/dpkg/status\n")
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\n", ""),
            },
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-mark showmanual" in str(excinfo.value)
        assert "exited 100" in str(excinfo.value)
        assert "/var/lib/dpkg/status" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_empty_source_manual_set_at_exit_zero_is_still_data(self) -> None:
        """The deliberate limit of the rule above, pinned so it is not silently widened
        later: the guard is on the EXIT CODE, and an empty answer at exit 0 still reaches
        the diff as "remove the target's packages". Widening it to "empty means broken"
        would fail every run against a machine whose manual set is legitimately empty.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-x\n", ""),
                "dpkg-query": CommandResult(0, "pkg-x\t1.0\n", ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        removals = {d.item_id for d in plan.diffs if d.action == DiffAction.REMOVE}
        assert removals == {"apt:package:pkg-x"}

    @staticmethod
    def _target_failing_nth_showmanual(n: int) -> Callable[..., CommandResult]:
        """A target whose n-th (1-based) `apt-mark showmanual` fails and whose others
        answer normally.

        `plan()` asks the target that ONE command twice — the manifest read, then the
        collateral protection set — and a substring fixture cannot tell the two apart. A
        fixture that failed both would pass on either guard's behalf, which is exactly the
        vacuous shape these two tests exist to avoid.
        """
        state = {"calls": 0}
        inner = respond_to({"dpkg-query": CommandResult(0, "pkg-x\t1.0\n", "")})

        def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
            if "apt-mark showmanual" in cmd:
                state["calls"] += 1
                if state["calls"] == n:
                    return CommandResult(100, "", "E: Could not open lock file\n")
                return CommandResult(0, "pkg-x\n", "")
            return inner(cmd, **kwargs)

        return _side_effect

    @pytest.mark.asyncio
    async def test_a_target_manifest_read_that_did_not_answer_fails_the_job(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            }
        )
        target.run_command = AsyncMock(side_effect=self._target_failing_nth_showmanual(1))

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-mark showmanual" in str(excinfo.value)
        assert "target" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_collateral_protection_read_that_did_not_answer_fails_the_job(self) -> None:
        """The second of the two. Its silence empties the target's manual set, which
        classifies every collateral package as automatic and switches D-30's protection off
        entirely — the manifest read above it answers normally, so only this one can fail.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            }
        )
        target.run_command = AsyncMock(side_effect=self._target_failing_nth_showmanual(2))

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-mark showmanual" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_version_read_that_did_not_answer_fails_the_job(self) -> None:
        """A `dpkg-query` that does not answer leaves every version empty, which reads as a
        version difference against the other machine on every package at once.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(1, "", "dpkg-query: error: unable to access the database\n"),
            },
            target_responses=_NO_PACKAGES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "dpkg-query --show" in str(excinfo.value)
        assert "exited 1" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_hold_read_that_did_not_answer_fails_the_job(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(100, "", "E: The package lists could not be parsed\n"),
            },
            target_responses=_NO_PACKAGES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-mark showhold" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_empty_hold_set_is_data_not_a_failure(self) -> None:
        """Holding nothing is what most machines do, so an empty `apt-mark showhold` at
        exit 0 must stay ordinary data — the plan completes and proposes no hold.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-mark showhold": CommandResult(0, "", ""),
            },
            target_responses={**_NO_PACKAGES, "apt-mark showhold": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert not [d for d in plan.diffs if d.item_class == ItemClass.APT_HOLD]

    @pytest.mark.asyncio
    async def test_a_target_policy_read_that_did_not_answer_fails_the_job(self) -> None:
        """The source has a package, so the only `apt-cache policy` the TARGET is asked at
        plan time is `collect_target_policy`'s.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "apt-cache policy": CommandResult(100, "", "E: Could not get lock /var/lib/dpkg/lock-frontend\n"),
            },
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-cache policy pkg-a" in str(excinfo.value)
        assert "lock-frontend" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_target_policy_that_knows_none_of_the_source_names_is_data(self) -> None:
        """The `blocks` half of the apt guard is deliberately NOT applied here: these are
        the SOURCE's names asked of the TARGET's apt, and a target that has never heard of
        any of them is the ordinary case this call exists to detect. It must still plan.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={**_NO_PACKAGES, "apt-cache policy": CommandResult(0, "", "")},
        )

        plan = await AptSyncJob(context).plan()

        assert {d.item_id for d in plan.diffs} == {"apt:package:pkg-a"}

    @pytest.mark.asyncio
    async def test_a_directory_digest_read_that_did_not_answer_fails_the_job(self) -> None:
        """`sudo find <dir> ... sha256sum` on the source keyrings directory. Its silence
        empties `_source_key_filenames`, which makes every `Signed-By:` reference look
        dangling.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(1, "", "find: '/etc/apt/keyrings': Permission denied\n"),
            },
            target_responses=_NO_PACKAGES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "find /etc/apt/keyrings" in str(excinfo.value)
        assert "Permission denied" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_absent_directory_answers_nothing_rather_than_failing(self) -> None:
        """The `sudo test -d` wrapper is what keeps a legitimately absent directory out of
        the failure path: it is what makes the command exit 0 with no output, which this
        asserts is planned through rather than raised on.

        The `sudo` on the TEST is pinned as tightly as the wrapper itself: an unprivileged
        `test -d` on a directory inside an unsearchable parent exits 1 and collapses the
        whole `if` to exit 0 with no output, which is the reshape answering "this machine
        has no pins" for a directory root would have listed.
        """
        context, _source, _target = make_context(source_responses=_NO_PACKAGES, target_responses=_NO_PACKAGES)
        job = AptSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert any(
            c.startswith(f"if sudo test -d {APT_PREFERENCES_DIR}; then sudo find {APT_PREFERENCES_DIR}")
            for c in all_calls(_source)
        )

    @pytest.mark.asyncio
    async def test_the_source_file_scan_selects_both_locations_from_one_start_point(self) -> None:
        """The shape of the scan, pinned verbatim, because the shape IS the classification:
        `/etc/apt` is the one start point whose existence apt guarantees, and the two
        locations are `-path` selectors under it.

        Naming `/etc/apt/sources.list` as a start point instead makes find exit 1 while
        still walking the directory when that file is absent, which is the same exit code a
        scan that could not run at all produces — and the scan's silence deletes keys that
        are still in use. A "simplification" back to two start points is the specific edit
        this asserts against, and no substring of the awk program can catch it.
        """
        context, _source, _target = make_context(source_responses=_NO_PACKAGES, target_responses=_NO_PACKAGES)

        await AptSyncJob(context).plan()

        scans = [c for c in all_calls(_source) if "-exec awk" in c]
        assert len(scans) == 1
        assert scans[0].startswith(
            "sudo find /etc/apt -maxdepth 2 -type f "
            "\\( -path /etc/apt/sources.list -o -path '/etc/apt/sources.list.d/*' \\) -exec awk "
        )

    @pytest.mark.asyncio
    async def test_a_source_file_scan_that_did_not_answer_fails_the_job(self) -> None:
        """The scan's silence reads as "no source file references any keyring", which is
        what deletes keys that are still in use.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(1, "", "find: '/etc/apt': No such file or directory\n"),
            },
            target_responses=_NO_PACKAGES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "-exec awk" in str(excinfo.value)
        assert "No such file or directory" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_conflict_content_read_that_did_not_answer_fails_the_job(self) -> None:
        """The two panes the repository-conflict review shows are `sudo cat` output
        (ADR-020 D-37). Reading that silence as CONTENT renders the source's pane empty and asks
        the user to approve an overwrite off a diff nobody could read. The TARGET's `cat`
        runs first and answers normally, so only the source's can fail this.
        """
        context, source, _target = differing_repo_context(recorded=decision_file("apt:package:curl"))
        answering = source.run_command.side_effect

        def failing_cat(cmd: str, **kwargs: object) -> CommandResult:
            """The conflict fixture unchanged, except that the source cannot read the file."""
            if cmd.startswith("sudo cat "):
                return CommandResult(1, "", f"cat: {cmd.removeprefix('sudo cat ')}: Permission denied\n")
            return answering(cmd, **kwargs)

        source.run_command = AsyncMock(side_effect=failing_cat)

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        # "probe on the source", not a bare "source": the path itself contains that word.
        assert "sudo cat /etc/apt/sources.list.d/vendor.list" in str(excinfo.value)
        assert "probe on the source" in str(excinfo.value)
        assert "Permission denied" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_removal_content_read_that_did_not_answer_fails_the_job(self) -> None:
        """The other `sudo cat` call site: a file only the target has is read to learn its
        format before it is offered for removal. Its silence makes the removal item describe
        a file this run never read.
        """
        context, _source, _target = _repo_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "gone.list"), ""),
                "cat /etc/apt/sources.list.d/gone.list": CommandResult(
                    1, "", "cat: /etc/apt/sources.list.d/gone.list: Input/output error\n"
                ),
            },
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "sudo cat /etc/apt/sources.list.d/gone.list" in str(excinfo.value)
        assert "probe on the target" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_removal_impact_read_that_did_not_answer_fails_the_job(self) -> None:
        """`AptProbe.packages_by_source_file`. Its silence answers "this
        repository strands nothing", which is the answer that lets a repository feeding
        machine-specific packages be removed or overwritten with no disclosure. The source
        holds no packages here, so `collect_target_policy` never runs and this is the only
        `apt-cache policy` the target is asked.
        """
        context, _source, _target = make_context(
            source_responses=_NO_PACKAGES,
            target_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "vendor.list"), ""),
                "apt.decisions.yaml": CommandResult(0, decision_file("apt:package:vendor-tool"), ""),
                "apt-cache policy": CommandResult(100, "", "E: Unable to read the package lists\n"),
            },
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await AptSyncJob(context).plan()

        assert "apt-cache policy vendor-tool" in str(excinfo.value)
        assert "Unable to read the package lists" in str(excinfo.value)
