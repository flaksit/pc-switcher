"""The run ends by naming every job and how it ended (`CORE-FR-SUMMARY`).

Without this block a `JobResult` reaches the user only as an exit code: SKIPPED is
invisible and a failure is a sentence with no list. The reasons quote text pc-switcher did
not author, so they must render literally rather than as Rich markup.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

import pytest

from pcswitcher.jobs.base import SyncJob
from pcswitcher.models import JobResult, JobStatus, SyncAbortedByUser, ValidationError
from pcswitcher.orchestrator import Orchestrator
from tests.unit.console_capture import PlainBuffer, captured_console
from tests.unit.jobs.test_package_sync_core import make_context


def _result(job_name: str, status: JobStatus, reason: str | None = None) -> JobResult:
    now = datetime.now(UTC)
    return JobResult(job_name=job_name, status=status, started_at=now, ended_at=now, error_message=reason)


def _capture(orchestrator: Orchestrator) -> PlainBuffer:
    """Point `orchestrator` at a console whose output the caller can read back."""
    console, buffer = captured_console()
    orchestrator._console = console  # pyright: ignore[reportPrivateUsage]
    return buffer


def _printed_lines(buffer: PlainBuffer) -> list[str]:
    return [line.rstrip() for line in buffer.getvalue().splitlines() if line.strip()]


class _AbortingJob(SyncJob):
    name: ClassVar[str] = "stub_aborting"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        raise SyncAbortedByUser("declined at the review")


class TestTheBlockNamesEveryJob:
    def test_each_job_gets_one_line_with_its_status(self, wired_orchestrator: Orchestrator) -> None:
        buffer = _capture(wired_orchestrator)
        wired_orchestrator._job_results.extend(  # pyright: ignore[reportPrivateUsage]
            [
                _result("install_on_target", JobStatus.SUCCESS),
                _result("apt_sync", JobStatus.FAILED, "could not install vim on Nomad"),
                _result("folder_sync", JobStatus.SKIPPED, "no enabled folders configured"),
            ]
        )

        wired_orchestrator._print_job_summary()  # pyright: ignore[reportPrivateUsage]

        lines = _printed_lines(buffer)
        assert lines[0] == "Job outcomes:"
        assert [line.split()[1] for line in lines[1:]] == ["install_on_target", "apt_sync", "folder_sync"]
        assert "success" in lines[1]
        assert "failed" in lines[2]
        assert "skipped" in lines[3]

    def test_a_skipped_or_failed_line_carries_the_reason_that_job_recorded(
        self, wired_orchestrator: Orchestrator
    ) -> None:
        buffer = _capture(wired_orchestrator)
        wired_orchestrator._job_results.extend(  # pyright: ignore[reportPrivateUsage]
            [
                _result("snap_sync", JobStatus.SKIPPED, "non-interactive run left every snap review item undecided"),
                _result("apt_sync", JobStatus.FAILED, "could not install vim on Nomad: E: Unable to fetch archives"),
            ]
        )

        wired_orchestrator._print_job_summary()  # pyright: ignore[reportPrivateUsage]

        printed = buffer.getvalue()
        assert "non-interactive run left every snap review item undecided" in printed
        assert "could not install vim on Nomad: E: Unable to fetch archives" in printed

    def test_a_successful_job_shows_no_reason(self, wired_orchestrator: Orchestrator) -> None:
        buffer = _capture(wired_orchestrator)
        wired_orchestrator._job_results.append(  # pyright: ignore[reportPrivateUsage]
            _result("apt_sync", JobStatus.SUCCESS, "left over from an earlier phase")
        )

        wired_orchestrator._print_job_summary()  # pyright: ignore[reportPrivateUsage]

        assert "left over from an earlier phase" not in buffer.getvalue()

    @pytest.mark.parametrize(
        "reason",
        [
            "dpkg: error processing archive [/usr/bin/apt] (--unpack)",  # raises MarkupError as markup
            "E: Sub-process returned an error code [installed]",  # silently swallowed as markup
            "snap [core22/stable] is not available",
        ],
    )
    def test_a_reason_shaped_like_rich_markup_is_printed_literally(
        self, wired_orchestrator: Orchestrator, reason: str
    ) -> None:
        """A `[/usr/bin/apt]` used to raise MarkupError, crashing the run after all its work."""
        buffer = _capture(wired_orchestrator)
        wired_orchestrator._job_results.append(  # pyright: ignore[reportPrivateUsage]
            _result("apt_sync", JobStatus.FAILED, reason)
        )

        wired_orchestrator._print_job_summary()  # pyright: ignore[reportPrivateUsage]

        assert reason in buffer.getvalue()

    def test_a_run_with_no_job_results_prints_nothing(self, wired_orchestrator: Orchestrator) -> None:
        buffer = _capture(wired_orchestrator)

        wired_orchestrator._print_job_summary()  # pyright: ignore[reportPrivateUsage]

        assert buffer.getvalue() == ""


class TestTheBlockSurvivesAnUnfinishedRun:
    @pytest.mark.asyncio
    async def test_a_run_that_stopped_mid_loop_still_names_the_jobs_that_ran(
        self, wired_orchestrator: Orchestrator
    ) -> None:
        """An abort leaves no session to report from, so the results have to be the orchestrator's own."""
        buffer = _capture(wired_orchestrator)
        aborting = _AbortingJob(make_context())
        wired_orchestrator._job_results.append(  # pyright: ignore[reportPrivateUsage]
            _result("install_on_target", JobStatus.SUCCESS)
        )

        with pytest.raises(SyncAbortedByUser):
            await wired_orchestrator._execute_jobs([aborting])  # pyright: ignore[reportPrivateUsage]
        wired_orchestrator._print_job_summary()  # pyright: ignore[reportPrivateUsage]

        assert "install_on_target" in buffer.getvalue()
