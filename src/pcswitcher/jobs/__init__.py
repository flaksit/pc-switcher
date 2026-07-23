"""Job system for pc-switcher sync operations."""

from __future__ import annotations

from .base import BackgroundJob, Job, SyncJob, SystemJob
from .btrfs import BtrfsSnapshotJob
from .context import JobContext
from .disk_space_monitor import DiskSpaceMonitorJob
from .dummy_fail import DummyFailJob
from .dummy_success import DummySuccessJob
from .folder_sync import FolderEntry, FolderSyncJob
from .install_on_target import InstallOnTargetJob
from .manual_installs_sync import ManualInstallsSyncJob
from .vscode_state_sync import VscodeStateSyncJob

__all__ = [
    "BackgroundJob",
    "BtrfsSnapshotJob",
    "DiskSpaceMonitorJob",
    "DummyFailJob",
    "DummySuccessJob",
    "FolderEntry",
    "FolderSyncJob",
    "InstallOnTargetJob",
    "Job",
    "JobContext",
    "ManualInstallsSyncJob",
    "SyncJob",
    "SystemJob",
    "VscodeStateSyncJob",
]
