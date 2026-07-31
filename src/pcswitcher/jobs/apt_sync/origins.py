"""Where a package actually comes from, and whether the target can be given the same place
(ADR-020 D-34), plus the read-back that makes it a guarantee rather than a prediction (D-35).

Two different jobs, deliberately in one module because they are two halves of one claim.
The classification decides which repository work to derive and what the review says; the
verification decides what may actually be installed, and only it sees the state the
derivation produced. Splitting them would let one drift from the other, which is exactly
the failure D-35 exists to catch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.apt_sync.commands import policy_command, require_apt_answer
from pcswitcher.jobs.apt_sync.items import AptPackageItem, package_name
from pcswitcher.jobs.apt_sync.keyrings import dangling_ref
from pcswitcher.jobs.apt_sync.messages import build_dangling_keyring_detail, build_origin_refusal_detail
from pcswitcher.jobs.apt_sync.probe import KeyDigests, SourceFileRefs, TargetPolicy
from pcswitcher.jobs.packages.apt_policy import candidate_origins_by_package
from pcswitcher.jobs.packages.items import DiffAction, ItemClass, ItemDiff, Machines
from pcswitcher.jobs.packages.review import Decision
from pcswitcher.models import Host

# The distribution as one member of an identity set. Not a URI and unable to collide with
# one: every real origin here has been through `normalise_repo_uri`, which never strips a
# scheme, so no origin can reduce to this string. `PKG-FR-DISTRO-ORIGIN` makes a machine's
# whole distribution ONE origin, which is exactly one member.
_DISTRIBUTION_ORIGIN = "<distribution>"


def _origin_identity(vendors: tuple[str, ...], from_distribution: bool) -> frozenset[str]:
    """One machine's origins for a package, as identity counts them."""
    return frozenset(vendors) | ({_DISTRIBUTION_ORIGIN} if from_distribution else frozenset())


class OriginOutcome(StrEnum):
    """What the origin facts say can be done about one package missing on the target.

    Three outcomes, not ADR-020 D-34's four: its classes 2 and 3 (the target has a candidate
    from the wrong vendor / the target has no candidate at all) differ only in what they
    look like, never in what happens — both install the package and both derive the source's
    repository first — so collapsing them keeps the diff from branching on a distinction
    that has no consequence.
    """

    SAME_ORIGIN = "same_origin"
    """Class 1. Ordinary install, zero repository work — the target already offers the
    package from a place the source uses, or there is no origin fact to hold it to."""

    REPLICABLE = "replicable"
    """Classes 2 and 3. Install, with the source's repository files derived from it."""

    UNREPLICABLE = "unreplicable"
    """Class 4. `REPO_UNAVAILABLE`/`REPORT_ONLY` — the origin is declared by no writable
    file on the source, so the package can only be reported."""


