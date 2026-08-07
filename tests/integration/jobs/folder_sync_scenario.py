"""The folder_sync scenario driven by `test_end_to_end_sync.py`: seeding, manifests, assertions.

The end-to-end test that calls this module syncs the real /home (the production default
scope). That is safe on the VMs because both boot from an identical btrfs baseline (so the
--delete mirror only moves the seeded subtree), pc-switcher's own runtime files are protected
by the hardcoded excludes (ADR-016), heavy regenerable trees are excluded in the test config
for speed, and .ssh/known_hosts is excluded because reset-vm.sh gives each VM the other's
host key. `remove_test_artifacts` cleans every seeded subtree up again.

Two independent trees are seeded, and they must stay independent:

- `TESTTREE` (pcsw-itest) carries the rich ownership/permission/metadata matrix and is
  asserted by STRICT manifest equality between source and target;
- `FILTER_TREE` (pcsw-filter) carries the #166 filter-rule scenario, whose whole point is
  that source and target deliberately diverge on excluded paths. Keeping it out of
  `TESTTREE` is what lets the manifest equality above stay strict.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath

from pcswitcher.executor import BashLoginRemoteExecutor

# Seeded rich test subtree, relative to the user's home.
TESTTREE = "pcsw-itest"

# uid/gid 1 = daemon on Ubuntu — a non-root system user, used numerically to prove
# uid/gid preservation for files the invoking user cannot access.
_OTHER_UID = 1
_OTHER_GID = 1

# Known mtimes (Unix epoch seconds) for backdated-mtime assertions.
_BACKDATED_MTIME = 1705312800  # 2024-01-15 10:00:00 UTC
_ADDITION_MTIME = 1710000000  # 2024-03-09 16:00:00 UTC

# pc-switcher runtime state dir (ADR-016 hardcoded exclude target).
STATE_DIR = "~/.local/share/pc-switcher"

# SC3 INCLUSION markers (home-relative). The default config deliberately SYNCS
# dev-tool caches and VS Code user state while excluding regenerable VS Code caches.
# These live outside the pcsw-itest tree (so they don't affect the tree manifest);
# each holds a distinctive content string so we can assert it transferred.
_INCLUDED_MARKERS = {
    ".cargo/pcsw-cache-marker.txt": "cargo-included",  # dev-tool cache — synced
    ".config/Code/User/pcsw-user-marker.json": "vscode-user-included",  # VS Code user state — synced
}
# Sibling of Code/User that IS excluded by config — proves inclusion is selective.
_EXCLUDED_MARKER = ".config/Code/Cache/pcsw-cache-marker.bin"


# ---------------------------------------------------------------------------
# #166 filter-rule scenario (separate from the strict-manifest TESTTREE).
#
# Rides on the same first A→B sync. Exercises, through the REAL rsync command
# built by _build_rsync_cmd, every filter surface:
#   - central include-override (keep pcsw-filter/cache/keep-uv + keep-pip, drop the rest);
#   - a wholly-excluded central subtree (pcsw-filter/excluded);
#   - nested per-directory .pcswitcher-filter files (pcsw-filter/nest and .../nest/deep);
# and verifies BOTH directions of correctness:
#   - included paths ADD / OVERWRITE / DELETE on the target (rsync --delete within);
#   - excluded paths leave the target AS-IS, both when a conflicting copy exists on the
#     source AND when the path exists ONLY on the target (the --delete survival case —
#     the important difference: no --delete-excluded, so excluded target files are never
#     removed even with no source counterpart);
#   - per-directory filter files themselves transfer to the target (no `e` modifier).
# ---------------------------------------------------------------------------

FILTER_TREE = "pcsw-filter"

# Files present on the SOURCE (pc1), home-relative → exact content.
_FILTER_SRC: dict[str, str] = {
    f"{FILTER_TREE}/synced/add_me.txt": "src-add",  # source-only, included → added on target
    f"{FILTER_TREE}/synced/overwrite_me.txt": "src-new",  # differs on target → overwrites it
    f"{FILTER_TREE}/synced/keep_me.txt": "same",  # identical on target → unchanged
    f"{FILTER_TREE}/cache/keep-uv/tool.txt": "uv-src",  # include-override kept subfolder
    f"{FILTER_TREE}/cache/keep-pip/tool.txt": "pip-src",  # include-override kept subfolder
    f"{FILTER_TREE}/cache/junk/blob.txt": "junk-src",  # dropped sibling → must not transfer
    f"{FILTER_TREE}/excluded/on_both.txt": "src-version",  # excluded → must not overwrite target
    f"{FILTER_TREE}/nest/keep.txt": "nest-keep-src",  # per-dir subtree, not excluded → synced
    f"{FILTER_TREE}/nest/nested_secret.txt": "src-secret",  # per-dir excluded → must not transfer
    f"{FILTER_TREE}/nest/deep/keep.txt": "deep-keep-src",  # nested, not excluded → synced
    f"{FILTER_TREE}/nest/deep/nested_secret.txt": "src-deep-secret",  # inherited exclude → no transfer
    f"{FILTER_TREE}/nest/deep/deep_only.txt": "src-deep-only",  # deep per-dir exclude → no transfer
}

# Per-directory filter files on the SOURCE (transfer to target; govern their subtree).
_FILTER_SRC_PERDIR: dict[str, str] = {
    f"{FILTER_TREE}/nest/.pcswitcher-filter": "- nested_secret.txt",
    f"{FILTER_TREE}/nest/deep/.pcswitcher-filter": "- deep_only.txt",
}

# Files PRE-EXISTING on the TARGET (pc2) before the first sync (drive the --delete cases).
# The `overwrite_me` target copy differs in SIZE from the source's, not just content, so
# rsync's default (size, mtime) quick-check always transfers it regardless of seed timing.
# The target deliberately does NOT hold the .pcswitcher-filter files: this models a genuine
# first sync. A dir-merge rule is read per-side, so a per-directory exclude protects a
# pre-existing target file from --delete only once the filter file is on the receiver
# (verified against rsync 3.2.7). folder_sync closes that gap: because the source filter
# files are not yet on the target (_needs_copy_pass detects the mismatch), execute()
# runs the mirror WITHOUT --delete first to place them, then the deleting mirror — so the
# per-dir survivors below are protected on this first sync. That copy pass is exactly
# what this scenario exercises end-to-end.
_FILTER_TGT: dict[str, str] = {
    f"{FILTER_TREE}/synced/overwrite_me.txt": "old",  # included, differing size → overwritten by source
    f"{FILTER_TREE}/synced/keep_me.txt": "same",  # identical → untouched
    f"{FILTER_TREE}/synced/delete_me.txt": "tgt-doomed",  # included, source-absent → deleted
    f"{FILTER_TREE}/cache/junk/tgt_junk.txt": "tgt-junk",  # central-excluded, source-absent → survives
    f"{FILTER_TREE}/excluded/on_both.txt": "tgt-version",  # central-excluded, source-present → not overwritten
    f"{FILTER_TREE}/excluded/tgt_only.txt": "tgt-survivor",  # central-excluded, source-absent → survives
    f"{FILTER_TREE}/nest/nested_secret.txt": "tgt-secret",  # per-dir excluded (pre-seeded rule) → survives
    f"{FILTER_TREE}/nest/deep/nested_secret.txt": "tgt-deep-secret",  # inherited exclude → survives
    f"{FILTER_TREE}/nest/deep/deep_only.txt": "tgt-deep",  # deep per-dir exclude → survives
}

# Expected TARGET state after the first A→B sync (None ⇒ must be absent).
_FILTER_EXPECT: dict[str, str | None] = {
    # Included subtree: add / overwrite / keep / delete.
    f"{FILTER_TREE}/synced/add_me.txt": "src-add",
    f"{FILTER_TREE}/synced/overwrite_me.txt": "src-new",
    f"{FILTER_TREE}/synced/keep_me.txt": "same",
    f"{FILTER_TREE}/synced/delete_me.txt": None,
    # Include-override (#166): kept dev caches sync; the dropped sibling never arrives.
    f"{FILTER_TREE}/cache/keep-uv/tool.txt": "uv-src",
    f"{FILTER_TREE}/cache/keep-pip/tool.txt": "pip-src",
    f"{FILTER_TREE}/cache/junk/blob.txt": None,
    f"{FILTER_TREE}/cache/junk/tgt_junk.txt": "tgt-junk",
    # Central exclusion protects the target both ways (conflicting copy, and target-only).
    f"{FILTER_TREE}/excluded/on_both.txt": "tgt-version",
    f"{FILTER_TREE}/excluded/tgt_only.txt": "tgt-survivor",
    # Nested per-directory filters: the files transfer; their rules protect the target subtree.
    f"{FILTER_TREE}/nest/.pcswitcher-filter": "- nested_secret.txt",
    f"{FILTER_TREE}/nest/deep/.pcswitcher-filter": "- deep_only.txt",
    f"{FILTER_TREE}/nest/keep.txt": "nest-keep-src",
    f"{FILTER_TREE}/nest/deep/keep.txt": "deep-keep-src",
    f"{FILTER_TREE}/nest/nested_secret.txt": "tgt-secret",
    f"{FILTER_TREE}/nest/deep/nested_secret.txt": "tgt-deep-secret",
    f"{FILTER_TREE}/nest/deep/deep_only.txt": "tgt-deep",
}


def tree_path() -> str:
    """Absolute path of the seeded rich test subtree within the test user's real home."""
    return f"/home/{os.environ['PC_SWITCHER_TEST_USER']}/{TESTTREE}"


