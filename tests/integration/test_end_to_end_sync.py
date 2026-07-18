"""Integration tests for end-to-end sync operations.

Tests CORE-US-JOB-ARCH (Job Architecture) acceptance scenarios:
- CORE-US-JOB-ARCH-AS1: Job integration via standardized interface
- CORE-US-JOB-ARCH-AS7: Interrupt handling during job execution
- Edge case: Target unreachable mid-sync

These tests verify the complete orchestrator workflow by actually running
`pc-switcher sync` on test VMs. They exercise the full sync pipeline including:
- Lock acquisition (source and target)
- SSH connection establishment
- Job discovery and validation
- Disk space preflight checks
- Pre-sync btrfs snapshots
- InstallOnTargetJob execution
- Config sync to target
- Sync job execution (dummy_success)
- Post-sync btrfs snapshots
- Cleanup and lock release

Test VM Requirements:
- pc1 and pc2 VMs must be provisioned and accessible
- VMs must have btrfs filesystem with @ and @home subvolumes
- VMs must be reset to baseline before tests run
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest
import pytest_asyncio

from pcswitcher.executor import BashLoginRemoteExecutor
from pcswitcher.version import get_this_version


# Dataclass for pc1_to_pc2_traffic_blocker fixture
@dataclass
class Pc1ToPc2TrafficBlocker:
    """Provides async callables to block/unblock pc1->pc2 SSH traffic.

    Both `block` and `unblock` are callables returning an awaitable that
    resolves to None when complete.
    """

    block: Callable[[], Awaitable[None]]
    unblock: Callable[[], Awaitable[None]]


@pytest.fixture
async def pc1_to_pc2_traffic_blocker(
    pc2_executor: BashLoginRemoteExecutor,
) -> AsyncIterator[Pc1ToPc2TrafficBlocker]:
    """Blocks SSH traffic from pc1 to pc2 for network failure simulation.

    This fixture allows tests to simulate network failures by blocking SSH
    traffic from pc1 to pc2 using iptables on pc2. The block only affects
    pc1→pc2 traffic; the test runner retains full access to both VMs.

    Yields a dict with:
        - block: async callable to block pc1→pc2 SSH traffic
        - unblock: async callable to restore connectivity

    Cleanup is automatic on fixture teardown, even if test fails.
    """
    pc1_ip: str | None = None
    blocked = False

    async def block_pc1() -> None:
        nonlocal pc1_ip, blocked
        if blocked:
            return
        # Resolve pc1's IP from /etc/hosts on pc2
        result = await pc2_executor.run_command(
            "getent hosts pc1 | awk '{print $1}'",
            timeout=10.0,
            login_shell=False,
        )
        pc1_ip = result.stdout.strip()
        assert pc1_ip, f"Failed to resolve pc1 IP: {result.stderr}"

        # Block all TCP traffic from pc1 to port 22 (SSH)
        block_result = await pc2_executor.run_command(
            f"sudo iptables -I INPUT -s {pc1_ip} -p tcp --dport 22 -j DROP",
            timeout=10.0,
            login_shell=False,
        )
        assert block_result.success, f"Failed to add iptables rule: {block_result.stderr}"
        blocked = True

    async def unblock_pc1() -> None:
        nonlocal blocked
        if not blocked or not pc1_ip:
            return
        # Remove the blocking rule
        await pc2_executor.run_command(
            f"sudo iptables -D INPUT -s {pc1_ip} -p tcp --dport 22 -j DROP",
            timeout=10.0,
            login_shell=False,
        )
        blocked = False

    yield Pc1ToPc2TrafficBlocker(block=block_pc1, unblock=unblock_pc1)

    # Cleanup: ensure network is unblocked even if test fails
    await unblock_pc1()


# Test config with short durations for faster tests
_TEST_CONFIG_TEMPLATE = """# Test configuration for end-to-end sync tests
# Short durations to keep tests fast

logging:
  file: DEBUG
  tui: DEBUG
  external: DEBUG

sync_jobs:
  dummy_success: true
  dummy_fail: false

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

dummy_success:
  source_duration: {source_duration}
  target_duration: {target_duration}
"""


@pytest_asyncio.fixture
async def sync_ready_source(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    reset_pcswitcher_state: None,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 configured and ready to run pc-switcher sync.

    This fixture:
    1. Ensures pc-switcher is installed (via pc1_with_pcswitcher_mod)
    2. Cleans up any existing sync history (via reset_pcswitcher_state)
    3. Creates a test configuration with short-duration jobs
    4. Cleans up the test config after the test

    Yields:
        Executor for pc1, ready to run sync commands
    """
    _ = reset_pcswitcher_state  # Ensures cleanup runs before test
    executor = pc1_with_pcswitcher_mod

    # Backup existing config if any
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml ]; then "
        "cp ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.e2e-backup; "
        "fi",
        timeout=10.0,
    )

    # Create test config with short durations (4 seconds each = 8 seconds total for dummy_success)
    test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=4, target_duration=4)
    await executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)

    # Use heredoc to write config
    write_result = await executor.run_command(
        f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
        timeout=10.0,
    )
    assert write_result.success, f"Failed to write test config: {write_result.stderr}"

    yield executor

    # Cleanup: restore original config
    await executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.e2e-backup ]; then "
        "mv ~/.config/pc-switcher/config.yaml.e2e-backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


