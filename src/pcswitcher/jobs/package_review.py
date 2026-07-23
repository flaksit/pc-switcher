"""Batched checkbox review — the single interaction surface for every package diff (D-24).

`apt_sync`, `snap_sync` and `flatpak_sync` (plans 02-06..02-11) each compute a set of
differences against the source's manifest and hand them to `review_items` as
`ReviewGroup`s before applying anything. The user ticks items off a checkable list in one
sitting rather than answering a sequence of yes/no prompts.

This composes with the single persistent Live display (Phase 1 plans 01-17/01-18) exactly
as `TerminalUIConfirmer.confirm` (`pcswitcher.confirmer`) does: pause the live region before
the prompt, run the blocking `questionary` checkbox off the event loop via
`asyncio.to_thread` (ADR-005 — no blocking calls on the event loop), and resume it in a
`finally` so the terminal is always handed back even if the prompt raises.

Removals get their own group, never sharing a checkbox list with installs (D-07/D-24): a
bulk tick that also deletes software would be exactly the silent-destruction failure D-07
exists to prevent. Which of a caller's `ReviewGroup`s are "removal-direction" is decided by
`ReviewGroup.action` — grouping itself (turning an `ItemDiff` into `ReviewGroup`s keyed by
manager+action) is Claude's Discretion for plan 02-05, which owns the real item model; this
module only consumes already-grouped input.

`PACKAGE_REVIEW_AUTOMATION_ENV`: undocumented escape hatch for integration tests, which run
without a TTY and cannot drive a real terminal prompt. When set, its value is trusted JSON
(no schema validation) mapping item_id -> decision, applied instead of prompting. It never
widens what the review offers (D-25 items are still exactly what the caller passed in) and
is deliberately absent from `--help`, the config schema and user docs (D-26).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from pcswitcher.terminal import is_interactive

__all__ = [
    "PACKAGE_REVIEW_AUTOMATION_ENV",
    "Decision",
    "ReviewEntry",
    "ReviewGroup",
    "ReviewOutcome",
    "review_items",
]

_logger = logging.getLogger("pcswitcher.jobs.package_review")

# Undocumented on purpose (D-26): lets integration tests answer a review without a TTY.
# Never mentioned in --help, the config schema, or docs/configuration.md.
PACKAGE_REVIEW_AUTOMATION_ENV = "PCSWITCHER_PACKAGE_REVIEW_AUTOMATION"

# Canonical removal-direction action values (D-07's "remove/delete/disable" family). Any
# `ReviewGroup.action` outside this set is treated as install-direction (checked by
# default) — covers "install"/"add"/"enable" as well as "change" (converging an existing
# item to match the source is not the destructive branch a bulk tick must guard against).
_REMOVAL_ACTIONS = frozenset({"remove", "delete", "disable"})


class PausableUI(Protocol):
    """The subset of `TerminalUI` the review needs to pause/resume the live display."""

    def pause(self) -> None: ...

    def resume(self) -> None: ...


@dataclass(frozen=True)
class ReviewEntry:
    """One item awaiting a decision inside a `ReviewGroup`.

    Deliberately minimal — this module has no dependency on the real item model plan
    02-03 introduces. Plan 02-05 adapts `ItemDiff` onto this shape.
    """

    item_id: str
    label: str
    action_label: str
    detail: str | None = None


@dataclass(frozen=True)
class ReviewGroup:
    """One checkbox screen's worth of same-manager, same-direction entries.

    `action` is shaped like the `DiffAction` enum a future plan introduces (e.g.
    "install"/"remove"/"change") but stays a plain string here so this module carries no
    dependency on that type yet. `title` must name the concrete verb for the item class
    ("Remove packages", not "Apply") — the caller building the group owns that wording.
    """

    manager: str
    action: str
    title: str
    entries: Sequence[ReviewEntry]


class Decision(StrEnum):
    """The three-way outcome D-07 requires for every reviewed item."""

    APPLY = "apply"
    SKIP_ONCE = "skip_once"
    # Not reachable from review_items yet — plan 02-04 adds the second prompt that
    # promotes a skip to permanent. Carried here so that addition is additive.
    SKIP_ALWAYS = "skip_always"


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of a review: every entry's decision, plus how it was reached."""

    decisions: Mapping[str, Decision]
    was_interactive: bool


