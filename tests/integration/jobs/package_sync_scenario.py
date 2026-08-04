"""The package_sync scenario driven by `test_package_sync.py`: subjects, seeding, captures, assertions.

Every helper the package-sync integration tests need against real VMs lives here: the pure
parsers over apt/snap/flatpak output, the convergers that put a machine into the state a
scenario needs, the constructions that BUILD a subject a stock Ubuntu 24.04 pair does not
offer (a vendor repository, a cascading pair, a sideloaded snap), and the whole-machine
state captures a "this run changed nothing" claim compares.

No test classes and no markers: this is a library, imported by the test modules that own the
claims. The apt subjects it selects are handed out by the `apt_subjects` fixture in
`tests/integration/conftest.py`.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.jobs.apt_sync.items import AptPackageItem, collateral_item_id
from pcswitcher.jobs.flatpak_sync import FlatpakItem
from pcswitcher.jobs.manual_installs_sync import UnreproducibleItem
from pcswitcher.jobs.packages.review import PACKAGE_REVIEW_AUTOMATION_ENV, Decision
from pcswitcher.jobs.packages.state import (
    DECISION_FILE_RELPATH_TEMPLATE,
    SNIPPET_REGISTRY_RELPATH,
    DecisionFile,
    Snippet,
    SnippetRegistry,
)
from pcswitcher.models import CommandResult
from tests.integration.conftest import write_pcswitcher_config

# Prefix marking each candidate's reverse-dependency block in the batched pc2 probe below.
RDEPENDS_MARKER = "@@RDEPENDS_FOR@@"


# How many shared packages to probe for reverse dependencies when looking for one safe
# to remove, per round and in total. Each probe is a separate `apt-cache rdepends` process
# on the target reloading the apt cache, so the cost is linear and the whole probe runs
# under a single command timeout: the total bounds the search inside that budget, and
# probing a ROUND at a time means a search that succeeds immediately — which is every
# search here so far — pays for one round instead of all of them (measured in a stock
# `ubuntu:24.04`: 8.2s for 12 probes against 26.7s for 40).
RDEPENDS_PROBE_ROUND = 12
RDEPENDS_PROBE_LIMIT = 48

# How many candidates beyond the requested count to rehearse, so apt refusing a few still
# leaves enough. Every one costs an `apt-get --dry-run remove` on the target, and no test
# asks for more than three subjects.
REMOVAL_REHEARSAL_HEADROOM = 4


def nonblank_lines(text: str) -> list[str]:
    """Split command output into stripped, non-empty lines."""
    return [line.strip() for line in text.splitlines() if line.strip()]


async def cleanup_in_parallel(*chains: Awaitable[None]) -> None:
    """Run one cleanup chain per machine at the same time (#216).

    Concurrency is safe because `BashLoginRemoteExecutor.run_command` opens its own asyncssh
    channel per command and this module takes no lock, so two machines' cleanups genuinely
    overlap; the ORDER inside one machine's chain is what each caller still owns.

    `return_exceptions=True` is what keeps one chain's failure from cancelling the other
    machine's, and what stops a cleanup failure from replacing the test's own error -- it is
    printed instead, like every other tolerated cleanup failure here.
    """
    for outcome in await asyncio.gather(*chains, return_exceptions=True):
        if isinstance(outcome, BaseException):
            print(f"[cleanup] {outcome!r}")


async def finish_both[T, U](first: Awaitable[T], second: Awaitable[U]) -> tuple[T, U]:
    """Run two things at once, let BOTH finish, then raise the first failure.

    For the gathers whose result a `finally` needs. A plain `asyncio.gather` raises the moment
    one side does and leaves the other RUNNING, unawaited: a test can reach its cleanup while a
    command is still writing to a machine, and it never gets the tuple, so a handle the other
    side already produced -- the repository to undeclare, the pair to take back -- is lost and
    what it names stays on the machine.

    Nothing is cancelled: an apt or snapd transaction stopped halfway leaves a machine worse
    off than one that ran to the end, and the rest of the run has to live on that machine.

    A caller whose cleanup needs a value from one side must still record it as it arrives
    (`nonlocal`): a raise here means there is no tuple to unpack.
    """
    left, right = await asyncio.gather(first, second, return_exceptions=True)
    if isinstance(left, BaseException):
        raise left
    if isinstance(right, BaseException):
        raise right
    return left, right


# Every escape sequence `logger.RichFormatter` can emit around a log line's styled fields.
# Stripped before any assertion reads the run's own output: the formatter always renders
# through a `force_terminal=True` console, so the text is coloured even when stdout is a pipe.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def collapse_run_output(text: str) -> str:
    """A sync's combined stdout+stderr as one ANSI-free, single-spaced line.

    Both renderers that carry a package job's own words wrap them: `RichFormatter` folds a
    long log record at its console width, and a review group arrives inside a Rich `Panel`.
    A phrase that has to be matched whole therefore has to be matched after the line breaks
    and the padding are gone. Single TOKENS (a package name, a ref, a URL) need none of
    this and are asserted against the raw output in the tests themselves — Rich never
    breaks a word that fits the line.

    Panel BORDER characters are deliberately left in place: they mark the wrap points
    inside a panel, so a phrase that spans one still fails to match here rather than
    matching a rendering nobody has seen.
    """
    return " ".join(ANSI_ESCAPE_RE.sub("", text).split())


def parse_dpkg_installed(dpkg_query_output: str) -> set[str]:
    """Parse `dpkg-query --show --showformat='${Package}\\t${Status}\\n'` into fully-installed package names.

    Only `install ok installed` counts as installed -- excludes packages merely known to
    dpkg (config-remaining after removal, half-installed, etc.).
    """
    installed: set[str] = set()
    for line in dpkg_query_output.splitlines():
        if not line.strip():
            continue
        name, _, status = line.partition("\t")
        if status.strip() == "install ok installed":
            installed.add(name)
    return installed


def parse_reverse_depends(rdepends_block: str) -> set[str]:
    """Parse one `apt-cache rdepends --installed <pkg>` block into its reverse-dep names.

    Output shape: the package's own name on the first line, a `Reverse Depends:` header,
    then one indented name per line; only names after the header count.
    """
    names: set[str] = set()
    seen_header = False
    for line in rdepends_block.splitlines():
        if line.strip() == "Reverse Depends:":
            seen_header = True
            continue
        if not seen_header:
            continue
        stripped = line.strip()
        if stripped:
            names.add(stripped.split()[0])
    return names


def parse_batched_rdepends(batched_output: str) -> dict[str, set[str]]:
    """Split a `for p in ...; do echo MARKER$p; apt-cache rdepends --installed "$p"; done`
    run into `{package: reverse_dep_names}` -- one SSH round-trip for every candidate
    instead of one per candidate (testing-guide.md's command-grouping rule).
    """
    result: dict[str, set[str]] = {}
    current: str | None = None
    block: list[str] = []
    for line in batched_output.splitlines():
        if line.startswith(RDEPENDS_MARKER):
            if current is not None:
                result[current] = parse_reverse_depends("\n".join(block))
            current = line.removeprefix(RDEPENDS_MARKER)
            block = []
        else:
            block.append(line)
    if current is not None:
        result[current] = parse_reverse_depends("\n".join(block))
    return result


def pick_safe_removal_candidates(
    pc1_manual: list[str],
    pc2_installed: set[str],
    pc2_manual: set[str],
    reverse_deps_by_candidate: dict[str, set[str]],
    count: int = 1,
) -> list[str]:
    """Pick up to `count` packages (alphabetically, for determinism) that are manually
    installed on pc1, present on pc2, and whose installed reverse dependencies on pc2
    include no manually-installed package there (T-02-28's safety check before removing
    anything from a real VM). Returns fewer than `count` entries -- possibly none -- when
    not enough candidates satisfy all three conditions.
    """
    picked: list[str] = []
    for name in sorted(set(pc1_manual) & pc2_installed):
        if not (reverse_deps_by_candidate.get(name, set()) & pc2_manual):
            picked.append(name)
            if len(picked) == count:
                break
    return picked


def pick_safe_removal_candidate(
    pc1_manual: list[str],
    pc2_installed: set[str],
    pc2_manual: set[str],
    reverse_deps_by_candidate: dict[str, set[str]],
) -> str | None:
    """Pick the first (alphabetically, for determinism) package that is manually installed
    on pc1, present on pc2, and whose installed reverse dependencies on pc2 include no
    manually-installed package there (T-02-28's safety check before removing anything from
    a real VM). Returns `None` when no candidate satisfies all three conditions.
    """
    picked = pick_safe_removal_candidates(pc1_manual, pc2_installed, pc2_manual, reverse_deps_by_candidate, count=1)
    return picked[0] if picked else None


def no_apt_candidate_message() -> str:
    """Why an apt subject could not be selected, for the assertion that fires if one
    ever cannot be: every Debian system has hundreds of manually-installed packages with
    no manually-installed reverse dependency, so an empty result means the machine is not
    what these tests assume, not that the test is inapplicable.
    """
    return (
        "No safe apt package candidate found: searched pc1's `apt-mark showmanual` "
        "intersected with pc2's installed set (`dpkg-query`), filtered to packages whose "
        "`apt-cache rdepends --installed` names no manually-installed package on pc2."
    )


async def apt_would_remove_these(executor: BashLoginRemoteExecutor, names: Sequence[str]) -> set[str]:
    """Of `names`, the ones `executor`'s own apt would actually carry out a removal for --
    one `apt-get --dry-run remove` each, batched into a single command
    (testing-guide.md's command-grouping rule).

    `pick_safe_removal_candidates` reads `apt-cache rdepends --installed` and rejects a
    candidate whose reverse dependencies include a MANUALLY-installed package. That misses
    the packages nothing marks manual and apt still refuses to let go: an essential one
    (`bash` was the case that failed here) is a reverse dependency like any other, so a
    candidate that takes one with it passes the rdepends check and then fails the real
    removal. Only apt can settle that, so it is asked.

    Individually safe implies safe together: a batch's removal closure is the union of the
    single ones, so nothing here needs to rehearse the combination.
    """
    if not names:
        return set()
    quoted = " ".join(shlex.quote(name) for name in names)
    result = await executor.run_command(
        f'for p in {quoted}; do echo "{RDEPENDS_MARKER}$p"; '
        'if apt-get --dry-run remove --assume-yes "$p" > /dev/null 2>&1; then echo SAFE; fi; done',
        login_shell=False,
        timeout=120.0,
    )
    safe: set[str] = set()
    current = ""
    for line in result.stdout.splitlines():
        if line.startswith(RDEPENDS_MARKER):
            current = line.removeprefix(RDEPENDS_MARKER)
        elif line.strip() == "SAFE" and current:
            safe.add(current)
    return safe


async def find_removable_candidates(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor, count: int = 1
) -> list[str]:
    """Query both VMs and pick up to `count` packages safe to remove from pc2 for a test
    (see `pick_safe_removal_candidates`, then `apt_would_remove_these`). Returns fewer than
    `count` -- possibly none -- when not enough candidates qualify.
    """
    # Three reads that write nothing and need nothing from each other, so they run at once.
    pc1_manual_result, pc2_manual_result, pc2_dpkg_result = await asyncio.gather(
        pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0),
        pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0),
        pc2_executor.run_command(
            "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
        ),
    )

    pc1_manual = nonblank_lines(pc1_manual_result.stdout)
    pc2_manual = set(nonblank_lines(pc2_manual_result.stdout))
    pc2_installed = parse_dpkg_installed(pc2_dpkg_result.stdout)

    initial_candidates = sorted(set(pc1_manual) & pc2_installed)
    if not initial_candidates:
        return []

    reverse_deps_by_candidate: dict[str, set[str]] = {}
    probed: set[str] = set()
    rehearsed: set[str] = set()
    confirmed: list[str] = []
    for start in range(0, min(len(initial_candidates), RDEPENDS_PROBE_LIMIT), RDEPENDS_PROBE_ROUND):
        this_round = initial_candidates[start : start + RDEPENDS_PROBE_ROUND]
        quoted = " ".join(shlex.quote(name) for name in this_round)
        rdepends_result = await pc2_executor.run_command(
            f'for p in {quoted}; do echo "{RDEPENDS_MARKER}$p"; apt-cache rdepends --installed "$p"; done',
            login_shell=False,
            timeout=120.0,
        )
        reverse_deps_by_candidate |= parse_batched_rdepends(rdepends_result.stdout)
        probed |= set(this_round)

        # `pick_safe_removal_candidates` walks the WHOLE intersection and reads an unprobed
        # name as having no reverse dependency at all, so only probed names may be kept.
        shortlist = [
            name
            for name in pick_safe_removal_candidates(
                pc1_manual, pc2_installed, pc2_manual, reverse_deps_by_candidate, len(probed)
            )
            if name in probed and name not in rehearsed
        ][: count - len(confirmed) + REMOVAL_REHEARSAL_HEADROOM]

        # apt's own verdict on each, because the rdepends check cannot see a candidate that
        # takes an essential package with it (`apt_would_remove_these`).
        removable = await apt_would_remove_these(pc2_executor, shortlist)
        rehearsed |= set(shortlist)
        confirmed += [name for name in shortlist if name in removable]
        if len(confirmed) >= count:
            break

    return confirmed[:count]


@dataclass(frozen=True)
class AptSubjects:
    """The apt packages the package-sync tests operate on, selected ONCE per module by the
    `apt_subjects` fixture (`tests/integration/conftest.py`).

    Pinned rather than rediscovered per test, for two reasons that both cost real wall
    clock (#216). Selecting one costs a round of `apt-cache rdepends` plus an
    `apt-get --dry-run remove` for each survivor, and six tests were each paying it. And a
    pinned name is what lets a test converge to its precondition instead of restoring
    afterwards: with a fresh selection each time, a package left removed simply drops out of
    the `apt-mark showmanual` intersection and the next test picks the NEXT one down the
    alphabet, so nothing is reused and the pool drains.

    Snap and flatpak subjects have always been pinned this way (`FIXTURE_SNAPS`,
    `FIXTURE_FLATPAK_APP`); apt's were discovered only because any Debian system offers
    hundreds, never because a test needed them to vary.
    """

    #: Packages a test may remove from the TARGET so the run has an install to converge.
    #: Three, because the one run proving a failing item does not stop the job needs three
    #: independent apt items.
    install_direction: tuple[str, str, str]
    #: A package a test may remove from the SOURCE, so the run has a removal to converge.
    #: Vetted against pc2 like the others: the two VMs come from one baseline, so a package
    #: safe to remove there is safe to remove here.
    removal_direction: str
    #: Installed at the same version on both machines and held on neither, so holding it on
    #: the source is a run's only apt work for it.
    hold: str


# Splits the two reads `ensure_installed_and_manual` issues as one command.
SUBJECT_STATE_MARKER = "@@PCSWITCHER_IT_SUBJECT@@"


async def subject_state(executor: BashLoginRemoteExecutor, name: str) -> tuple[bool, bool]:
    """`(fully installed, marked manual)` for `name` on `executor`'s machine, in one command.

    `apt-mark showmanual <name>` rather than the whole manual set: it answers about the one
    package, which is all a converger needs and a fraction of the cost.
    """
    quoted = shlex.quote(name)
    result = await executor.run_command(
        f"dpkg-query --show --showformat='${{Status}}' {quoted}; echo; echo {SUBJECT_STATE_MARKER}; "
        f"apt-mark showmanual {quoted}",
        login_shell=False,
        timeout=20.0,
    )
    status_block, _, manual_block = result.stdout.partition(SUBJECT_STATE_MARKER)
    return status_block.strip() == "install ok installed", name in nonblank_lines(manual_block)


async def ensure_absent(executor: BashLoginRemoteExecutor, name: str) -> None:
    """Make `name` absent from `executor`'s machine, doing nothing when it already is.

    The read is what makes a scenario that inherits the state it wanted pay nothing
    (measured on a test VM: the read is hundredths of a second against 6.5s for the
    removal).
    """
    installed, _manual = await subject_state(executor, name)
    if not installed:
        return
    result = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(name)}",
        login_shell=False,
        timeout=120.0,
    )
    assert result.success, f"Failed to remove {name}: {result.stderr}"


async def ensure_installed_and_manual(executor: BashLoginRemoteExecutor, name: str) -> None:
    """Make `name` installed and marked manual on `executor`'s machine, doing nothing when
    it already is (the counterpart of `ensure_absent`, 8.2s when it has to act).
    """
    installed, manual = await subject_state(executor, name)
    if installed and manual:
        return
    quoted = shlex.quote(name)
    result = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {quoted} && sudo apt-mark manual {quoted}",
        login_shell=False,
        timeout=120.0,
    )
    if not result.success:
        print(f"[converge] failed to restore {name}: {result.stderr}")


async def create_extra_on_target_apt_package(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
) -> str:
    """Give pc2 an apt package pc1 does not have, so a removal-direction
    (EXTRA_ON_TARGET) diff exists, and return its name.

    The two VMs are provisioned from one baseline, so their manual sets are identical
    and no such package exists until a test makes one. It is made by promoting one of
    pc2's automatically-installed packages to manual (`apt-mark manual`) rather than by
    installing or removing anything: `test_non_interactive_skip_all` must be able to
    assert pc2's installed set is untouched by the run, and a selection-state flip
    changes what apt_sync captures without changing what is on the disk. Reversed with
    `restore_auto_marked_package`.
    """
    # Three reads that write nothing and need nothing from each other, so they run at once.
    pc1_manual_result, pc2_manual_result, pc2_dpkg_result = await asyncio.gather(
        pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0),
        pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0),
        pc2_executor.run_command(
            "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
        ),
    )
    pc1_manual = set(nonblank_lines(pc1_manual_result.stdout))
    pc2_manual = set(nonblank_lines(pc2_manual_result.stdout))
    pc2_installed = parse_dpkg_installed(pc2_dpkg_result.stdout)

    candidates = sorted(pc2_installed - pc2_manual - pc1_manual)
    assert candidates, (
        "No automatically-installed package on pc2 is absent from pc1's `apt-mark "
        "showmanual`, so no removal-direction subject can be made. Every Debian system "
        "carries hundreds of auto-installed dependencies; an empty set means pc2 is not "
        "the machine these tests assume."
    )
    name = candidates[0]
    result = await pc2_executor.run_command(
        f"sudo apt-mark manual {shlex.quote(name)}", login_shell=False, timeout=30.0
    )
    assert result.success, f"Failed to mark {name} manual on pc2: {result.stderr}"
    return name


async def restore_auto_marked_package(executor: BashLoginRemoteExecutor, name: str) -> None:
    """Put a package promoted by `create_extra_on_target_apt_package` back to automatic."""
    result = await executor.run_command(f"sudo apt-mark auto {shlex.quote(name)}", login_shell=False, timeout=30.0)
    if not result.success:
        print(f"[cleanup] failed to mark {name} auto again on pc2: {result.stderr}")


def package_sync_test_config(*, extra_sections: str = "", **enabled_jobs: bool) -> str:
    """Minimal test config enabling exactly the given `sync_jobs` keys (e.g.
    `apt_sync=True, snap_sync=True`). `Configuration.sync_jobs` is iterated as-is from
    the YAML dict (config.py), with no schema-default injection, so a job name absent
    here is never instantiated -- no explicit `false` entries needed.

    `extra_sections` is appended verbatim: a job whose own config section is not optional
    (`folder_sync`, which needs its `folders` list) carries it there rather than forcing
    every caller to hand-write a whole config.
    """
    jobs_block = "\n".join(f"  {name}: true" for name, enabled in enabled_jobs.items() if enabled)
    return (
        "logging:\n"
        "  file: DEBUG\n"
        "  tui: DEBUG\n"
        "  external: DEBUG\n"
        "sync_jobs:\n"
        f"{jobs_block}\n"
        "disk_space_monitor:\n"
        '  preflight_minimum: "5%"\n'
        '  runtime_minimum: "3%"\n'
        '  warning_threshold: "10%"\n'
        "  check_interval: 5\n"
        "btrfs_snapshots:\n"
        "  subvolumes:\n"
        '    - "@"\n'
        '    - "@home"\n'
        "  keep_recent: 2\n"
        f"{extra_sections}"
    )


def folder_sync_section(*folder_paths: str) -> str:
    """A `folder_sync` config section mirroring exactly `folder_paths`, with no central
    filter file (the schema makes `filter_file` optional).

    Every path in ONE section: `folder_sync` is a mapping key, so two sections would make
    the config a YAML document with a duplicate key and the run would end before any job.
    """
    folders = "".join(f"    - path: {path}\n      enabled: true\n" for path in folder_paths)
    return f"folder_sync:\n  folders:\n{folders}"


async def write_package_sync_config(
    executor: BashLoginRemoteExecutor, *, extra_sections: str = "", **enabled_jobs: bool
) -> None:
    """Write a package-sync test config enabling exactly `enabled_jobs` to `executor`
    (always the machine acting as source for the sync under test).
    """
    await write_pcswitcher_config(executor, package_sync_test_config(extra_sections=extra_sections, **enabled_jobs))


async def write_apt_sync_config(executor: BashLoginRemoteExecutor) -> None:
    """Write the apt_sync-only test config to pc1 (source)."""
    await write_package_sync_config(executor, apt_sync=True)


async def decision_file_exists(executor: BashLoginRemoteExecutor, manager: str) -> bool:
    """Whether `manager`'s machine-local decision file currently exists on `executor`'s
    machine (D-09) -- used to prove a non-interactive run records nothing (D-26).
    """
    relpath = shlex.quote(DECISION_FILE_RELPATH_TEMPLATE.format(manager=manager))
    result = await executor.run_command(f"test -f ~/{relpath}", login_shell=False, timeout=10.0)
    return result.success


def automation_env_assignment_multi(decisions_by_item_id: Mapping[str, Decision]) -> str:
    """Shell-safe `VAR='{...}'` prefix pre-answering the review with one decision per
    item id (D-26's hidden hook -- `package_review.PACKAGE_REVIEW_AUTOMATION_ENV`).

    The automation hook accepts any `Decision` value for any item id present in the
    review's groups, regardless of whether the interactive checkbox UI can produce that
    value yet (`package_review.py`'s own docstring: SKIP_ALWAYS has no ordinary checkbox
    path for a non-unreproducible item) -- tests needing to exercise SKIP_ALWAYS on a
    regular item rely on exactly this to prove the underlying mechanism ahead of that UI.
    """
    mapping = json.dumps({item_id: decision.value for item_id, decision in decisions_by_item_id.items()})
    return f"{PACKAGE_REVIEW_AUTOMATION_ENV}={shlex.quote(mapping)}"


def automation_env_assignment(item_id: str) -> str:
    """Shell-safe `VAR='{...}'` prefix pre-answering the review with one APPLY decision for
    `item_id` (D-26's hidden hook -- `package_review.PACKAGE_REVIEW_AUTOMATION_ENV`).
    """
    return automation_env_assignment_multi({item_id: Decision.APPLY})


# ---------------------------------------------------------------------------------
# `TestAFailureCostsItsOwnItemAndNothingElse`: three "unowned install" snippets authored
# directly into pc1's registry (D-18/D-20/D-21) -- two that genuinely `apt-get install` a
# real package, one that deliberately exits non-zero.
# `ManualInstallsSyncJob._scan_unowned_installs` sorts its findings alphabetically by path,
# which is what places the failing item strictly BETWEEN the two installs in convergence
# order (a < b < c below), so "the item after the failure was still processed" is a real,
# ordered claim. `manual_installs_sync` is ordered FIRST in that test's config, so the same
# snippet is also what fails BEFORE the three jobs whose work the test then checks survived.
# ---------------------------------------------------------------------------------

CONTINUE_TEST_MARKER_ROOT = "/opt"
CONTINUE_TEST_MARKER_INSTALL_FIRST = f"{CONTINUE_TEST_MARKER_ROOT}/pcswitcher-it-continue-a-install-first"
CONTINUE_TEST_MARKER_FAIL = f"{CONTINUE_TEST_MARKER_ROOT}/pcswitcher-it-continue-b-fail"
CONTINUE_TEST_MARKER_INSTALL_SECOND = f"{CONTINUE_TEST_MARKER_ROOT}/pcswitcher-it-continue-c-install-second"
CONTINUE_TEST_MARKERS = (
    CONTINUE_TEST_MARKER_INSTALL_FIRST,
    CONTINUE_TEST_MARKER_FAIL,
    CONTINUE_TEST_MARKER_INSTALL_SECOND,
)
DELIBERATE_FAILURE_MESSAGE = "deliberate integration-test failure"


# What a stock Ubuntu 24.04 machine's own `/usr/local` holds — the two scan roots plus the
# nine entries `base-files` creates under `/usr/local`, none of which may ever be presented
# as a finding. Restated rather than imported (the same rule this module's snap/flatpak
# parsers follow): the claim is about what a real machine looks like, so a test agreeing
# with whatever the shipped constant currently says would assert nothing.
STOCK_DIRECTORIES = (
    "/opt",
    "/usr/local",
    "/usr/local/bin",
    "/usr/local/etc",
    "/usr/local/games",
    "/usr/local/include",
    "/usr/local/lib",
    "/usr/local/man",
    "/usr/local/sbin",
    "/usr/local/share",
    "/usr/local/src",
)

# What a run with nobody to ask writes about each item it could not ask about
# (`packages.review._warn_every_item_unasked`). It is the only place a run writes down its
# WHOLE finding set, one line per item, which is what makes it countable.
UNASKED_ITEM_MARKER = "not asked, declined for this run (no TTY): "


def unowned_item_id(path: str) -> str:
    """The `UnreproducibleItem.item_id` a `_scan_unowned_installs`-detected path at
    `path` would produce (module docstring: identity is `unreproducible:<origin>:
    <identifier>`, independent of `label`).
    """
    return UnreproducibleItem(origin="unowned-path", identifier=path, label=path).item_id


async def create_unowned_marker(executor: BashLoginRemoteExecutor, path: str) -> None:
    """Create a dpkg-unowned directory at `path` holding one file (requires root: `/opt` is
    root-owned) so `ManualInstallsSyncJob._scan_unowned_installs` detects `path` ITSELF as an
    UNREPRODUCIBLE item on the next `plan()`.

    The file is what makes it that item: a directory with no file anywhere beneath it is an
    empty shape and no finding at all, and one holding only directories is judged by its
    shape instead (`PKG-FR-MANUAL-SCOPE`, `PKG-FR-MANUAL-OPT-SHAPE`).
    """
    quoted = shlex.quote(path)
    result = await executor.run_command(
        f"sudo mkdir --parents {quoted} && sudo touch {shlex.quote(f'{path}/marker')}",
        login_shell=False,
        timeout=15.0,
    )
    assert result.success, f"Failed to create unowned marker {path}: {result.stderr}"


async def remove_unowned_marker(executor: BashLoginRemoteExecutor, path: str) -> None:
    await executor.run_command(f"sudo rm --recursive --force {shlex.quote(path)}", login_shell=False, timeout=15.0)


async def author_snippet(executor: BashLoginRemoteExecutor, item_id: str, label: str, body: str) -> None:
    """Author one snippet directly into `executor`'s registry (D-20), bypassing the
    interactive per-entry capture prompt entirely -- the test does not depend on that
    UI path, only on the registry's own read/write contract (`package_state.py`).
    """
    await SnippetRegistry(executor).add(
        Snippet(
            item_id=item_id,
            label=label,
            body=body,
            authored_at=datetime.now(UTC).isoformat(),
            authored_on="integration-test",
        )
    )


# -- snap helpers: name/revision parsing, independent of snap_sync's private parser --

SNAP_INFO_REVISION_RE = re.compile(r"\((\d+)\)")

# Snaps whose removal could break snapd itself or the base runtime every other snap
# depends on -- never a safe divergence/removal candidate for a VM test (T-02-28).
SNAP_REMOVAL_DENYLIST = frozenset({"snapd", "core", "core16", "core18", "core20", "core22", "core24", "bare"})

# The snaps `tests/integration/scripts/internal/vm-test-fixtures.sh` puts on BOTH VMs,
# alphabetically -- the subjects every snap test below operates on. A stock Ubuntu 24.04
# VM carries only `SNAP_REMOVAL_DENYLIST` members, so without these there is nothing a
# test may hold, diverge or remove. `hello` leads the list because it is the one with
# distinct revisions across its channels, which is what `alternate_snap_revision` needs.
FIXTURE_SNAPS = ("hello", "hello-world")


def parse_snap_list_names_revisions(output: str) -> dict[str, str]:
    """Parse `snap list --all` into `{name: revision}` by HEADER column names (RESEARCH
    Open Question 2: never assume fixed column offsets). Deliberately independent of
    `snap_sync._parse_snap_list` -- that parser is private to `snap_sync.py`, and this
    module must not reach into another module's private names.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return {}
    header = lines[0].split()
    try:
        name_idx = header.index("Name")
        rev_idx = header.index("Rev")
    except ValueError:
        return {}
    max_idx = max(name_idx, rev_idx)
    result: dict[str, str] = {}
    for line in lines[1:]:
        fields = line.split()
        if len(fields) <= max_idx:
            continue
        result[fields[name_idx]] = fields[rev_idx]
    return result


def parse_snap_info_revisions(output: str) -> set[str]:
    """Every revision number named in a `snap info` channel table (parenthesised
    integers) -- used to find an alternate installable revision to deliberately diverge
    a snap to, without hardcoding one.
    """
    return set(SNAP_INFO_REVISION_RE.findall(output))


def fixture_snap_names(count: int) -> list[str]:
    """The first `count` fixture snaps outside `SNAP_REMOVAL_DENYLIST` (T-02-28: never a
    base/snapd runtime everything else depends on).
    """
    subjects = [name for name in FIXTURE_SNAPS if name not in SNAP_REMOVAL_DENYLIST][:count]
    assert len(subjects) == count, (
        f"Need {count} subjects out of the fixture snaps {FIXTURE_SNAPS}, of which "
        f"{sorted(set(FIXTURE_SNAPS) & SNAP_REMOVAL_DENYLIST)} may never be one."
    )
    return subjects


async def ensure_snaps_installed(executor: BashLoginRemoteExecutor, names: Sequence[str]) -> None:
    """Make every one of `names` installed on `executor`'s machine, doing nothing about the
    ones that already are -- the snap counterpart of `ensure_installed_and_manual`.

    One `snap list --all` decides for all of them, so a machine that has them pays a single
    read (measured: hundredths of a second against seconds for an install).

    At whatever revision the store offers, exactly as the fixture script installs them: the
    tests that need a particular revision read it off the machines and diverge to it
    themselves.
    """
    result = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    installed = set(parse_snap_list_names_revisions(result.stdout))
    for name in names:
        if name in installed:
            continue
        created = await executor.run_command(
            f"sudo snap install {shlex.quote(name)}", login_shell=False, timeout=300.0
        )
        assert created.success, (
            f"Failed to install the fixture snap {name}, so this scenario has no subject to work on. The fixture "
            f"snaps are created by tests/integration/scripts/internal/vm-test-fixtures.sh.\n{created.stderr}"
        )


async def snap_subjects(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor, count: int = 1
) -> list[str]:
    """The first `count` fixture snaps (`FIXTURE_SNAPS`), converged to installed on BOTH
    machines.

    Converged rather than asserted, for the reason `test_package_sync.py`'s docstring gives for
    the apt subjects: a scenario here removes a snap when its claim needs one removed and puts nothing
    back, so getting the machines to "installed on both" belongs to whoever needs it next
    rather than to whoever last touched it. The read `ensure_snaps_installed` makes is what
    keeps that free whenever the previous scenario already left them installed.
    """
    subjects = fixture_snap_names(count)
    # One machine's snapd knows nothing of the other's, so both converge at once.
    _ = await asyncio.gather(
        ensure_snaps_installed(pc1_executor, subjects), ensure_snaps_installed(pc2_executor, subjects)
    )
    return subjects


async def snap_subject(pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor) -> str:
    """The single fixture snap every one-subject snap test operates on."""
    return (await snap_subjects(pc1_executor, pc2_executor, count=1))[0]


async def alternate_snap_revision(executor: BashLoginRemoteExecutor, name: str, current_revision: str) -> str:
    """An installable revision of `name` distinct from `current_revision`, read from
    `snap info`'s channel table -- what a test moves the target to so the sync has a real
    revision divergence to converge (D-06).

    Read rather than hardcoded: pinning a revision number would rot the moment the store
    published a new one. `FIXTURE_SNAPS[0]` is chosen precisely because it carries
    distinct revisions across its channels, so this always has something to return.
    """
    info = await executor.run_command(f"snap info {shlex.quote(name)}", login_shell=False, timeout=20.0)
    alternates = sorted(rev for rev in parse_snap_info_revisions(info.stdout) if rev != current_revision)
    assert alternates, (
        f"`snap info {name}` names no installable revision other than {current_revision}, so no revision "
        f"divergence can be created. The fixture snap is chosen for having several; check `snap info {name}`:\n"
        f"{info.stdout}"
    )
    return alternates[0]


def parse_snap_list_notes(output: str) -> dict[str, set[str]]:
    """Parse `snap list --all` into `{name: notes_tokens}` by HEADER column names, the
    same discipline as `parse_snap_list_names_revisions` (never fixed column offsets).

    The Notes column is a comma-separated token list (`-` when empty); `held` there is
    the PER-SNAP refresh hold #208 D9's capture-timing assumption is about.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return {}
    header = lines[0].split()
    try:
        name_idx = header.index("Name")
        notes_idx = header.index("Notes")
    except ValueError:
        return {}
    max_idx = max(name_idx, notes_idx)
    result: dict[str, set[str]] = {}
    for line in lines[1:]:
        fields = line.split()
        if len(fields) <= max_idx:
            continue
        result[fields[name_idx]] = {token for token in fields[notes_idx].split(",") if token and token != "-"}
    return result


async def snap_notes(executor: BashLoginRemoteExecutor, name: str) -> set[str]:
    """The Notes tokens `snap list --all` currently reports for `name` on `executor`'s
    machine (empty when the snap is absent).
    """
    result = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    return parse_snap_list_notes(result.stdout).get(name, set())


def parse_snap_saved_rows(output: str) -> list[tuple[str, str]]:
    """Parse `snap saved` into `(set_id, snap_name)` pairs by HEADER column names, the same
    discipline as the `snap list` parsers above (never fixed column offsets).

    `snap saved` prints `No snapshots found.` and no header on a machine holding none,
    which parses to an empty list rather than a crash.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].split()
    try:
        set_idx = header.index("Set")
        snap_idx = header.index("Snap")
    except ValueError:
        return []
    max_idx = max(set_idx, snap_idx)
    rows: list[tuple[str, str]] = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) <= max_idx:
            continue
        rows.append((fields[set_idx], fields[snap_idx]))
    return rows


async def snap_saved_rows(executor: BashLoginRemoteExecutor) -> list[tuple[str, str]]:
    """Every `(set_id, snap_name)` snapshot pair snapd currently holds on `executor`'s
    machine.

    Under sudo, like every other snapd read in this module: the snapshot snapd takes when a
    snap is removed covers system data as well as the invoking user's, and an unprivileged
    listing is not the whole picture of what the machine holds.
    """
    result = await executor.run_command("sudo snap saved", login_shell=False, timeout=20.0)
    return parse_snap_saved_rows(result.stdout)


async def snap_revision(executor: BashLoginRemoteExecutor, name: str) -> str | None:
    """The revision `name` is active at on `executor`'s machine, or None when it is absent."""
    result = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    return parse_snap_list_names_revisions(result.stdout).get(name)


# A sideloaded snap needs a base its machine already has, or snapd downloads one. Read off
# the machine rather than hardcoded (`installed_base_snap`): which core* snap a stock
# Ubuntu 24.04 carries depends on what else is installed, and the preference order below
# only decides which of the present ones to declare.
SIDELOAD_BASE_PREFERENCE = ("core24", "core22", "core20", "core18", "core16", "core")


async def installed_base_snap(executor: BashLoginRemoteExecutor) -> str:
    """A base snap already installed on `executor`'s machine, for a sideload's `snap.yaml`."""
    result = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    installed = set(parse_snap_list_names_revisions(result.stdout))
    for base in SIDELOAD_BASE_PREFERENCE:
        if base in installed:
            return base
    raise AssertionError(
        f"No base snap out of {SIDELOAD_BASE_PREFERENCE} is installed, so a sideload declaring one would make "
        f"snapd download it.\n{result.stdout}"
    )


async def create_sideloaded_snap(executor: BashLoginRemoteExecutor, directory: str, name: str, base: str) -> None:
    """Install `name` on `executor`'s machine from LOCAL bytes, so `snap list` reports it at
    an `x`-prefixed revision — the shape `PKG-FR-SNAP-SIDELOAD` puts out of scope.

    `snap try <dir>` rather than `snap pack` + `snap install --dangerous`: it produces the
    same sideloaded identity from a directory, with no squashfs build in between. The
    snap declares no apps and no hooks, so there is nothing to confine and nothing to run;
    `base` is one the machine already holds, so snapd fetches nothing.

    `/var/tmp` rather than `/opt` or `/usr/local`: those two are `manual_installs_sync`'s
    scan roots, and a directory left there by a failed cleanup would show up as somebody
    else's finding.
    """
    snap_yaml = (
        f"name: {name}\n"
        "version: '1.0'\n"
        "summary: pc-switcher integration-test sideload\n"
        "description: A snap installed from local bytes, which a sync must leave alone.\n"
        f"base: {base}\n"
        "confinement: strict\n"
        "grade: stable\n"
    )
    result = await executor.run_command(
        f"sudo mkdir --parents {shlex.quote(f'{directory}/meta')} && "
        f"printf %s {shlex.quote(snap_yaml)} | sudo tee {shlex.quote(f'{directory}/meta/snap.yaml')} > /dev/null && "
        f"sudo snap try {shlex.quote(directory)}",
        login_shell=False,
        timeout=180.0,
    )
    assert result.success, (
        f"`snap try {directory}` failed, so there is no sideloaded snap to test with: {result.stderr}"
    )


async def remove_sideloaded_snap(executor: BashLoginRemoteExecutor, directory: str, name: str) -> None:
    """Undo `create_sideloaded_snap`, unconditionally (`;`, not `&&`) so a setup that
    failed halfway still has the rest of itself removed.
    """
    await executor.run_command(
        f"sudo snap remove --purge {shlex.quote(name)}; sudo rm --recursive --force {shlex.quote(directory)}",
        login_shell=False,
        timeout=180.0,
    )


# snapd's own switch for a machine that must not reach the store (snapd 2.60+). It is what
# makes ONE snap item fail for a real reason while the rest of the run still lands: an
# install or a refresh needs the store, a removal does not.
SNAP_STORE_OFFLINE_CMD = "sudo snap set system store.access=offline"
SNAP_STORE_ONLINE_CMD = "sudo snap unset system store.access"


async def home_dir(executor: BashLoginRemoteExecutor) -> str:
    """The absolute home directory of the SSH user on `executor`'s machine.

    Read rather than composed from the test's own environment: `snap_sync_exclude_paths()`
    resolves `~/snap` against the home of the process running the sync, so the folder the
    boundary test mirrors has to be that same directory.
    """
    result = await executor.run_command('printf %s "$HOME"', login_shell=False, timeout=10.0)
    home = result.stdout.strip()
    assert result.success and home.startswith("/"), f"could not read $HOME: {result.stdout!r} {result.stderr!r}"
    return home


async def machine_utc_now(executor: BashLoginRemoteExecutor) -> datetime:
    """`executor`'s own idea of the current UTC instant.

    The suspension's expiry is computed on each machine's clock, so what it must be
    compared against is that machine's clock — never this test runner's.
    """
    result = await executor.run_command("date --utc +%Y-%m-%dT%H:%M:%SZ", login_shell=False, timeout=10.0)
    assert result.success, f"could not read the machine's clock: {result.stderr}"
    return parse_rfc3339_utc(result.stdout)


def parse_rfc3339_utc(value: str) -> datetime:
    """Parse an RFC3339 instant, the shape snapd's `refresh.hold` validator accepts.

    Anything that is not an instant at all — notably snapd's `forever` — raises
    `AssertionError` naming what was read, rather than the bare `ValueError` a caller
    reading a machine's own answer cannot interpret.
    """
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise AssertionError(f"{text!r} is not an RFC3339 instant: {exc}") from exc


# The orchestrator's own sync-window hold expression (`orchestrator._SNAP_HOLD_TIMESTAMP_CMD`
# / `_apply_snap_hold`), restated rather than imported: those names are private to
# `orchestrator.py`, and this module deliberately re-derives what it asserts against (the
# same rule the snap/flatpak parsers above follow). A timed RFC3339-UTC value, computed on
# the host, is exactly the shape snapd's `refresh.hold` validator accepts.
SYSTEM_REFRESH_HOLD_SET_CMD = (
    "sudo snap set system refresh.hold=\"$(date --utc --date='+6 hours' +%Y-%m-%dT%H:%M:%SZ)\""
)


async def capture_system_refresh_hold(executor: BashLoginRemoteExecutor) -> str | None:
    """`executor`'s current system-wide `refresh.hold`, or `None` when unset (`snap get`
    exits non-zero, or prints nothing, for an unset option). Read-only.

    Under sudo like the orchestrator's own capture: snapd admin-gates READING snap config,
    so unprivileged this never returns a value -- it fails with "access denied", and every
    machine reads as hold-free.
    """
    result = await executor.run_command("sudo snap get system refresh.hold", login_shell=False, timeout=15.0)
    value = result.stdout.strip()
    return value if result.success and value else None


async def engage_system_refresh_hold(executor: BashLoginRemoteExecutor) -> None:
    """Pause snapd auto-refresh on `executor`'s machine the same way a sync does, so a
    background auto-refresh cannot mutate `snap list` mid-test (and, for the #208 D9
    check, so a system-wide hold is genuinely in force while per-snap Notes are read).
    """
    result = await executor.run_command(SYSTEM_REFRESH_HOLD_SET_CMD, login_shell=False, timeout=30.0)
    assert result.success, f"Failed to engage a system-wide snapd refresh.hold: {result.stderr}"


async def restore_system_refresh_hold(executor: BashLoginRemoteExecutor, prior: str | None) -> None:
    """Put `executor`'s `refresh.hold` back exactly as found -- restoring the prior value
    or clearing it (empty string, which snapd treats as unset), mirroring the
    orchestrator's own teardown so the test leaves no standing hold behind.
    """
    value = shlex.quote(prior) if prior is not None else '""'
    result = await executor.run_command(f"sudo snap set system refresh.hold={value}", login_shell=False, timeout=30.0)
    if not result.success:
        print(f"[cleanup] failed to restore system refresh.hold: {result.stderr}")


async def holdable_snaps(executor: BashLoginRemoteExecutor, count: int = 1) -> list[str]:
    """The first `count` fixture snaps, converged to installed on `executor`'s machine --
    safe subjects for a per-snap `--hold`/`--unhold` round trip (which, unlike a removal,
    leaves the snap itself untouched).

    One-machine variant of `snap_subjects`, for the tests that never run a sync. It converges
    for the same reason: the scenarios before this one leave the fixture snaps wherever their
    own claims needed them.
    """
    subjects = fixture_snap_names(count)
    await ensure_snaps_installed(executor, subjects)
    return subjects


# -- flatpak helpers: independent of flatpak_sync's private parsers ------------------


def parse_flatpak_list_lines(output: str) -> list[tuple[str, str, str, str, str]]:
    """Parse `flatpak list --app --columns=application,version,origin,installation,ref`
    into `(application, version, origin, installation, ref)` tuples, tab-separated (mirrors
    `flatpak_sync._parse_flatpak_list`'s shape, kept independent since that parser is
    private to `flatpak_sync.py`).
    """
    rows: list[tuple[str, str, str, str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        rows.append((fields[0], fields[1], fields[2], fields[3], fields[4]))
    return rows


# The flatpak subject `tests/integration/scripts/internal/vm-test-fixtures.sh` provisions:
# THE REAL FLATHUB, user scope. Both VMs carry the remote and the app's runtime; only pc1
# (the sync source) carries the APP, which is the source->target ref divergence the
# convergence test needs — it is part of the baseline, not something a test manufactures.
#
# Real, not a local stand-in, because a synthetic repository only ever tests this
# project's model of a remote. Flathub's own trust configuration is what makes the #215
# key-replication claim below mean anything: an `options` column that is genuinely empty,
# a real `flathub.trustedkeys.gpg`, a real `--gpg-import` round trip.
FIXTURE_FLATPAK_APP = "io.github.fragglet.sdl_sopwith"
FIXTURE_FLATPAK_REMOTE = "flathub"
FIXTURE_FLATPAK_SCOPE: Literal["user", "system"] = "user"
# Flathub's `.flatpakrepo`, i.e. how a user adds the remote — it carries the URL, the
# `gpg-verify=true` and the signing key together. Used only to put the remote back after
# a test deleted it; `flatpak_subject` reads the resulting URL off the machine itself.
FIXTURE_FLATPAK_REPOFILE = "https://dl.flathub.org/repo/flathub.flatpakrepo"

# The second fixture remote, on pc1 only and feeding no ref: what makes "a remote no
# approved ref needs does not travel" falsifiable. Deleted from pc2 by the fixture script
# and by `restore_flatpak_target_baseline`, so a run that made it travel cannot leave the
# next run unable to detect that.
FIXTURE_UNUSED_FLATPAK_REMOTE = "flathub-beta"


async def flatpak_subject(
    executor: BashLoginRemoteExecutor,
) -> tuple[str, str, Literal["user", "system"], str, str, str]:
    """`(application, version, scope, remote_name, remote_url, ref)` for the fixture
    flatpak ref installed on `executor` (the source), used to prove D-06/D-14 convergence
    for a real ref+remote pair.

    Read off `flatpak list`/`flatpak remotes` rather than assembled from the constants
    above so the tuple carries the machine's own idea of the ref (notably `version`,
    which the diff compares) and so a machine missing the fixture fails naming it.
    """
    list_result = await executor.run_command(
        "flatpak list --app --columns=application,version,origin,installation,ref",
        login_shell=False,
        timeout=20.0,
    )
    rows = [row for row in parse_flatpak_list_lines(list_result.stdout) if row[0] == FIXTURE_FLATPAK_APP]
    assert rows, (
        f"The fixture flatpak {FIXTURE_FLATPAK_APP} is not installed. It is created by "
        f"tests/integration/scripts/internal/vm-test-fixtures.sh.\n{list_result.stdout}"
    )
    application, version, origin, installation, ref = rows[0]
    assert installation == FIXTURE_FLATPAK_SCOPE, (
        f"{application} is installed in scope {installation!r}, expected {FIXTURE_FLATPAK_SCOPE!r}"
    )

    scope_flag = "--user" if FIXTURE_FLATPAK_SCOPE == "user" else "--system"
    remotes_result = await executor.run_command(
        f"flatpak remotes {scope_flag} --columns=name,url", login_shell=False, timeout=20.0
    )
    for line in remotes_result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[0] == origin:
            return application, version, FIXTURE_FLATPAK_SCOPE, fields[0], fields[1], ref
    raise AssertionError(
        f"{application}'s origin remote {origin!r} is not configured in scope "
        f"{FIXTURE_FLATPAK_SCOPE}:\n{remotes_result.stdout}"
    )


async def restore_flatpak_target_baseline(executor: BashLoginRemoteExecutor) -> None:
    """Put `executor` (the sync TARGET) back to what the fixture script leaves behind:
    the Flathub remote configured, the app's runtime installed, and the app itself ABSENT.

    Not symmetric with the source: the app's absence here IS the fixture (see
    `FIXTURE_FLATPAK_APP`), so a test that made the sync install it must undo that or the
    next run starts from a converged pair and proves nothing. The remote is re-added from
    Flathub's own `.flatpakrepo`, which restores its real trust configuration — the same
    keyring bytes, so no spurious trust divergence is left behind (verified live).

    The runtime is deliberately left installed: `flatpak uninstall --unused` would sweep
    it and turn every later app install into a multi-hundred-MB download.
    """
    scope_flag = "--user" if FIXTURE_FLATPAK_SCOPE == "user" else "--system"
    sudo = "" if FIXTURE_FLATPAK_SCOPE == "user" else "sudo "
    result = await executor.run_command(
        f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(FIXTURE_FLATPAK_APP)} || true; "
        f"{sudo}flatpak remote-delete {scope_flag} --force "
        f"{shlex.quote(FIXTURE_UNUSED_FLATPAK_REMOTE)} || true; "
        f"{sudo}flatpak remote-add {scope_flag} --if-not-exists "
        f"{shlex.quote(FIXTURE_FLATPAK_REMOTE)} {shlex.quote(FIXTURE_FLATPAK_REPOFILE)}",
        login_shell=False,
        timeout=180.0,
    )
    if not result.success:
        print(f"[cleanup] failed to restore the target's flatpak baseline: {result.stderr}")


async def restore_flatpak_source_baseline(
    executor: BashLoginRemoteExecutor, remote_name: str, scope: Literal["user", "system"], filter_path: str
) -> None:
    """Put `executor` (the sync SOURCE) back to an UNFILTERED `remote_name`, and drop the
    filter file at `filter_path`.

    Delete-and-re-add rather than `flatpak remote-modify --no-filter`: that option's
    availability on this flatpak is not something this suite has measured, and re-adding from
    Flathub's own `.flatpakrepo` is the one restore already proven to reproduce the remote's
    trust configuration byte-for-byte (`restore_flatpak_target_baseline`). The app installed
    from it stays installed and keeps naming `remote_name` as its origin.
    """
    scope_flag = "--user" if scope == "user" else "--system"
    sudo = "" if scope == "user" else "sudo "
    result = await executor.run_command(
        f"{sudo}flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true; "
        f"{sudo}flatpak remote-add {scope_flag} --if-not-exists {shlex.quote(remote_name)} "
        f"{shlex.quote(FIXTURE_FLATPAK_REPOFILE)}; "
        f"rm --force {filter_path}",
        login_shell=False,
        timeout=180.0,
    )
    if not result.success:
        print(f"[cleanup] failed to restore the source's unfiltered {remote_name}: {result.stderr}")


async def flatpak_app_rows(executor: BashLoginRemoteExecutor) -> list[tuple[str, str, str, str, str]]:
    """Every installed APP on `executor`'s machine, as `parse_flatpak_list_lines` tuples.

    The same five columns `flatpak_sync` captures, so a comparison of this list before and
    after a run is a comparison of exactly what the job would have seen.
    """
    result = await executor.run_command(
        "flatpak list --app --columns=application,version,origin,installation,ref", login_shell=False, timeout=20.0
    )
    return parse_flatpak_list_lines(result.stdout)


async def flatpak_remote_row(
    executor: BashLoginRemoteExecutor, remote_name: str, scope: Literal["user", "system"]
) -> tuple[str, tuple[str, ...]]:
    """`(url, options)` for `remote_name` in `scope` on `executor`'s machine.

    `options` is flatpak's own comma-separated token list, split the way
    `flatpak_sync._parse_flatpak_remotes` splits it -- an optionless remote prints two fields
    rather than three, so both widths are accepted here as they are there. It is read as a
    tuple of TOKENS, never searched as a string: `filtered` and `no-gpg-verify` are
    independent members.
    """
    scope_flag = "--user" if scope == "user" else "--system"
    result = await executor.run_command(
        f"flatpak remotes {scope_flag} --columns=name,url,options", login_shell=False, timeout=20.0
    )
    for line in nonblank_lines(result.stdout):
        fields = line.split("\t")
        if fields[0] == remote_name:
            options = tuple(token for token in fields[2].split(",") if token) if len(fields) == 3 else ()
            return fields[1], options
    raise AssertionError(
        f"no {scope}-scope flatpak remote named {remote_name!r} on this machine. The fixture remotes are "
        f"created by tests/integration/scripts/internal/vm-test-fixtures.sh.\n{result.stdout}"
    )


async def flatpak_remote_filter(
    executor: BashLoginRemoteExecutor, remote_name: str, scope: Literal["user", "system"]
) -> str | None:
    """The absolute path of `remote_name`'s ref filter on `executor`'s machine, or `None`
    when it carries none.

    Read off flatpak's own `filter` column rather than from the string a test handed
    `remote-modify`: the path a test writes contains `$HOME` for the remote shell to expand,
    and it is the EXPANDED path flatpak records and `flatpak_sync` replicates. An unfiltered
    remote prints `-` there, which is flatpak's word for "no filter".
    """
    scope_flag = "--user" if scope == "user" else "--system"
    result = await executor.run_command(
        f"flatpak remotes {scope_flag} --columns=name,filter", login_shell=False, timeout=20.0
    )
    for line in nonblank_lines(result.stdout):
        fields = line.split("\t")
        if fields[0] == remote_name:
            path = fields[1] if len(fields) > 1 else "-"
            return None if path in ("", "-") else path
    raise AssertionError(f"no {scope}-scope flatpak remote named {remote_name!r} on this machine.\n{result.stdout}")


# The ref filter the source carries, and the bytes the target's copy must end up holding
# (`PKG-FR-FLATPAK-FILTER` replicates the file itself, not merely the `filtered` token). It
# allows the fixture app, so a machine left with it by a failed cleanup is no worse off than
# one with no filter at all.
FLATPAK_FILTER_BODY = f"allow {FIXTURE_FLATPAK_APP}\n"

# The token `flatpak remotes --columns=options` prints for a remote carrying a ref filter --
# restated here rather than imported, so the test fails when the shipped constant and the real
# flatpak disagree instead of agreeing with whatever `flatpak_sync` happens to say.
FLATPAK_FILTERED_OPTION = "filtered"


# -- apt repository-state helpers (D-11/D-12): synthesize a repo+key divergence -----
#
# The two `/etc/apt` directories the apt-repository-state test touches (apt_sync.py owns
# the full five-directory set).
APT_SOURCES_DIR = "/etc/apt/sources.list.d"
APT_KEYRINGS_DIR = "/etc/apt/keyrings"
APT_PREFERENCES_DIR = "/etc/apt/preferences.d"

# Host the synthetic repository points at. `.invalid` is reserved by RFC 2606 and can
# never resolve, so apt reaches this repo only to fail, and the name appears in
# `apt-get update`'s output for exactly as long as the repo is configured.
SYNTHETIC_REPO_HOST = "pcswitcher-it.invalid"


async def create_synthetic_repo_and_key(executor: BashLoginRemoteExecutor) -> tuple[str, str]:
    """Create a synthetic vendor apt repository the target lacks on `executor` (the source):
    a deb822 `.sources` file under `/etc/apt/sources.list.d/` whose `Signed-By:` names a
    signing-key file under `/etc/apt/keyrings/`, plus that key file with dummy bytes.
    Returns `(source_filename, key_filename)`.

    Both directories are root-owned and `/etc/apt/keyrings` is absent on a fresh Ubuntu
    24.04, so `mkdir --parents` runs first (the shipped invariant) and every write goes through
    `sudo tee`. Filenames are uuid-suffixed so the pair is unique and the fresh target
    provably lacks it. Dummy key bytes are fine: D-12 copies keys verbatim without
    validating, and `SYNTHETIC_REPO_HOST` never resolves, so an `apt-get update` that
    sees this repo can only fail to fetch its index -- it can never install anything from
    it, on a dry run or a real one.
    """
    uniq = uuid4().hex[:12]
    source_filename = f"pcswitcher-it-repo-{uniq}.sources"
    key_filename = f"pcswitcher-it-key-{uniq}.gpg"
    source_dest = f"{APT_SOURCES_DIR}/{source_filename}"
    key_dest = f"{APT_KEYRINGS_DIR}/{key_filename}"
    source_body = (
        "Types: deb\n"
        f"URIs: https://{SYNTHETIC_REPO_HOST}/repo\n"
        "Suites: stable\n"
        "Components: main\n"
        f"Signed-By: {key_dest}\n"
    )
    result = await executor.run_command(
        f"sudo mkdir --parents {shlex.quote(APT_KEYRINGS_DIR)} && "
        f"printf %s {shlex.quote(source_body)} | sudo tee {shlex.quote(source_dest)} > /dev/null && "
        f"printf %s {shlex.quote(f'pcswitcher-it-dummy-key-{uniq}')} | sudo tee {shlex.quote(key_dest)} > /dev/null",
        login_shell=False,
        timeout=20.0,
    )
    assert result.success, f"Failed to create synthetic repo+key on source: {result.stderr}"
    return source_filename, key_filename


async def install_from_a_repo_the_target_lacks(executor: BashLoginRemoteExecutor) -> tuple[str, str, str]:
    """On `executor` (the source): build a trivial `.deb`, publish it in a flat `file:` apt
    repository, declare that repository, and install the package from it. Returns
    `(package_name, repo_dir, list_filename)`.

    The only construction that produces ADR-020 D-34's class 3 on real machines: a package
    the source has FROM A REPOSITORY IT DECLARES whose name the target's apt has never
    heard. `create_synthetic_repo_and_key`'s repository cannot do it — its host does not
    resolve, so no package can be installed from it, and a repository feeding no package
    does not travel at all.

    A `file:` repository is used rather than a served one because the source must genuinely
    install from it: measured in a stock `ubuntu:24.04`, a flat `deb [trusted=yes] file:...
    ./` repository with a hand-written `Packages` index updates and installs, `apt-cache
    policy` reports the `file:` URI as the installed version's origin, and on a machine
    without the repository the same name gives `apt-cache policy` exit 0 with no block and
    `apt-get --dry-run install` exit 100 `E: Unable to locate package`.

    The package is empty — control metadata only, no files — so installing it changes
    nothing about the machine beyond dpkg's own records.
    """
    uniq = uuid4().hex[:12]
    name = f"pcswitcher-it-pkg-{uniq}"
    repo_dir = f"/opt/pcswitcher-it-repo-{uniq}"
    list_filename = f"pcswitcher-it-repo-{uniq}.list"
    control = (
        f"Package: {name}\nVersion: 1.0\nArchitecture: all\n"
        "Maintainer: pc-switcher integration tests <nobody@example.invalid>\n"
        "Description: synthetic package for pc-switcher integration tests\n"
    )
    # `Size`/`SHA256` are computed on the machine from the `.deb` `dpkg-deb` just produced:
    # apt rejects an index whose digest does not match the file it points at.
    build = "\n".join(
        (
            "set -eu",
            f"build=$(mktemp --directory)/{name}",
            'mkdir --parents "$build/DEBIAN"',
            f'printf %s {shlex.quote(control)} > "$build/DEBIAN/control"',
            'dpkg-deb --build "$build" "$build.deb" > /dev/null',
            f"sudo mkdir --parents {shlex.quote(repo_dir)}",
            f'sudo cp "$build.deb" {shlex.quote(f"{repo_dir}/{name}.deb")}',
            'size=$(stat --format=%s "$build.deb")',
            'digest=$(sha256sum "$build.deb" | cut --delimiter=" " --fields=1)',
            f"{{ printf %s {shlex.quote(control)};"
            f' printf "Filename: ./{name}.deb\\nSize: %s\\nSHA256: %s\\n\\n" "$size" "$digest"; }}'
            f" | sudo tee {shlex.quote(f'{repo_dir}/Packages')} > /dev/null",
            f"printf '%s\\n' {shlex.quote(f'deb [trusted=yes] file:{repo_dir} ./')}"
            f" | sudo tee {shlex.quote(f'{APT_SOURCES_DIR}/{list_filename}')} > /dev/null",
        )
    )
    built = await executor.run_command(build, login_shell=False, timeout=60.0)
    assert built.success, f"Failed to build the synthetic repository on the source: {built.stderr}"

    updated = await apt_get_update_for(executor, f"{APT_SOURCES_DIR}/{list_filename}")
    assert updated.success, f"apt-get update failed on the source after adding {repo_dir}: {updated.stderr}"

    installed = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(name)}",
        login_shell=False,
        timeout=120.0,
    )
    assert installed.success, f"Failed to install {name} from {repo_dir} on the source: {installed.stderr}"
    return name, repo_dir, list_filename


async def undeclare_local_repository(executor: BashLoginRemoteExecutor, repo_dir: str, list_filename: str) -> None:
    """Take a test-built `file:` repository off `executor`'s machine: its declaration, the
    published tree, and the index apt cached for it.

    The packages installed FROM it stay installed (`test_package_sync.py`'s docstring). The
    declaration is what every later `apt-get update` on the machine pays for and taking it off
    is a `rm`; the purge is dpkg work nothing later reads. The tree goes with it because it sits under
    `/opt`, one of `manual_installs_sync`'s scan roots.

    Every step runs unconditionally (`;`, not `&&`) so a setup that failed halfway still
    has the rest of itself removed.
    """
    await executor.run_command(
        f"sudo rm --force --recursive {shlex.quote(repo_dir)} "
        f"{shlex.quote(f'{APT_SOURCES_DIR}/{list_filename}')}; "
        f"sudo rm --force /var/lib/apt/lists/_opt_{repo_dir.rsplit('/', 1)[-1]}_*",
        login_shell=False,
        timeout=60.0,
    )


async def install_a_hand_downloaded_deb(executor: BashLoginRemoteExecutor) -> str:
    """On `executor`: build a trivial `.deb` and `dpkg --install` it, the way a user who
    downloaded a vendor package does. Returns the package name.

    No repository anywhere declares it, so apt reports the INSTALLED version as the whole of
    that package's version table and names no repository origin for it — the fact
    `PKG-FR-DEB-OWNERSHIP` and `PKG-FR-MANUAL-SCOPE` turn on, and the one a mocked
    `apt-cache policy` can only assert about output somebody wrote by hand.

    Deliberately not `install_from_a_repo_the_target_lacks`: that one publishes its package
    in a `file:` repository precisely so apt DOES name an origin for it. Here there is no
    repository at all.

    The package is empty — control metadata only, no files — so installing it changes
    nothing about the machine beyond dpkg's own records.
    """
    uniq = uuid4().hex[:12]
    name = f"pcswitcher-it-handdeb-{uniq}"
    control = (
        f"Package: {name}\nVersion: 1.0\nArchitecture: all\n"
        "Maintainer: pc-switcher integration tests <nobody@example.invalid>\n"
        "Description: synthetic hand-downloaded package for pc-switcher integration tests\n"
    )
    build = "\n".join(
        (
            "set -eu",
            f"build=$(mktemp --directory)/{name}",
            'mkdir --parents "$build/DEBIAN"',
            f'printf %s {shlex.quote(control)} > "$build/DEBIAN/control"',
            'dpkg-deb --build "$build" "$build.deb" > /dev/null',
            'sudo dpkg --install "$build.deb"',
        )
    )
    result = await executor.run_command(build, login_shell=False, timeout=120.0)
    assert result.success, f"Failed to build and install the hand-downloaded .deb on the source: {result.stderr}"
    return name


def no_candidate_item_id(name: str) -> str:
    """The `UnreproducibleItem.item_id` an installed package no repository supplies produces
    (`unreproducible:apt-no-candidate:<name>`), built from the shipped dataclass rather than
    from a literal so a change to the identity fails here.
    """
    return UnreproducibleItem(origin="apt-no-candidate", identifier=name, label=name).item_id


async def create_synthetic_pin(executor: BashLoginRemoteExecutor) -> str:
    """Create a uuid-suffixed `/etc/apt/preferences.d` file the target lacks, and return its
    filename.

    A pin is in ADR-020 D-36's always-sync bucket: it travels with no review line and no
    derivation predicate, which makes it the cheapest real subject for the derived-write
    preview. Its stanza names a package and an origin neither machine has, so it is inert
    wherever it lands — a pin naming an absent origin changes nothing about apt's choices.
    """
    uniq = uuid4().hex[:12]
    filename = f"pcswitcher-it-pin-{uniq}"
    dest = f"{APT_PREFERENCES_DIR}/{filename}"
    body = f"Package: pcswitcher-it-nothing-{uniq}\nPin: origin {SYNTHETIC_REPO_HOST}\nPin-Priority: 1000\n"
    result = await executor.run_command(
        f"printf %s {shlex.quote(body)} | sudo tee {shlex.quote(dest)} > /dev/null",
        login_shell=False,
        timeout=20.0,
    )
    assert result.success, f"Failed to create synthetic pin on source: {result.stderr}"
    return filename


async def take_paths_aside(executor: BashLoginRemoteExecutor, paths: Sequence[str]) -> str:
    """Move whichever of `paths` exist into a fresh backup directory, and return it for
    `put_paths_back`.

    A move, not a captured copy: the paths this is used on are root-owned and dpkg-shipped
    (`/etc/apt`) or snapd-managed (`~/snap`), and moving the file itself is the only
    restoration that is exact in content, mode, ownership and timestamp without reproducing
    any of them. The backup sits under `/var/tmp`, outside every tree a test hands to apt or
    to rsync, so nothing reads it while it is set aside.
    """
    backup_dir = f"/var/tmp/pcswitcher-it-aside-{uuid4().hex[:12]}"
    parents = sorted({backup_dir + path.rsplit("/", 1)[0] for path in paths})
    command = " && ".join(
        [f"sudo mkdir --parents {shlex.quote(parent)}" for parent in parents]
        + [
            f"(test ! -e {shlex.quote(path)} || sudo mv {shlex.quote(path)} {shlex.quote(backup_dir + path)})"
            for path in paths
        ]
    )
    result = await executor.run_command(command, login_shell=False, timeout=20.0)
    assert result.success, f"Failed to move {list(paths)} aside into {backup_dir}: {result.stderr}"
    return backup_dir


async def put_paths_back(executor: BashLoginRemoteExecutor, backup_dir: str, paths: Sequence[str]) -> None:
    """Undo `take_paths_aside`: each path ends as whatever was there before, present or
    absent.

    Every step runs unconditionally (`;`, not `&&`) so a test that failed halfway still
    leaves the machine as it found it.
    """
    steps = [
        f"sudo rm --recursive --force {shlex.quote(path)}; "
        f"test ! -e {shlex.quote(backup_dir + path)} || sudo mv {shlex.quote(backup_dir + path)} {shlex.quote(path)}"
        for path in paths
    ]
    steps.append(f"sudo rm --force --recursive {shlex.quote(backup_dir)}")
    await executor.run_command("; ".join(steps), login_shell=False, timeout=20.0)


async def apt_get_update(executor: BashLoginRemoteExecutor) -> CommandResult:
    """Run `apt-get update` on `executor` with the output locale pinned to C, so the
    `Err:` prefix `apt_update_lines_naming`'s callers key on is apt's untranslated one
    whatever locale the machine is configured with.
    """
    return await executor.run_command(
        "sudo LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get update", login_shell=False, timeout=180.0
    )


async def apt_get_update_for(executor: BashLoginRemoteExecutor, source_path: str) -> CommandResult:
    """Refresh the index of the repository `source_path` declares, and of nothing else.

    A test that has just written one repository file needs apt to notice THAT repository;
    a plain `apt-get update` also refetches every Ubuntu archive index over the network,
    which several setups here were paying for a purely local `file:` repository (#216).
    Measured in a stock `ubuntu:24.04`: 0.02s against 0.78s with warm archives, in both the
    one-line and the deb822 file format, after which the repository's package installs.

    `sourceparts` rather than `sourcelist` because it is the one that reads both formats;
    `List-Cleanup=0` because without it apt prunes the cached lists of every repository this
    run did not visit, and the next operation would have to fetch them all back.
    """
    quoted = shlex.quote(source_path)
    return await executor.run_command(
        "narrow=$(mktemp --directory) && "
        f'sudo cp {quoted} "$narrow/" && '
        "sudo LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get update"
        ' -o Dir::Etc::sourcelist=/dev/null -o Dir::Etc::sourceparts="$narrow"'
        " -o APT::Get::List-Cleanup=0; "
        'status=$?; rm --recursive --force "$narrow"; exit $status',
        login_shell=False,
        timeout=180.0,
    )


def apt_update_lines_naming(result: CommandResult, host: str) -> list[str]:
    """Every line of an `apt-get update` run (stdout and stderr together) that names
    `host`.

    The exit code cannot witness whether an unreachable repository is configured:
    `apt-get update` exits 0 when an index fails to fetch, downgrading it to a `W:`
    warning and reusing the cached list. The output can -- apt prints `Ign:`/`Err:` lines
    naming the URI it could not reach, and prints nothing at all about a repository that
    is no longer configured -- which makes the same reading a witness in both directions.
    """
    return [line for line in nonblank_lines(f"{result.stdout}\n{result.stderr}") if host in line]


async def assert_flatpak_available(executor: BashLoginRemoteExecutor) -> None:
    """flatpak can run on `executor`'s machine, probed exactly the way
    `FlatpakSyncJob.validate()` probes it.

    flatpak is in no default Ubuntu 24.04 install and enabling `flatpak_sync` on a
    machine that lacks it is a validation error that aborts the whole sync before any job
    executes -- which is why the fixture script installs it on both VMs. Checked here so
    that absence reports itself as "the machine is not provisioned" rather than as an
    unrelated-looking validation failure inside the sync under test.
    """
    result = await executor.run_command("flatpak --version", login_shell=False, timeout=15.0)
    assert result.success, (
        "flatpak is not installed. It is installed by "
        f"tests/integration/scripts/internal/vm-test-fixtures.sh.\n{result.stderr}"
    )


# -- whole-machine package state, for "this run changed nothing" assertions ----------


# The five `/etc/apt` directories a run can write, and the one directory-scoped read that
# covers all of them. Digests, not content: the claim is "nothing was rewritten", and a
# digest says that about a root-owned file without reading one.
APT_STATE_DIRS = (
    APT_SOURCES_DIR,
    APT_PREFERENCES_DIR,
    "/etc/apt/apt.conf.d",
    APT_KEYRINGS_DIR,
    "/etc/apt/trusted.gpg.d",
)


@dataclass(frozen=True)
class MachinePackageState:
    """Every piece of package-manager state the four jobs can write on one machine, read
    from the package managers and the filesystem themselves (`apt-mark`, `dpkg-query`,
    `sha256sum` over `/etc/apt`, `snap list`, `flatpak list`, `flatpak remotes`) rather than
    from anything pc-switcher reports about them.

    Compared whole for the idempotency claim: a second run that has nothing to do must
    leave all of it byte-identical, which is a far stronger statement than "the one
    package we diverged is still installed".

    `/etc/apt` and the remote table are here because a run writes both WITHOUT a review line
    of its own — derived repository files, pins and signing keys on one side, provisioned
    remotes, replicated ref filters and deleted unused remotes on the other. A capture that
    stopped at the installed sets would call a run that rewrote every one of them a fixed
    point.
    """

    apt_manual: tuple[str, ...]
    apt_held: tuple[str, ...]
    apt_installed: tuple[str, ...]
    etc_apt_digests: tuple[str, ...]
    snap_revisions: tuple[tuple[str, str], ...]
    flatpak_refs: tuple[tuple[str, str, str, str, str], ...]
    flatpak_remotes: tuple[str, ...]


async def capture_machine_package_state(executor: BashLoginRemoteExecutor) -> MachinePackageState:
    """Read `executor`'s complete apt/snap/flatpak state (see `MachinePackageState`).

    `snap list --all` is reduced to `{name: revision}` rather than kept as raw text: the
    Version column tracks the revision, so keeping both would only add a second way for
    the same fact to be reported.

    The `/etc/apt` listing is `sudo`-qualified and guarded per directory the same way
    `apt_sync.probe.capture_dir_digests` is: `/etc/apt/keyrings` is absent on a stock Ubuntu
    24.04, and an absent directory must read as "nothing here" rather than as a failure.

    Both flatpak scopes are read, because a job writes remotes in whichever scope an
    approved application came from.

    All seven run at once: every one of them is read-only and none needs another's answer, so
    the capture costs one round trip rather than seven -- and this is the read a whole-state
    comparison takes twice per run (#216).
    """
    manual, held, dpkg, etc_apt, snaps, flatpaks, remotes = await asyncio.gather(
        executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0),
        executor.run_command("apt-mark showhold", login_shell=False, timeout=15.0),
        executor.run_command(
            "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
        ),
        executor.run_command(
            "; ".join(
                f"if sudo test -d {shlex.quote(directory)}; then "
                f"sudo find {shlex.quote(directory)} -maxdepth 1 -type f -exec sha256sum {{}} +; fi"
                for directory in APT_STATE_DIRS
            ),
            login_shell=False,
            timeout=30.0,
        ),
        executor.run_command("snap list --all", login_shell=False, timeout=20.0),
        executor.run_command(
            "flatpak list --app --columns=application,version,origin,installation,ref", login_shell=False, timeout=20.0
        ),
        executor.run_command(
            "flatpak remotes --user --columns=name,url,options,filter; "
            "flatpak remotes --system --columns=name,url,options,filter",
            login_shell=False,
            timeout=20.0,
        ),
    )
    assert etc_apt.success, f"Failed to read /etc/apt digests: {etc_apt.stderr}"
    return MachinePackageState(
        apt_manual=tuple(sorted(nonblank_lines(manual.stdout))),
        apt_held=tuple(sorted(nonblank_lines(held.stdout))),
        apt_installed=tuple(sorted(parse_dpkg_installed(dpkg.stdout))),
        etc_apt_digests=tuple(sorted(nonblank_lines(etc_apt.stdout))),
        snap_revisions=tuple(sorted(parse_snap_list_names_revisions(snaps.stdout).items())),
        flatpak_refs=tuple(sorted(parse_flatpak_list_lines(flatpaks.stdout))),
        flatpak_remotes=tuple(sorted(nonblank_lines(remotes.stdout))),
    )


# -- apt selection state, for the tests that read `apt-mark` rather than a transaction ---

# Splits several reads issued as ONE command back into their own outputs
# (testing-guide.md's command-grouping rule). Chosen so no apt or dpkg output can contain
# it.
SECTION_MARKER = "@@PCSWITCHER_IT_SECTION@@"


async def apt_selection_snapshot(
    executor: BashLoginRemoteExecutor,
) -> tuple[set[str], set[str], dict[str, str]]:
    """One machine's `(manual set, hold set, {package: installed version})`, read in ONE
    command (testing-guide.md's command-grouping rule).
    """
    result = await executor.run_command(
        f"apt-mark showmanual; echo {SECTION_MARKER}; apt-mark showhold; echo {SECTION_MARKER}; "
        "dpkg-query --show --showformat='${Package}\\t${Version}\\n'",
        login_shell=False,
        timeout=30.0,
    )
    assert result.success, f"Failed to read the machine's apt selection state: {result.stderr}"
    manual_block, hold_block, version_block = result.stdout.split(SECTION_MARKER)
    versions: dict[str, str] = {}
    for line in nonblank_lines(version_block):
        name, _, version = line.partition("\t")
        versions[name] = version
    return set(nonblank_lines(manual_block)), set(nonblank_lines(hold_block)), versions


async def a_package_both_machines_have_unheld(
    pc1_executor: BashLoginRemoteExecutor,
    pc2_executor: BashLoginRemoteExecutor,
    exclude: frozenset[str] = frozenset(),
) -> str:
    """A package manually installed at the SAME version on both machines and held on
    neither, so holding it on the source is the run's only apt work for it.

    `exclude` keeps a scenario's other apt subjects out of the answer: a run that diverges
    one package and holds another needs the two to be different packages, and every
    selection in this module is alphabetical for determinism, so without this they collide.
    """
    # One read-only snapshot per machine, taken at once.
    (
        (source_manual, source_held, source_versions),
        (target_manual, target_held, target_versions),
    ) = await asyncio.gather(apt_selection_snapshot(pc1_executor), apt_selection_snapshot(pc2_executor))
    shared = sorted(
        name
        for name in source_manual & target_manual
        if name not in exclude
        and name not in source_held
        and name not in target_held
        and name in source_versions
        and source_versions[name] == target_versions.get(name)
    )
    assert shared, (
        "No package is manually installed at the same version on both machines and held on neither. The two VMs "
        "come from one baseline, so an empty result means the machines are not what these tests assume."
    )
    return shared[0]


# Small archive packages a stock Ubuntu 24.04 does not install, for the hold that names a
# package its machine does not have. Several, because the one requirement is that apt knows
# the name and dpkg does not have it, and which of them satisfies that is the machine's
# business rather than this module's.
UNINSTALLED_ARCHIVE_CANDIDATES = ("cowsay", "sl", "toilet", "fortune-mod")


async def a_name_apt_knows_the_machine_does_not_have(executor: BashLoginRemoteExecutor) -> str:
    """A package `UNINSTALLED_ARCHIVE_CANDIDATES` names that this machine's apt can resolve
    and dpkg does not have installed -- the only state `apt-mark hold` can be given to
    produce a hold that freezes nothing.
    """
    result = await executor.run_command(
        f"apt-cache policy {' '.join(UNINSTALLED_ARCHIVE_CANDIDATES)}", login_shell=False, timeout=30.0
    )
    assert result.success, f"Failed to read apt's policy for the hold candidates: {result.stderr}"

    facts: dict[str, dict[str, str]] = {}
    current = ""
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if line and not line[0].isspace() and stripped.endswith(":"):
            current = stripped[:-1]
            facts[current] = {}
            continue
        for field in ("Installed:", "Candidate:"):
            if current and stripped.startswith(field):
                facts[current][field] = stripped.removeprefix(field).strip()

    for name in UNINSTALLED_ARCHIVE_CANDIDATES:
        block = facts.get(name, {})
        if block.get("Installed:") == "(none)" and block.get("Candidate:", "(none)") != "(none)":
            return name
    raise AssertionError(
        f"None of {list(UNINSTALLED_ARCHIVE_CANDIDATES)} is both known to apt and absent from dpkg on this "
        f"machine, so no hold naming a package it does not have can be set up.\n{result.stdout}"
    )


# `snap:hold:<name>` has no `SnapHoldItem` dataclass to build the id from -- `snap_sync`
# constructs the `ItemDiff` inline (02-208-HOLD-MASK-REPLICATION.md's own deviation note),
# so the literal shape is restated here exactly as `_diff_snap_holds` emits it.
def snap_hold_item_id(name: str) -> str:
    return f"snap:hold:{name}"


# The directory the install-snippet registry lives in, derived from the relpath
# `packages.state` owns rather than restated, so moving the registry moves this with it.
REGISTRY_DIR_RELPATH = SNIPPET_REGISTRY_RELPATH.rsplit("/", 1)[0]


# ---------------------------------------------------------------------------------
# Three things only a real apt settles: what a removal takes with it, which repository
# wins a candidate, and what `apt-mark` records. Every subject below is BUILT, for the
# reason `test_package_sync.py`'s docstring gives for the snap and flatpak ones -- two VMs
# provisioned from one baseline hold no package pair with the dependency a cascade needs, and no
# vendor repository at all.
# ---------------------------------------------------------------------------------

SYNTHETIC_PACKAGE_VERSION = "1.0"


def synthetic_control(name: str, *, version: str = SYNTHETIC_PACKAGE_VERSION, depends: str = "") -> str:
    """A `DEBIAN/control` stanza for an empty package built on a VM.

    Empty on purpose -- control metadata and no files -- so installing one changes nothing
    about the machine beyond dpkg's own records. `Depends:` is the only field that makes two
    of them behave like a real dependency pair.
    """
    dependency = f"Depends: {depends}\n" if depends else ""
    return (
        f"Package: {name}\nVersion: {version}\nArchitecture: all\n{dependency}"
        "Maintainer: pc-switcher integration tests <nobody@example.invalid>\n"
        "Description: synthetic package for pc-switcher integration tests\n"
    )


def packages_index_stanza(name: str, control: str) -> tuple[str, str]:
    """The two shell lines that append one package's stanza to a flat repository's
    `Packages` index: the control fields, then the `Filename`/`Size`/`SHA256` apt needs to
    fetch and verify the `.deb`.

    Size and digest are computed on the machine from the file `dpkg-deb` has just produced:
    apt rejects an index whose digest does not match the file it points at.
    """
    return (
        f"printf %s {shlex.quote(control)}",
        f'printf "Filename: ./{name}.deb\\nSize: %s\\nSHA256: %s\\n\\n"'
        f' "$(stat --format=%s "$work/{name}.deb")"'
        f' "$(sha256sum "$work/{name}.deb" | cut --delimiter=" " --fields=1)"',
    )


async def publish_a_cascading_pair(executor: BashLoginRemoteExecutor) -> tuple[str, str, str, str]:
    """On `executor`: publish two packages in a flat `file:` apt repository -- `dependent`
    declaring `Depends:` on `base` -- install both from it and mark both manual. Returns
    `(base, dependent, repo_dir, list_filename)`.

    From a REPOSITORY and never `dpkg --install`: a package apt names no origin for is
    dropped from the manifest and offered for removal in no direction
    (`PKG-FR-DEB-OWNERSHIP`), so a hand-`.deb` pair would produce no removal item at all.
    Both end up manually installed, which is what makes each of them a removal candidate on
    a machine the source does not have them on AND puts each inside `Collateral.protected()`.

    `apt-get remove <base>` then genuinely takes `dependent` with it. That is apt's own
    resolution, and it is the whole of what this construction exists to put in front of the
    job.
    """
    uniq = uuid4().hex[:12]
    base = f"pcswitcher-it-base-{uniq}"
    dependent = f"pcswitcher-it-dependent-{uniq}"
    repo_dir = f"/opt/pcswitcher-it-cascade-{uniq}"
    list_filename = f"pcswitcher-it-cascade-{uniq}.list"
    base_control = synthetic_control(base)
    dependent_control = synthetic_control(dependent, depends=base)

    build = "\n".join(
        (
            "set -euo pipefail",
            "work=$(mktemp --directory)",
            f'mkdir --parents "$work/{base}/DEBIAN" "$work/{dependent}/DEBIAN"',
            f'printf %s {shlex.quote(base_control)} > "$work/{base}/DEBIAN/control"',
            f'printf %s {shlex.quote(dependent_control)} > "$work/{dependent}/DEBIAN/control"',
            f'dpkg-deb --build "$work/{base}" "$work/{base}.deb" > /dev/null',
            f'dpkg-deb --build "$work/{dependent}" "$work/{dependent}.deb" > /dev/null',
            f"sudo mkdir --parents {shlex.quote(repo_dir)}",
            f'sudo cp "$work/{base}.deb" "$work/{dependent}.deb" {shlex.quote(repo_dir)}/',
            "{",
            *packages_index_stanza(base, base_control),
            *packages_index_stanza(dependent, dependent_control),
            f"}} | sudo tee {shlex.quote(f'{repo_dir}/Packages')} > /dev/null",
            f"printf '%s\\n' {shlex.quote(f'deb [trusted=yes] file:{repo_dir} ./')}"
            f" | sudo tee {shlex.quote(f'{APT_SOURCES_DIR}/{list_filename}')} > /dev/null",
        )
    )
    built = await executor.run_command(build, login_shell=False, timeout=60.0)
    assert built.success, f"Failed to build the cascading pair's repository: {built.stderr}"

    updated = await apt_get_update_for(executor, f"{APT_SOURCES_DIR}/{list_filename}")
    assert updated.success, f"apt-get update failed after adding {repo_dir}: {updated.stderr}"

    installed = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes "
        f"{shlex.quote(base)} {shlex.quote(dependent)} "
        f"&& sudo apt-mark manual {shlex.quote(base)} {shlex.quote(dependent)}",
        login_shell=False,
        timeout=120.0,
    )
    assert installed.success, f"Failed to install the cascading pair from {repo_dir}: {installed.stderr}"

    # The cascade itself, asserted rather than assumed: without it the run below has no
    # collateral to ask about and every assertion after it would pass vacuously.
    rehearsal = await executor.run_command(
        f"apt-get --dry-run remove --assume-yes {shlex.quote(base)}", login_shell=False, timeout=60.0
    )
    assert rehearsal.success and dependent in rehearsal.stdout, (
        f"removing {base} does not take {dependent} with it, so there is no cascade to ask about.\n"
        f"stdout: {rehearsal.stdout}\nstderr: {rehearsal.stderr}"
    )
    return base, dependent, repo_dir, list_filename


def collateral_removal_item_id(package: str) -> str:
    """The item id a removal's cascade over `package` produces
    (`apt:collateral:remove:remove:<package>`), built from the shipped function so a change
    to the identity fails here rather than silently answering nothing.
    """
    return collateral_item_id("remove", "remove", package)


# -- the apt origin model: one real vendor repository, and a rival for its candidate ----
#
# GitHub's CLI repository, and not a locally built stand-in, for the reason the module
# docstring gives for using the real Flathub: a stand-in only ever tests this project's
# MODEL of a repository. Among the vendor repositories a CI machine can reach it is the
# cheapest real one -- a single ~15 MB package, one keyring served over HTTPS from the same
# host, a `stable main` suite that has not moved, and no package of that name anywhere in
# Ubuntu's own archive, so nothing about the machine's stock software changes. It is also
# the requirements' own worked example of a vendor origin (`gh` from `cli.github.com`).
VENDOR_REPO_URI = "https://cli.github.com/packages"
VENDOR_REPO_KEY_URL = "https://cli.github.com/packages/githubcli-archive-keyring.gpg"
VENDOR_REPO_HOST = "cli.github.com"
VENDOR_PACKAGE = "gh"


async def install_from_the_vendor_repository(executor: BashLoginRemoteExecutor) -> tuple[str, str]:
    """On `executor` (the source): declare the vendor repository with its real signing key
    and install `VENDOR_PACKAGE` from it. Returns `(source_filename, key_filename)`.

    A deb822 `.sources` naming the keyring in `Signed-By:`, which is the shape the derived
    write and the key copy both have to carry to the target for the install to be possible
    there at all. Filenames are uuid-suffixed so a fresh target provably lacks them and the
    divergence is exactly the one this builds.
    """
    uniq = uuid4().hex[:12]
    source_filename = f"pcswitcher-it-vendor-{uniq}.sources"
    key_filename = f"pcswitcher-it-vendor-{uniq}.gpg"
    key_dest = f"{APT_KEYRINGS_DIR}/{key_filename}"
    source_dest = f"{APT_SOURCES_DIR}/{source_filename}"

    declare = "\n".join(
        (
            "set -euo pipefail",
            f"sudo mkdir --parents {shlex.quote(APT_KEYRINGS_DIR)}",
            f"curl --fail --silent --show-error --location {shlex.quote(VENDOR_REPO_KEY_URL)}"
            f" | sudo tee {shlex.quote(key_dest)} > /dev/null",
            f"sudo chmod 0644 {shlex.quote(key_dest)}",
            f"printf 'Types: deb\\nURIs: %s\\nSuites: stable\\nComponents: main\\n"
            f"Architectures: %s\\nSigned-By: %s\\n'"
            f' {shlex.quote(VENDOR_REPO_URI)} "$(dpkg --print-architecture)" {shlex.quote(key_dest)}'
            f" | sudo tee {shlex.quote(source_dest)} > /dev/null",
        )
    )
    declared = await executor.run_command(declare, login_shell=False, timeout=60.0)
    assert declared.success, (
        f"Failed to declare {VENDOR_REPO_URI} on the source. It is fetched over the network with curl, so an "
        f"unreachable host or a missing curl reports itself here.\n{declared.stderr}"
    )

    updated = await apt_get_update_for(executor, source_dest)
    assert updated.success, f"apt-get update failed after adding {VENDOR_REPO_URI}: {updated.stderr}"

    installed = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends "
        f"{shlex.quote(VENDOR_PACKAGE)}",
        login_shell=False,
        timeout=300.0,
    )
    assert installed.success, f"Failed to install {VENDOR_PACKAGE} from {VENDOR_REPO_URI}: {installed.stderr}"
    return source_filename, key_filename


