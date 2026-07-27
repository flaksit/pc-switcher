"""Unit tests for the item model every package job shares (`packages/items.py`).

Scope is exactly what lives there: `HoldPinFact` and the detail builders more than one
manager writes. A shape only one manager constructs is tested beside that manager --
`SnapItem` in `test_snap_sync.py`, `compare_deb_versions` in `test_apt_sync.py` -- so a
job's own tests move with the job.
"""

from __future__ import annotations

from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    HoldPinFact,
    ItemClass,
    ItemDiff,
    build_held_or_pinned_detail,
    build_repo_unavailable_detail,
    build_version_mismatch_detail,
)


class TestHoldPinFactAndBuildDetail:
    """Hold and pin stay distinguishable facts even under one review category."""

    def test_hold_and_pin_details_are_distinguishable(self) -> None:
        hold = HoldPinFact(mechanism="hold", package="curl", source_ref="apt-mark showhold")
        pin = HoldPinFact(mechanism="pin", package="curl", source_ref="/etc/apt/preferences.d/curl-pin")

        hold_detail = build_held_or_pinned_detail(hold)
        pin_detail = build_held_or_pinned_detail(pin)

        assert hold_detail != pin_detail
        assert "hold" in hold_detail
        assert "pin" in pin_detail

    def test_hold_and_pin_diffs_carry_different_mechanism_values(self) -> None:
        hold = HoldPinFact(mechanism="hold", package="curl", source_ref="apt-mark showhold")
        pin = HoldPinFact(mechanism="pin", package="curl", source_ref="/etc/apt/preferences.d/curl-pin")

        hold_diff = ItemDiff(
            item_class=ItemClass.APT_PACKAGE,
            diff_class=DiffClass.HELD_OR_PINNED,
            action=DiffAction.REPORT_ONLY,
            item_id="apt:package:curl",
            label="curl",
            detail=build_held_or_pinned_detail(hold),
        )
        pin_diff = ItemDiff(
            item_class=ItemClass.APT_PACKAGE,
            diff_class=DiffClass.HELD_OR_PINNED,
            action=DiffAction.REPORT_ONLY,
            item_id="apt:package:curl",
            label="curl",
            detail=build_held_or_pinned_detail(pin),
        )

        assert hold_diff.diff_class == DiffClass.HELD_OR_PINNED
        assert pin_diff.diff_class == DiffClass.HELD_OR_PINNED
        assert hold_diff.detail != pin_diff.detail
        assert hold.mechanism != pin.mechanism

    def test_build_version_mismatch_detail_contains_both_versions(self) -> None:
        detail = build_version_mismatch_detail("1.0-1", "2.0-1")

        assert "1.0-1" in detail
        assert "2.0-1" in detail

    def test_build_repo_unavailable_detail_names_the_package(self) -> None:
        detail = build_repo_unavailable_detail("brscan3")

        assert "brscan3" in detail
