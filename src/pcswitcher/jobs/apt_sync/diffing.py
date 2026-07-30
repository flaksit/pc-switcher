"""Every diff this job produces — the whole of D-25 as apt expresses it.

This is apt's own, not the base class's (D-15): while the package diff lived on
`PackageSyncJob`, the other three managers inherited hold sets, pin facts and no-candidate
ids they never fill in, and each wrote its own diff anyway — because what a diff even IS
differs per ecosystem. `packages/items.py` keeps the taxonomy every manager is keyed on.

Pure over captured facts, with two exceptions that read a file: a pin and a repository
offered for DELETION are shown to the user whole, and a filename alone gives them nothing to
decide from. Both fetch content only for the files their own direction implicates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pcswitcher.jobs.apt_sync.items import (
    APT_PREFERENCES_DIR,
    APT_SOURCES_DIR,
    COLLATERAL_ID_PREFIX,
    DISTRO_SOURCE_FILENAMES,
    METADATA_REFRESH_ITEM_ID,
    AptConfigItem,
    AptHoldItem,
    AptPackageItem,
    AptPinItem,
    AptSourceItem,
)
from pcswitcher.jobs.apt_sync.messages import (
    build_origin_detail,
    build_origin_mismatch_detail,
    build_repo_removal_detail,
    build_repo_unavailable_detail,
)
from pcswitcher.jobs.apt_sync.origins import OriginOutcome, OriginPlan, is_origin_mismatch
from pcswitcher.jobs.apt_sync.probe import Run, parse_source_file, read_file_content
from pcswitcher.jobs.packages.items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
    Machines,
    build_version_mismatch_detail,
)
from pcswitcher.models import Host


@dataclass(frozen=True)
class FilenameDiff:
    """Filename-level classification of two `{filename: digest}` maps — the shared
    basis every one of the `/etc/apt/*` directories is compared with.
    """

    missing: frozenset[str]
    extra: frozenset[str]
    changed: frozenset[str]


def diff_filenames(source_digests: Mapping[str, str], target_digests: Mapping[str, str]) -> FilenameDiff:
    source_names = frozenset(source_digests)
    target_names = frozenset(target_digests)
    changed = frozenset(name for name in source_names & target_names if source_digests[name] != target_digests[name])
    return FilenameDiff(missing=source_names - target_names, extra=target_names - source_names, changed=changed)


def diff_apt_packages(
    source_items: Sequence[AptPackageItem],
    target_items: Sequence[AptPackageItem],
    origin_plan: Mapping[str, OriginPlan],
    machines: Machines,
    source_hold_names: frozenset[str] = frozenset(),
    target_hold_names: frozenset[str] = frozenset(),
) -> list[ItemDiff]:
    """One diff per item id present on either side, source-then-target order,
    followed by the `apt:hold:` membership diffs (#208, D5/D8 — holds emitted AFTER
    package diffs so install lands before its hold once the diffs converge).

    A HELD package (target hold set) has its install/upgrade action SUPPRESSED (a held
    package is never proposed for install/version change) but produces NO package-level
    report — the hold travels as its own `apt:hold:` item, so a held package is never
    double-reported. A PINNED package gets no echo of any kind: a pin's only job is
    deciding which origin wins, which D-35 checks against the target's real post-refresh
    state instead of guessing at it here. Otherwise:

    - missing-on-target -> `MISSING_ON_TARGET`/`INSTALL` when the source's origin either
      already serves the target or can be made to (`OriginPlan.outcome`), else
      `REPO_UNAVAILABLE`/`REPORT_ONLY`. This is ADR-020 D-34: the package a target could
      satisfy from a DIFFERENT vendor is still an install, but one that carries the source's
      repository with it, and the review line names where it will come from.
    - extra-on-target -> `EXTRA_ON_TARGET`/`REMOVE`.
    - present on both, from vendors that do not overlap -> `ORIGIN_MISMATCH`/`REPORT_ONLY`,
      checked BEFORE the version comparison: two vendors' copies of one name have no common
      version scale, so "source has X, target has Y" would report a difference of degree
      where the real difference is of origin.
    - present on both with differing versions -> `VERSION_MISMATCH`/`REPORT_ONLY` (D-04:
      reported, never force-downgraded).
    - present on both, same vendor, same version -> no diff at all.

    Hold membership (D2): source-held & target-not -> `AptHoldItem` INSTALL (hold);
    target-held & source-not -> REMOVE (unhold); held on both or neither -> no diff.
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

        # Both cannot be None: `item_id` is here because one side or the other carries it,
        # and the two sides' items share the name their id is built from.
        name = (target_item or source_item).name  # pyright: ignore[reportOptionalMemberAccess]
        if name in target_hold_names:
            # Held on the target: suppress its install/version action entirely (a held
            # package must never be proposed for install/upgrade). No package-level
            # report — the `apt:hold:` item below carries the hold fact.
            #
            # Keyed on the name, not on the target having a MANUAL entry for it: the
            # target's hold set is `apt-mark showhold`, which covers packages apt
            # installed automatically there too, and one of those held on the target and
            # manual on the source would otherwise be proposed for an install apt refuses.
            continue
        elif source_item is not None and target_item is None:
            origins = origin_plan.get(item_id, OriginPlan())
            if origins.outcome() is OriginOutcome.UNREPLICABLE:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_PACKAGE,
                        diff_class=DiffClass.REPO_UNAVAILABLE,
                        action=DiffAction.REPORT_ONLY,
                        item_id=item_id,
                        label=source_item.label(),
                        detail=build_repo_unavailable_detail(
                            source_item.name,
                            sorted(origins.source_origins),
                            origins.unavailable_cause(machines),
                            machines,
                        ),
                    )
                )
            else:
                diffs.append(
                    ItemDiff(
                        item_class=ItemClass.APT_PACKAGE,
                        diff_class=DiffClass.MISSING_ON_TARGET,
                        action=DiffAction.INSTALL,
                        item_id=item_id,
                        label=source_item.label(),
                        detail=build_origin_detail(origins.vendor_source_origins),
                    )
                )
        elif target_item is not None and source_item is None:
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_PACKAGE,
                    diff_class=DiffClass.EXTRA_ON_TARGET,
                    action=DiffAction.REMOVE,
                    item_id=item_id,
                    label=target_item.label(),
                    detail=None,
                )
            )
        elif is_origin_mismatch(origin_plan.get(item_id, OriginPlan())):
            origins = origin_plan[item_id]
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_PACKAGE,
                    diff_class=DiffClass.ORIGIN_MISMATCH,
                    action=DiffAction.REPORT_ONLY,
                    item_id=item_id,
                    label=target_item.label() if target_item is not None else item_id,
                    detail=build_origin_mismatch_detail(
                        origins.vendor_source_origins, origins.vendor_target_origins, machines
                    ),
                )
            )
        elif source_item is not None and target_item is not None and source_item.version != target_item.version:
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.APT_PACKAGE,
                    diff_class=DiffClass.VERSION_MISMATCH,
                    action=DiffAction.REPORT_ONLY,
                    item_id=item_id,
                    label=target_item.label(),
                    detail=build_version_mismatch_detail(source_item.version, target_item.version, machines),
                )
            )
        # else: present on both, one vendor, equal versions, not held -> no diff.

    # Hold membership diffs (#208, D2/D8): emitted AFTER every package diff so a
    # package install lands before its hold when both are approved.
    diffs.extend(diff_apt_holds(source_hold_names, target_hold_names))
    return diffs


