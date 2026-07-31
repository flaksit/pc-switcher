"""Unit tests for FlatpakSyncJob: tab-separated `flatpak list`/`flatpak remotes`
parsing, the flatpak-specific plan()/diff pipeline, scope-as-identity, the remotes derived
around the converge loop — provisioned before it, filtered and deleted after it — and the
missing-origin-remote skip guard.

All executor interactions are mocked; no real flatpak commands run.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from pcswitcher.config import Configuration
from pcswitcher.jobs import JobContext, flatpak_sync
from pcswitcher.jobs.flatpak_sync import (
    FlatpakItem,
    FlatpakRemoteItem,
    FlatpakSyncJob,
    _parse_flatpak_masks,  # pyright: ignore[reportPrivateUsage]
    _parse_flatpak_remotes,  # pyright: ignore[reportPrivateUsage]
    _parse_keyring_digests,  # pyright: ignore[reportPrivateUsage]
    flatpak_sync_exclude_paths,
)
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass, ItemDiff
from pcswitcher.jobs.packages.probes import ProbeFailed
from pcswitcher.jobs.packages.review import (
    REPO_CONFLICT_REVIEW_ACTION,
    Decision,
    ReviewEntry,
    ReviewGroup,
    ReviewOutcome,
    _is_promotable_group,  # pyright: ignore[reportPrivateUsage]
    _is_removal_direction,  # pyright: ignore[reportPrivateUsage]
)
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackageItemFailures, PackagePlan
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator

# `flatpak list --app --columns=application,version,origin,installation,ref` has NO
# header row (RESEARCH: verified live against Flatpak 1.14.6, unlike `snap list`) —
# the --columns flag itself names the columns, so output is exactly those four
# tab-separated fields per line.
FLATPAK_LIST_SOURCE = (
    "com.slack.Slack\t4.50.0\tflathub\tsystem\tcom.slack.Slack/x86_64/stable\n"
    "org.gnome.Podcasts\t1.0\tflathub\tuser\torg.gnome.Podcasts/x86_64/stable\n"
    "org.gimp.GIMP\t2.10\tflathub\tuser\torg.gimp.GIMP/x86_64/stable\n"
    "org.example.SplitScope\t1.0\tflathub\tuser\torg.example.SplitScope/x86_64/stable\n"
    "org.example.NeedsRemote\t1.0\tcustomremote\tuser\torg.example.NeedsRemote/x86_64/stable\n"
)

FLATPAK_LIST_TARGET = (
    "org.gnome.Podcasts\t1.0\tflathub\tuser\torg.gnome.Podcasts/x86_64/stable\n"
    "org.gimp.GIMP\t2.9\tflathub\tuser\torg.gimp.GIMP/x86_64/stable\n"
    "com.spotify.Client\t1.0\tflathub\tuser\tcom.spotify.Client/x86_64/stable\n"
    "org.example.SplitScope\t1.0\tflathub\tsystem\torg.example.SplitScope/x86_64/stable\n"
)

FLATPAK_LIST_BOTH_SCOPES = (
    "org.example.App\t1.0\tflathub\tuser\torg.example.App/x86_64/stable\n"
    "org.example.App\t1.0\tflathub\tsystem\torg.example.App/x86_64/stable\n"
)


def remote_row(name: str, url: str, *, options: str = "", ref_filter: str = "-") -> str:
    """One `flatpak remotes --columns=name,url,options,filter` line.

    Four tab-separated fields always: an unfiltered remote prints `-` in the filter column
    rather than nothing (measured, Flatpak 1.14.6), so requesting `filter` last is what makes
    the width fixed.
    """
    return f"{name}\t{url}\t{options}\t{ref_filter}\n"


_FLATHUB_REMOTE_LINE = remote_row("flathub", "https://dl.flathub.org/repo/")

SOURCE_RESPONSES = {
    "flatpak list --app --columns=application,version,origin,installation,ref": CommandResult(
        0, FLATPAK_LIST_SOURCE, ""
    ),
    "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
    "flatpak remotes --system --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
}

TARGET_RESPONSES = {
    "flatpak list --app --columns=application,version,origin,installation,ref": CommandResult(
        0, FLATPAK_LIST_TARGET, ""
    ),
    "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
    "flatpak remotes --system --columns=name,url": CommandResult(0, "", ""),
}


def respond_to(
    mapping: dict[str, CommandResult], default: CommandResult | None = None
) -> Callable[..., CommandResult]:
    """Build a run_command side_effect matching by substring (first match wins)."""
    fallback = default if default is not None else CommandResult(exit_code=0, stdout="", stderr="")

    def _side_effect(cmd: str, **_: object) -> CommandResult:
        for pattern, result in mapping.items():
            if pattern in cmd:
                return result
        return fallback

    return _side_effect


class FakeFlatpakTarget:
    """A target whose `flatpak remotes` and `flatpak list` answers reflect what this run
    has written to it.

    The origin guarantee reads the target's REAL state — once before a ref install and once
    after it — so a static command-to-result table can no longer express the cases that
    decide it: a `remote-add` that landed, one that exited 0 and changed nothing, an install
    whose ref came from a same-named remote pointing somewhere else. Each is one line of
    setup here.

    Only measured flatpak behaviour is modelled: `remote-add --if-not-exists` is a no-op on
    a name that already exists (so it exits 0 and leaves the old URL), `remote-modify --url`
    repoints it, and `flatpak install <remote> <ref>` records that remote as the ref's
    origin. Everything else falls through to `responses`.
    """

    def __init__(
        self,
        *,
        remotes: dict[str, dict[str, str]] | None = None,
        refs: dict[tuple[str, str], str] | None = None,
        runtimes: dict[tuple[str, str], str] | None = None,
        unverified: set[tuple[str, str]] | None = None,
        filters: dict[tuple[str, str], str] | None = None,
        filter_blocks: set[str] | None = None,
        install_records_origin: str | None = None,
        install_lands: bool = True,
        responses: dict[str, CommandResult] | None = None,
    ) -> None:
        self.remotes: dict[str, dict[str, str]] = {"user": {}, "system": {}}
        for scope, entries in (remotes or {}).items():
            self.remotes[scope] = dict(entries)
        # (scope, ref) -> origin remote name. `runtimes` are refs the `--app` listing filters
        # out and the columns-only one reports, which is the difference the remote-deletion
        # count runs on.
        self.refs: dict[tuple[str, str], str] = {**(refs or {}), **(runtimes or {})}
        self.runtime_refs: frozenset[tuple[str, str]] = frozenset(runtimes or {})
        # (scope, name) pairs this target reports with the `no-gpg-verify` option.
        self.unverified: set[tuple[str, str]] = set(unverified or ())
        # (scope, name) -> the path this target reports in the `filter` column.
        self.filters: dict[tuple[str, str], str] = dict(filters or {})
        # Refs an ACTIVE filter on their origin remote makes `flatpak install` refuse — real
        # behaviour, measured on Flatpak 1.14.6 (`_apply_remote_filters`). Opted into per test
        # rather than modelled everywhere, since only the filter tests set a filter at all.
        self.filter_blocks: set[str] = set(filter_blocks or ())
        # What an `install` that exits 0 actually leaves behind. Both default to the honest
        # case; they exist so the post-install read-back has conditions to find, since the
        # whole point of that check is the outcomes an exit code cannot rule out.
        self.install_records_origin = install_records_origin
        self.install_lands = install_lands
        # Substring overrides, consulted BEFORE the modelled behaviour, so a test can make
        # one command fail without having to model failure inside the fake.
        self._responses = dict(responses or {})
        self.run_command = AsyncMock(side_effect=self._run)
        self.send_file = AsyncMock()

    def _run(self, cmd: str, **kwargs: object) -> CommandResult:  # noqa: PLR0911, PLR0912
        for pattern, result in self._responses.items():
            if pattern in cmd:
                return result
        words = [word for word in shlex.split(cmd) if not word.startswith("-")]
        if words and words[0] == "sudo":
            words = words[1:]
        if not words or words[0] != "flatpak" or len(words) < 2:
            return CommandResult(0, "", "")
        verb, positional = words[1], words[2:]
        scope = "system" if "--system" in cmd else "user"

        if verb == "remotes":
            lines = "".join(
                remote_row(
                    name,
                    url,
                    options="no-gpg-verify" if (scope, name) in self.unverified else "",
                    ref_filter=self.filters.get((scope, name), "-"),
                )
                for name, url in sorted(self.remotes[scope].items())
            )
            return CommandResult(0, lines, "")
        if verb == "list":
            lines = "".join(
                f"{ref.split('/')[0]}\t1.0\t{origin}\t{ref_scope}\t{ref}\n"
                for (ref_scope, ref), origin in sorted(self.refs.items())
                if not ("--app" in cmd and (ref_scope, ref) in self.runtime_refs)
            )
            return CommandResult(0, lines, "")
        if verb == "remote-add":
            name, url = positional
            self.remotes[scope].setdefault(name, url)
            self._apply_verification(cmd, scope, name)
            return CommandResult(0, "", "")
        if verb == "remote-modify":
            words_with_flags = shlex.split(cmd)
            if "--no-filter" in words_with_flags:
                _ = self.filters.pop((scope, positional[0]), None)
                return CommandResult(0, "", "")
            ref_filter = next((w for w in words_with_flags if w.startswith("--filter=")), None)
            if ref_filter is not None:
                self.filters[(scope, positional[0])] = ref_filter.removeprefix("--filter=")
                return CommandResult(0, "", "")
            url = next(word for word in words_with_flags if word.startswith("--url=")).removeprefix("--url=")
            self.remotes[scope][positional[0]] = url
            self._apply_verification(cmd, scope, positional[0])
            return CommandResult(0, "", "")
        if verb == "remote-delete":
            _ = self.remotes[scope].pop(positional[0], None)
            _ = self.filters.pop((scope, positional[0]), None)
            return CommandResult(0, "", "")
        if verb == "install":
            remote, ref = positional
            if ref in self.filter_blocks and (scope, remote) in self.filters:
                return CommandResult(1, "", f"error: Nothing matches {ref} in remote {remote}\n")
            if self.install_lands:
                self.refs[(scope, ref)] = self.install_records_origin or remote
            return CommandResult(0, "", "")
        if verb == "uninstall":
            _ = self.refs.pop((scope, positional[0]), None)
            return CommandResult(0, "", "")
        return CommandResult(0, "", "")

    def _apply_verification(self, cmd: str, scope: str, name: str) -> None:
        """`--no-gpg-verify` / `--gpg-verify` decide what the next `flatpak remotes` reports
        in the `options` column — which is what the origin check reads back.
        """
        if "--no-gpg-verify" in cmd:
            self.unverified.add((scope, name))
        elif "--gpg-verify" in cmd:
            self.unverified.discard((scope, name))


def make_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    fake_target: FakeFlatpakTarget | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, Any]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target: Any = fake_target if fake_target is not None else MagicMock()
    if fake_target is None:
        target.run_command = AsyncMock(side_effect=respond_to(target_responses or {}))
        # Awaited by the signing-key staging path (#215); a bare MagicMock attribute is not
        # awaitable, so it has to be an AsyncMock even where a test asserts it is unused.
        target.send_file = AsyncMock()
    context = JobContext(
        config={},
        source=source,
        target=target,
        event_bus=MagicMock(),
        session_id="test-1234",
        source_hostname="source-host",
        target_hostname="target-host",
        dry_run=dry_run,
    )
    return context, source, target


def all_calls(mock: Any) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


async def run_job(
    job: FlatpakSyncJob,
    *,
    approve: Callable[[ItemDiff], bool] = lambda _diff: True,
    expect_failures: bool = False,
) -> PackagePlan:
    """Drive plan -> accept_review -> apply, the only path on which remotes are derived.

    Converging a single diff by hand no longer exercises a remote at all in the add
    direction: nothing derives one until the review's decisions exist.
    """
    plan = await job.plan()
    outcome = ReviewOutcome(
        decisions={diff.item_id: (Decision.APPLY if approve(diff) else Decision.SKIP_ONCE) for diff in plan.diffs},
        was_interactive=True,
    )
    job.accept_review(plan, outcome)
    if expect_failures:
        with pytest.raises(PackageItemFailures):
            await job.apply()
    else:
        await job.apply()
    return plan


class TestCapture:
    """Tab-separated capture (RESEARCH: `flatpak list`/`flatpak remotes` name their
    own columns via `--columns`, so there is no header row to parse).
    """

    @pytest.mark.asyncio
    async def test_capture_source_items_parses_application_version_origin_scope(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, FLATPAK_LIST_SOURCE, "")}
        )
        job = FlatpakSyncJob(context)

        items = await job.capture_source_items()

        assert [item.application for item in items] == [
            "com.slack.Slack",
            "org.gnome.Podcasts",
            "org.gimp.GIMP",
            "org.example.SplitScope",
            "org.example.NeedsRemote",
        ]
        slack = items[0]
        assert slack.version == "4.50.0"
        assert slack.origin == "flathub"
        assert slack.scope == "system"

    @pytest.mark.asyncio
    async def test_same_application_both_scopes_yields_two_distinct_identities(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, FLATPAK_LIST_BOTH_SCOPES, "")}
        )
        job = FlatpakSyncJob(context)

        items = await job.capture_source_items()

        assert len(items) == 2
        assert items[0].item_id != items[1].item_id
        assert {item.scope for item in items} == {"user", "system"}

    @pytest.mark.asyncio
    async def test_unrecognized_installation_value_is_skipped(self) -> None:
        weird = "org.example.Weird\t1.0\tflathub\tcustom-install\torg.example.Weird/x86_64/stable\n"
        context, _source, _target = make_context(source_responses={"flatpak list --app": CommandResult(0, weird, "")})
        job = FlatpakSyncJob(context)

        assert await job.capture_source_items() == []

    @pytest.mark.asyncio
    async def test_no_apps_installed_yields_empty_list_not_a_crash(self) -> None:
        context, _source, _target = make_context(source_responses={"flatpak list --app": CommandResult(0, "", "")})
        job = FlatpakSyncJob(context)

        assert await job.capture_source_items() == []


# `flatpak list --columns=ref` prints `<application>/<arch>/<branch>` (measured live), and
# that exact string is what `flatpak install`/`flatpak uninstall` accept positionally.
_BETA_REF_LINE = "org.example.App\t2.0b\tflathub-beta\tuser\torg.example.App/x86_64/beta\n"
_STABLE_REF_LINE = "org.example.App\t1.0\tflathub\tuser\torg.example.App/x86_64/stable\n"


class TestRefIdentityCarriesTheBranch:
    """The full ref, not the bare application id, is both the identity and the command
    argument — a remote or a machine holding two branches of one id cannot resolve the
    bare id (measured: `Multiple branches available for org.mozilla.firefox`).
    """

    @pytest.mark.asyncio
    async def test_capture_asks_for_the_ref_column_and_keeps_it_on_the_item(self) -> None:
        context, source, _target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, _BETA_REF_LINE, "")}
        )
        job = FlatpakSyncJob(context)

        items = await job.capture_source_items()

        assert any(",ref" in cmd for cmd in all_calls(source))
        assert [item.ref for item in items] == ["org.example.App/x86_64/beta"]
        assert items[0].item_id == "flatpak:ref:user:org.example.App/x86_64/beta"

    @pytest.mark.asyncio
    async def test_two_branches_of_one_application_in_one_scope_are_two_items(self) -> None:
        """`(scope, application)` is not a unique key for a machine's own listing: keying
        on it folds the two rows into one and silently loses a ref.
        """
        context, _source, _target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, _STABLE_REF_LINE + _BETA_REF_LINE, "")}
        )
        job = FlatpakSyncJob(context)

        items = await job.capture_source_items()

        assert len({item.item_id for item in items}) == 2

    def test_origin_stays_out_of_the_identity(self) -> None:
        """The opposite ruling from branch, and for a measured reason: an install-plus-
        removal pair on an origin change cannot converge, because
        `flatpak install <other remote> <ref>` on an installed ref refuses.
        """
        common = {"application": "org.example.App", "version": "1.0", "scope": "user"}
        from_flathub = FlatpakItem(origin="flathub", ref="org.example.App/x86_64/stable", **common)  # pyright: ignore[reportArgumentType]
        from_beta = FlatpakItem(origin="flathub-beta", ref="org.example.App/x86_64/stable", **common)  # pyright: ignore[reportArgumentType]

        assert from_flathub.item_id == from_beta.item_id

    @pytest.mark.asyncio
    async def test_a_branch_change_reads_as_install_plus_removal_never_a_version_mismatch(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, _BETA_REF_LINE, ""),
                "flatpak remotes --user": CommandResult(
                    0, remote_row("flathub-beta", "https://dl.flathub.org/beta-repo/"), ""
                ),
            },
            target_responses={
                "flatpak list --app": CommandResult(0, _STABLE_REF_LINE, ""),
                "flatpak remotes --user": CommandResult(
                    0, remote_row("flathub-beta", "https://dl.flathub.org/beta-repo/"), ""
                ),
            },
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        ref_diffs = {d.item_id: d.action for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REF}
        assert ref_diffs == {
            "flatpak:ref:user:org.example.App/x86_64/beta": DiffAction.INSTALL,
            "flatpak:ref:user:org.example.App/x86_64/stable": DiffAction.REMOVE,
        }

    @pytest.mark.asyncio
    async def test_install_names_the_full_ref_after_the_remote(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, _BETA_REF_LINE, ""),
                "flatpak remotes --user": CommandResult(
                    0, remote_row("flathub-beta", "https://dl.flathub.org/beta-repo/"), ""
                ),
            },
            fake_target=FakeFlatpakTarget(remotes={"user": {"flathub-beta": "https://dl.flathub.org/beta-repo/"}}),
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.action == DiffAction.INSTALL)

        await job.converge(diff)

        install_cmd = next(c for c in all_calls(target) if "flatpak install" in c)
        assert install_cmd.rstrip().endswith("flathub-beta org.example.App/x86_64/beta")

    @pytest.mark.asyncio
    async def test_uninstall_names_the_full_ref(self) -> None:
        context, _source, target = make_context(
            target_responses={"flatpak list --app": CommandResult(0, _BETA_REF_LINE, "")},
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.action == DiffAction.REMOVE)

        await job.converge(diff)

        uninstall_cmd = next(c for c in all_calls(target) if "flatpak uninstall" in c)
        assert uninstall_cmd.rstrip().endswith("--user org.example.App/x86_64/beta")


class TestPlanDiff:
    """`plan()`'s flatpak-specific diff: install/remove/report_only for refs, removal-only
    for remotes (the add and change directions are derived, not reviewed).
    """

    @pytest.mark.asyncio
    async def test_full_diff_taxonomy(self) -> None:
        context, _source, _target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 6
        by_id = {diff.item_id: diff for diff in plan.diffs}

        # Missing on target -> install.
        assert by_id["flatpak:ref:system:com.slack.Slack/x86_64/stable"].action == DiffAction.INSTALL
        assert by_id["flatpak:ref:system:com.slack.Slack/x86_64/stable"].diff_class == DiffClass.MISSING_ON_TARGET

        # Version differs, same scope -> report_only, never a converge verb (D-04).
        gimp = by_id["flatpak:ref:user:org.gimp.GIMP/x86_64/stable"]
        assert gimp.action == DiffAction.REPORT_ONLY
        assert gimp.diff_class == DiffClass.VERSION_MISMATCH
        assert gimp.detail is not None
        assert "2.10" in gimp.detail
        assert "2.9" in gimp.detail

        # Same application, different scope on each machine -> one install, one
        # removal, never a single change (scope is identity, module docstring).
        assert by_id["flatpak:ref:user:org.example.SplitScope/x86_64/stable"].action == DiffAction.INSTALL
        assert by_id["flatpak:ref:system:org.example.SplitScope/x86_64/stable"].action == DiffAction.REMOVE

        # Extra on target -> removal, its own review group.
        assert by_id["flatpak:ref:user:com.spotify.Client/x86_64/stable"].action == DiffAction.REMOVE
        remove_group = next(g for g in plan.groups if g.action == "remove")
        install_group = next(g for g in plan.groups if g.action == "install")
        assert "flatpak:ref:user:com.spotify.Client/x86_64/stable" in {e.item_id for e in remove_group.entries}
        assert "flatpak:ref:user:com.spotify.Client/x86_64/stable" not in {e.item_id for e in install_group.entries}

        # Identical application/version/scope on both -> no diff at all.
        assert "flatpak:ref:user:org.gnome.Podcasts/x86_64/stable" not in by_id

        # The target lacks the system-scope flathub the source has. That is no longer a
        # review line at all: it travels because a ref approved this run comes from it.
        assert not any(d.item_class == ItemClass.FLATPAK_REMOTE for d in plan.diffs)


_FLATHUB_URL = "https://dl.flathub.org/repo/"
_BETA_URL = "https://dl.flathub.org/beta-repo/"
_FIREFOX_REF = "org.mozilla.firefox/x86_64/stable"
_FIREFOX_ID = f"flatpak:ref:user:{_FIREFOX_REF}"
_DEFAULT_ORIGIN_REMOTES = (("flathub", _FLATHUB_URL), ("flathub-beta", _BETA_URL))


def _ref_line(ref: str, version: str, origin: str, scope: str) -> str:
    return f"{ref.split('/', maxsplit=1)[0]}\t{version}\t{origin}\t{scope}\t{ref}\n"


def _remote_lines(*remotes: tuple[str, str]) -> str:
    return "".join(remote_row(name, url) for name, url in remotes)


def origin_pair_context(
    *,
    source_origin: str = "flathub",
    target_origin: str = "flathub",
    source_version: str = "128.0",
    target_version: str = "128.0",
    source_remotes: tuple[tuple[str, str], ...] = _DEFAULT_ORIGIN_REMOTES,
    target_remotes: tuple[tuple[str, str], ...] = _DEFAULT_ORIGIN_REMOTES,
    target_scope: str = "user",
    target_decisions: str | None = None,
) -> tuple[JobContext, MagicMock, Any]:
    """Two machines that each hold ONE ref, installed on both, each from a remote that
    machine configures for itself.

    This is the shape `_origin_refusal`/`_installed_origin_refusal` structurally cannot see:
    the ref is present on both sides, so no install is ever issued and neither guard runs.
    Each machine therefore gets its OWN remote list — the same name may carry a different
    URL on the two sides, which is the whole divergence — rather than one list shared by
    both, which would make the wrong-vendor case unrepresentable.
    """
    target_responses = {
        "flatpak list --app": CommandResult(
            0, _ref_line(_FIREFOX_REF, target_version, target_origin, target_scope), ""
        ),
        "flatpak remotes --user --columns=name,url": CommandResult(0, _remote_lines(*target_remotes), ""),
        "flatpak remotes --system --columns=name,url": CommandResult(0, _remote_lines(*target_remotes), ""),
    }
    if target_decisions is not None:
        target_responses["flatpak.decisions.yaml"] = CommandResult(0, target_decisions, "")
    return make_context(
        source_responses={
            "flatpak list --app": CommandResult(0, _ref_line(_FIREFOX_REF, source_version, source_origin, "user"), ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, _remote_lines(*source_remotes), ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, _remote_lines(*source_remotes), ""),
        },
        target_responses=target_responses,
    )


class TestRefOriginMismatch:
    """ADR-020 D-41: a ref present on both machines from different remotes is
    `ORIGIN_MISMATCH`, reported and never converged.

    The comparison runs on the remotes' URLs, never their names, so the two ways a name
    lies both come out right: a target `flathub` pointing at the beta repo is a different
    vendor, and a remote the two machines merely named differently is not.
    """

    @pytest.mark.asyncio
    async def test_two_differently_named_remotes_yield_one_report_only_diff(self) -> None:
        context, _source, _target = origin_pair_context(source_origin="flathub", target_origin="flathub-beta")
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 1
        diff = plan.diffs[0]
        assert diff.item_id == _FIREFOX_ID
        assert (diff.diff_class, diff.action) == (DiffClass.ORIGIN_MISMATCH, DiffAction.REPORT_ONLY)
        assert diff.detail is not None
        assert "flathub-beta" in diff.detail
        assert _FLATHUB_URL in diff.detail
        assert _BETA_URL in diff.detail

        # "Reported" is the whole of what this diff does, so it has to reach the screen:
        # a diff class nobody sees closes nothing.
        entry = next(e for group in plan.groups for e in group.entries if e.item_id == _FIREFOX_ID)
        assert entry.detail == diff.detail

    @pytest.mark.asyncio
    async def test_origin_mismatch_outranks_a_version_mismatch(self) -> None:
        """Two vendors' builds of one ref share no version scale, so "source has X, target
        has Y" would state a difference of degree where the real difference is of provenance.
        """
        context, _source, _target = origin_pair_context(
            source_origin="flathub",
            target_origin="flathub-beta",
            source_version="128.0",
            target_version="129.0beta",
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 1
        assert plan.diffs[0].diff_class == DiffClass.ORIGIN_MISMATCH
        assert plan.diffs[0].detail is not None
        assert "129.0beta" not in plan.diffs[0].detail

    @pytest.mark.asyncio
    async def test_one_name_pointing_at_two_vendors_is_a_mismatch(self) -> None:
        """The case a name comparison cannot see (`5fc3ac01`): both machines report the
        ref's origin as `flathub`, and the two `flathub`s are different vendors.
        """
        context, _source, _target = origin_pair_context(
            source_origin="flathub",
            target_origin="flathub",
            source_remotes=(("flathub", _FLATHUB_URL),),
            target_remotes=(("flathub", _BETA_URL),),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert [d.diff_class for d in plan.diffs] == [DiffClass.ORIGIN_MISMATCH]
        assert plan.diffs[0].detail is not None
        assert _FLATHUB_URL in plan.diffs[0].detail
        assert _BETA_URL in plan.diffs[0].detail

    @pytest.mark.asyncio
    async def test_a_remote_the_two_machines_merely_named_differently_is_not_a_mismatch(self) -> None:
        """One vendor under two labels. Reporting it would be noise about a label, and the
        label is not what D-41 replicates.
        """
        context, _source, _target = origin_pair_context(
            source_origin="flathub",
            target_origin="flathub-mirror",
            source_remotes=(("flathub", _FLATHUB_URL),),
            target_remotes=(("flathub-mirror", _FLATHUB_URL),),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert [d for d in plan.diffs if d.item_class is ItemClass.FLATPAK_REF] == []

    @pytest.mark.asyncio
    async def test_one_vendor_and_one_version_still_produces_no_diff(self) -> None:
        """Negative control: the ordinary case must stay silent, or the mismatch branch is
        reporting the machines rather than a divergence.
        """
        context, _source, _target = origin_pair_context()
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert plan.diffs == ()

    @pytest.mark.asyncio
    async def test_an_origin_naming_no_configured_remote_matches_nothing(self) -> None:
        """A ref whose origin remote the machine no longer configures has no URL, and an
        absent URL is a value of its own that matches nothing — not even the same name on the
        other machine (`PKG-FR-FLATPAK-ORIGIN-DIFF`). Falling back to the name would call two
        unresolvable origins one origin on no evidence at all.
        """
        context, _source, _target = origin_pair_context(
            source_origin="flathub",
            target_origin="flathub",
            source_remotes=(("flathub", _FLATHUB_URL),),
            target_remotes=(),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert [d.diff_class for d in plan.diffs] == [DiffClass.ORIGIN_MISMATCH]

    @pytest.mark.asyncio
    async def test_a_side_with_no_url_says_so_rather_than_printing_a_bare_name(self) -> None:
        """Both sides name `flathub`, so a detail that printed the names alone would report a
        divergence and show two identical strings. The missing URL is the finding.
        """
        context, _source, _target = origin_pair_context(
            source_origin="flathub",
            target_origin="flathub",
            source_remotes=(("flathub", _FLATHUB_URL),),
            target_remotes=(),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        detail = plan.diffs[0].detail
        assert detail is not None
        assert f"source-host installed it from flathub ({_FLATHUB_URL})" in detail
        assert "target-host from flathub (no URL: the machine does not configure flathub)" in detail

    @pytest.mark.asyncio
    async def test_an_unresolvable_origin_with_a_different_name_is_still_reported(self) -> None:
        context, _source, _target = origin_pair_context(
            source_origin="flathub",
            target_origin="flathub-beta",
            source_remotes=(("flathub", _FLATHUB_URL),),
            target_remotes=(),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert [d.diff_class for d in plan.diffs] == [DiffClass.ORIGIN_MISMATCH]
        assert plan.diffs[0].detail is not None
        assert plan.diffs[0].detail.endswith(
            "target-host from flathub-beta (no URL: the machine does not configure flathub-beta)"
        )

    @pytest.mark.asyncio
    async def test_the_mismatch_is_reported_and_never_converged(self) -> None:
        """`REPORT_ONLY` is forced by flatpak, not chosen: `flatpak install <other remote>
        <installed ref>` refuses on an already-installed ref, so approving the entry must
        still issue no install and no uninstall.
        """
        context, _source, target = origin_pair_context(source_origin="flathub", target_origin="flathub-beta")
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert not any("flatpak install" in cmd for cmd in all_calls(target))
        assert not any("flatpak uninstall" in cmd for cmd in all_calls(target))
        assert not any("remote-add" in cmd or "remote-modify" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_skip_always_on_the_targets_own_remote_does_not_hide_the_mismatch(self) -> None:
        """The URL maps are the UNFILTERED captures. A machine-local mark on a remote makes
        it inert as an item; letting it withdraw the URL as well would silently switch the
        wrong-vendor finding off on exactly the machine that recorded it.
        """
        context, _source, _target = origin_pair_context(
            source_origin="flathub",
            target_origin="flathub",
            source_remotes=(("flathub", _FLATHUB_URL),),
            target_remotes=(("flathub", _BETA_URL),),
            target_decisions=target_decision_file("flatpak:remote:user:flathub"),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert [d.diff_class for d in plan.diffs] == [DiffClass.ORIGIN_MISMATCH]

    @pytest.mark.asyncio
    async def test_a_scope_split_is_two_items_not_an_origin_mismatch(self) -> None:
        """Scope is identity, so the two sides are different items and never meet in the
        both-present arm at all — the mismatch branch must not reach across scopes.
        """
        context, _source, _target = origin_pair_context(
            source_origin="flathub", target_origin="flathub-beta", target_scope="system"
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert {(d.item_id, d.action) for d in plan.diffs} == {
            (_FIREFOX_ID, DiffAction.INSTALL),
            (f"flatpak:ref:system:{_FIREFOX_REF}", DiffAction.REMOVE),
        }


_SRC_URL = "https://dl.flathub.org/repo/"
_TGT_URL = "https://old.mirror.example.org/repo/"
_APP_LINE = "org.example.App\t1.0\tflathub\tuser\torg.example.App/x86_64/stable\n"
_APP_ID = "flatpak:ref:user:org.example.App/x86_64/stable"


def derivation_source(
    *,
    remotes: str = remote_row("flathub", _SRC_URL),
    apps: str = _APP_LINE,
    system_remotes: str = "",
    all_refs: str | None = None,
    runtime: str = "",
) -> dict[str, CommandResult]:
    """A source with `apps` installed and `remotes` configured in the user scope.

    `all_refs` (the runtime-carrying listing) and `runtime` (`flatpak info --show-runtime`)
    default to answering nothing, which is the ordinary case: an app whose runtime comes
    from its own remote derives nothing extra.
    """
    return {
        "flatpak list --app": CommandResult(0, apps, ""),
        "flatpak list --columns": CommandResult(0, all_refs if all_refs is not None else apps, ""),
        "--show-runtime": CommandResult(0, runtime, ""),
        "flatpak remotes --user": CommandResult(0, remotes, ""),
        "flatpak remotes --system": CommandResult(0, system_remotes, ""),
    }


class TestRemotesAreDerivedFromApprovedRefs:
    """ADR-020 D-41: a remote travels because an approved ref needs it, and is
    never a tickable line in the add or the change direction.

    The pairing this makes unrepresentable is the one that mattered: a ref approved and its
    only possible source declined does nothing, and a ref approved from a same-named remote
    whose URL change was declined installs another vendor's build.
    """

    @staticmethod
    def _remote_writes(target: Any) -> list[str]:
        return [cmd for cmd in all_calls(target) if "remote-add" in cmd or "remote-modify" in cmd]

    @pytest.mark.asyncio
    async def test_no_remote_appears_in_any_review_group(self) -> None:
        context, _source, target = make_context(source_responses=derivation_source(), fake_target=FakeFlatpakTarget())
        job = FlatpakSyncJob(context)

        plan = await run_job(job)

        assert not any(entry.item_id.startswith("flatpak:remote:") for group in plan.groups for entry in group.entries)
        assert self._remote_writes(target)

    @pytest.mark.asyncio
    async def test_a_remote_is_provisioned_before_the_ref_that_needed_it(self) -> None:
        context, _source, target = make_context(source_responses=derivation_source(), fake_target=FakeFlatpakTarget())

        await run_job(FlatpakSyncJob(context))

        commands = all_calls(target)
        add = next(i for i, cmd in enumerate(commands) if "remote-add" in cmd)
        install = next(i for i, cmd in enumerate(commands) if "flatpak install" in cmd)
        assert add < install
        assert "--user" in commands[add]
        assert target.remotes["user"]["flathub"] == _SRC_URL

    @pytest.mark.asyncio
    async def test_a_remote_no_approved_ref_needs_does_not_travel(self) -> None:
        context, _source, target = make_context(
            source_responses=derivation_source(
                remotes=remote_row("flathub", _SRC_URL) + remote_row("unused", "https://unused.example.org/repo/")
            ),
            fake_target=FakeFlatpakTarget(),
        )

        await run_job(FlatpakSyncJob(context))

        assert list(target.remotes["user"]) == ["flathub"]

    @pytest.mark.asyncio
    async def test_declining_the_ref_declines_its_remote(self) -> None:
        context, _source, target = make_context(source_responses=derivation_source(), fake_target=FakeFlatpakTarget())

        await run_job(FlatpakSyncJob(context), approve=lambda _diff: False)

        assert self._remote_writes(target) == []
        assert target.remotes["user"] == {}

    @pytest.mark.asyncio
    async def test_a_differing_url_is_repointed_with_no_review_line(self) -> None:
        context, _source, target = make_context(
            source_responses=derivation_source(),
            fake_target=FakeFlatpakTarget(remotes={"user": {"flathub": _TGT_URL}}),
        )
        job = FlatpakSyncJob(context)

        plan = await run_job(job)

        modify = next(cmd for cmd in all_calls(target) if "remote-modify" in cmd)
        assert f"--url={_SRC_URL}" in modify
        assert target.remotes["user"]["flathub"] == _SRC_URL
        assert not any(entry.item_id.startswith("flatpak:remote:") for group in plan.groups for entry in group.entries)

    @pytest.mark.asyncio
    async def test_a_remote_the_target_already_matches_is_not_written_at_all(self) -> None:
        context, _source, target = make_context(
            source_responses=derivation_source(),
            fake_target=FakeFlatpakTarget(remotes={"user": {"flathub": _SRC_URL}}),
        )

        await run_job(FlatpakSyncJob(context))

        assert self._remote_writes(target) == []

    @pytest.mark.asyncio
    async def test_a_user_scope_ref_derives_only_the_user_scope_remote(self) -> None:
        context, _source, target = make_context(
            source_responses=derivation_source(system_remotes=remote_row("flathub", _SRC_URL)),
            fake_target=FakeFlatpakTarget(),
        )

        await run_job(FlatpakSyncJob(context))

        writes = self._remote_writes(target)
        assert len(writes) == 1
        assert "--user" in writes[0]
        assert not writes[0].startswith("sudo ")
        assert target.remotes["system"] == {}

    @pytest.mark.asyncio
    async def test_the_same_remote_in_two_scopes_is_derived_once_per_scope(self) -> None:
        """A remote is per-installation even with a byte-identical URL, so a user ref and a
        system ref from `flathub` need two provisionings, not one.
        """
        system_app = "org.example.Sys\t1.0\tflathub\tsystem\torg.example.Sys/x86_64/stable\n"
        context, _source, target = make_context(
            source_responses=derivation_source(
                apps=_APP_LINE + system_app, system_remotes=remote_row("flathub", _SRC_URL)
            ),
            fake_target=FakeFlatpakTarget(),
        )

        await run_job(FlatpakSyncJob(context))

        writes = self._remote_writes(target)
        assert {"--user" in cmd for cmd in writes} == {True, False}
        assert target.remotes["user"] == {"flathub": _SRC_URL}
        assert target.remotes["system"] == {"flathub": _SRC_URL}

    @pytest.mark.asyncio
    async def test_the_runtime_an_approved_app_needs_brings_its_own_remote(self) -> None:
        """An app on one remote built against a runtime the source holds from another:
        deriving from the app's origin alone leaves the install unable to resolve its
        runtime.
        """
        runtime_ref = "org.example.Platform/x86_64/49"
        context, _source, target = make_context(
            source_responses=derivation_source(
                remotes=remote_row("flathub", _SRC_URL) + remote_row("runtimes", "https://runtimes.example.org/repo/"),
                all_refs=_APP_LINE + f"org.example.Platform\t49\truntimes\tuser\t{runtime_ref}\n",
                runtime=f"{runtime_ref}\n",
            ),
            fake_target=FakeFlatpakTarget(),
        )

        await run_job(FlatpakSyncJob(context))

        assert sorted(target.remotes["user"]) == ["flathub", "runtimes"]

    @pytest.mark.asyncio
    async def test_a_failed_derived_write_fails_only_the_ref_that_needed_it(self) -> None:
        second = "org.example.Other\t1.0\tsecond\tuser\torg.example.Other/x86_64/stable\n"
        target = FakeFlatpakTarget(
            responses={
                "remote-add --if-not-exists --user second": CommandResult(1, "", "GPG verification failed"),
            }
        )
        context, _source, _target = make_context(
            source_responses=derivation_source(
                remotes=remote_row("flathub", _SRC_URL) + remote_row("second", "https://second.example.org/repo/"),
                apps=_APP_LINE + second,
            ),
            fake_target=target,
        )
        job = FlatpakSyncJob(context)

        await run_job(job, expect_failures=True)

        assert ("user", "org.example.App/x86_64/stable") in target.refs
        assert ("user", "org.example.Other/x86_64/stable") not in target.refs

    @pytest.mark.asyncio
    async def test_a_failed_derived_write_names_the_remote_and_its_own_stderr(self) -> None:
        target = FakeFlatpakTarget(
            responses={"remote-add": CommandResult(1, "", "GPG verification failed")},
        )
        context, _source, _target = make_context(source_responses=derivation_source(), fake_target=target)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan, ReviewOutcome(decisions={diff.item_id: Decision.APPLY for diff in plan.diffs}, was_interactive=True)
        )
        with pytest.raises(PackageItemFailures):
            await job.apply()

        with pytest.raises(ConvergeItemFailed) as excinfo:
            await job.converge(next(d for d in plan.diffs if d.item_id == _APP_ID))
        assert "flathub" in str(excinfo.value)
        assert "GPG verification failed" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_every_derived_write_carries_mutates(self) -> None:
        context, _source, target = make_context(
            source_responses=derivation_source(),
            fake_target=FakeFlatpakTarget(remotes={"user": {"flathub": _TGT_URL}}),
        )

        await run_job(FlatpakSyncJob(context))

        for call in target.run_command.call_args_list:
            command = call.args[0]
            if "remote-add" in command or "remote-modify" in command:
                assert call.kwargs.get("mutates"), f"ungated write: {command}"

    @pytest.mark.asyncio
    async def test_a_dry_run_previews_the_derived_writes_and_issues_none(self) -> None:
        context, _source, target = make_context(
            source_responses=derivation_source(), fake_target=FakeFlatpakTarget(), dry_run=True
        )

        await run_job(FlatpakSyncJob(context))

        assert self._remote_writes(target) == []
        assert not any("flatpak install" in cmd for cmd in all_calls(target))


_KEPT_REF = "org.example.Kept/x86_64/stable"
_KEPT_ID = f"flatpak:ref:user:{_KEPT_REF}"
_FLATHUB_USER_ID = "flatpak:remote:user:flathub"
_FLATHUB_CONFLICT_ID = "flatpak:conflict:user:flathub"


def target_decision_file(*item_ids: str) -> str:
    """The TARGET's own decision file recording `item_ids` skip-always — the one and only
    definition of "machine-specific" (ADR-020 D-41), read straight off the file rather
    than inferred from the ref being target-only.
    """
    body = "".join(
        f'  "{item_id}":\n'
        "    item_class: flatpak_ref\n"
        f'    label: "{item_id}"\n'
        "    reason: null\n"
        "    recorded_at: '2026-07-25T00:00:00Z'\n"
        for item_id in item_ids
    )
    return f"machine_specific:\n{body}"


def conflict_target(
    *,
    kept_refs: tuple[str, ...] = (_KEPT_REF,),
    recorded: tuple[str, ...] = (_KEPT_ID,),
    remotes: dict[str, dict[str, str]] | None = None,
    unverified: set[tuple[str, str]] | None = None,
) -> FakeFlatpakTarget:
    """A target holding `kept_refs` from `flathub`, with `recorded` marked skip-always in its
    own decision file, and a `flathub` the source disagrees with.
    """
    return FakeFlatpakTarget(
        remotes=remotes if remotes is not None else {"user": {"flathub": _TGT_URL}},
        refs={("user", ref): "flathub" for ref in kept_refs},
        unverified=unverified,
        responses={"flatpak.decisions.yaml": CommandResult(0, target_decision_file(*recorded), "")},
    )


async def run_with_conflict_answer(
    job: FlatpakSyncJob, answer: Decision | None, *, expect_failures: bool = False
) -> PackagePlan:
    """Drive plan -> accept_review -> apply, approving every diff and answering every
    conflict entry with `answer` (`None` leaves it undecided, which is what a non-interactive
    run produces for a screen nobody saw).
    """
    plan = await job.plan()
    decisions = {diff.item_id: Decision.APPLY for diff in plan.diffs}
    if answer is not None:
        for group in plan.groups:
            for entry in group.entries:
                if entry.item_id.startswith("flatpak:conflict:"):
                    decisions[entry.item_id] = answer
    job.accept_review(plan, ReviewOutcome(decisions=decisions, was_interactive=True))
    if expect_failures:
        with pytest.raises(PackageItemFailures):
            await job.apply()
    else:
        await job.apply()
    return plan


def conflict_entries(plan: PackagePlan) -> list[ReviewEntry]:
    return [entry for group in plan.groups if group.action == REPO_CONFLICT_REVIEW_ACTION for entry in group.entries]


class TestARepointThatMovesAMachineSpecificRefIsAsked:
    """ADR-020 D-41: repointing a flatpak remote is silent mechanism EXCEPT
    when a ref the target recorded skip-always takes that remote as its origin.

    Machine-specific is the trigger, exactly as apt reads it from the target's decision file
    — deliberately NOT "a ref the target has and the source does not". A skip-always ref
    produces no diff in any run, so nothing else in the review would ever mention that
    repointing its remote changes where it updates from.
    """

    @staticmethod
    def _remote_writes(target: Any) -> list[str]:
        return [cmd for cmd in all_calls(target) if "remote-add" in cmd or "remote-modify" in cmd]

    @pytest.mark.asyncio
    async def test_a_machine_specific_ref_turns_the_repoint_into_a_two_answer_entry(self) -> None:
        context, _source, _target = make_context(source_responses=derivation_source(), fake_target=conflict_target())
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        entries = conflict_entries(plan)
        assert [entry.item_id for entry in entries] == [_FLATHUB_CONFLICT_ID]
        assert entries[0].label == "flathub remote (user)"
        assert entries[0].action_label == "overwrite"

    @pytest.mark.asyncio
    async def test_a_target_only_ref_is_not_machine_specific_and_the_repoint_stays_silent(self) -> None:
        """The wording that matters. A ref the target has and the source does not is an
        ordinary REMOVE candidate with a review line of its own; only an entry in the
        target's decision file makes a ref invisible enough to need this screen.
        """
        context, _source, target = make_context(
            source_responses=derivation_source(), fake_target=conflict_target(recorded=())
        )
        job = FlatpakSyncJob(context)

        plan = await run_with_conflict_answer(job, None)

        assert conflict_entries(plan) == []
        assert any("remote-modify" in cmd for cmd in self._remote_writes(target))
        assert target.remotes["user"]["flathub"] == _SRC_URL

    @pytest.mark.asyncio
    async def test_the_entry_shows_both_configurations_target_first_and_never_a_diff(self) -> None:
        context, _source, _target = make_context(source_responses=derivation_source(), fake_target=conflict_target())
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert conflict_entries(plan)[0].versions == (f"url: {_TGT_URL}", f"url: {_SRC_URL}")

    @pytest.mark.asyncio
    async def test_only_the_differing_facets_are_shown_and_a_trust_divergence_is_one_of_them(self) -> None:
        context, _source, _target = make_context(
            source_responses=derivation_source(),
            fake_target=conflict_target(unverified={("user", "flathub")}),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        target_version, source_version = conflict_entries(plan)[0].versions or ("", "")
        assert target_version.splitlines() == [f"url: {_TGT_URL}", "gpg verification: disabled"]
        assert source_version.splitlines() == [f"url: {_SRC_URL}", "gpg verification: enabled"]
        assert " vs " not in target_version + source_version

    @pytest.mark.asyncio
    async def test_the_detail_names_the_machine_specific_refs_that_are_the_reason(self) -> None:
        second = "org.example.AlsoKept/x86_64/stable"
        context, _source, _target = make_context(
            source_responses=derivation_source(),
            fake_target=conflict_target(
                kept_refs=(_KEPT_REF, second), recorded=(_KEPT_ID, f"flatpak:ref:user:{second}")
            ),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        detail = conflict_entries(plan)[0].detail or ""
        assert _KEPT_REF in detail
        assert second in detail

    @pytest.mark.asyncio
    async def test_finding_the_conflict_costs_no_command_of_its_own(self) -> None:
        """Unlike apt's file-level screen, which pays two `cat`s per entry: a remote's whole
        record is already on the item the diff was built from.
        """
        context, source, target = make_context(source_responses=derivation_source(), fake_target=conflict_target())
        quiet_context, quiet_source, quiet_target = make_context(
            source_responses=derivation_source(), fake_target=conflict_target(recorded=())
        )

        _ = await FlatpakSyncJob(context).plan()
        _ = await FlatpakSyncJob(quiet_context).plan()

        assert all_calls(source) == all_calls(quiet_source)
        assert all_calls(target) == all_calls(quiet_target)

    @pytest.mark.asyncio
    async def test_a_remote_the_target_lacks_is_an_add_and_never_a_conflict(self) -> None:
        """Nothing of the target's is being replaced, so there is nothing to put to the user
        — even though the machine-specific ref names a remote the target cannot resolve.
        """
        context, _source, target = make_context(
            source_responses=derivation_source(), fake_target=conflict_target(remotes={"user": {}})
        )
        job = FlatpakSyncJob(context)

        plan = await run_with_conflict_answer(job, None)

        assert conflict_entries(plan) == []
        assert any("remote-add" in cmd for cmd in self._remote_writes(target))

    @pytest.mark.asyncio
    async def test_a_remote_no_approved_ref_could_need_is_never_a_conflict(self) -> None:
        """A remote nothing derives is never written, so asking about it would describe a
        change this run was not going to make. The source's app comes from `other`, and only
        the machine-specific ref uses `flathub`.
        """
        other = "org.example.App\t1.0\tother\tuser\torg.example.App/x86_64/stable\n"
        context, _source, target = make_context(
            source_responses=derivation_source(
                apps=other,
                remotes=remote_row("flathub", _SRC_URL) + remote_row("other", "https://other.example.org/repo/"),
            ),
            fake_target=conflict_target(),
        )
        job = FlatpakSyncJob(context)

        plan = await run_with_conflict_answer(job, None)

        assert conflict_entries(plan) == []
        assert target.remotes["user"]["flathub"] == _TGT_URL

    @pytest.mark.asyncio
    async def test_a_signing_key_difference_alone_stays_silent(self) -> None:
        """`--gpg-import` merges into the remote's ostree keyring rather than replacing it,
        so importing the source's key can neither move the ref's origin nor withdraw trust —
        there is no harm behind it to put to the user.
        """
        source_responses = {
            **derivation_source(),
            "sha256sum $HOME": CommandResult(0, keyring_line(_SOURCE_KEY_DIGEST, _USER_KEYRING_DIR, "flathub"), ""),
        }
        context, _source, _target = make_context(
            source_responses=source_responses,
            fake_target=conflict_target(remotes={"user": {"flathub": _SRC_URL}}),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert conflict_entries(plan) == []

    @pytest.mark.asyncio
    async def test_overwrite_repoints_the_remote_and_installs_the_ref(self) -> None:
        context, _source, target = make_context(source_responses=derivation_source(), fake_target=conflict_target())
        job = FlatpakSyncJob(context)

        await run_with_conflict_answer(job, Decision.APPLY)

        assert target.remotes["user"]["flathub"] == _SRC_URL
        assert ("user", "org.example.App/x86_64/stable") in target.refs

    @pytest.mark.asyncio
    async def test_skip_once_leaves_the_targets_remote_exactly_as_it_was(self) -> None:
        context, _source, target = make_context(source_responses=derivation_source(), fake_target=conflict_target())
        job = FlatpakSyncJob(context)

        await run_with_conflict_answer(job, Decision.SKIP_ONCE, expect_failures=True)

        assert self._remote_writes(target) == []
        assert target.remotes["user"]["flathub"] == _TGT_URL

    @pytest.mark.asyncio
    async def test_skip_once_fails_the_ref_that_needed_the_source_url_naming_the_decision(self) -> None:
        """D-39, and the reason a skipped conflict is not the same as no conflict: the ref
        the user approved cannot be delivered from the origin they were shown, and installing
        it from the URL the target still has is the wrong-vendor outcome the whole origin
        guarantee exists to prevent.
        """
        context, _source, target = make_context(source_responses=derivation_source(), fake_target=conflict_target())
        job = FlatpakSyncJob(context)

        plan = await run_with_conflict_answer(job, Decision.SKIP_ONCE, expect_failures=True)

        assert ("user", "org.example.App/x86_64/stable") not in target.refs
        with pytest.raises(ConvergeItemFailed) as excinfo:
            await job.converge(next(d for d in plan.diffs if d.item_id == _APP_ID))
        assert "chose to keep target-host's own version" in str(excinfo.value)
        assert "flathub" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_undecided_conflict_is_a_skip_not_a_silent_overwrite(self) -> None:
        """What a non-interactive run produces: `review_items` never presented the screen, so
        no decision exists for it. Anything other than an explicit overwrite must leave the
        target's remote alone.
        """
        context, _source, target = make_context(source_responses=derivation_source(), fake_target=conflict_target())

        await run_with_conflict_answer(FlatpakSyncJob(context), None, expect_failures=True)

        assert target.remotes["user"]["flathub"] == _TGT_URL

    @pytest.mark.asyncio
    async def test_the_conflict_screen_is_neither_a_removal_direction_nor_promotable(self) -> None:
        """Two answers, recorded nowhere: the never-offer-again promotion must not follow it,
        and it must not arrive unticked as if it were a deletion.
        """
        assert not _is_promotable_group(REPO_CONFLICT_REVIEW_ACTION)
        assert not _is_removal_direction(REPO_CONFLICT_REVIEW_ACTION)

    @pytest.mark.asyncio
    async def test_a_conflict_id_marked_skip_always_reaches_no_decision_file(self) -> None:
        """Deliberate negative control — it must stay green. A conflict id labels no diff, so
        `_record_permanent_skips` cannot reach one however the decision arrived, including
        from a hand-assembled outcome; there is no prefix filter to break and this test
        exists to keep it that way. The ref decided the same way in the same run IS recorded,
        so the assertion is about the id and not about a recording pass that did nothing.
        """
        context, source, _target = make_context(source_responses=derivation_source(), fake_target=conflict_target())
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={
                    **{diff.item_id: Decision.SKIP_ALWAYS for diff in plan.diffs},
                    _FLATHUB_CONFLICT_ID: Decision.SKIP_ALWAYS,
                },
                was_interactive=True,
            ),
        )
        await job.apply()

        recorded = [cmd for cmd in all_calls(source) if "decisions.yaml.pcswitcher-tmp" in cmd]
        assert len(recorded) == 1
        assert _APP_ID in recorded[0]
        assert _FLATHUB_CONFLICT_ID not in recorded[0]


_USER_KEYRING_DIR = "$HOME/.local/share/flatpak/repo"
_SYSTEM_KEYRING_DIR = "/var/lib/flatpak/repo"
_SOURCE_KEY_DIGEST = "1111111111111111111111111111111111111111111111111111111111111111"
_TARGET_KEY_DIGEST = "2222222222222222222222222222222222222222222222222222222222222222"


def keyring_line(digest: str, directory: str, remote: str) -> str:
    """One `sha256sum` output line, exactly as the batched per-scope read produces it."""
    return f"{digest}  {directory}/{remote}.trustedkeys.gpg\n"


def write_source_keyring(installation: Path, remote: str) -> Path:
    """Create the source machine's own keyring file for `remote` under `installation`."""
    path = installation / "repo" / f"{remote}.trustedkeys.gpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_bytes(b"fake source key bytes")
    return path


