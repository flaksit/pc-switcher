"""The whole pipeline, in both directions, over one pair of machines.

Tests CORE-US-JOB-ARCH (Job Architecture) acceptance scenarios:
- CORE-US-JOB-ARCH-AS1: Job integration via standardized interface

These tests verify the complete orchestrator workflow by actually running
`pc-switcher sync` on test VMs. They exercise the full sync pipeline including:
- Lock acquisition (source and target)
- SSH connection establishment
- Job discovery and validation
- Disk space preflight checks
- Pre-sync btrfs snapshots
- Install/upgrade of pc-switcher on the target
- Config sync to target
- Sync job execution: dummy_success, the four package managers, folder_sync
- Post-sync btrfs snapshots
- Cleanup and lock release

The happy path of every job runs here, including the package managers: one seeded divergence
per manager converges on the way out and the reverse direction converges on the way back, so
the standard workflow is proven by the two syncs this test already pays for rather than by
runs of its own (#216). What the package suite keeps are the runs no converging run can be —
runs that fail, abort, are killed, or have nobody to answer their review
(`tests/integration/jobs/test_package_sync.py`).

Interrupt (AS7) and target-unreachable-mid-sync coverage lives in test_interrupt_integration.py.

Test VM Requirements:
- pc1 and pc2 VMs must be provisioned and accessible
- VMs must have btrfs filesystem with @ and @home subvolumes
- VMs must be reset to baseline before tests run
"""

from __future__ import annotations

import pytest
import yaml

from pcswitcher.executor import BashLoginRemoteExecutor
from tests.integration.conftest import write_pcswitcher_config
from tests.integration.jobs import folder_sync_scenario, package_sync_scenario
from tests.integration.jobs.package_sync_scenario import AptSubjects

# The whole-pipeline runs here are cheap enough to be worth running on every PR, whatever the
# PR touches, so the file is smoke as a whole. Tests keep their own `area_*` markers on top.
pytestmark = pytest.mark.smoke

# The all-encompassing end-to-end run: the whole pipeline in one sync, with every kind of job it coordinates.
# The filter_file named here is written by folder_sync_scenario, which owns filter content.
# dummy_success durations of 1: the job ticks every 2s, so 1 is below its granularity and both phases
# run without sleeping. It still starts, runs a command on the target, and finishes — which is all this
# test asks of it (it stands in for "a generic job", and its ticks are covered by its own unit tests).
# Job order is execution order (`_discover_and_validate_jobs` iterates `sync_jobs` as written), and
# folder_sync must stay last: it asks the target for the snap revision map and the snippet registry's
# consent after the package jobs have gone (`PKG-FR-JOB-ORDER`).
_FULL_PIPELINE_CONFIG = """\
logging:
  tui: FULL
sync_jobs:
  dummy_success: true
  apt_sync: true
  snap_sync: true
  flatpak_sync: true
  manual_deb_sync: true
  manual_snap_sync: true
  manual_flatpak_sync: true
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
dummy_success:
  source_duration: 1
  target_duration: 1
folder_sync:
  folders:
    - path: /home
      enabled: true
      filter_file: ~/.config/pc-switcher/home.filter
"""

