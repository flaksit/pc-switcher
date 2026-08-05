"""The running package's version must be derived from a git tag, never the 0.0.0 fallback.

Deliberately not a unit test: it asserts a property of the ENVIRONMENT the package was
built in, which a unit test would have to mock away to stay hermetic. It needs no VMs
either — it lives here because `tests/integration/` is the phase whose checkout fetches
full history (`fetch-depth: 0`), which is exactly the condition under test.

`uv-dynamic-versioning` derives the version from the nearest reachable tag. A checkout
that fetches no tags leaves it nothing to resolve against, and it falls back to
`0.0.0+post.<n>.dev.0.<sha>` — a version that silently reads as OLDER than every release.
Two real behaviours break on that, neither of them noisily:

- `InstallOnTargetJob.validate` refuses to run when the target's version is newer than the
  source's; a `0.0.0` source inverts that comparison against any real target.
- The startup update check offers an upgrade to the newest release and, on a TTY, blocks
  on its prompt — which is how this was found.
"""

from __future__ import annotations

import subprocess

import pytest

from pcswitcher.version import get_this_version

pytestmark = pytest.mark.smoke

# The release triple dunamai produces when no tag is reachable. Compared as a triple, not
# with `>`: the fallback renders as `0.0.0+post.<n>.dev.0.<sha>`, whose post/dev segments
# make it sort ABOVE a bare `0.0.0`, so an ordering check would pass on the very version
# it is meant to catch.
_NO_TAG_FALLBACK_RELEASE = (0, 0, 0)


def test_version_resolves_from_a_tag_not_the_no_tag_fallback() -> None:
    """A tagless checkout yields 0.0.0 and silently reads as older than every release."""
    version = get_this_version()

    assert version.pkg_version.release[:3] != _NO_TAG_FALLBACK_RELEASE, (
        f"pc-switcher resolved to {version.semver_str()}, uv-dynamic-versioning's "
        "no-tag fallback. The checkout fetched no tags — a workflow step is missing "
        "`fetch-depth: 0`. Every version comparison in this run is against a version "
        "that reads as older than every release."
    )


def test_a_tag_is_actually_reachable_from_head() -> None:
    """The cause, asserted directly: `git describe` is what the build reads.

    Separate from the assertion above so a failure says whether the tag is missing from
    the checkout or the build resolved it wrongly, rather than leaving both open.
    """
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, (
        f"no tag is reachable from HEAD in this checkout, so the version cannot resolve: {result.stderr.strip()}"
    )
