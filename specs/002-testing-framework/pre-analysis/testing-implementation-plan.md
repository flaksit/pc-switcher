# Testing Implementation Plan for 001-Foundation

This document provides the detailed implementation plan for comprehensive testing of the foundation feature.

## Test Directory Structure

```text
tests/
├── conftest.py                      # Shared fixtures
├── pytest.ini                       # Pytest configuration
├── __init__.py
│
├── unit/                            # Fast tests, no VMs
│   ├── __init__.py
│   ├── conftest.py                  # Unit-specific fixtures
│   ├── test_config.py
│   ├── test_models.py
│   ├── test_events.py
│   ├── test_disk.py
│   ├── test_btrfs_snapshots.py
│   ├── test_version.py              # Extend existing
│   ├── test_lock.py
│   ├── test_logger.py
│   ├── test_executor.py
│   ├── test_ui.py                   # Event consumption, progress delivery
│   └── test_jobs/
│       ├── __init__.py
│       ├── test_base.py
│       ├── test_btrfs.py
│       ├── test_disk_space_monitor.py  # Extend existing
│       ├── test_install_on_target.py
│       ├── test_dummy_success.py
│       └── test_dummy_fail.py
│
├── contract/                        # Interface compliance (existing)
│   ├── __init__.py
│   └── test_job_interface.py
│
├── integration/                     # VM-required tests
│   ├── __init__.py
│   ├── conftest.py                  # VM fixtures
│   ├── test_connection.py
│   ├── test_executor.py
│   ├── test_lock.py
│   ├── test_disk.py
│   ├── test_btrfs_snapshots.py
│   ├── test_logger.py
│   ├── test_jobs/
│   │   ├── __init__.py
│   │   ├── test_btrfs.py
│   │   ├── test_install_on_target.py
│   │   ├── test_disk_space_monitor.py
│   │   ├── test_dummy_success.py
│   │   └── test_dummy_fail.py
│   ├── test_orchestrator.py
│   ├── test_cli.py
│   ├── test_cleanup_snapshots.py
│   └── test_install_script.py
│
├── infrastructure/                  # VM provisioning
│   ├── README.md
│   ├── main.tf
│   ├── variables.tf
│   ├── outputs.tf
│   ├── versions.tf
│   ├── cloud-config.yaml
│   └── scripts/
│       ├── provision.sh
│       ├── configure-hosts.sh
│       ├── reset-vm.sh
│       └── lock.sh
│
└── playbook/
    └── visual-verification.md
```

## Unit Test Specifications

### tests/unit/conftest.py

```python
# Key fixtures:
# - temp_config_file: Path to temporary config file
# - valid_config_dict: Valid configuration dictionary
# - temp_config_with_content: Config file with valid YAML
# - mock_subprocess: Mocked asyncio.create_subprocess_shell
# - mock_job_context: JobContext with mocked executors
# - mock_event_bus: Mocked EventBus
```

### tests/unit/test_config.py

| Test Class | Test Methods |
|------------|--------------|
| `TestConfigurationFromYaml` | `test_load_valid_minimal_config`, `test_load_valid_full_config`, `test_file_not_found_raises`, `test_yaml_syntax_error`, `test_invalid_schema_rejects`, `test_invalid_log_level`, `test_disk_config_defaults`, `test_btrfs_config_defaults`, `test_job_configs_extracted`, `test_unknown_sync_job_rejected` |
| `TestDiskConfig` | `test_default_values`, `test_custom_values` |
| `TestBtrfsConfig` | `test_default_subvolumes`, `test_custom_subvolumes` |

### tests/unit/test_models.py

| Test Class | Test Methods |
|------------|--------------|
| `TestHost` | `test_source_value`, `test_target_value` |
| `TestLogLevel` | `test_ordering`, `test_comparison` |
| `TestCommandResult` | `test_success_true_on_zero`, `test_success_false_on_nonzero` |
| `TestProgressUpdate` | `test_valid_percent`, `test_heartbeat_default` |
| `TestSnapshot` | `test_name_property_format`, `test_from_path_valid`, `test_from_path_invalid_raises` |
| `TestJobResult` | `test_creation`, `test_duration_calculation` |
| `TestSyncSession` | `test_creation`, `test_status_values` |

### tests/unit/test_events.py

| Test Class | Test Methods |
|------------|--------------|
| `TestEventBus` | `test_subscribe_returns_queue`, `test_publish_to_all_subscribers`, `test_close_sends_sentinel`, `test_publish_after_close_ignored`, `test_multiple_subscribers_isolated` |
| `TestLogEvent` | `test_creation`, `test_frozen_immutable` |
| `TestProgressEvent` | `test_creation` |

### tests/unit/test_disk.py

| Test Class | Test Methods |
|------------|--------------|
| `TestParseThreshold` | `test_percentage_format`, `test_gib_format`, `test_mib_format`, `test_gb_format`, `test_mb_format`, `test_invalid_format_raises`, `test_zero_percent`, `test_large_value` |
| `TestParseDfOutput` | `test_parses_valid_output`, `test_returns_none_for_missing_mount`, `test_handles_multiline_output`, `test_handles_long_device_names` |
| `TestDiskSpace` | `test_frozen_immutable`, `test_all_fields_populated` |
| `TestCheckDiskSpaceLocal` | `test_success_with_local_executor`, `test_failure_raises_runtime_error` |

