"""Item model for the package-sync pipeline (D-02, ADR-020).

Item identity is the primary key for the whole subsystem: every package, apt source,
signing key, pin, config file, snap, snap channel, flatpak ref, flatpak remote and
unreproducible install this phase handles is identified by a stable `item_id` string,
not by any manager-specific value. A package name, a flatpak ref, a filename — each is
a FIELD of one variant's dataclass, never the primary key itself. This is what lets the
shared diff/review/decision-file pipeline in `package_sync_core.py` operate on one shape
(`ItemDiff`) regardless of which manager or item class produced it.

This slice (plan 02-03, the tracer) only implements `AptPackageItem` and the
`MISSING_ON_TARGET`/`INSTALL` diff direction. `ItemClass` and `DiffClass` declare their
full D-02/D-25 member sets now so later plans add item types and diff directions without
reopening these enums' shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "AptPackageItem",
    "DiffAction",
    "DiffClass",
    "ItemClass",
    "ItemDiff",
]


class ItemClass(StrEnum):
    """The full D-02 item-class taxonomy. Only APT_PACKAGE is produced by this plan."""

    APT_PACKAGE = "apt_package"
    APT_SOURCE = "apt_source"
    APT_KEY = "apt_key"
    APT_PIN = "apt_pin"
    APT_CONFIG = "apt_config"
    SNAP = "snap"
    SNAP_CHANNEL = "snap_channel"
    FLATPAK_REF = "flatpak_ref"
    FLATPAK_REMOTE = "flatpak_remote"
    UNREPRODUCIBLE = "unreproducible"


class DiffClass(StrEnum):
    """The full D-25 conflict taxonomy. Only MISSING_ON_TARGET is produced by this plan."""

    MISSING_ON_TARGET = "missing_on_target"
    EXTRA_ON_TARGET = "extra_on_target"
    VERSION_MISMATCH = "version_mismatch"
    HELD_OR_PINNED = "held_or_pinned"
    REPO_UNAVAILABLE = "repo_unavailable"
    UNREPRODUCIBLE = "unreproducible"


class DiffAction(StrEnum):
    """The concrete converge verb a diff implies (D-07's direction-dependent "apply").

    Values match `package_review`'s private removal-action set (`{"remove", "delete",
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
    `package_review.review_items()` only ever see `ItemDiff`/`ReviewEntry` shapes.
    """

    item_class: ItemClass
    diff_class: DiffClass
    action: DiffAction
    item_id: str
    label: str
    detail: str | None = None
