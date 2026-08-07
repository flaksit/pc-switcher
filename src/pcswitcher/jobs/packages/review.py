"""Batched review — the single interaction surface for every package diff (`PKG-FR-BATCHED`).

Each package job computes its own set of differences against the source's manifest and hands
them to `review_items` as `ReviewGroup`s before applying anything. The user answers one
screen per group in one sitting rather than a sequence of yes/no prompts.

An actionable group is one `decision_list` screen (`packages.decision_list`): every item on
its own row, the decision it currently carries in a column to the right, one key per answer.
Nothing is echoed afterwards — the answered list stays in the scrollback, and the decision
column is the record. That also removes the Rich panel that used to precede each screen:
the control lists the items itself, so a panel above it said everything twice. The panel
survives where there is nothing to answer: a report group, and the whole non-interactive
path (`PKG-FR-NO-TERMINAL`), where it IS the report.

This composes with the single persistent Live display exactly
as `TerminalUIConfirmer.confirm` (`pcswitcher.confirmer`) does: pause the live region before
the prompt, run the blocking prompt off the event loop via `asyncio.to_thread` (ADR-005 —
no blocking calls on the event loop), and resume it in a `finally` so the terminal is always
handed back even if the prompt raises.

Removals get their own group, never sharing a screen with installs (`PKG-FR-SKIP-ONCE`/`PKG-FR-BATCHED`): a bulk
confirm that also deleted software would be exactly the silent-destruction failure `PKG-FR-SKIP-ONCE`
exists to prevent, which is also why a removal-direction row starts at skip-once while an
install-direction row starts applied. A group whose change replaces content the target's
own user wrote starts at skip-once too (`ReviewGroup.overwrites_authored_content`). Which
of a caller's `ReviewGroup`s are "removal-direction" is decided by `ReviewGroup.action`;
grouping itself (turning an `ItemDiff` into `ReviewGroup`s keyed by manager+action) belongs
to `PackageSyncJob._build_review_groups`, and this module only consumes already-grouped
input.

`ask_gate` is the one question here that is NOT a review item: a two-answer yes/no about the
target's environment, asked before any group is built, whose "no" answer means there is no
review to hold (`apt_sync`'s Ubuntu Pro gate, `PKG-FR-DISTRO-FILES`). It lives here because this
module already owns pause-the-live-UI-ask-resume, interactivity detection and the
Ctrl-C-aborts-the-sync rule; it returns `None` when nobody could be asked, and the caller
owns what that means.

`PKG-FR-SKIP-ONCE`'s three answers are all on the one screen for an actionable group (install / change /
remove direction): apply, skip now, or skip for good —
treat the item as specific to one machine, which makes it inert here in both roles (`PKG-FR-MACHINE-SPECIFIC`).
Those are the decisions; what each screen CALLS them is `_options_for` and the hints beside
them, which say the act, the machine it happens to, and how long the answer lasts.

One follow-up question comes after the batch, and only there: an item BOTH machines have
with different content has no holding machine the run's own direction can name, so "keep for
good" on one of those leaves open whose copy the answer is about (`PKG-FR-MARK-SIDE`). Every
such item answered that way goes onto one further screen — a row each, three answers naming
the two machines and both — and the chosen `MarkSide` comes back in
`ReviewOutcome.mark_sides` for `PackageSyncJob._record_permanent_skips` to write on. It is
one batched screen because the widget takes any number of answers per row, and it is a
follow-up rather than a fourth answer on the batch because the question only exists for the
rows that were answered permanently. A run that reaches no such answer never sees it, and a
run with nobody to ask never gets that far.
A `REPORT_ONLY` group is not answerable at all: nothing converges either way and no machine
holds an informational item, so `_print_report_group` prints it and the review moves on.

`PACKAGE_REVIEW_AUTOMATION_ENV`: undocumented escape hatch for integration tests, which run
without a TTY and cannot drive a real terminal prompt. When set, its value is trusted JSON
(no schema validation) mapping item_id -> decision, applied instead of prompting. It never
widens what the review offers (items are still exactly what the caller passed in) and
is deliberately absent from `--help` and the config schema (`PKG-FR-NO-TERMINAL`, `PKG-NG-AUTOMATION-ENV`), so
nothing offers it as a way of running the tool. It is named in the docs as an accepted cost,
because its answers count as the user's own: a permanent one writes a machine-specific mark.

A `ReviewGroup` whose `action` is `UNREPRODUCIBLE_REVIEW_ACTION` gets a different
interaction shape from every other group (`PKG-FR-MANUAL-RESOLUTION`): instead of a row on a decision screen, each
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
one was present to resolve (`PKG-FR-NO-TERMINAL`).

A `ReviewGroup` whose `action` is `REPO_REMOVAL_REVIEW_ACTION` uses the same screen with one
fewer answer (`PKG-FR-SKIP-ONCE`): delete, or leave it for now. It starts at skip-once like every
other removal direction and is never offered permanence, so `Decision.SKIP_ALWAYS` is
unreachable for it and nothing about it is ever recorded. That is why `_REMOVAL_ACTIONS` and
`_PROMOTABLE_ACTIONS` are two independent sets rather than one derived from the other. An
entry carrying `ReviewEntry.content` prints that whole file first: a pin file's name says
nothing about what it does, and its name is all a decision row can show.

A `ReviewGroup` whose `action` is `REPO_CONFLICT_REVIEW_ACTION` is the same two-answer screen
(`PKG-FR-REPO-CONFLICT`) preceded by its own content: something that differs on the two machines and
feeds an item the target recorded machine-specific — a repository file for `apt_sync`, a
remote for `flatpak_sync` — is printed as both versions, never a unified diff, before the one
screen that answers overwrite or skip-once for all of them. Nothing is recorded either way,
and it starts at skip-once: an overwrite moves software the target explicitly marked
machine-specific, so it is chosen, never defaulted.

A `ReviewGroup` whose `action` is `SNAP_CHANGE_REVIEW_ACTION` (defined in `sync_core`, where
the snap job builds it) is the ordinary decision screen with the third answer missing: it is
in neither `_REMOVAL_ACTIONS` — converging a snap onto the source's revision or channel
overwrites nothing the user authored, so the row starts applied — nor `_PROMOTABLE_ACTIONS`,
because a revision is not a standing per-machine preference and recording one leaves the two
machines' records disagreeing about a snap neither would raise again
(`PKG-FR-NO-MARK-ON-SNAP-REVISION`). Apply or skip-once, and nothing recorded either way.

A `ReviewGroup` whose `action` is `COLLATERAL_REVIEW_ACTION` likewise gets its own
interaction shape (`PKG-FR-COLLATERAL-MANUAL`): each entry is a package the TARGET protects — its own apt has it
marked manually installed, or that machine marked it machine-specific — that the pending
transaction would remove, downgrade or upgrade, resolved one at a time with a three-way
choice — apply, keep the package, or stop the whole sync. The decision
is recorded against the entry's `item_id` (the triggering change, set by the caller), so
"apply" proceeds with it, "keep" leaves it unapproved, and "stop" raises
`SyncAbortedByUser` naming the collateral package. A non-interactive run leaves every
collateral entry `SKIP_ONCE` like every other item, so the change it gates is simply not
approved (`PKG-FR-NO-TERMINAL`).

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
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import questionary
from prompt_toolkit.filters import is_done
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from pcswitcher.jobs.packages import prompt_navigation
from pcswitcher.jobs.packages.decision_list import DecisionOption, DecisionRow, decision_list
from pcswitcher.jobs.packages.state import SnippetBodies
from pcswitcher.models import SyncAbortedByUser
from pcswitcher.redaction import redact_credentials
from pcswitcher.terminal import is_interactive

__all__ = [
    "COLLATERAL_REVIEW_ACTION",
    "PACKAGE_REVIEW_AUTOMATION_ENV",
    "REPO_CONFLICT_REVIEW_ACTION",
    "REPO_REMOVAL_REVIEW_ACTION",
    "UNREPRODUCIBLE_RETRY_REVIEW_ACTION",
    "UNREPRODUCIBLE_REVIEW_ACTION",
    "UNREPRODUCIBLE_UPDATE_REVIEW_ACTION",
    "Decision",
    "MarkSide",
    "ReviewEntry",
    "ReviewGroup",
    "ReviewOutcome",
    "ReviewPolicy",
    "Reviewer",
    "TerminalUIReviewer",
    "ask_gate",
    "asks_for_a_decision",
    "policy_answers_any",
    "review_items",
]

_logger = logging.getLogger("pcswitcher.jobs.packages.review")

# Undocumented on purpose (`PKG-FR-NO-TERMINAL`): lets integration tests answer a review without a TTY.
# Never mentioned in --help, the config schema, or docs/configuration.md.
PACKAGE_REVIEW_AUTOMATION_ENV = "PCSWITCHER_PACKAGE_REVIEW_AUTOMATION"

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group of
# `/etc/apt` repository or pin DELETIONS as taking only two answers — delete, or leave it
# for now (`PKG-FR-SKIP-ONCE`). Unlike the other two sentinels this needs no
# per-entry flow: it renders as an ordinary decision screen starting at skip-once, and the
# whole difference is that the third answer is absent from it. A permanent machine-local
# mark on a file whose entire purpose is to feed packages would silently and permanently
# change where those packages come from, and the user's remedy is consolidating the two
# machines' files, not recording a preference. One sentinel, two groups: `_build_review_
# groups` keys on (action, item_class), so repositories and pins still reach the user as
# separate screens with separate titles.
REPO_REMOVAL_REVIEW_ACTION = "repo_removal"

# Canonical removal-direction action values (`PKG-FR-SKIP-ONCE`'s "remove/delete/disable" family). Any
# `ReviewGroup.action` outside this set is treated as install-direction (starting applied)
# — covers "install"/"add"/"enable" as well as "change" (converging an existing item to
# match the source is not the destructive branch a bulk confirm must guard against).
_REMOVAL_ACTIONS = frozenset({"remove", "delete", "disable", REPO_REMOVAL_REVIEW_ACTION})

# `ReviewGroup.action` values whose items carry a converge verb AND may be recorded
# machine-specific, and are therefore the only ones whose screen offers the third answer
# (`PKG-FR-SKIP-ONCE`).
#
# Enumerated independently of `_REMOVAL_ACTIONS` rather than derived from it: "starts at
# skip-once" and "is offered permanence" are two different questions about a group, and
# `PKG-FR-SKIP-ONCE`'s two-answer screens answer them differently — `REPO_REMOVAL_REVIEW_ACTION`
# is in the first set and deliberately absent from this one.
_PROMOTABLE_ACTIONS = frozenset({"install", "add", "enable", "change", "remove", "delete", "disable"})

# An item the two machines both have at different versions is neither arriving nor leaving,
# so its permanent answer names a different holder from an install's (`_hints`). A reported
# condition is not answered at all, and `review_items` routes it away before any screen is
# built. Spelled as the `DiffAction` values rather than imported: `sync_core` imports this
# module, not the other way round.
_CHANGE_ACTION = "change"
_REPORT_ACTION = "report_only"

# The version (or scope, or arch) an item label carries after its name: `tree (2.1.1)`,
# `sopwith/x86_64/stable (2.9.0, flathub, user)`. Anchored at the end and forbidding nested
# parentheses so a name that merely CONTAINS one keeps it.
_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)$")

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group
# of unreproducible items (`PKG-FR-MANUAL-SCOPE`/`PKG-FR-MANUAL-RESOLUTION`) as needing the three-way per-entry
# resolution flow
# below, rather than an ordinary decision screen. Not a `DiffAction` value — this is a
# `packages.review`-owned interaction kind, independent of the underlying diff's own
# `action` (which stays `REPORT_ONLY`/`INSTALL` per the diff-class taxonomy).
UNREPRODUCIBLE_REVIEW_ACTION = "unreproducible"

# The same per-entry flow for an item BOTH machines have at different versions, where the
# source's registry already holds a body (`PKG-FR-VERSION-SNIPPET`). Its act replays that body; its second
# answer replaces it first, because a version that will not move is usually a body that no
# longer installs what its author meant. There is no permanent answer: convergence and
# skip-for-this-run are the only two ways the loop behind it ends, and "never update this"
# is a standing preference about a version, which `PKG-FR-NO-MARK-ON-SNAP-REVISION` already
# rules out one ecosystem over.
UNREPRODUCIBLE_UPDATE_REVIEW_ACTION = "unreproducible_update"

# The narrowed menu the converge loop puts after a replay that changed no version
# (`PKG-FR-MANUAL-CONVERGE-LOOP`, `PKG-FR-ASK-AGAIN`): write a new body, or stop. The act
# is gone because it has just been carried out and did nothing — running the same bytes
# again is the same no-op, and offering it would invite a loop the user cannot win.
UNREPRODUCIBLE_RETRY_REVIEW_ACTION = "unreproducible_retry"

# The three actions whose answer is an authored shell body rather than a decision. Together
# they are what `policy_decision` refuses to answer from the command line and what a run
# with no terminal reports as unresolved — an editor is not something a flag can drive.
_AUTHORING_ACTIONS = frozenset({UNREPRODUCIBLE_REVIEW_ACTION, UNREPRODUCIBLE_RETRY_REVIEW_ACTION})

# Sentinel `ReviewGroup.action` a caller (today, only `AptSyncJob`) uses to mark a group
# of manual-collateral items (`PKG-FR-COLLATERAL-MANUAL`) as needing the three-way per-entry resolution flow
# below — apply / keep the package / stop the sync — rather than an ordinary decision
# screen. A manual-collateral item is a package `Collateral.protected` covers that the
# pending transaction would remove, downgrade or upgrade; whether to lose it is not
# a question the decision screen expresses, so it gets its own prompt (sibling to
# `UNREPRODUCIBLE_REVIEW_ACTION`). Apply records `Decision.APPLY` against
# `ReviewEntry.item_id`, keep records `Decision.SKIP_ONCE`, and stop raises
# `SyncAbortedByUser` naming the collateral package. The caller maps that recorded decision
# onto the changes that cause it (`AptSyncJob.accept_review`): APPLY lets them proceed and
# allows the collateral removal, SKIP_ONCE leaves exactly those unapproved.
COLLATERAL_REVIEW_ACTION = "collateral"

# Sentinel `ReviewGroup.action` for the one `/etc/apt` CHANGE that is still a question
# (`PKG-FR-REPO-CONFLICT`): a repository file present on both machines with different content that
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
    screen that shows two whole files side by side instead of a detail line (`PKG-FR-REPO-CONFLICT`'s
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

    `recorded_bodies` is what the snippet registry already holds for these entries, so an
    answer that rewrites a snippet opens its editors on that content instead of on nothing
    (`PKG-FR-VERSION-SNIPPET`). Only the two groups whose items HAVE a recorded snippet supply it; this module
    never reads a registry itself, exactly as it never reads a repository file.
    """

    manager: str
    action: str
    title: str
    entries: Sequence[ReviewEntry]
    note: str | None = None
    overwrites_authored_content: bool = False
    recorded_bodies: Mapping[str, SnippetBodies] | None = None


