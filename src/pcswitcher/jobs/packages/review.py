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

D-07's three-way decision is completed by a second checkbox per actionable group (install /
change / remove direction, which includes the block-state items): whatever the apply list
left UNTICKED is offered once more, and a tick there records `Decision.SKIP_ALWAYS` —
"never offer this again on this machine". Ticking nothing is the status quo (skip once).
`REPORT_ONLY` groups never get that offer: an informational item has no machine that holds
it, so a permanent mark would silently stop the underlying package syncing rather than stop
reporting the condition.

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
`ReviewOutcome.snippets` carries that group's authored snippets back to the caller
(`PackageSyncJob.apply()`), which persists them. An interactive review always resolves
every entry (decision 10): an empty snippet capture re-prompts rather than falling through
to an "unresolved" state, and Ctrl-C anywhere in the review aborts the whole sync
(`SyncAbortedByUser`) rather than skipping items. `ReviewOutcome.unresolved` is therefore
populated only on the non-interactive path, where it reports (never fails) the items no
one was present to resolve (D-26).

A `ReviewGroup` whose `action` is `REPO_REMOVAL_REVIEW_ACTION` keeps the ordinary checkbox
shape but takes only TWO answers (ADR-021 rulings 5 and 12): delete, or leave it for now.
It arrives unticked like every other removal direction and is never offered the "never
offer again" promotion, so `Decision.SKIP_ALWAYS` is unreachable for it and nothing about
it is ever recorded. That is why `_REMOVAL_ACTIONS` and `_PROMOTABLE_ACTIONS` are two
independent sets rather than one derived from the other.

A `ReviewGroup` whose `action` is `REPO_CONFLICT_REVIEW_ACTION` gets a per-entry two-way
flow instead (ADR-021 ruling 6): something that differs on the two machines and feeds an
item the target recorded machine-specific — a repository file for `apt_sync`, a remote for
`flatpak_sync` — is shown as both versions, never a unified diff, and answered overwrite or
skip-once. Nothing is recorded either way.

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
    "REPO_CONFLICT_REVIEW_ACTION",
    "REPO_REMOVAL_REVIEW_ACTION",
    "UNREPRODUCIBLE_REVIEW_ACTION",
    "Decision",
    "ReviewEntry",
    "ReviewGroup",
    "ReviewOutcome",
    "Reviewer",
    "TerminalUIReviewer",
    "ask_gate",
    "review_items",
]

_logger = logging.getLogger("pcswitcher.jobs.packages.review")

# Undocumented on purpose (D-26): lets integration tests answer a review without a TTY.
# Never mentioned in --help, the config schema, or docs/configuration.md.
PACKAGE_REVIEW_AUTOMATION_ENV = "PCSWITCHER_PACKAGE_REVIEW_AUTOMATION"

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group of
# `/etc/apt` repository or pin DELETIONS as taking only two answers — delete, or leave it
# for now (ADR-021 D-37, rulings 5 and 12). Unlike the other two sentinels this needs no
# per-entry flow: it renders as an ordinary unticked checkbox list, and the whole difference
# is that it is never offered the "never offer again" promotion. A permanent machine-local
# mark on a file whose entire purpose is to feed packages would silently and permanently
# change where those packages come from, and the user's remedy is consolidating the two
# machines' files, not recording a preference. One sentinel, two groups: `_build_review_
# groups` keys on (action, item_class), so repositories and pins still reach the user as
# separate screens with separate titles.
REPO_REMOVAL_REVIEW_ACTION = "repo_removal"

# Canonical removal-direction action values (D-07's "remove/delete/disable" family). Any
# `ReviewGroup.action` outside this set is treated as install-direction (checked by
# default) — covers "install"/"add"/"enable" as well as "change" (converging an existing
# item to match the source is not the destructive branch a bulk tick must guard against).
_REMOVAL_ACTIONS = frozenset({"remove", "delete", "disable", REPO_REMOVAL_REVIEW_ACTION})

