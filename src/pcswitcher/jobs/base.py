"""Base classes for sync jobs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import jsonschema

from pcswitcher.events import LogEvent, ProgressEvent
from pcswitcher.models import ConfigError, Host, LogLevel, ProgressUpdate, ValidationError

from .context import JobContext


class Job(ABC):
    """Abstract base class for all sync jobs.

    Jobs are self-contained sync operations that:
    - Own their configuration schema
    - Validate system state before execution
    - Execute sync logic
    - Handle cancellation gracefully
    """

    name: ClassVar[str]
    required: ClassVar[bool] = False
    CONFIG_SCHEMA: ClassVar[dict[str, Any]] = {}

    def __init__(self, context: JobContext) -> None:
        """Initialize job with context.

        Args:
            context: JobContext with executors, config, and event bus
        """
        self._context = context

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
        host: Host,
        level: LogLevel,
        message: str,
        **extra: Any,
    ) -> None:
        """Log a message through EventBus.

        Args:
            host: Which machine this log relates to (SOURCE or TARGET)
            level: Log level
            message: Human-readable message
            **extra: Additional structured context
        """
        self._context.event_bus.publish(
            LogEvent(
                level=level,
                job=self.name,
                host=host,
                message=message,
                context=extra,
            )
        )

    def _report_progress(
        self,
        update: ProgressUpdate,
    ) -> None:
        """Report progress through EventBus.

        Args:
            update: ProgressUpdate with percent/current/total/item
        """
        self._context.event_bus.publish(
            ProgressEvent(
                job=self.name,
                update=update,
            )
        )


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


class BackgroundJob(Job):
    """Jobs that run concurrently with other jobs.

    BackgroundJobs (like disk monitoring) are spawned by the orchestrator
    and run in a TaskGroup alongside sync jobs.
    """

    required: ClassVar[bool] = True
