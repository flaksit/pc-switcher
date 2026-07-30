"""Batched review — the single interaction surface for every package diff (D-24).

Each package job computes its own set of differences against the source's manifest and hands
them to `review_items` as `ReviewGroup`s before applying anything. The user answers one
screen per group in one sitting rather than a sequence of yes/no prompts.

An actionable group is one `decision_list` screen (`packages.decision_list`): every item on
its own row, the decision it currently carries in a column to the right, one key per answer.
Nothing is echoed afterwards — the answered list stays in the scrollback, and the decision
column is the record. That also removes the Rich panel that used to precede each screen:
the control lists the items itself, so a panel above it said everything twice. The panel
survives where there is nothing to answer: a report group, and the whole non-interactive
path (D-26), where it IS the report.

This composes with the single persistent Live display (Phase 1 plans 01-17/01-18) exactly
as `TerminalUIConfirmer.confirm` (`pcswitcher.confirmer`) does: pause the live region before
the prompt, run the blocking prompt off the event loop via `asyncio.to_thread` (ADR-005 —
no blocking calls on the event loop), and resume it in a `finally` so the terminal is always
handed back even if the prompt raises.

Removals get their own group, never sharing a screen with installs (D-07/D-24): a bulk
confirm that also deleted software would be exactly the silent-destruction failure D-07
exists to prevent, which is also why a removal-direction row starts at skip-once while an
install-direction row starts applied. A group whose change replaces content the target's
own user wrote starts at skip-once too (`ReviewGroup.overwrites_authored_content`). Which
of a caller's `ReviewGroup`s are "removal-direction" is decided by `ReviewGroup.action`;
grouping itself (turning an `ItemDiff` into `ReviewGroup`s keyed by manager+action) belongs
to `PackageSyncJob._build_review_groups`, and this module only consumes already-grouped
input.

`ask_gate` is the one question here that is NOT a review item: a two-answer yes/no about the
target's environment, asked before any group is built, whose "no" answer means there is no
review to hold (`apt_sync`'s Ubuntu Pro gate, ADR-020 D-38). It lives here because this
module already owns pause-the-live-UI-ask-resume, interactivity detection and the
Ctrl-C-aborts-the-sync rule; it returns `None` when nobody could be asked, and the caller
owns what that means.

D-07's three answers are all on the one screen for an actionable group (install / change /
remove direction, which includes the block-state items): apply, skip now, or skip for good —
treat the item as specific to one machine, which makes it inert here in both roles (D-08a).
Those are the decisions; what each screen CALLS them is `_options_for` and the hints beside
them, which say the act, the machine it happens to, and how long the answer lasts.
A `REPORT_ONLY` group is not answerable at all: nothing converges either way and no machine
holds an informational item, so `_print_report_group` prints it and the review moves on.

`PACKAGE_REVIEW_AUTOMATION_ENV`: undocumented escape hatch for integration tests, which run
without a TTY and cannot drive a real terminal prompt. When set, its value is trusted JSON
(no schema validation) mapping item_id -> decision, applied instead of prompting. It never
widens what the review offers (D-25 items are still exactly what the caller passed in) and
is deliberately absent from `--help` and the config schema (D-26, `PKG-NG-AUTOMATION-ENV`), so
nothing offers it as a way of running the tool. It is named in the docs as an accepted cost,
because its answers count as the user's own: a permanent one writes a machine-specific mark.

A `ReviewGroup` whose `action` is `UNREPRODUCIBLE_REVIEW_ACTION` gets a different
interaction shape from every other group (D-21): instead of a row on a decision screen, each
entry is resolved one at a time with a three-way choice — write the commands that install
it, mark it as belonging to the source machine alone, or skip for now — because "should this
apply" is not the question for an item no package manager can reproduce; "how does the other
machine get this" is.
`ReviewOutcome.snippets` carries that group's authored snippets back to the caller
(`PackageSyncJob.apply()`), which persists them. An interactive review always resolves
every entry (decision 10): an empty snippet capture re-prompts rather than falling through
to an "unresolved" state, and Ctrl-C anywhere in the review aborts the whole sync
(`SyncAbortedByUser`) rather than skipping items. `ReviewOutcome.unresolved` is therefore
populated only on the non-interactive path, where it reports (never fails) the items no
one was present to resolve (D-26).

A `ReviewGroup` whose `action` is `REPO_REMOVAL_REVIEW_ACTION` uses the same screen with one
fewer answer (ADR-020 D-07): delete, or leave it for now. It starts at skip-once like every
other removal direction and is never offered permanence, so `Decision.SKIP_ALWAYS` is
unreachable for it and nothing about it is ever recorded. That is why `_REMOVAL_ACTIONS` and
`_PROMOTABLE_ACTIONS` are two independent sets rather than one derived from the other. An
entry carrying `ReviewEntry.content` prints that whole file first: a pin file's name says
nothing about what it does, and its name is all a decision row can show.

A `ReviewGroup` whose `action` is `REPO_CONFLICT_REVIEW_ACTION` is the same two-answer screen
(ADR-020 D-37) preceded by its own content: something that differs on the two machines and
feeds an item the target recorded machine-specific — a repository file for `apt_sync`, a
remote for `flatpak_sync` — is printed as both versions, never a unified diff, before the one
screen that answers overwrite or skip-once for all of them. Nothing is recorded either way,
and it starts at skip-once: an overwrite moves software the target explicitly marked
machine-specific, so it is chosen, never defaulted.

A `ReviewGroup` whose `action` is `COLLATERAL_REVIEW_ACTION` likewise gets its own
interaction shape (D-30): each entry is a package the TARGET protects — its own apt has it
marked manually installed, or that machine marked it machine-specific — that the pending
transaction would remove, downgrade or upgrade, resolved one at a time with a three-way
choice — go ahead, keep the package, or stop the whole sync. The decision
is recorded against the entry's `item_id` (the triggering change, set by the caller), so
"go ahead" proceeds with it, "keep" leaves it unapproved, and "stop" raises
`SyncAbortedByUser` naming the collateral package. A non-interactive run leaves every
collateral entry `SKIP_ONCE` like every other item, so the change it gates is simply not
approved (D-26).

Every screen here names the two machines by hostname. `review_items` takes both and they
are required: what an answer costs is "nomad loses this package", never "the target loses
this package", and no wording in this module may fall back to the tool's own vocabulary for
the user's computers. Source and target survive as the names of the ROLES in code,
docstrings and logs, which is where they belong.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import questionary
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from pcswitcher.jobs.packages import prompt_navigation
from pcswitcher.jobs.packages.decision_list import DecisionOption, DecisionRow, decision_list
from pcswitcher.models import SyncAbortedByUser
from pcswitcher.redaction import redact_credentials
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
# for now (ADR-020 D-07). Unlike the other two sentinels this needs no
# per-entry flow: it renders as an ordinary decision screen starting at skip-once, and the
# whole difference is that the third answer is absent from it. A permanent machine-local
# mark on a file whose entire purpose is to feed packages would silently and permanently
# change where those packages come from, and the user's remedy is consolidating the two
# machines' files, not recording a preference. One sentinel, two groups: `_build_review_
# groups` keys on (action, item_class), so repositories and pins still reach the user as
# separate screens with separate titles.
REPO_REMOVAL_REVIEW_ACTION = "repo_removal"

# Canonical removal-direction action values (D-07's "remove/delete/disable" family). Any
# `ReviewGroup.action` outside this set is treated as install-direction (starting applied)
# — covers "install"/"add"/"enable" as well as "change" (converging an existing item to
# match the source is not the destructive branch a bulk confirm must guard against).
_REMOVAL_ACTIONS = frozenset({"remove", "delete", "disable", REPO_REMOVAL_REVIEW_ACTION})

# `ReviewGroup.action` values whose items carry a converge verb AND may be recorded
# machine-specific, and are therefore the only ones whose screen offers the third answer
# (D-07).
#
# Enumerated independently of `_REMOVAL_ACTIONS` rather than derived from it: "starts at
# skip-once" and "is offered permanence" are two different questions about a group, and
# ADR-020 D-07's two-answer screens answer them differently — `REPO_REMOVAL_REVIEW_ACTION`
# is in the first set and deliberately absent from this one.
_PROMOTABLE_ACTIONS = frozenset({"install", "add", "enable", "change", "remove", "delete", "disable"})

# An item the two machines both have at different versions is neither arriving nor leaving,
# so its permanent answer names a different holder from an install's (`_hints`). A reported
# condition is not answered at all, and `review_items` routes it away before any screen is
# built. Spelled as the `DiffAction` values rather than imported: `sync_core` imports this
# module, not the other way round.
_CHANGE_ACTION = "change"
_REPORT_ACTION = "report_only"

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group
# of unreproducible items (D-18/D-21) as needing the three-way per-entry resolution flow
# below, rather than an ordinary decision screen. Not a `DiffAction` value — this is a
# `packages.review`-owned interaction kind, independent of the underlying diff's own
# `action` (which stays `REPORT_ONLY`/`INSTALL` per D-25's taxonomy).
UNREPRODUCIBLE_REVIEW_ACTION = "unreproducible"

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group
# of manual-collateral items (D-30) as needing the three-way per-entry resolution flow
# below — go ahead / keep the package / stop the sync — rather than an ordinary decision
# screen. A manual-collateral item is a package `Collateral.protected` covers that the
# pending transaction would remove, downgrade or upgrade; whether to lose it is not
# a question the decision screen expresses, so it gets its own prompt (sibling to
# `UNREPRODUCIBLE_REVIEW_ACTION`). Go-ahead records `Decision.APPLY` against
# `ReviewEntry.item_id`, keep records `Decision.SKIP_ONCE`, and stop raises
# `SyncAbortedByUser` naming the collateral package. The caller maps that recorded decision
# onto the changes that cause it (`AptSyncJob.accept_review`): APPLY lets them proceed and
# allows the collateral removal, SKIP_ONCE leaves exactly those unapproved.
COLLATERAL_REVIEW_ACTION = "collateral"

# Sentinel `ReviewGroup.action` for the one `/etc/apt` CHANGE that is still a question
# (ADR-020 D-37): a repository file present on both machines with different content that
# feeds a package the target recorded machine-specific. Every other change overwrites
# silently, because the user asked for the two machines to match; this one cannot, because
# overwriting it moves software the user explicitly told this tool to leave alone.
#
# A two-answer decision screen preceded by each entry's two versions: overwrite records
# `Decision.APPLY`, skip records `Decision.SKIP_ONCE`, and there is no third answer — the
# remedy is consolidating the two files, not recording a preference. `ReviewEntry.versions`
# carries both file contents, printed as two panels rather than a unified diff.
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
    screen that shows two whole files side by side instead of a detail line (ADR-020 D-37's
    repository conflict). Optional and defaulted so every other construction site — and
    every other screen — is unaffected; a unified diff is deliberately not the shape.

    `content` is the one-block counterpart, for a screen that offers to DELETE a file the
    only machine holding it still has: there is no second version to compare it against, and
    a filename alone is not something anyone can decide a deletion from.

    `answer_hints` is `(act, skip now)` for the screens that ask about one item at a time,
    where the sentences beside the keys name THIS item's own change — "remove fortunes-min
    from nomad, so fortunes is removed as well" is not something a screen-wide hint can say,
    and the same screen's next item may be a downgrade rather than a removal. Only the
    caller knows the change that causes the item, so only the caller can phrase it.

    Every string here is redacted at construction (`PKG-FR-CREDENTIAL-PRIVACY`, ADR-021):
    this is the shape every review line is built from, and the only one carrying the whole
    file bodies a question prints — a repository file, a remote's fields, a pin file — each
    of which can hold `https://user:token@host/` inside it. `item_id` is left alone: it keys
    a recorded decision across runs, so rewriting it would make that decision unfindable.
    """

    item_id: str
    label: str
    action_label: str
    detail: str | None = None
    versions: tuple[str, str] | None = None
    content: str | None = None
    answer_hints: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", redact_credentials(self.label))
        if self.detail is not None:
            object.__setattr__(self, "detail", redact_credentials(self.detail))
        if self.versions is not None:
            object.__setattr__(self, "versions", tuple(redact_credentials(body) for body in self.versions))
        if self.content is not None:
            object.__setattr__(self, "content", redact_credentials(self.content))
        if self.answer_hints is not None:
            object.__setattr__(self, "answer_hints", tuple(redact_credentials(hint) for hint in self.answer_hints))


