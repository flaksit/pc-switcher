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

from collections.abc import Mapping, Sequence
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
# needs it and is never installed on its own. Three columns, because unreproducibility asks
# only where a ref came from and where it lives — `<application>/<arch>/<branch>` is the ref
# `flatpak install`/`uninstall` accept, and a snippet is what installs one here anyway, so
# neither the application id nor the version is a fact this job acts on.
_LIST_APPS_CMD = "flatpak list --app --columns=origin,installation,ref"

# Every installed ref, runtimes included — used only for the presence check behind mark
# reconciliation. Narrowing a "does this machine still have it" question to a subset is how
# a mark on something still installed gets dropped (`flatpak_sync.observe_absent_marks`).
_LIST_ALL_REFS_CMD = "flatpak list --columns=origin,installation,ref"


@dataclass(frozen=True)
class _InstalledRef:
    """One row of `_LIST_APPS_CMD`. Carries `scope` and `origin` so the shared predicate
    (`flatpak_policy.ScopedOrigin`) can read it without this module's dataclass leaving it.
    """

    scope: Literal["user", "system"]
    ref: str
    origin: str


def _parse_refs(output: str) -> list[_InstalledRef]:
    """Parse a `flatpak list --columns=origin,installation,ref` run into rows.

    A line whose `installation` field is neither `user` nor `system` is skipped rather than
    guessed at, for the reason `flatpak_sync._parse_flatpak_list` records: flatpak permits
    further named installations, and a third scope needs its own modelling decision.
    """
    refs: list[_InstalledRef] = []
    for line in lines_of(output):
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        origin, installation, ref = fields
        if installation not in ("user", "system"):
            continue
        scope: Literal["user", "system"] = "user" if installation == "user" else "system"
        refs.append(_InstalledRef(scope=scope, ref=ref, origin=origin))
    return refs


def _item(row: _InstalledRef) -> UnreproducibleItem:
    """This job's item for one installed ref.

    The identifier is `<scope>:<ref>`, so `item_id` reads
    `unreproducible:flatpak-no-remote:<scope>:<application>/<arch>/<branch>`. ADR-020 makes
    a ref's identity its full ref WITHIN its installation scope — user and system are
    separate installations and the same application can be in both, from different origins,
    at different versions — so scope has to be inside the identifier rather than a field
    beside it, exactly as it is inside `FlatpakItem.item_id`.
    """
    return UnreproducibleItem(
        origin=_ORIGIN,
        identifier=f"{row.scope}:{row.ref}",
        label=f"{row.ref} ({row.scope}, from {row.origin} — no such remote is configured)",
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
        """What the TARGET already holds, in the source's own identities, so `plan()` can
        drop a finding that is already there (`PKG-FR-MANUAL-DIFF`).

        A ref is held when the target has it installed in the same scope AT ALL, whatever
        origin put it there: a bundle the user already carried across by hand needs no
        snippet replayed over it, and re-asking the reproducibility question on the target
        would cost two more `flatpak remotes` reads to answer something its own installed
        set already answers.
        """
        return [_item(row) for row in await self._installed_apps(self.target, self.machines.target)]

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
        installed = {f"{row.scope}:{row.ref}" for row in _parse_refs(result.stdout)}
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
        snippets for flatpak refs no remote can supply."""
        return FirstSyncScope(
            job_name=cls.name,
            scope_items=["flatpak refs no remote can supply (via recorded install snippets)"],
            mechanism="replay install snippet per item, after review",
        )