### tests/unit/test_btrfs_snapshots.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSnapshotName` | `test_pre_phase_format`, `test_post_phase_format`, `test_includes_timestamp` |
| `TestSessionFolderName` | `test_format`, `test_includes_session_id` |
| `TestParseOlderThan` | `test_days_format`, `test_weeks_format`, `test_hours_format`, `test_invalid_raises` |

### tests/unit/test_version.py (extend existing)

| Test Class | Test Methods |
|------------|--------------|
| `TestGetThisVersion` | (existing tests) |
| `TestParseVersionFromCliOutput` | `test_parse_simple_version`, `test_parse_prefixed_version`, `test_parse_dev_version`, `test_parse_with_newline`, `test_invalid_format_raises` |

### tests/unit/test_lock.py

| Test Class | Test Methods |
|------------|--------------|
| `TestGetLocalHostname` | `test_returns_string`, `test_returns_socket_gethostname` |
| `TestSyncLock` | `test_acquire_creates_file`, `test_release_removes_file`, `test_get_holder_info` |

### tests/unit/test_logger.py

| Test Class | Test Methods |
|------------|--------------|
| `TestGenerateLogFilename` | `test_format_includes_session_id`, `test_format_includes_timestamp` |
| `TestGetLogsDirectory` | `test_returns_correct_path` |
| `TestLogger` | `test_log_publishes_event`, `test_log_with_context` |

### tests/unit/test_executor.py

| Test Class | Test Methods |
|------------|--------------|
| `TestLocalExecutor` | `test_run_command_success`, `test_run_command_failure`, `test_run_command_timeout`, `test_start_process_returns_handle`, `test_terminate_all_processes` |

### tests/unit/test_ui.py

| Test Class | Test Methods |
|------------|--------------|
| `TestTerminalUI` | `test_set_current_step`, `test_start_and_stop` |
| `TestUIEventConsumption` | `test_consumes_log_events`, `test_consumes_progress_events`, `test_consumes_connection_events`, `test_respects_log_level_filter`, `test_stops_on_sentinel` |

### tests/unit/test_jobs/test_base.py

| Test Class | Test Methods |
|------------|--------------|
| `TestJobValidateConfig` | `test_empty_schema_accepts_any`, `test_schema_validates_required`, `test_schema_validates_types`, `test_errors_include_job_name` |
| `TestJobHelpers` | `test_validation_error_creates_correct_type`, `test_log_publishes_to_event_bus`, `test_report_progress_publishes_event` |

### tests/unit/test_jobs/test_btrfs.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSubvolumeToMountPoint` | `test_root_subvolume`, `test_home_subvolume`, `test_var_subvolume`, `test_invalid_name_raises` |
| `TestBtrfsSnapshotJobConfigSchema` | `test_requires_phase`, `test_requires_subvolumes`, `test_requires_session_folder`, `test_valid_config_passes`, `test_invalid_phase_rejected` |

### tests/unit/test_jobs/test_disk_space_monitor.py (extend existing)

| Test Class | Test Methods |
|------------|--------------|
| `TestDiskSpaceMonitorConfigSchema` | (existing tests) |
| `TestDiskSpaceMonitorValidateConfig` | (existing tests) |
| `TestDiskSpaceMonitorValidation` | (existing tests) |
| `TestDiskSpaceMonitorExecution` | `test_monitors_at_interval`, `test_logs_warning_at_threshold`, `test_raises_critical_below_minimum` |

### tests/unit/test_jobs/test_install_on_target.py

| Test Class | Test Methods |
|------------|--------------|
| `TestInstallOnTargetJobValidate` | `test_returns_empty_when_target_older`, `test_returns_empty_when_target_missing`, `test_returns_error_when_target_newer` |
| `TestInstallOnTargetJobExecute` | `test_skips_when_versions_match`, `test_installs_when_missing`, `test_upgrades_when_older` |

### tests/unit/test_jobs/test_dummy_success.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDummySuccessJobConfigSchema` | `test_schema_has_duration_fields`, `test_valid_config_passes`, `test_default_durations` |

### tests/unit/test_jobs/test_dummy_fail.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDummyFailJobConfigSchema` | `test_schema_has_fail_at_percent`, `test_valid_config_passes`, `test_default_fail_percent` |

## Integration Test Specifications

### tests/integration/conftest.py

```python
# Key fixtures:
# - integration_lock: Acquires lock for test session
# - reset_vms: Resets VMs to baseline at session start
# - event_bus: Real EventBus instance
# - local_executor: Real LocalExecutor
# - ssh_connection: Real asyncssh connection to target
# - remote_executor: Real RemoteExecutor
# - test_session_id: Unique session ID for test isolation
# - cleanup_snapshots: Cleanup fixture for snapshot tests

# pytest markers:
# @pytest.mark.integration - marks tests requiring VMs
```

### tests/integration/test_connection.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSSHConnection` | `test_connect_success`, `test_disconnect`, `test_run_command_on_target`, `test_keepalive_works`, `test_connection_loss_detection` |

### tests/integration/test_executor.py

