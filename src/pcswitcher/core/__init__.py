"""Core types and interfaces for pc-switcher."""

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.job import RemoteExecutor, SyncError
from pcswitcher.core.session import JobResult, SessionState

__all__ = [
    "LogLevel",
    "JobResult",
    "RemoteExecutor",
    "SessionState",
    "SyncError",
]
