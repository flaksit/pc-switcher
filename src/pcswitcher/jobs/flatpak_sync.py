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

`flatpak install` refuses outright when the remote it names is not yet configured in
the scope being installed into (D-14), so remotes are captured and diffed as their
own item class (`FLATPAK_REMOTE`) and this job's own ordering stage (mirroring
`apt_sync`'s key-before-source sort) places every remote diff ahead of every ref diff
before the plan's `diffs` tuple ever reaches `apply()`'s per-diff loop. Before
converging a ref, its origin remote is checked against what already exists on the
target in that scope OR what this SAME run has already successfully added — a ref
whose remote is missing from both is skipped with a per-item failure naming the
remote, rather than issuing an install flatpak will reject (T-02-24). The removal
direction is disclosure rather than refusal (#214): a remote offered for deletion
carries, in its review `detail`, the target refs that still name it as their origin in
that same scope — deleting a remote whose refs are being removed too is legitimate
cleanup, so the decision stays in the review where D-30 puts apt's collateral, never as
a mid-apply refusal.

A remote carries its TRUST as part of the item, not as a property of the machine that
happens to hold it (#215): `FlatpakRemoteItem` records the remote's GPG-verification
setting and the digest of its own ostree keyring, and convergence replicates both —
`flatpak remote-add --gpg-import=<staged key>` for a signed remote, `--no-gpg-verify`
only when the SOURCE remote is itself unverified. Without this a replicated remote is
configured but unusable: flatpak refuses every install from it with `Can't check
signature: public key not found`. The key bytes travel byte-for-byte from the source
machine and are never re-fetched from a vendor (ADR-020 D-12's rule for apt signing
keys), staged under the target's `~/.cache/pc-switcher/` exactly as `apt_sync` stages
`/etc/apt` content, because SFTP reaches only the SSH user's own home.

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

`FlatpakSyncJob` subclasses `PackageSyncJob` but overrides `plan()` rather than
inheriting the base implementation, for the same reason `SnapSyncJob` does (see that
module's docstring and 02-08's own deviation note): `PackageSyncJob.diff_items`/
`_diff_apt_packages` is apt-package-shaped — one item class, `MISSING_ON_TARGET`/
`EXTRA_ON_TARGET`/`VERSION_MISMATCH` only, no notion of a second item class that must
converge ahead of the first. This job diffs and converges THREE item classes
(`FLATPAK_REMOTE`, `FLATPAK_REF`, `FLATPAK_MASK`) with an ordering dependency between
them (remotes -> refs -> masks, D-08/D-14), which the shared dispatch has no way to
express. `plan()` here reuses every manager-agnostic
building block the shared core provides — `DecisionFile`/`filter_inert` (D-08's
machine-local skip-always filtering) and `PackageSyncJob._build_review_groups`
(D-24's action-grouped review) — so only capture, diff and converge are genuinely
flatpak-specific. `accept_review()`, `apply()` and `execute()` are inherited
unchanged; this job implements no review of its own — the coordinator (plan 02-03)
reviews every enabled manager at once, and this module never calls that reviewing
function directly.

Flatpak ref VERSIONS are captured for reporting only (D-04, like apt package
versions): a version difference on a ref present in the same scope on both machines
is a `REPORT_ONLY` diff, never something this job installs or removes to force.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, override

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
    build_version_mismatch_detail,
)
from pcswitcher.jobs.packages.probes import require_answer
from pcswitcher.jobs.packages.state import DecisionFile, filter_inert
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, ValidationError
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

# Same reasoning for `flatpak remotes`, but flatpak tracks remotes PER INSTALLATION —
# even a byte-identical `flathub` URL is two separate configuration entries — so this
# is run once per scope rather than once combined (module docstring, D-14). `options` is
# the only place flatpak exposes a remote's GPG-verification state (#215): it carries a
# comma-separated token list in which `no-gpg-verify` appears exactly when the remote's
# `gpg-verify` is false, and the column is EMPTY (no trailing tab) for a remote with no
# options at all — RESEARCH: verified live against Flatpak 1.14.6, so the parser accepts
# both a two-field and a three-field line.
_FLATPAK_REMOTES_CMD_TEMPLATE = "flatpak remotes {flag} --columns=name,url,options"

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

# One batched `sha256sum` per scope over that glob, mirroring `apt_sync`'s
# `_capture_dir_digests` — never one command per remote. A scope with no keyring at all
# makes the glob match nothing, so `sha256sum` prints nothing on stdout and exits 1;
# stderr is discarded and the empty stdout parses to an empty map (verified live).
_FLATPAK_KEYRING_DIGESTS_CMD_TEMPLATE = "sha256sum {directory}/*{suffix} 2>/dev/null"

# The system installation's fixed location. Its `repo/` is 0755 root with 0644 keyring
# files (verified live), so reading a digest there needs no sudo even though writing to
# it does.
_FLATPAK_SYSTEM_INSTALLATION = Path("/var/lib/flatpak")

# Masks are ALSO per-installation (#208, D-10), listed one pattern per line with no
# header — but the scope flag MUST precede the `mask` subcommand: bare `flatpak mask`
# omits --user masks and defaults to --system (RESEARCH: verified live, Flatpak 1.14.6),
# so this always names its scope explicitly, once per scope like remotes.
_FLATPAK_MASK_CMD_TEMPLATE = "flatpak {flag} mask"

# Both scopes this item model and flatpak's own --user/--system flags recognise.
_SCOPES: tuple[Literal["user", "system"], ...] = ("user", "system")

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
# item or writes the orphaned-refs detail.


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
        return f"flatpak:ref:{self.scope}:{self.ref}"

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
    trust comes from a machine-level anchor under `/usr/share/ostree/trusted.gpg.d`
    rather than from a per-remote key.

    The DIGEST lives on the item, not the key bytes. An item is carried through the diff,
    the review and the decision file, all of which want an identity and a comparison,
    never a payload; the bytes themselves travel
    separately and byte-for-byte (`flatpak_sync` stages the source's keyring file and
    passes it to `flatpak remote-add --gpg-import`), which is ADR-020 D-12's rule that
    key material is copied from the source machine and never re-fetched from a vendor.
    """

    name: str
    url: str
    scope: Literal["user", "system"]
    gpg_verify: bool = True
    key_digest: str | None = None

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


def build_orphaned_refs_detail(remote: str, refs: Sequence[str]) -> str:
    """Detail string for a flatpak remote REMOVE diff whose removal would leave
    target-side refs without their origin (#214).

    Names the consequence in the review the user approves from, the same place D-30 puts
    apt's transaction collateral — deleting the remote is still offered, because a
    legitimate cleanup removes the refs too, but it is never offered as a bare presence
    difference with nothing said about what depends on it.
    """
    return f"target refs still using {remote}: {', '.join(refs)} (removal orphans them)"


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


def _source_keyring_path(item: FlatpakRemoteItem) -> Path:
    """The LOCAL path of the source machine's own keyring file for `item`.

    `send_file` transfers from the local filesystem, and the source executor runs on
    this machine as this user (the same assumption `AptSyncJob._write_or_remove_repo_item`
    makes for `/etc/apt`), so `Path.home()` resolves the very directory
    `_repo_dir_expression("user")`'s `$HOME` expands to on the source side.
    """
    installation = _FLATPAK_SYSTEM_INSTALLATION if item.scope == "system" else Path.home() / _FLATPAK_DATA_RELPATH
    return installation / "repo" / f"{item.name}{_TRUSTEDKEYS_SUFFIX}"


def _parse_keyring_digests(output: str) -> dict[str, str]:
    """`{remote name: sha256}` from one scope's batched `sha256sum` output.

    `<digest>  <path>` lines, mapped by stripping the `.trustedkeys.gpg` suffix off the
    basename — a remote name may contain dots (`my.remote.name.trustedkeys.gpg`,
    verified live), so only the fixed suffix is removed, never everything after the
    first dot.
    """
    digests: dict[str, str] = {}
    for line in _lines(output):
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, path = parts
        name = Path(path.strip()).name
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
    output: str, scope: Literal["user", "system"], key_digests: Mapping[str, str]
) -> list[FlatpakRemoteItem]:
    """Parse one scope's `flatpak remotes --columns=name,url,options` output.

    `scope` is a parameter, not a parsed column: unlike `flatpak list`, this command
    has no scope column of its own — the caller already knows which scope it asked
    about, because it chose the `--user`/`--system` flag (module docstring).
    `key_digests` is the same scope's `_parse_keyring_digests` map, joined here by
    remote name so a remote's trust arrives as part of the item rather than as a
    second lookup at converge time (#215).

    A remote with no options at all prints only two fields (no trailing tab), so both
    widths are accepted; a remote absent from `key_digests` keeps `key_digest=None`,
    which is the honest reading of "verification is on but this remote carries no key of
    its own" — trust then comes from a machine-level anchor this job neither reads nor
    replicates.
    """
    items: list[FlatpakRemoteItem] = []
    for line in _lines(output):
        fields = line.split("\t")
        if len(fields) not in (2, 3):
            continue
        name, url = fields[0], fields[1]
        options = fields[2] if len(fields) == 3 else ""
        gpg_verify = _NO_GPG_VERIFY_OPTION not in options.split(",")
        items.append(
            FlatpakRemoteItem(
                name=name,
                url=url,
                scope=scope,
                gpg_verify=gpg_verify,
                key_digest=key_digests.get(name) if gpg_verify else None,
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


def _version_mismatch_ref_diff(item_id: str, source_item: FlatpakItem, target_item: FlatpakItem) -> ItemDiff:
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
        detail=build_version_mismatch_detail(source_item.version, target_item.version),
    )


def _diff_flatpak_refs(source_items: Sequence[FlatpakItem], target_items: Sequence[FlatpakItem]) -> list[ItemDiff]:
    """One diff per ref `item_id` present on either side, source-then-target order —
    same shape as `PackageSyncJob._diff_apt_packages`/`snap_sync._diff_snap_items`.
    Scope already lives inside `item_id`, so an application installed in a different
    scope on each machine naturally produces one install-side entry and one
    remove-side entry here, never a single combined diff.
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
        elif source_item is not None and target_item is not None and source_item.version != target_item.version:
            diffs.append(_version_mismatch_ref_diff(item_id, source_item, target_item))
        # else: present on both, same scope, equal version -> no diff.

    return diffs


def _install_remote_diff(item: FlatpakRemoteItem) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.FLATPAK_REMOTE,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.INSTALL,
        item_id=item.item_id,
        label=item.label(),
        detail=None,
    )


def _remove_remote_diff(item: FlatpakRemoteItem, dependent_refs: Sequence[str]) -> ItemDiff:
    """Deleting a remote the target's own refs still name as their origin orphans them
    (#214), so the dependents are named in `detail` — the review states the consequence
    before the user approves it, D-30's placement for apt's transaction collateral.

    Not a refusal, unlike the ref-install direction's `_remote_ready_on_target` guard:
    removing a remote whose refs are being removed in the same run is a legitimate
    cleanup, and the decision belongs in the review rather than mid-apply. A remote with
    no dependents keeps `detail=None` — no noise on the common case.
    """
    return ItemDiff(
        item_class=ItemClass.FLATPAK_REMOTE,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REMOVE,
        item_id=item.item_id,
        label=item.label(),
        detail=build_orphaned_refs_detail(item.name, dependent_refs) if dependent_refs else None,
    )


def _remote_change_detail(source_item: FlatpakRemoteItem, target_item: FlatpakRemoteItem) -> str:
    """Name every facet in which the two sides' same-identity remotes differ.

    Only the differing facets appear, so a plain URL edit still reads exactly as it did
    before trust joined the item (#215) and a trust-only divergence never mentions a URL
    both machines agree on.
    """
    facets: list[str] = []
    if source_item.url != target_item.url:
        facets.append(f"url: {source_item.url} vs {target_item.url}")
    if source_item.gpg_verify != target_item.gpg_verify:
        facets.append(f"gpg verification: {_verification_word(source_item)} vs {_verification_word(target_item)}")
    if source_item.key_digest != target_item.key_digest:
        facets.append(f"signing key: {source_item.key_digest or 'none'} vs {target_item.key_digest or 'none'}")
    return f"remote {source_item.name} " + "; ".join(facets)


def _verification_word(item: FlatpakRemoteItem) -> str:
    return "enabled" if item.gpg_verify else "disabled"


def _remote_trust_flags(item: FlatpakRemoteItem, staged_key: str | None, *, restore_verification: bool) -> str:
    """The `flatpak remote-add`/`remote-modify` flags that replicate `item`'s trust
    (#215), as a string that begins with a space or is empty.

    `--no-gpg-verify` is emitted if and only if the SOURCE remote is itself unverified:
    a remote the source verifies can never be silently downgraded on the target, and an
    unverified one is replicated as unverified rather than as a verified remote that
    would then refuse every install. `restore_verification` adds the explicit
    `--gpg-verify` that only `remote-modify` accepts (`remote-add` has no such flag —
    verification is its default), so a CHANGE can lift a target-side remote back out of
    `no-gpg-verify` instead of leaving the divergence half-converged.

    A verified remote with `staged_key is None` carries no per-remote key at all: nothing
    is invented for it, and its trust stays whatever machine-level anchor the target has.
    """
    if not item.gpg_verify:
        return " --no-gpg-verify"
    flags = " --gpg-verify" if restore_verification else ""
    if staged_key is not None:
        flags += f" --gpg-import={shlex.quote(staged_key)}"
    return flags


def _trust_mutation_phrase(item: FlatpakRemoteItem) -> str:
    """Trailing clause for the `mutates=` phrase, so the confirm-each-command prompt and
    the trace state what a remote command does to TRUST, not only to the URL.
    """
    if not item.gpg_verify:
        return ", with gpg verification disabled (as on the source)"
    if item.key_digest is None:
        return ""
    return ", importing the source's signing key"


def _change_remote_diff(source_item: FlatpakRemoteItem, target_item: FlatpakRemoteItem) -> ItemDiff:
    """A remote present on both sides with the same name AND scope but a DIFFERING URL,
    GPG-verification setting or signing key: converge the target's remote to the
    source's configuration (decision 7, #215).

    A trust difference is a `CHANGE` for exactly the reason a URL difference is: the two
    machines hold the same remote with a different value, and `flatpak remote-modify`
    converges it in place — `--url`, `--gpg-verify`/`--no-gpg-verify` and `--gpg-import`
    combine in ONE command (verified live), so a differing key needs no REMOVE+INSTALL
    churn and never disturbs the refs whose origin the remote is. Tagged
    `VERSION_MISMATCH` for the same reason apt config/source edits and snap
    revision/channel changes are (`apt_sync`/`snap_sync`): it is the "same identity,
    differing value" conflict class, the only `DiffClass` a CHANGE ever carries.

    A key CHANGE converges by IMPORT, and ostree's import merges rather than replaces
    (verified live): a target that already trusted a different key for this remote ends
    up trusting both, so the digests stay unequal and the item is offered again on the
    next run. That is reported-as-found, consistent with the rest of this job — trust the
    user established locally is never deleted to make a digest match, and the usual
    skip-always decision (D-08) is the way to silence it.
    """
    return ItemDiff(
        item_class=ItemClass.FLATPAK_REMOTE,
        diff_class=DiffClass.VERSION_MISMATCH,
        action=DiffAction.CHANGE,
        item_id=source_item.item_id,
        label=source_item.label(),
        detail=_remote_change_detail(source_item, target_item),
    )


def _target_refs_by_origin_remote(target_refs: Sequence[FlatpakItem]) -> dict[str, list[str]]:
    """Target refs keyed by the `item_id` of the remote they name as origin, IN THEIR OWN
    SCOPE (#214).

    A remote is per-installation (module docstring), so `flathub` in `user` and
    `flathub` in `system` are two entries and only same-scope refs depend on either —
    keying by the full remote item_id rather than the bare name is what keeps a
    user-scope ref out of the system-scope remote's dependent list.
    """
    by_remote: dict[str, list[str]] = {}
    for ref in target_refs:
        by_remote.setdefault(f"flatpak:remote:{ref.scope}:{ref.origin}", []).append(ref.ref)
    return by_remote


def _diff_flatpak_remotes(
    source_items: Sequence[FlatpakRemoteItem],
    target_items: Sequence[FlatpakRemoteItem],
    target_refs: Sequence[FlatpakItem],
) -> list[ItemDiff]:
    """One diff per remote `item_id` (name + scope) present on either side.

    A remote present on both sides with the SAME name and scope but a DIFFERING URL,
    GPG-verification setting or signing key is a `CHANGE` diff that converges the target
    to the source's configuration (decision 7 / T-02-22, #215): a same-name remote whose
    URL or trust was silently not propagated is exactly the gap this closes. The
    comparison is whole-item equality — name and scope ARE the identity and are equal by
    construction here, so every remaining field is a value the two machines can
    legitimately disagree about, and a difference in name or scope produces two separate
    install/remove diffs rather than a change (module docstring's scope-as-identity rule).

    `target_refs` is the SAME queried ref list the ref diff is built from — it is what
    lets a REMOVE diff name the refs its removal would orphan (#214) without a
    per-remote query of its own.
    """
    source_by_id = {item.item_id: item for item in source_items}
    target_by_id = {item.item_id: item for item in target_items}
    dependents_by_remote_id = _target_refs_by_origin_remote(target_refs)

    seen: dict[str, None] = {}
    for item in (*source_items, *target_items):
        seen.setdefault(item.item_id, None)

    diffs: list[ItemDiff] = []
    for item_id in seen:
        source_item = source_by_id.get(item_id)
        target_item = target_by_id.get(item_id)

        if source_item is not None and target_item is None:
            diffs.append(_install_remote_diff(source_item))
        elif target_item is not None and source_item is None:
            diffs.append(_remove_remote_diff(target_item, dependents_by_remote_id.get(item_id, [])))
        elif source_item is not None and target_item is not None and source_item != target_item:
            diffs.append(_change_remote_diff(source_item, target_item))
        # else: present on both, identical url and trust -> no diff.

    return diffs


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
        self._source_remotes_by_id: dict[str, FlatpakRemoteItem] = {}
        self._target_remotes_by_id: dict[str, FlatpakRemoteItem] = {}
        # (scope, name) pairs for remotes THIS RUN has already successfully added —
        # consulted by a ref install's remote-readiness guard alongside
        # `_target_remotes_by_id`, since a remote approved in the same run is not yet
        # present in the plan-time target query (D-14's whole point: it converges
        # first, in the SAME run, not in a prior one).
        self._converged_remote_scope_names: set[tuple[str, str]] = set()
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
        require_answer(_FLATPAK_LIST_CMD, result, Host.SOURCE)
        return _parse_flatpak_list(result.stdout)

    async def query_target_items(self) -> Sequence[FlatpakItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The target's own `flatpak list --app` (same reasoning as `capture_source_items`)."""
        result = await self.target.run_command(_FLATPAK_LIST_CMD, login_shell=False)
        require_answer(_FLATPAK_LIST_CMD, result, Host.TARGET)
        return _parse_flatpak_list(result.stdout)

    async def _capture_source_remotes(self, scope: Literal["user", "system"]) -> list[FlatpakRemoteItem]:
        """One scope's remotes plus their per-remote keyring digests (#215): two reads,
        one listing and one batched `sha256sum`, never a command per remote.

        Only the listing is guarded. The keyring digest command is the documented
        counter-example to a blanket exit-code rule (ADR-022): its glob legitimately
        matches nothing on a scope with no remote keyring, and `sha256sum` then exits 1 —
        so on that command a non-zero exit is the NORMAL answer, and guarding it would fail
        every run on a machine with no flatpak remotes.
        """
        keys = await self.source.run_command(_keyring_digests_cmd(scope))
        command = _FLATPAK_REMOTES_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.source.run_command(command)
        require_answer(command, result, Host.SOURCE)
        return _parse_flatpak_remotes(result.stdout, scope, _parse_keyring_digests(keys.stdout))

    async def _query_target_remotes(self, scope: Literal["user", "system"]) -> list[FlatpakRemoteItem]:
        keys = await self.target.run_command(_keyring_digests_cmd(scope), login_shell=False)
        command = _FLATPAK_REMOTES_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.target.run_command(command, login_shell=False)
        require_answer(command, result, Host.TARGET)
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
        require_answer(cmd, result, Host.SOURCE)
        return _parse_flatpak_masks(result.stdout, scope)

    async def _query_target_masks(self, scope: Literal["user", "system"]) -> list[FlatpakMaskItem]:
        cmd = _FLATPAK_MASK_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.target.run_command(cmd, login_shell=False)
        require_answer(cmd, result, Host.TARGET)
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

    @override
    async def plan(self) -> PackagePlan:
        """Load decision files -> capture -> query -> diff -> build review groups.

        Read-only: only `flatpak list`/`flatpak remotes`/`flatpak mask` (both machines,
        both scopes) and a decision-file `cat` run here — no `flatpak install`/
        `uninstall`/`remote-add`/`remote-delete`/`mask` mutation before this returns.
        Caches the filtered source/target refs and remotes by id for `converge()` (see
        `__init__`); masks need no cache (pattern is fully in the item_id).

        Diffs are ordered remotes -> refs -> masks in the returned `diffs` tuple — this
        job's own ordering stage, mirroring `apt_sync`'s key-before-source sort: a ref's
        `flatpak install` fails when its remote is not yet configured in that scope
        (D-14), and a mask applied before its refs could suppress an auto-pulled
        dependency of a ref being installed the same run (D-08). The base `apply()` loop
        converges `plan.diffs` in order, so this ordering is what enforces both.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        target_decisions = await DecisionFile(self.manager_id, self.target).load()

        source_refs = await filter_inert(await self.capture_source_items(), source_decisions)
        installed_target_refs = await self.query_target_items()
        target_refs = await filter_inert(installed_target_refs, target_decisions)
        source_remotes = await filter_inert(await self._capture_all_source_remotes(), source_decisions)
        target_remotes = await filter_inert(await self._query_all_target_remotes(), target_decisions)
        source_masks = await filter_inert(await self._capture_all_source_masks(), source_decisions)
        target_masks = await filter_inert(await self._query_all_target_masks(), target_decisions)

        self._source_refs_by_id = {item.item_id: item for item in source_refs}
        self._source_remotes_by_id = {item.item_id: item for item in source_remotes}
        self._target_remotes_by_id = {item.item_id: item for item in target_remotes}

        # Target refs feed the remote diff too: a remote offered for REMOVE names the
        # refs whose origin it is (#214), read off the ref query already in hand rather
        # than a per-remote lookup. The UNFILTERED list, deliberately: a ref recorded
        # skip-always is excluded from the diff (D-08) but is still installed, so
        # deleting its remote still orphans it. Refs whose own REMOVE diff is proposed
        # this run are NOT excluded either — the review has not happened at plan time, a
        # proposal is not an approval, and the user may untick the ref removal while
        # ticking the remote's; the detail states the target's current state, which holds
        # either way.
        remote_diffs = _diff_flatpak_remotes(source_remotes, target_remotes, installed_target_refs)
        ref_diffs = _diff_flatpak_refs(source_refs, target_refs)
        mask_diffs = _diff_flatpak_masks(source_masks, target_masks)
        # Ordering (D-08): remotes -> refs -> masks. A mask must land AFTER the refs so
        # it can never suppress an auto-pulled dependency of a ref being installed the
        # same run; converge() carries the pattern fully in the item_id, so masks (unlike
        # refs) need no source-side cache.
        # Every flatpak item class carries its own id into `filter_inert` above, so this
        # pass is a no-op backstop here — kept so all four `plan()`s end the same way and
        # the read path can never drift from `_record_permanent_skips`'s write path.
        diffs = self._drop_inert_diffs((*remote_diffs, *ref_diffs, *mask_diffs), source_decisions, target_decisions)

        groups = self._build_review_groups(diffs)
        return PackagePlan(manager=self.manager_id, diffs=diffs, groups=groups)

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Add/install/remove/delete, dispatched by item class then action — the only
        D-06/D-14-safe verbs (module docstring). One item per invocation (D-27) so a
        single bad item cannot fail the whole batch. Every command is prefixed with
        `sudo` if and only if the item's own scope is `system` (`_sudo_prefix`,
        T-02-23): a `--user` command never runs as root, and a `--system` command
        always does, regardless of which of the four verbs it is.
        """
        if diff.item_class == ItemClass.FLATPAK_REMOTE:
            return await self._converge_remote(diff)
        if diff.item_class == ItemClass.FLATPAK_REF:
            return await self._converge_ref(diff)
        if diff.item_class == ItemClass.FLATPAK_MASK:
            return await self._converge_mask(diff)
        raise ConvergeItemFailed(
            f"FlatpakSyncJob.converge: unsupported item class {diff.item_class.value!r} for {diff.label}"
        )

    async def _converge_remote(self, diff: ItemDiff) -> CommandResult:
        scope, name = _split_flatpak_item_id(diff.item_id, "remote")
        scope_flag = _scope_flag(scope)
        sudo = _sudo_prefix(scope)

        if diff.action in (DiffAction.INSTALL, DiffAction.CHANGE):
            source_item = self._source_remotes_by_id.get(diff.item_id)
            if source_item is None:
                raise ConvergeItemFailed(
                    f"no captured source remote for {diff.label} (item_id={diff.item_id!r}); "
                    "was plan() run before converge()?"
                )
            # The source's own key bytes are staged on the target first, so ONE command
            # (D-27) then carries url and trust together.
            staged_key = await self._stage_source_key(source_item, diff)
            try:
                trust = _remote_trust_flags(
                    source_item, staged_key, restore_verification=diff.action == DiffAction.CHANGE
                )
                if diff.action == DiffAction.INSTALL:
                    cmd = (
                        f"{sudo}flatpak remote-add --if-not-exists {scope_flag}{trust} "
                        f"{shlex.quote(name)} {shlex.quote(source_item.url)}"
                    )
                    result = await self.target.run_command(
                        cmd,
                        login_shell=False,
                        mutates=(
                            f"add {scope} flatpak remote {name} ({source_item.url})"
                            f"{_trust_mutation_phrase(source_item)}"
                        ),
                    )
                    if result.success:
                        self._converged_remote_scope_names.add((scope, name))
                    return result

                # `remote-modify` edits the existing entry in place (the remote is
                # present on both sides by construction of a CHANGE diff), preserving its
                # other config and avoiding the ref-origin disruption a delete+re-add
                # would cause. No need to record it in `_converged_remote_scope_names`:
                # the remote already exists on the target, so ref-readiness
                # (`_remote_ready_on_target`) is already satisfied via the plan-time
                # target query.
                cmd = (
                    f"{sudo}flatpak remote-modify {scope_flag} --url={shlex.quote(source_item.url)}"
                    f"{trust} {shlex.quote(name)}"
                )
                return await self.target.run_command(
                    cmd,
                    login_shell=False,
                    mutates=(
                        f"repoint {scope} flatpak remote {name} at {source_item.url}"
                        f"{_trust_mutation_phrase(source_item)}"
                    ),
                )
            finally:
                await self._discard_staged_key(staged_key)

        if diff.action == DiffAction.REMOVE:
            # Takes the remote's per-remote keyring with it (verified live): trust is not
            # separable from the remote on the delete side, which is why the removal
            # review's `detail` names the refs it orphans (#214) rather than pretending
            # the configuration could be restored piecemeal afterwards.
            cmd = f"{sudo}flatpak remote-delete {scope_flag} {shlex.quote(name)}"
            return await self.target.run_command(
                cmd, login_shell=False, mutates=f"delete {scope} flatpak remote {name}"
            )

        raise ConvergeItemFailed(
            f"FlatpakSyncJob.converge: unsupported action {diff.action.value!r} for a flatpak remote ({diff.label})"
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
                    f"no captured source ref for {diff.label} (item_id={diff.item_id!r}); "
                    "was plan() run before converge()?"
                )
            if not self._remote_ready_on_target(scope, source_item.origin):
                # T-02-24: refuse rather than issue an install flatpak will reject.
                raise ConvergeItemFailed(
                    f"install of {ref} refused: origin remote {source_item.origin!r} ({scope}) is "
                    "neither already configured on the target nor among this run's successfully-added "
                    "remotes (D-14)"
                )
            cmd = (
                f"{sudo}flatpak install --assumeyes {scope_flag} {shlex.quote(source_item.origin)} {shlex.quote(ref)}"
            )
            return await self.target.run_command(
                cmd, login_shell=False, mutates=f"install {scope} flatpak {ref} from {source_item.origin}"
            )

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

    async def _stage_source_key(self, item: FlatpakRemoteItem, diff: ItemDiff) -> str | None:
        """Copy the source remote's own keyring onto the target and return its staged
        path, or `None` when the remote has no key to carry (#215).

        `RemoteExecutor.send_file` is plain SFTP as the ordinary SSH user with no sudo
        path, so it can only write under that user's home — the same constraint
        `AptSyncJob._write_or_remove_repo_item` solves by staging under
        `~/.cache/pc-switcher/`, reused here rather than reinvented. No `install`
        promotion follows it, unlike apt's: `flatpak remote-add --gpg-import` only READS
        the file, and a system-scope converge runs under sudo, where root reads the
        staged copy in the user's cache without it ever being moved into a root-owned
        directory. The bytes are the source's own (ADR-020 D-12) — never re-fetched from
        a vendor — and `_discard_staged_key` removes the copy afterwards.
        """
        if not item.gpg_verify or item.key_digest is None:
            return None

        local_path = _source_keyring_path(item)
        if not local_path.is_file():
            raise ConvergeItemFailed(
                f"signing key for {item.label()} is missing on the source at {local_path} "
                "(it existed when the plan was captured); refusing to provision a remote whose key cannot travel"
            )

        home = await self._target_home_dir()
        staging_dir = f"{home}/.cache/pc-switcher/flatpak-staging"
        mkdir = await self.target.run_command(
            f"mkdir --parents {shlex.quote(staging_dir)}",
            login_shell=False,
            mutates="create the flatpak signing-key staging directory",
        )
        if not mkdir.success:
            raise ConvergeItemFailed(f"failed to create {staging_dir} on the target: {mkdir.stderr.strip()}")

        staged = f"{staging_dir}/{diff.item_id.replace(':', '_').replace('/', '_')}.gpg"
        await self.target.send_file(
            local_path,
            staged,
            mutates=f"stage the signing key for {item.label()} into the target's cache",
        )
        return staged

    async def _discard_staged_key(self, staged_key: str | None) -> None:
        """Remove a staged key copy once flatpak has imported it — the same `finally`
        cleanup apt's staging does, so a failed remote-add never leaves transferred key
        material sitting in the target's cache.
        """
        if staged_key is None:
            return
        await self.target.run_command(
            f"rm --force {shlex.quote(staged_key)}",
            login_shell=False,
            mutates="discard the staged flatpak signing key",
        )

    async def _target_home_dir(self) -> str:
        """The target user's home directory, resolved once per run via `echo $HOME` and
        cached (`AptSyncJob._target_home_dir`'s established pattern) — every staged key
        needs the same absolute path.
        """
        if self._target_home is None:
            result = await self.target.run_command("echo $HOME", login_shell=False)
            self._target_home = result.stdout.strip()
        return self._target_home

    def _remote_ready_on_target(self, scope: str, origin: str) -> bool:
        """Whether `origin` is usable as a ref's remote in `scope`: already present on
        the target per the plan-time query, or successfully added earlier in THIS run
        (T-02-24's guard — see `converge()`'s module docstring).
        """
        remote_id = f"flatpak:remote:{scope}:{origin}"
        if remote_id in self._target_remotes_by_id:
            return True
        return (scope, origin) in self._converged_remote_scope_names

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
                "configured flatpak remotes (per scope)",
                "flatpak mask patterns (per scope)",
            ],
            mechanism="flatpak install/uninstall/remote-add/mask per item, after review",
        )
