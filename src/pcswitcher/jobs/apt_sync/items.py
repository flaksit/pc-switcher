"""What an apt item IS: the five shapes this job converges, their stable identities, and
where under `/etc/apt` each one's file lives.

Identity and location are one fact, which is why they share a module: `apt:pin:99-foo`
means "the file `/etc/apt/preferences.d/99-foo`", and a split between the two halves would
let an id exist that resolves to no path. Everything here is pure — no command, no I/O, no
decision. `packages/items.py` keeps the taxonomy every manager is keyed on
(`ItemClass`/`DiffClass`/`DiffAction`); these are apt's own shapes, which only this job
constructs (D-15).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, NamedTuple

from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff

# -- `/etc/apt` locations --------------------------------------------------------------

# The one start point the source-file scan walks: the only `/etc/apt` path whose existence
# is implied by apt existing at all, which `validate()` has already established, so a `find`
# rooted there has an unambiguous exit code (ADR-022).
APT_ROOT_DIR = "/etc/apt"

# The five `/etc/apt/*` directories D-11/D-13 pull into scope, each captured with one
# batched `sha256sum` listing (never one command per file).
APT_SOURCES_DIR = "/etc/apt/sources.list.d"
# The only two extensions apt reads in `sources.list.d`. Everything else there — the
# `.save` and `.curtin.orig` copies Ubuntu's own tooling leaves behind (four of them on the
# development machine) — is invisible to apt, so offering one as a syncable item would ask
# the user about a file that changes nothing.
APT_SOURCE_EXTENSIONS = (".list", ".sources")
# apt's other source location. It is scanned for keyring references, because a keyring named
# only here is still in use and deleting it would break apt — the clearest instance of "a
# source file this tool does not sync still counts as a reference" — and its digest is
# captured on both machines, which is what ADR-020 D-38's write-when-missing/overwrite-when-
# different rule compares. It is never a removal candidate in any direction.
APT_SOURCES_LIST = "/etc/apt/sources.list"
# The distribution's own source files in `sources.list.d` (ADR-020 D-38). Exact names, not
# a `ubuntu-esm-*` glob: a glob would also swallow a file a user happened to name
# `ubuntu-esm-mine.sources`, and the set is short enough to enumerate.
DISTRO_SOURCE_FILENAMES = frozenset({"ubuntu.sources", "ubuntu-esm-apps.sources", "ubuntu-esm-infra.sources"})
# The subset of the above that only a Pro-attached machine can actually fetch from, and so
# the only two files in the always-sync bucket that are gated on the target's attachment
# (D-38). Measured: `esm.ubuntu.com` serves its INDEX publicly, so an unattached target's
# `apt-get update` still exits 0 and the ESM suites enter candidate selection at priority
# 500 — above `noble/universe`. Only the pool is 401, so the failure surfaces much later,
# as `apt-get install` exiting 100 on the `.deb`, which no user connects back to a sync.
ESM_SOURCE_FILENAMES = frozenset({"ubuntu-esm-apps.sources", "ubuntu-esm-infra.sources"})
# The filenames whose URIs count as DISTRIBUTION ORIGINS, computed per machine (D-35): a
# package apt serves from one of these is served by the distribution, not by a vendor, so
# two machines pointed at different Ubuntu mirrors must not read as two different vendors.
# `/etc/apt/sources.list` joins the set by basename, which is how the reference scan keys it.
DISTRIBUTION_ORIGIN_FILENAMES = DISTRO_SOURCE_FILENAMES | {Path(APT_SOURCES_LIST).name}
APT_KEYRINGS_DIR = "/etc/apt/keyrings"
APT_TRUSTED_GPG_DIR = "/etc/apt/trusted.gpg.d"
# The third key directory (package docstring). Not an `/etc/apt` path at all, which is why
# it was missed: it is where `add-apt-repository`, Ubuntu's own `ubuntu.sources`/Pro
# sources and most vendor `.deb`s put the keyring their `Signed-By:` names.
APT_SHARED_KEYRINGS_DIR = "/usr/share/keyrings"
# The three directories a `Signed-By:` reference is resolved against, in the order a
# basename lookup consults them.
KEY_DIRS = (APT_KEYRINGS_DIR, APT_TRUSTED_GPG_DIR, APT_SHARED_KEYRINGS_DIR)
APT_PREFERENCES_DIR = "/etc/apt/preferences.d"
APT_CONF_DIR = "/etc/apt/apt.conf.d"


# -- Stable identities -----------------------------------------------------------------

# `AptPackageItem.item_id` is always this prefix + the package name. Parsing the name back
# out of the id is a legitimate use of a stable identity string, not string-matching on
# manager-specific content.
APT_PACKAGE_ID_PREFIX = "apt:package:"
APT_PIN_ID_PREFIX = "apt:pin:"
APT_SOURCE_ID_PREFIX = "apt:source:"
APT_CONFIG_ID_PREFIX = "apt:config:"
# `AptHoldItem.item_id` is always this prefix + the package name. `converge()` dispatches on
# it BEFORE the action-based package dispatch so an `apt:hold:` INSTALL never routes into
# `apt-get install` (#208, D4 — routed by prefix, never by action).
APT_HOLD_ID_PREFIX = "apt:hold:"
COLLATERAL_ID_PREFIX = "apt:collateral:"

# Identity of a repository-conflict review entry. Distinct from `apt:source:` because it is
# not the same question: `apt:source:<f>` asks whether to DELETE a file the source no longer
# has, `apt:conflict:<f>` asks which of two versions of a file both machines have should win.
# It reaches no diff and no decision file — it exists only between the review and the
# derived write set.
CONFLICT_ID_PREFIX = "apt:conflict:"

# Synthetic diff id for the one `apt-get update` this job issues per run when at least
# one source/key/pin/config item was approved. Not a real `/etc/apt` item — reuses
# `ItemClass.APT_SOURCE` so it sorts with the repo group (see `ITEM_CLASS_ORDER`) but is
# excluded from `REPO_GROUP_CLASSES` membership checks by item_id, not class.
METADATA_REFRESH_ITEM_ID = "apt:metadata-refresh"

# Item-id prefixes that may never appear in a decision file, in any direction (rulings 5
# and 12). `apt:config:` is absent on purpose — it keeps the registry.
UNRECORDABLE_ITEM_ID_PREFIXES = (APT_SOURCE_ID_PREFIX, APT_PIN_ID_PREFIX)


def package_name(item_id: str) -> str:
    if not item_id.startswith(APT_PACKAGE_ID_PREFIX):
        raise ValueError(f"Not an apt package item id: {item_id!r}")
    return item_id.removeprefix(APT_PACKAGE_ID_PREFIX)


def pin_filename(item_id: str) -> str:
    """`apt:pin:<filename>` -> `<filename>`, for looking a pin's captured content back up."""
    return item_id.removeprefix(APT_PIN_ID_PREFIX)


