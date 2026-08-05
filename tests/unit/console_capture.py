r"""One console for every test that asserts against what pc-switcher printed.

A test that reads a buffer as plain text needs the buffer to hold plain text on every
machine that runs it. Rich decides that from the environment: `FORCE_COLOR` (Claude Code
and many terminals set it) makes even a `StringIO` console styled and terminal-like, so
the same assertion passes in CI and fails on a developer's machine.

`no_color=True` is not the fix -- it drops colour and keeps attributes, leaving
`\x1b[1m...\x1b[0m` around anything bold. `color_system=None` is: Rich then renders every
style as its text, so no escape sequence is emitted at all.
"""

from __future__ import annotations

import io

from rich.console import Console

# Escape sequences can only reach a PlainBuffer through a console this module did not
# build, so the message points at that rather than at the assertion that tripped over it.
_STYLED = (
    "captured console wrote an escape sequence: build it with captured_console() "
    "so styling cannot depend on the environment running the suite"
)


class PlainBuffer(io.StringIO):
    """A buffer that refuses to hand out styled text.

    Without this, styling leaking back in is only noticed by whoever happens to have
    `FORCE_COLOR` set, and only as an assertion that says the text was never printed.
    """

    def getvalue(self) -> str:
        printed = super().getvalue()
        assert "\x1b" not in printed, f"{_STYLED}\nBuffer: {printed!r}"
        return printed


def captured_console(*, width: int = 200, terminal: bool = False) -> tuple[Console, PlainBuffer]:
    """A real `Console` writing to a buffer: a `MagicMock` accepts any string and would
    pass no matter how the message was built.

    Width and terminal-ness are pinned so no assertion depends on the terminal running the
    suite, and styling is off at the source so the buffer holds the literal characters
    printed. Pass `terminal=True` for the branches that only run for a live terminal.
    """
    buffer = PlainBuffer()
    console = Console(file=buffer, width=width, force_terminal=terminal, color_system=None, highlight=False)
    return console, buffer
