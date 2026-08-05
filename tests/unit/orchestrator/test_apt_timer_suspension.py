"""Unit tests for the suspension of Ubuntu's own apt update timers the orchestrator applies
around the RUN_JOBS window (#248, `PKG-FR-APT-TIMER-PAUSE`).

Covers:
- Both timers are stopped on BOTH hosts when `apt_sync` is enabled, and each host's timer
  state is read first (a read-only `systemctl show` before any stop).
- The stop is NOT applied when `apt_sync` is disabled, nor in dry-run.
- A host whose timer state could not be read is left untouched, and so is one whose timers
  are masked, disabled or already stopped — the set restarted is the set stopped, never more.
- The suspension undoes itself without this process: a transient systemd timer that restarts
  the exact timers stopped is scheduled BEFORE the stop, so there is no instant at which a
  machine's updates are off with nothing scheduled to turn them back on. Proven with
  `_cleanup` never running.
- Cleanup restarts the timers and then cancels that scheduled unit, in that order.
- A restore left pending by a run that died is ADOPTED rather than walked past: its timer set
  joins what this run owes, this run's own unit is scheduled first, and only then is the
  predecessor cancelled — so it can never fire part-way through this sync.
- Every warning names the machine it concerns by hostname (`PKG-FR-NAME-THE-MACHINES`).

All executor interactions are mocked; no real systemctl/systemd-run commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config import Configuration
from pcswitcher.models import CommandResult, SyncAbortedByUser
from pcswitcher.orchestrator import Orchestrator

APT_TIMERS = ("apt-daily.timer", "apt-daily-upgrade.timer")
# Both machine names are pinned: the source's is otherwise the real hostname of whatever
# machine runs the suite, and neither may contain "source"/"target" for the naming
# assertions in `TestWarningsNameTheMachines` to mean anything.
SOURCE_MACHINE = "Atlas"
TARGET_MACHINE = "Nomad"


def show_output(*units: tuple[str, str, str, str]) -> str:
    """`systemctl show`'s blank-line-separated blocks for `(id, load, active, file state)`."""
    return "\n".join(
        f"Id={unit}\nLoadState={load}\nActiveState={active}\nUnitFileState={file_state}\n"
        for unit, load, active, file_state in units
    )


RUNNING = show_output(
    ("apt-daily.timer", "loaded", "active", "enabled"),
    ("apt-daily-upgrade.timer", "loaded", "active", "enabled"),
)
MASKED = show_output(
    ("apt-daily.timer", "masked", "inactive", "masked"),
    ("apt-daily-upgrade.timer", "masked", "inactive", "masked"),
)
STOPPED = show_output(
    ("apt-daily.timer", "loaded", "inactive", "disabled"),
    ("apt-daily-upgrade.timer", "loaded", "inactive", "disabled"),
)
# Only the upgrade timer runs — the asymmetric case that says whether the restore is exact.
ONLY_UPGRADE = show_output(
    ("apt-daily.timer", "loaded", "inactive", "enabled"),
    ("apt-daily-upgrade.timer", "loaded", "active", "enabled"),
)


def pending_output(*units: tuple[str, str]) -> str:
    """`systemctl show`'s blocks for pending restore units: `(unit id, the argv it will run)`.

    Shaped like the real thing, `ExecStart={ path=… ; argv[]=<command> ; … }`, because reading
    the adopted timer set out of that rendering is the part that can silently go wrong.
    """
    return "\n".join(
        f"ExecStart={{ path=/usr/bin/systemctl ; argv[]={argv} ; ignore_errors=no ; pid=0 }}\nId={unit}\n"
        for unit, argv in units
    )


PREDECESSOR = "pc-switcher-apt-timers-deadbeef.service"
BOTH_TIMERS_ARGV = "/usr/bin/systemctl start apt-daily.timer apt-daily-upgrade.timer"

# The two reads are matched on what makes each unmistakable: the state capture asks for
# LoadState, the predecessor enumeration goes through `list-units`. Matching either on a bare
# "systemctl show" would make the enumeration answer with the timer state, which parses into
# two blocks that look like predecessor units — a fake that quietly tests nothing.
READ_OK = {"--property=LoadState": CommandResult(0, RUNNING, "")}
READ_FAILS = {"--property=LoadState": CommandResult(1, "", "Failed to connect to bus")}
NO_PENDING = {"list-units": CommandResult(0, "", "")}
ENUM_FAILS = {"list-units": CommandResult(1, "", "Failed to connect to bus")}


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