def hold_name(item_id: str) -> str:
    """`apt:hold:<name>` -> `<name>`."""
    return item_id.removeprefix(APT_HOLD_ID_PREFIX)


def collateral_name(item_id: str) -> str:
    """`apt:collateral:<name>` -> `<name>`."""
    return item_id.removeprefix(COLLATERAL_ID_PREFIX)


# -- apt's own item shapes -------------------------------------------------------------
#
# A shape only this job constructs is this job's business (D-15): while the package diff
# lived on `PackageSyncJob`, the other three managers inherited hold sets, pin facts and
# no-candidate ids they never fill in, and each wrote its own diff anyway -- because what
# a diff even IS differs per ecosystem.


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
        return f"{APT_PACKAGE_ID_PREFIX}{self.name}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.name} ({self.version})" if self.version else self.name


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
        return f"{APT_HOLD_ID_PREFIX}{self.name}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.name} (hold)"


@dataclass(frozen=True)
class AptSourceItem:
    """One apt repository definition file under `/etc/apt/sources.list.d` (D-11).

    Identity is the FILENAME (package docstring), not the parsed repository URI: a
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
        return f"{APT_SOURCE_ID_PREFIX}{self.filename}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs, naming the file's format so
        a reviewer can tell a `.list` repo from a `.sources` one at a glance.
        """
        return f"{self.filename} ({self.fmt})"


@dataclass(frozen=True)
class AptPinItem:
    """One apt pin-preference file under `/etc/apt/preferences.d` (D-13).

    Diffed by whole-file digest, never by parsed stanza. The package names a pin file
    mentions are deliberately not carried: under ADR-020 D-36 a pin is mechanism, and its
    only effect — which origin wins — is read back from the target's real candidate
    origins after the refresh (D-35), not predicted from the stanzas here.
    """

    filename: str
    digest: str

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.APT_PIN

    @property
    def item_id(self) -> str:
        """Stable identity string: `apt:pin:<filename>`."""
        return f"{APT_PIN_ID_PREFIX}{self.filename}"

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
        return f"{APT_CONFIG_ID_PREFIX}{self.filename}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return self.filename


# -- Convergence order ------------------------------------------------------------------

# The three repository-adjacent item classes that converge in a single ordered,
# transactional group ahead of packages — kept as one constant so the trigger check in
# `accept_review` and the group membership check in `converge` never drift. Signing keys
# are deliberately absent: they are not items at all, they are file operations this group
# brackets (package docstring).
REPO_GROUP_CLASSES = frozenset({ItemClass.APT_PIN, ItemClass.APT_CONFIG, ItemClass.APT_SOURCE})

