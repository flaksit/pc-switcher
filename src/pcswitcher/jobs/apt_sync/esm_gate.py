"""The one question this job asks that is not about an item (ADR-020 D-38).

Writing the source's `ubuntu-esm-*` sources to a target with no Ubuntu Pro attachment is not
harmless: `esm.ubuntu.com` serves its INDEX publicly, so `apt-get update` succeeds and the ESM
suites win candidate selection, and the failure surfaces much later as a 401 on the `.deb` of
the target's next install of an ESM-covered package — a failure nobody traces back to a sync.
pc-switcher cannot attach the target itself, so it asks.

Only the parsed `attached` boolean ever leaves this module: `pro status --format json` also
names the subscriber's account, so nothing else may be logged, shown or put in a
`JobSkipped` reason (D-38, and `PKG-FR-ESM-PRIVACY` is honoured by construction here rather
than by filtering downstream).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from pcswitcher.jobs.apt_sync.items import ESM_SOURCE_FILENAMES
from pcswitcher.jobs.apt_sync.messages import build_esm_gate_message
from pcswitcher.jobs.apt_sync.probe import AptProbe
from pcswitcher.jobs.apt_sync.reporting import Log
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.items import Machines
from pcswitcher.models import Host, JobSkipped, LogLevel

# `pro status --format json` exits 0 for an unprivileged user and reports a top-level
# `attached` boolean. Its payload ALSO names the subscriber's account, so only the parsed
# boolean may ever be logged, shown or put in a `JobSkipped` reason.
PRO_STATUS_COMMAND = "pro status --format json"


class EsmGate:
    """Ask before ESM sources travel to an unattached target, and remember what was withheld."""

    def __init__(
        self,
        *,
        probe: AptProbe,
        machines: Machines,
        job_name: str,
        manager_id: str,
        log: Log,
    ) -> None:
        self._probe = probe
        self._machines = machines
        self._job_name = job_name
        self._manager_id = manager_id
        self._log = log
        # ESM sources the gate held back. Only ever non-empty under `--dry-run`: a real
        # unattached run raises `JobSkipped` instead of writing a subset (D-38).
        self._withheld: frozenset[str] = frozenset()

    @property
    def withheld(self) -> frozenset[str]:
        return self._withheld

    def pending(self, source_digests: Mapping[str, str], target_digests: Mapping[str, str]) -> tuple[str, ...]:
        """The `ubuntu-esm-*` filenames this run's always-sync bucket would put on the
        target: present on the source, and absent or different there.

        Same predicate the derived-write set applies to the whole distribution bucket,
        so the gate can never ask about a file the run would not have written.
        """
        return tuple(
            filename
            for filename in sorted(ESM_SOURCE_FILENAMES & frozenset(source_digests))
            if source_digests[filename] != target_digests.get(filename)
        )

    def withhold(self, esm_files: Sequence[str]) -> None:
        """Record what a dry run would not have written, so the preview cannot claim writes no
        real run would make."""
        self._withheld = frozenset(esm_files)

    async def attached(self) -> bool:
        """Whether the target reports an Ubuntu Pro attachment.

        Every failure mode — no `pro` binary, a non-zero exit, output that will not parse —
        answers False, which is the recoverable one: False asks a question the user can
        answer, True writes files that break the target's next install.

        The payload also carries the subscriber's account, so nothing but the parsed
        boolean leaves this method (D-38).
        """
        result = await self._probe.target_pro_attached(PRO_STATUS_COMMAND)
        if not result.success:
            return False
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return False
        return isinstance(payload, dict) and payload.get("attached") is True

    async def allow(self, esm_files: Sequence[str], *, context: JobContext) -> bool:
        """Ask before putting `ubuntu-esm-*` sources on a target that is not Pro-attached
        (D-38), and return whether they may travel.

        `context` is passed per call rather than held: the orchestrator injects the reviewer
        into `JobContext` after the job is constructed, and a `JobContext` is a frozen
        dataclass replaced wholesale when that happens — a gate holding the one it was built
        with would ask a reviewer that is no longer the run's.

        Two real outcomes: True, the target is attached (possibly after the user attached it
        and this re-probed), so the files travel with the rest of the always-sync bucket;
        or `JobSkipped`, because the user chose to skip or nobody was there to answer. False
        is reachable only under `--dry-run`, where neither answer writes anything.

        Skipping the whole job rather than withholding the two files is the user's ruling
        and the only coherent partial outcome: `/etc/apt/preferences.d` always-syncs with no
        derivation predicate, so the source's ESM pins land on the target whether or not the
        sources they name do, leaving a candidate selection that matches neither machine.
        Skipping leaves `/etc/apt` exactly as it was.

        The re-check loop is deliberately unbounded — the user's ruling: re-probing is free,
        and the exit is choosing to skip.
        """
        if await self.attached():
            return True

        named = ", ".join(esm_files)
        if context.dry_run:
            self._log(
                Host.TARGET,
                LogLevel.WARNING,
                f"[dry-run] {self._machines.target} reports no Ubuntu Pro attachment, so {named} would not be "
                f"written: a real run would skip {self._job_name} entirely and leave every other job running.",
            )
            return False

        assert context.reviewer is not None, (
            f"{self._manager_id} sync has no reviewer; the orchestrator must inject one "
            "through JobContext.reviewer before plan()."
        )
        target = self._machines.target
        message = build_esm_gate_message(esm_files, self._machines, self._job_name)
        while True:
            answer = await context.reviewer.ask_gate(
                title=f"{target} needs an Ubuntu Pro attachment",
                message=message,
                proceed_label=f"I have attached {target} — check again and continue",
                stop_label=f"Skip {self._job_name} this run (every other job still runs)",
            )
            if answer is None:
                raise JobSkipped(
                    self._job_name,
                    f"{self._machines.source} carries {named} and {target} reports no Ubuntu Pro attachment; "
                    "no TTY was available to ask whether to attach it or skip",
                )
            if not answer:
                raise JobSkipped(
                    self._job_name,
                    f"{self._machines.source} carries {named}, {target} reports no Ubuntu Pro attachment, "
                    "and the user chose to skip rather than attach it",
                )
            if await self.attached():
                return True
            self._log(Host.TARGET, LogLevel.WARNING, f"{target} still reports no Ubuntu Pro attachment.")
