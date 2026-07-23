"""Unit tests for the package-sync item model (`package_items.py`, D-02/D-25).

Covers `compare_deb_versions` (dpkg-delegated version ordering), `HoldPinFact` plus
the `build_detail` helpers (hold-vs-pin distinguishability, RESEARCH Pitfall 2), and
the four manager item shapes (`SnapItem`, `FlatpakItem`, `FlatpakRemoteItem`,
`UnreproducibleItem`) this plan adds to the shared registry. All executor
interactions are mocked except one real-`dpkg` cross-check, skipped when absent.
"""

from __future__ import annotations

import shutil
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.executor import LocalExecutor
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    FlatpakItem,
    FlatpakRemoteItem,
    HoldPinFact,
    ItemClass,
    ItemDiff,
    SnapItem,
    UnreproducibleItem,
    build_held_or_pinned_detail,
    build_repo_unavailable_detail,
    build_version_mismatch_detail,
    compare_deb_versions,
)
from pcswitcher.models import CommandResult


def _stub_executor(responses: dict[str, CommandResult]) -> MagicMock:
    """A minimal `Executor`-shaped stub matching by substring (first match wins)."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        raise AssertionError(f"no stub response configured for command: {cmd!r}")

    executor = MagicMock()
    executor.run_command = AsyncMock(side_effect=_side_effect)
    return executor


class TestCompareDebVersions:
    """`compare_deb_versions` delegates ordering to `dpkg --compare-versions`."""

    @pytest.mark.asyncio
    async def test_lt_for_debian_revision_ordering(self) -> None:
        executor = _stub_executor(
            {
                "1.0-1 lt 1.0-2": CommandResult(0, "", ""),
                "1.0-1 gt 1.0-2": CommandResult(1, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "1.0-1", "1.0-2")

        assert result < 0

    @pytest.mark.asyncio
    async def test_gt_for_epoch_beats_larger_upstream_number(self) -> None:
        """`2:1.0` outranks `10.0` — the epoch outranks the larger upstream number."""
        executor = _stub_executor(
            {
                "2:1.0 lt 10.0": CommandResult(1, "", ""),
                "2:1.0 gt 10.0": CommandResult(0, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "2:1.0", "10.0")

        assert result > 0

    @pytest.mark.asyncio
    async def test_equal_for_identical_strings_without_a_second_executor_call(self) -> None:
        executor = _stub_executor({})

        result = await compare_deb_versions(executor, "1.0-1", "1.0-1")

        assert result == 0
        executor.run_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_shells_out_with_shlex_quoted_operands(self) -> None:
        executor = _stub_executor(
            {
                "dpkg --compare-versions 'a b' lt 'c;d'": CommandResult(0, "", ""),
            }
        )

        result = await compare_deb_versions(executor, "a b", "c;d")

        assert result < 0
        first_call = executor.run_command.call_args_list[0]
        assert "'a b'" in first_call.args[0]
        assert "'c;d'" in first_call.args[0]

    @pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg not available on this machine")
    @pytest.mark.asyncio
    async def test_real_dpkg_confirms_epoch_and_revision_ordering(self) -> None:
        """Cross-checks the stub-based tests above against the real binary."""
        executor = LocalExecutor()

        assert await compare_deb_versions(executor, "2:1.0", "10.0") > 0
        assert await compare_deb_versions(executor, "1.0-1", "1.0-2") < 0
        assert await compare_deb_versions(executor, "1.0-1", "1.0-1") == 0


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


class TestSnapItem:
    def test_reports_its_item_class(self) -> None:
        assert SnapItem.ITEM_CLASS == ItemClass.SNAP

    def test_label_names_the_snap_channel_and_revision(self) -> None:
        item = SnapItem(name="firefox", channel="latest/stable", revision="4536")

        assert item.item_id == "snap:firefox"
        label = item.label()
        assert "firefox" in label
        assert "latest/stable" in label
        assert "4536" in label


class TestFlatpakItem:
    def test_reports_its_item_class(self) -> None:
        assert FlatpakItem.ITEM_CLASS == ItemClass.FLATPAK_REF

    def test_same_application_different_scope_yields_distinct_item_ids(self) -> None:
        user_item = FlatpakItem(application="com.slack.Slack", version="4.50", origin="flathub", scope="user")
        system_item = FlatpakItem(application="com.slack.Slack", version="4.50", origin="flathub", scope="system")

        assert user_item.item_id != system_item.item_id

    def test_label_names_the_item_in_actionable_terms(self) -> None:
        item = FlatpakItem(application="com.slack.Slack", version="4.50", origin="flathub", scope="user")

        label = item.label()

        assert "com.slack.Slack" in label
        assert "4.50" in label
        assert "flathub" in label


class TestFlatpakRemoteItem:
    def test_reports_its_item_class(self) -> None:
        assert FlatpakRemoteItem.ITEM_CLASS == ItemClass.FLATPAK_REMOTE

    def test_same_remote_name_byte_identical_url_different_scope_yields_distinct_item_ids(self) -> None:
        url = "https://dl.flathub.org/repo/"
        user_remote = FlatpakRemoteItem(name="flathub", url=url, scope="user")
        system_remote = FlatpakRemoteItem(name="flathub", url=url, scope="system")

        assert user_remote.item_id != system_remote.item_id

    def test_label_names_the_remote(self) -> None:
        remote = FlatpakRemoteItem(name="flathub", url="https://dl.flathub.org/repo/", scope="user")

        label = remote.label()

        assert "flathub" in label
        assert "https://dl.flathub.org/repo/" in label


class TestUnreproducibleItem:
    def test_reports_its_item_class(self) -> None:
        assert UnreproducibleItem.ITEM_CLASS == ItemClass.UNREPRODUCIBLE

    def test_same_identifier_different_origin_yields_distinct_item_ids(self) -> None:
        no_candidate = UnreproducibleItem(origin="apt-no-candidate", identifier="brscan3", label="brscan3")
        unowned_path = UnreproducibleItem(origin="unowned-path", identifier="brscan3", label="/opt/brscan3")

        assert no_candidate.item_id != unowned_path.item_id

    def test_label_is_a_plain_field(self) -> None:
        item = UnreproducibleItem(origin="unowned-path", identifier="/opt/flux", label="flux (unowned in /opt)")

        assert item.label == "flux (unowned in /opt)"
