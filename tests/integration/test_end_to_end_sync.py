"""Integration tests for end-to-end sync operations.

Tests CORE-US-JOB-ARCH (Job Architecture) acceptance scenarios:
- CORE-US-JOB-ARCH-AS1: Job integration via standardized interface

These tests verify the complete orchestrator workflow by actually running
`pc-switcher sync` on test VMs. They exercise the full sync pipeline including:
- Lock acquisition (source and target)
- SSH connection establishment
- Job discovery and validation
- Disk space preflight checks
- Pre-sync btrfs snapshots
- Config sync to target
- Sync job execution (dummy_success)
- Post-sync btrfs snapshots
- Cleanup and lock release

Interrupt (AS7) and target-unreachable-mid-sync coverage lives in test_interrupt_integration.py.

Test VM Requirements:
- pc1 and pc2 VMs must be provisioned and accessible
- VMs must have btrfs filesystem with @ and @home subvolumes
- VMs must be reset to baseline before tests run
"""

from __future__ import annotations

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from tests.integration import SKIP_INSTALL_ON_TARGET
from tests.integration.conftest import write_pcswitcher_config
from tests.integration.jobs import folder_sync_scenario

# The whole-pipeline runs here are cheap enough to be worth running on every PR, whatever the
# PR touches, so the file is smoke as a whole. Tests keep their own `area_*` markers on top.
pytestmark = pytest.mark.smoke

# The all-encompassing end-to-end run: the whole pipeline in one sync, with every kind of job it coordinates.
# The filter_file named here is written by folder_sync_scenario, which owns filter content.
# dummy_success durations of 1: the job ticks every 2s, so 1 is below its granularity and both phases
# run without sleeping. It still starts, runs a command on the target, and finishes — which is all this
# test asks of it (it stands in for "a generic job", and its ticks are covered by its own unit tests).
_FULL_PIPELINE_CONFIG = """\
sync_jobs:
  dummy_success: true
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
dummy_success:
  source_duration: 1
  target_duration: 1
folder_sync:
  folders:
    - path: /home
      enabled: true
      filter_file: ~/.config/pc-switcher/home.filter
"""


async def _assert_job_integration(
    source_executor: BashLoginRemoteExecutor,
    target_executor: BashLoginRemoteExecutor,
) -> None:
    """Assert the standardized job interface ran: both jobs logged, snapshots taken, config synced."""
    log_content = await source_executor.run_command(
        "cat $(ls --sort=time ~/.local/share/pc-switcher/logs/sync-*.log | head --lines=1)", timeout=10.0
    )
    assert log_content.success, f"Failed to read log file: {log_content.stderr}"
    log_text = log_content.stdout.lower()
    assert "dummy_success" in log_text, "Generic job (dummy_success) not logged."
    assert "folder_sync" in log_text, "folder_sync job not logged."
    for role, executor in (("source", source_executor), ("target", target_executor)):
        snaps = await executor.run_command(
            "sudo ls /.snapshots/pc-switcher/ 2>/dev/null | head --lines=1", timeout=10.0, login_shell=False
        )
        assert snaps.stdout.strip(), f"Pre/post-sync snapshots missing on {role}."
    tgt_config = await target_executor.run_command("cat ~/.config/pc-switcher/config.yaml", timeout=10.0)
    assert tgt_config.success and "dummy_success: true" in tgt_config.stdout, "Config not synced to target."


