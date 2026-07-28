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
change to assert against. `apt-cache rdepends` output is also read to pick a safe removal
candidate before either machine's package state is touched.

`TestPackageSyncWholeRunContracts` (plan 02-11) extends this same module with the
phase's whole-run contracts -- properties of an entire sync (non-interactive skip-all,
continue-on-item-failure, snap/flatpak convergence, skip-always inertness in both roles,
per-manager review-before-own-mutation) that are invisible to any single item's
mocked-executor unit test, reusing the fixture/teardown/candidate-selection
conventions established below by the tracer.

`TestPackageSyncIdempotency`, `TestSnapHoldCaptureTiming`, `TestBlockStateDecisionRoundTrip`
and `TestCrossDirectionRoundTrips` cover what only more than one run can show
(02-SCENARIO-COVERAGE.md J10/N2, L10, N3, N4, C24): that a converged pair is a fixed
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

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.jobs.apt_sync import AptHoldItem, AptPackageItem
from pcswitcher.jobs.flatpak_sync import FlatpakItem, FlatpakRemoteItem
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


def _package_sync_test_config(**enabled_jobs: bool) -> str:
    """Minimal test config enabling exactly the given `sync_jobs` keys (e.g.
    `apt_sync=True, snap_sync=True`). `Configuration.sync_jobs` is iterated as-is from
    the YAML dict (config.py), with no schema-default injection, so a job name absent
    here is never instantiated -- no explicit `false` entries needed.
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
    )


async def _write_package_sync_config(executor: BashLoginRemoteExecutor, **enabled_jobs: bool) -> None:
    """Write a package-sync test config enabling exactly `enabled_jobs` to `executor`
    (always the machine acting as source for the sync under test).
    """
    config = _package_sync_test_config(**enabled_jobs)
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
        f"{sudo}flatpak remote-add {scope_flag} --if-not-exists "
        f"{shlex.quote(_FIXTURE_FLATPAK_REMOTE)} {shlex.quote(_FIXTURE_FLATPAK_REPOFILE)}",
        login_shell=False,
        timeout=180.0,
    )
    if not result.success:
        print(f"[cleanup] failed to restore the target's flatpak baseline: {result.stderr}")


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


async def _create_synthetic_pin(executor: BashLoginRemoteExecutor) -> str:
    """Create a uuid-suffixed `/etc/apt/preferences.d` file the target lacks, and return its
    filename.

    A pin is in ADR-021 D-36's always-sync bucket: it travels with no review line and no
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


@dataclass(frozen=True)
class _MachinePackageState:
    """Every piece of package-manager state the four jobs can write on one machine, read
    from the package managers themselves (`apt-mark`, `dpkg-query`, `snap list`,
    `flatpak list`) rather than from anything pc-switcher reports about them.

    Compared whole for the idempotency claim: a second run that has nothing to do must
    leave all of it byte-identical, which is a far stronger statement than "the one
    package we diverged is still installed".
    """

    apt_manual: tuple[str, ...]
    apt_held: tuple[str, ...]
    apt_installed: tuple[str, ...]
    snap_revisions: tuple[tuple[str, str], ...]
    flatpak_refs: tuple[tuple[str, str, str, str, str], ...]


