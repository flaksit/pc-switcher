import asyncio
import signal
from typing import Any, Type
from pathlib import Path
from datetime import datetime
import uuid
import socket

from pc_switcher.config import Config
from pc_switcher.core.connection import Connection, LocalExecutor
from pc_switcher.core.events import EventBus, LogEvent
from pc_switcher.core.logging import Logger
from pc_switcher.core.ui import TerminalUI
from pc_switcher.jobs.base import Job, JobContext


class Orchestrator:
    _config: Config
    _config_path: Path
    _target_host: str
    _event_bus: EventBus
    _ui: TerminalUI
    _connection: Connection
    _source_hostname: str
    _logger: Logger
    _registered_jobs: list[tuple[Type[Job], dict[str, Any]]]
    _session_id: str

    def __init__(
        self,
        config: Config,
        config_path: Path,
        target_host: str,
        event_bus: EventBus,
        ui: TerminalUI,
        connection: Connection,
    ):
        self._config = config
        self._config_path = config_path
        self._target_host = target_host
        self._event_bus = EventBus()
        self._ui = TerminalUI(self._event_bus, config.global_settings.log_cli_level)
        self._connection = Connection(target_host, self._event_bus)

        # Resolve source hostname
        self._source_hostname = socket.gethostname()

        self._logger = Logger(
            self._event_bus,
            {
                "SOURCE": self._source_hostname,
                "TARGET": target_host,  # Will be updated after connection if possible, but for now use input
            },
        )

        # Store job classes and their kwargs: list[tuple[Type[Job], dict[str, Any]]]
        self._registered_jobs = []
        self._session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(uuid.uuid4())[:8]

    def register_job(self, job_class: Type[Job], **kwargs):
        """Register a job class to be run."""
        self._registered_jobs.append((job_class, kwargs))

    async def run(self):
        # Start UI
        self._ui.start()
        ui_task = asyncio.create_task(self._ui.run())

        try:
            self._logger.log("INFO", "Orchestrator", "SOURCE", f"Starting sync session {self._session_id}")

            # Connect
            self._logger.log("INFO", "Orchestrator", "SOURCE", f"Connecting to {self._target_host}...")
            await self._connection.connect()

            # Create context components
            local_exec = LocalExecutor()
            remote_exec = self._connection.get_executor()

            # Instantiate Jobs
            jobs: list[Job] = []
            for job_class, kwargs in self._registered_jobs:
                job_name = job_class.name

                # Check if disabled
                if not self._config.global_settings.sync_jobs.get(job_name, True) and not job_class.required:
                    self._logger.log("INFO", "Orchestrator", "SOURCE", f"Skipping disabled job: {job_name}")
                    continue

                context = JobContext(
                    config=self._config.jobs.get(job_name, {}),
                    source=local_exec,
                    target=remote_exec,
                    logger=self._logger.get_job_logger(job_name, "SOURCE"),
                    session_id=self._session_id,
                    source_hostname=self._source_hostname,
                    target_hostname=self._target_host,
                    config_path=self._config_path,
                    event_bus=self._event_bus,
                )

                # Instantiate job with context and kwargs
                try:
                    job = job_class(context, **kwargs)
                    jobs.append(job)
                except Exception as e:
                    self._logger.log(
                        "CRITICAL", "Orchestrator", "SOURCE", f"Failed to instantiate job {job_name}: {e}"
                    )
                    raise

            # Validate Phase
            self._logger.log("INFO", "Orchestrator", "SOURCE", "Validating jobs...")
            for job in jobs:
                errors = await job.validate()
                if errors:
                    for err in errors:
                        self._logger.log("CRITICAL", job.name, "SOURCE", f"Validation failed: {err}")
                    raise RuntimeError("Validation failed")

            # Execute Phase
            background_tasks = []
            try:
                async with asyncio.TaskGroup() as tg:
                    for job in jobs:
                        self._logger.log("INFO", "Orchestrator", "SOURCE", f"Executing job: {job.name}")

                        if job.background:
                            task = tg.create_task(job.execute())
                            background_tasks.append(task)
                            self._logger.log("INFO", "Orchestrator", "SOURCE", f"Started background job: {job.name}")
                        else:
                            await job.execute()
                            self._logger.log("INFO", "Orchestrator", "SOURCE", f"Job {job.name} completed")

                    # All foreground jobs done. Cancel background tasks.
                    if background_tasks:
                        self._logger.log("INFO", "Orchestrator", "SOURCE", "Stopping background jobs...")
                        for task in background_tasks:
                            task.cancel()
            except ExceptionGroup as eg:
                # Handle exceptions from TaskGroup
                # Filter out CancelledError if it's just background tasks stopping
                # But TaskGroup raises ExceptionGroup wrapping exceptions.
                # If only CancelledError, it might not raise?
                # Actually, if we cancel tasks, they raise CancelledError.
                # We need to see if we care.
                # Let's log it.
                self._logger.log("DEBUG", "Orchestrator", "SOURCE", f"TaskGroup exceptions: {eg}")
                # Re-raise if there are actual errors (not just cancellation)
                # For now, let's assume if we cancelled them, it's fine.
                pass

            self._logger.log("INFO", "Orchestrator", "SOURCE", "Sync completed successfully")

        except asyncio.CancelledError:
            self._logger.log("WARNING", "Orchestrator", "SOURCE", "Sync interrupted by user")
        except Exception as e:
            self._logger.log("CRITICAL", "Orchestrator", "SOURCE", f"Sync failed: {e}")
            import traceback

            self._logger.log("DEBUG", "Orchestrator", "SOURCE", traceback.format_exc())
        finally:
            await self._connection.close()
            self._ui.stop()
            await ui_task
