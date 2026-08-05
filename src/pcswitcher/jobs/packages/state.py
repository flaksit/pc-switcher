"""Machine-local decision store: the ONLY pc-switcher state deliberately per-machine
and never synced (D-08, D-08a, D-09).

An entry recorded here means "inert on THIS machine in both roles": not pushed when
this machine is the source, not installed or removed when this machine is the target.
D-19's whole argument for scanning aggressively — a finding produces noise exactly
once, then never again — only holds if this durability is real.

Durable for as long as the item is, and no longer: an entry keeps THIS machine's copy of
something, so once this machine no longer has that something the entry has nothing left to
keep, and `DecisionFile.drop` takes it out (`PackageSyncJob.observe_absent_marks` decides
which entries those are). Leaving it in place is not the conservative option — an entry
suppresses its item in both roles, so a dead one silently blocks a later install of that
same software here, which is not what the answer that wrote it chose.

Which machine's file gets an entry follows which machine HOLDS the item (D-08a): an
install declined for good is recorded on the source, the only machine that has it; a
removal and an overwrite are both recorded on the target, which is the machine whose
copy the answer keeps. An overwrite is therefore the one direction whose mark can be
sitting on either machine when a later run reads it, since the run that recorded it may
have been launched the other way round — `PackageSyncJob._mark_holders` is where that
asymmetry is stated once, for the write and the read together. Because the file is
machine-local, the write must land on the correct END of the connection — on the
target this means going through the remote executor, never a local `pathlib` write
(ADR-002: the target has no direct filesystem access from here). `DecisionFile` takes
an `Executor` at construction and issues every read/write as a shell command through
it, so the SAME code path serves both roles; there is no separate "local write"
branch to accidentally use for the target.

Decision files live at `~/.config/pc-switcher/<manager>.decisions.yaml`, next to
`config.yaml` (D-09) — one file per manager, so `apt_sync`'s decisions never collide
with `snap_sync`'s. The directory portion is derived from `config_sync.CONFIG_REMOTE_DIR`
rather than a second hardcoded literal (the CR-01 precedent `folder_sync` already
follows for its own tool-state filter token). One manager may ADOPT a slice of another's
file (`AdoptedMarks`), which is how a job carved out of an older one keeps the answers the
user already gave about the items it took with it.

This module also owns `SnippetRegistry` (D-20, D-23): the SHARED, synced counterpart to
the machine-local decision store above. Where a `DecisionEntry` says "never touch this
item on this machine", a `Snippet` says "this is how to install something no package
manager can reproduce" — knowledge about the PACKAGE, not the machine, so it is SHARED
and synced (D-23) rather than living in a machine-local `*.decisions.yaml` file. It
travels source-to-target by an unreproducible job's own post-review `send_file` push,
not via `config_sync`. A snippet's body is stored and replayed as an opaque text
blob — never parsed, versioned, diffed or reasoned about (D-20) — and replay never
supplies stdin, since `pcswitcher.executor.Process` documents that commands must be
non-interactive; a snippet expecting a prompt fails rather than hanging the sync.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import yaml

from pcswitcher.config_sync import CONFIG_REMOTE_DIR
from pcswitcher.jobs.packages.items import ItemClass
from pcswitcher.models import CommandResult, SyncAborted

if TYPE_CHECKING:
    from pcswitcher.executor import Executor, RemoteExecutor

__all__ = [
    "DECISION_FILE_GLOB_RELPATH",
    "DECISION_FILE_RELPATH_TEMPLATE",
    "SNIPPET_REGISTRY_RELPATH",
    "AdoptedMarks",
    "DecisionEntry",
    "DecisionFile",
    "Snippet",
    "SnippetRegistry",
    "filter_inert",
    "load_snippets_from_text",
    "marks_on_either",
]

_logger = logging.getLogger("pcswitcher.jobs.packages.state")

# Home-relative directory holding every manager's decision file, derived from
# CONFIG_REMOTE_DIR ("~/.config/pc-switcher") rather than a second hardcoded literal.
_DECISION_DIR_RELPATH = CONFIG_REMOTE_DIR.removeprefix("~/")

# `{manager}.decisions.yaml`, home-relative — one file per manager (D-09).
DECISION_FILE_RELPATH_TEMPLATE = f"{_DECISION_DIR_RELPATH}/{{manager}}.decisions.yaml"

# The single glob `folder_sync` consumes for its non-overridable exclusion, so the
# exclusion and the store this module owns can never drift apart.
DECISION_FILE_GLOB_RELPATH = f"{_DECISION_DIR_RELPATH}/*.decisions.yaml"

# The shared install-snippet registry, home-relative, alongside every manager's
# decision file — but unlike those, this ONE file is not per-manager and is meant to be
# synced (D-23): an unreproducible job pushes it to the target with its own `send_file`
# call after its review, so a snippet authored on the fly reaches the target that same run.
SNIPPET_REGISTRY_RELPATH = f"{_DECISION_DIR_RELPATH}/package-snippets.yaml"

_FILE_HEADER = (
    "# pc-switcher machine-specific decision file — regenerated on every write.\n"
    "#\n"
    '# Every entry below came from an explicit "skip always" choice in a sync\n'
    "# review (D-08). An item listed here is inert on THIS machine in both roles:\n"
    "# never pushed to a peer when this machine is the source, never installed or\n"
    "# removed here when this machine is the target.\n"
    "#\n"
    "# This file is machine-local and is never synced to any peer. Remove\n"
    "# an entry (or delete the whole file) to make that item eligible again on the\n"
    "# next sync.\n"
    "#\n"
    "# An entry is dropped automatically once this machine no longer has the item it\n"
    "# names: the mark keeps THIS machine's copy, so it has nothing left to keep. If\n"
    "# the item comes back, it is reviewed again as a new item.\n"
)


@dataclass(frozen=True)
class DecisionEntry:
    """One permanent "skip always" decision (D-07's third outcome), persisted by
    `DecisionFile.record` and read back by `DecisionFile.load`.
    """

    item_id: str
    item_class: ItemClass
    label: str
    reason: str | None
    recorded_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class AdoptedMarks:
    """The marks one manager takes over from ANOTHER manager's decision file, for a job
    carved out of an older, wider one.

    A decision file is per manager (D-09) while an `item_id` names the finding and not the
    job that found it, so splitting a job moves the items without moving the entries the
    user already recorded about them. Left alone those entries are orphaned, and D-08's
    promise — a finding produces noise exactly once, then never again — silently breaks for
    exactly the items the user cared enough about to answer permanently.

    `manager` is the file to look in and `item_id_prefix` is the slice of it that belongs
    to the adopting job; the two jobs partition one legacy file by prefix, so neither ever
    reads the other's entries. Adoption is permanent rather than a one-shot copy: reading
    is the only thing a plan may do (`PackageSyncJob.plan` is read-only), and a migration
    that writes could never run before the first plan that needs its result.
    """

    manager: str
    item_id_prefix: str


class _HasItemId(Protocol):
    @property
    def item_id(self) -> str: ...


async def filter_inert[T: _HasItemId](items: Sequence[T], decisions: Mapping[str, DecisionEntry]) -> list[T]:
    """Items whose `item_id` is ABSENT from `decisions` — the ones still live.

    A pure, module-level function (not a method) so both the source-capture side
    (drop recorded items from the manifest before it is even diffed) and the
    target-query side (drop recorded items from what would otherwise become a
    proposed install/remove) share exactly one definition of "inert" (D-08).

    A job that captures an inventory from BOTH machines passes `marks_on_either` here
    rather than each machine's own file; see that function for why.
    """
    return [item for item in items if item.item_id not in decisions]


def marks_on_either(
    source_decisions: Mapping[str, DecisionEntry], target_decisions: Mapping[str, DecisionEntry]
) -> dict[str, DecisionEntry]:
    """Both machines' marks as one mapping, for the `filter_inert` pass over an inventory
    each machine has its own copy of.

    Filtering a machine's inventory by its own file alone is wrong wherever the OTHER
    machine can have the same item: the marked copy disappears from one side and the
    unmarked copy survives on the other, so an item that should have produced NO diff
    becomes a one-sided one pointing the wrong way — an install of software the target
    already has, or a removal of software the source still has. Both are exactly what
    `PKG-FR-MACHINE-SPECIFIC` forbids ("MUST NOT be proposed in any later review", "MUST
    NOT be removed or overwritten by a sync from any other machine"), and the removal
    direction destroys the copy the mark was given to protect. A snap whose revision
    differs is the case that makes it unavoidable: the mark is recorded once, and the very
    next run in the same direction offered the snap for removal.

    It is also what a decision file already claims to mean — "inert on THIS machine in
    both roles" — read from the other end of the connection: an entry on the target says
    the item is not to be installed or removed there, whichever machine the run was
    launched from.

    Right-biased so a `label` or `reason` the target recorded wins on a shared id; only
    membership is ever read here, so the choice is cosmetic.

    The unreproducible jobs are the ones that do not need this: each captures an
    inventory from the source alone, so there is no second copy to leave behind, and
    `PKG-FR-MANUAL-SOURCE-DECIDES` makes the source the only authority anyway.
    """
    return {**source_decisions, **target_decisions}


def _serialize(entries: Mapping[str, DecisionEntry]) -> str:
    """Render `entries` as the decision-file YAML body, header regenerated fresh."""
    machine_specific = {
        item_id: {
            "item_class": entry.item_class.value,
            "label": entry.label,
            "reason": entry.reason,
            "recorded_at": entry.recorded_at,
        }
        for item_id, entry in entries.items()
    }
    body = yaml.safe_dump({"machine_specific": machine_specific}, sort_keys=False, default_flow_style=False)
    return f"{_FILE_HEADER}\n{body}"


def _deserialize(raw: str) -> dict[str, DecisionEntry]:
    """Parse a decision file's content into `{item_id: DecisionEntry}`.

    Raises on anything that isn't the expected shape; callers translate that into
    the "no permanent decisions" empty-mapping fallback (see `DecisionFile.load`).
    """
    data = yaml.safe_load(raw)
    machine_specific = data.get("machine_specific") if isinstance(data, dict) else None
    if not isinstance(machine_specific, dict):
        raise ValueError("decision file has no 'machine_specific' mapping")

    entries: dict[str, DecisionEntry] = {}
    for item_id, fields in machine_specific.items():
        entries[str(item_id)] = DecisionEntry(
            item_id=str(item_id),
            item_class=ItemClass(fields["item_class"]),
            label=fields["label"],
            reason=fields.get("reason"),
            recorded_at=fields["recorded_at"],
        )
    return entries


class DecisionFile:
    """Read/write access to ONE manager's machine-local decision file, through
    whichever `Executor` is local to the machine that should hold it (D-08a).

    Construct with `executor=self.source` to read/write the SOURCE machine's file,
    or `executor=self.target` to read/write the TARGET machine's file — the caller
    (`PackageSyncJob`, plan 02-04 task 2) decides which per D-08a, this class only
    ever talks to the executor it was given.
    """

    def __init__(self, manager: str, executor: Executor, adopts: AdoptedMarks | None = None) -> None:
        self._manager = manager
        self._executor = executor
        self._adopts = adopts
        # shlex.quote() is a no-op for this fixed, already-shell-safe relpath (only
        # word chars, '.', '/'), but is applied anyway per T-02-01 (ASVS V5) rather
        # than assuming a future manager name stays that safe. Left OUTSIDE the `~/`
        # prefix: quoting the whole `~/...` expression would disable tilde expansion,
        # while `~/` immediately followed by a (possibly-)quoted word is still one
        # shell word — bash expands the leading `~/` and appends the rest literally.
        relpath = DECISION_FILE_RELPATH_TEMPLATE.format(manager=manager)
        self._path_expr = f"~/{shlex.quote(relpath)}"
        self._display_path = f"~/{relpath}"

    async def load(self) -> dict[str, DecisionEntry]:
        """Read this manager's decisions, plus any it adopts from an older manager's file
        (`AdoptedMarks`), or an empty mapping (D-08's degrade rule).

        This file wins on a shared `item_id`: an answer given since the split is the
        current one.
        """
        entries = await self._load_own()
        if self._adopts is None:
            return entries
        adopted = {
            item_id: entry
            for item_id, entry in (await DecisionFile(self._adopts.manager, self._executor)._load_own()).items()
            if item_id.startswith(self._adopts.item_id_prefix)
        }
        return {**adopted, **entries}

    async def _load_own(self) -> dict[str, DecisionEntry]:
        """This file's own entries alone.

        Absent, empty and malformed all degrade to "no permanent decisions" rather
        than aborting the sync; only the malformed case logs a WARNING (naming the
        path) since that one indicates the file was tampered with or hand-edited
        incorrectly, not simply "nothing recorded yet".
        """
        result = await self._executor.run_command(f"cat {self._path_expr} 2>/dev/null")
        if not result.success or not result.stdout.strip():
            return {}

        try:
            return _deserialize(result.stdout)
        except (yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError) as exc:
            _logger.warning(
                "Malformed decision file %s (%s); treating as no permanent decisions",
                self._display_path,
                exc,
            )
            return {}

    async def record(self, entry: DecisionEntry) -> None:
        """Merge `entry` into this file by `item_id` (last write wins) and write it back.

        The serialised bytes travel as one shlex-quoted `printf` argument through
        `self._executor` — never a local filesystem write — so this identical method
        is correct whether `self._executor` is the source's `LocalExecutor` or the
        target's `RemoteExecutor`.

        Own entries only: an adopted entry (`AdoptedMarks`) stays in the file that holds
        it, so recording one answer never rewrites another manager's file wholesale.
        """
        entries = await self._load_own()
        entries[entry.item_id] = entry
        await self._write(
            _serialize(entries),
            mutates=f"record permanent skip for {entry.label} in {self._display_path}",
            failure=f"failed to record decision for {entry.item_id!r}",
        )

    async def drop(self, item_ids: Collection[str]) -> frozenset[str]:
        """Remove `item_ids` from this file and write it back; returns what was actually
        removed, which is `item_ids` minus anything the file did not hold.

        The counterpart to `record`, and the reason a mark is not simply written once and
        left: an entry says "this machine's own copy of X stays as it is", and once this
        machine has no copy of X there is nothing left for it to say. Kept as durable as
        `record` makes it while its item is there (D-08's whole argument for scanning
        aggressively), and no longer: an entry naming software the machine no longer has
        goes on suppressing that item in BOTH roles, so it silently blocks a later install
        of it here — an outcome the answer that wrote the entry never chose. Which entries
        those are is `PackageSyncJob.observe_absent_marks`'s business; this method only
        writes the result.

        Writes nothing at all when the file holds none of `item_ids`, so a run over a file
        with nothing dead in it issues no command and needs no confirmation — and reads
        nothing either when `item_ids` is empty, which is every run that found no dead mark.

        Covers the adopted file too (`AdoptedMarks`): an entry `load` returns is one this
        job acts on, so a dead one has to leave the file it actually lives in. Dropping it
        from this file alone would leave `load` adopting it again on the next run, which is
        the mark outliving its item — the exact thing this method exists to prevent.
        """
        if not item_ids:
            return frozenset()

        removed = await self._drop_from_own_file(item_ids)
        if self._adopts is not None:
            adopted_ids = [item_id for item_id in item_ids if item_id.startswith(self._adopts.item_id_prefix)]
            legacy = DecisionFile(self._adopts.manager, self._executor)
            removed |= await legacy._drop_from_own_file(adopted_ids)
        return removed

    async def _drop_from_own_file(self, item_ids: Collection[str]) -> frozenset[str]:
        """`drop`, over this file's own entries alone."""
        if not item_ids:
            return frozenset()

        entries = await self._load_own()
        removed = frozenset(item_id for item_id in item_ids if item_id in entries)
        if not removed:
            return frozenset()

        remaining = {item_id: entry for item_id, entry in entries.items() if item_id not in removed}
        labels = ", ".join(sorted(entries[item_id].label for item_id in removed))
        await self._write(
            _serialize(remaining),
            mutates=f"drop the machine-specific mark on {labels} from {self._display_path}",
            failure=f"failed to drop {sorted(removed)} from",
        )
        return removed

    async def _write(self, content: str, *, mutates: str, failure: str) -> None:
        """Replace this file's content atomically: `mkdir --parents` the directory, write
        to a sibling `.pcswitcher-tmp` path, then `mv --force` it into place — the same
        atomic-replace-within-the-same-directory shape `vscode_state_sync._sync_editor`
        uses, so an interrupted write can never leave a truncated file.
        """
        dir_relpath = shlex.quote(_DECISION_DIR_RELPATH)
        tmp_expr = f"{self._path_expr}.pcswitcher-tmp"
        cmd = (
            f"mkdir --parents ~/{dir_relpath} && "
            f"printf '%s' {shlex.quote(content)} > {tmp_expr} && "
            f"mv --force {tmp_expr} {self._path_expr}"
        )
        result = await self._executor.run_command(cmd, mutates=mutates)
        if not result.success:
            raise RuntimeError(f"{failure} {self._display_path}: {result.stderr.strip()}")


# ---------------------------------------------------------------------------------
# SnippetRegistry — the shared, synced counterpart to DecisionFile above (D-20, D-23).
# ---------------------------------------------------------------------------------

_SNIPPET_FILE_HEADER = (
    "# pc-switcher install-snippet registry — regenerated on every write.\n"
    "#\n"
    "# Each entry is an opaque shell snippet pc-switcher replays VERBATIM to converge\n"
    "# an item no package manager can reproduce (D-20): a bare .deb or a manual\n"
    "# install. The tool never parses, versions, diffs or reasons about the body —\n"
    "# edit or remove an entry by hand if it goes stale.\n"
    "#\n"
    "# This file lives in the shared, synced config (D-23): every peer that runs\n"
    "# `pc-switcher sync` carries it to the target alongside config.yaml. A snippet\n"
    "# replays non-interactively with no stdin available — one that expects a prompt\n"
    "# fails rather than hanging the sync.\n"
)


@dataclass(frozen=True)
class Snippet:
    """One recorded install snippet (D-20): an opaque shell command that reproduces an
    item no package manager can install on its own.

    `label` mirrors the unreproducible item's own label at authoring time (a snapshot,
    not a live reference) so the registry file reads meaningfully on its own. `body` is
    NEVER inspected by this dataclass or its callers beyond being replayed byte-for-byte:
    it arrives already stripped of surrounding whitespace from the one place a snippet is
    captured (`packages.review`), which is what keeps the YAML a person can read and the
    string `replay` quotes identical.
    """

    item_id: str
    label: str
    body: str
    authored_at: str  # ISO-8601 UTC
    authored_on: str  # hostname of the machine the snippet was authored on


def _serialize_snippets(entries: Mapping[str, Snippet]) -> str:
    """Render `entries` as the snippet-registry YAML body, header regenerated fresh."""
    snippets = {
        item_id: {
            "label": entry.label,
            "body": entry.body,
            "authored_at": entry.authored_at,
            "authored_on": entry.authored_on,
        }
        for item_id, entry in entries.items()
    }
    body = yaml.safe_dump({"snippets": snippets}, sort_keys=False, default_flow_style=False)
    return f"{_SNIPPET_FILE_HEADER}\n{body}"


#: Everything `yaml.safe_load` and the shape check below can throw for a file that is not
#: a snippet registry. Named once so the two entry points that read a registry — the
#: executor-backed `SnippetRegistry.load` and the on-disk `load_snippets_from_text` — cannot
#: drift on which failures count as "unreadable".
_UNREADABLE = (yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError)


def _unreadable_registry(display_path: str, machine: str | None, exc: Exception) -> SyncAborted:
    """The abort a registry that cannot be parsed raises (`PKG-FR-REGISTRY-CONSENT`).

    An absent or empty registry means "no snippets"; a file that is there and cannot be read
    means nothing, and reading it as "no snippets" is what would let a wholesale push
    silently discard every entry the other machine holds. The run ends instead of the job
    failing, because the repair is a hand edit on one machine and the next sync is what
    should see the result — the same reason declining the overwrite question ends the run.

    Plain `SyncAborted`, not the ByUser subclass: nobody was asked anything here, so
    nothing rendered from it may report the user as having stopped the sync (#224).
    """
    where = f" on {machine}" if machine else ""
    return SyncAborted(
        f"the install-snippet registry {display_path}{where} cannot be read as a registry ({exc}); "
        "repair or delete that file, then start a new sync"
    )


def _deserialize_snippets(raw: str) -> dict[str, Snippet]:
    """Parse a snippet registry's content into `{item_id: Snippet}`.

    Raises anything in `_UNREADABLE` for content that is not a snippet registry; both
    callers turn that into `_unreadable_registry`'s abort.

    Every entry is tried before that is raised, and the failure names all of them: the repair
    is a hand edit of this one file, and stopping at the first malformed entry would have the
    user fix it, start a new sync, and only then be shown the next.
    """
    data = yaml.safe_load(raw)
    snippets = data.get("snippets") if isinstance(data, dict) else None
    if not isinstance(snippets, dict):
        raise ValueError("snippet registry has no 'snippets' mapping")

    entries: dict[str, Snippet] = {}
    malformed: list[str] = []
    for item_id, fields in snippets.items():
        try:
            entries[str(item_id)] = Snippet(
                item_id=str(item_id),
                label=fields["label"],
                body=fields["body"],
                authored_at=fields["authored_at"],
                authored_on=fields["authored_on"],
            )
        except KeyError as exc:
            malformed.append(f"{item_id} (missing field {exc})")
        except TypeError as exc:
            # The entry is not a mapping at all: a bare scalar, a list, or nothing.
            malformed.append(f"{item_id} ({exc})")
    if malformed:
        raise ValueError(f"unreadable snippet entries: {'; '.join(malformed)}")
    return entries


def load_snippets_from_text(raw: str, *, display_path: str, machine: str | None = None) -> dict[str, Snippet]:
    """Parse snippet-registry file content into `{item_id: Snippet}`; empty content means
    "no snippets" and content that cannot be parsed ends the run (`_unreadable_registry`) —
    the same rule `SnippetRegistry.load` applies to executor-read content.

    An unreproducible job uses this to read the SOURCE's on-disk registry — the exact
    bytes `_push_snippet_registry` is about to `send_file` to the target — when deciding
    whether a wholesale overwrite is purely additive (decision 9). Reading the file that
    is actually sent keeps the additive check consistent with the transfer, rather than
    re-querying the source executor which may lag the just-finalized on-disk file.

    `display_path` and `machine` are what the abort names, since the raw text carries
    neither.
    """
    if not raw.strip():
        return {}
    try:
        return _deserialize_snippets(raw)
    except _UNREADABLE as exc:
        raise _unreadable_registry(display_path, machine, exc) from exc


class SnippetRegistry:
    """Read/write/replay access to the shared install-snippet registry, through
    whichever `Executor` is local to the machine this instance should read/write
    (same one-`Executor`-per-instance shape `DecisionFile` follows).

    Unlike `DecisionFile`, the registry is not machine-scoped data — both machines may
    hold different copies of the SAME file until an unreproducible job reconciles them
    by pushing the source's copy to the target (D-23). Construct with
    `SnippetRegistry(self.source)` to read/write the source's own copy — the reproducibility
    authority `plan()` classifies against (corrected D-23), and where a freshly authored
    snippet is recorded before that run's push carries it to the target — or
    `SnippetRegistry(self.target)` to read the target's copy at converge time, after this
    run's push has already placed it there.
    """

    def __init__(self, executor: Executor, machine: str | None = None) -> None:
        self._executor = executor
        # The hostname of the machine this instance reads and writes, for the one message
        # that has to say WHICH copy of a same-named file is the problem. Optional so a
        # caller with no hostname to hand still gets a message naming the path.
        self._machine = machine
        # shlex.quote() is a no-op for this fixed, already-shell-safe relpath, applied
        # anyway per T-02-01 (ASVS V5) — see `DecisionFile.__init__`'s identical
        # reasoning for why it stays OUTSIDE the `~/` prefix.
        self._path_expr = f"~/{shlex.quote(SNIPPET_REGISTRY_RELPATH)}"
        self._display_path = f"~/{SNIPPET_REGISTRY_RELPATH}"

    async def load(self) -> dict[str, Snippet]:
        """Read every snippet, or an empty mapping if the registry is absent or empty.

        A registry that is THERE and cannot be parsed is neither: it ends the whole run
        (`_unreadable_registry`), because "no snippets" is a claim about the machine that
        this file no longer supports, and acting on it would push over entries nobody can
        see. Unlike `DecisionFile.load`, which degrades — a decision file that cannot be
        read costs the user a question they answered before, not their snippets.
        """
        result = await self._executor.run_command(f"cat {self._path_expr} 2>/dev/null")
        if not result.success or not result.stdout.strip():
            return {}

        try:
            return _deserialize_snippets(result.stdout)
        except _UNREADABLE as exc:
            raise _unreadable_registry(self._display_path, self._machine, exc) from exc

    async def get(self, item_id: str) -> Snippet | None:
        """The snippet registered for `item_id`, or `None` if there is none."""
        entries = await self.load()
        return entries.get(item_id)

    async def add(self, snippet: Snippet) -> None:
        """Merge `snippet` into the registry by `item_id` (last write wins) and write
        atomically — the identical `mkdir --parents && printf ... > tmp && mv --force` shape
        `DecisionFile.record` uses, so an interrupted write can never leave a truncated
        file.
        """
        entries = await self.load()
        entries[snippet.item_id] = snippet
        content = _serialize_snippets(entries)

        dir_relpath = shlex.quote(_DECISION_DIR_RELPATH)
        tmp_expr = f"{self._path_expr}.pcswitcher-tmp"
        cmd = (
            f"mkdir --parents ~/{dir_relpath} && "
            f"printf '%s' {shlex.quote(content)} > {tmp_expr} && "
            f"mv --force {tmp_expr} {self._path_expr}"
        )
        result = await self._executor.run_command(
            cmd, mutates=f"record install snippet for {snippet.label} in {self._display_path}"
        )
        if not result.success:
            raise RuntimeError(
                f"failed to add snippet for {snippet.item_id!r} in {self._display_path}: {result.stderr.strip()}"
            )

    async def replay(self, item_id: str, executor: RemoteExecutor) -> CommandResult:
        """Replay the snippet registered for `item_id` against `executor` (always the
        TARGET in practice) as an opaque blob (D-20): the body is never parsed,
        templated or inspected, only quoted.

        Runs as `bash -c <shlex.quote(body)>` — the same build-a-command,
        pass-content-as-an-argv-quoted-string shape `vscode_state_sync.target_sql_command`
        uses for SQL — with `login_shell=False`, since a snippet is a fixed shell script,
        not something that needs the user's `~/.profile` sourced. No stdin is ever
        supplied (`pcswitcher.executor.Process` documents this is intentional), so a
        snippet expecting a prompt fails rather than hanging the sync. The returned
        `CommandResult`'s exit code alone decides success — never raises for "no
        snippet registered", instead returning a failed `CommandResult` so a stale plan
        (the registry changed between `plan()` and `apply()`) is a per-item failure
        like any other, not a crash that stops the whole job (D-27).

        The exact `bash -c <quoted body>` command is what `--confirm-each-command` shows:
        an opaque snippet is the one thing in a sync whose content pc-switcher cannot vouch
        for, so it is displayed verbatim before it runs.
        """
        snippet = await self.get(item_id)
        if snippet is None:
            return CommandResult(exit_code=1, stdout="", stderr=f"no snippet registered for {item_id!r}")

        cmd = f"bash -c {shlex.quote(snippet.body)}"
        return await executor.run_command(
            cmd, login_shell=False, mutates=f"replay install snippet for {snippet.label}"
        )
