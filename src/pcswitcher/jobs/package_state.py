"""Machine-local decision store: the ONLY pc-switcher state deliberately per-machine
and never synced (D-08, D-08a, D-09).

An entry recorded here means "inert on THIS machine in both roles": not pushed when
this machine is the source, not installed or removed when this machine is the target.
D-19's whole argument for scanning aggressively — a finding produces noise exactly
once, then never again — only holds if this durability is real.

Which machine's file gets an entry follows which machine HOLDS the item (D-08a): a
source-held item declined during review is recorded on the source, a target-held
item whose removal is declined is recorded on the target. Because the file is
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
follows for its own tool-state filter token).
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import yaml

from pcswitcher.config_sync import CONFIG_REMOTE_DIR
from pcswitcher.jobs.package_items import ItemClass

if TYPE_CHECKING:
    from pcswitcher.executor import Executor

__all__ = [
    "DECISION_FILE_GLOB_RELPATH",
    "DECISION_FILE_RELPATH_TEMPLATE",
    "DecisionEntry",
    "DecisionFile",
    "filter_inert",
]

_logger = logging.getLogger("pcswitcher.jobs.package_state")

# Home-relative directory holding every manager's decision file, derived from
# CONFIG_REMOTE_DIR ("~/.config/pc-switcher") rather than a second hardcoded literal.
_DECISION_DIR_RELPATH = CONFIG_REMOTE_DIR.removeprefix("~/")

# `{manager}.decisions.yaml`, home-relative — one file per manager (D-09).
DECISION_FILE_RELPATH_TEMPLATE = f"{_DECISION_DIR_RELPATH}/{{manager}}.decisions.yaml"

# The single glob `folder_sync` consumes for its non-overridable exclusion, so the
# exclusion and the store this module owns can never drift apart.
DECISION_FILE_GLOB_RELPATH = f"{_DECISION_DIR_RELPATH}/*.decisions.yaml"

_FILE_HEADER = (
    "# pc-switcher machine-specific decision file — regenerated on every write.\n"
    "#\n"
    '# Every entry below came from an explicit "skip always" choice in a sync\n'
    "# review (D-08). An item listed here is inert on THIS machine in both roles:\n"
    "# never pushed to a peer when this machine is the source, never installed or\n"
    "# removed here when this machine is the target.\n"
    "#\n"
    "# This file is machine-local and is never synced to any peer (D-09). Remove\n"
    "# an entry (or delete the whole file) to make that item eligible again on the\n"
    "# next sync.\n"
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


class _HasItemId(Protocol):
    @property
    def item_id(self) -> str: ...


async def filter_inert[T: _HasItemId](items: Sequence[T], decisions: Mapping[str, DecisionEntry]) -> list[T]:
    """Items whose `item_id` is ABSENT from `decisions` — the ones still live.

    A pure, module-level function (not a method) so both the source-capture side
    (drop recorded items from the manifest before it is even diffed) and the
    target-query side (drop recorded items from what would otherwise become a
    proposed install/remove) share exactly one definition of "inert" (D-08).
    """
    return [item for item in items if item.item_id not in decisions]


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

    def __init__(self, manager: str, executor: Executor) -> None:
        self._manager = manager
        self._executor = executor
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
        """Read this manager's decisions, or an empty mapping (D-08's degrade rule).

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
        """Merge `entry` into this file by `item_id` (last write wins) and write
        atomically: `mkdir -p` the directory, write the new content to a sibling
        `.pcswitcher-tmp` path, then `mv -f` it into place — the same
        atomic-replace-within-the-same-directory shape `vscode_state_sync._sync_editor`
        uses, so an interrupted write can never leave a truncated file.

        The serialised bytes travel as one shlex-quoted `printf` argument through
        `self._executor` — never a local filesystem write — so this identical method
        is correct whether `self._executor` is the source's `LocalExecutor` or the
        target's `RemoteExecutor`.
        """
        entries = await self.load()
        entries[entry.item_id] = entry
        content = _serialize(entries)

        dir_relpath = shlex.quote(_DECISION_DIR_RELPATH)
        tmp_expr = f"{self._path_expr}.pcswitcher-tmp"
        cmd = (
            f"mkdir -p ~/{dir_relpath} && "
            f"printf '%s' {shlex.quote(content)} > {tmp_expr} && "
            f"mv -f {tmp_expr} {self._path_expr}"
        )
        result = await self._executor.run_command(cmd)
        if not result.success:
            raise RuntimeError(
                f"failed to record decision for {entry.item_id!r} in {self._display_path}: {result.stderr.strip()}"
            )
