"""`apt_sync`: apt package convergence — install, remove, and the full diff taxonomy
(D-01, D-03, D-04, D-07, D-24, D-25, ADR-020).

Captures the source's `apt-mark showmanual` set with `dpkg-query`-sourced versions
(never `apt list --installed` — its own manpage says the output has no stable scripting
contract), diffs it against the same query on the target into every D-25 class
(`PackageSyncJob._diff_apt_packages`), and converges the approved `INSTALL`/`REMOVE`
items via `apt-get install`/`apt-get remove`.

Every approved item's transaction is simulated with `apt-get -s` before the real
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

A signing key is NOT an item (ADR-020's 2026-07-27 amendment). It has no `ItemClass`, no
`item_id`, no diff, no review entry and no decision-file identity: the user thinks in
repositories and packages, and a key is only how a repository is made to work. Keys are
therefore two plain file operations bracketing the repository group, both driven by the
decisions the user already made about SOURCES:

- `_provision_keyrings` runs BEFORE any source file is written. It copies every source
  `/etc/apt/trusted.gpg.d` key the target lacks or differs on, and the keyrings named by
  the source files this run actually writes (INSTALL and CHANGE alike — a changed source
  may point at a keyring the target has never seen). `_require_keyrings_ready` still
  refuses to write a source whose keyring did not arrive, so a repository is never written
  ahead of its key.
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

Known limitation: because provisioning is driven by source-file writes, a keyring the
vendor ROTATED while its source file stayed byte-identical never travels, and the target's
apt keeps failing that repository's signature check until something else changes the source
file. Keys still travel byte-for-byte and are never re-fetched from a vendor (D-12).

Apt sources/keys/pins/config, and the other two managers (snap, flatpak), are later
Phase 2 plans.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, override
from uuid import uuid4

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import (
    AptConfigItem,
    AptPackageItem,
    AptPinItem,
    AptSourceItem,
    DiffAction,
    DiffClass,
    HoldPinFact,
    ItemClass,
    ItemDiff,
    build_dangling_keyring_detail,
    build_orphaned_packages_detail,
    build_version_mismatch_detail,
    compare_deb_versions,
)
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
)
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
_SOURCE_SUDO_COMMANDS = ("/usr/bin/find",)
_TARGET_SUDO_COMMANDS = (
    "/usr/bin/apt-get",
    "/usr/bin/apt-mark",
    "/usr/bin/find",
    "/usr/bin/install",
    "/usr/bin/cp",
    "/usr/bin/rm",
    "/usr/bin/fuser",
)

# The five `/etc/apt/*` directories D-11/D-13 pull into scope, each captured with one
# batched `sha256sum` listing (never one command per file).
_APT_SOURCES_DIR = "/etc/apt/sources.list.d"
# apt's other source location. NOT an item class — this file is never captured, diffed or
# written by pc-switcher — but it is scanned for keyring references, because a keyring named
# only here is still in use and deleting it would break apt. It is the clearest instance of
# "a source file this tool does not sync still counts as a reference".
_APT_SOURCES_LIST = "/etc/apt/sources.list"
_APT_KEYRINGS_DIR = "/etc/apt/keyrings"
_APT_TRUSTED_GPG_DIR = "/etc/apt/trusted.gpg.d"
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

# Matches one `apt-get -s` transaction line: `Inst <name> [<old>] (<new> ...)` for an
# install/upgrade (the `[<old>]` bracket only appears when a version is already
# installed), or `Remv <name> [<old>]` for a removal. Parsed by leading verb token and
# named groups rather than fixed column positions — the rest of an apt-get -s line's
# shape varies with the package and its dependency resolution.
_TRANSACTION_LINE_RE = re.compile(
    r"^(?P<verb>Inst|Remv)\s+(?P<name>\S+)"
    r"(?:\s+\[(?P<old_version>[^\]]+)\])?"
    r"(?:\s+\((?P<new_version>[^\s)]+)\)?)?"
)


def _package_name(item_id: str) -> str:
    if not item_id.startswith(_APT_PACKAGE_ID_PREFIX):
        raise ValueError(f"Not an apt package item id: {item_id!r}")
    return item_id.removeprefix(_APT_PACKAGE_ID_PREFIX)


def _lines(output: str) -> list[str]:
    """Non-blank, stripped lines — the shape every `apt-mark`/`find` list command in
    this module produces."""
    return [line.strip() for line in output.splitlines() if line.strip()]


def _packages_with_no_candidate(policy_output: str) -> set[str]:
    """Parse a multi-package `apt-cache policy <name...>` run: names whose `Candidate:`
    line reads `(none)`. Each package's block starts with an unindented `<name>:`
    header line, per `apt-cache policy`'s documented output shape.
    """
    no_candidate: set[str] = set()
    current: str | None = None
    for line in policy_output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            current = line[:-1]
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            if stripped.removeprefix("Candidate:").strip() == "(none)":
                no_candidate.add(current)
            current = None
    return no_candidate


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


def _normalise_repo_uri(uri: str) -> str:
    """A repository URI reduced to the shape `apt-cache policy` prints in its version
    table: apt strips the trailing slash a source file may carry
    (`https://packages.microsoft.com/repos/azure-cli/` -> `.../azure-cli`), so comparing
    the two forms verbatim would miss every repo written with one.
    """
    return uri.rstrip("/")


def _parse_source_file(
    filename: str, content: str
) -> tuple[Literal["deb822", "list"], tuple[str, ...], tuple[str, ...]]:
    """A source file's format (by extension), every keyring path it names, and every
    repository URI it points at (normalised by `_normalise_repo_uri`).

    deb822 `.sources` files name a key via a `Signed-By:` field and their repositories via
    `URIs:`; legacy `.list` files put both on the `deb` line, the key inside the options
    bracket as `[... signed-by=<path> ...]` and the URI immediately after it (RESEARCH
    Standard Stack). Parsed just far enough to extract these — never rewritten,
    normalised, or migrated between formats (RESEARCH Pitfall 3, deferred ideas).

    One parser, three consumers: the keyring refs drive D-12's dangling-reference check
    and keyring garbage collection, the URIs drive the source-removal impact (C26) by
    matching against the origin `apt-cache policy` reports for an installed package.

    A `Signed-By:` field may carry an INLINE armored key instead of a path — the field
    value is empty and the armored block follows on continuation lines. That yields NO
    ref, which is correct in both directions: the file depends on no key FILE (so nothing
    is invented for it), and the armored block's own lines are not mistaken for a path (so
    no real keyring is made to look referenced by it either).
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
                uris.extend(_normalise_repo_uri(uri) for uri in uri_field.group("uris").split())
        else:
            signed_by = _LEGACY_SIGNED_BY_RE.search(raw_line)
            deb_line = _LEGACY_DEB_LINE_RE.match(line)
            if deb_line:
                uris.append(_normalise_repo_uri(deb_line.group("uri")))
        if signed_by:
            refs.append(signed_by.group("path"))
    return fmt, tuple(refs), tuple(uris)


