"""Fixtures and fakes shared by the unreproducible jobs' unit tests: `manual_deb_sync`,
`manual_installs_sync`, and the shared half both of them inherit
(`packages/unreproducible.py`).

Not a test module — pytest collects nothing here. The `apt-cache policy` fixtures are also
imported by the apt probe/origin tests, which read the same real output through the shared
`packages/apt_policy.py` predicate.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from unittest.mock import AsyncMock, MagicMock

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.review import Decision, ReviewGroup, ReviewOutcome, ReviewPolicy
from pcswitcher.models import CommandResult

# -- Real `apt-cache policy` output, verbatim ------------------------------------------
#
# Measured on a live Ubuntu 24.04 machine. The four shapes the detector has to tell apart:
# a hand-installed `.deb`, a repo-installed package, a package every version of which is
# pinned below zero, and a repo-installed automatic dependency. Only the first is
# unreproducible, and all four report a `Candidate:` — three of them a real version, and
# the pinned one `(none)` despite being fully repo-available.


def hand_deb_policy(name: str, version: str = "1.0") -> str:
    """`POLICY_HAND_DEB`'s shape for an arbitrary package: an installed version whose
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
POLICY_HAND_DEB = hand_deb_policy("code", "1.129.1-1784303641")

# `gh`, installed from its vendor repository — note its block ALSO carries a
# `/var/lib/dpkg/status` line, and its older version rows name three Ubuntu URIs that are
# not where the installed version came from.
POLICY_REPO_INSTALLED = """gh:
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
POLICY_PINNED_NO_CANDIDATE = """docker.io:
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
POLICY_NEWER_THAN_REPO = """mytool:
  Installed: 3.0.0
  Candidate: 3.0.0
  Version table:
 *** 3.0.0 100
        100 /var/lib/dpkg/status
     2.1.0 500
        500 http://ftp.belnet.be/ubuntu noble/universe amd64 Packages
"""

# `7zip`, pulled in from a repository as an automatic dependency.
POLICY_AUTO_DEP = """7zip:
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


# The `dpkg-query` that names a machine's installed set, matched by its one distinctive
# field so a fixture keys on the question rather than on the whole format string. Both
# machines answer it now that a finding the target already holds is not presented
# (`PKG-FR-MANUAL-DIFF`).
STATUS_QUERY = "db:Status-Status"


def installed_on(*names: str) -> CommandResult:
    """What that `dpkg-query` answers on a machine holding exactly `names`."""
    return CommandResult(0, "".join(f"{name}\tinstalled\n" for name in names), "")


# What the target answers about its own installed set unless a test says otherwise. A
# machine with no packages installed does not exist, and reading one as empty is a probe
# failure by design (`_installed_names`), so every context needs an ordinary answer here for
# the target's half of the diff to be reachable at all.
TARGET_HOLDS_NOTHING_OF_INTEREST = ("coreutils",)


def scan_finds(*paths: str) -> CommandResult:
    """What the scan's `find` prints: one `<type letter>\\t<path>` line per entry.

    A path written with a trailing `/` is a directory, anything else a plain file — the
    shorthand keeps a listing readable, and the type is what the `/opt` shape rule and the
    empty-directory rule both turn on.
    """
    return CommandResult(
        0, "".join(f"{'d' if path.endswith('/') else 'f'}\t{path.rstrip('/')}\n" for path in paths), ""
    )


class FakeGate:
    """A `Reviewer` that answers the `/opt` shape question with a preset value and records
    what it was asked. `None` is the answer a run with no terminal gets."""

    def __init__(self, *, answer: bool | None) -> None:
        self._answer = answer
        self.asked: list[dict[str, str]] = []

    async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None:
        self.asked.append(
            {"title": title, "message": message, "proceed_label": proceed_label, "stop_label": stop_label}
        )
        return self._answer

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        raise AssertionError(f"no review was expected; got {len(groups)} group(s)")


def every_directory_holds_a_file(command: str) -> CommandResult:
    """The empty-directory probe's answer on a machine where every directory it was handed
    holds a file somewhere below: it echoes each one back."""
    asked = shlex.split(command.partition("for dir in ")[2].partition(";")[0])
    return CommandResult(0, "".join(f"{path}\n" for path in asked), "")


Answer = CommandResult | Callable[[str], CommandResult]


def respond_to(mapping: dict[str, Answer], default: CommandResult | None = None) -> Callable[..., CommandResult]:
    """Build a run_command side_effect matching by substring (first match wins).

    A mapped value may be a function of the command, for the probes whose answer depends on
    what they were asked — the empty-directory look is handed a different set of directories
    by every test that reaches it.
    """
    fallback = default if default is not None else CommandResult(exit_code=0, stdout="", stderr="")

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result(cmd) if callable(result) else result
        return fallback

    return _side_effect


def make_context(  # noqa: PLR0913 - test builder knobs; all keyword-only
    *,
    source_responses: dict[str, Answer] | None = None,
    target_responses: dict[str, Answer] | None = None,
    dry_run: bool = False,
    reviewer: object | None = None,
    confirmer: object | None = None,
    enabled_sync_jobs: dict[str, bool] | None = None,
    review_policy: ReviewPolicy | None = None,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target = MagicMock()
    target.run_command = AsyncMock(
        side_effect=respond_to(
            {STATUS_QUERY: installed_on(*TARGET_HOLDS_NOTHING_OF_INTEREST), **(target_responses or {})}
        )
    )
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
        review_policy=review_policy,
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


def decision_file_writes(mock: MagicMock, manager: str) -> list[str]:
    """Every command that WRITES `manager`'s machine-local decision file on `mock`'s
    machine — the `mv --force` half of the atomic write, so the file's `cat` read never
    counts as a write."""
    return [
        call.args[0]
        for call in mock.run_command.call_args_list
        if f"{manager}.decisions.yaml" in call.args[0] and "mv --force" in call.args[0]
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
        raise AssertionError(f"this job has no gate question; asked {title!r}")

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
