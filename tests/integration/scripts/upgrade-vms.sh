#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s inherit_errexit

# Upgrade apt packages on test VMs and update baseline snapshots.
# This script:
#   1. Acquires lock to prevent concurrent test runs
#   2. Resets VMs to baseline state
#   3. Upgrades all apt packages
#   4. Detects if reboot is needed
#   5. Reboots if necessary
#   6. Updates baseline snapshots (only if changes occurred)
#   7. Releases lock
#
# Usage: ./upgrade-vms.sh
#
# Environment Variables:
#   HCLOUD_TOKEN           (required) Hetzner Cloud API token
#   PC_SWITCHER_TEST_USER  SSH user for VM access (default: testuser)
#   CI_JOB_ID              GitLab CI job ID (for lock holder identification)
#   GITHUB_RUN_ID          GitHub Actions run ID (for lock holder identification)

# Source common helpers
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/internal/common.sh"
trap 'log_error "Unhandled error on line $LINENO"; exit 1' ERR

# Help
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<EOF
Usage: $(basename "$0")

Upgrade apt packages on test VMs and update baseline snapshots.

This script:
  1. Acquires lock to prevent concurrent integration tests
  2. Resets VMs to baseline state (removes test artifacts)
  3. Upgrades all apt packages non-interactively
  4. Runs apt-get autoremove to clean unused dependencies
  5. Detects if reboot is required
  6. Reboots VMs if needed (in parallel)
  7. Clears apt cache
  8. Updates baseline snapshots (only if changes occurred)
  9. Releases lock

Lock Behavior:
  - Acquires exclusive lock to prevent concurrent tests
  - Waits up to 5 minutes if lock is held by another process
  - Automatically releases lock on success or error (via trap)

