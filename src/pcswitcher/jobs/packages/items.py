"""Item model shared by every package job (D-02, ADR-020).

Item identity is the primary key for the whole subsystem: every package, apt source,
signing key, pin, config file, snap, snap channel, flatpak ref, flatpak remote and
unreproducible install this phase handles is identified by a stable `item_id` string,
not by any manager-specific value. A package name, a flatpak ref, a filename — each is
a FIELD of one variant's dataclass, never the primary key itself. This is what lets the
review and decision-file pipeline operate on one shape (`ItemDiff`) regardless of which
manager produced it.

What belongs here is only what more than one manager uses. A shape only `snap_sync`
constructs lives in `snap_sync`; `AptSourceItem` lives in `apt_sync`; a detail string
only `flatpak_sync` writes lives in `flatpak_sync`. The four jobs are deliberately
independent (D-15), and a registry of everyone's private shapes is a shared surface
they have to agree on — it couples the jobs without any of them gaining a thing.

`ItemClass` and `DiffClass` stay whole here even though each member is produced by one
manager: they are the taxonomy the shared review and the decision files are keyed on, so
they are the definition of what the pipeline can carry, not one manager's business.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "DiffAction",
    "DiffClass",
    "ItemClass",
    "ItemDiff",
    "build_version_mismatch_detail",
]


class ItemClass(StrEnum):
    """The full D-02 item-class taxonomy.

    `APT_PACKAGE` is captured and diffed by `apt_sync` (plan 02-03/02-05). `SNAP`,
    `FLATPAK_REF`, `FLATPAK_REMOTE` and `UNREPRODUCIBLE` have item dataclasses (below)
    but no capturing job yet — those arrive in plans 02-07..02-09. `SNAP_CHANNEL`
    never becomes a standalone item; see `SnapItem`'s docstring.
    """

    APT_PACKAGE = "apt_package"
    APT_SOURCE = "apt_source"
    APT_PIN = "apt_pin"
    APT_CONFIG = "apt_config"
    APT_HOLD = "apt_hold"
    SNAP = "snap"
    SNAP_CHANNEL = "snap_channel"
    SNAP_HOLD = "snap_hold"
    FLATPAK_REF = "flatpak_ref"
    FLATPAK_REMOTE = "flatpak_remote"
    FLATPAK_MASK = "flatpak_mask"
    UNREPRODUCIBLE = "unreproducible"


class DiffClass(StrEnum):
    """The full D-25 conflict taxonomy — every member is producible once this plan's
    `diff_items` dispatch (`packages/sync_core.py`) is filled out.

    `REPO_UNAVAILABLE` and `ORIGIN_MISMATCH` are the two ADR-021 D-34 classes and are about
    WHERE an item comes from, not whether it is present:

    - `REPO_UNAVAILABLE` — the source's origin cannot be provided on the target, so the
      item is reported rather than installed from somewhere else. Redefined by ADR-021: it
      no longer means "apt printed `Candidate: (none)`", which said nothing about
      provenance and read as unavailable for packages the target could have installed.
    - `ORIGIN_MISMATCH` — present on both machines, from two different vendors. A real
      divergence a presence-and-version diff cannot see.
    """

    MISSING_ON_TARGET = "missing_on_target"
    EXTRA_ON_TARGET = "extra_on_target"
    VERSION_MISMATCH = "version_mismatch"
    HELD_OR_PINNED = "held_or_pinned"
    REPO_UNAVAILABLE = "repo_unavailable"
    ORIGIN_MISMATCH = "origin_mismatch"
    UNREPRODUCIBLE = "unreproducible"


class DiffAction(StrEnum):
    """The concrete converge verb a diff implies (D-07's direction-dependent "apply").

    Values match `packages.review`'s private removal-action set (`{"remove", "delete",
    "disable"}`) so a `ReviewGroup` built from these actions gets the right
    default-checked/unchecked behavior without that module knowing this enum exists.
    """

    INSTALL = "install"
    REMOVE = "remove"
    CHANGE = "change"
    REPORT_ONLY = "report_only"


@dataclass(frozen=True)
class ItemDiff:
    """One item's diff result — the one shape the review and converge loop both consume.

    This is D-02's "all classes flow through one pipeline" made real: regardless of
    which manager or item class produced it, `PackageSyncJob.apply()` and
    `packages.review.review_items()` only ever see `ItemDiff`/`ReviewEntry` shapes.
    """

    item_class: ItemClass
    diff_class: DiffClass
    action: DiffAction
    item_id: str
    label: str
    detail: str | None = None


def build_version_mismatch_detail(source_version: str, target_version: str) -> str:
    """Detail string for a `VERSION_MISMATCH` diff: both versions, machine-labelled.

    Showing both versions in the review text is what makes D-04's "detected and
    reported, never force-downgraded" promise visible to the user — nothing here
    proposes a resolution, it names the two facts and leaves the decision alone.
    """
    return f"source has {source_version}, target has {target_version}"
