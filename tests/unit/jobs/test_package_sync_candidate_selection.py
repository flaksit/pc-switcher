"""Unit tests for the pure output parsers backing the VM-level package-sync integration tests.

These functions have no I/O of their own -- the integration scenario module
(`tests/integration/jobs/package_sync_scenario.py`) wires them to real `apt-mark`/
`dpkg-query` output over SSH, but the parsing itself is ordinary Python and gets fast,
VM-independent coverage here.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime

from rich.console import Console

from pcswitcher.models import JobResult, JobStatus
from pcswitcher.orchestrator import Orchestrator
from tests.integration.jobs.package_sync_scenario import (
    job_outcome_statuses,
    nonblank_lines,
    parse_dpkg_installed,
    parse_systemctl_show_blocks,
)


def _rendered_block(results: list[JobResult], *, width: int = 80) -> str:
    """What `Orchestrator._print_job_summary` actually prints for `results`.

    Rendered through the orchestrator's own method rather than a hand-written fixture:
    the parser's whole job is to read that renderer's output, so a hand-written sample
    would keep passing after the renderer changed shape -- which is the one failure this
    parser exists to make impossible.
    """
    buffer = io.StringIO()
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._console = Console(file=buffer, width=width, force_terminal=True)  # pyright: ignore[reportPrivateUsage] - rendering the private block is the subject
    orchestrator._job_results = results  # pyright: ignore[reportPrivateUsage] - rendering the private block is the subject
    orchestrator._print_job_summary()  # pyright: ignore[reportPrivateUsage] - rendering the private block is the subject
    return buffer.getvalue()


def _result(job_name: str, status: JobStatus, reason: str | None = None) -> JobResult:
    """A `JobResult` for `job_name`; the timestamps are immaterial to the block."""
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    return JobResult(
        job_name=job_name,
        job_display_name=job_name,
        status=status,
        started_at=moment,
        ended_at=moment,
        error_message=reason,
    )


class TestNonblankLines:
    def test_strips_and_drops_blank_lines(self) -> None:
        assert nonblank_lines("a\n  b  \n\n c\n") == ["a", "b", "c"]

    def test_empty_input_yields_empty_list(self) -> None:
        assert nonblank_lines("") == []


class TestParseDpkgInstalled:
    def test_only_install_ok_installed_counts(self) -> None:
        output = "pkg-a\tinstall ok installed\npkg-b\tdeinstall ok config-files\npkg-c\tinstall ok installed\n"
        assert parse_dpkg_installed(output) == {"pkg-a", "pkg-c"}

    def test_blank_lines_ignored(self) -> None:
        assert parse_dpkg_installed("\n\npkg-a\tinstall ok installed\n") == {"pkg-a"}

    def test_half_installed_status_excluded(self) -> None:
        assert parse_dpkg_installed("pkg-a\thalf-installed ok half-installed\n") == set()


class TestJobOutcomeStatuses:
    """The parser reads the block `Orchestrator._print_job_summary` really prints."""

    def test_every_job_and_status_is_read_off_the_block(self) -> None:
        block = _rendered_block(
            [
                _result("install_on_target", JobStatus.SUCCESS),
                _result("apt_sync", JobStatus.SKIPPED, "non-interactive run left every apt review item undecided"),
                _result("snap_sync", JobStatus.FAILED, "one item failed"),
            ]
        )
        assert job_outcome_statuses(block) == {
            "install_on_target": "success",
            "apt_sync": "skipped",
            "snap_sync": "failed",
        }

    def test_a_reason_folded_across_lines_adds_no_job(self) -> None:
        """A reason too long for the terminal wraps under its own column, and the
        continuation carries neither glyph nor job name -- so it must not read as a row.
        """
        reason = " ".join(f"word{index}" for index in range(40))
        block = _rendered_block([_result("apt_sync", JobStatus.FAILED, reason)], width=40)
        assert len(block.strip().splitlines()) > 2, "the reason did not fold, so this asserts nothing"
        assert job_outcome_statuses(block) == {"apt_sync": "failed"}

    def test_a_reason_quoting_a_status_word_is_not_read_as_a_row(self) -> None:
        """Package-manager stderr may contain the word `failed`; only a glyph starts a row."""
        block = _rendered_block([_result("apt_sync", JobStatus.SKIPPED, "dpkg said: install failed")])
        assert job_outcome_statuses(block) == {"apt_sync": "skipped"}

    def test_output_before_the_header_is_ignored(self) -> None:
        block = _rendered_block([_result("apt_sync", JobStatus.SUCCESS)])
        noise = "  ✔ not_a_job success\nJob outcomes are printed at the end.\n"
        assert job_outcome_statuses(noise + block) == {"apt_sync": "success"}

    def test_a_run_that_printed_no_block_yields_nothing(self) -> None:
        assert job_outcome_statuses("sync complete\n") == {}


class TestParseSystemctlShowBlocks:
    """`systemctl show` emits one blank-line-separated block per unit asked, in order."""

    def test_each_unit_becomes_its_own_block(self) -> None:
        output = (
            "Id=apt-daily.timer\nLoadState=loaded\nActiveState=active\n"
            "\n"
            "Id=apt-daily-upgrade.timer\nLoadState=masked\nActiveState=inactive\n"
        )
        assert parse_systemctl_show_blocks(output) == {
            "apt-daily.timer": {"Id": "apt-daily.timer", "LoadState": "loaded", "ActiveState": "active"},
            "apt-daily-upgrade.timer": {
                "Id": "apt-daily-upgrade.timer",
                "LoadState": "masked",
                "ActiveState": "inactive",
            },
        }

    def test_a_unit_that_does_not_exist_still_gets_a_block(self) -> None:
        """systemd answers for a unit it has never heard of, which is how the product tells
        a cancelled restore timer from a pending one.
        """
        output = "Id=pc-switcher-apt-timers.timer\nLoadState=not-found\n"
        blocks = parse_systemctl_show_blocks(output)
        assert blocks["pc-switcher-apt-timers.timer"]["LoadState"] == "not-found"

    def test_a_value_containing_an_equals_sign_is_kept_whole(self) -> None:
        output = "Id=x.timer\nDescription=restart timers after 6h\nExecStart=/bin/sh -c a=b\n"
        assert parse_systemctl_show_blocks(output)["x.timer"]["ExecStart"] == "/bin/sh -c a=b"

    def test_a_block_with_no_id_is_dropped(self) -> None:
        assert parse_systemctl_show_blocks("LoadState=loaded\n") == {}
