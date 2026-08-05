"""Unit tests for D-07's third answer on the decision screen: "always skip", which records
`Decision.SKIP_ALWAYS` for an item the user is declaring specific to this machine.

It used to be a second checkbox over whatever the apply list left unticked. It is now the
third option on the one screen a group gets, so these tests are about which groups OFFER it,
what the screen calls it, and what answering it produces.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Any, TypedDict
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    REPO_CONFLICT_REVIEW_ACTION,
    REPO_REMOVAL_REVIEW_ACTION,
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    review_items,
)
from pcswitcher.models import SyncAbortedByUser
from tests.unit.console_capture import captured_console


class _Hosts(TypedDict):
    """Typed so `**HOSTS` cannot silently land on another keyword parameter."""

    source_hostname: str
    target_hostname: str


# Concrete and distinct, so an assertion that a screen names the right machine cannot pass
# on the other one's text.
HOSTS: _Hosts = {"source_hostname": "atlas", "target_hostname": "nomad"}


def _mock_isatty(interactive: bool) -> MagicMock:
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = interactive
    return mock_stdin


def _interactive_console() -> Console:
    console, _ = captured_console(terminal=True)
    return console


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


def _permanent(call: Any) -> str | None:
    """The word of the screen's recorded-forever answer, or None where it offers none.

    By the flag rather than by the word: the word is direction-specific ("never install" on
    one screen, "keep for good" on another), and a test that hunted for one literal would
    read as absent on every screen that legitimately says the other.
    """
    words = [option.word for option in call.kwargs["options"] if option.is_permanent]
    return words[0] if words else None


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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.decisions == {
            "a": Decision.APPLY,
            "b": Decision.SKIP_ALWAYS,
            "c": Decision.SKIP_ONCE,
        }
        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_the_screen_names_the_permanent_answer_as_this_screen_s_own_act(self) -> None:
        """H81, H90 — The user's correction, twice over: the answer is not about being asked again on
        this machine but about the item belonging to it, and one generic "always skip" could
        not say that in both directions — an install screen refuses an arrival, a removal
        screen keeps what is already there.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _group("install", [_entry("a")])
        screen = _fake_prompt(ask_return={"a": "apply"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        assert _permanent(decision_list.call_args) == "never install"
        assert "never offer again" not in " ".join(_words(decision_list.call_args))

    async def test_no_group_is_ever_asked_about_permanence_a_second_time(self) -> None:
        """H33 — The two-pass shape is gone: one screen per group, whatever the answers were."""
        console = _interactive_console()
        ui = MagicMock()
        groups = [_group("install", [_entry("a")]), _group("remove", [_entry("b", action_label="remove")])]
        screen = _fake_prompt(ask_side_effect=[{"a": "skip_once"}, {"b": "skip_always"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui, **HOSTS)

        assert decision_list.call_count == len(groups)
        assert outcome.decisions == {"a": Decision.SKIP_ONCE, "b": Decision.SKIP_ALWAYS}

    @pytest.mark.parametrize("action", ["install", "add", "enable", "change", "remove", "delete", "disable"])
    async def test_every_promotable_direction_offers_all_three_answers(self, action: str) -> None:
        """H101 — every direction that records a mark offers the act, the one-sync skip and the permanent answer."""
        console = _interactive_console()
        ui = MagicMock()
        group = _group(action, [_entry("a", action_label=action)], title=f"{action} things")
        # The second answer is the follow-up's, which only the "change" direction reaches;
        # an unused side effect is harmless for the other six.
        screen = _fake_prompt(ask_side_effect=[{"a": "skip_always"}, {"a": "target"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert _values(decision_list.call_args_list[0]) == [Decision.APPLY, Decision.SKIP_ONCE, Decision.SKIP_ALWAYS]
        assert outcome.decisions == {"a": Decision.SKIP_ALWAYS}


@pytest.mark.asyncio
class TestGroupsNeverOfferedPermanence:
    """The two-answer screens (D-07). Same widget, one option short — the difference the
    user sees is a missing answer, not a different flow.
    """

    @pytest.mark.parametrize(
        ("action", "title", "action_label"),
        [
            (REPO_REMOVAL_REVIEW_ACTION, "Delete apt repositories atlas no longer has", "remove"),
            (REPO_CONFLICT_REVIEW_ACTION, "Resolve apt repository conflicts", "overwrite"),
        ],
    )
    async def test_two_answer_screens_omit_the_permanent_option(
        self, action: str, title: str, action_label: str
    ) -> None:
        """H93, H95, H136, H138 — a repository deletion and a repository overwrite are the
        same widget, one answer short.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _group(action, [_entry("a", action_label=action_label)], title=title)
        screen = _fake_prompt(ask_return={"a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert _values(decision_list.call_args) == [Decision.APPLY, Decision.SKIP_ONCE]
        assert _permanent(decision_list.call_args) is None
        assert outcome.decisions == {"a": Decision.SKIP_ONCE}

    async def test_unreproducible_group_offers_its_own_three_answers(self) -> None:
        """Its permanent answer is offered, but by its own flow — one screen per item, and
        an act that opens an editor rather than converging anything.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _group(
            UNREPRODUCIBLE_REVIEW_ACTION,
            [_entry("u1", label="brscan3")],
            title="Resolve apt items with no reproducible install",
        )
        screen = _fake_prompt(ask_return={"u1": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert _permanent(decision_list.call_args) == "never install"
        assert _words(decision_list.call_args)[0] == "install"
        assert outcome.decisions == {"u1": Decision.SKIP_ONCE}

    async def test_collateral_group_is_never_offered_permanence(self) -> None:
        """D19, H117 — Its third answer stops the sync; nothing about a collateral package is recorded,
        because nobody expressed a preference about it — apt's manual mark is not one.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _group(
            COLLATERAL_REVIEW_ACTION,
            [_entry("apt:package:pkg-a", label="other-manual")],
            title="Resolve apt manual-collateral removals",
        )
        screen = _fake_prompt(ask_return={"apt:package:pkg-a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert _permanent(decision_list.call_args) is None
        assert _values(decision_list.call_args) == [Decision.APPLY, Decision.SKIP_ONCE, "stop_sync"]
        assert outcome.decisions == {"apt:package:pkg-a": Decision.SKIP_ONCE}

    async def test_non_interactive_run_prompts_nothing(self) -> None:
        """H135, H160 — D-26: no TTY -> no screen at all, everything skip-once, nothing permanent."""
        console, _ = captured_console()
        ui = MagicMock()
        group = _group("install", [_entry("a"), _entry("b")])

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()
        assert outcome.decisions == {"a": Decision.SKIP_ONCE, "b": Decision.SKIP_ONCE}
        assert Decision.SKIP_ALWAYS not in outcome.decisions.values()


@pytest.mark.asyncio
class TestAbortAndTeardown:
    async def test_ctrl_c_at_a_decision_screen_aborts_the_whole_sync(self) -> None:
        """H148 — Ctrl-C at a decision screen ends the whole review; no later group is built."""
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
            await review_items([first_group, later_group], console=console, ui=ui, **HOSTS)

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
            await review_items([group], console=console, ui=ui, **HOSTS)

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert decision_list.call_args.kwargs["rows"][0].label == "pkg[weird]name"
        assert outcome.decisions == {"a": Decision.SKIP_ALWAYS}
