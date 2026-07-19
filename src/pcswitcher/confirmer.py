"""Reusable interactive confirmation prompt shared by the orchestrator and jobs.

The orchestrator's topology out-of-order gate and (per the ADR-015 refinement)
FolderSyncJob's first-sync overwrite gate need the same "pause the TUI, show a
yellow warning panel, ask the user to continue" behaviour. Rather than duplicate
that block, both go through a `Confirmer`:

- interactive (both stdin and stdout are TTYs, per ``is_interactive``): pause the live
  TUI, print a Rich ``Panel`` with the warning, prompt ``Continue anyway? [y/n]``
  (default ``n``), resume the TUI, and return whether the user chose ``y``.
- non-interactive (either end is not a TTY): there is nobody who can both see and answer
  the prompt, so the decision falls back to the caller's ``--allow-*`` flag. When
  ``allow`` is True the action is auto-approved (logged); otherwise it is refused
  (logged, with a hint to pass the flag) and False is returned.

Dry-run is intentionally NOT handled here: callers decide how a rehearsal should
behave (ADR-014) and short-circuit before calling ``confirm``.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from pcswitcher.terminal import is_interactive

__all__ = ["Confirmer", "PausableUI", "TerminalUIConfirmer"]


class PausableUI(Protocol):
    """The subset of ``TerminalUI`` the confirmer needs to pause/resume the live display."""

    def pause(self) -> None: ...

    def resume(self) -> None: ...


@runtime_checkable
class Confirmer(Protocol):
    """A yes/no confirmation gate that adapts to interactive vs non-interactive runs."""

    async def confirm(
        self,
        *,
        title: str,
        message: str,
        allow: bool,
        allow_flag: str,
        log_extra: dict[str, Any] | None = None,
    ) -> bool:
        """Ask the user to confirm a potentially destructive action.

        Args:
            title: Short heading for the warning (panel title / log prefix).
            message: Rich-markup body explaining the risk.
            allow: The caller's ``--allow-*`` flag value; used only in non-interactive
                mode to decide auto-approve (True) vs refuse (False).
            allow_flag: The flag name (e.g. ``--allow-first-sync``) surfaced in messages
                so the user knows how to proceed non-interactively.
            log_extra: Structured logging context (``job``/``host``) for the caller.

        Returns:
            True to proceed, False to abort.
        """
        ...


class TerminalUIConfirmer:
    """Confirmer backed by the Rich console and the live ``TerminalUI``.

    Holds the console and the TUI handle so it can pause the live display around the
    blocking prompt (a Rich ``Live`` and ``Prompt.ask`` cannot share the terminal).
    """

    def __init__(
        self,
        console: Console,
        ui: PausableUI,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._console = console
        self._ui = ui
        self._logger = logger if logger is not None else logging.getLogger("pcswitcher.confirmer")

    async def confirm(
        self,
        *,
        title: str,
        message: str,
        allow: bool,
        allow_flag: str,
        log_extra: dict[str, Any] | None = None,
    ) -> bool:
        extra: dict[str, Any] = {"job": "confirmer", "host": "source", **(log_extra or {})}

        if not is_interactive(self._console):
            # Not fully interactive (stdin and/or stdout is not a TTY): there is
            # nobody who can both see the prompt and answer it, so the
            # ``--allow-*`` flag is the only way to express intent. Shares
            # ``is_interactive`` with setup_logging so the live UI and the
            # prompt agree about interactivity under mixed redirection.
            if allow:
                self._logger.info("%s — auto-approved by %s", title, allow_flag, extra=extra)
                return True
            self._logger.warning(
                "%s — refused in non-interactive mode; pass %s to proceed",
                title,
                allow_flag,
                extra=extra,
            )
            self._console.print(f"[yellow]Warning: {title}[/yellow]")
            self._console.print(message)
            self._console.print(f"\nUse [bold]{allow_flag}[/bold] to proceed in non-interactive mode.")
            return False

        # Interactive: pause the live display, show the warning, prompt, resume.
        self._ui.pause()
        try:
            self._console.print()
            self._console.print(Panel(message, title=title, border_style="yellow"))
            self._console.print()
            response = Prompt.ask(
                "[bold]Continue anyway?[/bold]",
                choices=["y", "n"],
                default="n",
            )
            return response.lower() == "y"
        finally:
            self._ui.resume()
