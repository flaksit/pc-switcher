"""Withholding a credential embedded in a URL (`PKG-FR-CREDENTIAL-PRIVACY`).

A private PPA or a commercial repository carries its credential in its own address —
`https://bearer:TOKEN@esm.ubuntu.com/...` — so the URL *is* the secret. It reaches the user,
the log and the disk through five routes, and one rule has to cover all five: the command
trace and the per-command confirmation (`executor`), every log line and its structured
context (`logger`), everything a review shows while the user decides, including the files it
prints whole (`packages.review.ReviewEntry`), the label a recorded decision keeps
(`packages.items.ItemDiff`), and the snippet bodies the registry-overwrite question puts to
the user (`jobs.packages.unreproducible`).

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
# credential, and rewriting them would make `folder_sync`'s rsync trace unreadable.
#
# The class is RFC 3986's `userinfo` grammar itself — unreserved, percent-encoding,
# sub-delimiters and `:` — so every credential a URL may legally carry is matched. Legality
# is also what keeps the match inside the URL: `/`, `?`, `#`, `"` and whitespace are all
# illegal in userinfo, so a sentence or a shell command carrying a URL and an unrelated `@`
# later on redacts nothing, and a query string containing an `@` is left alone.
_URL_USERINFO = re.compile(r"(?<=://)[A-Za-z0-9\-._~%!$&'()*+,;=:]+@")


def redact_credentials(text: str) -> str:
    """Replace the userinfo of every absolute URL in `text` with `***@`.

    Idempotent, so a string that passes two of the four routes is not double-redacted, and
    cheap on the common case: the run's debug trace is hundreds of megabytes and almost none
    of it contains a URL at all.
    """
    if "://" not in text:
        return text
    return _URL_USERINFO.sub(REDACTED_USERINFO, text)
