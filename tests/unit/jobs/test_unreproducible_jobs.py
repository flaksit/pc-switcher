"""Unit tests for `packages/unreproducible.py`'s shared half — the detect/filter/diff
pipeline, the snippet registry with its push and consent question, the review grouping and
the replay — exercised through one concrete job, `ManualDebSyncJob`.

Nothing here is specific to that job's detection: `manual_installs_sync` inherits the same
code and is covered for its own detection in `test_manual_installs_sync.py`. All executor
interactions are mocked; no real dpkg/apt-cache/sudo commands run.
"""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.panel import Panel

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.manual_deb_sync import ManualDebSyncJob
from pcswitcher.jobs.packages.items import DiffAction, ItemClass
from pcswitcher.jobs.packages.review import (
    UNREPRODUCIBLE_RETRY_REVIEW_ACTION,
    UNREPRODUCIBLE_REVIEW_ACTION,
    UNREPRODUCIBLE_UPDATE_REVIEW_ACTION,
    Decision,
    ReviewGroup,
    ReviewOutcome,
    ReviewPolicy,
    policy_decision,
)
from pcswitcher.jobs.packages.state import SNIPPET_REGISTRY_RELPATH, SnippetBodies
from pcswitcher.jobs.packages.sync_core import ConvergeItemDeclined, PackageItemFailures, PackagePlan
from pcswitcher.jobs.packages.unreproducible import UnreproducibleItem
from pcswitcher.models import CommandResult, JobSkipped, SyncAborted
from tests.unit.console_capture import captured_console
from tests.unit.jobs.unreproducible_harness import (
    BRSCAN3_REGISTRY_YAML,
    POLICY_HAND_DEB,
    POLICY_REPO_INSTALLED,
    STATUS_QUERY,
    FakeConfirmer,
    FakeReviewer,
    all_calls,
    decision_file_writes,
    hand_deb_policy,
    installed_at,
    installed_on,
    job_diff,
    make_context,
    registry_writes,
)


class TestSnippetResolution:
    """A registry snippet makes an item reproducible: INSTALL + replay; without one it is
    REPORT_ONLY and carved into its own resolution group (`PKG-FR-SNIPPET-VERBATIM`/`PKG-FR-MANUAL-RESOLUTION`)."""

    @pytest.mark.asyncio
    async def test_item_with_snippet_plans_install_and_converges_by_replaying_it(self) -> None:
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                # plan() now classifies from the SOURCE registry (corrected `PKG-FR-MANUAL-SAME-RUN`).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                # converge/replay still reads the target's copy, placed there by the push.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "brscan3 installed\n", ""),
            },
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        diff = next(d for d in plan.diffs if d.item_id == item_id)
        assert diff.action == DiffAction.INSTALL

        result = await job.converge(diff)

        assert result.success
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert "dpkg --install /tmp/brscan3.deb" in replay_calls[0]

    @pytest.mark.asyncio
    async def test_item_without_snippet_is_report_only_and_grouped_separately(self) -> None:
        """G29 — an item the source holds no snippet for appears in its own resolution
        question and in no other list."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualDebSyncJob(context)

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
        """G85 — a snippet-backed diff whose snippet vanished between plan and converge (a
        registry race) fails as one item (`PKG-FR-OUTCOME-FAILED`), never raises."""
        context, _source, _target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            }
        )
        job = ManualDebSyncJob(context)
        diff = job_diff("unreproducible:apt-no-candidate:gone", DiffAction.INSTALL)

        result = await job.converge(diff)

        assert result.success is False


class TestPromptingSnippetCannotHang:
    """A snippet that would need stdin must FAIL rather than hang the sync. The
    mechanism is the replay command's shape — the body passed as ONE quoted argument to
    `bash -c`, `login_shell=False`, and no stdin supplied under any name — so a command
    that waits for input reads EOF and exits non-zero, becoming an ordinary per-item
    failure (`PKG-FR-OUTCOME-FAILED`). Asserted on the command shape; nothing here actually blocks.
    """

    @pytest.mark.asyncio
    async def test_replay_supplies_no_stdin_and_a_prompting_snippet_is_a_plain_item_failure(self) -> None:
        """G58 — a snippet whose command asks a question fails as its own item rather than
        hanging the sync: nothing is ever fed to its input."""
        item_id = "unreproducible:apt-no-candidate:brother-driver"
        body = "apt-get install brother-driver"  # a debconf prompt with nothing behind it
        registry_yaml = (
            "snippets:\n"
            f"  {item_id}:\n"
            "    label: brother-driver (no apt candidate)\n"
            f"    install_body: {body}\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                f"bash -c {shlex.quote(body)}": CommandResult(1, "", "debconf: EOF on stdin at conffile prompt"),
            }
        )
        job = ManualDebSyncJob(context)

        result = await job.converge(job_diff(item_id, DiffAction.INSTALL))

        assert result.success is False
        replay_calls = [c for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert replay_calls[0].args[0] == f"bash -c {shlex.quote(body)}"
        assert replay_calls[0].kwargs["login_shell"] is False
        # No stdin reaches the command under any name the executor could accept.
        assert not {"stdin", "input", "input_data"} & set(replay_calls[0].kwargs)


class TestPlanIsReadOnly:
    """`PKG-FR-REVIEW-FIRST`: nothing on the target may change before the user has answered,
    and this job plans entirely off the source — a scan plus its own registry read.

    Two halves, because this job has two ways to write: a command, and the registry transfer
    `after_review()` makes. Both are asserted absent, so a push that drifted earlier in the
    order would fail here rather than in the ordering test alone.
    """

    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_command_and_transfers_nothing(self) -> None:
        """H5 — planning reaches the target with neither a `mutates=` command nor a `send_file`."""
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        # Non-vacuous: the detection found something, so there was a plan to build at all.
        assert plan.diffs
        for call in target.run_command.call_args_list:
            assert "mutates" not in call.kwargs, call.args[0]
        target.send_file.assert_not_awaited()


class TestInstallOnly:
    """`PKG-FR-MANUAL-REMOVE`: a removal reaches only what the TARGET's own detector claims
    there. Software the target holds that some manager can account for is another job's, and
    no input can make this one propose deleting it.
    """

    @pytest.mark.asyncio
    async def test_no_removal_is_proposed_for_software_a_repository_supplies(self) -> None:
        """G22, G88 — the target is stocked with everything the source has plus its own extras,
        every one of them reproducible from a repository it configures — the shape that
        produces `EXTRA_ON_TARGET`/REMOVE in every other manager — and still no removal is
        proposed and nothing target-only is named anywhere.
        """
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            },
            target_responses={STATUS_QUERY: installed_on("target-only-tool")},
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert plan.diffs  # the source-side findings are present...
        assert all(diff.action != DiffAction.REMOVE for diff in plan.diffs)
        assert all(group.action != DiffAction.REMOVE.value for group in plan.groups)
        # ...and nothing the target alone holds appears in any list, in any direction.
        named = {diff.item_id for diff in plan.diffs} | {
            entry.item_id for group in plan.groups for entry in group.entries
        }
        assert not [item_id for item_id in named if "target-only" in item_id]


class TestWhatTheTargetAlreadyHolds:
    """`PKG-FR-MANUAL-DIFF`: both machines are read and only what the target lacks is
    presented, which is what stops a finding already reproduced there from being asked about
    on every later run.
    """

    @pytest.mark.asyncio
    async def test_a_finding_the_target_already_holds_is_not_presented(self) -> None:
        """G109 — the target is asked what it holds, and the finding it answers with is dropped
        rather than put to the user again."""
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3", "code"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3") + hand_deb_policy("code"), ""),
            },
            target_responses={STATUS_QUERY: installed_on("coreutils", "code")},
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert [diff.item_id for diff in plan.diffs] == ["unreproducible:apt-no-candidate:brscan3"]
        assert [cmd for cmd in all_calls(target) if STATUS_QUERY in cmd], "the target was never asked"

    @pytest.mark.asyncio
    async def test_the_target_is_read_even_when_the_source_found_nothing(self) -> None:
        """G113 — the target is asked whatever the source found, because a removal is exactly
        the case the source contributes nothing to (`PKG-FR-MANUAL-REMOVE`): skipping the
        read when the source has no findings would make that direction unreachable."""
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("gh"),
                "apt-cache policy": CommandResult(0, POLICY_REPO_INSTALLED, ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert [cmd for cmd in all_calls(target) if STATUS_QUERY in cmd]


class TestPermanentMarkWrites:
    """`_finalize_unreproducible`'s write side (`PKG-FR-MACHINE-SPECIFIC`/`PKG-FR-MANUAL-RESOLUTION`): which machine
    records a
    resolved unreproducible item, and which resolutions record nothing at all."""

    @staticmethod
    def _brscan3_context(*, dry_run: bool = False) -> tuple[JobContext, MagicMock, MagicMock]:
        return make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            },
            dry_run=dry_run,
        )

    @pytest.mark.asyncio
    async def test_never_install_it_records_the_mark_on_the_source_naming_the_item(self) -> None:
        """G36 — "never install it on Nomad" writes the mark through Atlas's executor, the
        machine that holds the software, never Nomad's; the entry carries the item's own id
        and its label."""
        context, source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        job.accept_review(plan, ReviewOutcome(decisions={item_id: Decision.SKIP_ALWAYS}, was_interactive=True))
        await job.apply()

        writes = decision_file_writes(source, "manual_deb")
        assert len(writes) == 1
        assert item_id in writes[0]
        assert "brscan3 (installed from no configured repository)" in writes[0]
        assert decision_file_writes(target, "manual_deb") == []

    @pytest.mark.asyncio
    async def test_not_for_now_records_nothing_on_either_machine(self) -> None:
        """G35 — skipping for this run is a resolution that leaves no trace, so the next
        sync asks about the finding again."""
        context, source, target = self._brscan3_context()
        job = ManualDebSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        job.accept_review(plan, ReviewOutcome(decisions={item_id: Decision.SKIP_ONCE}, was_interactive=True))
        await job.apply()

        assert decision_file_writes(source, "manual_deb") == []
        assert decision_file_writes(target, "manual_deb") == []

    @pytest.mark.asyncio
    async def test_a_rehearsal_records_no_permanent_mark(self) -> None:
        """G55 — ADR-014: the same answer under `--dry-run` writes nothing on Atlas."""
        context, source, target = self._brscan3_context(dry_run=True)
        job = ManualDebSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        job.accept_review(plan, ReviewOutcome(decisions={item_id: Decision.SKIP_ALWAYS}, was_interactive=True))
        await job.apply()

        assert decision_file_writes(source, "manual_deb") == []
        assert decision_file_writes(target, "manual_deb") == []


class TestEmptyDetection:
    @pytest.mark.asyncio
    async def test_empty_detection_produces_no_group_and_applies_nothing(self) -> None:
        """G17 — backstop (must_haves): an empty unreproducible set yields no review group and
        nothing to apply."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("gh"),
                "apt-cache policy": CommandResult(0, POLICY_REPO_INSTALLED, ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert plan.groups == ()

        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True))
        await job.apply()  # must not raise