class Decision(StrEnum):
    """The three-way outcome `PKG-FR-SKIP-ONCE` requires for every reviewed item."""

    APPLY = "apply"
    SKIP_ONCE = "skip_once"
    # "Treat this item as specific to this machine": it goes inert here in BOTH roles
    # (`PKG-FR-MACHINE-SPECIFIC`), so it is neither pushed from here nor converged onto here. The sentence the
    # user reads beside this answer says the consequence — they will not be asked again
    # (`PKG-FR-EFFECT-NOT-MECHANISM`) — because "pc-switcher stops touching the item" is
    # the machinery, and what it costs to choose permanence is what a permanent answer has
    # to be chosen on.
    SKIP_ALWAYS = "skip_always"


class MarkSide(StrEnum):
    """Which machine's own copy a `SKIP_ALWAYS` on a CONFLICTING item is about.

    A conflicting item is one both machines have with different content, and it is the one
    case where the run's own direction cannot name the holding machine: either copy can be
    the one the user means to keep, and the machine that recorded the mark takes the other
    role as soon as the next sync is launched from the other end (`PKG-FR-MACHINE-SPECIFIC`). So the user says
    which, and `BOTH` is a real answer rather than a hedge — it records on each machine, so
    the answer survives either machine losing its copy (`PKG-FR-MARK-LIFETIME`).

    Nothing else takes one: an install is on the source alone and a removal on the target
    alone, so for those the action already names the holder.
    """

    SOURCE = "source"
    TARGET = "target"
    BOTH = "both"


