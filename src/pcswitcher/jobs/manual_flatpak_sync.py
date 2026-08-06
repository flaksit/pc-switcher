"""`manual_flatpak_sync`: flatpak refs no remote can reproduce — an application installed
from a local bundle (`flatpak install --bundle`), or from a remote that no longer exists
(#252, D-15, D-18).

Detection is one question asked of the whole installed set on each machine: which refs'
`origin` names no remote configured in that ref's own installation scope. The predicate and
the evidence behind it live in `packages/flatpak_policy.py`, which `flatpak_sync` reads
too — the two jobs partition the same population, so one of them widening its idea of
"reproducible" without the other narrowing its own would leave a ref replicated by nobody
and reported nowhere. On the target the question is only whether the ref is installed
there at all, whatever origin put it there: software that is there is there.

Its own job, on its own enable flag, for the reason D-15 gives every package job one: an
independent failure surface, an independent review and an independent switch. It sits
beside `manual_deb_sync` and `manual_installs_sync`; all three subclass
`UnreproducibleSyncJob` and share one install-snippet registry, and none imports another
or imports the package-manager job it was carved out of (D-18).

The flatpak handoff is capture-time exclusion, not a message: `flatpak_sync` drops the same
refs from both its manifests using the shared predicate, and this job independently
re-runs it. Two jobs, one predicate, no result passed between them (D-15/D-16). The
consequence the user must know: this job's enable flag is its own, so enabling
`flatpak_sync` while disabling this one leaves bundle-installed refs replicated by nobody.
"""

from __future__ import annotations

import shlex
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, override

from pcswitcher.executor import Executor
from pcswitcher.jobs.packages.flatpak_policy import (
    FLATPAK_REMOTE_NAMES_CMD,
    SCOPES,
    partition_unreproducible,
    remote_names,
    scope_flag,
)
from pcswitcher.jobs.packages.probes import require_answer
from pcswitcher.jobs.packages.state import DecisionEntry
from pcswitcher.jobs.packages.unreproducible import UnreproducibleItem, UnreproducibleSyncJob, lines_of
from pcswitcher.models import FirstSyncScope, Host, ValidationError

__all__ = ["ManualFlatpakSyncJob"]

# The origin every item this job produces carries, and so the slice of an `item_id` space
# that belongs to it. Named once: detection and the mark reconciliation key on the same
# string.
_ORIGIN = "flatpak-no-remote"

# Apps only, matching what `flatpak_sync` replicates: a runtime is pulled in by the app that
# needs it and is never installed on its own. Four columns: unreproducibility asks where a
# ref came from and where it lives — `<application>/<arch>/<branch>` is the ref
# `flatpak install`/`uninstall` accept — and the version is what the two machines' copies of
# one ref are compared on (`PKG-FR-MANUAL-VERSION`). The application id stays out; the ref
# already carries it.
_LIST_APPS_CMD = "flatpak list --app --columns=origin,installation,ref,version"

# Every installed ref, runtimes included — used only for the presence check behind mark
# reconciliation. Narrowing a "does this machine still have it" question to a subset is how
# a mark on something still installed gets dropped (`flatpak_sync.observe_absent_marks`).
_LIST_ALL_REFS_CMD = "flatpak list --columns=origin,installation,ref,version"


@dataclass(frozen=True)
class _InstalledRef:
    """One row of `_LIST_APPS_CMD`. Carries `scope` and `origin` so the shared predicate
    (`flatpak_policy.ScopedOrigin`) can read it without this module's dataclass leaving it.
    """

    scope: Literal["user", "system"]
    ref: str
    origin: str
    version: str = ""


def _parse_refs(output: str) -> list[_InstalledRef]:
    """Parse a `flatpak list --columns=origin,installation,ref,version` run into rows.

    A line whose `installation` field is neither `user` nor `system` is skipped rather than
    guessed at, for the reason `flatpak_sync._parse_flatpak_list` records: flatpak permits
    further named installations, and a third scope needs its own modelling decision.

    The version column is genuinely optional in flatpak's own output — an app whose appdata
    declares none prints an empty field — so a row is accepted with three or four fields and
    a missing version stays the empty string, which the version comparison reads as "this
    machine did not say".
    """
    refs: list[_InstalledRef] = []
    for line in lines_of(output):
        fields = line.split("\t")
        if len(fields) not in (3, 4):
            continue
        origin, installation, ref = fields[0], fields[1], fields[2]
        if installation not in ("user", "system"):
            continue
        scope: Literal["user", "system"] = "user" if installation == "user" else "system"
        refs.append(_InstalledRef(scope=scope, ref=ref, origin=origin, version=fields[3] if len(fields) == 4 else ""))
    return refs


