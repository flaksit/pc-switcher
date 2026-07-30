"""How this job talks to apt: the argument strings it builds, the output it parses, and
what "apt did not answer" means for each.

The arguments are built here and nowhere else so the plan-time rehearsal, the apply-time
rehearsal and the real command cannot drift apart and rehearse a transaction other than the
one that runs. The parsers are here for the same reason the arguments are: a shape apt
prints is one fact, and two readers of it would disagree eventually.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pcswitcher.executor import Executor, RemoteExecutor
from pcswitcher.jobs.packages.probes import require_answer
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed
from pcswitcher.models import CommandResult, Host

# Binaries this job runs under sudo, quoted back to the user when the passwordless-sudo
# check fails. A lower bound on what must be permitted, not an exact scope (ADR-013).
# The source is only ever read, so it needs just the /etc/apt digest capture and the
# conflict-review `cat` of a file the two machines disagree about.
SOURCE_SUDO_COMMANDS = ("/usr/bin/find", "/usr/bin/sha256sum", "/usr/bin/test", "/usr/bin/cat")
TARGET_SUDO_COMMANDS = (
    "/usr/bin/apt-get",
    "/usr/bin/apt-mark",
    "/usr/bin/find",
    "/usr/bin/sha256sum",
    "/usr/bin/test",
    "/usr/bin/cat",
    "/usr/bin/install",
    "/usr/bin/cp",
    "/usr/bin/rm",
    "/usr/bin/fuser",
)

# Matches one `apt-get --dry-run` transaction line: `Inst <name> [<old>] (<new> ...)` for an
# install/upgrade (the `[<old>]` bracket only appears when a version is already
# installed), or `Remv <name> [<old>]` for a removal. Parsed by leading verb token and
# named groups rather than fixed column positions — the rest of an apt-get --dry-run line's
# shape varies with the package and its dependency resolution.
_TRANSACTION_LINE_RE = re.compile(
    r"^(?P<verb>Inst|Remv)\s+(?P<name>\S+)"
    r"(?:\s+\[(?P<old_version>[^\]]+)\])?"
    r"(?:\s+\((?P<new_version>[^\s)]+)\)?)?"
)


def lines(output: str) -> list[str]:
    """Non-blank, stripped lines — the shape every `apt-mark`/`find` list command in
    this job produces."""
    return [line.strip() for line in output.splitlines() if line.strip()]


def require_apt_answer(command: str, result: CommandResult, host: Host, *, blocks: int | None = None) -> None:
    """`require_answer` with apt's own evidence for what "did not answer" means (ADR-022).

    Two conditions, and no others, mean apt did not answer:

    * a NON-ZERO EXIT. Measured in a stock `ubuntu:24.04`: `apt-cache policy` exits 0 for a
      name it has never heard of, and 100 when it cannot read the sources at all. So a
      non-zero exit is never a statement about a package.
    * `blocks == 0` where the caller knows at least one block was owed. apt prints exactly
      one block per name it knows (measured on the development machine: 152 blocks for a
      152-name `apt-mark showmanual` set), so no blocks at all over names apt must know
      means the output is not apt's answer. Callers therefore pass `blocks` only where a
      block is genuinely owed: a name installed on the machine being asked, or one this run
      has already established the machine has a candidate for. A caller asking about names
      the machine may legitimately never have heard of passes nothing.
    """
    require_answer(command, result, host, answers=blocks, answer_noun="package block")


def install_args(names: Sequence[str]) -> str:
    """The apt-get arguments for installing `names`, shared by the plan-time rehearsal, the
    apply-time rehearsal and the real command, so no pair of them can drift apart and
    rehearse a transaction other than the one that runs."""
    return f"install --assume-yes --no-install-recommends {' '.join(shlex.quote(name) for name in names)}"


def remove_args(names: Sequence[str]) -> str:
    """`install_args`' counterpart for the removal direction."""
    return f"remove --assume-yes {' '.join(shlex.quote(name) for name in names)}"


def candidate_version(policy_output: str, name: str) -> str | None:
    """The version `apt-cache policy` says it would install for `name`, or `None` when the
    output holds no block for it or apt answers `(none)`.

    Only the version string, deliberately: the origin rows beside it are what
    `packages/apt_policy.py` parses, and the one place a version is needed is the refusal
    naming both versions of a held package (`PKG-FR-APT-HOLD-VERSION`). `None` covers "apt
    printed nothing about this name" and "apt will install no version of it" alike, because
    the refusal says the same thing for both.
    """
    in_block = False
    for line in policy_output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            in_block = line[:-1] == name
            continue
        stripped = line.strip()
        if in_block and stripped.startswith("Candidate:"):
            value = stripped.removeprefix("Candidate:").strip()
            return None if value == "(none)" else value
    return None