class TestTracerEndToEnd:
    """The tracer's single path: detect two items, one the source holds a snippet for and
    one it does not, plan, assert the review groups, then converge the snippet-backed item
    against the target."""

    @pytest.mark.asyncio
    async def test_detect_plan_and_replay_end_to_end(self) -> None:
        """G30 — an item the source holds a snippet for appears as an ordinary install
        alongside the rest, and converges by replaying it."""
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3", "falco-app"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3") + hand_deb_policy("falco-app"), ""),
                # Source registry holds only brscan3 -> it plans INSTALL, falco-app REPORT_ONLY.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "brscan3 installed\n", ""),
            },
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        # brscan3 has a snippet -> INSTALL (resolved); falco-app has none -> REPORT_ONLY.
        assert by_id["unreproducible:apt-no-candidate:brscan3"].action == DiffAction.INSTALL
        assert by_id["unreproducible:apt-no-candidate:falco-app"].action == DiffAction.REPORT_ONLY

        install_group = next(g for g in plan.groups if g.action == DiffAction.INSTALL.value)
        assert "unreproducible:apt-no-candidate:brscan3" in {e.item_id for e in install_group.entries}
        resolution_group = next(g for g in plan.groups if g.action == UNREPRODUCIBLE_REVIEW_ACTION)
        assert {e.item_id for e in resolution_group.entries} == {"unreproducible:apt-no-candidate:falco-app"}

        result = await job.converge(by_id["unreproducible:apt-no-candidate:brscan3"])
        assert result.success
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert "/tmp/brscan3.deb" in replay_calls[0]


