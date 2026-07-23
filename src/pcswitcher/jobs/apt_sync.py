"""`apt_sync`: apt package convergence — install, remove, and the full diff taxonomy
(D-01, D-03, D-04, D-07, D-24, D-25, ADR-020).

Captures the source's `apt-mark showmanual` set with `dpkg-query`-sourced versions
(never `apt list --installed` — its own manpage says the output has no stable scripting
contract), diffs it against the same query on the target into every D-25 class
(`PackageSyncJob._diff_apt_packages`), and converges the approved `INSTALL`/`REMOVE`
items via `apt-get install`/`apt-get remove`.

Every approved item's transaction is simulated with `apt-get -s` before the real
command runs, guarding against apt silently doing more than the review showed
(T-02-32): an install refuses if the simulation would remove anything or install an
already-present package at a lower version (a downgrade); a removal refuses if the
simulation would remove any package beyond the item itself that was not also an
approved removal in this run. `plan()` additionally runs two BATCHED simulations
(the whole install candidate set, the whole removal candidate set — not one
per-package, which would cost more than the sync itself for 150 packages) so the
review shows collateral effects before the user decides, not only when an item is
later converged.

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
from pcswitcher.jobs.package_items import (
    AptConfigItem,
    AptKeyItem,
    AptPackageItem,
    AptPinItem,
    AptSourceItem,
    DiffAction,
    DiffClass,
    HoldPinFact,
    ItemClass,
    ItemDiff,
    UnreproducibleItem,
    build_dangling_keyring_detail,
    build_version_mismatch_detail,
    compare_deb_versions,
)
from pcswitcher.jobs.package_review import (
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.package_state import DecisionFile, SnippetRegistry, filter_inert
from pcswitcher.jobs.package_sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, LogLevel, ValidationError
from pcswitcher.sudoers import passwordless_sudo_hint

__all__ = ["AptSyncJob", "AptTransactionPreview", "simulate_apt_transaction"]

# `AptPackageItem.item_id` is always this prefix + the package name (package_items.py).
# Parsing the name back out of the id is a legitimate use of a stable identity string,
# not string-matching on manager-specific content.
_APT_PACKAGE_ID_PREFIX = "apt:package:"

# Binaries this job runs under sudo, quoted back to the user when the passwordless-sudo
# check fails. A lower bound on what must be permitted, not an exact scope (ADR-013).
# The source is only ever read, so it needs just the /etc/apt digest capture.
_SOURCE_SUDO_COMMANDS = ("/usr/bin/find",)
_TARGET_SUDO_COMMANDS = (
    "/usr/bin/apt-get",
    "/usr/bin/find",
    "/usr/bin/install",
    "/usr/bin/cp",
    "/usr/bin/rm",
    "/usr/bin/fuser",
)

# The five `/etc/apt/*` directories D-11/D-13 pull into scope, each captured with one
# batched `sha256sum` listing (never one command per file).
_APT_SOURCES_DIR = "/etc/apt/sources.list.d"
_APT_KEYRINGS_DIR = "/etc/apt/keyrings"
_APT_TRUSTED_GPG_DIR = "/etc/apt/trusted.gpg.d"
_APT_PREFERENCES_DIR = "/etc/apt/preferences.d"
_APT_CONF_DIR = "/etc/apt/apt.conf.d"

# D-19's bounded unowned-install scan: top-level entries of `/usr/local` and `/opt`,
# plus the immediate children of `/usr/local/bin` and `/usr/local/lib` — one batched
# `find <dir...> -mindepth 1 -maxdepth 1` covers all four, since `find` applies the
# same depth options to every starting path it is given. Enough to NAME a finding
# (D-18), never enough to walk an entire tree — the item is decided on, not replicated
# (deferred ideas, CONTEXT.md).
_UNOWNED_SCAN_ROOTS = ("/usr/local", "/opt", "/usr/local/bin", "/usr/local/lib")

# Matches one `dpkg -S` "owned" line: `<package>[,<package>...]: <path>`. A path dpkg
# does not own produces no such line at all (its "no path found" diagnostic goes to
# stderr, which this scan never inspects) — absence from stdout is the only signal
# `_owned_paths_from_dpkg_s` needs.
_DPKG_S_OWNED_RE = re.compile(r"^[^:]+:\s+(?P<path>/\S.*)$")

# The four repository-adjacent item classes that converge in a single ordered,
# transactional group ahead of packages (Task 2) — kept as one constant so the trigger
# check in `accept_review` and the group membership check in `converge` never drift.
_REPO_GROUP_CLASSES = frozenset({ItemClass.APT_KEY, ItemClass.APT_PIN, ItemClass.APT_CONFIG, ItemClass.APT_SOURCE})

# Convergence order is an apt FACT (a repo needs its key before apt will trust it; a
# repo's metadata must be fetched before anything installs from it), not a general
# ordering concept — which is why it lives here, in the job, rather than as a sort the
# shared core imposes on every manager. Packages sort last (module-level default 3);
# pins and apt config share a rank since nothing depends on their relative order.
_ITEM_CLASS_ORDER: dict[ItemClass, int] = {
    ItemClass.APT_KEY: 0,
    ItemClass.APT_PIN: 1,
    ItemClass.APT_CONFIG: 1,
    ItemClass.APT_SOURCE: 2,
}

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


def _owned_paths_from_dpkg_s(output: str) -> frozenset[str]:
    """Every path `dpkg -S` reports as owned, parsed from its stdout alone (D-19's
    unowned-install scan). A queried path that dpkg does NOT own is simply absent from
    this set — its "no path found matching pattern" diagnostic is a stderr message this
    scan never reads, so a batched multi-path `dpkg -S` degrades cleanly even when some
    of the paths are unowned and others are not.
    """
    owned: set[str] = set()
    for line in output.splitlines():
        match = _DPKG_S_OWNED_RE.match(line)
        if match:
            owned.add(match.group("path"))
    return frozenset(owned)


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


def _parse_source_file(filename: str, content: str) -> tuple[Literal["deb822", "list"], tuple[str, ...]]:
    """A source file's format (by extension) and every keyring path it names.

    deb822 `.sources` files name a key via a `Signed-By:` field; legacy `.list` files
    name one inside the options bracket as `[... signed-by=<path> ...]` (RESEARCH
    Standard Stack). Parsed just far enough to extract these — never rewritten,
    normalised, or migrated between formats (RESEARCH Pitfall 3, deferred ideas).
    """
    fmt: Literal["deb822", "list"] = "deb822" if filename.endswith(".sources") else "list"
    refs: list[str] = []
    for line in content.splitlines():
        match = _SIGNED_BY_RE.match(line.strip()) if fmt == "deb822" else _LEGACY_SIGNED_BY_RE.search(line)
        if match:
            refs.append(match.group("path"))
    return fmt, tuple(refs)


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
    """One `cat <path>` — used only for a file a diff actually implicates."""
    result = await run(f"cat {shlex.quote(path)}")
    return result.stdout


@dataclass(frozen=True)
class _FilenameDiff:
    """Filename-level classification of two `{filename: digest}` maps — the shared
    basis every one of the five `/etc/apt/*` item classes diffs from.
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