# Convergence order is an apt FACT (a repo's metadata must be fetched before anything
# installs from it), not a general ordering concept — which is why it lives here, in the
# job, rather than as a sort the shared core imposes on every manager. Packages sort last
# (module-level default 3); pins and apt config share a rank since nothing depends on
# their relative order. Keys need no rank: keyring provisioning and collection are steps
# inside the group's own convergence, not diffs competing for a position in this sort.
ITEM_CLASS_ORDER: dict[ItemClass, int] = {
    ItemClass.APT_PIN: 1,
    ItemClass.APT_CONFIG: 1,
    ItemClass.APT_SOURCE: 2,
    # Holds converge AFTER package installs (#208, D8: install-before-hold) — rank 4,
    # behind the module-level package default (3). A hold is dpkg selection state only:
    # holding a package that this same run is installing must happen once it is present.
    ItemClass.APT_HOLD: 4,
}

# Deletion order inside the repository group (`02-SPEC-package-review-model.md` §3.3
# step 5), deliberately the reverse of the write order: the repository goes before the pin
# that prefers it, so the target never sits with a pin naming an origin apt no longer has.
REMOVAL_CLASS_ORDER: dict[ItemClass, int] = {
    ItemClass.APT_SOURCE: 1,
    ItemClass.APT_PIN: 2,
    ItemClass.APT_CONFIG: 3,
}


class RemovalVocabulary(NamedTuple):
    """How one deletable `/etc/apt` item class reads: `action_label` names the action for one
    entry, `plural` names the objects for the group title.

    Two words rather than one because the two positions are different parts of speech. A
    title composed by pluralising the action label produces "Delete repositorys" — English
    plurals do not follow from a verb phrase, and the noun is what is being counted.
    """

    action_label: str
    plural: str


# The two `/etc/apt` item classes whose ONLY remaining review direction is deletion, and
# the words each reads with (ADR-020 D-37, rulings 5 and 12). Both take two answers — delete
# or leave it for now — so both carry `REPO_REMOVAL_REVIEW_ACTION`; keeping them as two
# entries is what gives the user two separate screens rather than one mixed list.
# `remove`, not "delete repository"/"delete pin file": the action label is also the word in
# the decision column, where the group title above it already says what is being deleted, so
# the longer phrase said the noun once per row and pushed the column halfway across the
# screen. The title keeps the noun; the answer keeps the verb.
REPO_REMOVAL_VERBS: dict[ItemClass, RemovalVocabulary] = {
    ItemClass.APT_SOURCE: RemovalVocabulary("remove", "repositories"),
    ItemClass.APT_PIN: RemovalVocabulary("remove", "pin files"),
}


# -- Where an item's file lives ---------------------------------------------------------


def repo_item_destination(diff: ItemDiff) -> str:
    """The absolute `/etc/apt/...` path a repository-group diff's item_id names.

    Parses the item_id rather than needing the original item object at converge time
    (the plan only carries `ItemDiff`s, not the richer dataclasses) — a legitimate use
    of a stable identity string per the existing `package_name` precedent.
    """
    if diff.item_class == ItemClass.APT_SOURCE:
        return f"{APT_SOURCES_DIR}/{diff.item_id.removeprefix(APT_SOURCE_ID_PREFIX)}"
    if diff.item_class == ItemClass.APT_PIN:
        return f"{APT_PREFERENCES_DIR}/{diff.item_id.removeprefix(APT_PIN_ID_PREFIX)}"
    if diff.item_class == ItemClass.APT_CONFIG:
        return f"{APT_CONF_DIR}/{diff.item_id.removeprefix(APT_CONFIG_ID_PREFIX)}"
    raise AssertionError(f"not a repository-group item class: {diff.item_class!r}")


def source_file_destination(filename: str) -> str:
    """The absolute path a source-file scan entry names.

    The scan keys by BASENAME across `sources.list.d` and `/etc/apt/sources.list`, so the
    one entry that is not a `sources.list.d` member has to be mapped back by name. A file a
    user genuinely put at `sources.list.d/sources.list` would collide with it; apt reads
    both, and disambiguating a case nobody has is not worth a second scan shape.
    """
    return APT_SOURCES_LIST if filename == Path(APT_SOURCES_LIST).name else f"{APT_SOURCES_DIR}/{filename}"


def is_collateral_diff(diff: ItemDiff) -> bool:
    """A manual-collateral item, identified by its stable id prefix (D-30). These carve
    into their own `COLLATERAL_REVIEW_ACTION` group rather than a checkbox group."""
    return diff.item_id.startswith(COLLATERAL_ID_PREFIX)


def is_repo_removal_diff(diff: ItemDiff) -> bool:
    """A `/etc/apt` repository or pin DELETION — the only direction either class still
    reaches the user in, and a two-answer one (ADR-020 D-07)."""
    return diff.item_class in REPO_REMOVAL_VERBS and diff.action is DiffAction.REMOVE