class TestSameRunApplication:
    """Corrected `PKG-FR-MANUAL-SAME-RUN`: a snippet authored on the fly during review is APPLIED (replayed) on
    the target the SAME run, not one run too late. An item REPORT_ONLY at plan time (no
    source snippet) whose id the review returns in `outcome.snippets` is promoted to an
    INSTALL diff decided APPLY by `after_review()`, so the unchanged base `apply()`
    converges it this run — driven end to end through `execute()`, never by forcing private
    state."""

    @pytest.mark.asyncio
    async def test_on_the_fly_snippet_is_replayed_the_same_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G51 — a finding with no snippet at the start of the run, resolved by one written
        during the review, is installed on the target that same run."""
        # Point Path.home at an empty dir so no on-disk source registry exists: the push
        # early-returns (its overwrite guard never runs) and the replay reads the seeded
        # target registry below, which stands in for what the push would have delivered.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:falco-app"
        body = "sudo dpkg --install /tmp/falco.deb"
        # Post-push target registry: the mocked send_file transports nothing, so seed the
        # snippet the replay reads directly on the target (simulates after_review's push).
        target_registry_yaml = (
            "snippets:\n"
            f"  {item_id}:\n"
            "    label: falco-app (no apt candidate)\n"
            f"    install_body: {body}\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        reviewer = FakeReviewer(snippets={item_id: SnippetBodies(install_body=body)})
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("falco-app"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("falco-app"), ""),
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
        job = ManualDebSyncJob(context)

        # execute() must not raise: the promoted item converges successfully this run.
        await job.execute()

        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert body in replay_calls[0]


class TestClassificationAuthority:
    """Corrected `PKG-FR-MANUAL-SAME-RUN`: reproducibility is judged from the SOURCE registry, never the
    target. A snippet only on the target does NOT make an item reproducible; the same
    snippet on the source does. Direct pin of the one-run-too-late bug's root cause."""

    @pytest.mark.asyncio
    async def test_target_only_snippet_stays_report_only(self) -> None:
        """G43 — a snippet only the target holds leaves the item unresolved: the user is
        still asked to resolve it."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                # Source registry empty -> no source snippet.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            target_responses={
                # Present only on the target: must NOT make the item reproducible.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "unreproducible:apt-no-candidate:brscan3")
        assert diff.action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_source_snippet_classifies_install(self) -> None:
        """G44 — a snippet the source holds resolves the item: it is presented as an install."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "unreproducible:apt-no-candidate:brscan3")
        assert diff.action == DiffAction.INSTALL

    @pytest.mark.asyncio
    async def test_dry_run_previews_on_the_fly_install_without_replay_or_write(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G53, J50, J56 — ADR-014: under dry-run an on-the-fly-authored item is promoted and previewed as
        an install (`apply()`'s dry-run branch reports 1 change to apply), yet NO `bash -c`
        replay reaches the target and NO source registry write (`mv --force` of
        `package-snippets.yaml`) runs — a rehearsal leaves no trace and touches nothing."""
        item_id = "unreproducible:apt-no-candidate:falco-app"
        body = "sudo dpkg --install /tmp/falco.deb"
        reviewer = FakeReviewer(snippets={item_id: SnippetBodies(install_body=body)})
        context, source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("falco-app"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("falco-app"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            dry_run=True,
            reviewer=reviewer,
        )
        job = ManualDebSyncJob(context)

        with caplog.at_level(logging.INFO):
            await job.execute()  # must not raise

        # Promoted: previewed as an install rather than reported as no-change.
        assert "Applying 1 manual_deb change(s)" in caplog.text
        # No replay reached the target and no source registry write happened.
        assert not [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        source_writes = [
            c.args[0]
            for c in source.run_command.call_args_list
            if "package-snippets" in c.args[0] and "mv --force" in c.args[0]
        ]
        assert not source_writes

    @pytest.mark.asyncio
    async def test_dry_run_previews_a_pre_existing_snippet_install_naming_the_item(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G54 — a rehearsal of an item the source ALREADY holds a snippet for previews the
        install by name and issues no command on the target."""
        item_id = "unreproducible:apt-no-candidate:brscan3"
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            dry_run=True,
            reviewer=FakeReviewer(decisions={item_id: Decision.APPLY}),
        )
        job = ManualDebSyncJob(context)

        with caplog.at_level(logging.DEBUG):
            await job.execute()

        assert "Would install brscan3 (installed from no configured repository)" in caplog.text
        assert not [cmd for cmd in all_calls(target) if cmd.startswith("bash -c")]


class TestNoTerminalRun:
    """`PKG-FR-NO-TERMINAL` for this job's own `execute()`: a run with nobody to answer
    reports skipped before it touches the target."""

    @pytest.mark.asyncio
    async def test_a_run_with_no_terminal_and_findings_skips_before_touching_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G47 — with findings to resolve and no terminal, the job is reported skipped
        rather than applied: `after_review()` never runs, so no registry is transferred, and
        no snippet is replayed."""
        registry = tmp_path / SNIPPET_REGISTRY_RELPATH
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(BRSCAN3_REGISTRY_YAML)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            },
            target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")},
            reviewer=FakeReviewer(was_interactive=False),
        )
        job = ManualDebSyncJob(context)

        with pytest.raises(JobSkipped):
            await job.execute()

        target.send_file.assert_not_called()
        assert not [cmd for cmd in all_calls(target) if cmd.startswith("bash -c")]


class TestSkipOnceResolution:
    """`PKG-FR-MANUAL-RESOLUTION`: skip-once is a valid resolution — a run whose only items were skipped-once is
    clean. Decision 10: an interactive review can no longer leave an item genuinely
    undecided, so `unresolved` never fails an interactive run."""

    @pytest.mark.asyncio
    async def test_run_whose_only_items_were_skipped_once_passes(self) -> None:
        """G34, J7 — a run whose only findings were all answered "not for now" ends clean."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        # Explicit skip-once: a resolution, NOT in unresolved (`PKG-FR-MANUAL-RESOLUTION`).
        job.accept_review(
            plan,
            ReviewOutcome(decisions={item_id: Decision.SKIP_ONCE}, was_interactive=True, unresolved=()),
        )

        await job.apply()  # must not raise

    @pytest.mark.asyncio
    async def test_interactive_unresolved_no_longer_fails_the_run(self) -> None:
        """G48 — decision 10: the `_unresolved_as_failures` override is gone — an interactive
        outcome carrying an unresolved id (now unreachable through the real review) applies
        cleanly rather than failing the job."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualDebSyncJob(context)

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
        """G86 — one of two approved snippets exits non-zero: the other still runs, and
        only the failing item is reported failed."""
        registry_yaml = (
            "snippets:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    label: brscan3 (no apt candidate)\n"
            "    install_body: sudo dpkg --install /tmp/brscan3.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
            "  unreproducible:apt-no-candidate:cnpg:\n"
            "    label: cnpg (no apt candidate)\n"
            "    install_body: sudo dpkg --install /tmp/cnpg.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3", "cnpg"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3") + hand_deb_policy("cnpg"), ""),
                # plan() classifies both INSTALL from the SOURCE registry (corrected `PKG-FR-MANUAL-SAME-RUN`).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
                "bash -c 'sudo dpkg --install /tmp/cnpg.deb'": CommandResult(1, "", "dpkg: error processing archive"),
            },
        )
        job = ManualDebSyncJob(context)

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

    @pytest.mark.asyncio
    async def test_a_snippet_denied_administrative_rights_fails_like_any_other_item(self) -> None:
        """G87 — a snippet needing administrative rights it does not have on the target is not a
        special case: sudo's refusal is an ordinary non-zero replay, reported against its own
        item with what the machine said. Nothing establishes the right beforehand — what a
        snippet's body needs is unknowable, so there is nothing to pre-check.
        """
        registry_yaml = (
            "snippets:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    label: brscan3 (no apt candidate)\n"
            "    install_body: sudo dpkg --install /tmp/brscan3.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        denial = "sudo: a terminal is required to read the password"
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(1, "", denial),
            },
        )
        job = ManualDebSyncJob(context)

        plan = await job.plan()
        job.accept_review(
            plan,
            ReviewOutcome(decisions={"unreproducible:apt-no-candidate:brscan3": Decision.APPLY}, was_interactive=True),
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        assert [(diff.item_id, stderr) for diff, stderr in exc_info.value.failures] == [
            ("unreproducible:apt-no-candidate:brscan3", denial)
        ]
        assert not any("sudo --non-interactive" in cmd or "sudo -n " in cmd for cmd in all_calls(target))


class TestNoRunReachesSudoValidationDidNotClear:
    """`PKG-FR-SUDO-PRECONDITION`'s "rather than degrading", for this job.

    Its row of the article's table is the only "none" on both machines, and `validate()`
    accordingly probes for no grant at all. That makes the pairing fragile in the one
    direction that matters here: a `sudo` added to either scan would be established by
    nothing, and a machine refusing it would answer with a shorter list of `/opt` and
    `/usr/local` entries — a reduced capture that reads as "the source has nothing to sync".
    A snippet's own body may of course escalate; it is the user's opaque blob, run only after
    they approved it, and not a command this job composes.
    """

    @pytest.mark.asyncio
    async def test_neither_the_capture_nor_validation_asks_either_machine_to_escalate(self) -> None:
        """K67 — no command this job composes runs under sudo, on either machine, at either
        step. Nothing is established, so nothing may be needed.
        """
        context, source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("code"),
                "apt-cache policy": CommandResult(0, POLICY_HAND_DEB, ""),
            }
        )
        job = ManualDebSyncJob(context)

        await job.plan()
        assert await job.validate() == []

        for name, machine in (("source", source), ("target", target)):
            escalations = [cmd for cmd in all_calls(machine) if "sudo" in cmd]
            assert not escalations, f"{name} was asked to escalate: {escalations}"


class TestSnippetPush:
    """`PKG-FR-MANUAL-SAME-RUN`: `manual_installs_sync` pushes `package-snippets.yaml` to the target itself,
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
        """G65, K93 — the target ends the run holding the source's registry under the SSH user's
        own home, never a system directory."""
        source_registry = self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")})
        job = ManualDebSyncJob(context)

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
        """G66 — a source that never had a snippet written on it transfers nothing and fails
        nothing."""
        # No registry file exists under tmp_path — a user who has never authored a snippet.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context()
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]  # must not raise

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_run_with_no_terminal_pushes_nothing_even_with_nothing_to_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G82, J13, J46 — `PKG-FR-NO-TERMINAL`: a non-interactive run transfers no registry. A scan that
        finds nothing raises no `JobSkipped` — the target already matches, so the job
        succeeds (`PKG-FR-OUTCOME-SUCCESS`) — and the push must still not happen: the
        registry on disk holds entries from earlier runs that nobody approved sending
        tonight.
        """
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("gh"),
                "apt-cache policy": CommandResult(0, POLICY_REPO_INSTALLED, ""),
            },
            reviewer=FakeReviewer(was_interactive=False),
        )
        job = ManualDebSyncJob(context)

        await job.execute()  # no JobSkipped: the plan is empty

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_answered_run_with_nothing_to_review_still_transfers_the_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G83 — an empty review means nothing new to decide, not nothing to carry: a run
        at a terminal that found no finding still delivers the registry's earlier entries.
        """
        source_registry = self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("gh"),
                "apt-cache policy": CommandResult(0, POLICY_REPO_INSTALLED, ""),
            },
            target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")},
            reviewer=FakeReviewer(was_interactive=True),
        )
        job = ManualDebSyncJob(context)

        await job.execute()

        target.send_file.assert_called_once()
        assert target.send_file.call_args.args[0] == source_registry

    @pytest.mark.asyncio
    async def test_a_directory_that_cannot_be_created_fails_naming_the_target_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G84 — the transfer's own plumbing failing is a job failure naming the machine,
        never a half-finished transfer."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            target_responses={
                "mkdir --parents": CommandResult(1, "", "mkdir: cannot create directory: Permission denied"),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            }
        )
        job = ManualDebSyncJob(context)

        with pytest.raises(RuntimeError) as excinfo:
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert "target-host" in str(excinfo.value)
        assert "Permission denied" in str(excinfo.value)
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_home_that_cannot_be_resolved_fails_naming_the_target_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G84 — the second plumbing failure: with no home directory there is no absolute
        destination to send to, so nothing is sent."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(target_responses={"echo $HOME": CommandResult(1, "", "no such user")})
        job = ManualDebSyncJob(context)

        with pytest.raises(RuntimeError) as excinfo:
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert "target-host" in str(excinfo.value)
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_snippet_written_this_run_is_stamped_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G52 — `_finalize_unreproducible` runs twice in one run (from `after_review()`
        and again from `apply()`), so the guard that makes the second a no-op is what keeps
        one `authored_at` stamp on the record and the two machines' copies identical: the
        registry is written exactly once.

        Home points at an empty directory, so the push itself is a no-op and the replay
        reads the seeded target registry — what a real push would have delivered.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:falco-app"
        body = "sudo dpkg --install /tmp/falco.deb"
        target_registry_yaml = (
            "snippets:\n"
            f"  {item_id}:\n"
            "    label: falco-app (no apt candidate)\n"
            f"    install_body: {body}\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        context, source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("falco-app"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("falco-app"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, target_registry_yaml, ""),
                f"bash -c '{body}'": CommandResult(0, "falco installed\n", ""),
            },
            reviewer=FakeReviewer(snippets={item_id: SnippetBodies(install_body=body)}),
        )
        job = ManualDebSyncJob(context)

        await job.execute()

        assert len(registry_writes(source)) == 1

    @pytest.mark.asyncio
    async def test_a_successful_replay_records_nothing_on_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G90 — the only file this job writes on Nomad is the registry: two snippets
        replay successfully and Nomad keeps no record of what was installed, so a later run
        has no memory of it."""
        registry_yaml = BRSCAN3_REGISTRY_YAML + (
            "  unreproducible:apt-no-candidate:cnpg:\n"
            "    label: cnpg (no apt candidate)\n"
            "    install_body: sudo dpkg --install /tmp/cnpg.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        self._write_source_registry(tmp_path, registry_yaml)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        decisions = {
            "unreproducible:apt-no-candidate:brscan3": Decision.APPLY,
            "unreproducible:apt-no-candidate:cnpg": Decision.APPLY,
        }
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3", "cnpg"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3") + hand_deb_policy("cnpg"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
                "bash -c 'sudo dpkg --install /tmp/cnpg.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=FakeReviewer(decisions=decisions),
        )
        job = ManualDebSyncJob(context)

        await job.execute()

        assert len([cmd for cmd in all_calls(target) if cmd.startswith("bash -c")]) == 2
        # A WRITE, not any mention: every apply ends by READING both decision files to
        # reconcile them with what the machines hold (`_prune_dead_marks`).
        assert not [cmd for cmd in all_calls(target) if "decisions.yaml" in cmd and "mv --force" in cmd]
        assert decision_file_writes(target, "manual_deb") == []
        assert registry_writes(target) == []
        assert [call.args[1] for call in target.send_file.call_args_list] == [
            "/home/user/.config/pc-switcher/package-snippets.yaml"
        ]

    @pytest.mark.asyncio
    async def test_dry_run_pushes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """G81, J57 — a rehearsal transfers no registry and asks no question."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(dry_run=True)
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_snippet_authored_in_review_is_persisted_before_the_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G49 — finalize-then-push: the review's authored snippet is written to the SOURCE
        registry before the file is pushed, so the pushed copy includes it (`PKG-FR-MANUAL-SAME-RUN`)."""
        source_registry = self._write_source_registry(tmp_path, "snippets: {}\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:brscan3"
        context, source, target = make_context(target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")})
        job = ManualDebSyncJob(context)
        diff = job_diff(item_id, DiffAction.REPORT_ONLY)
        plan = PackagePlan(manager="manual", diffs=(diff,), groups=())

        events: list[str] = []
        base_source = source.run_command.side_effect

        def _rec_source(cmd: str, **kw: object) -> CommandResult:
            if "package-snippets" in cmd and "mv --force" in cmd:
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
                snippets={item_id: SnippetBodies(install_body="sudo dpkg --install /tmp/brscan3.deb")},
            ),
        )
        await job.after_review()

        assert events == ["persist", "push"]
        assert target.send_file.call_args.args[0] == source_registry

    @pytest.mark.asyncio
    async def test_push_runs_after_review_and_before_replay_in_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G50, H11 — end to end: `execute()` pushes the registry, then `apply()` replays the
        snippet-backed item against the target — push strictly before replay."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:brscan3"
        reviewer = FakeReviewer(decisions={item_id: Decision.APPLY})
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                # plan() classifies INSTALL from the SOURCE registry (corrected `PKG-FR-MANUAL-SAME-RUN`).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualDebSyncJob(context)

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
    "    install_body: sudo dpkg --install /tmp/cnpg.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# Two registries holding the same item with different bodies, each a `curl` of a private
# `.deb` — the documented shape of a snippet whose body carries a credential.
SOURCE_WITH_CREDENTIAL_YAML = BRSCAN3_REGISTRY_YAML + (
    "  unreproducible:apt-no-candidate:acme-agent:\n"
    "    label: acme-agent (no apt candidate)\n"
    "    install_body: curl --output /tmp/a.deb https://bearer:s0urce-token@dl.example.test/acme-2.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: laptop\n"
)

TARGET_WITH_CREDENTIAL_YAML = BRSCAN3_REGISTRY_YAML + (
    "  unreproducible:apt-no-candidate:acme-agent:\n"
    "    label: acme-agent (no apt candidate)\n"
    "    install_body: curl --output /tmp/a.deb https://bearer:t4rget-token@dl.example.test/acme-1.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# A target registry holding a bracketed label and body — console markup the question must
# show as written rather than parse.
TARGET_WITH_MARKUP_YAML = (
    "snippets:\n"
    "  unreproducible:unowned-path:/opt/[bold]tool:\n"
    "    label: '[bold]tool (unowned in /opt)'\n"
    "    install_body: 'sudo /opt/[bold]tool/install.sh --mode=[red]fast'\n"
    "    version_body: echo v\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# A target registry whose brscan3 body DIFFERS from the source's.
TARGET_CHANGED_BODY_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 (no apt candidate)\n"
    "    install_body: sudo dpkg --install /tmp/brscan3-OLD.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# A target registry whose brscan3 body matches the source's byte for byte and whose
# AUTHORING RECORD does not: same entry, different `authored_at`/`authored_on`.
TARGET_SAME_BODY_OTHER_AUTHORING_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 (no apt candidate)\n"
    "    install_body: sudo dpkg --install /tmp/brscan3.deb\n"
    "    authored_at: '2025-06-30T09:15:00+00:00'\n"
    "    authored_on: workstation\n"
)

# The same, with the LABEL as the only difference.
TARGET_SAME_BODY_OTHER_LABEL_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 scanner driver\n"
    "    install_body: sudo dpkg --install /tmp/brscan3.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: laptop\n"
)

# A target registry holding two entries the source lacks AND the source's brscan3 with a
# different body: one question has to name all three.
TARGET_WITH_TWO_LOST_AND_ONE_CHANGED_YAML = TARGET_CHANGED_BODY_YAML + (
    "  unreproducible:apt-no-candidate:cnpg:\n"
    "    label: cnpg (no apt candidate)\n"
    "    install_body: sudo dpkg --install /tmp/cnpg.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
    "  unreproducible:unowned-path:/opt/az:\n"
    "    label: az (unowned in /opt)\n"
    "    install_body: sudo /opt/az/install.sh\n"
    "    version_body: echo v\n"
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
        """G68 — target is a subset of the source (here empty): additive -> push, no prompt."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=False)  # would abort if ever consulted
        context, _source, target = make_context(
            target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")},
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_identical_target_entry_is_additive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """G69 — target holds exactly the same brscan3 body the source has: additive -> no prompt."""
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
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_lost_target_entry_prompts_and_proceeds_on_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G70, H16, N17 — target holds an entry (cnpg) absent from the source: non-additive -> confirm.
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
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        # The prompt names the entry that would be lost, and passes allow=False (no override).
        assert "cnpg" in str(confirmer.calls[0]["message"])
        assert confirmer.calls[0]["allow"] is False
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_lost_target_entry_aborts_on_decline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """G73, G74, H60 — declining the non-additive overwrite aborts the whole run and sends nothing."""
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
        job = ManualDebSyncJob(context)

        with pytest.raises(SyncAborted):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_body_is_non_additive_and_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G71 — target holds brscan3 with a DIFFERENT body than the source: non-additive."""
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
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        assert "CHANGED" in str(confirmer.calls[0]["message"])
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_different_authoring_record_is_non_additive_and_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G72 — the body is identical but the authoring record is not, so the push still changes
        the entry the target holds: it is named, and the question shows the authoring records
        rather than printing the unchanged body twice."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(
                    0, TARGET_SAME_BODY_OTHER_AUTHORING_YAML, ""
                ),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        message = str(confirmer.calls[0]["message"])
        assert "CHANGED" in message
        assert "brscan3" in message
        assert "2025-06-30T09:15:00+00:00 on workstation" in message
        assert "2026-01-01T00:00:00+00:00 on laptop" in message
        assert "sudo dpkg --install /tmp/brscan3.deb" not in message  # the body did not change
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_different_label_is_non_additive_and_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G72 — the label is part of the entry too, so replacing it is a change the user answers."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(
                    0, TARGET_SAME_BODY_OTHER_LABEL_YAML, ""
                ),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        message = str(confirmer.calls[0]["message"])
        assert "brscan3 scanner driver" in message
        assert "brscan3 (no apt candidate)" in message
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_credential_in_a_snippet_body_is_withheld_from_the_question(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G77, J125, J126 — ADR-021's fifth credential exit: the question displays two whole snippet bodies,
        and a body may legitimately fetch a private `.deb`. Only what is displayed is
        rewritten — the file the push sends keeps its author's bytes
        (`PKG-FR-SNIPPET-VERBATIM`)."""
        source = self._write_source_registry(tmp_path, SOURCE_WITH_CREDENTIAL_YAML)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_CREDENTIAL_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        message = str(confirmer.calls[0]["message"])
        assert "s0urce-token" not in message
        assert "t4rget-token" not in message
        assert "***@dl.example.test/acme-2.deb" in message
        assert "***@dl.example.test/acme-1.deb" in message
        assert "s0urce-token" in source.read_text(encoding="utf-8")
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_one_question_names_every_entry_the_push_would_lose_or_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G93 — two target entries the source lacks and a third whose body differs are put
        in ONE question, each named."""
        self._write_source_registry(tmp_path)  # source has brscan3 only
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(
                    0, TARGET_WITH_TWO_LOST_AND_ONE_CHANGED_YAML, ""
                ),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        message = str(confirmer.calls[0]["message"])
        assert "unreproducible:apt-no-candidate:cnpg" in message
        assert "unreproducible:unowned-path:/opt/az" in message
        assert "unreproducible:apt-no-candidate:brscan3" in message
        assert message.count("LOST") == 2
        assert message.count("CHANGED") == 1
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_bracketed_label_and_body_reach_the_question_as_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G78 — square-bracketed text is console markup to Rich, and the confirmer renders
        the message inside a `Panel`. Every snippet field is escaped, so the question shows
        the author's text instead of raising on it."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, _target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_MARKUP_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        message = str(confirmer.calls[0]["message"])
        # Rendered the way the real confirmer renders it: unescaped markup raises here.
        console, buffer = captured_console()
        console.print(Panel(message))
        rendered = buffer.getvalue()
        assert "[bold]tool (unowned in /opt)" in rendered
        assert "--mode=[red]fast" in rendered

    @pytest.mark.asyncio
    async def test_a_corrupt_source_registry_ends_the_run_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G80 — an unparsable source file is not a registry holding nothing: the run ends
        naming the file, nothing is asked, and the corrupt bytes never reach the target."""
        corrupt = "snippets: [\n  - broken\n"
        source_registry = self._write_source_registry(tmp_path, corrupt)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        with pytest.raises(SyncAborted, match=re.escape("package-snippets.yaml")):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_not_called()
        assert source_registry.read_text(encoding="utf-8") == corrupt

    @pytest.mark.asyncio
    async def test_a_corrupt_target_registry_ends_the_run_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G79 — the target's file cannot be parsed, so what it holds is unknown: the run
        ends rather than counting the push additive, and nothing is overwritten."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: [\n  - broken\n", ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        with pytest.raises(SyncAborted, match=re.escape("package-snippets.yaml")):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_registries_corrupt_ends_the_run_naming_both_machines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G116 — the two copies are two hand edits, so one ending names both: reading the
        source's first and stopping there would hide the target's until the source is
        repaired.
        """
        corrupt = "snippets: [\n  - broken\n"
        _ = self._write_source_registry(tmp_path, corrupt)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, corrupt, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        with pytest.raises(SyncAborted) as caught:
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        message = str(caught.value)
        assert "on source-host" in message
        assert "on target-host" in message
        assert confirmer.calls == []
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_absent_source_registry_leaves_the_targets_entries_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G94 — with no registry on the source there is no transfer, so nothing of the
        target's can be lost or changed and no question is asked: its two entries stay."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no source registry on disk
        confirmer = FakeConfirmer(approve=False)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualDebSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_not_called()
        assert registry_writes(target) == []

    @pytest.mark.asyncio
    async def test_non_additive_push_without_a_confirmer_fails_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G76 — the requirement is that a non-additive push NEVER silently overwrites; the
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
        job = ManualDebSyncJob(context)

        with pytest.raises(AssertionError, match="confirmer"):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_interactive_non_additive_aborts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """G75 — a non-interactive run cannot confirm: the confirmer returns its `allow` (False,
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
        job = ManualDebSyncJob(context)

        with pytest.raises(SyncAborted):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()


class TestUnreproducibleItem:
    def test_reports_its_item_class(self) -> None:
        assert UnreproducibleItem.ITEM_CLASS == ItemClass.UNREPRODUCIBLE

    def test_same_identifier_different_origin_yields_distinct_item_ids(self) -> None:
        """G21 — a package and a path that share a name are two independent items, one per
        kind of finding."""
        no_candidate = UnreproducibleItem(origin="apt-no-candidate", identifier="brscan3", label="brscan3")
        unowned_path = UnreproducibleItem(origin="unowned-path", identifier="brscan3", label="/opt/brscan3")

        assert no_candidate.item_id != unowned_path.item_id

    def test_label_is_a_plain_field(self) -> None:
        item = UnreproducibleItem(origin="unowned-path", identifier="/opt/flux", label="flux (unowned in /opt)")

        assert item.label == "flux (unowned in /opt)"


class _PolicyReviewer:
    """A `Reviewer` answering exactly as a run with no terminal and the apply flags in force
    does: `policy_decision` settles the groups the flags cover, everything else is declined
    for this run, and `was_interactive` stays False because no human was asked."""

    def __init__(self, policy: ReviewPolicy) -> None:
        self._policy = policy
        self.groups_seen: tuple[ReviewGroup, ...] | None = None

    async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None:
        raise AssertionError(f"an unreproducible job has no gate question; asked {title!r}")

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self.groups_seen = tuple(groups)
        decisions = {
            entry.item_id: policy_decision(group, self._policy) or Decision.SKIP_ONCE
            for group in groups
            for entry in group.entries
        }
        return ReviewOutcome(decisions=decisions, was_interactive=False)


class TestTheCommandLineAnswersTheReview:
    """#245: a run the apply flags answered must still transfer the registry its own
    approved replays read — while writing nothing a human did not author."""

    @staticmethod
    def _write_source_registry(tmp_path: Path, content: str = BRSCAN3_REGISTRY_YAML) -> Path:
        registry = tmp_path / SNIPPET_REGISTRY_RELPATH
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(content)
        return registry

    @pytest.mark.asyncio
    async def test_the_registry_is_pushed_before_the_replay_on_a_flag_answered_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H241 — `converge()` replays from the target's OWN copy of the registry, so a run
        that approves a manual install without a terminal must still reach the after-review
        seam: skipping the push would replay from a stale file or none at all.
        """
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        policy = ReviewPolicy(apply_installs=True)
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=_PolicyReviewer(policy),
            review_policy=policy,
        )
        job = ManualDebSyncJob(context)

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

    @pytest.mark.asyncio
    async def test_a_lossy_registry_transfer_still_aborts_under_both_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H242 — the guard on a non-additive push is not a review item, so no apply flag
        reaches it: the run ends so the two registries can be reconciled by hand, and nothing
        is sent."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        policy = ReviewPolicy(apply_installs=True, apply_removals=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
            },
            confirmer=FakeConfirmer(return_allow=True),
            review_policy=policy,
        )
        job = ManualDebSyncJob(context)

        with pytest.raises(SyncAborted, match="consolidate the two registries"):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_snippet_is_authored_and_the_registry_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H243 — the flags carry this run (brscan3 has a snippet, so its install is
        answered), but the item beside it that has none is answered by nobody: the seam that
        would stamp an authored snippet writes nothing, because it stays keyed to a human's
        answer. A run nobody watched records neither a snippet nor a permanent mark.

        It does not fail on the unresolved item either: `_unresolved_as_failures` fails one
        only on an interactive run, which is unchanged — nobody was asked to resolve it.
        """
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        policy = ReviewPolicy(apply_installs=True, apply_removals=True)
        context, source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("brscan3", "cnpg"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3") + hand_deb_policy("cnpg"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=_PolicyReviewer(policy),
            review_policy=policy,
        )
        job = ManualDebSyncJob(context)

        await job.execute()

        assert not [cmd for cmd in all_calls(source) if "package-snippets" in cmd and "mv --force" in cmd]
        assert not decision_file_writes(source, "manual_deb")


class TestVersionDrift:
    """`PKG-FR-MANUAL-VERSION`: an item both machines have is compared on its installed
    version, and only a difference produces anything (`PKG-FR-APT-HOLD-VERSION`'s exception,
    `PKG-FR-VERSION-SNIPPET`)."""

    @staticmethod
    def _both_hold(source_version: str, target_version: str, *, registry: str = BRSCAN3_REGISTRY_YAML) -> JobContext:
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_at({"brscan3": source_version}),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3", source_version), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry, ""),
            },
            target_responses={STATUS_QUERY: installed_at({"brscan3": target_version})},
        )
        return context

    @pytest.mark.asyncio
    async def test_the_same_version_on_both_machines_produces_nothing(self) -> None:
        """G162 — equal versions are convergence: the item is on both machines and there is
        nothing to say about it."""
        plan = await ManualDebSyncJob(self._both_hold("1.0", "1.0")).plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_newer_version_on_the_source_is_an_update_naming_both_versions(self) -> None:
        """G163 — a version difference is actionable here, unlike for a managed package: a
        repository will eventually move an apt package, and nothing will ever move this."""
        plan = await ManualDebSyncJob(self._both_hold("2.0", "1.0")).plan()

        (diff,) = plan.diffs
        assert diff.action == DiffAction.CHANGE
        assert diff.detail == "source-host has 2.0, target-host has 1.0"

    @pytest.mark.asyncio
    async def test_a_newer_version_on_the_target_still_replays_the_source(self) -> None:
        """G164 — version numbers never decide direction: a sync goes source to target
        whichever machine holds the higher number."""
        plan = await ManualDebSyncJob(self._both_hold("1.0", "2.0")).plan()

        (diff,) = plan.diffs
        assert diff.action == DiffAction.CHANGE
        assert diff.detail == "source-host has 1.0, target-host has 2.0"

    @pytest.mark.asyncio
    async def test_a_version_difference_gets_its_own_screen_carrying_the_recorded_bodies(self) -> None:
        """G165 — the update screen is its own question — run the recorded snippet, rewrite
        it first, or leave this machine's version alone — and it carries what the registry
        holds so a rewrite opens on it rather than on nothing."""
        plan = await ManualDebSyncJob(self._both_hold("2.0", "1.0")).plan()

        (group,) = [g for g in plan.groups if g.action == UNREPRODUCIBLE_UPDATE_REVIEW_ACTION]
        assert [e.item_id for e in group.entries] == ["unreproducible:apt-no-candidate:brscan3"]
        assert group.recorded_bodies is not None

    @pytest.mark.asyncio
    async def test_a_version_difference_with_no_snippet_is_an_item_to_resolve_as_an_update(self) -> None:
        """G166 — there is nothing to replay, so it goes to the resolution question like any
        unresolved item; its verb says update, because the software is already there."""
        plan = await ManualDebSyncJob(self._both_hold("2.0", "1.0", registry="snippets: {}\n")).plan()

        (diff,) = plan.diffs
        assert diff.action == DiffAction.REPORT_ONLY
        (group,) = [g for g in plan.groups if g.action == UNREPRODUCIBLE_REVIEW_ACTION]
        assert [e.action_label for e in group.entries] == ["update"]

    @pytest.mark.asyncio
    async def test_a_version_neither_machine_can_answer_for_produces_nothing(self) -> None:
        """G167 — a comparison needs two answers, so an unanswerable version is not a claimed
        difference: the item simply produces nothing."""
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_at({"brscan3": "1.0"}),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            # dpkg reports the name installed with no version at all.
            target_responses={STATUS_QUERY: CommandResult(0, "brscan3\t\tinstalled\n", "")},
        )

        plan = await ManualDebSyncJob(context).plan()

        assert plan.diffs == ()


