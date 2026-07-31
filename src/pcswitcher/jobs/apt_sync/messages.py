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
from pcswitcher.redaction import redact_credentials

# A URI's scheme, stripped for DISPLAY only (ruling 9). Matches `cdrom:`-style schemes too,
# whose `//` is optional, so an origin apt can never replicate still reads as itself.
_URI_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:(//)?", re.IGNORECASE)


def display_origin(uri: str) -> str:
    """A repository URI in the form the review shows it (ruling 9): the FULL path, its
    credential withheld and its scheme stripped — `ppa.launchpadcontent.net/git-core/ppa/ubuntu`,
    `***@repo.example.test/apt`.

    The path, never the bare host: one Launchpad host serves thousands of unrelated PPAs and
    one vendor host often serves several channels, so a hostname does not identify the
    repository the package actually came from. Only the display strips; the comparison form
    stays exactly what `normalise_repo_uri` produces, scheme included, because that is what
    apt prints and what the two machines' URIs are matched on.

    Withholding the userinfo (`PKG-FR-CREDENTIAL-PRIVACY`) happens HERE, before the scheme
    goes, and not only in the redaction pass every label and detail already goes through
    (`ItemDiff.__post_init__`). `redact_credentials` is anchored on `://` so that it cannot
    touch an scp-style `user@host:path`, which carries no credential — so a string this
    function has already reformatted is one that pass can no longer redact. Making the
    reformatting do the withholding is what stops the order of the two from mattering: there
    is no window in which a scheme-less origin exists un-redacted, whichever caller builds
    the sentence. A private repository carries its password in its own address, so an origin
    line is exactly what that article was written for.
    """
    return _URI_SCHEME_RE.sub("", redact_credentials(uri)).rstrip("/")


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
    """Detail for an `ORIGIN_MISMATCH` diff: the same package, two origins.

    Report only, and both sides are named because neither is wrong — converging it would
    mean a cross-origin reinstall, which is not a float (D-04) and not something the user
    asked for. The user is the only one who can say which machine is the odd one out.

    An empty sequence is that machine's DISTRIBUTION, not a missing half of the sentence.
    `is_origin_mismatch` is what puts a diff here, and it needs both machines' identity sets
    non-empty; a machine with no vendor origin and a non-empty identity set has exactly one
    member, the distribution `PKG-FR-DISTRO-ORIGIN` collapses every mirror and pocket into.
    That is the `gh`-from-GitHub against `gh`-from-Ubuntu case, so this branch is the
    requirement's own worked example rather than a defensive fallback.
    """
    source = _named_origins(source_origins, machines.source)
    target = _named_origins(target_origins, machines.target)
    return f"{machines.source} installed it from {source}, {machines.target} from {target}"


def _named_origins(origins: Sequence[str], machine: str) -> str:
    if not origins:
        return f"{machine}'s own distribution archive"
    return ", ".join(display_origin(uri) for uri in origins)


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


def build_repo_removal_detail(uris: Sequence[str], machines: Machines) -> str:
    """Detail for a repository-file DELETION: what the machine stops getting software from.

    The URLs, not just the filename. A filename is whatever whoever created the file decided
    to call it, and two machines' `/etc/apt/sources.list.d` routinely name the same vendor
    differently; the URL is the thing the user recognises and the thing the deletion actually
    removes. A file declaring none (a commented-out leftover, an unparsable stanza) says so
    rather than silently dropping the first half of the sentence.

    Nothing about stranded software is said here, because a file still feeding anything the
    target keeps is never offered for deletion in the first place
    (`PKG-FR-REPO-DELETE`) — every entry that reaches this text is one the deletion costs
    the machine nothing but the URLs.
    """
    where = ", ".join(uris) if uris else "nowhere — it declares no repository URL"
    return f"{machines.target} would stop getting software from {where}"


def build_stranded_repository_line(dest: str, uris: Sequence[str], packages: Sequence[str], machines: Machines) -> str:
    """What the run says about a repository it wrote for an install a late collateral answer
    then withdrew (`PKG-FR-REPO-DERIVED`).

    The URL as well as the filename, for the reason `build_repo_removal_detail` gives: the
    filename is whatever whoever created the file called it, and the URL is what the machine
    is actually pointed at. A file declaring none says so rather than dropping half the
    sentence.

    Nothing broke here, so nothing in this reads as breakage: the run wrote the file for a
    package the user's own answer then withdrew, the file is left where it is, and whether
    the repository should go with the package is theirs to say.
    """
    where = ", ".join(uris) if uris else "no repository URL, since the file declares none"
    return (
        f"{dest} stays on {machines.target}: it was written for {', '.join(packages)}, whose install was "
        f"declined, so nothing on {machines.target} installs from {where}. Left in place — remove it by "
        "hand if it is not wanted."
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


def build_collateral_group_title(machines: Machines, manager_id: str) -> str:
    """The heading over every manual-collateral question, whenever it is asked.

    Names both of `Collateral.protected`'s grounds, because one group can hold both: a
    package a mark alone protects is not one the user installed there. Which ground holds
    for a given entry is its own detail line (`Collateral._reason`).

    One builder because the question is asked from two places — the plan-time group and the
    one `LateCollateral` raises mid-apply — and a heading that differed between them would
    read as two different questions.
    """
    return (
        f"Packages you installed on {machines.target} or marked as its own that this sync "
        f"would remove, downgrade or upgrade ({manager_id})"
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
