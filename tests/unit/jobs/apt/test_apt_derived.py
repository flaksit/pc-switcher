"""The `/etc/apt` files that travel with no review line of their own (D-37/D-38/D-39).

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import dataclasses

import pytest

from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.packages.review import (
    ReviewOutcome,
)
from pcswitcher.models import CommandResult
from tests.unit.jobs.apt.helpers import (
    _NO_PACKAGES,
    _PIN_DIGEST_CMD,
    CountingReviewer,
    _repo_context,
    all_calls,
    install_reviewer,
    sha256_line,
)


class TestPinsStillTravelAsFiles:
    """The echo was a REPORT about pins, never the mechanism. A `preferences.d` file is
    what makes a vendor's origin outrank the archive's epoch-1 copy (D-36), so it has to
    keep reaching the target — deleting the echo must not touch that.
    """

    @pytest.mark.asyncio
    async def test_a_pin_file_the_target_lacks_is_written_with_no_review_line(self) -> None:
        """The always-sync bucket (D-36): the reviewer is handed nothing at all, and the pin
        still lands. A pin naming an origin the target does not have is inert, so this cannot
        get a derivation wrong — and it is what makes Mozilla's build outrank the archive's
        epoch-1 copy when the origin DOES arrive.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p1", "mozilla"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        reviewer = CountingReviewer({})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert any(
            "sudo install" in cmd and cmd.endswith("/etc/apt/preferences.d/mozilla") for cmd in all_calls(target)
        )
        assert reviewer.calls == [()]

    @pytest.mark.asyncio
    async def test_a_differing_pin_is_overwritten_rather_than_reviewed(self) -> None:
        """The change direction of the same rule. Under the old model this was a CHANGE line
        the user could untick; the file now simply travels.
        """
        context, _source, target = _repo_context(
            source_responses={**_NO_PACKAGES, _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p-new", "mozilla"), "")},
            target_responses={
                **_NO_PACKAGES,
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p-old", "mozilla"), ""),
                "test -f /etc/apt/preferences.d/mozilla": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        plan = await job.plan()
        job.accept_review(plan, ReviewOutcome(decisions={}, was_interactive=True))
        await job.apply()

        assert not any(d.item_id == "apt:pin:mozilla" for d in plan.diffs)
        assert any(
            "sudo install" in cmd and cmd.endswith("/etc/apt/preferences.d/mozilla") for cmd in all_calls(target)
        )

    @pytest.mark.asyncio
    async def test_the_pin_file_needs_no_read_of_its_contents(self) -> None:
        """Its whole-file digest decides everything. Nothing parses the stanzas any more, so
        the plan-time content read that only hydrated the retired echo is gone too — the
        bytes reach the target through `send_file`, never through a parse.
        """
        context, source, _target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                _PIN_DIGEST_CMD: CommandResult(0, sha256_line("p1", "mozilla"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        assert not any("cat /etc/apt/preferences.d" in cmd for cmd in all_calls(source))
