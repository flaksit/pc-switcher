"""Unit tests for `ManualInstallsSyncJob`: the filesystem half of what no package manager
can reproduce (D-18/D-19) — the bounded unowned scan under `/usr/local` and `/opt`, the
`/opt` shape question, this job's own validation, and the marks it keeps about paths.

Hand-installed `.deb` packages are `manual_deb_sync`'s (`test_manual_deb_sync.py`), and
the shared half both jobs inherit is covered in `test_unreproducible_jobs.py`. All executor
interactions are mocked; no real dpkg/apt-cache/sudo commands run.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext
from pcswitcher.jobs.manual_installs_sync import ManualInstallsSyncJob
from pcswitcher.jobs.packages.items import DiffAction
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import ReviewOutcome
from pcswitcher.jobs.packages.sync_core import PackagePlan
from pcswitcher.jobs.packages.unreproducible import UnreproducibleItem
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator
from tests.unit.jobs.unreproducible_harness import (
    DPKG_WITNESS_LINE,
    POLICY_REPO_INSTALLED,
    STATUS_QUERY,
    Answer,
    FakeGate,
    all_calls,
    every_directory_holds_a_file,
    installed_on,
    make_context,
    scan_finds,
)

# A registry entry for the /opt/az unowned path, with both bodies (D-22).
AZ_REGISTRY_YAML = (
    "snippets:\n"
    "  unreproducible:unowned-path:/opt/az:\n"
    "    label: /opt/az\n"
    "    install_body: sudo /opt/az/install.sh\n"
    "    version_body: az --version\n"
    "    authored_at: '2026-01-01T00:00:00+00:00'\n"
    "    authored_on: laptop\n"
)


class TestUnownedScan:
    """The filesystem half of detection: what the scan looks at, what it refuses to call a
    finding, and what it does when a read cannot answer (`PKG-FR-MANUAL-SCOPE`).
    """

    @staticmethod
    async def scan(job: ManualInstallsSyncJob) -> list[UnreproducibleItem]:
        """The source-side scan, where an ambiguous `/opt` shape is the user's to settle."""
        return await job._scan_unowned_installs(  # pyright: ignore[reportPrivateUsage]
            job.source, job.machines.source, ask_when_ambiguous=True
        )

    @pytest.mark.asyncio
    async def test_scan_unowned_installs_yields_two_items_from_four_candidates(self) -> None:
        """G13 — of four entries under the scan's roots, only the two no package owns are
        presented, each named by its path."""
        context, _source, _target = make_context(
            source_responses={
                "for root in": scan_finds(
                    "/usr/local/flux", "/usr/local/bin/talosctl", "/usr/local/bin/kubectl-cnpg", "/opt/az"
                ),
                "dpkg --search": CommandResult(
                    0, f"cnpg: /usr/local/bin/kubectl-cnpg\nazure-cli: /opt/az\n{DPKG_WITNESS_LINE}", ""
                ),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await self.scan(job)

        assert {item.identifier for item in items} == {"/usr/local/flux", "/usr/local/bin/talosctl"}
        assert all(item.origin == "unowned-path" for item in items)
        assert all(isinstance(item, UnreproducibleItem) for item in items)

    @pytest.mark.asyncio
    async def test_the_scan_covers_seven_roots_one_level_deep_in_one_command(self) -> None:
        """G14 — `/opt`, everything directly under `/usr/local`, and the entries of
        `/usr/local`'s `bin`, `sbin`, `lib`, `games` and `src`, one level deep each, in one
        command; the tree below a finding is never walked."""
        context, source, _target = make_context()
        job = ManualInstallsSyncJob(context)

        await self.scan(job)

        find_calls = [c.args[0] for c in source.run_command.call_args_list if "find " in c.args[0]]
        assert len(find_calls) == 1
        assert find_calls[0] == (
            "for root in /opt /usr/local /usr/local/bin /usr/local/sbin /usr/local/lib /usr/local/games "
            '/usr/local/src; do [ -d "$root" ] || continue; '
            "find \"$root\" -mindepth 1 -maxdepth 1 -printf '%y\\t%p\\n' || exit 1; done"
        )
        assert "\n" not in find_calls[0], "a multi-line command is mangled in the trace and the confirm gate"

    @pytest.mark.asyncio
    async def test_four_usr_local_directories_are_never_looked_into(self) -> None:
        """G97 — `etc`, `include`, `man` and `share` are not scanned: what a hand install puts
        there arrives with an application the scan finds elsewhere, and `man` is a symlink to
        `share/man` that following would walk twice."""
        context, source, _target = make_context()
        job = ManualInstallsSyncJob(context)

        await self.scan(job)

        listing = next(c.args[0] for c in source.run_command.call_args_list if "find " in c.args[0])
        for never in ("/usr/local/etc", "/usr/local/include", "/usr/local/man", "/usr/local/share"):
            assert f"{never} " not in f"{listing} ", listing

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
            await self.scan(job)

        assert "Permission denied" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_scan_root_that_is_not_there_is_skipped_not_an_error(self) -> None:
        """G15, J90 — the loop tests each root before listing it, so the one tolerated failure never
        reaches the exit code — which is what lets the guard above trust that exit code.
        """
        context, source, _target = make_context(
            source_responses={
                "for root in": scan_finds("/opt/az"),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await self.scan(job)

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
                "for root in": scan_finds("/usr/local/flux", "/opt/az"),
                "dpkg --search": CommandResult(1, "", "dpkg-query: error: unable to open files list file"),
            }
        )
        job = ManualInstallsSyncJob(context)

        with pytest.raises(ProbeFailed) as excinfo:
            await self.scan(job)

        assert "/usr/bin/dpkg" in str(excinfo.value)
        assert "unable to open files list file" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_batch_where_every_path_is_unowned_is_an_ordinary_answer(self) -> None:
        """G19, J92 — the legitimate exit-1 case dpkg cannot distinguish by exit code: the witness is
        answered, so every other path really is unowned.
        """
        context, _source, _target = make_context(
            source_responses={
                "for root in": scan_finds("/usr/local/flux", "/opt/az"),
                "dpkg --search": CommandResult(
                    1, DPKG_WITNESS_LINE, "dpkg-query: no path found matching pattern /opt/az"
                ),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await self.scan(job)

        assert [item.identifier for item in items] == ["/opt/az", "/usr/local/flux"]

    @pytest.mark.asyncio
    async def test_the_witness_is_never_reported_as_a_finding(self) -> None:
        """G20 — the path handed to `dpkg --search` to prove it answered is filtered out of
        the candidates and never reported."""
        context, _source, _target = make_context(
            source_responses={
                "for root in": scan_finds("/opt/az"),
                "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await self.scan(job)

        assert [item.identifier for item in items] == ["/opt/az"]

    @pytest.mark.asyncio
    async def test_a_scan_root_is_never_a_finding_while_everything_under_it_still_is(self) -> None:
        """G96 — the nine directories `base-files` creates under `/usr/local` are entries of
        `/usr/local` like any other, and dpkg owns none of them on a stock machine, so without
        this rule every user would be asked for an install snippet for a stock directory on
        every run — including the five this scan looks inside, which would then be findings of
        their own scan.

        The ownership reply here is the stock machine's: nothing but the witness is owned. The
        skeleton is dropped before ownership is asked at all — none of the nine is among the
        queried paths — while every path genuinely under them stays a finding.
        """
        context, source, _target = make_context(
            source_responses={
                "for root in": scan_finds(
                    "/usr/local/bin/",
                    "/usr/local/etc/",
                    "/usr/local/games/",
                    "/usr/local/include/",
                    "/usr/local/lib/",
                    "/usr/local/man",
                    "/usr/local/sbin/",
                    "/usr/local/share/",
                    "/usr/local/src/",
                    "/usr/local/Brother",
                    "/usr/local/bin/talosctl",
                    "/usr/local/lib/node_modules",
                    "/opt/az",
                ),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, "dpkg-query: no path found matching pattern"),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await self.scan(job)

        assert {item.identifier for item in items} == {
            "/usr/local/Brother",
            "/usr/local/bin/talosctl",
            "/usr/local/lib/node_modules",
            "/opt/az",
        }
        ownership_call = next(
            c.args[0] for c in source.run_command.call_args_list if c.args[0].startswith("dpkg --search")
        )
        assert shlex.split(ownership_call)[2:] == [
            "/opt/az",
            "/usr/local/Brother",
            "/usr/local/bin/talosctl",
            "/usr/local/lib/node_modules",
            "/usr/bin/dpkg",
        ]

    @pytest.mark.asyncio
    async def test_a_directory_with_no_file_beneath_it_is_not_a_finding(self) -> None:
        """G98 — an empty shape is not software: there is nothing an install snippet could
        reproduce. The directory holding a file somewhere below it stays a finding, and each
        is asked about with one bounded look that stops at the first file it meets."""
        context, source, _target = make_context(
            source_responses={
                "for root in": scan_finds("/usr/local/lib/node_modules/", "/usr/local/lib/leftover/"),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, ""),
                "for dir in": CommandResult(0, "/usr/local/lib/node_modules\n", ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await self.scan(job)

        assert [item.identifier for item in items] == ["/usr/local/lib/node_modules"]
        emptiness_call = next(c.args[0] for c in source.run_command.call_args_list if c.args[0].startswith("for dir"))
        assert "-print -quit" in emptiness_call

    @pytest.mark.asyncio
    async def test_a_directory_whose_emptiness_could_not_be_established_stays_a_finding(self) -> None:
        """G99 — an unreadable subtree says nothing about whether a file is down there, so the
        probe names the directory on its own failure branch too and the finding survives.
        Dropping it would be silence read as data."""
        context, source, _target = make_context(
            source_responses={
                "for root in": scan_finds("/usr/local/lib/node_modules/"),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, ""),
                "for dir in": CommandResult(
                    0, "/usr/local/lib/node_modules\n", "find: '/usr/local/lib/node_modules/x': Permission denied"
                ),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await self.scan(job)

        assert [item.identifier for item in items] == ["/usr/local/lib/node_modules"]
        probe = next(c.args[0] for c in source.run_command.call_args_list if c.args[0].startswith("for dir"))
        assert 'else echo "$dir"' in probe, "a look that failed must keep the directory, not drop it"

    @pytest.mark.asyncio
    async def test_a_file_and_a_symlink_are_findings_like_a_directory(self) -> None:
        """G100 — a finding may be a file, a directory or a symlink; only the directory has an
        emptiness question to answer at all."""
        context, source, _target = make_context(
            source_responses={
                "for root in": CommandResult(0, "f\t/usr/local/bin/flux\nl\t/usr/local/bin/kubectl\n", ""),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, ""),
            }
        )
        job = ManualInstallsSyncJob(context)

        items = await self.scan(job)

        assert {item.identifier for item in items} == {"/usr/local/bin/flux", "/usr/local/bin/kubectl"}
        assert not [c.args[0] for c in source.run_command.call_args_list if c.args[0].startswith("for dir")]


class TestTheShapeOfAnOptDirectory:
    """`PKG-FR-MANUAL-OPT-SHAPE`: `/opt/<application>` and `/opt/<publisher>/<application>`
    are the same shape from outside, so what the directory holds decides — and where it holds
    several directories and no file, only the user can say which it is.
    """

    @staticmethod
    def context_for(
        opt_entry: str, children: CommandResult, *, reviewer: object | None = None
    ) -> tuple[JobContext, MagicMock, MagicMock]:

        return make_context(
            source_responses={
                "for root in": scan_finds(f"{opt_entry}/"),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, ""),
                f"find {opt_entry}": children,
                # Every directory that survives the shape rule holds a file somewhere below.
                "for dir in": every_directory_holds_a_file,
            },
            reviewer=reviewer,
        )

    @staticmethod
    async def scan(job: ManualInstallsSyncJob) -> list[str]:
        items = await job._scan_unowned_installs(  # pyright: ignore[reportPrivateUsage]
            job.source, job.machines.source, ask_when_ambiguous=True
        )
        return [item.identifier for item in items]

    @pytest.mark.asyncio
    async def test_a_directory_holding_a_file_of_its_own_is_the_finding(self) -> None:
        """G101 — `/opt/az` holds files, so it is one application and it is what is named."""
        context, _source, _target = self.context_for("/opt/az", scan_finds("/opt/az/bin/", "/opt/az/README"))
        job = ManualInstallsSyncJob(context)

        assert await self.scan(job) == ["/opt/az"]

    @pytest.mark.asyncio
    async def test_a_directory_holding_one_directory_and_no_file_names_that_directory(self) -> None:
        """G102 — `/opt/vendor/app` is the application; the publisher's own directory is not
        something a snippet reproduces."""
        context, _source, _target = self.context_for("/opt/vendor", scan_finds("/opt/vendor/app/"))
        job = ManualInstallsSyncJob(context)

        assert await self.scan(job) == ["/opt/vendor/app"]

    @pytest.mark.asyncio
    async def test_a_directory_holding_nothing_is_not_a_finding(self) -> None:
        """G103 — an empty `/opt/vendor` is a leftover shape, not software."""
        context, _source, _target = self.context_for("/opt/vendor", CommandResult(0, "", ""))
        job = ManualInstallsSyncJob(context)

        assert await self.scan(job) == []

    @pytest.mark.asyncio
    async def test_several_directories_and_no_file_asks_the_user_which_it_is(self) -> None:
        """G104, H180 — the question names both machines, shows what is inside, and offers the two
        item lists that follow rather than the mechanism that produces them."""
        reviewer = FakeGate(answer=True)
        context, _source, _target = self.context_for(
            "/opt/vendor", scan_finds("/opt/vendor/one/", "/opt/vendor/two/"), reviewer=reviewer
        )
        job = ManualInstallsSyncJob(context)

        assert await self.scan(job) == ["/opt/vendor"]

        asked = reviewer.asked[0]
        spoken = " ".join(str(value) for value in asked.values())
        assert "/opt/vendor/one" in spoken and "/opt/vendor/two" in spoken
        assert "source-host" in spoken and "target-host" in spoken
        assert "source" not in spoken.replace("source-host", "") and "target" not in spoken.replace("target-host", "")
        assert "target-host" in asked["proceed_label"] and "target-host" in asked["stop_label"]

    @pytest.mark.asyncio
    async def test_answering_a_publishers_directory_names_each_application_under_it(self) -> None:
        """G105 — the other answer: each directory is its own item, and the directory holding
        them is not one."""
        context, _source, _target = self.context_for(
            "/opt/vendor", scan_finds("/opt/vendor/one/", "/opt/vendor/two/"), reviewer=FakeGate(answer=False)
        )
        job = ManualInstallsSyncJob(context)

        assert await self.scan(job) == ["/opt/vendor/one", "/opt/vendor/two"]

    @pytest.mark.asyncio
    async def test_with_nobody_to_ask_the_directory_itself_is_the_finding(self) -> None:
        """G106 — a run with no terminal takes the shallower reading rather than inventing a
        list of applications nobody named."""
        context, _source, _target = self.context_for(
            "/opt/vendor", scan_finds("/opt/vendor/one/", "/opt/vendor/two/"), reviewer=FakeGate(answer=None)
        )
        job = ManualInstallsSyncJob(context)

        assert await self.scan(job) == ["/opt/vendor"]

    @pytest.mark.asyncio
    async def test_the_question_is_put_while_the_run_is_planning(self) -> None:
        """G107 — the answer decides what the review lists, so it cannot wait for the review;
        planning still writes nothing to either machine."""
        reviewer = FakeGate(answer=True)
        context, _source, target = make_context(
            source_responses={
                STATUS_QUERY: installed_on("gh"),
                "apt-cache policy": CommandResult(0, POLICY_REPO_INSTALLED, ""),
                "for root in": scan_finds("/opt/vendor/"),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, ""),
                "find /opt/vendor": scan_finds("/opt/vendor/one/", "/opt/vendor/two/"),
                "for dir in": every_directory_holds_a_file,
            },
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert reviewer.asked, "the shape question was never put"
        assert [diff.item_id for diff in plan.diffs] == ["unreproducible:unowned-path:/opt/vendor"]
        for call in target.run_command.call_args_list:
            assert "mutates" not in call.kwargs, call.args[0]

    @pytest.mark.asyncio
    async def test_the_same_shape_on_the_target_is_not_asked_about_and_counts_both_ways(self) -> None:
        """G108 — on the machine being changed the shape decides nothing: both readings count
        as already held, so whichever one the answer produced is subtracted, and one fact does
        not cost two questions."""
        reviewer = FakeGate(answer=True)
        context, _source, _target = make_context(
            target_responses={
                "for root in": scan_finds("/opt/vendor/"),
                "dpkg --search": CommandResult(1, DPKG_WITNESS_LINE, ""),
                "find /opt/vendor": scan_finds("/opt/vendor/one/", "/opt/vendor/two/"),
                "for dir in": CommandResult(0, "/opt/vendor\n/opt/vendor/one\n/opt/vendor/two\n", ""),
            },
            reviewer=reviewer,
        )
        job = ManualInstallsSyncJob(context)

        held = {item.item_id for item in await job.query_target_items()}

        assert {
            "unreproducible:unowned-path:/opt/vendor",
            "unreproducible:unowned-path:/opt/vendor/one",
            "unreproducible:unowned-path:/opt/vendor/two",
        } <= held
        assert not reviewer.asked


class TestWhatTheTargetAlreadyHolds:
    """`PKG-FR-MANUAL-DIFF`: both machines are scanned and only what the target lacks is
    presented, which is what stops one snippet's several traces from being asked about on
    every later run.
    """

    @pytest.mark.asyncio
    async def test_a_second_path_to_one_application_stops_being_asked_about(self) -> None:
        """G110 — the run after the snippet: one application under `/opt` and the symlink in
        `bin` that starts it are both on the target now, so neither is raised again."""
        both = ("/opt/az", "/usr/local/bin/az")
        context, _source, _target = make_context(
            source_responses={
                "for root in": scan_finds(*both),
                "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
            },
            target_responses={
                "for root in": scan_finds(*both),
                "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
            },
        )
        job = ManualInstallsSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()
        assert plan.groups == ()


class TestUnownedValidate:
    """This job's own `validate()`: the two `dpkg` reads its detection makes, and nothing
    of apt's — hand-installed `.deb` packages are `manual_deb_sync`'s."""

    @pytest.mark.asyncio
    async def test_dpkg_unavailable_on_source_yields_validation_error(self) -> None:
        """G119, K64 — validation fails before anything runs, naming the source and the scan its
        missing tool would have answered."""
        context, _source, _target = make_context(
            source_responses={"dpkg --version": CommandResult(127, "", "not found")}
        )
        job = ManualInstallsSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "detect unowned installs" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_dpkg_unavailable_on_target_yields_validation_error(self) -> None:
        """G120 — the target is read too now that what it already holds decides what is presented,
        so its missing tool is named before the run starts rather than as a dead probe
        halfway through."""
        context, _source, _target = make_context(
            target_responses={"dpkg --version": CommandResult(127, "", "not found")}
        )
        job = ManualInstallsSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "dpkg" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_yields_no_errors_and_asks_nothing_of_apt(self) -> None:
        """G121, K62 — with dpkg present on both machines nothing fails, and no apt tool is probed
        for at all: what apt can or cannot supply is another job's question."""
        context, source, target = make_context()
        job = ManualInstallsSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []
        assert not [cmd for cmd in all_calls(source) + all_calls(target) if "apt" in cmd]


class TestFirstSyncScope:
    def test_the_announced_scope_names_snippet_replay_as_what_it_does_to_the_target(self) -> None:
        """G92 — ADR-015's first-sync announcement names this job and the mechanism it uses
        on the target."""
        scope = ManualInstallsSyncJob.describe_first_sync_scope({})

        assert scope is not None
        assert scope.job_name == "manual_installs_sync"
        assert any("snippet" in item for item in scope.scope_items)
        assert "replay install snippet" in scope.mechanism


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


def _manual_decisions(*item_ids: str) -> str:
    """A manual decision file recording each id skip-always."""
    body = "".join(
        f'  "{item_id}":\n    item_class: unreproducible\n    label: "{item_id}"\n'
        f"    reason: null\n    recorded_at: '2026-07-30T00:00:00+00:00'\n"
        for item_id in item_ids
    )
    return f"machine_specific:\n{body}"


def _exists(*paths: str) -> Callable[[str], CommandResult]:
    """The `test -e` loop's answer on a machine holding exactly `paths` of those asked."""

    def _answer(command: str) -> CommandResult:
        asked = shlex.split(command.partition("for p in ")[2].partition(";")[0])
        return CommandResult(0, "".join(f"{path}\n" for path in asked if path in paths), "")

    return _answer


class TestMarksFollowWhatTheMachineHolds:
    """An unreproducible mark lives as long as the path it names is on the machine holding
    it — asked of the filesystem, never of the bounded scan.
    """

    @staticmethod
    async def _run(*, source_responses: dict[str, Answer]) -> MagicMock:
        context, source, _target = make_context(
            source_responses={
                "find /opt": scan_finds(),
                **source_responses,
            }
        )
        job = ManualInstallsSyncJob(context)
        job.accept_review(
            PackagePlan(manager="manual", diffs=(), groups=()),
            ReviewOutcome(decisions={}, was_interactive=True),
        )
        await job.apply()
        return source

    @pytest.mark.asyncio
    async def test_a_marked_path_that_is_gone_is_dropped(self) -> None:
        """H213 — the directory was deleted by hand; the entry keeping it goes too."""
        source = await self._run(
            source_responses={
                "manual.decisions.yaml": CommandResult(
                    0, _manual_decisions("unreproducible:unowned-path:/opt/vendor-app"), ""
                ),
                "for p in": _exists(),
            }
        )

        rewrites = [cmd for cmd in all_calls(source) if "mv --force" in cmd]
        assert len(rewrites) == 1
        assert "vendor-app" not in rewrites[0]

    @pytest.mark.asyncio
    async def test_a_marked_path_the_scan_never_looks_at_keeps_its_mark(self) -> None:
        """H214 — the check is `test -e`, not the scan: `PKG-FR-MANUAL-SCOPE` bounds the scan
        to `/opt` and `/usr/local`, so a marked path elsewhere is absent from every scan while
        sitting on disk, and reading the scan as the answer would drop it."""
        source = await self._run(
            source_responses={
                "manual.decisions.yaml": CommandResult(
                    0, _manual_decisions("unreproducible:unowned-path:/srv/vendor-app"), ""
                ),
                "for p in": _exists("/srv/vendor-app"),
            }
        )

        assert not [cmd for cmd in all_calls(source) if "mv --force" in cmd]

    @pytest.mark.asyncio
    async def test_the_machine_being_synced_to_has_its_own_file_reconciled(self) -> None:
        """H217 — a mark is reconciled against the machine holding it, which is a different
        question from whose marks silence a finding: a machine only ever synced TO would
        otherwise carry its dead marks for good."""
        context, _source, target = make_context(
            source_responses={"find /opt": scan_finds()},
            target_responses={
                "manual.decisions.yaml": CommandResult(
                    0, _manual_decisions("unreproducible:unowned-path:/opt/vendor-app"), ""
                ),
                "for p in": _exists(),
            },
        )
        job = ManualInstallsSyncJob(context)
        job.accept_review(
            PackagePlan(manager="manual", diffs=(), groups=()),
            ReviewOutcome(decisions={}, was_interactive=True),
        )

        await job.apply()

        rewrites = [cmd for cmd in all_calls(target) if "mv --force" in cmd]
        assert len(rewrites) == 1
        assert "vendor-app" not in rewrites[0]


class TestRemovingAPathTheSourceDropped:
    """`PKG-FR-MANUAL-REMOVE`: an unowned path only the target holds is this job's own
    finding there, so it becomes a removal once the source no longer has it."""

    @staticmethod
    def _target_only() -> tuple[JobContext, MagicMock]:
        empty_scan: dict[str, Answer] = {
            "for root in": scan_finds(),
            "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
        }
        context, _source, target = make_context(
            source_responses=empty_scan,
            target_responses={
                "for root in": scan_finds("/opt/az/"),
                "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
                "find /opt/az": scan_finds("/opt/az/bin"),
                "for dir in": every_directory_holds_a_file,
            },
        )
        return context, target

    @pytest.mark.asyncio
    async def test_a_path_only_the_target_holds_is_offered_for_removal(self) -> None:
        """G176 — an unowned path under `/opt` is software no package manager accounts for
        whichever machine it is on, so the target's own scan claims it and the source having
        dropped it makes it a removal."""
        context, _target = self._target_only()

        plan = await ManualInstallsSyncJob(context).plan()

        (diff,) = plan.diffs
        assert (diff.item_id, diff.action) == ("unreproducible:unowned-path:/opt/az", DiffAction.REMOVE)

    @pytest.mark.asyncio
    async def test_the_removal_deletes_the_path_and_the_screen_says_what_it_leaves(self) -> None:
        """G177 — `rm --recursive --force` takes the scanned path and nothing else, while the
        snippet that created it will usually also have dropped a launcher or a symlink
        somewhere the scan never looks. The screen says so above the rows."""
        context, target = self._target_only()
        job = ManualInstallsSyncJob(context)
        plan = await job.plan()

        await job.converge(plan.diffs[0])

        (issued,) = [c for c in target.run_command.call_args_list if "rm --recursive" in c.args[0]]
        assert issued.args[0] == "sudo rm --recursive --force /opt/az"
        assert issued.kwargs["mutates"]
        (group,) = [g for g in plan.groups if g.action == DiffAction.REMOVE.value]
        assert group.note is not None
        assert "outside these directories" in group.note


class TestTheInstalledVersionSnippet:
    """`PKG-FR-VERSION-SNIPPET`: what "installed version" means for an unowned path is
    whatever the entry's own `version_body` prints on the machine running it (D-22)."""

    @staticmethod
    def _both_hold(registry: str, source_version: str, target_version: str) -> tuple[JobContext, MagicMock, MagicMock]:
        scan: dict[str, Answer] = {
            "for root in": scan_finds("/opt/az/"),
            "dpkg --search": CommandResult(0, DPKG_WITNESS_LINE, ""),
            "find /opt/az": scan_finds("/opt/az/bin"),
            "for dir in": every_directory_holds_a_file,
        }
        return make_context(
            source_responses={
                **scan,
                "cat ~/.config/pc-switcher/package-snippets.yaml": CommandResult(0, registry, ""),
                "bash -c 'az --version'": CommandResult(0, f"{source_version}\n", ""),
            },
            target_responses={**scan, "bash -c 'az --version'": CommandResult(0, f"{target_version}\n", "")},
        )

    @pytest.mark.asyncio
    async def test_the_version_body_runs_on_both_machines_and_its_output_is_compared(self) -> None:
        """G178 — one command per machine, ungated, while the run is still planning: the
        obligation to keep it read-only is the author's, and the two strings it prints are
        the whole of what the diff compares."""
        context, source, target = self._both_hold(AZ_REGISTRY_YAML, "2.0", "1.0")

        plan = await ManualInstallsSyncJob(context).plan()

        (diff,) = plan.diffs
        assert diff.action == DiffAction.CHANGE
        assert diff.detail == "source-host has 2.0, target-host has 1.0"
        for machine in (source, target):
            (call,) = [c for c in machine.run_command.call_args_list if c.args[0] == "bash -c 'az --version'"]
            assert "mutates" not in call.kwargs

    @pytest.mark.asyncio
    async def test_the_same_output_on_both_machines_produces_nothing(self) -> None:
        """G179 — equal versions are convergence, and the tree behind them is never read:
        the guarantee is apt's, snap's and flatpak's, and no more."""
        context, _source, _target = self._both_hold(AZ_REGISTRY_YAML, "2.0", "2.0")

        plan = await ManualInstallsSyncJob(context).plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_a_path_with_no_registry_entry_has_no_version_to_compare(self) -> None:
        """G180 — a path nobody has written a snippet for is a finding to resolve, not a
        version to compare: there is no body to ask either machine with."""
        context, _source, _target = self._both_hold("snippets: {}\n", "2.0", "1.0")

        plan = await ManualInstallsSyncJob(context).plan()

        assert plan.diffs == ()