def policy_command(names: Sequence[str]) -> str:
    """One batched `apt-cache policy` over `names` — never one call per package, and never
    one call per question the output answers."""
    return f"apt-cache policy {' '.join(shlex.quote(name) for name in names)}"


async def compare_deb_versions(executor: Executor, left: str, right: str) -> int:
    """Compare two Debian package version strings, `sorted`-comparator convention.

    Returns negative when `left` < `right`, zero when equal, positive when `left` >
    `right`. Not hand-rolled: Debian version ordering has epoch, tilde and revision
    tie-breaking rules that are neither lexicographic nor PEP 440 — only dpkg's own
    comparator correctly ranks an epoch-bearing version like `2:1.0` above `10.0`
    (RESEARCH Don't Hand-Roll). Shells out through `executor` (never assumes a local
    `dpkg`, since the target's version may need comparing against its own dpkg) with
    `shlex.quote` on both operands (ASVS V5, T-02-01). Short-circuits to equal for
    byte-identical strings so the common "nothing changed" case costs no subprocess.
    """
    if left == right:
        return 0

    quoted_left = shlex.quote(left)
    quoted_right = shlex.quote(right)

    lt_result = await executor.run_command(f"dpkg --compare-versions {quoted_left} lt {quoted_right}")
    if lt_result.success:
        return -1

    gt_result = await executor.run_command(f"dpkg --compare-versions {quoted_left} gt {quoted_right}")
    if gt_result.success:
        return 1

    return 0


@dataclass(frozen=True)
class AptTransactionPreview:
    """The parsed result of `apt-get --dry-run <args>` — what apt says it WOULD do.

    `apt-get --dry-run` is the only honest answer to "what will this command do": apt resolves
    dependencies and conflicts at run time, so the package the user ticked and the
    transaction apt actually runs are not necessarily the same thing.

    `install_versions` maps a package apt would `Inst` to `(currently_installed_version
    | None, candidate_version)` — the currently-installed version is `None` for a fresh
    install (no `[...]` bracket in the line), present for an upgrade/downgrade. This is
    what the downgrade guard compares via `compare_deb_versions` rather than assuming
    every `Inst` line is a new install.
    """

    installs: tuple[str, ...]
    removals: tuple[str, ...]
    raw: str
    install_versions: Mapping[str, tuple[str | None, str]] = field(default_factory=dict)


async def simulate_apt_transaction(
    executor: RemoteExecutor, apt_args: str, *, login_shell: bool | None = False
) -> AptTransactionPreview:
    """Run `apt-get --dry-run <apt_args>` on `executor` and parse its Inst/Remv action lines.

    No `sudo` is needed: simulation is read-only. Raises `ConvergeItemFailed` if the
    simulation itself fails: a failed `apt-get --dry-run` typically prints no Inst/Remv
    lines, which would otherwise parse as an indistinguishable-from-clean empty preview and
    let both call sites proceed with a real command whose simulation was never actually
    trustworthy (WR-01) — refuse rather than silently degrade.

    `ConvergeItemFailed` and not `ProbeFailed`, which is the deliberate boundary of ADR-022
    D-01: apt gives this command ONE failure code for both categories. Measured in a stock
    `ubuntu:24.04`, a name apt cannot locate exits 100 with `E: Unable to locate package`,
    which is byte-for-byte the exit code a held dpkg lock produces, and no rewrite of the
    command's SYNTAX separates them — apt offers no second code and no second mode.

    Apply time simulates one approved install or removal, where apt's refusal is a fact
    about that request (ADR-020 D-27). Plan time removes the ambiguity from its ARGUMENTS
    instead: it rehearses only names the target's `apt-cache policy` gave a candidate for
    (`origins.OriginClassifier.target_resolvable`), so "unable to locate" is not among the
    failures it can meet. What remains there — a lock, a broken apt, an unresolvable
    candidate set — has no per-item loop to be reported against, so it aborts the plan, which
    is correct: a rehearsal that did not happen must not read as a clean one.
    """
    result = await executor.run_command(f"apt-get --dry-run {apt_args}", login_shell=login_shell)
    if not result.success:
        raise ConvergeItemFailed(f"apt-get --dry-run {apt_args} failed: {result.stderr.strip()}")
    installs: list[str] = []
    removals: list[str] = []
    install_versions: dict[str, tuple[str | None, str]] = {}
    for line in result.stdout.splitlines():
        match = _TRANSACTION_LINE_RE.match(line)
        if match is None:
            continue
        verb, name = match.group("verb"), match.group("name")
        if verb == "Inst":
            installs.append(name)
            new_version = match.group("new_version")
            if new_version is not None:
                install_versions[name] = (match.group("old_version"), new_version)
        elif verb == "Remv":
            removals.append(name)
    return AptTransactionPreview(
        installs=tuple(installs), removals=tuple(removals), raw=result.stdout, install_versions=install_versions
    )