def _diff_apt_keys(
    source_digests: Mapping[str, str], target_digests: Mapping[str, str], scope: Literal["per-repo", "global-trust"]
) -> list[ItemDiff]:
    """Key-file diffs. No content fetch is ever needed: a key's identity, label and
    diff all derive from filename + digest alone (D-12 — keys travel byte-for-byte,
    never parsed).
    """
    names = _diff_filenames(source_digests, target_digests)
    diffs: list[ItemDiff] = []

    for filename in sorted(names.missing):
        item = AptKeyItem(filename=filename, digest=source_digests[filename], scope=scope)
        diffs.append(
            ItemDiff(
                item_class=ItemClass.APT_KEY,
                diff_class=DiffClass.MISSING_ON_TARGET,
                action=DiffAction.INSTALL,
                item_id=item.item_id,
                label=item.label(),
                detail=None,
            )
        )
    for filename in sorted(names.extra):
        item = AptKeyItem(filename=filename, digest=target_digests[filename], scope=scope)
        diffs.append(
            ItemDiff(
                item_class=ItemClass.APT_KEY,
                diff_class=DiffClass.EXTRA_ON_TARGET,
                action=DiffAction.REMOVE,
                item_id=item.item_id,
                label=item.label(),
                detail=None,
            )
        )
    for filename in sorted(names.changed):
        item = AptKeyItem(filename=filename, digest=source_digests[filename], scope=scope)
        diffs.append(
            ItemDiff(
                item_class=ItemClass.APT_KEY,
                diff_class=DiffClass.VERSION_MISMATCH,
                action=DiffAction.CHANGE,
                item_id=item.item_id,
                label=item.label(),
                detail=build_version_mismatch_detail(source_digests[filename], target_digests[filename]),
            )
        )
    return diffs


