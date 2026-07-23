"""`apt_sync`: install-missing-on-target apt packages, the tracer slice (D-01, D-03, ADR-020).

Captures the source's `apt-mark showmanual` set with `dpkg-query`-sourced versions
(never `apt list --installed` — its own manpage says the output has no stable scripting
contract), diffs it against the same query on the target, and installs the missing
packages via `apt-get install` after every approved item's transaction is simulated with
`apt-get -s` to guard against apt silently removing something the review never showed
(D-24, D-25, T-02-32).

This slice handles only the `MISSING_ON_TARGET`/`INSTALL` direction for apt packages.
Removal, version-mismatch/downgrade handling, apt sources/keys/pins/config, and the
other two managers (snap, flatpak) are later Phase 2 plans.
"""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, override

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.package_items import AptPackageItem, DiffAction, ItemDiff
from pcswitcher.jobs.package_sync_core import ConvergeItemFailed, PackageSyncJob
from pcswitcher.models import CommandResult, FirstSyncScope, Host, ValidationError

__all__ = ["AptSyncJob", "AptTransactionPreview", "simulate_apt_transaction"]

# `AptPackageItem.item_id` is always this prefix + the package name (package_items.py).
# Parsing the name back out of the id is a legitimate use of a stable identity string,
# not string-matching on manager-specific content.
_APT_PACKAGE_ID_PREFIX = "apt:package:"


def _package_name(item_id: str) -> str:
    if not item_id.startswith(_APT_PACKAGE_ID_PREFIX):
        raise ValueError(f"Not an apt package item id: {item_id!r}")
    return item_id.removeprefix(_APT_PACKAGE_ID_PREFIX)


@dataclass(frozen=True)
class AptTransactionPreview:
    """The parsed result of `apt-get -s <args>` — what apt says it WOULD do.

    `apt-get -s` is the only honest answer to "what will this command do": apt resolves
    dependencies and conflicts at run time, so the package the user ticked and the
    transaction apt actually runs are not necessarily the same thing.
    """

    installs: tuple[str, ...]
    removals: tuple[str, ...]
    raw: str


async def simulate_apt_transaction(
    executor: RemoteExecutor, apt_args: str, *, login_shell: bool | None = False
) -> AptTransactionPreview:
    """Run `apt-get -s <apt_args>` on `executor` and parse its Inst/Remv action lines.

    Parses by the leading verb token (`Inst`/`Remv`), not by position within the line —
    the rest of an apt-get -s line's shape varies with the package and its dependency
    resolution. No `sudo` is needed: simulation is read-only.
    """
    result = await executor.run_command(f"apt-get -s {apt_args}", login_shell=login_shell)
    installs: list[str] = []
    removals: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        verb, name = parts[0], parts[1]
        if verb == "Inst":
            installs.append(name)
        elif verb == "Remv":
            removals.append(name)
    return AptTransactionPreview(installs=tuple(installs), removals=tuple(removals), raw=result.stdout)


class AptSyncJob(PackageSyncJob):
    """Install apt packages missing on the target, after the coordinator's batched review."""

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

    @override
    async def capture_source_items(self) -> Sequence[AptPackageItem]:
        """Manually-installed apt packages on the source, with versions (D-03)."""
        manual = await self.source.run_command("apt-mark showmanual")
        return await self._resolve_versions(manual.stdout, self.source.run_command)

    @override
    async def query_target_items(self) -> Sequence[AptPackageItem]:
        """The target's own manually-installed apt packages, with versions."""
        manual = await self.target.run_command("apt-mark showmanual", login_shell=False)

        async def run(cmd: str) -> CommandResult:
            return await self.target.run_command(cmd, login_shell=False)

        return await self._resolve_versions(manual.stdout, run)

    @staticmethod
    async def _resolve_versions(
        showmanual_output: str, run: Callable[[str], Awaitable[CommandResult]]
    ) -> list[AptPackageItem]:
        """Resolve every name's version with ONE `dpkg-query` call (RESEARCH.md)."""
        names = [line.strip() for line in showmanual_output.splitlines() if line.strip()]
        if not names:
            return []

        quoted = " ".join(shlex.quote(name) for name in names)
        # dpkg-query, not `apt list --installed`: apt's own manpage warns the latter's
        # output has no stable contract for scripting. The literal \t/\n below are
        # dpkg-query's OWN format-string escapes (interpreted by dpkg-query, not the
        # shell) — hence a plain (non-f) string so Python leaves them as two-char
        # backslash sequences for dpkg-query to expand into real tab/newline.
        versions_result = await run("dpkg-query -W -f='${Package}\\t${Version}\\n' " + quoted)

        versions: dict[str, str] = {}
        for line in versions_result.stdout.splitlines():
            if not line.strip():
                continue
            pkg_name, _, version = line.partition("\t")
            versions[pkg_name] = version

        return [AptPackageItem(name=name, version=versions.get(name, "")) for name in names]

    @override
    async def converge(self, diff: ItemDiff) -> CommandResult:
        """Simulate the exact apt transaction, guard it, then run the real install.

        One package per invocation (D-27) so a single bad package cannot fail the whole
        batch, and so each package's simulation corresponds exactly to the command that
        follows it. The target resolves dependencies and downloads from its own repos
        (D-28) — no source cache is consulted.
        """
        if diff.action != DiffAction.INSTALL:
            # Only INSTALL exists on this tracer slice; plan 02-05 adds removal/change
            # and extends the transaction guard below to cover downgrades too.
            raise ConvergeItemFailed(
                f"AptSyncJob.converge: unsupported action {diff.action.value!r} for {diff.label} "
                "(only 'install' exists on this tracer slice)"
            )

        name = _package_name(diff.item_id)
        quoted = shlex.quote(name)
        install_args = f"install -y --no-install-recommends {quoted}"

        preview = await simulate_apt_transaction(self.target, install_args, login_shell=False)
        if preview.removals:
            removed = ", ".join(preview.removals)
            # Enforcement of this plan's own prohibition that an install-direction item
            # removes nothing as a side effect (D-24, D-25, T-02-32): without this check
            # the prohibition is a sentence in a document no code verifies. Downgrade
            # detection needs compare_deb_versions (dpkg --compare-versions), which
            # arrives in plan 02-05; that plan extends this guard to cover downgrades.
            raise ConvergeItemFailed(
                f"install of {name} refused: apt-get -s would also remove {removed}, "
                "which was never reviewed (D-24/D-25)"
            )

        real_cmd = f"sudo DEBIAN_FRONTEND=noninteractive apt-get {install_args}"
        return await self.target.run_command(real_cmd, login_shell=False)

    @override
    async def validate(self) -> list[ValidationError]:
        """apt-mark availability on both ends, sudo on target, dpkg lock free on target.

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

        sudo_check = await self.target.run_command("sudo -n true", login_shell=False)
        if not sudo_check.success:
            errors.append(
                self._validation_error(
                    Host.TARGET, "passwordless sudo is not available on target (required for apt-get install)"
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
