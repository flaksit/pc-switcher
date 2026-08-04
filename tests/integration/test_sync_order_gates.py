"""Integration tests for the sync-order gates that guard a `pc-switcher sync` (ADR-015).

The gates live in the CLI and the orchestrator, not in any sync job, so these carry
`area_core`. CI selects them for a change to `sync_history.py`; a change to the gate code in
cli.py or orchestrator.py reaches every area and selects the full suite, which runs them too.

**What these tests cover:**
- W1 first-sync gate: a target with no sync history needs --allow-first-sync
- W3 consecutive-push gate: a second A->B without a back-sync aborts non-interactively
- --allow-out-of-order bypasses the W3 gate
- Sync history on both machines, and a back-sync clearing the W3 state

**What these tests do NOT cover:**
- What a sync transfers (see test_end_to_end_sync.py). No sync job is enabled here: the
  gates decide before any job runs, so the runs below carry system jobs only.
- The gate decisions in isolation (see tests/unit/orchestrator/test_consecutive_sync.py and
  test_first_sync_scope.py) or the history file itself (see tests/unit/test_sync_history.py)
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pcswitcher.executor import BashLoginRemoteExecutor
from tests.integration import SKIP_INSTALL_ON_TARGET
from tests.integration.conftest import write_pcswitcher_config

pytestmark = pytest.mark.area_core

# What the gates need from a source config and nothing else: DEBUG logs to diagnose a
# failed run, and no `sync_jobs` section, so a sync that passes the gates does its system
# jobs and finishes. Everything else stays at pc-switcher's own defaults.
_GATE_TEST_CONFIG = """# Test configuration for sync-order gate tests

logging:
  file: DEBUG
  tui: DEBUG
  external: DEBUG
"""


@pytest.fixture
async def sync_ready_source(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    reset_pcswitcher_state: None,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 installed, its sync history cleared, and configured to run a sync.

    `reset_pcswitcher_state` removes config and history on both VMs before and after the
    test, so this only has to write the config the run needs.
    """
    _ = reset_pcswitcher_state  # Ensures cleanup runs before test
    executor = pc1_with_pcswitcher_mod

    await write_pcswitcher_config(executor, _GATE_TEST_CONFIG)

    yield executor


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
        - First sync to a fresh target (W1): blocked without --allow-first-sync in
          non-interactive mode, and nothing on the target changes; passes with the flag,
          after which sync history is updated on both machines.
        - Consecutive push (W3): second A→B without a back-sync is blocked in non-interactive
          mode (no flag, defaults to abort).
        - --allow-out-of-order bypasses the W3 consecutive-push gate.

        Workflow:
        1. First sync, no flag → verifies W1 gate blocks and pc2 stays untouched
        2. Same sync with --allow-first-sync → verifies W1 gate passed, history updated
        3. Second sync (no flag) → verifies blocked by W3 gate (consecutive push)
        4. Third sync with --allow-out-of-order → verifies W3 gate bypassed

        Each step depends on the state the previous one left, so they share one sync sequence
        rather than each paying for its own.
        """
        pc1_executor = sync_ready_source

        # History cleanup done by reset_pcswitcher_state fixture (via sync_ready_source)

        # Step 1: sync without --allow-first-sync — pc2 has no history, so the W1 gate fires
        # and non-interactive mode cannot confirm it.
        blocked_sync = await pc1_executor.run_command(
            f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes",
            timeout=180.0,
            login_shell=True,
        )
        assert not blocked_sync.success, (
            f"Sync without --allow-first-sync should fail (W1 gate, defaults to abort).\n"
            f"Exit code: {blocked_sync.exit_code}\nStdout: {blocked_sync.stdout}"
        )
        blocked_output = blocked_sync.stdout + blocked_sync.stderr
        # "--allow-first-sync" from the confirmer's non-interactive refusal; "abort" from
        # "Sync aborted at the sync-order check" (SyncAborted).
        assert "--allow-first-sync" in blocked_output and "abort" in blocked_output.lower(), (
            f"Output should name the first-sync flag and the abort.\nOutput: {blocked_output}"
        )
        # The gate runs before any job, so the target must still have no history at all.
        blocked_history = await pc2_executor.run_command(
            "cat ~/.local/share/pc-switcher/sync-history.json 2>/dev/null || echo absent",
            timeout=10.0,
        )
        assert blocked_history.stdout.strip() == "absent", (
            f"Blocked first sync wrote state to pc2.\nContent: {blocked_history.stdout}"
        )

        # Step 2: First sync (W1 gate) — pc2 has no history; --allow-first-sync is required
        # in non-interactive CI to bypass the first-sync overwrite confirmation.
        first_sync = await pc1_executor.run_command(
            f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes --allow-first-sync",
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

        # Step 3: Second sync WITHOUT --allow-out-of-order — W3 (consecutive push) gate fires
        # because pc1 is pushing to pc2 again without a back-sync.  Non-interactive mode
        # cannot confirm, so it aborts (title: "Consecutive Sync — No Back-Sync Received").
        second_sync = await pc1_executor.run_command(
            f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes",
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

        # Step 4: Third sync WITH --allow-out-of-order bypasses the W3 gate.
        third_sync = await pc1_executor.run_command(
            f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes --allow-out-of-order",
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
            f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes --allow-first-sync",
            timeout=180.0,
            login_shell=True,
        )
        assert first_sync.success, f"First sync (pc1→pc2) should succeed: {first_sync.stderr}"

        # Verify state: pc1=source, pc2=target
        pc1_history = await pc1_executor.run_command("cat ~/.local/share/pc-switcher/sync-history.json", timeout=10.0)
        assert '"last_role": "source"' in pc1_history.stdout, "pc1 should be source after first sync"

        # Step 2: pc2 syncs back to pc1 — pc1_with_pcswitcher_mod (via sync_ready_source)
        # already has pc1 at the branch tip, so nothing here needs installing either way.
        back_sync = await pc2_executor.run_command(
            f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc1 --yes",
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
            # No --allow-out-of-order needed (clean round-trip)
            f"{SKIP_INSTALL_ON_TARGET} pc-switcher sync pc2 --yes",
            timeout=180.0,
            login_shell=True,
        )
        assert third_sync.success, (
            f"Third sync should succeed without --allow-out-of-order (pc1 was target).\n"
            f"Exit code: {third_sync.exit_code}\nStderr: {third_sync.stderr}"
        )
