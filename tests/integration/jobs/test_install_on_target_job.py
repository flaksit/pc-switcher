"""Integration tests for InstallOnTargetJob class.

These tests verify that the InstallOnTargetJob correctly installs or upgrades
pc-switcher on the target machine as part of the sync process.

Tests verify real behavior on VMs using the InstallOnTargetJob from
src/pcswitcher/jobs/install_on_target.py.

User Stories covered:
- CORE-US-SELF-INSTALL-AS1: Install missing pc-switcher on target
- CORE-US-SELF-INSTALL-AS2: Upgrade outdated target

**What these tests cover:**
- InstallOnTargetJob validation and execution logic
- Error handling (missing pc-switcher, version mismatches, etc.)

**What these tests do NOT cover:**
- install.sh script functionality (see test_installation_script.py)
- install.sh with VERSION parameter (see test_installation_script.py)
- Config synchronization (see test_config_sync.py)
- Pre/post-sync snapshot operations (see test_snapshot_infrastructure.py)
"""

from __future__ import annotations

from pcswitcher.events import EventBus
from pcswitcher.executor import BashLoginRemoteExecutor, LocalExecutor
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.install_on_target import InstallOnTargetJob
from pcswitcher.version import Release, find_one_version, get_this_version

from ..conftest import get_installed_version


async def _create_integration_job_context(
    source_executor: BashLoginRemoteExecutor,
    target_executor: BashLoginRemoteExecutor,
) -> JobContext:
    """Create a JobContext for integration tests.

    Integration tests use real executors but don't need a real EventBus.
    We create a minimal EventBus that doesn't process events.
    """
    # Get hostnames from executors
    source_hostname_result = await source_executor.run_command("hostname")
    target_hostname_result = await target_executor.run_command("hostname")

    source_hostname = source_hostname_result.stdout.strip() if source_hostname_result.success else "source"
    target_hostname = target_hostname_result.stdout.strip() if target_hostname_result.success else "target"

    # Create a minimal event bus (integration tests don't need event processing)
    event_bus = EventBus()

    # Note: pc1 will act as both source LocalExecutor and RemoteExecutor for testing
    # In real usage, source would be LocalExecutor and target would be RemoteExecutor
    # For integration tests, both are RemoteExecutors since we're testing from outside
    return JobContext(
        config={},
        source=LocalExecutor(),  # Create local executor for source operations
        target=target_executor,
        event_bus=event_bus,
        session_id="integration-test-session",
        source_hostname=source_hostname,
        target_hostname=target_hostname,
    )


class TestSelfInstallation:
    """Integration tests for automatic pc-switcher installation on target."""

    async def test_core_us_self_install_as1_install_missing_pcswitcher(
        self,
        pc2_without_pcswitcher_fn: BashLoginRemoteExecutor,
        pc1_executor: BashLoginRemoteExecutor,
        this_release_floor: Release,
    ) -> None:
        """CORE-US-SELF-INSTALL-AS1: Install missing pc-switcher on target.

        Given source machine has pc-switcher installed and target machine has no
        pc-switcher installed, When sync begins, Then the orchestrator detects
        missing installation, installs pc-switcher on target from GitHub repository,
        verifies installation succeeded, and proceeds with sync.

        Spec Reference: docs/system/spec.md - CORE-US-SELF-INSTALL, AS1
        """
        # Create job context for integration test
        context = await _create_integration_job_context(pc1_executor, pc2_without_pcswitcher_fn)

        # Create and run install job
        job = InstallOnTargetJob(context)

        # Validate should pass (no version conflict)
        errors = await job.validate()
        assert len(errors) == 0, f"Validation should pass: {errors}"

        # Execute installation
        await job.execute()

        # Verify pc-switcher is now installed on target
        result = await pc2_without_pcswitcher_fn.run_command("pc-switcher --version")
        assert result.success, f"pc-switcher should be installed on target: {result.stderr}"

        # Verify installed version matches source
        target_version = find_one_version(result.stdout)
        assert target_version == this_release_floor, (
            f"Target version {target_version} should match source {this_release_floor}"
        )

    async def test_core_us_self_install_as2_upgrade_outdated_target(
        self,
        pc2_with_old_pcswitcher_fn: BashLoginRemoteExecutor,
        pc1_executor: BashLoginRemoteExecutor,
        this_release_floor: Release,
    ) -> None:
        """CORE-US-SELF-INSTALL-AS2: Upgrade outdated target.

        Given source has a newer version and target has an older version, When sync
        begins, Then orchestrator detects version mismatch, logs the upgrade action,
        upgrades pc-switcher on target from GitHub repository, and verifies upgrade
        completed successfully.

        Spec Reference: docs/system/spec.md - CORE-US-SELF-INSTALL, AS2
        """
        # Fixture guarantees target has old version installed
        # Verify source is newer
        source_version = get_this_version()
        target_version_old = await get_installed_version(pc2_with_old_pcswitcher_fn)
        assert source_version > target_version_old, (
            f"Source {source_version} should be newer than target {target_version_old}"
        )

        # Create job context for integration test
        context = await _create_integration_job_context(pc1_executor, pc2_with_old_pcswitcher_fn)

        # Create and run install job
        job = InstallOnTargetJob(context)

        # Validate should pass (target older is acceptable)
        errors = await job.validate()
        assert len(errors) == 0, f"Validation should pass for upgrade: {errors}"

        # Execute upgrade
        await job.execute()

        # Verify target is now upgraded
        result = await pc2_with_old_pcswitcher_fn.run_command("pc-switcher --version")
        assert result.success, f"pc-switcher should be upgraded on target: {result.stderr}"

        # Verify upgraded version matches source
        target_version_new = find_one_version(result.stdout)
        assert target_version_new == this_release_floor, (
            f"Target version {target_version_new} should match source {this_release_floor} after upgrade"
        )
