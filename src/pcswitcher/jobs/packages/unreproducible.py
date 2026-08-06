"""What every job for software no package manager can reproduce shares (D-18, D-20, D-21,
D-22, D-23): the item shape, the detect -> filter -> diff pipeline, the snippet replay, and
the whole install-snippet registry push/consent path.

Here rather than in one job's module because the registry is ONE shared file
(`SNIPPET_REGISTRY_RELPATH`) serving every such job, and because the pipeline around it is
identical whatever the detector found: a job supplies `capture_source_items()` and
`query_target_items()` and inherits everything else. `manual_deb_sync` (hand-installed
`.deb` packages), `manual_snap_sync` (sideloaded snaps), `manual_flatpak_sync` (refs no
remote can supply) and `manual_installs_sync` (unowned software under `/usr/local` and
`/opt`) are the ones today.

Not on `PackageSyncJob` itself: the three package-manager jobs (apt, snap, flatpak) produce
no unreproducible item at all, and a base holding this would make them inherit a registry,
a consent question and a finalize step none of them can reach. The no-op hooks that keep
`sync_core.apply()` generic (`_finalize_unreproducible`, `after_review`) stay there; their
real implementations are here.

An unreproducible item ends an interactive run resolved in one of three ways (D-21,
decision 10): it has an install snippet in the shared, synced registry (`SnippetRegistry`,
D-20/D-23), it is recorded machine-specific (skip-always) in its job's machine-local
decision file, or the user skipped it once — skip-once is a real decision. There is no
fourth "genuinely undecided" outcome an interactive review can reach: an empty snippet
capture re-prompts rather than falling through, and Ctrl-C/EOF aborts the whole sync
(`SyncAbortedByUser`) instead of leaving an item unresolved.

What the diff compares is the installed VERSION, not presence (D-05, D-22). Presence alone
made an item that is on both machines invisible for good: an application upgraded in place
on the source went on being "already there" on the target at whatever build it happened to
hold. So an item both machines have is compared on the string each machine's own version
source printed — the manager's own version for a package, a snap or a ref, and the entry's
`version_body` for an unowned path — and only a difference produces anything. That ordering
is deliberate: a snippet edited to change a comment or a mirror URL moves no version and so
raises no item at all.

The guarantee this buys is exactly apt's, snap's and flatpak's, and no more: equal version
means converged, and the content behind it is never verified. A half-applied or corrupted
tree whose version string did not move is invisible here, by design — there is no recursive
folder diff and no payload hashing anywhere.

Three directions now, where there was one:

- source only -> INSTALL (with a snippet) or an item to resolve (without one);
- both machines, versions differ -> CHANGE, converged by replaying the SOURCE's
  install-or-update body onto the target. Version numbers never decide direction: a sync
  goes source to target whichever version is higher, exactly as it does for apt;
- target only, and the target's own detector calls it a finding -> REMOVE.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal, override

from rich.markup import escape

from pcswitcher.config_sync import CONFIG_REMOTE_DIR
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
    build_version_mismatch_detail,
)
from pcswitcher.jobs.packages.review import (
    UNREPRODUCIBLE_RETRY_REVIEW_ACTION,
    UNREPRODUCIBLE_REVIEW_ACTION,
    UNREPRODUCIBLE_UPDATE_REVIEW_ACTION,
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
    SnippetBodies,
    SnippetRegistry,
    filter_inert,
    load_snippets_from_text,
    marks_on_either,
)
from pcswitcher.jobs.packages.sync_core import ConvergeItemDeclined, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, Host, LogLevel, SyncAborted
from pcswitcher.redaction import redact_credentials

__all__ = ["UnreproducibleItem", "UnreproducibleSyncJob", "lines_of"]

#: The origins an `UnreproducibleItem` can be found under. One per detector, and part of
#: identity rather than a field alongside it — see `UnreproducibleItem`.
UnreproducibleOrigin = Literal["apt-no-candidate", "flatpak-no-remote", "unowned-path", "snap-sideload"]


def lines_of(output: str) -> list[str]:
    """Non-blank, stripped lines — the shape every list command these jobs run produces."""
    return [line.strip() for line in output.splitlines() if line.strip()]


@dataclass(frozen=True)
class UnreproducibleItem:
    """One item no package manager can reproduce (D-18): an apt package installed from no
    configured repository, a sideloaded snap, a flatpak ref installed from no configured
    remote, or an unowned install under `/usr/local`/`/opt`.

    `origin` distinguishes how the item was found — `apt-no-candidate` (an installed
    package no repository can supply), `snap-sideload` (a snap whose bytes came from a
    local file), `flatpak-no-remote` (an installed ref whose origin names no remote
    configured in its scope) or `unowned-path` (a filesystem path dpkg does not claim) —
    and lives inside `item_id` for the same reason `scope`
    lives inside the two flatpak identities: the same `identifier` value can appear
    under both origins with no relation to each other (e.g. a package name that is
    also, coincidentally, a path component), so origin has to be part of identity, not
    just a field alongside it.

    That is also what lets one origin's items move to a job of their own without breaking
    anything already recorded: an `item_id` names the finding, never the job that found it,
    so the shared snippet registry keeps resolving across the split.

    Unlike the other item types, `label` here is a plain FIELD rather than a `label()`
    method: the human-readable description comes from whichever detector found the
    item (D-19's unowned-install scan, or the no-candidate check) and is not something
    this dataclass can derive from `origin`/`identifier` alone.

    `own_finding` separates the two questions a target listing answers, which used to need
    two listings. On the SOURCE every item is a finding by construction and the field is
    never read. On the TARGET both readings matter and they are not the same set: whether
    the target HOLDS the item at all decides that the source's copy is not offered for
    install (`PKG-FR-MANUAL-DIFF` — a package the target has from a repository is software
    that is there, whatever route put it there), while whether the target's own detector
    calls it a finding decides that an item the source no longer has may be offered for
    REMOVAL (`PKG-FR-MANUAL-REMOVE`). Defaulted True so every source-side construction site
    stays as it was; a job flags it on the target rows its own detector did not claim.

    The installed VERSION is deliberately not a field. It is read per machine through
    `UnreproducibleSyncJob.installed_versions`, which the converge loop asks again after
    every replay — a value captured once with the item would be a fact from before the
    change and would report every convergence as successful.
    """

    origin: UnreproducibleOrigin
    identifier: str
    label: str
    own_finding: bool = True

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.UNREPRODUCIBLE

    @staticmethod
    def id_prefix(origin: UnreproducibleOrigin) -> str:
        """What every `item_id` of one origin starts with, so a caller matching on origin
        (a job picking its own items out of a decision file that also holds another job's)
        builds the prefix from the same expression `item_id` does.
        """
        return f"unreproducible:{origin}:"

    @property
    def item_id(self) -> str:
        """Stable identity string: `unreproducible:<origin>:<identifier>`."""
        return f"{self.id_prefix(self.origin)}{self.identifier}"


class UnreproducibleSyncJob(PackageSyncJob):
    """The shared half of every job that detects software no package manager can reproduce
    and resolves it through the install-snippet registry (D-18/D-20/D-21/D-23).

    A subclass supplies its own detection — `capture_source_items()` and
    `query_target_items()` — plus the usual `name`, `manager_id`, `validate()` and
    `describe_first_sync_scope()`. Everything from the diff onwards is here: `plan()`,
    `converge()`, the review grouping, the registry push and its consent question, and the
    finalize step that persists this run's authoring and skip-always answers.

    Carries no `name` ClassVar, for the reason `PackageSyncJob` documents: job discovery
    scans a job module's attributes for a `SyncJob` subclass whose `name` matches the
    module name, and an abstract base without one is invisible to it even when a concrete
    subclass imports it into scope.
    """

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        # Guards `_finalize_unreproducible` to run at most once per run. `after_review()`
        # calls it (so the pushed registry includes on-the-fly snippets), then the base
        # `apply()` calls it again; the second call is a no-op so a snippet's `authored_at`
        # is stamped exactly once and the source and pushed target registries stay identical.
        self._unreproducible_finalized = False
        # What `plan()` read, for the two things `converge()` cannot re-derive from an
        # `ItemDiff`: the version the target has to reach, and the target-side item a
        # removal command is built from. Re-assigned on every `plan()`, never cached across
        # calls.
        self._source_versions: dict[str, str | None] = {}
        self._removable: dict[str, UnreproducibleItem] = {}
        # What the SOURCE's registry holds for the items on the update screen, so an answer
        # that rewrites a snippet opens its editors on that content rather than on nothing.
        # Collected in `plan()`, where the registry is already being read per item.
        self._recorded_bodies: dict[str, SnippetBodies] = {}

    # -- Detection, supplied per job ----------------------------------------------------

    @abstractmethod
    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:
        """This job's findings on the SOURCE — the candidates `plan()` works from."""
        ...

    @abstractmethod
    async def query_target_items(self) -> Sequence[UnreproducibleItem]:
        """What the TARGET holds, in the source's own identities (`PKG-FR-MANUAL-DIFF`).

        Two answers in one listing, told apart by `UnreproducibleItem.own_finding`: every
        row is something the target HAS, so the source's copy of it is never offered for
        install, and a row this job's own detector claims on the target is additionally a
        removal candidate once the source no longer has it (`PKG-FR-MANUAL-REMOVE`).

        A job whose two readings need different reads does both here rather than in two
        hooks: the expensive one is the detector's, and narrowing it to the names the source
        does not have is what keeps it affordable.
        """
        ...

    @abstractmethod
    async def installed_versions(self, item_ids: Collection[str], *, on_source: bool) -> Mapping[str, str | None]:
        """The version each of `item_ids` is installed at on ONE machine, read fresh
        (D-22, `PKG-FR-MANUAL-VERSION`).

        The whole of what a diff compares, and the one thing the converge loop re-reads to
        decide whether a replay actually converged — which is why it is a hook of its own
        rather than a field captured with the item.

        `None` for an id this machine cannot answer for, and every reason collapses into
        that one value: the item is not installed here, the manager printed no version, the
        entry has no `version_body`, or that body failed. A comparison needs two answers, so
        a `None` on either side produces no item rather than a claimed difference.

        Batched: an implementation issues one command over the whole set wherever its
        version source is a listing, and one per id only where each item carries its own
        `version_body`.
        """
        ...

    @abstractmethod
    def removal_command(self, item: UnreproducibleItem) -> str:
        """The command that takes `item` off the TARGET (`PKG-FR-MANUAL-REMOVE`).

        One command per ecosystem — `apt-get remove`, `snap remove`, `flatpak uninstall`,
        `rm --recursive --force` — and no uninstall snippet and no uninstall machinery
        behind any of them: this is not a package manager, and a second authored body for
        the one direction the user can always carry out by hand would cost more than it is
        worth.
        """
        ...

    def removal_warning(self) -> str | None:
        """What the removal screen says above the rows, where the removal reaches further
        than the item it names (`PKG-FR-MANUAL-REMOVE`).

        `None` on the base: a package manager's own removal is bounded by what that manager
        recorded. `manual_installs_sync` is the exception — `rm --recursive --force` takes
        the scanned path and nothing else, while the snippet that created it will usually
        also have dropped a `.desktop` file or a symlink somewhere the scan never looks.
        """
        return None

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

        Two enabled unreproducible jobs each push the same shared file. The second push is
        additive by construction — it sends a superset of what its own predecessor sent —
        so `_guard_registry_overwrite` passes it silently and no question is put twice.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        await self._finalize_unreproducible(self._accepted_plan, self._accepted_outcome)
        await self._push_snippet_registry()
        self._promote_authored_snippets_to_install()

    def _promote_authored_snippets_to_install(self) -> None:
        """Reclassify every on-the-fly-authored item's diff `REPORT_ONLY -> INSTALL`/`CHANGE`
        and force its decision to `APPLY`, so the unchanged base `apply()` — which converges
        only APPLY-decided, non-`REPORT_ONLY` diffs (`sync_core.py` apply_diffs filter) —
        replays the freshly authored snippet THIS run, closing the one-run-too-late gap.

        Which of the two it becomes is the diff's own class: an item the target lacks was
        demoted from `INSTALL` and a version difference from `CHANGE`, and both converge the
        same way, so the promotion only has to put back the verb the run reports.

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
            replace(
                diff,
                action=DiffAction.CHANGE if diff.diff_class is DiffClass.VERSION_MISMATCH else DiffAction.INSTALL,
            )
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
            lines.extend(body_lines(snippet.install_body, "             "))
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
            ("install snippet", target_snippet.install_body, source_snippet.install_body),
            ("installed-version snippet", target_snippet.version_body, source_snippet.version_body),
            (
                "authored",
                f"{target_snippet.authored_at} on {target_snippet.authored_on}",
                f"{source_snippet.authored_at} on {source_snippet.authored_on}",
            ),
        ]

    # -- plan() / converge() ------------------------------------------------------------

    @override
    async def plan(self) -> PackagePlan:
        """Detect on both machines -> filter inert -> diff on presence and then on version
        -> classify against the SOURCE's snippet registry. Read-only.

        Both machines' marks filter both inventories (`marks_on_either`), not each machine's
        own: with a removal direction in the model, a mark on the source filtered out of the
        source's findings alone would leave the target's copy unmatched and offer to delete
        the very software the mark was given to keep. That is the failure `state.marks_on_
        either` documents, reached here for the first time.

        Three directions come out of the pair of inventories:

        - the target does not have it at all -> INSTALL, or, with no snippet on the source,
          `REPORT_ONLY` in the group that asks the user to resolve it;
        - both machines have it -> the two installed versions are read and compared, and
          only a difference produces a `CHANGE`. Equal versions, and a version either
          machine could not answer for, produce nothing: the first is convergence and the
          second is not evidence of anything (`PKG-FR-MANUAL-VERSION`). A difference with no
          snippet on the source is `REPORT_ONLY` like any other unresolved item — there is
          nothing to replay;
        - only the target has it, and the target's OWN detector claims it -> REMOVE
          (`PKG-FR-MANUAL-REMOVE`). A target row this job's detector does not claim is
          software some manager can account for and is not this job's to delete.

        The target is queried whatever the source found, unlike before: a removal is exactly
        the case where the source has nothing to contribute, so skipping the read when the
        source's own findings are empty would make the whole direction unreachable.

        Reproducibility is judged from the SOURCE — the machine being replicated
        (`PKG-FR-MANUAL-SOURCE-DECIDES`): an item with a source-side registry snippet plans
        `INSTALL`/`CHANGE`, one without plans `REPORT_ONLY` and surfaces in its own review
        group for resolution. `after_review()`'s `send_file()` push places that snippet on
        the target before `converge()` reads it, so convergence still replays from the
        target's copy — only the classification authority is the source's.
        """
        source_decisions, target_decisions = await self._load_live_decisions()
        marks = marks_on_either(source_decisions, target_decisions)
        source_items = {item.item_id: item for item in await filter_inert(await self.capture_source_items(), marks)}
        target_items = {item.item_id: item for item in await filter_inert(await self.query_target_items(), marks)}

        registry = SnippetRegistry(self.source, self.machines.source)
        both = [item_id for item_id in source_items if item_id in target_items]
        source_versions = await self.installed_versions(both, on_source=True) if both else {}
        target_versions = await self.installed_versions(both, on_source=False) if both else {}
        self._source_versions = dict(source_versions)

        diffs: list[ItemDiff] = []
        self._recorded_bodies = {}
        for item_id, item in source_items.items():
            snippet = await registry.get(item_id)
            if snippet is not None:
                self._recorded_bodies[item_id] = SnippetBodies(
                    install_body=snippet.install_body, version_body=snippet.version_body
                )
            if item_id not in target_items:
                diffs.append(self._diff_for(item, DiffAction.INSTALL, snippet is not None, detail=None))
                continue
            source_version, target_version = source_versions.get(item_id), target_versions.get(item_id)
            if source_version is None or target_version is None or source_version == target_version:
                continue
            diffs.append(
                self._diff_for(
                    item,
                    DiffAction.CHANGE,
                    snippet is not None,
                    detail=build_version_mismatch_detail(source_version, target_version, self.machines),
                )
            )
        self._removable = {
            item_id: item for item_id, item in target_items.items() if item_id not in source_items and item.own_finding
        }
        diffs.extend(self._diff_for(item, DiffAction.REMOVE, True, detail=None) for item in self._removable.values())

        all_diffs = self._drop_inert_diffs(tuple(diffs), source_decisions, target_decisions)
        groups = self._build_review_groups(all_diffs)
        return PackagePlan(manager=self.manager_id, diffs=all_diffs, groups=groups)

    def _diff_for(
        self, item: UnreproducibleItem, action: DiffAction, reproducible: bool, *, detail: str | None
    ) -> ItemDiff:
        """One item's diff, demoted to `REPORT_ONLY` where the source holds no snippet.

        The demotion is what routes an item into the group that asks the user to resolve it,
        and it applies to a version difference exactly as it applies to a missing install:
        both converge by replaying the source's body, so neither is actionable without one.
        A removal needs no snippet, so it is never demoted.
        """
        return ItemDiff(
            item_class=ItemClass.UNREPRODUCIBLE,
            diff_class=DiffClass.VERSION_MISMATCH if action is DiffAction.CHANGE else DiffClass.UNREPRODUCIBLE,
            action=action if reproducible else DiffAction.REPORT_ONLY,
            item_id=item.item_id,
            label=item.label,
            detail=detail,
        )

    @override
    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """Carve the two groups this job asks differently from every other, and let the base
        build the rest.

        - still-unresolved diffs (`action=REPORT_ONLY`, D-21) go to
          `UNREPRODUCIBLE_REVIEW_ACTION`, whose act opens an editor. Presented after any
          resolved install group, so the user sees what is already answerable before being
          asked to answer the rest. Each entry's `action_label` says which of the two cases
          it is — an item the target lacks is installed, one whose version differs is
          updated — because the screen is titled with that verb and "install" would be a
          false statement about software that is already there.
        - snippet-backed version differences (`action=CHANGE`) go to
          `UNREPRODUCIBLE_UPDATE_REVIEW_ACTION`, which offers a third answer no ordinary
          decision row has: replace the recorded body before replaying it.

        A snippet-backed INSTALL and a REMOVE both flow through the base grouping like any
        other item of their direction; the removal group picks up this job's own warning as
        its note, where it has one (`removal_warning`).
        """
        needs_resolution = [
            diff
            for diff in diffs
            if diff.item_class == ItemClass.UNREPRODUCIBLE and diff.action == DiffAction.REPORT_ONLY
        ]
        updates = [
            diff for diff in diffs if diff.item_class == ItemClass.UNREPRODUCIBLE and diff.action == DiffAction.CHANGE
        ]
        carved_ids = {diff.item_id for diff in (*needs_resolution, *updates)}
        rest = [diff for diff in diffs if diff.item_id not in carved_ids]
        groups = [self._with_removal_warning(group) for group in super()._build_review_groups(rest)]

        if updates:
            groups.append(
                ReviewGroup(
                    manager=self.manager_id,
                    action=UNREPRODUCIBLE_UPDATE_REVIEW_ACTION,
                    title=(
                        f"{self.machines.source} and {self.machines.target} have these at different versions "
                        f"({self.manager_id})"
                    ),
                    entries=tuple(
                        ReviewEntry(item_id=diff.item_id, label=diff.label, action_label="update", detail=diff.detail)
                        for diff in updates
                    ),
                    recorded_bodies={
                        diff.item_id: self._recorded_bodies[diff.item_id]
                        for diff in updates
                        if diff.item_id in self._recorded_bodies
                    },
                )
            )
        if needs_resolution:
            groups.append(
                ReviewGroup(
                    manager=self.manager_id,
                    action=UNREPRODUCIBLE_REVIEW_ACTION,
                    title=(
                        f"{self.machines.source} has these and no package manager can reproduce them on "
                        f"{self.machines.target} ({self.manager_id})"
                    ),
                    entries=tuple(
                        ReviewEntry(
                            item_id=diff.item_id,
                            label=diff.label,
                            action_label="update" if diff.diff_class is DiffClass.VERSION_MISMATCH else "install",
                            detail=diff.detail,
                        )
                        for diff in needs_resolution
                    ),
                )
            )
        return tuple(groups)

    def _with_removal_warning(self, group: ReviewGroup) -> ReviewGroup:
        """This job's removal warning attached to its removal group, if it has one."""
        warning = self.removal_warning()
        if warning is None or group.action != DiffAction.REMOVE.value:
            return group
        return replace(group, note=warning)

    # -- converge -----------------------------------------------------------------------

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Take a removal off the target, or replay the source's install-or-update body
        until the target reports the source's version (D-20, D-22).

        A `REMOVE` is one command and one outcome. An `INSTALL` and a `CHANGE` are the same
        work — replay the source's body — and both go through `_converge_by_snippet`, whose
        loop is what makes a replay's success mean convergence rather than "the command
        exited 0". A body that exits 0 and installs nothing is the ordinary failure mode of
        an installer that no-ops over an existing tree, and it is invisible to an exit code.

        A `REPORT_ONLY` diff never reaches this hook (`apply()`'s filter).
        """
        if diff.action is DiffAction.REMOVE:
            return await self._converge_removal(diff)
        return await self._converge_by_snippet(diff)

    async def _converge_removal(self, diff: ItemDiff) -> CommandResult:
        """Run this job's own removal command for one approved REMOVE.

        No privilege is pre-validated for it, for the reason a snippet's is not (D-20,
        D-27): this job's validation has never required sudo on the target, an install-only
        run still does not need it, and demanding it up front would fail validation for
        every user who never approves a removal. A removal that lacks the privilege fails as
        its own item, named, like any other.
        """
        item = self._removable.get(diff.item_id)
        if item is None:
            return CommandResult(exit_code=1, stdout="", stderr=f"no target finding recorded for {diff.item_id!r}")
        return await self.target.run_command(
            self.removal_command(item), mutates=f"remove {item.label} from {self.machines.target}"
        )

    async def _converge_by_snippet(self, diff: ItemDiff) -> CommandResult:
        """Replay, then check, then ask — until the target reports the source's version or
        the user stops (D-22, `PKG-FR-MANUAL-CONVERGE-LOOP`, `PKG-FR-ASK-AGAIN`).

        Convergence always means replaying the SOURCE's body onto the target, whichever
        machine holds the higher version: a sync goes one way, and reading the numbers to
        pick a direction would make the tool decide something the user did not ask it to.

        The loop terminates on exactly two outcomes — the target reaching the source's
        version, or the user skipping the item for this run. `apply existing snippet` is
        offered once and never again inside one item's loop: after a replay that changed no
        version, running the same bytes a second time is the same no-op, so the menu narrows
        to writing a new body or stopping. There is deliberately no purge-and-retry answer;
        an author whose installer no-ops over an existing tree writes the `rm -rf … &&`
        into their own new body, which subsumes it and keeps pc-switcher out of deciding
        what to delete.

        With nobody to ask, one attempt is made with the recorded body and the item is then
        skipped with a warning: the only remaining answers need a person to write a shell
        script, and failing the item would report a broken sync where the truth is an
        unanswered question.

        A version neither machine can be asked for after the replay is not read as failure:
        the replay's own exit code is all the run has, and an item whose `version_body`
        stopped answering is reported converged on that evidence rather than looped on
        forever.
        """
        expected = self._source_versions.get(diff.item_id)
        while True:
            result = await SnippetRegistry(self.target, self.machines.target).replay(diff.item_id, self.target)
            if not result.success or expected is None:
                return result
            landed = (await self.installed_versions([diff.item_id], on_source=False)).get(diff.item_id)
            if landed is None or landed == expected:
                return result

            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"{diff.label}: {self.machines.target} still reports {landed}, not {expected}",
            )
            # Past this point the recorded body has run and left the version where it was,
            # so every further pass replays a body the user wrote just now.
            if not await self._author_replacement(diff, landed=landed, expected=expected):
                raise ConvergeItemDeclined(
                    f"{self.machines.target} still has {landed} rather than {expected}, and the recorded "
                    "install-or-update snippet did not change that"
                )

    async def _author_replacement(self, diff: ItemDiff, *, landed: str, expected: str) -> bool:
        """Put the narrowed menu — write a new snippet, or skip for this run — and record
        what comes back. True where a replacement was written and the loop should replay it.

        A one-entry review round rather than a bespoke prompt: it goes through the same
        injected `Reviewer` as everything else, so the automation environment, the
        no-terminal path and the Ctrl-C rule all behave here exactly as they do in the
        review proper. `PKG-FR-ASK-AGAIN` is what licenses the round — the fact it rests on,
        that this body does not move this machine's version, is one only the run's own
        earlier change could establish.

        The new bodies are written to BOTH registries directly, rather than to the source's
        and then pushed. The push is a whole-file overwrite gated on being additive
        (`_guard_registry_overwrite`), and a source entry changed after this run's own push
        is exactly what that gate calls a lost entry — so pushing again would put a
        consent question in front of the user about the change they had just made. Writing
        the identical entry on both machines leaves the two copies equal, which is what the
        gate is there to protect.
        """
        if self.context.reviewer is None:
            return False
        registry = SnippetRegistry(self.source, self.machines.source)
        recorded = await registry.get(diff.item_id)
        if recorded is None:
            return False

        bodies_now = SnippetBodies(install_body=recorded.install_body, version_body=recorded.version_body)
        outcome = await self.context.reviewer.review(
            (
                ReviewGroup(
                    manager=self.manager_id,
                    action=UNREPRODUCIBLE_RETRY_REVIEW_ACTION,
                    title=f"{diff.label} on {self.machines.target} is still {landed}, not {expected}",
                    entries=(
                        ReviewEntry(
                            item_id=diff.item_id,
                            label=diff.label,
                            action_label="update",
                            detail=build_version_mismatch_detail(expected, landed, self.machines),
                        ),
                    ),
                    recorded_bodies={diff.item_id: bodies_now},
                ),
            )
        )
        bodies = outcome.snippets.get(diff.item_id)
        if bodies is None:
            return False

        snippet = Snippet(
            item_id=diff.item_id,
            label=recorded.label,
            install_body=bodies.install_body,
            version_body=bodies.version_body,
            authored_at=datetime.now(UTC).isoformat(),
            authored_on=self.context.source_hostname,
        )
        await registry.add(snippet)
        await SnippetRegistry(self.target, self.machines.target).add(snippet)
        return True

    # -- Unreproducible finalize hook (moved off the base, D-18) -------------------------

    @override
    async def _finalize_unreproducible(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Persist this run's snippet authoring and unreproducible-item skip-always
        decisions (D-20/D-21/D-23). Overrides the base no-op hook (D-18: only an
        unreproducible job produces such items).

        Snippets are written to `self.source` — never `self.target` — because the source
        registry is this job's own source of truth; `after_review()` then pushes that file
        to the target (D-23) so a snippet authored during THIS run's review reaches the
        target THIS run, before `apply()` replays it.

        The skip-always half covers the resolve group alone (`REPORT_ONLY`), whose items are
        by definition on the source and not usably on the target, so the source is their
        holding machine. Every other direction this job now produces is recorded by the base
        `_record_permanent_skips`, which routes a removal's mark to the target
        (`_mark_recipients`) — the machine whose copy that answer keeps. Recording those
        here as well would put a second, source-side entry on software the source does not
        have.

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
            for item_id, bodies in outcome.snippets.items():
                diff = by_id.get(item_id)
                label = diff.label if diff is not None else item_id
                await registry.add(
                    Snippet(
                        item_id=item_id,
                        label=label,
                        install_body=bodies.install_body,
                        version_body=bodies.version_body,
                        authored_at=authored_at,
                        authored_on=self.context.source_hostname,
                    )
                )

        recorded_at = datetime.now(UTC).isoformat()
        for diff in plan.diffs:
            if diff.item_class != ItemClass.UNREPRODUCIBLE or diff.action is not DiffAction.REPORT_ONLY:
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
