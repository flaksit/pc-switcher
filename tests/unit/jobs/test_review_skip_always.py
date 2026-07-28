"""Unit tests for D-07's third answer on the decision screen: "always skip", which records
`Decision.SKIP_ALWAYS` for an item the user is declaring specific to this machine.

It used to be a second checkbox over whatever the apply list left unticked. It is now the
third option on the one screen a group gets, so these tests are about which groups OFFER it,
what the screen calls it, and what answering it produces.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Sequence
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    REPO_CONFLICT_REVIEW_ACTION,
    REPO_REMOVAL_REVIEW_ACTION,
    SKIP_ALWAYS_WORD,
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


def _words(call: Any) -> list[str]:
    return [option.word for option in call.kwargs["options"]]


def _values(call: Any) -> list[str]:
    return [option.value for option in call.kwargs["options"]]


@pytest.mark.asyncio
class TestThePermanentAnswer:
    async def test_answering_always_skip_records_skip_always(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a"), _entry("b"), _entry("c")])
        screen = _fake_prompt(ask_return={"a": "apply", "b": "skip_always", "c": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions == {
            "a": Decision.APPLY,
            "b": Decision.SKIP_ALWAYS,
            "c": Decision.SKIP_ONCE,
        }
        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_the_screen_calls_it_always_skip_not_never_offer_again(self) -> None:
        """The user's correction: the answer is not about being asked again on this machine,
        it is about the item belonging to this machine.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a")])
        screen = _fake_prompt(ask_return={"a": "apply"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            await review_items([group], console=console, ui=ui)

        assert SKIP_ALWAYS_WORD == "always skip"
        assert SKIP_ALWAYS_WORD in _words(decision_list.call_args)
        assert "never offer again" not in " ".join(_words(decision_list.call_args))

    async def test_no_group_is_ever_asked_about_permanence_a_second_time(self) -> None:
        """The two-pass shape is gone: one screen per group, whatever the answers were."""
        console = _interactive_console()
        ui = MagicMock()
        groups = [_group("install", [_entry("a")]), _group("remove", [_entry("b", action_label="remove")])]
        screen = _fake_prompt(ask_side_effect=[{"a": "skip_once"}, {"b": "skip_always"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui)

        assert decision_list.call_count == len(groups)
        assert outcome.decisions == {"a": Decision.SKIP_ONCE, "b": Decision.SKIP_ALWAYS}

    @pytest.mark.parametrize("action", ["install", "add", "enable", "change", "remove", "delete", "disable"])
    async def test_every_promotable_direction_offers_all_three_answers(self, action: str) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group(action, [_entry("a", action_label=action)], title=f"{action} things")
        screen = _fake_prompt(ask_return={"a": "skip_always"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert _values(decision_list.call_args) == [Decision.APPLY, Decision.SKIP_ONCE, Decision.SKIP_ALWAYS]
        assert outcome.decisions == {"a": Decision.SKIP_ALWAYS}


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
        screen = _fake_prompt(ask_return={"apt:hold:firefox": "skip_always"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert SKIP_ALWAYS_WORD in _words(decision_list.call_args)
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
        screen = _fake_prompt(ask_return={"flatpak:mask:user:org.gimp.GIMP": "skip_always"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert _words(decision_list.call_args)[0] == "unmask"
        assert outcome.decisions == {"flatpak:mask:user:org.gimp.GIMP": Decision.SKIP_ALWAYS}


@pytest.mark.asyncio
class TestGroupsNeverOfferedPermanence:
    """The two-answer screens (D-07). Same widget, one option short — the difference the
    user sees is a missing answer, not a different flow.
    """

    @pytest.mark.parametrize(
        ("action", "title", "action_label"),
        [
            ("report_only", "Report apt packages", "report"),
            (REPO_REMOVAL_REVIEW_ACTION, "Delete repositories (apt)", "delete repository"),
            (REPO_CONFLICT_REVIEW_ACTION, "Resolve apt repository conflicts", "overwrite"),
        ],
    )
    async def test_two_answer_screens_omit_the_permanent_option(
        self, action: str, title: str, action_label: str
    ) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group(action, [_entry("a", action_label=action_label)], title=title)
        screen = _fake_prompt(ask_return={"a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert _values(decision_list.call_args) == [Decision.APPLY, Decision.SKIP_ONCE]
        assert SKIP_ALWAYS_WORD not in _words(decision_list.call_args)
        assert outcome.decisions == {"a": Decision.SKIP_ONCE}

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
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        decision_list.assert_not_called()
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
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        decision_list.assert_not_called()
        assert outcome.decisions == {"apt:package:pkg-a": Decision.SKIP_ONCE}

    async def test_non_interactive_run_prompts_nothing(self) -> None:
        """D-26: no TTY -> no screen at all, everything skip-once, nothing permanent."""
        console = Console(file=io.StringIO())
        ui = MagicMock()
        group = _group("install", [_entry("a"), _entry("b")])

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        decision_list.assert_not_called()
        assert outcome.decisions == {"a": Decision.SKIP_ONCE, "b": Decision.SKIP_ONCE}
        assert Decision.SKIP_ALWAYS not in outcome.decisions.values()


@pytest.mark.asyncio
class TestAbortAndTeardown:
    async def test_ctrl_c_at_a_decision_screen_aborts_the_whole_sync(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        first_group = _group("install", [_entry("a")])
        later_group = _group("install", [_entry("b")], manager="snap", title="Install snaps")
        screen = _fake_prompt(ask_side_effect=[None, {"b": "apply"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
            pytest.raises(SyncAbortedByUser, match="Install packages"),
        ):
            await review_items([first_group, later_group], console=console, ui=ui)

        # The later group is never reached: the abort stops the whole review.
        assert decision_list.call_count == 1
        ui.resume.assert_called_once()

    async def test_ui_resumed_when_the_screen_raises(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a")])
        screen = _fake_prompt(ask_side_effect=KeyboardInterrupt)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            pytest.raises(KeyboardInterrupt),
        ):
            await review_items([group], console=console, ui=ui)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_an_untrusted_label_reaches_the_screen_verbatim(self) -> None:
        """T-02-02 at this layer: a bracketed package name is row text, never Rich markup —
        the interactive path prints no panel, so the only place it could break is the row.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a", label="pkg[weird]name")])
        screen = _fake_prompt(ask_return={"a": "skip_always"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert decision_list.call_args.kwargs["rows"][0].label == "pkg[weird]name"
        assert outcome.decisions == {"a": Decision.SKIP_ALWAYS}
