"""Remediation text for missing passwordless-sudo rights.

Several jobs escalate privilege for specific binaries (ADR-013 for rsync, and the
package jobs for their package managers and `/etc/apt` reads). Those rights are a
system prerequisite the user has to configure, so a `validate()` failure needs to
say concretely how — a bare "passwordless sudo is not available" leaves the user to
research sudoers syntax and the safe way to edit it.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["SUDOERS_DROPIN_PATH", "passwordless_sudo_hint"]

# A drop-in keeps pc-switcher's grants in one reviewable file and leaves the
# distribution's own /etc/sudoers untouched.
SUDOERS_DROPIN_PATH = "/etc/sudoers.d/pc-switcher"


def passwordless_sudo_hint(commands: Sequence[str], *, user: str | None = None) -> str:
    """Build copy-paste instructions for granting passwordless sudo.

    `commands` are the absolute binary paths the caller needs to run as root. They are
    a lower bound, not an exact scope (ADR-013): a machine may already grant the user
    broader rights, and other jobs need their own binaries.

    `user` is the account pc-switcher connects as, when known; otherwise the text uses
    a placeholder the user substitutes.

    `visudo -f` is specified rather than a plain editor because it syntax-checks before
    saving, and a malformed file under `/etc/sudoers.d/` can lock the user out of sudo
    entirely — which on a machine reached only over SSH is expensive to recover from.
    """
    account = user or "YOUR_USER"
    grant = ", ".join(commands)
    substitution = "" if user else f" (replacing {account} with the account pc-switcher connects as)"

    return (
        f"To fix, on that machine run:\n"
        f"    sudo visudo -f {SUDOERS_DROPIN_PATH}\n"
        f"and add this line{substitution}:\n"
        f"    {account} ALL=(ALL) NOPASSWD: {grant}\n"
        f"Then confirm it worked:\n"
        f"    sudo -n true && echo OK\n"
        f"Edit with visudo rather than a plain editor: it validates the syntax before saving, "
        f"and a broken file under /etc/sudoers.d/ can lock you out of sudo entirely. "
        f"A broader existing grant is fine — these are the paths that must be permitted, "
        f"not an exact scope."
    )
