"""The `/etc/apt` files that travel with no review line of their own
(`PKG-FR-REPO-DERIVED`/`PKG-FR-DISTRO-FILES`/`PKG-FR-DERIVED-FAILURE`).

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock

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
    actionable_entry_ids,
    all_calls,
    foo_source_responses,
    foo_target_side_effect,
    install_reviewer,
    key_writes,
    sha256_line,
)


class TestPinsStillTravelAsFiles:
    """The echo was a REPORT about pins, never the mechanism. A `preferences.d` file is
    what makes a vendor's origin outrank the archive's epoch-1 copy (`PKG-FR-PIN-ALWAYS`), so it has to
    keep reaching the target — deleting the echo must not touch that.
    """

    @pytest.mark.asyncio
    async def test_a_pin_file_the_target_lacks_is_written_with_no_review_line(self) -> None:
        """C105, C108, H51 — the always-sync bucket (`PKG-FR-PIN-ALWAYS`): the reviewer is handed nothing at all,
        and the pin still lands. This target has no repository at all, so the origin the pin
        names is one it does not have: inert, which is why always-sync cannot get a
        derivation wrong — and it is what makes Mozilla's build outrank the archive's epoch-1
        copy when the origin DOES arrive.
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
        """C106 — the change direction of the same rule. Under the old model this was a CHANGE line
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
        """C109 — its whole-file digest decides everything. Nothing parses the stanzas any more, so
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


class TestARepositoryTravelsOnlyForAnApprovedInstall:
    """`PKG-FR-REPO-DERIVED`'s rule read backwards: a repository file travels BECAUSE a package approved from
    it does, so an install nobody approved makes nothing travel — and there is no separate
    control the user could have ticked instead.
    """

    @pytest.mark.asyncio
    async def test_a_declined_install_writes_neither_its_repository_nor_its_key(self) -> None:
        """C23 — `pkg-a` is offered and left unticked: `foo.sources` stays on the source, its
        key is never promoted, and the target's apt metadata is not refreshed for nothing.
        """
        context, _source, target = _repo_context(source_responses=foo_source_responses())
        target.run_command = AsyncMock(side_effect=foo_target_side_effect())
        job = AptSyncJob(context)
        reviewer = CountingReviewer({})
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        assert actionable_entry_ids(reviewer.calls[0]) == {"apt:package:pkg-a"}, (
            "the install must have been offered, or the negatives below are about nothing"
        )
        commands = all_calls(target)
        assert not any("sudo install" in cmd and "sources.list.d/foo.sources" in cmd for cmd in commands)
        assert key_writes(target) == []
        assert not any(cmd == "sudo apt-get update" for cmd in commands)
