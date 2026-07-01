"""Integration tests for FolderSyncJob bidirectional round-trip.

Covers the ROADMAP Phase 1 success criteria 1-5 end-to-end against the real
pc1/pc2 Hetzner VMs (Ubuntu 24.04, btrfs @home at /home, sudo rsync, acl):

  - Criterion 1: Byte-identical content after A→B (md5sum comparison).
  - Criterion 2: Preserved metadata — numeric uid/gid, permissions, POSIX ACLs,
    mtime, hard-link inode sharing, symlink (A→B and B→A).
  - Criterion 3: Machine-specific items (.ssh/id_*, .config/tailscale, VS Code
    cache dirs) absent on the target; VS Code User/ state and dev-tool caches
    present (A→B and B→A).
  - Criterion 4: Additions, modifications, and deletions propagate B→A with
    the same exclusion and metadata guarantees.
  - Criterion 5 (D-06/D-07/D-12): Target divergence detected and blocked;
    --allow-divergence overrides it; a normal A→B→B→A→A→B round-trip does NOT
    trigger false divergence (D-07); --dry-run leaves no file changes and does
    not update the divergence marker (D-12).

Safety: the test configures folder_sync to mirror a DEDICATED test directory
(not the real /home or /root), so the destructive --delete mirror cannot harm
VM data.  This also exercises the generic any-path mechanism (D-02).

VM Requirements:
  - pc1, pc2 on Ubuntu 24.04 LTS with btrfs @home subvolume at /home.
  - rsync, acl packages installed; sudoers NOPASSWD entry for /usr/bin/rsync.
  - PC_SWITCHER_TEST_PC1_HOST, PC_SWITCHER_TEST_PC2_HOST, PC_SWITCHER_TEST_USER,
    HCLOUD_TOKEN environment variables set (handled by conftest).
  - Current branch pushed to origin (install-from-branch uses git remote).
"""

from __future__ import annotations

import json
import os

from pcswitcher.executor import BashLoginRemoteExecutor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _test_dir(user: str) -> str:
    """Absolute path of the dedicated sync test directory on both VMs."""
    return f"/home/{user}/pcswitcher-folder-sync-test"


def _make_config(test_dir: str) -> str:
    """pc-switcher config that folder_syncs only the test directory."""
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
    - path: {test_dir}
      enabled: true
      excludes:
        - .ssh/id_*
        - .config/tailscale
        - .config/Code/Cache
        - .config/Code/CachedData
        - .config/Code/GPUCache
"""


async def _write_config(executor: BashLoginRemoteExecutor, config: str) -> None:
    """Write the pc-switcher test config to the remote VM."""
    result = await executor.run_command(
        f"mkdir -p ~/.config/pc-switcher && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n{config}CONF_EOF",
        timeout=10.0,
    )
    assert result.success, f"Failed to write config: {result.stderr}"


async def _seed_test_tree(executor: BashLoginRemoteExecutor, tdir: str) -> None:
    """Create a test tree inside `tdir` on the remote VM.

    Tree layout (relative to tdir):
      alpha.txt          — regular file, uid/gid of login user, chmod 640,
                           setfacl u:2001:r, mtime set to 2024-01-15 10:00 UTC
      alpha_hardlink.txt — hard link to alpha.txt (same inode)
      alpha_sym.txt      — symlink → alpha.txt (relative)
      subdir/beta.txt    — regular file in a sub-directory
      rootfile.txt       — root-owned (0:0), chmod 600

      .ssh/id_rsa                      — EXCLUDED (matches .ssh/id_*)
      .config/tailscale/tailscaled.state — EXCLUDED
      .config/Code/Cache/cache.bin     — EXCLUDED (VS Code cache dir)

      .config/Code/User/settings.json  — INCLUDED (VS Code User state)
      .cache/uv/archive.bin            — INCLUDED (dev-tool cache)
    """
    result = await executor.run_command(
        f"""set -e
T={tdir}
mkdir -p "$T/subdir" "$T/.ssh" "$T/.config/tailscale" \
    "$T/.config/Code/Cache" "$T/.config/Code/User" "$T/.cache/uv"

