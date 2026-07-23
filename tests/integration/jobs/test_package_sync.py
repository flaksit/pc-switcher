"""Integration tests proving the tracer's end-to-end apt_sync path against real VMs.

`apt_sync` (plan 02-03) claims that a package missing on the target travels source
capture -> target query -> diff -> the coordinator's one batched review -> `apt-get
install` on the target. Plan 02-03's own unit tests only prove that shape against a
mocked executor; this module is the VM-level proof against real apt/dpkg/sudo, run one
wave after the tracer and before snap_sync/flatpak_sync exist (02-13-PLAN.md).

Both tests drive the review non-interactively through
`PCSWITCHER_PACKAGE_REVIEW_AUTOMATION` (D-26's hidden test hook, `package_review.py`)
rather than through a real TTY, and assert exclusively against pc2's own `apt-mark
showmanual` output -- never against pc-switcher's log text -- per this plan's own
prohibition. The one exception: `apt-cache rdepends` output is read to pick a safe
removal candidate before either machine's package state is touched.

`TestPackageSyncWholeRunContracts` (plan 02-11) extends this same module with the
phase's whole-run contracts -- properties of an entire sync (non-interactive skip-all,
continue-on-item-failure, snap/flatpak convergence, skip-always inertness in both
roles, cross-manager batched review ordering) that are invisible to any single item's
mocked-executor unit test, reusing the fixture/teardown/candidate-selection
conventions established below by the tracer.

The pure parsing/selection helpers below (`nonblank_lines`, `parse_dpkg_installed`,
`parse_reverse_depends`, `parse_batched_rdepends`, `pick_safe_removal_candidate`) have no
I/O of their own and are unit-tested directly in
`tests/unit/jobs/test_package_sync_candidate_selection.py`, independent of VM access.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.jobs.package_items import AptPackageItem, FlatpakItem, FlatpakRemoteItem, UnreproducibleItem
from pcswitcher.jobs.package_review import PACKAGE_REVIEW_AUTOMATION_ENV, Decision
from pcswitcher.jobs.package_state import DECISION_FILE_RELPATH_TEMPLATE, DecisionFile, Snippet, SnippetRegistry

# Prefix marking each candidate's reverse-dependency block in the batched pc2 probe below.
RDEPENDS_MARKER = "@@RDEPENDS_FOR@@"


def nonblank_lines(text: str) -> list[str]:
    """Split command output into stripped, non-empty lines."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def parse_dpkg_installed(dpkg_query_output: str) -> set[str]:
    """Parse `dpkg-query -W -f='${Package}\\t${Status}\\n'` into fully-installed package names.

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


def _skip_message() -> str:
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
        "dpkg-query -W -f='${Package}\\t${Status}\\n'", login_shell=False, timeout=20.0
    )

    pc1_manual = nonblank_lines(pc1_manual_result.stdout)
    pc2_manual = set(nonblank_lines(pc2_manual_result.stdout))
    pc2_installed = parse_dpkg_installed(pc2_dpkg_result.stdout)

    initial_candidates = sorted(set(pc1_manual) & pc2_installed)
    if not initial_candidates:
        return []

    quoted = " ".join(shlex.quote(name) for name in initial_candidates)
    rdepends_result = await pc2_executor.run_command(
        f'for p in {quoted}; do echo "{RDEPENDS_MARKER}$p"; apt-cache rdepends --installed "$p"; done',
        login_shell=False,
        timeout=60.0,
    )
    reverse_deps_by_candidate = parse_batched_rdepends(rdepends_result.stdout)

    return pick_safe_removal_candidates(pc1_manual, pc2_installed, pc2_manual, reverse_deps_by_candidate, count)


async def _find_removable_candidate(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
) -> str | None:
    """Query both VMs and pick a package safe to remove from pc2 for this test (see
    `pick_safe_removal_candidate`), or `None` if nothing qualifies.
    """
    found = await _find_removable_candidates(pc1_executor, pc2_executor, count=1)
    return found[0] if found else None


async def _find_extra_on_target_candidate(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
) -> str | None:
    """A package manually installed on pc2 but absent from pc1's `apt-mark showmanual` --
    a naturally-occurring EXTRA_ON_TARGET (removal-direction) diff needing no setup
    mutation at all. Used by `test_non_interactive_skip_all`, which must never actually
    remove anything: the whole point of the test is that a non-interactive run skips
    every decision, so the candidate has to already exist rather than be created by
    removing something from pc1.
    """
    pc1_manual_result = await pc1_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    pc2_manual_result = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
    pc1_manual = set(nonblank_lines(pc1_manual_result.stdout))
    pc2_manual = nonblank_lines(pc2_manual_result.stdout)
    for name in sorted(pc2_manual):
        if name not in pc1_manual:
            return name
    return None


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
        f"mkdir -p ~/.config/pc-switcher && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n{config}CONF_EOF",
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
        f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {quoted} && sudo apt-mark manual {quoted}",
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
# package, one that deliberately exits non-zero. `AptSyncJob.scan_unowned_installs`
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
    """The `UnreproducibleItem.item_id` a `scan_unowned_installs`-detected path at
    `path` would produce (module docstring: identity is `unreproducible:<origin>:
    <identifier>`, independent of `label`).
    """
    return UnreproducibleItem(origin="unowned-path", identifier=path, label=path).item_id


async def _create_unowned_marker(executor: BashLoginRemoteExecutor, path: str) -> None:
    """Create an empty, dpkg-unowned directory at `path` (requires root: `/opt` is
    root-owned) so `AptSyncJob.scan_unowned_installs` detects it as an UNREPRODUCIBLE
    item on the next `plan()`.
    """
    result = await executor.run_command(f"sudo mkdir -p {shlex.quote(path)}", login_shell=False, timeout=15.0)
    assert result.success, f"Failed to create unowned marker {path}: {result.stderr}"


async def _remove_unowned_marker(executor: BashLoginRemoteExecutor, path: str) -> None:
    await executor.run_command(f"sudo rm -rf {shlex.quote(path)}", login_shell=False, timeout=15.0)


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


async def _find_divergeable_snap(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
) -> tuple[str, str, str] | None:
    """`(name, pc1_revision, alternate_revision)` for a snap installed on both machines
    where `snap info` names an installable revision distinct from pc1's current one --
    used to deliberately diverge pc2 before proving D-06 reconverges it. `None` if no
    such snap exists.
    """
    pc1_list = await pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    pc2_list = await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    pc1_revisions = parse_snap_list_names_revisions(pc1_list.stdout)
    pc2_revisions = parse_snap_list_names_revisions(pc2_list.stdout)

    for name in sorted(set(pc1_revisions) & set(pc2_revisions)):
        info = await pc2_executor.run_command(f"snap info {shlex.quote(name)}", login_shell=False, timeout=20.0)
        alternates = sorted(rev for rev in parse_snap_info_revisions(info.stdout) if rev != pc1_revisions[name])
        if alternates:
            return name, pc1_revisions[name], alternates[0]
    return None


async def _find_removable_snap_candidate(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
) -> str | None:
    """A snap installed on both pc1 and pc2, excluding `_SNAP_REMOVAL_DENYLIST`
    (T-02-28: never a base/snapd runtime everything else depends on) -- used by
    `test_all_managers_diff_before_any_applies` to diverge a second manager alongside
    apt without needing an exact-revision match the way `_find_divergeable_snap` does.
    """
    pc1_list = await pc1_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    pc2_list = await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
    pc1_names = set(parse_snap_list_names_revisions(pc1_list.stdout))
    pc2_names = set(parse_snap_list_names_revisions(pc2_list.stdout))
    for name in sorted((pc1_names & pc2_names) - _SNAP_REMOVAL_DENYLIST):
        return name
    return None


# -- flatpak helpers: independent of flatpak_sync's private parsers ------------------


def parse_flatpak_list_lines(output: str) -> list[tuple[str, str, str, str]]:
    """Parse `flatpak list --app --columns=application,version,origin,installation`
    into `(application, version, origin, installation)` tuples, tab-separated (mirrors
    `flatpak_sync._parse_flatpak_list`'s shape, kept independent since that parser is
    private to `flatpak_sync.py`).
    """
    rows: list[tuple[str, str, str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        rows.append((fields[0], fields[1], fields[2], fields[3]))
    return rows


async def _find_flatpak_ref_and_remote(
    executor: BashLoginRemoteExecutor,
) -> tuple[str, str, Literal["user", "system"], str, str] | None:
    """`(application, version, scope, remote_name, remote_url)` for the first installed
    flatpak ref found on `executor` (source) whose origin remote is also configured
    there, used to prove D-06/D-14 convergence for a real ref+remote pair. `None` if no
    ref with a resolvable origin remote is installed at all.
    """
    list_result = await executor.run_command(
        "flatpak list --app --columns=application,version,origin,installation",
        login_shell=False,
        timeout=20.0,
    )
    rows = sorted(parse_flatpak_list_lines(list_result.stdout))
    for application, version, origin, installation in rows:
        scope: Literal["user", "system"]
        if installation == "user":
            scope = "user"
        elif installation == "system":
            scope = "system"
        else:
            continue
        scope_flag = "--user" if scope == "user" else "--system"
        remotes_result = await executor.run_command(
            f"flatpak remotes {scope_flag} --columns=name,url", login_shell=False, timeout=20.0
        )
        for line in remotes_result.stdout.splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != 2:
                continue
            name, url = fields
            if name == origin:
                return application, version, scope, name, url
    return None


class TestAptSyncEndToEnd:
    """VM-level proof of plan 02-03's tracer path: a package missing on pc2 travels
    source capture -> target query -> diff -> the coordinator's one batched review ->
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

        candidate = await _find_removable_candidate(pc1_executor, pc2_executor)
        if candidate is None:
            pytest.skip(_skip_message())

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y {shlex.quote(candidate)}",
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

        candidate = await _find_removable_candidate(pc1_executor, pc2_executor)
        if candidate is None:
            pytest.skip(_skip_message())

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y {shlex.quote(candidate)}",
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