# `ReviewGroup.action` values whose items carry a converge verb AND may be recorded
# machine-specific, and are therefore the only ones offered the "never offer again"
# promotion below (D-07). A `REPORT_ONLY` group is excluded on purpose: a version mismatch,
# an unreplicable origin or a cross-vendor mismatch has no machine that HOLDS the item for D-08a to
# record against, and recording one would stop the package syncing altogether rather than
# stop reporting the condition. Those are resolved by fixing the underlying condition, not
# by a machine-specific mark.
#
# Enumerated independently of `_REMOVAL_ACTIONS` rather than derived from it: "arrives
# unticked" and "is offered permanence" are two different questions about a group, and
# ADR-021's two-answer screens answer them differently — `REPO_REMOVAL_REVIEW_ACTION` is
# in the first set and deliberately absent from this one.
_PROMOTABLE_ACTIONS = frozenset({"install", "add", "enable", "change", "remove", "delete", "disable"})

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group
# of unreproducible items (D-18/D-21) as needing the three-way per-entry resolution flow
# below, rather than the ordinary checkbox tick. Not a `DiffAction` value — this is a
# `packages.review`-owned interaction kind, independent of the underlying diff's own
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

# Sentinel `ReviewGroup.action` for the one `/etc/apt` CHANGE that is still a question
# (ADR-021 ruling 6): a repository file present on both machines with different content that
# feeds a package the target recorded machine-specific. Every other change overwrites
# silently, because the user asked for the two machines to match; this one cannot, because
# overwriting it moves software the user explicitly told this tool to leave alone.
#
# Its own per-entry flow, a two-choice sibling of `COLLATERAL_REVIEW_ACTION`: overwrite
# records `Decision.APPLY`, skip records `Decision.SKIP_ONCE`, and there is no third answer —
# the remedy is consolidating the two files, not recording a preference. `ReviewEntry.
# versions` carries both file contents, shown as two panels rather than a unified diff.
REPO_CONFLICT_REVIEW_ACTION = "repo_conflict"


class PausableUI(Protocol):
    """The subset of `TerminalUI` the review needs to pause/resume the live display."""

    def pause(self) -> None: ...

    def resume(self) -> None: ...


@dataclass(frozen=True)
class ReviewEntry:
    """One item awaiting a decision inside a `ReviewGroup`.

    Deliberately minimal — this module has no dependency on the real item model plan
    02-03 introduces. Plan 02-05 adapts `ItemDiff` onto this shape.

    `versions` carries `(the target's current content, the source's content)` for the one
    screen that shows two whole files side by side instead of a detail line (ADR-021's
    repository conflict). Optional and defaulted so every other construction site — and
    every other screen — is unaffected; a unified diff is deliberately not the shape.
    """

    item_id: str
    label: str
    action_label: str
    detail: str | None = None
    versions: tuple[str, str] | None = None


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
    # A skip is promoted to permanent by its own prompt, never by a fourth checkbox state on
    # the apply list: the "never offer again" pass over an actionable group's UNTICKED
    # entries (`_offer_permanent_skips`), and the unreproducible group's "record as
    # machine-specific" choice.
    SKIP_ALWAYS = "skip_always"


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of a review: every entry's decision, plus how it was reached.

    `snippets` (item_id -> body, D-20) is populated by an `UNREPRODUCIBLE_REVIEW_ACTION`
    group's per-entry resolution. `unresolved` (item ids, D-21) is populated ONLY on a
    non-interactive run, listing the unreproducible items no one was present to resolve
    (D-26 reporting); an interactive review always resolves every entry (decision 10), so
    it leaves `unresolved` empty. Every other group leaves both at their empty defaults, so
    callers constructing a `ReviewOutcome` by hand (tests, and `PackageSyncJob.apply()`'s
    decision handling) are unaffected.
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


def _is_repo_conflict_group(action: str) -> bool:
    return action == REPO_CONFLICT_REVIEW_ACTION


def _is_promotable_group(action: str) -> bool:
    return action in _PROMOTABLE_ACTIONS