def _installed_origins_by_package(policy_output: str) -> dict[str, frozenset[str]]:
    """Parse a batched `apt-cache policy <name...>` run into `{package: origin URIs of
    its INSTALLED version}` (C26).

    `apt-cache policy`'s version table marks the installed version with a leading `***`
    and indents each of that version's origins by eight spaces as
    `<priority> <uri> <suite>/<component> <arch> Packages`. Only the installed version's
    origins count: another version row may list a repository that merely *offers* the
    package (Ubuntu's archive offers `gh` too), which is not the repository the target is
    actually tracking. `/var/lib/dpkg/status` is dpkg's own record of the installed
    package, not a repository, and is skipped.

    Defensive by construction: a name apt does not know produces no block at all, and a
    package installed from a local `.deb` has `/var/lib/dpkg/status` as its only origin.
    Both degrade to "no origin" -> no link found, never to a guess.
    """
    origins: dict[str, set[str]] = {}
    current: str | None = None
    in_installed_block = False
    for line in policy_output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            current, in_installed_block = line[:-1], False
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("***"):
            in_installed_block = True
            continue
        if line.startswith("        "):
            parts = stripped.split()
            if in_installed_block and len(parts) >= 2 and parts[1] != "/var/lib/dpkg/status":
                origins.setdefault(current, set()).add(_normalise_repo_uri(parts[1]))
            continue
        # Any other row at version-table depth is the next (non-installed) version.
        in_installed_block = False
    return {name: frozenset(uris) for name, uris in origins.items()}