@dataclass(frozen=True)
class OriginPlan:
    """Every origin fact one source package's classification turns on (ADR-020 D-34).

    Assembled per package from facts about BOTH machines, because the question "can the
    target end up with this package from the same place the source has it?" is not
    answerable from either machine alone.
    """

    source_origins: frozenset[str] = frozenset()
    """Origin URIs of the package's INSTALLED version on the source. Empty means apt named
    none for it, or printed no block at all — never evidence of anything (`df48cd07`)."""

    source_files: frozenset[str] = frozenset()
    """The source's repository files declaring any of `source_origins`. Computed only for a
    package missing on the target, which is the only case that could make one travel."""

    target_candidate_origins: frozenset[str] = frozenset()
    """Origin URIs of the version the TARGET would install. Empty means no repository on
    the target offers the name."""

    target_candidate_known: bool = False
    """Whether apt printed a block for the name on the target AT ALL. Silence is not the
    same answer as `Candidate: (none)` and must never be read as one (`df48cd07`): the
    first is a question apt did not answer, including the case where the whole command
    failed; only the second is apt saying it will install nothing."""

    vendor_source_origins: tuple[str, ...] = ()
    """`source_origins` minus the SOURCE's distribution origins, sorted. What the review
    names (ruling 9), and the left-hand side of the origin comparison (§2.6)."""

    vendor_target_origins: tuple[str, ...] = ()
    """The package's INSTALLED origins on the target minus the TARGET's distribution
    origins, sorted. Filtered against the target's own distribution files so two machines
    on different Ubuntu mirrors do not read as two vendors."""

    source_from_distribution: bool = False
    """Whether any of `source_origins` is one of the SOURCE's own distribution origins."""

    target_from_distribution: bool = False
    """Whether any of the package's installed origins on the target is one of the TARGET's
    own distribution origins."""

    unwritable: str | None = None
    """Why no file serving `source_origins` can be written on the target, or `None` when at
    least one can. A file whose `Signed-By:` resolves to no key on the source is a
    repository apt would refuse on the target, so it cannot deliver the origin."""

    def outcome(self) -> OriginOutcome:
        """Which of ADR-020 D-34's outcomes this package falls into.

        The ladder is ordered by what it takes to be sure: a target candidate from an origin
        the source uses settles the question outright, and only after that does it matter
        whether the origin could be replicated at all.
        """
        if not self.source_origins:
            # apt named no repository origin for the source's installed version, or printed
            # no block for it at all. Absence is never evidence (`df48cd07`): with no origin
            # to replicate there is nothing to compare, so the question degrades to the
            # presence one this job asked before origins existed — and on that question the
            # target's silence still condemns nothing, only an explicit `Candidate: (none)`
            # does. A run whose policy call failed proposes the install and lets the install
            # report its own failure, rather than reporting a repository problem it never
            # established.
            refused = self.target_candidate_known and not self.target_candidate_origins
            return OriginOutcome.UNREPLICABLE if refused else OriginOutcome.SAME_ORIGIN
        if self.source_origins & self.target_candidate_origins:
            return OriginOutcome.SAME_ORIGIN
        if self.source_files and self.unwritable is None:
            return OriginOutcome.REPLICABLE
        return OriginOutcome.UNREPLICABLE

    @property
    def derived_files(self) -> frozenset[str]:
        """The source repository files approving this package would make travel (ruling 4).

        Empty for `SAME_ORIGIN`: the target already offers the package from a place the
        source uses, so nothing about `/etc/apt` has to change for the install to be
        faithful. Empty for `UNREPLICABLE` too — that package is reported, not installed,
        and deriving a repository for a report-only item would break ruling 4's "derived
        from the packages approved from it".
        """
        return self.source_files if self.outcome() is OriginOutcome.REPLICABLE else frozenset()

    @property
    def source_origin_identity(self) -> frozenset[str]:
        """Where the SOURCE's copy comes from, as `PKG-FR-APT-IDENTITY` counts origins:
        each vendor origin on its own, plus the distribution as ONE origin however many
        mirrors and pockets declared it (`PKG-FR-DISTRO-ORIGIN`).

        Empty means apt named no origin for that machine's copy at all, which is absence of
        evidence and never a finding (`df48cd07`).
        """
        return _origin_identity(self.vendor_source_origins, self.source_from_distribution)

    @property
    def target_origin_identity(self) -> frozenset[str]:
        """`source_origin_identity` for the TARGET's installed copy, computed against the
        TARGET's own distribution files so two machines on different mirrors agree."""
        return _origin_identity(self.vendor_target_origins, self.target_from_distribution)

    def unavailable_cause(self, machines: Machines) -> str:
        """Why the source's origin cannot be provided on the target — the second half of a
        `REPO_UNAVAILABLE` detail, after the origin itself.
        """
        if self.unwritable is not None:
            return self.unwritable
        if not self.source_origins:
            return f"apt on {machines.source} names no repository origin for it"
        return f"no repository file on {machines.source} declares it"


def is_origin_mismatch(plan: OriginPlan) -> bool:
    """Whether a package present on BOTH machines came from two different origins (§2.6).

    Compared as `PKG-FR-APT-IDENTITY` defines identity, over the two identity sets rather
    than over the vendor lists: each vendor counts on its own, and each machine's whole
    distribution counts as ONE origin (`PKG-FR-DISTRO-ORIGIN`). So `gh` from
    `cli.github.com` against `gh` from the Ubuntu archive is a divergence — the requirement's
    own example of one name and two pieces of software — while the same package from two
    different Ubuntu mirrors is not, because both mirrors reduce to that one origin.

    Both sides must be non-empty: a machine whose copy apt named no origin for at all has
    told us nothing, and absence of evidence is never a finding.
    """
    source = plan.source_origin_identity
    target = plan.target_origin_identity
    return bool(source) and bool(target) and not (source & target)


