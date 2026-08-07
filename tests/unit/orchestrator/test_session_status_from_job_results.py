"""Unit tests for deriving session status and CLI exit code from job results.

`PackageItemFailures` is caught per-job and recorded as a FAILED `JobResult` without
re-raising, so one package manager's item failures cannot cancel another manager's
already-approved work (`PKG-FR-OUTCOME-FAILED`). That makes "no exception propagated" a strictly weaker
condition than "the sync was clean", so the session status and the CLI exit code must
be derived from `job_results` rather than from the absence of an exception.

Without these tests the regression is silent: a sync where every item failed still
exits 0 and reads as a success.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from pcswitcher.cli import _async_run_sync
from pcswitcher.config import Configuration
from pcswitcher.models import (
    JobResult,
    JobStatus,
    SessionStatus,
    SyncSession,
)
from pcswitcher.orchestrator import _summarize_job_outcomes


def _job_result(job_name: str, status: JobStatus, error_message: str | None = None) -> JobResult:
    return JobResult(
        job_name=job_name,
        status=status,
        started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
        ended_at=datetime(2025, 1, 15, 10, 1, 0, tzinfo=UTC),
        error_message=error_message,
    )


def _session(status: SessionStatus, error_message: str | None = None) -> SyncSession:
    return SyncSession(
        session_id="deadbeef",
        started_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=UTC),
        source_hostname="pc1",
        target_hostname="pc2",
        config={},
        status=status,
        error_message=error_message,
    )


class TestSessionStatusReflectsJobResults:
    """A run that finished without raising is not automatically a successful run."""

    def test_failed_job_result_marks_session_failed(self) -> None:
        """A FAILED JobResult among successes makes the whole session FAILED."""
        status, error_message = _summarize_job_outcomes(
            [
                _job_result("apt_sync", JobStatus.SUCCESS),
                _job_result("snap_sync", JobStatus.FAILED),
                _job_result("folder_sync", JobStatus.SUCCESS),
            ]
        )

        assert status is SessionStatus.FAILED
        assert error_message is not None
        assert "snap_sync" in error_message

    def test_every_failed_job_is_named(self) -> None:
        """J24 — the message names each failing job, not just the first one."""
        status, error_message = _summarize_job_outcomes(
            [
                _job_result("apt_sync", JobStatus.FAILED),
                _job_result("flatpak_sync", JobStatus.FAILED),
            ]
        )

        assert status is SessionStatus.FAILED
        assert error_message is not None
        assert "apt_sync" in error_message
        assert "flatpak_sync" in error_message

    def test_skipped_job_result_is_not_a_failure(self) -> None:
        """J16 — SKIPPED is a normal outcome for a disabled or not-applicable job."""
        status, error_message = _summarize_job_outcomes(
            [
                _job_result("apt_sync", JobStatus.SUCCESS),
                _job_result("flatpak_sync", JobStatus.SKIPPED),
            ]
        )

        assert status is SessionStatus.COMPLETED
        assert error_message is None

    def test_no_jobs_is_a_clean_run(self) -> None:
        """A run with every job disabled is clean, not failed."""
        status, error_message = _summarize_job_outcomes([])

        assert status is SessionStatus.COMPLETED
        assert error_message is None


class TestTheOutcomeMessageNamesWhatFailed:
    """`PKG-FR-OUTCOME-FAILED` / `PKG-FR-FAIL-NAMED`: the end-of-run message is what the
    user reads once the review screens are gone, so the names have to survive into it.
    """

    def test_each_failed_jobs_reason_reaches_the_message(self) -> None:
        """J24 — each failed job's own reason survives into the end-of-run message."""
        _status, error_message = _summarize_job_outcomes(
            [
                _job_result(
                    "apt_sync",
                    JobStatus.FAILED,
                    "2 apt item(s) failed to converge: ripgrep 14.1.0-1, fd-find 9.0.0-1",
                ),
                _job_result("snap_sync", JobStatus.FAILED, "probe did not answer — `snap list --all` exited 1"),
            ]
        )

        assert error_message is not None
        assert "ripgrep 14.1.0-1" in error_message
        assert "fd-find 9.0.0-1" in error_message
        assert "`snap list --all` exited 1" in error_message

    def test_a_job_with_many_failed_items_stays_on_one_line(self) -> None:
        """J23 — volume rule: one line per failed job, however many items it names."""
        names = ", ".join(f"pkg-{n}" for n in range(40))
        _status, error_message = _summarize_job_outcomes(
            [
                _job_result("apt_sync", JobStatus.FAILED, f"40 apt item(s) failed to converge: {names}"),
                _job_result("flatpak_sync", JobStatus.FAILED, "1 flatpak item(s) failed to converge: org.gimp.GIMP"),
            ]
        )

        assert error_message is not None
        assert len(error_message.splitlines()) == 2

    def test_a_failure_without_a_recorded_reason_still_names_its_job(self) -> None:
        """J25 — a failure with no recorded reason still names the job it came from."""
        _status, error_message = _summarize_job_outcomes([_job_result("folder_sync", JobStatus.FAILED)])

        assert error_message is not None
        assert error_message.startswith("folder_sync")


class TestCliExitCodeFromSessionStatus:
    """The CLI exit code comes from the session status, not from "nothing raised"."""

    @pytest.mark.asyncio
    async def test_failed_session_exits_non_zero(self) -> None:
        """J32 — a FAILED session must not exit 0, or a broken sync reads as a success."""
        session = _session(SessionStatus.FAILED, error_message="snap_sync — 1 snap item(s) failed to converge: bw")

        with patch("pcswitcher.cli.Orchestrator") as orchestrator_cls:
            orchestrator_cls.return_value.run = AsyncMock(return_value=session)
            exit_code = await _async_run_sync("pc2", Configuration())

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_completed_session_exits_zero(self) -> None:
        """A clean run still exits 0 — the guard must not fire on success."""
        session = _session(SessionStatus.COMPLETED)

        with patch("pcswitcher.cli.Orchestrator") as orchestrator_cls:
            orchestrator_cls.return_value.run = AsyncMock(return_value=session)
            exit_code = await _async_run_sync("pc2", Configuration())

        assert exit_code == 0

    @pytest.mark.asyncio
    async def test_cancelled_run_still_exits_130(self) -> None:
        """Interrupt handling is unchanged by the status-derived exit code."""

        async def _cancel() -> SyncSession:
            raise asyncio.CancelledError

        with patch("pcswitcher.cli.Orchestrator") as orchestrator_cls:
            orchestrator_cls.return_value.run = AsyncMock(side_effect=_cancel)
            exit_code = await _async_run_sync("pc2", Configuration())

        assert exit_code == 130