def _parse_pin_file(content: str) -> tuple[str, ...]:
    """Every package name named by a `Package:` stanza line in a `preferences.d` file.

    A stanza's `Package:` line may name several packages space-separated; all of them
    are pinned packages, not just the first (unlike the existing `collect_hold_pin_facts`
    awk one-liner, which only needs one representative name per fact).
    """
    packages: list[str] = []
    for line in content.splitlines():
        match = _PIN_PACKAGE_RE.match(line.strip())
        if match:
            packages.extend(match.group("packages").split())
    return tuple(packages)


def _dangling_keyring_ref(keyring_refs: Sequence[str], source_key_filenames: frozenset[str]) -> str | None:
    """The first `keyring_refs` entry whose basename is absent from
    `source_key_filenames`, or `None` if every reference resolves to a real file on the
    source. A source file with no `Signed-By:`/`signed-by=` at all (`keyring_refs` is
    empty) has nothing to validate — it is not itself a dangling reference.
    """
    for ref in keyring_refs:
        if Path(ref).name not in source_key_filenames:
            return ref
    return None


async def _capture_dir_digests(run: Callable[[str], Awaitable[CommandResult]], directory: str) -> dict[str, str]:
    """One `sudo find <dir> -maxdepth 1 -type f -exec sha256sum {} +` per directory —
    a single batched command, never one `sha256sum` per file. `-exec ... {} +` never
    runs at all when the directory has no matching files, so an empty/absent directory
    degrades to an empty digest map rather than a shell error.
    """
    quoted = shlex.quote(directory)
    result = await run(f"sudo find {quoted} -maxdepth 1 -type f -exec sha256sum {{}} +")
    return _parse_sha256sum(result.stdout)


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


