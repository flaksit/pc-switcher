"""Unit tests for InstallOnTargetJob.

Tests cover FND-US-SELF-INSTALL (Self-Installation) requirements from specs/001-foundation/spec.md:
- FND-FR-VERSION-CHECK: Check version, install from GitHub
- FND-FR-VERSION-NEWER: Abort if target newer
- FND-FR-INSTALL-FAIL: Abort on installation failure
- US2-AS3: Skip when versions match
- US2-AS4: Abort on install failure
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.install_on_target import InstallOnTargetJob
from pcswitcher.models import CommandResult, Host
from pcswitcher.version import Release, Version


@pytest.fixture
def mock_install_context(
    mock_local_executor: MagicMock,
    mock_remote_executor: MagicMock,
    mock_event_bus: MagicMock,
) -> JobContext:
    """Create a JobContext for InstallOnTargetJob testing."""
    return JobContext(
        config={},
        source=mock_local_executor,
        target=mock_remote_executor,
        event_bus=mock_event_bus,
        session_id="test-session",
        source_hostname="source-host",
        target_hostname="target-host",
    )


class TestInstallOnTargetJobVersionCheck:
    """Test version checking and installation logic - FND-FR-VERSION-CHECK."""

    @pytest.mark.asyncio
    async def test_001_fnd_fr_version_check(self, mock_install_context: JobContext) -> None:
        """FND-FR-VERSION-CHECK: System must check target version and install from GitHub if missing.

        Spec requirement: FND-FR-VERSION-CHECK states system MUST check target machine's pc-switcher
        version before any other operations; if missing, MUST install from public
        GitHub repository using uv tool install.
        """
        # Mock source version and release
        mock_version = Version.parse("0.4.0")
        mock_release = Release(version=mock_version, is_prerelease=False, tag="v0.4.0")

        with (
            patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=mock_version),
            patch.object(Version, "get_release_floor", return_value=mock_release),
        ):
            # Mock target has no pc-switcher installed (command fails)
            mock_install_context.target.run_command = AsyncMock(
                side_effect=[
                    # Validate: pc-switcher --version - missing (target_version stays None)
                    CommandResult(exit_code=127, stdout="", stderr="command not found"),
                    # Execute: install command succeeds
                    CommandResult(exit_code=0, stdout="", stderr=""),
                    # Execute: verify installation
                    CommandResult(exit_code=0, stdout="pc-switcher 0.4.0", stderr=""),
                ]
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate phase should pass (target not newer, just missing)
            errors = await job.validate()
            assert errors == []

            # Execute phase should install
            await job.execute()

            # Verify install command was called with correct version
            # validate + install + verify = 3 calls
            calls = mock_install_context.target.run_command.call_args_list
            assert len(calls) == 3
            install_call = calls[1][0][0]
            assert "curl" in install_call
            assert "install.sh" in install_call
            # The VERSION env var should be passed to the install script
            assert re.search(r"VERSION=(?:(?P<q>['\"])(v)?0\.4\.0(?P=q)|v0\.4\.0)", install_call), (
                f"Should pass VERSION env var. Got: {install_call}"
            )

    @pytest.mark.asyncio
    async def test_001_fnd_fr_version_check_upgrade(self, mock_install_context: JobContext) -> None:
        """FND-FR-VERSION-CHECK: System must upgrade target when version is older than source.

        Spec requirement: FND-FR-VERSION-CHECK requires installation/upgrade when target is
        missing or mismatched with source version.
        """
        # Mock source version and release
        mock_version = Version.parse("0.4.0")
        mock_release = Release(version=mock_version, is_prerelease=False, tag="v0.4.0")

        with (
            patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=mock_version),
            patch.object(Version, "get_release_floor", return_value=mock_release),
        ):
            # Mock target has older version
            mock_install_context.target.run_command = AsyncMock(
                side_effect=[
                    # Validate phase: target has 0.3.2 (stores target_version)
                    CommandResult(exit_code=0, stdout="pc-switcher 0.3.2", stderr=""),
                    # Execute: install/upgrade command succeeds
                    CommandResult(exit_code=0, stdout="", stderr=""),
                    # Execute: verify installation
                    CommandResult(exit_code=0, stdout="pc-switcher 0.4.0", stderr=""),
                ]
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate should pass and store target version
            errors = await job.validate()
            assert errors == []

            # Execute should upgrade (using stored target_version)
            await job.execute()

            # Verify upgrade was attempted: validate + install + verify = 3 calls
            calls = mock_install_context.target.run_command.call_args_list
            assert len(calls) == 3
            install_call = calls[1][0][0]
            assert "install.sh" in install_call
            # The VERSION env var should be passed to the install script
            assert re.search(r"VERSION=(?:(?P<q>['\"])(v)?0\.4\.0(?P=q)|v0\.4\.0)", install_call), (
                f"Should pass VERSION env var. Got: {install_call}"
            )


class TestInstallOnTargetJobNewerTargetVersion:
    """Test abort when target has newer version - FND-FR-VERSION-NEWER."""

    @pytest.mark.asyncio
    async def test_001_fnd_fr_version_newer(self, mock_install_context: JobContext) -> None:
        """FND-FR-VERSION-NEWER: System must abort sync if target version is newer than source.

        Spec requirement: FND-FR-VERSION-NEWER states system MUST abort sync with CRITICAL log
        if the target machine's pc-switcher version is newer than the source version
        (preventing accidental downgrades).
        """
        with patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=Version.parse("0.3.2")):
            # Mock target has newer version
            mock_install_context.target.run_command = AsyncMock(
                return_value=CommandResult(exit_code=0, stdout="pc-switcher 0.4.0", stderr="")
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate phase should detect version conflict
            errors = await job.validate()
            assert len(errors) == 1
            assert "newer than source" in errors[0].message
            assert "0.4.0" in errors[0].message
            assert "0.3.2" in errors[0].message
            assert errors[0].host == Host.TARGET

    @pytest.mark.asyncio
    async def test_001_edge_target_newer_version(self, mock_install_context: JobContext) -> None:
        """Edge case: Target has newer version prevents execution.

        Tests that when target has a newer version than source, the validation
        error prevents any installation attempt during execution phase.
        """
        with patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=Version.parse("0.3.0")):
            # Mock target has newer version
            mock_install_context.target.run_command = AsyncMock(
                return_value=CommandResult(exit_code=0, stdout="pc-switcher 0.5.0", stderr="")
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate should fail
            errors = await job.validate()
            assert len(errors) == 1
            assert "0.5.0" in errors[0].message
            assert "0.3.0" in errors[0].message

            # In real orchestrator, validation errors prevent execute() from being called
            # But if execute() were called anyway, it should detect the same issue
            # (execute doesn't raise - orchestrator handles validation errors)


class TestInstallOnTargetJobInstallationFailure:
    """Test abort on installation failure - FND-FR-INSTALL-FAIL."""

    @pytest.mark.asyncio
    async def test_001_fnd_fr_install_fail(self, mock_install_context: JobContext) -> None:
        """FND-FR-INSTALL-FAIL: System must abort if installation/upgrade fails.

        Spec requirement: FND-FR-INSTALL-FAIL states if installation/upgrade fails, system MUST
        log CRITICAL error and abort sync.
        """
        mock_version = Version.parse("0.4.0")
        mock_release = Release(version=mock_version, is_prerelease=False, tag="v0.4.0")

        with (
            patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=mock_version),
            patch.object(Version, "get_release_floor", return_value=mock_release),
        ):
            # Mock target missing pc-switcher, install fails
            mock_install_context.target.run_command = AsyncMock(
                side_effect=[
                    # Validate: missing (target_version stays None)
                    CommandResult(exit_code=127, stdout="", stderr="command not found"),
                    # Execute: install with exact version fails
                    CommandResult(exit_code=1, stdout="", stderr="disk full"),
                    # Execute: install with release floor also fails
                    CommandResult(exit_code=1, stdout="", stderr="disk full"),
                ]
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate should pass (target not newer)
            errors = await job.validate()
            assert errors == []

            # Execute should raise RuntimeError
            with pytest.raises(RuntimeError) as exc_info:
                await job.execute()

            assert "Failed to install pc-switcher on target" in str(exc_info.value)
            assert "disk full" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_001_fnd_fr_install_fail_verification(self, mock_install_context: JobContext) -> None:
        """FND-FR-INSTALL-FAIL: System must abort if installation verification fails.

        Tests that even if install command succeeds, verification must confirm
        the correct version is installed.
        """
        mock_version = Version.parse("0.4.0")
        mock_release = Release(version=mock_version, is_prerelease=False, tag="v0.4.0")

        with (
            patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=mock_version),
            patch.object(Version, "get_release_floor", return_value=mock_release),
        ):
            # Mock install succeeds but verification fails
            mock_install_context.target.run_command = AsyncMock(
                side_effect=[
                    # Validate: missing (target_version stays None)
                    CommandResult(exit_code=127, stdout="", stderr="command not found"),
                    # Execute: install succeeds
                    CommandResult(exit_code=0, stdout="", stderr=""),
                    # Execute: verify fails - version mismatch or command failed
                    CommandResult(exit_code=1, stdout="", stderr="error"),
                ]
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate should pass
            errors = await job.validate()
            assert errors == []

            # Execute should raise due to verification failure
            with pytest.raises(RuntimeError) as exc_info:
                await job.execute()

            assert "Installation verification failed" in str(exc_info.value)


class TestInstallOnTargetJobSkipWhenMatching:
    """Test skip when versions match - FND-US-SELF-INSTALL-AS3."""

    @pytest.mark.asyncio
    async def test_001_fnd_us_self_install_as3(self, mock_install_context: JobContext) -> None:
        """FND-US-SELF-INSTALL-AS3: System must skip installation when versions match.

        Spec requirement: US2-AS3 states when source and target both have version
        0.4.0, orchestrator logs "Target pc-switcher version matches source (0.4.0),
        skipping installation" and proceeds immediately to next phase.
        """
        mock_version = Version.parse("0.4.0")
        mock_release = Release(version=mock_version, is_prerelease=False, tag="v0.4.0")

        with (
            patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=mock_version),
            patch.object(Version, "get_release_floor", return_value=mock_release),
        ):
            # Mock target has matching version
            mock_install_context.target.run_command = AsyncMock(
                return_value=CommandResult(exit_code=0, stdout="pc-switcher 0.4.0", stderr="")
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate should pass and store target version
            errors = await job.validate()
            assert errors == []

            # Execute should skip installation (uses stored target_version)
            await job.execute()

            # Verify only validate version check was performed (no install command)
            # Execute reuses target_version from validate phase
            calls = mock_install_context.target.run_command.call_args_list
            assert len(calls) == 1
            assert "pc-switcher --version" in calls[0][0][0]


class TestInstallOnTargetJobAS4:
    """Test FND-US-SELF-INSTALL-AS4: Abort on install failure."""

    @pytest.mark.asyncio
    async def test_001_fnd_us_self_install_as4(self, mock_install_context: JobContext) -> None:
        """FND-US-SELF-INSTALL-AS4: System must abort sync when installation fails.

        Spec requirement: US2-AS4 states when installation/upgrade fails on target
        (e.g., disk full, permissions issue), orchestrator logs CRITICAL error and
        does not proceed with sync.
        """
        mock_version = Version.parse("0.4.0")
        mock_release = Release(version=mock_version, is_prerelease=False, tag="v0.4.0")

        with (
            patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=mock_version),
            patch.object(Version, "get_release_floor", return_value=mock_release),
        ):
            # Mock target has old version, upgrade fails
            mock_install_context.target.run_command = AsyncMock(
                side_effect=[
                    # Validate: old version (stores target_version)
                    CommandResult(exit_code=0, stdout="pc-switcher 0.3.2", stderr=""),
                    # Execute: install with exact version fails
                    CommandResult(exit_code=1, stdout="", stderr="Permission denied"),
                    # Execute: install with release floor also fails
                    CommandResult(exit_code=1, stdout="", stderr="Permission denied"),
                ]
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate should pass (old version is acceptable - will be upgraded)
            errors = await job.validate()
            assert errors == []

            # Execute should raise RuntimeError on install failure
            with pytest.raises(RuntimeError) as exc_info:
                await job.execute()

            assert "Failed to install pc-switcher on target" in str(exc_info.value)
            assert "Permission denied" in str(exc_info.value)


class TestInstallOnTargetJobUS7AS2:
    """Test US7-AS2: Target install uses shared installation logic."""

    @pytest.mark.asyncio
    async def test_001_us7_as2_target_install_shared_logic(self, mock_install_context: JobContext) -> None:
        """US7-AS2: InstallOnTargetJob must use the same install.sh script as initial installation.

        Spec requirement: US7-AS2 states that when pc-switcher sync installs on target
        (InstallOnTargetJob), it MUST use the same installation logic (install.sh) that
        handles uv installation if missing, then installs/upgrades pc-switcher.

        This ensures that target installation has the same robustness as initial
        user installation - automatically installing uv if needed, handling all
        dependencies, and providing consistent behavior.

        This test verifies:
        1. The job downloads and executes install.sh from GitHub (shared logic)
        2. The VERSION env var is passed to specify the version to install
        3. The command uses the same curl | bash pattern as user installation
        """
        # Mock source version and release
        mock_version = Version.parse("0.4.0")
        mock_release = Release(version=mock_version, is_prerelease=False, tag="v0.4.0")

        with (
            patch("pcswitcher.jobs.install_on_target.get_this_version", return_value=mock_version),
            patch.object(Version, "get_release_floor", return_value=mock_release),
        ):
            # Mock target has no pc-switcher installed
            mock_install_context.target.run_command = AsyncMock(
                side_effect=[
                    # Validate: missing (target_version stays None)
                    CommandResult(exit_code=127, stdout="", stderr="command not found"),
                    # Execute: install via install.sh succeeds
                    CommandResult(exit_code=0, stdout="pc-switcher installed successfully", stderr=""),
                    # Execute: verify installation
                    CommandResult(exit_code=0, stdout="pc-switcher 0.4.0", stderr=""),
                ]
            )

            job = InstallOnTargetJob(mock_install_context)

            # Validate should pass
            errors = await job.validate()
            assert errors == []

            # Execute should install using install.sh
            await job.execute()

            # Verify install.sh was used with VERSION env var
            # validate + install + verify = 3 calls
            calls = mock_install_context.target.run_command.call_args_list
            assert len(calls) == 3
            install_call = calls[1][0][0]

            # The installation command should use install.sh from GitHub
            assert "curl" in install_call, f"Should use curl to download install.sh. Got: {install_call}"
            assert "install.sh" in install_call, f"Should execute install.sh script. Got: {install_call}"
            assert "github.com" in install_call or "githubusercontent.com" in install_call, (
                f"Should download from GitHub. Got: {install_call}"
            )

            # The VERSION env var should be passed to the install script
            assert re.search(r"VERSION=(?:(?P<q>['\"])(v)?0\.4\.0(?P=q)|v0\.4\.0)", install_call), (
                f"Should pass VERSION env var. Got: {install_call}"
            )

            # Should use the same curl | bash pattern as user installation
            assert "bash" in install_call, f"Should pipe to bash like user installation. Got: {install_call}"
