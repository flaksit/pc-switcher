"""The one callback shape the collaborators use to reach the job's logger.

A collaborator that writes to a machine has to be able to say what it did — a derived write
and a provisioned key have no review entry and no item, so the FULL line is the only place
they are visible at all (ADR-014). Rather than each collaborator holding a `SyncJob`, they
hold this: the narrowest signature that reaches `SyncJob._log`, which keeps the direction of
the dependency one-way and lets a test pass a list's `append`.
"""

from __future__ import annotations

from typing import Any, Protocol

from pcswitcher.models import Host, LogLevel


class Log(Protocol):
    """`SyncJob._log`, as much of it as a collaborator needs."""

    def __call__(self, host: Host, level: LogLevel, message: str, **extra: Any) -> None: ...
