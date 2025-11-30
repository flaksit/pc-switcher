import structlog
import logging
import sys
from pathlib import Path
from typing import Dict, Any, Optional
import asyncio
from datetime import datetime

from pc_switcher.core.events import EventBus, LogEvent

# Define log levels
LOG_LEVELS = {
    "DEBUG": 10,
    "FULL": 15,  # Custom level between DEBUG and INFO
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

# Register custom level
logging.addLevelName(LOG_LEVELS["FULL"], "FULL")


class Logger:
    def __init__(self, event_bus: EventBus, hostnames: Dict[str, str]):
        self._event_bus = event_bus
        self._hostnames = hostnames  # Map Host enum (SOURCE/TARGET) to actual hostname

        # Configure structlog
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            cache_logger_on_first_use=True,
        )

    def log(self, level: str, job: str, host: str, message: str, **ctx) -> None:
        """Log a message and publish to event bus."""
        # Publish to event bus
        event = LogEvent(level=level, job=job, host=host, message=message, **ctx)
        self._event_bus.publish(event)

    def get_job_logger(self, job_name: str, host: str) -> "JobLogger":
        return JobLogger(self, job_name, host)


class JobLogger:
    def __init__(self, logger: Logger, job_name: str, host: str):
        self._logger = logger
        self._job_name = job_name
        self._host = host

    def debug(self, message: str, **ctx) -> None:
        self._logger.log("DEBUG", self._job_name, self._host, message, **ctx)

    def full(self, message: str, **ctx) -> None:
        self._logger.log("FULL", self._job_name, self._host, message, **ctx)

    def info(self, message: str, **ctx) -> None:
        self._logger.log("INFO", self._job_name, self._host, message, **ctx)

    def warning(self, message: str, **ctx) -> None:
        self._logger.log("WARNING", self._job_name, self._host, message, **ctx)

    def error(self, message: str, **ctx) -> None:
        self._logger.log("ERROR", self._job_name, self._host, message, **ctx)

    def critical(self, message: str, **ctx) -> None:
        self._logger.log("CRITICAL", self._job_name, self._host, message, **ctx)


class FileLogger:
    def __init__(self, queue: asyncio.Queue, file_path: Path, level: str):
        self._queue = queue
        self._file_path = file_path
        self._level_num = LOG_LEVELS.get(level.upper(), 20)

        # Ensure directory exists
        self._file_path.parent.mkdir(parents=True, exist_ok=True)

    async def consume(self) -> None:
        """Consume events from queue and write to file."""
        # Use structlog for formatting
        logger = structlog.get_logger()

        with open(self._file_path, "a") as f:
            while True:
                event = await self._queue.get()
                if isinstance(event, LogEvent):
                    event_level_num = LOG_LEVELS.get(event.level.upper(), 20)
                    if event_level_num >= self._level_num:
                        # Create a structlog-friendly dict
                        log_entry = {
                            "timestamp": event.timestamp.isoformat(),
                            "level": event.level,
                            "job": event.job,
                            "host": event.host,
                            "event": event.message,
                            **event.context,
                        }
                        # We manually write the JSON line to avoid double-wrapping if we used structlog directly here
                        # But wait, structlog is good for this.
                        # Let's just use json.dumps for simplicity and speed here as we defined the format in requirements
                        import json

                        f.write(json.dumps(log_entry) + "\n")
                        f.flush()
                self._queue.task_done()
