"""Terminal capability checks shared across pc-switcher."""

from __future__ import annotations

import sys
import termios
import tty
from collections.abc import Sequence

from rich.console import Console

_CTRL_C = "\x03"
_CTRL_D = "\x04"


def is_interactive(console: Console) -> bool:
    """Return True only when the run is fully interactive on both stdin and stdout.

    A run is interactive only if BOTH ends are a terminal: a real terminal on
    stdout (``console.is_terminal``) so a live UI / prompt is actually visible,
    AND a TTY on stdin (``sys.stdin.isatty()``) so the user can actually answer
    a prompt. Requiring both keeps logging setup and the confirmer in agreement
    under mixed redirection (e.g. stdout is a TTY but stdin is ``/dev/null``):
    a single split signal previously let the live UI + UILogHandler activate
    while confirmations silently fell back to ``--allow-*`` flags.
    """
    return console.is_terminal and sys.stdin.isatty()


def read_single_key(choices: Sequence[str]) -> str:
    """Block until one of ``choices`` is pressed, and return it — no Enter required.

    For prompts asked once per operation, where the Enter of a line-based prompt is pure
    friction: a run under ``--confirm-each-command`` asks dozens of times (#241).

    Any key outside ``choices`` is discarded and the read continues, which is what makes a
    stray Enter harmless where the prompt has no default. Input queued BEFORE the prompt is
    discarded too (``TCIFLUSH``): a key pressed while the previous command was running must
    never answer a question the user has not seen yet — the risk a single-key prompt adds
    over a line-based one, since there is no line to review before it commits.

    Args:
        choices: The accepted keys, each a single character, matched case-insensitively.

    Returns:
        The pressed key, as it appears in ``choices``.

    Raises:
        EOFError: stdin reached end-of-file, or Ctrl-D was pressed — nobody is answering.
        KeyboardInterrupt: Ctrl-C, which cbreak mode delivers as a byte rather than a
            signal, so it is re-raised here to read the same as an interrupted prompt.
    """
    accepted = {choice.lower(): choice for choice in choices}
    if not sys.stdin.isatty():
        # No terminal to put in cbreak mode; fall back to a line read so the prompt is
        # still answerable (piped stdin in a test or a harness). `input()` raises EOFError
        # by itself, which is the same outcome the cbreak path gives.
        while True:
            if (key := input().strip().lower()[:1]) in accepted:
                return accepted[key]

    file_descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(file_descriptor)
    try:
        tty.setcbreak(file_descriptor)
        termios.tcflush(file_descriptor, termios.TCIFLUSH)
        while True:
            key = sys.stdin.read(1)
            if key in {"", _CTRL_D}:
                raise EOFError("stdin closed while waiting for a key")
            if key == _CTRL_C:
                raise KeyboardInterrupt
            if (lowered := key.lower()) in accepted:
                return accepted[lowered]
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, saved)
