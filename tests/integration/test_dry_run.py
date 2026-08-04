"""One `pc-switcher sync --dry-run`, asserted to have written nothing (ADR-014).

Markers, one per area the single run actually reaches:

- `area_folder`: folder_sync is the run's only sync job, and its rsync preview is what must
  leave the target payload byte-for-byte where it was;
- `area_btrfs`: the pre- and post-snapshot steps both run, and neither may leave a snapshot;
- `area_core`: the spine the rehearsal exercises around the job — source and target locks,
  the first-sync gate, config sync, and the sync-history write the orchestrator must skip;
- `area_install`: the run reaches a target with no pc-switcher, so install-on-target has an
  install to preview and must not perform it;
- `area_package`: the four package managers run too, each with one pending write, because
  they are where a rehearsal has the most to write and the most to get wrong — a repository
  file, a signing key, a pin, an `apt-get install`, a snap revision, a remote, a replayed
  snippet — and none of it may reach the target.

Not `smoke`: a full sync run is too expensive for every PR, but the contract it guards is
cross-cutting, so it runs whenever any of the five areas is selected.

Scope choice: the seeded tree is the small filter tree, not the rich ownership/permission
matrix. Its value here is that a REAL sync would add files, overwrite one, and delete
several within it — the three kinds of write a rehearsal must not perform — which the rich
matrix would not prove any better while costing a much larger seed. The package seeding
follows the same rule (`package_sync_scenario.seed_a_pending_write_in_every_manager`): one
pending write per manager, none of the machinery a CONVERGING run needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from tests.integration.conftest import write_pcswitcher_config
from tests.integration.jobs import folder_sync_scenario, package_sync_scenario
from tests.integration.jobs.package_sync_scenario import AptSubjects

pytestmark = [
    pytest.mark.area_folder,
    pytest.mark.area_btrfs,
    pytest.mark.area_core,
    pytest.mark.area_install,
    pytest.mark.area_package,
]


def _dry_run_config(scope: str) -> str:
    """Source config for the rehearsal: the four package managers and one folder_sync scope.

    DEBUG file logging because the preview's own summary line is read back out of the log,
    and DEBUG on the tui because the package jobs' previews are read off the run's own output.
    No `filter_file`: filter semantics are `test_end_to_end_sync.py`'s subject, and leaving
    the seeded tree unfiltered maximises what the rehearsal would have written.
    """
    return f"""# Dry-run contract test configuration

logging:
  file: DEBUG
  tui: DEBUG

sync_jobs:
  apt_sync: true
  snap_sync: true
  flatpak_sync: true
  manual_installs_sync: true
  folder_sync: true

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

folder_sync:
  folders:
    - path: {scope}
      enabled: true
"""


# Reads, in one round trip, the three pieces of state ADR-014 forbids a rehearsal to touch.
# Deliberately does NOT read the log file or the lock file under the same state directory:
# both are written on a dry run by design (the run is logged, and the locks are still
# taken), so including them would assert the opposite of the contract.
_STATE_PROBE = (
    "printf '@@HISTORY@@'; "
    "cat ~/.local/share/pc-switcher/sync-history.json 2>/dev/null || printf __ABSENT__; "
    "printf '@@SNAPSHOTS@@'; "
    "sudo find /.snapshots/pc-switcher -mindepth 1 2>/dev/null | LC_ALL=C sort; "
    "printf '@@CONFIG@@'; "
    "cat ~/.config/pc-switcher/config.yaml 2>/dev/null || printf __ABSENT__"
)

# folder_sync's per-folder summary, which every pass emits and only a dry run prefixes
# (`[dry-run] Completed sync of '<path>': N files transferred, <size>, M deletions`).
# The two counts are the evidence that the run rehearsed the transfer rather than no-opped.
_PREVIEW_SUMMARY = re.compile(r"\[dry-run\] Completed sync of [^:]+: (\d+) files transferred, [^,]+, (\d+) deletions")

# Log lines proving the read-only half of the contract ran: the run knew it was a rehearsal,
# took both locks, reached the first-sync gate without aborting on it, and reached the point
# where it would have installed itself on a target that has no pc-switcher.
_SPINE_MARKERS = (
    "[DRY-RUN] Preview mode",
    "Acquiring source lock",
    "Acquiring target lock",
    "skipping confirmation in dry-run mode",
    "Installing pc-switcher",
)


@dataclass(frozen=True)
class _MachineState:
    """One machine's answer to `_STATE_PROBE`."""

    sync_history: str
    snapshots: str
    config: str