async def undeclare_the_vendor_repository(
    executor: BashLoginRemoteExecutor, source_filename: str, key_filename: str
) -> None:
    """Take the vendor repository's declaration and its signing key off `executor`'s machine.

    The two `rm`s and nothing else. `VENDOR_PACKAGE` stays wherever the run under test left
    it (`test_package_sync.py`'s docstring): what a later `apt-get update` pays for is the
    repository being configured, and purging a package installed from a repository this test
    declared undoes something nothing later reads.
    """
    await executor.run_command(
        f"sudo rm --force {shlex.quote(f'{APT_SOURCES_DIR}/{source_filename}')} "
        f"{shlex.quote(f'{APT_KEYRINGS_DIR}/{key_filename}')}",
        login_shell=False,
        timeout=15.0,
    )


async def publish_a_rival_candidate(executor: BashLoginRemoteExecutor) -> tuple[str, str, str]:
    """On `executor` (the target): make apt prefer somebody else's `VENDOR_PACKAGE` to the
    vendor's. Returns `(repo_dir, list_filename, pin_filename)`.

    Two mechanisms pointing the same way, because the point is that apt's own arithmetic
    decides this and the test must not depend on which half of it does:

    - a flat `file:` repository publishing the same NAME at a version far above anything the
      vendor ships, so the version comparison alone would already pick it;
    - a `preferences.d` pin dropping the vendor origin to priority 1, so the priority
      comparison picks it whatever priority apt assigns a `Release`-less flat repository.

    The pin is the target's own and the source has none, so a run offers it for deletion and
    nothing here approves that -- it survives the run it is set up for.
    """
    uniq = uuid4().hex[:12]
    repo_dir = f"/opt/pcswitcher-it-rival-{uniq}"
    list_filename = f"pcswitcher-it-rival-{uniq}.list"
    pin_filename = f"pcswitcher-it-demote-{uniq}"
    control = synthetic_control(VENDOR_PACKAGE, version="99.0")
    pin_body = f"Package: {VENDOR_PACKAGE}\nPin: origin {VENDOR_REPO_HOST}\nPin-Priority: 1\n"

    build = "\n".join(
        (
            "set -euo pipefail",
            "work=$(mktemp --directory)",
            f'mkdir --parents "$work/{VENDOR_PACKAGE}/DEBIAN"',
            f'printf %s {shlex.quote(control)} > "$work/{VENDOR_PACKAGE}/DEBIAN/control"',
            f'dpkg-deb --build "$work/{VENDOR_PACKAGE}" "$work/{VENDOR_PACKAGE}.deb" > /dev/null',
            f"sudo mkdir --parents {shlex.quote(repo_dir)}",
            f'sudo cp "$work/{VENDOR_PACKAGE}.deb" {shlex.quote(repo_dir)}/',
            "{",
            *packages_index_stanza(VENDOR_PACKAGE, control),
            f"}} | sudo tee {shlex.quote(f'{repo_dir}/Packages')} > /dev/null",
            f"printf '%s\\n' {shlex.quote(f'deb [trusted=yes] file:{repo_dir} ./')}"
            f" | sudo tee {shlex.quote(f'{APT_SOURCES_DIR}/{list_filename}')} > /dev/null",
            f"printf %s {shlex.quote(pin_body)}"
            f" | sudo tee {shlex.quote(f'{APT_PREFERENCES_DIR}/{pin_filename}')} > /dev/null",
        )
    )
    built = await executor.run_command(build, login_shell=False, timeout=60.0)
    assert built.success, f"Failed to publish the rival candidate on the target: {built.stderr}"

    updated = await apt_get_update_for(executor, f"{APT_SOURCES_DIR}/{list_filename}")
    assert updated.success, f"apt-get update failed after adding {repo_dir}: {updated.stderr}"

    # The rival is what the target would install today, before the vendor's repository has
    # been written there at all -- asserted so a run that refuses the install below is
    # refusing it for the reason this test is about.
    policy = await executor.run_command(
        f"apt-cache policy {shlex.quote(VENDOR_PACKAGE)}", login_shell=False, timeout=30.0
    )
    assert policy.success and repo_dir in policy.stdout, (
        f"the target's candidate for {VENDOR_PACKAGE} does not come from {repo_dir}.\n{policy.stdout}"
    )
    return repo_dir, list_filename, pin_filename