#: The jobs the config above enables, read back out of it rather than restated: the outcome
#: block must name every one, and a job added to the config with no matching entry here
#: would leave that claim quietly weaker instead of failing.
_PIPELINE_JOBS = tuple(name for name, enabled in yaml.safe_load(_FULL_PIPELINE_CONFIG)["sync_jobs"].items() if enabled)


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
    assert "install_on_target" in log_text, "install-on-target step not logged."
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
    @pytest.mark.area_package
    async def test_full_sync_pipeline_both_directions(  # noqa: PLR0913, PLR0917 - pytest fixtures, injected by name
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        package_sync_subjects: None,
        apt_subjects: AptSubjects,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """J1, K9, J116, B1, E21, E67, K10, E55, K11, F72, G67, H30, K12, K16, N8, J145, A54,
        N9, F103, J190 — CORE-US-JOB-ARCH-AS1, the full pipeline end-to-end (ADR-015/016), and
        `PKG-FR-FLATPAK-REMOTE-DERIVED` in both flatpak directions plus `PKG-FR-FLATPAK-FILTER`'s two halves.

        One scenario, one seed, two syncs:

        1. A→B --allow-first-sync: the real sync. Verifies
           - job integration via the standardized interface (every configured job discovered,
             logged, pre/post snapshots on both machines, config synced, pc-switcher put on the
             target), AND
           - folder_sync of the real /home: byte-identical content; numeric uid/gid; permissions incl.
             setuid/setgid/sticky; POSIX ACL; mtime; hard-link inode sharing; symlink; across user-,
             root-, and other-user-owned files AND directories the invoking user cannot read; config
             exclusions honoured; the ADR-016 runtime-file excludes (state/install/logs) via sentinels;
             and (1f) the full #166 filter surface end-to-end — a central include-override (keep
             pcsw-filter/cache/keep-uv+keep-pip, drop the rest), a wholly-excluded subtree, and nested
             per-directory .pcswitcher-filter files — proving included paths add/overwrite/delete on
             the target, excluded paths survive on the target whether or not a source copy exists, and
             per-directory filter files themselves transfer, AND
           - (1g) one seeded divergence per package manager converging on the target while the
             source's own package state does not move at all (`PKG-FR-SOURCE-INTENT`).
        2. Mutate pc2 and undo one of its installs, then B→A: the folder mutations propagate
           (add / modify / delete file / delete directory / chmod), the removal comes back and
           is applied, a ref filter the new source dropped comes off the new target — and
           nothing else on that machine moves, which is what a fixed point over an already
           converged pair looks like from the only direction this test has.

        The seeding, manifests and per-claim assertions live in
        `tests/integration/jobs/folder_sync_scenario.py` — whose module docstring explains why
        syncing the real /home is safe here — and in
        `tests/integration/jobs/package_sync_scenario.py`.

        The install-on-target step is deliberately NOT skipped here (`SKIP_INSTALL_ON_TARGET`):
        this is the one test whose subject is the whole pipeline, so the step that puts
        pc-switcher on the target belongs in it.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state, package_sync_subjects)
        tree = folder_sync_scenario.tree_path()
        seed = None

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

            seed = await package_sync_scenario.seed_a_divergence_in_every_manager(
                pc1_executor, pc2_executor, apt_subjects
            )
            approve = package_sync_scenario.automation_env_assignment_multi(seed.approve_everything())

            src_manifests = await folder_sync_scenario.capture_manifests(pc1_executor, tree)
            pc1_before = await package_sync_scenario.capture_machine_package_state(pc1_executor)

            # --- Step 1: real first sync (--allow-first-sync) ---
            sync_ab = await pc1_executor.run_command(
                f"{approve} pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=900.0,
                login_shell=True,
            )
            assert sync_ab.success, (
                f"A→B first sync failed.\nexit={sync_ab.exit_code}\nstdout: {sync_ab.stdout}\nstderr: {sync_ab.stderr}"
            )

            # 1a. Job integration via interface: log entries, snapshots on both, config synced.
            await _assert_job_integration(pc1_executor, pc2_executor)

            # 1a'. The end-of-run outcome block (`CORE-FR-SUMMARY`). Read here rather than in
            # a run of its own: this is the only test whose config enables the whole pipeline
            # AND lets `install_on_target` run, so it is the only place that step's own
            # JobResult can be anything but skipped.
            outcomes = package_sync_scenario.job_outcome_statuses(sync_ab.stdout + sync_ab.stderr)
            assert set(_PIPELINE_JOBS) <= set(outcomes), (
                f"the outcome block names {sorted(outcomes)}, missing "
                f"{sorted(set(_PIPELINE_JOBS) - set(outcomes))} of the jobs this run configured"
            )
            assert outcomes.get("install_on_target") == "success", (
                f"install_on_target is {outcomes.get('install_on_target')!r} in the outcome block; the step that "
                f"puts pc-switcher on the target records a JobResult of its own, not only when it is skipped"
            )
            # Which jobs report `success` rather than `skipped` depends on what this run's
            # seeding left each manager to do, so only the failures are pinned here — a
            # converging pipeline must show none.
            assert not [job for job, status in outcomes.items() if status == "failed"], (
                f"the outcome block reports failures: {outcomes}"
            )

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

            # 1g. Every package manager's half of the seeded divergence, on the target's own
            # package managers — and the source's whole package state, unmoved.
            await package_sync_scenario.assert_every_manager_converged(
                pc1_executor, pc2_executor, seed, sync_ab.stdout + sync_ab.stderr, pc1_before
            )

            # --- Step 2: mutate pc2 and undo one of its installs, then B→A ---
            await folder_sync_scenario.mutate_tree(pc2_executor, tree)
            pc1_after_forward = await package_sync_scenario.capture_machine_package_state(pc1_executor)
            await package_sync_scenario.seed_the_back_direction(pc2_executor, pc1_executor, seed)
            undo = package_sync_scenario.automation_env_assignment_multi(
                package_sync_scenario.back_direction_decisions(seed)
            )

            sync_ba = await pc2_executor.run_command(
                f"{undo} pc-switcher sync pc1 --yes", timeout=900.0, login_shell=True
            )
            assert sync_ba.success, (
                f"B→A sync failed.\nexit={sync_ba.exit_code}\nstdout: {sync_ba.stdout}\nstderr: {sync_ba.stderr}"
            )

            await folder_sync_scenario.assert_mutations_propagated(pc1_executor, tree)
            await package_sync_scenario.assert_the_back_direction_converged(
                pc2_executor, pc1_executor, seed, pc1_after_forward
            )

        finally:
            if seed is not None:
                await package_sync_scenario.restore_after_the_divergence(pc1_executor, pc2_executor, seed)
            await folder_sync_scenario.remove_test_artifacts(pc1_executor, pc2_executor, tree)