# Printed once before the multi-line capture, so a user does not author a snippet that
# hangs the sync (T-02-18): the executor supplies no stdin, and a worked shape showing
# the DEBIAN_FRONTEND=noninteractive + dependency-fix pattern is cheaper to read here
# than to discover as a stuck sync.
_SNIPPET_AUTHORING_NOTE = (
    "This snippet replays non-interactively on the target — no stdin is available, so a\n"
    "command that prompts (e.g. a debconf question) will hang the sync rather than fail.\n"
    "A typical shape:\n\n"
    "  sudo DEBIAN_FRONTEND=noninteractive dpkg --install /path/to/package.deb || \\\n"
    "  sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --fix-broken\n"
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
) -> None:
    """Resolve one `UNREPRODUCIBLE_REVIEW_ACTION` group's entries, one at a time, with
    the three-way choice D-21 requires: add an install snippet, record as
    machine-specific (skip always), or skip for now. Never a checkbox tick — a checkbox
    answers "should this apply", but an unreproducible item's question is "how does this
    get resolved", which is not a yes/no.

    All three choices are VALID resolutions (D-21): a snippet, a skip-always, and an
    explicit skip-once. There is no fourth "genuinely undecided" outcome (decision 10 —
    unresolved must be unrepresentable in an interactive flow):

    - Ctrl-C at the resolution choice (`select` returns `None`) means the user wants
      to stop, so it aborts the ENTIRE sync with `SyncAbortedByUser` — never a per-item
      skip-and-mark-unresolved.
    - Choosing "add an install snippet" and then submitting an empty body (or abandoning
      the editor) is NOT accepted and does NOT fall through: the three-way choice is
      re-prompted so the user must supply a real snippet or pick an explicit skip.

    The body is stored verbatim, never stripped — D-20 forbids reasoning about it, and
    leading whitespace/newlines are the user's own formatting choice; "empty" means only a
    completely blank submission.
    """
    for entry in group.entries:
        console.print()
        console.print(Text(entry.label, style="bold"))
        if entry.detail:
            console.print(Text(entry.detail, style="dim"))

        # Re-prompt until the entry is resolved by a real snippet or an explicit skip. An
        # empty snippet capture loops back here rather than manufacturing an unresolved
        # item (decision 10); a cancelled choice breaks out by aborting the whole sync.
        while True:
            choice_prompt = questionary.select(
                f"How should {entry.label} be resolved?",
                choices=[
                    questionary.Choice(title="Add an install snippet", value="add_snippet"),
                    questionary.Choice(title="Record as machine-specific (skip always)", value="skip_always"),
                    questionary.Choice(title="Skip for now", value="skip_once"),
                ],
            )
            selected = await asyncio.to_thread(choice_prompt.ask)

            if selected is None:
                # Ctrl-C: the user wants to abort, not skip this one item (decision
                # 10). Raise the clean-stop control-flow exception the orchestrator and CLI
                # already catch once at WARNING, so the whole sync stops here.
                raise SyncAbortedByUser(
                    f"package review aborted while resolving unreproducible item {entry.label!r} (Ctrl-C)"
                )

            if selected == "skip_always":
                decisions[entry.item_id] = Decision.SKIP_ALWAYS
                break

            if selected == "skip_once":
                # An explicit "Skip for now" is a real decision (D-21): the item is
                # resolved for this run.
                decisions[entry.item_id] = Decision.SKIP_ONCE
                break

            # selected == "add_snippet"
            console.print(Text(_SNIPPET_AUTHORING_NOTE, style="dim"))
            body_prompt = questionary.text(
                f"Install snippet for {entry.label} (Esc then Enter to finish):", multiline=True
            )
            body = await asyncio.to_thread(body_prompt.ask)
            if body:
                snippets[entry.item_id] = body
                break

            # Empty submission or an abandoned editor (`""`/`None`): not a resolution and
            # not an unresolved fall-through — re-prompt the three-way choice (decision 10).
            console.print(
                Text("An install snippet cannot be empty — enter a real snippet or choose a skip.", style="yellow")
            )