# ---------------------------------------------------------------------------
# Config and filter file
# ---------------------------------------------------------------------------


def _make_home_filter() -> str:
    """Central `merge` filter for the /home folder_sync entry (#166).

    `- .local/share/flatpak` is here purely for cost. The shipped `home.filter` ships no
    such rule on purpose (`PKG-FR-SNAP-DATA-BOUNDARY`: enabling `sync_jobs.flatpak_sync` excludes that store
    non-overridably, and a user who does not enable it legitimately wants it mirrored), but
    the calling test's config does NOT enable flatpak_sync, and the test VMs carry a ~2.8 GB Flathub
    runtime under it (`vm-test-fixtures.sh`). Mirroring it would add gigabytes to a test
    whose subject is filter mechanics on a small seeded tree, and would prove nothing —
    the strict manifests here cover only that tree.

    Exercises, end-to-end through the real rsync command, every central filter surface:
    the machine-identity/regenerable excludes (as before); the #166 include-override idiom
    (keep the dev caches under pcsw-filter/cache while dropping the rest, via `+` re-includes
    ordered before a `-` on the parent's children — the exact ancestor-descent shape the
    shipped home.filter uses); and a wholly-excluded subtree (pcsw-filter/excluded). The
    per-directory .pcswitcher-filter files seeded under pcsw-filter/nest are activated by the
    job's own always-emitted `dir-merge /.pcswitcher-filter` rule, not by this file. Patterns
    are floating (no leading /), matching the shipped home.filter, because /home syncs with
    each user's directory one level below the transfer root.
    """
    return f"""\
- .ssh/id_*
- .ssh/known_hosts
- .ssh/authorized_keys
- .config/tailscale
- .config/Code/Cache
- .config/Code/CachedData
- .config/Code/GPUCache
- .cache
- .local/share/uv/python
- .local/share/flatpak
+ {FILTER_TREE}/cache/
+ {FILTER_TREE}/cache/keep-uv/***
+ {FILTER_TREE}/cache/keep-pip/***
- {FILTER_TREE}/cache/*
- {FILTER_TREE}/excluded
- {TESTTREE}/secret
"""