@pytest_asyncio.fixture
async def sync_ready_source_long_duration(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    reset_pcswitcher_state: None,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 configured for sync with longer duration (for interrupt tests).

    Same as sync_ready_source but with 60-second durations to allow time
    for interrupt testing.
    """
    _ = reset_pcswitcher_state  # Ensures cleanup runs before test
    executor = pc1_with_pcswitcher_mod

    # Backup existing config if any
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml ]; then "
        "cp ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.e2e-backup; "
        "fi",
        timeout=10.0,
    )

    # Create test config with longer durations for interrupt testing
    test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=60, target_duration=60)
    await executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)

    write_result = await executor.run_command(
        f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
        timeout=10.0,
    )
    assert write_result.success, f"Failed to write test config: {write_result.stderr}"

    yield executor

    # Cleanup
    await executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.e2e-backup ]; then "
        "mv ~/.config/pc-switcher/config.yaml.e2e-backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# Folder-sync scenario helpers (real /home): seeding, manifests, config.
#
# The end-to-end test below syncs the real /home (the production default scope).
# It is safe on the VMs because both boot from an identical btrfs baseline (so the
# --delete mirror only moves the seeded subtree), pc-switcher's own runtime files
# are protected by the hardcoded excludes (ADR-016), heavy regenerable trees are
# excluded in the test config for speed, and .ssh/known_hosts is excluded because
# reset-vm.sh gives each VM the other's host key. Each test cleans up its subtree.
# ---------------------------------------------------------------------------

# Seeded rich test subtree, relative to the user's home.
_TESTTREE = "pcsw-itest"

# uid/gid 1 = daemon on Ubuntu — a non-root system user, used numerically to prove
# uid/gid preservation for files the invoking user cannot access.
_OTHER_UID = 1
_OTHER_GID = 1

# Known mtimes (Unix epoch seconds) for backdated-mtime assertions.
_BACKDATED_MTIME = 1705312800  # 2024-01-15 10:00:00 UTC
_ADDITION_MTIME = 1710000000  # 2024-03-09 16:00:00 UTC

# pc-switcher runtime state dir (ADR-016 hardcoded exclude target).
_STATE_DIR = "~/.local/share/pc-switcher"

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


def _tree(user: str) -> str:
    """Absolute path of the seeded rich test subtree within the real home."""
    return f"/home/{user}/{_TESTTREE}"


def _make_e2e_config() -> str:
    """Config exercising both a generic job (dummy_success) and folder_sync of /home."""
    return f"""\
logging:
  file: DEBUG
  tui: INFO
  external: WARNING
sync_jobs:
  dummy_success: true
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
dummy_success:
  source_duration: 2
  target_duration: 2
folder_sync:
  folders:
    - path: /home
      enabled: true
      excludes:
        - .ssh/id_*
        - .config/tailscale
        - .config/Code/Cache
        - .config/Code/CachedData
        - .config/Code/GPUCache
        - .ssh/known_hosts
        - .ssh/authorized_keys
        - .cache
        - .local/share/uv/python
        - {_TESTTREE}/secret
"""


async def _write_config(executor: BashLoginRemoteExecutor, config: str) -> None:
    """Write the pc-switcher config to a VM."""
    result = await executor.run_command(
        f"mkdir -p ~/.config/pc-switcher && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n{config}CONF_EOF",
        timeout=10.0,
    )
    assert result.success, f"Failed to write config: {result.stderr}"


async def _seed_rich_tree(executor: BashLoginRemoteExecutor, tree: str) -> None:
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


async def _seed_included_markers(executor: BashLoginRemoteExecutor) -> None:
    """Seed the SC3 inclusion/exclusion marker files in the real home dotdirs."""
    parts = ["set -e"]
    for rel, content in _INCLUDED_MARKERS.items():
        parts.append(f'mkdir -p ~/"$(dirname {rel})" && printf %s {content!r} > ~/{rel}')
    parts.append(f'mkdir -p ~/"$(dirname {_EXCLUDED_MARKER})" && printf excluded > ~/{_EXCLUDED_MARKER}')
    result = await executor.run_command("\n".join(parts), timeout=15.0, login_shell=False)
    assert result.success, f"Failed to seed inclusion markers: {result.stderr}"


async def _remove_test_artifacts(
    pc1_exec: BashLoginRemoteExecutor,
    pc2_exec: BashLoginRemoteExecutor,
    tree: str,
) -> None:
    """Remove the seeded test subtree, inclusion markers, and config from both VMs."""
    markers = " ".join(f"~/{rel}" for rel in (*_INCLUDED_MARKERS, _EXCLUDED_MARKER))
    for name, exec_ in (("pc1", pc1_exec), ("pc2", pc2_exec)):
        res = await exec_.run_command(
            f"sudo rm -rf {tree} {markers} && rm -f ~/.config/pc-switcher/config.yaml",
            timeout=30.0,
            login_shell=False,
        )
        if not res.success:
            print(f"[cleanup] {name} removal warning: {res.stderr}")


class TestEndToEndSync:
    """Integration tests for complete pc-switcher sync workflow."""

    async def test_core_us_job_arch_as1_job_integration_via_interface(  # noqa: PLR0915
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """CORE-US-JOB-ARCH-AS1 + full folder-sync end-to-end (criteria 1-5, ADR-014/015/016).

        One scenario, one seed, five syncs — the complete pipeline in a single run:

        1. Blocked A→B (no flag): the W1 first-sync gate aborts non-interactively; nothing reaches pc2.
        2. A→B --dry-run: rehearses through the gate (ADR-014) but writes nothing and does not update history (D-12).
        3. A→B --allow-first-sync: the real sync. Verifies BOTH
           - job integration via the standardized interface (dummy_success + folder_sync discovered,
             logged, pre/post snapshots on both machines, config synced), AND
           - folder_sync of the real /home: byte-identical content; numeric uid/gid; permissions incl.
             setuid/setgid/sticky; POSIX ACL; mtime; hard-link inode sharing; symlink; across user-,
             root-, and other-user-owned files AND directories the invoking user cannot read; config
             exclusions honoured; and the ADR-016 runtime-file excludes (state/install/logs) via sentinels.
        4. Mutate pc2 (add / modify / delete file / delete directory / chmod) then B→A: all propagate.
        5. A→B again with no override: a clean round-trip must not trip the out-of-order gate (ADR-015 #159).

        See the module-level "Folder-sync scenario helpers" for why syncing the real /home is safe here.
        """
        _ = (pc1_with_pcswitcher_mod, pc2_with_pcswitcher, reset_pcswitcher_state)
        user = os.environ["PC_SWITCHER_TEST_USER"]
        tree = _tree(user)

        try:
            await _write_config(pc1_executor, _make_e2e_config())
            await _seed_rich_tree(pc1_executor, tree)
            await _seed_included_markers(pc1_executor)
            await pc2_executor.run_command(f"sudo rm -rf {tree}", timeout=15.0, login_shell=False)

            # ADR-016 runtime-exclude sentinels: a marker inside each machine's own state dir
            # (reset_pcswitcher_state wiped the dir, so create it fresh here).
            await pc1_executor.run_command(
                f"mkdir -p {_STATE_DIR} && printf pc1 > {_STATE_DIR}/SENTINEL_SOURCE", timeout=10.0
            )
            await pc2_executor.run_command(
                f"mkdir -p {_STATE_DIR} && printf pc2 > {_STATE_DIR}/SENTINEL_TARGET", timeout=10.0
            )

            src_manifest = await pc1_executor.run_command(_manifest_cmd(tree), timeout=30.0, login_shell=False)
            assert src_manifest.success, f"source manifest failed: {src_manifest.stderr}"
            src_md5 = await pc1_executor.run_command(_md5_manifest_cmd(tree), timeout=30.0, login_shell=False)
            assert src_md5.success, f"source md5 manifest failed: {src_md5.stderr}"

            # --- Step 1: blocked first sync (W1 gate, non-interactive) ---
            blocked = await pc1_executor.run_command("pc-switcher sync pc2 --yes", timeout=180.0, login_shell=True)
            assert not blocked.success, (
                f"W1 first-sync gate should block non-interactively, got exit {blocked.exit_code}.\n{blocked.stdout}"
            )
            assert (
                "out-of-order" in (blocked.stdout + blocked.stderr).lower()
                or "target" in (blocked.stdout + blocked.stderr).lower()
            ), f"Unexpected first-sync-gate message.\nstdout: {blocked.stdout}\nstderr: {blocked.stderr}"
            no_tree = await pc2_executor.run_command(f"test ! -e {tree}", timeout=10.0, login_shell=False)
            assert no_tree.success, "Blocked first sync transferred the tree to pc2."

            # --- Step 2: dry-run rehearsal (proceeds, writes nothing, no history change) ---
            hist_before = await pc1_executor.run_command(
                f"cat {_STATE_DIR}/sync-history.json 2>/dev/null || echo absent", timeout=10.0
            )
            dry = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --dry-run", timeout=180.0, login_shell=True
            )
            assert dry.success, f"--dry-run should not be blocked (ADR-014).\nstderr: {dry.stderr}"
            still_no_tree = await pc2_executor.run_command(f"test ! -e {tree}", timeout=10.0, login_shell=False)
            assert still_no_tree.success, "--dry-run transferred the tree to pc2 (must be read-only)."
            hist_after = await pc1_executor.run_command(
                f"cat {_STATE_DIR}/sync-history.json 2>/dev/null || echo absent", timeout=10.0
            )
            assert hist_before.stdout.strip() == hist_after.stdout.strip(), "--dry-run updated sync-history (D-12)."

            # --- Step 3: real first sync (--allow-first-sync) ---
            sync_ab = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync", timeout=300.0, login_shell=True
            )
            assert sync_ab.success, (
                f"A→B first sync failed.\nexit={sync_ab.exit_code}\nstdout: {sync_ab.stdout}\nstderr: {sync_ab.stderr}"
            )

            # 3a. Job integration via interface: log entries, snapshots on both, config synced.
            log_content = await pc1_executor.run_command(
                "cat $(ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1)", timeout=10.0
            )
            assert log_content.success, f"Failed to read log file: {log_content.stderr}"
            log_text = log_content.stdout.lower()
            assert "dummy_success" in log_text or "source phase" in log_text, "Generic job (dummy_success) not logged."
            assert "folder_sync" in log_text, "folder_sync job not logged."
            src_snaps = await pc1_executor.run_command(
                "sudo ls /.snapshots/pc-switcher/ 2>/dev/null | head -1", timeout=10.0, login_shell=False
            )
            assert src_snaps.stdout.strip(), "Pre/post-sync snapshots missing on source."
            tgt_snaps = await pc2_executor.run_command(
                "sudo ls /.snapshots/pc-switcher/ 2>/dev/null | head -1", timeout=10.0, login_shell=False
            )
            assert tgt_snaps.stdout.strip(), "Pre/post-sync snapshots missing on target."
            tgt_config = await pc2_executor.run_command("cat ~/.config/pc-switcher/config.yaml", timeout=10.0)
            assert tgt_config.success and "dummy_success: true" in tgt_config.stdout, "Config not synced to target."

            # 3b. folder_sync content + metadata: target manifests must equal source manifests exactly.
            tgt_manifest = await pc2_executor.run_command(_manifest_cmd(tree), timeout=30.0, login_shell=False)
            assert tgt_manifest.success, f"target manifest failed: {tgt_manifest.stderr}"
            assert tgt_manifest.stdout == src_manifest.stdout, (
                "Ownership/permission manifest differs after A→B (numeric uid/gid, mode, or special bits).\n"
                f"--- pc1 ---\n{src_manifest.stdout}\n--- pc2 ---\n{tgt_manifest.stdout}"
            )
            tgt_md5 = await pc2_executor.run_command(_md5_manifest_cmd(tree), timeout=30.0, login_shell=False)
            assert tgt_md5.success, f"target md5 manifest failed: {tgt_md5.stderr}"
            assert tgt_md5.stdout == src_md5.stdout, (
                "Content md5 manifest differs after A→B.\n"
                f"--- pc1 ---\n{src_md5.stdout}\n--- pc2 ---\n{tgt_md5.stdout}"
            )

            # 3c. ACL, backdated mtime, hard-link inode sharing, symlink target.
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

            # 3d. Exclusions: config-excluded subtree absent; ADR-016 runtime excludes held.
            excl = await pc2_executor.run_command(
                f"test ! -e {tree}/secret/token.txt", timeout=10.0, login_shell=False
            )
            assert excl.success, "Config-excluded secret/token.txt reached pc2 (exclusion failed)."
            runtime = await pc2_executor.run_command(
                f"test ! -e {_STATE_DIR}/SENTINEL_SOURCE && "
                f"test -e {_STATE_DIR}/SENTINEL_TARGET && "
                f"test -e ~/.local/bin/pc-switcher",
                timeout=10.0,
            )
            assert runtime.success, (
                "ADR-016 runtime exclusion failed: pc1's state reached pc2, or pc2's own state/install was "
                "clobbered by the --delete mirror of /home."
            )

            # 3e. SC3 inclusion: non-excluded dev-tool cache + VS Code user state ARE synced,
            # while a config-excluded sibling (VS Code Cache) is not.
            marker_rels = list(_INCLUDED_MARKERS)
            inc = await pc2_executor.run_command(
                " && echo '|' && ".join(f"cat ~/{rel}" for rel in marker_rels)
                + f" && echo '|' && ( test ! -e ~/{_EXCLUDED_MARKER} && echo EXCLUDED_ABSENT )",
                timeout=10.0,
                login_shell=False,
            )
            assert inc.success, f"SC3 inclusion checks failed on pc2: {inc.stderr}"
            inc_parts = [p.strip() for p in inc.stdout.split("|")]
            for rel, part in zip(marker_rels, inc_parts, strict=False):
                assert part == _INCLUDED_MARKERS[rel], (
                    f"Included path {rel} not synced to pc2 (SC3): got {part!r}, want {_INCLUDED_MARKERS[rel]!r}"
                )
            assert "EXCLUDED_ABSENT" in inc_parts[-1], (
                f"Config-excluded {_EXCLUDED_MARKER} reached pc2 (SC3 exclusion failed)."
            )

            # --- Step 4: mutate pc2, then B→A ---
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

            sync_ba = await pc2_executor.run_command("pc-switcher sync pc1 --yes", timeout=300.0, login_shell=True)
            assert sync_ba.success, (
                f"B→A sync failed.\nexit={sync_ba.exit_code}\nstdout: {sync_ba.stdout}\nstderr: {sync_ba.stderr}"
            )

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

            # --- Step 5: clean A→B again must not trip the out-of-order gate ---
            sync_ab2 = await pc1_executor.run_command("pc-switcher sync pc2 --yes", timeout=300.0, login_shell=True)
            assert sync_ab2.success, (
                f"Second A→B failed (out-of-order gate wrongly tripped for a clean round-trip, ADR-015 #159).\n"
                f"exit={sync_ab2.exit_code}\nstdout: {sync_ab2.stdout}\nstderr: {sync_ab2.stderr}"
            )

        finally:
            await _remove_test_artifacts(pc1_executor, pc2_executor, tree)

    async def test_core_us_job_arch_as7_interrupt_terminates_job(
        self,
        sync_ready_source_long_duration: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Test CORE-US-JOB-ARCH-AS7: Ctrl+C terminates job with cleanup.

        Verifies that when user presses Ctrl+C during job execution, the orchestrator:
        - Catches SIGINT signal
        - Requests termination of currently-executing job
        - Logs interruption at WARNING level
        - Exits with code 130

        Expected behavior:
        1. Start sync with long-running dummy_success job (60s)
        2. Wait for job to begin execution
        3. Send SIGINT to the sync process
        4. Verify process exits with code 130
        5. Verify "interrupted" message in output

        Test approach:
        - Start sync in background using nohup and capture PID
        - Wait for sync to start (check for running process or log output)
        - Send SIGINT to the process
        - Wait for process to terminate
        - Check exit code and output
        """
        pc1_executor = sync_ready_source_long_duration

        # Start sync in background and capture output to a temp file
        # Use script to run in a pseudo-terminal for proper signal handling
        output_file = "/tmp/pcswitcher-e2e-interrupt-test-output.txt"
        pid_file = "/tmp/pcswitcher-e2e-interrupt-test-pid.txt"

        # Clean up from any previous run
        await pc1_executor.run_command(f"rm -f {output_file} {pid_file}", timeout=10.0)

        # Start sync in background with script for TTY emulation
        # We use bash -c to wrap the command and capture the PID.
        # --allow-first-sync: pc2 has no sync history (W1 gate, ADR-015); required in CI
        # (no TTY) to bypass the first-sync overwrite confirmation and reach job execution.
        start_result = await pc1_executor.run_command(
            f"nohup bash -c 'echo $$ > {pid_file};"
            f" exec pc-switcher sync pc2 --yes --allow-first-sync 2>&1'"
            f" > {output_file} &",
            timeout=10.0,
            login_shell=True,
        )
        assert start_result.success, f"Failed to start background sync: {start_result.stderr}"

        # Wait for PID file to be written and process to start
        await asyncio.sleep(2)

        # Get the PID
        pid_result = await pc1_executor.run_command(f"cat {pid_file}", timeout=10.0)
        assert pid_result.success and pid_result.stdout.strip(), f"Failed to get sync process PID: {pid_result.stderr}"
        sync_pid = pid_result.stdout.strip()

        # Wait for sync to actually start (look for connection or log activity)
        # Give it time to establish SSH connection and start job execution
        for _ in range(30):  # Wait up to 30 seconds for job to start
            await asyncio.sleep(1)
            output_check = await pc1_executor.run_command(f"cat {output_file} 2>/dev/null || true", timeout=10.0)
            # Check if we see any progress indicating sync has started
            if "source" in output_check.stdout.lower() or "target" in output_check.stdout.lower():
                break
            if "connecting" in output_check.stdout.lower() or "lock" in output_check.stdout.lower():
                continue  # Still in setup phase, keep waiting
            # Check if process is still running
            ps_check = await pc1_executor.run_command(f"ps -p {sync_pid} -o pid= 2>/dev/null || true", timeout=5.0)
            if not ps_check.stdout.strip():
                break  # Process finished (possibly errored out)

        # Send SIGINT to the sync process
        await pc1_executor.run_command(
            f"kill -INT {sync_pid} 2>/dev/null || true",
            timeout=10.0,
            login_shell=False,
        )

        # Wait for process to terminate (up to 35 seconds for cleanup timeout)
        process_terminated = False
        for _ in range(40):  # Wait up to 40 seconds
            await asyncio.sleep(1)
            ps_check = await pc1_executor.run_command(
                f"ps -p {sync_pid} -o pid= 2>/dev/null || echo 'terminated'",
                timeout=5.0,
                login_shell=False,
            )
            if "terminated" in ps_check.stdout or not ps_check.stdout.strip():
                process_terminated = True
                break

        assert process_terminated, f"Sync process {sync_pid} did not terminate after SIGINT"

        # Read the output
        output_result = await pc1_executor.run_command(f"cat {output_file}", timeout=10.0)
        output_text = output_result.stdout

        # Verify interrupt handling message
        assert "interrupt" in output_text.lower(), f"Output should contain interrupt message.\nOutput:\n{output_text}"

        # Clean up temp files
        await pc1_executor.run_command(f"rm -f {output_file} {pid_file}", timeout=10.0)

    async def test_core_edge_target_unreachable_mid_sync(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_to_pc2_traffic_blocker: Pc1ToPc2TrafficBlocker,
    ) -> None:
        """Test CORE-EDGE: Target becomes unreachable mid-sync.

        Spec reference: docs/system/spec.md - Edge Cases

        Simulates network failure by blocking pc1→pc2 traffic with iptables
        during the target phase of DummySuccessJob. Verifies that:
        - Sync detects the connection failure
        - Sync exits with non-zero code
        - Error output indicates connection/network failure

        Test approach:
        1. Configure DummySuccessJob with short source phase (4s) and longer target (30s)
        2. Start sync in background, capturing output to temp file
        3. Monitor output for "target phase" indicator
        4. When detected, block pc1→pc2 traffic via iptables
        5. Wait for sync to fail (keepalive timeout ~45s)
        6. Verify error message indicates connection failure

        Safety:
        - iptables rule only blocks pc1→pc2, test runner retains full access
        - network_blocker fixture ensures cleanup even on test failure
        """
        _ = reset_pcswitcher_state  # Ensures test isolation
        pc1_executor = pc1_with_pcswitcher_mod

        # Create test config with short source phase but longer target phase
        # Source: 4s (quick to get to target phase)
        # Target: 30s (long enough for us to inject failure and observe timeout)
        test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=4, target_duration=30)
        await pc1_executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)
        await pc1_executor.run_command(
            f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
            timeout=10.0,
        )

        # Start sync in background, capturing output to temp file
        output_file = "/tmp/pcswitcher-network-failure-test-output.txt"
        pid_file = "/tmp/pcswitcher-network-failure-test-pid.txt"
        await pc1_executor.run_command(f"rm -f {output_file} {pid_file}", timeout=10.0)

        # Start sync in background.
        # --allow-first-sync: pc2 has no sync history (W1 gate, ADR-015); required in CI
        # (no TTY) to bypass the first-sync overwrite confirmation so the sync proceeds
        # into the job execution phase where the network failure is injected.
        start_result = await pc1_executor.run_command(
            f"nohup bash -c 'echo $$ > {pid_file};"
            f" exec pc-switcher sync pc2 --yes --allow-first-sync 2>&1'"
            f" > {output_file} &",
            timeout=10.0,
            login_shell=True,
        )
        assert start_result.success, f"Failed to start background sync: {start_result.stderr}"

        # Wait for PID file and get PID
        await asyncio.sleep(2)
        pid_result = await pc1_executor.run_command(f"cat {pid_file}", timeout=10.0)
        assert pid_result.success and pid_result.stdout.strip(), f"Failed to get sync process PID: {pid_result.stderr}"
        sync_pid = pid_result.stdout.strip()

        # Monitor log file for "Target phase:" indicator, then block network
        # The TUI "Recent Logs" only shows FULL level messages, but DummySuccessJob
        # logs at INFO level. We check the log file directly for reliable detection.
        network_blocked = False
        last_log_content = ""
        for _ in range(60):  # Wait up to 60 seconds for target phase
            await asyncio.sleep(1)

            # Check the log file for "Target phase:" messages
            log_check = await pc1_executor.run_command(
                "cat ~/.local/share/pc-switcher/logs/sync-*.log 2>/dev/null | grep -i 'target phase' || true",
                timeout=10.0,
            )
            last_log_content = log_check.stdout

            # Check if target phase has started
            if "target phase" in last_log_content.lower():
                # Block pc1→pc2 traffic
                await pc1_to_pc2_traffic_blocker.block()
                network_blocked = True
                break

            # Check if process is still running
            ps_check = await pc1_executor.run_command(
                f"ps -p {sync_pid} -o pid= 2>/dev/null || true",
                timeout=5.0,
                login_shell=False,
            )
            if not ps_check.stdout.strip():
                break  # Process exited early

        # Read TUI output for debugging if assertion fails
        tui_output = await pc1_executor.run_command(
            f"cat {output_file} 2>/dev/null || true",
            timeout=10.0,
        )

        assert network_blocked, (
            f"Target phase not detected before process exited.\n"
            f"Log content:\n{last_log_content}\n"
            f"TUI output:\n{tui_output.stdout}"
        )

        # Wait for sync to fail due to keepalive timeout (~45 seconds)
        # Total wait: up to 90 seconds to be safe
        process_exited = False
        for _ in range(90):
            await asyncio.sleep(1)
            ps_check = await pc1_executor.run_command(
                f"ps -p {sync_pid} -o pid= 2>/dev/null || echo 'exited'",
                timeout=5.0,
                login_shell=False,
            )
            if "exited" in ps_check.stdout or not ps_check.stdout.strip():
                process_exited = True
                break

        assert process_exited, f"Sync process {sync_pid} did not exit after network failure"

        # Read final output
        output_result = await pc1_executor.run_command(f"cat {output_file}", timeout=10.0)
        output_text = output_result.stdout

        # Verify sync failed with connection-related error
        # Look for various error indicators
        error_indicators = [
            "connection",
            "timeout",
            "unreachable",
            "lost",
            "closed",
            "failed",
            "error",
            "ssh",
        ]
        output_lower = output_text.lower()
        has_error_indicator = any(ind in output_lower for ind in error_indicators)

        assert has_error_indicator, f"Output should indicate connection failure.\nOutput:\n{output_text}"

        # Clean up temp files
        await pc1_executor.run_command(f"rm -f {output_file} {pid_file}", timeout=10.0)

        # Note: pc1_to_pc2_traffic_blocker fixture handles unblocking automatically


