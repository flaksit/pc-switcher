"""Integration tests for FolderSyncJob against the real /home on pc1/pc2.

Covers the ROADMAP Phase 1 success criteria 1-5 end-to-end against the real
pc1/pc2 Hetzner VMs (Ubuntu 24.04, btrfs @home at /home, sudo rsync, acl), by
syncing the *real* /home — the production default scenario — rather than a
scratch directory.

  - Criterion 1: Byte-identical content after A→B (md5 manifest comparison).
  - Criterion 2: Preserved metadata — numeric uid/gid, permissions (including
    setuid/setgid/sticky bits), POSIX ACLs, mtime, hard-link inode sharing,
    symlink — across user-, root-, and other-(system-)user-owned files AND
    directories the invoking user cannot even read (rsync runs as root).
  - Criterion 3: Machine-specific / excluded items absent on the target; other
    content present.
  - Criterion 4: Additions, modifications, file deletions AND directory deletions
    propagate B→A with metadata preserved.
  - Criterion 5 (ADR-015): a normal A→B / B→A / A→B round-trip proceeds WITHOUT
    any override; the first-ever sync (W1) is gated by --allow-first-sync; a
    dry-run is a read-only rehearsal that writes no state (D-12).
  - ADR-016: pc-switcher's own runtime files (state/lock/logs, uv tool install,
    entry-point shim) are hardcoded-excluded so a --delete mirror of /home can
    never clobber them (verified via sentinels).

Why syncing the real /home is safe here:
  - Both VMs boot from an identical btrfs baseline, so the --delete mirror only
    propagates the deliberately-seeded test subtree.
  - pc-switcher's runtime files are protected by the hardcoded excludes (ADR-016).
  - Heavy regenerable trees (.cache, ~/.local/share/uv/python) are excluded in the
    TEST config to keep the transfer small and fast — a test-config choice, not a
    hardcoded rule (see ADR-016: the only hardcoded excludes are the runtime files).
  - .ssh/known_hosts is excluded because reset-vm.sh seeds each VM's known_hosts
    with the OTHER VM's host key; mirroring it would break the next sync's host-key
    verification and make the suite flaky.
  - Each test removes its seeded subtree in `finally`; reset_pcswitcher_state clears
    pc-switcher state before and after.

VM Requirements:
  - pc1, pc2 on Ubuntu 24.04 LTS with btrfs @home subvolume at /home.
  - rsync, acl packages installed; NOPASSWD sudo for the test user.
  - PC_SWITCHER_TEST_PC1_HOST, PC_SWITCHER_TEST_PC2_HOST, PC_SWITCHER_TEST_USER,
    HCLOUD_TOKEN environment variables set (handled by conftest).
  - Current branch pushed to origin (install-from-branch uses git remote).
"""

from __future__ import annotations

import os

from pcswitcher.executor import BashLoginRemoteExecutor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Seeded test subtree, relative to the user's home. All rich test data lives here
# so assertions have a known location while the sync still mirrors the whole /home.
_TESTTREE = "pcsw-itest"

# A single non-root system user used to prove numeric uid/gid preservation for
# files the invoking user cannot access. uid/gid 1 = daemon on Ubuntu (numeric so
# the assertion does not depend on name resolution on either machine).
_OTHER_UID = 1
_OTHER_GID = 1

# Known mtimes (Unix epoch seconds) for backdated-mtime assertions.
_BACKDATED_MTIME = 1705312800  # 2024-01-15 10:00:00 UTC
_ADDITION_MTIME = 1710000000  # 2024-03-09 16:00:00 UTC

# pc-switcher runtime state dir (ADR-016 hardcoded exclude target).
_STATE_DIR = "~/.local/share/pc-switcher"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tree(user: str) -> str:
    """Absolute path of the seeded rich test subtree within the real home."""
    return f"/home/{user}/{_TESTTREE}"