async def write_filter_file(executor: BashLoginRemoteExecutor) -> None:
    """Write the folder_sync filter_file referenced by the scenario config to a VM (#166)."""
    cmd = (
        "mkdir --parents ~/.config/pc-switcher && "
        f"cat > ~/.config/pc-switcher/home.filter << 'FILTER_EOF'\n{_make_home_filter()}FILTER_EOF"
    )
    result = await executor.run_command(cmd, timeout=10.0)
    assert result.success, f"Failed to write filter file: {result.stderr}"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _seed_files_script(files: dict[str, str]) -> str:
    """Build a `set -e` shell script writing each home-relative path with its exact content.

    `.pcswitcher-filter` files get a trailing newline (rsync merge-file convention); every
    other file is written with `printf %s` (no trailing newline) so an exact content
    comparison on the target is unambiguous.
    """
    lines = ["set -e"]
    for rel, content in files.items():
        rel_path = PurePosixPath(rel)
        fmt = r"'%s\n'" if rel_path.name == ".pcswitcher-filter" else "%s"
        lines.append(f"mkdir --parents ~/{rel_path.parent}")
        lines.append(f"printf {fmt} {shlex.quote(content)} > ~/{rel}")
    return "\n".join(lines)


async def seed_filter_source(executor: BashLoginRemoteExecutor) -> None:
    """Seed the #166 filter-scenario files (incl. per-directory filter files) on the source."""
    script = _seed_files_script({**_FILTER_SRC, **_FILTER_SRC_PERDIR})
    result = await executor.run_command(script, timeout=30.0, login_shell=False)
    assert result.success, f"Failed to seed filter-scenario source files: {result.stderr}"


