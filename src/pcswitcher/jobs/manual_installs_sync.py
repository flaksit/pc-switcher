"""`manual_installs_sync`: the fourth package job, owning everything no package manager
can reproduce (D-15, D-18, D-19, D-20, D-21).

Two detectors, both run on the SOURCE:

- apt packages in the source's `apt-mark showmanual` set whose SOURCE `apt-cache policy`
  reports no candidate at all — installed via `dpkg -i` of a bare `.deb`, never
  registered with any configured repository.
- paths directly under `/usr/local` and `/opt` (plus the immediate children of
  `/usr/local/bin` and `/usr/local/lib`) that no dpkg package owns — software an install
  script dropped there, bypassing apt entirely.

Both were previously folded into `apt_sync`. D-18 pulls them into their own job with its
own enable flag, because half of what they cover is not apt's business at all (unowned
files under `/usr/local`/`/opt`), and folding it into `apt_sync` meant disabling apt
silently disabled manual-install detection with nothing telling the user. This job does
its OWN `dpkg`/`apt-cache` queries rather than sharing `apt_sync`'s, so ownership stays
clean — it never imports `apt_sync` (D-18).

An unreproducible item ends a run resolved in one of three ways (D-21): it has an install
snippet in the shared, synced registry (`SnippetRegistry`, D-20/D-23), it is recorded
machine-specific (skip-always) in this job's machine-local decision file, or the user
skipped it once — skip-once is a real decision, not an unresolved state. Only a genuinely
undecided item leaves an interactive run unclean.

`ManualInstallsSyncJob` subclasses `PackageSyncJob` and overrides `plan()`, `converge()`,
`validate()` and `describe_first_sync_scope()`, following `SnapSyncJob`'s shape (a
non-apt item type driving an overridden `plan()` rather than the inherited apt-package
diff). The unreproducible-specific finalize and unresolved-as-failure logic lives here as
overrides of the base's now-no-op hooks, so the base `apply()` stays generic for the
three managers that produce no unreproducible items.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, override

from pcswitcher.config_sync import CONFIG_REMOTE_DIR
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.package_items import (
    DiffAction,
    DiffClass,
    ItemClass,
    ItemDiff,
    UnreproducibleItem,
)
from pcswitcher.jobs.package_review import (
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.package_state import (
    SNIPPET_REGISTRY_RELPATH,
    DecisionEntry,
    DecisionFile,
    Snippet,
    SnippetRegistry,
    filter_inert,
)
from pcswitcher.jobs.package_sync_core import PackagePlan, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, LogLevel, ValidationError

__all__ = ["ManualInstallsSyncJob"]

# D-19's bounded unowned-install scan: top-level entries of `/usr/local` and `/opt`, plus
# the immediate children of `/usr/local/bin` and `/usr/local/lib` — one batched
# `find <dir...> -mindepth 1 -maxdepth 1` covers all four, since `find` applies the same
# depth options to every starting path it is given. Enough to NAME a finding (D-18), never
# enough to walk an entire tree — the item is decided on, not replicated (deferred ideas,
# CONTEXT.md). Owned by this job now, no longer shared with apt_sync (D-18).
_UNOWNED_SCAN_ROOTS = ("/usr/local", "/opt", "/usr/local/bin", "/usr/local/lib")

# Matches one `dpkg -S` "owned" line: `<package>[,<package>...]: <path>`. A path dpkg does
# not own produces no such line at all (its "no path found" diagnostic goes to stderr,
# which this scan never inspects) — absence from stdout is the only signal
# `_owned_paths_from_dpkg_s` needs. A private copy of apt_sync's identical regex: D-18
# keeps ownership clean by NOT importing apt_sync, and this parser is small enough that
# one duplicated line is cheaper than a shared-core coupling.
_DPKG_S_OWNED_RE = re.compile(r"^[^:]+:\s+(?P<path>/\S.*)$")


def _lines(output: str) -> list[str]:
    """Non-blank, stripped lines — the shape every `apt-mark`/`find` list command this
    module runs produces. A private copy of apt_sync's identical helper (D-18)."""
    return [line.strip() for line in output.splitlines() if line.strip()]