@dataclass(frozen=True)
class ReviewOutcome:
    """The result of a review: every entry's decision, plus how it was reached.

    `snippets` (item_id -> both bodies, `PKG-FR-SNIPPET-VERBATIM`/`PKG-FR-VERSION-SNIPPET`) is populated by any of the
    three per-entry
    snippet groups' resolutions. `unresolved` (item ids, `PKG-FR-MANUAL-RESOLUTION`) is populated ONLY on a
    non-interactive run, listing the unreproducible items no one was present to resolve
    (`PKG-FR-NO-TERMINAL` reporting); an interactive review always resolves every entry (decision 10), so
    it leaves `unresolved` empty. Every other group leaves both at their empty defaults, so
    callers constructing a `ReviewOutcome` by hand (tests, and `PackageSyncJob.apply()`'s
    decision handling) are unaffected.

    `mark_sides` (item_id -> `MarkSide`, `PKG-FR-MARK-SIDE`) carries the follow-up answer
    for the conflicting items answered `SKIP_ALWAYS`, and holds an id only where a human
    gave that answer: an item missing from it is one nobody was asked about, which
    `_record_permanent_skips` treats as the target's own copy — the machine whose overwrite
    the permanent answer refused.
    """

    decisions: Mapping[str, Decision]
    was_interactive: bool
    snippets: Mapping[str, SnippetBodies] = field(default_factory=dict)
    unresolved: tuple[str, ...] = ()
    mark_sides: Mapping[str, MarkSide] = field(default_factory=dict)


def asks_for_a_decision(group: ReviewGroup) -> bool:
    """Whether `group` puts something to the user that they have to answer.

    A report-only group does not: it converges in neither direction, records nothing and
    offers no answer — the user is told about it and moves on. `PKG-FR-NO-TERMINAL` turns
    on this distinction rather than on whether a review printed anything, so a run with
    nobody to ask has decided everything there was to decide when every group is one of
    these (`sync_core.PackageSyncJob.execute`).
    """
    return group.action != _REPORT_ACTION


def _is_removal_direction(action: str) -> bool:
    return action in _REMOVAL_ACTIONS


def _is_unreproducible_group(action: str) -> bool:
    return action == UNREPRODUCIBLE_REVIEW_ACTION


def _asks_for_an_authored_body(action: str) -> bool:
    """A group whose only remaining answers need the user to write a shell body."""
    return action in _AUTHORING_ACTIONS


def _is_per_entry_snippet_group(action: str) -> bool:
    """A group resolved one entry at a time, with an editor behind at least one answer."""
    return action in _AUTHORING_ACTIONS or action == UNREPRODUCIBLE_UPDATE_REVIEW_ACTION


def _is_collateral_group(action: str) -> bool:
    return action == COLLATERAL_REVIEW_ACTION


def _is_repo_conflict_group(action: str) -> bool:
    return action == REPO_CONFLICT_REVIEW_ACTION


def _is_repo_removal_group(action: str) -> bool:
    return action == REPO_REMOVAL_REVIEW_ACTION


def _is_promotable_group(action: str) -> bool:
    return action in _PROMOTABLE_ACTIONS


def _is_conflicting_group(action: str) -> bool:
    """A group whose items are on BOTH machines with different content, and whose permanent
    answer is therefore the one that needs a side (`PKG-FR-MARK-SIDE`).

    Both halves are asserted, not just the action value: a group that cannot be answered
    permanently at all cannot reach the follow-up, whatever it is called. Today that leaves
    the apt configuration files and nothing else — a snap's revision change carries its own
    sentinel action precisely because it may never be recorded
    (`PKG-FR-NO-MARK-ON-SNAP-REVISION`).
    """
    return action == _CHANGE_ACTION and _is_promotable_group(action)


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


@dataclass(frozen=True)
class ReviewPolicy:
    """What the command line answers about a review before anyone is asked (issue #245).

    `--apply-package-installs` and `--apply-package-removals` let an unattended run converge
    the source's package state. Each answers ONE direction, so a run can converge what the
    source has without also carrying out what it no longer has.

    A flag answers as the SOURCE dictates, not as the screen's own pre-selection suggests
    (`_default_decision`): a removal screen starts at skip-once because confirming it unread
    would destroy something, and `--apply-package-removals` is the user saying that this run
    they mean it. Neither flag ever produces `SKIP_ALWAYS`: a machine-specific mark is the
    user's own statement about one machine, and a run nobody is watching may not make one
    (`PKG-FR-APPLY-FLAGS-NO-MARK`).

    Deliberately narrower than "answer everything". `policy_decision` names the groups no
    flag answers, and each one is a question about THIS machine rather than about what the
    source has, so the source cannot stand in for the answer.
    """

    apply_installs: bool = False
    apply_removals: bool = False

    @property
    def answers_anything(self) -> bool:
        """Whether any flag is in force at all — a run with neither takes today's path
        untouched, prompt for prompt and warning for warning."""
        return self.apply_installs or self.apply_removals


