"""Core types and interfaces for pc-switcher."""

from pcswitcher.core.logging import LogLevel
from pcswitcher.core.module import RemoteExecutor, SyncError
from pcswitcher.core.session import ModuleResult, SessionState

__all__ = [
    "LogLevel",
    "ModuleResult",
    "RemoteExecutor",
    "SessionState",
    "SyncError",
]