async def seed_filter_target(executor: BashLoginRemoteExecutor) -> None:
    """Seed the pre-existing target-side files that the #166 filter assertions check against."""
    result = await executor.run_command(_seed_files_script(_FILTER_TGT), timeout=30.0, login_shell=False)
    assert result.success, f"Failed to seed filter-scenario target files: {result.stderr}"


async def seed_rich_tree(executor: BashLoginRemoteExecutor, tree: str) -> None:
    """Create the rich metadata/ownership test tree inside `tree` on a VM.

    Covers the full ownership x permission matrix (user/root/other-user files AND
    directories), special permission bits (setuid/setgid/sticky), a POSIX ACL, a
    backdated mtime, a hard-link pair, a relative symlink, and a config-excluded
    subtree. Root-/other-user-owned entries are created then chowned, so rsync-as-root
    must read and preserve entries the invoking user cannot access.
    """
    result = await executor.run_command(
        f"""set -e
T={tree}
rm --recursive --force "$T"
mkdir --parents "$T"/d700 "$T"/d755 "$T"/setgid_dir "$T"/sticky_dir "$T"/secret

# User-owned files with varied permission bits
printf 'content-600' > "$T/f600.txt"; chmod 600 "$T/f600.txt"
printf 'content-640' > "$T/f640.txt"; chmod 640 "$T/f640.txt"
printf 'content-644' > "$T/f644.txt"; chmod 644 "$T/f644.txt"
printf 'content-755' > "$T/f755.txt"; chmod 755 "$T/f755.txt"
printf 'content-777' > "$T/f777.txt"; chmod 777 "$T/f777.txt"
printf 'content-suid' > "$T/setuid.bin"; chmod 4755 "$T/setuid.bin"
printf 'content-sgid' > "$T/setgid.bin"; chmod 2755 "$T/setgid.bin"

# User-owned directories with varied permission bits (each non-empty)
printf 'in-d700'   > "$T/d700/inside.txt";       chmod 700  "$T/d700"
printf 'in-d755'   > "$T/d755/inside.txt";       chmod 755  "$T/d755"
printf 'in-sgid'   > "$T/setgid_dir/inside.txt"; chmod 2775 "$T/setgid_dir"
printf 'in-sticky' > "$T/sticky_dir/inside.txt"; chmod 1777 "$T/sticky_dir"

# POSIX ACL (numeric uid, need not exist on either machine)
printf 'content-acl' > "$T/acl.txt"; setfacl --modify u:2001:r "$T/acl.txt"

# Backdated mtime
printf 'content-backdated' > "$T/backdated.txt"
touch --date="@{_BACKDATED_MTIME}" "$T/backdated.txt"

# Hard-link pair and relative symlink
printf 'content-hardlink' > "$T/hl_a.txt"
ln "$T/hl_a.txt" "$T/hl_b.txt"
ln --symbolic f644.txt "$T/sym.txt"

# Root-owned file and directory (created as the user, then chowned; the user
# ends up with no access, and rsync-as-root must still read and preserve them).
printf 'content-root-file' > "$T/root_file.txt"
sudo chown 0:0 "$T/root_file.txt"; sudo chmod 600 "$T/root_file.txt"
mkdir --parents "$T/root_dir"; printf 'content-root-dir' > "$T/root_dir/inside.txt"
sudo chown --recursive 0:0 "$T/root_dir"
sudo chmod 700 "$T/root_dir"; sudo chmod 600 "$T/root_dir/inside.txt"

# Other-(system-)user-owned file and directory (invoking user has no access)
printf 'content-other-file' > "$T/other_file.txt"
sudo chown {_OTHER_UID}:{_OTHER_GID} "$T/other_file.txt"; sudo chmod 600 "$T/other_file.txt"
mkdir --parents "$T/other_dir"; printf 'content-other-dir' > "$T/other_dir/inside.txt"
sudo chown --recursive {_OTHER_UID}:{_OTHER_GID} "$T/other_dir"
sudo chmod 700 "$T/other_dir"; sudo chmod 600 "$T/other_dir/inside.txt"

# Excluded subtree (must never reach the target)
printf 'top-secret' > "$T/secret/token.txt"
""",
        timeout=60.0,
        login_shell=False,
    )
    assert result.success, f"Failed to seed rich test tree: {result.stderr}"


