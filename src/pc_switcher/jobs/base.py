from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from pc_switcher.config import JobConfig
from pc_switcher.core.connection import LocalExecutor, RemoteExecutor
from pc_switcher.core.logging import JobLogger
from pc_switcher.core.events import ProgressEvent


@dataclass
class JobContext:
    config: Dict[str, Any]
    source: LocalExecutor
    target: RemoteExecutor
    logger: JobLogger
    session_id: str
    source_hostname: str
    target_hostname: str


class Job(ABC):
    name: str
    required: bool = False

    @classmethod
    def validate_config(cls, config: Dict[str, Any]) -> List[str]:
        """Validate configuration. Returns list of error messages."""
        return []

    @abstractmethod
    async def validate(self, context: JobContext) -> List[str]:
        """Validate system state. Returns list of error messages."""
        pass

    @abstractmethod
    async def execute(self, context: JobContext) -> None:
        """Execute the job."""
        pass

    def report_progress(
        self,
        context: JobContext,
        percent: int = None,
        current: int = None,
        total: int = None,
        item: str = None,
        heartbeat: bool = False,
    ):
        # We can't easily access the event bus directly here without passing it or the UI
        # But we can assume the orchestrator or logger handles it?
        # Actually, the architecture says Job -> report_progress -> UI
        # But JobContext doesn't have UI or EventBus directly exposed in the diagram?
        # Wait, diagram says JobContext has 'ui: TerminalUI'
        # Let's add UI to JobContext if we want direct reporting, OR use a special event channel.
        # The architecture says "Job -> TerminalUI: report_progress" in sequence diagram.
        # But also "Job -> Logger -> EventBus".
        # Let's use a helper on context or just assume we can emit events.
        # Ideally, we should use the EventBus.
        # Let's add event_bus to JobContext or a progress_reporter callback.
        # For now, let's assume we can use the logger to emit progress events if we want, OR just add a method to context.
        pass
