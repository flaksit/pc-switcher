"""Unit tests for `ManualInstallsSyncJob` (plan 02-17): the fourth package job owning
unreproducible detection (D-18/D-19), snippet replay (D-20), and the D-21 skip-once
resolution semantics.

All executor interactions are mocked; no real dpkg/apt-cache/sudo commands run. Detection
and snippet-replay coverage that previously lived against `AptSyncJob` in
`test_package_state.py`/`test_apt_sync.py` moved here when the ownership moved (D-18).
"""

from __future__ import annotations

import io
import logging
import re
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.console import Console
from rich.panel import Panel

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.manual_installs_sync import ManualInstallsSyncJob, UnreproducibleItem
from pcswitcher.jobs.packages.apt_policy import installed_origins_by_package
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import (
    UNREPRODUCIBLE_REVIEW_ACTION,
    Decision,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.jobs.packages.state import SNIPPET_REGISTRY_RELPATH
from pcswitcher.jobs.packages.sync_core import PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, Host, JobSkipped, SyncAbortedByUser, ValidationError
from pcswitcher.orchestrator import Orchestrator

# -- Real `apt-cache policy` output, verbatim ------------------------------------------
#
# Measured on a live Ubuntu 24.04 machine. The four shapes the detector has to tell apart:
# a hand-installed `.deb`, a repo-installed package, a package every version of which is
# pinned below zero, and a repo-installed automatic dependency. Only the first is
# unreproducible, and all four report a `Candidate:` — three of them a real version, and
# the pinned one `(none)` despite being fully repo-available.


def _hand_deb_policy(name: str, version: str = "1.0") -> str:
    """`_POLICY_HAND_DEB`'s shape for an arbitrary package: an installed version whose
    only version-table origin is `/var/lib/dpkg/status`."""
    return (
        f"{name}:\n"
        f"  Installed: {version}\n"
        f"  Candidate: {version}\n"
        "  Version table:\n"
        f" *** {version} 100\n"
        "        100 /var/lib/dpkg/status\n"
    )


# `code`, installed from a downloaded `.deb`: apt reports the installed version as the
# candidate because dpkg's status entry supplies it.
_POLICY_HAND_DEB = _hand_deb_policy("code", "1.129.1-1784303641")

# `gh`, installed from its vendor repository — note its block ALSO carries a
# `/var/lib/dpkg/status` line, and its older version rows name three Ubuntu URIs that are
# not where the installed version came from.
_POLICY_REPO_INSTALLED = """gh:
  Installed: 2.96.0
  Candidate: 2.96.0
  Version table:
 *** 2.96.0 1001
        500 https://cli.github.com/packages stable/main amd64 Packages
        100 /var/lib/dpkg/status
     2.45.0-1ubuntu0.3+esm3 510
        510 https://esm.ubuntu.com/apps/ubuntu noble-apps-security/main amd64 Packages
     2.45.0-1ubuntu0.3 500
        500 http://ftp.belnet.be/ubuntu noble-updates/universe amd64 Packages
        500 http://security.ubuntu.com/ubuntu noble-security/universe amd64 Packages
     2.45.0-1build1 500
        500 http://ftp.belnet.be/ubuntu noble/universe amd64 Packages
"""

# `docker.io`, fully repo-available but pinned below zero by a local `preferences.d` file:
# `Candidate: (none)` with real repository origins on the installed version.
_POLICY_PINNED_NO_CANDIDATE = """docker.io:
  Installed: 29.1.3-0ubuntu3~24.04.2
  Candidate: (none)
  Version table:
 *** 29.1.3-0ubuntu3~24.04.2 -1
        510 https://esm.ubuntu.com/apps/ubuntu noble-apps-security/main amd64 Packages
        500 http://ftp.belnet.be/ubuntu noble-updates/universe amd64 Packages
        500 http://security.ubuntu.com/ubuntu noble-security/universe amd64 Packages
        100 /var/lib/dpkg/status
     24.0.7-0ubuntu4 500
        500 http://ftp.belnet.be/ubuntu noble/universe amd64 Packages
"""

# `mytool`, hand-installed at a version NEWER than any repository offers: the `***` row
# carries only dpkg's status file while an OLDER row names a real repository. The
# repository cannot supply the version this machine actually has.
_POLICY_NEWER_THAN_REPO = """mytool:
  Installed: 3.0.0
  Candidate: 3.0.0
  Version table:
 *** 3.0.0 100
        100 /var/lib/dpkg/status
     2.1.0 500
        500 http://ftp.belnet.be/ubuntu noble/universe amd64 Packages
"""

# `7zip`, pulled in from a repository as an automatic dependency.
_POLICY_AUTO_DEP = """7zip:
  Installed: 23.01+dfsg-11ubuntu0.1~esm1
  Candidate: 23.01+dfsg-11ubuntu0.1~esm1
  Version table:
 *** 23.01+dfsg-11ubuntu0.1~esm1 510
        510 https://esm.ubuntu.com/apps/ubuntu noble-apps-security/main amd64 Packages
        100 /var/lib/dpkg/status
     23.01+dfsg-11 500
        500 http://ftp.belnet.be/ubuntu noble/universe amd64 Packages
"""

# The unowned scan hands `dpkg --search` one path dpkg is certain to own, and reads its
# "owned" line as the proof that dpkg answered at all. Every stub of that command must
# therefore reply with it, exactly as a working dpkg would.
DPKG_WITNESS_LINE = "dpkg: /usr/bin/dpkg\n"

# A `package-snippets.yaml` registry holding one snippet for the brscan3 no-candidate item.
BRSCAN3_REGISTRY_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 (no apt candidate)\n"
    "    body: sudo dpkg --install /tmp/brscan3.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: laptop\n"
)


# The `dpkg-query` that names the source's installed set, matched by its one distinctive
# field so a fixture keys on the question rather than on the whole format string.
_STATUS_QUERY = "db:Status-Status"


def installed_on_source(*names: str) -> CommandResult:
    """What that `dpkg-query` answers on a machine holding exactly `names`."""
    return CommandResult(0, "".join(f"{name}\tinstalled\n" for name in names), "")


def respond_to(
    mapping: dict[str, CommandResult], default: CommandResult | None = None
) -> Callable[..., CommandResult]:
    """Build a run_command side_effect matching by substring (first match wins)."""
    fallback = default if default is not None else CommandResult(exit_code=0, stdout="", stderr="")

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


def make_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    dry_run: bool = False,
    reviewer: object | None = None,
    confirmer: object | None = None,
    enabled_sync_jobs: dict[str, bool] | None = None,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target = MagicMock()
    target.run_command = AsyncMock(side_effect=respond_to(target_responses or {}))
    target.send_file = AsyncMock(return_value=None)
    context = JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
        confirmer=confirmer,  # pyright: ignore[reportArgumentType]
        reviewer=reviewer,  # pyright: ignore[reportArgumentType]
        enabled_sync_jobs=enabled_sync_jobs,
    )
    return context, source, target


class FakeConfirmer:
    """A `Confirmer` returning a preset answer (or mimicking the real non-interactive
    behavior of returning `allow`), recording every call for assertions."""

    def __init__(self, *, approve: bool | None = None, return_allow: bool = False) -> None:
        self._approve = approve
        self._return_allow = return_allow
        self.calls: list[dict[str, object]] = []

    async def confirm(
        self,
        *,
        title: str,
        message: str,
        allow: bool,
        allow_flag: str,
        log_extra: dict[str, object] | None = None,
    ) -> bool:
        self.calls.append({"title": title, "message": message, "allow": allow, "allow_flag": allow_flag})
        if self._return_allow:
            # Mirror TerminalUIConfirmer's non-interactive branch: the answer IS `allow`.
            return allow
        assert self._approve is not None, "FakeConfirmer needs either approve= or return_allow=True"
        return self._approve


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


def decision_file_writes(mock: MagicMock) -> list[str]:
    """Every command that WRITES this job's machine-local decision file on `mock`'s
    machine — the `mv --force` half of the atomic write, so the file's `cat` read never
    counts as a write."""
    return [
        call.args[0]
        for call in mock.run_command.call_args_list
        if "manual.decisions.yaml" in call.args[0] and "mv --force" in call.args[0]
    ]


