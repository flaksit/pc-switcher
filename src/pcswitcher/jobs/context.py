"""Job execution context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pcswitcher.confirmer import Confirmer
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
    dry_run: bool = False  # If True, skip state-modifying operations
    allow_first_sync: bool = False  # If True, auto-approve first-sync overwrite (ADR-015)
    # Interactive confirmation gate for destructive job actions (ADR-015 refinement).
    # Optional so lightweight test contexts can omit it; jobs that prompt assert it is set.
    confirmer: Confirmer | None = None
