import asyncio
from enum import Enum
from typing import Any, Dict, Optional, List
from dataclasses import dataclass, field
from datetime import datetime


class EventType(Enum):
    LOG = "log"
    PROGRESS = "progress"
    CONNECTION = "connection"


@dataclass(kw_only=True)
class Event:
    type: EventType
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(kw_only=True)
class LogEvent(Event):
    level: str
    job: str
    host: str  # "SOURCE" or "TARGET"
    message: str
    context: Dict[str, Any] = field(default_factory=dict)
    type: EventType = field(default=EventType.LOG, init=False)

    def __post_init__(self):
        self.type = EventType.LOG


@dataclass(kw_only=True)
class ProgressEvent(Event):
    job: str
    percent: Optional[int] = None
    current: Optional[int] = None
    total: Optional[int] = None
    item: Optional[str] = None
    heartbeat: bool = False
    type: EventType = field(default=EventType.PROGRESS, init=False)

    def __post_init__(self):
        self.type = EventType.PROGRESS


@dataclass(kw_only=True)
class ConnectionEvent(Event):
    status: str
    latency: Optional[float] = None
    type: EventType = field(default=EventType.CONNECTION, init=False)

    def __post_init__(self):
        self.type = EventType.CONNECTION


class EventBus:
    def __init__(self):
        self._consumers: List[asyncio.Queue] = []
        self._closed = False

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to events. Returns a queue that will receive all events."""
        queue = asyncio.Queue()
        self._consumers.append(queue)
        return queue

    def publish(self, event: Event) -> None:
        """Publish an event to all subscribers."""
        if self._closed:
            return

        for queue in self._consumers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Should not happen with unbounded queues

    def close(self) -> None:
        """Close the event bus."""
        self._closed = True
        # We don't close the queues here, consumers should handle None or similar sentinel if needed,
        # but typically we just stop publishing.
