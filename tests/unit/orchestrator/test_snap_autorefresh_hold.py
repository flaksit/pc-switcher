"""Unit tests for the transient snapd auto-refresh hold the orchestrator applies around
the RUN_JOBS window (decision 4, 02-UAT-REVIEW-FIXES).

Covers:
- The hold is set on BOTH hosts when `snap_sync` is enabled, and captures the prior
  `refresh.hold` first (read-only `snap get` before any `snap set`).
- The capture runs under sudo, because snapd admin-gates READING snap config: unprivileged
  the read fails, every host looks hold-free, and the restore then clears a hold the user
  set themselves. That is a silent data-loss bug, so the sudo is pinned here.
- The hold is NOT set when `snap_sync` is disabled, nor in dry-run.
- A host whose prior `refresh.hold` could not be read is left untouched: no hold is written
  there and none is cleared afterwards (`PKG-FR-SNAP-REFRESH-PAUSE`).
- Cleanup restores an unset state when there was no prior hold, restores the exact prior
  value when there was one, and leaves the option ALONE when the capture could not read it.
- Applying the hold is verified by reading it back; a hold that did not stick is a WARNING
  and never a failure.
- Restore is a no-op when no hold was engaged, and never blocks the manual `--revision`
  convergence (the hold command only writes the system-wide `refresh.hold` option).
- Every warning names the machine it concerns by hostname (`PKG-FR-NAME-THE-MACHINES`).

All executor interactions are mocked; no real snap/snapd commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcswitcher.config import Configuration
from pcswitcher.models import CommandResult, SyncAbortedByUser
from pcswitcher.orchestrator import Orchestrator


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
    ex.run_command = AsyncMock(side_effect=respond_to(responses or {}))
    return ex


# What `snap get` prints for an option that was never set (the orchestrator tells this
# apart from a failed read; see `_SNAP_HOLD_UNSET_MARKER`), and what it prints when the
# read was not privileged.
HOLD_UNSET_STDERR = 'error: snap "core" has no "refresh.hold" configuration option'
HOLD_DENIED_STDERR = "error: access denied (try with sudo)"
# The value the fake snapd below records for the orchestrator's `date`-computed timestamp.
APPLIED_HOLD = "2026-07-26T21:50:19Z"


def make_snapd(initial_hold: str | None = None) -> MagicMock:
    """Executor whose `snap get`/`snap set` share one `refresh.hold`, like snapd's own.

    A single fixed response cannot express what the read-back verification is about — the
    value has to change when it is written — so this fake stores it. `snap set` to the
    orchestrator's `date` expression records `APPLIED_HOLD`, to `""` records "no hold".
    """
    state: dict[str, str | None] = {"hold": initial_hold}

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if "snap set system refresh.hold=" in cmd:
            written = cmd.split("refresh.hold=", 1)[1].strip().strip('"').strip("'")
            state["hold"] = APPLIED_HOLD if "date --utc --date=" in written else (written or None)
            return CommandResult(exit_code=0, stdout="", stderr="")
        if "snap get system refresh.hold" in cmd:
            hold = state["hold"]
            if hold is None:
                return CommandResult(exit_code=1, stdout="", stderr=HOLD_UNSET_STDERR)
            return CommandResult(exit_code=0, stdout=hold + "\n", stderr="")
        return CommandResult(exit_code=0, stdout="", stderr="")

    ex = MagicMock()
    ex.run_command = AsyncMock(side_effect=_side_effect)
    ex.hold_state = state
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


# Both machine names are pinned: the source's is otherwise the real hostname of whatever
# machine runs the suite, and neither may contain "source"/"target" for the naming
# assertions in `TestWarningsNameTheMachines` to mean anything.
SOURCE_MACHINE = "Atlas"
TARGET_MACHINE = "Nomad"


def make_orchestrator(
    *,
    snap_sync_enabled: bool,
    dry_run: bool = False,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    source_executor: MagicMock | None = None,
    target_executor: MagicMock | None = None,
) -> tuple[Orchestrator, MagicMock, MagicMock]:
    config = MagicMock(spec=Configuration)
    config.sync_jobs = {"snap_sync": snap_sync_enabled}
    orchestrator = Orchestrator(target=TARGET_MACHINE, config=config, dry_run=dry_run)
    orchestrator._source_hostname = SOURCE_MACHINE  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    source = source_executor if source_executor is not None else make_executor(source_responses)
    target = target_executor if target_executor is not None else make_executor(target_responses)
    orchestrator._local_executor = source  # pyright: ignore[reportPrivateUsage]
    orchestrator._remote_executor = target  # pyright: ignore[reportPrivateUsage]
    return orchestrator, source, target


class TestHoldEngaged:
    @pytest.mark.asyncio
    async def test_hold_set_on_both_hosts_when_snap_sync_enabled(self) -> None:
        """E72, E88 — both machines are paused, with a timed value computed on each host's clock."""
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            cmds = all_calls(ex)
            assert any("snap set system refresh.hold=" in c for c in cmds)
            # Timed hold: the value is computed on the host from `date`, not indefinite.
            assert any("date --utc --date=" in c for c in cmds)
            # Never the indefinite `snap refresh --hold` verb (snap_sync Pitfall 1).
            assert not any("snap refresh --hold" in c for c in cmds)
        assert orchestrator._snap_hold_engaged is True  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_the_announcement_names_its_owner_and_why_it_spans_the_run(self) -> None:
        """#233 — the pause fires before the first job, so the line that announces it has to
        say who is holding it and why it is not scoped to the snap job: folder_sync mirrors
        each snap's data directory by the revision snap_sync converged, so a refresh between
        the two silently drops that directory from the mirror.
        """
        orchestrator, _source, _target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        announcements = [line for line in infos_of(orchestrator) if "Pausing snapd auto-refresh" in line]
        assert announcements
        for line in announcements:
            assert "whole run" in line
            assert "orchestrator" in line
            assert "snap_sync" in line
            assert "folder_sync" in line

    @pytest.mark.asyncio
    async def test_capture_is_read_only_and_precedes_the_set(self) -> None:
        """E73 — the existing policy is read, read-only, before anything is written."""
        orchestrator, source, _target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        cmds = all_calls(source)
        get_idx = next(i for i, c in enumerate(cmds) if "snap get system refresh.hold" in c)
        set_idx = next(i for i, c in enumerate(cmds) if "snap set system refresh.hold=" in c)
        assert get_idx < set_idx

    @pytest.mark.asyncio
    async def test_hold_not_set_when_snap_sync_disabled(self) -> None:
        """E92, K22 — nothing is suspended when `snap_sync` is not enabled."""
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=False)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert not any("refresh.hold" in c for c in all_calls(source))
        assert not any("refresh.hold" in c for c in all_calls(target))
        assert orchestrator._snap_hold_engaged is False  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_hold_skipped_in_dry_run(self) -> None:
        """E91 — nothing is suspended in a dry run."""
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True, dry_run=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert not any("refresh.hold" in c for c in all_calls(source))
        assert not any("refresh.hold" in c for c in all_calls(target))
        assert orchestrator._snap_hold_engaged is False  # pyright: ignore[reportPrivateUsage]


