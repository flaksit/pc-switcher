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
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from pcswitcher.jobs.package_review import (
    PACKAGE_REVIEW_AUTOMATION_ENV,
    Decision,
    ReviewEntry,
    ReviewGroup,
    review_items,
)


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
    """Build a fake `questionary.checkbox(...)` return value with a stubbed `.ask()`."""
    prompt = MagicMock()
    if ask_side_effect is not None:
        prompt.ask = MagicMock(side_effect=ask_side_effect)
    else:
        prompt.ask = MagicMock(return_value=ask_return)
    return prompt


@pytest.mark.asyncio
class TestNonInteractive:
    """D-26: no TTY -> prompt for nothing, skip everything once, record nothing permanent."""

    async def test_no_prompt_constructed_and_everything_skipped_once(self) -> None:
        console = _non_interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.package_review.questionary.checkbox") as checkbox,
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
            patch("pcswitcher.jobs.package_review.questionary.checkbox", return_value=prompt) as checkbox,
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
            patch("pcswitcher.jobs.package_review.questionary.checkbox", return_value=prompt),
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
                "pcswitcher.jobs.package_review.questionary.checkbox",
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
            patch("pcswitcher.jobs.package_review.questionary.checkbox", return_value=prompt) as checkbox,
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
            patch("pcswitcher.jobs.package_review.questionary.checkbox", return_value=prompt) as checkbox,
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
            patch("pcswitcher.jobs.package_review.questionary.checkbox", return_value=prompt) as checkbox,
        ):
            await review_items([group], console=console, ui=ui)

        message = checkbox.call_args.args[0]
        assert message == "Remove packages"
        assert message != "Apply"


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
            patch("pcswitcher.jobs.package_review.questionary.checkbox", return_value=prompt),
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
            patch("pcswitcher.jobs.package_review.questionary.checkbox") as checkbox,
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