| Test Class | Test Methods |
|------------|--------------|
| `TestLocalExecutorReal` | `test_run_command_real_success`, `test_run_command_real_failure`, `test_run_command_with_timeout`, `test_process_tracking` |
| `TestRemoteExecutorReal` | `test_run_command_on_target`, `test_run_command_real_failure`, `test_run_command_with_timeout`, `test_send_file`, `test_get_file`, `test_terminate_all_processes` |

### tests/integration/test_lock.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSyncLockReal` | `test_acquire_and_release`, `test_concurrent_access_blocked`, `test_holder_info_written`, `test_stale_lock_handling` |
| `TestTargetLock` | `test_acquire_target_lock`, `test_release_target_lock`, `test_concurrent_target_lock_blocked` |
| `TestLockChainBlocking` | `test_sync_a_to_b_blocks_sync_b_to_c` (while A→B sync is running, B→C sync should be blocked because B has source lock held) |

### tests/integration/test_disk.py

| Test Class | Test Methods |
|------------|--------------|
| `TestCheckDiskSpaceRemote` | `test_check_disk_space_on_target`, `test_returns_valid_disk_space` |

### tests/integration/test_btrfs_snapshots.py

| Test Class | Test Methods |
|------------|--------------|
| `TestCreateSnapshot` | `test_creates_readonly_snapshot`, `test_snapshot_path_correct` |
| `TestValidateSnapshotsDirectory` | `test_creates_if_missing`, `test_succeeds_if_exists` |
| `TestValidateSubvolumeExists` | `test_root_subvolume_exists`, `test_home_subvolume_exists`, `test_invalid_subvolume_fails` |
| `TestListSnapshots` | `test_lists_created_snapshots`, `test_empty_when_none` |
| `TestCleanupSnapshots` | `test_keeps_recent`, `test_deletes_old`, `test_respects_max_age` |

### tests/integration/test_logger.py

Note: These tests don't require VMs but are placed in integration/ because they test real file I/O and the full logging pipeline. They can run on any machine.

| Test Class | Test Methods |
|------------|--------------|
| `TestFileLoggerReal` | `test_creates_log_file`, `test_writes_json_lines`, `test_respects_log_level`, `test_aggregates_source_and_target_logs` |

### tests/integration/test_jobs/test_btrfs.py

| Test Class | Test Methods |
|------------|--------------|
| `TestBtrfsSnapshotJobReal` | `test_validate_success`, `test_validate_missing_subvolume`, `test_execute_creates_snapshots`, `test_execute_on_both_hosts` |

### tests/integration/test_jobs/test_install_on_target.py

| Test Class | Test Methods |
|------------|--------------|
| `TestInstallOnTargetJobReal` | `test_version_check_success`, `test_installs_when_missing`, `test_upgrades_when_older`, `test_skips_when_matching`, `test_errors_when_target_newer` (target version > source version should abort with CRITICAL) |

### tests/integration/test_jobs/test_disk_space_monitor.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDiskSpaceMonitorJobReal` | `test_monitors_source`, `test_monitors_target`, `test_logs_warning_at_threshold` |

### tests/integration/test_jobs/test_dummy_success.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDummySuccessJobReal` | `test_full_execution`, `test_logs_at_correct_levels`, `test_reports_progress`, `test_runs_on_both_hosts` |

### tests/integration/test_jobs/test_dummy_fail.py

| Test Class | Test Methods |
|------------|--------------|
| `TestDummyFailJobReal` | `test_raises_at_configured_percent`, `test_orchestrator_catches_exception`, `test_logs_critical` |

### tests/integration/test_orchestrator.py