async def remove_the_rival_candidate(
    executor: BashLoginRemoteExecutor, repo_dir: str, list_filename: str, pin_filename: str
) -> None:
    """Undo `publish_a_rival_candidate`, every step unconditional.

    Kept whole where the purges around it are not (`test_package_sync.py`'s docstring): all of
    it is `rm`, and a repository and a pin left in `/etc/apt` change what apt answers on this
    machine for the rest of the run.
    """
    await executor.run_command(
        f"sudo rm --force --recursive {shlex.quote(repo_dir)} "
        f"{shlex.quote(f'{APT_SOURCES_DIR}/{list_filename}')} "
        f"{shlex.quote(f'{APT_PREFERENCES_DIR}/{pin_filename}')}; "
        f"sudo rm --force /var/lib/apt/lists/_opt_{repo_dir.rsplit('/', 1)[-1]}_*",
        login_shell=False,
        timeout=60.0,
    )


# The two files ADR-020 D-38 gates on the target's Ubuntu Pro attachment, with the real
# stanzas `pro enable` writes. Their `Signed-By:` keyrings ship with `ubuntu-pro-client`
# on every Ubuntu 24.04, attached or not, so this is the file set a genuinely attached
# source carries — not an approximation of it.
ESM_SOURCE_BODIES = {
    "ubuntu-esm-apps.sources": (
        "Types: deb\n"
        "URIs: https://esm.ubuntu.com/apps/ubuntu\n"
        "Suites: noble-apps-security noble-apps-updates\n"
        "Components: main\n"
        "Signed-By: /usr/share/keyrings/ubuntu-pro-esm-apps.gpg\n"
    ),
    "ubuntu-esm-infra.sources": (
        "Types: deb\n"
        "URIs: https://esm.ubuntu.com/infra/ubuntu\n"
        "Suites: noble-infra-security noble-infra-updates\n"
        "Components: main\n"
        "Signed-By: /usr/share/keyrings/ubuntu-pro-esm-infra.gpg\n"
    ),
}


