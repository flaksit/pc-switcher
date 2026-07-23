"""`snap_sync`: snap name/channel/revision convergence through snapd's own verbs
(D-06, D-14, D-29, ADR-020).

WARNING — `snap refresh --hold` with NO snap name is a MUTATING command: called that
way it silently sets an INDEFINITE GLOBAL hold on auto-refresh for every snap on the
machine (RESEARCH Pitfall 1 — discovered live against a real machine during Phase 2
research, and only undone with a manual `snap refresh --unhold`). This module never
calls it. Hold state is inspected only through the read-only `snap get system
refresh.hold` (`validate()`, informational only, never acted on). Convergence uses
only `snap install --revision=<N>` and `snap refresh --revision=<N>`, which land the
target on the source's exact revision without touching the standing auto-refresh
policy at all — the mechanism D-06 requires: both machines converge on the same
revision, neither stops updating.

The snapd store stays authoritative for its own state (D-01): this job never touches
`/var/lib/snapd` directly, only shells out to `snap` itself, the same shape
`apt_sync` uses for `apt`/`dpkg`.

`SnapSyncJob` subclasses `PackageSyncJob` but overrides `plan()` rather than
inheriting the base implementation: `PackageSyncJob.diff_items`/`_diff_apt_packages`
is apt-package-shaped (a version difference is `REPORT_ONLY` per D-04, and it has no
notion of a tracking channel), while D-06 wants a snap's revision AND channel
differences to actively converge (`CHANGE`). `plan()` here reuses the same shared
building blocks apt_sync's own diff-taxonomy plan relies on — `DecisionFile`/
`filter_inert` (D-08's machine-local skip-always filtering) and
`PackageSyncJob._build_review_groups` (D-24's action-grouped review) — so the only
genuinely snap-specific code is capture, diff and converge. `accept_review()`,
`apply()` and `execute()` are inherited unchanged; this job implements no review of
its own — the coordinator (plan 02-03) reviews every enabled manager at once.

Revision AND channel differences share one `DiffAction.CHANGE` diff per snap, tagged
`ItemClass.SNAP` in both cases (never `ItemClass.SNAP_CHANNEL`) even though a
same-revision retrack is conceptually "just" a channel change: `PackageSyncJob.
_build_review_groups` derives one action_label verb per REVIEW GROUP from its first
entry's `item_class` (its own docstring flags this as unhandled for "a manager mixing
item classes under one action"), so tagging some CHANGE diffs `SNAP` and others
`SNAP_CHANNEL` would risk one of the two kinds getting the other's verb whenever both
occur in the same run. Using one item_class for every CHANGE diff avoids that
mislabeling entirely; the diff's `detail` text still names both revisions or both
channels, satisfying D-07's "review names the concrete action" without depending on a
shared-core behavior this plan does not own.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, override

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.package_items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
    SnapItem,
    build_version_mismatch_detail,
)
from pcswitcher.jobs.package_state import DecisionFile, filter_inert
from pcswitcher.jobs.package_sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, LogLevel, ValidationError

__all__ = ["SnapSyncJob", "snap_sync_exclude_paths"]

# `SnapItem.item_id` is always this prefix + the snap name (package_items.py).
_SNAP_ID_PREFIX = "snap:"

# Directory names under ~/snap/<app>/ that are NOT per-revision data snapd hands this
# job (D-29): `common` is revision-independent user data folder_sync must keep
# mirroring, `current` is a symlink snapd itself maintains to the active revision.
_NON_REVISION_DIR_NAMES = frozenset({"common", "current"})


def _snap_name(item_id: str) -> str:
    if not item_id.startswith(_SNAP_ID_PREFIX):
        raise ValueError(f"Not a snap item id: {item_id!r}")
    return item_id.removeprefix(_SNAP_ID_PREFIX)


def _parse_snap_list(output: str) -> list[SnapItem]:
    """Parse `snap list --all` by HEADER column names, never fixed offsets or assumed
    order (RESEARCH Open Question 2): a future snapd column reorder must yield correct
    values, never a silently wrong revision driving a wrong `--revision` install.

    Skips a disabled older-revision line (`Notes` names `disabled`) for a snap that
    also has an active line — only the active revision becomes the item. Output shaped
    like "No snaps are installed yet." (no recognizable header) degrades to an empty
    list rather than raising: a snap-free machine is a valid, if rare, state.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return []

    header = lines[0].split()
    try:
        name_idx = header.index("Name")
        rev_idx = header.index("Rev")
        tracking_idx = header.index("Tracking")
        notes_idx = header.index("Notes")
    except ValueError:
        return []

    max_idx = max(name_idx, rev_idx, tracking_idx, notes_idx)
    items: list[SnapItem] = []
    for line in lines[1:]:
        fields = line.split()
        if len(fields) <= max_idx:
            continue
        if "disabled" in fields[notes_idx].split(","):
            continue
        items.append(SnapItem(name=fields[name_idx], channel=fields[tracking_idx], revision=fields[rev_idx]))
    return items


