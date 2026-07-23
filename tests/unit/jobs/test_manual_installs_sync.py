"""Unit tests for `ManualInstallsSyncJob` (plan 02-17): the fourth package job owning
unreproducible detection (D-18/D-19), snippet replay (D-20), and the D-21 skip-once
resolution semantics.

All executor interactions are mocked; no real dpkg/apt-cache/sudo commands run. Detection
and snippet-replay coverage that previously lived against `AptSyncJob` in
`test_package_state.py`/`test_apt_sync.py` moved here when the ownership moved (D-18).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.manual_installs_sync import ManualInstallsSyncJob
from pcswitcher.jobs.package_items import DiffAction, DiffClass, ItemClass, ItemDiff, UnreproducibleItem
from pcswitcher.jobs.package_review import (
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.package_sync_core import PackageItemFailures
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator

# A `package-snippets.yaml` registry holding one snippet for the brscan3 no-candidate item.
BRSCAN3_REGISTRY_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 (no apt candidate)\n"
    "    body: sudo dpkg -i /tmp/brscan3.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: laptop\n"
)


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
    reviewer: object | None = None,
    enabled_sync_jobs: dict[str, bool] | None = None,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target = MagicMock()
    target.run_command = AsyncMock(side_effect=respond_to(target_responses or {}))
    target.send_file = AsyncMock(return_value=None)
    context = JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
        reviewer=reviewer,  # pyright: ignore[reportArgumentType]
        enabled_sync_jobs=enabled_sync_jobs,
    )
    return context, source, target


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


def job_diff(item_id: str, action: DiffAction) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.UNREPRODUCIBLE,
        diff_class=DiffClass.UNREPRODUCIBLE,
        action=action,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


class FakeReviewer:
    """A `Reviewer` returning a caller-supplied outcome, recording the groups it saw."""

    def __init__(
        self,
        *,
        decisions: dict[str, Decision] | None = None,
        snippets: dict[str, str] | None = None,
        unresolved: tuple[str, ...] = (),
        was_interactive: bool = True,
    ) -> None:
        self._decisions = decisions or {}
        self._snippets = snippets or {}
        self._unresolved = unresolved
        self._was_interactive = was_interactive
        self.groups_seen: tuple[ReviewGroup, ...] | None = None

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self.groups_seen = tuple(groups)
        item_ids = {entry.item_id for group in groups for entry in group.entries}
        decisions = {item_id: self._decisions.get(item_id, Decision.SKIP_ONCE) for item_id in item_ids}
        return ReviewOutcome(
            decisions=decisions,
            was_interactive=self._was_interactive,
            snippets=self._snippets,
            unresolved=self._unresolved,
        )


class TestNoCandidateDetection:
    """apt-no-candidate scan: a manually-installed package the SOURCE's own apt-cache
    cannot reinstall becomes an UNREPRODUCIBLE diff (D-18)."""

    @pytest.mark.asyncio
    async def test_no_candidate_source_package_becomes_unreproducible_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(
                    0, "brscan3:\n  Installed: 1.0\n  Candidate: (none)\n  Version table:\n", ""
                ),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        unreproducible = [d for d in plan.diffs if d.item_class == ItemClass.UNREPRODUCIBLE]
        assert len(unreproducible) == 1
        assert unreproducible[0].item_id == "unreproducible:apt-no-candidate:brscan3"
        assert unreproducible[0].diff_class == DiffClass.UNREPRODUCIBLE
        assert unreproducible[0].action == DiffAction.REPORT_ONLY


class TestUnownedScan:
    """Unowned-install scan (moved from test_package_state.py when D-18 moved ownership)."""

    @pytest.mark.asyncio
    async def test_scan_unowned_installs_yields_two_items_from_four_candidates(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "find /usr/local": CommandResult(
                    0,
                    "/usr/local/flux\n/usr/local/bin/talosctl\n/usr/local/bin/kubectl-cnpg\n/opt/az\n",
                    "",
                ),
                "dpkg -S": CommandResult(0, "cnpg: /usr/local/bin/kubectl-cnpg\nazure-cli: /opt/az\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        assert {item.identifier for item in items} == {"/usr/local/flux", "/usr/local/bin/talosctl"}
        assert all(item.origin == "unowned-path" for item in items)
        assert all(isinstance(item, UnreproducibleItem) for item in items)

    @pytest.mark.asyncio
    async def test_unowned_scan_queries_only_usr_local_and_opt(self) -> None:
        context, source, _target = make_context()
        job = ManualInstallsSyncJob(context)

        await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        find_calls = [c.args[0] for c in source.run_command.call_args_list if c.args[0].startswith("find ")]
        assert len(find_calls) == 1
        assert (
            find_calls[0] == "find /usr/local /opt /usr/local/bin /usr/local/lib -mindepth 1 -maxdepth 1 2>/dev/null"
        )


class TestSnippetResolution:
    """A registry snippet makes an item reproducible: INSTALL + replay; without one it is
    REPORT_ONLY and carved into its own resolution group (D-20/D-21)."""

    @pytest.mark.asyncio
    async def test_item_with_snippet_plans_install_and_converges_by_replaying_it(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg -i /tmp/brscan3.deb'": CommandResult(0, "brscan3 installed\n", ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        diff = next(d for d in plan.diffs if d.item_id == item_id)
        assert diff.action == DiffAction.INSTALL

        result = await job.converge(diff)

        assert result.success
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert "dpkg -i /tmp/brscan3.deb" in replay_calls[0]

    @pytest.mark.asyncio
    async def test_item_without_snippet_is_report_only_and_grouped_separately(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        item_id = "unreproducible:apt-no-candidate:brscan3"
        diff = next(d for d in plan.diffs if d.item_id == item_id)
        assert diff.action == DiffAction.REPORT_ONLY

        resolution_group = next(g for g in plan.groups if g.action == UNREPRODUCIBLE_REVIEW_ACTION)
        assert {e.item_id for e in resolution_group.entries} == {item_id}
        for group in plan.groups:
            if group.action != UNREPRODUCIBLE_REVIEW_ACTION:
                assert item_id not in {e.item_id for e in group.entries}

    @pytest.mark.asyncio
    async def test_missing_snippet_at_converge_is_a_failed_result_not_a_crash(self) -> None:
        """A snippet-backed diff whose snippet vanished between plan and converge (a
        registry race) fails as one item (D-27), never raises."""
        context, _source, _target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)
        diff = job_diff("unreproducible:apt-no-candidate:gone", DiffAction.INSTALL)

        result = await job.converge(diff)

        assert result.success is False


class TestInertFiltering:
    """An item recorded machine-specific on the source produces no diff (D-08/D-19)."""

    @pytest.mark.asyncio
    async def test_machine_specific_item_is_filtered_before_becoming_a_diff(self) -> None:
        decisions_yaml = (
            "machine_specific:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    item_class: unreproducible\n"
            "    label: brscan3 (no apt candidate)\n"
            "    reason: null\n"
            "    recorded_at: '2026-01-01T00:00:00+00:00'\n"
        )
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
                "cat ~/.config/pc-switcher/manual.decisions.yaml": CommandResult(0, decisions_yaml, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()


class TestEmptyDetection:
    @pytest.mark.asyncio
    async def test_empty_detection_produces_no_group_and_applies_nothing(self) -> None:
        """Backstop (must_haves): an empty unreproducible set yields no review group and
        nothing to apply."""
        context, _source, _target = make_context(source_responses={"apt-mark showmanual": CommandResult(0, "\n", "")})
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert plan.groups == ()

        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True))
        await job.apply()  # must not raise


class TestExecuteIndependentOfApt:
    """The job runs on its own enable flag, independent of apt_sync (D-15/D-18)."""

    @pytest.mark.asyncio
    async def test_plan_runs_with_apt_absent_from_config_and_manual_enabled(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
            },
            enabled_sync_jobs={"manual_installs_sync": True, "folder_sync": True},
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == ["unreproducible:apt-no-candidate:brscan3"]

    @pytest.mark.asyncio
    async def test_execute_runs_plan_review_apply_through_injected_reviewer(self) -> None:
        item_id = "unreproducible:apt-no-candidate:brscan3"
        reviewer = FakeReviewer(decisions={item_id: Decision.APPLY})
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg -i /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        await job.execute()

        assert reviewer.groups_seen is not None
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1


class TestTracerEndToEnd:
    """The tracer's single path: detect one no-candidate item and one unowned item, plan,
    assert the review groups, then converge the snippet-backed item against the target."""

    @pytest.mark.asyncio
    async def test_detect_plan_and_replay_end_to_end(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
                "find /usr/local": CommandResult(0, "/usr/local/flux\n/opt/az\n", ""),
                "dpkg -S": CommandResult(0, "azure-cli: /opt/az\n", ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg -i /tmp/brscan3.deb'": CommandResult(0, "brscan3 installed\n", ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        # brscan3 has a snippet -> INSTALL (resolved); the unowned flux path has none -> REPORT_ONLY.
        assert by_id["unreproducible:apt-no-candidate:brscan3"].action == DiffAction.INSTALL
        assert by_id["unreproducible:unowned-path:/usr/local/flux"].action == DiffAction.REPORT_ONLY

        install_group = next(g for g in plan.groups if g.action == DiffAction.INSTALL.value)
        assert "unreproducible:apt-no-candidate:brscan3" in {e.item_id for e in install_group.entries}
        resolution_group = next(g for g in plan.groups if g.action == UNREPRODUCIBLE_REVIEW_ACTION)
        assert {e.item_id for e in resolution_group.entries} == {"unreproducible:unowned-path:/usr/local/flux"}

        result = await job.converge(by_id["unreproducible:apt-no-candidate:brscan3"])
        assert result.success
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert "/tmp/brscan3.deb" in replay_calls[0]


class TestSkipOnceResolution:
    """D-21: skip-once is a valid resolution — a run whose only items were skipped-once
    is clean; a genuinely undecided item still fails an interactive run."""

    @pytest.mark.asyncio
    async def test_run_whose_only_items_were_skipped_once_passes(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        # Explicit skip-once: a resolution, NOT in unresolved (D-21).
        job.accept_review(
            plan,
            ReviewOutcome(decisions={item_id: Decision.SKIP_ONCE}, was_interactive=True, unresolved=()),
        )

        await job.apply()  # must not raise

    @pytest.mark.asyncio
    async def test_genuinely_undecided_item_fails_the_run(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        # Cancelled/abandoned in review: genuinely unresolved (D-21/D-27).
        job.accept_review(
            plan,
            ReviewOutcome(decisions={item_id: Decision.SKIP_ONCE}, was_interactive=True, unresolved=(item_id,)),
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        assert {d.item_id for d, _stderr in exc_info.value.failures} == {item_id}


class TestContinueOnFailure:
    @pytest.mark.asyncio
    async def test_failed_snippet_replay_is_a_per_item_failure_and_does_not_stop_the_job(self) -> None:
        registry_yaml = (
            "snippets:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    label: brscan3 (no apt candidate)\n"
            "    body: sudo dpkg -i /tmp/brscan3.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
            "  unreproducible:apt-no-candidate:cnpg:\n"
            "    label: cnpg (no apt candidate)\n"
            "    body: sudo dpkg -i /tmp/cnpg.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\ncnpg\n", ""),
                "apt-cache policy": CommandResult(
                    0, "brscan3:\n  Candidate: (none)\ncnpg:\n  Candidate: (none)\n", ""
                ),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                "bash -c 'sudo dpkg -i /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
                "bash -c 'sudo dpkg -i /tmp/cnpg.deb'": CommandResult(1, "", "dpkg: error processing archive"),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        decisions = {
            "unreproducible:apt-no-candidate:brscan3": Decision.APPLY,
            "unreproducible:apt-no-candidate:cnpg": Decision.APPLY,
        }
        job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        failed_ids = {diff.item_id for diff, _stderr in exc_info.value.failures}
        assert failed_ids == {"unreproducible:apt-no-candidate:cnpg"}


class TestValidate:
    @pytest.mark.asyncio
    async def test_apt_cache_unavailable_on_source_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            source_responses={"apt-cache --version": CommandResult(127, "", "not found")}
        )
        job = ManualInstallsSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "apt-cache" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_dpkg_unavailable_on_source_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            source_responses={"dpkg --version": CommandResult(127, "", "not found")}
        )
        job = ManualInstallsSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "dpkg" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors(self) -> None:
        context, _source, _target = make_context()
        job = ManualInstallsSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_manual_installs_sync_to_its_job(self) -> None:
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("manual_installs_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is ManualInstallsSyncJob
