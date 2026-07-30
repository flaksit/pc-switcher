"""The diff classes apt produces — the whole of D-25 as apt expresses it.

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import inspect

import pytest

from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.apt_sync.diffing import diff_apt_packages
from pcswitcher.jobs.apt_sync.items import AptPackageItem
from pcswitcher.jobs.apt_sync.origins import OriginPlan
from pcswitcher.jobs.apt_sync.probe import AptProbe
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass
from pcswitcher.models import CommandResult
from tests.unit.jobs.apt.helpers import (
    DPKG_QUERY_3,
    MACHINES,
    SHOWMANUAL_3,
    make_context,
)


class TestDiff:
    """Target query + diff: the tracer's MISSING_ON_TARGET/INSTALL slice."""

    @pytest.mark.asyncio
    async def test_diff_yields_exactly_two_missing_items(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-b\t2.0\n", ""),
            },
        )
        probe = AptProbe(context.source, context.target)

        source_items, _origins = await probe.capture_source_items()
        target_items = await probe.query_target_items()
        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES)

        assert len(diffs) == 2
        assert {d.item_id for d in diffs} == {"apt:package:pkg-a", "apt:package:pkg-c"}
        assert all(d.diff_class == DiffClass.MISSING_ON_TARGET for d in diffs)

    def test_extra_on_target_yields_extra_on_target_remove(self) -> None:
        """A name on the target but not the source yields EXTRA_ON_TARGET/REMOVE
        (plan 02-05 — the tracer's own boundary note for this case no longer holds)."""
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [
            AptPackageItem(name="pkg-a", version="1.0"),
            AptPackageItem(name="pkg-extra", version="9.9"),
        ]
        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].item_id == "apt:package:pkg-extra"
        assert diffs[0].diff_class == DiffClass.EXTRA_ON_TARGET
        assert diffs[0].action == DiffAction.REMOVE


class TestNoUnreproducibleDetectionInApt:
    """D-18: apt_sync no longer detects, reviews or converges unreproducible items —
    that ownership moved to manual_installs_sync. An input that previously produced an
    UNREPRODUCIBLE diff (a source package with no apt candidate) now produces none.
    """

    @pytest.mark.asyncio
    async def test_apt_plan_emits_no_unreproducible_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "dpkg-query": CommandResult(0, "brscan3\t1.0\n", ""),
                # A source with no apt candidate AND an unowned /usr/local path: both
                # previously became UNREPRODUCIBLE diffs inside apt_sync's own plan().
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
                "find /usr/local": CommandResult(0, "/usr/local/flux\n", ""),
                "dpkg --search": CommandResult(0, "", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "dpkg-query": CommandResult(0, "brscan3\t1.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.UNREPRODUCIBLE for d in plan.diffs)


class TestDiffEngine:
    """`diff_apt_packages` produces every D-25 diff class."""

    def test_missing_on_target_yields_install(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, [], {}, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.MISSING_ON_TARGET
        assert diffs[0].action == DiffAction.INSTALL

    def test_extra_on_target_yields_remove(self) -> None:
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages([], target_items, {}, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.EXTRA_ON_TARGET
        assert diffs[0].action == DiffAction.REMOVE

    def test_version_mismatch_yields_report_only_with_both_versions(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="2.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.VERSION_MISMATCH
        assert diffs[0].action == DiffAction.REPORT_ONLY
        assert diffs[0].detail is not None
        assert "1.0" in diffs[0].detail
        assert "2.0" in diffs[0].detail

    def test_equal_versions_yields_no_diff(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES)

        assert diffs == []

    def test_source_hold_only_yields_apt_hold_install(self) -> None:
        """#208: a name held on the source but not the target is an `apt:hold:` INSTALL
        (hold), a distinct APT_HOLD item — never a package-level report.
        """
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES, frozenset({"pkg-a"}), frozenset())

        assert len(diffs) == 1
        assert diffs[0].item_class == ItemClass.APT_HOLD
        assert diffs[0].item_id == "apt:hold:pkg-a"
        assert diffs[0].action == DiffAction.INSTALL

    def test_target_hold_only_yields_apt_hold_remove_and_suppresses_package_action(self) -> None:
        """#208: a name held on the target but not the source is an `apt:hold:` REMOVE
        (unhold); the version-mismatch package action it would otherwise carry is
        suppressed (a held package is never proposed for upgrade) and no package-level
        report is emitted for the hold mechanism.
        """
        source_items = [AptPackageItem(name="pkg-a", version="2.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES, frozenset(), frozenset({"pkg-a"}))

        assert len(diffs) == 1
        assert diffs[0].item_class == ItemClass.APT_HOLD
        assert diffs[0].action == DiffAction.REMOVE

    def test_a_held_package_outside_the_targets_manual_set_is_still_not_proposed(self) -> None:
        """`PKG-FR-APT-HELD-TARGET`: the target's hold set is `apt-mark showhold`, which
        covers packages apt installed automatically there. Such a package is absent from
        the target's manual set, so keying the suppression on that set proposed an install
        apt refuses with `E: Held packages were changed`. Its hold is still an item.
        """
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, [], {}, MACHINES, frozenset(), frozenset({"pkg-a"}))

        assert [(diff.item_id, diff.action) for diff in diffs] == [("apt:hold:pkg-a", DiffAction.REMOVE)]

    def test_held_on_both_yields_no_diff(self) -> None:
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [AptPackageItem(name="pkg-a", version="1.0")]

        diffs = diff_apt_packages(source_items, target_items, {}, MACHINES, frozenset({"pkg-a"}), frozenset({"pkg-a"}))

        assert diffs == []

    def test_the_diff_takes_no_pin_input_at_all(self) -> None:
        """The signature is the deletion, made structural: with no pin argument there is no
        way to reintroduce the echo without changing every caller (ADR-020 D-25).
        """
        parameters = inspect.signature(diff_apt_packages).parameters

        assert not any("pin" in name for name in parameters)

    def test_an_origin_no_source_file_declares_yields_repo_unavailable_not_install(self) -> None:
        """ADR-020 D-34 class 4, the only remaining meaning of `REPO_UNAVAILABLE`: the
        source has the package from a repository that has since been deleted from the
        source's own `/etc/apt`, so the origin cannot be handed to the target at all.
        """
        source_items = [AptPackageItem(name="brscan3", version="")]
        plan = {"apt:package:brscan3": OriginPlan(source_origins=frozenset({"https://gone.example.com/apt"}))}

        diffs = diff_apt_packages(source_items, [], plan, MACHINES)

        assert len(diffs) == 1
        assert diffs[0].diff_class == DiffClass.REPO_UNAVAILABLE
        assert diffs[0].action == DiffAction.REPORT_ONLY
        assert diffs[0].detail == (
            "target-host cannot install brscan3 from gone.example.com/apt: "
            "no repository file on source-host declares it"
        )
