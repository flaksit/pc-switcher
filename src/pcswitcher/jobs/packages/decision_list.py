"""One screen per review group, one decision per row (`PKG-FR-SKIP-ONCE` + `PKG-FR-BATCHED`).

`PKG-FR-SKIP-ONCE`'s three-way decision used to need two passes over the same group: a checkbox list
whose ticks meant apply, then a second checkbox list over the leftovers whose ticks meant
skip-always. Two screens made "apply" and "never offer this again" look like answers to
two different questions, and the second screen echoed the item's ACTION back ("remove
fortunes-min") after asking about permanence — the opposite of what was chosen.

This module replaces both with a single list. Each row shows the item, then the decision
the user has given it in a column aligned past the longest item on that screen; arrows
move, one key per option sets the focused row, Enter confirms the screen. A screen that
offers two answers is the same widget with one option missing, so the difference the user
sees is a shorter legend rather than a different flow.

Every row starts on an answer, which is what makes Enter alone a valid response — except on
a screen that opts out with `UNANSWERED`, where no answer is harmless enough to start on and
Enter is refused until every row has been answered.

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
  background is invisible in some terminals and unreadable in others. Foreground colour is
  emphasis on top of that — green for the act, dark rose for the answer that outlives the
  run — so a terminal with no colour at all loses nothing but the emphasis.
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

from pcswitcher.jobs.packages.prompt_navigation import step

__all__ = [
    "PREFIX_WIDTH",
    "UNANSWERED",
    "DecisionOption",
    "DecisionRow",
    "decision_list",
    "layout_widths",
    "legend",
    "render_rows",
    "wrap_label",
]

# `a` is conventionally Abort in a terminal prompt, so it may never be the key that sets a
# decision — least of all the one that is recorded and never asked again. `decision_list` rejects
# it rather than trusting every future caller to remember.
FORBIDDEN_KEY = "a"

# The `DecisionRow.default` for a row that must not start answered, and the value the
# control carries for it until a key sets a real one. Opt-in per screen: a row that names
# a real option value behaves exactly as it always has, so a screen with a harmless answer
# keeps starting on it (`PKG-FR-HARMLESS-DEFAULT`).
#
# It is for the screen where NO answer is harmless — every one of them writes a record that
# outlives the run — so there is nothing safe to start on and a confirmed-unread screen must
# not be indistinguishable from a decision. `<enter>` is refused while any row still carries
# it, and the refusal names the rows.
#
# Not a value any option may take: it must be unreachable by a keypress, and the guard has
# to be able to tell "nobody has answered this" from every answer on offer.
UNANSWERED = "__unanswered__"

# What an unanswered row shows in the decision column. Yellow, the one place this widget
# spends the colour the rest of the TUI uses for warnings and gates: an unanswered row IS
# the thing standing between the user and confirming, and the glyph is a question because
# the row is still a question.
_UNANSWERED_WORD = "not answered"
_UNANSWERED_GLYPH = "?"

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

# Between an answer's key+word and the sentence explaining it, and the narrowest that
# sentence's column may be squeezed to before it stops wrapping and runs off the edge.
_HINT_GAP = "   "
_MIN_HINT_WIDTH = 20

_DECISION_STYLE = Style(
    [
        # Light blue, deliberately NOT green: the act is as often "remove" or "delete" as
        # it is "install", and green reads as the safe answer on a screen where acting is
        # the destructive one. Blue is the answer that DOES something, without a verdict on
        # whether doing it is a good idea.
        ("decision-act", "fg:#5fafff bold"),
        ("decision-skip", ""),
        # Dark rose, and the only other emphasis on the screen: the recorded answer is the
        # one that outlives the run, and it read as the least conspicuous of the three
        # because it was the one word on the screen with no styling at all. Not yellow,
        # which this TUI already spends on warnings and gates, and not hard red — the
        # destructive answer here is routinely the green one ("remove", "delete
        # repository"), so red would point at the wrong row.
        ("decision-permanent", "fg:#d75f87 bold"),
        ("decision-unanswered", "fg:#d7af00 bold"),
        ("unanswered-notice", "fg:#d7af00"),
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

    `is_permanent` marks an answer that is recorded and never asked about again, which is
    what earns it its own emphasis in the column. It is independent of `is_act`: an act is
    a change to the machine, a permanent answer is a change to what pc-switcher will ask.

    `hint` is the sentence beside this answer in the legend, naming the machine it happens
    to and how long it lasts. The column `word` has to stay short enough to align three of
    them past the longest item on the screen, which is not enough room to say either.
    """

    value: str
    key: str
    word: str
    glyph: str
    is_act: bool = False
    is_permanent: bool = False
    hint: str = ""