class TestRemovingWhatTheSourceDropped:
    """`PKG-FR-MANUAL-REMOVE`: an item only the target holds, that the target's OWN detector
    claims, becomes a removal."""

    @staticmethod
    def _target_only(target_policy: str) -> tuple[JobContext, MagicMock]:
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("gh"),
                "apt-cache policy": CommandResult(0, POLICY_REPO_INSTALLED, ""),
            },
            target_responses={
                STATUS_QUERY: installed_on("gh", "brscan3"),
                "apt-cache policy": CommandResult(0, target_policy, ""),
            },
        )
        return context, target

    @pytest.mark.asyncio
    async def test_a_hand_installed_deb_the_source_dropped_is_offered_for_removal(self) -> None:
        """G168 — the target's own apt says no repository supplies it, and the source no
        longer has it: that is a removal."""
        context, _target = self._target_only(hand_deb_policy("brscan3"))

        plan = await ManualDebSyncJob(context).plan()

        (diff,) = plan.diffs
        assert (diff.item_id, diff.action) == ("unreproducible:apt-no-candidate:brscan3", DiffAction.REMOVE)

    @pytest.mark.asyncio
    async def test_a_package_a_repository_supplies_is_never_offered_for_removal(self) -> None:
        """G169 — a name the target's own apt can reinstall is not this job's to delete; it
        is `apt_sync`'s, which has the bookkeeping for it."""
        context, _target = self._target_only(POLICY_REPO_INSTALLED.replace("gh:", "brscan3:"))

        plan = await ManualDebSyncJob(context).plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_the_removal_runs_apt_get_remove_and_never_purge(self) -> None:
        """G170 — removal does not purge: what apt leaves under `/etc` can be deleted by hand
        at any time, and a purge cannot be undone."""
        context, target = self._target_only(hand_deb_policy("brscan3"))
        job = ManualDebSyncJob(context)
        plan = await job.plan()

        await job.converge(plan.diffs[0])

        (issued,) = [c for c in target.run_command.call_args_list if "apt-get remove" in c.args[0]]
        assert issued.args[0] == "sudo apt-get remove --assume-yes brscan3"
        assert "purge" not in issued.args[0]
        assert issued.kwargs["mutates"]

    @pytest.mark.asyncio
    async def test_a_mark_on_the_target_keeps_its_own_copy_off_the_removal_list(self) -> None:
        """G171 — a machine-specific mark is read from BOTH files now that a removal exists:
        one recorded on the target keeps that machine's copy, and reading only the source's
        would offer to delete the software the mark was given to protect."""
        marks = (
            "machine_specific:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    item_class: unreproducible\n"
            "    label: brscan3\n"
            "    reason: null\n"
            "    recorded_at: '2026-01-01T00:00:00+00:00'\n"
        )
        context, _source, _target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("gh"),
                "apt-cache policy": CommandResult(0, POLICY_REPO_INSTALLED, ""),
            },
            target_responses={
                STATUS_QUERY: installed_on("gh", "brscan3"),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/manual_deb.decisions.yaml": CommandResult(0, marks, ""),
            },
        )

        plan = await ManualDebSyncJob(context).plan()

        assert plan.diffs == ()


