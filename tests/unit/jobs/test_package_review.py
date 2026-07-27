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
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.manual_installs_sync import ManualInstallsSyncJob
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    PACKAGE_REVIEW_AUTOMATION_ENV,
    REPO_CONFLICT_REVIEW_ACTION,
    REPO_REMOVAL_REVIEW_ACTION,
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
    TerminalUIReviewer,
    review_items,
)
from pcswitcher.jobs.packages.sync_core import PackagePlan
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


def _apply_screens(checkbox: MagicMock) -> list[Any]:
    """The APPLY checkbox calls only — one per group, identified by the message being the
    group's own title.

    An actionable group that leaves anything unticked is followed by a second checkbox
    screen offering D-07's permanent skip ("never offer again on this machine?"). Tests
    about the apply list itself filter that screen out rather than indexing call positions.
    """
    return [call for call in checkbox.call_args_list if "never offer again" not in call.args[0]]


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
        apply_prompt = _fake_prompt(ask_return=["a", "c"])
        # The follow-up "never offer again" screen over the unticked entry: nothing ticked,
        # so every unticked item keeps its skip-once decision.
        keep_for_next_run = _fake_prompt(ask_return=[])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.checkbox",
                side_effect=[apply_prompt, keep_for_next_run],
            ) as checkbox,
        ):
            outcome = await review_items(groups, console=console, ui=ui)

        assert len(_apply_screens(checkbox)) == 1
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

    async def test_checkbox_ctrl_c_aborts_the_entire_sync(self) -> None:
        """Decision 10: Ctrl-C / EOF at a checkbox screen means the user wants to abort the
        whole sync, not silently skip the rest of the review."""
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
            pytest.raises(SyncAbortedByUser),
        ):
            await review_items(groups, console=console, ui=ui)

        # Only the first group's prompt is ever constructed; the second is never reached.
        checkbox.assert_called_once()
        # The live display is always handed back, even on abort.
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

        apply_calls = _apply_screens(checkbox)
        assert len(apply_calls) == 2
        install_choices = apply_calls[0].kwargs["choices"]
        removal_choices = apply_calls[1].kwargs["choices"]
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

        assert len(_apply_screens(checkbox)) == 3
        for call in checkbox.call_args_list:
            values = {choice.value for choice in call.kwargs["choices"]}
            # Every prompt's entries come from exactly one input group — the apply screens
            # and the follow-up permanent-skip screens alike.
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

        message = _apply_screens(checkbox)[0].args[0]
        assert message == "Remove packages"
        assert message != "Apply"

    async def test_every_direction_that_arrives_unticked_is_still_offered_permanence(self) -> None:
        """ "Arrives unticked" and "is offered permanence" are two independent properties of
        a group (`_REMOVAL_ACTIONS` vs `_PROMOTABLE_ACTIONS`), and ADR-021 makes them differ
        for the two-answer screens. Every ordinary removal direction must keep both.
        """
        console = _interactive_console()
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action=action, title=f"{action} things", entries=[_entry(action)])
            for action in ("remove", "delete", "disable")
        ]
        # Nothing ticked on the apply screen, everything ticked on the follow-up: a
        # promotion can only be observed where the follow-up screen is offered at all.
        asked: list[int] = []

        def _ask() -> list[str]:
            call = len(asked)
            asked.append(call)
            return [] if call % 2 == 0 else [groups[call // 2].entries[0].item_id]

        prompt = _fake_prompt(ask_side_effect=_ask)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt) as checkbox,
        ):
            outcome = await review_items(groups, console=console, ui=ui)

        assert len([call for call in checkbox.call_args_list if "never offer again" in call.args[0]]) == 3
        assert outcome.decisions == dict.fromkeys(("remove", "delete", "disable"), Decision.SKIP_ALWAYS)

    async def test_repo_removal_is_unticked_and_never_offered_permanence(self) -> None:
        """The two-answer screen (ADR-021 rulings 5 and 12). It is a removal direction, so
        it arrives unticked like any other; it is NOT promotable, so the "never offer again"
        screen is never built and `SKIP_ALWAYS` is unreachable — which is what "no registry
        entry" means at this layer.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = ReviewGroup(
            manager="apt",
            action=REPO_REMOVAL_REVIEW_ACTION,
            title="Delete repositories the source no longer has (apt)",
            entries=[_entry("apt:source:vendor.list", action_label="delete repository")],
        )
        prompt = _fake_prompt(ask_return=[])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox", return_value=prompt) as checkbox,
        ):
            outcome = await review_items([group], console=console, ui=ui)

        apply_calls = _apply_screens(checkbox)
        assert len(apply_calls) == 1
        assert apply_calls[0].kwargs["choices"][0].checked is False
        assert checkbox.call_count == 1, "no never-offer-again screen may follow a two-answer group"
        assert outcome.decisions == {"apt:source:vendor.list": Decision.SKIP_ONCE}


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

    @pytest.mark.asyncio
    async def test_malformed_automation_json_fails_loudly_and_prompts_nothing(self) -> None:
        """I15 — pins the ACTUAL behaviour: `_decisions_from_automation` hands the raw
        value straight to `json.loads` (review.py:230), so malformed JSON surfaces as
        `json.JSONDecodeError` out of `review_items`.

        A loud failure is the acceptable outcome here: the variable is a hidden, test-only
        hook (D-26), so the only ways to get it wrong are a broken test harness or a user
        who found it and mis-set it — both of which must stop the run rather than silently
        degrade into prompting (a TTY-less integration run would then hang or skip
        everything) or into applying a half-parsed decision map.
        """
        console = _non_interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]

        with (
            patch.dict("os.environ", {PACKAGE_REVIEW_AUTOMATION_ENV: "{not json"}),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
            pytest.raises(json.JSONDecodeError),
        ):
            await review_items(groups, console=console, ui=ui)

        # Nothing was prompted, and the live display was never touched: the automation
        # branch fails before `ui.pause()`, so there is no paused UI left behind.
        checkbox.assert_not_called()
        ui.pause.assert_not_called()
        ui.resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_decision_value_in_automation_json_fails_loudly(self) -> None:
        """Same contract for well-formed JSON naming a decision that does not exist: the
        `Decision(...)` lookup raises `ValueError` rather than defaulting to a skip.
        """
        console = _non_interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]

        with (
            patch.dict("os.environ", {PACKAGE_REVIEW_AUTOMATION_ENV: json.dumps({"a": "apply_everything"})}),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
            pytest.raises(ValueError, match="apply_everything"),
        ):
            await review_items(groups, console=console, ui=ui)

        checkbox.assert_not_called()

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
        body = "  sudo dpkg --install /tmp/x.deb\n\nsudo apt-get install --fix-broken --assume-yes\n"
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

    async def test_cancelled_select_aborts_the_entire_sync(self) -> None:
        """Decision 10: a cancelled select (`None`, i.e. Ctrl-C / EOF) means the user wants
        to abort the whole sync, not skip this one item."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return=None)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            pytest.raises(SyncAbortedByUser, match="brscan3"),
        ):
            await review_items([group], console=console, ui=ui)

        ui.resume.assert_called_once()

    async def test_empty_snippet_body_reprompts_until_a_real_choice(self) -> None:
        """Decision 10: an empty snippet capture is NOT accepted and does NOT fall through
        to 'unresolved' — the three-way choice is re-prompted until the user gives a real
        snippet or an explicit skip. Here the user submits an empty body, then chooses
        skip-once on the re-prompt."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        # First select -> add_snippet (empty body), second select -> skip_once.
        select_prompt = _fake_prompt(ask_side_effect=["add_snippet", "skip_once"])
        text_prompt = _fake_prompt(ask_return="")  # empty submission

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        # The empty body was rejected; the re-prompted skip-once is the real resolution.
        assert outcome.snippets == {}
        assert outcome.decisions["u1"] == Decision.SKIP_ONCE
        assert outcome.unresolved == ()

    async def test_empty_snippet_then_real_snippet_is_captured(self) -> None:
        """Decision 10: after an empty submission the user may re-choose add-snippet and
        supply a real body, which is then captured verbatim."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        body = "sudo dpkg --install /tmp/x.deb"
        select_prompt = _fake_prompt(ask_side_effect=["add_snippet", "add_snippet"])
        text_prompt = _fake_prompt(ask_side_effect=["", body])  # empty, then real

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui)

        assert outcome.snippets == {"u1": body}
        assert outcome.unresolved == ()

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