| Test Class | Test Methods |
|------------|--------------|
| `TestOrchestratorFullWorkflow` | `test_complete_sync_success`, `test_sync_with_validation_failure`, `test_sync_with_job_failure`, `test_all_phases_execute_in_order`, `test_cleanup_on_failure` |
| `TestOrchestratorJobDiscovery` | `test_discovers_enabled_jobs`, `test_skips_disabled_jobs`, `test_rejects_unknown_jobs` |
| `TestOrchestratorTermination` | `test_job_cleanup_timeout_triggers_force_kill` (when job doesn't cleanup within timeout, orchestrator force-kills processes) |
| `TestOrchestratorNetworkFailure` | `test_target_unreachable_mid_sync` (simulate network outage via iptables, verify CRITICAL log and abort) |

### tests/integration/test_cli.py

| Test Class | Test Methods |
|------------|--------------|
| `TestSyncCommand` | `test_sync_success`, `test_sync_target_unreachable` |
| `TestSigintHandling` | `test_single_sigint_graceful`, `test_double_sigint_force`, `test_no_orphaned_processes` |
| `TestInitCommand` | `test_creates_config_file`, `test_preserves_existing` |
| `TestLogsCommand` | `test_list_logs`, `test_show_last_log` |

### tests/integration/test_cleanup_snapshots.py

| Test Class | Test Methods |
|------------|--------------|
| `TestCleanupSnapshotsCommand` | `test_cleanup_deletes_old`, `test_cleanup_keeps_recent`, `test_dry_run_no_changes` |

### tests/integration/test_install_script.py

| Test Class | Test Methods |
|------------|--------------|
| `TestInstallScript` | `test_fresh_install`, `test_upgrade_install`, `test_config_preserved_on_upgrade`, `test_installs_uv_if_missing`, `test_installs_btrfs_progs` |

## Infrastructure Configuration

### tests/infrastructure/main.tf

```hcl
terraform {
  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = ">= 1.57.0"
    }
  }

  # State is stored in Hetzner Object Storage (S3-compatible)
  # Configure via environment variables or backend config file
  backend "s3" {
    bucket                      = "pc-switcher-tfstate"
    key                         = "test-infrastructure/terraform.tfstate"
    region                      = "eu-central-1"
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
    # endpoints configured via env: AWS_ENDPOINT_URL_S3
  }
}

provider "hcloud" {
  token = var.hcloud_token
}

resource "hcloud_ssh_key" "test_key" {
  name       = "pc-switcher-test-key"
  public_key = file(var.ssh_public_key_path)
}

resource "hcloud_server" "pc1" {
  name        = "pc-switcher-pc1-test"
  server_type = "cx23"
  image       = "ubuntu-24.04"
  location    = "fsn1"
  ssh_keys    = [hcloud_ssh_key.test_key.id]
  user_data   = file("${path.module}/cloud-config.yaml")

  labels = {
    project = "pc-switcher"
    role    = "pc1"
  }
}

resource "hcloud_server" "pc2" {
  name        = "pc-switcher-pc2-test"
  server_type = "cx23"
  image       = "ubuntu-24.04"
  location    = "fsn1"
  ssh_keys    = [hcloud_ssh_key.test_key.id]
  user_data   = file("${path.module}/cloud-config.yaml")

  labels = {
    project = "pc-switcher"
    role    = "pc2"
  }
}

output "pc1_ip" {
  value = hcloud_server.pc1.ipv4_address
}

output "pc2_ip" {
  value = hcloud_server.pc2.ipv4_address
}
```

### tests/infrastructure/cloud-config.yaml

Minimal cloud-config for initial SSH access. The actual btrfs and user configuration is done by `provision.sh` using Hetzner's installimage.

```yaml
#cloud-config
# Minimal config for initial boot - provision.sh does the real setup

# Disable password authentication for security
ssh_pwauth: false
```

Note: This cloud-config is intentionally minimal because `provision.sh` will reinstall the OS with btrfs using installimage, which wipes everything. The cloud-config only ensures the VM is accessible via SSH (using the SSH key configured in OpenTofu) so provision.sh can connect.

### tests/infrastructure/scripts/lock.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
LOCK_FILE="/tmp/pc-switcher-integration-test.lock"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME <holder> <acquire|release>

Manage integration test lock to prevent concurrent test runs.

Arguments:
  holder    Identifier for lock holder (e.g., CI job ID or username)
  action    One of: acquire, release

Examples:
  $SCRIPT_NAME github-123456 acquire
  $SCRIPT_NAME \$USER release

Lock file: $LOCK_FILE
EOF
}

# Handle help flags
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 2 ]]; then
    show_help
    [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && exit 0
    exit 1
fi

LOCK_HOLDER="$1"
ACTION="$2"

acquire_lock() {
    local max_wait=300
    local waited=0

    while true; do
        if mkdir "$LOCK_FILE" 2>/dev/null; then
            echo "$LOCK_HOLDER" > "$LOCK_FILE/holder"
            echo "Lock acquired by $LOCK_HOLDER"
            return 0
        fi

        if [[ $waited -ge $max_wait ]]; then
            echo "Failed to acquire lock after ${max_wait}s" >&2
            echo "Current holder: $(cat "$LOCK_FILE/holder" 2>/dev/null || echo 'unknown')" >&2
            return 1
        fi

        sleep 5
        waited=$((waited + 5))
    done
}

release_lock() {
    if [[ -d "$LOCK_FILE" ]]; then
        holder=$(cat "$LOCK_FILE/holder" 2>/dev/null || echo "unknown")
        if [[ "$holder" == "$LOCK_HOLDER" ]]; then
            rm -rf "$LOCK_FILE"
            echo "Lock released by $LOCK_HOLDER"
        else
            echo "Lock held by $holder, not releasing" >&2
        fi
    fi
}

case "$ACTION" in
    acquire) acquire_lock ;;
    release) release_lock ;;
    *) show_help; exit 1 ;;
