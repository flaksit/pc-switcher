"""The `/etc/apt` unit: ordered writes, one refresh, and all-or-nothing rollback (T-02-34).

Split out of the former single `test_apt_sync.py`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.jobs import JobContext
from pcswitcher.jobs.apt_sync import AptSyncJob
from pcswitcher.jobs.apt_sync.items import METADATA_REFRESH_ITEM_ID
from pcswitcher.jobs.packages.items import DiffAction, DiffClass
from pcswitcher.jobs.packages.review import (
    Decision,
)
from pcswitcher.jobs.packages.sync_core import PackageItemFailures
from pcswitcher.models import CommandResult
from tests.unit.jobs.apt.helpers import (
    _APPROVE_PKG_A,
    _DEB822_FOO,
    _LEGACY_BAR,
    _NO_PACKAGES,
    _POLICY_NO_CANDIDATE,
    _SOURCE_SCAN_CMD,
    CountingReviewer,
    _policy_block,
    _repo_context,
    _scan_line,
    actionable_entry_ids,
    all_calls,
    foo_source_responses,
    foo_target_side_effect,
    index_of,
    install_reviewer,
    key_writes,
    make_context,
    real_installs,
    respond_with_policy_sequence,
    sha256_line,
    target_offers,
)


def respond_with_update_sequence(
    mapping: dict[str, CommandResult],
    update_results: list[CommandResult],
    default: CommandResult | None = None,
) -> Callable[..., CommandResult]:
    """Like `respond_to`, but `sudo apt-get update` returns successive results from
    `update_results` (last one repeats) — needed to test the rollback-then-reprobe
    sequence, where the same command must fail once and then succeed.
    """
    fallback = default if default is not None else CommandResult(exit_code=0, stdout="", stderr="")
    state = {"update_calls": 0}

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        if "sudo apt-get update" in cmd:
            index = min(state["update_calls"], len(update_results) - 1)
            state["update_calls"] += 1
            return update_results[index]
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


def _no_candidate(*names: str) -> str:
    """`apt-cache policy` for names the target's apt cannot resolve yet — what a package
    whose repository this run has not written answers at plan time."""
    return "".join(f"{name}:\n  Installed: (none)\n  Candidate: (none)\n  Version table:\n" for name in names)


def _two_packages_one_repository_context(
    **overrides: CommandResult,
) -> tuple[JobContext, MagicMock, MagicMock]:
    """`pkg-a` and `pkg-b` both take their origin from the repository `foo.sources` declares,
    and the target's apt can resolve neither name until this run writes that file.

    The policy answers differ between the two reads the run makes, which is the shape a real
    target has: nothing before the derived write, both packages after it. `overrides` are
    merged last, so a test can fail one specific command.
    """
    context, source, target = _repo_context(
        source_responses=foo_source_responses(
            **{
                "apt-mark showmanual": CommandResult(0, "pkg-a\npkg-b\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\npkg-b\t1.0\n", ""),
                "apt-cache policy": CommandResult(
                    0,
                    _policy_block("pkg-a", "https://example.com") + _policy_block("pkg-b", "https://example.com"),
                    "",
                ),
            }
        )
    )
    target.run_command = AsyncMock(
        side_effect=respond_with_policy_sequence(
            {
                "echo $HOME": CommandResult(0, "/home/target-user", ""),
                "apt-mark showmanual": CommandResult(0, "", ""),
                "test -f": CommandResult(1, "", ""),
                "apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\nInst pkg-b (1.0)\n", ""),
                **overrides,
            },
            [
                CommandResult(0, _no_candidate("pkg-a", "pkg-b"), ""),
                CommandResult(0, target_offers("pkg-a", "pkg-b", origin="https://example.com"), ""),
            ],
        )
    )
    return context, source, target


class TestRepoGroupOrdering:
    @pytest.mark.asyncio
    async def test_key_then_source_then_update_then_package_install(self) -> None:
        """C1, C143, C144, N10 — N5 end to end, against the derived path: approving `pkg-a` is
        what makes `foo.sources` travel, the four commands land in apt's own dependency
        order, and a run carrying both `/etc/apt` writes and a package install still pays
        for exactly one refresh — the install path's own is a no-op.
        """
        context, _source, target = _repo_context(source_responses=foo_source_responses())
        target.run_command = AsyncMock(
            side_effect=foo_target_side_effect(
                {"apt-get --dry-run install": CommandResult(0, "Inst pkg-a (1.0)\n", "")}
            )
        )
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        commands = all_calls(target)
        key_idx = index_of(commands, lambda c: "sudo install" in c and "keyrings/foo.gpg" in c)
        source_idx = index_of(commands, lambda c: "sudo install" in c and "sources.list.d/foo.sources" in c)
        update_idx = index_of(commands, lambda c: c == "sudo apt-get update")
        package_idx = index_of(
            commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c
        )
        assert key_idx < source_idx < update_idx < package_idx
        assert sum(1 for c in commands if c == "sudo apt-get update") == 1

    @pytest.mark.asyncio
    async def test_pins_travel_without_a_review_line_and_land_before_the_sources(self) -> None:
        """A75, C26, C107, C144, H51, N10 — D-36's ordering requirement: the pin is what makes the derived repository's
        origin outrank the archive's, so it has to be in place before the sources it
        governs and before the refresh that reads them — and it reaches the target with no
        review entry of its own.
        """
        context, _source, target = _repo_context(
            source_responses=foo_source_responses(
                **{"find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "mozilla"), "")}
            )
        )
        target.run_command = AsyncMock(side_effect=foo_target_side_effect())
        job = AptSyncJob(context)
        reviewer = CountingReviewer(_APPROVE_PKG_A)
        job.context = dataclasses.replace(job.context, reviewer=reviewer)

        await job.execute()

        commands = all_calls(target)
        pin_idx = index_of(commands, lambda c: "sudo install" in c and c.endswith("/etc/apt/preferences.d/mozilla"))
        source_idx = index_of(commands, lambda c: "sudo install" in c and "sources.list.d/foo.sources" in c)
        update_idx = index_of(commands, lambda c: c == "sudo apt-get update")
        assert pin_idx < source_idx < update_idx
        assert actionable_entry_ids(reviewer.calls[0]) == {"apt:package:pkg-a"}

    @pytest.mark.asyncio
    async def test_a_package_apt_reports_no_candidate_for_is_withheld_from_the_first_pass(self) -> None:
        """The other half of what N5's ordering test cannot show: an available package is
        offered, one apt reports `Candidate: (none)` for is not — it is `REPORT_ONLY`, and
        the first review never even shows it as installable. The source's origin is one no
        file on the source declares, so nothing this run could add would supply it either.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "apt-cache policy": CommandResult(0, _policy_block("pkg-a", "https://gone.example.com/apt"), ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, _POLICY_NO_CANDIDATE, ""),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.REPO_UNAVAILABLE, DiffAction.REPORT_ONLY)]

    @pytest.mark.asyncio
    async def test_a_package_apt_has_never_heard_of_prints_no_block_and_is_still_offered(self) -> None:
        """`apt-cache policy` prints NOTHING for a name apt does not know — not a block with
        `Candidate: (none)`. That absence must read as "no evidence against", so a package
        whose repository this same run is about to add is still offered for install.
        """
        context, _source, _target = make_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
            },
            target_responses={
                "apt-mark showmanual": CommandResult(0, "", ""),
                "apt-cache policy": CommandResult(0, "", "N: Unable to locate package pkg-a\n"),
            },
        )

        plan = await AptSyncJob(context).plan()

        assert [(d.diff_class, d.action) for d in plan.diffs] == [(DiffClass.MISSING_ON_TARGET, DiffAction.INSTALL)]

    @pytest.mark.asyncio
    async def test_apt_get_update_runs_exactly_once_for_three_repo_items(self) -> None:
        """C142 — a pin, an apt-config file and a key write share one refresh, after all of them."""
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "a-pin"), ""),
                "cat /etc/apt/preferences.d/a-pin": CommandResult(0, "Package: a\n", ""),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "a-conf"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k1", "a.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/preferences.d/a-pin": CommandResult(1, "", ""),
                "test -f /etc/apt/apt.conf.d/a-conf": CommandResult(1, "", ""),
                "test -f /etc/apt/keyrings/a.gpg": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {
                "apt:pin:a-pin": Decision.APPLY,
                "apt:config:a-conf": Decision.APPLY,
                "apt:key:per-repo:a.gpg": Decision.APPLY,
            },
        )

        await job.execute()

        commands = all_calls(target)
        assert sum(1 for c in commands if c == "sudo apt-get update") == 1

    @pytest.mark.asyncio
    async def test_no_key_command_contains_a_url(self) -> None:
        """C72 — D-12: `foo.gpg` really is provisioned (the repository that needs it is
        derived), and not one command reaches for a vendor to get it.
        """
        context, _source, target = _repo_context(source_responses=foo_source_responses())
        target.run_command = AsyncMock(side_effect=foo_target_side_effect())
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        commands = all_calls(target)
        assert any("sudo install" in cmd and "keyrings/foo.gpg" in cmd for cmd in commands)
        for cmd in commands:
            assert "http://" not in cmd
            assert "https://" not in cmd

    @pytest.mark.asyncio
    async def test_a_failed_derived_repository_write_fails_the_package_that_needed_it(self) -> None:
        """C88, J22 — D-39's attribution. A keyring that could not be promoted is not a failed item —
        there is no key item, and there is no repository item either — so the repository is
        not written (a repo apt cannot verify is worse than no repo) and the failure lands
        on the PACKAGE, which is the thing the user decided about. The message names the
        file, and the install command is never issued.
        """
        context, _source, target = _repo_context(source_responses=foo_source_responses())
        target.run_command = AsyncMock(
            side_effect=foo_target_side_effect(
                {"sudo install --owner=root --group=root --mode=0644": CommandResult(1, "", "disk full")}
            )
        )
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a"}
        assert "/etc/apt/sources.list.d/foo.sources" in failures["apt:package:pkg-a"]
        assert "foo.gpg" in failures["apt:package:pkg-a"]
        commands = all_calls(target)
        assert not any("sudo install" in c and "sources.list.d/foo.sources" in c for c in commands)
        assert not real_installs(target)

    @pytest.mark.asyncio
    async def test_a_repository_whose_own_promotion_fails_also_fails_its_package(self) -> None:
        """C159 — the other way a derived write can fail: the key lands, the repository's own
        `sudo install` does not. The refusal must still reach the package (D-39) — there is
        no repository item left for it to land on.
        """
        context, _source, target = _repo_context(source_responses=foo_source_responses())
        target.run_command = AsyncMock(
            side_effect=foo_target_side_effect(
                {
                    "sudo install --owner=root --group=root --mode=0644 "
                    "/home/target-user/.cache/pc-switcher/apt-staging/etc_apt_sources.list.d_foo.sources": (
                        CommandResult(1, "", "Read-only file system")
                    )
                }
            )
        )
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a"}
        assert "/etc/apt/sources.list.d/foo.sources" in failures["apt:package:pkg-a"]
        assert "Read-only file system" in failures["apt:package:pkg-a"]
        assert key_writes(target) == ["/etc/apt/keyrings/foo.gpg"]
        assert not real_installs(target)

    @pytest.mark.asyncio
    async def test_remove_source_issues_single_rm_naming_that_file(self) -> None:
        """C59 — an approved repository deletion is one `sudo rm --force`, and nothing else
        under `/etc/apt` moves."""
        context, _source, target = _repo_context(
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "extra.list"), ""),
                "cat /etc/apt/sources.list.d/extra.list": CommandResult(
                    0, "deb https://example.com stable main\n", ""
                ),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:source:extra.list": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        etc_removals = [c for c in commands if "sudo rm --force" in c]
        assert len(etc_removals) == 1
        assert "sources.list.d/extra.list" in etc_removals[0]

    @pytest.mark.asyncio
    async def test_promotion_uses_sudo_install_with_owner_group_mode_never_mv(self) -> None:
        """C126 — an approved apt-config write is promoted as root:root 0644, never moved."""
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        promotions = [c for c in commands if "apt.conf.d/99conf" in c and "sudo install" in c]
        assert len(promotions) == 1
        assert "--owner=root --group=root --mode=0644" in promotions[0]
        assert not any("sudo mv" in c for c in commands)

    @pytest.mark.asyncio
    async def test_staging_file_removed_after_success_and_after_failure(self) -> None:
        """C126 — the staging copy leaves the target whichever way the promotion goes."""
        for promote_result, label in (
            (CommandResult(0, "", ""), "success"),
            (CommandResult(1, "", "boom"), "failure"),
        ):
            context, _source, target = _repo_context(
                source_responses={
                    **_NO_PACKAGES,
                    "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
                },
                target_responses={
                    **_NO_PACKAGES,
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "sudo install --owner=root --group=root --mode=0644": promote_result,
                    "sudo apt-get update": CommandResult(0, "", ""),
                },
            )
            job = AptSyncJob(context)
            install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

            if label == "success":
                await job.execute()
            else:
                with pytest.raises(PackageItemFailures):
                    await job.execute()

            commands = all_calls(target)
            staged_cleanup = [c for c in commands if c.startswith("rm --force") and "apt-staging" in c]
            assert len(staged_cleanup) == 1, f"expected one staging cleanup for {label}"

    @pytest.mark.asyncio
    async def test_send_file_destinations_start_with_home_never_contain_etc(self) -> None:
        """C126 — nothing is SFTP'd into `/etc`: every transfer lands under the target's home."""
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        await job.execute()

        destinations = [call.args[1] for call in target.send_file.call_args_list]
        assert destinations, "expected at least one send_file call"
        for dest in destinations:
            assert dest.startswith("/home/target-user")
            assert "/etc" not in dest