class TestRemoteTrustCapture:
    """#215 — a remote's trust travels as part of the item: GPG verification read from
    the `options` column, the signing key as the sha256 of that scope's own
    `<repo>/<remote>.trustedkeys.gpg` (RESEARCH: verified live against Flatpak 1.14.6 /
    libostree 2024.5 — no flatpak command prints a remote's key).
    """

    def test_absent_options_column_means_a_verified_remote(self) -> None:
        items = _parse_flatpak_remotes(remote_row("flathub", "https://dl.flathub.org/repo/"), "user", {})

        assert len(items) == 1
        assert items[0].gpg_verify is True
        assert items[0].key_digest is None
        assert items[0].filter_path is None

    def test_a_short_line_parses_as_a_remote_with_nothing_after_its_url(self) -> None:
        """A trailing empty column is omitted rather than printed (verified live), so the
        two- and three-field widths stay readable alongside the four-field one.
        """
        items = _parse_flatpak_remotes(
            "flathub\thttps://dl.flathub.org/repo/\nmirror\thttps://example.org/repo/\tno-gpg-verify\n", "user", {}
        )

        assert [(item.name, item.gpg_verify, item.filter_path) for item in items] == [
            ("flathub", True, None),
            ("mirror", False, None),
        ]

    def test_no_gpg_verify_token_marks_the_remote_unverified_and_keyless(self) -> None:
        items = _parse_flatpak_remotes(
            remote_row("local", "file:///srv/repo", options="no-enumerate,no-gpg-verify"),
            "user",
            {"local": _SOURCE_KEY_DIGEST},
        )

        assert items[0].gpg_verify is False
        assert items[0].key_digest is None

    def test_other_options_do_not_read_as_no_gpg_verify(self) -> None:
        items = _parse_flatpak_remotes(
            remote_row("mirror", "https://example.org/repo/", options="no-enumerate"), "user", {}
        )

        assert items[0].gpg_verify is True

    def test_key_digest_joins_the_scope_map_by_remote_name(self) -> None:
        items = _parse_flatpak_remotes(
            remote_row("flathub", "https://dl.flathub.org/repo/"), "system", {"flathub": _SOURCE_KEY_DIGEST}
        )

        assert items[0].key_digest == _SOURCE_KEY_DIGEST
        assert items[0].item_id == "flatpak:remote:system:flathub"

    def test_keyring_digests_strip_only_the_fixed_suffix(self) -> None:
        """A remote name may contain dots, so only `.trustedkeys.gpg` comes off."""
        output = keyring_line(_SOURCE_KEY_DIGEST, _SYSTEM_KEYRING_DIR, "my.remote.name")

        assert _parse_keyring_digests(output) == {"my.remote.name": _SOURCE_KEY_DIGEST}

    def test_no_keyring_files_yields_no_digests(self) -> None:
        """The glob matches nothing, `sha256sum` prints nothing and exits 1 — a scope
        whose remotes all rely on a machine-level trust anchor, not an error.
        """
        assert _parse_keyring_digests("") == {}

    @pytest.mark.asyncio
    async def test_each_scope_reads_its_own_repo_directory_on_both_machines(self) -> None:
        context, source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)

        await job.plan()

        for calls in (all_calls(source), all_calls(target)):
            assert any(f"sha256sum {_USER_KEYRING_DIR}/*.trustedkeys.gpg" in c for c in calls)
            assert any(f"sha256sum {_SYSTEM_KEYRING_DIR}/*.trustedkeys.gpg" in c for c in calls)

    @pytest.mark.asyncio
    async def test_same_name_remote_in_each_scope_carries_its_own_key(self) -> None:
        """Scope stays identity: the two installations' `flathub` entries carry their own
        keyrings, so the captured items differ in exactly the scope whose key differs.
        """
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, "", ""),
                "flatpak remotes --user": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                "flatpak remotes --system": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                f"sha256sum {_USER_KEYRING_DIR}": CommandResult(
                    0, keyring_line(_SOURCE_KEY_DIGEST, _USER_KEYRING_DIR, "flathub"), ""
                ),
                f"sha256sum {_SYSTEM_KEYRING_DIR}": CommandResult(
                    0, keyring_line(_SOURCE_KEY_DIGEST, _SYSTEM_KEYRING_DIR, "flathub"), ""
                ),
            },
            target_responses={
                "flatpak list --app": CommandResult(0, "", ""),
                "flatpak remotes --user": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                "flatpak remotes --system": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                f"sha256sum {_USER_KEYRING_DIR}": CommandResult(
                    0, keyring_line(_SOURCE_KEY_DIGEST, _USER_KEYRING_DIR, "flathub"), ""
                ),
                f"sha256sum {_SYSTEM_KEYRING_DIR}": CommandResult(
                    0, keyring_line(_TARGET_KEY_DIGEST, _SYSTEM_KEYRING_DIR, "flathub"), ""
                ),
            },
        )
        job = FlatpakSyncJob(context)

        source = {item.item_id: item for item in await job._capture_all_source_remotes()}  # pyright: ignore[reportPrivateUsage]
        target = {item.item_id: item for item in await job._query_all_target_remotes()}  # pyright: ignore[reportPrivateUsage]

        assert source["flatpak:remote:user:flathub"] == target["flatpak:remote:user:flathub"]
        assert source["flatpak:remote:system:flathub"] != target["flatpak:remote:system:flathub"]
        assert target["flatpak:remote:system:flathub"].key_digest == _TARGET_KEY_DIGEST


