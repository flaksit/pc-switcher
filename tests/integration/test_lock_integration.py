"""Integration test for the cross-machine target lock (flock-over-SSH).

The unified lock (`~/.local/share/pc-switcher/pc-switcher.lock`) prevents a
machine from participating in two syncs at once. With only two machines the
*source* lock always trips first (a second sync trips its own local lock before
reaching the remote target lock), so the *target* lock path — a persistent
`flock` held on the target over SSH (`start_persistent_remote_lock`) — is only
reachable when the target is busy for a reason independent of the second sync's
source. Unit tests cover this by mocking the remote lock; this test exercises the
real remote `flock` conflict by pre-holding the target's lock out-of-band.

VM Requirements: same pc1/pc2 Hetzner VMs as test_end_to_end_sync.py.
"""

from __future__ import annotations

import asyncio
import contextlib

from pcswitcher.executor import BashLoginRemoteExecutor

_LOCK = "$HOME/.local/share/pc-switcher/pc-switcher.lock"

# Minimal valid config. The sync must fail at the target-lock phase (before job
# discovery/execution). We deliberately use only the harmless dummy_success job and
# NO folder_sync: if the target lock ever regresses and the sync proceeds, it must
# not mirror real /home (a /home --delete mirror would clobber the target's
# .ssh/known_hosts and break subsequent tests — the very bug this test guards).
_MIN_CONFIG = """\
logging:
  file: DEBUG
  tui: INFO
  external: WARNING
sync_jobs:
  dummy_success: true
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
"""


class TestTargetLockConflict:
    """A→B fails at the target lock when B's unified lock is already held."""

    async def test_target_lock_blocks_when_target_busy(
        self,
        pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
        reset_pcswitcher_state: None,
        pc1_executor: BashLoginRemoteExecutor,
        pc2_executor: BashLoginRemoteExecutor,
    ) -> None:
        """Pre-hold pc2's lock, then A→pc2 must fail fast at the remote target lock.

        Asserts the failure identifies the target as busy and includes the
        how-to-unblock guidance (auto-release + force-clear the holder, not rm).
        """
        _ = (pc1_with_pcswitcher_mod, reset_pcswitcher_state)

        holder: asyncio.Task[object] | None = None
        try:
            await _write_config(pc1_executor, _MIN_CONFIG)
            await pc2_executor.run_command(f'mkdir -p "$(dirname "{_LOCK}")"', timeout=10.0)

            # Hold pc2's unified lock for the duration of the sync via a CONCURRENT,
            # still-running command. Keeping the SSH channel open keeps the remote
            # flock process alive (a detached background process would be reaped when
            # its channel closed, freeing the lock before the sync reached it).
            holder = asyncio.create_task(pc2_executor.run_command(f'flock -n "{_LOCK}" -c "sleep 45"', timeout=60.0))
            await asyncio.sleep(2.0)  # let flock acquire

            # Verify the lock is actually held (a non-blocking flock on another channel fails).
            check = await pc2_executor.run_command(
                f'if flock -n "{_LOCK}" -c true; then echo FREE; else echo HELD; fi',
                timeout=10.0,
            )
            assert "HELD" in check.stdout, (
                f"Precondition failed: pc2's lock is not held.\nstdout: {check.stdout}\nstderr: {check.stderr}"
            )

            # A→pc2 must fail at the target-lock phase (fast — the timeout guards
            # against a hang if the lock path ever became blocking).
            sync = await pc1_executor.run_command(
                "pc-switcher sync pc2 --yes --allow-first-sync",
                timeout=120.0,
                login_shell=True,
            )
            assert not sync.success, (
                f"A→pc2 should have failed at the target lock, got exit {sync.exit_code}.\n"
                f"stdout: {sync.stdout}\nstderr: {sync.stderr}"
            )
            out = sync.stdout + sync.stderr
            assert "already involved in a sync" in out, (
                f"Target-lock failure message missing.\nstdout: {sync.stdout}\nstderr: {sync.stderr}"
            )
            assert "releases automatically" in out and "pkill -f pc-switcher.lock" in out, (
                f"How-to-unblock guidance missing from target-lock error.\n{out}"
            )

        finally:
            if holder is not None:
                holder.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await holder
            # Belt-and-braces: kill any lingering holder and remove the lock file.
            await pc2_executor.run_command(
                f'pkill -f "pc-switcher.lock" || true; rm -f "{_LOCK}"',
                timeout=15.0,
            )
            await pc1_executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)


async def _write_config(executor: BashLoginRemoteExecutor, config: str) -> None:
    """Write the pc-switcher config to the remote VM."""
    result = await executor.run_command(
        f"mkdir -p ~/.config/pc-switcher && cat > ~/.config/pc-switcher/config.yaml << 'CONF_EOF'\n{config}CONF_EOF",
        timeout=10.0,
    )
    assert result.success, f"Failed to write config: {result.stderr}"
