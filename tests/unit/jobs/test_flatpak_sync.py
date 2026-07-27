"""Unit tests for FlatpakSyncJob: tab-separated `flatpak list`/`flatpak remotes`
parsing, the flatpak-specific plan()/diff pipeline, scope-as-identity, remote-before-
ref convergence ordering, and the missing-origin-remote skip guard.

All executor interactions are mocked; no real flatpak commands run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
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
    build_orphaned_refs_detail,
    flatpak_sync_exclude_paths,
)
from pcswitcher.jobs.packages.items import DiffAction, DiffClass, ItemClass
from pcswitcher.jobs.packages.review import (
    ReviewGroup,
    _is_removal_direction,  # pyright: ignore[reportPrivateUsage]
)
from pcswitcher.jobs.packages.sync_core import ConvergeItemFailed, PackagePlan
from pcswitcher.models import CommandResult, Host, ValidationError
from pcswitcher.orchestrator import Orchestrator

# `flatpak list --app --columns=application,version,origin,installation` has NO
# header row (RESEARCH: verified live against Flatpak 1.14.6, unlike `snap list`) —
# the --columns flag itself names the columns, so output is exactly those four
# tab-separated fields per line.
FLATPAK_LIST_SOURCE = (
    "com.slack.Slack\t4.50.0\tflathub\tsystem\n"
    "org.gnome.Podcasts\t1.0\tflathub\tuser\n"
    "org.gimp.GIMP\t2.10\tflathub\tuser\n"
    "org.example.SplitScope\t1.0\tflathub\tuser\n"
    "org.example.NeedsRemote\t1.0\tcustomremote\tuser\n"
)

FLATPAK_LIST_TARGET = (
    "org.gnome.Podcasts\t1.0\tflathub\tuser\n"
    "org.gimp.GIMP\t2.9\tflathub\tuser\n"
    "com.spotify.Client\t1.0\tflathub\tuser\n"
    "org.example.SplitScope\t1.0\tflathub\tsystem\n"
)

FLATPAK_LIST_BOTH_SCOPES = "org.example.App\t1.0\tflathub\tuser\norg.example.App\t1.0\tflathub\tsystem\n"

_FLATHUB_REMOTE_LINE = "flathub\thttps://dl.flathub.org/repo/\n"

SOURCE_RESPONSES = {
    "flatpak list --app --columns=application,version,origin,installation": CommandResult(0, FLATPAK_LIST_SOURCE, ""),
    "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
    "flatpak remotes --system --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
}

TARGET_RESPONSES = {
    "flatpak list --app --columns=application,version,origin,installation": CommandResult(0, FLATPAK_LIST_TARGET, ""),
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


def make_context(
    *,
    source_responses: dict[str, CommandResult] | None = None,
    target_responses: dict[str, CommandResult] | None = None,
    dry_run: bool = False,
) -> tuple[JobContext, MagicMock, MagicMock]:
    source = MagicMock()
    source.run_command = AsyncMock(side_effect=respond_to(source_responses or {}))
    target = MagicMock()
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


def all_calls(mock: MagicMock) -> list[str]:
    return [call.args[0] for call in mock.run_command.call_args_list]


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
        weird = "org.example.Weird\t1.0\tflathub\tcustom-install\n"
        context, _source, _target = make_context(source_responses={"flatpak list --app": CommandResult(0, weird, "")})
        job = FlatpakSyncJob(context)

        assert await job.capture_source_items() == []

    @pytest.mark.asyncio
    async def test_no_apps_installed_yields_empty_list_not_a_crash(self) -> None:
        context, _source, _target = make_context(source_responses={"flatpak list --app": CommandResult(0, "", "")})
        job = FlatpakSyncJob(context)

        assert await job.capture_source_items() == []


class TestPlanDiff:
    """`plan()`'s flatpak-specific diff: install/remove/report_only for refs,
    install/remove for remotes, ordered remotes-before-refs (D-14).
    """

    @pytest.mark.asyncio
    async def test_full_diff_taxonomy(self) -> None:
        context, _source, _target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert len(plan.diffs) == 7
        by_id = {diff.item_id: diff for diff in plan.diffs}

        # Missing on target -> install.
        assert by_id["flatpak:ref:system:com.slack.Slack"].action == DiffAction.INSTALL
        assert by_id["flatpak:ref:system:com.slack.Slack"].diff_class == DiffClass.MISSING_ON_TARGET

        # Version differs, same scope -> report_only, never a converge verb (D-04).
        gimp = by_id["flatpak:ref:user:org.gimp.GIMP"]
        assert gimp.action == DiffAction.REPORT_ONLY
        assert gimp.diff_class == DiffClass.VERSION_MISMATCH
        assert gimp.detail is not None
        assert "2.10" in gimp.detail
        assert "2.9" in gimp.detail

        # Same application, different scope on each machine -> one install, one
        # removal, never a single change (scope is identity, module docstring).
        assert by_id["flatpak:ref:user:org.example.SplitScope"].action == DiffAction.INSTALL
        assert by_id["flatpak:ref:system:org.example.SplitScope"].action == DiffAction.REMOVE

        # Extra on target -> removal, its own review group.
        assert by_id["flatpak:ref:user:com.spotify.Client"].action == DiffAction.REMOVE
        remove_group = next(g for g in plan.groups if g.action == "remove")
        install_group = next(g for g in plan.groups if g.action == "install")
        assert "flatpak:ref:user:com.spotify.Client" in {e.item_id for e in remove_group.entries}
        assert "flatpak:ref:user:com.spotify.Client" not in {e.item_id for e in install_group.entries}

        # Identical application/version/scope on both -> no diff at all.
        assert "flatpak:ref:user:org.gnome.Podcasts" not in by_id

        # Remote missing on target (system-scope flathub) -> its own add diff.
        assert by_id["flatpak:remote:system:flathub"].action == DiffAction.INSTALL
        assert "flatpak:remote:user:flathub" not in by_id  # identical on both -> no diff

    @pytest.mark.asyncio
    async def test_flathub_present_in_both_scopes_yields_two_remote_items(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, "", ""),
                "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
                "flatpak remotes --system --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
            },
            target_responses={"flatpak list --app": CommandResult(0, "", "")},
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        remote_ids = {diff.item_id for diff in plan.diffs if diff.item_class == ItemClass.FLATPAK_REMOTE}
        assert remote_ids == {"flatpak:remote:user:flathub", "flatpak:remote:system:flathub"}

    @pytest.mark.asyncio
    async def test_every_remote_diff_precedes_every_ref_diff(self) -> None:
        context, _source, _target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        remote_indices = [i for i, d in enumerate(plan.diffs) if d.item_class == ItemClass.FLATPAK_REMOTE]
        ref_indices = [i for i, d in enumerate(plan.diffs) if d.item_class == ItemClass.FLATPAK_REF]
        assert remote_indices
        assert ref_indices
        assert max(remote_indices) < min(ref_indices)


class TestRemoteUrlChange:
    """Decision 7: a remote present on both sides with the same name+scope but a
    DIFFERING URL is a CHANGE diff that converges the target to the source's URL via
    `flatpak remote-modify --url`, not a REMOVE+INSTALL churn and not silently ignored.
    """

    _SRC_URL = "https://dl.flathub.org/repo/"
    _TGT_URL = "https://old.mirror.example.org/repo/"

    def _responses(self, *, src_url: str, tgt_url: str) -> tuple[dict[str, CommandResult], dict[str, CommandResult]]:
        source = {
            "flatpak list --app": CommandResult(0, "", ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, f"flathub\t{src_url}\n", ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, "", ""),
        }
        target = {
            "flatpak list --app": CommandResult(0, "", ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, f"flathub\t{tgt_url}\n", ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, "", ""),
        }
        return source, target

    @pytest.mark.asyncio
    async def test_changed_url_yields_one_change_diff(self) -> None:
        source_responses, target_responses = self._responses(src_url=self._SRC_URL, tgt_url=self._TGT_URL)
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        remote_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE]
        assert len(remote_diffs) == 1
        change = remote_diffs[0]
        assert change.item_id == "flatpak:remote:user:flathub"
        assert change.action == DiffAction.CHANGE
        assert change.diff_class == DiffClass.VERSION_MISMATCH
        assert change.detail is not None
        assert self._SRC_URL in change.detail
        assert self._TGT_URL in change.detail

    @pytest.mark.asyncio
    async def test_changed_url_lands_in_default_ticked_change_group(self) -> None:
        source_responses, target_responses = self._responses(src_url=self._SRC_URL, tgt_url=self._TGT_URL)
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        change_group = next(g for g in plan.groups if g.action == "change")
        assert "flatpak:remote:user:flathub" in {e.item_id for e in change_group.entries}
        # A change is install-direction, not removal — it shares no group with removals.
        assert not any(g.action == "remove" for g in plan.groups)

    @pytest.mark.asyncio
    async def test_identical_url_yields_no_diff(self) -> None:
        source_responses, target_responses = self._responses(src_url=self._SRC_URL, tgt_url=self._SRC_URL)
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.FLATPAK_REMOTE for d in plan.diffs)

    @pytest.mark.asyncio
    async def test_converge_uses_remote_modify_with_source_url_and_scope_flag(self) -> None:
        source_responses, target_responses = self._responses(src_url=self._SRC_URL, tgt_url=self._TGT_URL)
        context, _source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        change = next(d for d in plan.diffs if d.action == DiffAction.CHANGE)

        await job.converge(change)

        modify_cmd = next(c for c in all_calls(target) if "remote-modify" in c)
        assert "--user" in modify_cmd
        assert "sudo" not in modify_cmd
        assert f"--url={self._SRC_URL}" in modify_cmd
        assert modify_cmd.rstrip().endswith("flathub")
        # No delete+add churn: remote-modify is the only remote-mutating verb issued.
        assert not any("remote-delete" in c for c in all_calls(target))
        assert not any("remote-add" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_system_scope_url_change_uses_sudo_and_system_flag(self) -> None:
        source_responses = {
            "flatpak list --app": CommandResult(0, "", ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, "", ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, f"flathub\t{self._SRC_URL}\n", ""),
        }
        target_responses = {
            "flatpak list --app": CommandResult(0, "", ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, "", ""),
            "flatpak remotes --system --columns=name,url": CommandResult(0, f"flathub\t{self._TGT_URL}\n", ""),
        }
        context, _source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        change = next(d for d in plan.diffs if d.action == DiffAction.CHANGE)

        await job.converge(change)

        modify_cmd = next(c for c in all_calls(target) if "remote-modify" in c)
        assert modify_cmd.startswith("sudo ")
        assert "--system" in modify_cmd


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
        # A remote with no options prints two fields and NO trailing tab (verified live).
        items = _parse_flatpak_remotes("flathub\thttps://dl.flathub.org/repo/\n", "user", {})

        assert len(items) == 1
        assert items[0].gpg_verify is True
        assert items[0].key_digest is None

    def test_no_gpg_verify_token_marks_the_remote_unverified_and_keyless(self) -> None:
        items = _parse_flatpak_remotes(
            "local\tfile:///srv/repo\tno-enumerate,no-gpg-verify\n", "user", {"local": _SOURCE_KEY_DIGEST}
        )

        assert items[0].gpg_verify is False
        assert items[0].key_digest is None

    def test_other_options_do_not_read_as_no_gpg_verify(self) -> None:
        items = _parse_flatpak_remotes("mirror\thttps://example.org/repo/\tno-enumerate\n", "user", {})

        assert items[0].gpg_verify is True

    def test_key_digest_joins_the_scope_map_by_remote_name(self) -> None:
        items = _parse_flatpak_remotes(
            "flathub\thttps://dl.flathub.org/repo/\n", "system", {"flathub": _SOURCE_KEY_DIGEST}
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
        """Scope stays identity: only the scope whose key actually differs diffs."""
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

        plan = await job.plan()

        remote_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE]
        assert [d.item_id for d in remote_diffs] == ["flatpak:remote:system:flathub"]
        assert remote_diffs[0].action == DiffAction.CHANGE


def trust_responses(
    *,
    remote_line: str,
    key_digest: str | None,
    keyring_dir: str = _USER_KEYRING_DIR,
    scope_flag: str = "--user",
) -> dict[str, CommandResult]:
    """One machine's flatpak responses for a single remote in a single scope."""
    digest_output = keyring_line(key_digest, keyring_dir, remote_line.split("\t", maxsplit=1)[0]) if key_digest else ""
    return {
        "flatpak list --app": CommandResult(0, "", ""),
        f"flatpak remotes {scope_flag}": CommandResult(0, remote_line, ""),
        f"sha256sum {keyring_dir}": CommandResult(0, digest_output, ""),
        "echo $HOME": CommandResult(0, "/home/tester\n", ""),
    }