def _conflict_group(entries: Sequence[ReviewEntry]) -> ReviewGroup:
    return ReviewGroup(
        manager="apt",
        action=REPO_CONFLICT_REVIEW_ACTION,
        title="Resolve apt repository conflicts",
        entries=tuple(entries),
    )


def _conflict_entry(
    *, target_version: str = "deb https://old.example.com stable main\n", source_version: str = "Types: deb\n"
) -> ReviewEntry:
    return ReviewEntry(
        item_id="apt:conflict:vendor.list",
        label="vendor.list",
        action_label="overwrite",
        detail="vendor.list differs on the two machines and feeds machine-specific packages on the target: curl",
        versions=(target_version, source_version),
    )


@pytest.mark.asyncio
class TestRepoConflictGroupResolution:
    """Ruling 6: a `REPO_CONFLICT_REVIEW_ACTION` group gets a two-way per-entry flow —
    overwrite or skip once — with both whole file versions shown, never a checkbox and
    never a third answer.
    """

    async def test_overwrite_records_apply(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        select_prompt = _fake_prompt(ask_return="overwrite")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui)

        assert outcome.decisions == {"apt:conflict:vendor.list": Decision.APPLY}

    async def test_skip_records_skip_once_and_only_two_answers_are_offered(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        select_prompt = _fake_prompt(ask_return="skip_once")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt) as select_mock,
        ):
            outcome = await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui)

        assert outcome.decisions == {"apt:conflict:vendor.list": Decision.SKIP_ONCE}
        assert [choice.value for choice in select_mock.call_args.kwargs["choices"]] == ["overwrite", "skip_once"]

    async def test_both_whole_versions_are_shown_and_no_unified_diff(self) -> None:
        """The user's own words: a diff of two repository definitions is not readable. The
        target's current file comes first, then the source's, each whole.
        """
        out = io.StringIO()
        console = Console(file=out, force_terminal=True, no_color=True, width=200)
        ui = MagicMock()
        entry = _conflict_entry(
            target_version="deb https://old.example.com stable main\n",
            source_version="deb https://new.example.com noble main\n",
        )
        select_prompt = _fake_prompt(ask_return="skip_once")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            await review_items([_conflict_group([entry])], console=console, ui=ui)

        printed = out.getvalue()
        assert "old.example.com" in printed
        assert "new.example.com" in printed
        assert printed.index("old.example.com") < printed.index("new.example.com")
        assert "@@" not in printed and "\n-deb" not in printed

    async def test_a_bracketed_filename_in_a_conflict_panel_renders_without_markup_error(self) -> None:
        """T-02-02: neither the filename nor either file body may reach Rich as a bare
        `str`. This screen is the only one that prints whole FILES, and a repository
        definition is exactly where a bracketed path shows up — one that Rich reads as a
        closing tag raises `MarkupError` and takes the review down with it.
        """
        console = _interactive_console()
        ui = MagicMock()
        entry = ReviewEntry(
            item_id="apt:conflict:vendor[1].list",
            label="vendor[1].list",
            action_label="overwrite",
            detail="feeds [curl]",
            versions=(
                "# migrated from [/etc/apt/sources.list]\n"
                "deb [signed-by=/etc/apt/keyrings/v.gpg] https://a.example.com s m\n",
                "Types: deb\nURIs: https://a.example.com\nSigned-By: [/etc/apt/keyrings/v.gpg]\n",
            ),
        )
        select_prompt = _fake_prompt(ask_return="skip_once")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([_conflict_group([entry])], console=console, ui=ui)

        assert outcome.decisions == {"apt:conflict:vendor[1].list": Decision.SKIP_ONCE}

    async def test_ctrl_c_aborts_the_sync_naming_the_file(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        select_prompt = _fake_prompt(ask_return=None)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            pytest.raises(SyncAbortedByUser, match=r"vendor\.list"),
        ):
            await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui)

        ui.resume.assert_called_once()

    async def test_conflict_group_never_offered_as_a_checkbox(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        select_prompt = _fake_prompt(ask_return="skip_once")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.checkbox") as checkbox,
        ):
            await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui)

        checkbox.assert_not_called()

    async def test_non_interactive_conflict_entries_skip_once_and_are_not_unresolved(self) -> None:
        console = _non_interactive_console()
        ui = MagicMock()

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.questionary.select") as select_mock,
        ):
            outcome = await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui)

        select_mock.assert_not_called()
        assert outcome.decisions == {"apt:conflict:vendor.list": Decision.SKIP_ONCE}
        assert outcome.unresolved == ()


# ---------------------------------------------------------------------------------
# Decision 10: `unresolved` is unrepresentable in an interactive flow, so
# `ManualInstallsSyncJob` no longer overrides `_unresolved_as_failures` — an interactive
# review always resolves every item (or aborts the whole sync). These apply()-only tests
# pin that a `ReviewOutcome` carrying `unresolved` (only ever produced non-interactively
# now) never fails the job on that basis. A thin subclass fixes name/manager_id.
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
class TestUnresolvedNeverFailsTheJob:
    async def test_interactive_unresolved_does_not_raise(self) -> None:
        """Decision 10: an interactive review can no longer produce `unresolved`, so the
        job no longer fails on it — even a hand-built interactive outcome carrying an
        unresolved id applies cleanly (the override that used to fail it is gone)."""
        context = _unresolved_job_context()
        job = _FakeUnreproducibleJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True, unresolved=(diff.item_id,)))

        await job.apply()  # must not raise

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
