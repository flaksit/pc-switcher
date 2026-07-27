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
from typing import ClassVar, Literal

__all__ = [
    "AptHoldItem",
    "AptPackageItem",
    "DiffAction",
    "DiffClass",
    "HoldPinFact",
    "ItemClass",
    "ItemDiff",
    "build_held_or_pinned_detail",
    "build_repo_unavailable_detail",
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
    """

    MISSING_ON_TARGET = "missing_on_target"
    EXTRA_ON_TARGET = "extra_on_target"
    VERSION_MISMATCH = "version_mismatch"
    HELD_OR_PINNED = "held_or_pinned"
    REPO_UNAVAILABLE = "repo_unavailable"
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
class AptPackageItem:
    """One manually-installed apt package (D-03), captured from `apt-mark showmanual`
    plus one batched `dpkg-query` call for versions.
    """

    name: str
    version: str

    @property
    def item_id(self) -> str:
        """Stable identity string: `apt:package:<name>`."""
        return f"apt:package:{self.name}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.name} ({self.version})" if self.version else self.name


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


@dataclass(frozen=True)
class HoldPinFact:
    """One fact about a package's upgrade being blocked, from one of two distinct
    mechanisms (RESEARCH Pitfall 2):

    - A HOLD is dpkg *selection state* stored under `/var/lib/dpkg`, read via
      `apt-mark showhold`. It blocks ALL upgrades of that package outright.
    - A PIN is an apt priority *preference* stored under `/etc/apt/preferences.d`. It
      can still allow an upgrade within whatever the pin's priority permits — it is
      not an absolute block.

    Both surface under the same `DiffClass.HELD_OR_PINNED` review category (D-25), but
    they are read from two different sources and mean different things. A diff
    implementation that reads only one silently misses every package blocked by the
    other mechanism, which is why `mechanism` and `source_ref` stay on this fact
    rather than being collapsed into a single boolean.
    """

    mechanism: Literal["hold", "pin"]
    package: str
    source_ref: str


def build_version_mismatch_detail(source_version: str, target_version: str) -> str:
    """Detail string for a `VERSION_MISMATCH` diff: both versions, machine-labelled.

    Showing both versions in the review text is what makes D-04's "detected and
    reported, never force-downgraded" promise visible to the user — nothing here
    proposes a resolution, it names the two facts and leaves the decision alone.
    """
    return f"source has {source_version}, target has {target_version}"


def build_held_or_pinned_detail(fact: HoldPinFact) -> str:
    """Detail string for a `HELD_OR_PINNED` diff: names the mechanism and its origin
    so a hold and a pin never read as the same fact in the review, even though both
    surface under one category (RESEARCH Pitfall 2).
    """
    verb = "held" if fact.mechanism == "hold" else "pinned"
    return f"{verb} ({fact.mechanism}, via {fact.source_ref})"


def build_repo_unavailable_detail(name: str) -> str:
    """Detail string for a `REPO_UNAVAILABLE` diff: the target's own repositories
    offer no installable candidate for this package (`apt-cache policy` showed none).
    This must read as its own fact, not silently downgrade to a proposed `INSTALL`.
    """
    return f"target's repositories offer no candidate for {name}"


@dataclass(frozen=True)
class AptHoldItem:
    """One apt package hold (#208): dpkg selection state read via `apt-mark showhold`.

    A hold is boolean-membership: a package is either held or it is not, so this item
    carries only the package `name` and diffs as a presence difference (source-held &
    target-not -> add the hold; target-held & source-not -> remove it). Its identity
    (`apt:hold:<name>`) is DISTINCT from the package item's (`apt:package:<name>`) so a
    package and its hold are two separate review items — replicating the user's
    deliberate "block all upgrades" intent independently of whether the package itself
    is being installed this run.
    """

    name: str

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.APT_HOLD

    @property
    def item_id(self) -> str:
        """Stable identity string: `apt:hold:<name>`."""
        return f"apt:hold:{self.name}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.name} (hold)"