async def _capture_machine_package_state(executor: BashLoginRemoteExecutor) -> _MachinePackageState:
    """Read `executor`'s complete apt/snap/flatpak state (see `_MachinePackageState`).

    `snap list --all` is reduced to `{name: revision}` rather than kept as raw text: the
    Version column tracks the revision, so keeping both would only add a second way for
    the same fact to be reported.
    """
    manual = await executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    held = await executor.run_command("apt-mark showhold", login_shell=False, timeout=15.0)
    dpkg = await executor.run_command(
        "dpkg-query --show --showformat='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
    )
    snaps = await executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    flatpaks = await executor.run_command(
        "flatpak list --app --columns=application,version,origin,installation,ref", login_shell=False, timeout=20.0
    )
    return _MachinePackageState(
        apt_manual=tuple(sorted(nonblank_lines(manual.stdout))),
        apt_held=tuple(sorted(nonblank_lines(held.stdout))),
        apt_installed=tuple(sorted(parse_dpkg_installed(dpkg.stdout))),
        snap_revisions=tuple(sorted(parse_snap_list_names_revisions(snaps.stdout).items())),
        flatpak_refs=tuple(sorted(parse_flatpak_list_lines(flatpaks.stdout))),
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
        """A real `pc-switcher sync pc2` reinstalls a package removed from pc2, proven by
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
        """`--dry-run` with the same automation mapping leaves pc2's `apt-mark showmanual`
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
        """ADR-021 D-37/D-38 at VM level, in both directions at once.

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
            # The always-sync pin is previewed as a derived write, with no review entry.
            assert f"Would write {pin_dest}" in combined_output, (
                f"always-sync pin {pin_dest!r} was not previewed as a derived write.\n{combined_output}"
            )
            # The repository feeds no approved package, so nothing about it travels — and it
            # is offered in no direction, which is what makes "derived, never ticked" true.
            assert f"install {source_filename}" not in combined_output, (
                f"repository {source_filename!r} was still offered as a review entry.\n{combined_output}"
            )
            assert key_filename not in combined_output, (
                f"signing key {key_filename!r} travelled for a repository no package needed.\n{combined_output}"
            )
            # The intended metadata refresh (the apt-get update the pin write requires) is
            # reported as its own marker item.
            assert "apt-get update" in combined_output, (
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


class TestPackageSyncWholeRunContracts:
    """VM-level proof of the phase's whole-run contracts (plan 02-11): properties of an
    entire sync -- non-interactive skip-all, continue-on-item-failure, snap/flatpak
    convergence, skip-always inertness in both roles, per-manager review-before-own-
    mutation -- rather than any single item's diff/converge, and therefore invisible to
    plans 02-03/02-05/02-07/02-08's mocked-executor unit tests.
    """

    async def test_non_interactive_skip_all(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """A non-interactive `pc-switcher sync` (no `PACKAGE_REVIEW_AUTOMATION_ENV`, no
        TTY on stdin/stdout -- the default for a command run through this fixture's
        plain SSH exec, which requests no pty) applies nothing, records no permanent
        decision, and reports every unresolved item (D-26), proven with an item
        diverged in each direction.
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
            # package state and the decision-file paths (this plan's own prohibition):
            # the non-interactive branch prints every group's entries and logs how many
            # were left unresolved (package_review.review_items).
            combined_output = sync_result.stdout + sync_result.stderr
            assert install_candidate in combined_output, "install-direction item not named in the run's output"
            assert removal_candidate in combined_output, "removal-direction item not named in the run's output"
            assert "unresolved" in combined_output.lower(), "run did not report unresolved items (D-26)"
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
        """A failing item does not stop the job (D-27): the item ordered after it still
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
        """snap convergence lands the target on the source's revision (D-06) without
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

    async def test_flatpak_installs_into_source_scope_after_remote(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """flatpak convergence installs into the scope the source item carries and
        provisions the remote first (D-06, D-14): `flatpak install` refuses outright
        when its remote is not yet configured in that scope.

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

        application, version, scope, remote_name, remote_url, ref = await _flatpak_subject(pc1_executor)
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""

        remote_item_id = FlatpakRemoteItem(name=remote_name, url=remote_url, scope=scope).item_id
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

            decisions = {remote_item_id: Decision.APPLY, ref_item_id: Decision.APPLY}
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
            remote_marker = f"install {remote_name} remote ({scope}):"
            ref_marker = f"install {application} ("
            remote_index = combined_output.find(remote_marker)
            ref_index = combined_output.find(ref_marker)
            assert remote_index != -1, f"remote converge log line not found: {remote_marker!r}"
            assert ref_index != -1, f"ref converge log line not found: {ref_marker!r}"
            assert remote_index < ref_index, "remote must be provisioned before the ref installs (D-14)"
        finally:
            # Put pc2 back to a freshly provisioned TARGET's state: Flathub configured
            # with its real trust, the runtime kept, the app gone again. Leaving the app
            # installed would converge the pair and silently make a re-run of this test
            # prove nothing.
            await _restore_flatpak_target_baseline(pc2_executor)

    async def test_skip_always_is_inert_in_both_roles(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """A skip-always decision recorded in one run makes the item produce no diff in
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
        """The corrected D-24 (per-manager review): with two package jobs enabled and
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
        """A snippet registered on the source travels to the target by the job's own push
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
        """Run 1 converges a real apt divergence; run 2, with all three package-manager
        jobs enabled, changes NO package-manager state on the target and no longer
        presents the converged item at all.

        All three managers run, none conditionally: apt and snap ship with Ubuntu 24.04,
        and flatpak is installed on both VMs by the fixture script. `flatpak_sync` fails
        validation where flatpak is absent, and a validation error aborts the whole sync
        before any job executes, so its absence is checked up front rather than quietly
        narrowing what this test covers.

        Two independent witnesses, both read off pc2's own package managers rather than
        pc-switcher's output:

        1. `_MachinePackageState` (apt manual set, hold set and full dpkg-installed set;
           `snap list` revisions; `flatpak list` refs) is captured immediately before and
           after run 2 and must be identical -- no install, no removal, no re-mark.
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
        """With a system-wide `refresh.hold` engaged, a per-snap hold still reads `held`
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
        """The same assumption, end to end: a hold set on the source only reaches the
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
        """Run 1 records SKIP_ALWAYS for a source-only `apt-mark hold`; run 2 force-maps
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
        """The snap half of the same requirement, whose identity (`snap:hold:<name>`) is
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
        """N4, whole: pc1 -> pc2 installs a package; the user then removes it on pc2;
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
        """C24: a vendor repository file that exists only on the TARGET is removed, and
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