def diff_apt_holds(source_hold_names: frozenset[str], target_hold_names: frozenset[str]) -> list[ItemDiff]:
    """`apt:hold:` membership diffs (#208, D2): source-held & target-not -> INSTALL
    (hold); target-held & source-not -> REMOVE (unhold); held on both or on neither
    -> no diff. `sorted` for a stable, deterministic review order.
    """
    diffs: list[ItemDiff] = []
    for name in sorted(source_hold_names | target_hold_names):
        in_source = name in source_hold_names
        in_target = name in target_hold_names
        if in_source == in_target:
            continue
        hold_item = AptHoldItem(name=name)
        diffs.append(
            ItemDiff(
                item_class=ItemClass.APT_HOLD,
                diff_class=DiffClass.MISSING_ON_TARGET if in_source else DiffClass.EXTRA_ON_TARGET,
                action=DiffAction.INSTALL if in_source else DiffAction.REMOVE,
                item_id=hold_item.item_id,
                label=hold_item.label(),
                detail=None,
            )
        )
    return diffs


def file_diff(
    item: AptPinItem | AptConfigItem, diff_class: DiffClass, action: DiffAction, *, detail: str | None = None
) -> ItemDiff:
    """One `ItemDiff` for a pin or config item — the two classes with no content-derived
    detail beyond the shared `VERSION_MISMATCH` digest wording (`AptSourceItem`'s
    dangling-keyring case is handled separately by `diff_apt_sources` itself).
    """
    item_class = ItemClass.APT_PIN if isinstance(item, AptPinItem) else ItemClass.APT_CONFIG
    return ItemDiff(
        item_class=item_class,
        diff_class=diff_class,
        action=action,
        item_id=item.item_id,
        label=item.label(),
        detail=detail,
    )


