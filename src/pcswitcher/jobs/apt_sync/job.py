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
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, override

from pcswitcher.jobs.apt_sync.collateral import Collateral, LateCollateral
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
    APT_CONFIG_ID_PREFIX,
    APT_HOLD_ID_PREFIX,
    APT_PACKAGE_ID_PREFIX,
    APT_SOURCE_ID_PREFIX,
    APT_SOURCES_LIST,
    CONFLICT_ID_PREFIX,
    DISTRO_SOURCE_FILENAMES,
    ITEM_CLASS_ORDER,
    METADATA_REFRESH_ITEM_ID,
    REPO_GROUP_CLASSES,
    REPO_REMOVAL_VERBS,
    UNRECORDABLE_ITEM_ID_PREFIXES,
    config_filename,
    is_collateral_diff,
    is_repo_removal_diff,
    package_name,
    pin_filename,
)
from pcswitcher.jobs.apt_sync.keyrings import Keyrings
from pcswitcher.jobs.apt_sync.messages import build_collateral_group_title, build_repo_conflict_detail
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
    change_title,
)
from pcswitcher.jobs.packages.state import DecisionEntry, filter_inert, marks_on_either
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, LogLevel, SyncAborted, ValidationError
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
    display_name: ClassVar[str] = "Apt packages"
    manager_id: ClassVar[str] = "apt"
    item_noun: ClassVar[str] = "apt package"
    item_noun_plural: ClassVar[str] = "apt packages"

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
        self._probe = AptProbe(self.source, self.target, self.machines)
        self._files = TargetFiles(self.target)
        self._refresh = MetadataRefresh()
        self._esm = EsmGate(
            probe=self._probe,
            machines=self.machines,
            job_name=self.name,
            job_display_name=self.display_name,
            log=self._log,
        )
        # `{filename: RepoConflict}` for the differing repository files that feed
        # machine-specific packages (ruling 6). Captured in `plan()` over every file a
        # PROPOSED install would write, narrowed by `plan_second_round` to the ones an
        # APPROVED install writes (`PKG-FR-REPO-CONFLICT`), then consumed by the conflict
        # review group and the derived write set. Narrowed rather than captured late so a
        # path that never reaches the second round still treats the file as unanswered,
        # which fails the packages that needed it rather than overwriting it silently.
        self._conflicts: dict[str, RepoConflict] = {}
        # `{filename: content}` for the pin files this run OFFERS to delete, read by
        # `diff_apt_pins` and shown whole on that screen — a pin filename alone gives the
        # user nothing to decide from.
        self._pin_contents: dict[str, str] = {}
        # `{filename: (the target's body, the source's body)}` for the `apt.conf.d` files
        # both machines have with different content, read by `diff_apt_configs` and printed
        # whole on the screen that offers the overwrite — target-first, the order
        # `ReviewEntry.versions` is defined in.
        self._conf_bodies: dict[str, tuple[str, str]] = {}
        # `{package name: the version the SOURCE holds it at}` for the packages this run
        # proposes to install and the source holds (`PKG-FR-APT-HOLD-VERSION`).
        self._held_versions: dict[str, str] = {}
        # `{filename: the target packages installed from it}` for every target-only
        # repository this run considered offering for deletion, counted before any removal
        # was taken out. `PKG-FR-REPO-DELETE` counts usage after this run's APPROVED
        # removals, which exist only once the first review round has returned, so the count
        # is taken again there: `_plan_repo_diffs` decides which files are even candidates,
        # `plan_second_round` decides which are raised at all.
        self._repo_users: dict[str, list[str]] = {}
        # Every package dpkg reports installed on the target, read at most once per run and
        # only by a run that has something to ask it: the bookkeeping-hold check
        # (`PKG-FR-HOLD-WITHOUT-PACKAGE`) and repository-usage counting
        # (`PKG-FR-REPO-DELETE`) share the answer, and a machine holding nothing with no
        # target-only repository pays no command at all. `None` means "not read yet".
        self._target_installed: frozenset[str] | None = None
        self._work = self._assemble_work(
            source_facts=OriginFacts.empty(),
            target_facts=OriginFacts.empty(),
            source_repo=RepoFacts.empty(),
            target_repo=RepoFacts.empty(),
            distribution_owned=frozenset(),
            origins=None,
            collateral=None,
        )

    def _assemble_work(  # noqa: PLR0913 - one set of captured facts wired into the collaborators; all keyword-only
        self,
        *,
        source_facts: OriginFacts,
        target_facts: OriginFacts,
        source_repo: RepoFacts,
        target_repo: RepoFacts,
        distribution_owned: frozenset[str],
        origins: OriginClassifier | None,
        collateral: Collateral | None,
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
            distribution_owned=distribution_owned,
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
                late=LateCollateral(
                    collateral=collateral,
                    origins=origins,
                    derived=derived,
                    machines=self.machines,
                    manager_id=self.manager_id,
                    reviewer=self.context.reviewer,
                    refresh=partial(self._refresh.ensure, self.target, self.manager_id),
                    log=self._log,
                ),
                held_versions=self._held_versions,
            ),
        )

    # -- plan ---------------------------------------------------------------------------

    @override
    async def plan(self) -> PackagePlan:
        """Read-only. The package diff, extended with plan-time transaction-collateral
        classification (`PKG-FR-COLLATERAL-MANUAL`) and the `/etc/apt` item classes
        (`PKG-FR-REPO-DERIVED`/`PKG-FR-KEY-COPY`/`PKG-FR-APT-IGNORES`).

        Unreproducible detection is NOT apt's business (`PKG-FR-MANUAL-SCOPE`): it moved to
        `manual_installs_sync` with its own enable flag, so this job never emits an
        `UNREPRODUCIBLE` diff.

        The origin state is captured FIRST, ahead of the package diff: a package's diff class
        depends on which repository file on the source declares its origin (`PKG-FR-APT-IDENTITY`), so the
        `/etc/apt` reference scans are an input to the package diff rather than a by-product of
        the repository one.

        The ESM gate (`PKG-FR-DISTRO-FILES`) runs next, for three reasons that each rule out a later spot:
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
            log=self._log,
        )
        collateral_diffs = await collateral.plan_time(base_plan.diffs)

        repo_diffs = await self._plan_repo_diffs(source_facts, target_facts, origins, collateral, base_plan.diffs)

        if not collateral_diffs and not repo_diffs:
            return base_plan

        # Ordering is an apt FACT (key before source before packages, T-02-16), not a
        # general one: the base loop stays a plain item-by-item iterator, and THIS job
        # sorts its own diffs before they reach it. `sorted` is stable, so within one
        # rank (e.g. every APT_PACKAGE diff, or every APT_PIN/APT_CONFIG diff) the
        # original relative order — base diff, then collateral, then repo diffs — is
        # preserved.
        # This job's OWN extra diffs (repo files) also need the `PKG-FR-MACHINE-SPECIFIC` inertness pass the
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

        Both machines' decision files are loaded first (a read, like everything else here),
        with any mark whose package or config file that machine no longer has left out
        (`_load_live_decisions`), and each side's captured/queried items are filtered
        through its OWN file before
        diffing (`PKG-FR-MACHINE-SPECIFIC`): an item recorded on the source is dropped from the source
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
        source_decisions, target_decisions = await self._load_live_decisions()

        # Both files against BOTH manifests (`marks_on_either`): a package both machines
        # have must vanish from the diff entirely once either machine records it, and
        # filtering each manifest by its own file alone leaves the other machine's copy
        # unmatched — an install of a package the target already has, or a removal of one
        # the source still has.
        marked = marks_on_either(source_decisions, target_decisions)
        captured, source_origins, source_no_repository = await self._probe.capture_source_items()
        # ONE verdict per NAME, applied to BOTH manifests (`PKG-FR-DEB-OWNERSHIP`): the
        # source's answer decides for every name the source HAS, and only a name the source
        # does not have is decided by the target's own. Asking each machine separately gives
        # one name two verdicts, and a name withheld from one manifest while the other keeps
        # it is exactly a removal — which is what an installed version the archive had
        # superseded produced on whichever machine lagged a phased update (#285).
        source_names = {item.name for item in captured}
        source_items = [item for item in await filter_inert(captured, marked) if item.name not in source_no_repository]
        # The target is asked about the surviving source names only: a name already withheld
        # must reach no downstream target read, the policy probe included.
        target_captured, policy = await self._probe.capture_target_items([item.name for item in source_items])
        withheld = source_no_repository | {n for n in policy.no_repository_can_install if n not in source_names}
        target_items = [item for item in await filter_inert(target_captured, marked) if item.name not in withheld]
        source_hold_names, target_hold_names = await self._probe.collect_hold_sets()
        await self._refuse_holds_without_their_package(source_hold_names, target_hold_names)
        origin_plan = origins.classify(source_items, target_items, policy, source_origins)
        diffs = self._drop_inert_diffs(
            diff_apt_packages(
                source_items,
                target_items,
                origin_plan,
                self.machines,
                source_hold_names=source_hold_names,
                target_hold_names=target_hold_names,
                source_marked_packages=self._marked_packages(source_decisions),
                target_marked_packages=self._marked_packages(target_decisions),
            ),
            source_decisions,
            target_decisions,
        )
        # `PKG-FR-APT-HOLD-VERSION`: a package the source HOLDS and the target lacks must be
        # installed at the source's own version, because the hold that follows would
        # otherwise freeze the target permanently on whatever its repositories happened to
        # offer, and nothing moves a held package again. Read off the surviving diffs so a
        # package `filter_inert` dropped cannot pin a version nothing will install.
        #
        # A held candidate enters this map whatever `dpkg-query` said about it, empty version
        # included: dropping it here is what let the install float onto whatever the target
        # offered, which the article forbids. `PackageConverger._install` refuses the empty
        # entry instead of falling back.
        source_versions = {item.name: item.version for item in source_items}
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

    async def _refuse_holds_without_their_package(
        self, source_hold_names: frozenset[str], target_hold_names: frozenset[str]
    ) -> None:
        """End the run over a hold naming a package that machine does not have
        (`PKG-FR-HOLD-WITHOUT-PACKAGE`), on either machine.

        Such a hold freezes nothing — there is no installed version to freeze — while
        refusing every later attempt to install the name, on the machine that carries it and,
        once replicated, on the other. It is a bookkeeping failure the user has to clear, so
        the run ends and says which package on which machine, rather than carrying a branch
        for a state that should not exist.

        Raised while planning, before anything is written, which is where the reads that
        answer it already are. Each machine's installed set is read only if that machine
        holds something at all, so an ordinary run pays nothing; the target's is the same
        single read `PKG-FR-REPO-DELETE`'s usage count uses.

        `SyncAborted` for the same reason an unparsable snippet registry raises it
        (`PKG-FR-REGISTRY-CONSENT`): the run must end for the user to repair something by
        hand, which is not this job failing and not the tool breaking.

        Both machines are scanned before anything is raised, and the one abort names every
        stray hold on each of them: a user told about one machine would clear it, sync again,
        and only then be told about the other. The two remediations stay separate lines
        because each `apt-mark unhold` runs on its own machine.
        """
        stray: list[tuple[str, frozenset[str]]] = []
        if source_hold_names:
            stray.append((self.machines.source, source_hold_names - await self._probe.capture_source_installed()))
        if target_hold_names:
            stray.append((self.machines.target, target_hold_names - await self._installed_on_target()))
        found = [(machine, sorted(names)) for machine, names in stray if names]
        if not found:
            return
        remediation = "\n".join(
            f"  {machine}: {', '.join(names)} — clear with `sudo apt-mark unhold {' '.join(names)}`"
            for machine, names in found
        )
        raise SyncAborted(
            "apt holds naming packages the machine does not have installed:\n"
            f"{remediation}\n"
            "A hold on a package the machine lacks freezes nothing and refuses every later attempt to "
            "install it. Clear every hold listed above, then sync again."
        )

    @staticmethod
    def _marked_packages(decisions: Mapping[str, DecisionEntry]) -> frozenset[str]:
        """The package NAMES one machine's decision file records machine-specific.

        Two callers need the same fact about different files: the collateral question reads
        the target's (`PKG-FR-COLLATERAL-MARKED`), and the hold diff reads each machine's own
        so a mark on a package makes its hold inert too (`diff_apt_holds`).
        """
        return frozenset(package_name(item_id) for item_id in decisions if item_id.startswith(APT_PACKAGE_ID_PREFIX))

    def _target_marked_packages(self) -> frozenset[str]:
        """Package names the TARGET recorded machine-specific, from the decision file
        `_plan_packages` has just read (`PKG-FR-COLLATERAL-MARKED`).

        These never reach a review of their own — `filter_inert` drops them from the target
        manifest before anything is diffed — so the collateral question is the only place
        the user can be told a mark is about to be overrun.
        """
        _source_decisions, target_decisions = self._plan_decisions
        return self._marked_packages(target_decisions)

    @override
    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """The marked apt items one machine no longer has: a package dpkg does not report as
        installed, an `/etc/apt/apt.conf.d` file that is not there.

        The installed set comes from `capture_*_installed` (dpkg's whole status listing) and
        NOT from this job's own manifest, which is `apt-mark showmanual`. The two differ for
        a package that is installed but automatically so, and reading the narrower one as
        "not installed" would drop the mark on any marked package apt has since reclassified
        — a package the user still has, and still asked to be left alone.

        The other three markable-looking apt classes answer nothing here. `apt:hold:` cannot
        be recorded at all (`PKG-FR-BLOCKS-DERIVED`), and `apt:source:`/`apt:pin:` cannot
        either (`PKG-FR-NO-MARK-ON-ORIGIN`), so an entry naming one is a hand edit this pass
        leaves exactly where it found it.

        Which class an entry belongs to is read off its ID and not off its recorded
        `item_class`, the same way `_marked_packages` reads it: the file is hand-editable, so
        the two can disagree, and the id is the half every consumer keys on.

        Each read is issued only when the file names an item of that class, so the ordinary
        run — decision files holding packages, or holding nothing — costs one command per
        machine at most.
        """
        package_ids = {item_id for item_id in entries if item_id.startswith(APT_PACKAGE_ID_PREFIX)}
        config_ids = {item_id for item_id in entries if item_id.startswith(APT_CONFIG_ID_PREFIX)}

        absent: set[str] = set()
        if package_ids:
            installed = (
                await self._probe.capture_source_installed()
                if on_source
                else await self._probe.capture_target_installed()
            )
            absent |= {item_id for item_id in package_ids if package_name(item_id) not in installed}
        if config_ids:
            filenames = await self._probe.capture_conf_filenames(on_source=on_source)
            absent |= {
                item_id for item_id in config_ids if item_id.removeprefix(APT_CONFIG_ID_PREFIX) not in filenames
            }
        return frozenset(absent)

    @staticmethod
    def _files_an_approval_would_write(package_diffs: Sequence[ItemDiff], origins: OriginClassifier) -> frozenset[str]:
        """The repository filenames the given package diffs would derive a write for.

        Called twice with two different sets, which is what makes `PKG-FR-REPO-CONFLICT`'s
        "only for a repository this run writes because an APPROVED package comes from it"
        exact rather than approximate: `_plan_repo_diffs` passes every proposed install, and
        the answer decides which files are worth reading both machines' copies of;
        `plan_second_round` passes the approved ones, and the answer decides which are
        actually asked about. A repository travels because an approved package comes from it,
        so a file no approved install needs is not a question, and answering "overwrite"
        cannot by itself make one travel.
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
        still HAVE a review direction (`PKG-FR-REPO-DERIVED`/`PKG-FR-APT-IGNORES`/`PKG-FR-REPO-CONFLICT`), by
        whole-file digest.

        Two of the three are now removal-only. A repository or pin the source has travels
        because a package needs it or because pins always travel, neither of which is a
        question; apt config keeps all three directions, because no package implies whether
        a proxy or a `no-install-recommends` policy should be replicated (`PKG-FR-APTCONF`).

        Both surviving repository questions are narrowed here, against what the TARGET still
        installs, before any diff is built — and narrowed AGAIN in `plan_second_round`, which
        is where each article's own gate falls, because both are written in terms of what
        this run approves:

        - a repository the source no longer has is WITHHELD outright while the target still
          gets software from it (`PKG-FR-REPO-DELETE`). "Anything" is every package installed
          there, automatic ones included, plus its machine-specific marks. Usage is counted
          here after this run's removal CANDIDATES, which withholds every file with a user
          this run does not even propose to remove; marks count as usage always, since they
          are never removal candidates. Whether the surviving candidates' removals were
          APPROVED is the second round's count.
        - a repository the two machines disagree about is worth READING both copies of only
          where this run might write it, which is what the proposed installs decide (the same
          gate `flatpak_sync._capture_remote_conflicts` applies). Whether it becomes a
          question is the second round's, against the approved installs
          (`PKG-FR-REPO-CONFLICT`). Every other differing file is overwritten silently under
          `PKG-FR-APTCONF`, so asking about it would put a decision to the user that changes nothing.

        This is also where the collaborators that need the full `/etc/apt` picture are
        assembled, since every fact they decide over exists by the end of it.
        """
        source_repo, target_repo = await self._probe.capture_repo_state()
        distribution_owned = await self._probe.capture_distribution_owned_keys(
            target_facts.keys, source_facts.keys, target_facts.refs.distribution_origins()
        )

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
        # Kept for the post-review recount: the users of a file this run may offer to delete,
        # before any removal is counted out of them.
        self._repo_users = {filename: by_file.get(filename, []) for filename in sorted(extra)}
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
            distribution_owned=distribution_owned,
            origins=origins,
            collateral=collateral,
        )

        diffs: list[ItemDiff] = []
        diffs.extend(
            await diff_apt_sources(
                self._probe.target_run, source_sources, target_sources, self.machines, frozenset(in_use)
            )
        )
        pin_diffs, self._pin_contents = await diff_apt_pins(
            self._probe.target_run, source_repo.pin_digests, target_repo.pin_digests, self.machines
        )
        diffs.extend(pin_diffs)
        config_diffs, self._conf_bodies = await diff_apt_configs(
            self._probe.source_run,
            self._probe.target_run,
            source_repo.conf_digests,
            target_repo.conf_digests,
            self.machines,
        )
        diffs.extend(config_diffs)
        return diffs

    @override
    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """Carve apt's non-standard screens out of the ordinary decision groups — the FIRST
        round's share of them.

        Pin DELETIONS (`PKG-FR-SKIP-ONCE`) become a `REPO_REMOVAL_REVIEW_ACTION` group: the same
        decision screen starting at skip-once, but offered only two answers because a
        permanent machine-local mark on a file whose purpose is to feed packages would
        silently change where those packages come from forever. Manual-collateral diffs
        (`PKG-FR-COLLATERAL-MANUAL`) become a `COLLATERAL_REVIEW_ACTION` group whose entries take the three-way
        apply / keep-the-package / stop-the-sync resolution.

        Both trail the base groups — packages and apt config — so the user sees the bulk of
        the diff before being asked to resolve anything, and collateral comes last because
        it is the only screen that can abort the run.

        The two `/etc/apt` questions whose article scopes them to APPROVED work — a
        repository DELETION and a repository CONFLICT — are absent here and built in
        `plan_second_round`. A repository removal diff is still carved out of the ordinary
        groups, so it never reaches a checkbox screen on the way past.

        The unreproducible carve-out is gone (`PKG-FR-MANUAL-SCOPE`: that concern moved to
        `manual_installs_sync`).
        """
        collateral = [diff for diff in diffs if is_collateral_diff(diff)]
        removals = [diff for diff in diffs if is_repo_removal_diff(diff)]
        if not collateral and not removals:
            return super()._build_review_groups(diffs)

        carved_ids = {diff.item_id for diff in (*collateral, *removals)}
        rest = [diff for diff in diffs if diff.item_id not in carved_ids]
        groups = list(super()._build_review_groups(rest))
        groups.extend(self._repo_removal_groups(removals, ItemClass.APT_PIN))
        if collateral:
            groups.append(self._collateral_group(collateral))
        return tuple(groups)

    @override
    def _entry_versions(self, diff: ItemDiff) -> tuple[str, str] | None:
        """Both machines' bodies for an `apt.conf.d` file they disagree about, so the row
        that offers the overwrite prints the two files instead of their digests (#277).

        Only the CHANGE direction has a pair: `_conf_bodies` holds exactly the files
        `diff_apt_configs` found changed, so an addition or a deletion looks itself up and
        finds nothing.
        """
        if diff.item_class is not ItemClass.APT_CONFIG:
            return None
        return self._conf_bodies.get(config_filename(diff.item_id))

    def _repo_removal_groups(self, removals: Sequence[ItemDiff], *classes: ItemClass) -> list[ReviewGroup]:
        """The two-answer deletion screens for the named `/etc/apt` classes, in
        `REPO_REMOVAL_VERBS` order.

        One sentinel, one group per class: repositories and pins reach the user as separate
        screens with separate titles, and the two are built in different review rounds — a
        pin's deletion depends on nothing anyone answers, a repository's depends on this
        run's approved removals (`PKG-FR-REPO-DELETE`).
        """
        groups: list[ReviewGroup] = []
        for item_class, words in REPO_REMOVAL_VERBS.items():
            if item_class not in classes:
                continue
            entries = [diff for diff in removals if diff.item_class is item_class]
            if not entries:
                continue
            groups.append(
                ReviewGroup(
                    manager=self.manager_id,
                    action=REPO_REMOVAL_REVIEW_ACTION,
                    # The manager belongs INSIDE the sentence — "apt repositories" is what
                    # the files are, while a trailing "(apt)" reads as a tag on the question.
                    title=change_title("delete", words.plural, self.machines.target),
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
        return groups

    def _collateral_group(self, collateral: Sequence[ItemDiff]) -> ReviewGroup:
        """One `COLLATERAL_REVIEW_ACTION` screen over the given manual-collateral diffs."""
        return ReviewGroup(
            manager=self.manager_id,
            action=COLLATERAL_REVIEW_ACTION,
            title=build_collateral_group_title(self.machines, self.manager_id),
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

    def _conflict_group(self) -> ReviewGroup:
        """The two-answer overwrite screen for the repository files still in conflict."""
        return ReviewGroup(
            manager=self.manager_id,
            action=REPO_CONFLICT_REVIEW_ACTION,
            title=change_title("resolve", "apt repository conflicts", self.machines.target),
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

    # -- the second review round --------------------------------------------------------

    @override
    async def plan_second_round(self, plan: PackagePlan, outcome: ReviewOutcome) -> PackagePlan:
        """apt's three questions about the work this run APPROVED, put once the first round's
        answers exist and before anything is written.

        Each of them is an article scoping its question to approvals rather than to
        candidates, which no plan-time computation can hold:

        - `PKG-FR-REPO-DELETE`: a repository the target still gets software from "MUST NOT be
          raised as an item at all", counted after this run's APPROVED removals. Its item is
          dropped here — not merely left unapplied — so the user is never offered a deletion
          the run would then withhold, and the log says what keeps the file.
        - `PKG-FR-REPO-CONFLICT`: the overwrite question is "raised only for a repository this
          run writes because an approved package comes from it". A file whose only install was
          declined is written by nothing, so there is nothing to ask about.
        - `PKG-FR-COLLATERAL-MANUAL`: only a removal the user APPROVED exempts a package from
          the collateral protection, so every other removal candidate — skipped, marked as the
          target's own (`PKG-FR-COLLATERAL-MARKED`), or unanswered — gets the collateral
          question its plan-time exemption as a candidate could not produce
          (`Collateral.after_answers`).

        The first round's manual-collateral answers are resolved before any of it, because
        one of them can withdraw an approved removal (`Collateral.resolve`): counting a
        repository's users against a removal a kept package has already cancelled would raise
        the very item this round exists to withhold. `accept_review` resolves again over the
        final decisions — the same computation, and idempotent — since a collateral question
        put in THIS round can withdraw a removal after it.

        Reads only — one `apt-get --dry-run` for the collateral pass, and nothing at all for
        the two `/etc/apt` questions, whose facts were captured while planning. Nothing here
        may write: the second round is still a review (`PKG-FR-REVIEW-FIRST`).
        """
        work = self._work
        decisions = work.collateral.resolve(plan.diffs, outcome).decisions
        diffs = list(plan.diffs)
        groups: list[ReviewGroup] = []

        kept = self._repositories_still_in_use(plan.diffs, decisions)
        if kept:
            withheld_ids = {f"{APT_SOURCE_ID_PREFIX}{filename}" for filename in kept}
            diffs = [diff for diff in diffs if diff.item_id not in withheld_ids]
        deletions = [diff for diff in diffs if is_repo_removal_diff(diff) and diff.item_class is ItemClass.APT_SOURCE]
        groups.extend(self._repo_removal_groups(deletions, ItemClass.APT_SOURCE))

        written = self._files_an_approval_would_write(
            [diff for diff in plan.diffs if decisions.get(diff.item_id) == Decision.APPLY], work.origins
        )
        self._conflicts = {filename: conflict for filename, conflict in self._conflicts.items() if filename in written}
        if self._conflicts:
            groups.append(self._conflict_group())

        still_protected = await work.collateral.after_answers(plan.diffs, decisions)
        if still_protected:
            diffs.extend(still_protected)
            groups.append(self._collateral_group(still_protected))

        return PackagePlan(manager=plan.manager, diffs=tuple(diffs), groups=tuple(groups))

    def _repositories_still_in_use(
        self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision]
    ) -> dict[str, list[str]]:
        """`{filename: what still installs from it}` for the repository deletions this run
        must not raise, counted after the APPROVED removals (`PKG-FR-REPO-DELETE`).

        The plan-time narrowing already withheld every file with a user this run does not even
        propose to remove. What it could not know is which of the proposed removals the user
        would actually approve: a file whose last user's removal is declined is a file the
        target still installs from, and deleting it would leave an installed package with no
        origin — the outcome the article forbids in the strongest terms it uses about
        repositories.

        Counted against the decisions rather than a fresh read of the machine, for a reason
        that is apt's rather than a shortcut: the repository unit converges BEFORE the package
        removals do, so a read taken at deletion time would still see every package the run is
        about to remove. `flatpak_sync` counts against the real machine because its remote
        deletion runs after its converge loop; the decisions are the same fact, known earlier.
        A removal that is approved and then FAILS is outside the article, which counts
        approved removals.

        The log line is the plan-time withholding's own, because it is the same fact: a file
        the review never mentions reaches the user nowhere else.
        """
        kept: dict[str, list[str]] = {}
        for diff in diffs:
            if diff.item_class is not ItemClass.APT_SOURCE or diff.action is not DiffAction.REMOVE:
                continue
            if diff.item_id == METADATA_REFRESH_ITEM_ID:
                continue
            filename = diff.item_id.removeprefix(APT_SOURCE_ID_PREFIX)
            keeping = [
                name
                for name in self._repo_users.get(filename, [])
                if decisions.get(f"{APT_PACKAGE_ID_PREFIX}{name}") != Decision.APPLY
            ]
            if keeping:
                kept[filename] = keeping
                self._log(
                    Host.TARGET,
                    LogLevel.FULL,
                    f"keeping repository {filename}: {self.machines.target} still installs "
                    f"{', '.join(keeping)} from it, so its deletion is not offered",
                )
        return kept

    # -- review -> work -----------------------------------------------------------------

    @override
    def accept_review(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Insert the synthetic metadata-refresh diff once the decisions are known, so it
        flows through the same per-item logging, dry-run gate and failure collection as
        everything else (`apply()`'s existing loop) instead of being a special case bolted
        onto the end.

        Runs AFTER `plan()` (so decisions exist) and is exactly where `PKG-FR-BATCHED`'s review
        already stopped being relevant for THIS item — the refresh is infrastructure
        the user never ticks, not a repository they decided about. Positioned immediately
        after the last non-package diff (repository group already sorted
        pin/config-before-source by `plan()`) and before every package diff, matching
        apt's own dependency order: metadata must be current before anything installs
        from it.

        The marker is ALSO what carries the work no diff represents: the derived writes
        (`PKG-FR-REPO-DERIVED`/`PKG-FR-DISTRO-FILES` — a repository, a pin or a distribution file travels without a
        review line, so nothing else would ever route into the repository unit), and a
        rotated keyring, which changes no source file at all. `Keyrings.pending_work` is a
        superset test — the unit recomputes the exact set from the real decisions and returns
        early if it is empty — so the cost of a false positive is one no-op call.

        Manual-collateral decisions (`PKG-FR-COLLATERAL-MANUAL`) are resolved first: applying a
        collateral item marks its package approved so the apply-time guard lets the
        removal through, while a skip is translated into `SKIP_ONCE` on the approved
        packages that cause that collateral, so a declined collateral cleanly leaves them
        unapproved rather than failing them at the guard.

        That resolution can withdraw a removal the second round already counted a repository
        against (`plan_second_round`), which is the one direction the ordering can go wrong:
        the repository is offered, its last user's removal is then cancelled by a collateral
        answer, and the file would go with an installed package still pointing at it. The
        recount below is the same one, run over the FINAL decisions, and it takes the
        approval back rather than reopening a question the review has closed.
        """
        work = self._work
        outcome = work.collateral.resolve(plan.diffs, outcome)
        outcome = self._withhold_repositories_still_in_use(plan, outcome)
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

    def _withhold_repositories_still_in_use(self, plan: PackagePlan, outcome: ReviewOutcome) -> ReviewOutcome:
        """The backstop behind `plan_second_round`'s count: take back the approval of any
        repository deletion the target still installs something from (`PKG-FR-REPO-DELETE`).

        The deletion is offered in the second round, over decisions that already carry the
        first round's collateral resolution, so the ordinary paths never reach this. What can
        still reach it is a collateral question put in that SAME round: keeping the package
        withdraws the removals that cause it (`Collateral.resolve`), and one of those may be
        the removal a repository was counted against moments earlier on the same screen.
        A deletion this job never offered cannot be approved by the review, but a
        hand-assembled `ReviewOutcome` can carry one, and the machine is protected either way.

        Withdrawn rather than failed: nothing went wrong, and the file is offered again next
        run. The log line names the file and what keeps it, because an approval taken back
        reaches the user nowhere else.
        """
        approved_deletions = [
            diff.item_id.removeprefix(APT_SOURCE_ID_PREFIX)
            for diff in plan.diffs
            if diff.item_class is ItemClass.APT_SOURCE
            and diff.item_id != METADATA_REFRESH_ITEM_ID
            and diff.action is DiffAction.REMOVE
            and outcome.decisions.get(diff.item_id) == Decision.APPLY
        ]
        withheld: dict[str, list[str]] = {}
        for filename in approved_deletions:
            keeping = [
                name
                for name in self._repo_users.get(filename, [])
                if outcome.decisions.get(f"{APT_PACKAGE_ID_PREFIX}{name}") != Decision.APPLY
            ]
            if keeping:
                withheld[filename] = keeping
        if not withheld:
            return outcome

        for filename, keeping in withheld.items():
            self._log(
                Host.TARGET,
                LogLevel.FULL,
                f"keeping repository {filename}: {self.machines.target} still installs "
                f"{', '.join(keeping)} from it, so its approved deletion is not applied",
            )
        return ReviewOutcome(
            decisions={
                **outcome.decisions,
                **{f"{APT_SOURCE_ID_PREFIX}{filename}": Decision.SKIP_ONCE for filename in withheld},
            },
            was_interactive=outcome.was_interactive,
            snippets=outcome.snippets,
            unresolved=outcome.unresolved,
        )

    @override
    async def _record_permanent_skips(self, plan: PackagePlan, decisions: Mapping[str, Decision]) -> None:
        """The base recording pass, minus every `apt:source:`/`apt:pin:` id (`PKG-FR-SKIP-ONCE`).

        The interactive flow already cannot produce a `SKIP_ALWAYS` for one — their groups
        are absent from `_PROMOTABLE_ACTIONS`, so the promotion screen never offers them —
        but "no registry entry" is a property of the model, not of one prompt's wiring, and
        a decision can also arrive from the review's automation hook or from a caller
        assembling a `ReviewOutcome` by hand. Filtered by id prefix rather than by action so
        it holds in EVERY direction, including ones this job no longer emits.

        `apt:config:` is deliberately not filtered: it keeps the full three-way decision and
        the machine-local registry, because no approved package implies whether a proxy or a
        recommends policy should travel (`PKG-FR-APTCONF`).
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
        around its own writes. Unreproducible items are not apt's concern (`PKG-FR-MANUAL-SCOPE`) —
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
                    "(required to read /etc/apt repository, keyring and pin config, and to pause this "
                    "machine's own apt update timers for the sync window).\n"
                    + passwordless_sudo_hint(SOURCE_SUDO_COMMANDS),
                )
            )

        sudo_check = await self.target.run_command("sudo --non-interactive true", login_shell=False)
        if not sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "passwordless sudo is not available on target "
                    "(required to install packages, write /etc/apt config, and pause that machine's own "
                    "apt update timers for the sync window).\n"
                    + passwordless_sudo_hint(TARGET_SUDO_COMMANDS, user=self.context.target_username),
                )
            )

        # fuser exits 0 when the file IS held by at least one process, non-zero when
        # free (man fuser EXIT CODES) — read-only probe, no lock is acquired or released.
        #
        # The message names both holders it could be and picks neither, because the probe
        # cannot tell: `fuser` reports that the lock is held, not who by. The sync-window
        # suspension of the apt timers does not narrow it either — it stops the updater
        # STARTING, and cannot stop one already running, let alone a person's own apt.
        lock_check = await self.target.run_command("sudo fuser /var/lib/dpkg/lock-frontend", login_shell=False)
        if lock_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET,
                    "dpkg frontend lock is held on target: another package operation is in progress there. "
                    "It may be a package command of your own (apt, dpkg, or a graphical package manager) or "
                    "the system's automatic updates — the lock does not say which. "
                    "Wait for it to finish, then run the sync again.",
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): the manual-install set."""
        return FirstSyncScope(
            job_name=cls.name,
            job_display_name=cls.display_name,
            scope_items=["apt packages (manually-installed set)"],
            mechanism="apt-get install/remove per item, after review",
        )
