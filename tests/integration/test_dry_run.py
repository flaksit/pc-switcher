"""One `pc-switcher sync --dry-run`, asserted to have written nothing (ADR-014).

Markers, one per area the single run actually reaches:

- `area_folder`: folder_sync is the run's only sync job, and its rsync preview is what must
  leave the target payload byte-for-byte where it was;
- `area_btrfs`: the pre- and post-snapshot steps both run, and neither may leave a snapshot;
- `area_core`: the spine the rehearsal exercises around the job — source and target locks,
  the first-sync gate, config sync, and the sync-history write the orchestrator must skip.

Not `smoke`: a full sync run is too expensive for every PR, but the contract it guards is
cross-cutting, so it runs whenever any of the three areas is selected.

Deliberately NOT `area_package`: the package managers' own rehearsal is asserted, with the
subjects it needs, in `jobs/test_package_sync.py`; enabling those jobs here would cost VM
fixtures and minutes for a claim already covered.

Install-on-target is skipped (`SKIP_INSTALL_ON_TARGET`) rather than asserted: proving a
rehearsal does not install would need a target that lacks pc-switcher or carries an older
build, i.e. a second fixture that mutates pc2 — outside this test's one-run budget.

Scope choice: the seeded tree is the small filter tree, not the rich ownership/permission
matrix. Its value here is that a REAL sync would add files, overwrite one, and delete
several within it — the three kinds of write a rehearsal must not perform — which the rich
matrix would not prove any better while costing a much larger seed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from tests.integration import SKIP_INSTALL_ON_TARGET
from tests.integration.jobs import folder_sync_scenario

pytestmark = [pytest.mark.area_folder, pytest.mark.area_btrfs, pytest.mark.area_core]


def _dry_run_config(scope: str) -> str:
    """Source config for the rehearsal: one folder_sync scope and nothing else.

    DEBUG file logging because the preview's own summary line is read back out of the log.
    No `filter_file`: filter semantics are `test_end_to_end_sync.py`'s subject, and leaving
    the seeded tree unfiltered maximises what the rehearsal would have written.
    """
    return f"""# Dry-run contract test configuration

logging:
  file: DEBUG

sync_jobs:
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
# took both locks, and reached the first-sync gate without aborting on it.
_SPINE_MARKERS = (
    "[DRY-RUN] Preview mode",
    "Acquiring source lock",
    "Acquiring target lock",
    "skipping confirmation in dry-run mode",
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


async def _write_source_config(executor: BashLoginRemoteExecutor, scope: str) -> None:
    """Write the rehearsal's config on the source machine."""
    result = await executor.run_command(
        "mkdir --parents ~/.config/pc-switcher"
        f" && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n{_dry_run_config(scope)}CONF_EOF",
        timeout=10.0,
    )
    assert result.success, f"Failed to write dry-run test config: {result.stderr}"


class TestDryRunContract:
    """The tool-wide `--dry-run` contract, on a real sync (ADR-014, D-12)."""

    async def test_dry_run_previews_the_whole_pipeline_and_writes_nothing(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """ADR-014 / D-12: one rehearsal, every forbidden write asserted absent.

        pc2 is seeded so a real sync would add files, overwrite one and delete several
        inside the synced scope, and has no sync history — so the run also passes the W1
        first-sync gate, which a rehearsal logs and proceeds through instead of aborting.

        Forbidden and asserted absent, on BOTH machines: any change to the target payload,
        any btrfs snapshot, any sync-history update, and any config written to the target.
        Required and asserted present: exit 0, the locks and the gate in the log, and a
        folder_sync preview reporting a non-zero number of would-be transfers AND deletions.

        Run without `--yes`: a rehearsal must reach the end without a single prompt, so
        every gate it passes is one the dry-run path itself waved through, not one the
        flag answered.
        """
        _ = reset_pcswitcher_state  # Wipes config, history and snapshots on both VMs
        pc1 = pc1_with_pcswitcher_mod
        scope = folder_sync_scenario.filter_tree_path()

        try:
            await _write_source_config(pc1, scope)
            await folder_sync_scenario.seed_filter_source(pc1)
            await folder_sync_scenario.seed_filter_target(pc2_executor)

            payload_before = await folder_sync_scenario.capture_manifests(pc2_executor, scope)
            source_before = await _capture_machine_state(pc1)
            target_before = await _capture_machine_state(pc2_executor)

            rehearsal = await pc1.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --dry-run",
                timeout=300.0,
                login_shell=True,
            )
            assert rehearsal.success, (
                f"pc-switcher sync --dry-run exited {rehearsal.exit_code} (a rehearsal must never be blocked, "
                f"ADR-014).\nstdout: {rehearsal.stdout}\nstderr: {rehearsal.stderr}"
            )

            # 1. The target payload a real sync would have rewritten.
            await folder_sync_scenario.assert_manifests_unchanged(
                pc2_executor, scope, payload_before, "--dry-run wrote to the target payload (ADR-014)."
            )

            # 2-4. Snapshots, sync history and config, on both machines at once.
            source_after = await _capture_machine_state(pc1)
            target_after = await _capture_machine_state(pc2_executor)
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
            await folder_sync_scenario.remove_test_artifacts(pc1, pc2_executor, scope)
