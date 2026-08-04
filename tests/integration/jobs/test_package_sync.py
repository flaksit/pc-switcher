"""Integration tests proving the tracer's end-to-end apt_sync path against real VMs.

`apt_sync` (plan 02-03) claims that a package missing on the target travels source
capture -> target query -> diff -> apt_sync's own batched review (each manager reviews
its own diffs inside its own `execute()`, per the corrected D-24; there is no
cross-manager coordinator) -> `apt-get install` on the target. Plan 02-03's own unit
tests only prove that shape against a mocked executor; this module is the VM-level proof
against real apt/dpkg/sudo.

The tests drive each manager's review non-interactively through
`PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` (D-26's hidden test hook,
`jobs.packages.review`) rather than through a real TTY, and assert against the target's
own package-manager or filesystem state (`apt-mark showmanual`, `/etc/apt`, `snap list`,
the pushed snippet registry) -- never against pc-switcher's log text -- except where an
explicit witness legitimately needs the run's own output: the apt-repository-state
dry-run test, whose subject IS the review output because a rehearsal makes no filesystem
change to assert against; the flatpak ORIGIN_MISMATCH test, for the same reason (a
REPORT_ONLY diff changes nothing anywhere); and the non-interactive skip-all test, whose
subject includes what a run with nobody to ask must SAY. Those read the output through
`_collapse_run_output`, which is where the wrapping every Rich renderer applies is dealt
with once, and each matches a whole phrase the code owns rather than a bare name: the log
records every command's own output verbatim at DEBUG (`PKG-FR-LOG-VERBATIM`) and every
config here sets `tui: DEBUG`, so a filename appearing SOMEWHERE in a run is no evidence
of anything. `apt-cache rdepends` output is also read to pick a safe removal candidate
before either machine's package state is touched.

The classes below are SCENARIOS, not one claim each. A `pc-switcher sync` costs 30-40s of
wall clock whatever it converges, so the number of runs is the whole cost of this module
(#216) -- and claims that want the same shape of run share one. Each class states which
premise its claims share, and each test's docstring lists every contract id it settles, so
what is proven stays traceable to
docs/dev/package-sync-scenario-coverage.md. Four shapes recur:

- a converging run over one seeded divergence PER MANAGER, rehearsed first and re-run
  afterwards (`TestOneRunConvergesEveryManager`);
- a run with nobody to ask, which applies nothing and is the only run that PRINTS every
  review group (`TestARunWithNobodyToAsk`, `TestWhatFolderSyncMayAndMayNotCarry`);
- a run sequence across both directions, carrying the removal-shaped claims that need a
  real `apt-get remove` (`TestCrossDirectionRoundTrips`, `TestSkipAlwaysIsInertInBothRoles`);
- runs that FAIL, ABORT or are KILLED, which no converging run can carry and which
  therefore keep a sync each.

The pure parsing/selection helpers below (`nonblank_lines`, `parse_dpkg_installed`,
`parse_reverse_depends`, `parse_batched_rdepends`, `pick_safe_removal_candidate`) have no
I/O of their own and are unit-tested directly in
`tests/unit/jobs/test_package_sync_candidate_selection.py`, independent of VM access.

Subjects: every test here needs a package it may hold, diverge, remove and reinstall, and
a stock Ubuntu 24.04 VM offers none for snap (only `_SNAP_REMOVAL_DENYLIST` members) and
none at all for flatpak (which is not installed). Those subjects are therefore CREATED --
`tests/integration/scripts/internal/vm-test-fixtures.sh`, baked into the baseline snapshot
by provisioning and re-applied by the module-scoped `vm_test_fixtures` fixture. No test in
this module declines to run for want of a subject: a missing subject is a broken machine
and fails naming what is missing and which script creates it. apt subjects are still
selected by querying the machines (any Debian system has hundreds), but once for the whole
module rather than per test (`apt_subjects`, `_AptSubjects`), and an empty selection is
likewise an assertion failure, never a skip.

Preconditions, not teardown: a test states the package state it needs and converges to it
(`_ensure_absent`, `_ensure_installed_and_manual` for apt, `_ensure_snaps_installed` behind
`_snap_subjects`/`_holdable_snaps` for snap) instead of putting the machines back afterwards.
What one scenario leaves behind is usually what the next one wanted anyway, so the converger
reads and returns. Nobody restores the packages at the end either:
`run-integration-tests.sh` replaces both VMs' subvolumes with their baseline btrfs
snapshots and reboots before every run, which is what makes the machines identical run to
run -- so a package left removed costs nothing and undoing it would.

Cleanup that costs nothing -- `/etc/apt` files, markers, holds, `refresh.hold`, paths taken
aside -- stays in each test's `finally`, and the `/etc/apt` half has to: a synthetic
repository left configured makes every later `apt-get update` on that machine slower and
noisier for the rest of the run. What a test INSTALLED is left installed. Every package built
or fetched here is uuid-suffixed or comes from a repository the same test declared, so no
later selection or assertion reaches it and the review lines it may raise are left unapproved
by the automation hook's SKIP_ONCE default -- an `apt-get purge` would spend seconds of dpkg
work undoing what the next run's baseline reset undoes anyway.

The flatpak subject is the REAL Flathub, and its app is provisioned on pc1 only, so the
source->target divergence the convergence test needs is part of the baseline rather than
something a test manufactures. A locally built stand-in repository would only ever test
this project's model of a remote; #215's key replication is about a real remote's real
trust configuration (`_FIXTURE_FLATPAK_APP`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest

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

pytestmark = pytest.mark.area_package

# Prefix marking each candidate's reverse-dependency block in the batched pc2 probe below.
RDEPENDS_MARKER = "@@RDEPENDS_FOR@@"


@pytest.fixture(scope="module", autouse=True)
async def _package_sync_subjects(vm_test_fixtures: None) -> None:  # pyright: ignore[reportUnusedFunction]
    """Every test in this module operates on a real snap or flatpak, so both VMs must own
    one before any of them runs (`conftest.vm_test_fixtures`).
    """
    _ = vm_test_fixtures


#: Set to any non-empty value to have each test report where its wall clock went. Off by
#: default: it wraps every command this module issues, and its only purpose is deciding what
#: to optimise next (#216), not proving anything about the product.
_TIMING_ENV = "PCSWITCHER_IT_TIMING"

#: What a `pc-switcher sync` invocation looks like, for splitting the runs from everything a
#: test does around them.
_SYNC_COMMAND_MARKER = "pc-switcher sync"


@pytest.fixture(autouse=True)
def _report_where_the_time_went(request: pytest.FixtureRequest) -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Split each test's wall clock into the syncs it runs and the work it does around them,
    and name the slowest individual commands.

    A sync costs what it costs; everything else is setup, convergence and cleanup, which is
    the only part a test can give back. Reading that split off a real run is what says which
    of the two is worth attacking.
    """
    if not os.environ.get(_TIMING_ENV):
        yield
        return

    original = BashLoginRemoteExecutor.run_command
    samples: list[tuple[float, str]] = []

    async def timed(self: BashLoginRemoteExecutor, command: str, *args: object, **kwargs: object) -> CommandResult:
        started = time.monotonic()
        try:
            return await original(self, command, *args, **kwargs)  # pyright: ignore[reportCallIssue, reportArgumentType]
        finally:
            samples.append((time.monotonic() - started, command))

    BashLoginRemoteExecutor.run_command = timed  # pyright: ignore[reportAttributeAccessIssue]
    try:
        yield
    finally:
        BashLoginRemoteExecutor.run_command = original  # pyright: ignore[reportAttributeAccessIssue]
        syncs = [sample for sample in samples if _SYNC_COMMAND_MARKER in sample[1]]
        around = [sample for sample in samples if _SYNC_COMMAND_MARKER not in sample[1]]
        print(
            f"\n[timing] {request.node.name}: "
            f"{sum(d for d, _ in syncs):.1f}s in {len(syncs)} sync(s), "
            f"{sum(d for d, _ in around):.1f}s in {len(around)} other command(s)",
            file=sys.stderr,
            flush=True,
        )
        for duration, command in sorted(around, reverse=True)[:5]:
            print(f"[timing]     {duration:6.1f}s  {' '.join(command.split())[:110]}", file=sys.stderr, flush=True)


# How many shared packages to probe for reverse dependencies when looking for one safe
# to remove, per round and in total. Each probe is a separate `apt-cache rdepends` process
# on the target reloading the apt cache, so the cost is linear and the whole probe runs
# under a single command timeout: the total bounds the search inside that budget, and
# probing a ROUND at a time means a search that succeeds immediately — which is every
# search here so far — pays for one round instead of all of them (measured in a stock
# `ubuntu:24.04`: 8.2s for 12 probes against 26.7s for 40).
_RDEPENDS_PROBE_ROUND = 12
_RDEPENDS_PROBE_LIMIT = 48

# How many candidates beyond the requested count to rehearse, so apt refusing a few still
# leaves enough. Every one costs an `apt-get --dry-run remove` on the target, and no test in
# this module asks for more than three subjects.
_REMOVAL_REHEARSAL_HEADROOM = 4


def nonblank_lines(text: str) -> list[str]:
    """Split command output into stripped, non-empty lines."""
    return [line.strip() for line in text.splitlines() if line.strip()]


# Every escape sequence `logger.RichFormatter` can emit around a log line's styled fields.
# Stripped before any assertion reads the run's own output: the formatter always renders
# through a `force_terminal=True` console, so the text is coloured even when stdout is a pipe.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _collapse_run_output(text: str) -> str:
    """A sync's combined stdout+stderr as one ANSI-free, single-spaced line.

    Both renderers that carry a package job's own words wrap them: `RichFormatter` folds a
    long log record at its console width, and a review group arrives inside a Rich `Panel`.
    A phrase that has to be matched whole therefore has to be matched after the line breaks
    and the padding are gone. Single TOKENS (a package name, a ref, a URL) need none of
    this and are asserted against the raw output elsewhere in this module — Rich never
    breaks a word that fits the line.

    Panel BORDER characters are deliberately left in place: they mark the wrap points
    inside a panel, so a phrase that spans one still fails to match here rather than
    matching a rendering nobody has seen.
    """
    return " ".join(_ANSI_ESCAPE_RE.sub("", text).split())


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


def _no_apt_candidate_message() -> str:
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


async def _apt_would_remove_these(executor: BashLoginRemoteExecutor, names: Sequence[str]) -> set[str]:
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


async def _find_removable_candidates(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor, count: int = 1
) -> list[str]:
    """Query both VMs and pick up to `count` packages safe to remove from pc2 for a test
    (see `pick_safe_removal_candidates`, then `_apt_would_remove_these`). Returns fewer than
    `count` -- possibly none -- when not enough candidates qualify.
    """
    pc1_manual_result = await pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    pc2_manual_result = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    pc2_dpkg_result = await pc2_executor.run_command(
        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
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
    for start in range(0, min(len(initial_candidates), _RDEPENDS_PROBE_LIMIT), _RDEPENDS_PROBE_ROUND):
        this_round = initial_candidates[start : start + _RDEPENDS_PROBE_ROUND]
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
        ][: count - len(confirmed) + _REMOVAL_REHEARSAL_HEADROOM]

        # apt's own verdict on each, because the rdepends check cannot see a candidate that
        # takes an essential package with it (`_apt_would_remove_these`).
        removable = await _apt_would_remove_these(pc2_executor, shortlist)
        rehearsed |= set(shortlist)
        confirmed += [name for name in shortlist if name in removable]
        if len(confirmed) >= count:
            break

    return confirmed[:count]