async def seed_included_markers(executor: BashLoginRemoteExecutor) -> None:
    """Seed the SC3 inclusion/exclusion marker files in the real home dotdirs."""
    parts = ["set -e"]
    for rel, content in _INCLUDED_MARKERS.items():
        parts.append(f'mkdir --parents ~/"$(dirname {rel})" && printf %s {content!r} > ~/{rel}')
    parts.append(f'mkdir --parents ~/"$(dirname {_EXCLUDED_MARKER})" && printf excluded > ~/{_EXCLUDED_MARKER}')
    result = await executor.run_command("\n".join(parts), timeout=15.0, login_shell=False)
    assert result.success, f"Failed to seed inclusion markers: {result.stderr}"


async def seed_state_sentinels(
    source_executor: BashLoginRemoteExecutor,
    target_executor: BashLoginRemoteExecutor,
) -> None:
    """Put a marker inside each machine's own ADR-016 runtime state dir.

    `reset_pcswitcher_state` wiped the dir, so it is created fresh here. After the sync,
    `assert_runtime_excludes` reads both markers back off the target.
    """
    for executor, label in ((source_executor, "SOURCE"), (target_executor, "TARGET")):
        result = await executor.run_command(
            f"mkdir --parents {STATE_DIR} && printf %s {label.lower()} > {STATE_DIR}/SENTINEL_{label}", timeout=10.0
        )
        assert result.success, f"Failed to seed {label} state sentinel: {result.stderr}"