async def diff_apt_pins(
    target_run: Run,
    source_digests: Mapping[str, str],
    target_digests: Mapping[str, str],
) -> tuple[list[ItemDiff], dict[str, str]]:
    """Pin-file diffs — the REMOVAL direction only — plus each offered file's content.

    A pin the source has is written to the target when missing and overwritten when
    different, with no review line at all (ADR-020 D-36): a pin is what makes an origin win,
    in the same sense a signing key is what makes a repository trusted, and neither is
    something an approved package leaves the user a basis to judge. A pin naming an origin
    the target does not have is inert, so the always-sync rule cannot get the derivation
    wrong and costs nothing.

    Deleting one is different, and that is why this direction survives: a pin the target has
    and the source does not is holding some origin above another on a machine the source
    knows nothing about, so removing it can flip which vendor supplies a package at the
    target's next upgrade — a consequence no approved package implies.

    Which is exactly why the file's CONTENT is read here and returned alongside the diffs,
    keyed by filename: `99-vendor.pref` names no origin, no priority and no package, so a
    review row carrying only that filename asks the user to approve a change to which vendor
    supplies their software while showing them nothing about it. One `sudo cat` per file
    OFFERED for deletion — never per pin file that exists — and only on a run that found
    one. It is a read (`read_file_content`, ADR-022-guarded): silence there fails the job
    rather than showing an empty pin the user would approve deleting.

    The content is deliberately not parsed. Pin syntax is small enough to read, and a
    rendered summary of it would be this module claiming to know which stanza wins.
    """
    names = diff_filenames(source_digests, target_digests)
    diffs: list[ItemDiff] = []
    contents: dict[str, str] = {}
    for filename in sorted(names.extra):
        contents[filename] = await read_file_content(target_run, f"{APT_PREFERENCES_DIR}/{filename}", Host.TARGET)
        diffs.append(
            file_diff(
                AptPinItem(filename=filename, digest=target_digests[filename]),
                DiffClass.EXTRA_ON_TARGET,
                DiffAction.REMOVE,
            )
        )
    return diffs, contents


def diff_apt_configs(
    source_digests: Mapping[str, str], target_digests: Mapping[str, str], machines: Machines
) -> list[ItemDiff]:
    """Config-file diffs — opaque, digest-only, filename identity."""
    names = diff_filenames(source_digests, target_digests)
    diffs: list[ItemDiff] = []

    for filename in sorted(names.missing):
        item = AptConfigItem(filename=filename, digest=source_digests[filename])
        diffs.append(file_diff(item, DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL))
    for filename in sorted(names.extra):
        item = AptConfigItem(filename=filename, digest=target_digests[filename])
        diffs.append(file_diff(item, DiffClass.EXTRA_ON_TARGET, DiffAction.REMOVE))
    for filename in sorted(names.changed):
        item = AptConfigItem(filename=filename, digest=source_digests[filename])
        detail = build_version_mismatch_detail(source_digests[filename], target_digests[filename], machines)
        diffs.append(file_diff(item, DiffClass.VERSION_MISMATCH, DiffAction.CHANGE, detail=detail))
    return diffs