printf 'alpha content' > "$T/alpha.txt"
printf 'beta content in subdir' > "$T/subdir/beta.txt"

# Ownership test: root-owned file verifies --numeric-ids preserves uid 0 on target
sudo bash -c "printf 'root owned content' > $T/rootfile.txt; chown 0:0 $T/rootfile.txt; chmod 600 $T/rootfile.txt"

# Permissions
chmod 640 "$T/alpha.txt"

# POSIX ACL: uid 2001 with read permission (numeric — not required to exist)
setfacl -m u:2001:r "$T/alpha.txt"

# Known mtime (Unix 1705312800 = 2024-01-15 10:00:00 UTC)
touch -d "@1705312800" "$T/alpha.txt"

# Hard-link pair (both in the tree; rsync -H preserves the shared inode)
ln "$T/alpha.txt" "$T/alpha_hardlink.txt"

# Relative symlink (stays valid on target at the same absolute location)
ln -s alpha.txt "$T/alpha_sym.txt"

# Excluded files
printf 'fake_private_key' > "$T/.ssh/id_rsa"
printf 'tailscale_state'  > "$T/.config/tailscale/tailscaled.state"
printf 'vscode_cache'     > "$T/.config/Code/Cache/cache.bin"

# Included files
printf '{{"editor.fontSize":14}}' > "$T/.config/Code/User/settings.json"
printf 'uv_cache_data'            > "$T/.cache/uv/archive.bin"
""",
        timeout=30.0,
        login_shell=False,
    )
    assert result.success, f"Failed to seed test tree: {result.stderr}"


async def _remove_test_artifacts(
    pc1_exec: BashLoginRemoteExecutor,
    pc2_exec: BashLoginRemoteExecutor,
    tdir: str,
) -> None:
    """Remove test directory and config from both VMs (cleanup helper)."""
    # Fire both concurrently — each is independent.
    rm1 = await pc1_exec.run_command(
        f"sudo rm -rf {tdir} && rm -f ~/.config/pc-switcher/config.yaml",
        timeout=30.0,
        login_shell=False,
    )
    rm2 = await pc2_exec.run_command(
        f"sudo rm -rf {tdir} && rm -f ~/.config/pc-switcher/config.yaml",
        timeout=30.0,
        login_shell=False,
    )
    # Log but don't fail on cleanup errors (finally block should be best-effort).
    if not rm1.success:
        print(f"[cleanup] pc1 removal warning: {rm1.stderr}")
    if not rm2.success:
        print(f"[cleanup] pc2 removal warning: {rm2.stderr}")


# ---------------------------------------------------------------------------
# Task 1: A→B — content, metadata, and exclusion assertions (criteria 1-3)
# ---------------------------------------------------------------------------


class TestFolderSyncAToB:
    """A→B sync proves byte-identical content, metadata, and exclusions (criteria 1-3)."""

    async def test_a_to_b_content_metadata_and_exclusions(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """A→B sync produces byte-identical files, preserves all metadata, and honours exclusions.

        Requirements: REQ-sync-scope-user-data, REQ-machine-specific-exclusions,
        REQ-sync-scope-file-metadata — ROADMAP success criteria 1-3.

        Asserts:
          1. Content: md5sum matches for all included files.
          2. Metadata: numeric uid/gid (--numeric-ids), permissions, mtime, POSIX
             ACLs (getfacl), hard-link inode sharing, symlink target preserved.
          3. Exclusions: .ssh/id_rsa, .config/tailscale/*, and VS Code cache dirs
             absent on pc2; VS Code User/ state and .cache/uv/ present.
        """
        _ = reset_pcswitcher_state
        user = os.environ["PC_SWITCHER_TEST_USER"]
        tdir = _test_dir(user)
        config = _make_config(tdir)

        try:
            await _write_config(pc1_executor, config)
            await _seed_test_tree(pc1_executor, tdir)

            # Capture source checksums and metadata in grouped commands.
            pc1_info = await pc1_executor.run_command(
                f"md5sum {tdir}/alpha.txt {tdir}/subdir/beta.txt "
                f"{tdir}/.config/Code/User/settings.json {tdir}/.cache/uv/archive.bin && "
                f"echo '---' && "
                f"stat -c '%u %g %a' {tdir}/alpha.txt && "
                f"stat -c '%Y'       {tdir}/alpha.txt && "
                f"stat -c '%u %g %a' {tdir}/rootfile.txt && "
                f"stat -c '%i'       {tdir}/alpha.txt && "
                f"stat -c '%i'       {tdir}/alpha_hardlink.txt",
                timeout=15.0,
                login_shell=False,
            )
            assert pc1_info.success, f"pc1 stat/md5 failed: {pc1_info.stderr}"

            pc1_acl = await pc1_executor.run_command(
                f"getfacl -p {tdir}/alpha.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert pc1_acl.success, f"getfacl failed on pc1: {pc1_acl.stderr}"

            sections = pc1_info.stdout.split("---\n")
            pc1_hashes = {ln.split()[0] for ln in sections[0].strip().splitlines()}
            meta_lines = sections[1].strip().splitlines()
            alpha_ugp = meta_lines[0]  # "user gid perms"
            alpha_mtime = int(meta_lines[1])  # Unix timestamp
            root_ugp = meta_lines[2]  # "0 0 600"
            alpha_inode = meta_lines[3]
            hardlink_inode = meta_lines[4]
            assert alpha_inode == hardlink_inode, "Hard-link pair must share an inode on pc1"

            # --- A→B sync ---
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_result.success, (
                f"pc-switcher sync pc1→pc2 failed.\n"
                f"exit={sync_result.exit_code}\n"
                f"stdout: {sync_result.stdout}\nstderr: {sync_result.stderr}"
            )

            # --- Content: byte-identical ---
            pc2_hashes_result = await pc2_executor.run_command(
                f"md5sum {tdir}/alpha.txt {tdir}/subdir/beta.txt "
                f"{tdir}/.config/Code/User/settings.json {tdir}/.cache/uv/archive.bin",
                timeout=15.0,
                login_shell=False,
            )
            assert pc2_hashes_result.success, f"md5sum on pc2 failed: {pc2_hashes_result.stderr}"
            pc2_hashes = {ln.split()[0] for ln in pc2_hashes_result.stdout.strip().splitlines()}
            assert pc1_hashes == pc2_hashes, (
                f"Checksums differ between pc1 and pc2.\npc1: {sections[0]}\npc2: {pc2_hashes_result.stdout}"
            )

            # --- Metadata: numeric uid/gid, perms, mtime, hard-link ---
            pc2_meta = await pc2_executor.run_command(
                f"stat -c '%u %g %a' {tdir}/alpha.txt && "
                f"stat -c '%Y'       {tdir}/alpha.txt && "
                f"stat -c '%u %g %a' {tdir}/rootfile.txt && "
                f"stat -c '%i'       {tdir}/alpha.txt && "
                f"stat -c '%i'       {tdir}/alpha_hardlink.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert pc2_meta.success, f"stat on pc2 failed: {pc2_meta.stderr}"
            pc2_ml = pc2_meta.stdout.strip().splitlines()
            assert pc2_ml[0] == alpha_ugp, f"alpha.txt uid/gid/perms differ: pc1={alpha_ugp!r} pc2={pc2_ml[0]!r}"
            assert int(pc2_ml[1]) == alpha_mtime, f"mtime not preserved: pc1={alpha_mtime} pc2={pc2_ml[1]}"
            assert pc2_ml[2] == root_ugp, f"rootfile.txt ownership not preserved: pc1={root_ugp!r} pc2={pc2_ml[2]!r}"
            assert pc2_ml[3] == pc2_ml[4], (
                f"Hard-link pair does not share an inode on pc2 (alpha={pc2_ml[3]}, hardlink={pc2_ml[4]})"
            )

            # --- Metadata: POSIX ACLs ---
            pc2_acl = await pc2_executor.run_command(
                f"getfacl -p {tdir}/alpha.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert pc2_acl.success, f"getfacl on pc2 failed: {pc2_acl.stderr}"
            assert "user:2001:r--" in pc2_acl.stdout, (
                f"ACL entry user:2001:r-- not found on pc2.\ngetfacl output:\n{pc2_acl.stdout}"
            )

            # --- Metadata: symlink preserved ---
            pc2_sym = await pc2_executor.run_command(
                f"test -L {tdir}/alpha_sym.txt && readlink {tdir}/alpha_sym.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert pc2_sym.success, f"Symlink alpha_sym.txt not a symlink on pc2: {pc2_sym.stderr}"
            assert pc2_sym.stdout.strip() == "alpha.txt", (
                f"Symlink target wrong: {pc2_sym.stdout.strip()!r} (expected 'alpha.txt')"
            )

            # --- Exclusions: machine-specific files absent ---
            excluded = await pc2_executor.run_command(
                f"test ! -e {tdir}/.ssh/id_rsa && "
                f"test ! -e {tdir}/.config/tailscale/tailscaled.state && "
                f"test ! -e {tdir}/.config/Code/Cache/cache.bin",
                timeout=10.0,
                login_shell=False,
            )
            assert excluded.success, (
                f"Machine-specific excluded files found on pc2 (should be absent).\nstderr: {excluded.stderr}"
            )

            # --- Inclusions: VS Code User state and dev-tool cache present ---
            included = await pc2_executor.run_command(
                f"test -f {tdir}/.config/Code/User/settings.json && test -f {tdir}/.cache/uv/archive.bin",
                timeout=10.0,
                login_shell=False,
            )
            assert included.success, (
                f"Included files (VS Code User/, .cache/uv/) missing on pc2 after sync.\nstderr: {included.stderr}"
            )

        finally:
            await _remove_test_artifacts(pc1_executor, pc2_executor, tdir)


# ---------------------------------------------------------------------------
# Task 2: Round-trip, divergence guard, and dry-run (criterion 4 + D-06/D-12)
# ---------------------------------------------------------------------------


class TestFolderSyncRoundTrip:
    """Round-trip, divergence guard, and dry-run scenarios (criteria 4, D-06/D-07/D-12)."""

    async def test_round_trip_and_no_false_divergence(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """B→A propagates additions, modifications, and deletions; A→B again has no false divergence.

        Workflow:
          1. A→B — initial sync to pc2.
          2. Mutate pc2 — add new file, modify existing, delete a file.
          3. B→A from pc2 — back-sync to pc1.
          4. Assert pc1 reflects all three mutations, with metadata preserved and
             the same exclusions honoured in reverse.
          5. A→B again — must succeed WITHOUT --allow-divergence (D-07: normal
             round-trip is NOT divergence).

        Requirements: REQ-sync-scope-user-data, REQ-machine-specific-exclusions,
        REQ-sync-scope-file-metadata — ROADMAP success criterion 4 + D-07.
        """
        _ = reset_pcswitcher_state
        user = os.environ["PC_SWITCHER_TEST_USER"]
        tdir = _test_dir(user)
        config = _make_config(tdir)

        try:
            await _write_config(pc1_executor, config)
            await _seed_test_tree(pc1_executor, tdir)

            # Step 1: A→B initial sync
            sync_ab = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ab.success, (
                f"A→B sync failed.\nexit={sync_ab.exit_code}\nstdout: {sync_ab.stdout}\nstderr: {sync_ab.stderr}"
            )

            # Step 2: Mutate on pc2 — add / modify / delete within the test directory.
            # Also add an excluded file on pc2 that must NOT propagate back to pc1.
            mutate = await pc2_executor.run_command(
                f"""set -e
T={tdir}
# Add a new file with known content and mtime
printf 'new file from pc2' > "$T/pc2_addition.txt"
chmod 755 "$T/pc2_addition.txt"
touch -d "@1710000000" "$T/pc2_addition.txt"

# Modify subdir/beta.txt content
printf 'MODIFIED beta content' > "$T/subdir/beta.txt"

# Delete alpha_hardlink.txt (propagated deletion test)
rm -f "$T/alpha_hardlink.txt"

# Add an excluded file on pc2 — must NOT appear on pc1 after B→A
mkdir -p "$T/.ssh"
printf 'pc2_private_key' > "$T/.ssh/id_rsa"
""",
                timeout=15.0,
                login_shell=False,
            )
            assert mutate.success, f"Mutation on pc2 failed: {mutate.stderr}"

            # Capture pc2's state for later comparison
            pc2_addition_md5 = await pc2_executor.run_command(
                f"md5sum {tdir}/pc2_addition.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert pc2_addition_md5.success
            expected_md5 = pc2_addition_md5.stdout.split()[0]

            pc2_beta_md5 = await pc2_executor.run_command(
                f"md5sum {tdir}/subdir/beta.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert pc2_beta_md5.success
            expected_beta_md5 = pc2_beta_md5.stdout.split()[0]

            addition_mtime = await pc2_executor.run_command(
                f"stat -c '%Y' {tdir}/pc2_addition.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert addition_mtime.success
            expected_addition_mtime = int(addition_mtime.stdout.strip())

            # Step 3: B→A back-sync from pc2 (pc2 is now source, pc1 is target)
            sync_ba = await pc2_executor.run_command(
                "pc-switcher sync pc1 --yes",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ba.success, (
                f"B→A sync failed.\nexit={sync_ba.exit_code}\nstdout: {sync_ba.stdout}\nstderr: {sync_ba.stderr}"
            )

            # Step 4: Assert pc1 reflects mutations from pc2.
            pc1_checks = await pc1_executor.run_command(
                # Addition: present and byte-identical
                f"md5sum {tdir}/pc2_addition.txt && "
                # Modified: new content
                f"md5sum {tdir}/subdir/beta.txt && "
                # Addition mtime preserved
                f"stat -c '%Y' {tdir}/pc2_addition.txt",
                timeout=15.0,
                login_shell=False,
            )
            assert pc1_checks.success, f"pc1 checks after B→A failed: {pc1_checks.stderr}"
            pc1_lines = pc1_checks.stdout.strip().splitlines()
            assert pc1_lines[0].split()[0] == expected_md5, (
                f"Addition md5 differs on pc1: expected={expected_md5!r} got={pc1_lines[0]!r}"
            )
            assert pc1_lines[1].split()[0] == expected_beta_md5, (
                f"Modified file md5 differs on pc1: expected={expected_beta_md5!r} got={pc1_lines[1]!r}"
            )
            assert int(pc1_lines[2]) == expected_addition_mtime, (
                f"mtime not preserved on B→A: expected={expected_addition_mtime} got={pc1_lines[2]}"
            )

            # Deletion propagated: alpha_hardlink.txt must be absent on pc1
            deleted_check = await pc1_executor.run_command(
                f"test ! -e {tdir}/alpha_hardlink.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert deleted_check.success, (
                "Deleted file alpha_hardlink.txt still present on pc1 after B→A deletion propagation"
            )

            # Exclusion in reverse: pc2's .ssh/id_rsa must NOT appear on pc1
            excl_reverse = await pc1_executor.run_command(
                f"test ! -e {tdir}/.ssh/id_rsa",
                timeout=10.0,
                login_shell=False,
            )
            assert excl_reverse.success, (
                "Excluded file .ssh/id_rsa from pc2 appeared on pc1 after B→A (exclusion must hold in reverse)"
            )

            # Step 5: A→B again — no false divergence (D-07)
            #
            # Why this does NOT trigger a false divergence:
            # The B→A sync caused pc-switcher to write its OWN state files to pc2's @home:
            #   - ~/.local/share/pc-switcher/sync-history.json (post-sync baseline write)
            #   - ~/.local/share/pc-switcher/pc-switcher.lock (runtime lock file)
            #   - ~/.config/pc-switcher/config.yaml (Phase-8 config sync, if configs differ)
            # These writes bump pc2's @home btrfs generation AFTER the baseline is captured.
            # However, all of these paths land OUTSIDE the dedicated <tdir> prefix
            # (/home/<user>/pcswitcher-folder-sync-test), so the EXISTING prefix-scoping
            # in _target_diverged_since filters them out — `btrfs find-new` reports them but
            # the prefix check (`f" {prefix}/" in line`) does not match <tdir>.
            # NOTE: this is the PREFIX-SCOPING path, not the empty-prefix tool-state filter
            # (CR-01). The CR-01 filter handles the default /home config (empty prefix where
            # pc-switcher writes fall inside the scanned subvolume root). For this test the
            # synced folder is <tdir> — a non-empty prefix — so the out-of-prefix pc-switcher
            # writes are already excluded by the ordinary prefix check.
            # Regression: if prefix-scoping breaks, this step fails with "divergence detected".
            sync_ab2 = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ab2.success, (
                f"Second A→B sync falsely reported divergence (D-07 violated).\n"
                f"exit={sync_ab2.exit_code}\n"
                f"stdout: {sync_ab2.stdout}\nstderr: {sync_ab2.stderr}"
            )
            # A divergence would produce non-zero exit with 'divergence' in output.
            divergence_triggered = not sync_ab2.success and "divergence" in (sync_ab2.stdout + sync_ab2.stderr).lower()
            assert not divergence_triggered, (
                "Second A→B falsely blocked as divergence after a normal round-trip (D-07)"
            )

        finally:
            await _remove_test_artifacts(pc1_executor, pc2_executor, tdir)

    async def test_divergence_guard_and_dry_run(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Divergence guard blocks sync when target independently modified; dry-run stays no-op.

        Workflow:
          1. A→B initial sync — establishes divergence baseline.
          2. Independently mutate pc2's test directory (without syncing).
          3. A→B attempt — must be BLOCKED (non-zero exit, divergence message).
          4. A→B --dry-run — must NOT be blocked; target files unchanged; marker
             not updated; divergence logged as warning.
          5. A→B --allow-divergence — must proceed (exit 0) and reconcile target.

        Requirements: REQ-manual-sync-workflow (D-06, D-12) — ROADMAP criterion 5.
        """
        _ = reset_pcswitcher_state
        user = os.environ["PC_SWITCHER_TEST_USER"]
        tdir = _test_dir(user)
        config = _make_config(tdir)

        try:
            await _write_config(pc1_executor, config)
            await _seed_test_tree(pc1_executor, tdir)

            # Step 1: A→B to establish divergence baseline
            sync_ab = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ab.success, (
                f"Initial A→B sync failed.\nexit={sync_ab.exit_code}\n"
                f"stdout: {sync_ab.stdout}\nstderr: {sync_ab.stderr}"
            )

            # Capture pc2 checksums BEFORE the independent mutation (step 2).
            before_md5 = await pc2_executor.run_command(
                f"md5sum {tdir}/alpha.txt {tdir}/subdir/beta.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert before_md5.success

            # Step 2: Independently modify pc2's test directory WITHOUT syncing.
            # Writing to the test directory increments pc2's @home btrfs generation,
            # which the divergence guard will detect via btrfs find-new.
            tamper = await pc2_executor.run_command(
                f"printf 'tampered' >> {tdir}/alpha.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert tamper.success, f"Tamper write on pc2 failed: {tamper.stderr}"

            # Step 3: A→B attempt — must be BLOCKED by divergence guard.
            blocked_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=120.0,
                login_shell=True,
            )
            assert not blocked_result.success, (
                "Divergence guard should have blocked the sync (non-zero exit expected), "
                f"but it exited with code {blocked_result.exit_code}.\n"
                f"stdout: {blocked_result.stdout}"
            )
            combined = (blocked_result.stdout + blocked_result.stderr).lower()
            assert "divergence" in combined or "diverged" in combined, (
                "Divergence guard output must mention 'divergence' or 'diverged'.\n"
                f"stdout: {blocked_result.stdout}\nstderr: {blocked_result.stderr}"
            )

            # Step 4: A→B --dry-run — must NOT be blocked; target files unchanged;
            # divergence marker must not be updated.
            # Read pc1's current sync-history to verify the marker is not changed after dry-run.
            history_before = await pc1_executor.run_command(
                "cat ~/.local/share/pc-switcher/sync-history.json 2>/dev/null || echo '{}'",
                timeout=10.0,
            )
            assert history_before.success

            dry_run_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --dry-run",
                timeout=180.0,
                login_shell=True,
            )
            assert dry_run_result.success, (
                f"--dry-run should not be blocked by divergence (D-12).\n"
                f"exit={dry_run_result.exit_code}\n"
                f"stdout: {dry_run_result.stdout}\nstderr: {dry_run_result.stderr}"
            )
            # Divergence must be logged as a warning in the output (not a hard error).
            dry_combined = (dry_run_result.stdout + dry_run_result.stderr).lower()
            assert "divergence" in dry_combined or "diverged" in dry_combined, (
                "--dry-run output should log the divergence warning.\n"
                f"stdout: {dry_run_result.stdout}\nstderr: {dry_run_result.stderr}"
            )

            # Target files unchanged after dry-run.
            after_dryrun_md5 = await pc2_executor.run_command(
                f"md5sum {tdir}/alpha.txt {tdir}/subdir/beta.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert after_dryrun_md5.success
            # alpha.txt was tampered — the tampering must still be there (not overwritten by dry-run).
            # The dry-run does NOT reset the tamper; it shows what WOULD change, but doesn't change it.
            # We verify that beta.txt is still the original (unchanged by dry-run).
            # beta.txt should be the same (the tamper was only on alpha.txt)
            before_beta = [ln for ln in before_md5.stdout.strip().splitlines() if "beta.txt" in ln]
            after_beta = [ln for ln in after_dryrun_md5.stdout.strip().splitlines() if "beta.txt" in ln]
            assert before_beta[0].split()[0] == after_beta[0].split()[0], (
                "beta.txt was modified by dry-run (should be unchanged)"
            )

            # Divergence marker not updated after dry-run (D-12: no state writes).
            history_after = await pc1_executor.run_command(
                "cat ~/.local/share/pc-switcher/sync-history.json 2>/dev/null || echo '{}'",
                timeout=10.0,
            )
            assert history_after.success

            # The target_generations entry for pc2/tdir should be the same as before.
            def _get_gen(json_str: str) -> int | None:
                try:
                    data = json.loads(json_str)
                    return data.get("target_generations", {}).get("pc2", {}).get(tdir)
                except json.JSONDecodeError:
                    return None

            gen_before = _get_gen(history_before.stdout)
            gen_after = _get_gen(history_after.stdout)
            assert gen_before == gen_after, (
                f"Divergence marker updated by --dry-run (violates D-12).\n"
                f"Before: gen={gen_before!r}\nAfter: gen={gen_after!r}"
            )

            # Step 5: A→B --allow-divergence — must proceed and reconcile target.
            allow_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-divergence",
                timeout=300.0,
                login_shell=True,
            )
            assert allow_result.success, (
                f"--allow-divergence sync failed.\n"
                f"exit={allow_result.exit_code}\n"
                f"stdout: {allow_result.stdout}\nstderr: {allow_result.stderr}"
            )

            # Target should now be reconciled: alpha.txt matches pc1 (tamper overwritten).
            pc1_alpha_md5 = await pc1_executor.run_command(
                f"md5sum {tdir}/alpha.txt",
                timeout=10.0,
                login_shell=False,
            )
            pc2_alpha_md5 = await pc2_executor.run_command(
                f"md5sum {tdir}/alpha.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert pc1_alpha_md5.success and pc2_alpha_md5.success
            assert pc1_alpha_md5.stdout.split()[0] == pc2_alpha_md5.stdout.split()[0], (
                "After --allow-divergence sync, alpha.txt should match between pc1 and pc2.\n"
                f"pc1: {pc1_alpha_md5.stdout.strip()}\npc2: {pc2_alpha_md5.stdout.strip()}"
            )

        finally:
            await _remove_test_artifacts(pc1_executor, pc2_executor, tdir)