def _packages_with_no_candidate(policy_output: str) -> set[str]:
    """Parse a multi-package `apt-cache policy <name...>` run: names whose `Candidate:`
    line reads `(none)`. Each package's block starts with an unindented `<name>:` header
    line, per `apt-cache policy`'s documented output shape. A private copy of apt_sync's
    identical parser (D-18): apt_sync keeps its own for `collect_unavailable_item_ids`
    (D-25 REPO_UNAVAILABLE, a distinct concern about the TARGET's repos), so both jobs own
    the parser they need without either importing the other.
    """
    no_candidate: set[str] = set()
    current: str | None = None
    for line in policy_output.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            current = line[:-1]
            continue
        if current is None:
            continue
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            if stripped.removeprefix("Candidate:").strip() == "(none)":
                no_candidate.add(current)
            current = None
    return no_candidate


def _owned_paths_from_dpkg_s(output: str) -> frozenset[str]:
    """Every path `dpkg -S` reports as owned, parsed from its stdout alone (D-19's
    unowned-install scan). A queried path dpkg does NOT own is simply absent from this set
    — its "no path found matching pattern" diagnostic is a stderr message this scan never
    reads, so a batched multi-path `dpkg -S` degrades cleanly even when some paths are
    unowned and others are not. A private copy of apt_sync's identical parser (D-18).
    """
    owned: set[str] = set()
    for line in output.splitlines():
        match = _DPKG_S_OWNED_RE.match(line)
        if match:
            owned.add(match.group("path"))
    return frozenset(owned)


