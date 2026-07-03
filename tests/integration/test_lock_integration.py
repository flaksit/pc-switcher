"""Integration test for the cross-machine target lock (flock-over-SSH).

The unified lock (`~/.local/share/pc-switcher/pc-switcher.lock`) prevents a
machine from participating in two syncs at once. With only two machines the
*source* lock always trips first (a second sync trips its own local lock before
reaching the remote target lock), so the *target* lock path — a persistent
`flock` held on the target over SSH (`start_persistent_remote_lock`) — is only
reachable when the target is busy for a reason independent of the second sync's
source. Unit tests cover this by mocking the remote lock; this test exercises the
real remote `flock` conflict by pre-holding the target's lock out-of-band.

VM Requirements: see test_folder_sync.py (same pc1/pc2 Hetzner VMs).
"""

from __future__ import annotations

from pcswitcher.executor import BashLoginRemoteExecutor

_LOCK = "$HOME/.local/share/pc-switcher/pc-switcher.lock"

# Minimal valid config: the sync fails at the target-lock phase (before job
# discovery/validation), so the folder contents are never inspected.
_MIN_CONFIG = """\
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

        try:
            await _write_config(pc1_executor, _MIN_CONFIG)

            # Hold pc2's unified lock out-of-band via a detached flock process.
            held = await pc2_executor.run_command(
                f'mkdir -p "$(dirname "{_LOCK}")"\n'
                f"nohup setsid flock -n \"{_LOCK}\" -c 'sleep 60' >/dev/null 2>&1 &\n"
                "sleep 0.5\n"
                f'if flock -n "{_LOCK}" -c true; then echo NOT_HELD; else echo HELD; fi',
                timeout=15.0,
            )
            assert held.success and "HELD" in held.stdout, (
                f"Failed to pre-hold pc2's lock out-of-band.\nstdout: {held.stdout}\nstderr: {held.stderr}"
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
            # Release the held lock (kills the flock holder; the child sleep dies with it).
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