def _install_diff(item: SnapItem) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.SNAP,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.INSTALL,
        item_id=item.item_id,
        label=item.label(),
        detail=None,
    )


def _remove_diff(item: SnapItem) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.SNAP,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REMOVE,
        item_id=item.item_id,
        label=item.label(),
        detail=None,
    )


def _change_diff(item_id: str, source_item: SnapItem, target_item: SnapItem) -> ItemDiff:
    """Present on both with a different revision and/or channel (D-06: unlike apt
    package versions, D-04, both actively converge — never `REPORT_ONLY`). `detail`
    names revisions when the revision differs (the more consequential fact); otherwise
    it is a same-revision retrack and names channels instead.
    """
    if source_item.revision != target_item.revision:
        detail = build_version_mismatch_detail(source_item.revision, target_item.revision)
    else:
        detail = build_version_mismatch_detail(source_item.channel, target_item.channel)
    return ItemDiff(
        item_class=ItemClass.SNAP,
        diff_class=DiffClass.VERSION_MISMATCH,
        action=DiffAction.CHANGE,
        item_id=item_id,
        label=target_item.label(),
        detail=detail,
    )


def _diff_snap_items(source_items: Sequence[SnapItem], target_items: Sequence[SnapItem]) -> list[ItemDiff]:
    """One diff per snap name present on either side, source-then-target order — same
    shape as `PackageSyncJob._diff_apt_packages`, but with D-06's own convergence rule.
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
            diffs.append(_install_diff(source_item))
        elif target_item is not None and source_item is None:
            diffs.append(_remove_diff(target_item))
        elif (
            source_item is not None
            and target_item is not None
            and (source_item.revision != target_item.revision or source_item.channel != target_item.channel)
        ):
            diffs.append(_change_diff(item_id, source_item, target_item))
        # else: present on both, identical revision and channel -> no diff.

    return diffs


def snap_sync_exclude_paths() -> list[Path]:
    """Absolute `~/snap/<app>/<revision>` directories this job owns (D-29), resolved
    against `Path.home()` at call time exactly like `vscode_state_exclude_paths()` —
    unlike VS Code's fixed relpath list, the revision set is dynamic, so it is
    enumerated from the filesystem rather than hardcoded.

    Deliberately excludes `~/snap/<app>/common` (revision-independent user data
    folder_sync must keep mirroring) and `~/snap/<app>/current` (a symlink snapd
    itself maintains) — the whole reason this export is not simply `~/snap`.
    """
    snap_root = Path.home() / "snap"
    if not snap_root.is_dir():
        return []

    paths: list[Path] = []
    for app_dir in sorted(snap_root.iterdir()):
        if not app_dir.is_dir():
            continue
        for entry in sorted(app_dir.iterdir()):
            if entry.name in _NON_REVISION_DIR_NAMES or not entry.is_dir():
                continue
            paths.append(entry)
    return paths


class SnapSyncJob(PackageSyncJob):
    """Converge snap name/channel/revision after the coordinator's batched review.

    Overrides `plan()` with a snap-specific capture -> diff -> review-group pipeline
    (module docstring explains why the inherited apt-package-shaped one cannot be
    reused); `accept_review()`, `apply()` and `execute()` are inherited unchanged.
    """

    name: ClassVar[str] = "snap_sync"
    manager_id: ClassVar[str] = "snap"

    # No configurable properties: mirrors AptSyncJob's empty schema — only the enable
    # flag in sync_jobs is needed for this slice.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        # Populated by plan()'s own capture/query step (post filter_inert) and
        # consulted by converge(): the base pipeline only ever hands converge() an
        # ItemDiff, whose item_id ("snap:<name>") carries no revision/channel data of
        # its own — unlike an apt package name, `snap install --revision=N` needs the
        # literal N, which has to come from somewhere other than the diff itself.
        self._source_items_by_id: dict[str, SnapItem] = {}
        self._target_items_by_id: dict[str, SnapItem] = {}

    @override
    async def capture_source_items(self) -> Sequence[SnapItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """`snap list --all` on the source (D-06).

        This job overrides `plan()` and never routes through `PackageSyncJob.
        diff_items`'s apt-package-shaped dispatch (module docstring), so widening this
        hook's item type here is safe: no code holding a `PackageSyncJob`-typed
        reference ever calls it expecting an `AptPackageItem` back.
        """
        result = await self.source.run_command("snap list --all")
        return _parse_snap_list(result.stdout)

    @override
    async def query_target_items(self) -> Sequence[SnapItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The target's own `snap list --all` (same reasoning as `capture_source_items`)."""
        result = await self.target.run_command("snap list --all", login_shell=False)
        return _parse_snap_list(result.stdout)

    @override
    async def plan(self) -> PackagePlan:
        """Load decision files -> capture -> query -> diff -> build review groups.

        Read-only: only `snap list --all` (both machines) and a decision-file `cat`
        run here — no `snap install`/`refresh`/`switch`/`remove` before this returns.
        Caches the filtered source/target items by id for `converge()` (see
        `__init__`), since `ItemDiff.item_id` alone carries no revision/channel data.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        target_decisions = await DecisionFile(self.manager_id, self.target).load()

        source_items = await filter_inert(await self.capture_source_items(), source_decisions)
        target_items = await filter_inert(await self.query_target_items(), target_decisions)

        self._source_items_by_id = {item.item_id: item for item in source_items}
        self._target_items_by_id = {item.item_id: item for item in target_items}

        diffs = tuple(_diff_snap_items(source_items, target_items))
        groups = self._build_review_groups(diffs)
        return PackagePlan(manager=self.manager_id, diffs=diffs, groups=groups)

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Install/refresh at the source's explicit revision, switch channel only when
        it differs, or remove (never purge) — the only D-06-safe verbs (module
        docstring). One snap per invocation (D-27) so a single bad snap cannot fail the
        whole batch.
        """
        if diff.action == DiffAction.REMOVE:
            return await self._converge_remove(diff)

        source_item = self._source_items_by_id.get(diff.item_id)
        if source_item is None:
            raise ConvergeItemFailed(
                f"no captured source snap for {diff.label} (item_id={diff.item_id!r}); "
                "was plan() run before converge()?"
            )

        if diff.action == DiffAction.INSTALL:
            return await self._converge_install(source_item)
        if diff.action == DiffAction.CHANGE:
            target_item = self._target_items_by_id.get(diff.item_id)
            return await self._converge_change(source_item, target_item)

        raise ConvergeItemFailed(f"SnapSyncJob.converge: unsupported action {diff.action.value!r} for {diff.label}")

    async def _converge_install(self, source_item: SnapItem) -> CommandResult:
        """`snap install --revision=<N>` lands the exact revision without ever
        touching a hold (D-06); the channel switch always follows so the target
        tracks the same channel as the source. There is no cheap way to learn
        "snapd's default channel for a not-yet-installed snap" from `snap list --all`
        alone (only installed snaps appear in it), and re-running `switch` to a
        channel the install already landed on is a harmless no-op, so always
        switching is simpler and no less correct than conditioning on that unknown.
        """
        name = shlex.quote(source_item.name)
        revision = shlex.quote(source_item.revision)
        install_result = await self.target.run_command(
            f"sudo snap install --revision={revision} {name}", login_shell=False
        )
        if not install_result.success:
            return install_result
        return await self._switch_channel(source_item)

    async def _converge_change(self, source_item: SnapItem, target_item: SnapItem | None) -> CommandResult:
        """`snap refresh --revision=<N>` when the revision differs, plus the channel
        switch when the channel also differs (or is the only thing that differs) —
        `converge()` only reaches here for a diff `_diff_snap_items` built because at
        least one of the two was true.
        """
        revision_differs = target_item is None or source_item.revision != target_item.revision
        channel_differs = target_item is None or source_item.channel != target_item.channel

        result: CommandResult | None = None
        if revision_differs:
            name = shlex.quote(source_item.name)
            revision = shlex.quote(source_item.revision)
            result = await self.target.run_command(
                f"sudo snap refresh --revision={revision} {name}", login_shell=False
            )
            if not result.success:
                return result

        if channel_differs:
            result = await self._switch_channel(source_item)

        assert result is not None, "converge() only calls this for a diff where something differed at plan time"
        return result

    async def _switch_channel(self, source_item: SnapItem) -> CommandResult:
        name = shlex.quote(source_item.name)
        channel = shlex.quote(source_item.channel)
        return await self.target.run_command(f"sudo snap switch --channel={channel} {name}", login_shell=False)

    async def _converge_remove(self, diff: ItemDiff) -> CommandResult:
        """`snap remove`, never `--purge`: purge discards snapd's own pre-removal
        snapshot, which is the user's only recovery path if the removal was a mistake.
        """
        name = shlex.quote(_snap_name(diff.item_id))
        return await self.target.run_command(f"sudo snap remove {name}", login_shell=False)

    @override
    async def validate(self) -> list[ValidationError]:
        """`snap version` on both ends, `sudo -n true` on the target, and a read-only
        informational hold check on both ends (never acted on — module docstring).
        Sequential checks appending to `errors`, matching `AptSyncJob.validate()`'s shape.
        """
        errors: list[ValidationError] = []

        source_check = await self.source.run_command("snap version")
        if not source_check.success:
            errors.append(self._validation_error(Host.SOURCE, "snap is not available on source"))

        target_check = await self.target.run_command("snap version", login_shell=False)
        if not target_check.success:
            errors.append(self._validation_error(Host.TARGET, "snap is not available on target"))

        sudo_check = await self.target.run_command("sudo -n true", login_shell=False)
        if not sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "passwordless sudo is not available on target (required for snap install/refresh/remove)",
                )
            )

        # Read-only informational context (RESEARCH Pitfall 1): NEVER `snap refresh
        # --hold` with no arguments to check this — that form mutates. A pre-existing
        # hold is not itself an error here; it only explains why a revision might not
        # converge, so it is logged, never appended to `errors`.
        source_hold = await self.source.run_command("snap get system refresh.hold")
        self._log(Host.SOURCE, LogLevel.FULL, f"source snap refresh.hold: {source_hold.stdout.strip() or '(none)'}")
        target_hold = await self.target.run_command("snap get system refresh.hold", login_shell=False)
        self._log(Host.TARGET, LogLevel.FULL, f"target snap refresh.hold: {target_hold.stdout.strip() or '(none)'}")

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): installed snaps."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["installed snaps (name, channel, revision)"],
            mechanism="snap install/refresh/remove per item, after review",
        )