def trust_responses(
    *,
    remote_line: str,
    key_digest: str | None,
    keyring_dir: str = _USER_KEYRING_DIR,
    scope_flag: str = "--user",
    apps: str = "",
) -> dict[str, CommandResult]:
    """One machine's flatpak responses for a single remote in a single scope.

    `apps` is what makes a remote travel at all now: nothing derives a remote until a ref
    from it is approved, so a trust test that installs nothing provisions nothing.
    """
    digest_output = keyring_line(key_digest, keyring_dir, remote_line.split("\t", maxsplit=1)[0]) if key_digest else ""
    return {
        "flatpak list --app": CommandResult(0, apps, ""),
        "flatpak list --columns": CommandResult(0, apps, ""),
        "--show-runtime": CommandResult(0, "", ""),
        f"flatpak remotes {scope_flag}": CommandResult(0, remote_line, ""),
        f"sha256sum {keyring_dir}": CommandResult(0, digest_output, ""),
        "echo $HOME": CommandResult(0, "/home/tester\n", ""),
    }


_TRUST_APP = {
    "user": "org.example.App\t1.0\tflathub\tuser\torg.example.App/x86_64/stable\n",
    "system": "org.example.App\t1.0\tflathub\tsystem\torg.example.App/x86_64/stable\n",
}


