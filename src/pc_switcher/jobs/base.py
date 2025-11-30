from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path
from pc_switcher.config import JobConfig
from pc_switcher.core.connection import LocalExecutor, RemoteExecutor
from pc_switcher.core.connection import LocalExecutor, RemoteExecutor, Executor
from pc_switcher.core.logging import JobLogger
from pc_switcher.core.events import ProgressEvent, EventBus


@dataclass
class JobContext:
    config: Dict[str, Any]
    source: Executor
    target: Executor
    logger: JobLogger
    session_id: str
    source_hostname: str
    target_hostname: str
    config_path: Path
    event_bus: EventBus


class Job(ABC):
    name: str
    required: bool = False
    background: bool = False

    def __init__(self, context: JobContext):
        self.context = context

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> List[str]:
        """Validate configuration. Returns list of error messages."""
        return []

    @abstractmethod
    async def validate(self) -> List[str]:
        """Validate system state. Returns list of error messages."""
        pass

    @abstractmethod
    async def execute(self) -> None:
        """Execute the job."""
        pass

    def report_progress(
        self,
        percent: Optional[int] = None,
        current: Optional[int] = None,
        total: Optional[int] = None,
        item: Optional[str] = None,
        heartbeat: bool = False,
    ):
        """Report progress via event bus."""
        event = ProgressEvent(
            job=self.name,
            percent=percent,
            current=current,
            total=total,
            item=item,
            heartbeat=heartbeat,
        )
        self.context.event_bus.publish(event)