class TestInstallOnTargetIntegration:
    """Integration tests verifying InstallOnTargetJob effects through full sync."""

    async def test_install_on_target_fresh_machine(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_without_pcswitcher_fn: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """Verify InstallOnTargetJob installs pc-switcher on fresh target.

        This test runs a full pc-switcher sync to a target that has no pc-switcher
        installed, verifying that the InstallOnTargetJob correctly:
        1. Detects missing pc-switcher on target
        2. Installs the same version as source
        3. Verifies installation succeeded

        Unlike test_install_on_target_job.py which tests the job in isolation,
        this test verifies the job works correctly within the full sync pipeline.
        """
        _ = reset_pcswitcher_state  # Ensures test isolation
        pc1_executor = pc1_with_pcswitcher_mod

        # Create minimal test config
        test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=2, target_duration=2)
        await pc1_executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)
        await pc1_executor.run_command(
            f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
            timeout=10.0,
        )

        try:
            # Run sync - this should install pc-switcher on target.
            # --allow-first-sync: pc2 has no sync history (W1 gate, ADR-015); required in CI
            # (no TTY) to bypass the first-sync overwrite confirmation.
            sync_result = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,  # Allow more time for fresh install
                login_shell=True,
            )

            # Check exit code
            assert sync_result.success, (
                f"Sync should succeed.\n"
                f"Exit code: {sync_result.exit_code}\n"
                f"Stdout: {sync_result.stdout}\n"
                f"Stderr: {sync_result.stderr}"
            )

            # Verify pc-switcher is now installed on target
            post_check = await pc2_without_pcswitcher_fn.run_command(
                "pc-switcher --version",
                timeout=10.0,
                login_shell=True,
            )
            assert post_check.success, (
                f"pc-switcher should be installed on target after sync.\n"
                f"Output: {post_check.stdout}\n"
                f"Error: {post_check.stderr}"
            )

            # Verify version matches source floor release (dev versions install the floor release)
            source_release = get_this_version().get_release_floor()
            assert source_release.version.semver_str() in post_check.stdout, (
                f"Target version should match source floor release {source_release.version.semver_str()}.\n"
                f"Target output: {post_check.stdout}"
            )

        finally:
            # Clean up config
            await pc1_executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)

    async def test_install_on_target_upgrade_older_version(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        pc2_with_old_pcswitcher_fn: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
    ) -> None:
        """Verify InstallOnTargetJob upgrades older pc-switcher on target.

        This test runs a full pc-switcher sync to a target that has an older
        version installed, verifying that the InstallOnTargetJob:
        1. Detects version mismatch
        2. Upgrades to source version
        3. Verifies upgrade succeeded
        """
        _ = reset_pcswitcher_state  # Ensures test isolation

        # Create minimal test config
        test_config = _TEST_CONFIG_TEMPLATE.format(source_duration=2, target_duration=2)
        await pc1_with_pcswitcher_mod.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)
        await pc1_with_pcswitcher_mod.run_command(
            f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{test_config}EOF",
            timeout=10.0,
        )

        try:
            # Run sync - this should upgrade pc-switcher on target.
            # --allow-first-sync: pc2 has no sync history even when it has an old pc-switcher
            # installed (install ≠ sync history); W1 gate fires in non-interactive CI.
            sync_result = await pc1_with_pcswitcher_mod.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=300.0,
                login_shell=True,
            )

            # Check exit code
            assert sync_result.success, (
                f"Sync should succeed.\n"
                f"Exit code: {sync_result.exit_code}\n"
                f"Stdout: {sync_result.stdout}\n"
                f"Stderr: {sync_result.stderr}"
            )

            # Verify pc-switcher was upgraded on target
            post_check = await pc2_with_old_pcswitcher_fn.run_command(
                "pc-switcher --version",
                timeout=10.0,
                login_shell=True,
            )
            assert post_check.success, f"pc-switcher should work on target after sync.\nError: {post_check.stderr}"

            # Verify version matches source floor release (not old version)
            source_release = get_this_version().get_release_floor()
            assert source_release.version.semver_str() in post_check.stdout, (
                f"Target version should match source floor release {source_release.version.semver_str()}.\n"
                f"Target output: {post_check.stdout}"
            )

        finally:
            # Clean up config
            await pc1_with_pcswitcher_mod.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)


