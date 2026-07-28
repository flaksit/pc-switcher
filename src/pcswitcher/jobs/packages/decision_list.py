"""One screen per review group, one decision per row (ADR-020 D-07 + D-24).

D-07's three-way decision used to need two passes over the same group: a checkbox list
whose ticks meant apply, then a second checkbox list over the leftovers whose ticks meant
skip-always. Two screens made "apply" and "never offer this again" look like answers to
two different questions, and the second screen echoed the item's ACTION back ("remove
fortunes-min") after asking about permanence — the opposite of what was chosen.

This module replaces both with a single list. Each row shows the item, then the decision
the user has given it in a column aligned past the longest item on that screen; arrows
move, one key per option sets the focused row, Enter confirms the screen. A screen that
offers two answers is the same widget with one option missing, so the difference the user
sees is a shorter legend rather than a different flow.

Shaped after `questionary.prompts.checkbox` so it composes with the paused Rich Live
display exactly as the other prompts do: a `FormattedTextControl` whose token stream
carries `[SetCursorPosition]` for the focused row (which is what makes prompt_toolkit
scroll a list longer than the terminal), wrapped in a `questionary.Question` so callers
keep the `.ask()`-returns-`None`-on-Ctrl-C contract every other review screen relies on.

Two deliberate departures from `checkbox`:

- The row window is NOT hidden once the question is answered, and nothing is echoed after
  the message. The decision column IS the record, and leaving the answered list in the
  scrollback is what makes an echo unnecessary — an echo could only restate it less
  precisely, which is how the inverted "remove" echo happened.
- State is carried by a glyph per decision, never by background colour alone: a reversed
  background is invisible in some terminals and unreadable in others.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import questionary
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.styles import Style
from questionary.styles import merge_styles_default

__all__ = [
    "PREFIX_WIDTH",
    "DecisionOption",
    "DecisionRow",
    "decision_list",
    "layout_widths",
    "legend",
    "render_rows",
    "wrap_label",
]

# `a` is conventionally Abort in a terminal prompt, so it may never be the key that sets a
# decision — least of all the irreversible-feeling "always skip". `decision_list` rejects
# it rather than trusting every future caller to remember.
FORBIDDEN_KEY = "a"

_POINTER = "»"
# " » " / "   " — the pointer cell, then one glyph and its trailing space.
_POINTER_WIDTH = 3
_GLYPH_WIDTH = 2
PREFIX_WIDTH = _POINTER_WIDTH + _GLYPH_WIDTH

# Blank cells between the longest item and the decision column.
_COLUMN_GAP = 2

# Below this the item column stops shrinking and the decision column runs off the right
# edge instead: a two-character item column tells the user nothing, while a truncated
# decision word still leaves the item readable.
_MIN_ITEM_WIDTH = 12

# One cell left unwritten at the right edge. Filling the last column of a line makes a
# terminal wrap it, which splits the decision word across two lines on exactly the widest
# screen the layout is supposed to fit.
_RIGHT_MARGIN = 1

# Used only if the terminal reports no width at all.
_FALLBACK_WIDTH = 80

# Between two legend entries, and the indent the legend hangs at under the title.
_LEGEND_SEPARATOR = "   "
_LEGEND_INDENT = "  "

_DECISION_STYLE = Style(
    [
        ("decision-act", "fg:#5fd75f bold"),
        ("decision-skip", ""),
        ("detail", "fg:#8a8a8a"),
    ]
)


@dataclass(frozen=True)
class DecisionOption:
    """One answer a screen offers.

    `word` is what the decision column says when this option is chosen; for the act option
    it is the group's own verb ("install", "delete repository"), which a row may override
    through `DecisionRow.act_word` when its own action genuinely differs from the group's.
    `glyph` carries the state on its own, without colour.
    """

    value: str
    key: str
    word: str
    glyph: str
    is_act: bool = False


@dataclass(frozen=True)
class DecisionRow:
    """One item awaiting a decision on a screen.

    `prefix` is set only when this row's action differs from the group's — the group title
    already names the common one, so repeating it on every row is noise. `act_word`
    overrides the act option's column word for the same reason.
    """

    row_id: str
    label: str
    default: str
    prefix: str | None = None
    act_word: str | None = None
    detail: str | None = None


def _row_text(row: DecisionRow) -> str:
    return f"{row.prefix} {row.label}" if row.prefix else row.label


def _cell(row: DecisionRow, option: DecisionOption) -> str:
    return row.act_word if option.is_act and row.act_word else option.word


def layout_widths(
    rows: Sequence[DecisionRow],
    options: Sequence[DecisionOption],
    *,
    total_width: int,
) -> tuple[int, int]:
    """Return `(item_width, decision_column)` for one screen.

    The decision column sits `_COLUMN_GAP` cells right of the longest item, so it is
    left-aligned and clear of every row. An item longer than the screen allows is wrapped
    to `item_width`, which then becomes the longest item — so the column position is the
    same computation either way.
    """
    decision_width = max((len(_cell(row, option)) for row in rows for option in options), default=0)
    budget = total_width - PREFIX_WIDTH - _COLUMN_GAP - decision_width - _RIGHT_MARGIN
    item_width = max(budget, _MIN_ITEM_WIDTH)
    longest = max((len(_row_text(row)) for row in rows), default=0)
    return item_width, PREFIX_WIDTH + min(longest, item_width) + _COLUMN_GAP


def wrap_label(label: str, width: int) -> list[str]:
    """Wrap one item onto `width`-wide lines.

    `break_long_words` because a package name or a path has no spaces to break on and must
    still fit; `break_on_hyphens` off because `fortunes-min` is one name, not two words.
    """
    return textwrap.wrap(label, width, break_long_words=True, break_on_hyphens=False) or [""]


def render_rows(
    rows: Sequence[DecisionRow],
    options: Sequence[DecisionOption],
    *,
    decisions: Mapping[str, str],
    focused: int,
    total_width: int,
) -> list[tuple[str, str]]:
    """Build the whole row area's prompt_toolkit token stream.

    Pure, so the layout and the decision words are testable without a terminal: the
    control below calls exactly this. `focused` outside `range(len(rows))` (the control
    uses -1 once answered) renders no pointer and no `[SetCursorPosition]`.
    """
    by_value = {option.value: option for option in options}
    item_width, column = layout_widths(rows, options, total_width=total_width)
    tokens: list[tuple[str, str]] = []

    for index, row in enumerate(rows):
        option = by_value[decisions[row.row_id]]
        decision_style = "class:decision-act" if option.is_act else "class:decision-skip"
        is_focused = index == focused

        tokens.append(("class:pointer", f" {_POINTER} ") if is_focused else ("class:text", " " * _POINTER_WIDTH))
        if is_focused:
            tokens.append(("[SetCursorPosition]", ""))
        tokens.append((decision_style, f"{option.glyph} "))

        label_style = "class:highlighted" if is_focused else "class:text"
        lines = wrap_label(_row_text(row), item_width)
        tokens.append((label_style, lines[0]))
        tokens.append(("class:text", " " * max(column - PREFIX_WIDTH - len(lines[0]), 1)))
        tokens.append((decision_style, _cell(row, option)))
        tokens.append(("", "\n"))

        for line in lines[1:]:
            tokens.append(("class:text", " " * PREFIX_WIDTH))
            tokens.append((label_style, line))
            tokens.append(("", "\n"))

        if row.detail:
            for line in wrap_label(row.detail, item_width):
                tokens.append(("class:text", " " * PREFIX_WIDTH))
                tokens.append(("class:detail", line))
                tokens.append(("", "\n"))

    if tokens:
        # No trailing newline: prompt_toolkit would render it as an empty final line.
        tokens.pop()
    return tokens


def legend(options: Sequence[DecisionOption], *, width: int = 0) -> str:
    """The instruction line(s) under the title.

    Every key the screen accepts, said as what it does — a two-answer screen's legend is
    shorter by exactly the option it does not offer, which is how the user sees that the
    permanent answer is unavailable rather than merely unmentioned.

    `width` packs the entries onto lines no wider than that, breaking BETWEEN entries.
    Left to the terminal, the wrap lands mid-word ("shift+key sets / every row"). 0 keeps
    it on one line.
    """
    parts = ["up/down move"]
    parts.extend(f"<{option.key}> {option.word}" for option in options)
    parts.extend(
        (
            "<space> cycles",
            "shift+key sets every row",
            "<enter> confirm",
            "<ctrl-c> abort",
        )
    )
    if width <= 0:
        return _LEGEND_SEPARATOR.join(parts)

    lines: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{_LEGEND_SEPARATOR}{part}" if current else part
        if current and len(candidate) > width:
            lines.append(current)
            current = part
        else:
            current = candidate
    lines.append(current)
    return "\n".join(lines)


def _screen_width() -> int:
    return get_app().output.get_size().columns or _FALLBACK_WIDTH


class _DecisionListControl(FormattedTextControl):
    """The row area: current decision per row, plus which row has focus."""

    def __init__(
        self,
        rows: Sequence[DecisionRow],
        options: Sequence[DecisionOption],
        width_of_screen: Callable[[], int],
    ) -> None:
        self.rows = tuple(rows)
        self.options = tuple(options)
        self.decisions: dict[str, str] = {row.row_id: row.default for row in rows}
        self.focused = 0
        self._width_of_screen = width_of_screen
        super().__init__(self._tokens)

    def _tokens(self) -> list[tuple[str, str]]:
        return render_rows(
            self.rows,
            self.options,
            decisions=self.decisions,
            focused=self.focused,
            total_width=self._width_of_screen(),
        )


def _validate(rows: Sequence[DecisionRow], options: Sequence[DecisionOption]) -> None:
    """Reject a screen no caller should be able to build — including one that would steal
    Abort's letter, which no future caller has to remember on its own."""
    if not rows:
        raise ValueError("a decision screen needs at least one row")
    keys = [option.key for option in options]
    if len(set(keys)) != len(keys):
        raise ValueError(f"decision keys must be unique, got {keys}")
    if FORBIDDEN_KEY in keys:
        raise ValueError(f"{FORBIDDEN_KEY!r} is conventionally abort and cannot set a decision")
    if any(len(key) != 1 or not key.islower() for key in keys):
        raise ValueError(f"decision keys must be single lowercase letters, got {keys}")