def _make_config() -> str:
    """pc-switcher config that folder-syncs the real /home.

    Excludes combine the realistic machine-specific set with test-stability and
    performance excludes (see module docstring). The hardcoded runtime-file
    excludes (ADR-016) are NOT listed here — they are added by the job itself and
    are exercised by the sentinel assertions.
    """
    return f"""\
logging:
  file: DEBUG
  tui: INFO
  external: WARNING
sync_jobs:
  folder_sync: true
disk_space_monitor:
  preflight_minimum: "5%"
  runtime_minimum: "3%"
  warning_threshold: "10%"
  check_interval: 5
btrfs_snapshots:
  subvolumes:
    - "@"
    - "@home"
  keep_recent: 2
folder_sync:
  folders:
    - path: /home
      enabled: true
      excludes:
        # Machine-specific (realistic default set)
        - .ssh/id_*
        - .config/tailscale
        - .config/Code/Cache
        - .config/Code/CachedData
        - .config/Code/GPUCache
        # Test-stability: reset-vm.sh gives each VM the other's host key
        - .ssh/known_hosts
        # Performance: heavy regenerable trees (keep the transfer small)
        - .cache
        - .local/share/uv/python
        # Test-only excluded subtree (proves the exclusion mechanism)
        - {_TESTTREE}/secret
"""


async def _write_config(executor: BashLoginRemoteExecutor, config: str) -> None:
    """Write the pc-switcher test config to the remote VM."""
    result = await executor.run_command(
        f"mkdir -p ~/.config/pc-switcher && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n{config}CONF_EOF",
        timeout=10.0,
    )
    assert result.success, f"Failed to write config: {result.stderr}"


async def _seed_rich_tree(executor: BashLoginRemoteExecutor, tree: str) -> None:
    """Create the rich metadata/ownership test tree inside `tree` on a VM.

    Layout (relative to `tree`), covering the full ownership x permission matrix:
      User-owned files, varied perms + special bits:
        f600.txt(600) f640.txt(640) f644.txt(644) f755.txt(755) f777.txt(777)
        setuid.bin(4755) setgid.bin(2755)
      User-owned dirs, varied perms + special bits:
        d700/(700) d755/(755) setgid_dir/(2775) sticky_dir/(1777)   (each non-empty)
      acl.txt        — setfacl u:2001:r (numeric, need not exist)
      backdated.txt  — mtime set to _BACKDATED_MTIME
      hl_a.txt/hl_b.txt — hard-link pair (shared inode, rsync -H)
      sym.txt        — relative symlink → f644.txt
      root_file.txt          — root-owned (0:0, 600), unreadable by the user
      root_dir/inside.txt    — root-owned dir (0:0, 700) + file (0:0, 600)
      other_file.txt         — daemon-owned (1:1, 600), unreadable by the user
      other_dir/inside.txt   — daemon-owned dir (1:1, 700) + file (1:1, 600)
      secret/token.txt       — EXCLUDED by config ({_TESTTREE}/secret)
    """
    result = await executor.run_command(
        f"""set -e
T={tree}
rm -rf "$T"
mkdir -p "$T"/d700 "$T"/d755 "$T"/setgid_dir "$T"/sticky_dir "$T"/secret

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
printf 'content-acl' > "$T/acl.txt"; setfacl -m u:2001:r "$T/acl.txt"

# Backdated mtime
printf 'content-backdated' > "$T/backdated.txt"
touch -d "@{_BACKDATED_MTIME}" "$T/backdated.txt"

# Hard-link pair and relative symlink
printf 'content-hardlink' > "$T/hl_a.txt"
ln "$T/hl_a.txt" "$T/hl_b.txt"
ln -s f644.txt "$T/sym.txt"

# Root-owned file and directory (created as the user, then chowned; the user
# ends up with no access, and rsync-as-root must still read and preserve them).
printf 'content-root-file' > "$T/root_file.txt"
sudo chown 0:0 "$T/root_file.txt"; sudo chmod 600 "$T/root_file.txt"
mkdir -p "$T/root_dir"; printf 'content-root-dir' > "$T/root_dir/inside.txt"
sudo chown -R 0:0 "$T/root_dir"
sudo chmod 700 "$T/root_dir"; sudo chmod 600 "$T/root_dir/inside.txt"

# Other-(system-)user-owned file and directory (invoking user has no access)
printf 'content-other-file' > "$T/other_file.txt"
sudo chown {_OTHER_UID}:{_OTHER_GID} "$T/other_file.txt"; sudo chmod 600 "$T/other_file.txt"
mkdir -p "$T/other_dir"; printf 'content-other-dir' > "$T/other_dir/inside.txt"
sudo chown -R {_OTHER_UID}:{_OTHER_GID} "$T/other_dir"
sudo chmod 700 "$T/other_dir"; sudo chmod 600 "$T/other_dir/inside.txt"

# Excluded subtree (must never reach the target)
printf 'top-secret' > "$T/secret/token.txt"
""",
        timeout=60.0,
        login_shell=False,
    )
    assert result.success, f"Failed to seed rich test tree: {result.stderr}"