def _identifier(row: _InstalledRef) -> str:
    """`<scope>:<ref>`, so `item_id` reads
    `unreproducible:flatpak-no-remote:<scope>:<application>/<arch>/<branch>`.

    ADR-020 makes a ref's identity its full ref WITHIN its installation scope — user and
    system are separate installations and the same application can be in both, from
    different origins, at different versions — so scope has to be inside the identifier
    rather than a field beside it, exactly as it is inside `FlatpakItem.item_id`.
    """
    return f"{row.scope}:{row.ref}"


def _item(row: _InstalledRef, *, own_finding: bool = True) -> UnreproducibleItem:
    """This job's item for one installed ref.

    `own_finding` is False for a target row some remote CAN supply: it is software that is
    there, so the source's copy of it is never offered for install, but it is
    `flatpak_sync`'s to remove rather than this job's (`PKG-FR-MANUAL-REMOVE`). Its label
    then says only what it is, since the origin clause would be a false statement about a
    remote that is configured.
    """
    return UnreproducibleItem(
        origin=_ORIGIN,
        identifier=_identifier(row),
        label=f"{row.ref} ({row.scope}, from {row.origin} — no such remote is configured)"
        if own_finding
        else f"{row.ref} ({row.scope})",
        own_finding=own_finding,
    )


