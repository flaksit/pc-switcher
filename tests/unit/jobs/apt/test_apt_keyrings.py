"""Signing keys: provisioned before a repository is written, collected after one is removed (D-12).

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.apt_sync.probe import parse_source_file
from pcswitcher.jobs.packages.items import DiffAction, DiffClass
from pcswitcher.jobs.packages.review import (
    Decision,
    ReviewOutcome,
)
from pcswitcher.models import CommandResult
from tests.unit.jobs.apt.helpers import (
    _APPROVE_PKG_A,
    _DEB822_FOO,
    _NO_PACKAGES,
    _SOURCE_SCAN_CMD,
    _policy_block,
    _repo_context,
    _scan_line,
    all_calls,
    foo_source_responses,
    foo_target_side_effect,
    index_of,
    install_reviewer,
    key_writes,
    make_context,
    sha256_line,
)

_KEEPER_LIST = "deb [signed-by=/etc/apt/keyrings/shared.gpg] https://keeper.example.com stable main\n"
_GOING_LIST = "deb [signed-by=/etc/apt/keyrings/shared.gpg] https://going.example.com stable main\n"
_INLINE_SOURCES = (
    "Types: deb\nURIs: https://inline.example.com\nSuites: stable\nComponents: main\n"
    "Signed-By:\n -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n mDMEY2FrZQ==\n -----END PGP PUBLIC KEY BLOCK-----\n"
)


def _scanning_target(
    target_sources: dict[str, str],
    *,
    responses: dict[str, CommandResult],
    sources_list: str = "",
) -> Callable[..., CommandResult]:
    """A target whose source-file SCAN reflects the deletions the run has actually issued.

    A `sudo rm --force /etc/apt/sources.list.d/<f>` drops `<f>` from every later scan, which is
    what lets a test prove the keyring reference count is taken against the target's real
    post-write state rather than the state `plan()` saw. `sources_list` is the content of
    `/etc/apt/sources.list`, a file pc-switcher never syncs and never deletes.
    """
    live = dict(target_sources)

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if cmd.startswith("sudo rm --force "):
            live.pop(Path(shlex.split(cmd)[-1]).name, None)
        if _SOURCE_SCAN_CMD in cmd:
            scan = "".join(_scan_line(name, content) for name, content in live.items())
            if sources_list:
                scan += _scan_line("sources.list", sources_list, path="/etc/apt/sources.list")
            return CommandResult(0, scan, "")
        for pattern, result in responses.items():
            if pattern in cmd:
                return result
        return CommandResult(0, "", "")

    return _side_effect


def _key_deletions(target: MagicMock) -> list[str]:
    return [c for c in all_calls(target) if c.startswith("sudo rm --force") and "/etc/apt/keyrings/" in c]


class TestKeysAreNotItems:
    """No `apt:key:` identity may reach a diff, a review group or a decision — in any
    direction. The user decides about repositories; keys follow.
    """

    @pytest.mark.asyncio
    async def test_no_key_reaches_a_diff_or_a_review_group_in_any_direction(self) -> None:
        """All three directions at once: `new.gpg` missing on the target, `rot.gpg` present
        with different bytes, `old.gpg` present on the target alone — under the old model
        an INSTALL, a CHANGE and a REMOVE entry.
        """
        context, _source, _target = make_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(
                    0, sha256_line("k1", "new.gpg") + sha256_line("k-new", "rot.gpg"), ""
                ),
                "find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g1", "legacy.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "cat /etc/apt/sources.list.d/foo.sources": CommandResult(0, _DEB822_FOO, ""),
                "find /etc/apt/keyrings": CommandResult(
                    0, sha256_line("k-old", "rot.gpg") + sha256_line("k9", "old.gpg"), ""
                ),
            },
        )
        job = AptSyncJob(context)

        plan = await job.plan()

        assert not any(diff.item_id.startswith("apt:key:") for diff in plan.diffs)
        assert not any(diff.item_class.value == "apt_key" for diff in plan.diffs)
        entries = {entry.item_id for group in plan.groups for entry in group.entries}
        assert not any(item_id.startswith("apt:key:") for item_id in entries)
        assert "apt:source:foo.sources" in entries, "the repository DELETION must still be reviewed"

    @pytest.mark.asyncio
    async def test_key_of_a_derived_repo_is_provisioned_with_no_decision_of_its_own(self) -> None:
        """The reviewer is told about the PACKAGE only. `foo.gpg` still lands, and lands
        before the repository that references it, which lands before the install.
        """
        context, _source, target = _repo_context(source_responses=foo_source_responses())
        target.run_command = AsyncMock(side_effect=foo_target_side_effect())
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        commands = all_calls(target)
        key_idx = index_of(commands, lambda c: "sudo install" in c and "keyrings/foo.gpg" in c)
        source_idx = index_of(commands, lambda c: "sudo install" in c and "sources.list.d/foo.sources" in c)
        install_idx = index_of(commands, lambda c: c.startswith("sudo DEBIAN") and "pkg-a" in c)
        assert key_idx < source_idx < install_idx

    @pytest.mark.asyncio
    async def test_key_of_an_overwritten_repo_is_provisioned_too(self) -> None:
        """A repository the target already has with different bytes may point at a keyring
        it has never seen — the `Signed-By:` line is part of what differs.
        """
        context, _source, target = _repo_context(source_responses=foo_source_responses())
        target.run_command = AsyncMock(
            side_effect=foo_target_side_effect(
                {"find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d-old", "foo.sources"), "")}
            )
        )
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        assert key_writes(target) == ["/etc/apt/keyrings/foo.gpg"]

    @pytest.mark.asyncio
    async def test_a_matching_keyring_is_never_written(self) -> None:
        """Same bytes on both machines: no transfer, no promotion, nothing for
        `--confirm-each-command` to prompt about.
        """
        both_sides = sha256_line("d1", "foo.sources")
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "foo.gpg"), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        assert not key_writes(target)
        assert not target.send_file.call_args_list
        assert not any(c == "sudo apt-get update" for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_one_rotated_key_serving_three_repos_is_written_once(self) -> None:
        """1-n: `shared.gpg` is named by three source files, all byte-identical on both
        machines. One rotation, one write.
        """
        names = ["a.list", "b.list", "c.list"]
        both_sides = "".join(sha256_line(f"d-{name}", name) for name in names)
        scan = "".join(_scan_line(name, _KEEPER_LIST) for name in names)
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-new", "shared.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, scan, ""),
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-old", "shared.gpg"), ""),
                "test -f /etc/apt/keyrings/shared.gpg": CommandResult(0, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        assert key_writes(target) == ["/etc/apt/keyrings/shared.gpg"]

    @pytest.mark.asyncio
    async def test_global_trust_keys_are_replicated_whether_missing_or_differing(self) -> None:
        """Nothing references a `trusted.gpg.d` key, so its own content is the only signal
        there is: copy the ones the target lacks, refresh the ones whose bytes differ.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/trusted.gpg.d": CommandResult(
                    0, sha256_line("g1", "fresh.gpg") + sha256_line("g-new", "rot.gpg"), ""
                ),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g-old", "rot.gpg"), ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        assert key_writes(target) == ["/etc/apt/trusted.gpg.d/fresh.gpg", "/etc/apt/trusted.gpg.d/rot.gpg"]

    @pytest.mark.asyncio
    async def test_an_unreferenced_source_keyring_is_not_copied_to_the_target(self) -> None:
        """`/etc/apt/keyrings` is not mirrored wholesale: a key no repository on the target
        points at would be litter, not configuration.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "nobody-wants-me.gpg"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        assert not key_writes(target)

    @pytest.mark.asyncio
    async def test_inline_armored_signed_by_names_no_keyring(self) -> None:
        """A deb822 `Signed-By:` carrying an inline armored block has an empty field value
        and continuation lines. It must yield no reference at all: not a bogus dependency
        on some file, and not a match that makes a real keyring look referenced.
        """
        _fmt, refs, _uris = parse_source_file("inline.sources", _INLINE_SOURCES)

        assert refs == ()


class TestUnusedKeyringCollection:
    """The removal half: after every repository operation, drop the `/etc/apt/keyrings`
    files no surviving source references — and nothing else.
    """

    @staticmethod
    def _context(
        *,
        target_sources: dict[str, str],
        target_source_digests: str,
        target_keyrings: str,
        source_sources: str = "",
        source_keyrings: str = "",
        sources_list: str = "",
        source_extra: dict[str, CommandResult] | None = None,
        target_extra: dict[str, CommandResult] | None = None,
    ) -> tuple[JobContext, MagicMock, MagicMock]:
        return _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, source_sources, ""),
                "find /etc/apt/keyrings": CommandResult(0, source_keyrings, ""),
                **(source_extra or {}),
            },
            target_side_effect=_scanning_target(
                target_sources,
                sources_list=sources_list,
                responses={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "find /etc/apt/sources.list.d": CommandResult(0, target_source_digests, ""),
                    "find /etc/apt/keyrings": CommandResult(0, target_keyrings, ""),
                    "test -f": CommandResult(0, "", ""),
                    "sudo apt-get update": CommandResult(0, "", ""),
                    **{
                        f"cat /etc/apt/sources.list.d/{name}": CommandResult(0, content, "")
                        for name, content in target_sources.items()
                    },
                    **(target_extra or {}),
                },
            ),
        )

    @pytest.mark.asyncio
    async def test_key_left_unreferenced_by_an_approved_removal_is_deleted(self) -> None:
        """The reference count is taken AFTER the repository is gone: the scan the
        collection pass runs no longer lists `going.list`, so `shared.gpg` is unused.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert _key_deletions(target) == ["sudo rm --force /etc/apt/keyrings/shared.gpg"]
        source_idx = index_of(commands, lambda c: "sudo rm --force" in c and "sources.list.d/going.list" in c)
        key_idx = index_of(commands, lambda c: "sudo rm --force" in c and "keyrings/shared.gpg" in c)
        update_idx = index_of(commands, lambda c: c == "sudo apt-get update")
        assert source_idx < key_idx < update_idx

    @pytest.mark.asyncio
    async def test_key_still_referenced_by_a_surviving_repo_is_kept(self) -> None:
        """`keeper.list` exists on both machines, so it has no diff of its own and nothing
        in the review mentions it — and it is exactly what keeps `shared.gpg` alive.
        """
        keeper_digest = sha256_line("d-keep", "keeper.list")
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST, "keeper.list": _KEEPER_LIST},
            target_source_digests=sha256_line("d9", "going.list") + keeper_digest,
            target_keyrings=sha256_line("k9", "shared.gpg"),
            source_sources=keeper_digest,
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert any("sources.list.d/going.list" in c for c in _all_removals(target))
        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_key_referenced_only_by_a_file_pc_switcher_never_syncs_is_kept(self) -> None:
        """`/etc/apt/sources.list` is not an item, is never captured and is never deleted —
        and a keyring named only there is still very much in use.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
            sources_list=_KEEPER_LIST,
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_key_referenced_by_a_repo_whose_removal_was_declined_is_kept(self) -> None:
        """Unticking the removal keeps the repository, and the repository keeps its key."""
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST, "extra.list": "deb https://other.example.com stable main\n"},
            target_source_digests=sha256_line("d9", "going.list") + sha256_line("d8", "extra.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
        )
        job = AptSyncJob(context)
        # Only the unrelated repo is approved, so a removal happens and the collection pass
        # runs — but `going.list`, which names the key, stays.
        install_reviewer(job, {"apt:source:extra.list": Decision.APPLY})

        await job.execute()

        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_key_referenced_by_a_machine_specific_repo_is_kept(self) -> None:
        """A source recorded skip-always produces no diff in any run, so nothing else could
        speak for it — and it still counts as a reference.
        """
        decisions_file = (
            'machine_specific:\n  "apt:source:keeper.list":\n    item_class: apt_source\n'
            "    label: \"keeper.list\"\n    reason: null\n    recorded_at: '2026-07-26T00:00:00Z'\n"
        )
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST, "keeper.list": _KEEPER_LIST},
            target_source_digests=sha256_line("d9", "going.list") + sha256_line("d-keep", "keeper.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
            target_extra={"apt.decisions.yaml": CommandResult(0, decisions_file, "")},
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        assert "apt:source:keeper.list" not in {diff.item_id for diff in plan.diffs}

        job.accept_review(
            plan,
            ReviewOutcome(decisions={"apt:source:going.list": Decision.APPLY}, was_interactive=True),
        )
        await job.apply()

        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_a_key_the_source_machine_still_has_is_never_collected(self) -> None:
        """Collection mirrors: a key both machines carry is configuration this sync is
        replicating, not litter, even when nothing on the target references it yet.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
            source_keyrings=sha256_line("k9", "shared.gpg"),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert not _key_deletions(target)

    @pytest.mark.asyncio
    async def test_a_global_trust_key_is_never_collected(self) -> None:
        """`trusted.gpg.d` is ambient trust nothing references by construction, so "unused"
        is not computable for it. It accumulates rather than being deleted on a guess.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings="",
            target_extra={"find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g9", "ambient.gpg"), "")},
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert not any("ambient.gpg" in c for c in _all_removals(target))

    @pytest.mark.asyncio
    async def test_no_source_removed_means_no_collection_pass_at_all(self) -> None:
        """ "Runs after removing sources" is literal: with no source deletion the pass does
        not run, so it does not even pay for the post-write re-scan.
        """
        context, _source, target = self._context(
            target_sources={},
            target_source_digests="",
            target_keyrings=sha256_line("k9", "orphan.gpg"),
            source_sources=sha256_line("c1", "new.sources"),
            source_extra={"cat /etc/apt/sources.list.d/new.sources": CommandResult(0, _DEB822_FOO, "")},
            source_keyrings=sha256_line("k1", "foo.gpg"),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:new.sources": Decision.APPLY})

        await job.execute()

        assert not _key_deletions(target)
        # One scan only: the plan-time one. A second would be the collection pass running.
        assert sum(1 for c in all_calls(target) if _SOURCE_SCAN_CMD in c) == 1

    @pytest.mark.asyncio
    async def test_a_key_only_the_departing_repo_needs_is_not_refreshed_first(self) -> None:
        """The keyring differs on the two machines, but its only referent is on its way
        out: refreshing it and then collecting it in the same run would be absurd.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k-old", "shared.gpg"),
            source_keyrings=sha256_line("k-new", "shared.gpg"),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        assert not key_writes(target)

    @pytest.mark.asyncio
    async def test_a_collected_key_is_backed_up_and_gated_as_a_modification(self) -> None:
        """It is backed up before deletion (so a failing `apt-get update` rolls it back)
        and its deletion carries `mutates=`, so `--confirm-each-command` shows it.
        """
        context, _source, target = self._context(
            target_sources={"going.list": _GOING_LIST},
            target_source_digests=sha256_line("d9", "going.list"),
            target_keyrings=sha256_line("k9", "shared.gpg"),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:going.list": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        backup_idx = index_of(commands, lambda c: c.startswith("sudo cp --archive /etc/apt/keyrings/shared.gpg"))
        delete_idx = index_of(commands, lambda c: c == "sudo rm --force /etc/apt/keyrings/shared.gpg")
        assert backup_idx < delete_idx
        delete_call = next(
            call
            for call in target.run_command.call_args_list
            if call.args[0] == "sudo rm --force /etc/apt/keyrings/shared.gpg"
        )
        assert delete_call.kwargs.get("mutates")


def _all_removals(target: MagicMock) -> list[str]:
    return [c for c in all_calls(target) if c.startswith("sudo rm --force")]


_SHARED_SOURCES = (
    "Types: deb\nURIs: https://vendor.example.com\nSuites: stable\nComponents: main\n"
    "Signed-By: /usr/share/keyrings/vendor.gpg\n"
)
_GHOST_SOURCES = (
    "Types: deb\nURIs: https://ghost.example.com\nSuites: stable\nComponents: main\n"
    "Signed-By: /etc/apt/keyrings/ghost.gpg\n"
)
_INLINE_ON_FIELD_LINE = (
    "Types: deb\nURIs: https://ppa.example.com\nSuites: noble\nComponents: main\n"
    "Signed-By: -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n mDMEY2FrZQ==\n"
    " -----END PGP PUBLIC KEY BLOCK-----\n"
)


def _shared_key_context(
    *,
    filename: str = "vendor.sources",
    content: str = _SHARED_SOURCES,
    origin: str = "https://vendor.example.com",
    source_shared: str = sha256_line("k1", "vendor.gpg"),
    target_shared: str = "",
    dpkg_output: str = "",
) -> tuple[JobContext, MagicMock, MagicMock]:
    """One repository whose `Signed-By:` points into `/usr/share/keyrings`, derived by the
    package `pkg-a` it serves, with the target's copy of that directory and its
    `dpkg --search` answer under the test's control.
    """
    context, source, target = _repo_context(
        source_responses={
            "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
            "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            "apt-cache policy": CommandResult(0, _policy_block("pkg-a", origin), ""),
            _SOURCE_SCAN_CMD: CommandResult(0, _scan_line(filename, content), ""),
            "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", filename), ""),
            "find /usr/share/keyrings": CommandResult(0, source_shared, ""),
        },
    )
    target.run_command = AsyncMock(
        side_effect=foo_target_side_effect(
            {
                "find /usr/share/keyrings": CommandResult(0, target_shared, ""),
                # dpkg --search exits non-zero as soon as ANY argument is unowned, which is
                # the norm: the exit code must not be what decides ownership.
                "dpkg --search": CommandResult(1, dpkg_output, "dpkg-query: no path found matching pattern\n"),
            },
            origin=origin,
        )
    )
    return context, source, target


