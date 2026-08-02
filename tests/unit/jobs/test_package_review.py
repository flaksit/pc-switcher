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
from dataclasses import fields
from typing import Any, ClassVar, TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from prompt_toolkit.keys import Keys
from rich.console import Console

from pcswitcher.config import (  # pyright: ignore[reportPrivateUsage]
    Configuration,
    _load_schema,
)
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
        """H160, J35, J36 — no terminal: no screen is built at all and every item comes back declined for this run."""
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

    async def test_warns_naming_every_item_and_reports_groups(self) -> None:
        """H161 — `PKG-FR-LOG-DECISIONS`: a count says which items were declined to nobody."""
        buffer = io.StringIO()
        console = Console(file=buffer)
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a"), _entry("b")])
        ]
        logger = MagicMock()

        with patch.object(sys, "stdin", _mock_isatty(False)):
            await review_items(groups, console=console, ui=ui, logger=logger, **HOSTS)

        assert logger.warning.call_count == 2
        labels = [call.args[2] for call in logger.warning.call_args_list]
        assert labels == ["pkg", "pkg"]
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

    async def test_a_report_line_is_the_bare_item_and_its_finding(self) -> None:
        """#226 — nothing acts on a reported condition, so the line carries no verb, and the
        version its label repeats is the one the finding attributes to each machine.
        """
        out = io.StringIO()
        console = Console(file=out, no_color=True, width=100)
        group = ReviewGroup(
            manager="apt",
            action="report_only",
            title="Version differences (apt packages)",
            entries=[
                ReviewEntry(
                    item_id="apt:package:tree",
                    label="tree (2.1.1-2ubuntu3)",
                    action_label="report",
                    detail="atlas has 2.1.1-2ubuntu3.24.04.2, nomad has 2.1.1-2ubuntu3",
                )
            ],
        )
        with patch.object(sys, "stdin", _mock_isatty(False)):
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)

        printed = out.getvalue()
        assert "tree: atlas has 2.1.1-2ubuntu3.24.04.2, nomad has 2.1.1-2ubuntu3" in printed
        assert "report tree" not in printed

    async def test_an_actionable_group_keeps_its_verb_and_version(self) -> None:
        """#226 changed the REPORT lines only: the report path a non-interactive run prints
        for a group that WOULD have acted still names the verb and the whole label.
        """
        out = io.StringIO()
        console = Console(file=out, no_color=True, width=100)
        group = ReviewGroup(
            manager="apt",
            action="install",
            title="Install apt packages",
            entries=[ReviewEntry(item_id="apt:package:sl", label="sl (5.02-1)", action_label="install")],
        )
        with patch.object(sys, "stdin", _mock_isatty(False)):
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)

        assert "install sl (5.02-1)" in out.getvalue()