# How long to wait for a running sync to engage its sync-window hold, and how often to look.
# The poll is what makes "inside the window" a measurement rather than a guess about how
# long the steps before RUN_JOBS take on a given machine.
HOLD_POLL_TIMEOUT_SECONDS = 180.0
HOLD_POLL_INTERVAL_SECONDS = 0.5

# `pkill --full` matches on the whole command line, and the shell that RUNS pkill has the
# pattern in its own. The bracket makes the two differ: this regex matches `pc-switcher
# sync`, and the literal text `pc-switcher[ ]sync` sitting in the shell's command line does
# not match it — so the kill reaches the sync and not the shell asking for it.
KILL_RUNNING_SYNC_CMD = "pkill --signal KILL --full 'pc-switcher[ ]sync'"

# How far ahead of the writing machine's own clock the sync-window suspension lapses, and
# how much of that may already have elapsed by the time the value is read back. Restated
# rather than imported, exactly as `SYSTEM_REFRESH_HOLD_SET_CMD` above is: the point is
# that the value snapd holds IS a near-future instant, which a test agreeing with whatever
# the orchestrator's private constant currently says would not assert.
SNAP_HOLD_EXPECTED_DURATION = timedelta(hours=6)
SNAP_HOLD_DURATION_SLACK = timedelta(minutes=15)