@dataclass(frozen=True)
class _AptSubjects:
    """The apt packages every test in this module operates on, selected ONCE for the whole
    module by the `apt_subjects` fixture.

    Pinned rather than rediscovered per test, for two reasons that both cost real wall
    clock (#216). Selecting one costs a round of `apt-cache rdepends` plus an
    `apt-get --dry-run remove` for each survivor, and six tests were each paying it. And a
    pinned name is what lets a test converge to its precondition instead of restoring
    afterwards: with a fresh selection each time, a package left removed simply drops out of
    the `apt-mark showmanual` intersection and the next test picks the NEXT one down the
    alphabet, so nothing is reused and the pool drains.

    Snap and flatpak subjects have always been pinned this way (`_FIXTURE_SNAPS`,
    `_FIXTURE_FLATPAK_APP`); apt's were discovered only because any Debian system offers
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


@pytest.fixture(scope="module")
async def apt_subjects(pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor) -> _AptSubjects:
    """Select this module's apt subjects once, before any test has touched a package, so
    the selection sees the machines as provisioning left them.

    Nothing is put back. `run-integration-tests.sh` replaces both VMs' subvolumes with
    their baseline btrfs snapshots and reboots before every run, so where these packages end
    up is not something this module owes the machines; restoring them would be ~36s spent
    undoing what the next run's reset undoes anyway. Within a run each test converges to the
    state IT needs (`_ensure_absent`, `_ensure_installed_and_manual`), which is a read
    whenever the previous scenario already left it that way.

    Under `--skip-reset`, where a developer keeps the machines between runs, the next run
    simply selects again from what is installed then. Subjects left removed drop out of that
    selection rather than breaking it -- and if enough runs drain the candidates,
    `_no_apt_candidate_message` says so instead of failing obscurely.
    """
    picked = await _find_removable_candidates(pc1_executor, pc2_executor, count=4)
    assert len(picked) == 4, f"{_no_apt_candidate_message()} Needed 4 subjects, found {len(picked)}."
    return _AptSubjects(
        install_direction=(picked[0], picked[1], picked[2]),
        removal_direction=picked[3],
        hold=await _a_package_both_machines_have_unheld(pc1_executor, pc2_executor, exclude=frozenset(picked)),
    )


# Splits the two reads `_ensure_installed_and_manual` issues as one command.
_SUBJECT_STATE_MARKER = "@@PCSWITCHER_IT_SUBJECT@@"


async def _subject_state(executor: BashLoginRemoteExecutor, name: str) -> tuple[bool, bool]:
    """`(fully installed, marked manual)` for `name` on `executor`'s machine, in one command.

    `apt-mark showmanual <name>` rather than the whole manual set: it answers about the one
    package, which is all a converger needs and a fraction of the cost.
    """
    quoted = shlex.quote(name)
    result = await executor.run_command(
        f"dpkg-query --show --showformat='${{Status}}' {quoted}; echo; echo {_SUBJECT_STATE_MARKER}; "
        f"apt-mark showmanual {quoted}",
        login_shell=False,
        timeout=20.0,
    )
    status_block, _, manual_block = result.stdout.partition(_SUBJECT_STATE_MARKER)
    return status_block.strip() == "install ok installed", name in nonblank_lines(manual_block)


async def _ensure_absent(executor: BashLoginRemoteExecutor, name: str) -> None:
    """Make `name` absent from `executor`'s machine, doing nothing when it already is.

    The read is what makes a scenario that inherits the state it wanted pay nothing
    (measured on a test VM: the read is hundredths of a second against 6.5s for the
    removal).
    """
    installed, _manual = await _subject_state(executor, name)
    if not installed:
        return
    result = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(name)}",
        login_shell=False,
        timeout=120.0,
    )
    assert result.success, f"Failed to remove {name}: {result.stderr}"


async def _ensure_installed_and_manual(executor: BashLoginRemoteExecutor, name: str) -> None:
    """Make `name` installed and marked manual on `executor`'s machine, doing nothing when
    it already is (the counterpart of `_ensure_absent`, 8.2s when it has to act).
    """
    installed, manual = await _subject_state(executor, name)
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


async def _create_extra_on_target_apt_package(
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
    `_restore_auto_marked_package`.
    """
    pc1_manual_result = await pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    pc2_manual_result = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    pc2_dpkg_result = await pc2_executor.run_command(
        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
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


async def _restore_auto_marked_package(executor: BashLoginRemoteExecutor, name: str) -> None:
    """Put a package promoted by `_create_extra_on_target_apt_package` back to automatic."""
    result = await executor.run_command(f"sudo apt-mark auto {shlex.quote(name)}", login_shell=False, timeout=30.0)
    if not result.success:
        print(f"[cleanup] failed to mark {name} auto again on pc2: {result.stderr}")


def _package_sync_test_config(*, extra_sections: str = "", **enabled_jobs: bool) -> str:
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


def _folder_sync_section(*folder_paths: str) -> str:
    """A `folder_sync` config section mirroring exactly `folder_paths`, with no central
    filter file (the schema makes `filter_file` optional).

    Every path in ONE section: `folder_sync` is a mapping key, so two sections would make
    the config a YAML document with a duplicate key and the run would end before any job.
    """
    folders = "".join(f"    - path: {path}\n      enabled: true\n" for path in folder_paths)
    return f"folder_sync:\n  folders:\n{folders}"


async def _write_package_sync_config(
    executor: BashLoginRemoteExecutor, *, extra_sections: str = "", **enabled_jobs: bool
) -> None:
    """Write a package-sync test config enabling exactly `enabled_jobs` to `executor`
    (always the machine acting as source for the sync under test).
    """
    config = _package_sync_test_config(extra_sections=extra_sections, **enabled_jobs)
    result = await executor.run_command(
        f"mkdir --parents ~/.config/pc-switcher"
        f" && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n{config}CONF_EOF",
        timeout=10.0,
    )
    assert result.success, f"Failed to write package-sync test config: {result.stderr}"


async def _write_apt_sync_config(executor: BashLoginRemoteExecutor) -> None:
    """Write the apt_sync-only test config to pc1 (source)."""
    await _write_package_sync_config(executor, apt_sync=True)


async def _decision_file_exists(executor: BashLoginRemoteExecutor, manager: str) -> bool:
    """Whether `manager`'s machine-local decision file currently exists on `executor`'s
    machine (D-09) -- used to prove a non-interactive run records nothing (D-26).
    """
    relpath = shlex.quote(DECISION_FILE_RELPATH_TEMPLATE.format(manager=manager))
    result = await executor.run_command(f"test -f ~/{relpath}", login_shell=False, timeout=10.0)
    return result.success


def _automation_env_assignment_multi(decisions_by_item_id: Mapping[str, Decision]) -> str:
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


def _automation_env_assignment(item_id: str) -> str:
    """Shell-safe `VAR='{...}'` prefix pre-answering the review with one APPLY decision for
    `item_id` (D-26's hidden hook -- `package_review.PACKAGE_REVIEW_AUTOMATION_ENV`).
    """
    return _automation_env_assignment_multi({item_id: Decision.APPLY})


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

_CONTINUE_TEST_MARKER_ROOT = "/opt"
_CONTINUE_TEST_MARKER_INSTALL_FIRST = f"{_CONTINUE_TEST_MARKER_ROOT}/pcswitcher-it-continue-a-install-first"
_CONTINUE_TEST_MARKER_FAIL = f"{_CONTINUE_TEST_MARKER_ROOT}/pcswitcher-it-continue-b-fail"
_CONTINUE_TEST_MARKER_INSTALL_SECOND = f"{_CONTINUE_TEST_MARKER_ROOT}/pcswitcher-it-continue-c-install-second"
_CONTINUE_TEST_MARKERS = (
    _CONTINUE_TEST_MARKER_INSTALL_FIRST,
    _CONTINUE_TEST_MARKER_FAIL,
    _CONTINUE_TEST_MARKER_INSTALL_SECOND,
)
_DELIBERATE_FAILURE_MESSAGE = "deliberate integration-test failure"


# What a stock Ubuntu 24.04 machine's own `/usr/local` holds — the two scan roots plus the
# nine entries `base-files` creates under `/usr/local`, none of which may ever be presented
# as a finding. Restated rather than imported (the same rule this module's snap/flatpak
# parsers follow): the claim is about what a real machine looks like, so a test agreeing
# with whatever the shipped constant currently says would assert nothing.
_STOCK_DIRECTORIES = (
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
_UNASKED_ITEM_MARKER = "not asked, declined for this run (no TTY): "


def _unowned_item_id(path: str) -> str:
    """The `UnreproducibleItem.item_id` a `_scan_unowned_installs`-detected path at
    `path` would produce (module docstring: identity is `unreproducible:<origin>:
    <identifier>`, independent of `label`).
    """
    return UnreproducibleItem(origin="unowned-path", identifier=path, label=path).item_id


async def _create_unowned_marker(executor: BashLoginRemoteExecutor, path: str) -> None:
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


async def _remove_unowned_marker(executor: BashLoginRemoteExecutor, path: str) -> None:
    await executor.run_command(f"sudo rm --recursive --force {shlex.quote(path)}", login_shell=False, timeout=15.0)


async def _author_snippet(executor: BashLoginRemoteExecutor, item_id: str, label: str, body: str) -> None:
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

_SNAP_INFO_REVISION_RE = re.compile(r"\((\d+)\)")

# Snaps whose removal could break snapd itself or the base runtime every other snap
# depends on -- never a safe divergence/removal candidate for a VM test (T-02-28).
_SNAP_REMOVAL_DENYLIST = frozenset({"snapd", "core", "core16", "core18", "core20", "core22", "core24", "bare"})

# The snaps `tests/integration/scripts/internal/vm-test-fixtures.sh` puts on BOTH VMs,
# alphabetically -- the subjects every snap test below operates on. A stock Ubuntu 24.04
# VM carries only `_SNAP_REMOVAL_DENYLIST` members, so without these there is nothing a
# test may hold, diverge or remove. `hello` leads the list because it is the one with
# distinct revisions across its channels, which is what `_alternate_snap_revision` needs.
_FIXTURE_SNAPS = ("hello", "hello-world")


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
    return set(_SNAP_INFO_REVISION_RE.findall(output))


def _fixture_snap_names(count: int) -> list[str]:
    """The first `count` fixture snaps outside `_SNAP_REMOVAL_DENYLIST` (T-02-28: never a
    base/snapd runtime everything else depends on).
    """
    subjects = [name for name in _FIXTURE_SNAPS if name not in _SNAP_REMOVAL_DENYLIST][:count]
    assert len(subjects) == count, (
        f"Need {count} subjects out of the fixture snaps {_FIXTURE_SNAPS}, of which "
        f"{sorted(set(_FIXTURE_SNAPS) & _SNAP_REMOVAL_DENYLIST)} may never be one."
    )
    return subjects


async def _ensure_snaps_installed(executor: BashLoginRemoteExecutor, names: Sequence[str]) -> None:
    """Make every one of `names` installed on `executor`'s machine, doing nothing about the
    ones that already are -- the snap counterpart of `_ensure_installed_and_manual`.

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


async def _snap_subjects(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor, count: int = 1
) -> list[str]:
    """The first `count` fixture snaps (`_FIXTURE_SNAPS`), converged to installed on BOTH
    machines.

    Converged rather than asserted, for the reason the module docstring gives for the apt
    subjects: a scenario here removes a snap when its claim needs one removed and puts nothing
    back, so getting the machines to "installed on both" belongs to whoever needs it next
    rather than to whoever last touched it. The read `_ensure_snaps_installed` makes is what
    keeps that free whenever the previous scenario already left them installed.
    """
    subjects = _fixture_snap_names(count)
    await _ensure_snaps_installed(pc1_executor, subjects)
    await _ensure_snaps_installed(pc2_executor, subjects)
    return subjects


async def _snap_subject(pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor) -> str:
    """The single fixture snap every one-subject snap test operates on."""
    return (await _snap_subjects(pc1_executor, pc2_executor, count=1))[0]


async def _alternate_snap_revision(executor: BashLoginRemoteExecutor, name: str, current_revision: str) -> str:
    """An installable revision of `name` distinct from `current_revision`, read from
    `snap info`'s channel table -- what a test moves the target to so the sync has a real
    revision divergence to converge (D-06).

    Read rather than hardcoded: pinning a revision number would rot the moment the store
    published a new one. `_FIXTURE_SNAPS[0]` is chosen precisely because it carries
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


async def _snap_notes(executor: BashLoginRemoteExecutor, name: str) -> set[str]:
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


async def _snap_saved_rows(executor: BashLoginRemoteExecutor) -> list[tuple[str, str]]:
    """Every `(set_id, snap_name)` snapshot pair snapd currently holds on `executor`'s
    machine.

    Under sudo, like every other snapd read in this module: the snapshot snapd takes when a
    snap is removed covers system data as well as the invoking user's, and an unprivileged
    listing is not the whole picture of what the machine holds.
    """
    result = await executor.run_command("sudo snap saved", login_shell=False, timeout=20.0)
    return parse_snap_saved_rows(result.stdout)


async def _snap_revision(executor: BashLoginRemoteExecutor, name: str) -> str | None:
    """The revision `name` is active at on `executor`'s machine, or None when it is absent."""
    result = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    return parse_snap_list_names_revisions(result.stdout).get(name)


# A sideloaded snap needs a base its machine already has, or snapd downloads one. Read off
# the machine rather than hardcoded (`_installed_base_snap`): which core* snap a stock
# Ubuntu 24.04 carries depends on what else is installed, and the preference order below
# only decides which of the present ones to declare.
_SIDELOAD_BASE_PREFERENCE = ("core24", "core22", "core20", "core18", "core16", "core")


async def _installed_base_snap(executor: BashLoginRemoteExecutor) -> str:
    """A base snap already installed on `executor`'s machine, for a sideload's `snap.yaml`."""
    result = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    installed = set(parse_snap_list_names_revisions(result.stdout))
    for base in _SIDELOAD_BASE_PREFERENCE:
        if base in installed:
            return base
    raise AssertionError(
        f"No base snap out of {_SIDELOAD_BASE_PREFERENCE} is installed, so a sideload declaring one would make "
        f"snapd download it.\n{result.stdout}"
    )


async def _create_sideloaded_snap(executor: BashLoginRemoteExecutor, directory: str, name: str, base: str) -> None:
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


async def _remove_sideloaded_snap(executor: BashLoginRemoteExecutor, directory: str, name: str) -> None:
    """Undo `_create_sideloaded_snap`, unconditionally (`;`, not `&&`) so a setup that
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
_SNAP_STORE_OFFLINE_CMD = "sudo snap set system store.access=offline"
_SNAP_STORE_ONLINE_CMD = "sudo snap unset system store.access"


async def _home_dir(executor: BashLoginRemoteExecutor) -> str:
    """The absolute home directory of the SSH user on `executor`'s machine.

    Read rather than composed from the test's own environment: `snap_sync_exclude_paths()`
    resolves `~/snap` against the home of the process running the sync, so the folder the
    boundary test mirrors has to be that same directory.
    """
    result = await executor.run_command('printf %s "$HOME"', login_shell=False, timeout=10.0)
    home = result.stdout.strip()
    assert result.success and home.startswith("/"), f"could not read $HOME: {result.stdout!r} {result.stderr!r}"
    return home


async def _machine_utc_now(executor: BashLoginRemoteExecutor) -> datetime:
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
_SYSTEM_REFRESH_HOLD_SET_CMD = (
    "sudo snap set system refresh.hold=\"$(date --utc --date='+6 hours' +%Y-%m-%dT%H:%M:%SZ)\""
)


async def _capture_system_refresh_hold(executor: BashLoginRemoteExecutor) -> str | None:
    """`executor`'s current system-wide `refresh.hold`, or `None` when unset (`snap get`
    exits non-zero, or prints nothing, for an unset option). Read-only.

    Under sudo like the orchestrator's own capture: snapd admin-gates READING snap config,
    so unprivileged this never returns a value -- it fails with "access denied", and every
    machine reads as hold-free.
    """
    result = await executor.run_command("sudo snap get system refresh.hold", login_shell=False, timeout=15.0)
    value = result.stdout.strip()
    return value if result.success and value else None


async def _engage_system_refresh_hold(executor: BashLoginRemoteExecutor) -> None:
    """Pause snapd auto-refresh on `executor`'s machine the same way a sync does, so a
    background auto-refresh cannot mutate `snap list` mid-test (and, for the #208 D9
    check, so a system-wide hold is genuinely in force while per-snap Notes are read).
    """
    result = await executor.run_command(_SYSTEM_REFRESH_HOLD_SET_CMD, login_shell=False, timeout=30.0)
    assert result.success, f"Failed to engage a system-wide snapd refresh.hold: {result.stderr}"


async def _restore_system_refresh_hold(executor: BashLoginRemoteExecutor, prior: str | None) -> None:
    """Put `executor`'s `refresh.hold` back exactly as found -- restoring the prior value
    or clearing it (empty string, which snapd treats as unset), mirroring the
    orchestrator's own teardown so the test leaves no standing hold behind.
    """
    value = shlex.quote(prior) if prior is not None else '""'
    result = await executor.run_command(f"sudo snap set system refresh.hold={value}", login_shell=False, timeout=30.0)
    if not result.success:
        print(f"[cleanup] failed to restore system refresh.hold: {result.stderr}")


async def _holdable_snaps(executor: BashLoginRemoteExecutor, count: int = 1) -> list[str]:
    """The first `count` fixture snaps, converged to installed on `executor`'s machine --
    safe subjects for a per-snap `--hold`/`--unhold` round trip (which, unlike a removal,
    leaves the snap itself untouched).

    One-machine variant of `_snap_subjects`, for the tests that never run a sync. It converges
    for the same reason: the scenarios before this one leave the fixture snaps wherever their
    own claims needed them.
    """
    subjects = _fixture_snap_names(count)
    await _ensure_snaps_installed(executor, subjects)
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
_FIXTURE_FLATPAK_APP = "io.github.fragglet.sdl_sopwith"
_FIXTURE_FLATPAK_REMOTE = "flathub"
_FIXTURE_FLATPAK_SCOPE: Literal["user", "system"] = "user"
# Flathub's `.flatpakrepo`, i.e. how a user adds the remote — it carries the URL, the
# `gpg-verify=true` and the signing key together. Used only to put the remote back after
# a test deleted it; `_flatpak_subject` reads the resulting URL off the machine itself.
_FIXTURE_FLATPAK_REPOFILE = "https://dl.flathub.org/repo/flathub.flatpakrepo"

# The second fixture remote, on pc1 only and feeding no ref: what makes "a remote no
# approved ref needs does not travel" falsifiable. Deleted from pc2 by the fixture script
# and by `_restore_flatpak_target_baseline`, so a run that made it travel cannot leave the
# next run unable to detect that.
_FIXTURE_UNUSED_FLATPAK_REMOTE = "flathub-beta"


async def _flatpak_subject(
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
    rows = [row for row in parse_flatpak_list_lines(list_result.stdout) if row[0] == _FIXTURE_FLATPAK_APP]
    assert rows, (
        f"The fixture flatpak {_FIXTURE_FLATPAK_APP} is not installed. It is created by "
        f"tests/integration/scripts/internal/vm-test-fixtures.sh.\n{list_result.stdout}"
    )
    application, version, origin, installation, ref = rows[0]
    assert installation == _FIXTURE_FLATPAK_SCOPE, (
        f"{application} is installed in scope {installation!r}, expected {_FIXTURE_FLATPAK_SCOPE!r}"
    )

    scope_flag = "--user" if _FIXTURE_FLATPAK_SCOPE == "user" else "--system"
    remotes_result = await executor.run_command(
        f"flatpak remotes {scope_flag} --columns=name,url", login_shell=False, timeout=20.0
    )
    for line in remotes_result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[0] == origin:
            return application, version, _FIXTURE_FLATPAK_SCOPE, fields[0], fields[1], ref
    raise AssertionError(
        f"{application}'s origin remote {origin!r} is not configured in scope "
        f"{_FIXTURE_FLATPAK_SCOPE}:\n{remotes_result.stdout}"
    )


async def _restore_flatpak_target_baseline(executor: BashLoginRemoteExecutor) -> None:
    """Put `executor` (the sync TARGET) back to what the fixture script leaves behind:
    the Flathub remote configured, the app's runtime installed, and the app itself ABSENT.

    Not symmetric with the source: the app's absence here IS the fixture (see
    `_FIXTURE_FLATPAK_APP`), so a test that made the sync install it must undo that or the
    next run starts from a converged pair and proves nothing. The remote is re-added from
    Flathub's own `.flatpakrepo`, which restores its real trust configuration — the same
    keyring bytes, so no spurious trust divergence is left behind (verified live).

    The runtime is deliberately left installed: `flatpak uninstall --unused` would sweep
    it and turn every later app install into a multi-hundred-MB download.
    """
    scope_flag = "--user" if _FIXTURE_FLATPAK_SCOPE == "user" else "--system"
    sudo = "" if _FIXTURE_FLATPAK_SCOPE == "user" else "sudo "
    result = await executor.run_command(
        f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(_FIXTURE_FLATPAK_APP)} || true; "
        f"{sudo}flatpak remote-delete {scope_flag} --force "
        f"{shlex.quote(_FIXTURE_UNUSED_FLATPAK_REMOTE)} || true; "
        f"{sudo}flatpak remote-add {scope_flag} --if-not-exists "
        f"{shlex.quote(_FIXTURE_FLATPAK_REMOTE)} {shlex.quote(_FIXTURE_FLATPAK_REPOFILE)}",
        login_shell=False,
        timeout=180.0,
    )
    if not result.success:
        print(f"[cleanup] failed to restore the target's flatpak baseline: {result.stderr}")


async def _restore_flatpak_source_baseline(
    executor: BashLoginRemoteExecutor, remote_name: str, scope: Literal["user", "system"], filter_path: str
) -> None:
    """Put `executor` (the sync SOURCE) back to an UNFILTERED `remote_name`, and drop the
    filter file at `filter_path`.

    Delete-and-re-add rather than `flatpak remote-modify --no-filter`: that option's
    availability on this flatpak is not something this suite has measured, and re-adding from
    Flathub's own `.flatpakrepo` is the one restore already proven to reproduce the remote's
    trust configuration byte-for-byte (`_restore_flatpak_target_baseline`). The app installed
    from it stays installed and keeps naming `remote_name` as its origin.
    """
    scope_flag = "--user" if scope == "user" else "--system"
    sudo = "" if scope == "user" else "sudo "
    result = await executor.run_command(
        f"{sudo}flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true; "
        f"{sudo}flatpak remote-add {scope_flag} --if-not-exists {shlex.quote(remote_name)} "
        f"{shlex.quote(_FIXTURE_FLATPAK_REPOFILE)}; "
        f"rm --force {filter_path}",
        login_shell=False,
        timeout=180.0,
    )
    if not result.success:
        print(f"[cleanup] failed to restore the source's unfiltered {remote_name}: {result.stderr}")


async def _flatpak_app_rows(executor: BashLoginRemoteExecutor) -> list[tuple[str, str, str, str, str]]:
    """Every installed APP on `executor`'s machine, as `parse_flatpak_list_lines` tuples.

    The same five columns `flatpak_sync` captures, so a comparison of this list before and
    after a run is a comparison of exactly what the job would have seen.
    """
    result = await executor.run_command(
        "flatpak list --app --columns=application,version,origin,installation,ref", login_shell=False, timeout=20.0
    )
    return parse_flatpak_list_lines(result.stdout)


async def _flatpak_remote_row(
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


async def _flatpak_remote_filter(
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
_FLATPAK_FILTER_BODY = f"allow {_FIXTURE_FLATPAK_APP}\n"

# The token `flatpak remotes --columns=options` prints for a remote carrying a ref filter --
# restated here rather than imported, so the test fails when the shipped constant and the real
# flatpak disagree instead of agreeing with whatever `flatpak_sync` happens to say.
_FLATPAK_FILTERED_OPTION = "filtered"


# -- apt repository-state helpers (D-11/D-12): synthesize a repo+key divergence -----
#
# The two `/etc/apt` directories the apt-repository-state test touches (apt_sync.py owns
# the full five-directory set).
_APT_SOURCES_DIR = "/etc/apt/sources.list.d"
_APT_KEYRINGS_DIR = "/etc/apt/keyrings"
_APT_PREFERENCES_DIR = "/etc/apt/preferences.d"

# Host the synthetic repository points at. `.invalid` is reserved by RFC 2606 and can
# never resolve, so apt reaches this repo only to fail, and the name appears in
# `apt-get update`'s output for exactly as long as the repo is configured.
_SYNTHETIC_REPO_HOST = "pcswitcher-it.invalid"


async def _create_synthetic_repo_and_key(executor: BashLoginRemoteExecutor) -> tuple[str, str]:
    """Create a synthetic vendor apt repository the target lacks on `executor` (the source):
    a deb822 `.sources` file under `/etc/apt/sources.list.d/` whose `Signed-By:` names a
    signing-key file under `/etc/apt/keyrings/`, plus that key file with dummy bytes.
    Returns `(source_filename, key_filename)`.

    Both directories are root-owned and `/etc/apt/keyrings` is absent on a fresh Ubuntu
    24.04, so `mkdir --parents` runs first (the shipped invariant) and every write goes through
    `sudo tee`. Filenames are uuid-suffixed so the pair is unique and the fresh target
    provably lacks it. Dummy key bytes are fine: D-12 copies keys verbatim without
    validating, and `_SYNTHETIC_REPO_HOST` never resolves, so an `apt-get update` that
    sees this repo can only fail to fetch its index -- it can never install anything from
    it, on a dry run or a real one.
    """
    uniq = uuid4().hex[:12]
    source_filename = f"pcswitcher-it-repo-{uniq}.sources"
    key_filename = f"pcswitcher-it-key-{uniq}.gpg"
    source_dest = f"{_APT_SOURCES_DIR}/{source_filename}"
    key_dest = f"{_APT_KEYRINGS_DIR}/{key_filename}"
    source_body = (
        "Types: deb\n"
        f"URIs: https://{_SYNTHETIC_REPO_HOST}/repo\n"
        "Suites: stable\n"
        "Components: main\n"
        f"Signed-By: {key_dest}\n"
    )
    result = await executor.run_command(
        f"sudo mkdir --parents {shlex.quote(_APT_KEYRINGS_DIR)} && "
        f"printf %s {shlex.quote(source_body)} | sudo tee {shlex.quote(source_dest)} > /dev/null && "
        f"printf %s {shlex.quote(f'pcswitcher-it-dummy-key-{uniq}')} | sudo tee {shlex.quote(key_dest)} > /dev/null",
        login_shell=False,
        timeout=20.0,
    )
    assert result.success, f"Failed to create synthetic repo+key on source: {result.stderr}"
    return source_filename, key_filename


async def _install_from_a_repo_the_target_lacks(executor: BashLoginRemoteExecutor) -> tuple[str, str, str]:
    """On `executor` (the source): build a trivial `.deb`, publish it in a flat `file:` apt
    repository, declare that repository, and install the package from it. Returns
    `(package_name, repo_dir, list_filename)`.

    The only construction that produces ADR-020 D-34's class 3 on real machines: a package
    the source has FROM A REPOSITORY IT DECLARES whose name the target's apt has never
    heard. `_create_synthetic_repo_and_key`'s repository cannot do it — its host does not
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
            f" | sudo tee {shlex.quote(f'{_APT_SOURCES_DIR}/{list_filename}')} > /dev/null",
        )
    )
    built = await executor.run_command(build, login_shell=False, timeout=60.0)
    assert built.success, f"Failed to build the synthetic repository on the source: {built.stderr}"

    updated = await _apt_get_update_for(executor, f"{_APT_SOURCES_DIR}/{list_filename}")
    assert updated.success, f"apt-get update failed on the source after adding {repo_dir}: {updated.stderr}"

    installed = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(name)}",
        login_shell=False,
        timeout=120.0,
    )
    assert installed.success, f"Failed to install {name} from {repo_dir} on the source: {installed.stderr}"
    return name, repo_dir, list_filename


async def _undeclare_local_repository(executor: BashLoginRemoteExecutor, repo_dir: str, list_filename: str) -> None:
    """Take a test-built `file:` repository off `executor`'s machine: its declaration, the
    published tree, and the index apt cached for it.

    The packages installed FROM it stay installed (module docstring). The declaration is what
    every later `apt-get update` on the machine pays for and taking it off is a `rm`; the
    purge is dpkg work nothing later reads. The tree goes with it because it sits under
    `/opt`, one of `manual_installs_sync`'s scan roots.

    Every step runs unconditionally (`;`, not `&&`) so a setup that failed halfway still
    has the rest of itself removed.
    """
    await executor.run_command(
        f"sudo rm --force --recursive {shlex.quote(repo_dir)} "
        f"{shlex.quote(f'{_APT_SOURCES_DIR}/{list_filename}')}; "
        f"sudo rm --force /var/lib/apt/lists/_opt_{repo_dir.rsplit('/', 1)[-1]}_*",
        login_shell=False,
        timeout=60.0,
    )


