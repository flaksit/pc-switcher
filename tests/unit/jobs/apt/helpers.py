"""Fixtures, fake executors and response builders shared by the apt_sync test modules.

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import dataclasses
import shlex
from collections.abc import Callable, Sequence
from unittest.mock import AsyncMock, MagicMock

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.packages.items import DiffAction, Machines
from pcswitcher.jobs.packages.review import (
    Decision,
    ReviewGroup,
    ReviewOutcome,
)
from pcswitcher.models import CommandResult
from tests.unit.jobs.test_package_sync_core import FakeReviewer

SHOWMANUAL_3 = "pkg-a\npkg-b\npkg-c\n"
DPKG_QUERY_3 = "pkg-a\t1.0\npkg-b\t2.0\npkg-c\t3.0\n"
_NO_PACKAGES = {"apt-mark showmanual": CommandResult(0, "", "")}


def sha256_line(digest: str, filename: str) -> str:
    """One `sha256sum`-shaped line: `<digest>  <filename>\\n`."""
    return f"{digest}  {filename}\n"


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


_BASELINE_ARCHIVE = "http://ftp.belnet.be/ubuntu"


def respond_to_source(mapping: dict[str, CommandResult]) -> Callable[..., CommandResult]:
    """`respond_to`, plus the two answers every real source machine gives about its own
    packages, for a fixture that does not state them.

    A source `apt-cache policy` that prints nothing is a BROKEN apt, not a machine with
    unusual packages — apt prints one block per installed name it is asked about — and
    `_require_apt_answer` now says so rather than reading the silence as "no package has a
    vendor origin". So the baseline answers with one archive block per queried name, plus
    the `ubuntu.sources` scan line that makes that archive a distribution origin. Any test
    with an opinion about either overrides its key and this never fires.
    """
    inner = respond_to(mapping)

    def _side_effect(cmd: str, **kwargs: object) -> CommandResult:
        if not any(pattern in cmd for pattern in mapping):
            if cmd.startswith("apt-cache policy"):
                names = shlex.split(cmd)[2:]
                return CommandResult(0, "".join(_policy_block(name, _BASELINE_ARCHIVE) for name in names), "")
            if _SOURCE_SCAN_CMD in cmd:
                return CommandResult(0, _scan_line("ubuntu.sources", _UBUNTU_SOURCES_BELNET), "")
        return inner(cmd, **kwargs)

    return _side_effect


def make_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    target_side_effect: Callable[..., CommandResult] | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to_source(source_responses or {}))
    target = MagicMock()
    target.run_command = AsyncMock(side_effect=target_side_effect or respond_to(target_responses or {}))
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
    )
    return context, source, target


MACHINES = Machines(source="source-host", target="target-host")


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


def install_reviewer(job: AptSyncJob, decisions: dict[str, Decision]) -> None:
    """Inject a `FakeReviewer` returning `decisions`, so `execute()` plans, reviews and
    applies through the same self-contained path production uses. Unlisted item ids
    default to `SKIP_ONCE`, matching the review's own default for an unticked entry.
    """
    job.context = dataclasses.replace(job.context, reviewer=FakeReviewer(decisions))


_DEB822_FOO = (
    "Types: deb\nURIs: https://example.com\nSuites: stable\nComponents: main\nSigned-By: /etc/apt/keyrings/foo.gpg\n"
)
_LEGACY_BAR = "deb [signed-by=/etc/apt/keyrings/bar.gpg] https://example.com stable main\n"
_UBUNTU_SOURCES_BELNET = "Types: deb\nURIs: http://ftp.belnet.be/ubuntu\nSuites: noble\nComponents: main\n"
_RIVAL_LIST = "deb https://rival.example.com/apt stable main\n"


def respond_with_policy_sequence(
    mapping: dict[str, CommandResult], policy_results: list[CommandResult]
) -> Callable[..., CommandResult]:
    """Like `respond_to`, but successive `apt-cache policy` calls return successive results
    (the last one repeats).

    The shape the target genuinely has across one run: the plan-time policy read and the
    post-`apt-get update` verification ask the same question of two different `/etc/apt`
    states, and a fixture that answers both identically cannot distinguish a verification
    that re-read the target from one that reused the plan's answer.
    """
    fallback = CommandResult(exit_code=0, stdout="", stderr="")
    state = {"policy_calls": 0}

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if "apt-cache policy" in cmd:
            index = min(state["policy_calls"], len(policy_results) - 1)
            state["policy_calls"] += 1
            return policy_results[index]
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


def real_installs(target: MagicMock) -> list[str]:
    """Every REAL `apt-get install` the target was asked to run — the `--dry-run`
    simulations share the verb and are deliberately excluded."""
    return [cmd for cmd in all_calls(target) if "sudo" in cmd and "apt-get install" in cmd]


def index_of(commands: list[str], predicate: Callable[[str], bool]) -> int:
    return next(i for i, cmd in enumerate(commands) if predicate(cmd))


def _repo_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    target_side_effect: Callable[..., CommandResult] | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, MagicMock]:
    """`make_context`, plus a resolved target `$HOME` (`/home/target-user`) — every
    repository-group write needs it for the staging path.
    """
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to_source(source_responses or {}))
    target = MagicMock()
    if target_side_effect is not None:
        target.run_command = AsyncMock(side_effect=target_side_effect)
    else:
        merged = {"echo $HOME": CommandResult(0, "/home/target-user", ""), **(target_responses or {})}
        target.run_command = AsyncMock(side_effect=respond_to(merged))
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
    )
    return context, source, target


def _policy_candidate(origin: str) -> str:
    """`apt-cache policy pkg-a` on a target that does not have the package but can now
    fetch it from `origin` — the shape the post-`apt-get update` verification reads."""
    return (
        "pkg-a:\n  Installed: (none)\n  Candidate: 1.0\n  Version table:\n"
        f"     1.0 500\n        500 {origin} stable/main amd64 Packages\n"
    )


_POLICY_NO_CANDIDATE = "pkg-a:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n"


def foo_source_responses(**overrides: CommandResult) -> dict[str, CommandResult]:
    """A source machine whose `pkg-a` comes from the repository `foo.sources` declares.

    The only shape that makes a repository travel now (ADR-020 D-37): a source file is
    derived from the packages approved from it, so a test that wants `foo.sources` written
    must give the source a package whose origin `foo.sources` serves. `foo.gpg` is the key
    that file names, present on the source, so the repository is writable.
    """
    return {
        "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
        "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
        "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://example.com"), ""),
        _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
        "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
        "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
        **overrides,
    }


def foo_target_side_effect(
    overrides: dict[str, CommandResult] | None = None, *, origin: str = "https://example.com"
) -> Callable[..., CommandResult]:
    """A target that offers `pkg-a` from nowhere at plan time and from `origin` afterwards.

    Two different answers to one command, which is the run's real shape: the plan-time
    policy read is what derives the repository (no candidate -> the source's origin has to
    be replicated), and the post-`apt-get update` read is what D-35 verifies the install
    against. A fixture answering both the same way could not tell the two apart.
    """
    return respond_with_policy_sequence(
        {
            "echo $HOME": CommandResult(0, "/home/target-user", ""),
            "apt-mark showmanual": CommandResult(0, "", ""),
            "test -f": CommandResult(1, "", ""),
            **(overrides or {}),
        },
        [CommandResult(0, _POLICY_NO_CANDIDATE, ""), CommandResult(0, _policy_candidate(origin), "")],
    )


_APPROVE_PKG_A = {"apt:package:pkg-a": Decision.APPLY}
_SOURCE_SCAN_CMD = "-path /etc/apt/sources.list -o"
_VENDOR_LIST = "deb [signed-by=/etc/apt/keyrings/vendor.gpg] https://vendor.example.com/apt stable main\n"


def decision_file(*item_ids: str) -> str:
    """A decision file recording each id skip-always as an apt package (D-08)."""
    body = "".join(
        f'  "{item_id}":\n'
        "    item_class: apt_package\n"
        f'    label: "{item_id.removeprefix("apt:package:")}"\n'
        "    reason: null\n"
        "    recorded_at: '2026-07-26T00:00:00Z'\n"
        for item_id in item_ids
    )
    return f"machine_specific:\n{body}"


def _policy_block(name: str, origin: str | None) -> str:
    """One `apt-cache policy` package block, installed, with `origin` as the installed
    version's repository — or dpkg's own record alone when `origin` is None (the shape a
    package installed from a local `.deb` has).
    """
    lines = [f"{name}:", "  Installed: 1.0", "  Candidate: 1.0", "  Version table:", " *** 1.0 500"]
    if origin is not None:
        lines.append(f"        500 {origin} stable/main amd64 Packages")
    lines.append("        100 /var/lib/dpkg/status")
    return "\n".join(lines) + "\n"


def target_offers(*names: str, origin: str = _BASELINE_ARCHIVE) -> str:
    """`apt-cache policy` blocks for names the TARGET does not have installed but CAN
    install — a candidate, no `***` row.

    What a target must answer for a package before that package can enter the plan-time
    rehearsal (`_target_resolvable`). A fixture that omits it is describing a target whose
    apt has never heard the name, on which a real `apt-get --dry-run install` exits 100.
    """
    return "".join(
        f"{name}:\n  Installed: (none)\n  Candidate: 1.0\n  Version table:\n"
        f"     1.0 500\n        500 {origin} stable/main amd64 Packages\n"
        for name in names
    )


def _scan_line(filename: str, content: str, *, path: str | None = None) -> str:
    """The `find ... -exec awk` scan's `<path>\\t<line>` output for one source file,
    filtered the way the shipped awk program filters it. `path` overrides the assumed
    `sources.list.d` location, for the `/etc/apt/sources.list` case.
    """
    keep = ("uris:", "signed-by", "deb ", "deb-src ")
    where = path or f"/etc/apt/sources.list.d/{filename}"
    return "".join(
        f"{where}\t{line}\n" for line in content.splitlines() if any(token in line.lower() for token in keep)
    )


_POLICY_FIXTURE_SCAN = (
    _scan_line("github-cli.list", "deb https://cli.github.com/packages stable main\n")
    + _scan_line(
        "ubuntu.sources",
        "Types: deb\nURIs: http://ftp.belnet.be/ubuntu http://security.ubuntu.com/ubuntu\n",
    )
    + _scan_line("ubuntu-esm-apps.sources", "Types: deb\nURIs: https://esm.ubuntu.com/apps/ubuntu\n")
)
_KEY_DEST_PREFIXES = ("/etc/apt/keyrings/", "/etc/apt/trusted.gpg.d/", "/usr/share/keyrings/")


def key_writes(target: MagicMock) -> list[str]:
    """Every key promotion this run issued, by destination path, across all three key
    directories."""
    return [
        c.rsplit(" ", 1)[1]
        for c in all_calls(target)
        if c.startswith("sudo install --owner=root --group=root --mode=0644")
        and c.rsplit(" ", 1)[1].startswith(_KEY_DEST_PREFIXES)
    ]


_PIN_DIGEST_CMD = "find /etc/apt/preferences.d -maxdepth 1 -type f -exec sha256sum"


class CountingReviewer(FakeReviewer):
    """`FakeReviewer` that keeps EVERY call's groups, not just the last one."""

    def __init__(self, decisions: dict[str, Decision]) -> None:
        super().__init__(decisions)
        self.calls: list[tuple[ReviewGroup, ...]] = []

    async def review(self, groups: Sequence[ReviewGroup]) -> ReviewOutcome:
        self.calls.append(tuple(groups))
        return await super().review(groups)