class OriginClassifier:
    """D-34's classification over two machines' captured facts, and D-35's read-back."""

    def __init__(
        self,
        *,
        machines: Machines,
        source_refs: SourceFileRefs,
        target_refs: SourceFileRefs,
        source_keys: KeyDigests,
    ) -> None:
        self._machines = machines
        self._source_refs = source_refs
        self._target_refs = target_refs
        self._source_keys = source_keys
        # One `OriginPlan` per source package item id, rebuilt whenever the package diff
        # is. It is what the diff classifies from and what the derived `/etc/apt` write set
        # is read out of, so it must describe the same run the accepted plan describes.
        self._plans: Mapping[str, OriginPlan] = {}
        # `{package name: refusal message}` for every approved install whose candidate on
        # the REAL post-`apt-get update` target comes from none of the source's origins
        # (D-35). `None` until the one batched verification runs; `{}` once it has run and
        # found nothing to refuse, which is what distinguishes "not yet checked" from
        # "checked, all clear" and keeps the call to exactly one per run.
        self._refusals: dict[str, str] | None = None

    @property
    def plans(self) -> Mapping[str, OriginPlan]:
        return self._plans

    def plan_for(self, item_id: str) -> OriginPlan:
        return self._plans.get(item_id, OriginPlan())

    def classify(
        self,
        source_items: Sequence[AptPackageItem],
        target_items: Sequence[AptPackageItem],
        policy: TargetPolicy,
        source_origins: Mapping[str, frozenset[str]],
    ) -> Mapping[str, OriginPlan]:
        """One `OriginPlan` per source package, from facts already captured this run.

        Distribution origins are resolved per machine (D-35) from that machine's own
        distribution source files, so a source on one Ubuntu mirror and a target on another
        agree that both are the distribution rather than two vendors.
        """
        source_distribution = self._source_refs.distribution_origins()
        target_distribution = self._target_refs.distribution_origins()
        on_target = {item.item_id for item in target_items}

        plans: dict[str, OriginPlan] = {}
        for item in source_items:
            origins = source_origins.get(item.name, frozenset())
            # Only a package the target lacks can make a repository file travel, so the
            # file lookup is skipped for one present on both.
            files = self._source_refs.files_serving(origins) if item.item_id not in on_target else frozenset()
            target_installed = policy.installed_origins.get(item.name, frozenset())
            plans[item.item_id] = OriginPlan(
                source_origins=origins,
                source_files=files,
                target_candidate_origins=policy.candidate_origins.get(item.name, frozenset()),
                target_candidate_known=item.name in policy.candidate_origins,
                vendor_source_origins=tuple(sorted(origins - source_distribution)),
                vendor_target_origins=tuple(sorted(target_installed - target_distribution)),
                source_from_distribution=bool(origins & source_distribution),
                target_from_distribution=bool(target_installed & target_distribution),
                unwritable=self.unwritable_reason(files),
            )
        self._plans = plans
        return plans

    def unwritable_reason(self, source_files: frozenset[str]) -> str | None:
        """Why none of `source_files` can be written on the target, or `None` when at least
        one can.

        ONE writable file is enough: the origin only has to be declared once for the target
        to install from it, so a package served by both a sound repository file and a broken
        one is still replicable. The reported reason is the first broken file's, sorted, so
        the review text does not depend on dict order.
        """
        reasons: list[str] = []
        for filename in sorted(source_files):
            dangling = dangling_ref(self._source_refs.refs_of(filename), self._source_keys.filenames)
            if dangling is None:
                return None
            reasons.append(build_dangling_keyring_detail(filename, dangling, self._machines))
        return reasons[0] if reasons else None

    def target_resolvable(self, item_id: str) -> bool:
        """Whether the target's apt named a version it would install for this package, from
        the batched `apt-cache policy` this run already ran (`AptProbe.collect_target_policy`).

        The plan-time simulation's admission test, and a D-30 ruling rather than a detail. An
        ADR-020 D-34 class-3 install — the repository that supplies it is derived from the
        package's own approval and written during converge — is a name the target's apt
        cannot locate yet, and `apt-get --dry-run` refuses the WHOLE batch on it with the
        exit code a held dpkg lock also produces. Including such a name therefore does not
        weaken that one package's protection, it removes the protection from every other
        package in the run and aborts `plan()` before the user sees anything. The evidence
        used is the policy read, never the simulation's exit code, which cannot separate the
        two causes (ADR-022 D-01).

        What is given up, deliberately: an excluded package gets NO plan-time collateral
        classification, because apt cannot say what it would remove for a package it cannot
        resolve — the facts the three-way go-ahead/keep/stop question needs do not
        exist yet. What still covers it is the per-item simulation the install converger runs
        after the `/etc/apt` unit has landed and `apt-get update` has run, so apt CAN
        resolve it there: unapproved manual collateral fails that one item (D-27) instead of
        being installed over. The residual cost is that the user is told afterwards rather
        than asked beforehand, and only for packages whose repository this run adds.

        A package whose origin can never be replicated needs no rule here: it is
        `REPO_UNAVAILABLE`/`REPORT_ONLY`, so it is never an install candidate at all.
        """
        return bool(self.plan_for(item_id).target_candidate_origins)

    async def refusal(
        self,
        name: str,
        *,
        diffs: Sequence[ItemDiff],
        decisions: Mapping[str, Decision],
        target: RemoteExecutor,
    ) -> str | None:
        """Why this approved install may not run, or `None` when its origin checks out
        (ADR-020 D-35) — the hard guarantee behind origin replication.

        Plan-time classification decides what `/etc/apt` work to derive; only this decides
        what may be installed, because only it sees the state that derivation actually
        produced: a repository whose write failed, a pin that never landed, a vendor version
        the archive's epoch still outranks. It is therefore the check that makes "the target
        silently installs a different vendor's package" unreachable even when everything
        upstream of it is wrong.

        ONE batched `apt-cache policy` for the whole approved set, computed on first use and
        cached — the answer cannot change between two installs of one run, and a per-package
        call would cost a full policy query per install. Reached from the install converger
        rather than from `apply()` so it is by construction after this run's single
        `apt-get update` (whichever of the two refresh paths issued it) and before the first
        install, which is the window in which the answer is about the converged target.

        Packages whose source origins are all distribution origins never enter the set
        (D-35's exemption): two machines on different Ubuntu mirrors are not two vendors.

        A name an ANSWERED probe printed no block for is refused like any other mismatch.
        That is deliberately stricter than the plan-time rule, where apt's silence condemns
        nothing (`df48cd07`): there, silence leaves the install to report its own failure;
        here the install IS the thing being guarded, and a guarantee that could not be
        evaluated has not been met. A probe that did not answer at all is a different thing
        and does not reach this refusal — `require_apt_answer` fails the run once instead,
        because "the environment broke" is not a finding about any package's origin.
        """
        if self._refusals is None:
            self._refusals = await self._verify(diffs, decisions, target)
        return self._refusals.get(name)

    async def _verify(
        self, diffs: Sequence[ItemDiff], decisions: Mapping[str, Decision], target: RemoteExecutor
    ) -> dict[str, str]:
        held_to: dict[str, frozenset[str]] = {}
        for diff in diffs:
            if diff.item_class is not ItemClass.APT_PACKAGE or diff.action is not DiffAction.INSTALL:
                continue
            if decisions.get(diff.item_id) != Decision.APPLY:
                continue
            plan = self._plans.get(diff.item_id)
            # `vendor_source_origins` is `source_origins` minus the SOURCE's own distribution
            # files, so an empty tuple is exactly D-35's exemption. The intersection below is
            # against the FULL set: a package the source has from both a vendor and the
            # archive is faithfully replicated by either.
            if plan is None or not plan.vendor_source_origins:
                continue
            held_to[package_name(diff.item_id)] = plan.source_origins

        if not held_to:
            return {}

        command = policy_command(sorted(held_to))
        result = await target.run_command(command, login_shell=False)
        candidates = candidate_origins_by_package(result.stdout)
        # Every name here is an approved install with a vendor origin, so plan-time
        # classification already found either a target candidate (class 2) or a source
        # repository this run has since written (class 3) — apt owes a block for each.
        require_apt_answer(command, result, Host.TARGET, blocks=len(candidates))
        return {
            name: build_origin_refusal_detail(
                name, sorted(origins), sorted(candidates.get(name, frozenset())), self._machines
            )
            for name, origins in sorted(held_to.items())
            if not (candidates.get(name, frozenset()) & origins)
        }
