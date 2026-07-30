"""Unit tests for the one-screen-per-group decision control (`packages.decision_list`).

Two kinds of test, because a terminal control has two testable halves:

- The layout and the row rendering are pure functions (`layout_widths`, `wrap_label`,
  `render_rows`, `legend`), so the column position, the wrapping and the words in the
  decision column are asserted directly on their token stream.
- The key handling is driven through the REAL `prompt_toolkit` Application over a pipe
  input, so "arrows move", "a key sets the focused row" and "Ctrl-C answers None" are
  exercised as keystrokes rather than mocked.

What is NOT covered here, and only a human at a real terminal can judge: whether the
column reads as a column, whether the glyphs are distinguishable, and how the screen
looks scrolled or resized mid-answer.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Window
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput

from pcswitcher.jobs.packages.decision_list import (
    PREFIX_WIDTH,
    DecisionOption,
    DecisionRow,
    decision_list,
    layout_widths,
    legend,
    render_rows,
    wrap_label,
)

_ACT = DecisionOption(
    value="apply", key="y", word="install", glyph="●", is_act=True, hint="go ahead — nomad changes this sync"
)
_SKIP_ONCE = DecisionOption(
    value="skip_once", key="s", word="skip now", glyph="○", hint="leave nomad alone; you are asked again next sync"
)
_SKIP_ALWAYS = DecisionOption(
    value="skip_always",
    key="x",
    word="never install",
    glyph="⊘",
    is_permanent=True,
    hint="atlas's own — nomad never gets it, never asked again",
)

THREE_ANSWERS = (_ACT, _SKIP_ONCE, _SKIP_ALWAYS)
TWO_ANSWERS = (_ACT, _SKIP_ONCE)

# Terminal escape sequences for the arrow keys, as a pipe input delivers them.
_DOWN = "\x1b[B"
_UP = "\x1b[A"
_ENTER = "\r"
_CTRL_C = "\x03"


def _row(row_id: str, label: str = "pkg", **kwargs: Any) -> DecisionRow:
    return DecisionRow(row_id=row_id, label=label, default="apply", **kwargs)


def _plain(tokens: list[tuple[str, str]]) -> str:
    return "".join(text for _, text in tokens)


def _lines(tokens: list[tuple[str, str]]) -> list[str]:
    return _plain(tokens).split("\n")


def _drive(keys: str, *, rows: list[DecisionRow], options: tuple[DecisionOption, ...] = THREE_ANSWERS) -> Any:
    """Run a real Application over a pipe input and return `.ask()`'s answer.

    The prompt is built INSIDE the app session on purpose: `Application.__init__` captures
    the session's input/output, so a prompt constructed outside would attach to the real
    stdin (`/dev/null` under pytest) and die on EOF.
    """
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            return decision_list("t", rows=rows, options=options).ask()


class TestLayout:
    def test_the_decision_column_sits_a_gap_right_of_the_longest_item(self) -> None:
        rows = [_row("a", "sl"), _row("b", "cmatrix-longer")]
        item_width, column = layout_widths(rows, THREE_ANSWERS, total_width=80)

        # 80 - prefix(5) - gap(2) - len("never install")(13) - right margin(1).
        assert item_width == 59
        assert column == PREFIX_WIDTH + len("cmatrix-longer") + 2

    def test_an_item_longer_than_the_screen_pins_the_column_at_the_wrap_width(self) -> None:
        rows = [_row("a", "x" * 200)]
        item_width, column = layout_widths(rows, THREE_ANSWERS, total_width=80)

        assert item_width == 59
        assert column == PREFIX_WIDTH + item_width + 2

    def test_two_answer_screens_reserve_less_because_their_widest_word_is_shorter(self) -> None:
        rows = [_row("a", "x" * 200)]
        three, _ = layout_widths(rows, THREE_ANSWERS, total_width=80)
        two, _ = layout_widths(rows, TWO_ANSWERS, total_width=80)

        assert two - three == len("never install") - len("skip now")

    def test_a_narrow_terminal_keeps_a_usable_item_column(self) -> None:
        item_width, _ = layout_widths([_row("a", "x" * 40)], THREE_ANSWERS, total_width=20)
        assert item_width == 12

    def test_wrap_label_breaks_a_token_that_has_nowhere_to_break(self) -> None:
        assert wrap_label("a" * 25, 10) == ["a" * 10, "a" * 10, "a" * 5]

    def test_wrap_label_keeps_a_hyphenated_package_name_whole_when_it_fits(self) -> None:
        assert wrap_label("fortunes-min (1:1.99.1)", 30) == ["fortunes-min (1:1.99.1)"]


class TestRenderRows:
    def test_every_decision_starts_at_the_same_column(self) -> None:
        rows = [_row("a", "sl"), _row("b", "cmatrix-longer"), _row("c", "x")]
        decisions = {"a": "apply", "b": "skip_once", "c": "skip_always"}
        _, column = layout_widths(rows, THREE_ANSWERS, total_width=100)

        lines = _lines(render_rows(rows, THREE_ANSWERS, decisions=decisions, focused=0, total_width=100))

        assert [line[column:] for line in lines] == ["install", "skip now", "never install"]

    def test_an_always_skipped_row_never_echoes_the_action_it_was_not_given(self) -> None:
        """The bug the rebuild had to make unrepresentable: the old second screen asked
        about permanence and confirmed back the item's ACTION ("remove fortunes-min"). The
        decision column now carries the answer that was given, and only that.
        """
        removal = DecisionOption(value="apply", key="y", word="remove", glyph="●", is_act=True)
        rows = [_row("a", "fortunes-min (1:1.99.1-7.3build1)")]

        line = _lines(
            render_rows(
                rows,
                (removal, _SKIP_ONCE, _SKIP_ALWAYS),
                decisions={"a": "skip_always"},
                focused=0,
                total_width=100,
            )
        )[0]

        assert line.endswith(_SKIP_ALWAYS.word)
        assert "remove" not in line

    def test_each_decision_has_its_own_glyph(self) -> None:
        """State is carried by a glyph, not by a background colour the user cannot read."""
        rows = [_row("a"), _row("b"), _row("c")]
        decisions = {"a": "apply", "b": "skip_once", "c": "skip_always"}

        lines = _lines(render_rows(rows, THREE_ANSWERS, decisions=decisions, focused=-1, total_width=100))
        glyphs = [line.strip()[0] for line in lines]

        assert glyphs == ["●", "○", "⊘"]
        assert len(set(glyphs)) == 3

    def test_the_permanent_answer_is_emphasised_apart_from_the_other_skip(self) -> None:
        """ "Always skip" outlives the run, and it used to be the one word on the screen
        with no styling at all. Skip-once stays plain — it is the answer that changes
        nothing, here or later.
        """
        rows = [_row("a"), _row("b"), _row("c")]
        decisions = {"a": "apply", "b": "skip_once", "c": "skip_always"}

        tokens = render_rows(rows, THREE_ANSWERS, decisions=decisions, focused=-1, total_width=100)
        words = {option.word for option in THREE_ANSWERS}
        styles = {text.strip(): style for style, text in tokens if text.strip() in words}

        assert styles == {
            _ACT.word: "class:decision-act",
            _SKIP_ONCE.word: "class:decision-skip",
            _SKIP_ALWAYS.word: "class:decision-permanent",
        }

    def test_a_glyph_carries_the_same_emphasis_as_the_word_it_belongs_to(self) -> None:
        tokens = render_rows([_row("a")], THREE_ANSWERS, decisions={"a": "skip_always"}, focused=-1, total_width=100)

        assert ("class:decision-permanent", "⊘ ") in tokens

    def test_no_row_repeats_the_group_verb_it_shares_with_its_title(self) -> None:
        rows = [_row("a", "sl (5.02-1)")]
        line = _lines(render_rows(rows, THREE_ANSWERS, decisions={"a": "apply"}, focused=-1, total_width=100))[0]

        item, _, decision = line.rpartition("  ")
        assert item.strip() == "● sl (5.02-1)"
        assert decision == "install"

    def test_a_row_whose_action_differs_keeps_it_as_a_prefix_and_as_its_own_word(self) -> None:
        rows = [_row("a", "sl"), _row("b", "vim", prefix="downgrade", act_word="downgrade")]

        lines = _lines(
            render_rows(rows, THREE_ANSWERS, decisions={"a": "apply", "b": "apply"}, focused=-1, total_width=100)
        )

        assert "sl" in lines[0] and "install" in lines[0]
        assert "downgrade vim" in lines[1] and lines[1].endswith("downgrade")

    def test_a_detail_lands_on_its_own_indented_line_under_the_item(self) -> None:
        rows = [_row("a", "cmatrix", detail="from a vendor repository")]

        lines = _lines(render_rows(rows, THREE_ANSWERS, decisions={"a": "apply"}, focused=-1, total_width=100))

        assert lines[1] == " " * PREFIX_WIDTH + "from a vendor repository"

    def test_a_wrapped_item_keeps_its_decision_on_the_first_line(self) -> None:
        rows = [_row("a", "y" * 120)]
        item_width, column = layout_widths(rows, THREE_ANSWERS, total_width=80)

        lines = _lines(render_rows(rows, THREE_ANSWERS, decisions={"a": "apply"}, focused=-1, total_width=80))

        assert lines[0][column:] == "install"
        assert lines[0][PREFIX_WIDTH:column].strip() == "y" * item_width
        assert "".join(line.strip() for line in lines[1:]) == "y" * (120 - item_width)

    def test_the_focused_row_is_the_only_one_carrying_a_pointer(self) -> None:
        rows = [_row("a"), _row("b")]
        tokens = render_rows(rows, THREE_ANSWERS, decisions={"a": "apply", "b": "apply"}, focused=1, total_width=100)

        assert ("[SetCursorPosition]", "") in tokens
        assert _lines(tokens)[0].startswith("   ")
        assert _lines(tokens)[1].startswith(" » ")

    def test_an_answered_screen_has_no_pointer_at_all(self) -> None:
        rows = [_row("a")]
        tokens = render_rows(rows, THREE_ANSWERS, decisions={"a": "apply"}, focused=-1, total_width=100)

        assert ("[SetCursorPosition]", "") not in tokens
        assert "»" not in _plain(tokens)

    def test_the_row_area_ends_without_a_trailing_blank_line(self) -> None:
        rows = [_row("a"), _row("b", detail="why")]
        tokens = render_rows(rows, THREE_ANSWERS, decisions={"a": "apply", "b": "apply"}, focused=0, total_width=100)

        assert tokens[-1][1] != "\n"
        assert not _plain(tokens).endswith("\n")


class TestLegend:
    def test_the_legend_names_every_key_with_what_it_does(self) -> None:
        text = legend(THREE_ANSWERS)

        assert "<y> install" in text
        assert "<s> skip now" in text
        assert "<x> never install" in text
        assert "<enter> confirm" in text

    def test_every_answer_gets_its_own_line_and_its_own_explanation(self) -> None:
        """The user's finding: three answers whose column words look interchangeable leave
        the difference between "this sync" and "for good" to be guessed. The hint is where
        each one says what it commits you to, so it may not share a line with another answer.
        """
        lines = legend(THREE_ANSWERS).split("\n")

        assert lines[0].startswith("<y> install") and lines[0].endswith("changes this sync")
        assert lines[1].startswith("<s> skip now") and lines[1].endswith("asked again next sync")
        assert lines[2].startswith("<x> never install") and lines[2].endswith("never asked again")

    def test_the_hints_start_at_one_column_for_the_whole_screen(self) -> None:
        answers = legend(THREE_ANSWERS).split("\n")[: len(THREE_ANSWERS)]

        starts = {line.index(option.hint) for line, option in zip(answers, THREE_ANSWERS, strict=True)}
        assert len(starts) == 1, f"hints do not share a column: {answers}"

    def test_an_answer_with_no_hint_is_still_listed(self) -> None:
        """`hint` is optional, and a caller that omits it loses the sentence, not the answer."""
        bare = DecisionOption(value="apply", key="y", word="install", glyph="●", is_act=True)

        assert "<y> install" in legend((bare,))

    def test_up_and_down_are_not_mentioned(self) -> None:
        """A list with a pointer on it does not need to be told it can be moved — and the
        keys that are listed are the ones a user would not guess."""
        assert "up/down" not in legend(THREE_ANSWERS)

    def test_a_bulk_key_says_that_it_sets_every_row(self) -> None:
        """The old legend said "<a> to toggle" for a key that toggled ALL of them."""
        assert "sets every row" in legend(THREE_ANSWERS)

    def test_a_two_answer_screen_is_short_by_exactly_the_answer_it_does_not_offer(self) -> None:
        assert "never install" not in legend(TWO_ANSWERS)
        assert "<s> skip now" in legend(TWO_ANSWERS)

    def test_a_narrow_screen_breaks_the_editing_keys_between_entries_never_mid_word(self) -> None:
        lines = legend(THREE_ANSWERS, width=40).split("\n")

        assert len(lines) > 1
        for entry in ("<shift+key> sets every row", "<x> never install", "<enter> confirm"):
            assert any(entry in line for line in lines)

    def test_a_narrow_screen_wraps_a_hint_under_itself(self) -> None:
        """Never under the key column, where a continuation would read as a further answer."""
        lines = legend(THREE_ANSWERS, width=44).split("\n")
        continuations = [line for line in lines if line.startswith(" ")]

        assert continuations, f"nothing wrapped at width 44: {lines}"
        assert all(not line.strip().startswith("<") for line in continuations)

    def test_the_legend_does_not_offer_abandoning_the_sync(self) -> None:
        """Ctrl-C still aborts; the legend is what to do with this screen, and an escape
        listed beside the answers gives it equal billing with the decision."""
        assert "ctrl" not in legend(THREE_ANSWERS).lower()
        assert "abort" not in legend(THREE_ANSWERS).lower()

    def test_a_wide_screen_keeps_the_editing_keys_on_one_line(self) -> None:
        """One line per answer plus one for the editing keys, and nothing wrapped."""
        assert len(legend(THREE_ANSWERS, width=500).split("\n")) == len(THREE_ANSWERS) + 1

    def test_the_invert_instruction_is_gone(self) -> None:
        """Deliberate negative control: questionary's `<i> to invert` was not useful here."""
        assert "invert" not in legend(THREE_ANSWERS)


