"""The end of a list of answers stops the cursor instead of wrapping it round.

Driven through a real prompt_toolkit Application over a pipe input, like
`test_decision_list`: the clamp is key bindings registered over questionary's own, so
nothing below the keystroke proves it works.
"""

from __future__ import annotations

import questionary
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from questionary.prompts.common import Separator

from pcswitcher.jobs.packages.prompt_navigation import select, step

_DOWN = "\x1b[B"
_UP = "\x1b[A"
_ENTER = "\r"

_THREE = ["first", "second", "third"]


def _drive(keys: str, choices: list[str | Separator] | None = None) -> str:
    """Run the prompt over a pipe input and return the chosen value.

    Built INSIDE the app session for the reason `test_decision_list._drive` gives:
    `Application.__init__` captures the session's input/output.
    """
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            options = [
                c if isinstance(c, Separator) else questionary.Choice(title=c, value=c) for c in (choices or _THREE)
            ]
            return select("pick", options).ask()


class TestStep:
    def test_it_stops_at_both_ends(self) -> None:
        assert step(0, -1, 3) == 0
        assert step(2, 1, 3) == 2

    def test_it_moves_normally_in_between(self) -> None:
        assert step(1, 1, 3) == 2
        assert step(1, -1, 3) == 0


class TestSelect:
    def test_up_on_the_first_answer_stays_there(self) -> None:
        """questionary's own binding would answer "third" here — the last answer on a review
        screen is routinely the destructive or permanent one.
        """
        assert _drive(_UP + _ENTER) == "first"

    def test_down_on_the_last_answer_stays_there(self) -> None:
        assert _drive(_DOWN * 5 + _ENTER) == "third"

    def test_movement_between_the_ends_is_unchanged(self) -> None:
        assert _drive(_DOWN + _ENTER) == "second"
        assert _drive(_DOWN + _DOWN + _UP + _ENTER) == "second"

    def test_j_and_k_are_clamped_too(self) -> None:
        """Every movement key questionary binds, or the wrap survives on the one missed."""
        assert _drive("k" + _ENTER) == "first"
        assert _drive("jjjjj" + _ENTER) == "third"

    def test_a_separator_at_the_end_is_a_wall_not_a_landing(self) -> None:
        """The case that rules out clamping `select_next` itself: questionary walks on with
        `while not ic.is_selection_valid()`, which a clamp inside that method turns into an
        infinite loop.
        """
        assert _drive(_DOWN * 3 + _ENTER, ["first", "second", Separator()]) == "second"