async def _install_a_hand_downloaded_deb(executor: BashLoginRemoteExecutor) -> str:
    """On `executor`: build a trivial `.deb` and `dpkg --install` it, the way a user who
    downloaded a vendor package does. Returns the package name.

    No repository anywhere declares it, so apt reports the INSTALLED version as the whole of
    that package's version table and names no repository origin for it — the fact
    `PKG-FR-DEB-OWNERSHIP` and `PKG-FR-MANUAL-SCOPE` turn on, and the one a mocked
    `apt-cache policy` can only assert about output somebody wrote by hand.

    Deliberately not `_install_from_a_repo_the_target_lacks`: that one publishes its package
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


def _no_candidate_item_id(name: str) -> str:
    """The `UnreproducibleItem.item_id` an installed package no repository supplies produces
    (`unreproducible:apt-no-candidate:<name>`), built from the shipped dataclass rather than
    from a literal so a change to the identity fails here.
    """
    return UnreproducibleItem(origin="apt-no-candidate", identifier=name, label=name).item_id


async def _create_synthetic_pin(executor: BashLoginRemoteExecutor) -> str:
    """Create a uuid-suffixed `/etc/apt/preferences.d` file the target lacks, and return its
    filename.

    A pin is in ADR-020 D-36's always-sync bucket: it travels with no review line and no
    derivation predicate, which makes it the cheapest real subject for the derived-write
    preview. Its stanza names a package and an origin neither machine has, so it is inert
    wherever it lands — a pin naming an absent origin changes nothing about apt's choices.
    """
    uniq = uuid4().hex[:12]
    filename = f"pcswitcher-it-pin-{uniq}"
    dest = f"{_APT_PREFERENCES_DIR}/{filename}"
    body = f"Package: pcswitcher-it-nothing-{uniq}\nPin: origin {_SYNTHETIC_REPO_HOST}\nPin-Priority: 1000\n"
    result = await executor.run_command(
        f"printf %s {shlex.quote(body)} | sudo tee {shlex.quote(dest)} > /dev/null",
        login_shell=False,
        timeout=20.0,
    )
    assert result.success, f"Failed to create synthetic pin on source: {result.stderr}"
    return filename


async def _take_paths_aside(executor: BashLoginRemoteExecutor, paths: Sequence[str]) -> str:
    """Move whichever of `paths` exist into a fresh backup directory, and return it for
    `_put_paths_back`.

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