async def _scan_target_source_references(
    run: Callable[[str], Awaitable[CommandResult]],
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """`{filename: (keyring_refs, repository URIs)}` for EVERY source file on a machine,
    from ONE batched command — `sources.list.d` AND `/etc/apt/sources.list`.

    Two consumers, both of which need a fact no diff carries. The source-removal impact
    (C26) needs the repository URIs of a file whose deletion is offered. Keyring garbage
    collection needs the reference count of a key across every source file that exists,
    which is emphatically not the set of files any diff implicates: a keyring is commonly
    named only by files that are byte-identical on both machines, or that the user marked
    machine-specific, or — `/etc/apt/sources.list` — that pc-switcher never syncs at all.
    Missing any of those would delete a key that is still in use.

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
    """The parsed result of `apt-get -s <args>` — what apt says it WOULD do.

    `apt-get -s` is the only honest answer to "what will this command do": apt resolves
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
    """Run `apt-get -s <apt_args>` on `executor` and parse its Inst/Remv action lines.

    No `sudo` is needed: simulation is read-only. Raises `ConvergeItemFailed` if the
    simulation itself fails (dpkg lock contention, unmet dependencies, a transient
    apt-cache read error): a failed `apt-get -s` typically prints no Inst/Remv lines,
    which would otherwise parse as an indistinguishable-from-clean empty preview and
    let both call sites proceed with a real command whose simulation was never
    actually trustworthy (WR-01) — refuse rather than silently degrade.
    """
    result = await executor.run_command(f"apt-get -s {apt_args}", login_shell=login_shell)
    if not result.success:
        raise ConvergeItemFailed(f"apt-get -s {apt_args} failed: {result.stderr.strip()}")
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
        # Absolute target paths `_provision_keyrings` successfully wrote this run. A source
        # file may only be written once every keyring it references is either already
        # byte-identical on the target or in here (`_require_keyrings_ready`).
        self._provisioned_keyrings: set[str] = set()
        # `{filename: (keyring_refs, repository URIs)}` for every source file ON THE TARGET,
        # captured once per `plan()` from one batched scan. This — not the diff — is what
        # says which keyrings matter: a keyring is commonly named only by files that are
        # byte-identical on both machines and so produce no diff at all.
        self._target_source_refs: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
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

    @override
    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        """Manually-installed apt packages on the source, with versions (D-03).

        Also records the source's raw `apt-mark showmanual` names into
        `self._source_manual_set` — captured here, before any decision-file filtering, so
        a package the user chose to skip on the source still counts as source-manual for
        collateral protection (decision 8, the SOURCE half of `_protected_manual_set`).
        """
        manual = await self.source.run_command("apt-mark showmanual")
        self._source_manual_set = frozenset(_lines(manual.stdout))
        return await self._resolve_versions(manual.stdout, self.source.run_command)

    @override
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
        versions_result = await run("dpkg-query -W -f='${Package}\\t${Version}\\n' " + quoted)

        versions: dict[str, str] = {}
        for line in versions_result.stdout.splitlines():
            if not line.strip():
                continue
            pkg_name, _, version = line.partition("\t")
            versions[pkg_name] = version

        return [AptPackageItem(name=name, version=versions.get(name, "")) for name in names]

    @override
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
        facts: list[HoldPinFact] = []

        # `find ... -exec ... {} +` passes every matching file to one awk invocation
        # (not a per-file command); if the directory has no files, -exec never runs,
        # so an empty preferences.d produces empty output rather than a shell error.
        pins = await self.target.run_command(
            "find /etc/apt/preferences.d -maxdepth 1 -type f -exec awk '/^Package:/{print FILENAME \"\\t\" $2}' {} +",
            login_shell=False,
        )
        for line in _lines(pins.stdout):
            filename, _, package = line.partition("\t")
            if package:
                facts.append(HoldPinFact(mechanism="pin", package=package, source_ref=filename))

        return facts

    @override
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

    @override
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
        no_candidate = _packages_with_no_candidate(result.stdout)
        return frozenset(f"{_APT_PACKAGE_ID_PREFIX}{name}" for name in names if name in no_candidate)

    @override
    async def plan(self) -> PackagePlan:
        """Extends the base diff (missing/extra/mismatch/held/unavailable) with
        plan-time apt transaction-collateral classification (D-30) and the four
        `/etc/apt/*` repository item classes (D-11/D-12/D-13).

        Unreproducible detection is NOT apt's business (D-18): it moved to
        `manual_installs_sync` with its own enable flag, so this job never emits an
        `UNREPRODUCIBLE` diff.

        Runs AFTER the base diff and BEFORE review groups are (re)built. The batched
        apt-get -s simulations reveal what the pending transaction would also remove or
        downgrade; each such package is split by provenance against the target's
        `apt-mark showmanual` set (captured here, once). An auto-installed collateral
        package is apt resolving its own dependencies — it proceeds silently, producing no
        review item. A manually-installed collateral package is something the user chose to
        have, so it becomes its own three-way review item (install-anyway / skip / abort)
        decided at plan time, in the SAME review the user approves from — never a prompt
        during apply.
        """
        base_plan = await super().plan()
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

        source_sources = await _capture_dir_digests(source_run, _APT_SOURCES_DIR)
        target_sources = await _capture_dir_digests(target_run, _APT_SOURCES_DIR)
        source_per_repo_keys = await _capture_dir_digests(source_run, _APT_KEYRINGS_DIR)
        target_per_repo_keys = await _capture_dir_digests(target_run, _APT_KEYRINGS_DIR)
        source_global_keys = await _capture_dir_digests(source_run, _APT_TRUSTED_GPG_DIR)
        target_global_keys = await _capture_dir_digests(target_run, _APT_TRUSTED_GPG_DIR)
        source_pins = await _capture_dir_digests(source_run, _APT_PREFERENCES_DIR)
        target_pins = await _capture_dir_digests(target_run, _APT_PREFERENCES_DIR)
        source_configs = await _capture_dir_digests(source_run, _APT_CONF_DIR)
        target_configs = await _capture_dir_digests(target_run, _APT_CONF_DIR)

        self._source_keyrings = source_per_repo_keys
        self._target_keyrings = target_per_repo_keys
        self._source_global_keys = source_global_keys
        self._target_global_keys = target_global_keys
        source_key_filenames = frozenset(source_per_repo_keys) | frozenset(source_global_keys)

        # Unconditional, one batched command: which keyrings the target's sources point at
        # is what makes a key correct, and that is a property of EVERY source file on the
        # target, not just the ones a diff implicates. `_source_removal_details` reuses it.
        self._target_source_refs = await _scan_target_source_references(target_run)
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
        origins_by_package = _installed_origins_by_package(policy.stdout)
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
                        detail=None,
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
                self.target, f"install -y --no-install-recommends {quoted}", login_shell=False
            )
            trigger_ids = frozenset(f"{_APT_PACKAGE_ID_PREFIX}{name}" for name in install_names)
            collateral.extend(await self._classify_collateral(preview, reviewed_names, trigger_ids, verb="installing"))
        if remove_names:
            quoted = " ".join(shlex.quote(name) for name in remove_names)
            preview = await simulate_apt_transaction(self.target, f"remove -y {quoted}", login_shell=False)
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
        install_args = f"install -y --no-install-recommends {quoted}"

        preview = await simulate_apt_transaction(self.target, install_args, login_shell=False)

        protected = self._protected_manual_set()
        refused = [pkg for pkg in preview.removals if pkg in protected and pkg not in self._approved_collateral]
        if refused:
            removed = ", ".join(refused)
            raise ConvergeItemFailed(
                f"install of {name} refused: apt-get -s would remove manually-installed {removed}, "
                "which was not approved as collateral in this run (D-30)"
            )

        for pkg, (old_version, new_version) in preview.install_versions.items():
            if old_version is None or pkg not in protected or pkg in self._approved_collateral:
                continue
            if await compare_deb_versions(self.target, new_version, old_version) < 0:
                raise ConvergeItemFailed(
                    f"install of {name} refused: apt-get -s would downgrade manually-installed {pkg} "
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
        remove_args = f"remove -y {quoted}"

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
                f"removal of {name} refused: apt-get -s would also remove manually-installed {removed}, "
                "which was neither an approved removal nor approved as collateral in this run (D-30)"
            )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {remove_args}"
        return await self.target.run_command(real_cmd, login_shell=False, mutates=f"remove apt package {name}")

    async def _converge_hold(self, diff: ItemDiff) -> CommandResult:
        """Converge one `apt:hold:<name>` membership item (#208, D4/D5): `apt-mark hold`
        for the add direction (INSTALL), `apt-mark unhold` for the remove direction
        (REMOVE). Selection state only — no `apt-get -s` simulation and no transaction
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
            f"mkdir -p {shlex.quote(staging_dir)}",
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
                f"rm -rf {shlex.quote(backup_dir)}",
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
                    f"sudo install -o root -g root -m 0644 {shlex.quote(backup_path)} {shlex.quote(dest)}",
                    login_shell=False,
                    mutates=f"ROLLBACK: restore {dest} from backup",
                )
            else:
                action = f"delete {dest}, which this run created"
                result = await self.target.run_command(
                    f"sudo rm -f {shlex.quote(dest)}",
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
                f"rm -rf {shlex.quote(backup_dir)}",
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
            f"mkdir -p {shlex.quote(backup_dir)}",
            login_shell=False,
            mutates="create the repository-group backup directory",
        )
        backup_path = _backup_path_for(backup_dir, dest)
        result = await self.target.run_command(
            f"sudo cp -a {quoted_dest} {shlex.quote(backup_path)}",
            login_shell=False,
            mutates=f"back up {dest} before the repository group is written",
        )
        if not result.success:
            raise ConvergeItemFailed(
                f"failed to back up {dest} before converging the repository group: {result.stderr.strip()}"
            )
        return True

    async def _write_or_remove_repo_item(self, diff: ItemDiff, staging_dir: str) -> None:
        """Converge one repository-group diff: `sudo rm -f` for a REMOVE, or
        `_stage_and_promote` for an INSTALL/CHANGE (T-02-35). A source file is gated on
        its keyrings having landed first (D-12).
        """
        dest = _repo_item_destination(diff)

        if diff.action == DiffAction.REMOVE:
            result = await self.target.run_command(
                f"sudo rm -f {shlex.quote(dest)}",
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
        # to a fresh machine would otherwise fail every time. `mkdir -p -m` only chmods
        # directories it actually creates (unlike `install -d`, which would also chmod
        # the four directories that already exist), so this is a no-op everywhere except
        # the one directory this project actually needs to create.
        dest_dir = str(Path(dest).parent)
        mkdir_result = await self.target.run_command(
            f"sudo mkdir -p -m 0755 {shlex.quote(dest_dir)}",
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
                f"sudo install -o root -g root -m 0644 {shlex.quote(staged_dest)} {shlex.quote(dest)}",
                login_shell=False,
                mutates=f"promote the staged file into {dest} as root:root 0644",
            )
            if not promote.success:
                raise ConvergeItemFailed(f"failed to install {dest}: {promote.stderr.strip()}")
        finally:
            await self.target.run_command(
                f"rm -f {shlex.quote(staged_dest)}",
                login_shell=False,
                mutates=f"remove the staging copy of {dest}",
            )

    # -- Keyrings: two file operations bracketing the repository group ------------------
    #
    # Keys are not items (module docstring). Everything below is driven by the decisions
    # the user made about SOURCES, and nothing below ever asks a question, builds an
    # `ItemDiff`, or writes a decision file.

    def _keyring_digests(self, ref: str) -> tuple[str | None, str | None]:
        """`(source digest, target digest)` for the key file a `Signed-By:` reference
        names, looked up by BASENAME across both key directories.

        Basename rather than the full path because that is how `_dangling_keyring_ref`
        already resolves a reference, and the two must agree: a reference this method
        cannot resolve is exactly one that check already downgraded the repository for.
        """
        name = Path(ref).name
        source = self._source_keyrings.get(name) or self._source_global_keys.get(name)
        target = self._target_keyrings.get(name) or self._target_global_keys.get(name)
        return source, target

    def _keyring_local_path(self, ref: str) -> str | None:
        """Where the SOURCE machine keeps the key a reference names, or `None` when it
        keeps it in neither directory this job manages (a package-owned keyring under
        `/usr/share/keyrings`, say, which the target's own packages provide).
        """
        name = Path(ref).name
        if name in self._source_keyrings:
            return f"{_APT_KEYRINGS_DIR}/{name}"
        if name in self._source_global_keys:
            return f"{_APT_TRUSTED_GPG_DIR}/{name}"
        return None

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
        - The `/etc/apt/keyrings` files that `refs` actually names. The whole directory is
          deliberately NOT mirrored: a keyring no source on the target points at is litter,
          not configuration.

        A destination is emitted at most once, so one rotated key serving three
        repositories is still exactly one write.
        """
        writes: dict[str, str] = {}
        for name, digest in self._source_global_keys.items():
            if self._target_global_keys.get(name) != digest:
                dest = f"{_APT_TRUSTED_GPG_DIR}/{name}"
                writes[dest] = dest
        for ref in refs:
            local = self._keyring_local_path(ref)
            if local is None:
                # The source machine has no such key. That is D-12's dangling reference,
                # already reported on the REPOSITORY item; inventing a key here is exactly
                # what "never re-fetched from a vendor" forbids.
                continue
            source_digest, target_digest = self._keyring_digests(ref)
            if source_digest == target_digest:
                continue
            writes[ref] = local
        return [(writes[dest], dest) for dest in sorted(writes)]

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

        Scoped to `/etc/apt/keyrings`. Legacy `/etc/apt/trusted.gpg.d` keys are ambient
        trust that nothing references by construction, so "unused" is not computable for
        them; they are left to accumulate rather than deleted on a guess.

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
        references = await _scan_target_source_references(target_run)
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
                f"sudo rm -f {shlex.quote(dest)}",
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
        """
        filename = diff.item_id.removeprefix("apt:source:")
        for ref in self._source_keyring_refs.get(filename, ()):
            if ref in self._provisioned_keyrings:
                continue
            source_digest, target_digest = self._keyring_digests(ref)
            if source_digest is not None and source_digest == target_digest:
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
        source_sudo_check = await self.source.run_command("sudo -n true")
        if not source_sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "passwordless sudo is not available on source "
                    "(required to read /etc/apt repository, keyring and pin config).\n"
                    + passwordless_sudo_hint(_SOURCE_SUDO_COMMANDS),
                )
            )

        sudo_check = await self.target.run_command("sudo -n true", login_shell=False)
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
