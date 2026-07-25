"""Unit tests for `ManualInstallsSyncJob` (plan 02-17): the fourth package job owning
unreproducible detection (D-18/D-19), snippet replay (D-20), and the D-21 skip-once
resolution semantics.

All executor interactions are mocked; no real dpkg/apt-cache/sudo commands run. Detection
and snippet-replay coverage that previously lived against `AptSyncJob` in
`test_package_state.py`/`test_apt_sync.py` moved here when the ownership moved (D-18).
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.manual_installs_sync import ManualInstallsSyncJob
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff, UnreproducibleItem
from pcswitcher.jobs.packages.review import (
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.state import SNIPPET_REGISTRY_RELPATH
from pcswitcher.jobs.packages.sync_core import PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, Host, SyncAbortedByUser, ValidationError
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
    confirmer: object | None = None,
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
        confirmer=confirmer,  # pyright: ignore[reportArgumentType]
        reviewer=reviewer,  # pyright: ignore[reportArgumentType]
        enabled_sync_jobs=enabled_sync_jobs,
    )
    return context, source, target


class FakeConfirmer:
    """A `Confirmer` returning a preset answer (or mimicking the real non-interactive
    behavior of returning `allow`), recording every call for assertions."""

    def __init__(self, *, approve: bool | None = None, return_allow: bool = False) -> None:
        self._approve = approve
        self._return_allow = return_allow
        self.calls: list[dict[str, object]] = []

    async def confirm(
        self,
        *,
        title: str,
        message: str,
        allow: bool,
        allow_flag: str,
        log_extra: dict[str, object] | None = None,
    ) -> bool:
        self.calls.append({"title": title, "message": message, "allow": allow, "allow_flag": allow_flag})
        if self._return_allow:
            # Mirror TerminalUIConfirmer's non-interactive branch: the answer IS `allow`.
            return allow
        assert self._approve is not None, "FakeConfirmer needs either approve= or return_allow=True"
        return self._approve


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
                # plan() now classifies from the SOURCE registry (corrected D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                # converge/replay still reads the target's copy, placed there by the push.
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


class TestPromptingSnippetCannotHang:
    """G30: a snippet that would need stdin must FAIL rather than hang the sync. The
    mechanism is the replay command's shape — the body passed as ONE quoted argument to
    `bash -c`, `login_shell=False`, and no stdin supplied under any name — so a command
    that waits for input reads EOF and exits non-zero, becoming an ordinary per-item
    failure (D-27). Asserted on the command shape; nothing here actually blocks.
    """

    @pytest.mark.asyncio
    async def test_replay_supplies_no_stdin_and_a_prompting_snippet_is_a_plain_item_failure(self) -> None:
        item_id = "unreproducible:apt-no-candidate:brother-driver"
        body = "apt-get install brother-driver"  # a debconf prompt with nothing behind it
        registry_yaml = (
            "snippets:\n"
            f"  {item_id}:\n"
            "    label: brother-driver (no apt candidate)\n"
            f"    body: {body}\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                f"bash -c {shlex.quote(body)}": CommandResult(1, "", "debconf: EOF on stdin at conffile prompt"),
            }
        )
        job = ManualInstallsSyncJob(context)

        result = await job.converge(job_diff(item_id, DiffAction.INSTALL))

        assert result.success is False
        replay_calls = [c for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert replay_calls[0].args[0] == f"bash -c {shlex.quote(body)}"
        assert replay_calls[0].kwargs["login_shell"] is False
        # No stdin reaches the command under any name the executor could accept.
        assert not {"stdin", "input", "input_data"} & set(replay_calls[0].kwargs)


class TestInstallOnly:
    """G24: `manual_installs_sync` is install-only. Unreproducible items describe what the
    SOURCE has installed; there is no target-side manifest to be "extra" against, so no
    input can make this job propose a removal.
    """

    @pytest.mark.asyncio
    async def test_target_query_is_empty_by_design(self) -> None:
        context, _source, _target = make_context(
            target_responses={"apt-mark showmanual": CommandResult(0, "target-only-tool\n", "")}
        )
        job = ManualInstallsSyncJob(context)

        assert await job.query_target_items() == []

    @pytest.mark.asyncio
    async def test_no_removal_diff_or_group_even_when_the_target_holds_items(self) -> None:
        """The target is stocked with everything the source has plus its own extras — the
        shape that produces `EXTRA_ON_TARGET`/REMOVE in every other manager — and still no
        removal is proposed, nor is the target ever asked for a manifest.
        """
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
                "find /usr/local": CommandResult(0, "/usr/local/flux\n", ""),
                "dpkg -S": CommandResult(0, "", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\ntarget-only-tool\n", ""),
                "find /usr/local": CommandResult(0, "/usr/local/flux\n/usr/local/target-only\n", ""),
                "dpkg -S": CommandResult(0, "", ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert plan.diffs  # the source-side findings are present...
        assert all(diff.action != DiffAction.REMOVE for diff in plan.diffs)
        assert all(group.action != DiffAction.REMOVE.value for group in plan.groups)
        # ...and no target-side detection ran at all, so nothing target-only can surface.
        assert not [cmd for cmd in all_calls(target) if "showmanual" in cmd or cmd.startswith("find ")]


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
                # plan() classifies INSTALL from the SOURCE registry (corrected D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
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
                # Source registry holds only brscan3 -> it plans INSTALL, flux plans REPORT_ONLY.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
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


class TestSameRunApplication:
    """Corrected D-23: a snippet authored on the fly during review is APPLIED (replayed) on
    the target the SAME run, not one run too late. An item REPORT_ONLY at plan time (no
    source snippet) whose id the review returns in `outcome.snippets` is promoted to an
    INSTALL diff decided APPLY by `after_review()`, so the unchanged base `apply()`
    converges it this run — driven end to end through `execute()`, never by forcing private
    state."""

    @pytest.mark.asyncio
    async def test_on_the_fly_snippet_is_replayed_the_same_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point Path.home at an empty dir so no on-disk source registry exists: the push
        # early-returns (its overwrite guard never runs) and the replay reads the seeded
        # target registry below, which stands in for what the push would have delivered.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:falco-app"
        body = "sudo dpkg -i /tmp/falco.deb"
        # Post-push target registry: the mocked send_file transports nothing, so seed the
        # snippet the replay reads directly on the target (simulates after_review's push).
        target_registry_yaml = (
            "snippets:\n"
            f"  {item_id}:\n"
            "    label: falco-app (no apt candidate)\n"
            f"    body: {body}\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        reviewer = FakeReviewer(snippets={item_id: body})
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "falco-app\n", ""),
                "apt-cache policy": CommandResult(0, "falco-app:\n  Candidate: (none)\n", ""),
                # Empty source registry -> plan classifies REPORT_ONLY (no source snippet).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, target_registry_yaml, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                f"bash -c '{body}'": CommandResult(0, "falco installed\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        # execute() must not raise: the promoted item converges successfully this run.
        await job.execute()

        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert body in replay_calls[0]


class TestClassificationAuthority:
    """Corrected D-23: reproducibility is judged from the SOURCE registry, never the
    target. A snippet only on the target does NOT make an item reproducible; the same
    snippet on the source does. Direct pin of the one-run-too-late bug's root cause."""

    @pytest.mark.asyncio
    async def test_target_only_snippet_stays_report_only(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
                # Source registry empty -> no source snippet.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            target_responses={
                # Present only on the target: must NOT make the item reproducible.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "unreproducible:apt-no-candidate:brscan3")
        assert diff.action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_source_snippet_classifies_install(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "unreproducible:apt-no-candidate:brscan3")
        assert diff.action == DiffAction.INSTALL

    @pytest.mark.asyncio
    async def test_dry_run_previews_on_the_fly_install_without_replay_or_write(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ADR-014: under dry-run an on-the-fly-authored item is promoted and previewed as
        an install (`apply()`'s dry-run branch reports 1 change to apply), yet NO `bash -c`
        replay reaches the target and NO source registry write (`mv -f` of
        `package-snippets.yaml`) runs — a rehearsal leaves no trace and touches nothing."""
        item_id = "unreproducible:apt-no-candidate:falco-app"
        body = "sudo dpkg -i /tmp/falco.deb"
        reviewer = FakeReviewer(snippets={item_id: body})
        context, source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "falco-app\n", ""),
                "apt-cache policy": CommandResult(0, "falco-app:\n  Candidate: (none)\n", ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            dry_run=True,
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        with caplog.at_level(logging.INFO):
            await job.execute()  # must not raise

        # Promoted: previewed as an install rather than reported as no-change.
        assert "Applying 1 manual change(s)" in caplog.text
        # No replay reached the target and no source registry write happened.
        assert not [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        source_writes = [
            c.args[0]
            for c in source.run_command.call_args_list
            if "package-snippets" in c.args[0] and "mv -f" in c.args[0]
        ]
        assert not source_writes


class TestSkipOnceResolution:
    """D-21: skip-once is a valid resolution — a run whose only items were skipped-once is
    clean. Decision 10: an interactive review can no longer leave an item genuinely
    undecided, so `unresolved` never fails an interactive run."""

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
    async def test_interactive_unresolved_no_longer_fails_the_run(self) -> None:
        """Decision 10: the `_unresolved_as_failures` override is gone — an interactive
        outcome carrying an unresolved id (now unreachable through the real review) applies
        cleanly rather than failing the job."""
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        job.accept_review(
            plan,
            ReviewOutcome(decisions={item_id: Decision.SKIP_ONCE}, was_interactive=True, unresolved=(item_id,)),
        )

        await job.apply()  # must not raise


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
                # plan() classifies both INSTALL from the SOURCE registry (corrected D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
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


class TestSnippetPush:
    """D-23: `manual_installs_sync` pushes `package-snippets.yaml` to the target itself,
    after its own review and before any replay, depending on no other job. The source
    registry lives at `~/.config/pc-switcher/package-snippets.yaml`; the source is the
    local machine, so its on-disk path resolves against `Path.home()`."""

    def _write_source_registry(self, tmp_path: Path, content: str = BRSCAN3_REGISTRY_YAML) -> Path:
        registry = tmp_path / SNIPPET_REGISTRY_RELPATH
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(content)
        return registry

    @pytest.mark.asyncio
    async def test_push_sends_source_registry_under_the_user_home_never_etc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source_registry = self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")})
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_called_once()
        local, remote = target.send_file.call_args.args
        assert local == source_registry
        assert remote == "/home/user/.config/pc-switcher/package-snippets.yaml"
        assert "/etc" not in remote

    @pytest.mark.asyncio
    async def test_absent_source_registry_makes_push_a_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No registry file exists under tmp_path — a user who has never authored a snippet.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context()
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]  # must not raise

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_pushes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(dry_run=True)
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_snippet_authored_in_review_is_persisted_before_the_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """finalize-then-push: the review's authored snippet is written to the SOURCE
        registry before the file is pushed, so the pushed copy includes it (D-23)."""
        source_registry = self._write_source_registry(tmp_path, "snippets: {}\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:brscan3"
        context, source, target = make_context(target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")})
        job = ManualInstallsSyncJob(context)
        diff = job_diff(item_id, DiffAction.REPORT_ONLY)
        plan = PackagePlan(manager="manual", diffs=(diff,), groups=())

        events: list[str] = []
        base_source = source.run_command.side_effect

        def _rec_source(cmd: str, **kw: object) -> CommandResult:
            if "package-snippets" in cmd and "mv -f" in cmd:
                events.append("persist")
            return base_source(cmd, **kw)

        source.run_command = AsyncMock(side_effect=_rec_source)

        async def _rec_send(_local: Path, _remote: str, **_: object) -> None:
            events.append("push")

        target.send_file = AsyncMock(side_effect=_rec_send)

        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={item_id: Decision.SKIP_ONCE},
                was_interactive=True,
                snippets={item_id: "sudo dpkg -i /tmp/brscan3.deb"},
            ),
        )
        await job.after_review()

        assert events == ["persist", "push"]
        assert target.send_file.call_args.args[0] == source_registry

    @pytest.mark.asyncio
    async def test_push_runs_after_review_and_before_replay_in_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End to end: `execute()` pushes the registry, then `apply()` replays the
        snippet-backed item against the target — push strictly before replay."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:brscan3"
        reviewer = FakeReviewer(decisions={item_id: Decision.APPLY})
        context, _source, target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "brscan3\n", ""),
                "apt-cache policy": CommandResult(0, "brscan3:\n  Candidate: (none)\n", ""),
                # plan() classifies INSTALL from the SOURCE registry (corrected D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                "bash -c 'sudo dpkg -i /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        events: list[str] = []
        base_run = target.run_command.side_effect

        def _rec_run(cmd: str, **kw: object) -> CommandResult:
            if cmd.startswith("bash -c"):
                events.append("replay")
            return base_run(cmd, **kw)

        target.run_command = AsyncMock(side_effect=_rec_run)

        async def _rec_send(_local: Path, _remote: str, **_: object) -> None:
            events.append("push")

        target.send_file = AsyncMock(side_effect=_rec_send)

        await job.execute()

        assert events == ["push", "replay"]


# A target registry holding brscan3 PLUS an extra entry the source does not have.
TARGET_WITH_EXTRA_YAML = BRSCAN3_REGISTRY_YAML + (
    "  unreproducible:apt-no-candidate:cnpg:\n"
    "    label: cnpg (no apt candidate)\n"
    "    body: sudo dpkg -i /tmp/cnpg.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# A target registry whose brscan3 body DIFFERS from the source's.
TARGET_CHANGED_BODY_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 (no apt candidate)\n"
    "    body: sudo dpkg -i /tmp/brscan3-OLD.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)


class TestSnippetRegistryOverwriteGuard:
    """Decision 9: the wholesale `package-snippets.yaml` push is guarded. A purely additive
    overwrite (source superset of target) proceeds silently; one that would lose or change a
    target entry needs explicit confirmation, and otherwise aborts the whole run."""

    def _write_source_registry(self, tmp_path: Path, content: str = BRSCAN3_REGISTRY_YAML) -> Path:
        registry = tmp_path / SNIPPET_REGISTRY_RELPATH
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(content)
        return registry

    @pytest.mark.asyncio
    async def test_additive_overwrite_proceeds_without_confirming(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Target is a subset of the source (here empty): additive -> push, no prompt."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=False)  # would abort if ever consulted
        context, _source, target = make_context(
            target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")},
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_identical_target_entry_is_additive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Target holds exactly the same brscan3 body the source has: additive -> no prompt."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=False)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_lost_target_entry_prompts_and_proceeds_on_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Target holds an entry (cnpg) absent from the source: non-additive -> confirm.
        On approval the wholesale push proceeds."""
        self._write_source_registry(tmp_path)  # source has brscan3 only
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        # The prompt names the entry that would be lost, and passes allow=False (no override).
        assert "cnpg" in str(confirmer.calls[0]["message"])
        assert confirmer.calls[0]["allow"] is False
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_lost_target_entry_aborts_on_decline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Declining the non-additive overwrite aborts the whole run and sends nothing."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=False)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(SyncAbortedByUser):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_body_is_non_additive_and_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Target holds brscan3 with a DIFFERENT body than the source: non-additive."""
        self._write_source_registry(tmp_path)  # source brscan3 body
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_CHANGED_BODY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        assert "CHANGED" in str(confirmer.calls[0]["message"])
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_additive_push_without_a_confirmer_fails_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G22 — the requirement is that a non-additive push NEVER silently overwrites; the
        two acceptable outcomes are a confirmed push or a failed run. With no confirmer on
        the context there is nothing to ask, and this pins the actual failure mode: the
        bare `assert self.context.confirmer is not None` in
        `manual_installs_sync._guard_registry_overwrite` (manual_installs_sync.py:305)
        raises `AssertionError` and the run fails.

        A misconfigured injection surfacing as a bare `AssertionError` is a rough message
        for a user, but it IS a loud, transfer-free failure — which is the property that
        matters here. Nothing is sent.
        """
        self._write_source_registry(tmp_path)  # source has brscan3 only
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=None,  # nothing injected
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(AssertionError, match="confirmer"):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_interactive_non_additive_aborts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-interactive run cannot confirm: the confirmer returns its `allow` (False,
        since no override flag exists), so a non-additive overwrite aborts."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(return_allow=True)  # mimic non-interactive: answer == allow
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(SyncAbortedByUser):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()


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