async def _put_paths_back(executor: BashLoginRemoteExecutor, backup_dir: str, paths: Sequence[str]) -> None:
    """Undo `_take_paths_aside`: each path ends as whatever was there before, present or
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


async def _apt_get_update(executor: BashLoginRemoteExecutor) -> CommandResult:
    """Run `apt-get update` on `executor` with the output locale pinned to C, so the
    `Err:` prefix `apt_update_lines_naming`'s callers key on is apt's untranslated one
    whatever locale the machine is configured with.
    """
    return await executor.run_command(
        "sudo LC_ALL=C DEBIAN_FRONTEND=noninteractive apt-get update", login_shell=False, timeout=180.0
    )


async def _apt_get_update_for(executor: BashLoginRemoteExecutor, source_path: str) -> CommandResult:
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


async def _assert_flatpak_available(executor: BashLoginRemoteExecutor) -> None:
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
_APT_STATE_DIRS = (
    _APT_SOURCES_DIR,
    _APT_PREFERENCES_DIR,
    "/etc/apt/apt.conf.d",
    _APT_KEYRINGS_DIR,
    "/etc/apt/trusted.gpg.d",
)


@dataclass(frozen=True)
class _MachinePackageState:
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


async def _capture_machine_package_state(executor: BashLoginRemoteExecutor) -> _MachinePackageState:
    """Read `executor`'s complete apt/snap/flatpak state (see `_MachinePackageState`).

    `snap list --all` is reduced to `{name: revision}` rather than kept as raw text: the
    Version column tracks the revision, so keeping both would only add a second way for
    the same fact to be reported.

    The `/etc/apt` listing is `sudo`-qualified and guarded per directory the same way
    `apt_sync.probe.capture_dir_digests` is: `/etc/apt/keyrings` is absent on a stock Ubuntu
    24.04, and an absent directory must read as "nothing here" rather than as a failure.

    Both flatpak scopes are read, because a job writes remotes in whichever scope an
    approved application came from.
    """
    manual = await executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    held = await executor.run_command("apt-mark showhold", login_shell=False, timeout=15.0)
    dpkg = await executor.run_command(
        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
    )
    etc_apt = await executor.run_command(
        "; ".join(
            f"if sudo test -d {shlex.quote(directory)}; then "
            f"sudo find {shlex.quote(directory)} -maxdepth 1 -type f -exec sha256sum {{}} +; fi"
            for directory in _APT_STATE_DIRS
        ),
        login_shell=False,
        timeout=30.0,
    )
    assert etc_apt.success, f"Failed to read /etc/apt digests: {etc_apt.stderr}"
    snaps = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    flatpaks = await executor.run_command(
        "flatpak list --app --columns=application,version,origin,installation,ref", login_shell=False, timeout=20.0
    )
    remotes = await executor.run_command(
        "flatpak remotes --user --columns=name,url,options,filter; "
        "flatpak remotes --system --columns=name,url,options,filter",
        login_shell=False,
        timeout=20.0,
    )
    return _MachinePackageState(
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
_SECTION_MARKER = "@@PCSWITCHER_IT_SECTION@@"


async def _apt_selection_snapshot(
    executor: BashLoginRemoteExecutor,
) -> tuple[set[str], set[str], dict[str, str]]:
    """One machine's `(manual set, hold set, {package: installed version})`, read in ONE
    command (testing-guide.md's command-grouping rule).
    """
    result = await executor.run_command(
        f"apt-mark showmanual; echo {_SECTION_MARKER}; apt-mark showhold; echo {_SECTION_MARKER}; "
        "dpkg-query --show --showformat='${Package}\\t${Version}\\n'",
        login_shell=False,
        timeout=30.0,
    )
    assert result.success, f"Failed to read the machine's apt selection state: {result.stderr}"
    manual_block, hold_block, version_block = result.stdout.split(_SECTION_MARKER)
    versions: dict[str, str] = {}
    for line in nonblank_lines(version_block):
        name, _, version = line.partition("\t")
        versions[name] = version
    return set(nonblank_lines(manual_block)), set(nonblank_lines(hold_block)), versions


async def _a_package_both_machines_have_unheld(
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
    source_manual, source_held, source_versions = await _apt_selection_snapshot(pc1_executor)
    target_manual, target_held, target_versions = await _apt_selection_snapshot(pc2_executor)
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
_UNINSTALLED_ARCHIVE_CANDIDATES = ("cowsay", "sl", "toilet", "fortune-mod")


async def _a_name_apt_knows_the_machine_does_not_have(executor: BashLoginRemoteExecutor) -> str:
    """A package `_UNINSTALLED_ARCHIVE_CANDIDATES` names that this machine's apt can resolve
    and dpkg does not have installed -- the only state `apt-mark hold` can be given to
    produce a hold that freezes nothing.
    """
    result = await executor.run_command(
        f"apt-cache policy {' '.join(_UNINSTALLED_ARCHIVE_CANDIDATES)}", login_shell=False, timeout=30.0
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

    for name in _UNINSTALLED_ARCHIVE_CANDIDATES:
        block = facts.get(name, {})
        if block.get("Installed:") == "(none)" and block.get("Candidate:", "(none)") != "(none)":
            return name
    raise AssertionError(
        f"None of {list(_UNINSTALLED_ARCHIVE_CANDIDATES)} is both known to apt and absent from dpkg on this "
        f"machine, so no hold naming a package it does not have can be set up.\n{result.stdout}"
    )


# `snap:hold:<name>` has no `SnapHoldItem` dataclass to build the id from -- `snap_sync`
# constructs the `ItemDiff` inline (02-208-HOLD-MASK-REPLICATION.md's own deviation note),
# so the literal shape is restated here exactly as `_diff_snap_holds` emits it.
def _snap_hold_item_id(name: str) -> str:
    return f"snap:hold:{name}"


# The directory the install-snippet registry lives in, derived from the relpath
# `packages.state` owns rather than restated, so moving the registry moves this with it.
_REGISTRY_DIR_RELPATH = SNIPPET_REGISTRY_RELPATH.rsplit("/", 1)[0]


# ---------------------------------------------------------------------------------
# Three things only a real apt settles: what a removal takes with it, which repository
# wins a candidate, and what `apt-mark` records. Every subject below is BUILT, for the
# reason the module docstring gives for the snap and flatpak ones -- two VMs provisioned
# from one baseline hold no package pair with the dependency a cascade needs, and no
# vendor repository at all.
# ---------------------------------------------------------------------------------

_SYNTHETIC_PACKAGE_VERSION = "1.0"


def _synthetic_control(name: str, *, version: str = _SYNTHETIC_PACKAGE_VERSION, depends: str = "") -> str:
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


def _packages_index_stanza(name: str, control: str) -> tuple[str, str]:
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


async def _publish_a_cascading_pair(executor: BashLoginRemoteExecutor) -> tuple[str, str, str, str]:
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
    base_control = _synthetic_control(base)
    dependent_control = _synthetic_control(dependent, depends=base)

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
            *_packages_index_stanza(base, base_control),
            *_packages_index_stanza(dependent, dependent_control),
            f"}} | sudo tee {shlex.quote(f'{repo_dir}/Packages')} > /dev/null",
            f"printf '%s\\n' {shlex.quote(f'deb [trusted=yes] file:{repo_dir} ./')}"
            f" | sudo tee {shlex.quote(f'{_APT_SOURCES_DIR}/{list_filename}')} > /dev/null",
        )
    )
    built = await executor.run_command(build, login_shell=False, timeout=60.0)
    assert built.success, f"Failed to build the cascading pair's repository: {built.stderr}"

    updated = await _apt_get_update_for(executor, f"{_APT_SOURCES_DIR}/{list_filename}")
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


def _collateral_removal_item_id(package: str) -> str:
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
_VENDOR_REPO_URI = "https://cli.github.com/packages"
_VENDOR_REPO_KEY_URL = "https://cli.github.com/packages/githubcli-archive-keyring.gpg"
_VENDOR_REPO_HOST = "cli.github.com"
_VENDOR_PACKAGE = "gh"


async def _install_from_the_vendor_repository(executor: BashLoginRemoteExecutor) -> tuple[str, str]:
    """On `executor` (the source): declare the vendor repository with its real signing key
    and install `_VENDOR_PACKAGE` from it. Returns `(source_filename, key_filename)`.

    A deb822 `.sources` naming the keyring in `Signed-By:`, which is the shape the derived
    write and the key copy both have to carry to the target for the install to be possible
    there at all. Filenames are uuid-suffixed so a fresh target provably lacks them and the
    divergence is exactly the one this builds.
    """
    uniq = uuid4().hex[:12]
    source_filename = f"pcswitcher-it-vendor-{uniq}.sources"
    key_filename = f"pcswitcher-it-vendor-{uniq}.gpg"
    key_dest = f"{_APT_KEYRINGS_DIR}/{key_filename}"
    source_dest = f"{_APT_SOURCES_DIR}/{source_filename}"

    declare = "\n".join(
        (
            "set -euo pipefail",
            f"sudo mkdir --parents {shlex.quote(_APT_KEYRINGS_DIR)}",
            f"curl --fail --silent --show-error --location {shlex.quote(_VENDOR_REPO_KEY_URL)}"
            f" | sudo tee {shlex.quote(key_dest)} > /dev/null",
            f"sudo chmod 0644 {shlex.quote(key_dest)}",
            f"printf 'Types: deb\\nURIs: %s\\nSuites: stable\\nComponents: main\\n"
            f"Architectures: %s\\nSigned-By: %s\\n'"
            f' {shlex.quote(_VENDOR_REPO_URI)} "$(dpkg --print-architecture)" {shlex.quote(key_dest)}'
            f" | sudo tee {shlex.quote(source_dest)} > /dev/null",
        )
    )
    declared = await executor.run_command(declare, login_shell=False, timeout=60.0)
    assert declared.success, (
        f"Failed to declare {_VENDOR_REPO_URI} on the source. It is fetched over the network with curl, so an "
        f"unreachable host or a missing curl reports itself here.\n{declared.stderr}"
    )

    updated = await _apt_get_update_for(executor, source_dest)
    assert updated.success, f"apt-get update failed after adding {_VENDOR_REPO_URI}: {updated.stderr}"

    installed = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends "
        f"{shlex.quote(_VENDOR_PACKAGE)}",
        login_shell=False,
        timeout=300.0,
    )
    assert installed.success, f"Failed to install {_VENDOR_PACKAGE} from {_VENDOR_REPO_URI}: {installed.stderr}"
    return source_filename, key_filename


async def _undeclare_the_vendor_repository(
    executor: BashLoginRemoteExecutor, source_filename: str, key_filename: str
) -> None:
    """Take the vendor repository's declaration and its signing key off `executor`'s machine.

    The two `rm`s and nothing else. `_VENDOR_PACKAGE` stays wherever the run under test left
    it (module docstring): what a later `apt-get update` pays for is the repository being
    configured, and purging a package installed from a repository this test declared undoes
    something nothing later reads.
    """
    await executor.run_command(
        f"sudo rm --force {shlex.quote(f'{_APT_SOURCES_DIR}/{source_filename}')} "
        f"{shlex.quote(f'{_APT_KEYRINGS_DIR}/{key_filename}')}",
        login_shell=False,
        timeout=15.0,
    )


async def _publish_a_rival_candidate(executor: BashLoginRemoteExecutor) -> tuple[str, str, str]:
    """On `executor` (the target): make apt prefer somebody else's `_VENDOR_PACKAGE` to the
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
    control = _synthetic_control(_VENDOR_PACKAGE, version="99.0")
    pin_body = f"Package: {_VENDOR_PACKAGE}\nPin: origin {_VENDOR_REPO_HOST}\nPin-Priority: 1\n"

    build = "\n".join(
        (
            "set -euo pipefail",
            "work=$(mktemp --directory)",
            f'mkdir --parents "$work/{_VENDOR_PACKAGE}/DEBIAN"',
            f'printf %s {shlex.quote(control)} > "$work/{_VENDOR_PACKAGE}/DEBIAN/control"',
            f'dpkg-deb --build "$work/{_VENDOR_PACKAGE}" "$work/{_VENDOR_PACKAGE}.deb" > /dev/null',
            f"sudo mkdir --parents {shlex.quote(repo_dir)}",
            f'sudo cp "$work/{_VENDOR_PACKAGE}.deb" {shlex.quote(repo_dir)}/',
            "{",
            *_packages_index_stanza(_VENDOR_PACKAGE, control),
            f"}} | sudo tee {shlex.quote(f'{repo_dir}/Packages')} > /dev/null",
            f"printf '%s\\n' {shlex.quote(f'deb [trusted=yes] file:{repo_dir} ./')}"
            f" | sudo tee {shlex.quote(f'{_APT_SOURCES_DIR}/{list_filename}')} > /dev/null",
            f"printf %s {shlex.quote(pin_body)}"
            f" | sudo tee {shlex.quote(f'{_APT_PREFERENCES_DIR}/{pin_filename}')} > /dev/null",
        )
    )
    built = await executor.run_command(build, login_shell=False, timeout=60.0)
    assert built.success, f"Failed to publish the rival candidate on the target: {built.stderr}"

    updated = await _apt_get_update_for(executor, f"{_APT_SOURCES_DIR}/{list_filename}")
    assert updated.success, f"apt-get update failed after adding {repo_dir}: {updated.stderr}"

    # The rival is what the target would install today, before the vendor's repository has
    # been written there at all -- asserted so a run that refuses the install below is
    # refusing it for the reason this test is about.
    policy = await executor.run_command(
        f"apt-cache policy {shlex.quote(_VENDOR_PACKAGE)}", login_shell=False, timeout=30.0
    )
    assert policy.success and repo_dir in policy.stdout, (
        f"the target's candidate for {_VENDOR_PACKAGE} does not come from {repo_dir}.\n{policy.stdout}"
    )
    return repo_dir, list_filename, pin_filename


async def _remove_the_rival_candidate(
    executor: BashLoginRemoteExecutor, repo_dir: str, list_filename: str, pin_filename: str
) -> None:
    """Undo `_publish_a_rival_candidate`, every step unconditional.

    Kept whole where the purges around it are not (module docstring): all of it is `rm`, and a
    repository and a pin left in `/etc/apt` change what apt answers on this machine for the
    rest of the run.
    """
    await executor.run_command(
        f"sudo rm --force --recursive {shlex.quote(repo_dir)} "
        f"{shlex.quote(f'{_APT_SOURCES_DIR}/{list_filename}')} "
        f"{shlex.quote(f'{_APT_PREFERENCES_DIR}/{pin_filename}')}; "
        f"sudo rm --force /var/lib/apt/lists/_opt_{repo_dir.rsplit('/', 1)[-1]}_*",
        login_shell=False,
        timeout=60.0,
    )


# The two files ADR-020 D-38 gates on the target's Ubuntu Pro attachment, with the real
# stanzas `pro enable` writes. Their `Signed-By:` keyrings ship with `ubuntu-pro-client`
# on every Ubuntu 24.04, attached or not, so this is the file set a genuinely attached
# source carries — not an approximation of it.
_ESM_SOURCE_BODIES = {
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
_HOLD_POLL_TIMEOUT_SECONDS = 180.0
_HOLD_POLL_INTERVAL_SECONDS = 0.5

# `pkill --full` matches on the whole command line, and the shell that RUNS pkill has the
# pattern in its own. The bracket makes the two differ: this regex matches `pc-switcher
# sync`, and the literal text `pc-switcher[ ]sync` sitting in the shell's command line does
# not match it — so the kill reaches the sync and not the shell asking for it.
_KILL_RUNNING_SYNC_CMD = "pkill --signal KILL --full 'pc-switcher[ ]sync'"

# How far ahead of the writing machine's own clock the sync-window suspension lapses, and
# how much of that may already have elapsed by the time the value is read back. Restated
# rather than imported, exactly as `_SYSTEM_REFRESH_HOLD_SET_CMD` above is: the point is
# that the value snapd holds IS a near-future instant, which a test agreeing with whatever
# the orchestrator's private constant currently says would not assert.
_SNAP_HOLD_EXPECTED_DURATION = timedelta(hours=6)
_SNAP_HOLD_DURATION_SLACK = timedelta(minutes=15)


class TestOneRunConvergesEveryManager:
    """One seeded divergence per manager, carried through four runs of the same pair: a
    rehearsal that changes nothing, a run that converges all of it, an identical re-run that
    finds nothing left to do, and a last run that takes a ref filter off the target after the
    source drops it.

    Every claim below used to be its own `pc-switcher sync`, and a three-manager sync costs
    30-40s of wall clock whatever it converges (#216). They share a run here because they
    share a premise: one pair of machines, one seeded divergence per manager, one set of
    decisions. What each of them needs from the run is an assertion, not a run of its own.

    The seeds, one per manager. The `finally` takes back the ones a later scenario would
    otherwise inherit -- holds, `/etc/apt` files, remotes, markers -- and leaves the package
    state where the run put it (module docstring):

    - apt: a package removed from pc2 (install direction), a package removed from pc1
      (removal direction), a hold set on pc1 for a package both machines already have at the
      same version, and a synthetic vendor repository + signing key + pin written to pc1's
      `/etc/apt`;
    - snap: pc2's first fixture snap moved to another revision, and a per-snap hold set on
      pc1's second one;
    - flatpak: the fixture app and the Flathub remote deleted from pc2, a ref filter applied
      to pc1's Flathub, and a uuid-named remote added to pc2 that pc1 does not have;
    - manual installs: an unowned `/opt` path on pc1 with a snippet authored against it.

    Witnesses are the target's own package managers and filesystem throughout, except where
    a claim is about ordering or about a rehearsal, which leave nothing on disk to read.
    """

    async def test_a_divergence_in_every_manager_is_rehearsed_converged_and_then_a_fixed_point(  # noqa: PLR0915
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: _AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """J1, K9, J58, J59, A65, C2, C165, C168, C169, J53, J116, B1, E21, E67, K10, E55,
        K11, F72, G67, H30, K12, K16, J2, N8, J145 — and ADR-020 D-37 in both flatpak
        directions plus `PKG-FR-FLATPAK-FILTER`'s two halves.

        Run 1 (`--dry-run`, ADR-014): pc2's whole `_MachinePackageState` is byte-identical
        across it, and the preview reports the always-sync pin as a derived write while
        saying nothing at all about the synthetic repository and its key -- which no approved
        package needs, so under derivation it neither travels nor becomes a review line
        (ruling 4). The decisions passed in name only packages and refs; neither `/etc/apt`
        file is decidable, and a run that wrote one because something else was ticked would
        be the defect.

        Run 2 converges everything and is the whole-run witness:

        - apt installs the package pc2 lost, removes the one pc1 lost, and registers pc1's
          hold on pc2 with no review line of its own (`PKG-FR-BLOCKS-DERIVED`);
        - snap lands pc2 on pc1's revision without either machine's `refresh.hold` moving
          (D-06), and pc1's per-snap hold reaches pc2's `snap list` Notes through the very
          window the orchestrator holds snapd in (#208 D9);
        - flatpak provisions the remote BEFORE installing the ref that needs it (D-14),
          carrying Flathub's real signing key (#215) and the source's ref filter, deletes the
          target-only remote together with its keyring, and leaves `flathub-beta` -- which no
          approved ref comes from -- on pc1 alone;
        - manual installs pushes the source registry to pc2 and replays the snippet there in
          the same run (D-23);
        - and pc1's own `_MachinePackageState` is identical across it
          (`PKG-FR-SOURCE-INTENT`): a run that genuinely installs, removes and re-revisions
          on the target changes nothing about what software the source has, nor where it gets
          it from.

        Run 3 is the fixed point (`--allow-out-of-order`): pc2's `_MachinePackageState` does
        not move, and the converged apt item is mapped SKIP_ALWAYS yet no decision entry
        appears on either machine -- state-based proof it was never presented. Witness 2 is
        scoped to that item; items already diverged between the two VMs and left SKIP_ONCE by
        run 2 are legitimately presented again, which is not what idempotency promises.
        Witness 1 is unscoped and covers all four managers.

        Run 4 removes pc1's ref filter and syncs once more: `_converge_remote_filters` takes
        pc2's copy off, which is the only thing that converges a filter the source has
        dropped, and the app stays installed.

        snapd auto-refresh is paused on both hosts for the whole test (the same timed
        `refresh.hold` a sync engages, restored exactly afterwards) so a background refresh
        cannot move `snap list` between two captures and be misread as a run's doing.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        await _assert_flatpak_available(pc1_executor)
        await _assert_flatpak_available(pc2_executor)

        # -- subjects, all selected before either machine is touched ---------------------
        install_candidate = apt_subjects.install_direction[0]
        removal_candidate = apt_subjects.removal_direction
        hold_subject = apt_subjects.hold

        revision_snap, hold_snap = await _snap_subjects(pc1_executor, pc2_executor, count=2)
        source_snap_revision = await _snap_revision(pc1_executor, revision_snap)
        target_snap_revision = await _snap_revision(pc2_executor, revision_snap)
        assert source_snap_revision and target_snap_revision, f"{revision_snap} is not installed on both machines"
        alternate_revision = await _alternate_snap_revision(pc2_executor, revision_snap, source_snap_revision)

        application, version, scope, remote_name, _remote_url, ref = await _flatpak_subject(pc1_executor)
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""
        ref_item_id = FlatpakItem(
            application=application, version=version, origin=remote_name, scope=scope, ref=ref
        ).item_id

        uniq = uuid4().hex[:12]
        unowned_path = f"/opt/pcswitcher-it-converge-{uniq}"
        manual_item_id = _unowned_item_id(unowned_path)
        # Home-relative marker so the snippet needs no sudo: replay runs `bash -c <body>` as
        # the SSH user on pc2, and $HOME expands there.
        replay_marker = f"$HOME/.cache/pcswitcher-it-converge-{uniq}"
        registry_relpath = f"~/{SNIPPET_REGISTRY_RELPATH}"
        # Unquoted `$HOME` on purpose: the remote shell expands it, and flatpak stores
        # whatever path it is given verbatim. `_flatpak_remote_filter` reads the expanded
        # path back off the machine, which is the one both machines must end up naming.
        filter_path = f"$HOME/.cache/pcswitcher-it-flatpak-filter-{uniq}"
        target_only_remote = f"pcswitcher-it-vendor-{uniq}"
        target_only_keyring = f"$HOME/.local/share/flatpak/repo/{target_only_remote}.trustedkeys.gpg"

        pc1_prior_hold = await _capture_system_refresh_hold(pc1_executor)
        pc2_prior_hold = await _capture_system_refresh_hold(pc2_executor)

        source_filename = key_filename = pin_filename = recorded_filter_path = ""
        try:
            await _engage_system_refresh_hold(pc1_executor)
            await _engage_system_refresh_hold(pc2_executor)

            # -- seed apt ----------------------------------------------------------------
            await _ensure_installed_and_manual(pc1_executor, install_candidate)
            await _ensure_absent(pc2_executor, install_candidate)
            await _ensure_installed_and_manual(pc2_executor, removal_candidate)
            await _ensure_absent(pc1_executor, removal_candidate)
            for executor in (pc1_executor, pc2_executor):
                await _ensure_installed_and_manual(executor, hold_subject)

            held = await pc1_executor.run_command(
                f"sudo apt-mark hold {shlex.quote(hold_subject)}", login_shell=False, timeout=30.0
            )
            assert held.success, f"Failed to hold {hold_subject} on pc1: {held.stderr}"

            source_filename, key_filename = await _create_synthetic_repo_and_key(pc1_executor)
            pin_filename = await _create_synthetic_pin(pc1_executor)
            source_dest = f"{_APT_SOURCES_DIR}/{source_filename}"
            key_dest = f"{_APT_KEYRINGS_DIR}/{key_filename}"
            pin_dest = f"{_APT_PREFERENCES_DIR}/{pin_filename}"
            absent = await pc2_executor.run_command(
                " && ".join(f"test ! -e {shlex.quote(path)}" for path in (source_dest, key_dest, pin_dest)),
                login_shell=False,
                timeout=10.0,
            )
            assert absent.success, "synthetic /etc/apt files unexpectedly already present on pc2 before the run"

            # -- seed snap ---------------------------------------------------------------
            diverged = await pc2_executor.run_command(
                f"sudo snap refresh --revision={shlex.quote(alternate_revision)} {shlex.quote(revision_snap)}",
                login_shell=False,
                timeout=180.0,
            )
            assert diverged.success, (
                f"Failed to move pc2's {revision_snap} to revision {alternate_revision}: {diverged.stderr}"
            )
            snap_held = await pc1_executor.run_command(
                f"sudo snap refresh --hold=forever {shlex.quote(hold_snap)}", login_shell=False, timeout=60.0
            )
            assert snap_held.success, f"Failed to set a per-snap hold on pc1's {hold_snap}: {snap_held.stderr}"
            assert "held" not in await _snap_notes(pc2_executor, hold_snap), (
                f"{hold_snap} is already held on pc2 before the run; its replication would prove nothing"
            )

            # -- seed flatpak ------------------------------------------------------------
            source_remotes = await pc1_executor.run_command(
                f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
            )
            assert _FIXTURE_UNUSED_FLATPAK_REMOTE in nonblank_lines(source_remotes.stdout), (
                f"the fixture remote {_FIXTURE_UNUSED_FLATPAK_REMOTE} is not configured on pc1. It is created by "
                f"tests/integration/scripts/internal/vm-test-fixtures.sh.\n{source_remotes.stdout}"
            )
            filtered = await pc1_executor.run_command(
                f"mkdir --parents $HOME/.cache && printf %s {shlex.quote(_FLATPAK_FILTER_BODY)} > {filter_path} && "
                f"{sudo}flatpak remote-modify {scope_flag} --filter={filter_path} {shlex.quote(remote_name)}",
                login_shell=False,
                timeout=30.0,
            )
            assert filtered.success, (
                f"`flatpak remote-modify {scope_flag} --filter=` failed on pc1, so there is no filtered remote to "
                f"replicate: {filtered.stderr}"
            )
            _source_url, source_options = await _flatpak_remote_row(pc1_executor, remote_name, scope)
            assert _FLATPAK_FILTERED_OPTION in source_options, (
                f"pc1's {remote_name} reports options {source_options!r} after `flatpak remote-modify {scope_flag} "
                f"--filter=`, so this flatpak does not print the {_FLATPAK_FILTERED_OPTION!r} token "
                "`flatpak_sync._FILTERED_OPTION` reads"
            )
            recorded_filter_path = await _flatpak_remote_filter(pc1_executor, remote_name, scope) or ""
            assert recorded_filter_path, (
                f"pc1's {remote_name} names no file in `flatpak remotes {scope_flag} --columns=filter`, so this "
                "flatpak does not record the path `flatpak_sync` replicates"
            )

            # The app is absent on pc2 by fixture; deleting the remote is what removes pc2's
            # only trust in Flathub and makes the key replication load-bearing.
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(application)} || true; "
                f"{sudo}flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true",
                login_shell=False,
                timeout=120.0,
            )
            before_remotes = nonblank_lines(
                (
                    await pc2_executor.run_command(
                        f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
                    )
                ).stdout
            )
            assert remote_name not in before_remotes, (
                f"remote {remote_name} still configured on pc2, so this run cannot show it being provisioned"
            )
            assert _FIXTURE_UNUSED_FLATPAK_REMOTE not in before_remotes, (
                f"{_FIXTURE_UNUSED_FLATPAK_REMOTE} is already on pc2, so this run cannot show that it did not travel"
            )
            assert application not in [row[0] for row in await _flatpak_app_rows(pc2_executor)], (
                f"{application} still installed on pc2, so no approved application derives {remote_name}"
            )

            added = await pc2_executor.run_command(
                f"flatpak remote-add {scope_flag} {shlex.quote(target_only_remote)} "
                f"{shlex.quote(_FIXTURE_FLATPAK_REPOFILE)}",
                login_shell=False,
                timeout=180.0,
            )
            assert added.success, f"could not add the target-only remote {target_only_remote} to pc2: {added.stderr}"
            key_before = await pc2_executor.run_command(
                f"test -f {target_only_keyring}", login_shell=False, timeout=15.0
            )
            assert key_before.success, (
                f"pc2 holds no {target_only_keyring} for {target_only_remote}, so this flatpak does not keep a "
                "per-remote keyring and its absence after the deletion would prove nothing"
            )

            # -- seed manual installs ----------------------------------------------------
            await _create_unowned_marker(pc1_executor, unowned_path)
            await _author_snippet(
                pc1_executor,
                manual_item_id,
                unowned_path,
                f'mkdir --parents "$(dirname {replay_marker})" && touch {replay_marker}',
            )

            await _write_package_sync_config(
                pc1_executor,
                apt_sync=True,
                snap_sync=True,
                flatpak_sync=True,
                manual_installs_sync=True,
            )

            decisions = {
                AptPackageItem(name=install_candidate, version="").item_id: Decision.APPLY,
                AptPackageItem(name=removal_candidate, version="").item_id: Decision.APPLY,
                f"snap:{revision_snap}": Decision.APPLY,
                _snap_hold_item_id(hold_snap): Decision.APPLY,
                ref_item_id: Decision.APPLY,
                manual_item_id: Decision.APPLY,
            }
            automation = _automation_env_assignment_multi(decisions)

            # -- run 1: the rehearsal ----------------------------------------------------
            before_rehearsal = await _capture_machine_package_state(pc2_executor)
            rehearsal = await pc1_executor.run_command(
                f"{automation} pc-switcher sync pc2 --yes --dry-run", timeout=600.0, login_shell=True
            )
            assert rehearsal.success, (
                f"pc-switcher sync --dry-run exited {rehearsal.exit_code}.\n"
                f"stdout: {rehearsal.stdout}\nstderr: {rehearsal.stderr}"
            )
            after_rehearsal = await _capture_machine_package_state(pc2_executor)
            assert after_rehearsal == before_rehearsal, (
                "--dry-run changed pc2's package-manager state (ADR-014 violation).\n"
                f"before: {before_rehearsal}\nafter: {after_rehearsal}"
            )

            rehearsal_output = rehearsal.stdout + rehearsal.stderr
            previewed = _collapse_run_output(rehearsal_output)
            # The always-sync pin is previewed as a derived write, with no review entry.
            assert f"Would write {pin_dest}" in previewed, (
                f"always-sync pin {pin_dest!r} was not previewed as a derived write.\n{rehearsal_output}"
            )
            # The repository feeds no approved package, so nothing about it is written — and
            # it is offered in no direction, which is what makes "derived, never ticked" true.
            assert f"install {source_filename}" not in previewed, (
                f"repository {source_filename!r} was still offered as a review entry.\n{rehearsal_output}"
            )
            assert f"Would write {source_dest}" not in previewed, (
                f"repository {source_dest!r} was previewed as a derived write although no approved package needs "
                f"it.\n{rehearsal_output}"
            )
            # A key is previewed by `AptSyncJob.apply` for the repositories that survive the
            # run (`PKG-FR-DERIVED-VISIBLE`), and this repository is not one of them.
            assert f"Would write signing key {key_dest}" not in previewed, (
                f"signing key {key_dest!r} was previewed as a write for a repository no package needed.\n"
                f"{rehearsal_output}"
            )
            # The intended metadata refresh (the apt-get update the pin write requires) is
            # reported as its own marker item, by the label that item carries.
            assert "Would change Refresh apt package metadata (apt-get update)" in previewed, (
                f"intended apt-get update (metadata refresh) not reported.\n{rehearsal_output}"
            )

            # -- run 2: the converging run -----------------------------------------------
            source_before = await _capture_machine_package_state(pc1_executor)
            converge = await pc1_executor.run_command(
                f"{automation} pc-switcher sync pc2 --yes --allow-first-sync", timeout=900.0, login_shell=True
            )
            assert converge.success, (
                f"pc-switcher sync exited {converge.exit_code}.\nstdout: {converge.stdout}\nstderr: {converge.stderr}"
            )
            converge_output = converge.stdout + converge.stderr
            converge_collapsed = _collapse_run_output(converge_output)

            target_manual = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            assert install_candidate in target_manual, (
                f"{install_candidate} not reinstalled on pc2 after the sync.\n{converge_output}"
            )
            assert removal_candidate not in target_manual, (
                f"{removal_candidate} was not removed from pc2, so the removal direction converged nothing.\n"
                f"{converge_output}"
            )
            target_holds = await pc2_executor.run_command("apt-mark showhold", login_shell=False, timeout=15.0)
            assert hold_subject in nonblank_lines(target_holds.stdout), (
                f"pc1's hold on {hold_subject} did not reach pc2, although a block replicates without review.\n"
                f"{converge_output}"
            )
            assert f"reviewed {hold_subject} (hold)" not in converge_collapsed, (
                f"the hold on {hold_subject} was presented as a reviewed item -- a block is never a question"
            )

            assert await _snap_revision(pc2_executor, revision_snap) == source_snap_revision, (
                f"pc2's {revision_snap} did not converge to source revision {source_snap_revision}.\n{converge_output}"
            )
            replicated_notes = await _snap_notes(pc2_executor, hold_snap)
            assert "held" in replicated_notes, (
                f"pc1's per-snap hold on {hold_snap} did not reach pc2 (pc2 notes: {sorted(replicated_notes)}). "
                "If the source capture ran inside the orchestrator's system-wide refresh.hold window and saw no "
                "hold, #208 D9's capture-timing assumption is false and the capture must move earlier."
            )
            for executor, machine in ((pc1_executor, "pc1"), (pc2_executor, "pc2")):
                assert await _capture_system_refresh_hold(executor) is not None, (
                    f"the run left {machine} without the refresh.hold this test engaged -- D-06 forbids the "
                    "convergence mechanism from touching either machine's auto-refresh policy"
                )

            after_remotes = nonblank_lines(
                (
                    await pc2_executor.run_command(
                        f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
                    )
                ).stdout
            )
            assert remote_name in after_remotes, (
                f"remote {remote_name} not provisioned in scope {scope} on pc2 after sync"
            )
            assert _FIXTURE_UNUSED_FLATPAK_REMOTE not in after_remotes, (
                f"{_FIXTURE_UNUSED_FLATPAK_REMOTE} travelled to pc2 although no approved ref comes from it"
            )
            assert target_only_remote not in after_remotes, (
                f"{target_only_remote} is still configured on pc2, so the source-lacks-it deletion never happened.\n"
                f"{converge_output}"
            )
            key_after = await pc2_executor.run_command(
                f"test -f {target_only_keyring}", login_shell=False, timeout=15.0
            )
            assert not key_after.success, (
                f"{target_only_keyring} survived the deletion of {target_only_remote}: pc2 still trusts that "
                "vendor's signing key for a remote it no longer has"
            )
            assert application in [row[0] for row in await _flatpak_app_rows(pc2_executor)], (
                f"{application} not installed in scope {scope} on pc2 after sync.\n{converge_output}"
            )
            _target_url, target_options = await _flatpak_remote_row(pc2_executor, remote_name, scope)
            assert _FLATPAK_FILTERED_OPTION in target_options, (
                f"pc2's provisioned {remote_name} reports options {target_options!r}: the source's ref filter was "
                f"not applied there.\n{converge_output}"
            )
            assert await _flatpak_remote_filter(pc2_executor, remote_name, scope) == recorded_filter_path, (
                f"pc2's {remote_name} does not name {recorded_filter_path} as its ref filter -- the file must land "
                "at the same absolute path the source records"
            )
            copied = await pc2_executor.run_command(
                f"cat {shlex.quote(recorded_filter_path)}", login_shell=False, timeout=15.0
            )
            assert copied.success and copied.stdout == _FLATPAK_FILTER_BODY, (
                f"pc2's copy of the ref filter at {recorded_filter_path} is not the source's file byte-for-byte: "
                f"{copied.stdout!r} ({copied.stderr})"
            )
            # The one ordering exception this module's own prohibition carves out: the
            # remote's mere presence afterwards does not distinguish "remote added before
            # ref" from any other order, so only the run's own per-item converge log
            # (`PackageSyncJob._converge_one`) proves it.
            remote_marker = f"provision {scope} flatpak remote {remote_name}"
            ref_marker = f"install {ref} ("
            remote_index = converge_output.find(remote_marker)
            ref_index = converge_output.find(ref_marker)
            assert remote_index != -1, f"derived remote write log line not found: {remote_marker!r}"
            assert ref_index != -1, f"ref converge log line not found: {ref_marker!r}"
            assert remote_index < ref_index, "remote must be provisioned before the ref installs (D-14)"

            registry_exists = await pc2_executor.run_command(
                f"test -f {registry_relpath}", login_shell=False, timeout=10.0
            )
            assert registry_exists.success, (
                f"snippet registry not present on pc2 at {registry_relpath} after the run -- the push did not land"
            )
            replayed = await pc2_executor.run_command(f"test -f {replay_marker}", login_shell=False, timeout=10.0)
            assert replayed.success, (
                f"marker {replay_marker} absent on pc2 -- the pushed snippet was not replayed.\n{converge_output}"
            )

            source_after = await _capture_machine_package_state(pc1_executor)
            assert source_after == source_before, (
                "the run changed pc1's own package state: a sync must not change what software the source has, nor "
                f"where it gets it from.\nbefore: {source_before}\nafter: {source_after}"
            )

            # -- run 3: the fixed point --------------------------------------------------
            before_second = await _capture_machine_package_state(pc2_executor)
            # SKIP_ALWAYS, not APPLY: an APPLY on an item that is genuinely no longer a diff
            # and an APPLY on an item that was never presented are indistinguishable from the
            # end state, whereas a SKIP_ALWAYS leaves a decision-file trace iff the item WAS
            # presented.
            converged_item = AptPackageItem(name=install_candidate, version="").item_id
            second = await pc1_executor.run_command(
                f"{_automation_env_assignment_multi({converged_item: Decision.SKIP_ALWAYS})} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order",
                timeout=600.0,
                login_shell=True,
            )
            assert second.success, (
                f"second sync exited {second.exit_code}.\nstdout: {second.stdout}\nstderr: {second.stderr}"
            )
            after_second = await _capture_machine_package_state(pc2_executor)
            assert after_second == before_second, (
                "the second consecutive sync changed pc2's package-manager state -- the run is not a fixed point.\n"
                f"before: {before_second}\nafter: {after_second}"
            )
            source_entries = await DecisionFile("apt", pc1_executor).load()
            target_entries = await DecisionFile("apt", pc2_executor).load()
            assert converged_item not in source_entries and converged_item not in target_entries, (
                f"{install_candidate} was still presented in the second run's review (its SKIP_ALWAYS was "
                "recorded) -- a converged item must produce no diff at all"
            )

            # -- run 4: the source drops its ref filter ----------------------------------
            # Delete-and-re-add rather than `--no-filter`, for
            # `_restore_flatpak_source_baseline`'s own reason.
            await _restore_flatpak_source_baseline(pc1_executor, remote_name, scope, filter_path)
            assert await _flatpak_remote_filter(pc1_executor, remote_name, scope) is None, (
                f"pc1's {remote_name} still carries a ref filter, so run 4 cannot show a target-only one coming off"
            )
            # The app comes off pc2 again, and only the app: a filter is converged as part of
            # writing the remote an approved ref DERIVES, so a run 4 over a converged pair
            # would have no ref item, derive no remote, and leave the filter alone for a
            # reason that has nothing to do with what this asserts.
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(application)}",
                login_shell=False,
                timeout=120.0,
            )
            assert await _flatpak_remote_filter(pc2_executor, remote_name, scope) == recorded_filter_path, (
                f"pc2's {remote_name} lost its ref filter before run 4 started; there is no target-only filter to "
                "take off"
            )
            unfiltered = await pc1_executor.run_command(
                f"{automation} pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order",
                timeout=900.0,
                login_shell=True,
            )
            assert unfiltered.success, (
                f"fourth pc-switcher sync exited {unfiltered.exit_code}.\n"
                f"stdout: {unfiltered.stdout}\nstderr: {unfiltered.stderr}"
            )
            assert await _flatpak_remote_filter(pc2_executor, remote_name, scope) is None, (
                f"pc2's {remote_name} still carries a ref filter the source does not have -- the two machines would "
                f"never converge.\nstdout: {unfiltered.stdout}\nstderr: {unfiltered.stderr}"
            )
            assert application in [row[0] for row in await _flatpak_app_rows(pc2_executor)], (
                f"{application} was uninstalled from pc2 by the filter run.\n"
                f"stdout: {unfiltered.stdout}\nstderr: {unfiltered.stderr}"
            )
        finally:
            await _restore_flatpak_source_baseline(pc1_executor, remote_name, scope, filter_path)
            if recorded_filter_path:
                # The replicated copy is pc-switcher's own write and lives outside anything
                # `_restore_flatpak_target_baseline` knows about.
                await pc2_executor.run_command(
                    f"rm --force {shlex.quote(recorded_filter_path)}", login_shell=False, timeout=15.0
                )
            await pc2_executor.run_command(
                f"flatpak remote-delete {scope_flag} --force {shlex.quote(target_only_remote)} || true; "
                f"rm --force {target_only_keyring}",
                login_shell=False,
                timeout=60.0,
            )
            await _restore_flatpak_target_baseline(pc2_executor)

            # The holds come off and the revisions do not: a per-snap hold is state every
            # later scenario reads, while which revision pc2 ends on is nobody's precondition
            # (`_snap_subjects`).
            for executor in (pc1_executor, pc2_executor):
                await executor.run_command(
                    f"sudo snap refresh --unhold {shlex.quote(hold_snap)}", login_shell=False, timeout=60.0
                )

            for executor in (pc1_executor, pc2_executor):
                await executor.run_command(
                    f"sudo apt-mark unhold {shlex.quote(hold_subject)}", login_shell=False, timeout=30.0
                )

            cleanup_paths = " ".join(
                shlex.quote(f"{directory}/{filename}")
                for directory, filename in (
                    (_APT_SOURCES_DIR, source_filename),
                    (_APT_KEYRINGS_DIR, key_filename),
                    (_APT_PREFERENCES_DIR, pin_filename),
                )
                if filename
            )
            if cleanup_paths:
                for executor in (pc1_executor, pc2_executor):
                    await executor.run_command(f"sudo rm --force {cleanup_paths}", login_shell=False, timeout=15.0)

            await _remove_unowned_marker(pc1_executor, unowned_path)
            await pc2_executor.run_command(
                f"rm --force {replay_marker} {registry_relpath}", login_shell=False, timeout=15.0
            )
            await pc1_executor.run_command(f"rm --force {registry_relpath}", login_shell=False, timeout=15.0)

            await _restore_system_refresh_hold(pc1_executor, pc1_prior_hold)
            await _restore_system_refresh_hold(pc2_executor, pc2_prior_hold)


class TestTheAptOriginModelOnRealRepositories:
    """`PKG-FR-APT-ORIGIN-DERIVED` and `PKG-FR-APT-ORIGIN-VERIFY` against a repository apt
    really fetches from.

    Everything the unit tier proves here it proves against `apt-cache policy` output written
    by hand, which is the one thing the model is about: which version apt would install, from
    which of several repositories, once priorities and version ordering are applied. A run
    that carries a repository and its key to the target and then installs from it is the only
    place that arithmetic is done by apt rather than by the test.

    Both halves share one declaration of the vendor repository on the source, which is a
    network fetch and an `apt-get update` (#216): the second half purges what the first
    installed on the target and publishes the rival there, so the two runs differ in the
    target's own arithmetic and in nothing else.
    """

    async def test_the_vendor_repository_travels_and_then_loses_to_a_rival_on_the_target(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: _AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """A29, A41, A42.

        Run 1: pc1 has a package from a vendor repository pc2 does not have; approving the
        install carries that repository and its signing key across, and pc2's own apt then
        installs the vendor's build. The whole chain is asserted on pc2's own state -- the
        `.sources` file and the keyring the approval derived, the package in `apt-mark
        showmanual`, and `apt-cache policy` naming the vendor as where it came from.

        Between the runs pc2 loses that package again and gains a rival: another repository
        offering the same name at a higher version, and a pin holding the vendor's build at
        priority 1.

        Run 2: the vendor's repository is on pc2 and still does not win, so after the run's
        `apt-get update` pc2's candidate is somebody else's software. That install is refused
        as its own failure naming both origins, and nothing of that name is installed. A
        second, ordinary install approved in the same run lands anyway, which is what makes
        the refusal one item's failure rather than the run's.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        other_candidate = apt_subjects.install_direction[0]
        vendor_item_id = AptPackageItem(name=_VENDOR_PACKAGE, version="").item_id
        source_filename = key_filename = ""
        repo_dir = list_filename = pin_filename = ""
        try:
            source_filename, key_filename = await _install_from_the_vendor_repository(pc1_executor)
            source_dest = f"{_APT_SOURCES_DIR}/{source_filename}"
            key_dest = f"{_APT_KEYRINGS_DIR}/{key_filename}"

            absent = await pc2_executor.run_command(
                f"test ! -e {shlex.quote(source_dest)} && test ! -e {shlex.quote(key_dest)}",
                login_shell=False,
                timeout=10.0,
            )
            assert absent.success, "the vendor repository is already on pc2, so nothing here would be derived"

            await _write_apt_sync_config(pc1_executor)

            # -- run 1: the repository travels and the vendor's build lands --------------
            first = await pc1_executor.run_command(
                f"{_automation_env_assignment(vendor_item_id)} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=600.0,
                login_shell=True,
            )
            assert first.success, (
                f"pc-switcher sync exited {first.exit_code}.\nstdout: {first.stdout}\nstderr: {first.stderr}"
            )

            landed = await pc2_executor.run_command(
                f"sudo test -f {shlex.quote(source_dest)} && sudo test -f {shlex.quote(key_dest)}",
                login_shell=False,
                timeout=10.0,
            )
            assert landed.success, (
                f"the approved install did not carry {source_dest} and {key_dest} to pc2.\n"
                f"stdout: {first.stdout}\nstderr: {first.stderr}"
            )
            manual = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert _VENDOR_PACKAGE in nonblank_lines(manual.stdout), (
                f"{_VENDOR_PACKAGE} was not installed on pc2.\nstdout: {first.stdout}\nstderr: {first.stderr}"
            )
            policy = await pc2_executor.run_command(
                f"apt-cache policy {shlex.quote(_VENDOR_PACKAGE)}", login_shell=False, timeout=30.0
            )
            assert _VENDOR_REPO_URI.removeprefix("https://") in policy.stdout, (
                f"pc2 has {_VENDOR_PACKAGE} but apt names no {_VENDOR_REPO_URI} version for it, so the copy that "
                f"landed is not the vendor's.\n{policy.stdout}"
            )

            # -- between the runs: pc2 loses the package and gains a rival for its name ---
            purged = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get purge --assume-yes {shlex.quote(_VENDOR_PACKAGE)}",
                login_shell=False,
                timeout=180.0,
            )
            assert purged.success, f"Failed to purge {_VENDOR_PACKAGE} from pc2 between the runs: {purged.stderr}"
            repo_dir, list_filename, pin_filename = await _publish_a_rival_candidate(pc2_executor)

            await _ensure_installed_and_manual(pc1_executor, other_candidate)
            await _ensure_absent(pc2_executor, other_candidate)

            # -- run 2: the target's own arithmetic refuses the vendor's build -----------
            decisions = {
                vendor_item_id: Decision.APPLY,
                AptPackageItem(name=other_candidate, version="").item_id: Decision.APPLY,
            }
            second = await pc1_executor.run_command(
                f"{_automation_env_assignment_multi(decisions)} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order",
                timeout=600.0,
                login_shell=True,
            )
            assert not second.success, (
                "a refused install is its own failure, so the run must not report success.\n"
                f"stdout: {second.stdout}\nstderr: {second.stderr}"
            )

            installed = parse_dpkg_installed(
                (
                    await pc2_executor.run_command(
                        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
                    )
                ).stdout
            )
            assert _VENDOR_PACKAGE not in installed, (
                f"pc2 installed {_VENDOR_PACKAGE} from {repo_dir} -- the verification let through a build that is "
                f"not the one pc1 has"
            )
            assert other_candidate in installed, (
                f"{other_candidate} was not installed, so the refusal ended the run instead of failing one item.\n"
                f"stdout: {second.stdout}\nstderr: {second.stderr}"
            )

            collapsed = _collapse_run_output(second.stdout + second.stderr)
            assert f"{_VENDOR_PACKAGE} was not installed:" in collapsed, (
                f"the run did not report {_VENDOR_PACKAGE} as refused.\n"
                f"stdout: {second.stdout}\nstderr: {second.stderr}"
            )
            assert (
                f"has it from {_VENDOR_REPO_URI.removeprefix('https://')}, but after this run's apt-get update"
                in collapsed
            ), f"the refusal did not name the origin pc1 has it from.\n{collapsed}"
            assert f"would install it from {repo_dir}" in collapsed, (
                f"the refusal did not name the origin pc2 would have taken it from.\n{collapsed}"
            )
        finally:
            if repo_dir:
                await _remove_the_rival_candidate(pc2_executor, repo_dir, list_filename, pin_filename)
            if source_filename:
                await _undeclare_the_vendor_repository(pc2_executor, source_filename, key_filename)
                await _undeclare_the_vendor_repository(pc1_executor, source_filename, key_filename)


class TestARunWithNobodyToAsk:
    """Everything a single non-interactive run says and refuses to do (`PKG-FR-NO-TERMINAL`,
    `PKG-FR-LOG-DECISIONS`).

    The runs these claims used to make one each were identical in every respect that costs
    wall clock: no `PACKAGE_REVIEW_AUTOMATION_ENV`, no TTY on stdin or stdout (the default
    for a command run through this fixture's plain SSH exec, which requests no pty), and
    therefore nothing applied anywhere. What differs between them is only which manager's
    groups they read, so they share one run here (#216).

    A run with nobody to ask is also the only run that PRINTS every group, which is why the
    two claims whose subject is legitimately the run's own output live here: a `REPORT_ONLY`
    flatpak diff changes nothing anywhere, and an apt item the target's own apt cannot
    resolve is proven to have reached a review by the group that named it. The automation
    hook answers a review without ever printing it, so neither could be read from a run that
    used it.
    """

    async def test_a_non_interactive_run_names_every_item_applies_none_and_prints_each_group(  # noqa: PLR0915
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: _AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H162, J9, J12, J14, J37, J44, J49, J103, A30, J98, G28 — and ADR-020 D-41's
        `ORIGIN_MISMATCH` at VM level.

        Four seeded divergences, one per claim, and one run:

        - an apt package removed from pc2 and another promoted to manual there, so an item
          exists in each direction: nothing is applied, no permanent decision is recorded on
          either machine, both items are NAMED as declined, and the job itself is reported
          skipped;
        - an apt package pc1 installed from a `file:` repository pc2 has never heard of
          (ADR-020 D-34 class 3): the target's apt refuses to rehearse any transaction
          containing the name, and the whole run survives it -- proven by the review group
          that names the package rather than by a run that quietly dropped it;
        - the fixture flatpak installed on pc2 from the real Flathub with pc2's `flathub`
          then repointed at the beta repository's URL: both machines print the same origin
          NAME, so a comparison by name is provably blind to it and only D-41's URL
          comparison can produce the finding, which is reported naming both vendors and
          converged by nothing;
        - one unowned `/opt` path on pc1, so the unreproducible scan has a finding of its own
          and its refusal to name any part of the stock `/usr/local` skeleton is a real claim
          rather than a scan that found nothing.

        This run is deliberately NOT a `--dry-run`. `PKG-FR-NO-TERMINAL` ends every package
        job before `apply()` when there is nobody to ask, which is the same protection for
        pc2 and the condition the printed groups above depend on; a rehearsal would add a
        second reason for the same silence and make neither observable.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        install_candidate = apt_subjects.install_direction[0]
        removal_candidate = await _create_extra_on_target_apt_package(pc1_executor, pc2_executor)
        application, _version, scope, remote_name, source_url, ref = await _flatpak_subject(pc1_executor)
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""
        witness_path = f"/opt/pcswitcher-it-scan-{uuid4().hex[:12]}"

        # The fixture's second remote supplies a real, differently-vendored URL, so nothing
        # here invents one. Both Flathub keyrings share a sha256 (measured,
        # vm-test-fixtures.sh), which is why the URL -- never a key digest -- is the whole
        # evidence.
        beta_url, _beta_options = await _flatpak_remote_row(pc1_executor, _FIXTURE_UNUSED_FLATPAK_REMOTE, scope)
        assert beta_url != source_url, (
            f"pc1's {remote_name} and {_FIXTURE_UNUSED_FLATPAK_REMOTE} both report {source_url!r}, so no vendor "
            "divergence can be built from the fixture remotes "
            "(tests/integration/scripts/internal/vm-test-fixtures.sh)"
        )

        unlocatable = repo_dir = list_filename = ""
        try:
            await _ensure_installed_and_manual(pc1_executor, install_candidate)
            await _ensure_absent(pc2_executor, install_candidate)

            unlocatable, repo_dir, list_filename = await _install_from_a_repo_the_target_lacks(pc1_executor)
            # The precondition, asserted rather than assumed: without it the run below proves
            # nothing, because a target that CAN resolve the name never had the defect.
            refused = await pc2_executor.run_command(
                f"apt-get --dry-run install --assume-yes --no-install-recommends {shlex.quote(unlocatable)}",
                login_shell=False,
                timeout=60.0,
            )
            assert not refused.success, (
                f"pc2 resolved {unlocatable}, so this run cannot exercise the class-3 path.\n"
                f"stdout: {refused.stdout}\nstderr: {refused.stderr}"
            )

            install = await pc2_executor.run_command(
                f"{sudo}flatpak install {scope_flag} --assumeyes --noninteractive "
                f"{shlex.quote(remote_name)} {shlex.quote(ref)}",
                login_shell=False,
                timeout=600.0,
            )
            assert install.success, (
                f"failed to install {ref} on pc2 from {remote_name}, so the two machines never share the ref this "
                f"test diverges: {install.stderr}"
            )
            target_rows = [row for row in await _flatpak_app_rows(pc2_executor) if row[4] == ref]
            assert target_rows, f"{ref} is not installed on pc2 after the install; there is no shared ref to diverge"
            assert target_rows[0][2] == remote_name, (
                f"pc2 reports origin {target_rows[0][2]!r} for {ref}, not {remote_name!r} -- the two machines must "
                "print the SAME origin name for the name comparison to be provably blind to this divergence"
            )
            repoint = await pc2_executor.run_command(
                f"{sudo}flatpak remote-modify {scope_flag} --url={shlex.quote(beta_url)} {shlex.quote(remote_name)}",
                login_shell=False,
                timeout=30.0,
            )
            assert repoint.success, f"failed to repoint pc2's {remote_name} at {beta_url}: {repoint.stderr}"
            target_url, _target_options = await _flatpak_remote_row(pc2_executor, remote_name, scope)
            assert target_url != source_url, (
                f"pc2's {remote_name} still reports {target_url!r} after the repoint, so both machines' copies of "
                f"{ref} still come from one vendor and this run cannot exercise ORIGIN_MISMATCH"
            )

            await _create_unowned_marker(pc1_executor, witness_path)

            await _write_package_sync_config(pc1_executor, apt_sync=True, flatpak_sync=True, manual_installs_sync=True)

            manual_before = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            flatpak_before = await _flatpak_app_rows(pc2_executor)
            pc1_decision_before = await _decision_file_exists(pc1_executor, "apt")
            pc2_decision_before = await _decision_file_exists(pc2_executor, "apt")

            # No automation env prefix and no pty on this exec -- genuinely non-interactive
            # on both stdin and stdout, D-26's actual trigger condition.
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync", timeout=900.0, login_shell=True
            )
            assert sync_result.success, (
                "non-interactive sync unexpectedly failed (D-26's skip-all must not fail the job).\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            manual_after = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            assert manual_after == manual_before, (
                "non-interactive run changed pc2's apt-mark showmanual -- D-26 requires nothing applied"
            )
            assert await _flatpak_app_rows(pc2_executor) == flatpak_before, (
                "the run changed pc2's installed refs; an ORIGIN_MISMATCH is reported and converged by nothing."
            )
            assert await _decision_file_exists(pc1_executor, "apt") == pc1_decision_before, (
                "non-interactive run created/removed a decision file on pc1"
            )
            assert await _decision_file_exists(pc2_executor, "apt") == pc2_decision_before, (
                "non-interactive run created/removed a decision file on pc2"
            )

            combined_output = sync_result.stdout + sync_result.stderr
            # A trailing space so the last finding on the line has the same right-hand
            # boundary as every other one (see the skeleton check below).
            collapsed = f"{_collapse_run_output(combined_output)} "

            # `PKG-FR-LOG-DECISIONS` requires the run to NAME each item nobody could be asked
            # about, so a count would no longer say which ones were declined; and
            # `PKG-FR-NO-TERMINAL` requires the job itself to be reported skipped.
            for candidate, direction in ((install_candidate, "install"), (removal_candidate, "removal")):
                assert f"{_UNASKED_ITEM_MARKER}{candidate} " in collapsed, (
                    f"{direction}-direction item {candidate} was not named as declined for this run.\n"
                    f"{combined_output}"
                )
            assert "Job apt_sync skipped: non-interactive run left every apt review item undecided" in collapsed, (
                f"the run did not report apt_sync as skipped (PKG-FR-NO-TERMINAL).\n{combined_output}"
            )

            # The group panel's own title is the witness that the class-3 package reached a
            # review at all rather than being dropped to keep the run alive.
            assert "Install apt packages" in collapsed, (
                f"the run drew no apt install review group at all.\n{combined_output}"
            )
            assert f"install {unlocatable}" in collapsed, (
                f"{unlocatable} reached no review line, so the run survived by dropping it.\n{combined_output}"
            )
            assert "Unable to locate package" not in combined_output, (
                f"apt's plan-time refusal still surfaced as a run-level failure.\n{combined_output}"
            )

            # A report group is titled by its CAUSE (`sync_core._REPORT_TITLES`), so this
            # asserts the mismatch reached the ORIGIN_MISMATCH group specifically rather than
            # any report group at all. The discriminating pair: a VERSION_MISMATCH -- what
            # this diverged pair would produce if the vendor comparison missed -- names two
            # versions and no URL at all.
            assert "Installed from different remotes (flatpak applications)" in collapsed, (
                f"the mismatch reached no origin-mismatch review group.\n{combined_output}"
            )
            assert ref in combined_output, f"the report does not name the ref {ref}.\n{combined_output}"
            assert source_url in combined_output, (
                f"the report does not name the source's vendor {source_url}.\n{combined_output}"
            )
            assert target_url in combined_output, (
                f"the report does not name the target's vendor {target_url}.\n{combined_output}"
            )

            assert f"{_UNASKED_ITEM_MARKER}{witness_path} " in collapsed, (
                f"the scan did not name {witness_path}, so this run says nothing about what it names.\n"
                f"{combined_output}"
            )
            for stock in _STOCK_DIRECTORIES:
                # The trailing space is the boundary: `/usr/local/bin` must not satisfy the
                # check for `/usr/local`.
                assert f"{_UNASKED_ITEM_MARKER}{stock} " not in collapsed, (
                    f"the scan reported {stock}, a directory the distribution itself creates, so every user "
                    f"would be asked to write an install snippet for a stock directory on every run.\n"
                    f"{combined_output}"
                )
        finally:
            await _remove_unowned_marker(pc1_executor, witness_path)
            if repo_dir:
                await _undeclare_local_repository(pc1_executor, repo_dir, list_filename)
            # `_restore_flatpak_target_baseline` re-adds with `--if-not-exists`, which cannot
            # repair a URL, so the repointed remote is deleted here first.
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(application)} || true; "
                f"{sudo}flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true",
                login_shell=False,
                timeout=120.0,
            )
            await _restore_flatpak_target_baseline(pc2_executor)
            await _restore_auto_marked_package(pc2_executor, removal_candidate)


class TestWhatFolderSyncMayAndMayNotCarry:
    """The two boundaries `folder_sync` has to honour, where each hands off from a package
    job (`PKG-FR-SNAP-DATA-BOUNDARY`, `PKG-FR-REGISTRY-CONSENT`), and the snap a whole run
    must not touch at all (`PKG-FR-SNAP-SIDELOAD`).

    The unit tests compute the snap exclusion from a revision map a test wrote into them and
    assert the `--filter` argument built for the registry. What they cannot show is where
    either comes from: `folder_sync` asking the real target machine mid-run, after the
    package jobs have gone (`PKG-FR-JOB-ORDER`), and both arguments holding against real
    rsync over the directories these files actually live in.

    All three claims want the same run: `snap_sync` and `manual_installs_sync` converging
    nothing, `folder_sync` mirroring two real directories with `--delete`. They share it
    (#216). The run has no terminal, which is one of the two shapes in which the registry
    push is never made and is also what declines the one snap install the boundary test needs
    declined.

    Both machines' real `~/snap` is set aside for the duration: the mirror deletes, so a
    hermetic tree is the only way the transfer's outcome is exactly what this test built.
    """

    async def test_the_mirror_honours_the_snap_revision_boundary_and_the_registrys_consent(  # noqa: PLR0915
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E113, E115, K88, E49.

        The `~/snap` tree pc1 offers holds two apps, each answering one of the two shapes
        pc2's own `snap list` can give:

        - the first fixture snap, which pc2 is active at a revision of: that revision's data
          directory reaches pc2 and one for a revision no snapd anywhere has ever installed
          does not;
        - the second, removed from pc2 with `--purge` and left declined by this run: no
          revision directory of it reaches pc2 at all, while `~/snap/<app>/common`, which
          belongs to no revision, does -- the witness that the mirror reached the app's tree
          and the absence is the exclusion at work.

        The registry's own directory is mirrored too, with both machines holding registries
        that disagree -- one entry each, neither known to the other -- which is exactly the
        loss `PKG-FR-REGISTRY-CONSENT` exists to put a question in front of. pc2 ends the run
        holding its own registry entry for entry, and a file of pc1's own beside it arrives,
        which is what makes the registry's survival evidence about the exclusion rather than
        about a mirror that never covered the directory.

        pc1 also carries a sideloaded snap for the whole run. Both machines' complete
        `snap list` listings are compared across the run, so "the run does nothing about it"
        includes not installing it on pc2, not removing it from pc1, and not moving anything
        else while it is there. snapd's automatic refresh is paused on both machines
        throughout (the same timed `refresh.hold` a sync engages, restored exactly
        afterwards) so a background refresh cannot change a revision between the two listings
        and be read as the run's doing.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        home = await _home_dir(pc1_executor)
        assert await _home_dir(pc2_executor) == home, (
            "the two machines' SSH users have different home directories, so `~/snap` and the registry's directory "
            "are not one path each to mirror"
        )
        snap_root = f"{home}/snap"
        registry_dir = f"{home}/{_REGISTRY_DIR_RELPATH}"

        held_app, absent_app = await _snap_subjects(pc1_executor, pc2_executor, count=2)
        held_revision = await _snap_revision(pc2_executor, held_app)
        absent_source_revision = await _snap_revision(pc1_executor, absent_app)
        absent_target_revision = await _snap_revision(pc2_executor, absent_app)
        assert held_revision and absent_source_revision and absent_target_revision, (
            f"{held_app} and {absent_app} must both be installed on both machines"
        )
        stale_revision = str(int(held_revision) + 1000) if held_revision.isdigit() else f"{held_revision}0"

        uniq = uuid4().hex[:12]
        held_marker = f"{snap_root}/{held_app}/{held_revision}/pcswitcher-it-{uniq}"
        stale_dir = f"{snap_root}/{held_app}/{stale_revision}"
        stale_marker = f"{stale_dir}/pcswitcher-it-{uniq}"
        absent_revision_dir = f"{snap_root}/{absent_app}/{absent_source_revision}"
        absent_revision_marker = f"{absent_revision_dir}/pcswitcher-it-{uniq}"
        common_marker = f"{snap_root}/{absent_app}/common/pcswitcher-it-{uniq}"

        sideload_name = f"pcswitcher-it-sideload-{uniq}"
        sideload_dir = f"/var/tmp/pcswitcher-it-sideload-{uniq}"

        source_path = f"/opt/pcswitcher-it-registry-source-{uniq}"
        source_item = _unowned_item_id(source_path)
        target_path = f"/opt/pcswitcher-it-registry-target-{uniq}"
        target_item = _unowned_item_id(target_path)
        target_body = f"# pc2's own snippet {uniq}"
        travelling = f"{registry_dir}/pcswitcher-it-{uniq}"

        pc1_prior_hold = await _capture_system_refresh_hold(pc1_executor)
        pc2_prior_hold = await _capture_system_refresh_hold(pc2_executor)
        source_aside = target_aside = ""
        try:
            await _engage_system_refresh_hold(pc1_executor)
            await _engage_system_refresh_hold(pc2_executor)

            # `--purge` leaves snapd no snapshot behind, so removing it here costs the next
            # scenario an install (`_snap_subjects`) and nothing more.
            purged = await pc2_executor.run_command(
                f"sudo snap remove --purge {shlex.quote(absent_app)}", login_shell=False, timeout=180.0
            )
            assert purged.success, f"could not remove {absent_app} from pc2: {purged.stderr}"
            assert await _snap_revision(pc2_executor, absent_app) is None, (
                f"{absent_app} is still installed on pc2 after `snap remove --purge`, so pc2 holds a revision of it "
                "and this run cannot exercise the branch"
            )

            base = await _installed_base_snap(pc1_executor)
            await _create_sideloaded_snap(pc1_executor, sideload_dir, sideload_name, base)

            # Captured AFTER the sideload and the purge, so what the run is held to is the
            # machines as it finds them, and set aside AFTER the capture so the sideload's own
            # `~/snap` entry travels with the rest of the real tree.
            pc1_snaps_before = parse_snap_list_names_revisions(
                (await pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0)).stdout
            )
            pc2_snaps_before = parse_snap_list_names_revisions(
                (await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)).stdout
            )
            assert pc1_snaps_before.get(sideload_name, "").startswith("x"), (
                f"pc1's {sideload_name} is at revision {pc1_snaps_before.get(sideload_name)!r}, not a sideloaded "
                "`x`-prefixed one, so this run cannot exercise the sideload branch"
            )
            assert sideload_name not in pc2_snaps_before, f"{sideload_name} is somehow already on pc2"

            source_aside = await _take_paths_aside(pc1_executor, [snap_root])
            target_aside = await _take_paths_aside(pc2_executor, [snap_root])

            # `current` decides which revision dir the source offers at all; without it every
            # one of an app's revision dirs is excluded and the run below proves nothing.
            markers = (held_marker, stale_marker, absent_revision_marker, common_marker)
            build = "\n".join(
                ["set -eu"]
                + [f"mkdir --parents {shlex.quote(path.rsplit('/', 1)[0])}" for path in markers]
                + [f"printf %s {uniq} > {shlex.quote(path)}" for path in markers]
                + [
                    f"ln --symbolic --no-dereference --force {shlex.quote(revision)} "
                    f"{shlex.quote(f'{snap_root}/{app}/current')}"
                    for app, revision in ((held_app, held_revision), (absent_app, absent_source_revision))
                ]
            )
            built = await pc1_executor.run_command(build, login_shell=False, timeout=30.0)
            assert built.success, f"could not build the ~/snap fixture on pc1: {built.stderr}"

            await _create_unowned_marker(pc1_executor, source_path)
            await _author_snippet(pc1_executor, source_item, source_path, f"touch /tmp/pcswitcher-it-{uniq}")
            await _author_snippet(pc2_executor, target_item, target_path, target_body)

            await _write_package_sync_config(
                pc1_executor,
                extra_sections=_folder_sync_section(snap_root, registry_dir),
                snap_sync=True,
                manual_installs_sync=True,
                folder_sync=True,
            )
            seeded = await pc1_executor.run_command(
                f"printf %s {uniq} > {shlex.quote(travelling)}", login_shell=False, timeout=15.0
            )
            assert seeded.success, f"could not seed {travelling} on pc1: {seeded.stderr}"

            # No pty and no automation hook: nobody is there to answer, so
            # `manual_installs_sync` makes no push and the mirror is the only route left to
            # the registry, and the one snap install below stays declined.
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync", timeout=900.0, login_shell=True
            )
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            assert await _snap_revision(pc2_executor, absent_app) is None, (
                f"{absent_app} was installed on pc2 although nobody approved it, so pc2 holds a revision of it and "
                f"the absence below would prove nothing.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            listing = await pc2_executor.run_command(
                f"find {shlex.quote(snap_root)} -mindepth 1 | sort", login_shell=False, timeout=30.0
            )
            assert listing.success, f"could not read pc2's {snap_root}: {listing.stderr}"
            arrived = set(nonblank_lines(listing.stdout))

            assert held_marker in arrived, (
                f"{held_marker} did not reach pc2, which is itself active at revision {held_revision} of "
                f"{held_app}.\n{listing.stdout}"
            )
            assert not any(path == stale_dir or path.startswith(f"{stale_dir}/") for path in arrived), (
                f"a data directory for revision {stale_revision} of {held_app} exists on pc2, whose snapd is on "
                f"{held_revision} and never installed {stale_revision}.\n{listing.stdout}"
            )
            assert common_marker in arrived, (
                f"{common_marker} did not reach pc2, so the mirror never reached {absent_app}'s tree at all and the "
                f"absence of its revision directory says nothing.\n{listing.stdout}"
            )
            assert not any(
                path == absent_revision_dir or path.startswith(f"{absent_revision_dir}/") for path in arrived
            ), (
                f"a data directory for revision {absent_source_revision} of {absent_app} exists on pc2, whose snapd "
                f"holds no revision of that app at all.\n{listing.stdout}"
            )

            landed = await pc2_executor.run_command(f"cat {shlex.quote(travelling)}", login_shell=False, timeout=15.0)
            assert landed.success and landed.stdout.strip() == uniq, (
                f"{travelling} did not reach pc2, so the mirror never covered the directory the registry lives in "
                f"and the registry surviving below says nothing.\nstdout: {landed.stdout}\nstderr: {landed.stderr}"
            )
            entries = await SnippetRegistry(pc2_executor).load()
            assert source_item not in entries, (
                f"pc1's snippet for {source_path} is in pc2's registry although nobody was asked: the registry "
                f"reached pc2 without the question that is its only route.\nregistry holds: {sorted(entries)}"
            )
            assert set(entries) == {target_item}, (
                f"pc2's registry is no longer its own: it holds {sorted(entries)} rather than the single entry "
                f"{target_item} pc2 had before the run"
            )
            assert entries[target_item].body == target_body, (
                f"pc2's own entry for {target_path} was overwritten: its body reads {entries[target_item].body!r} "
                f"rather than {target_body!r}"
            )

            pc1_snaps_after = parse_snap_list_names_revisions(
                (await pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0)).stdout
            )
            pc2_snaps_after = parse_snap_list_names_revisions(
                (await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)).stdout
            )
            assert pc1_snaps_after == pc1_snaps_before, (
                f"the run changed pc1's own snaps.\nbefore: {pc1_snaps_before}\nafter: {pc1_snaps_after}"
            )
            assert pc2_snaps_after == pc2_snaps_before, (
                f"the run changed pc2's snaps although nothing about them was approved.\n"
                f"before: {pc2_snaps_before}\nafter: {pc2_snaps_after}"
            )
        finally:
            await _remove_unowned_marker(pc1_executor, source_path)
            if source_aside:
                await _put_paths_back(pc1_executor, source_aside, [snap_root])
            if target_aside:
                await _put_paths_back(pc2_executor, target_aside, [snap_root])
            await _remove_sideloaded_snap(pc1_executor, sideload_dir, sideload_name)
            await _restore_system_refresh_hold(pc1_executor, pc1_prior_hold)
            await _restore_system_refresh_hold(pc2_executor, pc2_prior_hold)