class TestRepoGroupRemovalAndKeyChange:
    """C-24 and C-8: the two repository-group shapes the ordering tests above do not
    exercise — a source file and its key removed together, and a key whose bytes differ
    on the two machines.
    """

    @pytest.mark.asyncio
    async def test_source_and_its_key_both_removed_with_one_update_after_both(self) -> None:
        """C157 — both files are extra on the target and both approved: each gets its own
        `sudo rm --force`, and the run's single `apt-get update` runs after both writes — apt's
        metadata must never be refreshed against a half-removed repository.
        """
        context, _source, target = _repo_context(
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d9", "extra.list"), ""),
                "cat /etc/apt/sources.list.d/extra.list": CommandResult(0, _LEGACY_BAR, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k9", "bar.gpg"), ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(
            job,
            {"apt:key:per-repo:bar.gpg": Decision.APPLY, "apt:source:extra.list": Decision.APPLY},
        )

        await job.execute()

        commands = all_calls(target)
        removals = [c for c in commands if c.startswith("sudo rm --force")]
        assert len(removals) == 2
        assert any("keyrings/bar.gpg" in c for c in removals)
        assert any("sources.list.d/extra.list" in c for c in removals)

        assert sum(1 for c in commands if c == "sudo apt-get update") == 1
        update_idx = index_of(commands, lambda c: c == "sudo apt-get update")
        assert update_idx > max(commands.index(c) for c in removals)

    @pytest.mark.asyncio
    async def test_rotated_keyring_is_refreshed_although_its_source_file_is_identical(self) -> None:
        """C75, C156 — `foo.sources` is byte-identical on both machines and produces NO diff at
        all, but the keyring it names has different bytes — the vendor rotated it. The
        SOURCE's key file is staged under the target's home and promoted with `sudo
        install --owner=root --group=root --mode=0644`; never re-fetched, never parsed, never written
        from the target's own copy.
        """
        both_sides = sha256_line("d1", "foo.sources")
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-new", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, both_sides, ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-old", "foo.gpg"), ""),
                "test -f /etc/apt/keyrings/foo.gpg": CommandResult(0, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        plan = await job.plan()
        assert not plan.diffs, "a rotated key must not manufacture a diff of any kind"
        assert not plan.groups

        install_reviewer(job, {})
        await job.execute()

        transfers = [(call.args[0], call.args[1]) for call in target.send_file.call_args_list]
        assert len(transfers) == 1
        local_path, staged_dest = transfers[0]
        assert local_path == Path("/etc/apt/keyrings/foo.gpg")
        assert staged_dest.startswith("/home/target-user")

        promotions = [
            c
            for c in all_calls(target)
            if c.startswith("sudo install --owner=root --group=root --mode=0644") and "foo.gpg" in c
        ]
        assert len(promotions) == 1
        assert (
            promotions[0]
            == f"sudo install --owner=root --group=root --mode=0644 {staged_dest} /etc/apt/keyrings/foo.gpg"
        )


class TestOneRepositoryServingTwoPackages:
    """A derived repository is written once for however many approved installs need it, and
    a write that fails is charged to every one of them (`PKG-FR-DERIVED-FAILURE`).
    """

    @pytest.mark.asyncio
    async def test_one_repository_two_approved_installs_is_written_once(self) -> None:
        """C25 — the write set is a set: two packages taking their origin from `foo.sources`
        make it travel once, and both are installed from it.
        """
        context, _source, target = _two_packages_one_repository_context()
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        await job.execute()

        promotions = [
            c
            for c in all_calls(target)
            if c.startswith("sudo install --owner=root --group=root --mode=0644")
            and c.endswith("/etc/apt/sources.list.d/foo.sources")
        ]
        assert len(promotions) == 1
        assert len(real_installs(target)) == 2

    @pytest.mark.asyncio
    async def test_a_failed_repository_write_fails_both_packages_that_needed_it(self) -> None:
        """C160 — the attribution is per package, not per file: neither install may run
        against an `/etc/apt` the run failed to produce, so both fail naming the file.
        """
        context, _source, target = _two_packages_one_repository_context(
            **{
                "sudo install --owner=root --group=root --mode=0644 "
                "/home/target-user/.cache/pc-switcher/apt-staging/etc_apt_sources.list.d_foo.sources": (
                    CommandResult(1, "", "Read-only file system")
                )
            }
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a", "apt:package:pkg-b"}
        for message in failures.values():
            assert "/etc/apt/sources.list.d/foo.sources" in message
        assert not real_installs(target)

    @pytest.mark.asyncio
    async def test_one_packages_own_install_failure_is_not_charged_to_the_repository(self) -> None:
        """C161 — the file landed, so `pkg-a`'s own failure is `pkg-a`'s: `pkg-b` installs,
        and no derived destination is recorded as failed.
        """
        context, _source, target = _two_packages_one_repository_context(
            **{
                "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                    CommandResult(1, "", "dpkg error for pkg-a")
                )
            }
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:package:pkg-a": Decision.APPLY, "apt:package:pkg-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        assert {diff.item_id for diff, _ in exc_info.value.failures} == {"apt:package:pkg-a"}
        assert job._work.derived.failed == {}  # pyright: ignore[reportPrivateUsage]
        assert any("pkg-b" in cmd for cmd in real_installs(target))


class TestDerivedWritesAreVisible:
    """`PKG-FR-DERIVED-VISIBLE`: a derived `/etc/apt` file is on no review screen, so the log
    is the only record that it reached the target at all.
    """

    @pytest.mark.asyncio
    async def test_a_derived_pin_is_logged_as_it_lands(self, caplog: pytest.LogCaptureFixture) -> None:
        """C162 — the line names the destination and the machine the bytes came from."""
        context, _source, _target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "mozilla"), ""),
            },
            target_responses={**_NO_PACKAGES, "sudo apt-get update": CommandResult(0, "", "")},
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        with caplog.at_level(1):
            await job.execute()

        assert "wrote /etc/apt/preferences.d/mozilla from source-host" in caplog.text