class TestCursor:
    def test_no_control_draws_the_terminal_cursor(self) -> None:
        """prompt_toolkit focuses the first window when nothing is focusable, so a control
        that shows its cursor parks a blinking block on the header's `?`. The pointer is
        the only marker of where the user is.
        """
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            question = decision_list("t", rows=[_row("a"), _row("b")], options=THREE_ANSWERS)
            controls = [w.content for w in question.application.layout.walk() if isinstance(w, Window)]

            assert len(controls) == 2
            assert not any(control.create_content(80, 24).show_cursor for control in controls)


class TestConstruction:
    def test_a_key_may_not_be_the_abort_letter(self) -> None:
        """`<a>` is conventionally Abort, so it can never be the key that sets a decision."""
        options = (_ACT, DecisionOption(value="skip_always", key="a", word="always skip", glyph="⊘"))
        with pytest.raises(ValueError, match="abort"):
            decision_list("t", rows=[_row("a")], options=options)

    def test_two_options_may_not_share_a_key(self) -> None:
        clash = DecisionOption(value="skip_always", key="y", word="always skip", glyph="⊘")
        with pytest.raises(ValueError, match="unique"):
            decision_list("t", rows=[_row("a")], options=(_ACT, clash))

    def test_a_screen_needs_at_least_one_row(self) -> None:
        with pytest.raises(ValueError, match="at least one row"):
            decision_list("t", rows=[], options=THREE_ANSWERS)


