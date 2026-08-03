"""`manual_installs_sync`: the fourth package job, owning everything no package manager
can reproduce (D-15, D-18, D-19, D-20, D-21).

Two detectors, run on BOTH machines (`PKG-FR-MANUAL-DIFF`) — the source's findings are the
candidates, and a finding the target already holds is dropped rather than presented:

- apt packages installed on the source whose INSTALLED version comes from no repository the
  source has configured — installed via `dpkg --install` of a bare `.deb`, so only dpkg's
  own status file accounts for them. Every installed package, not the `apt-mark showmanual`
  set: apt's manual/automatic mark says how the package got there, not whether any
  repository can supply it. On the target the question is only whether dpkg reports the name
  installed at all: software that is there is there, whatever origin put it there.
- paths under `/opt`, directly under `/usr/local`, and inside `/usr/local`'s `bin`, `sbin`,
  `lib`, `games` and `src` that no dpkg package owns — software an install script dropped
  there, bypassing apt entirely (`PKG-FR-MANUAL-SCOPE`). Never `/usr/local`'s own skeleton
  (`_USR_LOCAL_SKELETON`), and never a directory with no file anywhere beneath it. An
  unowned entry directly under `/opt` is judged by its own shape, which can take a question
  (`PKG-FR-MANUAL-OPT-SHAPE`).

D-18 gives them their own job and their own enable flag, because half of what they cover is
not apt's business at all (unowned files under `/usr/local`/`/opt`), and folding them into
`apt_sync` would make disabling apt silently disable manual-install detection with nothing
telling the user. This job does its OWN `dpkg`/`apt-cache` queries rather than sharing
`apt_sync`'s, so ownership stays clean — it never imports `apt_sync` (D-18). The
`apt-cache policy` PARSING both jobs need lives in `packages/apt_policy.py`, a third
module neither job owns.

An unreproducible item ends an interactive run resolved in one of three ways (D-21,
decision 10): it has an install snippet in the shared, synced registry (`SnippetRegistry`,
D-20/D-23), it is recorded machine-specific (skip-always) in this job's machine-local
decision file, or the user skipped it once — skip-once is a real decision. There is no
fourth "genuinely undecided" outcome an interactive review can reach: an empty snippet
capture re-prompts rather than falling through, and Ctrl-C/EOF aborts the whole sync
(`SyncAbortedByUser`) instead of leaving an item unresolved.

`ManualInstallsSyncJob` subclasses `PackageSyncJob` and overrides `plan()`, `converge()`,
`validate()` and `describe_first_sync_scope()`, following `SnapSyncJob`'s shape (a
non-apt item type driving an overridden `plan()` rather than the inherited apt-package
diff). The unreproducible-specific finalize logic lives here as an override of the base's
now-no-op hook, so the base `apply()` stays generic for the three managers that produce no
unreproducible items.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, override

from rich.markup import escape

from pcswitcher.config_sync import CONFIG_REMOTE_DIR
from pcswitcher.executor import Executor
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.apt_policy import installed_origins_by_package, packages_installed_from_no_repository
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
)
from pcswitcher.jobs.packages.probes import ProbeFailed, require_answer
from pcswitcher.jobs.packages.review import (
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.state import (
    SNIPPET_REGISTRY_RELPATH,
    DecisionEntry,
    DecisionFile,
    Snippet,
    SnippetRegistry,
    filter_inert,
    load_snippets_from_text,
)
from pcswitcher.jobs.packages.sync_core import PackagePlan, PackageSyncJob
from pcswitcher.models import (
    CommandResult,
    FirstSyncScope,
    Host,
    SyncAborted,
    ValidationError,
)
from pcswitcher.redaction import redact_credentials

__all__ = ["ManualInstallsSyncJob"]

# The bounded unowned-install scan (`PKG-FR-MANUAL-SCOPE`): `/opt`, every entry directly
# under `/usr/local`, and the entries of the five `/usr/local` subdirectories software
# actually lands in. One shell loop runs `find <root> -mindepth 1 -maxdepth 1` over each,
# skipping any that is not there so a missing root is not an error to tell apart from a
# broken one (`_list_scan_entries`). Enough to NAME a finding, never enough to walk a tree:
# the item is decided on, not replicated.
#
# `etc`, `include`, `man` and `share` are deliberately absent. What a hand install puts
# there arrives with an application this scan finds elsewhere, so scanning them would raise
# a second finding for software already named once — and `man` is a symlink to `share/man`,
# which a scan that followed it would walk twice.
_UNOWNED_SCAN_ROOTS = (
    "/opt",
    "/usr/local",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/local/lib",
    "/usr/local/games",
    "/usr/local/src",
)

# The nine entries `base-files.postinst` creates directly under `/usr/local`: eight through
# its own `install_local_dir` helper, plus `man` as a symlink to `share/man`. Hardcoded for
# predictability rather than read off the machine — the scan's shape must not change with
# whatever a postinst happens to say on the day — with a VM test asserting that the machine's
# own `base-files.postinst` still declares exactly these, so a distribution that changes the
# skeleton is a failing test rather than a surprise in a review.
#
# None of them is ever a finding (`PKG-FR-MANUAL-SCOPE`): the distribution ships them and no
# package need own them, so presenting one would ask every user, on every machine, on every
# run, to write an install snippet for a stock directory whose contents this scan already
# names one level deeper. This is also what keeps a scanned directory out of its own scan —
# the five `/usr/local` roots above are all in this set, and `find` names them as entries of
# `/usr/local` like any other candidate.
_USR_LOCAL_SKELETON = frozenset(
    {
        "/usr/local/bin",
        "/usr/local/etc",
        "/usr/local/games",
        "/usr/local/include",
        "/usr/local/lib",
        "/usr/local/man",
        "/usr/local/sbin",
        "/usr/local/share",
        "/usr/local/src",
    }
)

# Where the vendor-or-application shape question applies (`PKG-FR-MANUAL-OPT-SHAPE`).
# `/usr/local` has no such ambiguity: its own layout puts an application's parts under the
# skeleton directories this scan already looks in one at a time.
_OPT_ROOT = "/opt"

# `find`'s type letter for a directory (`-printf '%y'`). Every other letter — `f`, `l`, `s`,
# `p`, `b`, `c` — means "not a directory", which is all this scan asks of it: a file, a
# symlink and a socket are equally a thing that is there rather than an empty shape.
_DIRECTORY = "d"

# Matches one `dpkg --search` "owned" line: `<package>[,<package>...]: <path>`. A path dpkg does
# not own produces no such line at all (its "no path found" diagnostic goes to stderr,
# which this scan never inspects) — absence from stdout is the only signal
# `_owned_paths_from_dpkg_s` needs. A private copy of apt_sync's identical regex: D-18
# keeps ownership clean by NOT importing apt_sync, and this parser is small enough that
# one duplicated line is cheaper than a shared-core coupling.
_DPKG_S_OWNED_RE = re.compile(r"^[^:]+:\s+(?P<path>/\S.*)$")

# One extra path handed to the unowned scan's `dpkg --search`, whose "owned" line is the
# proof that dpkg answered at all (`_scan_unowned_installs`). dpkg owns its own binary by
# construction — the package is Essential and ships it — so a batch that comes back without
# this line came back from a dpkg that did not answer, whatever it exited. It is filtered
# out of the candidates before anything is reported.
_DPKG_OWNERSHIP_WITNESS = "/usr/bin/dpkg"


# -- manual-install item shape --------------------------------------------------------
#
# Here rather than in the shared `packages/items.py`: no other job constructs one.


@dataclass(frozen=True)
class UnreproducibleItem:
    """One item no package manager can reproduce (D-18): an apt package installed from no
    configured repository, or an unowned install under `/usr/local`/`/opt`.

    `origin` distinguishes how the item was found — `apt-no-candidate` (an installed
    package no repository can supply) versus `unowned-path` (a filesystem path dpkg does
    not claim) — and lives inside `item_id` for the same reason `scope`
    lives inside the two flatpak identities: the same `identifier` value can appear
    under both origins with no relation to each other (e.g. a package name that is
    also, coincidentally, a path component), so origin has to be part of identity, not
    just a field alongside it.

    Unlike the other item types, `label` here is a plain FIELD rather than a `label()`
    method: the human-readable description comes from whichever detector found the
    item (D-19's unowned-install scan, or the no-candidate check) and is not something
    this dataclass can derive from `origin`/`identifier` alone.
    """

    origin: Literal["apt-no-candidate", "unowned-path"]
    identifier: str
    label: str

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.UNREPRODUCIBLE

    @property
    def item_id(self) -> str:
        """Stable identity string: `unreproducible:<origin>:<identifier>`."""
        return f"unreproducible:{self.origin}:{self.identifier}"


def _lines(output: str) -> list[str]:
    """Non-blank, stripped lines — the shape every `apt-mark`/`find` list command this
    module runs produces. A private copy of apt_sync's identical helper (D-18)."""
    return [line.strip() for line in output.splitlines() if line.strip()]


def _owned_paths_from_dpkg_s(output: str) -> frozenset[str]:
    """Every path `dpkg --search` reports as owned, parsed from its stdout alone (D-19's
    unowned-install scan). A queried path dpkg does NOT own is simply absent from this set
    — its "no path found matching pattern" diagnostic is a stderr message this scan never
    reads, so a batched multi-path `dpkg --search` degrades cleanly even when some paths are
    unowned and others are not. A private copy of apt_sync's identical parser (D-18).
    """
    owned: set[str] = set()
    for line in output.splitlines():
        match = _DPKG_S_OWNED_RE.match(line)
        if match:
            owned.add(match.group("path"))
    return frozenset(owned)


def _typed_entries(output: str) -> list[tuple[str, str]]:
    """`(type letter, path)` per line of a `find -printf '%y\\t%p\\n'` listing.

    The type rides along with the path because every rule after the listing needs it — the
    `/opt` shape question counts directories against files, and the empty-directory rule
    only applies to a directory. Asking `find` for it costs nothing; asking again per path
    would be one command per candidate.

    A line without a tab is dropped rather than guessed at: a path containing a newline
    would arrive as one, and this scan reports what it can name for certain.
    """
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        kind, tab, path = line.partition("\t")
        if tab and path.startswith("/"):
            entries.append((kind, path))
    return entries


class ManualInstallsSyncJob(PackageSyncJob):
    """Detect, review and reproduce items no package manager can install on its own
    (D-15/D-18), on this job's own enable flag independent of `apt_sync`'s.

    Overrides `plan()` with an unreproducible-specific detect -> filter -> diff pipeline
    (the inherited apt-package-shaped `diff_items` does not apply); `converge()` replays a
    registered install snippet verbatim (D-20); the unreproducible finalize hook the base
    leaves as a no-op is implemented here.
    """

    name: ClassVar[str] = "manual_installs_sync"
    manager_id: ClassVar[str] = "manual"

    # No configurable properties: mirrors AptSyncJob's empty schema — only the enable flag
    # in sync_jobs is needed. D-32 forbids an empty placeholder config SECTION, so there is
    # no `manual_installs_sync:` block in default-config.yaml, but the in-code CONFIG_SCHEMA
    # ClassVar still declares the empty object every job carries.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        # Guards `_finalize_unreproducible` to run at most once per run. `after_review()`
        # calls it (so the pushed registry includes on-the-fly snippets), then the base
        # `apply()` calls it again; the second call is a no-op so a snippet's `authored_at`
        # is stamped exactly once and the source and pushed target registries stay identical.
        self._unreproducible_finalized = False

    # -- after-review snippet push (D-23) -----------------------------------------------

    @override
    async def after_review(self) -> None:
        """Push the install-snippet registry to the target after this job's review and
        before `apply()` replays any snippet (D-23), then promote each on-the-fly-authored
        item so it is APPLIED — not merely transported — the same run.

        Finalize-then-push-then-promote: `_finalize_unreproducible` persists this run's
        authored snippets into the SOURCE registry first (idempotent — `apply()` calls it
        again as a no-op), then `_push_snippet_registry` copies that file to the target,
        and finally `_promote_authored_snippets_to_install` reclassifies each authored
        item's diff `REPORT_ONLY -> INSTALL` decided `APPLY` so the unchanged base
        `apply()` converges it this run rather than the next one. Promotion runs AFTER the
        push so the target already holds the snippet the replay reads. The push depends on
        no other job: it moves the file itself and reads neither `config_sync` nor
        `folder_sync` state, so disabling either cannot break snippet delivery.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        await self._finalize_unreproducible(self._accepted_plan, self._accepted_outcome)
        await self._push_snippet_registry()
        self._promote_authored_snippets_to_install()

    def _promote_authored_snippets_to_install(self) -> None:
        """Reclassify every on-the-fly-authored item's diff `REPORT_ONLY -> INSTALL` and
        force its decision to `APPLY`, so the unchanged base `apply()` — which converges
        only APPLY-decided, non-`REPORT_ONLY` diffs (`sync_core.py` apply_diffs filter) —
        replays the freshly authored snippet THIS run, closing the one-run-too-late gap.

        Called from `after_review()` only after the authored snippets are persisted to the
        source registry and pushed to the target, so the target holds the snippet by the
        time the promoted diff converges. The add-snippet review path records no decision
        for the item (`packages.review._review_unreproducible_group`), so the decision must
        be forced here in addition to reclassifying the action.

        Mutates only in-memory accepted state (a `dataclasses.replace` of the frozen plan
        and outcome), so it is safe under dry-run: `apply()`'s dry-run branch previews a
        would-install line and issues no converge, preserving ADR-014.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        outcome = self._accepted_outcome
        if not outcome.snippets:
            return

        plan = self._accepted_plan
        authored = frozenset(outcome.snippets)
        new_diffs = tuple(
            replace(diff, action=DiffAction.INSTALL)
            if diff.item_id in authored and diff.action is DiffAction.REPORT_ONLY
            else diff
            for diff in plan.diffs
        )
        new_decisions = dict(outcome.decisions)
        for item_id in authored:
            new_decisions[item_id] = Decision.APPLY

        self._accepted_plan = replace(plan, diffs=new_diffs)
        self._accepted_outcome = replace(outcome, decisions=new_decisions)

    async def _push_snippet_registry(self) -> None:
        """Copy the source's `~/.config/pc-switcher/package-snippets.yaml` to the target's
        own copy under the SSH user's home, mirroring `config_sync._copy_config_to_target`'s
        `mkdir --parents` -> `echo $HOME` -> `send_file` shape.

        `send_file` writes plain SFTP as the SSH user, always under that user's home
        (`~/.config/pc-switcher`) — never `/etc` — which is exactly where the registry
        belongs, so no `sudo install` staging is needed. A no-op if the source has no
        registry file yet (a user who has never authored a snippet) and under dry-run
        (ADR-014: a rehearsal transfers nothing).

        The push is a WHOLE-FILE overwrite (no per-entry merge). Before it runs,
        `_guard_registry_overwrite` compares the target's current registry against the
        source's on-disk file (decision 9): a purely additive push (source superset of
        target) proceeds silently, while one that would lose or change a target entry
        requires explicit confirmation and otherwise aborts the run.
        """
        if self.context.dry_run:
            return

        source_path = Path.home() / SNIPPET_REGISTRY_RELPATH
        if not source_path.exists():
            return

        await self._guard_registry_overwrite(source_path)

        mkdir = await self.target.run_command(
            f"mkdir --parents {CONFIG_REMOTE_DIR}",
            mutates=f"create the pc-switcher config directory on {self.machines.target}",
        )
        if not mkdir.success:
            raise RuntimeError(f"Failed to create the config directory on {self.machines.target}: {mkdir.stderr}")

        # send_file needs an absolute remote path, so expand the target's ~ once.
        home = await self.target.run_command("echo $HOME")
        if not home.success:
            raise RuntimeError(f"Failed to read the home directory on {self.machines.target}")
        absolute_remote_path = f"{home.stdout.strip()}/{SNIPPET_REGISTRY_RELPATH}"
        await self.target.send_file(
            source_path, absolute_remote_path, mutates=f"push the install-snippet registry to {self.machines.target}"
        )

    async def _guard_registry_overwrite(self, source_path: Path) -> None:
        """Gate the wholesale `package-snippets.yaml` overwrite on the loss/change of any
        target entry (decision 9).

        The source's on-disk file (the exact bytes about to be sent) is compared against
        the target's current registry per `item_id`. The push is purely additive when the
        source is a superset of the target — every target entry is present in the source
        and IDENTICAL, whole entry against whole entry — in which case it proceeds silently,
        as before. Otherwise the target holds an entry that a wholesale overwrite would LOSE
        (absent from the source) or CHANGE; the user is shown exactly which entries and must
        confirm. Declining, or a non-interactive run that cannot confirm, aborts the whole
        sync (`SyncAborted` — the confirmer answers False for both, so this site cannot tell
        a decline from a refusal nobody was asked about) so the user can consolidate the two
        registries by hand and
        re-run — the tool never silently discards a snippet only the target has.

        The comparison is the whole `Snippet`, not its body alone: `PKG-FR-REGISTRY-CONSENT`
        gates a transfer that would "lose or change an entry the target holds", and the label
        and the authoring record (`authored_at`, `authored_on`) are part of that entry — a
        push that replaces them changes what the target holds even where the body it replays
        stays byte-identical.

        Either registry being unparsable aborts the same way (`state._unreadable_registry`):
        a file nobody can read says nothing about which entries exist, so the comparison this
        method rests on cannot be made at all. Both are read before either abort is raised, so
        a user whose two copies are both broken repairs them in one go rather than learning of
        the target's only once the source's is fixed.
        """
        unreadable: list[SyncAborted] = []
        source_snippets: dict[str, Snippet] = {}
        target_snippets: dict[str, Snippet] = {}
        try:
            source_snippets = load_snippets_from_text(
                source_path.read_text(encoding="utf-8"),
                display_path=str(source_path),
                machine=self.machines.source,
            )
        except SyncAborted as exc:
            unreadable.append(exc)
        try:
            target_snippets = await SnippetRegistry(self.target, self.machines.target).load()
        except SyncAborted as exc:
            unreadable.append(exc)
        if unreadable:
            raise SyncAborted("\n".join(str(exc) for exc in unreadable))

        lost = [snippet for item_id, snippet in target_snippets.items() if item_id not in source_snippets]
        changed = [
            (snippet, source_snippets[item_id])
            for item_id, snippet in target_snippets.items()
            if item_id in source_snippets and source_snippets[item_id] != snippet
        ]
        if not lost and not changed:
            return  # purely additive — source is a superset of the target

        assert self.context.confirmer is not None, (
            "a non-additive snippet registry overwrite needs a confirmer to gate it"
        )
        approved = await self.context.confirmer.confirm(
            title="Snippet registry overwrite is not purely additive",
            message=self._render_overwrite_diff(lost, changed),
            # No override flag exists: a lossy overwrite is only ever approved by a human
            # answering the prompt, so `allow=False` makes any non-interactive run refuse
            # (and thus abort) rather than silently overwrite.
            allow=False,
            allow_flag="manual registry consolidation",
            log_extra={"job": self.name, "host": "source"},
        )
        if not approved:
            raise SyncAborted(
                f"snippet registry overwrite not approved: {self.machines.target} holds snippet entries "
                f"absent from or differing in {self.machines.source}'s that a wholesale push would lose "
                "or change; consolidate the two registries by hand and re-run"
            )

    def _render_overwrite_diff(self, lost: list[Snippet], changed: list[tuple[Snippet, Snippet]]) -> str:
        """Rich-markup body naming every target entry a wholesale push would lose or change.

        Every snippet field is untrusted package-manager/user text, so each is
        `rich.markup.escape`d before it reaches the confirmer's `Panel` — a body or label
        containing `[...]` must not be parsed as console markup (T-02-02).

        A CHANGED entry shows only the FIELDS that differ, each as the target's value then
        the source's. Printing the body unconditionally read as a contradiction when the
        body was the one thing that had not changed — the same text twice under "to be
        replaced" and "incoming" — and the question is about what the overwrite changes.

        This question is the fifth credential exit (ADR-021): a snippet body is opaque to
        the tool and a `curl` of a private `.deb` is a documented shape, so the composed
        text is redacted here, where it becomes the question. Only the rendering is
        rewritten — the registry on disk and the body replayed on the target stay the bytes
        their author wrote (`PKG-FR-SNIPPET-VERBATIM`).
        """

        def body_lines(body: str, indent: str) -> list[str]:
            return [f"{indent}{escape(line)}" for line in (body.splitlines() or [""])]

        lines = [
            f"{self.machines.target}'s snippet registry holds entries this overwrite would discard or replace:",
            "",
        ]
        for snippet in lost:
            lines.append(f"  LOST     {escape(snippet.label)}  ({escape(snippet.item_id)})")
            lines.extend(body_lines(snippet.body, "             "))
        for target_snippet, source_snippet in changed:
            lines.append(f"  CHANGED  {escape(target_snippet.label)}  ({escape(target_snippet.item_id)})")
            for field, target_value, source_value in self._snippet_fields(target_snippet, source_snippet):
                if target_value == source_value:
                    continue
                lines.append(f"             {field} on {self.machines.target} (to be replaced):")
                lines.extend(body_lines(target_value, "               "))
                lines.append(f"             {field} from {self.machines.source} (incoming):")
                lines.extend(body_lines(source_value, "               "))
        lines += [
            "",
            f"Continuing overwrites {self.machines.target}'s registry wholesale. Decline to abort and "
            "consolidate the two registries by hand.",
        ]
        return redact_credentials("\n".join(lines))

    @staticmethod
    def _snippet_fields(target_snippet: Snippet, source_snippet: Snippet) -> list[tuple[str, str, str]]:
        """The comparable fields of two copies of one entry, as `(name, target, source)`.

        `item_id` is absent because the pair is matched on it. `authored_at` and `authored_on`
        are rendered as ONE `authored` field: they are two halves of a single authoring
        record, and naming them apart would put two lines in front of the user for one fact.
        """
        return [
            ("label", target_snippet.label, source_snippet.label),
            ("body", target_snippet.body, source_snippet.body),
            (
                "authored",
                f"{target_snippet.authored_at} on {target_snippet.authored_on}",
                f"{source_snippet.authored_at} on {source_snippet.authored_on}",
            ),
        ]

    # -- Detection (D-18/D-19), run on both machines (`PKG-FR-MANUAL-DIFF`) --------------

    async def _scan_no_candidate_apt_packages(self, installed_names: Sequence[str]) -> list[UnreproducibleItem]:
        """D-18: installed packages whose INSTALLED version comes from no repository the
        SOURCE has configured — put there by `dpkg --install` of a bare `.deb`.

        Over the whole INSTALLED set, not `apt-mark showmanual`. `PKG-FR-MANUAL-SCOPE` draws
        the boundary at "every installed version no configured repository supplies", and
        apt's manual/automatic mark is a different fact: a `.deb` installed to satisfy
        another one, or one the user ran `apt-mark auto` over, is outside the manual set and
        is still software no package manager can put on the other machine. Narrowing to the
        manual set left it invisible to this job and — being automatic — invisible to
        `apt_sync` as well, so nothing named it anywhere.

        One batched `apt-cache policy` over that set (never one call per package), read
        through `packages_installed_from_no_repository`: a package's own `Candidate:` line
        cannot answer this, because dpkg's status entry makes apt report a hand-installed
        package's installed version as its candidate. Measured on the development machine
        (Ubuntu 24.04, apt 2.8.3): 2282 installed against 153 manual, 3.1s against 0.4s and
        718KB of output against 96KB — one command either way, and the wider set is what the
        article asks for.

        Guarded on the exit code AND on the block count (ADR-022 D-04), which is the guard
        `apt_sync._source_policy` puts on its own copy of this command: same host, same
        probe, so the same strictness. Its silence indicts nothing on its own — an
        unanswered probe reports no unreproducible packages, which proposes nothing — but
        it does not stay harmless in a whole run: `apt_sync.capture_source_items` DROPS the
        same bare-`.deb` packages from its own manifest off its own copy of this probe, so
        one probe answering and the other not makes a package vanish from the run with
        nothing said about it anywhere. Every name here is installed on this machine, so apt
        owes a block for each and no block at all is apt not answering rather than a machine
        with unusual packages.
        """
        if not installed_names:
            return []

        quoted = " ".join(shlex.quote(name) for name in installed_names)
        command = f"apt-cache policy {quoted}"
        result = await self.source.run_command(command)
        # A key per block apt printed, whatever it said inside it — so this counts blocks and
        # not packages, and a machine whose whole manual set is bare `.deb`s still answers.
        require_answer(
            command,
            result,
            self.machines.source,
            answers=len(installed_origins_by_package(result.stdout)),
            answer_noun="package block",
        )
        no_repository = packages_installed_from_no_repository(result.stdout, installed_names)
        return [
            UnreproducibleItem(
                origin="apt-no-candidate",
                identifier=name,
                label=f"{name} (installed from no configured repository)",
            )
            for name in sorted(no_repository)
        ]

    async def _scan_unowned_installs(
        self, executor: Executor, machine: str, *, ask_when_ambiguous: bool
    ) -> list[UnreproducibleItem]:
        """Paths no dpkg package owns under this scan's roots — software an install script
        dropped there directly, bypassing apt entirely (`PKG-FR-MANUAL-SCOPE`).

        Four bounded steps, each one command, each skipped when the step before it left
        nothing to ask about:

        1. `_list_scan_entries` names every entry of every root, with its type.
        2. `dpkg --search` decides ownership over what is left once `_USR_LOCAL_SKELETON` is
           dropped; a path absent from the reply is unowned.
        3. `_resolve_opt_shapes` turns an unowned `/opt` entry into the finding its own shape
           makes it (`PKG-FR-MANUAL-OPT-SHAPE`), asking the user where the shape is genuinely
           ambiguous and `ask_when_ambiguous` says this machine's findings are the items.
        4. `_directories_holding_a_file` drops a directory with no file anywhere beneath it:
           an empty shape is not software, and there would be nothing for a snippet to
           reproduce.

        `dpkg --search` exits 1 as soon as ONE queried path is unowned, which is precisely
        the finding this scan is looking for, so its exit code says nothing about whether it
        answered (ADR-022, `PKG-FR-READ-FAILS-JOB`). `_DPKG_OWNERSHIP_WITNESS` supplies the
        answer the exit code cannot: a path dpkg must claim rides along in the same batch,
        and its absence from the reply means dpkg did not answer. Without it a dead
        `dpkg --search` prints nothing, every candidate looks unowned, and the user is asked
        to write an install snippet for every entry under `/opt` and `/usr/local`.

        Runs on whichever machine it is handed (`PKG-FR-MANUAL-DIFF`): the source's findings
        are the candidates, the target's are what the diff subtracts.
        """
        entries = await self._list_scan_entries(executor, machine)
        candidates = {path: kind for kind, path in entries if path not in _USR_LOCAL_SKELETON}
        if not candidates:
            return []

        quoted_paths = " ".join(shlex.quote(path) for path in [*sorted(candidates), _DPKG_OWNERSHIP_WITNESS])
        ownership_command = f"dpkg --search {quoted_paths}"
        ownership = await executor.run_command(ownership_command)
        owned = _owned_paths_from_dpkg_s(ownership.stdout)
        if _DPKG_OWNERSHIP_WITNESS not in owned:
            raise ProbeFailed(
                f"probe on {machine} did not answer — `{ownership_command}` reported no owner for "
                f"{_DPKG_OWNERSHIP_WITNESS}, which dpkg owns on every machine, so its silence about the other "
                f"paths is not an answer about them: {ownership.stderr.strip()}"
            )
        unowned = {path: kind for path, kind in candidates.items() if path not in owned}

        shaped = await self._resolve_opt_shapes(executor, machine, unowned, ask_when_ambiguous=ask_when_ambiguous)
        directories = sorted(path for path, kind in shaped.items() if kind == _DIRECTORY)
        holds_a_file = await self._directories_holding_a_file(executor, machine, directories)
        findings = [path for path, kind in shaped.items() if kind != _DIRECTORY or path in holds_a_file]

        return [UnreproducibleItem(origin="unowned-path", identifier=path, label=path) for path in sorted(findings)]

    async def _list_scan_entries(self, executor: Executor, machine: str) -> list[tuple[str, str]]:
        """Every entry of every scan root, one level deep, as `(type letter, path)`.

        One `find` per root, driven from a shell loop that SKIPS a root that is not there, so
        the one tolerated error is gone from the exit code rather than hidden behind a
        `2>/dev/null`. What is left — an unreadable root, a missing binary — exits non-zero
        and reaches `require_answer`. Empty output on a clean exit stays an ordinary answer:
        a machine with nothing under `/opt` is an ordinary machine. Silence, on the other
        hand, is not "nothing is installed by hand here"; it would drop every finding this
        job exists to make (`PKG-FR-READ-FAILS-JOB`).
        """
        quoted_roots = " ".join(shlex.quote(root) for root in _UNOWNED_SCAN_ROOTS)
        # One line, never a multi-line script: the command is echoed verbatim into the debug
        # trace and the `--confirm-each-command` gate.
        command = (
            f'for root in {quoted_roots}; do [ -d "$root" ] || continue; '
            "find \"$root\" -mindepth 1 -maxdepth 1 -printf '%y\\t%p\\n' || exit 1; done"
        )
        listing = await executor.run_command(command)
        require_answer(command, listing, machine)
        return _typed_entries(listing.stdout)

    async def _resolve_opt_shapes(
        self, executor: Executor, machine: str, unowned: Mapping[str, str], *, ask_when_ambiguous: bool
    ) -> dict[str, str]:
        """Replace each unowned `/opt/<name>` directory with the finding its shape makes it
        (`PKG-FR-MANUAL-OPT-SHAPE`), leaving every other candidate untouched.

        `/opt/<application>` and `/opt/<publisher>/<application>` look the same from outside,
        so what the directory holds decides: a file of its own makes it the application; no
        file and exactly one directory makes that directory the application; no file and
        several directories cannot be told apart and is the one question this job asks
        outside the review.

        `ask_when_ambiguous` is what separates the two machines. On the machine whose
        findings become items, the user answers. On the other, the shape decides nothing —
        the reading is only used to subtract what is already there — so BOTH readings are
        kept as held and nothing is asked: whichever one the answer produced on the other
        machine is then subtracted, and asking twice for one fact that changes no item is
        exactly the noise the diff exists to remove.

        One `find` for all the `/opt` directories together, listing each one level deep.

        Known cost: the question comes before the answers that could make it pointless. The
        finding's identity IS the answer, so neither the source's own machine-specific marks
        nor what the target already holds can be applied to a shape nobody has read yet — and
        an ambiguous directory whose every reading is already settled is therefore asked
        about once per run. Resolving it would mean reading the target before the source's
        shape step, which costs that read on every machine that has no findings at all.
        """
        opt_directories = sorted(
            path
            for path, kind in unowned.items()
            if kind == _DIRECTORY and path.startswith(f"{_OPT_ROOT}/") and "/" not in path[len(_OPT_ROOT) + 1 :]
        )
        if not opt_directories:
            return dict(unowned)

        quoted = " ".join(shlex.quote(path) for path in opt_directories)
        command = f"find {quoted} -mindepth 1 -maxdepth 1 -printf '%y\\t%p\\n'"
        listing = await executor.run_command(command)
        # No `answers=` guard: every one of these directories may legitimately be empty, and
        # an empty answer is ordinary data (ADR-022).
        require_answer(command, listing, machine)

        children: dict[str, list[tuple[str, str]]] = {path: [] for path in opt_directories}
        for kind, path in _typed_entries(listing.stdout):
            parent = path.rpartition("/")[0]
            if parent in children:
                children[parent].append((kind, path))

        shaped = {path: kind for path, kind in unowned.items() if path not in children}
        for path, entries in children.items():
            subdirectories = sorted(child for kind, child in entries if kind == _DIRECTORY)
            if len(subdirectories) != len(entries):
                shaped[path] = _DIRECTORY  # holds a file of its own: one application
            elif not subdirectories:
                continue  # holds nothing at all: not a finding
            elif len(subdirectories) == 1:
                shaped[subdirectories[0]] = _DIRECTORY
            elif not ask_when_ambiguous:
                shaped[path] = _DIRECTORY
                shaped.update(dict.fromkeys(subdirectories, _DIRECTORY))
            elif await self._ask_whether_one_application(path, subdirectories):
                shaped[path] = _DIRECTORY
            else:
                shaped.update(dict.fromkeys(subdirectories, _DIRECTORY))
        return shaped

    async def _ask_whether_one_application(self, path: str, subdirectories: Sequence[str]) -> bool:
        """Ask whether `path` is one application or a publisher's directory holding several
        (`PKG-FR-MANUAL-OPT-SHAPE`). True keeps `path` as the finding; False makes each of
        `subdirectories` a finding of its own.

        Asked while the run is still planning, because the answer decides what the review
        lists rather than what happens to any item on it — and asked in a rehearsal too, since
        a dry run puts the same questions as a real one (`PKG-FR-DRY-RUN`).

        Both answers name the machine the software would land on and say what would be
        reproduced there rather than how this job would go about it
        (`PKG-FR-NAME-THE-MACHINES`, `PKG-FR-EFFECT-NOT-MECHANISM`). With nobody to ask, the
        directory itself is the finding: it is the shallower of the two readings, and this
        scan reports what it finds where it finds it.
        """
        assert self.context.reviewer is not None, (
            f"{self.manager_id} sync has no reviewer; the orchestrator must inject one "
            "through JobContext.reviewer before plan()."
        )
        source, target = self.machines.source, self.machines.target
        named = ", ".join(subdirectories)
        answer = await self.context.reviewer.ask_gate(
            title=f"What is {path} on {source}?",
            message=(
                f"{path} on {source} holds no file of its own — only the directories {named}. "
                f"Either it is one application whose parts live in those directories, or it is one "
                f"publisher's directory holding an application per directory. Nothing on {source} says "
                f"which, and the answer decides what {target} is offered."
            ),
            proceed_label=f"One application — {path} is what would be reproduced on {target}",
            stop_label=f"One per directory — {named} are what would be reproduced on {target}, each on its own",
        )
        return answer is None or answer

    async def _directories_holding_a_file(
        self, executor: Executor, machine: str, directories: Sequence[str]
    ) -> frozenset[str]:
        """Of `directories`, those holding a file somewhere beneath them — the ones that are
        software rather than an empty shape (`PKG-FR-MANUAL-SCOPE`).

        One `find` per directory, stopping at the first non-directory it meets (`-quit`), so
        the cost is bounded however large the tree is and no tree is ever walked whole.

        A directory whose own `find` FAILED is kept rather than dropped: an unreadable
        subtree says nothing about whether a file is down there, and the harmless reading is
        the one that still puts the finding in front of the user. Dropping it would be
        silence read as data — the failure this scan's other guards exist to prevent.
        """
        if not directories:
            return frozenset()

        quoted = " ".join(shlex.quote(path) for path in directories)
        # `exit 0` at the end, because the loop's own last status says nothing: every
        # directory's outcome is on stdout, and a shell that could not run at all still
        # fails the command itself, which is what `require_answer` reads.
        command = (
            f'for dir in {quoted}; do if found=$(find "$dir" ! -type d -print -quit); '
            'then [ -n "$found" ] && echo "$dir"; else echo "$dir"; fi; done; exit 0'
        )
        result = await executor.run_command(command)
        require_answer(command, result, machine)
        return frozenset(_lines(result.stdout))

    # -- plan() / converge() ------------------------------------------------------------

    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The source's unreproducible findings: apt-no-candidate packages plus unowned
        installs under this scan's roots (D-18). One `dpkg-query` names the source's
        installed set here and its result feeds the no-candidate scan.

        This job overrides `plan()` and never routes through `PackageSyncJob.diff_items`'s
        apt-package-shaped dispatch, so widening this hook's item type is safe (same
        reasoning as `SnapSyncJob.capture_source_items`).
        """
        return [
            *await self._scan_no_candidate_apt_packages(
                await self._installed_names(self.source, self.machines.source)
            ),
            *await self._scan_unowned_installs(self.source, self.machines.source, ask_when_ambiguous=True),
        ]

    async def _installed_names(self, executor: Executor, machine: str) -> list[str]:
        """Every package name dpkg reports as INSTALLED on `machine` — the population
        `PKG-FR-MANUAL-SCOPE` draws the no-candidate scan from on the source, and the whole
        of what the target holds on the apt side (`PKG-FR-MANUAL-DIFF`).

        `${Package}`, not `${binary:Package}`: the arch-qualified form only appears for a
        foreign architecture, and `apt-cache policy` speaks the plain name. Two dpkg entries
        for one name (multi-arch) therefore collapse to one, which is what the batched policy
        call wants anyway.

        Guarded on the exit code AND on emptiness (ADR-022): a machine with no installed
        packages does not exist, so nothing here is a legitimate empty answer, and silence
        read as data would report "nothing on this machine was hand-installed" — the one
        answer this job exists to be able to contradict.
        """
        command = "dpkg-query --show --showformat='${Package}\\t${db:Status-Status}\\n'"
        result = await executor.run_command(command)
        fields = (line.partition("\t") for line in _lines(result.stdout))
        installed = sorted({name for name, _, status in fields if status == "installed"})
        require_answer(command, result, machine, answers=len(installed), answer_noun="installed package")
        return installed

    async def query_target_items(self) -> Sequence[UnreproducibleItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """What the TARGET already holds, in the source's own identities, so `plan()` can
        drop a finding that is already there (`PKG-FR-MANUAL-DIFF`).

        Not a manifest and never a source of items: nothing here is ever presented, converged
        or removed (`PKG-NG-MANUAL-REMOVE`). It exists so that a snippet which has already
        run stops being asked about — one snippet installs one application and leaves several
        traces, and each trace is a finding of its own.

        The two halves are not symmetric, deliberately. A path is held when the same scan
        finds it there unowned. A PACKAGE is held when dpkg reports the name installed at
        all, whatever origin put it there: software that is on the machine is on the machine,
        and running the source's whole `apt-cache policy` origin analysis here would cost a
        second 3-second, 718KB read to answer a question its own installed set already
        answers.
        """
        installed = await self._installed_names(self.target, self.machines.target)
        return [
            *(UnreproducibleItem(origin="apt-no-candidate", identifier=name, label=name) for name in installed),
            *await self._scan_unowned_installs(self.target, self.machines.target, ask_when_ambiguous=False),
        ]

    @override
    async def plan(self) -> PackagePlan:
        """Detect on both machines -> filter inert -> drop what the target holds -> diff
        against the SOURCE's snippet registry. Read-only.

        An item already recorded machine-specific on the SOURCE is dropped by
        `filter_inert` before it becomes a diff (D-08/D-19: a finding produces noise
        exactly once, then never again). The marks that matter are the source's alone: a
        finding is something the source has and the target lacks, so the source is always
        its holding machine (`PKG-FR-MACHINE-SPECIFIC`).

        What the target already holds is then subtracted (`PKG-FR-MANUAL-DIFF`), which is
        what stops a second path to one application — the symlink that starts what the
        snippet unpacked — from being asked about on every later run. The target is not
        queried at all when nothing survives the source's own filtering: there would be
        nothing to subtract from, and a machine with no findings should cost no reads on the
        other one.

        Reproducibility is judged from the SOURCE — the machine being replicated
        (`PKG-FR-MANUAL-SOURCE-DECIDES`): an item with a source-side registry snippet plans
        `INSTALL` (a snippet makes it reproducible), one without plans `REPORT_ONLY` and
        surfaces in its own review group for resolution. `after_review()`'s `send_file()`
        push places that snippet on the target before `converge()` reads it, so convergence
        still replays from the target's copy — only the classification authority is the
        source's.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        items = await filter_inert(await self.capture_source_items(), source_decisions)
        if items:
            held = {item.item_id for item in await self.query_target_items()}
            items = [item for item in items if item.item_id not in held]

        registry = SnippetRegistry(self.source, self.machines.source)
        diffs: list[ItemDiff] = []
        for item in items:
            snippet = await registry.get(item.item_id)
            action = DiffAction.INSTALL if snippet is not None else DiffAction.REPORT_ONLY
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.UNREPRODUCIBLE,
                    diff_class=DiffClass.UNREPRODUCIBLE,
                    action=action,
                    item_id=item.item_id,
                    label=item.label,
                    detail=None,
                )
            )
        all_diffs = tuple(diffs)
        groups = self._build_review_groups(all_diffs)
        return PackagePlan(manager=self.manager_id, diffs=all_diffs, groups=groups)

    @override
    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """Carve still-unresolved `UNREPRODUCIBLE` diffs (`action=REPORT_ONLY`, D-21) into
        their own `UNREPRODUCIBLE_REVIEW_ACTION` group, presented after any resolved
        (snippet-backed, `action=INSTALL`) install group so the user sees resolved items
        before being asked to resolve the rest. A snippet-backed diff is NOT pulled out —
        it flows through the base grouping like any other install-direction item.
        """
        needs_resolution = [
            diff
            for diff in diffs
            if diff.item_class == ItemClass.UNREPRODUCIBLE and diff.action == DiffAction.REPORT_ONLY
        ]
        if not needs_resolution:
            return super()._build_review_groups(diffs)

        carved_ids = {diff.item_id for diff in needs_resolution}
        rest = [diff for diff in diffs if diff.item_id not in carved_ids]
        groups = list(super()._build_review_groups(rest))
        groups.append(
            ReviewGroup(
                manager=self.manager_id,
                action=UNREPRODUCIBLE_REVIEW_ACTION,
                title=(
                    f"{self.machines.source} has these and no package manager can install them on "
                    f"{self.machines.target} ({self.manager_id})"
                ),
                entries=tuple(
                    ReviewEntry(item_id=diff.item_id, label=diff.label, action_label="resolve", detail=diff.detail)
                    for diff in needs_resolution
                ),
            )
        )
        return tuple(groups)

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Replay this item's registered snippet against the target, verbatim (D-20).
        `SnippetRegistry.replay` never raises for "no snippet registered" — it returns a
        failed `CommandResult` instead, so a plan/apply-time race (the registry changed
        underneath this run) is a per-item failure like any other (D-27), not a crash. The
        only action reaching `converge()` is `INSTALL`: `plan()` sets that only when a
        snippet exists, and a `REPORT_ONLY` diff never reaches this hook (`apply()`'s
        filter).
        """
        return await SnippetRegistry(self.target, self.machines.target).replay(diff.item_id, self.target)

    # -- Unreproducible finalize hook (moved off the base, D-18) -------------------------

    @override
    async def _finalize_unreproducible(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Persist this run's snippet authoring and unreproducible-item skip-always
        decisions (D-20/D-21/D-23). Overrides the base no-op hook (D-18: only this job
        produces unreproducible items).

        Snippets are written to `self.source` — never `self.target` — because the source
        registry is this job's own source of truth; `after_review()` then pushes that file
        to the target (D-23) so a snippet authored during THIS run's review reaches the
        target THIS run, before `apply()` replays it. Skip-always decisions are also
        recorded on `self.source`: unreproducible items are always source-held (they
        describe what is installed on the machine currently acting as source), so there is
        no target-held case to route to `self.target`.

        Idempotent per run: `after_review()` calls this before its push and the base
        `apply()` calls it again; a `self._unreproducible_finalized` guard makes the second
        call a no-op so each snippet's `authored_at` is stamped once and the source and
        pushed target registries stay byte-identical.

        Never during dry-run (ADR-014) and never for a non-interactive outcome (D-26):
        nothing is recorded permanently when nothing was actually decided by a human.
        """
        if self._unreproducible_finalized:
            return
        self._unreproducible_finalized = True

        if self.context.dry_run or not outcome.was_interactive:
            return

        by_id = {diff.item_id: diff for diff in plan.diffs}

        if outcome.snippets:
            registry = SnippetRegistry(self.source, self.machines.source)
            authored_at = datetime.now(UTC).isoformat()
            for item_id, body in outcome.snippets.items():
                diff = by_id.get(item_id)
                label = diff.label if diff is not None else item_id
                await registry.add(
                    Snippet(
                        item_id=item_id,
                        label=label,
                        body=body,
                        authored_at=authored_at,
                        authored_on=self.context.source_hostname,
                    )
                )

        recorded_at = datetime.now(UTC).isoformat()
        for diff in plan.diffs:
            if diff.item_class != ItemClass.UNREPRODUCIBLE:
                continue
            if outcome.decisions.get(diff.item_id) != Decision.SKIP_ALWAYS:
                continue
            await DecisionFile(self.manager_id, self.source).record(
                DecisionEntry(
                    item_id=diff.item_id,
                    item_class=diff.item_class,
                    label=diff.label,
                    reason=None,
                    recorded_at=recorded_at,
                )
            )

    # No `_unresolved_as_failures` override (decision 10): an interactive review can no
    # longer leave an unreproducible item genuinely undecided. Every entry ends resolved
    # (snippet, skip-once or skip-always), an empty snippet re-prompts rather than falling
    # through, and Ctrl-C/EOF aborts the whole sync (`SyncAbortedByUser`) instead of
    # manufacturing an unresolved SKIP_ONCE — so `outcome.unresolved` is empty after any
    # interactive run. The base no-op hook (`[]`) is therefore correct here: nothing an
    # interactive review produces needs failing on this basis. A non-interactive run still
    # populates `outcome.unresolved` for reporting (D-26), but that path was always exempt
    # from failing the job, so removing the override changes no behavior.

    @override
    async def validate(self) -> list[ValidationError]:
        """The commands this job's own detection runs: `apt-cache` and `dpkg` on the source,
        and `dpkg` on the target, which is read too now that a finding the target already
        holds is not presented (`PKG-FR-MANUAL-DIFF`). Both machines are only ever read for
        detection, so no sudo is needed for it. A snippet's own sudo needs are unpredictable
        (an opaque blob, D-20), so this job does NOT pre-validate target sudo; a snippet that
        needs it and lacks it fails as a per-item converge failure (D-27), reported like any
        other.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `AptSyncJob.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        apt_cache_check = await self.source.run_command("apt-cache --version")
        if not apt_cache_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE, "apt-cache is not available on source (required to detect unreproducible packages)"
                )
            )

        dpkg_check = await self.source.run_command("dpkg --version")
        if not dpkg_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE, "dpkg is not available on source (required to detect unowned installs)"
                )
            )

        target_dpkg_check = await self.target.run_command("dpkg --version")
        if not target_dpkg_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET, "dpkg is not available on target (required to tell what it already has)"
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): replaying install
        snippets for unreproducible items."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["unreproducible/manual installs (via recorded install snippets)"],
            mechanism="replay install snippet per item, after review",
        )