class TestRepoGroupTransaction:
    @pytest.mark.asyncio
    async def test_failed_update_restores_changed_deletes_created_records_group_failures(self) -> None:
        """C145, C148, C149, C150, J21 — everything the unit touches was backed up first, and a
        failing refresh puts every file back, fails every group item and charges every
        derived write with it.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, "Package: curl\n", ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "test -f /etc/apt/preferences.d/curl-pin": CommandResult(0, "", ""),
                    "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p2", "curl-pin"), ""),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:config:99conf" in failed_ids
        # The reviewed half fails as an item; the derived pin has no item to fail, so the
        # rollback records it against its destination instead (D-39) — without which a
        # package depending on it would install against the pre-run `/etc/apt`.
        assert "/etc/apt/preferences.d/curl-pin" in job._work.derived.failed  # pyright: ignore[reportPrivateUsage]

        commands = all_calls(target)
        # Restore: the pre-existing pin file is put back from its backup.
        assert any("sudo install" in c and "backup-" in c and "preferences.d/curl-pin" in c for c in commands)
        # Delete: the brand-new config file this run created is removed.
        assert any("sudo rm --force" in c and "apt.conf.d/99conf" in c for c in commands)
        # A clean rollback discards the backup.
        assert any(c.startswith("rm --recursive --force") and "backup-" in c for c in commands)
        # Two `apt-get update` calls: the failing one and the post-rollback reprobe.
        assert sum(1 for c in commands if c == "sudo apt-get update") == 2

    @pytest.mark.asyncio
    async def test_failed_rollback_step_warns_and_keeps_the_backup(self) -> None:
        """C152 — a restore that fails must be named, and its backup must survive: that directory
        holds the only remaining copy of the file's pre-run content.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
                "cat /etc/apt/preferences.d/curl-pin": CommandResult(0, "Package: curl\n", ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "test -f /etc/apt/preferences.d/curl-pin": CommandResult(0, "", ""),
                    "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p2", "curl-pin"), ""),
                    # The restore itself fails — the case this test exists for.
                    "sudo install --owner=root --group=root --mode=0644 /home/target-user/.cache": CommandResult(
                        1, "", "Read-only file system"
                    ),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:pin:curl-pin": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        # The incomplete rollback reaches the user through every group item's failure text,
        # naming the file and where its backup was kept.
        messages = " ".join(stderr for _diff, stderr in exc_info.value.failures)
        assert "ROLLBACK INCOMPLETE" in messages
        assert "preferences.d/curl-pin" in messages
        assert "backup-" in messages

        # The backup is NOT discarded — it is the only copy of the pre-run file left.
        assert not any(c.startswith("rm --recursive --force") and "backup-" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_successful_update_issues_no_restore_command(self) -> None:
        """C147 — a refresh that succeeds rolls nothing back and discards the backup."""
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                "sudo apt-get update": CommandResult(0, "", ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        await job.execute()

        commands = all_calls(target)
        assert not any("sudo install" in c and "backup-" in c for c in commands)

    @pytest.mark.asyncio
    async def test_rollback_does_not_prevent_package_items_from_being_attempted(self) -> None:
        """C154 — a package that did not depend on the rolled-back unit is still attempted
        and is not reported failed."""
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    "apt-mark showmanual": CommandResult(0, "", ""),
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                        0, "Inst pkg-a (1.0)\n", ""
                    ),
                    "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                        CommandResult(0, "", "")
                    ),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:99conf": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert "apt:config:99conf" in failed_ids
        assert "apt:package:pkg-a" not in failed_ids

        commands = all_calls(target)
        assert any("sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c and "pkg-a" in c for c in commands)

    @pytest.mark.asyncio
    async def test_post_rollback_install_issues_no_further_apt_get_update(self) -> None:
        """C155 — D-18: the rollback's re-probe `apt-get update` succeeded, so `/etc/apt` is the
        pre-run configuration with fresh metadata. The package items that still run after
        the rollback (D-27) must issue no third refresh — the run's single-refresh
        guarantee (decision 1) holds across the rollback path too.
        """
        context, _source, target = _repo_context(
            source_responses={
                "apt-mark showmanual": CommandResult(0, "pkg-a\n", ""),
                "dpkg-query": CommandResult(0, "pkg-a\t1.0\n", ""),
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    "apt-mark showmanual": CommandResult(0, "", ""),
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "apt-get --dry-run install --assume-yes --no-install-recommends pkg-a": CommandResult(
                        0, "Inst pkg-a (1.0)\n", ""
                    ),
                    "sudo DEBIAN_FRONTEND=noninteractive apt-get install --assume-yes --no-install-recommends pkg-a": (
                        CommandResult(0, "", "")
                    ),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:99conf": Decision.APPLY, "apt:package:pkg-a": Decision.APPLY})

        with pytest.raises(PackageItemFailures):
            await job.execute()

        commands = all_calls(target)
        # Exactly two: the group's own failing refresh, and the rollback's re-probe.
        assert sum(1 for c in commands if c == "sudo apt-get update") == 2
        install_idx = index_of(commands, lambda c: "sudo DEBIAN_FRONTEND=noninteractive apt-get install" in c)
        assert not any(c == "sudo apt-get update" for c in commands[install_idx:])

    @pytest.mark.asyncio
    async def test_a_reprobe_that_also_fails_says_the_target_is_still_broken(self) -> None:
        """C151 — after a rollback the run re-probes apt and says which way it came out, so
        the user is not left to guess whether the target recovered.
        """
        context, _source, _target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(1, "", "still failing")],
            ),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        messages = " ".join(message for _diff, message in exc_info.value.failures)
        assert "still broken after rollback" in messages

    @pytest.mark.asyncio
    async def test_a_rollback_attempts_every_file_even_after_one_step_fails(self) -> None:
        """C153 — a destination that cannot be restored must not strand the remaining files
        in their post-run state: both steps are issued and both are named.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(0, sha256_line("c1", "99conf"), ""),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1", "curl-pin"), ""),
            },
            target_side_effect=respond_with_update_sequence(
                mapping={
                    "echo $HOME": CommandResult(0, "/home/target-user", ""),
                    **_NO_PACKAGES,
                    "test -f /etc/apt/apt.conf.d/99conf": CommandResult(1, "", ""),
                    "test -f /etc/apt/preferences.d/curl-pin": CommandResult(0, "", ""),
                    "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p2", "curl-pin"), ""),
                    # The restore of the pre-existing pin, and the deletion of the file this
                    # run created: the two rollback steps, both refused.
                    "--mode=0644 /home/target-user/.cache/pc-switcher/apt-staging/backup-": CommandResult(
                        1, "", "Read-only file system"
                    ),
                    "sudo rm --force /etc/apt/apt.conf.d/99conf": CommandResult(1, "", "Read-only file system"),
                },
                update_results=[CommandResult(1, "", "update failed"), CommandResult(0, "", "")],
            ),
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:99conf": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        messages = " ".join(message for _diff, message in exc_info.value.failures)
        assert "ROLLBACK INCOMPLETE" in messages
        assert "preferences.d/curl-pin" in messages
        assert "apt.conf.d/99conf" in messages
        commands = all_calls(target)
        assert any("--mode=0644" in c and "backup-" in c and "preferences.d/curl-pin" in c for c in commands)
        assert "sudo rm --force /etc/apt/apt.conf.d/99conf" in commands


class TestAnEmptyRepositoryUnit:
    """The metadata-refresh marker exists because a run can owe `/etc/apt` work no diff
    represents, and `Keyrings.pending_work` deliberately over-reports. The unit recomputes
    the exact set and must then do nothing at all.
    """

    @pytest.mark.asyncio
    async def test_the_marker_succeeds_with_no_refresh_when_the_unit_has_nothing_to_do(self) -> None:
        """C158 — the source's rotated `foo.gpg` is referenced only by a source file this run
        does not write (nothing on the target needs it), so the superset test raises the
        marker and the exact set is empty: no `apt-get update`, and the item still succeeds.
        """
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                _SOURCE_SCAN_CMD: CommandResult(0, _scan_line("foo.sources", _DEB822_FOO), ""),
                "find /etc/apt/sources.list.d": CommandResult(0, sha256_line("d1", "foo.sources"), ""),
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-new", "foo.gpg"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/keyrings": CommandResult(0, sha256_line("k-old", "foo.gpg"), ""),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {})

        await job.execute()

        assert job._accepted_plan is not None  # pyright: ignore[reportPrivateUsage]
        marker = [
            diff
            for diff in job._accepted_plan.diffs  # pyright: ignore[reportPrivateUsage]
            if diff.item_id == METADATA_REFRESH_ITEM_ID
        ]
        assert len(marker) == 1, "the marker is what routes an otherwise diff-less run into the unit"
        commands = all_calls(target)
        assert not any(c == "sudo apt-get update" for c in commands)
        assert key_writes(target) == []


class TestRepoGroupBackupFailure:
    """CR-01 regression: a `_backup_destination` failure must fail every repository-
    group item through the normal per-item `PackageItemFailures` path, never escape
    as a bare `KeyError` (which would crash the whole job and cancel every other
    already-approved job's `apply()`, violating D-27).
    """

    @pytest.mark.asyncio
    async def test_backup_failure_fails_every_group_item_without_crashing(self) -> None:
        """C146, J21 — a backup that fails aborts the unit before any write, and every group item
        and every derived write is recorded failed."""
        context, _source, target = _repo_context(
            source_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("c1-new", "conf-a") + sha256_line("c2-new", "conf-b"), ""
                ),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1-new", "pin-a"), ""),
            },
            target_responses={
                **_NO_PACKAGES,
                "find /etc/apt/apt.conf.d": CommandResult(
                    0, sha256_line("c1-old", "conf-a") + sha256_line("c2-old", "conf-b"), ""
                ),
                "find /etc/apt/preferences.d": CommandResult(0, sha256_line("p1-old", "pin-a"), ""),
                "test -f /etc/apt/apt.conf.d/conf-": CommandResult(0, "", ""),
                "test -f /etc/apt/preferences.d/pin-a": CommandResult(0, "", ""),
                "sudo cp --archive": CommandResult(1, "", "disk full"),
            },
        )
        job = AptSyncJob(context)
        install_reviewer(job, {"apt:config:conf-a": Decision.APPLY, "apt:config:conf-b": Decision.APPLY})

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        # Both group items (plus the auto-injected metadata-refresh marker) are
        # reported as failures — not just the one whose backup was actually
        # attempted before the loop aborted — and no KeyError escapes.
        failed_ids = {diff.item_id for diff, _ in exc_info.value.failures}
        assert {"apt:config:conf-a", "apt:config:conf-b"} <= failed_ids
        assert "/etc/apt/preferences.d/pin-a" in job._work.derived.failed  # pyright: ignore[reportPrivateUsage]

        commands = all_calls(target)
        # Nothing was written at all: the group aborts before any write once backing up
        # fails, derived files included.
        assert not any(
            "sudo install --owner=root --group=root --mode=0644" in c and "/etc/apt/" in c for c in commands
        )


class TestKeyringsDirectoryEnsured:
    """CR-02 regression: `/etc/apt/keyrings` does not ship on a fresh Ubuntu 24.04
    target (unlike `sources.list.d`/`preferences.d`/`apt.conf.d`/`trusted.gpg.d`,
    which are part of the `apt` package), so `sudo install` without `-D` fails with
    "No such file or directory" promoting a per-repo key to a fresh machine — exactly
    the "sync a fresh machine" scenario this subsystem exists for.
    """

    @staticmethod
    def _fresh_target(**extra: CommandResult) -> tuple[JobContext, MagicMock, MagicMock]:
        """`foo.sources` and the `foo.gpg` it names, both missing on a target that has no
        `/etc/apt/keyrings` directory at all, derived by the `pkg-a` that repository serves.
        """
        context, source, target = _repo_context(source_responses=foo_source_responses())
        target.run_command = AsyncMock(side_effect=foo_target_side_effect(extra))
        return context, source, target

    @pytest.mark.asyncio
    async def test_promotion_ensures_keyrings_directory_before_install(self) -> None:
        """C89 — `/etc/apt/keyrings` is created before a key is promoted into it."""
        context, _source, target = self._fresh_target()
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        await job.execute()

        commands = all_calls(target)
        mkdir_idx = index_of(commands, lambda c: c == "sudo mkdir --parents --mode=0755 /etc/apt/keyrings")
        install_idx = index_of(
            commands, lambda c: "sudo install --owner=root --group=root --mode=0644" in c and "keyrings/foo.gpg" in c
        )
        assert mkdir_idx < install_idx

    @pytest.mark.asyncio
    async def test_directory_preparation_failure_fails_the_item_not_the_run(self) -> None:
        """C90, J22 — the failure surfaces on the PACKAGE, the thing the user reviewed: its key
        never landed, so the repository is not written either (D-12/D-39)."""
        context, _source, target = self._fresh_target(
            **{"sudo mkdir --parents --mode=0755 /etc/apt/keyrings": CommandResult(1, "", "permission denied")}
        )
        job = AptSyncJob(context)
        install_reviewer(job, _APPROVE_PKG_A)

        with pytest.raises(PackageItemFailures) as exc_info:
            await job.execute()

        failures = {diff.item_id: message for diff, message in exc_info.value.failures}
        assert set(failures) == {"apt:package:pkg-a"}
        assert "foo.gpg" in failures["apt:package:pkg-a"]
        commands = all_calls(target)
        assert not any(
            "sudo install --owner=root --group=root --mode=0644" in c and "keyrings/foo.gpg" in c for c in commands
        )
