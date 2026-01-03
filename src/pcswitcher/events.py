"""Event system for decoupled logging and progress reporting."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pcswitcher.models import Host, LogLevel, ProgressUpdate

__all__ = [
    "ConnectionEvent",
    "EventBus",
    "LogEvent",
    "ProgressEvent",
]


@dataclass(frozen=True)
class LogEvent:
    """Event published to EventBus for logging.

    DEPRECATED: This class is deprecated and will be removed in a future version.
    Use stdlib logging with logging.getLogger("pcswitcher.xxx") instead.
    See ADR-010 for the migration plan.
    """

    level: LogLevel
    job: str  # Job name or "orchestrator"
    host: Host
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "level": self.level.name,
            "job": self.job,
            "host": self.host.value,
            "event": self.message,
            **self.context,
        }


@dataclass(frozen=True)
class ProgressEvent:
    """Event published to EventBus for progress updates."""

    job: str
    update: ProgressUpdate
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True)
class ConnectionEvent:
    """Event published when SSH connection status changes."""

    status: str  # "connected", "disconnected"
    latency: float | None  # Round-trip time in ms, None if disconnected


type Event = LogEvent | ProgressEvent | ConnectionEvent


class EventBus:
    """Pub/sub event bus with per-consumer queues.

    Supports fan-out to multiple consumers. Each consumer gets its own queue
    to prevent blocking between consumers.
    """

    def __init__(self) -> None:
        self._consumers: list[asyncio.Queue[Event | None]] = []
        self._closed = False

    def subscribe(self) -> asyncio.Queue[Event | None]:
        """Create and return a new consumer queue.

        The queue will receive all events published after subscription.
        When the EventBus is closed, a None sentinel is sent to signal shutdown.
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._consumers.append(queue)
        return queue

    def publish(self, event: Event) -> None:
        """Publish event to all consumer queues (non-blocking).

        If a consumer queue is full, the event is still added (Queue is unbounded).
        Events are dropped silently if the bus is closed.
        """
        if self._closed:
            return
        for queue in self._consumers:
            queue.put_nowait(event)

    def close(self) -> None:
        """Signal consumers to drain and exit.

        Sends None sentinel to all consumer queues. Further publish() calls
        are silently ignored.
        """
        self._closed = True
        for queue in self._consumers:
            queue.put_nowait(None)  # Sentinel value
