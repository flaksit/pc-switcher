"""`apt-cache policy` output parsing, shared by `apt_sync` and `manual_installs_sync`.

Two jobs ask apt two different questions off the same command, and both need the same
version-table walk to answer them: `apt_sync` asks what repository an INSTALLED package
came from (C26's source-removal impact) and whether the TARGET can install a name it does
not have (D-25's `REPO_UNAVAILABLE`); `manual_installs_sync` asks whether a package on the
SOURCE came from any repository at all (D-18).

Shared here rather than duplicated per job, and here rather than on `PackageSyncJob`.
D-15 forbids one manager's diff on the shared BASE CLASS (adr-020, "Job split into four
jobs"): this module defines no class and sits in no job's MRO, so the other three managers
inherit nothing from it. `manual_installs_sync`'s own rule — it never imports `apt_sync`
(D-18) — also still holds, since both jobs import a third module instead. What is NOT
worth duplicating is a stateful indentation walker with three separate subtle rules
(installed-block tracking, eight-space origin rows, the `/var/lib/dpkg/status` pseudo
-origin); `apt_sync._parse_pin_file`'s docstring records what one earlier duplicated
parser of this size already cost this project.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "installed_origins_by_package",
    "normalise_repo_uri",
    "packages_installed_from_no_repository",
    "packages_with_no_candidate",
]


def normalise_repo_uri(uri: str) -> str:
    """A repository URI reduced to the shape `apt-cache policy` prints in its version
    table: apt strips the trailing slash a source file may carry
    (`https://packages.microsoft.com/repos/azure-cli/` -> `.../azure-cli`), so comparing
    the two forms verbatim would miss every repo written with one.
    """
    return uri.rstrip("/")


def packages_with_no_candidate(policy_output: str) -> set[str]:
    """Parse a multi-package `apt-cache policy <name...>` run: names whose `Candidate:`
    line reads `(none)`. Each package's block starts with an unindented `<name>:` header
    line, per `apt-cache policy`'s documented output shape.

    `(none)` means apt knows the name but will not install ANY version of it: a pure
    virtual package, or every version pinned below zero. It does NOT mean the package is
    unavailable in general, and it is emphatically not what apt prints for a package
    installed from a bare `.deb` — dpkg's own status entry supplies a candidate for that,
    so a hand-installed package reports its installed version here
    (`packages_installed_from_no_repository` is the test for that case).

    A name apt has never heard of produces NO BLOCK AT ALL and is therefore absent from
    the result, which every caller reads as "no evidence against".
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
    origins: dict[str, set[str]] = {}
    current: str | None = None
    in_installed_block = False
    for line in policy_output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            current, in_installed_block = line[:-1], False
            origins.setdefault(current, set())
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
                origins.setdefault(current, set()).add(normalise_repo_uri(parts[1]))
            continue
        # Any other row at version-table depth is the next (non-installed) version.
        in_installed_block = False
    return {name: frozenset(uris) for name, uris in origins.items()}


def packages_installed_from_no_repository(policy_output: str, queried_names: Sequence[str]) -> frozenset[str]:
    """The `queried_names` whose INSTALLED version comes from no configured repository —
    a bare `.deb` put on the machine with `dpkg -i` (D-18).

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
