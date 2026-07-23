"""`flatpak_sync`: flatpak ref/remote convergence with scope as identity (D-06, D-14,
D-29, ADR-020).

Scope (user vs. system) is part of a flatpak item's identity, not just a field on it:
this project's own reference machine has several runtimes installed in BOTH scopes
under the same application id, and `FlatpakItem.item_id`/`FlatpakRemoteItem.item_id`
already fold scope into the identity string (`package_items.py`). That is what makes
"same application, different scope" fall out of the generic source-vs-target diff as
two independent items with no special-casing in this module — a ref present as
`user` on the source and `system` on the target produces one install diff and one
removal diff, never a single in-place change, because they are simply two different
`item_id`s. Normalising that scope split across a machine's own two installations is
explicitly out of scope (deferred, CONTEXT.md): it is a change to the machines, not a
sync feature, and this job reports the split exactly as found.

`flatpak install` refuses outright when the remote it names is not yet configured in
the scope being installed into (D-14), so remotes are captured and diffed as their
own item class (`FLATPAK_REMOTE`) and this job's own ordering stage (mirroring
`apt_sync`'s key-before-source sort) places every remote diff ahead of every ref diff
before the plan's `diffs` tuple ever reaches `apply()`'s per-diff loop. Before
converging a ref, its origin remote is checked against what already exists on the
target in that scope OR what this SAME run has already successfully added — a ref
whose remote is missing from both is skipped with a per-item failure naming the
remote, rather than issuing an install flatpak will reject (T-02-24).

The flatpak OSTree store stays authoritative for its own state (D-01): this job never
touches `/var/lib/flatpak` or `~/.local/share/flatpak` directly, only shells out to
`flatpak` itself. `flatpak_sync_exclude_paths()` exports `~/.local/share/flatpak` so
`folder_sync` stops mirroring the store this job owns (D-29, ADR-018) — but NOT
`~/.var/app`, which is per-application USER DATA that stays folder_sync's territory;
D-17's job-before-folder_sync ordering exists precisely so `flatpak install` creates
the store first and folder_sync's data lands on top of it, never the reverse.

`FlatpakSyncJob` subclasses `PackageSyncJob` but overrides `plan()` rather than
inheriting the base implementation, for the same reason `SnapSyncJob` does (see that
module's docstring and 02-08's own deviation note): `PackageSyncJob.diff_items`/
`_diff_apt_packages` is apt-package-shaped — one item class, `MISSING_ON_TARGET`/
`EXTRA_ON_TARGET`/`VERSION_MISMATCH` only, no notion of a second item class that must
converge ahead of the first. This job diffs and converges TWO item classes
(`FLATPAK_REF`, `FLATPAK_REMOTE`) with an ordering dependency between them, which the
shared dispatch has no way to express. `plan()` here reuses every manager-agnostic
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
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal, override

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.package_items import (
    DiffAction,
    DiffClass,
    FlatpakItem,
    FlatpakRemoteItem,
    ItemClass,
    ItemDiff,
    build_version_mismatch_detail,
)
from pcswitcher.jobs.package_state import DecisionFile, filter_inert
from pcswitcher.jobs.package_sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, ValidationError
from pcswitcher.sudoers import passwordless_sudo_hint

__all__ = ["FlatpakSyncJob", "flatpak_sync_exclude_paths"]

# `flatpak list --app` is run with an explicit --columns flag naming exactly these
# four fields in this order (RESEARCH: verified live against Flatpak 1.14.6) — unlike
# `snap list --all`, the invocation itself names its columns, so the output has no
# header row and is parsed by fixed tab-separated position.
_FLATPAK_LIST_CMD = "flatpak list --app --columns=application,version,origin,installation"

# Same reasoning for `flatpak remotes`, but flatpak tracks remotes PER INSTALLATION —
# even a byte-identical `flathub` URL is two separate configuration entries — so this
# is run once per scope rather than once combined (module docstring, D-14).
_FLATPAK_REMOTES_CMD_TEMPLATE = "flatpak remotes {flag} --columns=name,url"

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


def _split_flatpak_item_id(item_id: str, expected_kind: Literal["ref", "remote"]) -> tuple[str, str]:
    """`(scope, name)` from a `flatpak:<kind>:<scope>:<name>` item id (package_items.py).

    `name` is the application id for a ref, the remote name for a remote — both are
    dotted/alnum tokens with no `:` of their own (RESEARCH Standard Stack), so a fixed
    3-colon split is exact rather than a heuristic. This is a legitimate use of a
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
        if len(fields) != 4:
            continue
        application, version, origin, installation = fields
        scope: Literal["user", "system"]
        if installation == "user":
            scope = "user"
        elif installation == "system":
            scope = "system"
        else:
            continue
        items.append(FlatpakItem(application=application, version=version, origin=origin, scope=scope))
    return items