class TestKeyHandling:
    """Driven through the real Application: keystrokes in, answer out."""

    def test_a_bare_enter_confirms_every_row_at_its_default(self) -> None:
        rows = [_row("a"), DecisionRow(row_id="b", label="pkg", default="skip_once")]

        assert _drive(_ENTER, rows=rows) == {"a": "apply", "b": "skip_once"}

    def test_a_key_sets_only_the_focused_row(self) -> None:
        rows = [_row("a"), _row("b"), _row("c")]

        assert _drive(f"{_DOWN}x{_ENTER}", rows=rows) == {"a": "apply", "b": "skip_always", "c": "apply"}

    def test_the_ends_of_the_list_are_walls(self) -> None:
        """Ruled by the user, replacing the wrap this screen shipped with: UP on the first
        row jumped to the last one, which on a list of packages is a silent jump to a row
        the next keystroke then answered.
        """
        rows = [_row("a"), _row("b")]

        # Up from the first row stays on the first, and down from the last stays on the last.
        assert _drive(f"{_UP}s{_ENTER}", rows=rows) == {"a": "skip_once", "b": "apply"}
        assert _drive(f"{_DOWN}{_DOWN}{_DOWN}s{_ENTER}", rows=rows) == {"a": "apply", "b": "skip_once"}

    def test_shift_of_a_key_sets_every_row(self) -> None:
        rows = [_row("a"), _row("b"), _row("c")]

        assert _drive(f"S{_ENTER}", rows=rows) == dict.fromkeys("abc", "skip_once")

    def test_space_cycles_the_focused_row_through_the_options(self) -> None:
        assert _drive(f" {_ENTER}", rows=[_row("a")]) == {"a": "skip_once"}
        assert _drive(f"  {_ENTER}", rows=[_row("a")]) == {"a": "skip_always"}

    def test_a_two_answer_screen_ignores_the_key_it_does_not_offer(self) -> None:
        assert _drive(f"n{_ENTER}", rows=[_row("a")], options=TWO_ANSWERS) == {"a": "apply"}

    def test_a_two_answer_screen_cycles_between_its_two_answers_only(self) -> None:
        assert _drive(f"  {_ENTER}", rows=[_row("a")], options=TWO_ANSWERS) == {"a": "apply"}

    def test_a_stray_letter_changes_nothing(self) -> None:
        assert _drive(f"qwz{_ENTER}", rows=[_row("a")]) == {"a": "apply"}

    def test_ctrl_c_answers_none(self) -> None:
        """The contract every review screen's abort handling is written against."""
        assert _drive(_CTRL_C, rows=[_row("a")]) is None