class TestSharedKeyringsDirectory:
    """`/usr/share/keyrings` resolves references, is provisioned for referenced keys only,
    and is never collected.
    """

    @pytest.mark.asyncio
    async def test_a_usr_share_keyrings_reference_resolves_and_the_repo_is_replicable(self) -> None:
        context, _source, _target = _shared_key_context()
        job = AptSyncJob(context)

        plan = await job.plan()

        # The reference resolved, so the package is replicable and drags the repository with
        # it. A `/usr/share/keyrings` reference that went unseen would read as dangling and
        # make the package REPO_UNAVAILABLE instead.
        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert diff.action == DiffAction.INSTALL
        assert job._work.origins.plans["apt:package:pkg-a"].derived_files == frozenset({"vendor.sources"})  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_a_hand_placed_key_the_target_lacks_is_provisioned(self) -> None:
        """Nothing on this machine owns `vendor.gpg` — it is as machine-local as anything in
        `/etc/apt/keyrings`, and currently replicated nowhere.
        """
        context, _source, target = _shared_key_context()
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        assert key_writes(target) == ["/usr/share/keyrings/vendor.gpg"]

    @pytest.mark.asyncio
    async def test_a_package_owned_key_present_with_different_bytes_is_not_overwritten(self) -> None:
        """The target's own package manages that file. The repository is still written —
        refusing it over a difference this run deliberately did not touch would strand it.
        """
        context, _source, target = _shared_key_context(
            target_shared=sha256_line("k-old", "vendor.gpg"),
            dpkg_output="vendor-keyring: /usr/share/keyrings/vendor.gpg\n",
        )
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        assert key_writes(target) == []
        assert any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/vendor.sources") for c in all_calls(target)
        )

    @pytest.mark.asyncio
    async def test_a_package_owned_key_the_target_is_missing_is_copied_anyway(self) -> None:
        """The bootstrap case. `dpkg --search` answers from the package's FILE LIST, so a keyring
        can be owned and absent at once — and a vendor `.deb` that ships both a repository
        entry and the keyring trusting it can only be installed once that keyring is there.
        Ownership must gate the OVERWRITE, never the COPY.
        """
        context, _source, target = _shared_key_context(
            # The target has a key directory with something else in it, so ownership really
            # is probed, and dpkg names `vendor.gpg` as owned even though it is not there.
            target_shared=sha256_line("s9", "unrelated.gpg"),
            dpkg_output=(
                "unrelated-keyring: /usr/share/keyrings/unrelated.gpg\n"
                "vendor-keyring: /usr/share/keyrings/vendor.gpg\n"
            ),
        )
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        assert key_writes(target) == ["/usr/share/keyrings/vendor.gpg"]

    @pytest.mark.asyncio
    async def test_ownership_is_probed_once_for_every_key_directory(self) -> None:
        """One batched `dpkg --search` naming every key the target has across all three
        directories — never one call per file.
        """
        context, _source, target = _repo_context(
            source_responses={**_NO_PACKAGES},
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "per-repo.gpg"), ""),
                "find /etc/apt/trusted.gpg.d": CommandResult(0, sha256_line("g1", "legacy.gpg"), ""),
                "find /usr/share/keyrings": CommandResult(0, sha256_line("s1", "shared.gpg"), ""),
            },
        )

        await AptSyncJob(context).plan()

        dpkg_calls = [c for c in all_calls(target) if c.startswith("dpkg --search")]
        assert len(dpkg_calls) == 1
        assert "/etc/apt/keyrings/per-repo.gpg" in dpkg_calls[0]
        assert "/etc/apt/trusted.gpg.d/legacy.gpg" in dpkg_calls[0]
        assert "/usr/share/keyrings/shared.gpg" in dpkg_calls[0]

    @pytest.mark.asyncio
    async def test_a_shared_keyring_no_source_references_is_never_copied(self) -> None:
        """`/usr/share/keyrings` is not mirrored wholesale: it is mostly the distro's own."""
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /usr/share/keyrings": CommandResult(0, sha256_line("s1", "ubuntu-archive-keyring.gpg"), ""),
            },
            target_responses={**_NO_PACKAGES},
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        assert key_writes(target) == []

    @pytest.mark.asyncio
    async def test_a_genuinely_missing_key_is_still_reported_dangling(self) -> None:
        """The check must still bite, and it bites on the PACKAGE now (D-39): `ghost.gpg`
        exists in no key directory on the source, so the only file that could deliver
        `pkg-a`'s origin cannot be written and the package is reported, not installed.

        Exactly one line says so. Under the old model the repository ALSO reported the same
        dangling reference, which told the user the same thing twice about two objects.
        """
        context, _source, _target = _shared_key_context(
            filename="ghost.sources",
            content=_GHOST_SOURCES,
            origin="https://ghost.example.com",
            source_shared="",
        )

        plan = await AptSyncJob(context).plan()

        diff = next(d for d in plan.diffs if d.item_id == "apt:package:pkg-a")
        assert (diff.diff_class, diff.action) == (DiffClass.REPO_UNAVAILABLE, DiffAction.REPORT_ONLY)
        assert diff.detail is not None
        assert "/etc/apt/keyrings/ghost.gpg" in diff.detail
        assert not any(d.item_id.startswith("apt:source:") for d in plan.diffs)


class TestInlineArmoredSignedBy:
    """A `Signed-By:` value that is not an absolute path is an inline armored key, not a
    reference. Every PPA `add-apt-repository` adds is written that way.
    """

    def test_the_armor_first_line_on_the_field_line_yields_no_ref(self) -> None:
        _fmt, refs, _uris = parse_source_file("ppa.sources", _INLINE_ON_FIELD_LINE)

        assert refs == ()

    @pytest.mark.asyncio
    async def test_a_ppa_with_an_inline_key_installs_normally_and_needs_no_keyring(self) -> None:
        context, _source, target = _shared_key_context(
            filename="ppa.sources",
            content=_INLINE_ON_FIELD_LINE,
            origin="https://ppa.example.com",
            source_shared="",
        )
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        assert key_writes(target) == []
        assert any(
            "sudo install" in c and c.endswith("/etc/apt/sources.list.d/ppa.sources") for c in all_calls(target)
        )
