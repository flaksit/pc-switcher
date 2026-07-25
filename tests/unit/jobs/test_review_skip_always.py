"""Unit tests for D-07's third outcome in the ordinary checkbox review: the second
"never offer again on this machine" checkbox that promotes an unticked item's skip to
`Decision.SKIP_ALWAYS`.

The apply checkbox and the promotion checkbox are both `questionary.checkbox` calls, so
these tests drive `questionary.checkbox` with an ordered `side_effect` — first prompt is
the apply list, second is the promotion list for that same group.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Sequence
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    review_items,
)
from pcswitcher.models import SyncAbortedByUser


def _mock_isatty(interactive: bool) -> MagicMock:
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = interactive
    return mock_stdin


def _interactive_console() -> Console:
    return Console(file=io.StringIO(), force_terminal=True)


def _entry(item_id: str, label: str = "pkg", action_label: str = "install") -> ReviewEntry:
    return ReviewEntry(item_id=item_id, label=label, action_label=action_label, detail=None)


def _fake_prompt(*, ask_return: object = None, ask_side_effect: object = None) -> MagicMock:
    prompt = MagicMock()
    if ask_side_effect is not None:
        prompt.ask = MagicMock(side_effect=ask_side_effect)
    else:
        prompt.ask = MagicMock(return_value=ask_return)
    return prompt


def _group(action: str, entries: Sequence[ReviewEntry], *, manager: str = "apt", title: str = "Install packages"):
    return ReviewGroup(manager=manager, action=action, title=title, entries=tuple(entries))


@pytest.mark.asyncio
class TestPermanentSkipPromotion:
    async def test_promotion_tick_yields_skip_always_and_apply_tick_is_untouched(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a"), _entry("b"), _entry("c")])
        apply_prompt = _fake_prompt(ask_return=["a"])
        promote_prompt = _fake_prompt(ask_return=["b"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, promote_prompt],
            ) as checkbox,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions == {
            "a": Decision.APPLY,
            "b": Decision.SKIP_ALWAYS,
            "c": Decision.SKIP_ONCE,
        }
        # Only the unticked entries are offered for promotion.
        promote_values = {choice.value for choice in checkbox.call_args_list[1].kwargs["choices"]}
        assert promote_values == {"b", "c"}
        # Nothing is preselected: a bare Enter keeps the pre-existing skip-once behaviour.
        assert all(choice.checked is False for choice in checkbox.call_args_list[1].kwargs["choices"])
        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_fully_ticked_group_prompts_nothing_extra(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a"), _entry("b")])
        apply_prompt = _fake_prompt(ask_return=["a", "b"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", side_effect=[apply_prompt]) as checkbox,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        checkbox.assert_called_once()
        assert outcome.decisions == {"a": Decision.APPLY, "b": Decision.APPLY}

    async def test_empty_promotion_list_leaves_everything_skip_once(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a")])
        apply_prompt = _fake_prompt(ask_return=[])
        promote_prompt = _fake_prompt(ask_return=[])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, promote_prompt],
            ),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions == {"a": Decision.SKIP_ONCE}

    async def test_removal_group_promotion_is_offered(self) -> None:
        """A removal group starts fully unticked, so its every entry is promotable."""
        console = _interactive_console()
        ui = MagicMock()
        group = _group("remove", [_entry("a", action_label="remove")], title="Remove packages")
        apply_prompt = _fake_prompt(ask_return=[])
        promote_prompt = _fake_prompt(ask_return=["a"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, promote_prompt],
            ),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions == {"a": Decision.SKIP_ALWAYS}

    async def test_change_group_promotion_is_offered(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group("change", [_entry("s", action_label="change")], manager="snap", title="Change snaps")
        apply_prompt = _fake_prompt(ask_return=[])
        promote_prompt = _fake_prompt(ask_return=["s"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, promote_prompt],
            ),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions == {"s": Decision.SKIP_ALWAYS}

    async def test_each_group_gets_its_own_promotion_pass(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        install_group = _group("install", [_entry("a")])
        removal_group = _group("remove", [_entry("b", action_label="remove")], title="Remove packages")
        prompts = [
            _fake_prompt(ask_return=[]),  # install apply
            _fake_prompt(ask_return=["a"]),  # install promotion
            _fake_prompt(ask_return=[]),  # removal apply
            _fake_prompt(ask_return=[]),  # removal promotion
        ]

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", side_effect=prompts) as checkbox,
        ):
            outcome = await review_items([install_group, removal_group], console=console, ui=ui)

        assert checkbox.call_count == 4
        assert outcome.decisions == {"a": Decision.SKIP_ALWAYS, "b": Decision.SKIP_ONCE}


@pytest.mark.asyncio
class TestBlockStateItemsArePromotable:
    """#208: apt holds, snap holds and flatpak masks are ordinary INSTALL/REMOVE-direction
    items, so `docs/jobs/package-sync.md`'s promise of the full three-way choice for a
    block must hold in both directions.
    """

    async def test_hold_add_direction_can_be_made_permanent(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group(
            "install",
            [_entry("apt:hold:firefox", label="firefox", action_label="hold")],
            title="Hold apt packages",
        )
        apply_prompt = _fake_prompt(ask_return=[])
        promote_prompt = _fake_prompt(ask_return=["apt:hold:firefox"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, promote_prompt],
            ),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions == {"apt:hold:firefox": Decision.SKIP_ALWAYS}

    async def test_mask_removal_direction_can_be_made_permanent(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group(
            "remove",
            [_entry("flatpak:mask:user:org.gimp.GIMP", label="org.gimp.GIMP (user)", action_label="unmask")],
            manager="flatpak",
            title="Unmask flatpak packages",
        )
        apply_prompt = _fake_prompt(ask_return=[])
        promote_prompt = _fake_prompt(ask_return=["flatpak:mask:user:org.gimp.GIMP"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, promote_prompt],
            ),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions == {"flatpak:mask:user:org.gimp.GIMP": Decision.SKIP_ALWAYS}


@pytest.mark.asyncio
class TestGroupsNeverOfferedPromotion:
    async def test_report_only_group_is_never_offered_permanence(self) -> None:
        """An informational item has no holder machine: recording one would stop the
        package syncing altogether rather than stop reporting the drift.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _group(
            "report_only",
            [_entry("apt:package:vim", label="vim (2.0 vs 1.0)", action_label="report")],
            title="Report apt packages",
        )
        apply_prompt = _fake_prompt(ask_return=[])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", side_effect=[apply_prompt]) as checkbox,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        checkbox.assert_called_once()
        assert outcome.decisions == {"apt:package:vim": Decision.SKIP_ONCE}

    async def test_unreproducible_group_keeps_its_own_flow(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group(
            UNREPRODUCIBLE_REVIEW_ACTION,
            [_entry("u1", label="brscan3")],
            title="Resolve apt items with no reproducible install",
        )
        select_prompt = _fake_prompt(ask_return="skip_once")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        checkbox.assert_not_called()
        assert outcome.decisions == {"u1": Decision.SKIP_ONCE}

    async def test_collateral_group_keeps_its_own_flow(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group(
            COLLATERAL_REVIEW_ACTION,
            [_entry("apt:package:pkg-a", label="other-manual")],
            title="Resolve apt manual-collateral removals",
        )
        select_prompt = _fake_prompt(ask_return="skip")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        checkbox.assert_not_called()
        assert outcome.decisions == {"apt:package:pkg-a": Decision.SKIP_ONCE}

    async def test_non_interactive_run_prompts_nothing(self) -> None:
        """D-26: no TTY -> no promotion offer, everything skip-once, nothing permanent."""
        console = Console(file=io.StringIO())
        ui = MagicMock()
        group = _group("install", [_entry("a"), _entry("b")])

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        checkbox.assert_not_called()
        assert outcome.decisions == {"a": Decision.SKIP_ONCE, "b": Decision.SKIP_ONCE}
        assert Decision.SKIP_ALWAYS not in outcome.decisions.values()


@pytest.mark.asyncio
class TestPromotionAbortAndTeardown:
    async def test_ctrl_c_at_the_promotion_prompt_aborts_the_whole_sync(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        first_group = _group("install", [_entry("a")])
        later_group = _group("install", [_entry("b")], manager="snap", title="Install snaps")
        apply_prompt = _fake_prompt(ask_return=[])
        cancelled_promote = _fake_prompt(ask_return=None)
        never_prompt = _fake_prompt(ask_return=["b"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, cancelled_promote, never_prompt],
            ) as checkbox,
            pytest.raises(SyncAbortedByUser),
        ):
            await review_items([first_group, later_group], console=console, ui=ui)

        # The later group is never reached: the abort stops the whole review.
        assert checkbox.call_count == 2
        ui.resume.assert_called_once()

    async def test_ui_resumed_when_the_promotion_prompt_raises(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a")])
        apply_prompt = _fake_prompt(ask_return=[])
        promote_prompt = _fake_prompt(ask_side_effect=KeyboardInterrupt)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, promote_prompt],
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            await review_items([group], console=console, ui=ui)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_bracketed_label_reaches_the_console_without_markup_error(self) -> None:
        """T-02-02: an untrusted package name containing brackets must never reach a Rich
        console as a bare `str`.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a", label="pkg[weird]name")])
        apply_prompt = _fake_prompt(ask_return=[])
        promote_prompt = _fake_prompt(ask_return=["a"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, promote_prompt],
            ),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions == {"a": Decision.SKIP_ALWAYS}