def trust_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    remote_line: str,
    key_digest: str | None,
    scope: str = "user",
    source_anchor: str | None = None,
    target_anchor: str | None = None,
    target_remotes: dict[str, dict[str, str]] | None = None,
    target_unverified: set[tuple[str, str]] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
) -> tuple[FlatpakSyncJob, FakeFlatpakTarget]:
    """A job whose source holds one app from `remote_line`'s remote, so that remote is
    derived and provisioned when the app is approved (#215's assertions, re-driven).

    `source_anchor`/`target_anchor` are digests for the machine-level keyring directory
    (relocated into `tmp_path`, since the real one is `/usr/share/ostree/trusted.gpg.d`).
    Equal digests mean the two machines already trust the same key.
    """
    scope_flag = "--user" if scope == "user" else "--system"
    anchor_dir = tmp_path / "ostree-anchors"
    monkeypatch.setattr(flatpak_sync, "_OSTREE_TRUSTED_ANCHOR_DIR", anchor_dir)
    source_anchor_responses: dict[str, CommandResult] = {}
    target_anchor_responses: dict[str, CommandResult] = {}
    if source_anchor is not None:
        anchor_dir.mkdir(parents=True, exist_ok=True)
        anchor_file = anchor_dir / "vendor.gpg"
        _ = anchor_file.write_bytes(b"fake machine anchor bytes")
        source_anchor_responses[f"sha256sum {anchor_dir}"] = CommandResult(0, f"{source_anchor}  {anchor_file}\n", "")
    if target_anchor is not None:
        target_anchor_responses[f"sha256sum {anchor_dir}"] = CommandResult(
            0, f"{target_anchor}  {anchor_dir}/whatever-the-target-calls-it.gpg\n", ""
        )
    if scope == "user":
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        installation = tmp_path / ".local" / "share" / "flatpak"
        # The user-scope read is a shell expression the remote shell expands, so it is
        # unaffected by the patched `Path.home()` the local file lookup uses.
        keyring_dir = _USER_KEYRING_DIR
    else:
        installation = tmp_path / "var-lib-flatpak"
        monkeypatch.setattr(flatpak_sync, "_FLATPAK_SYSTEM_INSTALLATION", installation)
        keyring_dir = f"{installation}/repo"
    if key_digest is not None:
        _ = write_source_keyring(installation, remote_line.split("\t", maxsplit=1)[0])
    target = FakeFlatpakTarget(
        remotes=target_remotes,
        unverified=target_unverified,
        responses={
            "echo $HOME": CommandResult(0, "/home/tester\n", ""),
            **target_anchor_responses,
            **(target_responses or {}),
        },
    )
    context, _source, _target = make_context(
        source_responses={
            **trust_responses(
                remote_line=remote_line,
                key_digest=key_digest,
                keyring_dir=keyring_dir,
                scope_flag=scope_flag,
                apps=_TRUST_APP[scope],
            ),
            **source_anchor_responses,
        },
        fake_target=target,
    )
    return FlatpakSyncJob(context), target


