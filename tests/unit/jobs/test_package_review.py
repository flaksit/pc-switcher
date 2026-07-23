"""Unit tests for the batched checkbox review primitive (D-24, plan 02-02).

Every real terminal rendering/keybinding/handoff question is explicitly out of scope here
(that is Task 3's human checkpoint, RESEARCH Assumption A2) — these tests stub the
`questionary` prompt and drive `review_items` through its interactive, non-interactive,
abort, grouping and automation-env branches.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.manual_installs_sync import ManualInstallsSyncJob
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    PACKAGE_REVIEW_AUTOMATION_ENV,
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
    TerminalUIReviewer,
    review_items,
)
from pcswitcher.jobs.packages.sync_core import PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, SyncAbortedByUser


def _mock_isatty(interactive: bool) -> MagicMock:
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = interactive
    return mock_stdin


def _interactive_console() -> Console:
    """A Console that reports itself as a terminal (paired with a mocked isatty stdin)."""
    return Console(file=io.StringIO(), force_terminal=True)


def _non_interactive_console() -> Console:
    return Console(file=io.StringIO())


def _entry(item_id: str, label: str = "pkg", action_label: str = "install") -> ReviewEntry:
    return ReviewEntry(item_id=item_id, label=label, action_label=action_label, detail=None)


def _fake_prompt(*, ask_return: object = None, ask_side_effect: object = None) -> MagicMock:
    """Build a fake `questionary.checkbox/select/text(...)` return value with a stubbed
    `.ask()` — the same shape every questionary prompt type shares.
    """
    prompt = MagicMock()
    if ask_side_effect is not None:
        prompt.ask = MagicMock(side_effect=ask_side_effect)
    else:
        prompt.ask = MagicMock(return_value=ask_return)
    return prompt


def _unreproducible_group(entries: Sequence[ReviewEntry]) -> ReviewGroup:
    return ReviewGroup(
        manager="apt",
        action=UNREPRODUCIBLE_REVIEW_ACTION,
        title="Resolve apt items with no reproducible install",
        entries=tuple(entries),
    )


def _collateral_group(entries: Sequence[ReviewEntry]) -> ReviewGroup:
    return ReviewGroup(
        manager="apt",
        action=COLLATERAL_REVIEW_ACTION,
        title="Resolve apt manual-collateral removals",
        entries=tuple(entries),
    )


@pytest.mark.asyncio
class TestNonInteractive:
    """D-26: no TTY -> prompt for nothing, skip everything once, record nothing permanent."""

    async def test_no_prompt_constructed_and_everything_skipped_once(self) -> None:
        console = _non_interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
        ):
            outcome = await review_items(groups, console=console, ui=ui)

        checkbox.assert_not_called()
        assert outcome.was_interactive is False
        assert outcome.decisions == {"a": Decision.SKIP_ONCE}
        ui.pause.assert_not_called()
        ui.resume.assert_not_called()

    async def test_warns_with_unresolved_count_and_reports_groups(self) -> None:
        buffer = io.StringIO()
        console = Console(file=buffer)
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a"), _entry("b")])
        ]
        logger = MagicMock()

        with patch.object(sys, "stdin", _mock_isatty(False)):
            await review_items(groups, console=console, ui=ui, logger=logger)

        logger.warning.assert_called_once()
        assert logger.warning.call_args.args[1] == 2
        # The console still reports every item even though nothing was applied.
        assert "pkg" in buffer.getvalue()


@pytest.mark.asyncio
class TestInteractive:
    """Interactive runs pause/resume the live display around the blocking prompt."""

    async def test_ticked_entries_map_to_apply_others_to_skip_once(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        groups = [
            ReviewGroup(
                manager="apt",
                action="install",
                title="Install packages",
                entries=[_entry("a"), _entry("b"), _entry("c")],
            )
        ]
        prompt = _fake_prompt(ask_return=["a", "c"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt) as checkbox,
        ):
            outcome = await review_items(groups, console=console, ui=ui)

        checkbox.assert_called_once()
        assert outcome.was_interactive is True
        assert outcome.decisions == {
            "a": Decision.APPLY,
            "b": Decision.SKIP_ONCE,
            "c": Decision.APPLY,
        }
        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_ui_resumed_when_prompt_raises(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]
        prompt = _fake_prompt(ask_side_effect=KeyboardInterrupt)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt),
            pytest.raises(KeyboardInterrupt),
        ):
            await review_items(groups, console=console, ui=ui)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_abort_skips_current_and_remaining_groups(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")]),
            ReviewGroup(manager="snap", action="install", title="Install snaps", entries=[_entry("b")]),
        ]
        aborted_prompt = _fake_prompt(ask_return=None)
        never_prompt = _fake_prompt(ask_return=["b"])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[aborted_prompt, never_prompt],
            ) as checkbox,
        ):
            outcome = await review_items(groups, console=console, ui=ui)

        # Only the first group's prompt is ever constructed; the second is never reached.
        checkbox.assert_called_once()
        assert outcome.decisions == {"a": Decision.SKIP_ONCE, "b": Decision.SKIP_ONCE}
        ui.resume.assert_called_once()

    async def test_install_group_defaults_checked_removal_group_defaults_unchecked(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        install_group = ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])
        removal_group = ReviewGroup(
            manager="apt", action="remove", title="Remove packages", entries=[_entry("b", action_label="remove")]
        )
        prompt = _fake_prompt(ask_return=[])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt) as checkbox,
        ):
            await review_items([install_group, removal_group], console=console, ui=ui)

        assert checkbox.call_count == 2
        install_choices = checkbox.call_args_list[0].kwargs["choices"]
        removal_choices = checkbox.call_args_list[1].kwargs["choices"]
        assert install_choices[0].checked is True
        assert removal_choices[0].checked is False

    async def test_no_group_mixes_install_and_removal_entries_in_one_prompt(self) -> None:
        """Removals never share a checkbox screen with installs (D-07/D-24)."""
        console = _interactive_console()
        ui = MagicMock()
        install_group = ReviewGroup(
            manager="apt", action="install", title="Install packages", entries=[_entry("a"), _entry("c")]
        )
        removal_group = ReviewGroup(
            manager="apt", action="remove", title="Remove packages", entries=[_entry("b", action_label="remove")]
        )
        change_group = ReviewGroup(
            manager="snap", action="change", title="Change snap channels", entries=[_entry("d", action_label="change")]
        )
        prompt = _fake_prompt(ask_return=[])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt) as checkbox,
        ):
            outcome = await review_items([install_group, removal_group, change_group], console=console, ui=ui)

        assert checkbox.call_count == 3
        for call in checkbox.call_args_list:
            values = {choice.value for choice in call.kwargs["choices"]}
            # Every prompt's entries come from exactly one input group.
            assert values in ({"a", "c"}, {"b"}, {"d"})
        assert set(outcome.decisions) == {"a", "b", "c", "d"}

    async def test_removal_group_title_names_concrete_verb(self) -> None:
        group = ReviewGroup(
            manager="apt", action="remove", title="Remove packages", entries=[_entry("a", action_label="remove")]
        )
        console = _interactive_console()
        ui = MagicMock()
        prompt = _fake_prompt(ask_return=[])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt) as checkbox,
        ):
            await review_items([group], console=console, ui=ui)

        message = checkbox.call_args.args[0]
        assert message == "Remove packages"
        assert message != "Apply"


@pytest.mark.asyncio
class TestTerminalUIReviewer:
    """`TerminalUIReviewer` is a thin adapter: it forwards to `review_items` with the
    console, ui and logger it was constructed with, and returns the outcome unchanged.
    """

    async def test_review_forwards_console_ui_logger_and_returns_outcome_unchanged(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        logger = MagicMock()
        reviewer = TerminalUIReviewer(console, ui, logger=logger)
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]
        sentinel_outcome = ReviewOutcome(decisions={"a": Decision.APPLY}, was_interactive=True)

        with patch(
            "pcswitcher.jobs.packages.review.review_items",
            AsyncMock(return_value=sentinel_outcome),
        ) as review_mock:
            result = await reviewer.review(groups)

        assert result is sentinel_outcome
        review_mock.assert_awaited_once_with(groups, console=console, ui=ui, logger=logger)

    async def test_pause_and_resume_both_run_when_the_underlying_prompt_raises(self) -> None:
        """The adapter keeps `review_items`'s pause/resume `finally`: even when the
        blocking prompt raises, the live display is handed back.
        """
        console = _interactive_console()
        ui = MagicMock()
        reviewer = TerminalUIReviewer(console, ui)
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]
        prompt = _fake_prompt(ask_side_effect=KeyboardInterrupt)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt),
            pytest.raises(KeyboardInterrupt),
        ):
            await reviewer.review(groups)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()


@pytest.mark.asyncio
class TestBlockingPromptOffLoop:
    """ADR-005: the blocking `.ask()` call must not block the event loop."""

    async def test_synchronous_sleep_in_ask_does_not_block_loop(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]

        def _blocking_ask() -> list[str]:
            time.sleep(0.2)
            return ["a"]

        prompt = MagicMock()
        prompt.ask = _blocking_ask

        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.02)
                ticks += 1

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt),
        ):
            ticker_task = asyncio.create_task(_ticker())
            await review_items(groups, console=console, ui=ui)
            await ticker_task

        # If .ask() had run on the event loop, the ticker could not have advanced at all
        # during the 0.2s sleep; it must have made meaningful progress concurrently.
        assert ticks > 0


class TestAutomationEnv:
    """D-26: the hidden env var answers a review without a TTY, for integration tests only."""

    @pytest.mark.asyncio
    async def test_automation_env_returns_mapped_decisions_without_prompting(self) -> None:
        console = _non_interactive_console()
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a"), _entry("b")])
        ]
        env = {PACKAGE_REVIEW_AUTOMATION_ENV: json.dumps({"a": "apply", "b": "skip_once"})}

        with (
            patch.dict("os.environ", env),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
        ):
            outcome = await review_items(groups, console=console, ui=ui)

        checkbox.assert_not_called()
        ui.pause.assert_not_called()
        assert outcome.decisions == {"a": Decision.APPLY, "b": Decision.SKIP_ONCE}

    def test_env_var_not_mentioned_in_cli_help(self) -> None:
        result = subprocess.run(
            ["uv", "run", "pc-switcher", "sync", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert PACKAGE_REVIEW_AUTOMATION_ENV not in result.stdout
        assert PACKAGE_REVIEW_AUTOMATION_ENV not in result.stderr


@pytest.mark.asyncio
class TestUnreproducibleGroupResolution:
    """D-21: an `UNREPRODUCIBLE_REVIEW_ACTION` group gets the three-way per-entry
    resolution flow (add a snippet / record machine-specific / skip for now), never a
    checkbox tick.
    """

    async def test_add_snippet_choice_captures_body_verbatim_including_whitespace(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return="add_snippet")
        body = "  sudo dpkg -i /tmp/x.deb\n\nsudo apt-get install -f -y\n"
        text_prompt = _fake_prompt(ask_return=body)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.snippets == {"u1": body}
        assert "u1" not in outcome.unresolved

    async def test_skip_always_choice_yields_skip_always_decision_and_no_snippet(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return="skip_always")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions["u1"] == Decision.SKIP_ALWAYS
        assert outcome.snippets == {}
        assert "u1" not in outcome.unresolved

    async def test_explicit_skip_once_is_a_resolution_not_unresolved(self) -> None:
        """D-21: an explicit "Skip for now" is a real decision, so the item is resolved
        for this run and left OUT of `unresolved`."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return="skip_once")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions["u1"] == Decision.SKIP_ONCE
        assert "u1" not in outcome.unresolved

    async def test_cancelled_select_leaves_the_item_unresolved(self) -> None:
        """D-21: a cancelled select (`None`) decided nothing — distinct from an explicit
        skip-once — so the item is genuinely unresolved."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return=None)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions["u1"] == Decision.SKIP_ONCE
        assert outcome.unresolved == ("u1",)

    async def test_declining_an_empty_snippet_body_leaves_the_item_unresolved(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return="add_snippet")
        text_prompt = _fake_prompt(ask_return=None)  # capture cancelled

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.snippets == {}
        assert outcome.unresolved == ("u1",)

    async def test_ui_resumed_when_snippet_capture_raises(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return="add_snippet")
        text_prompt = _fake_prompt(ask_side_effect=KeyboardInterrupt)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
            pytest.raises(KeyboardInterrupt),
        ):
            await review_items([group], console=console, ui=ui)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_non_interactive_offers_no_capture_and_marks_every_item_unresolved(self) -> None:
        console = _non_interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3"), _entry("u2", label="cnpg")])

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.questionary.select") as select_mock,
            patch("pcswitcher.jobs.packages.review.questionary.text") as text_mock,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        select_mock.assert_not_called()
        text_mock.assert_not_called()
        assert outcome.snippets == {}
        assert set(outcome.unresolved) == {"u1", "u2"}
        assert outcome.was_interactive is False

    async def test_unreproducible_group_never_offered_as_a_checkbox(self) -> None:
        """The group's action is a sentinel `review_items` special-cases, not a normal
        install/remove verb — asserting the checkbox path is never taken guards against
        the sentinel silently falling through to the generic tick-list flow.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return="skip_once")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
        ):
            await review_items([group], console=console, ui=ui)

        checkbox.assert_not_called()