def _manifest_cmd(tree: str) -> str:
    """Shell command emitting a deterministic ownership/permission manifest of `tree`.

    One line per entry: `<type> <octal-mode-incl-special-bits> <uid> <gid> <relpath>`,
    C-sorted. Runs under sudo so root-/other-user-owned entries are readable. The
    excluded `secret/` subtree is pruned so source and target manifests match.
    """
    return (
        f"cd {tree} && sudo find . -path ./secret -prune -o "
        r"\( -type f -o -type d -o -type l \) -printf '%y %m %U %G %p\n' | LC_ALL=C sort"
    )


def _md5_manifest_cmd(tree: str) -> str:
    """Shell command emitting C-sorted md5sums of every regular file in `tree`.

    Symlinks and the excluded `secret/` subtree are skipped; paths are relative
    (./…) so source and target output match byte-for-byte on success.
    """
    return (
        f"cd {tree} && sudo find . -path ./secret -prune -o -type f ! -type l "
        r"-exec md5sum {} + | LC_ALL=C sort"
    )


async def _remove_test_artifacts(
    pc1_exec: BashLoginRemoteExecutor,
    pc2_exec: BashLoginRemoteExecutor,
    tree: str,
) -> None:
    """Remove the seeded test subtree and config from both VMs (best-effort cleanup)."""
    for name, exec_ in (("pc1", pc1_exec), ("pc2", pc2_exec)):
        res = await exec_.run_command(
            f"sudo rm -rf {tree} && rm -f ~/.config/pc-switcher/config.yaml",
            timeout=30.0,
            login_shell=False,
        )
        if not res.success:
            print(f"[cleanup] {name} removal warning: {res.stderr}")


# ---------------------------------------------------------------------------
# Consolidated end-to-end scenario: A→B first-sync, round-trip B→A, A→B again
# ---------------------------------------------------------------------------