class TestCaptureIsPrivileged:
    """snapd admin-gates READING snap config (`/v2/snaps/{name}/conf` sits behind
    `io.snapcraft.snapd.manage-configuration`, `auth_admin_keep` in the shipped polkit
    policy), so an unprivileged `snap get system refresh.hold` does not report "no hold" —
    it exits non-zero with "access denied".

    Drop the sudo and nothing breaks loudly: the capture returns None on BOTH hosts every
    run, and cleanup then writes `refresh.hold=""`, destroying whatever hold the user had
    set (including `forever`) on both machines, silently. That is why the privilege is
    asserted here rather than left to the docstring.
    """

    @pytest.mark.asyncio
    async def test_capture_reads_under_sudo_on_both_hosts(self) -> None:
        """E74 — the read is privileged, so a permission failure cannot read as "no policy"."""
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            reads = [c for c in all_calls(ex) if "snap get system refresh.hold" in c]
            assert reads, "the prior refresh.hold was never read"
            assert all(c.startswith("sudo ") for c in reads), f"unprivileged refresh.hold read: {reads}"

    @pytest.mark.asyncio
    async def test_a_denied_read_is_not_reported_as_no_hold(self) -> None:
        """E75 — an "access denied" read must not collapse into the same None a genuinely unset
        option produces — the two lead to opposite restore behaviour.
        """
        denied = {"snap get system refresh.hold": CommandResult(1, "", HOLD_DENIED_STDERR)}
        unset = {"snap get system refresh.hold": CommandResult(1, "", HOLD_UNSET_STDERR)}
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_responses=denied, target_responses=unset
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert orchestrator._snap_hold_readable_source is False  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._snap_hold_readable_target is True  # pyright: ignore[reportPrivateUsage]