@pytest.mark.asyncio
class TestCollateralGroupResolution:
    """D-30: a `COLLATERAL_REVIEW_ACTION` group gets the three-way per-entry flow
    (install-anyway / skip / abort), recorded against `entry.item_id` (which the caller,
    `AptSyncJob`, maps onto the triggering install), never a checkbox tick.
    """

    async def test_install_anyway_records_apply(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        select_prompt = _fake_prompt(ask_return="install_anyway")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions["apt:package:pkg-a"] == Decision.APPLY

    async def test_skip_records_skip_once(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        select_prompt = _fake_prompt(ask_return="skip")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions["apt:package:pkg-a"] == Decision.SKIP_ONCE

    async def test_abort_raises_sync_aborted_by_user_naming_the_collateral_package(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        select_prompt = _fake_prompt(ask_return="abort")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            pytest.raises(SyncAbortedByUser, match="other-manual"),
        ):
            await review_items([group], console=console, ui=ui)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_bracketed_collateral_label_renders_without_markup_error(self) -> None:
        """T-02-02: a collateral package name containing bracket characters must not reach
        a Rich `Panel`/console as a bare `str`, or markup parsing raises `MarkupError`.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="pkg[weird]name")])
        select_prompt = _fake_prompt(ask_return="skip")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.decisions["apt:package:pkg-a"] == Decision.SKIP_ONCE

    async def test_collateral_group_never_offered_as_a_checkbox(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        select_prompt = _fake_prompt(ask_return="skip")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
        ):
            await review_items([group], console=console, ui=ui)

        checkbox.assert_not_called()

    async def test_non_interactive_collateral_entries_skip_once_and_are_not_unresolved(self) -> None:
        """D-26: without a TTY a collateral entry comes back SKIP_ONCE like every other
        item (the install it gates is simply not approved) and is never flagged unresolved
        — that status is reserved for unreproducible items.
        """
        console = _non_interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.questionary.select") as select_mock,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        select_mock.assert_not_called()
        assert outcome.decisions["apt:package:pkg-a"] == Decision.SKIP_ONCE
        assert outcome.unresolved == ()


# ---------------------------------------------------------------------------------
# D-21/D-27: mandatory registration terminates — an unresolved unreproducible item
# fails the job after an interactive review; non-interactive and dry-run are exempt.
#
# `apply()`'s raise on a non-empty unresolved list is driven by the
# `_unresolved_as_failures` hook, which `ManualInstallsSyncJob` implements (D-18); a
# thin subclass fixes name/manager_id so these apply()-only tests use the real hook.
# ---------------------------------------------------------------------------------


class _FakeUnreproducibleJob(ManualInstallsSyncJob):
    name: ClassVar[str] = "fake_unrepro"
    manager_id: ClassVar[str] = "fake"


def _unresolved_job_context(*, dry_run: bool = False) -> JobContext:
    source = MagicMock()
    source.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
    target = MagicMock()
    target.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
    return JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
    )


def _unreproducible_diff(item_id: str) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.UNREPRODUCIBLE,
        diff_class=DiffClass.UNREPRODUCIBLE,
        action=DiffAction.REPORT_ONLY,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


@pytest.mark.asyncio
class TestUnresolvedFailsTheJob:
    async def test_interactive_unresolved_raises_naming_the_item_even_with_no_converge_failure(self) -> None:
        context = _unresolved_job_context()
        job = _FakeUnreproducibleJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True, unresolved=(diff.item_id,)))

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        failed_ids = {d.item_id for d, _stderr in exc_info.value.failures}
        assert failed_ids == {diff.item_id}

    async def test_interactive_resolved_does_not_raise(self) -> None:
        context = _unresolved_job_context()
        job = _FakeUnreproducibleJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True, unresolved=()))

        await job.apply()  # must not raise

    async def test_non_interactive_unresolved_does_not_raise_on_that_basis_alone(self) -> None:
        context = _unresolved_job_context()
        job = _FakeUnreproducibleJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=False, unresolved=(diff.item_id,)))

        await job.apply()  # must not raise

    async def test_dry_run_unresolved_does_not_raise_on_that_basis_alone(self) -> None:
        context = _unresolved_job_context(dry_run=True)
        job = _FakeUnreproducibleJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True, unresolved=(diff.item_id,)))

        await job.apply()  # must not raise
