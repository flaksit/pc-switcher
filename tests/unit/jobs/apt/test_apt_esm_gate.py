"""The Ubuntu Pro question, its two answers, and what neither of them leaks (`PKG-FR-DISTRO-FILES`).

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Sequence
from typing import override
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.executor import RemoteExecutor
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.apt_sync.esm_gate import PRO_STATUS_COMMAND
from pcswitcher.jobs.apt_sync.probe import AptProbe
from pcswitcher.jobs.packages.review import (
    Decision,
)
from pcswitcher.models import CommandResult, JobSkipped
from tests.unit.jobs.apt.helpers import (
    _NO_PACKAGES,
    MACHINES,
    _repo_context,
    all_calls,
    sha256_line,
)
from tests.unit.jobs.test_package_sync_core import FakeReviewer

_ESM_APPS = "ubuntu-esm-apps.sources"
_ESM_INFRA = "ubuntu-esm-infra.sources"
_PRO_ACCOUNT_EMAIL = "subscriber-9f3a@example.invalid"
_PRO_ACCOUNT = f'"account": {{"id": "aAbBcC", "name": "{_PRO_ACCOUNT_EMAIL}"}}'
_PRO_UNATTACHED = f'{{"attached": false, {_PRO_ACCOUNT}, "services": []}}'
_PRO_ATTACHED = f'{{"attached": true, {_PRO_ACCOUNT}, "services": [{{"name": "esm-apps"}}]}}'


class _GateReviewer(FakeReviewer):
    """`FakeReviewer` that also records what the TARGET had been asked at the moment each
    gate question was put — which is how "before the first mutating command" is measured
    rather than assumed.
    """

    def __init__(self, target: MagicMock, decisions: dict[str, Decision] | None = None) -> None:
        super().__init__(decisions)
        self._target = target
        self.target_calls_at_gate: list[list[str]] = []

    @override
    async def ask_gate(self, *, title: str, message: str, proceed_label: str, stop_label: str) -> bool | None:
        self.target_calls_at_gate.append(_mutating_calls(self._target))
        return await super().ask_gate(title=title, message=message, proceed_label=proceed_label, stop_label=stop_label)


def _mutating_calls(target: MagicMock) -> list[str]:
    """Every target command that declared itself a write. `mutates=` is mandatory on writes
    (`tests/unit/test_mutates_audit.py`), so this IS the set of changes the job made.
    """
    return [call.args[0] for call in target.run_command.call_args_list if call.kwargs.get("mutates") is not None] + [
        str(call.args[1]) for call in target.send_file.call_args_list if call.kwargs.get("mutates") is not None
    ]


def _esm_job(
    *,
    pro_status: CommandResult | list[CommandResult],
    gate_answers: Sequence[bool | None] = (),
    source_esm: Sequence[str] = (_ESM_APPS, _ESM_INFRA),
    dry_run: bool = False,
) -> tuple[AptSyncJob, MagicMock, _GateReviewer]:
    """A source carrying `source_esm` beside `ubuntu.sources`, and a target that has none of
    them and answers `pro status` with `pro_status` (a list is consumed one probe at a time,
    the last entry repeating).

    Every other target command succeeds, `apt-get update` included: an unattached machine is
    NOT a broken one, and a fixture that failed the refresh would let a wrong implementation
    pass for the wrong reason.
    """
    probes = pro_status if isinstance(pro_status, list) else [pro_status]
    state = {"probes": 0}

    def _target(cmd: str, **_: object) -> CommandResult:
        if cmd == PRO_STATUS_COMMAND:
            index = min(state["probes"], len(probes) - 1)
            state["probes"] += 1
            return probes[index]
        if "apt-mark showmanual" in cmd:
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    source_listing = sha256_line("d0", "ubuntu.sources") + "".join(
        sha256_line(f"e{n}", name) for n, name in enumerate(source_esm)
    )
    context, _source, target = _repo_context(
        source_responses={**_NO_PACKAGES, "find /etc/apt/sources.list.d": CommandResult(0, source_listing, "")},
        target_side_effect=_target,
        dry_run=dry_run,
    )
    job = AptSyncJob(context)
    reviewer = _GateReviewer(target)
    reviewer.gate_answers = list(gate_answers)
    job.context = dataclasses.replace(job.context, reviewer=reviewer)
    return job, target, reviewer


def _pro_probe_count(target: MagicMock) -> int:
    return sum(1 for cmd in all_calls(target) if cmd == PRO_STATUS_COMMAND)


def _promoted_files(target: MagicMock) -> list[str]:
    return [c.rsplit(" ", 1)[1] for c in all_calls(target) if c.startswith("sudo install --owner=root")]


class TestTheESMAttachmentGate:
    """`PKG-FR-DISTRO-FILES`: the two `ubuntu-esm-*` sources are the one always-sync bucket that waits on a
    fact about the TARGET. Writing them to a machine with no Pro attachment leaves an apt
    whose next install of an ESM-covered package fails with a 401 nobody traces back to the
    sync — and pc-switcher cannot attach the machine itself, because `pro attach` needs a
    dashboard token or a browser short-code flow.
    """

    @pytest.mark.asyncio
    async def test_an_unattached_target_is_asked_about_before_anything_is_written(self) -> None:
        """C127, C128, H18, H54 — the question precedes the job's first write and every other apt
        question, and it names both files, both commands and the tutorial.
        """
        job, _target, reviewer = _esm_job(
            pro_status=[CommandResult(0, _PRO_UNATTACHED, ""), CommandResult(0, _PRO_ATTACHED, "")],
            gate_answers=[True],
        )

        await job.execute()

        assert len(reviewer.gate_calls) == 1
        message = reviewer.gate_calls[0]["message"]
        assert _ESM_APPS in message
        assert _ESM_INFRA in message
        # The gate asks the user to go and attach the other machine, so the message must
        # carry both the commands and the link that outlives them if Ubuntu changes the flow.
        assert "pro attach" in message
        assert "pro enable esm-apps esm-infra" in message
        assert "https://documentation.ubuntu.com/pro/attach-tutorial/" in message
        assert reviewer.target_calls_at_gate == [[]], "the gate must precede the job's first write"

    @pytest.mark.asyncio
    async def test_the_gate_offers_exactly_two_answers_and_names_both_of_them(self) -> None:
        """C129 — a title naming the target, and exactly the two answers."""
        job, _target, reviewer = _esm_job(
            pro_status=[CommandResult(0, _PRO_UNATTACHED, ""), CommandResult(0, _PRO_ATTACHED, "")],
            gate_answers=[True],
        )

        await job.execute()

        call = reviewer.gate_calls[0]
        assert call["proceed_label"] == "I have attached target-host — check again and continue"
        assert call["stop_label"] == "Skip Apt packages this run (every other job still runs)"
        assert call["title"] == "target-host needs an Ubuntu Pro attachment"

    @pytest.mark.asyncio
    async def test_choosing_skip_raises_job_skipped_and_writes_nothing(self) -> None:
        """C132, C133, C134, J10 — skipping withholds the WHOLE job: no review, and not one write."""
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_UNATTACHED, ""), gate_answers=[False])

        with pytest.raises(JobSkipped) as excinfo:
            await job.execute()

        assert excinfo.value.job_name == "apt_sync"
        assert _mutating_calls(target) == []
        assert reviewer.groups_seen is None, "no review may be presented for a job that is about to skip"

    @pytest.mark.asyncio
    async def test_a_non_interactive_run_skips_the_whole_job(self) -> None:
        """C135, J11 — the user's ruling, replacing an earlier fallback that withheld only the two files:
        `/etc/apt/preferences.d` always-syncs with no derivation predicate, so the source's
        ESM pins would land on a target without the sources they name.
        """
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_UNATTACHED, ""), gate_answers=[None])

        with pytest.raises(JobSkipped) as excinfo:
            await job.execute()

        assert excinfo.value.job_name == "apt_sync"
        assert _ESM_APPS in excinfo.value.reason
        assert _ESM_INFRA in excinfo.value.reason
        assert "no TTY" in excinfo.value.reason
        assert _mutating_calls(target) == []
        assert reviewer.groups_seen is None

    @pytest.mark.asyncio
    async def test_attach_now_re_probes_and_continues_when_the_target_became_attached(self) -> None:
        """C130 — the claim is re-probed rather than believed, and the sources then land."""
        job, target, _reviewer = _esm_job(
            pro_status=[CommandResult(0, _PRO_UNATTACHED, ""), CommandResult(0, _PRO_ATTACHED, "")],
            gate_answers=[True],
        )

        await job.execute()

        assert _pro_probe_count(target) == 2, "the answer is re-checked against the machine, never trusted"
        promoted = _promoted_files(target)
        assert f"/etc/apt/sources.list.d/{_ESM_APPS}" in promoted
        assert f"/etc/apt/sources.list.d/{_ESM_INFRA}" in promoted

    @pytest.mark.asyncio
    async def test_attach_now_can_be_answered_any_number_of_times(self) -> None:
        """C131 — unbounded by the user's ruling: re-probing costs nothing and the exit is skip."""
        attempts = 10
        job, target, reviewer = _esm_job(
            pro_status=CommandResult(0, _PRO_UNATTACHED, ""),
            gate_answers=[*([True] * attempts), False],
        )

        with pytest.raises(JobSkipped):
            await job.execute()

        assert reviewer.gate_answers == [], "every answer must be consumed — no bound may cut the loop short"
        assert len(reviewer.gate_calls) == attempts + 1
        assert _pro_probe_count(target) == attempts + 1

    @pytest.mark.asyncio
    async def test_esm_sources_are_written_to_an_attached_target(self, caplog: pytest.LogCaptureFixture) -> None:
        """C137, N18 — an attached target is asked nothing, probed once, and written to without
        a warning."""
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_ATTACHED, ""))

        with caplog.at_level(1):
            await job.execute()

        assert reviewer.gate_calls == []
        assert [r for r in caplog.records if r.levelno >= 30] == []
        assert _pro_probe_count(target) == 1
        promoted = _promoted_files(target)
        assert f"/etc/apt/sources.list.d/{_ESM_APPS}" in promoted
        assert f"/etc/apt/sources.list.d/{_ESM_INFRA}" in promoted

    @pytest.mark.asyncio
    async def test_a_source_with_no_esm_sources_never_probes_at_all(self) -> None:
        """C138 — the trigger is a pending write, not the target's Pro state: a source with no ESM
        files has nothing to gate, so the run costs no probe and asks no question.
        """
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_UNATTACHED, ""), source_esm=())

        await job.execute()

        assert _pro_probe_count(target) == 0
        assert reviewer.gate_calls == []

    @pytest.mark.asyncio
    async def test_an_esm_file_the_target_already_matches_is_not_gated(self) -> None:
        """C139 — nothing to write is nothing to ask about. The target holds the same bytes, so the
        always-sync bucket skips the file and the gate must skip the question.
        """
        listing = sha256_line("d0", "ubuntu.sources") + sha256_line("e0", _ESM_APPS)

        def _target(cmd: str, **_: object) -> CommandResult:
            if cmd == PRO_STATUS_COMMAND:
                return CommandResult(0, _PRO_UNATTACHED, "")
            if "find /etc/apt/sources.list.d" in cmd:
                return CommandResult(0, listing, "")
            return CommandResult(0, "", "")

        context, _source, target = _repo_context(
            source_responses={**_NO_PACKAGES, "find /etc/apt/sources.list.d": CommandResult(0, listing, "")},
            target_side_effect=_target,
        )
        job = AptSyncJob(context)
        reviewer = _GateReviewer(target)
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert _pro_probe_count(target) == 0
        assert reviewer.gate_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "probe",
        [
            pytest.param(CommandResult(127, "", "pro: command not found\n"), id="no-pro-binary"),
            pytest.param(CommandResult(1, "", "Failed to access contract server\n"), id="non-zero-exit"),
            pytest.param(CommandResult(0, "attached: no\n", ""), id="not-json"),
            pytest.param(CommandResult(0, '["attached"]', ""), id="json-but-not-an-object"),
            pytest.param(CommandResult(0, '{"services": []}', ""), id="no-attached-key"),
        ],
    )
    async def test_an_unreadable_pro_probe_is_treated_as_unattached(self, probe: CommandResult) -> None:
        """C140 — False asks a question the user can answer; True writes files that break the
        target's next install. The recoverable answer is the default (`PKG-FR-READ-FAILS-JOB`).
        """
        job, target, reviewer = _esm_job(pro_status=probe, gate_answers=[False])

        with pytest.raises(JobSkipped):
            await job.execute()

        assert len(reviewer.gate_calls) == 1
        assert _mutating_calls(target) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [_PRO_UNATTACHED, _PRO_ATTACHED])
    async def test_the_probe_payload_is_never_logged(self, payload: str, caplog: pytest.LogCaptureFixture) -> None:
        """C141 — `pro status` names the subscriber. Only the parsed boolean may leave the probe."""
        job, _target, reviewer = _esm_job(pro_status=CommandResult(0, payload, ""), gate_answers=[False])

        with caplog.at_level(1), contextlib.suppress(JobSkipped):
            await job.execute()

        assert _PRO_ACCOUNT_EMAIL not in caplog.text
        assert "aAbBcC" not in caplog.text
        for call in reviewer.gate_calls:
            assert _PRO_ACCOUNT_EMAIL not in "".join(call.values())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [_PRO_UNATTACHED, _PRO_ATTACHED])
    async def test_the_probe_payload_never_reaches_the_executors_own_trace(
        self, payload: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """J115, C141 — through the REAL `RemoteExecutor`, which traces every command's stdout
        verbatim at DEBUG (`PKG-FR-LOG-VERBATIM`): a mock executor logs nothing, so it can
        only prove that `esm_gate` itself stays quiet, and the payload was reaching the log
        file one layer below it. The withholding has to happen at the read
        (`PKG-FR-ESM-PRIVACY`) — by the time a filter downstream could see it, it is written.
        """
        conn = MagicMock()
        conn.run = AsyncMock(return_value=MagicMock(exit_status=0, stdout=payload, stderr=""))
        probe = AptProbe(MagicMock(), RemoteExecutor(conn), MACHINES)  # pyright: ignore[reportArgumentType]

        with caplog.at_level(1):
            result = await probe.target_pro_attached(PRO_STATUS_COMMAND)

        assert result.stdout == payload, "the caller still gets the payload — only the log is denied it"
        assert _PRO_ACCOUNT_EMAIL not in caplog.text
        assert "aAbBcC" not in caplog.text
        assert PRO_STATUS_COMMAND in caplog.text, "the command itself is still traced"
        assert "PKG-FR-ESM-PRIVACY" in caplog.text, "and the trace says why the answer is not there"

    @pytest.mark.asyncio
    async def test_a_dry_run_never_prompts_about_attachment(self, caplog: pytest.LogCaptureFixture) -> None:
        """C136, J61 — a rehearsal must not make the user go and attach a machine, and ADR-014 makes the
        preview the whole report — so it has to say the real run would skip the job, not just
        that two files would be held back.
        """
        job, target, reviewer = _esm_job(pro_status=CommandResult(0, _PRO_UNATTACHED, ""), dry_run=True)

        with caplog.at_level(1):
            await job.execute()

        assert reviewer.gate_calls == []
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= 30]
        assert len(warnings) == 1
        assert _ESM_APPS in warnings[0]
        assert _ESM_INFRA in warnings[0]
        assert "skip Apt packages entirely" in warnings[0]
        assert _ESM_APPS not in "".join(
            r.getMessage() for r in caplog.records if "[dry-run] Would write" in r.getMessage()
        )
        assert _mutating_calls(target) == []