def _diff_apt_configs(source_digests: Mapping[str, str], target_digests: Mapping[str, str]) -> list[ItemDiff]:
    """Config-file diffs — opaque, digest-only, same shape as keys."""
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
    if diff.item_class == ItemClass.APT_KEY:
        _, _, scope, filename = diff.item_id.split(":", 3)
        directory = _APT_KEYRINGS_DIR if scope == "per-repo" else _APT_TRUSTED_GPG_DIR
        return f"{directory}/{filename}"
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
        # Populated by `_plan_repo_diffs` (Task 1) and consulted by the repository-group
        # convergence's key-readiness check (Task 2): the source's OWN key digests (both
        # scopes) and the target's, so "does this keyring already match on the target"
        # is a dict lookup against plan-time facts rather than a live re-probe.
        self._source_key_digests_by_filename: dict[str, str] = {}
        self._target_key_digests_by_filename: dict[str, str] = {}
        # Lazily computed the first time `converge()` sees a repository-group item
        # (key/pin/config/source, or the synthetic metadata-refresh marker): maps each
        # such diff's item_id to (succeeded, message). Populated all at once so the
        # required key-before-source write order and the transactional backup/rollback
        # happen exactly once per run, regardless of which order the base `apply()`
        # loop's per-diff `converge()` calls visit them in.
        self._repo_group_outcome: dict[str, tuple[bool, str]] | None = None
        # Resolved once per run via `echo $HOME` on the target (mirrors
        # `config_sync._copy_config_to_target`'s pattern) and cached, since every
        # repository-group file write needs the same absolute staging path.
        self._target_home: str | None = None

    @override
    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        """Manually-installed apt packages on the source, with versions (D-03)."""
        manual = await self.source.run_command("apt-mark showmanual")
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
        """Hold facts from `apt-mark showhold` on BOTH machines (RESEARCH: "either
        machine" — a hold recorded on either end is worth surfacing), pin facts from
        the target's `/etc/apt/preferences.d/*` `Package:` stanzas.

        Reading only one of the two mechanisms would silently miss half the held
        packages (RESEARCH Pitfall 2): a hold is dpkg selection state, a pin is an apt
        priority preference, and neither implies the other.
        """
        facts: list[HoldPinFact] = []

        source_hold = await self.source.run_command("apt-mark showhold")
        facts.extend(
            HoldPinFact(mechanism="hold", package=name, source_ref="source: apt-mark showhold")
            for name in _lines(source_hold.stdout)
        )

        target_hold = await self.target.run_command("apt-mark showhold", login_shell=False)
        facts.extend(
            HoldPinFact(mechanism="hold", package=name, source_ref="target: apt-mark showhold")
            for name in _lines(target_hold.stdout)
        )

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

    async def _scan_no_candidate_apt_packages(self, manual_names: Sequence[str]) -> list[UnreproducibleItem]:
        """D-18: manually-installed packages whose SOURCE's own `apt-cache` has no
        candidate at all — installed via `dpkg -i` of a bare `.deb`, never registered
        with any configured repository.

        Distinct from `collect_unavailable_item_ids` above (D-25's `REPO_UNAVAILABLE`),
        which asks whether the TARGET's repos can install something the source already
        has and is missing on the target; this asks whether the SOURCE's OWN repos can
        reproduce something it itself installed, independent of whether the target
        happens to already have it too — an item present on both machines still belongs
        here if the source can no longer reinstall it itself.
        """
        if not manual_names:
            return []

        quoted = " ".join(shlex.quote(name) for name in manual_names)
        result = await self.source.run_command(f"apt-cache policy {quoted}")
        no_candidate = _packages_with_no_candidate(result.stdout)
        return [
            UnreproducibleItem(origin="apt-no-candidate", identifier=name, label=f"{name} (no apt candidate)")
            for name in sorted(no_candidate)
        ]

    async def scan_unowned_installs(self) -> list[UnreproducibleItem]:
        """D-18/D-19: paths under `/usr/local` and `/opt` that no dpkg package owns —
        software an install script dropped there directly, bypassing apt entirely.

        One batched `find` over `_UNOWNED_SCAN_ROOTS` names every candidate path, then
        one batched `dpkg -S` over those candidates decides ownership; a path absent
        from the `dpkg -S` output is unowned (`_owned_paths_from_dpkg_s`). Both steps
        run on the SOURCE — this is a fact about what the source machine has installed,
        not about the target.
        """
        quoted_roots = " ".join(shlex.quote(root) for root in _UNOWNED_SCAN_ROOTS)
        listing = await self.source.run_command(f"find {quoted_roots} -mindepth 1 -maxdepth 1 2>/dev/null")
        candidates = _lines(listing.stdout)
        if not candidates:
            return []

        quoted_paths = " ".join(shlex.quote(path) for path in candidates)
        ownership = await self.source.run_command(f"dpkg -S {quoted_paths}")
        owned = _owned_paths_from_dpkg_s(ownership.stdout)

        return [
            UnreproducibleItem(origin="unowned-path", identifier=path, label=path)
            for path in sorted(set(candidates) - owned)
        ]

    async def _plan_unreproducible_diffs(self) -> list[ItemDiff]:
        """D-18/D-19/D-21: apt-no-candidate packages plus unowned `/usr/local`/`/opt`
        installs, routed through the normal diff pipeline as `DiffClass.UNREPRODUCIBLE`.

        An item already recorded machine-specific on the SOURCE is filtered out here,
        before it ever becomes a diff — exactly like every other `filter_inert` use in
        `PackageSyncJob.plan()` — which is what makes D-19's "a finding produces noise
        exactly once, then never again" true for these two detectors too. Unreproducible
        items are always source-held (they describe what the source machine has
        installed), so the source's own decision file is the only one consulted; see
        `PackageSyncJob._finalize_unreproducible` for the write side of that same rule.

        An item with a registry snippet already on the target converges this run
        (`action=INSTALL` — a snippet makes an item reproducible, which is the whole
        point of the registry); one without still needs resolving (`action=REPORT_ONLY`)
        and surfaces in its own review group (Task 2, `_build_review_groups` below).
        """
        manual = await self.source.run_command("apt-mark showmanual")
        manual_names = _lines(manual.stdout)

        items: list[UnreproducibleItem] = [
            *await self._scan_no_candidate_apt_packages(manual_names),
            *await self.scan_unowned_installs(),
        ]
        if not items:
            return []

        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        items = await filter_inert(items, source_decisions)
        if not items:
            return []

        registry = SnippetRegistry(self.target)
        diffs: list[ItemDiff] = []
        for item in items:
            snippet = await registry.get(item.item_id)
            action = DiffAction.INSTALL if snippet is not None else DiffAction.REPORT_ONLY
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.UNREPRODUCIBLE,
                    diff_class=DiffClass.UNREPRODUCIBLE,
                    action=action,
                    item_id=item.item_id,
                    label=item.label,
                    detail=None,
                )
            )
        return diffs

    @override
    async def plan(self) -> PackagePlan:
        """Extends the base diff (missing/extra/mismatch/held/unavailable) with
        plan-time apt transaction-collateral simulation (D-24, D-25, T-02-32) and D-18's
        unreproducible-item detection.

        Runs AFTER the base diff and BEFORE review groups are (re)built, so collateral
        effects apt-get -s reveals appear as their own REPORT_ONLY facts in the SAME
        review the user approves from — visible before any decision, not discovered
        only when an item is later converged.
        """
        base_plan = await super().plan()
        collateral_diffs = await self._collect_plan_time_collateral(base_plan.diffs)
        repo_diffs = await self._plan_repo_diffs()
        unreproducible_diffs = await self._plan_unreproducible_diffs()

        if not collateral_diffs and not repo_diffs and not unreproducible_diffs:
            return base_plan

        # Ordering is an apt FACT (key before source before packages, T-02-16), not a
        # general one: the base loop stays a plain item-by-item iterator, and THIS job
        # sorts its own diffs before they reach it. `sorted` is stable, so within one
        # rank (e.g. every APT_PACKAGE diff, or every APT_PIN/APT_CONFIG diff) the
        # original relative order — base diff, then collateral, then repo diffs, then
        # unreproducible diffs — is preserved.
        all_diffs = tuple(
            sorted(
                (*base_plan.diffs, *collateral_diffs, *repo_diffs, *unreproducible_diffs),
                key=lambda diff: _ITEM_CLASS_ORDER.get(diff.item_class, 3),
            )
        )
        groups = self._build_review_groups(all_diffs)
        return PackagePlan(manager=self.manager_id, diffs=all_diffs, groups=groups)

    @override
    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """Carves any still-unresolved `UNREPRODUCIBLE` diff (`action=REPORT_ONLY`) out
        into its own group, presented after the base groups (installs/changes/removals)
        so the user has already seen the bulk of the diff before being asked to resolve
        anything (Task 2, D-21). An `UNREPRODUCIBLE` diff that already has a snippet
        (`action=INSTALL`) is NOT pulled out here — it is already resolved, so it flows
        through the base grouping like any other install-direction item, letting the
        user simply approve or skip replaying it this run.
        """
        needs_resolution = [
            diff
            for diff in diffs
            if diff.item_class == ItemClass.UNREPRODUCIBLE and diff.action == DiffAction.REPORT_ONLY
        ]
        if not needs_resolution:
            return super()._build_review_groups(diffs)

        needs_resolution_ids = {diff.item_id for diff in needs_resolution}
        rest = [diff for diff in diffs if diff.item_id not in needs_resolution_ids]
        groups = super()._build_review_groups(rest)

        resolution_group = ReviewGroup(
            manager=self.manager_id,
            action=UNREPRODUCIBLE_REVIEW_ACTION,
            title=f"Resolve {self.manager_id} items with no reproducible install",
            entries=tuple(
                ReviewEntry(item_id=diff.item_id, label=diff.label, action_label="resolve", detail=diff.detail)
                for diff in needs_resolution
            ),
        )
        return (*groups, resolution_group)

    async def _plan_repo_diffs(self) -> list[ItemDiff]:
        """Capture + diff the four `/etc/apt/*` item classes (D-11/D-12/D-13), by
        whole-file digest (module docstring): one batched `sha256sum` listing per
        directory per machine, full content fetched only for a file a diff implicates.
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

        self._source_key_digests_by_filename = {**source_per_repo_keys, **source_global_keys}
        self._target_key_digests_by_filename = {**target_per_repo_keys, **target_global_keys}
        source_key_filenames = frozenset(self._source_key_digests_by_filename)

        diffs: list[ItemDiff] = []
        diffs.extend(
            await self._diff_apt_sources(source_run, target_run, source_sources, target_sources, source_key_filenames)
        )
        diffs.extend(_diff_apt_keys(source_per_repo_keys, target_per_repo_keys, "per-repo"))
        diffs.extend(_diff_apt_keys(source_global_keys, target_global_keys, "global-trust"))
        diffs.extend(await self._diff_apt_pins(source_run, target_run, source_pins, target_pins))
        diffs.extend(_diff_apt_configs(source_configs, target_configs))
        return diffs

    async def _diff_apt_sources(
        self,
        source_run: Callable[[str], Awaitable[CommandResult]],
        target_run: Callable[[str], Awaitable[CommandResult]],
        source_digests: Mapping[str, str],
        target_digests: Mapping[str, str],
        source_key_filenames: frozenset[str],
    ) -> list[ItemDiff]:
        """Source-file diffs, hydrated with format + keyring refs only for files a diff
        implicates (missing-on-target, extra-on-target, or digest-mismatched).

        A source item whose OWN keyring reference resolves to no key file on the source
        itself carries the dangling-reference detail and is downgraded to
        `REPORT_ONLY` instead of `INSTALL` — it is not proposed for install on its own
        (D-12): a repo written without its key is a repo apt refuses.
        """
        names = _diff_filenames(source_digests, target_digests)
        diffs: list[ItemDiff] = []

        for filename in sorted(names.missing):
            content = await _read_file_content(source_run, f"{_APT_SOURCES_DIR}/{filename}")
            fmt, refs = _parse_source_file(filename, content)
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
            fmt, _refs = _parse_source_file(filename, content)
            item = AptSourceItem(filename=filename, digest=target_digests[filename], fmt=fmt)
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_SOURCE,
                    diff_class=DiffClass.EXTRA_ON_TARGET,
                    action=DiffAction.REMOVE,
                    item_id=item.item_id,
                    label=item.label(),
                    detail=None,
                )
            )

        for filename in sorted(names.changed):
            content = await _read_file_content(source_run, f"{_APT_SOURCES_DIR}/{filename}")
            fmt, refs = _parse_source_file(filename, content)
            item = AptSourceItem(filename=filename, digest=source_digests[filename], fmt=fmt, keyring_refs=refs)
            dangling = _dangling_keyring_ref(refs, source_key_filenames)
            detail = build_version_mismatch_detail(source_digests[filename], target_digests[filename])
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_SOURCE,
                    diff_class=DiffClass.VERSION_MISMATCH,
                    action=DiffAction.CHANGE,
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

    async def _collect_plan_time_collateral(self, diffs: Sequence[ItemDiff]) -> list[ItemDiff]:
        """Two BATCHED simulations — the whole install candidate set, the whole
        removal candidate set — not one per package: a per-package simulation over a
        150-package manual set would cost more than the sync itself.
        """
        install_names = [_package_name(d.item_id) for d in diffs if d.action == DiffAction.INSTALL]
        remove_names = [_package_name(d.item_id) for d in diffs if d.action == DiffAction.REMOVE]
        reviewed_names = frozenset(install_names) | frozenset(remove_names)

        collateral: list[ItemDiff] = []
        if install_names:
            quoted = " ".join(shlex.quote(name) for name in install_names)
            preview = await simulate_apt_transaction(
                self.target, f"install -y --no-install-recommends {quoted}", login_shell=False
            )
            collateral.extend(await self._collateral_from_preview(preview, reviewed_names))
        if remove_names:
            quoted = " ".join(shlex.quote(name) for name in remove_names)
            preview = await simulate_apt_transaction(self.target, f"remove -y {quoted}", login_shell=False)
            collateral.extend(await self._collateral_from_preview(preview, reviewed_names))
        return collateral

    async def _collateral_from_preview(
        self, preview: AptTransactionPreview, reviewed_names: frozenset[str]
    ) -> list[ItemDiff]:
        """Every package the simulation would remove or downgrade that is not itself
        one of the reviewed candidates — apt's own words for "this will also happen",
        surfaced as a REPORT_ONLY fact rather than silently included in the batch.
        """
        collateral: list[ItemDiff] = [
            _collateral_diff(pkg, "would also be removed") for pkg in preview.removals if pkg not in reviewed_names
        ]
        for pkg, (old_version, new_version) in preview.install_versions.items():
            if pkg in reviewed_names or old_version is None:
                continue
            if await compare_deb_versions(self.target, new_version, old_version) < 0:
                collateral.append(_collateral_diff(pkg, f"would be downgraded from {old_version} to {new_version}"))
        return collateral

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
            if diff.action == DiffAction.REMOVE and decisions.get(diff.item_id) == Decision.APPLY
        )

    @override
    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Insert the synthetic metadata-refresh diff (Task 2) once the coordinator's
        decisions are known, so it flows through the same per-item logging, dry-run
        gate and failure collection as everything else (`apply()`'s existing loop)
        instead of being a special case bolted onto the end.

        Runs AFTER `plan()` (so decisions exist) and is exactly where D-24's review
        already stopped being relevant for THIS item — the refresh is infrastructure
        the user never ticks, not a repository or key they decided about. Positioned
        immediately after the last non-package diff (repository group already sorted
        key-before-pin/config-before-source by `plan()`) and before every package
        diff, matching apt's own dependency order: metadata must be current before
        anything installs from it.
        """
        approved_group = any(
            diff.item_class in _REPO_GROUP_CLASSES
            and diff.item_id != _METADATA_REFRESH_ITEM_ID
            and outcome.decisions.get(diff.item_id) == Decision.APPLY
            for diff in plan.diffs
        )
        if approved_group:
            marker = _metadata_refresh_diff()
            non_package = [diff for diff in plan.diffs if diff.item_class != ItemClass.APT_PACKAGE]
            package = [diff for diff in plan.diffs if diff.item_class == ItemClass.APT_PACKAGE]
            plan = PackagePlan(manager=plan.manager, diffs=(*non_package, marker, *package), groups=plan.groups)
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
        for apt packages. Repository-group items (keys, pins, apt config, sources) and
        the synthetic metadata-refresh marker converge as one ordered, transactional
        unit via `_converge_repo_group_item` instead (Task 2); an `UNREPRODUCIBLE` item
        converges by replaying its registered snippet (`_converge_unreproducible`) — the
        only action it can have here is `INSTALL`, since `plan()` only sets that once a
        snippet exists, and a `REPORT_ONLY` diff never reaches `converge()` at all
        (`apply()`'s filter).

        One package per invocation (D-27) so a single bad package cannot fail the
        whole batch, and so each package's simulation corresponds exactly to the
        command that follows it. The target resolves dependencies and downloads from
        its own repos (D-28) — no source cache is consulted.
        """
        if diff.item_class in _REPO_GROUP_CLASSES or diff.item_id == _METADATA_REFRESH_ITEM_ID:
            return await self._converge_repo_group_item(diff)
        if diff.item_class == ItemClass.UNREPRODUCIBLE:
            return await self._converge_unreproducible(diff)
        if diff.action == DiffAction.INSTALL:
            return await self._converge_install(diff)
        if diff.action == DiffAction.REMOVE:
            return await self._converge_remove(diff)
        raise ConvergeItemFailed(
            f"AptSyncJob.converge: unsupported action {diff.action.value!r} for {diff.label} "
            "(only 'install' and 'remove' exist for apt packages)"
        )

    async def _converge_unreproducible(self, diff: ItemDiff) -> CommandResult:
        """Replay this item's registered snippet against the target, verbatim (D-20).
        `SnippetRegistry.replay` never raises for "no snippet registered" — it returns a
        failed `CommandResult` instead, so a plan/apply-time race (the registry changed
        underneath this run) is a per-item failure like any other (D-27), not a crash.
        """
        return await SnippetRegistry(self.target).replay(diff.item_id, self.target)

    async def _converge_install(self, diff: ItemDiff) -> CommandResult:
        name = _package_name(diff.item_id)
        quoted = shlex.quote(name)
        install_args = f"install -y --no-install-recommends {quoted}"

        preview = await simulate_apt_transaction(self.target, install_args, login_shell=False)
        if preview.removals:
            removed = ", ".join(preview.removals)
            # Enforcement of this plan's own prohibition that an install-direction item
            # removes nothing as a side effect (D-24, D-25, T-02-32): without this check
            # the prohibition is a sentence in a document no code verifies.
            raise ConvergeItemFailed(
                f"install of {name} refused: apt-get -s would also remove {removed}, "
                "which was never reviewed (D-24/D-25)"
            )

        for pkg, (old_version, new_version) in preview.install_versions.items():
            if old_version is None:
                continue
            if await compare_deb_versions(self.target, new_version, old_version) < 0:
                raise ConvergeItemFailed(
                    f"install of {name} refused: apt-get -s would install {pkg} at {new_version}, "
                    f"a downgrade from the currently installed {old_version} (D-04, T-02-32)"
                )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {install_args}"
        return await self.target.run_command(real_cmd, login_shell=False)

    async def _converge_remove(self, diff: ItemDiff) -> CommandResult:
        name = _package_name(diff.item_id)
        quoted = shlex.quote(name)
        remove_args = f"remove -y {quoted}"

        preview = await simulate_apt_transaction(self.target, remove_args, login_shell=False)
        approved = self._approved_removal_names()
        unapproved = [pkg for pkg in preview.removals if pkg != name and pkg not in approved]
        if unapproved:
            removed = ", ".join(unapproved)
            # "removes nothing the user did not approve", not "removes nothing else":
            # removing a package legitimately removes things that depend on it.
            raise ConvergeItemFailed(
                f"removal of {name} refused: apt-get -s would also remove {removed}, "
                "which was not itself an approved removal item in this run (D-24/D-25)"
            )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {remove_args}"
        return await self.target.run_command(real_cmd, login_shell=False)

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
        """Every repository-group (key/pin/config/source) diff this run's decisions
        approved, in `plan.diffs` order — already key-before-pin/config-before-source
        (`plan()`'s sort). Excludes the synthetic metadata-refresh marker itself, which
        is tracked separately since it names no `/etc/apt` file to back up or write.
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
        written and others not would leave `/etc/apt` in a state nobody reviewed.

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

        if not group_diffs:
            self._repo_group_outcome = (
                {_METADATA_REFRESH_ITEM_ID: (True, "no repository changes to refresh for")} if marker_present else {}
            )
            return

        # Populated incrementally (not built up in a local dict and assigned at the
        # end) so a later diff in THIS SAME group — a source item, converging after its
        # key per the established order — can consult an earlier diff's real outcome
        # via `_keyring_ready_on_target` while the group is still being written.
        self._repo_group_outcome = {}

        home = await self._target_home_dir()
        staging_dir = f"{home}/.cache/pc-switcher/apt-staging"
        backup_dir = f"{staging_dir}/backup-{uuid4().hex}"
        await self.target.run_command(f"mkdir -p {shlex.quote(staging_dir)}", login_shell=False)

        existed_before: dict[str, bool] = {}
        try:
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

        for diff in group_diffs:
            try:
                await self._write_or_remove_repo_item(diff, staging_dir)
                self._repo_group_outcome[diff.item_id] = (True, "converged")
            except ConvergeItemFailed as exc:
                self._repo_group_outcome[diff.item_id] = (False, str(exc))

        update_result = await self.target.run_command("sudo apt-get update", login_shell=False)
        if update_result.success:
            await self.target.run_command(f"rm -rf {shlex.quote(backup_dir)}", login_shell=False)
            if marker_present:
                self._repo_group_outcome[_METADATA_REFRESH_ITEM_ID] = (True, "apt-get update succeeded")
            return

        # Rollback (T-02-34): restore every file that existed before, delete every file
        # the group created, discard the backup directory, then re-probe apt so the
        # failure summary can tell the user whether the target recovered rather than
        # leaving them to guess.
        for dest, existed in existed_before.items():
            if existed:
                backup_path = _backup_path_for(backup_dir, dest)
                await self.target.run_command(
                    f"sudo install -o root -g root -m 0644 {shlex.quote(backup_path)} {shlex.quote(dest)}",
                    login_shell=False,
                )
            else:
                await self.target.run_command(f"sudo rm -f {shlex.quote(dest)}", login_shell=False)
        await self.target.run_command(f"rm -rf {shlex.quote(backup_dir)}", login_shell=False)

        reprobe = await self.target.run_command("sudo apt-get update", login_shell=False)
        recovery = (
            "target apt recovered after rollback" if reprobe.success else "target apt still broken after rollback"
        )
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

        await self.target.run_command(f"mkdir -p {shlex.quote(backup_dir)}", login_shell=False)
        backup_path = _backup_path_for(backup_dir, dest)
        result = await self.target.run_command(
            f"sudo cp -a {quoted_dest} {shlex.quote(backup_path)}", login_shell=False
        )
        if not result.success:
            raise ConvergeItemFailed(
                f"failed to back up {dest} before converging the repository group: {result.stderr.strip()}"
            )
        return True

    async def _write_or_remove_repo_item(self, diff: ItemDiff, staging_dir: str) -> None:
        """Converge one repository-group diff: `sudo rm -f` for a REMOVE, or
        stage-then-promote for an INSTALL/CHANGE (T-02-35). `RemoteExecutor.send_file`
        is plain SFTP as the ordinary SSH user with no sudo path (`executor.py` around
        line 362) and cannot write into `/etc/apt` directly — bytes land under the
        target user's own `~/.cache` staging directory first, then `sudo install`
        promotes them with the right ownership/mode in one atomic step (no window where
        the file exists under `/etc/apt` owned by the wrong user, unlike a `mv` +
        separate `chown`/`chmod`). The staging copy is removed in a `finally` so a
        failed promotion never leaves transferred key material sitting in the cache.
        """
        dest = _repo_item_destination(diff)

        if diff.action == DiffAction.REMOVE:
            result = await self.target.run_command(f"sudo rm -f {shlex.quote(dest)}", login_shell=False)
            if not result.success:
                raise ConvergeItemFailed(f"failed to remove {dest}: {result.stderr.strip()}")
            return

        if diff.item_class == ItemClass.APT_SOURCE:
            await self._require_keyrings_ready(diff)

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
            f"sudo mkdir -p -m 0755 {shlex.quote(dest_dir)}", login_shell=False
        )
        if not mkdir_result.success:
            raise ConvergeItemFailed(
                f"failed to prepare directory {dest_dir} for {dest}: {mkdir_result.stderr.strip()}"
            )

        local_path = Path(dest)
        staged_name = diff.item_id.replace(":", "_").replace("/", "_")
        staged_dest = f"{staging_dir}/{staged_name}"
        try:
            await self.target.send_file(local_path, staged_dest)
            promote = await self.target.run_command(
                f"sudo install -o root -g root -m 0644 {shlex.quote(staged_dest)} {shlex.quote(dest)}",
                login_shell=False,
            )
            if not promote.success:
                raise ConvergeItemFailed(f"failed to install {dest}: {promote.stderr.strip()}")
        finally:
            await self.target.run_command(f"rm -f {shlex.quote(staged_dest)}", login_shell=False)

    async def _require_keyrings_ready(self, diff: ItemDiff) -> None:
        """Refuse to write a source file whose keyring reference is neither already
        matching on the target nor among this run's successfully converged key items
        (Task 2, D-12) — a repository written without its key is a repository apt
        refuses on every subsequent operation, which makes writing it anyway strictly
        worse than leaving the target alone.

        Re-reads and re-parses the source file's own content rather than threading the
        already-parsed `AptSourceItem.keyring_refs` through the plan/diff pipeline: the
        plan only carries `ItemDiff`s (module docstring — one shared shape for every
        item class), so re-deriving from the source's own bytes at converge time is the
        cost of keeping that shape uniform, and the file is small enough that a second
        `cat` is negligible next to the write it gates.
        """
        filename = diff.item_id.removeprefix("apt:source:")
        source_path = f"{_APT_SOURCES_DIR}/{filename}"
        content = await _read_file_content(self.source.run_command, source_path)
        _fmt, refs = _parse_source_file(filename, content)

        for ref in refs:
            ref_filename = Path(ref).name
            if self._keyring_ready_on_target(ref_filename):
                continue
            raise ConvergeItemFailed(
                f"source {filename} references keyring {ref_filename!r}, which is neither already "
                "present on the target nor among this run's successfully converged key items "
                "(D-12/T-02-16); skipping this repository write"
            )

    def _keyring_ready_on_target(self, ref_filename: str) -> bool:
        """A keyring reference is ready for a source file to depend on if it already
        matches byte-for-byte on the target (no diff was even needed, per the digest
        maps `_plan_repo_diffs` captured) or if this run already successfully converged
        it (checked against `_repo_group_outcome`, which — because keys sort before
        sources — already holds every key diff's real outcome by the time a source
        diff's own write is attempted).
        """
        source_digest = self._source_key_digests_by_filename.get(ref_filename)
        target_digest = self._target_key_digests_by_filename.get(ref_filename)
        if source_digest is not None and source_digest == target_digest:
            return True
        if self._repo_group_outcome is not None:
            for scope in ("per-repo", "global-trust"):
                outcome = self._repo_group_outcome.get(f"apt:key:{scope}:{ref_filename}")
                if outcome is not None and outcome[0]:
                    return True
        return False

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
        # /etc/apt state runs `sudo find` there, and without passwordless sudo that
        # capture degrades to empty digest maps rather than failing. The sync would then
        # report success having replicated no repository state at all — a silent
        # wrong-result, which is worse than refusing to start.
        source_sudo_check = await self.source.run_command("sudo -n true")
        if not source_sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "passwordless sudo is not available on source "
                    "(required to read /etc/apt repository, keyring and pin state).\n"
                    + passwordless_sudo_hint(_SOURCE_SUDO_COMMANDS),
                )
            )

        sudo_check = await self.target.run_command("sudo -n true", login_shell=False)
        if not sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "passwordless sudo is not available on target "
                    "(required to install packages and write /etc/apt state).\n"
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


def _collateral_diff(name: str, effect: str) -> ItemDiff:
    """One plan-time collateral fact — apt's own simulation, not a per-item guard."""
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REPORT_ONLY,
        item_id=f"apt:collateral:{name}",
        label=name,
        detail=f"apt's own simulation says this package {effect}",
    )