@dataclass(frozen=True)
class ReviewGroup:
    """One screen's worth of same-manager, same-direction entries.

    `action` is shaped like the `DiffAction` enum a future plan introduces (e.g.
    "install"/"remove"/"change") but stays a plain string here so this module carries no
    dependency on that type yet. `title` must name the concrete verb for the item class
    ("Remove packages", not "Apply") — the caller building the group owns that wording.

    `note` is a line printed under a group that is reported rather than asked about: what
    to do about the condition, when the answer is not a decision on this screen.

    `overwrites_authored_content` starts this group's rows at skip-once even though its
    action is not a removal (`PKG-FR-HARMLESS-DEFAULT`): an `/etc/apt/apt.conf.d` file the
    target already holds is content the user wrote there, and replacing it unread is as
    irreversible as a deletion. A snap the run moves to another revision or channel is not —
    converging software the user asked for overwrites nothing they authored — so the caller
    decides this per group rather than every CHANGE starting skipped.
    """

    manager: str
    action: str
    title: str
    entries: Sequence[ReviewEntry]
    note: str | None = None
    overwrites_authored_content: bool = False


class Decision(StrEnum):
    """The three-way outcome D-07 requires for every reviewed item."""

    APPLY = "apply"
    SKIP_ONCE = "skip_once"
    # "Treat this item as specific to this machine": it goes inert here in BOTH roles
    # (D-08a), so it is neither pushed from here nor converged onto here. The sentence the
    # user reads beside this answer says the consequence — they will not be asked again
    # (`PKG-FR-EFFECT-NOT-MECHANISM`) — because "pc-switcher stops touching the item" is
    # the machinery, and what it costs to choose permanence is what a permanent answer has
    # to be chosen on.
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


