"""`snap_sync`: snap name/channel/revision convergence through snapd's own verbs
(D-06, D-14, D-29, ADR-020).

WARNING — `snap refresh --hold` with NO snap name is a MUTATING command: called that
way it silently sets an INDEFINITE GLOBAL hold on auto-refresh for every snap on the
machine (RESEARCH Pitfall 1 — discovered live against a real machine during Phase 2
research, and only undone with a manual `snap refresh --unhold`). This module never
calls it. Hold state is inspected only through the read-only `sudo snap get system
refresh.hold` (`validate()`, informational only, never acted on; sudo because snapd
admin-gates reading snap config, not because the read changes anything). Convergence uses
only `snap install --revision=<N>` and `snap refresh --revision=<N>`, which land the
target on the source's exact revision without touching the standing auto-refresh
policy at all — the mechanism D-06 requires: both machines converge on the same
revision, neither stops updating.

The snapd store stays authoritative for its own state (D-01): this job never touches
`/var/lib/snapd` directly, only shells out to `snap` itself, the same shape
`apt_sync` uses for `apt`/`dpkg`.

Snap has no repository or key decision to replicate, and gets no screen for one (D-42).
One store serves the device, and name -> publisher is pinned store-side by a
canonical-signed `snap-declaration` snapd validates itself, so one name resolves to one
snap-id resolves to one publisher and there is no second `firefox` for the target to
install by accident. Keys are snapd's own, not the user's. A brand store or a store
proxy could in principle make two machines draw from different stores, but both are
device-provisioning facts rather than per-snap facts and neither is replicable, so snap
is treated as having one store and no store-identity check exists. The provenance
variable that remains is which revision of that one snap is installed and which channel
it tracks, and D-06 converges both.

`SnapSyncJob` subclasses `PackageSyncJob` and implements the abstract `plan()`: what a
diff even IS differs per manager, so the base class holds no diff to inherit. apt's own
diff lives in `apt_sync` and is apt-package-shaped (a version difference is `REPORT_ONLY`
per D-04, with no notion of a tracking channel), while D-06 wants a snap's revision AND
channel differences to actively converge (`CHANGE`). `plan()` here reuses the
manager-agnostic building blocks the shared core does provide — `DecisionFile`/
`filter_inert` (D-08's machine-local skip-always filtering) and
`PackageSyncJob._build_review_groups` (D-24's action-grouped review) — so the only
genuinely snap-specific code is capture, diff and converge. `accept_review()`, `apply()`
and `execute()` are inherited unchanged, and `execute()` is where this job's own single
review happens, before its own first mutating command: there is no coordinator and no
review spanning two managers (D-15, D-24).

Revision AND channel differences share one `DiffAction.CHANGE` diff per snap, tagged
`ItemClass.SNAP` in both cases (never `ItemClass.SNAP_CHANNEL`) even though a
same-revision retrack is conceptually "just" a channel change: one change can move both
at once, so no per-facet class can name it, and `PackageSyncJob._build_review_groups`
keys its groups on `(action, item_class)` — tagging some CHANGE diffs `SNAP_CHANNEL`
would put one snap's convergence on one screen or another according to which facets
happened to differ, for a decision that is the same either way. The diff's `detail` names
every value that differs, satisfying D-07's "review names the concrete action".
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, override

from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
    Machines,
)
from pcswitcher.jobs.packages.probes import require_answer
from pcswitcher.jobs.packages.review import Decision
from pcswitcher.jobs.packages.snap_listing import SnapItem, parse_snap_list, partition_sideloaded
from pcswitcher.jobs.packages.state import DecisionEntry, filter_inert, marks_on_either
from pcswitcher.jobs.packages.sync_core import (
    ConvergeItemDeclined,
    ConvergeItemFailed,
    PackagePlan,
    PackageSyncJob,
)
from pcswitcher.models import CommandResult, FirstSyncScope, Host, LogLevel, ValidationError
from pcswitcher.sudoers import passwordless_sudo_hint

if TYPE_CHECKING:
    from pcswitcher.executor import RemoteExecutor

__all__ = ["SnapSyncJob", "snap_sync_exclude_paths", "target_snap_revisions"]

# `SnapItem.item_id` is always this prefix + the snap name (below).
_SNAP_ID_PREFIX = "snap:"

# A per-snap hold membership item's item_id (#208, D1): this prefix + the snap name.
# NOTE this also starts with `_SNAP_ID_PREFIX`, so converge() must test this longer
# prefix FIRST, before the presence-diff (install/change/remove) dispatch, or a
# `snap:hold:<name>` REMOVE would be misrouted into `_converge_remove` as a `snap:`
# item (D4 — route by prefix, never by action).
_SNAP_HOLD_ID_PREFIX = "snap:hold:"

# Binaries this job runs under sudo, quoted back to the user when the passwordless-sudo
# check fails. A lower bound on what must be permitted, not an exact scope (ADR-013).
_TARGET_SUDO_COMMANDS = ("/usr/bin/snap",)

# Directory names under ~/snap/<app>/ that are never a per-revision data dir (D-29):
# `common` is revision-independent user data folder_sync must keep mirroring, `current`
# is the symlink snapd maintains to the active revision. Both are always kept for the
# mirror. Of the per-revision dirs, folder_sync mirrors the ONE the app's `current`
# resolves to AND the target is itself active at (so the active revision's per-user app
# data travels — decision 3, issue #118), and excludes every other one, since the target's
# snapd never installed those. See `snap_sync_exclude_paths`.
_NON_REVISION_DIR_NAMES = frozenset({"common", "current"})


def _snap_name(item_id: str) -> str:
    if not item_id.startswith(_SNAP_ID_PREFIX):
        raise ValueError(f"Not a snap item id: {item_id!r}")
    return item_id.removeprefix(_SNAP_ID_PREFIX)


def _confinement_flags(item: SnapItem) -> str:
    """The `--classic`/`--devmode` confirmation flag `snap install`/`snap refresh` need
    for this item's confinement, as a leading-space command fragment ("" for a strictly
    confined snap).

    At most ONE flag is ever emitted, classic winning. The two mean different, mutually
    incompatible things — `--classic` confirms a revision the store published with classic
    confinement, `--devmode` (per `snap install --help`) "relax[es] confinement for strict
    snaps" — so a snap is one or the other, never both. The `snap` CLI only rejects one
    mode-flag pair outright (`cannot use devmode and jailmode flags together`, verified in
    the 2.76.1 binary); a nonsensical `--classic --devmode` pair would be accepted
    silently, which is exactly why this never emits it.

    Passing `--classic` where it is not needed is safe: `snap` warns `flag --classic
    ignored for strictly confined snap %s` rather than failing.
    """
    if item.classic:
        return " --classic"
    if item.devmode:
        return " --devmode"
    return ""


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


def _change_diff(item_id: str, source_item: SnapItem, target_item: SnapItem, machines: Machines) -> ItemDiff:
    """Present on both with a different revision and/or channel (D-06: unlike apt
    package versions, D-04, both actively converge — never `REPORT_ONLY`).

    `detail` names EVERY value that differs (`PKG-FR-SNAP-CASES`): one change can move
    both the revision and the channel, and naming the revision alone left the retrack out
    of the only line the user reads before approving it.

    Worded as the effect on the target rather than as the two machines' states side by side
    (`PKG-FR-NO-MARK-ON-SNAP-REVISION`): what the user is deciding is whether their machine's
    revision is overwritten, and "atlas has 20, nomad has 15" leaves them to work out which
    of the two survives. Naming each facet is still required — "20" says nothing on its own
    about whether it is a revision or a channel — and so is naming where the new value comes
    from, since the line otherwise stated a revision the target is moved to without saying
    whose it is.
    """
    detail = "; ".join(
        f"overwrites {facet} {target_value} on {machines.target} with {facet} {source_value} from {machines.source}"
        for facet, source_value, target_value in (
            ("revision", source_item.revision, target_item.revision),
            ("channel", source_item.channel, target_item.channel),
        )
        if source_value != target_value
    )
    return ItemDiff(
        item_class=ItemClass.SNAP,
        diff_class=DiffClass.VERSION_MISMATCH,
        action=DiffAction.CHANGE,
        item_id=item_id,
        label=target_item.label(),
        detail=detail,
    )


def _hold_diff(name: str, *, in_source: bool) -> ItemDiff:
    """One `snap:hold:<name>` membership diff (#208, D2, the snap analog of
    `AptHoldItem`): `in_source` (source-held, target-not) -> INSTALL (hold on target);
    otherwise (target-held, source-not) -> REMOVE (unhold on target). The identity
    (`snap:hold:<name>`) is DISTINCT from the snap item's (`snap:<name>`) so a snap and
    its hold are two separate review items.
    """
    return ItemDiff(
        item_class=ItemClass.SNAP_HOLD,
        diff_class=DiffClass.MISSING_ON_TARGET if in_source else DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.INSTALL if in_source else DiffAction.REMOVE,
        item_id=f"{_SNAP_HOLD_ID_PREFIX}{name}",
        label=f"{name} (hold)",
        detail=None,
    )


def _diff_snap_holds(source_items: Sequence[SnapItem], target_items: Sequence[SnapItem]) -> list[ItemDiff]:
    """Per-snap hold membership diffs (#208, D2), emitted AFTER the presence diffs so
    install-before-hold ordering holds (`apply()` preserves diff order): source-held &
    not target-held -> INSTALL (hold); target-held & not source-held -> REMOVE (unhold);
    both-held or neither -> no diff.

    Only snaps present on the SOURCE are considered — source is authoritative for hold
    intent (a hold on a snap the source no longer has is not the user's current intent).
    `sorted` for a stable, deterministic review order.
    """
    source_by_name = {item.name: item for item in source_items}
    target_held = {item.name for item in target_items if item.held}

    diffs: list[ItemDiff] = []
    for name in sorted(source_by_name):
        in_source = source_by_name[name].held
        in_target = name in target_held
        if in_source == in_target:
            continue
        diffs.append(_hold_diff(name, in_source=in_source))
    return diffs


def _diff_snap_items(
    source_items: Sequence[SnapItem], target_items: Sequence[SnapItem], machines: Machines
) -> list[ItemDiff]:
    """One diff per snap name present on either side, source-then-target order — same
    shape as `apt_sync.diffing.diff_apt_packages`, but with D-06's own convergence rule.
    Per-snap hold membership diffs follow the presence diffs (D8: install-before-hold).
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
            diffs.append(_change_diff(item_id, source_item, target_item, machines))
        # else: present on both, identical revision and channel -> no diff.

    # Hold diffs AFTER presence diffs (D8) so a hold on a same-run install lands once the
    # snap is present.
    diffs.extend(_diff_snap_holds(source_items, target_items))

    return diffs


def _current_revision_name(app_dir: Path) -> str | None:
    """The revision directory name `~/<app>/current` resolves to, or None when the
    symlink is missing or dangling.

    snapd maintains `current` as a symlink to the active revision's directory. Resolving
    it (readlink, followed to the real dir) yields that revision's directory name so the
    caller can keep the matching data dir in the mirror (decision 3). A missing or dangling
    `current` — the active revision is then indeterminate — returns None, and the caller
    falls back to excluding every revision dir for that app (the safe default).
    """
    current = app_dir / "current"
    try:
        resolved = current.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_dir():
        return None
    return resolved.name


async def target_snap_revisions(executor: RemoteExecutor) -> dict[str, str] | None:
    """The revision each snap is active at on the machine `executor` reaches, or None when
    that machine's snapd could not be asked.

    This is the evidence `PKG-FR-SNAP-DATA-BOUNDARY` rests on — "revisions the target's
    snapd never installed" is a fact about the target, so it is read from the target rather
    than inferred from the source's filesystem. `PKG-FR-JOB-ORDER` is what makes the answer
    usable: every package job runs before `folder_sync`, so by the time `folder_sync` asks,
    this listing already carries everything `snap_sync` converged this run — and, equally,
    still shows the old revision (or no entry at all) where an install or a revision change
    was declined, failed, or never offered because `snap_sync` is disabled.

    None rather than an empty map when the read does not answer: a machine with no snaps
    exits 0 with an empty listing, which is ordinary data, while a snapd that cannot be
    reached says nothing about which revisions exist (ADR-022). Callers exclude every
    revision dir either way here, but the two must not be confused with each other.
    """
    result = await executor.run_command("snap list --all", login_shell=False)
    if not result.success:
        return None
    return {item.name: item.revision for item in parse_snap_list(result.stdout)}


def snap_sync_exclude_paths(target_revisions: Mapping[str, str] | None) -> list[Path]:
    """Absolute `~/snap/<app>/<revision>` data directories folder_sync must NOT mirror,
    resolved against `Path.home()` at call time exactly like `vscode_state_exclude_paths()`
    — unlike VS Code's fixed relpath list, the revision set is dynamic, so it is enumerated
    from the filesystem rather than hardcoded.

    `target_revisions` is snap name -> the revision the TARGET is active at
    (`target_snap_revisions`, read after the package jobs ran), or None when the target's
    snapd could not be asked. An app's data dir is kept out of the exclusion set — and so
    travels — only where the app's `current` symlink here resolves to a revision the target
    is ALSO on: that is the one directory the target's snapd will read (decision 3, issue
    #118). Every other revision dir is excluded, because the target's snapd never installed
    it (`PKG-FR-SNAP-DATA-BOUNDARY`), which covers the retained older revisions as well as an
    app whose install or revision change was declined, failed, or never proposed at all.

    `~/snap/<app>/common` (revision-independent user data folder_sync must keep mirroring)
    and `~/snap/<app>/current` (the symlink itself) are always kept — the whole reason this
    export is not simply `~/snap`. When `current` is missing or dangling the active revision
    cannot be determined, so ALL of that app's revision dirs are excluded (safe default), as
    they are when `target_revisions` is None.
    """
    snap_root = Path.home() / "snap"
    if not snap_root.is_dir():
        return []

    revisions = target_revisions or {}
    paths: list[Path] = []
    for app_dir in sorted(snap_root.iterdir()):
        if not app_dir.is_dir():
            continue
        current_revision = _current_revision_name(app_dir)
        # `!=` also covers the two None cases: an indeterminate `current` here, and a snap
        # the target does not hold (absent from the map, so `.get` is None).
        converged = current_revision is not None and revisions.get(app_dir.name) == current_revision
        for entry in sorted(app_dir.iterdir()):
            if entry.name in _NON_REVISION_DIR_NAMES or not entry.is_dir():
                continue
            if converged and entry.name == current_revision:
                continue  # the target is on this revision, so its data dir travels
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

    async def capture_source_items(self) -> Sequence[SnapItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """`snap list --all` on the source (D-06).

        This job overrides `plan()` and never routes through `PackageSyncJob.
        diff_items`'s apt-package-shaped dispatch (module docstring), so widening this
        hook's item type here is safe: no code holding a `PackageSyncJob`-typed
        reference ever calls it expecting an `AptPackageItem` back.

        Guarded on the exit code (ADR-022). This is the read whose silence is worst: an
        empty source manifest offers every snap on the target for removal, and the only
        thing standing between that and a wiped target is that removal groups arrive
        unticked. Measured against the real `snap` binary — with snapd unreachable it exits
        1, and with snapd answering that zero snaps are installed it exits 0, writes "No
        snaps are installed yet." to STDERR and leaves stdout empty. So the exit code
        separates the two cleanly, and an empty stdout at exit 0 is a machine with no snaps,
        which is an ordinary machine.
        """
        command = "snap list --all"
        result = await self.source.run_command(command)
        require_answer(command, result, self.machines.source)
        return parse_snap_list(result.stdout)

    async def query_target_items(self) -> Sequence[SnapItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The target's own `snap list --all` (same reasoning as `capture_source_items`)."""
        command = "snap list --all"
        result = await self.target.run_command(command, login_shell=False)
        require_answer(command, result, self.machines.target)
        return parse_snap_list(result.stdout)

    @override
    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """The marked snaps one machine no longer has, read off that machine's own
        `snap list --all` — the same listing the diff is built from, and the whole of what
        snapd says is installed there.

        A sideloaded snap is in that listing like any other, so a mark on one survives:
        `PKG-FR-SNAP-SIDELOAD` puts a sideload out of THIS job's scope, which says nothing
        about whether the machine has it. `manual_snap_sync` reconciles its own marks about
        the same snaps out of its own file, under its own `unreproducible:` ids.

        `snap:hold:` entries answer nothing — a hold is derived and can never be recorded
        (`PKG-FR-BLOCKS-DERIVED`) — so they are left alone. Which entries those are is read
        off the ID rather than the recorded `item_class`: the file is hand-editable, so the
        two can disagree, and a snap cannot be named `hold:…` for the prefixes to collide.
        """
        snap_ids = {
            item_id
            for item_id in entries
            if item_id.startswith(_SNAP_ID_PREFIX) and not item_id.startswith(_SNAP_HOLD_ID_PREFIX)
        }
        if not snap_ids:
            return frozenset()

        items = await self.capture_source_items() if on_source else await self.query_target_items()
        installed = {item.name for item in items}
        return frozenset(item_id for item_id in snap_ids if item_id.removeprefix(_SNAP_ID_PREFIX) not in installed)

    @override
    async def plan(self) -> PackagePlan:
        """Load decision files -> capture -> query -> diff -> build review groups.

        Read-only: only `snap list --all` (both machines) and a decision-file `cat`
        run here — no `snap install`/`refresh`/`switch`/`remove` before this returns.
        Caches the filtered source/target items by id for `converge()` (see
        `__init__`), since `ItemDiff.item_id` alone carries no revision/channel data.

        Sideloaded snaps on either machine are dropped from the diff input: their revision
        exists in no store, so snapd's own verbs can neither reproduce one nor replace one
        this job removes. Every diff they could produce — install, revision/channel change,
        the `snap:hold:` diff `_diff_snap_holds` derives from a source snap, and removal —
        is withheld, and this job says nothing about them (`PKG-FR-SNAP-SIDELOAD`).

        `manual_snap_sync` is where they go instead: it applies this same
        `partition_sideloaded` predicate to its own `snap list --all` and offers the
        sideloads it finds as items resolvable by an install snippet. The two jobs agree
        because both call `packages/snap_listing.py`, never because one tells the other —
        neither imports the other, and neither reads the other's enable flag (D-15/D-18).
        """
        source_decisions, target_decisions = await self._load_live_decisions()

        # Sideloads are partitioned off the RAW listing, before the machine-specific filter:
        # filtering first would drop a marked sideload before `withheld` below could see it,
        # leaving the OTHER machine's copy of that name unmatched — and an unmatched entry is
        # an item, which is exactly what the article forbids for a sideloaded name.
        source_items, source_sideloaded = partition_sideloaded(await self.capture_source_items())
        target_items, target_sideloaded = partition_sideloaded(await self.query_target_items())

        # Both files against BOTH manifests (`marks_on_either`): a snap present on both
        # machines at different revisions is marked on ONE of them, and filtering each
        # manifest by its own file alone would leave the other machine's copy unmatched —
        # turning the change the mark silenced into a removal of the very copy it protects.
        marked = marks_on_either(source_decisions, target_decisions)
        source_items = await filter_inert(source_items, marked)
        target_items = await filter_inert(target_items, marked)
        # A name sideloaded on ONE machine leaves the diff on BOTH. Dropping only the
        # machine holding the sideloaded copy would leave the other machine's entry
        # unmatched, and an unmatched entry is an item: a source-only install whose
        # target already holds a sideloaded copy of that name, or an EXTRA_ON_TARGET
        # removal of a snap nothing can reinstall.
        withheld = {item.name for item in (*source_sideloaded, *target_sideloaded)}
        source_items = [item for item in source_items if item.name not in withheld]
        target_items = [item for item in target_items if item.name not in withheld]

        self._source_items_by_id = {item.item_id: item for item in source_items}
        self._target_items_by_id = {item.item_id: item for item in target_items}

        # `_drop_inert_diffs` after the diff, not `filter_inert` before it: a hold's
        # identity is `snap:hold:<name>`, which no `SnapItem` carries (its own id is
        # `snap:<name>`), so a recorded hold decision can only be matched on the ItemDiff.
        diffs = self._drop_inert_diffs(
            _diff_snap_items(source_items, target_items, self.machines), source_decisions, target_decisions
        )
        groups = self._build_review_groups(diffs)
        return PackagePlan(manager=self.manager_id, diffs=diffs, groups=groups)

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Install/refresh at the source's explicit revision, switch channel only when
        it differs, or remove (never purge) — the only D-06-safe verbs (module
        docstring). One snap per invocation (D-27) so a single bad snap cannot fail the
        whole batch.

        Hold membership items (`snap:hold:<name>`) are routed FIRST, by item_id prefix
        (#208, D4), so a `snap:hold:` INSTALL runs `snap refresh --hold=forever` rather
        than a snap install, and a `snap:hold:` REMOVE (unhold) is never misread as a
        snap removal. This test MUST precede the action-based dispatch below, because
        `snap:hold:` also matches the plain `snap:` prefix.
        """
        if diff.item_id.startswith(_SNAP_HOLD_ID_PREFIX):
            return await self._converge_hold(diff)

        if diff.action == DiffAction.REMOVE:
            return await self._converge_remove(diff)

        source_item = self._source_items_by_id.get(diff.item_id)
        if source_item is None:
            raise ConvergeItemFailed(
                f"no snap captured from {self.machines.source} for {diff.label} (item_id={diff.item_id!r}); "
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

        The SOURCE item's confinement flag is interpolated because snapd requires it as
        explicit per-revision confirmation: without `--classic`, installing a
        classic-confinement snap fails with "repeat the command including --classic" on
        every run, forever. `--revision=N` does NOT bypass that check — snapd's own
        wording is "This revision of snap %q was published using classic confinement".
        """
        name = shlex.quote(source_item.name)
        revision = shlex.quote(source_item.revision)
        install_result = await self.target.run_command(
            f"sudo snap install{_confinement_flags(source_item)} --revision={revision} {name}",
            login_shell=False,
            mutates=f"install snap {source_item.name} at revision {source_item.revision}",
        )
        if not install_result.success:
            return install_result
        return await self._switch_channel(source_item)

    async def _converge_change(self, source_item: SnapItem, target_item: SnapItem | None) -> CommandResult:
        """`snap refresh --revision=<N>` when the revision differs, plus the channel
        switch when the channel also differs (or is the only thing that differs) —
        `converge()` only reaches here for a diff `_diff_snap_items` built because at
        least one of the two was true.

        The refresh carries the SOURCE item's confinement flag for the same reason the
        install does. `snap refresh --help` says a plain refresh preserves the snap's
        existing confinement options, but that describes the TARGET's current confinement,
        which is the wrong one whenever the two hosts disagree: source classic + target
        strict is a real case (a snap that changed confinement between the two installed
        revisions), and refreshing onto the source's classic revision without `--classic`
        hits the same per-revision refusal an install would. The flag is therefore always
        passed rather than conditioned on the target — the safe direction, since `--classic`
        on a snap that does not need it is only a warning ("flag --classic ignored for
        strictly confined snap"), whereas omitting it where it IS needed is a hard failure
        on every run.

        The reverse skew (source strict, target classic) emits no flag and leaves the
        target's confinement as-is: confinement is not part of item identity and never
        produces a diff of its own, so there is nothing here proposing to converge it.
        """
        revision_differs = target_item is None or source_item.revision != target_item.revision
        channel_differs = target_item is None or source_item.channel != target_item.channel

        result: CommandResult | None = None
        if revision_differs:
            name = shlex.quote(source_item.name)
            revision = shlex.quote(source_item.revision)
            result = await self.target.run_command(
                f"sudo snap refresh{_confinement_flags(source_item)} --revision={revision} {name}",
                login_shell=False,
                mutates=f"move snap {source_item.name} to revision {source_item.revision}",
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
        return await self.target.run_command(
            f"sudo snap switch --channel={channel} {name}",
            login_shell=False,
            mutates=f"track channel {source_item.channel} for snap {source_item.name}",
        )

    async def _converge_remove(self, diff: ItemDiff) -> CommandResult:
        """`snap remove`, never `--purge`: purge discards snapd's own pre-removal
        snapshot, which is the user's only recovery path if the removal was a mistake.
        """
        name = shlex.quote(_snap_name(diff.item_id))
        return await self.target.run_command(
            f"sudo snap remove {name}",
            login_shell=False,
            mutates=f"remove snap {_snap_name(diff.item_id)}",
        )

    async def _converge_hold(self, diff: ItemDiff) -> CommandResult:
        """Converge one `snap:hold:<name>` membership item (#208, D4/D6): INSTALL ->
        `snap refresh --hold=forever <name>` (per-snap hold on the target), REMOVE ->
        `snap refresh --unhold <name>`.

        CRITICAL (module docstring, RESEARCH Pitfall 1): the snap name is ALWAYS
        interpolated, so this never degenerates into a bare `snap refresh --hold` — the
        form that silently sets an INDEFINITE GLOBAL hold on every snap. The name comes
        from the item_id's `snap:hold:` suffix, which `_diff_snap_holds` only ever built
        from a concrete source/target snap name, so it is never empty.

        A hold whose snap this run was asked to install and did not is refused before any
        command (`PKG-FR-BLOCKS-DERIVED`): the hold replicates without review, so nothing
        else carries the user's answer to it, and holding a snap the target does not have is
        neither meaningful nor what they asked for. Declined, never failed — the user's own
        answer is what withdrew it. An install that was approved and then BROKE is the other
        case, and it needs nothing here: `snap refresh --hold` on the absent snap exits
        non-zero and fails that hold as its own item (D-27).
        """
        raw_name = diff.item_id.removeprefix(_SNAP_HOLD_ID_PREFIX)
        if diff.action == DiffAction.INSTALL and self._install_was_declined(raw_name):
            raise ConvergeItemDeclined(
                f"hold on {raw_name} not applied: its install was not approved, and holding a snap "
                f"{self.machines.target} lacks is not what was asked for"
            )
        name = shlex.quote(raw_name)
        flag = "--hold=forever" if diff.action == DiffAction.INSTALL else "--unhold"
        verb = "hold" if diff.action == DiffAction.INSTALL else "unhold"
        return await self.target.run_command(
            f"sudo snap refresh {flag} {name}",
            login_shell=False,
            mutates=f"{verb} snap {raw_name}",
        )

    def _install_was_declined(self, name: str) -> bool:
        """Whether this run offered to install `name` and the user did not approve it.

        `False` where the snap is no item at all — the target already has it and the hold is
        the whole change — and where the item is a revision or channel change, which says
        nothing about whether the snap is present.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        decisions = self._accepted_outcome.decisions
        return any(
            diff.item_id == f"{_SNAP_ID_PREFIX}{name}"
            and diff.action is DiffAction.INSTALL
            and decisions.get(diff.item_id) != Decision.APPLY
            for diff in self._accepted_plan.diffs
        )

    @override
    async def validate(self) -> list[ValidationError]:
        """`snap version` on both ends, passwordless `sudo --non-interactive true` on BOTH ends, and a
        read-only informational hold check on both ends (never acted on — module docstring).
        Sequential checks appending to `errors`, matching `AptSyncJob.validate()`'s shape.

        Passwordless sudo is now required on the SOURCE too, not just the target: the
        orchestrator pauses snapd auto-refresh across the sync window by writing
        `refresh.hold` via `sudo snap set system` on both hosts (decision 4), so the source
        needs the same `sudo --non-interactive` grant for `/usr/bin/snap` that the target already needed
        for install/refresh/remove.
        """
        errors: list[ValidationError] = []

        source_check = await self.source.run_command("snap version")
        if not source_check.success:
            errors.append(self._validation_error(Host.SOURCE, "snap is not available on source"))

        target_check = await self.target.run_command("snap version", login_shell=False)
        if not target_check.success:
            errors.append(self._validation_error(Host.TARGET, "snap is not available on target"))

        source_sudo_check = await self.source.run_command("sudo --non-interactive true")
        if not source_sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "passwordless sudo is not available on source "
                    "(required to pause snapd auto-refresh for the sync window).\n"
                    + passwordless_sudo_hint(_TARGET_SUDO_COMMANDS),
                )
            )

        sudo_check = await self.target.run_command("sudo --non-interactive true", login_shell=False)
        if not sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "passwordless sudo is not available on target "
                    "(required for snap install/refresh/remove).\n"
                    + passwordless_sudo_hint(_TARGET_SUDO_COMMANDS, user=self.context.target_username),
                )
            )

        # Read-only informational context (RESEARCH Pitfall 1): NEVER `snap refresh
        # --hold` with no arguments to check this — that form mutates. A pre-existing
        # hold is not itself an error here; it only explains why a revision might not
        # converge, so it is logged, never appended to `errors`.
        # Under sudo: snapd admin-gates READING snap config (`io.snapcraft.snapd.manage-
        # configuration`, `auth_admin_keep`), so unprivileged this does not report "no hold",
        # it fails with "access denied" and the line below would say `(none)` unconditionally.
        source_hold = await self.source.run_command("sudo snap get system refresh.hold")
        self._log(Host.SOURCE, LogLevel.FULL, f"snap refresh.hold: {source_hold.stdout.strip() or '(none)'}")
        target_hold = await self.target.run_command("sudo snap get system refresh.hold", login_shell=False)
        self._log(Host.TARGET, LogLevel.FULL, f"snap refresh.hold: {target_hold.stdout.strip() or '(none)'}")

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