async def remove_test_artifacts(
    pc1_exec: BashLoginRemoteExecutor,
    pc2_exec: BashLoginRemoteExecutor,
    tree: str,
) -> None:
    """Remove the seeded test subtree, filter tree, inclusion markers, and config from both VMs."""
    markers = " ".join(f"~/{rel}" for rel in (*_INCLUDED_MARKERS, _EXCLUDED_MARKER))
    for name, exec_ in (("pc1", pc1_exec), ("pc2", pc2_exec)):
        res = await exec_.run_command(
            f"sudo rm --recursive --force {tree} {markers} ~/{FILTER_TREE} && "
            "rm --force ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/home.filter",
            timeout=30.0,
            login_shell=False,
        )
        if not res.success:
            print(f"[cleanup] {name} removal warning: {res.stderr}")


async def clear_target_tree(executor: BashLoginRemoteExecutor, tree: str) -> None:
    """Remove the rich test subtree from the target so the first sync starts from nothing."""
    await executor.run_command(f"sudo rm --recursive --force {tree}", timeout=15.0, login_shell=False)


async def assert_tree_absent(executor: BashLoginRemoteExecutor, tree: str, reason: str) -> None:
    """Assert the rich test subtree never reached the target."""
    result = await executor.run_command(f"test ! -e {tree}", timeout=10.0, login_shell=False)
    assert result.success, reason


# ---------------------------------------------------------------------------
# Manifests and post-sync assertions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeManifests:
    """Ownership/permission and content manifests of the rich test tree on one machine."""

    entries: str
    md5s: str


def _manifest_cmd(tree: str) -> str:
    """Command emitting a deterministic `<type> <mode> <uid> <gid> <path>` manifest of `tree`.

    Runs under sudo (root-/other-user-owned entries readable); the excluded
    `secret/` subtree is pruned so source and target manifests match on success.
    """
    return (
        f"cd {tree} && sudo find . -path ./secret -prune -o "
        r"\( -type f -o -type d -o -type l \) -printf '%y %m %U %G %p\n' | LC_ALL=C sort"
    )


def _md5_manifest_cmd(tree: str) -> str:
    """Command emitting C-sorted md5sums of every regular file in `tree` (symlinks/secret skipped)."""
    return (
        f"cd {tree} && sudo find . -path ./secret -prune -o -type f ! -type l "
        r"-exec md5sum {} + | LC_ALL=C sort"
    )


async def capture_manifests(executor: BashLoginRemoteExecutor, tree: str) -> TreeManifests:
    """Read both manifests of `tree` off a machine."""
    entries = await executor.run_command(_manifest_cmd(tree), timeout=30.0, login_shell=False)
    assert entries.success, f"manifest failed: {entries.stderr}"
    md5s = await executor.run_command(_md5_manifest_cmd(tree), timeout=30.0, login_shell=False)
    assert md5s.success, f"md5 manifest failed: {md5s.stderr}"
    return TreeManifests(entries=entries.stdout, md5s=md5s.stdout)


async def assert_manifests_match(
    executor: BashLoginRemoteExecutor,
    tree: str,
    source: TreeManifests,
) -> None:
    """Assert the target's tree manifests are byte-identical to the source's."""
    target = await capture_manifests(executor, tree)
    assert target.entries == source.entries, (
        "Ownership/permission manifest differs after A→B (numeric uid/gid, mode, or special bits).\n"
        f"--- source ---\n{source.entries}\n--- target ---\n{target.entries}"
    )
    assert target.md5s == source.md5s, (
        f"Content md5 manifest differs after A→B.\n--- source ---\n{source.md5s}\n--- target ---\n{target.md5s}"
    )


async def assert_metadata_details(executor: BashLoginRemoteExecutor, tree: str) -> None:
    """Assert ACL, backdated mtime, hard-link inode sharing and symlink target survived the sync."""
    details = await executor.run_command(
        f"getfacl --absolute-names {tree}/acl.txt && echo '---' && "
        f"stat --format='%Y' {tree}/backdated.txt && "
        f"stat --format='%i' {tree}/hl_a.txt && stat --format='%i' {tree}/hl_b.txt && "
        f"readlink {tree}/sym.txt",
        timeout=15.0,
        login_shell=False,
    )
    assert details.success, f"metadata detail checks failed on target: {details.stderr}"
    acl_part, rest = details.stdout.split("---\n", 1)
    lines = rest.strip().splitlines()
    assert "user:2001:r--" in acl_part, f"ACL entry not preserved on target:\n{acl_part}"
    assert int(lines[0]) == _BACKDATED_MTIME, f"backdated mtime not preserved: {lines[0]}"
    assert lines[1] == lines[2], f"hard-link pair not sharing an inode on target ({lines[1]} != {lines[2]})"
    assert lines[3] == "f644.txt", f"symlink target wrong on target: {lines[3]!r}"


