"""Unit tests for the batched review primitive (D-24, plan 02-02).

The decision screen's own rendering and key handling live in `test_decision_list.py`;
these tests stub it out and drive `review_items` through its interactive, non-interactive,
abort, grouping and automation-env branches — what each group is ASKED, and what its
answers become.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any, ClassVar, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prompt_toolkit.keys import Keys
from rich.console import Console

from pcswitcher.config import Configuration
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
    ask_gate,
    review_items,
)
from pcswitcher.jobs.packages.sync_core import PackagePlan
from pcswitcher.models import CommandResult, SyncAbortedByUser
from pcswitcher.orchestrator import Orchestrator


class _Hosts(TypedDict):
    """`**HOSTS` as a typed unpack, so a mistyped keyword is a type error rather than a
    silent match against another parameter."""

    source_hostname: str
    target_hostname: str


# The two machine names every screen says out loud. Deliberately concrete and distinct, so
# an assertion that a message names the right one cannot pass on the other's text.
HOSTS: _Hosts = {"source_hostname": "atlas", "target_hostname": "nomad"}


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
    """Build a fake `decision_list/questionary.select/text(...)` return value with a stubbed
    `.ask()` — the same shape every prompt type here shares.
    """
    prompt = MagicMock()
    if ask_side_effect is not None:
        prompt.ask = MagicMock(side_effect=ask_side_effect)
    else:
        prompt.ask = MagicMock(return_value=ask_return)
    return prompt


def _screen_defaults(call: Any) -> dict[str, str]:
    """What one built screen would answer if the user pressed Enter without touching it."""
    return {row.row_id: row.default for row in call.kwargs["rows"]}


def _screen_words(call: Any) -> list[str]:
    """The decision words one built screen offers, in legend order."""
    return [option.word for option in call.kwargs["options"]]


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
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()
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
            await review_items(groups, console=console, ui=ui, logger=logger, **HOSTS)

        logger.warning.assert_called_once()
        assert logger.warning.call_args.args[1] == 2
        # The console still reports every item even though nothing was applied.
        assert "pkg" in buffer.getvalue()

    async def test_the_report_panel_ends_on_its_last_item(self) -> None:
        """A newline after the last entry renders as an empty final line inside the border."""
        buffer = io.StringIO()
        console = Console(file=buffer, no_color=True, width=60)
        groups = [
            ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a"), _entry("b")])
        ]

        with patch.object(sys, "stdin", _mock_isatty(False)):
            await review_items(groups, console=console, ui=MagicMock(), **HOSTS)

        lines = [line.rstrip() for line in buffer.getvalue().splitlines()]
        last_item = max(index for index, line in enumerate(lines) if "pkg" in line)
        assert lines[last_item + 1].startswith("\u2570"), lines[last_item : last_item + 3]


@pytest.mark.asyncio
class TestInteractive:
    """Interactive runs pause/resume the live display around the blocking prompt."""

    async def test_every_row_comes_back_with_the_decision_its_screen_returned(self) -> None:
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
        screen = _fake_prompt(ask_return={"a": "apply", "b": "skip_once", "c": "skip_always"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui, **HOSTS)

        decision_list.assert_called_once()
        assert outcome.was_interactive is True
        assert outcome.decisions == {
            "a": Decision.APPLY,
            "b": Decision.SKIP_ONCE,
            "c": Decision.SKIP_ALWAYS,
        }
        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_one_screen_per_group_and_no_second_pass_over_the_leftovers(self) -> None:
        """The rebuild's whole point: a group is presented once, not once to apply and
        again to promote what was left."""
        console = _interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]
        screen = _fake_prompt(ask_return={"a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui, **HOSTS)

        assert decision_list.call_count == 1
        assert outcome.decisions == {"a": Decision.SKIP_ONCE}

    async def test_an_interactive_group_prints_no_panel_above_its_screen(self) -> None:
        """The screen lists the items itself; a panel above it said everything twice."""
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, no_color=True, width=200)
        ui = MagicMock()
        groups = [
            ReviewGroup(
                manager="apt",
                action="install",
                title="Install packages",
                entries=[_entry("a", label="cmatrix (2.0-6)")],
            )
        ]
        screen = _fake_prompt(ask_return={"a": "apply"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            await review_items(groups, console=console, ui=ui, **HOSTS)

        printed = buffer.getvalue()
        assert "cmatrix (2.0-6)" not in printed
        assert "Install packages" not in printed

    async def test_ui_resumed_when_prompt_raises(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]
        prompt = _fake_prompt(ask_side_effect=KeyboardInterrupt)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt),
            pytest.raises(KeyboardInterrupt),
        ):
            await review_items(groups, console=console, ui=ui, **HOSTS)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_ctrl_c_at_a_decision_screen_aborts_the_entire_sync(self) -> None:
        """Decision 10: Ctrl-C / EOF at a decision screen means the user wants to abort the
        whole sync, not silently skip the rest of the review."""
        console = _interactive_console()
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")]),
            ReviewGroup(manager="snap", action="install", title="Install snaps", entries=[_entry("b")]),
        ]
        aborted = _fake_prompt(ask_return=None)
        later = _fake_prompt(ask_return={"b": "apply"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.decision_list",
                side_effect=[aborted, later],
            ) as decision_list,
            pytest.raises(SyncAbortedByUser),
        ):
            await review_items(groups, console=console, ui=ui, **HOSTS)

        # Only the first group's screen is ever constructed; the second is never reached.
        decision_list.assert_called_once()
        # The live display is always handed back, even on abort.
        ui.resume.assert_called_once()

    async def test_install_rows_start_applied_and_removal_rows_start_skipped(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        install_group = ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])
        removal_group = ReviewGroup(
            manager="apt", action="remove", title="Remove packages", entries=[_entry("b", action_label="remove")]
        )
        prompt = _fake_prompt(ask_side_effect=[{"a": "apply"}, {"b": "skip_once"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            await review_items([install_group, removal_group], console=console, ui=ui, **HOSTS)

        assert _screen_defaults(decision_list.call_args_list[0]) == {"a": Decision.APPLY}
        assert _screen_defaults(decision_list.call_args_list[1]) == {"b": Decision.SKIP_ONCE}

    async def test_no_group_mixes_install_and_removal_entries_in_one_prompt(self) -> None:
        """Removals never share a screen with installs (D-07/D-24)."""
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
        prompt = _fake_prompt(
            ask_side_effect=[
                {"a": "apply", "c": "apply"},
                {"b": "skip_once"},
                {"d": "apply"},
            ]
        )

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            outcome = await review_items([install_group, removal_group, change_group], console=console, ui=ui, **HOSTS)

        assert decision_list.call_count == 3
        for call in decision_list.call_args_list:
            values = {row.row_id for row in call.kwargs["rows"]}
            assert values in ({"a", "c"}, {"b"}, {"d"})
        assert set(outcome.decisions) == {"a", "b", "c", "d"}

    async def test_removal_group_title_names_concrete_verb(self) -> None:
        group = ReviewGroup(
            manager="apt", action="remove", title="Remove packages", entries=[_entry("a", action_label="remove")]
        )
        console = _interactive_console()
        ui = MagicMock()
        prompt = _fake_prompt(ask_return={"a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        message = decision_list.call_args.args[0]
        assert message == "Remove packages"
        assert message != "Apply"

    async def test_a_row_does_not_repeat_the_verb_its_group_title_already_names(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = ReviewGroup(
            manager="apt",
            action="remove",
            title="Remove packages",
            entries=[_entry("a", label="fortunes-min", action_label="remove")],
        )
        prompt = _fake_prompt(ask_return={"a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        row = decision_list.call_args.kwargs["rows"][0]
        assert row.label == "fortunes-min"
        assert row.prefix is None
        # The verb still names the act ANSWER — it is the decision column's word.
        assert decision_list.call_args.kwargs["options"][0].word == "remove"

    async def test_every_removal_direction_still_offers_the_permanent_answer(self) -> None:
        """ "Starts at skip-once" and "is offered permanence" are two independent properties
        of a group (`_REMOVAL_ACTIONS` vs `_PROMOTABLE_ACTIONS`), and ADR-020 D-07 makes them
        differ for the two-answer screens. Every ordinary removal direction keeps both.
        """
        console = _interactive_console()
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action=action, title=f"{action} things", entries=[_entry(action)])
            for action in ("remove", "delete", "disable")
        ]
        prompt = _fake_prompt(ask_side_effect=[{action: "skip_always"} for action in ("remove", "delete", "disable")])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui, **HOSTS)

        for call in decision_list.call_args_list:
            assert "always skip" in _screen_words(call)
            assert _screen_defaults(call) == dict.fromkeys(_screen_defaults(call), Decision.SKIP_ONCE)
        assert outcome.decisions == dict.fromkeys(("remove", "delete", "disable"), Decision.SKIP_ALWAYS)

    async def test_repo_removal_starts_skipped_and_is_never_offered_permanence(self) -> None:
        """The two-answer screen (ADR-020 D-07). It is a removal direction, so it starts at
        skip-once like any other; it is NOT promotable, so "always skip" is absent from its
        options and `SKIP_ALWAYS` is unreachable — which is what "no registry entry" means
        at this layer.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = ReviewGroup(
            manager="apt",
            action=REPO_REMOVAL_REVIEW_ACTION,
            title="Delete repositories the source no longer has (apt)",
            entries=[_entry("apt:source:vendor.list", action_label="delete repository")],
        )
        prompt = _fake_prompt(ask_return={"apt:source:vendor.list": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert _screen_defaults(decision_list.call_args) == {"apt:source:vendor.list": Decision.SKIP_ONCE}
        assert _screen_words(decision_list.call_args) == ["delete repository", "keep it on nomad"]
        assert outcome.decisions == {"apt:source:vendor.list": Decision.SKIP_ONCE}

    async def test_a_report_only_group_offers_two_answers_and_starts_applied(self) -> None:
        """D-07: report-only diffs offer apply or skip only — there is no holder machine to
        record a permanent decision against.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = ReviewGroup(
            manager="apt",
            action="report_only",
            title="Report apt packages",
            entries=[_entry("apt:package:tree", action_label="report")],
        )
        prompt = _fake_prompt(ask_return={"apt:package:tree": "apply"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert _screen_words(decision_list.call_args) == ["report", "skip once"]
        assert _screen_defaults(decision_list.call_args) == {"apt:package:tree": Decision.APPLY}
        assert outcome.decisions == {"apt:package:tree": Decision.APPLY}


@pytest.mark.asyncio
class TestTerminalUIReviewer:
    """`TerminalUIReviewer` is a thin adapter: it forwards to `review_items` with the
    console, ui and logger it was constructed with, and returns the outcome unchanged.
    """

    async def test_review_forwards_console_ui_logger_and_returns_outcome_unchanged(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        logger = MagicMock()
        reviewer = TerminalUIReviewer(console, ui, logger=logger, **HOSTS)
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]
        sentinel_outcome = ReviewOutcome(decisions={"a": Decision.APPLY}, was_interactive=True)

        with patch(
            "pcswitcher.jobs.packages.review.review_items",
            AsyncMock(return_value=sentinel_outcome),
        ) as review_mock:
            result = await reviewer.review(groups)

        assert result is sentinel_outcome
        review_mock.assert_awaited_once_with(groups, console=console, ui=ui, logger=logger, **HOSTS)

    async def test_pause_and_resume_both_run_when_the_underlying_prompt_raises(self) -> None:
        """The adapter keeps `review_items`'s pause/resume `finally`: even when the
        blocking prompt raises, the live display is handed back.
        """
        console = _interactive_console()
        ui = MagicMock()
        reviewer = TerminalUIReviewer(console, ui, **HOSTS)
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]
        prompt = _fake_prompt(ask_side_effect=KeyboardInterrupt)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt),
            pytest.raises(KeyboardInterrupt),
        ):
            await reviewer.review(groups)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()


@pytest.mark.asyncio
class TestAskGate:
    """`ask_gate` asks about the MACHINE, not an item: two answers, no automation hook, and
    a `None` the caller owns when there is no TTY (ADR-020 D-38).
    """

    @staticmethod
    def _ask(prompt: MagicMock, *, console: Console | None = None, ui: MagicMock | None = None) -> Any:
        return ask_gate(
            title="Ubuntu Pro attachment required on the target",
            message="body",
            proceed_label="re-check and continue",
            stop_label="skip apt_sync",
            console=console if console is not None else _interactive_console(),
            ui=ui if ui is not None else MagicMock(),
        )

    async def test_the_two_answers_come_back_as_true_and_false(self) -> None:
        for selected, expected in ((True, True), (False, False)):
            with (
                patch.object(sys, "stdin", _mock_isatty(True)),
                patch(
                    "pcswitcher.jobs.packages.review.questionary.select",
                    return_value=_fake_prompt(ask_return=selected),
                ),
            ):
                assert await self._ask(MagicMock()) is expected

    async def test_exactly_two_choices_are_offered_with_the_captions_the_caller_gave(self) -> None:
        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.select",
                return_value=_fake_prompt(ask_return=True),
            ) as select,
        ):
            await self._ask(MagicMock())

        choices = select.call_args.kwargs["choices"]
        assert [choice.title for choice in choices] == ["re-check and continue", "skip apt_sync"]

    async def test_no_tty_answers_none_without_constructing_a_prompt(self) -> None:
        ui = MagicMock()
        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.questionary.select") as select,
        ):
            answer = await self._ask(MagicMock(), console=_non_interactive_console(), ui=ui)

        assert answer is None
        select.assert_not_called()
        ui.pause.assert_not_called()

    async def test_the_automation_env_hook_cannot_answer_a_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Deliberate negative control: the review's scripted-answer hook must NOT reach
        here — no environment value can stand in for going and attaching the other machine.
        """
        monkeypatch.setenv(PACKAGE_REVIEW_AUTOMATION_ENV, "all")
        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.questionary.select") as select,
        ):
            assert await self._ask(MagicMock(), console=_non_interactive_console()) is None
        select.assert_not_called()

    async def test_ctrl_c_aborts_the_whole_sync_and_hands_the_display_back(self) -> None:
        ui = MagicMock()
        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.select",
                return_value=_fake_prompt(ask_return=None),
            ),
            pytest.raises(SyncAbortedByUser),
        ):
            await self._ask(MagicMock(), ui=ui)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_the_message_is_rendered_as_text_not_markup(self) -> None:
        """T-02-02: a bracketed token in the body must reach the console verbatim rather
        than being parsed as a Rich style.
        """
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True, width=200)
        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.select",
                return_value=_fake_prompt(ask_return=True),
            ),
        ):
            await ask_gate(
                title="gate",
                message="run [sudo pro attach] first",
                proceed_label="yes",
                stop_label="no",
                console=console,
                ui=MagicMock(),
            )

        assert "[sudo pro attach]" in buffer.getvalue()

    async def test_the_terminal_reviewer_forwards_its_console_ui_and_logger(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        logger = MagicMock()
        reviewer = TerminalUIReviewer(console, ui, logger=logger, **HOSTS)

        with patch("pcswitcher.jobs.packages.review.ask_gate", AsyncMock(return_value=True)) as gate:
            assert await reviewer.ask_gate(title="t", message="m", proceed_label="p", stop_label="s") is True

        gate.assert_awaited_once_with(
            title="t", message="m", proceed_label="p", stop_label="s", console=console, ui=ui, logger=logger
        )


@pytest.mark.asyncio
class TestBlockingPromptOffLoop:
    """ADR-005: the blocking `.ask()` call must not block the event loop."""

    async def test_synchronous_sleep_in_ask_does_not_block_loop(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]

        def _blocking_ask() -> dict[str, str]:
            time.sleep(0.2)
            return {"a": "apply"}

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
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt),
        ):
            ticker_task = asyncio.create_task(_ticker())
            await review_items(groups, console=console, ui=ui, **HOSTS)
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
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()
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
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
            pytest.raises(json.JSONDecodeError),
        ):
            await review_items(groups, console=console, ui=ui, **HOSTS)

        # Nothing was prompted, and the live display was never touched: the automation
        # branch fails before `ui.pause()`, so there is no paused UI left behind.
        decision_list.assert_not_called()
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
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
            pytest.raises(ValueError, match="apply_everything"),
        ):
            await review_items(groups, console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

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
            await review_items([group], console=console, ui=ui, **HOSTS)

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.snippets == {"u1": body}
        assert outcome.unresolved == ()

    async def test_a_whitespace_only_snippet_is_not_a_resolution(self) -> None:
        """A body of spaces and newlines replays as nothing at all, so accepting it would
        record a "snippet" that resolves the item without installing anything."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_side_effect=["add_snippet", "skip_once"])
        text_prompt = _fake_prompt(ask_return="   \n\t\n  ")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.snippets == {}
        assert outcome.decisions["u1"] == Decision.SKIP_ONCE

    async def test_the_snippet_editor_finishes_on_ctrl_d(self) -> None:
        """More discoverable than questionary's default "Alt+Enter or Esc then Enter". This
        binds Ctrl-D as SUBMIT inside the editor only; Ctrl-D as an abort stays unhandled.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return="add_snippet")
        text_prompt = _fake_prompt(ask_return="sudo dpkg --install /tmp/x.deb")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt) as text,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        assert "Ctrl-D" in text.call_args.kwargs["instruction"]
        assert "Esc" not in text.call_args.kwargs["instruction"]
        bound = {key for binding in text.call_args.kwargs["key_bindings"].bindings for key in binding.keys}
        assert Keys.ControlD in bound

    async def test_the_permanent_choice_says_what_it_means_rather_than_naming_the_concept(self) -> None:
        """The user's correction: not "record as machine-specific", and not about being
        offered again on this machine — what it MEANS and what will happen.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        select_prompt = _fake_prompt(ask_return="skip_once")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt) as select,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        titles = {choice.value: choice.title for choice in select.call_args.kwargs["choices"]}
        assert titles["skip_always"] == (
            "This one is specific to atlas. Always skip it — nomad never gets it, and you are not asked again"
        )
        assert titles["skip_once"] == (
            "Skip for now — nomad does not get it this sync, and you are asked again next sync"
        )
        assert titles["add_snippet"] == (
            "Write the commands that install it — nomad runs them, now and on every future sync"
        )
        assert select.call_args.args[0] == "How should nomad get brscan3?"

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
            await review_items([group], console=console, ui=ui, **HOSTS)

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        select_mock.assert_not_called()
        text_mock.assert_not_called()
        assert outcome.snippets == {}
        assert set(outcome.unresolved) == {"u1", "u2"}
        assert outcome.was_interactive is False

    async def test_unreproducible_group_never_offered_as_a_decision_screen(self) -> None:
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
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()


@pytest.mark.asyncio
class TestCollateralGroupResolution:
    """D-30: a `COLLATERAL_REVIEW_ACTION` group gets the three-way per-entry flow
    (go ahead / keep the package / stop the sync), recorded against `entry.item_id` (which the caller,
    `AptSyncJob`, maps onto the triggering install), never a checkbox tick.
    """

    async def test_go_ahead_records_apply(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        select_prompt = _fake_prompt(ask_return="proceed")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

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
            await review_items([group], console=console, ui=ui, **HOSTS)

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.decisions["apt:package:pkg-a"] == Decision.SKIP_ONCE

    async def test_collateral_group_never_offered_as_a_decision_screen(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        select_prompt = _fake_prompt(ask_return="skip")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.questionary.select", return_value=select_prompt),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()

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
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

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
        detail=(
            "vendor.list is different on the two machines, and nomad installs curl from it — packages you set "
            "to always skip, so a sync normally leaves them alone"
        ),
        versions=(target_version, source_version),
    )


@pytest.mark.asyncio
class TestRepoConflictGroupResolution:
    """Ruling 6: a `REPO_CONFLICT_REVIEW_ACTION` group is the ordinary decision screen with
    only two answers — overwrite or skip once — preceded by both whole file versions, and
    never a third answer.
    """

    async def test_overwrite_records_apply(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        screen = _fake_prompt(ask_return={"apt:conflict:vendor.list": "apply"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            outcome = await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui, **HOSTS)

        assert outcome.decisions == {"apt:conflict:vendor.list": Decision.APPLY}

    async def test_only_two_answers_are_offered_and_the_row_starts_skipped(self) -> None:
        """An overwrite displaces software the target explicitly marked machine-specific,
        so it is chosen, never defaulted — and it records nothing either way.
        """
        console = _interactive_console()
        ui = MagicMock()
        screen = _fake_prompt(ask_return={"apt:conflict:vendor.list": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui, **HOSTS)

        assert outcome.decisions == {"apt:conflict:vendor.list": Decision.SKIP_ONCE}
        assert _screen_words(decision_list.call_args) == ["overwrite", "keep nomad's version"]
        assert _screen_defaults(decision_list.call_args) == {"apt:conflict:vendor.list": Decision.SKIP_ONCE}

    async def test_one_screen_answers_every_conflicting_file(self) -> None:
        """D-24: the conflicts are a batch, not a queue of one prompt per file."""
        console = _interactive_console()
        ui = MagicMock()
        first = _conflict_entry()
        second = ReviewEntry(
            item_id="apt:conflict:other.list",
            label="other.list",
            action_label="overwrite",
            versions=("a\n", "b\n"),
        )
        screen = _fake_prompt(ask_return={"apt:conflict:vendor.list": "apply", "apt:conflict:other.list": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([_conflict_group([first, second])], console=console, ui=ui, **HOSTS)

        decision_list.assert_called_once()
        assert outcome.decisions == {
            "apt:conflict:vendor.list": Decision.APPLY,
            "apt:conflict:other.list": Decision.SKIP_ONCE,
        }

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
        screen = _fake_prompt(ask_return={"apt:conflict:vendor.list": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            await review_items([_conflict_group([entry])], console=console, ui=ui, **HOSTS)

        printed = out.getvalue()
        assert "old.example.com" in printed
        assert "new.example.com" in printed
        assert printed.index("old.example.com") < printed.index("new.example.com")
        assert "@@" not in printed and "\n-deb" not in printed

    async def test_each_version_panel_is_titled_with_the_machine_that_holds_it(self) -> None:
        """The user's ruling: no screen says "the target". The two panels are titled with the
        two machines' own names, and the target's says "now" because it is the one an
        overwrite would replace.
        """
        out = io.StringIO()
        console = Console(file=out, force_terminal=True, no_color=True, width=200)
        ui = MagicMock()
        screen = _fake_prompt(ask_return={"apt:conflict:vendor.list": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui, **HOSTS)

        printed = out.getvalue()
        assert "On nomad now" in printed
        assert "On atlas" in printed
        assert "the target" not in printed
        assert "the source" not in printed

    async def test_a_version_panel_ends_on_its_last_line_of_content(self) -> None:
        """A file body's own trailing newline renders as an empty last line inside the
        panel border — every other panel in the review had the same trailing gap.
        """
        out = io.StringIO()
        console = Console(file=out, force_terminal=True, no_color=True, width=60)
        ui = MagicMock()
        entry = _conflict_entry(target_version="deb https://old.example.com stable main\n", source_version="x\n")
        screen = _fake_prompt(ask_return={"apt:conflict:vendor.list": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            await review_items([_conflict_group([entry])], console=console, ui=ui, **HOSTS)

        lines = [line.rstrip() for line in out.getvalue().splitlines()]
        content = next(index for index, line in enumerate(lines) if "old.example.com" in line)
        assert lines[content + 1].startswith("╰"), lines[content : content + 3]

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
        screen = _fake_prompt(ask_return={"apt:conflict:vendor[1].list": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            outcome = await review_items([_conflict_group([entry])], console=console, ui=ui, **HOSTS)

        assert outcome.decisions == {"apt:conflict:vendor[1].list": Decision.SKIP_ONCE}

    async def test_ctrl_c_aborts_the_sync_naming_the_screen(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        screen = _fake_prompt(ask_return=None)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            pytest.raises(SyncAbortedByUser, match="Resolve apt repository conflicts"),
        ):
            await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui, **HOSTS)

        ui.resume.assert_called_once()

    async def test_non_interactive_conflict_entries_skip_once_and_are_not_unresolved(self) -> None:
        console = _non_interactive_console()
        ui = MagicMock()

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()
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


def _pin_removal_group(entries: Sequence[ReviewEntry]) -> ReviewGroup:
    return ReviewGroup(
        manager="apt",
        action=REPO_REMOVAL_REVIEW_ACTION,
        title="Delete pin files atlas no longer has (apt)",
        entries=tuple(entries),
    )


@pytest.mark.asyncio
class TestRemovalGroupContent:
    """A deletion screen shows the file it offers to delete, not only its name."""

    @staticmethod
    def _run(group: ReviewGroup, out: io.StringIO) -> Any:
        console = Console(file=out, force_terminal=True, no_color=True, width=200)
        screen = _fake_prompt(ask_return={entry.item_id: "skip_once" for entry in group.entries})
        return (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            console,
        )

    async def test_a_pin_file_is_printed_whole_under_the_machine_that_holds_it(self) -> None:
        out = io.StringIO()
        entry = ReviewEntry(
            item_id="apt:pin:99-vendor.pref",
            label="99-vendor.pref",
            action_label="delete pin file",
            content="Package: *\nPin: origin vendor.example.com\nPin-Priority: 900\n",
        )
        isatty, screen, console = self._run(_pin_removal_group([entry]), out)

        with isatty, screen:
            await review_items([_pin_removal_group([entry])], console=console, ui=MagicMock(), **HOSTS)

        printed = out.getvalue()
        assert "Pin-Priority: 900" in printed
        assert "origin vendor.example.com" in printed
        assert "On nomad" in printed
        assert "the target" not in printed

    async def test_an_entry_with_no_content_prints_no_panel(self) -> None:
        """A repository deletion carries its URLs in the detail line, so the screen it shares
        with the pin files must not grow an empty block for it."""
        out = io.StringIO()
        entry = ReviewEntry(item_id="apt:source:vendor.list", label="vendor.list (list)", action_label="delete")
        group = _pin_removal_group([entry])
        isatty, screen, console = self._run(group, out)

        with isatty, screen:
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)

        assert out.getvalue().strip() == ""

    async def test_a_bracketed_pin_body_renders_without_markup_error(self) -> None:
        out = io.StringIO()
        entry = ReviewEntry(
            item_id="apt:pin:99-vendor.pref",
            label="99-vendor.pref",
            action_label="delete pin file",
            content="Package: [bold red]not-markup[/]\n",
        )
        group = _pin_removal_group([entry])
        isatty, screen, console = self._run(group, out)

        with isatty, screen:
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)

        assert "[bold red]not-markup[/]" in out.getvalue()


@pytest.mark.asyncio
class TestCollateralPromptWording:
    """D-30's prompt, in the user's language: what is protected, what the change does to it,
    what each of the three answers costs — and how far "stop" reaches."""

    @staticmethod
    async def _titles(selected: str = "protect") -> tuple[MagicMock, str]:
        out = io.StringIO()
        console = Console(file=out, force_terminal=True, no_color=True, width=200)
        group = _collateral_group(
            [
                ReviewEntry(
                    item_id="apt:package:pkg-a",
                    label="fortunes",
                    action_label="resolve",
                    detail="Installing sl on nomad would remove fortunes",
                )
            ]
        )
        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.questionary.select",
                return_value=_fake_prompt(ask_return=selected),
            ) as select,
        ):
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)
        return select, out.getvalue()

    async def test_the_question_and_every_answer_name_the_machine_and_its_own_effect(self) -> None:
        select, _printed = await self._titles()

        assert select.call_args.args[0] == "What should happen to fortunes on nomad?"
        titles = {choice.value: choice.title for choice in select.call_args.kwargs["choices"]}
        assert titles["proceed"] == "Go ahead — fortunes changes on nomad as described above"
        assert titles["protect"] == (
            "Keep fortunes as it is — the changes that would touch it are dropped from this sync"
        )
        assert titles["abort"] == (
            "Stop the whole pc-switcher sync now — nothing more is changed on nomad, and what earlier jobs "
            "already did stays done"
        )
        assert "the target" not in " ".join(titles.values())

    async def test_the_prompt_says_why_this_package_is_protected(self) -> None:
        """Not "machine-specific" — nobody recorded anything. The target's own apt says the
        user asked for this package, which is a different fact and the true one."""
        _select, printed = await self._titles()

        assert "You asked for fortunes on nomad yourself" in printed
        assert "manually installed" in printed
        assert "Installing sl on nomad would remove fortunes" in printed

    async def test_stopping_names_the_package_and_the_machine_in_the_abort(self) -> None:
        with pytest.raises(SyncAbortedByUser) as excinfo:
            await self._titles("abort")

        assert str(excinfo.value) == (
            "fortunes on nomad would have been removed or downgraded; the whole sync was stopped in the package review"
        )


@pytest.mark.asyncio
class TestTheOrchestratorNamesBothMachines:
    """The reviewer is where the two hostnames enter the review, so it is the one place a
    missing name would turn every screen back into "the target"."""

    async def test_the_reviewer_is_built_with_both_machine_names(self) -> None:
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock(file=10, tui=20, external=30)
        config.sync_jobs = {}
        config.job_configs = {}
        config.btrfs_snapshots = MagicMock(subvolumes=["@"])
        config.disk = MagicMock(preflight_minimum="10%")

        with patch("pcswitcher.orchestrator.get_local_hostname", return_value="atlas"):
            orchestrator = Orchestrator(target="nomad", config=config)
        # `run()` builds it, along with the console and the live UI it wraps; everything
        # after the construction reaches a machine, so only that first slice is exercised.
        with (
            patch("pcswitcher.orchestrator.setup_logging", side_effect=RuntimeError("stop after construction")),
            pytest.raises(RuntimeError, match="stop after construction"),
        ):
            await orchestrator.run()

        reviewer = orchestrator._reviewer  # pyright: ignore[reportPrivateUsage]
        assert isinstance(reviewer, TerminalUIReviewer)
        assert reviewer._source_hostname == "atlas"  # pyright: ignore[reportPrivateUsage]
        assert reviewer._target_hostname == "nomad"  # pyright: ignore[reportPrivateUsage]