def decision_list(
    message: str,
    *,
    rows: Sequence[DecisionRow],
    options: Sequence[DecisionOption],
    style: Style | None = None,
) -> questionary.Question:
    """Build the one-screen-per-group decision prompt.

    `.ask()` returns `{row_id: option value}`, or `None` when the user pressed Ctrl-C —
    the same contract every other review screen is written against.
    """
    _validate(rows, options)
    control = _DecisionListControl(rows, options, _screen_width)
    answered = False

    def header_tokens() -> list[tuple[str, str]]:
        tokens = [("class:qmark", "?"), ("class:question", f" {message}")]
        if not answered:
            packed = legend(options, width=_screen_width() - len(_LEGEND_INDENT))
            hung = "\n".join(f"{_LEGEND_INDENT}{line}" for line in packed.split("\n"))
            tokens.append(("class:instruction", f"\n{hung}"))
        return tokens

    bindings = KeyBindings()

    # Registered by explicit call rather than as decorators: a decorated handler binds a
    # name nothing reads, which is a genuine unused-function report everywhere else.
    def _abort(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    for key in (Keys.ControlC, Keys.ControlQ):
        bindings.add(key, eager=True)(_abort)

    def _mover(delta: int) -> Callable[[KeyPressEvent], None]:
        def move(_event: KeyPressEvent) -> None:
            control.focused = (control.focused + delta) % len(control.rows)

        return move

    for key in (Keys.Down, Keys.ControlN):
        bindings.add(key, eager=True)(_mover(1))
    for key in (Keys.Up, Keys.ControlP):
        bindings.add(key, eager=True)(_mover(-1))

    def _setter(value: str) -> Callable[[KeyPressEvent], None]:
        def set_focused(_event: KeyPressEvent) -> None:
            control.decisions[control.rows[control.focused].row_id] = value

        return set_focused

    def _set_all(value: str) -> Callable[[KeyPressEvent], None]:
        def set_every(_event: KeyPressEvent) -> None:
            control.decisions = dict.fromkeys(control.decisions, value)

        return set_every

    for option in options:
        bindings.add(option.key, eager=True)(_setter(option.value))
        bindings.add(option.key.upper(), eager=True)(_set_all(option.value))

    def _cycle(_event: KeyPressEvent) -> None:
        """Step the focused row through the options, for a user who has not read the legend."""
        values = [option.value for option in options]
        row_id = control.rows[control.focused].row_id
        control.decisions[row_id] = values[(values.index(control.decisions[row_id]) + 1) % len(values)]

    def _confirm(event: KeyPressEvent) -> None:
        nonlocal answered
        answered = True
        # The answered list stays in the scrollback as the record of what was decided, so
        # the pointer and the legend — both meaningless once it is answered — come off it.
        control.focused = -1
        event.app.exit(result=dict(control.decisions))

    def _swallow(_event: KeyPressEvent) -> None:
        """No key outside the legend does anything, so a stray letter cannot edit a row."""

    bindings.add(" ", eager=True)(_cycle)
    bindings.add(Keys.ControlM, eager=True)(_confirm)
    bindings.add(Keys.Any)(_swallow)

    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(header_tokens), dont_extend_height=True, wrap_lines=True),
                Window(control, dont_extend_height=True),
            ]
        )
    )
    return questionary.Question(
        Application(
            layout=layout,
            key_bindings=bindings,
            style=merge_styles_default([_DECISION_STYLE, style]),
        )
    )
