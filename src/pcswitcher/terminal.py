"""Terminal capability checks shared across pc-switcher."""

from __future__ import annotations

import sys

from rich.console import Console


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
