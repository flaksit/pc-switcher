"""`apt_sync`: apt package convergence — install, remove, and the full diff taxonomy
(D-01, D-03, D-04, D-07, D-24, D-25, ADR-020).

Captures the source's `apt-mark showmanual` set with `dpkg-query`-sourced versions
(never `apt list --installed` — its own manpage says the output has no stable scripting
contract), diffs it against the same query on the target into every D-25 class
(`PackageSyncJob._diff_apt_packages`), and converges the approved `INSTALL`/`REMOVE`
items via `apt-get install`/`apt-get remove`.

Bare-`.deb` packages are NOT in scope and are dropped at capture
(`_source_bare_deb_packages`). A package whose installed version comes from no configured
repository was put there with `dpkg --install`; apt cannot install it on the target, and
`manual_installs_sync` offers it as an install snippet in the same run (D-18). Both jobs
compute the predicate from the shared `packages/apt_policy.py` parser — the same test, never
a result passed between them, since D-15/D-16 keep the four jobs independent.

The ownership split has a consequence this job may not paper over: `manual_installs_sync`
has its own enable flag, and reading another job's flag is exactly the coupling D-15
forbids. So enabling `apt_sync` while disabling `manual_installs_sync` leaves these packages
replicated by nobody — silently absent rather than offered as installs that fail. Documented
for the user in `docs/jobs/package-sync.md`.

Every approved item's transaction is simulated with `apt-get --dry-run` before the real
command runs, guarding against apt silently doing more than the review showed. Collateral
effects are classified by provenance (D-30): a package the simulation would remove or
downgrade that is auto-installed (not in the target's `apt-mark showmanual` set) is apt
resolving its own dependencies and proceeds silently, while a manually-installed one is
something the user chose to have and is refused unless the user approved losing it. `plan()`
runs two BATCHED simulations (the whole install candidate set, the whole removal candidate
set — not one per-package, which would cost more than the sync itself for 150 packages) and
classifies their collateral against the target manual set, emitting a three-way
install-anyway / skip / abort review item for each manual-collateral package so the decision
is made in the batched review, never as a prompt during apply.

The same plan-time-classification rule covers the `/etc/apt` removal direction (C26): a
source file offered for deletion because the source machine no longer has it carries, in
its review `detail`, the machine-specific packages the target still installs from that
repository. Those packages are recorded skip-always, so `filter_inert` keeps them out of
the target manifest and they produce no diff of their own in any run; without this the
review shows a bare file deletion and nothing else. Disclosure, not refusal: removing a
repository whose packages are going too is legitimate, so the removal stays offered (and,
like every removal group, unticked).

A signing key is NOT an item. It has no `ItemClass`, no `item_id`, no diff, no review
entry and no decision-file identity: the user thinks in repositories and packages, and a
key is only how a repository is made to work. Keys are therefore two plain file operations
bracketing the repository group, both driven by the decisions the user already made about
SOURCES:

- `_provision_keyrings` runs BEFORE any source file is written. It copies every source
  `/etc/apt/trusted.gpg.d` key the target lacks or differs on, and the keyrings named by
  the source files this run actually writes (INSTALL and CHANGE alike — a changed source
  may point at a keyring the target has never seen). `_require_keyrings_ready` still
  refuses to write a source whose keyring did not arrive, so a repository is never written
  ahead of its key. Provisioning is ownership-aware in ONE direction: a keyring the target
  LACKS is copied whatever owns it on the source, but a keyring the target already has
  with different bytes is left alone when the target's own dpkg owns that path — the
  target's package manages that file and clobbering a distro keyring is not this job's
  business. Ownership must not gate the COPY, because a vendor `.deb` (`code`,
  `tailscale-archive-keyring`) ships both its `sources.list.d` entry and its keyring, so
  the repository the package comes from cannot be trusted until the key that package owns
  is already there; skipping package-owned keys would make that bootstrap unsatisfiable.
  A key gets no decision of its own, but it is not invisible either: the source item's
  review detail names the keys approving it would copy, so the review and the `--dry-run`
  preview both report the write (ADR-014) without turning it back into a question.

Three key directories are captured, not two: `/etc/apt/keyrings`, `/etc/apt/trusted.gpg.d`
and `/usr/share/keyrings`. The last is where `add-apt-repository`, several vendor `.deb`s
and Ubuntu itself put keyrings, and most real `Signed-By:` values point there; leaving it
out made every such reference resolve to nothing and downgraded the repository to
`REPORT_ONLY`. Like `/etc/apt/keyrings` it is provisioned only for keys a source actually
references, and unlike it, it is never collected: it is package territory.
- `_remove_unused_keyrings` runs AFTER every source write and deletion, and only when this
  run actually removed a source file. It re-scans the target's REAL source files and drops
  each `/etc/apt/keyrings` file no surviving source references. Counting against the
  post-write state is what makes the hard cases come out right: a repository this run
  deleted stops counting as a reference, while one the user left unticked, one recorded
  machine-specific, and one pc-switcher never syncs at all (`/etc/apt/sources.list`) all
  keep counting.

Legacy `/etc/apt/trusted.gpg.d` keys are replicated but never collected: they are ambient
trust with no discoverable referent, so "unused" is not computable for them and they are
allowed to accumulate rather than be deleted on a guess.

Keys travel byte-for-byte and are never re-fetched from a vendor (D-12).

A run that changes `/etc/apt` invalidates the facts its own `plan()` collected from
`/etc/apt` — most visibly a `preferences.d` pin the user is deleting, which at plan time
still suppressed its packages into `HELD_OR_PINNED`/`REPORT_ONLY`. `apply()` therefore
converges the repository group FIRST, re-derives the package facts against the target the
run just produced, and puts whatever genuinely changed through a SECOND review
(`_rereview_repo_invalidated_packages`). ADR-020 D-24's one-review-per-manager is a
preference, not a constraint: a decision the user could not have made correctly the first
time is asked again rather than deferred to the next run.

Apt sources/keys/pins/config, and the other two managers (snap, flatpak), are later
Phase 2 plans.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Literal, override
from uuid import uuid4

from pcswitcher.executor import Executor, RemoteExecutor
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.apt_policy import (
    installed_origins_by_package,
    normalise_repo_uri,
    packages_installed_from_no_repository,
    packages_with_no_candidate,
)
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
    build_version_mismatch_detail,
)
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.state import DecisionFile, filter_inert
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, LogLevel, ValidationError
from pcswitcher.sudoers import passwordless_sudo_hint

__all__ = ["AptSyncJob", "AptTransactionPreview", "simulate_apt_transaction"]

# `AptPackageItem.item_id` is always this prefix + the package name (packages/items.py).
# Parsing the name back out of the id is a legitimate use of a stable identity string,
# not string-matching on manager-specific content.
_APT_PACKAGE_ID_PREFIX = "apt:package:"

# Binaries this job runs under sudo, quoted back to the user when the passwordless-sudo
# check fails. A lower bound on what must be permitted, not an exact scope (ADR-013).
# The source is only ever read, so it needs just the /etc/apt digest capture.
_SOURCE_SUDO_COMMANDS = ("/usr/bin/find", "/usr/bin/sha256sum")
_TARGET_SUDO_COMMANDS = (
    "/usr/bin/apt-get",
    "/usr/bin/apt-mark",
    "/usr/bin/find",
    "/usr/bin/sha256sum",
    "/usr/bin/install",
    "/usr/bin/cp",
    "/usr/bin/rm",
    "/usr/bin/fuser",
)

# The five `/etc/apt/*` directories D-11/D-13 pull into scope, each captured with one
# batched `sha256sum` listing (never one command per file).
_APT_SOURCES_DIR = "/etc/apt/sources.list.d"
# The only two extensions apt reads in `sources.list.d`. Everything else there — the
# `.save` and `.curtin.orig` copies Ubuntu's own tooling leaves behind (four of them on the
# development machine) — is invisible to apt, so offering one as a syncable item would ask
# the user about a file that changes nothing.
_APT_SOURCE_EXTENSIONS = (".list", ".sources")
# apt's other source location. It is scanned for keyring references, because a keyring named
# only here is still in use and deleting it would break apt — the clearest instance of "a
# source file this tool does not sync still counts as a reference" — and its digest is
# captured on both machines, which is what ADR-021 D-38's write-when-missing/overwrite-when-
# different rule compares. It is never a removal candidate in any direction.
_APT_SOURCES_LIST = "/etc/apt/sources.list"
_APT_KEYRINGS_DIR = "/etc/apt/keyrings"
_APT_TRUSTED_GPG_DIR = "/etc/apt/trusted.gpg.d"
# The third key directory (module docstring). Not an `/etc/apt` path at all, which is why
# it was missed: it is where `add-apt-repository`, Ubuntu's own `ubuntu.sources`/Pro
# sources and most vendor `.deb`s put the keyring their `Signed-By:` names.
_APT_SHARED_KEYRINGS_DIR = "/usr/share/keyrings"
# The three directories a `Signed-By:` reference is resolved against, in the order a
# basename lookup consults them.
_KEY_DIRS = (_APT_KEYRINGS_DIR, _APT_TRUSTED_GPG_DIR, _APT_SHARED_KEYRINGS_DIR)
_APT_PREFERENCES_DIR = "/etc/apt/preferences.d"
_APT_CONF_DIR = "/etc/apt/apt.conf.d"

# The three repository-adjacent item classes that converge in a single ordered,
# transactional group ahead of packages (Task 2) — kept as one constant so the trigger
# check in `accept_review` and the group membership check in `converge` never drift.
# Signing keys are deliberately absent: they are not items at all, they are file
# operations this group brackets (module docstring).
_REPO_GROUP_CLASSES = frozenset({ItemClass.APT_PIN, ItemClass.APT_CONFIG, ItemClass.APT_SOURCE})

# Convergence order is an apt FACT (a repo's metadata must be fetched before anything
# installs from it), not a general ordering concept — which is why it lives here, in the
# job, rather than as a sort the shared core imposes on every manager. Packages sort last
# (module-level default 3); pins and apt config share a rank since nothing depends on
# their relative order. Keys need no rank: keyring provisioning and collection are steps
# inside the group's own convergence, not diffs competing for a position in this sort.
_ITEM_CLASS_ORDER: dict[ItemClass, int] = {
    ItemClass.APT_PIN: 1,
    ItemClass.APT_CONFIG: 1,
    ItemClass.APT_SOURCE: 2,
    # Holds converge AFTER package installs (#208, D8: install-before-hold) — rank 4,
    # behind the module-level package default (3). A hold is dpkg selection state only:
    # holding a package that this same run is installing must happen once it is present.
    ItemClass.APT_HOLD: 4,
}

# `AptHoldItem.item_id` is always this prefix + the package name (packages/items.py).
# `converge()` dispatches on it BEFORE the action-based package dispatch so an
# `apt:hold:` INSTALL never routes into `apt-get install` (#208, D4 — routed by prefix,
# never by action).
_APT_HOLD_ID_PREFIX = "apt:hold:"

# Synthetic diff id for the one `apt-get update` this job issues per run when at least
# one source/key/pin/config item was approved (Task 2). Not a real `/etc/apt` item —
# reuses `ItemClass.APT_SOURCE` so it sorts with the repo group (see `_ITEM_CLASS_ORDER`)
# but is excluded from `_REPO_GROUP_CLASSES` membership checks by item_id, not class.
_METADATA_REFRESH_ITEM_ID = "apt:metadata-refresh"

# Matches one `apt-get --dry-run` transaction line: `Inst <name> [<old>] (<new> ...)` for an
# install/upgrade (the `[<old>]` bracket only appears when a version is already
# installed), or `Remv <name> [<old>]` for a removal. Parsed by leading verb token and
# named groups rather than fixed column positions — the rest of an apt-get --dry-run line's
# shape varies with the package and its dependency resolution.
_TRANSACTION_LINE_RE = re.compile(
    r"^(?P<verb>Inst|Remv)\s+(?P<name>\S+)"
    r"(?:\s+\[(?P<old_version>[^\]]+)\])?"
    r"(?:\s+\((?P<new_version>[^\s)]+)\)?)?"
)


# -- apt's own item shapes, detail strings and package diff ---------------------------
#
# All of it here, none of it in `packages/`. A shape only this job constructs and a diff
# only this job runs are this job's business (D-15): while the package diff lived on
# `PackageSyncJob`, the other three managers inherited hold sets, pin facts and
# no-candidate ids they never fill in, and each wrote its own diff anyway -- because what
# a diff even IS differs per ecosystem. `packages/items.py` keeps the taxonomy every
# manager is keyed on; `packages/sync_core.py` keeps the plan/review/apply order.


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


def _diff_apt_packages(
    source_items: Sequence[AptPackageItem],
    target_items: Sequence[AptPackageItem],
    hold_pin_facts: Sequence[HoldPinFact],
    unavailable_item_ids: frozenset[str],
    source_hold_names: frozenset[str] = frozenset(),
    target_hold_names: frozenset[str] = frozenset(),
) -> list[ItemDiff]:
    """One diff per item id present on either side, source-then-target order,
    followed by the `apt:hold:` membership diffs (#208, D5/D8 — holds emitted AFTER
    package diffs so install lands before its hold once the diffs converge).

    Precedence per package id: a PINNED package (present on the target and named by a
    pin fact) yields `HELD_OR_PINNED`/`REPORT_ONLY` — the pin echo D-25 requires,
    informational only. A HELD package (target hold set) has its install/upgrade
    action SUPPRESSED (a held package is never proposed for install/version change)
    but produces NO package-level report — the hold travels as its own `apt:hold:`
    item, so a held package is never double-reported. Otherwise: missing-on-target ->
    `REPO_UNAVAILABLE` if apt reports no candidate, else `MISSING_ON_TARGET`/`INSTALL`;
    extra-on-target -> `EXTRA_ON_TARGET`/`REMOVE`; present on both with differing
    versions -> `VERSION_MISMATCH`/`REPORT_ONLY` (D-04: reported, never
    force-downgraded); present on both with equal versions -> no diff at all.

    Hold membership (D2): source-held & target-not -> `AptHoldItem` INSTALL (hold);
    target-held & source-not -> REMOVE (unhold); held on both or neither -> no diff.
    """
    source_by_id = {item.item_id: item for item in source_items}
    target_by_id = {item.item_id: item for item in target_items}

    # PINS only (#208, D5): holds no longer key this guard — they travel as their own
    # `apt:hold:` items below, so this branch keys purely on the pin mechanism.
    pinned: dict[str, HoldPinFact] = {}
    for fact in hold_pin_facts:
        if fact.mechanism == "pin":
            pinned.setdefault(fact.package, fact)

    seen: dict[str, None] = {}
    for item in (*source_items, *target_items):
        seen.setdefault(item.item_id, None)

    diffs: list[ItemDiff] = []
    for item_id in seen:
        source_item = source_by_id.get(item_id)
        target_item = target_by_id.get(item_id)

        if target_item is not None and target_item.name in pinned:
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_PACKAGE,
                    diff_class=DiffClass.HELD_OR_PINNED,
                    action=DiffAction.REPORT_ONLY,
                    item_id=item_id,
                    label=target_item.label(),
                    detail=build_held_or_pinned_detail(pinned[target_item.name]),
                )
            )
        elif target_item is not None and target_item.name in target_hold_names:
            # Held on the target: suppress its install/version action entirely (a held
            # package must never be proposed for install/upgrade). No package-level
            # report — the `apt:hold:` item below carries the hold fact.
            continue
        elif source_item is not None and target_item is None:
            if item_id in unavailable_item_ids:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_PACKAGE,
                        diff_class=DiffClass.REPO_UNAVAILABLE,
                        action=DiffAction.REPORT_ONLY,
                        item_id=item_id,
                        label=source_item.label(),
                        detail=build_repo_unavailable_detail(source_item.name),
                    )
                )
            else:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_PACKAGE,
                        diff_class=DiffClass.MISSING_ON_TARGET,
                        action=DiffAction.INSTALL,
                        item_id=item_id,
                        label=source_item.label(),
                        detail=None,
                    )
                )
        elif target_item is not None and source_item is None:
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_PACKAGE,
                    diff_class=DiffClass.EXTRA_ON_TARGET,
                    action=DiffAction.REMOVE,
                    item_id=item_id,
                    label=target_item.label(),
                    detail=None,
                )
            )
        elif source_item is not None and target_item is not None and source_item.version != target_item.version:
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_PACKAGE,
                    diff_class=DiffClass.VERSION_MISMATCH,
                    action=DiffAction.REPORT_ONLY,
                    item_id=item_id,
                    label=target_item.label(),
                    detail=build_version_mismatch_detail(source_item.version, target_item.version),
                )
            )
        # else: present on both, equal versions, not held/pinned -> no diff.

    # Hold membership diffs (#208, D2/D8): emitted AFTER every package diff so a
    # package install lands before its hold when both are approved.
    diffs.extend(_diff_apt_holds(source_hold_names, target_hold_names))
    return diffs


def _diff_apt_holds(source_hold_names: frozenset[str], target_hold_names: frozenset[str]) -> list[ItemDiff]:
    """`apt:hold:` membership diffs (#208, D2): source-held & target-not -> INSTALL
    (hold); target-held & source-not -> REMOVE (unhold); held on both or on neither
    -> no diff. `sorted` for a stable, deterministic review order.
    """
    diffs: list[ItemDiff] = []
    for name in sorted(source_hold_names | target_hold_names):
        in_source = name in source_hold_names
        in_target = name in target_hold_names
        if in_source == in_target:
            continue
        hold_item = AptHoldItem(name=name)
        diffs.append(
            ItemDiff(
                item_class=ItemClass.APT_HOLD,
                diff_class=DiffClass.MISSING_ON_TARGET if in_source else DiffClass.EXTRA_ON_TARGET,
                action=DiffAction.INSTALL if in_source else DiffAction.REMOVE,
                item_id=hold_item.item_id,
                label=hold_item.label(),
                detail=None,
            )
        )
    return diffs


# -- the `/etc/apt/*` item shapes and their review details ----------------------------


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


def build_dangling_keyring_detail(filename: str, missing_ref: str) -> str:
    """Detail string when a source file's `Signed-By:`/`signed-by=` reference resolves
    to no keyring file on the SOURCE itself (a source referencing a key nobody
    captured). Flags the source item rather than letting it be proposed for install on
    its own (D-12): a repository written without its key is a repository apt refuses on
    every subsequent operation, so surfacing the gap here is cheaper than discovering it
    as an opaque apt-get failure on the target.
    """
    return f"{filename} references keyring {missing_ref!r}, which does not exist on the source"


def build_orphaned_packages_detail(source_filename: str, packages: Sequence[str]) -> str:
    """Detail string for an apt source-file REMOVE diff whose removal would leave
    machine-specific packages on the target without the repository that feeds them (C26).

    Those packages are the ones a review can never show by itself: recorded skip-always,
    they are filtered out of the target manifest before diffing (D-08), so they produce no
    `ItemDiff` in any run. Naming them here is the only place the user learns that
    approving the source deletion strands software they explicitly told this tool to keep.
    Disclosure, not refusal — D-30's placement, the same as flatpak's orphaned refs.
    """
    return (
        f"machine-specific packages on the target installed from {source_filename}: "
        f"{', '.join(packages)} (removal leaves them without updates)"
    )


def _package_name(item_id: str) -> str:
    if not item_id.startswith(_APT_PACKAGE_ID_PREFIX):
        raise ValueError(f"Not an apt package item id: {item_id!r}")
    return item_id.removeprefix(_APT_PACKAGE_ID_PREFIX)


def _lines(output: str) -> list[str]:
    """Non-blank, stripped lines — the shape every `apt-mark`/`find` list command in
    this module produces."""
    return [line.strip() for line in output.splitlines() if line.strip()]


# -- Repository/key/pin/config capture and diff (D-11, D-12, D-13) ---------------------
#
# Unlike apt packages, these five directories are diffed by whole-FILE digest (module
# docstring, RESEARCH's alternatives table): one batched `sha256sum` listing per
# directory tells us which filenames differ without transferring a single byte, and the
# full content of a file is only fetched for the files a diff actually implicates
# (missing-on-target, extra-on-target, or digest-mismatched) — never for a file that is
# already identical on both machines.

_SIGNED_BY_RE = re.compile(r"^Signed-By:\s*(?P<path>\S+)", re.IGNORECASE)
_LEGACY_SIGNED_BY_RE = re.compile(r"signed-by=(?P<path>[^\]\s,]+)")
_PIN_PACKAGE_RE = re.compile(r"^Package:\s*(?P<packages>.+)$", re.IGNORECASE)


def _keyring_reference(value: str) -> str | None:
    """`value` as a keyring PATH reference, or `None` when it is not one.

    A `Signed-By:` field carries either a path or an INLINE armored key. Only an absolute
    path is a reference to a file that has to exist; anything else is the armored block
    itself and needs no provisioning, because it travels inside the source file. The
    distinction is not cosmetic: `add-apt-repository` writes the armor's first line on the
    field line (`Signed-By: -----BEGIN PGP PUBLIC KEY BLOCK-----`), so a bare `\\S+` capture
    turns every PPA added the normal way into a reference to a file named `-----BEGIN`,
    which resolves nowhere and downgrades the repository to `REPORT_ONLY`.
    """
    return value if value.startswith("/") else None


def _pin_packages(line: str) -> list[str]:
    """The package names one `preferences.d` `Package:` stanza line pins.

    A stanza may name several packages space-separated. `Package: *` (and any other glob)
    is dropped: apt matches it against every package, but pc-switcher's consumers key facts
    by exact installed-package name, so a fact for the literal string `*` matches nothing
    and is only noise in the review.
    """
    match = _PIN_PACKAGE_RE.match(line.strip())
    if match is None:
        return []
    return [name for name in match.group("packages").split() if "*" not in name]


# A deb822 stanza's repository URIs (one field, possibly several space-separated values,
# and one file may hold several stanzas), and a legacy `.list` line's single URI — which
# sits after the optional `[opt=val ...]` bracket, so the bracket must be consumed rather
# than treated as the URI.
_URIS_RE = re.compile(r"^URIs:\s*(?P<uris>.+)$", re.IGNORECASE)
_LEGACY_DEB_LINE_RE = re.compile(r"^deb(?:-src)?\s+(?:\[[^\]]*\]\s*)?(?P<uri>\S+)")


def _parse_sha256sum(output: str) -> dict[str, str]:
    """`<digest>  <path>` lines (one per `sha256sum` invocation) -> `{basename: digest}`.

    Basename, not the full path: every caller already knows which directory it asked
    about, and item identity is the filename (module docstring), not the path.
    """
    digests: dict[str, str] = {}
    for line in _lines(output):
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest, path = parts
        digests[Path(path).name] = digest
    return digests


def _parse_source_file(
    filename: str, content: str
) -> tuple[Literal["deb822", "list"], tuple[str, ...], tuple[str, ...]]:
    """A source file's format (by extension), every keyring path it names, and every
    repository URI it points at (normalised by `normalise_repo_uri`).

    deb822 `.sources` files name a key via a `Signed-By:` field and their repositories via
    `URIs:`; legacy `.list` files put both on the `deb` line, the key inside the options
    bracket as `[... signed-by=<path> ...]` and the URI immediately after it (RESEARCH
    Standard Stack). Parsed just far enough to extract these — never rewritten,
    normalised, or migrated between formats (RESEARCH Pitfall 3, deferred ideas).

    One parser, three consumers: the keyring refs drive D-12's dangling-reference check
    and keyring garbage collection, the URIs drive the source-removal impact (C26) by
    matching against the origin `apt-cache policy` reports for an installed package.

    A `Signed-By:` field may carry an INLINE armored key instead of a path, either with an
    empty field value and the block on continuation lines or with the block's first line on
    the field line itself. Neither yields a ref (`_keyring_reference`), which is correct in
    both directions: the file depends on no key FILE, so nothing is invented for it, and no
    part of the armored block is mistaken for a path.
    """
    fmt: Literal["deb822", "list"] = "deb822" if filename.endswith(".sources") else "list"
    refs: list[str] = []
    uris: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if fmt == "deb822":
            signed_by = _SIGNED_BY_RE.match(line)
            uri_field = _URIS_RE.match(line)
            if uri_field:
                uris.extend(normalise_repo_uri(uri) for uri in uri_field.group("uris").split())
        else:
            signed_by = _LEGACY_SIGNED_BY_RE.search(raw_line)
            deb_line = _LEGACY_DEB_LINE_RE.match(line)
            if deb_line:
                uris.append(normalise_repo_uri(deb_line.group("uri")))
        if signed_by:
            ref = _keyring_reference(signed_by.group("path"))
            if ref is not None:
                refs.append(ref)
    return fmt, tuple(refs), tuple(uris)


def _parse_pin_file(content: str) -> tuple[str, ...]:
    """Every package name named by a `Package:` stanza line in a `preferences.d` file.

    Shares `_pin_packages` with `collect_hold_pin_facts`, which reads the same lines off
    the target: two parsers of one file format is two chances to disagree about what is
    pinned, and they did — the awk one-liner used to keep `$2` alone and so lost every name
    after the first in `Package: foo bar`.
    """
    return tuple(name for line in content.splitlines() for name in _pin_packages(line))


def _dangling_keyring_ref(keyring_refs: Sequence[str], source_key_filenames: frozenset[str]) -> str | None:
    """The first `keyring_refs` entry whose basename is absent from
    `source_key_filenames`, or `None` if every reference resolves to a real file on the
    source. A source file with no `Signed-By:`/`signed-by=` at all (`keyring_refs` is
    empty) has nothing to validate — it is not itself a dangling reference.

    `source_key_filenames` spans all three key directories (`_KEY_DIRS`), so a reference is
    dangling only when the source machine really has no such key — not merely when it keeps
    it somewhere this job did not think to look.
    """
    for ref in keyring_refs:
        if Path(ref).name not in source_key_filenames:
            return ref
    return None


async def _capture_dir_digests(
    run: Callable[[str], Awaitable[CommandResult]],
    directory: str,
    *,
    extensions: Sequence[str] = (),
) -> dict[str, str]:
    """One `sudo find <dir> -maxdepth 1 -type f -exec sha256sum {} +` per directory —
    a single batched command, never one `sha256sum` per file. `-exec ... {} +` never
    runs at all when the directory has no matching files, so an empty/absent directory
    degrades to an empty digest map rather than a shell error.

    `extensions` narrows the capture to the files apt itself reads, and is only correct
    where apt HAS such a rule: `sources.list.d` is read for `*.list`/`*.sources` alone, so
    the `.save`/`.curtin.orig` copies apt ignores must not become syncable items.
    `preferences.d` and `apt.conf.d` pass no extensions, because apt reads extensionless
    files in both.
    """
    quoted = shlex.quote(directory)
    predicate = ""
    if extensions:
        names = " -o ".join(f"-name {shlex.quote(f'*{ext}')}" for ext in extensions)
        predicate = f"\\( {names} \\) "
    result = await run(f"sudo find {quoted} -maxdepth 1 -type f {predicate}-exec sha256sum {{}} +")
    return _parse_sha256sum(result.stdout)


async def _capture_file_digest(run: Callable[[str], Awaitable[CommandResult]], path: str) -> str | None:
    """One `sudo sha256sum <path>`, or `None` when the file is absent.

    The single-file counterpart to `_capture_dir_digests`, for `/etc/apt/sources.list`,
    which is a file rather than a directory and so has no `find` listing to appear in.
    Verified: `sha256sum` on a missing path exits 1 and writes nothing to stdout, so the
    absent case falls out of the parse rather than needing a probe of its own.
    """
    result = await run(f"sudo sha256sum {shlex.quote(path)}")
    return _parse_sha256sum(result.stdout).get(Path(path).name)


async def _read_file_content(run: Callable[[str], Awaitable[CommandResult]], path: str) -> str:
    """One `sudo cat <path>` — used only for a file a diff actually implicates.

    `sudo`-qualified to match `_capture_dir_digests`'s `sudo find ... sha256sum`
    privilege (WR-04): an unprivileged `cat` on a source file locked down to
    `0600`-or-similar would silently return empty stdout instead of failing, while
    the digest capture (root) still sees it and proposes a diff — an `AptSourceItem`
    parsed from that empty content would find zero `keyring_refs`, so a dangling key
    reference this run never actually validated would go undetected.
    """
    result = await run(f"sudo cat {shlex.quote(path)}")
    return result.stdout


async def _scan_source_file_references(
    run: Callable[[str], Awaitable[CommandResult]],
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """`{filename: (keyring_refs, repository URIs)}` for EVERY source file on a machine,
    from ONE batched command — `sources.list.d` AND `/etc/apt/sources.list`.

    Machine-agnostic by construction (it takes the `run` callable and names no host), and
    run against BOTH machines: the target's answer drives the two consumers below, the
    source's answer is what maps a package's origin URIs back to the repository file that
    would have to travel for it (ADR-021 D-34).

    Two target-side consumers, both of which need a fact no diff carries. The source-removal
    impact (C26) needs the repository URIs of a file whose deletion is offered. Keyring
    garbage collection needs the reference count of a key across every source file that
    exists, which is emphatically not the set of files any diff implicates: a keyring is
    commonly named only by files that are byte-identical on both machines, or that the user
    marked machine-specific, or — `/etc/apt/sources.list` — that pc-switcher never syncs at
    all. Missing any of those would delete a key that is still in use.

    Deliberately unfiltered by extension, unlike `_capture_dir_digests`' `sources.list.d`
    capture: a keyring named only by a file apt ignores is still a key nothing else
    references, and keeping it is cheaper than deleting one that turns out to be in use.

    `find ... -exec awk {} +` passes every file to one awk process (the `collect_hold_pin_facts`
    shape), and awk emits only the `URIs:`/`Signed-By:`/`deb` lines rather than whole
    files — so this stays compatible with the module docstring's rule that full content is
    fetched only for a file a diff implicates. `sudo`-qualified to match
    `_capture_dir_digests`'s privilege (WR-04): an unprivileged read of a locked-down
    source file returns empty output rather than failing, which would silently report no
    dependency where one exists.
    """
    awk = (
        r"tolower($0) ~ /^uris:/ || tolower($0) ~ /signed-by/ || tolower($0) ~ /^[ \t]*deb(-src)?[ \t]/ "
        r'{print FILENAME "\t" $0}'
    )
    # Both paths in ONE `find`: a missing `/etc/apt/sources.list` makes find complain on
    # stderr about that path alone and still walk the directory, so the scan degrades to
    # "no references from that file" rather than failing.
    paths = f"{shlex.quote(_APT_SOURCES_DIR)} {shlex.quote(_APT_SOURCES_LIST)}"
    result = await run(f"sudo find {paths} -maxdepth 1 -type f -exec awk {shlex.quote(awk)} {{}} +")
    lines_by_file: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        path, tab, rest = line.partition("\t")
        if tab:
            lines_by_file.setdefault(Path(path).name, []).append(rest)
    parsed: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for filename, lines in lines_by_file.items():
        _fmt, refs, uris = _parse_source_file(filename, "\n".join(lines))
        parsed[filename] = (refs, uris)
    return parsed


@dataclass(frozen=True)
class _FilenameDiff:
    """Filename-level classification of two `{filename: digest}` maps — the shared
    basis every one of the `/etc/apt/*` directories is compared with.
    """

    missing: frozenset[str]
    extra: frozenset[str]
    changed: frozenset[str]


def _diff_filenames(source_digests: Mapping[str, str], target_digests: Mapping[str, str]) -> _FilenameDiff:
    source_names = frozenset(source_digests)
    target_names = frozenset(target_digests)
    changed = frozenset(name for name in source_names & target_names if source_digests[name] != target_digests[name])
    return _FilenameDiff(missing=source_names - target_names, extra=target_names - source_names, changed=changed)


def _file_diff(
    item: AptPinItem | AptConfigItem, diff_class: DiffClass, action: DiffAction, *, detail: str | None = None
) -> ItemDiff:
    """One `ItemDiff` for a pin or config item — the two classes with no content-derived
    detail beyond the shared `VERSION_MISMATCH` digest wording (`AptSourceItem`'s
    dangling-keyring case is handled separately by `_diff_apt_sources` itself).
    """
    item_class = ItemClass.APT_PIN if isinstance(item, AptPinItem) else ItemClass.APT_CONFIG
    return ItemDiff(
        item_class=item_class,
        diff_class=diff_class,
        action=action,
        item_id=item.item_id,
        label=item.label(),
        detail=detail,
    )


def _diff_apt_configs(source_digests: Mapping[str, str], target_digests: Mapping[str, str]) -> list[ItemDiff]:
    """Config-file diffs — opaque, digest-only, filename identity."""
    names = _diff_filenames(source_digests, target_digests)
    diffs: list[ItemDiff] = []

    for filename in sorted(names.missing):
        item = AptConfigItem(filename=filename, digest=source_digests[filename])
        diffs.append(_file_diff(item, DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL))
    for filename in sorted(names.extra):
        item = AptConfigItem(filename=filename, digest=target_digests[filename])
        diffs.append(_file_diff(item, DiffClass.EXTRA_ON_TARGET, DiffAction.REMOVE))
    for filename in sorted(names.changed):
        item = AptConfigItem(filename=filename, digest=source_digests[filename])
        detail = build_version_mismatch_detail(source_digests[filename], target_digests[filename])
        diffs.append(_file_diff(item, DiffClass.VERSION_MISMATCH, DiffAction.CHANGE, detail=detail))
    return diffs


def _metadata_refresh_diff() -> ItemDiff:
    """The one synthetic `apt-get update` diff a run inserts (Task 2, `accept_review`)
    when at least one repository-group item was approved. Reuses `ItemClass.APT_SOURCE`
    so it naturally sorts with the repository group if this diff were ever re-sorted —
    membership in `_REPO_GROUP_CLASSES` checks EXCLUDE it by item_id, never by class,
    which is what keeps it from being treated as a real `/etc/apt` file to back up.
    """
    return ItemDiff(
        item_class=ItemClass.APT_SOURCE,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.CHANGE,
        item_id=_METADATA_REFRESH_ITEM_ID,
        label="Refresh apt package metadata (apt-get update)",
        detail=None,
    )


def _repo_item_destination(diff: ItemDiff) -> str:
    """The absolute `/etc/apt/...` path a repository-group diff's item_id names.

    Parses the item_id rather than needing the original item object at converge time
    (the plan only carries `ItemDiff`s, not the richer dataclasses) — a legitimate use
    of a stable identity string per the existing `_package_name` precedent.
    """
    if diff.item_class == ItemClass.APT_SOURCE:
        return f"{_APT_SOURCES_DIR}/{diff.item_id.removeprefix('apt:source:')}"
    if diff.item_class == ItemClass.APT_PIN:
        return f"{_APT_PREFERENCES_DIR}/{diff.item_id.removeprefix('apt:pin:')}"
    if diff.item_class == ItemClass.APT_CONFIG:
        return f"{_APT_CONF_DIR}/{diff.item_id.removeprefix('apt:config:')}"
    raise AssertionError(f"not a repository-group item class: {diff.item_class!r}")


def _backup_path_for(backup_dir: str, dest: str) -> str:
    """A stable, unique backup filename for an absolute `dest` path, flattened into
    `backup_dir` (`/etc/apt/sources.list.d/foo.list` -> `etc_apt_sources.list.d_foo.list`)
    so every backed-up file lives directly under one run-scoped directory.
    """
    return f"{backup_dir}/{dest.lstrip('/').replace('/', '_')}"


@dataclass(frozen=True)
class AptTransactionPreview:
    """The parsed result of `apt-get --dry-run <args>` — what apt says it WOULD do.

    `apt-get --dry-run` is the only honest answer to "what will this command do": apt resolves
    dependencies and conflicts at run time, so the package the user ticked and the
    transaction apt actually runs are not necessarily the same thing.

    `install_versions` maps a package apt would `Inst` to `(currently_installed_version
    | None, candidate_version)` — the currently-installed version is `None` for a fresh
    install (no `[...]` bracket in the line), present for an upgrade/downgrade. This is
    what the downgrade guard compares via `compare_deb_versions` rather than assuming
    every `Inst` line is a new install.
    """

    installs: tuple[str, ...]
    removals: tuple[str, ...]
    raw: str
    install_versions: Mapping[str, tuple[str | None, str]] = field(default_factory=dict)


async def simulate_apt_transaction(
    executor: RemoteExecutor, apt_args: str, *, login_shell: bool | None = False
) -> AptTransactionPreview:
    """Run `apt-get --dry-run <apt_args>` on `executor` and parse its Inst/Remv action lines.

    No `sudo` is needed: simulation is read-only. Raises `ConvergeItemFailed` if the
    simulation itself fails (dpkg lock contention, unmet dependencies, a transient
    apt-cache read error): a failed `apt-get --dry-run` typically prints no Inst/Remv lines,
    which would otherwise parse as an indistinguishable-from-clean empty preview and
    let both call sites proceed with a real command whose simulation was never
    actually trustworthy (WR-01) — refuse rather than silently degrade.
    """
    result = await executor.run_command(f"apt-get --dry-run {apt_args}", login_shell=login_shell)
    if not result.success:
        raise ConvergeItemFailed(f"apt-get --dry-run {apt_args} failed: {result.stderr.strip()}")
    installs: list[str] = []
    removals: list[str] = []
    install_versions: dict[str, tuple[str | None, str]] = {}
    for line in result.stdout.splitlines():
        match = _TRANSACTION_LINE_RE.match(line)
        if match is None:
            continue
        verb, name = match.group("verb"), match.group("name")
        if verb == "Inst":
            installs.append(name)
            new_version = match.group("new_version")
            if new_version is not None:
                install_versions[name] = (match.group("old_version"), new_version)
        elif verb == "Remv":
            removals.append(name)
    return AptTransactionPreview(
        installs=tuple(installs), removals=tuple(removals), raw=result.stdout, install_versions=install_versions
    )


class AptSyncJob(PackageSyncJob):
    """Converge apt packages (install missing, remove extra) after the coordinator's
    batched review, guarded by plan-time and apply-time apt transaction simulation.
    """

    name: ClassVar[str] = "apt_sync"
    manager_id: ClassVar[str] = "apt"

    # No configurable properties yet: this slice needs nothing beyond the enable flag in
    # sync_jobs. `enabled_item_classes`-style filtering is premature until more item
    # classes than APT_PACKAGE exist, so the schema is an empty object on purpose rather
    # than inventing keys with no consumer.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        # Key-file digests both machines carry, per directory, captured once by
        # `_plan_repo_diffs`. Keys are not items (module docstring), so these maps ARE the
        # whole key model: provisioning compares them to decide what to copy, the
        # readiness check consults them instead of re-probing the target, and collection
        # uses the per-repo pair to tell a key the source machine still has from one it
        # dropped. Keyed by filename, since that is what a `Signed-By:` reference resolves
        # against.
        self._source_keyrings: dict[str, str] = {}
        self._target_keyrings: dict[str, str] = {}
        self._source_global_keys: dict[str, str] = {}
        self._target_global_keys: dict[str, str] = {}
        self._source_shared_keys: dict[str, str] = {}
        self._target_shared_keys: dict[str, str] = {}
        # Absolute paths of every key file on the TARGET that the target's own dpkg owns,
        # from one batched `dpkg --search` at plan time. Provisioning consults it in one direction
        # only (module docstring): it never blocks copying a key the target LACKS, it only
        # stops a differing key the target's package manages from being overwritten.
        self._target_package_owned_keys: frozenset[str] = frozenset()
        # Absolute target paths `_provision_keyrings` successfully wrote this run. A source
        # file may only be written once every keyring it references is either already
        # byte-identical on the target or in here (`_require_keyrings_ready`).
        self._provisioned_keyrings: set[str] = set()
        # `{filename: (keyring_refs, repository URIs)}` for every source file ON THE TARGET,
        # captured once per `plan()` from one batched scan. This — not the diff — is what
        # says which keyrings matter: a keyring is commonly named only by files that are
        # byte-identical on both machines and so produce no diff at all.
        self._target_source_refs: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        # The same scan run against the SOURCE machine. Its URIs are what map a package's
        # installed-version origins back to the repository file that declares them, which
        # is the file that has to travel for that package to be installable from the same
        # place on the target (ADR-021 D-34).
        self._source_source_refs: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        # `/etc/apt/sources.list`'s digest on each machine, or None where the file is
        # absent. Captured separately from the five directories because it is a single
        # file: it has no `find` listing to appear in, and it is one of the files that is
        # written and updated but never removed (ADR-021 D-38).
        self._source_sources_list_digest: str | None = None
        self._target_sources_list_digest: str | None = None
        # `{filename: keyring_refs}` parsed from the SOURCE machine's copy of every source
        # file a diff implicates — the refs that will apply once this run writes that file,
        # which are not necessarily the refs the target's current copy names.
        self._source_keyring_refs: dict[str, tuple[str, ...]] = {}
        # Lazily computed the first time `converge()` sees a repository-group item
        # (pin/config/source, or the synthetic metadata-refresh marker): maps each
        # such diff's item_id to (succeeded, message). Populated all at once so the
        # required key-before-source write order and the transactional backup/rollback
        # happen exactly once per run, regardless of which order the base `apply()`
        # loop's per-diff `converge()` calls visit them in.
        self._repo_group_outcome: dict[str, tuple[bool, str]] | None = None
        # Resolved once per run via `echo $HOME` on the target (mirrors
        # `config_sync._copy_config_to_target`'s pattern) and cached, since every
        # repository-group file write needs the same absolute staging path.
        self._target_home: str | None = None
        # The target's `apt-mark showmanual` set, captured once in `plan()`: the single
        # source of the auto-versus-manual collateral split (D-30). A collateral package
        # the simulation would remove or downgrade is manual (the user chose it -> a
        # review item) if it is in this set, auto (apt's own dependency -> proceed
        # silently) if it is not. Consulted at plan time by `_classify_collateral` and
        # at apply time by the converge guards, which must agree.
        self._target_manual_set: frozenset[str] = frozenset()
        # The source manifest the last `_plan_packages()` diffed, after decision-file
        # filtering. Kept so the post-repository re-review can re-diff against a CHANGED
        # target without re-capturing the source, which this run never mutates.
        self._plan_source_items: tuple[AptPackageItem, ...] = ()
        # The source's raw `apt-mark showmanual` set, captured in `capture_source_items`
        # (before decision-file filtering). The SOURCE half of the collateral-protection
        # union (decision 8): a package the user manually installed on EITHER machine is
        # protected from a silent collateral removal/downgrade, so `_protected_manual_set`
        # is `target` unioned with `source` rather than target alone.
        self._source_manual_set: frozenset[str] = frozenset()
        # Metadata-refresh bookkeeping (decision 1). At most ONE `apt-get update` runs per
        # run across both refresh paths: `_metadata_refreshed` is set True by the first
        # successful refresh — whether the repository-group convergence's own `apt-get
        # update` or `_ensure_metadata_refreshed`'s pre-install refresh — so the second
        # path becomes a no-op. `_metadata_refresh_error` caches an install-path refresh
        # failure so every remaining install this run aborts on the same error WITHOUT
        # issuing a second `apt-get update` (the "at most one" guarantee holds even on the
        # failure path).
        self._metadata_refreshed: bool = False
        self._metadata_refresh_error: str | None = None
        # Package names of every manual-collateral item the user resolved install-anyway,
        # computed in `accept_review` from the collateral group's decisions. The apply-time
        # guard lets a removal/downgrade of one of these through; every other manual
        # collateral stays refused (D-30 — the last line of defence behind plan-time
        # classification).
        self._approved_collateral: frozenset[str] = frozenset()
        # Each collateral item's `item_id` -> the triggering install/remove item_ids whose
        # transaction produced it. Used in `accept_review` to translate a `skip` decision
        # on a collateral item into `SKIP_ONCE` on the installs it gates, so a declined
        # collateral cleanly leaves its triggering installs unapproved rather than failing
        # them at the apply-time guard.
        self._collateral_trigger_ids: dict[str, frozenset[str]] = {}

    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        """Manually-installed apt packages on the source, with versions (D-03), minus the
        bare-`.deb` installs `manual_installs_sync` owns.

        Also records the source's raw `apt-mark showmanual` names into
        `self._source_manual_set` — captured here, before any decision-file filtering AND
        before the bare-`.deb` exclusion, so a package the user chose to skip on the source,
        or one this job does not sync at all, still counts as source-manual for collateral
        protection (decision 8, the SOURCE half of `_protected_manual_set`). Not syncing a
        package is no reason to let apt remove it as collateral.

        The exclusion happens HERE and nowhere else: an item that never enters the manifest
        cannot become an `ItemDiff`, reach a review group, reach `_collect_plan_time_collateral`'s
        `apt-get --dry-run` simulation, or reach `collect_unavailable_item_ids`.
        """
        manual = await self.source.run_command("apt-mark showmanual")
        names = _lines(manual.stdout)
        self._source_manual_set = frozenset(names)
        bare_debs = await self._source_bare_deb_packages(names)
        items = await self._resolve_versions(manual.stdout, self.source.run_command)
        return [item for item in items if item.name not in bare_debs]

    async def _source_bare_deb_packages(self, manual_names: Sequence[str]) -> frozenset[str]:
        """The source's manual packages installed from no configured repository — put there
        by `dpkg --install` of a bare `.deb`, and therefore `manual_installs_sync`'s territory
        alone (D-18).

        Apt cannot install these anywhere: the target's repositories have never heard the
        name, so `apt-cache policy` on the target prints no block at all and the package
        would fall through to a proposed `INSTALL` that fails with "Unable to locate
        package". `manual_installs_sync` offers the same package as an install snippet in
        the same run, so leaving it in this manifest surfaces one package twice with only
        one workable answer.

        Same predicate as `manual_installs_sync`, from the same shared parser rather than a
        shared result: D-15/D-16 keep the four jobs independent, so both jobs pay one
        batched `apt-cache policy` (never one per package) instead of one importing the
        other.
        """
        if not manual_names:
            return frozenset()

        quoted = " ".join(shlex.quote(name) for name in manual_names)
        result = await self.source.run_command(f"apt-cache policy {quoted}")
        return packages_installed_from_no_repository(result.stdout, manual_names)

    async def query_target_items(self) -> Sequence[AptPackageItem]:
        """The target's own manually-installed apt packages, with versions."""
        manual = await self.target.run_command("apt-mark showmanual", login_shell=False)

        async def run(cmd: str) -> CommandResult:
            return await self.target.run_command(cmd, login_shell=False)

        return await self._resolve_versions(manual.stdout, run)

    @staticmethod
    async def _resolve_versions(
        showmanual_output: str, run: Callable[[str], Awaitable[CommandResult]]
    ) -> list[AptPackageItem]:
        """Resolve every name's version with ONE `dpkg-query` call (RESEARCH.md)."""
        names = _lines(showmanual_output)
        if not names:
            return []

        quoted = " ".join(shlex.quote(name) for name in names)
        # dpkg-query, not `apt list --installed`: apt's own manpage warns the latter's
        # output has no stable contract for scripting. The literal \t/\n below are
        # dpkg-query's OWN format-string escapes (interpreted by dpkg-query, not the
        # shell) — hence a plain (non-f) string so Python leaves them as two-char
        # backslash sequences for dpkg-query to expand into real tab/newline.
        versions_result = await run("dpkg-query --show --showformat='${Package}\\t${Version}\\n' " + quoted)

        versions: dict[str, str] = {}
        for line in versions_result.stdout.splitlines():
            if not line.strip():
                continue
            pkg_name, _, version = line.partition("\t")
            versions[pkg_name] = version

        return [AptPackageItem(name=name, version=versions.get(name, "")) for name in names]

    async def collect_hold_pin_facts(self) -> Sequence[HoldPinFact]:
        """PIN facts from the target's `/etc/apt/preferences.d/*` `Package:` stanzas —
        the target-only `HELD_OR_PINNED`/`REPORT_ONLY` echo on the package item.

        As of #208 this reads PINS only. HOLDS moved out of this hook: a hold is dpkg
        selection state (`apt-mark showhold`) replicated as its own `apt:hold:` membership
        item via `collect_hold_sets`, not surfaced as a package-level report — so a held
        package is never double-reported (once here and once as a hold item). A pin
        remains an apt priority preference that can still permit an upgrade, so it stays a
        report on the package rather than a converge action (RESEARCH Pitfall 2).
        """
        return self._pin_facts_from_scan(await self._scan_target_pin_lines())

    async def _scan_target_pin_lines(self) -> str:
        """The target's raw `<file>\\t<Package: ...>` lines from one batched command.

        `find ... -exec ... {} +` passes every matching file to one awk invocation (not a
        per-file command); if the directory has no files, -exec never runs, so an empty
        `preferences.d` produces empty output rather than a shell error. awk prints the
        WHOLE line, not `$2`: `_pin_packages` — the same parser `_parse_pin_file` uses — is
        what splits it, so a `Package: foo bar` stanza yields both names and `Package: *`
        yields none.
        """
        result = await self.target.run_command(
            "find /etc/apt/preferences.d -maxdepth 1 -type f -exec awk '/^Package:/{print FILENAME \"\\t\" $0}' {} +",
            login_shell=False,
        )
        return result.stdout

    @staticmethod
    def _pin_facts_from_scan(scan_output: str) -> list[HoldPinFact]:
        facts: list[HoldPinFact] = []
        for line in _lines(scan_output):
            filename, _, stanza = line.partition("\t")
            facts.extend(
                HoldPinFact(mechanism="pin", package=package, source_ref=filename) for package in _pin_packages(stanza)
            )
        return facts

    async def collect_hold_sets(self) -> tuple[frozenset[str], frozenset[str]]:
        """Source and target package-hold NAME sets from `apt-mark showhold` on BOTH
        machines (#208, D5). Read from both ends because the hold is replicated as a
        membership diff: a name held on the source but not the target becomes a hold
        (INSTALL), the reverse an unhold (REMOVE), and the target set also suppresses a
        held package's own install/upgrade action in `_diff_apt_packages`.
        """
        source_hold = await self.source.run_command("apt-mark showhold")
        target_hold = await self.target.run_command("apt-mark showhold", login_shell=False)
        return frozenset(_lines(source_hold.stdout)), frozenset(_lines(target_hold.stdout))

    async def collect_unavailable_item_ids(self, missing_item_ids: frozenset[str]) -> frozenset[str]:
        """Batched `apt-cache policy` over every missing-on-target name (one call, not
        one per package): a `Candidate: (none)` means the target's repositories have
        nothing to install from (D-25's REPO_UNAVAILABLE, not a proposable INSTALL).
        """
        if not missing_item_ids:
            return frozenset()

        names = sorted(_package_name(item_id) for item_id in missing_item_ids)
        quoted = " ".join(shlex.quote(name) for name in names)
        result = await self.target.run_command(f"apt-cache policy {quoted}", login_shell=False)
        no_candidate = packages_with_no_candidate(result.stdout)
        return frozenset(f"{_APT_PACKAGE_ID_PREFIX}{name}" for name in names if name in no_candidate)

    async def _plan_packages(self) -> PackagePlan:
        """The package half of `plan()`: load decision files -> capture -> query -> diff
        -> build review groups. Read-only.

        Nothing here may mutate either machine: a job plans and reviews before it
        converges, so `plan()` runs before the user has approved anything. Both
        machines' decision files are loaded first (a read, like everything else here)
        and each side's captured/queried items are filtered through its OWN file before
        diffing (D-08): an item recorded on the source is dropped from the source
        manifest so it is never pushed to a peer again; an item recorded on the target
        is dropped from the target query so it is never proposed for
        install/remove/change again — either way it produces no `ItemDiff` and never
        reaches the review.

        The finished diffs go through `_drop_inert_diffs` as well, which catches the
        recorded items no input-side filter can see: the `apt:hold:` membership items are
        derived from hold-set membership, and the post-diff pass is the only CORRECT place
        for them anyway — the target hold set additionally suppresses a held package's own
        install/upgrade action, so filtering that input set would re-propose upgrading a
        held package. `plan()` runs the same pass again over the repository and collateral
        diffs it appends.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        target_decisions = await DecisionFile(self.manager_id, self.target).load()
        self._plan_decisions = (source_decisions, target_decisions)

        source_items = await filter_inert(await self.capture_source_items(), source_decisions)
        self._plan_source_items = tuple(source_items)
        target_items = await filter_inert(await self.query_target_items(), target_decisions)
        hold_pin_facts = await self.collect_hold_pin_facts()
        source_hold_names, target_hold_names = await self.collect_hold_sets()
        missing_item_ids = frozenset(item.item_id for item in source_items) - frozenset(
            item.item_id for item in target_items
        )
        unavailable_item_ids = await self.collect_unavailable_item_ids(missing_item_ids)
        diffs = self._drop_inert_diffs(
            _diff_apt_packages(
                source_items,
                target_items,
                hold_pin_facts,
                unavailable_item_ids,
                source_hold_names,
                target_hold_names,
            ),
            source_decisions,
            target_decisions,
        )
        groups = self._build_review_groups(diffs)
        return PackagePlan(manager=self.manager_id, diffs=diffs, groups=groups)

    async def plan(self) -> PackagePlan:
        """Extends the base diff (missing/extra/mismatch/held/unavailable) with
        plan-time apt transaction-collateral classification (D-30) and the four
        `/etc/apt/*` repository item classes (D-11/D-12/D-13).

        Unreproducible detection is NOT apt's business (D-18): it moved to
        `manual_installs_sync` with its own enable flag, so this job never emits an
        `UNREPRODUCIBLE` diff.

        Runs AFTER the base diff and BEFORE review groups are (re)built. The batched
        apt-get --dry-run simulations reveal what the pending transaction would also remove or
        downgrade; each such package is split by provenance against the target's
        `apt-mark showmanual` set (captured here, once). An auto-installed collateral
        package is apt resolving its own dependencies — it proceeds silently, producing no
        review item. A manually-installed collateral package is something the user chose to
        have, so it becomes its own three-way review item (install-anyway / skip / abort)
        decided at plan time, in the SAME review the user approves from — never a prompt
        during apply.
        """
        base_plan = await self._plan_packages()
        self._target_manual_set = await self._capture_target_manual_set()
        collateral_diffs = await self._collect_plan_time_collateral(base_plan.diffs)
        repo_diffs = await self._plan_repo_diffs()

        if not collateral_diffs and not repo_diffs:
            return base_plan

        # Ordering is an apt FACT (key before source before packages, T-02-16), not a
        # general one: the base loop stays a plain item-by-item iterator, and THIS job
        # sorts its own diffs before they reach it. `sorted` is stable, so within one
        # rank (e.g. every APT_PACKAGE diff, or every APT_PIN/APT_CONFIG diff) the
        # original relative order — base diff, then collateral, then repo diffs — is
        # preserved.
        # This job's OWN extra diffs (repo files) also need the D-08a inertness pass the
        # base `plan()` already ran over its diffs — they are derived from directory
        # digests, so no input item carried their id into `filter_inert`. The decision
        # files `super().plan()` just read are reused rather than re-read.
        all_diffs = self._drop_inert_diffs(
            sorted(
                (*base_plan.diffs, *collateral_diffs, *repo_diffs),
                key=lambda diff: _ITEM_CLASS_ORDER.get(diff.item_class, 3),
            ),
            *self._plan_decisions,
        )
        groups = self._build_review_groups(all_diffs)
        return PackagePlan(manager=self.manager_id, diffs=all_diffs, groups=groups)

    @override
    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """Carves the manual-collateral diffs (D-30) out of the checkbox groups into a
        `COLLATERAL_REVIEW_ACTION` group whose entries take the three-way install-anyway /
        skip / abort resolution, presented after the base groups (installs/changes/
        removals) so the user sees the bulk of the diff before being asked to resolve
        anything.

        The unreproducible carve-out is gone (D-18: that concern moved to
        `manual_installs_sync`); this override remains only for collateral.
        """
        collateral = [diff for diff in diffs if _is_collateral_diff(diff)]
        if not collateral:
            return super()._build_review_groups(diffs)

        carved_ids = {diff.item_id for diff in collateral}
        rest = [diff for diff in diffs if diff.item_id not in carved_ids]
        groups = list(super()._build_review_groups(rest))
        groups.append(
            ReviewGroup(
                manager=self.manager_id,
                action=COLLATERAL_REVIEW_ACTION,
                title=f"Resolve {self.manager_id} manual-collateral removals",
                entries=tuple(
                    ReviewEntry(item_id=diff.item_id, label=diff.label, action_label="resolve", detail=diff.detail)
                    for diff in collateral
                ),
            )
        )
        return tuple(groups)

    async def _plan_repo_diffs(self) -> list[ItemDiff]:
        """Capture the five `/etc/apt/*` directories and diff the three reviewable item
        classes (D-11/D-12/D-13), by whole-file digest (module docstring): one batched
        `sha256sum` listing per directory per machine, full content fetched only for a
        file a diff implicates.

        The two KEY directories are captured here but produce no diff: keys are not items
        (module docstring), so their digests are simply cached for the provisioning and
        collection steps the repository group brackets its own writes with.

        A source offered for REMOVAL is additionally classified against what the TARGET
        still needs (C26) before the diff is built, so the review names the consequence
        rather than presenting a bare presence difference.
        """

        async def source_run(cmd: str) -> CommandResult:
            return await self.source.run_command(cmd)

        async def target_run(cmd: str) -> CommandResult:
            return await self.target.run_command(cmd, login_shell=False)

        source_sources = await _capture_dir_digests(source_run, _APT_SOURCES_DIR, extensions=_APT_SOURCE_EXTENSIONS)
        target_sources = await _capture_dir_digests(target_run, _APT_SOURCES_DIR, extensions=_APT_SOURCE_EXTENSIONS)
        self._source_sources_list_digest = await _capture_file_digest(source_run, _APT_SOURCES_LIST)
        self._target_sources_list_digest = await _capture_file_digest(target_run, _APT_SOURCES_LIST)
        # One `sha256sum` listing per key directory per machine, driven by `_KEY_DIRS` so
        # capture, reference resolution and provisioning can never disagree about which
        # directories exist.
        source_keys = [await _capture_dir_digests(source_run, directory) for directory in _KEY_DIRS]
        target_keys = [await _capture_dir_digests(target_run, directory) for directory in _KEY_DIRS]
        source_pins = await _capture_dir_digests(source_run, _APT_PREFERENCES_DIR)
        target_pins = await _capture_dir_digests(target_run, _APT_PREFERENCES_DIR)
        source_configs = await _capture_dir_digests(source_run, _APT_CONF_DIR)
        target_configs = await _capture_dir_digests(target_run, _APT_CONF_DIR)

        self._source_keyrings, self._source_global_keys, self._source_shared_keys = source_keys
        self._target_keyrings, self._target_global_keys, self._target_shared_keys = target_keys
        source_key_filenames = frozenset(name for digests in source_keys for name in digests)
        self._target_package_owned_keys = await self._capture_package_owned_keys(target_run)

        # Unconditional, one batched command per machine: which keyrings the target's
        # sources point at is what makes a key correct, and that is a property of EVERY
        # source file on the target, not just the ones a diff implicates.
        # `_source_removal_details` reuses the target's scan; the source's answer is the
        # origin-to-file map ADR-021 D-34 derives repository writes from.
        self._target_source_refs = await _scan_source_file_references(target_run)
        self._source_source_refs = await _scan_source_file_references(source_run)
        removal_details = await self._source_removal_details(
            target_run, extra_sources=frozenset(target_sources) - frozenset(source_sources)
        )

        diffs: list[ItemDiff] = []
        diffs.extend(
            await self._diff_apt_sources(
                source_run,
                target_run,
                source_sources,
                target_sources,
                source_key_filenames,
                removal_details,
            )
        )
        diffs.extend(await self._diff_apt_pins(source_run, target_run, source_pins, target_pins))
        diffs.extend(_diff_apt_configs(source_configs, target_configs))
        return diffs

    async def _source_removal_details(
        self,
        target_run: Callable[[str], Awaitable[CommandResult]],
        *,
        extra_sources: frozenset[str],
    ) -> dict[str, str]:
        """Classify, at plan time, what each offered source-file deletion would strand on
        the target (C26/N7) — the disclosure D-30 and the flatpak orphan case (#214) both
        put in the review rather than in a refusal.

        Scope is deliberately the target's MACHINE-SPECIFIC packages, not every installed
        package from the repository. A skip-always package is structurally invisible:
        `filter_inert` drops it from the target manifest before diffing, so it can never
        produce an `ItemDiff` of its own in any run, and the user's explicit "this machine
        keeps this, syncs never touch it" is exactly the promise a silent repo deletion
        breaks. An ordinary package is at least eligible for its own removal diff, and
        keying off the whole manual set would make the detail's length a property of the
        machine — a base-repo deletion would name a hundred packages and inform nobody.
        The limitation is documented in `docs/jobs/package-sync.md`.

        There is no key counterpart: a signing key is never offered for deletion, so there
        is no review text for one to carry. The user approves the REPOSITORY; whichever
        keyring that leaves unused is collected afterwards without a decision of its own.

        Costs one batched `apt-cache policy` over the recorded package names (never one
        per package, the `collect_unavailable_item_ids` shape), gated on a removal actually
        being offered; the source-file scan it also needs was already captured for keyring
        correctness, so this adds no second scan.
        """
        if not extra_sources:
            return {}

        _source_decisions, target_decisions = self._plan_decisions
        # Identity by id prefix, not by `DecisionEntry.item_class`: the collateral
        # report items share `ItemClass.APT_PACKAGE` but are `REPORT_ONLY` and carry an
        # `apt:collateral:` id, so the prefix is what isolates real package decisions.
        names = sorted(
            _package_name(item_id) for item_id in target_decisions if item_id.startswith(_APT_PACKAGE_ID_PREFIX)
        )
        if not names:
            return {}

        quoted = " ".join(shlex.quote(name) for name in names)
        policy = await target_run(f"apt-cache policy {quoted}")
        origins_by_package = installed_origins_by_package(policy.stdout)
        packages_by_origin: dict[str, list[str]] = {}
        for name in names:
            for origin in origins_by_package.get(name, frozenset()):
                packages_by_origin.setdefault(origin, []).append(name)

        details: dict[str, str] = {}
        for filename in sorted(extra_sources):
            _refs, uris = self._target_source_refs.get(filename, ((), ()))
            reached: set[str] = set()
            for uri in uris:
                reached.update(packages_by_origin.get(uri, ()))
            if reached:
                details[filename] = build_orphaned_packages_detail(filename, sorted(reached))
        return details

    async def _diff_apt_sources(
        self,
        source_run: Callable[[str], Awaitable[CommandResult]],
        target_run: Callable[[str], Awaitable[CommandResult]],
        source_digests: Mapping[str, str],
        target_digests: Mapping[str, str],
        source_key_filenames: frozenset[str],
        removal_details: Mapping[str, str] | None = None,
    ) -> list[ItemDiff]:
        """Source-file diffs, hydrated with format + keyring refs only for files a diff
        implicates (missing-on-target, extra-on-target, or digest-mismatched).

        A source item whose OWN keyring reference resolves to no key file on the source
        itself carries the dangling-reference detail and is downgraded to
        `REPORT_ONLY` instead of `INSTALL` — it is not proposed for install on its own
        (D-12): a repo written without its key is a repo apt refuses.

        `removal_details` carries the C26 impact text for an extra-on-target file whose
        deletion would strand machine-specific packages, keyed by filename. Disclosure
        only: the REMOVE action is unchanged, since removing a repo whose packages are
        also going is legitimate.

        The SOURCE machine's `keyring_refs` are cached in `self._source_keyring_refs` for
        every file parsed here, since those are the references that will apply once this
        run writes the file — the keyring provisioning and the converge-time readiness
        check both read them from there rather than re-reading the file.
        """
        details = removal_details or {}
        names = _diff_filenames(source_digests, target_digests)
        diffs: list[ItemDiff] = []

        for filename in sorted(names.missing):
            content = await _read_file_content(source_run, f"{_APT_SOURCES_DIR}/{filename}")
            fmt, refs, _uris = _parse_source_file(filename, content)
            self._source_keyring_refs[filename] = refs
            item = AptSourceItem(filename=filename, digest=source_digests[filename], fmt=fmt, keyring_refs=refs)
            dangling = _dangling_keyring_ref(refs, source_key_filenames)
            if dangling is not None:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_SOURCE,
                        diff_class=DiffClass.MISSING_ON_TARGET,
                        action=DiffAction.REPORT_ONLY,
                        item_id=item.item_id,
                        label=item.label(),
                        detail=build_dangling_keyring_detail(filename, dangling),
                    )
                )
            else:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_SOURCE,
                        diff_class=DiffClass.MISSING_ON_TARGET,
                        action=DiffAction.INSTALL,
                        item_id=item.item_id,
                        label=item.label(),
                        detail=self._travelling_keyrings_detail(refs),
                    )
                )

        for filename in sorted(names.extra):
            content = await _read_file_content(target_run, f"{_APT_SOURCES_DIR}/{filename}")
            fmt, _refs, _uris = _parse_source_file(filename, content)
            item = AptSourceItem(filename=filename, digest=target_digests[filename], fmt=fmt)
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_SOURCE,
                    diff_class=DiffClass.EXTRA_ON_TARGET,
                    action=DiffAction.REMOVE,
                    item_id=item.item_id,
                    label=item.label(),
                    detail=details.get(filename),
                )
            )

        for filename in sorted(names.changed):
            content = await _read_file_content(source_run, f"{_APT_SOURCES_DIR}/{filename}")
            fmt, refs, _uris = _parse_source_file(filename, content)
            self._source_keyring_refs[filename] = refs
            item = AptSourceItem(filename=filename, digest=source_digests[filename], fmt=fmt, keyring_refs=refs)
            dangling = _dangling_keyring_ref(refs, source_key_filenames)
            detail = build_version_mismatch_detail(source_digests[filename], target_digests[filename])
            keys = self._travelling_keyrings_detail(refs)
            if keys is not None:
                detail = f"{detail}; {keys}"
            # Mirrors the "missing" branch above (D-12): a dangling keyring reference
            # makes this file converge-time-refused by `_require_keyrings_ready`
            # regardless, so the review must present it as the same informational fact
            # up front rather than as an ordinary change a user can tick and have fail.
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_SOURCE,
                    diff_class=DiffClass.VERSION_MISMATCH,
                    action=DiffAction.REPORT_ONLY if dangling is not None else DiffAction.CHANGE,
                    item_id=item.item_id,
                    label=item.label(),
                    detail=build_dangling_keyring_detail(filename, dangling) if dangling is not None else detail,
                )
            )

        return diffs

    def _travelling_keyrings_detail(self, refs: Sequence[str]) -> str | None:
        """The review/dry-run detail naming the signing keys writing this source file would
        also copy onto the target, or `None` when it would copy none (the target already
        holds every key it names, byte-identical or under its own dpkg's ownership).

        A key is not an item and gets no decision of its own (D-12), but writing one into
        `/etc/apt` is still a change to the target, and ADR-014 requires a run to report
        exactly what it would do. Naming the keys on the repository the user IS deciding
        about is what keeps "no decision of its own" from meaning "invisible".

        Judged from plan-time state, which is the same state `_keyring_writes` recomputes
        at converge time from the real decisions. The two can differ for one file only in
        the direction that copies FEWER keys — another approved source file writing the
        same rotated key first — so the detail never promises a write that does not happen.
        """
        names = sorted({Path(dest).name for dest in self._referenced_keyring_writes(refs)})
        if not names:
            return None
        plural = "s" if len(names) > 1 else ""
        return f"signing key{plural} copied with it: {', '.join(names)}"

    async def _diff_apt_pins(
        self,
        source_run: Callable[[str], Awaitable[CommandResult]],
        target_run: Callable[[str], Awaitable[CommandResult]],
        source_digests: Mapping[str, str],
        target_digests: Mapping[str, str],
    ) -> list[ItemDiff]:
        """Pin-file diffs; `pinned_packages` is hydrated the same way `AptSourceItem`'s
        format/keyring_refs are — only for a file a diff actually implicates.
        """
        names = _diff_filenames(source_digests, target_digests)
        diffs: list[ItemDiff] = []

        for filename in sorted(names.missing):
            content = await _read_file_content(source_run, f"{_APT_PREFERENCES_DIR}/{filename}")
            item = AptPinItem(
                filename=filename, digest=source_digests[filename], pinned_packages=_parse_pin_file(content)
            )
            diffs.append(_file_diff(item, DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL))

        for filename in sorted(names.extra):
            content = await _read_file_content(target_run, f"{_APT_PREFERENCES_DIR}/{filename}")
            item = AptPinItem(
                filename=filename, digest=target_digests[filename], pinned_packages=_parse_pin_file(content)
            )
            diffs.append(_file_diff(item, DiffClass.EXTRA_ON_TARGET, DiffAction.REMOVE))

        for filename in sorted(names.changed):
            content = await _read_file_content(source_run, f"{_APT_PREFERENCES_DIR}/{filename}")
            item = AptPinItem(
                filename=filename, digest=source_digests[filename], pinned_packages=_parse_pin_file(content)
            )
            detail = build_version_mismatch_detail(source_digests[filename], target_digests[filename])
            diffs.append(_file_diff(item, DiffClass.VERSION_MISMATCH, DiffAction.CHANGE, detail=detail))

        return diffs

    def _protected_manual_set(self) -> frozenset[str]:
        """Packages a collateral removal/downgrade must not silently touch: the union of
        the TARGET's and the SOURCE's `apt-mark showmanual` sets (decision 8). A package
        the user manually installed on EITHER machine is one they chose to have, so
        protecting the union closes the rare edge case where a package is manual on the
        source but auto-installed (or absent) on the target and would otherwise be
        removed/downgraded silently. The machine-specific decision list is intentionally
        NOT consulted (decision 8, accepted limitation).
        """
        return self._target_manual_set | self._source_manual_set

    async def _capture_target_manual_set(self) -> frozenset[str]:
        """The target's `apt-mark showmanual` set — one batched command, the single
        source of the auto-versus-manual collateral split (D-30). This is the same set
        apt itself consults to decide what it may remove, so classifying a collateral
        package by membership here matches apt's own notion of "the user chose this".
        """
        result = await self.target.run_command("apt-mark showmanual", login_shell=False)
        return frozenset(_lines(result.stdout))

    async def _collect_plan_time_collateral(self, diffs: Sequence[ItemDiff]) -> list[ItemDiff]:
        """Two BATCHED simulations — the whole install candidate set, the whole
        removal candidate set — not one per package: a per-package simulation over a
        150-package manual set would cost more than the sync itself (D-30 hangs its
        classification off these two results; no third simulation is added).

        Each simulation's would-remove/would-downgrade collateral is split by
        `_classify_collateral` against the target's manual set: auto collateral produces
        nothing (apt's own business, D-30), manual collateral becomes a review item whose
        `item_id` (`apt:collateral:<name>`) is mapped back to the triggering candidate set
        in `self._collateral_trigger_ids`, so a `skip` decision can later be translated
        into `SKIP_ONCE` on the installs it gates.
        """
        # APT_PACKAGE only: a hold item (`apt:hold:`) shares the INSTALL/REMOVE actions
        # but is dpkg selection state, not an apt-get transaction, so it drives no
        # collateral simulation and its id is not a package id (#208).
        pkg = [d for d in diffs if d.item_class == ItemClass.APT_PACKAGE]
        install_names = [_package_name(d.item_id) for d in pkg if d.action == DiffAction.INSTALL]
        remove_names = [_package_name(d.item_id) for d in pkg if d.action == DiffAction.REMOVE]
        reviewed_names = frozenset(install_names) | frozenset(remove_names)

        collateral: list[ItemDiff] = []
        if install_names:
            quoted = " ".join(shlex.quote(name) for name in install_names)
            preview = await simulate_apt_transaction(
                self.target, f"install --assume-yes --no-install-recommends {quoted}", login_shell=False
            )
            trigger_ids = frozenset(f"{_APT_PACKAGE_ID_PREFIX}{name}" for name in install_names)
            collateral.extend(await self._classify_collateral(preview, reviewed_names, trigger_ids, verb="installing"))
        if remove_names:
            quoted = " ".join(shlex.quote(name) for name in remove_names)
            preview = await simulate_apt_transaction(self.target, f"remove --assume-yes {quoted}", login_shell=False)
            trigger_ids = frozenset(f"{_APT_PACKAGE_ID_PREFIX}{name}" for name in remove_names)
            collateral.extend(await self._classify_collateral(preview, reviewed_names, trigger_ids, verb="removing"))
        return collateral

    async def _classify_collateral(
        self,
        preview: AptTransactionPreview,
        reviewed_names: frozenset[str],
        trigger_ids: frozenset[str],
        *,
        verb: str,
    ) -> list[ItemDiff]:
        """Partition a simulation's would-remove/would-downgrade packages by provenance
        (D-30): a package in the target OR source manual set becomes a manual-collateral
        review item (decision 8); one in neither is auto-installed — apt's own dependency
        — and produces nothing, not even a report line the user cannot act on.

        A downgrade is detected exactly as before: an `install_versions` entry with a
        non-`None` old version and `compare_deb_versions(target, new, old) < 0`. The
        triggering candidate set is recorded against each emitted item's id so `skip`
        can be translated to `SKIP_ONCE` on the installs it gates (`accept_review`).
        """
        protected = self._protected_manual_set()
        collateral: list[ItemDiff] = []

        for pkg in preview.removals:
            if pkg in reviewed_names or pkg not in protected:
                continue
            collateral.append(
                self._collateral_item(pkg, f"would be removed by {verb} the selected packages", trigger_ids)
            )

        for pkg, (old_version, new_version) in preview.install_versions.items():
            if pkg in reviewed_names or old_version is None or pkg not in protected:
                continue
            if await compare_deb_versions(self.target, new_version, old_version) < 0:
                effect = f"would be downgraded from {old_version} to {new_version} by {verb} the selected packages"
                collateral.append(self._collateral_item(pkg, effect, trigger_ids))

        return collateral

    def _collateral_item(self, name: str, effect: str, trigger_ids: frozenset[str]) -> ItemDiff:
        """Build one manual-collateral `ItemDiff` and record its triggering candidate set."""
        diff = _collateral_diff(name, effect)
        self._collateral_trigger_ids[diff.item_id] = trigger_ids
        return diff

    def _approved_removal_names(self) -> frozenset[str]:
        """Package names of every `REMOVE`-action diff this run's decisions approved.

        The removal guard's rule is "removes nothing the user did not approve", not
        "removes nothing else" — removing a package legitimately removes things that
        depend on it, so the guard needs to know the full approved-removal set, not
        just the one item currently converging.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        decisions = self._accepted_outcome.decisions
        return frozenset(
            _package_name(diff.item_id)
            for diff in self._accepted_plan.diffs
            if diff.item_class == ItemClass.APT_PACKAGE
            and diff.action == DiffAction.REMOVE
            and decisions.get(diff.item_id) == Decision.APPLY
        )

    def _resolve_collateral(self, plan: PackagePlan, outcome: ReviewOutcome) -> ReviewOutcome:
        """Translate the manual-collateral group's decisions (D-30) into the guard's
        approved set and the triggering installs' decisions.

        For each collateral item (`apt:collateral:<pkg>`): an `APPLY` (install-anyway)
        marks `<pkg>` approved, so `_converge_install`/`_converge_remove` let its removal
        or downgrade through; a `SKIP_ONCE` (skip) is propagated to every install that
        collateral gated (`self._collateral_trigger_ids`), so the install is cleanly left
        unapproved rather than attempted and refused at the guard. Abort never reaches
        here — it raised `SyncAbortedByUser` inside the review.

        Returns the outcome with any triggering-install decisions overridden; leaves the
        decisions map untouched when there is no collateral to resolve.
        """
        approved: set[str] = set()
        overrides: dict[str, Decision] = {}
        for diff in plan.diffs:
            if not _is_collateral_diff(diff):
                continue
            decision = outcome.decisions.get(diff.item_id)
            if decision == Decision.APPLY:
                approved.add(diff.item_id.removeprefix(_COLLATERAL_ID_PREFIX))
            elif decision == Decision.SKIP_ONCE:
                for trigger_id in self._collateral_trigger_ids.get(diff.item_id, frozenset()):
                    overrides[trigger_id] = Decision.SKIP_ONCE

        self._approved_collateral = frozenset(approved)
        if not overrides:
            return outcome
        return ReviewOutcome(
            decisions={**outcome.decisions, **overrides},
            was_interactive=outcome.was_interactive,
            snippets=outcome.snippets,
            unresolved=outcome.unresolved,
        )

    @override
    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Insert the synthetic metadata-refresh diff (Task 2) once the coordinator's
        decisions are known, so it flows through the same per-item logging, dry-run
        gate and failure collection as everything else (`apply()`'s existing loop)
        instead of being a special case bolted onto the end.

        Runs AFTER `plan()` (so decisions exist) and is exactly where D-24's review
        already stopped being relevant for THIS item — the refresh is infrastructure
        the user never ticks, not a repository they decided about. Positioned immediately
        after the last non-package diff (repository group already sorted
        pin/config-before-source by `plan()`) and before every package diff, matching
        apt's own dependency order: metadata must be current before anything installs
        from it.

        The marker is ALSO what carries a run whose only repository work is a keyring:
        a rotated key changes no source file, so it produces no diff and nothing else
        would ever route into `_converge_repo_group_item`. `_pending_keyring_work` is a
        superset test — the group recomputes the exact set from the real decisions and
        returns early if it is empty — so the cost of a false positive is one no-op call.

        Manual-collateral decisions (D-30) are resolved first: an install-anyway on a
        collateral item marks its package approved so the apply-time guard lets the
        removal through, while a skip is translated into `SKIP_ONCE` on the installs that
        collateral gated, so a declined collateral cleanly leaves its triggering installs
        unapproved rather than failing them at the guard.
        """
        outcome = self._resolve_collateral(plan, outcome)
        approved_group = any(
            diff.item_class in _REPO_GROUP_CLASSES
            and diff.item_id != _METADATA_REFRESH_ITEM_ID
            and outcome.decisions.get(diff.item_id) == Decision.APPLY
            for diff in plan.diffs
        )
        if approved_group or self._pending_keyring_work():
            marker = _metadata_refresh_diff()
            # Repo-group items sort before the metadata refresh, packages after it, and
            # holds LAST (#208, D8: install-before-hold). Holds are neither package nor
            # repo-group, so they are pulled out explicitly rather than folding into the
            # pre-marker `non_package` bucket, which would converge them before installs.
            repo_like = [
                diff for diff in plan.diffs if diff.item_class not in (ItemClass.APT_PACKAGE, ItemClass.APT_HOLD)
            ]
            package = [diff for diff in plan.diffs if diff.item_class == ItemClass.APT_PACKAGE]
            holds = [diff for diff in plan.diffs if diff.item_class == ItemClass.APT_HOLD]
            plan = PackagePlan(manager=plan.manager, diffs=(*repo_like, marker, *package, *holds), groups=plan.groups)
            outcome = ReviewOutcome(
                decisions={**outcome.decisions, marker.item_id: Decision.APPLY},
                was_interactive=outcome.was_interactive,
                # Carried through verbatim — rebuilding `decisions` above must not drop
                # this run's authored snippets/unresolved items (Task 2).
                snippets=outcome.snippets,
                unresolved=outcome.unresolved,
            )
        super().accept_review(plan, outcome)

    # -- Post-repository re-plan and second review -------------------------------------

    @override
    async def apply(self) -> None:
        """Converge the repository group FIRST, re-derive the package facts it invalidated,
        and re-review whatever actually changed — then run the base apply loop.

        `plan()` reads `/etc/apt/preferences.d` and asks the target's apt what it can
        install. This same run then REWRITES `/etc/apt`. Every such fact is therefore stale
        by the time packages converge, and the visible case is a pin file the user is
        deleting: at plan time it still forced its packages to `HELD_OR_PINNED`/
        `REPORT_ONLY`, so their real install/change/remove diff was suppressed and would
        only reappear on the next run.

        The repository group is converged eagerly here rather than lazily on the first
        repository diff `converge()` sees; `_ensure_repo_group_converged` is idempotent, so
        the base loop's own calls become cache lookups and every item still reports through
        it exactly as before. The only reordering is that skip-always decision files are now
        written after `/etc/apt` lands rather than before — neither depends on the other.

        A second review is ADR-020 D-24's one-review-per-manager giving way to correctness:
        a decision the user could not have made correctly the first time is asked again,
        not deferred. Aborting at that review therefore stops the run with `/etc/apt`
        already converged, which is a reviewed, coherent state.

        Skipped entirely under dry-run: nothing is written, so nothing is invalidated — but
        a dry-run preview does show the pre-repository package classification, and that is
        the one place the staleness remains visible.
        """
        if not self.context.dry_run and self._repository_work_approved():
            await self._ensure_repo_group_converged()
            await self._rereview_repo_invalidated_packages()
        await super().apply()

    def _repository_work_approved(self) -> bool:
        """Whether this run has any `/etc/apt` work at all, by the presence of the synthetic
        metadata-refresh marker: `accept_review` inserts it exactly when a repository-group
        item was approved or a keyring may need writing, so no marker means nothing can
        invalidate `plan()`'s facts.
        """
        assert self._accepted_outcome is not None
        return self._accepted_outcome.decisions.get(_METADATA_REFRESH_ITEM_ID) == Decision.APPLY

    async def _rereview_repo_invalidated_packages(self) -> None:
        """Re-diff packages against the target this run just produced, then withdraw the
        approvals the new state contradicts and review the actions it revealed.

        The source manifest is reused verbatim (`self._plan_source_items`): pc-switcher
        never mutates the source, so re-capturing it would cost two commands to learn
        nothing. Everything read from the TARGET is re-read, because that is what changed.

        Two directions, only one of which needs the user:

        - An item whose action APPEARED or CHANGED (a package the deleted pin was
          suppressing, or one the newly-installed repository can now provide) has never been
          reviewed in its current form, so it goes to a second review together with any
          manual collateral its transaction implies.
        - An item the user approved that the new state no longer supports (a package a pin
          this run INSTALLED now governs) has its approval withdrawn silently. Dropping work
          needs no decision — and asking would be asking the user to re-confirm a "no".
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None

        fresh = await self._replanned_package_diffs()
        fresh_by_id = {diff.item_id: diff for diff in fresh}
        reviewed_by_id = {diff.item_id: diff for diff in self._accepted_plan.diffs}

        revealed: list[ItemDiff] = []
        for diff in fresh:
            if diff.action == DiffAction.REPORT_ONLY:
                continue
            previous = reviewed_by_id.get(diff.item_id)
            if previous is None or previous.action != diff.action:
                revealed.append(diff)

        withdrawn: set[str] = set()
        for diff in self._accepted_plan.diffs:
            # Only package-level items are re-diffed here; the repository group has already
            # converged and its collateral report items carry no converge verb to withdraw.
            if diff.item_class not in (ItemClass.APT_PACKAGE, ItemClass.APT_HOLD):
                continue
            if diff.action == DiffAction.REPORT_ONLY:
                continue
            if self._accepted_outcome.decisions.get(diff.item_id) != Decision.APPLY:
                continue
            current = fresh_by_id.get(diff.item_id)
            if current is None or current.action != diff.action:
                withdrawn.add(diff.item_id)

        if not revealed and not withdrawn:
            return

        for item_id in sorted(withdrawn):
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"withdrawing {reviewed_by_id[item_id].label}: this run's /etc/apt changes make it "
                f"{fresh_by_id[item_id].action.value if item_id in fresh_by_id else 'inapplicable'}",
            )

        collateral = await self._collect_plan_time_collateral(revealed) if revealed else []
        decisions: dict[str, Decision] = {
            **self._accepted_outcome.decisions,
            **dict.fromkeys(withdrawn, Decision.SKIP_ONCE),
        }
        second_groups: tuple[ReviewGroup, ...] = ()

        if revealed or collateral:
            second_groups = self._rereview_groups([*revealed, *collateral])
            assert self.context.reviewer is not None
            second = await self.context.reviewer.review(second_groups)
            decisions.update(second.decisions)
            outcome = ReviewOutcome(
                decisions=decisions,
                was_interactive=self._accepted_outcome.was_interactive and second.was_interactive,
                snippets={**self._accepted_outcome.snippets, **second.snippets},
                unresolved=(*self._accepted_outcome.unresolved, *second.unresolved),
            )
        else:
            outcome = ReviewOutcome(
                decisions=decisions,
                was_interactive=self._accepted_outcome.was_interactive,
                snippets=self._accepted_outcome.snippets,
                unresolved=self._accepted_outcome.unresolved,
            )

        merged_plan = self._merge_replanned_diffs([*revealed, *collateral], second_groups)

        # Re-run over the MERGED plan so the first review's collateral resolutions survive:
        # `_resolve_collateral` recomputes `self._approved_collateral` from scratch, and
        # handing it only the second review's items would silently drop the first's.
        outcome = self._resolve_collateral(merged_plan, outcome)
        super().accept_review(merged_plan, outcome)

    async def _replanned_package_diffs(self) -> tuple[ItemDiff, ...]:
        """The package/hold diff as it stands NOW — every target-side input re-read, the
        source manifest reused, and the same decision-file inertness filter applied.
        """
        _source_decisions, target_decisions = self._plan_decisions
        target_items = await filter_inert(await self.query_target_items(), target_decisions)
        hold_pin_facts = await self.collect_hold_pin_facts()
        source_hold_names, target_hold_names = await self.collect_hold_sets()
        missing_item_ids = frozenset(item.item_id for item in self._plan_source_items) - frozenset(
            item.item_id for item in target_items
        )
        unavailable_item_ids = await self.collect_unavailable_item_ids(missing_item_ids)
        return self._drop_inert_diffs(
            _diff_apt_packages(
                self._plan_source_items,
                target_items,
                hold_pin_facts,
                unavailable_item_ids,
                source_hold_names,
                target_hold_names,
            ),
            *self._plan_decisions,
        )

    def _merge_replanned_diffs(self, extra: Sequence[ItemDiff], extra_groups: Sequence[ReviewGroup]) -> PackagePlan:
        """The accepted plan with `extra` folded in — replacing same-id diffs in place and
        appending genuinely new ones into the package band, holds still last (D8).
        `extra_groups` is appended so the plan's groups still describe everything the user
        was actually shown.

        The repository group and the metadata-refresh marker keep their positions and are
        never re-diffed: they have already converged by the time this runs.
        """
        assert self._accepted_plan is not None
        by_id = {diff.item_id: diff for diff in extra}
        merged = [by_id.get(diff.item_id, diff) for diff in self._accepted_plan.diffs]
        known = {diff.item_id for diff in self._accepted_plan.diffs}
        merged.extend(diff for diff in extra if diff.item_id not in known)
        holds = [diff for diff in merged if diff.item_class == ItemClass.APT_HOLD]
        rest = [diff for diff in merged if diff.item_class != ItemClass.APT_HOLD]
        return PackagePlan(
            manager=self._accepted_plan.manager,
            diffs=(*rest, *holds),
            groups=(*self._accepted_plan.groups, *extra_groups),
        )

    def _rereview_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """`_build_review_groups` over the re-diffed items, with every title marked as a
        second pass so the user can tell this screen from the one they already answered.
        """
        return tuple(
            replace(group, title=f"{group.title} (revealed by this run's /etc/apt changes)")
            for group in self._build_review_groups(diffs)
        )

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Simulate the exact apt transaction, guard it, then run the real command —
        for apt packages. Hold items (`apt:hold:<name>`) are routed FIRST, by item_id
        prefix, to `_converge_hold` so an `apt:hold:` INSTALL runs `apt-mark hold` rather
        than falling into the action-based `apt-get install` dispatch (#208, D4).
        Repository-group items (pins, apt config, sources) and the synthetic
        metadata-refresh marker converge as one ordered, transactional unit via
        `_converge_repo_group_item` instead (Task 2) — the unit that also provisions and
        collects signing keys around its own writes. Unreproducible items are not apt's
        concern (D-18) — `manual_installs_sync` owns their snippet replay — so `converge()`
        here only ever sees hold, repository-group, `INSTALL` or `REMOVE` diffs.

        One package per invocation (D-27) so a single bad package cannot fail the
        whole batch, and so each package's simulation corresponds exactly to the
        command that follows it. The target resolves dependencies and downloads from
        its own repos (D-28) — no source cache is consulted.
        """
        if diff.item_id.startswith(_APT_HOLD_ID_PREFIX):
            return await self._converge_hold(diff)
        if diff.item_class in _REPO_GROUP_CLASSES or diff.item_id == _METADATA_REFRESH_ITEM_ID:
            return await self._converge_repo_group_item(diff)
        if diff.action == DiffAction.INSTALL:
            return await self._converge_install(diff)
        if diff.action == DiffAction.REMOVE:
            return await self._converge_remove(diff)
        raise ConvergeItemFailed(
            f"AptSyncJob.converge: unsupported action {diff.action.value!r} for {diff.label} "
            "(only 'install' and 'remove' exist for apt packages)"
        )

    async def _ensure_metadata_refreshed(self) -> None:
        """Run exactly one `apt-get update` before the first package install of a run that
        approves an INSTALL but changes no repository-group item (decision 1) — resolving
        installs against a stale package list can pick candidates the target can no longer
        fetch. A no-op once metadata has already been refreshed this run, INCLUDING by the
        repository-group convergence's own `apt-get update` (which sets the same flag), so
        the two refresh paths never both fire.

        Aborts the install by raising `ConvergeItemFailed` if the refresh fails: unlike the
        repository-group path — which has `/etc/apt` writes to roll back and owns that
        behaviour — this path made no changes, so failing the item (installing nothing) is
        its whole safe response. The failure is cached so every remaining install this run
        aborts on the same error without issuing a second `apt-get update`. Never reached
        under dry-run: the base `apply()` loop does not call `converge()` when
        `self.context.dry_run` is set.
        """
        if self._metadata_refreshed:
            return
        if self._metadata_refresh_error is not None:
            raise ConvergeItemFailed(self._metadata_refresh_error)

        result = await self.target.run_command(
            "sudo apt-get update",
            login_shell=False,
            mutates="refresh apt package lists before the first install of this run",
        )
        if not result.success:
            self._metadata_refresh_error = (
                f"apt-get update failed before installing {self.manager_id} packages; refusing to install "
                f"against a stale package list (decision 1): {result.stderr.strip()}"
            )
            raise ConvergeItemFailed(self._metadata_refresh_error)
        self._metadata_refreshed = True

    async def _converge_install(self, diff: ItemDiff) -> CommandResult:
        """Simulate, then apply, one apt install — the last line of defence behind the
        plan-time collateral classification (D-30). Auto-installed collateral (a package
        apt pulls in that is in neither the target nor source `apt-mark showmanual` set)
        proceeds silently — apt resolving its own dependencies. A manually-installed
        collateral removal or downgrade (manual on the target OR source, decision 8) is
        refused unless the user approved it install-anyway in the review; the decision was
        made at plan time, and this guard only verifies the real transaction has not
        drifted to touch a manual package nobody saw.

        A single `apt-get update` runs before the first install of the run
        (`_ensure_metadata_refreshed`, decision 1) unless the repository-group convergence
        already refreshed metadata this run.
        """
        name = _package_name(diff.item_id)
        await self._ensure_metadata_refreshed()
        quoted = shlex.quote(name)
        install_args = f"install --assume-yes --no-install-recommends {quoted}"

        preview = await simulate_apt_transaction(self.target, install_args, login_shell=False)

        protected = self._protected_manual_set()
        refused = [pkg for pkg in preview.removals if pkg in protected and pkg not in self._approved_collateral]
        if refused:
            removed = ", ".join(refused)
            raise ConvergeItemFailed(
                f"install of {name} refused: apt-get --dry-run would remove manually-installed {removed}, "
                "which was not approved as collateral in this run (D-30)"
            )

        for pkg, (old_version, new_version) in preview.install_versions.items():
            if old_version is None or pkg not in protected or pkg in self._approved_collateral:
                continue
            if await compare_deb_versions(self.target, new_version, old_version) < 0:
                raise ConvergeItemFailed(
                    f"install of {name} refused: apt-get --dry-run would downgrade manually-installed {pkg} "
                    f"from {old_version} to {new_version}, which was not approved as collateral (D-30, D-04)"
                )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {install_args}"
        return await self.target.run_command(real_cmd, login_shell=False, mutates=f"install apt package {name}")

    async def _converge_remove(self, diff: ItemDiff) -> CommandResult:
        """Simulate, then apply, one apt remove — the same last line of defence the
        install guard is (D-30). A collateral removal of an auto-installed package (in
        neither the target nor source `apt-mark showmanual` set) proceeds — removing a
        package legitimately removes the now-orphaned dependencies apt pulled in for it. A
        collateral removal of a manually-installed package (manual on the target OR source,
        decision 8) is refused unless it was itself an approved removal this run or approved
        install-anyway as collateral; that decision was made at plan time, and this guard
        only catches a real transaction that drifted to touch a manual package nobody
        reviewed.
        """
        name = _package_name(diff.item_id)
        quoted = shlex.quote(name)
        remove_args = f"remove --assume-yes {quoted}"

        preview = await simulate_apt_transaction(self.target, remove_args, login_shell=False)
        approved = self._approved_removal_names()
        protected = self._protected_manual_set()
        refused = [
            pkg
            for pkg in preview.removals
            if pkg != name and pkg not in approved and pkg not in self._approved_collateral and pkg in protected
        ]
        if refused:
            removed = ", ".join(refused)
            raise ConvergeItemFailed(
                f"removal of {name} refused: apt-get --dry-run would also remove manually-installed {removed}, "
                "which was neither an approved removal nor approved as collateral in this run (D-30)"
            )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {remove_args}"
        return await self.target.run_command(real_cmd, login_shell=False, mutates=f"remove apt package {name}")

    async def _converge_hold(self, diff: ItemDiff) -> CommandResult:
        """Converge one `apt:hold:<name>` membership item (#208, D4/D5): `apt-mark hold`
        for the add direction (INSTALL), `apt-mark unhold` for the remove direction
        (REMOVE). Selection state only — no `apt-get --dry-run` simulation and no transaction
        guard (a hold changes nothing about the installed package set, D4). The command's
        exit code alone decides pass/fail (D-27); a hold on an absent or unknown package
        that `apt-mark` rejects is a normal per-item failure (D6), not a gated abort.
        """
        name = diff.item_id.removeprefix(_APT_HOLD_ID_PREFIX)
        quoted = shlex.quote(name)
        verb = "hold" if diff.action == DiffAction.INSTALL else "unhold"
        return await self.target.run_command(
            f"sudo apt-mark {verb} {quoted}", login_shell=False, mutates=f"{verb} apt package {name}"
        )

    # -- Repository-group convergence (Task 2: D-11, D-12, D-13, D-27, T-02-34/35) -----
    #
    # The base `apply()` loop calls `converge()` once per approved diff, in `plan.diffs`
    # order (already sorted key -> pin/config -> source -> metadata-refresh -> packages
    # by `plan()`/`accept_review`). Rather than doing each repository-group item's work
    # in ITS OWN `converge()` call — which would make the group's transactionality
    # (backup everything before ANY write; roll back everything if the metadata refresh
    # fails) impossible to express without the base loop knowing about groups — the
    # FIRST repository-group (or metadata-refresh) diff `converge()` sees triggers
    # `_ensure_repo_group_converged`, which does the WHOLE group's work right then:
    # every subsequent group diff's `converge()` call is then a cache lookup against the
    # per-item outcome that eager run recorded, including — critically — outcomes for
    # diffs `converge()` has not been called for yet, and outcomes for diffs a rollback
    # retroactively marks as failed even though their own write succeeded.

    async def _converge_repo_group_item(self, diff: ItemDiff) -> CommandResult:
        await self._ensure_repo_group_converged()
        assert self._repo_group_outcome is not None
        succeeded, message = self._repo_group_outcome[diff.item_id]
        if succeeded:
            return CommandResult(exit_code=0, stdout=message, stderr="")
        raise ConvergeItemFailed(message)

    def _approved_repo_group_diffs(self) -> list[ItemDiff]:
        """Every repository-group (pin/config/source) diff this run's decisions approved,
        in `plan.diffs` order — already pin/config-before-source (`plan()`'s sort).
        Excludes the synthetic metadata-refresh marker itself, which is tracked separately
        since it names no `/etc/apt` file to back up or write.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        decisions = self._accepted_outcome.decisions
        return [
            diff
            for diff in self._accepted_plan.diffs
            if diff.item_class in _REPO_GROUP_CLASSES
            and diff.item_id != _METADATA_REFRESH_ITEM_ID
            and diff.action in (DiffAction.INSTALL, DiffAction.REMOVE, DiffAction.CHANGE)
            and decisions.get(diff.item_id) == Decision.APPLY
        ]

    async def _ensure_repo_group_converged(self) -> None:
        """Do the repository group's entire convergence exactly once per run: back up
        every destination the group will touch, write/remove in the already-established
        order, run ONE `apt-get update`, and roll back the whole group if it fails
        (T-02-34) — never partially, since a failed metadata refresh with some files
        written and others not would leave `/etc/apt` in a configuration nobody reviewed.

        Idempotent: a no-op on every call after the first (`self._repo_group_outcome`
        is `None` only until this method's first successful completion). Never called
        under dry-run — the base `apply()` loop never calls `converge()` at all when
        `self.context.dry_run` is set, so this method's own logic can assume real
        commands are safe to issue.
        """
        if self._repo_group_outcome is not None:
            return

        assert self._accepted_outcome is not None
        group_diffs = self._approved_repo_group_diffs()
        marker_present = self._accepted_outcome.decisions.get(_METADATA_REFRESH_ITEM_ID) == Decision.APPLY

        # Every keyring write this run owes, decided from the SOURCE decisions the user
        # already made — never from a decision about a key, which does not exist.
        keyring_writes = self._keyring_writes(self._surviving_keyring_refs())
        # "Remove keys after removing sources" is literal: with no source deletion in this
        # run nothing can have become unused, so the collection pass does not run at all.
        collect_unused = any(
            diff.item_class == ItemClass.APT_SOURCE and diff.action == DiffAction.REMOVE for diff in group_diffs
        )

        if not group_diffs and not keyring_writes:
            self._repo_group_outcome = (
                {_METADATA_REFRESH_ITEM_ID: (True, "no repository changes to refresh for")} if marker_present else {}
            )
            return

        # Populated incrementally (not built up in a local dict and assigned at the
        # end) so a later diff in THIS SAME group can consult an earlier diff's real
        # outcome while the group is still being written.
        self._repo_group_outcome = {}

        home = await self._target_home_dir()
        staging_dir = f"{home}/.cache/pc-switcher/apt-staging"
        backup_dir = f"{staging_dir}/backup-{uuid4().hex}"
        await self.target.run_command(
            f"mkdir --parents {shlex.quote(staging_dir)}",
            login_shell=False,
            mutates="create the apt repository-group staging directory",
        )

        existed_before: dict[str, bool] = {}
        try:
            for _local, dest in keyring_writes:
                existed_before[dest] = await self._backup_destination(dest, backup_dir)
            for diff in group_diffs:
                dest = _repo_item_destination(diff)
                existed_before[dest] = await self._backup_destination(dest, backup_dir)
        except ConvergeItemFailed as exc:
            # A backup failure aborts the whole group before any write happens (T-02-34
            # never partially applies), but `self._repo_group_outcome` must still end up
            # populated for every group item (D-27) — otherwise the idempotency guard at
            # the top of this method treats the group as "already handled" on the next
            # `converge()` call, and `_converge_repo_group_item`'s
            # `self._repo_group_outcome[diff.item_id]` raises a bare `KeyError` for every
            # item after the first, escaping the per-item `ConvergeItemFailed` handler and
            # crashing the whole job instead of failing one item.
            self._record_group_failure(group_diffs, marker_present, f"repository group backup failed: {exc}")
            return

        # Keys FIRST, before any source file is written: a repository whose keyring has
        # not landed is a repository apt refuses on every subsequent operation, and
        # `_require_keyrings_ready` turns that into a refusal to write the source at all.
        await self._provision_keyrings(keyring_writes, staging_dir)

        for diff in group_diffs:
            try:
                await self._write_or_remove_repo_item(diff, staging_dir)
                self._repo_group_outcome[diff.item_id] = (True, "converged")
            except ConvergeItemFailed as exc:
                self._repo_group_outcome[diff.item_id] = (False, str(exc))

        # Keys LAST, after every source write and deletion: what a keyring is worth is a
        # reference count over the target's REAL source files, and only now is that count
        # taken against the state the run actually produced.
        if collect_unused:
            await self._remove_unused_keyrings(backup_dir, existed_before)

        update_result = await self.target.run_command(
            "sudo apt-get update",
            login_shell=False,
            mutates="refresh apt package lists against the newly written repository configuration",
        )
        if update_result.success:
            # This IS the run's single metadata refresh (decision 1): flag it so the
            # install path's `_ensure_metadata_refreshed` is a no-op and never issues a
            # second `apt-get update`.
            self._metadata_refreshed = True
            await self.target.run_command(
                f"rm --recursive --force {shlex.quote(backup_dir)}",
                login_shell=False,
                mutates="discard the repository-group backup after a successful refresh",
            )
            if marker_present:
                self._repo_group_outcome[_METADATA_REFRESH_ITEM_ID] = (True, "apt-get update succeeded")
            return

        # Rollback (T-02-34): restore every file that existed before, delete every file
        # the group created, discard the backup directory, then re-probe apt so the
        # failure summary can tell the user whether the target recovered rather than
        # leaving them to guess.
        recovery = await self._rollback_repo_group(existed_before, backup_dir)
        self._log(
            Host.TARGET,
            LogLevel.ERROR,
            f"apt-get update failed after repository group writes; rolled back ({recovery}): "
            f"{update_result.stderr.strip()}",
            stderr=update_result.stderr,
        )

        # Every group item is recorded as a failure (D-27) — even ones whose own write
        # just succeeded above — because the rollback undid it: what actually landed on
        # the target is the pre-run state, not what this run intended.
        self._record_group_failure(
            group_diffs,
            marker_present,
            f"repository group rolled back after apt-get update failure ({recovery}): {update_result.stderr.strip()}",
        )

    async def _rollback_repo_group(self, existed_before: dict[str, bool], backup_dir: str) -> str:
        """Undo the repository group's writes and re-probe apt; return a short phrase
        describing how the target ended up, for the caller's failure summary (T-02-34).

        Restores every file that existed before the group ran, deletes every file the
        group created, discards the backup, then runs `apt-get update` so the summary can
        state whether the target recovered rather than leaving the user to guess.

        One command per file, each result inspected. A single `;`-joined command would
        present the `--confirm-each-command` gate one all-or-nothing prompt, but it
        collapses N exit codes into one and makes "which file failed to restore"
        unanswerable — and a failing rollback step is exactly when the user needs that
        file named. Every step is attempted regardless of earlier failures, so one
        unwritable destination cannot strand the remaining files in their post-run state.
        """
        rollback_failures: list[str] = []
        for dest, existed in existed_before.items():
            if existed:
                backup_path = _backup_path_for(backup_dir, dest)
                action = f"restore {dest} from {backup_path}"
                result = await self.target.run_command(
                    f"sudo install --owner=root --group=root --mode=0644 "
                    f"{shlex.quote(backup_path)} {shlex.quote(dest)}",
                    login_shell=False,
                    mutates=f"ROLLBACK: restore {dest} from backup",
                )
            else:
                action = f"delete {dest}, which this run created"
                result = await self.target.run_command(
                    f"sudo rm --force {shlex.quote(dest)}",
                    login_shell=False,
                    mutates=f"ROLLBACK: delete {dest}, which this run created",
                )
            if not result.success:
                rollback_failures.append(f"could not {action}: {result.stderr.strip()}")
                self._log(
                    Host.TARGET,
                    LogLevel.WARNING,
                    f"Rollback step failed — could not {action}: {result.stderr.strip()}",
                    stderr=result.stderr,
                )

        if rollback_failures:
            # The backup directory is deliberately NOT discarded: a failed restore means it
            # holds the only remaining copy of that file's pre-run content, so deleting it
            # would destroy exactly what manual recovery depends on. Name the path — the
            # user has to finish this by hand.
            self._log(
                Host.TARGET,
                LogLevel.WARNING,
                f"Repository-group rollback incomplete; the backup is kept at {backup_dir} on the target "
                f"so the affected file(s) can be restored by hand: {'; '.join(rollback_failures)}",
            )
        else:
            await self.target.run_command(
                f"rm --recursive --force {shlex.quote(backup_dir)}",
                login_shell=False,
                mutates="ROLLBACK: discard the repository-group backup directory",
            )

        reprobe = await self.target.run_command(
            "sudo apt-get update",
            login_shell=False,
            mutates="ROLLBACK: re-probe apt against the restored repository configuration",
        )
        if reprobe.success:
            # After rollback `/etc/apt` is the pre-run configuration and this reprobe refreshed
            # metadata for it; package installs that still run against that config (D-27 —
            # a repo-group rollback does not cancel package items) then need no further
            # `apt-get update`. If the reprobe itself failed, the flag stays unset and the
            # install path's own refresh attempt will surface the still-broken apt.
            self._metadata_refreshed = True

        if rollback_failures:
            # Takes precedence over the reprobe's verdict: an incomplete rollback leaves
            # /etc/apt as neither the pre-run nor the post-run configuration, which a green
            # `apt-get update` would otherwise mask.
            return (
                f"ROLLBACK INCOMPLETE, {len(rollback_failures)} file(s) left unrestored "
                f"(backup kept at {backup_dir}): {'; '.join(rollback_failures)}"
            )
        return "target apt recovered after rollback" if reprobe.success else "target apt still broken after rollback"

    def _record_group_failure(self, group_diffs: list[ItemDiff], marker_present: bool, message: str) -> None:
        """Mark every `group_diffs` item (and the metadata-refresh marker, if present)
        as failed with `message`. Shared by the backup-failure short-circuit and the
        post-rollback failure path so `self._repo_group_outcome` always ends up fully
        populated (D-27) — a partially-populated map makes a later `converge()` call
        for an un-recorded item raise `KeyError` instead of `ConvergeItemFailed`.
        """
        assert self._repo_group_outcome is not None
        for diff in group_diffs:
            self._repo_group_outcome[diff.item_id] = (False, message)
        if marker_present:
            self._repo_group_outcome[_METADATA_REFRESH_ITEM_ID] = (False, message)

    async def _backup_destination(self, dest: str, backup_dir: str) -> bool:
        """Back up `dest` into `backup_dir` if it currently exists on the target;
        returns whether it existed (so rollback knows restore-vs-delete per file).
        """
        quoted_dest = shlex.quote(dest)
        exists = await self.target.run_command(f"test -f {quoted_dest}", login_shell=False)
        if not exists.success:
            return False

        await self.target.run_command(
            f"mkdir --parents {shlex.quote(backup_dir)}",
            login_shell=False,
            mutates="create the repository-group backup directory",
        )
        backup_path = _backup_path_for(backup_dir, dest)
        result = await self.target.run_command(
            f"sudo cp --archive {quoted_dest} {shlex.quote(backup_path)}",
            login_shell=False,
            mutates=f"back up {dest} before the repository group is written",
        )
        if not result.success:
            raise ConvergeItemFailed(
                f"failed to back up {dest} before converging the repository group: {result.stderr.strip()}"
            )
        return True

    async def _write_or_remove_repo_item(self, diff: ItemDiff, staging_dir: str) -> None:
        """Converge one repository-group diff: `sudo rm --force` for a REMOVE, or
        `_stage_and_promote` for an INSTALL/CHANGE (T-02-35). A source file is gated on
        its keyrings having landed first (D-12).
        """
        dest = _repo_item_destination(diff)

        if diff.action == DiffAction.REMOVE:
            result = await self.target.run_command(
                f"sudo rm --force {shlex.quote(dest)}",
                login_shell=False,
                mutates=f"delete repository file {dest}",
            )
            if not result.success:
                raise ConvergeItemFailed(f"failed to remove {dest}: {result.stderr.strip()}")
            return

        if diff.item_class == ItemClass.APT_SOURCE:
            self._require_keyrings_ready(diff)

        staged_name = diff.item_id.replace(":", "_").replace("/", "_")
        await self._stage_and_promote(dest, dest, staging_dir, staged_name)

    async def _stage_and_promote(self, local: str, dest: str, staging_dir: str, staged_name: str) -> None:
        """Copy the SOURCE machine's `local` onto the target at `dest`, byte-for-byte
        (T-02-35). The two paths are the same for every `/etc/apt` file this job writes
        except a keyring the two machines keep in different directories, where the
        destination has to be the path the repository's `Signed-By:` actually names.

        `RemoteExecutor.send_file` is plain SFTP as the ordinary SSH user with no sudo path
        (`executor.py` around line 362) and cannot write into `/etc/apt` directly — bytes
        land under the target user's own `~/.cache` staging directory first, then `sudo
        install` promotes them with the right ownership/mode in one atomic step (no window
        where the file exists under `/etc/apt` owned by the wrong user, unlike a `mv` plus
        separate `chown`/`chmod`). The staging copy is removed in a `finally` so a failed
        promotion never leaves transferred key material sitting in the cache.
        """
        # `sources.list.d`, `preferences.d`, `apt.conf.d` and `trusted.gpg.d` ship with
        # the `apt` package, but `/etc/apt/keyrings` is a third-party convention that a
        # fresh Ubuntu 24.04 target does not have — `install` (unlike `install -D`)
        # never creates DEST's missing parent directories, so a per-repo key promotion
        # to a fresh machine would otherwise fail every time. `mkdir --parents --mode` only chmods
        # directories it actually creates (unlike `install --directory`, which would also chmod
        # the four directories that already exist), so this is a no-op everywhere except
        # the one directory this project actually needs to create.
        dest_dir = str(Path(dest).parent)
        mkdir_result = await self.target.run_command(
            f"sudo mkdir --parents --mode=0755 {shlex.quote(dest_dir)}",
            login_shell=False,
            mutates=f"create directory {dest_dir} for {dest}",
        )
        if not mkdir_result.success:
            raise ConvergeItemFailed(
                f"failed to prepare directory {dest_dir} for {dest}: {mkdir_result.stderr.strip()}"
            )

        staged_dest = f"{staging_dir}/{staged_name}"
        try:
            await self.target.send_file(
                Path(local), staged_dest, mutates=f"stage {dest} into the target's cache before promotion"
            )
            promote = await self.target.run_command(
                f"sudo install --owner=root --group=root --mode=0644 {shlex.quote(staged_dest)} {shlex.quote(dest)}",
                login_shell=False,
                mutates=f"promote the staged file into {dest} as root:root 0644",
            )
            if not promote.success:
                raise ConvergeItemFailed(f"failed to install {dest}: {promote.stderr.strip()}")
        finally:
            await self.target.run_command(
                f"rm --force {shlex.quote(staged_dest)}",
                login_shell=False,
                mutates=f"remove the staging copy of {dest}",
            )

    # -- Keyrings: two file operations bracketing the repository group ------------------
    #
    # Keys are not items (module docstring). Everything below is driven by the decisions
    # the user made about SOURCES, and nothing below ever asks a question, builds an
    # `ItemDiff`, or writes a decision file.

    async def _capture_package_owned_keys(
        self, target_run: Callable[[str], Awaitable[CommandResult]]
    ) -> frozenset[str]:
        """Absolute paths of the target's key files that the target's own dpkg owns, from
        ONE batched `dpkg --search` over every key file the target has (never one call per file —
        the `manual_installs_sync._scan_unowned_installs` shape).

        The exit code is deliberately ignored: `dpkg --search` returns non-zero as soon as ANY
        argument matches no package, which for a machine with even one hand-placed key is
        always. Ownership is read out of stdout, where each matched path arrives as
        `<package[, package...]>: <path>`; unmatched paths go to stderr and simply produce
        no entry, which is exactly the "unowned" answer.

        Read-only, no sudo: `dpkg --search` queries the local dpkg database.
        """
        paths = sorted(f"{directory}/{name}" for directory, digests in self._target_key_dirs() for name in digests)
        if not paths:
            return frozenset()
        result = await target_run(f"dpkg --search {' '.join(shlex.quote(path) for path in paths)}")
        owned: set[str] = set()
        for line in result.stdout.splitlines():
            _packages, separator, path = line.rpartition(": ")
            if separator and path.startswith("/"):
                owned.add(path.strip())
        return frozenset(owned)

    def _keyring_digests(self, ref: str) -> tuple[str | None, str | None]:
        """`(source digest, target digest)` for the key file a `Signed-By:` reference
        names, looked up by BASENAME across all three key directories (`_KEY_DIRS`).

        Basename rather than the full path because that is how `_dangling_keyring_ref`
        already resolves a reference, and the two must agree: a reference this method
        cannot resolve is exactly one that check already downgraded the repository for.
        """
        name = Path(ref).name
        source = next((digests[name] for _dir, digests in self._source_key_dirs() if name in digests), None)
        target = next((digests[name] for _dir, digests in self._target_key_dirs() if name in digests), None)
        return source, target

    def _source_key_dirs(self) -> tuple[tuple[str, dict[str, str]], ...]:
        """Each key directory paired with the SOURCE machine's digest map for it, in
        `_KEY_DIRS` order — the single place the three directories are enumerated, so
        adding or dropping one cannot be done in resolution but missed in provisioning.
        """
        maps = (self._source_keyrings, self._source_global_keys, self._source_shared_keys)
        return tuple(zip(_KEY_DIRS, maps, strict=True))

    def _target_key_dirs(self) -> tuple[tuple[str, dict[str, str]], ...]:
        maps = (self._target_keyrings, self._target_global_keys, self._target_shared_keys)
        return tuple(zip(_KEY_DIRS, maps, strict=True))

    def _keyring_local_path(self, ref: str) -> str | None:
        """Where the SOURCE machine keeps the key a reference names, or `None` when the
        source machine has no such key at all — D-12's dangling reference, already reported
        on the REPOSITORY item.
        """
        name = Path(ref).name
        return next((f"{directory}/{name}" for directory, digests in self._source_key_dirs() if name in digests), None)

    def _target_manages_keyring(self, ref: str) -> bool:
        """Whether the target already has the key `ref` names AND its own dpkg owns that
        path — the one case where a differing keyring is deliberately left alone.

        Not a general ownership gate (module docstring): a key the target LACKS is copied
        whatever owns it on the source, because a vendor `.deb` that ships both a repository
        entry and the keyring trusting it cannot be installed until that keyring is present.
        """
        _source_digest, target_digest = self._keyring_digests(ref)
        return target_digest is not None and ref in self._target_package_owned_keys

    def _keyring_writes(self, refs: frozenset[str]) -> list[tuple[str, str]]:
        """`(local path, target destination)` for every keyring this run must copy, given
        the set of references that will be live on the target.

        Content-based, not presence-based: a key already on the target whose bytes differ
        from the source machine's is copied too. That is what keeps a ROTATED key correct —
        the vendor's new key changes no source FILE, so nothing else in the run would ever
        notice, and the target's apt would fail that repository's signature check.

        Two populations, one rule ("the target's copy matches the source machine's"):

        - Every `/etc/apt/trusted.gpg.d` key the source has. Nothing references these —
          they are ambient trust — so a reference count cannot select among them and their
          own content is the only signal there is.
        - The `/etc/apt/keyrings` and `/usr/share/keyrings` files that `refs` actually
          names. Neither directory is mirrored wholesale: a keyring no source on the target
          points at is litter, and `/usr/share/keyrings` is mostly the distro's own.

        Overwriting is ownership-aware, copying is not (module docstring): a key the target
        already has with different bytes is skipped when the target's dpkg owns that path,
        while a key the target LACKS is always copied — including a package-owned one,
        which is the only way a repository whose keyring ships inside a package it hosts can
        ever be bootstrapped.

        A destination is emitted at most once, so one rotated key serving three
        repositories is still exactly one write.
        """
        writes: dict[str, str] = {}
        for name, digest in self._source_global_keys.items():
            dest = f"{_APT_TRUSTED_GPG_DIR}/{name}"
            if self._target_global_keys.get(name) == digest or self._target_manages_keyring(dest):
                continue
            writes[dest] = dest
        writes.update(self._referenced_keyring_writes(refs))
        return [(writes[dest], dest) for dest in sorted(writes)]

    def _referenced_keyring_writes(self, refs: Iterable[str]) -> dict[str, str]:
        """`{destination: local path}` for the subset of `refs` whose key this run must
        copy — the reference-driven half of `_keyring_writes`, without the ambient
        `/etc/apt/trusted.gpg.d` population.

        Split out because it is also what a single source file's review detail may name
        (`build_travelling_keyrings_detail`): the keys that travel BECAUSE of that file.
        The global keys travel regardless of any source file, so attributing them to one
        would name the same key on every repository in the review.
        """
        writes: dict[str, str] = {}
        for ref in refs:
            local = self._keyring_local_path(ref)
            if local is None:
                # The source machine has no such key. That is D-12's dangling reference,
                # already reported on the REPOSITORY item; inventing a key here is exactly
                # what "never re-fetched from a vendor" forbids.
                continue
            source_digest, target_digest = self._keyring_digests(ref)
            if source_digest == target_digest or self._target_manages_keyring(ref):
                continue
            writes[ref] = local
        return writes

    def _surviving_keyring_refs(self) -> frozenset[str]:
        """Every keyring reference that will be live on the target once this run's approved
        source decisions have been applied.

        Three populations, and getting any of them wrong provisions or deletes the wrong
        key: source files this run WRITES contribute the SOURCE machine's references (a
        changed repository may point somewhere new); source files this run REMOVES
        contribute nothing (their keyring is about to be collected, not refreshed); every
        other source file on the target — untouched, unticked, recorded machine-specific,
        or never synced at all like `/etc/apt/sources.list` — contributes the references it
        currently carries.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        decisions = self._accepted_outcome.decisions
        written: set[str] = set()
        removed: set[str] = set()
        for diff in self._accepted_plan.diffs:
            if diff.item_class != ItemClass.APT_SOURCE or diff.item_id == _METADATA_REFRESH_ITEM_ID:
                continue
            if decisions.get(diff.item_id) != Decision.APPLY:
                continue
            filename = diff.item_id.removeprefix("apt:source:")
            if diff.action == DiffAction.REMOVE:
                removed.add(filename)
            elif diff.action in (DiffAction.INSTALL, DiffAction.CHANGE):
                written.add(filename)

        refs: set[str] = set()
        for filename, (target_refs, _uris) in self._target_source_refs.items():
            if filename not in removed and filename not in written:
                refs.update(target_refs)
        for filename in written:
            refs.update(self._source_keyring_refs.get(filename, ()))
        return frozenset(refs)

    def _pending_keyring_work(self) -> bool:
        """Whether ANY keyring could need writing this run, judged before the user has
        decided anything — the trigger that lets the repository group run for a rotated key
        whose source file is byte-identical and therefore produces no diff at all.

        Deliberately a superset: it counts the references of every source file on the target
        plus those of every file a diff implicates, because which of them survive is not yet
        known. A false positive costs nothing — `_ensure_repo_group_converged` recomputes the
        exact set from the real decisions and returns early when it turns out to be empty.
        """
        refs = frozenset(ref for refs, _uris in self._target_source_refs.values() for ref in refs) | frozenset(
            ref for refs in self._source_keyring_refs.values() for ref in refs
        )
        return bool(self._keyring_writes(refs))

    async def _provision_keyrings(self, writes: Sequence[tuple[str, str]], staging_dir: str) -> None:
        """Copy each planned keyring onto the target, recording the destinations that
        landed so `_require_keyrings_ready` can let their repositories be written.

        A failure here fails no ITEM — there is no key item to fail. It is logged and the
        destination is simply left out of `_provisioned_keyrings`, which makes every source
        file referencing that keyring refuse its own write with a message naming the key.
        That is the D-12 outcome either way, reported against the thing the user reviewed.
        """
        for local, dest in writes:
            try:
                await self._stage_and_promote(local, dest, staging_dir, dest.lstrip("/").replace("/", "_"))
            except ConvergeItemFailed as exc:
                self._log(
                    Host.TARGET, LogLevel.ERROR, f"failed to provision signing key {dest}: {exc}", stderr=str(exc)
                )
                continue
            self._provisioned_keyrings.add(dest)

    async def _remove_unused_keyrings(self, backup_dir: str, existed_before: dict[str, bool]) -> None:
        """Delete every `/etc/apt/keyrings` file on the target that no surviving source
        references — the garbage-collection half of transparent key handling.

        Called only after every source write and deletion in the group, and only when this
        run removed at least one source file. The reference count comes from a FRESH scan of
        the target's real source files, which is what makes the two cases the user cares
        about come out right without a guard of their own: a repository this run deleted has
        stopped referencing its key, and one whose deletion the user declined — or that
        failed to be deleted — still references it and keeps it alive.

        Scoped to `/etc/apt/keyrings`, the one directory that exists purely for this. Legacy
        `/etc/apt/trusted.gpg.d` keys are ambient trust that nothing references by
        construction, so "unused" is not computable for them; `/usr/share/keyrings` is
        package territory and holds keys the distro's own tooling put there. Both are left
        to accumulate rather than deleted on a guess.

        Each deletion is backed up into the group's own backup directory first and recorded
        in `existed_before`, so a failing `apt-get update` rolls a collected key back with
        everything else. A key that cannot be backed up is not deleted: without the backup a
        rollback could not restore it, and an unused keyring costs nothing to keep.
        """

        async def target_run(cmd: str) -> CommandResult:
            return await self.target.run_command(cmd, login_shell=False)

        candidates = frozenset(self._target_keyrings) - frozenset(self._source_keyrings)
        if not candidates:
            return
        references = await _scan_source_file_references(target_run)
        referenced = {Path(ref).name for refs, _uris in references.values() for ref in refs}

        for filename in sorted(candidates - referenced):
            dest = f"{_APT_KEYRINGS_DIR}/{filename}"
            try:
                existed = await self._backup_destination(dest, backup_dir)
            except ConvergeItemFailed as exc:
                self._log(
                    Host.TARGET,
                    LogLevel.WARNING,
                    f"keeping unused signing key {dest}: it could not be backed up first ({exc})",
                )
                continue
            if not existed:
                continue
            existed_before[dest] = True
            result = await self.target.run_command(
                f"sudo rm --force {shlex.quote(dest)}",
                login_shell=False,
                mutates=f"delete signing key {dest}, which no repository references any more",
            )
            if not result.success:
                self._log(
                    Host.TARGET,
                    LogLevel.WARNING,
                    f"could not delete unused signing key {dest}: {result.stderr.strip()}",
                    stderr=result.stderr,
                )

    def _require_keyrings_ready(self, diff: ItemDiff) -> None:
        """Refuse to write a source file whose keyring is neither already byte-identical on
        the target nor among the keys this run just provisioned (D-12) — a repository
        written without its key is a repository apt refuses on every subsequent operation,
        which makes writing it anyway strictly worse than leaving the target alone.

        Reads the references `_diff_apt_sources` already parsed from the SOURCE machine's
        copy of this file, which is the copy about to be written.

        A key the target has and its own dpkg owns counts as ready even though this run
        deliberately did not overwrite it: the target's package manages that file, so the
        repository is trusted there — refusing the write would strand a repository over a
        key difference the run chose not to touch.
        """
        filename = diff.item_id.removeprefix("apt:source:")
        for ref in self._source_keyring_refs.get(filename, ()):
            if ref in self._provisioned_keyrings:
                continue
            source_digest, target_digest = self._keyring_digests(ref)
            if source_digest is not None and source_digest == target_digest:
                continue
            if self._target_manages_keyring(ref):
                continue
            raise ConvergeItemFailed(
                f"source {filename} references keyring {ref!r}, which is neither already "
                "present on the target with the source's own bytes nor among the keys this run "
                "provisioned (D-12/T-02-16); skipping this repository write"
            )

    async def _target_home_dir(self) -> str:
        """The target user's home directory, resolved once per run via `echo $HOME`
        (`config_sync._copy_config_to_target`'s established pattern) and cached — every
        repository-group file write needs the same absolute staging path.
        """
        if self._target_home is None:
            result = await self.target.run_command("echo $HOME", login_shell=False)
            self._target_home = result.stdout.strip()
        return self._target_home

    @override
    async def validate(self) -> list[ValidationError]:
        """apt-mark availability on both ends, sudo on both ends, dpkg lock free on target.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `folder_sync.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        source_check = await self.source.run_command("apt-mark --version")
        if not source_check.success:
            errors.append(self._validation_error(Host.SOURCE, "apt-mark is not available on source"))

        target_check = await self.target.run_command("apt-mark --version", login_shell=False)
        if not target_check.success:
            errors.append(self._validation_error(Host.TARGET, "apt-mark is not available on target"))

        # Source-side sudo matters even though the source is never mutated: capturing
        # /etc/apt config runs `sudo find` there, and without passwordless sudo that
        # capture degrades to empty digest maps rather than failing. The sync would then
        # report success having replicated no repository configuration at all — a silent
        # wrong-result, which is worse than refusing to start.
        source_sudo_check = await self.source.run_command("sudo --non-interactive true")
        if not source_sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "passwordless sudo is not available on source "
                    "(required to read /etc/apt repository, keyring and pin config).\n"
                    + passwordless_sudo_hint(_SOURCE_SUDO_COMMANDS),
                )
            )

        sudo_check = await self.target.run_command("sudo --non-interactive true", login_shell=False)
        if not sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "passwordless sudo is not available on target "
                    "(required to install packages and write /etc/apt config).\n"
                    + passwordless_sudo_hint(_TARGET_SUDO_COMMANDS, user=self.context.target_username),
                )
            )

        # fuser exits 0 when the file IS held by at least one process, non-zero when
        # free (man fuser EXIT CODES) — read-only probe, no lock is acquired or released.
        lock_check = await self.target.run_command("sudo fuser /var/lib/dpkg/lock-frontend", login_shell=False)
        if lock_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "dpkg frontend lock is held on target (likely unattended-upgrades); "
                    "retry once it finishes (RESEARCH Pitfall 5)",
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): the manual-install set."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["apt packages (manually-installed set)"],
            mechanism="apt-get install/remove per item, after review",
        )


_COLLATERAL_ID_PREFIX = "apt:collateral:"


def _is_collateral_diff(diff: ItemDiff) -> bool:
    """A manual-collateral item, identified by its stable id prefix (D-30). These carve
    into their own `COLLATERAL_REVIEW_ACTION` group rather than a checkbox group."""
    return diff.item_id.startswith(_COLLATERAL_ID_PREFIX)


def _collateral_diff(name: str, effect: str) -> ItemDiff:
    """One manual-collateral item (D-30): a manually-installed package the pending apt
    transaction would remove or downgrade. Stays `REPORT_ONLY` so `apply()` never
    converges it directly — its decision governs the triggering install, not itself.
    """
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REPORT_ONLY,
        item_id=f"{_COLLATERAL_ID_PREFIX}{name}",
        label=name,
        detail=f"manually-installed package that apt's own simulation says {effect}",
    )