class TestSkipAlwaysIsInertInBothRoles:
    """D-08's permanent skip, recorded once and then held against every later run, in both
    roles a machine can play.

    The ordinary review checkbox has no UI path to SKIP_ALWAYS yet for a regular item
    (`packages.review`'s own docstring: only the unreproducible items' three-way prompt and a
    hand-constructed `ReviewOutcome` reach it today) -- this drives it through the same
    `PACKAGE_REVIEW_AUTOMATION_ENV` hook every other test in this module uses, proving the
    underlying mechanism (`PackageSyncJob._record_permanent_skips`/`filter_inert`)
    independent of that UI gap.

    Two item shapes are recorded in the same first run: an ordinary apt package, which is
    source-held for the direction it was decided in, and a package installed straight from a
    `.deb`, which is unreproducible and therefore always source-held (D-08a). One shape is
    what the hook can already express and the other is what the real UI already offers, and
    the same three runs settle both (#216).
    """

    async def test_a_recorded_skip_always_survives_a_forced_apply_in_either_direction(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: _AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H125, H126, H166, N1, N2, G27.

        Run 1 records SKIP_ALWAYS for both items and applies neither. The apt entry lands in
        pc1's apt decision file and the `.deb` entry in its manual one -- and that second
        entry is also the whole witness that a hand-installed package reached the review at
        all: SKIP_ALWAYS is recorded against an item only if the review presented it
        (`_finalize_unreproducible`), and `reset_pcswitcher_state` leaves pc1 holding no
        decision file before the run.

        What only a real apt can settle about that item: a package installed straight from a
        `.deb` has its INSTALLED version as its own candidate and no repository origin at
        all, so the detection rests on what apt genuinely prints for such a package rather
        than on policy output a test author composed. Nothing marks it manually installed
        either -- `dpkg --install` is the whole setup -- and the scan reads the INSTALLED set.

        Run 2 force-maps both items to APPLY in the same direction. If D-08's inertness
        holds, neither becomes a diff at all, so the mapping has nothing to attach to:
        proven by the apt package staying absent from pc2 despite being asked for, and by the
        `.deb` entry still reading SKIP_ALWAYS rather than being presented again.

        Run 3 reverses the roles. The decision lives on pc1, now the TARGET, and D-08
        promises inertness there too -- so force-mapping the same apt item to APPLY (which,
        if a diff existed at all, would mean REMOVE, since pc1 genuinely still has the
        package) must still leave it untouched.

        `--allow-out-of-order` bypasses the unrelated W3 consecutive-push gate a second
        same-direction sync would otherwise trip (ADR-015) -- orthogonal to what this proves.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = apt_subjects.install_direction[0]
        apt_item_id = AptPackageItem(name=candidate, version="").item_id

        hand_deb = ""
        try:
            await _ensure_installed_and_manual(pc1_executor, candidate)
            await _ensure_absent(pc2_executor, candidate)

            hand_deb = await _install_a_hand_downloaded_deb(pc1_executor)
            deb_item_id = _no_candidate_item_id(hand_deb)
            # The precondition, asserted rather than assumed: apt must name no repository for
            # the installed version, or the item this run is about was never detectable.
            policy = await pc1_executor.run_command(
                f"LC_ALL=C apt-cache policy {shlex.quote(hand_deb)}", login_shell=False, timeout=30.0
            )
            assert policy.success and "1.0" in policy.stdout, (
                f"apt says nothing about the hand-installed {hand_deb}.\n"
                f"stdout: {policy.stdout}\nstderr: {policy.stderr}"
            )
            assert "http" not in policy.stdout, (
                f"apt names a repository origin for the hand-installed {hand_deb}, so it is reproducible after all "
                f"and this run cannot exercise the branch.\n{policy.stdout}"
            )

            await _write_package_sync_config(pc1_executor, apt_sync=True, manual_installs_sync=True)

            # -- run 1: record -----------------------------------------------------------
            skip_always = {apt_item_id: Decision.SKIP_ALWAYS, deb_item_id: Decision.SKIP_ALWAYS}
            first = await pc1_executor.run_command(
                f"{_automation_env_assignment_multi(skip_always)} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,
                login_shell=True,
            )
            assert first.success, (
                f"skip-always run unexpectedly failed.\nstdout: {first.stdout}\nstderr: {first.stderr}"
            )

            apt_entries = await DecisionFile("apt", pc1_executor).load()
            assert apt_item_id in apt_entries, (
                f"{candidate} not recorded in pc1's apt decision file after a skip-always decision (D-08a)"
            )
            manual_entries = await DecisionFile("manual", pc1_executor).load()
            assert deb_item_id in manual_entries, (
                f"{hand_deb} was never presented as an item needing an install snippet: no decision was recorded "
                f"for {deb_item_id} on pc1 although the review was answered SKIP_ALWAYS for it.\n"
                f"recorded: {sorted(manual_entries)}"
            )
            still_absent = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(still_absent.stdout), "skip-always must not itself install the item"

            # -- run 2: same direction, forced ------------------------------------------
            force_apply = {apt_item_id: Decision.APPLY, deb_item_id: Decision.APPLY}
            second = await pc1_executor.run_command(
                f"{_automation_env_assignment_multi(force_apply)} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order",
                timeout=300.0,
                login_shell=True,
            )
            assert second.success, (
                f"second sync unexpectedly failed.\nstdout: {second.stdout}\nstderr: {second.stderr}"
            )
            still_absent_2 = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(still_absent_2.stdout), (
                f"{candidate} was installed on pc2 despite a source-held skip-always decision -- "
                "the item produced a diff when it should have been filtered out entirely (D-08)"
            )
            # A decision file holds nothing but skip-always entries, so the entry being
            # BYTE-IDENTICAL to run 1's -- `recorded_at` included -- is what says nobody
            # answered this item again.
            assert (await DecisionFile("manual", pc1_executor).load())[deb_item_id] == manual_entries[deb_item_id], (
                f"{hand_deb}'s recorded decision was rewritten by the second run -- the item was presented again, "
                "when D-08 makes it inert"
            )

            # -- run 3: reversed roles ---------------------------------------------------
            await _write_apt_sync_config(pc2_executor)
            reversed_result = await pc2_executor.run_command(
                f"{_automation_env_assignment_multi({apt_item_id: Decision.APPLY})} "
                "pc-switcher sync pc1 --yes --allow-first-sync --allow-out-of-order",
                timeout=300.0,
                login_shell=True,
            )
            assert reversed_result.success, (
                f"reversed sync unexpectedly failed.\n"
                f"stdout: {reversed_result.stdout}\nstderr: {reversed_result.stderr}"
            )
            pc1_manual_after = await pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate in nonblank_lines(pc1_manual_after.stdout), (
                f"{candidate} was removed from pc1 despite a target-held skip-always decision -- "
                "the item produced a diff when it should have been filtered out entirely (D-08)"
            )
        finally:
            if hand_deb:
                await pc1_executor.run_command(
                    f"sudo DEBIAN_FRONTEND=noninteractive dpkg --purge {shlex.quote(hand_deb)}",
                    login_shell=False,
                    timeout=120.0,
                )


class TestCrossDirectionRoundTrips:
    """Narratives that only exist across MORE than one run and more than one direction -- the
    shape a real two-machine workflow actually has, and the one thing a single-run test can
    never observe.

    Three runs carry the round trip: an install propagates one way, the user undoes it on the
    machine that is about to become the source, and the removal comes back the other way --
    offered first without effect and only then approved. Each of the two removal-direction
    runs also carries the removal-shaped claims that need a real `apt-get remove` on a real
    machine and nothing else of their own (#216): what a repository's deletion takes with it,
    what snapd keeps when a sync removes a snap, and what apt's own dependency resolution
    does to a second package the user did not approve.
    """

    async def test_an_install_propagates_and_the_removals_come_back_the_other_way(  # noqa: PLR0915
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: _AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """A54, H31, H114, N7, N9, C24, C63, C104, E36, E37, D37, D72.

        Run 1 (pc1 -> pc2) converges three removal-shaped things at once, plus the install
        the reversed direction later has something to undo:

        - a vendor repository file that exists only on pc2 is removed, and its signing key
          goes with it although the user decided only about the repository: once the
          repository file is gone nothing on pc2 references the key any more, and that count
          is taken after the deletion actually happened, which is why a real run is its only
          witness. apt's own account of which repositories it tried is the evidence, not its
          exit code -- `apt-get update` exits 0 when an index fails to fetch. While the pair
          exists apt prints an `Err:` line naming the unresolvable synthetic host, asserted
          BEFORE the run so its total absence afterwards is a real witness in both
          directions; the post-removal exit code is asserted too, for the failure the output
          check cannot see (an `/etc/apt` left syntactically unreadable);
        - a snap pc1 no longer has is removed from pc2 by an approved item, and `snap saved`
          on pc2 then lists a snapshot for it (`PKG-FR-SNAP-REMOVE-SNAPSHOT`). The subject is
          made target-only by removing it from pc1 with `--purge`, so pc1 keeps no snapshot
          of its own and the one found on pc2 can only be this run's, and it is given system
          data first: a snapshot of a snap that never held any is not the case the article is
          about;
        - two manually-installed packages pc2 has and pc1 does not, where removing the
          approved one takes the SKIPPED one with it. Answering "go ahead" at that question
          removes both, past the apply-time guard, while the skipped candidate's OWN removal
          item stays skipped -- what was approved is the consequence, not the item.

        Run 2 (pc2 -> pc1) is the point of the round trip. A removal-direction item lands in
        its own unticked group (D-07/I3), so leaving it undecided must leave pc1's package
        installed -- proven by deciding it SKIP_ONCE explicitly and reading pc1's own
        `apt-mark showmanual`.

        Run 3 (pc2 -> pc1) approves the same item, and pc1 loses the package. It also carries
        the other answer to the collateral question, over a fresh pair published on pc1:
        keeping the skipped candidate leaves the approved removal unapplied rather than
        failing it, so both packages survive and the run still succeeds.

        Each cascade claim asserts the question was put in the REVIEW and not at the
        apply-time guard, by the words each writes: the review's own decision pass logs
        `reviewed <pkg> (report_only)`, while `LateCollateral` logs `reviewed <pkg>
        (collateral)`. Both answers would otherwise leave the same machine state whichever
        round asked. "Stop the sync" is the one answer not driven here -- it is not a
        `Decision` value at all (`packages.review` raises `SyncAbortedByUser` from the screen
        itself), so the automation hook cannot express it.

        Candidate safety is vetted against pc2's reverse dependencies
        (`pick_safe_removal_candidates`) before either machine is touched; the two VMs are
        provisioned from one baseline, so the reverse-dependency picture is the same on both
        -- if it ever diverges, apt_sync's own collateral guard refuses the item and this
        fails loudly rather than damaging pc1.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = apt_subjects.install_direction[0]
        item_id = AptPackageItem(name=candidate, version="").item_id
        snap_name = (await _snap_subjects(pc1_executor, pc2_executor, count=2))[1]
        snap_source_revision = await _snap_revision(pc1_executor, snap_name)
        snap_target_revision = await _snap_revision(pc2_executor, snap_name)
        assert snap_source_revision and snap_target_revision, f"{snap_name} is not installed on both machines"
        snapshot_sets_before = {set_id for set_id, _snap in await _snap_saved_rows(pc2_executor)}

        uniq = uuid4().hex[:12]
        snap_data_file = f"/var/snap/{snap_name}/common/pcswitcher-it-{uniq}"

        source_filename = key_filename = ""
        target_pair: tuple[str, str, str, str] | None = None
        source_pair: tuple[str, str, str, str] | None = None
        try:
            await _ensure_installed_and_manual(pc1_executor, candidate)
            await _ensure_absent(pc2_executor, candidate)

            source_filename, key_filename = await _create_synthetic_repo_and_key(pc2_executor)
            source_dest = f"{_APT_SOURCES_DIR}/{source_filename}"
            key_dest = f"{_APT_KEYRINGS_DIR}/{key_filename}"
            broken_update = await _apt_get_update(pc2_executor)
            reached_for_repo = apt_update_lines_naming(broken_update, _SYNTHETIC_REPO_HOST)
            assert any(line.startswith("Err:") for line in reached_for_repo), (
                f"pc2's `apt-get update` reported no `Err:` line naming {_SYNTHETIC_REPO_HOST} while the "
                "unreachable synthetic repo was configured, so apt is not actually reaching for that repo and its "
                "absence from the post-removal run below would prove nothing.\n"
                f"lines naming the host: {reached_for_repo}\n"
                f"stdout: {broken_update.stdout}\nstderr: {broken_update.stderr}"
            )

            seeded = await pc2_executor.run_command(
                f"sudo mkdir --parents {shlex.quote(f'/var/snap/{snap_name}/common')} && "
                f"printf %s pcswitcher-it-{uniq} | sudo tee {shlex.quote(snap_data_file)} > /dev/null",
                login_shell=False,
                timeout=30.0,
            )
            assert seeded.success, f"could not give {snap_name} data on pc2 to snapshot: {seeded.stderr}"
            purged = await pc1_executor.run_command(
                f"sudo snap remove --purge {shlex.quote(snap_name)}", login_shell=False, timeout=180.0
            )
            assert purged.success, f"Failed to remove {snap_name} from pc1: {purged.stderr}"

            target_pair = await _publish_a_cascading_pair(pc2_executor)
            target_base, target_dependent, _target_repo, _target_list = target_pair

            await _write_package_sync_config(pc1_executor, apt_sync=True, snap_sync=True)

            # -- run 1: pc1 -> pc2, the install direction and every removal ---------------
            forward_decisions = {
                item_id: Decision.APPLY,
                f"apt:source:{source_filename}": Decision.APPLY,
                f"snap:{snap_name}": Decision.APPLY,
                AptPackageItem(name=target_base, version="").item_id: Decision.APPLY,
                AptPackageItem(name=target_dependent, version="").item_id: Decision.SKIP_ONCE,
                _collateral_removal_item_id(target_dependent): Decision.APPLY,
            }
            forward = await pc1_executor.run_command(
                f"{_automation_env_assignment_multi(forward_decisions)} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=600.0,
                login_shell=True,
            )
            assert forward.success, (
                f"forward sync exited {forward.exit_code}.\nstdout: {forward.stdout}\nstderr: {forward.stderr}"
            )

            after_forward = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate in nonblank_lines(after_forward.stdout), (
                f"{candidate} did not propagate to pc2; the reversed direction below would have nothing to remove"
            )

            gone = await pc2_executor.run_command(
                f"test ! -e {shlex.quote(source_dest)} && test ! -e {shlex.quote(key_dest)}",
                login_shell=False,
                timeout=10.0,
            )
            assert gone.success, (
                f"{source_filename} and/or {key_filename} still present under /etc/apt on pc2 after the repository "
                f"removal was approved -- the key it left unreferenced was not collected.\n"
                f"stdout: {forward.stdout}\nstderr: {forward.stderr}"
            )
            working_update = await _apt_get_update(pc2_executor)
            still_reaching = apt_update_lines_naming(working_update, _SYNTHETIC_REPO_HOST)
            assert not still_reaching, (
                f"pc2's `apt-get update` still names {_SYNTHETIC_REPO_HOST} after the repo file and its key were "
                "removed -- apt is still configured with the repository, so the pair did not actually leave "
                f"/etc/apt.\nlines naming the host: {still_reaching}\n"
                f"stdout: {working_update.stdout}\nstderr: {working_update.stderr}"
            )
            assert working_update.success, (
                "pc2's `apt-get update` exits non-zero after the repo file and its key were removed -- /etc/apt was "
                f"left unreadable.\nstdout: {working_update.stdout}\nstderr: {working_update.stderr}"
            )

            snap_still_there = await pc2_executor.run_command(
                f"snap list {shlex.quote(snap_name)}", login_shell=False, timeout=15.0
            )
            assert not snap_still_there.success, (
                f"{snap_name} is still installed on pc2, so no removal happened and the snapshot check below would "
                f"say nothing.\n{snap_still_there.stdout}"
            )
            saved = await _snap_saved_rows(pc2_executor)
            assert any(snap == snap_name for _set_id, snap in saved), (
                f"snapd kept no snapshot for {snap_name} after the sync removed it from pc2 — the removal took the "
                f"machine's data with it.\nsnap saved: {saved}"
            )

            target_installed = parse_dpkg_installed(
                (
                    await pc2_executor.run_command(
                        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
                    )
                ).stdout
            )
            assert target_base not in target_installed, (
                f"{target_base}'s approved removal did not run after the collateral go-ahead"
            )
            assert target_dependent not in target_installed, (
                f"{target_dependent} survived a removal the user let go ahead -- the apply-time guard refused a "
                "consequence that was approved"
            )
            forward_collapsed = _collapse_run_output(forward.stdout + forward.stderr)
            assert f"reviewed {target_dependent} (report_only): applied" in forward_collapsed, (
                f"the go-ahead for {target_dependent} was never recorded against a collateral item in the review.\n"
                f"stdout: {forward.stdout}\nstderr: {forward.stderr}"
            )
            assert f"reviewed {target_dependent} (collateral)" not in forward_collapsed, (
                f"{target_dependent} was asked about at the apply-time guard instead of in the review's second round"
            )
            assert (
                f"reviewed {target_dependent} ({_SYNTHETIC_PACKAGE_VERSION}) (remove): skipped this run"
                in forward_collapsed
            ), (
                f"{target_dependent}'s own removal item did not stay skipped -- the go-ahead answered the "
                f"consequence, not the item.\nstdout: {forward.stdout}\nstderr: {forward.stderr}"
            )

            # The user removes the package again on pc2, which is about to become the SOURCE.
            second_removal = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert second_removal.success, f"Failed to remove {candidate} from pc2 again: {second_removal.stderr}"

            await _write_apt_sync_config(pc2_executor)

            # -- run 2: pc2 -> pc1, removal direction, explicitly left undecided ----------
            undecided = await pc2_executor.run_command(
                f"{_automation_env_assignment_multi({item_id: Decision.SKIP_ONCE})} "
                "pc-switcher sync pc1 --yes --allow-first-sync",
                timeout=600.0,
                login_shell=True,
            )
            assert undecided.success, (
                f"reversed sync (undecided) exited {undecided.exit_code}.\n"
                f"stdout: {undecided.stdout}\nstderr: {undecided.stderr}"
            )
            pc1_after_undecided = await pc1_executor.run_command(
                "apt-mark showmanual", login_shell=False, timeout=15.0
            )
            assert candidate in nonblank_lines(pc1_after_undecided.stdout), (
                f"{candidate} was removed from pc1 without being approved -- a removal-direction item must take "
                "effect only when the user ticks it"
            )

            # -- run 3: pc2 -> pc1, same item approved, and the cascade kept --------------
            source_pair = await _publish_a_cascading_pair(pc1_executor)
            source_base, source_dependent, _source_repo, _source_list = source_pair
            approved_decisions = {
                item_id: Decision.APPLY,
                AptPackageItem(name=source_base, version="").item_id: Decision.APPLY,
                AptPackageItem(name=source_dependent, version="").item_id: Decision.SKIP_ONCE,
                _collateral_removal_item_id(source_dependent): Decision.SKIP_ONCE,
            }
            approved = await pc2_executor.run_command(
                f"{_automation_env_assignment_multi(approved_decisions)} "
                "pc-switcher sync pc1 --yes --allow-first-sync --allow-out-of-order",
                timeout=600.0,
                login_shell=True,
            )
            assert approved.success, (
                "keeping a collateral package must leave the change that causes it unapplied, not fail it.\n"
                f"exit {approved.exit_code}\nstdout: {approved.stdout}\nstderr: {approved.stderr}"
            )

            pc1_after_approved = await pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(pc1_after_approved.stdout), (
                f"{candidate} still manually installed on pc1 after the removal was approved -- the removal did not "
                "propagate back across the reversed direction"
            )

            source_installed = parse_dpkg_installed(
                (
                    await pc1_executor.run_command(
                        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
                    )
                ).stdout
            )
            assert source_dependent in source_installed, (
                f"{source_dependent} was removed from pc1 although the user kept it at the collateral question"
            )
            assert source_base in source_installed, (
                f"{source_base}'s approved removal still ran after {source_dependent} was kept -- keeping a "
                "collateral package must cancel the change that causes it"
            )
            approved_collapsed = _collapse_run_output(approved.stdout + approved.stderr)
            assert f"reviewed {source_dependent} (report_only): skipped this run" in approved_collapsed, (
                f"{source_dependent} was never put to the user as a collateral question in the review.\n"
                f"stdout: {approved.stdout}\nstderr: {approved.stderr}"
            )
            assert f"reviewed {source_dependent} (collateral)" not in approved_collapsed, (
                f"{source_dependent} was asked about at the apply-time guard instead of in the review's second round"
            )
        finally:
            if target_pair:
                _target_base, _target_dependent, target_repo, target_list = target_pair
                await _undeclare_local_repository(pc2_executor, target_repo, target_list)
            if source_pair:
                _source_base, _source_dependent, source_repo, source_list = source_pair
                await _undeclare_local_repository(pc1_executor, source_repo, source_list)
            for set_id, snap in await _snap_saved_rows(pc2_executor):
                if snap == snap_name and set_id not in snapshot_sets_before:
                    await pc2_executor.run_command(
                        f"sudo snap forget {shlex.quote(set_id)}", login_shell=False, timeout=60.0
                    )
            await pc2_executor.run_command(
                f"sudo rm --force {shlex.quote(snap_data_file)}", login_shell=False, timeout=15.0
            )
            cleanup_paths = " ".join(
                shlex.quote(f"{directory}/{filename}")
                for directory, filename in (
                    (_APT_SOURCES_DIR, source_filename),
                    (_APT_KEYRINGS_DIR, key_filename),
                )
                if filename
            )
            if cleanup_paths:
                for executor in (pc1_executor, pc2_executor):
                    await executor.run_command(f"sudo rm --force {cleanup_paths}", login_shell=False, timeout=15.0)


class TestAFailureCostsItsOwnItemAndNothingElse:
    """D-27 and `PKG-FR-JOB-INDEPENDENCE` in the one run that can show both: a job that fails
    on its own middle item while every job ordered after it still reviews and converges its
    own diff.

    The two claims are the same device at two scales -- one item's failure inside a job, and
    that job's failure inside a run -- so one failing snippet settles both (#216). It has to
    come FIRST for either to mean anything: a job that fails last leaves the others' work
    intact whatever the orchestrator does, and an item that fails last says nothing about the
    item after it. Jobs run in the order the config names them (`_discover_and_validate_jobs`
    iterates `sync_jobs` as written), so `manual_installs_sync` is written first; within it,
    `_scan_unowned_installs` sorts its findings alphabetically by path, which is what places
    the failing snippet strictly BETWEEN the two that install something
    (`_CONTINUE_TEST_MARKERS`, a < b < c).

    The failing item must genuinely reach the converge path. A package name that resolves to
    nothing is classified REPO_UNAVAILABLE/REPORT_ONLY (plan 02-05) and short-circuits before
    ever touching the target, so it would prove nothing about D-27 -- hence a snippet that
    deliberately exits non-zero.
    """

    async def test_the_item_after_a_failure_and_the_jobs_after_its_job_all_still_land(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: _AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """J20, J26, J34, H12, K20, N22.

        All four package jobs are enabled and `manual_installs_sync` runs first, holding
        three approved snippets: two that genuinely `apt-get install` a real package and,
        between them, one that exits 42. The sync's own exit code is non-zero -- the
        orchestrator derives it from job results, not from whether an exception propagated
        (`_summarize_job_outcomes`) -- and the failure's stderr lands in the run's own
        summary.

        The witnesses are pc2's own package managers, as everywhere else in this module. The
        snippet ordered AFTER the failing one installed its package, which is D-27's
        "continue, collect, report" promise. The apt package is back in `apt-mark showmanual`
        and the snap is back in `snap list`, each of which could only happen if that manager
        reviewed its own diff and then applied it, after the run had already failed a job --
        which is also how each manager settling its OWN review before its OWN mutation is
        carried here: no inter-manager ordering is asserted and no run-log line is scraped
        for it.

        `flatpak_sync` is enabled and left unanswered: this run's claim is about four jobs
        being enabled together, and a job whose items are all declined still plans, reviews
        and reports -- it just converges nothing, which is why nothing is asserted about it.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        snippet_first, snippet_second, apt_candidate = apt_subjects.install_direction
        snap_candidate = await _snap_subject(pc1_executor, pc2_executor)
        original_snap_revision = await _snap_revision(pc2_executor, snap_candidate)
        assert original_snap_revision, f"{snap_candidate} is not installed on pc2"

        try:
            for subject in apt_subjects.install_direction:
                await _ensure_installed_and_manual(pc1_executor, subject)
                await _ensure_absent(pc2_executor, subject)
            removed_snap = await pc2_executor.run_command(
                f"sudo snap remove {shlex.quote(snap_candidate)}", login_shell=False, timeout=60.0
            )
            assert removed_snap.success, f"Failed to remove {snap_candidate} from pc2: {removed_snap.stderr}"

            for path in _CONTINUE_TEST_MARKERS:
                await _create_unowned_marker(pc1_executor, path)

            item_id_first = _unowned_item_id(_CONTINUE_TEST_MARKER_INSTALL_FIRST)
            item_id_fail = _unowned_item_id(_CONTINUE_TEST_MARKER_FAIL)
            item_id_second = _unowned_item_id(_CONTINUE_TEST_MARKER_INSTALL_SECOND)
            await _author_snippet(
                pc1_executor,
                item_id_first,
                _CONTINUE_TEST_MARKER_INSTALL_FIRST,
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(snippet_first)}",
            )
            await _author_snippet(
                pc1_executor,
                item_id_fail,
                _CONTINUE_TEST_MARKER_FAIL,
                f'echo "{_DELIBERATE_FAILURE_MESSAGE}" >&2; exit 42',
            )
            await _author_snippet(
                pc1_executor,
                item_id_second,
                _CONTINUE_TEST_MARKER_INSTALL_SECOND,
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(snippet_second)}",
            )

            # Written in execution order: the failing job first, the three whose work must
            # survive it after.
            await _write_package_sync_config(
                pc1_executor,
                manual_installs_sync=True,
                apt_sync=True,
                snap_sync=True,
                flatpak_sync=True,
            )

            decisions = {
                item_id_first: Decision.APPLY,
                item_id_fail: Decision.APPLY,
                item_id_second: Decision.APPLY,
                AptPackageItem(name=apt_candidate, version="").item_id: Decision.APPLY,
                f"snap:{snap_candidate}": Decision.APPLY,
            }
            sync_result = await pc1_executor.run_command(
                f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=600.0,
                login_shell=True,
            )
            assert not sync_result.success, (
                "a run with a failed item and a failed job must exit non-zero (D-27, PKG-FR-OUTCOME-FAILED).\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after_lines = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            assert snippet_first in after_lines, f"{snippet_first} (before the failing item) not installed on pc2"
            assert snippet_second in after_lines, (
                f"{snippet_second} (after the failing item) not installed on pc2 -- "
                "D-27's 'continue, collect, report' promise did not hold"
            )
            assert apt_candidate in after_lines, (
                f"{apt_candidate} not reinstalled on pc2 -- apt_sync's approved work did not survive the earlier "
                "job's failure (PKG-FR-JOB-INDEPENDENCE)"
            )
            after_snap = await pc2_executor.run_command(
                f"snap list {shlex.quote(snap_candidate)}", login_shell=False, timeout=15.0
            )
            assert after_snap.success, (
                f"{snap_candidate} not reinstalled on pc2 -- snap_sync's approved work did not survive the earlier "
                f"job's failure (PKG-FR-JOB-INDEPENDENCE): {after_snap.stderr}"
            )

            # Secondary confirmation only -- the exit code and pc2's own managers above are
            # the primary evidence. This says the non-zero exit is THIS failure's and not
            # some unrelated trouble, which the exit code alone cannot distinguish.
            assert _DELIBERATE_FAILURE_MESSAGE in sync_result.stdout + sync_result.stderr
        finally:
            for executor in (pc1_executor, pc2_executor):
                for path in _CONTINUE_TEST_MARKERS:
                    await _remove_unowned_marker(executor, path)


class TestSnapPerItemFailureOnVMs:
    """`PKG-FR-SNAP-FAIL-ITEM` on real machines: one snap item failing costs that item and
    nothing else.

    The failure is real snapd's, not a mock's: pc2 is put offline as far as the store is
    concerned (`snap set system store.access=offline`), which is precisely the split the
    claim needs — an install has to reach the store and a removal does not. That also puts
    it out of reach of every converging run in this module, which is why it keeps a sync of
    its own.
    """

    async def test_one_snap_item_fails_and_the_item_after_it_still_lands(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E53 — Two approved snap items, the first of which pc2's snapd cannot carry out: it
        fails alone, the second still lands, and the sync's own exit code reports it.

        Ordering is what makes "the rest still landed" a real claim rather than an accident:
        `_diff_snap_items` walks the SOURCE's snaps before the target-only ones, so the
        install is converged before the removal. The install is the item that fails.

        Both subjects are fixture snaps, made divergent by removing one from each machine
        with `--purge` (no snapshot to clean up afterwards). Neither is put back: every
        scenario that wants a fixture snap converges to it itself (`_snap_subjects`), so what
        this one leaves removed costs the next one an install only if it needs one.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        removal_subject, install_subject = await _snap_subjects(pc1_executor, pc2_executor, count=2)
        removal_revision = await _snap_revision(pc2_executor, removal_subject)
        install_revision = await _snap_revision(pc1_executor, install_subject)
        source_removal_revision = await _snap_revision(pc1_executor, removal_subject)
        target_install_revision = await _snap_revision(pc2_executor, install_subject)
        assert removal_revision and install_revision and source_removal_revision and target_install_revision, (
            f"{removal_subject} and {install_subject} must both be installed on both machines"
        )

        store_offline = False
        try:
            purged = await pc2_executor.run_command(
                f"sudo snap remove --purge {shlex.quote(install_subject)}", login_shell=False, timeout=180.0
            )
            assert purged.success, f"Failed to remove {install_subject} from pc2: {purged.stderr}"
            purged_source = await pc1_executor.run_command(
                f"sudo snap remove --purge {shlex.quote(removal_subject)}", login_shell=False, timeout=180.0
            )
            assert purged_source.success, f"Failed to remove {removal_subject} from pc1: {purged_source.stderr}"

            offline = await pc2_executor.run_command(_SNAP_STORE_OFFLINE_CMD, login_shell=False, timeout=60.0)
            assert offline.success, (
                f"`{_SNAP_STORE_OFFLINE_CMD}` failed, so pc2's snapd cannot be made to refuse an install and this "
                f"run has no per-item failure to observe: {offline.stderr}"
            )
            store_offline = True
            # The precondition, asserted rather than assumed: a store pc2 can still reach
            # would install the snap and leave nothing to fail.
            reachable = await pc2_executor.run_command(
                f"snap info {shlex.quote(install_subject)}", login_shell=False, timeout=60.0
            )
            assert not reachable.success, (
                f"pc2 still reaches the store for {install_subject}, so the install below would succeed.\n"
                f"stdout: {reachable.stdout}\nstderr: {reachable.stderr}"
            )

            await _write_package_sync_config(pc1_executor, snap_sync=True)

            decisions = {f"snap:{install_subject}": Decision.APPLY, f"snap:{removal_subject}": Decision.APPLY}
            sync_cmd = f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)

            assert not sync_result.success, (
                "a run with a failed snap item must exit non-zero.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            failed_item = await pc2_executor.run_command(
                f"snap list {shlex.quote(install_subject)}", login_shell=False, timeout=15.0
            )
            assert not failed_item.success, (
                f"{install_subject} was installed on pc2 although its store is unreachable, so nothing failed and "
                f"this run proves nothing.\n{failed_item.stdout}"
            )
            landed_item = await pc2_executor.run_command(
                f"snap list {shlex.quote(removal_subject)}", login_shell=False, timeout=15.0
            )
            assert not landed_item.success, (
                f"{removal_subject} is still on pc2: the item ordered after the failing one was never converged.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            # Secondary confirmation only — the exit code and pc2's own snapd above are the
            # primary evidence. This says exactly one item failed rather than the whole job
            # having collapsed, which the exit code cannot distinguish.
            collapsed = _collapse_run_output(sync_result.stdout + sync_result.stderr)
            assert "1 snap item(s) failed" in collapsed, (
                f"the run did not report exactly one failed snap item.\n{sync_result.stdout}\n{sync_result.stderr}"
            )
        finally:
            if store_offline:
                restored = await pc2_executor.run_command(_SNAP_STORE_ONLINE_CMD, login_shell=False, timeout=60.0)
                if not restored.success:
                    print(f"[cleanup] failed to put pc2's snapd back online: {restored.stderr}")


class TestTheESMAttachmentGateOnVMs:
    """ADR-020 D-38 at VM level: a source carrying the two `ubuntu-esm-*` sources and a
    target with no Ubuntu Pro attachment.

    Only the SKIP arm is testable here, and that is a statement about the fixtures, not a
    gap in the gate: `pro attach` needs the user's own subscription token from their Pro
    dashboard or an interactive browser short-code flow, a machine's credentials are not
    transferable, and putting a subscription token in CI would violate the project's
    secrets rule. Both VMs are therefore permanently unattached — which is exactly the
    machine this test needs, and is why nothing here is skipped or discovered: the test
    puts both machines in the state the gate needs and restores them in a `finally`.

    It keeps a sync of its own because the gate costs the WHOLE apt job: no converging run
    can carry it, since a skipped apt_sync converges nothing at all.

    What only a VM can prove: that the skip costs the WHOLE job. `/etc/apt/preferences.d`
    always-syncs with no derivation predicate, so an implementation that withheld only the
    two sources would still put the source's pin on the target — visible here as a file on
    pc2, and invisible to any mocked-executor unit test. That pin is the uuid-suffixed
    synthetic one, not `ubuntu-pro-esm-apps`: `ubuntu-pro-client` SHIPS
    `/etc/apt/preferences.d/ubuntu-pro-esm-apps` and `-esm-infra` (`dpkg -L`, measured on
    both VMs) whether or not the machine is attached, so the real ESM pins are byte-identical
    on source and target and can never be a pending write to witness anything with.
    """

    async def test_an_unattached_target_skips_apt_sync_and_leaves_etc_apt_untouched(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H54, J10, N18."""
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        esm_dests = [f"{_APT_SOURCES_DIR}/{name}" for name in _ESM_SOURCE_BODIES]
        source_aside = ""
        target_aside = ""
        pin_dest = ""
        try:
            # Both machines are PUT in the state the gate needs rather than asked to be in
            # it already: pc2 carrying neither file — a target copy with the source's digest
            # is not a pending write, so the gate would never fire — and pc1 carrying both
            # with the bodies below, whatever either machine came with.
            target_aside = await _take_paths_aside(pc2_executor, esm_dests)
            source_aside = await _take_paths_aside(pc1_executor, esm_dests)
            writes = [
                f"printf %s {shlex.quote(body)} | sudo tee {shlex.quote(f'{_APT_SOURCES_DIR}/{name}')} > /dev/null"
                for name, body in _ESM_SOURCE_BODIES.items()
            ]
            created = await pc1_executor.run_command(" && ".join(writes), login_shell=False, timeout=20.0)
            assert created.success, f"Failed to create the ESM sources on pc1: {created.stderr}"

            pin_dest = f"{_APT_PREFERENCES_DIR}/{await _create_synthetic_pin(pc1_executor)}"

            # snap_sync runs after apt_sync and is the evidence that a skip is not an abort.
            await _write_package_sync_config(pc1_executor, apt_sync=True, snap_sync=True)

            # No automation env and no pty: `ask_gate` finds no TTY, which is the
            # non-interactive path the user ruled must skip the whole job.
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync", timeout=300.0, login_shell=True
            )
            assert sync_result.success, (
                f"a skipped job must not fail the run.\nstdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            combined_output = sync_result.stdout + sync_result.stderr
            assert "apt_sync skipped" in combined_output, f"apt_sync was not reported as skipped.\n{combined_output}"
            for name in _ESM_SOURCE_BODIES:
                assert name in combined_output, f"the skip reason does not name {name}.\n{combined_output}"
            assert "snap_sync" in combined_output, (
                f"the job after apt_sync did not run — a skip must not abort the sync.\n{combined_output}"
            )

            # The load-bearing assertion: pc2's /etc/apt is exactly as it was, the PIN
            # included. A gate that withheld only the two sources would leave the pin here.
            untouched = await pc2_executor.run_command(
                " && ".join(f"test ! -e {shlex.quote(path)}" for path in (*esm_dests, pin_dest)),
                login_shell=False,
                timeout=10.0,
            )
            assert untouched.success, (
                "a skipped apt_sync still wrote to pc2's /etc/apt — the whole job must leave it as it was"
            )
        finally:
            if pin_dest:
                # uuid-suffixed, so this can never name a file either machine came with.
                cleanup = shlex.quote(pin_dest)
                await pc1_executor.run_command(f"sudo rm --force {cleanup}", login_shell=False, timeout=15.0)
                await pc2_executor.run_command(f"sudo rm --force {cleanup}", login_shell=False, timeout=15.0)
            if source_aside:
                await _put_paths_back(pc1_executor, source_aside, esm_dests)
            if target_aside:
                await _put_paths_back(pc2_executor, target_aside, esm_dests)


class TestAStrayAptHoldEndsTheRun:
    """`PKG-FR-HOLD-WITHOUT-PACKAGE` against real `apt-mark` state: a hold naming a package
    its machine does not have ends the run before anything is written.

    It keeps a sync of its own for the reason the ESM gate does -- the run it needs is one
    that ABORTS, and no converging run can be that.
    """

    async def test_a_hold_naming_a_package_the_machine_does_not_have_ends_the_run(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        apt_subjects: _AptSubjects,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """B16 — pc2 records a hold for a package it does not have; the run ends naming the
        package and pc2, and pc2's package state is byte-identical afterwards.

        The run is given real work first -- a package removed from pc2 that it would
        otherwise install -- so "nothing was written" is a claim about a run that had
        something to write. `_MachinePackageState` is the comparison, because the article's
        "before anything is written" reaches further than the one package.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        ghost = await _a_name_apt_knows_the_machine_does_not_have(pc2_executor)
        install_candidate = apt_subjects.install_direction[0]
        try:
            await _ensure_installed_and_manual(pc1_executor, install_candidate)
            await _ensure_absent(pc2_executor, install_candidate)

            held = await pc2_executor.run_command(
                f"sudo apt-mark hold {shlex.quote(ghost)}", login_shell=False, timeout=30.0
            )
            assert held.success, f"Failed to hold {ghost} on pc2: {held.stderr}"
            recorded = await pc2_executor.run_command("apt-mark showhold", login_shell=False, timeout=15.0)
            assert ghost in nonblank_lines(recorded.stdout), (
                f"pc2 did not record a hold for {ghost}, so the bookkeeping failure this test is about does not "
                f"exist on it.\n{recorded.stdout}"
            )

            await _write_apt_sync_config(pc1_executor)
            before = await _capture_machine_package_state(pc2_executor)

            item_id = AptPackageItem(name=install_candidate, version="").item_id
            sync_cmd = f"{_automation_env_assignment(item_id)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)
            assert not sync_result.success, (
                "a hold naming a package the machine does not have must end the run.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            collapsed = _collapse_run_output(sync_result.stdout + sync_result.stderr)
            assert "apt holds naming packages the machine does not have installed:" in collapsed, (
                f"the run did not end over the stray hold on {ghost}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
            assert f"pc2: {ghost} — clear with `sudo apt-mark unhold {ghost}`" in collapsed, (
                f"the run did not name {ghost}, the machine holding it and the command that clears it.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after = await _capture_machine_package_state(pc2_executor)
            assert after == before, (
                "the run wrote to pc2 before ending over a hold it could not act on.\n"
                f"before: {before}\nafter: {after}"
            )
        finally:
            await pc2_executor.run_command(
                f"sudo apt-mark unhold {shlex.quote(ghost)}", login_shell=False, timeout=30.0
            )


class TestTheSyncWindowHoldIsTimed:
    """`PKG-FR-SNAP-REFRESH-PAUSE`'s self-healing half: the suspension a run writes is a
    timed value on each machine's own clock, so a run that dies without cleaning up leaves
    a hold that lapses rather than one that never does.

    Only a real run can show it, and only a run that never finishes: the value is written by
    the orchestrator and put back by its own cleanup, so the only moment it exists is inside
    the sync window. No completed run can carry this claim, which is why it keeps a sync of
    its own.
    """

    async def test_a_killed_run_leaves_a_timed_hold_on_each_machines_own_clock(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E88, E89 — A sync is killed inside its own window, and what snapd is left holding on
        BOTH machines is an instant in that machine's own near future — never `forever`.

        Killed with SIGKILL so no cleanup path can run: an orchestrator that restored the
        prior value would leave nothing to read, and a run that exited normally would say
        nothing about the case the article is about.

        `dummy_success` is enabled after `snap_sync` purely to widen the window: it sleeps
        for its configured default on each machine, which is what gives the poll below
        something to catch the run in the middle of. Both machines' `refresh.hold` is
        cleared first, so "a hold is set at all" is an unambiguous signal that the run wrote
        one, and both are put back exactly as found in the `finally`.

        The comparison is against each machine's OWN clock, never this runner's: an expiry
        computed anywhere else would still look like a future instant here, and would lapse
        at the wrong moment on the machine that has to honour it.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        pc1_prior_hold = await _capture_system_refresh_hold(pc1_executor)
        pc2_prior_hold = await _capture_system_refresh_hold(pc2_executor)
        run_log = f"/var/tmp/pcswitcher-it-killed-sync-{uuid4().hex[:12]}.log"

        try:
            await _restore_system_refresh_hold(pc1_executor, None)
            await _restore_system_refresh_hold(pc2_executor, None)
            assert await _capture_system_refresh_hold(pc1_executor) is None, "pc1 still holds a refresh.hold"
            assert await _capture_system_refresh_hold(pc2_executor) is None, "pc2 still holds a refresh.hold"

            await _write_package_sync_config(pc1_executor, snap_sync=True, dummy_success=True)

            started = await pc1_executor.run_command(
                f"setsid nohup pc-switcher sync pc2 --yes --allow-first-sync > {run_log} 2>&1 < /dev/null &",
                timeout=60.0,
                login_shell=True,
            )
            assert started.success, f"could not start a sync in the background: {started.stderr}"

            engaged_source: str | None = None
            engaged_target: str | None = None
            deadline = asyncio.get_running_loop().time() + _HOLD_POLL_TIMEOUT_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                engaged_source = await _capture_system_refresh_hold(pc1_executor)
                engaged_target = await _capture_system_refresh_hold(pc2_executor)
                if engaged_source and engaged_target:
                    break
                await asyncio.sleep(_HOLD_POLL_INTERVAL_SECONDS)
            log = await pc1_executor.run_command(f"cat {run_log}", login_shell=False, timeout=30.0)
            assert engaged_source and engaged_target, (
                "the run never paused snapd auto-refresh on both machines, so there is no window to die inside "
                f"(pc1: {engaged_source!r}, pc2: {engaged_target!r}).\n{log.stdout}"
            )

            killed = await pc1_executor.run_command(_KILL_RUNNING_SYNC_CMD, login_shell=False, timeout=30.0)
            assert killed.success, (
                f"no running sync to kill — the run had already finished and restored both machines.\n{log.stdout}"
            )

            for executor, machine in ((pc1_executor, "pc1"), (pc2_executor, "pc2")):
                left = await _capture_system_refresh_hold(executor)
                assert left is not None, (
                    f"{machine} was left with no refresh.hold at all by a run that died inside its own window"
                )
                assert left != "forever", (
                    f"{machine} was left with an INDEFINITE snapd refresh.hold by a run that died: nothing will ever "
                    "lift it, so that machine stops refreshing its snaps for good"
                )
                lapses = parse_rfc3339_utc(left)
                now = await _machine_utc_now(executor)
                assert lapses > now, (
                    f"{machine}'s refresh.hold {left!r} is not in its own future (its clock reads {now}), so the "
                    "suspension either never took effect or was computed against another machine's clock"
                )
                assert lapses - now <= _SNAP_HOLD_EXPECTED_DURATION, (
                    f"{machine}'s refresh.hold {left!r} lapses {lapses - now} from now, further ahead than the "
                    f"{_SNAP_HOLD_EXPECTED_DURATION} a sync window asks for"
                )
                assert lapses - now >= _SNAP_HOLD_EXPECTED_DURATION - _SNAP_HOLD_DURATION_SLACK, (
                    f"{machine}'s refresh.hold {left!r} lapses {lapses - now} from now, far sooner than the "
                    f"{_SNAP_HOLD_EXPECTED_DURATION} a sync window asks for"
                )
        finally:
            await pc1_executor.run_command(_KILL_RUNNING_SYNC_CMD, login_shell=False, timeout=30.0)
            await _restore_system_refresh_hold(pc1_executor, pc1_prior_hold)
            await _restore_system_refresh_hold(pc2_executor, pc2_prior_hold)
            await pc1_executor.run_command(f"rm --force {run_log}", login_shell=False, timeout=15.0)


class TestSnapHoldCaptureTiming:
    """The VM check #208 D9 promised and never got (L10), in the half that needs no sync.

    `SnapSyncJob` reads per-snap holds out of `snap list`'s Notes column DURING the sync,
    i.e. inside the window in which the orchestrator has a system-wide `refresh.hold`
    engaged on both hosts. D9 assumes those are separate snapstate -- that a system-wide
    hold neither sets nor clears an individual snap's `held` note -- and says so in a
    comment in `snap_sync._parse_snap_list`. Nothing had ever checked it against a real
    snapd.

    The end-to-end half of the same assumption, where a hold set on the source reaches the
    target through a real sync window, is one of the divergences
    `TestOneRunConvergesEveryManager` seeds.
    """

    async def test_system_refresh_hold_does_not_mask_a_per_snap_held_note(
        self,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """E71 — With a system-wide `refresh.hold` engaged, a per-snap hold still reads `held`
        in `snap list` Notes, and a snap WITHOUT a per-snap hold still reads no `held`.

        Both directions matter. If the system hold masked the note, capture inside the
        sync window would silently drop every hold the user set (holds would never
        replicate). If it ADDED the note, capture would invent a hold for every snap on
        the machine. D9's fail-safe (a system hold flips both hosts symmetrically, so a
        spurious flag cancels out in the membership diff) covers the second case only as
        long as both hosts are held -- which is why this asserts the note itself rather
        than relying on the diff to absorb it.

        Runs no sync, so it needs neither a pc-switcher install nor the state reset: the
        subject is snapd's own semantics, read straight off `snap list`.
        """
        held_name, unheld_name = await _holdable_snaps(pc2_executor, count=2)

        prior_hold = await _capture_system_refresh_hold(pc2_executor)
        try:
            hold_result = await pc2_executor.run_command(
                f"sudo snap refresh --hold=forever {shlex.quote(held_name)}", login_shell=False, timeout=60.0
            )
            assert hold_result.success, f"Failed to set a per-snap hold on {held_name}: {hold_result.stderr}"

            # Baseline, before any system-wide hold exists: the per-snap hold is visible
            # at all. Without this the assertion below could pass vacuously on a snapd
            # that never writes `held` into Notes.
            assert "held" in await _snap_notes(pc2_executor, held_name), (
                f"snapd did not report `held` in `snap list` Notes for {held_name} after "
                "`snap refresh --hold=forever` -- the per-snap hold mechanism this assumption is about is not visible"
            )

            await _engage_system_refresh_hold(pc2_executor)
            engaged = await _capture_system_refresh_hold(pc2_executor)
            assert engaged is not None, (
                "system-wide refresh.hold did not take effect; the check below would be vacuous"
            )

            notes_under_system_hold = await _snap_notes(pc2_executor, held_name)
            assert "held" in notes_under_system_hold, (
                f"#208 D9 IS FALSE: with a system-wide refresh.hold engaged ({engaged}), {held_name}'s per-snap hold "
                f"no longer reads `held` in `snap list` Notes (notes: {sorted(notes_under_system_hold)}). "
                "snap_sync captures inside exactly this window, so every per-snap hold would be silently dropped -- "
                "the capture must move BEFORE the sync-window hold is applied."
            )

            unheld_notes = await _snap_notes(pc2_executor, unheld_name)
            assert "held" not in unheld_notes, (
                f"#208 D9 IS FALSE in the other direction: a system-wide refresh.hold ({engaged}) put `held` "
                f"into {unheld_name}'s Notes even though no per-snap hold was set on it "
                f"(notes: {sorted(unheld_notes)}) -- capture inside the sync window would invent holds."
            )
        finally:
            await pc2_executor.run_command(
                f"sudo snap refresh --unhold {shlex.quote(held_name)}", login_shell=False, timeout=60.0
            )
            await _restore_system_refresh_hold(pc2_executor, prior_hold)


class TestTheStockSkeletonTheScanRefusesToName:
    """The tripwire under the hardcoded `/usr/local` skeleton `manual_installs_sync` refuses
    to present. It needs no sync at all -- the subject is one file the distribution ships.
    """

    async def test_the_stock_skeleton_is_still_what_base_files_creates(
        self, pc1_executor: BashLoginRemoteExecutor
    ) -> None:
        """G114 — the machine's own `base-files.postinst` must still create exactly the nine
        entries directly under `/usr/local` that the scan refuses to present.

        The list is hardcoded in the job for predictability — what the scan presents must not
        change with whatever a postinst says on the day — so this is the assertion that
        catches a distribution changing it: a failure here means the skeleton moved and the
        constant has to follow, not that a run is broken.

        `/usr/local` itself and `share/man` are excluded on purpose: the first is a scan root
        rather than an entry of anything, and the second is not directly under `/usr/local`,
        which is the only level the scan can meet these at.
        """
        postinst = await pc1_executor.run_command(
            "cat /var/lib/dpkg/info/base-files.postinst", login_shell=False, timeout=15.0
        )
        assert postinst.success, f"could not read base-files.postinst: {postinst.stderr}"

        created = set(re.findall(r"^\s*install_local_dir\s+(/usr/local/[^\s/]+)\s*$", postinst.stdout, re.MULTILINE))
        symlinked = set(re.findall(r'ln -s\S*\s+\S+\s+"?\$DPKG_ROOT(/usr/local/[^\s/"]+)"?', postinst.stdout))

        assert created | symlinked == {stock for stock in _STOCK_DIRECTORIES if stock.count("/") == 3}, (
            "base-files no longer creates the `/usr/local` skeleton the scan is built on; "
            f"it declares {sorted(created | symlinked)}.\n{postinst.stdout}"
        )
