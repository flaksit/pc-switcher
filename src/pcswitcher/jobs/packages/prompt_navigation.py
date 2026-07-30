"""The ends of a list of answers are walls, not doors.

questionary moves its cursor with `(pointed_at ± 1) % choice_count`, so UP on the first
answer lands on the last one. On a review screen the answers are not interchangeable — the
last one is routinely the recorded-for-good answer or "abort the sync" — and a wrap puts the cursor on a
consequential answer the user was moving AWAY from. There is no knob for it: `select_next`
and `select_previous` hardcode the modulo (questionary 2.1.1, latest at the time of
writing), and the movement keys are bound inside `questionary.prompts.select`.

So this module owns the rule for every prompt in the review: `step` for the screens that
manage their own cursor, and `select` for the ones questionary builds.
"""

from __future__ import annotations

from collections.abc import Callable

import questionary
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.keys import Keys
from questionary.prompts.common import InquirerControl, Separator

# Every key `questionary.prompts.select` binds to cursor movement under this project's
# settings (arrow keys, jk and emacs bindings all default on). A key missing here keeps
# questionary's wrapping handler, which is why they are enumerated rather than inferred.
_FORWARD_KEYS = (Keys.Down, Keys.ControlN, "j")
_BACKWARD_KEYS = (Keys.Up, Keys.ControlP, "k")


def step(index: int, delta: int, count: int) -> int:
    """The index `delta` rows away, stopping at either end instead of wrapping."""
    return max(0, min(count - 1, index + delta))


def select(message: str, choices: list[questionary.Choice]) -> questionary.Question:
    """`questionary.select`, with the ends of the list as walls.

    The movement keys are re-bound rather than the control patched: `move_cursor_down`
    walks on with `while not ic.is_selection_valid()`, which a clamping `select_next` would
    turn into an infinite loop the first time a screen ends in a disabled choice. Binding
    over it keeps the skip and the clamp in one handler that cannot spin — a later
    registration for the same key wins in prompt_toolkit, so questionary's own handler
    stays in place, unreachable.
    """
    question = questionary.select(message, choices=choices)
    controls = [c for c in question.application.layout.find_all_controls() if isinstance(c, InquirerControl)]
    assert len(controls) == 1, (
        f"expected exactly one InquirerControl in a questionary select layout, found {len(controls)}; "
        "questionary's layout changed and the end-of-list clamp is no longer bound to anything"
    )
    control = controls[0]

    def mover(delta: int) -> Callable[[KeyPressEvent], None]:
        def move(_event: KeyPressEvent) -> None:
            control.pointed_at = _next_selectable(control, delta)

        return move

    bindings = question.application.key_bindings
    assert isinstance(bindings, KeyBindings), (
        f"a questionary select's Application must carry the KeyBindings it was built with, got {type(bindings)}"
    )
    for key in _FORWARD_KEYS:
        bindings.add(key, eager=True)(mover(1))
    for key in _BACKWARD_KEYS:
        bindings.add(key, eager=True)(mover(-1))
    return question


def _next_selectable(control: InquirerControl, delta: int) -> int:
    """The nearest answer `delta` points towards that can actually be chosen.

    A separator or a disabled choice is stepped over, and a run of them at the end of the
    list is a wall like the end itself: the cursor stays where it is rather than landing on
    something the user cannot pick or sliding round to the other end.
    """
    index = control.pointed_at
    candidate = step(index, delta, control.choice_count)
    while candidate != index:
        choice = control.filtered_choices[candidate]
        if not isinstance(choice, Separator) and not choice.disabled:
            return candidate
        index, candidate = candidate, step(candidate, delta, control.choice_count)
    return control.pointed_at
