"""Per-action confirmation gate behind `--confirm-each-command`.

Generic infrastructure, not one job family's: it lives beside `executor.py`, which is its
only caller, and gates every write any job makes. A package sync is simply where the need
is sharpest — such a run converges dozens of small mutating commands — `apt-get install`,
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

Two outcomes only, decided by a single keypress with no default and no Enter: proceed, or
abort the whole sync. There is deliberately no "skip this command" — a single reviewed item can span
several commands (apt source stage-then-promote, snap install-then-switch-channel), so
skipping one of them leaves that item half-applied, which is a worse state than either
finishing it or stopping.

What is gated is everything that is not purely read-only, which is wider than "changes a
file": a `flock` that seizes the target's sync lock, a background process started and left
running — each changes the machine while writing nothing, and each is something a user
stepping through a run must be able to refuse. A call is ungated only when it can change no
state at all. Elevation is not a change: `sudo <read-only command>` stays a read.

Generic on purpose, and invoked from exactly one place: `pcswitcher.executor`. Any caller
— job, orchestrator, helper — reaches this gate by passing `mutates="..."` to the executor
method it already uses, so there is no second API to remember and no per-job wiring. The
executor supplies `job` (from its `active_job` context variable) and `host` (from which
executor it is). Everything that is not a pure read passes `mutates` except `folder_sync`'s
rsync pass (#209); `tests/unit/test_mutates_audit.py` holds that line, and names the few
reads whose incidental side effects are deliberately tolerated.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from pcswitcher.confirmer import PausableUI
from pcswitcher.models import Host, SyncAborted, SyncAbortedByUser
from pcswitcher.terminal import read_single_key

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
            SyncAbortedByUser: The user pressed the abort key.
            SyncAborted: The prompt could not be answered (EOF / Ctrl-C), so nobody
                decided anything. Never swallowed into a silent "proceed".
        """
        ...


class TerminalUIStepGate:
    """`StepGate` backed by the Rich console and the live `TerminalUI`.

    Pauses the live display around the blocking prompt for the same reason
    `TerminalUIConfirmer` does — a Rich `Live` and a `Prompt.ask` cannot share the
    terminal — and resumes it in a `finally` so the display is handed back even when the
    prompt raises.

    Both machine names are required, not defaulted (`PKG-FR-NAME-THE-MACHINES`): this
    prompt is a question the user answers, so the machine about to be changed is named
    here by hostname, once, in the heading. That is why a `mutates=` phrase does not have
    to repeat it — the phrase says what the change does, the heading says where.
    """

    def __init__(
        self,
        console: Console,
        ui: PausableUI,
        *,
        source_hostname: str,
        target_hostname: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._console = console
        self._ui = ui
        self._hostnames = {Host.SOURCE: source_hostname, Host.TARGET: target_hostname}
        self._logger = logger if logger is not None else logging.getLogger("pcswitcher.step_gate")

    async def confirm_action(self, *, job: str, host: Host, description: str, command: str) -> None:
        extra = {"job": job, "host": host.value}
        hostname = self._hostnames[host]

        # Body is assembled as a `Text`, never a markup string: `command` is arbitrary
        # content (package names, file paths, snippet bodies) and a stray `[` in it would
        # otherwise be parsed as Rich markup and raise mid-prompt.
        body = Text()
        body.append(f"{job} → {hostname}\n", style="bold")
        body.append(f"{description}\n\n", style="dim")
        body.append(command, style="bold")

        self._ui.pause()
        try:
            self._console.print()
            self._console.print(Panel(body, title="Confirm modification", border_style="yellow"))
            self._console.print()
            # One keypress decides, with no Enter to follow it (#241) — a run under this
            # flag answers this question dozens of times, and the second key is friction on
            # every one of them. No default and no other accepted key: an accidental Enter
            # is discarded and the read continues rather than picking an outcome.
            #
            # `<p>`, not `[p]`: Rich reads square brackets as markup and silently swallows
            # `[p]`/`[a]`, leaving the legend as bare words with no keys. Angle brackets are
            # also what the package-review screens use, so both prompts read alike.
            self._console.print(
                f"[bold]Run this?[/bold] <{_PROCEED}> proceed  <{_ABORT}> abort sync ",
                end="",
            )
            response = read_single_key([_PROCEED, _ABORT])
            # Nothing echoes in cbreak mode, so the answer is printed back: the user must
            # see which key registered, especially the one that aborts the run.
            self._console.print(response)
        except (EOFError, KeyboardInterrupt) as exc:
            self._console.print()  # close the unanswered prompt line
            # Unanswerable is not approval: the same rule the review applies to Ctrl-C.
            # Plain `SyncAborted` — an EOF means nobody was there to answer at all, so
            # this end of the run is not one to put on the user (#224).
            self._logger.warning("Confirmation prompt interrupted; aborting sync", extra=extra)
            raise SyncAborted(f"{job}: confirmation prompt interrupted before {description}") from exc
        finally:
            self._ui.resume()

        if response == _ABORT:
            self._logger.warning("Aborted by user at: %s", description, extra=extra)
            raise SyncAbortedByUser(f"{job}: aborted by user before {description} on {hostname}")
        self._logger.debug("Confirmed: %s", description, extra=extra)