class TestHomeSyncEndToEnd:
    """Full folder-sync scenario against the real /home (criteria 1-5, ADR-015/016)."""

    async def test_home_sync_metadata_ownership_and_roundtrip(  # noqa: PLR0915
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """One scenario, three syncs, covering all Phase-1 folder-sync criteria.

        1. Seed a rich tree on pc1 (+ runtime sentinels on both VMs).
        2. A→B first sync (--allow-first-sync). Assert:
           - content (md5 manifest) and metadata (type/mode/uid/gid manifest) match,
             including root-/other-user-owned files AND dirs and special-bit perms;
           - ACL, backdated mtime, hard-link inode sharing, symlink target preserved;
           - excluded subtree absent on pc2;
           - ADR-016 runtime excludes held: pc1's state sentinel did NOT reach pc2,
             pc2's own state sentinel and install shim survived the --delete mirror.
        3. Mutate pc2 (add, modify, delete file, delete directory, change perms),
           B→A, assert all mutations propagated to pc1.
        4. A→B again with no override — clean round-trip must not trip the
           out-of-order gate (ADR-015 #159).
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)
        user = os.environ["PC_SWITCHER_TEST_USER"]
        tree = _tree(user)

        try:
            await _write_config(pc1_executor, _make_config())
            await _seed_rich_tree(pc1_executor, tree)

            # Runtime-exclude sentinels: a marker inside each VM's own state dir.
            # (reset_pcswitcher_state wiped the state dir, so create it fresh here.)
            await pc1_executor.run_command(
                f"mkdir -p {_STATE_DIR} && printf pc1 > {_STATE_DIR}/SENTINEL_SOURCE",
                timeout=10.0,
            )
            await pc2_executor.run_command(
                f"mkdir -p {_STATE_DIR} && printf pc2 > {_STATE_DIR}/SENTINEL_TARGET",
                timeout=10.0,
            )

            # Capture source manifests before the sync.
            src_manifest = await pc1_executor.run_command(_manifest_cmd(tree), timeout=30.0, login_shell=False)
            assert src_manifest.success, f"source manifest failed: {src_manifest.stderr}"
            src_md5 = await pc1_executor.run_command(_md5_manifest_cmd(tree), timeout=30.0, login_shell=False)
            assert src_md5.success, f"source md5 manifest failed: {src_md5.stderr}"

            # --- Step 2: A→B first sync ---
            sync_ab = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ab.success, (
                f"A→B first sync failed.\nexit={sync_ab.exit_code}\nstdout: {sync_ab.stdout}\nstderr: {sync_ab.stderr}"
            )

            # Content + metadata: target manifests must equal source manifests exactly.
            tgt_manifest = await pc2_executor.run_command(_manifest_cmd(tree), timeout=30.0, login_shell=False)
            assert tgt_manifest.success, f"target manifest failed: {tgt_manifest.stderr}"
            assert tgt_manifest.stdout == src_manifest.stdout, (
                "Ownership/permission manifest differs after A→B "
                "(numeric uid/gid, mode, or special bits not preserved).\n"
                f"--- pc1 ---\n{src_manifest.stdout}\n--- pc2 ---\n{tgt_manifest.stdout}"
            )
            tgt_md5 = await pc2_executor.run_command(_md5_manifest_cmd(tree), timeout=30.0, login_shell=False)
            assert tgt_md5.success, f"target md5 manifest failed: {tgt_md5.stderr}"
            assert tgt_md5.stdout == src_md5.stdout, (
                "Content md5 manifest differs after A→B.\n"
                f"--- pc1 ---\n{src_md5.stdout}\n--- pc2 ---\n{tgt_md5.stdout}"
            )

            # ACL, backdated mtime, hard-link inode sharing, symlink target.
            details = await pc2_executor.run_command(
                f"getfacl -p {tree}/acl.txt && echo '---' && "
                f"stat -c '%Y' {tree}/backdated.txt && "
                f"stat -c '%i' {tree}/hl_a.txt && stat -c '%i' {tree}/hl_b.txt && "
                f"readlink {tree}/sym.txt",
                timeout=15.0,
                login_shell=False,
            )
            assert details.success, f"metadata detail checks failed on pc2: {details.stderr}"
            acl_part, rest = details.stdout.split("---\n", 1)
            lines = rest.strip().splitlines()
            assert "user:2001:r--" in acl_part, f"ACL entry not preserved on pc2:\n{acl_part}"
            assert int(lines[0]) == _BACKDATED_MTIME, f"backdated mtime not preserved: {lines[0]}"
            assert lines[1] == lines[2], f"hard-link pair not sharing an inode on pc2 ({lines[1]} != {lines[2]})"
            assert lines[3] == "f644.txt", f"symlink target wrong on pc2: {lines[3]!r}"

            # Exclusions: the config-excluded subtree must be absent on pc2.
            excl = await pc2_executor.run_command(
                f"test ! -e {tree}/secret/token.txt", timeout=10.0, login_shell=False
            )
            assert excl.success, "Config-excluded secret/token.txt reached pc2 (exclusion failed)."

            # ADR-016 runtime excludes: pc1's state sentinel must NOT have reached pc2,
            # and pc2's own state sentinel + install shim must have survived --delete.
            runtime = await pc2_executor.run_command(
                f"test ! -e {_STATE_DIR}/SENTINEL_SOURCE && "
                f"test -e {_STATE_DIR}/SENTINEL_TARGET && "
                f"test -e ~/.local/bin/pc-switcher",
                timeout=10.0,
            )
            assert runtime.success, (
                "ADR-016 runtime exclusion failed: either pc1's state reached pc2, or pc2's own "
                "state/install was clobbered by the --delete mirror of /home."
            )

            # --- Step 3: mutate pc2, then B→A ---
            mutate = await pc2_executor.run_command(
                f"""set -e
T={tree}
printf 'added-on-pc2' > "$T/added.txt"; chmod 750 "$T/added.txt"; touch -d "@{_ADDITION_MTIME}" "$T/added.txt"
printf 'MODIFIED-644' > "$T/f644.txt"
rm -f "$T/f600.txt"
rm -rf "$T/d700"
chmod 700 "$T/f755.txt"
""",
                timeout=15.0,
                login_shell=False,
            )
            assert mutate.success, f"Mutation on pc2 failed: {mutate.stderr}"

            sync_ba = await pc2_executor.run_command(
                "pc-switcher sync pc1 --yes",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ba.success, (
                f"B→A sync failed.\nexit={sync_ba.exit_code}\nstdout: {sync_ba.stdout}\nstderr: {sync_ba.stderr}"
            )

            # Assert all five mutation kinds propagated to pc1.
            roundtrip = await pc1_executor.run_command(
                f"cat {tree}/added.txt && echo '|' && "
                f"stat -c '%a %Y' {tree}/added.txt && echo '|' && "
                f"cat {tree}/f644.txt && echo '|' && "
                f"stat -c '%a' {tree}/f755.txt && echo '|' && "
                f"( test ! -e {tree}/f600.txt && echo GONE_FILE ) && "
                f"( test ! -e {tree}/d700 && echo GONE_DIR )",
                timeout=15.0,
                login_shell=False,
            )
            assert roundtrip.success, f"pc1 checks after B→A failed: {roundtrip.stderr}"
            added_content, added_meta, f644_content, f755_mode, gone = [p.strip() for p in roundtrip.stdout.split("|")]
            assert added_content == "added-on-pc2", f"addition content wrong on pc1: {added_content!r}"
            added_mode, added_mtime = added_meta.split()
            assert added_mode == "750", f"addition perms not preserved on B→A: {added_mode}"
            assert int(added_mtime) == _ADDITION_MTIME, f"addition mtime not preserved: {added_mtime}"
            assert f644_content == "MODIFIED-644", f"modification not propagated on B→A: {f644_content!r}"
            assert f755_mode == "700", f"permission change not propagated on B→A: {f755_mode}"
            assert "GONE_FILE" in gone, "file deletion (f600.txt) not propagated on B→A"
            assert "GONE_DIR" in gone, "directory deletion (d700) not propagated on B→A"

            # --- Step 4: A→B again — clean round-trip must not trip the out-of-order gate ---
            sync_ab2 = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ab2.success, (
                f"Second A→B failed (out-of-order gate wrongly tripped for a clean round-trip, ADR-015 #159).\n"
                f"exit={sync_ab2.exit_code}\nstdout: {sync_ab2.stdout}\nstderr: {sync_ab2.stderr}"
            )

        finally:
            await _remove_test_artifacts(pc1_executor, pc2_executor, tree)


# ---------------------------------------------------------------------------
# Topology safety gates: first-sync (W1) block and dry-run rehearsal (ADR-015/014)
# ---------------------------------------------------------------------------


class TestHomeSyncSafetyGates:
    """First-sync (W1) gate and dry-run read-only rehearsal against the real /home."""

    async def test_first_sync_gate_and_dry_run(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """W1 first-sync gate blocks non-interactively; --dry-run rehearses without writing state.

        1. Seed a marker on pc1; pc2 has no sync history.
        2. A→B without a bypass flag → blocked (W1 gate, non-interactive) and the
           marker must NOT reach pc2.
        3. A→B --dry-run → proceeds (ADR-014) but writes no files on pc2 and does
           not create/update pc1's sync-history (D-12).
        4. A→B --allow-first-sync → proceeds and the marker reaches pc2.
        """
        _ = (pc1_with_pcswitcher_mod, reset_pcswitcher_state)
        user = os.environ["PC_SWITCHER_TEST_USER"]
        tree = _tree(user)
        marker = f"{tree}/marker.txt"

        try:
            await _write_config(pc1_executor, _make_config())
            await pc1_executor.run_command(
                f"mkdir -p {tree} && printf 'marker' > {marker}", timeout=10.0, login_shell=False
            )
            # Ensure pc2 has no leftover marker from a prior run.
            await pc2_executor.run_command(f"sudo rm -rf {tree}", timeout=15.0, login_shell=False)

            # Step 2: blocked first sync (no flag, non-interactive).
            blocked = await pc1_executor.run_command("pc-switcher sync pc2 --yes", timeout=180.0, login_shell=True)
            assert not blocked.success, (
                f"W1 first-sync gate should block non-interactively, got exit {blocked.exit_code}.\n"
                f"stdout: {blocked.stdout}"
            )
            combined = (blocked.stdout + blocked.stderr).lower()
            assert "out-of-order" in combined or "target" in combined, (
                f"First-sync gate message unexpected.\nstdout: {blocked.stdout}\nstderr: {blocked.stderr}"
            )
            pc2_no_marker = await pc2_executor.run_command(f"test ! -e {marker}", timeout=10.0, login_shell=False)
            assert pc2_no_marker.success, "Blocked first sync still transferred the marker to pc2."

            # Step 3: dry-run rehearsal — proceeds, writes nothing, updates no history.
            history_before = await pc1_executor.run_command(
                f"cat {_STATE_DIR}/sync-history.json 2>/dev/null || echo absent", timeout=10.0
            )
            assert history_before.success
            dry = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --dry-run", timeout=180.0, login_shell=True
            )
            assert dry.success, f"--dry-run should not be blocked (ADR-014).\nstderr: {dry.stderr}"
            pc2_still_no_marker = await pc2_executor.run_command(
                f"test ! -e {marker}", timeout=10.0, login_shell=False
            )
            assert pc2_still_no_marker.success, "--dry-run transferred the marker to pc2 (must be read-only)."
            history_after = await pc1_executor.run_command(
                f"cat {_STATE_DIR}/sync-history.json 2>/dev/null || echo absent", timeout=10.0
            )
            assert history_after.success
            assert history_before.stdout.strip() == history_after.stdout.strip(), (
                "--dry-run updated pc1 sync-history (violates D-12).\n"
                f"before={history_before.stdout.strip()!r} after={history_after.stdout.strip()!r}"
            )

            # Step 4: --allow-first-sync proceeds and the marker reaches pc2.
            allow = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync", timeout=300.0, login_shell=True
            )
            assert allow.success, f"--allow-first-sync sync failed.\nstderr: {allow.stderr}"
            pc2_has_marker = await pc2_executor.run_command(f"test -f {marker}", timeout=10.0, login_shell=False)
            assert pc2_has_marker.success, "Marker missing on pc2 after --allow-first-sync."

        finally:
            await _remove_test_artifacts(pc1_executor, pc2_executor, tree)