@pytest.mark.asyncio
class TestInteractive:
    """Interactive runs pause/resume the live display around the blocking prompt."""

    async def test_every_row_comes_back_with_the_decision_its_screen_returned(self) -> None:
        """H112 — every row comes back carrying the decision its screen returned for it."""
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
        """H33 — The rebuild's whole point: a group is presented once, not once to apply and
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
        """H156 — a raising screen still hands the live display back, from the `finally`."""
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
        """H148 — Decision 10: Ctrl-C / EOF at a decision screen means the user wants to abort the
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
        """H98, H102 — install rows open at the act; removal rows open declined."""
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

    async def test_a_change_that_overwrites_what_the_user_wrote_starts_skipped(self) -> None:
        """H103, H104 — `PKG-FR-HARMLESS-DEFAULT`: an `/etc/apt/apt.conf.d` file the target already holds
        is the user's own work, and confirming the screen unread must not replace it. A snap
        moved to another revision starts applied on the same action — converging software
        the user asked for overwrites nothing they authored.
        """
        console = _interactive_console()
        ui = MagicMock()
        config_group = ReviewGroup(
            manager="apt",
            action="change",
            title="Update apt configuration files",
            entries=[_entry("cfg", action_label="update")],
            overwrites_authored_content=True,
        )
        snap_group = ReviewGroup(
            manager="snap", action="change", title="Change snaps", entries=[_entry("snp", action_label="change")]
        )
        prompt = _fake_prompt(ask_side_effect=[{"cfg": "skip_once"}, {"snp": "apply"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            await review_items([config_group, snap_group], console=console, ui=ui, **HOSTS)

        assert _screen_defaults(decision_list.call_args_list[0]) == {"cfg": Decision.SKIP_ONCE}
        assert _screen_defaults(decision_list.call_args_list[1]) == {"snp": Decision.APPLY}

    async def test_the_permanent_answer_says_the_user_will_not_be_asked_again(self) -> None:
        """H64, H87, H88, H89, H90 — `PKG-FR-EFFECT-NOT-MECHANISM`: what the mark stops pc-switcher doing is machinery.
        What it costs to choose is never being asked about the item again, and the two skips
        read as one set — the same act clause, then the duration.
        """
        console = _interactive_console()
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")]),
            ReviewGroup(
                manager="apt", action="remove", title="Remove packages", entries=[_entry("b", action_label="remove")]
            ),
            ReviewGroup(
                manager="apt",
                action="change",
                title="Update apt configuration files",
                entries=[_entry("c", action_label="update")],
                overwrites_authored_content=True,
            ),
        ]
        prompt = _fake_prompt(ask_side_effect=[{"a": "apply"}, {"b": "skip_once"}, {"c": "skip_once"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=prompt) as decision_list,
        ):
            await review_items(groups, console=console, ui=ui, **HOSTS)

        install_hints = [option.hint for option in decision_list.call_args_list[0].kwargs["options"]]
        removal_hints = [option.hint for option in decision_list.call_args_list[1].kwargs["options"]]
        change_hints = [option.hint for option in decision_list.call_args_list[2].kwargs["options"]]
        assert install_hints[1:] == [
            "do not install on nomad for now; will be asked again next sync",
            "do not install on nomad for good; it is atlas's own, and will not be asked again",
        ]
        assert removal_hints[1:] == [
            "keep on nomad for now; will be asked again next sync",
            "keep on nomad for good; it is nomad's own, and will not be asked again",
        ]
        # A change is the one direction whose item is on BOTH machines: the mark lands on
        # the machine that keeps its own version, which is the target.
        assert change_hints[1:] == [
            "do not update on nomad for now; will be asked again next sync",
            "do not update on nomad for good; it is nomad's own, and will not be asked again",
        ]
        assert not any("pc-switcher" in hint for hint in install_hints + removal_hints + change_hints)

    async def test_no_group_mixes_install_and_removal_entries_in_one_prompt(self) -> None:
        """H97 — Removals never share a screen with installs (D-07/D-24)."""
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
        """H83, H99 — a removal group's title says the deletion verb, so the screen says what it deletes."""
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
        """H82, H106 — "Starts at skip-once" and "is offered permanence" are two independent properties
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
            # A removal screen keeps what the machine has; "always skip" said neither
            # what happened nor to which machine.
            assert "keep for good" in _screen_words(call)
            assert _screen_defaults(call) == dict.fromkeys(_screen_defaults(call), Decision.SKIP_ONCE)
        assert outcome.decisions == dict.fromkeys(("remove", "delete", "disable"), Decision.SKIP_ALWAYS)

    async def test_repo_removal_starts_skipped_and_is_never_offered_permanence(self) -> None:
        """H100, H107, H136 — The two-answer screen (ADR-020 D-07). It is a removal direction, so it starts at
        skip-once like any other; it is NOT promotable, so the permanent answer is absent
        from its options and `SKIP_ALWAYS` is unreachable — which is what "no registry entry"
        means at this layer.
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
        assert _screen_words(decision_list.call_args) == ["delete repository", "skip now"]
        assert outcome.decisions == {"apt:source:vendor.list": Decision.SKIP_ONCE}

    async def test_a_report_only_group_is_printed_and_asks_nothing(self) -> None:
        """H67, H111, H142, J6 — Ruled by the user: both answers it used to offer changed nothing on either
        machine and recorded nothing, so the condition came back next sync whichever was
        chosen. A report is printed and the review moves on.
        """
        out = io.StringIO()
        console = Console(file=out, force_terminal=True)
        ui = MagicMock()
        group = ReviewGroup(
            manager="apt",
            action="report_only",
            title="Report apt packages",
            entries=[_entry("apt:package:tree", action_label="report")],
        )
        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()
        printed = out.getvalue()
        assert "Report apt packages" in printed and "pkg" in printed
        assert "Nothing on nomad changes" in printed
        assert outcome.decisions == {"apt:package:tree": Decision.SKIP_ONCE}


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
        """H156 — The adapter keeps `review_items`'s pause/resume `finally`: even when the
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
                    "pcswitcher.jobs.packages.review.prompt_navigation.select",
                    return_value=_fake_prompt(ask_return=selected),
                ),
            ):
                assert await self._ask(MagicMock()) is expected

    async def test_exactly_two_choices_are_offered_with_the_captions_the_caller_gave(self) -> None:
        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.prompt_navigation.select",
                return_value=_fake_prompt(ask_return=True),
            ) as select,
        ):
            await self._ask(MagicMock())

        choices = select.call_args.kwargs["choices"]
        assert [choice.title for choice in choices] == ["re-check and continue", "skip apt_sync"]

    async def test_no_tty_answers_none_without_constructing_a_prompt(self) -> None:
        """J43."""
        ui = MagicMock()
        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.prompt_navigation.select") as select,
        ):
            answer = await self._ask(MagicMock(), console=_non_interactive_console(), ui=ui)

        assert answer is None
        select.assert_not_called()
        ui.pause.assert_not_called()

    async def test_the_automation_env_hook_cannot_answer_a_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """H172 — Deliberate negative control: the review's scripted-answer hook must NOT reach
        here — no environment value can stand in for going and attaching the other machine.
        """
        monkeypatch.setenv(PACKAGE_REVIEW_AUTOMATION_ENV, "all")
        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.prompt_navigation.select") as select,
        ):
            assert await self._ask(MagicMock(), console=_non_interactive_console()) is None
        select.assert_not_called()

    async def test_ctrl_c_aborts_the_whole_sync_and_hands_the_display_back(self) -> None:
        """H154 — Ctrl-C at a machine gate ends the sync and hands the live display back."""
        ui = MagicMock()
        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.prompt_navigation.select",
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
                "pcswitcher.jobs.packages.review.prompt_navigation.select",
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
        """H164, H165, H173, J48 — mapped ids get their value, an unmapped one falls to a decline for
        this run, and no screen is built for either.
        """
        console = _non_interactive_console()
        ui = MagicMock()
        groups = [
            ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a"), _entry("b")])
        ]
        env = {PACKAGE_REVIEW_AUTOMATION_ENV: json.dumps({"a": "apply"})}

        with (
            patch.dict("os.environ", env),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()
        ui.pause.assert_not_called()
        # `b` is absent from the map and falls to a decline for this run.
        assert outcome.decisions == {"a": Decision.APPLY, "b": Decision.SKIP_ONCE}

    @pytest.mark.asyncio
    async def test_the_variable_answers_a_review_on_a_terminal_too(self) -> None:
        """H173 — the hook is read before the interactivity test, so a run WITH a terminal
        is answered from the map just as silently: no screen, and the live display is never
        paused. The accepted cost of a hook that exists for the integration suite.
        """
        console = _interactive_console()
        ui = MagicMock()
        groups = [ReviewGroup(manager="apt", action="install", title="Install packages", entries=[_entry("a")])]

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch.dict("os.environ", {PACKAGE_REVIEW_AUTOMATION_ENV: json.dumps({"a": "apply"})}),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items(groups, console=console, ui=ui, **HOSTS)

        decision_list.assert_not_called()
        ui.pause.assert_not_called()
        ui.resume.assert_not_called()
        assert outcome.decisions == {"a": Decision.APPLY}
        assert outcome.was_interactive is True

    @pytest.mark.asyncio
    async def test_malformed_automation_json_fails_loudly_and_prompts_nothing(self) -> None:
        """H168 — pins the ACTUAL behaviour: `_decisions_from_automation` hands the raw
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
        """H169 — Same contract for well-formed JSON naming a decision that does not exist: the
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
        """H170 — a hidden hook stays hidden: nothing in `sync --help` names it."""
        result = subprocess.run(
            ["uv", "run", "pc-switcher", "sync", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert PACKAGE_REVIEW_AUTOMATION_ENV not in result.stdout
        assert PACKAGE_REVIEW_AUTOMATION_ENV not in result.stderr

    def test_no_configuration_key_stands_in_for_an_answer(self) -> None:
        """H163, H171 — `PKG-NG-UNATTENDED`: consent is given at the review or not at all,
        so there is no standing-answers key. Asserted as an absence in both places a key
        would have to exist: the schema the config file is validated against, and the
        parsed `Configuration`'s own fields.

        The env var is the only thing that can answer a review, and it is deliberately not
        configurable — a config key would make unattended package syncs a supported mode.
        """
        schema = yaml.safe_dump(_load_schema())
        assert PACKAGE_REVIEW_AUTOMATION_ENV not in schema
        assert PACKAGE_REVIEW_AUTOMATION_ENV.lower() not in schema

        field_names = {f.name for f in fields(Configuration)}
        assert PACKAGE_REVIEW_AUTOMATION_ENV.lower() not in field_names
        # Nor anything else that would read as an answer given ahead of the review.
        assert not [name for name in field_names if "decision" in name or "review" in name or "answer" in name]


@pytest.mark.asyncio
class TestUnreproducibleGroupResolution:
    """D-21: an `UNREPRODUCIBLE_REVIEW_ACTION` group gets the three-way per-entry
    resolution flow (add a snippet / record machine-specific / skip for now), never a
    checkbox tick.
    """

    async def test_add_snippet_choice_captures_body_verbatim_including_whitespace(self) -> None:
        """G31."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_return={"u1": "add_snippet"})
        body = "  sudo dpkg --install /tmp/x.deb\n\nsudo apt-get install --fix-broken --assume-yes\n"
        text_prompt = _fake_prompt(ask_return=body)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.snippets == {"u1": body}
        assert "u1" not in outcome.unresolved

    async def test_the_authoring_warning_is_read_before_the_editor_opens(self) -> None:
        """G61 — the user is warned while they can still act on it. A snippet that asks a
        question does not fail on nomad, it HANGS there with nobody to answer, so the
        warning is worth nothing once the body is written: it is captured at the moment
        `questionary.text` is constructed, which is the moment the editor opens.

        The worked shape is asserted too — telling someone their command must not prompt
        without showing them what that looks like leaves them to discover
        `DEBIAN_FRONTEND` as a stuck sync.
        """
        sink = io.StringIO()
        console = Console(file=sink, force_terminal=True, no_color=True, width=200)
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_return={"u1": "add_snippet"})
        text_prompt = _fake_prompt(ask_return="sudo dpkg --install /tmp/x.deb")
        shown_when_the_editor_opened = ""

        def open_editor(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal shown_when_the_editor_opened
            shown_when_the_editor_opened = sink.getvalue()
            return text_prompt

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            patch("pcswitcher.jobs.packages.review.questionary.text", side_effect=open_editor),
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        assert "nomad" in shown_when_the_editor_opened
        assert "nobody watching" in shown_when_the_editor_opened
        assert "asks a question" in shown_when_the_editor_opened
        assert "hangs the" in shown_when_the_editor_opened
        assert "DEBIAN_FRONTEND=noninteractive" in shown_when_the_editor_opened

    async def test_skip_always_choice_yields_skip_always_decision_and_no_snippet(self) -> None:
        """G32."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_return={"u1": "skip_always"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.decisions["u1"] == Decision.SKIP_ALWAYS
        assert outcome.snippets == {}
        assert "u1" not in outcome.unresolved

    async def test_explicit_skip_once_is_a_resolution_not_unresolved(self) -> None:
        """G33, H115 — D-21: an explicit "Skip for now" is a real decision, so the item is resolved
        for this run and left OUT of `unresolved`."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_return={"u1": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.decisions["u1"] == Decision.SKIP_ONCE
        assert "u1" not in outcome.unresolved

    async def test_cancelled_select_aborts_the_entire_sync(self) -> None:
        """G38, H149 — Decision 10: a cancelled select (`None`, i.e. Ctrl-C / EOF) means the user wants
        to abort the whole sync, not skip this one item."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_return=None)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            pytest.raises(SyncAbortedByUser, match="brscan3"),
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        ui.resume.assert_called_once()

    async def test_empty_snippet_body_reprompts_until_a_real_choice(self) -> None:
        """G39 — Decision 10: an empty snippet capture is NOT accepted and does NOT fall through
        to 'unresolved' — the three-way choice is re-prompted until the user gives a real
        snippet or an explicit skip. Here the user submits an empty body, then chooses
        skip-once on the re-prompt."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        # First select -> add_snippet (empty body), second select -> skip_once.
        screen = _fake_prompt(ask_side_effect=[{"u1": "add_snippet"}, {"u1": "skip_once"}])
        text_prompt = _fake_prompt(ask_return="")  # empty submission

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        # The empty body was rejected; the re-prompted skip-once is the real resolution.
        assert outcome.snippets == {}
        assert outcome.decisions["u1"] == Decision.SKIP_ONCE
        assert outcome.unresolved == ()

    async def test_empty_snippet_then_real_snippet_is_captured(self) -> None:
        """G41 — Decision 10: after an empty submission the user may re-choose add-snippet and
        supply a real body, which is then captured verbatim."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        body = "sudo dpkg --install /tmp/x.deb"
        screen = _fake_prompt(ask_side_effect=[{"u1": "add_snippet"}, {"u1": "add_snippet"}])
        text_prompt = _fake_prompt(ask_side_effect=["", body])  # empty, then real

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.snippets == {"u1": body}
        assert outcome.unresolved == ()

    async def test_a_whitespace_only_snippet_is_not_a_resolution(self) -> None:
        """G40 — A body of spaces and newlines replays as nothing at all, so accepting it would
        record a "snippet" that resolves the item without installing anything."""
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_side_effect=[{"u1": "add_snippet"}, {"u1": "skip_once"}])
        text_prompt = _fake_prompt(ask_return="   \n\t\n  ")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
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
        screen = _fake_prompt(ask_return={"u1": "add_snippet"})
        text_prompt = _fake_prompt(ask_return="sudo dpkg --install /tmp/x.deb")

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt) as text,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        assert "Ctrl-D" in text.call_args.kwargs["instruction"]
        assert "Esc" not in text.call_args.kwargs["instruction"]
        bound = {key for binding in text.call_args.kwargs["key_bindings"].bindings for key in binding.keys}
        assert Keys.ControlD in bound

    async def test_the_three_answers_read_as_they_do_on_every_other_screen(self) -> None:
        """H61, H81, H92 — Same keys, same order, same words as an install screen — the act first, then the
        one that lasts a sync, then the one that is recorded.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_return={"u1": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        options = decision_list.call_args.kwargs["options"]
        assert [(option.key, option.word) for option in options] == [
            ("y", "install"),
            ("s", "skip now"),
            ("x", "never install"),
        ]
        assert options[0].hint == "write a command snippet that installs it; nomad runs it"
        assert options[1].hint == "do not install on nomad for now; will be asked again next sync"
        assert options[2].hint == "do not install on nomad for good; it is atlas's own, and will not be asked again"
        assert decision_list.call_args.args[0] == "How should nomad get brscan3?"

    async def test_ui_resumed_when_snippet_capture_raises(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_return={"u1": "add_snippet"})
        text_prompt = _fake_prompt(ask_side_effect=KeyboardInterrupt)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            patch("pcswitcher.jobs.packages.review.questionary.text", return_value=text_prompt),
            pytest.raises(KeyboardInterrupt),
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_non_interactive_offers_no_capture_and_marks_every_item_unresolved(self) -> None:
        """G46, J38."""
        console = _non_interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3"), _entry("u2", label="cnpg")])

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.decision_list") as screen_mock,
            patch("pcswitcher.jobs.packages.review.questionary.text") as text_mock,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        screen_mock.assert_not_called()
        text_mock.assert_not_called()
        assert outcome.snippets == {}
        assert set(outcome.unresolved) == {"u1", "u2"}
        assert outcome.was_interactive is False

    async def test_the_row_starts_at_writing_a_snippet(self) -> None:
        """H110 — `PKG-FR-HARMLESS-DEFAULT`: an unreproducible item is an install, and an
        install displaces nothing, so the row opens at the act. The act here is not a
        `Decision` at all — it opens the snippet editor — which is why the default is
        matched against the act option rather than against a decision value.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3")])
        screen = _fake_prompt(ask_return={"u1": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        act = next(option for option in decision_list.call_args.kwargs["options"] if option.is_act)
        assert _screen_defaults(decision_list.call_args) == {"u1": act.value}

    async def test_each_item_gets_a_decision_screen_of_its_own(self) -> None:
        """G42, H40 — ruled by the user: one question per item, because answering "install" opens
        an editor for that item and a later item may need different words — but in the format
        every other screen uses, not a picker of sentences.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _unreproducible_group([_entry("u1", label="brscan3"), _entry("u2", label="cnpg")])
        screen = _fake_prompt(ask_side_effect=[{"u1": "skip_once"}, {"u2": "skip_once"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        assert decision_list.call_count == 2
        assert [len(call.kwargs["rows"]) for call in decision_list.call_args_list] == [1, 1]


@pytest.mark.asyncio
class TestCollateralGroupResolution:
    """D-30: a `COLLATERAL_REVIEW_ACTION` group gets the three-way per-entry flow
    (go ahead / keep the package / stop the sync), recorded against `entry.item_id` (which the caller,
    `AptSyncJob`, maps onto the triggering install), never a checkbox tick.
    """

    async def test_go_ahead_records_apply(self) -> None:
        """H28 — letting the collateral happen records the act against the entry."""
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        screen = _fake_prompt(ask_return={"apt:package:pkg-a": "apply"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.decisions["apt:package:pkg-a"] == Decision.APPLY

    async def test_skip_records_skip_once(self) -> None:
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        screen = _fake_prompt(ask_return={"apt:package:pkg-a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.decisions["apt:package:pkg-a"] == Decision.SKIP_ONCE

    async def test_abort_raises_sync_aborted_by_user_naming_the_collateral_package(self) -> None:
        """D26, H153 — the explicit "stop the sync" answer ends the run, naming the package."""
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        screen = _fake_prompt(ask_return={"apt:package:pkg-a": "stop_sync"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            pytest.raises(SyncAbortedByUser, match="other-manual"),
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        ui.pause.assert_called_once()
        ui.resume.assert_called_once()

    async def test_ctrl_c_at_a_collateral_screen_aborts_the_whole_sync(self) -> None:
        """H151 — Ctrl-C is not the same gesture as the "stop the sync" answer, and it
        reaches a different line of code: `_ask_about_one_item` sees `None` from the prompt
        and raises before this group's own handling ever runs. It must still end the sync
        rather than fall through as a decline, and it names the package the screen was
        about.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group(
            [_entry("apt:package:pkg-a", label="other-manual"), _entry("apt:package:pkg-b", label="second-manual")]
        )
        screen = _fake_prompt(ask_return=None)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
            pytest.raises(SyncAbortedByUser, match="other-manual"),
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        # The second package is never put on a screen: the abort stops the whole review.
        assert decision_list.call_count == 1
        ui.resume.assert_called_once()

    async def test_the_row_starts_at_keeping_the_package(self) -> None:
        """H109 — `PKG-FR-HARMLESS-DEFAULT`: confirming this screen unread must protect the
        package, so the row opens on the skip and not on the act that loses it.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])
        screen = _fake_prompt(ask_return={"apt:package:pkg-a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        assert _screen_defaults(decision_list.call_args) == {"apt:package:pkg-a": Decision.SKIP_ONCE}
        act = next(option for option in decision_list.call_args.kwargs["options"] if option.is_act)
        assert decision_list.call_args.kwargs["rows"][0].default != act.value

    async def test_bracketed_collateral_label_renders_without_markup_error(self) -> None:
        """D27 — T-02-02: a collateral package name containing bracket characters must not reach
        a Rich `Panel`/console as a bare `str`, or markup parsing raises `MarkupError`.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="pkg[weird]name")])
        screen = _fake_prompt(ask_return={"apt:package:pkg-a": "skip_once"})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        assert outcome.decisions["apt:package:pkg-a"] == Decision.SKIP_ONCE

    async def test_each_package_gets_a_decision_screen_of_its_own(self) -> None:
        """D18, H39 — One question per package — the causes and effects differ per item, so one legend
        could not phrase them — in the format every other screen uses.
        """
        console = _interactive_console()
        ui = MagicMock()
        group = _collateral_group(
            [_entry("apt:package:pkg-a", label="other-manual"), _entry("apt:package:pkg-b", label="second-manual")]
        )
        screen = _fake_prompt(ask_side_effect=[{"apt:package:pkg-a": "skip_once"}, {"apt:package:pkg-b": "skip_once"}])

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        assert decision_list.call_count == 2
        assert [len(call.kwargs["rows"]) for call in decision_list.call_args_list] == [1, 1]

    async def test_non_interactive_collateral_entries_skip_once_and_are_not_unresolved(self) -> None:
        """D28, J40 — D-26: without a TTY a collateral entry comes back SKIP_ONCE like every other
        item (the install it gates is simply not approved) and is never flagged unresolved
        — that status is reserved for unreproducible items.
        """
        console = _non_interactive_console()
        ui = MagicMock()
        group = _collateral_group([_entry("apt:package:pkg-a", label="other-manual")])

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.decision_list") as screen_mock,
        ):
            outcome = await review_items([group], console=console, ui=ui, **HOSTS)

        screen_mock.assert_not_called()
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
            "vendor.list is different on the two machines, and nomad installs curl from it — package you "
            "marked as specific to nomad, so a sync normally leaves it alone"
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
        """C31, H108, H116, H138 — An overwrite displaces software the target explicitly marked machine-specific,
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
        # `<s>` means "skip now" on every screen of the review; which version survives is
        # said in the answer's hint, not by giving this one screen its own second word.
        assert _screen_words(decision_list.call_args) == ["overwrite", "skip now"]
        assert _screen_defaults(decision_list.call_args) == {"apt:conflict:vendor.list": Decision.SKIP_ONCE}

    async def test_each_conflicting_file_is_answered_right_after_it_is_shown(self) -> None:
        """C32, H38 — Ruled by the user, replacing the batch: two whole file bodies per entry meant a
        batched screen asked about definitions that had already scrolled off.
        """
        console = _interactive_console()
        ui = MagicMock()
        first = _conflict_entry()
        second = ReviewEntry(
            item_id="apt:conflict:other.list",
            label="other.list",
            action_label="overwrite",
            versions=("a\n", "b\n"),
        )
        screen = _fake_prompt(
            ask_side_effect=[{"apt:conflict:vendor.list": "apply"}, {"apt:conflict:other.list": "skip_once"}]
        )

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            outcome = await review_items([_conflict_group([first, second])], console=console, ui=ui, **HOSTS)

        assert decision_list.call_count == 2
        assert [len(call.kwargs["rows"]) for call in decision_list.call_args_list] == [1, 1]
        assert outcome.decisions == {
            "apt:conflict:vendor.list": Decision.APPLY,
            "apt:conflict:other.list": Decision.SKIP_ONCE,
        }

    async def test_both_whole_versions_are_shown_and_no_unified_diff(self) -> None:
        """C29, H94 — The user's own words: a diff of two repository definitions is not readable. The
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
        """C30, H65 — The user's ruling: no screen says "the target". The two panels are titled with the
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
        """C40 — T-02-02: neither the filename nor either file body may reach Rich as a bare
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
        """C41, H150 — Ctrl-C at a repository-conflict screen ends the sync, naming the file."""
        console = _interactive_console()
        ui = MagicMock()
        screen = _fake_prompt(ask_return=None)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
            pytest.raises(SyncAbortedByUser, match=r"vendor\.list"),
        ):
            await review_items([_conflict_group([_conflict_entry()])], console=console, ui=ui, **HOSTS)

        ui.resume.assert_called_once()

    async def test_non_interactive_conflict_entries_skip_once_and_are_not_unresolved(self) -> None:
        """C42, J41."""
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


@pytest.mark.asyncio
class TestAnswerSentencesNameTheMachineAsASet:
    """`PKG-FR-ANSWERS-AS-A-SET`: the answers on one screen are read together, so the
    machine is named in every sentence or in none of them. A set where one answer says
    "nomad" and its neighbour says nothing reads as though only the first one happens
    there.

    Asserted over every screen kind at once rather than incidentally per screen, because
    the rule is about the SET and no single-screen assertion can state it.
    """

    _GROUPS: ClassVar[dict[str, ReviewGroup]] = {
        "install": ReviewGroup(manager="apt", action="install", title="Install apt packages", entries=(_entry("a"),)),
        "change": ReviewGroup(
            manager="snap",
            action="change",
            title="Change snaps",
            entries=(_entry("a", action_label="change"),),
        ),
        "remove": ReviewGroup(
            manager="apt",
            action="remove",
            title="Remove apt packages",
            entries=(_entry("a", action_label="remove"),),
        ),
        "repo_conflict": ReviewGroup(
            manager="apt",
            action=REPO_CONFLICT_REVIEW_ACTION,
            title="Resolve apt repository conflicts",
            entries=(_entry("a", label="vendor.list", action_label="overwrite"),),
        ),
        "repo_removal": ReviewGroup(
            manager="apt",
            action=REPO_REMOVAL_REVIEW_ACTION,
            title="Delete pin files atlas no longer has (apt)",
            entries=(_entry("a", label="99-vendor.pref", action_label="delete"),),
        ),
        "collateral": ReviewGroup(
            manager="apt",
            action=COLLATERAL_REVIEW_ACTION,
            title="Resolve apt manual-collateral removals",
            entries=(_entry("a", label="fortunes", action_label="install sl anyway"),),
        ),
        "unreproducible": ReviewGroup(
            manager="apt",
            action=UNREPRODUCIBLE_REVIEW_ACTION,
            title="Resolve apt items with no reproducible install",
            entries=(_entry("a", label="brscan3"),),
        ),
    }

    @staticmethod
    async def _hints(group: ReviewGroup) -> list[str]:
        console = _interactive_console()
        screen = _fake_prompt(ask_return={entry.item_id: "skip_once" for entry in group.entries})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
        ):
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)

        return [option.hint for option in decision_list.call_args.kwargs["options"]]

    @pytest.mark.parametrize("kind", list(_GROUPS))
    async def test_every_answer_on_one_screen_names_a_machine_or_none_of_them_does(self, kind: str) -> None:
        """H93, H96 — the two hostnames are the only machine words a hint may use, so a
        hint that names neither is the odd one out whichever way the screen went.
        """
        hints = await self._hints(self._GROUPS[kind])

        naming = [hint for hint in hints if "nomad" in hint or "atlas" in hint]
        assert naming in ([], hints), hints

    async def test_the_repository_conflict_screen_says_which_version_survives_a_skip(self) -> None:
        """H93 — two answers, so the skip cannot be read as "nothing happens": it says whose
        version stays and that the question returns next sync.
        """
        act_hint, skip_hint = await self._hints(self._GROUPS["repo_conflict"])

        assert "nomad" in act_hint
        assert "keep nomad's version" in skip_hint
        assert "will be asked again next sync" in skip_hint


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
        """J39."""
        context = _unresolved_job_context()
        job = _FakeUnreproducibleJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=False, unresolved=(diff.item_id,)))

        await job.apply()  # must not raise

    async def test_dry_run_unresolved_does_not_raise_on_that_basis_alone(self) -> None:
        """J62."""
        context = _unresolved_job_context(dry_run=True)
        job = _FakeUnreproducibleJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        plan = PackagePlan(manager="fake", diffs=(diff,), groups=())
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True, unresolved=(diff.item_id,)))

        await job.apply()  # must not raise


@pytest.mark.asyncio
class TestAutomationEnvCannotResolveAnUnreproducibleItem:
    """`PKG-NG-AUTOMATION-ENV`: the map carries decisions and nothing else.

    An unreproducible item has two resolutions the map cannot tell apart from the outside —
    a permanent answer and an authored install snippet — and only one of them is expressible
    as a decision. Driven end to end rather than asserted on `_decisions_from_automation`,
    because what the article promises is about what the run leaves on disk.
    """

    async def test_a_permanent_answer_from_the_map_marks_the_item_and_writes_no_snippet(self) -> None:
        """H167 — the machine-specific mark is written and no snippet is: authoring one takes an
        editor, which the map has no way to stand in for.
        """
        context = _unresolved_job_context()
        job = _FakeUnreproducibleJob(context)
        diff = _unreproducible_diff("unreproducible:apt-no-candidate:brscan3")
        group = _unreproducible_group([_entry(diff.item_id, label=diff.label, action_label="resolve")])

        with (
            patch.dict("os.environ", {PACKAGE_REVIEW_AUTOMATION_ENV: json.dumps({diff.item_id: "skip_always"})}),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            outcome = await review_items([group], console=_non_interactive_console(), ui=MagicMock(), **HOSTS)

        decision_list.assert_not_called()
        assert outcome.decisions == {diff.item_id: Decision.SKIP_ALWAYS}
        assert outcome.snippets == {}

        job.accept_review(PackagePlan(manager="fake", diffs=(diff,), groups=(group,)), outcome)
        await job.apply()

        written = [call.args[0] for call in context.source.run_command.call_args_list]  # pyright: ignore[reportAttributeAccessIssue]
        assert any("mv --force" in cmd and "fake.decisions" in cmd for cmd in written)
        assert not any("package-snippets" in cmd and "mv --force" in cmd for cmd in written)


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
        """H37, H66 — a pin offered for deletion is printed whole, titled with the machine that holds it."""
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

        # Its name, and nothing else: no panel where there is no file body to show.
        assert "vendor.list (list)" in out.getvalue()
        assert "╭" not in out.getvalue()

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

    async def test_ctrl_c_at_a_deletion_screen_aborts_the_whole_sync(self) -> None:
        """H152 — a deletion screen goes through the same `_ask_about_one_item` as the
        collateral one, so Ctrl-C here must end the sync and name the file rather than be
        read as declining this one deletion and moving on to the next file.
        """
        out = io.StringIO()
        console = Console(file=out, force_terminal=True, no_color=True, width=200)
        group = _pin_removal_group(
            [
                ReviewEntry(
                    item_id="apt:pin:99-vendor.pref",
                    label="99-vendor.pref",
                    action_label="delete pin file",
                    content="Package: *\nPin-Priority: 900\n",
                ),
                ReviewEntry(item_id="apt:pin:98-other.pref", label="98-other.pref", action_label="delete pin file"),
            ]
        )
        ui = MagicMock()
        screen = _fake_prompt(ask_return=None)

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen) as decision_list,
            pytest.raises(SyncAbortedByUser, match=r"99-vendor\.pref"),
        ):
            await review_items([group], console=console, ui=ui, **HOSTS)

        # The second file is never reached, and neither deletion is answered.
        assert decision_list.call_count == 1
        assert "98-other.pref" not in out.getvalue()
        ui.resume.assert_called_once()


_SECRET_URL = "https://bearer:s3cr3t-token@packages.example.com/apt"
_SAFE_URL = "https://***@packages.example.com/apt"


@pytest.mark.asyncio
class TestCredentialsInPrintedFileBodies:
    """`PKG-FR-CREDENTIAL-PRIVACY`: a whole file body a question prints is printed redacted.

    A private PPA or a commercial repository carries its credential inside its own address,
    and these two bodies reach the terminal without passing any other redaction exit
    (ADR-021).
    """

    @staticmethod
    async def _printed(group: ReviewGroup) -> str:
        out = io.StringIO()
        console = Console(file=out, force_terminal=True, no_color=True, width=200)
        screen = _fake_prompt(ask_return={entry.item_id: "skip_once" for entry in group.entries})

        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch("pcswitcher.jobs.packages.review.decision_list", return_value=screen),
        ):
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)
        return out.getvalue()

    async def test_neither_version_of_a_conflicting_repository_shows_the_credential(self) -> None:
        """C37, J123."""
        entry = _conflict_entry(
            target_version=f"deb {_SECRET_URL} stable main\n", source_version=f"URIs: {_SECRET_URL}\n"
        )

        printed = await self._printed(_conflict_group([entry]))

        assert "s3cr3t-token" not in printed
        assert printed.count(_SAFE_URL) == 2

    async def test_a_pin_file_offered_for_deletion_shows_no_credential(self) -> None:
        """C116, J124."""
        entry = ReviewEntry(
            item_id="apt:pin:99-vendor.pref",
            label="99-vendor.pref",
            action_label="delete pin file",
            content=f"Package: *\nPin: origin {_SECRET_URL}\nPin-Priority: 900\n",
        )

        printed = await self._printed(_pin_removal_group([entry]))

        assert "s3cr3t-token" not in printed
        assert _SAFE_URL in printed


@pytest.mark.asyncio
class TestCredentialsInAReviewLine:
    """`PKG-FR-CREDENTIAL-PRIVACY`: an item's own line is withheld on the screen that prints
    it, not only in the value the line was built from.

    Driven through the non-interactive path, which is the one where this module renders a
    review line itself (`_render_group_panel`). An answered screen composes its rows in
    `decision_list`, which every test here stubs out, so the panel is where a rendered line
    can be read at all.
    """

    @staticmethod
    async def _printed(group: ReviewGroup) -> str:
        out = io.StringIO()
        console = Console(file=out, no_color=True, width=200)

        with (
            patch.object(sys, "stdin", _mock_isatty(False)),
            patch("pcswitcher.jobs.packages.review.decision_list") as decision_list,
        ):
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)

        decision_list.assert_not_called()
        return out.getvalue()

    async def test_a_credentialed_label_and_detail_reach_the_screen_withheld(self) -> None:
        """J122 — a review item naming a credentialed repository shows the address without its
        userinfo, in both the strings the user decides from.
        """
        group = ReviewGroup(
            manager="apt",
            action="install",
            title="Install apt packages",
            entries=[
                ReviewEntry(
                    item_id="apt:package:vendor-tool",
                    label=f"vendor-tool ({_SECRET_URL})",
                    action_label="install",
                    detail=f"nomad would get it from {_SECRET_URL}",
                )
            ],
        )

        printed = await self._printed(group)

        assert "s3cr3t-token" not in printed
        assert printed.count(_SAFE_URL) == 2


_COLLATERAL_DETAIL = (
    "Installing sl on nomad would remove fortunes\n"
    "apt on nomad has fortunes marked as manually installed: something asked for it there directly, rather "
    "than it arriving as another package's dependency."
)
"""One collateral item's detail as `Collateral` composes it: the finding, then the ground that
protects this package."""


@pytest.mark.asyncio
class TestCollateralPromptWording:
    """D-30's prompt, in the user's language: what is protected, what the change does to it,
    what each of the three answers costs — and how far "stop" reaches."""

    @staticmethod
    async def _titles(selected: str = "skip_once") -> tuple[MagicMock, str]:
        out = io.StringIO()
        console = Console(file=out, force_terminal=True, no_color=True, width=200)
        group = _collateral_group(
            [
                ReviewEntry(
                    item_id="apt:package:pkg-a",
                    label="fortunes",
                    action_label="remove",
                    detail=_COLLATERAL_DETAIL,
                    answer_hints=(
                        "install sl on nomad, so fortunes is removed as well",
                        "keep fortunes on nomad; sl will not be installed; will be asked again next sync",
                    ),
                )
            ]
        )
        with (
            patch.object(sys, "stdin", _mock_isatty(True)),
            patch(
                "pcswitcher.jobs.packages.review.decision_list",
                return_value=_fake_prompt(ask_return={"apt:package:pkg-a": selected}),
            ) as decision_list,
        ):
            await review_items([group], console=console, ui=MagicMock(), **HOSTS)
        return decision_list, out.getvalue()

    async def test_every_answer_names_the_machine_and_its_own_effect(self) -> None:
        """D22, H64, H91 — The act and skip sentences come from the ENTRY: they name the change that causes
        the collateral, which differs per item — an install here, a removal on the next
        screen — so no screen-wide legend could state them.
        """
        decision_list, _printed = await self._titles()

        options = {option.value: option for option in decision_list.call_args.kwargs["options"]}
        assert options["apply"].word == "remove"
        assert options["apply"].hint == "install sl on nomad, so fortunes is removed as well"
        assert options["skip_once"].word == "skip now"
        assert options["skip_once"].hint == (
            "keep fortunes on nomad; sl will not be installed; will be asked again next sync"
        )
        assert options["stop_sync"].word == "stop the sync"
        assert options["stop_sync"].hint == (
            "nothing more is changed on nomad; what earlier jobs already did stays done"
        )
        assert "the target" not in " ".join(option.hint for option in options.values())

    async def test_the_reason_is_the_item_own_and_the_review_adds_nothing(self) -> None:
        """H94 — Why the package is protected comes from the item: `Collateral.protected` is a union
        of the target's manual set and its marks, so a sentence composed here would be false
        about a package a mark alone protects.
        """
        decision_list, _printed = await self._titles()

        assert decision_list.call_args.kwargs["explanation"] == _COLLATERAL_DETAIL.split("\n")[1]

    async def test_the_screen_asks_this_package_own_case_above_the_legend(self) -> None:
        """#227 — the question is one package, so its title is that package's own case and the
        ground for it sits between the title and the keys, never as an annotation on a row
        the user has not answered yet.
        """
        decision_list, _printed = await self._titles()

        assert decision_list.call_args.args[0] == "Installing sl on nomad would remove fortunes"
        assert [row.detail for row in decision_list.call_args.kwargs["rows"]] == [""]

    async def test_stopping_names_the_package_and_the_machine_in_the_abort(self) -> None:
        """D26, H68 — the abort message names the package and the machine, never a role."""
        with pytest.raises(SyncAbortedByUser) as excinfo:
            await self._titles("stop_sync")

        assert str(excinfo.value) == (
            "fortunes on nomad would have been removed or downgraded; the whole sync was stopped in the package review"
        )


@pytest.mark.asyncio
class TestTheOrchestratorNamesBothMachines:
    """The reviewer is where the two hostnames enter the review, so it is the one place a
    missing name would turn every screen back into "the target"."""

    async def test_the_reviewer_is_built_with_both_machine_names(self) -> None:
        """H63 — both hostnames are required to build the reviewer; there is no default."""
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