async def _capture_machine_state(executor: BashLoginRemoteExecutor) -> _MachineState:
    """Read the forbidden-to-write state off one machine."""
    result = await executor.run_command(_STATE_PROBE, timeout=30.0, login_shell=False)
    assert result.success, f"state probe failed: {result.stderr}"
    _, _, rest = result.stdout.partition("@@HISTORY@@")
    history, _, rest = rest.partition("@@SNAPSHOTS@@")
    snapshots, _, config = rest.partition("@@CONFIG@@")
    return _MachineState(sync_history=history.strip(), snapshots=snapshots.strip(), config=config.strip())


class TestDryRunContract:
    """The tool-wide `--dry-run` contract, on a real sync (ADR-014, D-12)."""

    async def test_dry_run_previews_the_whole_pipeline_and_writes_nothing(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_without_pcswitcher_fn: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        package_sync_subjects: None,
        apt_subjects: AptSubjects,
    ) -> None:
        """ADR-014 / D-12, J1, K9 — one rehearsal, every forbidden write asserted absent.

        pc2 is seeded so a real sync would add files, overwrite one and delete several
        inside the synced scope, would install a package, a flatpak ref and a snippet, and
        would take a repository, a signing key and a pin into its `/etc/apt`. It has no sync
        history — so the run also passes the W1 first-sync gate, which a rehearsal logs and
        proceeds through instead of aborting — and carries no pc-switcher, so
        install-on-target has a real install to withhold. The run therefore does NOT set the
        install-on-target skip that every other sync test carries: the step it would skip is
        one of this test's subjects.

        Forbidden and asserted absent, on BOTH machines: any change to the target payload,
        any change to the target's whole package-manager state, any btrfs snapshot, any
        sync-history update, any config written to the target, and any pc-switcher installed
        on the target. Required and asserted present: exit 0, the locks, the gate and the
        withheld install in the log, a folder_sync preview reporting a non-zero number of
        would-be transfers AND deletions, and the always-sync pin previewed as a derived
        write while the repository that feeds no approved package is previewed not at all.

        Run without `--yes`: a rehearsal must reach the end without a single prompt, so
        every gate it passes is one the dry-run path itself waved through, not one the
        flag answered. It passes no review decisions either, which is what leaves every
        decidable item unapproved: what the preview must still report is the DERIVED writes,
        which no answer of the user's controls.
        """
        _ = (reset_pcswitcher_state, package_sync_subjects)  # Wipes config, history and snapshots on both VMs
        pc1 = pc1_with_pcswitcher_mod
        pc2 = pc2_without_pcswitcher_fn  # Same VM as pc2_executor, with pc-switcher removed
        scope = folder_sync_scenario.filter_tree_path()
        package_seed = None

        try:
            await write_pcswitcher_config(pc1, _dry_run_config(scope))
            await folder_sync_scenario.seed_filter_source(pc1)
            await folder_sync_scenario.seed_filter_target(pc2)
            package_seed = await package_sync_scenario.seed_a_pending_write_in_every_manager(pc1, pc2, apt_subjects)

            payload_before = await folder_sync_scenario.capture_manifests(pc2, scope)
            packages_before = await package_sync_scenario.capture_machine_package_state(pc2)
            source_before = await _capture_machine_state(pc1)
            target_before = await _capture_machine_state(pc2)

            rehearsal = await pc1.run_command(
                "pc-switcher sync pc2 --dry-run",
                timeout=600.0,
                login_shell=True,
            )
            assert rehearsal.success, (
                f"pc-switcher sync --dry-run exited {rehearsal.exit_code} (a rehearsal must never be blocked, "
                f"ADR-014).\nstdout: {rehearsal.stdout}\nstderr: {rehearsal.stderr}"
            )

            # 1. The target payload a real sync would have rewritten.
            await folder_sync_scenario.assert_manifests_unchanged(
                pc2, scope, payload_before, "--dry-run wrote to the target payload (ADR-014)."
            )

            # 1b. The target's whole package-manager state, and what the preview says it
            # would have written to `/etc/apt`.
            package_sync_scenario.assert_the_rehearsal_wrote_nothing(
                package_seed,
                packages_before,
                await package_sync_scenario.capture_machine_package_state(pc2),
                rehearsal.stdout + rehearsal.stderr,
            )

            # 2-5. Snapshots, sync history, target config, and the install withheld.
            source_after = await _capture_machine_state(pc1)
            target_after = await _capture_machine_state(pc2)
            assert source_after == source_before, (
                f"--dry-run changed source state (ADR-014).\nbefore: {source_before}\nafter: {source_after}"
            )
            assert target_after == target_before, (
                f"--dry-run changed target state (ADR-014).\nbefore: {target_before}\nafter: {target_after}"
            )
            # Equality alone would also hold if the run had found state already there, so
            # state the absolute claims the reset guarantees the run started from.
            assert not source_after.snapshots and not target_after.snapshots, (
                "--dry-run created btrfs snapshots.\n"
                f"source: {source_after.snapshots!r}\ntarget: {target_after.snapshots!r}"
            )
            assert source_after.sync_history == "__ABSENT__" and target_after.sync_history == "__ABSENT__", (
                "--dry-run recorded sync history (D-12).\n"
                f"source: {source_after.sync_history!r}\ntarget: {target_after.sync_history!r}"
            )
            assert target_after.config == "__ABSENT__", (
                f"--dry-run copied the config to the target.\n{target_after.config!r}"
            )
            # Not part of the probe: presence is only meaningful through a login shell,
            # which is how the uninstall in `pc2_without_pcswitcher_fn` checks it too.
            installed = await pc2.run_command("command -v pc-switcher", timeout=10.0)
            assert not installed.success, f"--dry-run installed pc-switcher on the target.\n{installed.stdout}"

            # 5. The rehearsal actually rehearsed: spine steps ran, and the preview has value.
            log = await pc1.run_command(
                "cat $(ls --sort=time ~/.local/share/pc-switcher/logs/sync-*.log | head --lines=1)", timeout=30.0
            )
            assert log.success, f"Failed to read the sync log: {log.stderr}"
            missing = [marker for marker in _SPINE_MARKERS if marker not in log.stdout]
            assert not missing, f"Read-only steps missing from the dry-run log: {missing}\n{log.stdout}"

            summary = _PREVIEW_SUMMARY.search(log.stdout)
            assert summary is not None, f"folder_sync reported no dry-run preview for {scope!r}.\n{log.stdout}"
            files_previewed, deletions_previewed = int(summary.group(1)), int(summary.group(2))
            assert files_previewed > 0 and deletions_previewed > 0, (
                f"The preview reported {files_previewed} transfers and {deletions_previewed} deletions: the "
                "rehearsal no-opped instead of previewing the seeded divergence (ADR-014 requires a real preview)."
            )

        finally:
            if package_seed is not None:
                await package_sync_scenario.restore_after_the_pending_writes(pc1, pc2, package_seed)
            await folder_sync_scenario.remove_test_artifacts(pc1, pc2, scope)
