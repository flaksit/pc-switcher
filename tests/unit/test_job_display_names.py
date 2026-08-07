"""A job is spelled two ways, and the two must not be swapped (#280).

`Job.name` is an identifier: the `sync_jobs` config key, the module a job is imported
from (`CORE-FR-JOB-LOAD`), the `job` field of every log record (`LOG-FR-JSON`).
`Job.display_name` is the same job worded for a human, and is what the status line, the
progress bars and the `Job outcomes:` block show.

These tests pin the split in both directions: prose that leaks an identifier is the
defect this fixed, and a config key or log field that starts following the wording would
break configs and log queries the next time a name is improved.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import ClassVar, cast
from unittest.mock import MagicMock

import pytest

import pcswitcher.jobs as jobs_pkg
from pcswitcher.jobs.base import Job, SyncJob
from pcswitcher.models import Host, JobStatus, LogLevel, ValidationError
from pcswitcher.orchestrator import Orchestrator
from tests.unit.console_capture import captured_console
from tests.unit.jobs.test_package_sync_core import make_context

# The two fixtures exist to be executed by tests, not to be read by a user, so they are
# the one case allowed to fall back to the identifier.
_FIXTURE_MODULES = frozenset({"pcswitcher.jobs.dummy_success", "pcswitcher.jobs.dummy_fail"})


def _shipped_job_classes() -> list[type[Job]]:
    """Every concrete Job class that ships, in name order."""
    found: dict[str, type[Job]] = {}
    for module_info in pkgutil.walk_packages(jobs_pkg.__path__, f"{jobs_pkg.__name__}."):
        if module_info.name in _FIXTURE_MODULES:
            continue
        module = importlib.import_module(module_info.name)
        for attr in vars(module).values():
            if isinstance(attr, type) and issubclass(attr, Job) and "name" in attr.__dict__:
                found[attr.name] = attr
    return [found[key] for key in sorted(found)]


class _NamedJob(SyncJob):
    """A job whose two spellings share no characters, so neither can pass for the other."""

    name: ClassVar[str] = "stub_display_name"
    display_name: ClassVar[str] = "Widget herding"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        self._log(Host.SOURCE, LogLevel.INFO, "herding widgets")


class _UnnamedJob(SyncJob):
    name: ClassVar[str] = "stub_no_display_name"

    async def validate(self) -> list[ValidationError]:
        return []

    async def execute(self) -> None:
        return None


class TestEveryShippedJobIsNamedForAHuman:
    @pytest.mark.parametrize("job_class", _shipped_job_classes(), ids=lambda c: c.name)
    def test_a_shipped_job_declares_prose_a_user_recognises(self, job_class: type[Job]) -> None:
        """The defect was module names on screen, so no shipped job may fall back."""
        assert job_class.display_name != job_class.name
        assert "_" not in job_class.display_name, "a display name is prose, not an identifier"
        assert job_class.display_name[:1].isupper()

    def test_the_audit_sees_the_real_jobs(self) -> None:
        """Guard: an import or filter mistake would silently leave the parametrize empty."""
        assert {c.name for c in _shipped_job_classes()} >= {"apt_sync", "folder_sync", "vscode_state_sync"}

    def test_a_job_declaring_no_display_name_answers_its_identifier(self) -> None:
        assert _UnnamedJob.display_name == "stub_no_display_name"


class TestTheIdentifierStaysTheIdentifier:
    @pytest.mark.parametrize("job_class", _shipped_job_classes(), ids=lambda c: c.name)
    def test_the_config_key_still_resolves_the_class(
        self, job_class: type[Job], wired_orchestrator: Orchestrator
    ) -> None:
        """`sync_jobs` keys and module paths are `name`; the display name resolves nothing."""
        if not issubclass(job_class, SyncJob):
            pytest.skip("not config-driven")
        resolve = wired_orchestrator._resolve_sync_job_class  # pyright: ignore[reportPrivateUsage]

        assert resolve(job_class.name) is job_class
        assert resolve(job_class.display_name) is None

    def test_a_jobs_log_records_carry_the_identifier(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            _NamedJob(make_context())._log(Host.SOURCE, LogLevel.INFO, "herding widgets")  # pyright: ignore[reportPrivateUsage]

        record = next(r for r in caplog.records if r.getMessage() == "herding widgets")
        assert record.job == "stub_display_name"  # pyright: ignore[reportAttributeAccessIssue]


class TestWhatTheUserReads:
    @pytest.mark.asyncio
    async def test_the_status_line_and_the_outcome_block_show_the_display_name(
        self, wired_orchestrator: Orchestrator
    ) -> None:
        console, buffer = captured_console()
        wired_orchestrator._console = console  # pyright: ignore[reportPrivateUsage]

        results = await wired_orchestrator._execute_jobs([_NamedJob(make_context())])  # pyright: ignore[reportPrivateUsage]
        wired_orchestrator._job_results = results  # pyright: ignore[reportPrivateUsage]
        wired_orchestrator._print_job_summary()  # pyright: ignore[reportPrivateUsage]

        # The result carries both spellings, so neither reader has to look the other up.
        assert results[0].job_name == "stub_display_name"
        assert results[0].job_display_name == "Widget herding"
        assert results[0].status is JobStatus.SUCCESS

        ui = cast(MagicMock, wired_orchestrator._ui)  # pyright: ignore[reportPrivateUsage]
        assert ui.set_current_step.call_args.args[1] == "Widget herding"

        block = buffer.getvalue()
        assert "Widget herding" in block
        assert "stub_display_name" not in block