esac
```

### tests/infrastructure/scripts/reset-vm.sh

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
USER="${PC_SWITCHER_TEST_USER:-testuser}"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME <hostname>

Reset a test VM to its baseline btrfs snapshot state.

This script:
  1. Cleans up test artifacts in /.snapshots/pc-switcher/
  2. Creates fresh r/w snapshots from baseline-root and baseline-home
  3. Sets the new root snapshot as default boot target
  4. Reboots the VM and waits for it to come back online

Arguments:
  hostname    SSH hostname of the VM to reset (e.g., pc1-test, pc2-test)

Environment:
  PC_SWITCHER_TEST_USER    SSH user (default: testuser)

Examples:
  $SCRIPT_NAME pc1-test
  $SCRIPT_NAME pc2-test
EOF
}

# Handle help flags
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
    show_help
    [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && exit 0
    exit 1
fi

HOST="$1"

echo "Resetting VM: $HOST"

# Reset to baseline snapshots
ssh "$USER@$HOST" << 'EOF'
    set -euo pipefail

    # Clean up test artifacts (keep baseline snapshots)
    echo "Cleaning up test artifacts..."
    sudo rm -rf /.snapshots/pc-switcher/test-* 2>/dev/null || true

    # Delete any previous reset snapshots
    sudo btrfs subvolume delete /.snapshots/reset-root 2>/dev/null || true
    sudo btrfs subvolume delete /.snapshots/reset-home 2>/dev/null || true

    # Create fresh r/w snapshots from baselines
    echo "Creating fresh snapshots from baseline..."
    sudo btrfs subvolume snapshot /.snapshots/baseline-root /.snapshots/reset-root
    sudo btrfs subvolume snapshot /.snapshots/baseline-home /.snapshots/reset-home

    # Get new root snapshot ID and set as default
    RESET_ROOT_ID=$(sudo btrfs subvolume list / | grep '/.snapshots/reset-root$' | awk '{print $2}')
    if [[ -n "$RESET_ROOT_ID" ]]; then
        sudo btrfs subvolume set-default "$RESET_ROOT_ID" /
        echo "Set reset-root as default boot target"
    else
        echo "ERROR: Could not find reset-root snapshot" >&2
        exit 1
    fi

    # Update fstab to mount reset-home at /home (if using subvol= mount option)
    # This depends on the specific btrfs mount configuration

    echo "Rebooting..."
    sudo reboot
EOF

# Wait for VM to come back
echo "Waiting for $HOST to come back online..."
sleep 15
until ssh -o ConnectTimeout=5 -o BatchMode=yes "$USER@$HOST" true 2>/dev/null; do
    sleep 5
done

echo "VM $HOST is ready"
```

### tests/infrastructure/scripts/provision.sh

This script is run once per VM to completely wipe and install a new ubuntu 24.04 with btrfs filesystem using Hetzner's installimage in rescue mode:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME <server-name>

Provision a Hetzner VM with btrfs filesystem using installimage.

This script:
  1. Enables rescue mode on the server
  2. Reboots into rescue mode
  3. Runs installimage with btrfs configuration
  4. Configures subvolumes (@, @home, @snapshots)
  5. Creates testuser with SSH access and sudo
  6. Installs Hetzner cloud server equivalents:
     - QEMU Guest Agent (for Hetzner Cloud integration)
     - Hetzner Cloud Utils (hc-utils)
     - fail2ban (SSH brute-force protection)
     - SSH hardening (disable password auth, only testuser allowed)
     - unattended-upgrades (with automatic reboot)
     - ufw firewall (SSH only by default)
  7. Creates baseline snapshots for test reset

Arguments:
  server-name    Name of the Hetzner server (e.g., pc-switcher-pc1-test)

Environment:
  HCLOUD_TOKEN   Hetzner Cloud API token (required)
  SSH_PUBLIC_KEY Path to SSH public key (default: ~/.ssh/id_ed25519.pub)

Examples:
  $SCRIPT_NAME pc-switcher-pc1-test
  $SCRIPT_NAME pc-switcher-pc2-test

Note: This is a one-time operation. After provisioning, use reset-vm.sh
      for subsequent test runs.
EOF
}

# Handle help flags
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
    show_help
    [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && exit 0
    exit 1
fi

SERVER_NAME="$1"
SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY:-$HOME/.ssh/id_ed25519.pub}"

if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
    echo "ERROR: HCLOUD_TOKEN environment variable is required" >&2
    exit 1
fi

echo "Provisioning server: $SERVER_NAME"

# Get server ID
SERVER_ID=$(hcloud server list -o noheader -o columns=id,name | grep "$SERVER_NAME" | awk '{print $1}')
if [[ -z "$SERVER_ID" ]]; then
    echo "ERROR: Server '$SERVER_NAME' not found" >&2
    exit 1
fi

# Get server IP
SERVER_IP=$(hcloud server ip "$SERVER_NAME")
echo "Server IP: $SERVER_IP"

# Enable rescue mode
echo "Enabling rescue mode..."
hcloud server enable-rescue "$SERVER_NAME" --ssh-key pc-switcher-test-key

# Reboot into rescue
echo "Rebooting into rescue mode..."
hcloud server reboot "$SERVER_NAME"

# Wait for rescue mode
echo "Waiting for rescue mode (this may take a minute)..."
sleep 30
# Remove old host key if exists, because rescue mode uses a different host key
ssh-keygen -R "$SERVER_IP" 2>/dev/null || true
# Wait until we can connect via SSH (accept-new auto-adds the host key)
until ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new root@"$SERVER_IP" true 2>/dev/null; do
    sleep 5
done

echo "Connected to rescue mode"

# Create installimage config
cat << INSTALLCONFIG | ssh root@"$SERVER_IP" "cat > /autosetup"
DRIVE1 /dev/sda
USE_KERNEL_MODE_SETTING
HOSTNAME $SERVER_NAME
PART /boot/efi ext4 128M
PART btrfs.1 btrfs all
SUBVOL btrfs.1 @ /
SUBVOL btrfs.1 @home /home
SUBVOL btrfs.1 @snapshots /.snapshots
IMAGE /root/.oldroot/nfs/images/Ubuntu-2404-noble-amd64-base.tar.gz
INSTALLCONFIG