def actionable_entry_ids(groups: Sequence[ReviewGroup]) -> set[str]:
    """Item ids the user was actually offered a converge action for. A `REPORT_ONLY` entry
    is shown but implies no verb, so it is exactly what a suppressed case looks like.
    """
    return {
        entry.item_id for group in groups if group.action != DiffAction.REPORT_ONLY.value for entry in group.entries
    }


_CHANGED_VENDOR = "deb [signed-by=/etc/apt/keyrings/vendor.gpg] https://vendor.example.com/apt noble main\n"


def differing_repo_context(*, recorded: str) -> tuple[JobContext, MagicMock, MagicMock]:
    """`vendor.list` on both machines with different bytes, declaring the origin the
    target's `curl` is installed from. `recorded` is the target's decision file.

    The source's `vendor-tool` comes from that same origin and the target lacks it, which is
    what makes `vendor.list` a file this run would write for an approved package — the gate
    `PKG-FR-REPO-CONFLICT` puts in front of the question.
    """
    return _repo_context(
        source_responses={
            "apt-mark showmanual": CommandResult(0, "vendor-tool\n", ""),
            "dpkg-query": CommandResult(0, "vendor-tool\t1.0\n", ""),
            "apt-cache policy": CommandResult(0, _policy_block("vendor-tool", "https://vendor.example.com/apt"), ""),
            _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _CHANGED_VENDOR), ""),
            "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-new", "vendor.list"), ""),
            "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _CHANGED_VENDOR, ""),
        },
        target_responses={
            "apt-mark showmanual": CommandResult(0, "curl\n", ""),
            "dpkg-query": CommandResult(0, "curl\t8.0\n", ""),
            _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("vendor.list", _VENDOR_LIST), ""),
            "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "vendor.list"), ""),
            "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "vendor.gpg"), ""),
            "cat /etc/apt/sources.list.d/vendor.list": CommandResult(0, _VENDOR_LIST, ""),
            "apt.decisions.yaml": CommandResult(0, recorded, ""),
            "apt-cache policy": CommandResult(0, _policy_block("curl", "https://vendor.example.com/apt"), ""),
        },
    )
