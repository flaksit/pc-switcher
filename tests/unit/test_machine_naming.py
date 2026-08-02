"""`PKG-FR-NAME-THE-MACHINES`: nothing the user reads calls a machine by its role.

Source and target are roles this run assigns; the user's computers have hostnames. A line
saying "the target loses this package" makes the reader work out which machine that is
before they can answer, so every question, answer, warning and summary line names the
machine outright.

Two things the article puts outside the rule. A log record, which carries the machine it
concerns as a field of its own, is asserted where the records are — over one package job's
`apply()` in `tests/unit/jobs/test_package_sync_core.py`. A validation failure, which ends
the run before there is anything to decide, is asserted below: the exemption is one this
code takes, not merely one it is granted.

One test per CLASS of place the words can reach the user, not one per string. Each would
fail if a role word came back anywhere in its class.
"""

from __future__ import annotations

import ast
import io
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

import pcswitcher
from pcswitcher.cli import app
from pcswitcher.config import Configuration
from pcswitcher.config_sync import (
    ConfigSyncAction,
    _prompt_config_diff,  # pyright: ignore[reportPrivateUsage]
    _prompt_new_config,  # pyright: ignore[reportPrivateUsage]
)
from pcswitcher.confirmer import TerminalUIConfirmer
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.context import JobContext
from pcswitcher.jobs.packages.probes import ProbeFailed, require_answer
from pcswitcher.models import CommandResult, Host
from pcswitcher.orchestrator import Orchestrator

_SRC = Path(pcswitcher.__file__).parent

ATLAS = "Atlas"
NOMAD = "Nomad"
VEGA = "Vega"

# The words themselves. Matched case-insensitively on the whole word so "sources.list",
# "resource" and "targeted" do not register, but "the target" and "on source" do.
_ROLE_WORDS = ("source", "target")

# The executor methods a `mutates=` phrase can be attached to.
_EXECUTOR_METHODS = frozenset({"run_command", "start_process", "send_file", "get_file", "declare_modification"})


def _role_words_in(text: str) -> list[str]:
    """Every whole-word occurrence of a role word in `text`."""
    words = [word.strip(".,;:!?'\"()[]").lower() for word in text.split()]
    return [word for word in words if word in _ROLE_WORDS or word in {f"{w}'s" for w in _ROLE_WORDS}]


def _assert_names_machines(text: str, *hostnames: str) -> None:
    """`text` names each hostname and no role."""
    for hostname in hostnames:
        assert hostname in text, f"expected {hostname!r} in {text!r}"
    assert not _role_words_in(text), f"role word in text the user reads: {text!r}"


