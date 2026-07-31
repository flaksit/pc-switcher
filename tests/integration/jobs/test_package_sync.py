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

`TestPackageSyncWholeRunContracts` (plan 02-11) extends this same module with the
phase's whole-run contracts -- properties of an entire sync (non-interactive skip-all,
continue-on-item-failure, snap/flatpak convergence, skip-always inertness in both roles,
per-manager review-before-own-mutation) that are invisible to any single item's
mocked-executor unit test, reusing the fixture/teardown/candidate-selection
conventions established below by the tracer.

`TestPackageSyncIdempotency`, `TestSnapHoldCaptureTiming`, `TestBlockStateDecisionRoundTrip`
and `TestCrossDirectionRoundTrips` cover what only more than one run can show
(docs/dev/package-sync-scenario-coverage.md N8, E71, N4, N5, N9, N13): that a converged pair is a fixed
point, that a system-wide snapd `refresh.hold` does not mask the per-snap `held` note
snap_sync reads inside that same window (#208 D9's promised VM check), that a
skip-always recorded against a hold silences it in the next run, and that an install
propagating one way and its removal propagating back the other are one continuous
narrative. They reuse the fixture, teardown and candidate-selection conventions below.

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
selected by querying the machines (any Debian system has hundreds), but an empty selection
is likewise an assertion failure, never a skip.

The flatpak subject is the REAL Flathub, and its app is provisioned on pc1 only, so the
source->target divergence the convergence test needs is part of the baseline rather than
something a test manufactures. A locally built stand-in repository would only ever test
this project's model of a remote; #215's key replication is about a real remote's real
trust configuration (`_FIXTURE_FLATPAK_APP`).
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.jobs.apt_sync.items import AptHoldItem, AptPackageItem
from pcswitcher.jobs.flatpak_sync import FlatpakItem
from pcswitcher.jobs.manual_installs_sync import UnreproducibleItem
from pcswitcher.jobs.packages.review import PACKAGE_REVIEW_AUTOMATION_ENV, Decision
from pcswitcher.jobs.packages.state import DECISION_FILE_RELPATH_TEMPLATE, DecisionFile, Snippet, SnippetRegistry
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


# How many shared packages to probe for reverse dependencies when looking for one safe
# to remove. Each probe is a separate `apt-cache rdepends` process on the target, so the
# cost is linear and the whole probe runs under a single command timeout — bounding it
# keeps the search well inside that budget while still offering far more candidates than
# any test asks for.
_RDEPENDS_PROBE_LIMIT = 40


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


async def _find_removable_candidates(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor, count: int = 1
) -> list[str]:
    """Query both VMs and pick up to `count` packages safe to remove from pc2 for a test
    (see `pick_safe_removal_candidates`). Returns fewer than `count` -- possibly none --
    when not enough candidates qualify.
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

    # Probe only a bounded slice, not every shared package. Each loop iteration is its
    # own `apt-cache rdepends` process reloading the apt cache, so probing the whole
    # `apt-mark showmanual` intersection (~100-150 packages on these VMs) costs more
    # wall-clock than the timeout allows — which is exactly how this helper timed out
    # and took all six tests in this module with it. We only ever need `count` safe
    # candidates, so a slice comfortably larger than `count` is sufficient; the
    # docstring already allows returning fewer than requested.
    probe_set = initial_candidates[:_RDEPENDS_PROBE_LIMIT]

    quoted = " ".join(shlex.quote(name) for name in probe_set)
    rdepends_result = await pc2_executor.run_command(
        f'for p in {quoted}; do echo "{RDEPENDS_MARKER}$p"; apt-cache rdepends --installed "$p"; done',
        login_shell=False,
        timeout=120.0,
    )
    reverse_deps_by_candidate = parse_batched_rdepends(rdepends_result.stdout)

    return pick_safe_removal_candidates(pc1_manual, pc2_installed, pc2_manual, reverse_deps_by_candidate, count)


async def _removable_candidate(pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor) -> str:
    """Query both VMs and pick a package safe to remove from pc2 for this test (see
    `pick_safe_removal_candidate`).
    """
    found = await _find_removable_candidates(pc1_executor, pc2_executor, count=1)
    assert found, _no_apt_candidate_message()
    return found[0]


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


def _folder_sync_section(folder_path: str) -> str:
    """A `folder_sync` config section mirroring exactly `folder_path`, with no central
    filter file (the schema makes `filter_file` optional).
    """
    return f"folder_sync:\n  folders:\n    - path: {folder_path}\n      enabled: true\n"


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


async def _restore_package(executor: BashLoginRemoteExecutor, name: str) -> None:
    """Idempotently ensure `name` is installed and marked manual on pc2, regardless of
    test outcome -- the test must not leave pc2's package state changed.
    """
    quoted = shlex.quote(name)
    result = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {quoted} && sudo apt-mark manual {quoted}",
        login_shell=False,
        timeout=120.0,
    )
    if not result.success:
        print(f"[cleanup] failed to restore {name} on pc2: {result.stderr}")


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
# test_continue_on_item_failure: three "unowned install" snippets authored directly
# into pc1's registry (D-18/D-20/D-21) -- two that genuinely `apt-get install` a real
# package, one that deliberately exits non-zero. `ManualInstallsSyncJob._scan_unowned_installs`
# sorts its findings alphabetically by path, which is what places the failing item
# strictly BETWEEN the two installs in convergence order (a < b < c below), so
# "the item after the failure was still processed" is a real, ordered claim.
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

# The same device for `test_one_failing_job_leaves_the_other_jobs_work_intact`: one
# deliberately-failing snippet, under its own marker path so the two tests can never see
# each other's items. `manual_installs_sync` is ordered FIRST in that test's config, so
# this is what fails BEFORE the three jobs whose work the test then checks survived.
_WHOLE_RUN_FAILURE_MARKER = f"{_CONTINUE_TEST_MARKER_ROOT}/pcswitcher-it-whole-run-fail"
_WHOLE_RUN_FAILURE_MESSAGE = "deliberate whole-run integration-test failure"


# The four directories `manual_installs_sync` scans one level deep, restated rather than
# imported (the same rule this module's snap/flatpak parsers follow): the claim is about
# what a real machine's own `/usr/local` and `/opt` contain, so a test agreeing with
# whatever the shipped constant currently says would assert nothing.
_UNOWNED_SCAN_ROOTS = ("/usr/local", "/opt", "/usr/local/bin", "/usr/local/lib")

# What a run with nobody to ask writes about each item it could not ask about
# (`packages.review._warn_every_item_unasked`). It is the only place a run writes down its
# WHOLE finding set, one line per item, which is what makes it countable.
_UNASKED_ITEM_MARKER = "not asked, declined for this run (no TTY): "

# How many unreproducible findings a user can still answer one at a time. Not a measured
# figure — the criterion says "few enough to review by hand", and this is the number past
# which that stops being true. It is deliberately loose: the test's subject is an order of
# magnitude, not an exact inventory.
_HAND_REVIEWABLE_FINDING_LIMIT = 25


def _unowned_item_id(path: str) -> str:
    """The `UnreproducibleItem.item_id` a `_scan_unowned_installs`-detected path at
    `path` would produce (module docstring: identity is `unreproducible:<origin>:
    <identifier>`, independent of `label`).
    """
    return UnreproducibleItem(origin="unowned-path", identifier=path, label=path).item_id


async def _create_unowned_marker(executor: BashLoginRemoteExecutor, path: str) -> None:
    """Create an empty, dpkg-unowned directory at `path` (requires root: `/opt` is
    root-owned) so `ManualInstallsSyncJob._scan_unowned_installs` detects it as an UNREPRODUCIBLE
    item on the next `plan()`.
    """
    result = await executor.run_command(f"sudo mkdir --parents {shlex.quote(path)}", login_shell=False, timeout=15.0)
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


async def _snap_subjects(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor, count: int = 1
) -> list[str]:
    """The first `count` fixture snaps (`_FIXTURE_SNAPS`) confirmed installed on BOTH
    machines and outside `_SNAP_REMOVAL_DENYLIST` (T-02-28: never a base/snapd runtime
    everything else depends on).

    Confirmed rather than assumed: the fixture script guarantees they are there, and
    this reads both machines' `snap list --all` so a machine that somehow lacks one
    fails naming it instead of failing later inside the sync under test.
    """
    pc1_list = await pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    pc2_list = await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    installed_on_both = (
        set(parse_snap_list_names_revisions(pc1_list.stdout)) & set(parse_snap_list_names_revisions(pc2_list.stdout))
    ) - _SNAP_REMOVAL_DENYLIST
    subjects = [name for name in _FIXTURE_SNAPS if name in installed_on_both][:count]
    assert len(subjects) == count, (
        f"Need {count} of the fixture snaps {_FIXTURE_SNAPS} installed on both machines, "
        f"found {sorted(installed_on_both)}. They are created by "
        "tests/integration/scripts/internal/vm-test-fixtures.sh."
    )
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


async def _restore_snap(executor: BashLoginRemoteExecutor, name: str, revision: str) -> None:
    """Idempotently put `name` back at `revision` on `executor`'s machine, whether the test
    removed it or moved it: install when absent, refresh when present at another revision.

    Same shape as the snap restore the whole-run tests already use, in one command so the
    two cases cost one SSH round trip rather than a read plus a write.
    """
    quoted = shlex.quote(name)
    rev = shlex.quote(revision)
    result = await executor.run_command(
        f"sudo snap install --revision={rev} {quoted} || sudo snap refresh --revision={rev} {quoted}",
        login_shell=False,
        timeout=300.0,
    )
    if not result.success:
        print(f"[cleanup] failed to restore snap {name} at revision {revision}: {result.stderr}")


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


async def _apt_holds(executor: BashLoginRemoteExecutor) -> set[str]:
    """The package names `apt-mark showhold` reports on `executor`'s machine."""
    result = await executor.run_command("apt-mark showhold", login_shell=False, timeout=15.0)
    return set(nonblank_lines(result.stdout))


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
    """The first `count` fixture snaps confirmed on `executor`'s machine and outside
    `_SNAP_REMOVAL_DENYLIST` -- safe subjects for a per-snap `--hold`/`--unhold` round
    trip (which, unlike a removal, leaves the snap itself untouched).

    One-machine variant of `_snap_subjects`, for the tests that never run a sync.
    """
    result = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    installed = set(parse_snap_list_names_revisions(result.stdout)) - _SNAP_REMOVAL_DENYLIST
    subjects = [name for name in _FIXTURE_SNAPS if name in installed][:count]
    assert len(subjects) == count, (
        f"Need {count} of the fixture snaps {_FIXTURE_SNAPS} installed, found {sorted(installed)}. "
        "They are created by tests/integration/scripts/internal/vm-test-fixtures.sh."
    )
    return subjects


async def _common_apt_package(pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor) -> str:
    """The first (alphabetically) package manually installed on BOTH machines -- a safe
    subject for a hold round trip, which changes only dpkg selection state and never
    installs or removes anything, so it needs none of `pick_safe_removal_candidates`'
    reverse-dependency vetting.

    Both VMs come from one baseline, so their manual sets are identical and non-empty;
    an empty intersection means the machines are not what these tests assume.
    """
    pc1_manual = await pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    pc2_manual = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    shared = sorted(set(nonblank_lines(pc1_manual.stdout)) & set(nonblank_lines(pc2_manual.stdout)))
    assert shared, (
        "No package is manually installed on both pc1 and pc2: searched the intersection "
        "of both machines' `apt-mark showmanual` output."
    )
    return shared[0]


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

    updated = await _apt_get_update(executor)
    assert updated.success, f"apt-get update failed on the source after adding {repo_dir}: {updated.stderr}"

    installed = await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(name)}",
        login_shell=False,
        timeout=120.0,
    )
    assert installed.success, f"Failed to install {name} from {repo_dir} on the source: {installed.stderr}"
    return name, repo_dir, list_filename


