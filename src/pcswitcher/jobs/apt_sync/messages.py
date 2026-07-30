"""Every sentence this job puts in front of the user, and nothing else.

One home for the review text (ADR-020 D-07's "the review names the concrete action"): a
detail string built where it is needed drifts from the one beside it, and
`tests/manual/review_harness.py` rehearses these builders directly so what a tester reads
is what a real run shows rather than a paraphrase of it.

Every builder takes `Machines` and names both machines by hostname — never "source" and
"target", which are the tool's words for the two ends of a run and not the names of
anybody's computers.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pcswitcher.jobs.packages.items import Machines

# A URI's scheme, stripped for DISPLAY only (ruling 9). Matches `cdrom:`-style schemes too,
# whose `//` is optional, so an origin apt can never replicate still reads as itself.
_URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:(//)?", re.IGNORECASE)


def display_origin(uri: str) -> str:
    """A repository URI in the form the review shows it (ruling 9): the FULL path with its
    scheme stripped — `ppa.launchpadcontent.net/git-core/ppa/ubuntu`.

    The path, never the bare host: one Launchpad host serves thousands of unrelated PPAs and
    one vendor host often serves several channels, so a hostname does not identify the
    repository the package actually came from. Only the display strips; the comparison form
    stays exactly what `normalise_repo_uri` produces, scheme included, because that is what
    apt prints and what the two machines' URIs are matched on.
    """
    return _URI_SCHEME_RE.sub("", uri).rstrip("/")


def build_origin_detail(origins: Sequence[str]) -> str | None:
    """Detail naming where an approved install would come from, or `None` when there is
    nothing worth naming (ruling 9).

    `origins` is the package's NON-distribution origins, already filtered by the caller
    against the origins that machine's own distribution files declare — so an empty sequence
    means the distribution's archive serves it, which is the unremarkable case and earns no
    text. Several are named comma-separated and sorted, because a package genuinely served
    by two vendors is a fact the user should see whole.
    """
    if not origins:
        return None
    return f"from {', '.join(display_origin(uri) for uri in origins)}"


def build_repo_unavailable_detail(name: str, origins: Sequence[str], cause: str, machines: Machines) -> str:
    """Detail for a `REPO_UNAVAILABLE` diff: where the source has this package from, and why
    the target cannot be given the same place (ADR-020 D-34).

    Both halves are load-bearing. Naming the origin is what stops this reading as "apt has
    never heard of it"; naming the cause is what tells the user whether the remedy is theirs
    (a repository file that no longer exists, a missing signing key) or nobody's.
    """
    where = f" from {', '.join(display_origin(uri) for uri in origins)}" if origins else ""
    return f"{machines.target} cannot install {name}{where}: {cause}"


def build_origin_mismatch_detail(
    source_origins: Sequence[str], target_origins: Sequence[str], machines: Machines
) -> str:
    """Detail for an `ORIGIN_MISMATCH` diff: the same package, two vendors.

    Report only, and both sides are named because neither is wrong — converging it would
    mean a cross-vendor reinstall, which is not a float (D-04) and not something the user
    asked for. The user is the only one who can say which machine is the odd one out.
    """
    source = ", ".join(display_origin(uri) for uri in source_origins)
    target = ", ".join(display_origin(uri) for uri in target_origins)
    return f"{machines.source} installed it from {source}, {machines.target} from {target}"


def build_origin_refusal_detail(
    name: str, source_origins: Sequence[str], target_origins: Sequence[str], machines: Machines
) -> str:
    """Why an approved install was refused at the last moment (ADR-020 D-35): the origin the
    source uses, and the origin the target's apt would have installed from instead.

    Both are named because either half alone is unactionable. "The wrong vendor" does not
    say which repository failed to land; "no candidate from packages.mozilla.org" does not
    say what the target would have shipped in its place. Together they are the whole finding,
    on the item the user actually decided about.
    """
    wanted = ", ".join(display_origin(uri) for uri in source_origins)
    if target_origins:
        instead = f"would install it from {', '.join(display_origin(uri) for uri in target_origins)}"
    else:
        instead = "offers it from no repository at all"
    return (
        f"{name} was not installed: {machines.source} has it from {wanted}, but after this run's "
        f"apt-get update {machines.target} {instead} (ADR-020 D-35)"
    )


def build_dangling_keyring_detail(filename: str, missing_ref: str, machines: Machines) -> str:
    """Detail string when a source file's `Signed-By:`/`signed-by=` reference resolves
    to no keyring file on the SOURCE itself (a source referencing a key nobody
    captured). Flags the source item rather than letting it be proposed for install on
    its own (D-12): a repository written without its key is a repository apt refuses on
    every subsequent operation, so surfacing the gap here is cheaper than discovering it
    as an opaque apt-get failure on the target.
    """
    return f"{filename} references keyring {missing_ref!r}, which does not exist on {machines.source}"


def build_repo_conflict_detail(filename: str, packages: Sequence[str], machines: Machines) -> str:
    """Detail for a repository-conflict entry: why THIS differing file is being put to the
    user when every other one is overwritten silently (ruling 6).

    The named packages are the whole reason. They are recorded skip-always, so `filter_inert`
    keeps them out of the target manifest and they produce no diff of their own in any run —
    without this line the user sees a file they are asked to overwrite and no indication that
    doing so moves software they explicitly told this tool to leave alone.
    """
    one = len(packages) == 1
    return (
        f"{filename} is different on the two machines, and {machines.target} installs "
        f"{', '.join(packages)} from it — {'package' if one else 'packages'} you marked as specific to "
        f"{machines.target}, so a sync normally leaves {'it' if one else 'them'} alone"
    )


def build_repo_removal_detail(uris: Sequence[str], orphaned: str | None, machines: Machines) -> str:
    """Detail for a repository-file DELETION: what the machine stops getting software from,
    then what that costs (`build_orphaned_packages_detail`) when it costs anything.

    The URLs, not just the filename. A filename is whatever whoever created the file decided
    to call it, and two machines' `/etc/apt/sources.list.d` routinely name the same vendor
    differently; the URL is the thing the user recognises and the thing the deletion actually
    removes. A file declaring none (a commented-out leftover, an unparsable stanza) says so
    rather than silently dropping the first half of the sentence.
    """
    where = ", ".join(uris) if uris else "nowhere — it declares no repository URL"
    stops = f"{machines.target} would stop getting software from {where}"
    return f"{stops}; {orphaned}" if orphaned else stops


def build_orphaned_packages_detail(source_filename: str, packages: Sequence[str], machines: Machines) -> str:
    """Detail string for an apt source-file REMOVE diff whose removal would leave
    machine-specific packages on the target without the repository that feeds them (C26).

    Those packages are the ones a review can never show by itself: recorded skip-always,
    they are filtered out of the target manifest before diffing (D-08), so they produce no
    `ItemDiff` in any run. Naming them here is the only place the user learns that
    approving the source deletion strands software they explicitly told this tool to keep.
    Disclosure, not refusal — D-30's placement, the same as flatpak's orphaned refs.
    """
    one = len(packages) == 1
    return (
        f"{machines.target} installs {', '.join(packages)} from {source_filename} — "
        f"{'package' if one else 'packages'} you marked as specific to {machines.target}, so "
        f"{'it' if one else 'they'} would stay installed but never get another update"
    )


def build_esm_gate_message(esm_files: Sequence[str], machines: Machines, job_name: str) -> str:
    """The ESM gate's question (D-38): the fact, how to fix it, what skipping costs.

    No account of the failure it prevents. Why an unattached machine still refreshes cleanly
    and fails only later, on a 401 from the pool, is why this gate exists at all — but it
    changes neither answer, so it stays in the package docstring and off the user's screen.

    The remedy is spelled out because this is the one gate whose "proceed" answer asks the
    user to go and DO something on the other machine first, and a question they cannot act
    on has only one answer. The two commands are Ubuntu's, not this tool's, so the tutorial
    link carries the weight: if the attach flow changes under us the link still lands on the
    current procedure, and the stale commands beside it are recognisably a summary of it.

    A module-level builder, not an f-string in the gate, so `tests/manual/review_harness.py`
    rehearses the text a real run shows rather than a paraphrase that drifts from it.
    """
    named = ", ".join(esm_files)
    return (
        f"{machines.source} carries {named}, which this sync would copy to {machines.target} — but "
        f"{machines.target} is not attached to Ubuntu Pro.\n\n"
        f"To attach {machines.target}, run there:\n"
        "    sudo pro attach <token from https://ubuntu.com/pro/dashboard>\n"
        "    sudo pro enable esm-apps esm-infra\n"
        "Full instructions: https://documentation.ubuntu.com/pro/attach-tutorial/\n\n"
        f"Skipping means {job_name} does nothing at all this run. Every other job still runs."
    )


def build_trigger_phrase(triggers: frozenset[str], candidates: Sequence[str]) -> str:
    """How a collateral item names what causes it: the attributed candidates, or a reference
    back to the earlier screens when the answer really is all of them and listing them would
    only reprint the batch.

    Both cases have to read as true in the prompt, because declining cancels exactly what is
    named here — one package when one package is to blame, the whole batch when apt only
    drops the collateral once every candidate goes (joint causation).
    """
    if len(candidates) > 1 and len(triggers) == len(candidates):
        return "the packages listed earlier"
    return ", ".join(sorted(triggers))