class TestUserHoldIsNeverDestroyed:
    """The end state of a hold the user set themselves, across a full engage/restore cycle
    against a fake that stores the value `snap set` writes (`make_snapd`).

    Asserting the end state, not just the commands: the destructive bug this guards against
    was a capture that could not read, so a test that only checks "the restore wrote the
    captured value back" would have passed while the machine lost its hold.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prior", ["2026-07-24T18:00:00Z", "forever"])
    async def test_a_prior_hold_survives_the_sync_window(self, prior: str) -> None:
        """E76, E77 — a machine's own hold, timed or indefinite, is back exactly as it was."""
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True, source_executor=make_snapd(prior), target_executor=make_snapd(prior)
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        for ex in (source, target):
            assert ex.hold_state["hold"] == APPLIED_HOLD, "the sync-window hold did not replace the prior one"

        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert ex.hold_state["hold"] == prior

    @pytest.mark.asyncio
    async def test_only_a_genuinely_absent_hold_is_cleared(self) -> None:
        """E78 — with no prior hold, the run's own suspension is cleared and nothing else is."""
        orchestrator, source, _target = make_orchestrator(snap_sync_enabled=True, source_executor=make_snapd(None))

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert source.hold_state["hold"] is None
        assert any('snap set system refresh.hold=""' in c for c in all_calls(source))

    @pytest.mark.asyncio
    async def test_an_unreadable_hold_is_never_written_in_the_first_place(self) -> None:
        """E80 — E80, `PKG-FR-SNAP-REFRESH-PAUSE`: a host whose prior policy could not be read is left
        untouched. Declining to CLEAR it at cleanup is not enough — the machine has already
        lost its own policy by then, since a timed hold expires into "no hold at all".
        """
        denied = {"snap get system refresh.hold": CommandResult(1, "", HOLD_DENIED_STDERR)}
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True, source_responses=denied, target_responses=denied
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert not any("snap set system refresh.hold" in c for c in all_calls(ex))
        assert len([w for w in warnings_of(orchestrator) if "Not pausing snapd auto-refresh" in w]) == 2

    @pytest.mark.asyncio
    async def test_the_readable_host_is_still_paused_when_the_other_is_not(self) -> None:
        """E82 — per host, not per run: one unreadable machine must not cost the other its pause."""
        denied = {"snap get system refresh.hold": CommandResult(1, "", HOLD_DENIED_STDERR)}
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True, source_responses=denied, target_executor=make_snapd(None)
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert not any("snap set system refresh.hold" in c for c in all_calls(source))
        assert target.hold_state["hold"] == APPLIED_HOLD

    @pytest.mark.asyncio
    async def test_an_unreadable_hold_is_left_alone_rather_than_cleared(self) -> None:
        """E81 — cleanup's half of the same rule: with no pre-sync value, clearing the option
        would destroy an unknown hold, so it is left as it was found.
        """
        denied = {"snap get system refresh.hold": CommandResult(1, "", HOLD_DENIED_STDERR)}
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True, source_responses=denied, target_responses=denied
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert not any('snap set system refresh.hold=""' in c for c in all_calls(ex))
        assert any("Leaving snapd refresh.hold alone" in w for w in warnings_of(orchestrator))
        assert orchestrator._snap_hold_engaged is False  # pyright: ignore[reportPrivateUsage]