async def _offer_permanent_skips(
    group: ReviewGroup,
    unticked: Sequence[ReviewEntry],
    *,
    console: Console,
    decisions: dict[str, Decision],
) -> None:
    """Offer D-07's third outcome over one actionable group's UNTICKED entries: a second
    checkbox whose ticks promote a skip-once to `Decision.SKIP_ALWAYS`.

    A second list rather than a per-item question (D-24): the user ticks items off a list,
    and turning the apply screen into a queue of three-way prompts is exactly the shape the
    batched review exists to avoid. It is also not a fourth state on the apply checkbox —
    "apply" and "never offer again" are opposite answers, so one list cannot carry both
    without an unticked item being ambiguous.

    Everything already ticked for apply is excluded, so a fully-ticked group prompts
    nothing. Leaving this list empty is the status quo (skip once, re-offered next run), so
    a bare Enter keeps the pre-existing behaviour.

    Ctrl-C (`ask` returns `None`) aborts the WHOLE sync like every other review
    screen — never a silent per-item fallthrough.
    """
    console.print(
        Text(
            "Tick anything that should never be offered again on this machine; Enter leaves them for next run.",
            style="dim",
        )
    )
    prompt = questionary.checkbox(
        f"{group.title} — never offer again on this machine?",
        choices=[
            questionary.Choice(title=f"{entry.action_label} {entry.label}", value=entry.item_id, checked=False)
            for entry in unticked
        ],
    )
    selected = await asyncio.to_thread(prompt.ask)

    if selected is None:
        raise SyncAbortedByUser("package review aborted at a never-offer-again screen (Ctrl-C)")

    # Scoped to the entries actually offered, so a promotion can never reach back and
    # overwrite an APPLY decision the apply list already recorded.
    promoted = set(selected)
    for entry in unticked:
        if entry.item_id in promoted:
            decisions[entry.item_id] = Decision.SKIP_ALWAYS


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


async def _review_repo_conflict_group(
    group: ReviewGroup,
    *,
    console: Console,
    decisions: dict[str, Decision],
) -> None:
    """Resolve one `REPO_CONFLICT_REVIEW_ACTION` group's entries, one at a time, with the
    two-way choice ADR-021 ruling 6 requires: overwrite the target's version with the
    source's, or skip for now.

    Both versions are printed, the target's first, never a unified diff — the user's own
    position is that a diff of two repository definitions is not readable, and the question
    is which of two configurations the machine should have, not what changed between them.

    Ecosystem-neutral wording throughout, because two managers raise this screen about two
    different subjects: `apt_sync` about a repository file, whose versions are the two whole
    file bodies, and `flatpak_sync` about a remote, whose versions are its differing fields.
    The entry's own `detail` is where the subject is named.

    Two answers, not three. Skip-always is deliberately absent: a permanent mark on a
    repository file would permanently change where the packages it feeds come from, and the
    remedy for two machines whose definitions have drifted is consolidating them.

    `Decision.APPLY` puts the file in the write set; `Decision.SKIP_ONCE` keeps it out AND
    fails every approved package whose origin depended on it (the caller's job) — a skipped
    conflict is not the same as no conflict, because the package the user ticked cannot be
    delivered from the origin they were promised.

    Ctrl-C (`select` returns `None`) aborts the whole sync naming the file, like every other
    screen. Every untrusted string — the filename, the detail, and both file bodies — is
    wrapped in `Text` before it reaches the console, so a bracketed line inside a repository
    definition cannot trigger the Rich markup crash (T-02-02).
    """
    for entry in group.entries:
        console.print()
        console.print(Text(entry.label, style="bold"))
        if entry.detail:
            console.print(Text(entry.detail, style="dim"))
        if entry.versions is not None:
            target_version, source_version = entry.versions
            console.print(Panel(Text(target_version), title=Text("on the target now"), border_style="yellow"))
            console.print(Panel(Text(source_version), title=Text("on the source"), border_style="cyan"))

        choice_prompt = questionary.select(
            f"{entry.label} differs on the two machines. Proceed?",
            choices=[
                questionary.Choice(title="Overwrite the target's version with the source's", value="overwrite"),
                questionary.Choice(title="Skip for now", value="skip_once"),
            ],
        )
        selected = await asyncio.to_thread(choice_prompt.ask)

        if selected is None:
            raise SyncAbortedByUser(f"package review aborted while resolving the conflict on {entry.label!r} (Ctrl-C)")
        decisions[entry.item_id] = Decision.APPLY if selected == "overwrite" else Decision.SKIP_ONCE


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
    a `finally`, so the live display is always handed back even if the prompt raises. An
    actionable group whose apply list left anything unticked then gets `_offer_permanent_skips`,
    which turns a tick into `SKIP_ALWAYS` (D-07).
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
    try:
        for group in groups:
            console.print()
            console.print(_render_group_panel(group))

            if _is_unreproducible_group(group.action):
                await _review_unreproducible_group(group, console=console, decisions=decisions, snippets=snippets)
                continue

            if _is_collateral_group(group.action):
                await _review_collateral_group(group, console=console, decisions=decisions)
                continue

            if _is_repo_conflict_group(group.action):
                await _review_repo_conflict_group(group, console=console, decisions=decisions)
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
                # Ctrl-C at a checkbox screen means the user wants to abort, not
                # silently skip the rest of the review (decision 10). Raise the clean-stop
                # control-flow exception so the whole sync stops here rather than leaving
                # this and every later group's items undecided.
                raise SyncAbortedByUser("package review aborted at a checkbox screen (Ctrl-C)")

            selected_ids = set(selected)
            for entry in group.entries:
                decisions[entry.item_id] = Decision.APPLY if entry.item_id in selected_ids else Decision.SKIP_ONCE

            if _is_promotable_group(group.action):
                unticked = [entry for entry in group.entries if entry.item_id not in selected_ids]
                if unticked:
                    await _offer_permanent_skips(group, unticked, console=console, decisions=decisions)
    finally:
        ui.resume()

    # An interactive review can no longer leave anything unresolved (decision 10): the
    # unreproducible flow re-prompts or aborts, and a checkbox abort raises above — so
    # `unresolved` is always empty here. It stays populated only on the non-interactive
    # path (D-26 reporting).
    return ReviewOutcome(decisions=decisions, was_interactive=True, snippets=snippets, unresolved=())


