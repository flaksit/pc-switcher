"""Withholding a credential embedded in a URL (`PKG-FR-CREDENTIAL-PRIVACY`).

A private PPA or a commercial repository carries its credential in its own address —
`https://bearer:TOKEN@esm.ubuntu.com/...` — so the URL *is* the secret. It reaches the user
and the log through four routes, and one rule has to cover all four: the command trace and
the per-command confirmation (`executor`), every log line and its structured context
(`logger`), and the review's own text, where a repository file is shown whole for a decision
(`ItemDiff`).

The whole userinfo component goes, not just the part after the colon. A repository that
authenticates with a bearer token puts the token where a username belongs, so keeping "the
username" would keep the secret in exactly the case that matters.
"""

from __future__ import annotations

import re

__all__ = ["REDACTED_USERINFO", "redact_credentials"]

REDACTED_USERINFO = "***@"

# The userinfo of an absolute URL: everything between `://` and the `@` that closes it.
# Anchored on `://` so it cannot touch an scp-style `user@host:path` — those carry no
# credential, and rewriting them would make `folder_sync`'s rsync trace unreadable. The
# character class stops at `/`, whitespace and quoting so a sentence containing a URL and an
# unrelated `@` later on redacts the URL and nothing else.
_URL_USERINFO = re.compile(r"(?<=://)[^/\s@'\"<>]+@")


def redact_credentials(text: str) -> str:
    """Replace the userinfo of every absolute URL in `text` with `***@`.

    Idempotent, so a string that passes two of the four routes is not double-redacted, and
    cheap on the common case: the run's debug trace is hundreds of megabytes and almost none
    of it contains a URL at all.
    """
    if "://" not in text:
        return text
    return _URL_USERINFO.sub(REDACTED_USERINFO, text)