# ---------------------------------------------------------------------------------
# The whole happy path, as one seeded divergence per manager and the assertions that read
# it back. Three modules drive it: `test_dry_run.py` rehearses it, `test_end_to_end_sync.py`
# converges it and then converges the reverse direction over the same pair. Keeping the
# seeds and the witnesses here is what lets each of those hold its own spine -- config, the
# runs, and one call per claim -- instead of two thousand lines of package detail.
# ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ConvergenceSeed:
    """What `seed_a_divergence_in_every_manager` picked and built, named back for the
    assertions that follow the run.

    Every field is a machine's own answer rather than a constant: which apt packages were
    free to move, which revision the source's snap is actually on, which ref and remote the
    fixture app reports, and which uuid-suffixed paths this seeding built. A witness that
    re-derived any of them would be reading the same machine twice instead of comparing the
    run against what was set up.
    """

    install_candidate: str
    removal_candidate: str
    #: A package both machines have, which the reverse direction removes from the target
    #: after the user undoes it on the machine that becomes the source. Distinct from
    #: `install_candidate`, which the reverse run needs still converged to witness a
    #: fixed point with.
    round_trip_candidate: str
    hold_subject: str
    revision_snap: str
    source_snap_revision: str
    hold_snap: str
    application: str
    scope: Literal["user", "system"]
    remote_name: str
    ref: str
    ref_item_id: str
    filter_path: str
    recorded_filter_path: str
    target_only_remote: str
    target_only_keyring: str
    unowned_path: str
    manual_item_id: str
    replay_marker: str
    source_filename: str
    key_filename: str
    pin_filename: str
    prior_source_hold: str | None
    prior_target_hold: str | None

    @property
    def scope_flag(self) -> str:
        """`--user` or `--system`, the flag every `flatpak` call about this ref needs."""
        return "--user" if self.scope == "user" else "--system"

    @property
    def sudo(self) -> str:
        """`sudo ` for a system-scope ref, empty for a user-scope one."""
        return "sudo " if self.scope == "system" else ""

    @property
    def source_dest(self) -> str:
        """Where the synthetic repository's declaration sits on the machine that has it."""
        return f"{APT_SOURCES_DIR}/{self.source_filename}"

    @property
    def key_dest(self) -> str:
        """Where the synthetic repository's signing key sits."""
        return f"{APT_KEYRINGS_DIR}/{self.key_filename}"

    @property
    def pin_dest(self) -> str:
        """Where the always-sync pin sits."""
        return f"{APT_PREFERENCES_DIR}/{self.pin_filename}"

    @property
    def registry_relpath(self) -> str:
        """The snippet registry, as a `~`-relative path both machines resolve for themselves."""
        return f"~/{SNIPPET_REGISTRY_RELPATH}"

    def approve_everything(self) -> dict[str, Decision]:
        """APPLY for every item this divergence raises, one per manager.

        The two `/etc/apt` files are absent on purpose: neither is decidable, and a run that
        wrote one because something else was ticked is the defect the rehearsal looks for.
        """
        return {
            AptPackageItem(name=self.install_candidate, version="").item_id: Decision.APPLY,
            AptPackageItem(name=self.removal_candidate, version="").item_id: Decision.APPLY,
            f"snap:{self.revision_snap}": Decision.APPLY,
            snap_hold_item_id(self.hold_snap): Decision.APPLY,
            self.ref_item_id: Decision.APPLY,
            self.manual_item_id: Decision.APPLY,
        }