Upgrade Behavior:
  - Uses conservative 'apt-get upgrade' (won't remove packages)
  - Preserves existing configuration files
  - Removes unused dependencies with autoremove
  - Only updates baselines if changes occurred

Reboot Behavior:
  - Checks /var/run/reboot-required (Ubuntu standard)
  - Also checks if kernel was upgraded
  - Reboots independently per VM (asymmetric reboot OK)
  - Waits up to 5 minutes for each VM to come back online

Environment Variables:
  HCLOUD_TOKEN            (required) Hetzner Cloud API token
  PC_SWITCHER_TEST_USER   SSH user for VM access (default: testuser)
  CI_JOB_ID               GitLab CI job ID (for lock holder)
  GITHUB_RUN_ID           GitHub Actions run ID (for lock holder)

Prerequisites:
  - HCLOUD_TOKEN environment variable must be set
  - Both VMs (pc1, pc2) must exist in Hetzner Cloud
  - Both VMs must have baseline snapshots
  - hcloud CLI installed and configured
  - SSH keys configured for VM access

Example:
  export HCLOUD_TOKEN=your_token_here
  $(basename "$0")

EOF
    exit 0
fi

# Check required environment variable
: "${HCLOUD_TOKEN:?HCLOUD_TOKEN environment variable must be set}"

# Configuration
readonly SSH_USER="${PC_SWITCHER_TEST_USER:-testuser}"
readonly VM1_NAME="pc1"
readonly VM2_NAME="pc2"

# Global state
PC1_IP=""
PC2_IP=""
CHANGES_OCCURRED=false
TMPDIR=""

# Cleanup function - called on exit to release lock and clean temp files
cleanup() {
    local exit_code=$?

    if [[ -n "$TMPDIR" && -d "$TMPDIR" ]]; then
        rm -rf "$TMPDIR" 2>/dev/null || true
    fi

    exit $exit_code
}

# Setup trap to ensure lock is always released and temp files cleaned
trap cleanup EXIT INT TERM

# Detect if apt output shows changes
# Returns 0 if changes occurred, 1 if no changes
detect_apt_changes() {
    local output="$1"

    # apt outputs a summary line like: "5 upgraded, 2 newly installed, 0 to remove..."
    # No changes if ALL three are zero
    if echo "$output" | grep -qE "^0 upgraded, 0 newly installed, 0 to remove"; then
        return 1  # No changes
    fi

    return 0  # Changes occurred
}

# Upgrade packages on a single VM
# Outputs all apt output to stdout (for caller to prefix/display)
# Final line is "RESULT:CHANGES" or "RESULT:NO_CHANGES"
upgrade_vm() {
    local vm_name="$1"
    local vm_ip="$2"

    echo "=== Upgrading packages on $vm_name ($vm_ip) ==="

    # Update package lists
    echo "--- apt-get update ---"
    if ! ssh_run "${SSH_USER}@${vm_ip}" "sudo apt-get update" 2>&1; then
        echo "ERROR: Failed to update package lists on $vm_name"
        exit 1
    fi

    # Upgrade packages non-interactively
    echo "--- apt-get upgrade ---"
    local upgrade_output
    if ! upgrade_output=$(ssh_run "${SSH_USER}@${vm_ip}" \
        "DEBIAN_FRONTEND=noninteractive sudo apt-get upgrade -y \
         -o Dpkg::Options::='--force-confold' \
         -o Dpkg::Options::='--force-confdef' 2>&1"); then
        echo "$upgrade_output"
        echo "ERROR: Package upgrade failed on $vm_name"
        exit 1
    fi
    echo "$upgrade_output"

    # Run autoremove to clean unused dependencies
    echo "--- apt-get autoremove ---"
    local autoremove_output
    if ! autoremove_output=$(ssh_run "${SSH_USER}@${vm_ip}" \
        "DEBIAN_FRONTEND=noninteractive sudo apt-get autoremove -y 2>&1"); then
        echo "$autoremove_output"
        echo "ERROR: Autoremove failed on $vm_name"
        exit 1
    fi
    echo "$autoremove_output"

    # Detect if changes occurred (check each output separately)
    local has_changes=false
    if detect_apt_changes "$upgrade_output"; then
        has_changes=true
    fi
    if detect_apt_changes "$autoremove_output"; then
        has_changes=true
    fi

    if [[ "$has_changes" == "true" ]]; then
        echo "RESULT:CHANGES"
    else
        echo "RESULT:NO_CHANGES"
    fi
}

# Check if VM needs reboot
# Returns 0 if reboot needed, 1 if no reboot needed, 2 on error
check_reboot_required() {
    local vm_name="$1"
    local vm_ip="$2"

    log_info "Checking if $vm_name requires reboot..."

    # Check for reboot-required flag file (standard Ubuntu)
    # Use || status=$? to capture exit code immediately (POSIX: $? after if-fi is 0 if no branch executed)
    local status=0
    ssh_run "${SSH_USER}@${vm_ip}" "test -f /var/run/reboot-required" 2>/dev/null || status=$?

    if [[ $status -eq 0 ]]; then
        log_info "$vm_name: Reboot required (flag file exists)"
        return 0
    elif [[ $status -eq 1 ]]; then
        log_info "$vm_name: No reboot required"
        return 1
    else
        log_error "$vm_name: Failed to check reboot requirement (exit code: $status)"
        return 2
    fi
}

# Reboot a VM and wait for it to come back online
reboot_vm() {
    local vm_name="$1"
    local vm_ip="$2"

    log_step "Rebooting $vm_name..."

    # Initiate reboot (connection will drop, ignore exit code)
    ssh_run "${SSH_USER}@${vm_ip}" "sudo reboot" 2>/dev/null || true
    log_info "Reboot initiated"

    # Give VM time to actually go down
    sleep 5

    # Wait for SSH (no REMOVE_KEY - host key doesn't change on reboot)
    if ! wait_for_ssh "${SSH_USER}@${vm_ip}" 300; then
        log_error "Timeout waiting for $vm_name to come back online"
        return 1
    fi

    return 0
}

# Main execution
log_step "VM upgrade and baseline update"

acquire_lock "upgrade-vms"

# Get VM IP addresses (single API call)
log_step "Resolving VM IP addresses..."
if ! VM_LIST=$(hcloud server list -o columns=name,ipv4 -o noheader); then
    log_error "Failed to list VMs via hcloud"
    exit 1
fi
PC1_IP=$(echo "$VM_LIST" | awk -v name="$VM1_NAME" '$1 == name {print $2}')
PC2_IP=$(echo "$VM_LIST" | awk -v name="$VM2_NAME" '$1 == name {print $2}')

if [[ -z "$PC1_IP" ]]; then
    log_error "Failed to resolve IP for $VM1_NAME"
    exit 1
fi
if [[ -z "$PC2_IP" ]]; then
    log_error "Failed to resolve IP for $VM2_NAME"
    exit 1
fi

log_info "$VM1_NAME: $PC1_IP"
log_info "$VM2_NAME: $PC2_IP"

# Reset VMs to baseline in parallel
log_step "Resetting VMs to baseline state..."
LOG_PREFIX="$VM1_NAME:" "$SCRIPT_DIR/reset-vm.sh" "$PC1_IP" &
PID1=$!
LOG_PREFIX="$VM2_NAME:" "$SCRIPT_DIR/reset-vm.sh" "$PC2_IP" &
PID2=$!

# Use || EXIT=$? to prevent ERR trap firing (set +e disables errexit but not ERR trap)
EXIT1=0
wait $PID1 || EXIT1=$?
EXIT2=0
wait $PID2 || EXIT2=$?

if [[ $EXIT1 -ne 0 || $EXIT2 -ne 0 ]]; then
    log_error "VM reset failed"
    exit 1
fi
log_info "VMs reset to baseline state"

# Upgrade packages in parallel (output prefixed with VM name)
log_step "Upgrading packages on both VMs..."
TMPDIR=$(mktemp -d)

# Run upgrades in parallel, prefix each line with VM name
(
    trap '' ERR  # Disable inherited ERR trap, we handle errors explicitly
    set -o pipefail
    exit_code=0
    upgrade_vm "$VM1_NAME" "$PC1_IP" 2>&1 | sed "s/^/[$VM1_NAME] /" | tee "$TMPDIR/pc1.out" || exit_code=$?
    exit "$exit_code"
) &
PID1=$!
(
    trap '' ERR  # Disable inherited ERR trap, we handle errors explicitly
    set -o pipefail
    exit_code=0
    upgrade_vm "$VM2_NAME" "$PC2_IP" 2>&1 | sed "s/^/[$VM2_NAME] /" | tee "$TMPDIR/pc2.out" || exit_code=$?
    exit "$exit_code"
) &
PID2=$!

# Use || EXIT=$? to prevent ERR trap firing (set +e disables errexit but not ERR trap)
EXIT1=0
wait $PID1 || EXIT1=$?
EXIT2=0
wait $PID2 || EXIT2=$?

if [[ $EXIT1 -ne 0 || $EXIT2 -ne 0 ]]; then
    log_error "Package upgrade failed"
    exit 1
fi

log_info "Package upgrades complete"

# Parse results from output (look for RESULT: marker)
PC1_RESULT=$(grep "RESULT:" "$TMPDIR/pc1.out" | sed 's/.*RESULT://')
PC2_RESULT=$(grep "RESULT:" "$TMPDIR/pc2.out" | sed 's/.*RESULT://')

# Determine if changes occurred on either VM
if [[ "$PC1_RESULT" == "CHANGES" || "$PC2_RESULT" == "CHANGES" ]]; then
    CHANGES_OCCURRED=true
    log_info "Changes detected, proceeding with reboot check and baseline update"
else
    CHANGES_OCCURRED=false
    log_info "No package changes detected on either VM"
fi

# Only proceed with reboot check and baseline update if changes occurred
if [[ "$CHANGES_OCCURRED" == "true" ]]; then
    # Check reboot requirements in parallel
    log_step "Checking reboot requirements..."
    check_reboot_required "$VM1_NAME" "$PC1_IP" &
    PID1=$!
    check_reboot_required "$VM2_NAME" "$PC2_IP" &
    PID2=$!

    # Use || EXIT=$? to prevent ERR trap firing (set +e disables errexit but not ERR trap)
    # check_reboot_required returns: 0=reboot needed, 1=no reboot, 2+=error
    EXIT1=0
    wait $PID1 || EXIT1=$?
    EXIT2=0
    wait $PID2 || EXIT2=$?

    if [[ $EXIT1 -eq 0 ]]; then
        PC1_NEEDS_REBOOT=true
    elif [[ $EXIT1 -eq 1 ]]; then
        PC1_NEEDS_REBOOT=false
    else
        log_error "Failed to check reboot requirement on $VM1_NAME"
        exit 1
    fi

    if [[ $EXIT2 -eq 0 ]]; then
        PC2_NEEDS_REBOOT=true
    elif [[ $EXIT2 -eq 1 ]]; then
        PC2_NEEDS_REBOOT=false
    else
        log_error "Failed to check reboot requirement on $VM2_NAME"
        exit 1
    fi

    # Reboot if needed
    if [[ "$PC1_NEEDS_REBOOT" == "true" || "$PC2_NEEDS_REBOOT" == "true" ]]; then
        log_step "Rebooting VMs..."

        if [[ "$PC1_NEEDS_REBOOT" == "true" ]]; then
            reboot_vm "$VM1_NAME" "$PC1_IP" &
            PID1=$!
        else
            PID1=""
        fi

        if [[ "$PC2_NEEDS_REBOOT" == "true" ]]; then
            reboot_vm "$VM2_NAME" "$PC2_IP" &
            PID2=$!
        else
            PID2=""
        fi

        # Wait for reboots to complete
        if [[ -n "$PID1" ]]; then
            wait $PID1 || {
                log_error "VM reboot failed on $VM1_NAME"
                exit 1
            }
        fi

        if [[ -n "$PID2" ]]; then
            wait $PID2 || {
                log_error "VM reboot failed on $VM2_NAME"
                exit 1
            }
        fi

        log_info "Reboots complete"
    else
        log_info "No reboots needed"
    fi

    # Clear apt cache in parallel
    log_step "Cleaning apt cache..."
    ssh_run "${SSH_USER}@${PC1_IP}" "sudo apt-get clean" >/dev/null 2>&1 &
    PID1=$!
    ssh_run "${SSH_USER}@${PC2_IP}" "sudo apt-get clean" >/dev/null 2>&1 &
    PID2=$!

    # Use || EXIT=$? to prevent ERR trap firing (set +e disables errexit but not ERR trap)
    EXIT1=0
    wait $PID1 || EXIT1=$?
    EXIT2=0
    wait $PID2 || EXIT2=$?

    if [[ $EXIT1 -ne 0 || $EXIT2 -ne 0 ]]; then
        log_error "Failed to clean apt cache on one or more VMs"
        exit 1
    fi
    log_info "Apt cache cleaned"

    # Create new baseline snapshots
    log_step "Creating new baseline snapshots..."
    if ! "$SCRIPT_DIR/internal/create-baseline-snapshots.sh"; then
        log_error "Failed to create baseline snapshots"
        exit 1
    fi
    log_info "Baseline snapshots created"

    log_step "VM upgrade and baseline update complete!"
else
    log_step "VM upgrade complete (no changes detected, baseline not updated)"
fi
