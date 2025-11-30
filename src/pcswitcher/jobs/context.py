"""Job execution context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pcswitcher.events import EventBus
    from pcswitcher.executor import LocalExecutor, RemoteExecutor


@dataclass(frozen=True)
class JobContext:
    """Context provided to jobs at execution time."""

    config: dict[str, Any]  # Job-specific config (validated)
    source: LocalExecutor
    target: RemoteExecutor
    event_bus: EventBus  # For logging and progress
    session_id: str
    source_hostname: str
    target_hostname: str