def make_executor(responses: dict[str, CommandResult] | None = None) -> MagicMock:
    ex = MagicMock()
    ex.run_command = AsyncMock(side_effect=respond_to({**NO_PENDING, **READ_OK, **(responses or {})}))
    return ex


def warnings_of(orchestrator: Orchestrator) -> list[str]:
    """Every WARNING the orchestrator logged, rendered with its args."""
    logger = cast(MagicMock, orchestrator._logger)  # pyright: ignore[reportPrivateUsage]
    return [
        str(call.args[0]) % tuple(call.args[1:]) if len(call.args) > 1 else str(call.args[0])
        for call in logger.warning.call_args_list
    ]


def infos_of(orchestrator: Orchestrator) -> list[str]:
    """Every INFO the orchestrator logged, rendered with its args."""
    logger = cast(MagicMock, orchestrator._logger)  # pyright: ignore[reportPrivateUsage]
    return [
        str(call.args[0]) % tuple(call.args[1:]) if len(call.args) > 1 else str(call.args[0])
        for call in logger.info.call_args_list
    ]


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


def stops_of(mock: MagicMock) -> list[str]:
    """Every command that stops one of the apt timers themselves.

    Deliberately not "every `systemctl stop`": scheduling the deferred restart and cancelling
    it both stop the transient unit, and counting those as suspensions would let a run that
    only ever touched its own bookkeeping read as one that paused the machine.
    """
    return [cmd for cmd in all_calls(mock) if "systemctl stop" in cmd and "apt-daily" in cmd]


def make_orchestrator(  # noqa: PLR0913 - test builder knobs; all keyword-only
    *,
    apt_sync_enabled: bool,
    dry_run: bool = False,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    source_executor: MagicMock | None = None,
    target_executor: MagicMock | None = None,
) -> tuple[Orchestrator, MagicMock, MagicMock]:
    config = MagicMock(spec=Configuration)
    config.sync_jobs = {"apt_sync": apt_sync_enabled}
    orchestrator = Orchestrator(target=TARGET_MACHINE, config=config, dry_run=dry_run)
    orchestrator._source_hostname = SOURCE_MACHINE  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    source = source_executor if source_executor is not None else make_executor(source_responses)
    target = target_executor if target_executor is not None else make_executor(target_responses)
    orchestrator._local_executor = source  # pyright: ignore[reportPrivateUsage]
    orchestrator._remote_executor = target  # pyright: ignore[reportPrivateUsage]
    return orchestrator, source, target


class TestSuspensionEngaged:
    @pytest.mark.asyncio
    async def test_both_timers_stopped_on_both_hosts_when_apt_sync_enabled(self) -> None:
        """K94 — both machines stop both apt update timers for the run."""
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            stops = stops_of(ex)
            assert len(stops) == 1, f"expected one stop per host, got {stops}"
            assert all(timer in stops[0] for timer in APT_TIMERS)
        assert orchestrator._apt_timers_engaged is True  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_capture_is_read_only_and_precedes_the_stop(self) -> None:
        """K95 — each machine's timer state is read first, and reading it changes nothing."""
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        cmds = all_calls(source)
        show_idx = next(i for i, c in enumerate(cmds) if "--property=LoadState" in c)
        stop_idx = next(i for i, c in enumerate(cmds) if "systemctl stop" in c and "apt-daily" in c)
        assert show_idx < stop_idx
        reads = [call for call in source.run_command.call_args_list if "--property=LoadState" in call.args[0]]
        assert all(call.kwargs.get("mutates") is None for call in reads)

    @pytest.mark.asyncio
    async def test_the_announcement_names_its_owner_its_span_and_the_self_restart(self) -> None:
        """K96 — the pause fires before the first job, so the line announcing it says who holds
        it, why it is not scoped to the apt job, and that each machine restarts its own timers
        on its own — which is what stops a reader from having to check afterwards.
        """
        orchestrator, _source, _target = make_orchestrator(apt_sync_enabled=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        announcements = [line for line in infos_of(orchestrator) if "Pausing the system apt update timers" in line]
        assert announcements
        for line in announcements:
            assert "whole run" in line
            assert "orchestrator" in line
            assert "dpkg lock" in line
            assert "restart by themselves" in line

    @pytest.mark.asyncio
    async def test_nothing_is_suspended_when_apt_sync_disabled(self) -> None:
        """K97 — nothing is suspended when `apt_sync` is not enabled."""
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=False)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert all_calls(ex) == []
        assert orchestrator._apt_timers_engaged is False  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_nothing_is_suspended_in_dry_run(self) -> None:
        """K98 — nothing is suspended in a dry run."""
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True, dry_run=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert all_calls(ex) == []
        assert orchestrator._apt_timers_engaged is False  # pyright: ignore[reportPrivateUsage]