def _is_repo_removal_group(action: str) -> bool:
    return action == REPO_REMOVAL_REVIEW_ACTION


def _is_promotable_group(action: str) -> bool:
    return action in _PROMOTABLE_ACTIONS


# The keys that set a decision. `y` for the act, `s` for the answer that lasts one sync, and
# `x` — a cross, read as "exclude" — for the one that is recorded. NOT `n`: `y` and `n` read
# as a yes/no pair, so `n` invited "no, not now" from the answer that actually means never
# again. `a` is unavailable: it is conventionally Abort in a terminal prompt, and
# `decision_list` rejects it.
_APPLY_KEY = "y"
_SKIP_NOW_KEY = "s"
_SKIP_ALWAYS_KEY = "x"

# The words the decision column shows for the answers that are not the act. They are short
# because they share a column with the act verb, past the longest item on the screen; what
# each one commits the user to is said in the legend hint beside it, which has room for a
# sentence. `SKIP_NOW_WORD` is that word on every screen whatever the direction, so the key
# means one thing across a review — including the conflict screen, whose version-keeping is
# stated in its hint rather than in the word.
SKIP_NOW_WORD = "skip now"
KEEP_FOR_GOOD_WORD = "keep for good"

# Verbs whose sentences take "from" rather than "on": "remove from nomad", against "hold on
# nomad" and "change on nomad". A verb missing from the set reads correctly with "on", which
# is why they are enumerated rather than derived.
_VERBS_TAKING_FROM = frozenset({"remove", "delete"})


# Filled / hollow / crossed. The glyph is what carries the row's state, so the screen stays
# readable in a terminal whose background colours the user cannot distinguish.
_APPLY_GLYPH = "●"
_SKIP_ONCE_GLYPH = "○"
_SKIP_ALWAYS_GLYPH = "⊘"


def _group_act_word(group: ReviewGroup) -> str:
    """The verb the group's rows share, used for the act option's legend and column word.

    The commonest `action_label` rather than the first: it is the one the group title
    already names, so it is the one a row does NOT need to repeat.
    """
    counts = Counter(entry.action_label for entry in group.entries)
    return counts.most_common(1)[0][0] if counts else "apply"


def _default_decision(group: ReviewGroup) -> Decision:
    """Where a group's rows start before the user touches anything.

    Install-direction rows start applied; anything that removes, deletes or disables starts
    at skip-once — and so does the repository/remote overwrite, which moves software the
    target explicitly marked machine-specific, and any group that replaces content the
    target's own user wrote (`ReviewGroup.overwrites_authored_content`). Confirming a screen
    unread must never destroy or displace something the user did not choose.
    """
    if group.overwrites_authored_content:
        return Decision.SKIP_ONCE
    if _is_removal_direction(group.action) or _is_repo_conflict_group(group.action):
        return Decision.SKIP_ONCE
    return Decision.APPLY


