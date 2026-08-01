"""`manual_installs_sync`: the fourth package job, owning everything no package manager
can reproduce (D-15, D-18, D-19, D-20, D-21).

Two detectors, both run on the SOURCE:

- apt packages installed on the source whose INSTALLED version comes from no repository the
  source has configured — installed via `dpkg --install` of a bare `.deb`, so only dpkg's
  own status file accounts for them. Every installed package, not the `apt-mark showmanual`
  set: apt's manual/automatic mark says how the package got there, not whether any
  repository can supply it.
- paths directly under `/usr/local` and `/opt` (plus the immediate children of
  `/usr/local/bin` and `/usr/local/lib`) that no dpkg package owns — software an install
  script dropped there, bypassing apt entirely. Never one of those four scan roots itself:
  they are directories the distribution ships, not software under themselves.

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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Literal, override

from rich.markup import escape

from pcswitcher.config_sync import CONFIG_REMOTE_DIR
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
    SyncAbortedByUser,
    ValidationError,
)
from pcswitcher.redaction import redact_credentials

__all__ = ["ManualInstallsSyncJob"]

# D-19's bounded unowned-install scan: top-level entries of `/usr/local` and `/opt`, plus
# the immediate children of `/usr/local/bin` and `/usr/local/lib` — one shell loop runs
# `find <root> -mindepth 1 -maxdepth 1` over each of the four, skipping any that is not
# there so a missing root is not an error to tell apart from a broken one
# (`_scan_unowned_installs`). Enough to NAME a finding (D-18), never
# enough to walk an entire tree — the item is decided on, not replicated (deferred ideas,
# CONTEXT.md). Owned by this job now, no longer shared with apt_sync (D-18).
#
# A root is never a finding of its own scan (`PKG-FR-MANUAL-SCOPE`): two of these four are
# also ENTRIES of a third (`/usr/local/bin` and `/usr/local/lib` sit under `/usr/local`),
# so `find` names them like any other candidate — see `_scan_unowned_installs`.
_UNOWNED_SCAN_ROOTS = ("/usr/local", "/opt", "/usr/local/bin", "/usr/local/lib")

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
        sync (`SyncAbortedByUser`) so the user can consolidate the two registries by hand and
        re-run — the tool never silently discards a snippet only the target has.

        The comparison is the whole `Snippet`, not its body alone: `PKG-FR-REGISTRY-CONSENT`
        gates a transfer that would "lose or change an entry the target holds", and the label
        and the authoring record (`authored_at`, `authored_on`) are part of that entry — a
        push that replaces them changes what the target holds even where the body it replays
        stays byte-identical.

        Either registry being unparsable aborts the same way, inside the two reads below
        (`state._unreadable_registry`): a file nobody can read says nothing about which
        entries exist, so the comparison this method rests on cannot be made at all.
        """
        source_snippets = load_snippets_from_text(
            source_path.read_text(encoding="utf-8"),
            display_path=str(source_path),
            machine=self.machines.source,
        )
        target_snippets = await SnippetRegistry(self.target, self.machines.target).load()

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
            raise SyncAbortedByUser(
                f"snippet registry overwrite declined: {self.machines.target} holds snippet entries "
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

    # -- Detection (D-18/D-19), all on the source ---------------------------------------

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

    async def _scan_unowned_installs(self) -> list[UnreproducibleItem]:
        """D-18/D-19: paths under `/usr/local` and `/opt` that no dpkg package owns —
        software an install script dropped there directly, bypassing apt entirely.

        One batched `find` over `_UNOWNED_SCAN_ROOTS` names every candidate path, then one
        batched `dpkg --search` over those candidates decides ownership; a path absent from the
        `dpkg --search` output is unowned. Both steps run on the SOURCE (D-18) — a fact about
        what the source machine has installed, not the target.

        Both are guarded, and neither by its bare exit code (ADR-022, `PKG-FR-READ-FAILS-JOB`):

        - `find` is driven from a shell loop that SKIPS a scan root that is not there, so
          the one tolerated error is gone from the exit code rather than hidden behind the
          `2>/dev/null` this used to carry. What is left — an unreadable root, a missing
          binary — exits non-zero and reaches `require_answer`. Empty output on a clean exit
          stays an ordinary answer: a machine with nothing under `/opt` is an ordinary
          machine. Silence, on the other hand, is not "nothing is installed by hand here":
          it would drop every finding this job exists to make.
        - `dpkg --search` exits 1 as soon as ONE queried path is unowned, which is precisely
          the finding this scan is looking for, so its exit code says nothing about whether
          it answered. `_DPKG_OWNERSHIP_WITNESS` supplies the answer the exit code cannot: a
          path dpkg must claim rides along in the same batch, and its absence from the reply
          means dpkg did not answer. Without it a dead `dpkg --search` prints nothing, every
          candidate looks unowned, and the user is asked to write an install snippet for
          every entry under `/opt` and `/usr/local`.

        A scan ROOT is dropped from the candidates before ownership is even asked. The scan
        covers software UNDER its roots (`PKG-FR-MANUAL-SCOPE`), and `/usr/local/bin` and
        `/usr/local/lib` are roots that `find` also names as entries of `/usr/local`.
        Ownership cannot settle it either: dpkg owns no `/usr/local` directory on a stock
        machine (`base-files` ships none), so both would be reported on every machine, in
        every run, forever — asking every user for an install snippet for a directory the
        distribution ships and whose interesting contents are already scanned one level
        deeper. Exact string equality suffices: `find` echoes the root verbatim as the
        prefix of each entry it prints, and no root carries a trailing slash.
        """
        quoted_roots = " ".join(shlex.quote(root) for root in _UNOWNED_SCAN_ROOTS)
        # One line, never a multi-line script: the command is echoed verbatim into the debug
        # trace and the `--confirm-each-command` gate.
        listing_command = (
            f'for root in {quoted_roots}; do [ -d "$root" ] || continue; '
            'find "$root" -mindepth 1 -maxdepth 1 || exit 1; done'
        )
        listing = await self.source.run_command(listing_command)
        require_answer(listing_command, listing, self.machines.source)
        candidates = [path for path in _lines(listing.stdout) if path not in _UNOWNED_SCAN_ROOTS]
        if not candidates:
            return []

        quoted_paths = " ".join(shlex.quote(path) for path in [*candidates, _DPKG_OWNERSHIP_WITNESS])
        ownership_command = f"dpkg --search {quoted_paths}"
        ownership = await self.source.run_command(ownership_command)
        owned = _owned_paths_from_dpkg_s(ownership.stdout)
        if _DPKG_OWNERSHIP_WITNESS not in owned:
            raise ProbeFailed(
                f"probe on {self.machines.source} did not answer — `{ownership_command}` reported no owner for "
                f"{_DPKG_OWNERSHIP_WITNESS}, which dpkg owns on every machine, so its silence about the other "
                f"paths is not an answer about them: {ownership.stderr.strip()}"
            )

        return [
            UnreproducibleItem(origin="unowned-path", identifier=path, label=path)
            for path in sorted(set(candidates) - owned)
        ]

    # -- plan() / converge() ------------------------------------------------------------

    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The source's unreproducible items: apt-no-candidate packages plus unowned
        `/usr/local`/`/opt` installs (D-18). One `dpkg-query` names the source's installed
        set here and its result feeds the no-candidate scan.

        This job overrides `plan()` and never routes through `PackageSyncJob.diff_items`'s
        apt-package-shaped dispatch, so widening this hook's item type is safe (same
        reasoning as `SnapSyncJob.capture_source_items`).
        """
        return [
            *await self._scan_no_candidate_apt_packages(await self._source_installed_names()),
            *await self._scan_unowned_installs(),
        ]

    async def _source_installed_names(self) -> list[str]:
        """Every package name dpkg reports as INSTALLED on the source — the population
        `PKG-FR-MANUAL-SCOPE` draws the no-candidate scan from.

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
        result = await self.source.run_command(command)
        fields = (line.partition("\t") for line in _lines(result.stdout))
        installed = sorted({name for name, _, status in fields if status == "installed"})
        require_answer(command, result, self.machines.source, answers=len(installed), answer_noun="installed package")
        return installed

    async def query_target_items(self) -> Sequence[UnreproducibleItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """No target-side manifest exists for unreproducible items: they are always
        source-held (they describe what the SOURCE machine has installed), and convergence
        is driven by the shared snippet registry, not by a diff against target state. The
        empty result keeps the abstract hook satisfied without a meaningless target query.
        """
        return []

    @override
    async def plan(self) -> PackagePlan:
        """Detect -> filter inert -> diff against the SOURCE's snippet registry. Read-only.

        An item already recorded machine-specific on the SOURCE is dropped by
        `filter_inert` before it becomes a diff (D-08/D-19: a finding produces noise
        exactly once, then never again). Unreproducible items are always source-held, so
        only the source's decision file is consulted.

        Reproducibility is judged from the SOURCE — the machine being replicated (corrected
        D-23): an item with a source-side registry snippet plans `INSTALL` (a snippet makes
        it reproducible), one without plans `REPORT_ONLY` and surfaces in its own review
        group for resolution. `after_review()`'s `send_file()` push places that snippet on
        the target before `converge()` reads it, so convergence still replays from the
        target's copy — only the classification authority moved to the source.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        items = await filter_inert(await self.capture_source_items(), source_decisions)

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
        """`apt-cache` and `dpkg` availability on the SOURCE — the commands this job's own
        detection runs (D-18). The source is only ever read, so no sudo is needed for
        detection. A snippet's own sudo needs are unpredictable (an opaque blob, D-20), so
        this job does NOT pre-validate target sudo; a snippet that needs it and lacks it
        fails as a per-item converge failure (D-27), reported like any other.

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