async def _remove_local_repo_package(
    executor: BashLoginRemoteExecutor, name: str, repo_dir: str, list_filename: str
) -> None:
    """Undo `_install_from_a_repo_the_target_lacks`: purge the package, drop the repository
    and its declaration, and discard the index apt cached for it.

    Every step runs unconditionally (`;`, not `&&`) so a setup that failed halfway still
    has the rest of itself removed.
    """
    await executor.run_command(
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get purge --assume-yes {shlex.quote(name)}; "
        f"sudo rm --force --recursive {shlex.quote(repo_dir)} "
        f"{shlex.quote(f'{_APT_SOURCES_DIR}/{list_filename}')}; "
        f"sudo rm --force /var/lib/apt/lists/_opt_{repo_dir.rsplit('/', 1)[-1]}_*",
        login_shell=False,
        timeout=120.0,
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


class TestAptSyncEndToEnd:
    """VM-level proof of plan 02-03's tracer path: a package missing on pc2 travels
    source capture -> target query -> diff -> apt_sync's own batched review ->
    `apt-get install` on pc2 -- proven against pc2's own package manager, never against
    pc-switcher's log text.
    """

    async def test_apt_sync_installs_missing_package(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """J1, K9 — A real `pc-switcher sync pc2` reinstalls a package removed from pc2, proven by
        pc2's own `apt-mark showmanual` (never pc-switcher's log output).
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = await _removable_candidate(pc1_executor, pc2_executor)

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_result.success, f"Failed to remove {candidate} from pc2: {remove_result.stderr}"

            after_removal = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(after_removal.stdout), (
                f"{candidate} still in pc2's apt-mark showmanual after removal"
            )

            await _write_apt_sync_config(pc1_executor)

            item_id = AptPackageItem(name=candidate, version="").item_id
            sync_cmd = f"{_automation_env_assignment(item_id)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            restored = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate in nonblank_lines(restored.stdout), (
                f"{candidate} not reinstalled on pc2 after sync.\n"
                f"sync stdout: {sync_result.stdout}\nsync stderr: {sync_result.stderr}"
            )
        finally:
            await _restore_package(pc2_executor, candidate)

    async def test_apt_sync_dry_run_changes_nothing(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """J58, J59 — `--dry-run` with the same automation mapping leaves pc2's `apt-mark showmanual`
        byte-identical before and after -- ADR-014's read-only preview contract for a
        package job.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = await _removable_candidate(pc1_executor, pc2_executor)

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_result.success, f"Failed to remove {candidate} from pc2: {remove_result.stderr}"

            await _write_apt_sync_config(pc1_executor)

            before = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert before.success, f"Failed to read pc2's apt-mark showmanual: {before.stderr}"

            item_id = AptPackageItem(name=candidate, version="").item_id
            sync_cmd = f"{_automation_env_assignment(item_id)} pc-switcher sync pc2 --yes --dry-run"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync --dry-run exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert after.stdout == before.stdout, (
                "--dry-run changed pc2's apt-mark showmanual output (ADR-014 violation).\n"
                f"before:\n{before.stdout}\nafter:\n{after.stdout}"
            )
        finally:
            await _restore_package(pc2_executor, candidate)

    async def test_apt_repository_state_dry_run_previews_derived_writes_and_reviews_no_repository(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """A65, C2, C165, C168, C169, J53, J116 — ADR-020 D-37/D-38 at VM level, in both directions at once.

        The synthetic repository the source has and the target lacks feeds NO package this
        run syncs, so under derivation it does not travel and it is not a review line
        either — ruling 4 working as intended, and the property a unit test can only assert
        against a mocked `/etc/apt`. The synthetic PIN beside it is in the always-sync
        bucket, so it travels with no review line at all, which is what the preview has to
        report for `--dry-run` to remain the whole truth about a run (ADR-014).

        This is the one test whose subject is legitimately the run's own output: `--dry-run`
        makes no filesystem change to assert against, so the preview IS the result. The
        decisions passed in name nothing — under this model the two `/etc/apt` files here
        need no decision, and a run that wrote them because something was ticked would be
        the defect.

        A fresh runner VM has neither file, so the test SETS UP its own divergence instead
        of skipping: a uuid-suffixed `.sources`+keyring pair and a uuid-suffixed
        `preferences.d` file, all written on the SOURCE (pc1). Because it is `--dry-run`
        nothing on pc2 changes; the synthetic files are removed from pc1 in a `finally`
        regardless of outcome.

        Every claim here is a PREVIEW LINE, never the bare presence of a filename: the run's
        log records each command's own output verbatim at DEBUG (`PKG-FR-LOG-VERBATIM`) and
        this config sets `tui: DEBUG`, so pc1's `sha256sum` listing of `/etc/apt/keyrings`
        and the `Signed-By:` scan both put the key's name on the run's output whatever the
        preview says about it.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        source_filename = ""
        key_filename = ""
        pin_filename = ""
        try:
            source_filename, key_filename = await _create_synthetic_repo_and_key(pc1_executor)
            pin_filename = await _create_synthetic_pin(pc1_executor)
            source_dest = f"{_APT_SOURCES_DIR}/{source_filename}"
            key_dest = f"{_APT_KEYRINGS_DIR}/{key_filename}"
            pin_dest = f"{_APT_PREFERENCES_DIR}/{pin_filename}"

            # The fresh target provably lacks every uuid-suffixed file, so the divergence is
            # exactly the one this test built (defensive, cheap: --dry-run writes nothing).
            absent = await pc2_executor.run_command(
                " && ".join(f"test ! -e {shlex.quote(path)}" for path in (source_dest, key_dest, pin_dest)),
                login_shell=False,
                timeout=10.0,
            )
            assert absent.success, "synthetic /etc/apt files unexpectedly already present on pc2 before the run"

            await _write_apt_sync_config(pc1_executor)

            # Nothing is decided: neither file is a question any more.
            sync_cmd = f"{_automation_env_assignment_multi({})} pc-switcher sync pc2 --yes --dry-run"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync --dry-run exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            combined_output = sync_result.stdout + sync_result.stderr
            collapsed = _collapse_run_output(combined_output)
            # The always-sync pin is previewed as a derived write, with no review entry.
            assert f"Would write {pin_dest}" in collapsed, (
                f"always-sync pin {pin_dest!r} was not previewed as a derived write.\n{combined_output}"
            )
            # The repository feeds no approved package, so nothing about it is written — and
            # it is offered in no direction, which is what makes "derived, never ticked" true.
            assert f"install {source_filename}" not in collapsed, (
                f"repository {source_filename!r} was still offered as a review entry.\n{combined_output}"
            )
            assert f"Would write {source_dest}" not in collapsed, (
                f"repository {source_dest!r} was previewed as a derived write although no approved package needs "
                f"it.\n{combined_output}"
            )
            # A key is previewed by `AptSyncJob.apply` for the repositories that survive the
            # run (`PKG-FR-DERIVED-VISIBLE`), and this repository is not one of them.
            assert f"Would write signing key {key_dest}" not in collapsed, (
                f"signing key {key_dest!r} was previewed as a write for a repository no package needed.\n"
                f"{combined_output}"
            )
            # The intended metadata refresh (the apt-get update the pin write requires) is
            # reported as its own marker item, by the label that item carries.
            assert "Would change Refresh apt package metadata (apt-get update)" in collapsed, (
                f"intended apt-get update (metadata refresh) not reported.\n{combined_output}"
            )
        finally:
            # Remove the synthetic files from pc1 (where they were created) regardless of
            # outcome; defensively from pc2 too, though --dry-run writes nothing there.
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
                await pc1_executor.run_command(f"sudo rm --force {cleanup_paths}", login_shell=False, timeout=15.0)
                await pc2_executor.run_command(f"sudo rm --force {cleanup_paths}", login_shell=False, timeout=15.0)

    async def test_a_package_the_targets_apt_cannot_locate_still_reaches_the_review(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """A30, J98 — ADR-020 D-34 class 3 at VM level: the repository that supplies the package is
        derived from the package's own approval and written during converge, so at plan time
        the target's apt has never heard the name and refuses to rehearse any transaction
        containing it.

        The property no mocked-executor test could establish: the whole run survives it, so
        the user sees the package and the rest of the diff rather than nothing at all.

        Run WITHOUT the automation hook, on purpose -- the same carve-out F23 takes: the
        hook answers a review without ever printing it, so with it set the package's name
        in the run's output comes from the dry-run apply preview and witnesses no review at
        all. `PKG-FR-NO-TERMINAL` then ends the job before `apply()`, which is what pins the
        name below to the printed group and nothing else.

        `--dry-run`, so pc2's `/etc/apt` and package set are untouched; the subject is built
        on pc1 and removed from pc1 in a `finally` regardless of outcome.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        name = repo_dir = list_filename = ""
        try:
            name, repo_dir, list_filename = await _install_from_a_repo_the_target_lacks(pc1_executor)

            # The precondition, asserted rather than assumed: without it the run below proves
            # nothing, because a target that CAN resolve the name never had the defect.
            refused = await pc2_executor.run_command(
                f"apt-get --dry-run install --assume-yes --no-install-recommends {shlex.quote(name)}",
                login_shell=False,
                timeout=60.0,
            )
            assert not refused.success, (
                f"pc2 resolved {name}, so this run cannot exercise the class-3 path.\n"
                f"stdout: {refused.stdout}\nstderr: {refused.stderr}"
            )

            await _write_apt_sync_config(pc1_executor)

            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --dry-run", timeout=300.0, login_shell=True
            )
            assert sync_result.success, (
                f"pc-switcher sync --dry-run exited {sync_result.exit_code} for a package pc2's apt "
                f"cannot locate yet.\nstdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            combined_output = sync_result.stdout + sync_result.stderr
            collapsed = _collapse_run_output(combined_output)
            # The group panel's own title is the witness. A dry run previews through the
            # review itself (ADR-014), so there is no separate apply output to confuse it
            # with, and only the review draws this title.
            assert "Install apt packages" in collapsed, (
                f"the run drew no apt install review group at all.\n{combined_output}"
            )
            assert f"install {name}" in collapsed, (
                f"{name} reached no review line, so the run survived by dropping it.\n{combined_output}"
            )
            assert "Unable to locate package" not in combined_output, (
                f"apt's plan-time refusal still surfaced as a run-level failure.\n{combined_output}"
            )
        finally:
            if name:
                await _remove_local_repo_package(pc1_executor, name, repo_dir, list_filename)


class TestPackageSyncWholeRunContracts:
    """VM-level proof of the phase's whole-run contracts (plan 02-11): properties of an
    entire sync -- non-interactive skip-all, continue-on-item-failure, snap/flatpak
    convergence, skip-always inertness in both roles, per-manager review-before-own-
    mutation, and one job's failure leaving every other job's approved work intact --
    rather than any single item's diff/converge, and therefore invisible to plans
    02-03/02-05/02-07/02-08's mocked-executor unit tests.
    """

    async def test_non_interactive_skip_all(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H162, J9, J12, J14, J37, J44, J49, J103 — A non-interactive `pc-switcher sync`
        (no `PACKAGE_REVIEW_AUTOMATION_ENV`, no
        TTY on stdin/stdout -- the default for a command run through this fixture's
        plain SSH exec, which requests no pty) applies nothing, records no permanent
        decision, names every item it could not ask about, and reports the job as skipped
        (`PKG-FR-NO-TERMINAL`, `PKG-FR-LOG-DECISIONS`), proven with an item diverged in
        each direction.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        install_candidate = await _removable_candidate(pc1_executor, pc2_executor)
        removal_candidate = await _create_extra_on_target_apt_package(pc1_executor, pc2_executor)

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(install_candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_result.success, f"Failed to remove {install_candidate} from pc2: {remove_result.stderr}"

            await _write_apt_sync_config(pc1_executor)

            pc2_manual_before = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            pc1_decision_before = await _decision_file_exists(pc1_executor, "apt")
            pc2_decision_before = await _decision_file_exists(pc2_executor, "apt")

            # No automation env prefix and no pty on this exec -- genuinely
            # non-interactive on both stdin and stdout, D-26's actual trigger condition.
            sync_cmd = "pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)
            assert sync_result.success, (
                "non-interactive sync unexpectedly failed (D-26's skip-all must not fail the job).\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            pc2_manual_after = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            assert pc2_manual_after == pc2_manual_before, (
                "non-interactive run changed pc2's apt-mark showmanual -- D-26 requires nothing applied"
            )

            pc1_decision_after = await _decision_file_exists(pc1_executor, "apt")
            pc2_decision_after = await _decision_file_exists(pc2_executor, "apt")
            assert pc1_decision_after == pc1_decision_before, (
                "non-interactive run created/removed a decision file on pc1"
            )
            assert pc2_decision_after == pc2_decision_before, (
                "non-interactive run created/removed a decision file on pc2"
            )

            # Secondary confirmation only -- the primary evidence above is pc2's own
            # package state and the decision-file paths (this plan's own prohibition).
            # `PKG-FR-LOG-DECISIONS` requires the run to NAME each item nobody could be
            # asked about, so a count would no longer say which ones were declined; and
            # `PKG-FR-NO-TERMINAL` requires the job itself to be reported skipped.
            combined_output = sync_result.stdout + sync_result.stderr
            collapsed = _collapse_run_output(combined_output)
            for candidate, direction in ((install_candidate, "install"), (removal_candidate, "removal")):
                assert f"not asked, declined for this run (no TTY): {candidate}" in collapsed, (
                    f"{direction}-direction item {candidate} was not named as declined for this run.\n"
                    f"{combined_output}"
                )
            assert "Job apt_sync skipped: non-interactive run left every apt review item undecided" in collapsed, (
                f"the run did not report apt_sync as skipped (PKG-FR-NO-TERMINAL).\n{combined_output}"
            )
        finally:
            await _restore_package(pc2_executor, install_candidate)
            await _restore_auto_marked_package(pc2_executor, removal_candidate)

    async def test_continue_on_item_failure(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """J20, J26, J34 — A failing item does not stop the job (D-27): the item ordered after it still
        converges, the failure's stderr and exit code land in the run's own summary, and
        the sync's own exit code is non-zero (the orchestrator derives it from job
        results, not from whether an exception propagated -- `_summarize_job_outcomes`).

        The failing item must genuinely reach the converge path. A package name that
        resolves to nothing is classified REPO_UNAVAILABLE/REPORT_ONLY (plan 02-05) and
        short-circuits before ever touching the target, so it would prove nothing about
        D-27. Instead this test authors three "unowned install" snippets (D-18/D-20)
        directly into pc1's registry -- two that genuinely `apt-get install` a real
        package, one that deliberately exits non-zero -- relying on
        `ManualInstallsSyncJob._scan_unowned_installs`'s alphabetical sort to place the failing one
        strictly between the two installs (`_CONTINUE_TEST_MARKERS`).
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidates = await _find_removable_candidates(pc1_executor, pc2_executor, count=2)
        assert len(candidates) == 2, (
            f"{_no_apt_candidate_message()} Needed 2 independent candidates, found {len(candidates)}."
        )
        pkg_first, pkg_second = candidates

        try:
            remove_result = await pc2_executor.run_command(
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes "
                f"{shlex.quote(pkg_first)} {shlex.quote(pkg_second)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_result.success, f"Failed to remove {pkg_first}/{pkg_second} from pc2: {remove_result.stderr}"

            for path in _CONTINUE_TEST_MARKERS:
                await _create_unowned_marker(pc1_executor, path)

            item_id_first = _unowned_item_id(_CONTINUE_TEST_MARKER_INSTALL_FIRST)
            item_id_fail = _unowned_item_id(_CONTINUE_TEST_MARKER_FAIL)
            item_id_second = _unowned_item_id(_CONTINUE_TEST_MARKER_INSTALL_SECOND)

            await _author_snippet(
                pc1_executor,
                item_id_first,
                _CONTINUE_TEST_MARKER_INSTALL_FIRST,
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(pkg_first)}",
            )
            await _author_snippet(
                pc1_executor,
                item_id_fail,
                _CONTINUE_TEST_MARKER_FAIL,
                'echo "deliberate integration-test failure" >&2; exit 42',
            )
            await _author_snippet(
                pc1_executor,
                item_id_second,
                _CONTINUE_TEST_MARKER_INSTALL_SECOND,
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes {shlex.quote(pkg_second)}",
            )

            # The three unowned-install snippets are owned by manual_installs_sync (D-18),
            # not apt_sync: enabling apt_sync alone leaves them inert, the sync exits 0, and
            # the D-27 `assert not sync_result.success` below never fires (the defect CI
            # caught on PR #206). With manual_installs_sync enabled they converge the same
            # run, and the deliberately-failing middle item makes the sync exit non-zero.
            await _write_package_sync_config(pc1_executor, manual_installs_sync=True)

            decisions = {
                item_id_first: Decision.APPLY,
                item_id_fail: Decision.APPLY,
                item_id_second: Decision.APPLY,
            }
            sync_cmd = f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)

            assert not sync_result.success, (
                "sync with a failed item must exit non-zero (D-27).\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after_lines = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            assert pkg_first in after_lines, f"{pkg_first} (before the failing item) not installed on pc2"
            assert pkg_second in after_lines, (
                f"{pkg_second} (after the failing item) not installed on pc2 -- "
                "D-27's 'continue, collect, report' promise did not hold"
            )

            # Secondary confirmation only -- the primary evidence above is pc2's own
            # apt-mark showmanual and the sync's own exit code: the failing item's
            # stderr should be named in the run's own failure summary
            # (PackageSyncJob.apply()'s per-item failure log).
            combined_output = sync_result.stdout + sync_result.stderr
            assert "deliberate integration-test failure" in combined_output
        finally:
            for path in _CONTINUE_TEST_MARKERS:
                await _remove_unowned_marker(pc1_executor, path)
            await _restore_package(pc2_executor, pkg_first)
            await _restore_package(pc2_executor, pkg_second)

    async def test_snap_revision_converges_without_hold(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E21, E67, K10 — snap convergence lands the target on the source's revision (D-06) without
        ever touching `snap get system refresh.hold` on either machine -- the exact
        constraint `SnapSyncJob` exists to satisfy (module docstring: `snap refresh
        --hold` with no snap name is a global-mutating command this job never calls).
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        name = await _snap_subject(pc1_executor, pc2_executor)
        pc1_list = await pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
        source_revision = parse_snap_list_names_revisions(pc1_list.stdout)[name]
        alternate_revision = await _alternate_snap_revision(pc2_executor, name, source_revision)

        pc2_list_before = await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
        original_pc2_revision = parse_snap_list_names_revisions(pc2_list_before.stdout)[name]
        pc1_hold_before = await _capture_system_refresh_hold(pc1_executor)
        pc2_hold_before = await _capture_system_refresh_hold(pc2_executor)

        try:
            diverge_result = await pc2_executor.run_command(
                f"sudo snap refresh --revision={shlex.quote(alternate_revision)} {shlex.quote(name)}",
                login_shell=False,
                timeout=120.0,
            )
            assert diverge_result.success, (
                f"Failed to diverge {name} to revision {alternate_revision} on pc2: {diverge_result.stderr}"
            )
            diverged = await pc2_executor.run_command(
                f"snap list {shlex.quote(name)}", login_shell=False, timeout=15.0
            )
            assert alternate_revision in diverged.stdout, f"pc2's {name} did not land on revision {alternate_revision}"

            await _write_package_sync_config(pc1_executor, snap_sync=True)

            item_id = f"snap:{name}"
            sync_cmd = f"{_automation_env_assignment(item_id)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            converged = await pc2_executor.run_command(
                f"snap list {shlex.quote(name)}", login_shell=False, timeout=15.0
            )
            assert source_revision in converged.stdout, (
                f"pc2's {name} did not converge to source revision {source_revision}.\n{converged.stdout}"
            )

            pc1_hold_after = await _capture_system_refresh_hold(pc1_executor)
            pc2_hold_after = await _capture_system_refresh_hold(pc2_executor)
            assert pc1_hold_after == pc1_hold_before, "sync mutated pc1's refresh.hold"
            assert pc2_hold_after == pc2_hold_before, (
                "sync mutated pc2's refresh.hold -- D-06 forbids the convergence mechanism from blocking auto-refresh"
            )
        finally:
            await pc2_executor.run_command(
                f"sudo snap refresh --revision={shlex.quote(original_pc2_revision)} {shlex.quote(name)}",
                login_shell=False,
                timeout=120.0,
            )

    async def test_flatpak_derives_the_remote_its_ref_needs_and_carries_its_key(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """K11 — flatpak convergence installs into the scope the source item carries and
        provisions the remote first (D-06, D-14): `flatpak install` refuses outright
        when its remote is not yet configured in that scope.

        The remote is DERIVED (ADR-020 D-41): a remote is no review entry in any
        direction, so the review below decides the ref alone and the remote travels as a
        consequence of that one approval.

        The subject is a real Flathub app, present on pc1 only (`vm-test-fixtures.sh`), and
        the real Flathub remote. Nothing on pc2 trusts Flathub once this test deletes the
        remote there — `flatpak remote-delete` takes `flathub.trustedkeys.gpg` with it and
        Ubuntu ships no machine-level anchor for it (both verified live) — so the ref
        install after the sync can only succeed if pc-switcher carried the remote's real
        signing key across (#215), which is exactly the claim this makes.

        pc2 keeps the app's runtime throughout, so the install the sync performs is the
        146 kB app and nothing else.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        application, version, scope, remote_name, _remote_url, ref = await _flatpak_subject(pc1_executor)
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""

        ref_item_id = FlatpakItem(
            application=application, version=version, origin=remote_name, scope=scope, ref=ref
        ).item_id

        try:
            # The app is already absent on pc2 (it is installed on pc1 only), so this is
            # defensive: it keeps the test independent of anything an earlier failure left
            # behind. Deleting the remote is NOT defensive — it is what removes pc2's only
            # trust in Flathub and makes the key replication load-bearing.
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall --assumeyes {scope_flag} {shlex.quote(application)}",
                login_shell=False,
                timeout=60.0,
            )
            await pc2_executor.run_command(
                f"{sudo}flatpak remote-delete --force {scope_flag} {shlex.quote(remote_name)}",
                login_shell=False,
                timeout=30.0,
            )

            before_refs = await pc2_executor.run_command(
                f"flatpak list --app {scope_flag} --columns=application", login_shell=False, timeout=15.0
            )
            assert application not in nonblank_lines(before_refs.stdout), (
                f"{application} still installed on pc2 after uninstall; cannot prove D-14 from a pre-existing state"
            )
            before_remotes = await pc2_executor.run_command(
                f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
            )
            assert remote_name not in nonblank_lines(before_remotes.stdout), (
                f"remote {remote_name} still configured on pc2 after remote-delete"
            )

            await _write_package_sync_config(pc1_executor, flatpak_sync=True)

            decisions = {ref_item_id: Decision.APPLY}
            sync_cmd = f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync"
            # Longer than the other syncs in this module: with pc2's runtime in place the
            # app install takes about a second, but if Flathub has moved the app onto a
            # newer runtime major since the baseline was built, this install pulls that
            # runtime. The fixture script fails loudly on that drift; the headroom here is
            # what keeps the failure legible instead of a timeout.
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=900.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after_remotes = await pc2_executor.run_command(
                f"flatpak remotes {scope_flag} --columns=name,url", login_shell=False, timeout=15.0
            )
            remote_lines = nonblank_lines(after_remotes.stdout)
            assert any(line.split("\t")[0] == remote_name for line in remote_lines), (
                f"remote {remote_name} not provisioned in scope {scope} on pc2 after sync"
            )

            after_refs = await pc2_executor.run_command(
                f"flatpak list --app {scope_flag} --columns=application", login_shell=False, timeout=15.0
            )
            assert application in nonblank_lines(after_refs.stdout), (
                f"{application} not installed in scope {scope} on pc2 after sync"
            )

            # The one ordering exception this plan's own prohibition carves out for
            # THIS particular claim: the remote's mere presence afterwards does not
            # distinguish "remote added before ref" from any other order, so only the
            # run's own per-item converge log (PackageSyncJob._converge_one) proves it.
            combined_output = sync_result.stdout + sync_result.stderr
            remote_marker = f"provision {scope} flatpak remote {remote_name}"
            ref_marker = f"install {ref} ("
            remote_index = combined_output.find(remote_marker)
            ref_index = combined_output.find(ref_marker)
            assert remote_index != -1, f"derived remote write log line not found: {remote_marker!r}"
            assert ref_index != -1, f"ref converge log line not found: {ref_marker!r}"
            assert remote_index < ref_index, "remote must be provisioned before the ref installs (D-14)"
        finally:
            # Put pc2 back to a freshly provisioned TARGET's state: Flathub configured
            # with its real trust, the runtime kept, the app gone again. Leaving the app
            # installed would converge the pair and silently make a re-run of this test
            # prove nothing.
            await _restore_flatpak_target_baseline(pc2_executor)

    async def test_a_flatpak_remote_no_synced_ref_needs_does_not_travel(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """The other half of derivation (ADR-020 D-37): a remote the source has and no
        approved ref comes from stays where it is.

        Without this, "the target ends up with the source's remotes" and "the target ends
        up with the remotes its refs need" are indistinguishable — the baseline used to
        carry exactly one remote, from which the subject app also came. `flathub-beta` is
        on pc1 only and feeds nothing (`vm-test-fixtures.sh`, FIXTURES_VERSION 4).

        There is no flatpak counterpart to apt's never-removed distribution sources here:
        a fresh flatpak install configures zero remotes, so nothing is exempt from this.

        pc2 loses the approved ref's OWN remote too, not just the ref: the guard that a
        derived remote did travel is what separates this claim from a run that provisioned
        nothing at all, and it can only fail if that remote is absent beforehand.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        application, version, scope, remote_name, _remote_url, ref = await _flatpak_subject(pc1_executor)
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""
        ref_item_id = FlatpakItem(
            application=application, version=version, origin=remote_name, scope=scope, ref=ref
        ).item_id

        source_remotes = await pc1_executor.run_command(
            f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
        )
        assert _FIXTURE_UNUSED_FLATPAK_REMOTE in nonblank_lines(source_remotes.stdout), (
            f"the fixture remote {_FIXTURE_UNUSED_FLATPAK_REMOTE} is not configured on pc1. It is created by "
            f"tests/integration/scripts/internal/vm-test-fixtures.sh.\n{source_remotes.stdout}"
        )
        before = await pc2_executor.run_command(
            f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
        )
        assert _FIXTURE_UNUSED_FLATPAK_REMOTE not in nonblank_lines(before.stdout), (
            f"{_FIXTURE_UNUSED_FLATPAK_REMOTE} is already on pc2, so this run cannot show that it did not travel"
        )

        try:
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall --assumeyes {scope_flag} {shlex.quote(ref)}",
                login_shell=False,
                timeout=60.0,
            )
            await pc2_executor.run_command(
                f"{sudo}flatpak remote-delete --force {scope_flag} {shlex.quote(remote_name)}",
                login_shell=False,
                timeout=30.0,
            )
            remotes_before = await pc2_executor.run_command(
                f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
            )
            assert remote_name not in nonblank_lines(remotes_before.stdout), (
                f"remote {remote_name} is still configured on pc2 after remote-delete, so its presence after the "
                "sync would say nothing about what the run provisioned"
            )
            await _write_package_sync_config(pc1_executor, flatpak_sync=True)

            sync_cmd = (
                f"{_automation_env_assignment_multi({ref_item_id: Decision.APPLY})} "
                "pc-switcher sync pc2 --yes --allow-first-sync"
            )
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=900.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after = await pc2_executor.run_command(
                f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
            )
            remotes_after = nonblank_lines(after.stdout)
            # The approved ref's own remote DID travel, from an absence asserted above:
            # without this the assertion below would pass on a run that provisioned
            # nothing at all.
            assert remote_name in remotes_after, f"{remote_name} not provisioned on pc2 after sync"
            assert _FIXTURE_UNUSED_FLATPAK_REMOTE not in remotes_after, (
                f"{_FIXTURE_UNUSED_FLATPAK_REMOTE} travelled to pc2 although no approved ref comes from it"
            )
        finally:
            await _restore_flatpak_target_baseline(pc2_executor)

    async def test_one_ref_from_two_vendors_is_reported_with_both_urls(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """ADR-020 D-41 at VM level: one ref, one scope, one branch, two vendors is
        `ORIGIN_MISMATCH` -- reported naming both remotes and both URLs, and converged by
        nothing.

        The divergence is built by installing the fixture app on pc2 from the real Flathub
        and then repointing pc2's `flathub` at the beta repository's URL (`remote-modify
        --url=`, no second download: the app is already there). Both machines then print
        `flathub` in `flatpak list --columns=origin`, which is asserted below BEFORE the
        sync -- so a comparison by remote NAME provably sees nothing here, and only the URL
        comparison D-41 mandates can produce the finding. The two URLs are what the run has
        to name, and the assertion that it does is what proves the origin column and the
        remote table were read the way the comparison assumes.

        Run WITHOUT the automation hook, on purpose. A `REPORT_ONLY` diff makes no
        filesystem change to assert against, so the review panel IS the result -- the same
        carve-out the apt repository-state dry run takes -- and the automation hook answers
        a review without ever printing it. D-26 then skips the job, which is why pc2's
        unchanged app list below is a guard against a convergence that must not happen
        rather than the claim itself; that a ticked `ORIGIN_MISMATCH` still converges
        nothing is `sync_core.apply()`'s own exclusion, asserted by unit test.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        application, _version, scope, remote_name, source_url, ref = await _flatpak_subject(pc1_executor)
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""

        # The fixture's second remote supplies a real, differently-vendored URL, so nothing
        # here invents one. Both Flathub keyrings share a sha256 (measured, vm-test-fixtures.sh),
        # which is why the URL -- never a key digest -- is the whole evidence.
        beta_url, _beta_options = await _flatpak_remote_row(pc1_executor, _FIXTURE_UNUSED_FLATPAK_REMOTE, scope)
        assert beta_url != source_url, (
            f"pc1's {remote_name} and {_FIXTURE_UNUSED_FLATPAK_REMOTE} both report {source_url!r}, so no vendor "
            "divergence can be built from the fixture remotes "
            "(tests/integration/scripts/internal/vm-test-fixtures.sh)"
        )

        try:
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

            before_rows = await _flatpak_app_rows(pc2_executor)
            await _write_package_sync_config(pc1_executor, flatpak_sync=True)

            # No automation env and no pty: the non-interactive path prints every group.
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync", timeout=900.0, login_shell=True
            )
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            combined_output = sync_result.stdout + sync_result.stderr
            collapsed = _collapse_run_output(combined_output)
            # A report group is titled by its CAUSE (`sync_core._REPORT_TITLES`), so this
            # asserts the mismatch reached the ORIGIN_MISMATCH group specifically rather
            # than any report group at all — which is the distinction the version-mismatch
            # check below is here to make.
            assert "Installed from different remotes (flatpak applications)" in collapsed, (
                f"the mismatch reached no origin-mismatch review group.\n{combined_output}"
            )
            assert ref in combined_output, f"the report does not name the ref {ref}.\n{combined_output}"
            # The discriminating pair: a VERSION_MISMATCH -- what this diverged pair would
            # produce if the vendor comparison missed -- names two versions and no URL at all.
            assert source_url in combined_output, (
                f"the report does not name the source's vendor {source_url}.\n{combined_output}"
            )
            assert target_url in combined_output, (
                f"the report does not name the target's vendor {target_url}.\n{combined_output}"
            )

            after_rows = await _flatpak_app_rows(pc2_executor)
            assert after_rows == before_rows, (
                "the run changed pc2's installed refs; an ORIGIN_MISMATCH is reported and converged by nothing.\n"
                f"before: {before_rows}\nafter: {after_rows}"
            )
        finally:
            # `_restore_flatpak_target_baseline` re-adds with `--if-not-exists`, which cannot
            # repair a URL, so the repointed remote is deleted here first.
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(application)} || true; "
                f"{sudo}flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true",
                login_shell=False,
                timeout=120.0,
            )
            await _restore_flatpak_target_baseline(pc2_executor)

    async def test_a_source_filter_replicates_and_a_target_only_filter_comes_off(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """`PKG-FR-FLATPAK-FILTER` at VM level, both halves, in two runs.

        Run 1: pc1's `flathub` carries a ref filter and pc2 has no `flathub` at all, so the
        derived write provisions it — and the filter file lands at the SAME absolute path on
        pc2 with the source's bytes, with `flatpak remote-modify --filter=` naming it there.
        A filter is derived like a signing key and is a review entry in no direction, so the
        decision below names the application alone.

        Run 2: pc1's filter is gone and pc2 still carries run 1's, which is the only case
        `_apply_remote_filters` cannot reach on its own — it iterates the SOURCE's filters.
        `_clear_target_filters` is what takes it off, before the application installs, and
        pc2 ends the run unfiltered.

        What only a real machine can establish is the fact the whole behaviour rests on:
        that `flatpak remote-modify --filter=<file>` puts a `filtered` token in THIS
        flatpak's `flatpak remotes --columns=options` and the path in its `filter` column,
        which is what `_parse_flatpak_remotes` reads. Both are asserted on pc1 before the
        first sync, so a flatpak that records the filter some other way fails here naming the
        command instead of leaving this test green for the wrong reason.

        The filter body allows the fixture app and denies nothing (`_FLATPAK_FILTER_BODY`),
        so it never stands between the run and the install it is replicated alongside; that
        a denying filter really does refuse an install is measured in
        `docs/adr/considerations/adr-020-flatpak-filter-and-trust-measurements.md`.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        application, version, scope, remote_name, _source_url, ref = await _flatpak_subject(pc1_executor)
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""
        ref_item_id = FlatpakItem(
            application=application, version=version, origin=remote_name, scope=scope, ref=ref
        ).item_id
        # Unquoted `$HOME` on purpose: the remote shell expands it, and flatpak stores
        # whatever path it is given verbatim. `_flatpak_remote_filter` reads the expanded
        # path back off the machine, which is the one both machines must end up naming.
        filter_path = f"$HOME/.cache/pcswitcher-it-flatpak-filter-{uuid4().hex[:12]}"
        recorded_path = ""

        await _write_package_sync_config(pc1_executor, flatpak_sync=True)
        sync_cmd = f"{_automation_env_assignment(ref_item_id)} pc-switcher sync pc2 --yes --allow-first-sync"

        try:
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
            _source_url_after, source_options = await _flatpak_remote_row(pc1_executor, remote_name, scope)
            assert _FLATPAK_FILTERED_OPTION in source_options, (
                f"pc1's {remote_name} reports options {source_options!r} after `flatpak remote-modify {scope_flag} "
                f"--filter=`, so this flatpak does not print the {_FLATPAK_FILTERED_OPTION!r} token "
                "`flatpak_sync._FILTERED_OPTION` reads"
            )
            recorded_path = await _flatpak_remote_filter(pc1_executor, remote_name, scope) or ""
            assert recorded_path, (
                f"pc1's {remote_name} names no file in `flatpak remotes {scope_flag} --columns=filter`, so this "
                "flatpak does not record the path `flatpak_sync` replicates"
            )

            # The app is absent on pc2 by fixture; deleting the remote is what makes the
            # derived write provision it, and the target's own filter column meaningful.
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(application)} || true; "
                f"{sudo}flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true",
                login_shell=False,
                timeout=120.0,
            )
            before_remotes = await pc2_executor.run_command(
                f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
            )
            assert remote_name not in nonblank_lines(before_remotes.stdout), (
                f"remote {remote_name} still configured on pc2, so this run cannot show it being provisioned"
            )
            assert application not in [row[0] for row in await _flatpak_app_rows(pc2_executor)], (
                f"{application} still installed on pc2, so no approved application derives {remote_name}"
            )

            first_result = await pc1_executor.run_command(sync_cmd, timeout=900.0, login_shell=True)
            assert first_result.success, (
                f"pc-switcher sync exited {first_result.exit_code}.\n"
                f"stdout: {first_result.stdout}\nstderr: {first_result.stderr}"
            )

            # The app first: a remote provisioned but not usable fails here rather than as a
            # confusing lookup miss on the rows read below.
            assert application in [row[0] for row in await _flatpak_app_rows(pc2_executor)], (
                f"{application} not installed on pc2 -- {remote_name} was never usably provisioned.\n"
                f"stdout: {first_result.stdout}\nstderr: {first_result.stderr}"
            )
            _target_url, target_options = await _flatpak_remote_row(pc2_executor, remote_name, scope)
            assert _FLATPAK_FILTERED_OPTION in target_options, (
                f"pc2's provisioned {remote_name} reports options {target_options!r}: the source's ref filter was not "
                f"re-applied there.\nstdout: {first_result.stdout}\nstderr: {first_result.stderr}"
            )
            assert await _flatpak_remote_filter(pc2_executor, remote_name, scope) == recorded_path, (
                f"pc2's {remote_name} does not name {recorded_path} as its ref filter -- the file must land at the "
                "same absolute path the source records"
            )
            copied = await pc2_executor.run_command(
                f"cat {shlex.quote(recorded_path)}", login_shell=False, timeout=15.0
            )
            assert copied.success and copied.stdout == _FLATPAK_FILTER_BODY, (
                f"pc2's copy of the ref filter at {recorded_path} is not the source's file byte-for-byte: "
                f"{copied.stdout!r} ({copied.stderr})"
            )

            _source_url_final, source_options_after = await _flatpak_remote_row(pc1_executor, remote_name, scope)
            assert _FLATPAK_FILTERED_OPTION in source_options_after, (
                f"the run changed pc1's own {remote_name}: a source is read, never converged"
            )

            # Run 2: the source no longer filters, pc2 still does. Delete-and-re-add rather
            # than `--no-filter` for `_restore_flatpak_source_baseline`'s own reason.
            await _restore_flatpak_source_baseline(pc1_executor, remote_name, scope, filter_path)
            assert await _flatpak_remote_filter(pc1_executor, remote_name, scope) is None, (
                f"pc1's {remote_name} still carries a ref filter, so run 2 cannot show a target-only one coming off"
            )
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall {scope_flag} --assumeyes {shlex.quote(application)}",
                login_shell=False,
                timeout=120.0,
            )
            assert await _flatpak_remote_filter(pc2_executor, remote_name, scope) == recorded_path, (
                f"pc2's {remote_name} lost its ref filter before run 2 started; there is no target-only filter to "
                "take off"
            )

            second_cmd = f"{sync_cmd} --allow-out-of-order"
            second_result = await pc1_executor.run_command(second_cmd, timeout=900.0, login_shell=True)
            assert second_result.success, (
                f"second pc-switcher sync exited {second_result.exit_code}.\n"
                f"stdout: {second_result.stdout}\nstderr: {second_result.stderr}"
            )

            assert await _flatpak_remote_filter(pc2_executor, remote_name, scope) is None, (
                f"pc2's {remote_name} still carries a ref filter the source does not have -- the two machines would "
                f"never converge.\nstdout: {second_result.stdout}\nstderr: {second_result.stderr}"
            )
            assert application in [row[0] for row in await _flatpak_app_rows(pc2_executor)], (
                f"{application} not installed on pc2 by the second run.\n"
                f"stdout: {second_result.stdout}\nstderr: {second_result.stderr}"
            )
        finally:
            await _restore_flatpak_source_baseline(pc1_executor, remote_name, scope, filter_path)
            if recorded_path:
                # The replicated copy is pc-switcher's own write and lives outside anything
                # `_restore_flatpak_target_baseline` knows about.
                await pc2_executor.run_command(
                    f"rm --force {shlex.quote(recorded_path)}", login_shell=False, timeout=15.0
                )
            await _restore_flatpak_target_baseline(pc2_executor)

    async def test_skip_always_is_inert_in_both_roles(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H125, H126, H166, N1, N2 — A skip-always decision recorded in one run makes the item produce no diff in
        the next run, in BOTH roles this machine can play (D-08): source (never pushed
        again) and target (never installed/removed here again).

        The ordinary review checkbox has no UI path to SKIP_ALWAYS yet for a regular
        item (`package_review.py`'s own docstring: only the unreproducible-items'
        three-way prompt and a hand-constructed `ReviewOutcome` reach it today) -- this
        test drives it through the same `PACKAGE_REVIEW_AUTOMATION_ENV` hook every
        other test in this module uses, proving the underlying mechanism
        (`PackageSyncJob._record_permanent_skips`/`filter_inert`) independent of that
        UI gap.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = await _removable_candidate(pc1_executor, pc2_executor)
        item_id = AptPackageItem(name=candidate, version="").item_id

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_result.success, f"Failed to remove {candidate} from pc2: {remove_result.stderr}"

            await _write_apt_sync_config(pc1_executor)

            skip_always = {item_id: Decision.SKIP_ALWAYS}
            first_sync_cmd = (
                f"{_automation_env_assignment_multi(skip_always)} pc-switcher sync pc2 --yes --allow-first-sync"
            )
            first_result = await pc1_executor.run_command(first_sync_cmd, timeout=180.0, login_shell=True)
            assert first_result.success, (
                f"skip-always run unexpectedly failed.\nstdout: {first_result.stdout}\nstderr: {first_result.stderr}"
            )

            entries = await DecisionFile("apt", pc1_executor).load()
            assert item_id in entries, (
                f"{candidate} not recorded in pc1's apt decision file after a skip-always decision (D-08a)"
            )

            still_absent = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(still_absent.stdout), "skip-always must not itself install the item"

            # Second sync, SOURCE role, same direction: force-map the same item to
            # APPLY. If D-08's inertness genuinely holds, the item never becomes a diff
            # at all, so this mapping has nothing to attach to -- proven by the package
            # staying absent despite explicitly asking for it to be applied.
            # --allow-out-of-order bypasses the unrelated W3 consecutive-push gate a
            # second same-direction sync would otherwise trip (ADR-015) -- orthogonal
            # to what this test proves.
            force_apply = {item_id: Decision.APPLY}
            second_sync_cmd = (
                f"{_automation_env_assignment_multi(force_apply)} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order"
            )
            second_result = await pc1_executor.run_command(second_sync_cmd, timeout=180.0, login_shell=True)
            assert second_result.success, (
                f"second sync unexpectedly failed.\nstdout: {second_result.stdout}\nstderr: {second_result.stderr}"
            )
            still_absent_2 = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(still_absent_2.stdout), (
                f"{candidate} was installed on pc2 despite a source-held skip-always decision -- "
                "the item produced a diff when it should have been filtered out entirely (D-08)"
            )

            # Reversed role: pc2 as source, pc1 as target. The decision lives on pc1
            # (this machine), now the TARGET -- D-08 promises inertness there too, so
            # force-mapping the same item to APPLY (which, if a diff existed at all,
            # would mean REMOVE -- pc1 genuinely still has the package installed) must
            # still leave it untouched.
            await _write_apt_sync_config(pc2_executor)
            reversed_sync_cmd = (
                f"{_automation_env_assignment_multi(force_apply)} "
                "pc-switcher sync pc1 --yes --allow-first-sync --allow-out-of-order"
            )
            reversed_result = await pc2_executor.run_command(reversed_sync_cmd, timeout=180.0, login_shell=True)
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
            await _restore_package(pc2_executor, candidate)

    async def test_each_manager_reviews_before_its_own_mutation(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """H12, J144, K20 — The corrected D-24 (per-manager review): with two package jobs enabled and
        both machines diverged, each enabled manager completes its OWN batched review
        before that same manager issues its OWN first mutating command. With the
        cross-manager coordinator gone (plan 02-15), the old "no manager mutates before
        EVERY manager has diffed" contract no longer exists and is not asserted here --
        each job runs plan -> review -> apply inside its own `execute()`, independently.

        The per-manager property is proven by end state, not a log witness: an item
        converges on pc2 ONLY because that manager's own review returned APPLY for it
        (`apply()` reads the accepted review outcome), so both items landing on pc2's own
        package managers -- apt via `apt-mark showmanual`, snap via `snap list` -- is the
        witness that each manager reviewed-then-mutated its own diff. No inter-manager
        ordering is asserted (this plan's prohibition), and no run-log line is scraped.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        apt_candidate = await _removable_candidate(pc1_executor, pc2_executor)
        snap_candidate = await _snap_subject(pc1_executor, pc2_executor)

        pc2_snap_list_before = await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
        original_snap_revision = parse_snap_list_names_revisions(pc2_snap_list_before.stdout)[snap_candidate]

        try:
            remove_apt = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(apt_candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_apt.success, f"Failed to remove {apt_candidate} from pc2: {remove_apt.stderr}"

            remove_snap = await pc2_executor.run_command(
                f"sudo snap remove {shlex.quote(snap_candidate)}", login_shell=False, timeout=60.0
            )
            assert remove_snap.success, f"Failed to remove {snap_candidate} from pc2: {remove_snap.stderr}"

            await _write_package_sync_config(pc1_executor, apt_sync=True, snap_sync=True)

            apt_item_id = AptPackageItem(name=apt_candidate, version="").item_id
            snap_item_id = f"snap:{snap_candidate}"
            decisions = {apt_item_id: Decision.APPLY, snap_item_id: Decision.APPLY}
            sync_cmd = f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            # Per-manager end state is the witness (this plan's prohibition: assert the
            # target's own package-manager state, not a run-log line): apt's item is back
            # in pc2's own `apt-mark showmanual`, and snap's item is back in pc2's own
            # `snap list`. Each converged only because its OWN manager's review approved
            # it, so both landing proves each manager reviewed-then-mutated its own diff.
            after_apt = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert apt_candidate in nonblank_lines(after_apt.stdout), (
                f"{apt_candidate} not reinstalled on pc2 -- apt_sync did not converge its own approved diff"
            )
            after_snap = await pc2_executor.run_command(
                f"snap list {shlex.quote(snap_candidate)}", login_shell=False, timeout=15.0
            )
            assert after_snap.success, (
                f"{snap_candidate} not reinstalled on pc2 -- snap_sync did not converge its own approved diff: "
                f"{after_snap.stderr}"
            )
        finally:
            await _restore_package(pc2_executor, apt_candidate)
            current_snap = await pc2_executor.run_command(
                f"snap list {shlex.quote(snap_candidate)}", login_shell=False, timeout=15.0
            )
            if original_snap_revision not in current_snap.stdout:
                restore_result = await pc2_executor.run_command(
                    f"sudo snap install --revision={shlex.quote(original_snap_revision)} "
                    f"{shlex.quote(snap_candidate)} || "
                    f"sudo snap refresh --revision={shlex.quote(original_snap_revision)} "
                    f"{shlex.quote(snap_candidate)}",
                    login_shell=False,
                    timeout=120.0,
                )
                if not restore_result.success:
                    print(
                        f"[cleanup] failed to restore {snap_candidate} to revision "
                        f"{original_snap_revision} on pc2: {restore_result.stderr}"
                    )

    async def test_one_failing_job_leaves_the_other_jobs_work_intact(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """N22 — All four package jobs enabled in one run, the first of them failing: the three
        ordered after it still review and converge their own diffs, and the sync's own exit
        code reports the failure (`PKG-FR-JOB-INDEPENDENCE`, `PKG-FR-OUTCOME-FAILED`).

        The failure has to come FIRST for the claim to mean anything -- a job that fails last
        leaves the others' work intact whatever the orchestrator does. Jobs run in the order
        the config names them (`_discover_and_validate_jobs` iterates `sync_jobs` as written),
        so `manual_installs_sync` is written first and fails on a snippet that exits non-zero,
        the same device as `test_continue_on_item_failure` but at whole-run scale.

        `flatpak_sync` is enabled and left unanswered: this run's claim is about four jobs
        being enabled together, and a job whose items are all declined still plans, reviews
        and reports -- it just converges nothing, which is why nothing is asserted about it.

        The witness is pc2's own package managers, as everywhere else in this module: the apt
        package is back in `apt-mark showmanual` and the snap is back in `snap list`, each of
        which could only happen if that manager reviewed its own diff and then applied it,
        after the run had already failed a job.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        apt_candidate = await _removable_candidate(pc1_executor, pc2_executor)
        snap_candidate = await _snap_subject(pc1_executor, pc2_executor)

        pc2_snap_list_before = await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
        original_snap_revision = parse_snap_list_names_revisions(pc2_snap_list_before.stdout)[snap_candidate]

        try:
            remove_apt = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(apt_candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_apt.success, f"Failed to remove {apt_candidate} from pc2: {remove_apt.stderr}"

            remove_snap = await pc2_executor.run_command(
                f"sudo snap remove {shlex.quote(snap_candidate)}", login_shell=False, timeout=60.0
            )
            assert remove_snap.success, f"Failed to remove {snap_candidate} from pc2: {remove_snap.stderr}"

            await _create_unowned_marker(pc1_executor, _WHOLE_RUN_FAILURE_MARKER)
            failing_item_id = _unowned_item_id(_WHOLE_RUN_FAILURE_MARKER)
            await _author_snippet(
                pc1_executor,
                failing_item_id,
                _WHOLE_RUN_FAILURE_MARKER,
                f'echo "{_WHOLE_RUN_FAILURE_MESSAGE}" >&2; exit 42',
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

            apt_item_id = AptPackageItem(name=apt_candidate, version="").item_id
            decisions = {
                failing_item_id: Decision.APPLY,
                apt_item_id: Decision.APPLY,
                f"snap:{snap_candidate}": Decision.APPLY,
            }
            sync_cmd = f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)

            assert not sync_result.success, (
                "a run with a failed job must exit non-zero (PKG-FR-OUTCOME-FAILED).\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after_apt = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert apt_candidate in nonblank_lines(after_apt.stdout), (
                f"{apt_candidate} not reinstalled on pc2 -- apt_sync's approved work did not survive the "
                "earlier job's failure (PKG-FR-JOB-INDEPENDENCE)"
            )
            after_snap = await pc2_executor.run_command(
                f"snap list {shlex.quote(snap_candidate)}", login_shell=False, timeout=15.0
            )
            assert after_snap.success, (
                f"{snap_candidate} not reinstalled on pc2 -- snap_sync's approved work did not survive the "
                f"earlier job's failure (PKG-FR-JOB-INDEPENDENCE): {after_snap.stderr}"
            )

            # Secondary confirmation only -- the exit code and pc2's own managers above are
            # the primary evidence. This says the non-zero exit is THIS failure's and not
            # some unrelated trouble, which the exit code alone cannot distinguish.
            assert _WHOLE_RUN_FAILURE_MESSAGE in sync_result.stdout + sync_result.stderr
        finally:
            await _remove_unowned_marker(pc1_executor, _WHOLE_RUN_FAILURE_MARKER)
            await _remove_unowned_marker(pc2_executor, _WHOLE_RUN_FAILURE_MARKER)
            await _restore_package(pc2_executor, apt_candidate)
            current_snap = await pc2_executor.run_command(
                f"snap list {shlex.quote(snap_candidate)}", login_shell=False, timeout=15.0
            )
            if original_snap_revision not in current_snap.stdout:
                restore_result = await pc2_executor.run_command(
                    f"sudo snap install --revision={shlex.quote(original_snap_revision)} "
                    f"{shlex.quote(snap_candidate)} || "
                    f"sudo snap refresh --revision={shlex.quote(original_snap_revision)} "
                    f"{shlex.quote(snap_candidate)}",
                    login_shell=False,
                    timeout=120.0,
                )
                if not restore_result.success:
                    print(
                        f"[cleanup] failed to restore {snap_candidate} to revision "
                        f"{original_snap_revision} on pc2: {restore_result.stderr}"
                    )


class TestManualInstallsSyncEndToEnd:
    """VM-level proof that `manual_installs_sync` pushes the install-snippet registry to
    the target with its OWN `send_file()` (D-23) and replays a snippet there (D-18/D-20),
    against pc2's own filesystem and registry file -- never pc-switcher's log text.
    """

    async def test_manual_installs_sync_pushes_registry_and_replays_snippet(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """G67, H30, K12, K16 — A snippet registered on the source travels to the target by the job's own push
        and is replayed there, all in one run (D-23), proven against pc2's own filesystem
        and registry file.

        Under the corrected source-based classification (02-22), `manual_installs_sync.plan()`
        judges reproducibility from the SOURCE registry: a snippet present on pc1 classifies
        the item `INSTALL` regardless of whether pc2 already holds one. `after_review()` then
        pushes the source registry to the target before `apply()` replays it. So the snippet
        is authored on the SOURCE (pc1) ONLY -- no target seeding is needed, and the old
        item-on-both-machines trick (an OLD body on pc2 to force the pre-correction
        target-registry classification) is gone. pc2 has no registry before the run, so its
        presence afterwards witnesses the push, and the NEW marker witnesses the pushed
        source snippet being replayed the same run. An unowned `/opt` path on pc1 makes the
        item detectable (`_scan_unowned_installs`).
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        uniq = uuid4().hex[:12]
        unowned_path = f"/opt/pcswitcher-it-manual-{uniq}"
        item_id = _unowned_item_id(unowned_path)
        # Home-relative marker so the snippet needs no sudo: replay runs `bash -c <body>`
        # as the SSH user on pc2, and $HOME expands there.
        new_marker = f"$HOME/.cache/pcswitcher-it-manual-new-{uniq}"
        registry_relpath = "~/.config/pc-switcher/package-snippets.yaml"

        try:
            # The source item to detect (unowned, so root-owned /opt needs sudo to create).
            await _create_unowned_marker(pc1_executor, unowned_path)

            # Author on the SOURCE (pc1) ONLY: plan() classifies reproducibility from the
            # source registry (corrected D-23), so a source snippet plans INSTALL without
            # pc2 holding anything. The post-review push then places it on pc2 before replay.
            await _author_snippet(
                pc1_executor, item_id, unowned_path, f'mkdir --parents "$(dirname {new_marker})" && touch {new_marker}'
            )

            await _write_package_sync_config(pc1_executor, manual_installs_sync=True)

            sync_cmd = f"{_automation_env_assignment(item_id)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            # The push placed the source registry on pc2 (D-23): pc2 held no registry before
            # the run, so its presence afterwards witnesses the push landing.
            registry_exists = await pc2_executor.run_command(
                f"test -f {registry_relpath}", login_shell=False, timeout=10.0
            )
            assert registry_exists.success, (
                f"snippet registry not present on pc2 at {registry_relpath} after the run -- the push did not land"
            )

            # The replay ran the pushed source snippet body: the NEW marker exists on pc2 --
            # proving the pushed source snippet was replayed the same run (D-23).
            new_exists = await pc2_executor.run_command(f"test -f {new_marker}", login_shell=False, timeout=10.0)
            assert new_exists.success, (
                f"NEW marker {new_marker} absent on pc2 -- the pushed snippet was not replayed.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
        finally:
            await _remove_unowned_marker(pc1_executor, unowned_path)
            await pc2_executor.run_command(
                f"rm --force {new_marker} {registry_relpath}", login_shell=False, timeout=15.0
            )
            await pc1_executor.run_command(f"rm --force {registry_relpath}", login_shell=False, timeout=15.0)

    async def test_a_real_hand_downloaded_deb_is_presented_as_needing_a_snippet(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """G27 — A `.deb` installed by hand on a real machine reaches the review as an item no
        package manager can install on pc2.

        What only a real apt can establish: a package installed straight from a `.deb` has
        its INSTALLED version as its own candidate and no repository origin at all, so the
        detection rests on what apt genuinely prints for such a package rather than on
        policy output a test author composed. It is not marked manually installed by anything
        here either — `dpkg --install` is the whole setup — and the scan reads the INSTALLED
        set, so `apt-mark showmanual` is not the boundary being relied on.

        The witness is state, not log text: SKIP_ALWAYS is recorded against an item only if
        the review presented it (`_finalize_unreproducible`), and it is recorded on pc1
        because an unreproducible item is always source-held (D-08a). pc1 holds no decision
        file before the run (`reset_pcswitcher_state`), so the entry's presence afterwards
        can only come from this item having been offered.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)
        _ = pc2_executor

        name = ""
        try:
            name = await _install_a_hand_downloaded_deb(pc1_executor)
            item_id = _no_candidate_item_id(name)

            # The precondition, asserted rather than assumed: apt must name no repository for
            # the installed version, or the item this run is about was never detectable.
            policy = await pc1_executor.run_command(
                f"LC_ALL=C apt-cache policy {shlex.quote(name)}", login_shell=False, timeout=30.0
            )
            assert policy.success and "1.0" in policy.stdout, (
                f"apt says nothing about the hand-installed {name}.\nstdout: {policy.stdout}\nstderr: {policy.stderr}"
            )
            assert "http" not in policy.stdout, (
                f"apt names a repository origin for the hand-installed {name}, so it is reproducible after all and "
                f"this run cannot exercise the branch.\n{policy.stdout}"
            )

            await _write_package_sync_config(pc1_executor, manual_installs_sync=True)

            decisions = {item_id: Decision.SKIP_ALWAYS}
            sync_cmd = f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            entries = await DecisionFile("manual", pc1_executor).load()
            assert item_id in entries, (
                f"{name} was never presented as an item needing an install snippet: no decision was recorded for "
                f"{item_id} on pc1 although the review was answered SKIP_ALWAYS for it.\n"
                f"recorded: {sorted(entries)}"
            )
        finally:
            if name:
                await pc1_executor.run_command(
                    f"sudo DEBIAN_FRONTEND=noninteractive dpkg --purge {shlex.quote(name)}",
                    login_shell=False,
                    timeout=120.0,
                )

    async def test_the_scan_of_a_real_machine_names_few_findings_and_never_its_own_roots(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """G28 — What the unreproducible scan actually names on a stock Ubuntu 24.04 machine:
        few enough findings to review by hand, and never one of the four directories the scan
        walks.

        A characterisation test, so it asserts the PROPERTY rather than a golden list: the
        set of things under `/usr/local` and `/opt` on a real machine is not this project's
        to fix, and a list of them would fail on every unrelated package that ships one.

        The roots matter because two of them (`/usr/local/bin`, `/usr/local/lib`) are also
        entries of another root (`/usr/local`), so they are queried twice and reach the
        ownership check like any other candidate. That they are not reported rests on dpkg
        owning them on a real machine — exactly the kind of fact no mocked `dpkg --search`
        can settle.

        Run non-interactively on purpose, and with only this job enabled: with nobody to ask,
        the run NAMES every item it could not ask about (`PKG-FR-LOG-DECISIONS`,
        `_warn_every_item_unasked`), which is the one place the whole finding set is written
        down. Every such line here is therefore this job's.

        One unowned marker of the test's own is created under `/opt` and asserted to be
        among the findings: without it a scan that found nothing at all would pass this
        vacuously.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)
        _ = pc2_executor

        witness_path = f"/opt/pcswitcher-it-g28-{uuid4().hex[:12]}"
        try:
            await _create_unowned_marker(pc1_executor, witness_path)

            await _write_package_sync_config(pc1_executor, manual_installs_sync=True)

            # No automation env and no pty: the non-interactive path names every item.
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync", timeout=300.0, login_shell=True
            )
            assert sync_result.success, (
                f"a run with nobody to ask must not fail.\nstdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            combined_output = sync_result.stdout + sync_result.stderr
            # A trailing space so the last finding on the line has the same right-hand
            # boundary as every other one (see the root check below).
            collapsed = f"{_collapse_run_output(combined_output)} "
            findings = collapsed.count(_UNASKED_ITEM_MARKER)
            assert f"{_UNASKED_ITEM_MARKER}{witness_path} " in collapsed, (
                f"the scan did not name {witness_path}, so this run says nothing about what it names.\n"
                f"{combined_output}"
            )
            assert findings <= _HAND_REVIEWABLE_FINDING_LIMIT, (
                f"the scan named {findings} items on a stock machine, more than the "
                f"{_HAND_REVIEWABLE_FINDING_LIMIT} a user can reasonably answer one at a time.\n{combined_output}"
            )
            for root in _UNOWNED_SCAN_ROOTS:
                # The trailing space is the boundary: `/usr/local/bin` must not satisfy the
                # check for `/usr/local`.
                assert f"{_UNASKED_ITEM_MARKER}{root} " not in collapsed, (
                    f"the scan reported its own root {root} as a finding, so dpkg does not own it on this machine "
                    f"and every user would be asked to write an install snippet for a directory the distribution "
                    f"ships.\n{combined_output}"
                )
        finally:
            await _remove_unowned_marker(pc1_executor, witness_path)


# `snap:hold:<name>` has no `SnapHoldItem` dataclass to build the id from -- `snap_sync`
# constructs the `ItemDiff` inline (02-208-HOLD-MASK-REPLICATION.md's own deviation note),
# so the literal shape is restated here exactly as `_diff_snap_holds` emits it.
def _snap_hold_item_id(name: str) -> str:
    return f"snap:hold:{name}"


class TestPackageSyncIdempotency:
    """The property a convergence tool exists to have (J10/N2): converge once, and the
    NEXT identical run has nothing left to do.

    Every other test in this module proves a single run does the right thing. None of
    them proves the run is a fixed point, which is what makes the tool usable at all --
    a sync that re-proposes what it just applied is indistinguishable, from the user's
    seat, from one that never applied it.
    """

    async def test_second_consecutive_sync_has_nothing_to_do(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """J2, N8 — Run 1 converges a real apt divergence; run 2, with all three package-manager
        jobs enabled, changes NO package-manager state on the target and no longer
        presents the converged item at all.

        All three managers run, none conditionally: apt and snap ship with Ubuntu 24.04,
        and flatpak is installed on both VMs by the fixture script. `flatpak_sync` fails
        validation where flatpak is absent, and a validation error aborts the whole sync
        before any job executes, so its absence is checked up front rather than quietly
        narrowing what this test covers.

        Two independent witnesses, both read off pc2's own package managers rather than
        pc-switcher's output:

        1. `_MachinePackageState` (apt manual set, hold set, full dpkg-installed set and
           `/etc/apt` digests; `snap list` revisions; `flatpak list` refs and the remote
           table) is captured immediately before and after run 2 and must be identical --
           no install, no removal, no re-mark, and none of the derived `/etc/apt` or remote
           writes a run makes without a review line of its own.
        2. Run 2 maps the converged item to SKIP_ALWAYS. A SKIP_ALWAYS on a presented
           item writes a `DecisionEntry` on the machine that holds it (D-08a), so the
           entry's ABSENCE from both machines' decision files afterwards is state-based
           proof that the review never presented the item -- it is no longer a diff.

        Scope, stated honestly: witness 2 is scoped to the item this test converged.
        Items that were already diverged between the two VMs and were left SKIP_ONCE by
        run 1 are legitimately presented again in run 2; that is not what idempotency
        promises. Witness 1 is unscoped and covers all three managers.

        snapd auto-refresh is paused on both hosts for the whole test (the same timed
        `refresh.hold` a sync engages, restored exactly afterwards) so a background
        refresh cannot change `snap list` between the two captures and be misread as the
        run having done something.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = await _removable_candidate(pc1_executor, pc2_executor)
        item_id = AptPackageItem(name=candidate, version="").item_id

        pc1_prior_hold = await _capture_system_refresh_hold(pc1_executor)
        pc2_prior_hold = await _capture_system_refresh_hold(pc2_executor)

        try:
            await _engage_system_refresh_hold(pc1_executor)
            await _engage_system_refresh_hold(pc2_executor)

            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_result.success, f"Failed to remove {candidate} from pc2: {remove_result.stderr}"

            await _assert_flatpak_available(pc1_executor)
            await _assert_flatpak_available(pc2_executor)
            await _write_package_sync_config(pc1_executor, apt_sync=True, snap_sync=True, flatpak_sync=True)

            first_cmd = f"{_automation_env_assignment(item_id)} pc-switcher sync pc2 --yes --allow-first-sync"
            first_result = await pc1_executor.run_command(first_cmd, timeout=300.0, login_shell=True)
            assert first_result.success, (
                f"converging sync exited {first_result.exit_code}.\n"
                f"stdout: {first_result.stdout}\nstderr: {first_result.stderr}"
            )
            converged = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate in nonblank_lines(converged.stdout), (
                f"{candidate} not reinstalled on pc2 -- run 1 did not converge, so run 2 proves nothing"
            )

            before = await _capture_machine_package_state(pc2_executor)

            # SKIP_ALWAYS, not APPLY: an APPLY on an item that is genuinely no longer a
            # diff and an APPLY on an item that was never presented are indistinguishable
            # from the end state, whereas a SKIP_ALWAYS leaves a decision-file trace iff
            # the item WAS presented.
            second_decisions = {item_id: Decision.SKIP_ALWAYS}
            second_cmd = (
                f"{_automation_env_assignment_multi(second_decisions)} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order"
            )
            second_result = await pc1_executor.run_command(second_cmd, timeout=300.0, login_shell=True)
            assert second_result.success, (
                f"second sync exited {second_result.exit_code}.\n"
                f"stdout: {second_result.stdout}\nstderr: {second_result.stderr}"
            )

            after = await _capture_machine_package_state(pc2_executor)
            assert after == before, (
                "the second consecutive sync changed pc2's package-manager state -- the run is not a fixed point.\n"
                f"before: {before}\nafter: {after}"
            )

            source_entries = await DecisionFile("apt", pc1_executor).load()
            target_entries = await DecisionFile("apt", pc2_executor).load()
            assert item_id not in source_entries and item_id not in target_entries, (
                f"{candidate} was still presented in the second run's review (its SKIP_ALWAYS was recorded) -- "
                "a converged item must produce no diff at all"
            )
        finally:
            await _restore_package(pc2_executor, candidate)
            await _restore_system_refresh_hold(pc1_executor, pc1_prior_hold)
            await _restore_system_refresh_hold(pc2_executor, pc2_prior_hold)


class TestSnapHoldCaptureTiming:
    """The VM check #208 D9 promised and never got (L10).

    `SnapSyncJob` reads per-snap holds out of `snap list`'s Notes column DURING the sync,
    i.e. inside the window in which the orchestrator has a system-wide `refresh.hold`
    engaged on both hosts. D9 assumes those are separate snapstate -- that a system-wide
    hold neither sets nor clears an individual snap's `held` note -- and says so in a
    comment in `snap_sync._parse_snap_list`. Nothing had ever checked it against a real
    snapd.
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

    async def test_per_snap_hold_replicates_through_a_real_sync_window(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E55 — The same assumption, end to end: a hold set on the source only reaches the
        target through a real sync -- whose orchestrator engages the system-wide
        `refresh.hold` on both hosts around the job window (L6) before snap_sync reads
        `snap list`.

        Proven against pc2's own `snap list` Notes. If the system hold masked per-snap
        holds, the source-side capture would see no hold, emit no `snap:hold:` diff, and
        pc2 would end the run unheld.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        name = await _snap_subject(pc1_executor, pc2_executor)

        try:
            hold_result = await pc1_executor.run_command(
                f"sudo snap refresh --hold=forever {shlex.quote(name)}", login_shell=False, timeout=60.0
            )
            assert hold_result.success, f"Failed to set a per-snap hold on pc1's {name}: {hold_result.stderr}"
            assert "held" not in await _snap_notes(pc2_executor, name), (
                f"{name} is already held on pc2 before the run; the replication below would prove nothing"
            )

            await _write_package_sync_config(pc1_executor, snap_sync=True)

            sync_cmd = (
                f"{_automation_env_assignment(_snap_hold_item_id(name))} pc-switcher sync pc2 --yes --allow-first-sync"
            )
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            target_notes = await _snap_notes(pc2_executor, name)
            assert "held" in target_notes, (
                f"pc1's per-snap hold on {name} did not reach pc2 (pc2 notes: {sorted(target_notes)}). "
                "If the source capture ran inside the orchestrator's system-wide refresh.hold window and saw no "
                "hold, #208 D9's capture-timing assumption is false and the capture must move earlier."
            )
        finally:
            for executor in (pc1_executor, pc2_executor):
                await executor.run_command(
                    f"sudo snap refresh --unhold {shlex.quote(name)}", login_shell=False, timeout=60.0
                )


class TestBlockStateDecisionRoundTrip:
    """A skip-always recorded against a hold in run 1 must silence that hold in run 2
    (N3), for apt and for snap.

    A block-state item's identity (`apt:hold:<pkg>`, `snap:hold:<name>`) exists on no
    captured item -- it is derived from hold-set membership -- so the input-side
    `filter_inert` cannot see it and the decision has to be honoured on the finished
    diff. Getting this wrong is worse than merely noisy: the add direction is
    default-ticked, so a re-emitted hold rides in on the next bulk accept and applies the
    very block the user permanently declined.
    """

    async def test_skip_always_on_an_apt_hold_is_inert_next_run(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """B42, B47, N4 — Run 1 records SKIP_ALWAYS for a source-only `apt-mark hold`; run 2 force-maps
        the same item to APPLY and the hold still never lands on pc2.

        Same proof shape as `test_skip_always_is_inert_in_both_roles`: if the item is
        genuinely filtered out it never becomes a diff, so the APPLY has nothing to
        attach to -- witnessed by pc2's own `apt-mark showhold`, not by log text.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = await _common_apt_package(pc1_executor, pc2_executor)
        item_id = AptHoldItem(name=candidate).item_id

        try:
            hold_result = await pc1_executor.run_command(
                f"sudo apt-mark hold {shlex.quote(candidate)}", login_shell=False, timeout=30.0
            )
            assert hold_result.success, f"Failed to hold {candidate} on pc1: {hold_result.stderr}"
            assert candidate not in await _apt_holds(pc2_executor), (
                f"{candidate} is already held on pc2 before the run; the assertions below would prove nothing"
            )

            await _write_apt_sync_config(pc1_executor)

            first_cmd = (
                f"{_automation_env_assignment_multi({item_id: Decision.SKIP_ALWAYS})} "
                "pc-switcher sync pc2 --yes --allow-first-sync"
            )
            first_result = await pc1_executor.run_command(first_cmd, timeout=300.0, login_shell=True)
            assert first_result.success, (
                f"skip-always run unexpectedly failed.\nstdout: {first_result.stdout}\nstderr: {first_result.stderr}"
            )

            # The hold is an INSTALL-direction item, so the decision lands on the SOURCE.
            entries = await DecisionFile("apt", pc1_executor).load()
            assert item_id in entries, (
                f"{item_id} not recorded in pc1's apt decision file after a skip-always decision (D-08a)"
            )
            assert candidate not in await _apt_holds(pc2_executor), "skip-always must not itself apply the hold"

            second_cmd = (
                f"{_automation_env_assignment_multi({item_id: Decision.APPLY})} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order"
            )
            second_result = await pc1_executor.run_command(second_cmd, timeout=300.0, login_shell=True)
            assert second_result.success, (
                f"second sync unexpectedly failed.\nstdout: {second_result.stdout}\nstderr: {second_result.stderr}"
            )

            assert candidate not in await _apt_holds(pc2_executor), (
                f"{candidate} was held on pc2 despite a recorded skip-always for {item_id} -- the hold item was "
                "re-emitted as a diff in the next run instead of being dropped (#208 D3's skip-always promise)"
            )
        finally:
            for executor in (pc1_executor, pc2_executor):
                await executor.run_command(
                    f"sudo apt-mark unhold {shlex.quote(candidate)}", login_shell=False, timeout=30.0
                )

    async def test_skip_always_on_a_snap_hold_is_inert_next_run(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E68, H122, N5 — The snap half of the same requirement, whose identity (`snap:hold:<name>`) is
        a strict superstring of the snap item's own (`snap:<name>`) -- so a filter that
        matched on the plain prefix would silence the wrong item, and one that matched
        only captured items would silence neither.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        name = await _snap_subject(pc1_executor, pc2_executor)
        item_id = _snap_hold_item_id(name)

        try:
            hold_result = await pc1_executor.run_command(
                f"sudo snap refresh --hold=forever {shlex.quote(name)}", login_shell=False, timeout=60.0
            )
            assert hold_result.success, f"Failed to set a per-snap hold on pc1's {name}: {hold_result.stderr}"
            assert "held" not in await _snap_notes(pc2_executor, name), (
                f"{name} is already held on pc2 before the run; the assertions below would prove nothing"
            )

            await _write_package_sync_config(pc1_executor, snap_sync=True)

            first_cmd = (
                f"{_automation_env_assignment_multi({item_id: Decision.SKIP_ALWAYS})} "
                "pc-switcher sync pc2 --yes --allow-first-sync"
            )
            first_result = await pc1_executor.run_command(first_cmd, timeout=300.0, login_shell=True)
            assert first_result.success, (
                f"skip-always run unexpectedly failed.\nstdout: {first_result.stdout}\nstderr: {first_result.stderr}"
            )

            entries = await DecisionFile("snap", pc1_executor).load()
            assert item_id in entries, (
                f"{item_id} not recorded in pc1's snap decision file after a skip-always decision (D-08a)"
            )
            assert "held" not in await _snap_notes(pc2_executor, name), "skip-always must not itself apply the hold"

            second_cmd = (
                f"{_automation_env_assignment_multi({item_id: Decision.APPLY})} "
                "pc-switcher sync pc2 --yes --allow-first-sync --allow-out-of-order"
            )
            second_result = await pc1_executor.run_command(second_cmd, timeout=300.0, login_shell=True)
            assert second_result.success, (
                f"second sync unexpectedly failed.\nstdout: {second_result.stdout}\nstderr: {second_result.stderr}"
            )

            target_notes = await _snap_notes(pc2_executor, name)
            assert "held" not in target_notes, (
                f"{name} was held on pc2 despite a recorded skip-always for {item_id} (pc2 notes: "
                f"{sorted(target_notes)}) -- the hold item was re-emitted as a diff in the next run"
            )
        finally:
            for executor in (pc1_executor, pc2_executor):
                await executor.run_command(
                    f"sudo snap refresh --unhold {shlex.quote(name)}", login_shell=False, timeout=60.0
                )


class TestCrossDirectionRoundTrips:
    """Narratives that only exist across MORE than one run and, for N4, more than one
    direction -- the shape a real two-machine workflow actually has, and the one thing a
    single-run test can never observe.
    """

    async def test_install_propagates_then_reversed_removal_needs_approval(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """A54, H31, H114, N7, N9 — N4, whole: pc1 -> pc2 installs a package; the user then removes it on pc2;
        pc2 -> pc1 offers that removal as an item that does NOT take effect on its own
        and DOES when approved.

        The middle run is the point. A removal-direction item lands in its own unticked
        group (D-07/I3), so leaving it undecided must leave pc1's package installed --
        proven by deciding it SKIP_ONCE explicitly and reading pc1's own
        `apt-mark showmanual`. Only the third run, which approves the same item, may
        remove it.

        Candidate safety is vetted against pc2's reverse dependencies
        (`pick_safe_removal_candidates`); the final run removes the package from pc1
        instead. The two VMs are provisioned from one baseline, so the reverse-dependency
        picture is the same on both -- if it ever diverges, apt_sync's own collateral
        guard refuses the item and this test fails loudly rather than damaging pc1.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidate = await _removable_candidate(pc1_executor, pc2_executor)
        item_id = AptPackageItem(name=candidate, version="").item_id

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert remove_result.success, f"Failed to remove {candidate} from pc2: {remove_result.stderr}"

            # Run 1: pc1 -> pc2, the install direction.
            await _write_apt_sync_config(pc1_executor)
            forward_cmd = f"{_automation_env_assignment(item_id)} pc-switcher sync pc2 --yes --allow-first-sync"
            forward_result = await pc1_executor.run_command(forward_cmd, timeout=300.0, login_shell=True)
            assert forward_result.success, (
                f"forward sync exited {forward_result.exit_code}.\n"
                f"stdout: {forward_result.stdout}\nstderr: {forward_result.stderr}"
            )
            after_forward = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate in nonblank_lines(after_forward.stdout), (
                f"{candidate} did not propagate to pc2; the reversed direction below would have nothing to remove"
            )

            # The user removes it again on pc2, which is about to become the SOURCE.
            second_removal = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert second_removal.success, f"Failed to remove {candidate} from pc2 again: {second_removal.stderr}"

            await _write_apt_sync_config(pc2_executor)

            # Run 2: pc2 -> pc1, removal direction, explicitly left undecided.
            undecided_cmd = (
                f"{_automation_env_assignment_multi({item_id: Decision.SKIP_ONCE})} "
                "pc-switcher sync pc1 --yes --allow-first-sync"
            )
            undecided_result = await pc2_executor.run_command(undecided_cmd, timeout=300.0, login_shell=True)
            assert undecided_result.success, (
                f"reversed sync (undecided) exited {undecided_result.exit_code}.\n"
                f"stdout: {undecided_result.stdout}\nstderr: {undecided_result.stderr}"
            )
            pc1_after_undecided = await pc1_executor.run_command(
                "apt-mark showmanual", login_shell=False, timeout=15.0
            )
            assert candidate in nonblank_lines(pc1_after_undecided.stdout), (
                f"{candidate} was removed from pc1 without being approved -- a removal-direction item must take "
                "effect only when the user ticks it"
            )

            # Run 3: same direction, same item, approved this time.
            approved_cmd = (
                f"{_automation_env_assignment(item_id)} "
                "pc-switcher sync pc1 --yes --allow-first-sync --allow-out-of-order"
            )
            approved_result = await pc2_executor.run_command(approved_cmd, timeout=300.0, login_shell=True)
            assert approved_result.success, (
                f"reversed sync (approved) exited {approved_result.exit_code}.\n"
                f"stdout: {approved_result.stdout}\nstderr: {approved_result.stderr}"
            )
            pc1_after_approved = await pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert candidate not in nonblank_lines(pc1_after_approved.stdout), (
                f"{candidate} still manually installed on pc1 after the removal was approved -- the removal did not "
                "propagate back across the reversed direction"
            )
        finally:
            await _restore_package(pc1_executor, candidate)
            await _restore_package(pc2_executor, candidate)

    async def test_apt_source_and_its_key_removed_together(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """C24, C63, C104: a vendor repository file that exists only on the TARGET is removed, and
        its signing key goes with it although the user decided only about the repository —
        leaving pc2 with an `apt-get update` that works and no longer reaches for the
        removed repository at all.

        The key is collected because, once the repository file is gone, nothing on pc2
        references it any more. That count is taken after the deletion actually happened,
        which is why this run is the VM-level witness for it.

        The witness is apt's own account of which repositories it tried, not its exit
        code: `apt-get update` exits 0 when an index fails to fetch (it downgrades the
        failure to a `W:` warning and reuses the cached list), so the exit code says the
        same thing before and after. What differs is the output. While the pair exists
        apt prints an `Err:` line naming the unresolvable synthetic host -- asserted
        before the run, which is what makes that host's total absence from the run
        afterwards a real witness rather than a tautology, and non-vacuous in both
        directions: had the removal not happened, the `Err:` line would still be there.

        The post-removal exit code is asserted too, for the failure the output check
        cannot see: an `/etc/apt` left syntactically unreadable, which apt does treat as
        a hard error.

        Unlike this module's other repo test this one is NOT a dry run: the removals are
        real, which is the only way `/etc/apt` and a live `apt-get update` can be the
        witnesses.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        source_filename = ""
        key_filename = ""
        try:
            source_filename, key_filename = await _create_synthetic_repo_and_key(pc2_executor)
            source_dest = f"{_APT_SOURCES_DIR}/{source_filename}"
            key_dest = f"{_APT_KEYRINGS_DIR}/{key_filename}"

            broken_update = await _apt_get_update(pc2_executor)
            reached_for_repo = apt_update_lines_naming(broken_update, _SYNTHETIC_REPO_HOST)
            assert any(line.startswith("Err:") for line in reached_for_repo), (
                f"pc2's `apt-get update` reported no `Err:` line naming {_SYNTHETIC_REPO_HOST} while the unreachable "
                "synthetic repo was configured, so apt is not actually reaching for that repo and its absence from "
                "the post-removal run below would prove nothing.\n"
                f"lines naming the host: {reached_for_repo}\n"
                f"stdout: {broken_update.stdout}\nstderr: {broken_update.stderr}"
            )

            await _write_apt_sync_config(pc1_executor)

            # The REPOSITORY only: the key has no reviewable identity to decide about.
            decisions = {f"apt:source:{source_filename}": Decision.APPLY}
            sync_cmd = f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            gone = await pc2_executor.run_command(
                f"test ! -e {shlex.quote(source_dest)} && test ! -e {shlex.quote(key_dest)}",
                login_shell=False,
                timeout=10.0,
            )
            assert gone.success, (
                f"{source_filename} and/or {key_filename} still present under /etc/apt on pc2 after the repository "
                f"removal was approved -- the key it left unreferenced was not collected.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
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
        finally:
            cleanup_paths = " ".join(
                shlex.quote(f"{directory}/{filename}")
                for directory, filename in (
                    (_APT_SOURCES_DIR, source_filename),
                    (_APT_KEYRINGS_DIR, key_filename),
                )
                if filename
            )
            if cleanup_paths:
                await pc2_executor.run_command(f"sudo rm --force {cleanup_paths}", login_shell=False, timeout=15.0)
                await pc1_executor.run_command(f"sudo rm --force {cleanup_paths}", login_shell=False, timeout=15.0)


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


class TestSnapRemovalKeepsSnapdsSnapshot:
    """`PKG-FR-SNAP-REMOVE-SNAPSHOT` on real machines: a removal made through a sync leaves
    snapd's own pre-removal snapshot behind, so the data the removed snap held is still
    recoverable.

    The unit test proves the command shape — the removal never passes `--purge`. What it
    cannot show is the consequence the article is actually about, which lives entirely in
    snapd: that a removal without `--purge` really does take a snapshot, and that it is
    still there once the run has finished.
    """

    async def test_a_removal_through_a_sync_leaves_a_snapshot_snap_saved_lists(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E36, E37 — A snap the source no longer has is removed from pc2 by an approved item,
        and `snap saved` on pc2 then lists a snapshot for it.

        The subject is made target-only by removing it from pc1 with `--purge`, so pc1 keeps
        no snapshot of its own and the one found on pc2 afterwards can only be this run's.
        The snap is given system data first: a snapshot of a snap that never held any is not
        the case the article is about, and asserting on one would leave the test passing for
        a reason nobody intended.

        `_FIXTURE_SNAPS[1]` rather than `[0]`: the first fixture snap is the one carrying
        distinct revisions across channels, which `_alternate_snap_revision` needs intact.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        name = (await _snap_subjects(pc1_executor, pc2_executor, count=2))[1]
        quoted = shlex.quote(name)
        source_revision = await _snap_revision(pc1_executor, name)
        target_revision = await _snap_revision(pc2_executor, name)
        assert source_revision and target_revision, f"{name} is not installed on both machines"
        sets_before = {set_id for set_id, _snap in await _snap_saved_rows(pc2_executor)}

        uniq = uuid4().hex[:12]
        data_file = f"/var/snap/{name}/common/pcswitcher-it-{uniq}"
        try:
            seeded = await pc2_executor.run_command(
                f"sudo mkdir --parents {shlex.quote(f'/var/snap/{name}/common')} && "
                f"printf %s pcswitcher-it-{uniq} | sudo tee {shlex.quote(data_file)} > /dev/null",
                login_shell=False,
                timeout=30.0,
            )
            assert seeded.success, f"could not give {name} data on pc2 to snapshot: {seeded.stderr}"

            removed = await pc1_executor.run_command(
                f"sudo snap remove --purge {quoted}", login_shell=False, timeout=180.0
            )
            assert removed.success, f"Failed to remove {name} from pc1: {removed.stderr}"

            await _write_package_sync_config(pc1_executor, snap_sync=True)

            sync_cmd = f"{_automation_env_assignment(f'snap:{name}')} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            still_there = await pc2_executor.run_command(f"snap list {quoted}", login_shell=False, timeout=15.0)
            assert not still_there.success, (
                f"{name} is still installed on pc2, so no removal happened and the snapshot check below would say "
                f"nothing.\n{still_there.stdout}"
            )

            saved = await _snap_saved_rows(pc2_executor)
            assert any(snap == name for _set_id, snap in saved), (
                f"snapd kept no snapshot for {name} after the sync removed it from pc2 — the removal took the "
                f"machine's data with it.\nsnap saved: {saved}"
            )
        finally:
            for set_id, snap in await _snap_saved_rows(pc2_executor):
                if snap == name and set_id not in sets_before:
                    await pc2_executor.run_command(
                        f"sudo snap forget {shlex.quote(set_id)}", login_shell=False, timeout=60.0
                    )
            await _restore_snap(pc1_executor, name, source_revision)
            await _restore_snap(pc2_executor, name, target_revision)
            await pc2_executor.run_command(
                f"sudo rm --force {shlex.quote(data_file)}", login_shell=False, timeout=15.0
            )


class TestSideloadedSnapsThroughARealRun:
    """`PKG-FR-SNAP-SIDELOAD` end to end: a snap installed from local bytes is out of scope
    entirely, so a whole run leaves it exactly where it was.

    The unit coverage of the branch set is dense, and every one of those tests asserts about
    a `snap list` listing this project composed. This asserts about one snapd produced for a
    snap that genuinely has no store revision.
    """

    async def test_a_sideloaded_snap_survives_a_whole_run_untouched(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E49 — pc1 carries a sideloaded snap when the run starts; the run finishes and both
        machines' `snap list` are exactly what they were.

        Both machines' whole listings are compared, not just the sideload's row: "the run
        does nothing about it" includes not installing it on pc2, not removing it from pc1,
        and not moving anything else while it is there.

        snapd's automatic refresh is paused on both machines for the whole test (the same
        timed `refresh.hold` a sync engages, restored exactly afterwards), so a background
        refresh cannot change a revision between the two listings and be read as the run's
        doing.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        uniq = uuid4().hex[:12]
        name = f"pcswitcher-it-sideload-{uniq}"
        directory = f"/var/tmp/pcswitcher-it-sideload-{uniq}"
        pc1_prior_hold = await _capture_system_refresh_hold(pc1_executor)
        pc2_prior_hold = await _capture_system_refresh_hold(pc2_executor)

        try:
            await _engage_system_refresh_hold(pc1_executor)
            await _engage_system_refresh_hold(pc2_executor)

            base = await _installed_base_snap(pc1_executor)
            await _create_sideloaded_snap(pc1_executor, directory, name, base)

            pc1_before = parse_snap_list_names_revisions(
                (await pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0)).stdout
            )
            pc2_before = parse_snap_list_names_revisions(
                (await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)).stdout
            )
            assert pc1_before.get(name, "").startswith("x"), (
                f"pc1's {name} is at revision {pc1_before.get(name)!r}, not a sideloaded `x`-prefixed one, so this "
                "run cannot exercise the sideload branch"
            )
            assert name not in pc2_before, f"{name} is somehow already on pc2"

            await _write_package_sync_config(pc1_executor, snap_sync=True)

            # An empty automation map: the review is answered, and nothing is approved. A
            # sideload must produce no item at all, so there is nothing here to answer.
            sync_cmd = f"{_automation_env_assignment_multi({})} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=300.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code} with a sideloaded snap on the source.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            pc1_after = parse_snap_list_names_revisions(
                (await pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0)).stdout
            )
            pc2_after = parse_snap_list_names_revisions(
                (await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)).stdout
            )
            assert pc1_after == pc1_before, (
                f"the run changed pc1's own snaps.\nbefore: {pc1_before}\nafter: {pc1_after}"
            )
            assert pc2_after == pc2_before, (
                f"the run changed pc2's snaps although the only divergence was a sideload.\n"
                f"before: {pc2_before}\nafter: {pc2_after}"
            )
        finally:
            await _remove_sideloaded_snap(pc1_executor, directory, name)
            await _restore_system_refresh_hold(pc1_executor, pc1_prior_hold)
            await _restore_system_refresh_hold(pc2_executor, pc2_prior_hold)


class TestSnapPerItemFailureOnVMs:
    """`PKG-FR-SNAP-FAIL-ITEM` on real machines: one snap item failing costs that item and
    nothing else.

    The failure is real snapd's, not a mock's: pc2 is put offline as far as the store is
    concerned (`snap set system store.access=offline`), which is precisely the split the
    claim needs — an install has to reach the store and a removal does not.
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
        with `--purge` (no snapshot to clean up afterwards), and both are put back at their
        original revisions in the `finally`.
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
            await _restore_snap(pc1_executor, removal_subject, source_removal_revision)
            await _restore_snap(pc2_executor, removal_subject, removal_revision)
            await _restore_snap(pc2_executor, install_subject, target_install_revision)


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


class TestTheSyncWindowHoldIsTimed:
    """`PKG-FR-SNAP-REFRESH-PAUSE`'s self-healing half: the suspension a run writes is a
    timed value on each machine's own clock, so a run that dies without cleaning up leaves
    a hold that lapses rather than one that never does.

    Only a real run can show it. The value is written by the orchestrator and put back by
    its own cleanup, so the only moment it exists is inside the sync window — and the only
    way it survives to be read is a run that never reaches its cleanup.
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


class TestTheSnapDataBoundaryOnVMs:
    """`PKG-FR-SNAP-DATA-BOUNDARY` with both jobs on: what `~/snap` actually looks like on
    the target after a real transfer.

    The unit tests assert which absolute paths `snap_sync` hands `folder_sync` and which
    rsync filters that produces. Neither runs rsync, so neither shows the one thing the
    article is about: that the directories those rules name really do stay home while the
    rest of the tree travels.
    """

    async def test_only_the_revision_the_target_holds_arrives_under_snap(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """E113, E115 — With `snap_sync` and `folder_sync` both enabled, pc2 ends the run holding
        the data directory of the revision its OWN snapd is on, and none of the others.

        Two apps, because the boundary has two sides. One is a snap pc2 genuinely holds:
        its active revision's directory travels, its retained older one does not. The other
        is a name pc2's snapd has never heard of, standing for a snap whose install was
        declined, failed, or never offered: not one of its revision directories may arrive.
        Both keep their revision-independent `common` directory, which is what separates
        "the boundary held" from "nothing was transferred at all".

        The revision the first app's `current` points at is read off PC2 — the exclusion set
        is computed from the target's own `snap list --all`, so a directory travels because
        the target holds that revision, not because the source does.

        `~/snap` alone is the synced folder, and both machines' real one is set aside for the
        duration: the mirror is a `--delete` one, so a hermetic tree is the only way the
        transfer's outcome is exactly what this test built.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        home = await _home_dir(pc1_executor)
        assert await _home_dir(pc2_executor) == home, (
            "the two machines' SSH users have different home directories, so `~/snap` is not one path to mirror"
        )
        snap_root = f"{home}/snap"

        held_app = await _snap_subject(pc1_executor, pc2_executor)
        held_revision = await _snap_revision(pc2_executor, held_app)
        assert held_revision, f"{held_app} is not installed on pc2, so pc2 holds no revision of it"
        stale_revision = str(int(held_revision) + 1000) if held_revision.isdigit() else f"{held_revision}0"

        uniq = uuid4().hex[:12]
        absent_app = f"pcswitcher-it-nosnap-{uniq}"
        absent_revision = "1"
        markers = {
            "held-active": f"{snap_root}/{held_app}/{held_revision}/pcswitcher-it-{uniq}",
            "held-stale": f"{snap_root}/{held_app}/{stale_revision}/pcswitcher-it-{uniq}",
            "held-common": f"{snap_root}/{held_app}/common/pcswitcher-it-{uniq}",
            "absent-revision": f"{snap_root}/{absent_app}/{absent_revision}/pcswitcher-it-{uniq}",
            "absent-common": f"{snap_root}/{absent_app}/common/pcswitcher-it-{uniq}",
        }

        source_aside = ""
        target_aside = ""
        try:
            source_aside = await _take_paths_aside(pc1_executor, [snap_root])
            target_aside = await _take_paths_aside(pc2_executor, [snap_root])

            build = "\n".join(
                ["set -eu"]
                + [f"mkdir --parents {shlex.quote(path.rsplit('/', 1)[0])}" for path in markers.values()]
                + [f"printf %s {uniq} > {shlex.quote(path)}" for path in markers.values()]
                + [
                    f"ln --symbolic --no-dereference --force {shlex.quote(revision)} "
                    f"{shlex.quote(f'{snap_root}/{app}/current')}"
                    for app, revision in ((held_app, held_revision), (absent_app, absent_revision))
                ]
            )
            built = await pc1_executor.run_command(build, login_shell=False, timeout=30.0)
            assert built.success, f"could not build the ~/snap fixture on pc1: {built.stderr}"

            await _write_package_sync_config(
                pc1_executor,
                extra_sections=_folder_sync_section(snap_root),
                snap_sync=True,
                folder_sync=True,
            )

            # An empty automation map: both machines are converged on the snaps themselves,
            # and what this asserts is the transfer, not a convergence.
            sync_cmd = f"{_automation_env_assignment_multi({})} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=600.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            listing = await pc2_executor.run_command(
                f"find {shlex.quote(snap_root)} -mindepth 1 | sort", login_shell=False, timeout=30.0
            )
            assert listing.success, f"could not read pc2's {snap_root}: {listing.stderr}"
            arrived = set(nonblank_lines(listing.stdout))

            for key in ("held-active", "held-common", "absent-common"):
                assert markers[key] in arrived, (
                    f"{markers[key]} did not reach pc2, although nothing excludes it.\n{listing.stdout}"
                )
            assert markers["held-stale"] not in arrived, (
                f"{markers['held-stale']} reached pc2, which is on revision {held_revision} of {held_app} and has "
                f"no snapd that ever installed {stale_revision}.\n{listing.stdout}"
            )
            stale_dir = f"{snap_root}/{held_app}/{stale_revision}"
            assert not any(path == stale_dir or path.startswith(f"{stale_dir}/") for path in arrived), (
                f"a data directory for revision {stale_revision} of {held_app} exists on pc2.\n{listing.stdout}"
            )
            absent_dir = f"{snap_root}/{absent_app}/{absent_revision}"
            assert not any(path == absent_dir or path.startswith(f"{absent_dir}/") for path in arrived), (
                f"a data directory arrived on pc2 for {absent_app}, a snap its own snapd has never installed.\n"
                f"{listing.stdout}"
            )
        finally:
            if source_aside:
                await _put_paths_back(pc1_executor, source_aside, [snap_root])
            if target_aside:
                await _put_paths_back(pc2_executor, target_aside, [snap_root])


class TestADeletedFlatpakRemoteTakesItsKey:
    """`PKG-FR-FLATPAK-REMOTE-DELETE`'s last sentence: deleting a remote takes its signing
    key with it.

    Delegated to flatpak — this job issues `flatpak remote-delete` and nothing else — so the
    claim is about what flatpak does, and no mocked executor can say anything about it. What
    is at stake is trust: a keyring left behind is a vendor the machine still trusts for a
    remote it no longer has.
    """

    async def test_deleting_an_unused_target_remote_removes_its_keyring_file(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """F72 — After a run deletes a remote pc1 does not have and nothing on pc2 installs
        from, `<installation>/repo/<remote>.trustedkeys.gpg` is gone from pc2.

        The remote is added on pc2 from Flathub's own `.flatpakrepo`, under a uuid-suffixed
        name pc1 provably lacks: a real signing key, so the keyring file genuinely exists
        beforehand — asserted, which is what makes its absence afterwards a witness rather
        than a tautology.

        The review is answered with an empty automation map. A remote is a review item in no
        direction, so this deletion needs no answer; what the map buys is a run that reaches
        `apply()` at all, where the deletion is derived after the converge loop.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        await _assert_flatpak_available(pc1_executor)
        await _assert_flatpak_available(pc2_executor)

        scope_flag = "--user"
        remote_name = f"pcswitcher-it-vendor-{uuid4().hex[:12]}"
        keyring = f"$HOME/.local/share/flatpak/repo/{remote_name}.trustedkeys.gpg"

        try:
            added = await pc2_executor.run_command(
                f"flatpak remote-add {scope_flag} {shlex.quote(remote_name)} {shlex.quote(_FIXTURE_FLATPAK_REPOFILE)}",
                login_shell=False,
                timeout=180.0,
            )
            assert added.success, f"could not add the target-only remote {remote_name} to pc2: {added.stderr}"

            source_remotes = await pc1_executor.run_command(
                f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
            )
            assert remote_name not in nonblank_lines(source_remotes.stdout), (
                f"{remote_name} is configured on pc1 too, so it is not a remote the source lacks"
            )
            key_before = await pc2_executor.run_command(f"test -f {keyring}", login_shell=False, timeout=15.0)
            assert key_before.success, (
                f"pc2 holds no {keyring} for {remote_name}, so this flatpak does not keep a per-remote keyring and "
                "its absence after the deletion would prove nothing"
            )

            await _write_package_sync_config(pc1_executor, flatpak_sync=True)

            sync_cmd = f"{_automation_env_assignment_multi({})} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=900.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after_remotes = await pc2_executor.run_command(
                f"flatpak remotes {scope_flag} --columns=name", login_shell=False, timeout=15.0
            )
            assert remote_name not in nonblank_lines(after_remotes.stdout), (
                f"{remote_name} is still configured on pc2, so nothing was deleted and the keyring check below "
                f"would say nothing.\nstdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
            key_after = await pc2_executor.run_command(f"test -f {keyring}", login_shell=False, timeout=15.0)
            assert not key_after.success, (
                f"{keyring} survived the deletion of {remote_name}: pc2 still trusts that vendor's signing key for a "
                "remote it no longer has"
            )
        finally:
            await pc2_executor.run_command(
                f"flatpak remote-delete {scope_flag} --force {shlex.quote(remote_name)} || true; rm --force {keyring}",
                login_shell=False,
                timeout=60.0,
            )


class TestASyncLeavesTheSourcesOwnSoftwareAlone:
    """`PKG-FR-SOURCE-INTENT` on real machines: the source states the intent and a sync must
    not change what software it has, nor where it gets it from.

    The unit half is as far as a mock reaches — no command carrying `mutates=` is issued on
    the source's executor while both directions are applied. It says nothing about the
    machine: a source can be left changed by something that never went through that
    executor at all, and only two real machines can show that it was not.
    """

    async def test_a_converging_run_leaves_the_sources_own_package_state_identical(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """J145 — A run that genuinely installs, removes and re-revisions software on pc2 leaves
        every piece of package state pc1 holds byte-identical.

        A converging run, never a no-op: pc2 gains an apt package, loses another, and moves a
        snap to pc1's revision, all in the one run. Those three are asserted on pc2 first —
        without them "pc1 is unchanged" is what an empty run would also produce.

        `_MachinePackageState` is the comparison, so "what software Atlas has" is its apt
        manual, hold and installed sets, its snap revisions and its flatpak refs, and "where
        it gets it from" is the digests of all five `/etc/apt` directories plus its own remote
        table. All three managers are enabled, so all three read pc1 during the run and the
        snap auto-refresh pause is genuinely engaged on it.

        Nothing is answered permanently. A machine-specific mark is one of the exactly three
        writes a sync IS allowed to make on the source, so a SKIP_ALWAYS anywhere here would
        change pc1's decision file and prove the wrong thing; every decision below is APPLY.
        The pause is the second of those three, and it moves pc1's `refresh.hold` — which is
        why what is captured is software, never refresh policy.

        The two apt subjects are vetted by `pick_safe_removal_candidates` before either
        machine is touched, and the setup that makes them divergent happens BEFORE the
        capture, so it is not part of what the run is being held to.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        await _assert_flatpak_available(pc1_executor)
        await _assert_flatpak_available(pc2_executor)

        candidates = await _find_removable_candidates(pc1_executor, pc2_executor, count=2)
        assert len(candidates) == 2, (
            f"{_no_apt_candidate_message()} Needed 2 independent candidates, found {len(candidates)}."
        )
        install_candidate, removal_candidate = candidates

        snap_name = await _snap_subject(pc1_executor, pc2_executor)
        source_snap_revision = await _snap_revision(pc1_executor, snap_name)
        target_snap_revision = await _snap_revision(pc2_executor, snap_name)
        assert source_snap_revision and target_snap_revision, f"{snap_name} is not installed on both machines"
        alternate_revision = await _alternate_snap_revision(pc2_executor, snap_name, source_snap_revision)

        pc1_prior_hold = await _capture_system_refresh_hold(pc1_executor)
        pc2_prior_hold = await _capture_system_refresh_hold(pc2_executor)

        try:
            await _engage_system_refresh_hold(pc1_executor)
            await _engage_system_refresh_hold(pc2_executor)

            # pc2 loses one package (so the run installs it) and pc1 loses another (so the
            # run removes pc2's copy). Both happen before the capture below.
            removed_on_target = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(install_candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert removed_on_target.success, (
                f"Failed to remove {install_candidate} from pc2: {removed_on_target.stderr}"
            )
            removed_on_source = await pc1_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove --assume-yes {shlex.quote(removal_candidate)}",
                login_shell=False,
                timeout=120.0,
            )
            assert removed_on_source.success, (
                f"Failed to remove {removal_candidate} from pc1: {removed_on_source.stderr}"
            )
            diverged = await pc2_executor.run_command(
                f"sudo snap refresh --revision={shlex.quote(alternate_revision)} {shlex.quote(snap_name)}",
                login_shell=False,
                timeout=180.0,
            )
            assert diverged.success, (
                f"Failed to move pc2's {snap_name} to revision {alternate_revision}: {diverged.stderr}"
            )

            await _write_package_sync_config(pc1_executor, apt_sync=True, snap_sync=True, flatpak_sync=True)

            before = await _capture_machine_package_state(pc1_executor)

            decisions = {
                AptPackageItem(name=install_candidate, version="").item_id: Decision.APPLY,
                AptPackageItem(name=removal_candidate, version="").item_id: Decision.APPLY,
                f"snap:{snap_name}": Decision.APPLY,
            }
            sync_cmd = f"{_automation_env_assignment_multi(decisions)} pc-switcher sync pc2 --yes --allow-first-sync"
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=600.0, login_shell=True)
            assert sync_result.success, (
                f"pc-switcher sync exited {sync_result.exit_code}.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            # Non-vacuous first: the run really did install, remove and re-revision on pc2.
            target_manual = nonblank_lines(
                (await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)).stdout
            )
            assert install_candidate in target_manual, (
                f"{install_candidate} was not installed on pc2, so this run converged nothing and pc1 being "
                f"unchanged says nothing.\nstdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
            assert removal_candidate not in target_manual, (
                f"{removal_candidate} was not removed from pc2, so the removal direction converged nothing.\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )
            assert await _snap_revision(pc2_executor, snap_name) == source_snap_revision, (
                f"pc2's {snap_name} did not converge to pc1's revision {source_snap_revision}, so the snap manager "
                f"converged nothing.\nstdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            after = await _capture_machine_package_state(pc1_executor)
            assert after == before, (
                "the run changed pc1's own package state: a sync must not change what software the source has, nor "
                f"where it gets it from.\nbefore: {before}\nafter: {after}"
            )
        finally:
            await _restore_package(pc2_executor, install_candidate)
            await _restore_package(pc1_executor, removal_candidate)
            await _restore_package(pc2_executor, removal_candidate)
            await _restore_snap(pc2_executor, snap_name, target_snap_revision)
            await _restore_system_refresh_hold(pc1_executor, pc1_prior_hold)
            await _restore_system_refresh_hold(pc2_executor, pc2_prior_hold)
