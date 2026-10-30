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
from pcswitcher.models import CommandResult

# Binaries this job runs under sudo, quoted back to the user when the passwordless-sudo
# check fails. A lower bound on what must be permitted, not an exact scope (ADR-013).
# The source's package state is never changed, so it needs just the /etc/apt digest capture
# and the conflict-review `cat` of a file the two machines disagree about — plus `systemctl`
# and `systemd-run`, which BOTH machines need: the orchestrator pauses each host's own apt
# update timers for the sync window and schedules the transient unit that restarts them —
# and `apt-get`, for the throwaway repository check (`REPOSITORY_CHECK_CMD`).
SOURCE_SUDO_COMMANDS = (
    "/usr/bin/apt-get",
    "/usr/bin/find",
    "/usr/bin/sha256sum",
    "/usr/bin/test",
    "/usr/bin/cat",
    "/usr/bin/systemctl",
    "/usr/bin/systemd-run",
)
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
    "/usr/bin/systemctl",
    "/usr/bin/systemd-run",
)

# Whether apt can fetch and verify every repository a machine has configured, asked of apt
# itself: a full `apt-get update` into a throwaway lists and cache directory, so the
# machine's own package lists, its package cache and its update hooks are all left alone —
# which is what lets validation, and a dry run, issue it. The machine's own `apt-get update`
# cannot answer this: with a cached index it only warns and keeps the stale copy, which is
# how a repository whose vendor rotated its signing key goes unnoticed on the source until
# the sync copies it to a machine with no cached index, where the same failure is an error
# that rolls back every repository write and refuses every install.
#
# The `#clear` lines can only be given in a config file (`-o` cannot clear a list), so one
# is written into the throwaway directory. `APT::Sandbox::User=root`: apt otherwise drops to
# `_apt` for downloads, which cannot enter a directory `mktemp` made private. The directory
# and everything in it are owned by the connecting user, so the final `rm` needs no sudo.
# One line: the command is echoed verbatim into the trace and the confirm gate.
REPOSITORY_CHECK_CMD = (
    "check_dir=$(mktemp --directory) "
    '&& mkdir --parents "$check_dir/lists/partial" "$check_dir/cache/archives/partial" '
    "&& printf '%s\\n' '#clear APT::Update::Pre-Invoke;' '#clear APT::Update::Post-Invoke;' "
    "'#clear APT::Update::Post-Invoke-Success;' > \"$check_dir/no-hooks.conf\" "
    '&& sudo apt-get update --config-file="$check_dir/no-hooks.conf" -o Dir::State::Lists="$check_dir/lists" '
    '-o Dir::Cache="$check_dir/cache" -o APT::Sandbox::User=root; '
    'status=$?; rm --recursive --force -- "$check_dir"; exit $status'
)


def repository_check_problems(result: CommandResult) -> list[str]:
    """apt's own `W:`/`E:` lines from a failed `REPOSITORY_CHECK_CMD`, or every stderr
    line when it printed none (the check could not run at all)."""
    lines = [line.strip() for line in f"{result.stdout}\n{result.stderr}".splitlines()]
    flagged = [line for line in lines if line.startswith(("W:", "E:"))]
    return flagged or [line for line in result.stderr.splitlines() if line.strip()]


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


def require_apt_answer(command: str, result: CommandResult, machine: str, *, blocks: int | None = None) -> None:
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
    require_answer(command, result, machine, answers=blocks, answer_noun="package block")


def install_args(names: Sequence[str]) -> str:
    """The apt-get arguments for installing `names`, shared by the plan-time rehearsal, the
    apply-time rehearsal and the real command, so no pair of them can drift apart and
    rehearse a transaction other than the one that runs.

    `--allow-change-held-packages` appears nowhere, rehearsal included. It would let apt move
    OTHER held packages, and apt refusing to is the only thing protecting a held package apt
    installed automatically. The one case that needed it — a name the target held without
    having it installed, which apt refuses the whole batch over — cannot reach a rehearsal
    any more: `PKG-FR-HOLD-WITHOUT-PACKAGE` ends the run over such a hold while planning.

    `--allow-downgrades` is what lets the rehearsal see a downgrade at all: under
    `--assume-yes` without it, apt aborts any transaction containing one with `E: Packages
    were downgraded and -y was used without --allow-downgrades`, before printing a single
    `Inst` line — so collateral classification never sees the downgrade, and the item fails
    on apt's error instead of being asked about (`PKG-FR-COLLATERAL-MANUAL`). Measured on a
    target switching Ubuntu's snap-transitional `firefox` (`1:1snap1-0ubuntu5`) to Mozilla's
    pinned `159.0~build1`: the epoch makes apt read the switch as a downgrade. The real
    command needs it too, or every downgrade the rehearsal let through fails there; nothing
    is downgraded unguarded, because the rehearsal of these same arguments is what decides.
    """
    quoted = " ".join(shlex.quote(name) for name in names)
    return f"install --assume-yes --allow-downgrades --no-install-recommends {quoted}"


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