async def assert_exclusions(executor: BashLoginRemoteExecutor, tree: str) -> None:
    """Assert the config-excluded subtree is absent and the ADR-016 runtime excludes held."""
    excl = await executor.run_command(f"test ! -e {tree}/secret/token.txt", timeout=10.0, login_shell=False)
    assert excl.success, "Config-excluded secret/token.txt reached the target (exclusion failed)."
    runtime = await executor.run_command(
        f"test ! -e {STATE_DIR}/SENTINEL_SOURCE && "
        f"test -e {STATE_DIR}/SENTINEL_TARGET && "
        f"test -e ~/.local/bin/pc-switcher",
        timeout=10.0,
    )
    assert runtime.success, (
        "ADR-016 runtime exclusion failed: the source's state reached the target, or the target's own "
        "state/install was clobbered by the --delete mirror of /home."
    )


async def assert_included_markers(executor: BashLoginRemoteExecutor) -> None:
    """Assert SC3 inclusion: dev-tool cache + VS Code user state synced, excluded sibling did not."""
    marker_rels = list(_INCLUDED_MARKERS)
    inc = await executor.run_command(
        " && echo '|' && ".join(f"cat ~/{rel}" for rel in marker_rels)
        + f" && echo '|' && ( test ! -e ~/{_EXCLUDED_MARKER} && echo EXCLUDED_ABSENT )",
        timeout=10.0,
        login_shell=False,
    )
    assert inc.success, f"SC3 inclusion checks failed on target: {inc.stderr}"
    inc_parts = [p.strip() for p in inc.stdout.split("|")]
    for rel, part in zip(marker_rels, inc_parts, strict=False):
        assert part == _INCLUDED_MARKERS[rel], (
            f"Included path {rel} not synced to target (SC3): got {part!r}, want {_INCLUDED_MARKERS[rel]!r}"
        )
    assert "EXCLUDED_ABSENT" in inc_parts[-1], (
        f"Config-excluded {_EXCLUDED_MARKER} reached the target (SC3 exclusion failed)."
    )


async def assert_filter_outcomes(executor: BashLoginRemoteExecutor) -> None:
    """Assert the target's #166 filter tree matches `_FILTER_EXPECT` after the first sync.

    One command probes every expected path, emitting `<path>@@F@@<content-or-__ABSENT__>@@R@@`
    records (printable separators, robust to any file content); the parsed results are then
    compared here so a single assertion reports all discrepancies at once.
    """
    probe = "\n".join(
        f"printf %s {shlex.quote(rel)}; printf '@@F@@'; "
        f"if [ -e ~/{rel} ]; then cat ~/{rel}; else printf %s __ABSENT__; fi; printf '@@R@@'"
        for rel in _FILTER_EXPECT
    )
    result = await executor.run_command(probe, timeout=30.0, login_shell=False)
    assert result.success, f"filter-outcome probe failed on target: {result.stderr}"

    got: dict[str, str] = {}
    for record in result.stdout.split("@@R@@"):
        if not record:
            continue
        path, _, content = record.partition("@@F@@")
        got[path] = content

    errors: list[str] = []
    for rel, expected in _FILTER_EXPECT.items():
        actual = got.get(rel)
        if expected is None:
            if actual != "__ABSENT__":
                errors.append(f"{rel}: expected ABSENT on target, got {actual!r}")
        elif actual is None or actual == "__ABSENT__":
            errors.append(f"{rel}: expected {expected!r}, but the file is ABSENT on target")
        elif actual.strip() != expected.strip():
            errors.append(f"{rel}: expected {expected!r}, got {actual!r}")
    assert not errors, "Filter-rule outcomes on target are wrong:\n" + "\n".join(errors)


