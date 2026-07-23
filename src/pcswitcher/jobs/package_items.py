"""Item model for the package-sync pipeline (D-02, ADR-020).

Item identity is the primary key for the whole subsystem: every package, apt source,
signing key, pin, config file, snap, snap channel, flatpak ref, flatpak remote and
unreproducible install this phase handles is identified by a stable `item_id` string,
not by any manager-specific value. A package name, a flatpak ref, a filename — each is
a FIELD of one variant's dataclass, never the primary key itself. This is what lets the
shared diff/review/decision-file pipeline in `package_sync_core.py` operate on one shape
(`ItemDiff`) regardless of which manager or item class produced it.

This module is the phase's single registry of item shapes: the manager jobs built in
plans 02-07 (snap_sync), 02-08 and 02-09 (flatpak_sync, unreproducible-item detection)
import `SnapItem`, `FlatpakItem`, `FlatpakRemoteItem` and `UnreproducibleItem` from here
and add none of their own. Those three plans are written and run in parallel — a shape
defined in three places is a shape that drifts. No plan after wave 4 modifies this
module, which is what lets 02-07/02-08/02-09 run in one wave with disjoint file sets.

`ItemClass` and `DiffClass` declare their full D-02/D-25 member sets so item types and
diff directions were always addable without reopening these enums' shape; this plan
(02-05) is where every remaining `DiffClass` member and the four remaining item
dataclasses actually arrive.

Plan 02-06 adds the four `/etc/apt/*`-adjacent item classes (`AptSourceItem`,
`AptKeyItem`, `AptPinItem`, `AptConfigItem`) and is the last plan permitted to modify
this module (02-05's own note). Each is identified by its FILENAME, not by any value
parsed from its content: RESEARCH's Pitfall 3 documents that a legacy `.list` file and
a deb822 `.sources` file can legitimately coexist in `sources.list.d` describing the
same repository (e.g. after a partial `apt modernize-sources` run), and identifying by
filename is what keeps that pair visible in the review as two distinct items rather
than one merged one — the "conflicting values" failure mode apt itself raises when both
exist stays a fact the user sees, not something this tool silently resolves for them.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Literal

if TYPE_CHECKING:
    from pcswitcher.executor import Executor

__all__ = [
    "AptConfigItem",
    "AptKeyItem",
    "AptPackageItem",
    "AptPinItem",
    "AptSourceItem",
    "DiffAction",
    "DiffClass",
    "FlatpakItem",
    "FlatpakRemoteItem",
    "HoldPinFact",
    "ItemClass",
    "ItemDiff",
    "SnapItem",
    "UnreproducibleItem",
    "build_dangling_keyring_detail",
    "build_held_or_pinned_detail",
    "build_repo_unavailable_detail",
    "build_version_mismatch_detail",
    "compare_deb_versions",
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
    APT_KEY = "apt_key"
    APT_PIN = "apt_pin"
    APT_CONFIG = "apt_config"
    SNAP = "snap"
    SNAP_CHANNEL = "snap_channel"
    FLATPAK_REF = "flatpak_ref"
    FLATPAK_REMOTE = "flatpak_remote"
    UNREPRODUCIBLE = "unreproducible"


class DiffClass(StrEnum):
    """The full D-25 conflict taxonomy — every member is producible once this plan's
    `diff_items` dispatch (`package_sync_core.py`) is filled out.
    """

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


async def compare_deb_versions(executor: Executor, left: str, right: str) -> int:
    """Compare two Debian package version strings, `sorted`-comparator convention.

    Returns negative when `left` < `right`, zero when equal, positive when `left` >
    `right`. Not hand-rolled: Debian version ordering has epoch, tilde and revision
    tie-breaking rules that are neither lexicographic nor PEP 440 — only dpkg's own
    comparator correctly ranks an epoch-bearing version like `2:1.0` above `10.0`
    (RESEARCH Don't Hand-Roll). Shells out through `executor` (never assumes a local
    `dpkg`, since the target's version may need comparing against its own dpkg) with
    `shlex.quote` on both operands (ASVS V5, T-02-01). Short-circuits to equal for
    byte-identical strings so the common "nothing changed" case costs no subprocess.
    """
    if left == right:
        return 0

    quoted_left = shlex.quote(left)
    quoted_right = shlex.quote(right)

    lt_result = await executor.run_command(f"dpkg --compare-versions {quoted_left} lt {quoted_right}")
    if lt_result.success:
        return -1

    gt_result = await executor.run_command(f"dpkg --compare-versions {quoted_left} gt {quoted_right}")
    if gt_result.success:
        return 1

    return 0


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


def build_dangling_keyring_detail(filename: str, missing_ref: str) -> str:
    """Detail string when a source file's `Signed-By:`/`signed-by=` reference resolves
    to no keyring file on the SOURCE itself (a source referencing a key nobody
    captured). Flags the source item rather than letting it be proposed for install on
    its own (D-12): a repository written without its key is a repository apt refuses on
    every subsequent operation, so surfacing the gap here is cheaper than discovering it
    as an opaque apt-get failure on the target.
    """
    return f"{filename} references keyring {missing_ref!r}, which does not exist on the source"


@dataclass(frozen=True)
class AptSourceItem:
    """One apt repository definition file under `/etc/apt/sources.list.d` (D-11).

    Identity is the FILENAME (module docstring), not the parsed repository URI: a
    legacy `.list` and a deb822 `.sources` file can coexist describing the same repo
    (RESEARCH Pitfall 3), and filename identity is what keeps that visible as two
    review entries rather than one silently merged one. `fmt` records which shape the
    file had so a converged copy preserves it — this tool never normalises one format
    into the other (that migration is explicitly deferred, see CONTEXT.md's deferred
    ideas). `keyring_refs` holds every `Signed-By:` (deb822) / `signed-by=` (legacy)
    path this file names, so the source item's dependency on its key(s) is a captured
    fact, not something re-derived by re-parsing the file at convergence time.
    """

    filename: str
    digest: str
    fmt: Literal["deb822", "list"]
    keyring_refs: tuple[str, ...] = ()

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.APT_SOURCE

    @property
    def item_id(self) -> str:
        """Stable identity string: `apt:source:<filename>`."""
        return f"apt:source:{self.filename}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs, naming the file's format so
        a reviewer can tell a `.list` repo from a `.sources` one at a glance.
        """
        return f"{self.filename} ({self.fmt})"


@dataclass(frozen=True)
class AptKeyItem:
    """One apt signing-key file (D-12), either per-repo (`/etc/apt/keyrings`) or
    legacy global-trust (`/etc/apt/trusted.gpg.d`).

    `scope` keeps the two populations distinct even though both are "a key file":
    a per-repo key is referenced by exactly the source item(s) whose `keyring_refs`
    name its path, while a global-trust key is referenced by none (RESEARCH: apt-key
    deprecated Ubuntu 20.10+, but existing `trusted.gpg.d` entries still work and are
    copied verbatim, never migrated). Keys always travel byte-for-byte — this item
    never re-fetches or re-derives key content, only compares/transfers the bytes the
    source machine already has.
    """

    filename: str
    digest: str
    scope: Literal["per-repo", "global-trust"]

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.APT_KEY

    @property
    def item_id(self) -> str:
        """Stable identity string: `apt:key:<scope>:<filename>`."""
        return f"apt:key:{self.scope}:{self.filename}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.filename} ({self.scope} key)"


@dataclass(frozen=True)
class AptPinItem:
    """One apt pin-preference file under `/etc/apt/preferences.d` (D-13).

    Diffed by whole-file digest, not by parsed stanza (RESEARCH's alternatives table
    recommends whole-file for v1; CONTEXT.md leaves the choice to discretion) —
    `pinned_packages` is populated by whichever caller parses the file's content (this
    module's diff-time capture, or `AptSyncJob.collect_hold_pin_facts`'s own read) and
    stays empty when only the digest was needed to decide there was no diff at all.
    """

    filename: str
    digest: str
    pinned_packages: tuple[str, ...] = ()

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.APT_PIN

    @property
    def item_id(self) -> str:
        """Stable identity string: `apt:pin:<filename>`."""
        return f"apt:pin:{self.filename}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return self.filename


@dataclass(frozen=True)
class AptConfigItem:
    """One apt behavior-configuration file under `/etc/apt/apt.conf.d` (D-13).

    Synced as an opaque item — whole-file digest only, no parsing of apt's config
    grammar — since these files are plain, hand-authored `Acquire::.../APT::...`
    stanzas with no sub-item this phase needs to address individually.
    """

    filename: str
    digest: str

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.APT_CONFIG

    @property
    def item_id(self) -> str:
        """Stable identity string: `apt:config:<filename>`."""
        return f"apt:config:{self.filename}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return self.filename


@dataclass(frozen=True)
class SnapItem:
    """One installed snap (D-06): name, tracked channel, and installed revision.

    `channel` is a FIELD of the snap item, not a standalone item class:
    `ItemClass.SNAP_CHANNEL` is reserved for the diff DETAIL on a channel-only change
    (retracking with no revision change) and never becomes a standalone item — a
    channel with no snap attached to it has no meaning of its own.
    """

    name: str
    channel: str
    revision: str

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.SNAP

    @property
    def item_id(self) -> str:
        """Stable identity string: `snap:<name>`."""
        return f"snap:{self.name}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.name} ({self.channel}, revision {self.revision})"


@dataclass(frozen=True)
class FlatpakItem:
    """One installed flatpak application ref (D-06), scoped user or system.

    `scope` lives inside the identity string, not just as a field: this project's own
    machine has several runtimes installed in both scopes with the same application
    id, and folding scope into `item_id` is what makes "same name, different scope"
    fall out of the generic diff engine as two distinct items with no special-casing
    in `flatpak_sync`.
    """

    application: str
    version: str
    origin: str
    scope: Literal["user", "system"]

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.FLATPAK_REF

    @property
    def item_id(self) -> str:
        """Stable identity string: `flatpak:ref:<scope>:<application>`."""
        return f"flatpak:ref:{self.scope}:{self.application}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.application} ({self.version}, {self.origin}, {self.scope})"


@dataclass(frozen=True)
class FlatpakRemoteItem:
    """One configured flatpak remote (D-11/D-14), scoped user or system.

    Flatpak tracks remotes per-installation: `flathub` commonly exists in both scopes
    with a byte-identical URL, yet the two are separate configuration the target must
    provision separately. `scope` inside `item_id` (same reasoning as `FlatpakItem`)
    is what keeps those two facts distinct rather than colliding on the shared name.
    """

    name: str
    url: str
    scope: Literal["user", "system"]

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.FLATPAK_REMOTE

    @property
    def item_id(self) -> str:
        """Stable identity string: `flatpak:remote:<scope>:<name>`."""
        return f"flatpak:remote:{self.scope}:{self.name}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.name} remote ({self.scope}): {self.url}"


@dataclass(frozen=True)
class UnreproducibleItem:
    """One item no package manager can reproduce (D-18): an apt package with no repo
    candidate, or an unowned install under `/usr/local`/`/opt`.

    `origin` distinguishes how the item was found — `apt-no-candidate` (a name that
    exists but has nothing to install from) versus `unowned-path` (a filesystem path
    dpkg does not claim) — and lives inside `item_id` for the same reason `scope`
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