# One dependency in apt's "The following packages have unmet dependencies:" block: either the
# first line ` <pkg> : Depends: <dep> but it is not installable` or a continuation line
# `          Depends: <dep> (>= 1) but 2 is to be installed`. Only the first alternative of a
# `a | b` dependency is captured, and the `:any`/`:amd64` qualifier is dropped.
_UNMET_DEPENDENCY_RE = re.compile(r"^\s*(?:\S+ : )?(?:Pre-?)?Depends: (?P<dep>[^\s:(|]+)")


class AptRehearsalFailed(ConvergeItemFailed):
    """`apt-get --dry-run` refused the transaction. `unmet_dependencies` are the packages apt
    named as the dependencies it could not satisfy, so the caller can say WHY when one of them
    is an item this run itself refused — apt's own wording ("you have held broken packages")
    names neither the dependency nor a reason, and nothing need be held at all.
    """

    def __init__(self, message: str, unmet_dependencies: tuple[str, ...]) -> None:
        super().__init__(message)
        self.unmet_dependencies = unmet_dependencies


def unmet_dependencies(stdout: str) -> tuple[str, ...]:
    """The dependency names in a failed rehearsal's unmet-dependencies block, in apt's order."""
    found: dict[str, None] = {}
    for line in stdout.splitlines():
        match = _UNMET_DEPENDENCY_RE.match(line)
        if match is not None:
            found[match.group("dep")] = None
    return tuple(found)


async def simulate_apt_transaction(
    executor: RemoteExecutor, apt_args: str, *, login_shell: bool | None = False
) -> AptTransactionPreview:
    """Run `apt-get --dry-run <apt_args>` on `executor` and parse its Inst/Remv action lines.

    No `sudo` is needed: simulation is read-only. Raises `AptRehearsalFailed` (a
    `ConvergeItemFailed`) if the simulation itself fails: a failed `apt-get --dry-run` typically prints no Inst/Remv
    lines, which would otherwise parse as an indistinguishable-from-clean empty preview and
    let both call sites proceed with a real command whose simulation was never actually
    trustworthy (WR-01) — refuse rather than silently degrade.

    `ConvergeItemFailed` and not `ProbeFailed`, which is the deliberate boundary of ADR-022
    `PKG-FR-MANAGER-CONVERGES`: apt gives this command ONE failure code for both categories. Measured in a stock
    `ubuntu:24.04`, a name apt cannot locate exits 100 with `E: Unable to locate package`,
    which is byte-for-byte the exit code a held dpkg lock produces, and no rewrite of the
    command's SYNTAX separates them — apt offers no second code and no second mode.

    Apply time simulates one approved install or removal, where apt's refusal is a fact
    about that request (`PKG-FR-OUTCOME-FAILED`). Plan time removes the ambiguity from its ARGUMENTS
    instead: it rehearses only names the target's `apt-cache policy` gave a candidate for
    (`origins.OriginClassifier.target_resolvable`), so "unable to locate" is not among the
    failures it can meet. What remains there — a lock, a broken apt, an unresolvable
    candidate set — has no per-item loop to be reported against, so it aborts the plan, which
    is correct: a rehearsal that did not happen must not read as a clean one.
    """
    result = await executor.run_command(f"apt-get --dry-run {apt_args}", login_shell=login_shell)
    if not result.success:
        raise AptRehearsalFailed(
            f"apt-get --dry-run {apt_args} failed: {result.stderr.strip()}", unmet_dependencies(result.stdout)
        )
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