class TestTheAnsweredFrame:
    """What the user is left looking at, which is why nothing is echoed afterwards."""

    @staticmethod
    def _final_frame(keys: str, rows: list[DecisionRow]) -> str:
        buffer = io.StringIO()
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            with create_app_session(input=pipe, output=PlainTextOutput(buffer)):
                decision_list("Install apt packages", rows=rows, options=THREE_ANSWERS).ask()
        rendered = buffer.getvalue()
        return rendered[rendered.rindex("? Install apt packages") :]

    def test_the_answered_list_stays_on_screen_as_the_record(self) -> None:
        frame = self._final_frame(f"s{_DOWN}x{_ENTER}", [_row("a", "sl"), _row("b", "cmatrix")])

        assert "sl" in frame and "cmatrix" in frame
        assert _SKIP_ONCE.word in frame and _SKIP_ALWAYS.word in frame

    def test_nothing_is_echoed_after_the_question(self) -> None:
        """questionary's checkbox echoed one of four shapes ("done", "done (2 selections)",
        a description, or `[title]`) — one of which said "remove" on a permanence screen.
        The decision column already says it, per row, in one shape.
        """
        frame = self._final_frame(f"s{_ENTER}", [_row("a", "sl")])

        assert "done" not in frame
        assert "selections" not in frame

    def test_the_legend_comes_off_once_the_screen_is_answered(self) -> None:
        frame = self._final_frame(f"s{_ENTER}", [_row("a", "sl")])

        assert "up/down move" not in frame
        assert "<enter> confirm" not in frame