class TestOnlyRunningTimersAreTouched:
    """The set stopped is the set restarted. A machine that had the updater masked, disabled
    or simply stopped said so; starting its timers at cleanup would impose a policy it never
    had, and the machine would have no way to notice.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [MASKED, STOPPED])
    async def test_a_machine_that_is_not_running_the_updater_is_left_alone(self, state: str) -> None:
        """K99 — masked or already stopped: nothing is stopped and nothing is scheduled."""
        idle = {"--property=LoadState": CommandResult(0, state, "")}
        orchestrator, source, target = make_orchestrator(
            apt_sync_enabled=True, source_responses=idle, target_responses=idle
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert stops_of(ex) == []
            assert not any("systemd-run" in c for c in all_calls(ex))
            assert not any("systemctl start" in c for c in all_calls(ex))

    @pytest.mark.asyncio
    async def test_only_the_running_timer_is_stopped_and_only_it_comes_back(self) -> None:
        """K100 — one timer running and one not: exactly that one is stopped, scheduled for
        restart and restarted; the machine does not gain a timer it did not have running.
        """
        partial = {"--property=LoadState": CommandResult(0, ONLY_UPGRADE, "")}
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True, source_responses=partial)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        touching_the_idle_timer = [
            c for c in all_calls(source) if "apt-daily.timer" in c and "--property=LoadState" not in c
        ]
        assert touching_the_idle_timer == []
        assert any("systemctl start apt-daily-upgrade.timer" in c for c in all_calls(source))

    @pytest.mark.asyncio
    async def test_a_machine_whose_state_cannot_be_read_is_left_untouched(self) -> None:
        """K101 — with no reading of the prior state there is nothing to put back, so the
        timers are not stopped there at all. The other machine is unaffected.
        """
        orchestrator, source, target = make_orchestrator(
            apt_sync_enabled=True, source_responses=READ_FAILS, target_responses=READ_OK
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert stops_of(source) == []
        assert len(stops_of(target)) == 1
        assert any("could not be read" in w for w in warnings_of(orchestrator))

    @pytest.mark.asyncio
    async def test_a_truncated_reading_is_not_read_as_a_machine_without_timers(self) -> None:
        """K102 — an answer naming only one of the two timers is not this machine's state; it
        is an answer that did not arrive, and a machine is not suspended on it.
        """
        half = show_output(("apt-daily.timer", "loaded", "active", "enabled"))
        orchestrator, source, _target = make_orchestrator(
            apt_sync_enabled=True, source_responses={"--property=LoadState": CommandResult(0, half, "")}
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert stops_of(source) == []


class TestTheSuspensionUndoesItselfWithoutThisProcess:
    """The property the whole design exists for. `systemctl stop` has no expiry, so a run
    killed between the stop and `_cleanup` would leave a user's machine with its security
    updates off — permanently, and silently. Every test here runs the suspension and NEVER
    calls `_restore_apt_timers` or `_cleanup`.
    """

    @pytest.mark.asyncio
    async def test_a_restart_is_scheduled_on_each_host_and_this_process_owns_none_of_it(self) -> None:
        """K103 — with cleanup never reached, each machine still holds a transient systemd timer
        whose payload starts back exactly the timers this run stopped.
        """
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            scheduled = [c for c in all_calls(ex) if "systemd-run" in c]
            assert len(scheduled) == 1, f"expected exactly one scheduled restart, got {scheduled}"
            command = scheduled[0]
            assert "--on-active=" in command, "the restart is not deferred, so nothing fires after a crash"
            payload = command.split("--description=", 1)[1]
            assert "/usr/bin/systemctl start" in payload
            for timer in APT_TIMERS:
                assert timer in payload

    @pytest.mark.asyncio
    async def test_the_restart_is_scheduled_before_the_timers_are_stopped(self) -> None:
        """K104 — the safety net is placed before the fall: a run killed at any instant after
        the stop has a scheduled restart behind it, which stopping first could not promise.
        """
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        cmds = all_calls(source)
        schedule_idx = next(i for i, c in enumerate(cmds) if "systemd-run" in c)
        stop_idx = next(i for i, c in enumerate(cmds) if "systemctl stop" in c and "apt-daily" in c)
        assert schedule_idx < stop_idx

    @pytest.mark.asyncio
    async def test_a_machine_whose_restart_cannot_be_scheduled_is_never_stopped(self) -> None:
        """K105 — no scheduled restart, no suspension: running one sync against a machine that
        may patch itself is a race, while turning its updates off with no way back is lasting.
        """
        orchestrator, source, target = make_orchestrator(
            apt_sync_enabled=True,
            source_responses={"systemd-run": CommandResult(1, "", "Unit already exists")},
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert stops_of(source) == []
        assert len(stops_of(target)) == 1
        assert any("could not be scheduled" in w for w in warnings_of(orchestrator))

    @pytest.mark.asyncio
    async def test_the_scheduled_restart_says_whose_it_is_on_the_machine_it_sits_on(self) -> None:
        """K106 — a user finding a pending unit on their own machine has to be able to tell what
        put it there and what it will do, from `systemctl list-timers` alone.
        """
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        scheduled = next(c for c in all_calls(source) if "systemd-run" in c)
        unit = scheduled.split("--unit=", 1)[1].split()[0]
        assert unit.startswith("pc-switcher-")
        assert "pc-switcher: restart the system apt update timers" in scheduled

    @pytest.mark.asyncio
    async def test_a_stop_that_fails_still_leaves_the_machine_restorable(self) -> None:
        """K107 — the machine is recorded as suspended before the stop is issued, so even a stop
        whose outcome is unknown gets its scheduled unit cancelled and its timers started.
        """
        orchestrator, source, _target = make_orchestrator(
            apt_sync_enabled=True,
            source_responses={"systemctl stop apt-daily": CommandResult(1, "", "Interactive authentication required")},
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert any("systemctl start apt-daily.timer apt-daily-upgrade.timer" in c for c in all_calls(source))
        assert any("Could not pause the system apt update timers on" in w for w in warnings_of(orchestrator))


class TestAdoptingAPredecessor:
    """A run that died leaves its restore unit pending AND the timers stopped. The next run
    therefore reads the timers as inactive, and without adoption would walk straight past that
    host — scheduling nothing, cancelling nothing — leaving the dead run's unit to fire in the
    middle of it and start `apt-daily-upgrade.timer` against a converging apt.

    Adoption is what closes it: the pending unit's timer set joins what this run owes, so the
    unit becomes safe to cancel and the machine is restored once, at this run's cleanup.
    """

    @staticmethod
    def _with_predecessor(state: str, argv: str = BOTH_TIMERS_ARGV) -> dict[str, CommandResult]:
        return {
            "--property=LoadState": CommandResult(0, state, ""),
            "list-units": CommandResult(0, pending_output((PREDECESSOR, argv)), ""),
        }

    @pytest.mark.asyncio
    async def test_a_predecessor_on_a_stopped_host_is_adopted_cancelled_and_honoured(self) -> None:
        """K121 — the reported hole: timers already stopped, a restore pending. The run takes
        over its timer set, cancels it so it cannot fire mid-sync, and puts the timers back at
        cleanup — which is where the machine's updates should return, not at a random moment
        inside the next sync.
        """
        orchestrator, source, _target = make_orchestrator(
            apt_sync_enabled=True, source_responses=self._with_predecessor(STOPPED)
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert orchestrator._apt_timers_owed_source == APT_TIMERS  # pyright: ignore[reportPrivateUsage]
        assert any("systemd-run" in c for c in all_calls(source)), "no restore of this run's own was scheduled"
        assert any(f"systemctl stop {PREDECESSOR.removesuffix('.service')}.timer" in c for c in all_calls(source))
        # Nothing was running, so nothing is stopped — the suspension here is adoption alone.
        assert stops_of(source) == []

        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert any("systemctl start apt-daily.timer apt-daily-upgrade.timer" in c for c in all_calls(source))

    @pytest.mark.asyncio
    async def test_a_predecessor_on_a_manually_restarted_host_does_not_cost_the_suspension(self) -> None:
        """K122 — someone started the timers again after the dead run. The host is suspended
        normally: the per-run unit name cannot clash with the predecessor's, so scheduling
        succeeds where a fixed name would have been refused and left the host unguarded.
        """
        orchestrator, source, _target = make_orchestrator(
            apt_sync_enabled=True, source_responses=self._with_predecessor(RUNNING)
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        scheduled = [c for c in all_calls(source) if "systemd-run" in c]
        assert len(scheduled) == 1
        assert PREDECESSOR.removesuffix(".service") not in scheduled[0].split("--unit=", 1)[1].split()[0]
        assert len(stops_of(source)) == 1
        assert warnings_of(orchestrator) == []

    @pytest.mark.asyncio
    async def test_this_runs_restore_is_scheduled_before_any_predecessor_is_cancelled(self) -> None:
        """K123 — the ordering that makes adoption safe. Cancelling first would strand the
        machine: between the cancel and a schedule that then failed, its timers would be
        stopped with nothing at all pending to start them.
        """
        orchestrator, source, _target = make_orchestrator(
            apt_sync_enabled=True, source_responses=self._with_predecessor(RUNNING)
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        cmds = all_calls(source)
        schedule_idx = next(i for i, c in enumerate(cmds) if "systemd-run" in c)
        cancel_idx = next(i for i, c in enumerate(cmds) if PREDECESSOR.removesuffix(".service") in c)
        stop_idx = next(i for i, c in enumerate(cmds) if "systemctl stop" in c and "apt-daily" in c)
        assert schedule_idx < cancel_idx < stop_idx

    @pytest.mark.asyncio
    async def test_a_failed_schedule_cancels_nothing_so_the_machine_is_never_stranded(self) -> None:
        """K124 — "a predecessor was cancelled and then scheduling failed" is made unreachable
        rather than recovered from: scheduling comes first, so its failure means nothing was
        cancelled and nothing was stopped. The timers are left running and the predecessor's
        unit still covers the machine, which is a stronger guarantee than starting them back.
        """
        responses = self._with_predecessor(RUNNING)
        responses["systemd-run"] = CommandResult(1, "", "Failed to start transient timer")
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True, source_responses=responses)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert not any(PREDECESSOR.removesuffix(".service") in c for c in all_calls(source))
        assert stops_of(source) == []
        assert orchestrator._apt_timers_owed_source == ()  # pyright: ignore[reportPrivateUsage]
        assert any("could not be scheduled" in w for w in warnings_of(orchestrator))

    @pytest.mark.asyncio
    async def test_the_adopted_set_comes_off_exec_start_and_holds_only_apt_timers(self) -> None:
        """K125 — what a unit under our prefix will run is still input. Only the two apt timers
        are adopted from its `ExecStart`, so a hand-edited unit cannot talk a run into starting
        something else, and a partial promise is adopted as exactly that.
        """
        argv = "/usr/bin/systemctl start apt-daily-upgrade.timer some-other.timer"
        orchestrator, _source, _target = make_orchestrator(
            apt_sync_enabled=True, source_responses=self._with_predecessor(STOPPED, argv)
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        owed = orchestrator._apt_timers_owed_source  # pyright: ignore[reportPrivateUsage]
        assert owed == ("apt-daily-upgrade.timer",)

    @pytest.mark.asyncio
    async def test_a_host_whose_pending_restores_cannot_be_listed_is_left_untouched(self) -> None:
        """K126 — an unseen pending unit is the one that fires mid-run, so a failed enumeration
        is the same refusal as an unreadable timer state rather than "there is no predecessor".
        """
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True, source_responses=ENUM_FAILS)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert stops_of(source) == []
        assert not any("systemd-run" in c for c in all_calls(source))
        assert len(stops_of(target)) == 1

    @pytest.mark.asyncio
    async def test_the_takeover_is_announced_with_the_unit_it_took_over(self) -> None:
        """K127 — a restore firing mid-sync is the failure this prevents, so the run says when
        it found one, and names the unit a reader would otherwise have to hunt for.
        """
        orchestrator, _source, _target = make_orchestrator(
            apt_sync_enabled=True, source_responses=self._with_predecessor(STOPPED)
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        takeovers = [line for line in infos_of(orchestrator) if "Taking over" in line]
        assert takeovers
        assert all(PREDECESSOR in line and SOURCE_MACHINE in line for line in takeovers)


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_starts_the_timers_then_cancels_the_scheduled_restart(self) -> None:
        """K108 — starting first and cancelling second: an interruption between the two leaves
        the machine with the scheduled restart still pending rather than with neither.
        """
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True)
        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        cmds = all_calls(source)
        start_idx = next(i for i, c in enumerate(cmds) if "systemctl start apt-daily" in c)
        cancel_idx = next(
            i for i, c in enumerate(cmds) if "systemctl stop" in c and "apt-daily" not in c and i > start_idx
        )
        assert start_idx < cancel_idx
        assert orchestrator._apt_timers_engaged is False  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_a_scheduled_restart_that_already_fired_is_not_a_warning(self) -> None:
        """K109 — on a run longer than the suspension the unit is gone by cleanup, and
        cancelling nothing is the expected outcome rather than something to report.
        """
        orchestrator, _source, _target = make_orchestrator(
            apt_sync_enabled=True,
            source_responses={"systemctl stop pc-switcher": CommandResult(5, "", "Unit not loaded.")},
        )
        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert warnings_of(orchestrator) == []

    @pytest.mark.asyncio
    async def test_restore_is_noop_when_nothing_was_suspended(self) -> None:
        """K110 — a run that never suspended anything issues no command at cleanup."""
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=False)

        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert all_calls(source) == []
        assert all_calls(target) == []

    @pytest.mark.asyncio
    async def test_restore_is_idempotent(self) -> None:
        """K111 — cleanup twice restarts once."""
        orchestrator, _source, target = make_orchestrator(apt_sync_enabled=True)
        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]
        starts = len([c for c in all_calls(target) if "systemctl start apt-daily" in c])
        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert len([c for c in all_calls(target) if "systemctl start apt-daily" in c]) == starts


class TestCleanupOrder:
    @pytest.mark.asyncio
    async def test_the_restart_runs_before_the_connection_it_needs_is_torn_down(self) -> None:
        """K112 — the target's restart travels over the SSH connection `_cleanup` also closes."""
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True)
        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        order: list[str] = []

        def _record(cmd: str, **_: object) -> CommandResult:
            if "systemctl start apt-daily" in cmd:
                order.append("restart")
            return CommandResult(exit_code=0, stdout="", stderr="")

        for ex in (source, target):
            ex.run_command = AsyncMock(side_effect=_record)
            ex.terminate_all_processes = AsyncMock()
        connection = MagicMock()
        connection.kill_all_remote_processes = AsyncMock()
        connection.disconnect = AsyncMock(side_effect=lambda: order.append("disconnect"))
        orchestrator._connection = connection  # pyright: ignore[reportPrivateUsage]

        await orchestrator._cleanup()  # pyright: ignore[reportPrivateUsage]

        assert order == ["restart", "restart", "disconnect"]