def _skip_always_word(group: ReviewGroup) -> str:
    """The word for the answer that is recorded and never asked about again.

    Said as this screen's own act, not as a generic "always skip": on a removal screen the
    item is on the target and the answer keeps it, while everywhere else the item is not
    there at all and the answer is that it never arrives. One word cannot be both.
    """
    if _is_removal_direction(group.action):
        return KEEP_FOR_GOOD_WORD
    return f"never {_group_act_word(group)}"


def _hints(group: ReviewGroup, source_hostname: str, target_hostname: str) -> tuple[str, str, str]:
    """The act / skip-now / skip-always sentences for this screen's legend.

    Each says what the answer DOES — the act named as itself on the machine it happens to,
    and each skip as the state it leaves behind plus how long that lasts. Not "go ahead" or
    "leave nomad alone": a neutral word for the act is the same non-answer as "apply", and
    the user is reading these to find out what the key does, not to be reassured.

    The two skips share the act's own clause and differ only in the duration that follows —
    "for now, will be asked again next sync" against "for good, will not be asked again"
    (`PKG-FR-EFFECT-NOT-MECHANISM`, `PKG-FR-ANSWERS-AS-A-SET`). The mark's own effect on
    later runs is what the permanent answer states; what the mark stops pc-switcher doing
    is machinery the user cannot weigh a permanent answer against.

    Three shapes, because the answers genuinely differ by direction. A conflict is a choice
    between two versions of one file; an item already on the target is kept rather than
    refused; everything else arrives or does not.
    """
    verb = _group_act_word(group)
    if _is_repo_conflict_group(group.action):
        return (
            f"{target_hostname} changes this sync",
            f"keep {target_hostname}'s version; will be asked again next sync",
            "",
        )

    takes_from = verb in _VERBS_TAKING_FROM
    act = f"{verb} {'from' if takes_from else 'on'} {target_hostname}"
    now = (
        f"keep on {target_hostname} for now; will be asked again next sync"
        if takes_from
        else f"do not {verb} on {target_hostname} for now; will be asked again next sync"
    )
    # The permanent sentence takes the skip-now clause and swaps its duration, so each
    # branch here mirrors `_skip_always_word`'s own choice of verb. Whose machine the item
    # becomes is the second half: an item already on the target belongs to the target, and
    # one that has not arrived belongs to the machine it came from.
    if _is_removal_direction(group.action):
        holder = target_hostname
        permanent = f"keep on {target_hostname} for good"
    else:
        holder = target_hostname if group.action == _CHANGE_ACTION else source_hostname
        permanent = f"do not {verb} on {target_hostname} for good"
    return act, now, f"{permanent}; it is {holder}'s own, and will not be asked again"


def _options_for(group: ReviewGroup, *, source_hostname: str, target_hostname: str) -> tuple[DecisionOption, ...]:
    """The answers one group's screen offers — three, or two where D-07 records nothing.

    The same widget either way: the user sees a missing option in the legend rather than a
    differently-shaped prompt.
    """
    act_hint, now_hint, always_hint = _hints(group, source_hostname, target_hostname)
    options = [
        DecisionOption(
            value=Decision.APPLY,
            key=_APPLY_KEY,
            word=_group_act_word(group),
            glyph=_APPLY_GLYPH,
            is_act=True,
            hint=act_hint,
        ),
        DecisionOption(
            value=Decision.SKIP_ONCE,
            key=_SKIP_NOW_KEY,
            word=SKIP_NOW_WORD,
            glyph=_SKIP_ONCE_GLYPH,
            hint=now_hint,
        ),
    ]
    if _is_promotable_group(group.action):
        options.append(
            DecisionOption(
                value=Decision.SKIP_ALWAYS,
                key=_SKIP_ALWAYS_KEY,
                word=_skip_always_word(group),
                glyph=_SKIP_ALWAYS_GLYPH,
                is_permanent=True,
                hint=always_hint,
            )
        )
    return tuple(options)


def _rows_for(group: ReviewGroup) -> tuple[DecisionRow, ...]:
    """One row per entry, with the group's own verb stripped off the label.

    Every row of an `AptSyncJob`/`PackageSyncJob` group carries the same `action_label` as
    its title's verb, so prefixing each row with it said the same word once per line. A row
    whose action genuinely differs keeps it, in both places it matters: as a prefix on the
    item, and as its own word in the decision column.
    """
    act_word = _group_act_word(group)
    default = _default_decision(group)
    return tuple(
        DecisionRow(
            row_id=entry.item_id,
            label=entry.label,
            default=default,
            prefix=None if entry.action_label == act_word else entry.action_label,
            act_word=None if entry.action_label == act_word else entry.action_label,
            detail=entry.detail,
        )
        for entry in group.entries
    )


def _snippet_authoring_note(target_hostname: str) -> str:
    """Printed once before the multi-line capture, so a user does not author a snippet that
    hangs the sync (T-02-18): the executor supplies no stdin, and a worked shape showing the
    DEBIAN_FRONTEND=noninteractive + dependency-fix pattern is cheaper to read here than to
    discover as a stuck sync. Said as what happens on the machine that will run it, rather
    than as a fact about the executor.
    """
    return (
        f"These commands run on {target_hostname} with nobody watching — there is no keyboard\n"
        "attached to them, so a command that asks a question (e.g. a debconf prompt) hangs the\n"
        "sync instead of failing. A typical shape:\n\n"
        "  sudo DEBIAN_FRONTEND=noninteractive dpkg --install /path/to/package.deb || \\\n"
        "  sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --fix-broken\n"
    )