# Run installimage
echo "Running installimage (this takes several minutes)..."
ssh root@"$SERVER_IP" "installimage -a -c /autosetup"

# Reboot into new system
echo "Rebooting into new system..."
ssh root@"$SERVER_IP" "reboot" || true

# Wait for new system and update known_hosts
echo "Waiting for system to come online..."
sleep 60
ssh-keygen -R "$SERVER_IP" 2>/dev/null || true
until ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new root@"$SERVER_IP" true 2>/dev/null; do
    sleep 10
done

echo "System online, configuring..."

# Copy and run the configuration script
SCRIPT_DIR="$(dirname "$0")"
scp "$SCRIPT_DIR/configure-vm.sh" root@"$SERVER_IP":/tmp/
ssh root@"$SERVER_IP" "bash /tmp/configure-vm.sh '$(cat "$SSH_PUBLIC_KEY")'"
ssh root@"$SERVER_IP" "rm /tmp/configure-vm.sh"

echo "Server $SERVER_NAME provisioned successfully"
echo ""
echo "IMPORTANT: After provisioning both VMs, run configure-hosts.sh to set up /etc/hosts"
```

### tests/infrastructure/scripts/configure-vm.sh

This script is copied to and executed on the VM by provision.sh. It configures the system with required packages, security hardening, and creates the testuser:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Called by provision.sh with SSH public key as argument
SSH_PUBLIC_KEY="$1"

# Install required packages
apt-get update
apt-get install -y btrfs-progs

# ========================================
# Hetzner cloud server equivalents
# (installimage doesn't include these by default)
# ========================================

# Install QEMU Guest Agent for Hetzner Cloud integration
apt-get install -y qemu-guest-agent
systemctl enable qemu-guest-agent
systemctl start qemu-guest-agent

# Install Hetzner Cloud Utils
curl -s https://packages.hetzner.com/hcloud/deb/hc-utils_0.0.7-1%2Bnoble_all.deb -o /tmp/hc-utils.deb
apt-get install -y /tmp/hc-utils.deb
rm /tmp/hc-utils.deb

# Install and configure fail2ban for SSH brute-force protection
apt-get install -y fail2ban
cat > /etc/fail2ban/jail.local << 'FAIL2BAN'
[DEFAULT]
bantime = 10m
findtime = 10m
maxretry = 5
# Ubuntu 24.04 may not have rsyslog, use systemd journal
backend = systemd

[sshd]
enabled = true
port = ssh
banaction = iptables-multiport
FAIL2BAN
systemctl enable fail2ban
systemctl restart fail2ban

# SSH hardening
cat > /etc/ssh/sshd_config.d/99-hardening.conf << 'SSHCONF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
MaxAuthTries 2
AllowTcpForwarding no
X11Forwarding no
AllowAgentForwarding no
AuthorizedKeysFile .ssh/authorized_keys
AllowUsers testuser
SSHCONF
systemctl restart ssh

# Install and configure ufw firewall
apt-get install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

# Install and configure unattended-upgrades with automatic reboot
apt-get install -y unattended-upgrades update-notifier-common
cat > /etc/apt/apt.conf.d/51unattended-upgrades-custom << 'UNATTENDED'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "now";
UNATTENDED
systemctl enable unattended-upgrades

# Create testuser with sudo and SSH access
useradd -m -s /bin/bash testuser
echo "testuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/testuser
chmod 440 /etc/sudoers.d/testuser

mkdir -p /home/testuser/.ssh
echo "$SSH_PUBLIC_KEY" > /home/testuser/.ssh/authorized_keys
chmod 700 /home/testuser/.ssh
chmod 600 /home/testuser/.ssh/authorized_keys
chown -R testuser:testuser /home/testuser/.ssh

# Clean up
apt-get autoremove -y
apt-get clean

# Create baseline snapshots for test reset
btrfs subvolume snapshot -r / /.snapshots/baseline-root
btrfs subvolume snapshot -r /home /.snapshots/baseline-home

echo "VM configuration complete!"
```

### tests/infrastructure/scripts/configure-hosts.sh

Run this after both VMs are provisioned to configure /etc/hosts with correct IPs:

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"

