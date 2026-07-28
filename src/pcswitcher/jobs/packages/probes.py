"""The fail-fast boundary for package-manager READS (ADR-022).

Free functions, not a base class: ADR-020 D-15/D-16 (carried forward by ADR-021) keeps the
four package jobs independent, and a guard every job needs is exactly the kind of thing a
shared base class would smuggle a manager's assumptions into. `apt_policy.py` is the
precedent.

The distinction this module exists to hold is the one in ADR-022: a tool that ANSWERED —
including answering "nothing" — produced data, and data is this project's business to
handle. A tool that did not answer produced nothing, and reading its silence as data is how
an empty manifest becomes "that machine has nothing" and every item on the other machine
becomes a removal proposal. That second case fails the job, once, naming the command.

What counts as "did not answer" is per command and is NOT inferable from the exit code
alone — measured, in a stock `ubuntu:24.04` container unless noted:

* `apt-mark showmanual`/`showhold`, `apt-cache policy`, `flatpak list`/`remotes`/`mask`,
  `snap list --all`: a real failure exits non-zero (apt 100, flatpak 1, snap 1), and an
  empty answer exits 0. Their exit code IS the discriminator.
* `snap list --all` with zero snaps installed exits 0, prints the "No snaps are installed
  yet." hint on stderr, and leaves stdout empty (measured against the real `snap` binary
  driven by a stub snapd socket answering `/v2/snaps` with an empty result). Empty stdout
  is a legitimate answer, never a failure.
* `apt-cache policy` exits 0 for a name it has never heard of, so a non-zero exit is never
  a statement about a package — which is why apt additionally passes `answers`.
* `dpkg --search` over paths that are deliberately expected to be unowned, and
  `sha256sum <glob>` over a glob that legitimately matches nothing, BOTH exit non-zero in
  their normal case. Those callers must not use this guard; see ADR-022.
"""

from __future__ import annotations

from pcswitcher.models import CommandResult, Host

__all__ = ["ProbeFailed", "require_answer"]


class ProbeFailed(RuntimeError):
    """A package-manager READ this run's correctness depends on did not answer at all.

    Deliberately NOT a `ConvergeItemFailed`. That type means "what we asked for is wrong",
    which is under our control, belongs to one item, and lets the run continue (ADR-021
    D-27). This one means the tool or the machine is broken — a transient network failure,
    a package-manager lock, an interrupted dpkg, a daemon that is not running — which no
    item's own state explains. It escapes the per-item loops on purpose, so the run fails
    ONCE naming the command that failed rather than N times naming N items.

    One type across all four package jobs, because the caller that must react to it is the
    orchestrator, which has no reason to care which manager's read went dark.
    """


def require_answer(
    command: str,
    result: CommandResult,
    host: Host,
    *,
    answers: int | None = None,
    answer_noun: str = "answer",
) -> None:
    """Refuse to read a probe's silence as an answer about the things it was asked about.

    Raises `ProbeFailed` on a NON-ZERO EXIT, and — only when the caller passes `answers` —
    on `answers == 0`. `answers` is the number of parsed answers the caller KNOWS was owed;
    pass it only where at least one answer is genuinely guaranteed, never as a blanket
    "output was empty" check, because for most of these commands an empty answer is a
    legitimate state of the machine.

    The `answers` half is a JUDGEMENT, recorded as one: a probe that answered "I know none
    of these" and a probe that died both print nothing, and the output alone cannot
    separate them. It is resolved toward failing fast because the alternative misattributes
    an environment failure to every item, and because a set the tool knows nothing about is
    a set nothing could be done with anyway. A per-name absence INSIDE an answered probe is
    left alone: that is the tool saying it does not know that one name, which is evidence
    about that one request.
    """
    if result.success and answers != 0:
        return
    condition = (
        f"exited {result.exit_code}"
        if not result.success
        else f"exited 0 but printed no {answer_noun}, so its output is not an answer"
    )
    detail = f": {result.stderr.strip()}" if result.stderr.strip() else ""
    raise ProbeFailed(f"probe on the {host.value} did not answer — `{command}` {condition}{detail}")