# Shown in place of questionary's own multiline instruction, which offers "Alt+Enter or Esc
# then Enter" — two chords for one gesture, neither of which a user guesses.
_SNIPPET_INSTRUCTION = "(Ctrl-D to finish)\n>"


def _snippet_submit_bindings() -> KeyBindings:
    """Ctrl-D finishes the snippet: prompt_toolkit's own end-of-input gesture, and the one a
    user reaches for in a multi-line capture.

    Scoped to this editor only. Ctrl-D as an ABORT anywhere else stays unhandled — that is a
    separate question this does not reopen.
    """
    bindings = KeyBindings()

    def submit(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    bindings.add(Keys.ControlD, eager=True)(submit)
    return bindings


_SNIPPET_SUBMIT_BINDINGS = _snippet_submit_bindings()

# questionary styles a prompt's ANSWER bold orange, which for a one-line answer is a
# highlight and for a multi-line editor is the whole thing the user is typing. A shell
# snippet is not an answer to be echoed back; it is text being written, and it reads as
# text.
_SNIPPET_STYLE = Style([("answer", "noinherit")])


def _render_group_panel(group: ReviewGroup) -> Panel:
    """Build the REPORT panel for one group — the non-interactive path only, where there is
    nothing to answer and this is all the user gets (D-26).

    An interactive run never prints it: the decision screen lists the same items, and a
    panel above it made every group appear twice.

    Package names, versions and stderr fragments come from package-manager output and
    must never reach a `Panel` as a bare `str` — Rich would parse `[...]`-shaped
    substrings as console markup and raise `MarkupError` (T-02-02).
    """
    body = Text()
    for index, entry in enumerate(group.entries):
        if index:
            # Separator, not terminator: a newline after the last entry renders as an empty
            # final line inside the panel border.
            body.append("\n")
        body.append(entry.action_label, style="bold")
        body.append(" ")
        body.append(entry.label)
        if entry.detail:
            body.append(" (")
            body.append(entry.detail, style="dim")
            body.append(")")
    return Panel(body, title=Text(group.title), border_style="cyan")


def _decisions_from_automation(groups: Sequence[ReviewGroup], raw: str) -> dict[str, Decision]:
    mapping: dict[str, str] = json.loads(raw)
    return {
        entry.item_id: Decision(mapping.get(entry.item_id, Decision.SKIP_ONCE.value))
        for group in groups
        for entry in group.entries
    }


# The unreproducible screen's own act: not a `Decision` at all, because answering it opens
# an editor and only what comes back decides whether the item is resolved.
_ADD_SNIPPET_VALUE = "add_snippet"
_ADD_SNIPPET_GLYPH = "◆"


def _unreproducible_options(source_hostname: str, target_hostname: str) -> tuple[DecisionOption, ...]:
    """The three answers for an item no package manager can install (D-21).

    In the review's own order — act, skip now, never — so the keys mean here what they mean
    on every other screen, and the act is the one that resolves the item rather than the one
    that is listed first because it is the interesting case.
    """
    return (
        DecisionOption(
            value=_ADD_SNIPPET_VALUE,
            key=_APPLY_KEY,
            word="install",
            glyph=_ADD_SNIPPET_GLYPH,
            is_act=True,
            hint=f"write a command snippet that installs it; {target_hostname} runs it",
        ),
        DecisionOption(
            value=Decision.SKIP_ONCE,
            key=_SKIP_NOW_KEY,
            word=SKIP_NOW_WORD,
            glyph=_SKIP_ONCE_GLYPH,
            hint=f"do not install on {target_hostname} for now; will be asked again next sync",
        ),
        DecisionOption(
            value=Decision.SKIP_ALWAYS,
            key=_SKIP_ALWAYS_KEY,
            word="never install",
            glyph=_SKIP_ALWAYS_GLYPH,
            is_permanent=True,
            hint=f"do not install on {target_hostname} for good; it is {source_hostname}'s own, "
            "and will not be asked again",
        ),
    )


async def _ask_about_one_item(
    entry: ReviewEntry,
    *,
    title: str,
    options: Sequence[DecisionOption],
    default: str,
    detail: str | None = None,
) -> str:
    """Put ONE item on a decision screen and return the answer's value.

    Some questions cannot be batched: a collateral package's answers name the change that
    causes it, and an unreproducible item's "install" answer opens an editor. Both were
    plain pickers of sentence-long choices, which made them the two screens in the review
    that looked nothing like the rest of it. The shape is what is shared here, not the
    batching — one row, the same glyphs, colours, keys and hint column.

    Ctrl-C (`ask` returns `None`) is the caller's to interpret: what stopping means differs
    between an item that can be left undecided and one that cannot.
    """
    prompt = decision_list(
        title,
        rows=[DecisionRow(row_id=entry.item_id, label=entry.label, default=default, detail=detail or entry.detail)],
        options=options,
    )
    answered: Mapping[str, str] | None = await asyncio.to_thread(prompt.ask)
    if answered is None:
        raise SyncAbortedByUser(f"package review aborted at {entry.label!r} (Ctrl-C)")
    return answered[entry.item_id]


async def _review_unreproducible_group(
    group: ReviewGroup,
    *,
    console: Console,
    source_hostname: str,
    target_hostname: str,
    decisions: dict[str, Decision],
    snippets: dict[str, str],
) -> None:
    """Resolve one `UNREPRODUCIBLE_REVIEW_ACTION` group's entries, one at a time, with
    the three-way choice D-21 requires: write an install snippet, skip for now, or never
    install it here. One item per screen, because answering "install" opens an editor for
    that item — but a decision screen like every other, so the review has one shape.

    All three choices are VALID resolutions (D-21): a snippet, a skip-always, and an
    explicit skip-once. There is no fourth "genuinely undecided" outcome (decision 10 —
    unresolved must be unrepresentable in an interactive flow):

    - Ctrl-C at the resolution screen means the user wants to stop, so `_ask_about_one_item`
      aborts the ENTIRE sync with `SyncAbortedByUser` — never a per-item
      skip-and-mark-unresolved.
    - Choosing "add an install snippet" and then submitting an empty body (or abandoning
      the editor) is NOT accepted and does NOT fall through: the three-way choice is
      re-prompted so the user must supply a real snippet or pick an explicit skip.

    The body is STORED verbatim, never stripped — D-20 forbids reasoning about it, and
    leading whitespace/newlines are the user's own formatting choice. Emptiness is decided
    on the stripped body, though: a body of only spaces and newlines replays as nothing at
    all, so accepting it would record a snippet that resolves the item without installing
    anything.
    """
    options = _unreproducible_options(source_hostname, target_hostname)
    for entry in group.entries:
        # Re-prompt until the entry is resolved by a real snippet or an explicit skip. An
        # empty snippet capture loops back here rather than manufacturing an unresolved
        # item (decision 10); a cancelled screen breaks out by aborting the whole sync.
        while True:
            selected = await _ask_about_one_item(
                entry,
                title=f"How should {target_hostname} get {entry.label}?",
                options=options,
                default=_ADD_SNIPPET_VALUE,
            )

            if selected == Decision.SKIP_ALWAYS:
                decisions[entry.item_id] = Decision.SKIP_ALWAYS
                break

            if selected == Decision.SKIP_ONCE:
                # An explicit "skip now" is a real decision (D-21): the item is resolved
                # for this run.
                decisions[entry.item_id] = Decision.SKIP_ONCE
                break

            # selected == _ADD_SNIPPET_VALUE
            console.print(Text(_snippet_authoring_note(target_hostname), style="dim"))
            body_prompt = questionary.text(
                f"Install snippet for {entry.label}:",
                multiline=True,
                instruction=_SNIPPET_INSTRUCTION,
                key_bindings=_SNIPPET_SUBMIT_BINDINGS,
                style=_SNIPPET_STYLE,
            )
            body = await asyncio.to_thread(body_prompt.ask)
            if body and body.strip():
                snippets[entry.item_id] = body
                break

            # Empty, whitespace-only, or an abandoned editor (`None`): not a resolution and
            # not an unresolved fall-through — re-prompt the three-way choice (decision 10).
            console.print(
                Text("An install snippet cannot be empty — enter a real snippet or choose a skip.", style="yellow")
            )


def _print_report_group(group: ReviewGroup, *, console: Console, target_hostname: str) -> None:
    """Print one report group — the conditions this manager found and cannot converge.

    A report, not a question: it is followed by the next screen, not by a prompt. The panel
    is the same one a non-interactive run prints, so a condition reads identically whether
    or not anyone was there.
    """
    console.print()
    console.print(_render_group_panel(group))
    console.print(Text(f"Nothing on {target_hostname} changes for these.", style="dim"))
    if group.note:
        console.print(Text(group.note, style="dim"))


async def _review_decision_group(
    group: ReviewGroup,
    *,
    source_hostname: str,
    target_hostname: str,
    decisions: dict[str, Decision],
) -> None:
    """Present one actionable group as a single screen and record every row's answer.

    The whole of D-07 in one pass: each row starts at `_default_decision` and ends wherever
    the user left it, so there is no leftover set to re-offer and no way for a screen asking
    about permanence to echo back an item's action. Every entry gets a decision, because the
    screen carries one per row from the moment it opens.

    Ctrl-C (`ask` returns `None`) aborts the WHOLE sync like every other review screen —
    never a silent fallthrough that leaves this and every later group undecided.
    """
    prompt = decision_list(
        group.title,
        rows=_rows_for(group),
        options=_options_for(group, source_hostname=source_hostname, target_hostname=target_hostname),
    )
    answered: Mapping[str, str] | None = await asyncio.to_thread(prompt.ask)

    if answered is None:
        raise SyncAbortedByUser(f"package review aborted at {group.title!r} (Ctrl-C)")

    for entry in group.entries:
        decisions[entry.item_id] = Decision(answered[entry.item_id])


# The collateral screen's third answer. Not a decision about the item — it ends the run —
# so it carries a value no `Decision` has, and `q` for quit rather than a letter the other
# screens spend on an answer.
_STOP_SYNC_VALUE = "stop_sync"
_STOP_SYNC_KEY = "q"
_STOP_SYNC_GLYPH = "■"


def _collateral_options(entry: ReviewEntry, target_hostname: str) -> tuple[DecisionOption, ...]:
    """The three answers D-30 requires for one collateral package.

    The act and skip sentences come from the entry (`answer_hints`), because they name the
    change that causes the collateral and what it does — "remove fortunes-min from nomad, so
    fortunes is removed as well" — which differs per item and will differ more as removals
    stop being the only cause. A caller that supplies none gets sentences that are true of
    every collateral item and specific to none.
    """
    act_hint, skip_hint = entry.answer_hints or (
        f"the change described above goes ahead on {target_hostname}",
        f"keep {entry.label} on {target_hostname}; the change that would touch it is dropped from this sync; "
        "will be asked again next sync",
    )
    return (
        DecisionOption(
            value=Decision.APPLY,
            key=_APPLY_KEY,
            word=entry.action_label,
            glyph=_APPLY_GLYPH,
            is_act=True,
            hint=act_hint,
        ),
        DecisionOption(
            value=Decision.SKIP_ONCE,
            key=_SKIP_NOW_KEY,
            word=SKIP_NOW_WORD,
            glyph=_SKIP_ONCE_GLYPH,
            hint=skip_hint,
        ),
        DecisionOption(
            value=_STOP_SYNC_VALUE,
            key=_STOP_SYNC_KEY,
            word="stop the sync",
            glyph=_STOP_SYNC_GLYPH,
            hint=f"nothing more is changed on {target_hostname}; what earlier jobs already did stays done",
        ),
    )


async def _review_collateral_group(
    group: ReviewGroup,
    *,
    console: Console,
    target_hostname: str,
    decisions: dict[str, Decision],
) -> None:
    """Resolve one `COLLATERAL_REVIEW_ACTION` group's entries, one at a time, with the
    three-way choice D-30 requires for a package the pending transaction would remove,
    downgrade or upgrade behind the user's back: let it happen, protect the package, or
    stop. Never a row on a decision screen — losing a package the user chose to have is not
    the same question as approving an install off a list.

    What is protected is a fact about the TARGET (`Collateral.protected`): its own
    `apt-mark showmanual` set, plus the packages that machine marked machine-specific.
    Either the user asked for the package on the machine being changed, or they told this
    tool to leave it alone there. Which of the two applies to THIS package is stated in
    `entry.detail`, composed by `Collateral` — the only layer that knows the ground.

    The decision is recorded against `entry.item_id`: proceed records `Decision.APPLY`,
    protect records `Decision.SKIP_ONCE`. `Collateral.resolve` maps that onto the changes
    that CAUSE the collateral — APPLY lets them proceed and allows the collateral
    removal, SKIP_ONCE leaves exactly those unapproved. Stopping
    raises `SyncAbortedByUser` — the existing user-decline control-flow exception, caught
    once at WARNING by both the orchestrator and the CLI — naming the collateral package.
    That ends the WHOLE pc-switcher sync, not just this job: the orchestrator's per-job
    handler re-raises it untouched (`_run_jobs_in_task_group`), so no later job runs and
    `run()` records the session ABORTED. The choice says so, because "abort" alone reads
    like "abort this question".

    Every untrusted label/detail is wrapped in `Text` before it reaches the console, so a
    package name containing bracket characters cannot trigger the Rich markup crash the
    phase already guards against (T-02-02).
    """
    for entry in group.entries:
        selected = await _ask_about_one_item(
            entry,
            title=group.title,
            options=_collateral_options(entry, target_hostname),
            default=Decision.SKIP_ONCE,
        )

        if selected == Decision.APPLY:
            decisions[entry.item_id] = Decision.APPLY
        elif selected == _STOP_SYNC_VALUE:
            raise SyncAbortedByUser(
                f"{entry.label} on {target_hostname} would have been removed or downgraded; the whole sync was "
                "stopped in the package review"
            )
        else:
            # Skip now: leave the causing changes unapproved for this run, so the collateral
            # is not removed.
            decisions[entry.item_id] = Decision.SKIP_ONCE


async def _review_removal_group(
    group: ReviewGroup,
    *,
    console: Console,
    source_hostname: str,
    target_hostname: str,
    decisions: dict[str, Decision],
) -> None:
    """The two-answer deletion screen (`REPO_REMOVAL_REVIEW_ACTION`), one file at a time:
    the file's own content, then the question about that file.

    Ruled by the user, and the reason is the printing. A pin file's name says nothing about
    what it does, so the file being offered for deletion is printed whole — and a batch
    printed three or four bodies in a row and then asked about all of them at once, by which
    point the first file was off the screen and the row said only its name. The decision now
    follows the thing it is about.

    An entry with no `content` (a repository file, whose URLs are in its detail line) prints
    nothing extra; its row's detail carries the finding as on any other screen.

    The body's own trailing newline is dropped: inside a panel border it renders as an empty
    last line. Wrapped in `Text` like every other untrusted string (T-02-02).
    """
    options = _options_for(group, source_hostname=source_hostname, target_hostname=target_hostname)
    for entry in group.entries:
        console.print()
        console.print(Text(entry.label, style="bold"))
        if entry.content is not None:
            console.print(
                Panel(Text(entry.content.rstrip("\n")), title=Text(f"On {target_hostname}"), border_style="yellow")
            )
        decisions[entry.item_id] = Decision(
            await _ask_about_one_item(entry, title=group.title, options=options, default=_default_decision(group))
        )


async def _review_repo_conflict_group(
    group: ReviewGroup,
    *,
    console: Console,
    source_hostname: str,
    target_hostname: str,
    decisions: dict[str, Decision],
) -> None:
    """Resolve one `REPO_CONFLICT_REVIEW_ACTION` group with the two-way choice ADR-020 D-37
    requires: overwrite the target's version with the source's, or skip for now.

    Both versions are printed, the target's first, never a unified diff — the user's own
    position is that a diff of two repository definitions is not readable, and the question
    is which of two configurations the machine should have, not what changed between them.
    One file at a time, also ruled by the user: two whole file bodies are long enough that a
    batched screen asked about definitions that had already scrolled away.

    Ecosystem-neutral wording throughout, because two managers raise this screen about two
    different subjects: `apt_sync` about a repository file, whose versions are the two whole
    file bodies, and `flatpak_sync` about a remote, whose versions are its differing fields.
    The entry's own `detail` is where the subject is named.

    Two answers, not three. Skip-always is deliberately absent: a permanent mark on a
    repository file would permanently change where the packages it feeds come from, and the
    remedy for two machines whose definitions have drifted is consolidating them.

    `Decision.APPLY` puts the file in the write set; `Decision.SKIP_ONCE` keeps it out AND
    fails every approved package whose origin depended on it (the caller's job) — a skipped
    conflict is not the same as no conflict, because the package the user approved cannot be
    delivered from the origin they were promised. Skip-once is also where each row STARTS
    (`_default_decision`), because an overwrite displaces software the target explicitly
    marked machine-specific.

    Every untrusted string — the filename, the detail, and both file bodies — is wrapped in
    `Text` before it reaches the console, so a bracketed line inside a repository definition
    cannot trigger the Rich markup crash (T-02-02). A file body's own trailing newline is
    dropped for display: inside a panel border it renders as an empty last line.
    """
    options = _options_for(group, source_hostname=source_hostname, target_hostname=target_hostname)
    for entry in group.entries:
        console.print()
        console.print(Text(entry.label, style="bold"))
        if entry.versions is not None:
            target_version, source_version = entry.versions
            console.print(
                Panel(
                    Text(target_version.rstrip("\n")),
                    title=Text(f"On {target_hostname} now"),
                    border_style="yellow",
                )
            )
            console.print(
                Panel(Text(source_version.rstrip("\n")), title=Text(f"On {source_hostname}"), border_style="cyan")
            )
        decisions[entry.item_id] = Decision(
            await _ask_about_one_item(entry, title=group.title, options=options, default=_default_decision(group))
        )


def _warn_every_item_unasked(groups: Sequence[ReviewGroup], log: logging.Logger) -> None:
    """Name every item a run with no terminal could not ask about (`PKG-FR-LOG-DECISIONS`).

    Names, not a count: every one of them is declined for this run, and a number says which
    items were declined to nobody.
    """
    for group in groups:
        for entry in group.entries:
            log.warning("%s — not asked, declined for this run (no TTY): %s", group.title, entry.label)


async def review_items(
    groups: Sequence[ReviewGroup],
    *,
    console: Console,
    ui: PausableUI,
    source_hostname: str,
    target_hostname: str,
    logger: logging.Logger | None = None,
) -> ReviewOutcome:
    """Present every group as one decision screen and return the user's decisions.

    Both machine names are required, not defaulted: every screen here names the machine an
    answer acts on, and "the target" is a word for the tool's own plumbing rather than for
    either of the user's computers. A caller that cannot name them has no business asking
    these questions.

    Non-interactive runs (`is_interactive(console)` is False) prompt for nothing: every
    item comes back `SKIP_ONCE`, nothing is recorded permanently, a warning names how many
    items went unresolved, and the group panels are printed as the report (D-26).
    Interactive runs pause `ui` around each group's blocking prompt (dispatched via
    `asyncio.to_thread`) and resume it in a `finally`, so the live display is always handed
    back even if the prompt raises. They print no group panel: the screen lists the items
    itself, and its answered form stays in the scrollback as the record.
    """
    log = logger if logger is not None else _logger

    automation_raw = os.environ.get(PACKAGE_REVIEW_AUTOMATION_ENV)
    if automation_raw is not None:
        return ReviewOutcome(decisions=_decisions_from_automation(groups, automation_raw), was_interactive=True)

    if not is_interactive(console):
        _warn_every_item_unasked(groups, log)
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

            if group.action == _REPORT_ACTION:
                # Nothing to answer (ruled by the user): a reported condition converges in
                # neither direction and records nothing, so both answers it used to offer
                # left the machines and the next run identical. Printing it and moving on
                # is what it always was.
                _print_report_group(group, console=console, target_hostname=target_hostname)
                for entry in group.entries:
                    decisions[entry.item_id] = Decision.SKIP_ONCE
                continue

            if _is_unreproducible_group(group.action):
                await _review_unreproducible_group(
                    group,
                    console=console,
                    source_hostname=source_hostname,
                    target_hostname=target_hostname,
                    decisions=decisions,
                    snippets=snippets,
                )
                continue

            if _is_collateral_group(group.action):
                await _review_collateral_group(
                    group, console=console, target_hostname=target_hostname, decisions=decisions
                )
                continue

            if _is_repo_conflict_group(group.action):
                await _review_repo_conflict_group(
                    group,
                    console=console,
                    source_hostname=source_hostname,
                    target_hostname=target_hostname,
                    decisions=decisions,
                )
                continue

            if _is_repo_removal_group(group.action):
                await _review_removal_group(
                    group,
                    console=console,
                    source_hostname=source_hostname,
                    target_hostname=target_hostname,
                    decisions=decisions,
                )
                continue

            await _review_decision_group(
                group, source_hostname=source_hostname, target_hostname=target_hostname, decisions=decisions
            )
    finally:
        ui.resume()

    # An interactive review can no longer leave anything unresolved (decision 10): the
    # unreproducible flow re-prompts or aborts, and a decision screen's abort raises above —
    # so `unresolved` is always empty here. It stays populated only on the non-interactive
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

    Ctrl-C aborts the whole sync (`SyncAbortedByUser`), matching every decision screen.
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
        prompt = prompt_navigation.select(
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
        source_hostname: str,
        target_hostname: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._console = console
        self._ui = ui
        self._source_hostname = source_hostname
        self._target_hostname = target_hostname
        self._logger = logger

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        return await review_items(
            groups,
            console=self._console,
            ui=self._ui,
            source_hostname=self._source_hostname,
            target_hostname=self._target_hostname,
            logger=self._logger,
        )

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