async def diff_apt_sources(
    target_run: Run,
    source_digests: Mapping[str, str],
    target_digests: Mapping[str, str],
    machines: Machines,
    in_use: frozenset[str] = frozenset(),
) -> list[ItemDiff]:
    """Source-file diffs — the REMOVAL direction only (ADR-020 D-37).

    Adding a repository is not a question. A source file lands on the target because a
    package approved on the review comes from it, so "package ticked, its repository
    unticked" is unrepresentable: the repository has no tick. Overwriting one that
    differs on the two machines is derived for the same reason. Both directions are
    built in `derived.DerivedWrites` instead, from the packages that need them.

    Removal survives because nothing derives it: a repository the source no longer has
    is not implied by any approved package. `in_use` names the files the target still
    installs something from once this run's proposed removals are counted out, and they are
    withheld outright rather than offered with a disclosure of what the deletion would
    strand (`PKG-FR-REPO-DELETE`): a repository feeding software the machine keeps is not a
    decision the user can usefully take, and the packages it feeds are the ones no review
    can show — recorded machine-specific, they are filtered out before anything is diffed.

    The URLs the file declares are what the remaining decision is actually about: a filename
    is somebody's naming convention, while `https://cli.github.com/packages` is the thing the
    machine would stop getting software from. They cost nothing — the file is already read
    here for its format, and `parse_source_file` returns the URIs from the same parse.

    The distribution's own files are excluded outright (D-38): they are written and
    updated but never removed, so a target that has `ubuntu.sources` and a source that
    somehow does not must not turn into an offer to delete the target's archive.
    """
    names = diff_filenames(source_digests, target_digests)
    diffs: list[ItemDiff] = []

    for filename in sorted(names.extra - DISTRO_SOURCE_FILENAMES - in_use):
        content = await read_file_content(target_run, f"{APT_SOURCES_DIR}/{filename}", Host.TARGET)
        fmt, _refs, uris = parse_source_file(filename, content)
        item = AptSourceItem(filename=filename, digest=target_digests[filename], fmt=fmt)
        diffs.append(
            ItemDiff(
                item_class=ItemClass.APT_SOURCE,
                diff_class=DiffClass.EXTRA_ON_TARGET,
                action=DiffAction.REMOVE,
                item_id=item.item_id,
                label=item.label(),
                detail=build_repo_removal_detail(uris, machines),
            )
        )

    return diffs


def metadata_refresh_diff() -> ItemDiff:
    """The one synthetic `apt-get update` diff a run inserts (`accept_review`)
    when at least one repository-group item was approved. Reuses `ItemClass.APT_SOURCE`
    so it naturally sorts with the repository group if this diff were ever re-sorted —
    membership in `REPO_GROUP_CLASSES` checks EXCLUDE it by item_id, not class,
    which is what keeps it from being treated as a real `/etc/apt` file to back up.
    """
    return ItemDiff(
        item_class=ItemClass.APT_SOURCE,
        diff_class=DiffClass.MISSING_ON_TARGET,
        action=DiffAction.CHANGE,
        item_id=METADATA_REFRESH_ITEM_ID,
        label="Refresh apt package metadata (apt-get update)",
        detail=None,
    )


def collateral_diff(
    name: str, detail: str, *, act_word: str = "resolve", answer_hints: tuple[str, str] | None = None
) -> ItemDiff:
    """One manual-collateral item (D-30): a package the TARGET's apt has marked manually
    installed that the pending transaction would remove or downgrade. Stays `REPORT_ONLY` so
    `apply()` never converges it directly — its decision governs the changes that cause it,
    not itself.

    `detail` is the whole finding as one sentence — which change, on which machine, and what
    it does to this package — because that sentence is the entire basis for the answer. It is
    apt's own simulation talking; the review screen says so once, rather than every item
    repeating it.
    """
    return ItemDiff(
        item_class=ItemClass.APT_PACKAGE,
        diff_class=DiffClass.EXTRA_ON_TARGET,
        action=DiffAction.REPORT_ONLY,
        item_id=f"{COLLATERAL_ID_PREFIX}{name}",
        label=name,
        detail=detail,
        answer_hints=answer_hints,
    )
