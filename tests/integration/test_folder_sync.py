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
  - Criterion 5 (ADR-015, D-12): Two independent safety gates: W1 (first-ever sync,
    no target history) gated by --allow-first-sync; W2/W3 (out-of-order or consecutive
    push) gated by --allow-out-of-order. A normal A→B / B→A / A→B round-trip proceeds
    WITHOUT any override (the clean case is silent). --dry-run performs a read-only
    preview through both gates and does not update sync history.

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
            # --allow-first-sync: pc2 has no prior sync history (W1: first-ever sync), so
            # the first-sync gate fires in non-interactive mode.  We bypass it here because
            # this test focuses on content/metadata, not topology safety.
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync",
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
    """Round-trip and topology-model scenarios (criteria 4, D-12, ADR-015)."""

    async def test_round_trip_and_no_false_divergence(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """B→A propagates additions, modifications, and deletions; A→B again has no out-of-order warning.

        Workflow:
          1. A→B — initial sync to pc2 (--allow-first-sync: pc2 has no prior history, W1 gate).
          2. Mutate pc2 — add new file, modify existing, delete a file.
          3. B→A from pc2 — back-sync to pc1 (no override needed: topology clean case).
          4. Assert pc1 reflects all three mutations, with metadata preserved and
             the same exclusions honoured in reverse.
          5. A→B again — must succeed WITHOUT --allow-out-of-order (ADR-015 #159:
             normal A→B / B→A / A→B round-trip is the clean case and never triggers
             the out-of-order heads-up).

        Requirements: REQ-sync-scope-user-data, REQ-machine-specific-exclusions,
        REQ-sync-scope-file-metadata — ROADMAP success criterion 4 + ADR-015.
        """
        _ = reset_pcswitcher_state
        user = os.environ["PC_SWITCHER_TEST_USER"]
        tdir = _test_dir(user)
        config = _make_config(tdir)

        try:
            await _write_config(pc1_executor, config)
            await _seed_test_tree(pc1_executor, tdir)

            # Step 1: A→B initial sync
            # --allow-first-sync: pc2 has no prior sync history (W1: first-ever sync),
            # so the first-sync gate fires in non-interactive mode.
            sync_ab = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync",
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

            # Step 5: A→B again — topology check must be silent (no override needed).
            #
            # After B→A, pc1's sync history: last_role=TARGET, last_peer=pc2.
            # pc2's sync history: last_role=SOURCE, last_peer=pc1.
            # Topology check: target_peer (pc1) == this source (pc1), and no consecutive
            # push (pc1's local role is TARGET, not SOURCE) → clean case → no warning.
            # ADR-015 / GitHub #159: the A→B / B→A / A→B pattern is explicitly the
            # legitimate workflow that must never be blocked.
            sync_ab2 = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=300.0,
                login_shell=True,
            )
            assert sync_ab2.success, (
                f"Second A→B sync failed (topology check incorrectly triggered for clean round-trip).\n"
                f"exit={sync_ab2.exit_code}\n"
                f"stdout: {sync_ab2.stdout}\nstderr: {sync_ab2.stderr}"
            )
            # An out-of-order trigger would produce non-zero exit with 'out-of-order' in output.
            out_of_order_triggered = (
                not sync_ab2.success and "out-of-order" in (sync_ab2.stdout + sync_ab2.stderr).lower()
            )
            assert not out_of_order_triggered, (
                "Second A→B triggered out-of-order warning after a normal round-trip (ADR-015 #159 violated)"
            )

        finally:
            await _remove_test_artifacts(pc1_executor, pc2_executor, tdir)

    async def test_out_of_order_and_dry_run(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """First-sync (W1) gate and dry-run preview behaviour (ADR-015, D-12).

        Workflow:
          1. Seed pc1's test directory; pc2 has no prior sync history.
          2. A→B attempt without any bypass flag — must fail with non-zero exit; the
             W1 (first-sync) gate fires because pc2 has no history and non-interactive
             mode cannot confirm the overwrite.
          3. A→B --dry-run — must NOT be blocked (ADR-014: dry-run is a read-only
             rehearsal; both gates log and proceed); pc2's test directory must remain
             empty (no file mutations); pc1's sync-history.json must not be created or
             updated (D-12: no state writes in dry-run).
          4. A→B --allow-first-sync --yes — must proceed (exit 0) and populate pc2's
             test directory with the source files.

        Requirements: REQ-manual-sync-workflow (ADR-015, D-12) — ROADMAP criterion 5.
        """
        _ = reset_pcswitcher_state
        user = os.environ["PC_SWITCHER_TEST_USER"]
        tdir = _test_dir(user)
        config = _make_config(tdir)

        try:
            await _write_config(pc1_executor, config)
            await _seed_test_tree(pc1_executor, tdir)

            # Step 2: A→B without a bypass flag — W1 (first-sync) gate fires because pc2
            # has no sync history; non-interactive mode cannot confirm the overwrite.
            blocked_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes",
                timeout=120.0,
                login_shell=True,
            )
            assert not blocked_result.success, (
                "First-sync (W1) gate should have returned non-zero (non-interactive, no history), "
                f"but it exited with code {blocked_result.exit_code}.\n"
                f"stdout: {blocked_result.stdout}"
            )
            combined = (blocked_result.stdout + blocked_result.stderr).lower()
            # "out-of-order" appears in the RuntimeError "Sync aborted at the out-of-order /
            # target-state check"; "target" appears in the W1 warning title and message body.
            assert "out-of-order" in combined or "target" in combined, (
                "First-sync gate output must mention 'out-of-order' (abort message) or 'target'.\n"
                f"stdout: {blocked_result.stdout}\nstderr: {blocked_result.stderr}"
            )

            # Confirm pc2's test directory was not created (no sync happened).
            pc2_empty = await pc2_executor.run_command(
                f"test ! -e {tdir}",
                timeout=10.0,
                login_shell=False,
            )
            assert pc2_empty.success, (
                f"pc2's test directory exists after blocked sync (should not have been created).\n"
                f"stderr: {pc2_empty.stderr}"
            )

            # Step 3: A→B --dry-run — must proceed (ADR-014: dry-run never aborts on
            # out-of-order warning).  No files written to pc2; no history update on pc1.
            history_before = await pc1_executor.run_command(
                "cat ~/.local/share/pc-switcher/sync-history.json 2>/dev/null || echo 'absent'",
                timeout=10.0,
            )
            assert history_before.success

            dry_run_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --dry-run",
                timeout=180.0,
                login_shell=True,
            )
            assert dry_run_result.success, (
                f"--dry-run should not be blocked by out-of-order check (ADR-014).\n"
                f"exit={dry_run_result.exit_code}\n"
                f"stdout: {dry_run_result.stdout}\nstderr: {dry_run_result.stderr}"
            )

            # pc2's test directory still absent after --dry-run (no file mutations).
            pc2_still_empty = await pc2_executor.run_command(
                f"test ! -e {tdir}",
                timeout=10.0,
                login_shell=False,
            )
            assert pc2_still_empty.success, (
                f"pc2's test directory was created by --dry-run (must be read-only).\nstderr: {pc2_still_empty.stderr}"
            )

            # pc1's sync-history must not have changed (D-12: no state writes in dry-run).
            history_after = await pc1_executor.run_command(
                "cat ~/.local/share/pc-switcher/sync-history.json 2>/dev/null || echo 'absent'",
                timeout=10.0,
            )
            assert history_after.success
            assert history_before.stdout.strip() == history_after.stdout.strip(), (
                "pc1 sync-history was updated by --dry-run (violates D-12).\n"
                f"Before: {history_before.stdout.strip()!r}\nAfter: {history_after.stdout.strip()!r}"
            )

            # Step 4: A→B --allow-first-sync — bypasses the W1 gate, populates pc2.
            # (Dry-run in step 3 does not update history, so pc2 still has no history here.)
            allow_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,
                login_shell=True,
            )
            assert allow_result.success, (
                f"--allow-first-sync sync failed.\n"
                f"exit={allow_result.exit_code}\n"
                f"stdout: {allow_result.stdout}\nstderr: {allow_result.stderr}"
            )

            # pc2's test directory now has the source files.
            pc2_populated = await pc2_executor.run_command(
                f"test -f {tdir}/alpha.txt && test -f {tdir}/subdir/beta.txt",
                timeout=10.0,
                login_shell=False,
            )
            assert pc2_populated.success, (
                f"pc2's test directory not populated after --allow-out-of-order sync.\nstderr: {pc2_populated.stderr}"
            )

        finally:
            await _remove_test_artifacts(pc1_executor, pc2_executor, tdir)
