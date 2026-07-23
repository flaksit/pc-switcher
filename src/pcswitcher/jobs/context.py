"""Job execution context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pcswitcher.confirmer import Confirmer
    from pcswitcher.events import EventBus
    from pcswitcher.executor import LocalExecutor, RemoteExecutor
    from pcswitcher.jobs.package_review import Reviewer


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
    # Per-manager batched review for package jobs (D-24). Optional for the same reason
    # `confirmer` is: lightweight unit-test contexts construct `JobContext` without one, so
    # tests that never reach a review keep working; a `PackageSyncJob.execute()` that does
    # reach one asserts it is set rather than silently applying unreviewed diffs.
    reviewer: Reviewer | None = None
    # SSH username on the target, resolved from the live asyncssh connection.
    # Optional so existing lightweight test contexts (which don't set up a real connection)
    # keep working; jobs that need it fall back to getpass.getuser() when None.
    target_username: str | None = None
    # Read-only enablement map for ALL sync jobs (not just this one), keyed by job name,
    # so a job can ask whether a sibling is enabled (e.g. plan 02-10 gates folder_sync's
    # package-path exclusions on flatpak_sync/snap_sync being enabled). `config` above
    # stays job-specific — widening it would silently change what every existing job sees
    # (e.g. folder_sync expects its own section at config["folders"]). Optional with a
    # `None` default so the lightweight `JobContext(...)` constructions in existing unit
    # tests keep working; `None` means "no siblings known", reproducing today's behavior.
    enabled_sync_jobs: Mapping[str, bool] | None = None