def _is_removal_direction(action: str) -> bool:
    return action in _REMOVAL_ACTIONS


def _render_group_panel(group: ReviewGroup) -> Panel:
    """Build a Panel for one group, wrapping every untrusted field in `Text`.

    Package names, versions and stderr fragments come from package-manager output and
    must never reach a `Panel` as a bare `str` — Rich would parse `[...]`-shaped
    substrings as console markup and raise `MarkupError` (T-02-02).
    """
    body = Text()
    for entry in group.entries:
        body.append(entry.action_label, style="bold")
        body.append(" ")
        body.append(entry.label)
        if entry.detail:
            body.append(" (")
            body.append(entry.detail, style="dim")
            body.append(")")
        body.append("\n")
    return Panel(body, title=Text(group.title), border_style="cyan")


def _decisions_from_automation(groups: Sequence[ReviewGroup], raw: str) -> dict[str, Decision]:
    mapping: dict[str, str] = json.loads(raw)
    return {
        entry.item_id: Decision(mapping.get(entry.item_id, Decision.SKIP_ONCE.value))
        for group in groups
        for entry in group.entries
    }


async def review_items(
    groups: Sequence[ReviewGroup],
    *,
    console: Console,
    ui: PausableUI,
    logger: logging.Logger | None = None,
) -> ReviewOutcome:
    """Present every group as a checkable list and return the user's decisions.

    Non-interactive runs (`is_interactive(console)` is False) prompt for nothing: every
    item comes back `SKIP_ONCE`, nothing is recorded permanently, and a warning names how
    many items went unresolved (D-26). Interactive runs pause `ui` around each group's
    blocking `questionary` checkbox (dispatched via `asyncio.to_thread`) and resume it in
    a `finally`, so the live display is always handed back even if the prompt raises.
    """
    log = logger if logger is not None else _logger

    automation_raw = os.environ.get(PACKAGE_REVIEW_AUTOMATION_ENV)
    if automation_raw is not None:
        return ReviewOutcome(decisions=_decisions_from_automation(groups, automation_raw), was_interactive=True)

    if not is_interactive(console):
        total = sum(len(group.entries) for group in groups)
        log.warning("%d package review item(s) left unresolved (non-interactive run)", total)
        for group in groups:
            console.print(_render_group_panel(group))
        decisions = {entry.item_id: Decision.SKIP_ONCE for group in groups for entry in group.entries}
        return ReviewOutcome(decisions=decisions, was_interactive=False)

    ui.pause()
    decisions: dict[str, Decision] = {}
    try:
        for index, group in enumerate(groups):
            console.print()
            console.print(_render_group_panel(group))
            removal = _is_removal_direction(group.action)
            choices = [
                questionary.Choice(
                    title=f"{entry.action_label} {entry.label}",
                    value=entry.item_id,
                    checked=not removal,
                )
                for entry in group.entries
            ]
            prompt = questionary.checkbox(group.title, choices=choices)
            selected = await asyncio.to_thread(prompt.ask)

            if selected is None:
                # Aborted (e.g. Ctrl-C): everything not yet decided, including the rest
                # of this group, comes back SKIP_ONCE and later groups are not shown.
                for remaining_group in groups[index:]:
                    for entry in remaining_group.entries:
                        decisions.setdefault(entry.item_id, Decision.SKIP_ONCE)
                break

            selected_ids = set(selected)
            for entry in group.entries:
                decisions[entry.item_id] = Decision.APPLY if entry.item_id in selected_ids else Decision.SKIP_ONCE
    finally:
        ui.resume()

    return ReviewOutcome(decisions=decisions, was_interactive=True)