class TestPackageSyncWholeRunContracts:
    """VM-level proof of the phase's whole-run contracts (plan 02-11): properties of an
    entire sync -- non-interactive skip-all, continue-on-item-failure, snap/flatpak
    convergence, skip-always inertness in both roles, cross-manager batched-review
    ordering -- rather than any single item's diff/converge, and therefore invisible to
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

        install_candidate = await _find_removable_candidate(pc1_executor, pc2_executor)
        if install_candidate is None:
            pytest.skip(_skip_message())
        removal_candidate = await _find_extra_on_target_candidate(pc1_executor, pc2_executor)
        if removal_candidate is None:
            pytest.skip(
                "No package manually installed on pc2 but absent from pc1's apt-mark "
                "showmanual: searched pc2's showmanual minus pc1's, for a naturally-"
                "occurring EXTRA_ON_TARGET (removal-direction) candidate."
            )

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y {shlex.quote(install_candidate)}",
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
        `AptSyncJob.scan_unowned_installs`'s alphabetical sort to place the failing one
        strictly between the two installs (`_CONTINUE_TEST_MARKERS`).
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        candidates = await _find_removable_candidates(pc1_executor, pc2_executor, count=2)
        if len(candidates) < 2:
            pytest.skip(f"{_skip_message()} Needed 2 independent candidates, found {len(candidates)}.")
        pkg_first, pkg_second = candidates

        try:
            remove_result = await pc2_executor.run_command(
                "sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y "
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
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {shlex.quote(pkg_first)}",
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
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {shlex.quote(pkg_second)}",
            )

            await _write_apt_sync_config(pc1_executor)

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

        found = await _find_divergeable_snap(pc1_executor, pc2_executor)
        if found is None:
            pytest.skip(
                "No snap installed on both pc1 and pc2 with an alternate installable "
                "revision found via `snap info`: searched the intersection of both "
                "machines' `snap list --all` output."
            )
        name, source_revision, alternate_revision = found

        pc2_list_before = await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
        original_pc2_revision = parse_snap_list_names_revisions(pc2_list_before.stdout)[name]
        pc1_hold_before = await pc1_executor.run_command(
            "snap get system refresh.hold", login_shell=False, timeout=10.0
        )
        pc2_hold_before = await pc2_executor.run_command(
            "snap get system refresh.hold", login_shell=False, timeout=10.0
        )

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

            pc1_hold_after = await pc1_executor.run_command(
                "snap get system refresh.hold", login_shell=False, timeout=10.0
            )
            pc2_hold_after = await pc2_executor.run_command(
                "snap get system refresh.hold", login_shell=False, timeout=10.0
            )
            assert pc1_hold_after.stdout == pc1_hold_before.stdout, "sync mutated pc1's refresh.hold"
            assert pc2_hold_after.stdout == pc2_hold_before.stdout, (
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
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        found = await _find_flatpak_ref_and_remote(pc1_executor)
        if found is None:
            pytest.skip(
                "No installed flatpak ref found on pc1 with a matching configured "
                "remote: searched `flatpak list --app` and `flatpak remotes` in both scopes."
            )
        application, version, scope, remote_name, remote_url = found
        scope_flag = "--user" if scope == "user" else "--system"
        sudo = "sudo " if scope == "system" else ""

        remote_item_id = FlatpakRemoteItem(name=remote_name, url=remote_url, scope=scope).item_id
        ref_item_id = FlatpakItem(application=application, version=version, origin=remote_name, scope=scope).item_id

        try:
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall -y {scope_flag} {shlex.quote(application)}",
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
            sync_result = await pc1_executor.run_command(sync_cmd, timeout=180.0, login_shell=True)
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
            await pc2_executor.run_command(
                f"{sudo}flatpak uninstall -y {scope_flag} {shlex.quote(application)}",
                login_shell=False,
                timeout=60.0,
            )
            await pc2_executor.run_command(
                f"{sudo}flatpak remote-delete --force {scope_flag} {shlex.quote(remote_name)}",
                login_shell=False,
                timeout=30.0,
            )

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

        candidate = await _find_removable_candidate(pc1_executor, pc2_executor)
        if candidate is None:
            pytest.skip(_skip_message())
        item_id = AptPackageItem(name=candidate, version="").item_id

        try:
            remove_result = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y {shlex.quote(candidate)}",
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

    async def test_all_managers_diff_before_any_applies(
        self,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """The whole-run proof of D-24: with two package jobs enabled and both machines
        diverged, no manager's first mutating command runs before every enabled manager
        has produced its diff. `PackagePhaseCoordinator.run` plans every job, reviews
        once, then distributes -- proven here by finding the coordinator's own "N
        package manager(s) planned" log line and confirming it precedes the first
        per-item converge success log from either manager.

        This is the one test in this module whose PRIMARY claim is about ordering
        rather than end state (this plan's own prohibition explicitly carves this out):
        the end state alone -- both items converged -- cannot distinguish "planned
        together, then applied" from "apt_sync planned, applied, THEN snap_sync
        planned, applied", so the run's own per-item log is the only witness available.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)

        apt_candidate = await _find_removable_candidate(pc1_executor, pc2_executor)
        if apt_candidate is None:
            pytest.skip(_skip_message())
        snap_candidate = await _find_removable_snap_candidate(pc1_executor, pc2_executor)
        if snap_candidate is None:
            pytest.skip(
                "No snap installed on both pc1 and pc2 outside the system/snapd "
                "denylist: searched the intersection of both machines' `snap list --all` output."
            )

        pc2_snap_list_before = await pc2_executor.run_command("snap list --all", login_shell=False, timeout=20.0)
        original_snap_revision = parse_snap_list_names_revisions(pc2_snap_list_before.stdout)[snap_candidate]

        try:
            remove_apt = await pc2_executor.run_command(
                f"sudo DEBIAN_FRONTEND=noninteractive apt-get remove -y {shlex.quote(apt_candidate)}",
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

            after_apt = await pc2_executor.run_command("apt-mark showmanual", login_shell=False, timeout=15.0)
            assert apt_candidate in nonblank_lines(after_apt.stdout), f"{apt_candidate} not reinstalled on pc2"
            after_snap = await pc2_executor.run_command(
                f"snap list {shlex.quote(snap_candidate)}", login_shell=False, timeout=15.0
            )
            assert after_snap.success, f"{snap_candidate} not reinstalled on pc2: {after_snap.stderr}"

            # Ordering evidence (this test's one, explicitly-permitted exception to
            # asserting only against package-manager output): the coordinator's own
            # "planned; review covers" log line must precede EVERY converge success
            # log line from either manager.
            combined_output = sync_result.stdout + sync_result.stderr
            assert "2 package manager(s) planned" in combined_output, (
                "coordinator did not report both enabled managers planned together (D-24)"
            )
            coordinator_index = combined_output.find("package manager(s) planned; review covers")
            assert coordinator_index != -1, "coordinator's 'planned; review covers' log line not found"

            apt_converge_index = combined_output.find(f"install {apt_candidate} (")
            snap_converge_index = combined_output.find(f"install {snap_candidate} (")
            assert apt_converge_index != -1, "apt converge success log line not found"
            assert snap_converge_index != -1, "snap converge success log line not found"
            first_converge_index = min(apt_converge_index, snap_converge_index)

            assert coordinator_index < first_converge_index, (
                "a manager's first mutating command ran before every enabled manager "
                "had planned/diffed and the one batched review completed (D-24)"
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
