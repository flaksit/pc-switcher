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

A `ReviewGroup` whose `action` is `UNREPRODUCIBLE_REVIEW_ACTION` gets a different
interaction shape from every other group (D-21): instead of a checkbox tick, each entry
is resolved one at a time with a three-way choice — add an install snippet, record it as
machine-specific (skip always), or skip for now — because "should this apply" is not the
question for an item no package manager can reproduce; "how does this get resolved" is.
`ReviewOutcome.snippets`/`unresolved` carry that group's results back to the caller
(`PackageSyncJob.apply()`), which persists snippets/decisions and fails the job when
anything is left unresolved after an interactive review (D-21, D-27).

A `ReviewGroup` whose `action` is `COLLATERAL_REVIEW_ACTION` likewise gets its own
interaction shape (D-30): each entry is a manually-installed package the pending apt
transaction would remove or downgrade, resolved one at a time with a three-way choice —
install anyway, skip, or abort. The decision is recorded against the entry's `item_id`
(the triggering install, set by the caller), so install-anyway proceeds with that install,
skip leaves it unapproved, and abort raises `SyncAbortedByUser` naming the collateral
package. A non-interactive run leaves every collateral entry `SKIP_ONCE` like every other
item, so the install it gates is simply not approved (D-26).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from pcswitcher.models import SyncAbortedByUser
from pcswitcher.terminal import is_interactive