class TestEndToEndSync:
    """Integration tests for complete pc-switcher sync workflow."""

    # Exercises every kind of job the pipeline coordinates, in both directions, for two syncs.
    @pytest.mark.area_folder
    async def test_full_sync_pipeline_both_directions(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """CORE-US-JOB-ARCH-AS1 + full folder-sync end-to-end (ADR-015/016).

        One scenario, one seed, two syncs:

        1. A→B --allow-first-sync: the real sync. Verifies BOTH
           - job integration via the standardized interface (dummy_success + folder_sync discovered,
             logged, pre/post snapshots on both machines, config synced), AND
           - folder_sync of the real /home: byte-identical content; numeric uid/gid; permissions incl.
             setuid/setgid/sticky; POSIX ACL; mtime; hard-link inode sharing; symlink; across user-,
             root-, and other-user-owned files AND directories the invoking user cannot read; config
             exclusions honoured; the ADR-016 runtime-file excludes (state/install/logs) via sentinels;
             and (1f) the full #166 filter surface end-to-end — a central include-override (keep
             pcsw-filter/cache/keep-uv+keep-pip, drop the rest), a wholly-excluded subtree, and nested
             per-directory .pcswitcher-filter files — proving included paths add/overwrite/delete on
             the target, excluded paths survive on the target whether or not a source copy exists, and
             per-directory filter files themselves transfer.
        2. Mutate pc2 (add / modify / delete file / delete directory / chmod) then B→A: all propagate.

        The scenario's seeding, manifests and folder-sync assertions live in
        `tests/integration/jobs/folder_sync_scenario.py`, whose module docstring explains why
        syncing the real /home is safe here.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)
        tree = folder_sync_scenario.tree_path()

        try:
            await write_pcswitcher_config(pc1_executor, _FULL_PIPELINE_CONFIG)
            await folder_sync_scenario.write_filter_file(pc1_executor)
            await folder_sync_scenario.seed_rich_tree(pc1_executor, tree)
            await folder_sync_scenario.seed_included_markers(pc1_executor)
            await folder_sync_scenario.seed_filter_source(pc1_executor)
            await folder_sync_scenario.clear_target_tree(pc2_executor, tree)
            # Pre-seed the target-side files that drive the #166 --delete filter cases
            # (overwrite / delete-within-included / excluded-survivor). NOT under `tree`,
            # so the pc2 tree removal above leaves them in place for the first sync.
            await folder_sync_scenario.seed_filter_target(pc2_executor)
            await folder_sync_scenario.seed_state_sentinels(pc1_executor, pc2_executor)

            src_manifests = await folder_sync_scenario.capture_manifests(pc1_executor, tree)

            # --- Step 1: real first sync (--allow-first-sync) ---
            sync_ab = await pc1_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ab.success, (
                f"A→B first sync failed.\nexit={sync_ab.exit_code}\nstdout: {sync_ab.stdout}\nstderr: {sync_ab.stderr}"
            )

            # 1a. Job integration via interface: log entries, snapshots on both, config synced.
            await _assert_job_integration(pc1_executor, pc2_executor)

            # 1b. folder_sync content + metadata: target manifests must equal source manifests exactly.
            await folder_sync_scenario.assert_manifests_match(pc2_executor, tree, src_manifests)

            # 1c. ACL, backdated mtime, hard-link inode sharing, symlink target.
            await folder_sync_scenario.assert_metadata_details(pc2_executor, tree)

            # 1d. Exclusions: config-excluded subtree absent; ADR-016 runtime excludes held.
            await folder_sync_scenario.assert_exclusions(pc2_executor, tree)

            # 1e. SC3 inclusion: non-excluded dev-tool cache + VS Code user state ARE synced,
            # while a config-excluded sibling (VS Code Cache) is not.
            await folder_sync_scenario.assert_included_markers(pc2_executor)

            # 1f. #166 filter rules end-to-end (central include-override + wholly-excluded
            # subtree + nested per-directory .pcswitcher-filter files). Verifies that
            # included paths add/overwrite/delete on the target, that excluded paths leave
            # the target as-is whether or not a source counterpart exists (the --delete
            # survival case), and that per-directory filter files themselves transfer.
            await folder_sync_scenario.assert_filter_outcomes(pc2_executor)

            # --- Step 2: mutate pc2, then B→A ---
            await folder_sync_scenario.mutate_tree(pc2_executor, tree)

            sync_ba = await pc2_executor.run_command(
                f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc1 --yes", timeout=300.0, login_shell=True
            )
            assert sync_ba.success, (
                f"B→A sync failed.\nexit={sync_ba.exit_code}\nstdout: {sync_ba.stdout}\nstderr: {sync_ba.stderr}"
            )

            await folder_sync_scenario.assert_mutations_propagated(pc1_executor, tree)

        finally:
            await folder_sync_scenario.remove_test_artifacts(pc1_executor, pc2_executor, tree)