show_help() {
    cat << EOF
Usage: $SCRIPT_NAME

Configure networking and SSH keys on both test VMs.

This script:
  1. Gets the IPs of both VMs from Hetzner
  2. Updates /etc/hosts on both VMs with pc1-test/pc2-test entries
  3. Generates SSH keys for testuser on each VM (if not present)
  4. Exchanges SSH public keys so testuser can SSH between VMs

Environment:
  HCLOUD_TOKEN   Hetzner Cloud API token (required)

Run this after provisioning both VMs with provision.sh.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    show_help
    exit 0
fi

if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
    echo "ERROR: HCLOUD_TOKEN environment variable is required" >&2
    exit 1
fi

PC1_IP=$(hcloud server ip pc-switcher-pc1-test)
PC2_IP=$(hcloud server ip pc-switcher-pc2-test)

echo "PC1 IP: $PC1_IP"
echo "PC2 IP: $PC2_IP"

# Configure /etc/hosts on both VMs
for HOST in "$PC1_IP" "$PC2_IP"; do
    echo "Configuring /etc/hosts on $HOST..."
    ssh testuser@"$HOST" << EOF
# Remove old entries
sudo sed -i '/pc1-test/d' /etc/hosts
sudo sed -i '/pc2-test/d' /etc/hosts

# Add new entries
echo "$PC1_IP pc1-test" | sudo tee -a /etc/hosts > /dev/null
echo "$PC2_IP pc2-test" | sudo tee -a /etc/hosts > /dev/null
EOF
done

echo "Hosts configured"

# Generate SSH keys for testuser on each VM (if not present)
for HOST in "$PC1_IP" "$PC2_IP"; do
    echo "Generating SSH key on $HOST..."
    ssh testuser@"$HOST" << 'EOF'
if [[ ! -f ~/.ssh/id_ed25519 ]]; then
    ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
    echo "SSH key generated"
else
    echo "SSH key already exists"
fi
EOF
done

# Exchange SSH public keys between VMs
echo "Exchanging SSH keys..."

PC1_PUBKEY=$(ssh testuser@"$PC1_IP" cat ~/.ssh/id_ed25519.pub)
PC2_PUBKEY=$(ssh testuser@"$PC2_IP" cat ~/.ssh/id_ed25519.pub)

# Add pc1's key to pc2's authorized_keys
echo "Adding pc1 key to pc2..."
ssh testuser@"$PC2_IP" << EOF
grep -qF "$PC1_PUBKEY" ~/.ssh/authorized_keys 2>/dev/null || echo "$PC1_PUBKEY" >> ~/.ssh/authorized_keys
EOF

# Add pc2's key to pc1's authorized_keys
echo "Adding pc2 key to pc1..."
ssh testuser@"$PC1_IP" << EOF
grep -qF "$PC2_PUBKEY" ~/.ssh/authorized_keys 2>/dev/null || echo "$PC2_PUBKEY" >> ~/.ssh/authorized_keys
EOF

# Set up known_hosts for VM-to-VM SSH (testuser)
echo "Configuring known_hosts for VM-to-VM SSH..."

# Fetch host keys
PC1_HOSTKEY=$(ssh-keyscan -H "$PC1_IP" 2>/dev/null)
PC2_HOSTKEY=$(ssh-keyscan -H "$PC2_IP" 2>/dev/null)

# Add pc2's host key to pc1's known_hosts
ssh testuser@"$PC1_IP" << EOF
echo "$PC2_HOSTKEY" >> ~/.ssh/known_hosts
EOF

# Add pc1's host key to pc2's known_hosts
ssh testuser@"$PC2_IP" << EOF
echo "$PC1_HOSTKEY" >> ~/.ssh/known_hosts
EOF

echo ""
echo "Done! testuser can now:"
echo "  - SSH from pc1 to pc2: ssh pc2-test"
echo "  - SSH from pc2 to pc1: ssh pc1-test"
```

## GitHub Actions Workflow

### .github/workflows/test.yml

```yaml
name: Tests

on:
  push:
    branches: ['**']  # All branches
  pull_request:
    branches: [main]
  workflow_dispatch:
    inputs:
      run_integration:
        description: 'Run integration tests'
        type: boolean
        default: false

env:
  UV_VERSION: "latest"

jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          version: ${{ env.UV_VERSION }}
      # uv run automatically installs Python and syncs dependencies
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run basedpyright
      - run: uv run codespell

  unit-tests:
    name: Unit Tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: astral-sh/setup-uv@v6
        with:
          version: ${{ env.UV_VERSION }}
      - run: uv run pytest tests/unit tests/contract -v --tb=short
      - name: Coverage
        run: uv run pytest tests/unit tests/contract --cov=src/pcswitcher --cov-report=xml
      - uses: codecov/codecov-action@v4
        with:
          files: coverage.xml
        if: always()

  integration-tests:
    name: Integration Tests
    runs-on: ubuntu-latest
    if: |
      github.event_name == 'pull_request' && github.base_ref == 'main' ||
      github.event.inputs.run_integration == 'true'
    needs: [lint, unit-tests]
    concurrency:
      group: pc-switcher-integration
      cancel-in-progress: false

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: astral-sh/setup-uv@v6
        with:
          version: ${{ env.UV_VERSION }}

      - uses: opentofu/setup-opentofu@v1
        with:
          tofu_version: "1.10.7"

      - name: Setup SSH key
        run: |
          mkdir -p ~/.ssh
          echo "${{ secrets.HETZNER_SSH_PRIVATE_KEY }}" > ~/.ssh/id_ed25519
          chmod 600 ~/.ssh/id_ed25519
          ssh-keyscan -H ${{ secrets.PC1_TEST_HOST }} >> ~/.ssh/known_hosts
          ssh-keyscan -H ${{ secrets.PC2_TEST_HOST }} >> ~/.ssh/known_hosts

      - name: Configure Terraform backend
        run: |
          # Configure S3 backend for Hetzner Object Storage
          export AWS_ACCESS_KEY_ID="${{ secrets.HETZNER_S3_ACCESS_KEY }}"
          export AWS_SECRET_ACCESS_KEY="${{ secrets.HETZNER_S3_SECRET_KEY }}"
          export AWS_ENDPOINT_URL_S3="https://fsn1.your-objectstorage.com"
        working-directory: tests/infrastructure

      - name: Provision VMs (if needed)
        working-directory: tests/infrastructure
        run: |
          tofu init
          tofu apply -auto-approve
        env:
          TF_VAR_hcloud_token: ${{ secrets.HCLOUD_TOKEN }}
          AWS_ACCESS_KEY_ID: ${{ secrets.HETZNER_S3_ACCESS_KEY }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.HETZNER_S3_SECRET_KEY }}
          AWS_ENDPOINT_URL_S3: https://fsn1.your-objectstorage.com

      - name: Reset VMs
        run: |
          ./tests/infrastructure/scripts/reset-vm.sh ${{ secrets.PC1_TEST_HOST }}
          ./tests/infrastructure/scripts/reset-vm.sh ${{ secrets.PC2_TEST_HOST }}

      - name: Run integration tests
        run: uv run pytest tests/integration -v -m integration --tb=short
        env:
          PC_SWITCHER_TEST_PC1_HOST: ${{ secrets.PC1_TEST_HOST }}
          PC_SWITCHER_TEST_PC2_HOST: ${{ secrets.PC2_TEST_HOST }}
          PC_SWITCHER_TEST_USER: testuser
          CI_JOB_ID: ${{ github.run_id }}