__all__ = [
    "COLLATERAL_REVIEW_ACTION",
    "PACKAGE_REVIEW_AUTOMATION_ENV",
    "UNREPRODUCIBLE_REVIEW_ACTION",
    "Decision",
    "ReviewEntry",
    "ReviewGroup",
    "ReviewOutcome",
    "Reviewer",
    "TerminalUIReviewer",
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

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group
# of unreproducible items (D-18/D-21) as needing the three-way per-entry resolution flow
# below, rather than the ordinary checkbox tick. Not a `DiffAction` value — this is a
# `package_review`-owned interaction kind, independent of the underlying diff's own
# `action` (which stays `REPORT_ONLY`/`INSTALL` per D-25's taxonomy).
UNREPRODUCIBLE_REVIEW_ACTION = "unreproducible"

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group
# of manual-collateral items (D-30) as needing the three-way per-entry resolution flow
# below — install-anyway / skip / abort — rather than an ordinary checkbox tick. A
# manual-collateral item is a manually-installed package the pending apt transaction would
# remove or downgrade; whether to lose it is not a yes/no the checkbox path expresses, so
# it gets its own prompt (sibling to `UNREPRODUCIBLE_REVIEW_ACTION`). Install-anyway records
# `Decision.APPLY` against `ReviewEntry.item_id`, skip records `Decision.SKIP_ONCE`, and
# abort raises `SyncAbortedByUser` naming the collateral package. The caller maps that
# recorded decision onto the triggering install (`AptSyncJob.accept_review`): APPLY lets the
# install proceed and allows the collateral removal, SKIP_ONCE leaves the install unapproved.
COLLATERAL_REVIEW_ACTION = "collateral"


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
    # Reachable two ways: the unreproducible group's "record as machine-specific" choice
    # below (plan 02-07), and hand-constructed `ReviewOutcome`s elsewhere (plan 02-04's
    # `PackageSyncJob.apply()`/`_record_permanent_skips`). No ordinary checkbox tick
    # produces this value — D-07's three-way decision needs its own dedicated prompt to
    # promote a skip to permanent, not a fourth checkbox state.
    SKIP_ALWAYS = "skip_always"


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of a review: every entry's decision, plus how it was reached.

    `snippets` (item_id -> body, D-20) and `unresolved` (item ids, D-21) are populated
    only by an `UNREPRODUCIBLE_REVIEW_ACTION` group's per-entry resolution; every other
    group leaves both at their empty defaults, so callers constructing a `ReviewOutcome`
    by hand (tests, and `PackageSyncJob.apply()`'s decision handling) are unaffected.
    """

    decisions: Mapping[str, Decision]
    was_interactive: bool
    snippets: Mapping[str, str] = field(default_factory=dict)
    unresolved: tuple[str, ...] = ()


def _is_removal_direction(action: str) -> bool:
    return action in _REMOVAL_ACTIONS


def _is_unreproducible_group(action: str) -> bool:
    return action == UNREPRODUCIBLE_REVIEW_ACTION


def _is_collateral_group(action: str) -> bool:
    return action == COLLATERAL_REVIEW_ACTION


# Printed once before the multi-line capture, so a user does not author a snippet that
# hangs the sync (T-02-18): the executor supplies no stdin, and a worked shape showing
# the DEBIAN_FRONTEND=noninteractive + dependency-fix pattern is cheaper to read here
# than to discover as a stuck sync.
_SNIPPET_AUTHORING_NOTE = (
    "This snippet replays non-interactively on the target — no stdin is available, so a\n"
    "command that prompts (e.g. a debconf question) will hang the sync rather than fail.\n"
    "A typical shape:\n\n"
    "  sudo DEBIAN_FRONTEND=noninteractive dpkg -i /path/to/package.deb || \\\n"
    "  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -f\n"
)


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


async def _review_unreproducible_group(
    group: ReviewGroup,
    *,
    console: Console,
    decisions: dict[str, Decision],
    snippets: dict[str, str],
    unresolved: list[str],
) -> None:
    """Resolve one `UNREPRODUCIBLE_REVIEW_ACTION` group's entries, one at a time, with
    the three-way choice D-21 requires: add an install snippet, record as
    machine-specific, or skip for now. Never a checkbox tick — a checkbox answers
    "should this apply", but an unreproducible item's question is "how does this get
    resolved", which is not a yes/no.

    An entry that ends up neither snippet-authored nor skip-always'd (skip-once, an
    aborted/empty snippet capture, or a cancelled select) is appended to `unresolved` —
    the caller (`PackageSyncJob.apply()`) reports every such item and fails the job
    after an interactive review (D-21, D-27).
    """
    for entry in group.entries:
        console.print()
        console.print(Text(entry.label, style="bold"))
        if entry.detail:
            console.print(Text(entry.detail, style="dim"))

        choice_prompt = questionary.select(
            f"How should {entry.label} be resolved?",
            choices=[
                questionary.Choice(title="Add an install snippet", value="add_snippet"),
                questionary.Choice(title="Record as machine-specific (skip always)", value="skip_always"),
                questionary.Choice(title="Skip for now", value="skip_once"),
            ],
        )
        selected = await asyncio.to_thread(choice_prompt.ask)

        if selected == "skip_always":
            decisions[entry.item_id] = Decision.SKIP_ALWAYS
            continue

        if selected == "add_snippet":
            console.print(Text(_SNIPPET_AUTHORING_NOTE, style="dim"))
            body_prompt = questionary.text(
                f"Install snippet for {entry.label} (Esc then Enter to finish):", multiline=True
            )
            # Stored verbatim, never stripped — D-20 forbids reasoning about the body,
            # and leading whitespace/newlines are the user's own formatting choice.
            body = await asyncio.to_thread(body_prompt.ask)
            if body:
                snippets[entry.item_id] = body
                continue

        # selected is "skip_once", None (the select was cancelled), or the snippet
        # capture came back empty/None — none of these permanently resolve the item.
        decisions[entry.item_id] = Decision.SKIP_ONCE
        unresolved.append(entry.item_id)


async def _review_collateral_group(
    group: ReviewGroup,
    *,
    console: Console,
    decisions: dict[str, Decision],
) -> None:
    """Resolve one `COLLATERAL_REVIEW_ACTION` group's entries, one at a time, with the
    three-way choice D-30 requires for a manually-installed package the pending apt
    transaction would remove or downgrade: install anyway, skip, or abort. Never a
    checkbox tick — losing a package the user chose to have is not the same yes/no as
    ticking an install off a list.

    The decision is recorded against `entry.item_id`: install-anyway records
    `Decision.APPLY`, skip records `Decision.SKIP_ONCE`. The caller (`AptSyncJob`) maps
    that onto the triggering install — APPLY lets the install proceed and allows the
    collateral removal, SKIP_ONCE leaves the install unapproved so the collateral is not
    removed. Abort raises `SyncAbortedByUser` — the existing user-decline control-flow
    exception, caught once at WARNING by both the orchestrator and the CLI — naming the
    collateral package, so the whole run stops cleanly rather than applying a transaction
    the user did not accept.

    Every untrusted label/detail is wrapped in `Text` before it reaches the console, so a
    package name containing bracket characters cannot trigger the Rich markup crash the
    phase already guards against (T-02-02).
    """
    for entry in group.entries:
        console.print()
        console.print(Text(entry.label, style="bold"))
        if entry.detail:
            console.print(Text(entry.detail, style="dim"))

        choice_prompt = questionary.select(
            f"{entry.label} is manually installed and would be removed or downgraded. Proceed?",
            choices=[
                questionary.Choice(title="Install anyway (allow the collateral removal)", value="install_anyway"),
                questionary.Choice(title="Skip (leave the triggering install unapproved)", value="skip"),
                questionary.Choice(title="Abort the sync", value="abort"),
            ],
        )
        selected = await asyncio.to_thread(choice_prompt.ask)

        if selected == "install_anyway":
            decisions[entry.item_id] = Decision.APPLY
        elif selected == "abort":
            raise SyncAbortedByUser(
                f"collateral removal of manually-installed {entry.label} declined (abort chosen in review)"
            )
        else:
            # "skip", None (the select was cancelled): leave the triggering install
            # unapproved for this run, so the collateral is not removed.
            decisions[entry.item_id] = Decision.SKIP_ONCE


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
        # D-26: no capture is ever offered without a TTY, so every unreproducible item
        # is unresolved by construction — never a snippet, never a recorded decision.
        non_interactive_unresolved = tuple(
            entry.item_id for group in groups if _is_unreproducible_group(group.action) for entry in group.entries
        )
        return ReviewOutcome(decisions=decisions, was_interactive=False, unresolved=non_interactive_unresolved)

    ui.pause()
    decisions: dict[str, Decision] = {}
    snippets: dict[str, str] = {}
    unresolved: list[str] = []
    try:
        for index, group in enumerate(groups):
            console.print()
            console.print(_render_group_panel(group))

            if _is_unreproducible_group(group.action):
                await _review_unreproducible_group(
                    group, console=console, decisions=decisions, snippets=snippets, unresolved=unresolved
                )
                continue

            if _is_collateral_group(group.action):
                await _review_collateral_group(group, console=console, decisions=decisions)
                continue

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
                # of this group AND every later group, comes back SKIP_ONCE and later
                # groups are not shown — any unreproducible group among them is
                # unresolved too, since it never got its own resolution prompt.
                for remaining_group in groups[index:]:
                    unreproducible = _is_unreproducible_group(remaining_group.action)
                    for entry in remaining_group.entries:
                        decisions.setdefault(entry.item_id, Decision.SKIP_ONCE)
                        if unreproducible:
                            unresolved.append(entry.item_id)
                break

            selected_ids = set(selected)
            for entry in group.entries:
                decisions[entry.item_id] = Decision.APPLY if entry.item_id in selected_ids else Decision.SKIP_ONCE
    finally:
        ui.resume()

    return ReviewOutcome(decisions=decisions, was_interactive=True, snippets=snippets, unresolved=tuple(unresolved))


@runtime_checkable
class Reviewer(Protocol):
    """A package job's review seam (D-24): given the groups one job planned, return that
    job's decisions.

    Injected through `JobContext.reviewer` exactly as `Confirmer` is through
    `JobContext.confirmer`, so a `PackageSyncJob.execute()` reaches its own review without
    any component outside the job owning it. Each job reviews its own groups before its own
    first mutating command; there is no cross-manager review.
    """

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome: ...


class TerminalUIReviewer:
    """`Reviewer` backed by the Rich console and the live `TerminalUI`.

    A thin adapter: `review()` forwards to `review_items`, which keeps every behaviour it
    has — the automation-environment hook, the non-interactive path, and the pause/resume
    `finally` that lets the blocking prompt run inside the job TaskGroup. Mirrors
    `TerminalUIConfirmer`'s shape (console + UI + optional logger), constructed once by the
    orchestrator.
    """

    def __init__(
        self,
        console: Console,
        ui: PausableUI,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._console = console
        self._ui = ui
        self._logger = logger

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        return await review_items(groups, console=self._console, ui=self._ui, logger=self._logger)