class TestApplyIsVerified:
    """`snap set` exiting 0 says the command ran, not that the option changed — the failure
    mode that hid an unprivileged capture for as long as it lived. The hold is read back.
    """

    @pytest.mark.asyncio
    async def test_no_warning_when_the_hold_took_effect(self) -> None:
        """E87 — everything works: no warning at all."""
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_executor=make_snapd(None), target_executor=make_snapd(None)
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert warnings_of(orchestrator) == []

    @pytest.mark.asyncio
    async def test_warns_when_the_hold_did_not_stick(self) -> None:
        """E84 — `snap set` succeeds, yet the option still reads unset afterwards."""
        never_set = {"snap get system refresh.hold": CommandResult(1, "", HOLD_UNSET_STDERR)}
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_responses=never_set, target_responses=never_set
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        stuck = [w for w in warnings_of(orchestrator) if "auto-refresh is NOT paused" in w]
        assert len(stuck) == 2, f"expected one warning per host, got {warnings_of(orchestrator)}"
        assert all("snap refresh --time" in w for w in stuck)

    @pytest.mark.asyncio
    async def test_warns_when_the_read_back_is_denied(self) -> None:
        """E85 — the capture answers, so the hold IS applied; only the read-back is denied."""

        def _deny_after_the_capture() -> Callable[..., CommandResult]:
            reads = {"n": 0}

            def _side_effect(cmd: str, **_: object) -> CommandResult:
                if "snap get system refresh.hold" in cmd:
                    reads["n"] += 1
                    stderr = HOLD_UNSET_STDERR if reads["n"] == 1 else HOLD_DENIED_STDERR
                    return CommandResult(1, "", stderr)
                return CommandResult(exit_code=0, stdout="", stderr="")

            return _side_effect

        source = MagicMock()
        source.run_command = AsyncMock(side_effect=_deny_after_the_capture())
        target = MagicMock()
        target.run_command = AsyncMock(side_effect=_deny_after_the_capture())
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_executor=source, target_executor=target
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert any("Could not confirm snapd auto-refresh is paused" in w for w in warnings_of(orchestrator))

    @pytest.mark.asyncio
    async def test_a_read_back_that_raises_never_fails_the_sync(self) -> None:
        """E86 — the pause is a race guard; a check on it must not be able to end the run."""
        calls = {"n": 0}

        def _raise_after_the_set(cmd: str, **_: object) -> CommandResult:
            if "snap get system refresh.hold" in cmd:
                calls["n"] += 1
                if calls["n"] > 1:  # the capture succeeds, the read-back explodes
                    raise ConnectionResetError("connection lost")
                return CommandResult(1, "", HOLD_UNSET_STDERR)
            return CommandResult(exit_code=0, stdout="", stderr="")

        source = MagicMock()
        source.run_command = AsyncMock(side_effect=_raise_after_the_set)
        orchestrator, _source, _target = make_orchestrator(snap_sync_enabled=True, source_executor=source)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert any("connection lost" in w for w in warnings_of(orchestrator))
        assert orchestrator._snap_hold_engaged is True  # pyright: ignore[reportPrivateUsage]


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_unsets_when_no_prior_hold(self) -> None:
        """E78 — the cleanup half: with no prior hold, the option is cleared."""
        # `snap get` returns non-zero for an unset option -> no prior hold captured.
        no_hold = {"snap get system refresh.hold": CommandResult(1, "", 'has no "refresh.hold"')}
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True, source_responses=no_hold, target_responses=no_hold
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            assert any('snap set system refresh.hold=""' in c for c in all_calls(ex))
        assert orchestrator._snap_hold_engaged is False  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_restore_preserves_prior_hold_per_host(self) -> None:
        """E79 — a hold the user already set is captured and restored EXACTLY — a timestamp on the
        source, the literal `forever` on the target (decision 4: do not clobber it).
        """
        prior_ts = "2026-07-24T18:00:00Z"
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True,
            source_responses={"snap get system refresh.hold": CommandResult(0, prior_ts + "\n", "")},
            target_responses={"snap get system refresh.hold": CommandResult(0, "forever\n", "")},
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._snap_hold_prior_source == prior_ts  # pyright: ignore[reportPrivateUsage]
        assert orchestrator._snap_hold_prior_target == "forever"  # pyright: ignore[reportPrivateUsage]

        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert any(f"snap set system refresh.hold={prior_ts}" in c for c in all_calls(source))
        assert any("snap set system refresh.hold=forever" in c for c in all_calls(target))
        # The restore must NOT unset (that would clobber the user's prior hold).
        assert not any('snap set system refresh.hold=""' in c for c in all_calls(source))

    @pytest.mark.asyncio
    async def test_restore_is_noop_when_no_hold_engaged(self) -> None:
        """E93 — a run that never engaged the pause issues no command at cleanup."""
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=False)

        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert all_calls(source) == []
        assert all_calls(target) == []

    @pytest.mark.asyncio
    async def test_restore_is_idempotent(self) -> None:
        """E94 — cleanup twice restores once."""
        orchestrator, _source, target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        restore_count = sum(1 for c in all_calls(target) if "snap set system refresh.hold=" in c)
        # A second restore must issue no further commands.
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        assert sum(1 for c in all_calls(target) if "snap set system refresh.hold=" in c) == restore_count