async def seed_a_divergence_in_every_manager(  # noqa: PLR0915
    source: BashLoginRemoteExecutor, target: BashLoginRemoteExecutor, apt: AptSubjects
) -> ConvergenceSeed:
    """Put one divergence per manager between `source` and `target`, and hold snapd still.

    - apt: a package removed from the target (install direction), a package removed from the
      source (removal direction), a hold set on the source for a package both machines have
      at the same version, and a synthetic vendor repository, signing key and always-sync pin
      written to the source's `/etc/apt`;
    - snap: the target's first fixture snap moved to another revision, and a per-snap hold set
      on the source's second one;
    - flatpak: the fixture app and its remote deleted from the target, a ref filter applied to
      the source's copy of that remote, and a uuid-named remote added to the target that the
      source does not have;
    - manual installs: an unowned `/opt` path on the source with a snippet authored against it.

    Converges to those preconditions rather than assuming them: by the time a module using
    this runs, an earlier one may already have converged the same pair.

    Both machines' snapd auto-refresh is suspended for the duration -- the same timed
    `refresh.hold` a sync engages -- so a background refresh cannot move `snap list` between
    two captures and be misread as a run's doing. `restore_after_the_divergence` puts the
    prior value back.
    """
    _ = await asyncio.gather(assert_flatpak_available(source), assert_flatpak_available(target))

    install_candidate = apt.install_direction[0]
    round_trip_candidate = apt.install_direction[1]
    removal_candidate = apt.removal_direction
    hold_subject = apt.hold

    revision_snap, hold_snap = await snap_subjects(source, target, count=2)
    source_snap_revision, target_snap_revision = await asyncio.gather(
        snap_revision(source, revision_snap), snap_revision(target, revision_snap)
    )
    assert source_snap_revision and target_snap_revision, f"{revision_snap} is not installed on both machines"
    alternate_revision = await alternate_snap_revision(target, revision_snap, source_snap_revision)

    application, version, scope, remote_name, _remote_url, ref = await flatpak_subject(source)
    scope_flag = "--user" if scope == "user" else "--system"
    sudo = "sudo " if scope == "system" else ""
    ref_item_id = FlatpakItem(
        application=application, version=version, origin=remote_name, scope=scope, ref=ref
    ).item_id

    uniq = uuid4().hex[:12]
    unowned_path = f"/opt/pcswitcher-it-converge-{uniq}"
    # Home-relative marker so the snippet needs no sudo: replay runs `bash -c <body>` as the
    # SSH user on the target, and $HOME expands there.
    replay_marker = f"$HOME/.cache/pcswitcher-it-converge-{uniq}"
    # Unquoted `$HOME` on purpose: the remote shell expands it, and flatpak stores whatever
    # path it is given verbatim. `flatpak_remote_filter` reads the expanded path back off the
    # machine, which is the one both machines must end up naming.
    filter_path = f"$HOME/.cache/pcswitcher-it-flatpak-filter-{uniq}"
    target_only_remote = f"pcswitcher-it-vendor-{uniq}"
    target_only_keyring = f"$HOME/.local/share/flatpak/repo/{target_only_remote}.trustedkeys.gpg"

    prior_source_hold, prior_target_hold = await asyncio.gather(
        capture_system_refresh_hold(source), capture_system_refresh_hold(target)
    )
    _ = await asyncio.gather(engage_system_refresh_hold(source), engage_system_refresh_hold(target))

    # -- apt: one chain per machine, run at once. apt writes on ONE machine serialise on
    # dpkg's own lock, but the two machines' apt work is independent and this is several
    # transactions on each (#216).
    async def seed_source_apt() -> CommandResult:
        await ensure_installed_and_manual(source, install_candidate)
        await ensure_absent(source, removal_candidate)
        await ensure_installed_and_manual(source, hold_subject)
        return await source.run_command(
            f"sudo apt-mark hold {shlex.quote(hold_subject)}", login_shell=False, timeout=30.0
        )

    async def seed_target_apt() -> None:
        await ensure_absent(target, install_candidate)
        await ensure_installed_and_manual(target, removal_candidate)
        await ensure_installed_and_manual(target, hold_subject)

    held, _ = await asyncio.gather(seed_source_apt(), seed_target_apt())
    assert held.success, f"Failed to hold {hold_subject} on the source: {held.stderr}"

    source_filename, key_filename = await create_synthetic_repo_and_key(source)
    pin_filename = await create_synthetic_pin(source)
    absent = await target.run_command(
        " && ".join(
            f"test ! -e {shlex.quote(path)}"
            for path in (
                f"{APT_SOURCES_DIR}/{source_filename}",
                f"{APT_KEYRINGS_DIR}/{key_filename}",
                f"{APT_PREFERENCES_DIR}/{pin_filename}",
            )
        ),
        login_shell=False,
        timeout=10.0,
    )
    assert absent.success, "synthetic /etc/apt files unexpectedly already present on the target before the run"

    # -- snap: one snapd change on each machine, and neither snapd knows about the other's.
    diverged, snap_held = await asyncio.gather(
        target.run_command(
            f"sudo snap refresh --revision={shlex.quote(alternate_revision)} {shlex.quote(revision_snap)}",
            login_shell=False,
            timeout=180.0,
        ),
        source.run_command(
            f"sudo snap refresh --hold=forever {shlex.quote(hold_snap)}", login_shell=False, timeout=60.0
        ),
    )
    assert diverged.success, (
        f"Failed to move the target's {revision_snap} to revision {alternate_revision}: {diverged.stderr}"
    )
    assert snap_held.success, f"Failed to set a per-snap hold on the source's {hold_snap}: {snap_held.stderr}"
    assert "held" not in await snap_notes(target, hold_snap), (
        f"{hold_snap} is already held on the target before the run; its replication would prove nothing"
    )

    # -- flatpak: the source's remote table is read while the target gives up the app and the
    # remote. Deleting the remote is what removes the target's only trust in Flathub and makes
    # the key replication load-bearing.
    source_remotes, _dropped_on_target = await asyncio.gather(
        source.run_command(f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0),
        target.run_command(
            f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(application)} || true; "
            f"{sudo}flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true",
            login_shell=False,
            timeout=120.0,
        ),
    )
    assert FIXTURE_UNUSED_FLATPAK_REMOTE in nonblank_lines(source_remotes.stdout), (
        f"the fixture remote {FIXTURE_UNUSED_FLATPAK_REMOTE} is not configured on the source. It is created by "
        f"tests/integration/scripts/internal/vm-test-fixtures.sh.\n{source_remotes.stdout}"
    )
    filtered = await source.run_command(
        f"mkdir --parents $HOME/.cache && printf %s {shlex.quote(FLATPAK_FILTER_BODY)} > {filter_path} && "
        f"{sudo}flatpak remote-modify {scope_flag} --filter={filter_path} {shlex.quote(remote_name)}",
        login_shell=False,
        timeout=30.0,
    )
    assert filtered.success, (
        f"`flatpak remote-modify {scope_flag} --filter=` failed on the source, so there is no filtered remote to "
        f"replicate: {filtered.stderr}"
    )
    # Two read-only views of the same remote table, taken at once.
    (_source_url, source_options), recorded = await asyncio.gather(
        flatpak_remote_row(source, remote_name, scope), flatpak_remote_filter(source, remote_name, scope)
    )
    assert FLATPAK_FILTERED_OPTION in source_options, (
        f"the source's {remote_name} reports options {source_options!r} after `flatpak remote-modify {scope_flag} "
        f"--filter=`, so this flatpak does not print the {FLATPAK_FILTERED_OPTION!r} token "
        "`flatpak_sync._FILTERED_OPTION` reads"
    )
    recorded_filter_path = recorded or ""
    assert recorded_filter_path, (
        f"the source's {remote_name} names no file in `flatpak remotes {scope_flag} --columns=filter`, so this "
        "flatpak does not record the path `flatpak_sync` replicates"
    )

    before_remotes_result, before_app_rows = await asyncio.gather(
        target.run_command(f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0),
        flatpak_app_rows(target),
    )
    before_remotes = nonblank_lines(before_remotes_result.stdout)
    assert remote_name not in before_remotes, (
        f"remote {remote_name} still configured on the target, so this run cannot show it being provisioned"
    )
    assert FIXTURE_UNUSED_FLATPAK_REMOTE not in before_remotes, (
        f"{FIXTURE_UNUSED_FLATPAK_REMOTE} is already on the target, so this run cannot show that it did not travel"
    )
    assert application not in [row[0] for row in before_app_rows], (
        f"{application} still installed on the target, so no approved application derives {remote_name}"
    )

    added = await target.run_command(
        f"flatpak remote-add {scope_flag} {shlex.quote(target_only_remote)} {shlex.quote(FIXTURE_FLATPAK_REPOFILE)}",
        login_shell=False,
        timeout=180.0,
    )
    assert added.success, f"could not add the target-only remote {target_only_remote}: {added.stderr}"
    key_before = await target.run_command(f"test -f {target_only_keyring}", login_shell=False, timeout=15.0)
    assert key_before.success, (
        f"the target holds no {target_only_keyring} for {target_only_remote}, so this flatpak does not keep a "
        "per-remote keyring and its absence after the deletion would prove nothing"
    )

    # -- manual installs
    await create_unowned_marker(source, unowned_path)
    manual_item_id = unowned_item_id(unowned_path)
    await author_snippet(
        source,
        manual_item_id,
        unowned_path,
        f'mkdir --parents "$(dirname {replay_marker})" && touch {replay_marker}',
    )

    return ConvergenceSeed(
        install_candidate=install_candidate,
        removal_candidate=removal_candidate,
        round_trip_candidate=round_trip_candidate,
        hold_subject=hold_subject,
        revision_snap=revision_snap,
        source_snap_revision=source_snap_revision,
        hold_snap=hold_snap,
        application=application,
        scope=scope,
        remote_name=remote_name,
        ref=ref,
        ref_item_id=ref_item_id,
        filter_path=filter_path,
        recorded_filter_path=recorded_filter_path,
        target_only_remote=target_only_remote,
        target_only_keyring=target_only_keyring,
        unowned_path=unowned_path,
        manual_item_id=manual_item_id,
        replay_marker=replay_marker,
        source_filename=source_filename,
        key_filename=key_filename,
        pin_filename=pin_filename,
        prior_source_hold=prior_source_hold,
        prior_target_hold=prior_target_hold,
    )


