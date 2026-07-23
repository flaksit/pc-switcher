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

This file is also the phase's integration-test module: plan 02-11 adds the whole-run
non-interactive/continue-on-failure/snap/flatpak contract tests here once all three
managers exist, reusing the fixture and teardown patterns established below.

The pure parsing/selection helpers below (`nonblank_lines`, `parse_dpkg_installed`,
`parse_reverse_depends`, `parse_batched_rdepends`, `pick_safe_removal_candidate`) have no
I/O of their own and are unit-tested directly in
`tests/unit/jobs/test_package_sync_candidate_selection.py`, independent of VM access.
"""

from __future__ import annotations

import json
import shlex

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.jobs.package_items import AptPackageItem
from pcswitcher.jobs.package_review import PACKAGE_REVIEW_AUTOMATION_ENV, Decision

# apt_sync is the only enabled job. Configuration.sync_jobs is iterated as-is from the
# YAML dict (config.py), with no schema-default injection, so job names not listed here
# (folder_sync, dummy_success, ...) are never instantiated -- no explicit `false` needed.
_APT_SYNC_TEST_CONFIG = """\
logging:
  file: DEBUG
  tui: DEBUG
  external: DEBUG
sync_jobs:
  apt_sync: true
disk_space_monitor:
  preflight_minimum: "5%"
  runtime_minimum: "3%"
  warning_threshold: "10%"
  check_interval: 5
btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
  keep_recent: 2
"""

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
    for name in sorted(set(pc1_manual) & pc2_installed):
        if not (reverse_deps_by_candidate.get(name, set()) & pc2_manual):
            return name
    return None


def _skip_message() -> str:
    return (
        "No safe apt package candidate found: searched pc1's `apt-mark showmanual` "
        "intersected with pc2's installed set (`dpkg-query`), filtered to packages whose "
        "`apt-cache rdepends --installed` names no manually-installed package on pc2."
    )


async def _find_removable_candidate(
    pc1_executor: BashLoginRemoteExecutor, pc2_executor: BashLoginRemoteExecutor
) -> str | None:
    """Query both VMs and pick a package safe to remove from pc2 for this test (see
    `pick_safe_removal_candidate`), or `None` if nothing qualifies.
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
        return None

    quoted = " ".join(shlex.quote(name) for name in initial_candidates)
    rdepends_result = await pc2_executor.run_command(
        f'for p in {quoted}; do echo "{RDEPENDS_MARKER}$p"; apt-cache rdepends --installed "$p"; done',
        login_shell=False,
        timeout=60.0,
    )
    reverse_deps_by_candidate = parse_batched_rdepends(rdepends_result.stdout)

    return pick_safe_removal_candidate(pc1_manual, pc2_installed, pc2_manual, reverse_deps_by_candidate)


async def _write_apt_sync_config(executor: BashLoginRemoteExecutor) -> None:
    """Write the apt_sync-only test config to pc1 (source)."""
    result = await executor.run_command(
        f"mkdir -p ~/.config/pc-switcher && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n"
        f"{_APT_SYNC_TEST_CONFIG}CONF_EOF",
        timeout=10.0,
    )
    assert result.success, f"Failed to write apt_sync test config: {result.stderr}"


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


def _automation_env_assignment(item_id: str) -> str:
    """Shell-safe `VAR='{...}'` prefix pre-answering the review with one APPLY decision for
    `item_id` (D-26's hidden hook -- `package_review.PACKAGE_REVIEW_AUTOMATION_ENV`).
    """
    mapping = json.dumps({item_id: Decision.APPLY.value})
    return f"{PACKAGE_REVIEW_AUTOMATION_ENV}={shlex.quote(mapping)}"


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
