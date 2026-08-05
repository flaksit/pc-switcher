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
- A restore a dead run left pending is RUN and cleared before this run reads anything, so the
  machine is back to its real prior state and that unit cannot fire part-way through this sync.
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

RESTORE_UNIT = "pc-switcher-apt-timers"
# The probe for a dead run's leftover restore is also a `systemctl show`, so it needs a key of
# its own AHEAD of the timer-state one — otherwise it answers with the timer state, which
# parses to blocks under the apt timers' names and quietly reports "nothing pending" forever.
RESTORE_PROBE = f"show {RESTORE_UNIT}.timer"
NO_PENDING = {RESTORE_PROBE: CommandResult(0, f"Id={RESTORE_UNIT}.timer\nLoadState=not-found\n", "")}
PENDING = {RESTORE_PROBE: CommandResult(0, f"Id={RESTORE_UNIT}.timer\nLoadState=loaded\n", "")}
READ_OK = {"systemctl show": CommandResult(0, RUNNING, "")}
READ_FAILS = {"systemctl show": CommandResult(1, "", "Failed to connect to bus")}


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


def make_host_with_pending_restore(state_after_trigger: str = RUNNING) -> MagicMock:
    """A host a dead run left behind: timers stopped, its restore unit still pending.

    Stateful because the behaviour under test is a sequence — the trigger RUNS the recorded
    payload, and only then does the timer state read as it really is. A fixed response could
    not tell "the machine was put back" from "the capture happened to say so".
    """
    state = {"timers": STOPPED, "pending": True}

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if RESTORE_PROBE in cmd:
            load = "loaded" if state["pending"] else "not-found"
            return CommandResult(0, f"Id={RESTORE_UNIT}.timer\nLoadState={load}\n", "")
        if f"systemctl start {RESTORE_UNIT}.service" in cmd:
            state["timers"] = state_after_trigger
            return CommandResult(exit_code=0, stdout="", stderr="")
        if f"systemctl stop {RESTORE_UNIT}.timer" in cmd:
            state["pending"] = False
            return CommandResult(exit_code=0, stdout="", stderr="")
        if "systemctl show" in cmd:
            return CommandResult(0, cast(str, state["timers"]), "")
        return CommandResult(exit_code=0, stdout="", stderr="")

    ex = MagicMock()
    ex.run_command = AsyncMock(side_effect=_side_effect)
    ex.host_state = state
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
        show_idx = next(i for i, c in enumerate(cmds) if "systemctl show" in c)
        stop_idx = next(i for i, c in enumerate(cmds) if "systemctl stop" in c and "apt-daily" in c)
        assert show_idx < stop_idx
        reads = [call for call in source.run_command.call_args_list if "systemctl show" in call.args[0]]
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
        idle = {"systemctl show": CommandResult(0, state, "")}
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
        partial = {"systemctl show": CommandResult(0, ONLY_UPGRADE, "")}
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True, source_responses=partial)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_apt_timers()  # pyright: ignore[reportPrivateUsage]

        touching_the_idle_timer = [
            c for c in all_calls(source) if "apt-daily.timer" in c and "systemctl show" not in c
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
            apt_sync_enabled=True, source_responses={"systemctl show": CommandResult(0, half, "")}
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


class TestSettlingAPendingRestore:
    """A run that died leaves its restore unit pending AND the timers stopped, so the next run
    would read them inactive, skip that host, and let the dead run's unit fire part-way through
    it — starting `apt-daily-upgrade.timer` against a converging apt, the exact race this
    feature prevents.

    Settling it RUNS the recorded payload and then cancels the schedule, before this run reads
    anything. Running it rather than only cancelling is what matters: cancel alone and the
    capture still sees stopped timers, the host is skipped again, and the machine keeps its
    updates off on this run and every later one until it reboots.
    """

    @pytest.mark.asyncio
    async def test_a_pending_restore_is_run_then_cleared_and_the_host_suspended_afresh(self) -> None:
        """K121 — the machine ends the suspension owned by THIS run: the dead run's payload has
        been executed, its schedule is gone, and the timers it had are stopped again behind a
        restart this run scheduled.
        """
        source = make_host_with_pending_restore()
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True, source_executor=source)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        cmds = all_calls(source)
        assert any(f"systemctl start {RESTORE_UNIT}.service" in c for c in cmds), "the recorded payload never ran"
        assert any(f"systemctl stop {RESTORE_UNIT}.timer" in c for c in cmds), "the old schedule was left pending"
        assert source.host_state["pending"] is False
        # The capture saw the restored truth, so this run suspended the host on its own terms.
        assert len(stops_of(source)) == 1
        assert any("systemd-run" in c for c in cmds)
        assert orchestrator._apt_timers_stopped_source == APT_TIMERS  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_the_pending_restore_is_settled_before_the_state_is_read(self) -> None:
        """K122 — the order is the point: reading first would capture the dead run's leftovers
        as if they were the machine's own policy.
        """
        source = make_host_with_pending_restore()
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True, source_executor=source)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        cmds = all_calls(source)
        trigger_idx = next(i for i, c in enumerate(cmds) if f"systemctl start {RESTORE_UNIT}.service" in c)
        capture_idx = next(i for i, c in enumerate(cmds) if "systemctl show" in c and "apt-daily.timer" in c)
        assert trigger_idx < capture_idx

    @pytest.mark.asyncio
    async def test_no_pending_restore_is_a_silent_no_op(self) -> None:
        """K123 — the ordinary case, and the first run on any machine. `systemctl start` on an
        absent unit exits 5, so issuing it blind would warn on every run and prompt on every
        `--confirm-each-command` run; it is guarded on a read instead.
        """
        orchestrator, source, target = make_orchestrator(apt_sync_enabled=True)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert not any(f"systemctl start {RESTORE_UNIT}" in c for c in all_calls(ex))
            assert not any(f"systemctl stop {RESTORE_UNIT}" in c for c in all_calls(ex))
        assert warnings_of(orchestrator) == []

    @pytest.mark.asyncio
    async def test_a_failed_trigger_leaves_the_pending_restore_in_place(self) -> None:
        """K124 — cancelling after a failed trigger would take away the machine's only remaining
        way back. The schedule stays, and the run says so.
        """
        responses = {
            **PENDING,
            f"systemctl start {RESTORE_UNIT}.service": CommandResult(1, "", "Failed to start"),
        }
        orchestrator, source, _target = make_orchestrator(apt_sync_enabled=True, source_responses=responses)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        assert not any(f"systemctl stop {RESTORE_UNIT}.timer" in c for c in all_calls(source))
        assert any("Leaving it scheduled" in w for w in warnings_of(orchestrator))

    @pytest.mark.asyncio
    async def test_settling_names_the_machine_and_says_what_it_is_doing(self) -> None:
        """K125 — a restart firing mid-sync is the failure this prevents, so a run that found one
        says so and names the machine (`PKG-FR-NAME-THE-MACHINES`).
        """
        source = make_host_with_pending_restore()
        orchestrator, _source, _target = make_orchestrator(apt_sync_enabled=True, source_executor=source)

        await orchestrator._suspend_apt_timers()  # pyright: ignore[reportPrivateUsage]

        settled = [line for line in infos_of(orchestrator) if "Completing an apt update-timer restart" in line]
        assert settled
        assert all(SOURCE_MACHINE in line for line in settled)


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
        return respond_to(READ_OK)(cmd)

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