class TestTheConvergeLoop:
    """`PKG-FR-MANUAL-CONVERGE-LOOP`: a replay that exits 0 is not convergence. The target's
    version is read back, and a body that moved nothing narrows the menu (`PKG-FR-VERSION-SNIPPET`)."""

    @staticmethod
    def _updating_job(target_versions: list[str], *, reviewer: object | None = None) -> tuple[Any, MagicMock]:
        """A job whose one item differs in version, and whose target reports `target_versions`
        in turn — the first entry before the replay, each later one after another replay."""
        answers = iter(target_versions)
        current = {"version": next(answers)}

        def status(_cmd: str) -> CommandResult:
            return installed_at({"brscan3": current["version"]})

        def replay(_cmd: str) -> CommandResult:
            current["version"] = next(answers, current["version"])
            return CommandResult(0, "", "")

        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_at({"brscan3": "2.0"}),
                "apt-cache policy": CommandResult(0, hand_deb_policy("brscan3", "2.0"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                STATUS_QUERY: status,
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c": replay,
            },
            reviewer=reviewer,
        )
        return ManualDebSyncJob(context), target

    @pytest.mark.asyncio
    async def test_a_replay_that_lands_the_source_version_converges_and_asks_nothing(self) -> None:
        """G172 — the loop's first exit: the target reports the source's version afterwards,
        so one replay is the whole of it and nobody is asked anything."""
        job, target = self._updating_job(["1.0", "2.0"])
        plan = await job.plan()

        result = await job.converge(plan.diffs[0])

        assert result.success
        assert len([c for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]) == 1

    @pytest.mark.asyncio
    async def test_a_replay_that_moves_no_version_asks_again_and_replays_what_is_written(self) -> None:
        """G173 — an installer that no-ops over an existing tree exits 0 and changes nothing,
        which no exit code can show. The version read back is what catches it, and the
        narrowed menu is what the user answers."""
        reviewer = FakeReviewer(
            snippets={
                "unreproducible:apt-no-candidate:brscan3": SnippetBodies(
                    install_body="sudo rm -rf /opt/brscan3 && sudo dpkg --install /tmp/brscan3.deb",
                )
            }
        )
        job, target = self._updating_job(["1.0", "1.0", "2.0"], reviewer=reviewer)
        plan = await job.plan()

        result = await job.converge(plan.diffs[0])

        assert result.success
        assert len([c for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]) == 2
        assert reviewer.groups_seen is not None
        assert reviewer.groups_seen[0].action == UNREPRODUCIBLE_RETRY_REVIEW_ACTION

    @pytest.mark.asyncio
    async def test_the_rewritten_snippet_lands_on_both_machines_so_the_push_stays_additive(self) -> None:
        """G174 — the replacement is written to both registries rather than pushed: a source
        entry changed after this run's own push is what the transfer guard calls a lost
        entry, so pushing again would put a consent question in front of the change the user
        had just made."""
        reviewer = FakeReviewer(
            snippets={"unreproducible:apt-no-candidate:brscan3": SnippetBodies(install_body="echo rewritten")}
        )
        job, target = self._updating_job(["1.0", "1.0", "2.0"], reviewer=reviewer)
        plan = await job.plan()

        await job.converge(plan.diffs[0])

        assert registry_writes(target)

    @pytest.mark.asyncio
    async def test_with_nobody_to_ask_one_attempt_is_made_and_the_item_is_skipped(self) -> None:
        """G175 — the only remaining answers need a person to write a shell script, so a
        headless run stops after one attempt: the item is neither applied nor failed, and the
        run says why."""
        job, target = self._updating_job(["1.0", "1.0"], reviewer=FakeReviewer(was_interactive=False))
        plan = await job.plan()

        with pytest.raises(ConvergeItemDeclined, match=r"still has 1\.0 rather than 2\.0"):
            await job.converge(plan.diffs[0])

        assert len([c for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]) == 1