class TestHoldDoesNotBlockConvergence:
    @pytest.mark.asyncio
    async def test_hold_only_writes_refresh_hold_never_a_snap_refresh_command(self) -> None:
        """E90 — the hold writes the auto-refresh gate (`refresh.hold`) only; it issues no
        `snap install/refresh --revision` command, so it cannot interfere with (nor
        substitute for) snap_sync's own manual convergence.
        """
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            for c in all_calls(ex):
                assert "snap install" not in c
                assert "snap refresh" not in c


class TestConfirmEachCommandGate:
    """The snapd hold declares itself a modification on both machines, and an abort at the
    restore is honoured rather than absorbed by the best-effort handler.

    The gate itself lives on the executors (`executor.py`), so what the orchestrator owns —
    and what these assert — is that it passes `mutates=` for the two writes and NOT for the
    read-only capture. That the executor then prompts is covered in `test_step_gate.py`.
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
        return CommandResult(exit_code=0, stdout="", stderr="")

    @pytest.mark.asyncio
    async def test_apply_and_restore_declare_mutations_on_both_hosts(self) -> None:
        """E73, E95, J148, J156 — the pause and the restore are shown as changes; the policy read is not."""
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True)

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        for ex in (source, target):
            declared = self._mutating_calls(ex)
            # Apply plus restore, and nothing else: the `snap get` capture is a read.
            assert len(declared) == 2
            assert all("snap set system refresh.hold" in cmd for cmd, _ in declared)
            assert not any("snap get" in cmd for cmd, _ in declared)

    @pytest.mark.asyncio
    async def test_restore_names_the_prior_value_it_is_writing_back(self) -> None:
        """E96, J157 — skipping the restore loses a hold the user set themselves, so the prompt has to
        say which value is at stake — not just "restore".
        """
        orchestrator, source, _target = make_orchestrator(
            snap_sync_enabled=True,
            source_responses={"snap get system refresh.hold": CommandResult(0, "forever\n", "")},
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        assert any("forever" in mutates for _cmd, mutates in self._mutating_calls(source))

    @pytest.mark.asyncio
    async def test_abort_at_restore_is_not_swallowed_by_the_best_effort_handler(self) -> None:
        """E97, J158 — declining the restore at the gate stops the write rather than being absorbed."""
        orchestrator, source, _target = make_orchestrator(snap_sync_enabled=True)
        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        source.run_command = AsyncMock(side_effect=self._refuse_mutations)
        with pytest.raises(SyncAbortedByUser):
            await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_cleanup_honours_the_abort_but_still_releases_resources(self) -> None:
        """E97 — an abort at the restore stops the write and nothing else: a confirmation prompt
        must never be able to leak the target lock or the SSH connection.
        """
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True)
        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
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


class TestCleanupOrder:
    @pytest.mark.asyncio
    async def test_the_restore_runs_before_the_connection_it_needs_is_torn_down(self) -> None:
        """E99 — the target's restore travels over the SSH connection `_cleanup` also closes.
        Closing first would turn every run's restore into a connection error and leave
        Nomad paused until the timed hold lapsed, so the order is pinned rather than left
        to the position of two statements.
        """
        orchestrator, source, target = make_orchestrator(snap_sync_enabled=True)
        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        order: list[str] = []

        def _record_restore(cmd: str, **_: object) -> CommandResult:
            if "snap set system refresh.hold" in cmd:
                order.append("restore")
            return CommandResult(exit_code=0, stdout="", stderr="")

        for ex in (source, target):
            ex.run_command = AsyncMock(side_effect=_record_restore)
            ex.terminate_all_processes = AsyncMock()
        connection = MagicMock()
        connection.kill_all_remote_processes = AsyncMock()
        connection.disconnect = AsyncMock(side_effect=lambda: order.append("disconnect"))
        orchestrator._connection = connection  # pyright: ignore[reportPrivateUsage]

        await orchestrator._cleanup()  # pyright: ignore[reportPrivateUsage]

        assert order == ["restore", "restore", "disconnect"]


class TestWarningsNameTheMachines:
    """E100, `PKG-FR-NAME-THE-MACHINES`: a warning names the machine it concerns by hostname.

    The end-of-run summary reprints every captured warning on its own, long after the line
    that maps each role to a machine, so "not paused on the target" leaves the reader to
    work out which of their two computers is running unpaused.
    """

    @staticmethod
    def _named(orchestrator: Orchestrator) -> list[str]:
        """Every warning logged, asserted to be free of the two role words."""
        warnings = warnings_of(orchestrator)
        assert warnings, "expected at least one warning"
        for w in warnings:
            assert "source" not in w.lower(), w
            assert "target" not in w.lower(), w
        return warnings

    @pytest.mark.asyncio
    async def test_an_unreadable_hold_names_the_machine_it_leaves_unpaused(self) -> None:
        """E100 — the unreadable-policy warning names the machine it leaves unpaused."""
        denied = {"snap get system refresh.hold": CommandResult(1, "", HOLD_DENIED_STDERR)}
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_responses=denied, target_responses=denied
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"Not pausing snapd auto-refresh on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"Not pausing snapd auto-refresh on {TARGET_MACHINE}" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_failed_pause_names_the_machine(self) -> None:
        """E83, E100 — a pause that failed names the machine, and the run continues."""
        refuses = {"snap set system refresh.hold": CommandResult(1, "", "error: cannot set")}
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_responses=refuses, target_responses=refuses
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"Could not pause snapd auto-refresh on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"Could not pause snapd auto-refresh on {TARGET_MACHINE}" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_hold_that_did_not_stick_names_the_machine(self) -> None:
        """E84, E100 — a pause that did not stick names the machine."""
        never_set = {"snap get system refresh.hold": CommandResult(1, "", HOLD_UNSET_STDERR)}
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_responses=never_set, target_responses=never_set
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"snapd auto-refresh is NOT paused on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"snapd auto-refresh is NOT paused on {TARGET_MACHINE}" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_pause_that_cannot_be_confirmed_names_the_machine(self) -> None:
        """E85, E100 — the capture answers, so the hold is written; only the read-back is denied."""

        def _deny_the_read_back() -> Callable[..., CommandResult]:
            reads = {"n": 0}

            def _side_effect(cmd: str, **_: object) -> CommandResult:
                if "snap get system refresh.hold" in cmd:
                    reads["n"] += 1
                    return CommandResult(1, "", HOLD_UNSET_STDERR if reads["n"] == 1 else HOLD_DENIED_STDERR)
                return CommandResult(exit_code=0, stdout="", stderr="")

            return _side_effect

        source, target = MagicMock(), MagicMock()
        source.run_command = AsyncMock(side_effect=_deny_the_read_back())
        target.run_command = AsyncMock(side_effect=_deny_the_read_back())
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_executor=source, target_executor=target
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"Could not confirm snapd auto-refresh is paused on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"Could not confirm snapd auto-refresh is paused on {TARGET_MACHINE}" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_an_option_left_alone_at_restore_names_the_machine(self) -> None:
        """E100 — the left-alone warning at restore names the machine."""
        denied = {"snap get system refresh.hold": CommandResult(1, "", HOLD_DENIED_STDERR)}
        orchestrator, _source, _target = make_orchestrator(
            snap_sync_enabled=True, source_responses=denied, target_responses=denied
        )

        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"Leaving snapd refresh.hold alone on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"Leaving snapd refresh.hold alone on {TARGET_MACHINE}" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_failed_restore_names_the_machine(self) -> None:
        """E98, E100 — a restore command that failed names the machine."""
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True, source_executor=make_snapd(None), target_executor=make_snapd(None)
        )
        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        # The restore is the next write on each host, and it fails on both.
        refused = CommandResult(1, "", "error: cannot set")
        for ex in (source, target):
            ex.run_command = AsyncMock(return_value=refused)

        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"Could not restore snapd refresh.hold on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"Could not restore snapd refresh.hold on {TARGET_MACHINE}" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_a_restore_that_raises_names_the_machine(self) -> None:
        """E98, E100 — a restore whose connection is already gone names the machine."""
        orchestrator, source, target = make_orchestrator(
            snap_sync_enabled=True, source_executor=make_snapd(None), target_executor=make_snapd(None)
        )
        await orchestrator._hold_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]
        for ex in (source, target):
            ex.run_command = AsyncMock(side_effect=ConnectionResetError("connection lost"))

        await orchestrator._restore_snap_autorefresh()  # pyright: ignore[reportPrivateUsage]

        warnings = self._named(orchestrator)
        assert any(f"Error restoring snapd refresh.hold on {SOURCE_MACHINE}" in w for w in warnings)
        assert any(f"Error restoring snapd refresh.hold on {TARGET_MACHINE}" in w for w in warnings)
