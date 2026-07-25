"""Per-action confirmation gate behind `--confirm-each-command`.

A package sync run converges dozens of small mutating commands — `apt-get install`,
`snap remove`, `sudo install` into `/etc/apt`, an SFTP push of the snippet registry — each
of which was approved only in aggregate, as a ticked line in a batched review. This gate
inserts one prompt before every single one of them, showing the EXACT operation about to
happen: the literal shell command as it will be passed to the shell, or the local path and
remote destination of a file transfer. Nothing is paraphrased or reconstructed for display,
because a display string that differs from what runs is worse than no display at all.

Distinct from `pcswitcher.confirmer.Confirmer`, which gates ONE coarse decision per run
(first-sync overwrite, out-of-order topology) and falls back to an `--allow-*` flag when
nobody is there to answer. This gate has no such fallback and never auto-proceeds: it is
opt-in per run and refused outright without a TTY (`cli.sync`), because a gate that
auto-proceeds when unanswerable is precisely the failure it exists to prevent.

Two outcomes only, with no default (the user must type one): proceed, or abort the whole
sync. There is deliberately no "skip this command" — a single reviewed item can span
several commands (apt source stage-then-promote, snap install-then-switch-channel), so
skipping one of them leaves that item half-applied, which is a worse state than either
finishing it or stopping.

Generic on purpose, and invoked from exactly one place: `pcswitcher.executor`. Any caller
— job, orchestrator, helper — gates a write by passing `mutates="..."` to the executor
method it already uses, so there is no second API to remember and no per-job wiring. The
executor supplies `job` (from its `active_job` context variable) and `host` (from which
executor it is). Every write passes `mutates` except `folder_sync`'s rsync pass (#209);
`tests/unit/test_mutates_audit.py` holds that line.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from pcswitcher.confirmer import PausableUI
from pcswitcher.models import Host, SyncAbortedByUser

__all__ = ["StepGate", "TerminalUIStepGate"]

_PROCEED = "p"
_ABORT = "a"


@runtime_checkable
class StepGate(Protocol):
    """A per-action confirmation gate: show one pending modification, block, decide."""

    async def confirm_action(self, *, job: str, host: Host, description: str, command: str) -> None:
        """Show a pending modification and block until the user decides.

        Args:
            job: Name of the job about to modify something (panel heading, log context).
            host: Which machine the modification lands on.
            description: Short human phrase for what this achieves ("install firefox").
            command: The operation VERBATIM — the shell command as it will be executed, or
                a `send_file <local> -> <remote>` rendering for a transfer.

        Returns:
            None when the user chose to proceed.

        Raises:
            SyncAbortedByUser: The user aborted, or the prompt could not be answered
                (EOF / Ctrl-C). Never swallowed into a silent "proceed".
        """
        ...


class TerminalUIStepGate:
    """`StepGate` backed by the Rich console and the live `TerminalUI`.

    Pauses the live display around the blocking prompt for the same reason
    `TerminalUIConfirmer` does — a Rich `Live` and a `Prompt.ask` cannot share the
    terminal — and resumes it in a `finally` so the display is handed back even when the
    prompt raises.
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
        self._logger = logger if logger is not None else logging.getLogger("pcswitcher.step_gate")

    async def confirm_action(self, *, job: str, host: Host, description: str, command: str) -> None:
        extra = {"job": job, "host": host.value}

        # Body is assembled as a `Text`, never a markup string: `command` is arbitrary
        # content (package names, file paths, snippet bodies) and a stray `[` in it would
        # otherwise be parsed as Rich markup and raise mid-prompt.
        body = Text()
        body.append(f"{job} → {host.value}\n", style="bold")
        body.append(f"{description}\n\n", style="dim")
        body.append(command, style="bold")

        self._ui.pause()
        try:
            self._console.print()
            self._console.print(Panel(body, title="Confirm modification", border_style="yellow"))
            self._console.print()
            # No `default=`: the user must type a choice. An accidental Enter re-prompts
            # rather than picking either outcome for them.
            response = Prompt.ask(
                f"[bold]Run this?[/bold] [{_PROCEED}] proceed  [{_ABORT}] abort sync",
                choices=[_PROCEED, _ABORT],
                show_choices=False,
            )
        except (EOFError, KeyboardInterrupt) as exc:
            # Unanswerable is not approval: the same rule the review applies to Ctrl-C.
            self._logger.warning("Confirmation prompt interrupted; aborting sync", extra=extra)
            raise SyncAbortedByUser(f"{job}: confirmation prompt interrupted before {description}") from exc
        finally:
            self._ui.resume()

        if response == _ABORT:
            self._logger.warning("Aborted by user at: %s", description, extra=extra)
            raise SyncAbortedByUser(f"{job}: aborted by user before {description} on {host.value}")
        self._logger.debug("Confirmed: %s", description, extra=extra)
