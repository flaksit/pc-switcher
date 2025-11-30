"""Job system for pc-switcher sync operations."""

from __future__ import annotations

from .base import BackgroundJob, Job, SyncJob, SystemJob
from .btrfs import BtrfsSnapshotJob
from .context import JobContext
from .disk_space_monitor import DiskSpaceMonitorJob
from .dummy import DummyFailJob, DummySuccessJob

__all__ = [
    "BackgroundJob",
    "BtrfsSnapshotJob",
    "DiskSpaceMonitorJob",
    "DummyFailJob",
    "DummySuccessJob",
    "Job",
    "JobContext",
    "SyncJob",
    "SystemJob",
]