def assert_the_rehearsal_wrote_nothing(
    seed: ConvergenceSeed,
    before: MachinePackageState,
    after: MachinePackageState,
    run_output: str,
) -> None:
    """ADR-014 over the seeded divergence: the target's whole package state is byte-identical
    across the rehearsal, and the preview reports what a real run WOULD write.

    The always-sync pin is previewed as a derived write. The synthetic repository is not:
    nothing approved comes from it, so under derivation it neither travels nor becomes a
    review line, and its key is previewed only for the repositories that survive a run
    (`PKG-FR-DERIVED-VISIBLE`). The decisions passed in name only packages and refs, so a
    preview that wrote either `/etc/apt` file because something else was ticked is the defect.
    """
    assert after == before, (
        f"--dry-run changed the target's package-manager state (ADR-014 violation).\nbefore: {before}\nafter: {after}"
    )

    previewed = collapse_run_output(run_output)
    assert f"Would write {seed.pin_dest}" in previewed, (
        f"always-sync pin {seed.pin_dest!r} was not previewed as a derived write.\n{run_output}"
    )
    assert f"install {seed.source_filename}" not in previewed, (
        f"repository {seed.source_filename!r} was still offered as a review entry.\n{run_output}"
    )
    assert f"Would write {seed.source_dest}" not in previewed, (
        f"repository {seed.source_dest!r} was previewed as a derived write although no approved package needs it.\n"
        f"{run_output}"
    )
    assert f"Would write signing key {seed.key_dest}" not in previewed, (
        f"signing key {seed.key_dest!r} was previewed as a write for a repository no package needed.\n{run_output}"
    )
    # The intended metadata refresh (the apt-get update the pin write requires) is reported as
    # its own marker item, by the label that item carries.
    assert "Would change Refresh apt package metadata (apt-get update)" in previewed, (
        f"intended apt-get update (metadata refresh) not reported.\n{run_output}"
    )