async def ask_gate(
    *,
    title: str,
    message: str,
    proceed_label: str,
    stop_label: str,
    console: Console,
    ui: PausableUI,
    logger: logging.Logger | None = None,
) -> bool | None:
    """Ask one two-answer question about the MACHINE, outside the item review.

    Sibling of `review_items`, not a group inside it: a gate asks whether the job may run
    at all, so one of its answers means there is no review to present. It reuses this
    module for the pause-ask-resume `finally` and the interactivity test, which is the only
    place in the codebase that knows how to run a blocking `questionary` prompt under the
    Rich live display.

    True is the proceed answer and False the stop answer. `None` means nobody was there to
    ask — the caller owns that fallback, because "no TTY" means something different to
    every question. There is deliberately NO automation-environment hook: the answer here
    can require the user to go and change the other machine, which no scripted value can
    stand in for.

    Ctrl-C aborts the whole sync (`SyncAbortedByUser`), matching every checkbox screen.
    `title` and `message` are rendered as `Text`, never markup (T-02-02).
    """
    log = logger if logger is not None else _logger

    if not is_interactive(console):
        log.warning("%s — not asked, no TTY", title)
        return None

    ui.pause()
    try:
        console.print()
        console.print(Panel(Text(message), title=Text(title), border_style="yellow"))
        prompt = questionary.select(
            title,
            choices=[
                questionary.Choice(title=proceed_label, value=True),
                questionary.Choice(title=stop_label, value=False),
            ],
        )
        selected = await asyncio.to_thread(prompt.ask)
    finally:
        ui.resume()

    if selected is None:
        raise SyncAbortedByUser(f"sync aborted at a gate question (Ctrl-C): {title}")
    return bool(selected)


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

    async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None: ...


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

    async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None:
        return await ask_gate(
            title=title,
            message=message,
            proceed_label=proceed_label,
            stop_label=stop_label,
            console=self._console,
            ui=self._ui,
            logger=self._logger,
        )
