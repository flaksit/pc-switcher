"""Integration tests for logging system.

Tests LOG-FR-AGGREGATE: Aggregation of source and target logs into unified log stream.

This test verifies that the logging system correctly aggregates logs from
both source and target machines during a real sync operation.

Note: LOG-US-SYSTEM-AS6 (logs --last command) is tested in tests/unit/cli/test_commands.py
as it doesn't require VM infrastructure.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from pcswitcher.executor import BashLoginRemoteExecutor

# Test config for logging integration tests
_TEST_CONFIG = """# Test configuration for logging integration tests
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


@pytest_asyncio.fixture
async def logging_test_source(
    pc1_with_pcswitcher_mod: BashLoginRemoteExecutor,
    reset_pcswitcher_state: None,
) -> AsyncIterator[BashLoginRemoteExecutor]:
    """Provide pc1 configured for logging tests.

    Sets up a minimal config with short-duration dummy job for fast tests.
    """
    _ = reset_pcswitcher_state
    executor = pc1_with_pcswitcher_mod

    # Backup existing config if any
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml ]; then "
        "cp ~/.config/pc-switcher/config.yaml ~/.config/pc-switcher/config.yaml.log-backup; "
        "fi",
        timeout=10.0,
    )

    # Create test config
    await executor.run_command("mkdir -p ~/.config/pc-switcher", timeout=10.0)
    write_result = await executor.run_command(
        f"cat > ~/.config/pc-switcher/config.yaml << 'EOF'\n{_TEST_CONFIG}EOF",
        timeout=10.0,
    )
    assert write_result.success, f"Failed to write test config: {write_result.stderr}"

    yield executor

    # Cleanup: restore original config
    await executor.run_command("rm -f ~/.config/pc-switcher/config.yaml", timeout=10.0)
    await executor.run_command(
        "if [ -f ~/.config/pc-switcher/config.yaml.log-backup ]; then "
        "mv ~/.config/pc-switcher/config.yaml.log-backup ~/.config/pc-switcher/config.yaml; "
        "fi",
        timeout=10.0,
    )


async def test_log_fr_aggregate(
    logging_test_source: BashLoginRemoteExecutor,
    pc2_executor: BashLoginRemoteExecutor,
) -> None:
    """Test LOG-FR-AGGREGATE: System aggregates logs from both source and target into unified log stream.

    Spec reference: LOG-FR-AGGREGATE, LOG-FR-SESSION-HOSTNAMES

    Verifies that during a sync operation, logs from both source-side orchestrator
    and target-side operations are aggregated into a single unified log file with
    proper host identification.

    Expected behavior:
    1. Run a sync operation from pc1 to pc2
    2. During sync, both source (pc1) and target (pc2) operations emit logs
    3. All logs are written to single log file in JSON Lines format
    4. Each log entry includes:
       - host: "source" or "target"
       - timestamp, level, job, event fields
    5. Hostname mapping (source and target machine names) logged once at session start
    6. Log file contains entries from both hosts in chronological order
    7. Verify log aggregation worked by parsing the log file

    Test approach:
    - Configure pc-switcher on pc1 with dummy jobs
    - Run pc-switcher sync from pc1 to pc2
    - Retrieve the generated log file from pc1
    - Parse JSON Lines and verify entries from both source and target hosts
    - Verify session start contains hostname mapping
    """
    pc1_executor = logging_test_source
    _ = pc2_executor  # Used implicitly as sync target

    # Run pc-switcher sync from pc1 to pc2
    sync_result = await pc1_executor.run_command(
        "pc-switcher sync pc2 --yes",
        timeout=180.0,
        login_shell=True,
    )

    # Verify sync completed successfully
    assert sync_result.success, (
        f"pc-switcher sync failed with exit code {sync_result.exit_code}.\n"
        f"stdout: {sync_result.stdout}\n"
        f"stderr: {sync_result.stderr}"
    )

    # Get the latest log file path
    log_path_result = await pc1_executor.run_command(
        "ls -t ~/.local/share/pc-switcher/logs/sync-*.log | head -1",
        timeout=10.0,
    )
    assert log_path_result.success, f"No log files found: {log_path_result.stderr}"
    log_path = log_path_result.stdout.strip()
    assert log_path, "Log file path is empty"

    # Read the log file content
    log_content_result = await pc1_executor.run_command(
        f"cat {log_path}",
        timeout=10.0,
    )
    assert log_content_result.success, f"Failed to read log file: {log_content_result.stderr}"

    # Parse JSON Lines
    log_lines = log_content_result.stdout.strip().split("\n")
    log_entries = []
    for i, line in enumerate(log_lines):
        if line.strip():
            try:
                entry = json.loads(line)
                log_entries.append(entry)
            except json.JSONDecodeError as e:
                pytest.fail(f"Invalid JSON on line {i + 1}: {e}\nLine content: {line[:200]}")

    assert log_entries, "Log file is empty"

    # Verify LOG-FR-SESSION-HOSTNAMES: session start contains hostname mapping
    session_start_entries = [e for e in log_entries if "source_hostname" in e and "target_hostname" in e]
    assert session_start_entries, (
        f"No session start entry found with hostname mapping.\nFirst 5 entries: {log_entries[:5]}"
    )

    # Verify the hostname mapping contains actual hostnames (not empty)
    first_session_entry = session_start_entries[0]
    assert first_session_entry.get("source_hostname"), "source_hostname is empty"
    assert first_session_entry.get("target_hostname"), "target_hostname is empty"

    # Verify LOG-FR-AGGREGATE: entries from both source and target hosts
    source_entries = [e for e in log_entries if e.get("host") == "source"]
    target_entries = [e for e in log_entries if e.get("host") == "target"]

    assert source_entries, (
        f"No log entries with host='source' found.\nSample entries: {[e.get('host') for e in log_entries[:10]]}"
    )
    assert target_entries, (
        f"No log entries with host='target' found.\nSample entries: {[e.get('host') for e in log_entries[:10]]}"
    )

    # Verify required fields in log entries (LOG-FR-JSON)
    for entry in log_entries[:10]:  # Check first 10 entries
        assert "timestamp" in entry, f"Missing timestamp in entry: {entry}"
        assert "level" in entry, f"Missing level in entry: {entry}"
        assert "event" in entry, f"Missing event in entry: {entry}"
