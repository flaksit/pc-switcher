"""Unit tests for AptSyncJob and the shared PackageSyncJob plan()/apply() split.

Covers the tracer's single path — one apt package missing on the target — through
capture, diff, plan/apply separation, the coordinator-accepted-plan ordering guard,
converge (with the apt-get -s transaction guard), dry-run, continue-on-failure, and
validate(). All executor interactions are mocked; no real apt/dpkg/sudo commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.package_items import AptPackageItem, DiffClass
from pcswitcher.jobs.package_review import Decision, ReviewOutcome
from pcswitcher.jobs.package_sync_core import PackageItemFailures
from pcswitcher.models import CommandResult
from pcswitcher.orchestrator import Orchestrator

SHOWMANUAL_3 = "pkg-a\npkg-b\npkg-c\n"
DPKG_QUERY_3 = "pkg-a\t1.0\npkg-b\t2.0\npkg-c\t3.0\n"


def respond_to(
    mapping: dict[str, CommandResult], default: CommandResult | None = None
) -> Callable[..., CommandResult]:
    """Build a run_command side_effect matching by substring (first match wins)."""
    fallback = default if default is not None else CommandResult(exit_code=0, stdout="", stderr="")

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


def make_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target = MagicMock()
    target.run_command = AsyncMock(side_effect=respond_to(target_responses or {}))
    context = JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
    )
    return context, source, target


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


class TestCapture:
    """Capture: apt-mark showmanual + one batched dpkg-query call for versions (D-03)."""

    @pytest.mark.asyncio
    async def test_capture_source_items_returns_three_items_with_versions(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            }
        )
        job = AptSyncJob(context)

        items = await job.capture_source_items()

        assert [item.name for item in items] == ["pkg-a", "pkg-b", "pkg-c"]
        assert [item.version for item in items] == ["1.0", "2.0", "3.0"]

    @pytest.mark.asyncio
    async def test_dpkg_query_used_not_apt_list_installed(self) -> None:
        """Backstop: versions come from dpkg-query, never `apt list --installed`."""
        context, source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            }
        )
        job = AptSyncJob(context)

        await job.capture_source_items()

        commands = all_calls(source)
        assert any("dpkg-query" in cmd for cmd in commands)
        assert not any("apt list" in cmd for cmd in commands)


class TestDiff:
    """Target query + diff: only MISSING_ON_TARGET/INSTALL is produced by this slice."""

    @pytest.mark.asyncio
    async def test_diff_yields_exactly_two_missing_items(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-b\t2.0\n", ""),
            },
        )
        job = AptSyncJob(context)

        source_items = await job.capture_source_items()
        target_items = await job.query_target_items()
        diffs = job.diff_items(source_items, target_items)

        assert len(diffs) == 2
        assert {d.item_id for d in diffs} == {"apt:package:pkg-a", "apt:package:pkg-c"}
        assert all(d.diff_class == DiffClass.MISSING_ON_TARGET for d in diffs)

    def test_diff_is_symmetric_blind_extra_on_target_produces_no_item(self) -> None:
        """A name on the target but not the source produces no item (this slice)."""
        source_items = [AptPackageItem(name="pkg-a", version="1.0")]
        target_items = [
            AptPackageItem(name="pkg-a", version="1.0"),
            AptPackageItem(name="pkg-extra", version="9.9"),
        ]
        context, _source, _target = make_context()
        job = AptSyncJob(context)

        diffs = job.diff_items(source_items, target_items)

        assert diffs == ()


class TestPlanApplySplit:
    """plan() issues only read commands; execute() refuses without an accepted plan."""

    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_command(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 3
        for cmd in all_calls(target):
            assert "apt-get install" not in cmd
            assert "apt-get -s" not in cmd
            assert "sudo" not in cmd

    @pytest.mark.asyncio
    async def test_execute_without_accepted_plan_raises_naming_coordinator(self) -> None:
        context, _source, _target = make_context()
        job = AptSyncJob(context)

        with pytest.raises(RuntimeError, match="PackagePhaseCoordinator"):
            await job.execute()

    @pytest.mark.asyncio
    async def test_execute_reraises_stored_plan_failure(self) -> None:
        context, _source, _target = make_context(
            source_responses={"apt-mark showmanual": CommandResult(1, "", "boom")}
        )
        job = AptSyncJob(context)
        failure = RuntimeError("plan blew up")
        job.record_plan_failure(failure)

        with pytest.raises(RuntimeError, match="plan blew up"):
            await job.execute()


def _accept(job: AptSyncJob, plan: Any, decisions: dict[str, Decision]) -> None:
    job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))


class TestConverge:
    """Only APPLY-decided items reach the target; SKIP_ONCE items reach no command."""

    @pytest.mark.asyncio
    async def test_only_apply_decision_installs_skip_once_never_sent(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t2.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.SKIP_ONCE})

        await job.execute()

        commands = all_calls(target)
        assert any("apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)
        assert not any("pkg-b" in cmd for cmd in commands)


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_issues_no_mutating_command(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={"apt-mark showmanual": CommandResult(0, "", "")},
            dry_run=True,
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        for cmd in all_calls(target):
            assert "apt-get install" not in cmd
            assert "apt-get -s" not in cmd


class TestContinueOnFailure:
    @pytest.mark.asyncio
    async def test_second_of_three_fails_all_attempted_one_failure_raised(self) -> None:
        clean_preview = CommandResult(0, "Inst dummy (1.0)\n", "")
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, SHOWMANUAL_3, ""),
                "dpkg-query": CommandResult(0, DPKG_QUERY_3, ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "dpkg-query": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": clean_preview,
                "apt-get -s install -y --no-install-recommends pkg-b": clean_preview,
                "apt-get -s install -y --no-install-recommends pkg-c": clean_preview,
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-b": (
                    CommandResult(1, "", "dpkg error for pkg-b")
                ),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-c": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(
            job,
            plan,
            {
                "apt:package:pkg-a": Decision.APPLY,
                "apt:package:pkg-b": Decision.APPLY,
                "apt:package:pkg-c": Decision.APPLY,
            },
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        assert exc_info.value.failures[0][0].item_id == "apt:package:pkg-b"

        commands = all_calls(target)
        real_installs = [c for c in commands if "sudo" in c and "apt-get install" in c]
        assert len(real_installs) == 3
        simulations = [c for c in commands if "apt-get -s" in c]
        assert len(simulations) == 3


class TestTransactionGuard:
    @pytest.mark.asyncio
    async def test_collateral_removal_refuses_install_and_names_the_package(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(
                    0, "Inst pkg-a (1.0)\nRemv other-pkg [1.0]\n", ""
                ),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert len(exc_info.value.failures) == 1
        _diff, message = exc_info.value.failures[0]
        assert "other-pkg" in message

        commands = all_calls(target)
        assert not any("sudo" in cmd and "apt-get install" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_clean_simulation_proceeds_to_real_install(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-get -s install -y --no-install-recommends pkg-a": CommandResult(0, "Inst pkg-a (1.0)\n", ""),
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends pkg-a": (
                    CommandResult(0, "", "")
                ),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        _accept(job, plan, {"apt:package:pkg-a": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert any("sudo" in cmd and "apt-get install" in cmd and "pkg-a" in cmd for cmd in commands)


class TestValidate:
    @pytest.mark.asyncio
    async def test_all_checks_pass_returns_no_errors(self) -> None:
        # fuser exits 1 (not 0) when the lock file is NOT held (man fuser EXIT CODES) —
        # the "all clear" baseline, unlike every other check here where 0 means success.
        context, _source, _target = make_context(
            target_responses={"fuser /var/lib/dpkg/lock-frontend": CommandResult(1, "", "")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert errors == []

    @pytest.mark.asyncio
    async def test_apt_mark_unavailable_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            target_responses={"apt-mark --version": CommandResult(127, "", "not found")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert any("apt-mark" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_dpkg_lock_held_yields_distinct_validation_error(self) -> None:
        context, _source, _target = make_context(
            target_responses={"fuser /var/lib/dpkg/lock-frontend": CommandResult(0, "1234", "")}
        )
        job = AptSyncJob(context)

        errors = await job.validate()

        assert any("lock" in e.message.lower() for e in errors)


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_apt_sync_to_apt_sync_job(self) -> None:
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("apt_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is AptSyncJob
