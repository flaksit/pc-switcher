"""Base classes for sync jobs."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

import jsonschema

from pcswitcher.events import ProgressEvent
from pcswitcher.models import (
    ConfigError,
    FirstSyncScope,
    Host,
    LogLevel,
    ProgressUpdate,
    RunPause,
    ValidationError,
)

from .context import JobContext

logger = logging.getLogger("pcswitcher.jobs.base")

if TYPE_CHECKING:
    from pcswitcher.executor import LocalExecutor, RemoteExecutor


class Job(ABC):
    """Abstract base class for all sync jobs.

    Jobs are self-contained sync operations that:
    - Own their configuration schema
    - Validate system state before execution
    - Execute sync logic
    - Handle cancellation gracefully
    """

    name: ClassVar[str]
    display_name: ClassVar[str]
    required: ClassVar[bool] = False
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Default `display_name` to `name` for any job that does not declare one.

        `name` is an identifier and cannot be prettified: it is the `sync_jobs` config
        key, the module a job is imported from (`CORE-FR-JOB-LOAD`), the `job` field of
        every log record (`LOG-FR-JSON`) and what `PKG-FR-JOB-ORDER`'s config error
        names. `display_name` is the same job worded for a human and is read ONLY where
        a human reads it — status line, progress bars, `Job outcomes:` block, review and
        gate prose. Anything a machine matches on keeps `name`.

        Defaulted here rather than at each read site so a job that has no better wording
        (test fixtures) still answers `display_name`.
        """
        super().__init_subclass__(**kwargs)
        if "display_name" not in cls.__dict__ and "name" in cls.__dict__:
            cls.display_name = cls.name

    def __init__(self, context: JobContext) -> None:
        """Initialize job with context.

        Args:
            context: JobContext with executors, config, and event bus
        """
        self.context = context

    @property
    def source(self) -> LocalExecutor:
        """Shortcut to context.source executor."""
        return self.context.source

    @property
    def target(self) -> RemoteExecutor:
        """Shortcut to context.target executor."""
        return self.context.target

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> list[ConfigError]:
        """Validate job-specific configuration against CONFIG_SCHEMA.

        Args:
            config: Job configuration from config.yaml

        Returns:
            List of ConfigError for any validation failures.
            Empty list if config is valid.
        """
        if not cls.CONFIG_SCHEMA:
            return []

        validator = jsonschema.Draft7Validator(cls.CONFIG_SCHEMA)
        errors: list[ConfigError] = []

        for error in validator.iter_errors(config):
            path = ".".join(str(p) for p in error.absolute_path) or "(root)"
            errors.append(
                ConfigError(
                    job=cls.name,
                    path=path,
                    message=error.message,
                )
            )

        return errors

    @abstractmethod
    async def validate(self) -> list[ValidationError]:
        """Validate system state before execution.

        Called after SSH connection established, before any state modifications.

        Every environmental assumption a job makes MUST be checked here, unless it is
        already an explicit project-wide convention (matching architecture, OS, user
        ids). Required binaries, passwordless sudo for the commands this job runs,
        required packages, filesystem features, reachable paths, free locks — if the
        job would misbehave without it, `validate()` is where that is caught.

        The reason is that validation runs before any state changes, so a failure here
        costs the user nothing. The same assumption discovered mid-`execute()` fails
        against a half-modified target, and some of them do not fail loudly at all: a
        missing sudo right can degrade a capture to an empty result, and the sync then
        reports success having replicated nothing.

        A validation error MUST tell the user how to fix it, concretely enough to act
        on without further research — the offending host, what is missing, and the
        command or configuration that resolves it.

        Checks run sequentially and append to a list; never raise mid-validate, so the
        user sees every problem at once rather than fixing them one round-trip at a
        time.

        Returns:
            List of ValidationError for any issues found.
            Empty list if system state is valid.
        """
        ...

    @abstractmethod
    async def execute(self) -> None:
        """Execute job logic.

        Raises:
            Exception: Any exception halts sync with CRITICAL log
            asyncio.CancelledError: Caught, cleanup performed, re-raised
        """
        ...

    def _log(
        self,
        level: LogLevel,
        message: str,
        *,
        host: Host | None = None,
        **extra: Any,
    ) -> None:
        """Log a message through stdlib logging.

        The side is optional because not every record is about one machine: a record
        describing both hosts at once has no side to name, and naming one is worse than
        naming none (#291). The whole pipeline already travels without it — all three
        formatters omit the field when the call did not supply it (`LOG-FR-JSON`), as
        startup and shutdown records have always done.

        Args:
            level: Log level
            message: Human-readable message
            host: The machine this record is about, when it is about one
            **extra: Additional structured context
        """
        logger.log(
            level.value,
            message,
            extra={"job": self.name, **({"host": host.value} if host is not None else {}), **extra},
        )

    def _report_progress(
        self,
        update: ProgressUpdate,
    ) -> None:
        """Report progress through EventBus.

        Args:
            update: ProgressUpdate with percent/current/total/item
        """
        self.context.event_bus.publish(
            ProgressEvent(
                job=self.name,
                display_name=self.display_name,
                update=update,
            )
        )

    def _validation_error(self, host: Host, message: str) -> ValidationError:
        """Create a ValidationError with job name filled in automatically.

        Args:
            host: Which machine the error relates to (SOURCE or TARGET)
            message: Human-readable error message

        Returns:
            ValidationError with job=self.name
        """
        return ValidationError(job=self.name, host=host, message=message)


