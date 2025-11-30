import asyncio
import signal
from typing import List, Dict, Type
from datetime import datetime
import uuid
import socket

from pc_switcher.config import Config
from pc_switcher.core.connection import Connection, LocalExecutor
from pc_switcher.core.events import EventBus, LogEvent
from pc_switcher.core.logging import Logger
from pc_switcher.ui.tui import TerminalUI
from pc_switcher.jobs.base import Job, JobContext


class Orchestrator:
    def __init__(self, config: Config, target_host: str):
        self._config = config
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

        self._jobs: List[Job] = []
        self._session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + str(uuid.uuid4())[:8]

    def register_job(self, job: Job):
        self._jobs.append(job)

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

            # Validate Phase
            self._logger.log("INFO", "Orchestrator", "SOURCE", "Validating jobs...")
            for job in self._jobs:
                if not self._config.global_settings.sync_jobs.get(job.name, True) and not job.required:
                    self._logger.log("INFO", "Orchestrator", "SOURCE", f"Skipping disabled job: {job.name}")
                    continue

                context = JobContext(
                    config=self._config.jobs.get(job.name, {}),
                    source=local_exec,
                    target=remote_exec,
                    logger=self._logger.get_job_logger(job.name, "SOURCE"),
                    session_id=self._session_id,
                    source_hostname=self._source_hostname,
                    target_hostname=self._target_host,
                )

                errors = await job.validate(context)
                if errors:
                    for err in errors:
                        self._logger.log("CRITICAL", job.name, "SOURCE", f"Validation failed: {err}")
                    raise RuntimeError("Validation failed")

            # Execute Phase
            for job in self._jobs:
                if not self._config.global_settings.sync_jobs.get(job.name, True) and not job.required:
                    continue

                self._logger.log("INFO", "Orchestrator", "SOURCE", f"Executing job: {job.name}")

                context = JobContext(
                    config=self._config.jobs.get(job.name, {}),
                    source=local_exec,
                    target=remote_exec,
                    logger=self._logger.get_job_logger(job.name, "SOURCE"),
                    session_id=self._session_id,
                    source_hostname=self._source_hostname,
                    target_hostname=self._target_host,
                )

                await job.execute(context)
                self._logger.log("INFO", "Orchestrator", "SOURCE", f"Job {job.name} completed")

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
