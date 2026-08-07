"""`snap list --all` output parsing and the sideload rule, shared by `snap_sync` and
`manual_snap_sync`.

Two jobs read the same listing and divide the machine between them on one predicate:
`snap_sync` converges the store-installed snaps and withholds the sideloaded ones
(`PKG-FR-SNAP-SIDELOAD`), `manual_snap_sync` offers exactly the ones it withheld. The two
therefore have to agree about what a sideload IS, and they agree by both calling
`is_sideloaded` rather than by one telling the other: a second copy of the `x`-prefix rule
would let the two drift and leave a snap owned by both jobs or by neither.

Shared here rather than duplicated per job, and here rather than on `PackageSyncJob` —
`apt_policy.py` is the precedent, and its reasoning holds unchanged: this module defines
no job class and sits in no job's MRO, so the other managers inherit nothing from it, and
`manual_snap_sync` never imports `snap_sync` (`PKG-FR-MANUAL-SCOPE`).

`SnapItem` moved here with the parser that builds it: it was in `snap_sync.py` while no
other job constructed one, and `manual_snap_sync` now does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from pcswitcher.jobs.packages.items import ItemClass

__all__ = ["SnapItem", "is_sideloaded", "parse_snap_list", "partition_sideloaded"]


@dataclass(frozen=True)
class SnapItem:
    """One installed snap (`PKG-FR-SNAP-REVISION`): name, tracked channel, and installed revision.

    `channel` is a FIELD of the snap item, not a standalone item class:
    `ItemClass.SNAP_CHANNEL` is reserved for the diff DETAIL on a channel-only change
    (retracking with no revision change) and never becomes a standalone item — a
    channel with no snap attached to it has no meaning of its own.

    `held` is per-snap refresh-hold state parsed from `snap list` Notes (#208): it is a
    FIELD, not part of the snap's identity, and defaults `False` so existing construction
    sites and the shared diff never have to name it. `snap_sync` populates it and diffs it
    into a separate `snap:hold:<name>` membership item (`ItemClass.SNAP_HOLD`), keeping the
    hold's own `snap:hold:<name>` membership diff (`ItemClass.SNAP_HOLD`), which replicates
    without review like every other block (`PKG-FR-BLOCKS-DERIVED`).

    `classic` and `devmode` are the snap's CONFINEMENT, likewise parsed from the Notes
    column and likewise FIELDS rather than identity, defaulted so existing construction
    sites and the shared diff never have to name them. They are not identity because
    confinement is a property snapd derives from the revision the store published, not a
    user choice the two machines can legitimately disagree about for the same revision:
    making it identity would split one snap into two items, and diffing on it would emit a
    `CHANGE` proposing a "convergence" with no command behind it. They exist solely so
    `snap_sync` can pass `--classic`/`--devmode` to `snap install`/`snap refresh`, which
    snapd requires as explicit per-revision confirmation before it will install a
    classic-confinement or devmode revision at all.

    `version` is the snap's own declared version, the `Version` column beside `Rev`, and it
    is excluded from equality (`compare=False`). `snap_sync` converges the REVISION and must
    go on comparing two `SnapItem`s on the fields it acts upon; the version is a fact
    `manual_snap_sync` needs and `snap_sync` does not, and folding it into equality would
    make two identical snaps compare unequal whenever the store restated a version.

    That version, and never the revision, is what `manual_snap_sync` compares a sideload on
    (`PKG-FR-MANUAL-VERSION`): two machines' `x<N>` revisions are independent install
    counters rather than two builds, so comparing them would report a difference between one
    machine having reinstalled more often than the other.
    """

    name: str
    channel: str
    revision: str
    held: bool = False
    classic: bool = False
    devmode: bool = False
    version: str = field(default="", compare=False)

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.SNAP

    @property
    def item_id(self) -> str:
        """Stable identity string: `snap:<name>`."""
        return f"snap:{self.name}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.name} ({self.channel}, revision {self.revision})"


def parse_snap_list(output: str) -> list[SnapItem]:
    """Parse `snap list --all` by HEADER column names, never fixed offsets or assumed
    order (RESEARCH Open Question 2): a future snapd column reorder must yield correct
    values, never a silently wrong revision driving a wrong `--revision` install.

    Skips a disabled older-revision line (`Notes` names `disabled`) for a snap that
    also has an active line — only the active revision becomes the item. Output shaped
    like "No snaps are installed yet." (no recognizable header) degrades to an empty
    list rather than raising: a snap-free machine is a valid, if rare, state.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return []

    header = lines[0].split()
    try:
        name_idx = header.index("Name")
        rev_idx = header.index("Rev")
        tracking_idx = header.index("Tracking")
        notes_idx = header.index("Notes")
    except ValueError:
        return []
    # Optional, unlike the four above: a listing whose header omits it still yields usable
    # snaps for `snap_sync`, which converges revisions and never reads a version.
    version_idx = header.index("Version") if "Version" in header else None

    max_idx = max(name_idx, rev_idx, tracking_idx, notes_idx)
    items: list[SnapItem] = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) <= max_idx:
            continue
        notes = fields[notes_idx].split(",")
        if "disabled" in notes:
            continue
        # `held` in the Notes column is a PER-SNAP refresh hold (#208, D9) — snapstate
        # attached to this one snap. It is SEPARATE state from the system-wide
        # `refresh.hold` the orchestrator sets across the sync window via `snap set
        # system refresh.hold` (different snapd namespaces), so capturing here, inside
        # the sync window, does not mask a per-snap hold: the system hold never writes
        # `held` into an individual snap's Notes. If a VM integration test ever shows a
        # system hold flipping this token, capture would have to move BEFORE the
        # sync-window hold is applied. Fail-safe even then: a system hold flips both
        # hosts symmetrically -> both-held -> no spurious diff.
        held = "held" in notes
        # Confinement, from the same Notes list: snapd refuses to install a classic or a
        # devmode revision without the matching flag as explicit confirmation, so the
        # capture has to carry it or `_converge_install` cannot build a working command.
        items.append(
            SnapItem(
                name=fields[name_idx],
                channel=fields[tracking_idx],
                revision=fields[rev_idx],
                held=held,
                classic="classic" in notes,
                devmode="devmode" in notes,
                version=fields[version_idx] if version_idx is not None and version_idx < len(fields) else "",
            )
        )
    return items


def is_sideloaded(item: SnapItem) -> bool:
    """Whether this snap's bytes came from a local `.snap` file rather than the store.

    snapd assigns a store-less revision to a snap installed from a file (`snap install
    --dangerous ./foo.snap`, `snap try`) and `snap list` renders it with an `x` prefix —
    `x1`, `x2`, … — where a store revision is a plain integer. No store can serve an
    `x<N>` revision, so `snap install --revision=x1 <name>` can never succeed; and such a
    snap usually tracks no channel either, which makes the channel switch that follows
    meaningless too.

    The seam between the two snap jobs: `snap_sync` withholds every name this answers True
    for and `manual_snap_sync` detects exactly those, so a snap is one job's or the other's
    and never both or neither.
    """
    return item.revision.startswith("x")


def partition_sideloaded(items: Sequence[SnapItem]) -> tuple[list[SnapItem], list[SnapItem]]:
    """Split a captured listing into (store-installed, sideloaded), preserving order."""
    store_items = [item for item in items if not is_sideloaded(item)]
    sideloaded = [item for item in items if is_sideloaded(item)]
    return store_items, sideloaded