def _parse_flatpak_remotes(output: str, scope: Literal["user", "system"]) -> list[FlatpakRemoteItem]:
    """Parse one scope's `flatpak remotes --columns=name,url` output.

    `scope` is a parameter, not a parsed column: unlike `flatpak list`, this command
    has no scope column of its own — the caller already knows which scope it asked
    about, because it chose the `--user`/`--system` flag (module docstring).
    """
    items: list[FlatpakRemoteItem] = []
    for line in _lines(output):
        fields = line.split("\t")
        if len(fields) != 2:
            continue
        name, url = fields
        items.append(FlatpakRemoteItem(name=name, url=url, scope=scope))
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
    never force-installed/removed to converge it. Only reachable for two items
    sharing the same `item_id`, i.e. the same application AND the same scope: a scope
    difference is never this diff (module docstring — it is two distinct items).
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


def _remove_remote_diff(item: FlatpakRemoteItem) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.FLATPAK_REMOTE,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REMOVE,
        item_id=item.item_id,
        label=item.label(),
        detail=None,
    )


def _diff_flatpak_remotes(
    source_items: Sequence[FlatpakRemoteItem], target_items: Sequence[FlatpakRemoteItem]
) -> list[ItemDiff]:
    """One diff per remote `item_id` (name + scope) present on either side.

    A remote present on both sides with the SAME name and scope is never diffed by
    URL here (T-02-22's own review-item requirement is about ADDING a remote, not
    about detecting a URL edit on an existing one — out of this plan's stated
    behavior set; D-11 names remotes as three-way-decision items, not as a field the
    diff engine compares byte-for-byte the way apt keys/config do).
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
            diffs.append(_install_remote_diff(source_item))
        elif target_item is not None and source_item is None:
            diffs.append(_remove_remote_diff(target_item))
        # else: present on both -> no diff.

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

    @override
    async def capture_source_items(self) -> Sequence[FlatpakItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """`flatpak list --app` on the source (D-06).

        This job overrides `plan()` and never routes through `PackageSyncJob.
        diff_items`'s apt-package-shaped dispatch (module docstring), so widening this
        hook's item type here is safe: no code holding a `PackageSyncJob`-typed
        reference ever calls it expecting an `AptPackageItem` back — the same
        justification `SnapSyncJob.capture_source_items` documents.
        """
        result = await self.source.run_command(_FLATPAK_LIST_CMD)
        return _parse_flatpak_list(result.stdout)

    @override
    async def query_target_items(self) -> Sequence[FlatpakItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The target's own `flatpak list --app` (same reasoning as `capture_source_items`)."""
        result = await self.target.run_command(_FLATPAK_LIST_CMD, login_shell=False)
        return _parse_flatpak_list(result.stdout)

    async def _capture_source_remotes(self, scope: Literal["user", "system"]) -> list[FlatpakRemoteItem]:
        cmd = _FLATPAK_REMOTES_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.source.run_command(cmd)
        return _parse_flatpak_remotes(result.stdout, scope)

    async def _query_target_remotes(self, scope: Literal["user", "system"]) -> list[FlatpakRemoteItem]:
        cmd = _FLATPAK_REMOTES_CMD_TEMPLATE.format(flag=_scope_flag(scope))
        result = await self.target.run_command(cmd, login_shell=False)
        return _parse_flatpak_remotes(result.stdout, scope)

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

    @override
    async def plan(self) -> PackagePlan:
        """Load decision files -> capture -> query -> diff -> build review groups.

        Read-only: only `flatpak list`/`flatpak remotes` (both machines, both scopes)
        and a decision-file `cat` run here — no `flatpak install`/`uninstall`/
        `remote-add`/`remote-delete` before this returns. Caches the filtered
        source/target items by id for `converge()` (see `__init__`).

        Remote diffs are placed before ref diffs in the returned `diffs` tuple — this
        job's own ordering stage, mirroring `apt_sync`'s key-before-source sort, for
        the same class of reason: `flatpak install` fails when its remote is not yet
        configured in that scope (D-14), so the base `apply()` loop (which converges
        `plan.diffs` in order) must see every remote diff first.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        target_decisions = await DecisionFile(self.manager_id, self.target).load()

        source_refs = await filter_inert(await self.capture_source_items(), source_decisions)
        target_refs = await filter_inert(await self.query_target_items(), target_decisions)
        source_remotes = await filter_inert(await self._capture_all_source_remotes(), source_decisions)
        target_remotes = await filter_inert(await self._query_all_target_remotes(), target_decisions)

        self._source_refs_by_id = {item.item_id: item for item in source_refs}
        self._source_remotes_by_id = {item.item_id: item for item in source_remotes}
        self._target_remotes_by_id = {item.item_id: item for item in target_remotes}

        remote_diffs = _diff_flatpak_remotes(source_remotes, target_remotes)
        ref_diffs = _diff_flatpak_refs(source_refs, target_refs)
        diffs = (*remote_diffs, *ref_diffs)

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
        raise ConvergeItemFailed(
            f"FlatpakSyncJob.converge: unsupported item class {diff.item_class.value!r} for {diff.label}"
        )

    async def _converge_remote(self, diff: ItemDiff) -> CommandResult:
        scope, name = _split_flatpak_item_id(diff.item_id, "remote")
        scope_flag = _scope_flag(scope)
        sudo = _sudo_prefix(scope)

        if diff.action == DiffAction.INSTALL:
            source_item = self._source_remotes_by_id.get(diff.item_id)
            if source_item is None:
                raise ConvergeItemFailed(
                    f"no captured source remote for {diff.label} (item_id={diff.item_id!r}); "
                    "was plan() run before converge()?"
                )
            cmd = (
                f"{sudo}flatpak remote-add --if-not-exists {scope_flag} "
                f"{shlex.quote(name)} {shlex.quote(source_item.url)}"
            )
            result = await self.target.run_command(cmd, login_shell=False)
            if result.success:
                self._converged_remote_scope_names.add((scope, name))
            return result

        if diff.action == DiffAction.REMOVE:
            cmd = f"{sudo}flatpak remote-delete {scope_flag} {shlex.quote(name)}"
            return await self.target.run_command(cmd, login_shell=False)

        raise ConvergeItemFailed(
            f"FlatpakSyncJob.converge: unsupported action {diff.action.value!r} for a flatpak remote ({diff.label})"
        )

    async def _converge_ref(self, diff: ItemDiff) -> CommandResult:
        scope, application = _split_flatpak_item_id(diff.item_id, "ref")
        scope_flag = _scope_flag(scope)
        sudo = _sudo_prefix(scope)

        if diff.action == DiffAction.REMOVE:
            cmd = f"{sudo}flatpak uninstall -y {scope_flag} {shlex.quote(application)}"
            return await self.target.run_command(cmd, login_shell=False)

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
                    f"install of {application} refused: origin remote {source_item.origin!r} ({scope}) is "
                    "neither already configured on the target nor among this run's successfully-added "
                    "remotes (D-14)"
                )
            cmd = f"{sudo}flatpak install -y {scope_flag} {shlex.quote(source_item.origin)} {shlex.quote(application)}"
            return await self.target.run_command(cmd, login_shell=False)

        raise ConvergeItemFailed(
            f"FlatpakSyncJob.converge: unsupported action {diff.action.value!r} for a flatpak ref ({diff.label}) "
            "— version mismatches are report_only per D-04 and never reach converge()"
        )

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
        """Whether ANY system-scope ref or remote exists on either machine — the gate
        for `validate()`'s sudo check (T-02-23, ASVS V4): user-scope flatpak
        operations need no root at all, so this job never asks for a privilege it
        will not use.
        """
        if any(item.scope == "system" for item in await self.capture_source_items()):
            return True
        if any(item.scope == "system" for item in await self.query_target_items()):
            return True
        if await self._capture_source_remotes("system"):
            return True
        return bool(await self._query_target_remotes("system"))

    @override
    async def validate(self) -> list[ValidationError]:
        """`flatpak --version` on both ends — a missing binary is a reported
        validation error naming flatpak's absence (it ships in no default Ubuntu
        24.04 install and may genuinely be absent), never an exception. `sudo -n
        true` on the target only when a system-scope ref or remote actually exists on
        either machine.

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
            sudo_check = await self.target.run_command("sudo -n true", login_shell=False)
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
        """Name this job's destructive first-sync scope (ADR-015): flatpak refs and remotes."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["installed flatpak refs (per user/system scope)", "configured flatpak remotes (per scope)"],
            mechanism="flatpak install/uninstall/remote-add per item, after review",
        )