```

### Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `HCLOUD_TOKEN` | Hetzner Cloud API token |
| `HETZNER_SSH_PRIVATE_KEY` | SSH private key for VM access |
| `HETZNER_S3_ACCESS_KEY` | Hetzner Object Storage access key (for tfstate) |
| `HETZNER_S3_SECRET_KEY` | Hetzner Object Storage secret key (for tfstate) |
| `PC1_TEST_HOST` | IP/hostname of pc1 test VM |
| `PC2_TEST_HOST` | IP/hostname of pc2 test VM |

## Implementation Order

Tasks within the same phase can be implemented **in parallel** by multiple agents. Dependencies are noted where they exist.

### Phase 1: Unit Test Foundation

**Can run in parallel:**
1. Update `tests/conftest.py` with shared fixtures
2. Create `tests/unit/conftest.py` (depends on 1)

**Can run in parallel after 1-2:**
3. Implement `tests/unit/test_config.py`
4. Implement `tests/unit/test_models.py`
5. Implement `tests/unit/test_events.py`
6. Implement `tests/unit/test_disk.py`
7. Implement `tests/unit/test_btrfs_snapshots.py`
8. Extend `tests/unit/test_version.py`
9. Implement `tests/unit/test_lock.py`
10. Implement `tests/unit/test_logger.py`
11. Implement `tests/unit/test_executor.py`
12. Implement `tests/unit/test_ui.py`

### Phase 2: Unit Tests for Jobs

**Can run in parallel (all independent):**
13. Implement `tests/unit/test_jobs/test_base.py`
14. Implement `tests/unit/test_jobs/test_btrfs.py`
15. Extend `tests/unit/test_jobs/test_disk_space_monitor.py`
16. Implement `tests/unit/test_jobs/test_install_on_target.py`
17. Implement `tests/unit/test_jobs/test_dummy_success.py`
18. Implement `tests/unit/test_jobs/test_dummy_fail.py`

### Phase 3: Infrastructure Setup

**Can run in parallel (all independent):**
19. Create `tests/infrastructure/README.md`
20. Create `tests/infrastructure/main.tf`
21. Create `tests/infrastructure/variables.tf`
22. Create `tests/infrastructure/outputs.tf`
23. Create `tests/infrastructure/versions.tf`
24. Create `tests/infrastructure/cloud-config.yaml`
25. Create `tests/infrastructure/scripts/lock.sh`
26. Create `tests/infrastructure/scripts/reset-vm.sh`
27. Create `tests/infrastructure/scripts/provision.sh`
28. Create `tests/infrastructure/scripts/configure-hosts.sh`

### Phase 4: Integration Tests

**Sequential (conftest first):**
29. Create `tests/integration/conftest.py`

**Can run in parallel after 29:**
30. Implement `tests/integration/test_connection.py`
31. Implement `tests/integration/test_executor.py`
32. Implement `tests/integration/test_lock.py`
33. Implement `tests/integration/test_disk.py`
34. Implement `tests/integration/test_btrfs_snapshots.py`
35. Implement `tests/integration/test_logger.py`

### Phase 5: Integration Tests for Jobs

**Can run in parallel (all independent):**
36. Implement `tests/integration/test_jobs/test_btrfs.py`
37. Implement `tests/integration/test_jobs/test_install_on_target.py`
38. Implement `tests/integration/test_jobs/test_disk_space_monitor.py`
39. Implement `tests/integration/test_jobs/test_dummy_success.py`
40. Implement `tests/integration/test_jobs/test_dummy_fail.py`

### Phase 6: Full System Tests

**Can run in parallel (all independent):**
41. Implement `tests/integration/test_orchestrator.py`
42. Implement `tests/integration/test_cli.py`
43. Implement `tests/integration/test_cleanup_snapshots.py`
44. Implement `tests/integration/test_install_script.py`

### Phase 7: CI/CD & Finalization

**Sequential:**
45. Create `.github/workflows/test.yml`
46. Create `specs/001-foundation/testing-playbook.md`
47. Update `pyproject.toml` with freezegun and pytest-cov dependencies