class TestConfirmEachCommandGate:
    """The suspension declares itself a modification on both machines, and an abort at the
    restart is honoured rather than absorbed by the best-effort handler.
    """

    @staticmethod
    def _mutating_calls(mock: MagicMock) -> list[tuple[str, str]]:
        """(command, mutates) for every call that declared itself a modification."""
        return [
            (call.args[0], call.kwargs["mutates"])
            for call in mock.run_command.call_args_list
            if call.kwargs.get("mutates") is not None
        ]

    @staticmethod
    def _refuse_mutations(cmd: str, **kwargs: object) -> CommandResult:
        """Stand in for a user answering "abort" at the gate."""
        if kwargs.get("mutates") is not None:
            raise SyncAbortedByUser("declined")
        return respond_to({**NO_PENDING, **READ_OK})(cmd)

    @pytest.mark.asyncio
    async def test_every_write_declares_itself_and_the_state_read_does_not(self) -> None:
        """K113 — the schedule, the stop, the restart and the cancel are shown as changes; the
        `systemctl show` reading is not.
        """
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            declared = self._mutating_calls(ex)
            assert len(declared) == 4
            assert not any("systemctl show" in cmd for cmd, _ in declared)

    @pytest.mark.asyncio
    async def test_abort_at_the_restart_is_not_swallowed_by_the_best_effort_handler(self) -> None:
        """K114 — declining the restart at the gate stops the write rather than being absorbed."""
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True)
        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        source.run_command = AsyncMock(side_effect=self._refuse_mutations)
        with pytest.raises(SyncAbortedByUser):
            await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_cleanup_honours_the_abort_but_still_releases_resources(self) -> None:
        """K115 — an abort at the restart stops the write and nothing else: a confirmation
        prompt must never be able to leak the target lock or the SSH connection.
        """
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True)
        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]
        source.run_command = AsyncMock(side_effect=self._refuse_mutations)
        source.terminate_all_processes = AsyncMock()
        target.terminate_all_processes = AsyncMock()

        lock_process = MagicMock()
        orchestrator._target_lock_process = lock_process  # pyright: ignore[reportPrivateUsage]
        source_lock = MagicMock()
        orchestrator._source_lock = source_lock  # pyright: ignore[reportPrivateUsage]

        with patch("pcswitcher.orchestrator.release_remote_lock", new=AsyncMock()) as release:
            await orchestrator._cleanup()  # pyright: ignore[reportPrivateUsage]

        release.assert_awaited_once_with(lock_process)
        source_lock.release.assert_called_once()
        assert any("restart" in w and "not restarted" in w for w in warnings_of(orchestrator))


