"""What every job for software no package manager can reproduce shares (D-18, D-20, D-21,
D-23): the item shape, the detect -> filter -> diff pipeline, the snippet replay, and the
whole install-snippet registry push/consent path.

Here rather than in one job's module because the registry is ONE shared file
(`SNIPPET_REGISTRY_RELPATH`) serving every such job, and because the pipeline around it is
identical whatever the detector found: a job supplies `capture_source_items()` and
`query_target_items()` and inherits everything else. `manual_deb_sync` (hand-installed
`.deb` packages), `manual_snap_sync` (sideloaded snaps) and `manual_installs_sync`
(unowned software under `/usr/local` and `/opt`) are the ones today.

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
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence
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
)
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
    Snippet,
    SnippetRegistry,
    filter_inert,
    load_snippets_from_text,
)
from pcswitcher.jobs.packages.sync_core import PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, SyncAborted
from pcswitcher.redaction import redact_credentials

__all__ = ["UnreproducibleItem", "UnreproducibleSyncJob", "lines_of"]

#: The origins an `UnreproducibleItem` can be found under. One per detector, and part of
#: identity rather than a field alongside it — see `UnreproducibleItem`.
UnreproducibleOrigin = Literal["apt-no-candidate", "unowned-path", "snap-sideload"]


def lines_of(output: str) -> list[str]:
    """Non-blank, stripped lines — the shape every list command these jobs run produces."""
    return [line.strip() for line in output.splitlines() if line.strip()]


@dataclass(frozen=True)
class UnreproducibleItem:
    """One item no package manager can reproduce (D-18): an apt package installed from no
    configured repository, a sideloaded snap, or an unowned install under
    `/usr/local`/`/opt`.

    `origin` distinguishes how the item was found — `apt-no-candidate` (an installed
    package no repository can supply), `snap-sideload` (a snap whose bytes came from a
    local file) or `unowned-path` (a filesystem path dpkg does not claim) — and lives
    inside `item_id` for the same reason `scope`
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
    """

    origin: UnreproducibleOrigin
    identifier: str
    label: str

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

    # -- Detection, supplied per job ----------------------------------------------------

    @abstractmethod
    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:
        """This job's findings on the SOURCE — the candidates `plan()` works from."""
        ...

    @abstractmethod
    async def query_target_items(self) -> Sequence[UnreproducibleItem]:
        """What the TARGET already holds, in the source's own identities, so `plan()` can
        drop a finding that is already there (`PKG-FR-MANUAL-DIFF`).

        Never a manifest and never a source of items: nothing it returns is presented,
        converged or removed (`PKG-NG-MANUAL-REMOVE`).
        """
        ...

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

    # -- plan() / converge() ------------------------------------------------------------

    @override
    async def plan(self) -> PackagePlan:
        """Detect on both machines -> filter inert -> drop what the target holds -> diff
        against the SOURCE's snippet registry. Read-only.

        An item already recorded machine-specific on the SOURCE is dropped by
        `filter_inert` before it becomes a diff (D-08/D-19: a finding produces noise
        exactly once, then never again) — unless the source no longer has it, in which case
        `_load_live_decisions_on` has already left that mark out and the item is a finding
        again like any other. The marks that matter are the source's alone: a finding is
        something the source has and the target lacks, so the source is always its holding
        machine (`PKG-FR-MACHINE-SPECIFIC`).

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
        source_decisions = await self._load_live_decisions_on(on_source=True)
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
        decisions (D-20/D-21/D-23). Overrides the base no-op hook (D-18: only an
        unreproducible job produces such items).

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
            await self._decision_file(self.source).record(
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
