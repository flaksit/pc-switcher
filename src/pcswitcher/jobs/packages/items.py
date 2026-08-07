"""Item model shared by every package job (ADR-020, ADR-020).

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
independent (`PKG-FR-JOB-INDEPENDENCE`), and a registry of everyone's private shapes is a shared surface
they have to agree on — it couples the jobs without any of them gaining a thing.

`ItemClass` and `DiffClass` stay whole here even though each member is produced by one
manager: they are the taxonomy the shared review and the decision files are keyed on, so
they are the definition of what the pipeline can carry, not one manager's business.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pcswitcher.redaction import redact_credentials

__all__ = [
    "DiffAction",
    "DiffClass",
    "ItemClass",
    "ItemDiff",
    "Machines",
    "build_version_mismatch_detail",
]


@dataclass(frozen=True)
class Machines:
    """The two machines' own names, for every string a user reads.

    Source and target are ROLES this run assigns; they are not what the user calls the two
    computers in front of them, and a review line that says "the target" makes the reader
    translate before they can decide. Carried as one value rather than two parameters because
    every detail builder that needs one usually needs both, and because a builder that takes
    it is built once per job from `JobContext`, so no call site can pair the two names up
    itself and get them the wrong way round.
    """

    source: str
    target: str


class ItemClass(StrEnum):
    """The full ADR-020 item-class taxonomy.

    Not every member is reviewable in every direction. `APT_SOURCE` and `APT_PIN` identify
    reviewed REMOVALS only, and `FLATPAK_REMOTE` likewise: adds and changes for all three
    are derived from the packages or refs approved from them
    (`PKG-FR-APT-IDENTITY`/`PKG-FR-PIN-ALWAYS`/`PKG-FR-FLATPAK-REMOTE-DERIVED`) and
    carry no `item_id` at all. `SNAP_CHANNEL` never becomes a standalone item; see
    `SnapItem`'s docstring. A signing key has no member here in any direction.
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
    """The full diff/conflict taxonomy. Each manager's own diff decides which members it
    can produce; the enum is the definition of what the shared review and the decision
    files can carry, not one manager's business.

    `REPO_UNAVAILABLE` and `ORIGIN_MISMATCH` are the two provenance members (`PKG-FR-APT-IDENTITY`)
    and are about WHERE an item comes from, not whether it is present:

    - `REPO_UNAVAILABLE` — the source's origin cannot be provided on the target, so the
      item is reported rather than installed from somewhere else. It is a statement about
      provenance, NOT "apt printed `Candidate: (none)`" — that says nothing about where a
      package comes from and reads as unavailable for packages the target could install.
    - `ORIGIN_MISMATCH` — present on both machines, from two different vendors. A real
      divergence a presence-and-version diff cannot see.

    There is deliberately no `HELD_OR_PINNED` member. A hold replicates as its own
    `apt:hold:`/`snap:hold:` membership item, and a pin's only effect — which origin wins —
    is read back off the target after the refresh (`PKG-FR-APT-ORIGIN-VERIFY`) rather than echoed onto
    every package a pin file happens to name. Such an echo makes a target-only package
    named by any pin impossible to remove: `REPORT_ONLY` outranks its own
    `EXTRA_ON_TARGET`/`REMOVE` diff, and a report-only item cannot be skipped-always
    either.
    """

    MISSING_ON_TARGET = "missing_on_target"
    EXTRA_ON_TARGET = "extra_on_target"
    VERSION_MISMATCH = "version_mismatch"
    REPO_UNAVAILABLE = "repo_unavailable"
    ORIGIN_MISMATCH = "origin_mismatch"
    UNREPRODUCIBLE = "unreproducible"


class DiffAction(StrEnum):
    """The concrete converge verb a diff implies (`PKG-FR-SKIP-ONCE`'s direction-dependent "apply").

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

    This is ADR-020's "all classes flow through one pipeline" made real: regardless of
    which manager or item class produced it, `PackageSyncJob.apply()` and
    `packages.review.review_items()` only ever see `ItemDiff`/`ReviewEntry` shapes.

    Being the one shape is also why the credential rule lands here
    (`PKG-FR-CREDENTIAL-PRIVACY`): a URL a job composes into its own text — a repository's,
    a flatpak remote's — is redacted at construction instead of at each of the dozen places
    that build a detail string, and `label` reaches the decision file on disk already
    redacted. A file body shown whole for a decision never passes through here;
    `packages.review.ReviewEntry` is that exit. `item_id` is left alone: it is the item's
    stable identity across runs and is what a recorded decision is keyed on, so rewriting it
    would make that decision unfindable.
    """

    item_class: ItemClass
    diff_class: DiffClass
    action: DiffAction
    item_id: str
    label: str
    detail: str | None = None
    # `(act, skip now)` for a screen that asks about this item alone, where the answers name
    # THIS item's own change. A collateral package is the case: "install sl on nomad, so
    # fortunes is removed as well" is not something a screen-wide legend can say, and the
    # next item may be a downgrade caused by something else entirely. `act_word` is that
    # screen's verb for the same reason — one group's items can be removals and downgrades
    # at once, so the group's own verb would be wrong for half of them.
    answer_hints: tuple[str, str] | None = None
    act_word: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", redact_credentials(self.label))
        if self.detail is not None:
            object.__setattr__(self, "detail", redact_credentials(self.detail))
        if self.answer_hints is not None:
            object.__setattr__(self, "answer_hints", tuple(redact_credentials(h) for h in self.answer_hints))


def build_version_mismatch_detail(source_version: str, target_version: str, machines: Machines) -> str:
    """Detail string for a `VERSION_MISMATCH` diff: both versions, machine-labelled.

    Showing both versions in the review text is what makes `PKG-FR-VERSION-FLOAT`'s "detected and
    reported, never force-downgraded" promise visible to the user — nothing here
    proposes a resolution, it names the two facts and leaves the decision alone.
    """
    return f"{machines.source} has {source_version}, {machines.target} has {target_version}"