# ---------------------------------------------------------------------------
# Reverse direction (B→A)
# ---------------------------------------------------------------------------


async def mutate_tree(executor: BashLoginRemoteExecutor, tree: str) -> None:
    """Apply the round-trip mutation set — add / modify / delete file / delete dir / chmod."""
    mutate = await executor.run_command(
        f"""set -e
T={tree}
printf 'added-on-pc2' > "$T/added.txt"; chmod 750 "$T/added.txt"; touch --date="@{_ADDITION_MTIME}" "$T/added.txt"
printf 'MODIFIED-644' > "$T/f644.txt"
rm --force "$T/f600.txt"
rm --recursive --force "$T/d700"
chmod 700 "$T/f755.txt"
""",
        timeout=15.0,
        login_shell=False,
    )
    assert mutate.success, f"Mutation failed: {mutate.stderr}"


async def assert_mutations_propagated(executor: BashLoginRemoteExecutor, tree: str) -> None:
    """Assert every change made by `mutate_tree` arrived on the machine that received the back-sync."""
    roundtrip = await executor.run_command(
        f"cat {tree}/added.txt && echo '|' && "
        f"stat --format='%a %Y' {tree}/added.txt && echo '|' && "
        f"cat {tree}/f644.txt && echo '|' && "
        f"stat --format='%a' {tree}/f755.txt && echo '|' && "
        f"( test ! -e {tree}/f600.txt && echo GONE_FILE ) && "
        f"( test ! -e {tree}/d700 && echo GONE_DIR )",
        timeout=15.0,
        login_shell=False,
    )
    assert roundtrip.success, f"checks after B→A failed: {roundtrip.stderr}"
    added_content, added_meta, f644_content, f755_mode, gone = [p.strip() for p in roundtrip.stdout.split("|")]
    assert added_content == "added-on-pc2", f"addition content wrong after B→A: {added_content!r}"
    added_mode, added_mtime = added_meta.split()
    assert added_mode == "750", f"addition perms not preserved on B→A: {added_mode}"
    assert int(added_mtime) == _ADDITION_MTIME, f"addition mtime not preserved: {added_mtime}"
    assert f644_content == "MODIFIED-644", f"modification not propagated on B→A: {f644_content!r}"
    assert f755_mode == "700", f"permission change not propagated on B→A: {f755_mode}"
    assert "GONE_FILE" in gone, "file deletion (f600.txt) not propagated on B→A"
    assert "GONE_DIR" in gone, "directory deletion (d700) not propagated on B→A"


# ---------------------------------------------------------------------------
# Rehearsals (ADR-014): the same seeded trees, asserted NOT to have moved
# ---------------------------------------------------------------------------


def filter_tree_path() -> str:
    """Absolute path of the seeded filter subtree within the test user's real home."""
    return f"/home/{os.environ['PC_SWITCHER_TEST_USER']}/{FILTER_TREE}"


async def assert_manifests_unchanged(
    executor: BashLoginRemoteExecutor,
    tree: str,
    before: TreeManifests,
    reason: str,
) -> None:
    """Assert `tree` still matches manifests captured off the SAME machine earlier."""
    after = await capture_manifests(executor, tree)
    assert after.entries == before.entries, (
        f"{reason}\nOwnership/permission manifest moved.\n--- before ---\n{before.entries}\n--- after ---\n"
        f"{after.entries}"
    )
    assert after.md5s == before.md5s, (
        f"{reason}\nContent md5 manifest moved.\n--- before ---\n{before.md5s}\n--- after ---\n{after.md5s}"
    )
