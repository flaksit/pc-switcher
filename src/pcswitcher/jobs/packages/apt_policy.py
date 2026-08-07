"""`apt-cache policy` output parsing, shared by `apt_sync` and `manual_deb_sync`.

Two jobs ask apt three different questions off the same command, and all three need the
same version-table walk to answer them: `apt_sync` asks what repository an INSTALLED
package came from (C26's source-removal impact, and ADR-020 D-34's provenance comparison)
and what repository the version the TARGET would install comes from (D-34's origin
classification); `manual_deb_sync` asks whether a package on the SOURCE came from any
repository at all (D-18).

The installed and the candidate row are DIFFERENT rows and answer different questions —
apt happily offers a package from one vendor while a second vendor's copy is the one
installed — so the two live behind one walker (`_origins_by_package`) that differs only in
which version row it collects from.

Shared here rather than duplicated per job, and here rather than on `PackageSyncJob`.
D-15 forbids one manager's diff on the shared BASE CLASS (adr-020, "Job split into one job
per kind of finding"): this module defines no class and sits in no job's MRO, so the other
managers inherit nothing from it. `manual_deb_sync`'s own rule — it never imports `apt_sync`
(D-18) — also still holds, since both jobs import a third module instead. What is NOT
worth duplicating is a stateful indentation walker with three separate subtle rules
(installed-block tracking, eight-space origin rows, the `/var/lib/dpkg/status` pseudo
-origin); `apt_sync._parse_pin_file`'s docstring records what one earlier duplicated
parser of this size already cost this project.
"""

from __future__ import annotations

from collections.abc import Sequence

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
    and indents each of that version's origins by eight spaces as
    `<priority> <uri> <suite>/<component> <arch> Packages`. Only the installed version's
    origins count: another version row may list a repository that merely *offers* the
    package (Ubuntu's archive offers `gh` too), which is not the repository the machine is
    actually tracking. `/var/lib/dpkg/status` is dpkg's own record of the installed
    package, not a repository, and is skipped — every installed package lists it.

    A key MEANS apt printed a block for that name; the value means what apt said about it.
    The two are deliberately separable, because a package installed from a local `.deb`
    reaches an empty origin set while a name apt has never heard of reaches no key at all,
    and the difference is the difference between "apt says no repository supplies this" and
    "apt said nothing" — including the case where the whole command failed and produced no
    output. `packages_installed_from_no_repository` may only indict the first.
    """
    return _origins_by_package(policy_output, of_candidate=False)


def candidate_origins_by_package(policy_output: str) -> dict[str, frozenset[str]]:
    """Parse a batched `apt-cache policy <name...>` run into `{package: origin URIs of the
    version apt WOULD install}` (ADR-020 D-34).

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
    return _origins_by_package(policy_output, of_candidate=True)


def _origins_by_package(policy_output: str, *, of_candidate: bool) -> dict[str, frozenset[str]]:
    """The shared version-table walk: `{package: origin URIs of one of its version rows}`.

    `of_candidate` picks the row — the one whose version equals the block's `Candidate:`,
    or the `***`-marked installed one. Everything else is identical, and duplicating the
    walk to vary that one predicate is what `_parse_pin_file`'s docstring records the cost
    of. `apt-cache policy` indents each version's origins by eight spaces as
    `<priority> <uri> <suite>/<component> <arch> Packages`; a block starts with an
    unindented `<name>:` header.
    """
    origins: dict[str, set[str]] = {}
    current: str | None = None
    candidate: str | None = None
    collecting = False
    for line in policy_output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            current, candidate, collecting = line[:-1], None, False
            origins.setdefault(current, set())
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
        if line.startswith("        "):
            parts = stripped.split()
            if collecting and len(parts) >= 2 and parts[1] != _DPKG_STATUS:
                origins[current].add(normalise_repo_uri(parts[1]))
            continue
        # Any other row at version-table depth opens the next version. `Installed:` and
        # `Version table:` land here too and match neither predicate, which closes the
        # previous row exactly as a real version row would.
        version = stripped.removeprefix("***").split()
        collecting = (bool(version) and version[0] == candidate) if of_candidate else stripped.startswith("***")
    return {name: frozenset(uris) for name, uris in origins.items()}


def packages_installed_from_no_repository(policy_output: str, queried_names: Sequence[str]) -> frozenset[str]:
    """The `queried_names` whose INSTALLED version comes from no configured repository —
    a bare `.deb` put on the machine with `dpkg --install` (D-18).

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
    needs the `.deb`, and the repository cannot supply it.
    """
    origins = installed_origins_by_package(policy_output)
    return frozenset(name for name in queried_names if name in origins and not origins[name])
