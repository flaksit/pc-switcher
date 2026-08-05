"""`flatpak_sync`: flatpak ref/remote convergence with scope as identity (D-06, D-14,
D-29, ADR-020).

Scope (user vs. system) is part of a flatpak item's identity, not just a field on it:
this project's own reference machine has several runtimes installed in BOTH scopes
under the same application id, and `FlatpakItem.item_id`/`FlatpakRemoteItem.item_id`
already fold scope into the identity string (`FlatpakItem` below). That is what makes
"same application, different scope" fall out of the generic source-vs-target diff as
two independent items with no special-casing in this module — a ref present as
`user` on the source and `system` on the target produces one install diff and one
removal diff, never a single in-place change, because they are simply two different
`item_id`s. Normalising that scope split across a machine's own two installations is
explicitly out of scope (deferred, CONTEXT.md): it is a change to the machines, not a
sync feature, and this job reports the split exactly as found.

BRANCH is identity for the same reason (`FlatpakItem`): a ref is identified by its full
`<application>/<arch>/<branch>` ref, which is also the only string `flatpak install` and
`flatpak uninstall` can resolve on a remote or a machine holding two branches of one
application id. Origin is deliberately NOT identity — see `FlatpakItem` for why the two
go opposite ways.

A remote is DERIVED from the refs approved from it, never ticked (ADR-020 D-41, which is
D-37's rule for apt repositories in a second ecosystem). `flatpak install` refuses outright when
the remote it names is not configured in the scope being installed into (D-14), so
"ref ticked, its remote unticked" was an unrepresentable pairing offered as two independent
review lines — and worse, "ref ticked, its remote's URL change declined" silently installed
another vendor's build. `accept_review()` therefore turns the approved ref installs into
the set of remotes this run must provision (`_derive_remotes`), and `apply()` writes them
before the base converge loop reaches the first ref. A remote the source has that feeds no
ref approved this run does not travel; there is no flatpak counterpart to apt's
never-removed distribution sources, because a fresh flatpak install configures ZERO remotes
and a machine with none is a perfectly ordinary machine (measured), so even Flathub travels
only as a consequence of something needing it. A derived write has no item of its own, so a
failure is recorded against the remote and charged to every approved ref that depended on
it (D-39). Deletion is derived by the same rule (`_delete_unused_remotes`), so a remote is
never a review item in any direction.

Repointing a remote is silent too, with ADR-020 D-41's single exception, which this job
applies to remotes exactly as `apt_sync` applies D-37's to repository files: a remote whose URL
or verification setting differs is overwritten without a word UNLESS a ref the TARGET
recorded skip-always takes it as its origin, in which case both configurations are shown and
the answer is overwrite or skip-once (`_capture_remote_conflicts`,
`REPO_CONFLICT_REVIEW_ACTION`). Machine-specific is the trigger, not target-only: a
skip-always ref is structurally invisible — `filter_inert` keeps it out of the manifest, so
it produces no diff in any run — and repointing the remote it updates from moves software
the user explicitly told this tool to leave alone. Two answers, nothing recorded. A skipped
conflict keeps the remote out of the write set but NOT out of the D-39 attribution map, so
every approved ref that needed the source's URL fails naming the decision rather than
installing from the URL the target still has.

Before converging a ref, its origin remote is re-read off the TARGET and required to carry
the source
remote's URL and verification setting — not merely to exist under the same name
(`_origin_refusal`), because a same-named remote pointing elsewhere serves a different
vendor's build of the same ref at exit 0 with no warning; and after the install the ref's
own reported origin is read back and resolved against a FRESHLY read remote listing, on those
same two facets (`_installed_origin_refusal`), so the guarantee is checked rather than
inferred — a cached listing would make the read-back a second reading of the evidence the
pre-install check already judged. Either refusal is a per-item failure naming both URLs,
never an install issued in hope (T-02-24). That same read is what checks the derived writes
actually landed: `flatpak remote-add --if-not-exists <name> <other url>`
exits 0 and changes nothing (measured), so the write's exit code proves nothing and only
the target's own answer does.

A remote the source does not have is deleted rather than offered (`_delete_unused_remotes`).
It runs after the converge loop, re-reads what the target actually holds — every installed
ref, runtimes and machine-specific apps included — and deletes only a remote nothing names
as its origin; while anything still names it, it stays and the run says which refs kept it.
Counting after the loop rather than at plan time is what makes "after this run's approved
removals" a measurement instead of a prediction: an approved removal that then failed leaves
its ref installed, and its remote with it.

A remote carries its TRUST as part of the item, not as a property of the machine that
happens to hold it (#215): `FlatpakRemoteItem` records the remote's GPG-verification
setting and the digest of its own ostree keyring, and convergence replicates both —
`flatpak remote-add --gpg-import=<staged key>` for a signed remote, `--no-gpg-verify`
only when the SOURCE remote is itself unverified. Without this a replicated remote is
configured but unusable: flatpak refuses every install from it with `Can't check
signature: public key not found`. The key bytes are synced byte-for-byte from the source
machine and are never re-fetched from a vendor (ADR-020 D-12's rule for apt signing
keys), staged under the target's `~/.cache/pc-switcher/` exactly as `apt_sync` stages
`/etc/apt` content, because SFTP reaches only the SSH user's own home.

A verified remote need not hold a keyring of its own: libostree also verifies against
`_OSTREE_TRUSTED_ANCHOR_DIR`, which is the MACHINE's trust rather than the remote's, and
replicating name, URL and `gpg-verify` alone hands the target a remote it cannot install
from. `_anchors_to_import` closes that by giving the replicated remote the source's anchor
files as its OWN keyring: the target's remote then trusts exactly what the source's remote
trusted, and no other remote on the target gains anything. An anchor the target already
holds is left alone, and a verified remote with no key material anywhere on the source is
refused outright — every approved ref from it fails naming the remote, which is the honest
end of `PKG-FR-FLATPAK-REMOTE-TRUST` when there is nothing to sync.

The third place a remote's trust can live is the ostree per-remote option `gpgkeypath`, read
off the source installation's own `repo/config` (`_FLATPAK_REPO_CONFIG_CMD_TEMPLATE`) and
carried like any other key. flatpak never writes that option, so only a hand-edited config
sets it — but `PKG-FR-FLATPAK-REMOTE-TRUST` knows no exception, and a remote holding its key
that way is unusable on a target that did not get it. The files it names travel
alongside the anchor set rather than instead of it, because libostree treats them that way:
a per-remote `trustedkeys.gpg` suppresses the anchor directory and `gpgkeypath` does not
(`_ostree_repo_gpg_prepare_verifier`, libostree v2024.5).

A remote's FILTER replicates by the same mechanism as its key. flatpak records only the
path (`_FLATPAK_REMOTES_CMD_TEMPLATE`'s fourth column), so the file at that path is copied
byte-for-byte to the same absolute path on the target and `remote-modify --filter` applies it
there. It lands with the remote and BEFORE the refs install (`_converge_remote_filters`, run
between the derived writes and the converge loop): the remote is added or repointed, its
filter is brought to the source's, and only then does anything install from it — so no run
can end with the target's remote offering more than either machine meant. A remote the source
does not filter has the target's own filter taken off in that same pass, which is the other
half of converging and what makes an unfiltered source remote reach an unfiltered target one.
A filter that cannot be copied, written or applied warns naming the remote and the path, and
every approved ref whose OWN origin is that remote fails with the same reason
(`_failed_remote_filters`, consulted by `_converge_ref` exactly as a failed derived write is).

Nothing is cleared for the installs' benefit. A filter narrower than the set being replicated
would block them — measured, not assumed: on Flatpak 1.14.6 an install of a ref its remote's
filter denies exits 1 with `Nothing matches <id> in remote <remote>` and lands nothing, while
the same install of an allowed ref succeeds
(`docs/adr/considerations/adr-020-flatpak-filter-and-trust-measurements.md`). But a source
whose own filtered remote will not offer an app that source has installed from it is
contradicting itself, and `_abort_on_a_source_filter_that_denies_its_own_apps` ends the run at
plan time naming the app, the remote and the filter rather than carrying logic for a state that
should not exist.

WHICH app that is, flatpak answers rather than this job: `flatpak remote-ls <name>` lists the
remote AS ITS FILTER RESTRICTS IT (measured), so an app the source installed from that remote
and absent from that remote's own listing is one the two machines cannot be made to agree on.
Nothing here reimplements flatpak's glob matching, so nothing here can drift from it when
flatpak changes it, and a filter file edited since it was applied is judged as it now stands,
which reading the file could not tell (flatpak re-reads the configured path per listing and
keeps a backup copy of its own — measured).

The listing is the whole of the evidence, and it does not say WHY: a delisted ref and a ref
`remote-ls` will not list for its architecture are absent from it exactly as a denied one is,
and flatpak 1.14.6 offers no unfiltered view of the same remote to tell them apart — the
`remote-ls <uri>` form its help documents is refused for an `http(s)` URL, `remote-info` is
filtered too, and the only unfiltered answer would come from reconfiguring the source's own
remote, which no run may do. So the abort names what was measured — the remote does not offer
the app under the filter it carries — and names the filter and the app as the two things that
can be corrected, rather than asserting which of them is wrong. A listing flatpak declines to
produce — an unreachable remote, a filter file it refuses to parse — says nothing at all and
ends no run: the abort needs certainty, and `remote-modify --filter` reports that file's real
error later.

The flatpak OSTree store stays authoritative for its own state (D-01): this job never
WRITES into `/var/lib/flatpak` or `~/.local/share/flatpak`, only shells out to `flatpak`
itself. It does READ one file there, `<installation>/repo/<remote>.trustedkeys.gpg`,
because no flatpak command prints or exports a remote's key and libostree's own CLI is
not installed alongside flatpak on Ubuntu. `flatpak_sync_exclude_paths()` exports
`~/.local/share/flatpak` so `folder_sync` stops mirroring the store this job owns
(D-29, ADR-018) — but NOT `~/.var/app`, which is per-application USER DATA that stays
folder_sync's territory;
D-17's job-before-folder_sync ordering exists precisely so `flatpak install` creates
the store first and folder_sync's data lands on top of it, never the reverse.

`FlatpakSyncJob` subclasses `PackageSyncJob` and implements the abstract `plan()`, for the
same reason `SnapSyncJob` does: what a diff even IS differs per manager, so the base class
holds no diff to inherit. apt's own diff is apt-package-shaped — one item class,
`MISSING_ON_TARGET`/`EXTRA_ON_TARGET`/`VERSION_MISMATCH` only, no notion of derived work
that must land ahead of an item. `plan()` here reuses every manager-agnostic building
block the shared core provides — `DecisionFile`/`filter_inert` (D-08's machine-local
skip-always filtering) and `PackageSyncJob._build_review_groups` (D-24's action-grouped
review) — so only capture, diff and converge are genuinely flatpak-specific.
`accept_review()` is overridden to turn the approved refs into the derived remote set,
and `apply()` to write those remotes before the base converge loop reaches its first ref.
`execute()` is inherited unchanged and is where this job's own single review happens,
before its own first mutating command: there is no coordinator and no review spanning two
managers (D-15, D-24).

Flatpak ref VERSIONS are captured for reporting only (D-04, like apt package
versions): a version difference on a ref present in the same scope on both machines
is a `REPORT_ONLY` diff, never something this job installs or removes to force.

A ref's ORIGIN is reported the same way and outranks it (`_diff_flatpak_refs`, D-41). The
two install-time refusals above guard an install, and a ref already present on BOTH machines
issues none — so the already-diverged case they cannot see is `ORIGIN_MISMATCH`: same scope,
same `<application>/<arch>/<branch>`, two vendors. It is compared by the remotes' URLs rather
than their names for the reason `5fc3ac01` records, so a target `flathub` pointing at the beta
repo is caught and a remote the two machines merely named differently is not. It is
`REPORT_ONLY` because flatpak leaves no verb: `flatpak install <other remote> <installed ref>`
refuses with `already installed from remote <name>` (measured), so converging it would mean
uninstalling the app the user has and reinstalling it from the other vendor.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, override

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
    Machines,
    build_version_mismatch_detail,
)
from pcswitcher.jobs.packages.probes import require_answer
from pcswitcher.jobs.packages.review import (
    REPO_CONFLICT_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.state import DecisionEntry, filter_inert, marks_on_either
from pcswitcher.jobs.packages.sync_core import (
    ConvergeItemFailed,
    PackageItemFailures,
    PackagePlan,
    PackageSyncJob,
)
from pcswitcher.models import (
    CommandResult,
    FirstSyncScope,
    Host,
    LogLevel,
    ProgressUpdate,
    SyncAborted,
    ValidationError,
)
from pcswitcher.sudoers import passwordless_sudo_hint

__all__ = ["FlatpakSyncJob", "flatpak_sync_exclude_paths"]

# `flatpak list --app` is run with an explicit --columns flag naming exactly these
# five fields in this order (RESEARCH: verified live against Flatpak 1.14.6) — unlike
# `snap list --all`, the invocation itself names its columns, so the output has no
# header row and is parsed by fixed tab-separated position.
#
# `ref` is what makes a ref nameable. It prints `<application>/<arch>/<branch>` (measured
# live: `com.slack.Slack/x86_64/stable`), and that exact string is what `flatpak install`
# and `flatpak uninstall` accept positionally — the bare application id is NOT enough on a
# remote carrying two branches of one id, where `flatpak install <remote> <id>` exits 1
# with `Multiple branches available for <id>` (measured against real Flathub-beta, which
# carries both `stable` and `beta` for `org.mozilla.firefox`). Without the branch such an
# app fails to converge on every single run.
_FLATPAK_LIST_CMD = "flatpak list --app --columns=application,version,origin,installation,ref"

# Every installed ref on the source, runtimes included (no `--app`), for the runtime half of
# remote derivation: an approved app pulls its runtime, and the runtime may come from a
# remote no directly-approved ref uses. Same five columns so one parser serves both — the
# `version` a runtime reports is unused here.
_FLATPAK_ALL_REFS_CMD = "flatpak list --columns=application,version,origin,installation,ref"

# The runtime one installed app is built against, printed as a bare `<id>/<arch>/<branch>`
# ref with no `runtime/` prefix — i.e. byte-identical to what the `ref` column prints for
# that runtime, so the origin lookup is a dictionary hit and needs no reformatting
# (measured live on Flatpak 1.14.6: `org.gnome.Platform/x86_64/50`, 10 ms, no network).
_FLATPAK_RUNTIME_CMD_TEMPLATE = "flatpak info {flag} --show-runtime {ref}"

# Same reasoning for `flatpak remotes`, but flatpak tracks remotes PER INSTALLATION —
# even a byte-identical `flathub` URL is two separate configuration entries — so this
# is run once per scope rather than once combined (module docstring, D-14). Two of the four
# columns are configuration this job replicates rather than identity:
#
# `options` is the only place flatpak exposes a remote's GPG-verification state (#215): a
# comma-separated token list in which `no-gpg-verify` appears exactly when the remote's
# `gpg-verify` is false.
#
# `filter` is the path `flatpak remote-modify --filter=<path> <name>` recorded. Measured in a
# stock `ubuntu:24.04` container on Flatpak 1.14.6: that command exits 0 and stores the path
# VERBATIM as `xa.filter` in the installation's `repo/config`, without validating it — a
# relative path and a path that does not exist are both accepted. The path is what flatpak
# reports and what it re-reads on every use, so the filter's content is an ordinary file at an
# arbitrary absolute location outside the ostree store, which is what `_converge_remote_filters`
# carries. flatpak keeps a copy of its own beside the config (`repo/<name>.filter`, headed
# `backup copy of <path>, do not edit!`) and falls back to it once the path is gone, which is
# why the file is the thing to replicate and the copy is not.
#
# An unfiltered remote prints `-` in that column rather than nothing (measured), so with
# `filter` requested last a line carries four fields even for a remote with no options at all
# — the two-and-three-field widths the same command produced when `options` was last are
# still accepted, since a trailing EMPTY column is omitted rather than printed.
_FLATPAK_REMOTES_CMD_TEMPLATE = "flatpak remotes {flag} --columns=name,url,options,filter"

# What that column prints for a remote carrying no filter.
_NO_FILTER = "-"

# What a remote offers, in flatpak's own words — the only judge of a filter this job has
# (`_abort_on_a_source_filter_that_denies_its_own_apps`). One ref per line, printed with its
# kind: `app/<id>/<arch>/<branch>`, i.e. exactly what `flatpak list --columns=ref` prints with
# `app/` in front (measured, Flatpak 1.14.6).
#
# The listing carries the named remote's own filter, which is what makes it an answer about the
# filter at all (measured, over `file://` and over `http://` alike). There is no unfiltered
# counterpart to compare it against: `remote-ls <uri>`, which flatpak's own help documents, is
# accepted for a `file://` URL and refused for an `http(s)` one with `Remote "<url>" not found
# in the system installation`, and `remote-info` applies the filter as well.
#
# `--arch='*'` because `remote-ls` otherwise lists the running machine's architecture alone
# (measured: an `aarch64` ref in a single-summary repository appears only with it). It is not a
# guarantee of completeness — a real remote serves per-architecture subsummaries and flathub
# answered `--arch='*'` with 10178 refs, not one of them `i386`, on an x86_64 host — which is
# among the reasons the abort claims only what the listing shows and never a cause.
_FLATPAK_REMOTE_LS_CMD_TEMPLATE = "flatpak remote-ls {flag} --arch='*' --columns=ref {remote}"

# The kind prefix `remote-ls` prints and `flatpak list --columns=ref` does not, and the only
# kind the filter check asks about: `capture_source_items` lists apps (`--app`).
_APP_REF_PREFIX = "app/"

# The token `flatpak remotes --columns=options` prints for a remote with GPG
# verification turned off.
_NO_GPG_VERIFY_OPTION = "no-gpg-verify"

# ostree stores a remote's own trusted public keys in one file per remote inside the
# installation's repo, named `<remote>.trustedkeys.gpg` (verified live, libostree
# 2024.5). Nothing in flatpak's CLI prints or exports that key, and the `ostree` binary
# is not installed by a flatpak install on Ubuntu — so the digest is read straight off
# the file. This is the one place this job looks INSIDE the OSTree store, and it is a
# read: D-01's "flatpak stays authoritative for its own state" bars WRITING there, which
# convergence still does exclusively through `flatpak remote-add`/`remote-modify`.
_TRUSTEDKEYS_SUFFIX = ".trustedkeys.gpg"

# The ostree repo config of one installation, where a remote's `gpgkeypath` lives — the third
# place a remote's key material can be, besides its own keyring and the machine-level anchor
# directory (`PKG-FR-FLATPAK-REMOTE-TRUST` knows no exception, so neither may this job).
# `flatpak remotes` cannot report it and no flatpak command writes it: only a hand-edited
# config sets it, and only this file records it. Unguarded on the exit code for the same
# reason the keyring digests are: a scope with no flatpak installation has no config file, and
# that is ordinary rather than a failure.
_FLATPAK_REPO_CONFIG_CMD_TEMPLATE = "cat {directory}/config 2>/dev/null"

# The per-remote option that names key files or directories outside the ostree store, and the
# two characters libostree accepts between several of them
# (`ot_keyfile_get_string_list_with_separator_choice (..., "gpgkeypath", ";,", ...)` in
# `_ostree_repo_gpg_prepare_verifier`, libostree v2024.5). Each entry is an ASCII-armoured key
# file, or a directory whose regular files are all read as one
# (`_ostree_gpg_verifier_add_keyfile_path`), and the same function is what settles that these
# keys ADD to the anchor directory rather than suppressing it as a per-remote keyring does.
_GPGKEYPATH_OPTION = "gpgkeypath"
_GPGKEYPATH_SEPARATORS = ";,"

# One batched `sha256sum` per scope over that glob, mirroring `apt_sync`'s
# `_capture_dir_digests` — never one command per remote. A scope with no keyring at all
# makes the glob match nothing, so `sha256sum` prints nothing on stdout and exits 1;
# stderr is discarded and the empty stdout parses to an empty map (verified live).
_FLATPAK_KEYRING_DIGESTS_CMD_TEMPLATE = "sha256sum {directory}/*{suffix} 2>/dev/null"

# The system installation's fixed location. Its `repo/` is 0755 root with 0644 keyring
# files (verified live), so reading a digest there needs no sudo even though writing to
# it does.
_FLATPAK_SYSTEM_INSTALLATION = Path("/var/lib/flatpak")

# The one keyring directory libostree consults BESIDES a remote's own
# `<remote>.trustedkeys.gpg` — measured on libostree 2024.5: a signed pull succeeds with the
# key here and fails with it in `/etc/ostree/trusted.gpg.d`, and a remote holding a keyring of
# its own suppresses this directory outright, which is why `_anchors_to_import` acts only on a
# remote that holds none (`docs/adr/considerations/adr-020-flatpak-filter-and-trust-measurements.md`).
# A remote can therefore be verified while holding no key of its own, its trust supplied by
# whichever machine put a keyring here. That is trust the MACHINE holds rather than the remote, so replicating the
# remote alone gives the target a remote marked verified against a key it may not have, and
# every install from it fails the signature check. `_anchors_to_import` carries these files
# into the replicated remote's OWN keyring instead of replicating them machine-wide: the
# target's remote then trusts exactly what the source's remote trusted, and nothing else on
# the target gains trust it did not have.
_OSTREE_TRUSTED_ANCHOR_DIR = Path("/usr/share/ostree/trusted.gpg.d")

# The two `.gpg` names libostree skips in that directory — gpg's own database files rather
# than keyrings (`_ostree_gpg_verifier_add_keyring_dir_at`, libostree v2024.5, which takes
# regular files whose name ends in `.gpg` and then drops exactly these two). Everything that
# survives that filter is merged into ONE verifier keyring and a signature verifies if any
# key in it matches, which is what `_anchors_to_import` rests on: nothing anywhere records
# which of those keys a given remote's signatures were checked against, so the trust a keyless
# verified remote rests on is the whole merged set, not one file inside it.
_ANCHOR_FILES_LIBOSTREE_IGNORES = frozenset({"trustdb.gpg", "secring.gpg"})


# Masks are ALSO per-installation (#208, D-10), listed one pattern per line with no
# header — but the scope flag MUST precede the `mask` subcommand: bare `flatpak mask`
# omits --user masks and defaults to --system (RESEARCH: verified live, Flatpak 1.14.6),
# so this always names its scope explicitly, once per scope like remotes.
_FLATPAK_MASK_CMD_TEMPLATE = "flatpak {flag} mask"

# Both scopes this item model and flatpak's own --user/--system flags recognise.
_SCOPES: tuple[Literal["user", "system"], ...] = ("user", "system")

# Every id a remote can carry, in every direction. `_record_permanent_skips` filters on it
# so "a remote is never recorded machine-specific" holds even for a decision that arrives
# from the review's automation hook or a hand-built `ReviewOutcome`, not only for the one
# screen that no longer offers the promotion.
_REMOTE_ITEM_ID_PREFIX = "flatpak:remote:"

# Identity of an installed application. Named because `observe_absent_marks` has to tell a
# decision file's ref entries from its remote and mask ones, and the file is hand-editable —
# so the ID is what says which is which, not the `item_class` recorded beside it.
_REF_ITEM_ID_PREFIX = "flatpak:ref:"

# Identity of a remote-CONFLICT review entry (ADR-020 D-41). Deliberately not a
# `flatpak:remote:` id, because it is not the same question: that one asks whether to DELETE
# a remote the source no longer has, this one asks which of two configurations of a remote
# BOTH machines have should win. It names no diff and reaches no decision file — a conflict
# exists only between `_build_review_groups` and `accept_review`, so `_record_permanent_skips`
# needs no filter for it (unlike `flatpak:remote:`, which does label real diffs).
_CONFLICT_ID_PREFIX = "flatpak:conflict:"

# The two facets of a remote whose divergence a machine-specific ref can be HARMED by, and so
# the only ones that turn a silent repoint into ruling 6's question. The URL decides which
# vendor's builds that ref updates from; the verification setting decides whether it is
# checked at all, in either direction. The third facet a remote carries, its per-remote
# signing key, deliberately does not: `--gpg-import` merges into the remote's ostree keyring
# rather than replacing it (the same measured fact `_origin_refusal` cites for not comparing
# digests), so importing the source's key can neither move a ref's origin nor withdraw trust
# — there is no harm behind it to put to the user, and a key-only difference stays silent.
_URL_FACET = "url"
_VERIFICATION_FACET = "gpg verification"
_PROVENANCE_FACETS = frozenset({_URL_FACET, _VERIFICATION_FACET})

# Why a derived remote is being provisioned, for the dry-run preview: the app's own origin
# is obvious from the ref, its runtime's is not.
_DERIVED_REASON_WORDS: dict[str, str] = {
    "ref_origin": "an approved ref's origin",
    "runtime_origin": "the runtime an approved ref needs",
}

# Binaries this job runs under sudo, quoted back to the user when the passwordless-sudo
# check fails (ADR-013). Only needed when a system-scope item is actually in play —
# user-scope flatpak operations need no root at all (ASVS V4, T-02-23).
_TARGET_SUDO_COMMANDS = ("/usr/bin/flatpak",)

# Directory this job owns and exports to folder_sync (D-29): the OSTree store and
# flatpak's own per-installation metadata, NOT `~/.var/app` (per-application user
# data, folder_sync's territory — module docstring).
_FLATPAK_DATA_RELPATH = Path(".local") / "share" / "flatpak"


# -- flatpak-owned item shapes and review details -------------------------------------
#
# Here rather than in the shared `packages/items.py`: no other job constructs a flatpak
# item or writes its review details.


@dataclass(frozen=True)
class FlatpakItem:
    """One installed flatpak application ref (D-06), scoped user or system.

    `scope` lives inside the identity string, not just as a field: this project's own
    machine has several runtimes installed in both scopes with the same application
    id, and folding scope into `item_id` is what makes "same name, different scope"
    fall out of the generic diff engine as two distinct items with no special-casing
    in `flatpak_sync`.

    So does `ref` (`<application>/<arch>/<branch>`), and NOT the bare application id, for
    two reasons that the application id alone cannot serve:

    - Two branches of one application id can be installed side by side in ONE scope —
      that is what branches are for — so `(scope, application)` is not a unique key for a
      machine's own listing, and keying on it silently drops one of the two rows when the
      captured items are folded into a `{item_id: item}` map.
    - The install and the removal both need the full ref anyway (`_FLATPAK_LIST_CMD`), so
      the identity and the command argument are the same string rather than two facts that
      can drift.

    ORIGIN deliberately stays out (a field, not identity), because the install-plus-removal
    pair it would produce cannot converge: `flatpak install <other remote> <ref>` on an
    already-installed ref exits with `<ref> is already installed from remote <name>`, so
    the install half could never run while the removal half proposed deleting the app the
    user has. A BRANCH difference has the opposite property — branches coexist, so the
    install half succeeds and the removal half then leaves exactly the source's set — which
    is why a branch change replicates as two items and an origin change does not.
    """

    application: str
    version: str
    origin: str
    scope: Literal["user", "system"]
    ref: str

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.FLATPAK_REF

    @property
    def item_id(self) -> str:
        """Stable identity string: `flatpak:ref:<scope>:<application>/<arch>/<branch>`."""
        return f"{_REF_ITEM_ID_PREFIX}{self.scope}:{self.ref}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.ref} ({self.version}, {self.origin}, {self.scope})"


@dataclass(frozen=True)
class FlatpakRemoteItem:
    """One configured flatpak remote (D-11/D-14), scoped user or system.

    Flatpak tracks remotes per-installation: `flathub` commonly exists in both scopes
    with a byte-identical URL, yet the two are separate configuration the target must
    provision separately. `scope` inside `item_id` (same reasoning as `FlatpakItem`)
    is what keeps those two facts distinct rather than colliding on the shared name.

    `gpg_verify` and `key_digest` are the remote's TRUST configuration (#215). A remote
    replicated as name+url alone is configured but unusable — flatpak refuses every
    install from it with `Can't check signature: public key not found` — so trust is
    part of the item, not an incidental of the machine. `gpg_verify` is read from
    `flatpak remotes --columns=options` (the `no-gpg-verify` token) and `key_digest` is
    the sha256 of the remote's own ostree keyring, `<installation>/repo/<name>.
    trustedkeys.gpg`; it is `None` for an unverified remote and for a verified one whose
    trust comes from the machine-level anchor `_OSTREE_TRUSTED_ANCHOR_DIR` rather than from
    a per-remote key. The anchor deliberately stays OFF the item: it is one machine-wide
    fact, not a facet of any one remote, and folding it in would make every remote on two
    machines with different anchors compare unequal and be rewritten every run. It is read
    once per run and consulted at converge time instead (`_anchors_to_import`).

    The DIGEST lives on the item, not the key bytes. An item is carried through the diff,
    the review and the decision file, all of which want an identity and a comparison,
    never a payload; the bytes themselves are synced
    separately and byte-for-byte (`flatpak_sync` stages the source's keyring file and
    passes it to `flatpak remote-add --gpg-import`), which is ADR-020 D-12's rule that
    key material is copied from the source machine and never re-fetched from a vendor.
    """

    name: str
    url: str
    scope: Literal["user", "system"]
    gpg_verify: bool = True
    key_digest: str | None = None
    # The absolute path of the remote's ref filter, or `None` for an unfiltered remote
    # (`_FLATPAK_REMOTES_CMD_TEMPLATE`). `compare=False` because the filter is converged by a
    # pass of its own ahead of the refs (`_converge_remote_filters`): letting it into
    # `__eq__` would make `_write_derived_remote`'s whole-item equality test miss and issue a
    # `remote-modify --url` that changes nothing, every run.
    filter_path: str | None = field(default=None, compare=False)
    # The paths the remote's ostree `gpgkeypath` option names on the machine this item was
    # captured from, in the order the option lists them (`_GPGKEYPATH_OPTION`). Read on the
    # SOURCE only: what travels is the bytes at those paths, imported into the target's own
    # per-remote keyring, so the target never reports a `gpgkeypath` of its own and comparing
    # one side's filesystem paths against the other's would be a permanent inequality by
    # construction. `key_digest` is where a trust comparison lives; `_write_derived_remote`
    # asks about these separately, as it already does about the machine-level anchors.
    key_paths: tuple[str, ...] = field(default=(), compare=False)

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.FLATPAK_REMOTE

    @property
    def item_id(self) -> str:
        """Stable identity string: `flatpak:remote:<scope>:<name>`."""
        return f"flatpak:remote:{self.scope}:{self.name}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.name} remote ({self.scope}): {self.url}"


@dataclass(frozen=True)
class FlatpakMaskItem:
    """One flatpak mask pattern (#208, D-10), scoped user or system.

    A mask is a pattern flatpak refuses to install or update (`flatpak mask <pattern>`),
    replicated as a PURE pattern — never filtered to installed refs (D-10) — so a mask
    edit reads as remove-old + add-new and a scope split as add + remove, reported as
    found rather than normalised. `scope` lives inside `item_id` (same reasoning as
    `FlatpakItem`/`FlatpakRemoteItem`) so the same pattern masked in both scopes falls
    out of the generic diff as two distinct items.
    """

    pattern: str
    scope: Literal["user", "system"]

    ITEM_CLASS: ClassVar[ItemClass] = ItemClass.FLATPAK_MASK

    @property
    def item_id(self) -> str:
        """Stable identity string: `flatpak:mask:<scope>:<pattern>`."""
        return f"flatpak:mask:{self.scope}:{self.pattern}"

    def label(self) -> str:
        """Human-readable text for the review UI and logs."""
        return f"{self.pattern} (mask, {self.scope})"


def _origin_display(name: str, url: str | None) -> str:
    """One side of an origin comparison, as the user meets it: the remote NAME they see in
    `flatpak list --columns=origin`, and the URL that decides which vendor it actually is.

    The URL is what the comparison runs on (ADR-020 D-41), but a bare URL names nothing the
    user configured, and a bare name is exactly the string that reads identical on both
    machines in the same-name-different-origin case.

    A machine that configures no remote by that name has no URL, and saying so is load-bearing
    rather than cosmetic: `_same_vendor` treats an absent URL as matching nothing, so the two
    sides of such a comparison always differ — and printing the bare name on both would state
    a difference while showing two identical strings.
    """
    return f"{name} ({url})" if url is not None else f"{name} (no URL: the machine does not configure {name})"


def build_flatpak_origin_mismatch_detail(
    source_origin: str, source_url: str | None, target_origin: str, target_url: str | None, machines: Machines
) -> str:
    """Detail for a ref's `ORIGIN_MISMATCH` diff: the same ref, two vendors (ADR-020 D-41).

    Same shape and same reason as `apt_sync`'s `build_origin_mismatch_detail` — report only,
    both sides named, because neither machine is wrong and only the user can say which one
    is the odd one out. Converging it is not on the table at all here: `flatpak install
    <other remote> <installed ref>` refuses with `already installed from remote <name>`
    (measured), so the only mechanical resolution would be uninstall-then-reinstall, which
    is a cross-vendor replacement of an app the user has, not a float (D-04).
    """
    source = _origin_display(source_origin, source_url)
    target = _origin_display(target_origin, target_url)
    return f"{machines.source} installed it from {source}, {machines.target} from {target}"


def _lines(output: str) -> list[str]:
    """Non-blank lines, exactly as every tab-separated `flatpak` list command in this
    module produces them — no per-field stripping, since a real flatpak app id, remote
    name or URL never carries leading/trailing whitespace of its own.
    """
    return [line for line in output.splitlines() if line.strip()]


def _scope_flag(scope: str) -> str:
    return "--user" if scope == "user" else "--system"


def _sudo_prefix(scope: str) -> str:
    """`sudo ` for a system-scope converge command, empty for user-scope (T-02-23,
    ASVS V4): `--system` writes into `/var/lib/flatpak`, root-owned, while `--user`
    writes into the invoking user's own home directory and needs no elevation at
    all. The scope flag alone decides this — never a separate "is this destructive"
    guess — so a user-scope item can never silently escalate to a root-run command.
    """
    return "sudo " if scope == "system" else ""


def _repo_dir_expression(scope: str) -> str:
    """The scope's ostree repo directory, as a SHELL EXPRESSION for `run_command`.

    `$HOME` is left for the remote shell to expand rather than resolved here: the user
    installation lives under the invoking user's own home on each machine, and the two
    machines' usernames differ. Both ends therefore compute the same relative location
    (`~/.local/share/flatpak`, the very path `flatpak_sync_exclude_paths()` already
    claims) in their own environment. `$XDG_DATA_HOME` is deliberately NOT consulted: it
    is typically set in a desktop session and unset over a non-interactive SSH exec
    channel, so honouring it would make the source and the target disagree about where
    the same user's remotes live and manufacture a phantom key diff.
    """
    if scope == "system":
        return f"{_FLATPAK_SYSTEM_INSTALLATION}/repo"
    return f"$HOME/{_FLATPAK_DATA_RELPATH}/repo"


def _keyring_digests_cmd(scope: str) -> str:
    return _FLATPAK_KEYRING_DIGESTS_CMD_TEMPLATE.format(
        directory=_repo_dir_expression(scope), suffix=_TRUSTEDKEYS_SUFFIX
    )


def _repo_config_cmd(scope: str) -> str:
    """The scope's ostree `repo/config`, read as a whole (`_FLATPAK_REPO_CONFIG_CMD_TEMPLATE`)
    — the only place a remote's `gpgkeypath` is recorded. Built at call time for the reason
    `_keyring_digests_cmd` is: `_repo_dir_expression` stays the one place the path is written.
    """
    return _FLATPAK_REPO_CONFIG_CMD_TEMPLATE.format(directory=_repo_dir_expression(scope))


def _trust_anchor_digests_cmd() -> str:
    """Digests of the anchor files in `_OSTREE_TRUSTED_ANCHOR_DIR`, batched exactly like the
    per-remote keyring read and unguarded for the same reason: a machine with no anchor at all
    leaves the glob unmatched, so `sha256sum` prints nothing and exits 1, and that is the
    ordinary case rather than a failure.

    `*.gpg` rather than `*`, because that is the set libostree itself loads
    (`_ANCHOR_FILES_LIBOSTREE_IGNORES`). A file in that directory libostree never reads is not
    trust the source's remote rests on, and importing it would grant the target's remote
    something the source's never had — or fail the whole write, since `--gpg-import` is given
    a file it cannot parse as a keyring.

    Built at call time rather than held as a constant, exactly as `_keyring_digests_cmd` is,
    so the directory stays the single place the path is written down.
    """
    return f"sha256sum {_OSTREE_TRUSTED_ANCHOR_DIR}/*.gpg 2>/dev/null"


def _source_keyring_path(item: FlatpakRemoteItem) -> Path:
    """The LOCAL path of the source machine's own keyring file for `item`.

    `send_file` transfers from the local filesystem, and the source executor runs on
    this machine as this user (the same assumption `apt_sync.etc_apt.EtcApt._write_or_remove`
    makes for `/etc/apt`), so `Path.home()` resolves the very directory
    `_repo_dir_expression("user")`'s `$HOME` expands to on the source side.
    """
    installation = _FLATPAK_SYSTEM_INSTALLATION if item.scope == "system" else Path.home() / _FLATPAK_DATA_RELPATH
    return installation / "repo" / f"{item.name}{_TRUSTEDKEYS_SUFFIX}"


def _parse_file_digests(output: str) -> dict[str, str]:
    """`{path: sha256}` from a batched `sha256sum` run — `<digest>  <path>` per line."""
    digests: dict[str, str] = {}
    for line in _lines(output):
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, path = parts
        digests[path.strip()] = digest
    return digests


def _anchor_digests(output: str) -> dict[str, str]:
    """`{path: sha256}` for the machine-level anchor files libostree actually merges.

    The glob already narrows to `*.gpg` (`_trust_anchor_digests_cmd`); this drops the two
    names libostree skips inside that set (`_ANCHOR_FILES_LIBOSTREE_IGNORES`). Done here as
    well as in the glob because it is the same rule read from the other end: the shell can
    express the suffix cheaply and the exclusions clumsily.
    """
    return {
        path: digest
        for path, digest in _parse_file_digests(output).items()
        if Path(path).name not in _ANCHOR_FILES_LIBOSTREE_IGNORES
    }


def _parse_repo_config_key_paths(output: str) -> dict[str, tuple[str, ...]]:
    """`{remote name: the paths its `gpgkeypath` names}` from one installation's ostree
    `repo/config` (`_FLATPAK_REPO_CONFIG_CMD_TEMPLATE`).

    Hand-scanned rather than handed to `configparser`, for the reason every other read in
    this module is: the file belongs to ostree, a line this job does not understand is not a
    reason to fail a run, and `configparser` raises on duplicate keys and on a section header
    it dislikes. Only two line shapes matter — `[remote "<name>"]` and, inside one,
    `gpgkeypath=<paths>` — and anything else is skipped in silence.
    """
    key_paths: dict[str, tuple[str, ...]] = {}
    remote: str | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = re.fullmatch(r'\[remote\s+"(?P<name>[^"]+)"\]', line)
            remote = section.group("name") if section is not None else None
            continue
        if remote is None or "=" not in line or line.startswith(("#", ";")):
            continue
        option, _, value = line.partition("=")
        if option.strip() != _GPGKEYPATH_OPTION:
            continue
        paths = re.split(f"[{re.escape(_GPGKEYPATH_SEPARATORS)}]", value)
        stripped = tuple(path.strip() for path in paths if path.strip())
        if stripped:
            key_paths[remote] = stripped
    return key_paths


def _parse_keyring_digests(output: str) -> dict[str, str]:
    """`{remote name: sha256}` from one scope's batched `sha256sum` output.

    Mapped by stripping the `.trustedkeys.gpg` suffix off the basename — a remote name may
    contain dots (`my.remote.name.trustedkeys.gpg`, verified live), so only the fixed suffix
    is removed, never everything after the first dot.
    """
    digests: dict[str, str] = {}
    for path, digest in _parse_file_digests(output).items():
        name = Path(path).name
        if not name.endswith(_TRUSTEDKEYS_SUFFIX):
            continue
        digests[name[: -len(_TRUSTEDKEYS_SUFFIX)]] = digest
    return digests


def _split_flatpak_item_id(item_id: str, expected_kind: Literal["ref", "remote", "mask"]) -> tuple[str, str]:
    """`(scope, name)` from a `flatpak:<kind>:<scope>:<name>` item id (`FlatpakItem` above).

    `name` is the full `<application>/<arch>/<branch>` ref for a ref, the remote name for
    a remote, the pattern for a mask — none carries a `:` of its own: application ids and
    remote names are dotted/alnum tokens, and refs and mask patterns are partial refs with
    `/` and `*` but never `:` (RESEARCH Standard Stack, verified live), so a fixed 3-colon
    split is exact rather than a heuristic (the `split(":", 3)` cap keeps every `/` intact).
    This is a legitimate use of a
    stable identity string (the same pattern `apt_sync._package_name` and
    `snap_sync._snap_name` already establish): the plan only ever carries `ItemDiff`s,
    not the richer item dataclasses, so converge() recovers scope/name from the id.
    """
    parts = item_id.split(":", 3)
    if len(parts) != 4 or parts[0] != "flatpak" or parts[1] != expected_kind:
        raise ValueError(f"not a flatpak {expected_kind} item id: {item_id!r}")
    _, _, scope, name = parts
    return scope, name


def _parse_flatpak_list(output: str) -> list[FlatpakItem]:
    """Parse `_FLATPAK_LIST_CMD`'s tab-separated output into `FlatpakItem`s.

    A line whose `installation` field is neither `user` nor `system` (flatpak permits
    additional named installations beyond the two this item model represents) is
    skipped rather than guessed at — this project's own machines only ever use the
    two standard scopes (CONTEXT.md's live inventory), and a third would need its own
    modelling decision, not a silent default.
    """
    items: list[FlatpakItem] = []
    for line in _lines(output):
        fields = line.split("\t")
        if len(fields) != 5:
            continue
        application, version, origin, installation, ref = fields
        scope: Literal["user", "system"]
        if installation == "user":
            scope = "user"
        elif installation == "system":
            scope = "system"
        else:
            continue
        items.append(FlatpakItem(application=application, version=version, origin=origin, scope=scope, ref=ref))
    return items


def _parse_flatpak_remotes(
    output: str,
    scope: Literal["user", "system"],
    key_digests: Mapping[str, str],
    key_paths: Mapping[str, tuple[str, ...]] | None = None,
) -> list[FlatpakRemoteItem]:
    """Parse one scope's `flatpak remotes --columns=name,url,options` output.

    `scope` is a parameter, not a parsed column: unlike `flatpak list`, this command
    has no scope column of its own — the caller already knows which scope it asked
    about, because it chose the `--user`/`--system` flag (module docstring).
    `key_digests` is the same scope's `_parse_keyring_digests` map, joined here by
    remote name so a remote's trust arrives as part of the item rather than as a
    second lookup at converge time (#215).

    A line carries four fields, and two or three are accepted as well because a trailing
    empty column is omitted rather than printed (`_FLATPAK_REMOTES_CMD_TEMPLATE`); `-` in the
    filter column is flatpak's own word for "no filter". A remote absent from `key_digests`
    keeps `key_digest=None`, which is the honest reading of "verification is on but this
    remote carries no key of its own" — trust then comes from a machine-level anchor or from
    `key_paths`, the same scope's `_parse_repo_config_key_paths` map, joined here by remote
    name for the same reason the digests are. `key_paths` is read on the source alone
    (`FlatpakRemoteItem.key_paths`), so the target's parse passes none.
    """
    items: list[FlatpakRemoteItem] = []
    for line in _lines(output):
        fields = line.split("\t")
        if not 2 <= len(fields) <= 4:
            continue
        name, url, options_field, filter_field = (*fields, "", "")[:4]
        options = options_field.split(",") if options_field else []
        gpg_verify = _NO_GPG_VERIFY_OPTION not in options
        items.append(
            FlatpakRemoteItem(
                name=name,
                url=url,
                scope=scope,
                gpg_verify=gpg_verify,
                key_digest=key_digests.get(name) if gpg_verify else None,
                filter_path=filter_field if filter_field and filter_field != _NO_FILTER else None,
                key_paths=(key_paths or {}).get(name, ()) if gpg_verify else (),
            )
        )
    return items


def _parse_flatpak_masks(output: str, scope: Literal["user", "system"]) -> list[FlatpakMaskItem]:
    """Parse one scope's `flatpak {--user|--system} mask` output into `FlatpakMaskItem`s.

    Unlike the tab-separated list commands, `flatpak mask` prints each pattern on its
    own line prefixed with two leading spaces and no header (RESEARCH: verified live,
    Flatpak 1.14.6), so this strips leading/trailing whitespace per non-blank line
    rather than splitting on tabs. `scope` is a parameter, not a parsed column: the
    command has no scope column: the caller already chose the `--user`/`--system` flag
    (same reasoning as `_parse_flatpak_remotes`).
    """
    items: list[FlatpakMaskItem] = []
    for line in output.splitlines():
        pattern = line.strip()
        if not pattern:
            continue
        items.append(FlatpakMaskItem(pattern=pattern, scope=scope))
    return items


def _install_ref_diff(item: FlatpakItem) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.FLATPAK_REF,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.INSTALL,
        item_id=item.item_id,
        label=item.label(),
        detail=None,
    )


def _remove_ref_diff(item: FlatpakItem) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.FLATPAK_REF,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REMOVE,
        item_id=item.item_id,
        label=item.label(),
        detail=None,
    )


def _version_mismatch_ref_diff(
    item_id: str, source_item: FlatpakItem, target_item: FlatpakItem, machines: Machines
) -> ItemDiff:
    """D-04: a flatpak ref's version floats like an apt package's does — reported,
    never force-installed/removed to converge it. Only reachable for two items sharing the
    same `item_id`, i.e. the same ref (application, arch AND branch) in the same scope: a
    scope or branch difference is never this diff (`FlatpakItem` — it is two distinct
    items, an install and a removal).
    """
    return ItemDiff(
        item_class=ItemClass.FLATPAK_REF,
        diff_class=DiffClass.VERSION_MISMATCH,
        action=DiffAction.REPORT_ONLY,
        item_id=item_id,
        label=target_item.label(),
        detail=build_version_mismatch_detail(source_item.version, target_item.version, machines),
    )


def _origin_mismatch_ref_diff(  # noqa: PLR0913 - sibling of _version_mismatch_ref_diff plus both remote URLs
    item_id: str,
    source_item: FlatpakItem,
    target_item: FlatpakItem,
    machines: Machines,
    *,
    source_url: str | None,
    target_url: str | None,
) -> ItemDiff:
    """ADR-020 D-41: a ref present on both machines from different remotes is reported and
    never converged.

    `REPORT_ONLY` is forced by flatpak itself, not chosen for symmetry with `VERSION_MISMATCH`:
    origin is deliberately out of `item_id` (`FlatpakItem`) because the install-plus-removal
    pair it would produce cannot run — `flatpak install <other remote> <installed ref>` exits
    with `already installed from remote <name>` (measured) — so there is no verb to offer.
    The mismatch is therefore a diff CLASS over the single item both machines share, never a
    second item.
    """
    return ItemDiff(
        item_class=ItemClass.FLATPAK_REF,
        diff_class=DiffClass.ORIGIN_MISMATCH,
        action=DiffAction.REPORT_ONLY,
        item_id=item_id,
        label=target_item.label(),
        detail=build_flatpak_origin_mismatch_detail(
            source_item.origin, source_url, target_item.origin, target_url, machines
        ),
    )


def _remote_urls_by_scope_and_name(remotes: Sequence[FlatpakRemoteItem]) -> dict[tuple[str, str], str]:
    """`(scope, remote name) -> url`, the lookup that turns a ref's `origin` column into the
    vendor it actually names (ADR-020 D-41).
    """
    return {(item.scope, item.name): item.url for item in remotes}


def _same_vendor(
    source_item: FlatpakItem,
    target_item: FlatpakItem,
    source_remote_urls: Mapping[tuple[str, str], str],
    target_remote_urls: Mapping[tuple[str, str], str],
) -> bool:
    """Whether the two machines' copies of one ref provably come from the same vendor.

    ADR-020 D-41: origin is compared by the remote's URL, never by its name. Both directions
    of that rule matter here and pull opposite ways, which is why neither comparison alone is
    the answer:

    - Same NAME, different URL is the dangerous case — a target remote called `flathub`
      pointing at the beta repo serves a different vendor's build and `flatpak list
      --columns=origin` prints `flathub` on both machines (measured, `5fc3ac01`). Comparing
      names would report nothing at all.
    - Different NAME, same URL is a pure rename. The origin is identical, so reporting it
      would be noise about a label.

    An origin resolving to no configured remote — the machine holds a ref whose remote it has
    since deleted — has no URL, and an absent URL is a value of its own that matches nothing,
    not even another absent one. Falling back to the name would put the rule the other way
    round: two machines whose refs both name a `flathub` neither of them still configures
    would read as one origin on no evidence at all, and the user would be told there is no
    difference when neither machine can say where its copy came from.
    """
    source_url = source_remote_urls.get((source_item.scope, source_item.origin))
    target_url = target_remote_urls.get((target_item.scope, target_item.origin))
    return source_url is not None and target_url is not None and source_url == target_url


def _diff_flatpak_refs(
    source_items: Sequence[FlatpakItem],
    target_items: Sequence[FlatpakItem],
    source_remote_urls: Mapping[tuple[str, str], str],
    target_remote_urls: Mapping[tuple[str, str], str],
    machines: Machines,
) -> list[ItemDiff]:
    """One diff per ref `item_id` present on either side, source-then-target order —
    same shape as `apt_sync.diffing.diff_apt_packages`/`snap_sync._diff_snap_items`.
    Scope already lives inside `item_id`, so an application installed in a different
    scope on each machine naturally produces one install-side entry and one
    remove-side entry here, never a single combined diff.

    Present on both, the origin comparison runs BEFORE the version comparison, exactly as
    `diff_apt_packages` orders its own two provenance-and-version branches: two vendors'
    builds of one ref share no version scale — Flathub's and Flathub-beta's `org.mozilla.
    firefox` are numbered independently — so reporting "source has X, target has Y" would
    state a difference of degree where the real difference is of provenance, and would hide
    the wrong-vendor finding behind a version line the user reads as ordinary drift.
    """
    source_by_id = {item.item_id: item for item in source_items}
    target_by_id = {item.item_id: item for item in target_items}

    seen: dict[str, None] = {}
    for item in (*source_items, *target_items):
        seen.setdefault(item.item_id, None)

    diffs: list[ItemDiff] = []
    for item_id in seen:
        source_item = source_by_id.get(item_id)
        target_item = target_by_id.get(item_id)

        if source_item is not None and target_item is None:
            diffs.append(_install_ref_diff(source_item))
        elif target_item is not None and source_item is None:
            diffs.append(_remove_ref_diff(target_item))
        elif source_item is not None and target_item is not None:
            if not _same_vendor(source_item, target_item, source_remote_urls, target_remote_urls):
                diffs.append(
                    _origin_mismatch_ref_diff(
                        item_id,
                        source_item,
                        target_item,
                        machines,
                        source_url=source_remote_urls.get((source_item.scope, source_item.origin)),
                        target_url=target_remote_urls.get((target_item.scope, target_item.origin)),
                    )
                )
            elif source_item.version != target_item.version:
                diffs.append(_version_mismatch_ref_diff(item_id, source_item, target_item, machines))
            # else: present on both, one vendor, equal version -> no diff.

    return diffs


@dataclass(frozen=True)
class _DerivedRemote:
    """One remote this run must provision because an approved ref needs it (ADR-020 D-41).

    Not an `ItemDiff` and never in a review group: the user decided about a ref, and the
    remote is the mechanism that delivers it. `reason` is carried so a failure can say why
    the remote was in play at all — a runtime's remote is far less obvious to the reader
    than the app's own.
    """

    remote_id: str
    scope: Literal["user", "system"]
    name: str
    reason: Literal["ref_origin", "runtime_origin"]


@dataclass(frozen=True)
class _RemoteConflict:
    """One remote this run would repoint that a machine-specific target ref takes as its
    origin (ADR-020 D-41) — the only remote CHANGE that is still a question.

    Both configurations are carried, never a rendering of the difference between them, for
    the reason `_remote_conflict_versions` documents. Unlike apt's file-level counterpart
    this costs no extra read: a remote's whole record is already on `FlatpakRemoteItem`,
    captured for the diff.
    """

    scope: Literal["user", "system"]
    name: str
    refs: tuple[str, ...]
    target_version: str
    source_version: str


def _conflict_id(remote_id: str) -> str:
    """The conflict entry id for a remote id — derived from it rather than formatted a
    second time, so the review's question and the write it gates cannot name different
    remotes.
    """
    return f"{_CONFLICT_ID_PREFIX}{remote_id.removeprefix(_REMOTE_ITEM_ID_PREFIX)}"


def _derive_remotes(
    approved_ref_ids: frozenset[str],
    source_refs_by_id: Mapping[str, FlatpakItem],
    source_ref_origins: Mapping[tuple[str, str], str],
    source_runtime_by_ref_id: Mapping[str, str],
) -> tuple[tuple[_DerivedRemote, ...], dict[str, frozenset[str]]]:
    """`(remotes to provision, {approved ref item_id: the remote_ids it depends on})`.

    Two sources feed the set, both computed from facts `plan()` already read off the source
    and neither costing a command here:

    - the approved ref's own origin, and
    - the origin of the runtime that ref is built against, because `flatpak install` pulls
      the runtime too and resolves it from whatever remotes are configured — an app on
      remote X built against a runtime the source holds from remote Y would otherwise be
      approved with only X provisioned.

    The runtime is looked up in EITHER scope on the source (a user app against a
    system-installed runtime is the ordinary case) but its remote is always derived in the
    APP's scope, because that is the installation the target may have to pull the runtime
    into. Deriving one remote too many in the rare cross-scope case costs a
    `flatpak remote-add`; deriving one too few costs the install.

    The attribution map is D-39's: a derived write has no item to fail, so it fails every
    approved ref that named it.
    """
    derived: dict[str, _DerivedRemote] = {}
    depends_on: dict[str, set[str]] = {}

    def need(item_id: str, scope: Literal["user", "system"], name: str, reason: str) -> None:
        remote_id = f"flatpak:remote:{scope}:{name}"
        derived.setdefault(
            remote_id,
            _DerivedRemote(
                remote_id=remote_id,
                scope=scope,
                name=name,
                reason="ref_origin" if reason == "ref_origin" else "runtime_origin",
            ),
        )
        depends_on.setdefault(item_id, set()).add(remote_id)

    for item_id in sorted(approved_ref_ids):
        item = source_refs_by_id.get(item_id)
        if item is None:
            continue
        need(item_id, item.scope, item.origin, "ref_origin")
        runtime = source_runtime_by_ref_id.get(item_id)
        if runtime is None:
            continue
        other_scope = "system" if item.scope == "user" else "user"
        runtime_origin = source_ref_origins.get((item.scope, runtime)) or source_ref_origins.get(
            (other_scope, runtime)
        )
        if runtime_origin is not None:
            need(item_id, item.scope, runtime_origin, "runtime_origin")

    return (
        tuple(derived[remote_id] for remote_id in sorted(derived)),
        {item_id: frozenset(remote_ids) for item_id, remote_ids in depends_on.items()},
    )


def _remote_facets(source_item: FlatpakRemoteItem, target_item: FlatpakRemoteItem) -> tuple[tuple[str, str, str], ...]:
    """`(facet, the source's value, the target's value)` for every facet in which the two
    sides' same-identity remotes differ.

    One definition serving both readers of that question — the log line a silent repoint
    leaves (`_remote_change_detail`) and the two configurations a conflict screen shows
    (`_remote_conflict_versions`) — so the two can never disagree about what "differs" means.
    Only differing facets are returned, so a plain URL edit still reads exactly as it did
    before trust joined the item (#215) and a trust-only divergence never mentions a URL both
    machines agree on.
    """
    facets: list[tuple[str, str, str]] = []
    if source_item.url != target_item.url:
        facets.append((_URL_FACET, source_item.url, target_item.url))
    if source_item.gpg_verify != target_item.gpg_verify:
        facets.append((_VERIFICATION_FACET, _verification_word(source_item), _verification_word(target_item)))
    if source_item.key_digest != target_item.key_digest:
        facets.append(("signing key", source_item.key_digest or "none", target_item.key_digest or "none"))
    return tuple(facets)


def _remote_change_detail(source_item: FlatpakRemoteItem, target_item: FlatpakRemoteItem) -> str:
    """Name every facet in which the two sides' same-identity remotes differ, source first."""
    return f"remote {source_item.name} " + "; ".join(
        f"{facet}: {source_value} vs {target_value}"
        for facet, source_value, target_value in _remote_facets(source_item, target_item)
    )


def _remote_conflict_versions(source_item: FlatpakRemoteItem, target_item: FlatpakRemoteItem) -> tuple[str, str]:
    """`(the target's configuration, the source's)` for a conflict screen, one differing
    facet per line.

    Two versions, never a computed diff, and never the whole record: apt shows two file
    bodies because a repository file IS a body, while a remote is a handful of named values,
    so the readable answer to "which of these two configurations should this machine have" is
    the fields that actually disagree, printed twice. The user's own verdict on apt's version
    — that a diff of two repository definitions is not readable — is what rules out rendering
    the difference instead of the two sides.
    """
    facets = _remote_facets(source_item, target_item)
    return (
        "\n".join(f"{facet}: {target_value}" for facet, _source_value, target_value in facets),
        "\n".join(f"{facet}: {source_value}" for facet, source_value, _target_value in facets),
    )


def _verification_word(item: FlatpakRemoteItem) -> str:
    return "enabled" if item.gpg_verify else "disabled"


def build_remote_conflict_detail(name: str, scope: str, refs: Sequence[str], machines: Machines) -> str:
    """Detail for a remote-conflict entry: why THIS differing remote is being put to the user
    when every other one is repointed silently (ADR-020 D-41).

    The named refs are the whole reason. They are recorded skip-always, so `filter_inert`
    keeps them out of the target manifest and they produce no diff of their own in any run —
    without this line the user sees a remote they are asked to overwrite and no indication
    that doing so changes where software they told this tool to leave alone comes from.
    """
    one = len(refs) == 1
    return (
        f"the {scope}-scope remote {name} is different on the two machines, and {machines.target} installs "
        f"{', '.join(refs)} from it — {'app' if one else 'apps'} you marked as specific to {machines.target}, "
        f"so a sync normally leaves {'it' if one else 'them'} alone"
    )


def _remote_trust_flags(item: FlatpakRemoteItem, staged_keys: Sequence[str], *, restore_verification: bool) -> str:
    """The `flatpak remote-add`/`remote-modify` flags that replicate `item`'s trust
    (#215), as a string that begins with a space or is empty.

    `--no-gpg-verify` is emitted if and only if the SOURCE remote is itself unverified:
    a remote the source verifies can never be silently downgraded on the target, and an
    unverified one is replicated as unverified rather than as a verified remote that
    would then refuse every install. `restore_verification` adds the explicit
    `--gpg-verify` that only `remote-modify` accepts (`remote-add` has no such flag —
    verification is its default), so a CHANGE can lift a target-side remote back out of
    `no-gpg-verify` instead of leaving the divergence half-converged.

    `--gpg-import` is repeatable, so an anchor-trusted remote can carry several files
    (`_anchors_to_import`) through the same flag a per-remote keyring uses. An empty
    `staged_keys` means the target already trusts what the source's remote trusts and
    nothing needs importing.
    """
    if not item.gpg_verify:
        return " --no-gpg-verify"
    flags = " --gpg-verify" if restore_verification else ""
    return flags + "".join(f" --gpg-import={shlex.quote(key)}" for key in staged_keys)


def _trust_mutation_phrase(item: FlatpakRemoteItem, source_hostname: str, staged_keys: Sequence[str]) -> str:
    """Trailing clause for the `mutates=` phrase, so the confirm-each-command prompt and
    the trace state what a remote command does to TRUST, not only to the URL.
    """
    if not item.gpg_verify:
        return f", with gpg verification disabled (as on {source_hostname})"
    if not staged_keys:
        return ""
    if item.key_digest is None and not item.key_paths:
        return f", importing the machine-level signing key {source_hostname} verifies it against"
    return f", importing {source_hostname}'s signing key"


def _target_refs_by_origin_remote(target_refs: Sequence[FlatpakItem]) -> dict[str, list[str]]:
    """Target refs keyed by the `item_id` of the remote they name as origin, IN THEIR OWN
    SCOPE (#214).

    A remote is per-installation (module docstring), so `flathub` in `user` and
    `flathub` in `system` are two entries and only same-scope refs depend on either —
    keying by the full remote item_id rather than the bare name is what keeps a
    user-scope ref out of the system-scope remote's dependent list.

    Both readers of "what uses this remote" run on it: ruling 6's conflict trigger, over the
    machine-specific refs alone, and `_delete_unused_remotes`, over every ref the target
    holds.
    """
    by_remote: dict[str, list[str]] = {}
    for ref in target_refs:
        by_remote.setdefault(f"flatpak:remote:{ref.scope}:{ref.origin}", []).append(ref.ref)
    return by_remote


def _install_mask_diff(item: FlatpakMaskItem) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.FLATPAK_MASK,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.INSTALL,
        item_id=item.item_id,
        label=item.label(),
        detail=None,
    )


def _remove_mask_diff(item: FlatpakMaskItem) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.FLATPAK_MASK,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REMOVE,
        item_id=item.item_id,
        label=item.label(),
        detail=None,
    )


def _diff_flatpak_masks(
    source_items: Sequence[FlatpakMaskItem], target_items: Sequence[FlatpakMaskItem]
) -> list[ItemDiff]:
    """One diff per mask `item_id` (scope + pattern) present on either side (#208, D-10).

    Pure membership, no `CHANGE`: a mask has no value to change, only presence — so
    source-has & target-lacks -> `INSTALL` (add the mask on target); target-has &
    source-lacks -> `REMOVE` (unmask on target); present on both -> no diff. A pattern
    edit therefore reads as remove-old + add-new and a user/system scope split as
    add + remove (scope is identity, same as refs/remotes), reported as found rather
    than normalised.
    """
    source_by_id = {item.item_id: item for item in source_items}
    target_by_id = {item.item_id: item for item in target_items}

    seen: dict[str, None] = {}
    for item in (*source_items, *target_items):
        seen.setdefault(item.item_id, None)

    diffs: list[ItemDiff] = []
    for item_id in seen:
        source_item = source_by_id.get(item_id)
        target_item = target_by_id.get(item_id)

        if source_item is not None and target_item is None:
            diffs.append(_install_mask_diff(source_item))
        elif target_item is not None and source_item is None:
            diffs.append(_remove_mask_diff(target_item))
        # else: present on both -> no diff (pure membership, no value to change).

    return diffs


def flatpak_sync_exclude_paths() -> list[Path]:
    """The single absolute path this job owns (D-29), resolved against `Path.home()`
    at call time exactly like `vscode_state_exclude_paths()`/`snap_sync_exclude_paths()`.

    Returns `~/.local/share/flatpak` ONLY — never `~/.var/app`, which is
    per-application user data that stays folder_sync's territory (module docstring).
    D-17's job-before-folder_sync ordering is what lets `flatpak install` create this
    store before folder_sync's own data lands on top of it.
    """
    return [Path.home() / _FLATPAK_DATA_RELPATH]


class FlatpakSyncJob(PackageSyncJob):
    """Converge flatpak refs and remotes, per scope, after the coordinator's batched
    review.

    Overrides `plan()` with a flatpak-specific capture -> diff -> review-group
    pipeline (module docstring explains why the inherited apt-package-shaped one
    cannot express two ordered item classes); `accept_review()`, `apply()` and
    `execute()` are inherited unchanged.
    """

    name: ClassVar[str] = "flatpak_sync"
    manager_id: ClassVar[str] = "flatpak"

    # No configurable properties: mirrors AptSyncJob/SnapSyncJob's empty schema — only
    # the enable flag in sync_jobs is needed for this slice.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        # Populated by plan()'s own capture/query step (post filter_inert) and
        # consulted by converge(): the base pipeline only ever hands converge() an
        # ItemDiff, whose item_id carries scope + name but not the source's origin
        # remote or a remote's URL — those have to come from somewhere else.
        self._source_refs_by_id: dict[str, FlatpakItem] = {}
        # The source's remotes as captured, with NO `filter_inert` pass: a source remote is
        # no longer reviewable in any direction (only the target's own removals are), and a
        # decision file must not be able to withhold the remote an approved ref needs or the
        # URL its origin is checked against.
        self._source_remotes_by_id: dict[str, FlatpakRemoteItem] = {}
        self._target_remotes_by_id: dict[str, FlatpakRemoteItem] = {}
        # `(scope, ref) -> origin` over EVERY installed source ref, runtimes included, and
        # `ref item_id -> the runtime ref it needs`: the two inputs the runtime half of
        # `_derive_remotes` consumes. Read in plan(), because derivation runs in the
        # synchronous `accept_review()` and cannot issue commands of its own.
        self._source_ref_origins: dict[tuple[str, str], str] = {}
        self._source_runtime_by_ref_id: dict[str, str] = {}
        # The machine-level trust `_OSTREE_TRUSTED_ANCHOR_DIR` holds on each machine: the
        # source's as `{path: digest}`, because the paths are what gets staged, and the
        # target's as digests alone, because only "does the target already trust this" is
        # asked of it. Machine-level, so read once per run rather than per remote or scope.
        self._source_trust_anchors: dict[str, str] = {}
        self._target_trust_anchor_digests: frozenset[str] = frozenset()
        # Set by `accept_review()` from the approved ref installs, consumed by `apply()`:
        # the remotes to provision, which approved ref depended on each (D-39), and the
        # writes that failed, keyed by remote_id.
        self._derived_remotes: tuple[_DerivedRemote, ...] = ()
        self._ref_derived_remote_ids: dict[str, frozenset[str]] = {}
        self._failed_derived_remotes: dict[str, str] = {}
        # `{remote_id: why}` for the filters `_converge_remote_filters` could not put right
        # before the installs. A filter has no item of its own either, so the reason is charged
        # to every approved ref whose own origin is that remote (`_remote_filter_failure`).
        self._failed_remote_filters: dict[str, str] = {}
        # `{remote_id: _RemoteConflict}` for the repoints that would move a machine-specific
        # target ref's origin (ruling 6). Populated in `plan()`, consumed by the conflict
        # review group and then by `accept_review()`'s write set.
        self._remote_conflicts: dict[str, _RemoteConflict] = {}
        # The target's remotes as they ACTUALLY are once this run's remote writes have run:
        # re-read lazily at the first ref install, discarded whenever a remote write lands.
        # Neither the plan-time query nor "this run added it" is admissible evidence —
        # `flatpak remote-add --if-not-exists <name> <different url>` exits 0 and leaves the
        # old URL in place (measured), so a run that trusted its own exit code would install
        # from whatever URL the target's same-named remote already had.
        self._target_remotes_now_by_id: dict[str, FlatpakRemoteItem] | None = None
        # Resolved once by `_target_home_dir()`: where a remote's signing key is staged
        # before `flatpak remote-add --gpg-import` reads it (#215).
        self._target_home: str | None = None

    async def capture_source_items(self) -> Sequence[FlatpakItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """`flatpak list --app` on the source (D-06).

        This job overrides `plan()` and never routes through `PackageSyncJob.
        diff_items`'s apt-package-shaped dispatch (module docstring), so widening this
        hook's item type here is safe: no code holding a `PackageSyncJob`-typed
        reference ever calls it expecting an `AptPackageItem` back — the same
        justification `SnapSyncJob.capture_source_items` documents.

        Guarded on the exit code (ADR-022), like every flatpak read in this job. Measured
        in a container with flatpak installed: an unreadable or unparsable installation
        makes `flatpak list`, `remotes` and `mask` all exit 1 with `error:` on stderr, and
        all three exit 0 printing nothing when the machine simply has none of what was
        asked for. So the exit code is the whole discriminator, and empty output at exit 0
        is a machine with no apps — an ordinary machine, and never a failure.
        """
        result = await self.source.run_command(_FLATPAK_LIST_CMD)
        require_answer(_FLATPAK_LIST_CMD, result, self.machines.source)
        return _parse_flatpak_list(result.stdout)

    async def query_target_items(self) -> Sequence[FlatpakItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The target's own `flatpak list --app` (same reasoning as `capture_source_items`)."""
        result = await self.target.run_command(_FLATPAK_LIST_CMD, login_shell=False)
        require_answer(_FLATPAK_LIST_CMD, result, self.machines.target)
        return _parse_flatpak_list(result.stdout)

    async def _capture_source_remotes(self, scope: Literal["user", "system"]) -> list[FlatpakRemoteItem]:
        """One scope's remotes with the trust each carries (#215): three reads — one listing,
        one batched `sha256sum` over the per-remote keyrings, and the installation's ostree
        `repo/config` for the `gpgkeypath` option — never a command per remote.

        Only the listing is guarded. The other two are the documented counter-examples to a
        blanket exit-code rule (ADR-022): the digest glob legitimately matches nothing on a
        scope with no remote keyring and the config file legitimately does not exist on a
        scope with no flatpak installation, so `sha256sum` and `cat` exit non-zero on an
        ordinary machine and guarding them would fail every run on one.
        """
        keys = await self.source.run_command(_keyring_digests_cmd(scope))
        config = await self.source.run_command(_repo_config_cmd(scope))
        command = _FLATPAK_REMOTES_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.source.run_command(command)
        require_answer(command, result, self.machines.source)
        return _parse_flatpak_remotes(
            result.stdout,
            scope,
            _parse_keyring_digests(keys.stdout),
            _parse_repo_config_key_paths(config.stdout),
        )

    async def _query_target_remotes(self, scope: Literal["user", "system"]) -> list[FlatpakRemoteItem]:
        """The target's own remotes. No `gpgkeypath` read: what the source holds that way is
        imported into the target's per-remote keyring, so the option is a source-side fact
        about which bytes to carry rather than one of the item's comparable facets
        (`FlatpakRemoteItem.key_paths`).
        """
        keys = await self.target.run_command(_keyring_digests_cmd(scope), login_shell=False)
        command = _FLATPAK_REMOTES_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.target.run_command(command, login_shell=False)
        require_answer(command, result, self.machines.target)
        return _parse_flatpak_remotes(result.stdout, scope, _parse_keyring_digests(keys.stdout))

    async def _capture_all_source_remotes(self) -> list[FlatpakRemoteItem]:
        """Both scopes, one call each (D-14): flatpak tracks remotes per-installation
        even when the URL is identical, so `flathub` in both scopes needs two reads.
        """
        remotes: list[FlatpakRemoteItem] = []
        for scope in _SCOPES:
            remotes.extend(await self._capture_source_remotes(scope))
        return remotes

    async def _query_all_target_remotes(self) -> list[FlatpakRemoteItem]:
        remotes: list[FlatpakRemoteItem] = []
        for scope in _SCOPES:
            remotes.extend(await self._query_target_remotes(scope))
        return remotes

    async def _capture_source_masks(self, scope: Literal["user", "system"]) -> list[FlatpakMaskItem]:
        cmd = _FLATPAK_MASK_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.source.run_command(cmd)
        require_answer(cmd, result, self.machines.source)
        return _parse_flatpak_masks(result.stdout, scope)

    async def _query_target_masks(self, scope: Literal["user", "system"]) -> list[FlatpakMaskItem]:
        cmd = _FLATPAK_MASK_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.target.run_command(cmd, login_shell=False)
        require_answer(cmd, result, self.machines.target)
        return _parse_flatpak_masks(result.stdout, scope)

    async def _capture_all_source_masks(self) -> list[FlatpakMaskItem]:
        """Both scopes, one call each (D-10): masks are per-installation like remotes,
        so a pattern masked in both scopes is two independent reads.
        """
        masks: list[FlatpakMaskItem] = []
        for scope in _SCOPES:
            masks.extend(await self._capture_source_masks(scope))
        return masks

    async def _query_all_target_masks(self) -> list[FlatpakMaskItem]:
        masks: list[FlatpakMaskItem] = []
        for scope in _SCOPES:
            masks.extend(await self._query_target_masks(scope))
        return masks

    async def _capture_source_ref_origins(self) -> dict[tuple[str, str], str]:
        """`(scope, ref) -> origin` over EVERY installed source ref, runtimes included.

        The `--app` listing cannot serve this: a runtime is exactly what it filters out, and
        the runtime's own origin is the second input to remote derivation. Guarded on the
        exit code for the same measured reason `capture_source_items` is.
        """
        result = await self.source.run_command(_FLATPAK_ALL_REFS_CMD)
        require_answer(_FLATPAK_ALL_REFS_CMD, result, self.machines.source)
        return {(item.scope, item.ref): item.origin for item in _parse_flatpak_list(result.stdout)}

    async def _capture_source_runtimes(self, source_refs: Sequence[FlatpakItem]) -> dict[str, str]:
        """`ref item_id -> the runtime ref it is built against`, one local read per app.

        Batched is not available: `flatpak info` answers about one ref. Measured at 10 ms
        with no network, so the cost is proportional to the source's app count and nothing
        else. Guarded on the exit code (ADR-022): the question is only ever asked about a
        ref the source's own listing just reported, so a non-zero exit means the tool did
        not answer, never "no such ref". An app with no runtime at all is not a state
        flatpak has — but an empty answer is still read as "nothing to derive from" rather
        than invented.
        """
        runtimes: dict[str, str] = {}
        for item in source_refs:
            cmd = _FLATPAK_RUNTIME_CMD_TEMPLATE.format(flag=_scope_flag(item.scope), ref=shlex.quote(item.ref))
            result = await self.source.run_command(cmd)
            require_answer(cmd, result, self.machines.source)
            runtime = result.stdout.strip()
            if runtime:
                runtimes[item.item_id] = runtime
        return runtimes

    async def _capture_trust_anchors(self) -> None:
        """Both machines' machine-level trust anchors, one batched read each
        (`_OSTREE_TRUSTED_ANCHOR_DIR`).

        Once per run, not per scope or per remote: the directory is outside every flatpak
        installation and every remote in either scope verifies against the same files.
        Unguarded on the exit code, like the per-remote keyring read and for the same measured
        reason (`_capture_source_remotes`).
        """
        command = _trust_anchor_digests_cmd()
        source = await self.source.run_command(command)
        target = await self.target.run_command(command, login_shell=False)
        self._source_trust_anchors = _anchor_digests(source.stdout)
        self._target_trust_anchor_digests = frozenset(_anchor_digests(target.stdout).values())

    def _anchors_to_import(self, item: FlatpakRemoteItem) -> list[Path]:
        """The source's machine-level anchor files a verified remote with no keyring of its
        own needs imported into its keyring on the target (`PKG-FR-FLATPAK-REMOTE-TRUST`).

        A remote whose key sits in `_OSTREE_TRUSTED_ANCHOR_DIR` rather than in
        `<remote>.trustedkeys.gpg` is verified on the source and unusable on a target that
        lacks the same files: flatpak refuses every install from it with `Can't check
        signature: public key not found`. Replicating the anchor machine-wide would grant that
        trust to every remote the target has; importing it into this remote's own keyring
        grants exactly what the source's remote had, and nothing else on the target changes.

        EVERY such anchor the target lacks travels, not some subset chosen as "the one this
        remote used". That is not a shortcut: libostree merges every keyring in that directory
        into a single verifier and accepts a signature any key in it validates
        (`_ANCHOR_FILES_LIBOSTREE_IGNORES`), and neither flatpak nor ostree records which key
        verified anything — so the trust a keyless verified remote rests on IS the merged set,
        and reproducing it is reproducing exactly what the source's remote had. The only
        narrowing that is decidable at all is libostree's own file filter, and
        `_capture_trust_anchors` applies it.

        An anchor the target already holds is left out — its trust is already there, and
        importing it would issue a write per run for no change.
        """
        if not item.gpg_verify or item.key_digest is not None:
            return []
        return [
            Path(path)
            for path, digest in sorted(self._source_trust_anchors.items())
            if digest not in self._target_trust_anchor_digests
        ]

    @override
    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """The marked refs one machine no longer has, read off that machine's own
        `flatpak list`.

        The listing is `_FLATPAK_ALL_REFS_CMD` rather than the `--app` one the diff is built
        from. Only an app can be marked, so the two answer this question identically today;
        the wider listing is used anyway because it is a statement about what the machine
        HAS, and narrowing a presence check to a subset is how a mark on something still
        installed gets dropped.

        Neither a remote nor a mask answers anything here. Neither can be recorded at all
        (`PKG-FR-BLOCKS-DERIVED`, and a remote is never offered), so an entry naming one is
        a hand edit and stays where it is — recognised by its ID rather than its recorded
        `item_class`, since a hand-edited file can have the two disagree.
        """
        ref_ids = {item_id for item_id in entries if item_id.startswith(_REF_ITEM_ID_PREFIX)}
        if not ref_ids:
            return frozenset()

        result = (
            await self.source.run_command(_FLATPAK_ALL_REFS_CMD)
            if on_source
            else await self.target.run_command(_FLATPAK_ALL_REFS_CMD, login_shell=False)
        )
        require_answer(_FLATPAK_ALL_REFS_CMD, result, self.machines.source if on_source else self.machines.target)
        installed = {item.item_id for item in _parse_flatpak_list(result.stdout)}
        return frozenset(ref_ids - installed)

    @override
    async def plan(self) -> PackagePlan:
        """Load decision files -> capture -> query -> diff -> build review groups.

        Read-only: only `flatpak list`/`flatpak remotes`/`flatpak mask`/`flatpak info`/`flatpak
        remote-ls` (both machines, both scopes) and a decision-file `cat` run here — no `flatpak
        install`/`uninstall`/`remote-add`/`remote-delete`/`mask` mutation before this returns.
        Caches the source/target refs and remotes by id for `converge()` (see `__init__`);
        masks need no cache (pattern is fully in the item_id).

        The two derivation inputs are read here for the same reason apt reads its origin
        state before the package diff: derivation runs in the synchronous
        `accept_review()`, which cannot issue a command, so every fact it consumes has to
        be in hand by then. That costs one extra source listing plus one
        `flatpak info --show-runtime` per source app (10 ms each, no network — measured).

        Diffs are ordered refs -> masks in the returned `diffs` tuple: a mask applied before
        its refs could suppress an auto-pulled dependency of a ref being installed the same
        run (D-08). Remotes are not in this ordering at all: `apply()` writes the derived
        ones ahead of the whole loop and deletes the unused ones after it.

        Ruling 6's conflicts are computed here too and cost no command at all: every fact
        they need — both machines' remote records, the target's own ref listing and its
        decision file — was already read for the diff.
        """
        source_decisions, target_decisions = await self._load_live_decisions()

        # Both files against BOTH listings (`marks_on_either`): a ref or a mask the two
        # machines share must vanish from the diff entirely once either machine records it,
        # and filtering each listing by its own file alone leaves the other machine's copy
        # unmatched — which is an install of an application the target already has, or a
        # removal of one the source still has.
        marked = marks_on_either(source_decisions, target_decisions)
        source_refs = await filter_inert(await self.capture_source_items(), marked)
        installed_target_refs = await self.query_target_items()
        target_refs = await filter_inert(installed_target_refs, marked)
        source_remotes = await self._capture_all_source_remotes()
        # No `filter_inert` pass on either side's remotes: a remote is never a review item in
        # any direction, so a decision file has nothing to withhold — and withholding one
        # would hide the URL an origin comparison runs on or keep a dead remote configured
        # for good.
        installed_target_remotes = await self._query_all_target_remotes()
        # No `filter_inert` pass on either side's masks either: a mask is derived and no
        # answer about one can be recorded (`PKG-FR-BLOCKS-DERIVED`), so an entry naming one
        # — left by an older version of the tool, or written by hand — must not silence a
        # replication the user never declined. A mark on an APPLICATION still reaches the
        # masks that cover it, through the ref filtering above: the mask lands on a machine
        # whose copy of that application the mark protects, which is what the mark is about.
        source_masks = await self._capture_all_source_masks()
        target_masks = await self._query_all_target_masks()

        self._source_refs_by_id = {item.item_id: item for item in source_refs}
        self._source_remotes_by_id = {item.item_id: item for item in source_remotes}
        self._target_remotes_by_id = {item.item_id: item for item in installed_target_remotes}
        self._target_remotes_now_by_id = None
        self._source_ref_origins = await self._capture_source_ref_origins()
        self._source_runtime_by_ref_id = await self._capture_source_runtimes(source_refs)
        await self._capture_trust_anchors()
        await self._abort_on_a_source_filter_that_denies_its_own_apps(source_refs, source_remotes)

        ref_diffs = _diff_flatpak_refs(
            source_refs,
            target_refs,
            _remote_urls_by_scope_and_name(source_remotes),
            _remote_urls_by_scope_and_name(installed_target_remotes),
            self.machines,
        )
        mask_diffs = _diff_flatpak_masks(source_masks, target_masks)
        # Ordering (D-08): refs -> masks. A mask must land AFTER the refs so it can never
        # suppress an auto-pulled dependency of a ref being installed the same run;
        # converge() carries the pattern fully in the item_id, so masks (unlike refs) need no
        # source-side cache.
        # Every reviewable flatpak item class carries its own id into `filter_inert` above,
        # so this pass is a no-op backstop here — kept so all four `plan()`s end the same way
        # and the read path can never drift from `_record_permanent_skips`'s write path.
        diffs = self._drop_inert_diffs((*ref_diffs, *mask_diffs), source_decisions, target_decisions)

        self._capture_remote_conflicts(diffs, installed_target_refs, target_decisions)
        groups = self._build_review_groups(diffs)
        return PackagePlan(manager=self.manager_id, diffs=diffs, groups=groups)

    async def _refs_the_remote_offers(self, scope: str, remote: str) -> frozenset[str] | None:
        """What one of the source's remotes offers under its own filter, or `None` when flatpak
        did not answer (`_FLATPAK_REMOTE_LS_CMD_TEMPLATE`).

        Deliberately unguarded, unlike every other capture here: the one caller uses this to
        decide whether to END THE RUN, and every way flatpak has of not answering — an
        unreachable remote, a filter file it refuses to parse — is a reason to say nothing
        rather than to fail a run over a question about a filter. `None` is that silence, and
        it stays distinct from an empty listing, which is a remote answering that it offers
        nothing.

        A listing is a read: it needs no elevation even for a `--system` remote and caches
        under the invoking user's own `~/.cache/flatpak` (measured), so it carries no
        `mutates=` phrase.
        """
        command = _FLATPAK_REMOTE_LS_CMD_TEMPLATE.format(flag=_scope_flag(scope), remote=shlex.quote(remote))
        result = await self.source.run_command(command)
        if result.exit_code != 0:
            return None
        return frozenset(line.strip() for line in _lines(result.stdout))

    async def _abort_on_a_source_filter_that_denies_its_own_apps(
        self, source_refs: Sequence[FlatpakItem], source_remotes: Sequence[FlatpakRemoteItem]
    ) -> None:
        """End the run when a filtered source remote does not offer an application the source
        installed from it (`PKG-FR-FLATPAK-FILTER`).

        The filter is in force before the installs, so a filter narrower than the set being
        replicated would block them — but that is the source contradicting itself, not a case
        to carry logic for: the same machine says "install this from there" and "nothing from
        there may be installed". Ending the run here, before this job has changed anything and
        before the user is asked to decide anything, is what puts the repair in front of them
        (`SyncAborted`, the same end an unparsable snippet registry gets).

        What a remote offers under its filter is flatpak's own business, so flatpak is asked
        rather than imitated: one listing per filtered remote, whatever the source's apps from
        it, and the filter's semantics stay where they are implemented (module docstring).

        The listing is all the evidence there is, and it names no cause: a ref the remote has
        delisted and a ref `remote-ls` will not list for its architecture are absent from it
        exactly as a denied one is, and flatpak offers no unfiltered view of the same remote to
        separate them (module docstring). So the run ends on what was measured — this remote,
        under this filter, does not offer this app — with both repairs named and neither
        blamed. Nothing is measured at all when flatpak declines to answer, and nothing is
        claimed then either.

        Every filtered remote in both scopes is asked before anything is raised, and the one
        abort lists every application each of them denies: aborting on the first would have
        the user correct one filter, sync again, and only then be told about the next. The
        listing is grouped by remote because the repair is per remote — one filter file to
        correct, whatever number of its own applications it withholds.
        """
        filtered = {item.item_id: item for item in source_remotes if item.filter_path is not None}
        offered_by: dict[str, frozenset[str] | None] = {}
        denied: dict[tuple[str, str, str], list[str]] = {}
        for ref in source_refs:
            remote = filtered.get(f"{_REMOTE_ITEM_ID_PREFIX}{ref.scope}:{ref.origin}")
            if remote is None or remote.filter_path is None:
                continue
            if remote.item_id not in offered_by:
                offered_by[remote.item_id] = await self._refs_the_remote_offers(remote.scope, remote.name)
            offered = offered_by[remote.item_id]
            if offered is None or f"{_APP_REF_PREFIX}{ref.ref}" in offered:
                continue
            denied.setdefault((ref.scope, ref.origin, remote.filter_path), []).append(ref.ref)
        if not denied:
            return
        listing = "\n".join(
            f"  {scope}-scope remote {origin}, ref filter {filter_path}: {', '.join(sorted(refs))}"
            for (scope, origin, filter_path), refs in sorted(denied.items())
        )
        raise SyncAborted(
            f"{self.machines.source} has flatpaks installed from remotes that do not offer them under the ref "
            f"filter {self.machines.source} applies to those remotes:\n"
            f"{listing}\n"
            f"Those filters would be applied to {self.machines.target} before anything installs from those "
            f"remotes, so replicating each remote and its own applications together is impossible; correct the "
            f"filter — or, where the remote no longer carries the application at all, uninstall it from "
            f"{self.machines.source} — before syncing again"
        )

    def _capture_remote_conflicts(
        self,
        diffs: Sequence[ItemDiff],
        installed_target_refs: Sequence[FlatpakItem],
        target_decisions: Mapping[str, DecisionEntry],
    ) -> None:
        """Find the repoints ADR-020 D-41 turns into a question instead of a silent
        write: a remote this run may repoint, whose URL or verification setting differs, and
        which a MACHINE-SPECIFIC target ref takes as its origin in that same scope.

        Machine-specific means recorded skip-always in the TARGET's decision file, exactly as
        `AptProbe.packages_by_source_file` reads it — not "a ref the
        target has and the source does not". The narrower set is the point: a skip-always ref
        is structurally invisible (`filter_inert` drops it before the diff, so it can never
        produce an `ItemDiff` of its own in any run) and the user's explicit "this machine
        keeps this, syncs never touch it" is exactly the promise a silent repoint breaks. An
        ordinary target ref is at least eligible for its own diff, and keying off every ref
        from the remote would make the question a property of the machine — one Flathub
        repoint would name every app on it and inform nobody.

        The candidate set is what `_derive_remotes` would provision if the review approved
        every proposed install — a superset of what `accept_review()` actually derives, since
        the review has not happened yet. Gating on it is what keeps this job's rule intact:
        a remote travels because a ref needs it, so a remote nothing will touch this run is
        not a question, and answering "overwrite" cannot by itself make one travel. That is
        the one place this deliberately diverges from apt, whose conflict screen covers every
        differing file and whose approval does force the write.

        A remote the target lacks entirely is an ADD, not a repoint: nothing of the target's
        is being replaced, so there is nothing to ask about.
        """
        machine_specific = [ref for ref in installed_target_refs if ref.item_id in target_decisions]
        self._remote_conflicts = {}
        if not machine_specific:
            return

        dependents = _target_refs_by_origin_remote(machine_specific)
        candidates, _attribution = _derive_remotes(
            frozenset(
                diff.item_id
                for diff in diffs
                if diff.item_class is ItemClass.FLATPAK_REF and diff.action is DiffAction.INSTALL
            ),
            self._source_refs_by_id,
            self._source_ref_origins,
            self._source_runtime_by_ref_id,
        )
        for derived in candidates:
            refs = dependents.get(derived.remote_id)
            source_item = self._source_remotes_by_id.get(derived.remote_id)
            # The target's map, not a fresh read, so the trigger and `_write_derived_remote`'s
            # own add-or-repoint test read the same fact.
            target_item = self._target_remotes_by_id.get(derived.remote_id)
            if not refs or source_item is None or target_item is None:
                continue
            facets = _remote_facets(source_item, target_item)
            if not any(facet in _PROVENANCE_FACETS for facet, _source, _target in facets):
                continue
            target_version, source_version = _remote_conflict_versions(source_item, target_item)
            self._remote_conflicts[derived.remote_id] = _RemoteConflict(
                scope=derived.scope,
                name=derived.name,
                refs=tuple(sorted(refs)),
                target_version=target_version,
                source_version=source_version,
            )

    @override
    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """The base groups, plus D-41's conflict screen when `_capture_remote_conflicts`
        found one.

        That screen is the two-answer one (`REPO_CONFLICT_REVIEW_ACTION`), preceded by both
        versions of every remote it lists, and records nothing either way. It is the only
        question this job asks about a remote, and it trails the base groups so the user sees
        the refs first — the refs are what the answer is really about.
        """
        if not self._remote_conflicts:
            return super()._build_review_groups(diffs)
        return (
            *super()._build_review_groups(diffs),
            ReviewGroup(
                manager=self.manager_id,
                action=REPO_CONFLICT_REVIEW_ACTION,
                title=f"Resolve {self.manager_id} remote conflicts",
                entries=tuple(
                    ReviewEntry(
                        item_id=_conflict_id(remote_id),
                        label=f"{conflict.name} remote ({conflict.scope})",
                        action_label="overwrite",
                        detail=build_remote_conflict_detail(
                            conflict.name, conflict.scope, conflict.refs, self.machines
                        ),
                        versions=(conflict.target_version, conflict.source_version),
                    )
                    for remote_id, conflict in sorted(self._remote_conflicts.items())
                ),
            ),
        )

    @override
    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Turn the approved ref installs into the remotes this run provisions, before the
        base stores the accepted pair.

        Here rather than in `plan()` for the same reason `apt_sync.derived.DerivedWrites`
        is built where it is: the input is the set of APPROVED items, which does not exist
        until the review returns. Every fact it reads was captured in `plan()`, so this stays synchronous.

        A conflict the user declined (ruling 6) leaves the derived set but stays in the D-39
        attribution map, and that asymmetry IS the rule: the remote is not repointed, and
        every approved ref that named it fails saying so instead of being installed from the
        URL the target still has. `_origin_refusal` would refuse those installs anyway — it
        re-reads the target and compares URL and verification, which is exactly what a
        declined conflict leaves diverging — so this seeding is not what makes the outcome
        safe; it is what makes the failure name the user's own decision (checked first in
        `_converge_ref`) rather than report the symptom.
        """
        derived, self._ref_derived_remote_ids = _derive_remotes(
            frozenset(
                diff.item_id
                for diff in plan.diffs
                if diff.item_class is ItemClass.FLATPAK_REF
                and diff.action is DiffAction.INSTALL
                and outcome.decisions.get(diff.item_id) == Decision.APPLY
            ),
            self._source_refs_by_id,
            self._source_ref_origins,
            self._source_runtime_by_ref_id,
        )
        skipped = {
            remote_id: f"the user chose to keep {self.machines.target}'s own version of it for now (ADR-020 D-41)"
            for remote_id in self._remote_conflicts
            if outcome.decisions.get(_conflict_id(remote_id)) != Decision.APPLY
        }
        self._derived_remotes = tuple(item for item in derived if item.remote_id not in skipped)
        self._failed_derived_remotes = dict(skipped)
        super().accept_review(plan, outcome)

    @override
    async def _record_permanent_skips(self, plan: PackagePlan, decisions: Mapping[str, Decision]) -> None:
        """The base recording pass, minus every `flatpak:remote:` id (ADR-020 D-41).

        The interactive flow already cannot produce a `SKIP_ALWAYS` for one — the removal
        group is absent from `_PROMOTABLE_ACTIONS`, so the promotion screen never offers it
        — but "no registry entry" is a property of the model, not of one prompt's wiring,
        and a decision can also arrive from the review's automation hook or from a caller
        assembling a `ReviewOutcome` by hand. Filtered by id prefix so it holds in EVERY
        direction, including the two this job no longer emits.

        `flatpak:mask:` is deliberately not filtered: a mask is a standing preference about
        updating, like an apt hold, and nothing about an approved ref implies whether it
        should travel — so it keeps the full three-way decision and the registry.
        """
        recordable = PackagePlan(
            manager=plan.manager,
            diffs=tuple(diff for diff in plan.diffs if not diff.item_id.startswith(_REMOTE_ITEM_ID_PREFIX)),
            groups=plan.groups,
        )
        await super()._record_permanent_skips(recordable, decisions)

    @override
    async def apply(self) -> None:
        """Provision the derived remotes, bring their filters to the source's, run the base
        converge loop, then delete the remotes nothing uses.

        The ordering `plan()` used to carry in its `diffs` tuple lives here now, and has to:
        no remote is a diff in any direction, so nothing in the base loop would reach one.
        Remote, then filter, then install (`PKG-FR-FLATPAK-FILTER`) — the first two are D-14's
        guarantee that everything an approved ref depends on is in place before the loop issues
        its first `flatpak install`, and the filter is one of those things rather than an
        afterthought put back once the installs are safely past. `_delete_unused_remotes` is
        the one pass that genuinely needs the loop to have finished: "nothing uses it" is only
        a measurement once this run's approved removals have actually run.

        The base raises `PackageItemFailures` at the end of its own loop; it is caught only so
        the deletion pass still runs after a failing loop, and re-raised unchanged.

        Dry-run (ADR-014): each intended write is logged at FULL with the same `[dry-run] `
        prefix the base loop uses, and no command is issued. Without this a preview of a first
        sync would show the installs and say nothing about the remotes they depend on.

        The trust warning precedes the dry-run branch: it qualifies what provisioning this
        remote achieves, so a preview that hid it would overstate the real run.

        Everything before `super().apply()` is reported as progress in its own right (#235):
        the base loop's first report arrives with its first item, which on this job is a
        remote write and a filter convergence away — the longest stretch any package job
        spends between "Applying N changes" and its first sign of movement.
        """
        self._report_progress(ProgressUpdate(percent=0, item="preparing remotes"))
        for derived in self._derived_remotes:
            self._report_progress(ProgressUpdate(percent=0, item=f"remote {derived.name} ({derived.scope})"))
            self._warn_about_trust(derived)
            if self.context.dry_run:
                self._log(
                    Host.TARGET,
                    LogLevel.FULL,
                    f"[dry-run] Would provision {derived.scope} flatpak remote {derived.name} "
                    f"({_DERIVED_REASON_WORDS[derived.reason]})",
                )
                continue
            await self._write_derived_remote(derived)
        self._report_progress(ProgressUpdate(percent=0, item="remote filters"))
        await self._converge_remote_filters()

        failures: list[tuple[ItemDiff, str]] = []
        try:
            await super().apply()
        except PackageItemFailures as exc:
            failures = list(exc.failures)
        await self._delete_unused_remotes()
        if failures:
            raise PackageItemFailures(self.manager_id, failures)

    def _warn_about_trust(self, derived: _DerivedRemote) -> None:
        """One WARNING per derived remote whose trust a successful provisioning leaves
        weaker than it reads (`PKG-FR-FLATPAK-REMOTE-TRUST`). Two cases, mutually exclusive:

        - The SOURCE does not verify it. Replicating it unverified is the correct outcome —
          the alternative is a remote that refuses every install — but the target then trusts
          whatever that URL serves.
        - The source verifies it and holds no key for it anywhere: no keyring of its own,
          nothing under `_OSTREE_TRUSTED_ANCHOR_DIR`, and no `gpgkeypath` naming one. Nothing
          can be synced, because there is
          nothing there; the source cannot install from that remote either, and the target
          inherits that exactly. Said out loud rather than left to flatpak's signature error
          on each ref.

        The `mutates=` phrase says the first of these too, and reaches only
        `--confirm-each-command`; this is what tells an ordinary run.

        Keyed on the derived set rather than the source's remote list: a remote no approved
        ref needs is never provisioned (D-41). `_derived_remotes` is deduplicated by remote
        id, so a remote several approved refs need still warns once.
        """
        source_item = self._source_remotes_by_id.get(derived.remote_id)
        if source_item is None:
            return
        if not source_item.gpg_verify:
            self._log(
                Host.TARGET,
                LogLevel.WARNING,
                f"The {derived.scope} flatpak remote {derived.name} does not verify signatures on "
                f"{self.machines.source}, so it is provisioned on {self.machines.target} with gpg verification "
                f"disabled: nothing checks what {derived.name} serves there either.",
            )
        elif source_item.key_digest is None and not source_item.key_paths and not self._source_trust_anchors:
            self._log(
                Host.TARGET,
                LogLevel.WARNING,
                f"The {derived.scope} flatpak remote {derived.name} verifies signatures on {self.machines.source} "
                f"but has no signing key there — neither its own keyring, nor a {_GPGKEYPATH_OPTION} of its own, "
                f"nor anything under {_OSTREE_TRUSTED_ANCHOR_DIR} — so there is none to sync and installs from it "
                f"will fail the signature check on {self.machines.target} as they do on {self.machines.source}.",
            )

    async def _write_derived_remote(self, derived: _DerivedRemote) -> None:
        """Bring one derived remote's URL and trust on the target to the source's.

        Nothing is written when the target's copy already matches whole-item — name and
        scope are the identity and equal by construction, so any remaining difference is a
        value the two machines legitimately disagree about, including the signing key: a
        target holding the remote but not its key is configured and unusable, and refuses
        every install with `Can't check signature: public key not found` (#215).

        A failure is recorded against the remote, never raised: there is no item to fail
        here, so it is charged to the approved refs that needed it (`_derived_remote_failure`,
        D-39). The exit code is not treated as proof of success either — `_origin_refusal`
        re-reads the target before each install, which is what actually catches a
        `remote-add --if-not-exists` that exited 0 and changed nothing.
        """
        source_item = self._source_remotes_by_id.get(derived.remote_id)
        if source_item is None:
            self._failed_derived_remotes[derived.remote_id] = (
                f"{self.machines.source} reports no {derived.scope}-scope remote named {derived.name!r}"
            )
            return
        target_item = self._target_remotes_by_id.get(derived.remote_id)
        # Key material held outside the remote's own keyring is what whole-item equality
        # cannot settle: two remotes both carrying no keyring of their own compare equal
        # whether or not the target holds the machine-level key the source verifies against,
        # and a `gpgkeypath` is a source-side path the target never reports at all. Both are
        # asked separately rather than read off `==` (`_anchors_to_import`, `key_paths`).
        if target_item == source_item and not self._anchors_to_import(source_item) and not source_item.key_paths:
            return

        scope_flag = _scope_flag(derived.scope)
        sudo = _sudo_prefix(derived.scope)
        try:
            staged_keys = await self._stage_source_keys(source_item, derived.remote_id)
        except ConvergeItemFailed as exc:
            self._failed_derived_remotes[derived.remote_id] = str(exc)
            return
        try:
            trust = _remote_trust_flags(source_item, staged_keys, restore_verification=target_item is not None)
            if target_item is None:
                cmd = (
                    f"{sudo}flatpak remote-add --if-not-exists {scope_flag}{trust} "
                    f"{shlex.quote(derived.name)} {shlex.quote(source_item.url)}"
                )
                phrase = f"add {derived.scope} flatpak remote {derived.name} ({source_item.url})"
            else:
                # `remote-modify` edits the existing entry in place, preserving its other
                # config and avoiding the ref-origin disruption a delete+re-add would cause.
                cmd = (
                    f"{sudo}flatpak remote-modify {scope_flag} --url={shlex.quote(source_item.url)}"
                    f"{trust} {shlex.quote(derived.name)}"
                )
                phrase = f"repoint {derived.scope} flatpak remote {derived.name} at {source_item.url}"
                # A derived write leaves no review line, so this is the only place the run
                # says which facets of the remote were actually out of step.
                self._log(Host.TARGET, LogLevel.FULL, _remote_change_detail(source_item, target_item))
            result = await self.target.run_command(
                cmd,
                login_shell=False,
                mutates=f"{phrase}{_trust_mutation_phrase(source_item, self.machines.source, staged_keys)}",
            )
            self._target_remotes_now_by_id = None
            if result.success:
                # The only trace a derived write leaves in the run's own log: it has no
                # review line and no per-item converge entry to appear in.
                self._log(Host.TARGET, LogLevel.FULL, f"provision {derived.scope} flatpak remote {derived.name}")
            else:
                self._failed_derived_remotes[derived.remote_id] = result.stderr.strip() or f"`{cmd}` failed"
        finally:
            for staged in staged_keys:
                await self._discard_staged_file(staged, "signing key")

    def _derived_remote_failure(self, item_id: str) -> str | None:
        """Why an approved ref cannot be installed because a remote it needed did not get
        provisioned (D-39) — the derived write has no item of its own to carry the failure.
        """
        for remote_id in sorted(self._ref_derived_remote_ids.get(item_id, frozenset())):
            reason = self._failed_derived_remotes.get(remote_id)
            if reason is not None:
                scope, name = _split_flatpak_item_id(remote_id, "remote")
                return (
                    f"the {scope} remote {name!r} it needs could not be provisioned on "
                    f"{self.machines.target}: {reason}"
                )
        return None

    async def _converge_remote_filters(self) -> None:
        """Bring every derived remote's ref filter to the source's, between the derived writes
        and the converge loop's first install (`PKG-FR-FLATPAK-FILTER`).

        Two directions, one pass, because they are the same obligation read from either end: a
        remote the source filters gets that filter, file and all; a remote the source does not
        filter loses whatever filter the target had (`--no-filter`, `remote-modify`'s own verb
        for it on Flatpak 1.14.6), which is the only thing that converges a filter the source
        has dropped. Nothing is cleared for the installs' benefit — the filter that is in force
        while they run is the one both machines are meant to end with.

        The target's CURRENT filters are read back rather than taken from `plan()`: the derived
        writes have just run, and `_target_remotes_now` is the same "the target's own answer is
        the only evidence" rule the origin guard uses. It answers with a path and nothing else,
        so a filter the source has is copied and applied on every run that provisions its
        remote: whether the file at that path still holds the source's bytes is not something
        the target records, and re-copying it is cheaper than a digest read per filtered
        remote per machine.

        A failure warns and is recorded against the remote (`_failed_remote_filters`), which
        `_converge_ref` turns into a per-ref failure for every approved ref whose OWN origin is
        that remote — the app's own origin, never the remote that supplied its runtime. No
        install is attempted behind a filter this run could not put right, because the filter
        it could not write is precisely what may refuse them.
        """
        for derived in self._derived_remotes:
            if derived.remote_id in self._failed_derived_remotes:
                continue
            source_item = self._source_remotes_by_id.get(derived.remote_id)
            if source_item is None:
                continue
            if source_item.filter_path is not None:
                await self._apply_source_filter(derived, source_item.filter_path)
            else:
                await self._clear_target_filter(derived)

    async def _apply_source_filter(self, derived: _DerivedRemote, path: str) -> None:
        """Copy the source's filter file to the same absolute path on the target and apply it
        there, or record why not.
        """
        if self.context.dry_run:
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"[dry-run] Would copy the ref filter {path} and apply it to the {derived.scope} "
                f"flatpak remote {derived.name} before installing from it",
            )
            return
        reason = await self._replicate_remote_filter(derived, path)
        if reason is None:
            # A filter has no item of its own, so this is the only line the run leaves about
            # it — the same reason `_write_derived_remote` logs its own success.
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"apply the ref filter {path} to the {derived.scope} flatpak remote {derived.name}",
            )
            return
        self._log(
            Host.TARGET,
            LogLevel.WARNING,
            f"The ref filter {path} of the {derived.scope} flatpak remote {derived.name} could not be replicated "
            f"to {self.machines.target}: {reason}",
        )
        self._failed_remote_filters[derived.remote_id] = (
            f"the ref filter of the {derived.scope} remote {derived.name!r} could not be replicated to {path}: "
            f"{reason}"
        )

    async def _clear_target_filter(self, derived: _DerivedRemote) -> None:
        """Take the target's own ref filter off a remote the source does not restrict, or
        record why not (`PKG-FR-MANAGER-CONVERGES`).
        """
        target_item = (await self._target_remotes_now()).get(derived.remote_id)
        if target_item is None or target_item.filter_path is None:
            return
        path = target_item.filter_path
        if self.context.dry_run:
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"[dry-run] Would take the ref filter {path} off the {derived.scope} flatpak remote {derived.name}, "
                f"which {self.machines.source} does not restrict",
            )
            return
        result = await self.target.run_command(
            f"{_sudo_prefix(derived.scope)}flatpak remote-modify {_scope_flag(derived.scope)} --no-filter "
            f"{shlex.quote(derived.name)}",
            login_shell=False,
            mutates=(
                f"take the ref filter {path} off the {derived.scope} flatpak remote {derived.name} on "
                f"{self.machines.target}"
            ),
        )
        self._target_remotes_now_by_id = None
        if result.success:
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"take the ref filter {path} off the {derived.scope} flatpak remote {derived.name}",
            )
            return
        reason = result.stderr.strip() or "flatpak refused --no-filter"
        self._log(
            Host.TARGET,
            LogLevel.WARNING,
            f"The ref filter {path} of the {derived.scope} flatpak remote {derived.name} could not be taken off "
            f"{self.machines.target}: {reason}",
        )
        self._failed_remote_filters[derived.remote_id] = (
            f"the ref filter {path} of the {derived.scope} remote {derived.name!r} could not be taken off "
            f"{self.machines.target}: {reason}"
        )

    async def _replicate_remote_filter(self, derived: _DerivedRemote, filter_path: str) -> str | None:
        """`None` once the source's filter file sits at the same absolute path on the target
        and the remote names it, otherwise why not.

        The path is the user's, not this job's (`_FLATPAK_REMOTES_CMD_TEMPLATE`), so the file
        is staged under the target's own cache like a signing key and promoted from there —
        `send_file` is plain SFTP and reaches only the SSH user's home. Both writes are
        privileged if and only if the remote's scope is `system` (`_sudo_prefix`), so a
        user-scope filter at a path the SSH user cannot write fails naming that path rather
        than escalating a user-scope run to root (T-02-23).
        """
        local_path = Path(filter_path)
        if not local_path.is_file():
            return f"{self.machines.source} has no file there"

        sudo = _sudo_prefix(derived.scope)
        staged: str | None = None
        try:
            staged = await self._stage_source_file(local_path, f"{derived.remote_id}.filter", derived.name)
            parent = shlex.quote(str(local_path.parent))
            mkdir = await self.target.run_command(
                f"{sudo}mkdir --parents {parent}",
                login_shell=False,
                mutates=f"create {local_path.parent} on {self.machines.target} for the {derived.name} ref filter",
            )
            if not mkdir.success:
                return mkdir.stderr.strip() or f"{local_path.parent} could not be created"
            write = await self.target.run_command(
                f"{sudo}install --mode=0644 {shlex.quote(staged)} {shlex.quote(filter_path)}",
                login_shell=False,
                mutates=f"write the {derived.scope} flatpak remote {derived.name}'s ref filter to {filter_path}",
            )
            if not write.success:
                return write.stderr.strip() or "the file could not be written"
            modify = await self.target.run_command(
                f"{sudo}flatpak remote-modify {_scope_flag(derived.scope)} --filter={shlex.quote(filter_path)} "
                f"{shlex.quote(derived.name)}",
                login_shell=False,
                mutates=f"filter the {derived.scope} flatpak remote {derived.name} with {filter_path}",
            )
            self._target_remotes_now_by_id = None
            if not modify.success:
                return modify.stderr.strip() or "flatpak refused the filter"
        except ConvergeItemFailed as exc:
            return str(exc)
        finally:
            await self._discard_staged_file(staged, "ref filter")
        return None

    def _remote_filter_failure(self, item: FlatpakItem) -> str | None:
        """Why an approved ref cannot be installed because the filter of the remote it comes
        from could not be converged (`PKG-FR-FLATPAK-FILTER`) — a filter has no item of its
        own, exactly like a derived write.

        Keyed on the ref's OWN origin, never on `_ref_derived_remote_ids`: that map also
        carries the remote a ref's RUNTIME came from, and an app is not "from" the remote that
        supplied its runtime.
        """
        return self._failed_remote_filters.get(f"{_REMOTE_ITEM_ID_PREFIX}{item.scope}:{item.origin}")

    async def _delete_unused_remotes(self) -> None:
        """Delete every target remote the source does not have that nothing on the target
        still uses (`PKG-FR-FLATPAK-REMOTE-DELETE`) — derived in this direction as in every
        other, so no answer is asked for and none is recorded.

        Both inputs are read as they ARE, after the converge loop: the target's own remote
        list and its own full ref list, runtimes and machine-specific apps included. That is
        what "counted after this run's approved removals" means here, and it is a measurement
        rather than a prediction — an approved removal that then failed leaves its ref
        installed, so its remote is still in use and stays.

        A deletion that fails is a WARNING and nothing more: no approved item depended on it,
        so there is nothing to charge the failure to, and the remote is simply still there
        next run.
        """
        refs = await self._target_refs_now()
        if self.context.dry_run:
            # No removal has actually run, so the preview subtracts the approved ones itself.
            # Without that it would find every remote still in use and preview no deletion at
            # all, which is exactly the case a preview of a first sync needs to show.
            approved_removals = self._approved_ref_removal_ids()
            refs = [ref for ref in refs if ref.item_id not in approved_removals]
        users = _target_refs_by_origin_remote(refs)

        target_remotes = await self._target_remotes_now()
        for remote_id in sorted(target_remotes):
            item = target_remotes[remote_id]
            if remote_id in self._source_remotes_by_id:
                continue
            still_used = users.get(remote_id)
            if still_used:
                self._log(
                    Host.TARGET,
                    LogLevel.FULL,
                    f"keeping {item.scope} flatpak remote {item.name}: {self.machines.target} still installs "
                    f"{', '.join(sorted(still_used))} from it",
                )
                continue
            if self.context.dry_run:
                self._log(
                    Host.TARGET,
                    LogLevel.FULL,
                    f"[dry-run] Would delete {item.scope} flatpak remote {item.name}, which nothing on "
                    f"{self.machines.target} installs from",
                )
                continue
            # Takes the remote's per-remote keyring with it (verified live): trust is not
            # separable from the remote on the delete side.
            result = await self.target.run_command(
                f"{_sudo_prefix(item.scope)}flatpak remote-delete {_scope_flag(item.scope)} {shlex.quote(item.name)}",
                login_shell=False,
                mutates=(
                    f"delete {item.scope} flatpak remote {item.name}, which {self.machines.source} does not have "
                    f"and nothing on {self.machines.target} installs from"
                ),
            )
            self._target_remotes_now_by_id = None
            if result.success:
                self._log(Host.TARGET, LogLevel.FULL, f"delete {item.scope} flatpak remote {item.name}")
            else:
                self._log(
                    Host.TARGET,
                    LogLevel.WARNING,
                    f"The {item.scope} flatpak remote {item.name} is unused on {self.machines.target} and "
                    f"{self.machines.source} does not have it, but it could not be deleted: "
                    f"{result.stderr.strip()}",
                )

    def _approved_ref_removal_ids(self) -> frozenset[str]:
        """The ref removals this run's review approved, for the dry-run preview alone."""
        plan, outcome = self._accepted_plan, self._accepted_outcome
        if plan is None or outcome is None:
            return frozenset()
        return frozenset(
            diff.item_id
            for diff in plan.diffs
            if diff.action is DiffAction.REMOVE and outcome.decisions.get(diff.item_id) == Decision.APPLY
        )

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Add/install/remove/delete, dispatched by item class then action — the only
        D-06/D-14-safe verbs (module docstring). One item per invocation (D-27) so a
        single bad item cannot fail the whole batch. Every command is prefixed with
        `sudo` if and only if the item's own scope is `system` (`_sudo_prefix`,
        T-02-23): a `--user` command never runs as root, and a `--system` command
        always does, regardless of which of the four verbs it is.
        """
        if diff.item_class == ItemClass.FLATPAK_REF:
            return await self._converge_ref(diff)
        if diff.item_class == ItemClass.FLATPAK_MASK:
            return await self._converge_mask(diff)
        raise ConvergeItemFailed(
            f"FlatpakSyncJob.converge: unsupported item class {diff.item_class.value!r} for {diff.label} "
            "— a remote is derived in every direction and never a diff"
        )

    async def _converge_ref(self, diff: ItemDiff) -> CommandResult:
        """Install or uninstall one ref, always naming the FULL `<application>/<arch>/
        <branch>` ref rather than the bare application id.

        Both verbs need it. `flatpak install <remote> <id>` exits 1 with `Multiple branches
        available for <id>` on a remote carrying two branches of that id, and
        `flatpak uninstall <id>` is equally ambiguous once two branches are installed
        locally — measured live against real Flathub-beta, which carries `stable` and
        `beta` for `org.mozilla.firefox`. The ref comes straight out of the item_id
        (`FlatpakItem`), so neither direction needs a source-side lookup to name its
        subject.
        """
        scope, ref = _split_flatpak_item_id(diff.item_id, "ref")
        scope_flag = _scope_flag(scope)
        sudo = _sudo_prefix(scope)

        if diff.action == DiffAction.REMOVE:
            cmd = f"{sudo}flatpak uninstall --assumeyes {scope_flag} {shlex.quote(ref)}"
            return await self.target.run_command(cmd, login_shell=False, mutates=f"uninstall {scope} flatpak {ref}")

        if diff.action == DiffAction.INSTALL:
            source_item = self._source_refs_by_id.get(diff.item_id)
            if source_item is None:
                raise ConvergeItemFailed(
                    f"no ref captured from {self.machines.source} for {diff.label} (item_id={diff.item_id!r}); "
                    "was plan() run before converge()?"
                )
            # D-39 first: a derived write that failed has no item of its own, and its own
            # stderr says far more than the symptom `_origin_refusal` would report.
            blocked = self._derived_remote_failure(diff.item_id) or self._remote_filter_failure(source_item)
            if blocked is not None:
                raise ConvergeItemFailed(f"install of {ref} refused: {blocked}")
            refusal = await self._origin_refusal(scope, source_item.origin)
            if refusal is not None:
                # T-02-24: refuse rather than issue an install that would land the wrong
                # vendor's bytes, or one flatpak will reject outright.
                raise ConvergeItemFailed(f"install of {ref} refused: {refusal}")
            cmd = (
                f"{sudo}flatpak install --assumeyes {scope_flag} {shlex.quote(source_item.origin)} {shlex.quote(ref)}"
            )
            result = await self.target.run_command(
                cmd, login_shell=False, mutates=f"install {scope} flatpak {ref} from {source_item.origin}"
            )
            if result.success:
                landed = await self._installed_origin_refusal(scope, ref, source_item.origin)
                if landed is not None:
                    raise ConvergeItemFailed(f"install of {ref} did not replicate its origin: {landed}")
            return result

        raise ConvergeItemFailed(
            f"FlatpakSyncJob.converge: unsupported action {diff.action.value!r} for a flatpak ref ({diff.label}) "
            "— version mismatches are report_only per D-04 and never reach converge()"
        )

    async def _converge_mask(self, diff: ItemDiff) -> CommandResult:
        """Add or remove one flatpak mask (#208, D-10). Scope + pattern come entirely
        from the item_id (no source-side lookup, unlike refs/remotes): a mask is a pure
        pattern, so `_split_flatpak_item_id(..., "mask")` recovers everything converge
        needs. `sudo` iff system scope (`_sudo_prefix`), the pattern `shlex.quote`d.

        Idempotent for the add direction (masking an already-present pattern exits 0);
        the remove direction only ever targets a pattern the target scope actually
        reported (it came from a REMOVE diff against the target's own mask set), so
        `mask --remove` never hits the exit-1 non-existent-pattern path. Exit code alone
        decides pass/fail (D-27).
        """
        scope, pattern = _split_flatpak_item_id(diff.item_id, "mask")
        scope_flag = _scope_flag(scope)
        sudo = _sudo_prefix(scope)

        if diff.action == DiffAction.INSTALL:
            cmd = f"{sudo}flatpak {scope_flag} mask {shlex.quote(pattern)}"
            return await self.target.run_command(
                cmd, login_shell=False, mutates=f"mask {scope} flatpak pattern {pattern}"
            )

        if diff.action == DiffAction.REMOVE:
            cmd = f"{sudo}flatpak {scope_flag} mask --remove {shlex.quote(pattern)}"
            return await self.target.run_command(
                cmd, login_shell=False, mutates=f"unmask {scope} flatpak pattern {pattern}"
            )

        raise ConvergeItemFailed(
            f"FlatpakSyncJob.converge: unsupported action {diff.action.value!r} for a flatpak mask ({diff.label})"
        )

    async def _stage_source_keys(self, item: FlatpakRemoteItem, remote_id: str) -> list[str]:
        """Copy the key material a verified source remote verifies against onto the target and
        return the staged paths (#215, `PKG-FR-FLATPAK-REMOTE-TRUST`).

        Three places the key material can be, and a remote can rest on more than one:

        - The remote has a keyring of its own: that file, byte-for-byte. It suppresses the
          machine-level anchors on the source (measured), so they do not travel either.
        - Its trust is machine-level: the source's anchor files the target lacks
          (`_anchors_to_import`), imported into this remote's own keyring on the target.
        - Its ostree `gpgkeypath` names key files or directories (`_gpgkeypath_files`). These
          travel ALONGSIDE whichever of the two above applies, because libostree adds them to
          the same verifier without suppressing anything (module docstring).

        Nothing is staged when the source's every anchor is already on the target, and
        nothing is staged when the source holds no key material for the remote at all —
        `_warn_about_trust` reports that second case, because it is a remote the SOURCE
        cannot install from either, and replicating the source's own state is what this job
        does with an unverified remote too.

        Staged and no more (`_stage_source_file`), unlike a repository key or a ref filter:
        `flatpak remote-add --gpg-import` only READS the file, and a system-scope converge
        runs under sudo, where root reads the staged copy in the user's cache without it ever
        being moved into a root-owned directory. The bytes are the source's own (ADR-020
        D-12) — never re-fetched from a vendor — and `_discard_staged_file` removes the copies
        afterwards.
        """
        if not item.gpg_verify:
            return []

        staged: list[str] = []
        if item.key_digest is not None:
            local_path = _source_keyring_path(item)
            if not local_path.is_file():
                raise ConvergeItemFailed(
                    f"signing key for {item.label()} is missing on {self.machines.source} at {local_path} "
                    "(it existed when the plan was captured); refusing to provision a remote whose key cannot "
                    "be synced"
                )
            staged.append(
                await self._stage_source_file(local_path, f"{remote_id}.gpg", f"the signing key for {item.label()}")
            )
        else:
            for index, anchor in enumerate(self._anchors_to_import(item)):
                if not anchor.is_file():
                    raise ConvergeItemFailed(
                        f"the machine-level signing key {anchor} that {item.label()} verifies against is missing on "
                        f"{self.machines.source} (it existed when the plan was captured); refusing to provision a "
                        "remote whose trust cannot be synced"
                    )
                staged.append(
                    await self._stage_source_file(
                        anchor, f"{remote_id}.anchor{index}.gpg", f"the machine-level signing key for {item.label()}"
                    )
                )

        for index, key_file in enumerate(self._gpgkeypath_files(item)):
            staged.append(
                await self._stage_source_file(
                    key_file, f"{remote_id}.keypath{index}.gpg", f"the signing key for {item.label()}"
                )
            )
        return staged

    def _gpgkeypath_files(self, item: FlatpakRemoteItem) -> list[Path]:
        """Every file the remote's `gpgkeypath` names on the source, directories expanded.

        libostree reads a listed directory's regular files one level down and a listed file as
        an ASCII key (`_ostree_gpg_verifier_add_keyfile_path`, v2024.5), so this reproduces
        that set exactly — no name filter, because libostree applies none there.

        A path that is neither fails the refs rather than provisioning a remote missing part
        of its trust, for the same reason a vanished keyring does: the option was read at plan
        time, and a key the source's own verifier cannot load is not a state to replicate
        silently.
        """
        files: list[Path] = []
        for raw in item.key_paths:
            path = Path(raw)
            if path.is_dir():
                files.extend(sorted(child for child in path.iterdir() if child.is_file()))
            elif path.is_file():
                files.append(path)
            else:
                raise ConvergeItemFailed(
                    f"the signing key {path} that {item.label()} names through its ostree {_GPGKEYPATH_OPTION} "
                    f"option is missing on {self.machines.source}; refusing to provision a remote whose trust "
                    "cannot be synced"
                )
        return files

    async def _stage_source_file(self, local_path: Path, staged_name: str, what: str) -> str:
        """Copy one of the source's own files into the target's `~/.cache/pc-switcher/` and
        return the staged path.

        `RemoteExecutor.send_file` is plain SFTP as the ordinary SSH user with no sudo path,
        so it can only write under that user's home — the same constraint
        `apt_sync.etc_apt.EtcApt._write_or_remove` solves by staging under `~/.cache/pc-switcher/`,
        reused here rather than reinvented. `staged_name` carries a remote id, so its `:` and
        `/` are flattened into one path component.
        """
        home = await self._target_home_dir()
        staging_dir = f"{home}/.cache/pc-switcher/flatpak-staging"
        mkdir = await self.target.run_command(
            f"mkdir --parents {shlex.quote(staging_dir)}",
            login_shell=False,
            mutates="create the flatpak staging directory",
        )
        if not mkdir.success:
            raise ConvergeItemFailed(
                f"failed to create {staging_dir} on {self.machines.target}: {mkdir.stderr.strip()}"
            )

        staged = f"{staging_dir}/{staged_name.replace(':', '_').replace('/', '_')}"
        await self.target.send_file(local_path, staged, mutates=f"stage {what} into {self.machines.target}'s cache")
        return staged

    async def _discard_staged_file(self, staged: str | None, what: str) -> None:
        """Remove a staged copy once flatpak has read it — the same `finally` cleanup apt's
        staging does, so a failed write never leaves transferred key or filter content sitting
        in the target's cache.
        """
        if staged is None:
            return
        await self.target.run_command(
            f"rm --force {shlex.quote(staged)}",
            login_shell=False,
            mutates=f"discard the staged flatpak {what}",
        )

    async def _target_home_dir(self) -> str:
        """The target user's home directory, resolved once per run via `echo $HOME` and
        cached (`apt_sync.files.TargetFiles.home`'s established pattern) — every staged key
        needs the same absolute path.
        """
        if self._target_home is None:
            result = await self.target.run_command("echo $HOME", login_shell=False)
            self._target_home = result.stdout.strip()
        return self._target_home

    async def _target_remotes_now(self) -> dict[str, FlatpakRemoteItem]:
        """The target's remotes as the TARGET reports them right now, cached until the next
        remote write (`__init__`).

        Read here rather than reused from `plan()` because every other candidate is
        inadmissible: the plan-time query predates this run's writes, and "this run added
        it" is not evidence at all — `flatpak remote-add --if-not-exists <name> <other url>`
        exits 0 and leaves the existing URL untouched (measured), so a successful exit code
        says nothing about what the name now points at.
        """
        if self._target_remotes_now_by_id is None:
            self._target_remotes_now_by_id = {item.item_id: item for item in await self._query_all_target_remotes()}
        return self._target_remotes_now_by_id

    async def _target_refs_now(self) -> list[FlatpakItem]:
        """Every ref the target holds right now, runtimes included — the input to
        `_delete_unused_remotes`'s "nothing still uses it" test.

        `_FLATPAK_ALL_REFS_CMD` rather than the `--app` listing: a runtime installed from a
        remote uses it exactly as an app does, and deleting the remote under it would leave it
        without updates just the same. Uncached, and read after the converge loop, so this
        run's successful removals are already absent from it.
        """
        result = await self.target.run_command(_FLATPAK_ALL_REFS_CMD, login_shell=False)
        require_answer(_FLATPAK_ALL_REFS_CMD, result, self.machines.target)
        return _parse_flatpak_list(result.stdout)

    async def _origin_refusal(self, scope: str, origin: str) -> str | None:
        """`None` if a ref may be installed from `origin` in `scope`, otherwise why not.

        Name equality is NOT the test, and that is the whole point of this guard. Measured
        against real Flathub: a target remote called `flathub` pointing at
        `https://dl.flathub.org/beta-repo/` serves a DIFFERENT vendor's build of the same
        ref — different commit, different collection id, different binary — and
        `flatpak install --assumeyes flathub <ref>` installs it at exit 0 with no warning,
        while `flatpak list --columns=origin` reports `flathub` on both machines. Only the
        URL separates the two, so the URL is what is compared (ADR-020 D-41: an origin is
        a remote's URL, never its name).

        GPG verification is compared too: a ref the source takes from a verified remote,
        landing on the target from an unverified one of the same name, has not replicated
        its provenance either. The per-remote KEY DIGEST deliberately is not — ostree's
        import merges rather than replaces, so a target that already trusted another key
        for this remote keeps both digests unequal forever, and refusing on that would
        refuse every install from it for good.
        """
        remote_id = f"flatpak:remote:{scope}:{origin}"
        source_remote = self._source_remotes_by_id.get(remote_id)
        if source_remote is None:
            return (
                f"{self.machines.source} has no {scope}-scope remote named {origin!r}, so the ref's own origin "
                "cannot be replicated (ADR-020 D-41)"
            )
        target_remote = (await self._target_remotes_now()).get(remote_id)
        if target_remote is None:
            return f"origin remote {origin!r} ({scope}) is not configured on {self.machines.target} (D-14)"
        if target_remote.url != source_remote.url:
            return (
                f"{self.machines.target}'s {scope}-scope remote {origin!r} points at {target_remote.url}, "
                f"but {self.machines.source} takes this ref from {source_remote.url} — same name, different "
                "repository, so installing would replicate the name and invert the provenance"
            )
        if target_remote.gpg_verify != source_remote.gpg_verify:
            # The URL belongs in this message as much as in the one above, even though this
            # branch is reached only once the two URLs match: an origin is a repository plus
            # the verification of what it serves, and naming the setting alone tells the
            # reader a check differs without saying which repository it is a check on.
            return (
                f"{self.machines.target}'s {scope}-scope remote {origin!r} points at {target_remote.url} with "
                f"gpg verification {_verification_word(target_remote)}, while {self.machines.source} takes this "
                f"ref from {source_remote.url} with it {_verification_word(source_remote)}"
            )
        return None

    async def _installed_origin_refusal(self, scope: str, ref: str, expected_origin: str) -> str | None:
        """`None` if `ref` really did land in `scope` from `expected_origin`'s repository,
        otherwise why not — read back off the target AFTER the install (ADR-020 D-41's
        "checked, not inferred", the flatpak counterpart of D-35).

        The read is `_FLATPAK_LIST_CMD`, not `flatpak info --show-origin`, because ADR-022
        D-03 forbids an ambiguous discriminator and `flatpak info` exits 1 both for a ref
        that is not installed (data — this function's own finding) and for an installation
        that cannot be opened (a probe that did not answer). The listing separates them: a
        ref that is not installed is simply an absent row at exit 0, so a non-zero exit
        means only that the tool failed and `require_answer` fails the job once.

        What is compared is the URL behind the reported origin AND that remote's verification
        setting, never the origin's name: the wrong-vendor case is precisely two same-named
        remotes, and a name-only check passes it. Both facets, because the article defines an
        origin as both and this check is the same check as `_origin_refusal`'s, run on what
        landed rather than on what was configured beforehand — a remote whose verification was
        turned off between the two would otherwise pass here having failed there.

        The remote listing is re-read rather than reused: `_target_remotes_now` caches until
        the next remote WRITE, and this run issues none between the pre-install check and
        here, so the cached snapshot is the one the pre-install check already judged. Reusing
        it would make this pass a second reading of the same evidence instead of a look at the
        target's state after the install — which is the whole of what this function is for.
        """
        result = await self.target.run_command(_FLATPAK_LIST_CMD, login_shell=False)
        require_answer(_FLATPAK_LIST_CMD, result, self.machines.target)
        landed = next(
            (item for item in _parse_flatpak_list(result.stdout) if item.scope == scope and item.ref == ref), None
        )
        if landed is None:
            return f"flatpak exited 0 but {self.machines.target} does not list {ref} in the {scope} installation"
        source_remote = self._source_remotes_by_id[f"flatpak:remote:{scope}:{expected_origin}"]
        self._target_remotes_now_by_id = None
        target_remotes = await self._target_remotes_now()
        landed_remote = target_remotes.get(f"flatpak:remote:{scope}:{landed.origin}")
        if landed_remote is None:
            return (
                f"{ref} reports origin {landed.origin!r}, which {self.machines.target} does not configure "
                f"in {scope} scope"
            )
        if landed_remote.url != source_remote.url:
            return (
                f"{ref} came from {landed.origin!r} at {landed_remote.url}, but {self.machines.source} takes it "
                f"from {source_remote.url}"
            )
        if landed_remote.gpg_verify != source_remote.gpg_verify:
            return (
                f"{ref} came from {landed.origin!r} at {landed_remote.url} with gpg verification "
                f"{_verification_word(landed_remote)}, but {self.machines.source} takes it from "
                f"{source_remote.url} with it {_verification_word(source_remote)}"
            )
        return None

    async def _system_scope_in_play(self) -> bool:
        """Whether ANY system-scope ref, remote or mask exists on either machine — the
        gate for `validate()`'s sudo check (T-02-23, ASVS V4): user-scope flatpak
        operations need no root at all, so this job never asks for a privilege it
        will not use. A system-scope mask on either machine (#208, D-07) writes into
        `/var/lib/flatpak` just like a system remote, so it too requires target sudo.
        """
        if any(item.scope == "system" for item in await self.capture_source_items()):
            return True
        if any(item.scope == "system" for item in await self.query_target_items()):
            return True
        if await self._capture_source_remotes("system"):
            return True
        if await self._query_target_remotes("system"):
            return True
        if await self._capture_source_masks("system"):
            return True
        return bool(await self._query_target_masks("system"))

    @override
    async def validate(self) -> list[ValidationError]:
        """`flatpak --version` on both ends — a missing binary is a reported
        validation error naming flatpak's absence (it ships in no default Ubuntu
        24.04 install and may genuinely be absent), never an exception. `sudo --non-interactive
        true` on the target only when a system-scope ref, remote or mask actually
        exists on either machine.

        Sequential checks appending to `errors`, matching `AptSyncJob.validate()`'s/
        `SnapSyncJob.validate()`'s shape.
        """
        errors: list[ValidationError] = []

        source_check = await self.source.run_command("flatpak --version")
        if not source_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "flatpak is not available on source (it is not part of a default Ubuntu 24.04 "
                    "install and may genuinely be absent; there is nothing for flatpak_sync to capture here).",
                )
            )

        target_check = await self.target.run_command("flatpak --version", login_shell=False)
        if not target_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "flatpak is not available on target (it is not part of a default Ubuntu 24.04 "
                    "install; run `sudo apt install flatpak` on the target before enabling flatpak_sync).",
                )
            )

        if source_check.success and target_check.success and await self._system_scope_in_play():
            sudo_check = await self.target.run_command("sudo --non-interactive true", login_shell=False)
            if not sudo_check.success:
                errors.append(
                    self._validation_error(
                        Host.TARGET,
                        "passwordless sudo is not available on target "
                        "(required for system-scope flatpak install/uninstall/remote-add/remote-delete).\n"
                        + passwordless_sudo_hint(_TARGET_SUDO_COMMANDS, user=self.context.target_username),
                    )
                )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): flatpak refs, remotes and masks."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=[
                "installed flatpak refs (per user/system scope)",
                "flatpak mask patterns (per scope)",
                # Named as a consequence rather than as something reviewed: remotes are
                # derived from the approved refs, so they are never ticked, but a first
                # sync does add, repoint and delete them on the target. Ruling 6's conflict
                # screen is the one exception and is not named here — it asks about a remote
                # feeding refs this machine already keeps, which a first sync has none of.
                "the flatpak remotes those refs come from, and unused remotes this sync "
                "deletes (per scope, without a review line)",
            ],
            mechanism="flatpak install/uninstall/mask per item after review, with each ref's remote provisioned first",
        )