async def assert_every_manager_converged(
    source: BashLoginRemoteExecutor,
    target: BashLoginRemoteExecutor,
    seed: ConvergenceSeed,
    run_output: str,
    source_before: MachinePackageState,
) -> None:
    """Every manager's half of the seeded divergence, read off the target's own package
    managers and filesystem -- and the source's own state, which a run must not move.

    apt installs what the target lost, removes what the source lost, and registers the
    source's hold with no review line of its own (`PKG-FR-BLOCKS-DERIVED`). snap lands the
    target on the source's revision without either machine's `refresh.hold` moving (D-06),
    and the source's per-snap hold reaches the target's `snap list` Notes through the very
    window the orchestrator holds snapd in (#208 D9). flatpak provisions the remote BEFORE
    installing the ref that needs it (D-14), carrying the real signing key (#215) and the
    source's ref filter, deletes the target-only remote together with its keyring, and leaves
    the unused remote -- which no approved ref comes from -- on the source alone. manual
    installs pushes the registry and replays the snippet in the same run (D-23).

    The source's own `MachinePackageState` is identical across all of it
    (`PKG-FR-SOURCE-INTENT`): a run that genuinely installs, removes and re-revisions on the
    target changes nothing about what software the source has, nor where it gets it from.
    """
    collapsed = collapse_run_output(run_output)

    target_manual = nonblank_lines(
        (await target.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
    )
    assert seed.install_candidate in target_manual, (
        f"{seed.install_candidate} not reinstalled on the target after the sync.\n{run_output}"
    )
    assert seed.removal_candidate not in target_manual, (
        f"{seed.removal_candidate} was not removed from the target, so the removal direction converged nothing.\n"
        f"{run_output}"
    )
    target_holds = await target.run_command("apt-mark showhold", login_shell=False, timeout=15.0)
    assert seed.hold_subject in nonblank_lines(target_holds.stdout), (
        f"the source's hold on {seed.hold_subject} did not reach the target, although a block replicates without "
        f"review.\n{run_output}"
    )
    assert f"reviewed {seed.hold_subject} (hold)" not in collapsed, (
        f"the hold on {seed.hold_subject} was presented as a reviewed item -- a block is never a question"
    )

    assert await snap_revision(target, seed.revision_snap) == seed.source_snap_revision, (
        f"the target's {seed.revision_snap} did not converge to source revision {seed.source_snap_revision}.\n"
        f"{run_output}"
    )
    replicated_notes = await snap_notes(target, seed.hold_snap)
    assert "held" in replicated_notes, (
        f"the source's per-snap hold on {seed.hold_snap} did not reach the target (target notes: "
        f"{sorted(replicated_notes)}). If the source capture ran inside the orchestrator's system-wide refresh.hold "
        "window and saw no hold, #208 D9's capture-timing assumption is false and the capture must move earlier."
    )
    source_hold_after, target_hold_after = await asyncio.gather(
        capture_system_refresh_hold(source), capture_system_refresh_hold(target)
    )
    for hold_after, machine in ((source_hold_after, "the source"), (target_hold_after, "the target")):
        assert hold_after is not None, (
            f"the run left {machine} without the refresh.hold this scenario engaged -- D-06 forbids the convergence "
            "mechanism from touching either machine's auto-refresh policy"
        )

    after_remotes = nonblank_lines(
        (
            await target.run_command(
                f"flatpak remotes {seed.scope_flag} --columns=name", login_shell=False, timeout=15.0
            )
        ).stdout
    )
    assert seed.remote_name in after_remotes, (
        f"remote {seed.remote_name} not provisioned in scope {seed.scope} on the target after sync"
    )
    assert FIXTURE_UNUSED_FLATPAK_REMOTE not in after_remotes, (
        f"{FIXTURE_UNUSED_FLATPAK_REMOTE} travelled to the target although no approved ref comes from it"
    )
    assert seed.target_only_remote not in after_remotes, (
        f"{seed.target_only_remote} is still configured on the target, so the source-lacks-it deletion never "
        f"happened.\n{run_output}"
    )
    key_after = await target.run_command(f"test -f {seed.target_only_keyring}", login_shell=False, timeout=15.0)
    assert not key_after.success, (
        f"{seed.target_only_keyring} survived the deletion of {seed.target_only_remote}: the target still trusts "
        "that vendor's signing key for a remote it no longer has"
    )
    assert seed.application in [row[0] for row in await flatpak_app_rows(target)], (
        f"{seed.application} not installed in scope {seed.scope} on the target after sync.\n{run_output}"
    )
    _target_url, target_options = await flatpak_remote_row(target, seed.remote_name, seed.scope)
    assert FLATPAK_FILTERED_OPTION in target_options, (
        f"the target's provisioned {seed.remote_name} reports options {target_options!r}: the source's ref filter "
        f"was not applied there.\n{run_output}"
    )
    assert await flatpak_remote_filter(target, seed.remote_name, seed.scope) == seed.recorded_filter_path, (
        f"the target's {seed.remote_name} does not name {seed.recorded_filter_path} as its ref filter -- the file "
        "must land at the same absolute path the source records"
    )
    copied = await target.run_command(f"cat {shlex.quote(seed.recorded_filter_path)}", login_shell=False, timeout=15.0)
    assert copied.success and copied.stdout == FLATPAK_FILTER_BODY, (
        f"the target's copy of the ref filter at {seed.recorded_filter_path} is not the source's file byte-for-byte: "
        f"{copied.stdout!r} ({copied.stderr})"
    )
    # The one ordering exception the package suite's own prohibition carves out: the remote's
    # mere presence afterwards does not distinguish "remote added before ref" from any other
    # order, so only the run's own per-item converge log (`PackageSyncJob._converge_one`)
    # proves it.
    remote_marker = f"provision {seed.scope} flatpak remote {seed.remote_name}"
    ref_marker = f"install {seed.ref} ("
    remote_index = run_output.find(remote_marker)
    ref_index = run_output.find(ref_marker)
    assert remote_index != -1, f"derived remote write log line not found: {remote_marker!r}"
    assert ref_index != -1, f"ref converge log line not found: {ref_marker!r}"
    assert remote_index < ref_index, "remote must be provisioned before the ref installs (D-14)"

    registry_exists = await target.run_command(f"test -f {seed.registry_relpath}", login_shell=False, timeout=10.0)
    assert registry_exists.success, (
        f"snippet registry not present on the target at {seed.registry_relpath} after the run -- the push did not land"
    )
    replayed = await target.run_command(f"test -f {seed.replay_marker}", login_shell=False, timeout=10.0)
    assert replayed.success, (
        f"marker {seed.replay_marker} absent on the target -- the pushed snippet was not replayed.\n{run_output}"
    )

    source_after = await capture_machine_package_state(source)
    assert source_after == source_before, (
        "the run changed the source's own package state: a sync must not change what software the source has, nor "
        f"where it gets it from.\nbefore: {source_before}\nafter: {source_after}"
    )


def back_direction_decisions(seed: ConvergenceSeed) -> dict[str, Decision]:
    """What the reverse-direction run is told, once `seed_the_back_direction` has run.

    The round-trip package's removal and the ref are approved; the package the FORWARD run
    installed is mapped SKIP_ALWAYS, which is what makes "it was never presented" readable
    off the decision files. An APPLY there could not tell an item that is genuinely no longer
    a diff from an item that was never raised, whereas a SKIP_ALWAYS leaves a decision-file
    entry if and only if the item WAS presented.
    """
    return {
        AptPackageItem(name=seed.round_trip_candidate, version="").item_id: Decision.APPLY,
        AptPackageItem(name=seed.install_candidate, version="").item_id: Decision.SKIP_ALWAYS,
        seed.ref_item_id: Decision.APPLY,
    }


async def seed_the_back_direction(
    new_source: BashLoginRemoteExecutor, new_target: BashLoginRemoteExecutor, seed: ConvergenceSeed
) -> None:
    """Over the pair a converging run just left, set up the reverse direction: the user undoes
    an install on the machine that is about to become the source, and that machine drops the
    ref filter the forward run gave it.

    The app comes off the new target as well, and only the app: a filter is converged as part
    of writing the remote an approved ref DERIVES, so a reverse run over a fully converged
    pair would have no ref item, derive no remote, and leave the filter alone for a reason
    that has nothing to do with what the run is meant to show.
    """
    await ensure_installed_and_manual(new_target, seed.round_trip_candidate)
    await asyncio.gather(
        ensure_absent(new_source, seed.round_trip_candidate),
        restore_flatpak_source_baseline(new_source, seed.remote_name, seed.scope, seed.recorded_filter_path),
    )
    assert await flatpak_remote_filter(new_source, seed.remote_name, seed.scope) is None, (
        f"the new source's {seed.remote_name} still carries a ref filter, so the run cannot show a target-only one "
        "coming off"
    )
    await new_target.run_command(
        f"{seed.sudo}flatpak uninstall {seed.scope_flag} --assumeyes {shlex.quote(seed.application)}",
        login_shell=False,
        timeout=120.0,
    )
    assert await flatpak_remote_filter(new_target, seed.remote_name, seed.scope) == seed.recorded_filter_path, (
        f"the new target's {seed.remote_name} lost its ref filter before the reverse run started; there is no "
        "target-only filter to take off"
    )


async def assert_the_back_direction_converged(
    new_source: BashLoginRemoteExecutor,
    new_target: BashLoginRemoteExecutor,
    seed: ConvergenceSeed,
    before: MachinePackageState,
) -> None:
    """The reverse direction over the same pair: what the user undid comes back, a ref filter
    the source dropped comes off the target, and NOTHING else on the target moves.

    `before` is the new target's state as the forward run left it, so the expected state is
    that one minus the round-trip package: the app this scenario uninstalls between the runs
    is reinstalled by the run itself, and every other field must survive the round trip
    untouched. A run that rewrote `/etc/apt`, re-revisioned a snap or dropped a hold on the
    way past would be visible here and in nothing else.

    The package the FORWARD run installed is the fixed-point witness: mapped SKIP_ALWAYS and
    yet absent from both machines' decision files, which is state-based proof it was never
    presented -- a converged item produces no diff at all. It is scoped to that one item on
    purpose. Items still diverged between the two machines are legitimately presented again,
    which is not what idempotency promises.
    """
    manual = nonblank_lines(
        (await new_target.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
    )
    assert seed.round_trip_candidate not in manual, (
        f"{seed.round_trip_candidate} is still manually installed on the new target -- the removal did not propagate "
        "back across the reversed direction"
    )

    assert await flatpak_remote_filter(new_target, seed.remote_name, seed.scope) is None, (
        f"the new target's {seed.remote_name} still carries a ref filter the source does not have -- the two "
        "machines would never converge"
    )
    assert seed.application in [row[0] for row in await flatpak_app_rows(new_target)], (
        f"{seed.application} was not reinstalled on the new target by the reverse run"
    )

    converged_item = AptPackageItem(name=seed.install_candidate, version="").item_id
    source_entries, target_entries = await asyncio.gather(
        DecisionFile("apt", new_source).load(), DecisionFile("apt", new_target).load()
    )
    assert converged_item not in source_entries and converged_item not in target_entries, (
        f"{seed.install_candidate} was still presented in the reverse run's review (its SKIP_ALWAYS was recorded) "
        "-- an item the forward run converged must produce no diff at all"
    )

    after = await capture_machine_package_state(new_target)
    expected = replace(
        before,
        apt_manual=tuple(name for name in before.apt_manual if name != seed.round_trip_candidate),
        apt_installed=tuple(name for name in before.apt_installed if name != seed.round_trip_candidate),
    )
    assert after == expected, (
        "the reverse run moved something on the new target beyond the removal it was given.\n"
        f"expected: {expected}\nactual: {after}"
    )


async def restore_after_the_divergence(
    source: BashLoginRemoteExecutor, target: BashLoginRemoteExecutor, seed: ConvergenceSeed
) -> None:
    """Take back what a later scenario would otherwise inherit, and leave the package state
    where the runs put it.

    Holds, `/etc/apt` files, remotes, keyrings, markers and the replicated filter come off;
    which revision the target ends on and which packages the runs moved are nobody's
    precondition (`test_package_sync.py`'s module docstring, `snap_subjects`).
    """
    cleanup_paths = " ".join(
        shlex.quote(f"{directory}/{filename}")
        for directory, filename in (
            (APT_SOURCES_DIR, seed.source_filename),
            (APT_KEYRINGS_DIR, seed.key_filename),
            (APT_PREFERENCES_DIR, seed.pin_filename),
        )
        if filename
    )

    async def clean_the_source() -> None:
        await restore_flatpak_source_baseline(source, seed.remote_name, seed.scope, seed.filter_path)
        # `;`, so the apt hold comes off even when the snap one is already gone.
        await source.run_command(
            f"sudo snap refresh --unhold {shlex.quote(seed.hold_snap)}; "
            f"sudo apt-mark unhold {shlex.quote(seed.hold_subject)}",
            login_shell=False,
            timeout=90.0,
        )
        if cleanup_paths:
            await source.run_command(f"sudo rm --force {cleanup_paths}", login_shell=False, timeout=15.0)
        await remove_unowned_marker(source, seed.unowned_path)
        await source.run_command(f"rm --force {seed.registry_relpath}", login_shell=False, timeout=15.0)
        await restore_system_refresh_hold(source, seed.prior_source_hold)

    async def clean_the_target() -> None:
        if seed.recorded_filter_path:
            # The replicated copy is pc-switcher's own write and lives outside anything
            # `restore_flatpak_target_baseline` knows about.
            await target.run_command(
                f"rm --force {shlex.quote(seed.recorded_filter_path)}", login_shell=False, timeout=15.0
            )
        await target.run_command(
            f"flatpak remote-delete {seed.scope_flag} --force {shlex.quote(seed.target_only_remote)} || true; "
            f"rm --force {seed.target_only_keyring}",
            login_shell=False,
            timeout=60.0,
        )
        await restore_flatpak_target_baseline(target)
        # `;`, so the apt hold comes off even when the snap one is already gone.
        await target.run_command(
            f"sudo snap refresh --unhold {shlex.quote(seed.hold_snap)}; "
            f"sudo apt-mark unhold {shlex.quote(seed.hold_subject)}",
            login_shell=False,
            timeout=90.0,
        )
        if cleanup_paths:
            await target.run_command(f"sudo rm --force {cleanup_paths}", login_shell=False, timeout=15.0)
        await target.run_command(
            f"rm --force {seed.replay_marker} {seed.registry_relpath}", login_shell=False, timeout=15.0
        )
        await restore_system_refresh_hold(target, seed.prior_target_hold)

    await cleanup_in_parallel(clean_the_source(), clean_the_target())