class TestWarningsNameTheMachines:
    """`PKG-FR-NAME-THE-MACHINES`: a warning names the machine it concerns by hostname.

    The end-of-run summary reprints every captured warning on its own, long after the line
    that maps each role to a machine, so "not paused on the target" leaves the reader to work
    out which of their two computers is running its updater.
    """

    @staticmethod
    def _named(orchestrator: Orchestrator) -> list[str]:
        warnings = warnings_of(orchestrator)
        assert warnings, "expected at least one warning"
        for w in warnings:
            assert "source" not in w.lower(), w
            assert "target" not in w.lower(), w
        return warnings

    @pytest.mark.asyncio
    async def test_an_unreadable_state_names_the_machine_it_leaves_running(self) -> None:
        """K116 — the unreadable-state warning names the machine it leaves unsuspended."""
        orchestrator, _source, _target = make_orchestrator(
            apt_sync_enabled=True, source_responses=READ_FAILS, target_responses=READ_FAILS
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"Not pausing the system apt update timers on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"Not pausing the system apt update timers on {TARGET_MACHINE}" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_failed_stop_names_the_machine(self) -> None:
        """K107 — a stop that failed names the machine, and the run continues."""
        refuses = {"systemctl stop apt-daily": CommandResult(1, "", "Interactive authentication required")}
        orchestrator, _source, _target = make_orchestrator(
            apt_sync_enabled=True, source_responses=refuses, target_responses=refuses
        )

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"Could not pause the system apt update timers on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"Could not pause the system apt update timers on {TARGET_MACHINE}" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_failed_restart_names_the_machine_and_says_it_still_comes_back(self) -> None:
        """K117 — a restart command that failed names the machine and points at the scheduled
        restart, which is what actually puts the machine right.
        """
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True)
        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]
        for ex in (source, target):
            ex.run_command = AsyncMock(return_value=CommandResult(1, "", "Failed to start"))

        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        for machine in (SOURCE_MACHINE, TARGET_MACHINE):
            assert any(f"Could not restart the system apt update timers on {machine}" in w for w in warnings)
        assert all("restart by themselves" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_restart_that_raises_names_the_machine(self) -> None:
        """K118 — a restart whose connection is already gone names the machine."""
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True)
        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]
        for ex in (source, target):
            ex.run_command = AsyncMock(side_effect=ConnectionResetError("connection lost"))

        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        for machine in (SOURCE_MACHINE, TARGET_MACHINE):
            assert any(f"Error restarting the system apt update timers on {machine}" in w for w in warnings)