def registry_writes(mock: MagicMock) -> list[str]:
    """Every command that WRITES the install-snippet registry on `mock`'s machine."""
    return [
        call.args[0]
        for call in mock.run_command.call_args_list
        if "package-snippets" in call.args[0] and "mv --force" in call.args[0]
    ]


def job_diff(item_id: str, action: DiffAction) -> ItemDiff:
    return ItemDiff(
        item_class=ItemClass.UNREPRODUCIBLE,
        diff_class=DiffClass.UNREPRODUCIBLE,
        action=action,
        item_id=item_id,
        label=item_id,
        detail=None,
    )


class FakeReviewer:
    """A `Reviewer` returning a caller-supplied outcome, recording the groups it saw."""

    def __init__(
        self,
        *,
        decisions: dict[str, Decision] | None = None,
        snippets: dict[str, str] | None = None,
        unresolved: tuple[str, ...] = (),
        was_interactive: bool = True,
    ) -> None:
        self._decisions = decisions or {}
        self._snippets = snippets or {}
        self._unresolved = unresolved
        self._was_interactive = was_interactive
        self.groups_seen: tuple[ReviewGroup, ...] | None = None

    async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None:
        raise AssertionError(f"manual_installs_sync has no gate question; asked {title!r}")

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self.groups_seen = tuple(groups)
        item_ids = {entry.item_id for group in groups for entry in group.entries}
        decisions = {item_id: self._decisions.get(item_id, Decision.SKIP_ONCE) for item_id in item_ids}
        return ReviewOutcome(
            decisions=decisions,
            was_interactive=self._was_interactive,
            snippets=self._snippets,
            unresolved=self._unresolved,
        )


class TestNoCandidateDetection:
    """apt-no-candidate scan: a manually-installed package no configured repository can
    supply becomes an UNREPRODUCIBLE diff (D-18).

    The predicate is the INSTALLED version's repository origins, never the `Candidate:`
    line: dpkg's own status entry makes apt report a hand-installed package's version as
    its candidate, while a negatively-pinned but fully repo-available package reports
    `Candidate: (none)`.
    """

    @staticmethod
    def _unreproducible_ids(plan: PackagePlan) -> set[str]:
        return {d.item_id for d in plan.diffs if d.item_class == ItemClass.UNREPRODUCIBLE}

    @pytest.mark.asyncio
    async def test_package_whose_only_origin_is_dpkg_status_is_unreproducible(self) -> None:
        """G1 — a hand-downloaded `.deb` whose installed version only dpkg's status file
        accounts for is presented as an item no package manager can reproduce."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("code"),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        unreproducible = [d for d in plan.diffs if d.item_class == ItemClass.UNREPRODUCIBLE]
        assert len(unreproducible) == 1
        assert unreproducible[0].item_id == "unreproducible:apt-no-candidate:code"
        assert unreproducible[0].diff_class == DiffClass.UNREPRODUCIBLE
        assert unreproducible[0].action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_repo_installed_package_is_not_unreproducible(self) -> None:
        """G2 — `gh` comes from its vendor repository and is reinstallable. Its block also
        carries a `/var/lib/dpkg/status` line — every installed package's does — so
        "the block mentions dpkg status" is not the predicate.
        """
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("gh"),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == set()

    @pytest.mark.asyncio
    async def test_negatively_pinned_package_is_not_unreproducible(self) -> None:
        """G3 — `docker.io` reports `Candidate: (none)` only because a local pin holds every
        version below zero. It is fully repo-available, so reproducing it needs no
        snippet — the item the `Candidate:` test used to invent.
        """
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("docker.io"),
                "apt-cache policy": CommandResult(0, _POLICY_PINNED_NO_CANDIDATE, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == set()

    @pytest.mark.asyncio
    async def test_package_installed_from_a_repo_as_an_automatic_dependency_is_not_unreproducible(self) -> None:
        """G4 — the installed version comes from an ESM origin, so a repository supplies it."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("7zip"),
                "apt-cache policy": CommandResult(0, _POLICY_AUTO_DEP, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == set()

    @pytest.mark.asyncio
    async def test_a_hand_deb_apt_marks_automatic_is_still_detected(self) -> None:
        """G5 — `code` came from a `.deb` and apt has it marked automatically installed, so
        it is outside `apt-mark showmanual`. The boundary the article draws is "no configured
        repository supplies the installed version", which this still is: `apt_sync` will not
        touch it either, so nothing else in the run would ever name it.
        """
        context, source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("code"),
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            }
        )

        plan = await ManualInstallsSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:code"}
        # The manual/automatic mark is not consulted at all — asking for it and then
        # ignoring it would leave the boundary looking like a filter that happens to pass.
        assert not any("apt-mark" in cmd for cmd in all_calls(source))

    @pytest.mark.asyncio
    async def test_a_version_newer_than_any_repository_offers_is_unreproducible(self) -> None:
        """G6 — the installed version's own row names no repository while an older row
        does: replicating THIS machine's version needs the `.deb`, so the item is
        presented rather than left to apt."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("mytool"),
                "apt-cache policy": CommandResult(0, _POLICY_NEWER_THAN_REPO, ""),
            }
        )

        plan = await ManualInstallsSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:mytool"}

    @pytest.mark.asyncio
    async def test_one_batched_scan_separates_the_hand_deb_from_the_repo_installed(self) -> None:
        """G7 — the whole manual set goes through a SINGLE `apt-cache policy` (never one
        call per package), and only the hand-installed `.deb` comes back unreproducible."""
        policy = _POLICY_HAND_DEB + _POLICY_REPO_INSTALLED + _POLICY_PINNED_NO_CANDIDATE + _POLICY_AUTO_DEP
        context, source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("code", "gh", "docker.io", "7zip"),
                "apt-cache policy": CommandResult(0, policy, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:code"}
        policy_calls = [cmd for cmd in all_calls(source) if "apt-cache policy" in cmd]
        assert len(policy_calls) == 1
        for name in ("code", "gh", "docker.io", "7zip"):
            assert name in policy_calls[0]

    @pytest.mark.asyncio
    async def test_no_block_inside_an_answered_policy_read_indicts_nothing(self) -> None:
        """G8 — no block for a queried name is silence, not evidence. Indicting on absence would
        declare a machine's whole manual set unreproducible, and hand `apt_sync`'s exclusion
        the same verdict. The probe ANSWERED here — exit 0, and a block for the other name —
        so nothing but `gh`'s missing block can decide this."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("code", "gh"),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            }
        )

        plan = await ManualInstallsSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:code"}

    @pytest.mark.asyncio
    async def test_a_policy_read_that_did_not_answer_fails_the_job(self) -> None:
        """G9, J81 — ADR-022: the detection probe exits non-zero, so it reported nothing about any
        package. Reading that as "no unreproducible packages here" silently drops findings
        that `apt_sync` has meanwhile excluded from its own manifest off the same predicate.
        """
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("code", "gh"),
                "apt-cache policy": CommandResult(100, "", "E: could not read the package lists\n"),
            }
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await ManualInstallsSyncJob(context).plan()

        assert "apt-cache policy code gh" in str(excinfo.value)
        assert "could not read the package lists" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_policy_read_that_printed_no_block_at_all_fails_the_job(self) -> None:
        """G10, J82 — the `blocks` half of ADR-022 D-04, which `apt_sync._source_policy` puts on the
        BYTE-IDENTICAL command — same names, same host, same probe. apt prints one block per
        name it knows and every name here is installed on this machine, so zero blocks at
        exit 0 is apt not answering. The two jobs disagreeing about that
        silence is the divergence this scan's guard exists to prevent: `apt_sync` would drop
        the same bare-`.deb` packages from its manifest while this job reports none.
        """
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("code", "gh"),
                "apt-cache policy": CommandResult(0, "", ""),
            }
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await ManualInstallsSyncJob(context).plan()

        assert "apt-cache policy code gh" in str(excinfo.value)
        assert "printed no package block" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_policy_read_over_only_bare_deb_packages_still_answers(self) -> None:
        """G11 — the limit of the rule above, and the reason the count is of BLOCKS rather than of
        packages with an origin: a machine whose whole manual set was hand-installed from
        `.deb` files gets one origin-less block per name, which is apt answering.
        """
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("code"),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            }
        )

        plan = await ManualInstallsSyncJob(context).plan()

        assert self._unreproducible_ids(plan) == {"unreproducible:apt-no-candidate:code"}

    @pytest.mark.asyncio
    async def test_an_installed_set_read_that_did_not_answer_fails_the_job(self) -> None:
        """G12, J83 — the other end of the same detection: the `dpkg-query` naming the source's
        installed packages exits non-zero, so the run knows nothing about them. The policy
        probe below it is left answering normally, so only that read can fail this."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: CommandResult(100, "", "E: Problem opening /var/lib/dpkg/status\n"),
                "apt-cache policy": CommandResult(0, _POLICY_HAND_DEB, ""),
            }
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await ManualInstallsSyncJob(context).plan()

        assert "dpkg-query" in str(excinfo.value)
        assert "exited 100" in str(excinfo.value)

    def test_only_the_installed_version_row_contributes_origins(self) -> None:
        """`gh`'s older version rows name three Ubuntu URIs that merely OFFER the package.
        Only the `***` row's origin is where the installed version actually came from."""
        assert installed_origins_by_package(_POLICY_REPO_INSTALLED)["gh"] == frozenset(
            {"https://cli.github.com/packages"}
        )


