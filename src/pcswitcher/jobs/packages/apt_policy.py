"""`apt-cache policy` output parsing, shared by `apt_sync` and `manual_deb_sync`.

Two jobs ask apt three different questions off the same command, and all three need the
same version-table walk to answer them: `apt_sync` asks what repository an INSTALLED
package came from (C26's source-removal impact, and `PKG-FR-APT-IDENTITY`'s provenance comparison)
and what repository the version the TARGET would install comes from (`PKG-FR-APT-IDENTITY`'s origin
classification); `manual_deb_sync` asks whether a package on the SOURCE came from any
repository at all (`PKG-FR-MANUAL-SCOPE`).

The installed and the candidate row are DIFFERENT rows and answer different questions —
apt happily offers a package from one vendor while a second vendor's copy is the one
installed — so the two live behind one walker (`_origins_by_package`) that differs only in
which version row it collects from.

Shared here rather than duplicated per job, and here rather than on `PackageSyncJob`.
`PKG-FR-JOB-INDEPENDENCE` forbids one manager's diff on the shared BASE CLASS (adr-020, "Job split into one job
per kind of finding"): this module defines no class and sits in no job's MRO, so the other
managers inherit nothing from it. `manual_deb_sync`'s own rule — it never imports `apt_sync`
(`PKG-FR-MANUAL-SCOPE`) — also still holds, since both jobs import a third module instead. What is NOT
worth duplicating is a stateful indentation walker with three separate subtle rules
(installed-block tracking, origin rows told apart by shape, the `/var/lib/dpkg/status` pseudo
-origin); `apt_sync._parse_pin_file`'s docstring records what one earlier duplicated
parser of this size already cost this project.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "candidate_origins_by_package",
    "installed_origins_by_package",
    "normalise_repo_uri",
    "packages_installed_from_no_repository",
]

# dpkg's own record of an installed package. It appears as an origin row on every
# installed version and is not a repository.
_DPKG_STATUS = "/var/lib/dpkg/status"


def normalise_repo_uri(uri: str) -> str:
    """A repository URI reduced to the shape `apt-cache policy` prints in its version
    table: apt strips the trailing slash a source file may carry
    (`https://packages.microsoft.com/repos/azure-cli/` -> `.../azure-cli`), so comparing
    the two forms verbatim would miss every repo written with one.
    """
    return uri.rstrip("/")


def installed_origins_by_package(policy_output: str) -> dict[str, frozenset[str]]:
    """Parse a batched `apt-cache policy <name...>` run into `{package: origin URIs of
    its INSTALLED version}` (C26).

    `apt-cache policy`'s version table marks the installed version with a leading `***`
    and lists each of that version's origins below it as
    `<priority> <uri> <suite>/<component> <arch> Packages`. Only the installed version's
    origins count: another version row may list a repository that merely *offers* the
    package (Ubuntu's archive offers `gh` too), which is not the repository the machine is
    actually tracking. `/var/lib/dpkg/status` is dpkg's own record of the installed
    package, not a repository, and is skipped — every installed package lists it.

    An installed row naming no repository falls back to the CANDIDATE row's origins. A
    repository that keeps only its latest build (Google's Chrome repository does) drops the
    installed version from its index as soon as an update lands, leaving a repo-tracked
    package with dpkg's record as its only origin until the next upgrade. The candidate row
    tells that apart from a hand-installed `.deb`: dpkg's status entry makes the `.deb` its
    own candidate, so its fallback is empty too, while a stale repository package's
    candidate is the repository's newer build. A package with no installed row gets no
    fallback — nothing is feeding a package that is not installed.

    A key MEANS apt printed a block for that name; the value means what apt said about it.
    The two are deliberately separable, because a package installed from a local `.deb`
    reaches an empty origin set while a name apt has never heard of reaches no key at all,
    and the difference is the difference between "apt says no repository supplies this" and
    "apt said nothing" — including the case where the whole command failed and produced no
    output. `packages_installed_from_no_repository` may only indict the first.
    """
    return {
        name: frozenset() if block.installed is None else block.installed or block.candidate
        for name, block in _policy_blocks(policy_output).items()
    }


def candidate_origins_by_package(policy_output: str) -> dict[str, frozenset[str]]:
    """Parse a batched `apt-cache policy <name...>` run into `{package: origin URIs of the
    version apt WOULD install}` (`PKG-FR-APT-IDENTITY`).

    Not the same rows as `installed_origins_by_package`, and the difference is the whole
    point: a machine can have vendor A's copy installed while apt's candidate is vendor B's,
    and matching a package by name alone reads the second as "the target can already supply
    this" when it would supply someone else's software. The row is located by matching the
    version table's version string against the block's own `Candidate:` value rather than by
    the `***` marker, which points at the installed version.

    Key and value are separable exactly as above: a name apt printed no block for gets NO
    key and must never be read as evidence of anything, while `Candidate: (none)` — apt
    knows the name and will install no version of it — gets an EMPTY SET. That distinction
    is what separates "the target has a candidate from the wrong place" from "the target
    has no candidate at all".
    """
    return {name: block.candidate for name, block in _policy_blocks(policy_output).items()}


@dataclass(frozen=True)
class _PolicyBlock:
    installed: frozenset[str] | None
    """Origins of the `***` row; `None` when the table has none (not installed)."""

    candidate: frozenset[str]
    """Origins of the row matching `Candidate:`; empty for `Candidate: (none)`."""


def _policy_blocks(policy_output: str) -> dict[str, _PolicyBlock]:
    """The shared version-table walk: one `_PolicyBlock` per `<name>:` block apt printed.

    One walk reads both rows — the installed answer falls back on the candidate one, and
    duplicating the walk is what `_parse_pin_file`'s docstring records the cost of.
    `apt-cache policy` lists each version's origins below it as
    `<priority> <uri> <suite>/<component> <arch> Packages`; a block starts with an
    unindented `<name>:` header.

    An origin row is recognised by its shape (`_is_origin_row`), never by its indent: apt
    right-aligns the priority, so a four-character one (`1000`, `-100`) sits one column to
    the left of a three-character one. Reading by a fixed eight-space indent took Mozilla's
    `1000` row for a new version row and left the installed `firefox` with no origin (#302).
    """
    installed: dict[str, set[str]] = {}
    candidates: dict[str, set[str]] = {}
    current: str | None = None
    candidate: str | None = None
    in_installed = in_candidate = False
    for line in policy_output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            current, candidate, in_installed, in_candidate = line[:-1], None, False, False
            candidates.setdefault(current, set())
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Candidate:"):
            value = stripped.removeprefix("Candidate:").strip()
            # `(none)` leaves `candidate` unset so no row ever matches: the key survives
            # with an empty set, which is not the same answer as no key at all.
            candidate = None if value == "(none)" else value
            continue
        parts = stripped.split()
        if _is_origin_row(parts):
            if parts[1] != _DPKG_STATUS:
                uri = normalise_repo_uri(parts[1])
                if in_installed:
                    installed[current].add(uri)
                if in_candidate:
                    candidates[current].add(uri)
            continue
        # Any other row at version-table depth opens the next version. `Installed:` and
        # `Version table:` land here too and match neither predicate, which closes the
        # previous row exactly as a real version row would.
        version = stripped.removeprefix("***").split()
        in_candidate = bool(version) and version[0] == candidate
        in_installed = stripped.startswith("***")
        if in_installed:
            installed.setdefault(current, set())
    return {
        name: _PolicyBlock(
            installed=frozenset(installed[name]) if name in installed else None, candidate=frozenset(uris)
        )
        for name, uris in candidates.items()
    }


def _is_origin_row(parts: Sequence[str]) -> bool:
    """`<priority> <uri-or-path> ...`: an integer, then something apt locates packages at —
    a URI (`http://`, `file:/`, `cdrom:[...]/`) or an absolute path (`/var/lib/dpkg/status`).

    A version row is `<version> <priority>`, so its second field is a bare integer and never
    matches, even when the version itself is all digits.
    """
    if len(parts) < 2:
        return False
    priority, location = parts[0], parts[1]
    return priority.lstrip("-").isdigit() and (location.startswith("/") or ":" in location)


def packages_installed_from_no_repository(policy_output: str, queried_names: Sequence[str]) -> frozenset[str]:
    """The `queried_names` whose INSTALLED version comes from no configured repository —
    a bare `.deb` put on the machine with `dpkg --install` (`PKG-FR-MANUAL-SCOPE`).

    Callers MUST pass only names known to be installed (`apt-mark showmanual` on the
    machine `policy_output` came from). A name that is not installed is indistinguishable
    from a dpkg-only one here: neither has a repository origin for an installed version,
    because neither has an installed version.

    A name apt printed NO block for is never flagged. Absence is not evidence: apt prints a
    block for every installed package (verified against a live `apt-mark showmanual` set),
    so no block means the question was not answered — an unknown name, or an `apt-cache
    policy` that failed outright and returned nothing. Indicting on absence would let one
    failed command declare a machine's entire manual set unreproducible.

    A package hand-installed at a version NEWER than the repository's is flagged, which is
    correct rather than a false positive: replicating *this machine's* installed version
    needs the `.deb`, and the repository cannot supply it. One installed at a version OLDER
    than the repository's is not: the repository supplies the package, at the version
    `PKG-FR-VERSION-FLOAT` accepts anyway (see `installed_origins_by_package`'s fallback).
    """
    origins = installed_origins_by_package(policy_output)
    return frozenset(name for name in queried_names if name in origins and not origins[name])