# Never in a screen's `options`: it is not an answer, so it has no key and no keybinding.
# It exists so the column, the glyph and the style of an unanswered row are decided by the
# same code that decides them for every real answer.
_UNANSWERED_OPTION = DecisionOption(value=UNANSWERED, key="", word=_UNANSWERED_WORD, glyph=_UNANSWERED_GLYPH)


@dataclass(frozen=True)
class DecisionRow:
    """One item awaiting a decision on a screen.

    `default` is the option value the row starts on, or `UNANSWERED` for a screen that must
    not pre-answer anything.

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


def _column_options(rows: Sequence[DecisionRow], options: Sequence[DecisionOption]) -> tuple[DecisionOption, ...]:
    """`options`, plus the unanswered pseudo-option where a row starts unanswered.

    Read off the DEFAULTS rather than off the current decisions so the decision column
    cannot move as rows are answered: a table whose column shifts under the user's own
    keystrokes is unreadable, and nothing ever sets a row back to unanswered.
    """
    if any(row.default == UNANSWERED for row in rows):
        return (*options, _UNANSWERED_OPTION)
    return tuple(options)


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
    decision_width = max(
        (len(_cell(row, option)) for row in rows for option in _column_options(rows, options)), default=0
    )
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


def _decision_class(option: DecisionOption) -> str:
    """The style class for a chosen answer's glyph and column word — one per state, so the
    glyph and the word always agree and a colourless terminal loses nothing but the tint."""
    if option.value == UNANSWERED:
        return "decision-unanswered"
    if option.is_act:
        return "decision-act"
    return "decision-permanent" if option.is_permanent else "decision-skip"


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
    by_value = {option.value: option for option in _column_options(rows, options)}
    item_width, column = layout_widths(rows, options, total_width=total_width)
    tokens: list[tuple[str, str]] = []

    for index, row in enumerate(rows):
        option = by_value[decisions[row.row_id]]
        decision_style = f"class:{_decision_class(option)}"
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
            # Newlines in a detail are paragraph breaks, wrapped independently: a row that
            # states a finding and then why it is being asked about must not run the two
            # sentences together into one block.
            for paragraph in row.detail.split("\n"):
                for line in wrap_label(paragraph, item_width):
                    tokens.append(("class:text", " " * PREFIX_WIDTH))
                    tokens.append(("class:detail", line))
                    tokens.append(("", "\n"))

    if tokens:
        # No trailing newline: prompt_toolkit would render it as an empty final line.
        tokens.pop()
    return tokens


def legend(options: Sequence[DecisionOption], *, width: int = 0, every_row_required: bool = False) -> str:
    """The instruction block under the title: one line per ANSWER, then the editing keys.

    An answer gets a line of its own because the column word cannot carry what the answer
    commits the user to. A word short enough to align in that column cannot also say that
    the decision is recorded and never asked again, and a packed one-line legend has no room
    to say it — which left the difference between the second and third answers to be
    inferred from two words that look interchangeable. `DecisionOption.hint` is that sentence, aligned into a
    second column so the three read as a set.

    A two-answer screen's block is shorter by exactly the option it does not offer, which is
    how the user sees that the permanent answer is unavailable rather than merely unmentioned.

    The editing keys stay packed on one line and up/down is not among them: a list with a
    pointer on it does not need to be told it can be moved. Ctrl-C still aborts and is also
    unlisted — a legend is what to do with the screen in front of you, and offering "abandon
    the sync" beside the answers gives an escape equal billing with the decision.

    `width` wraps: an answer's hint continues under itself, and the editing keys break
    BETWEEN entries (left to the terminal that wrap lands mid-phrase, "shift+key sets /
    every row"). 0 means no wrapping at all.

    `every_row_required` says so on the `<enter>` entry itself, for the screen where no row
    starts answered (`UNANSWERED`). The refusal names the rows, but only after the user has
    pressed a key that did nothing — telling them up front is what stops the press.
    """
    keyed = [f"<{option.key}> {option.word}" for option in options]
    hint_column = max((len(text) for text in keyed), default=0) + len(_HINT_GAP)
    lines: list[str] = []
    for text, option in zip(keyed, options, strict=True):
        if not option.hint:
            lines.append(text)
            continue
        answer = f"{text.ljust(hint_column)}{option.hint}"
        # A hint is a sentence, so it wraps rather than truncates, and its continuation
        # lines sit under the hint column — never under the key, which would read as a
        # further answer.
        if width > 0 and len(answer) > width:
            lines.extend(
                textwrap.wrap(
                    answer,
                    max(width, hint_column + _MIN_HINT_WIDTH),
                    subsequent_indent=" " * hint_column,
                )
            )
        else:
            lines.append(answer)

    confirm = "<enter> confirm, once every row is answered" if every_row_required else "<enter> confirm"
    editing = ["<space> cycles", "<shift+key> sets every row", confirm]
    if width <= 0:
        return "\n".join([*lines, _LEGEND_SEPARATOR.join(editing)])

    current = ""
    for part in editing:
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
        # Header state, held here because the header is rebuilt from it on every frame:
        # whether the screen has been confirmed (which takes the legend off), and the
        # refusal a rejected `<enter>` leaves standing.
        self.answered = False
        self.notice: str | None = None
        self._width_of_screen = width_of_screen
        # The pointer marks the focused row; a terminal cursor blinking on top of the glyph
        # would be a second, competing marker. `[SetCursorPosition]` is still emitted — it
        # is what scrolls a list longer than the screen — it is simply not drawn.
        super().__init__(self._tokens, show_cursor=False)

    def _tokens(self) -> list[tuple[str, str]]:
        return render_rows(
            self.rows,
            self.options,
            decisions=self.decisions,
            focused=self.focused,
            total_width=self._width_of_screen(),
        )

    def set_decision(self, row_id: str, value: str) -> None:
        """Answer one row, dropping any refusal that named it: a notice still listing a row
        the user has just answered is worse than none."""
        self.decisions[row_id] = value
        self.notice = None

    def set_every_decision(self, value: str) -> None:
        self.decisions = dict.fromkeys(self.decisions, value)
        self.notice = None


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
    values = {option.value for option in options}
    if UNANSWERED in values:
        raise ValueError(f"{UNANSWERED!r} marks a row nobody has answered and cannot be an answer")
    # A row starting on a value no option offers renders as a KeyError deep in the token
    # stream, which is a stack trace rather than a message. Caught here so the sentinel
    # cannot be misspelt into one either.
    unknown = {row.default for row in rows} - values - {UNANSWERED}
    if unknown:
        raise ValueError(f"a row's default must be an offered answer or {UNANSWERED!r}, got {sorted(unknown)}")


def _hang(text: str, width: int) -> str:
    """Wrap `text` and indent every line to the legend's own hanging indent.

    Newlines are paragraph breaks, wrapped independently, so a block that states a finding
    and then its ground does not run the two sentences together.
    """
    lines = [line for paragraph in text.split("\n") for line in (textwrap.wrap(paragraph, max(width, 1)) or [""])]
    return "\n".join(f"{_LEGEND_INDENT}{line}" for line in lines)


def _unanswered_notice(rows: Sequence[DecisionRow], decisions: Mapping[str, str]) -> str | None:
    """The line a refused `<enter>` puts up, naming every row still unanswered — or None
    when there are none and the screen may be confirmed.

    Names them because "answer everything first" on a list longer than the screen is not an
    instruction anyone can act on without hunting for the rows it means.
    """
    pending = [_row_text(row) for row in rows if decisions[row.row_id] == UNANSWERED]
    if not pending:
        return None
    return f"Every row needs an answer before <enter>. Not answered yet: {', '.join(pending)}."


def _header_tokens(
    control: _DecisionListControl,
    *,
    message: str,
    explanation: str | None,
    every_row_required: bool,
    width: int,
) -> list[tuple[str, str]]:
    """The block above the table: the question, its ground, the key legend, and any refusal
    a rejected `<enter>` left standing. Rebuilt every frame, so it reads the control's own
    state rather than a snapshot taken when the screen was built.
    """
    tokens = [("class:qmark", "?"), ("class:question", f" {message}")]
    if explanation:
        tokens.append(("class:detail", f"\n{_hang(explanation, width)}"))
    if not control.answered:
        # Already wrapped by `legend`, whose continuation lines carry their own hanging
        # indent — so this only prepends the block indent, never re-wraps.
        packed = legend(control.options, width=width, every_row_required=every_row_required)
        hung = "\n".join(f"{_LEGEND_INDENT}{line}" for line in packed.split("\n"))
        tokens.append(("class:instruction", f"\n{hung}"))
    if control.notice:
        # Last, so it sits against the table it is about rather than above the legend the
        # user has already read past.
        tokens.append(("class:unanswered-notice", f"\n{_hang(control.notice, width)}"))
    return tokens


def decision_list(
    message: str,
    *,
    rows: Sequence[DecisionRow],
    options: Sequence[DecisionOption],
    explanation: str | None = None,
    style: Style | None = None,
) -> questionary.Question:
    """Build the one-screen-per-group decision prompt.

    `.ask()` returns `{row_id: option value}`, or `None` when the user pressed Ctrl-C —
    the same contract every other review screen is written against.

    `explanation` is why this screen is being shown, printed between the question and the
    key legend: a screen asking about ONE item states the concrete case in its title, and
    the ground for it belongs with the question rather than under the single row, where it
    read as an annotation on an answer the user had not made yet. It survives the answer,
    unlike the legend — the ground the decision was taken on is part of what the scrollback
    records.

    A row whose `default` is `UNANSWERED` starts on no answer at all, and `<enter>` is
    REFUSED while any such row is left — with a line naming the ones still outstanding,
    directly above the table where they are. Without it a key that does nothing is
    indistinguishable from a screen that has hung.
    """
    _validate(rows, options)
    control = _DecisionListControl(rows, options, _screen_width)
    starts_unanswered = any(row.default == UNANSWERED for row in rows)

    def header_tokens() -> list[tuple[str, str]]:
        return _header_tokens(
            control,
            message=message,
            explanation=explanation,
            every_row_required=starts_unanswered,
            width=_screen_width() - len(_LEGEND_INDENT),
        )

    bindings = KeyBindings()

    # Registered by explicit call rather than as decorators: a decorated handler binds a
    # name nothing reads, which is a genuine unused-function report everywhere else.
    def _abort(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    for key in (Keys.ControlC, Keys.ControlQ):
        bindings.add(key, eager=True)(_abort)

    def _mover(delta: int) -> Callable[[KeyPressEvent], None]:
        def move(_event: KeyPressEvent) -> None:
            control.focused = step(control.focused, delta, len(control.rows))

        return move

    for key in (Keys.Down, Keys.ControlN):
        bindings.add(key, eager=True)(_mover(1))
    for key in (Keys.Up, Keys.ControlP):
        bindings.add(key, eager=True)(_mover(-1))

    def _setter(value: str) -> Callable[[KeyPressEvent], None]:
        def set_focused(_event: KeyPressEvent) -> None:
            control.set_decision(control.rows[control.focused].row_id, value)

        return set_focused

    def _set_all(value: str) -> Callable[[KeyPressEvent], None]:
        def set_every(_event: KeyPressEvent) -> None:
            control.set_every_decision(value)

        return set_every

    for option in options:
        bindings.add(option.key, eager=True)(_setter(option.value))
        bindings.add(option.key.upper(), eager=True)(_set_all(option.value))

    def _cycle(_event: KeyPressEvent) -> None:
        """Step the focused row through the options, for a user who has not read the legend.

        An unanswered row steps to the FIRST option: unanswered is not one of the states
        being cycled, and cycling back into it would let the user leave a row on a value
        `<enter>` refuses.
        """
        values = [option.value for option in options]
        row_id = control.rows[control.focused].row_id
        current = control.decisions[row_id]
        following = values[(values.index(current) + 1) % len(values)] if current in values else values[0]
        control.set_decision(row_id, following)

    def _confirm(event: KeyPressEvent) -> None:
        control.notice = _unanswered_notice(control.rows, control.decisions)
        if control.notice is not None:
            return
        control.answered = True
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
                # prompt_toolkit focuses the FIRST window when nothing is focusable, and a
                # focused control that shows its cursor parks the terminal's blinking block
                # on the header's first character — the `?`, which is the one thing on the
                # screen the user has no reason to look at.
                Window(
                    FormattedTextControl(header_tokens, show_cursor=False),
                    dont_extend_height=True,
                    wrap_lines=True,
                ),
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