class TestRemoteTrustTravelsWithTheDerivedWrite:
    """#215 — provisioning a remote carries its key, so the ref installs that follow can
    actually verify their signatures. `--no-gpg-verify` is emitted only for a remote the
    SOURCE itself does not verify. Unchanged by derivation except in what triggers it: an
    approved ref rather than an approved remote item.
    """

    _URL = _SRC_URL
    _SIGNED = f"flathub\t{_URL}\n"
    _UNVERIFIED = f"flathub\t{_URL}\tno-gpg-verify\n"
    _STAGED = "/home/tester/.cache/pc-switcher/flatpak-staging/flatpak_remote_user_flathub.gpg"

    @pytest.mark.asyncio
    async def test_signed_remote_is_added_with_the_sources_own_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = trust_job(tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST)

        await run_job(job)

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert f"--gpg-import={self._STAGED}" in add_cmd
        assert "--no-gpg-verify" not in add_cmd
        # The key travels as bytes from the source's own keyring file (ADR-020 D-12).
        sent_local, sent_remote = target.send_file.call_args.args
        assert sent_local == tmp_path / ".local" / "share" / "flatpak" / "repo" / "flathub.trustedkeys.gpg"
        assert sent_remote == self._STAGED

    @pytest.mark.asyncio
    async def test_staging_stays_under_the_targets_own_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`send_file` is plain SFTP as the SSH user, so every staged byte lands under
        that user's home — never `/etc`, never the flatpak store.
        """
        job, target = trust_job(tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST)

        await run_job(job)

        _sent_local, sent_remote = target.send_file.call_args.args
        assert sent_remote.startswith("/home/tester/.cache/pc-switcher/")
        assert any("mkdir --parents /home/tester/.cache/pc-switcher/flatpak-staging" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_every_staging_write_carries_mutates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        job, target = trust_job(tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST)

        await run_job(job)

        assert target.send_file.call_args.kwargs["mutates"]
        for call in target.run_command.call_args_list:
            command = call.args[0]
            if "mkdir --parents" in command or "rm --force" in command or "remote-add" in command:
                assert call.kwargs.get("mutates"), f"ungated write: {command}"

    @pytest.mark.asyncio
    async def test_staged_key_is_discarded_even_when_remote_add_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = trust_job(
            tmp_path,
            monkeypatch,
            remote_line=self._SIGNED,
            key_digest=_SOURCE_KEY_DIGEST,
            target_responses={"remote-add": CommandResult(1, "", "boom")},
        )

        await run_job(job, expect_failures=True)

        assert any(f"rm --force {self._STAGED}" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_unverified_source_remote_replicates_as_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = trust_job(tmp_path, monkeypatch, remote_line=self._UNVERIFIED, key_digest=None)

        await run_job(job)

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert "--no-gpg-verify" in add_cmd
        assert "--gpg-import" not in add_cmd
        target.send_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_verified_source_remote_is_never_downgraded_even_if_the_target_is_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = trust_job(
            tmp_path,
            monkeypatch,
            remote_line=self._SIGNED,
            key_digest=_SOURCE_KEY_DIGEST,
            target_remotes={"user": {"flathub": self._URL}},
            target_unverified={("user", "flathub")},
        )

        await run_job(job)

        modify_cmd = next(c for c in all_calls(target) if "remote-modify" in c)
        assert "--no-gpg-verify" not in modify_cmd
        # Explicit re-enable: `remote-modify` is the only verb that accepts it, and
        # without it the target stays on `no-gpg-verify` (verified live).
        assert "--gpg-verify" in modify_cmd
        assert f"--gpg-import={self._STAGED}" in modify_cmd
        assert f"--url={self._URL}" in modify_cmd

    @pytest.mark.asyncio
    async def test_change_to_an_unverified_source_remote_disables_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The source's own state is what replicates: an unverified source remote lands
        unverified rather than converged into a lie.
        """
        job, target = trust_job(
            tmp_path,
            monkeypatch,
            remote_line=self._UNVERIFIED,
            key_digest=None,
            target_remotes={"user": {"flathub": "https://old.mirror.example.org/repo/"}},
        )

        await run_job(job)

        modify_cmd = next(c for c in all_calls(target) if "remote-modify" in c)
        assert "--no-gpg-verify" in modify_cmd
        assert "--gpg-import" not in modify_cmd

    @pytest.mark.asyncio
    async def test_system_scope_add_uses_sudo_and_still_stages_in_the_user_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = trust_job(
            tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST, scope="system"
        )

        await run_job(job)

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert add_cmd.startswith("sudo ")
        assert "--system" in add_cmd
        _sent_local, sent_remote = target.send_file.call_args.args
        assert sent_remote == "/home/tester/.cache/pc-switcher/flatpak-staging/flatpak_remote_system_flathub.gpg"
        assert "--gpg-import=/home/tester/.cache/pc-switcher/" in add_cmd

    @pytest.mark.asyncio
    async def test_missing_source_keyring_fails_the_ref_rather_than_provisioning_a_dead_remote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The digest was captured at plan time, so the file disappearing before the write
        is a real inconsistency — never a provision-anyway. The derived write has no item,
        so the refusal lands on the ref that needed it (D-39).
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no keyring file written
        target = FakeFlatpakTarget(responses={"echo $HOME": CommandResult(0, "/home/tester\n", "")})
        context, _source, _target = make_context(
            source_responses=trust_responses(
                remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST, apps=_TRUST_APP["user"]
            ),
            fake_target=target,
        )
        job = FlatpakSyncJob(context)

        await run_job(job, expect_failures=True)

        assert not any("remote-add" in c for c in all_calls(target))
        with pytest.raises(ConvergeItemFailed, match="signing key"):
            await job.converge(
                next(
                    d
                    for d in (await job.plan()).diffs
                    if d.item_id == "flatpak:ref:user:org.example.App/x86_64/stable"
                )
            )

    _STAGED_ANCHOR = "/home/tester/.cache/pc-switcher/flatpak-staging/flatpak_remote_user_flathub.anchor0.gpg"

    @pytest.mark.asyncio
    async def test_a_machine_level_anchor_the_target_lacks_becomes_the_remotes_own_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A remote verified through `/usr/share/ostree/trusted.gpg.d` holds no key of its
        own, and replicating name, URL and `gpg-verify` alone leaves the target refusing every
        install from it. The anchor is imported into the replicated remote's OWN keyring, so
        the target's remote trusts what the source's remote trusted and no other remote gains
        anything.
        """
        job, target = trust_job(
            tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=None, source_anchor=_SOURCE_KEY_DIGEST
        )

        await run_job(job)

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert f"--gpg-import={self._STAGED_ANCHOR}" in add_cmd
        assert "--no-gpg-verify" not in add_cmd
        sent_local, sent_remote = target.send_file.call_args.args
        assert sent_local == tmp_path / "ostree-anchors" / "vendor.gpg"
        assert sent_remote == self._STAGED_ANCHOR

    @pytest.mark.asyncio
    async def test_an_anchor_the_target_already_holds_is_not_imported_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same digest on both machines: the target already trusts that key, so importing it
        would be a write per run for no change. Its file name on the target is irrelevant —
        the digest is the identity.
        """
        job, target = trust_job(
            tmp_path,
            monkeypatch,
            remote_line=self._SIGNED,
            key_digest=None,
            source_anchor=_SOURCE_KEY_DIGEST,
            target_anchor=_SOURCE_KEY_DIGEST,
        )

        await run_job(job)

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert "--gpg-import" not in add_cmd
        assert "--no-gpg-verify" not in add_cmd
        target.send_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_anchor_reaches_a_remote_the_target_otherwise_already_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two anchor-trusted remotes compare equal whether or not the target holds the key,
        so whole-item equality cannot be what decides this one.
        """
        job, target = trust_job(
            tmp_path,
            monkeypatch,
            remote_line=self._SIGNED,
            key_digest=None,
            source_anchor=_SOURCE_KEY_DIGEST,
            target_anchor=_TARGET_KEY_DIGEST,
            target_remotes={"user": {"flathub": self._URL}},
        )

        await run_job(job)

        modify_cmd = next(c for c in all_calls(target) if "remote-modify" in c)
        assert f"--gpg-import={self._STAGED_ANCHOR}" in modify_cmd

    @pytest.mark.asyncio
    async def test_the_anchor_import_says_so_in_its_mutates_phrase(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = trust_job(
            tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=None, source_anchor=_SOURCE_KEY_DIGEST
        )

        await run_job(job)

        add_call = next(c for c in target.run_command.call_args_list if "remote-add" in c.args[0])
        assert "machine-level signing key" in add_call.kwargs["mutates"]

    @pytest.mark.asyncio
    async def test_a_verified_remote_with_no_key_anywhere_is_added_plainly_and_the_run_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing to sync, because the source holds nothing: that remote refuses every
        install on the source too, so the target inherits the source's own state — said out
        loud rather than left to flatpak's signature error on each ref.
        """
        job, target = trust_job(tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=None)

        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.base"):
            await run_job(job)

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert "--gpg-import" not in add_cmd
        assert "--no-gpg-verify" not in add_cmd
        target.send_file.assert_not_awaited()
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "flathub" in warnings[0]
        assert "/usr/share/ostree/trusted.gpg.d" not in warnings[0]  # the relocated dir, not the real one

    @pytest.mark.asyncio
    async def test_a_missing_anchor_file_fails_the_ref_rather_than_provisioning_a_dead_remote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same rule as a vanished per-remote keyring: the digest was captured at plan time,
        so the file disappearing before the write is a real inconsistency.
        """
        job, target = trust_job(
            tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=None, source_anchor=_SOURCE_KEY_DIGEST
        )
        (tmp_path / "ostree-anchors" / "vendor.gpg").unlink()

        await run_job(job, expect_failures=True)

        assert not any("remote-add" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_deletion_transfers_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`remote-delete` takes the per-remote keyring with it (verified live), so the
        deletion pass stages no key and needs no source lookup at all.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses=trust_responses(remote_line="", key_digest=None),
            fake_target=FakeFlatpakTarget(remotes={"user": {"flathub": self._URL}}),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert any("flatpak remote-delete --user flathub" in c for c in all_calls(target))
        target.send_file.assert_not_awaited()


class TestPlanReadOnly:
    @pytest.mark.asyncio
    async def test_plan_issues_no_mutating_flatpak_command(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)

        await job.plan()

        for cmd in all_calls(target):
            assert "flatpak install" not in cmd
            assert "flatpak uninstall" not in cmd
            assert "remote-add" not in cmd
            assert "remote-delete" not in cmd


def converge_target() -> FakeFlatpakTarget:
    """`TARGET_RESPONSES`' picture of the target, as a machine whose reads answer for the
    writes this run makes — required from the moment a ref install verifies the target's
    real remote and its own landed origin rather than trusting the plan.
    """
    return FakeFlatpakTarget(
        remotes={"user": {"flathub": _FLATHUB_URL}},
        refs={
            ("user", "org.gnome.Podcasts/x86_64/stable"): "flathub",
            ("user", "org.gimp.GIMP/x86_64/stable"): "flathub",
            ("user", "com.spotify.Client/x86_64/stable"): "flathub",
            ("system", "org.example.SplitScope/x86_64/stable"): "flathub",
        },
    )


class TestConverge:
    @pytest.mark.asyncio
    async def test_user_scope_ref_install_has_no_sudo_and_carries_user_flag(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, fake_target=converge_target())
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:org.example.SplitScope/x86_64/stable")

        await job.converge(diff)

        commands = all_calls(target)
        install_cmd = next(c for c in commands if "flatpak install" in c and "org.example.SplitScope" in c)
        assert "--user" in install_cmd
        assert "sudo" not in install_cmd

    @pytest.mark.asyncio
    async def test_system_scope_ref_install_uses_sudo_and_system_flag(self) -> None:
        target = converge_target()
        target.remotes["system"]["flathub"] = _FLATHUB_URL
        context, _source, _target = make_context(source_responses=SOURCE_RESPONSES, fake_target=target)
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        await job.converge(
            next(d for d in plan.diffs if d.item_id == "flatpak:ref:system:com.slack.Slack/x86_64/stable")
        )

        commands = all_calls(target)
        install_cmd = next(c for c in commands if "flatpak install" in c and "com.slack.Slack" in c)
        assert "--system" in install_cmd
        assert install_cmd.startswith("sudo ")

    @pytest.mark.asyncio
    async def test_ref_removal_never_needs_source_lookup(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, fake_target=converge_target())
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:com.spotify.Client/x86_64/stable")

        await job.converge(diff)

        commands = all_calls(target)
        assert any("flatpak uninstall --assumeyes --user com.spotify.Client" in c for c in commands)

    @pytest.mark.asyncio
    async def test_ref_with_missing_origin_remote_is_skipped_with_named_failure(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, fake_target=converge_target())
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:org.example.NeedsRemote/x86_64/stable")

        with pytest.raises(ConvergeItemFailed, match="customremote"):
            await job.converge(diff)

        assert not any("customremote" in c for c in all_calls(target) if "flatpak install" in c)


_REAL_FLATHUB = "https://dl.flathub.org/repo/"
_BETA_FLATHUB = "https://dl.flathub.org/beta-repo/"
_APP_REF = "org.mozilla.firefox/x86_64/stable"


class TestOriginIsReplicatedNotJustNamed:
    """ADR-020 D-41: a ref replicates as (ref, origin), and the origin is
    checked against the target's real state rather than inferred from a name.

    Measured against real Flathub: a target remote called `flathub` pointing at
    `https://dl.flathub.org/beta-repo/` serves a different vendor's build of
    `org.mozilla.firefox` — 148.0 / `org.flathub.Beta` versus 153.0 / `org.flathub.Stable`,
    a different commit and a different binary — and `flatpak install --assumeyes flathub
    <ref>` installs it at exit 0 with nothing said. `flatpak list --columns=origin` reports
    `flathub` in both cases, so only the URL separates them.
    """

    _SOURCE: ClassVar[dict[str, CommandResult]] = {
        "flatpak list --app": CommandResult(0, f"org.mozilla.firefox\t153.0\tflathub\tuser\t{_APP_REF}\n", ""),
        "flatpak remotes --user": CommandResult(0, f"flathub\t{_REAL_FLATHUB}\n", ""),
    }

    @staticmethod
    def _ref_install(plan: PackagePlan) -> ItemDiff:
        return next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REF)

    @pytest.mark.asyncio
    async def test_a_same_named_remote_pointing_elsewhere_refuses_the_install(self) -> None:
        """The live wrong-vendor case: the remote change is declined, the ref install is
        approved, and both remotes are called `flathub`.
        """
        target = FakeFlatpakTarget(remotes={"user": {"flathub": _BETA_FLATHUB}})
        context, _source, _target = make_context(source_responses=self._SOURCE, fake_target=target)
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        with pytest.raises(ConvergeItemFailed) as excinfo:
            await job.converge(self._ref_install(plan))

        assert _BETA_FLATHUB in str(excinfo.value)
        assert _REAL_FLATHUB in str(excinfo.value)
        assert not any("flatpak install" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_the_derived_write_repointing_the_remote_lets_the_same_install_through(self) -> None:
        """The other half of the fixture above, and what keeps that test from passing on
        the strength of something unrelated: once the derived write has really repointed the
        target's remote, the very same ref installs.
        """
        target = FakeFlatpakTarget(remotes={"user": {"flathub": _BETA_FLATHUB}})
        context, _source, _target = make_context(source_responses=self._SOURCE, fake_target=target)

        await run_job(FlatpakSyncJob(context))

        assert target.remotes["user"]["flathub"] == _REAL_FLATHUB
        assert target.refs[("user", _APP_REF)] == "flathub"

    @pytest.mark.asyncio
    async def test_a_remote_add_that_exited_zero_and_changed_nothing_refuses_the_install(self) -> None:
        """`flatpak remote-add --if-not-exists <name> <other url>` exits 0 and leaves the
        existing URL in place (measured), so neither the derived write's exit code nor the
        plan's own picture of the target is evidence — only re-reading the target is.
        """
        target = FakeFlatpakTarget()
        context, _source, _target = make_context(source_responses=self._SOURCE, fake_target=target)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        # Between plan and apply the target gains the name, pointing elsewhere. The derived
        # write is therefore an `--if-not-exists` add that succeeds and changes nothing.
        target.remotes["user"]["flathub"] = _BETA_FLATHUB
        job.accept_review(
            plan, ReviewOutcome(decisions={d.item_id: Decision.APPLY for d in plan.diffs}, was_interactive=True)
        )

        with pytest.raises(PackageItemFailures):
            await job.apply()

        assert any("remote-add" in cmd for cmd in all_calls(target))
        assert not any("flatpak install" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_remote_written_after_a_refusal_is_seen_on_the_next_attempt(self) -> None:
        """The read-back is cached per run, so a remote write has to discard it. Without
        that, the derived write would land and the ref install would still be judged against
        a picture of the target taken before this run wrote to it.
        """
        target = FakeFlatpakTarget()
        context, _source, _target = make_context(source_responses=self._SOURCE, fake_target=target)
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        with pytest.raises(ConvergeItemFailed, match="not configured on target-host"):
            await job.converge(self._ref_install(plan))
        job.accept_review(
            plan, ReviewOutcome(decisions={d.item_id: Decision.APPLY for d in plan.diffs}, was_interactive=True)
        )
        await job.apply()

        assert target.refs[("user", _APP_REF)] == "flathub"

    @pytest.mark.asyncio
    async def test_a_target_remote_that_does_not_verify_signatures_refuses_the_install(self) -> None:
        target = FakeFlatpakTarget(remotes={"user": {"flathub": _REAL_FLATHUB}}, unverified={("user", "flathub")})
        context, _source, _target = make_context(source_responses=self._SOURCE, fake_target=target)
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        with pytest.raises(ConvergeItemFailed, match="gpg verification"):
            await job.converge(self._ref_install(plan))

    @pytest.mark.asyncio
    async def test_a_ref_that_landed_from_another_repository_fails_after_the_install(self) -> None:
        """The read-back, not the pre-check: the target's remote is the source's, the
        install exits 0, and the ref nevertheless reports an origin resolving to a
        different URL.
        """
        target = FakeFlatpakTarget(
            remotes={"user": {"flathub": _REAL_FLATHUB, "mirror": _BETA_FLATHUB}},
            install_records_origin="mirror",
        )
        context, _source, _target = make_context(source_responses=self._SOURCE, fake_target=target)
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        with pytest.raises(ConvergeItemFailed) as excinfo:
            await job.converge(self._ref_install(plan))

        assert "mirror" in str(excinfo.value)
        assert _BETA_FLATHUB in str(excinfo.value)
        assert any("flatpak install" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_an_install_that_exited_zero_and_installed_nothing_fails_the_item(self) -> None:
        target = FakeFlatpakTarget(remotes={"user": {"flathub": _REAL_FLATHUB}}, install_lands=False)
        context, _source, _target = make_context(source_responses=self._SOURCE, fake_target=target)
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        with pytest.raises(ConvergeItemFailed, match="does not list"):
            await job.converge(self._ref_install(plan))

    @pytest.mark.asyncio
    async def test_a_recorded_source_remote_is_not_withheld_from_the_derivation(self) -> None:
        """A source remote is not reviewable in any direction, so a `skip always` sitting
        against one in the source's decision file must change nothing: it can neither
        withhold the remote an approved ref needs nor the URL its origin is checked against,
        and it must not turn the target's own copy into a removal proposal.
        """
        decisions = (
            "machine_specific:\n"
            '  "flatpak:remote:user:flathub":\n'
            "    item_class: flatpak_remote\n"
            '    label: "flathub remote (user)"\n'
            "    reason: null\n"
            "    recorded_at: '2026-07-25T00:00:00Z'\n"
        )
        target = FakeFlatpakTarget(remotes={"user": {"flathub": _REAL_FLATHUB}})
        context, _source, _target = make_context(
            source_responses={**self._SOURCE, "flatpak.decisions.yaml": CommandResult(0, decisions, "")},
            fake_target=target,
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        assert not any(d.item_class == ItemClass.FLATPAK_REMOTE for d in plan.diffs)
        result = await job.converge(self._ref_install(plan))

        assert result.success


class TestMaskParse:
    """`flatpak {--user|--system} mask` prints one pattern per line, each prefixed with
    two leading spaces and no header (RESEARCH: verified live, Flatpak 1.14.6) — parsed
    by stripping leading whitespace, unlike the tab-separated list commands.
    """

    def test_parses_two_leading_space_format_and_wildcard_patterns(self) -> None:
        output = (
            "  org.freedesktop.Platform.ffmpeg-full\n"
            "  app/com.example.Blocked/x86_64/*\n"
            "  runtime/org.gnome.*/x86_64/45\n"
        )

        items = _parse_flatpak_masks(output, "user")

        assert [item.pattern for item in items] == [
            "org.freedesktop.Platform.ffmpeg-full",
            "app/com.example.Blocked/x86_64/*",
            "runtime/org.gnome.*/x86_64/45",
        ]
        assert all(item.scope == "user" for item in items)

    def test_blank_lines_skipped_and_scope_is_the_passed_argument(self) -> None:
        output = "\n  org.example.Blocked\n\n"

        items = _parse_flatpak_masks(output, "system")

        assert [item.pattern for item in items] == ["org.example.Blocked"]
        assert items[0].scope == "system"
        assert items[0].item_id == "flatpak:mask:system:org.example.Blocked"

    def test_no_masks_yields_empty_list(self) -> None:
        assert _parse_flatpak_masks("", "user") == []


class TestMaskDiff:
    """Pure membership diff (#208, D-10): source-only -> INSTALL (mask), target-only ->
    REMOVE (unmask), present-both -> no diff. No CHANGE — a mask has no value to change.
    """

    @pytest.mark.asyncio
    async def test_source_user_mask_absent_on_target_yields_install(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, "  org.freedesktop.Platform.ffmpeg-full\n", "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        mask_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK]
        assert len(mask_diffs) == 1
        diff = mask_diffs[0]
        assert diff.item_id == "flatpak:mask:user:org.freedesktop.Platform.ffmpeg-full"
        assert diff.action == DiffAction.INSTALL
        assert diff.diff_class == DiffClass.MISSING_ON_TARGET

    @pytest.mark.asyncio
    async def test_target_only_system_mask_yields_removal(self) -> None:
        context, _source, _target = make_context(
            target_responses={"flatpak --system mask": CommandResult(0, "  org.example.Blocked\n", "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        mask_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK]
        assert len(mask_diffs) == 1
        diff = mask_diffs[0]
        assert diff.item_id == "flatpak:mask:system:org.example.Blocked"
        assert diff.action == DiffAction.REMOVE
        assert diff.diff_class == DiffClass.EXTRA_ON_TARGET
        # A removal lands in its own unticked removal group, never the install group.
        remove_group = next(g for g in plan.groups if g.action == "remove")
        assert "flatpak:mask:system:org.example.Blocked" in {e.item_id for e in remove_group.entries}

    @pytest.mark.asyncio
    async def test_mask_present_on_both_yields_no_diff(self) -> None:
        mask_line = "  org.example.Both\n"
        context, _source, _target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, mask_line, "")},
            target_responses={"flatpak --user mask": CommandResult(0, mask_line, "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.FLATPAK_MASK for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_masks_ordered_after_refs_in_diffs_tuple(self) -> None:
        # A ref install (source-only app) plus a mask install (source-only mask): the
        # mask diff must come AFTER the ref diff so it cannot suppress an auto-pulled
        # dependency of the ref being installed the same run (D-08).
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(
                    0, "org.example.App\t1.0\tflathub\tuser\torg.example.App/x86_64/stable\n", ""
                ),
                "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                "flatpak --user mask": CommandResult(0, "  org.example.Blocked\n", ""),
            },
            target_responses={
                "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
            },
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        ref_indices = [i for i, d in enumerate(plan.diffs) if d.item_class == ItemClass.FLATPAK_REF]
        mask_indices = [i for i, d in enumerate(plan.diffs) if d.item_class == ItemClass.FLATPAK_MASK]
        assert ref_indices
        assert mask_indices
        assert max(ref_indices) < min(mask_indices)


class TestMaskEditsAndScopeMoves:
    """#208 D-10 — masks are pure membership, so neither an edited pattern nor a moved
    scope is ever normalised into a single CHANGE: both read as remove-old + add-new and
    are reported exactly as found (the same rule refs and remotes already follow).
    """

    @pytest.mark.asyncio
    async def test_edited_pattern_reads_as_two_membership_diffs_never_a_change(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, "  org.example.Blocked/x86_64/24.08\n", "")},
            target_responses={"flatpak --user mask": CommandResult(0, "  org.example.Blocked/x86_64/23.08\n", "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        mask_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK]
        assert {(d.item_id, d.action) for d in mask_diffs} == {
            ("flatpak:mask:user:org.example.Blocked/x86_64/24.08", DiffAction.INSTALL),
            ("flatpak:mask:user:org.example.Blocked/x86_64/23.08", DiffAction.REMOVE),
        }
        assert not any(d.action == DiffAction.CHANGE for d in mask_diffs)

    @pytest.mark.asyncio
    async def test_scope_move_reads_as_add_system_plus_remove_user(self) -> None:
        """Scope is identity (module docstring), so the same pattern masked `system` on
        the source and `user` on the target is two independent items, not one move.
        """
        pattern = "org.freedesktop.Platform.ffmpeg-full"
        context, _source, _target = make_context(
            source_responses={"flatpak --system mask": CommandResult(0, f"  {pattern}\n", "")},
            target_responses={"flatpak --user mask": CommandResult(0, f"  {pattern}\n", "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        mask_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK]
        assert {(d.item_id, d.action) for d in mask_diffs} == {
            (f"flatpak:mask:system:{pattern}", DiffAction.INSTALL),
            (f"flatpak:mask:user:{pattern}", DiffAction.REMOVE),
        }

    @pytest.mark.asyncio
    async def test_mask_replicates_even_when_its_pattern_matches_no_installed_ref(self) -> None:
        """#208 D-10 — masks are captured from `flatpak mask`, never filtered against
        `flatpak list`: a pattern matching nothing installed on EITHER machine is still
        replicated, because it encodes the user's intent about future installs.
        """
        installed = "org.gnome.Podcasts\t1.0\tflathub\tuser\torg.gnome.Podcasts/x86_64/stable\n"
        # Both machines configure the ref's origin, so its origin resolves to one URL on both
        # sides and the ref stays out of the way of what this asserts.
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, installed, ""),
                "flatpak remotes --user": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                "flatpak --user mask": CommandResult(0, "  org.example.NeverInstalledAnywhere\n", ""),
            },
            target_responses={
                "flatpak list --app": CommandResult(0, installed, ""),
                "flatpak remotes --user": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
            },
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.FLATPAK_REF for d in plan.diffs)
        mask_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK]
        assert len(mask_diffs) == 1
        assert mask_diffs[0].item_id == "flatpak:mask:user:org.example.NeverInstalledAnywhere"
        assert mask_diffs[0].action == DiffAction.INSTALL


class TestMaskSkipAlways:
    @pytest.mark.asyncio
    async def test_recorded_mask_produces_no_diff_on_the_next_run(self) -> None:
        """A `skip always` recorded for a mask in the machine-local decision file makes it
        inert on the next run: masks reach `filter_inert` under the SAME item_id the
        decision was written against, so `plan()` never re-emits the diff.
        """
        pattern = "org.example.Blocked"
        item_id = f"flatpak:mask:user:{pattern}"
        decision_file = (
            "machine_specific:\n"
            f'  "{item_id}":\n'
            "    item_class: flatpak_mask\n"
            f'    label: "{pattern} (mask, user)"\n'
            "    reason: null\n"
            "    recorded_at: '2026-07-25T00:00:00Z'\n"
        )
        context, _source, _target = make_context(
            source_responses={
                "flatpak.decisions.yaml": CommandResult(0, decision_file, ""),
                "flatpak --user mask": CommandResult(0, f"  {pattern}\n", ""),
            },
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.FLATPAK_MASK for d in plan.diffs)


class TestMaskReviewVerbs:
    """#208 D3 — a mask item NEVER displays under an install/remove flatpak group.

    `_build_review_groups` keys the group title AND every entry's `action_label` off
    `_ACTION_VOCABULARY` by the group's own item class, so a `FLATPAK_MASK` INSTALL reads
    "mask" and a REMOVE reads "unmask" even when ref and remote diffs share those very
    actions in the same plan.
    """

    @staticmethod
    async def _mixed_plan() -> PackagePlan:
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(
                    0, "org.example.SourceOnly\t1.0\tflathub\tuser\torg.example.SourceOnly/x86_64/stable\n", ""
                ),
                "flatpak remotes --user --columns=name,url": CommandResult(
                    0, _FLATHUB_REMOTE_LINE + remote_row("srcremote", "https://src.example.org/repo/"), ""
                ),
                "flatpak --user mask": CommandResult(0, "  org.example.MaskNew\n", ""),
            },
            target_responses={
                "flatpak list --app": CommandResult(
                    0, "org.example.TargetOnly\t1.0\tflathub\tuser\torg.example.TargetOnly/x86_64/stable\n", ""
                ),
                "flatpak remotes --user --columns=name,url": CommandResult(
                    0, _FLATHUB_REMOTE_LINE + remote_row("tgtremote", "https://tgt.example.org/repo/"), ""
                ),
                "flatpak --user mask": CommandResult(0, "  org.example.MaskOld\n", ""),
            },
        )
        return await FlatpakSyncJob(context).plan()

    @staticmethod
    def _group_holding(plan: PackagePlan, item_id: str) -> ReviewGroup:
        return next(g for g in plan.groups if any(e.item_id == item_id for e in g.entries))

    @pytest.mark.asyncio
    async def test_mask_install_group_reads_mask_never_install(self) -> None:
        plan = await self._mixed_plan()

        group = self._group_holding(plan, "flatpak:mask:user:org.example.MaskNew")

        assert group.title == "Mask flatpak applications"
        assert [e.action_label for e in group.entries] == ["mask"]
        assert {e.item_id for e in group.entries} == {"flatpak:mask:user:org.example.MaskNew"}

    @pytest.mark.asyncio
    async def test_mask_remove_group_reads_unmask_and_is_removal_direction(self) -> None:
        plan = await self._mixed_plan()

        group = self._group_holding(plan, "flatpak:mask:user:org.example.MaskOld")

        assert group.title == "Unmask flatpak applications"
        assert [e.action_label for e in group.entries] == ["unmask"]
        assert _is_removal_direction(group.action)

    @pytest.mark.asyncio
    async def test_ref_groups_keep_their_own_verbs_and_exclude_masks(self) -> None:
        plan = await self._mixed_plan()

        ref_install = self._group_holding(plan, "flatpak:ref:user:org.example.SourceOnly/x86_64/stable")
        ref_remove = self._group_holding(plan, "flatpak:ref:user:org.example.TargetOnly/x86_64/stable")

        assert ref_install.title == "Install flatpak applications"
        assert ref_remove.title == "Remove flatpak applications"
        # Refs have no vocabulary entry of their own, so they fall back to the bare
        # DiffAction verb — which is exactly the verb a mask must NOT inherit.
        assert {e.action_label for e in ref_install.entries} == {"install"}
        assert {e.action_label for e in ref_remove.entries} == {"remove"}
        # Neither remote is offered anywhere: a remote is never a review item in any
        # direction (`PKG-FR-FLATPAK-REMOTE-DELETE`).
        assert not any(e.item_id.startswith("flatpak:remote:") for g in plan.groups for e in g.entries)
        for group in (ref_install, ref_remove):
            assert not any(e.item_id.startswith("flatpak:mask:") for e in group.entries)


class TestUnusedRemoteIsDeleted:
    """`PKG-FR-FLATPAK-REMOTE-DELETE` — a remote the source does not have is deleted once
    nothing on the target still uses it, and is a review item in no direction.

    "Nothing still uses it" is counted after the converge loop against what the target
    actually holds, so a ref installed identically on both machines keeps its remote even
    though that ref produces no diff of its own and never appears in the review at all.
    """

    _URL = "https://custom.example.org/repo/"
    _USER_REF = "org.example.NeedsRemote/x86_64/stable"
    _SYSTEM_REF = "org.example.SystemOnly/x86_64/stable"

    @staticmethod
    def _source(remotes: str = "", apps: str = "") -> dict[str, CommandResult]:
        return {
            "flatpak list --app": CommandResult(0, apps, ""),
            "flatpak list --columns": CommandResult(0, apps, ""),
            "flatpak remotes --user": CommandResult(0, remotes, ""),
            "flatpak remotes --system": CommandResult(0, "", ""),
        }

    @staticmethod
    def _deletions(target: FakeFlatpakTarget) -> list[str]:
        return [cmd for cmd in all_calls(target) if "remote-delete" in cmd]

    @pytest.mark.asyncio
    async def test_a_remote_nothing_uses_is_deleted_and_never_offered(self) -> None:
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(remotes={"user": {"customremote": self._URL}}),
        )
        job = FlatpakSyncJob(context)

        plan = await run_job(job)

        assert plan.diffs == ()
        assert plan.groups == ()
        assert self._deletions(target) == ["flatpak remote-delete --user customremote"]

    @pytest.mark.asyncio
    async def test_a_remote_a_target_ref_still_uses_stays(self) -> None:
        """The ref is installed on both machines, so it is not even a diff — and it is still
        what keeps the remote.
        """
        line = f"org.example.NeedsRemote\t1.0\tcustomremote\tuser\t{self._USER_REF}\n"
        context, _source, target = make_context(
            source_responses=self._source(remotes=remote_row("flathub", _FLATHUB_URL), apps=line),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL, "flathub": _FLATHUB_URL}},
                refs={("user", self._USER_REF): "customremote"},
            ),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert self._deletions(target) == []

    @pytest.mark.asyncio
    async def test_a_same_name_remote_in_the_other_scope_does_not_keep_it(self) -> None:
        """A remote is per-installation, so only same-scope refs use it. The system-scope ref
        below is installed on both machines, so nothing removes it and its own remote stays.
        """
        system_line = f"org.example.SystemOnly\t1.0\tcustomremote\tsystem\t{self._SYSTEM_REF}\n"
        context, _source, target = make_context(
            source_responses=self._source(apps=system_line),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL}, "system": {"customremote": self._URL}},
                refs={("system", self._SYSTEM_REF): "customremote"},
            ),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert self._deletions(target) == ["flatpak remote-delete --user customremote"]

    @pytest.mark.asyncio
    async def test_a_machine_specific_app_keeps_its_remote(self) -> None:
        """A skip-always ref is dropped before the diff and produces no review line in any
        run, so counting anything but the machine's real state would delete the remote
        feeding software the user told this tool to leave alone.
        """
        item_id = f"flatpak:ref:user:{self._USER_REF}"
        decisions = (
            "machine_specific:\n"
            f'  "{item_id}":\n'
            "    item_class: flatpak_ref\n"
            f'    label: "{self._USER_REF}"\n'
            "    reason: null\n"
            "    recorded_at: 2026-01-01T00:00:00Z\n"
        )
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL}},
                refs={("user", self._USER_REF): "customremote"},
                responses={"flatpak.decisions.yaml": CommandResult(0, decisions, "")},
            ),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert self._deletions(target) == []

    @pytest.mark.asyncio
    async def test_a_runtime_keeps_its_remote(self) -> None:
        """Counted over every installed ref, runtimes included: a runtime installed from a
        remote loses its updates just as an app does.
        """
        runtime = "org.example.Platform/x86_64/24.08"
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL}}, runtimes={("user", runtime): "customremote"}
            ),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert self._deletions(target) == []

    @pytest.mark.asyncio
    async def test_a_remote_the_source_has_is_never_deleted(self) -> None:
        context, _source, target = make_context(
            source_responses=self._source(remotes=remote_row("customremote", self._URL)),
            fake_target=FakeFlatpakTarget(remotes={"user": {"customremote": self._URL}}),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert self._deletions(target) == []

    @pytest.mark.asyncio
    async def test_the_count_runs_after_this_runs_approved_removals(self) -> None:
        """The target's only user of the remote is uninstalled by this very run, so the
        remote is free by the time the count happens.
        """
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL}}, refs={("user", self._USER_REF): "customremote"}
            ),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert any("flatpak uninstall" in cmd for cmd in all_calls(target))
        assert self._deletions(target) == ["flatpak remote-delete --user customremote"]

    @pytest.mark.asyncio
    async def test_a_removal_the_user_declined_keeps_the_remote(self) -> None:
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL}}, refs={("user", self._USER_REF): "customremote"}
            ),
        )
        job = FlatpakSyncJob(context)

        await run_job(job, approve=lambda _diff: False)

        assert self._deletions(target) == []

    @pytest.mark.asyncio
    async def test_a_removal_that_failed_keeps_the_remote(self) -> None:
        """A measurement, not a prediction: the ref the user approved for removal is still
        installed, so its remote is still in use.
        """
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL}},
                refs={("user", self._USER_REF): "customremote"},
                responses={"flatpak uninstall": CommandResult(1, "", "error: busy\n")},
            ),
        )
        job = FlatpakSyncJob(context)

        await run_job(job, expect_failures=True)

        assert self._deletions(target) == []

    @pytest.mark.asyncio
    async def test_a_system_scope_deletion_uses_sudo(self) -> None:
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(remotes={"system": {"customremote": self._URL}}),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert self._deletions(target) == ["sudo flatpak remote-delete --system customremote"]

    @pytest.mark.asyncio
    async def test_the_deletion_carries_mutates(self) -> None:
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(remotes={"user": {"customremote": self._URL}}),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        call = next(c for c in target.run_command.call_args_list if "remote-delete" in c.args[0])
        assert "customremote" in call.kwargs["mutates"]

    @pytest.mark.asyncio
    async def test_a_dry_run_previews_the_deletion_and_issues_none(self, caplog: pytest.LogCaptureFixture) -> None:
        context, _source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL}}, refs={("user", self._USER_REF): "customremote"}
            ),
            dry_run=True,
        )
        job = FlatpakSyncJob(context)

        with caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"):
            await run_job(job)

        # The ref removal has not run, so the preview subtracts it itself rather than
        # reporting the remote as still in use.
        assert any("Would delete user flatpak remote customremote" in r.message for r in caplog.records)
        assert self._deletions(target) == []

    @pytest.mark.asyncio
    async def test_a_deletion_that_fails_warns_and_fails_no_item(self, caplog: pytest.LogCaptureFixture) -> None:
        """No approved item depended on it, so there is nothing to charge the failure to."""
        context, _source, _target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(
                remotes={"user": {"customremote": self._URL}},
                responses={"remote-delete": CommandResult(1, "", "error: in use\n")},
            ),
        )
        job = FlatpakSyncJob(context)

        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.base"):
            await run_job(job)

        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "customremote" in warnings[0]
        assert "error: in use" in warnings[0]

    @pytest.mark.asyncio
    async def test_no_decision_file_entry_is_ever_written_for_a_remote(self) -> None:
        """Not merely unoffered: a SKIP_ALWAYS arriving from anywhere writes nothing."""
        context, source, target = make_context(
            source_responses=self._source(),
            fake_target=FakeFlatpakTarget(remotes={"user": {"customremote": self._URL}}),
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        job.accept_review(
            plan,
            ReviewOutcome(
                decisions={d.item_id: Decision.SKIP_ALWAYS for d in plan.diffs}
                | {"flatpak:remote:user:customremote": Decision.SKIP_ALWAYS},
                was_interactive=True,
            ),
        )
        await job.apply()

        for executor in (source, target):
            assert not any("mv --force" in c for c in all_calls(executor)), (
                "a remote reached the machine-local decision file"
            )


class TestMaskConverge:
    """`[sudo] flatpak {--user|--system} mask [--remove] <pattern>` (#208, D-10): scope +
    pattern recovered from the item_id (no source-side lookup), sudo iff system scope.
    """

    @pytest.mark.asyncio
    async def test_user_scope_mask_install_runs_mask_without_sudo(self) -> None:
        pattern = "org.freedesktop.Platform.ffmpeg-full"
        context, _source, target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, f"  {pattern}\n", "")},
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK)

        await job.converge(diff)

        # The capture call is `flatpak --user mask` (no pattern); only converge carries
        # the pattern, so filtering by it uniquely selects the mutating command.
        mask_cmd = next(c for c in all_calls(target) if pattern in c)
        assert "--user" in mask_cmd
        assert "sudo" not in mask_cmd
        assert "--remove" not in mask_cmd
        assert mask_cmd.rstrip().endswith(pattern)

    @pytest.mark.asyncio
    async def test_system_scope_mask_removal_uses_sudo_and_remove_flag(self) -> None:
        pattern = "org.example.Blocked"
        context, _source, target = make_context(
            target_responses={"flatpak --system mask": CommandResult(0, f"  {pattern}\n", "")},
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_MASK)

        await job.converge(diff)

        mask_cmd = next(c for c in all_calls(target) if pattern in c and "--remove" in c)
        assert mask_cmd.startswith("sudo ")
        assert "--system" in mask_cmd
        assert "--remove" in mask_cmd
        assert mask_cmd.rstrip().endswith(pattern)


class TestMaskSystemScopeGate:
    """A system-scope mask on either machine (#208, D-07) writes into `/var/lib/flatpak`
    just like a system remote, so it flips `_system_scope_in_play` and requires target
    sudo; a user-scope-only mask never does.
    """

    @pytest.mark.asyncio
    async def test_system_scope_mask_requires_target_sudo(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak --system mask": CommandResult(0, "  org.example.Blocked\n", "")},
            target_responses={"sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required")},
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_user_scope_only_mask_never_checks_sudo(self) -> None:
        context, _source, target = make_context(
            source_responses={"flatpak --user mask": CommandResult(0, "  org.example.UserOnly\n", "")},
        )
        job = FlatpakSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []
        assert not any("sudo --non-interactive true" in c for c in all_calls(target))


class TestRemoteFilterReplicates:
    """`PKG-FR-FLATPAK-FILTER` — a remote the source restricts with a filter is replicated
    with it: the file copied byte-for-byte to the same absolute path on the target and
    re-applied there, derived rather than reviewed.

    Measured in a stock `ubuntu:24.04` container, Flatpak 1.14.6: `flatpak remote-modify
    --filter=<path> <name>` exits 0 and stores the path VERBATIM as `xa.filter` in the
    installation's `repo/config` — unvalidated, so a relative path and a path that does not
    exist are both accepted. The path is all flatpak records; the content is an ordinary file
    at that path, which is what makes carrying it possible at all.
    """

    _URL = "https://custom.example.org/repo/"
    _APP_LINE = "org.example.App\t1.0\tcustomremote\tuser\torg.example.App/x86_64/stable\n"
    _APP_ID = "flatpak:ref:user:org.example.App/x86_64/stable"

    def _source(self, ref_filter: str, *, extra: str = "") -> dict[str, CommandResult]:
        return derivation_source(
            remotes=remote_row("customremote", self._URL, ref_filter=ref_filter) + extra, apps=self._APP_LINE
        )

    @staticmethod
    def _filter_file(tmp_path: Path) -> Path:
        path = tmp_path / "filters" / "custom.filter"
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text("org.example.*\n")
        return path

    @classmethod
    def _target(
        cls,
        responses: dict[str, CommandResult] | None = None,
        *,
        carries: str | None = None,
        filter_blocks: set[str] | None = None,
    ) -> FakeFlatpakTarget:
        """`carries` gives the target its OWN filter on the remote this run derives — the
        state the ordering rule is really about, and the one the fake used to model as absent.
        """
        return FakeFlatpakTarget(
            remotes={"user": {"customremote": cls._URL}} if carries is not None else None,
            filters={("user", "customremote"): carries} if carries is not None else None,
            filter_blocks=filter_blocks,
            responses={"echo $HOME": CommandResult(0, "/home/tgt\n", ""), **(responses or {})},
        )

    def test_the_filter_column_is_read_next_to_the_options_column(self) -> None:
        """Two independent columns: a filtered remote's verification state must parse exactly
        as an unfiltered one's, and vice versa.
        """
        signed = _parse_flatpak_remotes(remote_row("a", "https://example.org/a/", ref_filter="/etc/a.f"), "user", {})
        unsigned = _parse_flatpak_remotes(
            remote_row("b", "https://example.org/b/", options="no-gpg-verify", ref_filter="/etc/b.f"), "user", {}
        )
        plain = _parse_flatpak_remotes(remote_row("c", "https://example.org/c/"), "user", {})

        assert (signed[0].filter_path, signed[0].gpg_verify) == ("/etc/a.f", True)
        assert (unsigned[0].filter_path, unsigned[0].gpg_verify) == ("/etc/b.f", False)
        assert (plain[0].filter_path, plain[0].gpg_verify) == (None, True)

    def test_a_filter_difference_never_makes_two_remotes_compare_unequal(self) -> None:
        """`filter_path` is `compare=False` on purpose: the filter is converged by its own
        later pass, so letting it into `__eq__` would make `_write_derived_remote`'s
        whole-item equality test miss and issue a `remote-modify --url` that changes nothing,
        on every run.
        """
        filtered = FlatpakRemoteItem(name="r", url="https://example.org/r/", scope="user", filter_path="/etc/r.f")
        plain = FlatpakRemoteItem(name="r", url="https://example.org/r/", scope="user")

        assert filtered == plain

    @pytest.mark.asyncio
    async def test_the_file_is_copied_to_the_same_absolute_path_and_re_applied(self, tmp_path: Path) -> None:
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(source_responses=self._source(str(path)), fake_target=self._target())
        job = FlatpakSyncJob(context)

        await run_job(job)

        staged = target.send_file.await_args_list[-1].args[1]
        assert staged.startswith("/home/tgt/.cache/pc-switcher/")
        assert target.send_file.await_args_list[-1].args[0] == path
        assert any(f"install --mode=0644 {staged} {path}" in cmd for cmd in all_calls(target))
        assert any(f"flatpak remote-modify --user --filter={path} customremote" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_the_filter_lands_after_the_app_it_could_exclude(self, tmp_path: Path) -> None:
        """A filter can be narrower than what the source has installed, so applying it first
        would exclude the very ref being replicated: flatpak refuses an install its remote's
        filter denies, measured on 1.14.6, and leaves an already-installed ref alone. The
        ordering is what turns the first into the second.
        """
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(source_responses=self._source(str(path)), fake_target=self._target())
        job = FlatpakSyncJob(context)

        await run_job(job)

        calls = all_calls(target)
        install = next(i for i, cmd in enumerate(calls) if "flatpak install" in cmd)
        applied = next(i for i, cmd in enumerate(calls) if "--filter=" in cmd)
        assert install < applied

    @pytest.mark.asyncio
    async def test_the_filter_the_target_already_carries_comes_off_before_the_install(self, tmp_path: Path) -> None:
        """The late pass covers the source's filter; the target's own is in force throughout
        the installs unless it is taken off first.
        """
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(
            source_responses=self._source(str(path)), fake_target=self._target(carries="/etc/old.filter")
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        calls = all_calls(target)
        cleared = next(i for i, cmd in enumerate(calls) if "--no-filter" in cmd)
        install = next(i for i, cmd in enumerate(calls) if "flatpak install" in cmd)
        applied = next(i for i, cmd in enumerate(calls) if "--filter=" in cmd)
        assert cleared < install < applied
        assert target.filters == {("user", "customremote"): str(path)}

    @pytest.mark.asyncio
    async def test_a_filter_the_target_carries_does_not_refuse_the_app_being_replicated(self, tmp_path: Path) -> None:
        """The end the ordering exists for, with flatpak's refusal modelled: the app installs
        because no filter is in force while it does.
        """
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(
            source_responses=self._source(str(path)),
            fake_target=self._target(carries="/etc/old.filter", filter_blocks={"org.example.App/x86_64/stable"}),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert ("user", "org.example.App/x86_64/stable") in target.refs

    @pytest.mark.asyncio
    async def test_a_filter_the_source_no_longer_has_is_taken_off_the_target(self, tmp_path: Path) -> None:
        """`PKG-FR-MANAGER-CONVERGES`: the source-driven late pass has nothing to iterate for
        an unfiltered source remote, so without the clear the target keeps its filter for good.
        """
        _ = self._filter_file(tmp_path)
        context, _source, target = make_context(
            source_responses=self._source("-"), fake_target=self._target(carries="/etc/old.filter")
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert target.filters == {}
        assert not any("--filter=" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_clear_that_fails_fails_the_app_naming_the_remote_and_the_filter(self, tmp_path: Path) -> None:
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(
            source_responses=self._source(str(path)),
            fake_target=self._target(
                {"--no-filter": CommandResult(1, "", "error: no such remote\n")}, carries="/etc/old.filter"
            ),
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan, ReviewOutcome(decisions={d.item_id: Decision.APPLY for d in plan.diffs}, was_interactive=True)
        )

        with pytest.raises(PackageItemFailures) as raised:
            await job.apply()

        failed = {diff.item_id: reason for diff, reason in raised.value.failures}
        assert list(failed) == [self._APP_ID]
        assert "customremote" in failed[self._APP_ID]
        assert "/etc/old.filter" in failed[self._APP_ID]
        assert not any("flatpak install" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_dry_run_previews_the_clear_and_issues_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(
            source_responses=self._source(str(path)),
            fake_target=self._target(carries="/etc/old.filter"),
            dry_run=True,
        )
        job = FlatpakSyncJob(context)

        with caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"):
            await run_job(job)

        assert any("/etc/old.filter" in record.message for record in caplog.records)
        assert not any("--no-filter" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_remote_with_no_filter_on_either_machine_is_never_cleared(self, tmp_path: Path) -> None:
        """Negative control: the clear runs on what the target reports, not on every remote."""
        _ = self._filter_file(tmp_path)
        context, _source, target = make_context(source_responses=self._source("-"), fake_target=self._target())
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert not any("--no-filter" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_the_filter_is_no_review_item(self, tmp_path: Path) -> None:
        path = self._filter_file(tmp_path)
        context, _source, _target = make_context(source_responses=self._source(str(path)), fake_target=self._target())
        job = FlatpakSyncJob(context)

        plan = await run_job(job)

        assert not any("filter" in entry.item_id for group in plan.groups for entry in group.entries)
        assert not any(entry.item_id.startswith("flatpak:remote:") for group in plan.groups for entry in group.entries)

    @pytest.mark.asyncio
    async def test_an_unfiltered_remote_applies_none(self, tmp_path: Path) -> None:
        """Negative control: without it the pass could be running on every derived remote."""
        _ = self._filter_file(tmp_path)
        context, _source, target = make_context(source_responses=self._source("-"), fake_target=self._target())
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert not any("--filter=" in cmd for cmd in all_calls(target))
        target.send_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_remote_no_approved_ref_needs_carries_no_filter_either(self, tmp_path: Path) -> None:
        """A remote nothing derives is not provisioned (D-41), so there is nothing to filter."""
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(
            source_responses=self._source(
                "-", extra=remote_row("unused", "https://unused.example.org/", ref_filter=str(path))
            ),
            fake_target=self._target(),
        )
        job = FlatpakSyncJob(context)

        await run_job(job)

        assert not any("--filter=" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_missing_source_file_fails_the_app_naming_the_remote_and_the_path(self, tmp_path: Path) -> None:
        path = tmp_path / "gone.filter"
        context, _source, _target = make_context(source_responses=self._source(str(path)), fake_target=self._target())
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan, ReviewOutcome(decisions={d.item_id: Decision.APPLY for d in plan.diffs}, was_interactive=True)
        )

        with pytest.raises(PackageItemFailures) as raised:
            await job.apply()

        failed = {diff.item_id: reason for diff, reason in raised.value.failures}
        assert list(failed) == [self._APP_ID]
        assert "customremote" in failed[self._APP_ID]
        assert str(path) in failed[self._APP_ID]

    @pytest.mark.asyncio
    async def test_a_filter_flatpak_refuses_fails_the_app_and_quotes_the_error(self, tmp_path: Path) -> None:
        path = self._filter_file(tmp_path)
        context, _source, _target = make_context(
            source_responses=self._source(str(path)),
            fake_target=self._target(responses={"--filter=": CommandResult(1, "", "error: no such remote\n")}),
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan, ReviewOutcome(decisions={d.item_id: Decision.APPLY for d in plan.diffs}, was_interactive=True)
        )

        with pytest.raises(PackageItemFailures) as raised:
            await job.apply()

        assert [diff.item_id for diff, _reason in raised.value.failures] == [self._APP_ID]
        assert "error: no such remote" in raised.value.failures[0][1]

    @pytest.mark.asyncio
    async def test_an_app_from_another_remote_is_untouched_by_the_failure(self, tmp_path: Path) -> None:
        """ "Every approved application from that remote" is the app's OWN origin, so an app
        whose origin is a different remote keeps its own outcome.
        """
        path = tmp_path / "gone.filter"
        other = "org.example.Other\t1.0\tflathub\tuser\torg.example.Other/x86_64/stable\n"
        context, _source, _target = make_context(
            source_responses=derivation_source(
                remotes=remote_row("customremote", self._URL, ref_filter=str(path)) + remote_row("flathub", _SRC_URL),
                apps=self._APP_LINE + other,
            ),
            fake_target=self._target(),
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        job.accept_review(
            plan, ReviewOutcome(decisions={d.item_id: Decision.APPLY for d in plan.diffs}, was_interactive=True)
        )

        with pytest.raises(PackageItemFailures) as raised:
            await job.apply()

        assert [diff.item_id for diff, _reason in raised.value.failures] == [self._APP_ID]

    @pytest.mark.asyncio
    async def test_the_staged_copy_is_discarded(self, tmp_path: Path) -> None:
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(source_responses=self._source(str(path)), fake_target=self._target())
        job = FlatpakSyncJob(context)

        await run_job(job)

        staged = target.send_file.await_args_list[-1].args[1]
        assert any(f"rm --force {staged}" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_dry_run_previews_the_filter_and_issues_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = self._filter_file(tmp_path)
        context, _source, target = make_context(
            source_responses=self._source(str(path)), fake_target=self._target(), dry_run=True
        )
        job = FlatpakSyncJob(context)

        with caplog.at_level(logging.DEBUG, logger="pcswitcher.jobs.base"):
            await run_job(job)

        assert any(str(path) in record.message for record in caplog.records)
        assert not any("--filter=" in cmd for cmd in all_calls(target))
        target.send_file.assert_not_awaited()


class TestAnUnverifiedRemoteIsReported:
    """`PKG-FR-FLATPAK-REMOTE-TRUST` — a remote the source does not verify is replicated
    unverified, and an ORDINARY run is told so. The `mutates=` phrase says it too, but that
    reaches only `--confirm-each-command` and the command trace.
    """

    _URL = "https://custom.example.org/repo/"
    _APP_LINE = "org.example.App\t1.0\tcustomremote\tuser\torg.example.App/x86_64/stable\n"

    def _source(self, *, options: str) -> dict[str, CommandResult]:
        return derivation_source(remotes=remote_row("customremote", self._URL, options=options), apps=self._APP_LINE)

    @staticmethod
    def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
        return [record.message for record in caplog.records if record.levelno == logging.WARNING]

    @pytest.mark.asyncio
    async def test_an_unverified_source_remote_warns_once(self, caplog: pytest.LogCaptureFixture) -> None:
        context, _source, target = make_context(
            source_responses=self._source(options="no-gpg-verify"), fake_target=FakeFlatpakTarget()
        )
        job = FlatpakSyncJob(context)

        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.base"):
            await run_job(job)

        warnings = self._warnings(caplog)
        assert len(warnings) == 1
        assert "customremote" in warnings[0]
        assert "target-host" in warnings[0]
        # The remote is still provisioned unverified — the alternative is one that refuses
        # every install.
        assert any("remote-add" in cmd and "--no-gpg-verify" in cmd for cmd in all_calls(target))

    @pytest.mark.asyncio
    async def test_a_verified_source_remote_with_its_own_key_produces_no_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _ = write_source_keyring(tmp_path / ".local" / "share" / "flatpak", "customremote")
        context, _source, _target = make_context(
            source_responses={
                **self._source(options=""),
                f"sha256sum {_USER_KEYRING_DIR}": CommandResult(
                    0, keyring_line(_SOURCE_KEY_DIGEST, _USER_KEYRING_DIR, "customremote"), ""
                ),
            },
            fake_target=FakeFlatpakTarget(responses={"echo $HOME": CommandResult(0, "/home/tester\n", "")}),
        )
        job = FlatpakSyncJob(context)

        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.base"):
            await run_job(job)

        assert self._warnings(caplog) == []

    @pytest.mark.asyncio
    async def test_the_warning_fires_in_a_dry_run_too(self, caplog: pytest.LogCaptureFixture) -> None:
        """ADR-014 makes the preview the whole report, so a preview that hid this would
        overstate the real run.
        """
        context, _source, target = make_context(
            source_responses=self._source(options="no-gpg-verify"), fake_target=FakeFlatpakTarget(), dry_run=True
        )
        job = FlatpakSyncJob(context)

        with caplog.at_level(logging.WARNING, logger="pcswitcher.jobs.base"):
            await run_job(job)

        assert len(self._warnings(caplog)) == 1
        assert not any("remote-add" in cmd for cmd in all_calls(target))


class TestExcludePaths:
    def test_returns_flatpak_data_dir_excludes_var_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        paths = flatpak_sync_exclude_paths()

        assert paths == [tmp_path / ".local" / "share" / "flatpak"]
        assert not any(p == tmp_path / ".var" / "app" for p in paths)


class TestValidate:
    @pytest.mark.asyncio
    async def test_flatpak_unavailable_on_source_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            source_responses={"flatpak --version": CommandResult(127, "", "not found")}
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.SOURCE and "flatpak is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_flatpak_unavailable_on_target_yields_validation_error_and_does_not_raise(self) -> None:
        context, _source, _target = make_context(
            target_responses={"flatpak --version": CommandResult(127, "", "not found")}
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "flatpak is not available" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_valid_environment_with_no_system_scope_items_yields_no_errors(self) -> None:
        context, _source, _target = make_context()
        job = FlatpakSyncJob(context)

        errors: list[ValidationError] = await job.validate()

        assert errors == []

    @pytest.mark.asyncio
    async def test_system_scope_item_present_without_sudo_yields_validation_error(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(
                    0, "com.slack.Slack\t1.0\tflathub\tsystem\tcom.slack.Slack/x86_64/stable\n", ""
                )
            },
            target_responses={"sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required")},
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_user_scope_only_never_checks_sudo(self) -> None:
        context, _source, target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(
                    0, "org.example.App\t1.0\tflathub\tuser\torg.example.App/x86_64/stable\n", ""
                )
            }
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert errors == []
        assert not any("sudo --non-interactive true" in c for c in all_calls(target))


class TestJobDiscovery:
    @pytest.mark.asyncio
    async def test_orchestrator_resolves_flatpak_sync_to_flatpak_sync_job(self) -> None:
        config = MagicMock(spec=Configuration)
        config.logging = MagicMock()
        config.logging.file = 10
        config.logging.tui = 20
        config.logging.external = 30
        config.sync_jobs = {}
        config.job_configs = {}
        orchestrator = Orchestrator(target="target-host", config=config)

        job_class = orchestrator._resolve_sync_job_class("flatpak_sync")  # pyright: ignore[reportPrivateUsage]

        assert job_class is FlatpakSyncJob


class TestFlatpakItem:
    def test_reports_its_item_class(self) -> None:
        assert FlatpakItem.ITEM_CLASS == ItemClass.FLATPAK_REF

    def test_same_application_different_scope_yields_distinct_item_ids(self) -> None:
        user_item = FlatpakItem(
            application="com.slack.Slack",
            version="4.50",
            origin="flathub",
            scope="user",
            ref="com.slack.Slack/x86_64/stable",
        )
        system_item = FlatpakItem(
            application="com.slack.Slack",
            version="4.50",
            origin="flathub",
            scope="system",
            ref="com.slack.Slack/x86_64/stable",
        )

        assert user_item.item_id != system_item.item_id

    def test_label_names_the_item_in_actionable_terms(self) -> None:
        item = FlatpakItem(
            application="com.slack.Slack",
            version="4.50",
            origin="flathub",
            scope="user",
            ref="com.slack.Slack/x86_64/stable",
        )

        label = item.label()

        assert "com.slack.Slack" in label
        assert "4.50" in label
        assert "flathub" in label


class TestFlatpakRemoteItem:
    def test_reports_its_item_class(self) -> None:
        assert FlatpakRemoteItem.ITEM_CLASS == ItemClass.FLATPAK_REMOTE

    def test_same_remote_name_byte_identical_url_different_scope_yields_distinct_item_ids(self) -> None:
        url = "https://dl.flathub.org/repo/"
        user_remote = FlatpakRemoteItem(name="flathub", url=url, scope="user")
        system_remote = FlatpakRemoteItem(name="flathub", url=url, scope="system")

        assert user_remote.item_id != system_remote.item_id

    def test_label_names_the_remote(self) -> None:
        remote = FlatpakRemoteItem(name="flathub", url="https://dl.flathub.org/repo/", scope="user")

        label = remote.label()

        assert "flathub" in label
        assert "https://dl.flathub.org/repo/" in label


class TestAProbeThatDidNotAnswer:
    """ADR-022: a flatpak read that did not answer fails the job; one that answered
    "nothing" is data.

    Measured in a container with flatpak installed: `list`, `remotes` and `mask` all exit 1
    with `error:` on stderr when the installation cannot be opened, and all three exit 0
    printing nothing when the machine has none of what was asked for.
    """

    @pytest.mark.asyncio
    async def test_a_source_list_that_did_not_answer_fails_the_job(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(1, "", "error: While opening repository: Permission denied\n"),
                **SOURCE_RESPONSES,
            },
            target_responses=TARGET_RESPONSES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await FlatpakSyncJob(context).plan()

        assert "flatpak list --app" in str(excinfo.value)
        assert "exited 1" in str(excinfo.value)
        assert "Permission denied" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_remotes_read_that_did_not_answer_fails_the_job(self) -> None:
        """Only the user-scope remotes read fails; both list reads and the system-scope
        remotes read answer normally, so nothing else can produce this."""
        context, _source, _target = make_context(
            source_responses={
                "flatpak remotes --user": CommandResult(1, "", "error: Couldn't parse config file\n"),
                **SOURCE_RESPONSES,
            },
            target_responses=TARGET_RESPONSES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await FlatpakSyncJob(context).plan()

        assert "flatpak remotes --user" in str(excinfo.value)
        assert "Couldn't parse config file" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_mask_read_that_did_not_answer_fails_the_job(self) -> None:
        context, _source, _target = make_context(
            source_responses={**SOURCE_RESPONSES, "flatpak --user mask": CommandResult(1, "", "error: no repo\n")},
            target_responses=TARGET_RESPONSES,
        )

        with pytest.raises(ProbeFailed) as excinfo:
            await FlatpakSyncJob(context).plan()

        assert "flatpak --user mask" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_keyring_digest_read_exiting_non_zero_is_not_a_failure(self) -> None:
        """The counter-example a blanket exit-code rule would break. `sha256sum` over a
        glob that matches nothing exits 1, and that is the NORMAL answer for a scope whose
        remotes rely on a machine-level trust anchor — so this read is deliberately
        unguarded and the plan must complete.
        """
        context, _source, _target = make_context(
            source_responses={**SOURCE_RESPONSES, "sha256sum": CommandResult(1, "", "sha256sum: No such file\n")},
            target_responses={**TARGET_RESPONSES, "sha256sum": CommandResult(1, "", "sha256sum: No such file\n")},
        )

        plan = await FlatpakSyncJob(context).plan()

        assert plan.diffs

    @pytest.mark.asyncio
    async def test_a_target_with_nothing_installed_is_data_not_a_failure(self) -> None:
        """The legitimate-empty half: every target read answers empty at exit 0, which is a
        machine with no flatpaks, and the source's apps must reach the diff as installs.
        """
        context, _source, _target = make_context(
            source_responses=SOURCE_RESPONSES,
            target_responses={
                "flatpak list --app": CommandResult(0, "", ""),
                "flatpak remotes": CommandResult(0, "", ""),
            },
        )

        plan = await FlatpakSyncJob(context).plan()

        installs = {diff.item_id for diff in plan.diffs if diff.action == DiffAction.INSTALL}
        assert "flatpak:ref:user:org.gimp.GIMP/x86_64/stable" in installs