class TestUnownedScan:
    """Unowned-install scan (moved from test_package_state.py when D-18 moved ownership)."""

    @pytest.mark.asyncio
    async def test_scan_unowned_installs_yields_two_items_from_four_candidates(self) -> None:
        """G13 — of four entries under `/usr/local` and `/opt`, only the two no package owns
        are presented, each named by its path."""
        context, _source, _target = make_context(
            source_responses={
                "for root in": CommandResult(
                    0,
                    "/usr/local/flux\n/usr/local/bin/talosctl\n/usr/local/bin/kubectl-cnpg\n/opt/az\n",
                    "",
                ),
                "dpkg --search": CommandResult(
                    0, f"cnpg: /usr/local/bin/kubectl-cnpg\nazure-cli: /opt/az\n{DPKG_WITNESS_LINE}", ""
                ),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        assert {item.identifier for item in items} == {"/usr/local/flux", "/usr/local/bin/talosctl"}
        assert all(item.origin == "unowned-path" for item in items)
        assert all(isinstance(item, UnreproducibleItem) for item in items)

    @pytest.mark.asyncio
    async def test_unowned_scan_queries_only_usr_local_and_opt(self) -> None:
        """G14 — the scan names top-level findings in ONE command over exactly four roots,
        one level deep each; it never walks the tree below them."""
        context, source, _target = make_context()
        job = ManualInstallsSyncJob(context)

        await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        find_calls = [c.args[0] for c in source.run_command.call_args_list if "find " in c.args[0]]
        assert len(find_calls) == 1
        assert find_calls[0] == (
            'for root in /usr/local /opt /usr/local/bin /usr/local/lib; do [ -d "$root" ] || continue; '
            'find "$root" -mindepth 1 -maxdepth 1 || exit 1; done'
        )
        assert "\n" not in find_calls[0], "a multi-line command is mangled in the trace and the confirm gate"

    @pytest.mark.asyncio
    async def test_a_find_that_could_not_run_fails_the_job_rather_than_reporting_nothing(self) -> None:
        """G16, J84 — `PKG-FR-READ-FAILS-JOB`: an unreadable scan root, a missing binary, a shell that
        could not start — none of them mean this machine installed nothing by hand.
        """
        context, _source, _target = make_context(
            source_responses={"for root in": CommandResult(1, "", "find: '/opt': Permission denied")}
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(ProbeFailed) as excinfo:
            await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        assert "Permission denied" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_scan_root_that_is_not_there_is_skipped_not_an_error(self) -> None:
        """G15, J90 — the loop tests each root before listing it, so the one tolerated failure never
        reaches the exit code — which is what lets the guard above trust that exit code.
        """
        context, source, _target = make_context(
            source_responses={
                "for root in": CommandResult(0, "/opt/az\n", ""),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        assert [item.identifier for item in items] == ["/opt/az"]
        assert '[ -d "$root" ] || continue' in all_calls(source)[0]

    @pytest.mark.asyncio
    async def test_a_dpkg_that_did_not_answer_does_not_make_every_path_unowned(self) -> None:
        """G18, J85 — a dead `dpkg --search` prints nothing and exits 1 — the same shape as a batch
        where every path is genuinely unowned. Without the witness, every entry under
        `/opt` and `/usr/local` would become an item demanding an install snippet.
        """
        context, _source, _target = make_context(
            source_responses={
                "for root in": CommandResult(0, "/usr/local/flux\n/opt/az\n", ""),
                "dpkg --search": CommandResult(1, "", "dpkg-query: error: unable to open files list file"),
            }
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(ProbeFailed) as excinfo:
            await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        assert "/usr/bin/dpkg" in str(excinfo.value)
        assert "unable to open files list file" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_batch_where_every_path_is_unowned_is_an_ordinary_answer(self) -> None:
        """G19, J92 — the legitimate exit-1 case dpkg cannot distinguish by exit code: the witness is
        answered, so every other path really is unowned.
        """
        context, _source, _target = make_context(
            source_responses={
                "for root in": CommandResult(0, "/usr/local/flux\n/opt/az\n", ""),
                "dpkg --search": CommandResult(
                    1, DPKG_WITNESS_LINE, "dpkg-query: no path found matching pattern /opt/az"
                ),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        assert [item.identifier for item in items] == ["/opt/az", "/usr/local/flux"]

    @pytest.mark.asyncio
    async def test_the_witness_is_never_reported_as_a_finding(self) -> None:
        """G20 — the path handed to `dpkg --search` to prove it answered is filtered out of
        the candidates and never reported."""
        context, _source, _target = make_context(
            source_responses={
                "for root in": CommandResult(0, "/opt/az\n", ""),
                "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await job._scan_unowned_installs()  # pyright: ignore[reportPrivateUsage]

        assert [item.identifier for item in items] == ["/opt/az"]


class TestSnippetResolution:
    """A registry snippet makes an item reproducible: INSTALL + replay; without one it is
    REPORT_ONLY and carved into its own resolution group (D-20/D-21)."""

    @pytest.mark.asyncio
    async def test_item_with_snippet_plans_install_and_converges_by_replaying_it(self) -> None:
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                # plan() now classifies from the SOURCE registry (corrected D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                # converge/replay still reads the target's copy, placed there by the push.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "brscan3 installed\n", ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        diff = next(d for d in plan.diffs if d.item_id == item_id)
        assert diff.action == DiffAction.INSTALL

        result = await job.converge(diff)

        assert result.success
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert "dpkg --install /tmp/brscan3.deb" in replay_calls[0]

    @pytest.mark.asyncio
    async def test_item_without_snippet_is_report_only_and_grouped_separately(self) -> None:
        """G29 — an item the source holds no snippet for appears in its own resolution
        question and in no other list."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        item_id = "unreproducible:apt-no-candidate:brscan3"
        diff = next(d for d in plan.diffs if d.item_id == item_id)
        assert diff.action == DiffAction.REPORT_ONLY

        resolution_group = next(g for g in plan.groups if g.action == UNREPRODUCIBLE_REVIEW_ACTION)
        assert {e.item_id for e in resolution_group.entries} == {item_id}
        for group in plan.groups:
            if group.action != UNREPRODUCIBLE_REVIEW_ACTION:
                assert item_id not in {e.item_id for e in group.entries}

    @pytest.mark.asyncio
    async def test_missing_snippet_at_converge_is_a_failed_result_not_a_crash(self) -> None:
        """G85 — a snippet-backed diff whose snippet vanished between plan and converge (a
        registry race) fails as one item (D-27), never raises."""
        context, _source, _target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)
        diff = job_diff("unreproducible:apt-no-candidate:gone", DiffAction.INSTALL)

        result = await job.converge(diff)

        assert result.success is False


class TestPromptingSnippetCannotHang:
    """A snippet that would need stdin must FAIL rather than hang the sync. The
    mechanism is the replay command's shape — the body passed as ONE quoted argument to
    `bash -c`, `login_shell=False`, and no stdin supplied under any name — so a command
    that waits for input reads EOF and exits non-zero, becoming an ordinary per-item
    failure (D-27). Asserted on the command shape; nothing here actually blocks.
    """

    @pytest.mark.asyncio
    async def test_replay_supplies_no_stdin_and_a_prompting_snippet_is_a_plain_item_failure(self) -> None:
        """G58 — a snippet whose command asks a question fails as its own item rather than
        hanging the sync: nothing is ever fed to its input."""
        item_id = "unreproducible:apt-no-candidate:brother-driver"
        body = "apt-get install brother-driver"  # a debconf prompt with nothing behind it
        registry_yaml = (
            "snippets:\n"
            f"  {item_id}:\n"
            "    label: brother-driver (no apt candidate)\n"
            f"    body: {body}\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                f"bash -c {shlex.quote(body)}": CommandResult(1, "", "debconf: EOF on stdin at conffile prompt"),
            }
        )
        job = ManualInstallsSyncJob(context)

        result = await job.converge(job_diff(item_id, DiffAction.INSTALL))

        assert result.success is False
        replay_calls = [c for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert replay_calls[0].args[0] == f"bash -c {shlex.quote(body)}"
        assert replay_calls[0].kwargs["login_shell"] is False
        # No stdin reaches the command under any name the executor could accept.
        assert not {"stdin", "input", "input_data"} & set(replay_calls[0].kwargs)


class TestInstallOnly:
    """G24: `manual_installs_sync` is install-only. Unreproducible items describe what the
    SOURCE has installed; there is no target-side manifest to be "extra" against, so no
    input can make this job propose a removal.
    """

    @pytest.mark.asyncio
    async def test_target_query_is_empty_by_design(self) -> None:
        """G89 — the target is never asked what unreproducible software it holds."""
        context, _source, _target = make_context(
            target_responses={_STATUS_QUERY: installed_on_source("target-only-tool")}
        )
        job = ManualInstallsSyncJob(context)

        assert await job.query_target_items() == []

    @pytest.mark.asyncio
    async def test_no_removal_diff_or_group_even_when_the_target_holds_items(self) -> None:
        """G22, G88 — the target is stocked with everything the source has plus its own extras — the
        shape that produces `EXTRA_ON_TARGET`/REMOVE in every other manager — and still no
        removal is proposed, nor is the target ever asked for a manifest.
        """
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                "for root in": CommandResult(0, "/usr/local/flux\n", ""),
                "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
            },
            target_responses={
                _STATUS_QUERY: installed_on_source("brscan3", "target-only-tool"),
                "for root in": CommandResult(0, "/usr/local/flux\n/usr/local/target-only\n", ""),
                "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert plan.diffs  # the source-side findings are present...
        assert all(diff.action != DiffAction.REMOVE for diff in plan.diffs)
        assert all(group.action != DiffAction.REMOVE.value for group in plan.groups)
        # ...and no target-side detection ran at all, so nothing target-only can surface.
        assert not [cmd for cmd in all_calls(target) if "showmanual" in cmd or "find " in cmd]


class TestInertFiltering:
    """An item recorded machine-specific on the source produces no diff (D-08/D-19)."""

    @pytest.mark.asyncio
    async def test_machine_specific_item_is_filtered_before_becoming_a_diff(self) -> None:
        """G37 — a mark from an earlier run for a still-present finding keeps it out of every
        list."""
        decisions_yaml = (
            "machine_specific:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    item_class: unreproducible\n"
            "    label: brscan3 (no apt candidate)\n"
            "    reason: null\n"
            "    recorded_at: '2026-01-01T00:00:00+00:00'\n"
        )
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/manual.decisions.yaml": CommandResult(0, decisions_yaml, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_mark_on_the_target_does_not_silence_a_source_held_finding(self) -> None:
        """G45 — only the SOURCE's marks silence a source-held finding: the same recorded
        item on the target leaves the finding presented, and the target's decision file is
        never even read."""
        decisions_yaml = (
            "machine_specific:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    item_class: unreproducible\n"
            "    label: brscan3 (no apt candidate)\n"
            "    reason: null\n"
            "    recorded_at: '2026-01-01T00:00:00+00:00'\n"
        )
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
            },
            target_responses={"cat ~/.config/pc-switcher/manual.decisions.yaml": CommandResult(0, decisions_yaml, "")},
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == ["unreproducible:apt-no-candidate:brscan3"]
        assert not [cmd for cmd in all_calls(target) if "manual.decisions.yaml" in cmd]


class TestPermanentMarkWrites:
    """`_finalize_unreproducible`'s write side (D-08a/D-21): which machine records a
    resolved unreproducible item, and which resolutions record nothing at all."""

    @staticmethod
    def _brscan3_context(*, dry_run: bool = False) -> tuple[JobContext, MagicMock, MagicMock]:
        return make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
            },
            dry_run=dry_run,
        )

    @pytest.mark.asyncio
    async def test_never_install_it_records_the_mark_on_the_source_naming_the_item(self) -> None:
        """G36 — "never install it on Nomad" writes the mark through Atlas's executor, the
        machine that holds the software, never Nomad's; the entry carries the item's own id
        and its label."""
        context, source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        job.accept_review(plan, ReviewOutcome(decisions={item_id: Decision.SKIP_ALWAYS}, was_interactive=True))
        await job.apply()

        writes = decision_file_writes(source)
        assert len(writes) == 1
        assert item_id in writes[0]
        assert "brscan3 (installed from no configured repository)" in writes[0]
        assert decision_file_writes(target) == []

    @pytest.mark.asyncio
    async def test_not_for_now_records_nothing_on_either_machine(self) -> None:
        """G35 — skipping for this run is a resolution that leaves no trace, so the next
        sync asks about the finding again."""
        context, source, target = self._brscan3_context()
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        job.accept_review(plan, ReviewOutcome(decisions={item_id: Decision.SKIP_ONCE}, was_interactive=True))
        await job.apply()

        assert decision_file_writes(source) == []
        assert decision_file_writes(target) == []

    @pytest.mark.asyncio
    async def test_a_rehearsal_records_no_permanent_mark(self) -> None:
        """G55 — ADR-014: the same answer under `--dry-run` writes nothing on Atlas."""
        context, source, target = self._brscan3_context(dry_run=True)
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        job.accept_review(plan, ReviewOutcome(decisions={item_id: Decision.SKIP_ALWAYS}, was_interactive=True))
        await job.apply()

        assert decision_file_writes(source) == []
        assert decision_file_writes(target) == []


class TestEmptyDetection:
    @pytest.mark.asyncio
    async def test_empty_detection_produces_no_group_and_applies_nothing(self) -> None:
        """G17 — backstop (must_haves): an empty unreproducible set yields no review group and
        nothing to apply."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("gh"),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert plan.groups == ()

        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True))
        await job.apply()  # must not raise


class TestExecuteIndependentOfApt:
    """The job runs on its own enable flag, independent of apt_sync (D-15/D-18)."""

    @pytest.mark.asyncio
    async def test_plan_runs_with_apt_absent_from_config_and_manual_enabled(self) -> None:
        """G26 — the hand-`.deb` finding is detected with apt sync absent from the
        configuration: this job asks apt and dpkg its own questions."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
            },
            enabled_sync_jobs={"manual_installs_sync": True, "folder_sync": True},
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert [d.item_id for d in plan.diffs] == ["unreproducible:apt-no-candidate:brscan3"]

    @pytest.mark.asyncio
    async def test_execute_runs_plan_review_apply_through_injected_reviewer(self) -> None:
        item_id = "unreproducible:apt-no-candidate:brscan3"
        reviewer = FakeReviewer(decisions={item_id: Decision.APPLY})
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                # plan() classifies INSTALL from the SOURCE registry (corrected D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        await job.execute()

        assert reviewer.groups_seen is not None
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1


class TestTracerEndToEnd:
    """The tracer's single path: detect one no-candidate item and one unowned item, plan,
    assert the review groups, then converge the snippet-backed item against the target."""

    @pytest.mark.asyncio
    async def test_detect_plan_and_replay_end_to_end(self) -> None:
        """G30 — an item the source holds a snippet for appears as an ordinary install
        alongside the rest, and converges by replaying it."""
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                "for root in": CommandResult(0, "/usr/local/flux\n/opt/az\n", ""),
                "dpkg --search": CommandResult(0, f"azure-cli: /opt/az\n{DPKG_WITNESS_LINE}", ""),
                # Source registry holds only brscan3 -> it plans INSTALL, flux plans REPORT_ONLY.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "brscan3 installed\n", ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        by_id = {d.item_id: d for d in plan.diffs}
        # brscan3 has a snippet -> INSTALL (resolved); the unowned flux path has none -> REPORT_ONLY.
        assert by_id["unreproducible:apt-no-candidate:brscan3"].action == DiffAction.INSTALL
        assert by_id["unreproducible:unowned-path:/usr/local/flux"].action == DiffAction.REPORT_ONLY

        install_group = next(g for g in plan.groups if g.action == DiffAction.INSTALL.value)
        assert "unreproducible:apt-no-candidate:brscan3" in {e.item_id for e in install_group.entries}
        resolution_group = next(g for g in plan.groups if g.action == UNREPRODUCIBLE_REVIEW_ACTION)
        assert {e.item_id for e in resolution_group.entries} == {"unreproducible:unowned-path:/usr/local/flux"}

        result = await job.converge(by_id["unreproducible:apt-no-candidate:brscan3"])
        assert result.success
        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert "/tmp/brscan3.deb" in replay_calls[0]


class TestSameRunApplication:
    """Corrected D-23: a snippet authored on the fly during review is APPLIED (replayed) on
    the target the SAME run, not one run too late. An item REPORT_ONLY at plan time (no
    source snippet) whose id the review returns in `outcome.snippets` is promoted to an
    INSTALL diff decided APPLY by `after_review()`, so the unchanged base `apply()`
    converges it this run — driven end to end through `execute()`, never by forcing private
    state."""

    @pytest.mark.asyncio
    async def test_on_the_fly_snippet_is_replayed_the_same_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G51 — a finding with no snippet at the start of the run, resolved by one written
        during the review, is installed on the target that same run."""
        # Point Path.home at an empty dir so no on-disk source registry exists: the push
        # early-returns (its overwrite guard never runs) and the replay reads the seeded
        # target registry below, which stands in for what the push would have delivered.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:falco-app"
        body = "sudo dpkg --install /tmp/falco.deb"
        # Post-push target registry: the mocked send_file transports nothing, so seed the
        # snippet the replay reads directly on the target (simulates after_review's push).
        target_registry_yaml = (
            "snippets:\n"
            f"  {item_id}:\n"
            "    label: falco-app (no apt candidate)\n"
            f"    body: {body}\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        reviewer = FakeReviewer(snippets={item_id: body})
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("falco-app"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("falco-app"), ""),
                # Empty source registry -> plan classifies REPORT_ONLY (no source snippet).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, target_registry_yaml, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                f"bash -c '{body}'": CommandResult(0, "falco installed\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        # execute() must not raise: the promoted item converges successfully this run.
        await job.execute()

        replay_calls = [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        assert len(replay_calls) == 1
        assert body in replay_calls[0]


class TestClassificationAuthority:
    """Corrected D-23: reproducibility is judged from the SOURCE registry, never the
    target. A snippet only on the target does NOT make an item reproducible; the same
    snippet on the source does. Direct pin of the one-run-too-late bug's root cause."""

    @pytest.mark.asyncio
    async def test_target_only_snippet_stays_report_only(self) -> None:
        """G43 — a snippet only the target holds leaves the item unresolved: the user is
        still asked to resolve it."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                # Source registry empty -> no source snippet.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            target_responses={
                # Present only on the target: must NOT make the item reproducible.
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "unreproducible:apt-no-candidate:brscan3")
        assert diff.action == DiffAction.REPORT_ONLY

    @pytest.mark.asyncio
    async def test_source_snippet_classifies_install(self) -> None:
        """G44 — a snippet the source holds resolves the item: it is presented as an install."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "unreproducible:apt-no-candidate:brscan3")
        assert diff.action == DiffAction.INSTALL

    @pytest.mark.asyncio
    async def test_dry_run_previews_on_the_fly_install_without_replay_or_write(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G53, J50, J56 — ADR-014: under dry-run an on-the-fly-authored item is promoted and previewed as
        an install (`apply()`'s dry-run branch reports 1 change to apply), yet NO `bash -c`
        replay reaches the target and NO source registry write (`mv --force` of
        `package-snippets.yaml`) runs — a rehearsal leaves no trace and touches nothing."""
        item_id = "unreproducible:apt-no-candidate:falco-app"
        body = "sudo dpkg --install /tmp/falco.deb"
        reviewer = FakeReviewer(snippets={item_id: body})
        context, source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("falco-app"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("falco-app"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            dry_run=True,
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        with caplog.at_level(logging.INFO):
            await job.execute()  # must not raise

        # Promoted: previewed as an install rather than reported as no-change.
        assert "Applying 1 manual change(s)" in caplog.text
        # No replay reached the target and no source registry write happened.
        assert not [c.args[0] for c in target.run_command.call_args_list if c.args[0].startswith("bash -c")]
        source_writes = [
            c.args[0]
            for c in source.run_command.call_args_list
            if "package-snippets" in c.args[0] and "mv --force" in c.args[0]
        ]
        assert not source_writes

    @pytest.mark.asyncio
    async def test_dry_run_previews_a_pre_existing_snippet_install_naming_the_item(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """G54 — a rehearsal of an item the source ALREADY holds a snippet for previews the
        install by name and issues no command on the target."""
        item_id = "unreproducible:apt-no-candidate:brscan3"
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            dry_run=True,
            reviewer=FakeReviewer(decisions={item_id: Decision.APPLY}),
        )
        job = ManualInstallsSyncJob(context)

        with caplog.at_level(logging.DEBUG):
            await job.execute()

        assert "Would install brscan3 (installed from no configured repository)" in caplog.text
        assert not [cmd for cmd in all_calls(target) if cmd.startswith("bash -c")]


class TestNoTerminalRun:
    """`PKG-FR-NO-TERMINAL` for this job's own `execute()`: a run with nobody to answer
    reports skipped before it touches the target."""

    @pytest.mark.asyncio
    async def test_a_run_with_no_terminal_and_findings_skips_before_touching_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G47 — with findings to resolve and no terminal, the job is reported skipped
        rather than applied: `after_review()` never runs, so no registry is transferred, and
        no snippet is replayed."""
        registry = tmp_path / SNIPPET_REGISTRY_RELPATH
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(BRSCAN3_REGISTRY_YAML)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
            },
            target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")},
            reviewer=FakeReviewer(was_interactive=False),
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(JobSkipped):
            await job.execute()

        target.send_file.assert_not_called()
        assert not [cmd for cmd in all_calls(target) if cmd.startswith("bash -c")]


class TestFirstSyncScope:
    def test_the_announced_scope_names_snippet_replay_as_what_it_does_to_the_target(self) -> None:
        """G92 — ADR-015's first-sync announcement names this job and the mechanism it uses
        on the target."""
        scope = ManualInstallsSyncJob.describe_first_sync_scope({})

        assert scope is not None
        assert scope.job_name == "manual_installs_sync"
        assert any("snippet" in item for item in scope.scope_items)
        assert "replay install snippet" in scope.mechanism


class TestSkipOnceResolution:
    """D-21: skip-once is a valid resolution — a run whose only items were skipped-once is
    clean. Decision 10: an interactive review can no longer leave an item genuinely
    undecided, so `unresolved` never fails an interactive run."""

    @pytest.mark.asyncio
    async def test_run_whose_only_items_were_skipped_once_passes(self) -> None:
        """G34, J7 — a run whose only findings were all answered "not for now" ends clean."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        # Explicit skip-once: a resolution, NOT in unresolved (D-21).
        job.accept_review(
            plan,
            ReviewOutcome(decisions={item_id: Decision.SKIP_ONCE}, was_interactive=True, unresolved=()),
        )

        await job.apply()  # must not raise

    @pytest.mark.asyncio
    async def test_interactive_unresolved_no_longer_fails_the_run(self) -> None:
        """G48 — decision 10: the `_unresolved_as_failures` override is gone — an interactive
        outcome carrying an unresolved id (now unreachable through the real review) applies
        cleanly rather than failing the job."""
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        item_id = "unreproducible:apt-no-candidate:brscan3"
        job.accept_review(
            plan,
            ReviewOutcome(decisions={item_id: Decision.SKIP_ONCE}, was_interactive=True, unresolved=(item_id,)),
        )

        await job.apply()  # must not raise


class TestContinueOnFailure:
    @pytest.mark.asyncio
    async def test_failed_snippet_replay_is_a_per_item_failure_and_does_not_stop_the_job(self) -> None:
        """G86 — one of two approved snippets exits non-zero: the other still runs, and
        only the failing item is reported failed."""
        registry_yaml = (
            "snippets:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    label: brscan3 (no apt candidate)\n"
            "    body: sudo dpkg --install /tmp/brscan3.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
            "  unreproducible:apt-no-candidate:cnpg:\n"
            "    label: cnpg (no apt candidate)\n"
            "    body: sudo dpkg --install /tmp/cnpg.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        context, _source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3", "cnpg"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3") + _hand_deb_policy("cnpg"), ""),
                # plan() classifies both INSTALL from the SOURCE registry (corrected D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
                "bash -c 'sudo dpkg --install /tmp/cnpg.deb'": CommandResult(1, "", "dpkg: error processing archive"),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        decisions = {
            "unreproducible:apt-no-candidate:brscan3": Decision.APPLY,
            "unreproducible:apt-no-candidate:cnpg": Decision.APPLY,
        }
        job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        failed_ids = {diff.item_id for diff, _stderr in exc_info.value.failures}
        assert failed_ids == {"unreproducible:apt-no-candidate:cnpg"}

    @pytest.mark.asyncio
    async def test_a_snippet_denied_administrative_rights_fails_like_any_other_item(self) -> None:
        """G87 — a snippet needing administrative rights it does not have on the target is not a
        special case: sudo's refusal is an ordinary non-zero replay, reported against its own
        item with what the machine said. Nothing establishes the right beforehand — what a
        snippet's body needs is unknowable, so there is nothing to pre-check.
        """
        registry_yaml = (
            "snippets:\n"
            "  unreproducible:apt-no-candidate:brscan3:\n"
            "    label: brscan3 (no apt candidate)\n"
            "    body: sudo dpkg --install /tmp/brscan3.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        denial = "sudo: a terminal is required to read the password"
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(1, "", denial),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()
        job.accept_review(
            plan,
            ReviewOutcome(decisions={"unreproducible:apt-no-candidate:brscan3": Decision.APPLY}, was_interactive=True),
        )

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.apply()

        assert [(diff.item_id, stderr) for diff, stderr in exc_info.value.failures] == [
            ("unreproducible:apt-no-candidate:brscan3", denial)
        ]
        assert not any("sudo --non-interactive" in cmd or "sudo -n " in cmd for cmd in all_calls(target))


class TestValidate:
    @pytest.mark.asyncio
    async def test_apt_cache_unavailable_on_source_yields_validation_error(self) -> None:
        """G23, K63 — validation fails before anything runs, naming the source and the missing tool."""
        context, _source, _target = make_context(
            source_responses={"apt-cache --version": CommandResult(127, "", "not found")}
        )
        job = ManualInstallsSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "apt-cache" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_dpkg_unavailable_on_source_yields_validation_error(self) -> None:
        """G24, K64 — validation fails before anything runs, naming the source and the missing tool."""
        context, _source, _target = make_context(
            source_responses={"dpkg --version": CommandResult(127, "", "not found")}
        )
        job = ManualInstallsSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "dpkg" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors(self) -> None:
        """G25, K50, K51, K62 — with both tools present nothing fails, and no administrative-rights
        precondition is imposed on either machine: a snippet's own needs are unknowable,
        so there is nothing to probe for and passing is not conditional on sudo."""
        context, source, target = make_context()
        job = ManualInstallsSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []
        assert not any("sudo --non-interactive true" in cmd for cmd in all_calls(source) + all_calls(target))


class TestSnippetPush:
    """D-23: `manual_installs_sync` pushes `package-snippets.yaml` to the target itself,
    after its own review and before any replay, depending on no other job. The source
    registry lives at `~/.config/pc-switcher/package-snippets.yaml`; the source is the
    local machine, so its on-disk path resolves against `Path.home()`."""

    def _write_source_registry(self, tmp_path: Path, content: str = BRSCAN3_REGISTRY_YAML) -> Path:
        registry = tmp_path / SNIPPET_REGISTRY_RELPATH
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(content)
        return registry

    @pytest.mark.asyncio
    async def test_push_sends_source_registry_under_the_user_home_never_etc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G65, K93 — the target ends the run holding the source's registry under the SSH user's
        own home, never a system directory."""
        source_registry = self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")})
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_called_once()
        local, remote = target.send_file.call_args.args
        assert local == source_registry
        assert remote == "/home/user/.config/pc-switcher/package-snippets.yaml"
        assert "/etc" not in remote

    @pytest.mark.asyncio
    async def test_absent_source_registry_makes_push_a_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G66 — a source that never had a snippet written on it transfers nothing and fails
        nothing."""
        # No registry file exists under tmp_path — a user who has never authored a snippet.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context()
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]  # must not raise

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_run_with_no_terminal_pushes_nothing_even_with_nothing_to_review(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G82, J13, J46 — `PKG-FR-NO-TERMINAL`: a non-interactive run transfers no registry. A scan that
        finds nothing raises no `JobSkipped` — the target already matches, so the job
        succeeds (`PKG-FR-OUTCOME-SUCCESS`) — and the push must still not happen: the
        registry on disk holds entries from earlier runs that nobody approved sending
        tonight.
        """
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("gh"),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
                "for root in": CommandResult(0, "", ""),
            },
            reviewer=FakeReviewer(was_interactive=False),
        )
        job = ManualInstallsSyncJob(context)

        await job.execute()  # no JobSkipped: the plan is empty

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_answered_run_with_nothing_to_review_still_transfers_the_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G83 — an empty review means nothing new to decide, not nothing to carry: a run
        at a terminal that found no finding still delivers the registry's earlier entries.
        """
        source_registry = self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("gh"),
                "apt-cache policy": CommandResult(0, _POLICY_REPO_INSTALLED, ""),
            },
            target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")},
            reviewer=FakeReviewer(was_interactive=True),
        )
        job = ManualInstallsSyncJob(context)

        await job.execute()

        target.send_file.assert_called_once()
        assert target.send_file.call_args.args[0] == source_registry

    @pytest.mark.asyncio
    async def test_a_directory_that_cannot_be_created_fails_naming_the_target_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G84 — the transfer's own plumbing failing is a job failure naming the machine,
        never a half-finished transfer."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            target_responses={
                "mkdir --parents": CommandResult(1, "", "mkdir: cannot create directory: Permission denied"),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(RuntimeError) as excinfo:
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert "target-host" in str(excinfo.value)
        assert "Permission denied" in str(excinfo.value)
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_home_that_cannot_be_resolved_fails_naming_the_target_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G84 — the second plumbing failure: with no home directory there is no absolute
        destination to send to, so nothing is sent."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(target_responses={"echo $HOME": CommandResult(1, "", "no such user")})
        job = ManualInstallsSyncJob(context)

        with pytest.raises(RuntimeError) as excinfo:
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert "target-host" in str(excinfo.value)
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_snippet_written_this_run_is_stamped_exactly_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G52 — `_finalize_unreproducible` runs twice in one run (from `after_review()`
        and again from `apply()`), so the guard that makes the second a no-op is what keeps
        one `authored_at` stamp on the record and the two machines' copies identical: the
        registry is written exactly once.

        Home points at an empty directory, so the push itself is a no-op and the replay
        reads the seeded target registry — what a real push would have delivered.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:falco-app"
        body = "sudo dpkg --install /tmp/falco.deb"
        target_registry_yaml = (
            "snippets:\n"
            f"  {item_id}:\n"
            "    label: falco-app (no apt candidate)\n"
            f"    body: {body}\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        context, source, _target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("falco-app"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("falco-app"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: {}\n", ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, target_registry_yaml, ""),
                f"bash -c '{body}'": CommandResult(0, "falco installed\n", ""),
            },
            reviewer=FakeReviewer(snippets={item_id: body}),
        )
        job = ManualInstallsSyncJob(context)

        await job.execute()

        assert len(registry_writes(source)) == 1

    @pytest.mark.asyncio
    async def test_a_successful_replay_records_nothing_on_the_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G90 — the only file this job writes on Nomad is the registry: two snippets
        replay successfully and Nomad keeps no record of what was installed, so a later run
        has no memory of it."""
        registry_yaml = BRSCAN3_REGISTRY_YAML + (
            "  unreproducible:apt-no-candidate:cnpg:\n"
            "    label: cnpg (no apt candidate)\n"
            "    body: sudo dpkg --install /tmp/cnpg.deb\n"
            "    authored_at: '2026-01-01T00:00:00+00:00'\n"
            "    authored_on: laptop\n"
        )
        self._write_source_registry(tmp_path, registry_yaml)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        decisions = {
            "unreproducible:apt-no-candidate:brscan3": Decision.APPLY,
            "unreproducible:apt-no-candidate:cnpg": Decision.APPLY,
        }
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3", "cnpg"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3") + _hand_deb_policy("cnpg"), ""),
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry_yaml, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
                "bash -c 'sudo dpkg --install /tmp/cnpg.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=FakeReviewer(decisions=decisions),
        )
        job = ManualInstallsSyncJob(context)

        await job.execute()

        assert len([cmd for cmd in all_calls(target) if cmd.startswith("bash -c")]) == 2
        assert not [cmd for cmd in all_calls(target) if "decisions.yaml" in cmd]
        assert decision_file_writes(target) == []
        assert registry_writes(target) == []
        assert [call.args[1] for call in target.send_file.call_args_list] == [
            "/home/user/.config/pc-switcher/package-snippets.yaml"
        ]

    @pytest.mark.asyncio
    async def test_dry_run_pushes_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """G81, J57 — a rehearsal transfers no registry and asks no question."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(dry_run=True)
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_snippet_authored_in_review_is_persisted_before_the_push(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G49 — finalize-then-push: the review's authored snippet is written to the SOURCE
        registry before the file is pushed, so the pushed copy includes it (D-23)."""
        source_registry = self._write_source_registry(tmp_path, "snippets: {}\n")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:brscan3"
        context, source, target = make_context(target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")})
        job = ManualInstallsSyncJob(context)
        diff = job_diff(item_id, DiffAction.REPORT_ONLY)
        plan = PackagePlan(manager="manual", diffs=(diff,), groups=())

        events: list[str] = []
        base_source = source.run_command.side_effect

        def _rec_source(cmd: str, **kw: object) -> CommandResult:
            if "package-snippets" in cmd and "mv --force" in cmd:
                events.append("persist")
            return base_source(cmd, **kw)

        source.run_command = AsyncMock(side_effect=_rec_source)

        async def _rec_send(_local: Path, _remote: str, **_: object) -> None:
            events.append("push")

        target.send_file = AsyncMock(side_effect=_rec_send)

        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={item_id: Decision.SKIP_ONCE},
                was_interactive=True,
                snippets={item_id: "sudo dpkg --install /tmp/brscan3.deb"},
            ),
        )
        await job.after_review()

        assert events == ["persist", "push"]
        assert target.send_file.call_args.args[0] == source_registry

    @pytest.mark.asyncio
    async def test_push_runs_after_review_and_before_replay_in_execute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G50, H11 — end to end: `execute()` pushes the registry, then `apply()` replays the
        snippet-backed item against the target — push strictly before replay."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        item_id = "unreproducible:apt-no-candidate:brscan3"
        reviewer = FakeReviewer(decisions={item_id: Decision.APPLY})
        context, _source, target = make_context(
            source_responses={
                _STATUS_QUERY: installed_on_source("brscan3"),
                "apt-cache policy": CommandResult(0, _hand_deb_policy("brscan3"), ""),
                # plan() classifies INSTALL from the SOURCE registry (corrected D-23).
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
            },
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
                "bash -c 'sudo dpkg --install /tmp/brscan3.deb'": CommandResult(0, "installed\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        events: list[str] = []
        base_run = target.run_command.side_effect

        def _rec_run(cmd: str, **kw: object) -> CommandResult:
            if cmd.startswith("bash -c"):
                events.append("replay")
            return base_run(cmd, **kw)

        target.run_command = AsyncMock(side_effect=_rec_run)

        async def _rec_send(_local: Path, _remote: str, **_: object) -> None:
            events.append("push")

        target.send_file = AsyncMock(side_effect=_rec_send)

        await job.execute()

        assert events == ["push", "replay"]


# A target registry holding brscan3 PLUS an extra entry the source does not have.
TARGET_WITH_EXTRA_YAML = BRSCAN3_REGISTRY_YAML + (
    "  unreproducible:apt-no-candidate:cnpg:\n"
    "    label: cnpg (no apt candidate)\n"
    "    body: sudo dpkg --install /tmp/cnpg.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# Two registries holding the same item with different bodies, each a `curl` of a private
# `.deb` — the documented shape of a snippet whose body carries a credential.
SOURCE_WITH_CREDENTIAL_YAML = BRSCAN3_REGISTRY_YAML + (
    "  unreproducible:apt-no-candidate:acme-agent:\n"
    "    label: acme-agent (no apt candidate)\n"
    "    body: curl --output /tmp/a.deb https://bearer:s0urce-token@dl.example.test/acme-2.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: laptop\n"
)

TARGET_WITH_CREDENTIAL_YAML = BRSCAN3_REGISTRY_YAML + (
    "  unreproducible:apt-no-candidate:acme-agent:\n"
    "    label: acme-agent (no apt candidate)\n"
    "    body: curl --output /tmp/a.deb https://bearer:t4rget-token@dl.example.test/acme-1.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# A target registry holding a bracketed label and body — console markup the question must
# show as written rather than parse.
TARGET_WITH_MARKUP_YAML = (
    "snippets:\n"
    "  unreproducible:unowned-path:/opt/[bold]tool:\n"
    "    label: '[bold]tool (unowned in /opt)'\n"
    "    body: 'sudo /opt/[bold]tool/install.sh --mode=[red]fast'\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# A target registry whose brscan3 body DIFFERS from the source's.
TARGET_CHANGED_BODY_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 (no apt candidate)\n"
    "    body: sudo dpkg --install /tmp/brscan3-OLD.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)

# A target registry whose brscan3 body matches the source's byte for byte and whose
# AUTHORING RECORD does not: same entry, different `authored_at`/`authored_on`.
TARGET_SAME_BODY_OTHER_AUTHORING_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 (no apt candidate)\n"
    "    body: sudo dpkg --install /tmp/brscan3.deb\n"
    "    authored_at: '2025-06-30T09:15:00+00:00'\n"
    "    authored_on: workstation\n"
)

# The same, with the LABEL as the only difference.
TARGET_SAME_BODY_OTHER_LABEL_YAML = (
    "snippets:\n"
    "  unreproducible:apt-no-candidate:brscan3:\n"
    "    label: brscan3 scanner driver\n"
    "    body: sudo dpkg --install /tmp/brscan3.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: laptop\n"
)

# A target registry holding two entries the source lacks AND the source's brscan3 with a
# different body: one question has to name all three.
TARGET_WITH_TWO_LOST_AND_ONE_CHANGED_YAML = TARGET_CHANGED_BODY_YAML + (
    "  unreproducible:apt-no-candidate:cnpg:\n"
    "    label: cnpg (no apt candidate)\n"
    "    body: sudo dpkg --install /tmp/cnpg.deb\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
    "  unreproducible:unowned-path:/opt/az:\n"
    "    label: az (unowned in /opt)\n"
    "    body: sudo /opt/az/install.sh\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: workstation\n"
)


class TestSnippetRegistryOverwriteGuard:
    """Decision 9: the wholesale `package-snippets.yaml` push is guarded. A purely additive
    overwrite (source superset of target) proceeds silently; one that would lose or change a
    target entry needs explicit confirmation, and otherwise aborts the whole run."""

    def _write_source_registry(self, tmp_path: Path, content: str = BRSCAN3_REGISTRY_YAML) -> Path:
        registry = tmp_path / SNIPPET_REGISTRY_RELPATH
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(content)
        return registry

    @pytest.mark.asyncio
    async def test_additive_overwrite_proceeds_without_confirming(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G68 — target is a subset of the source (here empty): additive -> push, no prompt."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=False)  # would abort if ever consulted
        context, _source, target = make_context(
            target_responses={"echo $HOME": CommandResult(0, "/home/user\n", "")},
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_identical_target_entry_is_additive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """G69 — target holds exactly the same brscan3 body the source has: additive -> no prompt."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=False)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, BRSCAN3_REGISTRY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_lost_target_entry_prompts_and_proceeds_on_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G70, H16, N17 — target holds an entry (cnpg) absent from the source: non-additive -> confirm.
        On approval the wholesale push proceeds."""
        self._write_source_registry(tmp_path)  # source has brscan3 only
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        # The prompt names the entry that would be lost, and passes allow=False (no override).
        assert "cnpg" in str(confirmer.calls[0]["message"])
        assert confirmer.calls[0]["allow"] is False
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_lost_target_entry_aborts_on_decline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """G73, G74, H60 — declining the non-additive overwrite aborts the whole run and sends nothing."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=False)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(SyncAbortedByUser):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_body_is_non_additive_and_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G71 — target holds brscan3 with a DIFFERENT body than the source: non-additive."""
        self._write_source_registry(tmp_path)  # source brscan3 body
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_CHANGED_BODY_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        assert "CHANGED" in str(confirmer.calls[0]["message"])
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_different_authoring_record_is_non_additive_and_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G72 — the body is identical but the authoring record is not, so the push still changes
        the entry the target holds: it is named, and the question shows the authoring records
        rather than printing the unchanged body twice."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(
                    0, TARGET_SAME_BODY_OTHER_AUTHORING_YAML, ""
                ),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        message = str(confirmer.calls[0]["message"])
        assert "CHANGED" in message
        assert "brscan3" in message
        assert "2025-06-30T09:15:00+00:00 on workstation" in message
        assert "2026-01-01T00:00:00+00:00 on laptop" in message
        assert "sudo dpkg --install /tmp/brscan3.deb" not in message  # the body did not change
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_different_label_is_non_additive_and_prompts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G72 — the label is part of the entry too, so replacing it is a change the user answers."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(
                    0, TARGET_SAME_BODY_OTHER_LABEL_YAML, ""
                ),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        message = str(confirmer.calls[0]["message"])
        assert "brscan3 scanner driver" in message
        assert "brscan3 (no apt candidate)" in message
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_credential_in_a_snippet_body_is_withheld_from_the_question(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G77, J125, J126 — ADR-021's fifth credential exit: the question displays two whole snippet bodies,
        and a body may legitimately fetch a private `.deb`. Only what is displayed is
        rewritten — the file the push sends keeps its author's bytes
        (`PKG-FR-SNIPPET-VERBATIM`)."""
        source = self._write_source_registry(tmp_path, SOURCE_WITH_CREDENTIAL_YAML)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_CREDENTIAL_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        message = str(confirmer.calls[0]["message"])
        assert "s0urce-token" not in message
        assert "t4rget-token" not in message
        assert "***@dl.example.test/acme-2.deb" in message
        assert "***@dl.example.test/acme-1.deb" in message
        assert "s0urce-token" in source.read_text(encoding="utf-8")
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_one_question_names_every_entry_the_push_would_lose_or_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G93 — two target entries the source lacks and a third whose body differs are put
        in ONE question, each named."""
        self._write_source_registry(tmp_path)  # source has brscan3 only
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(
                    0, TARGET_WITH_TWO_LOST_AND_ONE_CHANGED_YAML, ""
                ),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert len(confirmer.calls) == 1
        message = str(confirmer.calls[0]["message"])
        assert "unreproducible:apt-no-candidate:cnpg" in message
        assert "unreproducible:unowned-path:/opt/az" in message
        assert "unreproducible:apt-no-candidate:brscan3" in message
        assert message.count("LOST") == 2
        assert message.count("CHANGED") == 1
        target.send_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_bracketed_label_and_body_reach_the_question_as_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G78 — square-bracketed text is console markup to Rich, and the confirmer renders
        the message inside a `Panel`. Every snippet field is escaped, so the question shows
        the author's text instead of raising on it."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, _target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_MARKUP_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        message = str(confirmer.calls[0]["message"])
        # Rendered the way the real confirmer renders it: unescaped markup raises here.
        console = Console(file=io.StringIO(), width=200, force_terminal=False, no_color=True)
        console.print(Panel(message))
        rendered = console.file.getvalue()  # pyright: ignore[reportAttributeAccessIssue]
        assert "[bold]tool (unowned in /opt)" in rendered
        assert "--mode=[red]fast" in rendered

    @pytest.mark.asyncio
    async def test_a_corrupt_source_registry_ends_the_run_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G80 — an unparsable source file is not a registry holding nothing: the run ends
        naming the file, nothing is asked, and the corrupt bytes never reach the target."""
        corrupt = "snippets: [\n  - broken\n"
        source_registry = self._write_source_registry(tmp_path, corrupt)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(SyncAbortedByUser, match=re.escape("package-snippets.yaml")):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_not_called()
        assert source_registry.read_text(encoding="utf-8") == corrupt

    @pytest.mark.asyncio
    async def test_a_corrupt_target_registry_ends_the_run_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G79 — the target's file cannot be parsed, so what it holds is unknown: the run
        ends rather than counting the push additive, and nothing is overwritten."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(approve=True)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, "snippets: [\n  - broken\n", ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(SyncAbortedByUser, match=re.escape("package-snippets.yaml")):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_absent_source_registry_leaves_the_targets_entries_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G94 — with no registry on the source there is no transfer, so nothing of the
        target's can be lost or changed and no question is asked: its two entries stay."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no source registry on disk
        confirmer = FakeConfirmer(approve=False)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        assert confirmer.calls == []
        target.send_file.assert_not_called()
        assert registry_writes(target) == []

    @pytest.mark.asyncio
    async def test_non_additive_push_without_a_confirmer_fails_and_sends_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G76 — the requirement is that a non-additive push NEVER silently overwrites; the
        two acceptable outcomes are a confirmed push or a failed run. With no confirmer on
        the context there is nothing to ask, and this pins the actual failure mode: the
        bare `assert self.context.confirmer is not None` in
        `manual_installs_sync._guard_registry_overwrite` (manual_installs_sync.py:305)
        raises `AssertionError` and the run fails.

        A misconfigured injection surfacing as a bare `AssertionError` is a rough message
        for a user, but it IS a loud, transfer-free failure — which is the property that
        matters here. Nothing is sent.
        """
        self._write_source_registry(tmp_path)  # source has brscan3 only
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=None,  # nothing injected
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(AssertionError, match="confirmer"):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_interactive_non_additive_aborts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """G75 — a non-interactive run cannot confirm: the confirmer returns its `allow` (False,
        since no override flag exists), so a non-additive overwrite aborts."""
        self._write_source_registry(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        confirmer = FakeConfirmer(return_allow=True)  # mimic non-interactive: answer == allow
        context, _source, target = make_context(
            target_responses={
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, TARGET_WITH_EXTRA_YAML, ""),
                "echo $HOME": CommandResult(0, "/home/user\n", ""),
            },
            confirmer=confirmer,
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(SyncAbortedByUser):
            await job._push_snippet_registry()  # pyright: ignore[reportPrivateUsage]

        target.send_file.assert_not_called()


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_manual_installs_sync_to_its_job(self) -> None:
        """G91, K38 — named in the configuration, the job resolves to its own class."""
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("manual_installs_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is ManualInstallsSyncJob


class TestUnreproducibleItem:
    def test_reports_its_item_class(self) -> None:
        assert UnreproducibleItem.ITEM_CLASS == ItemClass.UNREPRODUCIBLE

    def test_same_identifier_different_origin_yields_distinct_item_ids(self) -> None:
        """G21 — a package and a path that share a name are two independent items, one per
        kind of finding."""
        no_candidate = UnreproducibleItem(origin="apt-no-candidate", identifier="brscan3", label="brscan3")
        unowned_path = UnreproducibleItem(origin="unowned-path", identifier="brscan3", label="/opt/brscan3")

        assert no_candidate.item_id != unowned_path.item_id

    def test_label_is_a_plain_field(self) -> None:
        item = UnreproducibleItem(origin="unowned-path", identifier="/opt/flux", label="flux (unowned in /opt)")

        assert item.label == "flux (unowned in /opt)"
