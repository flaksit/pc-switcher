"""Integration tests for InstallOnTargetJob class.

These tests verify that the InstallOnTargetJob correctly installs or upgrades
pc-switcher on the target machine as part of the sync process.

Tests verify real behavior on VMs using the InstallOnTargetJob from
src/pcswitcher/jobs/install_on_target.py.

User Stories covered:
- US2-AS1: Install missing pc-switcher on target
- US2-AS2: Upgrade outdated target

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

import pytest

from pcswitcher.events import EventBus
from pcswitcher.executor import BashLoginRemoteExecutor, LocalExecutor
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.install_on_target import InstallOnTargetJob
from pcswitcher.version import Version, find_one_version, get_this_version


def _is_dev_version() -> bool:
    """Check if current version is a development version.

    Development versions have .dev in their version string and don't have
    corresponding release tags on GitHub.
    """
    version_str = get_this_version().pep440_str()
    return ".dev" in version_str or ".post" in version_str


# Skip marker for tests that require a released version
requires_release_version = pytest.mark.skipif(
    _is_dev_version(),
    reason="Test requires a released version (not a development version)",
)


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

    @requires_release_version
    async def test_001_us2_as1_install_missing_pcswitcher(
        self,
        pc2_without_pcswitcher: BashLoginRemoteExecutor,
        pc1_executor: BashLoginRemoteExecutor,
    ) -> None:
        """US2-AS1: Install missing pc-switcher on target.

        Given source machine has pc-switcher installed and target machine has no
        pc-switcher installed, When sync begins, Then the orchestrator detects
        missing installation, installs pc-switcher on target from GitHub repository,
        verifies installation succeeded, and proceeds with sync.

        Spec Reference: specs/001-foundation/spec.md - User Story 2, AS1
        """
        # Create job context for integration test
        context = await _create_integration_job_context(pc1_executor, pc2_without_pcswitcher)

        # Create and run install job
        job = InstallOnTargetJob(context)

        # Validate should pass (no version conflict)
        errors = await job.validate()
        assert len(errors) == 0, f"Validation should pass: {errors}"

        # Execute installation
        await job.execute()

        # Verify pc-switcher is now installed on target
        result = await pc2_without_pcswitcher.run_command("pc-switcher --version")
        assert result.success, f"pc-switcher should be installed on target: {result.stderr}"

        # Verify installed version matches source
        source_version = get_this_version()
        target_version = find_one_version(result.stdout)
        assert target_version == source_version, (
            f"Target version {target_version} should match source {source_version}"
        )

    @requires_release_version
    async def test_001_us2_as2_upgrade_outdated_target(
        self,
        pc2_with_old_pcswitcher: BashLoginRemoteExecutor,
        pc1_executor: BashLoginRemoteExecutor,
    ) -> None:
        """US2-AS2: Upgrade outdated target.

        Given source has a newer version and target has an older version, When sync
        begins, Then orchestrator detects version mismatch, logs the upgrade action,
        upgrades pc-switcher on target from GitHub repository, and verifies upgrade
        completed successfully.

        Spec Reference: specs/001-foundation/spec.md - User Story 2, AS2
        """
        # Fixture guarantees target has 0.1.0-alpha.1 installed
        # Verify source is newer
        source_version = get_this_version()
        target_version_old = Version.parse("0.1.0-alpha.1")
        assert source_version > target_version_old, (
            f"Source {source_version} should be newer than target {target_version_old}"
        )

        # Create job context for integration test
        context = await _create_integration_job_context(pc1_executor, pc2_with_old_pcswitcher)

        # Create and run install job
        job = InstallOnTargetJob(context)

        # Validate should pass (target older is acceptable)
        errors = await job.validate()
        assert len(errors) == 0, f"Validation should pass for upgrade: {errors}"

        # Execute upgrade
        await job.execute()

        # Verify target is now upgraded
        result = await pc2_with_old_pcswitcher.run_command("pc-switcher --version")
        assert result.success, f"pc-switcher should be upgraded on target: {result.stderr}"

        # Verify upgraded version matches source
        target_version_new = find_one_version(result.stdout)
        assert target_version_new == source_version, (
            f"Target version {target_version_new} should match source {source_version} after upgrade"
        )