def policy_decision(group: ReviewGroup, policy: ReviewPolicy) -> Decision | None:
    """The answer `policy` gives every entry of `group`, or `None` where no flag answers it.

    Whole groups, never single entries: a group IS one direction's question about one item
    class, which is exactly the granularity the two flags are stated at.

    The `None` cases are the issue's own out-of-scope list, and each is a question the
    source's state cannot answer:

    - report-only, which nobody answers at all (`asks_for_a_decision`);
    - `overwrites_authored_content` — today an `/etc/apt/apt.conf.d` file the target already
      holds, which states how the user's own apt behaves on that machine;
    - a repository conflict (`PKG-FR-REPO-CONFLICT`), which moves where software the target recorded
      machine-specific comes from;
    - an unreproducible item still to be resolved (`PKG-FR-MANUAL-RESOLUTION`), and the narrowed menu the converge
      loop puts after a body that changed no version (`PKG-FR-VERSION-SNIPPET`): the only answers either offers
      are an authored shell body and a skip, and no flag can write one.

    A version difference whose body the source already holds is NOT in that list: replaying
    a recorded body is an install-direction act needing nothing authored, so
    `--apply-package-installs` answers it like any other addition. What the loop behind it
    then does with nobody to ask is its own rule — one attempt, then a warning.

    Everything else is one of the two directions. Removal covers `remove`/`delete`/`disable`,
    a repository or pin deletion, and the collateral question (`PKG-FR-COLLATERAL-MANUAL`) — a protected package an
    approved transaction would lose is a loss on the target, so it is the removal flag that
    speaks for it. Install-direction is the remainder: `install`/`add`/`enable`, and `change`,
    which converges an item both machines have to the source's version.
    """
    if not asks_for_a_decision(group):
        return None
    if group.overwrites_authored_content:
        return None
    if _is_repo_conflict_group(group.action) or _asks_for_an_authored_body(group.action):
        return None
    if _is_removal_direction(group.action) or _is_collateral_group(group.action):
        return Decision.APPLY if policy.apply_removals else None
    return Decision.APPLY if policy.apply_installs else None


def policy_answers_any(groups: Sequence[ReviewGroup], policy: ReviewPolicy) -> bool:
    """Whether `policy` answers at least one of `groups`.

    The job's own test for "somebody answered this review" on a run with no terminal, where
    `ReviewOutcome.was_interactive` says only whether a HUMAN did (`sync_core`). Pure, so a
    job can ask it of groups it has not put to the reviewer yet.
    """
    return any(policy_decision(group, policy) is not None for group in groups)


