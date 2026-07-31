"""`AptSyncJob` itself: the `PackageSyncJob` contract, and the wiring that turns captured
facts into the collaborators that decide and act.

Everything substantive lives in the modules this one composes. What stays here is what the
base class calls — `plan`, `accept_review`, `apply`, `converge`, `validate` — plus apt's own
review-group carving, which is presentation rather than a decision about a machine.

`_work` replaces the 35 attributes that used to carry state from `plan()` to `converge()`:
one frozen object holding this run's captured facts and the collaborators that decide over
them, replaced wholesale by `plan()` rather than mutated in place.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, override

from pcswitcher.jobs.apt_sync.collateral import Collateral
from pcswitcher.jobs.apt_sync.commands import SOURCE_SUDO_COMMANDS, TARGET_SUDO_COMMANDS
from pcswitcher.jobs.apt_sync.derived import DerivedWrites
from pcswitcher.jobs.apt_sync.diffing import (
    diff_apt_configs,
    diff_apt_packages,
    diff_apt_pins,
    diff_apt_sources,
    diff_filenames,
    metadata_refresh_diff,
)
from pcswitcher.jobs.apt_sync.esm_gate import EsmGate
from pcswitcher.jobs.apt_sync.etc_apt import EtcApt
from pcswitcher.jobs.apt_sync.files import TargetFiles
from pcswitcher.jobs.apt_sync.items import (
    APT_HOLD_ID_PREFIX,
    APT_PACKAGE_ID_PREFIX,
    APT_SOURCES_LIST,
    CONFLICT_ID_PREFIX,
    DISTRO_SOURCE_FILENAMES,
    ITEM_CLASS_ORDER,
    METADATA_REFRESH_ITEM_ID,
    REPO_GROUP_CLASSES,
    REPO_REMOVAL_VERBS,
    UNRECORDABLE_ITEM_ID_PREFIXES,
    is_collateral_diff,
    is_repo_removal_diff,
    package_name,
    pin_filename,
)
from pcswitcher.jobs.apt_sync.keyrings import Keyrings
from pcswitcher.jobs.apt_sync.messages import build_repo_conflict_detail
from pcswitcher.jobs.apt_sync.origins import OriginClassifier, OriginPlan
from pcswitcher.jobs.apt_sync.packages import MetadataRefresh, PackageConverger
from pcswitcher.jobs.apt_sync.probe import AptProbe, OriginFacts, RepoConflict, RepoFacts
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import (
    COLLATERAL_REVIEW_ACTION,
    REPO_CONFLICT_REVIEW_ACTION,
    REPO_REMOVAL_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.state import DecisionFile, filter_inert
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, LogLevel, ValidationError
from pcswitcher.sudoers import passwordless_sudo_hint


@dataclass(frozen=True)
class _Work:
    """This run's captured facts and the collaborators that decide and act over them.

    Replaced wholesale by `plan()` rather than mutated, so "what is this run working from" has
    one answer at every moment. A job that has not planned holds the empty set, which is what
    lets `accept_review` on a hand-assembled plan derive an empty write set instead of
    requiring a capture it has no reason to run.
    """

    source_facts: OriginFacts
    target_facts: OriginFacts
    origins: OriginClassifier
    keyrings: Keyrings
    derived: DerivedWrites
    collateral: Collateral
    etc_apt: EtcApt
    packages: PackageConverger


class AptSyncJob(PackageSyncJob):
    """Converge apt packages (install missing, remove extra) after this job's own batched
    review, guarded by plan-time and apply-time apt transaction simulation.
    """

    name: ClassVar[str] = "apt_sync"
    manager_id: ClassVar[str] = "apt"

    # No configurable properties yet: this slice needs nothing beyond the enable flag in
    # sync_jobs. `enabled_item_classes`-style filtering is premature until more item
    # classes than APT_PACKAGE exist, so the schema is an empty object on purpose rather
    # than inventing keys with no consumer.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        self._probe = AptProbe(self.source, self.target)
        self._files = TargetFiles(self.target)
        self._refresh = MetadataRefresh()
        self._esm = EsmGate(
            probe=self._probe,
            machines=self.machines,
            job_name=self.name,
            manager_id=self.manager_id,
            log=self._log,
        )
        # `{filename: RepoConflict}` for the differing repository files that feed
        # machine-specific packages (ruling 6). Captured in `plan()`, consumed by the
        # conflict review group and then by the derived write set.
        self._conflicts: dict[str, RepoConflict] = {}
        # `{filename: content}` for the pin files this run OFFERS to delete, read by
        # `diff_apt_pins` and shown whole on that screen — a pin filename alone gives the
        # user nothing to decide from.
        self._pin_contents: dict[str, str] = {}
        # `{package name: the version the SOURCE holds it at}` for the packages this run
        # proposes to install and the source holds (`PKG-FR-APT-HOLD-VERSION`).
        self._held_versions: dict[str, str] = {}
        # Every package dpkg reports installed on the target, read at most once per run and
        # only by a run that has something to ask it: the hold handling
        # (`PKG-FR-APT-HOLD-VERSION`) and repository-usage counting (`PKG-FR-REPO-DELETE`)
        # share the answer, and a machine holding nothing with no target-only repository
        # pays no command at all. `None` means "not read yet".
        self._target_installed: frozenset[str] | None = None
        self._stale_holds: frozenset[str] = frozenset()
        self._work = self._assemble_work(
            source_facts=OriginFacts.empty(),
            target_facts=OriginFacts.empty(),
            source_repo=RepoFacts.empty(),
            target_repo=RepoFacts.empty(),
            package_owned=frozenset(),
            origins=None,
            collateral=None,
        )

    def _assemble_work(
        self,
        *,
        source_facts: OriginFacts,
        target_facts: OriginFacts,
        source_repo: RepoFacts,
        target_repo: RepoFacts,
        package_owned: frozenset[str],
        origins: OriginClassifier | None,
        collateral: Collateral | None,
        stale_holds: frozenset[str] = frozenset(),
    ) -> _Work:
        """Wire the collaborators over one set of captured facts.

        `origins` and `collateral` are passed in when `plan()` has already had to build them
        earlier — the origin classification is an input to the package diff, and the collateral
        rehearsal runs before the `/etc/apt` capture — and built here for the empty set.
        """
        if origins is None:
            origins = OriginClassifier(
                machines=self.machines,
                source_refs=source_facts.refs,
                target_refs=target_facts.refs,
                source_keys=source_facts.keys,
            )
        if collateral is None:
            collateral = Collateral(
                target=self.target, machines=self.machines, target_manual_set=frozenset(), origins=origins
            )
        derived = DerivedWrites(
            source_origin_facts=source_facts,
            target_origin_facts=target_facts,
            source_repo_facts=source_repo,
            target_repo_facts=target_repo,
            origins=origins,
            machines=self.machines,
        )
        keyrings = Keyrings(
            source_keys=source_facts.keys,
            target_keys=target_facts.keys,
            source_refs=source_facts.refs,
            target_refs=target_facts.refs,
            package_owned=package_owned,
            probe=self._probe,
            files=self._files,
            log=self._log,
            machines=self.machines,
        )
        return _Work(
            source_facts=source_facts,
            target_facts=target_facts,
            origins=origins,
            keyrings=keyrings,
            derived=derived,
            collateral=collateral,
            etc_apt=EtcApt(
                target=self.target,
                files=self._files,
                keyrings=keyrings,
                derived=derived,
                refresh=self._refresh,
                log=self._log,
                machines=self.machines,
            ),
            packages=PackageConverger(
                target=self.target,
                manager_id=self.manager_id,
                machines=self.machines,
                collateral=collateral,
                derived=derived,
                origins=origins,
                refresh=self._refresh,
                held_versions=self._held_versions,
                stale_holds=stale_holds,
            ),
        )

    # -- plan ---------------------------------------------------------------------------

    @override
    async def plan(self) -> PackagePlan:
        """Read-only. The package diff, extended with plan-time transaction-collateral
        classification (D-30) and the `/etc/apt` item classes (D-11/D-12/D-13).

        Unreproducible detection is NOT apt's business (D-18): it moved to
        `manual_installs_sync` with its own enable flag, so this job never emits an
        `UNREPRODUCIBLE` diff.

        The origin state is captured FIRST, ahead of the package diff: a package's diff class
        depends on which repository file on the source declares its origin (D-34), so the
        `/etc/apt` reference scans are an input to the package diff rather than a by-product of
        the repository one.

        The ESM gate (D-38) runs next, for three reasons that each rule out a later spot:
        one of its answers ends the job, so it must precede the expensive planning and a
        review the user would otherwise answer for nothing; its probe is a read, and this is
        the last read-only phase; and it puts the target's environment problem and its
        copy-paste remedy on screen before anything is approved or written. Not `validate()`
        — every `ValidationError` is fatal (`orchestrator.py`, `models.py`), so there is no
        way to express "the user answered, carry on".

        Collateral classification runs AFTER the base diff and BEFORE review groups are
        (re)built, so every manual-collateral package becomes its own three-way review item
        decided in the SAME review the user approves from — never a prompt during apply.
        """
        source_facts, target_facts = await self._probe.capture_origin_state()

        pending_esm = self._esm.pending(source_facts.source_digests, target_facts.source_digests)
        if pending_esm and not await self._esm.allow(pending_esm, context=self.context):
            # Dry run only (`EsmGate.allow`): a real run has raised by now. Withholding
            # keeps the preview from claiming writes no real run would make.
            self._esm.withhold(pending_esm)

        origins = OriginClassifier(
            machines=self.machines,
            source_refs=source_facts.refs,
            target_refs=target_facts.refs,
            source_keys=source_facts.keys,
        )
        base_plan = await self._plan_packages(origins)

        target_manual_set = await self._probe.capture_target_manual_set()
        collateral = Collateral(
            target=self.target,
            machines=self.machines,
            target_manual_set=target_manual_set,
            origins=origins,
            marked=self._target_marked_packages(),
            stale_holds=self._stale_holds,
            log=self._log,
        )
        collateral_diffs = await collateral.plan_time(base_plan.diffs)

        repo_diffs = await self._plan_repo_diffs(source_facts, target_facts, origins, collateral, base_plan.diffs)

        if not collateral_diffs and not repo_diffs and not self._conflicts:
            return base_plan

        # Ordering is an apt FACT (key before source before packages, T-02-16), not a
        # general one: the base loop stays a plain item-by-item iterator, and THIS job
        # sorts its own diffs before they reach it. `sorted` is stable, so within one
        # rank (e.g. every APT_PACKAGE diff, or every APT_PIN/APT_CONFIG diff) the
        # original relative order — base diff, then collateral, then repo diffs — is
        # preserved.
        # This job's OWN extra diffs (repo files) also need the D-08a inertness pass the
        # package half already ran over its diffs — they are derived from directory digests,
        # so no input item carried their id into `filter_inert`. The decision files
        # `_plan_packages` just read are reused rather than re-read.
        all_diffs = self._drop_inert_diffs(
            sorted(
                (*base_plan.diffs, *collateral_diffs, *repo_diffs),
                key=lambda diff: ITEM_CLASS_ORDER.get(diff.item_class, 3),
            ),
            *self._plan_decisions,
        )
        return PackagePlan(manager=self.manager_id, diffs=all_diffs, groups=self._build_review_groups(all_diffs))

    async def _plan_packages(self, origins: OriginClassifier) -> PackagePlan:
        """The package half: load decision files -> capture -> query -> diff -> build review
        groups. Read-only.

        Both machines' decision files are loaded first (a read, like everything else here)
        and each side's captured/queried items are filtered through its OWN file before
        diffing (D-08): an item recorded on the source is dropped from the source
        manifest so it is never pushed to a peer again; an item recorded on the target
        is dropped from the target query so it is never proposed for
        install/remove/change again — either way it produces no `ItemDiff` and never
        reaches the review.

        The finished diffs go through `_drop_inert_diffs` as well, which catches the
        recorded items no input-side filter can see: the `apt:hold:` membership items are
        derived from hold-set membership, and the post-diff pass is the only CORRECT place
        for them anyway — the target hold set additionally suppresses a held package's own
        install/upgrade action, so filtering that input set would re-propose upgrading a
        held package. `plan()` runs the same pass again over the repository and collateral
        diffs it appends.
        """
        self._target_installed = None
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        target_decisions = await DecisionFile(self.manager_id, self.target).load()
        self._plan_decisions = (source_decisions, target_decisions)

        captured, source_origins = await self._probe.capture_source_items()
        source_items = await filter_inert(captured, source_decisions)
        target_items = await filter_inert(await self._probe.query_target_items(), target_decisions)
        source_hold_names, target_hold_names = await self._probe.collect_hold_sets()
        # A hold naming a package the target does not have is dpkg selection state that
        # freezes nothing and refuses the install the source is asking for. Kept apart from
        # the real holds here, once, so the diff and the converger agree on which is which.
        self._stale_holds = frozenset()
        if target_hold_names:
            self._stale_holds = target_hold_names - await self._installed_on_target()
        policy = await self._probe.collect_target_policy([item.name for item in source_items])
        origin_plan = origins.classify(source_items, target_items, policy, source_origins)
        diffs = self._drop_inert_diffs(
            diff_apt_packages(
                source_items,
                target_items,
                origin_plan,
                self.machines,
                source_hold_names,
                target_hold_names,
                self._stale_holds,
            ),
            source_decisions,
            target_decisions,
        )
        # `PKG-FR-APT-HOLD-VERSION`: a package the source HOLDS and the target lacks must be
        # installed at the source's own version, because the hold that follows would
        # otherwise freeze the target permanently on whatever its repositories happened to
        # offer, and nothing moves a held package again. Read off the surviving diffs so a
        # package `filter_inert` dropped cannot pin a version nothing will install.
        source_versions = {item.name: item.version for item in source_items if item.version}
        self._held_versions = {
            name: source_versions[name]
            for diff in diffs
            if diff.item_class is ItemClass.APT_PACKAGE and diff.action is DiffAction.INSTALL
            for name in [package_name(diff.item_id)]
            if name in source_hold_names and name in source_versions
        }
        return PackagePlan(manager=self.manager_id, diffs=diffs, groups=self._build_review_groups(diffs))

    async def _installed_on_target(self) -> frozenset[str]:
        """The target's installed package set, read once per run and only when asked for."""
        if self._target_installed is None:
            self._target_installed = await self._probe.capture_target_installed()
        return self._target_installed

    def _target_marked_packages(self) -> frozenset[str]:
        """Package names the TARGET recorded machine-specific, from the decision file
        `_plan_packages` has just read (`PKG-FR-COLLATERAL-MARKED`).

        These never reach a review of their own — `filter_inert` drops them from the target
        manifest before anything is diffed — so the collateral question is the only place
        the user can be told a mark is about to be overrun.
        """
        _source_decisions, target_decisions = self._plan_decisions
        return frozenset(
            package_name(item_id) for item_id in target_decisions if item_id.startswith(APT_PACKAGE_ID_PREFIX)
        )

    @staticmethod
    def _files_an_approval_would_write(package_diffs: Sequence[ItemDiff], origins: OriginClassifier) -> frozenset[str]:
        """The repository filenames this run would derive if the review approved every
        install it proposes — a superset of what `DerivedWrites.build` finally writes, since
        the review has not happened yet.

        Gating the conflict question on it is what keeps D-37's rule intact: a repository
        travels because an approved package comes from it, so a file no install needs is not
        a question, and answering "overwrite" cannot by itself make one travel.
        """
        return frozenset(
            filename
            for diff in package_diffs
            if diff.item_class is ItemClass.APT_PACKAGE and diff.action is DiffAction.INSTALL
            for filename in origins.plans.get(diff.item_id, OriginPlan()).derived_files
        )

    async def _plan_repo_diffs(
        self,
        source_facts: OriginFacts,
        target_facts: OriginFacts,
        origins: OriginClassifier,
        collateral: Collateral,
        package_diffs: Sequence[ItemDiff],
    ) -> list[ItemDiff]:
        """Capture the two remaining `/etc/apt` directories and diff the item classes that
        still HAVE a review direction (D-11/D-13, ADR-020 D-37), by whole-file digest.

        Two of the three are now removal-only. A repository or pin the source has travels
        because a package needs it or because pins always travel, neither of which is a
        question; apt config keeps all three directions, because no package implies whether
        a proxy or a `no-install-recommends` policy should be replicated (D-37).

        Both surviving repository questions are narrowed here, against what the TARGET still
        installs, before any diff is built:

        - a repository the source no longer has is WITHHELD outright while the target still
          gets software from it (`PKG-FR-REPO-DELETE`). "Anything" is every package installed
          there, automatic ones included, plus its machine-specific marks. Usage is counted
          after this run's own removal candidates, which is the approve-everything reading
          the review has not happened yet to improve on; marks count as usage always, since
          they are never removal candidates.
        - a repository the two machines disagree about becomes a question only when it is
          also one this run would write for an approved package (`PKG-FR-REPO-CONFLICT`), the
          same gate `flatpak_sync._capture_remote_conflicts` applies. Every other differing
          file is overwritten silently under D-37, so asking about it would put a decision to
          the user that changes nothing.

        This is also where the collaborators that need the full `/etc/apt` picture are
        assembled, since every fact they decide over exists by the end of it.
        """
        source_repo, target_repo = await self._probe.capture_repo_state()
        package_owned = await self._probe.capture_package_owned_keys(target_facts.keys)

        source_sources = source_facts.source_digests
        target_sources = target_facts.source_digests
        extra = frozenset(target_sources) - frozenset(source_sources) - DISTRO_SOURCE_FILENAMES
        changed = frozenset(diff_filenames(source_sources, target_sources).changed)
        if (
            source_facts.sources_list_digest is not None
            and source_facts.sources_list_digest != target_facts.sources_list_digest
        ):
            changed |= {Path(APT_SOURCES_LIST).name}
        marked = self._target_marked_packages()
        # ONE batched policy call answers both follow-ups (§4.4): withholding a repository
        # still in use and triggering the conflict question are the same computation over two
        # filename sets and two package populations.
        #
        # Counted over everything dpkg has installed, not the manual set: `remove_args` runs
        # `apt-get remove`, never `autoremove`, so nothing here takes an automatically-
        # installed package away when its reason goes, and a manual package the user keeps
        # can require it regardless. Deleting its only repository strands it. The wider set
        # costs one `apt-cache policy` over roughly 4000 names instead of a few hundred —
        # measured on `ubuntu:24.04` at 1.1s and 0.9MB of output — paid only by a run that
        # found a target-only or conflicting repository.
        conflict_candidates = changed & self._files_an_approval_would_write(package_diffs, origins)
        counted: frozenset[str] = frozenset()
        if extra or conflict_candidates:
            counted = await self._installed_on_target() | marked
        by_file = await self._probe.packages_by_source_file(
            extra | conflict_candidates, sorted(counted), target_facts.refs
        )
        going = {
            package_name(diff.item_id)
            for diff in package_diffs
            if diff.item_class is ItemClass.APT_PACKAGE
            and diff.action is DiffAction.REMOVE
            and diff.item_id.startswith(APT_PACKAGE_ID_PREFIX)
        }
        in_use: dict[str, list[str]] = {}
        for filename in sorted(extra):
            keeping = [name for name in by_file.get(filename, []) if name not in going]
            if keeping:
                in_use[filename] = keeping
                self._log(
                    Host.TARGET,
                    LogLevel.FULL,
                    f"keeping repository {filename}: {self.machines.target} still installs "
                    f"{', '.join(keeping)} from it, so its deletion is not offered",
                )
        self._conflicts = await self._probe.capture_repo_conflicts(
            {
                filename: [name for name in by_file.get(filename, []) if name in marked]
                for filename in sorted(conflict_candidates)
                if any(name in marked for name in by_file.get(filename, []))
            }
        )

        self._work = self._assemble_work(
            source_facts=source_facts,
            target_facts=target_facts,
            source_repo=source_repo,
            target_repo=target_repo,
            package_owned=package_owned,
            origins=origins,
            collateral=collateral,
            stale_holds=self._stale_holds,
        )

        diffs: list[ItemDiff] = []
        diffs.extend(
            await diff_apt_sources(
                self._probe.target_run, source_sources, target_sources, self.machines, frozenset(in_use)
            )
        )
        pin_diffs, self._pin_contents = await diff_apt_pins(
            self._probe.target_run, source_repo.pin_digests, target_repo.pin_digests
        )
        diffs.extend(pin_diffs)
        diffs.extend(diff_apt_configs(source_repo.conf_digests, target_repo.conf_digests, self.machines))
        return diffs

    @override
    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """Carve apt's two non-standard screens out of the ordinary decision groups.

        Repository and pin DELETIONS (ADR-020 D-07) become
        `REPO_REMOVAL_REVIEW_ACTION` groups: the same decision screen starting at skip-once, but
        offered only two answers because a permanent machine-local mark on a file whose
        purpose is to feed packages would silently change where those packages come from
        forever. Manual-collateral diffs (D-30) become a `COLLATERAL_REVIEW_ACTION` group
        whose entries take the three-way go-ahead / keep-the-package / stop-the-sync
        resolution.

        Both trail the base groups — packages and apt config — so the user sees the bulk of
        the diff before being asked to resolve anything, and collateral comes last because
        it is the only screen that can abort the run.

        The unreproducible carve-out is gone (D-18: that concern moved to
        `manual_installs_sync`).
        """
        collateral = [diff for diff in diffs if is_collateral_diff(diff)]
        removals = [diff for diff in diffs if is_repo_removal_diff(diff)]
        if not collateral and not removals and not self._conflicts:
            return super()._build_review_groups(diffs)

        carved_ids = {diff.item_id for diff in (*collateral, *removals)}
        rest = [diff for diff in diffs if diff.item_id not in carved_ids]
        groups = list(super()._build_review_groups(rest))
        if self._conflicts:
            groups.append(
                ReviewGroup(
                    manager=self.manager_id,
                    action=REPO_CONFLICT_REVIEW_ACTION,
                    title=f"Resolve {self.manager_id} repository conflicts",
                    entries=tuple(
                        ReviewEntry(
                            item_id=f"{CONFLICT_ID_PREFIX}{filename}",
                            label=filename,
                            action_label="overwrite",
                            detail=build_repo_conflict_detail(filename, conflict.packages, self.machines),
                            versions=(conflict.target_version, conflict.source_version),
                        )
                        for filename, conflict in sorted(self._conflicts.items())
                    ),
                )
            )
        for item_class, words in REPO_REMOVAL_VERBS.items():
            entries = [diff for diff in removals if diff.item_class is item_class]
            if not entries:
                continue
            groups.append(
                ReviewGroup(
                    manager=self.manager_id,
                    action=REPO_REMOVAL_REVIEW_ACTION,
                    title=f"Delete {words.plural} {self.machines.source} no longer has ({self.manager_id})",
                    entries=tuple(
                        ReviewEntry(
                            item_id=diff.item_id,
                            label=diff.label,
                            action_label=words.action_label,
                            detail=diff.detail,
                            content=self._pin_contents.get(pin_filename(diff.item_id))
                            if diff.item_class is ItemClass.APT_PIN
                            else None,
                        )
                        for diff in entries
                    ),
                )
            )
        if collateral:
            groups.append(
                ReviewGroup(
                    manager=self.manager_id,
                    action=COLLATERAL_REVIEW_ACTION,
                    # Both of `Collateral.protected`'s grounds, because one group can hold
                    # both: a package a mark alone protects is not one the user installed
                    # there. Which ground holds for a given entry is its own detail line
                    # (`Collateral._reason`).
                    title=(
                        f"Packages you installed on {self.machines.target} or marked as its own that this sync "
                        f"would remove, downgrade or upgrade ({self.manager_id})"
                    ),
                    entries=tuple(
                        ReviewEntry(
                            item_id=diff.item_id,
                            label=diff.label,
                            action_label=diff.act_word or "resolve",
                            detail=diff.detail,
                            answer_hints=diff.answer_hints,
                        )
                        for diff in collateral
                    ),
                )
            )
        return tuple(groups)

    # -- review -> work -----------------------------------------------------------------

    @override
    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Insert the synthetic metadata-refresh diff once the decisions are known, so it
        flows through the same per-item logging, dry-run gate and failure collection as
        everything else (`apply()`'s existing loop) instead of being a special case bolted
        onto the end.

        Runs AFTER `plan()` (so decisions exist) and is exactly where D-24's review
        already stopped being relevant for THIS item — the refresh is infrastructure
        the user never ticks, not a repository they decided about. Positioned immediately
        after the last non-package diff (repository group already sorted
        pin/config-before-source by `plan()`) and before every package diff, matching
        apt's own dependency order: metadata must be current before anything installs
        from it.

        The marker is ALSO what carries the work no diff represents: the derived writes
        (ADR-020 D-37/D-38 — a repository, a pin or a distribution file travels without a
        review line, so nothing else would ever route into the repository unit), and a
        rotated keyring, which changes no source file at all. `Keyrings.pending_work` is a
        superset test — the unit recomputes the exact set from the real decisions and returns
        early if it is empty — so the cost of a false positive is one no-op call.

        Manual-collateral decisions (D-30) are resolved first: a go-ahead on a
        collateral item marks its package approved so the apply-time guard lets the
        removal through, while a skip is translated into `SKIP_ONCE` on the approved
        packages that cause that collateral, so a declined collateral cleanly leaves them
        unapproved rather than failing them at the guard.
        """
        work = self._work
        outcome = work.collateral.resolve(plan.diffs, outcome)
        work.derived.build(plan.diffs, outcome.decisions, conflicts=self._conflicts, withheld_esm=self._esm.withheld)
        approved_group = any(
            diff.item_class in REPO_GROUP_CLASSES
            and diff.item_id != METADATA_REFRESH_ITEM_ID
            and outcome.decisions.get(diff.item_id) == Decision.APPLY
            for diff in plan.diffs
        )
        if approved_group or work.derived.all_writes() or work.keyrings.pending_work():
            marker = metadata_refresh_diff()
            # Repo-group items sort before the metadata refresh, packages after it, and
            # holds LAST (#208, D8: install-before-hold). Holds are neither package nor
            # repo-group, so they are pulled out explicitly rather than folding into the
            # pre-marker `non_package` bucket, which would converge them before installs.
            repo_like = [
                diff for diff in plan.diffs if diff.item_class not in (ItemClass.APT_PACKAGE, ItemClass.APT_HOLD)
            ]
            package = [diff for diff in plan.diffs if diff.item_class == ItemClass.APT_PACKAGE]
            holds = [diff for diff in plan.diffs if diff.item_class == ItemClass.APT_HOLD]
            plan = PackagePlan(manager=plan.manager, diffs=(*repo_like, marker, *package, *holds), groups=plan.groups)
            outcome = ReviewOutcome(
                decisions={**outcome.decisions, marker.item_id: Decision.APPLY},
                was_interactive=outcome.was_interactive,
                # Carried through verbatim — rebuilding `decisions` above must not drop
                # this run's authored snippets/unresolved items.
                snippets=outcome.snippets,
                unresolved=outcome.unresolved,
            )
        super().accept_review(plan, outcome)

    @override
    async def _record_permanent_skips(self, plan: PackagePlan, decisions: Mapping[str, Decision]) -> None:
        """The base recording pass, minus every `apt:source:`/`apt:pin:` id (ADR-020 D-07).

        The interactive flow already cannot produce a `SKIP_ALWAYS` for one — their groups
        are absent from `_PROMOTABLE_ACTIONS`, so the promotion screen never offers them —
        but "no registry entry" is a property of the model, not of one prompt's wiring, and
        a decision can also arrive from the review's automation hook or from a caller
        assembling a `ReviewOutcome` by hand. Filtered by id prefix rather than by action so
        it holds in EVERY direction, including ones this job no longer emits.

        `apt:config:` is deliberately not filtered: it keeps the full three-way decision and
        the machine-local registry, because no approved package implies whether a proxy or a
        recommends policy should travel (D-37).
        """
        recordable = PackagePlan(
            manager=plan.manager,
            diffs=tuple(diff for diff in plan.diffs if not diff.item_id.startswith(UNRECORDABLE_ITEM_ID_PREFIXES)),
            groups=plan.groups,
        )
        await super()._record_permanent_skips(recordable, decisions)

    # -- apply --------------------------------------------------------------------------

    @override
    async def apply(self) -> None:
        """The base converge loop, preceded under dry-run by the derived `/etc/apt` writes
        and the signing keys that travel with them.

        A derived write is not a diff, so the base loop has nothing to say about it, and
        ADR-014 makes the preview the whole report of a rehearsal: without this, a run whose
        entire `/etc/apt` work is derived would preview an `apt-get update` and no reason
        for it. Keys are here for the stronger version of the same reason — a key has no diff
        in ANY run, so without this line a rotated key would reach a real target having been
        previewed nowhere (`PKG-FR-DERIVED-VISIBLE`). On a real run the same facts are logged
        as each file lands, which is the honest place for them — the write may still fail.
        """
        if self.context.dry_run:
            work = self._work
            for dest in work.derived.all_writes():
                self._log(Host.TARGET, LogLevel.FULL, f"[dry-run] Would write {dest} from {self.machines.source}")
            if self._accepted_plan is not None and self._accepted_outcome is not None:
                diffs = self._accepted_plan.diffs
                decisions = self._accepted_outcome.decisions
                surviving = work.keyrings.surviving_refs(diffs, decisions, work.derived.written_source_filenames)
                for _local, dest in work.keyrings.writes(surviving):
                    self._log(
                        Host.TARGET,
                        LogLevel.FULL,
                        f"[dry-run] Would write signing key {dest} from {self.machines.source}",
                    )
                if any(
                    diff.item_class is ItemClass.APT_SOURCE
                    and diff.item_id != METADATA_REFRESH_ITEM_ID
                    and diff.action is DiffAction.REMOVE
                    and decisions.get(diff.item_id) == Decision.APPLY
                    for diff in diffs
                ):
                    for dest in work.keyrings.unreferenced(surviving):
                        self._log(
                            Host.TARGET,
                            LogLevel.FULL,
                            f"[dry-run] Would delete signing key {dest}, which no repository would reference",
                        )
        await super().apply()

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Route one approved diff to whatever converges it.

        Hold items (`apt:hold:<name>`) are routed FIRST, by item_id prefix, so an
        `apt:hold:` INSTALL runs `apt-mark hold` rather than falling into the action-based
        `apt-get install` dispatch (#208, D4). Repository-group items (pins, apt config,
        sources) and the synthetic metadata-refresh marker converge as one ordered,
        transactional unit instead — the unit that also provisions and collects signing keys
        around its own writes. Unreproducible items are not apt's concern (D-18) —
        `manual_installs_sync` owns their snippet replay — so this only ever sees hold,
        repository-group, `INSTALL` or `REMOVE` diffs.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        diffs = self._accepted_plan.diffs
        decisions = self._accepted_outcome.decisions
        work = self._work

        if diff.item_id.startswith(APT_HOLD_ID_PREFIX):
            return await work.packages.hold(diff, diffs, decisions)
        if diff.item_class in REPO_GROUP_CLASSES or diff.item_id == METADATA_REFRESH_ITEM_ID:
            return await work.etc_apt.converge_item(diff, diffs, decisions)
        if diff.action == DiffAction.INSTALL:
            return await work.packages.install(diff, diffs, decisions)
        if diff.action == DiffAction.REMOVE:
            return await work.packages.remove(diff, diffs, decisions)
        raise ConvergeItemFailed(
            f"AptSyncJob.converge: unsupported action {diff.action.value!r} for {diff.label} "
            "(only 'install' and 'remove' exist for apt packages)"
        )

    # -- validate -----------------------------------------------------------------------

    @override
    async def validate(self) -> list[ValidationError]:
        """apt-mark availability on both ends, sudo on both ends, dpkg lock free on target.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `folder_sync.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        source_check = await self.source.run_command("apt-mark --version")
        if not source_check.success:
            errors.append(self._validation_error(Host.SOURCE, "apt-mark is not available on source"))

        target_check = await self.target.run_command("apt-mark --version", login_shell=False)
        if not target_check.success:
            errors.append(self._validation_error(Host.TARGET, "apt-mark is not available on target"))

        # Source-side sudo matters even though the source is never mutated: capturing
        # /etc/apt config runs `sudo find` there, and without passwordless sudo that
        # capture degrades to empty digest maps rather than failing. The sync would then
        # report success having replicated no repository configuration at all — a silent
        # wrong-result, which is worse than refusing to start.
        source_sudo_check = await self.source.run_command("sudo --non-interactive true")
        if not source_sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE,
                    "passwordless sudo is not available on source "
                    "(required to read /etc/apt repository, keyring and pin config).\n"
                    + passwordless_sudo_hint(SOURCE_SUDO_COMMANDS),
                )
            )

        sudo_check = await self.target.run_command("sudo --non-interactive true", login_shell=False)
        if not sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "passwordless sudo is not available on target "
                    "(required to install packages and write /etc/apt config).\n"
                    + passwordless_sudo_hint(TARGET_SUDO_COMMANDS, user=self.context.target_username),
                )
            )

        # fuser exits 0 when the file IS held by at least one process, non-zero when
        # free (man fuser EXIT CODES) — read-only probe, no lock is acquired or released.
        lock_check = await self.target.run_command("sudo fuser /var/lib/dpkg/lock-frontend", login_shell=False)
        if lock_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "dpkg frontend lock is held on target (likely unattended-upgrades); "
                    "retry once it finishes (RESEARCH Pitfall 5)",
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): the manual-install set."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["apt packages (manually-installed set)"],
            mechanism="apt-get install/remove per item, after review",
        )