class ManualFlatpakSyncJob(UnreproducibleSyncJob):
    """Detect, review and reproduce flatpak refs installed from no configured remote, on
    this job's own enable flag independent of `flatpak_sync`'s.

    Supplies the two detection hooks `UnreproducibleSyncJob` leaves abstract; everything
    from the diff onwards — the snippet registry, its push and consent question, the review
    grouping and the replay — is inherited.
    """

    name: ClassVar[str] = "manual_flatpak_sync"
    manager_id: ClassVar[str] = "manual_flatpak"

    # No configurable properties, mirroring the other package jobs: only the enable flag in
    # sync_jobs is needed. A job earns a config SECTION only when it has a real key, so there is
    # no `manual_flatpak_sync:` block in default-config.yaml, but the in-code CONFIG_SCHEMA
    # ClassVar still declares the empty object every job carries.
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    # -- Detection (D-18), run on both machines (`PKG-FR-MANUAL-DIFF`) -------------------

    async def _configured_remotes(self, executor: Executor, machine: str) -> dict[str, frozenset[str]]:
        """`scope -> the remote names configured there`, one `flatpak remotes` per scope.

        Per scope and never combined, because flatpak tracks remotes per installation
        (D-14): `flathub` configured system-wide says nothing about a user-scope ref whose
        origin is `flathub`. Guarded on the exit code alone — a scope with no remote at all
        is an ordinary machine, and `flatpak remotes` exits 0 for it.
        """
        configured: dict[str, frozenset[str]] = {}
        for scope in SCOPES:
            command = FLATPAK_REMOTE_NAMES_CMD.format(flag=scope_flag(scope))
            result = await executor.run_command(command)
            require_answer(command, result, machine)
            configured[scope] = remote_names(result.stdout)
        return configured

    async def _installed_apps(self, executor: Executor, machine: str) -> list[_InstalledRef]:
        """Every application ref installed on `machine`, in either scope.

        Guarded on the exit code alone: a machine with no flatpak application installed is
        an ordinary machine and prints nothing, while a broken flatpak exits 1 with `error:`
        on stderr (ADR-022, and the guard `flatpak_sync.capture_source_items` puts on the
        same command).
        """
        result = await executor.run_command(_LIST_APPS_CMD)
        require_answer(_LIST_APPS_CMD, result, machine)
        return _parse_refs(result.stdout)

    @override
    async def capture_source_items(self) -> Sequence[UnreproducibleItem]:
        """The source's refs no remote can reproduce: the installed applications whose
        origin names no remote configured in their own scope (`flatpak_policy`).
        """
        apps = await self._installed_apps(self.source, self.machines.source)
        if not apps:
            return []
        _, unreproducible = partition_unreproducible(
            apps, await self._configured_remotes(self.source, self.machines.source)
        )
        return [_item(row) for row in sorted(unreproducible, key=lambda row: (row.scope, row.ref))]

    @override
    async def query_target_items(self) -> Sequence[UnreproducibleItem]:
        """What the TARGET holds, in the source's own identities (`PKG-FR-MANUAL-DIFF`).

        A ref is HELD when the target has it installed in the same scope AT ALL, whatever
        origin put it there — a bundle the user already carried across by hand needs no
        snippet replayed over it — and the two copies' versions are compared instead of the
        source's being offered again.

        `own_finding` is the reproducibility question asked of the TARGET's own remotes, and
        it costs the two `flatpak remotes` reads this job used to skip here: a ref whose
        origin names no remote the target configures, and which the source no longer has, is
        a bundle install the source has dropped and this job's to remove
        (`PKG-FR-MANUAL-REMOVE`). Every other row stays unflagged, so a ref some remote can
        supply is never deleted here — that is `flatpak_sync`'s decision, taken with its own
        remote bookkeeping behind it.
        """
        apps = await self._installed_apps(self.target, self.machines.target)
        if not apps:
            return []
        _, unreproducible = partition_unreproducible(
            apps, await self._configured_remotes(self.target, self.machines.target)
        )
        unreproducible_ids = {_identifier(row) for row in unreproducible}
        return [_item(row, own_finding=_identifier(row) in unreproducible_ids) for row in apps]

    @override
    async def installed_versions(self, item_ids: Collection[str], *, on_source: bool) -> Mapping[str, str | None]:
        """Each ref's installed version on one machine, from one `flatpak list`.

        Read fresh rather than taken from the capture: the converge loop asks this again
        after every replay, and an answer from before the change would report every
        convergence as successful. An app whose appdata declares no version answers `None`,
        which produces no item rather than a claimed difference.
        """
        executor = self.source if on_source else self.target
        machine = self.machines.source if on_source else self.machines.target
        result = await executor.run_command(_LIST_ALL_REFS_CMD)
        require_answer(_LIST_ALL_REFS_CMD, result, machine)
        versions = {_identifier(row): row.version for row in _parse_refs(result.stdout)}
        prefix = UnreproducibleItem.id_prefix(_ORIGIN)
        return {item_id: versions.get(item_id.removeprefix(prefix)) or None for item_id in item_ids}

    @override
    def removal_command(self, item: UnreproducibleItem) -> str:
        """`flatpak uninstall` for a ref no remote can supply that the source has dropped.

        Privileged if and only if the ref's own scope is `system`, which is the rule every
        flatpak write in this codebase follows: a user-scope run never has to ask for root
        (`PKG-FR-FLATPAK-PRIVILEGE`). The full `<application>/<arch>/<branch>` is named
        because the bare application id is what flatpak refuses to guess between when a
        machine holds two branches of one app.
        """
        scope, _, ref = item.identifier.partition(":")
        privilege = "sudo " if scope == "system" else ""
        return f"{privilege}flatpak uninstall --assumeyes {scope_flag(scope)} {shlex.quote(ref)}"

    @override
    async def observe_absent_marks(self, entries: Mapping[str, DecisionEntry], *, on_source: bool) -> frozenset[str]:
        """The marked refs one machine no longer has installed.

        Asked of BOTH machines, unlike `plan()`, which reads the source's file alone: which
        marks silence a FINDING is the source's business, but whether a marked ref is still
        on the machine holding the mark is a question about that machine and nothing else.

        The listing is `_LIST_ALL_REFS_CMD` rather than the `--app` one detection uses, for
        the reason `flatpak_sync.observe_absent_marks` records: this is a statement about
        what the machine HAS, and narrowing a presence check to a subset is how a mark on
        something still installed gets dropped. Presence alone, never the reproducibility
        question: a marked ref whose remote the user has since re-added is still installed,
        and dropping its mark on those grounds would re-offer software the user asked to be
        left alone.

        Entries this job cannot recognise are left exactly where they are.
        """
        executor = self.source if on_source else self.target
        machine = self.machines.source if on_source else self.machines.target

        prefix = UnreproducibleItem.id_prefix(_ORIGIN)
        marked = {item_id: item_id.removeprefix(prefix) for item_id in entries if item_id.startswith(prefix)}
        if not marked:
            return frozenset()

        result = await executor.run_command(_LIST_ALL_REFS_CMD)
        require_answer(_LIST_ALL_REFS_CMD, result, machine)
        installed = {_identifier(row) for row in _parse_refs(result.stdout)}
        return frozenset(item_id for item_id, identifier in marked.items() if identifier not in installed)

    @override
    async def validate(self) -> list[ValidationError]:
        """`flatpak` on both machines: the source is read for detection and the target for
        what it already holds (`PKG-FR-MANUAL-DIFF`). Both are only ever READ here, so no
        sudo is needed for detection — and a snippet's own sudo needs are unpredictable (an
        opaque blob, D-20), so this job does NOT pre-validate target sudo; a snippet that
        needs it and lacks it fails as a per-item converge failure (D-27), reported like any
        other.

        Sequential checks appending to `errors`, never raising mid-validate (matches
        `FlatpakSyncJob.validate()`'s shape).
        """
        errors: list[ValidationError] = []

        source_check = await self.source.run_command("flatpak --version")
        if not source_check.success:
            errors.append(
                self._validation_error(
                    Host.SOURCE, "flatpak is not available on source (required to detect unreproducible refs)"
                )
            )

        target_check = await self.target.run_command("flatpak --version")
        if not target_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET, "flatpak is not available on target (required to tell what it already has)"
                )
            )

        return errors

    @classmethod
    @override
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Name this job's destructive first-sync scope (ADR-015): replaying install
        snippets for flatpak refs no remote can supply, and removing the ones the source has
        dropped."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=[
                "flatpak refs no remote can supply (via recorded install snippets)",
                "flatpak refs no remote can supply that the source no longer has (flatpak uninstall)",
            ],
            mechanism="replay install snippet or uninstall, per item, after review",
        )