def _answer_by_policy(
    groups: Sequence[ReviewGroup], policy: ReviewPolicy | None, log: logging.Logger
) -> tuple[dict[str, Decision], Sequence[ReviewGroup]]:
    """Split `groups` into the decisions `policy` supplies and the groups still to be put.

    What is left over is handed to the ordinary path rather than declined here, so a flag
    changes nothing about a group it does not answer: on a terminal the user is still asked,
    and without one the entries are still named in a warning and skipped for this run (`PKG-FR-NO-TERMINAL`).

    A `policy` of `None`, or one with no flag in force, answers nothing and returns `groups`
    untouched — which is what makes a run without the flags identical to one before they
    existed.
    """
    if policy is None or not policy.answers_anything:
        return {}, groups

    decisions: dict[str, Decision] = {}
    remaining: list[ReviewGroup] = []
    for group in groups:
        decision = policy_decision(group, policy)
        if decision is None:
            remaining.append(group)
            continue
        for entry in group.entries:
            decisions[entry.item_id] = decision

    if decisions:
        _log_policy_answers(decisions, policy, log)
    return decisions, tuple(remaining)


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
    and each skip as the state it leaves behind plus how long that lasts. Not "proceed" or
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
    """The answers one group's screen offers — three, or two where `PKG-FR-SKIP-ONCE` records nothing.

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
    """Read on the editor's own screen, so a user does not author a snippet that hangs the
    sync (T-02-18): the executor supplies no stdin, and a worked shape showing the
    DEBIAN_FRONTEND=noninteractive + dependency-fix pattern is cheaper to read here than to
    discover as a stuck sync. Said as what happens on the machine that will run it, rather
    than as a fact about the executor.

    It states the install-or-update contract too (`PKG-FR-VERSION-SNIPPET`): the body is replayed onto a machine
    that may already hold an older version, and an installer that no-ops over an existing
    tree is the one failure the run cannot fix on the author's behalf.
    """
    return (
        f"These commands run on {target_hostname} with nobody watching — there is no keyboard\n"
        "attached to them, so a command that asks a question (e.g. a debconf prompt) hangs the\n"
        f"sync instead of failing. {target_hostname} may already hold an OLDER version, so write\n"
        "this to install or to update, whichever applies. A typical shape:\n\n"
        "  sudo DEBIAN_FRONTEND=noninteractive dpkg --install /path/to/package.deb || \\\n"
        "  sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --fix-broken\n"
    )


def _version_authoring_note(source_hostname: str, target_hostname: str) -> str:
    """The second editor's own screen: what the installed-version snippet has to do, and the
    one obligation pc-switcher cannot check for the author (`PKG-FR-VERSION-SNIPPET`, `PKG-FR-VERSION-SNIPPET`).

    Both machines are named because it runs on both, every sync, while the run is still
    planning — which is also why it must be read-only and why it is not covered by
    `--confirm-each-command`. Said as what happens on the machines rather than as a fact
    about `plan()`.
    """
    return (
        f"This runs on {source_hostname} AND on {target_hostname}, on every sync, before anything\n"
        "is proposed. It must print the version installed on whichever machine runs it — not the\n"
        "version the commands above would install — and it must change nothing: pc-switcher\n"
        "cannot check that and does not ask you to confirm it. The two machines' output is\n"
        "compared as text, so anything stable will do. A typical shape:\n\n"
        "  /opt/foo/bin/foo --version\n"
    )


# Shown in place of questionary's own multiline instruction, which offers "Alt+Enter or Esc
# then Enter" — two chords for one gesture, neither of which a user guesses.
_SNIPPET_FINISH_HINT = "(Ctrl-D to finish)\n>"


class _SnippetInstruction(str):
    """The snippet editor's header — the authoring note and the finish key — which is on
    screen while the editor is open and gone from the scrollback once it is answered.

    Every other review screen collapses to its title and the answer (`decision_list` drops
    its legend on `answered`). This one could not: `questionary.text` appends `instruction`
    on EVERY render, the final one included, with no `is_done` check of its own, and the
    note printed above the editor was ordinary console scrollback nothing could take back
    (#236).

    A `str` subclass because that is what questionary's signature takes and what its
    truthiness test reads; the override is on `__format__`, which is what
    `"{}".format(instruction)` calls once per render — so prompt_toolkit's own final,
    `is_done` render is where the header drops out. Nothing about the Ctrl-D binding or the
    multiline buffer changes.
    """

    __slots__ = ()

    def __format__(self, _spec: str, /) -> str:
        return "" if is_done() else str(self)


def _snippet_instruction(note: str) -> _SnippetInstruction:
    """The editor header for one body: its own note, then the finish key."""
    return _SnippetInstruction(f"\n{note}\n{_SNIPPET_FINISH_HINT}")


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


def _bare_item_name(label: str) -> str:
    """A review label with its trailing `(...)` parenthetical dropped.

    Item labels carry the version the machine holds — `tree (2.1.1-2ubuntu3)` — which a
    report line then states per machine anyway. Stripping the suffix rather than asking every
    manager for a second, version-free label: the parenthetical is the one shape every
    `label()` in the codebase shares, and a label that has none is returned untouched.
    """
    return _TRAILING_PARENTHETICAL.sub("", label)


def _render_group_panel(group: ReviewGroup) -> Panel:
    """Build the REPORT panel for one group — the non-interactive path only, where there is
    nothing to answer and this is all the user gets (`PKG-FR-NO-TERMINAL`).

    An interactive run never prints it: the decision screen lists the same items, and a
    panel above it made every group appear twice.

    A report group's lines are `name: finding`, with no verb: nothing acts on a reported
    condition, so the leading "report" named an action that does not exist, and the label's
    own version was the same number the finding then attributes to each machine.

    Package names, versions and stderr fragments come from package-manager output and
    must never reach a `Panel` as a bare `str` — Rich would parse `[...]`-shaped
    substrings as console markup and raise `MarkupError` (T-02-02).
    """
    body = Text()
    reported = group.action == _REPORT_ACTION
    for index, entry in enumerate(group.entries):
        if index:
            # Separator, not terminator: a newline after the last entry renders as an empty
            # final line inside the panel border.
            body.append("\n")
        if reported:
            body.append(_bare_item_name(entry.label), style="bold")
            if entry.detail:
                body.append(": ")
                body.append(entry.detail, style="dim")
            continue
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


# The unreproducible screens' own act: not a `Decision` at all, because answering it opens
# an editor and only what comes back decides whether the item is resolved.
_ADD_SNIPPET_VALUE = "add_snippet"
_ADD_SNIPPET_GLYPH = "◆"

# The key for "replace the recorded snippet before replaying it", on the two screens that
# already have a recorded one. `w` for write: `y` is spent on the act that replays what is
# there, and `x` on the permanent answer wherever one is offered.
_NEW_SNIPPET_KEY = "w"


def _unreproducible_options(
    source_hostname: str, target_hostname: str, verb: str = "install"
) -> tuple[DecisionOption, ...]:
    """The three answers for an item this run cannot reproduce yet (`PKG-FR-MANUAL-RESOLUTION`).

    In the review's own order — act, skip now, never — so the keys mean here what they mean
    on every other screen, and the act is the one that resolves the item rather than the one
    that is listed first because it is the interesting case.

    `verb` is the entry's own, because the same screen resolves two cases: software
    `target_hostname` does not have, which is installed, and software it has at another
    version, which is updated (`PKG-FR-VERSION-SNIPPET`). Calling the second one "install" would state something
    false about what is on the machine.
    """
    return (
        DecisionOption(
            value=_ADD_SNIPPET_VALUE,
            key=_APPLY_KEY,
            word=verb,
            glyph=_ADD_SNIPPET_GLYPH,
            is_act=True,
            hint=f"write a command snippet that {verb}s it; {target_hostname} runs it",
        ),
        DecisionOption(
            value=Decision.SKIP_ONCE,
            key=_SKIP_NOW_KEY,
            word=SKIP_NOW_WORD,
            glyph=_SKIP_ONCE_GLYPH,
            hint=f"do not {verb} on {target_hostname} for now; will be asked again next sync",
        ),
        DecisionOption(
            value=Decision.SKIP_ALWAYS,
            key=_SKIP_ALWAYS_KEY,
            word=f"never {verb}",
            glyph=_SKIP_ALWAYS_GLYPH,
            is_permanent=True,
            hint=f"do not {verb} on {target_hostname} for good; it is {source_hostname}'s own, "
            "and will not be asked again",
        ),
    )


def _update_options(target_hostname: str) -> tuple[DecisionOption, ...]:
    """The three answers for an item both machines have at different versions (`PKG-FR-VERSION-SNIPPET`).

    No permanent answer, for `PKG-FR-NO-MARK-ON-SNAP-REVISION`'s reason one ecosystem over:
    nobody holds a version as a standing preference about one machine, and a mark would
    leave the two machines' records disagreeing about software neither would raise again.
    Skipping says what the user means and the difference surfaces on the next sync.
    """
    return (
        DecisionOption(
            value=Decision.APPLY,
            key=_APPLY_KEY,
            word="update",
            glyph=_APPLY_GLYPH,
            is_act=True,
            hint=f"run the recorded snippet on {target_hostname}",
        ),
        DecisionOption(
            value=_ADD_SNIPPET_VALUE,
            key=_NEW_SNIPPET_KEY,
            word="new snippet",
            glyph=_ADD_SNIPPET_GLYPH,
            hint=f"rewrite the snippet first, then run it on {target_hostname}",
        ),
        DecisionOption(
            value=Decision.SKIP_ONCE,
            key=_SKIP_NOW_KEY,
            word=SKIP_NOW_WORD,
            glyph=_SKIP_ONCE_GLYPH,
            hint=f"leave {target_hostname}'s version as it is for now; will be asked again next sync",
        ),
    )


def _retry_options(target_hostname: str) -> tuple[DecisionOption, ...]:
    """The narrowed menu after a snippet ran and moved no version (`PKG-FR-VERSION-SNIPPET`).

    Two answers, because the third has just been carried out and did nothing: replaying the
    same bytes again is the same no-op, and offering it would invite a loop with no exit.
    """
    return (
        DecisionOption(
            value=_ADD_SNIPPET_VALUE,
            key=_NEW_SNIPPET_KEY,
            word="new snippet",
            glyph=_ADD_SNIPPET_GLYPH,
            is_act=True,
            hint=f"rewrite the snippet, then run it on {target_hostname} again",
        ),
        DecisionOption(
            value=Decision.SKIP_ONCE,
            key=_SKIP_NOW_KEY,
            word=SKIP_NOW_WORD,
            glyph=_SKIP_ONCE_GLYPH,
            hint=f"leave {target_hostname}'s version as it is for now; will be asked again next sync",
        ),
    )


async def _ask_about_one_item(  # noqa: PLR0913 - one decision screen's content; all but the entry keyword-only
    entry: ReviewEntry,
    *,
    title: str,
    options: Sequence[DecisionOption],
    default: str,
    explanation: str | None = None,
    detail: str | None = None,
) -> str:
    """Put ONE item on a decision screen and return the answer's value.

    Some questions cannot be batched: a collateral package's answers name the change that
    causes it, and an unreproducible item's "install" answer opens an editor. Both were
    plain pickers of sentence-long choices, which made them the two screens in the review
    that looked nothing like the rest of it. The shape is what is shared here, not the
    batching — one row, the same glyphs, colours, keys and hint column.

    `explanation` sits between the title and the key legend, for a screen whose title states
    the concrete case and whose ground is a sentence rather than a row annotation. `detail`
    overrides the row's own line; the empty string suppresses it, which is what a screen
    whose title and explanation already carry `entry.detail` passes.

    Ctrl-C (`ask` returns `None`) is the caller's to interpret: what stopping means differs
    between an item that can be left undecided and one that cannot.
    """
    prompt = decision_list(
        title,
        rows=[
            DecisionRow(
                row_id=entry.item_id,
                label=entry.label,
                default=default,
                detail=entry.detail if detail is None else detail,
            )
        ],
        options=options,
        explanation=explanation,
    )
    answered: Mapping[str, str] | None = await asyncio.to_thread(prompt.ask)
    if answered is None:
        raise SyncAbortedByUser(f"package review aborted at {entry.label!r} (Ctrl-C)")
    return answered[entry.item_id]


async def _capture_body(prompt_title: str, note: str, existing: str) -> str:
    """Open one editor and return what it captured, stripped.

    The authoring note rides on the prompt rather than being printed above it: printed, it
    stayed in the scrollback beside the body the user wrote, which no other screen does
    (#236).

    `existing` prefills the buffer, which is what makes rewriting a recorded body an edit
    rather than a retype (`PKG-FR-VERSION-SNIPPET`): the body that did not converge is usually nearly right, and
    a blank editor invites a shorter, worse replacement. The empty string is an ordinary
    value here — a first authoring simply opens on nothing.

    Stripped once, here, before anything else sees it: the registry, the plan and the replay
    all get the same string, so what the user reads in the YAML file is exactly what runs
    (`PKG-FR-SNIPPET-VERBATIM`). That is not reasoning about the body (`PKG-FR-SNIPPET-VERBATIM`) — the editor's
    own trailing newlines and the blank lines a paste leaves behind are not something the
    user typed as part of the command, and they change nothing about what `bash -c` runs.
    """
    prompt = questionary.text(
        prompt_title,
        multiline=True,
        default=existing,
        instruction=_snippet_instruction(note),
        key_bindings=_SNIPPET_SUBMIT_BINDINGS,
        style=_SNIPPET_STYLE,
    )
    captured: str | None = await asyncio.to_thread(prompt.ask)
    return captured.strip() if captured else ""


async def _capture_bodies(
    entry: ReviewEntry, *, source_hostname: str, target_hostname: str, recorded: SnippetBodies | None
) -> SnippetBodies | None:
    """Both bodies for one item, or `None` where either editor came back empty (`PKG-FR-VERSION-SNIPPET`).

    Two editors, always, and never one: an entry carrying only an install body is one the
    registry itself refuses to parse back, so authoring the pair is what authoring means.
    The second opens on whatever version body the item already had, which is how a rewrite
    that only needs the install half costs one keystroke.

    `None` for an empty capture of EITHER body, which the caller turns into a re-prompt
    rather than a resolution (`PKG-FR-SNIPPET-VERBATIM`: an empty snippet is not an answer).
    A body of only spaces and newlines lands here as empty for the same reason it would
    replay as nothing at all.
    """
    install_body = await _capture_body(
        f"Install-or-update snippet for {entry.label}:",
        _snippet_authoring_note(target_hostname),
        recorded.install_body if recorded else "",
    )
    if not install_body:
        return None
    version_body = await _capture_body(
        f"Installed-version snippet for {entry.label}:",
        _version_authoring_note(source_hostname, target_hostname),
        recorded.version_body if recorded else "",
    )
    if not version_body:
        return None
    return SnippetBodies(install_body=install_body, version_body=version_body)


async def _review_snippet_group(  # noqa: PLR0913 - screen content plus the two dicts it fills; all but the group keyword-only
    group: ReviewGroup,
    *,
    console: Console,
    source_hostname: str,
    target_hostname: str,
    decisions: dict[str, Decision],
    snippets: dict[str, SnippetBodies],
) -> None:
    """Resolve one per-entry snippet group, one item at a time (`PKG-FR-MANUAL-RESOLUTION`, `PKG-FR-VERSION-SNIPPET`).

    Three groups share this flow because they share its shape — one item per screen, because
    the answer that resolves it opens an editor — and differ only in the answers offered:

    - `UNREPRODUCIBLE_REVIEW_ACTION`: write a snippet, skip for now, or never do it here.
      All three are VALID resolutions (`PKG-FR-MANUAL-RESOLUTION`) and there is no fourth "genuinely undecided"
      outcome (decision 10 — unresolved must be unrepresentable in an interactive flow).
    - `UNREPRODUCIBLE_UPDATE_REVIEW_ACTION`: run the recorded snippet, rewrite it first, or
      leave the version alone this run. No permanent answer.
    - `UNREPRODUCIBLE_RETRY_REVIEW_ACTION`: the same two minus the act, put by the converge
      loop after a body that moved no version.

    Two exits are shared by all three:

    - Ctrl-C at the screen means the user wants to stop, so `_ask_about_one_item` aborts the
      ENTIRE sync with `SyncAbortedByUser` — never a per-item skip-and-mark-unresolved.
    - Choosing to write a snippet and then submitting an empty body, for either half, is NOT
      accepted and does NOT fall through: the choice is re-prompted so the user must supply
      real bodies or pick an explicit skip.

    `ReviewGroup.recorded_bodies` carries what the registry already holds, so a rewrite opens
    on it; it is absent for the resolve group, whose items have nothing recorded by
    definition.
    """
    recorded = group.recorded_bodies or {}
    is_update = group.action == UNREPRODUCIBLE_UPDATE_REVIEW_ACTION
    is_retry = group.action == UNREPRODUCIBLE_RETRY_REVIEW_ACTION
    for entry in group.entries:
        verb = entry.action_label
        if is_update:
            options = _update_options(target_hostname)
            title = f"{target_hostname} has a different version of {entry.label} — update it?"
            default = Decision.APPLY.value
        elif is_retry:
            options = _retry_options(target_hostname)
            title = group.title
            default = _ADD_SNIPPET_VALUE
        else:
            options = _unreproducible_options(source_hostname, target_hostname, verb)
            title = f"{verb.capitalize()} {entry.label} on {target_hostname}?"
            default = _ADD_SNIPPET_VALUE

        # Re-prompt until the entry is resolved by real bodies or an explicit answer. An
        # empty capture loops back here rather than manufacturing an unresolved item
        # (decision 10); a cancelled screen breaks out by aborting the whole sync.
        while True:
            selected = await _ask_about_one_item(entry, title=title, options=options, default=default)

            if selected in (Decision.SKIP_ALWAYS, Decision.SKIP_ONCE, Decision.APPLY):
                # Every one of these is a real decision (`PKG-FR-MANUAL-RESOLUTION`): the item is resolved, this
                # run or for good.
                decisions[entry.item_id] = Decision(selected)
                break

            bodies = await _capture_bodies(
                entry,
                source_hostname=source_hostname,
                target_hostname=target_hostname,
                recorded=recorded.get(entry.item_id),
            )
            if bodies is not None:
                snippets[entry.item_id] = bodies
                break

            console.print(Text("Neither snippet can be empty — enter both, or choose a skip.", style="yellow"))


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

    The whole of `PKG-FR-SKIP-ONCE` in one pass: each row starts at `_default_decision` and ends wherever
    the user left it, so there is no leftover set to re-offer and no way for a screen asking
    about permanence to echo back an item's action. Every entry gets a decision, because the
    screen carries one per row from the moment it opens.

    Ctrl-C (`ask` returns `None`) aborts the WHOLE sync like every other review screen —
    never a silent fallthrough that leaves this and every later group undecided.

    `ReviewGroup.note` becomes the screen's explanation, between the title and the keys,
    where the group carries one. It used to reach a report group alone; a removal whose
    reach the rows cannot state — deleting a path leaves what the snippet dropped elsewhere
    behind — needs the same sentence in front of an answer rather than after it.
    """
    prompt = decision_list(
        group.title,
        rows=_rows_for(group),
        options=_options_for(group, source_hostname=source_hostname, target_hostname=target_hostname),
        explanation=group.note,
    )
    answered: Mapping[str, str] | None = await asyncio.to_thread(prompt.ask)

    if answered is None:
        raise SyncAbortedByUser(f"package review aborted at {group.title!r} (Ctrl-C)")

    for entry in group.entries:
        decisions[entry.item_id] = Decision(answered[entry.item_id])


# The keys of the follow-up screen's three answers. Letters rather than the hostnames' own
# initials: two machines can share one, an initial can be `a` (Abort's letter, which
# `decision_list` rejects) or not a lowercase letter at all. `h` and `o` are here and other
# — the review runs on the machine the sync was launched from, which is the source — and
# neither word appears on the screen, where the answers are the hostnames themselves.
_MARK_HERE_KEY = "h"
_MARK_OTHER_KEY = "o"
_MARK_BOTH_KEY = "b"

# Left half / right half / whole. Which side of the pair the answer keeps is the whole
# question, so the glyphs say it without the words — the three column words are two
# hostnames and "both", which colour cannot distinguish and this screen tints identically
# anyway (every answer here is recorded).
_MARK_SOURCE_GLYPH = "◐"
_MARK_TARGET_GLYPH = "◑"
_MARK_BOTH_GLYPH = "●"


def _mark_side_options(source_hostname: str, target_hostname: str) -> tuple[DecisionOption, ...]:
    """The three answers to "whose own copy is this?" (`PKG-FR-MARK-SIDE`).

    Each names a machine and says what the answer leaves standing there, in one grammar
    across all three (`PKG-FR-ANSWERS-AS-A-SET`). What actually differs between them is
    where the mark is recorded and therefore how long it lives: a mark makes the item inert
    in both roles wherever it sits, so every one of these answers already stops the
    overwrite — but an entry is dropped once the machine holding it no longer has the item
    (`PKG-FR-MARK-LIFETIME`), which is what "while <machine> has it" states and what makes
    `both` more than a hedge.

    All three are `is_permanent`: this screen has no act and no one-sync answer, so there
    is nothing here the emphasis could distinguish it from, and understating a recorded
    answer is the error worth avoiding.
    """
    return (
        DecisionOption(
            value=MarkSide.SOURCE,
            key=_MARK_HERE_KEY,
            word=source_hostname,
            glyph=_MARK_SOURCE_GLYPH,
            is_permanent=True,
            hint=f"it is {source_hostname}'s own version; nothing overwrites it while {source_hostname} has it",
        ),
        DecisionOption(
            value=MarkSide.TARGET,
            key=_MARK_OTHER_KEY,
            word=target_hostname,
            glyph=_MARK_TARGET_GLYPH,
            is_permanent=True,
            hint=f"it is {target_hostname}'s own version; nothing overwrites it while {target_hostname} has it",
        ),
        DecisionOption(
            value=MarkSide.BOTH,
            key=_MARK_BOTH_KEY,
            word="both",
            glyph=_MARK_BOTH_GLYPH,
            is_permanent=True,
            hint=f"each version is its own machine's; nothing overwrites {source_hostname}'s while it has it, "
            f"nor {target_hostname}'s while it has it",
        ),
    )


def _conflicting_permanent_entries(
    groups: Sequence[ReviewGroup], decisions: Mapping[str, Decision]
) -> tuple[ReviewEntry, ...]:
    """The entries the follow-up is about: conflicting items answered `SKIP_ALWAYS`."""
    return tuple(
        entry
        for group in groups
        if _is_conflicting_group(group.action)
        for entry in group.entries
        if decisions.get(entry.item_id) is Decision.SKIP_ALWAYS
    )


async def _ask_mark_sides(
    groups: Sequence[ReviewGroup],
    *,
    source_hostname: str,
    target_hostname: str,
    decisions: Mapping[str, Decision],
) -> dict[str, MarkSide]:
    """Ask, once and for all of them, whose own copy each permanently-kept conflicting item
    is (`PKG-FR-MARK-SIDE`).

    One screen, a row per item, three answers per row — the batch `PKG-FR-BATCHED` asks for, and what
    `decision_list` already supports: it takes any number of options with any values, so
    nothing about the question forces a screen per item. The two other reasons a package
    question is asked one at a time do not apply: no answer here opens an editor, and there
    is nothing to READ before answering that the row's own detail cannot carry.

    A follow-up rather than a fourth answer on the batch screen, because the question exists
    only for the rows answered permanently. Folding it in would have put five answers on
    every conflict row — three of them the same decision under three names — and asked about
    a side on rows that will never have a mark.

    The default is `TARGET`: an unread screen then records what the permanent answer on the
    batch already said in its own words ("do not change on <target> for good; it is
    <target>'s own"), so confirming without choosing changes nothing about how the tool has
    always behaved.

    Ctrl-C aborts the whole sync like every other screen in the review. The rows carry each
    item's own detail — normally "<atlas> has X, <nomad> has Y" — because that difference is
    what the question is about and the batch screen is already off the top of the terminal.
    """
    entries = _conflicting_permanent_entries(groups, decisions)
    if not entries:
        return {}

    prompt = decision_list(
        "Kept for good — whose own version is it?",
        rows=[
            DecisionRow(row_id=entry.item_id, label=entry.label, default=MarkSide.TARGET, detail=entry.detail)
            for entry in entries
        ],
        explanation=(
            f"{source_hostname} and {target_hostname} both have these, with different content, so neither "
            "version travels from now on. Naming a machine says whose copy the answer is about, and the "
            "answer lasts as long as that machine still has the item."
        ),
        options=_mark_side_options(source_hostname, target_hostname),
    )
    answered: Mapping[str, str] | None = await asyncio.to_thread(prompt.ask)
    if answered is None:
        raise SyncAbortedByUser("package review aborted at the machine-specific follow-up (Ctrl-C)")
    return {entry.item_id: MarkSide(answered[entry.item_id]) for entry in entries}


# The collateral screen's third answer. Not a decision about the item — it ends the run —
# so it carries a value no `Decision` has, and `q` for quit rather than a letter the other
# screens spend on an answer.
_STOP_SYNC_VALUE = "stop_sync"
_STOP_SYNC_KEY = "q"
_STOP_SYNC_GLYPH = "■"


def _collateral_options(entry: ReviewEntry, target_hostname: str) -> tuple[DecisionOption, ...]:
    """The three answers `PKG-FR-COLLATERAL-MANUAL` requires for one collateral package.

    The act and skip sentences come from the entry (`answer_hints`), because they name the
    change that causes the collateral and what it does — "remove fortunes-min from nomad, so
    fortunes is removed as well" — which differs per item and will differ more as removals
    stop being the only cause. A caller that supplies none gets sentences that are true of
    every collateral item and specific to none.
    """
    act_hint, skip_hint = entry.answer_hints or (
        f"the change described above is applied on {target_hostname}",
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
    three-way choice `PKG-FR-COLLATERAL-MANUAL` requires for a package the pending transaction would remove,
    downgrade or upgrade behind the user's back: let it happen, protect the package, or
    stop. Never a row on a decision screen — losing a package the user chose to have is not
    the same question as approving an install off a list.

    What is protected is a fact about the TARGET (`Collateral.protected`): its own
    `apt-mark showmanual` set, plus the packages that machine marked machine-specific.
    Either the user asked for the package on the machine being changed, or they told this
    tool to leave it alone there. Which of the two applies to THIS package is the second
    half of `entry.detail`, composed by `Collateral` — the only layer that knows the ground
    — and is printed as the screen's explanation, under the case it is the ground for.

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
        # The question IS this one package, so its title is this one case — "Removing
        # fortunes-min on nomad would remove fortunes" — and `group.title`, which has to be
        # true of every entry at once, stays the heading of the report a run with no
        # terminal prints. `Collateral._item` composes the detail as the finding, a newline,
        # then the ground; the finding is the question and the ground is why it is being
        # asked, so they land above the legend rather than under the row.
        finding, _, ground = (entry.detail or "").partition("\n")
        selected = await _ask_about_one_item(
            entry,
            title=finding or group.title,
            explanation=ground or None,
            detail="",
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
    """Resolve one `REPO_CONFLICT_REVIEW_ACTION` group with the two-way choice `PKG-FR-REPO-CONFLICT`
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


def _log_policy_answers(decisions: Mapping[str, Decision], policy: ReviewPolicy, log: logging.Logger) -> None:
    """Say that the command line answered, and with which flag (`PKG-FR-LOG-DECISIONS`).

    The per-item decision lines a job writes afterwards read the same whoever gave the
    answer, so without this one line the log of an unattended run cannot be told from the log
    of a run somebody sat through. A count and the flags, not the item names: each item is
    named on its own line already.
    """
    flags = " ".join(
        flag
        for flag, enabled in (
            ("--apply-package-installs", policy.apply_installs),
            ("--apply-package-removals", policy.apply_removals),
        )
        if enabled
    )
    log.info("%d review item(s) answered by the command line, unasked: %s", len(decisions), flags)


def _warn_every_item_unasked(groups: Sequence[ReviewGroup], log: logging.Logger) -> None:
    """Name every item a run with no terminal could not ask about (`PKG-FR-LOG-DECISIONS`).

    Names, not a count: every one of them is declined for this run, and a number says which
    items were declined to nobody.
    """
    for group in groups:
        for entry in group.entries:
            log.warning("%s — not asked, declined for this run (no TTY): %s", group.title, entry.label)


async def review_items(  # noqa: PLR0913 - review surface plus both machine names; all but the groups keyword-only
    groups: Sequence[ReviewGroup],
    *,
    console: Console,
    ui: PausableUI,
    source_hostname: str,
    target_hostname: str,
    logger: logging.Logger | None = None,
    policy: ReviewPolicy | None = None,
) -> ReviewOutcome:
    """Present every group as one decision screen and return the user's decisions.

    Both machine names are required, not defaulted: every screen here names the machine an
    answer acts on, and "the target" is a word for the tool's own plumbing rather than for
    either of the user's computers. A caller that cannot name them has no business asking
    these questions.

    `policy` is the command line's own answer to whole groups (`ReviewPolicy`, issue #245),
    applied FIRST and to nothing else: the groups it answers are never put to anyone, and
    every remaining group takes the path below unchanged. Answering here rather than in the
    caller is what puts one flag in front of every review this module serves, the collateral
    question `apt_sync` asks mid-apply included (`PKG-FR-ASK-AGAIN`). The returned
    `was_interactive` stays a statement about a HUMAN — a policy-answered set was decided by
    nobody, so a run whose every group the flags answered comes back False and records no
    machine-specific mark (`sync_core._record_permanent_skips`).

    Non-interactive runs (`is_interactive(console)` is False) prompt for nothing: every
    item comes back `SKIP_ONCE`, nothing is recorded permanently, one warning NAMES each
    item nobody could be asked about (`_warn_every_item_unasked`), and the group panels are
    printed as the report (`PKG-FR-NO-TERMINAL`).
    Interactive runs pause `ui` around each group's blocking prompt (dispatched via
    `asyncio.to_thread`) and resume it in a `finally`, so the live display is always handed
    back even if the prompt raises. They print no group panel: the screen lists the items
    itself, and its answered form stays in the scrollback as the record.

    One further screen can follow the groups on an interactive run: `_ask_mark_sides`, for
    the conflicting items answered permanently (`PKG-FR-MARK-SIDE`). Both paths that return
    early — no TTY, and the automation environment — leave `mark_sides` empty, which is what
    keeps a permanent answer nobody gave from also choosing a machine.
    """
    log = logger if logger is not None else _logger

    by_policy, groups = _answer_by_policy(groups, policy, log)

    # Whatever the flags left over goes through unchanged, the empty set included: a review
    # the flags answered whole reaches the branches below with no groups, which is what an
    # empty plan already does (a terminal still gets its pause/resume, and `was_interactive`
    # still says only whether there was one).
    automation_raw = os.environ.get(PACKAGE_REVIEW_AUTOMATION_ENV)
    if automation_raw is not None:
        return ReviewOutcome(
            decisions={**by_policy, **_decisions_from_automation(groups, automation_raw)}, was_interactive=True
        )

    if not is_interactive(console):
        _warn_every_item_unasked(groups, log)
        for group in groups:
            console.print(_render_group_panel(group))
        decisions = {
            **by_policy,
            **{entry.item_id: Decision.SKIP_ONCE for group in groups for entry in group.entries},
        }
        # `PKG-FR-NO-TERMINAL`: no capture is ever offered without a TTY, so every unreproducible item
        # is unresolved by construction — never a snippet, never a recorded decision.
        non_interactive_unresolved = tuple(
            entry.item_id for group in groups if _is_unreproducible_group(group.action) for entry in group.entries
        )
        return ReviewOutcome(decisions=decisions, was_interactive=False, unresolved=non_interactive_unresolved)

    ui.pause()
    decisions: dict[str, Decision] = dict(by_policy)
    snippets: dict[str, SnippetBodies] = {}
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

            if _is_per_entry_snippet_group(group.action):
                await _review_snippet_group(
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

        # After the batch, never inside it: the question is only about the rows that were
        # answered permanently, and which those are is not known until the screen is
        # confirmed (`PKG-FR-MARK-SIDE`). Still one round with nothing done between the
        # questions, which is what `PKG-FR-BATCHED` constrains.
        mark_sides = await _ask_mark_sides(
            groups,
            source_hostname=source_hostname,
            target_hostname=target_hostname,
            decisions=decisions,
        )
    finally:
        ui.resume()

    # An interactive review can no longer leave anything unresolved (decision 10): the
    # unreproducible flow re-prompts or aborts, and a decision screen's abort raises above —
    # so `unresolved` is always empty here. It stays populated only on the non-interactive
    # path (`PKG-FR-NO-TERMINAL` reporting).
    return ReviewOutcome(
        decisions=decisions, was_interactive=True, snippets=snippets, unresolved=(), mark_sides=mark_sides
    )


async def ask_gate(  # noqa: PLR0913 - one two-answer screen's content; all keyword-only
    *,
    title: str,
    message: str,
    proceed_label: str,
    stop_label: str,
    console: Console,
    ui: PausableUI,
    logger: logging.Logger | None = None,
) -> bool | None:
    """Ask one two-answer question about the MACHINE, before the review it precedes.

    Sibling of `review_items`, not a group inside it: both callers ask something whose
    answer decides what the review holds, so neither can be a row inside one. `apt_sync`'s
    Ubuntu Pro gate asks whether the job may run at all, and one of its answers means there
    is no review to present; `manual_installs_sync` asks whether an unowned `/opt` directory
    is one application or a publisher's shelf (`PKG-FR-MANUAL-OPT-SHAPE`), and the answer
    decides which items the review lists. It reuses this module for the pause-ask-resume
    `finally` and the interactivity test, which is the only place in the codebase that knows
    how to run a blocking `questionary` prompt under the Rich live display.

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
    """A package job's review seam (`PKG-FR-BATCHED`): given the groups one job planned, return that
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
    has — the command line's own answers, the automation-environment hook, the
    non-interactive path, and the pause/resume `finally` that lets the blocking prompt run
    inside the job TaskGroup. Mirrors `TerminalUIConfirmer`'s shape (console + UI + optional
    logger), constructed once by the orchestrator.

    `policy` is held here rather than passed per call so every review this one object serves
    is answered by the same flags, whichever job or round asks (issue #245).
    """

    def __init__(  # noqa: PLR0913 - one adapter's collaborators plus both machine names; all but the first two keyword-only
        self,
        console: Console,
        ui: PausableUI,
        *,
        source_hostname: str,
        target_hostname: str,
        logger: logging.Logger | None = None,
        policy: ReviewPolicy | None = None,
    ) -> None:
        self._console = console
        self._ui = ui
        self._source_hostname = source_hostname
        self._target_hostname = target_hostname
        self._logger = logger
        self._policy = policy

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        return await review_items(
            groups,
            console=self._console,
            ui=self._ui,
            source_hostname=self._source_hostname,
            target_hostname=self._target_hostname,
            logger=self._logger,
            policy=self._policy,
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