def _literal_arguments(callee_names: frozenset[str], keyword: str | None = None) -> list[tuple[str, int, str]]:
    """`(relpath, lineno, literal text)` for every call to one of `callee_names` in `src/`.

    With `keyword`, only that keyword argument is read; otherwise the positional ones are.
    f-strings contribute their literal parts only — an interpolated hostname is not a role.
    """
    found: list[tuple[str, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name not in callee_names:
                continue
            if keyword is None:
                sources: list[ast.expr] = list(node.args)
            else:
                sources = [kw.value for kw in node.keywords if kw.arg == keyword]
            literals = [
                n.value
                for a in sources
                for n in ast.walk(a)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            if literals:
                found.append((path.relative_to(_SRC).as_posix(), node.lineno, " ".join(literals)))
    return found


class TestMutatesPhrases:
    """Every `mutates=` phrase, which the per-command confirmation shows at its question.

    The gate's heading already names the machine by hostname, so a phrase does not have to
    repeat it — but it must not name the ROLE instead.
    """

    @staticmethod
    def _phrases() -> list[tuple[str, int, str]]:
        return _literal_arguments(_EXECUTOR_METHODS, keyword="mutates")

    def test_the_audit_sees_the_real_call_sites(self) -> None:
        """Binds the scan to the source, so a broken scan cannot pass by finding nothing."""
        phrases = self._phrases()
        assert len(phrases) > 40
        assert any("install apt package" in text for _, _, text in phrases)

    def test_no_mutates_phrase_names_a_role(self) -> None:
        """H72."""
        offenders = [(rel, line, text) for rel, line, text in self._phrases() if _role_words_in(text)]
        assert not offenders, f"`mutates=` phrases naming a role: {offenders}"


class TestStepLabels:
    """The step label the status bar shows while each sync step runs."""

    @staticmethod
    def _labels() -> list[tuple[str, int, str]]:
        return _literal_arguments(frozenset({"set_current_step"}))

    def test_the_audit_sees_the_real_call_sites(self) -> None:
        labels = self._labels()
        assert any("Discover jobs" in text for _, _, text in labels)

    def test_no_step_label_names_a_role(self) -> None:
        """H73."""
        offenders = [entry for entry in self._labels() if _role_words_in(entry[2])]
        assert not offenders, f"step labels naming a role: {offenders}"


class TestOutcomeMessages:
    """The exception messages the run's final summary prints: what failed, what was
    skipped, and where the user stopped it."""

    _TYPES = frozenset(
        {
            "ConvergeItemDeclined",
            "ConvergeItemFailed",
            "JobSkipped",
            "ProbeFailed",
            "SyncAborted",
            "SyncAbortedByUser",
            "SyncLockedError",
        }
    )

    @staticmethod
    def _messages() -> list[tuple[str, int, str]]:
        return _literal_arguments(TestOutcomeMessages._TYPES)

    def test_the_audit_sees_the_real_call_sites(self) -> None:
        messages = self._messages()
        assert len(messages) > 30
        assert any("no enabled folders configured" in text for _, _, text in messages)

    def test_no_outcome_message_names_a_role(self) -> None:
        """H74."""
        offenders = [entry for entry in self._messages() if _role_words_in(entry[2])]
        assert not offenders, f"outcome messages naming a role: {offenders}"


class TestProbeFailure:
    """`require_answer`'s message, which reaches the user through the failure summary."""

    def test_the_message_names_the_machine(self) -> None:
        """H75, J96."""
        with pytest.raises(ProbeFailed) as excinfo:
            require_answer("snap list --all", CommandResult(1, "", "cannot communicate with server"), NOMAD)

        _assert_names_machines(str(excinfo.value), NOMAD)


class TestValidationFailure:
    """The one place role words still reach the user, and the article says they may.

    A validation failure ends the run before a single question is put, so nobody is left
    working out which machine an answer would act on. The exemption is exercised rather
    than merely granted: the message says "on target" and the orchestrator prints it as
    `- apt_sync (target): …`, so no hostname reaches the reader even though the job holds
    both. Asserted so that the row claiming an exemption is not read as coverage of the
    stricter rule.
    """

    @staticmethod
    def _job_whose_target_lacks_sudo() -> AptSyncJob:
        def target_answer(command: str, **_: object) -> CommandResult:
            if "sudo --non-interactive true" in command:
                return CommandResult(1, "", "sudo: a password is required")
            # fuser exits non-zero when the dpkg lock is FREE, so this leaves sudo the
            # only complaint.
            return CommandResult(1 if "fuser" in command else 0, "", "")

        source = MagicMock()
        source.run_command = AsyncMock(return_value=CommandResult(0, "", ""))
        target = MagicMock()
        target.run_command = AsyncMock(side_effect=target_answer)
        return AptSyncJob(
            JobContext(
                config={},
                source=source,
                target=target,
                event_bus=MagicMock(),
                session_id="test-1234",
                source_hostname=ATLAS,
                target_hostname=NOMAD,
            )
        )

    @pytest.mark.asyncio
    async def test_a_package_jobs_sudo_failure_names_the_role_and_no_hostname(self) -> None:
        """H80."""
        errors = await self._job_whose_target_lacks_sudo().validate()

        [sudo_error] = [error for error in errors if "sudo" in error.message]
        assert sudo_error.host is Host.TARGET
        assert "target" in _role_words_in(sudo_error.message)
        assert NOMAD not in sudo_error.message
        assert ATLAS not in sudo_error.message


def _orchestrator(config: MagicMock, *, remote_stdout: str = "", dry_run: bool = False) -> Orchestrator:
    orchestrator = Orchestrator(target=NOMAD, config=config, dry_run=dry_run)
    orchestrator._console = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._ui = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._logger = MagicMock()  # pyright: ignore[reportPrivateUsage]
    orchestrator._source_hostname = ATLAS  # pyright: ignore[reportPrivateUsage]
    orchestrator._confirmer = TerminalUIConfirmer(  # pyright: ignore[reportPrivateUsage]
        orchestrator._console,  # pyright: ignore[reportPrivateUsage, reportArgumentType]
        orchestrator._ui,  # pyright: ignore[reportPrivateUsage, reportArgumentType]
        logger=orchestrator._logger,  # pyright: ignore[reportPrivateUsage]
    )
    executor = AsyncMock()
    executor.run_command.return_value = CommandResult(exit_code=0, stdout=remote_stdout, stderr="")
    orchestrator._remote_executor = executor  # pyright: ignore[reportPrivateUsage]
    return orchestrator


@pytest.fixture
def config() -> MagicMock:
    cfg = MagicMock(spec=Configuration)
    cfg.logging = MagicMock()
    cfg.sync_jobs = {}
    cfg.job_configs = {}
    return cfg


class TestOrchestratorQuestions:
    """The two coarse questions the orchestrator asks before any job runs."""

    @staticmethod
    async def _ask(orchestrator: Orchestrator) -> tuple[str, str]:
        """Run the pre-flight check and return the `(title, message)` it would show."""
        asked: dict[str, str] = {}

        async def record(*, title: str, message: str, **_: Any) -> bool:
            asked["title"] = title
            asked["message"] = message
            return False

        confirmer = MagicMock()
        confirmer.confirm = record
        orchestrator._confirmer = confirmer  # pyright: ignore[reportPrivateUsage]
        await orchestrator._check_out_of_order()  # pyright: ignore[reportPrivateUsage]
        return asked["title"], asked["message"]

    @pytest.mark.asyncio
    async def test_first_sync_question_names_both_machines(
        self, config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """H76 — No readable history on the other machine — the overwrite question."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        title, message = await self._ask(_orchestrator(config, remote_stdout=""))

        _assert_names_machines(title, NOMAD)
        _assert_names_machines(message, NOMAD)

    @pytest.mark.asyncio
    async def test_third_machine_question_names_every_machine(
        self, config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other machine last synced with a third one — all three are named."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        history = json.dumps({"last_role": "target", "last_peer": VEGA})
        title, message = await self._ask(_orchestrator(config, remote_stdout=history))

        _assert_names_machines(title, NOMAD)
        _assert_names_machines(message, ATLAS, NOMAD, VEGA)

    @pytest.mark.asyncio
    async def test_consecutive_sync_question_names_both_machines(
        self, config: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repeat push with no sync back — the same rule applies."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        local_history = tmp_path / ".local/share/pc-switcher/sync-history.json"
        local_history.parent.mkdir(parents=True, exist_ok=True)
        local_history.write_text(json.dumps({"last_role": "source", "last_peer": NOMAD}))

        history = json.dumps({"last_role": "target", "last_peer": ATLAS})
        title, message = await self._ask(_orchestrator(config, remote_stdout=history))

        assert not _role_words_in(title), title
        _assert_names_machines(message, ATLAS, NOMAD)


class TestConfigSyncQuestions:
    """The config-sync screens, the only question asked before the jobs start."""

    @staticmethod
    def _console() -> tuple[Console, io.StringIO]:
        """A real console, not a mock: the machine names live inside Rich panels, which a
        mock records as object addresses."""
        sink = io.StringIO()
        return Console(file=sink, width=200, no_color=True, legacy_windows=False), sink

    def test_new_config_question_names_both_machines(self) -> None:
        """H77."""
        console, sink = self._console()
        with patch("pcswitcher.config_sync.Prompt.ask", return_value="n"):
            _prompt_new_config(console, "log_level: INFO", ATLAS, NOMAD)

        _assert_names_machines(sink.getvalue(), ATLAS, NOMAD)

    def test_differing_config_answers_name_both_machines(self) -> None:
        console, sink = self._console()
        with patch("pcswitcher.config_sync.Prompt.ask", return_value="x") as ask:
            assert _prompt_config_diff(console, "one line\n", ATLAS, NOMAD) == ConfigSyncAction.ABORT

        _assert_names_machines(sink.getvalue(), ATLAS, NOMAD)
        assert not _role_words_in(str(ask.call_args))


class TestCommandLineHelp:
    """`--help` is read before there is a run, so no hostname exists — but no role word
    may stand in for one either."""

    def test_sync_help_names_no_role(self) -> None:
        """H78."""
        result = CliRunner().invoke(app, ["sync", "--help"])
        assert result.exit_code == 0
        assert not _role_words_in(result.output), result.output