class TestRemoteTrustDiff:
    """#215 — a remote whose key or verification setting differs is the same `CHANGE`
    class a differing URL already was: same identity, differing value, converged in
    place by `flatpak remote-modify`.
    """

    _SIGNED = f"flathub\t{TestRemoteUrlChange._SRC_URL}\n"  # pyright: ignore[reportPrivateUsage]
    _UNVERIFIED = f"flathub\t{TestRemoteUrlChange._SRC_URL}\tno-gpg-verify\n"  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.asyncio
    async def test_differing_key_yields_one_change_diff_naming_both_digests(self) -> None:
        context, _source, _target = make_context(
            source_responses=trust_responses(remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST),
            target_responses=trust_responses(remote_line=self._SIGNED, key_digest=_TARGET_KEY_DIGEST),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        remote_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE]
        assert len(remote_diffs) == 1
        change = remote_diffs[0]
        assert change.action == DiffAction.CHANGE
        assert change.diff_class == DiffClass.VERSION_MISMATCH
        assert change.detail is not None
        assert _SOURCE_KEY_DIGEST in change.detail
        assert _TARGET_KEY_DIGEST in change.detail
        # The URL is identical on both sides, so it is not named as a difference.
        assert "url:" not in change.detail

    @pytest.mark.asyncio
    async def test_target_lost_its_key_is_a_change_not_a_silent_pass(self) -> None:
        context, _source, _target = make_context(
            source_responses=trust_responses(remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST),
            target_responses=trust_responses(remote_line=self._SIGNED, key_digest=None),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        change = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)
        assert change.action == DiffAction.CHANGE
        assert change.detail is not None
        assert "none" in change.detail

    @pytest.mark.asyncio
    async def test_differing_verification_setting_is_a_change_naming_both_states(self) -> None:
        context, _source, _target = make_context(
            source_responses=trust_responses(remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST),
            target_responses=trust_responses(remote_line=self._UNVERIFIED, key_digest=None),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        change = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)
        assert change.action == DiffAction.CHANGE
        assert change.detail is not None
        assert "gpg verification: enabled vs disabled" in change.detail

    @pytest.mark.asyncio
    async def test_identical_url_and_trust_yields_no_diff(self) -> None:
        context, _source, _target = make_context(
            source_responses=trust_responses(remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST),
            target_responses=trust_responses(remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST),
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        assert not any(d.item_class == ItemClass.FLATPAK_REMOTE for d in plan.diffs)


class TestRemoteTrustConverge:
    """#215 — provisioning a remote carries its key, so the ref installs that follow can
    actually verify their signatures. `--no-gpg-verify` is emitted only for a remote the
    SOURCE itself does not verify.
    """

    _URL = "https://dl.flathub.org/repo/"
    _SIGNED = f"flathub\t{_URL}\n"
    _UNVERIFIED = f"flathub\t{_URL}\tno-gpg-verify\n"
    _STAGED = "/home/tester/.cache/pc-switcher/flatpak-staging/flatpak_remote_user_flathub.gpg"

    @staticmethod
    def _job_with_source_key(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        remote_line: str,
        key_digest: str | None,
        target_responses: dict[str, CommandResult] | None = None,
        scope: str = "user",
    ) -> tuple[FlatpakSyncJob, MagicMock]:
        scope_flag = "--user" if scope == "user" else "--system"
        if scope == "user":
            monkeypatch.setattr(Path, "home", lambda: tmp_path)
            installation = tmp_path / ".local" / "share" / "flatpak"
            # The user-scope read is a shell expression the remote shell expands, so it
            # is unaffected by the patched `Path.home()` the local file lookup uses.
            keyring_dir = _USER_KEYRING_DIR
        else:
            installation = tmp_path / "var-lib-flatpak"
            monkeypatch.setattr(flatpak_sync, "_FLATPAK_SYSTEM_INSTALLATION", installation)
            keyring_dir = f"{installation}/repo"
        if key_digest is not None:
            _ = write_source_keyring(installation, remote_line.split("\t", maxsplit=1)[0])
        context, _source, target = make_context(
            source_responses=trust_responses(
                remote_line=remote_line, key_digest=key_digest, keyring_dir=keyring_dir, scope_flag=scope_flag
            ),
            target_responses=target_responses or {"echo $HOME": CommandResult(0, "/home/tester\n", "")},
        )
        return FlatpakSyncJob(context), target

    @pytest.mark.asyncio
    async def test_signed_remote_is_added_with_the_sources_own_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = self._job_with_source_key(
            tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST
        )
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)

        result = await job.converge(diff)

        assert result.success
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
        job, target = self._job_with_source_key(
            tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST
        )
        plan = await job.plan()

        await job.converge(next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE))

        _sent_local, sent_remote = target.send_file.call_args.args
        assert sent_remote.startswith("/home/tester/.cache/pc-switcher/")
        assert any("mkdir --parents /home/tester/.cache/pc-switcher/flatpak-staging" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_every_staging_write_carries_mutates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        job, target = self._job_with_source_key(
            tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST
        )
        plan = await job.plan()

        await job.converge(next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE))

        assert target.send_file.call_args.kwargs["mutates"]
        for call in target.run_command.call_args_list:
            command = call.args[0]
            if "mkdir --parents" in command or "rm --force" in command or "remote-add" in command:
                assert call.kwargs.get("mutates"), f"ungated write: {command}"

    @pytest.mark.asyncio
    async def test_staged_key_is_discarded_even_when_remote_add_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = self._job_with_source_key(
            tmp_path,
            monkeypatch,
            remote_line=self._SIGNED,
            key_digest=_SOURCE_KEY_DIGEST,
            target_responses={
                "echo $HOME": CommandResult(0, "/home/tester\n", ""),
                "remote-add": CommandResult(1, "", "boom"),
            },
        )
        plan = await job.plan()

        result = await job.converge(next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE))

        assert not result.success
        assert any(f"rm --force {self._STAGED}" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_unverified_source_remote_replicates_as_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = self._job_with_source_key(tmp_path, monkeypatch, remote_line=self._UNVERIFIED, key_digest=None)
        plan = await job.plan()

        await job.converge(next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE))

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert "--no-gpg-verify" in add_cmd
        assert "--gpg-import" not in add_cmd
        target.send_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_verified_source_remote_is_never_downgraded_even_if_the_target_is_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _ = write_source_keyring(tmp_path / ".local" / "share" / "flatpak", "flathub")
        context, _source, target = make_context(
            source_responses=trust_responses(remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST),
            target_responses=trust_responses(remote_line=self._UNVERIFIED, key_digest=None),
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        change = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)

        await job.converge(change)

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
        unverified, stated in the review's detail rather than converged into a lie.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses=trust_responses(remote_line=self._UNVERIFIED, key_digest=None),
            target_responses=trust_responses(remote_line=self._SIGNED, key_digest=_TARGET_KEY_DIGEST),
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        change = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)
        assert change.detail is not None
        assert "gpg verification: disabled vs enabled" in change.detail

        await job.converge(change)

        modify_cmd = next(c for c in all_calls(target) if "remote-modify" in c)
        assert "--no-gpg-verify" in modify_cmd
        assert "--gpg-import" not in modify_cmd

    @pytest.mark.asyncio
    async def test_system_scope_add_uses_sudo_and_still_stages_in_the_user_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        job, target = self._job_with_source_key(
            tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST, scope="system"
        )
        plan = await job.plan()

        await job.converge(next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE))

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert add_cmd.startswith("sudo ")
        assert "--system" in add_cmd
        _sent_local, sent_remote = target.send_file.call_args.args
        assert sent_remote == "/home/tester/.cache/pc-switcher/flatpak-staging/flatpak_remote_system_flathub.gpg"
        assert "--gpg-import=/home/tester/.cache/pc-switcher/" in add_cmd

    @pytest.mark.asyncio
    async def test_missing_source_keyring_refuses_rather_than_provisioning_a_dead_remote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The digest was captured at plan time, so the file disappearing before
        converge is a real inconsistency — never an install-anyway.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no keyring file written
        context, _source, target = make_context(
            source_responses=trust_responses(remote_line=self._SIGNED, key_digest=_SOURCE_KEY_DIGEST),
            target_responses={"echo $HOME": CommandResult(0, "/home/tester\n", "")},
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)

        with pytest.raises(ConvergeItemFailed, match="signing key"):
            await job.converge(diff)

        assert not any("remote-add" in c for c in all_calls(target))

    @pytest.mark.asyncio
    async def test_verified_remote_without_a_key_of_its_own_adds_plainly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A remote trusted through a machine-level anchor has no per-remote key to
        carry: nothing is invented for it, and verification is left on.
        """
        job, target = self._job_with_source_key(tmp_path, monkeypatch, remote_line=self._SIGNED, key_digest=None)
        plan = await job.plan()

        await job.converge(next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE))

        add_cmd = next(c for c in all_calls(target) if "remote-add" in c)
        assert "--gpg-import" not in add_cmd
        assert "--no-gpg-verify" not in add_cmd
        target.send_file.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_removal_transfers_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`remote-delete` takes the per-remote keyring with it (verified live), so the
        removal direction stages no key and needs no source lookup at all.
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        context, _source, target = make_context(
            source_responses=trust_responses(remote_line="", key_digest=None),
            target_responses=trust_responses(remote_line=self._SIGNED, key_digest=_TARGET_KEY_DIGEST),
        )
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)
        assert diff.action == DiffAction.REMOVE

        await job.converge(diff)

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


class TestConverge:
    @pytest.mark.asyncio
    async def test_remotes_converge_before_refs_that_depend_on_them(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()

        applicable = [
            diff
            for diff in plan.diffs
            if diff.action != DiffAction.REPORT_ONLY and diff.item_id != "flatpak:ref:user:org.example.NeedsRemote"
        ]
        for diff in applicable:
            await job.converge(diff)

        commands = all_calls(target)
        remote_add_idx = next(i for i, c in enumerate(commands) if "remote-add" in c)
        slack_install_idx = next(
            i for i, c in enumerate(commands) if "flatpak install" in c and "com.slack.Slack" in c
        )
        assert remote_add_idx < slack_install_idx

    @pytest.mark.asyncio
    async def test_user_scope_ref_install_has_no_sudo_and_carries_user_flag(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:org.example.SplitScope")

        await job.converge(diff)

        commands = all_calls(target)
        install_cmd = next(c for c in commands if "flatpak install" in c and "org.example.SplitScope" in c)
        assert "--user" in install_cmd
        assert "sudo" not in install_cmd

    @pytest.mark.asyncio
    async def test_system_scope_ref_install_uses_sudo_and_system_flag(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        remote_diff = next(d for d in plan.diffs if d.item_id == "flatpak:remote:system:flathub")
        ref_diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:system:com.slack.Slack")

        await job.converge(remote_diff)
        await job.converge(ref_diff)

        commands = all_calls(target)
        install_cmd = next(c for c in commands if "flatpak install" in c and "com.slack.Slack" in c)
        assert "--system" in install_cmd
        assert install_cmd.startswith("sudo ")

    @pytest.mark.asyncio
    async def test_ref_removal_never_needs_source_lookup(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:com.spotify.Client")

        await job.converge(diff)

        commands = all_calls(target)
        assert any("flatpak uninstall --assumeyes --user com.spotify.Client" in c for c in commands)

    @pytest.mark.asyncio
    async def test_ref_with_missing_origin_remote_is_skipped_with_named_failure(self) -> None:
        context, _source, target = make_context(source_responses=SOURCE_RESPONSES, target_responses=TARGET_RESPONSES)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_id == "flatpak:ref:user:org.example.NeedsRemote")

        with pytest.raises(ConvergeItemFailed, match="customremote"):
            await job.converge(diff)

        assert not any("customremote" in c for c in all_calls(target) if "flatpak install" in c)


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
                "flatpak list --app": CommandResult(0, "org.example.App\t1.0\tflathub\tuser\n", ""),
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
        installed = "org.gnome.Podcasts\t1.0\tflathub\tuser\n"
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, installed, ""),
                "flatpak --user mask": CommandResult(0, "  org.example.NeverInstalledAnywhere\n", ""),
            },
            target_responses={"flatpak list --app": CommandResult(0, installed, "")},
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
                "flatpak list --app": CommandResult(0, "org.example.SourceOnly\t1.0\tflathub\tuser\n", ""),
                "flatpak remotes --user --columns=name,url": CommandResult(
                    0, _FLATHUB_REMOTE_LINE + "srcremote\thttps://src.example.org/repo/\n", ""
                ),
                "flatpak --user mask": CommandResult(0, "  org.example.MaskNew\n", ""),
            },
            target_responses={
                "flatpak list --app": CommandResult(0, "org.example.TargetOnly\t1.0\tflathub\tuser\n", ""),
                "flatpak remotes --user --columns=name,url": CommandResult(
                    0, _FLATHUB_REMOTE_LINE + "tgtremote\thttps://tgt.example.org/repo/\n", ""
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

        assert group.title == "Mask flatpak packages"
        assert [e.action_label for e in group.entries] == ["mask"]
        assert {e.item_id for e in group.entries} == {"flatpak:mask:user:org.example.MaskNew"}

    @pytest.mark.asyncio
    async def test_mask_remove_group_reads_unmask_and_is_removal_direction(self) -> None:
        plan = await self._mixed_plan()

        group = self._group_holding(plan, "flatpak:mask:user:org.example.MaskOld")

        assert group.title == "Unmask flatpak packages"
        assert [e.action_label for e in group.entries] == ["unmask"]
        assert _is_removal_direction(group.action)

    @pytest.mark.asyncio
    async def test_ref_and_remote_groups_keep_their_own_verbs_and_exclude_masks(self) -> None:
        plan = await self._mixed_plan()

        ref_install = self._group_holding(plan, "flatpak:ref:user:org.example.SourceOnly")
        ref_remove = self._group_holding(plan, "flatpak:ref:user:org.example.TargetOnly")
        remote_install = self._group_holding(plan, "flatpak:remote:user:srcremote")
        remote_remove = self._group_holding(plan, "flatpak:remote:user:tgtremote")

        assert ref_install.title == "Install flatpak packages"
        assert ref_remove.title == "Remove flatpak packages"
        # Refs and remotes have no vocabulary entry of their own, so they fall back to the
        # bare DiffAction verb — which is exactly the verb a mask must NOT inherit.
        assert {e.action_label for e in (*ref_install.entries, *remote_install.entries)} == {"install"}
        assert {e.action_label for e in (*ref_remove.entries, *remote_remove.entries)} == {"remove"}
        for group in (ref_install, ref_remove, remote_install, remote_remove):
            assert not any(e.item_id.startswith("flatpak:mask:") for e in group.entries)


class TestRemoteRemovalOrphansRefs:
    """F21 (#214) — a remote offered for removal names the target refs it would orphan.

    The removal itself is NOT guarded and is not meant to be: `_converge_remote`'s
    REMOVE branch still issues `flatpak remote-delete` for any approved diff, because
    deleting a remote whose refs are being removed in the same run is legitimate
    cleanup. What plan() adds is disclosure — the REMOVE diff's `detail` names the
    target-side refs whose origin the remote is, in that same scope, so the review
    states the consequence before approval (D-30's placement, unlike the ref-INSTALL
    direction's `_remote_ready_on_target` hard refusal). The ref below is installed
    identically on both machines, so it produces no diff of its own and would otherwise
    never appear in the review at all.
    """

    _USER_REF_LINE = "org.example.NeedsRemote\t1.0\tcustomremote\tuser\n"
    _SYSTEM_REF_LINE = "org.example.SystemOnly\t1.0\tcustomremote\tsystem\n"
    _CUSTOM_REMOTE_LINE = "customremote\thttps://custom.example.org/repo/\n"

    def _responses(self) -> tuple[dict[str, CommandResult], dict[str, CommandResult]]:
        """User-scope `customremote` is target-only (so: one REMOVE diff), while the
        system-scope remote of the same name exists on both sides (so: no diff). Both
        refs are installed identically on both machines.
        """
        source = {
            "flatpak list --app": CommandResult(0, self._USER_REF_LINE + self._SYSTEM_REF_LINE, ""),
            "flatpak remotes --user --columns=name,url": CommandResult(0, _FLATHUB_REMOTE_LINE, ""),
            "flatpak remotes --system --columns=name,url": CommandResult(
                0, _FLATHUB_REMOTE_LINE + self._CUSTOM_REMOTE_LINE, ""
            ),
        }
        target = {
            "flatpak list --app": CommandResult(0, self._USER_REF_LINE + self._SYSTEM_REF_LINE, ""),
            "flatpak remotes --user --columns=name,url": CommandResult(
                0, _FLATHUB_REMOTE_LINE + self._CUSTOM_REMOTE_LINE, ""
            ),
            "flatpak remotes --system --columns=name,url": CommandResult(
                0, _FLATHUB_REMOTE_LINE + self._CUSTOM_REMOTE_LINE, ""
            ),
        }
        return source, target

    @pytest.mark.asyncio
    async def test_dependent_target_ref_is_named_in_the_removal_detail(self) -> None:
        source_responses, target_responses = self._responses()
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        # Both refs exist identically on both machines, so neither is reviewed itself.
        assert not any(d.item_class == ItemClass.FLATPAK_REF for d in plan.diffs)
        remote_diffs = [d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE]
        assert len(remote_diffs) == 1
        assert remote_diffs[0].item_id == "flatpak:remote:user:customremote"
        assert remote_diffs[0].action == DiffAction.REMOVE
        assert remote_diffs[0].detail is not None
        assert "org.example.NeedsRemote" in remote_diffs[0].detail

    @pytest.mark.asyncio
    async def test_same_name_remote_in_the_other_scope_contributes_no_dependents(self) -> None:
        """A remote is per-installation, so only same-scope refs depend on it: the
        system-scope ref whose origin is also named `customremote` must not be named in
        the USER-scope removal's detail.
        """
        source_responses, target_responses = self._responses()
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)
        assert diff.detail is not None
        assert "org.example.SystemOnly" not in diff.detail

    @pytest.mark.asyncio
    async def test_remote_with_no_dependent_refs_keeps_detail_none(self) -> None:
        context, _source, _target = make_context(
            source_responses={
                "flatpak list --app": CommandResult(0, self._USER_REF_LINE, ""),
                "flatpak remotes --user --columns=name,url": CommandResult(
                    0, _FLATHUB_REMOTE_LINE + self._CUSTOM_REMOTE_LINE, ""
                ),
            },
            target_responses={
                "flatpak list --app": CommandResult(0, self._USER_REF_LINE, ""),
                "flatpak remotes --user --columns=name,url": CommandResult(
                    0,
                    _FLATHUB_REMOTE_LINE
                    + self._CUSTOM_REMOTE_LINE
                    + "unusedremote\thttps://unused.example.org/repo/\n",
                    "",
                ),
            },
        )
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        diff = next(d for d in plan.diffs if d.item_id == "flatpak:remote:user:unusedremote")
        assert diff.action == DiffAction.REMOVE
        assert diff.detail is None

    @pytest.mark.asyncio
    async def test_review_entry_carries_the_dependent_refs_to_the_user(self) -> None:
        source_responses, target_responses = self._responses()
        context, _source, _target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)

        plan = await job.plan()

        remove_group = next(g for g in plan.groups if _is_removal_direction(g.action))
        entry = next(e for e in remove_group.entries if e.item_id == "flatpak:remote:user:customremote")
        assert entry.detail is not None
        assert "org.example.NeedsRemote" in entry.detail
        assert "customremote" in entry.detail

    @pytest.mark.asyncio
    async def test_approved_removal_still_deletes_the_remote(self) -> None:
        """Disclosure, not refusal: converge is unchanged, so an approved removal of a
        remote with dependents still runs `flatpak remote-delete`.
        """
        source_responses, target_responses = self._responses()
        context, _source, target = make_context(source_responses=source_responses, target_responses=target_responses)
        job = FlatpakSyncJob(context)
        plan = await job.plan()
        diff = next(d for d in plan.diffs if d.item_class == ItemClass.FLATPAK_REMOTE)

        result = await job.converge(diff)

        assert result.success
        assert any("flatpak remote-delete --user customremote" in c for c in all_calls(target))


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
            source_responses={"flatpak list --app": CommandResult(0, "com.slack.Slack\t1.0\tflathub\tsystem\n", "")},
            target_responses={"sudo --non-interactive true": CommandResult(1, "", "sudo: a password is required")},
        )
        job = FlatpakSyncJob(context)

        errors = await job.validate()

        assert any(e.host is Host.TARGET and "sudo" in e.message for e in errors)

    @pytest.mark.asyncio
    async def test_user_scope_only_never_checks_sudo(self) -> None:
        context, _source, target = make_context(
            source_responses={"flatpak list --app": CommandResult(0, "org.example.App\t1.0\tflathub\tuser\n", "")}
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
        user_item = FlatpakItem(application="com.slack.Slack", version="4.50", origin="flathub", scope="user")
        system_item = FlatpakItem(application="com.slack.Slack", version="4.50", origin="flathub", scope="system")

        assert user_item.item_id != system_item.item_id

    def test_label_names_the_item_in_actionable_terms(self) -> None:
        item = FlatpakItem(application="com.slack.Slack", version="4.50", origin="flathub", scope="user")

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


class TestOrphanedRefsDetail:
    def test_names_the_remote_and_every_dependent(self) -> None:
        detail = build_orphaned_refs_detail("customremote", ["org.example.One", "org.example.Two"])

        assert "customremote" in detail
        assert "org.example.One" in detail
        assert "org.example.Two" in detail