class ManualInstallsSyncJob(PackageSyncJob):
    """Detect, review and reproduce items no package manager can install on its own
    (D-15/D-18), on this job's own enable flag independent of `apt_sync`'s.

    Overrides `plan()` with an unreproducible-specific detect -> filter -> diff pipeline
    (the inherited apt-package-shaped `diff_items` does not apply); `converge()` replays a
    registered install snippet verbatim (D-20); the unreproducible finalize and
    unresolved-as-failure hooks the base leaves as no-ops are implemented here.
    """

    name: ClassVar[str] = "manual_installs_sync"
    manager_id: ClassVar[str] = "manual"

    # No configurable properties: mirrors AptSyncJob's empty schema — only the enable flag
    # in sync_jobs is needed. D-32 forbids an empty placeholder config SECTION, so there is
    # no `manual_installs_sync:` block in default-config.yaml, but the in-code CONFIG_SCHEMA
    # ClassVar still declares the empty object every job carries.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, context: JobContext) -> None:
        super().__init__(context)
        # Guards `_finalize_unreproducible` to run at most once per run. `after_review()`
        # calls it (so the pushed registry includes on-the-fly snippets), then the base
        # `apply()` calls it again; the second call is a no-op so a snippet's `authored_at`
        # is stamped exactly once and the source and pushed target registries stay identical.
        self._unreproducible_finalized = False

    # -- after-review snippet push (D-23) -----------------------------------------------

    @override
    async def after_review(self) -> None:
        """Push the install-snippet registry to the target after this job's review and
        before `apply()` replays any snippet (D-23), so a snippet authored on the fly in
        the just-finished review reaches the target THIS run rather than the next one.

        Finalize-then-push: `_finalize_unreproducible` persists this run's authored
        snippets into the SOURCE registry first (idempotent — `apply()` calls it again as
        a no-op), then `_push_snippet_registry` copies that file to the target. The push
        depends on no other job: it moves the file itself and reads neither `config_sync`
        nor `folder_sync` state, so disabling either cannot break snippet delivery.
        """
        assert self._accepted_plan is not None
        assert self._accepted_outcome is not None
        await self._finalize_unreproducible(self._accepted_plan, self._accepted_outcome)
        await self._push_snippet_registry()

    async def _push_snippet_registry(self) -> None:
        """Copy the source's `~/.config/pc-switcher/package-snippets.yaml` to the target's
        own copy under the SSH user's home, mirroring `config_sync._copy_config_to_target`'s
        `mkdir -p` -> `echo $HOME` -> `send_file` shape.

        `send_file` writes plain SFTP as the SSH user, always under that user's home
        (`~/.config/pc-switcher`) — never `/etc` — which is exactly where the registry
        belongs, so no `sudo install` staging is needed. A no-op if the source has no
        registry file yet (a user who has never authored a snippet) and under dry-run
        (ADR-014: a rehearsal transfers nothing).
        """
        if self.context.dry_run:
            return

        source_path = Path.home() / SNIPPET_REGISTRY_RELPATH
        if not source_path.exists():
            return

        mkdir = await self.target.run_command(f"mkdir -p {CONFIG_REMOTE_DIR}")
        if not mkdir.success:
            raise RuntimeError(f"Failed to create config directory on target: {mkdir.stderr}")

        # send_file needs an absolute remote path, so expand the target's ~ once.
        home = await self.target.run_command("echo $HOME")
        if not home.success:
            raise RuntimeError("Failed to get home directory on target")
        absolute_remote_path = f"{home.stdout.strip()}/{SNIPPET_REGISTRY_RELPATH}"
        await self.target.send_file(source_path, absolute_remote_path)

    # -- Detection (D-18/D-19), all on the source ---------------------------------------

    async def _scan_no_candidate_apt_packages(self, manual_names: Sequence[str]) -> list[UnreproducibleItem]:
        """D-18: manually-installed packages whose SOURCE's own `apt-cache` has no
        candidate at all — installed via `dpkg -i` of a bare `.deb`, never registered with
        any configured repository. Runs the job's OWN `apt-cache policy` (D-18), never
        apt_sync's.
        """
        if not manual_names:
            return []

        quoted = " ".join(shlex.quote(name) for name in manual_names)
        result = await self.source.run_command(f"apt-cache policy {quoted}")
        no_candidate = _packages_with_no_candidate(result.stdout)
        return [
            UnreproducibleItem(origin="apt-no-candidate", identifier=name, label=f"{name} (no apt candidate)")
            for name in sorted(no_candidate)
        ]

    async def _scan_unowned_installs(self) -> list[UnreproducibleItem]:
        """D-18/D-19: paths under `/usr/local` and `/opt` that no dpkg package owns —
        software an install script dropped there directly, bypassing apt entirely.

        One batched `find` over `_UNOWNED_SCAN_ROOTS` names every candidate path, then one
        batched `dpkg -S` over those candidates decides ownership; a path absent from the
        `dpkg -S` output is unowned. Both steps run on the SOURCE (D-18) — a fact about
        what the source machine has installed, not the target.
        """
        quoted_roots = " ".join(shlex.quote(root) for root in _UNOWNED_SCAN_ROOTS)
        listing = await self.source.run_command(f"find {quoted_roots} -mindepth 1 -maxdepth 1 2>/dev/null")
        candidates = _lines(listing.stdout)
        if not candidates:
            return []

        quoted_paths = " ".join(shlex.quote(path) for path in candidates)
        ownership = await self.source.run_command(f"dpkg -S {quoted_paths}")
        owned = _owned_paths_from_dpkg_s(ownership.stdout)

        return [
            UnreproducibleItem(origin="unowned-path", identifier=path, label=path)
            for path in sorted(set(candidates) - owned)
        ]

    # -- plan() / converge() ------------------------------------------------------------

    @override
    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """The source's unreproducible items: apt-no-candidate packages plus unowned
        `/usr/local`/`/opt` installs (D-18). `apt-mark showmanual` runs once here and its
        result feeds the no-candidate scan.

        This job overrides `plan()` and never routes through `PackageSyncJob.diff_items`'s
        apt-package-shaped dispatch, so widening this hook's item type is safe (same
        reasoning as `SnapSyncJob.capture_source_items`).
        """
        manual = await self.source.run_command("apt-mark showmanual")
        manual_names = _lines(manual.stdout)
        return [
            *await self._scan_no_candidate_apt_packages(manual_names),
            *await self._scan_unowned_installs(),
        ]

    @override
    async def query_target_items(self) -> Sequence[UnreproducibleItem]:  # pyright: ignore[reportIncompatibleMethodOverride]
        """No target-side manifest exists for unreproducible items: they are always
        source-held (they describe what the SOURCE machine has installed), and convergence
        is driven by the shared snippet registry, not by a diff against target state. The
        empty result keeps the abstract hook satisfied without a meaningless target query.
        """
        return []

    @override
    async def plan(self) -> PackagePlan:
        """Detect -> filter inert -> diff against the target's snippet registry. Read-only.

        An item already recorded machine-specific on the SOURCE is dropped by
        `filter_inert` before it becomes a diff (D-08/D-19: a finding produces noise
        exactly once, then never again). Unreproducible items are always source-held, so
        only the source's decision file is consulted. An item with a target-side registry
        snippet plans `INSTALL` (a snippet makes it reproducible); one without plans
        `REPORT_ONLY` and surfaces in its own review group for resolution.
        """
        source_decisions = await DecisionFile(self.manager_id, self.source).load()
        items = await filter_inert(await self.capture_source_items(), source_decisions)

        registry = SnippetRegistry(self.target)
        diffs: list[ItemDiff] = []
        for item in items:
            snippet = await registry.get(item.item_id)
            action = DiffAction.INSTALL if snippet is not None else DiffAction.REPORT_ONLY
            diffs.append(
                ItemDiff(
                    item_class=ItemClass.UNREPRODUCIBLE,
                    diff_class=DiffClass.UNREPRODUCIBLE,
                    action=action,
                    item_id=item.item_id,
                    label=item.label,
                    detail=None,
                )
            )
        all_diffs = tuple(diffs)
        groups = self._build_review_groups(all_diffs)
        return PackagePlan(manager=self.manager_id, diffs=all_diffs, groups=groups)

    @override
    def _build_review_groups(self, diffs: Sequence[ItemDiff]) -> tuple[ReviewGroup, ...]:
        """Carve still-unresolved `UNREPRODUCIBLE` diffs (`action=REPORT_ONLY`, D-21) into
        their own `UNREPRODUCIBLE_REVIEW_ACTION` group, presented after any resolved
        (snippet-backed, `action=INSTALL`) install group so the user sees resolved items
        before being asked to resolve the rest. A snippet-backed diff is NOT pulled out —
        it flows through the base grouping like any other install-direction item.
        """
        needs_resolution = [
            diff
            for diff in diffs
            if diff.item_class == ItemClass.UNREPRODUCIBLE and diff.action == DiffAction.REPORT_ONLY
        ]
        if not needs_resolution:
            return super()._build_review_groups(diffs)

        carved_ids = {diff.item_id for diff in needs_resolution}
        rest = [diff for diff in diffs if diff.item_id not in carved_ids]
        groups = list(super()._build_review_groups(rest))
        groups.append(
            ReviewGroup(
                manager=self.manager_id,
                action=UNREPRODUCIBLE_REVIEW_ACTION,
                title=f"Resolve {self.manager_id} items with no reproducible install",
                entries=tuple(
                    ReviewEntry(item_id=diff.item_id, label=diff.label, action_label="resolve", detail=diff.detail)
                    for diff in needs_resolution
                ),
            )
        )
        return tuple(groups)

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Replay this item's registered snippet against the target, verbatim (D-20).
        `SnippetRegistry.replay` never raises for "no snippet registered" — it returns a
        failed `CommandResult` instead, so a plan/apply-time race (the registry changed
        underneath this run) is a per-item failure like any other (D-27), not a crash. The
        only action reaching `converge()` is `INSTALL`: `plan()` sets that only when a
        snippet exists, and a `REPORT_ONLY` diff never reaches this hook (`apply()`'s
        filter).
        """
        return await SnippetRegistry(self.target).replay(diff.item_id, self.target)

    # -- Unreproducible finalize / unresolved hooks (moved off the base, D-18) -----------

    @override
    async def _finalize_unreproducible(self, plan: PackagePlan, outcome: ReviewOutcome) -> None:
        """Persist this run's snippet authoring and unreproducible-item skip-always
        decisions (D-20/D-21/D-23). Overrides the base no-op hook (D-18: only this job
        produces unreproducible items).

        Snippets are written to `self.source` — never `self.target` — because the source
        registry is this job's own source of truth; `after_review()` then pushes that file
        to the target (D-23) so a snippet authored during THIS run's review reaches the
        target THIS run, before `apply()` replays it. Skip-always decisions are also
        recorded on `self.source`: unreproducible items are always source-held (they
        describe what is installed on the machine currently acting as source), so there is
        no target-held case to route to `self.target`.

        Idempotent per run: `after_review()` calls this before its push and the base
        `apply()` calls it again; a `self._unreproducible_finalized` guard makes the second
        call a no-op so each snippet's `authored_at` is stamped once and the source and
        pushed target registries stay byte-identical.

        Never during dry-run (ADR-014) and never for a non-interactive outcome (D-26):
        nothing is recorded permanently when nothing was actually decided by a human.
        """
        if self._unreproducible_finalized:
            return
        self._unreproducible_finalized = True

        if self.context.dry_run or not outcome.was_interactive:
            return

        by_id = {diff.item_id: diff for diff in plan.diffs}

        if outcome.snippets:
            registry = SnippetRegistry(self.source)
            authored_at = datetime.now(UTC).isoformat()
            for item_id, body in outcome.snippets.items():
                diff = by_id.get(item_id)
                label = diff.label if diff is not None else item_id
                await registry.add(
                    Snippet(
                        item_id=item_id,
                        label=label,
                        body=body,
                        authored_at=authored_at,
                        authored_on=self.context.source_hostname,
                    )
                )

        recorded_at = datetime.now(UTC).isoformat()
        for diff in plan.diffs:
            if diff.item_class != ItemClass.UNREPRODUCIBLE:
                continue
            if outcome.decisions.get(diff.item_id) != Decision.SKIP_ALWAYS:
                continue
            await DecisionFile(self.manager_id, self.source).record(
                DecisionEntry(
                    item_id=diff.item_id,
                    item_class=diff.item_class,
                    label=diff.label,
                    reason=None,
                    recorded_at=recorded_at,
                )
            )

    @override
    def _unresolved_as_failures(self, plan: PackagePlan, outcome: ReviewOutcome) -> list[tuple[ItemDiff, str]]:
        """D-21/D-27: after an INTERACTIVE, non-dry-run review, an unreproducible item
        left with neither a snippet nor a recorded decision makes this job's result a
        failure — the run is visibly not clean, and stays that way every run until the
        item is resolved. Overrides the base no-op hook (D-18).

        Two exemptions, both governed by a decision more specific than D-21 for their
        case: a non-interactive run (D-26 — skip all, record nothing, report everything,
        never fail on that basis alone, since the user was never offered a resolution) and
        a dry-run (ADR-014 — a preview that fails would make `--dry-run` unusable as a
        check). A deliberate skip-once is NOT in `outcome.unresolved` (D-21, decided in
        `package_review._review_unreproducible_group`), so it never reaches this method.
        Converge failures (`failures` in `apply()`) are NOT covered by either exemption;
        they fail the job unconditionally.
        """
        if not outcome.unresolved or not outcome.was_interactive or self.context.dry_run:
            return []

        by_id = {diff.item_id: diff for diff in plan.diffs}
        failures: list[tuple[ItemDiff, str]] = []
        for item_id in outcome.unresolved:
            diff = by_id[item_id]
            message = (
                f"{diff.label} has no install snippet and no recorded machine-specific decision (D-21); "
                "author a snippet or choose 'skip always' on a future sync to resolve it"
            )
            self._log(Host.SOURCE, LogLevel.ERROR, message)
            failures.append((diff, message))
        return failures

    @override
    async def validate(self) -> list[ValidationError]:
        """`apt-cache` and `dpkg` availability on the SOURCE — the commands this job's own
        detection runs (D-18). The source is only ever read, so no sudo is needed for
        detection. A snippet's own sudo needs are unpredictable (an opaque blob, D-20), so
        this job does NOT pre-validate target sudo; a snippet that needs it and lacks it
        fails as a per-item converge failure (D-27), reported like any other.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `AptSyncJob.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        apt_cache_check = await self.source.run_command("apt-cache --version")
        if not apt_cache_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE, "apt-cache is not available on source (required to detect unreproducible packages)"
                )
            )

        dpkg_check = await self.source.run_command("dpkg --version")
        if not dpkg_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE, "dpkg is not available on source (required to detect unowned installs)"
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): replaying install
        snippets for unreproducible items."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["unreproducible/manual installs (via recorded install snippets)"],
            mechanism="replay install snippet per item, after review",
        )
