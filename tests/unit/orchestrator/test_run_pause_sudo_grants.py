"""Unit tests for the sudo grant the run's machine-wide pauses need, established in the
validation step from the jobs' own declarations (`PKG-FR-SUDO-PRECONDITION`).

Both pauses are the ORCHESTRATOR's commands on BOTH machines — `sudo snap set system
refresh.hold`, `sudo systemctl` and `sudo systemd-run` — so no job's `validate()` covers
them, and a run whose only declaring job is `folder_sync` or `manual_deb_sync` reached them
with nothing having established the grant. The grant now travels with the pause
(`RunPause.sudo_commands`), so declaring one is what puts its check in the run.

Covers:
- A `folder_sync`-only run establishes the snapd pause's grant on both machines, and a
  `manual_deb_sync`-only run the apt-timer pause's.
- Two jobs declaring the same pause ask for one grant: one probe per machine, one error.
- The remediation names the binaries of every declared pause, in one sudoers line.
- A run whose enabled jobs declare no pause asks neither machine for anything.
- The failure names the machine by hostname (`PKG-FR-NAME-THE-MACHINES`).

All executor interactions are mocked; no real sudo runs.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator

SUDO_PROBE = "sudo --non-interactive true"
SUDO_REFUSED = CommandResult(exit_code=1, stdout="", stderr="sudo: a password is required")

# Pinned so the assertions about naming the machines mean something: neither may contain
# the words "source" or "target", and the source's is otherwise this machine's own hostname.
SOURCE_MACHINE = "Atlas"
TARGET_MACHINE = "Nomad"
TARGET_ACCOUNT = "testuser"


def respond_to(responses: dict[str, CommandResult]) -> Callable[..., CommandResult]:
    """Build a run_command side_effect matching by substring, succeeding otherwise."""

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        return CommandResult(exit_code=0, stdout="", stderr="")

    return _side_effect


def make_executor(responses: dict[str, CommandResult] | None = None) -> MagicMock:
    ex = MagicMock()
    ex.run_command = AsyncMock(side_effect=respond_to(responses or {}))
    return ex


def make_orchestrator(
    *,
    sync_jobs: dict[str, bool],
    dry_run: bool = False,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
) -> tuple[Orchestrator, MagicMock, MagicMock]:
    config = MagicMock(spec=Configuration)
    config.sync_jobs = sync_jobs
    orchestrator = Orchestrator(target=TARGET_MACHINE, config=config, dry_run=dry_run)
    orchestrator._source_hostname = SOURCE_MACHINE  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._connection = MagicMock(username=TARGET_ACCOUNT)  # pyright: ignore[reportPrivateUsage]
    source = make_executor(source_responses)
    target = make_executor(target_responses)
    orchestrator._local_executor = source  # pyright: ignore[reportPrivateUsage]
    orchestrator._remote_executor = target  # pyright: ignore[reportPrivateUsage]
    return orchestrator, source, target


async def grant_errors(orchestrator: Orchestrator) -> list[ValidationError]:
    return await orchestrator._validate_run_pause_grants()  # pyright: ignore[reportPrivateUsage]


def probes(executor: MagicMock) -> list[str]:
    return [call.args[0] for call in executor.run_command.call_args_list if SUDO_PROBE in call.args[0]]


class TestTheDeclarationCarriesItsGrant:
    """A job that declares a pause gets its grant checked, on both machines, whether or not
    the job the pause used to be gated on is enabled.
    """

    @pytest.mark.asyncio
    async def test_folder_sync_alone_asks_both_machines_for_the_snap_grant(self) -> None:
        """E126 — `folder_sync` declares the snapd pause, so the run writes `refresh.hold` on
        both machines; the grant for it is established even though `snap_sync`, whose own
        `validate()` used to carry the source's half, is off.
        """
        orchestrator, source, target = make_orchestrator(sync_jobs={"folder_sync": True, "snap_sync": False})

        assert await grant_errors(orchestrator) == []

        assert probes(source) == [SUDO_PROBE]
        assert probes(target) == [SUDO_PROBE]

    @pytest.mark.asyncio
    async def test_a_machine_refusing_the_snap_grant_fails_validation_naming_it(self) -> None:
        """E101, K46 — the source needs the grant because the pause writes there, and the refusal
        names the machine by hostname, says the pause is why, and carries the remediation.
        """
        orchestrator, _source, _target = make_orchestrator(
            sync_jobs={"folder_sync": True},
            source_responses={SUDO_PROBE: SUDO_REFUSED},
        )

        errors = await grant_errors(orchestrator)

        assert [error.host for error in errors] == [Host.SOURCE]
        message = errors[0].message
        assert SOURCE_MACHINE in message
        assert "snapd auto-refresh" in message
        assert "/usr/bin/snap" in message
        assert "sudo visudo --file=/etc/sudoers.d/pc-switcher" in message

    @pytest.mark.asyncio
    async def test_manual_deb_sync_alone_asks_both_machines_for_the_timer_grant(self) -> None:
        """K135 — `manual_deb_sync` declares the apt-timer pause, so `systemctl` and
        `systemd-run` run on both machines with `apt_sync` off; both binaries are named.
        """
        orchestrator, _source, _target = make_orchestrator(
            sync_jobs={"manual_deb_sync": True, "apt_sync": False},
            target_responses={SUDO_PROBE: SUDO_REFUSED},
        )

        errors = await grant_errors(orchestrator)

        assert [error.host for error in errors] == [Host.TARGET]
        message = errors[0].message
        assert TARGET_MACHINE in message
        assert "apt update timers" in message
        assert "/usr/bin/systemctl" in message
        assert "/usr/bin/systemd-run" in message
        # The account pc-switcher connects as, substituted into the grant line.
        assert f"{TARGET_ACCOUNT} ALL=(ALL) NOPASSWD:" in message

    @pytest.mark.asyncio
    async def test_a_run_declaring_no_pause_asks_neither_machine_for_anything(self) -> None:
        """K138 — nothing is probed for a pause nothing needs: `manual_snap_sync` declares
        none, and a grant it does not need must not refuse its run.
        """
        orchestrator, source, target = make_orchestrator(
            sync_jobs={"manual_snap_sync": True},
            source_responses={SUDO_PROBE: SUDO_REFUSED},
            target_responses={SUDO_PROBE: SUDO_REFUSED},
        )

        assert await grant_errors(orchestrator) == []

        assert probes(source) == []
        assert probes(target) == []

    @pytest.mark.asyncio
    async def test_a_dry_run_establishes_the_same_grant(self) -> None:
        """K139 — the pauses themselves are skipped in a dry run, but the check is not: a dry
        run that passed and a real run that then refused is the surprise this exists to remove.
        """
        orchestrator, _source, _target = make_orchestrator(
            sync_jobs={"snap_sync": True},
            dry_run=True,
            source_responses={SUDO_PROBE: SUDO_REFUSED},
        )

        assert [error.host for error in await grant_errors(orchestrator)] == [Host.SOURCE]


class TestOneGrantIsCheckedOnce:
    """Two jobs asking for the same pause ask for one grant — probed once per machine and
    reported once — and two pauses share the one sudoers line their binaries go on.
    """

    @pytest.mark.asyncio
    async def test_two_jobs_declaring_the_same_pause_are_probed_and_reported_once(self) -> None:
        """K136 — `snap_sync` and `folder_sync` both declare the snapd pause; the union is what
        the run checks, so neither machine is asked twice nor told twice.
        """
        orchestrator, source, target = make_orchestrator(
            sync_jobs={"snap_sync": True, "folder_sync": True},
            source_responses={SUDO_PROBE: SUDO_REFUSED},
            target_responses={SUDO_PROBE: SUDO_REFUSED},
        )

        errors = await grant_errors(orchestrator)

        assert [error.host for error in errors] == [Host.SOURCE, Host.TARGET]
        assert probes(source) == [SUDO_PROBE]
        assert probes(target) == [SUDO_PROBE]

    @pytest.mark.asyncio
    async def test_a_run_needing_both_pauses_names_every_binary_in_one_grant_line(self) -> None:
        """K137 — one machine, one message, one sudoers line: a user granting rights should not
        have to run the sync twice to discover the second pause's binaries.
        """
        orchestrator, _source, _target = make_orchestrator(
            sync_jobs={"apt_sync": True, "snap_sync": True},
            source_responses={SUDO_PROBE: SUDO_REFUSED},
        )

        errors = await grant_errors(orchestrator)

        assert len(errors) == 1
        grant_line = next(line for line in errors[0].message.splitlines() if "NOPASSWD:" in line)
        assert "/usr/bin/snap" in grant_line
        assert "/usr/bin/systemctl" in grant_line
        assert "/usr/bin/systemd-run" in grant_line