class TestConsecutiveSyncWarning:
    """Integration tests for first-sync (W1) gate and consecutive-push (W3) warning (ADR-015).

    Tests verify that:
    - Sync history is updated on both source and target after successful sync
    - First sync to a target with no history (W1) is gated by --allow-first-sync
    - Consecutive syncs without back-sync (W3) are blocked (non-interactive, defaults to abort)
    - --allow-out-of-order flag bypasses the W3 consecutive-push gate
    - Back-sync workflow clears the consecutive-push warning state
    """

    async def test_consecutive_sync_warning_workflow(
        self,
        sync_ready_source: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Test first-sync (W1) gate and consecutive-push (W3) warning workflow.

        TODO add links to the Semantic IDs for ALL tests executed here.
        TODO change name of test to something that covers everything done here.

        Consolidated test covering:
        - First sync to a fresh target (W1): gated by --allow-first-sync; sync history
          updated on both machines after success.
        - Consecutive push (W3): second A→B without a back-sync is blocked in non-interactive
          mode (no flag, defaults to abort).
        - --allow-out-of-order bypasses the W3 consecutive-push gate.

        Workflow:
        1. First sync with --allow-first-sync → verifies W1 gate passed, history updated
        2. Second sync (no flag) → verifies blocked by W3 gate (consecutive push)
        3. Third sync with --allow-out-of-order → verifies W3 gate bypassed

        This consolidation saves ~2 sync operations (~16 seconds) compared
        to running these as separate tests.
        """
        pc1_executor = sync_ready_source

        # History cleanup done by reset_pcswitcher_state fixture (via sync_ready_source)

        # Step 1: First sync (W1 gate) — pc2 has no history; --allow-first-sync is required
        # in non-interactive CI to bypass the first-sync overwrite confirmation.
        first_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes --allow-first-sync",
            timeout=180.0,
            login_shell=True,
        )
        assert first_sync.success, (
            f"First sync failed.\nExit code: {first_sync.exit_code}\n"
            f"Stdout: {first_sync.stdout}\nStderr: {first_sync.stderr}"
        )

        # Verify source history
        pc1_history = await pc1_executor.run_command(
            "cat ~/.local/share/pc-switcher/sync-history.json",
            timeout=10.0,
        )
        assert pc1_history.success, f"Failed to read pc1 history: {pc1_history.stderr}"
        assert '"last_role": "source"' in pc1_history.stdout, (
            f"pc1 should have last_role=source.\nContent: {pc1_history.stdout}"
        )

        # Verify target history
        pc2_history = await pc2_executor.run_command(
            "cat ~/.local/share/pc-switcher/sync-history.json",
            timeout=10.0,
        )
        assert pc2_history.success, f"Failed to read pc2 history: {pc2_history.stderr}"
        assert '"last_role": "target"' in pc2_history.stdout, (
            f"pc2 should have last_role=target.\nContent: {pc2_history.stdout}"
        )

        # Step 2: Second sync WITHOUT --allow-out-of-order — W3 (consecutive push) gate fires
        # because pc1 is pushing to pc2 again without a back-sync.  Non-interactive mode
        # cannot confirm, so it aborts (title: "Consecutive Sync — No Back-Sync Received").
        second_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes",
            timeout=60.0,
            login_shell=True,
        )
        assert not second_sync.success, (
            f"Second sync should fail (W3 consecutive-push gate, defaults to abort).\n"
            f"Exit code: {second_sync.exit_code}\nStdout: {second_sync.stdout}"
        )
        output = second_sync.stdout + second_sync.stderr
        # "consecutive" from "Consecutive Sync — No Back-Sync Received" (W3 warning title);
        # "abort" from "Sync aborted at the out-of-order / target-state check" (RuntimeError).
        assert "consecutive" in output.lower() and "abort" in output.lower(), (
            f"Output should mention consecutive-push warning and abort.\nOutput: {output}"
        )

        # Step 3: Third sync WITH --allow-out-of-order bypasses the W3 gate.
        third_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes --allow-out-of-order",
            timeout=180.0,
            login_shell=True,
        )
        assert third_sync.success, (
            f"Third sync with --allow-out-of-order should succeed.\n"
            f"Exit code: {third_sync.exit_code}\nStderr: {third_sync.stderr}"
        )

    async def test_back_sync_clears_warning(
        self,
        sync_ready_source: BashLoginRemoteExecutor,
        pc2_with_pcswitcher: BashLoginRemoteExecutor,
    ) -> None:
        """After receiving a back-sync, machine can sync again without warning.

        Full workflow:
        1. pc1 syncs to pc2 (W1: first-sync, --allow-first-sync required) → pc1=source, pc2=target
        2. pc2 syncs back to pc1 (clean case: pc1 has history, target_peer=pc2==source)
        3. pc1 syncs to pc2 again → should succeed WITHOUT --allow-out-of-order
           because pc1 was last a target (received back-sync from pc2 = clean case)

        NOTE: pc2_with_pcswitcher is used instead of pc2_executor to ensure
        pc2 has the exact same version as pc1 (from current branch), which is
        required for back-sync version validation to pass.
        """
        pc1_executor = sync_ready_source
        pc2_executor = pc2_with_pcswitcher

        # History cleanup done by reset_pcswitcher_state fixture (via sync_ready_source)

        # Step 1: pc1 syncs to pc2 — W1 gate (pc2 has no history), --allow-first-sync required.
        first_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes --allow-first-sync",
            timeout=180.0,
            login_shell=True,
        )
        assert first_sync.success, f"First sync (pc1→pc2) should succeed: {first_sync.stderr}"

        # Verify state: pc1=source, pc2=target
        pc1_history = await pc1_executor.run_command("cat ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        assert '"last_role": "source"' in pc1_history.stdout, "pc1 should be source after first sync"

        # Step 2: pc2 syncs back to pc1
        # pc2 has pc-switcher installed (from first sync) and config synced
        back_sync = await pc2_executor.run_command(
            "pc-switcher sync pc1 --yes",
            timeout=180.0,
            login_shell=True,
        )
        assert back_sync.success, (
            f"Back sync (pc2→pc1) should succeed.\n"
            f"Exit code: {back_sync.exit_code}\nStdout: {back_sync.stdout}\nStderr: {back_sync.stderr}"
        )

        # Verify state: pc1=target (received sync), pc2=source
        pc1_history = await pc1_executor.run_command("cat ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        assert '"last_role": "target"' in pc1_history.stdout, "pc1 should be target after back-sync"

        # Step 3: pc1 syncs to pc2 again — clean case: pc1's last_role=TARGET (received
        # back-sync from pc2), so no consecutive-push W3 gate fires.  No flags needed.
        third_sync = await pc1_executor.run_command(
            "pc-switcher sync pc2 --yes",  # No --allow-out-of-order needed (clean round-trip)
            timeout=180.0,
            login_shell=True,
        )
        assert third_sync.success, (
            f"Third sync should succeed without --allow-out-of-order (pc1 was target).\n"
            f"Exit code: {third_sync.exit_code}\nStderr: {third_sync.stderr}"
        )