class SystemJob(Job):
    """Required infrastructure jobs (snapshots, installation).

    SystemJobs run regardless of sync_jobs config. They are orchestrator-managed.
    """

    required: ClassVar[bool] = True


class SyncJob(Job):
    """Optional user-facing sync jobs.

    SyncJobs can be enabled/disabled via sync_jobs config.
    """

    required: ClassVar[bool] = False

    @classmethod
    def describe_first_sync_scope(cls, config: dict[str, Any]) -> FirstSyncScope | None:
        """Describe this job's first-sync overwrite scope, if any (ADR-015).

        A first sync (target has no readable sync-history) is a distinct question
        from an out-of-order sync: there is no prior topology to reconcile, so any
        job that destructively replaces in-scope target state overrides this to
        name its own scope entries and overwrite mechanism. The orchestrator
        composes its first-sync warning from these self-descriptions instead of
        reaching into a specific job's config shape, so the warning stays correct
        for future non-rsync jobs without any orchestrator change.

        Called as a classmethod before job instances exist — the first-sync check
        runs before job discovery.

        Args:
            config: This job's configuration dict from config.yaml.

        Returns:
            A FirstSyncScope describing this job's scope and mechanism, or None
            if this job does not destructively overwrite target state (the
            default) or has nothing in scope for the given config.
        """
        return None

    @classmethod
    def declare_run_pauses(cls) -> frozenset[RunPause]:
        """Declare the machine-wide automatic updaters this job needs held for the run.

        The orchestrator unions the declarations of the ENABLED jobs and holds exactly
        that set, on the same principle as `describe_first_sync_scope`: it composes from
        job self-descriptions rather than reaching into specific jobs' names or config, so
        a job added later cannot silently miss a pause it depends on. The union is what
        makes each declaration independent — two jobs naming the same pause hold it once,
        and one of them being switched off does not lift it for the other.

        Called as a classmethod before job instances exist: the pauses are applied before
        the job window opens. Takes no config, unlike `describe_first_sync_scope`, because
        a job's exposure to a background updater is a property of what the job does at all,
        not of how it is configured.

        A declaration also carries its own privilege: each pause names the binaries it runs
        under sudo (`RunPause.sudo_commands`), and the orchestrator establishes the grant for
        the whole union on both machines in the validation step, before any job runs. So a job
        declaring a pause needs to say nothing about sudo in its own `validate()` — but it
        cannot decline the check either, which is the point: the declaration and the grant
        cannot drift apart.

        Returns:
            The pauses this job depends on; empty (the default) for a job that does not
            care whether either updater runs during the sync.
        """
        return frozenset()


class BackgroundJob(Job):
    """Jobs that run concurrently with other jobs.

    BackgroundJobs (like disk monitoring) are spawned by the orchestrator
    and run in a TaskGroup alongside sync jobs.
    """

    required: ClassVar[bool] = True
